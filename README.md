# codex-auto-continue

Auto-send a follow-up prompt to Codex in tmux after each completed turn.

## Quick Start

```bash
# Add an alias (optional but recommended)
alias acw='python3 /path/to/codex-auto-continue/bin/auto_continue_watchd.py'

acw start 2        # window index — opens $EDITOR for the message
acw start %6       # or pane id
acw start uvm      # or tmux window name
```

That's it. The watcher discovers the Codex thread for the pane and sends your
message whenever a turn completes. If a thread-id cannot be discovered, `start`
fails instead of running with an unknown thread.

Run `acw --help` for the full command summary, target rules, and examples.

`start` only accepts live tmux targets: pane id, window index, `session:window`,
or exact tmux window name. If you already know the Codex thread id, pass it as
the second positional argument: `acw start uvm <thread-id>`.

For all-watcher operations, prefer the no-target forms:

```bash
acw pause
acw resume
acw restart
```

Quoted `'*'` is still accepted as an explicit synonym, but an unquoted `*`
will be expanded by your shell before `acw` sees it.

## Commands

```
start       <target> [thread-id] [--message TEXT | --message-file FILE]
stop        [target]
edit        <target>
pause       [target|*]
resume      [target|*]
restart     [target|*]
cleanup
status      [target]
doctor      [target]
```

For `start`, `<target>` is a pane id (`%6`), window index (`2`), `session:window` (`0:2`), or a tmux window name (`uvm`).
`stop` with no target stops all running watchers.
`pause`, `resume`, and `restart` with no target act on all running watchers.
`cleanup` removes stale watcher files.
`doctor` checks tmux reachability, state-dir writability, Codex auth state, and
optionally the target pane's thread detection and watcher status.

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

The watcher primarily tails `~/.codex/log/codex-tui.log` for completion
signals:

- old Codex logs `post sampling token usage ... needs_follow_up=false`
- current Codex logs `codex_core::tasks: close`

On startup it also scans the recent tail of `codex-tui.log` so a watcher that
attaches just after a completed turn can still send the next continue message.

When a watched thread completes a turn, the watcher sends the continue message
to the tmux pane.

### Thread Auto-Discovery

The watch daemon discovers which Codex thread belongs to each pane by inspecting
the pane's live process tree, Codex's local state DB, and thread-keyed session
state. This works for both resumed Codex panes and plain `codex
--full-auto` panes running on different tmux windows at the same time.

Watcher processes also record the tmux socket they were started against. That
keeps watcher discovery scoped to the current tmux server and avoids pane-id
collisions between your live server and the isolated real-Codex test harness.

Automatic thread discovery is pane-local only. If `acw` cannot prove which
thread belongs to the target pane yet, it waits instead of guessing from the
most recent global Codex log activity.

Pid-based thread discovery is also bounded to the current Codex process
lifetime, so `acw` will not reuse stale thread ids from an older process that
happened to share the same Linux pid later.

Canonical session state is stored as `~/.codex/acw_session.<thread-id>.json`.

If the Codex session restarts with a new thread, the watcher re-discovers it during periodic health checks.

Window names are synchronized via a tmux `window-renamed` hook (no periodic
rename polling).

### Live Pane Resolution

`acw status` resolves thread→pane mappings dynamically so the WINDOW and PANE
columns reflect the current tmux layout.

### Tmux Socket Recovery

If tmux is still running but its socket path disappears, `acw` now prints a
recovery hint instead of only reporting a generic tmux failure. In that case,
recreate the socket with the suggested command, for example:

```bash
kill -USR1 1996933
```

## Doctor

Run `acw doctor` for environment checks. Inside tmux, `acw doctor` also checks
the current pane when `TMUX_PANE` is available:

```bash
$ acw doctor
Doctor
  state_dir: /home/user/.codex
  tmux_socket: /tmp/tmux-1000/default
[ok] state dir writable: /home/user/.codex
[ok] Codex auth state present: /home/user/.codex/auth.json
[ok] tmux server reachable
[ok] pane resolved: %6
[ok] Codex thread detected: 01a2b3c6-d5e6-7f80-9a1b-2c3d4e5f6a7b
[info] no watcher running for pane %6
RESULT: ok
```

