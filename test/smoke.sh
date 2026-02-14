#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "[smoke] bash syntax"
bash -n "$ROOT/bin/auto_continue_watchd.sh"

echo "[smoke] python syntax"
python3 -m py_compile "$ROOT/bin/auto_continue_logwatch.py"
python3 -m py_compile "$ROOT/legacy/auto_continue_notify_hook.py"

echo "[smoke] usage output"
"$ROOT/bin/auto_continue_watchd.sh" bogus >/dev/null 2>&1 || true

echo "[smoke] ok"

