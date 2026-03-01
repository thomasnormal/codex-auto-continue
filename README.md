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

## Screenshots

### Status overview

```
$ acw status
Active watchers: 3
WINDOW  PANE  PID     THREAD_ID     STATE    LAST_EVENT                   MESSAGE
------  ----- ------- ------------- -------- ---------------------------- -------
0:1     %1    48201   01a2b3c4-d... running  continue turn=01a2b3c5-ef... msg:please continue working on the API...
0:2     %2    48305   01a2b3c6-d... running  continue turn=01a2b3c7-ef... msg:continue writing tests for the auth...
0:3     %3    48410   01a2b3c8-d... paused   continue turn=01a2b3c9-ef... msg:keep refactoring the database layer...
```

### Start with interactive editor

```
$ acw start 2
# $EDITOR opens with an empty buffer — write your multi-line continue message.
# Save and quit to start the watcher. Empty message = cancel.
started: pid=48305 pane=%2 thread_id=01a2b3c6-d5e6-7f80-9a1b-2c3d4e5f6a7b
```

### Start with inline message

```
$ acw start %6 --message "continue and focus on tests"
started: pid=49122 pane=%6 thread_id=01a2b3ca-d5e6-7f80-9a1b-2c3d4e5f6a7b
```

### Pause and resume

```
$ acw pause 2
paused: pane=%2 pid=48305

$ acw resume 2
resumed: pane=%2 pid=48305

$ acw pause-all
paused: pane=%1 pid=48201
paused: pane=%2 pid=48305
```

### Edit message for a running watcher

```
$ acw edit 2
# $EDITOR opens pre-filled with the current message.
# Save and quit to update. The watcher restarts with the new message.
resolved: target=2 pane=%2
stopped: pane=%2 pid=48305
started: pid=49501 pane=%2 thread_id=01a2b3c6-d5e6-7f80-9a1b-2c3d4e5f6a7b
```

## Requirements

- `python3`, `tmux`, `pstree`
- Codex CLI
- Linux (uses `/proc` for thread discovery)
