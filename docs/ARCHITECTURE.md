# Architecture

## Overview

`acw` is a tmux-side automation layer for Codex sessions.

It has two runtime pieces:

- `bin/auto_continue_watchd.py`
  - the manager CLI
  - starts, stops, restarts, diagnoses, and reports on watchers
  - reconstructs the live world from tmux, `ps`, local Codex state, and saved
    `acw` session files on every command
- `bin/auto_continue_logwatch.py`
  - the long-running per-pane watcher
  - tails `~/.codex/log/codex-tui.log`
  - decides when a watched Codex thread completed a turn
  - injects the saved continue message into the tmux pane

The design is intentionally external. `acw` does not depend on private Codex
hooks or a manager process embedded inside Codex itself. It watches the same
local artifacts an operator can inspect:

- tmux pane metadata
- process trees
- Codex local SQLite state
- `codex-tui.log`
- `~/.codex/acw_session.<thread-id>.json`


## Design Constraints

The current design is shaped by a few hard rules:

- Correctness over convenience.
  `acw` does not guess thread IDs from weak signals like cwd or timing.
- One logical managed session should map 1:1:1:1 to:
  - one tmux pane
  - one watcher process
  - one Codex thread ID
  - one saved `acw` session record
- Live tmux and process state outrank saved state.
  Saved session JSON is durable metadata, not the live truth.
- The manager is not a resident daemon.
  Every `acw` command re-queries tmux and the process table.
- Watchers are intentionally simple.
  They react to Codex completion signals and pane-visible failures; they do not
  try to be a full task scheduler.


## High-Level Shape

```text
operator
  |
  v
acw command (watchd.py, one-shot CLI)
  |
  +--> tmux list-panes / display-message
  +--> ps process snapshot
  +--> ~/.codex/acw_session.<thread>.json
  +--> ~/.codex/state_*.sqlite
  |
  +--> start / restart / doctor / status / cleanup decisions
  |
  +--> spawn auto_continue_logwatch.py for one pane
            |
            +--> tail ~/.codex/log/codex-tui.log
            +--> watch pane banners via tmux capture-pane
            +--> write session health/state
            +--> send continue prompt via tmux send-keys
```


## Sources Of Truth

`acw` intentionally separates live state from saved state.

### 1. Live tmux state

The manager queries tmux directly on each run, primarily with:

- `list-panes -a`
- `display-message`
- `capture-pane`

The main live pane snapshot is built in
`_list_live_pane_records()` in `bin/auto_continue_watchd.py`.
For each pane it records:

- `session_name`
- `window_index`
- `window_name`
- `pane_id`
- `pane_pid`
- detected Codex thread, if one can be proven

This means live window renames or moves are noticed the next time any `acw`
command runs. There is no cached global window map that status trusts for live
panes.

### 2. Live watcher processes

The manager finds running watchers by scanning `ps` for
`auto_continue_logwatch.py` processes in `watcher_rows()`.

Each watcher row is parsed from argv and includes:

- watcher pid
- pane id
- thread id
- watch log path
- state file path
- message text from argv
- tmux socket binding

This is how `status`, `stop`, `pause`, and `restart` know which watcher
processes are actually alive.

### 3. Saved managed-session state

Canonical durable `acw` session metadata lives in:

```text
~/.codex/acw_session.<thread-id>.json
```

These files store the manager-owned metadata for a managed session:

- `thread_id`
- session/window `name`
- saved continue `message`
- watcher health
- health detail and timestamp
- last continue time

This is the durable identity for a managed session when the watcher is restarted
or temporarily absent.

### 4. Codex local SQLite state

`acw` reads Codex `state_*.sqlite` files for:

- thread creation / activity timestamps
- pid -> thread mappings from the `logs` table

This is the preferred durable source for mapping a live Codex process to a
thread ID.

### 5. `codex-tui.log`

The watcher tails:

```text
~/.codex/log/codex-tui.log
```

It watches for:

- completion events
- interrupt events

This is the primary event stream for deciding when to send the saved continue
message.

### 6. Live Codex process-owned evidence

When SQLite is not enough, `acw` still only uses process-owned evidence:

- explicit `codex resume <thread-id>` argv
- thread IDs embedded in the Codex process's open files under `/proc/<pid>/fd`

It does not fall back to cwd or timing guesses.


## Runtime Identity Model

The most important architectural change in the current design is that the
manager no longer collapses everything onto `thread_id` first.

Instead, each `acw` command builds a fresh `RuntimeSnapshot` and `RuntimeModel`
containing three separate inventories:

- live panes
- live watcher processes
- saved sessions

These types live in `bin/auto_continue_watchd.py`:

- `LivePaneRecord`
- `LiveWatcherRecord`
- `SavedSessionRecord`
- `RuntimeSnapshot`
- `RuntimeModel`

The model indexes them by:

- pane id
- thread id
- session name

This lets `status` and `doctor` report mismatches directly instead of hiding
them behind optimistic merging.

