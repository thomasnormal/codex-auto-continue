import contextlib
import io
import unittest
from types import SimpleNamespace
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_observe  # noqa: E402


def _observe_context(*, status_table, status_details=None):
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
    )


class AcwObserveUnitTests(unittest.TestCase):
    def test_watch_status_loop_refreshes_multiple_times_on_tty(self):
        rendered: list[int] = []
        cleared: list[int] = []
        slept: list[float] = []
        output = io.StringIO()

        context = _observe_context(status_table=lambda resolved: rendered.append(len(resolved)))

        with contextlib.redirect_stdout(io.StringIO()):
            acw_observe.watch_status_loop(
                context,
                [],
                stdout_isatty_fn=lambda: True,
                clear_screen_fn=lambda: cleared.append(1),
                sleep_fn=lambda seconds: slept.append(seconds),
                refresh_secs=0.5,
                stop_after_iterations=2,
                print_fn=lambda text="": output.write(text + "\n"),
            )

        self.assertEqual([1, 1], rendered)
        self.assertEqual([1, 1], cleared)
        self.assertEqual([0.5], slept)
        self.assertIn("Watching acw status", output.getvalue())

    def test_watch_status_loop_falls_back_to_single_status_when_not_tty(self):
        rendered: list[int] = []
        cleared: list[int] = []
        slept: list[float] = []

        context = _observe_context(status_table=lambda resolved: rendered.append(len(resolved)))

        with contextlib.redirect_stdout(io.StringIO()):
            acw_observe.watch_status_loop(
                context,
                [],
                stdout_isatty_fn=lambda: False,
                clear_screen_fn=lambda: cleared.append(1),
                sleep_fn=lambda seconds: slept.append(seconds),
                print_fn=lambda text="": None,
            )

        self.assertEqual([1], rendered)
        self.assertEqual([], cleared)
        self.assertEqual([], slept)


if __name__ == "__main__":
    unittest.main()
