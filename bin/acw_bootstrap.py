from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable


def resolve_project_cwd() -> str:
    env = os.environ.get("AUTO_CONTINUE_PROJECT_CWD", "")
    if env:
        if os.path.isdir(env):
            return str(Path(env).resolve())
        return env

    pwd_real = str(Path.cwd().resolve())
    try:
        git_root = subprocess.check_output(
            ["git", "-C", pwd_real, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        git_root = ""
    if git_root:
        return git_root

    if os.path.basename(pwd_real) == ".codex":
        return os.path.dirname(pwd_real)

    return pwd_real


def build_help_text(prog: str) -> str:
    return f"""Usage:
  {prog}
  {prog} watch [target]
  {prog} status [target] [--details]
  {prog} start <target> [thread-id] [--message TEXT]
  {prog} stop [target]
  {prog} pause [target]
  {prog} resume [target ...]
  {prog} restart [target]
  {prog} edit <target>
  {prog} repair [target] [--dry-run|--all]
  {prog} cleanup
  {prog} doctor [target]

Commands:
  watch     Continuously refresh managed session state. In an interactive terminal you can also type e/edit, pause, or restart commands directly in the watch view. Default command when no subcommand is given in an interactive terminal.
  status    Show managed session state once. Default command when no subcommand is given without a TTY.
  start     Start managing a live tmux target. Auto-detect the Codex thread unless you pass one explicitly.
  stop      Stop one managed session, one pane's runtimes, or all managed sessions, and forget their saved sessions.
  pause     Pause one managed session, or all managed sessions when no target is given.
  resume    Restart one or more paused managed sessions, or all paused managed sessions, from current state.
  restart   Restart one managed session or all live panes, including recoverable dead sessions, when no target is given. Use --restart-codex on a single target to relaunch Codex with the same thread.
  edit      Edit the stored continue message for a running managed session, then restart it.
  repair    Inspect or apply safe live-state repairs using the invariant model. With no target it defaults to --dry-run; use --all to apply safe repairs across all panes.
  cleanup   Remove dead saved sessions and stale daemon/runtime files.
  doctor    Check tmux reachability, Codex auth/state, and optional pane/thread health.

Targets:
  Commands with a target accept a tmux pane id (%6), window index (2), session:window (0:2), or exact tmux window name (uvm).
  pause, resume, and restart act on all managed sessions when no target is given. resume also accepts multiple explicit targets.

Examples:
  {prog}
  {prog} status --details
  {prog} start %6 --message "continue"
  {prog} start uvm 019cb235-bc2c-7920-8832-f2d5656fead8
  {prog} resume formal aot
  {prog} restart bugs --restart-codex
  {prog} edit %6
  {prog} repair --dry-run
  {prog} repair %6
  {prog} repair --all
  {prog} restart
  {prog} doctor
"""


def print_help(
    *,
    argv0: str,
    build_help_text_fn: Callable[[str], str],
    file=None,
) -> None:
    if file is None:
        file = sys.stdout
    prog = os.path.basename(argv0) or "acw"
    print(build_help_text_fn(prog), file=file)


def main(
    *,
    argv: list[str],
    cleanup_stale_files_fn: Callable[[], None],
    print_help_fn: Callable[..., None],
    commands: dict[str, Callable[[list[str]], None]],
    stdout_isatty_fn: Callable[[], bool],
) -> None:
    if len(argv) < 2:
        subcmd = "watch" if stdout_isatty_fn() else "status"
        rest: list[str] = []
    else:
        subcmd = argv[1]
        rest = argv[2:]

    if subcmd in ("-h", "--help", "help"):
        print_help_fn(file=sys.stdout)
        raise SystemExit(0)

    if subcmd in ("start", "stop", "restart", "edit", "repair", "cleanup"):
        cleanup_stale_files_fn()

    if subcmd in commands:
        commands[subcmd](rest)
        return

    print_help_fn(file=sys.stderr)
    raise SystemExit(2)
