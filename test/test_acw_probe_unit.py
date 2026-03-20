import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import acw_probe  # noqa: E402


class AcwProbeUnitTests(unittest.TestCase):
    def test_paused_activity_from_pane_text_detects_directory_trust_prompt_as_input(self):
        text = (
            "> You are in /home/thomas-ahle/codex-auto-continue\n"
            "\n"
            "  Do you trust the contents of this directory? Working with untrusted contents comes with "
            "higher risk of prompt injection.\n"
            "\n"
            "› 1. Yes, continue\n"
            "  2. No, quit\n"
            "\n"
            "  Press enter to continue\n"
            "\n"
            "╭──────────────────────────────────────────────────╮\n"
            "│ >_ OpenAI Codex (v0.114.0)                       │\n"
            "╰──────────────────────────────────────────────────╯\n"
        )

        self.assertEqual("input", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_from_pane_text_detects_tool_approval_prompt_as_input(self):
        text = (
            "Reviewing approval request\n"
            "\n"
            "Tool call needs your approval. Reason: network access outside the allowlist.\n"
            "Do you want to approve network access to \"https://example.com\"?\n"
            "\n"
            "› 1. Yes, allow\n"
            "  2. No, continue without permissions\n"
            "\n"
            "  ctrl + p / ctrl + n change field\n"
        )

        self.assertEqual("input", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_from_pane_text_detects_waiting_for_input(self):
        text = (
            "Running scope as unit: run-r45f69361fa3c4de882dca4bdf4996f6b.scope\n"
            "╭─────────────────────────────────────────────╮\n"
            "│ >_ OpenAI Codex (v0.114.0)                  │\n"
            "╰─────────────────────────────────────────────╯\n"
            "\n"
            "› Use /skills to list available skills\n"
            "\n"
            "  gpt-5.4 xhigh · 100% left · ~\n"
        )

        self.assertEqual("waiting", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_from_pane_text_detects_thinking_working(self):
        text = (
            "› Investigate the failure\n"
            "\n"
            "working...\n"
            "\n"
            "  gpt-5.4 xhigh · 99% left · ~\n"
        )

        self.assertEqual("busy", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_prefers_working_over_queued_prompt(self):
        text = (
            "• The detached #0 tracker is green now on today’s CIRCT build.\n"
            "\n"
            "• Explored\n"
            "  └ Read uvm-report-message-empty-elements-runtime.sv\n"
            "\n"
            "• Working (53m 10s • esc to interrupt)\n"
            "\n"
            "\n"
            "› Explain this codebase\n"
            "\n"
            "  gpt-5.4 xhigh · 6% left · ~/circt\n"
        )

        self.assertEqual("busy", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_keeps_scanning_above_prompt_for_busy_indicator(self):
        text = (
            "• Both phase-hopper locals are already stale on today’s build.\n"
            "\n"
            "◦ Waiting for background terminal (1h 17m 17s • esc to interrupt) · "
            "1 background terminal running · /ps to view · /clean to close\n"
            "  └ set -euo pipefail…\n"
            "\n"
            "› Explain this codebase\n"
            "\n"
            "  gpt-5.4 xhigh · 45% left · ~/circt\n"
        )

        self.assertEqual("busy", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_ignores_stale_working_line_once_prompt_returns(self):
        text = (
            "working...\n"
            "\n"
            "• RUNWAIT\n"
            "\n"
            "›\n"
        )

        self.assertEqual("waiting", acw_probe.paused_activity_from_pane_text(text))

    def test_paused_activity_detects_plan_question_as_asking(self):
        text = (
            "Implement this plan?\n"
            "\n"
            "› 1. Yes, implement this plan  Switch to Default and start coding.\n"
            "  2. No, stay in Plan mode     Continue planning with the model.\n"
            "\n"
            "  Press enter to confirm or esc to go back\n"
        )

        self.assertEqual("asking", acw_probe.paused_activity_from_pane_text(text))

    def test_extract_last_agent_snippet_ignores_approval_prompt_chrome(self):
        text = (
            "• The previous real agent update.\n"
            "\n"
            "Reviewing approval request\n"
            "Tool call needs your approval. Reason: network access outside the allowlist.\n"
            "› 1. Yes, allow\n"
            "  2. No, continue without permissions\n"
        )

        self.assertEqual("The previous real agent update.", acw_probe.extract_last_agent_snippet(text))

    def test_repo_root_hint_from_pane_text_prefers_explicit_repo_path_over_prompt_path(self):
        text = (
            "• Edited ~/circt-debug-ppa/tools/circt-synth/circt-synth.cpp (+1 -1)\n"
            "\n"
            "› Summarize recent commits\n"
            "\n"
            "  gpt-5.4 high · 71% left · ~/circt\n"
        )

        def resolve_repo_root(path: str) -> str:
            if path.startswith("/home/thomas-ahle/circt-debug-ppa"):
                return "/home/thomas-ahle/circt-debug-ppa"
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        self.assertEqual(
            "/home/thomas-ahle/circt-debug-ppa",
            acw_probe.repo_root_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                home_dir="/home/thomas-ahle",
            ),
        )

    def test_repo_root_hint_from_pane_text_uses_last_valid_repo_path_in_command_line(self):
        text = (
            "• Ran /usr/lib64/ccache/clang++ -I/home/thomas-ahle/circt/llvm/llvm/include "
            "-I/home/thomas-ahle/circt-debug-ppa/include "
            "-c /home/thomas-ahle/circt-debug-ppa/tools/circt-synth/circt-synth.cpp\n"
            "\n"
            "  gpt-5.4 high · 71% left · ~/circt\n"
        )

        def resolve_repo_root(path: str) -> str:
            if path.startswith("/home/thomas-ahle/circt-debug-ppa"):
                return "/home/thomas-ahle/circt-debug-ppa"
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        self.assertEqual(
            "/home/thomas-ahle/circt-debug-ppa",
            acw_probe.repo_root_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                home_dir="/home/thomas-ahle",
            ),
        )

    def test_repo_root_hint_from_pane_text_stops_after_first_valid_path_from_right(self):
        text = (
            "• Ran /usr/lib64/ccache/clang++ -I/home/thomas-ahle/circt/llvm/llvm/include "
            "-I/home/thomas-ahle/circt-debug-ppa/include "
            "-c /home/thomas-ahle/circt-debug-ppa/tools/circt-synth/circt-synth.cpp\n"
        )
        seen: list[str] = []

        def resolve_repo_root(path: str) -> str:
            seen.append(path)
            if path.startswith("/home/thomas-ahle/circt-debug-ppa"):
                return "/home/thomas-ahle/circt-debug-ppa"
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        self.assertEqual(
            "/home/thomas-ahle/circt-debug-ppa",
            acw_probe.repo_root_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                home_dir="/home/thomas-ahle",
            ),
        )
        self.assertEqual(
            ["/home/thomas-ahle/circt-debug-ppa/tools/circt-synth/circt-synth.cpp"],
            seen,
        )

    def test_repo_root_hint_from_pane_text_ignores_more_recent_build_dir_repo_paths(self):
        text = (
            "main_build = Path('/home/thomas-ahle/circt/build_debugger_plan')\n"
            "root = Path('/home/thomas-ahle/circt-debug-ppa')\n"
            "\n"
            "  gpt-5.4 high · 71% left · ~/circt\n"
        )

        def resolve_repo_root(path: str) -> str:
            if path.startswith("/home/thomas-ahle/circt-debug-ppa"):
                return "/home/thomas-ahle/circt-debug-ppa"
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        self.assertEqual(
            "/home/thomas-ahle/circt-debug-ppa",
            acw_probe.repo_root_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                home_dir="/home/thomas-ahle",
            ),
        )

    def test_repo_root_hint_from_pane_text_uses_prompt_path_when_no_explicit_repo_path_found(self):
        text = (
            "› Use /skills to list available skills\n"
            "\n"
            "  gpt-5.4 xhigh · 100% left · ~/circt\n"
        )

        def resolve_repo_root(path: str) -> str:
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        self.assertEqual(
            "/home/thomas-ahle/circt",
            acw_probe.repo_root_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                home_dir="/home/thomas-ahle",
            ),
        )

    def test_branch_hint_from_pane_text_prefers_explicit_branch_output_over_main_repo_path(self):
        text = (
            "Switched to branch 'debug/ppa-liberty'\n"
            "/home/thomas-ahle/circt/lib/Support/Foo.cpp\n"
        )

        def resolve_repo_root(path: str) -> str:
            if path.startswith("/home/thomas-ahle/circt-debug-ppa"):
                return "/home/thomas-ahle/circt-debug-ppa"
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        def branch_for_path(path: str) -> str:
            return {
                "/home/thomas-ahle/circt": "main",
                "/home/thomas-ahle/circt-debug-ppa": "debug/ppa-liberty",
            }.get(path, "-")

        self.assertEqual(
            "debug/ppa-liberty",
            acw_probe.branch_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                branch_for_path_fn=branch_for_path,
                home_dir="/home/thomas-ahle",
                preferred_repo_root="/home/thomas-ahle/circt",
            ),
        )

    def test_branch_hint_from_pane_text_prefers_nonpreferred_repo_root_over_shared_main_root(self):
        text = (
            "• Ran git -C /home/thomas-ahle/circt status --short\n"
            "• Edited /home/thomas-ahle/circt-todo-sweep/lib/Conversion/ImportVerilog/Structure.cpp (+2 -1)\n"
        )

        def resolve_repo_root(path: str) -> str:
            if path.startswith("/home/thomas-ahle/circt-todo-sweep"):
                return "/home/thomas-ahle/circt-todo-sweep"
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        def branch_for_path(path: str) -> str:
            return {
                "/home/thomas-ahle/circt": "main",
                "/home/thomas-ahle/circt-todo-sweep": "@afa9e32f26",
            }.get(path, "-")

        self.assertEqual(
            "@afa9e32f26",
            acw_probe.branch_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                branch_for_path_fn=branch_for_path,
                home_dir="/home/thomas-ahle",
                preferred_repo_root="/home/thomas-ahle/circt",
            ),
        )

    def test_branch_hint_from_pane_text_prefers_explicit_branch_even_when_newer_path_is_other_repo(self):
        text = (
            "[thomas-ahle@thomas-ahle codex-auto-continue]$ codex 'say exactly BRANCHHINT and nothing else'\n"
            "working...\n"
            "• Switched to branch 'debug/ppa-liberty'\n"
            "working...\n"
            "• /home/thomas-ahle/circt/lib/Support/Foo.cpp\n"
            "›\n"
        )

        def resolve_repo_root(path: str) -> str:
            if path.startswith("/home/thomas-ahle/codex-auto-continue"):
                return "/home/thomas-ahle/codex-auto-continue"
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        def branch_for_path(path: str) -> str:
            return {
                "/home/thomas-ahle/codex-auto-continue": "main",
                "/home/thomas-ahle/circt": "main",
                "/home/thomas-ahle/circt-debug-ppa": "debug/ppa-liberty",
            }.get(path, "-")

        self.assertEqual(
            "debug/ppa-liberty",
            acw_probe.branch_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                branch_for_path_fn=branch_for_path,
                home_dir="/home/thomas-ahle",
                preferred_repo_root="/home/thomas-ahle/codex-auto-continue",
            ),
        )

    def test_workdir_hint_from_pane_text_prefers_explicit_working_directory_line(self):
        text = (
            "• I’m continuing in /home/thomas-ahle/circt-todo-sweep, not ~/circt main.\n"
            "• Ran git -C /home/thomas-ahle/circt status --short\n"
        )

        def resolve_repo_root(path: str) -> str:
            if path.startswith("/home/thomas-ahle/circt-todo-sweep"):
                return "/home/thomas-ahle/circt-todo-sweep"
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        self.assertEqual(
            "/home/thomas-ahle/circt-todo-sweep",
            acw_probe.workdir_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                home_dir="/home/thomas-ahle",
            ),
        )

    def test_workdir_hint_from_pane_text_prefers_git_c_path_when_no_working_line_present(self):
        text = (
            "• Ran git -C /home/thomas-ahle/circt-hopper-wt status --short --branch\n"
            "  └ ## HEAD (no branch)\n"
        )

        def resolve_repo_root(path: str) -> str:
            if path.startswith("/home/thomas-ahle/circt-hopper-wt"):
                return "/home/thomas-ahle/circt-hopper-wt"
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        self.assertEqual(
            "/home/thomas-ahle/circt-hopper-wt",
            acw_probe.workdir_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                home_dir="/home/thomas-ahle",
            ),
        )

    def test_workdir_hint_from_pane_text_prefers_dominant_worktree_over_later_shared_repo_query(self):
        text = "\n".join(
            [
                "• Ran /home/thomas-ahle/circt-worktree-qda9-frontier/utils/ninja-with-lock.sh -C /home/thomas-ahle/circt-worktree-qda9-frontier/build_frontier circt-verilog",
                "• Ran git -C /home/thomas-ahle/circt-worktree-qda9-frontier status --short",
                "• Ran /home/thomas-ahle/circt-worktree-qda9-frontier/utils/ninja-with-lock.sh -C /home/thomas-ahle/circt-worktree-qda9-frontier/build_frontier circt-sim",
                "• Ran git -C /home/thomas-ahle/circt log --oneline -n 20",
            ]
        )

        def resolve_repo_root(path: str) -> str:
            if path.startswith("/home/thomas-ahle/circt-worktree-qda9-frontier"):
                return "/home/thomas-ahle/circt-worktree-qda9-frontier"
            if path.startswith("/home/thomas-ahle/circt"):
                return "/home/thomas-ahle/circt"
            return ""

        self.assertEqual(
            "/home/thomas-ahle/circt-worktree-qda9-frontier",
            acw_probe.workdir_hint_from_pane_text(
                text,
                resolve_repo_root_fn=resolve_repo_root,
                home_dir="/home/thomas-ahle",
            ),
        )


if __name__ == "__main__":
    unittest.main()
