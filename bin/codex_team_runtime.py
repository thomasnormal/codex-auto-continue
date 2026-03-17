from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from typing import Iterable
from typing import Optional
from typing import TextIO

from acw_codex import CodexLogEvent
from codex_team_state import TeamAgent
from codex_team_state import TeamAgentPhase
from codex_team_state import TeamState


@dataclass(frozen=True)
class TeamRuntimeContext:
    enter_delay_secs: float
    known_shell_commands: tuple[str, ...]
    codex_log_path_fn: Callable[[], str]
    time_fn: Callable[[], float]
    sleep_fn: Callable[[float], None]
    append_log: Callable[[Path, str], None]
    parse_codex_log_event: Callable[[str], Optional[tuple[str, str, str]]]
    capture_pane_text: Callable[[str], str]
    tmux_send: Callable[[str, str, float], object]
    normalize_injected_message: Callable[[str], str]
    initial_launch_command: Callable[[TeamAgent, Iterable[str]], str]
    extract_last_agent_block: Callable[[str], str]
    parse_routed_messages: Callable[[str], list[tuple[str, str]]]
    build_reply_message: Callable[[str, str], str]
    build_system_message: Callable[[str], str]
    pane_exists: Callable[[str], bool]
    pane_current_command: Callable[[str], str]
    refresh_agent_threads: Callable[[TeamState], bool]
    load_team_state: Callable[[Path], TeamState]
    save_team_state: Callable[[TeamState], None]
    pid_is_alive: Callable[[int], bool]
    log_tail_text: Callable[[Path], str]
    daemon_argv_fn: Callable[[str], list[str]]
    daemon_cwd: str
    popen_fn: Callable[..., subprocess.Popen]


def pending_completion_for_thread(
    context: TeamRuntimeContext,
    thread_id: str,
    last_handled_turn: str,
    last_handled_thread: str,
    *,
    log_path: Path,
) -> CodexLogEvent | None:
    if not thread_id:
        return None
    try:
        file_size = log_path.stat().st_size
    except OSError:
        return None
    if file_size <= 0:
        return None

    tail_bytes = min(file_size, 2 * 1024 * 1024)
    try:
        with log_path.open("rb") as fh:
            if tail_bytes < file_size:
                fh.seek(-tail_bytes, os.SEEK_END)
            tail = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return None

    for line in reversed(tail.splitlines()):
        event = context.parse_codex_log_event(line)
        if not event:
            continue
        event_thread_id, turn_id, detail = event
        if event_thread_id != thread_id:
            continue
        if detail != "false":
            return None
        if turn_id == last_handled_turn and thread_id == last_handled_thread:
            return None
        return CodexLogEvent("completion", event_thread_id, turn_id, detail)
    return None


def collect_pending_completion_events(context: TeamRuntimeContext, state: TeamState) -> list[CodexLogEvent]:
    log_path = Path(context.codex_log_path_fn())
    events: list[CodexLogEvent] = []
    for agent in state.all_agents():
        if not agent.thread_id:
            continue
        event = pending_completion_for_thread(
            context,
            agent.thread_id,
            agent.last_handled_turn,
            agent.last_handled_thread,
            log_path=log_path,
        )
        if event is not None:
            events.append(event)
    return events


def agent_setup_status(agent: TeamAgent) -> str:
    if agent.phase == TeamAgentPhase.READY:
        return "ready"
    if agent.phase == TeamAgentPhase.STARTING:
        if agent.thread_id:
            return "waiting for initial reply"
        return "waiting for codex process"
    if agent.phase == TeamAgentPhase.BUSY:
        return "busy"
    if agent.thread_id:
        return f"thread detected ({agent.thread_id[:8]}…)"
    return "starting codex"


def team_is_ready(state: TeamState) -> bool:
    agents = state.all_agents()
    return bool(agents) and all(agent.initial_prompt_sent and agent.ready for agent in agents)


def team_members_ready(state: TeamState) -> bool:
    return all(agent.initial_prompt_sent and agent.ready for agent in state.members)


