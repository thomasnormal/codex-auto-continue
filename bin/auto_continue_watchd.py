#!/usr/bin/env python3
"""Process manager for auto-continue watchers.

Requires: rich (pip install rich).
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from auto_continue_logwatch import discover_thread_for_pane as _discover_thread_for_pane
from auto_continue_logwatch import thread_from_codex_pid as _thread_from_codex_pid

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

os.makedirs(STATE_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sanitize_key(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)


def key_from_pane(pane: str) -> str:
    return sanitize_key(pane)


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


def state_file_for_thread(thread_id: str) -> str:
    return os.path.join(STATE_DIR, f"acw_session.{thread_id}.json")


def _read_session_state(thread_id: str) -> dict:
    path = state_file_for_thread(thread_id)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_session_state(thread_id: str, data: dict) -> None:
    path = state_file_for_thread(thread_id)
    existing = _read_session_state(thread_id)
    existing.update(data)
    try:
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(existing, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Tmux helpers
# ---------------------------------------------------------------------------


def run_tmux(*args: str) -> str | None:
    """Run a tmux command, trying socket env then fallback without TMUX env."""
    cmd: list[str] = ["tmux"]
    sock = os.environ.get("AUTO_CONTINUE_TMUX_SOCKET", "")
    if not sock:
        sock = _tmux_socket_from_env()
    if sock:
        cmd += ["-S", sock]
    cmd += list(args)
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    if not sock and os.environ.get("TMUX", ""):
        # If the current client/server is healthy, do not silently fall back to
        # another tmux server (that can create windows in the wrong session).
        if _tmux_client_env_healthy():
            return None
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        try:
            return subprocess.check_output(
                ["tmux"] + list(args), stderr=subprocess.DEVNULL, text=True, env=env
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    elif sock:
        # Socket override can become stale; fallback to default tmux server.
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        try:
            return subprocess.check_output(
                ["tmux"] + list(args),
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return None


def _tmux_socket_from_env() -> str:
    """Return tmux socket path parsed from $TMUX, or empty string."""
    tmux_env = os.environ.get("TMUX", "")
    if not tmux_env:
        return ""
    # TMUX format: "/tmp/tmux-UID/default,<client_pid>,<session_id>"
    parts = tmux_env.split(",", 1)
    if not parts:
        return ""
    sock = parts[0].strip()
    return sock if sock else ""


def _tmux_client_env_healthy() -> bool:
    """Return True when TMUX points to a live current client/server."""
    if not os.environ.get("TMUX", ""):
        return False
    try:
        subprocess.check_output(
            ["tmux", "display-message", "-p", "#{session_name}"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


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


def resolve_pane_from_window_name(name: str) -> str | None:
    """Resolve a window name to a pane id.  Returns None if not found or ambiguous."""
    fmt = "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_active}"
    listing = run_tmux("list-panes", "-a", "-F", fmt)
    if not listing:
        return None

    matches: list[tuple[str, str, str, str]] = []  # (session, window_index, pane_id, pane_active)
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        sess, widx, wname, pid_, pactive = parts[:5]
        if wname == name:
            matches.append((sess, widx, pid_, pactive))

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0][2] if is_pane_id(matches[0][2]) else None

    # Multiple panes across windows with the same name — ambiguous.
    unique_windows = {(s, w) for s, w, _, _ in matches}
    if len(unique_windows) > 1:
        labels = ", ".join(f"{s}:{w}" for s, w in sorted(unique_windows))
        print(
            f"error: window name '{name}' is ambiguous across windows: {labels}",
            file=sys.stderr,
        )
        return None

    # Multiple panes in the same window — pick the active one, else first.
    for s, w, p, pa in matches:
        if pa == "1" and is_pane_id(p):
            return p
    return matches[0][2] if is_pane_id(matches[0][2]) else None


def resolve_pane_target(target: str) -> str:
    """Resolve any pane/window target to a tmux pane id (%N).

    Tries, in order: pane id, window index, tmux window name, thread id,
    then state file window_name (for watchers whose pane is gone).
    """
    if is_pane_id(target):
        return target

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target(target)
        if pane and is_pane_id(pane):
            return pane
        print(f"error: could not resolve window target '{target}'", file=sys.stderr)
        sys.exit(1)

    # Try resolving as a tmux window name.
    pane = resolve_pane_from_window_name(target)
    if pane and is_pane_id(pane):
        return pane

    # Try as a thread id — find the watcher's pane.
    if is_thread_id(target):
        for r in watcher_rows():
            if r["thread"].lower() == target.lower():
                return r["pane"]

    print(f"error: no watcher or window found for '{target}'", file=sys.stderr)
    print(
        "hint: use a pane id ('%6'), window name, thread id, or window index ('2')",
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


def detect_thread_from_pane_tty(pane: str) -> str | None:
    """Check pane tty for a `codex resume <thread-id>` command."""
    pane_tty = run_tmux("display-message", "-p", "-t", pane, "#{pane_tty}")
    if not pane_tty:
        return None
    pane_tty = pane_tty.strip()
    if not pane_tty:
        return None
    tty_for_ps = pane_tty.removeprefix("/dev/")
    if not tty_for_ps:
        return None

    try:
        ps_out = subprocess.check_output(
            ["ps", "-t", tty_for_ps, "-o", "pid=,args="],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

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
    return None


def detect_thread_id_for_pane(pane: str) -> str | None:
    for method in (_discover_thread_for_pane, detect_thread_from_pane_tty):
        tid = method(pane)
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

    print(
        f"error: could not determine thread_id for pane={pane}; start requires a concrete thread id",
        file=sys.stderr,
    )
    sys.exit(1)


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


def _normalize_message_text(text: str) -> str:
    """Match logwatch behavior: strip comment lines and trim."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).strip()


