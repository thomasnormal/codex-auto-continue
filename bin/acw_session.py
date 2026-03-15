from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Callable

import acw_state


def read_session_state(thread_id: str, *, state_dir: str) -> dict[str, object]:
    data = acw_state.read_session_state(thread_id, state_dir=state_dir)
    return {str(key): value for key, value in data.items()} if data else {}


def write_session_state(thread_id: str, data: dict, *, state_dir: str) -> None:
    acw_state.write_session_state(thread_id, data, state_dir=state_dir)


def remove_session_state(thread_id: str, *, state_dir: str) -> bool:
    return acw_state.remove_session_state(thread_id, state_dir=state_dir)


def load_sessions(*, state_dir: str, scope: str) -> list[dict[str, object]]:
    rows = acw_state.load_sessions(state_dir=state_dir, scope=scope)
    return [{str(key): value for key, value in row.items()} for row in rows]


def normalize_message_text(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).strip()


def message_text(value: str) -> str:
    return normalize_message_text(value)


def set_thread_name(
    thread_id: str,
    name: str,
    *,
    is_thread_id_fn: Callable[[str], bool],
    write_session_state_fn: Callable[[str, dict], None],
) -> None:
    if not is_thread_id_fn(thread_id):
        return
    write_session_state_fn(thread_id, {"thread_id": thread_id, "name": name})


def ensure_tmux_window_rename_hook(
    *,
    python_executable: str,
    entrypoint: str,
    run_tmux_fn: Callable[..., str | None],
) -> None:
    cmd = (
        f"{shlex.quote(python_executable)} "
        f"{shlex.quote(entrypoint)} "
        "_window-renamed "
        "\"#{window_id}\" "
        "#{q:window_name}"
    )
    run_tmux_fn("set-hook", "-g", "window-renamed", f"run-shell {shlex.quote(cmd)}")


def handle_window_renamed(
    argv: list[str],
    *,
    run_tmux_fn: Callable[..., str | None],
    watcher_rows_fn: Callable[[str], list[dict[str, str]]],
    is_pane_id_fn: Callable[[str], bool],
    is_thread_id_fn: Callable[[str], bool],
    set_thread_name_fn: Callable[[str, str], None],
) -> None:
    if len(argv) < 2:
        return
    window_id = argv[0]
    new_name = argv[1]
    listing = run_tmux_fn("list-panes", "-t", window_id, "-F", "#{pane_id}")
    if not listing:
        return

    seen_threads: set[str] = set()
    for pane in (line.strip() for line in listing.splitlines()):
        if not is_pane_id_fn(pane):
            continue
        rows = watcher_rows_fn(pane)
        thread_id = rows[0]["thread"].lower() if rows and is_thread_id_fn(rows[0].get("thread", "")) else ""
        if not thread_id or thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)
        set_thread_name_fn(thread_id, new_name)
