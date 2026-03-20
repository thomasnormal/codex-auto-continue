import tempfile
import time
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_status  # noqa: E402


THREAD = "019cd7fb-f4c1-7613-bf98-4915dcb1970a"


class AcwStatusUnitTests(unittest.TestCase):
    def test_average_continue_label_uses_saved_continue_count(self):
        now = time.time()
        label = acw_status.average_continue_label(
            {
                "thread": THREAD,
                "watch": "",
            },
            {
                "continue_count": "3",
            },
            thread_started_at_fn=lambda _thread_id: now - (3 * 4200),
            continue_count_for_watch_log_fn=lambda _watch_log: 0,
            format_age_fn=lambda epoch, compact: "1h10m" if compact and abs(epoch - (now - 4200)) < 1.0 else "?",
            now_fn=lambda: now,
        )

        self.assertEqual("1h10m/acw", label)

    def test_average_continue_label_falls_back_to_watch_log_when_count_missing(self):
        with tempfile.TemporaryDirectory() as td:
            watch_log = Path(td) / "watch.log"
            watch_log.write_text(
                "watch: pane=%11 thread_id=019cd7fb-f4c1-7613-bf98-4915dcb1970a\n"
                "continue: sent turn=1 thread=019cd7fb-f4c1-7613-bf98-4915dcb1970a\n"
                "continue: sent turn=2 thread=019cd7fb-f4c1-7613-bf98-4915dcb1970a\n",
                encoding="utf-8",
            )
            now = time.time()

            label = acw_status.average_continue_label(
                {
                    "thread": THREAD,
                    "watch": str(watch_log),
                },
                {},
                thread_started_at_fn=lambda _thread_id: now - 7200,
                continue_count_for_watch_log_fn=acw_status.continue_count_for_watch_log,
                format_age_fn=lambda epoch, compact: "1h00m" if compact and abs(epoch - (now - 3600)) < 1.0 else "?",
                now_fn=lambda: now,
            )

        self.assertEqual("1h00m/acw", label)

    def test_status_table_combines_started_last_acw_and_average(self):
        captured: list[tuple[str, str, str, str, str]] = []
        acw_status.status_table(
            [(
                {
                    "thread": THREAD,
                    "state": "sqlite:session:" + THREAD,
                    "watch": "",
                    "runtime_id": "runtime-1",
                    "cwd": "/repo",
                },
                "%11",
                "0:11:formal",
            )],
            read_state_json_fn=lambda _state: {
                "last_continue_at": "2026-03-19 12:34:56",
                "continue_count": "3",
            },
            compute_state_fn=lambda _row, _session_json: "running",
            status_row_detail_fn=lambda _row, _session_json: "",
            thread_times_fn=lambda _thread, compact: ("3h30m", "5m"),
            average_continue_label_fn=lambda _row, _session_json: "1h10m/acw",
            format_age_fn=lambda _epoch, compact: "5m",
            last_agent_snippet_for_pane_fn=lambda _pane, _thread: "Explored",
            paused_activity_for_pane_fn=lambda _pane: "",
            message_summary_for_row_fn=lambda _row: "continue",
            branch_for_thread_fn=lambda _thread: "main",
            status_window_pane_cell_fn=lambda window_label, pane, thread, branch: f"{window_label}/{pane}\n{thread}\nbr={branch}",
            state_summary_fn=lambda state, _row: state,
            render_status_table_fn=lambda rows_data, _section_every, _max_msg_lines: captured.extend(rows_data),
            section_every=3,
            max_msg_lines=3,
        )

        self.assertEqual("s: 3h30m\nl: 5m\na: 1h10m", captured[0][2])
        self.assertEqual("0:11:formal/%11\n019cd7fb-f4c1-7613-bf98-4915dcb1970a\nbr=main", captured[0][0])

    def test_status_rows_data_uses_branch_for_thread(self):
        rows_data = acw_status.status_rows_data(
            [(
                {
                    "thread": THREAD,
                    "state": "sqlite:session:" + THREAD,
                    "watch": "",
                    "runtime_id": "runtime-1",
                    "cwd": "/wrong",
                },
                "%11",
                "0:11:formal",
            )],
            read_state_json_fn=lambda _state: {},
            compute_state_fn=lambda _row, _session_json: "running",
            status_row_detail_fn=lambda _row, _session_json: "",
            thread_times_fn=lambda _thread, _compact: ("3h30m", "5m"),
            average_continue_label_fn=lambda _row, _session_json: "",
            format_age_fn=lambda _epoch, _compact: "5m",
            last_agent_snippet_for_pane_fn=lambda _pane, _thread: "Explored",
            paused_activity_for_pane_fn=lambda _pane: "",
            message_summary_for_row_fn=lambda _row: "continue",
            branch_for_thread_fn=lambda thread: "main" if thread == THREAD else "-",
            status_window_pane_cell_fn=lambda window_label, pane, thread, branch: f"{window_label}/{pane}\n{thread}\nbr={branch}",
            state_summary_fn=lambda state, _row: state,
        )

        self.assertEqual("0:11:formal/%11\n019cd7fb-f4c1-7613-bf98-4915dcb1970a\nbr=main", rows_data[0][0])

    def test_status_rows_data_shows_unknown_when_thread_branch_unknown(self):
        rows_data = acw_status.status_rows_data(
            [(
                {
                    "thread": THREAD,
                    "state": "sqlite:session:" + THREAD,
                    "watch": "",
                    "runtime_id": "runtime-1",
                    "cwd": "/wrong",
                },
                "%11",
                "0:11:formal",
            )],
            read_state_json_fn=lambda _state: {},
            compute_state_fn=lambda _row, _session_json: "running",
            status_row_detail_fn=lambda _row, _session_json: "",
            thread_times_fn=lambda _thread, _compact: ("3h30m", "5m"),
            average_continue_label_fn=lambda _row, _session_json: "",
            format_age_fn=lambda _epoch, _compact: "5m",
            last_agent_snippet_for_pane_fn=lambda _pane, _thread: "Explored",
            paused_activity_for_pane_fn=lambda _pane: "",
            message_summary_for_row_fn=lambda _row: "continue",
            branch_for_thread_fn=lambda _thread: "-",
            status_window_pane_cell_fn=lambda window_label, pane, thread, branch: f"{window_label}/{pane}\n{thread}\nbr={branch}",
            state_summary_fn=lambda state, _row: state,
        )

        self.assertEqual("0:11:formal/%11\n019cd7fb-f4c1-7613-bf98-4915dcb1970a\nbr=-", rows_data[0][0])


if __name__ == "__main__":
    unittest.main()