def _message_text(mode: str, value: str) -> str:
    """Resolve the concrete continue message text."""
    if mode == "inline":
        return _normalize_message_text(value)
    try:
        text = Path(value).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _normalize_message_text(text)


def _set_thread_name(thread_id: str, name: str) -> None:
    if not is_thread_id(thread_id):
        return
    _write_session_state(thread_id, {"thread_id": thread_id, "name": name})


def ensure_tmux_window_rename_hook() -> None:
    """Install a global tmux hook to keep session names in sync."""
    cmd = (
        f"{shlex.quote(sys.executable)} "
        f"{shlex.quote(str(Path(__file__).resolve()))} "
        "_window-renamed "
        "\"#{window_id}\" "
        "#{q:window_name}"
    )
    run_tmux("set-hook", "-g", "window-renamed", f"run-shell {shlex.quote(cmd)}")


def cmd_window_renamed(argv: list[str]) -> None:
    """Hook handler: sync renamed tmux window title into session state."""
    if len(argv) < 2:
        return
    window_id = argv[0]
    new_name = argv[1]
    listing = run_tmux("list-panes", "-t", window_id, "-F", "#{pane_id}")
    if not listing:
        return

    seen_threads: set[str] = set()
    for pane in (ln.strip() for ln in listing.splitlines()):
        if not is_pane_id(pane):
            continue
        rows = watcher_rows(pane)
        tid = rows[0]["thread"].lower() if rows and is_thread_id(rows[0].get("thread", "")) else ""
        if not tid or tid in seen_threads:
            continue
        seen_threads.add(tid)
        _set_thread_name(tid, new_name)


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


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
        # ps output loses null-delimited argv boundaries; use shell-like
        # splitting and rely on state files for canonical message data.
        try:
            tokens = shlex.split(argstr)
        except ValueError:
            tokens = argstr.split()
        has_python = any(re.search(r"(^|/)python([0-9.]+)?$", t) for t in tokens)
        has_script = any(t.endswith("auto_continue_logwatch.py") for t in tokens)
        if not has_python or not has_script:
            continue

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
    """Check if a process is in stopped (T) state via ``ps``."""
    try:
        state = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "state="],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return state.startswith("T")
    except (subprocess.CalledProcessError, FileNotFoundError):
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
        "--state-file", state_file_for_thread(thread_id),
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
    ensure_tmux_window_rename_hook()

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

    # Warn if another watcher is already running for the same thread_id.
    if is_thread_id(thread_id):
        for r in watcher_rows():
            if r["pane"] == pane:
                continue
            if r["thread"].lower() == thread_id.lower():
                print(
                    f"warning: another watcher (pid={r['pid']} pane={r['pane']}) "
                    f"is already watching thread {thread_id[:8]}…",
                    file=sys.stderr,
                )
                print(
                    f"hint: stop it first with 'acw stop {r['pane']}' to avoid duplicates",
                    file=sys.stderr,
                )
                break

    message_text = _message_text(msg_mode, msg_value)
    wn = run_tmux("display-message", "-p", "-t", pane, "#{window_name}") or ""
    _write_session_state(thread_id, {
        "thread_id": thread_id,
        "name": wn.strip(),
        "message": message_text,
    })

    if (not thread_arg or thread_arg == "auto") and thread_id != "auto":
        print(f"resolved: pane={pane} thread_id={thread_id}")

    Path(pf).unlink(missing_ok=True)

    # Disable tmux automatic window renaming so the window name doesn't
    # change to "python3" (the logwatch process) when codex exits.
    run_tmux("set-option", "-w", "-t", pane, "automatic-rename", "off")

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


