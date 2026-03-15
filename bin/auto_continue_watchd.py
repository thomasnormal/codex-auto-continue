#!/usr/bin/env python3
"""Process manager for auto-continue managed sessions."""

from __future__ import annotations

import glob
import io
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import acw_state
import acw_session
import acw_status
import acw_rpc
import acw_daemon
import acw_control
import acw_commands
import acw_observe
import acw_probe
import acw_repair
import acw_runtime
import acw_tmux
import acw_ui
from auto_continue_logwatch import thread_times_from_state_db
from auto_continue_logwatch import thread_from_codex_pid as _thread_from_codex_pid
from auto_continue_logwatch import CodexLogEvent as _LogwatchCodexLogEvent
from auto_continue_logwatch import append_log as _watch_append_log
from auto_continue_logwatch import check_codex_log_tail_for_pending as _check_codex_log_tail_for_pending
from auto_continue_logwatch import check_pane_for_errors as _check_pane_for_errors
from auto_continue_logwatch import check_pane_for_interrupt as _check_pane_for_interrupt
from auto_continue_logwatch import compute_health as _compute_watch_health
from auto_continue_logwatch import now_ts as _watch_now_ts
from auto_continue_logwatch import parse_codex_log_event as _parse_codex_log_event
from auto_continue_logwatch import parse_codex_log_interrupt as _parse_codex_log_interrupt
from auto_continue_logwatch import tmux_send as _watch_tmux_send

# ---------------------------------------------------------------------------
# Constants & path resolution
# ---------------------------------------------------------------------------

THREAD_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

DAEMON_POLL_INTERVAL_SECS = 0.2
DAEMON_IDLE_EXIT_SECS = 2.0
DAEMON_CODEX_LOG_PATH = Path.home() / ".codex" / "log" / "codex-tui.log"
_DAEMON_RUNTIME_ID_SEQ = 0


def _next_daemon_runtime_id() -> str:
    global _DAEMON_RUNTIME_ID_SEQ
    _DAEMON_RUNTIME_ID_SEQ += 1
    return f"{os.getpid()}{_DAEMON_RUNTIME_ID_SEQ:06d}"


