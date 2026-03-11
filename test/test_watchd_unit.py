import io
import json
import subprocess
import unittest
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
    def test_short_thread_id_keeps_prefix_and_suffix(self):
        self.assertEqual("11111111…1111", acw._short_thread_id(THREAD))

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

    def test_select_session_files_matches_thread_prefix(self):
        sessions = [
            {"thread_id": THREAD, "name": "alpha", "state_file": "/tmp/a.json"},
            {"thread_id": "22222222-2222-2222-2222-222222222222", "name": "beta", "state_file": "/tmp/b.json"},
        ]
        selected = acw._select_session_files(sessions, THREAD[:8])
        self.assertEqual(THREAD, selected[0]["thread_id"])

    def test_cleanup_selector_removes_matched_state_file(self):
        session_file = f"/state/acw_session.{THREAD}.json"
        sessions = [{"thread_id": THREAD, "name": "aot", "state_file": session_file}]
        with patch.object(acw, "_load_sessions", return_value=sessions):
            with patch.object(acw.Path, "unlink") as unlink:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    acw.cmd_cleanup(["aot"])
        unlink.assert_called_once_with(missing_ok=True)

    def test_cleanup_selector_ambiguous_exits(self):
        t2 = "22222222-2222-2222-2222-222222222222"
        sessions = [
            {"thread_id": THREAD, "name": "aot", "state_file": f"/state/acw_session.{THREAD}.json"},
            {"thread_id": t2, "name": "aot", "state_file": f"/state/acw_session.{t2}.json"},
        ]
        with patch.object(acw, "_load_sessions", return_value=sessions):
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
                with patch.object(
                    acw,
                    "is_running_pid_file",
                    side_effect=lambda path: path == acw.LEGACY_PID_FILE,
                ):
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
                            with patch.object(acw, "_rollout_times", return_value=("-", "-")):
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
