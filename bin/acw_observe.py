from __future__ import annotations

import contextlib
import io
import os
import select
import shlex
import sys
import time
from dataclasses import dataclass
from typing import Any
from typing import Callable

import acw_ui


@dataclass
class DoctorReport:
    checks: list[tuple[str, str]]
    result: str
    exit_code: int
    recommendations: list[str]


@dataclass(frozen=True)
class WatchCommandFeedback:
    text: str
    style: str


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
    status_watch_frame: Callable[[list[tuple[dict[str, str], str, str]]], tuple[str, Any]]
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
    run_watch_command: Callable[[list[str]], None] | None = None


_WATCH_COMMAND_HELP = (
    "Commands: s/start <target> [thread-id] [--message TEXT], "
    "e/edit <target>, p/pause [target], u/resume [target], r/restart [target]"
)
_WATCH_FEEDBACK_TTL_SECS = 10.0


def _status_args(argv: list[str]) -> tuple[str, bool]:
    pane_arg = ""
    details = False
    for arg in argv:
        if arg == "--details":
            details = True
        else:
            pane_arg = arg
    return pane_arg, details


def render_status_once(
    context: ObserveContext,
    argv: list[str],
) -> None:
    resolved, model, details = _build_status_entries(
        context,
        argv,
        warn_if_tmux_unavailable=True,
        print_resolution=True,
    )

    detail_tag = " (details)" if details else ""
    print(f"Sessions{detail_tag}: {len(resolved)}")

    if not resolved:
        print("(none)")
        return

    if details:
        context.status_details(resolved, model=model)
    else:
        context.status_table(resolved)
        context.print_status_recommendations(resolved)


def render_status_text(
    context: ObserveContext,
    argv: list[str],
) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render_status_once(context, argv)
    return buffer.getvalue()


def render_status_live_frame(
    context: ObserveContext,
    argv: list[str],
) -> tuple[str, Any]:
    resolved, _model, _details = _build_status_entries(
        context,
        argv,
        warn_if_tmux_unavailable=False,
        print_resolution=False,
    )
    return context.status_watch_frame(resolved)


def parse_watch_command(command_text: str) -> list[str]:
    stripped = command_text.strip()
    if not stripped:
        return []
    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        raise ValueError(f"invalid command: {exc}") from exc
    if not tokens:
        return []
    if tokens[0] == "acw":
        tokens = tokens[1:]
        if not tokens:
            raise ValueError("expected a command after 'acw'")
    cmd = tokens[0].lower()
    aliases = {
        "?": "help",
        "h": "help",
        "e": "edit",
        "s": "start",
        "p": "pause",
        "u": "resume",
        "r": "restart",
    }
    cmd = aliases.get(cmd, cmd)
    if cmd not in {"help", "start", "edit", "pause", "resume", "restart"}:
        raise ValueError(f"unsupported command '{tokens[0]}'; {_WATCH_COMMAND_HELP.lower()}")
    tokens[0] = cmd
    return tokens


def _trim_feedback_lines(text: str, *, max_lines: int = 8) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(["..."] + lines[-(max_lines - 1) :])


