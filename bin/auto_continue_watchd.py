#!/usr/bin/env python3
"""Process manager for auto-continue watchers."""

from __future__ import annotations

from collections import defaultdict
import glob
import io
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from auto_continue_logwatch import thread_times_from_state_db
from auto_continue_logwatch import thread_from_codex_pid as _thread_from_codex_pid
from auto_continue_logwatch import CodexLogEvent as _LogwatchCodexLogEvent
from auto_continue_logwatch import append_log as _watch_append_log
from auto_continue_logwatch import check_codex_log_tail_for_pending as _check_codex_log_tail_for_pending
from auto_continue_logwatch import check_pane_for_errors as _check_pane_for_errors
from auto_continue_logwatch import check_pane_for_interrupt as _check_pane_for_interrupt
from auto_continue_logwatch import compute_health as _compute_watch_health
from auto_continue_logwatch import now_ts as _watch_now_ts
from auto_continue_logwatch import parse_codex_log_event as _parse_codex_log_event
from auto_continue_logwatch import parse_codex_log_interrupt as _parse_codex_log_interrupt
from auto_continue_logwatch import tmux_send as _watch_tmux_send

# ---------------------------------------------------------------------------
# Constants & path resolution
# ---------------------------------------------------------------------------

THREAD_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

DAEMON_POLL_INTERVAL_SECS = 0.2
DAEMON_IDLE_EXIT_SECS = 2.0
DAEMON_CODEX_LOG_PATH = Path.home() / ".codex" / "log" / "codex-tui.log"
_DAEMON_RUNTIME_ID_SEQ = 0


