#!/usr/bin/env bash
# End-to-end test: start codex in a background tmux session, start the watcher,
# and verify that a task_complete event from the rollout JSONL is detected and
# triggers the auto-continue send.
#
# Requirements:
#   - tmux available
#   - codex CLI on $PATH
#   - Valid codex credentials / API key
#
# Usage:
#   bash test/test_rollout_e2e.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SESSION_NAME="test-rollout-e2e-$$"
PROJECT_CWD="$(mktemp -d)"
STATE_DIR="$PROJECT_CWD/.codex"
WATCH_LOG=""
PANE_ID=""
WATCHER_PID=""
BEFORE_LIST="$(mktemp)"
THREAD_ID_RE='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

cleanup() {
    local exit_code=$?
    set +e
    if [[ -n "$WATCHER_PID" ]] && kill -0 "$WATCHER_PID" 2>/dev/null; then
        kill "$WATCHER_PID" 2>/dev/null
        wait "$WATCHER_PID" 2>/dev/null
    fi
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null
    rm -rf "$PROJECT_CWD" "$BEFORE_LIST"
    if [[ $exit_code -eq 0 ]]; then
        echo "[PASS] test_rollout_e2e"
    else
        echo "[FAIL] test_rollout_e2e (exit $exit_code)"
    fi
    exit $exit_code
}
trap cleanup EXIT

mkdir -p "$STATE_DIR"

# Snapshot existing rollout files so we can identify the new one by diffing.
find ~/.codex/sessions -name 'rollout-*.jsonl' 2>/dev/null | sort > "$BEFORE_LIST"

# ── 1. Create a detached tmux session ──────────────────────────────────────────
echo "[test] creating tmux session: $SESSION_NAME"
tmux new-session -d -s "$SESSION_NAME" -x 200 -y 50
PANE_ID="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_id}' | head -n1)"
echo "[test] pane_id=$PANE_ID"

# ── 2. Start codex with a trivial prompt ───────────────────────────────────────
echo "[test] starting codex in pane $PANE_ID"
tmux send-keys -t "$PANE_ID" "codex 'say the word hello and nothing else'" C-m

# Wait for codex to create a new rollout file with a task_complete event.
echo "[test] waiting for codex to complete first turn..."
DEADLINE=$((SECONDS + 60))
THREAD_ID=""
ROLLOUT_FILE=""
while [[ $SECONDS -lt $DEADLINE ]]; do
    # Find rollout files that did NOT exist before we started codex.
    while IFS= read -r f; do
        [[ -n "$f" ]] || continue
        if grep -q task_complete "$f" 2>/dev/null; then
            ROLLOUT_FILE="$f"
            base="$(basename "$f")"
            if [[ "$base" =~ ^rollout-[0-9T-]+-($THREAD_ID_RE)\.jsonl$ ]]; then
                THREAD_ID="${BASH_REMATCH[1]}"
            fi
            break 2
        fi
    done < <(comm -13 "$BEFORE_LIST" <(find ~/.codex/sessions -name 'rollout-*.jsonl' 2>/dev/null | sort))
    sleep 2
done

if [[ -z "$THREAD_ID" ]]; then
    echo "[FAIL] codex did not produce a task_complete event within 60s"
    echo "=== new rollout files ==="
    comm -13 "$BEFORE_LIST" <(find ~/.codex/sessions -name 'rollout-*.jsonl' 2>/dev/null | sort)
    echo "=== pane capture ==="
    tmux capture-pane -t "$PANE_ID" -p -S -30 2>/dev/null || true
    exit 1
fi
echo "[test] codex completed turn; thread_id=$THREAD_ID"
echo "[test] rollout file: $(basename "$ROLLOUT_FILE")"

# ── 3. Start the watcher ──────────────────────────────────────────────────────
PANE_KEY="$(echo "$PANE_ID" | sed 's/[^a-zA-Z0-9._-]/_/g')"
WATCH_LOG="$STATE_DIR/auto_continue_logwatch.${PANE_KEY}.log"

echo "[test] starting watcher for pane=$PANE_ID thread=$THREAD_ID"
python3 "$ROOT/bin/auto_continue_logwatch.py" \
    --cwd "$PROJECT_CWD" \
    --pane "$PANE_ID" \
    --thread-id "$THREAD_ID" \
    --message "test continue" \
    --cooldown-secs 0.5 \
    --send-delay-secs 0.1 \
    --enter-delay-secs 0.1 \
    --state-file "$STATE_DIR/auto_continue_logwatch.${PANE_KEY}.state.local.json" \
    --watch-log "$WATCH_LOG" &
WATCHER_PID=$!
echo "[test] watcher started: pid=$WATCHER_PID"

# Give the watcher a moment to open file handles and seek to end.
sleep 2

# Verify watcher found the rollout file.
if ! grep -q "rollout: watching" "$WATCH_LOG" 2>/dev/null; then
    echo "[FAIL] watcher did not find rollout file"
    cat "$WATCH_LOG" 2>/dev/null || echo "(no log)"
    exit 1
fi
echo "[test] watcher found rollout file"

# ── 4. Send another prompt to codex to generate a new task_complete ───────────
echo "[test] sending second prompt to codex"
tmux send-keys -t "$PANE_ID" -l "what is 1+1" && sleep 0.2 && tmux send-keys -t "$PANE_ID" C-m

# ── 5. Wait for the watcher to detect task_complete and send continue ─────────
echo "[test] waiting for watcher to detect task_complete and send continue..."
DEADLINE=$((SECONDS + 90))
DETECTED=0
while [[ $SECONDS -lt $DEADLINE ]]; do
    if [[ -f "$WATCH_LOG" ]] && grep -q "continue: sent" "$WATCH_LOG" 2>/dev/null; then
        DETECTED=1
        break
    fi
    if ! kill -0 "$WATCHER_PID" 2>/dev/null; then
        echo "[FAIL] watcher process died unexpectedly"
        cat "$WATCH_LOG" 2>/dev/null || true
        exit 1
    fi
    sleep 2
done

if [[ $DETECTED -ne 1 ]]; then
    echo "[FAIL] watcher did not send continue within 90s"
    echo "=== watcher log ==="
    cat "$WATCH_LOG" 2>/dev/null || echo "(no log)"
    echo "=== pane capture ==="
    tmux capture-pane -t "$PANE_ID" -p -S -30 2>/dev/null || true
    exit 1
fi

echo "[test] watcher detected task_complete and sent continue!"
echo "=== watcher log ==="
cat "$WATCH_LOG"

# ── 6. Verify the pane received the continue message ─────────────────────────
sleep 3
PANE_TEXT="$(tmux capture-pane -t "$PANE_ID" -p -S -40 2>/dev/null || true)"
if echo "$PANE_TEXT" | grep -q "test continue"; then
    echo "[test] confirmed: continue message visible in pane"
else
    echo "[warn] continue message not visible in pane capture (may have scrolled)"
fi

echo "[test] all checks passed"
exit 0
