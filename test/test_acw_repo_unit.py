import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_repo  # noqa: E402


class AcwRepoUnitTests(unittest.TestCase):
    def tearDown(self):
        acw_repo.clear_branch_cache()

    def test_branch_for_cwd_reads_symbolic_branch(self):
        with tempfile.TemporaryDirectory() as td:
            calls: list[list[str]] = []

            def fake_run(args, **_kwargs):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")

            branch = acw_repo.branch_for_cwd(td, ttl_secs=0.0, run_fn=fake_run)

        self.assertEqual("main", branch)
        self.assertEqual([["git", "-C", str(Path(td).resolve()), "symbolic-ref", "--quiet", "--short", "HEAD"]], calls)

    def test_branch_for_cwd_falls_back_to_detached_head(self):
        with tempfile.TemporaryDirectory() as td:
            results = [
                subprocess.CompletedProcess([], 1, stdout="", stderr="detached"),
                subprocess.CompletedProcess([], 0, stdout="12e2319\n", stderr=""),
            ]

            def fake_run(_args, **_kwargs):
                return results.pop(0)

            branch = acw_repo.branch_for_cwd(td, ttl_secs=0.0, run_fn=fake_run)

        self.assertEqual("@12e2319", branch)

    def test_branch_for_cwd_caches_recent_result(self):
        with tempfile.TemporaryDirectory() as td:
            calls = 0

            def fake_run(args, **_kwargs):
                nonlocal calls
                calls += 1
                return subprocess.CompletedProcess(args, 0, stdout="feature/really-long-branch-name\n", stderr="")

            branch1 = acw_repo.branch_for_cwd(td, ttl_secs=10.0, time_fn=lambda: 100.0, run_fn=fake_run)
            branch2 = acw_repo.branch_for_cwd(td, ttl_secs=10.0, time_fn=lambda: 101.0, run_fn=fake_run)

        self.assertEqual("feature/really-long…", branch1)
        self.assertEqual(branch1, branch2)
        self.assertEqual(1, calls)

    def test_repo_root_for_path_walks_file_path_up_to_git_root(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            nested = repo / "tools" / "circt-synth"
            nested.mkdir(parents=True)
            file_path = nested / "circt-synth.cpp"
            file_path.write_text("// test\n", encoding="utf-8")
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)

            root = acw_repo.repo_root_for_path(str(file_path), ttl_secs=0.0)

        self.assertEqual(str(repo.resolve()), root)

    def test_branch_for_path_uses_repo_root_for_file_path(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            nested = repo / "include"
            nested.mkdir(parents=True)
            file_path = nested / "Thing.h"
            file_path.write_text("// test\n", encoding="utf-8")
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)

            calls: list[list[str]] = []

            def fake_run(args, **_kwargs):
                calls.append(args)
                if args[3:6] == ["rev-parse", "--show-toplevel"]:
                    return subprocess.CompletedProcess(args, 0, stdout=str(repo.resolve()) + "\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")

            branch = acw_repo.branch_for_path(str(file_path), ttl_secs=0.0, run_fn=fake_run)

        self.assertEqual("main", branch)
        self.assertEqual(
            [
                ["git", "-C", str(nested.resolve()), "rev-parse", "--show-toplevel"],
                ["git", "-C", str(repo.resolve()), "symbolic-ref", "--quiet", "--short", "HEAD"],
            ],
            calls,
        )


if __name__ == "__main__":
    unittest.main()
