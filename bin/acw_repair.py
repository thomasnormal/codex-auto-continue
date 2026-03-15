from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Callable


@dataclass(frozen=True)
class RepairStep:
    kind: str
    target: str
    summary: str
    auto_apply: bool
    thread_id: str = ""
    message: str = ""


@dataclass(frozen=True)
class RepairContext:
    is_pane_id: Callable[[str], bool]
    is_thread_id: Callable[[str], bool]
    short_thread_id: Callable[[str], str]
    message_text: Callable[[str], str]
    runtime_watchers_for_pane: Callable[[Any, str], tuple[Any, ...]]
    runtime_watchers_for_thread: Callable[[Any, str], tuple[Any, ...]]
    runtime_panes_for_thread: Callable[[Any, str], tuple[Any, ...]]
    runtime_session_for_thread: Callable[[Any, str], Any | None]
    runtime_session_name_conflict: Callable[[Any, Any | None], str]


def pane_numeric_sort_key(pane: str, *, context: RepairContext) -> tuple[int, str]:
    if context.is_pane_id(pane):
        try:
            return int(pane[1:]), pane
        except ValueError:
            pass
    return 10**9, pane


def repair_message_for_pane(
    model: Any,
    pane: str,
    live_thread: str,
    *,
    context: RepairContext,
) -> str:
    session = context.runtime_session_for_thread(model, live_thread)
    if session and session.message:
        return session.message
    for watcher in context.runtime_watchers_for_pane(model, pane):
        if watcher.thread_id != live_thread:
            continue
        if watcher.message_inline:
            return context.message_text(watcher.message_inline)
    return ""