### Health Monitoring

Each watcher tracks a small amount of operational health:

- **ok** — the watcher has a thread id and a readable `codex-tui.log`
- **warn** — the watcher is still waiting for a pane-local thread id, or `codex-tui.log` is missing
- **paused** / **dead** — process state, not log-source state

View health with `acw status`.

When the summary table shows a degraded watcher, `acw status` now prints a
follow-up recommendation like `acw doctor uvm`. `acw doctor` also evaluates the
watcher's current health and, when appropriate, highlights a concrete next
command such as `acw restart uvm` or `acw start uvm <thread-id>`.

## Screenshots

### Status overview

```
$ acw status
Sessions: 3
WINDOW      STATE    STARTED    LAST_ACW   LAST_AGENT              MESSAGE
----------- -------- ---------- ---------- ----------------------- ------------------------------
0:1:api     running  2d14h ago  4m ago     Ran unit tests          msg:please continue working…
0:2:tests   running  1d08h ago  8m ago     Edited auth middleware  msg:continue writing tests…
0:3:refac   paused   3d02h ago  1h22m ago  Explored schema drift   msg:keep refactoring the…
```

The columns show:
- **WINDOW** — `session:index:name` (uses live tmux state, survives window reordering)
- **STARTED** — when the Codex thread was created, from local Codex SQLite state
- **LAST_ACW** — when the watcher last sent a continue prompt
- **LAST_AGENT** — recent assistant-visible text from the live tmux pane, falling back to the thread title when needed
- **MESSAGE** — the stored continue prompt that `acw` will inject on the next completion

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

$ acw pause
paused: pane=%1 pid=48201
paused: pane=%2 pid=48305
```

Watchers also auto-pause when the pane shows a Codex-level interruption or
account error banner, such as `Conversation interrupted`, `Model interrupted to
submit steer instructions`, auth failures, or quota/usage-limit errors. Resume
them explicitly with `acw resume <target>` after you have handled the issue in
the pane.

If a watcher process is gone, `acw status` now reports it as `dead` even if the
last persisted health snapshot was `ok`.

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

`rich` is optional. If it is installed, `acw status` uses the formatted table;
otherwise it falls back to a plain-text summary.

## E2E Test

Run `bash test/test_rollout_e2e.sh` to execute the real-Codex integration suite.
The shell script is a thin wrapper around a shared Python harness and currently
runs thirteen real-Codex tests:

- a Codex contract test that proves which completion signals the current Codex build emits
- a watcher integration test that verifies `auto_continue_logwatch.py` sends the continue prompt
- a watcher regression test that proves watcher health no longer emits legacy rollout warnings
- a watcher regression test that sends `Escape` in the isolated tmux pane and verifies the watcher auto-pauses on the real interrupt banner
- a manager integration test that starts a watcher against a plain `codex --full-auto` pane
- a manager integration test that starts a watcher against a `codex resume <thread-id>` pane
- a manager integration test that updates a watcher's message through `acw edit <pane>`
- a manager integration test that verifies `acw doctor` reports a healthy current pane with a detected thread
- a manager regression test that verifies `acw doctor` reports a missing thread on a plain shell pane
- a manager regression test that verifies `acw start <window-name>` fails cleanly on a non-Codex pane
- a manager integration test that reports `dead` after a real watcher process exits
- a manager integration test that shows recent assistant output in the `LAST_AGENT` status column
- a manager integration test that recreates a private tmux socket with `kill -USR1` and then starts successfully

The harness always uses a dedicated tmux server on its own socket, so it does
not interfere with your existing tmux sessions. It also always uses an isolated
test home seeded from your existing Codex auth/config files, so tmux state,
watcher state, and Codex session artifacts stay out of your live `~/.codex`.

If a real-Codex test fails, the harness archives pane capture, watcher logs,
Codex log tail, and state files under `~/.codex/auto-continue-e2e-tmp/failures/`
for postmortem debugging.
