import io
import json
import os
import sqlite3
import tempfile
import threading
import subprocess
import time
import unittest
import builtins
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import MagicMock
from unittest.mock import mock_open

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_rpc  # noqa: E402
import acw_state  # noqa: E402
import auto_continue_watchd as acw  # noqa: E402


THREAD = "11111111-1111-1111-1111-111111111111"
OTHER_THREAD = "22222222-2222-2222-2222-222222222222"
THIRD_THREAD = "33333333-3333-3333-3333-333333333333"
ORIGINAL_READ_RUNTIME_ROWS = acw._read_runtime_rows


def _session_record(
    thread_id: str,
    *,
    name: str = "formal",
    message: str = "continue",
    state_file: str = "/state/acw_session.json",
    health: str = "",
    health_detail: str = "",
    health_ts: str = "",
    last_continue_at: str = "",
) -> acw.SavedSessionRecord:
    return acw.SavedSessionRecord(
        thread_id=thread_id.lower(),
        name=name,
        message=message,
        state_file=state_file,
        health=health,
        health_detail=health_detail,
        health_ts=health_ts,
        last_continue_at=last_continue_at,
    )


def _watcher_record(
    pane: str,
    thread_id: str,
    *,
    pid: str = "1234",
    state_file: str = "/state/acw_session.json",
    watch_log: str = "/state/watch.log",
    cwd: str = "/repo",
    message_inline: str = "continue",
    tmux_socket: str = "",
) -> acw.LiveWatcherRecord:
    return acw.LiveWatcherRecord(
        pid=pid,
        pane=pane,
        thread_id=thread_id.lower(),
        state_file=state_file,
        watch_log=watch_log,
        cwd=cwd,
        message_inline=message_inline,
        tmux_socket=tmux_socket,
    )


def _pane_record(
    pane_id: str,
    thread_id: str,
    *,
    window_label: str = "0:7:formal",
    window_name: str = "formal",
    shell_pid: str = "7777",
    detected_thread_source: str = "pid",
    detected_thread_detail: str = "",
    has_codex_process: bool = True,
) -> acw.LivePaneRecord:
    return acw.LivePaneRecord(
        pane_id=pane_id,
        window_label=window_label,
        window_name=window_name,
        shell_pid=shell_pid,
        detected_thread=thread_id.lower(),
        detected_thread_source=detected_thread_source if thread_id else "",
        detected_thread_detail=detected_thread_detail,
        has_codex_process=has_codex_process,
    )


def _snapshot(
    *,
    panes: tuple[acw.LivePaneRecord, ...] = (),
    watchers: tuple[acw.LiveWatcherRecord, ...] = (),
    sessions: tuple[acw.SavedSessionRecord, ...] = (),
) -> acw.RuntimeSnapshot:
    return acw.RuntimeSnapshot(
        panes=tuple(panes),
        watchers=tuple(watchers),
        sessions=tuple(sessions),
    )


class WatchdUnitTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._state_dir_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_dir_tmp.cleanup)

        def _daemon_upsert_stub(
            thread_id,
            *,
            pane,
            name,
            message,
            paused,
            generation,
            extra_session=None,
            timeout_secs=5.0,
        ):
            payload = {
                "thread_id": thread_id,
                "pane": pane,
                "name": name,
                "message": message,
                "paused": paused,
                "generation": generation,
            }
            if extra_session:
                payload.update(extra_session)
            acw._write_session_state(thread_id, payload)
            return {
                "thread": thread_id,
                "pane": pane,
                "runtime_status": "paused" if paused else "running",
                "generation": str(generation),
                "paused": "1" if paused else "",
                "runtime_id": "runtime-9999",
                "daemon_pid": "9999",
            }

        def _daemon_patch_stub(thread_id, changes, *, wait=None, timeout_secs=5.0):
            acw._write_session_state(thread_id, changes)
            state = acw._read_session_state(thread_id)
            if isinstance(wait, dict) and wait.get("kind") == "absent":
                return {}
            pane = str(state.get("pane", changes.get("pane", "%0"))).strip() or "%0"
            paused = bool(state.get("paused", changes.get("paused", False)))
            generation = int(state.get("generation", changes.get("generation", 0)) or 0)
            statuses = wait.get("statuses", []) if isinstance(wait, dict) else []
            runtime_status = str(statuses[0]).strip() if statuses else ("paused" if paused else "running")
            return {
                "thread": thread_id,
                "pane": pane,
                "runtime_status": runtime_status,
                "generation": str(generation),
                "paused": "1" if paused else "",
                "runtime_id": "runtime-9999",
                "daemon_pid": "9999",
            }

        def _daemon_remove_stub(thread_id, *, timeout_secs=2.0):
            acw._remove_session_state(thread_id)

        def _daemon_restart_codex_stub(pane, thread_id, *, timeout_secs=10.0):
            return None

        self._patchers = [
            patch.object(acw, "STATE_DIR", self._state_dir_tmp.name),
            patch.object(acw, "_read_runtime_rows", return_value=[]),
            patch.object(acw, "_daemon_rpc_upsert_session", side_effect=_daemon_upsert_stub),
            patch.object(acw, "_daemon_rpc_patch_session", side_effect=_daemon_patch_stub),
            patch.object(acw, "_daemon_rpc_remove_session", side_effect=_daemon_remove_stub),
            patch.object(acw, "_daemon_rpc_restart_codex", side_effect=_daemon_restart_codex_stub),
        ]
        for patcher in self._patchers:
            patcher.start()
        self.addCleanup(self._stop_patchers)

    def _stop_patchers(self):
        for patcher in reversed(getattr(self, "_patchers", [])):
            patcher.stop()

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

    def test_main_help_uses_invoked_program_name(self):
        out = io.StringIO()
        with patch.object(acw.sys, "argv", ["acw", "--help"]):
            with redirect_stdout(out):
                with self.assertRaises(SystemExit):
                    acw.main()
        text = out.getvalue()
        self.assertIn("Usage:\n  acw\n", text)
        self.assertIn("  acw status [target] [--details]", text)

    def test_help_text_no_longer_mentions_migrate(self):
        text = acw.build_help_text("acw")
        self.assertNotIn("migrate", text)

    def test_help_text_uses_managed_session_terms(self):
        text = acw.build_help_text("acw")
        self.assertIn("Show managed session state", text)
        self.assertIn("Start managing a live tmux target", text)
        self.assertNotIn("Show watcher state", text)
        self.assertNotIn("Start a watcher", text)

    def test_daemon_socket_file_for_long_tmux_scope_stays_within_unix_limit(self):
        long_scope = (
            "/home/thomas-ahle/.codex/auto-continue-e2e-tmp/"
            "real-codex-s2u7nxzm/tmux/tmux.sock"
        )
        long_state_dir = (
            "/home/thomas-ahle/.codex/auto-continue-e2e-tmp/"
            "real-codex-s2u7nxzm/home/.codex"
        )
        with patch.object(acw, "STATE_DIR", long_state_dir):
            socket_path = acw.daemon_socket_file_for_scope(long_scope)

        self.assertTrue(socket_path.endswith(".sock"))
        self.assertLess(len(socket_path), 100)

    def test_parse_thread_and_message_args_rejects_message_file(self):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                acw.parse_thread_and_message_args(["--message-file", "/tmp/msg.txt"])
        self.assertEqual(1, ctx.exception.code)
        self.assertIn("unknown option '--message-file'", err.getvalue())

    def test_daemon_rpc_transport_streams_progress_and_returns_payload(self):
        with tempfile.TemporaryDirectory() as td:
            socket_path = os.path.join(td, "acw.sock")
            server = acw_rpc.listen(socket_path)
            self.addCleanup(server.close)

            def serve_once():
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    handled = acw_rpc.serve_pending(
                        server,
                        handle_request=lambda request, send_progress: (
                            send_progress(f"progress:{request['command']}") or {"ok": True}
                        ),
                    )
                    if handled:
                        return
                    time.sleep(0.01)

            worker = threading.Thread(target=serve_once)
            worker.start()
            out = io.StringIO()
            with redirect_stdout(out):
                payload = acw_rpc.call(
                    socket_path,
                    {"command": "ping", "params": {}},
                    on_progress=acw._print_progress,
                    timeout_secs=2.0,
                )
            worker.join(timeout=2.0)

        self.assertEqual({"ok": True}, payload)
        self.assertIn("progress:ping", out.getvalue())

    def test_daemon_rpc_call_backfills_daemon_scope_state(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                with patch.object(acw, "_ensure_daemon_running", return_value="4321"):
                    with patch.object(acw.acw_rpc, "call", return_value={"ok": True}):
                        payload = acw._daemon_rpc_call("ping", {}, timeout_secs=0.5)
                scope_state = acw._read_daemon_scope_state()

        self.assertEqual("4321", payload["daemon_pid"])
        self.assertEqual("4321", scope_state["daemon_pid"])
        self.assertTrue(scope_state["socket_path"].endswith("acw_daemon.default.sock"))
        self.assertNotEqual("", scope_state["heartbeat_ts"])

    def test_daemon_handle_upsert_session_request_reconciles_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            log_tail = acw._DaemonCodexLogTail(log_path=Path(td) / "codex-tui.log")
            try:
                with patch.object(acw, "STATE_DIR", td):
                    with patch.object(acw, "_compute_watch_health", return_value=("ok", "")):
                        payload = acw._daemon_handle_rpc_request(
                            {
                                "command": "upsert_session",
                                "params": {
                                    "thread_id": THREAD,
                                    "session": {
                                        "thread_id": THREAD,
                                        "pane": "%7",
                                        "name": "formal",
                                        "message": "continue",
                                        "paused": False,
                                        "generation": 3,
                                        "tmux_socket": "",
                                    },
                                    "wait": {
                                        "kind": "runtime",
                                        "pane": "%7",
                                        "generation": 3,
                                        "paused": False,
                                        "statuses": ["running", "paused"],
                                    },
                                    "timeout_secs": 1.0,
                                },
                            },
                            scope="",
                            log_tail=log_tail,
                            runtimes={},
                            send_progress=lambda _message: None,
                        )
                    state = acw._read_session_state(THREAD)
            finally:
                log_tail.close()

        self.assertEqual("%7", payload["row"]["pane"])
        self.assertEqual("running", payload["row"]["runtime_status"])
        self.assertEqual("%7", state["pane"])
        self.assertEqual("formal", state["name"])

    def test_sqlite_store_migrates_legacy_tables_into_agent_schema(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = acw_state.state_db_path(td)
            alive_pid = str(os.getpid())
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE sessions (
                        thread_id TEXT PRIMARY KEY,
                        pane TEXT NOT NULL DEFAULT '',
                        name TEXT NOT NULL DEFAULT '',
                        message TEXT NOT NULL DEFAULT '',
                        tmux_socket TEXT NOT NULL DEFAULT '',
                        paused INTEGER NOT NULL DEFAULT 0,
                        generation INTEGER NOT NULL DEFAULT 0,
                        health TEXT NOT NULL DEFAULT '',
                        health_detail TEXT NOT NULL DEFAULT '',
                        health_ts TEXT NOT NULL DEFAULT '',
                        last_continue_at TEXT NOT NULL DEFAULT '',
                        last_handled_turn TEXT NOT NULL DEFAULT '',
                        last_handled_thread TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE runtimes (
                        thread_id TEXT PRIMARY KEY,
                        runtime_id TEXT NOT NULL DEFAULT '',
                        daemon_pid TEXT NOT NULL DEFAULT '',
                        pane TEXT NOT NULL DEFAULT '',
                        state_ref TEXT NOT NULL DEFAULT '',
                        watch_log TEXT NOT NULL DEFAULT '',
                        cwd TEXT NOT NULL DEFAULT '',
                        message_inline TEXT NOT NULL DEFAULT '',
                        tmux_socket TEXT NOT NULL DEFAULT '',
                        paused INTEGER NOT NULL DEFAULT 0,
                        runtime_status TEXT NOT NULL DEFAULT '',
                        runtime_detail TEXT NOT NULL DEFAULT '',
                        generation INTEGER NOT NULL DEFAULT 0,
                        worker_label TEXT NOT NULL DEFAULT '',
                        heartbeat_ts TEXT NOT NULL DEFAULT ''
                    );
                    """
                )
                conn.execute(
                    """
                    INSERT INTO sessions(
                        thread_id, pane, name, message, tmux_socket, paused, generation,
                        health, health_detail, health_ts, last_continue_at,
                        last_handled_turn, last_handled_thread
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        THREAD,
                        "%7",
                        "formal",
                        "continue by docs/FORMAL_PLAN.md",
                        "",
                        0,
                        3,
                        "ok",
                        "",
                        "",
                        "2026-03-14 12:00:00",
                        "turn-7",
                        THREAD,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO runtimes(
                        thread_id, runtime_id, daemon_pid, pane, state_ref, watch_log, cwd,
                        message_inline, tmux_socket, paused, runtime_status, runtime_detail,
                        generation, worker_label, heartbeat_ts
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        THREAD,
                        "runtime-7",
                        alive_pid,
                        "%7",
                        "sqlite:session:11111111-1111-1111-1111-111111111111",
                        "/state/watch.log",
                        "/repo",
                        "continue by docs/FORMAL_PLAN.md",
                        "",
                        0,
                        "running",
                        "",
                        3,
                        "runtime-7",
                        "2026-03-14 12:01:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            sessions = acw_state.load_sessions(state_dir=td, scope="")
            runtimes = acw_state.read_runtime_rows(state_dir=td, scope="")

            conn = sqlite3.connect(db_path)
            try:
                agent_row = conn.execute(
                    "SELECT agent_id, thread_id, pane, name, message FROM agents"
                ).fetchone()
                runtime_row = conn.execute(
                    "SELECT agent_id, runtime_id, generation FROM agent_runtimes"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(1, len(sessions))
        self.assertEqual(1, len(runtimes))
        self.assertIsNotNone(agent_row)
        self.assertEqual(THREAD, agent_row[1])
        self.assertEqual("%7", agent_row[2])
        self.assertEqual("formal", agent_row[3])
        self.assertEqual("continue by docs/FORMAL_PLAN.md", agent_row[4])
        self.assertIsNotNone(runtime_row)
        self.assertEqual("runtime-7", runtime_row[1])
        self.assertEqual(3, runtime_row[2])

    def test_sqlite_store_enforces_unique_scope_pane_and_thread_per_agent(self):
        acw._write_session_state(
            THREAD,
            {
                "thread_id": THREAD,
                "pane": "%7",
                "name": "formal",
                "message": "continue",
                "tmux_socket": "",
            },
        )
        db_path = acw_state.state_db_path(acw.STATE_DIR)
        conn = sqlite3.connect(db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO agents(
                        agent_id, thread_id, pane, name, message, tmux_socket,
                        paused, generation, health, health_detail, health_ts,
                        last_continue_at, last_handled_turn, last_handled_thread
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "agent-other-pane",
                        OTHER_THREAD,
                        "%7",
                        "aot",
                        "continue",
                        "",
                        0,
                        0,
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO agents(
                        agent_id, thread_id, pane, name, message, tmux_socket,
                        paused, generation, health, health_detail, health_ts,
                        last_continue_at, last_handled_turn, last_handled_thread
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "agent-other-thread",
                        THREAD,
                        "%8",
                        "aot",
                        "continue",
                        "",
                        0,
                        0,
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ),
                )
        finally:
            conn.close()

    def test_sqlite_connect_skips_wal_pragma_after_initial_setup(self):
        fake_conn = MagicMock()
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw_state.sqlite3, "connect", return_value=fake_conn):
                with patch.object(acw_state, "_ensure_schema"):
                    with patch.object(acw_state, "_maybe_migrate_legacy_json"):
                        with patch.object(acw_state, "_maybe_migrate_legacy_sqlite_tables"):
                            with patch.object(acw_state, "_meta_value", return_value="1"):
                                conn = acw_state._connect(td)

        self.assertIs(fake_conn, conn)
        executed = [call.args[0] for call in fake_conn.execute.call_args_list]
        self.assertNotIn("PRAGMA journal_mode=WAL", executed)

    def test_sqlite_connect_survives_operational_error_enabling_wal(self):
        fake_conn = MagicMock()

        def execute(sql, *args, **kwargs):
            if sql == "PRAGMA journal_mode=WAL":
                raise sqlite3.OperationalError("unable to open database file")
            result = MagicMock()
            result.fetchone.return_value = None
            return result

        fake_conn.execute.side_effect = execute

        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw_state.sqlite3, "connect", return_value=fake_conn):
                with patch.object(acw_state, "_ensure_schema"):
                    with patch.object(acw_state, "_maybe_migrate_legacy_json"):
                        with patch.object(acw_state, "_maybe_migrate_legacy_sqlite_tables"):
                            with patch.object(acw_state, "_meta_value", return_value=""):
                                with patch.object(acw_state, "_set_meta_value") as set_meta:
                                    conn = acw_state._connect(td)

        self.assertIs(fake_conn, conn)
        set_meta.assert_not_called()

    def test_short_thread_id_keeps_prefix_and_suffix(self):
        self.assertEqual("11111111…1111", acw._short_thread_id(THREAD))

    def test_status_window_pane_cell_splits_thread_and_branch_lines(self):
        self.assertEqual(
            "0:10:aot2/%10\n[dim]11111111…1111[/dim]\n[dim]br=main[/dim]",
            acw._status_window_pane_cell("0:10:aot2", "%10", THREAD, "main"),
        )

    def test_format_age_compact_omits_ago_suffix(self):
        with patch.object(acw.time, "time", return_value=10_000.0):
            self.assertEqual("1h02m", acw._format_age(10_000.0 - 3720.0, compact=True))

    def test_status_flex_widths_prefers_last_agent_column(self):
        last_agent_col_w, msg_col_w = acw._status_flex_widths(120, [16, 7, 7, 8])
        self.assertGreater(last_agent_col_w, msg_col_w)
        self.assertEqual(63, last_agent_col_w + msg_col_w)

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

    def test_resolve_message_prefers_persisted_session_message_over_ps_inline(self):
        row = {
            "thread": THREAD,
            "msg_inline": "continue",
        }
        with patch.object(acw, "_read_session_state", return_value={"message": "continue by aot_plan_7.md"}):
            self.assertEqual(("inline", "continue by aot_plan_7.md"), acw._resolve_message(row))

    def test_message_summary_shows_prompt_without_msg_prefix(self):
        row = {
            "thread": THREAD,
            "msg_inline": "continue",
        }
        with patch.object(acw, "_read_session_state", return_value={"message": "continue by docs/DDR3_PLAN.md"}):
            self.assertEqual("continue by docs/DDR3_PLAN.md", acw._message_summary_for_row(row))

    def test_read_state_json_reads_sqlite_session_ref(self):
        acw._write_session_state(
            THREAD,
            {
                "thread_id": THREAD,
                "health": "warn",
                "health_detail": "codex log missing",
                "health_ts": "2026-03-15 12:00:00",
                "last_continue_at": "2026-03-15 12:34:56",
            },
        )

        data = acw._read_state_json(acw_state.session_state_ref(THREAD))

        self.assertEqual(
            {
                "continue_count": "0",
                "health": "warn",
                "health_detail": "codex log missing",
                "health_ts": "2026-03-15 12:00:00",
                "last_continue_at": "2026-03-15 12:34:56",
                "thread_id": THREAD,
            },
            data,
        )

    def test_status_summary_reports_shared_daemon_failure_once(self):
        snapshot = _snapshot(
            panes=(
                _pane_record("%6", THREAD, window_label="0:6:formal", window_name="formal"),
                _pane_record("%7", OTHER_THREAD, window_label="0:7:aot", window_name="aot"),
            ),
            sessions=(
                _session_record(THREAD, name="formal"),
                _session_record(OTHER_THREAD, name="aot", state_file="/state/acw_session.other.json"),
            ),
        )
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
            with patch.object(acw, "_read_state_json", return_value={}):
                with patch.object(acw, "_thread_times", return_value=("-", "-")):
                    with patch.object(acw, "_last_agent_snippet_for_pane", return_value="Ran tests"):
                        with patch.object(acw, "_is_pid_stopped", return_value=False):
                            with patch.object(
                                acw,
                                "_read_daemon_scope_state",
                                create=True,
                                return_value={
                                    "daemon_pid": "9999",
                                    "socket_path": "/tmp/acw.sock",
                                    "heartbeat_ts": "2026-03-14 12:00:00",
                                    "alive": False,
                                    "detail": "pid file exists but process is dead",
                                },
                            ):
                                out = io.StringIO()
                                with redirect_stdout(out):
                                    acw.cmd_status([])
        text = out.getvalue()
        self.assertIn("Shared daemon error:", text)
        self.assertNotIn("live Codex pane %6 has thread", text)
        self.assertNotIn("live Codex pane %7 has thread", text)

    def test_status_summary_reports_shared_daemon_failure_without_scope_row(self):
        snapshot = _snapshot(
            panes=(
                _pane_record("%6", THREAD, window_label="0:6:formal", window_name="formal"),
                _pane_record("%7", OTHER_THREAD, window_label="0:7:aot", window_name="aot"),
            ),
            sessions=(
                _session_record(THREAD, name="formal"),
                _session_record(OTHER_THREAD, name="aot", state_file="/state/acw_session.other.json"),
            ),
        )
        with tempfile.TemporaryDirectory() as td:
            Path(td, "acw_daemon.default.pid").write_text("999999\n", encoding="utf-8")
            with patch.object(acw, "STATE_DIR", td):
                with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
                    with patch.object(acw, "_read_state_json", return_value={}):
                        with patch.object(acw, "_thread_times", return_value=("-", "-")):
                            with patch.object(acw, "_last_agent_snippet_for_pane", return_value="Ran tests"):
                                with patch.object(acw, "_is_pid_stopped", return_value=False):
                                    with patch.object(acw, "_read_daemon_scope_state", return_value={}):
                                        out = io.StringIO()
                                        with redirect_stdout(out):
                                            acw.cmd_status([])
        text = out.getvalue()
        self.assertIn("Shared daemon error:", text)
        self.assertIn("pid file points to dead daemon pid 999999", text)
        self.assertNotIn("live Codex pane %6 has thread", text)
        self.assertNotIn("live Codex pane %7 has thread", text)

    def test_edit_prefers_persisted_session_message_over_stale_inline_message(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%2", THREAD, window_label="0:2:ddr3", window_name="ddr3"),),
            watchers=(_watcher_record("%2", THREAD, message_inline=acw.DEFAULT_MESSAGE_TEXT),),
            sessions=(_session_record(THREAD, name="ddr3", message="continue by docs/DDR3_PLAN.md"),),
        ))
        with patch.object(acw, "resolve_pane_target", return_value="%2"):
            with patch.object(acw, "_build_runtime_model", return_value=model):
                with patch.object(acw, "_edit_message_interactive", return_value=None) as edit_message:
                    acw.cmd_edit(["ddr3"])

        edit_message.assert_called_once_with("continue by docs/DDR3_PLAN.md")

    def test_status_sort_key_orders_by_numeric_window_index(self):
        rows = [
            ({"pane": "%10"}, "%10", "0:10:aot2"),
            ({"pane": "%1"}, "%1", "0:1:disc"),
            ({"pane": "%2"}, "%2", "0:2:ddr3"),
        ]
        ordered = sorted(rows, key=acw._status_sort_key)
        self.assertEqual(
            ["0:1:disc", "0:2:ddr3", "0:10:aot2"],
            [item[2] for item in ordered],
        )

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
        snapshot = _snapshot(
            panes=(_pane_record("%7", THREAD, window_label="0:7:formal", window_name="formal"),),
            watchers=(_watcher_record("%7", THREAD),),
            sessions=(_session_record(THREAD, name="formal"),),
        )
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
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

    def test_status_summary_includes_duplicate_live_watchers_hidden_by_thread_merge(self):
        snapshot = _snapshot(
            panes=(
                _pane_record("%6", THREAD, window_label="0:6:formal", window_name="formal"),
                _pane_record("%10", THREAD, window_label="0:10:aot2", window_name="aot2"),
            ),
            watchers=(
                _watcher_record("%10", THREAD, pid="1010", watch_log="/state/watch10.log"),
                _watcher_record("%6", THREAD, pid="606", watch_log="/state/watch6.log"),
            ),
            sessions=(_session_record(THREAD, name="formal"),),
        )
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
            with patch.object(acw, "_read_state_json", return_value={}):
                with patch.object(acw, "_thread_times", return_value=("-", "-")):
                    with patch.object(acw, "_last_agent_snippet_for_pane", return_value="Ran tests"):
                        with patch.object(acw, "_is_pid_stopped", return_value=False):
                            out = io.StringIO()
                            with redirect_stdout(out):
                                acw.cmd_status([])
        text = out.getvalue()
        self.assertIn("Sessions: 2", text)
        self.assertIn("0:6:formal/%6", text)
        self.assertIn("0:10:aot2/%10", text)
        self.assertIn("Invariant error: thread 11111111…1111 has managed runtimes on panes %10, %6", text)
        self.assertIn("Hint: run acw repair --dry-run for live invariant fixes.", text)

    def test_status_summary_flags_duplicate_agent_name_on_multiple_threads(self):
        snapshot = _snapshot(
            panes=(
                _pane_record("%6", THREAD, window_label="0:6:formal", window_name="formal"),
                _pane_record("%7", OTHER_THREAD, window_label="0:7:formal", window_name="formal"),
            ),
            watchers=(
                _watcher_record("%6", THREAD, pid="606"),
                _watcher_record("%7", OTHER_THREAD, pid="707"),
            ),
            sessions=(
                _session_record(THREAD, name="formal"),
                _session_record(OTHER_THREAD, name="formal", state_file="/state/acw_session.other.json"),
            ),
        )
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
            with patch.object(acw, "_read_state_json", return_value={}):
                with patch.object(acw, "_thread_times", return_value=("-", "-")):
                    with patch.object(acw, "_last_agent_snippet_for_pane", return_value="Ran tests"):
                        with patch.object(acw, "_is_pid_stopped", return_value=False):
                            out = io.StringIO()
                            with redirect_stdout(out):
                                acw.cmd_status([])
        text = out.getvalue()
        self.assertIn("Sessions: 2", text)
        self.assertIn("Invariant error: agent name 'formal' maps to multiple threads", text)

    def test_status_summary_prints_doctor_recommendation_for_warn(self):
        snapshot = _snapshot(
            panes=(_pane_record("%7", THREAD, window_label="0:7:formal", window_name="formal"),),
            watchers=(_watcher_record("%7", THREAD),),
            sessions=(_session_record(THREAD, name="formal"),),
        )
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
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
        self.assertIn("Recommendation: run acw doctor %7", text)

    def test_status_summary_prints_cleanup_hint_for_dead_sessions(self):
        snapshot = _snapshot(sessions=(_session_record(THREAD, name="formal"),))
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
            with patch.object(acw, "_read_state_json", return_value={"health": "ok"}):
                with patch.object(acw, "_thread_times", return_value=("-", "-")):
                    with patch.object(acw, "_last_agent_snippet_for_pane", return_value="Ran tests"):
                        with patch.object(acw, "_is_pid_stopped", return_value=False):
                            out = io.StringIO()
                            with redirect_stdout(out):
                                acw.cmd_status([])
        self.assertIn("Hint: run acw cleanup to remove dead sessions.", out.getvalue())

    def test_status_summary_prefers_repair_for_pane_local_invariant_errors(self):
        snapshot = _snapshot(
            panes=(_pane_record("%7", THREAD, window_label="0:7:formal", window_name="formal"),),
            watchers=(_watcher_record("%7", OTHER_THREAD, pid="707"),),
            sessions=(_session_record(OTHER_THREAD, name="formal"),),
        )
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
            with patch.object(acw, "_read_state_json", return_value={}):
                with patch.object(acw, "_thread_times", return_value=("-", "-")):
                    with patch.object(acw, "_last_agent_snippet_for_pane", return_value="Ran tests"):
                        with patch.object(acw, "_is_pid_stopped", return_value=False):
                            out = io.StringIO()
                            with redirect_stdout(out):
                                acw.cmd_status([])
        text = out.getvalue()
        self.assertIn("Invariant error: pane %7 runtime id=707 is bound to the wrong thread.", text)
        self.assertIn("Fix: acw repair %7", text)

    def test_status_summary_falls_back_without_rich(self):
        snapshot = _snapshot(
            panes=(_pane_record("%7", THREAD, window_label="0:7:formal", window_name="formal"),),
            watchers=(_watcher_record("%7", THREAD),),
            sessions=(_session_record(THREAD, name="formal"),),
        )
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name.startswith("rich"):
                raise ModuleNotFoundError("No module named 'rich'")
            return real_import(name, globals, locals, fromlist, level)

        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
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
        self.assertIn("LAST AGENT MESSAGE", text)
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
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record(
                "%1",
                "",
                window_label="0:1:uvm",
                window_name="uvm",
                detected_thread_detail="could not determine thread_id for pane=%1 from the live Codex process",
                has_codex_process=True,
            ),),
        ))
        with patch.object(acw, "_build_runtime_model", return_value=model):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    acw.resolve_thread_id("%1", "")

    def test_resolve_thread_id_fails_for_auto_when_unknown(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record(
                "%1",
                "",
                window_label="0:1:uvm",
                window_name="uvm",
                detected_thread_detail="could not determine thread_id for pane=%1 from the live Codex process",
                has_codex_process=True,
            ),),
        ))
        with patch.object(acw, "_build_runtime_model", return_value=model):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    acw.resolve_thread_id("%1", "auto")

    def test_resolve_thread_id_uses_runtime_model_thread(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%1", THREAD, window_label="0:1:uvm", window_name="uvm"),),
        ))
        with patch.object(acw, "_build_runtime_model", return_value=model):
            self.assertEqual(THREAD, acw.resolve_thread_id("%1", ""))

    def test_start_rejects_duplicate_thread_watcher(self):
        err = io.StringIO()
        with patch.object(acw, "resolve_start_pane_target", return_value="%8"):
            with patch.object(acw, "ensure_tmux_window_rename_hook"):
                with patch.object(acw, "watcher_pids_for_pane", return_value=[]):
                    with patch.object(
                        acw,
                        "watcher_rows",
                        return_value=[{
                            "pane": "%6",
                            "thread": THREAD,
                            "pid": "1234",
                        }],
                    ):
                        with redirect_stderr(err):
                            with self.assertRaises(SystemExit) as ctx:
                                acw.cmd_start(["aot2", THREAD, "--message", "continue"])
        self.assertEqual(1, ctx.exception.code)
        self.assertIn("another runtime (id=1234 pane=%6) is already watching thread", err.getvalue())

    def test_start_uses_daemon_rpc_upsert_session(self):
        out = io.StringIO()
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%8", THREAD, window_label="0:8:aot2", window_name="aot2"),),
        ))
        with patch.object(acw, "resolve_start_pane_target", return_value="%8"):
            with patch.object(acw, "ensure_tmux_window_rename_hook"):
                with patch.object(acw, "_build_runtime_model", return_value=model):
                    with patch.object(acw, "watcher_rows", return_value=[]):
                        with patch.object(acw, "resolve_thread_id", return_value=THREAD):
                            with patch.object(acw, "run_tmux", return_value="aot2\n"):
                                with patch.object(
                                    acw,
                                    "_daemon_rpc_upsert_session",
                                    return_value={
                                        "thread": THREAD,
                                        "pane": "%8",
                                        "runtime_status": "running",
                                        "runtime_id": "runtime-321",
                                        "daemon_pid": "9999",
                                    },
                                ) as upsert:
                                    with redirect_stdout(out), redirect_stderr(io.StringIO()):
                                        acw.cmd_start(["aot2", THREAD, "--message", "continue"])
        upsert.assert_called_once_with(
            THREAD,
            pane="%8",
            name="aot2",
            message="continue",
            paused=False,
            generation=0,
        )
        self.assertIn("started: pane=%8 thread_id=11111111-1111-1111-1111-111111111111 daemon_pid=9999", out.getvalue())

    def test_start_rejects_existing_pane_watcher_when_thread_is_duplicated_elsewhere(self):
        err = io.StringIO()

        def fake_rows(pane_filter: str = ""):
            rows = [
                {"pane": "%8", "thread": THREAD, "pid": "1111"},
                {"pane": "%6", "thread": THREAD, "pid": "2222"},
            ]
            if pane_filter:
                return [row for row in rows if row["pane"] == pane_filter]
            return rows

        with patch.object(acw, "resolve_start_pane_target", return_value="%8"):
            with patch.object(acw, "ensure_tmux_window_rename_hook"):
                with patch.object(acw, "watcher_rows", side_effect=fake_rows):
                    with redirect_stderr(err):
                        with self.assertRaises(SystemExit) as ctx:
                            acw.cmd_start(["uvm", "--message", "continue"])
        self.assertEqual(1, ctx.exception.code)
        text = err.getvalue()
        self.assertIn("existing runtime for pane=%8 is duplicated", text)
        self.assertIn("acw stop %8", text)

    def test_start_rejects_existing_pane_watcher_with_wrong_explicit_thread(self):
        err = io.StringIO()

        def fake_rows(pane_filter: str = ""):
            rows = [{"pane": "%8", "thread": THREAD, "pid": "1111"}]
            if pane_filter:
                return [row for row in rows if row["pane"] == pane_filter]
            return rows

        with patch.object(acw, "resolve_start_pane_target", return_value="%8"):
            with patch.object(acw, "ensure_tmux_window_rename_hook"):
                with patch.object(acw, "watcher_rows", side_effect=fake_rows):
                    with redirect_stderr(err):
                        with self.assertRaises(SystemExit) as ctx:
                            acw.cmd_start(["uvm", OTHER_THREAD, "--message", "continue"])
        self.assertEqual(1, ctx.exception.code)
        self.assertIn("existing runtime for pane=%8 is using thread", err.getvalue())

    def test_cmd_stop_by_name_forgets_saved_session_state(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                acw._write_session_state(THREAD, {"thread_id": THREAD, "name": "acw", "message": "continue"})
                with patch.object(
                    acw,
                    "watcher_rows",
                    return_value=[{"pane": "%9", "thread": THREAD, "pid": "1234", "runtime_status": "running"}],
                ):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        acw.cmd_stop(["acw"])
                self.assertEqual({}, acw._read_session_state(THREAD))
        text = out.getvalue()
        self.assertIn(f"stopped: pane=%9 thread_id={THREAD}", text)

    def test_resolve_start_pane_target_rejects_thread_id(self):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit):
                acw.resolve_start_pane_target(THREAD)
        self.assertIn("start target must be", err.getvalue())

    def test_resolve_start_pane_target_uses_window_name(self):
        with patch.object(acw, "resolve_pane_from_window_name", return_value="%7"):
            self.assertEqual("%7", acw.resolve_start_pane_target("formal"))

    def test_watcher_rows_use_runtime_rows_only(self):
        runtime_rows = [{
            "pane": "%0",
            "thread": THREAD,
            "pid": "202",
            "tmux_socket": "/tmp/current.sock",
            "runtime_status": "running",
        }]
        with patch.object(acw, "_read_runtime_rows", return_value=runtime_rows):
            with patch.object(
                acw.subprocess,
                "check_output",
                side_effect=AssertionError("legacy ps scan should not run"),
            ):
                rows = acw.watcher_rows()
        self.assertEqual(runtime_rows, rows)

    def test_session_state_round_trips_through_sqlite_store(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                acw._write_session_state(
                    THREAD,
                    {
                        "thread_id": THREAD,
                        "name": "formal",
                        "message": "continue by docs/FORMAL_PLAN.md",
                        "paused": True,
                        "generation": 3,
                    },
                )

                state = acw._read_session_state(THREAD)

        self.assertEqual("formal", state["name"])
        self.assertEqual("continue by docs/FORMAL_PLAN.md", state["message"])
        self.assertTrue(state["paused"])
        self.assertEqual(3, state["generation"])

    def test_read_runtime_rows_uses_runtime_id_and_daemon_pid_liveness_from_sqlite_store(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                acw._write_runtime_state(
                    THREAD,
                    {
                        "runtime_id": "4321",
                        "daemon_pid": "9999",
                        "pane": "%7",
                        "runtime_status": "running",
                        "tmux_socket": "/tmp/current.sock",
                    },
                    "/tmp/current.sock",
                )
                with patch.object(acw.os, "kill") as kill:
                    rows = ORIGINAL_READ_RUNTIME_ROWS("/tmp/current.sock")

        kill.assert_called_once_with(9999, 0)
        self.assertEqual(1, len(rows))
        self.assertEqual("4321", rows[0]["runtime_id"])
        self.assertEqual("%7", rows[0]["pane"])
        self.assertEqual("running", rows[0]["runtime_status"])

    def test_doctor_checks_current_pane_and_thread(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%7", THREAD, window_label="0:7:formal", window_name="formal"),),
        ))
        with patch.dict(acw.os.environ, {"TMUX_PANE": "%7"}, clear=False):
            with patch.object(acw, "_state_dir_is_writable", return_value=(True, acw.STATE_DIR)):
                with patch.object(acw, "_codex_auth_state_available", return_value=(True, "/tmp/auth.json")):
                    with patch.object(acw, "run_tmux", return_value="session\n"):
                        with patch.object(acw, "_build_runtime_model", return_value=model):
                            report = acw._doctor_report("")
        self.assertEqual(0, report.exit_code)
        rendered = "\n".join(f"{level}:{msg}" for level, msg in report.checks)
        self.assertIn("ok:tmux server reachable", rendered)
        self.assertIn("ok:pane resolved: %7", rendered)
        self.assertIn(f"ok:Codex thread detected: {THREAD}", rendered)
        self.assertIn("info:thread provenance: live Codex process-owned evidence", rendered)

    def test_daemon_log_tail_only_reads_new_lines(self):
        existing_turn = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as handle:
            handle.write(
                f"2026-03-10 INFO session_loop{{thread_id={THREAD}}}: "
                f"codex_core::codex: post sampling token usage turn_id={existing_turn} "
                "total_usage_tokens=1 needs_follow_up=false\n"
            )
            log_path = Path(handle.name)
        try:
            tail = acw._DaemonCodexLogTail(log_path=log_path)
            self.assertEqual([], acw._drain_daemon_codex_log_events(tail))

            new_turn = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"2026-03-10 INFO session_loop{{thread_id={THREAD}}}: "
                    f"codex_core::codex: post sampling token usage turn_id={new_turn} "
                    "total_usage_tokens=1 needs_follow_up=false\n"
                )

            events = acw._drain_daemon_codex_log_events(tail)
            self.assertEqual(1, len(events))
            self.assertEqual("completion", events[0].kind)
            self.assertEqual(THREAD, events[0].thread_id)
            self.assertEqual(new_turn, events[0].turn_id)
        finally:
            tail.close()
            log_path.unlink(missing_ok=True)

    def test_daemon_process_session_events_updates_saved_state_after_send(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                state_file = acw.state_file_for_thread(THREAD)
                acw._write_session_state(THREAD, {"thread_id": THREAD, "message": "continue"})
                runtime = acw._daemon_session_runtime(
                    _session_record(THREAD, name="formal", message="continue", state_file=state_file),
                    codex_log_path=Path(td) / "codex-tui.log",
                )
                event = acw._LogwatchCodexLogEvent("completion", THREAD, "turn-1", "false")

                with patch.object(acw, "_check_pane_for_interrupt", return_value=None):
                    with patch.object(acw, "_check_pane_for_errors", return_value=None):
                        with patch.object(
                            acw,
                            "_watch_tmux_send",
                            return_value=type("SendResult", (), {"status": "ok", "detail": ""})(),
                        ):
                            with patch.object(acw, "_compute_watch_health", return_value=("ok", "")):
                                with patch.object(acw, "_watch_now_ts", return_value="2026-03-14 12:00:00"):
                                    with patch.object(acw.time, "sleep"):
                                        acw._daemon_process_session_events(runtime, [event])

                state = acw._read_session_state(THREAD)
                self.assertEqual("turn-1", state["last_handled_turn"])
                self.assertEqual(THREAD, state["last_handled_thread"])
                self.assertEqual("2026-03-14 12:00:00", state["last_continue_at"])
                self.assertEqual("running", runtime.runtime_status)

    def test_daemon_process_session_events_auto_pauses_on_pane_error(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                state_file = acw.state_file_for_thread(THREAD)
                acw._write_session_state(THREAD, {"thread_id": THREAD, "message": "continue"})
                runtime = acw._daemon_session_runtime(
                    _session_record(THREAD, name="formal", message="continue", state_file=state_file),
                    codex_log_path=Path(td) / "codex-tui.log",
                )
                event = acw._LogwatchCodexLogEvent("completion", THREAD, "turn-1", "false")

                with patch.object(acw, "_check_pane_for_interrupt", return_value=None):
                    with patch.object(acw, "_check_pane_for_errors", return_value="Authentication failed"):
                        acw._daemon_process_session_events(runtime, [event])

                state = acw._read_session_state(THREAD)
                self.assertTrue(state["paused"])
                self.assertEqual("auto-paused: Authentication failed", state["health_detail"])
                self.assertEqual("paused", runtime.runtime_status)
                self.assertTrue(runtime.paused)

    def test_daemon_main_cleanup_ignores_sqlite_errors_removing_runtime_state(self):
        class DummyServer:
            def close(self):
                return None

        class DummyTail:
            def close(self):
                return None

        with tempfile.TemporaryDirectory() as td:
            pid_file = os.path.join(td, "acw_daemon.pid")
            socket_file = os.path.join(td, "acw_daemon.sock")

            def fail_reconcile(_scope, *, log_tail, runtimes):
                del log_tail
                runtimes[THREAD] = object()
                raise KeyboardInterrupt

            with patch.object(acw.acw_rpc, "listen", return_value=DummyServer()):
                with patch.object(acw, "daemon_pid_file_for_scope", return_value=pid_file):
                    with patch.object(acw, "daemon_socket_file_for_scope", return_value=socket_file):
                        with patch.object(acw, "_DaemonCodexLogTail", return_value=DummyTail()):
                            with patch.object(acw, "_write_daemon_scope_state"):
                                with patch.object(acw, "_daemon_process_rpc_requests", return_value=0):
                                    with patch.object(acw, "_daemon_reconcile_once", side_effect=fail_reconcile):
                                        with patch.object(
                                            acw,
                                            "_remove_runtime_state",
                                            side_effect=sqlite3.OperationalError("unable to open database file"),
                                        ) as remove_runtime:
                                            err = io.StringIO()
                                            with redirect_stderr(err):
                                                with self.assertRaises(KeyboardInterrupt):
                                                    acw._daemon_main([])

        remove_runtime.assert_called_once_with(THREAD, "")
        self.assertIn("failed to remove runtime state", err.getvalue())

    def test_repair_steps_restart_duplicate_watchers_on_same_pane(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%7", THREAD, window_label="0:7:formal", window_name="formal"),),
            watchers=(
                _watcher_record("%7", THREAD, pid="111"),
                _watcher_record("%7", THREAD, pid="222"),
            ),
            sessions=(_session_record(THREAD, name="formal", message="continue by plan"),),
        ))
        steps = acw._repair_steps_for_pane(model, "%7")
        self.assertEqual(["restart_watcher"], [step.kind for step in steps])
        self.assertTrue(steps[0].auto_apply)
        self.assertEqual("%7", steps[0].target)
        self.assertEqual(THREAD, steps[0].thread_id)
        self.assertEqual("continue by plan", steps[0].message)

    def test_repair_steps_start_unwatched_live_pane_with_saved_session(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%7", THREAD, window_label="0:7:formal", window_name="formal"),),
            sessions=(_session_record(THREAD, name="formal", message="continue by plan"),),
        ))
        steps = acw._repair_steps_for_pane(model, "%7")
        self.assertEqual(["start_watcher"], [step.kind for step in steps])
        self.assertTrue(steps[0].auto_apply)
        self.assertEqual("continue by plan", steps[0].message)

    def test_repair_steps_flag_duplicate_thread_across_panes_as_manual(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(
                _pane_record("%7", THREAD, window_label="0:7:formal", window_name="formal"),
                _pane_record("%8", THREAD, window_label="0:8:aot", window_name="aot"),
            ),
            sessions=(_session_record(THREAD, name="formal", message="continue"),),
        ))
        steps = acw._repair_steps_for_pane(model, "%7")
        self.assertEqual(["manual"], [step.kind for step in steps])
        self.assertFalse(steps[0].auto_apply)
        self.assertIn("%7, %8", steps[0].summary)

    def test_repair_plan_reports_dead_sessions_as_cleanup_only(self):
        model = acw._build_runtime_model(_snapshot(
            sessions=(_session_record(THREAD, name="formal", message="continue"),),
        ))
        steps = acw._build_repair_plan(model)
        self.assertEqual(["cleanup_dead_sessions"], [step.kind for step in steps])
        self.assertFalse(steps[0].auto_apply)
        self.assertIn("acw cleanup", steps[0].summary)

    def test_print_repair_plan_shows_command_hints(self):
        steps = [
            acw.RepairStep(
                kind="restart_watcher",
                target="%7",
                summary="restart runtime on pane %7",
                auto_apply=True,
                thread_id=THREAD,
            ),
            acw.RepairStep(
                kind="cleanup_dead_sessions",
                target="",
                summary="run acw cleanup to remove 1 dead saved session(s)",
                auto_apply=False,
            ),
        ]
        out = io.StringIO()
        with redirect_stdout(out):
            acw._print_repair_plan(steps, dry_run=True)
        text = out.getvalue()
        self.assertIn("AUTO: restart runtime on pane %7", text)
        self.assertIn("run: acw repair %7", text)
        self.assertIn("run: acw cleanup", text)

    def test_repair_plan_ignores_unmanaged_live_panes_by_default(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%9", THREAD, window_label="0:9:scratch", window_name="scratch"),),
        ))
        self.assertEqual([], acw._build_repair_plan(model))

    def test_repair_plan_adopts_dead_named_session_for_live_pane_without_runtime(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%50", THIRD_THREAD, window_label="0:50:bugs", window_name="bugs"),),
            sessions=(_session_record(THREAD, name="bugs", message="continue through docs"),),
        ))

        steps = acw._repair_steps_for_pane(model, "%50")

        self.assertEqual(1, len(steps))
        self.assertEqual("start_watcher", steps[0].kind)
        self.assertEqual(THIRD_THREAD, steps[0].thread_id)
        self.assertEqual(THREAD, steps[0].source_thread_id)
        self.assertEqual("continue through docs", steps[0].message)
        self.assertIn("adopt saved session 'bugs'", steps[0].summary)

    def test_repair_plan_adopts_dead_named_session_even_with_stale_missing_watcher(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%50", THIRD_THREAD, window_label="0:50:bugs", window_name="bugs"),),
            watchers=(_watcher_record("%5", THREAD, pid="9999"),),
            sessions=(_session_record(THREAD, name="bugs", message="continue through docs"),),
        ))

        steps = acw._repair_steps_for_pane(model, "%50")

        self.assertEqual(1, len(steps))
        self.assertEqual("start_watcher", steps[0].kind)
        self.assertEqual(THREAD, steps[0].source_thread_id)

    def test_repair_plan_includes_recoverable_dead_named_session_in_no_target_mode(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%50", THIRD_THREAD, window_label="0:50:bugs", window_name="bugs"),),
            sessions=(_session_record(THREAD, name="bugs", message="continue through docs"),),
        ))

        steps = acw._build_repair_plan(model)

        self.assertEqual(["start_watcher"], [step.kind for step in steps])

    def test_restart_target_adopts_dead_named_session_and_removes_old_session(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%50", THIRD_THREAD, window_label="0:50:bugs", window_name="bugs"),),
            sessions=(_session_record(THREAD, name="bugs", message="continue through docs"),),
        ))

        with patch.object(acw, "_build_runtime_model", return_value=model):
            with patch.object(acw, "_daemon_rpc_remove_session") as remove_session:
                with patch.object(acw, "run_tmux", return_value="bugs\n"):
                    def _upsert(*args, **kwargs):
                        self.assertFalse(remove_session.called)
                        return {
                            "thread": THIRD_THREAD,
                            "pane": "%50",
                            "runtime_status": "running",
                            "runtime_id": "runtime-9999",
                            "daemon_pid": "9999",
                        }
                    with patch.object(
                        acw,
                        "_daemon_rpc_upsert_session",
                        side_effect=_upsert,
                    ) as upsert:
                        out = io.StringIO()
                        with redirect_stdout(out), redirect_stderr(io.StringIO()):
                            acw._restart_one("bugs", "%50", [])

        remove_session.assert_called_once_with(THREAD, timeout_secs=2.0)
        upsert.assert_called_once_with(
            THIRD_THREAD,
            pane="%50",
            name="bugs",
            message="continue through docs",
            paused=False,
            generation=1,
        )
        self.assertIn("restart: adopting saved continue message from dead thread", out.getvalue())

    def test_restart_target_keeps_old_session_when_adopted_start_fails(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%50", THIRD_THREAD, window_label="0:50:bugs", window_name="bugs"),),
            sessions=(_session_record(THREAD, name="bugs", message="continue through docs"),),
        ))

        with patch.object(acw, "_build_runtime_model", return_value=model):
            with patch.object(acw, "_daemon_rpc_remove_session") as remove_session:
                with patch.object(acw, "run_tmux", return_value="bugs\n"):
                    with patch.object(acw, "_daemon_rpc_upsert_session", side_effect=RuntimeError("boom")):
                        with self.assertRaisesRegex(RuntimeError, "boom"):
                            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                                acw._restart_one("bugs", "%50", [])

        remove_session.assert_not_called()

    def test_apply_repair_step_keeps_source_session_when_start_fails(self):
        context = SimpleNamespace(
            daemon_rpc_remove_session=MagicMock(),
            print_progress=lambda message: None,
            run_tmux=lambda *args, **kwargs: "bugs\n",
            write_session_state=MagicMock(),
            current_tmux_scope=lambda: "default",
            message_text=lambda text: text,
        )
        step = acw.RepairStep(
            kind="start_watcher",
            target="%50",
            summary="adopt bugs",
            auto_apply=True,
            thread_id=THIRD_THREAD,
            source_thread_id=THREAD,
            message="continue through docs",
        )

        with self.assertRaisesRegex(RuntimeError, "boom"):
            acw.acw_commands.apply_repair_step(
                context,
                step,
                cmd_start_fn=MagicMock(side_effect=RuntimeError("boom")),
                stop_pane_watchers_keep_session_fn=MagicMock(),
            )

        context.daemon_rpc_remove_session.assert_not_called()

    def test_doctor_report_recommends_restart_for_warn_watcher(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%7", THREAD, window_label="0:7:uvm", window_name="uvm"),),
            watchers=(_watcher_record("%7", THREAD),),
            sessions=(_session_record(THREAD, name="uvm"),),
        ))
        with patch.object(acw, "_state_dir_is_writable", return_value=(True, acw.STATE_DIR)):
            with patch.object(acw, "_codex_auth_state_available", return_value=(True, "/tmp/auth.json")):
                with patch.object(acw, "run_tmux", return_value="session\n"):
                    with patch.object(acw, "_doctor_resolve_target", return_value=("%7", "")):
                        with patch.object(acw, "_build_runtime_model", return_value=model):
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
        self.assertIn("warn:runtime health: warn - rollout channel closed", rendered)

    def test_doctor_resolve_target_uses_saved_session_pane(self):
        with patch.object(acw, "_saved_session_panes_for_target", return_value=["%9"]):
            with patch.object(acw, "resolve_pane_from_window_name", return_value=None):
                self.assertEqual(("%9", ""), acw._doctor_resolve_target("formal"))

    def test_doctor_report_errors_when_live_pane_thread_cannot_be_detected(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record(
                "%7",
                "",
                window_label="0:7:formal",
                window_name="formal",
                detected_thread_detail="could not determine thread_id for pane=%7 from the live Codex process",
                has_codex_process=True,
            ),),
            watchers=(_watcher_record("%7", THREAD),),
            sessions=(_session_record(THREAD, name="formal"),),
        ))
        with patch.object(acw, "_state_dir_is_writable", return_value=(True, acw.STATE_DIR)):
            with patch.object(acw, "_codex_auth_state_available", return_value=(True, "/tmp/auth.json")):
                with patch.object(acw, "run_tmux", return_value="session\n"):
                    with patch.object(acw, "_doctor_resolve_target", return_value=("%7", "")):
                        with patch.object(acw, "_build_runtime_model", return_value=model):
                            with patch.object(acw, "_read_state_json", return_value={}):
                                report = acw._doctor_report("formal")
        self.assertEqual("error", report.result)
        rendered = "\n".join(f"{level}:{msg}" for level, msg in report.checks)
        self.assertIn("error:could not determine thread_id for pane=%7 from the live Codex process", rendered)
        self.assertIn(
            "error:runtime identity mismatch: could not determine thread_id for pane=%7 from the live Codex process",
            rendered,
        )

    def test_status_summary_skips_unmanaged_unresolved_live_codex_pane(self):
        snapshot = _snapshot(
            panes=(_pane_record(
                "%7",
                "",
                window_label="0:7:formal",
                window_name="formal",
                detected_thread_detail="could not determine thread_id for pane=%7 from the live Codex process",
                has_codex_process=True,
            ),),
        )
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
            with patch.object(acw, "_read_state_json", return_value={}):
                with patch.object(acw, "_thread_times", return_value=("-", "-")):
                    with patch.object(acw, "_last_agent_snippet_for_pane", return_value="Ran tests"):
                        with patch.object(acw, "_is_pid_stopped", return_value=False):
                            out = io.StringIO()
                            with redirect_stdout(out):
                                acw.cmd_status([])
        text = out.getvalue()
        self.assertIn("Sessions: 0", text)
        self.assertIn("(none)", text)

    def test_doctor_plain_output_shows_recommended_command(self):
        report = acw.DoctorReport(
            checks=[("warn", "runtime health: warn - rollout channel closed")],
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
        empty_model = acw._build_runtime_model(_snapshot())
        with patch.dict(acw.os.environ, {}, clear=True):
            with patch.object(acw, "_state_dir_is_writable", return_value=(True, acw.STATE_DIR)):
                with patch.object(acw, "_codex_auth_state_available", return_value=(True, "/tmp/auth.json")):
                    with patch.object(acw, "run_tmux", return_value="session\n"):
                        with patch.object(acw, "_build_runtime_model", return_value=empty_model):
                            report = acw._doctor_report("")
        self.assertEqual(0, report.exit_code)
        rendered = "\n".join(f"{level}:{msg}" for level, msg in report.checks)
        self.assertIn("info:pane checks skipped", rendered)

    def test_doctor_rejects_explicit_dot_target(self):
        empty_model = acw._build_runtime_model(_snapshot())
        with patch.object(acw, "_build_runtime_model", return_value=empty_model):
            report = acw._doctor_report(".")
        self.assertEqual(1, report.exit_code)
        rendered = "\n".join(f"{level}:{msg}" for level, msg in report.checks)
        self.assertIn("error:could not resolve target '.'", rendered)

    def test_load_sessions_reads_thread_keyed_state(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                acw._write_session_state(
                    THREAD,
                    {
                        "thread_id": THREAD,
                        "name": "aot",
                        "message": "continue",
                    },
                )
                sessions = acw._load_sessions()
        self.assertEqual(1, len(sessions))
        self.assertEqual("aot", sessions[0]["name"])
        self.assertEqual("continue", sessions[0]["message"])
        self.assertEqual(acw.state_file_for_thread(THREAD), sessions[0]["state_file"])

    def test_load_sessions_ignores_invalid_thread_ids(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                acw._write_session_state("bad", {"thread_id": "bad", "name": "oops"})
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

    def test_cleanup_stale_files_default_keeps_session_state(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                acw._write_session_state(THREAD, {"thread_id": THREAD, "name": "formal", "message": "continue"})
                with patch.object(acw, "watcher_rows", return_value=[]):
                    acw.cleanup_stale_files()
                self.assertEqual(1, len(acw._load_sessions()))

    def test_cleanup_stale_files_removes_dead_session_state_without_live_pane(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                acw._write_session_state(THREAD, {"thread_id": THREAD, "name": "formal", "message": "continue"})
                with patch.object(acw, "watcher_rows", return_value=[]):
                    with patch.object(acw, "_build_runtime_model", return_value=acw._build_runtime_model(_snapshot())):
                        acw.cleanup_stale_files(remove_dead_sessions=True)
                self.assertEqual([], acw._load_sessions())

    def test_cleanup_stale_files_keeps_dead_session_state_with_live_pane(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acw, "STATE_DIR", td):
                acw._write_session_state(THREAD, {"thread_id": THREAD, "name": "formal", "message": "continue"})
                with patch.object(acw, "watcher_rows", return_value=[]):
                    live_model = acw._build_runtime_model(_snapshot(
                        panes=(_pane_record("%7", THREAD, window_label="0:7:formal", window_name="formal"),),
                    ))
                    with patch.object(acw, "_build_runtime_model", return_value=live_model):
                        acw.cleanup_stale_files(remove_dead_sessions=True)
                self.assertEqual(1, len(acw._load_sessions()))

    def test_build_thread_pane_map_uses_tmux_process_tree(self):
        def fake_tmux(*args):
            if args[:4] == ("list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"):
                return "%7\t1234\n"
            return None

        with patch.object(acw, "run_tmux", side_effect=fake_tmux):
            with patch.object(
                acw.subprocess,
                "check_output",
                return_value="222 1234 codex\n",
            ):
                with patch.object(acw, "_thread_from_codex_pid", return_value=THREAD):
                    mapping = acw._build_thread_pane_map()
        self.assertEqual("%7", mapping[THREAD])

    def test_list_live_pane_records_uses_one_process_snapshot_for_all_panes(self):
        listing = (
            "0\t7\tformal\t%7\t1234\n"
            "0\t8\tbugs\t%8\t5678\n"
        )
        with patch.object(acw, "run_tmux", return_value=listing):
            with patch.object(
                acw.subprocess,
                "check_output",
                return_value="222 1234 codex\n333 5678 codex\n",
            ) as check_output:
                with patch.object(acw, "_thread_from_codex_pid", side_effect=[THREAD, OTHER_THREAD]):
                    rows = acw._list_live_pane_records()
        self.assertEqual(1, check_output.call_count)
        self.assertEqual([THREAD, OTHER_THREAD], [row.detected_thread for row in rows])

    def test_build_thread_pane_map_does_not_guess_when_process_tree_has_no_thread(self):
        def fake_tmux(*args):
            if args[:4] == ("list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"):
                return "%7\t1234\n"
            return None

        with patch.object(acw, "run_tmux", side_effect=fake_tmux):
            with patch.object(
                acw.subprocess,
                "check_output",
                return_value="222 1234 codex\n",
            ):
                with patch.object(acw, "_thread_from_codex_pid", return_value=None):
                    mapping = acw._build_thread_pane_map()
        self.assertEqual({}, mapping)

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
        snapshot = _snapshot(
            watchers=(_watcher_record("%7", THREAD),),
            sessions=(_session_record(THREAD, name="formal"),),
        )
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
            with patch.object(acw, "_read_state_json", return_value={}):
                with patch.object(acw, "_thread_times", return_value=("-", "-")):
                    with patch.object(acw, "_is_pid_stopped", return_value=False):
                        out = io.StringIO()
                        with redirect_stdout(out):
                            acw.cmd_status(["--details"])
        text = out.getvalue()
        self.assertIn("PANE:            %7", text)
        self.assertIn("ID:              1234", text)
        self.assertIn("STATE:           error", text)
        self.assertIn("ANOMALY:         tmux pane %7 is missing", text)
        self.assertIn("WINDOW:          formal", text)

    def test_status_target_matches_saved_session_name_without_live_pane(self):
        snapshot = _snapshot(sessions=(_session_record(THREAD, name="formal"),))
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
            out = io.StringIO()
            with redirect_stdout(out):
                acw.cmd_status(["formal"])
        text = out.getvalue()
        self.assertIn("Sessions: 1", text)
        self.assertIn("formal", text)

    def test_status_skips_unmanaged_live_codex_pane_without_saved_session(self):
        snapshot = _snapshot(
            panes=(_pane_record("%9", THREAD, window_label="0:9:acw", window_name="acw"),),
        )
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
            out = io.StringIO()
            with redirect_stdout(out):
                acw.cmd_status([])
        text = out.getvalue()
        self.assertIn("Sessions: 0", text)
        self.assertIn("(none)", text)
        self.assertNotIn("acw/%9", text)

    def test_status_shows_unwatched_live_codex_pane_when_saved_session_exists(self):
        snapshot = _snapshot(
            panes=(_pane_record("%9", THREAD, window_label="0:9:acw", window_name="acw"),),
            sessions=(_session_record(THREAD, name="acw", message="continue"),),
        )
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
            out = io.StringIO()
            with redirect_stdout(out):
                acw.cmd_status(["--details"])
        text = out.getvalue()
        self.assertIn("Sessions (details): 1", text)
        self.assertIn("WINDOW:          0:9:acw", text)
        self.assertIn("ANOMALY:         no runtime for managed live Codex pane", text)

    def test_status_prints_tmux_recovery_hint_when_metadata_is_unavailable(self):
        snapshot = _snapshot(sessions=(_session_record(THREAD, name="formal"),))
        with patch.object(acw, "_build_runtime_snapshot", return_value=snapshot):
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

    def test_pause_without_target_pauses_all_watchers(self):
        rows = [
            {"pane": "%1", "pid": "101", "thread": THREAD, "runtime_status": "running"},
            {"pane": "%2", "pid": "202", "thread": OTHER_THREAD, "runtime_status": "running"},
        ]
        with patch.object(acw, "watcher_rows", return_value=rows):
            with patch.object(acw, "_daemon_rpc_patch_session") as patch_session:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    acw.cmd_pause([])
        self.assertEqual(
            [
                (
                    (THREAD, {"paused": True}),
                    {
                        "wait": {
                            "kind": "runtime",
                            "pane": "%1",
                            "paused": True,
                            "statuses": ["paused"],
                        },
                        "timeout_secs": 2.0,
                    },
                ),
                (
                    (OTHER_THREAD, {"paused": True}),
                    {
                        "wait": {
                            "kind": "runtime",
                            "pane": "%2",
                            "paused": True,
                            "statuses": ["paused"],
                        },
                        "timeout_secs": 2.0,
                    },
                ),
            ],
            [(call.args, call.kwargs) for call in patch_session.call_args_list],
        )

    def test_pause_rejects_star_selector(self):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                acw.cmd_pause(["*"])
        self.assertEqual(2, ctx.exception.code)
        self.assertIn("use 'acw pause' with no target", err.getvalue())

    def test_resume_without_target_resumes_all_paused_watchers(self):
        rows = [
            {"pane": "%1", "pid": "101", "thread": THREAD, "runtime_status": "paused", "paused": "1"},
            {"pane": "%2", "pid": "202", "thread": OTHER_THREAD, "runtime_status": "running", "paused": ""},
            {"pane": "%3", "pid": "303", "thread": "33333333-3333-3333-3333-333333333333", "runtime_status": "paused", "paused": "1"},
        ]
        with patch.object(acw, "watcher_rows", return_value=rows):
            with patch.object(acw, "_restart_panes") as restart_panes:
                out = io.StringIO()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    acw.cmd_resume([])
        restart_panes.assert_called_once_with(["%1", "%3"])
        self.assertIn("resuming 2 paused managed session(s) via restart", out.getvalue())

    def test_resume_rejects_star_selector(self):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                acw.cmd_resume(["*"])
        self.assertEqual(2, ctx.exception.code)
        self.assertIn("use 'acw resume' with no target", err.getvalue())

    def test_resume_target_restarts_paused_watcher_instead_of_sigcont(self):
        with patch.object(acw, "resolve_pane_target", return_value="%7"):
            with patch.object(
                acw,
                "watcher_rows",
                return_value=[{"pane": "%7", "pid": "1234", "thread": THREAD, "runtime_status": "paused", "paused": "1"}],
            ):
                with patch.object(acw, "_restart_one") as restart_one:
                    with patch.object(acw.os, "kill") as kill:
                        out = io.StringIO()
                        with redirect_stdout(out), redirect_stderr(io.StringIO()):
                            acw.cmd_resume(["aot"])
        restart_one.assert_called_once_with("aot", "%7", [])
        kill.assert_not_called()
        self.assertIn(f"resuming paused session via restart: pane=%7 thread_id={THREAD}", out.getvalue())

    def test_resume_target_sends_continue_for_waiting_session(self):
        with patch.object(acw, "resolve_pane_target", return_value="%7"):
            with patch.object(
                acw,
                "watcher_rows",
                return_value=[{"pane": "%7", "pid": "1234", "thread": THREAD, "runtime_status": "running", "paused": ""}],
            ):
                with patch.object(acw, "_paused_activity_for_pane", return_value="waiting"):
                    with patch.object(acw, "_daemon_rpc_continue_session") as continue_session:
                        with patch.object(acw, "_restart_one") as restart_one:
                            out = io.StringIO()
                            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                                acw.cmd_resume(["ship"])
        continue_session.assert_called_once_with(THREAD, timeout_secs=2.0)
        restart_one.assert_not_called()
        self.assertIn(f"resuming waiting session: pane=%7 thread_id={THREAD}", out.getvalue())

    def test_resume_target_does_not_send_continue_for_busy_session(self):
        with patch.object(acw, "resolve_pane_target", return_value="%7"):
            with patch.object(
                acw,
                "watcher_rows",
                return_value=[{"pane": "%7", "pid": "1234", "thread": THREAD, "runtime_status": "running", "paused": ""}],
            ):
                with patch.object(acw, "_paused_activity_for_pane", return_value="busy"):
                    with patch.object(acw, "_daemon_rpc_continue_session") as continue_session:
                        with patch.object(acw, "_restart_one") as restart_one:
                            out = io.StringIO()
                            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                                acw.cmd_resume(["ship"])
        continue_session.assert_not_called()
        restart_one.assert_not_called()
        self.assertIn(f"already busy: pane=%7 thread_id={THREAD}", out.getvalue())

    def test_resume_multiple_targets_restarts_each_paused_target(self):
        resolved = {"formal": "%6", "aot": "%7"}

        with patch.object(acw, "resolve_pane_target", side_effect=lambda target: resolved[target]):
            with patch.object(
                acw,
                "watcher_rows",
                side_effect=lambda pane="": {
                    "%6": [{"pane": "%6", "pid": "111", "thread": THREAD, "runtime_status": "paused", "paused": "1"}],
                    "%7": [{"pane": "%7", "pid": "222", "thread": OTHER_THREAD, "runtime_status": "paused", "paused": "1"}],
                }[pane],
            ):
                with patch.object(acw, "_restart_one") as restart_one:
                    out = io.StringIO()
                    with redirect_stdout(out), redirect_stderr(io.StringIO()):
                        acw.cmd_resume(["formal", "aot"])
        self.assertEqual(
            [
                (("formal", "%6", []),),
                (("aot", "%7", []),),
            ],
            restart_one.call_args_list,
        )
        text = out.getvalue()
        self.assertIn(f"resuming paused session via restart: pane=%6 thread_id={THREAD}", text)
        self.assertIn(f"resuming paused session via restart: pane=%7 thread_id={OTHER_THREAD}", text)

    def test_resume_multiple_targets_continues_when_one_is_not_paused(self):
        resolved = {"formal": "%6", "aot": "%7"}

        with patch.object(acw, "resolve_pane_target", side_effect=lambda target: resolved[target]):
            with patch.object(
                acw,
                "watcher_rows",
                side_effect=lambda pane="": {
                    "%6": [{"pane": "%6", "pid": "111", "thread": THREAD, "runtime_status": "running", "paused": ""}],
                    "%7": [{"pane": "%7", "pid": "222", "thread": OTHER_THREAD, "runtime_status": "paused", "paused": "1"}],
                }[pane],
            ):
                with patch.object(acw, "_paused_activity_for_pane", return_value="busy"):
                    with patch.object(acw, "_daemon_rpc_continue_session") as continue_session:
                        with patch.object(acw, "_restart_one") as restart_one:
                            out = io.StringIO()
                            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                                acw.cmd_resume(["formal", "aot"])
        continue_session.assert_not_called()
        restart_one.assert_called_once_with("aot", "%7", [])
        text = out.getvalue()
        self.assertIn(f"already busy: pane=%6 thread_id={THREAD}", text)
        self.assertIn(f"resuming paused session via restart: pane=%7 thread_id={OTHER_THREAD}", text)

    def test_restart_rejects_star_selector(self):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                acw.cmd_restart(["*"])
        self.assertEqual(2, ctx.exception.code)
        self.assertIn("use 'acw restart' with no target", err.getvalue())

    def test_restart_without_target_restarts_all_live_panes(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(
                _pane_record("%1", THREAD, window_label="0:1:one", window_name="one"),
                _pane_record("%2", OTHER_THREAD, window_label="0:2:two", window_name="two"),
            ),
        ))
        with patch.object(acw, "_build_runtime_model", return_value=model):
            with patch.object(acw, "_restart_panes") as restart_panes:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    acw.cmd_restart([])
        restart_panes.assert_called_once_with(["%1", "%2"])

    def test_restart_without_target_includes_recoverable_dead_sessions(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(
                _pane_record("%1", THREAD, window_label="0:1:formal", window_name="formal"),
                _pane_record("%9", OTHER_THREAD, window_label="0:9:aot", window_name="aot"),
            ),
            watchers=(_watcher_record("%1", THREAD, pid="101"),),
            sessions=(
                _session_record(THREAD, name="formal", state_file="/tmp/one"),
                _session_record(OTHER_THREAD, name="aot", state_file="/tmp/two"),
            ),
        ))
        with patch.object(acw, "_build_runtime_model", return_value=model):
            with patch.object(acw, "_restart_panes") as restart_panes:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    acw.cmd_restart([])
        restart_panes.assert_called_once_with(["%1", "%9"])

    def test_restart_panes_continues_after_one_pane_fails(self):
        def fake_tmux(*args):
            if args[:4] == ("display-message", "-p", "-t", "%1"):
                return "%1\n"
            if args[:4] == ("display-message", "-p", "-t", "%2"):
                return "%2\n"
            if args[:4] == ("display-message", "-p", "-t", "%3"):
                return "%3\n"
            return None

        def fake_restart_one(target: str, pane: str, rest: list[str]) -> None:
            if pane == "%2":
                print("error: duplicate thread detected", file=sys.stderr)
                print("hint: stop it first with 'acw stop %2'", file=sys.stderr)
                raise SystemExit(1)

        out = io.StringIO()
        err = io.StringIO()
        with patch.object(acw, "run_tmux", side_effect=fake_tmux):
            with patch.object(acw, "_restart_one", side_effect=fake_restart_one) as restart_one:
                with redirect_stdout(out), redirect_stderr(err):
                    acw._restart_panes(["%1", "%2", "%3"])
        self.assertEqual(
            [
                (("%1", "%1", []),),
                (("%2", "%2", []),),
                (("%3", "%3", []),),
            ],
            restart_one.call_args_list,
        )
        self.assertIn("error: duplicate thread detected", err.getvalue())
        self.assertIn("error: restart failed for pane=%2: duplicate thread detected", err.getvalue())
        self.assertIn("restart: 1 pane(s) failed", out.getvalue())

    def test_restart_target_falls_back_to_dead_session_thread_mapping(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%6", THREAD, window_label="0:6:formal", window_name="formal"),),
            sessions=(_session_record(THREAD, name="formal", state_file="/tmp/one"),),
        ))
        with patch.object(acw, "_build_runtime_model", return_value=model):
            with patch.object(acw, "_restart_one") as restart_one:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    acw.cmd_restart(["formal"])
        restart_one.assert_called_once_with("formal", "%6", [])

    def test_resolve_restart_target_prefers_saved_session_pane_over_window_name(self):
        with patch.object(acw, "_saved_session_panes_for_target", return_value=["%9"]):
            with patch.object(acw, "resolve_pane_from_window_name", return_value="%6"):
                self.assertEqual("%9", acw._resolve_restart_pane_target("formal"))

    def test_restart_target_with_restart_codex_relaunches_same_thread(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%7", THREAD, window_label="0:7:bugs", window_name="bugs"),),
            watchers=(_watcher_record("%7", THREAD),),
            sessions=(_session_record(THREAD, name="bugs", message="continue"),),
        ))
        with patch.object(acw, "_build_runtime_model", return_value=model):
            with patch.object(acw, "_daemon_rpc_restart_codex") as restart_codex:
                with patch.object(
                    acw,
                    "_daemon_rpc_upsert_session",
                    return_value={
                        "thread": THREAD,
                        "pane": "%7",
                        "runtime_status": "running",
                        "runtime_id": "runtime-9999",
                        "daemon_pid": "9999",
                    },
                ) as upsert:
                    acw._restart_one("bugs", "%7", ["--restart-codex"])
        restart_codex.assert_called_once_with("%7", THREAD)
        upsert.assert_called_once()

    def test_restart_target_fails_when_watcher_thread_and_live_pane_disagree(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%2", OTHER_THREAD, window_label="0:2:ddr3", window_name="ddr3"),),
            watchers=(_watcher_record("%2", THREAD),),
            sessions=(_session_record(THREAD, name="ddr3", message="continue"),),
        ))
        err = io.StringIO()
        with patch.object(acw, "_build_runtime_model", return_value=model):
            with patch.object(acw, "stop_pane_watchers") as stop_watchers:
                with patch.object(acw, "cmd_start") as cmd_start:
                    with redirect_stderr(err):
                        with self.assertRaises(SystemExit) as ctx:
                            acw._restart_one("ddr3", "%2", [])
        self.assertEqual(1, ctx.exception.code)
        stop_watchers.assert_not_called()
        cmd_start.assert_not_called()
        self.assertIn("does not match watcher thread", err.getvalue())

    def test_restart_target_prints_progress_steps(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record("%7", THREAD, window_label="0:7:bugs", window_name="bugs"),),
            sessions=(_session_record(THREAD, name="bugs", message="continue"),),
        ))
        with patch.object(acw, "_build_runtime_model", return_value=model):
            with patch.object(
                acw,
                "_daemon_rpc_upsert_session",
                return_value={
                    "thread": THREAD,
                    "pane": "%7",
                    "runtime_status": "running",
                    "runtime_id": "runtime-9999",
                    "daemon_pid": "9999",
                },
            ):
                out = io.StringIO()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    acw._restart_one("bugs", "%7", [])
        text = out.getvalue()
        self.assertIn("restart: preparing pane=%7", text)
        self.assertIn("restart: resolving Codex thread for pane=%7", text)
        self.assertIn(f"restart: thread_id={THREAD}", text)
        self.assertIn("restart: loading saved continue message for pane=%7", text)
        self.assertIn("restart: starting runtime for pane=%7", text)

    def test_restart_target_with_restart_codex_fails_when_thread_unknown(self):
        model = acw._build_runtime_model(_snapshot(
            panes=(_pane_record(
                "%7",
                "",
                window_label="0:7:bugs",
                window_name="bugs",
                detected_thread_detail="could not determine thread_id for pane=%7 from the live Codex process",
                has_codex_process=True,
            ),),
        ))
        with patch.object(acw, "_build_runtime_model", return_value=model):
            with patch.object(acw, "stop_pane_watchers") as stop_watchers:
                err = io.StringIO()
                with redirect_stderr(err):
                    with self.assertRaises(SystemExit) as ctx:
                        acw._restart_one("bugs", "%7", ["--restart-codex"])
        self.assertEqual(1, ctx.exception.code)
        stop_watchers.assert_not_called()
        self.assertIn("could not determine thread_id for pane=%7", err.getvalue())

    def test_codex_top_level_descendants_returns_direct_child_of_shell(self):
        graph = (
            {
                "1000": ("2000",),
                "2000": ("3000",),
            },
            {
                "1000": "bash",
                "2000": "node",
                "3000": "codex",
            },
            {
                "1000": "1",
                "2000": "1000",
                "3000": "2000",
            },
        )
        self.assertEqual(["2000"], acw._codex_top_level_descendants("1000", graph))

    def test_terminate_codex_processes_in_pane_sends_sigterm_to_top_level_descendant(self):
        with patch.object(acw, "_pane_command_snapshot", return_value=("node", "bash", "1000", "")):
            with patch.object(acw, "_codex_top_level_descendants", return_value=["2000"]) as top_level:
                with patch.object(acw.os, "kill") as kill:
                    ok, error = acw._terminate_codex_processes_in_pane("%7")
        self.assertTrue(ok)
        self.assertEqual("", error)
        top_level.assert_called_once_with("1000")
        kill.assert_called_once_with(2000, acw.signal.SIGTERM)

    def test_restart_codex_in_pane_waits_for_shell_before_resume(self):
        with patch.object(acw, "_wait_for_shell_prompt", side_effect=[(False, ""), (True, "")]) as wait_shell:
            with patch.object(acw, "_terminate_codex_processes_in_pane", return_value=(True, "")) as terminate:
                with patch.object(acw, "run_tmux", return_value="") as run_tmux:
                    with patch.object(acw, "_wait_for_codex_process", return_value=(True, "")) as wait_codex:
                        with patch.object(acw, "_accept_directory_trust_prompt_in_pane") as accept_trust:
                            acw._restart_codex_in_pane("%7", THREAD)
        self.assertEqual(
            [
                (("send-keys", "-t", "%7", "-l", f"codex resume {THREAD}"),),
                (("send-keys", "-t", "%7", "C-m"),),
            ],
            run_tmux.call_args_list,
        )
        self.assertEqual(
            [(("%7",), {"attempts": 1}), (("%7",), {})],
            [(call.args, call.kwargs) for call in wait_shell.call_args_list],
        )
        terminate.assert_called_once_with("%7")
        wait_codex.assert_called_once_with("%7")
        accept_trust.assert_called_once_with("%7", timeout_secs=2.0)

    def test_restart_codex_in_pane_fails_when_shell_never_returns(self):
        with patch.object(
            acw,
            "_wait_for_shell_prompt",
            side_effect=[
                (False, ""),
                (False, "pane %7 did not return to a shell prompt (current command 'node', expected 'bash')"),
            ],
        ) as wait_shell:
            with patch.object(acw, "_terminate_codex_processes_in_pane", return_value=(True, "")) as terminate:
                with patch.object(acw, "run_tmux", return_value="") as run_tmux:
                    with redirect_stderr(io.StringIO()) as err:
                        with self.assertRaises(SystemExit) as ctx:
                            acw._restart_codex_in_pane("%7", THREAD)
        self.assertEqual(1, ctx.exception.code)
        terminate.assert_called_once_with("%7")
        self.assertEqual([], run_tmux.call_args_list)
        self.assertEqual(
            [(("%7",), {"attempts": 1}), (("%7",), {})],
            [(call.args, call.kwargs) for call in wait_shell.call_args_list],
        )
        self.assertIn("did not return to a shell prompt", err.getvalue())

    def test_restart_codex_in_pane_fails_when_codex_does_not_relaunch(self):
        with patch.object(acw, "_wait_for_shell_prompt", side_effect=[(False, ""), (True, "")]) as wait_shell:
            with patch.object(acw, "_terminate_codex_processes_in_pane", return_value=(True, "")) as terminate:
                with patch.object(acw, "run_tmux", return_value="") as run_tmux:
                    with patch.object(acw, "_wait_for_codex_process", return_value=(False, "Codex did not relaunch in pane %7")):
                        with redirect_stderr(io.StringIO()) as err:
                            with self.assertRaises(SystemExit) as ctx:
                                acw._restart_codex_in_pane("%7", THREAD)
        self.assertEqual(1, ctx.exception.code)
        self.assertIn(
            (("send-keys", "-t", "%7", "-l", f"codex resume {THREAD}"),),
            run_tmux.call_args_list,
        )
        self.assertEqual(
            [(("%7",), {"attempts": 1}), (("%7",), {})],
            [(call.args, call.kwargs) for call in wait_shell.call_args_list],
        )
        terminate.assert_called_once_with("%7")
        self.assertIn("Codex did not relaunch", err.getvalue())

    def test_accept_directory_trust_prompt_in_pane_confirms_prompt(self):
        with patch.object(
            acw,
            "run_tmux",
            side_effect=[
                "Do you trust the contents of this directory?",
                "",
            ],
        ) as run_tmux:
            with patch.object(acw.time, "sleep") as sleep:
                self.assertTrue(acw._accept_directory_trust_prompt_in_pane("%7", timeout_secs=0.5))
        self.assertEqual(
            [
                (("capture-pane", "-t", "%7", "-p", "-S", "-80"),),
                (("send-keys", "-t", "%7", "C-m"),),
            ],
            run_tmux.call_args_list,
        )
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