def resolve_project_cwd() -> str:
    env = os.environ.get("AUTO_CONTINUE_PROJECT_CWD", "")
    if env:
        if os.path.isdir(env):
            return str(Path(env).resolve())
        return env

    pwd_real = str(Path.cwd().resolve())
    try:
        git_root = subprocess.check_output(
            ["git", "-C", pwd_real, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        git_root = ""
    if git_root:
        return git_root

    if os.path.basename(pwd_real) == ".codex":
        return os.path.dirname(pwd_real)

    return pwd_real


PROJECT_CWD = resolve_project_cwd()
# State dir is always ~/.codex/ regardless of CWD.  Pane IDs are tmux-global,
# so per-project state dirs cause fragmentation: the same pane gets different
# message/pid/state files depending on which directory `acw` was invoked from.
STATE_DIR = os.path.join(os.path.expanduser("~"), ".codex")
SEND_DELAY_SECS = os.environ.get("AUTO_CONTINUE_SEND_DELAY_SECS", "0.25")
ENTER_DELAY_SECS = os.environ.get("AUTO_CONTINUE_ENTER_DELAY_SECS", "0.15")
DEFAULT_MESSAGE_TEXT = (
    "please continue with the workstream.\n\n"
    "Keep momentum, validate changes, and move to the next highest-value task.\n"
)
KNOWN_SHELL_COMMANDS = frozenset(
    command
    for command in {
        os.path.basename(os.environ.get("SHELL", "")).strip().lower(),
        "bash",
        "sh",
        "dash",
        "zsh",
        "fish",
        "ksh",
        "tcsh",
        "csh",
    }
    if command
)

os.makedirs(STATE_DIR, exist_ok=True)

def build_help_text(prog: str) -> str:
    return f"""Usage:
  {prog} status [target] [--details]
  {prog} start <target> [thread-id] [--message TEXT]
  {prog} stop [target]
  {prog} pause [target]
  {prog} resume [target ...]
  {prog} restart [target]
  {prog} edit <target>
  {prog} repair [target] [--dry-run|--all]
  {prog} cleanup
  {prog} doctor [target]

Commands:
  status    Show managed session state. Default command when no subcommand is given.
  start     Start managing a live tmux target. Auto-detect the Codex thread unless you pass one explicitly.
  stop      Stop one managed session, one pane's runtimes, or all managed sessions, and forget their saved sessions.
  pause     Pause one managed session, or all managed sessions when no target is given.
  resume    Restart one or more paused managed sessions, or all paused managed sessions, from current state.
  restart   Restart one managed session or all live panes, including recoverable dead sessions, when no target is given. Use --restart-codex on a single target to relaunch Codex with the same thread.
  edit      Edit the stored continue message for a running managed session, then restart it.
  repair    Inspect or apply safe live-state repairs using the invariant model. With no target it defaults to --dry-run; use --all to apply safe repairs across all panes.
  cleanup   Remove dead saved sessions and stale daemon/runtime files.
  doctor    Check tmux reachability, Codex auth/state, and optional pane/thread health.

Targets:
  Commands with a target accept a tmux pane id (%6), window index (2), session:window (0:2), or exact tmux window name (uvm).
  pause, resume, and restart act on all managed sessions when no target is given. resume also accepts multiple explicit targets.

Examples:
  {prog}
  {prog} status --details
  {prog} start %6 --message "continue"
  {prog} start uvm 019cb235-bc2c-7920-8832-f2d5656fead8
  {prog} resume formal aot
  {prog} restart bugs --restart-codex
  {prog} edit %6
  {prog} repair --dry-run
  {prog} repair %6
  {prog} repair --all
  {prog} restart
  {prog} doctor
"""

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sanitize_key(s: str) -> str:
    return acw_tmux.sanitize_key(s)


def key_from_pane(pane: str) -> str:
    return sanitize_key(pane)


def is_pane_id(s: str) -> bool:
    return bool(re.fullmatch(r"%[0-9]+", s))


def current_tmux_pane() -> str:
    pane = os.environ.get("TMUX_PANE", "").strip()
    return pane if is_pane_id(pane) else ""


def _truncate(s: str, width: int) -> str:
    """Truncate string to *width* chars, adding '...' suffix if needed."""
    return s if len(s) <= width else s[: width - 3] + "..."


def _read_pid_file(path: str) -> str | None:
    """Read a PID file and return the pid string, or None if missing/invalid."""
    try:
        with open(path) as f:
            pid_str = f.read().strip()
    except OSError:
        return None
    return pid_str if pid_str.isdigit() else None


def is_thread_id(s: str) -> bool:
    return bool(THREAD_ID_RE.fullmatch(s))


def watch_log_for_key(key: str) -> str:
    return os.path.join(STATE_DIR, f"auto_continue_logwatch.{key}.log")


def state_file_for_thread(thread_id: str) -> str:
    return acw_state.session_state_ref(thread_id)


def state_db_file() -> str:
    return acw_state.state_db_path(STATE_DIR)


def _read_session_state(thread_id: str) -> dict:
    return acw_session.read_session_state(thread_id, state_dir=STATE_DIR)


def _write_session_state(thread_id: str, data: dict) -> None:
    acw_session.write_session_state(thread_id, data, state_dir=STATE_DIR)


def _remove_session_state(thread_id: str) -> bool:
    return acw_session.remove_session_state(thread_id, state_dir=STATE_DIR)


# ---------------------------------------------------------------------------
# Tmux helpers
# ---------------------------------------------------------------------------


def run_tmux(*args: str) -> str | None:
    return acw_tmux.run_tmux(
        *args,
        preferred_tmux_socket_fn=_preferred_tmux_socket,
        tmux_client_env_healthy_fn=_tmux_client_env_healthy,
        check_output_fn=subprocess.check_output,
    )


def _tmux_socket_from_env() -> str:
    return acw_tmux.tmux_socket_from_env()


def _preferred_tmux_socket() -> str:
    return acw_tmux.preferred_tmux_socket()


def _is_default_tmux_socket_path(sock: str) -> bool:
    return acw_tmux.is_default_tmux_socket_path(sock)


def _tmux_scope_key(socket_path: str | None = None) -> str:
    return acw_tmux.tmux_scope_key(
        socket_path,
        preferred_tmux_socket_fn=_preferred_tmux_socket,
    )


def _tmux_scope_token(socket_path: str | None = None) -> str:
    return acw_tmux.tmux_scope_token(
        socket_path,
        preferred_tmux_socket_fn=_preferred_tmux_socket,
    )


def _tmux_scope_label(socket_path: str | None = None) -> str:
    return acw_tmux.tmux_scope_label(
        socket_path,
        preferred_tmux_socket_fn=_preferred_tmux_socket,
    )


def _session_scope_matches_current(data: dict) -> bool:
    return acw_tmux.session_scope_matches_current(
        data,
        preferred_tmux_socket_fn=_preferred_tmux_socket,
    )


def daemon_pid_file_for_scope(socket_path: str | None = None) -> str:
    return acw_tmux.daemon_pid_file_for_scope(
        STATE_DIR,
        socket_path,
        preferred_tmux_socket_fn=_preferred_tmux_socket,
    )


def daemon_log_file_for_scope(socket_path: str | None = None) -> str:
    return acw_tmux.daemon_log_file_for_scope(
        STATE_DIR,
        socket_path,
        preferred_tmux_socket_fn=_preferred_tmux_socket,
    )


def _daemon_socket_root() -> str:
    return acw_tmux.daemon_socket_root()


def daemon_socket_file_for_scope(socket_path: str | None = None) -> str:
    return acw_tmux.daemon_socket_file_for_scope(
        socket_path,
        preferred_tmux_socket_fn=_preferred_tmux_socket,
    )


def daemon_runtime_file_for_thread(thread_id: str, socket_path: str | None = None) -> str:
    return acw_tmux.daemon_runtime_file_for_thread(
        state_db_file(),
        thread_id,
        socket_path,
        preferred_tmux_socket_fn=_preferred_tmux_socket,
    )


def daemon_runtime_glob_for_scope(socket_path: str | None = None) -> str:
    return acw_tmux.daemon_runtime_glob_for_scope(
        state_db_file(),
        socket_path,
        preferred_tmux_socket_fn=_preferred_tmux_socket,
    )


def _write_runtime_state(thread_id: str, data: dict, socket_path: str | None = None) -> None:
    acw_state.write_runtime_state(
        thread_id,
        data,
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(socket_path),
    )


def _remove_runtime_state(thread_id: str, socket_path: str | None = None) -> None:
    acw_state.remove_runtime_state(
        thread_id,
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(socket_path),
    )


def _read_runtime_rows(socket_path: str | None = None) -> list[dict[str, str]]:
    rows = acw_state.read_runtime_rows(
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(socket_path),
    )
    return [{str(key): value for key, value in row.items()} for row in rows]


def _read_daemon_scope_state(socket_path: str | None = None) -> dict[str, str]:
    row = acw_state.read_daemon_scope_state(
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(socket_path),
    )
    return {str(key): str(value) for key, value in row.items()}


def _write_daemon_scope_state(
    *,
    daemon_pid: str,
    socket_path: str,
    heartbeat_ts: str,
    started_ts: str = "",
    scope: str | None = None,
) -> None:
    acw_state.write_daemon_scope_state(
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(scope),
        daemon_pid=daemon_pid,
        socket_path=socket_path,
        heartbeat_ts=heartbeat_ts,
        started_ts=started_ts,
    )


def _tmux_client_env_healthy() -> bool:
    return acw_tmux.tmux_client_env_healthy(
        check_output_fn=subprocess.check_output,
    )


def _tmux_server_pids() -> list[str]:
    return acw_tmux.tmux_server_pids(
        preferred_tmux_socket_fn=_preferred_tmux_socket,
        check_output_fn=subprocess.check_output,
    )


def _tmux_socket_recovery_hint() -> str:
    return acw_tmux.tmux_socket_recovery_hint(
        preferred_tmux_socket_fn=_preferred_tmux_socket,
        tmux_server_pids_fn=_tmux_server_pids,
    )


def _print_tmux_unavailable_hint(target: str) -> None:
    acw_tmux.print_tmux_unavailable_hint(
        target,
        tmux_socket_recovery_hint_fn=_tmux_socket_recovery_hint,
        file=sys.stderr,
    )


def resolve_pane_from_window_target(target: str, quiet: bool = False) -> str | None:
    return acw_tmux.resolve_pane_from_window_target(
        target,
        run_tmux_fn=run_tmux,
        is_pane_id_fn=is_pane_id,
        print_tmux_unavailable_hint_fn=_print_tmux_unavailable_hint,
        quiet=quiet,
    )


def resolve_pane_from_window_name(name: str, quiet: bool = False) -> str | None:
    return acw_tmux.resolve_pane_from_window_name(
        name,
        run_tmux_fn=run_tmux,
        is_pane_id_fn=is_pane_id,
        quiet=quiet,
    )


def resolve_pane_target(target: str) -> str:
    return acw_tmux.resolve_pane_target(
        target,
        is_pane_id_fn=is_pane_id,
        resolve_pane_from_window_target_fn=resolve_pane_from_window_target,
        resolve_pane_from_window_name_fn=resolve_pane_from_window_name,
        tmux_socket_recovery_hint_fn=_tmux_socket_recovery_hint,
        print_tmux_unavailable_hint_fn=_print_tmux_unavailable_hint,
    )


def _resolve_live_pane_target(target: str) -> str:
    return acw_tmux.resolve_live_pane_target(
        target,
        is_pane_id_fn=is_pane_id,
        resolve_pane_from_window_target_fn=resolve_pane_from_window_target,
        resolve_pane_from_window_name_fn=resolve_pane_from_window_name,
    )


def resolve_start_pane_target(target: str) -> str:
    return acw_tmux.resolve_start_pane_target(
        target,
        is_pane_id_fn=is_pane_id,
        is_thread_id_fn=is_thread_id,
        resolve_pane_from_window_target_fn=resolve_pane_from_window_target,
        resolve_pane_from_window_name_fn=resolve_pane_from_window_name,
        tmux_socket_recovery_hint_fn=_tmux_socket_recovery_hint,
        print_tmux_unavailable_hint_fn=_print_tmux_unavailable_hint,
    )


# ---------------------------------------------------------------------------
# Thread ID detection
# ---------------------------------------------------------------------------


def resolve_thread_id(pane: str, requested: str = "", *, model: RuntimeModel | None = None) -> str:
    if requested and requested != "auto":
        if is_thread_id(requested):
            return requested.lower()
        print(f"error: invalid thread_id '{requested}'", file=sys.stderr)
        sys.exit(1)

    if model is None:
        model = _build_runtime_model()
    tid, thread_error = _strict_thread_for_pane(model, pane)
    if tid and is_thread_id(tid):
        return tid

    error_text = thread_error or f"could not determine thread_id for pane={pane}; start requires a concrete thread id"
    print(f"error: {error_text}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Message argument parsing
# ---------------------------------------------------------------------------


def parse_thread_and_message_args(
    args: list[str],
) -> tuple[str, str, bool]:
    return acw_commands.parse_thread_and_message_args(args)


def parse_restart_args(
    args: list[str],
) -> tuple[str, str, bool, bool]:
    return acw_commands.parse_restart_args(args)


def _parse_restartable_args(
    args: list[str],
    allow_restart_codex: bool = False,
) -> tuple[str, str, bool, bool]:
    return acw_commands.parse_restartable_args(args, allow_restart_codex=allow_restart_codex)


# ---------------------------------------------------------------------------
# Message meta persistence
# ---------------------------------------------------------------------------


def _normalize_message_text(text: str) -> str:
    return acw_session.normalize_message_text(text)


def _message_text(value: str) -> str:
    return acw_session.message_text(value)


def _set_thread_name(thread_id: str, name: str) -> None:
    acw_session.set_thread_name(
        thread_id,
        name,
        is_thread_id_fn=is_thread_id,
        write_session_state_fn=_write_session_state,
    )


def ensure_tmux_window_rename_hook() -> None:
    acw_session.ensure_tmux_window_rename_hook(
        python_executable=sys.executable,
        entrypoint=str(Path(__file__).resolve()),
        run_tmux_fn=run_tmux,
    )


def cmd_window_renamed(argv: list[str]) -> None:
    acw_session.handle_window_renamed(
        argv,
        run_tmux_fn=run_tmux,
        watcher_rows_fn=watcher_rows,
        is_pane_id_fn=is_pane_id,
        is_thread_id_fn=is_thread_id,
        set_thread_name_fn=_set_thread_name,
    )


# ---------------------------------------------------------------------------
# Runtime row access
# ---------------------------------------------------------------------------


def watcher_rows(pane_filter: str = "") -> list[dict[str, str]]:
    """Return daemon-managed runtime rows for the current tmux socket scope."""
    rows = _read_runtime_rows()
    if pane_filter:
        rows = [row for row in rows if row.get("pane", "") == pane_filter]
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (
            row.get("pid", ""),
            row.get("pane", ""),
            row.get("thread", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def watcher_pids_for_pane(pane: str) -> list[str]:
    return [r["pid"] for r in watcher_rows(pane)]


def _watcher_row_for_pane(pane: str) -> dict[str, str] | None:
    rows = watcher_rows(pane)
    if len(rows) == 1:
        return rows[0]
    return None


def _system_exit_detail(exc: SystemExit, stderr_text: str = "") -> str:
    return acw_commands.system_exit_detail(exc, stderr_text)


def _other_watcher_for_thread(pane: str, thread_id: str) -> dict[str, str] | None:
    if not is_thread_id(thread_id):
        return None
    for row in watcher_rows():
        if row.get("pane", "") == pane:
            continue
        row_tid = row.get("thread", "")
        if row_tid and row_tid.lower() == thread_id.lower():
            return row
    return None


def _is_pid_stopped(pid: int) -> bool:
    """Check if a process is in stopped (T) state via ``ps``."""
    try:
        state = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "state="],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return state.startswith("T")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return False


def thread_from_running_watcher_for_pane(pane: str) -> str | None:
    rows = watcher_rows(pane)
    if len(rows) != 1:
        return None
    tid = rows[0]["thread"]
    if is_thread_id(tid):
        return tid.lower()
    return None


# ---------------------------------------------------------------------------
# Daemon runtime
# ---------------------------------------------------------------------------

_DaemonCodexLogTail = acw_daemon.CodexLogTail
_DaemonSessionRuntime = acw_daemon.SessionRuntime


def _daemon_scope_env(socket_path: str) -> dict[str, str]:
    env = dict(os.environ)
    if socket_path:
        env["AUTO_CONTINUE_TMUX_SOCKET"] = socket_path
    else:
        env.pop("AUTO_CONTINUE_TMUX_SOCKET", None)
    return env


def _daemon_pid_alive(pid_file: str) -> str:
    return acw_control.daemon_pid_alive(
        pid_file,
        context=acw_control.DaemonProcessContext(
            read_pid_file=_read_pid_file,
            current_scope_key=_tmux_scope_key,
            current_scope_label=_tmux_scope_label,
            daemon_pid_file_for_scope=daemon_pid_file_for_scope,
            daemon_socket_file_for_scope=daemon_socket_file_for_scope,
            daemon_log_file_for_scope=daemon_log_file_for_scope,
            daemon_scope_env=_daemon_scope_env,
            daemon_entrypoint=str(Path(__file__).resolve()),
        ),
    )


def _stop_existing_daemon(pid_str: str, pid_file: str, socket_file: str) -> None:
    acw_control.stop_existing_daemon(
        pid_str,
        pid_file,
        socket_file,
        context=acw_control.DaemonProcessContext(
            read_pid_file=_read_pid_file,
            current_scope_key=_tmux_scope_key,
            current_scope_label=_tmux_scope_label,
            daemon_pid_file_for_scope=daemon_pid_file_for_scope,
            daemon_socket_file_for_scope=daemon_socket_file_for_scope,
            daemon_log_file_for_scope=daemon_log_file_for_scope,
            daemon_scope_env=_daemon_scope_env,
            daemon_entrypoint=str(Path(__file__).resolve()),
        ),
    )


def _ensure_daemon_running() -> str:
    return acw_control.ensure_daemon_running(
        context=acw_control.DaemonProcessContext(
            read_pid_file=_read_pid_file,
            current_scope_key=_tmux_scope_key,
            current_scope_label=_tmux_scope_label,
            daemon_pid_file_for_scope=daemon_pid_file_for_scope,
            daemon_socket_file_for_scope=daemon_socket_file_for_scope,
            daemon_log_file_for_scope=daemon_log_file_for_scope,
            daemon_scope_env=_daemon_scope_env,
            daemon_entrypoint=str(Path(__file__).resolve()),
        )
    )


def _current_session_generation(data: dict) -> int:
    return acw_control.current_session_generation(data)


def _normalize_runtime_row(data: dict[str, object] | None) -> dict[str, str]:
    return acw_control.normalize_runtime_row(data)


def _daemon_rpc_call(
    command: str,
    params: dict[str, object],
    *,
    timeout_secs: float,
) -> dict[str, object]:
    return acw_control.daemon_rpc_call(
        command,
        params,
        timeout_secs=timeout_secs,
        context=acw_control.DaemonRpcClientContext(
            ensure_daemon_running=_ensure_daemon_running,
            current_socket_file=daemon_socket_file_for_scope,
            current_log_file=daemon_log_file_for_scope,
            write_daemon_scope_state=_write_daemon_scope_state,
            watch_now_ts=_watch_now_ts,
            current_scope_key=_tmux_scope_key,
            print_progress=_print_progress,
        ),
    )


def _daemon_rpc_upsert_session(
    thread_id: str,
    *,
    pane: str,
    name: str,
    message: str,
    paused: bool,
    generation: int,
    extra_session: dict[str, object] | None = None,
    timeout_secs: float = 5.0,
) -> dict[str, str]:
    return acw_control.daemon_rpc_upsert_session(
        thread_id,
        pane=pane,
        name=name,
        message=message,
        paused=paused,
        generation=generation,
        daemon_rpc_call_fn=_daemon_rpc_call,
        message_text=_message_text,
        tmux_scope_key=_tmux_scope_key,
        extra_session=extra_session,
        timeout_secs=timeout_secs,
    )


def _daemon_rpc_patch_session(
    thread_id: str,
    changes: dict[str, object],
    *,
    wait: dict[str, object] | None = None,
    timeout_secs: float = 5.0,
) -> dict[str, str]:
    return acw_control.daemon_rpc_patch_session(
        thread_id,
        changes,
        daemon_rpc_call_fn=_daemon_rpc_call,
        wait=wait,
        timeout_secs=timeout_secs,
    )


def _daemon_rpc_remove_session(thread_id: str, *, timeout_secs: float = 2.0) -> None:
    acw_control.daemon_rpc_remove_session(
        thread_id,
        daemon_rpc_call_fn=_daemon_rpc_call,
        timeout_secs=timeout_secs,
    )


def _daemon_rpc_restart_codex(pane: str, thread_id: str, *, timeout_secs: float = 10.0) -> None:
    acw_control.daemon_rpc_restart_codex(
        pane,
        thread_id,
        daemon_rpc_call_fn=_daemon_rpc_call,
        timeout_secs=timeout_secs,
    )


def _session_signature(session: SavedSessionRecord) -> tuple[str, str, int]:
    return acw_control.session_signature(session)


def _load_scoped_session_records(socket_path: str | None = None) -> list[SavedSessionRecord]:
    return acw_control.load_scoped_session_records(
        socket_path,
        context=acw_control.ScopedSessionContext(
            load_sessions=_load_sessions,
            normalize_tmux_socket=_tmux_scope_key,
        ),
    )


def _daemon_runtime_payload(runtime: _DaemonSessionRuntime) -> dict:
    return acw_control.daemon_runtime_payload(runtime, now_ts=_watch_now_ts)


def _drain_daemon_codex_log_events(tail: _DaemonCodexLogTail) -> list[_LogwatchCodexLogEvent]:
    return acw_daemon.drain_codex_log_events(
        tail,
        parse_interrupt=_parse_codex_log_interrupt,
        parse_event=_parse_codex_log_event,
        event_cls=_LogwatchCodexLogEvent,
    )


def _daemon_session_runtime(
    session: SavedSessionRecord,
    *,
    codex_log_path: Path,
) -> _DaemonSessionRuntime:
    state_file = session.state_file or state_file_for_thread(session.thread_id)
    watch_log = watch_log_for_key(key_from_pane(session.pane or session.thread_id))
    return acw_daemon.daemon_session_runtime(
        session,
        codex_log_path=codex_log_path,
        state_file=state_file,
        watch_log=watch_log,
        project_cwd=PROJECT_CWD,
        message_text=_message_text,
        read_session_state=_read_session_state,
        write_session_state=_write_session_state,
        append_log=_watch_append_log,
        check_pending=_check_codex_log_tail_for_pending,
        event_cls=_LogwatchCodexLogEvent,
        next_runtime_id=_next_daemon_runtime_id,
    )


def _daemon_mark_handled(runtime: _DaemonSessionRuntime, turn_id: str, thread_id: str) -> None:
    acw_daemon.daemon_mark_handled(runtime, turn_id, thread_id, write_session_state=_write_session_state)


def _daemon_skip_interrupted_turn(
    runtime: _DaemonSessionRuntime,
    turn_id: str,
    thread_id: str,
    reason: str,
) -> None:
    acw_daemon.daemon_skip_interrupted_turn(
        runtime,
        turn_id,
        thread_id,
        reason,
        write_session_state=_write_session_state,
        append_log=_watch_append_log,
    )


def _daemon_pause_session(runtime: _DaemonSessionRuntime, reason: str) -> None:
    acw_daemon.daemon_pause_session(
        runtime,
        reason,
        write_session_state=_write_session_state,
        append_log=_watch_append_log,
        now_ts=_watch_now_ts,
    )


def _daemon_process_session_events(
    runtime: _DaemonSessionRuntime,
    events: list[_LogwatchCodexLogEvent],
) -> None:
    acw_daemon.daemon_process_session_events(
        runtime,
        events,
        check_pane_for_interrupt=_check_pane_for_interrupt,
        check_pane_for_errors=_check_pane_for_errors,
        tmux_send=_watch_tmux_send,
        compute_watch_health=_compute_watch_health,
        now_ts=_watch_now_ts,
        append_log=_watch_append_log,
        write_session_state=_write_session_state,
        send_delay_secs=SEND_DELAY_SECS,
        enter_delay_secs=ENTER_DELAY_SECS,
        time_module=time,
    )


def _daemon_refresh_session_health(
    runtime: _DaemonSessionRuntime,
    *,
    codex_log_exists: bool,
    now: float,
) -> None:
    acw_daemon.daemon_refresh_session_health(
        runtime,
        codex_log_exists=codex_log_exists,
        now=now,
        compute_watch_health=_compute_watch_health,
        append_log=_watch_append_log,
        write_session_state=_write_session_state,
        now_ts=_watch_now_ts,
    )


def _daemon_runtime_events(
    runtime: _DaemonSessionRuntime,
    events_by_thread: dict[str, list[_LogwatchCodexLogEvent]],
) -> list[_LogwatchCodexLogEvent]:
    return acw_daemon.daemon_runtime_events(runtime, events_by_thread)


def _daemon_runtime_row(thread_id: str, runtime: _DaemonSessionRuntime | None) -> dict[str, str]:
    return acw_control.daemon_runtime_row(
        thread_id,
        _daemon_runtime_payload(runtime) if runtime is not None else None,
    )


def _daemon_reconcile_once(
    scope: str,
    *,
    log_tail: _DaemonCodexLogTail,
    runtimes: dict[str, _DaemonSessionRuntime],
) -> int:
    return acw_control.daemon_reconcile_once(
        scope,
        log_tail=log_tail,
        runtimes=runtimes,
        context=acw_control.DaemonReconcileContext(
            drain_daemon_codex_log_events=_drain_daemon_codex_log_events,
            load_scoped_session_records=_load_scoped_session_records,
            remove_runtime_state=_remove_runtime_state,
            daemon_session_runtime=_daemon_session_runtime,
            message_text=_message_text,
            daemon_process_session_events=_daemon_process_session_events,
            daemon_runtime_events=_daemon_runtime_events,
            daemon_refresh_session_health=_daemon_refresh_session_health,
            daemon_runtime_payload=_daemon_runtime_payload,
            write_runtime_state=_write_runtime_state,
            append_log=_watch_append_log,
            codex_log_path=DAEMON_CODEX_LOG_PATH,
            is_thread_id=is_thread_id,
            time_module=time,
        ),
    )


def _daemon_wait_for_condition(
    scope: str,
    *,
    thread_id: str,
    wait: dict[str, object],
    log_tail: _DaemonCodexLogTail,
    runtimes: dict[str, _DaemonSessionRuntime],
    send_progress,
    timeout_secs: float,
) -> dict[str, str]:
    return acw_control.daemon_wait_for_condition(
        scope,
        thread_id=thread_id,
        wait=wait,
        log_tail=log_tail,
        runtimes=runtimes,
        send_progress=send_progress,
        timeout_secs=timeout_secs,
        context=acw_control.DaemonWaitContext(
            daemon_reconcile_once=_daemon_reconcile_once,
            daemon_runtime_row=_daemon_runtime_row,
            current_session_generation=_current_session_generation,
            time_module=time,
        ),
    )


def _daemon_wait_matches(row: dict[str, str], wait: dict[str, object]) -> bool:
    return acw_control.daemon_wait_matches(
        row,
        wait,
        current_session_generation_fn=_current_session_generation,
    )


def _restart_codex_in_pane_result(pane: str, thread_id: str, *, progress) -> tuple[bool, str]:
    return acw_control.restart_codex_in_pane_result(
        pane,
        thread_id,
        progress=progress,
        context=acw_control.RestartCodexContext(
            wait_for_shell_prompt=_wait_for_shell_prompt,
            terminate_codex_processes_in_pane=_terminate_codex_processes_in_pane,
            run_tmux=run_tmux,
            wait_for_codex_process=_wait_for_codex_process,
            accept_directory_trust_prompt_in_pane=_accept_directory_trust_prompt_in_pane,
            print_progress=_print_progress,
        ),
    )


def _daemon_handle_rpc_request(
    request: dict[str, object],
    *,
    scope: str,
    log_tail: _DaemonCodexLogTail,
    runtimes: dict[str, _DaemonSessionRuntime],
    send_progress,
) -> dict[str, object]:
    return acw_control.daemon_handle_rpc_request(
        request,
        scope=scope,
        log_tail=log_tail,
        runtimes=runtimes,
        send_progress=send_progress,
        context=acw_control.DaemonRpcServerContext(
            is_thread_id=is_thread_id,
            write_session_state=_write_session_state,
            remove_session_state=_remove_session_state,
            daemon_wait_for_condition=_daemon_wait_for_condition,
            restart_codex_in_pane_result=lambda pane, thread_id, progress_fn: _restart_codex_in_pane_result(
                pane,
                thread_id,
                progress=progress_fn,
            ),
        ),
    )


def _daemon_process_rpc_requests(
    rpc_server,
    *,
    scope: str,
    log_tail: _DaemonCodexLogTail,
    runtimes: dict[str, _DaemonSessionRuntime],
) -> int:
    return acw_control.daemon_process_rpc_requests(
        rpc_server,
        scope=scope,
        log_tail=log_tail,
        runtimes=runtimes,
        context=acw_control.DaemonRpcServeContext(
            daemon_handle_rpc_request=_daemon_handle_rpc_request,
        ),
    )


def _daemon_main(argv: list[str]) -> None:
    acw_control.daemon_main(
        argv,
        context=acw_control.DaemonMainContext(
            normalize_tmux_socket=_tmux_scope_key,
            daemon_pid_file_for_scope=daemon_pid_file_for_scope,
            daemon_socket_file_for_scope=daemon_socket_file_for_scope,
            daemon_log_tail_cls=_DaemonCodexLogTail,
            codex_log_path=DAEMON_CODEX_LOG_PATH,
            write_daemon_scope_state=_write_daemon_scope_state,
            watch_now_ts=_watch_now_ts,
            daemon_process_rpc_requests=_daemon_process_rpc_requests,
            daemon_reconcile_once=_daemon_reconcile_once,
            remove_runtime_state=_remove_runtime_state,
            daemon_poll_interval_secs=DAEMON_POLL_INTERVAL_SECS,
            daemon_idle_exit_secs=DAEMON_IDLE_EXIT_SECS,
            time_module=time,
        ),
    )


def _edit_message_interactive(initial: str = "") -> str | None:
    """Open $EDITOR with *initial* text and return the edited content.

    Returns None if the editor exits with an error or the user leaves the
    content empty.  Strips a single trailing newline that editors tend to add.
    """
    editor = os.environ.get("EDITOR", "vim")
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="acw_message_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(initial)
        rc = subprocess.call([editor, tmp])
        if rc != 0:
            print("editor exited with error, message unchanged", file=sys.stderr)
            return None
        with open(tmp) as f:
            new_value = f.read()
        # Strip a single trailing newline that editors tend to add.
        if new_value.endswith("\n") and not initial.endswith("\n"):
            new_value = new_value[:-1]
        return new_value
    finally:
        Path(tmp).unlink(missing_ok=True)
def _validate_existing_pane_watcher(
    pane: str,
    requested_thread: str = "",
    model: RuntimeModel | None = None,
) -> tuple[str, str] | None:
    return acw_commands.validate_existing_pane_watcher(
        _command_context(),
        pane,
        requested_thread,
        model=model,
    )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_start(argv: list[str]) -> None:
    acw_commands.cmd_start(_command_context(), argv)


def stop_pane_watchers(pane: str) -> None:
    acw_commands.stop_pane_watchers(_command_context(), pane)


def _stop_pane_watchers_keep_session(pane: str) -> None:
    acw_commands.stop_pane_watchers_keep_session(_command_context(), pane)


def _find_watchers_by_window_name(name: str) -> list[dict[str, str]]:
    return acw_commands.find_watchers_by_window_name(_command_context(), name)


def _stop_watcher_rows(rows: list[dict[str, str]]) -> None:
    acw_commands.stop_watcher_rows(_command_context(), rows)


def cmd_stop(argv: list[str]) -> None:
    acw_commands.cmd_stop(
        _command_context(),
        argv,
        stop_pane_watchers_fn=stop_pane_watchers,
    )


def cmd_pause(argv: list[str]) -> None:
    acw_commands.cmd_pause(_command_context(), argv)


def cmd_resume(argv: list[str]) -> None:
    acw_commands.cmd_resume(
        _command_context(),
        argv,
        restart_panes_fn=_restart_panes,
        restart_one_fn=_restart_one,
    )


def _restart_panes(panes: list[str]) -> None:
    acw_commands.restart_panes(
        _command_context(),
        panes,
        stop_pane_watchers_fn=stop_pane_watchers,
        restart_one_fn=_restart_one,
    )


def _accept_directory_trust_prompt_in_pane(pane: str, timeout_secs: float = 2.0) -> bool:
    return acw_control.accept_directory_trust_prompt_in_pane(
        pane,
        timeout_secs=timeout_secs,
        context=acw_control.TrustPromptContext(
            run_tmux=run_tmux,
            time_module=time,
        ),
    )


def _command_name_for_pid(pid: str) -> str:
    return acw_control.command_name_for_pid(pid, check_output_fn=subprocess.check_output)


def _pane_command_snapshot(pane: str) -> tuple[str, str, str, str]:
    return acw_control.pane_command_snapshot(
        pane,
        context=acw_control.PaneCommandContext(
            run_tmux=run_tmux,
            command_name_for_pid=_command_name_for_pid,
        ),
    )


def _wait_for_shell_prompt(
    pane: str,
    *,
    attempts: int = 30,
    poll_interval_secs: float = 0.1,
) -> tuple[bool, str]:
    return acw_control.wait_for_shell_prompt(
        pane,
        attempts=attempts,
        poll_interval_secs=poll_interval_secs,
        context=acw_control.WaitForShellContext(
            pane_command_snapshot=_pane_command_snapshot,
            known_shell_commands=KNOWN_SHELL_COMMANDS,
            time_module=time,
        ),
    )


def _wait_for_codex_process(
    pane: str,
    *,
    attempts: int = 30,
    poll_interval_secs: float = 0.1,
) -> tuple[bool, str]:
    return acw_control.wait_for_codex_process(
        pane,
        attempts=attempts,
        poll_interval_secs=poll_interval_secs,
        context=acw_control.WaitForCodexContext(
            pane_command_snapshot=_pane_command_snapshot,
            codex_pids_in_process_tree=_codex_pids_in_process_tree,
            time_module=time,
        ),
    )


def _terminate_codex_processes_in_pane(pane: str) -> tuple[bool, str]:
    return acw_control.terminate_codex_processes_in_pane(
        pane,
        context=acw_control.TerminateCodexContext(
            pane_command_snapshot=_pane_command_snapshot,
            known_shell_commands=KNOWN_SHELL_COMMANDS,
            codex_top_level_descendants=_codex_top_level_descendants,
            os_kill=os.kill,
            sigterm=signal.SIGTERM,
        ),
    )


def _restart_codex_in_pane(pane: str, thread_id: str) -> None:
    acw_control.restart_codex_in_pane(
        pane,
        thread_id,
        context=acw_control.RestartCodexContext(
            wait_for_shell_prompt=_wait_for_shell_prompt,
            terminate_codex_processes_in_pane=_terminate_codex_processes_in_pane,
            run_tmux=run_tmux,
            wait_for_codex_process=_wait_for_codex_process,
            accept_directory_trust_prompt_in_pane=_accept_directory_trust_prompt_in_pane,
            print_progress=_print_progress,
        ),
    )


def _restart_one(target: str, pane: str, rest: list[str]) -> None:
    acw_commands.restart_one(_command_context(), target, pane, rest)


def cmd_restart(argv: list[str]) -> None:
    acw_commands.cmd_restart(
        _command_context(),
        argv,
        restart_panes_fn=_restart_panes,
        restart_one_fn=_restart_one,
    )


def cmd_edit(argv: list[str]) -> None:
    acw_commands.cmd_edit(
        _command_context(),
        argv,
        cmd_restart_fn=cmd_restart,
    )

# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def _parse_repair_args(args: list[str]) -> tuple[str, bool, bool]:
    return acw_commands.parse_repair_args(args)


def _repair_resolve_target(target: str, model: RuntimeModel | None = None) -> tuple[str, str]:
    return acw_commands.repair_resolve_target(_command_context(), target, model=model)


def _print_repair_plan(steps: list[RepairStep], *, dry_run: bool) -> None:
    acw_commands.print_repair_plan(steps, dry_run=dry_run)


def _repair_command_hint(step: RepairStep) -> str:
    return acw_commands.repair_command_hint(step)


def _apply_repair_step(step: RepairStep) -> None:
    acw_commands.apply_repair_step(
        _command_context(),
        step,
        cmd_start_fn=cmd_start,
        stop_pane_watchers_keep_session_fn=_stop_pane_watchers_keep_session,
    )


def cmd_repair(argv: list[str]) -> None:
    acw_commands.cmd_repair(
        _command_context(),
        argv,
        cmd_start_fn=cmd_start,
        stop_pane_watchers_keep_session_fn=_stop_pane_watchers_keep_session,
    )


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------


def _build_pane_window_map() -> dict[str, str]:
    return acw_probe.build_pane_window_map(run_tmux)


def _load_process_graph() -> tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str]]:
    return acw_probe.load_process_graph(subprocess.check_output)


def _load_process_snapshot() -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    return acw_probe.load_process_snapshot(subprocess.check_output)


def _codex_pids_in_process_tree(
    shell_pid: str,
    process_snapshot: tuple[dict[str, tuple[str, ...]], dict[str, str]] | None = None,
) -> list[str]:
    if process_snapshot is None:
        process_snapshot = _load_process_snapshot()
    return acw_probe.codex_pids_in_process_tree(shell_pid, process_snapshot)


def _threads_from_process_tree(
    shell_pid: str,
    process_snapshot: tuple[dict[str, tuple[str, ...]], dict[str, str]] | None = None,
) -> list[str]:
    if process_snapshot is None:
        process_snapshot = _load_process_snapshot()
    return acw_probe.threads_from_process_tree(shell_pid, process_snapshot, _thread_from_codex_pid)


def _codex_top_level_descendants(
    shell_pid: str,
    process_graph: tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str]] | None = None,
) -> list[str]:
    if process_graph is None:
        process_graph = _load_process_graph()
    return acw_probe.codex_top_level_descendants(shell_pid, process_graph)