def wait_for_team_ready(
    context: TeamRuntimeContext,
    state_path: Path,
    *,
    timeout: float = 60.0,
    progress_file: TextIO | None = None,
    include_leader: bool = True,
) -> TeamState:
    import sys

    if progress_file is None:
        progress_file = sys.stdout
    seen_status: dict[str, str] = {}
    deadline = context.time_fn() + timeout
    while context.time_fn() < deadline:
        if not state_path.exists():
            context.sleep_fn(0.1)
            continue
        state = context.load_team_state(state_path)
        agents = state.all_agents() if include_leader else state.members
        for agent in agents:
            status = agent_setup_status(agent)
            if seen_status.get(agent.name) != status:
                print(f"setup: {agent.name} {status}", file=progress_file, flush=True)
                seen_status[agent.name] = status
        if team_is_ready(state) if include_leader else team_members_ready(state):
            return state
        if state.monitor_pid and not context.pid_is_alive(state.monitor_pid):
            detail = context.log_tail_text(Path(state.log_file))
            if detail:
                raise RuntimeError(f"codex-team monitor exited during setup:\n{detail}")
            raise RuntimeError("codex-team monitor exited during setup")
        context.sleep_fn(0.2)
    detail = ""
    if state_path.exists():
        state = context.load_team_state(state_path)
        agents = state.all_agents() if include_leader else state.members
        detail = ", ".join(f"{agent.name}={agent_setup_status(agent)}" for agent in agents)
    raise RuntimeError(f"timed out waiting for codex-team setup to finish ({detail})")


def wait_for_daemon_start(
    context: TeamRuntimeContext,
    pid_file: Path,
    timeout: float = 10.0,
) -> int:
    deadline = context.time_fn() + timeout
    while context.time_fn() < deadline:
        try:
            text = pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text.isdigit() and context.pid_is_alive(int(text)):
            return int(text)
        context.sleep_fn(0.1)
    raise RuntimeError("timed out waiting for codex-team monitor startup")


