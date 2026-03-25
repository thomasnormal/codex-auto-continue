from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from typing import Any
from typing import Callable

import acw_daemon
import acw_rpc
import acw_runtime


# ---------------------------------------------------------------------------
# Disk space monitor
# ---------------------------------------------------------------------------

DISK_CHECK_INTERVAL_SECS = 60.0
DISK_FREE_THRESHOLD = 0.10  # 10% free
DISK_ALERT_COOLDOWN_SECS = 600.0  # don't re-alert within 10 minutes


def _check_disk_space(
    runtimes: dict[str, Any],
    *,
    last_check: float,
    last_alert: float,
    now: float,
    path: str = os.path.expanduser("~"),
) -> tuple[float, float]:
    """Check disk space and alert agents if free space is critically low.

    Returns updated (last_check, last_alert) timestamps.
    """
    if now - last_check < DISK_CHECK_INTERVAL_SECS:
        return last_check, last_alert

    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return now, last_alert

    free_ratio = usage.free / usage.total
    if free_ratio >= DISK_FREE_THRESHOLD:
        return now, last_alert

    if last_alert > 0.0 and now - last_alert < DISK_ALERT_COOLDOWN_SECS:
        return now, last_alert

    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
    pct_used = 100.0 * (1.0 - free_ratio)
    msg = (
        f"DISK SPACE WARNING: {free_gb:.1f}GB free of {total_gb:.0f}GB "
        f"({pct_used:.0f}% used). "
        f"Please help free up space — delete stale build artifacts, "
        f"worktrees, or large temporary files in your working directory."
    )

    alerted_panes: set[str] = set()
    for runtime in runtimes.values():
        pane = getattr(runtime, "pane", "")
        if not pane or pane in alerted_panes:
            continue
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", pane, "-l", msg],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", pane, "C-m"],
                capture_output=True, timeout=5,
            )
            alerted_panes.add(pane)
        except (subprocess.TimeoutExpired, OSError):
            pass

    if alerted_panes:
        _safe_stderr_print(
            f"disk-monitor: alerted {len(alerted_panes)} pane(s), "
            f"{free_gb:.1f}GB free ({pct_used:.0f}% used)"
        )
        return now, now

    return now, last_alert


@dataclass(frozen=True)
class DaemonProcessContext:
    read_pid_file: Callable[[str], str | None]
    current_scope_key: Callable[[], str]
    current_scope_label: Callable[[], str]
    daemon_pid_file_for_scope: Callable[[str | None], str]
    daemon_socket_file_for_scope: Callable[[str | None], str]
    daemon_log_file_for_scope: Callable[[str | None], str]
    daemon_scope_env: Callable[[str], dict[str, str]]
    daemon_entrypoint: str


@dataclass(frozen=True)
class DaemonRpcClientContext:
    ensure_daemon_running: Callable[[], str]
    current_socket_file: Callable[[], str]
    current_log_file: Callable[[], str]
    write_daemon_scope_state: Callable[..., None]
    watch_now_ts: Callable[[], str]
    current_scope_key: Callable[[], str]
    print_progress: Callable[[str], None]


@dataclass(frozen=True)
class ScopedSessionContext:
    load_sessions: Callable[[str | None], list[dict[str, str]]]
    normalize_tmux_socket: Callable[[str | None], str]


@dataclass(frozen=True)
class DaemonReconcileContext:
    drain_daemon_codex_log_events: Callable[[Any], list[Any]]
    load_scoped_session_records: Callable[[str | None], list[acw_runtime.SavedSessionRecord]]
    remove_runtime_state: Callable[[str, str | None], None]
    daemon_session_runtime: Callable[..., Any]
    message_text: Callable[[str], str]
    daemon_process_session_events: Callable[[Any, list[Any]], None]
    daemon_runtime_events: Callable[[Any, dict[str, list[Any]]], list[Any]]
    daemon_refresh_session_health: Callable[..., None]
    daemon_refresh_session_location: Callable[[Any], None]
    daemon_runtime_payload: Callable[[Any], dict]
    write_runtime_state: Callable[[str, dict, str | None], None]
    append_log: Callable[[Path, str], None]
    codex_log_path: Path
    is_thread_id: Callable[[str], bool]
    time_module: Any


@dataclass(frozen=True)
class DaemonWaitContext:
    daemon_reconcile_once: Callable[..., int]
    daemon_runtime_row: Callable[[str, Any | None], dict[str, str]]
    current_session_generation: Callable[[dict], int]
    time_module: Any


