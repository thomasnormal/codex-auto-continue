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


if __name__ == "__main__":
    unittest.main()
