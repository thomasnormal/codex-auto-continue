import tempfile
import unittest
from pathlib import Path
import sqlite3
import os
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import auto_continue_logwatch as logwatch  # noqa: E402


THREAD = "019cd7fb-f4c1-7613-bf98-4915dcb1970a"
TURN = "019cd7fb-f4cf-7512-8325-dae60d5294e4"


class LogwatchUnitTests(unittest.TestCase):
    def test_thread_from_state_db_pid_uses_process_uuid_mapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chmod(tmpdir, 0o700)
            db_path = Path(tmpdir) / "state_5.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "create table logs ("
                    "thread_id text, "
                    "process_uuid text, "
                    "ts integer, "
                    "ts_nanos integer)"
                )
                conn.execute(
                    "insert into logs(thread_id, process_uuid, ts, ts_nanos) values (?, ?, ?, ?)",
                    ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "pid:111:old", 1, 1),
                )
                conn.execute(
                    "insert into logs(thread_id, process_uuid, ts, ts_nanos) values (?, ?, ?, ?)",
                    (THREAD, "pid:222:live", 10, 5),
                )
                conn.execute(
                    "insert into logs(thread_id, process_uuid, ts, ts_nanos) values (?, ?, ?, ?)",
                    ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "pid:333:other", 20, 1),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                THREAD,
                logwatch._thread_from_state_db_pid("222", Path(tmpdir)),
            )

    def test_parse_codex_log_event_parses_post_sampling_line(self):
        line = (
            f"2026-03-10 INFO session_loop{{thread_id={THREAD}}}: codex_core::codex: "
            f"post sampling token usage turn_id={TURN} total_usage_tokens=1 needs_follow_up=false"
        )
        self.assertEqual((THREAD, TURN, "false"), logwatch.parse_codex_log_event(line))

    def test_parse_codex_log_event_parses_task_close_line(self):
        line = (
            f'2026-03-10T13:42:21Z INFO session_loop{{thread_id={THREAD}}}:'
            f'submission_dispatch{{submission.id="{TURN}"}}:'
            f'turn{{otel.name="session_task.turn" thread.id={THREAD} turn.id={TURN} model=gpt-5.4}}: '
            "codex_core::tasks: close time.busy=23.7ms time.idle=2.04s"
        )
        self.assertEqual((THREAD, TURN, "false"), logwatch.parse_codex_log_event(line))

    def test_discover_thread_id_uses_pane_local_discovery(self):
        line = (
            f'2026-03-10T13:42:21Z INFO session_loop{{thread_id={THREAD}}}:'
            f'submission_dispatch{{submission.id="{TURN}"}}:'
            f'turn{{otel.name="session_task.turn" thread.id={THREAD} turn.id={TURN} model=gpt-5.4}}: '
            "codex_core::tasks: close time.busy=23.7ms time.idle=2.04s"
        )
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as f:
            f.write(line + "\n")
            log_path = Path(f.name)
        try:
            with patch.object(logwatch, "discover_thread_for_pane", return_value=THREAD):
                self.assertEqual(THREAD, logwatch.discover_thread_id(log_path, pane="%11"))
        finally:
            log_path.unlink(missing_ok=True)

    def test_discover_thread_id_does_not_fall_back_to_global_log_when_pane_discovery_fails(self):
        line = (
            f'2026-03-10T13:42:21Z INFO session_loop{{thread_id={THREAD}}}:'
            f'submission_dispatch{{submission.id="{TURN}"}}:'
            f'turn{{otel.name="session_task.turn" thread.id={THREAD} turn.id={TURN} model=gpt-5.4}}: '
            "codex_core::tasks: close time.busy=23.7ms time.idle=2.04s"
        )
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as f:
            f.write(line + "\n")
            log_path = Path(f.name)
        try:
            with patch.object(logwatch, "discover_thread_for_pane", return_value=None):
                self.assertIsNone(logwatch.discover_thread_id(log_path, pane="%11"))
        finally:
            log_path.unlink(missing_ok=True)

    def test_discover_thread_id_does_not_fall_back_to_latest_rollout_when_pane_discovery_fails(self):
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as f:
            log_path = Path(f.name)
        try:
            with patch.object(logwatch, "discover_thread_for_pane", return_value=None):
                with patch.object(
                    logwatch,
                    "find_latest_rollout",
                    side_effect=AssertionError("global rollout fallback should not run"),
                ):
                    self.assertIsNone(logwatch.discover_thread_id(log_path, pane="%11"))
        finally:
            log_path.unlink(missing_ok=True)

    def test_compute_health_warns_when_rollout_channel_closed_but_codex_log_works(self):
        health, detail = logwatch.compute_health(
            watched_thread=THREAD,
            rollout_path=None,
            watcher_start=0.0,
            now=120.0,
            rollout_channel_closed=True,
            rollout_channel_closed_at=100.0,
            codex_log_completion_seen=True,
        )
        self.assertEqual("warn", health)
        self.assertEqual("rollout channel closed; using codex log", detail)

    def test_compute_health_errors_when_rollout_channel_closed_before_codex_signal(self):
        health, detail = logwatch.compute_health(
            watched_thread=THREAD,
            rollout_path=None,
            watcher_start=0.0,
            now=120.0,
            rollout_channel_closed=True,
            rollout_channel_closed_at=100.0,
            codex_log_completion_seen=False,
        )
        self.assertEqual("error", health)
        self.assertEqual("rollout channel closed", detail)

    def test_compute_health_ok_when_codex_log_completion_seen_without_rollout(self):
        health, detail = logwatch.compute_health(
            watched_thread=THREAD,
            rollout_path=None,
            watcher_start=0.0,
            now=120.0,
            rollout_channel_closed=False,
            rollout_channel_closed_at=0.0,
            codex_log_completion_seen=True,
        )
        self.assertEqual("ok", health)
        self.assertEqual("", detail)

    def test_compute_health_grace_period_avoids_spurious_error(self):
        health, detail = logwatch.compute_health(
            watched_thread=THREAD,
            rollout_path=None,
            watcher_start=0.0,
            now=102.0,
            rollout_channel_closed=True,
            rollout_channel_closed_at=100.0,
            codex_log_completion_seen=False,
        )
        self.assertEqual("ok", health)
        self.assertEqual("", detail)


if __name__ == "__main__":
    unittest.main()
