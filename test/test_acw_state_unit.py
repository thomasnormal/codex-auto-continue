import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch
import sqlite3

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_state  # noqa: E402


THREAD = "019cd7fb-f4c1-7613-bf98-4915dcb1970a"


class AcwStateUnitTests(unittest.TestCase):
    def tearDown(self):
        acw_state.clear_connection_init_cache()

    def test_write_session_state_persists_continue_count(self):
        with tempfile.TemporaryDirectory() as td:
            acw_state.write_session_state(
                THREAD,
                {
                    "thread_id": THREAD,
                    "message": "continue",
                    "continue_count": 7,
                },
                state_dir=td,
            )

            state = acw_state.read_session_state(THREAD, state_dir=td)

        self.assertEqual(7, state["continue_count"])

    def test_write_session_state_persists_last_proven_workdir_and_branch(self):
        with tempfile.TemporaryDirectory() as td:
            acw_state.write_session_state(
                THREAD,
                {
                    "thread_id": THREAD,
                    "message": "continue",
                    "last_proven_workdir": "/home/thomas-ahle/circt-ddr3-debug",
                    "last_proven_branch": "ddr3-debug-20260320",
                },
                state_dir=td,
            )

            state = acw_state.read_session_state(THREAD, state_dir=td)

        self.assertEqual("/home/thomas-ahle/circt-ddr3-debug", state["last_proven_workdir"])
        self.assertEqual("ddr3-debug-20260320", state["last_proven_branch"])

    def test_connect_initializes_schema_once_per_state_dir(self):
        with tempfile.TemporaryDirectory() as td:
            Path(acw_state.state_db_path(td)).touch()
            fake_conn1 = MagicMock()
            fake_conn2 = MagicMock()

            with patch.object(acw_state.sqlite3, "connect", side_effect=[fake_conn1, fake_conn2]):
                with patch.object(acw_state, "_ensure_schema") as ensure_schema:
                    with patch.object(acw_state, "_maybe_enable_wal") as enable_wal:
                        with patch.object(acw_state, "_maybe_migrate_legacy_json") as migrate_json:
                            with patch.object(acw_state, "_maybe_migrate_legacy_sqlite_tables") as migrate_sqlite:
                                acw_state._connect(td)
                                acw_state._connect(td)

        ensure_schema.assert_called_once_with(fake_conn1)
        enable_wal.assert_called_once_with(fake_conn1)
        migrate_json.assert_called_once_with(fake_conn1, td)
        migrate_sqlite.assert_called_once_with(fake_conn1)

    def test_connect_retries_once_after_operational_error(self):
        with tempfile.TemporaryDirectory() as td:
            Path(acw_state.state_db_path(td)).touch()
            bad_conn = MagicMock()
            good_conn = MagicMock()
            bad_conn.execute.side_effect = sqlite3.OperationalError("unable to open database file")

            with patch.object(acw_state.sqlite3, "connect", side_effect=[bad_conn, good_conn]):
                with patch.object(acw_state, "_ensure_schema") as ensure_schema:
                    with patch.object(acw_state, "_maybe_enable_wal") as enable_wal:
                        with patch.object(acw_state, "_maybe_migrate_legacy_json") as migrate_json:
                            with patch.object(acw_state, "_maybe_migrate_legacy_sqlite_tables") as migrate_sqlite:
                                conn = acw_state._connect(td)

        self.assertIs(good_conn, conn)
        bad_conn.close.assert_called_once()
        ensure_schema.assert_called_once_with(good_conn)
        enable_wal.assert_called_once_with(good_conn)
        migrate_json.assert_called_once_with(good_conn, td)
        migrate_sqlite.assert_called_once_with(good_conn)

    def test_read_runtime_rows_does_not_delete_dead_rows(self):
        with tempfile.TemporaryDirectory() as td:
            acw_state.write_session_state(
                THREAD,
                {
                    "thread_id": THREAD,
                    "pane": "%7",
                    "name": "formal",
                    "message": "continue",
                    "tmux_socket": "",
                },
                state_dir=td,
            )
            acw_state.write_runtime_state(
                THREAD,
                {
                    "runtime_id": "runtime-7",
                    "daemon_pid": "12345",
                    "pane": "%7",
                    "state": acw_state.session_state_ref(THREAD),
                    "watch": "/tmp/watch.log",
                    "cwd": "/repo",
                    "msg_inline": "continue",
                    "paused": False,
                    "runtime_status": "running",
                    "runtime_detail": "",
                    "generation": 1,
                    "worker_label": "runtime-7",
                    "heartbeat_ts": "2026-03-21 12:00:00",
                },
                state_dir=td,
                scope="",
            )

            with patch.object(acw_state.os, "kill", side_effect=OSError(3, "No such process")):
                rows = acw_state.read_runtime_rows(state_dir=td, scope="")

            conn = acw_state._connect(td)
            try:
                runtime_count = conn.execute("SELECT COUNT(*) FROM agent_runtimes").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual([], rows)
        self.assertEqual(1, runtime_count)


if __name__ == "__main__":
    unittest.main()
