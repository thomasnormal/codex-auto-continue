import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_state  # noqa: E402


THREAD = "019cd7fb-f4c1-7613-bf98-4915dcb1970a"


class AcwStateUnitTests(unittest.TestCase):
    def test_write_session_state_persists_continue_count(self):
        with tempfile.TemporaryDirectory() as td:
            acw_state.write_session_state(
                THREAD,
                {
                    "thread_id": THREAD,
                    "message": "continue",
                    "continue_count": 7,
                },
                state_dir=td,
            )

            state = acw_state.read_session_state(THREAD, state_dir=td)

        self.assertEqual(7, state["continue_count"])


if __name__ == "__main__":
    unittest.main()
