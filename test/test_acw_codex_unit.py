import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_codex  # noqa: E402


THREAD = "019cd7fb-f4c1-7613-bf98-4915dcb1970a"
TURN = "019cd7fb-f4cf-7512-8325-dae60d5294e4"


class AcwCodexUnitTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
