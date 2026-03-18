from __future__ import annotations

from collections import defaultdict
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from acw_ids import is_thread_id

_PANE_CHROME_ONLY_RE = re.compile(r"^[\s─━╭╮╰╯│┌┐└┘═]+$")
_SHELL_PROMPT_RE = re.compile(r"^\[[^\]]+\]\$\s*$")
_WORKING_STATUS_RE = re.compile(
    r"^(?:[•◦]\s*)?(?:working(?:\.\.\.)?|messages to be submitted after next tool call)\b",
    re.IGNORECASE,
)


def _freeze_multimap(mapping: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    return {key: tuple(values) for key, values in mapping.items()}


def build_pane_window_map(run_tmux) -> dict[str, str]:
    listing = run_tmux("list-panes", "-a", "-F", "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}")
    mapping: dict[str, str] = {}
    if listing:
        for line in listing.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4 and parts[3]:
                mapping[parts[3]] = f"{parts[0]}:{parts[1]}:{parts[2]}"
    return mapping


def load_process_graph(check_output) -> tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str]]:
    try:
        output = check_output(
            ["ps", "-eo", "pid=", "-o", "ppid=", "-o", "comm="],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}, {}, {}

    children_by_parent: dict[str, list[str]] = defaultdict(list)
    command_by_pid: dict[str, str] = {}
    parent_by_pid: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, command = parts
        if not pid.isdigit() or not ppid.isdigit():
            continue
        children_by_parent[ppid].append(pid)
        command_by_pid[pid] = command.strip()
        parent_by_pid[pid] = ppid

    return _freeze_multimap(children_by_parent), command_by_pid, parent_by_pid


def load_process_snapshot(check_output) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    children_by_parent, command_by_pid, _parent_by_pid = load_process_graph(check_output)
    return children_by_parent, command_by_pid


def codex_pids_in_process_tree(
    shell_pid: str,
    process_snapshot: tuple[dict[str, tuple[str, ...]], dict[str, str]] | None = None,
) -> list[str]:
    if not shell_pid.isdigit():
        return []
    if process_snapshot is None:
        return []

    children_by_parent, command_by_pid = process_snapshot
    codex_pids: list[str] = []
    stack = list(children_by_parent.get(shell_pid, ()))
    seen: set[str] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if command_by_pid.get(pid, "") == "codex":
            codex_pids.append(pid)
        stack.extend(children_by_parent.get(pid, ()))
    return codex_pids


def threads_from_process_tree(
    shell_pid: str,
    process_snapshot: tuple[dict[str, tuple[str, ...]], dict[str, str]] | None,
    thread_from_codex_pid,
) -> list[str]:
    if process_snapshot is None:
        return []
    threads: list[str] = []
    for pid in codex_pids_in_process_tree(shell_pid, process_snapshot):
        thread_id = thread_from_codex_pid(pid)
        if thread_id:
            threads.append(thread_id)
            break
    return threads


def codex_top_level_descendants(
    shell_pid: str,
    process_graph: tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str]] | None,
) -> list[str]:
    if not shell_pid.isdigit() or process_graph is None:
        return []
    children_by_parent, command_by_pid, parent_by_pid = process_graph
    codex_pids = codex_pids_in_process_tree(shell_pid, (children_by_parent, command_by_pid))
    top_level: set[str] = set()
    for codex_pid in codex_pids:
        candidate = codex_pid
        while True:
            parent = parent_by_pid.get(candidate, "")
            if not parent or parent == shell_pid:
                break
            candidate = parent
        top_level.add(candidate)
    return sorted(top_level)


def pane_thread_probe(
    pane_id: str,
    shell_pid: str,
    process_snapshot: tuple[dict[str, tuple[str, ...]], dict[str, str]] | None,
    thread_from_codex_pid,
) -> tuple[str, str, str, bool]:
    if not shell_pid.isdigit():
        return "", "", "", False

    tids = threads_from_process_tree(shell_pid, process_snapshot, thread_from_codex_pid)
    if tids:
        return tids[0].lower(), "pid", "", True

    codex_pids = codex_pids_in_process_tree(shell_pid, process_snapshot)
    if not codex_pids:
        return "", "", "", False
    return "", "", f"could not determine thread_id for pane={pane_id} from the live Codex process", True


def thread_provenance_text(source: str) -> str:
    if not source:
        return ""
    if source == "pid":
        return "live Codex process-owned evidence"
    return source.replace("_", " ")


def build_thread_pane_map(run_tmux, check_output, thread_from_codex_pid) -> dict[str, str]:
    listing = run_tmux("list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}")
    mapping: dict[str, str] = {}
    process_snapshot = load_process_snapshot(check_output)
    if listing:
        for line in listing.splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 2:
                continue
            pane_id, shell_pid = parts[0], parts[1]
            if not pane_id or not shell_pid.isdigit():
                continue
            tids = threads_from_process_tree(shell_pid, process_snapshot, thread_from_codex_pid)
            for thread_id in tids:
                mapping[thread_id] = pane_id
    return mapping


