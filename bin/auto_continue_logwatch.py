#!/usr/bin/env python3
"""Auto-continue watcher that does not use Codex notify hooks.

It tails ~/.codex/log/codex-tui.log and sends a prompt to a tmux pane whenever
Codex emits a turn-complete signal (`needs_follow_up=false`) for a watched
thread/turn.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

EVENT_RE = re.compile(
    r"session_loop\{thread_id=([0-9a-f\-]+)\}.*post sampling token usage "
    r"turn_id=([^ ]+).*needs_follow_up=(true|false)"
)

SESSIONS_DIR = Path.home() / ".codex" / "sessions"

ROLLOUT_RE = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def find_rollout_file(thread_id: str, sessions_dir: Path) -> Optional[Path]:
    """Glob for the rollout JSONL file matching *thread_id*."""
    if not sessions_dir.is_dir():
        return None
    pattern = f"*/*/*/rollout-*-{thread_id}.jsonl"
    matches = list(sessions_dir.glob(pattern))
    if not matches:
        return None
    # Most recent first (there should normally be exactly one).
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


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
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"[{now_ts()}] {msg}\n")


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


def tmux_pane_exists(pane: str) -> bool:
    rc = run_tmux(["display-message", "-p", "-t", pane, "#{pane_id}"])
    return rc.returncode == 0


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def discover_thread_id(log_path: Path) -> Optional[str]:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        lines = []
    # Pick most recent thread id with a post-sampling line.
    for line in reversed(lines[-4000:]):
        m = EVENT_RE.search(line)
        if m:
            return m.group(1)
    # Fallback: discover from rollout session files (new-codex path).
    result = find_latest_rollout(SESSIONS_DIR)
    if result:
        return result[0]
    return None


def pane_key(pane: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", pane)


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

    pause_file = cwd / ".codex" / "AUTO_CONTINUE_PAUSE"
    pane_pause_file = cwd / ".codex" / f"AUTO_CONTINUE_PAUSE.{pane_key(args.pane)}"
    state_file = Path(args.state_file) if args.state_file else (cwd / ".codex" / "auto_continue_logwatch.state.local.json")
    watch_log = Path(args.watch_log) if args.watch_log else (cwd / ".codex" / "auto_continue_logwatch.log")
    codex_log = Path(args.log_path)

    msg = args.message
    if args.message_file:
        msg = Path(args.message_file).read_text(encoding="utf-8")

    state = read_state(state_file)
    last_sent_turn = str(state.get("last_sent_turn", ""))
    last_sent_thread = str(state.get("last_sent_thread", state.get("thread_id", "")))

    auto_mode = args.thread_id == "auto"
    watched_thread = args.thread_id
    if watched_thread == "auto":
        watched_thread = discover_thread_id(codex_log) or ""

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

    # Try to locate the rollout file for the initial thread right away.
    if watched_thread:
        rollout_path = find_rollout_file(watched_thread, SESSIONS_DIR)
        if rollout_path:
            append_log(watch_log, f"rollout: watching {rollout_path.name}")

    while True:
        events: list[tuple[str, str, str]] = []
        tnow = time.time()

        # --- Poll codex-tui.log ---
        if codex_log.exists():
            if codex_fh is None:
                codex_fh = codex_log.open("r", encoding="utf-8", errors="ignore")
                codex_fh.seek(0, os.SEEK_END)
            while True:
                line = codex_fh.readline()
                if not line:
                    break
                m = EVENT_RE.search(line)
                if m:
                    events.append((m.group(1), m.group(2), m.group(3)))
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
            elif auto_mode:
                result = find_latest_rollout(SESSIONS_DIR)
                if result:
                    watched_thread, rollout_path = result
                    watched_last_event_at = tnow
                    append_log(
                        watch_log,
                        f"watch: auto-selected thread_id={watched_thread} from rollout",
                    )

        # --- Poll rollout JSONL ---
        if rollout_path is not None and rollout_path.exists():
            if rollout_fh is None:
                rollout_fh = rollout_path.open("r", encoding="utf-8", errors="ignore")
                rollout_fh.seek(0, os.SEEK_END)
            while True:
                line = rollout_fh.readline()
                if not line:
                    break
                turn_id = parse_rollout_event(line)
                if turn_id and watched_thread:
                    events.append((watched_thread, turn_id, "false"))
            # Detect truncation/rotation.
            try:
                if rollout_fh.tell() > rollout_path.stat().st_size:
                    rollout_fh.close()
                    rollout_fh = None
            except FileNotFoundError:
                rollout_fh.close()
                rollout_fh = None

        # --- Process collected events ---
        for thread_id, turn_id, needs_follow_up in events:
            tnow = time.time()

            if auto_mode:
                if not watched_thread:
                    watched_thread = thread_id
                    watched_last_event_at = tnow
                    append_log(watch_log, f"watch: auto-selected thread_id={watched_thread}")
                elif thread_id == watched_thread:
                    watched_last_event_at = tnow
                elif watched_last_event_at == 0.0 or (tnow - watched_last_event_at) > max(0.0, args.auto_rebind_idle_secs):
                    prev = watched_thread
                    watched_thread = thread_id
                    watched_last_event_at = tnow
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
            if needs_follow_up != "false":
                continue
            if turn_id == last_sent_turn and thread_id == last_sent_thread:
                continue

            if pause_file.exists():
                append_log(watch_log, f"skip: pause file present ({pause_file}) turn={turn_id}")
                continue
            if pane_pause_file.exists():
                append_log(watch_log, f"skip: pane pause file present ({pane_pause_file}) turn={turn_id}")
                continue
            if args.require_pane_active and not tmux_pane_active(args.pane):
                append_log(watch_log, f"skip: pane inactive turn={turn_id}")
                continue

            if tnow - last_send_time < max(0.0, args.cooldown_secs):
                append_log(watch_log, f"skip: cooldown turn={turn_id}")
                continue

            if args.send_delay_secs > 0.0:
                time.sleep(args.send_delay_secs)

            ok, send_error = tmux_send(args.pane, msg, max(0.0, args.enter_delay_secs))
            if ok:
                last_send_time = tnow
                last_sent_turn = turn_id
                last_sent_thread = thread_id
                state["last_sent_turn"] = turn_id
                state["last_sent_thread"] = thread_id
                state["thread_id"] = watched_thread
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

        time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
