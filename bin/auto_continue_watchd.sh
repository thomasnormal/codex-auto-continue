#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
resolve_project_cwd() {
  if [[ -n "${AUTO_CONTINUE_PROJECT_CWD:-}" ]]; then
    if [[ -d "$AUTO_CONTINUE_PROJECT_CWD" ]]; then
      (
        cd "$AUTO_CONTINUE_PROJECT_CWD"
        pwd -P
      )
      return
    fi
    printf '%s\n' "$AUTO_CONTINUE_PROJECT_CWD"
    return
  fi

  local pwd_real git_root
  pwd_real="$(pwd -P)"
  git_root="$(git -C "$pwd_real" rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -n "$git_root" ]]; then
    printf '%s\n' "$git_root"
    return
  fi

  if [[ "$(basename "$pwd_real")" == ".codex" ]]; then
    printf '%s\n' "$(dirname "$pwd_real")"
    return
  fi

  printf '%s\n' "$pwd_real"
}

PROJECT_CWD="$(resolve_project_cwd)"
STATE_DIR="$PROJECT_CWD/.codex"
SCRIPT="$SCRIPT_DIR/auto_continue_logwatch.py"
DEFAULT_MSG_FILE="$STATE_DIR/auto_continue.message.txt"
if [[ ! -f "$DEFAULT_MSG_FILE" ]]; then
  DEFAULT_MSG_FILE="$REPO_ROOT/examples/messages/default_continue_message.txt"
fi
LEGACY_PID_FILE="$STATE_DIR/auto_continue_logwatch.pid"
LEGACY_RUN_LOG="$STATE_DIR/auto_continue_logwatch.runner.log"
THREAD_ID_RE='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
PARSED_THREAD_ARG=""
PARSED_MESSAGE_MODE=""
PARSED_MESSAGE_VALUE=""
PARSED_MESSAGE_EXPLICIT=""

mkdir -p "$STATE_DIR"

sanitize_key() {
  local s="$1"
  s="${s//[^a-zA-Z0-9._-]/_}"
  printf '%s' "$s"
}

key_from_pane() {
  local pane="$1"
  sanitize_key "$pane"
}

pid_file_for_key() {
  local key="$1"
  echo "$STATE_DIR/auto_continue_logwatch.$key.pid"
}

run_log_for_key() {
  local key="$1"
  echo "$STATE_DIR/auto_continue_logwatch.$key.runner.log"
}

watch_log_for_key() {
  local key="$1"
  echo "$STATE_DIR/auto_continue_logwatch.$key.log"
}

state_file_for_key() {
  local key="$1"
  echo "$STATE_DIR/auto_continue_logwatch.$key.state.local.json"
}

global_pause_file() {
  echo "$STATE_DIR/AUTO_CONTINUE_PAUSE"
}

pause_file_for_key() {
  local key="$1"
  echo "$STATE_DIR/AUTO_CONTINUE_PAUSE.$key"
}

message_meta_file_for_key() {
  local key="$1"
  echo "$STATE_DIR/auto_continue_logwatch.$key.message.local.txt"
}

write_message_meta() {
  local key="$1"
  local mode="$2"
  local value="$3"
  local path
  path="$(message_meta_file_for_key "$key")"
  {
    echo "mode=$mode"
    echo "value=$value"
  } > "$path"
}

is_thread_id() {
  local maybe="$1"
  [[ "$maybe" =~ ^$THREAD_ID_RE$ ]]
}

extract_resume_thread_id() {
  local args="$1"
  if [[ "$args" =~ [[:space:]]resume[[:space:]]($THREAD_ID_RE)($|[[:space:]]) ]]; then
    printf '%s' "${BASH_REMATCH[1],,}"
    return 0
  fi
  return 1
}

thread_id_from_snapshot_file() {
  local file="$1"
  local base
  base="$(basename "$file")"
  if [[ "$base" =~ ^($THREAD_ID_RE)\.sh$ ]]; then
    printf '%s' "${BASH_REMATCH[1],,}"
    return 0
  fi
  return 1
}

