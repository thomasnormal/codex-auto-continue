import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sqlite3

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_codex  # noqa: E402


THREAD = "019cd7fb-f4c1-7613-bf98-4915dcb1970a"
TURN = "019cd7fb-f4cf-7512-8325-dae60d5294e4"


class AcwCodexUnitTests(unittest.TestCase):
    def tearDown(self):
        acw_codex.clear_thread_workdir_cache()

    def test_parse_codex_log_event_parses_task_close_line(self):
        line = (
            f'2026-03-10T13:42:21Z INFO session_loop{{thread_id={THREAD}}}:'
            f'submission_dispatch{{submission.id="{TURN}"}}:'
            f'turn{{otel.name="session_task.turn" thread.id={THREAD} turn.id={TURN} model=gpt-5.4}}: '
            "codex_core::tasks: close time.busy=23.7ms time.idle=2.04s"
        )
        self.assertEqual((THREAD, TURN, "false"), acw_codex.parse_codex_log_event(line))

    def test_discover_thread_for_pane_uses_exact_target_pane_pid(self):
        tmux_calls: list[list[str]] = []

        def fake_run_tmux(args, capture_output=True):
            tmux_calls.append(list(args))
            return subprocess.CompletedProcess(["tmux"], 0, "7777\n", "")

        with patch.object(acw_codex, "run_tmux", side_effect=fake_run_tmux):
            with patch.object(
                acw_codex.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["pstree"], 0, "bash(7777)---codex(222)\n", ""),
            ):
                with patch.object(
                    acw_codex,
                    "thread_from_codex_pid",
                    side_effect=lambda pid: THREAD if pid == "222" else None,
                ):
                    self.assertEqual(THREAD, acw_codex.discover_thread_for_pane("%11"))

        self.assertEqual(
            ["display-message", "-p", "-t", "%11", "#{pane_pid}"],
            tmux_calls[0],
        )

    def test_thread_from_codex_pid_accepts_proc_comm_name(self):
        def fake_readlink(path: str) -> str:
            if path == "/proc/222/exe":
                return "/usr/bin/python3"
            raise OSError(path)

        def fake_read_text(self: Path, encoding=None, errors=None) -> str:
            if str(self) == "/proc/222/comm":
                return "codex\n"
            raise OSError(str(self))

        with patch.object(acw_codex.os, "readlink", side_effect=fake_readlink):
            with patch.object(acw_codex.Path, "read_text", fake_read_text):
                with patch.object(acw_codex, "_thread_from_state_db_pid", return_value=THREAD):
                    self.assertEqual(THREAD, acw_codex.thread_from_codex_pid("222"))

    def test_thread_from_state_db_pid_prefers_newest_logs_db_mapping(self):
        newer_thread = "019cfffe-ec07-7b82-b11f-13070a79b958"
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)

            conn = sqlite3.connect(state_dir / "state_5.sqlite")
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
                    (THREAD, "pid:222:stale", 100, 1),
                )
                conn.commit()
            finally:
                conn.close()

            conn = sqlite3.connect(state_dir / "logs_1.sqlite")
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
                    (newer_thread, "pid:222:live", 200, 5),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                newer_thread,
                acw_codex._thread_from_state_db_pid(
                    "222",
                    state_dir,
                    process_start_epoch_fn=lambda _pid: 0.0,
                ),
            )

    def test_tmux_send_does_not_retype_prompt_after_enter_failure(self):
        calls = []

        def fake_run_tmux(args, capture_output=True):
            calls.append(list(args))
            failed = args[-1] == "C-m"
            return subprocess.CompletedProcess(args, int(failed), "", "Enter failed" if failed else "")

        result = acw_codex.tmux_send(
            "%11", "continue", 0,
            run_tmux_fn=fake_run_tmux,
            cancel_mode_if_needed_fn=lambda _pane: None,
        )
        self.assertEqual("error", result.status)
        self.assertIn("send-enter rc=1", result.detail)
        self.assertEqual([
            ["send-keys", "-t", "%11", "-l", "continue"],
            ["send-keys", "-t", "%11", "C-m"],
        ], calls)

    def test_tmux_send_preserves_text_sent_after_initial_write_retry(self):
        calls = []

        def fake_run_tmux(args, capture_output=True):
            calls.append(list(args))
            failed = len(calls) == 1 or args[-1] == "C-m"
            return subprocess.CompletedProcess(args, int(failed), "", "failed" if failed else "")

        result = acw_codex.tmux_send(
            "%11", "continue", 0,
            run_tmux_fn=fake_run_tmux,
            cancel_mode_if_needed_fn=lambda _pane: None,
        )
        self.assertEqual("error", result.status)
        self.assertTrue(result.text_sent)
        self.assertEqual(3, len(calls))

    def test_tmux_send_interrupts_after_text_before_enter(self):
        calls = []

        def fake_run_tmux(args, capture_output=True):
            calls.append((list(args), capture_output))
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(acw_codex, "run_tmux", side_effect=fake_run_tmux):
            with patch.object(acw_codex, "tmux_cancel_mode_if_needed"):
                with patch.object(acw_codex.time, "sleep"):
                    result = acw_codex.tmux_send(
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

    def test_thread_workdir_from_rollout_uses_latest_function_call_workdir(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            rollout_path = state_dir / "rollout.jsonl"
            rollout_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "payload": {
                                    "type": "function_call",
                                    "name": "exec_command",
                                    "arguments": json.dumps({"cmd": "pwd", "workdir": "/repo-one"}),
                                }
                            }
                        ),
                        json.dumps(
                            {
                                "payload": {
                                    "type": "function_call",
                                    "name": "exec_command",
                                    "arguments": json.dumps({"cmd": "git status", "workdir": "/repo-two"}),
                                }
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            conn = sqlite3.connect(state_dir / "state_test.sqlite")
            conn.execute(
                "create table threads (id text primary key, rollout_path text not null, updated_at integer not null)"
            )
            conn.execute(
                "insert into threads(id, rollout_path, updated_at) values (?, ?, ?)",
                (THREAD, str(rollout_path), 1),
            )
            conn.commit()
            conn.close()

            workdir = acw_codex.thread_workdir_from_rollout(THREAD, state_dir=state_dir, ttl_secs=0.0)

        self.assertEqual("/repo-two", workdir)

    def test_thread_workdir_from_rollout_returns_empty_when_no_workdir_found(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            rollout_path = state_dir / "rollout.jsonl"
            rollout_path.write_text(
                json.dumps({"payload": {"type": "message", "role": "assistant"}}) + "\n",
                encoding="utf-8",
            )
            conn = sqlite3.connect(state_dir / "state_test.sqlite")
            conn.execute(
                "create table threads (id text primary key, rollout_path text not null, updated_at integer not null)"
            )
            conn.execute(
                "insert into threads(id, rollout_path, updated_at) values (?, ?, ?)",
                (THREAD, str(rollout_path), 1),
            )
            conn.commit()
            conn.close()

            workdir = acw_codex.thread_workdir_from_rollout(THREAD, state_dir=state_dir, ttl_secs=0.0)

        self.assertEqual("", workdir)


if __name__ == "__main__":
    unittest.main()
