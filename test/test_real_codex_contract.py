import unittest
from pathlib import Path

from test.support.real_codex_harness import RealCodexHarness
from test.support.real_codex_harness import require_real_codex_prereqs


ROOT = Path(__file__).resolve().parents[1]


def _test_failed(case: unittest.TestCase) -> bool:
    outcome = getattr(case, "_outcome", None)
    result = getattr(outcome, "result", None)
    if result is None:
        return False
    for test, _ in list(result.failures) + list(result.errors):
        if test.id() == case.id():
            return True
    return False


class RealCodexContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = require_real_codex_prereqs()
        if reason:
            raise unittest.SkipTest(reason)

    def setUp(self):
        self.harness = RealCodexHarness(ROOT)
        self.harness.start()

    def tearDown(self):
        if _test_failed(self):
            self.harness.archive_failure_artifacts(self.id())
        self.harness.cleanup()

    def test_real_codex_emits_supported_completion_signal(self):
        self.harness.start_codex("say the word hello and nothing else")
        turn = self.harness.wait_for_first_completed_turn()

        self.assertTrue(turn.thread_id, self.harness.diagnostics())
        self.assertTrue(
            turn.sources & {
                "codex_log_post_sampling",
                "codex_log_task_close",
            },
            self.harness.diagnostics(),
        )


if __name__ == "__main__":
    unittest.main()
