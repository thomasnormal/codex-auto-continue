# codex-auto-continue

Small utility to auto-send a follow-up prompt to Codex in tmux after each completed turn.

It supports:
- one watcher per pane
- per-pane message text or message file
- per-pane pause/resume
- thread auto-discovery (so `start %6` usually just works)
- optional legacy notify-hook mode

## Requirements

- Linux/macOS shell environment with `bash`
- `python3`
- `tmux`
- Codex CLI writing `~/.codex/log/codex-tui.log`

## Quick Start

1. Pick your target project directory (the one where your Codex panes work):

```bash
cd /path/to/your/project
```

2. Start watcher for a pane (thread auto-detected):

```bash
/path/to/codex-auto-continue/bin/auto_continue_watchd.sh start %2
```

3. Check status:

```bash
/path/to/codex-auto-continue/bin/auto_continue_watchd.sh status
```

If you run the command from a different directory, set:

```bash
AUTO_CONTINUE_PROJECT_CWD=/path/to/your/project
```

## Commands

```bash
auto_continue_watchd.sh start <pane> [thread-id|auto] [--message TEXT | --message-file FILE]
auto_continue_watchd.sh stop [pane]
auto_continue_watchd.sh pause <pane>
auto_continue_watchd.sh resume <pane>
auto_continue_watchd.sh status [pane]
auto_continue_watchd.sh run <pane> [thread-id|auto] [--message TEXT | --message-file FILE]
```

Notes:
- `start` enforces one live watcher per pane.
- `run` is foreground mode (useful for debugging).
- `pause`/`resume` are pane-local and immediate.
- If a global pause file exists at `<project>/.codex/AUTO_CONTINUE_PAUSE`, all panes are paused.

## Message Control

Examples:

```bash
auto_continue_watchd.sh start %0 --message "continue and focus on tests"
auto_continue_watchd.sh start %2 --message-file /path/to/msg.txt
```

Default message file lookup:
1. `<project>/.codex/auto_continue.message.txt`
2. `examples/messages/default_continue_message.txt`

## Where Runtime Files Go

Runtime state is written under:

```text
<project>/.codex/
```

including `pid`, `state.local.json`, logs, pause files, and message metadata.

## Legacy Notify-Hook Mode

The old notify-hook script is kept at:

```text
legacy/auto_continue_notify_hook.py
```

Use this only if you prefer Codex notify-hook integration over log watching.

## Smoke Test

```bash
./test/smoke.sh
```
