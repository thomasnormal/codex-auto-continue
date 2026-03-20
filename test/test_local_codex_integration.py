import time
import os
import pty
import select
import subprocess
import unittest
from pathlib import Path
import sys

from test.support.local_codex_harness import LocalCodexHarness
from test.support.local_codex_harness import require_local_codex_prereqs
from test.support.manager_scenarios import ManagerScenarioMixin
from test.support.manager_scenarios import manager_scenario_backend_enabled


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_repo  # noqa: E402


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

        self.assertIn("busy", status.stdout, self.harness.diagnostics())
        self.harness.wait_for_pane_contains("BUSYDONE", timeout=30.0)

    def test_status_shows_waiting_for_running_idle_runtime_via_real_cli(self):
        self.harness.rename_window("runningwait")
        self.harness.start_codex("say exactly RUNWAIT and nothing else")
        turn = self.harness.wait_for_first_completed_turn()

        self.harness.run_manager("start", "runningwait", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)

        status = self.harness.run_manager("status")

        self.assertIn("waiting", status.stdout, self.harness.diagnostics())

    def test_resume_sends_continue_for_waiting_runtime_via_real_cli(self):
        self.harness.rename_window("resumewait")
        self.harness.start_codex("say exactly RESUMEWAIT and nothing else")
        turn = self.harness.wait_for_first_completed_turn()

        self.harness.run_manager("start", "resumewait", "--message", "continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        self.harness.mutate_manager_session_state(
            turn.thread_id,
            message="say exactly RESUMEFIRED and nothing else",
        )
        time.sleep(1.1)

        resume = self.harness.run_manager("resume", "resumewait")
        self.harness.wait_for_pane_contains("RESUMEFIRED", timeout=30.0)

        self.assertIn("resuming waiting session", resume.stdout, self.harness.diagnostics())

    def test_looping_docs_only_checkpoint_turns_auto_pause_via_real_cli(self):
        self.harness.rename_window("loopdoc")
        self.harness.start_codex("say exactly LOOPDOCSTART and nothing else")
        turn = self.harness.wait_for_first_completed_turn()
        pane = self.harness.pane_id

        self.harness.run_manager("start", "loopdoc", "--message", "continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        loop_prompt = (
            "emit verbatim: Updated:\n"
            "- docs/DDR3_PLAN.md\n"
            "\n"
            "Committed:\n"
            "[DDR3] checkpoint"
        )
        self.harness.mutate_manager_session_state(
            turn.thread_id,
            message=loop_prompt,
            loop_signature="docs-only:docs/DDR3_PLAN.md",
            loop_score=3,
        )
        time.sleep(1.1)

        resume = self.harness.run_manager("resume", "loopdoc")
        self.harness.wait_for_pane_contains("Committed:", timeout=30.0)
        self.harness.wait_for_manager_watcher_paused_in_pane(pane, timeout=30.0)
        status = self.harness.run_manager("status")

        self.assertIn("resuming waiting session", resume.stdout, self.harness.diagnostics())
        self.assertIn("suspected loop", status.stdout, self.harness.diagnostics())

    def test_status_shows_busy_for_running_busy_runtime_via_real_cli(self):
        self.harness.rename_window("runningbusy")
        self.harness.start_codex("say exactly RUNBUSY and nothing else")
        turn = self.harness.wait_for_first_completed_turn()

        self.harness.run_manager("start", "runningbusy", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        self.harness.send_codex_prompt("sleep 3 seconds then say exactly RUNBUSYDONE and nothing else")
        self.harness.wait_for_pane_contains("working...", timeout=10.0)

        status = self.harness.run_manager("status")

        self.assertIn("busy", status.stdout, self.harness.diagnostics())
        self.harness.wait_for_pane_contains("RUNBUSYDONE", timeout=30.0)

    def test_status_shows_input_for_running_approval_prompt_via_real_cli(self):
        self.harness.rename_window("runninginput")
        self.harness.start_codex("say exactly RUNINPUT and nothing else")
        turn = self.harness.wait_for_first_completed_turn()

        self.harness.run_manager("start", "runninginput", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        self.harness.send_codex_prompt("show approval prompt")
        self.harness.wait_for_pane_contains("Tool call needs your approval", timeout=10.0)

        status = self.harness.run_manager("status")

        self.assertIn("input", status.stdout, self.harness.diagnostics())

    def test_status_shows_average_time_per_continue_via_real_cli(self):
        self.harness.rename_window("avgacw")
        self.harness.start_codex("say exactly AVGACW and nothing else")
        turn = self.harness.wait_for_first_completed_turn()

        self.harness.run_manager("start", "avgacw", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        self.harness.mutate_manager_session_state(
            turn.thread_id,
            last_continue_at="2026-03-19 12:34:56",
            continue_count=1,
        )

        status = self.harness.run_manager("status")

        self.assertIn("a: ", status.stdout, self.harness.diagnostics())
        self.assertIn("l: ", status.stdout, self.harness.diagnostics())
        self.assertIn("s: ", status.stdout, self.harness.diagnostics())

    def test_status_shows_branch_for_managed_runtime_via_real_cli(self):
        self.harness.rename_window("branchpane")
        nongit = self.harness.root / "nongit-branchpane"
        nongit.mkdir(parents=True, exist_ok=True)
        self.harness.send_keys(f"cd {nongit}", "C-m")
        pane_cwd = ""
        for _ in range(20):
            pane_cwd = self.harness.tmux_stdout(
                "display-message",
                "-p",
                "-t",
                self.harness.pane_id,
                "#{pane_current_path}",
            )
            if pane_cwd == str(nongit):
                break
            time.sleep(0.2)
        self.harness.start_codex("say exactly BRANCHPANE and nothing else")
        turn = self.harness.wait_for_first_completed_turn()

        self.harness.run_manager("start", "branchpane", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        self.harness.append_codex_rollout_workdir(turn.thread_id, str(ROOT), cmd="pwd")

        status = self.harness.run_manager("status")
        expected = acw_repo.branch_for_cwd(str(ROOT), ttl_secs=0.0)

        self.assertEqual(str(nongit), pane_cwd, self.harness.diagnostics())
        self.assertIn(f"br={expected}", status.stdout, self.harness.diagnostics())

    def test_bare_acw_refreshes_interactive_status_over_pty(self):
        self.harness.rename_window("watchlive")
        self.harness.start_codex("say exactly WATCHLIVE and nothing else")
        turn = self.harness.wait_for_first_completed_turn()
        self.harness.run_manager("start", "watchlive", "--message", "INITIALPROMPT")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)
        subprocess_has_rich = (
            subprocess.run(
                [
                    "python3",
                    "-c",
                    "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('rich') else 1)",
                ],
                env=self.harness.process_env,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            == 0
        )

        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            ["python3", str(ROOT / "bin" / "auto_continue_watchd.py")],
            env=self.harness.process_env,
            cwd=str(ROOT),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
            preexec_fn=lambda: os.umask(0o077),
        )
        os.close(slave_fd)

        def _read_until(needle: str, timeout: float = 15.0) -> str:
            end = time.time() + timeout
            data = b""
            while time.time() < end:
                ready, _, _ = select.select([master_fd], [], [], 0.2)
                if master_fd in ready:
                    chunk = os.read(master_fd, 65536)
                    if not chunk:
                        break
                    data += chunk
                    text = data.decode("utf-8", errors="ignore")
                    if needle in text:
                        return text
            raise AssertionError(f"did not observe {needle!r}\n{data.decode('utf-8', errors='ignore')}\n{self.harness.diagnostics()}")

        try:
            first = _read_until("INITIALPROMPT")
            self.assertIn("Ctrl-C: exit; e: edit; s: start; p: pause; u: resume; r: restart", first, self.harness.diagnostics())
            if subprocess_has_rich:
                self.assertRegex(first, "\x1b\\[[0-9;]*m", self.harness.diagnostics())
                self.assertNotIn("\x1b[2J", first, self.harness.diagnostics())
            self.harness.mutate_manager_session_state(turn.thread_id, message="UPDATEDPROMPT")
            second = _read_until("UPDATEDPROMPT")
            self.assertIn("UPDATEDPROMPT", second, self.harness.diagnostics())
            if subprocess_has_rich:
                self.assertRegex(second, "\x1b\\[[0-9;]*m", self.harness.diagnostics())
                self.assertNotIn("\x1b[2J", second, self.harness.diagnostics())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
            os.close(master_fd)

    def test_bare_acw_accepts_pause_command_over_pty(self):
        self.harness.rename_window("watchpause")
        self.harness.start_codex("say exactly WATCHPAUSE and nothing else")
        turn = self.harness.wait_for_first_completed_turn()
        pane = self.harness.pane_id
        self.harness.run_manager("start", "watchpause", "--message", "INITIALPROMPT")
        self.harness.wait_for_manager_watcher_started(turn.thread_id)

        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            ["python3", str(ROOT / "bin" / "auto_continue_watchd.py")],
            env=self.harness.process_env,
            cwd=str(ROOT),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
            preexec_fn=lambda: os.umask(0o077),
        )
        os.close(slave_fd)

        def _read_until(needle: str, timeout: float = 15.0) -> str:
            end = time.time() + timeout
            data = b""
            while time.time() < end:
                ready, _, _ = select.select([master_fd], [], [], 0.2)
                if master_fd in ready:
                    chunk = os.read(master_fd, 65536)
                    if not chunk:
                        break
                    data += chunk
                    text = data.decode("utf-8", errors="ignore")
                    if needle in text:
                        return text
            raise AssertionError(
                f"did not observe {needle!r}\n{data.decode('utf-8', errors='ignore')}\n{self.harness.diagnostics()}"
            )

        def _read_available(timeout: float = 2.0) -> str:
            end = time.time() + timeout
            data = b""
            while time.time() < end:
                ready, _, _ = select.select([master_fd], [], [], 0.2)
                if master_fd not in ready:
                    continue
                chunk = os.read(master_fd, 65536)
                if not chunk:
                    break
                data += chunk
            return data.decode("utf-8", errors="ignore")

        try:
            first = _read_until("INITIALPROMPT")
            self.assertIn("Ctrl-C: exit; e: edit; s: start; p: pause; u: resume; r: restart", first, self.harness.diagnostics())
            os.write(master_fd, b"pause watchpause\r")
            self.harness.wait_for_manager_watcher_paused_in_pane(pane)
            second = _read_available()
            self.assertNotIn("paused: pane=", second, self.harness.diagnostics())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
            os.close(master_fd)

    def test_bare_acw_accepts_start_command_over_pty(self):
        self.harness.rename_window("watchstart")
        self.harness.start_codex("say exactly WATCHSTART and nothing else")
        turn = self.harness.wait_for_first_completed_turn()

        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            ["python3", str(ROOT / "bin" / "auto_continue_watchd.py")],
            env=self.harness.process_env,
            cwd=str(ROOT),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
            preexec_fn=lambda: os.umask(0o077),
        )
        os.close(slave_fd)

        def _read_until(needle: str, timeout: float = 15.0) -> str:
            end = time.time() + timeout
            data = b""
            while time.time() < end:
                ready, _, _ = select.select([master_fd], [], [], 0.2)
                if master_fd in ready:
                    chunk = os.read(master_fd, 65536)
                    if not chunk:
                        break
                    data += chunk
                    text = data.decode("utf-8", errors="ignore")
                    if needle in text:
                        return text
            raise AssertionError(
                f"did not observe {needle!r}\n{data.decode('utf-8', errors='ignore')}\n{self.harness.diagnostics()}"
            )

        try:
            first = _read_until("Ctrl-C: exit; e: edit; s: start; p: pause; u: resume; r: restart")
            self.assertIn("Ctrl-C: exit; e: edit; s: start; p: pause; u: resume; r: restart", first, self.harness.diagnostics())
            os.write(master_fd, b"s watchstart --message STARTPROMPT\r")
            self.harness.wait_for_manager_watcher_started(turn.thread_id)
            second = _read_until("start: thread_id=")
            self.assertIn("start: thread_id=", second, self.harness.diagnostics())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
            os.close(master_fd)


if __name__ == "__main__":
    unittest.main()
