from __future__ import annotations

import json
import os
import re


def manager_scenario_backends() -> set[str]:
    raw = os.environ.get("ACW_MANAGER_SCENARIO_BACKEND", "").strip().lower()
    if not raw:
        backends = {"mock"}
        if os.environ.get("AUTO_CONTINUE_RUN_LOCAL_CODEX_TESTS") == "1":
            backends.add("local")
        if os.environ.get("AUTO_CONTINUE_RUN_REAL_CODEX_TESTS") == "1":
            backends.add("real")
        return backends
    if raw == "both":
        return {"mock", "real"}
    if raw == "all":
        return {"mock", "local", "real"}
    parts = {part.strip() for part in raw.split(",") if part.strip()}
    if parts and parts <= {"mock", "local", "real"}:
        return parts
    raise RuntimeError("ACW_MANAGER_SCENARIO_BACKEND must be one of: mock, local, real, both, all, or a comma-separated subset")


def manager_scenario_backend_enabled(name: str) -> bool:
    return name in manager_scenario_backends()


class ManagerScenarioMixin:
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
        seed_pane = self.harness.pane_id

        self.harness.new_window("resume")
        self.harness.resume_codex(first_turn.thread_id)
        self.harness.wait_for_thread_visible(first_turn.thread_id)
        self.harness.kill_pane(seed_pane)

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
        formal_id_before = str(
            self.harness.manager_runtime_row_for_pane(formal_pane).get("runtime_id", "")
        )
        aot_id_before = str(
            self.harness.manager_runtime_row_for_pane(aot_pane).get("runtime_id", "")
        )

        self.harness.run_manager("pause", "formal")
        self.harness.run_manager("pause", "aot")
        self.harness.wait_for_manager_watcher_paused_in_pane(formal_pane)
        self.harness.wait_for_manager_watcher_paused_in_pane(aot_pane)

        resume = self.harness.run_manager("resume", "formal", "aot")
        self.harness.wait_for_manager_watcher_started_in_pane(formal_pane, formal_turn.thread_id)
        self.harness.wait_for_manager_watcher_started_in_pane(aot_pane, aot_turn.thread_id)

        self.assertIn("resolved: target=formal pane=", resume.stdout, self.harness.diagnostics())
        self.assertIn("resolved: target=aot pane=", resume.stdout, self.harness.diagnostics())
        self.assertIsNotNone(self.harness.manager_runtime_row(formal_turn.thread_id), self.harness.diagnostics())
        self.assertIsNotNone(self.harness.manager_runtime_row(aot_turn.thread_id), self.harness.diagnostics())
        self.assertNotEqual(
            formal_id_before,
            str(self.harness.manager_runtime_row_for_pane(formal_pane).get("runtime_id", "")),
        )
        self.assertNotEqual(
            aot_id_before,
            str(self.harness.manager_runtime_row_for_pane(aot_pane).get("runtime_id", "")),
        )

    def test_manager_resume_without_target_resumes_all_paused_sessions(self):
        self.harness.rename_window("formal")
        self.harness.start_codex("say exactly FORMALALLDONE and nothing else")
        formal_turn = self.harness.wait_for_first_completed_turn()
        formal_pane = self.harness.pane_id

        self.harness.new_window("aot")
        self.harness.start_codex("say exactly AOTALLDONE and nothing else")
        aot_turn = self.harness.wait_for_first_completed_turn()
        aot_pane = self.harness.pane_id

        self.harness.run_manager("start", "formal", "--message", "continue formal")
        self.harness.wait_for_manager_watcher_started_in_pane(formal_pane, formal_turn.thread_id)
        self.harness.run_manager("start", "aot", "--message", "continue aot")
        self.harness.wait_for_manager_watcher_started_in_pane(aot_pane, aot_turn.thread_id)

        self.harness.run_manager("pause")
        self.harness.wait_for_manager_watcher_paused_in_pane(formal_pane)
        self.harness.wait_for_manager_watcher_paused_in_pane(aot_pane)
        formal_id_before = str(
            self.harness.manager_runtime_row_for_pane(formal_pane).get("runtime_id", "")
        )
        aot_id_before = str(
            self.harness.manager_runtime_row_for_pane(aot_pane).get("runtime_id", "")
        )

        resume = self.harness.run_manager("resume")
        self.harness.wait_for_manager_watcher_started_in_pane(formal_pane, formal_turn.thread_id)
        self.harness.wait_for_manager_watcher_started_in_pane(aot_pane, aot_turn.thread_id)

        self.assertIn("resuming 2 paused managed session(s) via restart", resume.stdout, self.harness.diagnostics())
        self.assertNotEqual(
            formal_id_before,
            str(self.harness.manager_runtime_row_for_pane(formal_pane).get("runtime_id", "")),
        )
        self.assertNotEqual(
            aot_id_before,
            str(self.harness.manager_runtime_row_for_pane(aot_pane).get("runtime_id", "")),
        )

    def test_manager_pause_without_target_pauses_all_managed_sessions(self):
        self.harness.rename_window("formal")
        self.harness.start_codex("say exactly PAUSEFORMAL and nothing else")
        formal_turn = self.harness.wait_for_first_completed_turn()
        formal_pane = self.harness.pane_id

        self.harness.new_window("aot")
        self.harness.start_codex("say exactly PAUSEAOT and nothing else")
        aot_turn = self.harness.wait_for_first_completed_turn()
        aot_pane = self.harness.pane_id

        self.harness.run_manager("start", "formal", "--message", "continue formal")
        self.harness.wait_for_manager_watcher_started_in_pane(formal_pane, formal_turn.thread_id)
        self.harness.run_manager("start", "aot", "--message", "continue aot")
        self.harness.wait_for_manager_watcher_started_in_pane(aot_pane, aot_turn.thread_id)

        pause = self.harness.run_manager("pause")
        self.harness.wait_for_manager_watcher_paused_in_pane(formal_pane)
        self.harness.wait_for_manager_watcher_paused_in_pane(aot_pane)

        self.assertIn(
            f"paused: pane={formal_pane} thread_id={formal_turn.thread_id}",
            pause.stdout,
            self.harness.diagnostics(),
        )
        self.assertIn(
            f"paused: pane={aot_pane} thread_id={aot_turn.thread_id}",
            pause.stdout,
            self.harness.diagnostics(),
        )
        self.assertEqual(
            "1",
            str(self.harness.manager_runtime_row_for_pane(formal_pane).get("paused", "")),
            self.harness.diagnostics(),
        )
        self.assertEqual(
            "1",
            str(self.harness.manager_runtime_row_for_pane(aot_pane).get("paused", "")),
            self.harness.diagnostics(),
        )

        status = self.harness.run_manager("status")
        self.assertIn("paused", status.stdout, self.harness.diagnostics())

    def test_manager_restart_restart_codex_reuses_same_thread(self):
        self.harness.rename_window("bugs")
        self.harness.start_codex("say exactly BUGSDONE and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        bugs_pane = self.harness.pane_id

        self.harness.run_manager("start", "bugs", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started_in_pane(bugs_pane, first_turn.thread_id)
        runtime_id_before = str(
            self.harness.manager_runtime_row_for_pane(bugs_pane).get("runtime_id", "")
        )

        restart = self.harness.run_manager("restart", "bugs", "--restart-codex")
        self.harness.wait_for_manager_watcher_started_in_pane(bugs_pane, first_turn.thread_id)
        self.harness.reset_codex_log_baseline()
        self.harness.send_codex_prompt_to_pane(
            bugs_pane,
            "say exactly RESTARTRESUMEOK and nothing else",
        )
        resumed_turn = self.harness.wait_for_first_completed_turn(timeout=60.0)

        self.assertIn("restarting codex: pane=", restart.stdout, self.harness.diagnostics())
        self.assertEqual(first_turn.thread_id, resumed_turn.thread_id, self.harness.diagnostics())
        self.assertNotEqual(
            runtime_id_before,
            str(self.harness.manager_runtime_row_for_pane(bugs_pane).get("runtime_id", "")),
        )

    def test_manager_edit_updates_message_for_current_pane(self):
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        self.harness.run_manager("start", self.harness.pane_id, "--message", "initial continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        editor = self.harness.make_editor_script("updated continue")
        self.harness.run_manager(
            "edit",
            self.harness.pane_id,
            env_overrides={"EDITOR": str(editor)},
        )
        self.harness.wait_for_manager_state_contains(first_turn.thread_id, "updated continue")

        state_row = self.harness.manager_session_row(first_turn.thread_id)
        state_text = json.dumps(state_row or {}, sort_keys=True)
        self.assertIn("updated continue", state_text, self.harness.diagnostics())

    def test_manager_stop_forgets_managed_session(self):
        self.harness.rename_window("stopcase")
        self.harness.start_codex("say exactly STOPCASE and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        pane = self.harness.pane_id

        self.harness.run_manager("start", "stopcase", "--message", "stop me")
        self.harness.wait_for_manager_watcher_started_in_pane(pane, first_turn.thread_id)
        self.assertIsNotNone(self.harness.manager_session_row(first_turn.thread_id), self.harness.diagnostics())
        self.assertIsNotNone(self.harness.manager_runtime_row(first_turn.thread_id), self.harness.diagnostics())

        stop = self.harness.run_manager("stop", "stopcase")
        self.harness.wait_for_manager_watcher_stopped_in_pane(pane)

        self.assertIn(f"stopped: pane={pane} thread_id={first_turn.thread_id}", stop.stdout, self.harness.diagnostics())
        self.assertIsNone(self.harness.manager_runtime_row(first_turn.thread_id), self.harness.diagnostics())
        self.assertIsNone(self.harness.manager_session_row(first_turn.thread_id), self.harness.diagnostics())

        status = self.harness.run_manager("status")
        # After stop, the pane still runs Codex so it shows as unmanaged
        self.assertIn("unmanaged", status.stdout, self.harness.diagnostics())

    def test_manager_stop_without_target_forgets_all_managed_sessions(self):
        self.harness.rename_window("formal")
        self.harness.start_codex("say exactly STOPFORMAL and nothing else")
        formal_turn = self.harness.wait_for_first_completed_turn()
        formal_pane = self.harness.pane_id

        self.harness.new_window("aot")
        self.harness.start_codex("say exactly STOPAOT and nothing else")
        aot_turn = self.harness.wait_for_first_completed_turn()
        aot_pane = self.harness.pane_id

        self.harness.run_manager("start", "formal", "--message", "continue formal")
        self.harness.wait_for_manager_watcher_started_in_pane(formal_pane, formal_turn.thread_id)
        self.harness.run_manager("start", "aot", "--message", "continue aot")
        self.harness.wait_for_manager_watcher_started_in_pane(aot_pane, aot_turn.thread_id)

        stop = self.harness.run_manager("stop")
        self.harness.wait_for_manager_watcher_stopped_in_pane(formal_pane)
        self.harness.wait_for_manager_watcher_stopped_in_pane(aot_pane)

        self.assertIn(
            f"stopped: pane={formal_pane} thread_id={formal_turn.thread_id}",
            stop.stdout,
            self.harness.diagnostics(),
        )
        self.assertIn(
            f"stopped: pane={aot_pane} thread_id={aot_turn.thread_id}",
            stop.stdout,
            self.harness.diagnostics(),
        )
        self.assertIsNone(self.harness.manager_session_row(formal_turn.thread_id), self.harness.diagnostics())
        self.assertIsNone(self.harness.manager_session_row(aot_turn.thread_id), self.harness.diagnostics())
        self.assertIsNone(self.harness.manager_runtime_row(formal_turn.thread_id), self.harness.diagnostics())
        self.assertIsNone(self.harness.manager_runtime_row(aot_turn.thread_id), self.harness.diagnostics())

        status = self.harness.run_manager("status")
        # After stop, panes still run Codex so they show as unmanaged
        self.assertIn("unmanaged", status.stdout, self.harness.diagnostics())

    def test_manager_status_details_reads_live_sqlite_state_ref(self):
        self.harness.rename_window("details")
        self.harness.start_codex("say exactly DETAILSOK and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        self.harness.run_manager("start", "details", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        runtime_row = self.harness.manager_runtime_row(first_turn.thread_id)
        self.assertIsNotNone(runtime_row, self.harness.diagnostics())
        self.assertEqual(
            f"sqlite:session:{first_turn.thread_id}",
            str(runtime_row.get("state", "") or runtime_row.get("state_ref", "")),
            self.harness.diagnostics(),
        )

        status = self.harness.run_manager("status", "--details")

        self.assertIn("=== Entry 1/1 ===", status.stdout, self.harness.diagnostics())
        self.assertIn(f"THREAD_ID:       {first_turn.thread_id}", status.stdout, self.harness.diagnostics())
        self.assertIn(
            f"STATE_REF:       sqlite:session:{first_turn.thread_id}",
            status.stdout,
            self.harness.diagnostics(),
        )
        self.assertIn("STATE:           running", status.stdout, self.harness.diagnostics())

    def test_manager_doctor_reports_healthy_current_pane(self):
        self.harness.start_codex("say the word hello and nothing else")
        turn = self.harness.wait_for_first_completed_turn()

        doctor = self.harness.run_manager_in_current_pane("doctor")

        self.assertIn("RESULT: ok", doctor.stdout, self.harness.diagnostics())
        self.assertIn(turn.thread_id, doctor.stdout, self.harness.diagnostics())
        self.assertRegex(
            doctor.stdout,
            re.compile(r"Codex thread detected: [0-9a-f]{8}-[0-9a-f-]{27}"),
            self.harness.diagnostics(),
        )

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
        self.assertIsNone(self.harness.manager_daemon_pid(), self.harness.diagnostics())

    def test_manager_status_reports_shared_daemon_error_after_daemon_exit(self):
        self.harness.rename_window("deadwatch")
        self.harness.start_codex("say the word hello and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        self.harness.run_manager("start", "deadwatch", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        self.harness.stop_manager_watcher()
        status = self.harness.run_manager("status", "--details")

        self.assertIn("deadwatch", status.stdout, self.harness.diagnostics())
        self.assertIn("STATE:           error", status.stdout, self.harness.diagnostics())
        self.assertIn("Shared daemon error:", status.stdout, self.harness.diagnostics())

    def test_manager_repair_without_target_recovers_shared_daemon_for_multiple_live_panes(self):
        self.harness.rename_window("formal")
        self.harness.start_codex("say exactly FORMALREAL and nothing else")
        formal_turn = self.harness.wait_for_first_completed_turn()
        formal_pane = self.harness.pane_id

        self.harness.new_window("aot")
        self.harness.start_codex("say exactly AOTREAL and nothing else")
        aot_turn = self.harness.wait_for_first_completed_turn()
        aot_pane = self.harness.pane_id

        self.harness.run_manager("start", "formal", "--message", "continue formal")
        self.harness.wait_for_manager_watcher_started_in_pane(formal_pane, formal_turn.thread_id)
        self.harness.run_manager("start", "aot", "--message", "continue aot")
        self.harness.wait_for_manager_watcher_started_in_pane(aot_pane, aot_turn.thread_id)

        daemon_pid_before = self.harness.manager_daemon_pid()
        self.assertIsNotNone(daemon_pid_before, self.harness.diagnostics())

        self.harness.stop_manager_watcher()
        status = self.harness.run_manager("status")
        self.assertIn("Shared daemon error:", status.stdout, self.harness.diagnostics())
        self.assertIn(
            "2 managed live Codex pane(s) currently have no runtime",
            status.stdout,
            self.harness.diagnostics(),
        )

        repair = self.harness.run_manager("repair")
        self.harness.wait_for_manager_watcher_started_in_pane(formal_pane, formal_turn.thread_id)
        self.harness.wait_for_manager_watcher_started_in_pane(aot_pane, aot_turn.thread_id)

        daemon_pid_after = self.harness.manager_daemon_pid()
        self.assertIsNotNone(daemon_pid_after, self.harness.diagnostics())
        self.assertNotEqual(daemon_pid_before, daemon_pid_after, self.harness.diagnostics())
        self.assertIn("repair:", repair.stdout, self.harness.diagnostics())

        recovered = self.harness.run_manager("status")
        self.assertNotIn("Shared daemon error:", recovered.stdout, self.harness.diagnostics())
        self.assertIn("formal", recovered.stdout, self.harness.diagnostics())
        self.assertIn("aot", recovered.stdout, self.harness.diagnostics())

    def test_manager_repair_dry_run_reports_plan_without_recreating_runtime(self):
        self.harness.rename_window("repairdry")
        self.harness.start_codex("say exactly REPAIRDRY and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        pane = self.harness.pane_id

        self.harness.run_manager("start", "repairdry", "--message", "repair continue")
        self.harness.wait_for_manager_watcher_started_in_pane(pane, first_turn.thread_id)

        self.harness.stop_manager_watcher()
        broken = self.harness.run_manager("status")
        self.assertIn("Shared daemon error:", broken.stdout, self.harness.diagnostics())

        repair = self.harness.run_manager("repair", "--dry-run")

        self.assertIn("repair: dry-run plan", repair.stdout, self.harness.diagnostics())
        self.assertIn(
            f"AUTO: start one managed runtime on pane {pane} for thread {first_turn.thread_id[:8]}",
            repair.stdout,
            self.harness.diagnostics(),
        )
        self.assertIn(f"run: acw repair {pane}", repair.stdout, self.harness.diagnostics())

        status = self.harness.run_manager("status")
        self.assertIn("Shared daemon error:", status.stdout, self.harness.diagnostics())

    def test_manager_restart_without_target_recovers_dead_runtime(self):
        self.harness.rename_window("recoverdead")
        self.harness.start_codex("say exactly RECOVERDEAD and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        pane = self.harness.pane_id

        self.harness.run_manager("start", "recoverdead", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started_in_pane(pane, first_turn.thread_id)
        runtime_id_before = str(
            self.harness.manager_runtime_row_for_pane(pane).get("runtime_id", "")
        )

        self.harness.stop_manager_watcher()
        restart = self.harness.run_manager("restart")
        self.harness.wait_for_manager_watcher_started_in_pane(pane, first_turn.thread_id)

        self.assertIn("--- restarting", restart.stdout, self.harness.diagnostics())
        self.assertNotEqual(
            runtime_id_before,
            str(self.harness.manager_runtime_row_for_pane(pane).get("runtime_id", "")),
        )

    def test_manager_status_summary_shows_recent_last_agent_text(self):
        token = "PANELTOK"
        self.harness.rename_window("summary")
        self.harness.start_codex(f"say exactly {token} and nothing else")
        first_turn = self.harness.wait_for_first_completed_turn()
        self.harness.run_manager("start", "summary", "--message", "test continue")
        self.harness.wait_for_manager_watcher_started(first_turn.thread_id)

        status = self.harness.run_manager("status")

        self.assertIn("LAST AGENT MESSAGE", status.stdout, self.harness.diagnostics())
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