def _find_watchers_by_window_name(name: str) -> list[dict[str, str]]:
    """Find running watchers whose session state name matches *name*."""
    matches: list[dict[str, str]] = []
    for r in watcher_rows():
        tid = r["thread"]
        if not tid or not is_thread_id(tid):
            continue
        ss = _read_session_state(tid)
        if ss.get("name", "") == name:
            matches.append(r)
    return matches


def _stop_watcher_rows(rows: list[dict[str, str]]) -> None:
    """Stop watcher processes given rows from watcher_rows()."""
    for r in rows:
        pid_str = r["pid"]
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        try:
            if _is_pid_stopped(pid):
                os.kill(pid, signal.SIGCONT)
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        key = key_from_pane(r["pane"])
        Path(pid_file_for_key(key)).unlink(missing_ok=True)
        print(f"stopped: pane={r['pane']} pid={pid_str}")


def _find_watchers_by_thread(thread_id: str) -> list[dict[str, str]]:
    """Find running watchers whose thread_id matches."""
    tid = thread_id.lower()
    return [r for r in watcher_rows() if r["thread"].lower() == tid]


def cmd_stop(argv: list[str]) -> None:
    target = argv[0] if argv else ""

    if target:
        # Thread ID — stable identifier.
        if is_thread_id(target):
            matches = _find_watchers_by_thread(target)
            if matches:
                _stop_watcher_rows(matches)
                return
            print(f"not running: thread={target}", file=sys.stderr)
            return

        # Pane ID.
        if is_pane_id(target):
            stop_pane_watchers(target)
            return

        # Match running watchers by state file window_name first — this
        # finds the actual watcher process even if the tmux window has
        # been reassigned to a different pane.
        matches = _find_watchers_by_window_name(target)
        if matches:
            _stop_watcher_rows(matches)
            return

        # Tmux window name or index.
        pane = resolve_pane_from_window_name(target)
        if not pane:
            pane = resolve_pane_from_window_target(target) if re.fullmatch(r"\d+|[^:]+:\d+", target) else None
        if pane and is_pane_id(pane):
            stop_pane_watchers(pane)
            return

        print(f"error: no watcher found for '{target}'", file=sys.stderr)
        print(
            "hint: use a thread id, pane id ('%6'), window name, or window index ('2')",
            file=sys.stderr,
        )
        sys.exit(1)

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

    if pane_arg and pane_arg != "*":
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

    if pane_arg and pane_arg != "*":
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


def _restart_panes(panes: list[str]) -> None:
    restarted = 0
    for pane in panes:
        if run_tmux("display-message", "-p", "-t", pane, "#{pane_id}") is None:
            stop_pane_watchers(pane)
            continue

        print(f"--- restarting {pane} ---")
        _restart_one(pane, pane, [])
        restarted += 1

    if restarted == 0:
        print("no live panes to restart")


