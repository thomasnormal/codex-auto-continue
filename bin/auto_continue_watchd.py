#!/usr/bin/env python3
"""Process manager for auto-continue watchers.

Drop-in replacement for auto_continue_watchd.sh.  Stdlib-only (no pip deps).
"""

from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants & path resolution
# ---------------------------------------------------------------------------

THREAD_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SCRIPT = SCRIPT_DIR / "auto_continue_logwatch.py"


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


PROJECT_CWD = resolve_project_cwd()
# State dir is always ~/.codex/ regardless of CWD.  Pane IDs are tmux-global,
# so per-project state dirs cause fragmentation: the same pane gets different
# message/pid/state files depending on which directory `acw` was invoked from.
STATE_DIR = os.path.join(os.path.expanduser("~"), ".codex")
SEND_DELAY_SECS = os.environ.get("AUTO_CONTINUE_SEND_DELAY_SECS", "0.25")
ENTER_DELAY_SECS = os.environ.get("AUTO_CONTINUE_ENTER_DELAY_SECS", "0.15")

DEFAULT_MSG_FILE = os.path.join(STATE_DIR, "auto_continue.message.txt")
if not os.path.isfile(DEFAULT_MSG_FILE):
    DEFAULT_MSG_FILE = str(REPO_ROOT / "examples" / "messages" / "default_continue_message.txt")

LEGACY_PID_FILE = os.path.join(STATE_DIR, "auto_continue_logwatch.pid")
LEGACY_RUN_LOG = os.path.join(STATE_DIR, "auto_continue_logwatch.runner.log")

