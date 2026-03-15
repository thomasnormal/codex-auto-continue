from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any
from typing import Callable


@dataclass
class DoctorReport:
    checks: list[tuple[str, str]]
    result: str
    exit_code: int
    recommendations: list[str]


@dataclass(frozen=True)
class ObserveContext:
    state_dir: str
    preferred_tmux_socket: Callable[[], str]
    is_thread_id: Callable[[str], bool]
    build_runtime_snapshot: Callable[[], Any]
    build_runtime_model: Callable[[Any | None], Any]
    build_status_entries_from_snapshot: Callable[[Any], list[tuple[dict[str, str], str, str]]]
    tmux_socket_recovery_hint: Callable[[], str]
    resolve_live_pane_target: Callable[[str], str]
    status_sort_key: Callable[[tuple[dict[str, str], str, str]], Any]
    status_details: Callable[[list[tuple[dict[str, str], str, str]], Any | None], None]
    status_table: Callable[[list[tuple[dict[str, str], str, str]]], None]
    print_status_recommendations: Callable[[list[tuple[dict[str, str], str, str]]], None]
    state_dir_is_writable: Callable[[], tuple[bool, str]]
    codex_auth_state_available: Callable[[], tuple[bool, str]]
    run_tmux: Callable[..., str | None]
    doctor_resolve_target: Callable[[str, Any | None], tuple[str, str]]
    strict_thread_for_pane: Callable[[Any, str], tuple[str, str]]
    thread_provenance_text: Callable[[str], str]
    runtime_watchers_for_pane: Callable[[Any, str], tuple[Any, ...]]
    watcher_anomalies: Callable[[Any, Any], list[str]]
    read_state_json: Callable[[str], dict[str, str]]
    compute_state: Callable[[dict[str, str], dict[str, str]], str]
    repair_steps_for_pane: Callable[[Any, str], list[Any]]
    current_tmux_pane: Callable[[], str]
    read_session_state: Callable[[str], dict[str, Any]]
    print_doctor_plain: Callable[[DoctorReport], None]
    print_doctor_rich: Callable[[DoctorReport], None]
    short_thread_id: Callable[[str], str]


def cmd_status(context: ObserveContext, argv: list[str]) -> None:
    pane_arg = ""
    details = False
    for arg in argv:
        if arg == "--details":
            details = True
        else:
            pane_arg = arg

    snapshot = context.build_runtime_snapshot()
    model = context.build_runtime_model(snapshot)
    recovery = ""
    if not snapshot.panes and not snapshot.watchers:
        recovery = context.tmux_socket_recovery_hint()
        if recovery:
            print(
                "warning: tmux metadata is unavailable; status is falling back to saved session state",
                file=sys.stderr,
            )
            print(recovery, file=sys.stderr)

    resolved = context.build_status_entries_from_snapshot(snapshot)

    if pane_arg:
        target = pane_arg
        pane = context.resolve_live_pane_target(target)
        if pane and pane != target:
            print(f"resolved: target={target} pane={pane}")
        target_thread = target.lower() if context.is_thread_id(target) else ""
        resolved = [
            (row, live_pane, window_label)
            for row, live_pane, window_label in resolved
            if (pane and live_pane == pane)
            or (target_thread and row.get("thread", "").lower() == target_thread)
            or row.get("session_name", "") == target
        ]

    detail_tag = " (details)" if details else ""
    print(f"Sessions{detail_tag}: {len(resolved)}")

    if not resolved:
        print("(none)")
        return

    resolved.sort(key=context.status_sort_key)

    if details:
        context.status_details(resolved, model=model)
    else:
        context.status_table(resolved)
        context.print_status_recommendations(resolved)


def doctor_recommendation_target(
    context: ObserveContext,
    target: str,
    pane: str,
    rows: list[dict[str, str]],
) -> str:
    if target:
        return target
    if rows:
        name = context.read_session_state(rows[0].get("thread", "")).get("name", "").strip()
        if name:
            return name
    return pane