def _restart_one(target: str, pane: str, rest: list[str]) -> None:
    if pane != target:
        print(f"resolved: target={target} pane={pane}")

    thread_arg, msg_mode, msg_value, msg_explicit = parse_thread_and_message_args(rest)

    if not msg_explicit:
        tid = detect_thread_id_for_pane(pane) or thread_from_running_watcher_for_pane(pane) or ""
        if tid and is_thread_id(tid):
            ss = _read_session_state(tid)
            msg = ss.get("message", "")
            if msg:
                msg_mode, msg_value = "inline", msg

    if msg_mode == "file" and not os.path.isfile(msg_value):
        msg_value = DEFAULT_MSG_FILE

    if not thread_arg:
        thread_arg = detect_thread_id_for_pane(pane) or ""
        if not thread_arg:
            thread_arg = thread_from_running_watcher_for_pane(pane) or ""

    stop_pane_watchers(pane)

    start_args: list[str] = [pane]
    if thread_arg:
        start_args.append(thread_arg)
    if msg_mode == "inline":
        start_args += ["--message", msg_value]
    else:
        start_args += ["--message-file", msg_value]

    cmd_start(start_args)


def cmd_restart(argv: list[str]) -> None:
    rest = list(argv)
    if rest and not rest[0].startswith("--"):
        target = rest.pop(0)
    else:
        target = ""
    if not target or target == "*":
        if rest:
            selector = "restart '*'" if target == "*" else "restart"
            print(f"error: {selector} does not accept additional arguments", file=sys.stderr)
            sys.exit(2)
        panes = sorted({r["pane"] for r in watcher_rows() if r.get("pane")})
        if not panes:
            print("no running watchers to restart")
            return
        _restart_panes(panes)
        return

    pane = resolve_pane_target(target)
    _restart_one(target, pane, rest)


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

    tid = detect_thread_id_for_pane(pane) or thread_from_running_watcher_for_pane(pane) or ""

    value = ""
    if tid and is_thread_id(tid):
        ss = _read_session_state(tid)
        value = ss.get("message", "")
    if not value:
        rows = watcher_rows(pane)
        if rows:
            value = rows[0]["msg_inline"]

    new_value = _edit_message_interactive(value)
    if new_value is None:
        return

    if new_value == value:
        print("edit: message unchanged")
        return

    if tid and is_thread_id(tid):
        _write_session_state(tid, {"message": new_value})
    print(f"edit: message updated for pane {pane}")
    if watcher_rows(pane):
        cmd_restart([pane, "--message", new_value])


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------


def _build_pane_window_map() -> dict[str, str]:
    listing = run_tmux("list-panes", "-a", "-F", "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}")
    mapping: dict[str, str] = {}
    if listing:
        for line in listing.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4 and parts[3]:
                mapping[parts[3]] = f"{parts[0]}:{parts[1]}:{parts[2]}"
    return mapping


def _threads_from_pstree(shell_pid: str) -> list[str]:
    """Walk a process tree and return thread_ids from any codex processes found."""
    try:
        result = subprocess.run(
            ["pstree", "-p", shell_pid],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return []
    except FileNotFoundError:
        return []
    threads: list[str] = []
    for pid in re.findall(r"\((\d+)\)", result.stdout):
        tid = _thread_from_codex_pid(pid)
        if tid:
            threads.append(tid)
            break  # one codex per tree is enough
    return threads


def _build_thread_pane_map() -> dict[str, str]:
    """Map thread_id -> current tmux pane_id by scanning pane process trees."""
    listing = run_tmux("list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}")
    mapping: dict[str, str] = {}
    if listing:
        for line in listing.splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 2:
                continue
            pane_id, shell_pid = parts[0], parts[1]
            if not pane_id or not shell_pid.isdigit():
                continue
            for tid in _threads_from_pstree(shell_pid):
                mapping[tid] = pane_id
    return mapping


SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".codex", "sessions")