def _pane_thread_probe(
    pane_id: str,
    shell_pid: str,
    process_snapshot: tuple[dict[str, tuple[str, ...]], dict[str, str]] | None = None,
) -> tuple[str, str, str, bool]:
    if process_snapshot is None:
        process_snapshot = _load_process_snapshot()
    return acw_probe.pane_thread_probe(pane_id, shell_pid, process_snapshot, _thread_from_codex_pid)


def _thread_provenance_text(source: str) -> str:
    return acw_probe.thread_provenance_text(source)


def _build_thread_pane_map() -> dict[str, str]:
    return acw_probe.build_thread_pane_map(run_tmux, subprocess.check_output, _thread_from_codex_pid)


def _thread_times(thread_id: str, compact: bool = False) -> tuple[str, str]:
    return acw_probe.thread_times(
        thread_id,
        compact=compact,
        thread_times_from_state_db=thread_times_from_state_db,
        format_age_fn=_format_age,
    )


def _thread_title(thread_id: str) -> str:
    return acw_probe.thread_title(thread_id, state_dir=STATE_DIR)


def _format_age(epoch: float, compact: bool = False) -> str:
    return acw_probe.format_age(epoch, compact=compact)


def _clean_pane_line_candidate(line: str) -> tuple[str, bool]:
    return acw_probe.clean_pane_line_candidate(line)


