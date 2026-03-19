from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_daemon  # noqa: E402


THREAD = "019cd7fb-f4c1-7613-bf98-4915dcb1970a"
TURN = "019cd7fb-f4cf-7512-8325-dae60d5294e4"


class AcwDaemonUnitTests(unittest.TestCase):
    def test_daemon_session_runtime_backfills_continue_count_from_watch_log(self):
        with tempfile.TemporaryDirectory() as td:
            watch_log = Path(td) / "watch.log"
            watch_log.write_text(
                "watch: pane=%11 thread_id=019cd7fb-f4c1-7613-bf98-4915dcb1970a\n"
                "continue: sent turn=1 thread=019cd7fb-f4c1-7613-bf98-4915dcb1970a\n"
                "continue: sent turn=2 thread=019cd7fb-f4c1-7613-bf98-4915dcb1970a\n",
                encoding="utf-8",
            )
            session = SimpleNamespace(
                thread_id=THREAD,
                pane="%11",
                name="formal",
                message="continue",
                tmux_socket="",
                generation=0,
                paused=False,
                health_detail="",
            )
            writes: list[tuple[str, dict[str, object]]] = []

            runtime = acw_daemon.daemon_session_runtime(
                session,
                codex_log_path=Path(td) / "codex.log",
                state_file="sqlite:session:" + THREAD,
                watch_log=str(watch_log),
                project_cwd=str(ROOT),
                message_text=lambda text: text,
                read_session_state=lambda _thread_id: {},
                write_session_state=lambda thread_id, data: writes.append((thread_id, dict(data))),
                append_log=lambda _path, _line: None,
                check_pending=lambda *_args: None,
                event_cls=lambda *_args: None,
                next_runtime_id=lambda: "runtime-1",
            )

        self.assertEqual(2, runtime.continue_count)

    def test_daemon_process_session_events_increments_continue_count(self):
        runtime = acw_daemon.SessionRuntime(
            thread_id=THREAD,
            pane="%11",
            message="continue",
            tmux_socket="",
            generation=0,
            runtime_id="runtime-1",
            state_file="sqlite:session:" + THREAD,
            watch_log=str(ROOT / "watch.log"),
            cwd=str(ROOT),
            continue_count=2,
        )
        writes: list[tuple[str, dict[str, object]]] = []
        event = SimpleNamespace(
            kind="completion",
            thread_id=THREAD,
            turn_id=TURN,
            detail="false",
        )

        acw_daemon.daemon_process_session_events(
            runtime,
            [event],
            check_pane_for_interrupt=lambda _pane: "",
            check_pane_for_errors=lambda _pane: "",
            tmux_send=lambda *_args, **_kwargs: SimpleNamespace(status="ok", detail=""),
            compute_watch_health=lambda *_args, **_kwargs: ("ok", ""),
            now_ts=lambda: "2026-03-19 12:34:56",
            append_log=lambda _path, _line: None,
            write_session_state=lambda thread_id, data: writes.append((thread_id, dict(data))),
            send_delay_secs="0",
            enter_delay_secs="0",
            time_module=SimpleNamespace(time=lambda: 100.0, sleep=lambda _secs: None),
        )

        self.assertEqual(3, runtime.continue_count)
        self.assertTrue(
            any(
                thread_id == THREAD
                and data.get("continue_count") == 3
                and data.get("last_continue_at") == "2026-03-19 12:34:56"
                for thread_id, data in writes
            )
        )


if __name__ == "__main__":
    unittest.main()
