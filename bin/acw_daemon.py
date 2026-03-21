from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass
from pathlib import Path

import acw_loop
import acw_probe


@dataclass
class CodexLogTail:
    log_path: Path
    handle: io.TextIOWrapper | None = None

    def close(self) -> None:
        if self.handle is None:
            return
        self.handle.close()
        self.handle = None


@dataclass
class SessionRuntime:
    thread_id: str
    pane: str
    message: str
    tmux_socket: str
    generation: int
    runtime_id: str
    state_file: str
    watch_log: str
    cwd: str
    paused: bool = False
    runtime_status: str = "starting"
    runtime_detail: str = ""
    last_handled_turn: str = ""
    last_handled_thread: str = ""
    continue_count: int = 0
    last_send_time: float = 0.0
    health: str = "ok"
    health_detail: str = ""
    last_health_check: float = 0.0
    watcher_start: float = 0.0
    interrupt_skip_armed: bool = False
    pending_startup_event: object | None = None
    loop_signature: str = ""
    loop_score: int = 0
    proven_branch: str = ""
    last_location_check: float = 0.0


def continue_count_for_watch_log(watch_log: str) -> int:
    if not watch_log:
        return 0
    path = Path(watch_log)
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return sum(1 for line in handle if "continue: sent" in line)
    except OSError:
        return 0


def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def daemon_runtime_payload(runtime: SessionRuntime, *, now_ts) -> dict:
    return {
        "runtime_id": runtime.runtime_id,
        "pid": runtime.runtime_id,
        "daemon_pid": str(os.getpid()),
        "pane": runtime.pane,
        "state": runtime.state_file,
        "watch": runtime.watch_log,
        "cwd": runtime.cwd,
        "msg_inline": runtime.message,
        "paused": runtime.paused,
        "runtime_status": "paused" if runtime.paused else (runtime.runtime_status or "running"),
        "runtime_detail": runtime.runtime_detail,
        "generation": runtime.generation,
        "worker_label": runtime.runtime_id,
        "heartbeat_ts": now_ts(),
    }


def drain_codex_log_events(
    tail: CodexLogTail,
    *,
    parse_interrupt,
    parse_event,
    event_cls,
) -> list[object]:
    events: list[object] = []
    if tail.handle is None:
        if not tail.log_path.exists():
            return events
        tail.log_path.parent.mkdir(parents=True, exist_ok=True)
        tail.handle = tail.log_path.open("r", encoding="utf-8", errors="ignore")
        tail.handle.seek(0, os.SEEK_END)
        return events

    while True:
        line = tail.handle.readline()
        if not line:
            break
        interrupt_thread = parse_interrupt(line)
        if interrupt_thread:
            events.append(event_cls("interrupt", interrupt_thread, detail="Conversation interrupted"))
            continue
        parsed = parse_event(line)
        if parsed:
            events.append(event_cls("completion", *parsed))

    try:
        if tail.handle.tell() > tail.log_path.stat().st_size:
            tail.close()
    except FileNotFoundError:
        tail.close()
    return events


