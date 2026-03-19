import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_ui  # noqa: E402


class AcwUiUnitTests(unittest.TestCase):
    def test_status_time_markup_dims_labels_and_average_line(self):
        self.assertEqual(
            "[dim]s:[/dim] 1d\n[dim]l:[/dim] 5m\n[dim]a: 20m[/dim]",
            acw_ui.status_time_markup("s: 1d\nl: 5m\na: 20m"),
        )

    def test_build_status_table_renderable_uses_combined_time_column(self):
        from rich.console import Console

        renderable = acw_ui.build_status_table_renderable(
            [
                (
                    "0:0:gaps/%0\n[dim]019cc20c…b9ea[/dim]\n[dim]id=1[/dim]",
                    "[green]busy[/green]",
                    "s: 1d\nl: 5m\na: 20m",
                    "Explored",
                    "continue",
                )
            ],
            section_every=3,
            max_msg_lines=3,
        )
        console = Console(record=True, force_terminal=True, color_system="standard", width=120)
        console.print(renderable)
        text = console.export_text(styles=False)

        self.assertIn("TIME", text)
        self.assertIn("LAST AGENT MESSAGE", text)
        self.assertIn("CONTINUE MESSAGE", text)
        self.assertNotIn("LAST_AGENT", text)
        self.assertNotIn("\nMESSAGE\n", text)
        self.assertNotIn("STARTED", text)
        self.assertNotIn("LAST_ACW", text)
        self.assertIn("s: 1d", text)
        self.assertIn("l: 5m", text)
        self.assertIn("a: 20m", text)

    def test_build_status_watch_renderable_omits_sessions_summary_line(self):
        from rich.console import Console

        renderable = acw_ui.build_status_watch_renderable(
            [
                (
                    "0:0:gaps/%0\n[dim]019cc20c…b9ea[/dim]\n[dim]id=1[/dim]",
                    "[green]busy[/green]",
                    "s: 1d\nl: 5m\na: 20m",
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

        self.assertIn("Ctrl-C: exit; e: edit; s: start; p: pause; u: resume; r: restart", text)
        self.assertNotIn("Sessions:", text)


if __name__ == "__main__":
    unittest.main()