### Intended invariant

For a healthy managed session, the system wants:

- exactly one live tmux pane for the thread
- exactly one live watcher for that pane
- exactly one saved session for that thread
- the pane's live Codex thread to match the watcher's configured thread
- the saved session name to match the live window name

Violations are surfaced as invariant errors rather than silently "healed" by
guesswork.


## Manager Architecture

### The manager is one-shot, not resident

`auto_continue_watchd.py` is a CLI command, not a daemon. Each invocation
rebuilds its view of the world from:

- tmux
- `ps`
- saved sessions
- Codex SQLite state

That is why `status`, `doctor`, `start`, `restart`, and `edit` all notice pane
renames, index moves, and process changes without a background manager loop.

### Start flow

`cmd_start()` is the main creation path.

At a high level it does this:

1. Resolve the target to a live tmux pane.
2. Install the tmux `window-renamed` hook.
3. Resolve or interactively edit the continue message.
4. Build a fresh runtime model.
5. Reject duplicates or inconsistent pre-existing watchers.
6. Resolve the pane's Codex thread strictly.
7. Write the thread-keyed session JSON.
8. Disable tmux automatic window renaming for that pane.
9. Spawn `auto_continue_logwatch.py`.
10. Persist the watcher pid file.

The key design point is that start fails if thread identity is not proven.

### Stop / pause / resume / restart

#### `stop`

`stop` is destructive from the manager's point of view.

It:

- kills the watcher process
- removes the pane pid file
- forgets the saved `acw_session.<thread>.json`

So `stop` means "stop managing this session", not just "temporarily stop the
process".

#### `pause`

`pause` sends `SIGSTOP` to the watcher process and keeps the saved session.

This is the reversible operation.

#### `resume`

`resume` does not send `SIGCONT`.
It restarts paused watchers from current pane/thread state.

That avoids backlog replay from an old stopped watcher that still has unread
`codex-tui.log` history.

#### `restart`

`restart` is implemented as:

- resolve target pane
- prove the thread
- load the saved message if needed
- stop the old watcher
- optionally relaunch Codex into the same thread
- start a fresh watcher

With no target, it restarts all live managed panes and also recovers dead
managed sessions whose live Codex pane still exists.

#### `restart --restart-codex`

This path exists for same-session pane recovery.

It intentionally does not send a blind `C-c` and hope the shell returns.
Instead it:

1. verifies whether the pane is already at its shell prompt
2. if not, finds the top-level live Codex subprocess under the pane shell
3. sends `SIGTERM` to that subtree root
4. waits until the pane is back at a real shell
5. runs `codex resume <thread-id>`
6. waits until a new live Codex process appears
7. briefly checks for the directory-trust prompt

This is much more reliable than terminal-keystroke guesses when the pane is
still inside an active Codex TUI session.

### Edit flow

`edit` is a message-management command.

It:

- resolves the live pane
- proves pane/thread/session identity strictly
- loads the canonical saved session message
- opens the editor with that message
- writes the updated message back to the saved session
- restarts the watcher if one is running on that pane

The important detail is that `edit` reads the saved session message, not the
watcher's inline argv text.


## Thread Discovery

Thread discovery is intentionally strict.

The manager path is:

1. enumerate live panes with `tmux list-panes -a`
2. capture each pane's root shell pid
3. build one shared process snapshot from `ps`
4. walk each shell's descendant process tree
5. find Codex processes
6. map Codex pid -> thread id

That logic lives primarily in:

- `_load_process_graph()`
- `_codex_pids_in_process_tree()`
- `_threads_from_process_tree()`
- `_pane_thread_probe()`

### Thread sources, in order

For a live Codex pid, `thread_from_codex_pid()` uses:

1. Codex SQLite `logs.process_uuid = pid:<pid>:...`
2. `/proc/<pid>/cmdline` looking for `resume <thread-id>`
3. `/proc/<pid>/fd/*` targets containing a thread id

If none of those prove the thread, discovery returns "unknown".

### What it does not do

It does not infer a thread from:

- pane cwd
- most-recent global thread
- timing coincidence
- another watcher's thread

That rule is central to the current reliability model.


## Watcher Architecture

Each watcher is one long-running `auto_continue_logwatch.py` process tied to one
tmux pane and one saved continue message.

### Startup

On startup, the watcher:

1. validates the target pane still exists
2. normalizes the continue message
3. resolves the initial thread if started in `auto` mode
4. loads prior session state
5. seeds `last_handled_turn` from saved state
6. writes initial session JSON
7. scans the tail of `codex-tui.log` for a pending completion that happened
   just before the watcher attached
8. sets up inotify-style wakeups via `watchdog` when available

### Main loop

The main loop does three things repeatedly:

#### 1. Collect events

It reads newly appended `codex-tui.log` lines and converts them into:

- `completion` events
- `interrupt` events

The accepted completion patterns currently include:

