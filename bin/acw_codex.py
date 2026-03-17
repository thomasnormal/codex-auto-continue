from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from typing import Literal
from typing import Optional

import acw_config
from acw_ids import is_thread_id
from acw_ids import THREAD_ID_RE


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

PSTREE_PROCESS_PID_RE = re.compile(r"(?<!\{)[A-Za-z0-9_.+-]+\((\d+)\)")

STATE_DIR = acw_config.state_dir_path()


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


def tmux_pane_in_mode(
    pane: str,
    *,
    run_tmux_fn: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> bool:
    if run_tmux_fn is None:
        run_tmux_fn = run_tmux
    rc = run_tmux_fn(["display-message", "-p", "-t", pane, "#{pane_in_mode}"])
    return rc.returncode == 0 and rc.stdout.strip() == "1"


def tmux_cancel_mode_if_needed(
    pane: str,
    *,
    tmux_pane_in_mode_fn: Callable[[str], bool] | None = None,
    run_tmux_fn: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> None:
    if tmux_pane_in_mode_fn is None:
        tmux_pane_in_mode_fn = tmux_pane_in_mode
    if run_tmux_fn is None:
        run_tmux_fn = run_tmux
    if tmux_pane_in_mode_fn(pane):
        run_tmux_fn(["send-keys", "-t", pane, "-X", "cancel"], capture_output=False)


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
    *,
    run_tmux_fn: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> TmuxSendResult:
    if run_tmux_fn is None:
        run_tmux_fn = run_tmux
    send_text = run_tmux_fn(["send-keys", "-t", pane, "-l", msg])
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

    send_enter = run_tmux_fn(["send-keys", "-t", pane, "C-m"])
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
    *,
    run_tmux_fn: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
    cancel_mode_if_needed_fn: Callable[[str], None] | None = None,
) -> TmuxSendResult:
    if run_tmux_fn is None:
        run_tmux_fn = run_tmux
    if cancel_mode_if_needed_fn is None:
        cancel_mode_if_needed_fn = tmux_cancel_mode_if_needed
    cancel_mode_if_needed_fn(pane)
    result = _tmux_send_once(
        pane,
        msg,
        enter_delay_secs,
        interrupt_checker=interrupt_checker,
        run_tmux_fn=run_tmux_fn,
    )
    if result.status != "error":
        return result

    cancel_mode_if_needed_fn(pane)
    retry = _tmux_send_once(
        pane,
        msg,
        enter_delay_secs,
        interrupt_checker=interrupt_checker,
        run_tmux_fn=run_tmux_fn,
    )
    if retry.status != "error":
        return retry
    if result.detail and retry.detail:
        return TmuxSendResult("error", f"{result.detail}; retry: {retry.detail}")
    return TmuxSendResult("error", retry.detail or result.detail)


def _process_start_epoch(pid: str) -> Optional[float]:
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


def _thread_from_state_db_pid(
    pid: str,
    state_dir: Path = STATE_DIR,
    *,
    process_start_epoch_fn: Callable[[str], Optional[float]] | None = None,
) -> Optional[str]:
    if not pid.isdigit() or not state_dir.is_dir():
        return None
    if process_start_epoch_fn is None:
        process_start_epoch_fn = _process_start_epoch
    started_at = process_start_epoch_fn(pid)
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


def _thread_id_from_cmdline_fields(fields: list[str]) -> Optional[str]:
    for idx, field in enumerate(fields):
        if field == "resume" and idx + 1 < len(fields):
            candidate = fields[idx + 1].lower()
            if is_thread_id(candidate):
                return candidate

    candidates = sorted({field.lower() for field in fields if is_thread_id(field)})
    if len(candidates) == 1:
        return candidates[0]
    return None


def _thread_id_from_text_candidates(candidates: list[str]) -> Optional[str]:
    matches = sorted(
        {
            match.group(0).lower()
            for text in candidates
            for match in THREAD_ID_RE.finditer(text)
        }
    )
    if len(matches) == 1:
        return matches[0]
    return None


def _thread_from_procfs_cmdline(pid: str) -> Optional[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    fields = [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\0") if chunk]
    return _thread_id_from_cmdline_fields(fields)


def _thread_from_procfs_open_files(pid: str) -> Optional[str]:
    try:
        fd_names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return None

    targets: list[str] = []
    for fd_name in fd_names:
        try:
            targets.append(os.readlink(f"/proc/{pid}/fd/{fd_name}"))
        except OSError:
            continue
    return _thread_id_from_text_candidates(targets)


def _thread_from_codex_procfs(pid: str) -> Optional[str]:
    return _thread_from_procfs_cmdline(pid) or _thread_from_procfs_open_files(pid)


def thread_from_codex_pid(pid: str) -> Optional[str]:
    exe_name = ""
    try:
        exe_name = os.readlink(f"/proc/{pid}/exe").rsplit("/", 1)[-1]
    except OSError:
        exe_name = ""
    exe_name = exe_name.removesuffix(" (deleted)")
    if exe_name != "codex":
        try:
            proc_name = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            return None
        if proc_name != "codex":
            return None
    return _thread_from_state_db_pid(pid) or _thread_from_codex_procfs(pid)


def discover_thread_for_pane(
    pane: str,
    *,
    run_tmux_fn: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
    subprocess_run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    thread_from_codex_pid_fn: Callable[[str], Optional[str]] | None = None,
) -> Optional[str]:
    if run_tmux_fn is None:
        run_tmux_fn = run_tmux
    if subprocess_run_fn is None:
        subprocess_run_fn = subprocess.run
    if thread_from_codex_pid_fn is None:
        thread_from_codex_pid_fn = thread_from_codex_pid
    rc = run_tmux_fn(["display-message", "-p", "-t", pane, "#{pane_pid}"])
    if rc.returncode != 0 or not rc.stdout.strip():
        return None
    try:
        result = subprocess_run_fn(
            ["pstree", "-p", rc.stdout.strip()],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
    except FileNotFoundError:
        return None
    for pid in PSTREE_PROCESS_PID_RE.findall(result.stdout):
        tid = thread_from_codex_pid_fn(pid)
        if tid:
            return tid
    return None


def discover_thread_id(log_path: Path, pane: str = "") -> Optional[str]:
    del log_path
    if pane:
        tid = discover_thread_for_pane(pane)
        if tid:
            return tid
    return None
