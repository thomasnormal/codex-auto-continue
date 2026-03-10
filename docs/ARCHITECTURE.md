# Architecture

## Components

- `bin/auto_continue_watchd.py`
  - CLI process manager (Python).
  - Starts/stops one watcher per tmux pane.
  - Handles thread discovery, per-pane message config, tmux rename hook sync,
    and status/cleanup.
- `bin/auto_continue_logwatch.py`
  - Event engine.
  - Tails `~/.codex/log/codex-tui.log`.
  - On `needs_follow_up=false` for the watched thread, sends message to target tmux pane.

## Data Flow

1. `start` resolves pane + thread ID.
2. `watchd` launches `auto_continue_logwatch.py` with pane/thread/message arguments.
3. Python watcher tails Codex log and emits message via `tmux send-keys`.
4. Runtime state/logs live under `~/.codex/`.

## Runtime Files

Files are in `~/.codex/`, including:
- `auto_continue_logwatch.<pane>.pid`
- `auto_continue_logwatch.<pane>.log`
- `acw_session.<thread-id>.json` (thread-keyed canonical session state)