ROLLOUT_FILENAME_RE = re.compile(
    r"^rollout-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})-"
    + THREAD_ID_RE.pattern + r"\.jsonl$"
)


def _find_rollout_for_thread(thread_id: str) -> str | None:
    """Return the path to the rollout JSONL file for *thread_id*, or None."""
    if not thread_id or not is_thread_id(thread_id):
        return None
    pattern = os.path.join(SESSIONS_DIR, "*", "*", "*", f"rollout-*-{thread_id}.jsonl")
    matches = glob.glob(pattern)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _rollout_times(thread_id: str) -> tuple[str, str]:
    """Return (started, last_message) for a thread from its rollout file.

    *started* is parsed from the filename; *last_message* from the file mtime.
    Both are returned as ``HH:MM:SS`` (today) or ``Mon DD HH:MM`` (older).
    """
    path = _find_rollout_for_thread(thread_id)
    if not path:
        return "-", "-"

    # Started: parse from filename.
    m = ROLLOUT_FILENAME_RE.match(os.path.basename(path))
    if m:
        started_epoch = time.mktime(time.strptime(
            f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
            "%Y-%m-%d %H:%M:%S",
        ))
        started = _format_age(started_epoch)
    else:
        started = "-"

    # Last message: file mtime.
    try:
        mtime = os.path.getmtime(path)
        last_msg = _format_age(mtime)
    except OSError:
        last_msg = "-"

    return started, last_msg


