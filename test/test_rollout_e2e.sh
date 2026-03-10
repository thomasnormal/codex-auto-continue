#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export AUTO_CONTINUE_RUN_REAL_CODEX_TESTS=1

cd "$ROOT"
python3 -m unittest \
    test.test_real_codex_contract \
    test.test_real_codex_integration