os.makedirs(STATE_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sanitize_key(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)


def key_from_pane(pane: str) -> str:
    return sanitize_key(pane)


def key_to_pane(key: str) -> str:
    """Inverse of key_from_pane: '_3' → '%3'."""
    return "%" + key.lstrip("_")


def is_pane_id(s: str) -> bool:
    return bool(re.fullmatch(r"%[0-9]+", s))


def _truncate(s: str, width: int) -> str:
    """Truncate string to *width* chars, adding '...' suffix if needed."""
    return s if len(s) <= width else s[: width - 3] + "..."


def _read_pid_file(path: str) -> str | None:
    """Read a PID file and return the pid string, or None if missing/invalid."""
    try:
        with open(path) as f:
            pid_str = f.read().strip()
    except OSError:
        return None
    return pid_str if pid_str.isdigit() else None


def is_thread_id(s: str) -> bool:
    return bool(THREAD_ID_RE.fullmatch(s))


def pid_file_for_key(key: str) -> str:
    return os.path.join(STATE_DIR, f"auto_continue_logwatch.{key}.pid")


def run_log_for_key(key: str) -> str:
    return os.path.join(STATE_DIR, f"auto_continue_logwatch.{key}.runner.log")


def watch_log_for_key(key: str) -> str:
    return os.path.join(STATE_DIR, f"auto_continue_logwatch.{key}.log")


def state_file_for_key(key: str) -> str:
    return os.path.join(STATE_DIR, f"auto_continue_logwatch.{key}.state.local.json")


def message_meta_file_for_key(key: str) -> str:
    return os.path.join(STATE_DIR, f"auto_continue_logwatch.{key}.message.local.txt")


# ---------------------------------------------------------------------------
# Tmux helpers
# ---------------------------------------------------------------------------


def run_tmux(*args: str) -> str | None:
    """Run a tmux command, trying socket env then fallback without TMUX env."""
    cmd: list[str] = ["tmux"]
    sock = os.environ.get("AUTO_CONTINUE_TMUX_SOCKET", "")
    if sock:
        cmd += ["-S", sock]
    cmd += list(args)
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    if not sock and os.environ.get("TMUX", ""):
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        try:
            return subprocess.check_output(
                ["tmux"] + list(args), stderr=subprocess.DEVNULL, text=True, env=env
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return None


def resolve_pane_from_window_target(target: str) -> str | None:
    """Resolve a window index or session:window to a pane id."""
    m = re.fullmatch(r"(\d+)", target)
    if m:
        requested_session = ""
        requested_window = m.group(1)
    else:
        m = re.fullmatch(r"([^:]+):(\d+)", target)
        if m:
            requested_session = m.group(1)
            requested_window = m.group(2)
        else:
            return None

    fmt = "#{session_name}\t#{window_index}\t#{pane_id}\t#{pane_active}\t#{window_active}\t#{pane_index}"
    listing = run_tmux("list-panes", "-a", "-F", fmt)
    if listing is None:
        print(
            f"error: tmux server is unavailable; cannot resolve window target '{target}'",
            file=sys.stderr,
        )
        print(
            "hint: start/reconnect tmux, or use a pane id if you already know it (for example '%6')",
            file=sys.stderr,
        )
        return None

    rows: list[dict[str, str]] = []
    session_seen: set[str] = set()
    for line in listing.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        sess, widx, pid_, pactive, wactive, pidx = parts[:6]
        if not pid_ or widx != requested_window:
            continue
        if requested_session and sess != requested_session:
            continue
        rows.append(
            dict(
                session=sess,
                window_index=widx,
                pane_id=pid_,
                pane_active=pactive,
                window_active=wactive,
                pane_index=pidx,
            )
        )
        session_seen.add(sess)

    if not rows:
        return None

    if not requested_session and len(session_seen) > 1:
        active_session = ""
        active_conflict = False
        for r in rows:
            if r["window_active"] != "1":
                continue
            if not active_session:
                active_session = r["session"]
            elif active_session != r["session"]:
                active_conflict = True
                break

        if active_session and not active_conflict:
            requested_session = active_session
        else:
            csv = ",".join(sorted(session_seen))
            print(
                f"error: window '{requested_window}' is ambiguous across tmux sessions: {csv}",
                file=sys.stderr,
            )
            print(
                f"hint: use 'session:window' (for example '0:{requested_window}') or a pane id (for example '%6')",
                file=sys.stderr,
            )
            return None

    selected = ""
    fallback = ""
    for r in rows:
        if requested_session and r["session"] != requested_session:
            continue
        if not fallback:
            fallback = r["pane_id"]
        if r["pane_active"] == "1":
            selected = r["pane_id"]
            break

    if not selected:
        selected = fallback

    return selected if is_pane_id(selected) else None


def resolve_pane_target(target: str) -> str:
    """Resolve any pane/window target to a tmux pane id (%N)."""
    if is_pane_id(target):
        return target

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target(target)
        if pane and is_pane_id(pane):
            return pane
        print(f"error: could not resolve window target '{target}'", file=sys.stderr)
        sys.exit(1)

    print(f"error: invalid pane/window target '{target}'", file=sys.stderr)
    print(
        "hint: use a pane id (for example '%6'), a window index (for example '2'), or 'session:window' (for example '0:2')",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Thread ID detection
# ---------------------------------------------------------------------------


def extract_resume_thread_id(args: str) -> str | None:
    m = re.search(
        r"\s" r"resume" r"\s" r"(" + THREAD_ID_RE.pattern + r")(?:$|\s)",
        args,
    )
    return m.group(1).lower() if m else None


def thread_id_from_snapshot_file(path: str) -> str | None:
    base = os.path.basename(path)
    m = re.fullmatch(r"(" + THREAD_ID_RE.pattern + r")\.sh", base)
    return m.group(1).lower() if m else None


def detect_thread_from_shell_snapshot(pane: str) -> str | None:
    snap_dir = os.path.join(os.path.expanduser("~"), ".codex", "shell_snapshots")
    if not os.path.isdir(snap_dir):
        return None
    files = sorted(glob.glob(os.path.join(snap_dir, "*.sh")), key=os.path.getmtime, reverse=True)
    if not files:
        return None
    needle = f'declare -x TMUX_PANE="{pane}"'
    for f in files:
        try:
            with open(f) as fh:
                if needle in fh.read():
                    tid = thread_id_from_snapshot_file(f)
                    if tid and is_thread_id(tid):
                        return tid
        except OSError:
            continue
    return None


def closest_thread_id_for_start_epoch(target_epoch: int) -> tuple[str, int] | None:
    pattern = os.path.join(os.path.expanduser("~"), ".codex", "sessions", "*", "*", "*", "rollout-*.jsonl")
    files = glob.glob(pattern)
    if not files:
        return None
    rollout_re = re.compile(
        r"^rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})-(" + THREAD_ID_RE.pattern + r")\.jsonl$"
    )
    best_tid = ""
    best_diff = -1
    for f in files:
        base = os.path.basename(f)
        m = rollout_re.fullmatch(base)
        if not m:
            continue
        ts_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
        tid = m.group(7).lower()
        try:
            candidate_epoch = int(
                subprocess.check_output(
                    ["date", "-d", ts_str, "+%s"], stderr=subprocess.DEVNULL, text=True
                ).strip()
            )
        except (subprocess.CalledProcessError, ValueError):
            continue
        diff = abs(candidate_epoch - target_epoch)
        if best_diff < 0 or diff < best_diff:
            best_diff = diff
            best_tid = tid
    if is_thread_id(best_tid):
        return best_tid, best_diff
    return None


def detect_thread_from_pane_tty(pane: str) -> str | None:
    pane_tty = run_tmux("display-message", "-p", "-t", pane, "#{pane_tty}")
    if not pane_tty:
        return None
    pane_tty = pane_tty.strip()
    if not pane_tty:
        return None
    tty_for_ps = pane_tty.removeprefix("/dev/")
    if not tty_for_ps:
        return None

    # Fast path: thread id in `codex ... resume <thread-id>`.
    try:
        ps_out = subprocess.check_output(
            ["ps", "-t", tty_for_ps, "-o", "pid=,args="],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        ps_out = ""

    for line in ps_out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_str, args = parts
        if not pid_str.isdigit() or "codex" not in args:
            continue
        tid = extract_resume_thread_id(args)
        if tid and is_thread_id(tid):
            return tid

    # Fallback: match codex process start time to closest rollout session.
    best_tid = ""
    best_diff = -1
    for line in ps_out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_str, args = parts
        if not pid_str.isdigit() or "codex" not in args:
            continue
        try:
            lstart = subprocess.check_output(
                ["ps", "-p", pid_str, "-o", "lstart="],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        if not lstart:
            continue
        try:
            start_epoch = int(
                subprocess.check_output(
                    ["date", "-d", lstart, "+%s"], stderr=subprocess.DEVNULL, text=True
                ).strip()
            )
        except (subprocess.CalledProcessError, ValueError):
            continue
        result = closest_thread_id_for_start_epoch(start_epoch)
        if not result:
            continue
        cand_tid, cand_diff = result
        if best_diff < 0 or cand_diff < best_diff:
            best_diff = cand_diff
            best_tid = cand_tid

    if is_thread_id(best_tid) and best_diff <= 600:
        return best_tid
    return None


def detect_thread_from_proc_fd(pane: str) -> str | None:
    """Inspect /proc/PID/fd to find which rollout file the pane's codex has open."""
    rc = subprocess.run(
        ["tmux", "list-panes", "-t", pane, "-F", "#{pane_pid}"],
        capture_output=True, text=True, check=False,
    )
    if rc.returncode != 0 or not rc.stdout.strip():
        return None
    shell_pid = rc.stdout.strip()

    try:
        result = subprocess.run(
            ["pstree", "-p", shell_pid],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return None
    except FileNotFoundError:
        return None

    pids = re.findall(r"\((\d+)\)", result.stdout)
    rollout_re = re.compile(
        r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
        r"(" + THREAD_ID_RE.pattern + r")\.jsonl$"
    )

    for pid in pids:
        try:
            exe = Path(f"/proc/{pid}/exe").resolve().name
        except OSError:
            continue
        if exe != "codex":
            continue
        fd_dir = Path(f"/proc/{pid}/fd")
        try:
            for fd in fd_dir.iterdir():
                try:
                    target = fd.resolve()
                except OSError:
                    continue
                m = rollout_re.search(target.name)
                if m:
                    return m.group(1).lower()
        except OSError:
            continue
    return None


def detect_thread_id_for_pane(pane: str) -> str | None:
    # Most reliable: check which rollout file the codex process has open.
    tid = detect_thread_from_proc_fd(pane)
    if tid and is_thread_id(tid):
        return tid
    tid = detect_thread_from_shell_snapshot(pane)
    if tid and is_thread_id(tid):
        return tid
    tid = detect_thread_from_pane_tty(pane)
    if tid and is_thread_id(tid):
        return tid
    return None


def resolve_thread_id(pane: str, requested: str = "") -> str:
    if requested and requested != "auto":
        if is_thread_id(requested):
            return requested.lower()
        print(f"error: invalid thread_id '{requested}'", file=sys.stderr)
        sys.exit(1)

    tid = detect_thread_id_for_pane(pane)
    if tid and is_thread_id(tid):
        return tid

    if requested == "auto":
        return "auto"

    print(
        f"warn: could not auto-detect thread_id for pane={pane}; falling back to auto mode",
        file=sys.stderr,
    )
    return "auto"


# ---------------------------------------------------------------------------
# Message argument parsing
# ---------------------------------------------------------------------------


def parse_thread_and_message_args(
    args: list[str],
) -> tuple[str, str, str, bool]:
    """Return (thread_arg, message_mode, message_value, message_explicit)."""
    thread_arg = ""
    message_mode = "file"
    message_value = DEFAULT_MSG_FILE
    message_explicit = False

    rest = list(args)
    if rest and not rest[0].startswith("--"):
        thread_arg = rest.pop(0)

    while rest:
        tok = rest.pop(0)
        if tok == "--message":
            if not rest:
                print("error: --message requires a value", file=sys.stderr)
                sys.exit(1)
            message_mode = "inline"
            message_value = rest.pop(0)
            message_explicit = True
        elif tok == "--message-file":
            if not rest:
                print("error: --message-file requires a path", file=sys.stderr)
                sys.exit(1)
            message_mode = "file"
            message_value = rest.pop(0)
            message_explicit = True
        else:
            print(f"error: unknown option '{tok}'", file=sys.stderr)
            sys.exit(1)

    if message_mode == "file" and not os.path.isfile(message_value):
        print(f"error: message file not found: {message_value}", file=sys.stderr)
        sys.exit(1)

    return thread_arg, message_mode, message_value, message_explicit


# ---------------------------------------------------------------------------
# Message meta persistence
# ---------------------------------------------------------------------------


def write_message_meta(key: str, mode: str, value: str) -> None:
    path = message_meta_file_for_key(key)
    with open(path, "w") as f:
        f.write(f"mode={mode}\n")
        f.write(f"value={value}\n")


def read_message_meta_for_key(key: str) -> tuple[str, str] | None:
    path = message_meta_file_for_key(key)
    mode = ""
    value_lines: list[str] = []
    in_value = False
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("mode="):
                    mode = line[5:]
                    in_value = False
                elif line.startswith("value="):
                    value_lines = [line[6:]]
                    in_value = True
                elif in_value:
                    value_lines.append(line)
    except OSError:
        return None
    value = "\n".join(value_lines)
    # Strip the trailing newline that write_message_meta always adds.
    if value.endswith("\n"):
        value = value[:-1]
    if mode in ("inline", "file"):
        return mode, value
    return None


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


def _read_proc_cmdline(pid: str) -> list[str] | None:
    """Read a process's argv from /proc (null-delimited, no word-splitting issues)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read()
        if not data:
            return None
        args = data.rstrip(b"\0").split(b"\0")
        return [a.decode("utf-8", errors="replace") for a in args]
    except OSError:
        return None


def _parse_logwatch_args(tokens: list[str]) -> dict[str, str]:
    """Parse logwatch.py command-line tokens into an info dict."""
    info: dict[str, str] = {
        "pane": "", "thread": "", "state": "",
        "watch": "", "cwd": "", "msg_file": "", "msg_inline": "",
    }
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok == "--pane":
            info["pane"] = nxt
            i += 2
        elif tok == "--thread-id":
            info["thread"] = nxt
            i += 2
        elif tok == "--state-file":
            info["state"] = nxt
            i += 2
        elif tok == "--watch-log":
            info["watch"] = nxt
            i += 2
        elif tok == "--cwd":
            info["cwd"] = nxt
            i += 2
        elif tok == "--message-file":
            info["msg_file"] = nxt
            i += 2
        elif tok == "--message":
            info["msg_inline"] = nxt
            i += 2
        else:
            i += 1
    return info


def watcher_rows(pane_filter: str = "") -> list[dict[str, str]]:
    """Find running logwatch.py instances via ps."""
    try:
        ps_out = subprocess.check_output(
            ["ps", "-ww", "-eo", "pid=,args="], stderr=subprocess.DEVNULL, text=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    results: list[dict[str, str]] = []
    for line in ps_out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        pid_str, argstr = parts
        if not pid_str.isdigit():
            continue
        tokens = argstr.split()
        has_python = any(re.search(r"(^|/)python([0-9.]+)?$", t) for t in tokens)
        has_script = any(t.endswith("auto_continue_logwatch.py") for t in tokens)
        if not has_python or not has_script:
            continue

        # Use /proc/PID/cmdline for proper null-delimited args (avoids breaking
        # multi-word --message values that ps joins with spaces).
        proc_argv = _read_proc_cmdline(pid_str)
        if proc_argv:
            info = _parse_logwatch_args(proc_argv)
        else:
            # Fallback to ps-parsed tokens (non-Linux or /proc unavailable).
            info = _parse_logwatch_args(tokens)
        info["pid"] = pid_str

        if not info["pane"]:
            continue
        if pane_filter and info["pane"] != pane_filter:
            continue
        results.append(info)
    return results


def watcher_pids_for_pane(pane: str) -> list[str]:
    return [r["pid"] for r in watcher_rows(pane)]


def _is_pid_stopped(pid: int) -> bool:
    """Check if a process is in stopped (T) state via /proc."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    return "\tT " in line or "\tT\t" in line
    except OSError:
        pass
    return False


def is_running_pid_file(pid_file: str) -> bool:
    pid_str = _read_pid_file(pid_file)
    if not pid_str:
        return False
    try:
        os.kill(int(pid_str), 0)
        return True
    except OSError:
        return False


def thread_from_running_watcher_for_pane(pane: str) -> str | None:
    rows = watcher_rows(pane)
    if not rows:
        return None
    tid = rows[0]["thread"]
    if is_thread_id(tid):
        return tid.lower()
    return None


def thread_from_state_file_for_key(key: str) -> str | None:
    sf = state_file_for_key(key)
    if not os.path.isfile(sf):
        return None
    try:
        with open(sf) as f:
            data = json.load(f)
        tid = data.get("thread_id", "")
        if is_thread_id(tid):
            return tid.lower()
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return None


def collect_known_keys() -> set[str]:
    keys: set[str] = set()
    for r in watcher_rows():
        k = key_from_pane(r["pane"])
        if k:
            keys.add(k)
    for pat in (
        os.path.join(STATE_DIR, "auto_continue_logwatch.*.state.local.json"),
        os.path.join(STATE_DIR, "auto_continue_logwatch.*.pid"),
    ):
        for f in glob.glob(pat):
            base = os.path.basename(f)
            k = base.removeprefix("auto_continue_logwatch.")
            for suffix in (".state.local.json", ".pid"):
                if k.endswith(suffix):
                    k = k.removesuffix(suffix)
                    break
            if k and k != base:
                keys.add(k)
    return keys


# ---------------------------------------------------------------------------
# Logwatch command builder
# ---------------------------------------------------------------------------


def _edit_message_interactive(initial: str = "") -> str | None:
    """Open $EDITOR with *initial* text and return the edited content.

    Returns None if the editor exits with an error or the user leaves the
    content empty.  Strips a single trailing newline that editors tend to add.
    """
    editor = os.environ.get("EDITOR", "vim")
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="acw_message_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(initial)
        rc = subprocess.call([editor, tmp])
        if rc != 0:
            print("editor exited with error, message unchanged", file=sys.stderr)
            return None
        with open(tmp) as f:
            new_value = f.read()
        # Strip a single trailing newline that editors tend to add.
        if new_value.endswith("\n") and not initial.endswith("\n"):
            new_value = new_value[:-1]
        return new_value
    finally:
        Path(tmp).unlink(missing_ok=True)


def _logwatch_cmd(pane: str, thread_id: str, message_args: list[str], key: str) -> list[str]:
    return [
        "python3",
        str(SCRIPT),
        "--cwd", PROJECT_CWD,
        "--pane", pane,
        "--thread-id", thread_id,
        *message_args,
        "--cooldown-secs", "1.0",
        "--send-delay-secs", SEND_DELAY_SECS,
        "--enter-delay-secs", ENTER_DELAY_SECS,
        "--state-file", state_file_for_key(key),
        "--watch-log", watch_log_for_key(key),
    ]


def _message_args(mode: str, value: str) -> list[str]:
    if mode == "inline":
        return ["--message", value]
    return ["--message-file", value]


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_start(argv: list[str]) -> None:
    rest = list(argv)
    if rest and not rest[0].startswith("--"):
        target = rest.pop(0)
    else:
        target = ""
    if not target:
        print(
            "error: pane target is required\n"
            "usage: auto_continue_watchd.py start <pane-id|window-index|session:window> "
            "[thread-id|auto] [--message TEXT | --message-file FILE]",
            file=sys.stderr,
        )
        sys.exit(2)

    pane = resolve_pane_target(target)
    if pane != target:
        print(f"resolved: target={target} pane={pane}")

    thread_arg, msg_mode, msg_value, msg_explicit = parse_thread_and_message_args(rest)

    # No --message/--message-file given: open editor for interactive input.
    if not msg_explicit:
        value = _edit_message_interactive()
        if value is None or not value.strip():
            print("start: no message provided, aborting", file=sys.stderr)
            sys.exit(1)
        msg_mode = "inline"
        msg_value = value

    key = key_from_pane(pane)
    pf = pid_file_for_key(key)
    rl = run_log_for_key(key)

    if is_running_pid_file(pf):
        print(f"already running: pane={pane} pid={_read_pid_file(pf)}")
        return

    existing_pids = watcher_pids_for_pane(pane)
    if existing_pids:
        if len(existing_pids) == 1:
            with open(pf, "w") as f:
                f.write(existing_pids[0])
            print(f"already running: pane={pane} pid={existing_pids[0]}")
            return
        joined = " ".join(existing_pids)
        print(
            f"error: multiple watchers already running for pane={pane}: {joined}",
            file=sys.stderr,
        )
        print(
            f"hint: run 'auto_continue_watchd.py stop {pane}' to stop all pane watchers, then start once",
            file=sys.stderr,
        )
        sys.exit(1)

    thread_id = resolve_thread_id(pane, thread_arg)
    write_message_meta(key, msg_mode, msg_value)
    if (not thread_arg or thread_arg == "auto") and thread_id != "auto":
        print(f"resolved: pane={pane} thread_id={thread_id}")

    Path(pf).unlink(missing_ok=True)

    cmd = _logwatch_cmd(pane, thread_id, _message_args(msg_mode, msg_value), key)
    log_fh = open(rl, "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    log_fh.close()

    time.sleep(0.2)
    if proc.poll() is None:
        with open(pf, "w") as f:
            f.write(str(proc.pid))
        print(f"started: pid={proc.pid} pane={pane} thread_id={thread_id}")
        return

    Path(pf).unlink(missing_ok=True)
    print(
        f"failed: watcher exited immediately (pane={pane} thread_id={thread_id})",
        file=sys.stderr,
    )
    try:
        with open(rl) as f:
            lines = f.readlines()
        for line in lines[-40:]:
            print(line, end="")
    except OSError:
        pass
    sys.exit(1)


def stop_pid_file(pid_file: str) -> None:
    pid_str = _read_pid_file(pid_file)
    if pid_str is None:
        return
    pid = int(pid_str)
    try:
        if _is_pid_stopped(pid):
            os.kill(pid, signal.SIGCONT)
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    Path(pid_file).unlink(missing_ok=True)
    print(f"stopped: pid={pid_str}")


def stop_pane_watchers(pane: str) -> None:
    key = key_from_pane(pane)
    pf = pid_file_for_key(key)
    pane_pids = watcher_pids_for_pane(pane)
    if not pane_pids:
        Path(pf).unlink(missing_ok=True)
        print(f"not running: pane={pane}")
        return

    for pid_str in pane_pids:
        pid = int(pid_str)
        try:
            # Resume first so a SIGSTOP'd process can handle SIGTERM.
            if _is_pid_stopped(pid):
                os.kill(pid, signal.SIGCONT)
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        print(f"stopped: pane={pane} pid={pid_str}")
    Path(pf).unlink(missing_ok=True)


def cmd_stop(argv: list[str]) -> None:
    pane_arg = argv[0] if argv else ""

    if pane_arg:
        pane = resolve_pane_target(pane_arg)
        stop_pane_watchers(pane)
        return

    had_any = False
    for pf in glob.glob(os.path.join(STATE_DIR, "auto_continue_logwatch.*.pid")):
        had_any = True
        stop_pid_file(pf)

    if os.path.isfile(LEGACY_PID_FILE):
        had_any = True
        stop_pid_file(LEGACY_PID_FILE)

    for r in watcher_rows():
        pid_str = r["pid"]
        if not pid_str.isdigit():
            continue
        had_any = True
        try:
            os.kill(int(pid_str), signal.SIGTERM)
        except OSError:
            pass
        print(f"stopped: pid={pid_str}")

    if not had_any:
        print("not running")


def cmd_pause(argv: list[str]) -> None:
    """Pause watcher(s) by sending SIGSTOP."""
    pane_arg = argv[0] if argv else ""

    if pane_arg:
        pane = resolve_pane_target(pane_arg)
        pids = watcher_pids_for_pane(pane)
        if not pids:
            print(f"not running: pane={pane}")
            return
        for pid_str in pids:
            pid = int(pid_str)
            if _is_pid_stopped(pid):
                print(f"already paused: pane={pane} pid={pid_str}")
            else:
                try:
                    os.kill(pid, signal.SIGSTOP)
                    print(f"paused: pane={pane} pid={pid_str}")
                except OSError as e:
                    print(f"error: could not pause pid={pid_str}: {e}", file=sys.stderr)
        return

    had_any = False
    for r in watcher_rows():
        pid_str = r["pid"]
        if not pid_str.isdigit():
            continue
        had_any = True
        pid = int(pid_str)
        if _is_pid_stopped(pid):
            print(f"already paused: pane={r['pane']} pid={pid_str}")
        else:
            try:
                os.kill(pid, signal.SIGSTOP)
                print(f"paused: pane={r['pane']} pid={pid_str}")
            except OSError as e:
                print(f"error: could not pause pid={pid_str}: {e}", file=sys.stderr)

    if not had_any:
        print("not running")


def cmd_resume(argv: list[str]) -> None:
    """Resume paused watcher(s) by sending SIGCONT."""
    pane_arg = argv[0] if argv else ""

    if pane_arg:
        pane = resolve_pane_target(pane_arg)
        pids = watcher_pids_for_pane(pane)
        if not pids:
            print(f"not running: pane={pane}")
            return
        for pid_str in pids:
            pid = int(pid_str)
            if not _is_pid_stopped(pid):
                print(f"not paused: pane={pane} pid={pid_str}")
            else:
                try:
                    os.kill(pid, signal.SIGCONT)
                    print(f"resumed: pane={pane} pid={pid_str}")
                except OSError as e:
                    print(f"error: could not resume pid={pid_str}: {e}", file=sys.stderr)
        return

    had_any = False
    for r in watcher_rows():
        pid_str = r["pid"]
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if not _is_pid_stopped(pid):
            continue
        had_any = True
        try:
            os.kill(pid, signal.SIGCONT)
            print(f"resumed: pane={r['pane']} pid={pid_str}")
        except OSError as e:
            print(f"error: could not resume pid={pid_str}: {e}", file=sys.stderr)

    if not had_any:
        print("no paused watchers")


def cmd_restart(argv: list[str]) -> None:
    rest = list(argv)
    if rest and not rest[0].startswith("--"):
        target = rest.pop(0)
    else:
        target = ""
    if not target:
        print(
            "error: pane target is required\n"
            "usage: auto_continue_watchd.py restart <pane-id|window-index|session:window> "
            "[thread-id|auto] [--message TEXT | --message-file FILE]",
            file=sys.stderr,
        )
        sys.exit(2)

    pane = resolve_pane_target(target)
    if pane != target:
        print(f"resolved: target={target} pane={pane}")

    thread_arg, msg_mode, msg_value, msg_explicit = parse_thread_and_message_args(rest)

    key = key_from_pane(pane)

    if not msg_explicit:
        meta = read_message_meta_for_key(key)
        if meta:
            msg_mode, msg_value = meta

    if msg_mode == "file" and not os.path.isfile(msg_value):
        msg_value = DEFAULT_MSG_FILE

    if not thread_arg:
        # Re-detect from the pane's live codex process first (most reliable).
        thread_arg = detect_thread_id_for_pane(pane) or ""
        if not thread_arg:
            thread_arg = thread_from_running_watcher_for_pane(pane) or ""
        if not thread_arg:
            thread_arg = thread_from_state_file_for_key(key) or ""

    stop_pane_watchers(pane)

    start_args: list[str] = [pane]
    if thread_arg:
        start_args.append(thread_arg)
    if msg_mode == "inline":
        start_args += ["--message", msg_value]
    else:
        start_args += ["--message-file", msg_value]

    cmd_start(start_args)


def cmd_edit(argv: list[str]) -> None:
    if not argv or argv[0].startswith("--"):
        print(
            "error: pane target is required\n"
            "usage: auto_continue_watchd.py edit <pane-id|window-index|session:window>",
            file=sys.stderr,
        )
        sys.exit(2)

    target = argv[0]
    pane = resolve_pane_target(target)
    if pane != target:
        print(f"resolved: target={target} pane={pane}")

    key = key_from_pane(pane)

    # Read current message (meta file → running watcher args → default).
    mode = "inline"
    value = ""
    meta = read_message_meta_for_key(key)
    if meta:
        mode, value = meta
    if not value:
        rows = watcher_rows(pane)
        if rows:
            value = rows[0]["msg_inline"] or rows[0]["msg_file"]
            if rows[0]["msg_file"] and not rows[0]["msg_inline"]:
                mode = "file"

    # For file-mode messages, open the file, then restart if changed.
    if mode == "file" and value and os.path.isfile(value):
        try:
            old_mtime = os.path.getmtime(value)
        except OSError:
            old_mtime = None
        editor = os.environ.get("EDITOR", "vim")
        rc = subprocess.call([editor, value])
        if rc != 0:
            print("edit: editor exited with error, not restarting", file=sys.stderr)
            return
        try:
            new_mtime = os.path.getmtime(value)
        except OSError:
            new_mtime = None
        if old_mtime == new_mtime:
            print("edit: message file unchanged")
            return
        print(f"edit: message file updated")
        if watcher_rows(pane):
            cmd_restart([pane, "--message-file", value])
        return

    # For inline messages, open editor with current value.
    new_value = _edit_message_interactive(value)
    if new_value is None:
        return

    if new_value == value:
        print("edit: message unchanged")
        return

    write_message_meta(key, "inline", new_value)
    print(f"edit: message updated for pane {pane}")
    if watcher_rows(pane):
        cmd_restart([pane, "--message", new_value])


def cmd_restart_all(argv: list[str]) -> None:
    """Restart every known watcher (running or dead) using saved thread/message.

    Panes that no longer exist in tmux are stopped and cleaned up.
    """
    known = collect_known_keys()
    if not known:
        print("no known watchers to restart")
        return

    restarted = 0
    for key in sorted(known):
        pane = key_to_pane(key)
        if not is_pane_id(pane):
            print(f"skip: cannot recover pane id from key '{key}'")
            continue

        # Check that the tmux pane still exists.
        if run_tmux("display-message", "-p", "-t", pane, "#{pane_id}") is None:
            # Pane is gone — kill any orphaned watcher and clean up.
            pids = watcher_pids_for_pane(pane)
            if pids:
                for p in pids:
                    pid = int(p)
                    try:
                        if _is_pid_stopped(pid):
                            os.kill(pid, signal.SIGCONT)
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                print(f"killed: orphaned watcher(s) for pane {pane} (pane no longer exists)")
            Path(pid_file_for_key(key)).unlink(missing_ok=True)
            continue

        print(f"--- restarting {pane} ---")
        cmd_restart([pane])
        restarted += 1

    if restarted == 0:
        print("no live panes to restart")


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------


def _build_pane_window_map() -> dict[str, str]:
    listing = run_tmux("list-panes", "-a", "-F", "#{session_name}\t#{window_index}\t#{pane_id}")
    mapping: dict[str, str] = {}
    if listing:
        for line in listing.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2]:
                mapping[parts[2]] = f"{parts[0]}:{parts[1]}"
    return mapping


def _event_summary_from_watch_log(watch_path: str) -> str:
    if not watch_path:
        return "-"
    try:
        with open(watch_path, "rb") as f:
            # Read last line efficiently
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return "-"
            pos = max(0, size - 4096)
            f.seek(pos)
            chunk = f.read().decode("utf-8", errors="replace")
            lines = chunk.splitlines()
            last_line = ""
            for ln in reversed(lines):
                if ln.strip():
                    last_line = ln.strip()
                    break
    except OSError:
        return "-"
    if not last_line:
        return "-"

    # Strip timestamp prefix "...] "
    idx = last_line.find("] ")
    if idx >= 0:
        summary = last_line[idx + 2:]
    else:
        summary = last_line

    m = re.match(r"^continue: sent turn=(\S+)", summary)
    if m:
        return f"continue turn={m.group(1)}"
    if summary.startswith("watch: pane="):
        return "watch start"
    if summary.startswith("watch: auto-rebind"):
        return "watch rebind"
    if summary.startswith("error:"):
        return "error"
    if len(summary) > 56:
        summary = summary[:53] + "..."
    return summary


def _read_state_json(state_path: str) -> dict[str, str]:
    """Read health fields from state JSON file."""
    result: dict[str, str] = {}
    if not state_path or not os.path.isfile(state_path):
        return result
    try:
        with open(state_path) as f:
            data = json.load(f)
        for k in ("health", "health_detail", "health_ts", "last_continue_at"):
            if k in data:
                result[k] = str(data[k])
    except (OSError, json.JSONDecodeError):
        pass
    return result


def _resolve_message(r: dict[str, str]) -> tuple[str, str]:
    """Return (mode, value) from meta file or watcher row args.

    mode is 'file', 'inline', or '' if no message is found.
    """
    key = key_from_pane(r["pane"])
    meta = read_message_meta_for_key(key)
    if meta:
        return meta
    if r["msg_file"]:
        return "file", r["msg_file"]
    if r["msg_inline"]:
        return "inline", r["msg_inline"]
    return "", ""


def _compute_state(r: dict[str, str], sj: dict[str, str]) -> str:
    """Derive display state from process status and health JSON."""
    if r["pid"].isdigit() and _is_pid_stopped(int(r["pid"])):
        return "paused"
    h = sj.get("health", "")
    if h and h != "ok":
        return h
    return "running"


def _message_summary_for_row(r: dict[str, str]) -> str:
    mode, value = _resolve_message(r)
    if mode == "file" and value:
        return f"file:{os.path.basename(value)}"
    if mode == "inline" and value:
        return f"msg:{value}"
    return "-"


def _status_table(rows: list[dict[str, str]], pane_window: dict[str, str]) -> None:
    header = f"{'WINDOW':<7} {'PANE':<5} {'PID':<7} {'THREAD_ID':<13} {'STATE':<8} {'LAST_EVENT':<28} MESSAGE"
    sep = f"{'------':<7} {'-----':<5} {'-------':<7} {'-------------':<13} {'--------':<8} {'----------------------------':<28} -------"
    print(header)
    print(sep)

    for r in rows:
        thread_short = _truncate(r["thread"] or "unknown", 13)

        sj = _read_state_json(r["state"])
        state_value = _compute_state(r, sj)

        event_summary = _event_summary_from_watch_log(r["watch"])
        # Override event summary with health detail when degraded.
        if state_value not in ("running", "paused"):
            hd = sj.get("health_detail", "")
            if hd:
                event_summary = hd
        event_summary = _truncate(event_summary, 28)

        msg_summary = _truncate(_message_summary_for_row(r), 44)
        window_label = pane_window.get(r["pane"], "-")

        print(
            f"{window_label:<7} {r['pane']:<5} {r['pid']:<7} {thread_short:<13} "
            f"{state_value:<8} {event_summary:<28} {msg_summary}"
        )


def _status_details(rows: list[dict[str, str]], pane_window: dict[str, str]) -> None:
    total = len(rows)
    for idx, r in enumerate(rows, 1):
        if idx > 1:
            print()

        sj = _read_state_json(r["state"])
        state_value = _compute_state(r, sj)

        last_event = "-"
        if r["watch"]:
            try:
                with open(r["watch"]) as f:
                    lines = f.readlines()
                if lines:
                    last_event = lines[-1].rstrip("\n")
            except OSError:
                pass
            if not last_event:
                last_event = "-"

        mode, value = _resolve_message(r)
        if mode == "file" and value:
            msg_full = f"file: {value}"
        elif mode == "inline" and value:
            msg_full = value
        else:
            msg_full = "-"

        window_label = pane_window.get(r["pane"], "-")

        print(f"=== Watcher {idx}/{total} ===")
        print(f"  {'WINDOW:':<16} {window_label}")
        print(f"  {'PANE:':<16} {r['pane']}")
        print(f"  {'PID:':<16} {r['pid']}")
        print(f"  {'THREAD_ID:':<16} {r['thread'] or 'unknown'}")
        print(f"  {'STATE:':<16} {state_value}")
        hd = sj.get("health_detail", "")
        if hd:
            print(f"  {'HEALTH_DETAIL:':<16} {hd}")
        hts = sj.get("health_ts", "")
        if hts:
            print(f"  {'HEALTH_SINCE:':<16} {hts}")
        print(f"  {'LAST_EVENT:':<16} {last_event}")
        lc = sj.get("last_continue_at", "")
        if lc:
            print(f"  {'LAST_CONTINUE:':<16} {lc}")
        print(f"  {'MESSAGE:':<16} {msg_full}")
        if r["watch"]:
            print(f"  {'WATCH_LOG:':<16} {r['watch']}")
        if r["state"]:
            print(f"  {'STATE_FILE:':<16} {r['state']}")


def cmd_status(argv: list[str]) -> None:
    pane_arg = ""
    details = False
    for arg in argv:
        if arg == "--details":
            details = True
        else:
            pane_arg = arg

    pane_window = _build_pane_window_map()
    detail_tag = " (details)" if details else ""

    if pane_arg:
        target = pane_arg
        pane = resolve_pane_target(target)
        if pane != target:
            print(f"resolved: target={target} pane={pane}")
        rows = watcher_rows(pane)
        print(f"Active watchers{detail_tag} for pane {pane}: {len(rows)}")
    else:
        rows = watcher_rows()
        print(f"Active watchers{detail_tag}: {len(rows)}")

    if not rows:
        print("(none)")
        return

    if details:
        _status_details(rows, pane_window)
    else:
        _status_table(rows, pane_window)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_stale_files() -> None:
    """Remove pid/log/message files for watchers that are no longer running."""
    # PID files for dead processes.
    for pf in glob.glob(os.path.join(STATE_DIR, "auto_continue_logwatch.*.pid")):
        if not is_running_pid_file(pf):
            Path(pf).unlink(missing_ok=True)
    if not is_running_pid_file(LEGACY_PID_FILE):
        Path(LEGACY_PID_FILE).unlink(missing_ok=True)

    # Build the set of known keys once (single ps scan).
    known = collect_known_keys()

    # Log files for unknown keys.
    keep_logs: set[str] = set()
    for k in known:
        keep_logs.add(watch_log_for_key(k))
        keep_logs.add(run_log_for_key(k))
    for pat in (
        os.path.join(STATE_DIR, "auto_continue_logwatch*.log"),
        os.path.join(STATE_DIR, "auto_continue_logwatch*.runner.log"),
    ):
        for lf in glob.glob(pat):
            if lf not in keep_logs:
                Path(lf).unlink(missing_ok=True)

    # Message meta files for unknown keys.
    keep_meta: set[str] = set()
    for k in known:
        keep_meta.add(message_meta_file_for_key(k))
    for mf in glob.glob(os.path.join(STATE_DIR, "auto_continue_logwatch.*.message.local.txt")):
        if mf not in keep_meta:
            Path(mf).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        subcmd = "status"
        rest: list[str] = []
    else:
        subcmd = sys.argv[1]
        rest = sys.argv[2:]

    # Only clean up stale files on commands that mutate state.  Read-only
    # commands (status, pause, resume) skip the cleanup overhead (ps scan +
    # multiple globs).
    if subcmd in ("start", "stop", "restart", "restart-all", "edit"):
        cleanup_stale_files()

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "pause": cmd_pause,
        "pause-all": lambda _: cmd_pause([]),
        "resume": cmd_resume,
        "resume-all": lambda _: cmd_resume([]),
        "restart": cmd_restart,
        "restart-all": cmd_restart_all,
        "status": cmd_status,
        "edit": cmd_edit,
    }

    if subcmd in commands:
        commands[subcmd](rest)
    else:
        print(
            "usage: auto_continue_watchd.py {start|stop|pause|pause-all|resume|resume-all|restart|restart-all|status|edit} "
            "[pane-id|window-index|session:window] [thread-id|auto] "
            "[--message TEXT | --message-file FILE]",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