def _extract_last_agent_snippet(text: str) -> str:
    return acw_probe.extract_last_agent_snippet(text)


def _last_agent_snippet_for_pane(pane: str, thread_id: str = "") -> str:
    return acw_probe.last_agent_snippet_for_pane(pane, thread_id, run_tmux, STATE_DIR)


SavedSessionRecord = acw_runtime.SavedSessionRecord
LiveWatcherRecord = acw_runtime.LiveWatcherRecord
LivePaneRecord = acw_runtime.LivePaneRecord
RuntimeSnapshot = acw_runtime.RuntimeSnapshot
RuntimeModel = acw_runtime.RuntimeModel


def _runtime_context() -> acw_runtime.RuntimeContext:
    return acw_runtime.RuntimeContext(
        load_sessions=_load_sessions,
        watcher_rows=watcher_rows,
        run_tmux=run_tmux,
        load_process_snapshot=_load_process_snapshot,
        pane_thread_probe=_pane_thread_probe,
        is_pane_id=is_pane_id,
        is_thread_id=is_thread_id,
        short_thread_id=_short_thread_id,
        normalize_tmux_socket=_tmux_scope_key,
    )


def _load_session_records() -> list[SavedSessionRecord]:
    return acw_runtime.load_session_records(context=_runtime_context())