def start_monitor(context: TeamRuntimeContext, state: TeamState) -> int:
    log_path = Path(state.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_fh:
        proc = context.popen_fn(
            context.daemon_argv_fn(state.state_file),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            cwd=context.daemon_cwd,
            env=os.environ.copy(),
            start_new_session=True,
        )
    state.monitor_pid = proc.pid
    context.save_team_state(state)
    return wait_for_daemon_start(context, Path(state.pid_file))


def stop_monitor(context: TeamRuntimeContext, state: TeamState) -> None:
    pid = state.monitor_pid
    if pid and context.pid_is_alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = context.time_fn() + 10.0
        while context.time_fn() < deadline:
            if not context.pid_is_alive(pid):
                break
            context.sleep_fn(0.1)
    Path(state.pid_file).unlink(missing_ok=True)
    state.monitor_pid = 0


def refresh_agent_runtime(context: TeamRuntimeContext, state: TeamState) -> bool:
    return context.refresh_agent_threads(state)


def bootstrap_agents(context: TeamRuntimeContext, state: TeamState) -> bool:
    log_path = Path(state.log_file)
    changed = False
    if refresh_agent_runtime(context, state):
        changed = True
    member_names = [member.name for member in state.members]
    for agent in state.all_agents():
        if not context.pane_exists(agent.pane):
            raise RuntimeError(f"pane disappeared: {agent.pane}")
        if not agent.thread_id:
            current_cmd = context.pane_current_command(agent.pane)
            if current_cmd in context.known_shell_commands:
                now = context.time_fn()
                if not agent.launch_requested or (now - agent.launch_requested_at) >= 5.0:
                    result = context.tmux_send(
                        agent.pane,
                        context.initial_launch_command(agent, member_names),
                        context.enter_delay_secs,
                    )
                    if result.status == "ok":
                        agent.phase = TeamAgentPhase.STARTING
                        agent.launch_requested_at = now
                        context.append_log(
                            log_path,
                            f"launch: started codex in {agent.name} pane={agent.pane} with initial prompt",
                        )
                        changed = True
                    else:
                        context.append_log(
                            log_path,
                            f"launch: failed to start codex in {agent.name} pane={agent.pane}: {result.detail}",
                        )
    return changed


def deliver_pending(context: TeamRuntimeContext, agent: TeamAgent, log_path: Path) -> bool:
    if not agent.pending_messages or not agent.initial_prompt_sent:
        return False
    message = agent.pending_messages.pop(0)
    result = context.tmux_send(
        agent.pane,
        context.normalize_injected_message(message),
        context.enter_delay_secs,
    )
    if result.status != "ok":
        agent.pending_messages.insert(0, message)
        context.append_log(
            log_path,
            f"route: failed to send queued message to {agent.name} pane={agent.pane}: {result.detail}",
        )
        return False
    agent.phase = TeamAgentPhase.BUSY
    context.append_log(log_path, f"route: queued message to {agent.name} pane={agent.pane}")
    return True


def mark_completion_handled(agent: TeamAgent, event: CodexLogEvent) -> None:
    agent.last_handled_turn = event.turn_id
    agent.last_handled_thread = event.thread_id


def process_events(
    context: TeamRuntimeContext,
    state: TeamState,
    events: list[CodexLogEvent],
) -> bool:
    if state.leader is None:
        return False
    changed = False
    log_path = Path(state.log_file)
    by_thread = {agent.thread_id: agent for agent in state.all_agents() if agent.thread_id}
    for event in events:
        if event.kind != "completion" or event.detail != "false":
            continue
        agent = by_thread.get(event.thread_id)
        if agent is None:
            continue
        block = context.extract_last_agent_block(context.capture_pane_text(agent.pane))
        if agent.phase == TeamAgentPhase.STARTING:
            agent.phase = TeamAgentPhase.READY
            mark_completion_handled(agent, event)
            changed = True
            context.append_log(log_path, f"ready: {agent.name} initialized on thread={event.thread_id}")
            continue

        agent.phase = TeamAgentPhase.READY
        mark_completion_handled(agent, event)
        if agent.role == "leader":
            routed = context.parse_routed_messages(block)
            if not routed:
                context.append_log(log_path, f"leader: no routed messages on thread={event.thread_id}")
                changed = True
                continue
            for receiver, message in routed:
                member = state.member_named(receiver)
                if member is None:
                    state.leader.pending_messages.append(
                        context.build_system_message(
                            f"unknown receiver '{receiver}'. Valid worker names: "
                            + ", ".join(member.name for member in state.members)
                        )
                    )
                    context.append_log(log_path, f"leader: unknown receiver {receiver}")
                    changed = True
                    continue
                member.pending_messages.append(message)
                context.append_log(log_path, f"leader: queued message for {receiver}")
                changed = True
        else:
            reply = context.build_reply_message(agent.name, block)
            state.leader.pending_messages.append(reply)
            context.append_log(log_path, f"worker: queued reply from {agent.name} to leader")
            changed = True
    return changed


def monitor_loop(context: TeamRuntimeContext, state_file: Path) -> int:
    state = context.load_team_state(state_file)
    if state.tmux_socket:
        os.environ["AUTO_CONTINUE_TMUX_SOCKET"] = state.tmux_socket

    stop_requested = False
    log_path = Path(state.log_file)
    pid_path = Path(state.pid_file)

    def _stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    state.monitor_pid = os.getpid()
    context.save_team_state(state)
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    context.append_log(log_path, f"monitor: started for window={state.window_id}")

    try:
        while not stop_requested:
            if state.leader is None or not context.pane_exists(state.leader.pane):
                context.append_log(log_path, "monitor: leader pane disappeared; exiting")
                return 0
            changed = bootstrap_agents(context, state)
            events = collect_pending_completion_events(context, state)
            if events and process_events(context, state, events):
                changed = True
            if state.leader is not None and deliver_pending(context, state.leader, log_path):
                changed = True
            for member in state.members:
                if deliver_pending(context, member, log_path):
                    changed = True
            if changed:
                context.save_team_state(state)
            context.sleep_fn(0.2)
    finally:
        pid_path.unlink(missing_ok=True)
        state.monitor_pid = 0
        try:
            context.save_team_state(state)
        except OSError:
            pass
        context.append_log(log_path, f"monitor: stopped for window={state.window_id}")
    return 0
