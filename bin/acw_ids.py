from __future__ import annotations

import os
import re


THREAD_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
PANE_ID_RE = re.compile(r"%[0-9]+")


def is_thread_id(value: str) -> bool:
    return bool(THREAD_ID_RE.fullmatch(value or ""))


def is_pane_id(value: str) -> bool:
    return bool(PANE_ID_RE.fullmatch(value or ""))


def current_tmux_pane(*, env: dict[str, str] | None = None) -> str:
    environment = os.environ if env is None else env
    pane = environment.get("TMUX_PANE", "").strip()
    return pane if is_pane_id(pane) else ""


def short_thread_id(thread_id: str) -> str:
    if not is_thread_id(thread_id):
        return thread_id
    return f"{thread_id[:8]}…{thread_id[-4:]}"
