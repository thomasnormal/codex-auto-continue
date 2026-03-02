# codex-auto-continue

Auto-send a follow-up prompt to Codex in tmux after each completed turn.

## Quick Start

```bash
# Add an alias (optional but recommended)
alias acw='/path/to/codex-auto-continue/bin/auto_continue_watchd.sh'

acw start 2        # window index — opens $EDITOR for the message
acw start %6       # or pane id
acw start uvm      # or tmux window name
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

`<target>` is a pane id (`%6`), window index (`2`), `session:window` (`0:2`), or a tmux window name (`uvm`).

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

### Live Pane Resolution

After a tmux crash and restart, Codex threads may end up in different panes than where the watcher was originally started. `acw status` resolves thread→pane mappings dynamically (via `/proc/PID/fd/`), so the WINDOW and PANE columns always reflect the current tmux layout.

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
WINDOW      STATE    STARTED    LAST_MSG   LAST_ACW   MESSAGE
----------- -------- ---------- ---------- ---------- -------
0:1:api     running  2d14h ago  0s ago     4m ago     msg:please continue working on the API...
0:2:tests   running  1d08h ago  12s ago    8m ago     msg:continue writing tests for the auth...
0:3:refac   paused   3d02h ago  1h22m ago  1h22m ago  msg:keep refactoring the database layer...
```

The columns show:
- **WINDOW** — `session:index:name` (uses live tmux state, survives window reordering)
- **STARTED** — when the Codex thread was created
- **LAST_MSG** — last activity in the Codex session (rollout file mtime)
- **LAST_ACW** — when the watcher last sent a continue prompt

### Start with interactive editor

```
$ acw start tests
# $EDITOR opens with an empty buffer — write your multi-line continue message.
# Save and quit to start the watcher. Empty message = cancel.
resolved: target=tests pane=%2
started: pid=48305 pane=%2 thread_id=01a2b3c6-d5e6-7f80-9a1b-2c3d4e5f6a7b
```

### Start with inline message

```
$ acw start api --message "continue and focus on tests"
resolved: target=api pane=%1
started: pid=49122 pane=%1 thread_id=01a2b3ca-d5e6-7f80-9a1b-2c3d4e5f6a7b
```

### Pause and resume

```
$ acw pause tests
paused: pane=%2 pid=48305

$ acw resume tests
resumed: pane=%2 pid=48305

$ acw pause-all
paused: pane=%1 pid=48201
paused: pane=%2 pid=48305
```

### Edit message for a running watcher

```
$ acw edit tests
# $EDITOR opens pre-filled with the current message.
# Save and quit to update. The watcher restarts with the new message.
resolved: target=tests pane=%2
stopped: pane=%2 pid=48305
started: pid=49501 pane=%2 thread_id=01a2b3c6-d5e6-7f80-9a1b-2c3d4e5f6a7b
```

## Requirements

- `python3`, `tmux`, `pstree`
- Codex CLI
- Linux (uses `/proc` for thread discovery)
