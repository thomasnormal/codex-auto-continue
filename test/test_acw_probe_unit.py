import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_probe  # noqa: E402


class AcwProbeUnitTests(unittest.TestCase):
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

        self.assertEqual("waiting for input", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_from_pane_text_detects_thinking_working(self):
        text = (
            "› Investigate the failure\n"
            "\n"
            "working...\n"
            "\n"
            "  gpt-5.4 xhigh · 99% left · ~\n"
        )

        self.assertEqual("thinking/working", acw_probe.paused_activity_from_pane_text(text))


if __name__ == "__main__":
    unittest.main()
