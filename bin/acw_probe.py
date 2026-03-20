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
_SOFT_WORKING_STATUS_RE = re.compile(
    r"^(?:[•◦]\s*)?working(?:\.\.\.)?\b",
    re.IGNORECASE,
)
_HARD_WORKING_STATUS_RE = re.compile(
    r"^(?:[•◦]\s*)?(?:messages to be submitted after next tool call|waiting for background terminal)\b",
    re.IGNORECASE,
)
_INPUT_REQUIRED_PROMPT_RE = re.compile(
    r"(?:do you trust the contents of this directory\?|tool call needs your approval|"
    r"do you want to approve network access to |enable full access\?|reviewing approval request)",
    re.IGNORECASE,
)
_INPUT_REQUIRED_CHOICE_RE = re.compile(
    r"^(?:[› ]\s*)?(?:\d+\.\s*)?(?:"
    r"yes,\s*(?:continue|allow|continue anyway)|"
    r"no,\s*(?:quit|continue without permissions)|"
    r"yes,\s*and don't ask again|"
    r"apply full access for this session|"
    r"go back without enabling full access"
    r")\b",
    re.IGNORECASE,
)
_INPUT_REQUIRED_HELP_RE = re.compile(
    r"^(?:press enter to continue|ctrl \+ p / ctrl \+ n change field)\b",
    re.IGNORECASE,
)
_ASKING_CHOICE_RE = re.compile(
    r"^(?:[› ]\s*)?\d+\.\s*(?:yes|no)\b",
    re.IGNORECASE,
)
_ASKING_HELP_RE = re.compile(
    r"^press enter to confirm(?: or esc to go back)?\b",
    re.IGNORECASE,
)
_ASKING_QUESTION_RE = re.compile(r".+\?$")
_GIT_STATUS_BRANCH_RE = re.compile(
    r"^##\s+(?:no commits yet on\s+)?(?P<branch>.+?)(?:\.\.\.| \[|$)",
    re.IGNORECASE,
)
_GIT_SWITCHED_BRANCH_RE = re.compile(r"^Switched to branch '([^']+)'$")
_GIT_TRACKING_BRANCH_RE = re.compile(r"^branch '([^']+)' set up to track\b")
_GIT_HEAD_DECORATION_RE = re.compile(r"\(HEAD -> ([^),]+)")
_GIT_WORKTREE_BRANCH_RE = re.compile(
    r"(?:(?:~|/)\S+)\s+[0-9a-f]{7,40}\s+\[(?P<branch>[^\]]+)\]"
)
_WORKDIR_LINE_RE = re.compile(
    r"\b(?:continued|working)\s+in\s+(?P<path>(?:~|/)[^\s\"'`()\[\]{}<>|]+)",
    re.IGNORECASE,
)
_WORKTREE_FIELD_RE = re.compile(
    r"^(?:-\s*)?worktree:\s*(?P<path>(?:~|/)\S+)$",
    re.IGNORECASE,
)
_GIT_C_PATH_RE = re.compile(r"\bgit\s+-C\s+(?P<path>(?:~|/)[^\s\"'`()\[\]{}<>|]+)")
_PANE_PATH_TOKEN_RE = re.compile(r"(?P<path>(?:~|/)[^\s\"'`()\[\]{}<>|]+)")
_PROMPT_PATH_RE = re.compile(r"(?:directory:\s*|·\s*)(?P<path>(?:~|/)[^·]+?)\s*$", re.IGNORECASE)


def _normalize_pane_state_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    line = re.sub(r"^[│└┌┐┘┙┚┛┕┖┗┓┏]+\s*", "", line)
    line = re.sub(r"\s*[│┘┙┚┛┐┑┒┓]+$", "", line)
    return line.strip()


def _normalize_path_token(token: str, home_dir: str) -> str:
    cleaned = token.strip().rstrip(".,:;!?)]}»…")
    if cleaned == "~":
        return home_dir
    if cleaned.startswith("~/"):
        return str(Path(home_dir) / cleaned[2:])
    return cleaned


def _repo_path_is_build_only(candidate: str, repo_root: str) -> bool:
    try:
        candidate_path = Path(candidate).resolve(strict=False)
        repo_root_path = Path(repo_root).resolve(strict=False)
        relative = candidate_path.relative_to(repo_root_path)
    except ValueError:
        return False
    if not relative.parts:
        return False
    return relative.parts[0].startswith("build")