- older `needs_follow_up=false`
- newer `codex_core::tasks: close`

#### 2. Filter and react

For each event on the watched thread, it applies:

- duplicate-turn suppression
- one-turn skip after interrupts
- pane-visible interrupt checks
- pane-visible error checks
- cooldown gating

If the pane shows errors like auth/quota/remote compact failures, the watcher
records an auto-pause reason and stops itself with `SIGSTOP`.

#### 3. Send the continue message

If the event survives all gates, the watcher:

- optionally waits `send_delay_secs`
- leaves tmux copy mode if needed
- sends the message with `tmux send-keys -l`
- waits `enter_delay_secs`
- sends `Enter`
- updates session state and watch log

The send path has one retry after a best-effort tmux mode cancel.

### Health and rebinding

The watcher also performs periodic health updates.

Health is computed from:

- whether a thread is known
- whether `codex-tui.log` exists
- whether startup grace has expired

In `auto` thread mode, the watcher periodically re-checks the pane's live Codex
thread and can rebind to a new thread if the pane has been restarted into a new
session.


## Window Naming And Rename Handling

There are two separate mechanisms here.

### Live status / targeting

For live panes, `acw` does not trust saved names.
It always asks tmux again.

That means if a window is:

- renamed
- moved to a different index
- moved between sessions

the next `acw status`, `acw doctor`, `acw start`, or `acw restart` sees the new
live label immediately.

### Saved-session rename sync

`start` installs a global tmux `window-renamed` hook:

- `ensure_tmux_window_rename_hook()`
- callback: `cmd_window_renamed()`

When a managed live window is renamed, the callback updates the saved
session's `name` field.

This matters for:

- targeting dead sessions by name
- presenting consistent names after watcher restarts

### Important limitation

The rename hook is only about saved metadata.
Live status correctness does not depend on it.

If the hook is absent, stale, or the watcher is already gone at rename time:

- live panes are still shown correctly via tmux
- saved session names for dead/offline recovery may drift


## Status, Doctor, And Cleanup

### Status

`status` builds fresh entries from the runtime snapshot.

It shows three kinds of managed rows:

- live watcher rows
- managed live panes whose watcher is missing
- dead saved sessions with no live pane

It intentionally ignores random unmanaged live Codex panes with no saved `acw`
session.

This keeps the dashboard focused on manager-owned state instead of every Codex
process on the tmux server.

### Doctor

`doctor` is a stricter, targeted diagnosis command.

It checks:

- state-dir writability
- Codex auth presence
- tmux reachability
- pane resolution
- thread detectability
- watcher presence
- watcher identity / health

It also prints concrete next-command recommendations like:

- `acw start ...`
- `acw resume ...`
- `acw restart ...`
- `acw stop ...`

### Cleanup

`cleanup` removes stale manager-owned files:

- dead pid files
- logs for dead watchers
- dead saved sessions with neither a live watcher nor a live Codex pane

Normal mutating commands use the fast cleanup path.
Deep dead-session pruning only happens on explicit `acw cleanup`.


## Runtime Files

All manager-owned runtime state lives under `~/.codex/`.

Important files:

- `auto_continue_logwatch.<pane>.pid`
  - watcher pid file keyed by pane
- `auto_continue_logwatch.<pane>.log`
  - watcher watch log
- `auto_continue_logwatch.<pane>.runner.log`
  - watcher process stdout/stderr
- `acw_session.<thread-id>.json`
  - canonical manager-owned saved session record

The Codex-owned files `acw` reads but does not own include:

- `~/.codex/log/codex-tui.log`
- `~/.codex/state_*.sqlite`


## Performance Characteristics

The manager is intentionally snapshot-based, so every command does some live
inspection work.

The current design keeps that reasonable by:

- using one shared `ps` snapshot per command
- avoiding per-pane `pstree` subprocesses in the manager
- avoiding deep stale-session cleanup on every command
- using tmux queries only where needed

The watcher side is mostly event-driven:

- `watchdog`/inotify if available
- periodic timeout fallback if not


## Testing Model

There are three layers of confidence in the repository:

- unit tests for manager logic in `test/test_watchd_unit.py`
- unit tests for watcher logic in `test/test_logwatch_unit.py`
- real Codex integration and harness tests in:
  - `test/test_real_codex_harness_unit.py`
  - `test/test_real_codex_integration.py`
  - `test/test_rollout_e2e.sh`

The real-Codex tests matter because several behaviors are only meaningful when
the tool interacts with an actual tmux server and a real Codex process tree.


## Practical Mental Model

The shortest correct mental model is:

- `watchd.py` is a stateless-on-entry manager that reconstructs live state on
  each command.
- `logwatch.py` is the only long-running automation process.
- thread identity is sacred and must be proven, never guessed.
- saved session JSON is durable manager metadata, not the live truth.
- tmux and the Codex process tree are the live truth.

If those rules stay true, the rest of the system remains understandable.
