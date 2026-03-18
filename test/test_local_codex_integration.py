import time
import unittest
from pathlib import Path

from test.support.local_codex_harness import LocalCodexHarness
from test.support.local_codex_harness import require_local_codex_prereqs
from test.support.manager_scenarios import ManagerScenarioMixin
from test.support.manager_scenarios import manager_scenario_backend_enabled


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


class _LocalCodexHarnessTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = require_local_codex_prereqs()
        if reason:
            raise unittest.SkipTest(reason)

    def setUp(self):
        self.harness = LocalCodexHarness(ROOT)
        self.harness.start()

    def tearDown(self):
        if _test_failed(self):
            self.harness.archive_failure_artifacts(self.id())
        self.harness.cleanup()


class LocalCodexManagerScenarioTests(ManagerScenarioMixin, _LocalCodexHarnessTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not manager_scenario_backend_enabled("local"):
            raise unittest.SkipTest("ACW manager scenarios are running without the local backend")


class LocalCodexCliIntegrationTests(_LocalCodexHarnessTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not manager_scenario_backend_enabled("local"):
            raise unittest.SkipTest("ACW local integration tests are running without the local backend")

    def test_pause_rejects_star_selector_via_real_cli(self):
        proc = self.harness.run_manager("pause", "*", check=False)

        self.assertEqual(2, proc.returncode, self.harness.diagnostics())
        self.assertIn("use 'acw pause' with no target", proc.stderr, self.harness.diagnostics())

    def test_resume_rejects_star_selector_via_real_cli(self):
        proc = self.harness.run_manager("resume", "*", check=False)

        self.assertEqual(2, proc.returncode, self.harness.diagnostics())
        self.assertIn("use 'acw resume' with no target", proc.stderr, self.harness.diagnostics())

    def test_restart_rejects_star_selector_via_real_cli(self):
        proc = self.harness.run_manager("restart", "*", check=False)

        self.assertEqual(2, proc.returncode, self.harness.diagnostics())
        self.assertIn("use 'acw restart' with no target", proc.stderr, self.harness.diagnostics())

    def test_doctor_rejects_explicit_dot_target_via_real_cli(self):
        proc = self.harness.run_manager("doctor", ".", check=False)

        self.assertEqual(1, proc.returncode, self.harness.diagnostics())
        self.assertIn("could not resolve target '.'", proc.stdout, self.harness.diagnostics())

    def test_status_recommends_doctor_for_warn_runtime_via_real_cli(self):
        self.harness.rename_window("warnpane")
        self.harness.start_codex("say exactly WARNSTATUS and nothing else")
        turn = self.harness.wait_for_first_completed_turn()
        pane = self.harness.pane_id
        self.harness.run_manager("start", "warnpane", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        self.harness.mutate_manager_session_state(
            turn.thread_id,
            health="warn",
            health_detail="codex log missing",
        )

        proc = self.harness.run_manager("status")

        self.assertIn(f"Recommendation: run acw doctor {pane}", proc.stdout, self.harness.diagnostics())

    def test_doctor_recommends_restart_for_warn_runtime_via_real_cli(self):
        self.harness.rename_window("warnpane")
        self.harness.start_codex("say exactly WARNDOCTOR and nothing else")
        turn = self.harness.wait_for_first_completed_turn()
        self.harness.run_manager("start", "warnpane", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        self.harness.mutate_manager_session_state(
            turn.thread_id,
            health="warn",
            health_detail="rollout channel closed",
        )

        proc = self.harness.run_manager("doctor", "warnpane")

        self.assertEqual(0, proc.returncode, self.harness.diagnostics())
        self.assertIn("runtime health: warn - rollout channel closed", proc.stdout, self.harness.diagnostics())
        self.assertIn("Recommendation: run acw restart warnpane", proc.stdout, self.harness.diagnostics())

    def test_status_shows_waiting_for_input_for_paused_runtime_via_real_cli(self):
        self.harness.rename_window("pausedwait")
        self.harness.start_codex("say exactly PAUSEDWAIT and nothing else")
        turn = self.harness.wait_for_first_completed_turn()
        pane = self.harness.pane_id

        self.harness.run_manager("start", "pausedwait", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        self.harness.run_manager("pause", "pausedwait")
        self.harness.wait_for_manager_watcher_paused_in_pane(pane)

        status = self.harness.run_manager("status")

        self.assertIn("waiting", status.stdout, self.harness.diagnostics())

    def test_status_shows_thinking_working_for_paused_busy_runtime_via_real_cli(self):
        self.harness.rename_window("pausedbusy")
        self.harness.start_codex("say exactly PAUSEDBUSY and nothing else")
        turn = self.harness.wait_for_first_completed_turn()
        pane = self.harness.pane_id

        self.harness.run_manager("start", "pausedbusy", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        self.harness.send_codex_prompt("sleep 3 seconds then say exactly BUSYDONE and nothing else")
        self.harness.wait_for_pane_contains("working...", timeout=10.0)
        time.sleep(0.1)
        self.harness.run_manager("pause", "pausedbusy")
        self.harness.wait_for_manager_watcher_paused_in_pane(pane)

        status = self.harness.run_manager("status")

        self.assertIn("thinking/working", status.stdout, self.harness.diagnostics())
        self.harness.wait_for_pane_contains("BUSYDONE", timeout=30.0)


if __name__ == "__main__":
    unittest.main()
