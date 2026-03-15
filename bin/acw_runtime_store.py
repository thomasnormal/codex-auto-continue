from __future__ import annotations

import os
from typing import Callable

import acw_state


def watch_log_for_key(state_dir: str, key: str) -> str:
    return os.path.join(state_dir, f"auto_continue_logwatch.{key}.log")


def state_file_for_thread(thread_id: str) -> str:
    return acw_state.session_state_ref(thread_id)


def state_db_file(state_dir: str) -> str:
    return acw_state.state_db_path(state_dir)


def write_runtime_state(
    thread_id: str,
    data: dict,
    *,
    state_dir: str,
    scope: str,
) -> None:
    acw_state.write_runtime_state(
        thread_id,
        data,
        state_dir=state_dir,
        scope=scope,
    )


def remove_runtime_state(
    thread_id: str,
    *,
    state_dir: str,
    scope: str,
) -> None:
    acw_state.remove_runtime_state(
        thread_id,
        state_dir=state_dir,
        scope=scope,
    )


def read_runtime_rows(
    *,
    state_dir: str,
    scope: str,
) -> list[dict[str, object]]:
    rows = acw_state.read_runtime_rows(
        state_dir=state_dir,
        scope=scope,
    )
    return [{str(key): value for key, value in row.items()} for row in rows]


def read_daemon_scope_state(
    *,
    state_dir: str,
    scope: str,
) -> dict[str, str]:
    row = acw_state.read_daemon_scope_state(
        state_dir=state_dir,
        scope=scope,
    )
    return {str(key): str(value) for key, value in row.items()}


def write_daemon_scope_state(
    *,
    state_dir: str,
    scope: str,
    daemon_pid: str,
    socket_path: str,
    heartbeat_ts: str,
    started_ts: str = "",
) -> None:
    acw_state.write_daemon_scope_state(
        state_dir=state_dir,
        scope=scope,
        daemon_pid=daemon_pid,
        socket_path=socket_path,
        heartbeat_ts=heartbeat_ts,
        started_ts=started_ts,
    )


def watcher_rows(
    pane_filter: str = "",
    *,
    read_runtime_rows_fn: Callable[[], list[dict[str, str]]],
) -> list[dict[str, str]]:
    rows = read_runtime_rows_fn()
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


def watcher_pids_for_pane(
    pane: str,
    *,
    watcher_rows_fn: Callable[[str], list[dict[str, str]]],
) -> list[str]:
    return [row["pid"] for row in watcher_rows_fn(pane)]


def watcher_row_for_pane(
    pane: str,
    *,
    watcher_rows_fn: Callable[[str], list[dict[str, str]]],
) -> dict[str, str] | None:
    rows = watcher_rows_fn(pane)
    if len(rows) == 1:
        return rows[0]
    return None


def other_watcher_for_thread(
    pane: str,
    thread_id: str,
    *,
    is_thread_id_fn: Callable[[str], bool],
    watcher_rows_fn: Callable[[str], list[dict[str, str]]],
) -> dict[str, str] | None:
    if not is_thread_id_fn(thread_id):
        return None
    for row in watcher_rows_fn(""):
        if row.get("pane", "") == pane:
            continue
        row_thread = row.get("thread", "")
        if row_thread and row_thread.lower() == thread_id.lower():
            return row
    return None


def thread_from_running_watcher_for_pane(
    pane: str,
    *,
    watcher_rows_fn: Callable[[str], list[dict[str, str]]],
    is_thread_id_fn: Callable[[str], bool],
) -> str | None:
    rows = watcher_rows_fn(pane)
    if len(rows) != 1:
        return None
    thread_id = rows[0]["thread"]
    if is_thread_id_fn(thread_id):
        return thread_id.lower()
    return None