def repair_steps_for_pane(
    model: Any,
    pane: str,
    *,
    context: RepairContext,
) -> list[RepairStep]:
    pane_record = model.panes_by_id.get(pane)
    watchers = list(context.runtime_watchers_for_pane(model, pane))

    if not pane_record:
        if watchers:
            return [
                RepairStep(
                    kind="stop_watchers_keep_session",
                    target=pane,
                    summary=f"pane {pane} is missing from tmux; stop its runtime entries but keep any saved session",
                    auto_apply=True,
                )
            ]
        return []

    if not pane_record.has_codex_process:
        if watchers:
            return [
                RepairStep(
                    kind="stop_watchers_keep_session",
                    target=pane,
                    summary=f"pane {pane} has no live Codex agent; stop its runtime entries but keep any saved session",
                    auto_apply=True,
                )
            ]
        return [
            RepairStep(
                kind="manual",
                target=pane,
                summary=f"pane {pane} has no live Codex agent; restart Codex there manually before repairing runtime state",
                auto_apply=False,
            )
        ]

    if not context.is_thread_id(pane_record.detected_thread):
        detail = (
            pane_record.detected_thread_detail
            or f"could not determine thread_id for pane={pane} from the live Codex process"
        )
        return [
            RepairStep(
                kind="manual",
                target=pane,
                summary=f"pane {pane} is unresolved: {detail}",
                auto_apply=False,
            )
        ]

    live_thread = pane_record.detected_thread.lower()
    related_panes = sorted(
        {
            row.pane_id
            for row in context.runtime_panes_for_thread(model, live_thread)
            if row.pane_id
        },
        key=lambda value: pane_numeric_sort_key(value, context=context),
    )
    if len(related_panes) > 1:
        return [
            RepairStep(
                kind="manual",
                target=pane,
                summary=(
                    f"thread {context.short_thread_id(live_thread)} is active across panes {', '.join(related_panes)}; "
                    "choose one pane to keep and repair the others manually"
                ),
                auto_apply=False,
                thread_id=live_thread,
            )
        ]

    watched_panes = sorted(
        {
            row.pane
            for row in context.runtime_watchers_for_thread(model, live_thread)
            if row.pane
        },
        key=lambda value: pane_numeric_sort_key(value, context=context),
    )
    if len(watched_panes) > 1:
        return [
            RepairStep(
                kind="manual",
                target=pane,
                summary=(
                    f"thread {context.short_thread_id(live_thread)} is watched across panes {', '.join(watched_panes)}; "
                    "choose one pane to keep and repair the others manually"
                ),
                auto_apply=False,
                thread_id=live_thread,
            )
        ]

    session = context.runtime_session_for_thread(model, live_thread)
    if session:
        conflict = context.runtime_session_name_conflict(model, session)
        if conflict:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=f"pane {pane} is blocked: {conflict}",
                    auto_apply=False,
                    thread_id=live_thread,
                )
            ]

    message = repair_message_for_pane(model, pane, live_thread, context=context)
    if not watchers:
        if not session or not message:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=(
                        f"pane {pane} has live thread {context.short_thread_id(live_thread)} but no runtime and no saved continue message; "
                        "start it manually with an explicit --message"
                    ),
                    auto_apply=False,
                    thread_id=live_thread,
                )
            ]
        return [
            RepairStep(
                kind="start_watcher",
                target=pane,
                summary=f"start one managed runtime on pane {pane} for thread {context.short_thread_id(live_thread)}",
                auto_apply=True,
                thread_id=live_thread,
                message=message,
            )
        ]

    if len(watchers) > 1:
        if not message:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=(
                        f"pane {pane} has duplicate runtime entries but no saved continue message for thread "
                        f"{context.short_thread_id(live_thread)}; repair it manually"
                    ),
                    auto_apply=False,
                    thread_id=live_thread,
                )
            ]
        return [
            RepairStep(
                kind="restart_watcher",
                target=pane,
                summary=f"replace duplicate runtime entries on pane {pane} with one managed runtime for thread {context.short_thread_id(live_thread)}",
                auto_apply=True,
                thread_id=live_thread,
                message=message,
            )
        ]

    watcher = watchers[0]
    if not context.is_thread_id(watcher.thread_id):
        if not message:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=f"pane {pane} has a runtime with an invalid thread id and no saved continue message; repair it manually",
                    auto_apply=False,
                )
            ]
        return [
            RepairStep(
                kind="restart_watcher",
                target=pane,
                summary=f"restart the runtime on pane {pane} with the live thread {context.short_thread_id(live_thread)}",
                auto_apply=True,
                thread_id=live_thread,
                message=message,
            )
        ]

    if watcher.thread_id != live_thread:
        if not message:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=(
                        f"pane {pane} runtime is bound to {context.short_thread_id(watcher.thread_id)}, but the live pane is "
                        f"{context.short_thread_id(live_thread)} and no saved continue message is available; repair it manually"
                    ),
                    auto_apply=False,
                    thread_id=live_thread,
                )
            ]
        return [
            RepairStep(
                kind="restart_watcher",
                target=pane,
                summary=f"rebind the runtime on pane {pane} to the live thread {context.short_thread_id(live_thread)}",
                auto_apply=True,
                thread_id=live_thread,
                message=message,
            )
        ]

    if not session:
        if not message:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=f"pane {pane} has no saved session and no recoverable continue message; repair it manually",
                    auto_apply=False,
                    thread_id=live_thread,
                )
            ]
        return [
            RepairStep(
                kind="recreate_session",
                target=pane,
                summary=f"recreate the missing saved session for pane {pane} and thread {context.short_thread_id(live_thread)}",
                auto_apply=True,
                thread_id=live_thread,
                message=message,
            )
        ]

    if pane_record.window_name and session.name and pane_record.window_name != session.name:
        return [
            RepairStep(
                kind="sync_session_name",
                target=pane,
                summary=f"sync the saved session name for pane {pane} to the current window name '{pane_record.window_name}'",
                auto_apply=True,
                thread_id=live_thread,
            )
        ]

    return []


def build_repair_plan(
    model: Any,
    target_panes: tuple[str, ...] | None = None,
    *,
    context: RepairContext,
) -> list[RepairStep]:
    if target_panes is None:
        candidate_panes_set = set(model.watchers_by_pane)
        for pane in model.snapshot.panes:
            if not pane.pane_id or not context.is_thread_id(pane.detected_thread):
                continue
            if context.runtime_session_for_thread(model, pane.detected_thread):
                candidate_panes_set.add(pane.pane_id)
        candidate_panes = sorted(
            candidate_panes_set,
            key=lambda value: pane_numeric_sort_key(value, context=context),
        )
    else:
        candidate_panes = list(target_panes)

    steps: list[RepairStep] = []
    seen: set[tuple[object, ...]] = set()
    for pane in candidate_panes:
        for step in repair_steps_for_pane(model, pane, context=context):
            key = (
                (step.kind, step.summary)
                if not step.auto_apply
                else (step.kind, step.target, step.thread_id)
            )
            if key in seen:
                continue
            seen.add(key)
            steps.append(step)

    if target_panes is None:
        live_threads = set(model.panes_by_thread) | set(model.watchers_by_thread)
        dead_sessions = [
            session
            for session in model.snapshot.sessions
            if session.thread_id not in live_threads
        ]
        if dead_sessions:
            steps.append(
                RepairStep(
                    kind="cleanup_dead_sessions",
                    target="",
                    summary=f"run acw cleanup to remove {len(dead_sessions)} dead saved session(s)",
                    auto_apply=False,
                )
            )

    return steps