def format_age(epoch: float, *, compact: bool = False, now: float | None = None) -> str:
    delta = (time.time() if now is None else now) - epoch
    if delta < 0:
        delta = 0
    suffix = "" if compact else " ago"
    if delta < 60:
        return f"{int(delta)}s{suffix}"
    if delta < 3600:
        return f"{int(delta // 60)}m{suffix}"
    if delta < 86400:
        hours = int(delta // 3600)
        minutes = int((delta % 3600) // 60)
        return f"{hours}h{minutes:02d}m{suffix}"
    days = int(delta // 86400)
    hours = int((delta % 86400) // 3600)
    return f"{days}d{hours}h{suffix}"


def thread_times(thread_id: str, *, compact: bool, thread_times_from_state_db, format_age_fn) -> tuple[str, str]:
    started_at, last_activity_at = thread_times_from_state_db(thread_id)
    started = format_age_fn(started_at, compact=compact) if started_at else "-"
    last_msg = format_age_fn(last_activity_at, compact=compact) if last_activity_at else "-"
    return started, last_msg


def thread_title(thread_id: str, *, state_dir: str) -> str:
    if not is_thread_id(thread_id):
        return ""
    root = Path(state_dir)
    if not root.is_dir():
        return ""
    for db_path in sorted(root.glob("state_*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            row = conn.execute(
                "select title, first_user_message from threads where id = ?",
                (thread_id,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
        if not row:
            continue
        for value in row:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def clean_pane_line_candidate(line: str) -> tuple[str, bool]:
    raw = line.rstrip()
    stripped = raw.strip()
    if not stripped or _PANE_CHROME_ONLY_RE.fullmatch(stripped):
        return "", False

    lowered = stripped.lower()
    if stripped.startswith("› "):
        return "", False
    if "working (" in lowered and "esc to interrupt" in lowered:
        return "", False
    if stripped.startswith(("Tip:", "model:", "directory:", "Press enter to continue")):
        return "", False
    if stripped.startswith(("1. Yes", "2. No", "> You are in ")):
        return "", False
    if "do you trust the contents of this directory?" in lowered:
        return "", False
    if "? for shortcuts" in stripped:
        return "", False
    if "context left" in stripped and "gpt-" in stripped:
        return "", False
    if _SHELL_PROMPT_RE.match(stripped):
        return "", False

    preferred = False
    candidate = stripped
    if candidate.startswith("• "):
        candidate = candidate[2:].strip()
        preferred = True
    elif candidate.startswith("◦ "):
        candidate = candidate[2:].strip()
    elif candidate.startswith("└ "):
        candidate = candidate[2:].strip()
    elif candidate.startswith("│ "):
        candidate = candidate[2:].strip()

    if not candidate or candidate.startswith("OpenAI Codex"):
        return "", False
    return candidate, preferred


def extract_last_agent_snippet(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    cutoff = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].lstrip().startswith("› "):
            cutoff = idx
            break
    search = lines[:cutoff] if cutoff < len(lines) else lines
    fallback = ""
    for line in reversed(search):
        candidate, preferred = clean_pane_line_candidate(line)
        if not candidate:
            continue
        if preferred:
            return candidate
        if not fallback:
            fallback = candidate
    return fallback


def last_agent_snippet_for_pane(pane: str, thread_id: str, run_tmux, state_dir: str) -> str:
    if pane:
        text = run_tmux("capture-pane", "-t", pane, "-p", "-S", "-40") or ""
        snippet = extract_last_agent_snippet(text)
        if snippet:
            return snippet
    return thread_title(thread_id, state_dir=state_dir)


def paused_activity_from_pane_text(text: str) -> str:
    if not text:
        return ""
    prompt_seen = False
    for raw in reversed(text.splitlines()):
        stripped = raw.strip()
        if not stripped or _PANE_CHROME_ONLY_RE.fullmatch(stripped):
            continue

        if _WORKING_STATUS_RE.match(stripped) or "esc to interrupt" in stripped.lower():
            return "busy"

        lowered = stripped.lower()
        if stripped.startswith(("Tip:", "model:", "directory:", "Press enter to continue")):
            continue
        if stripped.startswith(("1. Yes", "2. No", "> You are in ", "Running scope as unit:")):
            continue
        if "do you trust the contents of this directory?" in lowered:
            continue
        if "? for shortcuts" in stripped:
            continue
        if stripped.startswith("OpenAI Codex"):
            continue
        if "gpt-" in lowered and "left" in lowered and "·" in stripped:
            continue
        if stripped.startswith("›"):
            prompt_seen = True
            continue
        if _SHELL_PROMPT_RE.match(stripped):
            return ""
        if prompt_seen:
            return "waiting"
        return "busy"
    if prompt_seen:
        return "waiting"
    return ""


def paused_activity_for_pane(pane: str, run_tmux) -> str:
    if not pane:
        return ""
    text = run_tmux("capture-pane", "-t", pane, "-p", "-S", "-40") or ""
    return paused_activity_from_pane_text(text)