def _prompt_repo_root_from_line(
    line: str,
    *,
    resolve_repo_root_fn,
    home_dir: str,
) -> str:
    match = _PROMPT_PATH_RE.search(line)
    if not match:
        return ""
    candidate = _normalize_path_token(match.group("path"), home_dir)
    return resolve_repo_root_fn(candidate)


def _explicit_repo_root_from_line(
    line: str,
    *,
    resolve_repo_root_fn,
    home_dir: str,
) -> str:
    if line.lstrip().startswith("› "):
        return ""
    for match in reversed(list(_PANE_PATH_TOKEN_RE.finditer(line))):
        candidate = _normalize_path_token(match.group("path"), home_dir)
        resolved = resolve_repo_root_fn(candidate)
        if resolved and not _repo_path_is_build_only(candidate, resolved):
            return resolved
    return ""


def _explicit_branch_from_line(line: str) -> str:
    normalized = line.strip()
    for prefix in ("• ", "◦ ", "└ ", "│ "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
            break

    status_match = _GIT_STATUS_BRANCH_RE.match(normalized)
    if status_match:
        branch = status_match.group("branch").strip()
        if branch and not branch.lower().startswith("head "):
            return branch

    for regex in (
        _GIT_SWITCHED_BRANCH_RE,
        _GIT_TRACKING_BRANCH_RE,
        _GIT_HEAD_DECORATION_RE,
        _GIT_WORKTREE_BRANCH_RE,
    ):
        match = regex.search(normalized)
        if match:
            branch = match.group(1).strip() if match.lastindex else ""
            if branch:
                return branch
    return ""


def _explicit_workdir_root_from_line(
    line: str,
    *,
    resolve_repo_root_fn,
    home_dir: str,
) -> str:
    normalized = line.strip()
    for prefix in ("• ", "◦ ", "└ ", "│ "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
            break

    for regex in (_WORKDIR_LINE_RE, _WORKTREE_FIELD_RE):
        match = regex.search(normalized)
        if not match:
            continue
        candidate = _normalize_path_token(match.group("path"), home_dir)
        resolved = resolve_repo_root_fn(candidate)
        if resolved and not _repo_path_is_build_only(candidate, resolved):
            return resolved
    return ""


def _git_c_repo_root_from_line(
    line: str,
    *,
    resolve_repo_root_fn,
    home_dir: str,
) -> str:
    normalized = line.strip()
    for prefix in ("• ", "◦ ", "└ ", "│ "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
            break

    match = _GIT_C_PATH_RE.search(normalized)
    if not match:
        return ""
    candidate = _normalize_path_token(match.group("path"), home_dir)
    resolved = resolve_repo_root_fn(candidate)
    if resolved and not _repo_path_is_build_only(candidate, resolved):
        return resolved
    return ""


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
    if _INPUT_REQUIRED_PROMPT_RE.search(stripped):
        return "", False
    if _INPUT_REQUIRED_CHOICE_RE.match(stripped):
        return "", False
    if _INPUT_REQUIRED_HELP_RE.match(stripped):
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
    shell_prompt_seen = False
    input_prompt_seen = False
    asking_choice_seen = False
    asking_help_seen = False
    for raw in reversed(text.splitlines()):
        stripped = _normalize_pane_state_line(raw)
        if not stripped or _PANE_CHROME_ONLY_RE.fullmatch(stripped):
            continue

        if _INPUT_REQUIRED_PROMPT_RE.search(stripped):
            if not prompt_seen:
                return "input"
            continue
        if _INPUT_REQUIRED_CHOICE_RE.match(stripped) or _INPUT_REQUIRED_HELP_RE.match(stripped):
            if not prompt_seen:
                input_prompt_seen = True
            continue
        if _ASKING_HELP_RE.match(stripped):
            if not prompt_seen:
                asking_help_seen = True
            continue
        if _ASKING_CHOICE_RE.match(stripped):
            if not prompt_seen:
                asking_choice_seen = True
            continue
        if (asking_help_seen or asking_choice_seen) and _ASKING_QUESTION_RE.match(stripped) and not stripped.startswith("›"):
            return "asking"
        if _HARD_WORKING_STATUS_RE.match(stripped) or "esc to interrupt" in stripped.lower():
            return "busy"

        lowered = stripped.lower()
        if stripped.startswith(("Tip:", "model:", "directory:")):
            continue
        if stripped.startswith(">_ OpenAI Codex"):
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
            shell_prompt_seen = True
            continue
        if _SOFT_WORKING_STATUS_RE.match(stripped):
            if not prompt_seen:
                return "busy"
            continue
        if not prompt_seen:
            return "busy"
    if input_prompt_seen:
        return "input"
    if asking_help_seen and asking_choice_seen:
        return "asking"
    if prompt_seen:
        return "waiting"
    if shell_prompt_seen:
        return ""
    return ""


def paused_activity_for_pane(pane: str, run_tmux) -> str:
    if not pane:
        return ""
    text = run_tmux("capture-pane", "-t", pane, "-p", "-S", "-40") or ""
    return paused_activity_from_pane_text(text)


def repo_root_hint_from_pane_text(
    text: str,
    *,
    resolve_repo_root_fn,
    home_dir: str,
) -> str:
    if not text:
        return ""
    prompt_root = ""
    for raw in reversed(text.splitlines()):
        line = _normalize_pane_state_line(raw)
        if not line or _PANE_CHROME_ONLY_RE.fullmatch(line):
            continue
        if not prompt_root:
            prompt_root = _prompt_repo_root_from_line(
                line,
                resolve_repo_root_fn=resolve_repo_root_fn,
                home_dir=home_dir,
            )
            if prompt_root:
                continue
        repo_root = _explicit_repo_root_from_line(
            line,
            resolve_repo_root_fn=resolve_repo_root_fn,
            home_dir=home_dir,
        )
        if repo_root:
            return repo_root
    return prompt_root


def branch_hint_from_pane_text(
    text: str,
    *,
    resolve_repo_root_fn,
    branch_for_path_fn,
    home_dir: str,
    preferred_repo_root: str = "",
) -> str:
    if not text:
        return ""
    normalized_lines: list[str] = []
    for raw in reversed(text.splitlines()):
        line = _normalize_pane_state_line(raw)
        if not line or _PANE_CHROME_ONLY_RE.fullmatch(line):
            continue
        normalized_lines.append(line)

    for line in normalized_lines:
        branch = _explicit_branch_from_line(line)
        if branch:
            return branch

    fallback_root = ""
    for line in normalized_lines:
        repo_root = _explicit_repo_root_from_line(
            line,
            resolve_repo_root_fn=resolve_repo_root_fn,
            home_dir=home_dir,
        )
        if not repo_root:
            continue
        if preferred_repo_root and repo_root != preferred_repo_root:
            branch = branch_for_path_fn(repo_root)
            if branch and branch != "-":
                return branch
        if not fallback_root:
            fallback_root = repo_root
    if not fallback_root:
        return ""
    branch = branch_for_path_fn(fallback_root)
    return branch if branch and branch != "-" else ""


def workdir_hint_from_pane_text(
    text: str,
    *,
    resolve_repo_root_fn,
    home_dir: str,
) -> str:
    if not text:
        return ""
    normalized_lines: list[str] = []
    for raw in reversed(text.splitlines()):
        line = _normalize_pane_state_line(raw)
        if not line or _PANE_CHROME_ONLY_RE.fullmatch(line):
            continue
        normalized_lines.append(line)

    for line in normalized_lines:
        explicit_root = _explicit_workdir_root_from_line(
            line,
            resolve_repo_root_fn=resolve_repo_root_fn,
            home_dir=home_dir,
        )
        if explicit_root:
            return explicit_root

    for line in normalized_lines:
        git_c_root = _git_c_repo_root_from_line(
            line,
            resolve_repo_root_fn=resolve_repo_root_fn,
            home_dir=home_dir,
        )
        if git_c_root:
            return git_c_root

    for line in normalized_lines:
        repo_root = _explicit_repo_root_from_line(
            line,
            resolve_repo_root_fn=resolve_repo_root_fn,
            home_dir=home_dir,
        )
        if repo_root:
            return repo_root
    return ""


def repo_root_hint_for_pane(
    pane: str,
    run_tmux,
    *,
    resolve_repo_root_fn,
    home_dir: str,
) -> str:
    if not pane:
        return ""
    text = run_tmux("capture-pane", "-t", pane, "-p", "-S", "-80") or ""
    return repo_root_hint_from_pane_text(
        text,
        resolve_repo_root_fn=resolve_repo_root_fn,
        home_dir=home_dir,
    )
