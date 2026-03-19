from __future__ import annotations

import io
import shlex
import sys
from dataclasses import dataclass
from typing import Any
from typing import Callable


@dataclass(frozen=True)
class CommandContext:
    is_pane_id: Callable[[str], bool]
    is_thread_id: Callable[[str], bool]
    print_progress: Callable[[str], None]
    build_runtime_model: Callable[[], Any]
    watcher_rows: Callable[[str], list[dict[str, str]]]
    runtime_watchers_for_pane: Callable[[Any, str], tuple[Any, ...]]
    runtime_panes_for_thread: Callable[[Any, str], tuple[Any, ...]]
    runtime_session_for_thread: Callable[[Any, str], Any | None]
    recoverable_named_session_for_pane: Callable[[Any, str], Any | None]
    watcher_anomalies: Callable[[Any, Any], list[str]]
    other_watcher_for_thread: Callable[[str, str], dict[str, str] | None]
    resolve_start_pane_target: Callable[[str], str]
    resolve_pane_target: Callable[[str], str]
    resolve_pane_from_window_name: Callable[[str], str | None]
    resolve_pane_from_window_target: Callable[[str], str | None]
    resolve_thread_id: Callable[..., str]
    resolve_restart_pane_target: Callable[[str, Any | None], str]
    ensure_tmux_window_rename_hook: Callable[[], None]
    edit_message_interactive: Callable[[str], str | None]
    message_text: Callable[[str], str]
    read_session_state: Callable[[str], dict[str, Any]]
    write_session_state: Callable[[str, dict[str, Any]], None]
    remove_session_state: Callable[[str], bool]
    daemon_rpc_upsert_session: Callable[..., dict[str, str]]
    daemon_rpc_patch_session: Callable[..., dict[str, str]]
    daemon_rpc_remove_session: Callable[..., None]
    daemon_rpc_restart_codex: Callable[..., None]
    current_session_generation: Callable[[dict[str, Any]], int]
    current_tmux_scope: Callable[[], str]
    load_session_records: Callable[[], list[Any]]
    strict_thread_for_pane: Callable[[Any, str], tuple[str, str]]
    strict_session_for_thread: Callable[[Any, str], tuple[Any | None, str]]
    short_thread_id: Callable[[str], str]
    build_repair_plan: Callable[[Any, tuple[str, ...] | None], list[Any]]
    doctor_resolve_target: Callable[[str, Any | None], tuple[str, str]]
    run_tmux: Callable[..., str | None]


def parse_thread_and_message_args(args: list[str]) -> tuple[str, str, bool]:
    thread_arg, message_value, message_explicit, _ = parse_restartable_args(args)
    return thread_arg, message_value, message_explicit


def parse_restart_args(args: list[str]) -> tuple[str, str, bool, bool]:
    return parse_restartable_args(args, allow_restart_codex=True)


def parse_restartable_args(
    args: list[str],
    allow_restart_codex: bool = False,
) -> tuple[str, str, bool, bool]:
    thread_arg = ""
    message_value = ""
    message_explicit = False
    restart_codex = False

    rest = list(args)
    if rest and not rest[0].startswith("--"):
        thread_arg = rest.pop(0)

    while rest:
        tok = rest.pop(0)
        if tok == "--message":
            if not rest:
                print("error: --message requires a value", file=sys.stderr)
                sys.exit(1)
            message_value = rest.pop(0)
            message_explicit = True
        elif allow_restart_codex and tok == "--restart-codex":
            restart_codex = True
        else:
            print(f"error: unknown option '{tok}'", file=sys.stderr)
            sys.exit(1)

    return thread_arg, message_value, message_explicit, restart_codex


