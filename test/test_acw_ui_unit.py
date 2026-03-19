import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_ui  # noqa: E402


class AcwUiUnitTests(unittest.TestCase):
    def test_build_status_watch_renderable_omits_sessions_summary_line(self):
        from rich.console import Console

        renderable = acw_ui.build_status_watch_renderable(
            [
                (
                    "0:0:gaps/%0\n[dim]019cc20c…b9ea[/dim]\n[dim]id=1[/dim]",
                    "[green]busy[/green]",
                    "1d",
                    "5m",
                    "Explored",
                    "continue",
                )
            ],
            section_every=3,
            max_msg_lines=3,
            recommendations=[],
            invariant_messages=[],
        )
        console = Console(record=True, force_terminal=True, color_system="standard", width=120)
        console.print(renderable)
        text = console.export_text(styles=False)

        self.assertIn("Watching acw status. Press Ctrl-C to exit.", text)
        self.assertNotIn("Sessions:", text)


if __name__ == "__main__":
    unittest.main()