def _load_live_watcher_records() -> list[LiveWatcherRecord]:
    return acw_runtime.load_live_watcher_records(context=_runtime_context())


def _list_live_pane_records() -> list[LivePaneRecord]:
    return acw_runtime.list_live_pane_records(context=_runtime_context())


def _build_runtime_snapshot() -> RuntimeSnapshot:
    return acw_runtime.build_runtime_snapshot(context=_runtime_context())


def _build_runtime_model(snapshot: RuntimeSnapshot | None = None) -> RuntimeModel:
    return acw_runtime.build_runtime_model(snapshot, context=_runtime_context())


def _runtime_watchers_for_pane(model: RuntimeModel, pane: str) -> tuple[LiveWatcherRecord, ...]:
    return acw_runtime.runtime_watchers_for_pane(model, pane)


def _runtime_watchers_for_thread(model: RuntimeModel, thread_id: str) -> tuple[LiveWatcherRecord, ...]:
    return acw_runtime.runtime_watchers_for_thread(model, thread_id)


def _runtime_panes_for_thread(model: RuntimeModel, thread_id: str) -> tuple[LivePaneRecord, ...]:
    return acw_runtime.runtime_panes_for_thread(model, thread_id)


def _runtime_session_for_thread(model: RuntimeModel, thread_id: str) -> SavedSessionRecord | None:
    return acw_runtime.runtime_session_for_thread(model, thread_id)


