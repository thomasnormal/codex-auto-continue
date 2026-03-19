import io
import unittest

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_bootstrap  # noqa: E402


class AcwBootstrapUnitTests(unittest.TestCase):
    def test_main_defaults_to_watch_when_stdout_is_a_tty(self):
        calls: list[tuple[str, list[str]]] = []

        acw_bootstrap.main(
            argv=["acw"],
            cleanup_stale_files_fn=lambda: calls.append(("cleanup", [])),
            print_help_fn=lambda **_kwargs: calls.append(("help", [])),
            commands={
                "watch": lambda argv: calls.append(("watch", list(argv))),
                "status": lambda argv: calls.append(("status", list(argv))),
            },
            stdout_isatty_fn=lambda: True,
        )

        self.assertEqual([("watch", [])], calls)

    def test_main_defaults_to_status_when_stdout_is_not_a_tty(self):
        calls: list[tuple[str, list[str]]] = []

        acw_bootstrap.main(
            argv=["acw"],
            cleanup_stale_files_fn=lambda: calls.append(("cleanup", [])),
            print_help_fn=lambda **_kwargs: calls.append(("help", [])),
            commands={
                "watch": lambda argv: calls.append(("watch", list(argv))),
                "status": lambda argv: calls.append(("status", list(argv))),
            },
            stdout_isatty_fn=lambda: False,
        )

        self.assertEqual([("status", [])], calls)

    def test_build_help_text_mentions_watch_command(self):
        text = acw_bootstrap.build_help_text("acw")

        self.assertIn("acw watch [target]", text)
        self.assertIn("watch     Continuously refresh managed session state", text)


if __name__ == "__main__":
    unittest.main()
