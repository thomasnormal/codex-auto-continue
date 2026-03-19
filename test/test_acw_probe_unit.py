import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_probe  # noqa: E402


class AcwProbeUnitTests(unittest.TestCase):
    def test_paused_activity_from_pane_text_detects_directory_trust_prompt_as_input(self):
        text = (
            "> You are in /home/thomas-ahle/codex-auto-continue\n"
            "\n"
            "  Do you trust the contents of this directory? Working with untrusted contents comes with "
            "higher risk of prompt injection.\n"
            "\n"
            "› 1. Yes, continue\n"
            "  2. No, quit\n"
            "\n"
            "  Press enter to continue\n"
            "\n"
            "╭──────────────────────────────────────────────────╮\n"
            "│ >_ OpenAI Codex (v0.114.0)                       │\n"
            "╰──────────────────────────────────────────────────╯\n"
        )

        self.assertEqual("input", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_from_pane_text_detects_tool_approval_prompt_as_input(self):
        text = (
            "Reviewing approval request\n"
            "\n"
            "Tool call needs your approval. Reason: network access outside the allowlist.\n"
            "Do you want to approve network access to \"https://example.com\"?\n"
            "\n"
            "› 1. Yes, allow\n"
            "  2. No, continue without permissions\n"
            "\n"
            "  ctrl + p / ctrl + n change field\n"
        )

        self.assertEqual("input", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_from_pane_text_detects_waiting_for_input(self):
        text = (
            "Running scope as unit: run-r45f69361fa3c4de882dca4bdf4996f6b.scope\n"
            "╭─────────────────────────────────────────────╮\n"
            "│ >_ OpenAI Codex (v0.114.0)                  │\n"
            "╰─────────────────────────────────────────────╯\n"
            "\n"
            "› Use /skills to list available skills\n"
            "\n"
            "  gpt-5.4 xhigh · 100% left · ~\n"
        )

        self.assertEqual("waiting", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_from_pane_text_detects_thinking_working(self):
        text = (
            "› Investigate the failure\n"
            "\n"
            "working...\n"
            "\n"
            "  gpt-5.4 xhigh · 99% left · ~\n"
        )

        self.assertEqual("busy", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_prefers_working_over_queued_prompt(self):
        text = (
            "• The detached #0 tracker is green now on today’s CIRCT build.\n"
            "\n"
            "• Explored\n"
            "  └ Read uvm-report-message-empty-elements-runtime.sv\n"
            "\n"
            "• Working (53m 10s • esc to interrupt)\n"
            "\n"
            "\n"
            "› Explain this codebase\n"
            "\n"
            "  gpt-5.4 xhigh · 6% left · ~/circt\n"
        )

        self.assertEqual("busy", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_keeps_scanning_above_prompt_for_busy_indicator(self):
        text = (
            "• Both phase-hopper locals are already stale on today’s build.\n"
            "\n"
            "◦ Waiting for background terminal (1h 17m 17s • esc to interrupt) · "
            "1 background terminal running · /ps to view · /clean to close\n"
            "  └ set -euo pipefail…\n"
            "\n"
            "› Explain this codebase\n"
            "\n"
            "  gpt-5.4 xhigh · 45% left · ~/circt\n"
        )

        self.assertEqual("busy", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_ignores_stale_working_line_once_prompt_returns(self):
        text = (
            "working...\n"
            "\n"
            "• RUNWAIT\n"
            "\n"
            "›\n"
        )

        self.assertEqual("waiting", acw_probe.paused_activity_from_pane_text(text))

    def test_extract_last_agent_snippet_ignores_approval_prompt_chrome(self):
        text = (
            "• The previous real agent update.\n"
            "\n"
            "Reviewing approval request\n"
            "Tool call needs your approval. Reason: network access outside the allowlist.\n"
            "› 1. Yes, allow\n"
            "  2. No, continue without permissions\n"
        )

        self.assertEqual("The previous real agent update.", acw_probe.extract_last_agent_snippet(text))


if __name__ == "__main__":
    unittest.main()
