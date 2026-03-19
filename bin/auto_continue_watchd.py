#!/usr/bin/env python3
"""Process manager for auto-continue managed sessions."""

from __future__ import annotations

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

import acw_bootstrap
import acw_config
import acw_state
import acw_session
import acw_status
import acw_rpc
import acw_daemon
import acw_control
import acw_commands
import acw_ids
import acw_housekeeping
import acw_observe
import acw_probe
import acw_repair
import acw_runtime
import acw_runtime_store
import acw_tmux
import acw_targets
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

DAEMON_POLL_INTERVAL_SECS = 0.2
DAEMON_IDLE_EXIT_SECS = 2.0
DAEMON_CODEX_LOG_PATH = acw_config.codex_log_path()
_DAEMON_RUNTIME_ID_SEQ = 0


def _next_daemon_runtime_id() -> str:
    global _DAEMON_RUNTIME_ID_SEQ
    _DAEMON_RUNTIME_ID_SEQ += 1
    return f"{os.getpid()}{_DAEMON_RUNTIME_ID_SEQ:06d}"


def resolve_project_cwd() -> str:
    return acw_config.resolve_project_cwd()


PROJECT_CWD = resolve_project_cwd()
# State dir is always ~/.codex/ regardless of CWD.  Pane IDs are tmux-global,
# so per-project state dirs cause fragmentation: the same pane gets different
# message/pid/state files depending on which directory `acw` was invoked from.
STATE_DIR = acw_config.state_dir()
SEND_DELAY_SECS = acw_config.send_delay_secs()
ENTER_DELAY_SECS = acw_config.enter_delay_secs()
DEFAULT_MESSAGE_TEXT = acw_config.DEFAULT_MESSAGE_TEXT
KNOWN_SHELL_COMMANDS = acw_config.known_shell_commands()

os.makedirs(STATE_DIR, exist_ok=True)

def build_help_text(prog: str) -> str:
    return acw_bootstrap.build_help_text(prog)

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sanitize_key(s: str) -> str:
    return acw_tmux.sanitize_key(s)


def key_from_pane(pane: str) -> str:
    return sanitize_key(pane)


def is_pane_id(s: str) -> bool:
    return acw_ids.is_pane_id(s)


def current_tmux_pane() -> str:
    return acw_ids.current_tmux_pane()


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
    return acw_ids.is_thread_id(s)


def watch_log_for_key(key: str) -> str:
    return acw_runtime_store.watch_log_for_key(STATE_DIR, key)


def state_file_for_thread(thread_id: str) -> str:
    return acw_runtime_store.state_file_for_thread(thread_id)


def state_db_file() -> str:
    return acw_runtime_store.state_db_file(STATE_DIR)


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
    acw_runtime_store.write_runtime_state(
        thread_id,
        data,
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(socket_path),
    )


def _remove_runtime_state(thread_id: str, socket_path: str | None = None) -> None:
    acw_runtime_store.remove_runtime_state(
        thread_id,
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(socket_path),
    )


def _read_runtime_rows(socket_path: str | None = None) -> list[dict[str, str]]:
    rows = acw_runtime_store.read_runtime_rows(
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(socket_path),
    )
    return [{str(key): value for key, value in row.items()} for row in rows]


def _read_daemon_scope_state(socket_path: str | None = None) -> dict[str, str]:
    return acw_runtime_store.read_daemon_scope_state(
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(socket_path),
    )


