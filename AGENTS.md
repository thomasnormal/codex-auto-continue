# Repository Guidelines

## Project Structure & Module Organization
Core entrypoints live in `bin/`: `auto_continue_watchd.py` manages watchers and `auto_continue_logwatch.py` tails Codex events. Tests live in `test/` with Python unit suites in `test_watchd_unit.py`, `test_logwatch_unit.py`, `test_real_codex_contract.py`, and `test_real_codex_integration.py`. `test/smoke.sh` and `test/test_rollout_e2e.sh` are the fast CLI and real-Codex integration wrappers. Architecture notes and the running engineering log live in `docs/`. Example prompt text lives in `examples/messages/`.

## Build, Test, and Development Commands
Run the watcher locally with `python3 bin/auto_continue_watchd.py status` or `python3 bin/auto_continue_watchd.py start %6 --message "continue"`. Use `bash test/smoke.sh` for fast syntax and CLI checks. Use `python3 -m unittest test.test_watchd_unit test.test_logwatch_unit` for isolated manager and watcher logic. Use `bash test/test_rollout_e2e.sh` only when `tmux`, `codex`, and valid credentials are available; it launches real-Codex tests on a private tmux socket so it does not touch the existing server.

## Coding Style & Naming Conventions
Follow existing Python and shell style: 4-space indentation in Python, `set -euo pipefail` in shell, and small stdlib-first helpers over new dependencies. Prefer snake_case for Python functions, variables, and test names. Keep CLI output short and operator-focused. Preserve the current file naming pattern: `auto_continue_*.py` for runtime code and `test_*.py` or `test_*.sh` for tests.

## Testing Guidelines
Prefer test-driven changes. Add or extend a unit or regression test before fixing behavior, especially for pane resolution, thread detection, completion-source health, and thread-keyed state handling. Put pure logic coverage in `test/test_watchd_unit.py` and `test/test_logwatch_unit.py` using `unittest` and `unittest.mock`. Reserve the real-Codex harness for contract and integration behavior. Name tests after the behavior under test, for example `test_resolve_thread_id_fails_when_unknown`.

## Commit & Pull Request Guidelines
Match the existing history: short, imperative, capitalized commit subjects such as `Fix stop not killing paused watchers` or `Replace all-commands with wildcard targets`. Commit regularly, but do not create branches or worktrees unless explicitly approved. PRs should describe the operator-facing change, note tmux/Codex prerequisites, list commands run, and include terminal output or screenshots only when they clarify a UI or status-table change.

## Operational Notes
Keep `docs/ENGINEERING_LOG.md` updated with realizations, surprises, and design decisions. Runtime state is stored under `~/.codex/`; avoid assumptions that depend on the current working directory. When working with tmux, do not start a fresh server with bare `tmux` or `tmux new-session`; target the existing server with explicit subcommands instead.
