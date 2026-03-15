from __future__ import annotations

from dataclasses import dataclass
import re
import sys
from typing import Any
from typing import Callable


@dataclass(frozen=True)
class TargetContext:
    is_pane_id: Callable[[str], bool]
    is_thread_id: Callable[[str], bool]
    build_runtime_model: Callable[[], Any]
    strict_thread_for_pane: Callable[[Any, str], tuple[str, str]]
    runtime_session_for_thread: Callable[[Any, str], Any | None]
    runtime_panes_for_thread: Callable[[Any, str], tuple[Any, ...]]
    saved_session_panes_for_target: Callable[[str, Any | None], list[str]] | None
    resolve_pane_from_window_name: Callable[[str], str | None]
    resolve_pane_from_window_name_quiet: Callable[[str], str | None]
    resolve_pane_from_window_target: Callable[[str], str | None]
    tmux_socket_recovery_hint: Callable[[], str]
    print_tmux_unavailable_hint: Callable[[str], None]
    current_tmux_pane: Callable[[], str]


def resolve_thread_id(
    pane: str,
    requested: str = "",
    *,
    model: Any | None = None,
    context: TargetContext,
) -> str:
    if requested and requested != "auto":
        if context.is_thread_id(requested):
            return requested.lower()
        print(f"error: invalid thread_id '{requested}'", file=sys.stderr)
        raise SystemExit(1)

    if model is None:
        model = context.build_runtime_model()
    thread_id, thread_error = context.strict_thread_for_pane(model, pane)
    if thread_id and context.is_thread_id(thread_id):
        return thread_id

    error_text = thread_error or f"could not determine thread_id for pane={pane}; start requires a concrete thread id"
    print(f"error: {error_text}", file=sys.stderr)
    raise SystemExit(1)


def saved_session_panes_for_target(
    target: str,
    *,
    model: Any | None = None,
    context: TargetContext,
) -> list[str]:
    if model is None:
        model = context.build_runtime_model()

    panes: list[str] = []
    seen: set[str] = set()
    if context.is_thread_id(target):
        sessions = tuple([session] if (session := context.runtime_session_for_thread(model, target)) else [])
    else:
        sessions = model.sessions_by_name.get(target, ())

    for session in sessions:
        thread_id = session.thread_id
        if context.is_thread_id(target) and thread_id != target.lower():
            continue
        for pane in context.runtime_panes_for_thread(model, thread_id):
            if not context.is_pane_id(pane.pane_id) or pane.pane_id in seen:
                continue
            seen.add(pane.pane_id)
            panes.append(pane.pane_id)
    return panes


def resolve_restart_pane_target(
    target: str,
    *,
    model: Any | None = None,
    context: TargetContext,
) -> str:
    if context.is_pane_id(target):
        return target

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = context.resolve_pane_from_window_target(target)
        if pane and context.is_pane_id(pane):
            return pane
        print(f"error: could not resolve window target '{target}'", file=sys.stderr)
        raise SystemExit(1)

    saved_panes = (
        context.saved_session_panes_for_target(target, model)
        if context.saved_session_panes_for_target is not None
        else saved_session_panes_for_target(target, model=model, context=context)
    )
    if len(saved_panes) == 1:
        return saved_panes[0]
    if len(saved_panes) > 1:
        joined = ", ".join(saved_panes)
        print(
            f"error: saved session target '{target}' is ambiguous across live panes: {joined}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    pane = context.resolve_pane_from_window_name_quiet(target)
    if pane and context.is_pane_id(pane):
        return pane

    if context.tmux_socket_recovery_hint():
        context.print_tmux_unavailable_hint(target)
        raise SystemExit(1)

    print(f"error: no managed session, window, or live Codex pane found for '{target}'", file=sys.stderr)
    print(
        "hint: use a pane id ('%6'), window name, window index ('2'), or a saved session name with a live Codex pane",
        file=sys.stderr,
    )
    raise SystemExit(1)


def doctor_resolve_target(
    target: str,
    *,
    model: Any | None = None,
    context: TargetContext,
) -> tuple[str, str]:
    if model is None:
        model = context.build_runtime_model()
    if not target:
        pane = context.current_tmux_pane()
        if pane:
            return pane, ""
        return "", ""

    if context.is_pane_id(target):
        return target, ""

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = context.resolve_pane_from_window_target(target)
        if pane and context.is_pane_id(pane):
            return pane, ""
        return "", f"could not resolve window target '{target}'"

    saved_panes = (
        context.saved_session_panes_for_target(target, model)
        if context.saved_session_panes_for_target is not None
        else saved_session_panes_for_target(target, model=model, context=context)
    )
    if len(saved_panes) == 1:
        return saved_panes[0], ""
    if len(saved_panes) > 1:
        joined = ", ".join(saved_panes)
        return "", f"saved session target '{target}' is ambiguous across live panes: {joined}"

    pane = context.resolve_pane_from_window_name(target)
    if pane and context.is_pane_id(pane):
        return pane, ""

    recovery = context.tmux_socket_recovery_hint()
    if recovery:
        return "", recovery
    return "", f"could not resolve target '{target}'"
