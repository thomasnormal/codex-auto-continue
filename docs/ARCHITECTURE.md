# Architecture

## Components

- `bin/auto_continue_watchd.py`
  - CLI process manager (Python).
  - Starts/stops one watcher per tmux pane.
  - Handles thread discovery, per-pane message config, tmux rename hook sync,
    thread-keyed session state, and status/cleanup.
- `bin/auto_continue_logwatch.py`
  - Event engine.
  - Primarily tails `~/.codex/log/codex-tui.log`.
  - Uses rollout JSONL under `~/.codex/sessions/` as a supplemental startup and
    health source when present.
  - On a supported completion signal for the watched thread, sends the message
    to the target tmux pane.

## Data Flow

1. `start` resolves pane + thread ID and writes thread-keyed session metadata.
2. `watchd` launches `auto_continue_logwatch.py` with pane/thread/message arguments.
3. The watcher reads `codex-tui.log`, optionally correlates rollout JSONL, and
   emits the message via `tmux send-keys`.
4. `watchd` keeps the stored window name synchronized via a tmux
   `window-renamed` hook.
5. Runtime state/logs live under `~/.codex/`.

## Runtime Files

Files are in `~/.codex/`, including:
- `auto_continue_logwatch.<pane>.pid`
- `auto_continue_logwatch.<pane>.log`
- `auto_continue_logwatch.<pane>.runner.log`
- `acw_session.<thread-id>.json` (thread-keyed canonical session state)
