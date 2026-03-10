import unittest
from pathlib import Path

from test.support.real_codex_harness import RealCodexHarness
from test.support.real_codex_harness import require_real_codex_prereqs


ROOT = Path(__file__).resolve().parents[1]


class RealCodexWatcherIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = require_real_codex_prereqs()
        if reason:
            raise unittest.SkipTest(reason)

    def setUp(self):
        self.harness = RealCodexHarness(ROOT)
        self.harness.start()

    def tearDown(self):
        self.harness.cleanup()

    def test_watcher_sends_continue_for_second_real_codex_turn(self):
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        state_file = self.harness.start_watcher(first_turn.thread_id)
        self.harness.wait_for_watcher_started(first_turn.thread_id)

        self.harness.send_codex_prompt("what is 1+1")
        watch_log = self.harness.wait_for_continue_sent()
        pane_text = self.harness.capture_pane()

        self.assertIn("continue: sent", watch_log, self.harness.diagnostics())
        self.assertTrue(state_file.is_file(), self.harness.diagnostics())
        self.assertIn("test continue", pane_text, self.harness.diagnostics())

    def test_watcher_does_not_report_rollout_channel_closed_as_error(self):
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        self.harness.start_watcher(first_turn.thread_id)
        self.harness.wait_for_watcher_started(first_turn.thread_id)

        self.harness.send_codex_prompt("what is 2+2")
        watch_log = self.harness.wait_for_continue_sent()

        self.assertNotIn(
            "health: error - rollout channel closed",
            watch_log,
            self.harness.diagnostics(),
        )


if __name__ == "__main__":
    unittest.main()