def parse_repair_args(args: list[str]) -> tuple[str, bool, bool]:
    target = ""
    dry_run = False
    all_targets = False

    rest = list(args)
    if rest and not rest[0].startswith("--"):
        target = rest.pop(0)

    while rest:
        tok = rest.pop(0)
        if tok == "--dry-run":
            dry_run = True
        elif tok == "--all":
            all_targets = True
        else:
            print(f"error: unknown option '{tok}'", file=sys.stderr)
            sys.exit(2)

    if target and all_targets:
        print("error: repair accepts either a target or --all, not both", file=sys.stderr)
        sys.exit(2)

    if not target and not all_targets:
        dry_run = True

    return target, dry_run, all_targets


def system_exit_detail(exc: SystemExit, stderr_text: str = "") -> str:
    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("error:"):
            return line.removeprefix("error:").strip()
    for line in reversed(lines):
        if not line.startswith("hint:"):
            return line
    if isinstance(exc.code, str) and exc.code.strip():
        return exc.code.strip()
    code = exc.code if isinstance(exc.code, int) else 1
    return f"exit {code}"


def validate_existing_pane_watcher(
    context: CommandContext,
    pane: str,
    requested_thread: str = "",
    *,
    model: Any | None = None,
) -> tuple[str, str] | None:
    if model is None:
        model = context.build_runtime_model()

    watchers = list(context.runtime_watchers_for_pane(model, pane))
    if not watchers:
        return None

    if len(watchers) != 1:
        joined = " ".join(sorted(w.pid or "?" for w in watchers))
        return (
            f"error: multiple managed runtimes already running for pane={pane}: {joined}",
            f"hint: run 'acw repair {pane} --dry-run' to inspect the fix plan, or 'acw stop {pane}' to stop all pane runtimes first",
        )

    row = watchers[0]
    thread_id = row.thread_id
    if requested_thread and context.is_thread_id(thread_id) and thread_id.lower() != requested_thread.lower():
        return (
            f"error: existing runtime for pane={pane} is using thread {thread_id[:8]}…, "
            f"not requested thread {requested_thread[:8]}…",
            f"hint: inspect 'acw repair {pane} --dry-run' or stop it first with 'acw stop {pane}' if you want to start a different session there",
        )

    anomalies = context.watcher_anomalies(model, row)
    other = context.other_watcher_for_thread(pane, thread_id)
    if other:
        return (
            f"error: existing runtime for pane={pane} is duplicated; pane={other['pane']} "
            f"id={other.get('runtime_id') or other.get('pid', '?')} is already watching thread {thread_id[:8]}…",
            f"hint: inspect 'acw repair {pane} --dry-run' or stop the duplicate runtimes with 'acw stop {pane}' and 'acw stop {other['pane']}', then start once",
        )
    if anomalies:
        detail = "; ".join(anomalies)
        return (
            f"error: existing runtime for pane={pane} is inconsistent: {detail}",
            f"hint: stop it first with 'acw stop {pane}', then start once after the pane/session identity is clean",
        )
    return None


