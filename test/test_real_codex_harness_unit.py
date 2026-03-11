import tempfile
import time
import unittest
from pathlib import Path

from test.support.real_codex_harness import harness_process_roots
from test.support.real_codex_harness import stale_harness_roots


class RealCodexHarnessUnitTests(unittest.TestCase):
    def test_stale_harness_roots_marks_dead_owner_pid_as_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir).chmod(0o700)
            root = Path(tmpdir) / "real-codex-old"
            root.mkdir()
            root.chmod(0o700)
            (root / "owner.pid").write_text("999999\n", encoding="utf-8")

            stale = stale_harness_roots(
                Path(tmpdir),
                active_owner_pid=1234,
                live_pids={1234},
            )

        self.assertEqual([root], stale)

    def test_stale_harness_roots_skips_live_owner_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir).chmod(0o700)
            root = Path(tmpdir) / "real-codex-live"
            root.mkdir()
            root.chmod(0o700)
            (root / "owner.pid").write_text("2222\n", encoding="utf-8")

            stale = stale_harness_roots(
                Path(tmpdir),
                active_owner_pid=1234,
                live_pids={1234, 2222},
            )

        self.assertEqual([], stale)

    def test_stale_harness_roots_marks_old_ownerless_dir_after_grace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir).chmod(0o700)
            root = Path(tmpdir) / "real-codex-ownerless"
            root.mkdir()
            root.chmod(0o700)
            stale = stale_harness_roots(
                Path(tmpdir),
                active_owner_pid=1234,
                live_pids={1234},
                now=time.time() + 600.0,
                grace_secs=300.0,
            )

        self.assertEqual([root], stale)

    def test_harness_process_roots_extracts_private_harness_roots(self):
        temp_root = Path("/tmp/auto-continue-e2e-tmp")
        ps_out = (
            "111 tmux -S /tmp/auto-continue-e2e-tmp/real-codex-abcd/tmux/tmux.sock new-session -d\n"
            "222 python3 /repo/bin/auto_continue_logwatch.py --tmux-socket "
            "/tmp/auto-continue-e2e-tmp/real-codex-efgh/tmux/tmux.sock\n"
            "333 python3 /repo/bin/auto_continue_logwatch.py --tmux-socket /tmp/other/tmux.sock\n"
        )

        roots = harness_process_roots(temp_root, ps_out)

        self.assertEqual(
            {
                Path("/tmp/auto-continue-e2e-tmp/real-codex-abcd"),
                Path("/tmp/auto-continue-e2e-tmp/real-codex-efgh"),
            },
            roots,
        )


if __name__ == "__main__":
    unittest.main()
