from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable


ProcessSnapshot = tuple[dict[str, tuple[str, ...]], dict[str, str]]


@dataclass(frozen=True)
class SavedSessionRecord:
    thread_id: str
    name: str
    message: str
    pane: str = ""
    tmux_socket: str = ""
    paused: bool = False
    generation: int = 0
    state_file: str = ""
    health: str = ""
    health_detail: str = ""
    health_ts: str = ""
    last_continue_at: str = ""


@dataclass(frozen=True)
class LiveWatcherRecord:
    pid: str
    pane: str
    thread_id: str
    state_file: str
    watch_log: str
    cwd: str
    message_inline: str
    tmux_socket: str
    paused: bool = False
    runtime_status: str = ""
    runtime_detail: str = ""
    generation: int = 0
    worker_label: str = ""


@dataclass(frozen=True)
class LivePaneRecord:
    pane_id: str
    window_label: str
    window_name: str
    shell_pid: str
    detected_thread: str
    detected_thread_source: str
    detected_thread_detail: str
    has_codex_process: bool


@dataclass(frozen=True)
class RuntimeSnapshot:
    panes: tuple[LivePaneRecord, ...]
    watchers: tuple[LiveWatcherRecord, ...]
    sessions: tuple[SavedSessionRecord, ...]


@dataclass(frozen=True)
class RuntimeModel:
    snapshot: RuntimeSnapshot
    panes_by_id: dict[str, LivePaneRecord]
    panes_by_thread: dict[str, tuple[LivePaneRecord, ...]]
    watchers_by_pane: dict[str, tuple[LiveWatcherRecord, ...]]
    watchers_by_thread: dict[str, tuple[LiveWatcherRecord, ...]]
    sessions_by_thread: dict[str, SavedSessionRecord]
    sessions_by_name: dict[str, tuple[SavedSessionRecord, ...]]


@dataclass(frozen=True)
class RuntimeContext:
    load_sessions: Callable[[], list[dict[str, str]]]
    watcher_rows: Callable[[str], list[dict[str, str]]]
    run_tmux: Callable[..., str | None]
    load_process_snapshot: Callable[[], ProcessSnapshot]
    pane_thread_probe: Callable[[str, str, ProcessSnapshot | None], tuple[str, str, str, bool]]
    is_pane_id: Callable[[str], bool]
    is_thread_id: Callable[[str], bool]
    short_thread_id: Callable[[str], str]
    normalize_tmux_socket: Callable[[str | None], str]