def cmd_start(context: CommandContext, argv: list[str]) -> None:
    rest = list(argv)
    if rest and not rest[0].startswith("--"):
        target = rest.pop(0)
    else:
        target = ""
    if not target:
        print(
            "error: pane target is required\n"
            "usage: auto_continue_watchd.py start <pane-id|window-index|session:window> "
            "[thread-id] [--message TEXT]",
            file=sys.stderr,
        )
        sys.exit(2)

    pane = context.resolve_start_pane_target(target)
    if pane != target:
        context.print_progress(f"resolved: target={target} pane={pane}")
    context.ensure_tmux_window_rename_hook()

    thread_arg, msg_value, msg_explicit = parse_thread_and_message_args(rest)

    if not msg_explicit:
        value = context.edit_message_interactive("")
        if value is None or not value.strip():
            print("start: no message provided, aborting", file=sys.stderr)
            sys.exit(1)
        msg_value = value

    model = context.build_runtime_model()
    existing_rows = [row for row in context.watcher_rows("") if row.get("pane", "") == pane]
    if existing_rows:
        validation = validate_existing_pane_watcher(context, pane, thread_arg, model=model)
        if validation:
            print(validation[0], file=sys.stderr)
            print(validation[1], file=sys.stderr)
            sys.exit(1)
        existing = existing_rows[0]
        print(f"already running: pane={pane} thread_id={existing.get('thread', '')}")
        return

    if thread_arg and thread_arg != "auto":
        context.print_progress(f"start: using explicit thread_id for pane={pane}")
    else:
        context.print_progress(f"start: resolving Codex thread for pane={pane}")
    thread_id = context.resolve_thread_id(pane, thread_arg, model=model)
    context.print_progress(f"start: thread_id={thread_id}")

    other = context.other_watcher_for_thread(pane, thread_id)
    if other:
        print(
            f"error: another runtime (id={other.get('runtime_id') or other.get('pid', '?')} pane={other['pane']}) "
            f"is already watching thread {thread_id[:8]}…",
            file=sys.stderr,
        )
        print(
            f"hint: inspect 'acw repair {pane} --dry-run' or stop it first with 'acw stop {other['pane']}' to avoid duplicates",
            file=sys.stderr,
        )
        sys.exit(1)

    other_panes = [row for row in context.runtime_panes_for_thread(model, thread_id) if row.pane_id != pane]
    if other_panes:
        panes = ", ".join(sorted({row.pane_id for row in other_panes if row.pane_id}))
        print(
            f"error: another live Codex pane is already using thread {thread_id[:8]}… on {panes}",
            file=sys.stderr,
        )
        print(
            f"hint: clean up the duplicate panes first, then start exactly one managed runtime for {thread_id[:8]}…",
            file=sys.stderr,
        )
        sys.exit(1)

    message_text = context.message_text(msg_value)
    wn = context.run_tmux("display-message", "-p", "-t", pane, "#{window_name}") or ""
    existing_session = context.read_session_state(thread_id)
    row = context.daemon_rpc_upsert_session(
        thread_id,
        pane=pane,
        name=wn.strip(),
        message=message_text,
        paused=False,
        generation=context.current_session_generation(existing_session),
    )

    if (not thread_arg or thread_arg == "auto") and thread_id != "auto":
        context.print_progress(f"resolved: pane={pane} thread_id={thread_id}")

    daemon_pid = row.get("daemon_pid", "")
    context.print_progress(f"started: pane={pane} thread_id={thread_id} daemon_pid={daemon_pid}")


def find_watchers_by_window_name(context: CommandContext, name: str) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for row in context.watcher_rows(""):
        tid = row["thread"]
        if not tid or not context.is_thread_id(tid):
            continue
        ss = context.read_session_state(tid)
        if ss.get("name", "") == name:
            matches.append(row)
    return matches


def stop_watcher_rows(context: CommandContext, rows: list[dict[str, str]]) -> None:
    forgotten_threads: set[str] = set()
    for row in rows:
        thread_id = row.get("thread", "").lower()
        if row.get("runtime_status", "") and context.is_thread_id(thread_id) and thread_id not in forgotten_threads:
            forgotten_threads.add(thread_id)
            context.daemon_rpc_remove_session(thread_id, timeout_secs=2.0)
            print(f"stopped: pane={row['pane']} thread_id={thread_id}")


def stop_pane_watchers_keep_session(context: CommandContext, pane: str) -> None:
    rows = context.watcher_rows(pane)
    if not rows:
        print(f"not running: pane={pane}")
        return

    for row in rows:
        thread_id = row.get("thread", "").lower()
        if not row.get("runtime_status", "") or not context.is_thread_id(thread_id):
            continue
        existing = context.read_session_state(thread_id)
        context.daemon_rpc_patch_session(
            thread_id,
            {
                "pane": "",
                "paused": True,
                "generation": context.current_session_generation(existing) + 1,
            },
            wait={"kind": "absent"},
            timeout_secs=2.0,
        )
        print(f"stopped: pane={pane} thread_id={thread_id}")


