import unittest
from pathlib import Path
import sys

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


if __name__ == "__main__":
    unittest.main()