def load_session_records(*, context: RuntimeContext) -> list[SavedSessionRecord]:
    records: list[SavedSessionRecord] = []
    for session in context.load_sessions():
        records.append(
            SavedSessionRecord(
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


def load_live_watcher_records(*, context: RuntimeContext) -> list[LiveWatcherRecord]:
    records: list[LiveWatcherRecord] = []
    for row in context.watcher_rows(""):
        thread_id = row.get("thread", "")
        records.append(
            LiveWatcherRecord(
                pid=row.get("pid", ""),
                pane=row.get("pane", ""),
                thread_id=thread_id.lower() if context.is_thread_id(thread_id) else thread_id,
                state_file=row.get("state", ""),
                watch_log=row.get("watch", ""),
                cwd=row.get("cwd", ""),
                message_inline=row.get("msg_inline", ""),
                tmux_socket=row.get("tmux_socket", ""),
                paused=row.get("paused", "") == "1",
                runtime_status=row.get("runtime_status", ""),
                runtime_detail=row.get("runtime_detail", ""),
                generation=int(row.get("generation", "0") or 0),
                worker_label=row.get("worker_label", ""),
            )
        )
    return records


def list_live_pane_records(*, context: RuntimeContext) -> list[LivePaneRecord]:
    listing = context.run_tmux(
        "list-panes",
        "-a",
        "-F",
        "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_pid}",
    )
    records: list[LivePaneRecord] = []
    if not listing:
        return records

    process_snapshot = context.load_process_snapshot()
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        session_name, window_index, window_name, pane_id, shell_pid = parts[:5]
        if not context.is_pane_id(pane_id):
            continue

        detected_thread, detected_thread_source, detected_thread_detail, has_codex_process = (
            context.pane_thread_probe(pane_id, shell_pid, process_snapshot)
        )
        if not context.is_thread_id(detected_thread):
            detected_thread = ""
            if not detected_thread_source:
                detected_thread_detail = ""

        records.append(
            LivePaneRecord(
                pane_id=pane_id,
                window_label=f"{session_name}:{window_index}:{window_name}",
                window_name=window_name,
                shell_pid=shell_pid,
                detected_thread=detected_thread,
                detected_thread_source=detected_thread_source,
                detected_thread_detail=detected_thread_detail,
                has_codex_process=has_codex_process,
            )
        )

    return records


def build_runtime_snapshot(*, context: RuntimeContext) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        panes=tuple(list_live_pane_records(context=context)),
        watchers=tuple(load_live_watcher_records(context=context)),
        sessions=tuple(load_session_records(context=context)),
    )


def _freeze_multimap(mapping: dict[str, list[object]]) -> dict[str, tuple[object, ...]]:
    return {key: tuple(values) for key, values in mapping.items()}


def build_runtime_model(
    snapshot: RuntimeSnapshot | None = None,
    *,
    context: RuntimeContext | None = None,
) -> RuntimeModel:
    if snapshot is None:
        if context is None:
            raise ValueError("context is required when snapshot is omitted")
        snapshot = build_runtime_snapshot(context=context)

    panes_by_id: dict[str, LivePaneRecord] = {}
    panes_by_thread: dict[str, list[LivePaneRecord]] = defaultdict(list)
    watchers_by_pane: dict[str, list[LiveWatcherRecord]] = defaultdict(list)
    watchers_by_thread: dict[str, list[LiveWatcherRecord]] = defaultdict(list)
    sessions_by_thread: dict[str, SavedSessionRecord] = {}
    sessions_by_name: dict[str, list[SavedSessionRecord]] = defaultdict(list)

    for pane in snapshot.panes:
        if pane.pane_id:
            panes_by_id[pane.pane_id] = pane
        if context and context.is_thread_id(pane.detected_thread):
            panes_by_thread[pane.detected_thread].append(pane)
        elif not context and pane.detected_thread:
            panes_by_thread[pane.detected_thread].append(pane)

    for watcher in snapshot.watchers:
        if watcher.pane:
            watchers_by_pane[watcher.pane].append(watcher)
        if context and context.is_thread_id(watcher.thread_id):
            watchers_by_thread[watcher.thread_id].append(watcher)
        elif not context and watcher.thread_id:
            watchers_by_thread[watcher.thread_id].append(watcher)

    for session in snapshot.sessions:
        if context and context.is_thread_id(session.thread_id):
            sessions_by_thread[session.thread_id] = session
        elif not context and session.thread_id:
            sessions_by_thread[session.thread_id] = session
        if session.name:
            sessions_by_name[session.name].append(session)

    return RuntimeModel(
        snapshot=snapshot,
        panes_by_id=panes_by_id,
        panes_by_thread=_freeze_multimap(panes_by_thread),  # type: ignore[arg-type]
        watchers_by_pane=_freeze_multimap(watchers_by_pane),  # type: ignore[arg-type]
        watchers_by_thread=_freeze_multimap(watchers_by_thread),  # type: ignore[arg-type]
        sessions_by_thread=sessions_by_thread,
        sessions_by_name=_freeze_multimap(sessions_by_name),  # type: ignore[arg-type]
    )


def runtime_watchers_for_pane(model: RuntimeModel, pane: str) -> tuple[LiveWatcherRecord, ...]:
    return model.watchers_by_pane.get(pane, ())


def runtime_watchers_for_thread(model: RuntimeModel, thread_id: str) -> tuple[LiveWatcherRecord, ...]:
    return model.watchers_by_thread.get(thread_id.lower(), ())


def runtime_panes_for_thread(model: RuntimeModel, thread_id: str) -> tuple[LivePaneRecord, ...]:
    return model.panes_by_thread.get(thread_id.lower(), ())


def runtime_session_for_thread(model: RuntimeModel, thread_id: str) -> SavedSessionRecord | None:
    return model.sessions_by_thread.get(thread_id.lower())


def recoverable_named_session_for_pane(
    model: RuntimeModel,
    pane: str,
    *,
    context: RuntimeContext,
) -> SavedSessionRecord | None:
    pane_record = model.panes_by_id.get(pane)
    if not pane_record:
        return None

    window_name = pane_record.window_name.strip()
    if not window_name:
        return None

    named = model.sessions_by_name.get(window_name, ())
    if len(named) != 1:
        return None

    session = named[0]
    live_thread = pane_record.detected_thread.lower() if context.is_thread_id(pane_record.detected_thread) else ""
    if live_thread and session.thread_id == live_thread:
        return None

    if runtime_panes_for_thread(model, session.thread_id):
        return None
    for watcher in runtime_watchers_for_thread(model, session.thread_id):
        if watcher.pane and watcher.pane in model.panes_by_id:
            return None
    return session


def runtime_session_name_conflict(
    model: RuntimeModel,
    session: SavedSessionRecord | None,
    *,
    context: RuntimeContext,
) -> str:
    if not session or not session.name:
        return ""
    named = model.sessions_by_name.get(session.name, ())
    if len(named) <= 1:
        return ""
    threads = ", ".join(context.short_thread_id(item.thread_id) for item in named)
    return f"agent name '{session.name}' is shared by threads {threads}"


def strict_thread_for_pane(
    model: RuntimeModel,
    pane: str,
    *,
    context: RuntimeContext,
) -> tuple[str, str]:
    watchers = list(runtime_watchers_for_pane(model, pane))
    pane_record = model.panes_by_id.get(pane)

    if len(watchers) > 1:
        pids = ", ".join(sorted(w.pid or "?" for w in watchers))
        return "", f"pane {pane} has {len(watchers)} watcher entries ({pids})"

    if not pane_record:
        return "", f"tmux pane {pane} is missing"

    if not context.is_thread_id(pane_record.detected_thread):
        if pane_record.has_codex_process:
            return (
                "",
                pane_record.detected_thread_detail
                or f"could not determine thread_id for pane={pane} from the live Codex process",
            )
        return "", f"no live Codex agent detected in pane {pane}"

    live_thread = pane_record.detected_thread.lower()

    watcher_thread = ""
    if watchers:
        if not context.is_thread_id(watchers[0].thread_id):
            runtime_id = watchers[0].pid or "?"
            return "", f"pane {pane} watcher id={runtime_id} has an invalid thread id"
        watcher_thread = watchers[0].thread_id.lower()

    if watcher_thread and watcher_thread != live_thread:
        return (
            "",
            f"pane {pane} live Codex thread {context.short_thread_id(live_thread)} "
            f"does not match watcher thread {context.short_thread_id(watcher_thread)}",
        )

    other_watchers = [
        row for row in runtime_watchers_for_thread(model, live_thread) if row.pane != pane
    ]
    if other_watchers:
        panes = ", ".join(sorted({row.pane for row in other_watchers if row.pane}))
        return "", f"thread {context.short_thread_id(live_thread)} is already watched on panes {panes}"

    other_panes = [row for row in runtime_panes_for_thread(model, live_thread) if row.pane_id != pane]
    if other_panes:
        panes = ", ".join(sorted({row.pane_id for row in other_panes if row.pane_id}))
        return "", f"thread {context.short_thread_id(live_thread)} is active in panes {panes}"

    return live_thread, ""


def strict_session_for_thread(
    model: RuntimeModel,
    thread_id: str,
    *,
    context: RuntimeContext,
) -> tuple[SavedSessionRecord | None, str]:
    session = runtime_session_for_thread(model, thread_id)
    if not session:
        return None, f"no saved session for thread {context.short_thread_id(thread_id)}"
    conflict = runtime_session_name_conflict(model, session, context=context)
    if conflict:
        return None, conflict
    return session, ""


def watcher_anomalies(
    model: RuntimeModel,
    watcher: LiveWatcherRecord,
    *,
    context: RuntimeContext,
) -> list[str]:
    pane = model.panes_by_id.get(watcher.pane)
    session = runtime_session_for_thread(model, watcher.thread_id)
    anomalies: list[str] = []

    if watcher.pane and len(runtime_watchers_for_pane(model, watcher.pane)) != 1:
        watcher_count = len(runtime_watchers_for_pane(model, watcher.pane))
        anomalies.append(f"{watcher_count} watchers on pane {watcher.pane}")
    if not context.is_thread_id(watcher.thread_id):
        anomalies.append("invalid watcher thread id")
    elif len(runtime_watchers_for_thread(model, watcher.thread_id)) != 1:
        panes = sorted(
            {row.pane for row in runtime_watchers_for_thread(model, watcher.thread_id) if row.pane}
        )
        anomalies.append(f"thread also watched on {', '.join(panes)}")
    if not pane:
        anomalies.append(f"tmux pane {watcher.pane or '?'} is missing")
    elif not pane.detected_thread:
        if pane.has_codex_process:
            anomalies.append(
                pane.detected_thread_detail
                or f"could not determine thread_id for pane={watcher.pane or '?'} from the live Codex process"
            )
        else:
            anomalies.append("no live Codex agent detected in pane")
    elif pane.detected_thread != watcher.thread_id:
        anomalies.append(f"pane agent thread is {context.short_thread_id(pane.detected_thread)}")
    if not session:
        anomalies.append("no saved session")
    else:
        conflict = runtime_session_name_conflict(model, session, context=context)
        if conflict:
            anomalies.append(conflict)
        if pane and session.name and pane.window_name and session.name != pane.window_name:
            anomalies.append(f"session name '{session.name}' != window '{pane.window_name}'")

    return anomalies


def unwatched_pane_anomalies(
    model: RuntimeModel,
    pane: LivePaneRecord,
    *,
    context: RuntimeContext,
) -> list[str]:
    anomalies = ["no runtime for managed live Codex pane"]
    if len(runtime_panes_for_thread(model, pane.detected_thread)) != 1:
        panes = sorted(
            {row.pane_id for row in runtime_panes_for_thread(model, pane.detected_thread) if row.pane_id}
        )
        anomalies.append(f"thread also active in panes {', '.join(panes)}")
    session = runtime_session_for_thread(model, pane.detected_thread)
    if not session:
        anomalies.append("no saved session")
    else:
        conflict = runtime_session_name_conflict(model, session, context=context)
        if conflict:
            anomalies.append(conflict)
    return anomalies


def unresolved_live_pane_anomalies(pane: LivePaneRecord) -> list[str]:
    detail = (
        pane.detected_thread_detail
        or f"could not determine thread_id for pane={pane.pane_id} from the live Codex process"
    )
    return [detail]


def build_status_entries_from_snapshot(
    snapshot: RuntimeSnapshot,
    *,
    context: RuntimeContext,
) -> list[tuple[dict[str, str], str, str]]:
    model = build_runtime_model(snapshot, context=context)

    entries: list[tuple[dict[str, str], str, str]] = []
    live_pane_threads = set(model.panes_by_thread)
    claimed_session_threads: set[str] = set()

    for watcher in snapshot.watchers:
        pane = model.panes_by_id.get(watcher.pane)
        session = runtime_session_for_thread(model, watcher.thread_id)
        if session:
            claimed_session_threads.add(session.thread_id)
        anomalies = watcher_anomalies(model, watcher, context=context)

        window_label = pane.window_label if pane else (session.name or "-")
        entries.append(
            (
                {
                    "entity_kind": "watcher",
                    "runtime_id": watcher.pid,
                    "pid": watcher.pid,
                    "cwd": watcher.cwd,
                    "pane": watcher.pane,
                    "thread": watcher.thread_id,
                    "name": session.name if session else "",
                    "session_name": session.name if session else "",
                    "state": session.state_file if session else watcher.state_file,
                    "watch": watcher.watch_log,
                    "msg_inline": session.message if session and session.message else watcher.message_inline,
                    "paused": "1" if watcher.paused else "",
                    "runtime_status": watcher.runtime_status,
                    "runtime_detail": watcher.runtime_detail,
                    "generation": str(watcher.generation),
                    "worker_label": watcher.worker_label,
                    "anomalies": tuple(anomalies),
                    "anomaly_detail": "; ".join(anomalies),
                },
                watcher.pane,
                window_label,
            )
        )

    watched_panes = {watcher.pane for watcher in snapshot.watchers if watcher.pane}
    for pane in snapshot.panes:
        if pane.pane_id in watched_panes:
            continue
        if not context.is_thread_id(pane.detected_thread):
            continue
        session = runtime_session_for_thread(model, pane.detected_thread)
        if not session:
            continue
        claimed_session_threads.add(session.thread_id)
        anomalies = unwatched_pane_anomalies(model, pane, context=context)
        entries.append(
            (
                {
                    "entity_kind": "unwatched_pane",
                    "runtime_id": "",
                    "pid": "",
                    "cwd": "",
                    "pane": pane.pane_id,
                    "thread": pane.detected_thread,
                    "name": session.name if session else "",
                    "session_name": session.name if session else "",
                    "state": session.state_file if session else "",
                    "watch": "",
                    "msg_inline": session.message if session and session.message else "",
                    "anomalies": tuple(anomalies),
                    "anomaly_detail": "; ".join(anomalies),
                },
                pane.pane_id,
                pane.window_label,
            )
        )

    for session in snapshot.sessions:
        if session.thread_id in claimed_session_threads:
            continue
        if session.thread_id in live_pane_threads:
            continue
        entries.append(
            (
                {
                    "entity_kind": "dead_session",
                    "runtime_id": "",
                    "pid": "",
                    "cwd": "",
                    "pane": "",
                    "thread": session.thread_id,
                    "name": session.name,
                    "session_name": session.name,
                    "state": session.state_file,
                    "watch": "",
                    "msg_inline": session.message,
                    "anomalies": tuple(),
                    "anomaly_detail": "",
                },
                "",
                session.name or "-",
            )
        )

    return entries
