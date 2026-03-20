import contextlib
import io
import unittest
from types import SimpleNamespace
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_observe  # noqa: E402


def _observe_context(*, status_table, status_details=None, run_watch_command=None):
    if status_details is None:
        status_details = lambda resolved, model=None: None
    return acw_observe.ObserveContext(
        state_dir="/tmp",
        preferred_tmux_socket=lambda: "",
        is_thread_id=lambda value: value.startswith("019c"),
        build_runtime_snapshot=lambda: SimpleNamespace(panes=(), watchers=()),
        build_runtime_model=lambda snapshot: {"snapshot": snapshot},
        build_status_entries_from_snapshot=lambda snapshot: [
            (
                {
                    "thread": "019cd7fb-f4c1-7613-bf98-4915dcb1970a",
                    "state": "sqlite:session:019cd7fb-f4c1-7613-bf98-4915dcb1970a",
                    "session_name": "formal",
                },
                "%11",
                "0:11:formal",
            )
        ],
        tmux_socket_recovery_hint=lambda: "",
        resolve_live_pane_target=lambda target: target,
        status_sort_key=lambda item: item[2],
        status_details=status_details,
        status_table=status_table,
        status_watch_frame=lambda resolved: (f"resolved:{len(resolved)}", f"renderable:{len(resolved)}"),
        print_status_recommendations=lambda resolved: None,
        state_dir_is_writable=lambda: (True, "/tmp"),
        codex_auth_state_available=lambda: (True, "/tmp/auth.json"),
        run_tmux=lambda *args, **kwargs: "",
        doctor_resolve_target=lambda target, model: ("", ""),
        strict_thread_for_pane=lambda model, pane: ("", ""),
        thread_provenance_text=lambda source: source,
        runtime_watchers_for_pane=lambda model, pane: (),
        watcher_anomalies=lambda model, watcher: [],
        read_state_json=lambda state: {},
        compute_state=lambda row, session_json: "running",
        repair_steps_for_pane=lambda model, pane: [],
        current_tmux_pane=lambda: "%11",
        read_session_state=lambda thread_id: {},
        print_doctor_plain=lambda report: None,
        print_doctor_rich=lambda report: None,
        short_thread_id=lambda thread_id: thread_id[:8],
        run_watch_command=run_watch_command,
    )