@dataclass(frozen=True)
class DaemonRpcServerContext:
    is_thread_id: Callable[[str], bool]
    write_session_state: Callable[[str, dict[str, object]], None]
    remove_session_state: Callable[[str], bool]
    daemon_wait_for_condition: Callable[..., dict[str, str]]
    restart_codex_in_pane_result: Callable[[str, str, Callable[[str], None]], tuple[bool, str]]
    continue_session: Callable[[Any], tuple[bool, str]]


@dataclass(frozen=True)
class DaemonRpcServeContext:
    daemon_handle_rpc_request: Callable[..., dict[str, object]]


@dataclass(frozen=True)
class DaemonMainContext:
    normalize_tmux_socket: Callable[[str | None], str]
    daemon_pid_file_for_scope: Callable[[str | None], str]
    daemon_socket_file_for_scope: Callable[[str | None], str]
    daemon_log_tail_cls: Callable[..., Any]
    codex_log_path: Path
    write_daemon_scope_state: Callable[..., None]
    watch_now_ts: Callable[[], str]
    daemon_process_rpc_requests: Callable[..., int]
    daemon_reconcile_once: Callable[..., int]
    remove_runtime_state: Callable[[str, str | None], None]
    daemon_poll_interval_secs: float
    daemon_idle_exit_secs: float
    time_module: Any


@dataclass(frozen=True)
class TrustPromptContext:
    run_tmux: Callable[..., str | None]
    time_module: Any


@dataclass(frozen=True)
class PaneCommandContext:
    run_tmux: Callable[..., str | None]
    command_name_for_pid: Callable[[str], str]


@dataclass(frozen=True)
class WaitForShellContext:
    pane_command_snapshot: Callable[[str], tuple[str, str, str, str]]
    known_shell_commands: set[str]
    time_module: Any


@dataclass(frozen=True)
class WaitForCodexContext:
    pane_command_snapshot: Callable[[str], tuple[str, str, str, str]]
    codex_pids_in_process_tree: Callable[[str], list[str]]
    time_module: Any


@dataclass(frozen=True)
class TerminateCodexContext:
    pane_command_snapshot: Callable[[str], tuple[str, str, str, str]]
    known_shell_commands: set[str]
    codex_top_level_descendants: Callable[[str], list[str]]
    os_kill: Callable[[int, int], None]
    sigterm: int


@dataclass(frozen=True)
class RestartCodexContext:
    wait_for_shell_prompt: Callable[..., tuple[bool, str]]
    terminate_codex_processes_in_pane: Callable[[str], tuple[bool, str]]
    run_tmux: Callable[..., str | None]
    wait_for_codex_process: Callable[..., tuple[bool, str]]
    accept_directory_trust_prompt_in_pane: Callable[[str, float], bool]
    print_progress: Callable[[str], None]


def daemon_pid_alive(pid_file: str, *, context: DaemonProcessContext) -> str:
    pid_str = context.read_pid_file(pid_file)
    if not pid_str or not pid_str.isdigit():
        return ""
    try:
        os.kill(int(pid_str), 0)
    except OSError:
        return ""
    return pid_str