def _runtime_session_name_conflict(model: RuntimeModel, session: SavedSessionRecord | None) -> str:
    return acw_runtime.runtime_session_name_conflict(model, session, context=_runtime_context())


def _strict_thread_for_pane(model: RuntimeModel, pane: str) -> tuple[str, str]:
    return acw_runtime.strict_thread_for_pane(model, pane, context=_runtime_context())


def _strict_session_for_thread(model: RuntimeModel, thread_id: str) -> tuple[SavedSessionRecord | None, str]:
    return acw_runtime.strict_session_for_thread(model, thread_id, context=_runtime_context())


def _watcher_anomalies(model: RuntimeModel, watcher: LiveWatcherRecord) -> list[str]:
    return acw_runtime.watcher_anomalies(model, watcher, context=_runtime_context())


def _unwatched_pane_anomalies(model: RuntimeModel, pane: LivePaneRecord) -> list[str]:
    return acw_runtime.unwatched_pane_anomalies(model, pane, context=_runtime_context())


def _unresolved_live_pane_anomalies(pane: LivePaneRecord) -> list[str]:
    return acw_runtime.unresolved_live_pane_anomalies(pane)


def _status_row_detail(row: dict[str, str], sj: dict[str, str]) -> str:
    detail = row.get("anomaly_detail", "").strip()
    if detail:
        return detail
    runtime_detail = row.get("runtime_detail", "").strip()
    if runtime_detail:
        return runtime_detail
    return sj.get("health_detail", "").strip()


def _build_status_entries_from_snapshot(
    snapshot: RuntimeSnapshot,
) -> list[tuple[dict[str, str], str, str]]:
    return acw_runtime.build_status_entries_from_snapshot(snapshot, context=_runtime_context())


def _read_state_json(state_path: str) -> dict[str, str]:
    return acw_status.read_state_json(
        state_path,
        state_dir=STATE_DIR,
        session_ref_prefix=acw_state.SESSION_REF_PREFIX,
        read_state_ref_fn=acw_state.read_state_ref,
    )


def _resolve_message(r: dict[str, str]) -> tuple[str, str]:
    return acw_status.resolve_message(
        r,
        is_thread_id_fn=is_thread_id,
        read_session_state_fn=_read_session_state,
    )


def _short_thread_id(thread_id: str) -> str:
    return acw_status.short_thread_id(thread_id, is_thread_id_fn=is_thread_id)


def _compute_state(r: dict[str, str], sj: dict[str, str]) -> str:
    return acw_status.compute_state(
        r,
        sj,
        is_pid_stopped_fn=_is_pid_stopped,
    )


def _message_summary_for_row(r: dict[str, str]) -> str:
    return acw_status.message_summary_for_row(
        r,
        resolve_message_fn=_resolve_message,
    )


def _styled_state(state: str) -> str:
    return acw_status.styled_state(state)


_DOCTOR_RESULT_STYLES = {
    "ok": "green",
    "warn": "yellow",
    "error": "bold red",
}


def _state_summary(state: str, sj: dict[str, str]) -> str:
    return acw_status.state_summary(
        state,
        sj.get("health_detail", ""),
        truncate_fn=_truncate,
    )


_MAX_MSG_LINES = 3
_STATUS_SECTION_EVERY = 3


def _clamp_visual_lines(text: str, max_lines: int, col_width: int) -> str:
    return acw_ui.clamp_visual_lines(text, max_lines, col_width)


def _strip_rich_markup(text: str) -> str:
    return acw_ui.strip_rich_markup(text)


def _status_window_pane_cell(window_label: str, current_pane: str, thread_id: str, pid: str) -> str:
    return acw_ui.status_window_pane_cell(window_label, current_pane, thread_id, pid)


def _status_flex_widths(term_w: int, fixed_col_widths: list[int]) -> tuple[int, int]:
    return acw_ui.status_flex_widths(term_w, fixed_col_widths)


def _status_table_plain(rows_data: list[tuple[str, str, str, str, str, str]]) -> None:
    acw_ui.status_table_plain(rows_data, section_every=_STATUS_SECTION_EVERY)


def _status_target_for_row(row: dict[str, str], current_pane: str, window_label: str) -> str:
    return acw_status.status_target_for_row(row, current_pane, window_label)


def _sort_session_key(session_name: str) -> tuple[int, object]:
    return acw_status.sort_session_key(session_name)


def _status_sort_key(item: tuple[dict[str, str], str, str]) -> tuple[tuple[int, object], int, int, int, str]:
    return acw_status.status_sort_key(
        item,
        is_pane_id_fn=is_pane_id,
    )


def _status_recommendations(resolved: list[tuple[dict[str, str], str, str]]) -> list[str]:
    return acw_status.status_recommendations(
        resolved,
        compute_state_fn=_compute_state,
        read_state_json_fn=_read_state_json,
        status_target_for_row_fn=_status_target_for_row,
        quote_target_fn=shlex.quote,
    )


def _daemon_scope_alive(scope_state: dict[str, str]) -> bool:
    return acw_status.daemon_scope_alive(scope_state)


def _daemon_scope_down_detail(
    scope_state: dict[str, str],
    *,
    pid_file: str,
    socket_file: str,
) -> str:
    return acw_status.daemon_scope_down_detail(
        scope_state,
        pid_file=pid_file,
        socket_file=socket_file,
    )


def _observed_daemon_scope_state(socket_path: str | None = None) -> dict[str, str]:
    return acw_status.observed_daemon_scope_state(
        socket_path=socket_path,
        read_daemon_scope_state_fn=_read_daemon_scope_state,
        read_pid_file_fn=_read_pid_file,
        daemon_pid_file_for_scope_fn=daemon_pid_file_for_scope,
        daemon_socket_file_for_scope_fn=daemon_socket_file_for_scope,
        daemon_scope_alive_fn=_daemon_scope_alive,
        daemon_scope_down_detail_fn=lambda scope_state, pid_file, socket_file: _daemon_scope_down_detail(
            scope_state,
            pid_file=pid_file,
            socket_file=socket_file,
        ),
    )


def _shared_daemon_error_message(unwatched_rows: list[tuple[dict[str, str], str]]) -> str:
    return acw_status.shared_daemon_error_message(
        unwatched_rows,
        observed_daemon_scope_state_fn=_observed_daemon_scope_state,
        daemon_scope_alive_fn=_daemon_scope_alive,
        tmux_scope_label_fn=_tmux_scope_label,
    )


def _status_invariant_messages(resolved: list[tuple[dict[str, str], str, str]]) -> list[str]:
    return acw_status.status_invariant_messages(
        resolved,
        is_thread_id_fn=is_thread_id,
        short_thread_id_fn=_short_thread_id,
        shared_daemon_error_message_fn=_shared_daemon_error_message,
    )


