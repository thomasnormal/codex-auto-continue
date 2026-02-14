# Architecture

## Components

- `bin/auto_continue_watchd.sh`
  - CLI process manager.
  - Starts/stops one watcher per tmux pane.
  - Handles thread auto-discovery, per-pane message config, status table, and pause/resume.
- `bin/auto_continue_logwatch.py`
  - Event engine.
  - Tails `~/.codex/log/codex-tui.log`.
  - On `needs_follow_up=false` for the watched thread, sends message to target tmux pane.
- `legacy/auto_continue_notify_hook.py`
  - Old notify-hook mode retained for compatibility.

## Data Flow

1. `start` resolves pane + thread ID.
2. `watchd` launches `auto_continue_logwatch.py` with pane/thread/message arguments.
3. Python watcher tails Codex log and emits message via `tmux send-keys`.
4. Runtime state/logs live under `<project>/.codex/`.

## Runtime Files

All per-project files are in `<project>/.codex/`, including:
- `auto_continue_logwatch.<pane>.pid`
- `auto_continue_logwatch.<pane>.state.local.json`
- `auto_continue_logwatch.<pane>.log`
- `auto_continue_logwatch.<pane>.message.local.txt`
- `AUTO_CONTINUE_PAUSE.<pane>`

Global pause file (optional):
- `AUTO_CONTINUE_PAUSE`

