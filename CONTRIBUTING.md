# Contributing

## Development Setup

Install the CLI into an isolated tool environment from a local checkout:

```bash
uv tool install --editable .
```

Reinstall after local code changes:

```bash
uv tool install --editable --reinstall .
```

## Fast Validation

Run the fast path before opening a pull request:

```bash
bash test/smoke.sh
python3 -m unittest test.test_watchd_unit test.test_logwatch_unit test.test_real_codex_harness_unit
```

Run the real-Codex isolated suite only when you are touching watcher/Codex or tmux integration:

```bash
bash test/test_rollout_e2e.sh
```

That suite uses a private tmux socket and isolated Codex home seeded from your auth state. It must not touch the live tmux server.

## Engineering Expectations

- Prefer unit or regression coverage before changing runtime behavior.
- Keep `docs/ENGINEERING_LOG.md` updated with design decisions, surprises, and regressions you found.
- Keep operator-facing output short, explicit, and actionable.
- Remove dead compatibility paths instead of layering new fallbacks on top.

## tmux Safety

Do not start a fresh tmux server with bare `tmux`, `tmux new`, or `tmux new-session`.

Use explicit subcommands against the existing server, for example:

```bash
tmux list-windows
tmux send-keys -t %6 Escape
```

## Pull Requests

Pull requests should include:

- a short summary of the operator-facing change
- why the change was needed
- the validation commands you ran
- screenshots or terminal captures only when they clarify a UI/status change