def _watch_feedback_noteworthy(argv: list[str], exit_code: int, text: str) -> bool:
    if exit_code != 0:
        return True
    if not text.strip():
        return False
    if argv and argv[0] not in {"edit", "pause", "resume"}:
        return True
    noteworthy_prefixes = (
        "warning:",
        "error:",
        "hint:",
        "already ",
        "not ",
        "no ",
        "recommendation:",
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith(noteworthy_prefixes):
            return True
    return False


def run_watch_command(
    context: ObserveContext,
    command_text: str,
) -> WatchCommandFeedback | None:
    try:
        argv = parse_watch_command(command_text)
    except ValueError as exc:
        return WatchCommandFeedback(str(exc), "bold red")

    if not argv:
        return None
    if argv[0] == "help":
        return WatchCommandFeedback(_WATCH_COMMAND_HELP, "yellow")
    if context.run_watch_command is None:
        return WatchCommandFeedback("interactive commands are unavailable in this build", "bold red")

    output = io.StringIO()
    exit_code = 0
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            context.run_watch_command(argv)
        except SystemExit as exc:
            if isinstance(exc.code, str) and exc.code.strip():
                print(exc.code)
            exit_code = exc.code if isinstance(exc.code, int) else 1

    text = _trim_feedback_lines(output.getvalue())
    if not _watch_feedback_noteworthy(argv, exit_code, text):
        return None
    if not text:
        rendered = " ".join(argv)
        text = f"ok: {rendered}" if exit_code == 0 else f"error: {rendered}"
    style = "bold red" if exit_code else "green"
    return WatchCommandFeedback(text, style)


def _consume_watch_input(
    buffer: str,
    chars: str,
) -> tuple[str, list[str], bool]:
    current = buffer
    submitted: list[str] = []
    changed = False

    for ch in chars:
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in ("\r", "\n"):
            if current.strip():
                submitted.append(current.strip())
            current = ""
            changed = True
            continue
        if ch in ("\x7f", "\b"):
            if current:
                current = current[:-1]
                changed = True
            continue
        if ch == "\x15":
            if current:
                current = ""
                changed = True
            continue
        if ch.isprintable():
            current += ch
            changed = True

    return current, submitted, changed


class _CbreakInput:
    def __init__(self) -> None:
        self._fd = sys.stdin.fileno()
        self._saved_attrs: Any | None = None
        self._cbreak_attrs: Any | None = None

    def __enter__(self) -> "_CbreakInput":
        import termios

        self._saved_attrs = termios.tcgetattr(self._fd)
        self._cbreak_attrs = termios.tcgetattr(self._fd)
        self._cbreak_attrs[3] &= ~(termios.ICANON | termios.ECHO)
        self._cbreak_attrs[6][termios.VMIN] = 1
        self._cbreak_attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self._fd, termios.TCSANOW, self._cbreak_attrs)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        import termios

        if self._saved_attrs is not None:
            termios.tcsetattr(self._fd, termios.TCSANOW, self._saved_attrs)

    def read(self, timeout: float) -> str:
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if self._fd not in ready:
            return ""
        data = os.read(self._fd, 1024)
        return data.decode("utf-8", errors="ignore")

    @contextlib.contextmanager
    def pause(self):
        import termios

        if self._saved_attrs is None:
            yield
            return
        termios.tcsetattr(self._fd, termios.TCSANOW, self._saved_attrs)
        try:
            yield
        finally:
            if self._cbreak_attrs is not None:
                termios.tcsetattr(self._fd, termios.TCSANOW, self._cbreak_attrs)


def _write_watch_frame(text: str, *, first_frame: bool) -> None:
    if first_frame:
        sys.stdout.write("\x1b[2J\x1b[H")
    else:
        sys.stdout.write("\x1b[H\x1b[J")
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _build_status_entries(
    context: ObserveContext,
    argv: list[str],
    *,
    warn_if_tmux_unavailable: bool,
    print_resolution: bool,
) -> tuple[list[tuple[dict[str, str], str, str]], Any, bool]:
    pane_arg, details = _status_args(argv)
    snapshot = context.build_runtime_snapshot()
    model = context.build_runtime_model(snapshot)
    if warn_if_tmux_unavailable and not snapshot.panes and not snapshot.watchers:
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
        if print_resolution and pane and pane != target:
            print(f"resolved: target={target} pane={pane}")
        target_thread = target.lower() if context.is_thread_id(target) else ""
        resolved = [
            (row, live_pane, window_label)
            for row, live_pane, window_label in resolved
            if (pane and live_pane == pane)
            or (target_thread and row.get("thread", "").lower() == target_thread)
            or row.get("session_name", "") == target
        ]

    resolved.sort(key=context.status_sort_key)
    return resolved, model, details


class _RichLiveDisplay:
    def __init__(self) -> None:
        from rich.console import Console
        from rich.live import Live

        self._live = Live(
            "",
            console=Console(force_terminal=True, color_system="standard"),
            auto_refresh=False,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
            screen=False,
            vertical_overflow="visible",
        )

    def __enter__(self) -> "_RichLiveDisplay":
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._live.__exit__(exc_type, exc, tb)

    def update(self, renderable: Any) -> None:
        self._live.update(renderable, refresh=True)

    @contextlib.contextmanager
    def pause(self):
        self._live.stop()
        try:
            yield
        finally:
            self._live.start(refresh=True)


