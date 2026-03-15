import unittest

from pathlib import Path

from test.support.manager_scenarios import ManagerScenarioMixin
from test.support.manager_scenarios import manager_scenario_backend_enabled
from test.support.mock_manager_harness import MockManagerHarness


ROOT = Path(__file__).resolve().parents[1]


class MockManagerScenarioTests(ManagerScenarioMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not manager_scenario_backend_enabled("mock"):
            raise unittest.SkipTest("ACW manager scenarios are running without the mock backend")

    def setUp(self):
        self.harness = MockManagerHarness(ROOT)
        self.harness.start()

    def tearDown(self):
        self.harness.cleanup()


if __name__ == "__main__":
    unittest.main()