class AcwObserveUnitTests(unittest.TestCase):
    def test_parse_watch_command_supports_aliases(self):
        self.assertEqual(["edit", "uvm"], acw_observe.parse_watch_command("e uvm"))
        self.assertEqual(["start", "uvm"], acw_observe.parse_watch_command("s uvm"))
        self.assertEqual(["pause", "formal"], acw_observe.parse_watch_command("pause formal"))
        self.assertEqual(["resume", "formal"], acw_observe.parse_watch_command("u formal"))
        self.assertEqual(["resume", "formal"], acw_observe.parse_watch_command("resume formal"))
        self.assertEqual(["restart"], acw_observe.parse_watch_command("r"))

    def test_parse_watch_command_rejects_unknown_command(self):
        with self.assertRaisesRegex(ValueError, "unsupported command"):
            acw_observe.parse_watch_command("frobnicate uvm")

    def test_watch_status_loop_repaints_only_when_content_changes_in_text_mode(self):
        rendered: list[int] = []
        frames: list[tuple[bool, str]] = []
        slept: list[float] = []
        statuses = iter(
            [
                "Sessions: 1\nrow one\n",
                "Sessions: 1\nrow one\n",
                "Sessions: 1\nrow two\n",
            ]
        )

        context = _observe_context(status_table=lambda resolved: rendered.append(len(resolved)))

        with contextlib.redirect_stdout(io.StringIO()):
            acw_observe.watch_status_loop(
                context,
                [],
                stdout_isatty_fn=lambda: True,
                sleep_fn=lambda seconds: slept.append(seconds),
                refresh_secs=0.5,
                stop_after_iterations=3,
                render_status_text_fn=lambda _context, _argv: next(statuses),
                write_frame_fn=lambda text, first_frame: frames.append((first_frame, text)),
                live_display_factory=None,
            )

        self.assertEqual([], rendered)
        self.assertEqual([0.5, 0.5], slept)
        self.assertEqual(2, len(frames))
        self.assertEqual(
            [
                (True, "Watching acw status. Press Ctrl-C to exit.\n\nSessions: 1\nrow one\n"),
                (False, "Watching acw status. Press Ctrl-C to exit.\n\nSessions: 1\nrow two\n"),
            ],
            frames,
        )

    def test_watch_status_loop_updates_live_display_only_when_content_changes(self):
        frames: list[str] = []
        slept: list[float] = []
        statuses = iter(
            [
                ("frame-one", "renderable-one"),
                ("frame-one", "renderable-one"),
                ("frame-two", "renderable-two"),
            ]
        )

        class FakeLive:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def update(self, renderable: str) -> None:
                frames.append(renderable)

        context = _observe_context(status_table=lambda resolved: None)

        with contextlib.redirect_stdout(io.StringIO()):
            acw_observe.watch_status_loop(
                context,
                [],
                stdout_isatty_fn=lambda: True,
                sleep_fn=lambda seconds: slept.append(seconds),
                refresh_secs=0.5,
                stop_after_iterations=3,
                render_status_live_frame_fn=lambda _context, _argv: next(statuses),
                live_display_factory=lambda: FakeLive(),
            )

        self.assertEqual([0.5, 0.5], slept)
        self.assertEqual(
            [
                "renderable-one",
                "renderable-two",
            ],
            frames,
        )

    def test_watch_status_loop_falls_back_to_single_status_when_not_tty(self):
        rendered: list[int] = []
        slept: list[float] = []

        context = _observe_context(status_table=lambda resolved: rendered.append(len(resolved)))

        with contextlib.redirect_stdout(io.StringIO()):
            acw_observe.watch_status_loop(
                context,
                [],
                stdout_isatty_fn=lambda: False,
                sleep_fn=lambda seconds: slept.append(seconds),
            )

        self.assertEqual([1], rendered)
        self.assertEqual([], slept)

    def test_watch_status_loop_dispatches_interactive_command_in_text_mode(self):
        frames: list[tuple[bool, str]] = []
        commands: list[list[str]] = []

        class FakeInput:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self, timeout: float) -> str:
                if not hasattr(self, "_calls"):
                    self._calls = 0
                self._calls += 1
                if self._calls == 1:
                    return "e formal\r"
                return ""

            @contextlib.contextmanager
            def pause(self):
                yield

        def _run_watch_command(argv: list[str]) -> None:
            commands.append(argv)
            print("edit: message unchanged")

        context = _observe_context(
            status_table=lambda resolved: None,
            run_watch_command=_run_watch_command,
        )

        with contextlib.redirect_stdout(io.StringIO()):
            acw_observe.watch_status_loop(
                context,
                [],
                stdout_isatty_fn=lambda: True,
                stdin_isatty_fn=lambda: True,
                refresh_secs=0.5,
                stop_after_iterations=2,
                render_status_text_fn=lambda _context, _argv: "Sessions: 1\nrow one\n",
                write_frame_fn=lambda text, first_frame: frames.append((first_frame, text)),
                live_display_factory=None,
                input_controller_factory=FakeInput,
                monotonic_fn=lambda: 0.0,
            )

        self.assertEqual([["edit", "formal"]], commands)
        self.assertEqual(1, len(frames))
        self.assertIn("> ", frames[0][1])

    def test_run_watch_command_suppresses_benign_pause_success_output(self):
        def _run_watch_command(argv: list[str]) -> None:
            self.assertEqual(["pause", "formal"], argv)
            print("paused: pane=%11 thread_id=019cd7fb-f4c1-7613-bf98-4915dcb1970a")

        context = _observe_context(
            status_table=lambda resolved: None,
            run_watch_command=_run_watch_command,
        )

        feedback = acw_observe.run_watch_command(context, "pause formal")

        self.assertIsNone(feedback)

    def test_run_watch_command_keeps_warning_output_for_pause(self):
        def _run_watch_command(argv: list[str]) -> None:
            self.assertEqual(["pause", "formal"], argv)
            print("not running: pane=%11")

        context = _observe_context(
            status_table=lambda resolved: None,
            run_watch_command=_run_watch_command,
        )

        feedback = acw_observe.run_watch_command(context, "pause formal")

        self.assertIsNotNone(feedback)
        assert feedback is not None
        self.assertIn("not running: pane=%11", feedback.text)

    def test_watch_status_loop_expires_command_feedback_after_ten_seconds(self):
        frames: list[tuple[bool, str]] = []
        commands: list[list[str]] = []

        class FakeClock:
            def __init__(self):
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = FakeClock()

        class FakeInput:
            def __init__(self):
                self.calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self, timeout: float) -> str:
                self.calls += 1
                if self.calls == 1:
                    return "restart formal\r"
                clock.now += timeout
                return ""

            @contextlib.contextmanager
            def pause(self):
                yield

        def _run_watch_command(argv: list[str]) -> None:
            commands.append(argv)
            print("restart: preparing pane=%11")

        context = _observe_context(
            status_table=lambda resolved: None,
            run_watch_command=_run_watch_command,
        )

        with contextlib.redirect_stdout(io.StringIO()):
            acw_observe.watch_status_loop(
                context,
                [],
                stdout_isatty_fn=lambda: True,
                stdin_isatty_fn=lambda: True,
                refresh_secs=1.0,
                stop_after_iterations=12,
                render_status_text_fn=lambda _context, _argv: "Sessions: 1\nrow one\n",
                write_frame_fn=lambda text, first_frame: frames.append((first_frame, text)),
                live_display_factory=None,
                input_controller_factory=FakeInput,
                monotonic_fn=clock.monotonic,
            )

        self.assertEqual([["restart", "formal"]], commands)
        self.assertGreaterEqual(len(frames), 3)
        self.assertIn("restart: preparing pane=%11", frames[1][1])
        self.assertNotIn("restart: preparing pane=%11", frames[-1][1])

    def test_watch_status_loop_swallows_keyboard_interrupt_from_interactive_teardown(self):
        class FakeInput:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                raise KeyboardInterrupt

            def read(self, timeout: float) -> str:
                return ""

        context = _observe_context(status_table=lambda resolved: None)

        with contextlib.redirect_stdout(io.StringIO()):
            acw_observe.watch_status_loop(
                context,
                [],
                stdout_isatty_fn=lambda: True,
                stdin_isatty_fn=lambda: True,
                refresh_secs=0.5,
                stop_after_iterations=1,
                render_status_text_fn=lambda _context, _argv: "Sessions: 1\nrow one\n",
                write_frame_fn=lambda text, first_frame: None,
                live_display_factory=None,
                input_controller_factory=FakeInput,
            )

    def test_watch_status_loop_keeps_running_after_refresh_error(self):
        frames: list[tuple[bool, str]] = []
        calls = 0
        clock = {"now": 0.0}

        class FakeInput:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self, timeout: float) -> str:
                clock["now"] += timeout
                return ""

        def _render_status_text(_context, _argv):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("unable to open database file")
            return "Sessions: 1\nrow one\n"

        context = _observe_context(status_table=lambda resolved: None)

        with contextlib.redirect_stdout(io.StringIO()):
            acw_observe.watch_status_loop(
                context,
                [],
                stdout_isatty_fn=lambda: True,
                stdin_isatty_fn=lambda: True,
                refresh_secs=0.5,
                stop_after_iterations=2,
                render_status_text_fn=_render_status_text,
                write_frame_fn=lambda text, first_frame: frames.append((first_frame, text)),
                live_display_factory=None,
                input_controller_factory=FakeInput,
                monotonic_fn=lambda: clock["now"],
            )

        self.assertEqual(2, calls)
        self.assertEqual(2, len(frames))
        self.assertIn("status refresh failed: unable to open database file", frames[0][1])
        self.assertIn("Sessions: 1\nrow one\n", frames[1][1])

    def test_watch_status_loop_keeps_running_after_refresh_error_in_live_mode(self):
        frames: list[object] = []
        calls = 0
        clock = {"now": 0.0}

        class FakeInput:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self, timeout: float) -> str:
                clock["now"] += timeout
                return ""

        class FakeLive:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def update(self, renderable) -> None:
                frames.append(renderable)

        def _render_status_live(_context, _argv):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("unable to open database file")
            return ("frame-two", "renderable-two")

        context = _observe_context(status_table=lambda resolved: None)

        with contextlib.redirect_stdout(io.StringIO()):
            acw_observe.watch_status_loop(
                context,
                [],
                stdout_isatty_fn=lambda: True,
                stdin_isatty_fn=lambda: True,
                refresh_secs=0.5,
                stop_after_iterations=2,
                render_status_live_frame_fn=_render_status_live,
                live_display_factory=lambda: FakeLive(),
                input_controller_factory=FakeInput,
                monotonic_fn=lambda: clock["now"],
            )

        self.assertEqual(2, calls)
        self.assertEqual(2, len(frames))


if __name__ == "__main__":
    unittest.main()
