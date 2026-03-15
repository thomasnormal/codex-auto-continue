import unittest
from pathlib import Path

from test.support.manager_scenarios import ManagerScenarioMixin
from test.support.manager_scenarios import manager_scenario_backend_enabled
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


class _RealCodexHarnessTestCase(unittest.TestCase):
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


class RealCodexManagerScenarioTests(ManagerScenarioMixin, _RealCodexHarnessTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not manager_scenario_backend_enabled("real"):
            raise unittest.SkipTest("ACW manager scenarios are running without the real backend")


class RealCodexWatcherIntegrationTests(_RealCodexHarnessTestCase):
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

    def test_watcher_health_no_longer_uses_legacy_rollout_warnings(self):
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        self.harness.start_watcher(first_turn.thread_id)
        self.harness.wait_for_watcher_started(first_turn.thread_id)

        self.harness.send_codex_prompt("what is 2+2")
        watch_log = self.harness.wait_for_continue_sent()

        self.assertNotIn(
            "rollout",
            watch_log,
            self.harness.diagnostics(),
        )

    def test_watcher_skips_interrupted_turn_and_resumes_after_manual_prompt(self):
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        state_file = self.harness.start_watcher(first_turn.thread_id)
        self.harness.wait_for_watcher_started(first_turn.thread_id)

        self.harness.send_codex_prompt(
            "Write exactly 200 numbered bullet points about prime numbers, one short sentence each."
        )
        self.harness.wait_for_pane_contains("Working", timeout=30.0)
        self.harness.send_escape()
        watch_log = self.harness.wait_for_watch_log_contains(
            "skip: interrupted turn=",
            timeout=45.0,
        )
        self.assertIsNone(self.harness.watcher_proc.poll(), self.harness.diagnostics())
        self.assertIn("skip: interrupted turn=", watch_log)
        self.assertNotIn("continue: sent", watch_log, self.harness.diagnostics())

        self.harness.send_codex_prompt("say exactly RESUMED and nothing else")
        watch_log = self.harness.wait_for_continue_sent()
        pane_text = self.harness.wait_for_pane_contains("test continue", timeout=60.0)

        state_text = state_file.read_text(encoding="utf-8", errors="ignore")
        self.assertIn("continue: sent", watch_log)
        self.assertIn("skip: interrupted turn=", watch_log)
        self.assertNotIn("auto-paused:", state_text)
        self.assertIn("test continue", pane_text, self.harness.diagnostics())

    def test_manager_recovers_after_private_tmux_socket_recreation(self):
        self.harness.rename_window("socket")
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()

        self.harness.delete_tmux_socket()
        stale = self.harness.run_manager("start", "socket", "--message", "test continue", check=False)
        self.assertNotEqual(0, stale.returncode, self.harness.diagnostics())
        self.assertIn("kill -USR1", stale.stderr, self.harness.diagnostics())

        self.harness.recreate_tmux_socket()
        restored = self.harness.run_manager("start", "socket", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        self.assertIn("resolved: target=socket pane=", restored.stdout, self.harness.diagnostics())
        self.assertIn(first_turn.thread_id, restored.stdout, self.harness.diagnostics())


if __name__ == "__main__":
    unittest.main()
