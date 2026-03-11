#!/usr/bin/env python3
"""Auto-continue watcher that does not use Codex notify hooks.

It tails ``~/.codex/log/codex-tui.log`` and sends a prompt to a tmux pane when
Codex emits a turn-complete signal for a watched thread/turn. Older Codex
builds log ``needs_follow_up=false`` and newer builds log
``codex_core::tasks: close``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
import threading
from typing import Optional

try:
    from watchdog.observers import Observer as _WatchdogObserver
    from watchdog.events import FileSystemEventHandler as _FSHandler

    class _WakeHandler(_FSHandler):
        """Signal the main loop when a watched file is modified or created."""

        def __init__(self, wake: threading.Event, paths: set[str]):
            self.wake = wake
            self.paths = paths

        def on_modified(self, event):
            if not event.is_directory and event.src_path in self.paths:
                self.wake.set()

        def on_created(self, event):
            if not event.is_directory and event.src_path in self.paths:
                self.wake.set()
except ImportError:
    _WatchdogObserver = None  # type: ignore[assignment,misc]

EVENT_RE = re.compile(
    r"session_loop\{thread_id=([0-9a-f\-]+)\}.*post sampling token usage "
    r"turn_id=([^ ]+).*needs_follow_up=(true|false)"
)

TASK_CLOSE_RE = re.compile(
    r'session_loop\{thread_id=([0-9a-f\-]+)\}.*'
    r'turn\{[^}]*turn.id=([^ ]+)[^}]*\}: codex_core::tasks: close\b'
)

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
STATE_DIR = Path.home() / ".codex"


def _session_state_path(thread_id: str) -> Path:
    return STATE_DIR / f"acw_session.{thread_id}.json"

ROLLOUT_RE = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)

THREAD_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

def is_thread_id(s: str) -> bool:
    return bool(THREAD_ID_RE.fullmatch(s))

CHANNEL_CLOSED_RE = re.compile(
    r"session_loop\{thread_id=([0-9a-f\-]+)\}.*(?:channel closed|failed to record rollout)"
)

HEALTH_CHECK_INTERVAL = 30.0   # seconds between periodic health checks
ROLLOUT_STALE_SECS = 300.0     # rollout file considered stale after 5 minutes
ROLLOUT_CHANNEL_CLOSED_GRACE_SECS = 5.0


def find_rollout_file(thread_id: str, sessions_dir: Path) -> Optional[Path]:
    """Glob for the rollout JSONL file matching *thread_id*."""
    if not sessions_dir.is_dir():
        return None
    pattern = f"*/*/*/rollout-*-{thread_id}.jsonl"
    matches = list(sessions_dir.glob(pattern))
    if not matches:
        return None
    # Most recent (there should normally be exactly one).
    return max(matches, key=lambda p: p.stat().st_mtime)


def parse_rollout_event(line: str) -> Optional[str]:
    """Parse a JSONL line; return *turn_id* if it is a ``task_complete`` event."""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if obj.get("type") != "event_msg":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "task_complete":
        return None
    return payload.get("turn_id")


def parse_codex_log_event(line: str) -> Optional[tuple[str, str, str]]:
    """Return a normalized completion event from codex-tui.log."""
    m = EVENT_RE.search(line)
    if m:
        return m.group(1), m.group(2), m.group(3)

    m = TASK_CLOSE_RE.search(line)
    if m:
        return m.group(1), m.group(2), "false"

    return None


def compute_health(
    *,
    watched_thread: str,
    rollout_path: Optional[Path],
    watcher_start: float,
    now: float,
    rollout_channel_closed: bool,
    rollout_channel_closed_at: float,
    codex_log_completion_seen: bool,
) -> tuple[str, str]:
    """Compute watcher health from the current event-source state.

    `codex-tui.log` is the primary completion source. Once we have observed a
    completion event there for the watched thread, missing/stale rollout data is
    no longer an error condition.
    """
    if rollout_channel_closed:
        if codex_log_completion_seen:
            return "warn", "rollout channel closed; using codex log"
        if rollout_channel_closed_at and now - rollout_channel_closed_at < ROLLOUT_CHANNEL_CLOSED_GRACE_SECS:
            return "ok", ""
        return "error", "rollout channel closed"

    if codex_log_completion_seen:
        return "ok", ""

    if rollout_path is not None and rollout_path.exists():
        try:
            stale_secs = now - rollout_path.stat().st_mtime
            if stale_secs > ROLLOUT_STALE_SECS and now - watcher_start > ROLLOUT_STALE_SECS:
                return "stale", f"rollout no writes {int(stale_secs / 60)}m"
        except OSError:
            pass
        return "ok", ""

    if watched_thread and now - watcher_start > 60:
        return "warn", "no rollout file found"

    return "ok", ""


def _parse_rollout_line_type(line: str) -> Optional[str]:
    """Return the event sub-type for a rollout JSONL line, or None."""
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    payload = obj.get("payload")
    if isinstance(payload, dict):
        return payload.get("type")
    return obj.get("type")


def _check_rollout_tail_for_pending(
    rollout_path: Path,
    watched_thread: str,
    last_sent_turn: str,
    last_sent_thread: str,
    watch_log: Path,
) -> Optional[tuple[str, str, str]]:
    """Scan backwards from EOF for a task_complete not yet followed by a user_message.

    Returns a synthetic event tuple ``(thread_id, turn_id, "false")`` if codex
    appears idle and we haven't already responded, else ``None``.
    """
    try:
        file_size = rollout_path.stat().st_size
    except OSError:
        return None
    if file_size == 0:
        return None

    # Read up to 2 MB from the end — task_complete can be followed by large
    # response_item / token_count events before the file goes quiet.
    tail_bytes = min(file_size, 2 * 1024 * 1024)
    try:
        with rollout_path.open("rb") as f:
            f.seek(-tail_bytes, os.SEEK_END)
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None

    # Walk lines in reverse.  If we hit a user_message first, codex already
    # received new input → no action.  If we hit task_complete first, codex
    # is idle and we should send a continue.
    for line in reversed(tail.splitlines()):
        etype = _parse_rollout_line_type(line)
        if etype == "user_message":
            return None  # new turn already started
        if etype == "task_complete":
            turn_id = parse_rollout_event(line)
            if not turn_id:
                return None
            if turn_id == last_sent_turn and watched_thread == last_sent_thread:
                return None  # already responded to this turn
            append_log(
                watch_log,
                f"startup: found pending task_complete turn={turn_id}",
            )
            return (watched_thread, turn_id, "false")

    return None


def find_latest_rollout(sessions_dir: Path) -> Optional[tuple[str, Path]]:
    """Find the most recently modified rollout file; return ``(thread_id, path)``."""
    if not sessions_dir.is_dir():
        return None
    matches = list(sessions_dir.glob("*/*/*/rollout-*.jsonl"))
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in matches[:5]:
        m = ROLLOUT_RE.search(path.name)
        if m:
            return m.group(1), path
    return None


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def append_log(log_file: Path, msg: str) -> None:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{now_ts()}] {msg}\n")
    except OSError:
        pass  # disk full / read-only — keep running


def run_tmux(args: list[str], *, capture_output: bool = True) -> subprocess.CompletedProcess:
    cmd = ["tmux"]
    socket_path = os.environ.get("AUTO_CONTINUE_TMUX_SOCKET", "")
    if socket_path:
        cmd.extend(["-S", socket_path])
    cmd.extend(args)
    rc = subprocess.run(
        cmd,
        capture_output=capture_output,
        text=True,
        check=False,
    )
    if rc.returncode == 0:
        return rc

    # Recover from stale TMUX socket by retrying against default server.
    if os.environ.get("TMUX") and not socket_path:
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        return subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            check=False,
            env=env,
        )

    return rc


PANE_ERROR_PATTERNS = [
    re.compile(r"conversation interrupted", re.IGNORECASE),
    re.compile(r"something went wrong\? hit /feedback", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"try again at\b", re.IGNORECASE),
    re.compile(r"exceeded.*quota", re.IGNORECASE),
    re.compile(r"billing", re.IGNORECASE),
    re.compile(r"unauthorized|authentication.*(failed|error)", re.IGNORECASE),
]


def tmux_capture_pane(pane: str, lines: int = 10) -> str:
    """Capture recent visible lines from a tmux pane."""
    rc = run_tmux(["capture-pane", "-t", pane, "-p", "-S", f"-{lines}"])
    if rc.returncode == 0:
        return rc.stdout or ""
    return ""


def check_pane_for_errors(pane: str) -> Optional[str]:
    """Return the first matched error string from the pane, or None."""
    text = tmux_capture_pane(pane, lines=10)
    for pat in PANE_ERROR_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def auto_pause_current_watcher(reason: str, watch_log: Path, state_file: Path, state: dict) -> None:
    """Record an auto-pause reason and stop the current watcher."""
    now = now_ts()
    next_state = dict(state)
    next_state["health_detail"] = f"auto-paused: {reason}"
    next_state["health_ts"] = now
    write_state(state_file, next_state)
    append_log(watch_log, f"pause: auto-pausing watcher ({reason})")
    os.kill(os.getpid(), signal.SIGSTOP)


def tmux_pane_exists(pane: str) -> bool:
    rc = run_tmux(["display-message", "-p", "-t", pane, "#{pane_id}"])
    return rc.returncode == 0


def tmux_window_name(pane: str) -> str:
    """Return the tmux window name for *pane*, or '' on failure."""
    rc = run_tmux(["display-message", "-p", "-t", pane, "#{window_name}"])
    if rc.returncode == 0 and rc.stdout:
        return rc.stdout.strip()
    return ""


def tmux_window_index(pane: str) -> str:
    """Return the tmux window index for *pane*, or '' on failure."""
    rc = run_tmux(["display-message", "-p", "-t", pane, "#{window_index}"])
    if rc.returncode == 0 and rc.stdout:
        return rc.stdout.strip()
    return ""


def tmux_pane_active(pane: str) -> bool:
    rc = run_tmux(["display-message", "-p", "-t", pane, "#{pane_active}"])
    return rc.returncode == 0 and rc.stdout.strip() == "1"


def tmux_pane_in_mode(pane: str) -> bool:
    rc = run_tmux(["display-message", "-p", "-t", pane, "#{pane_in_mode}"])
    return rc.returncode == 0 and rc.stdout.strip() == "1"


def tmux_cancel_mode_if_needed(pane: str) -> None:
    if tmux_pane_in_mode(pane):
        run_tmux(["send-keys", "-t", pane, "-X", "cancel"], capture_output=False)


def _tmux_send_once(pane: str, msg: str, enter_delay_secs: float) -> tuple[bool, str]:
    send_text = run_tmux(["send-keys", "-t", pane, "-l", msg])
    if enter_delay_secs > 0.0:
        time.sleep(enter_delay_secs)
    send_enter = run_tmux(["send-keys", "-t", pane, "C-m"])
    ok = send_text.returncode == 0 and send_enter.returncode == 0
    detail_parts = []
    if send_text.returncode != 0:
        detail_parts.append(f"send-text rc={send_text.returncode}")
    if send_enter.returncode != 0:
        detail_parts.append(f"send-enter rc={send_enter.returncode}")
    for label, proc in (("send-text", send_text), ("send-enter", send_enter)):
        for stream_name, stream in (("stderr", proc.stderr), ("stdout", proc.stdout)):
            text = (stream or "").strip()
            if text:
                detail_parts.append(f"{label} {stream_name}={text}")
    return ok, "; ".join(detail_parts)


def tmux_send(pane: str, msg: str, enter_delay_secs: float) -> tuple[bool, str]:
    # If user is browsing scrollback, leave copy mode before injecting text.
    tmux_cancel_mode_if_needed(pane)
    ok, detail = _tmux_send_once(pane, msg, enter_delay_secs)
    if ok:
        return True, ""

    # One retry after a best-effort mode cancel handles transient mode races.
    tmux_cancel_mode_if_needed(pane)
    ok_retry, detail_retry = _tmux_send_once(pane, msg, enter_delay_secs)
    if ok_retry:
        return True, ""
    if detail and detail_retry:
        return False, f"{detail}; retry: {detail_retry}"
    return False, detail_retry or detail


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(state, indent=2, sort_keys=True))
        os.replace(tmp, str(path))
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, UnboundLocalError):
            pass


def _thread_from_state_db_pid(pid: str, state_dir: Path = STATE_DIR) -> Optional[str]:
    """Return the most recent thread_id logged by the Codex process *pid*."""
    if not pid.isdigit() or not state_dir.is_dir():
        return None

    for db_path in sorted(state_dir.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            row = conn.execute(
                "select thread_id "
                "from logs "
                "where process_uuid like ? and thread_id is not null and thread_id != '' "
                "order by ts desc, ts_nanos desc "
                "limit 1",
                (f"pid:{pid}:%",),
            ).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()

        if not row:
            continue
        thread_id = str(row[0]).lower()
        if is_thread_id(thread_id):
            return thread_id
    return None


def thread_from_codex_pid(pid: str) -> Optional[str]:
    """If *pid* is a codex process, return its thread_id from local Codex state."""
    try:
        exe = os.readlink(f"/proc/{pid}/exe").rsplit("/", 1)[-1]
        if exe.removesuffix(" (deleted)") != "codex":
            return None
    except OSError:
        return None
    try:
        for entry in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{entry}")
            except OSError:
                continue
            m = ROLLOUT_RE.search(os.path.basename(target))
            if m:
                return m.group(1).lower()
    except OSError:
        pass
    return _thread_from_state_db_pid(pid)


def discover_thread_for_pane(pane: str) -> Optional[str]:
    """Discover the thread_id for the codex process running in *pane*."""
    rc = run_tmux(["list-panes", "-t", pane, "-F", "#{pane_pid}"])
    if rc.returncode != 0 or not rc.stdout.strip():
        return None
    try:
        result = subprocess.run(
            ["pstree", "-p", rc.stdout.strip()],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return None
    except FileNotFoundError:
        return None
    for pid in re.findall(r"\((\d+)\)", result.stdout):
        tid = thread_from_codex_pid(pid)
        if tid:
            return tid
    return None


def discover_thread_id(log_path: Path, pane: str = "") -> Optional[str]:
    # Best method: inspect the codex process running in the target pane.
    if pane:
        tid = discover_thread_for_pane(pane)
        if tid:
            return tid
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pane", default=os.environ.get("TMUX_PANE", ""))
    p.add_argument("--thread-id", default="auto")
    p.add_argument("--auto-rebind-idle-secs", type=float, default=20.0)
    p.add_argument("--message-file", default="")
    p.add_argument("--message", default="please continue")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--log-path", default=str(Path.home() / ".codex" / "log" / "codex-tui.log"))
    p.add_argument("--cooldown-secs", type=float, default=1.0)
    p.add_argument("--send-delay-secs", type=float, default=0.25)
    p.add_argument("--enter-delay-secs", type=float, default=0.15)
    p.add_argument("--require-pane-active", action="store_true")
    p.add_argument("--state-file", default="")
    p.add_argument("--watch-log", default="")
    args = p.parse_args()

    cwd = Path(args.cwd)
    if not args.pane:
        sys.stderr.write("auto_continue_logwatch: --pane is required (or TMUX_PANE env)\n")
        return 2
    if not tmux_pane_exists(args.pane):
        sys.stderr.write(f"auto_continue_logwatch: tmux pane not found: {args.pane}\n")
        return 2

    state_file = Path(args.state_file) if args.state_file else _session_state_path("unknown")
    watch_log = Path(args.watch_log) if args.watch_log else (cwd / ".codex" / "auto_continue_logwatch.log")
    codex_log = Path(args.log_path)

    msg = args.message
    if args.message_file:
        msg = Path(args.message_file).read_text(encoding="utf-8")
    # Strip comment lines (starting with #).
    msg = "\n".join(
        line for line in msg.splitlines() if not line.lstrip().startswith("#")
    ).strip()

    auto_mode = args.thread_id == "auto"
    watched_thread = args.thread_id
    if watched_thread == "auto":
        watched_thread = discover_thread_id(codex_log, pane=args.pane) or ""

    # Read initial state from session state file.
    state = read_state(state_file)
    last_sent_turn = str(state.get("last_sent_turn", ""))
    last_sent_thread = str(state.get("last_sent_thread", state.get("thread_id", "")))

    if not watched_thread:
        append_log(watch_log, "warn: could not auto-discover thread id yet; waiting for first event")
    else:
        append_log(watch_log, f"watch: pane={args.pane} thread_id={watched_thread}")

    last_send_time = 0.0
    watched_last_event_at = 0.0

    codex_fh = None
    rollout_fh = None
    rollout_path: Optional[Path] = None
    last_rollout_scan = 0.0
    health = "ok"
    health_detail = ""
    last_health_check = 0.0
    rollout_channel_closed = False
    rollout_channel_closed_at = 0.0
    codex_log_completion_seen = False
    watcher_start = time.time()

    def update_health(now: float) -> None:
        nonlocal health, health_detail
        new_health, new_detail = compute_health(
            watched_thread=watched_thread,
            rollout_path=rollout_path,
            watcher_start=watcher_start,
            now=now,
            rollout_channel_closed=rollout_channel_closed,
            rollout_channel_closed_at=rollout_channel_closed_at,
            codex_log_completion_seen=codex_log_completion_seen,
        )
        if new_health == health and new_detail == health_detail:
            return
        health = new_health
        health_detail = new_detail
        if health_detail:
            append_log(watch_log, f"health: {health} - {health_detail}")
        else:
            append_log(watch_log, f"health: {health}")

    # Initialize session state file.
    if watched_thread and is_thread_id(watched_thread):
        state["thread_id"] = watched_thread
        wn = tmux_window_name(args.pane)
        if wn:
            state["name"] = wn
        if "message" not in state:
            state["message"] = msg
        write_state(state_file, state)

    # Try to locate the rollout file for the initial thread right away.
    if watched_thread:
        rollout_path = find_rollout_file(watched_thread, SESSIONS_DIR)
        if rollout_path:
            append_log(watch_log, f"rollout: watching {rollout_path.name}")

    # Check if codex already finished its turn before we started.  Scan the
    # tail of the rollout for a task_complete that hasn't been followed by a
    # user_message (which would mean a new turn already started).  If found,
    # inject it so the first loop iteration sends the continue.
    pending_initial_event: Optional[tuple[str, str, str]] = None
    if rollout_path and rollout_path.exists() and watched_thread:
        pending_initial_event = _check_rollout_tail_for_pending(
            rollout_path, watched_thread, last_sent_turn, last_sent_thread,
            watch_log,
        )

    # --- Set up inotify file watching (falls back to polling if unavailable) ---
    _wake_event = threading.Event()
    _watched_paths: set[str] = set()
    _watched_dirs: set[str] = set()
    _observer = None

    def _add_inotify_watch(file_path: Path) -> None:
        """Register a file for inotify monitoring."""
        nonlocal _observer
        if _WatchdogObserver is None:
            return
        path_str = str(file_path.resolve())
        if path_str in _watched_paths:
            return
        _watched_paths.add(path_str)
        dir_str = str(file_path.resolve().parent)
        if dir_str in _watched_dirs:
            return
        if not os.path.isdir(dir_str):
            return
        if _observer is None:
            _observer = _WatchdogObserver()
            _observer.daemon = True
            _observer._acw_handler = _WakeHandler(_wake_event, _watched_paths)
            _observer.start()
        try:
            _observer.schedule(_observer._acw_handler, dir_str, recursive=False)
        except OSError:
            return
        _watched_dirs.add(dir_str)

    codex_log.parent.mkdir(parents=True, exist_ok=True)
    _add_inotify_watch(codex_log)
    if rollout_path:
        _add_inotify_watch(rollout_path)

    while True:
        _wake_event.clear()

        events: list[tuple[str, str, str, str]] = []
        tnow = time.time()

        # Inject the pending startup event on the first iteration only.
        if pending_initial_event is not None:
            events.append((*pending_initial_event, "rollout"))
            pending_initial_event = None

        # --- Poll codex-tui.log ---
        if codex_fh is not None or codex_log.exists():
            if codex_fh is None:
                codex_fh = codex_log.open("r", encoding="utf-8", errors="ignore")
                codex_fh.seek(0, os.SEEK_END)
            while True:
                line = codex_fh.readline()
                if not line:
                    break
                event = parse_codex_log_event(line)
                if event:
                    events.append((*event, "codex_log"))
                elif not rollout_channel_closed and watched_thread:
                    cm = CHANNEL_CLOSED_RE.search(line)
                    if cm and cm.group(1) == watched_thread:
                        rollout_channel_closed = True
                        rollout_channel_closed_at = time.time()
                        update_health(time.time())
            # Detect truncation/rotation.
            try:
                if codex_fh.tell() > codex_log.stat().st_size:
                    codex_fh.close()
                    codex_fh = None
            except FileNotFoundError:
                codex_fh.close()
                codex_fh = None

        # --- Discover/refresh rollout file periodically ---
        if rollout_path is None and tnow - last_rollout_scan > 5.0:
            last_rollout_scan = tnow
            if watched_thread:
                found = find_rollout_file(watched_thread, SESSIONS_DIR)
                if found:
                    rollout_path = found
                    append_log(watch_log, f"rollout: watching {rollout_path.name}")
                    _add_inotify_watch(rollout_path)

        # --- Poll rollout JSONL ---
        if rollout_path is not None and (rollout_fh is not None or rollout_path.exists()):
            if rollout_fh is None:
                rollout_fh = rollout_path.open("r", encoding="utf-8", errors="ignore")
                rollout_fh.seek(0, os.SEEK_END)
            while True:
                line = rollout_fh.readline()
                if not line:
                    break
                turn_id = parse_rollout_event(line)
                if turn_id and watched_thread:
                    events.append((watched_thread, turn_id, "false", "rollout"))
            # Detect truncation/rotation.
            try:
                if rollout_fh.tell() > rollout_path.stat().st_size:
                    rollout_fh.close()
                    rollout_fh = None
            except FileNotFoundError:
                rollout_fh.close()
                rollout_fh = None

        # --- Process collected events ---
        for thread_id, turn_id, needs_follow_up, source in events:
            tnow = time.time()

            if auto_mode:
                if not watched_thread:
                    watched_thread = thread_id
                    watched_last_event_at = tnow
                    rollout_channel_closed = False
                    rollout_channel_closed_at = 0.0
                    codex_log_completion_seen = source == "codex_log"
                    append_log(watch_log, f"watch: auto-selected thread_id={watched_thread}")
                elif thread_id == watched_thread:
                    watched_last_event_at = tnow
                elif watched_last_event_at == 0.0 or (tnow - watched_last_event_at) > max(0.0, args.auto_rebind_idle_secs):
                    prev = watched_thread
                    watched_thread = thread_id
                    watched_last_event_at = tnow
                    rollout_channel_closed = False
                    rollout_channel_closed_at = 0.0
                    codex_log_completion_seen = source == "codex_log"
                    # Reset rollout tracking for the new thread.
                    if rollout_fh is not None:
                        rollout_fh.close()
                        rollout_fh = None
                    rollout_path = None
                    last_rollout_scan = 0.0
                    append_log(
                        watch_log,
                        (
                            f"watch: auto-rebind thread_id={prev} -> {watched_thread} "
                            f"(idle>{args.auto_rebind_idle_secs:.1f}s)"
                        ),
                    )

            if thread_id != watched_thread:
                continue
            if source == "codex_log":
                codex_log_completion_seen = True
                update_health(tnow)
            if needs_follow_up != "false":
                continue
            if turn_id == last_sent_turn and thread_id == last_sent_thread:
                continue

            if args.require_pane_active and not tmux_pane_active(args.pane):
                append_log(watch_log, f"skip: pane inactive turn={turn_id}")
                continue

            if tnow - last_send_time < max(0.0, args.cooldown_secs):
                append_log(watch_log, f"skip: cooldown turn={turn_id}")
                continue

            if args.send_delay_secs > 0.0:
                time.sleep(args.send_delay_secs)

            pane_error = check_pane_for_errors(args.pane)
            if pane_error:
                auto_pause_current_watcher(pane_error, watch_log, state_file, state)
                continue

            ok, send_error = tmux_send(args.pane, msg, max(0.0, args.enter_delay_secs))
            if ok:
                last_send_time = tnow
                last_sent_turn = turn_id
                last_sent_thread = thread_id
                now = now_ts()
                state["last_sent_turn"] = turn_id
                state["last_sent_thread"] = thread_id
                state["thread_id"] = watched_thread
                state["last_continue_at"] = now
                state["health"] = health
                state["health_detail"] = health_detail
                write_state(state_file, state)
                append_log(watch_log, f"continue: sent turn={turn_id} thread={thread_id}")
            else:
                if send_error:
                    append_log(
                        watch_log,
                        (
                            "error: tmux send failed "
                            f"turn={turn_id} thread={thread_id} detail={send_error}"
                        ),
                    )
                else:
                    append_log(
                        watch_log,
                        f"error: tmux send failed turn={turn_id} thread={thread_id}",
                    )

        # --- Periodic health check ---
        if tnow - last_health_check > HEALTH_CHECK_INTERVAL:
            last_health_check = tnow

            # Re-discover thread from pane's codex process in case the
            # session was restarted with a new thread.
            if auto_mode and args.pane:
                live_tid = discover_thread_for_pane(args.pane)
                if live_tid and live_tid != watched_thread:
                    prev_thread = watched_thread
                    watched_thread = live_tid
                    watched_last_event_at = tnow
                    if rollout_fh is not None:
                        rollout_fh.close()
                        rollout_fh = None
                    rollout_path = find_rollout_file(watched_thread, SESSIONS_DIR)
                    if rollout_path:
                        append_log(watch_log, f"rollout: watching {rollout_path.name}")
                        _add_inotify_watch(rollout_path)
                    last_rollout_scan = tnow
                    rollout_channel_closed = False
                    rollout_channel_closed_at = 0.0
                    codex_log_completion_seen = False
                    append_log(
                        watch_log,
                        f"watch: pane-rebind thread_id={prev_thread} -> {watched_thread}",
                    )
                    # Update state file for the new thread.
                    state_file = _session_state_path(watched_thread)
                    state = {"thread_id": watched_thread, "message": msg}
                    wn = tmux_window_name(args.pane)
                    if wn:
                        state["name"] = wn
                    write_state(state_file, state)

            update_health(tnow)

            now = now_ts()
            state["health"] = health
            state["health_detail"] = health_detail
            state["health_ts"] = now
            write_state(state_file, state)

        # Wait for inotify file notification, or fall back to timeout for
        # periodic tasks (rollout discovery, health checks).
        if not _wake_event.is_set():
            if _observer is None:
                # No inotify — poll at a reasonable rate.
                timeout = 1.0
            elif rollout_path is None:
                # Need periodic scan to discover rollout file.
                timeout = 5.0
            else:
                timeout = HEALTH_CHECK_INTERVAL
            _wake_event.wait(timeout=timeout)


if __name__ == "__main__":
    raise SystemExit(main())