def _print_status_recommendations(resolved: list[tuple[dict[str, str], str, str]]) -> None:
    commands = _status_recommendations(resolved)
    for cmd in commands:
        print(f"Recommendation: run {cmd}")
    for message in _status_invariant_messages(resolved):
        print(message)


def _status_table(resolved: list[tuple[dict[str, str], str, str]]) -> None:
    # Precompute row data to estimate column widths.
    # (window_pane, state, started, last_acw, last_agent, msg_raw)
    rows_data: list[tuple[str, str, str, str, str, str]] = []
    for r, current_pane, window_label in resolved:
        sj = _read_state_json(r["state"])
        state_value = _compute_state(r, sj)
        state_detail = _status_row_detail(r, sj)
        started, _last_activity = _thread_times(r["thread"], compact=True)
        last_acw = sj.get("last_continue_at", "")
        if last_acw:
            try:
                epoch = time.mktime(time.strptime(last_acw, "%Y-%m-%d %H:%M:%S"))
                last_acw = _format_age(epoch, compact=True)
            except (ValueError, OverflowError):
                pass
        if not last_acw:
            last_acw = "-"
        last_agent = _last_agent_snippet_for_pane(current_pane, r["thread"]) or "-"
        msg_raw = _message_summary_for_row(r)
        window_pane = _status_window_pane_cell(
            window_label,
            current_pane,
            r["thread"],
            r.get("runtime_id") or r.get("pid", ""),
        )
        rows_data.append((
            window_pane,
            _state_summary(state_value, {"health_detail": state_detail}),
            started,
            last_acw,
            last_agent,
            msg_raw,
        ))

    acw_ui.render_status_table(
        rows_data,
        section_every=_STATUS_SECTION_EVERY,
        max_msg_lines=_MAX_MSG_LINES,
    )


def _status_details(
    resolved: list[tuple[dict[str, str], str, str]],
    model: RuntimeModel | None = None,
) -> None:
    if model is None:
        model = _build_runtime_model()
    total = len(resolved)
    for idx, (r, current_pane, window_label) in enumerate(resolved, 1):
        if idx > 1:
            print()

        sj = _read_state_json(r["state"])
        state_value = _compute_state(r, sj)
        state_detail = _status_row_detail(r, sj)

        last_event = "-"
        if r["watch"]:
            try:
                with open(r["watch"]) as f:
                    lines = f.readlines()
                if lines:
                    last_event = lines[-1].rstrip("\n")
            except OSError:
                pass
            if not last_event:
                last_event = "-"

        mode, value = _resolve_message(r)
        if mode == "inline" and value:
            msg_full = value
        else:
            msg_full = "-"

        started, last_activity = _thread_times(r["thread"])
        last_agent = _last_agent_snippet_for_pane(current_pane, r["thread"]) or "-"

        last_acw = sj.get("last_continue_at", "-")

        print(f"=== Entry {idx}/{total} ===")
        print(f"  {'WINDOW:':<16} {window_label}")
        print(f"  {'PANE:':<16} {current_pane or '(no live pane)'}")
        runtime_id = r.get("runtime_id") or r.get("pid", "")
        print(f"  {'ID:':<16} {runtime_id or '-'}")
        print(f"  {'THREAD_ID:':<16} {r['thread'] or 'unknown'}")
        pane_record = model.panes_by_id.get(current_pane) if current_pane else None
        provenance = _thread_provenance_text(pane_record.detected_thread_source) if pane_record else ""
        if provenance:
            print(f"  {'THREAD_PROVENANCE:':<16} {provenance}")
        print(f"  {'STATE:':<16} {state_value}")
        print(f"  {'STARTED:':<16} {started}")
        print(f"  {'LAST_ACTIVITY:':<16} {last_activity}")
        print(f"  {'LAST_ACW:':<16} {last_acw}")
        print(f"  {'LAST_AGENT:':<16} {last_agent}")
        if state_detail:
            label = "ANOMALY" if r.get("anomalies") else "HEALTH_DETAIL"
            print(f"  {f'{label}:':<16} {state_detail}")
        hts = sj.get("health_ts", "")
        if hts:
            print(f"  {'HEALTH_SINCE:':<16} {hts}")
        print(f"  {'LAST_EVENT:':<16} {last_event}")
        print(f"  {'MESSAGE:':<16} {msg_full}")
        if r["watch"]:
            print(f"  {'WATCH_LOG:':<16} {r['watch']}")
        if r["state"]:
            print(f"  {'STATE_REF:':<16} {r['state']}")
    _print_status_recommendations(resolved)


def _load_sessions(socket_path: str | None = None) -> list[dict[str, str]]:
    rows = acw_session.load_sessions(
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(socket_path),
    )
    return [{str(key): value for key, value in row.items()} for row in rows]


def _saved_session_panes_for_target(target: str, model: RuntimeModel | None = None) -> list[str]:
    if model is None:
        model = _build_runtime_model()
    panes: list[str] = []
    seen: set[str] = set()
    if is_thread_id(target):
        sessions: tuple[SavedSessionRecord, ...] = tuple(
            [session] if (session := _runtime_session_for_thread(model, target)) else []
        )
    else:
        sessions = model.sessions_by_name.get(target, ())

    for session in sessions:
        thread_id = session.thread_id
        if is_thread_id(target):
            if thread_id != target.lower():
                continue
        for pane in _runtime_panes_for_thread(model, thread_id):
            if not is_pane_id(pane.pane_id) or pane.pane_id in seen:
                continue
            seen.add(pane.pane_id)
            panes.append(pane.pane_id)
    return panes