def _next_daemon_runtime_id() -> str:
    global _DAEMON_RUNTIME_ID_SEQ
    _DAEMON_RUNTIME_ID_SEQ += 1
    return f"{os.getpid()}{_DAEMON_RUNTIME_ID_SEQ:06d}"


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
DEFAULT_MESSAGE_TEXT = (
    "please continue with the workstream.\n\n"
    "Keep momentum, validate changes, and move to the next highest-value task.\n"
)
KNOWN_SHELL_COMMANDS = frozenset(
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

os.makedirs(STATE_DIR, exist_ok=True)

def build_help_text(prog: str) -> str:
    return f"""Usage:
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
  status    Show watcher state. Default command when no subcommand is given.
  start     Start a watcher for a live tmux target. Auto-detects the Codex thread unless you pass one explicitly.
  stop      Stop one watcher, one pane's watchers, or all watchers, and forget their saved sessions.
  pause     Pause one managed session, or all managed sessions when no target is given.
  resume    Restart one or more paused watchers, or all paused watchers, from current state.
  restart   Restart one watcher or all live panes, including recoverable dead sessions, when no target is given. Use --restart-codex on a single target to relaunch Codex with the same thread.
  edit      Edit the stored continue message for a running watcher, then restart it.
  repair    Inspect or apply safe live-state repairs using the invariant model. With no target it defaults to --dry-run; use --all to apply safe repairs across all panes.
  cleanup   Remove dead saved sessions and stale daemon/runtime files.
  doctor    Check tmux reachability, Codex auth/state, and optional pane/thread health.

Targets:
  Commands with a target accept a tmux pane id (%6), window index (2), session:window (0:2), or exact tmux window name (uvm).
  pause, resume, and restart act on all watchers when no target is given. resume also accepts multiple explicit targets.

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

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def sanitize_key(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)


def key_from_pane(pane: str) -> str:
    return sanitize_key(pane)


def is_pane_id(s: str) -> bool:
    return bool(re.fullmatch(r"%[0-9]+", s))


def current_tmux_pane() -> str:
    pane = os.environ.get("TMUX_PANE", "").strip()
    return pane if is_pane_id(pane) else ""


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


def _remove_session_state(thread_id: str) -> bool:
    if not is_thread_id(thread_id):
        return False
    path = Path(state_file_for_thread(thread_id.lower()))
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Tmux helpers
# ---------------------------------------------------------------------------


def run_tmux(*args: str) -> str | None:
    """Run a tmux command, trying socket env then fallback without TMUX env."""
    cmd: list[str] = ["tmux"]
    sock = _preferred_tmux_socket()
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


def _preferred_tmux_socket() -> str:
    sock = os.environ.get("AUTO_CONTINUE_TMUX_SOCKET", "")
    return sock if sock else _tmux_socket_from_env()


def _is_default_tmux_socket_path(sock: str) -> bool:
    return bool(re.fullmatch(r"/tmp/tmux-\d+/default", sock))


def _tmux_scope_key(socket_path: str | None = None) -> str:
    sock = _preferred_tmux_socket() if socket_path is None else socket_path
    sock = (sock or "").strip()
    if not sock or _is_default_tmux_socket_path(sock):
        return ""
    return sock


def _tmux_scope_token(socket_path: str | None = None) -> str:
    scope = _tmux_scope_key(socket_path)
    return sanitize_key(scope if scope else "default")


def _tmux_scope_label(socket_path: str | None = None) -> str:
    scope = _tmux_scope_key(socket_path)
    return scope if scope else "(default)"


def _session_scope_matches_current(data: dict) -> bool:
    return _tmux_scope_key(str(data.get("tmux_socket", "")).strip()) == _tmux_scope_key()


def daemon_pid_file_for_scope(socket_path: str | None = None) -> str:
    return os.path.join(STATE_DIR, f"acw_daemon.{_tmux_scope_token(socket_path)}.pid")


def daemon_log_file_for_scope(socket_path: str | None = None) -> str:
    return os.path.join(STATE_DIR, f"acw_daemon.{_tmux_scope_token(socket_path)}.log")


def daemon_runtime_file_for_thread(thread_id: str, socket_path: str | None = None) -> str:
    token = _tmux_scope_token(socket_path)
    return os.path.join(STATE_DIR, f"acw_runtime.{token}.{thread_id.lower()}.json")


def daemon_runtime_glob_for_scope(socket_path: str | None = None) -> str:
    token = _tmux_scope_token(socket_path)
    return os.path.join(STATE_DIR, f"acw_runtime.{token}.*.json")


def _write_runtime_state(thread_id: str, data: dict, socket_path: str | None = None) -> None:
    if not is_thread_id(thread_id):
        return
    path = Path(daemon_runtime_file_for_thread(thread_id, socket_path))
    existing: dict[str, object] = {}
    try:
        with path.open() as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            existing = loaded
    except (OSError, json.JSONDecodeError):
        existing = {}
    existing.update(data)
    existing["thread"] = thread_id.lower()
    existing["tmux_socket"] = _tmux_scope_key(socket_path)
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


def _remove_runtime_state(thread_id: str, socket_path: str | None = None) -> None:
    if not is_thread_id(thread_id):
        return
    Path(daemon_runtime_file_for_thread(thread_id, socket_path)).unlink(missing_ok=True)


def _read_runtime_rows(socket_path: str | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path_str in glob.glob(daemon_runtime_glob_for_scope(socket_path)):
        try:
            with open(path_str) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        thread_id = str(data.get("thread", "")).lower()
        daemon_pid = str(data.get("daemon_pid", "")).strip()
        if daemon_pid and daemon_pid.isdigit():
            try:
                os.kill(int(daemon_pid), 0)
            except OSError:
                Path(path_str).unlink(missing_ok=True)
                continue
        row = {
            "pid": str(data.get("pid", "")).strip() or daemon_pid,
            "pane": str(data.get("pane", "")).strip(),
            "thread": thread_id,
            "state": str(data.get("state", "")).strip(),
            "watch": str(data.get("watch", "")).strip(),
            "cwd": str(data.get("cwd", "")).strip(),
            "msg_inline": str(data.get("msg_inline", "")).strip(),
            "tmux_socket": str(data.get("tmux_socket", "")).strip(),
            "paused": "1" if data.get("paused") else "",
            "runtime_status": str(data.get("runtime_status", "")).strip(),
            "runtime_detail": str(data.get("runtime_detail", "")).strip(),
            "generation": str(data.get("generation", "")).strip(),
            "worker_label": str(data.get("worker_label", "")).strip(),
        }
        rows.append(row)
    return rows


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


def _tmux_server_pids() -> list[str]:
    sock = _preferred_tmux_socket()
    try:
        ps_out = subprocess.check_output(
            ["ps", "-u", str(os.getuid()), "-o", "pid=,args="],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    socket_matches: list[str] = []
    pids: list[str] = []
    for line in ps_out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid_str, args = parts
        if not pid_str.isdigit() or "tmux" not in args:
            continue
        if sock and sock in args:
            socket_matches.append(pid_str)
            continue
        pids.append(pid_str)
    if socket_matches:
        return socket_matches
    return pids


def _tmux_socket_recovery_hint() -> str:
    sock = _preferred_tmux_socket()
    if not sock or os.path.exists(sock):
        return ""

    pids = _tmux_server_pids()
    if not pids:
        return ""
    if len(pids) == 1:
        return (
            f"hint: tmux socket '{sock}' is unreachable; "
            f"recreate it with `kill -USR1 {pids[0]}`"
        )
    joined = ", ".join(pids)
    return (
        f"hint: tmux socket '{sock}' is unreachable; "
        f"if the right server is still running, recreate its socket with "
        f"`kill -USR1 <pid>` (tmux pids: {joined})"
    )


def _print_tmux_unavailable_hint(target: str) -> None:
    print(
        f"error: tmux server is unavailable; cannot resolve window target '{target}'",
        file=sys.stderr,
    )
    print(
        "hint: start/reconnect tmux, or use a pane id if you already know it (for example '%6')",
        file=sys.stderr,
    )
    recovery = _tmux_socket_recovery_hint()
    if recovery:
        print(recovery, file=sys.stderr)


def resolve_pane_from_window_target(target: str, quiet: bool = False) -> str | None:
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
        if not quiet:
            _print_tmux_unavailable_hint(target)
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
            if not quiet:
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


def resolve_pane_from_window_name(name: str, quiet: bool = False) -> str | None:
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
        if not quiet:
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

    Tries, in order: pane id, window index, then exact tmux window name.
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

    if _tmux_socket_recovery_hint():
        _print_tmux_unavailable_hint(target)
        sys.exit(1)

    print(f"error: no watcher or window found for '{target}'", file=sys.stderr)
    print(
        "hint: use a pane id ('%6'), window name, or window index ('2')",
        file=sys.stderr,
    )
    sys.exit(1)


def _resolve_live_pane_target(target: str) -> str:
    """Best-effort live tmux target resolution without exiting on failure."""
    if is_pane_id(target):
        return target
    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target(target)
        return pane if pane and is_pane_id(pane) else ""
    pane = resolve_pane_from_window_name(target, quiet=True)
    return pane if pane and is_pane_id(pane) else ""


def resolve_start_pane_target(target: str) -> str:
    """Resolve only the canonical tmux targets accepted by `start`.

    Supported forms: pane id, window index, session:window, or exact tmux
    window name. Thread ids are intentionally rejected here because the second
    positional argument is reserved for the explicit Codex thread id.
    """
    if is_pane_id(target):
        return target

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target(target)
        if pane and is_pane_id(pane):
            return pane
        print(f"error: could not resolve window target '{target}'", file=sys.stderr)
        sys.exit(1)

    if is_thread_id(target):
        print(
            "error: start target must be a pane id, window index, session:window, or tmux window name",
            file=sys.stderr,
        )
        print(
            "hint: pass the thread id as the second positional argument: "
            "acw start <target> <thread-id>",
            file=sys.stderr,
        )
        sys.exit(2)

    pane = resolve_pane_from_window_name(target)
    if pane and is_pane_id(pane):
        return pane

    if _tmux_socket_recovery_hint():
        _print_tmux_unavailable_hint(target)
        sys.exit(1)

    print(f"error: could not resolve start target '{target}'", file=sys.stderr)
    print(
        "hint: use a pane id ('%6'), window name, or window index ('2')",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Thread ID detection
# ---------------------------------------------------------------------------


def resolve_thread_id(pane: str, requested: str = "", *, model: RuntimeModel | None = None) -> str:
    if requested and requested != "auto":
        if is_thread_id(requested):
            return requested.lower()
        print(f"error: invalid thread_id '{requested}'", file=sys.stderr)
        sys.exit(1)

    if model is None:
        model = _build_runtime_model()
    tid, thread_error = _strict_thread_for_pane(model, pane)
    if tid and is_thread_id(tid):
        return tid

    error_text = thread_error or f"could not determine thread_id for pane={pane}; start requires a concrete thread id"
    print(f"error: {error_text}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Message argument parsing
# ---------------------------------------------------------------------------


def parse_thread_and_message_args(
    args: list[str],
) -> tuple[str, str, bool]:
    thread_arg, message_value, message_explicit, _ = _parse_restartable_args(args)
    return thread_arg, message_value, message_explicit


def parse_restart_args(
    args: list[str],
) -> tuple[str, str, bool, bool]:
    return _parse_restartable_args(args, allow_restart_codex=True)


def _parse_restartable_args(
    args: list[str],
    allow_restart_codex: bool = False,
) -> tuple[str, str, bool, bool]:
    """Return (thread_arg, message_value, message_explicit, restart_codex)."""
    thread_arg = ""
    message_value = ""
    message_explicit = False
    restart_codex = False

    rest = list(args)
    if rest and not rest[0].startswith("--"):
        thread_arg = rest.pop(0)

    while rest:
        tok = rest.pop(0)
        if tok == "--message":
            if not rest:
                print("error: --message requires a value", file=sys.stderr)
                sys.exit(1)
            message_value = rest.pop(0)
            message_explicit = True
        elif allow_restart_codex and tok == "--restart-codex":
            restart_codex = True
        else:
            print(f"error: unknown option '{tok}'", file=sys.stderr)
            sys.exit(1)

    return thread_arg, message_value, message_explicit, restart_codex


# ---------------------------------------------------------------------------
# Message meta persistence
# ---------------------------------------------------------------------------


def _normalize_message_text(text: str) -> str:
    """Match logwatch behavior: strip comment lines and trim."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).strip()


def _message_text(value: str) -> str:
    """Resolve the concrete continue message text."""
    return _normalize_message_text(value)


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
# Runtime row access
# ---------------------------------------------------------------------------


def watcher_rows(pane_filter: str = "") -> list[dict[str, str]]:
    """Return daemon-managed runtime rows for the current tmux socket scope."""
    rows = _read_runtime_rows()
    if pane_filter:
        rows = [row for row in rows if row.get("pane", "") == pane_filter]
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (
            row.get("pid", ""),
            row.get("pane", ""),
            row.get("thread", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def watcher_pids_for_pane(pane: str) -> list[str]:
    return [r["pid"] for r in watcher_rows(pane)]


def _watcher_row_for_pane(pane: str) -> dict[str, str] | None:
    rows = watcher_rows(pane)
    if len(rows) == 1:
        return rows[0]
    return None


def _system_exit_detail(exc: SystemExit, stderr_text: str = "") -> str:
    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("error:"):
            return line.removeprefix("error:").strip()
    for line in reversed(lines):
        if not line.startswith("hint:"):
            return line
    if isinstance(exc.code, str) and exc.code.strip():
        return exc.code.strip()
    code = exc.code if isinstance(exc.code, int) else 1
    return f"exit {code}"


def _other_watcher_for_thread(pane: str, thread_id: str) -> dict[str, str] | None:
    if not is_thread_id(thread_id):
        return None
    for row in watcher_rows():
        if row.get("pane", "") == pane:
            continue
        row_tid = row.get("thread", "")
        if row_tid and row_tid.lower() == thread_id.lower():
            return row
    return None


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


def thread_from_running_watcher_for_pane(pane: str) -> str | None:
    rows = watcher_rows(pane)
    if len(rows) != 1:
        return None
    tid = rows[0]["thread"]
    if is_thread_id(tid):
        return tid.lower()
    return None


# ---------------------------------------------------------------------------
# Daemon runtime
# ---------------------------------------------------------------------------


@dataclass
class _DaemonCodexLogTail:
    log_path: Path
    handle: io.TextIOWrapper | None = None

    def close(self) -> None:
        if self.handle is None:
            return
        self.handle.close()
        self.handle = None


@dataclass
class _DaemonSessionRuntime:
    thread_id: str
    pane: str
    message: str
    tmux_socket: str
    generation: int
    runtime_id: str
    state_file: str
    watch_log: str
    cwd: str
    paused: bool = False
    runtime_status: str = "starting"
    runtime_detail: str = ""
    last_handled_turn: str = ""
    last_handled_thread: str = ""
    last_send_time: float = 0.0
    health: str = "ok"
    health_detail: str = ""
    last_health_check: float = 0.0
    watcher_start: float = 0.0
    interrupt_skip_armed: bool = False
    pending_startup_event: _LogwatchCodexLogEvent | None = None


def _daemon_scope_env(socket_path: str) -> dict[str, str]:
    env = dict(os.environ)
    if socket_path:
        env["AUTO_CONTINUE_TMUX_SOCKET"] = socket_path
    else:
        env.pop("AUTO_CONTINUE_TMUX_SOCKET", None)
    return env


def _daemon_pid_alive(pid_file: str) -> str:
    pid_str = _read_pid_file(pid_file)
    if not pid_str or not pid_str.isdigit():
        return ""
    try:
        os.kill(int(pid_str), 0)
    except OSError:
        return ""
    return pid_str


def _ensure_daemon_running() -> str:
    socket_path = _tmux_scope_key()
    pid_file = daemon_pid_file_for_scope(socket_path)
    pid_str = _daemon_pid_alive(pid_file)
    if pid_str:
        return pid_str

    Path(pid_file).unlink(missing_ok=True)
    log_path = daemon_log_file_for_scope(socket_path)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_daemon",
        *(["--tmux-socket", socket_path] if socket_path else []),
    ]
    log_fh = open(log_path, "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
        env=_daemon_scope_env(socket_path),
    )
    log_fh.close()

    deadline = time.time() + 5.0
    while time.time() < deadline:
        pid_str = _daemon_pid_alive(pid_file)
        if pid_str:
            return pid_str
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    print(
        f"error: failed to start acw daemon for tmux socket {_tmux_scope_label(socket_path)}",
        file=sys.stderr,
    )
    try:
        with open(log_path) as f:
            lines = f.readlines()
        for line in lines[-40:]:
            print(line, end="", file=sys.stderr)
    except OSError:
        pass
    sys.exit(1)


def _current_session_generation(data: dict) -> int:
    try:
        return int(data.get("generation", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _write_managed_session(
    thread_id: str,
    *,
    pane: str,
    name: str,
    message: str,
    paused: bool,
    generation: int | None = None,
    tmux_socket: str | None = None,
) -> None:
    if not is_thread_id(thread_id):
        return
    existing = _read_session_state(thread_id)
    if generation is None:
        generation = _current_session_generation(existing)
    _write_session_state(
        thread_id,
        {
            "thread_id": thread_id.lower(),
            "pane": pane,
            "name": name,
            "message": _message_text(message),
            "paused": bool(paused),
            "generation": int(generation),
            "tmux_socket": _tmux_scope_key(tmux_socket),
        },
    )


def _set_session_paused(thread_id: str, paused: bool) -> None:
    if not is_thread_id(thread_id):
        return
    existing = _read_session_state(thread_id)
    _write_session_state(
        thread_id,
        {
            "paused": bool(paused),
            "generation": _current_session_generation(existing),
        },
    )


def _wait_for_runtime_row(
    thread_id: str,
    predicate,
    *,
    timeout_secs: float = 5.0,
) -> dict[str, str] | None:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        row = next((item for item in watcher_rows() if item.get("thread", "").lower() == thread_id.lower()), None)
        if predicate(row):
            return row
        time.sleep(0.05)
    row = next((item for item in watcher_rows() if item.get("thread", "").lower() == thread_id.lower()), None)
    return row if predicate(row) else None


def _session_signature(session: SavedSessionRecord) -> tuple[str, str, int]:
    return (session.pane, session.message, session.generation)


def _load_scoped_session_records(socket_path: str | None = None) -> list[SavedSessionRecord]:
    records: list[SavedSessionRecord] = []
    for session in _load_sessions(socket_path):
        records.append(
            SavedSessionRecord(
                thread_id=session["thread_id"].lower(),
                name=session.get("name", ""),
                message=session.get("message", ""),
                pane=session.get("pane", ""),
                tmux_socket=_tmux_scope_key(session.get("tmux_socket", "")),
                paused=bool(session.get("paused", False)),
                generation=int(session.get("generation", 0) or 0),
                state_file=session.get("state_file", ""),
                health=session.get("health", ""),
                health_detail=session.get("health_detail", ""),
                health_ts=session.get("health_ts", ""),
                last_continue_at=session.get("last_continue_at", ""),
            )
        )
    return records


def _daemon_runtime_payload(runtime: _DaemonSessionRuntime) -> dict:
    return {
        "pid": runtime.runtime_id,
        "daemon_pid": str(os.getpid()),
        "pane": runtime.pane,
        "state": runtime.state_file,
        "watch": runtime.watch_log,
        "cwd": runtime.cwd,
        "msg_inline": runtime.message,
        "paused": runtime.paused,
        "runtime_status": "paused" if runtime.paused else (runtime.runtime_status or "running"),
        "runtime_detail": runtime.runtime_detail,
        "generation": runtime.generation,
        "worker_label": runtime.runtime_id,
        "heartbeat_ts": _watch_now_ts(),
    }


def _drain_daemon_codex_log_events(tail: _DaemonCodexLogTail) -> list[_LogwatchCodexLogEvent]:
    events: list[_LogwatchCodexLogEvent] = []
    if tail.handle is None:
        if not tail.log_path.exists():
            return events
        tail.log_path.parent.mkdir(parents=True, exist_ok=True)
        tail.handle = tail.log_path.open("r", encoding="utf-8", errors="ignore")
        tail.handle.seek(0, os.SEEK_END)
        return events

    while True:
        line = tail.handle.readline()
        if not line:
            break
        interrupt_thread = _parse_codex_log_interrupt(line)
        if interrupt_thread:
            events.append(
                _LogwatchCodexLogEvent(
                    "interrupt",
                    interrupt_thread,
                    detail="Conversation interrupted",
                )
            )
            continue
        parsed = _parse_codex_log_event(line)
        if parsed:
            events.append(_LogwatchCodexLogEvent("completion", *parsed))

    try:
        if tail.handle.tell() > tail.log_path.stat().st_size:
            tail.close()
    except FileNotFoundError:
        tail.close()
    return events


def _daemon_session_runtime(
    session: SavedSessionRecord,
    *,
    codex_log_path: Path,
) -> _DaemonSessionRuntime:
    state_file = session.state_file or state_file_for_thread(session.thread_id)
    watch_log = watch_log_for_key(key_from_pane(session.pane or session.thread_id))
    state = _read_session_state(session.thread_id)
    runtime = _DaemonSessionRuntime(
        thread_id=session.thread_id,
        pane=session.pane,
        message=_message_text(session.message),
        tmux_socket=session.tmux_socket,
        generation=session.generation,
        runtime_id=_next_daemon_runtime_id(),
        state_file=state_file,
        watch_log=watch_log,
        cwd=PROJECT_CWD,
        paused=session.paused,
        runtime_status="paused" if session.paused else "running",
        runtime_detail=session.health_detail if session.paused else "",
        last_handled_turn=str(state.get("last_handled_turn", state.get("last_sent_turn", ""))),
        last_handled_thread=str(
            state.get("last_handled_thread", state.get("last_sent_thread", state.get("thread_id", "")))
        ),
        watcher_start=time.time(),
    )
    _write_session_state(
        session.thread_id,
        {
            "thread_id": session.thread_id,
            "pane": session.pane,
            "name": session.name,
            "message": runtime.message,
            "paused": session.paused,
            "generation": session.generation,
            "tmux_socket": session.tmux_socket,
        },
    )
    _watch_append_log(Path(runtime.watch_log), f"watch: pane={runtime.pane} thread_id={runtime.thread_id}")
    pending = _check_codex_log_tail_for_pending(
        codex_log_path,
        runtime.thread_id,
        runtime.last_handled_turn,
        runtime.last_handled_thread,
        Path(runtime.watch_log),
    )
    if pending:
        runtime.pending_startup_event = _LogwatchCodexLogEvent("completion", *pending)
    return runtime


def _daemon_mark_handled(runtime: _DaemonSessionRuntime, turn_id: str, thread_id: str) -> None:
    runtime.last_handled_turn = turn_id
    runtime.last_handled_thread = thread_id
    _write_session_state(
        runtime.thread_id,
        {
            "last_handled_turn": turn_id,
            "last_handled_thread": thread_id,
        },
    )


def _daemon_skip_interrupted_turn(
    runtime: _DaemonSessionRuntime,
    turn_id: str,
    thread_id: str,
    reason: str,
) -> None:
    runtime.interrupt_skip_armed = False
    _daemon_mark_handled(runtime, turn_id, thread_id)
    _watch_append_log(
        Path(runtime.watch_log),
        f"skip: interrupted turn={turn_id} thread={thread_id} reason={reason}",
    )


def _daemon_pause_session(runtime: _DaemonSessionRuntime, reason: str) -> None:
    now = _watch_now_ts()
    runtime.paused = True
    runtime.runtime_status = "paused"
    runtime.runtime_detail = reason
    runtime.health_detail = f"auto-paused: {reason}"
    _write_session_state(
        runtime.thread_id,
        {
            "paused": True,
            "health_detail": runtime.health_detail,
            "health_ts": now,
        },
    )
    _watch_append_log(Path(runtime.watch_log), f"pause: auto-pausing watcher ({reason})")


def _daemon_process_session_events(
    runtime: _DaemonSessionRuntime,
    events: list[_LogwatchCodexLogEvent],
) -> None:
    if runtime.paused:
        runtime.runtime_status = "paused"
        return

    runtime.runtime_status = "running"
    runtime.runtime_detail = ""
    watch_log = Path(runtime.watch_log)

    for event in events:
        if runtime.paused:
            break
        if event.thread_id != runtime.thread_id:
            continue

        if event.kind == "interrupt":
            if not runtime.interrupt_skip_armed:
                runtime.interrupt_skip_armed = True
                _watch_append_log(watch_log, f"skip: armed after interrupt thread={event.thread_id}")
            continue

        if event.detail != "false":
            continue
        if event.turn_id == runtime.last_handled_turn and event.thread_id == runtime.last_handled_thread:
            continue

        if runtime.interrupt_skip_armed:
            _daemon_skip_interrupted_turn(runtime, event.turn_id, event.thread_id, "Conversation interrupted")
            continue

        pane_interrupt = _check_pane_for_interrupt(runtime.pane)
        if pane_interrupt:
            _daemon_skip_interrupted_turn(runtime, event.turn_id, event.thread_id, pane_interrupt)
            continue

        pane_error = _check_pane_for_errors(runtime.pane)
        if pane_error:
            _daemon_pause_session(runtime, pane_error)
            continue

        if time.time() - runtime.last_send_time < 1.0:
            _watch_append_log(watch_log, f"skip: cooldown turn={event.turn_id}")
            continue

        if float(SEND_DELAY_SECS) > 0.0:
            time.sleep(float(SEND_DELAY_SECS))

        pane_interrupt = _check_pane_for_interrupt(runtime.pane)
        if pane_interrupt:
            _daemon_skip_interrupted_turn(runtime, event.turn_id, event.thread_id, pane_interrupt)
            continue

        pane_error = _check_pane_for_errors(runtime.pane)
        if pane_error:
            _daemon_pause_session(runtime, pane_error)
            continue

        send_result = _watch_tmux_send(
            runtime.pane,
            runtime.message,
            max(0.0, float(ENTER_DELAY_SECS)),
            interrupt_checker=lambda: _check_pane_for_interrupt(runtime.pane),
        )
        if send_result.status == "ok":
            runtime.last_send_time = time.time()
            _daemon_mark_handled(runtime, event.turn_id, event.thread_id)
            now = _watch_now_ts()
            _write_session_state(
                runtime.thread_id,
                {
                    "thread_id": runtime.thread_id,
                    "last_continue_at": now,
                    "health": runtime.health,
                    "health_detail": runtime.health_detail,
                },
            )
            _watch_append_log(watch_log, f"continue: sent turn={event.turn_id} thread={event.thread_id}")
            continue
        if send_result.status == "interrupted":
            _daemon_skip_interrupted_turn(runtime, event.turn_id, event.thread_id, send_result.detail)
            continue
        if send_result.detail:
            _watch_append_log(
                watch_log,
                (
                    "error: tmux send failed "
                    f"turn={event.turn_id} thread={event.thread_id} detail={send_result.detail}"
                ),
            )
        else:
            _watch_append_log(
                watch_log,
                f"error: tmux send failed turn={event.turn_id} thread={event.thread_id}",
            )


def _daemon_refresh_session_health(
    runtime: _DaemonSessionRuntime,
    *,
    codex_log_exists: bool,
    now: float,
) -> None:
    new_health, new_detail = _compute_watch_health(
        watched_thread=runtime.thread_id,
        watcher_start=runtime.watcher_start,
        now=now,
        codex_log_exists=codex_log_exists,
    )
    if new_health != runtime.health or new_detail != runtime.health_detail:
        if new_detail:
            _watch_append_log(Path(runtime.watch_log), f"health: {new_health} - {new_detail}")
        else:
            _watch_append_log(Path(runtime.watch_log), f"health: {new_health}")
    runtime.health = new_health
    runtime.health_detail = new_detail
    _write_session_state(
        runtime.thread_id,
        {
            "health": runtime.health,
            "health_detail": runtime.health_detail,
            "health_ts": _watch_now_ts(),
        },
    )


def _daemon_runtime_events(
    runtime: _DaemonSessionRuntime,
    events_by_thread: dict[str, list[_LogwatchCodexLogEvent]],
) -> list[_LogwatchCodexLogEvent]:
    if runtime.paused:
        return []
    combined: list[_LogwatchCodexLogEvent] = []
    pending = runtime.pending_startup_event
    if pending is not None:
        combined.append(pending)
        runtime.pending_startup_event = None
    for event in events_by_thread.get(runtime.thread_id, ()):
        if (
            pending is not None
            and event.kind == "completion"
            and event.thread_id == pending.thread_id
            and event.turn_id == pending.turn_id
        ):
            continue
        combined.append(event)
    return combined


def _daemon_main(argv: list[str]) -> None:
    socket_path = ""
    rest = list(argv)
    while rest:
        tok = rest.pop(0)
        if tok == "--tmux-socket":
            if not rest:
                print("error: --tmux-socket requires a value", file=sys.stderr)
                sys.exit(2)
            socket_path = rest.pop(0)
        else:
            print(f"error: unknown daemon option '{tok}'", file=sys.stderr)
            sys.exit(2)

    scope = _tmux_scope_key(socket_path)
    if scope:
        os.environ["AUTO_CONTINUE_TMUX_SOCKET"] = scope
    else:
        os.environ.pop("AUTO_CONTINUE_TMUX_SOCKET", None)

    pid_file = Path(daemon_pid_file_for_scope(scope))
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    idle_since = 0.0
    log_tail = _DaemonCodexLogTail(log_path=DAEMON_CODEX_LOG_PATH)
    runtimes: dict[str, _DaemonSessionRuntime] = {}

    try:
        while True:
            drained_events = _drain_daemon_codex_log_events(log_tail)
            events_by_thread: dict[str, list[_LogwatchCodexLogEvent]] = defaultdict(list)
            for event in drained_events:
                events_by_thread[event.thread_id.lower()].append(event)

            desired_sessions = {
                session.thread_id: session
                for session in _load_scoped_session_records(scope)
                if session.pane and is_thread_id(session.thread_id)
            }

            for thread_id in sorted(set(runtimes) - set(desired_sessions)):
                _remove_runtime_state(thread_id, scope)
                del runtimes[thread_id]

            for thread_id, desired in desired_sessions.items():
                current = runtimes.get(thread_id)
                if current is None or _session_signature(desired) != (
                    current.pane,
                    current.message,
                    current.generation,
                ):
                    runtimes[thread_id] = _daemon_session_runtime(
                        desired,
                        codex_log_path=DAEMON_CODEX_LOG_PATH,
                    )
                    current = runtimes[thread_id]
                current.paused = desired.paused
                current.pane = desired.pane
                current.message = _message_text(desired.message)
                current.tmux_socket = desired.tmux_socket
                current.generation = desired.generation
                if current.paused:
                    current.runtime_status = "paused"
                elif current.runtime_status != "error":
                    current.runtime_status = "running"
                    current.runtime_detail = ""

            now = time.time()
            for thread_id, runtime in runtimes.items():
                try:
                    _daemon_process_session_events(runtime, _daemon_runtime_events(runtime, events_by_thread))
                    if not runtime.paused and now - runtime.last_health_check > 30.0:
                        runtime.last_health_check = now
                        _daemon_refresh_session_health(
                            runtime,
                            codex_log_exists=DAEMON_CODEX_LOG_PATH.exists(),
                            now=now,
                        )
                except Exception as exc:
                    runtime.runtime_status = "error"
                    runtime.runtime_detail = str(exc)
                    _watch_append_log(
                        Path(runtime.watch_log),
                        f"error: daemon session crashed: {exc}\n{traceback.format_exc()}",
                    )
                _write_runtime_state(thread_id, _daemon_runtime_payload(runtime), scope)

            if not desired_sessions and not runtimes:
                if idle_since == 0.0:
                    idle_since = time.time()
                elif time.time() - idle_since >= DAEMON_IDLE_EXIT_SECS:
                    return
            else:
                idle_since = 0.0

            time.sleep(DAEMON_POLL_INTERVAL_SECS)
    finally:
        log_tail.close()
        for thread_id in list(runtimes):
            _remove_runtime_state(thread_id, scope)
        pid_file.unlink(missing_ok=True)


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
def _validate_existing_pane_watcher(
    pane: str,
    requested_thread: str = "",
    model: RuntimeModel | None = None,
) -> tuple[str, str] | None:
    if model is None:
        model = _build_runtime_model()

    watchers = list(_runtime_watchers_for_pane(model, pane))
    if not watchers:
        return None

    if len(watchers) != 1:
        joined = " ".join(sorted(w.pid or "?" for w in watchers))
        return (
            f"error: multiple watchers already running for pane={pane}: {joined}",
            f"hint: run 'acw stop {pane}' to stop all pane watchers, then start once",
        )

    row = watchers[0]
    thread_id = row.thread_id
    if requested_thread and is_thread_id(thread_id) and thread_id.lower() != requested_thread.lower():
        return (
            f"error: existing watcher for pane={pane} is using thread {thread_id[:8]}…, "
            f"not requested thread {requested_thread[:8]}…",
            f"hint: stop it first with 'acw stop {pane}' if you want to start a different session there",
        )

    anomalies = _watcher_anomalies(model, row)
    other = _other_watcher_for_thread(pane, thread_id)
    if other:
        return (
            f"error: existing watcher for pane={pane} is duplicated; pane={other['pane']} "
            f"pid={other['pid']} is already watching thread {thread_id[:8]}…",
            f"hint: stop the duplicate watchers with 'acw stop {pane}' and 'acw stop {other['pane']}', then start once",
        )
    if anomalies:
        detail = "; ".join(anomalies)
        return (
            f"error: existing watcher for pane={pane} is inconsistent: {detail}",
            f"hint: stop it first with 'acw stop {pane}', then start once after the pane/session identity is clean",
        )
    return None


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
            "[thread-id] [--message TEXT]",
            file=sys.stderr,
        )
        sys.exit(2)

    pane = resolve_start_pane_target(target)
    if pane != target:
        _print_progress(f"resolved: target={target} pane={pane}")
    ensure_tmux_window_rename_hook()

    thread_arg, msg_value, msg_explicit = parse_thread_and_message_args(rest)

    # No --message given: open editor for interactive input.
    if not msg_explicit:
        value = _edit_message_interactive()
        if value is None or not value.strip():
            print("start: no message provided, aborting", file=sys.stderr)
            sys.exit(1)
        msg_value = value

    model = _build_runtime_model()
    existing_rows = [row for row in watcher_rows() if row.get("pane", "") == pane]
    if existing_rows:
        validation = _validate_existing_pane_watcher(pane, thread_arg, model=model)
        if validation:
            print(validation[0], file=sys.stderr)
            print(validation[1], file=sys.stderr)
            sys.exit(1)
        existing = existing_rows[0]
        print(
            f"already running: pane={pane} thread_id={existing.get('thread', '')}",
        )
        return

    if thread_arg and thread_arg != "auto":
        _print_progress(f"start: using explicit thread_id for pane={pane}")
    else:
        _print_progress(f"start: resolving Codex thread for pane={pane}")
    thread_id = resolve_thread_id(pane, thread_arg, model=model)
    _print_progress(f"start: thread_id={thread_id}")

    other = _other_watcher_for_thread(pane, thread_id)
    if other:
        print(
            f"error: another watcher (pid={other['pid']} pane={other['pane']}) "
            f"is already watching thread {thread_id[:8]}…",
            file=sys.stderr,
        )
        print(
            f"hint: stop it first with 'acw stop {other['pane']}' to avoid duplicates",
            file=sys.stderr,
        )
        sys.exit(1)

    other_panes = [row for row in _runtime_panes_for_thread(model, thread_id) if row.pane_id != pane]
    if other_panes:
        panes = ", ".join(sorted({row.pane_id for row in other_panes if row.pane_id}))
        print(
            f"error: another live Codex pane is already using thread {thread_id[:8]}… on {panes}",
            file=sys.stderr,
        )
        print(
            f"hint: clean up the duplicate panes first, then start exactly one watcher for {thread_id[:8]}…",
            file=sys.stderr,
        )
        sys.exit(1)

    message_text = _message_text(msg_value)
    wn = run_tmux("display-message", "-p", "-t", pane, "#{window_name}") or ""
    existing_session = _read_session_state(thread_id)
    _write_managed_session(
        thread_id,
        pane=pane,
        name=wn.strip(),
        message=message_text,
        paused=False,
        generation=_current_session_generation(existing_session),
        tmux_socket=_tmux_scope_key(),
    )

    if (not thread_arg or thread_arg == "auto") and thread_id != "auto":
        _print_progress(f"resolved: pane={pane} thread_id={thread_id}")

    _print_progress(f"start: ensuring shared daemon for pane={pane}")
    daemon_pid = _ensure_daemon_running()
    row = _wait_for_runtime_row(
        thread_id,
        lambda item: bool(item)
        and item.get("pane", "") == pane
        and item.get("runtime_status", "") in {"running", "paused"},
    )
    if not row:
        print(
            f"error: daemon did not start a watcher for pane={pane} thread_id={thread_id}",
            file=sys.stderr,
        )
        print(
            f"hint: inspect {daemon_log_file_for_scope()} for daemon startup details",
            file=sys.stderr,
        )
        sys.exit(1)
    _print_progress(f"started: pane={pane} thread_id={thread_id} daemon_pid={daemon_pid}")


def stop_pane_watchers(pane: str) -> None:
    model = _build_runtime_model()
    session_threads: set[str] = set()
    for watcher in _runtime_watchers_for_pane(model, pane):
        if is_thread_id(watcher.thread_id) and _runtime_session_for_thread(model, watcher.thread_id):
            session_threads.add(watcher.thread_id.lower())
    pane_record = model.panes_by_id.get(pane)
    if pane_record and is_thread_id(pane_record.detected_thread):
        session = _runtime_session_for_thread(model, pane_record.detected_thread)
        if session:
            session_threads.add(session.thread_id.lower())
    rows = watcher_rows(pane)
    managed_threads = {
        row.get("thread", "").lower()
        for row in rows
        if row.get("runtime_status", "")
        and is_thread_id(row.get("thread", ""))
    }

    if not managed_threads and not session_threads:
        print(f"not running: pane={pane}")
        return

    for thread_id in sorted(managed_threads | session_threads):
        _remove_session_state(thread_id)
        _wait_for_runtime_row(thread_id, lambda item: not item, timeout_secs=2.0)
        print(f"stopped: pane={pane} thread_id={thread_id}")


def _stop_pane_watchers_keep_session(pane: str) -> None:
    rows = watcher_rows(pane)
    if not rows:
        print(f"not running: pane={pane}")
        return

    for row in rows:
        thread_id = row.get("thread", "").lower()
        if not row.get("runtime_status", "") or not is_thread_id(thread_id):
            continue
        existing = _read_session_state(thread_id)
        _write_session_state(
            thread_id,
            {
                "pane": "",
                "paused": True,
                "generation": _current_session_generation(existing) + 1,
            },
        )
        _wait_for_runtime_row(thread_id, lambda item: not item, timeout_secs=2.0)
        print(f"stopped: pane={pane} thread_id={thread_id}")


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
    """Stop managed watcher sessions."""
    forgotten_threads: set[str] = set()
    for r in rows:
        thread_id = r.get("thread", "").lower()
        if r.get("runtime_status", "") and is_thread_id(thread_id) and thread_id not in forgotten_threads:
            forgotten_threads.add(thread_id)
            _remove_session_state(thread_id)
            print(f"stopped: pane={r['pane']} thread_id={thread_id}")
            _wait_for_runtime_row(thread_id, lambda item: not item, timeout_secs=2.0)


def cmd_stop(argv: list[str]) -> None:
    target = argv[0] if argv else ""

    if target:
        # Pane ID.
        if is_pane_id(target):
            stop_pane_watchers(target)
            return

        # Match managed runtime rows by saved session name first — this
        # still finds the intended session even if the tmux window name
        # was reused elsewhere.
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
            "hint: use a pane id ('%6'), window name, or window index ('2')",
            file=sys.stderr,
        )
        sys.exit(1)

    had_any = False
    for r in watcher_rows():
        had_any = True
        _stop_watcher_rows([r])

    if not had_any:
        print("not running")
    forgotten = 0
    for session in _load_session_records():
        if _remove_session_state(session.thread_id):
            forgotten += 1
            print(f"forgot session: thread_id={session.thread_id}")
    if not had_any and forgotten == 0:
        return


def cmd_pause(argv: list[str]) -> None:
    """Pause watcher(s) without forgetting their saved sessions."""
    if argv and argv[0] == "*":
        print("error: '*' is no longer supported for pause; use 'acw pause' with no target", file=sys.stderr)
        sys.exit(2)
    pane_arg = argv[0] if argv else ""

    if pane_arg:
        pane = resolve_pane_target(pane_arg)
        rows = watcher_rows(pane)
        if not rows:
            print(f"not running: pane={pane}")
            return
        for row in rows:
            thread_id = row.get("thread", "").lower()
            if not row.get("runtime_status", "") or not is_thread_id(thread_id):
                continue
            if row.get("paused", "") == "1":
                print(f"already paused: pane={pane} thread_id={thread_id}")
                continue
            _set_session_paused(thread_id, True)
            _wait_for_runtime_row(
                thread_id,
                lambda item: bool(item) and item.get("paused", "") == "1",
                timeout_secs=2.0,
            )
            print(f"paused: pane={pane} thread_id={thread_id}")
        return

    had_any = False
    for r in watcher_rows():
        had_any = True
        thread_id = r.get("thread", "").lower()
        if not r.get("runtime_status", "") or not is_thread_id(thread_id):
            continue
        if r.get("paused", "") == "1":
            print(f"already paused: pane={r['pane']} thread_id={thread_id}")
            continue
        _set_session_paused(thread_id, True)
        _wait_for_runtime_row(
            thread_id,
            lambda item: bool(item) and item.get("paused", "") == "1",
            timeout_secs=2.0,
        )
        print(f"paused: pane={r['pane']} thread_id={thread_id}")

    if not had_any:
        print("not running")


def cmd_resume(argv: list[str]) -> None:
    """Resume paused watcher(s) by restarting from current state."""
    if "*" in argv:
        print("error: '*' is no longer supported for resume; use 'acw resume' with no target", file=sys.stderr)
        sys.exit(2)

    if argv:
        seen_panes: set[str] = set()
        for pane_arg in argv:
            pane = resolve_pane_target(pane_arg)
            if pane in seen_panes:
                continue
            seen_panes.add(pane)
            rows = watcher_rows(pane)
            if not rows:
                print(f"not running: pane={pane}")
                continue
            for row in rows:
                thread_id = row.get("thread", "").lower()
                if not row.get("runtime_status", "") or not is_thread_id(thread_id):
                    continue
                if row.get("paused", "") != "1":
                    print(f"not paused: pane={pane} thread_id={thread_id}")
                    continue
                print(f"resuming paused watcher via restart: pane={pane} thread_id={thread_id}")
                _restart_one(pane_arg, pane, [])
                break
        return

    paused_panes: list[str] = []
    for r in watcher_rows():
        thread_id = r.get("thread", "").lower()
        if r.get("runtime_status", "") and is_thread_id(thread_id) and r.get("paused", "") == "1" and r["pane"]:
            paused_panes.append(r["pane"])

    if not paused_panes:
        print("no paused watchers")
        return

    print(f"resuming {len(paused_panes)} paused watcher(s) via restart")
    _restart_panes(paused_panes)


def _restart_panes(panes: list[str]) -> None:
    restarted = 0
    failed = 0
    failed_details: list[str] = []
    for pane in panes:
        if run_tmux("display-message", "-p", "-t", pane, "#{pane_id}") is None:
            stop_pane_watchers(pane)
            continue

        _print_progress(f"--- restarting {pane} ---")
        captured_err = io.StringIO()
        printed_err = False
        try:
            old_stderr = sys.stderr
            sys.stderr = captured_err
            _restart_one(pane, pane, [])
        except SystemExit as exc:
            failed += 1
            detail_text = captured_err.getvalue()
            if detail_text:
                print(detail_text, file=old_stderr, end="" if detail_text.endswith("\n") else "\n")
                printed_err = True
            detail = _system_exit_detail(exc, detail_text)
            failed_details.append(f"{pane}: {detail}")
            print(f"error: restart failed for pane={pane}: {detail}", file=old_stderr)
            continue
        finally:
            sys.stderr = old_stderr
            detail_text = captured_err.getvalue()
            if detail_text and not printed_err:
                print(detail_text, file=old_stderr, end="" if detail_text.endswith("\n") else "\n")
        restarted += 1

    if restarted == 0:
        print("no live panes to restart")
    elif failed:
        print(f"restart: {failed} pane(s) failed")
        for detail in failed_details:
            print(f"restart: failure detail: {detail}")


def _accept_directory_trust_prompt_in_pane(pane: str, timeout_secs: float = 2.0) -> bool:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        text = run_tmux("capture-pane", "-t", pane, "-p", "-S", "-80") or ""
        if "Do you trust the contents of this directory?" in text:
            if run_tmux("send-keys", "-t", pane, "C-m") is None:
                print(f"error: tmux send-keys failed for pane={pane}", file=sys.stderr)
                sys.exit(1)
            return True
        time.sleep(0.5)
    return False


def _command_name_for_pid(pid: str) -> str:
    if not pid.isdigit():
        return ""
    proc_comm = Path("/proc") / pid / "comm"
    try:
        return proc_comm.read_text().strip().lower()
    except OSError:
        pass
    try:
        return subprocess.check_output(
            ["ps", "-p", pid, "-o", "comm="],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip().lower()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _pane_command_snapshot(pane: str) -> tuple[str, str, str, str]:
    listing = run_tmux("list-panes", "-t", pane, "-F", "#{pane_current_command}\t#{pane_pid}")
    if listing is None:
        return "", "", "", f"tmux list-panes failed for pane={pane}"
    line = next((entry for entry in listing.splitlines() if entry.strip()), "")
    if not line:
        return "", "", "", f"tmux pane {pane} is missing"
    parts = line.split("\t")
    current_command = parts[0].strip().lower() if parts else ""
    pane_pid = parts[1].strip() if len(parts) > 1 else ""
    if not pane_pid.isdigit():
        return current_command, "", "", f"tmux pane {pane} did not report a valid pane pid"
    shell_command = _command_name_for_pid(pane_pid)
    if not shell_command:
        return current_command, "", pane_pid, f"could not determine root shell command for pane={pane}"
    return current_command, shell_command, pane_pid, ""


def _wait_for_shell_prompt(
    pane: str,
    *,
    attempts: int = 30,
    poll_interval_secs: float = 0.1,
) -> tuple[bool, str]:
    last_current = ""
    last_shell = ""
    for index in range(max(1, attempts)):
        current_command, shell_command, _pane_pid, error = _pane_command_snapshot(pane)
        if error:
            return False, error
        last_current = current_command
        last_shell = shell_command
        if shell_command not in KNOWN_SHELL_COMMANDS:
            return False, f"pane {pane} root command is '{shell_command}', not a supported shell"
        if current_command == shell_command:
            return True, ""
        if index + 1 < attempts:
            time.sleep(poll_interval_secs)
    current_label = last_current or "unknown"
    shell_label = last_shell or "shell"
    return (
        False,
        f"pane {pane} did not return to a shell prompt (current command '{current_label}', expected '{shell_label}')",
    )


def _wait_for_codex_process(
    pane: str,
    *,
    attempts: int = 30,
    poll_interval_secs: float = 0.1,
) -> tuple[bool, str]:
    for index in range(max(1, attempts)):
        _current_command, _shell_command, pane_pid, error = _pane_command_snapshot(pane)
        if error:
            return False, error
        if _codex_pids_in_process_tree(pane_pid):
            return True, ""
        if index + 1 < attempts:
            time.sleep(poll_interval_secs)
    return False, f"Codex did not relaunch in pane {pane}"


def _terminate_codex_processes_in_pane(pane: str) -> tuple[bool, str]:
    current_command, shell_command, pane_pid, error = _pane_command_snapshot(pane)
    if error:
        return False, error
    if shell_command in KNOWN_SHELL_COMMANDS and current_command == shell_command:
        return True, ""

    top_level_pids = _codex_top_level_descendants(pane_pid)
    if not top_level_pids:
        current_label = current_command or "unknown"
        return False, f"pane {pane} is running '{current_label}', but no live Codex process was found to restart"

    for pid in top_level_pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError as exc:
            return False, f"failed to terminate Codex pid={pid} in pane {pane}: {exc}"
    return True, ""


def _restart_codex_in_pane(pane: str, thread_id: str) -> None:
    at_shell, shell_error = _wait_for_shell_prompt(pane, attempts=1)
    if not at_shell:
        _print_progress(f"restarting codex: terminating live Codex process in pane={pane}")
        terminated, terminate_error = _terminate_codex_processes_in_pane(pane)
        if not terminated:
            print(f"error: {terminate_error}", file=sys.stderr)
            sys.exit(1)
        _print_progress(f"restarting codex: waiting for shell prompt in pane={pane}")
        at_shell, shell_error = _wait_for_shell_prompt(pane)
        if not at_shell:
            print(f"error: {shell_error}", file=sys.stderr)
            sys.exit(1)
    _print_progress(f"restarting codex: launching codex resume in pane={pane}")
    if run_tmux("send-keys", "-t", pane, "-l", f"codex resume {thread_id}") is None:
        print(f"error: tmux send-keys failed for pane={pane}", file=sys.stderr)
        sys.exit(1)
    if run_tmux("send-keys", "-t", pane, "C-m") is None:
        print(f"error: tmux send-keys failed for pane={pane}", file=sys.stderr)
        sys.exit(1)
    _print_progress(f"restarting codex: waiting for Codex process in pane={pane}")
    relaunched, relaunch_error = _wait_for_codex_process(pane)
    if not relaunched:
        print(f"error: {relaunch_error}", file=sys.stderr)
        sys.exit(1)
    _print_progress(f"restarting codex: checking directory trust prompt in pane={pane}")
    _accept_directory_trust_prompt_in_pane(pane, timeout_secs=2.0)


def _restart_one(target: str, pane: str, rest: list[str]) -> None:
    if pane != target:
        _print_progress(f"resolved: target={target} pane={pane}")
    _print_progress(f"restart: preparing pane={pane}")

    thread_arg, msg_value, msg_explicit, restart_codex = parse_restart_args(rest)
    model = _build_runtime_model()

    resolved_thread = ""
    if thread_arg and thread_arg != "auto":
        if restart_codex and not is_thread_id(thread_arg):
            print(f"error: invalid thread_id '{thread_arg}'", file=sys.stderr)
            sys.exit(1)
        if is_thread_id(thread_arg):
            resolved_thread = thread_arg.lower()
        pane_record = model.panes_by_id.get(pane)
        live_thread = pane_record.detected_thread.lower() if pane_record and is_thread_id(pane_record.detected_thread) else ""
        if live_thread and resolved_thread and live_thread != resolved_thread and not restart_codex:
            print(
                f"error: pane {pane} is running thread {_short_thread_id(live_thread)}, "
                f"not requested thread {_short_thread_id(resolved_thread)}",
                file=sys.stderr,
            )
            print(
                "hint: use --restart-codex to relaunch Codex into that thread, or restart against the live pane thread",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        _print_progress(f"restart: resolving Codex thread for pane={pane}")
        resolved_thread, thread_error = _strict_thread_for_pane(model, pane)
        if thread_error:
            print(f"error: {thread_error}", file=sys.stderr)
            print(
                f"hint: clean up pane {pane} so exactly one live pane, one watcher, and one session agree on the thread",
                file=sys.stderr,
            )
            sys.exit(1)
    if resolved_thread:
        _print_progress(f"restart: thread_id={resolved_thread}")
    else:
        _print_progress(f"restart: thread_id unresolved for pane={pane}")

    if not msg_explicit:
        _print_progress(f"restart: loading saved continue message for pane={pane}")
        if resolved_thread and is_thread_id(resolved_thread):
            session, session_error = _strict_session_for_thread(model, resolved_thread)
            if session_error:
                print(f"error: {session_error}", file=sys.stderr)
                print(
                    f"hint: repair the saved session for thread {_short_thread_id(resolved_thread)} before restarting without --message",
                    file=sys.stderr,
                )
                sys.exit(1)
            if session and session.message:
                msg_value = session.message

    if not thread_arg:
        thread_arg = resolved_thread

    if restart_codex:
        if not resolved_thread or not is_thread_id(resolved_thread):
            print(
                f"error: could not determine thread_id for pane={pane}; "
                "--restart-codex requires a concrete thread id",
                file=sys.stderr,
            )
            sys.exit(1)

    existing_session = _read_session_state(resolved_thread) if is_thread_id(resolved_thread) else {}
    next_generation = _current_session_generation(existing_session) + 1

    if restart_codex and is_thread_id(resolved_thread):
        _write_session_state(
            resolved_thread,
            {
                "paused": True,
                "generation": _current_session_generation(existing_session),
            },
        )
        _wait_for_runtime_row(
            resolved_thread,
            lambda item: bool(item) and item.get("paused", "") == "1",
            timeout_secs=2.0,
        )

    if restart_codex:
        _print_progress(f"restarting codex: pane={pane} thread_id={resolved_thread}")
        _restart_codex_in_pane(pane, resolved_thread)

    _print_progress(f"restart: starting watcher for pane={pane}")
    window_name = (run_tmux("display-message", "-p", "-t", pane, "#{window_name}") or "").strip()
    _write_managed_session(
        resolved_thread,
        pane=pane,
        name=window_name,
        message=msg_value,
        paused=False,
        generation=next_generation,
        tmux_socket=_tmux_scope_key(),
    )
    daemon_pid = _ensure_daemon_running()
    row = _wait_for_runtime_row(
        resolved_thread,
        lambda item: bool(item)
        and item.get("pane", "") == pane
        and item.get("generation", "") == str(next_generation)
        and item.get("runtime_status", "") in {"running", "paused"},
    )
    if not row:
        print(
            f"error: daemon did not restart pane={pane} thread_id={resolved_thread}",
            file=sys.stderr,
        )
        print(
            f"hint: inspect {daemon_log_file_for_scope()} for daemon restart details",
            file=sys.stderr,
        )
        sys.exit(1)
    _print_progress(f"started: pane={pane} thread_id={resolved_thread} daemon_pid={daemon_pid}")


def cmd_restart(argv: list[str]) -> None:
    rest = list(argv)
    if rest and not rest[0].startswith("--"):
        target = rest.pop(0)
    else:
        target = ""
    if target == "*":
        print("error: '*' is no longer supported for restart; use 'acw restart' with no target", file=sys.stderr)
        sys.exit(2)
    if not target:
        if rest:
            print("error: restart does not accept additional arguments", file=sys.stderr)
            sys.exit(2)
        model = _build_runtime_model()
        panes = {pane.pane_id for pane in model.snapshot.panes if pane.pane_id and is_thread_id(pane.detected_thread)}
        panes.update({watcher.pane for watcher in model.snapshot.watchers if watcher.pane})
        panes = sorted(panes)
        if not panes:
            print("no live panes to restart")
            return
        _restart_panes(panes)
        return

    model = _build_runtime_model()
    pane = _resolve_restart_pane_target(target, model=model)
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

    model = _build_runtime_model()
    tid, thread_error = _strict_thread_for_pane(model, pane)
    if thread_error:
        print(f"error: {thread_error}", file=sys.stderr)
        print(
            f"hint: clean up pane {pane} so exactly one live pane, one watcher, and one saved session agree before editing",
            file=sys.stderr,
        )
        sys.exit(1)

    session, session_error = _strict_session_for_thread(model, tid)
    if session_error:
        print(f"error: {session_error}", file=sys.stderr)
        print(
            f"hint: repair the saved session for thread {_short_thread_id(tid)} before editing its message",
            file=sys.stderr,
        )
        sys.exit(1)

    value = session.message if session else ""

    new_value = _edit_message_interactive(value)
    if new_value is None:
        return

    if new_value == value:
        print("edit: message unchanged")
        return

    if tid and is_thread_id(tid):
        _write_session_state(tid, {"message": new_value})
    print(f"edit: message updated for pane {pane}")
    if _runtime_watchers_for_pane(model, pane):
        cmd_restart([pane, "--message", new_value])


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def _parse_repair_args(args: list[str]) -> tuple[str, bool, bool]:
    target = ""
    dry_run = False
    all_targets = False

    rest = list(args)
    if rest and not rest[0].startswith("--"):
        target = rest.pop(0)

    while rest:
        tok = rest.pop(0)
        if tok == "--dry-run":
            dry_run = True
        elif tok == "--all":
            all_targets = True
        else:
            print(f"error: unknown option '{tok}'", file=sys.stderr)
            sys.exit(2)

    if target and all_targets:
        print("error: repair accepts either a target or --all, not both", file=sys.stderr)
        sys.exit(2)

    if not target and not all_targets:
        dry_run = True

    return target, dry_run, all_targets


def _repair_resolve_target(target: str, model: RuntimeModel | None = None) -> tuple[str, str]:
    if model is None:
        model = _build_runtime_model()
    pane, error = _doctor_resolve_target(target, model=model)
    if pane or not target:
        return pane, error

    if is_thread_id(target):
        session = _runtime_session_for_thread(model, target)
        if session:
            return "", (
                f"saved session for thread {_short_thread_id(target)} has no live pane; "
                "repair only handles live state, so use 'acw cleanup' or restart Codex in the intended pane first"
            )
    elif model.sessions_by_name.get(target, ()):
        return "", (
            f"saved session target '{target}' has no live pane; repair only handles live state, "
            "so use 'acw cleanup' or restart Codex in the intended pane first"
        )
    return "", error


def _print_repair_plan(steps: list[RepairStep], *, dry_run: bool) -> None:
    header = "repair: dry-run plan" if dry_run else "repair: plan"
    print(header)
    for step in steps:
        prefix = "AUTO" if step.auto_apply else "MANUAL"
        print(f"{prefix}: {step.summary}")


def _apply_repair_step(step: RepairStep) -> None:
    if step.kind == "restart_watcher":
        _print_progress(f"repair: restarting watcher on pane={step.target}")
        _stop_pane_watchers_keep_session(step.target)
        cmd_start([step.target, step.thread_id, "--message", step.message])
        return

    if step.kind == "start_watcher":
        _print_progress(f"repair: starting watcher on pane={step.target}")
        cmd_start([step.target, step.thread_id, "--message", step.message])
        return

    if step.kind == "stop_watchers_keep_session":
        _print_progress(f"repair: stopping watcher entries on pane={step.target} while keeping saved session state")
        _stop_pane_watchers_keep_session(step.target)
        return

    if step.kind == "recreate_session":
        _print_progress(f"repair: recreating saved session state for pane={step.target}")
        window_name = (run_tmux("display-message", "-p", "-t", step.target, "#{window_name}") or "").strip()
        _write_session_state(
            step.thread_id,
            {
                "thread_id": step.thread_id,
                "name": window_name,
                "message": _message_text(step.message),
            },
        )
        return

    if step.kind == "sync_session_name":
        _print_progress(f"repair: syncing saved session name for pane={step.target}")
        window_name = (run_tmux("display-message", "-p", "-t", step.target, "#{window_name}") or "").strip()
        _write_session_state(step.thread_id, {"thread_id": step.thread_id, "name": window_name})
        return

    raise RuntimeError(f"unsupported repair step: {step.kind}")


def cmd_repair(argv: list[str]) -> None:
    target, dry_run, all_targets = _parse_repair_args(argv)
    model = _build_runtime_model()

    target_panes: tuple[str, ...] | None = None
    if target:
        pane, error = _repair_resolve_target(target, model=model)
        if error:
            print(f"error: {error}", file=sys.stderr)
            sys.exit(1)
        target_panes = (pane,)

    steps = _build_repair_plan(model, target_panes=target_panes)
    if not steps:
        print("repair: no live invariant issues found")
        return

    auto_steps = [step for step in steps if step.auto_apply]
    manual_steps = [step for step in steps if not step.auto_apply]

    if dry_run:
        _print_repair_plan(steps, dry_run=True)
        return

    if target and manual_steps:
        for step in manual_steps:
            print(f"error: {step.summary}", file=sys.stderr)
        print(
            f"hint: run 'acw repair {shlex.quote(target)} --dry-run' to inspect the blocked repair plan",
            file=sys.stderr,
        )
        sys.exit(1)

    if not auto_steps:
        print("repair: no safe automatic repairs available")
        for step in manual_steps:
            print(f"MANUAL: {step.summary}")
        if manual_steps:
            sys.exit(1)
        return

    _print_progress(f"repair: applying {len(auto_steps)} step(s)")
    for step in auto_steps:
        _apply_repair_step(step)
    print(f"repair: applied {len(auto_steps)} step(s)")

    if manual_steps:
        print(f"repair: {len(manual_steps)} issue(s) still require manual action")
        for step in manual_steps:
            print(f"MANUAL: {step.summary}")
        sys.exit(1)


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


def _load_process_graph() -> tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str]]:
    """Return a process-tree snapshot as ``(children_by_parent, command_by_pid, parent_by_pid)``."""
    try:
        output = subprocess.check_output(
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

    return _freeze_multimap(children_by_parent), command_by_pid, parent_by_pid  # type: ignore[arg-type]


def _load_process_snapshot() -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """Return a process-tree snapshot as ``(children_by_parent, command_by_pid)``."""
    children_by_parent, command_by_pid, _parent_by_pid = _load_process_graph()
    return children_by_parent, command_by_pid


def _codex_pids_in_process_tree(
    shell_pid: str,
    process_snapshot: tuple[dict[str, tuple[str, ...]], dict[str, str]] | None = None,
) -> list[str]:
    """Return Codex process ids found below ``shell_pid`` in one process snapshot."""
    if not shell_pid.isdigit():
        return []
    if process_snapshot is None:
        process_snapshot = _load_process_snapshot()

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


def _threads_from_process_tree(
    shell_pid: str,
    process_snapshot: tuple[dict[str, tuple[str, ...]], dict[str, str]] | None = None,
) -> list[str]:
    """Walk a process tree and return thread_ids from any Codex processes found."""
    if process_snapshot is None:
        process_snapshot = _load_process_snapshot()
    threads: list[str] = []
    for pid in _codex_pids_in_process_tree(shell_pid, process_snapshot):
        tid = _thread_from_codex_pid(pid)
        if tid:
            threads.append(tid)
            break  # one codex per tree is enough
    return threads


def _codex_top_level_descendants(
    shell_pid: str,
    process_graph: tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str]] | None = None,
) -> list[str]:
    if not shell_pid.isdigit():
        return []
    if process_graph is None:
        process_graph = _load_process_graph()
    children_by_parent, command_by_pid, parent_by_pid = process_graph
    codex_pids = _codex_pids_in_process_tree(shell_pid, (children_by_parent, command_by_pid))
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


def _pane_thread_probe(
    pane_id: str,
    shell_pid: str,
    process_snapshot: tuple[dict[str, tuple[str, ...]], dict[str, str]] | None = None,
) -> tuple[str, str, str, bool]:
    """Return ``(thread_id, source, detail, has_codex_process)`` for a pane."""
    if not shell_pid.isdigit():
        return "", "", "", False

    # Prefer direct pid->thread mapping; this is strong evidence.
    tids = _threads_from_process_tree(shell_pid, process_snapshot)
    if tids:
        return tids[0].lower(), "pid", "", True

    codex_pids = _codex_pids_in_process_tree(shell_pid, process_snapshot)
    if not codex_pids:
        return "", "", "", False
    return "", "", f"could not determine thread_id for pane={pane_id} from the live Codex process", True


def _thread_provenance_text(source: str) -> str:
    if not source:
        return ""
    if source == "pid":
        return "live Codex process-owned evidence"
    return source.replace("_", " ")


def _build_thread_pane_map() -> dict[str, str]:
    """Map thread_id -> current tmux pane_id by scanning pane process trees."""
    listing = run_tmux("list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}")
    mapping: dict[str, str] = {}
    process_snapshot = _load_process_snapshot()
    if listing:
        for line in listing.splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 2:
                continue
            pane_id, shell_pid = parts[0], parts[1]
            if not pane_id or not shell_pid.isdigit():
                continue
            tids = _threads_from_process_tree(shell_pid, process_snapshot)
            for tid in tids:
                mapping[tid] = pane_id
    return mapping


def _thread_times(thread_id: str, compact: bool = False) -> tuple[str, str]:
    """Return (started, last_message) for a thread from local Codex SQLite state."""
    started_at, last_activity_at = thread_times_from_state_db(thread_id)
    started = _format_age(started_at, compact=compact) if started_at else "-"
    last_msg = _format_age(last_activity_at, compact=compact) if last_activity_at else "-"
    return started, last_msg


def _thread_title(thread_id: str) -> str:
    """Return a stable thread summary from local Codex SQLite state."""
    if not is_thread_id(thread_id):
        return ""
    state_dir = Path(STATE_DIR)
    if not state_dir.is_dir():
        return ""
    for db_path in sorted(state_dir.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True):
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


def _format_age(epoch: float, compact: bool = False) -> str:
    """Format an epoch as a human-friendly relative age string."""
    delta = time.time() - epoch
    if delta < 0:
        delta = 0
    suffix = "" if compact else " ago"
    if delta < 60:
        return f"{int(delta)}s{suffix}"
    if delta < 3600:
        return f"{int(delta // 60)}m{suffix}"
    if delta < 86400:
        h = int(delta // 3600)
        m = int((delta % 3600) // 60)
        return f"{h}h{m:02d}m{suffix}"
    d = int(delta // 86400)
    h = int((delta % 86400) // 3600)
    return f"{d}d{h}h{suffix}"


_PANE_CHROME_ONLY_RE = re.compile(r"^[\s─━╭╮╰╯│┌┐└┘═]+$")
_SHELL_PROMPT_RE = re.compile(r"^\[[^\]]+\]\$\s*$")


def _clean_pane_line_candidate(line: str) -> tuple[str, bool]:
    """Return ``(candidate, preferred)`` for a pane line, or ``('', False)``."""
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


def _extract_last_agent_snippet(text: str) -> str:
    """Extract a compact recent assistant-visible snippet from captured pane text."""
    if not text:
        return ""
    lines = text.splitlines()
    cutoff = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("› "):
            cutoff = i
            break
    search = lines[:cutoff] if cutoff < len(lines) else lines
    fallback = ""
    for line in reversed(search):
        candidate, preferred = _clean_pane_line_candidate(line)
        if not candidate:
            continue
        if preferred:
            return candidate
        if not fallback:
            fallback = candidate
    return fallback


def _last_agent_snippet_for_pane(pane: str, thread_id: str = "") -> str:
    """Return a recent assistant snippet for a live pane, with thread-title fallback."""
    if pane:
        text = run_tmux("capture-pane", "-t", pane, "-p", "-S", "-40") or ""
        snippet = _extract_last_agent_snippet(text)
        if snippet:
            return snippet
    return _thread_title(thread_id)


@dataclass(frozen=True)
class SavedSessionRecord:
    thread_id: str
    name: str
    message: str
    pane: str = ""
    tmux_socket: str = ""
    paused: bool = False
    generation: int = 0
    state_file: str = ""
    health: str = ""
    health_detail: str = ""
    health_ts: str = ""
    last_continue_at: str = ""


@dataclass(frozen=True)
class LiveWatcherRecord:
    pid: str
    pane: str
    thread_id: str
    state_file: str
    watch_log: str
    cwd: str
    message_inline: str
    tmux_socket: str
    paused: bool = False
    runtime_status: str = ""
    runtime_detail: str = ""
    generation: int = 0
    worker_label: str = ""


@dataclass(frozen=True)
class LivePaneRecord:
    pane_id: str
    window_label: str
    window_name: str
    shell_pid: str
    detected_thread: str
    detected_thread_source: str
    detected_thread_detail: str
    has_codex_process: bool


@dataclass(frozen=True)
class RuntimeSnapshot:
    panes: tuple[LivePaneRecord, ...]
    watchers: tuple[LiveWatcherRecord, ...]
    sessions: tuple[SavedSessionRecord, ...]


@dataclass(frozen=True)
class RuntimeModel:
    snapshot: RuntimeSnapshot
    panes_by_id: dict[str, LivePaneRecord]
    panes_by_thread: dict[str, tuple[LivePaneRecord, ...]]
    watchers_by_pane: dict[str, tuple[LiveWatcherRecord, ...]]
    watchers_by_thread: dict[str, tuple[LiveWatcherRecord, ...]]
    sessions_by_thread: dict[str, SavedSessionRecord]
    sessions_by_name: dict[str, tuple[SavedSessionRecord, ...]]


def _load_session_records() -> list[SavedSessionRecord]:
    records: list[SavedSessionRecord] = []
    for session in _load_sessions():
        records.append(
            SavedSessionRecord(
                thread_id=session["thread_id"].lower(),
                name=session.get("name", ""),
                message=session.get("message", ""),
                pane=session.get("pane", ""),
                tmux_socket=_tmux_scope_key(session.get("tmux_socket", "")),
                paused=bool(session.get("paused", False)),
                generation=int(session.get("generation", 0) or 0),
                state_file=session.get("state_file", ""),
                health=session.get("health", ""),
                health_detail=session.get("health_detail", ""),
                health_ts=session.get("health_ts", ""),
                last_continue_at=session.get("last_continue_at", ""),
            )
        )
    return records


def _load_live_watcher_records() -> list[LiveWatcherRecord]:
    records: list[LiveWatcherRecord] = []
    for row in watcher_rows():
        thread_id = row.get("thread", "")
        records.append(
            LiveWatcherRecord(
                pid=row.get("pid", ""),
                pane=row.get("pane", ""),
                thread_id=thread_id.lower() if is_thread_id(thread_id) else thread_id,
                state_file=row.get("state", ""),
                watch_log=row.get("watch", ""),
                cwd=row.get("cwd", ""),
                message_inline=row.get("msg_inline", ""),
                tmux_socket=row.get("tmux_socket", ""),
                paused=row.get("paused", "") == "1",
                runtime_status=row.get("runtime_status", ""),
                runtime_detail=row.get("runtime_detail", ""),
                generation=int(row.get("generation", "0") or 0),
                worker_label=row.get("worker_label", ""),
            )
        )
    return records


def _list_live_pane_records() -> list[LivePaneRecord]:
    listing = run_tmux("list-panes", "-a", "-F", "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_pid}")
    records: list[LivePaneRecord] = []
    if not listing:
        return records

    process_snapshot = _load_process_snapshot()
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        session_name, window_index, window_name, pane_id, shell_pid = parts[:5]
        if not is_pane_id(pane_id):
            continue

        detected_thread, detected_thread_source, detected_thread_detail, has_codex_process = _pane_thread_probe(
            pane_id,
            shell_pid,
            process_snapshot,
        )
        if not is_thread_id(detected_thread):
            detected_thread = ""
            if not detected_thread_source:
                detected_thread_detail = ""

        records.append(
            LivePaneRecord(
                pane_id=pane_id,
                window_label=f"{session_name}:{window_index}:{window_name}",
                window_name=window_name,
                shell_pid=shell_pid,
                detected_thread=detected_thread,
                detected_thread_source=detected_thread_source,
                detected_thread_detail=detected_thread_detail,
                has_codex_process=has_codex_process,
            )
        )

    return records


def _build_runtime_snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        panes=tuple(_list_live_pane_records()),
        watchers=tuple(_load_live_watcher_records()),
        sessions=tuple(_load_session_records()),
    )


def _freeze_multimap(mapping: dict[str, list[object]]) -> dict[str, tuple[object, ...]]:
    return {key: tuple(values) for key, values in mapping.items()}


def _build_runtime_model(snapshot: RuntimeSnapshot | None = None) -> RuntimeModel:
    if snapshot is None:
        snapshot = _build_runtime_snapshot()

    panes_by_id: dict[str, LivePaneRecord] = {}
    panes_by_thread: dict[str, list[LivePaneRecord]] = defaultdict(list)
    watchers_by_pane: dict[str, list[LiveWatcherRecord]] = defaultdict(list)
    watchers_by_thread: dict[str, list[LiveWatcherRecord]] = defaultdict(list)
    sessions_by_thread: dict[str, SavedSessionRecord] = {}
    sessions_by_name: dict[str, list[SavedSessionRecord]] = defaultdict(list)

    for pane in snapshot.panes:
        if pane.pane_id:
            panes_by_id[pane.pane_id] = pane
        if is_thread_id(pane.detected_thread):
            panes_by_thread[pane.detected_thread].append(pane)

    for watcher in snapshot.watchers:
        if watcher.pane:
            watchers_by_pane[watcher.pane].append(watcher)
        if is_thread_id(watcher.thread_id):
            watchers_by_thread[watcher.thread_id].append(watcher)

    for session in snapshot.sessions:
        if is_thread_id(session.thread_id):
            sessions_by_thread[session.thread_id] = session
        if session.name:
            sessions_by_name[session.name].append(session)

    return RuntimeModel(
        snapshot=snapshot,
        panes_by_id=panes_by_id,
        panes_by_thread=_freeze_multimap(panes_by_thread),  # type: ignore[arg-type]
        watchers_by_pane=_freeze_multimap(watchers_by_pane),  # type: ignore[arg-type]
        watchers_by_thread=_freeze_multimap(watchers_by_thread),  # type: ignore[arg-type]
        sessions_by_thread=sessions_by_thread,
        sessions_by_name=_freeze_multimap(sessions_by_name),  # type: ignore[arg-type]
    )


def _runtime_watchers_for_pane(model: RuntimeModel, pane: str) -> tuple[LiveWatcherRecord, ...]:
    return model.watchers_by_pane.get(pane, ())


def _runtime_watchers_for_thread(model: RuntimeModel, thread_id: str) -> tuple[LiveWatcherRecord, ...]:
    return model.watchers_by_thread.get(thread_id.lower(), ())


def _runtime_panes_for_thread(model: RuntimeModel, thread_id: str) -> tuple[LivePaneRecord, ...]:
    return model.panes_by_thread.get(thread_id.lower(), ())


def _runtime_session_for_thread(model: RuntimeModel, thread_id: str) -> SavedSessionRecord | None:
    return model.sessions_by_thread.get(thread_id.lower())


def _runtime_session_name_conflict(model: RuntimeModel, session: SavedSessionRecord | None) -> str:
    if not session or not session.name:
        return ""
    named = model.sessions_by_name.get(session.name, ())
    if len(named) <= 1:
        return ""
    threads = ", ".join(_short_thread_id(item.thread_id) for item in named)
    return f"agent name '{session.name}' is shared by threads {threads}"


def _strict_thread_for_pane(model: RuntimeModel, pane: str) -> tuple[str, str]:
    watchers = list(_runtime_watchers_for_pane(model, pane))
    pane_record = model.panes_by_id.get(pane)

    if len(watchers) > 1:
        pids = ", ".join(sorted(w.pid or "?" for w in watchers))
        return "", f"pane {pane} has {len(watchers)} watcher entries ({pids})"

    if not pane_record:
        return "", f"tmux pane {pane} is missing"

    if not is_thread_id(pane_record.detected_thread):
        if pane_record.has_codex_process:
            return "", pane_record.detected_thread_detail or f"could not determine thread_id for pane={pane} from the live Codex process"
        return "", f"no live Codex agent detected in pane {pane}"

    live_thread = pane_record.detected_thread.lower()

    watcher_thread = ""
    if watchers:
        if not is_thread_id(watchers[0].thread_id):
            pid = watchers[0].pid or "?"
            return "", f"pane {pane} watcher entry pid={pid} has an invalid thread id"
        watcher_thread = watchers[0].thread_id.lower()

    if watcher_thread and watcher_thread != live_thread:
        return (
            "",
            f"pane {pane} live Codex thread {_short_thread_id(live_thread)} "
            f"does not match watcher thread {_short_thread_id(watcher_thread)}",
        )

    other_watchers = [row for row in _runtime_watchers_for_thread(model, live_thread) if row.pane != pane]
    if other_watchers:
        panes = ", ".join(sorted({row.pane for row in other_watchers if row.pane}))
        return "", f"thread {_short_thread_id(live_thread)} is already watched on panes {panes}"

    other_panes = [row for row in _runtime_panes_for_thread(model, live_thread) if row.pane_id != pane]
    if other_panes:
        panes = ", ".join(sorted({row.pane_id for row in other_panes if row.pane_id}))
        return "", f"thread {_short_thread_id(live_thread)} is active in panes {panes}"

    return live_thread, ""


def _strict_session_for_thread(model: RuntimeModel, thread_id: str) -> tuple[SavedSessionRecord | None, str]:
    session = _runtime_session_for_thread(model, thread_id)
    if not session:
        return None, f"no saved session for thread {_short_thread_id(thread_id)}"
    conflict = _runtime_session_name_conflict(model, session)
    if conflict:
        return None, conflict
    return session, ""


def _watcher_anomalies(model: RuntimeModel, watcher: LiveWatcherRecord) -> list[str]:
    pane = model.panes_by_id.get(watcher.pane)
    session = _runtime_session_for_thread(model, watcher.thread_id)
    anomalies: list[str] = []

    if watcher.pane and len(_runtime_watchers_for_pane(model, watcher.pane)) != 1:
        watcher_count = len(_runtime_watchers_for_pane(model, watcher.pane))
        anomalies.append(f"{watcher_count} watchers on pane {watcher.pane}")
    if not is_thread_id(watcher.thread_id):
        anomalies.append("invalid watcher thread id")
    elif len(_runtime_watchers_for_thread(model, watcher.thread_id)) != 1:
        panes = sorted({row.pane for row in _runtime_watchers_for_thread(model, watcher.thread_id) if row.pane})
        anomalies.append(f"thread also watched on {', '.join(panes)}")
    if not pane:
        anomalies.append(f"tmux pane {watcher.pane or '?'} is missing")
    elif not pane.detected_thread:
        if pane.has_codex_process:
            anomalies.append(
                pane.detected_thread_detail
                or f"could not determine thread_id for pane={watcher.pane or '?'} from the live Codex process"
            )
        else:
            anomalies.append("no live Codex agent detected in pane")
    elif pane.detected_thread != watcher.thread_id:
        anomalies.append(f"pane agent thread is {_short_thread_id(pane.detected_thread)}")
    if not session:
        anomalies.append("no saved session")
    else:
        conflict = _runtime_session_name_conflict(model, session)
        if conflict:
            anomalies.append(conflict)
        if pane and session.name and pane.window_name and session.name != pane.window_name:
            anomalies.append(f"session name '{session.name}' != window '{pane.window_name}'")

    return anomalies


def _unwatched_pane_anomalies(model: RuntimeModel, pane: LivePaneRecord) -> list[str]:
    anomalies = ["no watcher for live Codex pane"]
    if len(_runtime_panes_for_thread(model, pane.detected_thread)) != 1:
        panes = sorted({row.pane_id for row in _runtime_panes_for_thread(model, pane.detected_thread) if row.pane_id})
        anomalies.append(f"thread also active in panes {', '.join(panes)}")
    session = _runtime_session_for_thread(model, pane.detected_thread)
    if not session:
        anomalies.append("no saved session")
    else:
        conflict = _runtime_session_name_conflict(model, session)
        if conflict:
            anomalies.append(conflict)
    return anomalies


def _unresolved_live_pane_anomalies(pane: LivePaneRecord) -> list[str]:
    detail = pane.detected_thread_detail or f"could not determine thread_id for pane={pane.pane_id} from the live Codex process"
    return [detail]


def _status_row_detail(row: dict[str, str], sj: dict[str, str]) -> str:
    detail = row.get("anomaly_detail", "").strip()
    if detail:
        return detail
    runtime_detail = row.get("runtime_detail", "").strip()
    if runtime_detail:
        return runtime_detail
    return sj.get("health_detail", "").strip()


def _build_status_entries_from_snapshot(
    snapshot: RuntimeSnapshot,
) -> list[tuple[dict[str, str], str, str]]:
    model = _build_runtime_model(snapshot)

    entries: list[tuple[dict[str, str], str, str]] = []
    live_pane_threads = set(model.panes_by_thread)
    claimed_session_threads: set[str] = set()

    for watcher in snapshot.watchers:
        pane = model.panes_by_id.get(watcher.pane)
        session = _runtime_session_for_thread(model, watcher.thread_id)
        if session:
            claimed_session_threads.add(session.thread_id)
        anomalies = _watcher_anomalies(model, watcher)

        window_label = pane.window_label if pane else (session.name or "-")
        entries.append((
            {
                "entity_kind": "watcher",
                "pid": watcher.pid,
                "pane": watcher.pane,
                "thread": watcher.thread_id,
                "name": session.name if session else "",
                "session_name": session.name if session else "",
                "state": session.state_file if session else watcher.state_file,
                "watch": watcher.watch_log,
                "msg_inline": session.message if session and session.message else watcher.message_inline,
                "paused": "1" if watcher.paused else "",
                "runtime_status": watcher.runtime_status,
                "runtime_detail": watcher.runtime_detail,
                "generation": str(watcher.generation),
                "worker_label": watcher.worker_label,
                "anomalies": tuple(anomalies),
                "anomaly_detail": "; ".join(anomalies),
            },
            watcher.pane,
            window_label,
        ))

    watched_panes = {watcher.pane for watcher in snapshot.watchers if watcher.pane}
    for pane in snapshot.panes:
        if pane.pane_id in watched_panes:
            continue
        if not is_thread_id(pane.detected_thread):
            continue
        session = _runtime_session_for_thread(model, pane.detected_thread)
        if not session:
            continue
        claimed_session_threads.add(session.thread_id)
        anomalies = _unwatched_pane_anomalies(model, pane)
        entries.append((
            {
                "entity_kind": "unwatched_pane",
                "pid": "",
                "pane": pane.pane_id,
                "thread": pane.detected_thread,
                "name": session.name if session else "",
                "session_name": session.name if session else "",
                "state": session.state_file if session else "",
                "watch": "",
                "msg_inline": session.message if session and session.message else "",
                "anomalies": tuple(anomalies),
                "anomaly_detail": "; ".join(anomalies),
            },
            pane.pane_id,
            pane.window_label,
        ))

    for session in snapshot.sessions:
        if session.thread_id in claimed_session_threads:
            continue
        if session.thread_id in live_pane_threads:
            continue
        entries.append((
            {
                "entity_kind": "dead_session",
                "pid": "",
                "pane": "",
                "thread": session.thread_id,
                "name": session.name,
                "session_name": session.name,
                "state": session.state_file,
                "watch": "",
                "msg_inline": session.message,
                "anomalies": tuple(),
                "anomaly_detail": "",
            },
            "",
            session.name or "-",
        ))

    return entries


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
    """Return (mode, value), preferring canonical persisted session state.

    mode is 'file', 'inline', or '' if no message is found.
    """
    tid = r.get("thread", "")
    if tid and is_thread_id(tid):
        ss = _read_session_state(tid)
        msg = ss.get("message", "")
        if msg:
            return "inline", msg
    if r.get("msg_inline"):
        return "inline", r["msg_inline"]
    return "", ""


def _short_thread_id(thread_id: str) -> str:
    """Return a compact display form for a thread id in summary tables."""
    if not is_thread_id(thread_id):
        return thread_id
    return f"{thread_id[:8]}…{thread_id[-4:]}"


def _compute_state(r: dict[str, str], sj: dict[str, str]) -> str:
    """Derive display state from process status and health JSON."""
    if r.get("entity_kind") == "dead_session":
        return "dead"
    if r.get("anomalies"):
        return "error"
    runtime_status = r.get("runtime_status", "")
    if runtime_status == "paused" or r.get("paused", "") == "1":
        return "paused"
    if runtime_status == "error":
        return "error"
    if runtime_status == "warn":
        return "warn"
    if runtime_status == "running":
        h = sj.get("health", "")
        if h == "stale":
            h = "warn"
        if h and h != "ok":
            return h
        return "running"
    if runtime_status == "stopping":
        return "warn"
    pid = r.get("pid", "")
    if pid and pid.isdigit() and _is_pid_stopped(int(pid)):
        return "paused"
    # No live watcher runtime.
    if not pid:
        return "dead"
    h = sj.get("health", "")
    if h == "stale":
        h = "warn"
    if h and h != "ok":
        return h
    return "running"


def _message_summary_for_row(r: dict[str, str]) -> str:
    mode, value = _resolve_message(r)
    if mode == "inline" and value:
        return value
    return "-"


_STATE_STYLES = {
    "running": "green",
    "paused": "yellow",
    "warn": "dark_orange",
    "error": "bold red",
    "dead": "dim",
}


def _styled_state(state: str) -> str:
    style = _STATE_STYLES.get(state, "")
    return f"[{style}]{state}[/{style}]" if style else state


_DOCTOR_RESULT_STYLES = {
    "ok": "green",
    "warn": "yellow",
    "error": "bold red",
}


def _state_summary(state: str, sj: dict[str, str]) -> str:
    """Return a compact state cell, with inline detail for degraded rows."""
    detail = sj.get("health_detail", "").strip()
    if not detail or state == "running":
        return _styled_state(state)
    detail_summary = _truncate(detail, 28)
    return f"{_styled_state(state)}\n[dim]{detail_summary}[/dim]"


_MAX_MSG_LINES = 3
_STATUS_SECTION_EVERY = 3


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


def _strip_rich_markup(text: str) -> str:
    return re.sub(r"\[/?[^\]]*\]", "", text)


def _status_window_pane_cell(window_label: str, current_pane: str, thread_id: str, pid: str) -> str:
    line1 = f"{window_label}/{current_pane}" if current_pane else window_label
    lines = [line1]
    if is_thread_id(thread_id):
        lines.append(f"[dim]{_short_thread_id(thread_id)}[/dim]")
    if pid:
        lines.append(f"[dim]pid={pid}[/dim]")
    return "\n".join(lines)


def _status_flex_widths(term_w: int, fixed_col_widths: list[int]) -> tuple[int, int]:
    fixed_total = sum(fixed_col_widths)
    # Rich table overhead: borders (7 │) + padding (2 per col × 6 = 12) = 19
    flex_total = max(48, term_w - fixed_total - 19)
    msg_col_w = max(20, min(32, flex_total // 3))
    last_agent_col_w = max(32, min(72, flex_total - msg_col_w))
    return last_agent_col_w, msg_col_w


def _status_table_plain(rows_data: list[tuple[str, str, str, str, str, str]]) -> None:
    headers = ("WINDOW/PANE", "STATE", "STARTED", "LAST_ACW", "LAST_AGENT", "MESSAGE")
    plain_rows: list[tuple[str, str, str, str, str, str]] = []
    for row in rows_data:
        plain_rows.append(
            tuple(_strip_rich_markup(cell).replace("\n", " / ") for cell in row)
        )

    widths = [len(header) for header in headers]
    for row in plain_rows:
        for i, cell in enumerate(row):
            widths[i] = min(max(widths[i], len(cell)), 60)

    def _fmt(cell: str, width: int) -> str:
        if len(cell) > width:
            return cell[: width - 1] + "…"
        return cell.ljust(width)

    print("  ".join(_fmt(header, widths[i]) for i, header in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    separator = "  ".join("-" * widths[i] for i in range(len(headers)))
    for idx, row in enumerate(plain_rows, 1):
        print("  ".join(_fmt(cell, widths[i]) for i, cell in enumerate(row)))
        if idx % _STATUS_SECTION_EVERY == 0 and idx != len(plain_rows):
            print(separator)


def _status_target_for_row(row: dict[str, str], current_pane: str, window_label: str) -> str:
    if row.get("entity_kind") != "dead_session" and current_pane:
        return current_pane
    name = row.get("name", "").strip()
    if name and name != "-":
        return name
    session_name = row.get("session_name", "").strip()
    if session_name and session_name != "-":
        return session_name
    if window_label and ":" in window_label:
        tail = window_label.rsplit(":", 1)[-1].strip()
        if tail and tail != "-":
            return tail
    return current_pane or row.get("pane", "")


def _sort_session_key(session_name: str) -> tuple[int, object]:
    if session_name.isdigit():
        return 0, int(session_name)
    return 1, session_name


def _status_sort_key(item: tuple[dict[str, str], str, str]) -> tuple[tuple[int, object], int, int, int, str]:
    row, current_pane, window_label = item
    match = re.match(r"([^:]+):(\d+):", window_label)
    if match:
        session_name = match.group(1)
        window_index = int(match.group(2))
    else:
        session_name = "~"
        window_index = 10**9

    pane_number = 10**9
    pane = current_pane or row.get("pane", "")
    if is_pane_id(pane):
        try:
            pane_number = int(pane[1:])
        except ValueError:
            pass
    pid_number = 10**9
    pid = row.get("pid", "")
    if pid.isdigit():
        pid_number = int(pid)
    return (_sort_session_key(session_name), window_index, pane_number, pid_number, window_label)


def _status_recommendations(resolved: list[tuple[dict[str, str], str, str]]) -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()
    for row, current_pane, window_label in resolved:
        if row.get("anomalies"):
            continue
        if row.get("entity_kind") == "dead_session":
            continue
        state = _compute_state(row, _read_state_json(row["state"]))
        if state not in {"warn", "error"}:
            continue
        target = _status_target_for_row(row, current_pane, window_label)
        if not target:
            continue
        cmd = f"acw doctor {shlex.quote(target)}"
        if cmd in seen:
            continue
        seen.add(cmd)
        commands.append(cmd)
    return commands


def _status_invariant_messages(resolved: list[tuple[dict[str, str], str, str]]) -> list[str]:
    messages: list[str] = []
    seen: set[str] = set()

    watcher_rows: list[tuple[dict[str, str], str]] = []
    unwatched_rows: list[tuple[dict[str, str], str]] = []
    unresolved_rows: list[tuple[dict[str, str], str]] = []
    dead_rows: list[tuple[dict[str, str], str, str]] = []
    rows_by_session_name: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row, current_pane, window_label in resolved:
        kind = row.get("entity_kind", "watcher")
        session_name = row.get("session_name", "").strip()
        if session_name:
            rows_by_session_name[session_name].append(row)
        if kind == "watcher":
            watcher_rows.append((row, current_pane))
        elif kind == "unwatched_pane":
            unwatched_rows.append((row, current_pane))
        elif kind == "unresolved_pane":
            unresolved_rows.append((row, current_pane))
        elif kind == "dead_session":
            dead_rows.append((row, current_pane, window_label))

    live_issue_found = any(row.get("anomalies") for row, _ in watcher_rows) or bool(unwatched_rows or unresolved_rows)

    watchers_by_pane: dict[str, list[dict[str, str]]] = defaultdict(list)
    watchers_by_thread: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row, current_pane in watcher_rows:
        pane = current_pane or row.get("pane", "")
        if pane:
            watchers_by_pane[pane].append(row)
        thread_id = row.get("thread", "")
        if is_thread_id(thread_id):
            watchers_by_thread[thread_id.lower()].append(row)

    for pane, rows in sorted(watchers_by_pane.items()):
        if len(rows) <= 1:
            continue
        pids = ", ".join(sorted(row.get("pid", "?") for row in rows))
        msg = f"Invariant error: pane {pane} has {len(rows)} watcher entries ({pids}). Clean up: acw stop {pane}"
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    for thread_id, rows in sorted(watchers_by_thread.items()):
        panes = sorted({row.get("pane", "") for row in rows if row.get("pane", "")})
        if len(panes) <= 1:
            continue
        cleanup = "; ".join(f"acw stop {pane}" for pane in panes)
        msg = (
            f"Invariant error: thread {_short_thread_id(thread_id)} is watched on panes {', '.join(panes)}. "
            f"Clean up: {cleanup}; then start exactly one watcher."
        )
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    for session_name, rows in sorted(rows_by_session_name.items()):
        threads = sorted({row.get("thread", "").lower() for row in rows if is_thread_id(row.get("thread", ""))})
        if len(threads) <= 1:
            continue
        thread_list = ", ".join(_short_thread_id(thread_id) for thread_id in threads)
        msg = (
            f"Invariant error: agent name '{session_name}' maps to multiple threads ({thread_list}). "
            "Clean up the duplicate session/pane state before targeting it by name."
        )
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    for row, current_pane in unwatched_rows:
        pane = current_pane or row.get("pane", "")
        msg = (
            f"Invariant error: live Codex pane {pane} has thread {_short_thread_id(row.get('thread', ''))} "
            f"but no watcher. Clean up: acw start {pane}"
        )
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    for row, current_pane in unresolved_rows:
        pane = current_pane or row.get("pane", "")
        detail = row.get("anomaly_detail", "").strip() or "could not determine thread_id from the live Codex process"
        msg = (
            f"Invariant error: live Codex pane {pane} has a Codex process but no detectable thread id ({detail}). "
            f"Clean up: restart Codex there, then run acw start {pane}."
        )
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    for row, current_pane in watcher_rows:
        anomalies = set(row.get("anomalies", ()))
        pane = current_pane or row.get("pane", "")
        pid = row.get("pid", "?")
        if "no saved session" in anomalies:
            msg = (
                f"Invariant error: pane {pane} watcher entry pid={pid} has no saved session. "
                f"Clean up: acw stop {pane}; then start it again."
            )
            if msg not in seen:
                seen.add(msg)
                messages.append(msg)
        if "no live Codex agent detected in pane" in anomalies:
            msg = (
                f"Invariant error: pane {pane} watcher entry pid={pid} has no live Codex agent in the pane. "
                f"Clean up: acw stop {pane}; restart Codex there, then start once."
            )
            if msg not in seen:
                seen.add(msg)
                messages.append(msg)
        if any(detail.startswith("could not determine thread_id") for detail in anomalies):
            msg = (
                f"Invariant error: pane {pane} watcher entry pid={pid} has a live Codex process but its thread id "
                f"could not be detected. Clean up: acw stop {pane}; restart Codex there, then start once."
            )
            if msg not in seen:
                seen.add(msg)
                messages.append(msg)
        if any(detail.startswith("pane agent thread is ") for detail in anomalies):
            msg = (
                f"Invariant error: pane {pane} watcher entry pid={pid} is bound to the wrong thread. "
                f"Clean up: acw stop {pane}; then start it again against the live pane thread."
            )
            if msg not in seen:
                seen.add(msg)
                messages.append(msg)

    if dead_rows:
        msg = "Hint: run acw cleanup to remove dead sessions."
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    if live_issue_found:
        msg = "Hint: run acw repair --dry-run for live invariant fixes."
        if msg not in seen:
            seen.add(msg)
            messages.append(msg)

    return messages


def _print_status_recommendations(resolved: list[tuple[dict[str, str], str, str]]) -> None:
    commands = _status_recommendations(resolved)
    for cmd in commands:
        print(f"Recommendation: run {cmd}")
    for message in _status_invariant_messages(resolved):
        print(message)


def _status_table(resolved: list[tuple[dict[str, str], str, str]]) -> None:
    import shutil

    # Precompute row data to estimate column widths.
    # (window_pane, state, started, last_acw, last_agent, msg_raw)
    rows_data: list[tuple[str, str, str, str, str, str]] = []
    for r, current_pane, window_label in resolved:
        sj = _read_state_json(r["state"])
        state_value = _compute_state(r, sj)
        state_detail = _status_row_detail(r, sj)
        started, _last_activity = _thread_times(r["thread"], compact=True)
        last_acw = sj.get("last_continue_at", "")
        if last_acw:
            try:
                epoch = time.mktime(time.strptime(last_acw, "%Y-%m-%d %H:%M:%S"))
                last_acw = _format_age(epoch, compact=True)
            except (ValueError, OverflowError):
                pass
        if not last_acw:
            last_acw = "-"
        last_agent = _last_agent_snippet_for_pane(current_pane, r["thread"]) or "-"
        msg_raw = _message_summary_for_row(r)
        window_pane = _status_window_pane_cell(
            window_label,
            current_pane,
            r["thread"],
            r.get("pid", ""),
        )
        rows_data.append((
            window_pane,
            _state_summary(state_value, {"health_detail": state_detail}),
            started,
            last_acw,
            last_agent,
            msg_raw,
        ))

    try:
        from rich.console import Console
        from rich.table import Table
    except ModuleNotFoundError:
        _status_table_plain(rows_data)
        return

    # Estimate MESSAGE column width from terminal width and other columns.
    term_w = shutil.get_terminal_size((120, 24)).columns
    headers = ("WINDOW/PANE", "STATE", "STARTED", "LAST_ACW")
    col_widths = [len(h) for h in headers]
    for row in rows_data:
        for i in range(4):
            # Measure first line only, strip rich markup tags.
            text = row[i].split("\n")[0]
            text = re.sub(r"\[/?[^\]]*\]", "", text)
            col_widths[i] = max(col_widths[i], len(text))
    last_agent_col_w, msg_col_w = _status_flex_widths(term_w, col_widths)

    table = Table(show_lines=False, expand=True)
    table.add_column("WINDOW/PANE", no_wrap=True)
    table.add_column("STATE", no_wrap=True)
    table.add_column("STARTED", no_wrap=True)
    table.add_column("LAST_ACW", no_wrap=True)
    table.add_column("LAST_AGENT", ratio=2, max_width=last_agent_col_w)
    table.add_column("MESSAGE", ratio=1, max_width=msg_col_w)

    for idx, (window_pane, state_value, started, last_acw, last_agent, msg_raw) in enumerate(rows_data, 1):
        last_agent_summary = _clamp_visual_lines(last_agent, _MAX_MSG_LINES, last_agent_col_w)
        msg_summary = _clamp_visual_lines(msg_raw, _MAX_MSG_LINES, msg_col_w)
        table.add_row(
            window_pane, state_value,
            started, last_acw, last_agent_summary, msg_summary,
        )
        if idx % _STATUS_SECTION_EVERY == 0 and idx != len(rows_data):
            table.add_section()

    Console().print(table)


def _status_details(
    resolved: list[tuple[dict[str, str], str, str]],
    model: RuntimeModel | None = None,
) -> None:
    if model is None:
        model = _build_runtime_model()
    total = len(resolved)
    for idx, (r, current_pane, window_label) in enumerate(resolved, 1):
        if idx > 1:
            print()

        sj = _read_state_json(r["state"])
        state_value = _compute_state(r, sj)
        state_detail = _status_row_detail(r, sj)

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
        if mode == "inline" and value:
            msg_full = value
        else:
            msg_full = "-"

        started, last_activity = _thread_times(r["thread"])
        last_agent = _last_agent_snippet_for_pane(current_pane, r["thread"]) or "-"

        last_acw = sj.get("last_continue_at", "-")

        print(f"=== Entry {idx}/{total} ===")
        print(f"  {'WINDOW:':<16} {window_label}")
        print(f"  {'PANE:':<16} {current_pane or '(no live pane)'}")
        print(f"  {'PID:':<16} {r['pid']}")
        print(f"  {'THREAD_ID:':<16} {r['thread'] or 'unknown'}")
        pane_record = model.panes_by_id.get(current_pane) if current_pane else None
        provenance = _thread_provenance_text(pane_record.detected_thread_source) if pane_record else ""
        if provenance:
            print(f"  {'THREAD_PROVENANCE:':<16} {provenance}")
        print(f"  {'STATE:':<16} {state_value}")
        print(f"  {'STARTED:':<16} {started}")
        print(f"  {'LAST_ACTIVITY:':<16} {last_activity}")
        print(f"  {'LAST_ACW:':<16} {last_acw}")
        print(f"  {'LAST_AGENT:':<16} {last_agent}")
        if state_detail:
            label = "ANOMALY" if r.get("anomalies") else "HEALTH_DETAIL"
            print(f"  {f'{label}:':<16} {state_detail}")
        hts = sj.get("health_ts", "")
        if hts:
            print(f"  {'HEALTH_SINCE:':<16} {hts}")
        print(f"  {'LAST_EVENT:':<16} {last_event}")
        print(f"  {'MESSAGE:':<16} {msg_full}")
        if r["watch"]:
            print(f"  {'WATCH_LOG:':<16} {r['watch']}")
        if r["state"]:
            print(f"  {'STATE_FILE:':<16} {r['state']}")
    _print_status_recommendations(resolved)


def _load_sessions(socket_path: str | None = None) -> list[dict[str, str]]:
    """Load all session state files for one tmux socket scope."""
    candidates: list[dict[str, str]] = []
    scope = _tmux_scope_key(socket_path)
    for sf in glob.glob(os.path.join(STATE_DIR, "acw_session.*.json")):
        try:
            with open(sf) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        thread_id = data.get("thread_id", "")
        if not thread_id or not is_thread_id(thread_id):
            continue
        if _tmux_scope_key(str(data.get("tmux_socket", "")).strip()) != scope:
            continue
        candidates.append({
            "thread_id": thread_id,
            "name": data.get("name", ""),
            "message": data.get("message", ""),
            "pane": data.get("pane", ""),
            "tmux_socket": _tmux_scope_key(str(data.get("tmux_socket", "")).strip()),
            "paused": bool(data.get("paused", False)),
            "generation": int(data.get("generation", 0) or 0),
            "state_file": sf,
            "health": data.get("health", ""),
            "health_detail": data.get("health_detail", ""),
            "health_ts": data.get("health_ts", ""),
            "last_continue_at": data.get("last_continue_at", ""),
        })
    return candidates


def _saved_session_panes_for_target(target: str, model: RuntimeModel | None = None) -> list[str]:
    if model is None:
        model = _build_runtime_model()
    panes: list[str] = []
    seen: set[str] = set()
    if is_thread_id(target):
        sessions: tuple[SavedSessionRecord, ...] = tuple(
            [session] if (session := _runtime_session_for_thread(model, target)) else []
        )
    else:
        sessions = model.sessions_by_name.get(target, ())

    for session in sessions:
        thread_id = session.thread_id
        if is_thread_id(target):
            if thread_id != target.lower():
                continue
        for pane in _runtime_panes_for_thread(model, thread_id):
            if not is_pane_id(pane.pane_id) or pane.pane_id in seen:
                continue
            seen.add(pane.pane_id)
            panes.append(pane.pane_id)
    return panes


def _resolve_restart_pane_target(target: str, model: RuntimeModel | None = None) -> str:
    if is_pane_id(target):
        return target

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target(target)
        if pane and is_pane_id(pane):
            return pane
        print(f"error: could not resolve window target '{target}'", file=sys.stderr)
        sys.exit(1)

    saved_panes = _saved_session_panes_for_target(target, model=model)
    if len(saved_panes) == 1:
        return saved_panes[0]
    if len(saved_panes) > 1:
        joined = ", ".join(saved_panes)
        print(
            f"error: saved session target '{target}' is ambiguous across live panes: {joined}",
            file=sys.stderr,
        )
        sys.exit(1)

    pane = resolve_pane_from_window_name(target, quiet=True)
    if pane and is_pane_id(pane):
        return pane

    if _tmux_socket_recovery_hint():
        _print_tmux_unavailable_hint(target)
        sys.exit(1)

    print(f"error: no watcher, window, or live Codex pane found for '{target}'", file=sys.stderr)
    print(
        "hint: use a pane id ('%6'), window name, window index ('2'), or a saved session name with a live Codex pane",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_status(argv: list[str]) -> None:
    pane_arg = ""
    details = False
    for arg in argv:
        if arg == "--details":
            details = True
        else:
            pane_arg = arg

    snapshot = _build_runtime_snapshot()
    model = _build_runtime_model(snapshot)
    recovery = ""
    if not snapshot.panes and not snapshot.watchers:
        recovery = _tmux_socket_recovery_hint()
        if recovery:
            print(
                "warning: tmux metadata is unavailable; status is falling back to saved session state",
                file=sys.stderr,
            )
            print(recovery, file=sys.stderr)

    resolved = _build_status_entries_from_snapshot(snapshot)

    # Optional pane filter.
    if pane_arg:
        target = pane_arg
        pane = _resolve_live_pane_target(target)
        if pane and pane != target:
            print(f"resolved: target={target} pane={pane}")
        target_thread = target.lower() if is_thread_id(target) else ""
        resolved = [
            (r, lp, wl)
            for r, lp, wl in resolved
            if (pane and lp == pane)
            or (target_thread and r.get("thread", "").lower() == target_thread)
            or r.get("session_name", "") == target
        ]

    detail_tag = " (details)" if details else ""
    print(f"Sessions{detail_tag}: {len(resolved)}")

    if not resolved:
        print("(none)")
        return

    resolved.sort(key=_status_sort_key)

    if details:
        _status_details(resolved, model=model)
    else:
        _status_table(resolved)
        _print_status_recommendations(resolved)


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def _state_dir_is_writable() -> tuple[bool, str]:
    try:
        fd, tmp = tempfile.mkstemp(prefix="acw_doctor_", dir=STATE_DIR)
        os.close(fd)
        os.unlink(tmp)
        return True, STATE_DIR
    except OSError as exc:
        return False, f"{STATE_DIR} ({exc})"


def _codex_auth_state_available() -> tuple[bool, str]:
    auth = Path(STATE_DIR) / "auth.json"
    if auth.is_file():
        return True, str(auth)
    return False, str(auth)


def _doctor_resolve_target(target: str, model: RuntimeModel | None = None) -> tuple[str, str]:
    if model is None:
        model = _build_runtime_model()
    if not target:
        pane = current_tmux_pane()
        if pane:
            return pane, ""
        return "", ""

    if is_pane_id(target):
        return target, ""

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target(target)
        if pane and is_pane_id(pane):
            return pane, ""
        return "", f"could not resolve window target '{target}'"

    saved_panes = _saved_session_panes_for_target(target, model=model)
    if len(saved_panes) == 1:
        return saved_panes[0], ""
    if len(saved_panes) > 1:
        joined = ", ".join(saved_panes)
        return "", f"saved session target '{target}' is ambiguous across live panes: {joined}"

    pane = resolve_pane_from_window_name(target)
    if pane and is_pane_id(pane):
        return pane, ""

    if _tmux_socket_recovery_hint():
        return "", _tmux_socket_recovery_hint()
    return "", f"could not resolve target '{target}'"


@dataclass
class DoctorReport:
    checks: list[tuple[str, str]]
    result: str
    exit_code: int
    recommendations: list[str]


@dataclass(frozen=True)
class RepairStep:
    kind: str
    target: str
    summary: str
    auto_apply: bool
    thread_id: str = ""
    message: str = ""


def _pane_numeric_sort_key(pane: str) -> tuple[int, str]:
    if is_pane_id(pane):
        try:
            return int(pane[1:]), pane
        except ValueError:
            pass
    return 10**9, pane


def _repair_message_for_pane(model: RuntimeModel, pane: str, live_thread: str) -> str:
    session = _runtime_session_for_thread(model, live_thread)
    if session and session.message:
        return session.message
    for watcher in _runtime_watchers_for_pane(model, pane):
        if watcher.thread_id != live_thread:
            continue
        if watcher.message_inline:
            return _message_text(watcher.message_inline)
    return ""


def _repair_steps_for_pane(model: RuntimeModel, pane: str) -> list[RepairStep]:
    pane_record = model.panes_by_id.get(pane)
    watchers = list(_runtime_watchers_for_pane(model, pane))

    if not pane_record:
        if watchers:
            return [
                RepairStep(
                    kind="stop_watchers_keep_session",
                    target=pane,
                    summary=f"pane {pane} is missing from tmux; stop its watcher entries but keep any saved session",
                    auto_apply=True,
                )
            ]
        return []

    if not pane_record.has_codex_process:
        if watchers:
            return [
                RepairStep(
                    kind="stop_watchers_keep_session",
                    target=pane,
                    summary=f"pane {pane} has no live Codex agent; stop its watcher entries but keep any saved session",
                    auto_apply=True,
                )
            ]
        return [
            RepairStep(
                kind="manual",
                target=pane,
                summary=f"pane {pane} has no live Codex agent; restart Codex there manually before repairing watcher state",
                auto_apply=False,
            )
        ]

    if not is_thread_id(pane_record.detected_thread):
        detail = pane_record.detected_thread_detail or f"could not determine thread_id for pane={pane} from the live Codex process"
        return [
            RepairStep(
                kind="manual",
                target=pane,
                summary=f"pane {pane} is unresolved: {detail}",
                auto_apply=False,
            )
        ]

    live_thread = pane_record.detected_thread.lower()
    related_panes = sorted(
        {
            row.pane_id
            for row in _runtime_panes_for_thread(model, live_thread)
            if row.pane_id
        },
        key=_pane_numeric_sort_key,
    )
    if len(related_panes) > 1:
        return [
            RepairStep(
                kind="manual",
                target=pane,
                summary=(
                    f"thread {_short_thread_id(live_thread)} is active across panes {', '.join(related_panes)}; "
                    "choose one pane to keep and repair the others manually"
                ),
                auto_apply=False,
                thread_id=live_thread,
            )
        ]

    watched_panes = sorted(
        {
            row.pane
            for row in _runtime_watchers_for_thread(model, live_thread)
            if row.pane
        },
        key=_pane_numeric_sort_key,
    )
    if len(watched_panes) > 1:
        return [
            RepairStep(
                kind="manual",
                target=pane,
                summary=(
                    f"thread {_short_thread_id(live_thread)} is watched across panes {', '.join(watched_panes)}; "
                    "choose one pane to keep and repair the others manually"
                ),
                auto_apply=False,
                thread_id=live_thread,
            )
        ]

    session = _runtime_session_for_thread(model, live_thread)
    if session:
        conflict = _runtime_session_name_conflict(model, session)
        if conflict:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=f"pane {pane} is blocked: {conflict}",
                    auto_apply=False,
                    thread_id=live_thread,
                )
            ]

    message = _repair_message_for_pane(model, pane, live_thread)
    if not watchers:
        if not session or not message:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=(
                        f"pane {pane} has live thread {_short_thread_id(live_thread)} but no watcher and no saved continue message; "
                        "start it manually with an explicit --message"
                    ),
                    auto_apply=False,
                    thread_id=live_thread,
                )
            ]
        return [
            RepairStep(
                kind="start_watcher",
                target=pane,
                summary=f"start one watcher on pane {pane} for thread {_short_thread_id(live_thread)}",
                auto_apply=True,
                thread_id=live_thread,
                message=message,
            )
        ]

    if len(watchers) > 1:
        if not message:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=(
                        f"pane {pane} has duplicate watcher entries but no saved continue message for thread "
                        f"{_short_thread_id(live_thread)}; repair it manually"
                    ),
                    auto_apply=False,
                    thread_id=live_thread,
                )
            ]
        return [
            RepairStep(
                kind="restart_watcher",
                target=pane,
                summary=f"replace duplicate watcher entries on pane {pane} with one watcher for thread {_short_thread_id(live_thread)}",
                auto_apply=True,
                thread_id=live_thread,
                message=message,
            )
        ]

    watcher = watchers[0]
    if not is_thread_id(watcher.thread_id):
        if not message:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=f"pane {pane} has a watcher with an invalid thread id and no saved continue message; repair it manually",
                    auto_apply=False,
                )
            ]
        return [
            RepairStep(
                kind="restart_watcher",
                target=pane,
                summary=f"restart the watcher on pane {pane} with the live thread {_short_thread_id(live_thread)}",
                auto_apply=True,
                thread_id=live_thread,
                message=message,
            )
        ]

    if watcher.thread_id != live_thread:
        if not message:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=(
                        f"pane {pane} watcher is bound to {_short_thread_id(watcher.thread_id)}, but the live pane is "
                        f"{_short_thread_id(live_thread)} and no saved continue message is available; repair it manually"
                    ),
                    auto_apply=False,
                    thread_id=live_thread,
                )
            ]
        return [
            RepairStep(
                kind="restart_watcher",
                target=pane,
                summary=f"rebind the watcher on pane {pane} to the live thread {_short_thread_id(live_thread)}",
                auto_apply=True,
                thread_id=live_thread,
                message=message,
            )
        ]

    if not session:
        if not message:
            return [
                RepairStep(
                    kind="manual",
                    target=pane,
                    summary=f"pane {pane} has no saved session and no recoverable continue message; repair it manually",
                    auto_apply=False,
                    thread_id=live_thread,
                )
            ]
        return [
            RepairStep(
                kind="recreate_session",
                target=pane,
                summary=f"recreate the missing saved session for pane {pane} and thread {_short_thread_id(live_thread)}",
                auto_apply=True,
                thread_id=live_thread,
                message=message,
            )
        ]

    if pane_record.window_name and session.name and pane_record.window_name != session.name:
        return [
            RepairStep(
                kind="sync_session_name",
                target=pane,
                summary=f"sync the saved session name for pane {pane} to the current window name '{pane_record.window_name}'",
                auto_apply=True,
                thread_id=live_thread,
            )
        ]

    return []