def doctor_report(context: ObserveContext, target: str = "") -> DoctorReport:
    checks: list[tuple[str, str]] = []
    recommendations: list[str] = []
    errors = 0
    warnings = 0
    model = context.build_runtime_model(None)

    ok, detail = context.state_dir_is_writable()
    checks.append(("ok" if ok else "error", f"state dir writable: {detail}"))
    errors += 0 if ok else 1

    ok, detail = context.codex_auth_state_available()
    checks.append(("ok" if ok else "error", f"Codex auth state present: {detail}"))
    errors += 0 if ok else 1

    tmux_ok = context.run_tmux("list-sessions") is not None
    checks.append(("ok" if tmux_ok else "error", "tmux server reachable"))
    errors += 0 if tmux_ok else 1
    recovery = context.tmux_socket_recovery_hint()
    if recovery:
        checks.append(("info", recovery))

    pane, pane_error = context.doctor_resolve_target(target, model)
    if pane_error:
        checks.append(("error", pane_error))
        errors += 1
        return DoctorReport(checks=checks, result="error", exit_code=1, recommendations=recommendations)

    if not pane:
        checks.append(("info", "pane checks skipped: no target and TMUX_PANE not set"))
        result = "error" if errors else "ok"
        return DoctorReport(checks=checks, result=result, exit_code=1 if errors else 0, recommendations=recommendations)

    checks.append(("ok", f"pane resolved: {pane}"))
    tid, thread_error = context.strict_thread_for_pane(model, pane)
    if tid and context.is_thread_id(tid):
        checks.append(("ok", f"Codex thread detected: {tid}"))
        pane_record = model.panes_by_id.get(pane)
        provenance = context.thread_provenance_text(pane_record.detected_thread_source) if pane_record else ""
        if provenance:
            checks.append(("info", f"thread provenance: {provenance}"))
    else:
        checks.append(("error", thread_error or f"could not detect a Codex thread for pane {pane}"))
        errors += 1

    rows = [watcher for watcher in context.runtime_watchers_for_pane(model, pane)]
    ps_rows = [
        {
            "pid": watcher.pid,
            "pane": watcher.pane,
            "thread": watcher.thread_id,
            "state": watcher.state_file,
            "watch": watcher.watch_log,
            "msg_inline": watcher.message_inline,
            "paused": "1" if watcher.paused else "",
            "runtime_status": watcher.runtime_status,
            "runtime_detail": watcher.runtime_detail,
        }
        for watcher in rows
    ]
    command_target = doctor_recommendation_target(context, target, pane, ps_rows)
    if len(rows) > 1:
        runtime_ids = ", ".join(sorted(watcher.pid or "?" for watcher in rows))
        checks.append(("error", f"multiple managed runtimes running on pane {pane}: {runtime_ids}"))
        errors += 1
        recommendations.append(f"acw stop {pane}")
    elif rows:
        watcher = rows[0]
        checks.append(("ok", f"runtime running: pane={pane} id={watcher.pid}"))
        anomalies = context.watcher_anomalies(model, watcher)
        if anomalies:
            detail = "; ".join(anomalies)
            checks.append(("error", f"runtime identity mismatch: {detail}"))
            errors += 1
            recommendations.append(f"acw stop {pane}")
        else:
            row = {
                "pid": watcher.pid,
                "pane": watcher.pane,
                "thread": watcher.thread_id,
                "state": watcher.state_file,
                "watch": watcher.watch_log,
                "msg_inline": watcher.message_inline,
                "paused": "1" if watcher.paused else "",
                "runtime_status": watcher.runtime_status,
                "runtime_detail": watcher.runtime_detail,
            }
            sj = context.read_state_json(watcher.state_file)
            watcher_state = context.compute_state(row, sj)
            health_detail = sj.get("health_detail", "").strip()
            if watcher_state == "running":
                checks.append(("ok", "runtime health: running"))
            elif watcher_state == "paused":
                msg = "runtime paused"
                if health_detail:
                    msg += f" - {health_detail}"
                checks.append(("warn", msg))
                warnings += 1
                recommendations.append(f"acw resume {command_target}")
            else:
                msg = f"runtime health: {watcher_state}"
                if health_detail:
                    msg += f" - {health_detail}"
                level = "warn" if watcher_state == "warn" else "error"
                checks.append((level, msg))
                if level == "warn":
                    warnings += 1
                else:
                    errors += 1
                recommendations.append(f"acw restart {command_target}")
    else:
        checks.append(("info", f"no managed runtime running for pane {pane}"))
        if tid and context.is_thread_id(tid):
            recommendations.append(f"acw start {command_target} {tid}")

    repair_steps = context.repair_steps_for_pane(model, pane) if pane else []
    if any(step.auto_apply for step in repair_steps):
        recommendations.insert(0, f"acw repair {command_target}")
    elif repair_steps:
        recommendations.insert(0, f"acw repair {command_target} --dry-run")

    result = "error" if errors else ("warn" if warnings else "ok")
    exit_code = 1 if errors else 0
    return DoctorReport(
        checks=checks,
        result=result,
        exit_code=exit_code,
        recommendations=list(dict.fromkeys(recommendations)),
    )


def cmd_doctor(context: ObserveContext, argv: list[str]) -> None:
    target = ""
    if argv:
        if len(argv) > 1 or argv[0].startswith("--"):
            print("error: doctor accepts at most one target", file=sys.stderr)
            sys.exit(2)
        target = argv[0]

    report = doctor_report(context, target)
    try:
        context.print_doctor_rich(report)
    except ModuleNotFoundError:
        context.print_doctor_plain(report)
    if report.exit_code:
        sys.exit(report.exit_code)
