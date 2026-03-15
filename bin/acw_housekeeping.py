from __future__ import annotations

import glob
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Callable


def state_dir_is_writable(state_dir: str) -> tuple[bool, str]:
    try:
        fd, tmp = tempfile.mkstemp(prefix="acw_doctor_", dir=state_dir)
        os.close(fd)
        os.unlink(tmp)
        return True, state_dir
    except OSError as exc:
        return False, f"{state_dir} ({exc})"


def codex_auth_state_available(state_dir: str) -> tuple[bool, str]:
    auth = Path(state_dir) / "auth.json"
    if auth.is_file():
        return True, str(auth)
    return False, str(auth)


@dataclass(frozen=True)
class CleanupContext:
    daemon_pid_file_for_scope: Callable[[], str]
    daemon_socket_file_for_scope: Callable[[], str]
    daemon_pid_alive: Callable[[str], str]
    watcher_rows: Callable[[], list[dict[str, str]]]
    key_from_pane: Callable[[str], str]
    is_thread_id: Callable[[str], bool]
    watch_log_for_key: Callable[[str], str]
    state_dir: str
    build_runtime_model: Callable[[], Any]
    tmux_scope_key: Callable[[], str]
    cleanup_rows: Callable[..., None]
    os_kill: Callable[[int, int], None]


def cleanup_stale_files(remove_dead_sessions: bool = False, *, context: CleanupContext) -> None:
    """Remove files and DB rows for managed runtimes that are no longer live."""
    daemon_pid_file = context.daemon_pid_file_for_scope()
    daemon_socket_file = context.daemon_socket_file_for_scope()
    if daemon_pid_file and os.path.exists(daemon_pid_file) and not context.daemon_pid_alive(daemon_pid_file):
        Path(daemon_pid_file).unlink(missing_ok=True)
        Path(daemon_socket_file).unlink(missing_ok=True)

    live_keys: set[str] = set()
    live_runtime_threads: set[str] = set()
    for row in context.watcher_rows():
        key = context.key_from_pane(row["pane"])
        if key:
            live_keys.add(key)
        thread_id = row.get("thread", "")
        if context.is_thread_id(thread_id):
            live_runtime_threads.add(thread_id.lower())

    keep_logs = {context.watch_log_for_key(key) for key in live_keys}
    for path in glob.glob(os.path.join(context.state_dir, "auto_continue_logwatch*.log")):
        if path not in keep_logs:
            Path(path).unlink(missing_ok=True)

    if not remove_dead_sessions:
        return

    model = context.build_runtime_model()
    live_thread_panes = {thread_id.lower() for thread_id in model.panes_by_thread}
    active_threads = live_runtime_threads | live_thread_panes

    def daemon_pid_is_alive(pid_str: str) -> bool:
        if not pid_str.isdigit():
            return False
        try:
            context.os_kill(int(pid_str), 0)
            return True
        except OSError:
            return False

    context.cleanup_rows(
        state_dir=context.state_dir,
        scope=context.tmux_scope_key(),
        active_threads=active_threads,
        daemon_pid_alive=daemon_pid_is_alive,
    )