def _resolve_restart_pane_target(target: str, model: RuntimeModel | None = None) -> str:
    if is_pane_id(target):
        return target

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target(target)
        if pane and is_pane_id(pane):
            return pane
        print(f"error: could not resolve window target '{target}'", file=sys.stderr)
        sys.exit(1)

    saved_panes = _saved_session_panes_for_target(target, model=model)
    if len(saved_panes) == 1:
        return saved_panes[0]
    if len(saved_panes) > 1:
        joined = ", ".join(saved_panes)
        print(
            f"error: saved session target '{target}' is ambiguous across live panes: {joined}",
            file=sys.stderr,
        )
        sys.exit(1)

    pane = resolve_pane_from_window_name(target, quiet=True)
    if pane and is_pane_id(pane):
        return pane

    if _tmux_socket_recovery_hint():
        _print_tmux_unavailable_hint(target)
        sys.exit(1)

    print(f"error: no managed session, window, or live Codex pane found for '{target}'", file=sys.stderr)
    print(
        "hint: use a pane id ('%6'), window name, window index ('2'), or a saved session name with a live Codex pane",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_status(argv: list[str]) -> None:
    acw_observe.cmd_status(_observe_context(), argv)


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def _state_dir_is_writable() -> tuple[bool, str]:
    try:
        fd, tmp = tempfile.mkstemp(prefix="acw_doctor_", dir=STATE_DIR)
        os.close(fd)
        os.unlink(tmp)
        return True, STATE_DIR
    except OSError as exc:
        return False, f"{STATE_DIR} ({exc})"


def _codex_auth_state_available() -> tuple[bool, str]:
    auth = Path(STATE_DIR) / "auth.json"
    if auth.is_file():
        return True, str(auth)
    return False, str(auth)


def _doctor_resolve_target(target: str, model: RuntimeModel | None = None) -> tuple[str, str]:
    if model is None:
        model = _build_runtime_model()
    if not target:
        pane = current_tmux_pane()
        if pane:
            return pane, ""
        return "", ""

    if is_pane_id(target):
        return target, ""

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target(target)
        if pane and is_pane_id(pane):
            return pane, ""
        return "", f"could not resolve window target '{target}'"

    saved_panes = _saved_session_panes_for_target(target, model=model)
    if len(saved_panes) == 1:
        return saved_panes[0], ""
    if len(saved_panes) > 1:
        joined = ", ".join(saved_panes)
        return "", f"saved session target '{target}' is ambiguous across live panes: {joined}"

    pane = resolve_pane_from_window_name(target)
    if pane and is_pane_id(pane):
        return pane, ""

    if _tmux_socket_recovery_hint():
        return "", _tmux_socket_recovery_hint()
    return "", f"could not resolve target '{target}'"


DoctorReport = acw_observe.DoctorReport


RepairStep = acw_repair.RepairStep


def _repair_context() -> acw_repair.RepairContext:
    return acw_repair.RepairContext(
        is_pane_id=is_pane_id,
        is_thread_id=is_thread_id,
        short_thread_id=_short_thread_id,
        message_text=_message_text,
        runtime_watchers_for_pane=_runtime_watchers_for_pane,
        runtime_watchers_for_thread=_runtime_watchers_for_thread,
        runtime_panes_for_thread=_runtime_panes_for_thread,
        runtime_session_for_thread=_runtime_session_for_thread,
        runtime_session_name_conflict=_runtime_session_name_conflict,
    )


def _command_context() -> acw_commands.CommandContext:
    return acw_commands.CommandContext(
        is_pane_id=is_pane_id,
        is_thread_id=is_thread_id,
        print_progress=_print_progress,
        build_runtime_model=_build_runtime_model,
        watcher_rows=watcher_rows,
        runtime_watchers_for_pane=_runtime_watchers_for_pane,
        runtime_panes_for_thread=_runtime_panes_for_thread,
        runtime_session_for_thread=_runtime_session_for_thread,
        watcher_anomalies=_watcher_anomalies,
        other_watcher_for_thread=_other_watcher_for_thread,
        resolve_start_pane_target=resolve_start_pane_target,
        resolve_pane_target=resolve_pane_target,
        resolve_pane_from_window_name=resolve_pane_from_window_name,
        resolve_pane_from_window_target=resolve_pane_from_window_target,
        resolve_thread_id=resolve_thread_id,
        resolve_restart_pane_target=_resolve_restart_pane_target,
        ensure_tmux_window_rename_hook=ensure_tmux_window_rename_hook,
        edit_message_interactive=_edit_message_interactive,
        message_text=_message_text,
        read_session_state=_read_session_state,
        write_session_state=_write_session_state,
        remove_session_state=_remove_session_state,
        daemon_rpc_upsert_session=_daemon_rpc_upsert_session,
        daemon_rpc_patch_session=_daemon_rpc_patch_session,
        daemon_rpc_remove_session=_daemon_rpc_remove_session,
        daemon_rpc_restart_codex=_daemon_rpc_restart_codex,
        current_session_generation=_current_session_generation,
        load_session_records=_load_session_records,
        strict_thread_for_pane=_strict_thread_for_pane,
        strict_session_for_thread=_strict_session_for_thread,
        short_thread_id=_short_thread_id,
        build_repair_plan=_build_repair_plan,
        doctor_resolve_target=_doctor_resolve_target,
        run_tmux=run_tmux,
    )


def _observe_context() -> acw_observe.ObserveContext:
    return acw_observe.ObserveContext(
        state_dir=STATE_DIR,
        preferred_tmux_socket=_preferred_tmux_socket,
        is_thread_id=is_thread_id,
        build_runtime_snapshot=_build_runtime_snapshot,
        build_runtime_model=_build_runtime_model,
        build_status_entries_from_snapshot=_build_status_entries_from_snapshot,
        tmux_socket_recovery_hint=_tmux_socket_recovery_hint,
        resolve_live_pane_target=_resolve_live_pane_target,
        status_sort_key=_status_sort_key,
        status_details=_status_details,
        status_table=_status_table,
        print_status_recommendations=_print_status_recommendations,
        state_dir_is_writable=_state_dir_is_writable,
        codex_auth_state_available=_codex_auth_state_available,
        run_tmux=run_tmux,
        doctor_resolve_target=_doctor_resolve_target,
        strict_thread_for_pane=_strict_thread_for_pane,
        thread_provenance_text=_thread_provenance_text,
        runtime_watchers_for_pane=_runtime_watchers_for_pane,
        watcher_anomalies=_watcher_anomalies,
        read_state_json=_read_state_json,
        compute_state=_compute_state,
        repair_steps_for_pane=_repair_steps_for_pane,
        current_tmux_pane=current_tmux_pane,
        read_session_state=_read_session_state,
        print_doctor_plain=_print_doctor_plain,
        print_doctor_rich=_print_doctor_rich,
        short_thread_id=_short_thread_id,
    )


def _pane_numeric_sort_key(pane: str) -> tuple[int, str]:
    return acw_repair.pane_numeric_sort_key(pane, context=_repair_context())


def _repair_message_for_pane(model: RuntimeModel, pane: str, live_thread: str) -> str:
    return acw_repair.repair_message_for_pane(
        model,
        pane,
        live_thread,
        context=_repair_context(),
    )


def _build_repair_plan(
    model: RuntimeModel,
    target_panes: tuple[str, ...] | None = None,
) -> list[RepairStep]:
    return acw_repair.build_repair_plan(
        model,
        target_panes=target_panes,
        context=_repair_context(),
    )


def _repair_steps_for_pane(model: RuntimeModel, pane: str) -> list[RepairStep]:
    return acw_repair.repair_steps_for_pane(
        model,
        pane,
        context=_repair_context(),
    )


def _doctor_recommendation_target(target: str, pane: str, rows: list[dict[str, str]]) -> str:
    if target:
        return target
    if rows:
        name = _read_session_state(rows[0].get("thread", "")).get("name", "").strip()
        if name:
            return name
    return pane


def _doctor_report(target: str = "") -> DoctorReport:
    return acw_observe.doctor_report(_observe_context(), target)

def _print_doctor_plain(report: DoctorReport) -> None:
    acw_ui.print_doctor_plain(
        report,
        state_dir=STATE_DIR,
        tmux_socket=_preferred_tmux_socket() or "(default)",
    )


def _print_doctor_rich(report: DoctorReport) -> None:
    acw_ui.print_doctor_rich(
        report,
        state_dir=STATE_DIR,
        tmux_socket=_preferred_tmux_socket() or "(default)",
        result_styles=_DOCTOR_RESULT_STYLES,
    )


def cmd_doctor(argv: list[str]) -> None:
    acw_observe.cmd_doctor(_observe_context(), argv)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cmd_cleanup(argv: list[str]) -> None:
    """Cleanup stale files."""
    if argv:
        print("error: cleanup does not accept a target", file=sys.stderr)
        sys.exit(2)
    cleanup_stale_files(remove_dead_sessions=True)
    print("cleanup: removed stale files")


def cleanup_stale_files(remove_dead_sessions: bool = False) -> None:
    """Remove files and DB rows for managed runtimes that are no longer live."""
    daemon_pid_file = daemon_pid_file_for_scope()
    daemon_socket_file = daemon_socket_file_for_scope()
    if daemon_pid_file and os.path.exists(daemon_pid_file) and not _daemon_pid_alive(daemon_pid_file):
        Path(daemon_pid_file).unlink(missing_ok=True)
        Path(daemon_socket_file).unlink(missing_ok=True)

    # Build set of live watcher keys for watch logs.
    live_keys: set[str] = set()
    live_runtime_threads: set[str] = set()
    for r in watcher_rows():
        k = key_from_pane(r["pane"])
        if k:
            live_keys.add(k)
        tid = r.get("thread", "")
        if is_thread_id(tid):
            live_runtime_threads.add(tid.lower())

    # Watch logs for dead sessions.
    keep_logs: set[str] = set()
    for k in live_keys:
        keep_logs.add(watch_log_for_key(k))
    for pat in (os.path.join(STATE_DIR, "auto_continue_logwatch*.log"),):
        for lf in glob.glob(pat):
            if lf not in keep_logs:
                Path(lf).unlink(missing_ok=True)

    if not remove_dead_sessions:
        return

    model = _build_runtime_model()
    live_thread_panes = {tid.lower() for tid in model.panes_by_thread}
    active_threads = live_runtime_threads | live_thread_panes

    def _daemon_pid_is_alive(pid_str: str) -> bool:
        if not pid_str.isdigit():
            return False
        try:
            os.kill(int(pid_str), 0)
            return True
        except OSError:
            return False

    acw_state.cleanup_rows(
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(),
        active_threads=active_threads,
        daemon_pid_alive=_daemon_pid_is_alive,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def print_help(file=None) -> None:
    if file is None:
        file = sys.stdout
    prog = os.path.basename(sys.argv[0]) or "acw"
    print(build_help_text(prog), file=file)


def _print_progress(message: str) -> None:
    print(message, flush=True)


def main() -> None:
    if len(sys.argv) < 2:
        subcmd = "status"
        rest: list[str] = []
    else:
        subcmd = sys.argv[1]
        rest = sys.argv[2:]

    if subcmd in ("-h", "--help", "help"):
        print_help()
        raise SystemExit(0)

    # Only clean up stale files on commands that mutate state.  Read-only
    # commands (status, pause, resume) skip the cleanup overhead (ps scan +
    # multiple globs).
    if subcmd in ("start", "stop", "restart", "edit", "repair", "cleanup"):
        cleanup_stale_files()

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "restart": cmd_restart,
        "repair": cmd_repair,
        "cleanup": cmd_cleanup,
        "status": cmd_status,
        "edit": cmd_edit,
        "doctor": cmd_doctor,
        "_window-renamed": cmd_window_renamed,
        "_daemon": _daemon_main,
    }

    if subcmd in commands:
        commands[subcmd](rest)
    else:
        print_help(file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