def _build_repair_plan(
    model: RuntimeModel,
    target_panes: tuple[str, ...] | None = None,
) -> list[RepairStep]:
    if target_panes is None:
        candidate_panes_set = set(model.watchers_by_pane)
        for pane in model.snapshot.panes:
            if not pane.pane_id or not is_thread_id(pane.detected_thread):
                continue
            if _runtime_session_for_thread(model, pane.detected_thread):
                candidate_panes_set.add(pane.pane_id)
        candidate_panes = sorted(candidate_panes_set, key=_pane_numeric_sort_key)
    else:
        candidate_panes = list(target_panes)

    steps: list[RepairStep] = []
    seen: set[tuple[object, ...]] = set()
    for pane in candidate_panes:
        for step in _repair_steps_for_pane(model, pane):
            key = (step.kind, step.summary) if not step.auto_apply else (step.kind, step.target, step.thread_id)
            if key in seen:
                continue
            seen.add(key)
            steps.append(step)

    if target_panes is None:
        live_threads = set(model.panes_by_thread) | set(model.watchers_by_thread)
        dead_sessions = [
            session
            for session in model.snapshot.sessions
            if session.thread_id not in live_threads
        ]
        if dead_sessions:
            steps.append(
                RepairStep(
                    kind="cleanup_dead_sessions",
                    target="",
                    summary=f"run acw cleanup to remove {len(dead_sessions)} dead saved session(s)",
                    auto_apply=False,
                )
            )

    return steps


