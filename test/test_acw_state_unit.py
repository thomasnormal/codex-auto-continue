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


if __name__ == "__main__":
    unittest.main()