def daemon_session_runtime(
    session,
    *,
    codex_log_path: Path,
    state_file: str,
    watch_log: str,
    project_cwd: str,
    message_text,
    read_session_state,
    write_session_state,
    append_log,
    check_pending,
    event_cls,
    next_runtime_id,
) -> SessionRuntime:
    state = read_session_state(session.thread_id)
    continue_count = _coerce_int(state.get("continue_count", 0))
    if continue_count <= 0:
        continue_count = continue_count_for_watch_log(watch_log)
    last_proven_workdir = str(state.get("last_proven_workdir", "")).strip()
    last_proven_branch = str(state.get("last_proven_branch", "")).strip()
    runtime = SessionRuntime(
        thread_id=session.thread_id,
        pane=session.pane,
        message=message_text(session.message),
        tmux_socket=session.tmux_socket,
        generation=session.generation,
        runtime_id=next_runtime_id(),
        state_file=state_file,
        watch_log=watch_log,
        cwd=last_proven_workdir or project_cwd,
        paused=session.paused,
        runtime_status="paused" if session.paused else "running",
        runtime_detail=session.health_detail if session.paused else "",
        last_handled_turn=str(state.get("last_handled_turn", state.get("last_sent_turn", ""))),
        last_handled_thread=str(
            state.get("last_handled_thread", state.get("last_sent_thread", state.get("thread_id", "")))
        ),
        continue_count=continue_count,
        watcher_start=time.time(),
        loop_signature=str(state.get("loop_signature", "")).strip(),
        loop_score=_coerce_int(state.get("loop_score", 0)),
        proven_branch=last_proven_branch,
    )
    write_session_state(
        session.thread_id,
        {
            "thread_id": session.thread_id,
            "pane": session.pane,
            "name": session.name,
            "message": runtime.message,
            "paused": session.paused,
            "generation": session.generation,
            "tmux_socket": session.tmux_socket,
            "continue_count": runtime.continue_count,
            "loop_signature": runtime.loop_signature,
            "loop_score": runtime.loop_score,
        },
    )
    append_log(Path(runtime.watch_log), f"watch: pane={runtime.pane} thread_id={runtime.thread_id}")
    pending = check_pending(
        codex_log_path,
        runtime.thread_id,
        runtime.last_handled_turn,
        runtime.last_handled_thread,
        Path(runtime.watch_log),
    )
    if pending:
        runtime.pending_startup_event = event_cls("completion", *pending)
    return runtime


def daemon_mark_handled(runtime: SessionRuntime, turn_id: str, thread_id: str, *, write_session_state) -> None:
    runtime.last_handled_turn = turn_id
    runtime.last_handled_thread = thread_id
    write_session_state(
        runtime.thread_id,
        {
            "last_handled_turn": turn_id,
            "last_handled_thread": thread_id,
        },
    )


def daemon_skip_interrupted_turn(
    runtime: SessionRuntime,
    turn_id: str,
    thread_id: str,
    reason: str,
    *,
    write_session_state,
    append_log,
) -> None:
    runtime.interrupt_skip_armed = False
    daemon_mark_handled(runtime, turn_id, thread_id, write_session_state=write_session_state)
    append_log(Path(runtime.watch_log), f"skip: interrupted turn={turn_id} thread={thread_id} reason={reason}")


def daemon_pause_session(
    runtime: SessionRuntime,
    reason: str,
    *,
    write_session_state,
    append_log,
    now_ts,
) -> None:
    runtime.paused = True
    runtime.runtime_status = "paused"
    runtime.runtime_detail = reason
    runtime.health_detail = f"auto-paused: {reason}"
    write_session_state(
        runtime.thread_id,
        {
            "paused": True,
            "health_detail": runtime.health_detail,
            "health_ts": now_ts(),
        },
    )
    append_log(Path(runtime.watch_log), f"pause: auto-pausing watcher ({reason})")


def daemon_continue_session(
    runtime: SessionRuntime,
    *,
    check_pane_for_interrupt,
    check_pane_for_errors,
    tmux_send,
    now_ts,
    append_log,
    write_session_state,
    enter_delay_secs: str,
    time_module,
) -> tuple[bool, str]:
    if runtime.paused:
        runtime.runtime_status = "paused"
        return False, "managed session is paused"

    pane_interrupt = check_pane_for_interrupt(runtime.pane)
    if pane_interrupt:
        return False, pane_interrupt

    pane_error = check_pane_for_errors(runtime.pane)
    if pane_error:
        daemon_pause_session(
            runtime,
            pane_error,
            write_session_state=write_session_state,
            append_log=append_log,
            now_ts=now_ts,
        )
        return False, pane_error

    if time_module.time() - runtime.last_send_time < 1.0:
        return False, "cooldown active"

    send_result = tmux_send(
        runtime.pane,
        runtime.message,
        max(0.0, float(enter_delay_secs)),
        interrupt_checker=lambda: check_pane_for_interrupt(runtime.pane),
    )
    if send_result.status != "ok":
        detail = send_result.detail or "tmux send failed"
        if send_result.status != "interrupted":
            append_log(
                Path(runtime.watch_log),
                f"error: manual continue failed thread={runtime.thread_id} detail={detail}",
            )
        return False, detail

    runtime.last_send_time = time_module.time()
    runtime.continue_count += 1
    write_session_state(
        runtime.thread_id,
        {
            "thread_id": runtime.thread_id,
            "last_continue_at": now_ts(),
            "continue_count": runtime.continue_count,
            "health": runtime.health,
            "health_detail": runtime.health_detail,
        },
    )
    append_log(Path(runtime.watch_log), f"continue: sent manual thread={runtime.thread_id}")
    return True, ""