def stop_pane_watchers(context: CommandContext, pane: str) -> None:
    model = context.build_runtime_model()
    session_threads: set[str] = set()
    for watcher in context.runtime_watchers_for_pane(model, pane):
        if context.is_thread_id(watcher.thread_id) and context.runtime_session_for_thread(model, watcher.thread_id):
            session_threads.add(watcher.thread_id.lower())
    pane_record = model.panes_by_id.get(pane)
    if pane_record and context.is_thread_id(pane_record.detected_thread):
        session = context.runtime_session_for_thread(model, pane_record.detected_thread)
        if session:
            session_threads.add(session.thread_id.lower())
    rows = context.watcher_rows(pane)
    managed_threads = {
        row.get("thread", "").lower()
        for row in rows
        if row.get("runtime_status", "") and context.is_thread_id(row.get("thread", ""))
    }

    if not managed_threads and not session_threads:
        print(f"not running: pane={pane}")
        return

    for thread_id in sorted(managed_threads | session_threads):
        context.daemon_rpc_remove_session(thread_id, timeout_secs=2.0)
        print(f"stopped: pane={pane} thread_id={thread_id}")


def cmd_stop(context: CommandContext, argv: list[str], *, stop_pane_watchers_fn) -> None:
    target = argv[0] if argv else ""

    if target:
        if context.is_pane_id(target):
            stop_pane_watchers_fn(target)
            return

        matches = find_watchers_by_window_name(context, target)
        if matches:
            stop_watcher_rows(context, matches)
            return

        pane = context.resolve_pane_from_window_name(target)
        if not pane:
            pane = (
                context.resolve_pane_from_window_target(target)
                if context.is_pane_id(target) is False
                else None
            )
        if pane and context.is_pane_id(pane):
            stop_pane_watchers_fn(pane)
            return

        print(f"error: no managed session found for '{target}'", file=sys.stderr)
        print(
            "hint: use a pane id ('%6'), window name, or window index ('2')",
            file=sys.stderr,
        )
        sys.exit(1)

    had_any = False
    for row in context.watcher_rows(""):
        had_any = True
        stop_watcher_rows(context, [row])

    if not had_any:
        print("not running")
    forgotten = 0
    for session in context.load_session_records():
        if context.remove_session_state(session.thread_id):
            forgotten += 1
            print(f"forgot session: thread_id={session.thread_id}")
    if not had_any and forgotten == 0:
        return


def cmd_pause(context: CommandContext, argv: list[str]) -> None:
    if argv and argv[0] == "*":
        print("error: '*' is no longer supported for pause; use 'acw pause' with no target", file=sys.stderr)
        sys.exit(2)
    pane_arg = argv[0] if argv else ""

    if pane_arg:
        pane = context.resolve_pane_target(pane_arg)
        rows = context.watcher_rows(pane)
        if not rows:
            print(f"not running: pane={pane}")
            return
        for row in rows:
            thread_id = row.get("thread", "").lower()
            if not row.get("runtime_status", "") or not context.is_thread_id(thread_id):
                continue
            if row.get("paused", "") == "1":
                print(f"already paused: pane={pane} thread_id={thread_id}")
                continue
            context.daemon_rpc_patch_session(
                thread_id,
                {"paused": True},
                wait={
                    "kind": "runtime",
                    "pane": pane,
                    "paused": True,
                    "statuses": ["paused"],
                },
                timeout_secs=2.0,
            )
            print(f"paused: pane={pane} thread_id={thread_id}")
        return

    had_any = False
    for row in context.watcher_rows(""):
        had_any = True
        thread_id = row.get("thread", "").lower()
        if not row.get("runtime_status", "") or not context.is_thread_id(thread_id):
            continue
        if row.get("paused", "") == "1":
            print(f"already paused: pane={row['pane']} thread_id={thread_id}")
            continue
        context.daemon_rpc_patch_session(
            thread_id,
            {"paused": True},
            wait={
                "kind": "runtime",
                "pane": row["pane"],
                "paused": True,
                "statuses": ["paused"],
            },
            timeout_secs=2.0,
        )
        print(f"paused: pane={row['pane']} thread_id={thread_id}")

    if not had_any:
        print("not running")


