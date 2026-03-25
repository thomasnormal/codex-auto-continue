from types import SimpleNamespace
import subprocess
import tempfile
import unittest
import unittest.mock
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
            capture_pane_text=lambda _pane: "",
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

    def test_daemon_process_session_events_auto_pauses_repeated_docs_only_checkpoint_loop(self):
        runtime = acw_daemon.SessionRuntime(
            thread_id=THREAD,
            pane="%11",
            message="continue by docs/DDR3_PLAN.md",
            tmux_socket="",
            generation=0,
            runtime_id="runtime-1",
            state_file="sqlite:session:" + THREAD,
            watch_log=str(ROOT / "watch.log"),
            cwd=str(ROOT),
            loop_signature="docs-only:docs/DDR3_PLAN.md",
            loop_score=3,
        )
        writes: list[tuple[str, dict[str, object]]] = []
        event = SimpleNamespace(
            kind="completion",
            thread_id=THREAD,
            turn_id=TURN,
            detail="false",
        )
        pane_text = (
            "• Updated:\n"
            "  - docs/DDR3_PLAN.md\n"
            "\n"
            "  Committed:\n"
            "  [DDR3] checkpoint\n"
            "\n"
            "› continue by docs/DDR3_PLAN.md\n"
        )
        send_calls: list[tuple[object, ...]] = []

        acw_daemon.daemon_process_session_events(
            runtime,
            [event],
            check_pane_for_interrupt=lambda _pane: "",
            check_pane_for_errors=lambda _pane: "",
            capture_pane_text=lambda _pane: pane_text,
            tmux_send=lambda *_args, **_kwargs: send_calls.append(_args) or SimpleNamespace(status="ok", detail=""),
            compute_watch_health=lambda *_args, **_kwargs: ("ok", ""),
            now_ts=lambda: "2026-03-19 12:34:56",
            append_log=lambda _path, _line: None,
            write_session_state=lambda thread_id, data: writes.append((thread_id, dict(data))),
            send_delay_secs="0",
            enter_delay_secs="0",
            time_module=SimpleNamespace(time=lambda: 100.0, sleep=lambda _secs: None),
        )

        self.assertTrue(runtime.paused)
        self.assertEqual("paused", runtime.runtime_status)
        self.assertEqual(4, runtime.loop_score)
        self.assertIn("suspected loop: repeated docs-only checkpoint turns", runtime.health_detail)
        self.assertEqual([], send_calls)
        self.assertTrue(
            any(
                thread_id == THREAD
                and data.get("paused") is True
                and "suspected loop" in str(data.get("health_detail", ""))
                for thread_id, data in writes
            )
        )

    def test_daemon_continue_session_increments_continue_count(self):
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

        ok, detail = acw_daemon.daemon_continue_session(
            runtime,
            check_pane_for_interrupt=lambda _pane: "",
            check_pane_for_errors=lambda _pane: "",
            tmux_send=lambda *_args, **_kwargs: SimpleNamespace(status="ok", detail=""),
            now_ts=lambda: "2026-03-19 12:34:56",
            append_log=lambda _path, _line: None,
            write_session_state=lambda thread_id, data: writes.append((thread_id, dict(data))),
            enter_delay_secs="0",
            time_module=SimpleNamespace(time=lambda: 100.0),
        )

        self.assertTrue(ok)
        self.assertEqual("", detail)
        self.assertEqual(3, runtime.continue_count)
        self.assertTrue(
            any(
                thread_id == THREAD
                and data.get("continue_count") == 3
                and data.get("last_continue_at") == "2026-03-19 12:34:56"
                for thread_id, data in writes
            )
        )


import acw_control  # noqa: E402