def daemon_check_loop(
    runtime: SessionRuntime,
    *,
    capture_pane_text,
    write_session_state,
    append_log,
    now_ts,
) -> bool:
    pane_text = capture_pane_text(runtime.pane)
    observation = acw_loop.loop_observation_from_pane_text(pane_text, runtime.message)
    state = acw_loop.advance_loop_state(
        runtime.loop_signature,
        runtime.loop_score,
        observation,
    )
    if state.signature == runtime.loop_signature and state.score == runtime.loop_score:
        return False

    runtime.loop_signature = state.signature
    runtime.loop_score = state.score
    write_session_state(
        runtime.thread_id,
        {
            "loop_signature": runtime.loop_signature,
            "loop_score": runtime.loop_score,
        },
    )
    if not state.should_pause:
        return False

    append_log(
        Path(runtime.watch_log),
        (
            f"loop: detected repeated docs-only checkpoint turns score={runtime.loop_score}"
            + (f" detail={observation.detail}" if observation.detail else "")
        ),
    )
    daemon_pause_session(
        runtime,
        state.reason,
        write_session_state=write_session_state,
        append_log=append_log,
        now_ts=now_ts,
    )
    return True


def daemon_refresh_session_location(
    runtime: SessionRuntime,
    *,
    capture_pane_text,
    resolve_repo_root_fn,
    branch_for_cwd,
    home_dir: str,
    write_session_state,
) -> bool:
    pane_text = capture_pane_text(runtime.pane)
    workdir, source = acw_probe.workdir_proof_from_pane_text(
        pane_text,
        resolve_repo_root_fn=resolve_repo_root_fn,
        home_dir=home_dir,
    )
    if not workdir or source not in {"explicit", "git_c"}:
        return False
    branch = branch_for_cwd(workdir)
    if branch == "-":
        branch = ""
    if workdir == runtime.cwd and branch == runtime.proven_branch:
        return False
    runtime.cwd = workdir
    runtime.proven_branch = branch
    write_session_state(
        runtime.thread_id,
        {
            "last_proven_workdir": workdir,
            "last_proven_branch": branch,
        },
    )
    return True


