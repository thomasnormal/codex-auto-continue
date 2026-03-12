#!/usr/bin/env python3
"""Auto-continue watcher that does not use Codex notify hooks.

It tails ``~/.codex/log/codex-tui.log`` and sends a prompt to a tmux pane when
Codex emits a turn-complete signal for a watched thread/turn. Older Codex
builds log ``needs_follow_up=false`` and newer builds log
``codex_core::tasks: close``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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
from typing import Callable, Literal, Optional

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

INTERRUPT_RE = re.compile(
    r"session_loop\{thread_id=([0-9a-f\-]+)\}.*"
    r"codex_core::codex: interrupt received: abort current task, if any\b"
)

STATE_DIR = Path.home() / ".codex"


def _session_state_path(thread_id: str) -> Path:
    return STATE_DIR / f"acw_session.{thread_id}.json"

THREAD_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

THREAD_LINE_RE = re.compile(r"session_loop\{thread_id=([0-9a-f\-]+)\}")

def is_thread_id(s: str) -> bool:
    return bool(THREAD_ID_RE.fullmatch(s))

HEALTH_CHECK_INTERVAL = 30.0   # seconds between periodic health checks
STARTUP_GRACE_SECS = 60.0


@dataclass(frozen=True)
class TmuxSendResult:
    status: Literal["ok", "error", "interrupted"]
    detail: str = ""


@dataclass(frozen=True)
class CodexLogEvent:
    kind: Literal["completion", "interrupt"]
    thread_id: str
    turn_id: str = ""
    detail: str = ""


def parse_codex_log_event(line: str) -> Optional[tuple[str, str, str]]:
    """Return a normalized completion event from codex-tui.log."""
    m = EVENT_RE.search(line)
    if m:
        return m.group(1), m.group(2), m.group(3)

    m = TASK_CLOSE_RE.search(line)
    if m:
        return m.group(1), m.group(2), "false"

    return None


def parse_codex_log_interrupt(line: str) -> Optional[str]:
    m = INTERRUPT_RE.search(line)
    if not m:
        return None
    return m.group(1)


def compute_health(
    *,
    watched_thread: str,
    watcher_start: float,
    now: float,
    codex_log_exists: bool,
) -> tuple[str, str]:
    """Compute watcher health from pane-local state and codex-tui.log availability."""
    if now - watcher_start <= STARTUP_GRACE_SECS:
        return "ok", ""
    if not watched_thread:
        return "warn", "waiting for thread id"
    if not codex_log_exists:
        return "warn", "codex log not found"
    return "ok", ""


def _thread_id_from_codex_log_line(line: str) -> Optional[str]:
    m = THREAD_LINE_RE.search(line)
    if not m:
        return None
    return m.group(1).lower()


def check_codex_log_tail_for_pending(
    log_path: Path,
    watched_thread: str,
    last_sent_turn: str,
    last_sent_thread: str,
    watch_log: Path,
) -> Optional[tuple[str, str, str]]:
    """Return a pending completion if the latest thread activity is a completion."""
    try:
        file_size = log_path.stat().st_size
    except OSError:
        return None
    if file_size == 0:
        return None

    tail_bytes = min(file_size, 2 * 1024 * 1024)
    try:
        with log_path.open("rb") as f:
            if tail_bytes < file_size:
                f.seek(-tail_bytes, os.SEEK_END)
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None

    for line in reversed(tail.splitlines()):
        thread_id = _thread_id_from_codex_log_line(line)
        if thread_id != watched_thread:
            continue
        event = parse_codex_log_event(line)
        if not event:
            return None
        _, turn_id, needs_follow_up = event
        if needs_follow_up != "false":
            return None
        if turn_id == last_sent_turn and watched_thread == last_sent_thread:
            return None
        append_log(
            watch_log,
            f"startup: found pending completion turn={turn_id}",
        )
        return (watched_thread, turn_id, "false")
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

    # Recover from stale socket state by retrying against the default server.
    if socket_path or os.environ.get("TMUX"):
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        return subprocess.run(
            ["tmux", *args],
            capture_output=capture_output,
            text=True,
            check=False,
            env=env,
        )

    return rc


PANE_INTERRUPT_PATTERNS = [
    re.compile(r"conversation interrupted", re.IGNORECASE),
    re.compile(r"model interrupted to submit steer instructions", re.IGNORECASE),
]

PROMPT_MARKER_RE = re.compile(r"(?m)^› ")


PANE_ERROR_PATTERNS = [
    re.compile(r"unauthorized|authentication.*(failed|error)", re.IGNORECASE),
    re.compile(r"something went wrong\? hit /feedback", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"try again at\b", re.IGNORECASE),
    re.compile(r"exceeded.*quota", re.IGNORECASE),
    re.compile(r"billing", re.IGNORECASE),
]


def tmux_capture_pane(pane: str, lines: int = 20) -> str:
    """Capture recent visible lines from a tmux pane."""
    rc = run_tmux(["capture-pane", "-t", pane, "-p", "-S", f"-{lines}"])
    if rc.returncode == 0:
        return rc.stdout or ""
    return ""


def _check_pane_for_patterns(pane: str, patterns: list[re.Pattern[str]]) -> Optional[str]:
    text = tmux_capture_pane(pane, lines=20)
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def check_pane_for_interrupt(pane: str) -> Optional[str]:
    """Return a fresh interrupt banner from the pane, or None.

    Interrupt banners linger in scrollback after the user submits a new prompt.
    Treat them as active only when they appear after the most recent visible
    prompt marker.
    """
    text = tmux_capture_pane(pane, lines=20)
    last_prompt = None
    for match in PROMPT_MARKER_RE.finditer(text):
        last_prompt = match.start()
    for pat in PANE_INTERRUPT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        if last_prompt is not None and m.start() < last_prompt:
            continue
        return m.group(0)
    return None


def check_pane_for_errors(pane: str) -> Optional[str]:
    """Return the first matched non-interrupt error string from the pane, or None."""
    if check_pane_for_interrupt(pane):
        return None
    return _check_pane_for_patterns(pane, PANE_ERROR_PATTERNS)


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


def tmux_pane_cwd(pane: str) -> str:
    """Return the current working directory for *pane*, or '' on failure."""
    rc = run_tmux(["display-message", "-p", "-t", pane, "#{pane_current_path}"])
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


def _interrupt_reason(
    interrupt_checker: Optional[Callable[[], Optional[str]]],
) -> Optional[str]:
    if interrupt_checker is None:
        return None
    return interrupt_checker()


def _tmux_send_once(
    pane: str,
    msg: str,
    enter_delay_secs: float,
    interrupt_checker: Optional[Callable[[], Optional[str]]] = None,
) -> TmuxSendResult:
    send_text = run_tmux(["send-keys", "-t", pane, "-l", msg])
    if send_text.returncode != 0:
        detail_parts = [f"send-text rc={send_text.returncode}"]
        for stream_name, stream in (("stderr", send_text.stderr), ("stdout", send_text.stdout)):
            text = (stream or "").strip()
            if text:
                detail_parts.append(f"send-text {stream_name}={text}")
        return TmuxSendResult("error", "; ".join(detail_parts))

    reason = _interrupt_reason(interrupt_checker)
    if reason:
        return TmuxSendResult("interrupted", reason)

    if enter_delay_secs > 0.0:
        time.sleep(enter_delay_secs)
    reason = _interrupt_reason(interrupt_checker)
    if reason:
        return TmuxSendResult("interrupted", reason)

    send_enter = run_tmux(["send-keys", "-t", pane, "C-m"])
    if send_enter.returncode == 0:
        return TmuxSendResult("ok")

    detail_parts = [f"send-enter rc={send_enter.returncode}"]
    for stream_name, stream in (("stderr", send_enter.stderr), ("stdout", send_enter.stdout)):
        text = (stream or "").strip()
        if text:
            detail_parts.append(f"send-enter {stream_name}={text}")
    return TmuxSendResult("error", "; ".join(detail_parts))


def tmux_send(
    pane: str,
    msg: str,
    enter_delay_secs: float,
    interrupt_checker: Optional[Callable[[], Optional[str]]] = None,
) -> TmuxSendResult:
    # If user is browsing scrollback, leave copy mode before injecting text.
    tmux_cancel_mode_if_needed(pane)
    result = _tmux_send_once(pane, msg, enter_delay_secs, interrupt_checker=interrupt_checker)
    if result.status != "error":
        return result

    # One retry after a best-effort mode cancel handles transient mode races.
    tmux_cancel_mode_if_needed(pane)
    retry = _tmux_send_once(pane, msg, enter_delay_secs, interrupt_checker=interrupt_checker)
    if retry.status != "error":
        return retry
    if result.detail and retry.detail:
        return TmuxSendResult("error", f"{result.detail}; retry: {retry.detail}")
    return TmuxSendResult("error", retry.detail or result.detail)


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


def _process_start_epoch(pid: str) -> Optional[float]:
    """Return the wall-clock start time for *pid* using /proc metadata."""
    if not pid.isdigit():
        return None
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        start_ticks = int(stat_fields[21])
        uptime_secs = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None
    hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    boot_time = time.time() - uptime_secs
    return boot_time + (start_ticks / hz)


def thread_times_from_state_db(thread_id: str, state_dir: Path = STATE_DIR) -> tuple[Optional[int], Optional[int]]:
    """Return ``(started_at, last_activity_at)`` for a thread from local Codex state."""
    if not is_thread_id(thread_id) or not state_dir.is_dir():
        return None, None

    best: tuple[Optional[int], Optional[int]] = (None, None)
    best_last = -1
    for db_path in sorted(state_dir.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            thread_row = conn.execute(
                "select created_at, updated_at from threads where id = ?",
                (thread_id,),
            ).fetchone()
        except sqlite3.Error:
            thread_row = None
        try:
            log_row = conn.execute(
                "select min(ts), max(ts) from logs where thread_id = ?",
                (thread_id,),
            ).fetchone()
        except sqlite3.Error:
            log_row = None
        finally:
            conn.close()

        thread_created = int(thread_row[0]) if thread_row and thread_row[0] is not None else None
        thread_updated = int(thread_row[1]) if thread_row and thread_row[1] is not None else None
        log_first = int(log_row[0]) if log_row and log_row[0] is not None else None
        log_last = int(log_row[1]) if log_row and log_row[1] is not None else None

        started_at = thread_created if thread_created is not None else log_first
        last_activity_at = log_last if log_last is not None else thread_updated
        if last_activity_at is None:
            last_activity_at = thread_updated
        if started_at is None and last_activity_at is None:
            continue

        candidate_last = last_activity_at if last_activity_at is not None else -1
        if candidate_last > best_last:
            best = (started_at, last_activity_at)
            best_last = candidate_last

    return best


def _thread_from_state_db_cwd(
    cwd: str,
    process_started_at: Optional[float],
    state_dir: Path = STATE_DIR,
) -> Optional[str]:
    """Return the thread whose recorded cwd/start time best matches the pane process."""
    if not cwd or not state_dir.is_dir():
        return None

    started_at = int(process_started_at) if process_started_at is not None else None
    for db_path in sorted(state_dir.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            query = (
                "select id "
                "from threads "
                "where cwd = ? and id is not null and id != '' "
            )
            params: list[object] = [cwd]
            if started_at is not None:
                query += "and created_at between ? and ? "
                params.extend([started_at - 120, started_at + 120])
                query += "order by abs(created_at - ?) asc, updated_at desc limit 1"
                params.append(started_at)
            else:
                query += "order by updated_at desc, created_at desc limit 1"
            row = conn.execute(query, params).fetchone()
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


def _thread_from_state_db_pid(pid: str, state_dir: Path = STATE_DIR) -> Optional[str]:
    """Return the most recent thread_id logged by the Codex process *pid*."""
    if not pid.isdigit() or not state_dir.is_dir():
        return None
    started_at = _process_start_epoch(pid)
    min_ts = int(started_at) if started_at is not None else None

    for db_path in sorted(state_dir.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            query = (
                "select thread_id "
                "from logs "
                "where process_uuid like ? and thread_id is not null and thread_id != '' "
            )
            params: list[object] = [f"pid:{pid}:%"]
            if min_ts is not None:
                query += "and ts >= ? "
                params.append(min_ts)
            query += "order by ts desc, ts_nanos desc limit 1"
            row = conn.execute(query, params).fetchone()
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
    codex_pids: list[str] = []
    for pid in re.findall(r"\((\d+)\)", result.stdout):
        codex_pids.append(pid)
        tid = thread_from_codex_pid(pid)
        if tid:
            return tid
    pane_cwd = tmux_pane_cwd(pane)
    for pid in codex_pids:
        started_at = _process_start_epoch(pid)
        tid = _thread_from_state_db_cwd(pane_cwd, started_at)
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
    p.add_argument("--tmux-socket", default=os.environ.get("AUTO_CONTINUE_TMUX_SOCKET", ""))
    p.add_argument("--message-file", default="")
    p.add_argument("--message", default="please continue")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--log-path", default=str(Path.home() / ".codex" / "log" / "codex-tui.log"))
    p.add_argument("--cooldown-secs", type=float, default=1.0)
    p.add_argument("--send-delay-secs", type=float, default=0.25)
    p.add_argument("--enter-delay-secs", type=float, default=0.15)
    p.add_argument("--state-file", default="")
    p.add_argument("--watch-log", default="")
    args = p.parse_args()

    if args.tmux_socket:
        os.environ["AUTO_CONTINUE_TMUX_SOCKET"] = args.tmux_socket

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
    last_handled_turn = str(state.get("last_handled_turn", state.get("last_sent_turn", "")))
    last_handled_thread = str(
        state.get("last_handled_thread", state.get("last_sent_thread", state.get("thread_id", "")))
    )

    if not watched_thread:
        append_log(watch_log, "warn: could not auto-discover thread id yet; waiting for first event")
    else:
        append_log(watch_log, f"watch: pane={args.pane} thread_id={watched_thread}")

    last_send_time = 0.0
    codex_fh = None
    health = "ok"
    health_detail = ""
    last_health_check = 0.0
    watcher_start = time.time()
    interrupt_skip_armed = False

    def mark_handled_turn(turn_id: str, thread_id: str) -> None:
        nonlocal last_handled_turn, last_handled_thread
        last_handled_turn = turn_id
        last_handled_thread = thread_id
        state["last_handled_turn"] = turn_id
        state["last_handled_thread"] = thread_id

    def skip_interrupted_turn(turn_id: str, thread_id: str, reason: str) -> None:
        nonlocal interrupt_skip_armed
        interrupt_skip_armed = False
        mark_handled_turn(turn_id, thread_id)
        write_state(state_file, state)
        append_log(
            watch_log,
            f"skip: interrupted turn={turn_id} thread={thread_id} reason={reason}",
        )

    def update_health(now: float) -> None:
        nonlocal health, health_detail
        new_health, new_detail = compute_health(
            watched_thread=watched_thread,
            watcher_start=watcher_start,
            now=now,
            codex_log_exists=codex_log.exists(),
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

    # Check if codex already finished its turn before we started. Scan the tail
    # of codex-tui.log and only replay a completion when it is the latest known
    # activity for this thread.
    pending_initial_event: Optional[tuple[str, str, str]] = None
    if watched_thread and codex_log.exists():
        pending_initial_event = check_codex_log_tail_for_pending(
            codex_log, watched_thread, last_handled_turn, last_handled_thread,
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

    while True:
        _wake_event.clear()

        events: list[CodexLogEvent] = []
        tnow = time.time()

        # Inject the pending startup event on the first iteration only.
        if pending_initial_event is not None:
            events.append(CodexLogEvent("completion", *pending_initial_event))
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
                interrupt_thread = parse_codex_log_interrupt(line)
                if interrupt_thread:
                    events.append(
                        CodexLogEvent(
                            "interrupt",
                            interrupt_thread,
                            detail="Conversation interrupted",
                        )
                    )
                    continue
                event = parse_codex_log_event(line)
                if event:
                    events.append(CodexLogEvent("completion", *event))
            # Detect truncation/rotation.
            try:
                if codex_fh.tell() > codex_log.stat().st_size:
                    codex_fh.close()
                    codex_fh = None
            except FileNotFoundError:
                codex_fh.close()
                codex_fh = None

        # --- Process collected events ---
        for event in events:
            tnow = time.time()
            thread_id = event.thread_id

            if auto_mode:
                if not watched_thread:
                    watched_thread = thread_id
                    append_log(watch_log, f"watch: auto-selected thread_id={watched_thread}")

            if thread_id != watched_thread:
                continue

            if event.kind == "interrupt":
                if not interrupt_skip_armed:
                    interrupt_skip_armed = True
                    append_log(watch_log, f"skip: armed after interrupt thread={thread_id}")
                continue

            turn_id = event.turn_id
            needs_follow_up = event.detail
            if needs_follow_up != "false":
                continue
            if turn_id == last_handled_turn and thread_id == last_handled_thread:
                continue

            if interrupt_skip_armed:
                skip_interrupted_turn(turn_id, thread_id, "Conversation interrupted")
                continue

            pane_interrupt = check_pane_for_interrupt(args.pane)
            if pane_interrupt:
                skip_interrupted_turn(turn_id, thread_id, pane_interrupt)
                continue

            pane_error = check_pane_for_errors(args.pane)
            if pane_error:
                auto_pause_current_watcher(pane_error, watch_log, state_file, state)
                continue

            if tnow - last_send_time < max(0.0, args.cooldown_secs):
                append_log(watch_log, f"skip: cooldown turn={turn_id}")
                continue

            if args.send_delay_secs > 0.0:
                time.sleep(args.send_delay_secs)

            pane_interrupt = check_pane_for_interrupt(args.pane)
            if pane_interrupt:
                skip_interrupted_turn(turn_id, thread_id, pane_interrupt)
                continue

            pane_error = check_pane_for_errors(args.pane)
            if pane_error:
                auto_pause_current_watcher(pane_error, watch_log, state_file, state)
                continue

            send_result = tmux_send(
                args.pane,
                msg,
                max(0.0, args.enter_delay_secs),
                interrupt_checker=lambda: check_pane_for_interrupt(args.pane),
            )
            if send_result.status == "ok":
                last_send_time = tnow
                mark_handled_turn(turn_id, thread_id)
                now = now_ts()
                state["thread_id"] = watched_thread
                state["last_continue_at"] = now
                state["health"] = health
                state["health_detail"] = health_detail
                write_state(state_file, state)
                append_log(watch_log, f"continue: sent turn={turn_id} thread={thread_id}")
            elif send_result.status == "interrupted":
                skip_interrupted_turn(turn_id, thread_id, send_result.detail)
            else:
                if send_result.detail:
                    append_log(
                        watch_log,
                        (
                            "error: tmux send failed "
                            f"turn={turn_id} thread={thread_id} detail={send_result.detail}"
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
        # periodic health checks.
        if not _wake_event.is_set():
            if _observer is None:
                timeout = 1.0
            else:
                timeout = HEALTH_CHECK_INTERVAL
            _wake_event.wait(timeout=timeout)


if __name__ == "__main__":
    raise SystemExit(main())
