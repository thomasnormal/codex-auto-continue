#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "[smoke] bash syntax"
bash -n "$ROOT/test/test_rollout_e2e.sh"

echo "[smoke] python syntax"
python3 -m py_compile "$ROOT/bin/auto_continue_watchd.py"
python3 -m py_compile "$ROOT/bin/auto_continue_logwatch.py"
python3 -m py_compile "$ROOT/bin/codex_team.py"

echo "[smoke] usage output"
python3 "$ROOT/bin/auto_continue_watchd.py" --help >/dev/null
python3 "$ROOT/bin/auto_continue_watchd.py" bogus >/dev/null 2>&1 || true
python3 "$ROOT/bin/codex_team.py" --help >/dev/null

echo "[smoke] uv tool entrypoint"
(
  umask 022
  UV_NO_CACHE=1 uv tool run --from "$ROOT" acw --help >/dev/null
  UV_NO_CACHE=1 uv tool run --from "$ROOT" codex-team --help >/dev/null
)

echo "[smoke] ok"
