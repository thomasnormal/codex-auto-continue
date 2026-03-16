import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from test.support.local_codex_harness import LocalCodexHarness
from test.support.local_codex_harness import require_local_codex_prereqs


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


class CodexTeamLocalIntegrationTests(unittest.TestCase):
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

    def wait_for_team_state(self, state_path: Path, predicate, timeout: float = 30.0) -> dict:
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

    def test_codex_team_creates_team_and_routes_worker_reply(self):
        proc = self.run_team("1")

        self.assertIn("leader: leader", proc.stdout, self.harness.diagnostics())
        self.assertIn("agent1", proc.stdout, self.harness.diagnostics())
        state_match = re.search(r"^state:\s+(.+)$", proc.stdout, re.MULTILINE)
        self.assertIsNotNone(state_match, self.harness.diagnostics())
        state_path = Path(state_match.group(1).strip())
        state = self.wait_for_team_state(
            state_path,
            lambda payload: payload["leader"]["ready"] and all(member["ready"] for member in payload["members"]),
            timeout=60.0,
        )

        leader_pane = str(state["leader"]["pane"])
        worker_pane = str(state["members"][0]["pane"])
        panes = self.harness.tmux_stdout("list-panes", "-t", self.harness.session_name, "-F", "#{pane_id}").splitlines()
        self.assertEqual(2, len(panes), self.harness.diagnostics())
        self.assertIn(leader_pane, panes, self.harness.diagnostics())
        self.assertIn(worker_pane, panes, self.harness.diagnostics())

        self.harness.send_codex_prompt_to_pane(
            leader_pane,
            'say exactly <message to="agent1">what is 40 + 2</message> and nothing else',
        )

        self.harness.wait_for_pane_contains_in_pane(worker_pane, "42", timeout=60.0, lines=200)
        leader_text = self.harness.wait_for_pane_contains_in_pane(
            leader_pane,
            '<reply from="agent1">',
            timeout=60.0,
            lines=240,
        )
        self.assertIn("42", leader_text, self.harness.diagnostics())

    def test_codex_team_returns_and_leader_finishes_booting_in_background(self):
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
            timeout=60.0,
        )

        self.assertTrue(state["leader"]["initial_prompt_sent"], self.harness.diagnostics())
        self.assertTrue(state["leader"]["ready"], self.harness.diagnostics())
        self.assertTrue(all(member["initial_prompt_sent"] for member in state["members"]), self.harness.diagnostics())
        self.assertTrue(all(member["ready"] for member in state["members"]), self.harness.diagnostics())

    def test_codex_team_rejects_multi_pane_window(self):
        self.harness.tmux("split-window", "-t", self.harness.pane_id, "-P", "-F", "#{pane_id}")

        proc = self.run_team("1", check=False)

        self.assertEqual(1, proc.returncode, self.harness.diagnostics())
        self.assertIn("current window already has 2 panes", proc.stderr, self.harness.diagnostics())


if __name__ == "__main__":
    unittest.main()
