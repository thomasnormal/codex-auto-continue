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
        if _test_failed(self):
            self.harness.archive_failure_artifacts(self.id())
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

    def test_watcher_auto_pauses_on_interrupt_banner(self):
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
            "pause: auto-pausing watcher",
            timeout=45.0,
        )
        self.harness.wait_for_watcher_stopped(timeout=15.0)

        state_text = state_file.read_text(encoding="utf-8", errors="ignore")
        self.assertIn("pause: auto-pausing watcher", watch_log)
        self.assertIn("auto-paused:", state_text)

    def test_manager_start_works_for_plain_full_auto_codex_pane(self):
        self.harness.rename_window("fullauto")
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()

        start = self.harness.run_manager("start", "fullauto", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        self.assertIn("resolved: target=fullauto pane=", start.stdout, self.harness.diagnostics())
        self.assertIn(first_turn.thread_id, start.stdout, self.harness.diagnostics())

    def test_manager_edit_updates_message_for_current_pane(self):
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        self.harness.run_manager("start", self.harness.pane_id, "--message", "initial continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        editor = self.harness.make_editor_script("updated continue")
        self.harness.run_manager("edit", self.harness.pane_id, env_overrides={"EDITOR": str(editor)})
        self.harness.wait_for_manager_state_contains(first_turn.thread_id, "updated continue")

        state_text = self.harness.manager_state_file(first_turn.thread_id).read_text(encoding="utf-8", errors="ignore")
        self.assertIn("updated continue", state_text, self.harness.diagnostics())

    def test_manager_doctor_reports_healthy_current_pane(self):
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()

        doctor = self.harness.run_manager_in_current_pane("doctor")

        self.assertIn("RESULT: ok", doctor.stdout, self.harness.diagnostics())
        self.assertIn(f"Codex thread detected: {first_turn.thread_id}", doctor.stdout, self.harness.diagnostics())

    def test_manager_doctor_reports_missing_thread_for_plain_shell_pane(self):
        self.harness.rename_window("shellpane")

        doctor = self.harness.run_manager_in_current_pane("doctor", check=False)

        self.assertNotEqual(0, doctor.returncode, self.harness.diagnostics())
        self.assertIn("pane resolved:", doctor.stdout, self.harness.diagnostics())
        self.assertIn("could not detect a Codex thread", doctor.stdout, self.harness.diagnostics())
        self.assertIn("RESULT: error", doctor.stdout, self.harness.diagnostics())

    def test_manager_start_fails_on_plain_shell_pane_without_thread(self):
        self.harness.rename_window("plain")

        start = self.harness.run_manager("start", "plain", "--message", "test continue", check=False)

        self.assertNotEqual(0, start.returncode, self.harness.diagnostics())
        self.assertIn("resolved: target=plain pane=", start.stdout, self.harness.diagnostics())
        self.assertIn("could not determine thread_id", start.stderr, self.harness.diagnostics())
        self.assertFalse(self.harness.manager_pid_file.exists(), self.harness.diagnostics())

    def test_manager_status_reports_dead_after_real_watcher_exit(self):
        self.harness.rename_window("deadwatch")
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        self.harness.run_manager("start", "deadwatch", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        self.harness.stop_manager_watcher()
        status = self.harness.run_manager("status", "--details")

        self.assertIn("deadwatch", status.stdout, self.harness.diagnostics())
        self.assertIn("STATE:           dead", status.stdout, self.harness.diagnostics())

    def test_manager_status_summary_shows_recent_last_agent_text(self):
        token = "CONTROLPANELTOKEN"
        self.harness.rename_window("summary")
        self.harness.start_codex(f"say exactly {token} and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        self.harness.run_manager("start", "summary", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        status = self.harness.run_manager("status")

        self.assertIn("LAST_AGENT", status.stdout, self.harness.diagnostics())
        self.assertIn(token, status.stdout, self.harness.diagnostics())

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