def _doctor_recommendation_target(target: str, pane: str, rows: list[dict[str, str]]) -> str:
    if target:
        return target
    if rows:
        name = _read_session_state(rows[0].get("thread", "")).get("name", "").strip()
        if name:
            return name
    return pane


def _doctor_report(target: str = "") -> DoctorReport:
    checks: list[tuple[str, str]] = []
    recommendations: list[str] = []
    errors = 0
    warnings = 0
    model = _build_runtime_model()

    ok, detail = _state_dir_is_writable()
    checks.append(("ok" if ok else "error", f"state dir writable: {detail}"))
    errors += 0 if ok else 1

    ok, detail = _codex_auth_state_available()
    checks.append(("ok" if ok else "error", f"Codex auth state present: {detail}"))
    errors += 0 if ok else 1

    tmux_ok = run_tmux("list-sessions") is not None
    checks.append(("ok" if tmux_ok else "error", "tmux server reachable"))
    errors += 0 if tmux_ok else 1
    recovery = _tmux_socket_recovery_hint()
    if recovery:
        checks.append(("info", recovery))

    pane, pane_error = _doctor_resolve_target(target, model=model)
    if pane_error:
        checks.append(("error", pane_error))
        errors += 1
        return DoctorReport(checks=checks, result="error", exit_code=1, recommendations=recommendations)

    if not pane:
        checks.append(("info", "pane checks skipped: no target and TMUX_PANE not set"))
        result = "error" if errors else "ok"
        return DoctorReport(checks=checks, result=result, exit_code=1 if errors else 0, recommendations=recommendations)

    checks.append(("ok", f"pane resolved: {pane}"))
    tid, thread_error = _strict_thread_for_pane(model, pane)
    if tid and is_thread_id(tid):
        checks.append(("ok", f"Codex thread detected: {tid}"))
        pane_record = model.panes_by_id.get(pane)
        provenance = _thread_provenance_text(pane_record.detected_thread_source) if pane_record else ""
        if provenance:
            checks.append(("info", f"thread provenance: {provenance}"))
    else:
        checks.append(("error", thread_error or f"could not detect a Codex thread for pane {pane}"))
        errors += 1

    rows = [watcher for watcher in _runtime_watchers_for_pane(model, pane)]
    ps_rows = [
        {
            "pid": watcher.pid,
            "pane": watcher.pane,
            "thread": watcher.thread_id,
            "state": watcher.state_file,
            "watch": watcher.watch_log,
            "msg_inline": watcher.message_inline,
            "paused": "1" if watcher.paused else "",
            "runtime_status": watcher.runtime_status,
            "runtime_detail": watcher.runtime_detail,
        }
        for watcher in rows
    ]
    command_target = _doctor_recommendation_target(target, pane, ps_rows)
    if len(rows) > 1:
        pids = ", ".join(sorted(watcher.pid or "?" for watcher in rows))
        checks.append(("error", f"multiple watchers running on pane {pane}: {pids}"))
        errors += 1
        recommendations.append(f"acw stop {shlex.quote(pane)}")
    elif rows:
        watcher = rows[0]
        checks.append(("ok", f"watcher running: pane={pane} pid={watcher.pid}"))
        anomalies = _watcher_anomalies(model, watcher)
        if anomalies:
            detail = "; ".join(anomalies)
            checks.append(("error", f"watcher identity mismatch: {detail}"))
            errors += 1
            recommendations.append(f"acw stop {shlex.quote(pane)}")
        else:
            row = {
                "pid": watcher.pid,
                "pane": watcher.pane,
                "thread": watcher.thread_id,
                "state": watcher.state_file,
                "watch": watcher.watch_log,
                "msg_inline": watcher.message_inline,
                "paused": "1" if watcher.paused else "",
                "runtime_status": watcher.runtime_status,
                "runtime_detail": watcher.runtime_detail,
            }
            sj = _read_state_json(watcher.state_file)
            watcher_state = _compute_state(row, sj)
            health_detail = sj.get("health_detail", "").strip()
            if watcher_state == "running":
                checks.append(("ok", "watcher health: running"))
            elif watcher_state == "paused":
                msg = "watcher paused"
                if health_detail:
                    msg += f" - {health_detail}"
                checks.append(("warn", msg))
                warnings += 1
                recommendations.append(f"acw resume {shlex.quote(command_target)}")
            else:
                msg = f"watcher health: {watcher_state}"
                if health_detail:
                    msg += f" - {health_detail}"
                level = "warn" if watcher_state == "warn" else "error"
                checks.append((level, msg))
                if level == "warn":
                    warnings += 1
                else:
                    errors += 1
                recommendations.append(f"acw restart {shlex.quote(command_target)}")
    else:
        checks.append(("info", f"no watcher running for pane {pane}"))
        if tid and is_thread_id(tid):
            recommendations.append(f"acw start {shlex.quote(command_target)} {tid}")

    repair_steps = _repair_steps_for_pane(model, pane) if pane else []
    if any(step.auto_apply for step in repair_steps):
        recommendations.insert(0, f"acw repair {shlex.quote(command_target)}")
    elif repair_steps:
        recommendations.insert(0, f"acw repair {shlex.quote(command_target)} --dry-run")

    result = "error" if errors else ("warn" if warnings else "ok")
    exit_code = 1 if errors else 0
    return DoctorReport(
        checks=checks,
        result=result,
        exit_code=exit_code,
        recommendations=list(dict.fromkeys(recommendations)),
    )

