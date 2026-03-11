# Engineering Log

## 2026-03-11

- Change: made `acw status` fall back to live `watcher_rows()` data when tmux pane scans are unavailable. This keeps pane ids, watcher pids, and `running`/`paused` state visible even if the tmux client environment is stale or the server socket is temporarily unreachable.
- Change: fixed stale-socket tmux fallback in `run_tmux()`. When `$TMUX` points to a dead socket, the retry path now clears `TMUX` and `TMUX_PANE` before probing the default tmux server, so window-name and window-index resolution keep working after a broken client env leaks into the shell.
- Change: shortened thread IDs in the default `acw status` table to `prefix…suffix` form so the summary view stays compact while `acw status --details` still shows the full thread id.
- Realization: after the CLI cleanup, the highest-friction operator mistake is shell expansion of bare `*`. The docs now prefer the no-target forms (`pause`, `resume`, `restart`) and treat quoted `'*'` as a secondary explicit form.
- Change: refreshed `README.md`, `docs/ARCHITECTURE.md`, and `AGENTS.md` to match the current acw surface: Python-only entrypoint, no `recover`, thread-keyed session state, private-socket real-Codex E2E harness, and the current real test count in the wrapper suite.

## 2026-03-10

- Change: removed the `recover` subcommand and all related attach/rebind logic. The feature was broken and added a large amount of untested complexity around tmux session creation, detached codex processes, and pane remapping.
- Change: removed the legacy notify-hook implementation (`legacy/auto_continue_notify_hook.py`) and stopped compiling it in smoke tests.
- Change: removed pane-keyed legacy state handling. Runtime state is now thread-keyed only via `acw_session.<thread>.json`.
- Change: simplified `cleanup` to operate directly on thread-keyed session files and made stale session cleanup unconditional now that recovery is gone.
- Realization: current Codex builds do not reliably emit rollout `task_complete` records in this environment; they do emit `codex_core::tasks: close` lines in `~/.codex/log/codex-tui.log`.
- Change: taught `auto_continue_logwatch.py` and the real E2E script to key off `codex-tui.log` completion lines, while still keeping rollout JSONL as a supplemental startup/health source when present.
- Verification: `bash test/test_rollout_e2e.sh` now passes against a real Codex session on an isolated tmux socket without touching the user's existing tmux server.
- Change: replaced the one-off shell E2E script with a reusable Python harness in `test/support/real_codex_harness.py`. Real-Codex scenarios now live in `test/test_real_codex_contract.py` and `test/test_real_codex_integration.py`.
- Realization: this shell environment uses `umask 0117`, which strips execute bits from newly created directories. The harness now normalizes temp directory modes and launches tmux/watcher subprocesses under `umask 077` so isolated test homes and tmux sockets are traversable.
- Realization: when no repo-local env file exists, current Codex authentication comes from the user's live `~/.codex` state rather than exported env vars. The harness now uses an isolated home only when an explicit env file is available; otherwise it reuses the real Codex home for authentication while still isolating tmux and watcher state.
- Change: tightened the real Codex contract test so it now requires a real completion signal in `codex-tui.log`. If Codex stops emitting a supported log completion signal, the contract suite now fails instead of silently passing on rollout-only evidence.
- Change: replaced ad hoc watcher health transitions with `compute_health()`, a pure helper that treats `codex-tui.log` as the primary source. `rollout channel closed` now becomes `warn` after a matching Codex log completion is observed, and only remains `error` if no matching log completion arrives within a short grace window.
- Change: removed the separate `pause-all`, `resume-all`, and `restart-all` commands. `pause *`, `resume *`, and `restart *` now cover the same behavior with a smaller CLI surface.
- Change: removed `bin/auto_continue_watchd.sh`. The Python entrypoint is now the only supported manager interface, which removes one more compatibility layer and keeps smoke/docs aligned with the real implementation.
- Change: made `restart` with no target mean "restart all running watchers". This avoids shell glob expansion pitfalls from `restart *` while keeping the wildcard form as an explicit synonym.

## 2026-03-06

- Realization: `acw` had already started moving toward thread-keyed state (`acw_session.<thread>.json`), but runtime behavior still allowed ambiguous startup (`thread-id=auto`).
- Change: made `start` strict. If thread-id cannot be detected, it now fails immediately instead of falling back to `auto`.
- Change: installed a tmux `window-renamed` hook that calls back into `auto_continue_watchd.py` and synchronizes the renamed window title into thread-keyed session state.
- Change: removed `/proc` usage for watcher argv parsing and stopped-process checks. Watcher argv now comes from `ps` tokenization, and stop-state checks use `ps -o state`.
- Realization: `run_tmux` could silently fall back to a different tmux server when a command failed inside an active client.
- Change: when `TMUX` points to a healthy current client/server, `run_tmux` no longer falls back to env-cleared tmux. Also, session targeting now prefers the invoking client session name (`#{session_name}`) before using attached/first-session heuristics.
- Change: `run_tmux` now parses socket path from `$TMUX` and uses it directly (unless overridden by `AUTO_CONTINUE_TMUX_SOCKET`) to anchor commands to the current server.