def _wrap_watch_renderable(
    renderable: Any,
    command_buffer: str,
    feedback: WatchCommandFeedback | None,
) -> Any:
    from rich.console import Group
    from rich.text import Text

    renderables: list[Any] = [renderable]
    if feedback and feedback.text:
        renderables.append(Text(feedback.text, style=feedback.style))
    renderables.append(Text(f"> {command_buffer}", style="bold cyan" if command_buffer else "cyan"))
    return Group(*renderables)


def _build_watch_text_frame(
    status_text: str,
    command_buffer: str,
    feedback: WatchCommandFeedback | None,
) -> str:
    parts = ["Watching acw status. Press Ctrl-C to exit.", "", status_text.rstrip("\n")]
    parts[0] = acw_ui.watch_header_text()
    if feedback and feedback.text:
        parts.extend(["", feedback.text])
    parts.extend(["", f"> {command_buffer}"])
    return "\n".join(parts) + "\n"


@contextlib.contextmanager
def _null_pause():
    yield


def _interactive_watch_loop(
    context: ObserveContext,
    argv: list[str],
    *,
    use_live: bool,
    sleep_fn: Callable[[float], None],
    refresh_secs: float,
    stop_after_iterations: int | None,
    render_status_text_fn: Callable[[ObserveContext, list[str]], str],
    render_status_live_frame_fn: Callable[[ObserveContext, list[str]], tuple[str, Any]],
    write_frame_fn: Callable[[str, bool], None],
    live_display_factory: Callable[[], Any] | None,
    input_controller_factory: Callable[[], Any],
    monotonic_fn: Callable[[], float],
) -> None:
    command_buffer = ""
    feedback: WatchCommandFeedback | None = None
    feedback_expires_at = 0.0
    iterations = 0
    last_frame_key = ""
    first_frame = True
    next_refresh = 0.0
    base_frame_key = ""
    base_renderable: Any = None
    base_text = ""

    def refresh_base() -> None:
        nonlocal base_frame_key, base_renderable, base_text, next_refresh, feedback, feedback_expires_at
        try:
            if use_live:
                base_frame_key, base_renderable = render_status_live_frame_fn(context, argv)
            else:
                base_text = render_status_text_fn(context, argv)
                base_frame_key = base_text
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            message = f"status refresh failed: {exc}"
            feedback = WatchCommandFeedback(message, "red")
            feedback_expires_at = monotonic_fn() + _WATCH_FEEDBACK_TTL_SECS
            if use_live:
                base_frame_key = f"error:{message}"
                base_renderable = acw_ui.build_rich_watch_header()
            else:
                base_text = ""
                base_frame_key = f"error:{message}"
        next_refresh = monotonic_fn() + refresh_secs

    def render_current(live: Any | None = None) -> None:
        nonlocal last_frame_key, first_frame, feedback, feedback_expires_at
        if feedback is not None and monotonic_fn() >= feedback_expires_at:
            feedback = None
            feedback_expires_at = 0.0
        frame_key = repr(
            (
                base_frame_key,
                command_buffer,
                feedback.text if feedback else "",
                feedback.style if feedback else "",
            )
        )
        if frame_key == last_frame_key:
            return
        if use_live:
            live.update(_wrap_watch_renderable(base_renderable, command_buffer, feedback))
        else:
            write_frame_fn(
                _build_watch_text_frame(base_text, command_buffer, feedback),
                first_frame,
            )
            first_frame = False
        last_frame_key = frame_key

    def execute_submitted_commands(
        submitted: list[str],
        *,
        live: Any | None = None,
        input_controller: Any | None = None,
    ) -> None:
        nonlocal feedback, feedback_expires_at
        for command_text in submitted:
            display_pause = live.pause() if live is not None and hasattr(live, "pause") else _null_pause()
            input_pause = input_controller.pause() if input_controller is not None and hasattr(input_controller, "pause") else _null_pause()
            with display_pause, input_pause:
                feedback = run_watch_command(context, command_text)
                feedback_expires_at = monotonic_fn() + _WATCH_FEEDBACK_TTL_SECS if feedback is not None else 0.0

    with input_controller_factory() as input_controller:
        live_context = None
        if use_live:
            try:
                live_context = live_display_factory() if live_display_factory is not None else None
            except ModuleNotFoundError:
                live_context = None
            if live_context is None:
                use_live = False
        if use_live and live_context is not None:
            with live_context as live:
                while True:
                    if monotonic_fn() >= next_refresh:
                        refresh_base()
                    render_current(live)
                    iterations += 1
                    if stop_after_iterations is not None and iterations >= stop_after_iterations:
                        return
                    timeout = max(0.0, next_refresh - monotonic_fn())
                    chars = input_controller.read(timeout)
                    if not chars:
                        continue
                    command_buffer, submitted, changed = _consume_watch_input(command_buffer, chars)
                    if changed and not submitted:
                        feedback = None
                        feedback_expires_at = 0.0
                    if submitted:
                        execute_submitted_commands(submitted, live=live, input_controller=input_controller)
                        refresh_base()
        else:
            while True:
                if monotonic_fn() >= next_refresh:
                    refresh_base()
                render_current()
                iterations += 1
                if stop_after_iterations is not None and iterations >= stop_after_iterations:
                    return
                timeout = max(0.0, next_refresh - monotonic_fn())
                chars = input_controller.read(timeout)
                if not chars:
                    continue
                command_buffer, submitted, changed = _consume_watch_input(command_buffer, chars)
                if changed and not submitted:
                    feedback = None
                    feedback_expires_at = 0.0
                if submitted:
                    execute_submitted_commands(submitted, input_controller=input_controller)
                    refresh_base()


