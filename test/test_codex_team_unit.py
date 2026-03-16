import unittest
from pathlib import Path
import shlex
import sys
import tempfile
from unittest.mock import call
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import codex_team


class CodexTeamUnitTests(unittest.TestCase):
    def test_extract_last_agent_block_returns_latest_block_between_prompts(self):
        text = (
            "OpenAI Codex\n"
            "› first request\n"
            "\n"
            "• first response\n"
            "\n"
            "› say exactly <message to=\"agent1\">hello</message> and nothing else\n"
            "\n"
            "• <message to=\"agent1\">hello</message>\n"
            "\n"
            "› \n"
        )

        self.assertEqual(
            "• <message to=\"agent1\">hello</message>",
            codex_team.extract_last_agent_block(text),
        )

    def test_parse_routed_messages_extracts_multiple_messages(self):
        block = (
            "• I am delegating work.\n"
            "<message to=\"agent1\">say exactly READY and nothing else</message>\n"
            "<message to=\"agent2\">\n"
            "inspect the failing tests\n"
            "</message>\n"
        )

        self.assertEqual(
            [
                ("agent1", "say exactly READY and nothing else"),
                ("agent2", "inspect the failing tests"),
            ],
            codex_team.parse_routed_messages(block),
        )

    def test_extract_last_agent_block_falls_back_without_codex_prompt_lines(self):
        text = (
            "[thomas-ahle@host repo]$ codex\n"
            "\n"
            "• boot line\n"
            "\n"
            "• <message to=\"agent1\">what is 40 + 2</message>\n"
            "\n"
            "\n"
        )

        self.assertEqual(
            "• <message to=\"agent1\">what is 40 + 2</message>",
            codex_team.extract_last_agent_block(text),
        )

    def test_validate_team_window_requires_single_pane(self):
        with self.assertRaisesRegex(ValueError, "current window already has 2 panes"):
            codex_team.validate_team_window("%1", ["%1", "%2"])

    def test_pending_completion_for_thread_accepts_tuple_parser_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "codex-tui.log"
            log_path.write_text("ignored\ncompletion line\n", encoding="utf-8")
            with patch.object(
                codex_team,
                "parse_codex_log_event",
                side_effect=[None, ("11111111-1111-1111-1111-111111111111", "turn-1", "false")],
            ):
                event = codex_team.pending_completion_for_thread(
                    "11111111-1111-1111-1111-111111111111",
                    "",
                    "",
                    log_path=log_path,
                )

        self.assertEqual(
            codex_team.CodexLogEvent(
                "completion",
                "11111111-1111-1111-1111-111111111111",
                "turn-1",
                "false",
            ),
            event,
        )

    def test_bootstrap_agents_launches_codex_with_initial_prompt_before_thread_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = codex_team.TeamState(
                team_id="team",
                tmux_socket="",
                window_id="@1",
                created_at=0.0,
                state_file=str(Path(tmpdir) / "state.json"),
                log_file=str(Path(tmpdir) / "team.log"),
                pid_file=str(Path(tmpdir) / "team.pid"),
                leader=codex_team.TeamAgent(
                    name="leader",
                    pane="%1",
                    role="leader",
                ),
                members=[
                    codex_team.TeamAgent(
                        name="agent1",
                        pane="%2",
                        role="member",
                    )
                ],
            )

            with patch.object(codex_team, "refresh_agent_threads", return_value=False), patch.object(
                codex_team, "pane_exists", return_value=True
            ), patch.object(
                codex_team, "pane_current_command", return_value="bash"
            ), patch.object(
                codex_team, "tmux_send"
            ) as mock_send:
                mock_send.return_value.status = "ok"
                mock_send.return_value.detail = ""
                changed = codex_team.bootstrap_agents(state)

        self.assertTrue(changed)
        expected_calls = [
            call(
                "%1",
                f"codex {shlex.quote(codex_team.leader_initial_prompt(['agent1']))}",
                codex_team.ENTER_DELAY_SECS,
            ),
            call(
                "%2",
                f"codex {shlex.quote(codex_team.worker_initial_prompt('agent1'))}",
                codex_team.ENTER_DELAY_SECS,
            ),
        ]
        self.assertEqual(expected_calls, mock_send.call_args_list)
        self.assertTrue(state.leader.initial_prompt_sent)
        self.assertTrue(state.leader.ignore_next_completion)
        self.assertTrue(state.leader.busy)
        self.assertTrue(state.members[0].initial_prompt_sent)
        self.assertTrue(state.members[0].ignore_next_completion)
        self.assertTrue(state.members[0].busy)

    def test_process_events_ignores_leader_bootstrap_completion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = codex_team.TeamState(
                team_id="team",
                tmux_socket="",
                window_id="@1",
                created_at=0.0,
                state_file=str(Path(tmpdir) / "state.json"),
                log_file=str(Path(tmpdir) / "team.log"),
                pid_file=str(Path(tmpdir) / "team.pid"),
                leader=codex_team.TeamAgent(
                    name="leader",
                    pane="%1",
                    role="leader",
                    thread_id="11111111-1111-1111-1111-111111111111",
                    initial_prompt_sent=True,
                    ignore_next_completion=True,
                    busy=True,
                ),
                members=[
                    codex_team.TeamAgent(
                        name="agent1",
                        pane="%2",
                        role="member",
                        thread_id="22222222-2222-2222-2222-222222222222",
                        initial_prompt_sent=True,
                    )
                ],
            )
            event = codex_team.CodexLogEvent(
                "completion",
                "11111111-1111-1111-1111-111111111111",
                "turn-1",
                "false",
            )

            with patch.object(
                codex_team,
                "capture_pane_text",
                return_value='[shell]$ codex\n\n• <message to="agent1">what is 40 + 2</message>\n',
            ):
                changed = codex_team.process_events(state, [event])

        self.assertTrue(changed)
        self.assertTrue(state.leader.ready)
        self.assertFalse(state.leader.busy)
        self.assertFalse(state.leader.ignore_next_completion)
        self.assertEqual([], state.members[0].pending_messages)

    def test_deliver_pending_sends_even_when_agent_is_busy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = codex_team.TeamAgent(
                name="agent1",
                pane="%2",
                role="member",
                initial_prompt_sent=True,
                busy=True,
                ready=False,
                pending_messages=["say exactly 42 and nothing else"],
            )
            log_path = Path(tmpdir) / "team.log"

            with patch.object(codex_team, "tmux_send") as mock_send:
                mock_send.return_value.status = "ok"
                mock_send.return_value.detail = ""
                changed = codex_team.deliver_pending(agent, log_path)

        self.assertTrue(changed)
        self.assertEqual([], agent.pending_messages)
        self.assertTrue(agent.busy)
        self.assertFalse(agent.ready)
        mock_send.assert_called_once_with("%2", "say exactly 42 and nothing else", codex_team.ENTER_DELAY_SECS)

    def test_cmd_down_keeps_current_pane_and_kills_other_window_panes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "team.json"
            pid_path = Path(tmpdir) / "team.pid"
            state = codex_team.TeamState(
                team_id="team",
                tmux_socket="",
                window_id="@1",
                created_at=0.0,
                state_file=str(state_path),
                log_file=str(Path(tmpdir) / "team.log"),
                pid_file=str(pid_path),
                monitor_pid=1234,
                leader=codex_team.TeamAgent(name="leader", pane="%1", role="leader"),
                members=[
                    codex_team.TeamAgent(name="agent1", pane="%2", role="member"),
                    codex_team.TeamAgent(name="agent2", pane="%3", role="member"),
                ],
            )
            codex_team.save_team_state(state)
            pid_path.write_text("1234\n", encoding="utf-8")

            with patch.object(codex_team, "current_tmux_pane", return_value="%2"), patch.object(
                codex_team, "current_window_state_path", return_value=state_path
            ), patch.object(
                codex_team, "pid_is_alive", return_value=False
            ), patch.object(
                codex_team, "list_window_panes", return_value=["%1", "%2", "%3"]
            ), patch.object(
                codex_team, "run_tmux"
            ) as mock_run_tmux:
                rc = codex_team.cmd_down()

        self.assertEqual(0, rc)
        self.assertFalse(state_path.exists())
        self.assertFalse(pid_path.exists())
        self.assertEqual(
            [
                call("kill-pane", "-t", "%1"),
                call("kill-pane", "-t", "%3"),
            ],
            mock_run_tmux.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
