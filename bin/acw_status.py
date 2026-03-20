from __future__ import annotations

from collections import defaultdict
import json
import os
from pathlib import Path
import time
from typing import Callable


_STATE_STYLES = {
    "running": "green",
    "busy": "green",
    "waiting": "cyan",
    "input": "magenta",
    "paused": "yellow",
    "warn": "dark_orange",
    "error": "bold red",
    "dead": "dim",
}


def read_state_json(
    state_path: str,
    *,
    state_dir: str,
    session_ref_prefix: str,
    read_state_ref_fn: Callable[..., dict[str, object]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    if not state_path:
        return result
    if state_path.startswith(session_ref_prefix):
        data = read_state_ref_fn(state_path, state_dir=state_dir)
        for key in ("health", "health_detail", "health_ts", "last_continue_at", "continue_count", "thread_id"):
            if key in data:
                result[key] = str(data[key])
        return result
    if not os.path.isfile(state_path):
        return result
    try:
        with open(state_path) as handle:
            data = json.load(handle)
        for key in (
            "health",
            "health_detail",
            "health_ts",
            "last_continue_at",
            "continue_count",
            "window_name",
            "thread_id",
        ):
            if key in data:
                result[key] = str(data[key])
    except (OSError, json.JSONDecodeError):
        pass
    return result


def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def continue_count_for_watch_log(watch_log: str) -> int:
    if not watch_log or not os.path.isfile(watch_log):
        return 0
    try:
        with open(watch_log, encoding="utf-8", errors="ignore") as handle:
            return sum(1 for line in handle if "continue: sent" in line)
    except OSError:
        return 0


def average_continue_label(
    row: dict[str, str],
    session_json: dict[str, str],
    *,
    thread_started_at_fn: Callable[[str], float | int | None],
    continue_count_for_watch_log_fn: Callable[[str], int],
    format_age_fn: Callable[[float, bool], str],
    now_fn: Callable[[], float] = time.time,
) -> str:
    now = now_fn()
    continue_count = _coerce_int(session_json.get("continue_count", "0"))
    if continue_count <= 0:
        continue_count = continue_count_for_watch_log_fn(row.get("watch", ""))
    if continue_count <= 0:
        return ""
    started_at = thread_started_at_fn(row.get("thread", ""))
    if not started_at:
        return ""
    average_secs = max(1, (now - float(started_at)) / continue_count)
    return f"{format_age_fn(now - average_secs, True)}/acw"


def average_continue_value(label: str) -> str:
    label = label.strip()
    if label.endswith("/acw"):
        return label[:-4]
    return label


def status_time_cell(started: str, last_acw: str, average_continue: str) -> str:
    started = started.strip() or "-"
    last_acw = last_acw.strip() or "-"
    average_continue = average_continue_value(average_continue) or "-"
    return f"s: {started}\nl: {last_acw}\na: {average_continue}"


def resolve_message(
    row: dict[str, str],
    *,
    is_thread_id_fn: Callable[[str], bool],
    read_session_state_fn: Callable[[str], dict[str, object]],
) -> tuple[str, str]:
    thread_id = row.get("thread", "")
    if thread_id and is_thread_id_fn(thread_id):
        session = read_session_state_fn(thread_id)
        message = str(session.get("message", ""))
        if message:
            return "inline", message
    if row.get("msg_inline"):
        return "inline", row["msg_inline"]
    return "", ""


def short_thread_id(thread_id: str, *, is_thread_id_fn: Callable[[str], bool]) -> str:
    if not is_thread_id_fn(thread_id):
        return thread_id
    return f"{thread_id[:8]}…{thread_id[-4:]}"


def compute_state(
    row: dict[str, str],
    session_json: dict[str, str],
    *,
    is_pid_stopped_fn: Callable[[int], bool],
) -> str:
    if row.get("entity_kind") == "dead_session":
        return "dead"
    if row.get("anomalies"):
        return "error"
    runtime_status = row.get("runtime_status", "")
    if runtime_status == "paused" or row.get("paused", "") == "1":
        return "paused"
    if runtime_status == "error":
        return "error"
    if runtime_status == "warn":
        return "warn"
    if runtime_status == "running":
        health = session_json.get("health", "")
        if health == "stale":
            health = "warn"
        if health and health != "ok":
            return health
        return "running"
    if runtime_status == "stopping":
        return "warn"
    runtime_id = row.get("runtime_id", "") or row.get("pid", "")
    if runtime_id and runtime_id.isdigit() and is_pid_stopped_fn(int(runtime_id)):
        return "paused"
    if not runtime_id:
        return "dead"
    health = session_json.get("health", "")
    if health == "stale":
        health = "warn"
    if health and health != "ok":
        return health
    return "running"


def message_summary_for_row(
    row: dict[str, str],
    *,
    resolve_message_fn: Callable[[dict[str, str]], tuple[str, str]],
) -> str:
    mode, value = resolve_message_fn(row)
    if mode == "inline" and value:
        return value
    return "-"


def styled_state(state: str) -> str:
    style = _STATE_STYLES.get(state, "")
    return f"[{style}]{state}[/{style}]" if style else state


def state_summary(
    state: str,
    health_detail: str,
    *,
    truncate_fn: Callable[[str, int], str],
) -> str:
    detail = health_detail.strip()
    if not detail or state == "running":
        return styled_state(state)
    detail_summary = truncate_fn(detail, 28)
    return f"{styled_state(state)}\n[dim]{detail_summary}[/dim]"


def status_target_for_row(row: dict[str, str], current_pane: str, window_label: str) -> str:
    if row.get("entity_kind") != "dead_session" and current_pane:
        return current_pane
    name = row.get("name", "").strip()
    if name and name != "-":
        return name
    session_name = row.get("session_name", "").strip()
    if session_name and session_name != "-":
        return session_name
    if window_label and ":" in window_label:
        tail = window_label.rsplit(":", 1)[-1].strip()
        if tail and tail != "-":
            return tail
    return current_pane or row.get("pane", "")


def sort_session_key(session_name: str) -> tuple[int, object]:
    if session_name.isdigit():
        return 0, int(session_name)
    return 1, session_name


def status_sort_key(
    item: tuple[dict[str, str], str, str],
    *,
    is_pane_id_fn: Callable[[str], bool],
) -> tuple[tuple[int, object], int, int, int, str]:
    row, current_pane, window_label = item
    session_name = "~"
    window_index = 10**9
    head, sep, _tail = window_label.partition(":")
    if sep:
        rest = window_label[len(head) + 1 :]
        window_part, sep2, _ = rest.partition(":")
        if sep2 and window_part.isdigit():
            session_name = head
            window_index = int(window_part)

    pane_number = 10**9
    pane = current_pane or row.get("pane", "")
    if is_pane_id_fn(pane):
        try:
            pane_number = int(pane[1:])
        except ValueError:
            pass
    pid_number = 10**9
    pid = row.get("pid", "")
    if pid.isdigit():
        pid_number = int(pid)
    return (sort_session_key(session_name), window_index, pane_number, pid_number, window_label)


def status_recommendations(
    resolved: list[tuple[dict[str, str], str, str]],
    *,
    compute_state_fn: Callable[[dict[str, str], dict[str, str]], str],
    read_state_json_fn: Callable[[str], dict[str, str]],
    status_target_for_row_fn: Callable[[dict[str, str], str, str], str],
    quote_target_fn: Callable[[str], str],
) -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()
    for row, current_pane, window_label in resolved:
        if row.get("anomalies"):
            continue
        if row.get("entity_kind") == "dead_session":
            continue
        state = compute_state_fn(row, read_state_json_fn(row["state"]))
        if state not in {"warn", "error"}:
            continue
        target = status_target_for_row_fn(row, current_pane, window_label)
        if not target:
            continue
        command = f"acw doctor {quote_target_fn(target)}"
        if command in seen:
            continue
        seen.add(command)
        commands.append(command)
    return commands


def daemon_scope_alive(scope_state: dict[str, str]) -> bool:
    alive_text = str(scope_state.get("alive", "")).strip().lower()
    if alive_text in {"1", "true", "yes"}:
        return True
    if alive_text in {"0", "false", "no"}:
        return False
    daemon_pid = str(scope_state.get("daemon_pid", "")).strip()
    if not daemon_pid.isdigit():
        return False
    try:
        os.kill(int(daemon_pid), 0)
    except OSError:
        return False
    socket_path = str(scope_state.get("socket_path", "")).strip()
    if not socket_path or not Path(socket_path).exists():
        return False
    return True


def daemon_scope_down_detail(scope_state: dict[str, str], *, pid_file: str, socket_file: str) -> str:
    daemon_pid = str(scope_state.get("daemon_pid", "")).strip()
    if daemon_pid:
        if not daemon_pid.isdigit():
            return f"daemon pid '{daemon_pid}' is invalid"
        try:
            os.kill(int(daemon_pid), 0)
        except OSError:
            if Path(pid_file).exists():
                return f"pid file points to dead daemon pid {daemon_pid}"
            return f"daemon pid {daemon_pid} is not running"

    socket_path = str(scope_state.get("socket_path", "")).strip() or socket_file
    if socket_path and not Path(socket_path).exists():
        return f"control socket {socket_path} is missing"
    return "no daemon heartbeat or control socket found"


def observed_daemon_scope_state(
    *,
    socket_path: str | None,
    read_daemon_scope_state_fn: Callable[[str | None], dict[str, str]],
    read_pid_file_fn: Callable[[str], str | None],
    daemon_pid_file_for_scope_fn: Callable[[str | None], str],
    daemon_socket_file_for_scope_fn: Callable[[str | None], str],
    daemon_scope_alive_fn: Callable[[dict[str, str]], bool],
    daemon_scope_down_detail_fn: Callable[[dict[str, str], str, str], str],
) -> dict[str, str]:
    scope_state = dict(read_daemon_scope_state_fn(socket_path))
    pid_file = daemon_pid_file_for_scope_fn(socket_path)
    socket_file = daemon_socket_file_for_scope_fn(socket_path)
    pid_from_file = read_pid_file_fn(pid_file) or ""
    socket_exists = Path(socket_file).exists()

    if pid_from_file:
        scope_state["daemon_pid"] = pid_from_file
    if socket_exists:
        scope_state["socket_path"] = socket_file
    elif not str(scope_state.get("socket_path", "")).strip() and pid_from_file:
        scope_state["socket_path"] = socket_file

    if not str(scope_state.get("alive", "")).strip():
        scope_state["alive"] = "1" if daemon_scope_alive_fn(scope_state) else "0"

    if scope_state["alive"] in {"0", "false", "no"} and not str(scope_state.get("detail", "")).strip():
        scope_state["detail"] = daemon_scope_down_detail_fn(
            scope_state,
            pid_file,
            socket_file,
        )

    return scope_state


def shared_daemon_error_message(
    unwatched_rows: list[tuple[dict[str, str], str]],
    *,
    observed_daemon_scope_state_fn: Callable[[], dict[str, str]],
    daemon_scope_alive_fn: Callable[[dict[str, str]], bool],
    tmux_scope_label_fn: Callable[[], str],
) -> str:
    if not unwatched_rows:
        return ""
    scope_state = observed_daemon_scope_state_fn()
    if daemon_scope_alive_fn(scope_state):
        return ""
    detail = str(scope_state.get("detail", "")).strip()
    scope_label = tmux_scope_label_fn()
    message = (
        f"Shared daemon error: acw daemon is down for tmux scope {scope_label}; "
        f"{len(unwatched_rows)} managed live Codex pane(s) currently have no runtime."
    )
    if detail:
        message += f" Detail: {detail}."
    message += " Fix: run acw repair or any mutating acw command to restart the daemon."
    return message


def status_invariant_messages(
    resolved: list[tuple[dict[str, str], str, str]],
    *,
    is_thread_id_fn: Callable[[str], bool],
    short_thread_id_fn: Callable[[str], str],
    shared_daemon_error_message_fn: Callable[[list[tuple[dict[str, str], str]]], str],
) -> list[str]:
    messages: list[str] = []
    seen: set[str] = set()

    watcher_rows: list[tuple[dict[str, str], str]] = []
    unwatched_rows: list[tuple[dict[str, str], str]] = []
    unresolved_rows: list[tuple[dict[str, str], str]] = []
    dead_rows: list[tuple[dict[str, str], str, str]] = []
    rows_by_session_name: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row, current_pane, window_label in resolved:
        kind = row.get("entity_kind", "watcher")
        session_name = row.get("session_name", "").strip()
        if session_name:
            rows_by_session_name[session_name].append(row)
        if kind == "watcher":
            watcher_rows.append((row, current_pane))
        elif kind == "unwatched_pane":
            unwatched_rows.append((row, current_pane))
        elif kind == "unresolved_pane":
            unresolved_rows.append((row, current_pane))
        elif kind == "dead_session":
            dead_rows.append((row, current_pane, window_label))

    daemon_down_message = shared_daemon_error_message_fn(unwatched_rows)
    live_issue_found = any(row.get("anomalies") for row, _ in watcher_rows) or bool(unwatched_rows or unresolved_rows)

    watchers_by_pane: dict[str, list[dict[str, str]]] = defaultdict(list)
    watchers_by_thread: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row, current_pane in watcher_rows:
        pane = current_pane or row.get("pane", "")
        if pane:
            watchers_by_pane[pane].append(row)
        thread_id = row.get("thread", "")
        if is_thread_id_fn(thread_id):
            watchers_by_thread[thread_id.lower()].append(row)

    for pane, rows in sorted(watchers_by_pane.items()):
        if len(rows) <= 1:
            continue
        runtime_ids = ", ".join(sorted((row.get("runtime_id") or row.get("pid", "?")) for row in rows))
        msg = f"Invariant error: pane {pane} has {len(rows)} managed runtimes ({runtime_ids}). Fix: acw repair {pane}"
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    for thread_id, rows in sorted(watchers_by_thread.items()):
        panes = sorted({row.get("pane", "") for row in rows if row.get("pane", "")})
        if len(panes) <= 1:
            continue
        cleanup = "; ".join(f"acw stop {pane}" for pane in panes)
        msg = (
            f"Invariant error: thread {short_thread_id_fn(thread_id)} has managed runtimes on panes {', '.join(panes)}. "
            f"Clean up: {cleanup}; then start exactly one managed runtime."
        )
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    for session_name, rows in sorted(rows_by_session_name.items()):
        threads = sorted({row.get("thread", "").lower() for row in rows if is_thread_id_fn(row.get("thread", ""))})
        if len(threads) <= 1:
            continue
        thread_list = ", ".join(short_thread_id_fn(thread_id) for thread_id in threads)
        msg = (
            f"Invariant error: agent name '{session_name}' maps to multiple threads ({thread_list}). "
            "Clean up the duplicate session/pane state before targeting it by name."
        )
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    if daemon_down_message:
        if daemon_down_message not in seen:
            seen.add(daemon_down_message)
            messages.append(daemon_down_message)
    else:
        for row, current_pane in unwatched_rows:
            pane = current_pane or row.get("pane", "")
            msg = (
                f"Invariant error: live Codex pane {pane} has thread {short_thread_id_fn(row.get('thread', ''))} "
                f"but no runtime. Fix: acw repair {pane}"
            )
            if msg not in seen:
                seen.add(msg)
                messages.append(msg)

    for row, current_pane in unresolved_rows:
        pane = current_pane or row.get("pane", "")
        detail = row.get("anomaly_detail", "").strip() or "could not determine thread_id from the live Codex process"
        msg = (
            f"Invariant error: live Codex pane {pane} has a Codex process but no detectable thread id ({detail}). "
            f"Clean up: restart Codex there, then run acw start {pane}."
        )
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    for row, current_pane in watcher_rows:
        anomalies = set(row.get("anomalies", ()))
        pane = current_pane or row.get("pane", "")
        runtime_id = row.get("runtime_id") or row.get("pid", "?")
        if "no saved session" in anomalies:
            msg = (
                f"Invariant error: pane {pane} runtime id={runtime_id} has no saved session. "
                f"Fix: acw repair {pane}"
            )
            if msg not in seen:
                seen.add(msg)
                messages.append(msg)
        if "no live Codex agent detected in pane" in anomalies:
            msg = (
                f"Invariant error: pane {pane} runtime id={runtime_id} has no live Codex agent in the pane. "
                f"Fix: acw repair {pane}"
            )
            if msg not in seen:
                seen.add(msg)
                messages.append(msg)
        if any(detail.startswith("could not determine thread_id") for detail in anomalies):
            msg = (
                f"Invariant error: pane {pane} runtime id={runtime_id} has a live Codex process but its thread id "
                f"could not be detected. Fix: acw repair {pane} --dry-run"
            )
            if msg not in seen:
                seen.add(msg)
                messages.append(msg)
        if any(detail.startswith("pane agent thread is ") for detail in anomalies):
            msg = (
                f"Invariant error: pane {pane} runtime id={runtime_id} is bound to the wrong thread. "
                f"Fix: acw repair {pane}"
            )
            if msg not in seen:
                seen.add(msg)
                messages.append(msg)

    if dead_rows:
        msg = "Hint: run acw cleanup to remove dead sessions."
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    if live_issue_found:
        msg = "Hint: run acw repair --dry-run for live invariant fixes."
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    return messages


def status_row_detail(row: dict[str, str], session_json: dict[str, str]) -> str:
    detail = row.get("anomaly_detail", "").strip()
    if detail:
        return detail
    runtime_detail = row.get("runtime_detail", "").strip()
    if runtime_detail:
        return runtime_detail
    return session_json.get("health_detail", "").strip()


def paused_state_detail(activity: str, detail: str) -> str:
    activity = activity.strip()
    detail = detail.strip()
    if activity and detail:
        return f"{activity} · {detail}"
    return activity or detail


def display_state(base_state: str, activity: str) -> str:
    activity = activity.strip()
    if base_state == "running" and activity in {"busy", "waiting", "input"}:
        return activity
    return base_state


def print_status_recommendations(
    resolved: list[tuple[dict[str, str], str, str]],
    *,
    status_recommendations_fn: Callable[[list[tuple[dict[str, str], str, str]]], list[str]],
    status_invariant_messages_fn: Callable[[list[tuple[dict[str, str], str, str]]], list[str]],
    print_fn: Callable[[str], None] = print,
) -> None:
    for command in status_recommendations_fn(resolved):
        print_fn(f"Recommendation: run {command}")
    for message in status_invariant_messages_fn(resolved):
        print_fn(message)


def status_table(
    resolved: list[tuple[dict[str, str], str, str]],
    *,
    read_state_json_fn: Callable[[str], dict[str, str]],
    compute_state_fn: Callable[[dict[str, str], dict[str, str]], str],
    status_row_detail_fn: Callable[[dict[str, str], dict[str, str]], str],
    thread_times_fn: Callable[[str, bool], tuple[str, str]],
    average_continue_label_fn: Callable[[dict[str, str], dict[str, str]], str],
    format_age_fn: Callable[[float, bool], str],
    last_agent_snippet_for_pane_fn: Callable[[str, str], str],
    paused_activity_for_pane_fn: Callable[[str], str],
    message_summary_for_row_fn: Callable[[dict[str, str]], str],
    branch_for_pane_fn: Callable[[str, str], str],
    status_window_pane_cell_fn: Callable[[str, str, str, str], str],
    state_summary_fn: Callable[[str, dict[str, str]], str],
    render_status_table_fn: Callable[[list[tuple[str, str, str, str, str]], int, int], None],
    section_every: int,
    max_msg_lines: int,
) -> None:
    rows_data = status_rows_data(
        resolved,
        read_state_json_fn=read_state_json_fn,
        compute_state_fn=compute_state_fn,
        status_row_detail_fn=status_row_detail_fn,
        thread_times_fn=thread_times_fn,
        average_continue_label_fn=average_continue_label_fn,
        format_age_fn=format_age_fn,
        last_agent_snippet_for_pane_fn=last_agent_snippet_for_pane_fn,
        paused_activity_for_pane_fn=paused_activity_for_pane_fn,
        message_summary_for_row_fn=message_summary_for_row_fn,
        branch_for_pane_fn=branch_for_pane_fn,
        status_window_pane_cell_fn=status_window_pane_cell_fn,
        state_summary_fn=state_summary_fn,
    )
    render_status_table_fn(rows_data, section_every, max_msg_lines)


def status_rows_data(
    resolved: list[tuple[dict[str, str], str, str]],
    *,
    read_state_json_fn: Callable[[str], dict[str, str]],
    compute_state_fn: Callable[[dict[str, str], dict[str, str]], str],
    status_row_detail_fn: Callable[[dict[str, str], dict[str, str]], str],
    thread_times_fn: Callable[[str, bool], tuple[str, str]],
    average_continue_label_fn: Callable[[dict[str, str], dict[str, str]], str],
    format_age_fn: Callable[[float, bool], str],
    last_agent_snippet_for_pane_fn: Callable[[str, str], str],
    paused_activity_for_pane_fn: Callable[[str], str],
    message_summary_for_row_fn: Callable[[dict[str, str]], str],
    branch_for_pane_fn: Callable[[str, str], str],
    status_window_pane_cell_fn: Callable[[str, str, str, str], str],
    state_summary_fn: Callable[[str, dict[str, str]], str],
) -> list[tuple[str, str, str, str, str]]:
    rows_data: list[tuple[str, str, str, str, str]] = []
    for row, current_pane, window_label in resolved:
        session_json = read_state_json_fn(row["state"])
        state_value = compute_state_fn(row, session_json)
        state_detail = status_row_detail_fn(row, session_json)
        activity = paused_activity_for_pane_fn(current_pane) if state_value in {"running", "paused"} else ""
        if state_value == "paused":
            state_detail = paused_state_detail(activity, state_detail)
        rendered_state = display_state(state_value, activity)
        started, _last_activity = thread_times_fn(row["thread"], True)
        last_acw = session_json.get("last_continue_at", "")
        if last_acw:
            try:
                epoch = time.mktime(time.strptime(last_acw, "%Y-%m-%d %H:%M:%S"))
                last_acw = format_age_fn(epoch, True)
            except (ValueError, OverflowError):
                pass
        if not last_acw:
            last_acw = "-"
        average_continue = average_continue_label_fn(row, session_json)
        time_cell = status_time_cell(started, last_acw, average_continue)
        last_agent = last_agent_snippet_for_pane_fn(current_pane, row["thread"]) or "-"
        msg_raw = message_summary_for_row_fn(row)
        branch = branch_for_pane_fn(current_pane, row["thread"])
        window_pane = status_window_pane_cell_fn(
            window_label,
            current_pane,
            row["thread"],
            branch,
        )
        rows_data.append(
            (
                window_pane,
                state_summary_fn(rendered_state, {"health_detail": state_detail}),
                time_cell,
                last_agent,
                msg_raw,
            )
        )
    return rows_data


def status_details(
    resolved: list[tuple[dict[str, str], str, str]],
    *,
    model,
    read_state_json_fn: Callable[[str], dict[str, str]],
    compute_state_fn: Callable[[dict[str, str], dict[str, str]], str],
    status_row_detail_fn: Callable[[dict[str, str], dict[str, str]], str],
    resolve_message_fn: Callable[[dict[str, str]], tuple[str, str]],
    thread_times_fn: Callable[[str, bool], tuple[str, str]],
    last_agent_snippet_for_pane_fn: Callable[[str, str], str],
    paused_activity_for_pane_fn: Callable[[str], str],
    thread_provenance_text_fn: Callable[[str], str],
    print_status_recommendations_fn: Callable[[list[tuple[dict[str, str], str, str]]], None],
    print_fn: Callable[[str], None] = print,
) -> None:
    total = len(resolved)
    for idx, (row, current_pane, window_label) in enumerate(resolved, 1):
        if idx > 1:
            print_fn("")

        session_json = read_state_json_fn(row["state"])
        state_value = compute_state_fn(row, session_json)
        state_detail = status_row_detail_fn(row, session_json)
        activity = paused_activity_for_pane_fn(current_pane) if state_value in {"running", "paused"} else ""

        last_event = "-"
        if row["watch"]:
            try:
                with open(row["watch"]) as handle:
                    lines = handle.readlines()
                if lines:
                    last_event = lines[-1].rstrip("\n")
            except OSError:
                pass
            if not last_event:
                last_event = "-"

        mode, value = resolve_message_fn(row)
        msg_full = value if mode == "inline" and value else "-"

        started, last_activity = thread_times_fn(row["thread"], False)
        last_agent = last_agent_snippet_for_pane_fn(current_pane, row["thread"]) or "-"
        last_acw = session_json.get("last_continue_at", "-")

        print_fn(f"=== Entry {idx}/{total} ===")
        print_fn(f"  {'WINDOW:':<16} {window_label}")
        print_fn(f"  {'PANE:':<16} {current_pane or '(no live pane)'}")
        runtime_id = row.get("runtime_id") or row.get("pid", "")
        print_fn(f"  {'ID:':<16} {runtime_id or '-'}")
        print_fn(f"  {'THREAD_ID:':<16} {row['thread'] or 'unknown'}")
        pane_record = model.panes_by_id.get(current_pane) if current_pane else None
        provenance = thread_provenance_text_fn(pane_record.detected_thread_source) if pane_record else ""
        if provenance:
            print_fn(f"  {'THREAD_PROVENANCE:':<16} {provenance}")
        print_fn(f"  {'STATE:':<16} {state_value}")
        if activity:
            label = "PAUSED_ACTIVITY:" if state_value == "paused" else "ACTIVITY:"
            print_fn(f"  {label:<16} {activity}")
        print_fn(f"  {'STARTED:':<16} {started}")
        print_fn(f"  {'LAST_ACTIVITY:':<16} {last_activity}")
        print_fn(f"  {'LAST_ACW:':<16} {last_acw}")
        print_fn(f"  {'LAST_AGENT:':<16} {last_agent}")
        if state_detail:
            label = "ANOMALY" if row.get("anomalies") else "HEALTH_DETAIL"
            print_fn(f"  {f'{label}:':<16} {state_detail}")
        health_ts = session_json.get("health_ts", "")
        if health_ts:
            print_fn(f"  {'HEALTH_SINCE:':<16} {health_ts}")
        print_fn(f"  {'LAST_EVENT:':<16} {last_event}")
        print_fn(f"  {'MESSAGE:':<16} {msg_full}")
        if row["watch"]:
            print_fn(f"  {'WATCH_LOG:':<16} {row['watch']}")
        if row["state"]:
            print_fn(f"  {'STATE_REF:':<16} {row['state']}")
    print_status_recommendations_fn(resolved)