def daemon_process_session_events(
    runtime: SessionRuntime,
    events: list[object],
    *,
    check_pane_for_interrupt,
    check_pane_for_errors,
    capture_pane_text,
    tmux_send,
    compute_watch_health,
    now_ts,
    append_log,
    write_session_state,
    send_delay_secs: str,
    enter_delay_secs: str,
    time_module,
) -> None:
    if runtime.paused:
        runtime.runtime_status = "paused"
        return

    runtime.runtime_status = "running"
    runtime.runtime_detail = ""
    watch_log = Path(runtime.watch_log)

    for event in events:
        if runtime.paused:
            break
        if event.thread_id != runtime.thread_id:
            continue

        if event.kind == "interrupt":
            if not runtime.interrupt_skip_armed:
                runtime.interrupt_skip_armed = True
                append_log(watch_log, f"skip: armed after interrupt thread={event.thread_id}")
            continue

        if event.detail != "false":
            continue
        if event.turn_id == runtime.last_handled_turn and event.thread_id == runtime.last_handled_thread:
            continue

        if runtime.interrupt_skip_armed:
            daemon_skip_interrupted_turn(
                runtime,
                event.turn_id,
                event.thread_id,
                "Conversation interrupted",
                write_session_state=write_session_state,
                append_log=append_log,
            )
            continue

        pane_interrupt = check_pane_for_interrupt(runtime.pane)
        if pane_interrupt:
            daemon_skip_interrupted_turn(
                runtime,
                event.turn_id,
                event.thread_id,
                pane_interrupt,
                write_session_state=write_session_state,
                append_log=append_log,
            )
            continue

        pane_error = check_pane_for_errors(runtime.pane)
        if pane_error:
            daemon_pause_session(
                runtime,
                pane_error,
                write_session_state=write_session_state,
                append_log=append_log,
                now_ts=now_ts,
            )
            continue

        if daemon_check_loop(
            runtime,
            capture_pane_text=capture_pane_text,
            write_session_state=write_session_state,
            append_log=append_log,
            now_ts=now_ts,
        ):
            daemon_mark_handled(runtime, event.turn_id, event.thread_id, write_session_state=write_session_state)
            continue

        if time_module.time() - runtime.last_send_time < 1.0:
            append_log(watch_log, f"skip: cooldown turn={event.turn_id}")
            continue

        if float(send_delay_secs) > 0.0:
            time_module.sleep(float(send_delay_secs))

        pane_interrupt = check_pane_for_interrupt(runtime.pane)
        if pane_interrupt:
            daemon_skip_interrupted_turn(
                runtime,
                event.turn_id,
                event.thread_id,
                pane_interrupt,
                write_session_state=write_session_state,
                append_log=append_log,
            )
            continue

        pane_error = check_pane_for_errors(runtime.pane)
        if pane_error:
            daemon_pause_session(
                runtime,
                pane_error,
                write_session_state=write_session_state,
                append_log=append_log,
                now_ts=now_ts,
            )
            continue

        send_result = tmux_send(
            runtime.pane,
            runtime.message,
            max(0.0, float(enter_delay_secs)),
            interrupt_checker=lambda: check_pane_for_interrupt(runtime.pane),
        )
        if send_result.status == "ok":
            runtime.last_send_time = time_module.time()
            runtime.continue_count += 1
            daemon_mark_handled(runtime, event.turn_id, event.thread_id, write_session_state=write_session_state)
            write_session_state(
                runtime.thread_id,
                {
                    "thread_id": runtime.thread_id,
                    "last_continue_at": now_ts(),
                    "continue_count": runtime.continue_count,
                    "health": runtime.health,
                    "health_detail": runtime.health_detail,
                },
            )
            append_log(watch_log, f"continue: sent turn={event.turn_id} thread={event.thread_id}")
            continue
        if send_result.status == "interrupted":
            daemon_skip_interrupted_turn(
                runtime,
                event.turn_id,
                event.thread_id,
                send_result.detail,
                write_session_state=write_session_state,
                append_log=append_log,
            )
            continue
        if send_result.detail:
            append_log(
                watch_log,
                f"error: tmux send failed turn={event.turn_id} thread={event.thread_id} detail={send_result.detail}",
            )
        else:
            append_log(watch_log, f"error: tmux send failed turn={event.turn_id} thread={event.thread_id}")


def daemon_refresh_session_health(
    runtime: SessionRuntime,
    *,
    codex_log_exists: bool,
    now: float,
    compute_watch_health,
    append_log,
    write_session_state,
    now_ts,
) -> None:
    new_health, new_detail = compute_watch_health(
        watched_thread=runtime.thread_id,
        watcher_start=runtime.watcher_start,
        now=now,
        codex_log_exists=codex_log_exists,
    )
    if new_health != runtime.health or new_detail != runtime.health_detail:
        append_log(Path(runtime.watch_log), f"health: {new_health} - {new_detail}" if new_detail else f"health: {new_health}")
    runtime.health = new_health
    runtime.health_detail = new_detail
    write_session_state(
        runtime.thread_id,
        {
            "health": runtime.health,
            "health_detail": runtime.health_detail,
            "health_ts": now_ts(),
        },
    )


def daemon_runtime_events(runtime: SessionRuntime, events_by_thread: dict[str, list[object]]) -> list[object]:
    if runtime.paused:
        return []
    combined: list[object] = []
    pending = runtime.pending_startup_event
    if pending is not None:
        combined.append(pending)
        runtime.pending_startup_event = None
    for event in events_by_thread.get(runtime.thread_id, ()):
        if (
            pending is not None
            and event.kind == "completion"
            and event.thread_id == pending.thread_id
            and event.turn_id == pending.turn_id
        ):
            continue
        combined.append(event)
    return combined