def watch_status_loop(
    context: ObserveContext,
    argv: list[str],
    *,
    stdout_isatty_fn: Callable[[], bool] = lambda: sys.stdout.isatty(),
    stdin_isatty_fn: Callable[[], bool] = lambda: sys.stdin.isatty(),
    sleep_fn: Callable[[float], None] = time.sleep,
    refresh_secs: float = 1.0,
    stop_after_iterations: int | None = None,
    render_status_text_fn: Callable[[ObserveContext, list[str]], str] = render_status_text,
    render_status_live_frame_fn: Callable[[ObserveContext, list[str]], tuple[str, Any]] = render_status_live_frame,
    write_frame_fn: Callable[[str, bool], None] = lambda text, first_frame: _write_watch_frame(
        text,
        first_frame=first_frame,
    ),
    live_display_factory: Callable[[], Any] | None = _RichLiveDisplay,
    input_controller_factory: Callable[[], Any] | None = _CbreakInput,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> None:
    if not stdout_isatty_fn():
        render_status_once(context, argv)
        return

    _pane_arg, details = _status_args(argv)
    use_live = live_display_factory is not None and not details
    if stdin_isatty_fn() and input_controller_factory is not None:
        try:
            _interactive_watch_loop(
                context,
                argv,
                use_live=use_live,
                sleep_fn=sleep_fn,
                refresh_secs=refresh_secs,
                stop_after_iterations=stop_after_iterations,
                render_status_text_fn=render_status_text_fn,
                render_status_live_frame_fn=render_status_live_frame_fn,
                write_frame_fn=write_frame_fn,
                live_display_factory=live_display_factory,
                input_controller_factory=input_controller_factory,
                monotonic_fn=monotonic_fn,
            )
        except KeyboardInterrupt:
            return
        return
    iterations = 0
    last_frame = ""
    first_frame = True
    try:
        if use_live:
            try:
                live_context = live_display_factory()
            except ModuleNotFoundError:
                live_context = None
            if live_context is None:
                use_live = False
        if use_live:
            with live_context as live:
                while True:
                    frame_key, renderable = render_status_live_frame_fn(context, argv)
                    if frame_key != last_frame:
                        live.update(renderable)
                        last_frame = frame_key
                    iterations += 1
                    if stop_after_iterations is not None and iterations >= stop_after_iterations:
                        return
                    sleep_fn(refresh_secs)
        else:
            while True:
                frame = "Watching acw status. Press Ctrl-C to exit.\n\n"
                frame += render_status_text_fn(context, argv)
                if frame != last_frame:
                    write_frame_fn(frame, first_frame)
                    last_frame = frame
                    first_frame = False
                iterations += 1
                if stop_after_iterations is not None and iterations >= stop_after_iterations:
                    return
                sleep_fn(refresh_secs)
    except KeyboardInterrupt:
        return


def cmd_status(context: ObserveContext, argv: list[str]) -> None:
    render_status_once(context, argv)


def cmd_watch(context: ObserveContext, argv: list[str]) -> None:
    watch_status_loop(context, argv)


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
