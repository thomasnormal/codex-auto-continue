import io
import json
import subprocess
import unittest
import builtins
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from unittest.mock import mock_open

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import auto_continue_watchd as acw  # noqa: E402


THREAD = "11111111-1111-1111-1111-111111111111"


class WatchdUnitTests(unittest.TestCase):
    def test_main_help_prints_command_summary(self):
        out = io.StringIO()
        err = io.StringIO()
        with patch.object(acw.sys, "argv", ["auto_continue_watchd.py", "--help"]):
            with redirect_stdout(out), redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    acw.main()
        self.assertEqual(0, ctx.exception.code)
        text = out.getvalue()
        self.assertIn("Usage:", text)
        self.assertIn("Commands:", text)
        self.assertIn("doctor", text)
        self.assertIn("Examples:", text)
        self.assertEqual("", err.getvalue())

    def test_short_thread_id_keeps_prefix_and_suffix(self):
        self.assertEqual("11111111…1111", acw._short_thread_id(THREAD))

    def test_compute_state_shows_dead_without_live_pid_even_if_health_was_ok(self):
        self.assertEqual("dead", acw._compute_state({"pid": ""}, {"health": "ok"}))

    def test_compute_state_normalizes_legacy_stale_to_warn(self):
        self.assertEqual("warn", acw._compute_state({"pid": "1234"}, {"health": "stale"}))

    def test_state_summary_shows_health_detail_for_warn(self):
        summary = acw._state_summary("warn", {"health_detail": "codex log missing for pane %3"})
        self.assertIn("warn", summary)
        self.assertIn("codex log missing for pan...", summary)

    def test_state_summary_omits_detail_for_running(self):
        summary = acw._state_summary("running", {"health_detail": "should stay hidden"})
        self.assertEqual("[green]running[/green]", summary)

    def test_thread_times_formats_sqlite_metadata(self):
        with patch.object(acw, "thread_times_from_state_db", return_value=(100.0, 150.0)):
            with patch.object(acw, "_format_age", side_effect=["started", "last-msg"]):
                self.assertEqual(("started", "last-msg"), acw._thread_times(THREAD))

    def test_last_agent_snippet_prefers_recent_bullet_summary(self):
        pane = "\n".join([
            "  └ some tool output",
            "",
            "• Ran unit tests",
            "",
            "◦ Working (4m 04s • esc to interrupt)",
            "",
            "› Summarize recent commits",
            "",
            "  gpt-5.4 xhigh · 21% left · ~/circt",
        ])
        self.assertEqual("Ran unit tests", acw._extract_last_agent_snippet(pane))

    def test_status_summary_skips_global_thread_pane_scan(self):
        sessions = [{
            "thread_id": THREAD,
            "name": "formal",
            "message": "continue",
            "state_file": "/state/acw_session.json",
        }]
        live_rows = [{
            "pane": "%7",
            "thread": THREAD,
            "state": "/state/acw_session.json",
            "watch": "/state/watch.log",
            "msg_file": "",
            "msg_inline": "continue",
            "pid": "1234",
        }]
        with patch.object(acw, "_load_sessions", return_value=sessions):
            with patch.object(acw, "_build_pane_window_map", return_value={"%7": "0:7:formal"}):
                with patch.object(
                    acw,
                    "_build_thread_pane_map",
                    side_effect=AssertionError("summary status should not scan all panes"),
                ):
                    with patch.object(acw, "watcher_rows", return_value=live_rows):
                        with patch.object(acw, "_read_state_json", return_value={}):
                            with patch.object(acw, "_thread_times", return_value=("-", "-")):
                                with patch.object(acw, "_last_agent_snippet_for_pane", return_value="Ran tests"):
                                    with patch.object(acw, "_is_pid_stopped", return_value=False):
                                        out = io.StringIO()
                                        with redirect_stdout(out):
                                            acw.cmd_status([])
        text = out.getvalue()
        self.assertIn("Sessions: 1", text)
        self.assertIn("Ran tests", text)

    def test_status_summary_prints_doctor_recommendation_for_warn(self):
        sessions = [{
            "thread_id": THREAD,
            "name": "formal",
            "message": "continue",
            "state_file": "/state/acw_session.json",
        }]
        live_rows = [{
            "pane": "%7",
            "thread": THREAD,
            "state": "/state/acw_session.json",
            "watch": "/state/watch.log",
            "msg_file": "",
            "msg_inline": "continue",
            "pid": "1234",
        }]
        with patch.object(acw, "_load_sessions", return_value=sessions):
            with patch.object(acw, "_build_pane_window_map", return_value={"%7": "0:7:formal"}):
                with patch.object(
                    acw,
                    "_build_thread_pane_map",
                    side_effect=AssertionError("summary status should not scan all panes"),
                ):
                    with patch.object(acw, "watcher_rows", return_value=live_rows):
                        with patch.object(
                            acw,
                            "_read_state_json",
                            return_value={"health": "warn", "health_detail": "codex log missing"},
                        ):
                            with patch.object(acw, "_thread_times", return_value=("-", "-")):
                                with patch.object(acw, "_last_agent_snippet_for_pane", return_value="Ran tests"):
                                    with patch.object(acw, "_is_pid_stopped", return_value=False):
                                        out = io.StringIO()
                                        with redirect_stdout(out):
                                            acw.cmd_status([])
        text = out.getvalue()
        self.assertIn("Recommendation: run acw doctor formal", text)

    def test_status_summary_falls_back_without_rich(self):
        sessions = [{
            "thread_id": THREAD,
            "name": "formal",
            "message": "continue",
            "state_file": "/state/acw_session.json",
        }]
        live_rows = [{
            "pane": "%7",
            "thread": THREAD,
            "state": "/state/acw_session.json",
            "watch": "/state/watch.log",
            "msg_file": "",
            "msg_inline": "continue",
            "pid": "1234",
        }]
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name.startswith("rich"):
                raise ModuleNotFoundError("No module named 'rich'")
            return real_import(name, globals, locals, fromlist, level)

        with patch.object(acw, "_load_sessions", return_value=sessions):
            with patch.object(acw, "_build_pane_window_map", return_value={"%7": "0:7:formal"}):
                with patch.object(
                    acw,
                    "_build_thread_pane_map",
                    side_effect=AssertionError("summary status should not scan all panes"),
                ):
                    with patch.object(acw, "watcher_rows", return_value=live_rows):
                        with patch.object(
                            acw,
                            "_read_state_json",
                            return_value={"health": "warn", "health_detail": "codex log missing"},
                        ):
                            with patch.object(acw, "_thread_times", return_value=("-", "-")):
                                with patch.object(acw, "_last_agent_snippet_for_pane", return_value="Ran tests"):
                                    with patch.object(acw, "_is_pid_stopped", return_value=False):
                                        out = io.StringIO()
                                        with patch("builtins.__import__", side_effect=fake_import):
                                            with redirect_stdout(out):
                                                acw.cmd_status([])
        text = out.getvalue()
        self.assertIn("WINDOW/PANE", text)
        self.assertIn("LAST_AGENT", text)
        self.assertIn("Ran tests", text)
        self.assertIn("warn / codex log missing", text)

    def test_status_table_plain_adds_separator_every_third_row(self):
        rows = [
            ("w1", "running", "-", "-", "a1", "m1"),
            ("w2", "running", "-", "-", "a2", "m2"),
            ("w3", "running", "-", "-", "a3", "m3"),
            ("w4", "running", "-", "-", "a4", "m4"),
        ]
        out = io.StringIO()
        with redirect_stdout(out):
            acw._status_table_plain(rows)
        lines = out.getvalue().splitlines()
        separator_count = sum(1 for line in lines if set(line) <= {"-", " "} and "-" in line)
        self.assertGreaterEqual(separator_count, 2)

    def test_resolve_thread_id_fails_when_unknown(self):
        with patch.object(acw, "detect_thread_id_for_pane", return_value=None):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    acw.resolve_thread_id("%1", "")

    def test_resolve_thread_id_fails_for_auto_when_unknown(self):
        with patch.object(acw, "detect_thread_id_for_pane", return_value=None):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    acw.resolve_thread_id("%1", "auto")

    def test_resolve_start_pane_target_rejects_thread_id(self):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit):
                acw.resolve_start_pane_target(THREAD)
        self.assertIn("start target must be", err.getvalue())

    def test_resolve_start_pane_target_uses_window_name(self):
        with patch.object(acw, "resolve_pane_from_window_name", return_value="%7"):
            self.assertEqual("%7", acw.resolve_start_pane_target("formal"))

    def test_watcher_rows_filter_to_current_tmux_socket(self):
        ps_out = (
            "101 python3 /repo/bin/auto_continue_logwatch.py --pane %0 "
            "--thread-id 11111111-1111-1111-1111-111111111111 --tmux-socket /tmp/other.sock\n"
            "202 python3 /repo/bin/auto_continue_logwatch.py --pane %0 "
            "--thread-id 22222222-2222-2222-2222-222222222222 --tmux-socket /tmp/current.sock\n"
        )
        with patch.dict(acw.os.environ, {"AUTO_CONTINUE_TMUX_SOCKET": "/tmp/current.sock"}, clear=False):
            with patch.object(acw.subprocess, "check_output", return_value=ps_out):
                rows = acw.watcher_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("202", rows[0]["pid"])
        self.assertEqual("/tmp/current.sock", rows[0]["tmux_socket"])

    def test_doctor_checks_current_pane_and_thread(self):
        with patch.dict(acw.os.environ, {"TMUX_PANE": "%7"}, clear=False):
            with patch.object(acw, "_state_dir_is_writable", return_value=(True, acw.STATE_DIR)):
                with patch.object(acw, "_codex_auth_state_available", return_value=(True, "/tmp/auth.json")):
                    with patch.object(acw, "run_tmux", return_value="session\n"):
                        with patch.object(acw, "detect_thread_id_for_pane", return_value=THREAD):
                            with patch.object(acw, "watcher_rows", return_value=[]):
                                report = acw._doctor_report("")
        self.assertEqual(0, report.exit_code)
        rendered = "\n".join(f"{level}:{msg}" for level, msg in report.checks)
        self.assertIn("ok:tmux server reachable", rendered)
        self.assertIn("ok:pane resolved: %7", rendered)
        self.assertIn(f"ok:Codex thread detected: {THREAD}", rendered)

    def test_doctor_report_recommends_restart_for_warn_watcher(self):
        row = {
            "pane": "%7",
            "thread": THREAD,
            "state": "/state/acw_session.json",
            "watch": "/state/watch.log",
            "msg_file": "",
            "msg_inline": "continue",
            "pid": "1234",
        }
        with patch.object(acw, "_state_dir_is_writable", return_value=(True, acw.STATE_DIR)):
            with patch.object(acw, "_codex_auth_state_available", return_value=(True, "/tmp/auth.json")):
                with patch.object(acw, "run_tmux", return_value="session\n"):
                    with patch.object(acw, "_doctor_resolve_target", return_value=("%7", "")):
                        with patch.object(acw, "detect_thread_id_for_pane", return_value=THREAD):
                            with patch.object(acw, "watcher_rows", return_value=[row]):
                                with patch.object(
                                    acw,
                                    "_read_state_json",
                                    return_value={"health": "warn", "health_detail": "rollout channel closed"},
                                ):
                                    report = acw._doctor_report("uvm")
        self.assertEqual("warn", report.result)
        self.assertEqual(0, report.exit_code)
        self.assertIn("acw restart uvm", report.recommendations)
        rendered = "\n".join(f"{level}:{msg}" for level, msg in report.checks)
        self.assertIn("warn:watcher health: warn - rollout channel closed", rendered)

    def test_doctor_plain_output_shows_recommended_command(self):
        report = acw.DoctorReport(
            checks=[("warn", "watcher health: warn - rollout channel closed")],
            result="warn",
            exit_code=0,
            recommendations=["acw restart uvm"],
        )
        out = io.StringIO()
        with redirect_stdout(out):
            acw._print_doctor_plain(report)
        text = out.getvalue()
        self.assertIn("Recommendation: run acw restart uvm", text)
        self.assertIn("RESULT: warn", text)

    def test_doctor_skips_pane_checks_without_target_or_current_pane(self):
        with patch.dict(acw.os.environ, {}, clear=True):
            with patch.object(acw, "_state_dir_is_writable", return_value=(True, acw.STATE_DIR)):
                with patch.object(acw, "_codex_auth_state_available", return_value=(True, "/tmp/auth.json")):
                    with patch.object(acw, "run_tmux", return_value="session\n"):
                        report = acw._doctor_report("")
        self.assertEqual(0, report.exit_code)
        rendered = "\n".join(f"{level}:{msg}" for level, msg in report.checks)
        self.assertIn("info:pane checks skipped", rendered)

    def test_doctor_rejects_explicit_dot_target(self):
        report = acw._doctor_report(".")
        self.assertEqual(1, report.exit_code)
        rendered = "\n".join(f"{level}:{msg}" for level, msg in report.checks)
        self.assertIn("error:could not resolve target '.'", rendered)

    def test_load_sessions_reads_thread_keyed_state(self):
        session_file = f"/state/acw_session.{THREAD}.json"
        with patch.object(acw.glob, "glob", return_value=[session_file]):
            with patch("builtins.open", mock_open(read_data=json.dumps({
                "thread_id": THREAD,
                "name": "aot",
                "message": "continue",
            }))):
                sessions = acw._load_sessions()
        self.assertEqual(1, len(sessions))
        self.assertEqual("aot", sessions[0]["name"])
        self.assertEqual("continue", sessions[0]["message"])
        self.assertEqual(session_file, sessions[0]["state_file"])

    def test_load_sessions_ignores_invalid_thread_ids(self):
        with patch.object(acw.glob, "glob", return_value=["/state/acw_session.bad.json"]):
            with patch("builtins.open", mock_open(read_data=json.dumps({
                "thread_id": "bad",
                "name": "oops",
            }))):
                sessions = acw._load_sessions()
        self.assertEqual([], sessions)

    def test_window_renamed_hook_updates_session_name(self):
        def fake_tmux(*args):
            if args[:3] == ("list-panes", "-t", "@3"):
                return "%9\n"
            return None

        with patch.object(acw, "run_tmux", side_effect=fake_tmux):
            with patch.object(acw, "watcher_rows", return_value=[{"thread": THREAD}]):
                with patch.object(acw, "_write_session_state") as write_state:
                    acw.cmd_window_renamed(["@3", "new-name"])

        write_state.assert_called_once_with(THREAD, {"thread_id": THREAD, "name": "new-name"})

    def test_cleanup_rejects_target(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                acw.cmd_cleanup(["aot"])

    def test_cleanup_stale_files_removes_dead_session_state(self):
        session_file = f"/state/acw_session.{THREAD}.json"

        def fake_glob(pattern):
            if pattern.endswith("auto_continue_logwatch.*.pid"):
                return []
            if pattern.endswith("auto_continue_logwatch*.log"):
                return []
            if pattern.endswith("auto_continue_logwatch*.runner.log"):
                return []
            if pattern.endswith("acw_session.*.json"):
                return [session_file]
            return []

        with patch.object(acw.glob, "glob", side_effect=fake_glob):
            with patch.object(acw, "watcher_rows", return_value=[]):
                with patch.object(acw.Path, "unlink") as unlink:
                    acw.cleanup_stale_files()
        unlink.assert_called_once_with(missing_ok=True)

    def test_build_thread_pane_map_uses_tmux_process_tree(self):
        def fake_tmux(*args):
            if args[:4] == ("list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"):
                return "%7\t1234\n"
            return None

        with patch.object(acw, "run_tmux", side_effect=fake_tmux):
            with patch.object(acw, "_threads_from_pstree", return_value=[THREAD]):
                mapping = acw._build_thread_pane_map()
        self.assertEqual("%7", mapping[THREAD])

    def test_run_tmux_avoids_cross_server_fallback_when_client_healthy(self):
        with patch.dict(acw.os.environ, {"TMUX": "/tmp/tmux-1/default,1,0"}, clear=False):
            with patch.object(acw, "_tmux_client_env_healthy", return_value=True):
                with patch.object(
                    acw.subprocess,
                    "check_output",
                    side_effect=subprocess.CalledProcessError(1, ["tmux"]),
                ) as chk:
                    out = acw.run_tmux("list-sessions")
        self.assertIsNone(out)
        self.assertGreaterEqual(chk.call_count, 1)

    def test_run_tmux_falls_back_when_client_unhealthy(self):
        with patch.dict(acw.os.environ, {"TMUX": "/tmp/tmux-1/default,1,0"}, clear=False):
            with patch.object(acw, "_tmux_client_env_healthy", return_value=False):
                with patch.object(
                    acw.subprocess,
                    "check_output",
                    side_effect=[
                        subprocess.CalledProcessError(1, ["tmux"]),
                        "ok",
                    ],
                ):
                    out = acw.run_tmux("list-sessions")
        self.assertEqual("ok", out)

    def test_run_tmux_stale_socket_fallback_clears_tmux_env(self):
        calls = []

        def fake_check_output(cmd, stderr=None, text=None, env=None):
            calls.append((cmd, env))
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, cmd)
            return "ok"

        env = {
            "TMUX": "/tmp/tmux-1/default,1,0",
            "TMUX_PANE": "%9",
            "PATH": "/usr/bin",
        }
        with patch.dict(acw.os.environ, env, clear=True):
            with patch.object(acw.subprocess, "check_output", side_effect=fake_check_output):
                out = acw.run_tmux("list-sessions")

        self.assertEqual("ok", out)
        self.assertEqual(["tmux", "-S", "/tmp/tmux-1/default", "list-sessions"], calls[0][0])
        self.assertEqual(["tmux", "list-sessions"], calls[1][0])
        self.assertNotIn("TMUX", calls[1][1])
        self.assertNotIn("TMUX_PANE", calls[1][1])
        self.assertEqual("/usr/bin", calls[1][1]["PATH"])

    def test_tmux_socket_recovery_hint_uses_live_tmux_pid(self):
        with patch.dict(acw.os.environ, {"TMUX": "/tmp/tmux-1013/default,22,0"}, clear=False):
            with patch.object(acw.os.path, "exists", return_value=False):
                with patch.object(
                    acw.subprocess,
                    "check_output",
                    return_value="1996933 tmux\n",
                ):
                    hint = acw._tmux_socket_recovery_hint()
        self.assertIn("kill -USR1 1996933", hint)
        self.assertIn("/tmp/tmux-1013/default", hint)

    def test_resolve_pane_target_prints_tmux_recovery_hint_for_window_name(self):
        with patch.object(acw, "run_tmux", return_value=None):
            with patch.object(
                acw,
                "_tmux_socket_recovery_hint",
                return_value="hint: tmux socket is unreachable; recreate it with `kill -USR1 1996933`",
            ):
                err = io.StringIO()
                with redirect_stderr(err):
                    with self.assertRaises(SystemExit):
                        acw.resolve_pane_target("four")
        text = err.getvalue()
        self.assertIn("tmux server is unavailable", text)
        self.assertIn("kill -USR1 1996933", text)

    def test_resolve_pane_target_rejects_thread_id(self):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit):
                acw.resolve_pane_target(THREAD)
        self.assertIn("use a pane id", err.getvalue())

    def test_status_uses_live_watcher_rows_when_tmux_metadata_is_unavailable(self):
        sessions = [{
            "thread_id": THREAD,
            "name": "formal",
            "message": "continue",
            "state_file": "/state/acw_session.json",
        }]
        live_rows = [{
            "pane": "%7",
            "thread": THREAD,
            "state": "/state/acw_session.json",
            "watch": "/state/watch.log",
            "msg_file": "",
            "msg_inline": "continue",
            "pid": "1234",
        }]
        with patch.object(acw, "_load_sessions", return_value=sessions):
            with patch.object(acw, "_build_pane_window_map", return_value={}):
                with patch.object(acw, "_build_thread_pane_map", return_value={}):
                    with patch.object(acw, "watcher_rows", return_value=live_rows):
                        with patch.object(acw, "_read_state_json", return_value={}):
                            with patch.object(acw, "_thread_times", return_value=("-", "-")):
                                with patch.object(acw, "_is_pid_stopped", return_value=False):
                                    out = io.StringIO()
                                    with redirect_stdout(out):
                                        acw.cmd_status(["--details"])
        text = out.getvalue()
        self.assertIn("PANE:            %7", text)
        self.assertIn("PID:             1234", text)
        self.assertIn("STATE:           running", text)
        self.assertIn("WINDOW:          formal", text)

    def test_status_prints_tmux_recovery_hint_when_metadata_is_unavailable(self):
        sessions = [{
            "thread_id": THREAD,
            "name": "formal",
            "message": "continue",
            "state_file": "/state/acw_session.json",
        }]
        with patch.object(acw, "_load_sessions", return_value=sessions):
            with patch.object(acw, "_build_pane_window_map", return_value={}):
                with patch.object(acw, "_build_thread_pane_map", return_value={}):
                    with patch.object(acw, "watcher_rows", return_value=[]):
                        with patch.object(
                            acw,
                            "_tmux_socket_recovery_hint",
                            return_value="hint: tmux socket is unreachable; recreate it with `kill -USR1 1996933`",
                        ):
                            err = io.StringIO()
                            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                                acw.cmd_status([])
        text = err.getvalue()
        self.assertIn("tmux metadata is unavailable", text)
        self.assertIn("kill -USR1 1996933", text)

    def test_tmux_socket_from_env(self):
        with patch.dict(acw.os.environ, {"TMUX": "/tmp/tmux-1013/default,22,0"}, clear=False):
            self.assertEqual("/tmp/tmux-1013/default", acw._tmux_socket_from_env())

    def test_pause_star_pauses_all_watchers(self):
        rows = [
            {"pane": "%1", "pid": "101"},
            {"pane": "%2", "pid": "202"},
        ]
        with patch.object(acw, "watcher_rows", return_value=rows):
            with patch.object(acw, "_is_pid_stopped", return_value=False):
                with patch.object(acw.os, "kill") as kill:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        acw.cmd_pause(["*"])
        self.assertEqual(
            [
                ((101, acw.signal.SIGSTOP),),
                ((202, acw.signal.SIGSTOP),),
            ],
            kill.call_args_list,
        )

    def test_resume_star_resumes_all_paused_watchers(self):
        rows = [
            {"pane": "%1", "pid": "101"},
            {"pane": "%2", "pid": "202"},
        ]
        with patch.object(acw, "watcher_rows", return_value=rows):
            with patch.object(acw, "_is_pid_stopped", side_effect=[True, False]):
                with patch.object(acw.os, "kill") as kill:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        acw.cmd_resume(["*"])
        kill.assert_called_once_with(101, acw.signal.SIGCONT)

    def test_restart_star_restarts_all_live_panes(self):
        rows = [
            {"pane": "%1", "pid": "101"},
            {"pane": "%2", "pid": "202"},
        ]

        def fake_tmux(*args):
            if args[:4] == ("display-message", "-p", "-t", "%1"):
                return "%1\n"
            if args[:4] == ("display-message", "-p", "-t", "%2"):
                return "%2\n"
            return None

        with patch.object(acw, "watcher_rows", return_value=rows):
            with patch.object(acw, "run_tmux", side_effect=fake_tmux):
                with patch.object(acw, "_restart_panes") as restart_panes:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        acw.cmd_restart(["*"])
        restart_panes.assert_called_once_with(["%1", "%2"])

    def test_restart_without_target_restarts_all_live_panes(self):
        rows = [
            {"pane": "%1", "pid": "101"},
            {"pane": "%2", "pid": "202"},
        ]

        def fake_tmux(*args):
            if args[:4] == ("display-message", "-p", "-t", "%1"):
                return "%1\n"
            if args[:4] == ("display-message", "-p", "-t", "%2"):
                return "%2\n"
            return None

        with patch.object(acw, "watcher_rows", return_value=rows):
            with patch.object(acw, "run_tmux", side_effect=fake_tmux):
                with patch.object(acw, "_restart_panes") as restart_panes:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        acw.cmd_restart([])
        restart_panes.assert_called_once_with(["%1", "%2"])


if __name__ == "__main__":
    unittest.main()
