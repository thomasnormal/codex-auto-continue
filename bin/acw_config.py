from __future__ import annotations

import os
from pathlib import Path

import acw_bootstrap


DEFAULT_MESSAGE_TEXT = (
    "please continue with the workstream.\n\n"
    "Keep momentum, validate changes, and move to the next highest-value task.\n"
)


def resolve_project_cwd() -> str:
    return acw_bootstrap.resolve_project_cwd()


def state_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".codex")


def state_dir_path() -> Path:
    return Path(state_dir())


def codex_log_path() -> Path:
    return state_dir_path() / "log" / "codex-tui.log"


def send_delay_secs() -> str:
    return os.environ.get("AUTO_CONTINUE_SEND_DELAY_SECS", "0.25")


def enter_delay_secs() -> str:
    return os.environ.get("AUTO_CONTINUE_ENTER_DELAY_SECS", "0.15")


def known_shell_commands() -> frozenset[str]:
    return frozenset(
        command
        for command in {
            os.path.basename(os.environ.get("SHELL", "")).strip().lower(),
            "bash",
            "sh",
            "dash",
            "zsh",
            "fish",
            "ksh",
            "tcsh",
            "csh",
        }
        if command
    )