def _format_age(epoch: float) -> str:
    """Format an epoch as a human-friendly relative age string."""
    delta = time.time() - epoch
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        return f"{h}h{m:02d}m ago"
    d = int(delta // 86400)
    h = int((delta % 86400) // 3600)
    return f"{d}d{h}h ago"


def _read_state_json(state_path: str) -> dict[str, str]:
    """Read health fields from state JSON file."""
    result: dict[str, str] = {}
    if not state_path or not os.path.isfile(state_path):
        return result
    try:
        with open(state_path) as f:
            data = json.load(f)
        for k in ("health", "health_detail", "health_ts", "last_continue_at", "window_name", "thread_id"):
            if k in data:
                result[k] = str(data[k])
    except (OSError, json.JSONDecodeError):
        pass
    return result


def _resolve_message(r: dict[str, str]) -> tuple[str, str]:
    """Return (mode, value) from session state or watcher row args.

    mode is 'file', 'inline', or '' if no message is found.
    """
    if r.get("msg_inline"):
        return "inline", r["msg_inline"]
    if r.get("msg_file"):
        return "file", r["msg_file"]

    tid = r.get("thread", "")
    if tid and is_thread_id(tid):
        ss = _read_session_state(tid)
        msg = ss.get("message", "")
        if msg:
            return "inline", msg
    return "", ""


def _short_thread_id(thread_id: str) -> str:
    """Return a compact display form for a thread id in summary tables."""
    if not is_thread_id(thread_id):
        return thread_id
    return f"{thread_id[:8]}…{thread_id[-4:]}"


def _compute_state(r: dict[str, str], sj: dict[str, str]) -> str:
    """Derive display state from process status and health JSON."""
    pid = r.get("pid", "")
    if pid and pid.isdigit() and _is_pid_stopped(int(pid)):
        return "paused"
    # No running watcher process.
    if not pid:
        return sj.get("health", "") or "dead"
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


_STATE_STYLES = {
    "running": "green",
    "paused": "yellow",
    "stale": "red",
    "warn": "dark_orange",
    "error": "bold red",
    "dead": "dim",
}


def _styled_state(state: str) -> str:
    style = _STATE_STYLES.get(state, "")
    return f"[{style}]{state}[/{style}]" if style else state


_MAX_MSG_LINES = 3


def _clamp_visual_lines(text: str, max_lines: int, col_width: int) -> str:
    """Truncate *text* so it fits in *max_lines* visual lines at *col_width*."""
    if col_width < 10:
        col_width = 40
    lines = text.split("\n")
    visual = 0
    kept: list[str] = []
    for line in lines:
        vl = max(1, (len(line) + col_width - 1) // col_width) if line else 1
        if visual + vl > max_lines:
            remaining = max_lines - visual
            if remaining > 0:
                kept.append(line[: remaining * col_width])
            result = "\n".join(kept)
            if len(result) < len(text):
                result = result.rstrip() + "…"
            return result
        kept.append(line)
        visual += vl
    return text


def _status_table(resolved: list[tuple[dict[str, str], str, str]]) -> None:
    import shutil

    from rich.console import Console
    from rich.table import Table

    # Precompute row data to estimate column widths.
    # (window_pane, state, started, last_msg, last_acw, msg_raw)
    rows_data: list[tuple[str, str, str, str, str, str]] = []
    for r, current_pane, window_label in resolved:
        sj = _read_state_json(r["state"])
        state_value = _compute_state(r, sj)
        started, last_msg = _rollout_times(r["thread"])
        last_acw = sj.get("last_continue_at", "")
        if last_acw:
            try:
                epoch = time.mktime(time.strptime(last_acw, "%Y-%m-%d %H:%M:%S"))
                last_acw = _format_age(epoch)
            except (ValueError, OverflowError):
                pass
        if not last_acw:
            last_acw = "-"
        msg_raw = _message_summary_for_row(r)
        line1 = f"{window_label}/{current_pane}" if current_pane else window_label
        tid = r["thread"] if is_thread_id(r["thread"]) else ""
        short_tid = _short_thread_id(tid) if tid else ""
        window_pane = f"{line1}\n[dim]{short_tid}[/dim]" if short_tid else line1
        rows_data.append((window_pane, state_value, started, last_msg, last_acw, msg_raw))

    # Estimate MESSAGE column width from terminal width and other columns.
    term_w = shutil.get_terminal_size((120, 24)).columns
    headers = ("WINDOW/PANE", "STATE", "STARTED", "LAST_MSG", "LAST_ACW")
    col_widths = [len(h) for h in headers]
    for row in rows_data:
        for i in range(5):
            # Measure first line only, strip rich markup tags.
            text = row[i].split("\n")[0]
            text = re.sub(r"\[/?[^\]]*\]", "", text)
            col_widths[i] = max(col_widths[i], len(text))
    # Rich table overhead: borders (7 │) + padding (2 per col × 6 = 12) = 19
    msg_col_w = max(20, min(60, term_w - sum(col_widths) - 19))

    table = Table(show_lines=False, expand=True)
    table.add_column("WINDOW/PANE", no_wrap=True)
    table.add_column("STATE", no_wrap=True)
    table.add_column("STARTED", no_wrap=True)
    table.add_column("LAST_MSG", no_wrap=True)
    table.add_column("LAST_ACW", no_wrap=True)
    table.add_column("MESSAGE", ratio=1, max_width=60)

    for window_pane, state_value, started, last_msg, last_acw, msg_raw in rows_data:
        msg_summary = _clamp_visual_lines(msg_raw, _MAX_MSG_LINES, msg_col_w)
        table.add_row(
            window_pane, _styled_state(state_value),
            started, last_msg, last_acw, msg_summary,
        )

    Console().print(table)


def _status_details(resolved: list[tuple[dict[str, str], str, str]]) -> None:
    total = len(resolved)
    for idx, (r, current_pane, window_label) in enumerate(resolved, 1):
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

        started, last_msg = _rollout_times(r["thread"])

        last_acw = sj.get("last_continue_at", "-")

        print(f"=== Watcher {idx}/{total} ===")
        print(f"  {'WINDOW:':<16} {window_label}")
        print(f"  {'PANE:':<16} {current_pane or '(no live pane)'}")
        print(f"  {'PID:':<16} {r['pid']}")
        print(f"  {'THREAD_ID:':<16} {r['thread'] or 'unknown'}")
        print(f"  {'STATE:':<16} {state_value}")
        print(f"  {'STARTED:':<16} {started}")
        print(f"  {'LAST_MSG:':<16} {last_msg}")
        print(f"  {'LAST_ACW:':<16} {last_acw}")
        hd = sj.get("health_detail", "")
        if hd:
            print(f"  {'HEALTH_DETAIL:':<16} {hd}")
        hts = sj.get("health_ts", "")
        if hts:
            print(f"  {'HEALTH_SINCE:':<16} {hts}")
        print(f"  {'LAST_EVENT:':<16} {last_event}")
        print(f"  {'MESSAGE:':<16} {msg_full}")
        if r["watch"]:
            print(f"  {'WATCH_LOG:':<16} {r['watch']}")
        if r["state"]:
            print(f"  {'STATE_FILE:':<16} {r['state']}")


def _load_sessions() -> list[dict[str, str]]:
    """Load all sessions from session state files (keyed by thread_id)."""
    candidates: list[dict[str, str]] = []
    for sf in glob.glob(os.path.join(STATE_DIR, "acw_session.*.json")):
        try:
            with open(sf) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        thread_id = data.get("thread_id", "")
        if not thread_id or not is_thread_id(thread_id):
            continue
        candidates.append({
            "thread_id": thread_id,
            "name": data.get("name", ""),
            "message": data.get("message", ""),
            "state_file": sf,
            "health": data.get("health", ""),
            "health_detail": data.get("health_detail", ""),
            "health_ts": data.get("health_ts", ""),
            "last_continue_at": data.get("last_continue_at", ""),
        })
    return candidates


def _select_session_files(
    sessions: list[dict[str, str]],
    selector: str,
) -> list[dict[str, str]]:
    """Select session rows by exact name or thread-id prefix."""
    sel = selector.lower()
    matches = [
        s for s in sessions
        if s["thread_id"].lower() == sel
        or s["thread_id"].lower().startswith(sel)
        or s.get("name", "").lower() == sel
    ]
    if not matches:
        print(f"error: no session matches '{selector}'", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(
            f"error: selector '{selector}' is ambiguous; matched {len(matches)} sessions",
            file=sys.stderr,
        )
        for s in matches:
            name = s.get("name", "") or "-"
            print(f"  - {name}: {s['thread_id']}", file=sys.stderr)
        print(
            "hint: use an exact window name or a longer thread id prefix",
            file=sys.stderr,
        )
        sys.exit(1)
    return matches


def cmd_status(argv: list[str]) -> None:
    pane_arg = ""
    details = False
    for arg in argv:
        if arg == "--details":
            details = True
        else:
            pane_arg = arg

    # 1. Load all sessions from state files.
    sessions = _load_sessions()

    # 2. Build maps from tmux (two scans total).
    pane_window = _build_pane_window_map()
    thread_pane = _build_thread_pane_map()

    # 3. Build set of running watcher PIDs per pane.
    watcher_row_for_thread: dict[str, dict[str, str]] = {}
    watcher_pid_for_pane: dict[str, str] = {}
    for r in watcher_rows():
        tid = r.get("thread", "").lower()
        if tid and tid not in watcher_row_for_thread:
            watcher_row_for_thread[tid] = r
        watcher_pid_for_pane[r["pane"]] = r["pid"]

    # 4. Merge: for each session, look up live pane and watcher status.
    resolved: list[tuple[dict[str, str], str, str]] = []
    for s in sessions:
        tid = s["thread_id"].lower()
        watcher_row = watcher_row_for_thread.get(tid, {})
        ref = thread_pane.get(tid, "") if thread_pane else ""
        if not ref:
            ref = watcher_row.get("pane", "")
        live_pane = ref if is_pane_id(ref) else ""
        # Prefer live tmux window name, fall back to state file.
        window_label = pane_window.get(live_pane, "") if live_pane else ""
        if not window_label:
            window_label = s["name"] or "-"

        # Build a row dict compatible with _status_table / _status_details.
        # Find watcher PID: if live_pane is known, look up by pane.
        watcher_pid = watcher_pid_for_pane.get(live_pane, "") if live_pane else ""
        if not watcher_pid:
            watcher_pid = watcher_row.get("pid", "")
        row: dict[str, str] = {
            "pid": watcher_pid,
            "pane": live_pane,
            "thread": s["thread_id"],
            "state": s["state_file"],
            "watch": (
                watch_log_for_key(key_from_pane(live_pane))
                if live_pane else watcher_row.get("watch", "")
            ),
            "msg_file": "",
            "msg_inline": watcher_row.get("msg_inline", "") or s.get("message", ""),
        }
        resolved.append((row, live_pane, window_label))

    # Optional pane filter.
    if pane_arg:
        target = pane_arg
        pane = resolve_pane_target(target)
        if pane != target:
            print(f"resolved: target={target} pane={pane}")
        resolved = [(r, lp, wl) for r, lp, wl in resolved if lp == pane]

    detail_tag = " (details)" if details else ""
    print(f"Sessions{detail_tag}: {len(resolved)}")

    if not resolved:
        print("(none)")
        return

    resolved.sort(key=lambda t: t[2])

    if details:
        _status_details(resolved)
    else:
        _status_table(resolved)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cmd_cleanup(argv: list[str]) -> None:
    """Cleanup stale files, or remove a single selected session state file."""
    selector = ""
    rest = list(argv)
    while rest:
        tok = rest.pop(0)
        if tok.startswith("-"):
            print(f"error: unknown option '{tok}'", file=sys.stderr)
            sys.exit(1)
        if selector:
            print("error: cleanup accepts at most one target selector", file=sys.stderr)
            sys.exit(1)
        selector = tok

    if not selector:
        cleanup_stale_files()
        print("cleanup: removed stale files")
        return

    sessions = _load_sessions()
    selected = _select_session_files(sessions, selector)
    target = selected[0].get("state_file", "")
    if not target:
        print(f"error: selected session has no state file: {selector}", file=sys.stderr)
        sys.exit(1)

    Path(target).unlink(missing_ok=True)
    print(f"cleanup: removed {target}")


def cleanup_stale_files() -> None:
    """Remove files for watchers that are no longer running."""
    # PID files for dead processes.
    for pf in glob.glob(os.path.join(STATE_DIR, "auto_continue_logwatch.*.pid")):
        if not is_running_pid_file(pf):
            Path(pf).unlink(missing_ok=True)
    if not is_running_pid_file(LEGACY_PID_FILE):
        Path(LEGACY_PID_FILE).unlink(missing_ok=True)

    # Build set of live watcher keys/threads (single ps scan).
    live_keys: set[str] = set()
    live_threads: set[str] = set()
    for r in watcher_rows():
        k = key_from_pane(r["pane"])
        if k:
            live_keys.add(k)
        tid = r.get("thread", "")
        if tid:
            live_threads.add(tid.lower())

    # Log files for dead watchers.
    keep_logs: set[str] = set()
    for k in live_keys:
        keep_logs.add(watch_log_for_key(k))
        keep_logs.add(run_log_for_key(k))
    for pat in (
        os.path.join(STATE_DIR, "auto_continue_logwatch*.log"),
        os.path.join(STATE_DIR, "auto_continue_logwatch*.runner.log"),
    ):
        for lf in glob.glob(pat):
            if lf not in keep_logs:
                Path(lf).unlink(missing_ok=True)

    # Session files for threads without a running watcher.
    for sf in glob.glob(os.path.join(STATE_DIR, "acw_session.*.json")):
        base = os.path.basename(sf)
        tid = base.removeprefix("acw_session.").removesuffix(".json")
        if tid.lower() not in live_threads:
            Path(sf).unlink(missing_ok=True)


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
    if subcmd in ("start", "stop", "restart", "edit", "cleanup"):
        cleanup_stale_files()

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "restart": cmd_restart,
        "cleanup": cmd_cleanup,
        "status": cmd_status,
        "edit": cmd_edit,
        "_window-renamed": cmd_window_renamed,
    }

    if subcmd in commands:
        commands[subcmd](rest)
    else:
        print(
            "usage: auto_continue_watchd.py {start|stop|pause|resume|restart|cleanup|status|edit} "
            "[pane-id|window-index|session:window] [thread-id|auto] "
            "[--message TEXT | --message-file FILE]",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
