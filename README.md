# codex-auto-continue

`acw` watches a Codex tmux pane and sends your follow-up prompt after each
completed turn.

It is designed for long-running Codex sessions where you want a stable
"continue" loop, but still want to interrupt, inspect, edit the prompt, or
recover from tmux problems without losing control of the pane.

## Requirements

- Linux
- `python3`
- `tmux`
- `pstree`
- Codex CLI

`rich` is optional. If it is installed, `acw status` uses the formatted table;
otherwise it falls back to a plain-text summary.

## Install

Install from a local checkout with `uv`:

```bash
uv tool install --editable /path/to/codex-auto-continue
```

From inside the repo:

```bash
uv tool install --editable .
```

After pulling new changes:

```bash
uv tool install --editable --reinstall /path/to/codex-auto-continue
```

To try it without installing:

```bash
uv tool run --from /path/to/codex-auto-continue acw --help
```

## Quick Start

1. Start Codex in a tmux pane.

```bash
codex
```

2. Check that `acw` can see the pane and thread.

```bash
acw doctor
```

3. Start the watcher from any shell attached to the same tmux server.

```bash
acw start uvm
acw start %6
acw start 2
```

`start` accepts a pane id (`%6`), window index (`2`), `session:window`
(`0:2`), or exact tmux window name (`uvm`).

4. Watch the control panel.

```bash
acw
```

If thread discovery is not ready yet, `start` fails instead of guessing.
If you already know the Codex thread id, pass it explicitly:

```bash
acw start uvm 019cb235-bc2c-7920-8832-f2d5656fead8
```

## Everyday Commands

```text
acw                  Show status
acw status --details Show full watcher details
acw start <target>   Start a watcher
acw stop [target]    Stop one watcher or all watchers
acw pause [target]   Pause one watcher or all watchers
acw resume [target]  Resume one paused watcher or all paused watchers
acw restart [target] Restart one watcher or all watchers
acw edit <target>    Edit the stored continue prompt, then restart
acw doctor [target]  Diagnose tmux, auth, pane, and watcher state
acw cleanup          Remove stale watcher files
```

For all-watcher operations, prefer the no-target forms:

```bash
acw pause
acw resume
acw restart
```

Quoted `'*'` is accepted too, but an unquoted `*` will be expanded by your
shell before `acw` sees it.

## Continue Message

If you do not pass `--message` or `--message-file`, `acw start` opens `$EDITOR`
so you can write a multi-line continue prompt. An empty message cancels the
start.

Examples:

```bash
acw start %0 --message "continue and focus on tests"
acw start tests --message-file /path/to/message.txt
acw edit tests
```

Default message file:

```text
~/.codex/auto_continue.message.txt
```

`acw` creates that file from the bundled template on first run if it does not
exist yet.

## Status And Health

`acw status` is the main control panel. It shows:

- the live tmux window and pane
- whether the watcher is `running`, `warn`, `paused`, or `dead`
- when the Codex thread started
- when `acw` last sent a continue prompt
- recent visible agent output from the pane
- the stored continue message

Use:

```bash
acw status --details
```

to see `HEALTH_DETAIL`, state file paths, watch logs, and the last recorded
event for each watcher.

When the summary table shows a degraded watcher, `acw` prints a follow-up
recommendation such as:

```text
Recommendation: run acw doctor uvm
```

## Interrupts And Failures

User interrupt behavior is intentionally different from real Codex failures.

- Pressing `Escape` in the Codex pane skips exactly one interrupted turn.
- The watcher stays running.
- After you send your next manual prompt, automatic continue resumes on the
  next completed turn.

Real Codex error banners still auto-pause the watcher, including:

- authentication failures
- quota / usage limit failures
- similar pane-visible Codex errors

After fixing the problem in the pane, resume with:

```bash
acw resume <target>
```

If a watcher process is gone entirely, `acw status` shows it as `dead` even if
the last on-disk health snapshot looked healthy.

## Doctor

`acw doctor` is the first command to run when something looks wrong.

Inside tmux, `acw doctor` with no target checks the current pane. You can also
target a specific watcher or pane:

```bash
acw doctor
acw doctor uvm
acw doctor %6
```

It checks:

- whether `~/.codex` is writable
- whether Codex auth state exists
- whether the tmux server is reachable
- whether the target pane resolves
- whether a Codex thread is discoverable
- whether a watcher is running, paused, degraded, or dead

It also prints an explicit recommended next command when appropriate.

## Troubleshooting

`acw start <target>` says `could not determine thread_id`

- Codex is not fully started yet in that pane, or it is not a Codex pane.
- Wait for Codex startup to settle, then retry.
- If needed, use `acw doctor <target>` or pass the thread id explicitly.

`acw status` says `dead`

- The watcher process is gone.
- Run `acw restart <target>` or `acw start <target>`.

`acw status` says `warn`

- Run `acw doctor <target>`.
- `warn` usually means degraded but still partially functional, not necessarily
  dead.

tmux socket disappeared

- `acw` prints a recovery hint when it can identify the tmux server pid.
- Recreate the socket with the suggested command, for example:

```bash
kill -USR1 1996933
```

## How It Works

The watcher reads `~/.codex/log/codex-tui.log` and reacts to Codex completion
events.

Supported completion signals:

- older Codex: `post sampling token usage ... needs_follow_up=false`
- current Codex: `codex_core::tasks: close`

Thread discovery is pane-local. `acw` maps the pane's live Codex process to the
thread id using the process tree and Codex's local SQLite state. It does not
guess from unrelated global activity.

Canonical watcher/session state is stored under:

```text
~/.codex/acw_session.<thread-id>.json
```

Watcher processes also record the tmux socket they are using, which avoids pane
id collisions between your real tmux server and the isolated real-Codex test
harness.

## Development And Tests

Fast checks:

```bash
bash test/smoke.sh
python3 -m unittest test.test_watchd_unit test.test_logwatch_unit
```

Real end-to-end suite:

```bash
bash test/test_rollout_e2e.sh
```

The real suite:

- uses a dedicated private tmux server on its own socket
- runs real Codex sessions
- does not touch your existing tmux sessions
- stores failure artifacts under `~/.codex/auto-continue-e2e-tmp/failures/`

Current real-Codex coverage includes:

- basic continue delivery
- interrupt skip and auto-resume after the next manual prompt
- manager start for plain `codex` panes
- manager start for `codex resume <thread-id>` panes
- `doctor`, `edit`, `status`, dead-watcher reporting, and tmux socket recovery