def restart_one(context: CommandContext, target: str, pane: str, rest: list[str]) -> None:
    if pane != target:
        context.print_progress(f"resolved: target={target} pane={pane}")
    context.print_progress(f"restart: preparing pane={pane}")

    thread_arg, msg_value, msg_explicit, restart_codex = parse_restart_args(rest)
    model = context.build_runtime_model()
    recovered_session = None

    resolved_thread = ""
    if thread_arg and thread_arg != "auto":
        if restart_codex and not context.is_thread_id(thread_arg):
            print(f"error: invalid thread_id '{thread_arg}'", file=sys.stderr)
            sys.exit(1)
        if context.is_thread_id(thread_arg):
            resolved_thread = thread_arg.lower()
        pane_record = model.panes_by_id.get(pane)
        live_thread = (
            pane_record.detected_thread.lower()
            if pane_record and context.is_thread_id(pane_record.detected_thread)
            else ""
        )
        if live_thread and resolved_thread and live_thread != resolved_thread and not restart_codex:
            print(
                f"error: pane {pane} is running thread {context.short_thread_id(live_thread)}, "
                f"not requested thread {context.short_thread_id(resolved_thread)}",
                file=sys.stderr,
            )
            print(
                "hint: use --restart-codex to relaunch Codex into that thread, or restart against the live pane thread",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        context.print_progress(f"restart: resolving Codex thread for pane={pane}")
        resolved_thread, thread_error = context.strict_thread_for_pane(model, pane)
        if thread_error:
            print(f"error: {thread_error}", file=sys.stderr)
            print(
                f"hint: clean up pane {pane} so exactly one live pane, one runtime, and one session agree on the thread",
                file=sys.stderr,
            )
            sys.exit(1)
    if resolved_thread:
        context.print_progress(f"restart: thread_id={resolved_thread}")
    else:
        context.print_progress(f"restart: thread_id unresolved for pane={pane}")

    if not msg_explicit:
        context.print_progress(f"restart: loading saved continue message for pane={pane}")
        if resolved_thread and context.is_thread_id(resolved_thread):
            session = None
            session, session_error = context.strict_session_for_thread(model, resolved_thread)
            if session_error:
                recovered_session = context.recoverable_named_session_for_pane(model, pane)
                if not recovered_session or not recovered_session.message:
                    print(f"error: {session_error}", file=sys.stderr)
                    print(
                        f"hint: repair the saved session for thread {context.short_thread_id(resolved_thread)} before restarting without --message",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                context.print_progress(
                    "restart: adopting saved continue message from dead thread "
                    f"{context.short_thread_id(recovered_session.thread_id)}"
                )
                msg_value = recovered_session.message
            if session and session.message:
                msg_value = session.message

    if not thread_arg:
        thread_arg = resolved_thread

    if restart_codex:
        if not resolved_thread or not context.is_thread_id(resolved_thread):
            print(
                f"error: could not determine thread_id for pane={pane}; "
                "--restart-codex requires a concrete thread id",
                file=sys.stderr,
            )
            sys.exit(1)

    existing_session = context.read_session_state(resolved_thread) if context.is_thread_id(resolved_thread) else {}
    next_generation = context.current_session_generation(existing_session) + 1

    if restart_codex and context.is_thread_id(resolved_thread):
        context.daemon_rpc_patch_session(
            resolved_thread,
            {
                "paused": True,
                "generation": context.current_session_generation(existing_session),
            },
            wait={
                "kind": "runtime",
                "pane": pane,
                "paused": True,
                "statuses": ["paused"],
            },
            timeout_secs=2.0,
        )

    if restart_codex:
        context.print_progress(f"restarting codex: pane={pane} thread_id={resolved_thread}")
        context.daemon_rpc_restart_codex(pane, resolved_thread)

    context.print_progress(f"restart: starting runtime for pane={pane}")
    window_name = (context.run_tmux("display-message", "-p", "-t", pane, "#{window_name}") or "").strip()
    row = context.daemon_rpc_upsert_session(
        resolved_thread,
        pane=pane,
        name=window_name,
        message=msg_value,
        paused=False,
        generation=next_generation,
    )
    if recovered_session and recovered_session.thread_id != resolved_thread:
        context.daemon_rpc_remove_session(recovered_session.thread_id, timeout_secs=2.0)
    daemon_pid = row.get("daemon_pid", "")
    context.print_progress(f"started: pane={pane} thread_id={resolved_thread} daemon_pid={daemon_pid}")


def restart_panes(
    context: CommandContext,
    panes: list[str],
    *,
    stop_pane_watchers_fn,
    restart_one_fn,
) -> None:
    restarted = 0
    failed = 0
    failed_details: list[str] = []
    for pane in panes:
        if context.run_tmux("display-message", "-p", "-t", pane, "#{pane_id}") is None:
            stop_pane_watchers_fn(pane)
            continue

        context.print_progress(f"--- restarting {pane} ---")
        captured_err = io.StringIO()
        printed_err = False
        try:
            old_stderr = sys.stderr
            sys.stderr = captured_err
            restart_one_fn(pane, pane, [])
        except SystemExit as exc:
            failed += 1
            detail_text = captured_err.getvalue()
            if detail_text:
                print(detail_text, file=old_stderr, end="" if detail_text.endswith("\n") else "\n")
                printed_err = True
            detail = system_exit_detail(exc, detail_text)
            failed_details.append(f"{pane}: {detail}")
            print(f"error: restart failed for pane={pane}: {detail}", file=old_stderr)
            continue
        finally:
            sys.stderr = old_stderr
            detail_text = captured_err.getvalue()
            if detail_text and not printed_err:
                print(detail_text, file=old_stderr, end="" if detail_text.endswith("\n") else "\n")
        restarted += 1

    if restarted == 0:
        print("no live panes to restart")
    elif failed:
        print(f"restart: {failed} pane(s) failed")
        for detail in failed_details:
            print(f"restart: failure detail: {detail}")


def cmd_resume(
    context: CommandContext,
    argv: list[str],
    *,
    restart_panes_fn,
    restart_one_fn,
) -> None:
    if "*" in argv:
        print("error: '*' is no longer supported for resume; use 'acw resume' with no target", file=sys.stderr)
        sys.exit(2)

    if argv:
        seen_panes: set[str] = set()
        for pane_arg in argv:
            pane = context.resolve_pane_target(pane_arg)
            if pane in seen_panes:
                continue
            seen_panes.add(pane)
            rows = context.watcher_rows(pane)
            if not rows:
                print(f"not running: pane={pane}")
                continue
            for row in rows:
                thread_id = row.get("thread", "").lower()
                if not row.get("runtime_status", "") or not context.is_thread_id(thread_id):
                    continue
                if row.get("paused", "") != "1":
                    print(f"not paused: pane={pane} thread_id={thread_id}")
                    continue
                print(f"resuming paused session via restart: pane={pane} thread_id={thread_id}")
                restart_one_fn(pane_arg, pane, [])
                break
        return

    paused_panes: list[str] = []
    for row in context.watcher_rows(""):
        thread_id = row.get("thread", "").lower()
        if (
            row.get("runtime_status", "")
            and context.is_thread_id(thread_id)
            and row.get("paused", "") == "1"
            and row["pane"]
        ):
            paused_panes.append(row["pane"])

    if not paused_panes:
        print("no paused managed sessions")
        return

    print(f"resuming {len(paused_panes)} paused managed session(s) via restart")
    restart_panes_fn(paused_panes)


def cmd_restart(
    context: CommandContext,
    argv: list[str],
    *,
    restart_panes_fn,
    restart_one_fn,
) -> None:
    rest = list(argv)
    if rest and not rest[0].startswith("--"):
        target = rest.pop(0)
    else:
        target = ""
    if target == "*":
        print("error: '*' is no longer supported for restart; use 'acw restart' with no target", file=sys.stderr)
        sys.exit(2)
    if not target:
        if rest:
            print("error: restart does not accept additional arguments", file=sys.stderr)
            sys.exit(2)
        model = context.build_runtime_model()
        panes = {pane.pane_id for pane in model.snapshot.panes if pane.pane_id and context.is_thread_id(pane.detected_thread)}
        panes.update({watcher.pane for watcher in model.snapshot.watchers if watcher.pane})
        live_panes = sorted(panes)
        if not live_panes:
            print("no live panes to restart")
            return
        restart_panes_fn(live_panes)
        return

    model = context.build_runtime_model()
    pane = context.resolve_restart_pane_target(target, model)
    restart_one_fn(target, pane, rest)


def cmd_edit(context: CommandContext, argv: list[str], *, cmd_restart_fn) -> None:
    if not argv or argv[0].startswith("--"):
        print(
            "error: pane target is required\n"
            "usage: auto_continue_watchd.py edit <pane-id|window-index|session:window>",
            file=sys.stderr,
        )
        sys.exit(2)

    target = argv[0]
    pane = context.resolve_pane_target(target)
    if pane != target:
        print(f"resolved: target={target} pane={pane}")

    model = context.build_runtime_model()
    tid, thread_error = context.strict_thread_for_pane(model, pane)
    if thread_error:
        print(f"error: {thread_error}", file=sys.stderr)
        print(
            f"hint: clean up pane {pane} so exactly one live pane, one runtime, and one saved session agree before editing",
            file=sys.stderr,
        )
        sys.exit(1)

    session, session_error = context.strict_session_for_thread(model, tid)
    if session_error:
        print(f"error: {session_error}", file=sys.stderr)
        print(
            f"hint: repair the saved session for thread {context.short_thread_id(tid)} before editing its message",
            file=sys.stderr,
        )
        sys.exit(1)

    value = session.message if session else ""

    new_value = context.edit_message_interactive(value)
    if new_value is None:
        return

    if new_value == value:
        print("edit: message unchanged")
        return

    if tid and context.is_thread_id(tid):
        context.daemon_rpc_patch_session(tid, {"message": new_value}, timeout_secs=2.0)
    print(f"edit: message updated for pane {pane}")
    if context.runtime_watchers_for_pane(model, pane):
        cmd_restart_fn([pane, "--message", new_value])


def repair_resolve_target(
    context: CommandContext,
    target: str,
    *,
    model: Any | None = None,
) -> tuple[str, str]:
    if model is None:
        model = context.build_runtime_model()
    pane, error = context.doctor_resolve_target(target, model)
    if pane or not target:
        return pane, error

    if context.is_thread_id(target):
        session = context.runtime_session_for_thread(model, target)
        if session:
            return "", (
                f"saved session for thread {context.short_thread_id(target)} has no live pane; "
                "repair only handles live state, so use 'acw cleanup' or restart Codex in the intended pane first"
            )
    elif model.sessions_by_name.get(target, ()):
        return "", (
            f"saved session target '{target}' has no live pane; repair only handles live state, "
            "so use 'acw cleanup' or restart Codex in the intended pane first"
        )
    return "", error


def repair_command_hint(step: Any) -> str:
    if step.kind == "cleanup_dead_sessions":
        return "acw cleanup"
    if step.target:
        return f"acw repair {shlex.quote(step.target)}"
    return ""


def print_repair_plan(steps: list[Any], *, dry_run: bool) -> None:
    header = "repair: dry-run plan" if dry_run else "repair: plan"
    print(header)
    for step in steps:
        prefix = "AUTO" if step.auto_apply else "MANUAL"
        print(f"{prefix}: {step.summary}")
        command = repair_command_hint(step)
        if command:
            print(f"run: {command}")


def apply_repair_step(
    context: CommandContext,
    step: Any,
    *,
    cmd_start_fn,
    stop_pane_watchers_keep_session_fn,
) -> None:
    def remove_source_session() -> None:
        if step.source_thread_id and step.source_thread_id != step.thread_id:
            context.daemon_rpc_remove_session(step.source_thread_id, timeout_secs=2.0)

    if step.kind == "restart_watcher":
        context.print_progress(f"repair: restarting runtime on pane={step.target}")
        stop_pane_watchers_keep_session_fn(step.target)
        cmd_start_fn([step.target, step.thread_id, "--message", step.message])
        remove_source_session()
        return

    if step.kind == "start_watcher":
        context.print_progress(f"repair: starting runtime on pane={step.target}")
        cmd_start_fn([step.target, step.thread_id, "--message", step.message])
        remove_source_session()
        return

    if step.kind == "stop_watchers_keep_session":
        context.print_progress(
            f"repair: stopping runtime entries on pane={step.target} while keeping saved session state"
        )
        stop_pane_watchers_keep_session_fn(step.target)
        return

    if step.kind == "recreate_session":
        context.print_progress(f"repair: recreating saved session state for pane={step.target}")
        window_name = (context.run_tmux("display-message", "-p", "-t", step.target, "#{window_name}") or "").strip()
        context.write_session_state(
            step.thread_id,
            {
                "thread_id": step.thread_id,
                "pane": step.target,
                "name": window_name,
                "message": context.message_text(step.message),
                "tmux_socket": context.current_tmux_scope(),
                "generation": step.generation,
                "paused": step.paused,
            },
        )
        remove_source_session()
        return

    if step.kind == "sync_session_name":
        context.print_progress(f"repair: syncing saved session name for pane={step.target}")
        window_name = (context.run_tmux("display-message", "-p", "-t", step.target, "#{window_name}") or "").strip()
        context.write_session_state(step.thread_id, {"thread_id": step.thread_id, "name": window_name})
        return

    raise RuntimeError(f"unsupported repair step: {step.kind}")


def cmd_repair(
    context: CommandContext,
    argv: list[str],
    *,
    cmd_start_fn,
    stop_pane_watchers_keep_session_fn,
) -> None:
    target, dry_run, _all_targets = parse_repair_args(argv)
    model = context.build_runtime_model()

    target_panes: tuple[str, ...] | None = None
    if target:
        pane, error = repair_resolve_target(context, target, model=model)
        if error:
            print(f"error: {error}", file=sys.stderr)
            sys.exit(1)
        target_panes = (pane,)

    steps = context.build_repair_plan(model, target_panes)
    if not steps:
        print("repair: no live invariant issues found")
        return

    auto_steps = [step for step in steps if step.auto_apply]
    manual_steps = [step for step in steps if not step.auto_apply]

    if dry_run:
        print_repair_plan(steps, dry_run=True)
        return

    if target and manual_steps:
        for step in manual_steps:
            print(f"error: {step.summary}", file=sys.stderr)
        print(
            f"hint: run 'acw repair {shlex.quote(target)} --dry-run' to inspect the blocked repair plan",
            file=sys.stderr,
        )
        sys.exit(1)

    if not auto_steps:
        print("repair: no safe automatic repairs available")
        for step in manual_steps:
            print(f"MANUAL: {step.summary}")
        if manual_steps:
            sys.exit(1)
        return

    context.print_progress(f"repair: applying {len(auto_steps)} step(s)")
    for step in auto_steps:
        apply_repair_step(
            context,
            step,
            cmd_start_fn=cmd_start_fn,
            stop_pane_watchers_keep_session_fn=stop_pane_watchers_keep_session_fn,
        )
    print(f"repair: applied {len(auto_steps)} step(s)")

    if manual_steps:
        print(f"repair: {len(manual_steps)} issue(s) still require manual action")
        for step in manual_steps:
            print(f"MANUAL: {step.summary}")
        sys.exit(1)
