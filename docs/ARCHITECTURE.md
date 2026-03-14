# Architecture

## Overview

`acw` is a tmux-side automation layer for Codex sessions.

The current runtime model has seven code-level pieces:

- `bin/auto_continue_watchd.py`
  - the CLI and command/control plane
  - wires the probe, state, daemon, repair, and UI modules together
- `bin/acw_probe.py`
  - strict tmux/process/Codex thread discovery
  - pane snippet extraction and thread provenance helpers
- `bin/acw_state.py`
  - the SQLite-backed manager state store
  - desired session rows plus observed runtime rows
- `bin/acw_daemon.py`
  - daemon-owned per-session runtime state and shared event-loop helpers
- `bin/acw_rpc.py`
  - Unix-socket request/response transport between the CLI and the daemon
- `bin/acw_ui.py`
  - status-table and doctor rendering helpers
- `bin/auto_continue_logwatch.py`
  - shared low-level Codex-log, pane, and tmux-send helpers
  - still usable as a direct single-session CLI for debugging/tests
  - no longer discovered or spawned by the manager at runtime

The key simplification is that `acw` no longer launches or discovers one OS
watcher process per managed pane. The daemon is the only long-running
manager-owned process for one tmux socket, and each managed session is a
state machine inside it.


## Core Model

`acw` is built around a strict 1:1:1 relationship:

- one live tmux pane
- one live Codex thread id
- one managed session record
- one live managed runtime row

Window or agent name is metadata attached to that mapping, not the primary
identity.

If any of those entities are:

- missing
- duplicated
- mismatched

that is treated as broken state. `acw` reports the invariant error instead of
guessing.


## Socket Scope

All manager state is scoped to one tmux server.

The scope key is:

- `AUTO_CONTINUE_TMUX_SOCKET` when explicitly set
- otherwise the live `$TMUX` socket path, except the default tmux socket is
  normalized to the empty default scope

This lets:

- the default tmux server behave consistently whether `acw` is invoked inside
  or outside tmux
- private harness sockets keep fully isolated daemon/runtime state


## Sources Of Truth

### 1. Saved session state

Durable desired state lives in:

```text
~/.codex/acw_state.sqlite
```

The `sessions` table holds manager-owned intent for one managed session:

- `thread_id`
- `pane`
- `name`
- `message`
- `tmux_socket`
- `paused`
- `generation`

They also carry durable health metadata written by the worker:

- `health`
- `health_detail`
- `health_ts`
- `last_continue_at`

The session row is the desired state for the daemon.

### 2. Runtime state

Observed managed-worker state lives in:

```text
~/.codex/acw_state.sqlite
```

The `runtimes` table is written by the daemon and describes the currently
attached live session runtime for one managed session:

- daemon-assigned runtime id (`runtime=` in the UI; it identifies one live
  managed session runtime and is not an OS pid)
- daemon pid
- pane
- thread
- runtime status
- paused flag
- generation
- watch log path
- heartbeat timestamp

The runtime row is the daemon-owned observed state.

### 3. Live tmux and process state

The manager still probes tmux and live processes directly for correctness:

- `tmux list-panes -a`
- `tmux display-message`
- tmux pane capture
- one `ps` process snapshot for pane process trees

This is how `acw` proves:

- pane existence
- live window names
- pane shell pid
- whether a live Codex process exists in the pane

### 4. Codex SQLite state and process-owned evidence

Thread identity is proven only from strong evidence:

- pid -> thread mappings from Codex `state_*.sqlite`
- explicit `codex resume <thread-id>` argv
- thread ids embedded in Codex-owned open files

`acw` never guesses a thread id from cwd, timestamps, or other weak signals.

### 5. `codex-tui.log`

The daemon reads:

```text
~/.codex/log/codex-tui.log
```

It uses that log for:

- completion events
- interrupt events
- startup replay of one pending completion


## Why The Shared Daemon Exists

The older design used one `auto_continue_logwatch.py` process per pane. That
caused a lot of avoidable complexity:

- one watcher pid per pane
- pid files as primary runtime identity
- repeated `ps` scans just to discover watcher state
- pause/resume implemented as OS stop/continue
- duplicated global `codex-tui.log` tailers

The shared daemon simplifies the control plane:

- one daemon process per tmux socket
- one reconcile loop
- one desired-state model
- one runtime-state model
- per-session state machines instead of manager-owned per-pane processes
- one shared `codex-tui.log` tail feeding all managed sessions
- no manager-side legacy process migration path


## Daemon Reconcile Loop

The hidden `_daemon` subcommand is started on demand by the CLI.

At a high level the loop does this:

1. Drain new Codex events from one shared `codex-tui.log` tail.
2. Load all session rows for the current tmux socket scope.
3. Reconcile them against the in-memory per-session runtime map.
4. Create missing runtimes.
5. Drop runtimes whose session was deleted.
6. Reinitialize runtimes whose `(pane, message, generation)` signature changed.
7. Route the drained events to the matching session runtime by `thread_id`.
8. Refresh session health and upsert one runtime row per live managed session.
9. Exit automatically after a short idle period when the scope has no sessions.

That means normal commands are cheap:

- they ensure the daemon exists
- they send one local Unix-socket RPC to the daemon
- the daemon persists desired state and waits for runtime convergence
- progress lines stream back over the same socket

They do not spawn or discover per-pane manager processes anymore.


## Daemon Control Socket

Each tmux scope now has one daemon control socket:

- `~/.codex/acw_daemon.<scope>.sock`

The CLI uses it for mutating commands like:

- `start`
- `stop`
- `pause`
- `resume`
- `restart`
- `edit`
- `migrate`

The transport is intentionally small:

- local Unix domain socket only
- one JSON request per connection
- streamed `progress` messages
- one final `result` or `error`

SQLite remains the durable source of truth, but the socket is the fast control
path. That removes the old manager-side "write state, then poll the database"
behavior from normal mutations and gives `restart --restart-codex` and similar
flows a place to surface step-by-step daemon progress.


## Session Runtime Model

Each managed session runtime inside the daemon keeps:

- pane
- thread id
- continue message
- generation
- paused flag
- last handled turn
- one pending startup completion, if any
- recent health state
- watch-log path

The daemon uses shared helper code from `bin/auto_continue_logwatch.py` for:

- Codex completion parsing
- interrupt parsing
- pane-visible error detection
- pending-startup completion detection
- health evaluation
- tmux `send-keys`

The important daemon-specific behaviors are:

- `pause` is a logical session flag, not `SIGSTOP`
- `resume` still restarts the generation instead of thawing an old reader, so
  stale log backlog is not replayed
- one shared log tail means the daemon never duplicates global log reads across
  sessions

This preserves the correctness fix from the old resume rewrite without keeping
per-pane OS processes around.


## Manager Command Flows

### `start`

`start` now means:

1. Resolve the target pane.
2. Prove the live Codex thread.
3. Resolve or edit the continue message.
4. Reject conflicting pane/thread/session state.
5. Upsert the thread-keyed session row for that pane.
6. Ensure the shared daemon is running.
7. Wait for the matching runtime row.

### `stop`

`stop` is destructive manager intent:

- remove the saved session row
- let the daemon tear down the runtime row
- forget the session from `acw status`

### `pause`

`pause` sets `paused=true` in the saved session row and waits for the daemon
runtime row to reflect that state.

### `resume`

`resume` does not unfreeze an old reader in place.

Instead it restarts the managed session generation from the current pane/thread
state so backlog is not replayed.

### `restart`

`restart` is implemented as:

1. resolve pane and prove thread
2. load the saved message unless `--message` overrides it
3. optionally relaunch Codex with `codex resume <thread-id>`
4. bump the session `generation`
5. ensure the daemon is running
6. wait for the runtime row with the new generation

With no target, `restart` iterates all live managed panes for the current
scope.

### `restart --restart-codex`

The Codex relaunch path is still explicit and strict:

1. prove the current thread
2. terminate the live Codex subprocess tree in the pane
3. wait for a real shell prompt
4. run `codex resume <thread-id>`
5. handle the directory-trust prompt if it appears
6. request the new session generation

### `edit`

`edit` is just:

1. prove pane and thread
2. seed the editor from the saved session message
3. persist the new message
4. restart the session generation if the pane is managed

### `status`

`status` builds a fresh runtime model from:

- live panes
- live managed runtime rows
- saved session rows

It does not collapse ambiguity away.
If there are duplicate or missing entities, the table shows them as invariant
errors.

### `doctor`

`doctor` is the strict proof path for one target.
It reports:

- tmux reachability
- auth/state prerequisites
- target resolution
- live thread identity
- thread provenance
- runtime/session agreement
- a concrete next command when possible

### `repair`

`repair` is built on the same invariant model as `status` and `doctor`.

It only auto-applies safe changes, such as:

- recreating a missing saved session for a proven live pane
- restarting one mismatched managed session generation
- syncing the saved session name to the live tmux window name

It does not guess how to resolve genuinely ambiguous multi-pane or
multi-thread corruption.

### `cleanup`

`cleanup` is intentionally narrower than `repair`.

It only removes stale artifacts, such as:

- dead runtime rows from a vanished daemon
- dead session rows whose thread has neither a live pane nor a live worker


## Runtime Snapshot Types

The command-layer runtime model lives in `bin/auto_continue_watchd.py`, while
the probing, persistence, daemon-session, and UI helpers now live in
`bin/acw_probe.py`, `bin/acw_state.py`, `bin/acw_daemon.py`, and
`bin/acw_ui.py`.

The main record types are:

- `LivePaneRecord`
- `LiveWatcherRecord`
- `SavedSessionRecord`
- `RuntimeSnapshot`
- `RuntimeModel`

These are indexed separately by:

- pane id
- thread id
- session name

The critical design rule is:

**never merge multiplicity away.**

If two workers claim one thread, or two panes claim one thread, the model keeps
both and reports the invariant violation.


## Thread Discovery

Thread discovery is strict by design.

For one live pane:

1. `tmux list-panes` provides the pane shell pid.
2. One `ps` snapshot walks the process tree below that shell.
3. Any live `codex` process is mapped to a thread id through:
   - Codex SQLite pid rows first
   - then explicit `resume <thread-id>` argv
   - then Codex-owned open files

If that still does not prove a thread, `acw` returns an error.


## Rename Handling

The tmux `window-renamed` hook is still installed by the manager.

When a window name changes, `acw` syncs the saved session `name` field for the
thread attached to panes in that window.

Because pane/thread identity is rebuilt from live state on every command, a
rename is naturally visible to later `status`, `doctor`, and `edit` calls even
without trusting the hook alone.


## State Store

All manager-owned state lives under `~/.codex/`.

Important files:

- `acw_state.sqlite`
  - `sessions` table: desired state for one managed session
  - `runtimes` table: observed daemon runtime state for one live managed session
- `acw_daemon.<scope>.pid`
  - daemon pid for one tmux socket scope
- `acw_daemon.<scope>.sock`
  - daemon control socket for one tmux socket scope
- `acw_daemon.<scope>.log`
  - daemon stdout/stderr log
- `auto_continue_logwatch.<pane>.log`
  - per-session event log written by the daemon


## Performance

The daemon rewrite improves command latency in two ways:

- no manager-side discovery of one OS watcher process per pane
- one shared `codex-tui.log` reader instead of one per managed session
- no process spawning for normal start/edit/restart flows

On this checkout after the rewrite:

- `status` is about `0.57s`
- `edit %0` is about `0.23s`

After removing the transitional legacy layer and collapsing the daemon to one
shared log tail plus per-session state objects, a direct
`_build_runtime_snapshot()` microbenchmark on this checkout is about `85ms`
best-of-3 for 5 loops.

The remaining cost is mostly:

- live tmux metadata
- one process-tree snapshot for thread proof
- rendering


## Testing

The rewrite is covered at three levels:

- unit tests for manager/runtime logic in `test/test_watchd_unit.py`
- unit tests for worker logic in `test/test_logwatch_unit.py`
- real Codex integration tests in `test/test_real_codex_integration.py`

The important real-manager cases are:

- `start` against a live Codex pane
- multi-target `resume`
- `restart --restart-codex`
- dead-worker recovery
- `doctor` and status behavior on live panes


## Summary

The current architecture is:

- strict about identity
- explicit about ambiguity
- centered on thread-keyed desired state
- backed by one daemon per tmux socket
- still external to Codex and tmux internals

That keeps the operator model simple:

- `start` means "manage this pane/thread"
- `pause` means "stop continuing this managed session"
- `resume` and `restart` mean "request a fresh session generation"
- `stop` means "forget this managed session"
- `repair` means "fix live invariant problems"
- `cleanup` means "delete stale artifacts"
