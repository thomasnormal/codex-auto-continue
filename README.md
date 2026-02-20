# codex-auto-continue

Auto-send a follow-up prompt to Codex in tmux after each completed turn.

## Quick Start

```bash
cd /path/to/your/project
/path/to/codex-auto-continue/bin/auto_continue_watchd.sh start 2        # window index
/path/to/codex-auto-continue/bin/auto_continue_watchd.sh start %6       # or pane id
```

That's it. The watcher auto-discovers the Codex thread and sends "please continue" whenever a turn completes.

## Commands

```
start   <target> [thread-id] [--message TEXT | --message-file FILE]
stop    [target]
restart <target>
pause   <target>
resume  <target>
status  [target]
```

`<target>` is a pane id (`%6`), window index (`2`), or `session:window` (`0:2`).

## Custom Message

```bash
auto_continue_watchd.sh start %0 --message "continue and focus on tests"
auto_continue_watchd.sh start 2 --message-file /path/to/msg.txt
```

Default: `<project>/.codex/auto_continue.message.txt`, falling back to `examples/messages/default_continue_message.txt`.

## How It Works

The watcher polls two event sources to support both old and new Codex versions:

- **codex-tui.log** — old Codex emits `post sampling token usage` lines here
- **rollout JSONL** (`~/.codex/sessions/`) — new Codex emits `task_complete` events here

When either source signals a completed turn, the watcher sends the continue message to the tmux pane.

## Requirements

- `bash`, `python3`, `tmux`
- Codex CLI