def stop_existing_daemon(
    pid_str: str,
    pid_file: str,
    socket_file: str,
    *,
    context: DaemonProcessContext,
) -> None:
    if pid_str.isdigit():
        try:
            os.kill(int(pid_str), signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not daemon_pid_alive(pid_file, context=context):
            break
        time.sleep(0.05)
    Path(pid_file).unlink(missing_ok=True)
    Path(socket_file).unlink(missing_ok=True)


def ensure_daemon_running(*, context: DaemonProcessContext) -> str:
    socket_path = context.current_scope_key()
    pid_file = context.daemon_pid_file_for_scope(socket_path)
    socket_file = context.daemon_socket_file_for_scope(socket_path)
    pid_str = daemon_pid_alive(pid_file, context=context)
    if pid_str:
        deadline = time.time() + 0.5
        while time.time() < deadline:
            if Path(socket_file).exists():
                return pid_str
            time.sleep(0.05)
        stop_existing_daemon(pid_str, pid_file, socket_file, context=context)

    Path(pid_file).unlink(missing_ok=True)
    Path(socket_file).unlink(missing_ok=True)
    log_path = context.daemon_log_file_for_scope(socket_path)
    cmd = [
        sys.executable,
        context.daemon_entrypoint,
        "_daemon",
        *(["--tmux-socket", socket_path] if socket_path else []),
    ]
    log_fh = open(log_path, "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
        env=context.daemon_scope_env(socket_path),
    )
    log_fh.close()

    deadline = time.time() + 5.0
    while time.time() < deadline:
        pid_str = daemon_pid_alive(pid_file, context=context)
        if pid_str and Path(socket_file).exists():
            return pid_str
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    print(
        f"error: failed to start acw daemon for tmux socket {context.current_scope_label()}",
        file=sys.stderr,
    )
    try:
        with open(log_path) as f:
            lines = f.readlines()
        for line in lines[-40:]:
            print(line, end="", file=sys.stderr)
    except OSError:
        pass
    sys.exit(1)


def current_session_generation(data: dict) -> int:
    try:
        return int(data.get("generation", 0) or 0)
    except (TypeError, ValueError):
        return 0


def normalize_runtime_row(data: dict[str, object] | None) -> dict[str, str]:
    if not data:
        return {}
    normalized: dict[str, str] = {}
    for key, value in data.items():
        if key == "paused":
            normalized[key] = "1" if value else ""
        elif value is None:
            normalized[key] = ""
        else:
            normalized[key] = str(value)
    if not normalized.get("thread") and normalized.get("thread_id"):
        normalized["thread"] = normalized["thread_id"].lower()
    return normalized


def daemon_rpc_call(
    command: str,
    params: dict[str, object],
    *,
    timeout_secs: float,
    context: DaemonRpcClientContext,
) -> dict[str, object]:
    daemon_pid = context.ensure_daemon_running()
    try:
        payload = acw_rpc.call(
            context.current_socket_file(),
            {"command": command, "params": params},
            on_progress=context.print_progress,
            timeout_secs=timeout_secs + 1.0,
        )
    except acw_rpc.RpcError as exc:
        print(f"error: daemon RPC '{command}' failed: {exc}", file=sys.stderr)
        if exc.detail:
            print(f"detail: {exc.detail}", file=sys.stderr)
        print(
            f"hint: inspect {context.current_log_file()} for daemon control-path details",
            file=sys.stderr,
        )
        sys.exit(1)
    payload["daemon_pid"] = str(payload.get("daemon_pid", "")).strip() or daemon_pid
    context.write_daemon_scope_state(
        daemon_pid=payload["daemon_pid"],
        socket_path=context.current_socket_file(),
        heartbeat_ts=context.watch_now_ts(),
        scope=context.current_scope_key(),
    )
    return payload


def daemon_rpc_upsert_session(
    thread_id: str,
    *,
    pane: str,
    name: str,
    message: str,
    paused: bool,
    generation: int,
    daemon_rpc_call_fn: Callable[..., dict[str, object]],
    message_text: Callable[[str], str],
    tmux_scope_key: Callable[[str | None], str],
    extra_session: dict[str, object] | None = None,
    timeout_secs: float = 5.0,
) -> dict[str, str]:
    session_payload = {
        "thread_id": thread_id.lower(),
        "pane": pane,
        "name": name,
        "message": message_text(message),
        "paused": bool(paused),
        "generation": int(generation),
        "tmux_socket": tmux_scope_key(),
    }
    if extra_session:
        session_payload.update(extra_session)
    payload = daemon_rpc_call_fn(
        "upsert_session",
        {
            "thread_id": thread_id.lower(),
            "session": session_payload,
            "wait": {
                "kind": "runtime",
                "pane": pane,
                "generation": int(generation),
                "paused": bool(paused),
                "statuses": ["paused"] if paused else ["running", "paused"],
            },
            "timeout_secs": timeout_secs,
        },
        timeout_secs=timeout_secs,
    )
    row = normalize_runtime_row(payload.get("row", {}))
    row["daemon_pid"] = str(payload.get("daemon_pid", "")).strip()
    return row


def daemon_rpc_patch_session(
    thread_id: str,
    changes: dict[str, object],
    *,
    daemon_rpc_call_fn: Callable[..., dict[str, object]],
    wait: dict[str, object] | None = None,
    timeout_secs: float = 5.0,
) -> dict[str, str]:
    payload = daemon_rpc_call_fn(
        "patch_session",
        {
            "thread_id": thread_id.lower(),
            "changes": changes,
            "wait": wait or {},
            "timeout_secs": timeout_secs,
        },
        timeout_secs=timeout_secs,
    )
    row = normalize_runtime_row(payload.get("row", {}))
    row["daemon_pid"] = str(payload.get("daemon_pid", "")).strip()
    return row


def daemon_rpc_remove_session(
    thread_id: str,
    *,
    daemon_rpc_call_fn: Callable[..., dict[str, object]],
    timeout_secs: float = 2.0,
) -> None:
    daemon_rpc_call_fn(
        "remove_session",
        {
            "thread_id": thread_id.lower(),
            "wait": {"kind": "absent"},
            "timeout_secs": timeout_secs,
        },
        timeout_secs=timeout_secs,
    )


def daemon_rpc_restart_codex(
    pane: str,
    thread_id: str,
    *,
    daemon_rpc_call_fn: Callable[..., dict[str, object]],
    timeout_secs: float = 10.0,
) -> None:
    daemon_rpc_call_fn(
        "restart_codex",
        {
            "pane": pane,
            "thread_id": thread_id.lower(),
            "timeout_secs": timeout_secs,
        },
        timeout_secs=timeout_secs,
    )


def daemon_rpc_continue_session(
    thread_id: str,
    *,
    daemon_rpc_call_fn: Callable[..., dict[str, object]],
    timeout_secs: float = 2.0,
) -> dict[str, str]:
    payload = daemon_rpc_call_fn(
        "continue_session",
        {
            "thread_id": thread_id.lower(),
            "timeout_secs": timeout_secs,
        },
        timeout_secs=timeout_secs,
    )
    row = normalize_runtime_row(payload.get("row", {}))
    row["daemon_pid"] = str(payload.get("daemon_pid", "")).strip()
    return row


def session_signature(session: acw_runtime.SavedSessionRecord) -> tuple[str, str, int]:
    return (session.pane, session.message, session.generation)


def load_scoped_session_records(
    socket_path: str | None = None,
    *,
    context: ScopedSessionContext,
) -> list[acw_runtime.SavedSessionRecord]:
    records: list[acw_runtime.SavedSessionRecord] = []
    for session in context.load_sessions(socket_path):
        records.append(
            acw_runtime.SavedSessionRecord(
                thread_id=session["thread_id"].lower(),
                name=session.get("name", ""),
                message=session.get("message", ""),
                pane=session.get("pane", ""),
                tmux_socket=context.normalize_tmux_socket(session.get("tmux_socket", "")),
                paused=bool(session.get("paused", False)),
                generation=int(session.get("generation", 0) or 0),
                state_file=session.get("state_file", ""),
                health=session.get("health", ""),
                health_detail=session.get("health_detail", ""),
                health_ts=session.get("health_ts", ""),
                last_continue_at=session.get("last_continue_at", ""),
            )
        )
    return records


def daemon_runtime_payload(runtime: Any, *, now_ts) -> dict:
    return acw_daemon.daemon_runtime_payload(runtime, now_ts=now_ts)


def daemon_runtime_row(thread_id: str, runtime: Any | None) -> dict[str, str]:
    if runtime is None:
        return {}
    row = normalize_runtime_row(runtime)
    if not row:
        return {}
    row["thread"] = thread_id.lower()
    row["thread_id"] = thread_id.lower()
    return row


def daemon_reconcile_once(
    scope: str,
    *,
    log_tail: Any,
    runtimes: dict[str, Any],
    context: DaemonReconcileContext,
) -> int:
    drained_events = context.drain_daemon_codex_log_events(log_tail)
    events_by_thread: dict[str, list[Any]] = defaultdict(list)
    for event in drained_events:
        events_by_thread[event.thread_id.lower()].append(event)

    desired_sessions = {
        session.thread_id: session
        for session in context.load_scoped_session_records(scope)
        if session.pane and context.is_thread_id(session.thread_id)
    }

    for thread_id in sorted(set(runtimes) - set(desired_sessions)):
        context.remove_runtime_state(thread_id, scope)
        del runtimes[thread_id]

    for thread_id, desired in desired_sessions.items():
        current = runtimes.get(thread_id)
        if current is None or session_signature(desired) != (
            current.pane,
            current.message,
            current.generation,
        ):
            runtimes[thread_id] = context.daemon_session_runtime(
                desired,
                codex_log_path=context.codex_log_path,
            )
            current = runtimes[thread_id]
        current.paused = desired.paused
        current.pane = desired.pane
        current.message = context.message_text(desired.message)
        current.tmux_socket = desired.tmux_socket
        current.generation = desired.generation
        if current.paused:
            current.runtime_status = "paused"
        elif current.runtime_status != "error":
            current.runtime_status = "running"
            current.runtime_detail = ""

    now = context.time_module.time()
    for thread_id, runtime in runtimes.items():
        runtime_events = context.daemon_runtime_events(runtime, events_by_thread)
        try:
            context.daemon_process_session_events(
                runtime,
                runtime_events,
            )
            last_location_check = float(getattr(runtime, "last_location_check", 0.0) or 0.0)
            if runtime_events or now - last_location_check > 15.0:
                runtime.last_location_check = now
                context.daemon_refresh_session_location(runtime)
            if not runtime.paused and now - runtime.last_health_check > 30.0:
                runtime.last_health_check = now
                context.daemon_refresh_session_health(
                    runtime,
                    codex_log_exists=context.codex_log_path.exists(),
                    now=now,
                )
        except Exception as exc:
            runtime.runtime_status = "error"
            runtime.runtime_detail = str(exc)
            context.append_log(
                Path(runtime.watch_log),
                f"error: daemon session crashed: {exc}\n{traceback.format_exc()}",
            )
        context.write_runtime_state(thread_id, context.daemon_runtime_payload(runtime), scope)

    return len(desired_sessions) + len(runtimes)


def daemon_wait_matches(
    row: dict[str, str],
    wait: dict[str, object],
    *,
    current_session_generation_fn: Callable[[dict], int],
) -> bool:
    kind = str(wait.get("kind", "")).strip()
    if not kind:
        return True
    if kind == "absent":
        return not row
    if kind != "runtime":
        return False
    if not row:
        return False

    pane = str(wait.get("pane", "")).strip()
    if pane and row.get("pane", "") != pane:
        return False

    generation = wait.get("generation", None)
    if generation not in ("", None):
        try:
            if current_session_generation_fn(row) != int(generation):
                return False
        except (TypeError, ValueError):
            return False

    statuses = {str(item).strip() for item in wait.get("statuses", []) if str(item).strip()}
    if statuses and row.get("runtime_status", "") not in statuses:
        return False

    if "paused" in wait and wait.get("paused", None) is not None:
        if (row.get("paused", "") == "1") != bool(wait.get("paused", False)):
            return False

    return True


def daemon_wait_for_condition(
    scope: str,
    *,
    thread_id: str,
    wait: dict[str, object],
    log_tail: Any,
    runtimes: dict[str, Any],
    send_progress,
    timeout_secs: float,
    context: DaemonWaitContext,
) -> dict[str, str]:
    kind = str(wait.get("kind", "")).strip()
    if not kind:
        context.daemon_reconcile_once(scope, log_tail=log_tail, runtimes=runtimes)
        return context.daemon_runtime_row(thread_id, runtimes.get(thread_id))

    if kind == "runtime":
        pane = str(wait.get("pane", "")).strip()
        send_progress(
            f"daemon: waiting for runtime convergence for pane={pane or '?'} thread_id={thread_id}"
        )
    elif kind == "absent":
        send_progress(f"daemon: waiting for runtime shutdown for thread_id={thread_id}")
    else:
        raise acw_rpc.RpcError("unsupported daemon wait condition", detail=kind)

    deadline = context.time_module.time() + max(timeout_secs, 0.1)
    last_row: dict[str, str] = {}
    while context.time_module.time() < deadline:
        context.daemon_reconcile_once(scope, log_tail=log_tail, runtimes=runtimes)
        last_row = context.daemon_runtime_row(thread_id, runtimes.get(thread_id))
        if daemon_wait_matches(
            last_row,
            wait,
            current_session_generation_fn=context.current_session_generation,
        ):
            return last_row
        context.time_module.sleep(0.05)

    context.daemon_reconcile_once(scope, log_tail=log_tail, runtimes=runtimes)
    last_row = context.daemon_runtime_row(thread_id, runtimes.get(thread_id))
    if daemon_wait_matches(
        last_row,
        wait,
        current_session_generation_fn=context.current_session_generation,
    ):
        return last_row

    raise acw_rpc.RpcError(
        f"daemon did not satisfy wait condition for thread_id={thread_id}",
        detail=f"wait={wait} last_row={last_row}",
    )


def daemon_handle_rpc_request(
    request: dict[str, object],
    *,
    scope: str,
    log_tail: Any,
    runtimes: dict[str, Any],
    send_progress,
    context: DaemonRpcServerContext,
) -> dict[str, object]:
    command = str(request.get("command", "")).strip()
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise acw_rpc.RpcError("invalid daemon RPC params", detail="params must be a JSON object")

    try:
        timeout_secs = float(params.get("timeout_secs", 5.0) or 5.0)
    except (TypeError, ValueError) as exc:
        raise acw_rpc.RpcError("invalid daemon RPC timeout", detail=str(exc)) from exc

    if command == "upsert_session":
        thread_id = str(params.get("thread_id", "")).strip().lower()
        session = params.get("session", {})
        wait = params.get("wait", {})
        if not context.is_thread_id(thread_id):
            raise acw_rpc.RpcError("invalid thread_id for upsert_session", detail=thread_id)
        if not isinstance(session, dict):
            raise acw_rpc.RpcError("invalid upsert_session payload", detail="session must be a JSON object")
        send_progress(f"daemon: updating managed session for pane={session.get('pane', '')}")
        context.write_session_state(thread_id, session)
        row = context.daemon_wait_for_condition(
            scope,
            thread_id=thread_id,
            wait=wait if isinstance(wait, dict) else {},
            log_tail=log_tail,
            runtimes=runtimes,
            send_progress=send_progress,
            timeout_secs=timeout_secs,
        )
        return {"daemon_pid": str(os.getpid()), "row": row}

    if command == "patch_session":
        thread_id = str(params.get("thread_id", "")).strip().lower()
        changes = params.get("changes", {})
        wait = params.get("wait", {})
        if not context.is_thread_id(thread_id):
            raise acw_rpc.RpcError("invalid thread_id for patch_session", detail=thread_id)
        if not isinstance(changes, dict):
            raise acw_rpc.RpcError("invalid patch_session payload", detail="changes must be a JSON object")
        send_progress(f"daemon: updating saved session state for thread_id={thread_id}")
        context.write_session_state(thread_id, changes)
        row = context.daemon_wait_for_condition(
            scope,
            thread_id=thread_id,
            wait=wait if isinstance(wait, dict) else {},
            log_tail=log_tail,
            runtimes=runtimes,
            send_progress=send_progress,
            timeout_secs=timeout_secs,
        )
        return {"daemon_pid": str(os.getpid()), "row": row}

    if command == "remove_session":
        thread_id = str(params.get("thread_id", "")).strip().lower()
        wait = params.get("wait", {})
        if not context.is_thread_id(thread_id):
            raise acw_rpc.RpcError("invalid thread_id for remove_session", detail=thread_id)
        send_progress(f"daemon: removing managed session for thread_id={thread_id}")
        context.remove_session_state(thread_id)
        row = context.daemon_wait_for_condition(
            scope,
            thread_id=thread_id,
            wait=wait if isinstance(wait, dict) else {"kind": "absent"},
            log_tail=log_tail,
            runtimes=runtimes,
            send_progress=send_progress,
            timeout_secs=timeout_secs,
        )
        return {"daemon_pid": str(os.getpid()), "row": row}

    if command == "restart_codex":
        pane = str(params.get("pane", "")).strip()
        thread_id = str(params.get("thread_id", "")).strip().lower()
        if not pane:
            raise acw_rpc.RpcError("restart_codex requires a pane")
        if not context.is_thread_id(thread_id):
            raise acw_rpc.RpcError("invalid thread_id for restart_codex", detail=thread_id)
        ok, detail = context.restart_codex_in_pane_result(pane, thread_id, send_progress)
        if not ok:
            raise acw_rpc.RpcError(f"failed to restart Codex in pane={pane}", detail=detail)
        return {"daemon_pid": str(os.getpid())}

    if command == "continue_session":
        thread_id = str(params.get("thread_id", "")).strip().lower()
        if not context.is_thread_id(thread_id):
            raise acw_rpc.RpcError("invalid thread_id for continue_session", detail=thread_id)
        runtime = runtimes.get(thread_id)
        if runtime is None:
            raise acw_rpc.RpcError(
                "continue_session requires a live managed runtime",
                detail=f"thread_id={thread_id}",
            )
        send_progress(f"daemon: sending continue to pane={runtime.pane}")
        ok, detail = context.continue_session(runtime)
        if not ok:
            raise acw_rpc.RpcError(
                f"failed to send continue for thread_id={thread_id}",
                detail=detail,
            )
        return {
            "daemon_pid": str(os.getpid()),
            "row": daemon_runtime_row(
                thread_id,
                acw_daemon.daemon_runtime_payload(runtime, now_ts=lambda: ""),
            ),
        }

    raise acw_rpc.RpcError("unknown daemon RPC command", detail=command)


def daemon_process_rpc_requests(
    rpc_server,
    *,
    scope: str,
    log_tail: Any,
    runtimes: dict[str, Any],
    context: DaemonRpcServeContext,
) -> int:
    return acw_rpc.serve_pending(
        rpc_server,
        handle_request=lambda request, send_progress: context.daemon_handle_rpc_request(
            request,
            scope=scope,
            log_tail=log_tail,
            runtimes=runtimes,
            send_progress=send_progress,
        ),
    )


def _safe_stderr_print(message: str) -> None:
    try:
        print(message, file=sys.stderr)
    except OSError:
        pass


def _is_retryable_daemon_error(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.OperationalError):
        error_name = str(getattr(exc, "sqlite_errorname", "")).strip().upper()
        error_code = getattr(exc, "sqlite_errorcode", None)
        if error_name in {"SQLITE_BUSY", "SQLITE_LOCKED"}:
            return True
        if error_code in {getattr(sqlite3, "SQLITE_BUSY", 5), getattr(sqlite3, "SQLITE_LOCKED", 6)}:
            return True
        text = str(exc).lower()
        return any(
            needle in text
            for needle in (
                "database is locked",
                "database schema is locked",
                "database table is locked",
            )
        )
    if isinstance(exc, BrokenPipeError):
        return True
    return isinstance(exc, OSError) and exc.errno in {
        errno.EPIPE,
        errno.ECONNRESET,
        errno.ECONNABORTED,
    }


def daemon_main(argv: list[str], *, context: DaemonMainContext) -> None:
    socket_path = ""
    rest = list(argv)
    while rest:
        tok = rest.pop(0)
        if tok == "--tmux-socket":
            if not rest:
                print("error: --tmux-socket requires a value", file=sys.stderr)
                sys.exit(2)
            socket_path = rest.pop(0)
        else:
            print(f"error: unknown daemon option '{tok}'", file=sys.stderr)
            sys.exit(2)

    scope = context.normalize_tmux_socket(socket_path)
    if scope:
        os.environ["AUTO_CONTINUE_TMUX_SOCKET"] = scope
    else:
        os.environ.pop("AUTO_CONTINUE_TMUX_SOCKET", None)

    pid_file = Path(context.daemon_pid_file_for_scope(scope))
    socket_file = Path(context.daemon_socket_file_for_scope(scope))
    socket_file.unlink(missing_ok=True)
    rpc_server = acw_rpc.listen(str(socket_file))
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    idle_since = 0.0
    disk_last_check = 0.0
    disk_last_alert = 0.0
    log_tail = context.daemon_log_tail_cls(log_path=context.codex_log_path)
    runtimes: dict[str, Any] = {}
    started_ts = context.watch_now_ts()
    context.write_daemon_scope_state(
        daemon_pid=str(os.getpid()),
        socket_path=str(socket_file),
        heartbeat_ts=started_ts,
        started_ts=started_ts,
        scope=scope,
    )

    try:
        while True:
            try:
                context.daemon_process_rpc_requests(
                    rpc_server,
                    scope=scope,
                    log_tail=log_tail,
                    runtimes=runtimes,
                )
                active_count = context.daemon_reconcile_once(
                    scope,
                    log_tail=log_tail,
                    runtimes=runtimes,
                )
                context.daemon_process_rpc_requests(
                    rpc_server,
                    scope=scope,
                    log_tail=log_tail,
                    runtimes=runtimes,
                )
                context.write_daemon_scope_state(
                    daemon_pid=str(os.getpid()),
                    socket_path=str(socket_file),
                    heartbeat_ts=context.watch_now_ts(),
                    started_ts=started_ts,
                    scope=scope,
                )
                disk_last_check, disk_last_alert = _check_disk_space(
                    runtimes,
                    last_check=disk_last_check,
                    last_alert=disk_last_alert,
                    now=context.time_module.time(),
                )
            except BaseException as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                if not _is_retryable_daemon_error(exc):
                    raise
                _safe_stderr_print(f"warning: transient daemon error: {exc}")
                context.time_module.sleep(context.daemon_poll_interval_secs)
                continue

            if active_count == 0:
                if idle_since == 0.0:
                    idle_since = context.time_module.time()
                elif context.time_module.time() - idle_since >= context.daemon_idle_exit_secs:
                    return
            else:
                idle_since = 0.0

            context.time_module.sleep(context.daemon_poll_interval_secs)
    finally:
        rpc_server.close()
        socket_file.unlink(missing_ok=True)
        log_tail.close()
        for thread_id in list(runtimes):
            try:
                context.remove_runtime_state(thread_id, scope)
            except sqlite3.Error as exc:
                _safe_stderr_print(
                    f"warning: failed to remove runtime state for thread_id={thread_id}: {exc}"
                )
        pid_file.unlink(missing_ok=True)


def accept_directory_trust_prompt_in_pane(
    pane: str,
    timeout_secs: float = 2.0,
    *,
    context: TrustPromptContext,
) -> bool:
    deadline = context.time_module.time() + timeout_secs
    while context.time_module.time() < deadline:
        text = context.run_tmux("capture-pane", "-t", pane, "-p", "-S", "-80") or ""
        if "Do you trust the contents of this directory?" in text:
            if context.run_tmux("send-keys", "-t", pane, "C-m") is None:
                print(f"error: tmux send-keys failed for pane={pane}", file=sys.stderr)
                sys.exit(1)
            return True
        context.time_module.sleep(0.5)
    return False


def command_name_for_pid(
    pid: str,
    *,
    check_output_fn=subprocess.check_output,
) -> str:
    if not pid.isdigit():
        return ""
    proc_comm = Path("/proc") / pid / "comm"
    try:
        return proc_comm.read_text().strip().lower()
    except OSError:
        pass
    try:
        return check_output_fn(
            ["ps", "-p", pid, "-o", "comm="],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip().lower()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def pane_command_snapshot(
    pane: str,
    *,
    context: PaneCommandContext,
) -> tuple[str, str, str, str]:
    listing = context.run_tmux("list-panes", "-t", pane, "-F", "#{pane_current_command}\t#{pane_pid}")
    if listing is None:
        return "", "", "", f"tmux list-panes failed for pane={pane}"
    line = next((entry for entry in listing.splitlines() if entry.strip()), "")
    if not line:
        return "", "", "", f"tmux pane {pane} is missing"
    parts = line.split("\t")
    current_command = parts[0].strip().lower() if parts else ""
    pane_pid = parts[1].strip() if len(parts) > 1 else ""
    if not pane_pid.isdigit():
        return current_command, "", "", f"tmux pane {pane} did not report a valid pane pid"
    shell_command = context.command_name_for_pid(pane_pid)
    if not shell_command:
        return current_command, "", pane_pid, f"could not determine root shell command for pane={pane}"
    return current_command, shell_command, pane_pid, ""


def wait_for_shell_prompt(
    pane: str,
    *,
    attempts: int = 30,
    poll_interval_secs: float = 0.1,
    context: WaitForShellContext,
) -> tuple[bool, str]:
    last_current = ""
    last_shell = ""
    for index in range(max(1, attempts)):
        current_command, shell_command, _pane_pid, error = context.pane_command_snapshot(pane)
        if error:
            return False, error
        last_current = current_command
        last_shell = shell_command
        if shell_command not in context.known_shell_commands:
            return False, f"pane {pane} root command is '{shell_command}', not a supported shell"
        if current_command == shell_command:
            return True, ""
        if index + 1 < attempts:
            context.time_module.sleep(poll_interval_secs)
    current_label = last_current or "unknown"
    shell_label = last_shell or "shell"
    return (
        False,
        f"pane {pane} did not return to a shell prompt (current command '{current_label}', expected '{shell_label}')",
    )


def wait_for_codex_process(
    pane: str,
    *,
    attempts: int = 30,
    poll_interval_secs: float = 0.1,
    context: WaitForCodexContext,
) -> tuple[bool, str]:
    for index in range(max(1, attempts)):
        _current_command, _shell_command, pane_pid, error = context.pane_command_snapshot(pane)
        if error:
            return False, error
        if context.codex_pids_in_process_tree(pane_pid):
            return True, ""
        if index + 1 < attempts:
            context.time_module.sleep(poll_interval_secs)
    return False, f"Codex did not relaunch in pane {pane}"


def terminate_codex_processes_in_pane(
    pane: str,
    *,
    context: TerminateCodexContext,
) -> tuple[bool, str]:
    current_command, shell_command, pane_pid, error = context.pane_command_snapshot(pane)
    if error:
        return False, error
    if shell_command in context.known_shell_commands and current_command == shell_command:
        return True, ""

    top_level_pids = context.codex_top_level_descendants(pane_pid)
    if not top_level_pids:
        current_label = current_command or "unknown"
        return (
            False,
            f"pane {pane} is running '{current_label}', but no live Codex process was found to restart",
        )

    for pid in top_level_pids:
        try:
            context.os_kill(int(pid), context.sigterm)
        except ProcessLookupError:
            continue
        except OSError as exc:
            return False, f"failed to terminate Codex pid={pid} in pane {pane}: {exc}"
    return True, ""


def restart_codex_in_pane_result(
    pane: str,
    thread_id: str,
    *,
    progress,
    context: RestartCodexContext,
) -> tuple[bool, str]:
    at_shell, shell_error = context.wait_for_shell_prompt(pane, attempts=1)
    if not at_shell:
        progress(f"restarting codex: terminating live Codex process in pane={pane}")
        terminated, terminate_error = context.terminate_codex_processes_in_pane(pane)
        if not terminated:
            return False, terminate_error
        progress(f"restarting codex: waiting for shell prompt in pane={pane}")
        at_shell, shell_error = context.wait_for_shell_prompt(pane)
        if not at_shell:
            return False, shell_error
    progress(f"restarting codex: launching codex resume in pane={pane}")
    if context.run_tmux("send-keys", "-t", pane, "-l", f"codex resume {thread_id}") is None:
        return False, f"tmux send-keys failed for pane={pane}"
    if context.run_tmux("send-keys", "-t", pane, "C-m") is None:
        return False, f"tmux send-keys failed for pane={pane}"
    progress(f"restarting codex: waiting for Codex process in pane={pane}")
    relaunched, relaunch_error = context.wait_for_codex_process(pane)
    if not relaunched:
        return False, relaunch_error
    progress(f"restarting codex: checking directory trust prompt in pane={pane}")
    context.accept_directory_trust_prompt_in_pane(pane, timeout_secs=2.0)
    return True, ""


def restart_codex_in_pane(
    pane: str,
    thread_id: str,
    *,
    context: RestartCodexContext,
) -> None:
    ok, detail = restart_codex_in_pane_result(
        pane,
        thread_id,
        progress=context.print_progress,
        context=context,
    )
    if ok:
        return
    print(f"error: {detail}", file=sys.stderr)
    sys.exit(1)
