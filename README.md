# codex-auto-continue

Small utility to auto-send a follow-up prompt to Codex in tmux after each completed turn.

It supports:
- one watcher per pane
- per-pane message text or message file
- per-pane pause/resume
- thread auto-discovery (so `start %6` or `start 2` usually just works)
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

2. Start watcher for a pane or window (thread auto-detected):

```bash
/path/to/codex-auto-continue/bin/auto_continue_watchd.sh start 2
```

3. Check status:

```bash
/path/to/codex-auto-continue/bin/auto_continue_watchd.sh status
```

Project root detection defaults to:
1. `AUTO_CONTINUE_PROJECT_CWD` (if set)
2. current Git repo root (if inside a repo)
3. parent directory when run from a `.codex/` folder
4. current directory

If that is not what you want, set:

```bash
AUTO_CONTINUE_PROJECT_CWD=/path/to/your/project
```

Optional timing knob for stubborn Enter races:

```bash
AUTO_CONTINUE_SEND_DELAY_SECS=0.25
AUTO_CONTINUE_ENTER_DELAY_SECS=0.15
```

If your tmux windows live on a non-default socket, set:

```bash
AUTO_CONTINUE_TMUX_SOCKET=/path/to/tmux/socket
```

## Commands

```bash
auto_continue_watchd.sh start <target> [thread-id|auto] [--message TEXT | --message-file FILE]
auto_continue_watchd.sh stop [target]
auto_continue_watchd.sh restart <target> [thread-id|auto] [--message TEXT | --message-file FILE]
auto_continue_watchd.sh pause <target>
auto_continue_watchd.sh resume <target>
auto_continue_watchd.sh status [target]
auto_continue_watchd.sh run <target> [thread-id|auto] [--message TEXT | --message-file FILE]
```

Notes:
- `<target>` can be a pane id (`%6`), a window index (`2`), or `session:window` (`0:2`).
- window targets resolve to the active pane in that window.
- if thread auto-detection misses at `start`, the watcher now falls back to `thread-id=auto`.
- `status` shows both `WINDOW` (`session:window`) and `PANE` columns.
- `start` enforces one live watcher per pane.
- `restart` stops and starts a pane watcher (reuses the pane's previous message/thread by default).
- `run` is foreground mode (useful for debugging).
- `pause`/`resume` are pane-local and immediate.
- If a global pause file exists at `<project>/.codex/AUTO_CONTINUE_PAUSE`, all panes are paused.

## Message Control

Examples:

```bash
auto_continue_watchd.sh start %0 --message "continue and focus on tests"
auto_continue_watchd.sh start 2 --message-file /path/to/msg.txt
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