class DiskSpaceMonitorTests(unittest.TestCase):
    def _make_runtimes(self, panes, paused=None):
        paused = paused or {}
        return {
            f"thread-{i}": SimpleNamespace(pane=pane, paused=paused.get(pane, False))
            for i, pane in enumerate(panes)
        }

    def test_skips_check_before_interval(self):
        last_check, last_alert = acw_control._check_disk_space(
            {},
            last_check=100.0,
            last_alert=0.0,
            now=110.0,  # only 10s since last check, interval is 60s
        )
        self.assertEqual(last_check, 100.0)  # unchanged

    def test_no_alert_when_enough_space(self):
        tmux_calls = []
        orig_run = subprocess.run

        def fake_run(*args, **kwargs):
            tmux_calls.append(args)

        with unittest.mock.patch("acw_control.shutil.disk_usage") as mock_du, \
             unittest.mock.patch("acw_control.subprocess.run", fake_run):
            mock_du.return_value = SimpleNamespace(
                total=500 * 1024**3, free=100 * 1024**3, used=400 * 1024**3,
            )  # 20% free
            last_check, last_alert = acw_control._check_disk_space(
                self._make_runtimes(["%1"]),
                last_check=0.0,
                last_alert=0.0,
                now=100.0,
            )

        self.assertEqual(last_check, 100.0)  # updated
        self.assertEqual(last_alert, 0.0)  # no alert
        self.assertEqual(tmux_calls, [])

    def test_alerts_all_panes_when_low(self):
        tmux_calls = []

        def fake_run(cmd, **kwargs):
            tmux_calls.append(cmd)
            return SimpleNamespace(returncode=0)

        with unittest.mock.patch("acw_control.shutil.disk_usage") as mock_du, \
             unittest.mock.patch("acw_control.subprocess.run", fake_run):
            mock_du.return_value = SimpleNamespace(
                total=500 * 1024**3, free=20 * 1024**3, used=480 * 1024**3,
            )  # 4% free
            last_check, last_alert = acw_control._check_disk_space(
                self._make_runtimes(["%1", "%5"]),
                last_check=0.0,
                last_alert=0.0,
                now=100.0,
            )

        self.assertEqual(last_check, 100.0)
        self.assertEqual(last_alert, 100.0)
        # 2 panes x 2 tmux calls each (send-keys -l + C-m)
        self.assertEqual(len(tmux_calls), 4)
        # Verify both panes were targeted
        targeted_panes = {c[3] for c in tmux_calls if "send-keys" in c}
        self.assertEqual(targeted_panes, {"%1", "%5"})

    def test_alerts_paused_agents_too(self):
        tmux_calls = []

        def fake_run(cmd, **kwargs):
            tmux_calls.append(cmd)
            return SimpleNamespace(returncode=0)

        with unittest.mock.patch("acw_control.shutil.disk_usage") as mock_du, \
             unittest.mock.patch("acw_control.subprocess.run", fake_run):
            mock_du.return_value = SimpleNamespace(
                total=500 * 1024**3, free=20 * 1024**3, used=480 * 1024**3,
            )
            last_check, last_alert = acw_control._check_disk_space(
                self._make_runtimes(["%1", "%5"], paused={"%5": True}),
                last_check=0.0,
                last_alert=0.0,
                now=100.0,
            )

        targeted_panes = {c[3] for c in tmux_calls if "send-keys" in c}
        self.assertEqual(targeted_panes, {"%1", "%5"})

    def test_respects_cooldown(self):
        tmux_calls = []

        def fake_run(cmd, **kwargs):
            tmux_calls.append(cmd)
            return SimpleNamespace(returncode=0)

        with unittest.mock.patch("acw_control.shutil.disk_usage") as mock_du, \
             unittest.mock.patch("acw_control.subprocess.run", fake_run):
            mock_du.return_value = SimpleNamespace(
                total=500 * 1024**3, free=20 * 1024**3, used=480 * 1024**3,
            )
            last_check, last_alert = acw_control._check_disk_space(
                self._make_runtimes(["%1"]),
                last_check=0.0,
                last_alert=500.0,  # alerted 100s ago
                now=600.0,  # within 600s cooldown
            )

        self.assertEqual(last_alert, 500.0)  # unchanged — cooldown
        self.assertEqual(tmux_calls, [])

    def test_deduplicates_panes(self):
        tmux_calls = []

        def fake_run(cmd, **kwargs):
            tmux_calls.append(cmd)
            return SimpleNamespace(returncode=0)

        runtimes = {
            "thread-1": SimpleNamespace(pane="%1", paused=False),
            "thread-2": SimpleNamespace(pane="%1", paused=False),  # same pane
        }

        with unittest.mock.patch("acw_control.shutil.disk_usage") as mock_du, \
             unittest.mock.patch("acw_control.subprocess.run", fake_run):
            mock_du.return_value = SimpleNamespace(
                total=500 * 1024**3, free=20 * 1024**3, used=480 * 1024**3,
            )
            acw_control._check_disk_space(
                runtimes, last_check=0.0, last_alert=0.0, now=100.0,
            )

        # Only 2 calls (send-keys + C-m) despite 2 runtimes on same pane
        self.assertEqual(len(tmux_calls), 2)


if __name__ == "__main__":
    unittest.main()