def _write_daemon_scope_state(
    *,
    daemon_pid: str,
    socket_path: str,
    heartbeat_ts: str,
    started_ts: str = "",
    scope: str | None = None,
) -> None:
    acw_runtime_store.write_daemon_scope_state(
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
    return acw_targets.resolve_thread_id(
        pane,
        requested,
        model=model,
        context=_target_context(),
    )


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
    return acw_runtime_store.watcher_rows(
        pane_filter,
        read_runtime_rows_fn=_read_runtime_rows,
    )


def watcher_pids_for_pane(pane: str) -> list[str]:
    return acw_runtime_store.watcher_pids_for_pane(
        pane,
        watcher_rows_fn=watcher_rows,
    )


def _watcher_row_for_pane(pane: str) -> dict[str, str] | None:
    return acw_runtime_store.watcher_row_for_pane(
        pane,
        watcher_rows_fn=watcher_rows,
    )


def _system_exit_detail(exc: SystemExit, stderr_text: str = "") -> str:
    return acw_commands.system_exit_detail(exc, stderr_text)


def _other_watcher_for_thread(pane: str, thread_id: str) -> dict[str, str] | None:
    return acw_runtime_store.other_watcher_for_thread(
        pane,
        thread_id,
        is_thread_id_fn=is_thread_id,
        watcher_rows_fn=watcher_rows,
    )


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
    return acw_runtime_store.thread_from_running_watcher_for_pane(
        pane,
        watcher_rows_fn=watcher_rows,
        is_thread_id_fn=is_thread_id,
    )


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


def _daemon_process_context() -> acw_control.DaemonProcessContext:
    return acw_control.DaemonProcessContext(
        read_pid_file=_read_pid_file,
        current_scope_key=_tmux_scope_key,
        current_scope_label=_tmux_scope_label,
        daemon_pid_file_for_scope=daemon_pid_file_for_scope,
        daemon_socket_file_for_scope=daemon_socket_file_for_scope,
        daemon_log_file_for_scope=daemon_log_file_for_scope,
        daemon_scope_env=_daemon_scope_env,
        daemon_entrypoint=str(Path(__file__).resolve()),
    )


def _daemon_pid_alive(pid_file: str) -> str:
    return acw_control.daemon_pid_alive(
        pid_file,
        context=_daemon_process_context(),
    )


def _stop_existing_daemon(pid_str: str, pid_file: str, socket_file: str) -> None:
    acw_control.stop_existing_daemon(
        pid_str,
        pid_file,
        socket_file,
        context=_daemon_process_context(),
    )


def _ensure_daemon_running() -> str:
    return acw_control.ensure_daemon_running(
        context=_daemon_process_context(),
    )


def _current_session_generation(data: dict) -> int:
    return acw_control.current_session_generation(data)


def _normalize_runtime_row(data: dict[str, object] | None) -> dict[str, str]:
    return acw_control.normalize_runtime_row(data)


def _daemon_rpc_client_context() -> acw_control.DaemonRpcClientContext:
    return acw_control.DaemonRpcClientContext(
        ensure_daemon_running=_ensure_daemon_running,
        current_socket_file=daemon_socket_file_for_scope,
        current_log_file=daemon_log_file_for_scope,
        write_daemon_scope_state=_write_daemon_scope_state,
        watch_now_ts=_watch_now_ts,
        current_scope_key=_tmux_scope_key,
        print_progress=_print_progress,
    )


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
        context=_daemon_rpc_client_context(),
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


def _daemon_rpc_continue_session(thread_id: str, *, timeout_secs: float = 2.0) -> dict[str, str]:
    return acw_control.daemon_rpc_continue_session(
        thread_id,
        daemon_rpc_call_fn=_daemon_rpc_call,
        timeout_secs=timeout_secs,
    )


def _session_signature(session: SavedSessionRecord) -> tuple[str, str, int]:
    return acw_control.session_signature(session)


def _scoped_session_context() -> acw_control.ScopedSessionContext:
    return acw_control.ScopedSessionContext(
        load_sessions=_load_sessions,
        normalize_tmux_socket=_tmux_scope_key,
    )


def _load_scoped_session_records(socket_path: str | None = None) -> list[SavedSessionRecord]:
    return acw_control.load_scoped_session_records(
        socket_path,
        context=_scoped_session_context(),
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


def _daemon_continue_session(runtime: _DaemonSessionRuntime) -> tuple[bool, str]:
    return acw_daemon.daemon_continue_session(
        runtime,
        check_pane_for_interrupt=_check_pane_for_interrupt,
        check_pane_for_errors=_check_pane_for_errors,
        tmux_send=_watch_tmux_send,
        now_ts=_watch_now_ts,
        append_log=_watch_append_log,
        write_session_state=_write_session_state,
        enter_delay_secs=ENTER_DELAY_SECS,
        time_module=time,
    )


def _daemon_reconcile_context() -> acw_control.DaemonReconcileContext:
    return acw_control.DaemonReconcileContext(
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
        context=_daemon_reconcile_context(),
    )


def _daemon_wait_context() -> acw_control.DaemonWaitContext:
    return acw_control.DaemonWaitContext(
        daemon_reconcile_once=_daemon_reconcile_once,
        daemon_runtime_row=_daemon_runtime_row,
        current_session_generation=_current_session_generation,
        time_module=time,
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
        context=_daemon_wait_context(),
    )


def _daemon_wait_matches(row: dict[str, str], wait: dict[str, object]) -> bool:
    return acw_control.daemon_wait_matches(
        row,
        wait,
        current_session_generation_fn=_current_session_generation,
    )


def _restart_codex_context() -> acw_control.RestartCodexContext:
    return acw_control.RestartCodexContext(
        wait_for_shell_prompt=_wait_for_shell_prompt,
        terminate_codex_processes_in_pane=_terminate_codex_processes_in_pane,
        run_tmux=run_tmux,
        wait_for_codex_process=_wait_for_codex_process,
        accept_directory_trust_prompt_in_pane=_accept_directory_trust_prompt_in_pane,
        print_progress=_print_progress,
    )


def _restart_codex_in_pane_result(pane: str, thread_id: str, *, progress) -> tuple[bool, str]:
    return acw_control.restart_codex_in_pane_result(
        pane,
        thread_id,
        progress=progress,
        context=_restart_codex_context(),
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
            continue_session=_daemon_continue_session,
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


def _daemon_main_context() -> acw_control.DaemonMainContext:
    return acw_control.DaemonMainContext(
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
    )


def _daemon_main(argv: list[str]) -> None:
    acw_control.daemon_main(
        argv,
        context=_daemon_main_context(),
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


def _thread_started_at(thread_id: str) -> int | None:
    started_at, _last_activity_at = thread_times_from_state_db(thread_id)
    return started_at


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


def _paused_activity_for_pane(pane: str) -> str:
    return acw_probe.paused_activity_for_pane(pane, run_tmux)


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


def _saved_session_target_context() -> acw_targets.TargetContext:
    return acw_targets.TargetContext(
        is_pane_id=is_pane_id,
        is_thread_id=is_thread_id,
        build_runtime_model=_build_runtime_model,
        strict_thread_for_pane=_strict_thread_for_pane,
        runtime_session_for_thread=_runtime_session_for_thread,
        runtime_panes_for_thread=_runtime_panes_for_thread,
        saved_session_panes_for_target=None,
        resolve_pane_from_window_name=resolve_pane_from_window_name,
        resolve_pane_from_window_name_quiet=lambda target: resolve_pane_from_window_name(target, quiet=True),
        resolve_pane_from_window_target=resolve_pane_from_window_target,
        tmux_socket_recovery_hint=_tmux_socket_recovery_hint,
        print_tmux_unavailable_hint=_print_tmux_unavailable_hint,
        current_tmux_pane=current_tmux_pane,
    )


def _target_context() -> acw_targets.TargetContext:
    return acw_targets.TargetContext(
        is_pane_id=is_pane_id,
        is_thread_id=is_thread_id,
        build_runtime_model=_build_runtime_model,
        strict_thread_for_pane=_strict_thread_for_pane,
        runtime_session_for_thread=_runtime_session_for_thread,
        runtime_panes_for_thread=_runtime_panes_for_thread,
        saved_session_panes_for_target=_saved_session_panes_for_target,
        resolve_pane_from_window_name=resolve_pane_from_window_name,
        resolve_pane_from_window_name_quiet=lambda target: resolve_pane_from_window_name(target, quiet=True),
        resolve_pane_from_window_target=resolve_pane_from_window_target,
        tmux_socket_recovery_hint=_tmux_socket_recovery_hint,
        print_tmux_unavailable_hint=_print_tmux_unavailable_hint,
        current_tmux_pane=current_tmux_pane,
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


def _recoverable_named_session_for_pane(model: RuntimeModel, pane: str) -> SavedSessionRecord | None:
    return acw_runtime.recoverable_named_session_for_pane(model, pane, context=_runtime_context())


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
    return acw_status.status_row_detail(row, sj)


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
    acw_status.print_status_recommendations(
        resolved,
        status_recommendations_fn=_status_recommendations,
        status_invariant_messages_fn=_status_invariant_messages,
    )


def _status_table(resolved: list[tuple[dict[str, str], str, str]]) -> None:
    rows_data = _status_rows_data(resolved)
    acw_ui.render_status_table(
        rows_data,
        section_every=_STATUS_SECTION_EVERY,
        max_msg_lines=_MAX_MSG_LINES,
    )


def _status_rows_data(resolved: list[tuple[dict[str, str], str, str]]) -> list[tuple[str, str, str, str, str, str]]:
    return acw_status.status_rows_data(
        resolved,
        read_state_json_fn=_read_state_json,
        compute_state_fn=_compute_state,
        status_row_detail_fn=_status_row_detail,
        thread_times_fn=lambda thread_id, compact: _thread_times(thread_id, compact=compact),
        average_continue_label_fn=lambda row, session_json: acw_status.average_continue_label(
            row,
            session_json,
            thread_started_at_fn=_thread_started_at,
            continue_count_for_watch_log_fn=acw_status.continue_count_for_watch_log,
            format_age_fn=lambda epoch, compact: _format_age(epoch, compact=compact),
        ),
        format_age_fn=lambda epoch, compact: _format_age(epoch, compact=compact),
        last_agent_snippet_for_pane_fn=_last_agent_snippet_for_pane,
        paused_activity_for_pane_fn=_paused_activity_for_pane,
        message_summary_for_row_fn=_message_summary_for_row,
        status_window_pane_cell_fn=_status_window_pane_cell,
        state_summary_fn=_state_summary,
    )


def _status_watch_frame(resolved: list[tuple[dict[str, str], str, str]]) -> tuple[str, object]:
    rows_data = _status_rows_data(resolved)
    recommendations = _status_recommendations(resolved)
    invariant_messages = _status_invariant_messages(resolved)
    frame_key = repr((rows_data, recommendations, invariant_messages))
    renderable = acw_ui.build_status_watch_renderable(
        rows_data,
        section_every=_STATUS_SECTION_EVERY,
        max_msg_lines=_MAX_MSG_LINES,
        recommendations=recommendations,
        invariant_messages=invariant_messages,
    )
    return frame_key, renderable


def _status_details(
    resolved: list[tuple[dict[str, str], str, str]],
    model: RuntimeModel | None = None,
) -> None:
    if model is None:
        model = _build_runtime_model()
    acw_status.status_details(
        resolved,
        model=model,
        read_state_json_fn=_read_state_json,
        compute_state_fn=_compute_state,
        status_row_detail_fn=_status_row_detail,
        resolve_message_fn=_resolve_message,
        thread_times_fn=lambda thread_id, compact: _thread_times(thread_id, compact=compact),
        last_agent_snippet_for_pane_fn=_last_agent_snippet_for_pane,
        paused_activity_for_pane_fn=_paused_activity_for_pane,
        thread_provenance_text_fn=_thread_provenance_text,
        print_status_recommendations_fn=_print_status_recommendations,
    )


def _load_sessions(socket_path: str | None = None) -> list[dict[str, str]]:
    rows = acw_session.load_sessions(
        state_dir=STATE_DIR,
        scope=_tmux_scope_key(socket_path),
    )
    return [{str(key): value for key, value in row.items()} for row in rows]


def _saved_session_panes_for_target(target: str, model: RuntimeModel | None = None) -> list[str]:
    return acw_targets.saved_session_panes_for_target(
        target,
        model=model,
        context=_saved_session_target_context(),
    )


def _resolve_restart_pane_target(target: str, model: RuntimeModel | None = None) -> str:
    return acw_targets.resolve_restart_pane_target(
        target,
        model=model,
        context=_target_context(),
    )


def cmd_status(argv: list[str]) -> None:
    acw_observe.cmd_status(_observe_context(), argv)


def cmd_watch(argv: list[str]) -> None:
    acw_observe.cmd_watch(_observe_context(), argv)


def _run_watch_command(argv: list[str]) -> None:
    if not argv:
        return
    subcmd = argv[0]
    rest = argv[1:]
    if subcmd in ("start", "restart", "edit"):
        cleanup_stale_files()
    commands = {
        "start": cmd_start,
        "edit": cmd_edit,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "restart": cmd_restart,
    }
    handler = commands.get(subcmd)
    if handler is None:
        print(f"error: unsupported interactive command '{subcmd}'", file=sys.stderr)
        sys.exit(2)
    handler(rest)


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def _state_dir_is_writable() -> tuple[bool, str]:
    return acw_housekeeping.state_dir_is_writable(STATE_DIR)


def _codex_auth_state_available() -> tuple[bool, str]:
    return acw_housekeeping.codex_auth_state_available(STATE_DIR)


def _doctor_resolve_target(target: str, model: RuntimeModel | None = None) -> tuple[str, str]:
    return acw_targets.doctor_resolve_target(
        target,
        model=model,
        context=_target_context(),
    )


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
        recoverable_named_session_for_pane=_recoverable_named_session_for_pane,
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
        recoverable_named_session_for_pane=_recoverable_named_session_for_pane,
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
        daemon_rpc_continue_session=_daemon_rpc_continue_session,
        current_session_generation=_current_session_generation,
        current_tmux_scope=_tmux_scope_key,
        load_session_records=_load_session_records,
        strict_thread_for_pane=_strict_thread_for_pane,
        strict_session_for_thread=_strict_session_for_thread,
        short_thread_id=_short_thread_id,
        pane_activity_for_pane=_paused_activity_for_pane,
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
        status_watch_frame=_status_watch_frame,
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
        run_watch_command=_run_watch_command,
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
    acw_housekeeping.cleanup_stale_files(
        remove_dead_sessions,
        context=acw_housekeeping.CleanupContext(
            daemon_pid_file_for_scope=daemon_pid_file_for_scope,
            daemon_socket_file_for_scope=daemon_socket_file_for_scope,
            daemon_pid_alive=_daemon_pid_alive,
            watcher_rows=lambda: watcher_rows(),
            key_from_pane=key_from_pane,
            is_thread_id=is_thread_id,
            watch_log_for_key=watch_log_for_key,
            state_dir=STATE_DIR,
            build_runtime_model=_build_runtime_model,
            tmux_scope_key=_tmux_scope_key,
            cleanup_rows=acw_state.cleanup_rows,
            os_kill=os.kill,
        ),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def print_help(file=None) -> None:
    acw_bootstrap.print_help(
        argv0=sys.argv[0],
        build_help_text_fn=build_help_text,
        file=file,
    )


def _print_progress(message: str) -> None:
    print(message, flush=True)


def main() -> None:
    commands = {
        "watch": cmd_watch,
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
    acw_bootstrap.main(
        argv=sys.argv,
        cleanup_stale_files_fn=cleanup_stale_files,
        print_help_fn=print_help,
        commands=commands,
        stdout_isatty_fn=lambda: sys.stdout.isatty(),
    )


if __name__ == "__main__":
    main()
