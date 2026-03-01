# codex-auto-continue

Auto-send a follow-up prompt to Codex in tmux after each completed turn.

## Quick Start

```bash
# Add an alias (optional but recommended)
alias acw='/path/to/codex-auto-continue/bin/auto_continue_watchd.sh'

acw start 2        # window index — opens $EDITOR for the message
acw start %6       # or pane id
```

That's it. The watcher auto-discovers the Codex thread and sends your message whenever a turn completes.

## Commands

```
start       <target> [thread-id] [--message TEXT | --message-file FILE]
stop        [target]
restart     <target>
edit        <target>
pause       <target>
resume      <target>
pause-all
resume-all
restart-all
status      [target]
```

`<target>` is a pane id (`%6`), window index (`2`), or `session:window` (`0:2`).

## Custom Message

When starting without `--message` or `--message-file`, your `$EDITOR` opens so you can write a multi-line message interactively. An empty message cancels the start.

You can also provide a message inline or via file:

```bash
acw start %0 --message "continue and focus on tests"
acw start 2 --message-file /path/to/msg.txt
```

To edit the message for a running watcher:

```bash
acw edit 2    # opens $EDITOR with the current message
```

Default message location: `~/.codex/auto_continue.message.txt`, falling back to `examples/messages/default_continue_message.txt`.

## How It Works

The watcher polls two event sources to support both old and new Codex versions:

- **codex-tui.log** — old Codex emits `post sampling token usage` lines here
- **rollout JSONL** (`~/.codex/sessions/`) — new Codex emits `task_complete` events here

When either source signals a completed turn, the watcher sends the continue message to the tmux pane.

### Thread Auto-Discovery

The watcher automatically discovers which Codex session belongs to each pane by inspecting the pane's process tree and checking which rollout file the `codex` process has open (`/proc/PID/fd/`). This works reliably even when multiple Codex sessions run on different panes simultaneously.

If the Codex session restarts with a new thread, the watcher re-discovers it automatically during periodic health checks (every 30 seconds).

### Health Monitoring

Each watcher tracks its health status:

- **ok** — rollout file is being written, watcher is active
- **stale** — rollout file hasn't been written in 5+ minutes
- **warn** — no rollout file found for the tracked thread
- **error** — rollout channel closed

View health with `acw status`.

## Requirements

- `python3`, `tmux`, `pstree`
- Codex CLI
- Linux (uses `/proc` for thread discovery)
