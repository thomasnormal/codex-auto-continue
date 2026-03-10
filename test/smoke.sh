#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "[smoke] bash syntax"
bash -n "$ROOT/test/test_rollout_e2e.sh"

echo "[smoke] python syntax"
python3 -m py_compile "$ROOT/bin/auto_continue_watchd.py"
python3 -m py_compile "$ROOT/bin/auto_continue_logwatch.py"

echo "[smoke] usage output"
python3 "$ROOT/bin/auto_continue_watchd.py" bogus >/dev/null 2>&1 || true

echo "[smoke] ok"