def _print_doctor_plain(report: DoctorReport) -> None:
    print("Doctor")
    print(f"  state_dir: {STATE_DIR}")
    sock = _preferred_tmux_socket() or "(default)"
    print(f"  tmux_socket: {sock}")
    for level, message in report.checks:
        print(f"[{level}] {message}")
    if report.recommendations:
        print()
        for cmd in report.recommendations:
            print(f"Recommendation: run {cmd}")
    print(f"RESULT: {report.result}")


def _print_doctor_rich(report: DoctorReport) -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    console.print(
        Panel.fit(
            f"state_dir: {STATE_DIR}\n"
            f"tmux_socket: {_preferred_tmux_socket() or '(default)'}",
            title="Doctor",
            border_style="cyan",
        )
    )

    table = Table(show_header=False, box=None, expand=True, pad_edge=False)
    table.add_column("level", no_wrap=True)
    table.add_column("message")
    for level, message in report.checks:
        style = _DOCTOR_RESULT_STYLES.get(level, "dim") if level != "info" else "dim"
        table.add_row(f"[{style}]{level.upper()}[/{style}]", message)
    console.print(table)

    if report.recommendations:
        body = "\n".join(f"[bold cyan]{cmd}[/bold cyan]" for cmd in report.recommendations)
        console.print(
            Panel.fit(
                body,
                title="Recommended Command",
                border_style="yellow",
            )
        )

    style = _DOCTOR_RESULT_STYLES.get(report.result, "")
    result_text = f"[{style}]RESULT: {report.result}[/{style}]" if style else f"RESULT: {report.result}"
    console.print(result_text)


