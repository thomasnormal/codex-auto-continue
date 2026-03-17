#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import acw_config
import acw_probe
import acw_tmux
import codex_team_runtime
import codex_team_state
from acw_codex import CodexLogEvent
from acw_codex import parse_codex_log_event
from acw_codex import thread_from_codex_pid
from acw_codex import tmux_send
from acw_ids import current_tmux_pane
from codex_team_protocol import WORKER_REASONING_EFFORT
from codex_team_protocol import build_reply_message
from codex_team_protocol import build_system_message
from codex_team_protocol import extract_last_agent_block
from codex_team_protocol import initial_launch_command
from codex_team_protocol import leader_initial_prompt
from codex_team_protocol import normalize_injected_message
from codex_team_protocol import parse_routed_messages
from codex_team_protocol import shell_join
from codex_team_protocol import worker_initial_prompt
from codex_team_state import append_log
from codex_team_state import forget_team_state
from codex_team_state import load_team_state
from codex_team_state import log_tail_text
from codex_team_state import pid_is_alive
from codex_team_state import read_json
from codex_team_state import save_team_state
from codex_team_state import short_window_token
from codex_team_state import STATE_DIR
from codex_team_state import TeamAgent
from codex_team_state import TeamAgentPhase
from codex_team_state import TeamState
from codex_team_state import write_json


ENTER_DELAY_SECS = float(acw_config.enter_delay_secs())
KNOWN_SHELL_COMMANDS = acw_config.known_shell_commands()


def build_help_text(prog: str) -> str:
    return f"""Usage:
  {prog} <team-size>
  {prog} status
  {prog} stop
  {prog} down

Commands:
  <team-size>  Create one leader pane plus <team-size> worker panes in the current tmux window,
               launch Codex in each pane, and start a background monitor that routes leader
               <message to="agentN">...</message> tags to the named worker.
  status       Show the codex-team state for the current tmux window.
  stop         Stop the codex-team monitor for the current tmux window and forget its state.
  down         Stop the codex-team monitor, keep the current pane, and close the other panes in the
               current tmux window.

Notes:
  - Run {prog} from a single-pane tmux window.
  - The current pane becomes the leader pane.
  - The default layout keeps the leader on the left and stacks workers on the right.
  - Workers launch with medium reasoning effort by default; the leader keeps the normal Codex config.
  - Worker names are agent1, agent2, ... and worker replies come back to the leader as
    <reply from="agentN">...</reply>.
"""


def print_help(*, file) -> None:
    print(build_help_text(os.path.basename(sys.argv[0]) or "codex-team"), file=file)


def preferred_tmux_socket() -> str:
    return acw_tmux.preferred_tmux_socket()


def tmux_command(*args: str) -> list[str]:
    cmd = ["tmux"]
    sock = preferred_tmux_socket()
    if sock:
        cmd += ["-S", sock]
    cmd += list(args)
    return cmd


def run_tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        tmux_command(*args),
        text=True,
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"tmux {' '.join(args)} failed: {detail}")
    return proc


