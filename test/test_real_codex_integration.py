import unittest
from pathlib import Path
import json
import os
import re
import subprocess

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


class RealCodexTeamIntegrationTests(_RealCodexHarnessTestCase):
    def run_team(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = self.harness.process_env
        env["TMUX_PANE"] = self.harness.pane_id
        proc = subprocess.run(
            ["python3", str(ROOT / "bin" / "codex_team.py"), *args],
            env=env,
            cwd=str(ROOT),
            text=True,
            capture_output=True,
            check=False,
            preexec_fn=lambda: os.umask(0o077),
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"codex-team failed: {' '.join(args)}\n"
                f"stdout:\n{proc.stdout}\n"
                f"stderr:\n{proc.stderr}\n"
                f"{self.harness.diagnostics()}"
            )
        return proc

    def wait_for_team_state(self, state_path: Path, predicate, timeout: float = 60.0) -> dict:
        def _probe() -> dict | None:
            if not state_path.is_file():
                return None
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
            if predicate(state):
                return state
            return None

        return self.harness._wait_for_value(_probe, timeout=timeout, description=f"team state {state_path.name}")

    def test_codex_team_waits_for_ready_and_routes_real_worker_reply(self):
        proc = self.run_team("1")

        state_match = re.search(r"^state:\s+(.+)$", proc.stdout, re.MULTILINE)
        self.assertIsNotNone(state_match, self.harness.diagnostics())
        state_path = Path(state_match.group(1).strip())
        self.assertIn(
            "leader will finish booting after this command returns",
            proc.stdout,
            self.harness.diagnostics(),
        )
        state = self.wait_for_team_state(
            state_path,
            lambda payload: payload["leader"]["ready"] and all(member["ready"] for member in payload["members"]),
            timeout=90.0,
        )

        self.assertTrue(state["leader"]["ready"], self.harness.diagnostics())
        self.assertTrue(all(member["ready"] for member in state["members"]), self.harness.diagnostics())

        leader_pane = str(state["leader"]["pane"])
        worker_pane = str(state["members"][0]["pane"])
        self.harness.send_codex_prompt_to_pane(
            leader_pane,
            'Output exactly this tag and nothing else: <message to="agent1">say exactly 42 and nothing else</message>',
        )

        worker_text = self.harness.wait_for_pane_contains_in_pane(worker_pane, "42", timeout=120.0, lines=240)
        leader_text = self.harness.wait_for_pane_contains_in_pane(
            leader_pane,
            '<reply from="agent1">',
            timeout=120.0,
            lines=320,
        )

        self.assertIn("42", worker_text, self.harness.diagnostics())
        self.assertIn("42", leader_text, self.harness.diagnostics())

if __name__ == "__main__":
    unittest.main()
