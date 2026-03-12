import tempfile
import unittest
from pathlib import Path
import sqlite3
import os
from unittest.mock import patch
import subprocess

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import auto_continue_logwatch as logwatch  # noqa: E402


THREAD = "019cd7fb-f4c1-7613-bf98-4915dcb1970a"
TURN = "019cd7fb-f4cf-7512-8325-dae60d5294e4"


class LogwatchUnitTests(unittest.TestCase):
    def test_thread_times_from_state_db_reads_threads_and_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chmod(tmpdir, 0o700)
            db_path = Path(tmpdir) / "state_5.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "create table threads ("
                    "id text, "
                    "created_at integer, "
                    "updated_at integer)"
                )
                conn.execute(
                    "create table logs ("
                    "thread_id text, "
                    "process_uuid text, "
                    "ts integer, "
                    "ts_nanos integer)"
                )
                conn.execute(
                    "insert into threads(id, created_at, updated_at) values (?, ?, ?)",
                    (THREAD, 100, 140),
                )
                conn.execute(
                    "insert into logs(thread_id, process_uuid, ts, ts_nanos) values (?, ?, ?, ?)",
                    (THREAD, "pid:222:live", 150, 5),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                (100, 150),
                logwatch.thread_times_from_state_db(THREAD, Path(tmpdir)),
            )

    def test_thread_times_from_state_db_falls_back_to_logs_when_threads_row_missing(self):
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
                    (THREAD, "pid:222:live", 120, 0),
                )
                conn.execute(
                    "insert into logs(thread_id, process_uuid, ts, ts_nanos) values (?, ?, ?, ?)",
                    (THREAD, "pid:222:live", 180, 0),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                (120, 180),
                logwatch.thread_times_from_state_db(THREAD, Path(tmpdir)),
            )

    def test_thread_from_state_db_cwd_uses_nearest_thread_creation_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chmod(tmpdir, 0o700)
            db_path = Path(tmpdir) / "state_5.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "create table threads ("
                    "id text, "
                    "created_at integer, "
                    "updated_at integer, "
                    "cwd text)"
                )
                conn.execute(
                    "insert into threads(id, created_at, updated_at, cwd) values (?, ?, ?, ?)",
                    ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 200, 210, "/repo"),
                )
                conn.execute(
                    "insert into threads(id, created_at, updated_at, cwd) values (?, ?, ?, ?)",
                    (THREAD, 390, 420, "/repo"),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                THREAD,
                logwatch._thread_from_state_db_cwd("/repo", 392.0, Path(tmpdir)),
            )

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

    def test_thread_from_state_db_pid_ignores_rows_older_than_process_start(self):
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
                    ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "pid:222:stale", 50, 0),
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(logwatch, "_process_start_epoch", return_value=100.0):
                self.assertIsNone(logwatch._thread_from_state_db_pid("222", Path(tmpdir)))

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

    def test_discover_thread_id_returns_none_when_pane_discovery_fails(self):
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as f:
            log_path = Path(f.name)
        try:
            with patch.object(logwatch, "discover_thread_for_pane", return_value=None):
                self.assertIsNone(logwatch.discover_thread_id(log_path, pane="%11"))
        finally:
            log_path.unlink(missing_ok=True)

    def test_check_codex_log_tail_for_pending_returns_latest_pending_completion(self):
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as f:
            f.write("2026-03-10T13:42:00Z INFO session_loop{thread_id=other}: noise\n")
            f.write(
                f'2026-03-10T13:42:21Z INFO session_loop{{thread_id={THREAD}}}:'
                f'submission_dispatch{{submission.id="{TURN}"}}:'
                f'turn{{otel.name="session_task.turn" thread.id={THREAD} turn.id={TURN} model=gpt-5.4}}: '
                "codex_core::tasks: close time.busy=23.7ms time.idle=2.04s\n"
            )
            log_path = Path(f.name)
        try:
            with patch.object(logwatch, "append_log") as append_log:
                pending = logwatch.check_codex_log_tail_for_pending(
                    log_path,
                    THREAD,
                    "",
                    "",
                    Path("/tmp/watch.log"),
                )
            self.assertEqual((THREAD, TURN, "false"), pending)
            append_log.assert_called_once()
        finally:
            log_path.unlink(missing_ok=True)

    def test_check_codex_log_tail_for_pending_skips_if_thread_has_newer_activity(self):
        newer_line = (
            f"2026-03-10T13:42:22Z INFO session_loop{{thread_id={THREAD}}}: "
            'codex_core::stream_events_utils: ToolCall: exec_command {"cmd":"echo hi"} '
            f"thread_id={THREAD}"
        )
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as f:
            f.write(
                f'2026-03-10T13:42:21Z INFO session_loop{{thread_id={THREAD}}}:'
                f'submission_dispatch{{submission.id="{TURN}"}}:'
                f'turn{{otel.name="session_task.turn" thread.id={THREAD} turn.id={TURN} model=gpt-5.4}}: '
                "codex_core::tasks: close time.busy=23.7ms time.idle=2.04s\n"
            )
            f.write(newer_line + "\n")
            log_path = Path(f.name)
        try:
            pending = logwatch.check_codex_log_tail_for_pending(
                log_path,
                THREAD,
                "",
                "",
                Path("/tmp/watch.log"),
            )
            self.assertIsNone(pending)
        finally:
            log_path.unlink(missing_ok=True)

    def test_check_pane_for_errors_matches_conversation_interrupted(self):
        text = (
            "■ Conversation interrupted - tell the model what to do differently.\n"
            "Something went wrong? Hit /feedback to report the issue.\n"
        )
        with patch.object(logwatch, "tmux_capture_pane", return_value=text):
            reason = logwatch.check_pane_for_errors("%11")
        self.assertEqual("Conversation interrupted", reason)

    def test_check_pane_for_errors_matches_model_interrupted_banner(self):
        with patch.object(
            logwatch,
            "tmux_capture_pane",
            return_value="• Model interrupted to submit steer instructions.\n",
        ):
            reason = logwatch.check_pane_for_errors("%11")
        self.assertEqual("Model interrupted to submit steer instructions", reason)

    def test_auto_pause_current_watcher_stops_process_for_pane_error(self):
        state = {"thread_id": THREAD, "message": "continue"}
        with patch.object(logwatch, "append_log") as append_log:
            with patch.object(logwatch, "write_state") as write_state:
                with patch.object(logwatch.os, "kill") as kill:
                    with patch.object(logwatch, "now_ts", return_value="2026-03-11 12:34:56"):
                        logwatch.auto_pause_current_watcher(
                            "Conversation interrupted",
                            Path("/tmp/acw.log"),
                            Path("/tmp/acw.json"),
                            state,
                        )
        append_log.assert_called_once()
        write_state.assert_called_once()
        written_state = write_state.call_args.args[1]
        self.assertEqual("auto-paused: Conversation interrupted", written_state["health_detail"])
        self.assertEqual("2026-03-11 12:34:56", written_state["health_ts"])
        kill.assert_called_once_with(logwatch.os.getpid(), logwatch.signal.SIGSTOP)

    def test_tmux_send_interrupts_after_text_before_enter(self):
        calls = []

        def fake_run_tmux(args, capture_output=True):
            calls.append((list(args), capture_output))
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(logwatch, "run_tmux", side_effect=fake_run_tmux):
            with patch.object(logwatch, "tmux_cancel_mode_if_needed"):
                with patch.object(logwatch.time, "sleep"):
                    result = logwatch.tmux_send(
                        "%11",
                        "continue",
                        0.1,
                        interrupt_checker=lambda: "Conversation interrupted",
                    )

        self.assertEqual("interrupted", result.status)
        self.assertEqual("Conversation interrupted", result.detail)
        send_key_calls = [cmd for cmd, _ in calls if cmd[:3] == ["send-keys", "-t", "%11"]]
        self.assertIn(["send-keys", "-t", "%11", "-l", "continue"], send_key_calls)
        self.assertFalse(any(cmd[-1] == "C-m" for cmd in send_key_calls))

    def test_run_tmux_falls_back_when_explicit_socket_is_stale(self):
        calls = []

        def fake_run(cmd, capture_output=None, text=None, check=None, env=None):
            calls.append((cmd, env))
            if len(calls) == 1:
                return subprocess.CompletedProcess(cmd, 1, "", "stale socket")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        env = {
            "AUTO_CONTINUE_TMUX_SOCKET": "/tmp/tmux-test/socket",
            "TMUX": "/tmp/tmux-test/socket,1,0",
            "TMUX_PANE": "%9",
            "PATH": "/usr/bin",
        }
        with patch.dict(logwatch.os.environ, env, clear=True):
            with patch.object(logwatch.subprocess, "run", side_effect=fake_run):
                rc = logwatch.run_tmux(["list-sessions"])

        self.assertEqual(0, rc.returncode)
        self.assertEqual(["tmux", "-S", "/tmp/tmux-test/socket", "list-sessions"], calls[0][0])
        self.assertEqual(["tmux", "list-sessions"], calls[1][0])
        self.assertNotIn("TMUX", calls[1][1])
        self.assertNotIn("TMUX_PANE", calls[1][1])

    def test_compute_health_warns_when_waiting_for_thread_id_too_long(self):
        health, detail = logwatch.compute_health(
            watched_thread=THREAD,
            watcher_start=0.0,
            now=120.0,
            codex_log_exists=True,
        )
        self.assertEqual("ok", health)
        self.assertEqual("", detail)

    def test_compute_health_warns_when_thread_is_still_unknown(self):
        health, detail = logwatch.compute_health(
            watched_thread="",
            watcher_start=0.0,
            now=120.0,
            codex_log_exists=True,
        )
        self.assertEqual("warn", health)
        self.assertEqual("waiting for thread id", detail)

    def test_compute_health_warns_when_codex_log_is_missing(self):
        health, detail = logwatch.compute_health(
            watched_thread=THREAD,
            watcher_start=0.0,
            now=120.0,
            codex_log_exists=False,
        )
        self.assertEqual("warn", health)
        self.assertEqual("codex log not found", detail)

    def test_compute_health_ok_during_startup_grace(self):
        health, detail = logwatch.compute_health(
            watched_thread=THREAD,
            watcher_start=100.0,
            now=120.0,
            codex_log_exists=False,
        )
        self.assertEqual("ok", health)
        self.assertEqual("", detail)


if __name__ == "__main__":
    unittest.main()
