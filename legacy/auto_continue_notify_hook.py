#!/usr/bin/env python3
import json, os, re, subprocess, sys, time

MSG = """please continue with the workstream.

Keep momentum, validate changes, and move to the next highest-value task.
"""

FATAL_RE = re.compile(
    r"""
    (you(?:'ve| have)\s+hit\s+your\s+limit|
     insufficient[_ ]quota|out\s+of\s+credits|exceeded\s+your\s+current\s+quota|
     payment\s+required|
     invalid[_ ]api[_ ]key|authentication\s+failed)
    """,
    re.IGNORECASE | re.VERBOSE,
)

def touch(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8"):
        pass

def load_payload() -> dict:
    if len(sys.argv) > 1 and sys.argv[1]:
        return json.loads(sys.argv[1])
    raw = sys.stdin.read()
    if raw:
        return json.loads(raw)
    return {}

payload = load_payload()
cwd = payload.get("cwd") or os.getcwd()
log_file = os.path.join(cwd, ".codex", "auto_continue.log")

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        os.makedirs(os.path.join(cwd, ".codex"), exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

def pane_exists(pane: str) -> bool:
    if not pane:
        return False
    rc = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_id}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return rc.returncode == 0

def write_debug(payload: dict, cwd: str) -> None:
    try:
        os.makedirs(os.path.join(cwd, ".codex"), exist_ok=True)
        # NOTE: Avoid writing to tracked files by default. The `auto_continue`
        # runner frequently updates debug state, which is useful locally but
        # should not show up in `git status` during development.
        #
        # Use CODEX_AUTO_CONTINUE_WRITE_TRACKED_DEBUG=1 to restore the previous
        # behavior of writing to `.codex/auto_continue.last.json`.
        debug_path = os.path.join(cwd, ".codex", "auto_continue.last.local.json")
        if os.environ.get("CODEX_AUTO_CONTINUE_WRITE_TRACKED_DEBUG", "") == "1":
            debug_path = os.path.join(cwd, ".codex", "auto_continue.last.json")
        panes = None
        try:
            panes_out = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index} #{pane_id} #{pane_active} #{pane_current_command}"],
                check=False,
                capture_output=True,
                text=True,
            )
            if panes_out.stdout:
                panes = panes_out.stdout.strip().splitlines()
        except Exception:
            panes = None
        input_messages = payload.get("input-messages")
        if not isinstance(input_messages, list):
            input_messages = []
        last_assistant = payload.get("last-assistant-message")
        if not isinstance(last_assistant, str):
            last_assistant = ""
        debug = {
            "payload_meta": {
                "type": payload.get("type"),
                "cwd": payload.get("cwd"),
                "thread-id": payload.get("thread-id"),
                "turn-id": payload.get("turn-id"),
                "input_messages_count": len(input_messages),
                "last_assistant_message_bytes": len(last_assistant.encode("utf-8", errors="ignore")),
            },
            "tmux_pane": os.environ.get("TMUX_PANE"),
            "codex_tmux_pane": os.environ.get("CODEX_TMUX_PANE"),
            "tmux_panes": panes,
        }
        with open(debug_path, "w", encoding="utf-8") as f:
            json.dump(debug, f, indent=2, sort_keys=True)
    except Exception:
        pass

write_debug(payload, cwd)

payload_type = payload.get("type", "")
# notify currently fires on turn completion events (commonly: agent-turn-complete)
if payload_type and "turn-complete" not in payload_type:
    log(f"skip: payload type={payload_type}")
    sys.exit(0)

last = payload.get("last-assistant-message") or ""

# Per-project pause file (lives in the repo’s .codex/)
pause_file = os.path.join(cwd, ".codex", "AUTO_CONTINUE_PAUSE")

# If we hit a likely “credits/quota/auth/rate limit” situation, stop auto-continue.
if FATAL_RE.search(last):
    touch(pause_file)
    log("pause: fatal pattern matched in last assistant message")

    pane = os.environ.get("TMUX_PANE")
    if pane:
        subprocess.run(
            ["tmux", "display-message", "-t", pane, "Codex auto-continue paused (quota/auth/rate-limit suspected)."],
            check=False,
        )
    sys.exit(0)

# Manual pause
if os.path.exists(pause_file):
    log(f"skip: pause file present at {pause_file}")
    sys.exit(0)

# Inject the next prompt into the same tmux pane Codex is running in.
pane = os.environ.get("CODEX_TMUX_PANE")
if pane and not pane_exists(pane):
    log(f"warn: CODEX_TMUX_PANE {pane} not found; falling back to TMUX_PANE")
    pane = None
if not pane:
    pane = os.environ.get("TMUX_PANE")
if not pane:
    log("skip: no valid tmux pane (TMUX_PANE missing)")
    sys.stderr.write("auto_continue: TMUX_PANE not set; skipping send-keys\n")
    sys.exit(0)

payload_size = len(json.dumps(payload, ensure_ascii=False))

time.sleep(0.2)
send1 = subprocess.run(
    ["tmux", "send-keys", "-t", pane, "-l", MSG],
    check=False,
)
send2 = subprocess.run(
    ["tmux", "send-keys", "-t", pane, "C-m"],
    check=False,
)
if send1.returncode != 0 or send2.returncode != 0:
    log(f"error: tmux send-keys failed for pane={pane} rc1={send1.returncode} rc2={send2.returncode}")
else:
    log(f"continue: sent prompt to pane={pane} size={payload_size}B")