def run_tmux_text(*args: str) -> str:
    proc = run_tmux(*args, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def current_window_id(pane: str) -> str:
    return run_tmux_text("display-message", "-p", "-t", pane, "#{window_id}")


def list_window_panes(window_id: str) -> list[str]:
    listing = run_tmux_text("list-panes", "-t", window_id, "-F", "#{pane_id}")
    return [line.strip() for line in listing.splitlines() if line.strip()]


def pane_exists(pane: str) -> bool:
    return bool(run_tmux_text("list-panes", "-t", pane, "-F", "#{pane_id}"))


def pane_current_command(pane: str) -> str:
    return run_tmux_text("display-message", "-p", "-t", pane, "#{pane_current_command}").strip().lower()


def pane_shell_pid(pane: str) -> str:
    return run_tmux_text("display-message", "-p", "-t", pane, "#{pane_pid}").strip()


def validate_team_window(leader_pane: str, panes: list[str]) -> None:
    if leader_pane not in panes:
        raise ValueError(f"current pane {leader_pane} is not in the current tmux window")
    if len(panes) != 1:
        raise ValueError(
            f"current window already has {len(panes)} panes; codex-team requires a single-pane window"
        )


def split_team_panes(leader_pane: str, team_size: int) -> list[str]:
    created: list[str] = []
    for _ in range(team_size):
        pane = run_tmux_text("split-window", "-P", "-F", "#{pane_id}", "-t", leader_pane)
        if not pane:
            raise RuntimeError("failed to create tmux pane for codex-team")
        created.append(pane)
    if team_size:
        run_tmux("select-pane", "-t", leader_pane)
        run_tmux("select-layout", "-t", leader_pane, "main-vertical")
    else:
        run_tmux("select-pane", "-t", leader_pane)
    return created


def team_state_path(window_id: str) -> Path:
    return codex_team_state.team_state_path(
        window_id,
        preferred_tmux_socket_fn=preferred_tmux_socket,
    )


def team_log_path(window_id: str) -> Path:
    return codex_team_state.team_log_path(
        window_id,
        preferred_tmux_socket_fn=preferred_tmux_socket,
    )


def team_pid_path(window_id: str) -> Path:
    return codex_team_state.team_pid_path(
        window_id,
        preferred_tmux_socket_fn=preferred_tmux_socket,
    )


def capture_pane_text(pane: str, *, lines: int = 160) -> str:
    return run_tmux_text("capture-pane", "-t", pane, "-p", "-S", f"-{lines}")


def _daemon_argv(state_file: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "__daemon", state_file]


def _runtime_context() -> codex_team_runtime.TeamRuntimeContext:
    return codex_team_runtime.TeamRuntimeContext(
        enter_delay_secs=ENTER_DELAY_SECS,
        known_shell_commands=tuple(KNOWN_SHELL_COMMANDS),
        codex_log_path_fn=acw_config.codex_log_path,
        time_fn=time.time,
        sleep_fn=time.sleep,
        append_log=append_log,
        parse_codex_log_event=parse_codex_log_event,
        capture_pane_text=capture_pane_text,
        tmux_send=tmux_send,
        normalize_injected_message=normalize_injected_message,
        initial_launch_command=initial_launch_command,
        extract_last_agent_block=extract_last_agent_block,
        parse_routed_messages=parse_routed_messages,
        build_reply_message=build_reply_message,
        build_system_message=build_system_message,
        pane_exists=pane_exists,
        pane_current_command=pane_current_command,
        refresh_agent_threads=refresh_agent_threads,
        load_team_state=load_team_state,
        save_team_state=save_team_state,
        pid_is_alive=pid_is_alive,
        log_tail_text=log_tail_text,
        daemon_argv_fn=_daemon_argv,
        daemon_cwd=str(Path(__file__).resolve().parents[1]),
        popen_fn=subprocess.Popen,
    )


def pending_completion_for_thread(
    thread_id: str,
    last_handled_turn: str,
    last_handled_thread: str,
    *,
    log_path: Path,
) -> CodexLogEvent | None:
    return codex_team_runtime.pending_completion_for_thread(
        _runtime_context(),
        thread_id,
        last_handled_turn,
        last_handled_thread,
        log_path=log_path,
    )


def collect_pending_completion_events(state: TeamState) -> list[CodexLogEvent]:
    return codex_team_runtime.collect_pending_completion_events(_runtime_context(), state)


def agent_setup_status(agent: TeamAgent) -> str:
    return codex_team_runtime.agent_setup_status(agent)


def team_is_ready(state: TeamState) -> bool:
    return codex_team_runtime.team_is_ready(state)


def team_members_ready(state: TeamState) -> bool:
    return codex_team_runtime.team_members_ready(state)


def wait_for_daemon_start(pid_file: Path, timeout: float = 10.0) -> int:
    return codex_team_runtime.wait_for_daemon_start(_runtime_context(), pid_file, timeout=timeout)


def wait_for_team_ready(
    state_path: Path,
    *,
    timeout: float = 60.0,
    progress_file=None,
    include_leader: bool = True,
) -> TeamState:
    return codex_team_runtime.wait_for_team_ready(
        _runtime_context(),
        state_path,
        timeout=timeout,
        progress_file=progress_file,
        include_leader=include_leader,
    )


def start_monitor(state: TeamState) -> int:
    return codex_team_runtime.start_monitor(_runtime_context(), state)


def stop_monitor(state: TeamState) -> None:
    codex_team_runtime.stop_monitor(_runtime_context(), state)


def refresh_agent_threads(state: TeamState) -> bool:
    changed = False
    process_snapshot = acw_probe.load_process_snapshot(subprocess.check_output)
    for agent in state.all_agents():
        if agent.thread_id:
            continue
        shell_pid = pane_shell_pid(agent.pane)
        tid, _source, _detail, _had_codex = acw_probe.pane_thread_probe(
            agent.pane,
            shell_pid,
            process_snapshot,
            thread_from_codex_pid,
        )
        if tid:
            agent.thread_id = tid
            changed = True
    return changed


def bootstrap_agents(state: TeamState) -> bool:
    return codex_team_runtime.bootstrap_agents(_runtime_context(), state)


def deliver_pending(agent: TeamAgent, log_path: Path) -> bool:
    return codex_team_runtime.deliver_pending(_runtime_context(), agent, log_path)


def mark_completion_handled(agent: TeamAgent, event: CodexLogEvent) -> None:
    codex_team_runtime.mark_completion_handled(agent, event)


def process_events(
    state: TeamState,
    events: list[CodexLogEvent],
) -> bool:
    return codex_team_runtime.process_events(_runtime_context(), state, events)


def close_other_window_panes(window_id: str, keep_pane: str) -> int:
    closed = 0
    for pane in list_window_panes(window_id):
        if pane == keep_pane:
            continue
        run_tmux("kill-pane", "-t", pane)
        closed += 1
    return closed


def current_window_state_path() -> Path:
    pane = current_tmux_pane()
    if not pane:
        raise RuntimeError("codex-team must be run from inside a tmux pane")
    window_id = current_window_id(pane)
    if not window_id:
        raise RuntimeError("could not resolve the current tmux window")
    return team_state_path(window_id)


def setup_team(team_size: int) -> int:
    if team_size < 0:
        raise RuntimeError("team size must be a non-negative integer")

    leader_pane = current_tmux_pane()
    if not leader_pane:
        raise RuntimeError("codex-team must be run from inside a tmux pane")
    if not preferred_tmux_socket() and not os.environ.get("TMUX", ""):
        raise RuntimeError("codex-team must be run from inside a tmux session")

    window_id = current_window_id(leader_pane)
    if not window_id:
        raise RuntimeError("could not resolve the current tmux window")

    state_path = team_state_path(window_id)
    existing = load_team_state(state_path) if state_path.exists() else None
    if existing and existing.monitor_pid and pid_is_alive(existing.monitor_pid):
        raise RuntimeError(f"codex-team is already running for window {window_id}")

    validate_team_window(leader_pane, list_window_panes(window_id))
    member_panes = split_team_panes(leader_pane, team_size)
    members = [
        TeamAgent(name=f"agent{index}", pane=pane, role="member")
        for index, pane in enumerate(member_panes, start=1)
    ]
    state = TeamState(
        team_id=(
            f"{acw_tmux.tmux_scope_token(preferred_tmux_socket(), preferred_tmux_socket_fn=preferred_tmux_socket)}"
            f":{window_id}"
        ),
        tmux_socket=preferred_tmux_socket(),
        window_id=window_id,
        created_at=time.time(),
        state_file=str(state_path),
        log_file=str(team_log_path(window_id)),
        pid_file=str(team_pid_path(window_id)),
        leader=TeamAgent(name="leader", pane=leader_pane, role="leader"),
        members=members,
    )
    save_team_state(state)
    monitor_pid = start_monitor(state)
    state.monitor_pid = monitor_pid
    save_team_state(state)
    print(f"setup: waiting for {len(state.members)} workers to become ready", flush=True)
    state = wait_for_team_ready(state_path, include_leader=False)
    print(
        "setup: workers ready; leader will finish booting after this command returns",
        flush=True,
    )

    print(f"created codex team in window {window_id}")
    print(f"state: {state.state_file}")
    print(f"monitor: {monitor_pid}")
    print(f"leader: {state.leader.name} {state.leader.pane}")
    if state.members:
        for member in state.members:
            print(f"team: {member.name} {member.pane}")
    else:
        print("team: (none)")
    return 0


def cmd_status() -> int:
    state_path = current_window_state_path()
    if not state_path.exists():
        raise RuntimeError("no codex-team state for the current tmux window")
    state = load_team_state(state_path)
    print(f"state: {state.state_file}")
    print(f"window: {state.window_id}")
    print(f"monitor: {state.monitor_pid}")
    if state.leader is not None:
        print(
            f"leader: {state.leader.name} pane={state.leader.pane} "
            f"thread={state.leader.thread_id or '-'} ready={state.leader.ready}"
        )
    for member in state.members:
        print(
            f"team: {member.name} pane={member.pane} "
            f"thread={member.thread_id or '-'} ready={member.ready}"
        )
    return 0


def cmd_stop() -> int:
    state_path = current_window_state_path()
    if not state_path.exists():
        raise RuntimeError("no codex-team state for the current tmux window")
    state = load_team_state(state_path)
    stop_monitor(state)
    forget_team_state(state)
    print(f"stopped codex-team for window {state.window_id}")
    return 0


def cmd_down() -> int:
    keep_pane = current_tmux_pane()
    if not keep_pane:
        raise RuntimeError("codex-team down must be run from inside the pane you want to keep")
    state_path = current_window_state_path()
    if not state_path.exists():
        raise RuntimeError("no codex-team state for the current tmux window")
    state = load_team_state(state_path)
    stop_monitor(state)
    closed = close_other_window_panes(state.window_id, keep_pane)
    forget_team_state(state)
    print(f"stopped codex-team for window {state.window_id}")
    print(f"kept pane: {keep_pane}")
    print(f"closed panes: {closed}")
    return 0


def monitor_loop(state_file: Path) -> int:
    return codex_team_runtime.monitor_loop(_runtime_context(), state_file)


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print_help(file=sys.stdout)
        return 0

    if args[0] == "__daemon":
        if len(args) != 2:
            print("usage: codex_team.py __daemon <state-file>", file=sys.stderr)
            return 2
        return monitor_loop(Path(args[1]))

    if args[0] == "status":
        return cmd_status()
    if args[0] == "stop":
        return cmd_stop()
    if args[0] == "down":
        return cmd_down()

    if len(args) != 1 or not re.fullmatch(r"\d+", args[0]):
        print_help(file=sys.stderr)
        return 2
    return setup_team(int(args[0]))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