detect_thread_from_shell_snapshot() {
  local pane="$1"
  local snapshot_dir="$HOME/.codex/shell_snapshots"
  [[ -d "$snapshot_dir" ]] || return 1

  local snapshot_files=()
  shopt -s nullglob
  snapshot_files=("$snapshot_dir"/*.sh)
  shopt -u nullglob
  [[ "${#snapshot_files[@]}" -gt 0 ]] || return 1

  local file tid
  while IFS= read -r file; do
    if grep -Fq "declare -x TMUX_PANE=\"$pane\"" "$file"; then
      tid="$(thread_id_from_snapshot_file "$file" || true)"
      if is_thread_id "$tid"; then
        printf '%s' "$tid"
        return 0
      fi
    fi
  done < <(ls -t "${snapshot_files[@]}" 2>/dev/null)

  return 1
}

closest_thread_id_for_start_epoch() {
  local target_epoch="$1"
  local sessions_glob="$HOME/.codex/sessions/*/*/*/rollout-*.jsonl"

  local best_tid=""
  local best_diff=-1
  local file base ts tid candidate_epoch diff

  shopt -s nullglob
  local session_files=($sessions_glob)
  shopt -u nullglob
  [[ "${#session_files[@]}" -gt 0 ]] || return 1

  for file in "${session_files[@]}"; do
    base="$(basename "$file")"
    if [[ "$base" =~ ^rollout-([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2})-([0-9]{2})-([0-9]{2})-($THREAD_ID_RE)\.jsonl$ ]]; then
      ts="${BASH_REMATCH[1]}-${BASH_REMATCH[2]}-${BASH_REMATCH[3]} ${BASH_REMATCH[4]}:${BASH_REMATCH[5]}:${BASH_REMATCH[6]}"
      tid="${BASH_REMATCH[7],,}"
      candidate_epoch="$(date -d "$ts" +%s 2>/dev/null || true)"
      [[ "$candidate_epoch" =~ ^[0-9]+$ ]] || continue

      if (( candidate_epoch >= target_epoch )); then
        diff=$((candidate_epoch - target_epoch))
      else
        diff=$((target_epoch - candidate_epoch))
      fi

      if (( best_diff < 0 || diff < best_diff )); then
        best_diff=$diff
        best_tid="$tid"
      fi
    fi
  done

  if is_thread_id "$best_tid"; then
    printf '%s %s' "$best_tid" "$best_diff"
    return 0
  fi
  return 1
}

detect_thread_from_pane_tty() {
  local pane="$1"
  local pane_tty tty_for_ps
  pane_tty="$(tmux display-message -p -t "$pane" '#{pane_tty}' 2>/dev/null || true)"
  [[ -n "$pane_tty" ]] || return 1
  tty_for_ps="${pane_tty#/dev/}"
  [[ -n "$tty_for_ps" ]] || return 1

  # Fast path: thread id in `codex ... resume <thread-id>`.
  local pid args tid
  while read -r pid args; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    [[ "$args" == *codex* ]] || continue
    tid="$(extract_resume_thread_id "$args" || true)"
    if is_thread_id "$tid"; then
      printf '%s' "$tid"
      return 0
    fi
  done < <(ps -t "$tty_for_ps" -o pid=,args= 2>/dev/null || true)

  # Fallback: match codex process start time to closest rollout session start.
  local best_tid=""
  local best_diff=-1
  local lstart start_epoch candidate candidate_tid candidate_diff
  while read -r pid args; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    [[ "$args" == *codex* ]] || continue

    lstart="$(ps -p "$pid" -o lstart= 2>/dev/null | sed 's/^[[:space:]]*//')"
    [[ -n "$lstart" ]] || continue
    start_epoch="$(date -d "$lstart" +%s 2>/dev/null || true)"
    [[ "$start_epoch" =~ ^[0-9]+$ ]] || continue

    candidate="$(closest_thread_id_for_start_epoch "$start_epoch" || true)"
    [[ -n "$candidate" ]] || continue
    candidate_tid="${candidate%% *}"
    candidate_diff="${candidate##* }"
    [[ "$candidate_diff" =~ ^[0-9]+$ ]] || continue
    is_thread_id "$candidate_tid" || continue

    if (( best_diff < 0 || candidate_diff < best_diff )); then
      best_diff="$candidate_diff"
      best_tid="$candidate_tid"
    fi
  done < <(ps -t "$tty_for_ps" -o pid=,args= 2>/dev/null || true)

  # Require a close start-time match to avoid binding the wrong session.
  if is_thread_id "$best_tid" && (( best_diff <= 600 )); then
    printf '%s' "$best_tid"
    return 0
  fi
  return 1
}

detect_thread_id_for_pane() {
  local pane="$1"
  local tid

  tid="$(detect_thread_from_shell_snapshot "$pane" || true)"
  if is_thread_id "$tid"; then
    printf '%s' "$tid"
    return 0
  fi

  tid="$(detect_thread_from_pane_tty "$pane" || true)"
  if is_thread_id "$tid"; then
    printf '%s' "$tid"
    return 0
  fi
  return 1
}

resolve_thread_id() {
  local pane="$1"
  local requested="${2:-}"
  local tid

  if [[ -n "$requested" && "$requested" != "auto" ]]; then
    if is_thread_id "$requested"; then
      printf '%s' "${requested,,}"
      return 0
    fi
    echo "error: invalid thread_id '$requested'" >&2
    return 1
  fi

  tid="$(detect_thread_id_for_pane "$pane" || true)"
  if is_thread_id "$tid"; then
    printf '%s' "$tid"
    return 0
  fi

  # Keep backward-compatibility for explicit `auto`.
  if [[ "$requested" == "auto" ]]; then
    printf '%s' "auto"
    return 0
  fi

  echo "failed: could not auto-detect thread_id for pane=$pane" >&2
  echo "hint: pass it explicitly: $0 start $pane <thread-id>" >&2
  return 1
}

parse_thread_and_message_args() {
  PARSED_THREAD_ARG=""
  PARSED_MESSAGE_MODE="file"
  PARSED_MESSAGE_VALUE="$DEFAULT_MSG_FILE"
  PARSED_MESSAGE_EXPLICIT="0"

  if [[ $# -gt 0 && "$1" != --* ]]; then
    PARSED_THREAD_ARG="$1"
    shift
  fi

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --message)
        if [[ $# -lt 2 ]]; then
          echo "error: --message requires a value" >&2
          return 1
        fi
        PARSED_MESSAGE_MODE="inline"
        PARSED_MESSAGE_VALUE="$2"
        PARSED_MESSAGE_EXPLICIT="1"
        shift 2
        ;;
      --message-file)
        if [[ $# -lt 2 ]]; then
          echo "error: --message-file requires a path" >&2
          return 1
        fi
        PARSED_MESSAGE_MODE="file"
        PARSED_MESSAGE_VALUE="$2"
        PARSED_MESSAGE_EXPLICIT="1"
        shift 2
        ;;
      *)
        echo "error: unknown option '$1'" >&2
        return 1
        ;;
    esac
  done

  if [[ "$PARSED_MESSAGE_MODE" == "file" && ! -f "$PARSED_MESSAGE_VALUE" ]]; then
    echo "error: message file not found: $PARSED_MESSAGE_VALUE" >&2
    return 1
  fi
}

watcher_rows() {
  local pane_filter="${1:-}"
  ps -ww -eo pid=,args= 2>/dev/null | awk -v root="$PROJECT_CWD" -v pane_filter="$pane_filter" '
    {
      pid=$1
      pane=""; thread=""; state=""; watch=""; cwd=""; msg_file=""; msg_inline=""
      py=0; script=0
      for (i = 2; i <= NF; i++) {
        tok=$i
        if (tok ~ /(^|\/)python([0-9.]+)?$/) py=1
        if (tok ~ /auto_continue_logwatch\.py$/) script=1
        if (tok == "--pane" && i + 1 <= NF) pane=$(i + 1)
        if (tok == "--thread-id" && i + 1 <= NF) thread=$(i + 1)
        if (tok == "--state-file" && i + 1 <= NF) state=$(i + 1)
        if (tok == "--watch-log" && i + 1 <= NF) watch=$(i + 1)
        if (tok == "--cwd" && i + 1 <= NF) cwd=$(i + 1)
        if (tok == "--message-file" && i + 1 <= NF) msg_file=$(i + 1)
        if (tok == "--message" && i + 1 <= NF) msg_inline=$(i + 1)
      }
      if (!py || !script || pane == "") next
      if (cwd != "" && cwd != root) next
      if (pane_filter != "" && pane != pane_filter) next
      print pid "\t" pane "\t" thread "\t" state "\t" watch "\t" msg_file "\t" msg_inline
    }'
}

collect_known_keys() {
  local row pid pane_id thread_id state_path watch_path msg_file msg_inline
  local file base key

  while IFS=$'\t' read -r pid pane_id thread_id state_path watch_path msg_file msg_inline; do
    [[ -n "$pane_id" ]] || continue
    key="$(key_from_pane "$pane_id")"
    [[ -n "$key" ]] && echo "$key"
  done < <(watcher_rows)

  shopt -s nullglob
  for file in \
    "$STATE_DIR"/auto_continue_logwatch.*.state.local.json \
    "$STATE_DIR"/auto_continue_logwatch.*.pid; do
    base="$(basename "$file")"
    key="${base#auto_continue_logwatch.}"
    key="${key%.state.local.json}"
    key="${key%.pid}"
    [[ -n "$key" && "$key" != "$base" ]] && echo "$key"
  done
  shopt -u nullglob
}

read_message_meta_for_key() {
  local key="$1"
  local meta_file
  local mode=""
  local value=""
  meta_file="$(message_meta_file_for_key "$key")"
  [[ -f "$meta_file" ]] || return 1

  while IFS='=' read -r mkey mval; do
    case "$mkey" in
      mode) mode="$mval" ;;
      value) value="$mval" ;;
    esac
  done < "$meta_file"

  if [[ "$mode" == "inline" || "$mode" == "file" ]]; then
    printf '%s\t%s' "$mode" "$value"
    return 0
  fi
  return 1
}

thread_from_running_watcher_for_pane() {
  local pane="$1"
  local row pid pane_id thread_id state_path watch_path msg_file msg_inline
  row="$(watcher_rows "$pane" | head -n 1 || true)"
  [[ -n "$row" ]] || return 1
  IFS=$'\t' read -r pid pane_id thread_id state_path watch_path msg_file msg_inline <<< "$row"
  if is_thread_id "$thread_id"; then
    printf '%s' "${thread_id,,}"
    return 0
  fi
  return 1
}

thread_from_state_file_for_key() {
  local key="$1"
  local state_file tid
  state_file="$(state_file_for_key "$key")"
  [[ -f "$state_file" ]] || return 1

  tid="$(sed -nE "s/.*\"thread_id\"[[:space:]]*:[[:space:]]*\"($THREAD_ID_RE)\".*/\1/p" "$state_file" | head -n 1 | tr '[:upper:]' '[:lower:]')"
  if is_thread_id "$tid"; then
    printf '%s' "$tid"
    return 0
  fi
  return 1
}

watcher_pids_for_pane() {
  local pane="$1"
  watcher_rows "$pane" | awk -F '\t' '{print $1}'
}

is_paused_for_pane() {
  local pane="$1"
  local key pause_file
  key="$(key_from_pane "$pane")"
  pause_file="$(pause_file_for_key "$key")"
  [[ -f "$(global_pause_file)" || -f "$pause_file" ]]
}

is_running_pid_file() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

cmd_run() {
  local pane
  local thread_arg thread_id key
  local message_mode message_value
  local -a message_args=()

  if [[ $# -gt 0 && "$1" != --* ]]; then
    pane="$1"
    shift
  else
    pane="${TMUX_PANE:-}"
  fi
  if [[ -z "$pane" ]]; then
    echo "usage: $0 run <tmux-pane-id> [thread-id|auto] [--message TEXT | --message-file FILE]" >&2
    exit 2
  fi

  parse_thread_and_message_args "$@"
  thread_arg="$PARSED_THREAD_ARG"
  message_mode="$PARSED_MESSAGE_MODE"
  message_value="$PARSED_MESSAGE_VALUE"
  if [[ "$message_mode" == "inline" ]]; then
    message_args=(--message "$message_value")
  else
    message_args=(--message-file "$message_value")
  fi

  thread_id="$(resolve_thread_id "$pane" "$thread_arg")"
  key="$(key_from_pane "$pane")"
  write_message_meta "$key" "$message_mode" "$message_value"
  if [[ ("$thread_arg" == "" || "$thread_arg" == "auto") && "$thread_id" != "auto" ]]; then
    echo "resolved: pane=$pane thread_id=$thread_id"
  fi
  exec python3 "$SCRIPT" \
    --cwd "$PROJECT_CWD" \
    --pane "$pane" \
    --thread-id "$thread_id" \
    "${message_args[@]}" \
    --cooldown-secs 1.0 \
    --state-file "$(state_file_for_key "$key")" \
    --watch-log "$(watch_log_for_key "$key")"
}

cmd_start() {
  local pane
  local thread_arg
  local thread_id key pid_file run_log
  local message_mode message_value
  local -a existing_pids=()
  local -a message_args=()
  local pids_joined
  if [[ $# -gt 0 && "$1" != --* ]]; then
    pane="$1"
    shift
  else
    pane="${TMUX_PANE:-}"
  fi
  if [[ -z "$pane" ]]; then
    echo "usage: $0 start <tmux-pane-id> [thread-id|auto] [--message TEXT | --message-file FILE]" >&2
    exit 2
  fi

  parse_thread_and_message_args "$@"
  thread_arg="$PARSED_THREAD_ARG"
  message_mode="$PARSED_MESSAGE_MODE"
  message_value="$PARSED_MESSAGE_VALUE"
  if [[ "$message_mode" == "inline" ]]; then
    message_args=(--message "$message_value")
  else
    message_args=(--message-file "$message_value")
  fi

  key="$(key_from_pane "$pane")"
  pid_file="$(pid_file_for_key "$key")"
  run_log="$(run_log_for_key "$key")"

  if is_running_pid_file "$pid_file"; then
    echo "already running: pane=$pane pid=$(cat "$pid_file")"
    exit 0
  fi

  mapfile -t existing_pids < <(watcher_pids_for_pane "$pane")
  if (( ${#existing_pids[@]} > 0 )); then
    if (( ${#existing_pids[@]} == 1 )); then
      echo "${existing_pids[0]}" > "$pid_file"
      echo "already running: pane=$pane pid=${existing_pids[0]}"
      return 0
    fi
    pids_joined="$(printf '%s ' "${existing_pids[@]}")"
    pids_joined="${pids_joined% }"
    echo "error: multiple watchers already running for pane=$pane: $pids_joined" >&2
    echo "hint: run '$0 stop $pane' to stop all pane watchers, then start once" >&2
    return 1
  fi

  thread_id="$(resolve_thread_id "$pane" "$thread_arg")"
  write_message_meta "$key" "$message_mode" "$message_value"
  if [[ ("$thread_arg" == "" || "$thread_arg" == "auto") && "$thread_id" != "auto" ]]; then
    echo "resolved: pane=$pane thread_id=$thread_id"
  fi

  rm -f "$pid_file"

  nohup python3 "$SCRIPT" \
    --cwd "$PROJECT_CWD" \
    --pane "$pane" \
    --thread-id "$thread_id" \
    "${message_args[@]}" \
    --cooldown-secs 1.0 \
    --state-file "$(state_file_for_key "$key")" \
    --watch-log "$(watch_log_for_key "$key")" \
    >>"$run_log" 2>&1 &
  local pid=$!

  sleep 0.2
  if kill -0 "$pid" 2>/dev/null; then
    echo "$pid" > "$pid_file"
    echo "started: pid=$pid pane=$pane thread_id=$thread_id"
    return 0
  fi

  rm -f "$pid_file"
  echo "failed: watcher exited immediately (pane=$pane thread_id=$thread_id)" >&2
  tail -n 40 "$run_log" 2>/dev/null || true
  return 1
}

stop_pid_file() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    echo "stopped: pid=$pid"
  else
    echo "stopped: removed stale pid file"
  fi
}

stop_pane_watchers() {
  local pane="$1"
  local key pid_file
  local -a pane_pids=()
  local pid

  key="$(key_from_pane "$pane")"
  pid_file="$(pid_file_for_key "$key")"
  mapfile -t pane_pids < <(watcher_pids_for_pane "$pane")
  if (( ${#pane_pids[@]} == 0 )); then
    [[ -f "$pid_file" ]] && rm -f "$pid_file"
    echo "not running: pane=$pane"
    return 0
  fi

  for pid in "${pane_pids[@]}"; do
    kill "$pid" 2>/dev/null || true
    echo "stopped: pane=$pane pid=$pid"
  done
  rm -f "$pid_file"
  return 0
}

cmd_stop() {
  local pane="${1:-}"

  if [[ -n "$pane" ]]; then
    stop_pane_watchers "$pane"
    return 0
  fi

  local had_any=0
  local -a live_rows=()
  shopt -s nullglob
  local pid_file
  for pid_file in "$STATE_DIR"/auto_continue_logwatch.*.pid; do
    had_any=1
    stop_pid_file "$pid_file"
  done
  shopt -u nullglob

  if [[ -f "$LEGACY_PID_FILE" ]]; then
    had_any=1
    stop_pid_file "$LEGACY_PID_FILE"
  fi

  mapfile -t live_rows < <(watcher_rows)
  for row in "${live_rows[@]}"; do
    local live_pid
    live_pid="${row%%$'\t'*}"
    [[ "$live_pid" =~ ^[0-9]+$ ]] || continue
    had_any=1
    kill "$live_pid" 2>/dev/null || true
    echo "stopped: pid=$live_pid"
  done

  if [[ "$had_any" -eq 0 ]]; then
    echo "not running"
  fi
}

cmd_restart() {
  local pane key
  local thread_arg message_mode message_value message_explicit
  local meta_data meta_mode meta_value
  local -a start_args=()

  if [[ $# -gt 0 && "$1" != --* ]]; then
    pane="$1"
    shift
  else
    pane="${TMUX_PANE:-}"
  fi
  if [[ -z "$pane" ]]; then
    echo "usage: $0 restart <tmux-pane-id> [thread-id|auto] [--message TEXT | --message-file FILE]" >&2
    exit 2
  fi

  parse_thread_and_message_args "$@"
  thread_arg="$PARSED_THREAD_ARG"
  message_mode="$PARSED_MESSAGE_MODE"
  message_value="$PARSED_MESSAGE_VALUE"
  message_explicit="$PARSED_MESSAGE_EXPLICIT"

  key="$(key_from_pane "$pane")"

  if [[ "$message_explicit" != "1" ]]; then
    meta_data="$(read_message_meta_for_key "$key" || true)"
    if [[ -n "$meta_data" ]]; then
      IFS=$'\t' read -r meta_mode meta_value <<< "$meta_data"
      if [[ "$meta_mode" == "inline" || "$meta_mode" == "file" ]]; then
        message_mode="$meta_mode"
        message_value="$meta_value"
      fi
    fi
  fi

  if [[ "$message_mode" == "file" && ! -f "$message_value" ]]; then
    message_value="$DEFAULT_MSG_FILE"
  fi

  if [[ -z "$thread_arg" ]]; then
    thread_arg="$(thread_from_running_watcher_for_pane "$pane" || true)"
    if [[ -z "$thread_arg" ]]; then
      thread_arg="$(thread_from_state_file_for_key "$key" || true)"
    fi
  fi

  stop_pane_watchers "$pane"

  start_args=("$pane")
  if [[ -n "$thread_arg" ]]; then
    start_args+=("$thread_arg")
  fi
  if [[ "$message_mode" == "inline" ]]; then
    start_args+=(--message "$message_value")
  else
    start_args+=(--message-file "$message_value")
  fi

  cmd_start "${start_args[@]}"
}

cmd_pause() {
  local pane="${1:-${TMUX_PANE:-}}"
  local key pause_file
  local -a pane_pids=()
  local pid
  if [[ -z "$pane" ]]; then
    echo "usage: $0 pause <tmux-pane-id>" >&2
    exit 2
  fi
  key="$(key_from_pane "$pane")"
  pause_file="$(pause_file_for_key "$key")"
  touch "$pause_file"
  mapfile -t pane_pids < <(watcher_pids_for_pane "$pane")
  for pid in "${pane_pids[@]}"; do
    kill -STOP "$pid" 2>/dev/null || true
  done
  echo "paused: pane=$pane file=$pause_file"
}

cmd_resume() {
  local pane="${1:-${TMUX_PANE:-}}"
  local key pause_file
  local -a pane_pids=()
  local pid
  if [[ -z "$pane" ]]; then
    echo "usage: $0 resume <tmux-pane-id>" >&2
    exit 2
  fi
  key="$(key_from_pane "$pane")"
  pause_file="$(pause_file_for_key "$key")"
  rm -f "$pause_file"
  mapfile -t pane_pids < <(watcher_pids_for_pane "$pane")
  for pid in "${pane_pids[@]}"; do
    kill -CONT "$pid" 2>/dev/null || true
  done
  echo "resumed: pane=$pane"
  if [[ -f "$(global_pause_file)" ]]; then
    echo "note: global pause file still present at $(global_pause_file)"
  fi
}

cmd_status() {
  local pane="${1:-}"
  local -a rows=()
  local row pid pane_id thread_id state_path watch_path msg_file msg_inline
  local last_line event_summary thread_short state_value
  local key meta_file message_mode message_value message_summary

  if [[ -n "$pane" ]]; then
    mapfile -t rows < <(watcher_rows "$pane")
    echo "Active watchers for pane $pane: ${#rows[@]}"
  else
    mapfile -t rows < <(watcher_rows)
    echo "Active watchers: ${#rows[@]}"
  fi

  printf "%-5s %-7s %-13s %-8s %-20s %s\n" "PANE" "PID" "THREAD_ID" "STATE" "LAST_EVENT" "MESSAGE"
  printf "%-5s %-7s %-13s %-8s %-20s %s\n" "-----" "-------" "-------------" "--------" "--------------------" "-------"

  if (( ${#rows[@]} == 0 )); then
    echo "(none)"
    return 0
  fi

  for row in "${rows[@]}"; do
    IFS=$'\t' read -r pid pane_id thread_id state_path watch_path msg_file msg_inline <<< "$row"

    thread_short="${thread_id:-unknown}"
    if [[ "$thread_short" != "unknown" && ${#thread_short} -gt 8 ]]; then
      thread_short="${thread_short:0:8}..."
    fi

    event_summary="-"
    if [[ -n "$watch_path" ]]; then
      last_line="$(tail -n 1 "$watch_path" 2>/dev/null || true)"
      if [[ -n "$last_line" ]]; then
        event_summary="${last_line#*] }"
        if [[ "$event_summary" =~ ^continue:\ sent\ turn=([0-9]+) ]]; then
          event_summary="continue turn=${BASH_REMATCH[1]}"
        elif [[ "$event_summary" =~ ^watch:\ pane= ]]; then
          event_summary="watch start"
        elif [[ "$event_summary" =~ ^watch:\ auto-rebind ]]; then
          event_summary="watch rebind"
        elif [[ "$event_summary" =~ ^skip:\ pause\ file\ present ]]; then
          event_summary="paused"
        elif [[ "$event_summary" =~ ^error: ]]; then
          event_summary="error"
        elif (( ${#event_summary} > 56 )); then
          event_summary="${event_summary:0:53}..."
        fi
      fi
    fi

    if (( ${#event_summary} > 20 )); then
      event_summary="${event_summary:0:17}..."
    fi

    message_summary="-"
    key="$(key_from_pane "$pane_id")"
    meta_file="$(message_meta_file_for_key "$key")"
    if [[ -f "$meta_file" ]]; then
      message_mode=""
      message_value=""
      while IFS='=' read -r mkey mval; do
        case "$mkey" in
          mode) message_mode="$mval" ;;
          value) message_value="$mval" ;;
        esac
      done < "$meta_file"
      if [[ "$message_mode" == "file" && -n "$message_value" ]]; then
        message_summary="file:$(basename "$message_value")"
      elif [[ "$message_mode" == "inline" && -n "$message_value" ]]; then
        message_summary="msg:$message_value"
      fi
    fi
    if [[ "$message_summary" == "-" ]]; then
      if [[ -n "$msg_file" ]]; then
        message_summary="file:$(basename "$msg_file")"
      elif [[ -n "$msg_inline" ]]; then
        message_summary="msg:$msg_inline"
      fi
    fi
    if (( ${#message_summary} > 44 )); then
      message_summary="${message_summary:0:41}..."
    fi

    state_value="running"
    if is_paused_for_pane "$pane_id"; then
      state_value="paused"
    fi

    printf "%-5s %-7s %-13s %-8s %-20s %s\n" \
      "$pane_id" "$pid" "$thread_short" "$state_value" "$event_summary" "$message_summary"
  done
}

cleanup_stale_pid_files() {
  shopt -s nullglob
  local pid_file
  for pid_file in "$STATE_DIR"/auto_continue_logwatch.*.pid; do
    if ! is_running_pid_file "$pid_file"; then
      rm -f "$pid_file"
    fi
  done
  shopt -u nullglob

  if [[ -f "$LEGACY_PID_FILE" ]]; then
    if ! is_running_pid_file "$LEGACY_PID_FILE"; then
      rm -f "$LEGACY_PID_FILE"
    fi
  fi
}

cleanup_stale_log_files() {
  declare -A keep_logs=()
  declare -A keep_keys=()
  local key
  local log_file

  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    keep_keys["$key"]=1
  done < <(collect_known_keys)

  for key in "${!keep_keys[@]}"; do
    keep_logs["$(watch_log_for_key "$key")"]=1
    keep_logs["$(run_log_for_key "$key")"]=1
  done

  shopt -s nullglob
  for log_file in \
    "$STATE_DIR"/auto_continue_logwatch*.log \
    "$STATE_DIR"/auto_continue_logwatch*.runner.log; do
    if [[ -n "${keep_logs[$log_file]+x}" ]]; then
      continue
    fi
    rm -f "$log_file"
  done
  shopt -u nullglob
}

cleanup_stale_message_meta_files() {
  declare -A keep_meta_files=()
  declare -A keep_keys=()
  local key meta_file

  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    keep_keys["$key"]=1
  done < <(collect_known_keys)

  for key in "${!keep_keys[@]}"; do
    meta_file="$(message_meta_file_for_key "$key")"
    keep_meta_files["$meta_file"]=1
  done

  shopt -s nullglob
  for meta_file in "$STATE_DIR"/auto_continue_logwatch.*.message.local.txt; do
    if [[ -n "${keep_meta_files[$meta_file]+x}" ]]; then
      continue
    fi
    rm -f "$meta_file"
  done
  shopt -u nullglob
}

subcmd="${1:-status}"
shift || true
cleanup_stale_pid_files
cleanup_stale_log_files
cleanup_stale_message_meta_files
case "$subcmd" in
  run) cmd_run "$@" ;;
  start) cmd_start "$@" ;;
  stop) cmd_stop "$@" ;;
  restart) cmd_restart "$@" ;;
  pause) cmd_pause "$@" ;;
  resume) cmd_resume "$@" ;;
  status) cmd_status "$@" ;;
  *)
    echo "usage: $0 {start|stop|restart|pause|resume|status|run} [pane] [thread-id|auto] [--message TEXT | --message-file FILE]" >&2
    exit 2
    ;;
esac