def cmd_doctor(argv: list[str]) -> None:
    target = ""
    if argv:
        if len(argv) > 1 or argv[0].startswith("--"):
            print("error: doctor accepts at most one target", file=sys.stderr)
            sys.exit(2)
        target = argv[0]

    report = _doctor_report(target)
    try:
        _print_doctor_rich(report)
    except ModuleNotFoundError:
        _print_doctor_plain(report)
    if report.exit_code:
        sys.exit(report.exit_code)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cmd_cleanup(argv: list[str]) -> None:
    """Cleanup stale files."""
    if argv:
        print("error: cleanup does not accept a target", file=sys.stderr)
        sys.exit(2)
    cleanup_stale_files(remove_dead_sessions=True)
    print("cleanup: removed stale files")


def cleanup_stale_files(remove_dead_sessions: bool = False) -> None:
    """Remove files for watchers that are no longer running."""
    daemon_pid_file = daemon_pid_file_for_scope()
    if daemon_pid_file and os.path.exists(daemon_pid_file) and not _daemon_pid_alive(daemon_pid_file):
        Path(daemon_pid_file).unlink(missing_ok=True)

    for runtime_path in glob.glob(daemon_runtime_glob_for_scope()):
        try:
            with open(runtime_path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            Path(runtime_path).unlink(missing_ok=True)
            continue
        daemon_pid = str(data.get("daemon_pid", "")).strip() or str(data.get("pid", "")).strip()
        if daemon_pid and daemon_pid.isdigit():
            try:
                os.kill(int(daemon_pid), 0)
            except OSError:
                Path(runtime_path).unlink(missing_ok=True)

    # Build set of live watcher keys for watch logs.
    live_keys: set[str] = set()
    live_runtime_threads: set[str] = set()
    for r in watcher_rows():
        k = key_from_pane(r["pane"])
        if k:
            live_keys.add(k)
        tid = r.get("thread", "")
        if is_thread_id(tid):
            live_runtime_threads.add(tid.lower())

    # Watch logs for dead sessions.
    keep_logs: set[str] = set()
    for k in live_keys:
        keep_logs.add(watch_log_for_key(k))
    for pat in (os.path.join(STATE_DIR, "auto_continue_logwatch*.log"),):
        for lf in glob.glob(pat):
            if lf not in keep_logs:
                Path(lf).unlink(missing_ok=True)

    if not remove_dead_sessions:
        return

    model = _build_runtime_model()
    live_thread_panes = {tid.lower() for tid in model.panes_by_thread}
    scope = _tmux_scope_key()

    # Session files for threads without a running watcher or live Codex pane.
    for sf in glob.glob(os.path.join(STATE_DIR, "acw_session.*.json")):
        try:
            with open(sf) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            Path(sf).unlink(missing_ok=True)
            continue
        tid = str(data.get("thread_id", "")).lower()
        if not is_thread_id(tid):
            Path(sf).unlink(missing_ok=True)
            continue
        if _tmux_scope_key(str(data.get("tmux_socket", "")).strip()) != scope:
            continue
        if tid not in live_runtime_threads and tid not in live_thread_panes:
            Path(sf).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def print_help(file=None) -> None:
    if file is None:
        file = sys.stdout
    prog = os.path.basename(sys.argv[0]) or "acw"
    print(build_help_text(prog), file=file)


def _print_progress(message: str) -> None:
    print(message, flush=True)


def main() -> None:
    if len(sys.argv) < 2:
        subcmd = "status"
        rest: list[str] = []
    else:
        subcmd = sys.argv[1]
        rest = sys.argv[2:]

    if subcmd in ("-h", "--help", "help"):
        print_help()
        raise SystemExit(0)

    # Only clean up stale files on commands that mutate state.  Read-only
    # commands (status, pause, resume) skip the cleanup overhead (ps scan +
    # multiple globs).
    if subcmd in ("start", "stop", "restart", "edit", "repair", "cleanup"):
        cleanup_stale_files()

    commands = {
        "start": cmd_start,
        "stop": cmd_stop,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "restart": cmd_restart,
        "repair": cmd_repair,
        "cleanup": cmd_cleanup,
        "status": cmd_status,
        "edit": cmd_edit,
        "doctor": cmd_doctor,
        "_window-renamed": cmd_window_renamed,
        "_daemon": _daemon_main,
    }

    if subcmd in commands:
        commands[subcmd](rest)
    else:
        print_help(file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
