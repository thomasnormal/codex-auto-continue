# Architecture

## Components

- `bin/auto_continue_watchd.py`
  - CLI process manager (Python).
  - Starts/stops one watcher per tmux pane.
  - Handles thread discovery, per-pane message config, tmux rename hook sync,
    thread-keyed session state, and status/cleanup.
- `bin/auto_continue_logwatch.py`
  - Event engine.
  - Tails `~/.codex/log/codex-tui.log`.
  - Replays the recent log tail on startup so a watcher can catch a completion
    that happened just before it attached.
  - On a supported completion signal for the watched thread, sends the message
    to the target tmux pane.

## Data Flow

1. `start` resolves pane + thread ID and writes thread-keyed session metadata.
2. `watchd` launches `auto_continue_logwatch.py` with pane/thread/message arguments.
3. The watcher reads `codex-tui.log` and emits the message via `tmux send-keys`.
4. `watchd` keeps the stored window name synchronized via a tmux
   `window-renamed` hook.
5. Runtime state/logs live under `~/.codex/`, and `status` reads local Codex
   SQLite state for thread creation/last-activity timestamps.

## Runtime Files

Files are in `~/.codex/`, including:
- `auto_continue_logwatch.<pane>.pid`
- `auto_continue_logwatch.<pane>.log`
- `auto_continue_logwatch.<pane>.runner.log`
- `acw_session.<thread-id>.json` (thread-keyed canonical session state)
