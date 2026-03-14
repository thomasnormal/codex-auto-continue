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

    def test_manager_start_works_for_plain_full_auto_codex_pane(self):
        self.harness.rename_window("fullauto")
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()

        start = self.harness.run_manager("start", "fullauto", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        self.assertIn("resolved: target=fullauto pane=", start.stdout, self.harness.diagnostics())
        self.assertIn(first_turn.thread_id, start.stdout, self.harness.diagnostics())

    def test_manager_start_works_for_codex_resume_pane(self):
        self.harness.rename_window("seed")
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()

        self.harness.new_window("resume")
        self.harness.resume_codex(first_turn.thread_id)
        self.harness.wait_for_recent_codex_log_contains(first_turn.thread_id, timeout=45.0)
        self.harness.tmux("kill-window", "-t", f"{self.harness.session_name}:0")

        start = self.harness.run_manager("start", "resume", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        self.assertIn("resolved: target=resume pane=", start.stdout, self.harness.diagnostics())
        self.assertIn(first_turn.thread_id, start.stdout, self.harness.diagnostics())

    def test_manager_resume_accepts_multiple_targets(self):
        self.harness.rename_window("formal")
        self.harness.start_codex("say exactly FORMALDONE and nothing else")
        formal_turn = self.harness.wait_for_first_completed_turn()
        formal_pane = self.harness.pane_id

        self.harness.new_window("aot")
        self.harness.start_codex("say exactly AOTDONE and nothing else")
        aot_turn = self.harness.wait_for_first_completed_turn()
        aot_pane = self.harness.pane_id

        self.harness.run_manager("start", "formal", "--message", "continue formal")
        self.harness.wait_for_manager_watcher_started_in_pane(formal_pane, formal_turn.thread_id)
        self.harness.run_manager("start", "aot", "--message", "continue aot")
        self.harness.wait_for_manager_watcher_started_in_pane(aot_pane, aot_turn.thread_id)
        formal_pid_before = self.harness._read_manager_pid_for_pane(formal_pane)
        aot_pid_before = self.harness._read_manager_pid_for_pane(aot_pane)

        self.harness.run_manager("pause", "formal")
        self.harness.run_manager("pause", "aot")
        self.harness.wait_for_manager_watcher_paused_in_pane(formal_pane)
        self.harness.wait_for_manager_watcher_paused_in_pane(aot_pane)

        resume = self.harness.run_manager("resume", "formal", "aot")
        self.harness.wait_for_manager_watcher_started_in_pane(formal_pane, formal_turn.thread_id)
        self.harness.wait_for_manager_watcher_started_in_pane(aot_pane, aot_turn.thread_id)

        self.assertIn("resolved: target=formal pane=", resume.stdout, self.harness.diagnostics())
        self.assertIn("resolved: target=aot pane=", resume.stdout, self.harness.diagnostics())
        self.assertTrue(self.harness.manager_runtime_file(formal_turn.thread_id).is_file(), self.harness.diagnostics())
        self.assertTrue(self.harness.manager_runtime_file(aot_turn.thread_id).is_file(), self.harness.diagnostics())
        self.assertNotEqual(formal_pid_before, self.harness._read_manager_pid_for_pane(formal_pane))
        self.assertNotEqual(aot_pid_before, self.harness._read_manager_pid_for_pane(aot_pane))

    def test_manager_restart_restart_codex_reuses_same_thread(self):
        self.harness.rename_window("bugs")
        self.harness.start_codex("say exactly BUGSDONE and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        bugs_pane = self.harness.pane_id

        self.harness.run_manager("start", "bugs", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started_in_pane(bugs_pane, first_turn.thread_id)
        watcher_pid_before = self.harness._read_manager_pid_for_pane(bugs_pane)

        restart = self.harness.run_manager("restart", "bugs", "--restart-codex")
        self.harness.wait_for_manager_watcher_started_in_pane(bugs_pane, first_turn.thread_id)
        self.harness.wait_for_pane_contains_in_pane(
            bugs_pane,
            f"codex resume {first_turn.thread_id}",
            timeout=30.0,
        )
        self.harness.reset_codex_log_baseline()
        self.harness.send_codex_prompt_to_pane(
            bugs_pane,
            "say exactly RESTARTRESUMEOK and nothing else",
        )
        resumed_turn = self.harness.wait_for_first_completed_turn(timeout=60.0)

        self.assertIn("restarting codex: pane=", restart.stdout, self.harness.diagnostics())
        self.assertEqual(first_turn.thread_id, resumed_turn.thread_id, self.harness.diagnostics())
        self.assertNotEqual(watcher_pid_before, self.harness._read_manager_pid_for_pane(bugs_pane))

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
        self.assertIn("no live Codex agent detected in pane", doctor.stdout, self.harness.diagnostics())
        self.assertIn("RESULT: error", doctor.stdout, self.harness.diagnostics())

    def test_manager_start_fails_on_plain_shell_pane_without_thread(self):
        self.harness.rename_window("plain")

        start = self.harness.run_manager("start", "plain", "--message", "test continue", check=False)

        self.assertNotEqual(0, start.returncode, self.harness.diagnostics())
        self.assertIn("resolved: target=plain pane=", start.stdout, self.harness.diagnostics())
        self.assertIn("no live Codex agent detected in pane", start.stderr, self.harness.diagnostics())
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
        self.assertIn("STATE:           error", status.stdout, self.harness.diagnostics())
        self.assertIn("no watcher for live Codex pane", status.stdout, self.harness.diagnostics())

    def test_manager_restart_without_target_recovers_dead_watcher(self):
        self.harness.rename_window("recoverdead")
        self.harness.start_codex("say exactly RECOVERDEAD and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        self.harness.run_manager("start", "recoverdead", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started_in_pane(self.harness.pane_id, first_turn.thread_id)
        watcher_pid_before = self.harness._read_manager_pid_for_pane(self.harness.pane_id)

        self.harness.stop_manager_watcher()
        restart = self.harness.run_manager("restart")
        self.harness.wait_for_manager_watcher_started_in_pane(self.harness.pane_id, first_turn.thread_id)

        self.assertIn("--- restarting", restart.stdout, self.harness.diagnostics())
        self.assertNotEqual(watcher_pid_before, self.harness._read_manager_pid_for_pane(self.harness.pane_id))

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
