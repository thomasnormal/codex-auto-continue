#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Iterable
from typing import Optional

import acw_config
import acw_probe
import acw_tmux
from acw_ids import current_tmux_pane
from auto_continue_logwatch import CodexLogEvent
from auto_continue_logwatch import parse_codex_log_event
from auto_continue_logwatch import thread_from_codex_pid
from auto_continue_logwatch import tmux_send


MESSAGE_RE = re.compile(r"<message\s+to=\"([^\"]+)\">(.*?)</message>", re.IGNORECASE | re.DOTALL)
PROMPT_LINE_RE = re.compile(r"^\s*›(?:\s|$)")

STATE_DIR = acw_config.state_dir_path()
ENTER_DELAY_SECS = float(acw_config.enter_delay_secs())
KNOWN_SHELL_COMMANDS = acw_config.known_shell_commands()
WORKER_REASONING_EFFORT = "medium"


@dataclass
class TeamAgent:
    name: str
    pane: str
    role: str
    thread_id: str = ""
    ready: bool = False
    busy: bool = False
    launch_requested: bool = False
    launch_requested_at: float = 0.0
    initial_prompt_sent: bool = False
    ignore_next_completion: bool = False
    pending_messages: list[str] = field(default_factory=list)
    last_handled_turn: str = ""
    last_handled_thread: str = ""


@dataclass
class TeamState:
    team_id: str
    tmux_socket: str
    window_id: str
    created_at: float
    state_file: str
    log_file: str
    pid_file: str
    monitor_pid: int = 0
    leader: TeamAgent | None = None
    members: list[TeamAgent] = field(default_factory=list)

    def all_agents(self) -> list[TeamAgent]:
        leader = [self.leader] if self.leader is not None else []
        return leader + list(self.members)

    def member_named(self, name: str) -> TeamAgent | None:
        for member in self.members:
            if member.name == name:
                return member
        return None


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


def short_window_token(window_id: str) -> str:
    return acw_tmux.sanitize_key(window_id) or "window"


def team_state_path(window_id: str) -> Path:
    scope = acw_tmux.tmux_scope_token(preferred_tmux_socket(), preferred_tmux_socket_fn=preferred_tmux_socket)
    return STATE_DIR / f"codex_team.{scope}.{short_window_token(window_id)}.json"


def team_log_path(window_id: str) -> Path:
    scope = acw_tmux.tmux_scope_token(preferred_tmux_socket(), preferred_tmux_socket_fn=preferred_tmux_socket)
    return STATE_DIR / f"codex_team.{scope}.{short_window_token(window_id)}.log"


def team_pid_path(window_id: str) -> Path:
    scope = acw_tmux.tmux_scope_token(preferred_tmux_socket(), preferred_tmux_socket_fn=preferred_tmux_socket)
    return STATE_DIR / f"codex_team.{scope}.{short_window_token(window_id)}.pid"


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def append_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}\n")


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def load_team_state(path: Path) -> TeamState:
    raw = read_json(path)
    leader_raw = raw.get("leader") or {}
    members_raw = raw.get("members") or []
    state = TeamState(
        team_id=str(raw.get("team_id", "")),
        tmux_socket=str(raw.get("tmux_socket", "")).strip(),
        window_id=str(raw.get("window_id", "")).strip(),
        created_at=float(raw.get("created_at", 0.0) or 0.0),
        state_file=str(raw.get("state_file", path)).strip() or str(path),
        log_file=str(raw.get("log_file", "")).strip(),
        pid_file=str(raw.get("pid_file", "")).strip(),
        monitor_pid=int(raw.get("monitor_pid", 0) or 0),
        leader=TeamAgent(**leader_raw) if leader_raw else None,
        members=[TeamAgent(**item) for item in members_raw if item],
    )
    return state


def save_team_state(state: TeamState) -> None:
    payload = asdict(state)
    write_json(Path(state.state_file), payload)


def normalize_injected_message(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " ".join(lines)


def leader_initial_prompt(member_names: Iterable[str]) -> str:
    names = ", ".join(member_names)
    return normalize_injected_message(
        "You are the leader of a small Codex team working in this tmux window. "
        f"You have access to these worker agents: {names}. "
        "To delegate work, include one or more tags exactly like this in your reply: "
        '<message to="agent1">work item here</message>. '
        "The text inside each tag will be sent verbatim to that worker. "
        'Worker replies will come back to you as <reply from="agentN">...</reply> messages. '
        "Do not ask the human to relay messages for you. "
        "You can still talk normally outside the tags."
    )


def worker_initial_prompt(name: str) -> str:
    return normalize_injected_message(
        f"You are {name}, a worker in a codex-team tmux session. "
        "You will receive tasks from the leader as normal user messages. "
        "Work on the task and reply normally in natural language. "
        "Do not emit <message ...> tags yourself. "
        "After you answer, wait for the next leader task."
    )


def shell_join(args: Iterable[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def initial_launch_command(agent: TeamAgent, member_names: Iterable[str]) -> str:
    prompt = leader_initial_prompt(member_names) if agent.role == "leader" else worker_initial_prompt(agent.name)
    args = ["codex"]
    if agent.role != "leader":
        args.extend(
            [
                "-c",
                f'model_reasoning_effort="{WORKER_REASONING_EFFORT}"',
                "-c",
                f'plan_mode_reasoning_effort="{WORKER_REASONING_EFFORT}"',
            ]
        )
    args.append(prompt)
    return shell_join(args)


def extract_last_agent_block(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    prompt_indices = [idx for idx, line in enumerate(lines) if PROMPT_LINE_RE.match(line)]
    if prompt_indices:
        end = prompt_indices[-1]
        start = prompt_indices[-2] + 1 if len(prompt_indices) >= 2 else 0
        return "\n".join(lines[start:end]).strip()

    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    if end == 0:
        return ""
    start = end - 1
    while start > 0 and lines[start - 1].strip():
        start -= 1
    return "\n".join(lines[start:end]).strip()


def parse_routed_messages(block: str) -> list[tuple[str, str]]:
    routed: list[tuple[str, str]] = []
    for match in MESSAGE_RE.finditer(block or ""):
        receiver = match.group(1).strip()
        text = match.group(2).strip()
        if receiver and text:
            routed.append((receiver, text))
    return routed


def build_reply_message(name: str, text: str) -> str:
    body = normalize_injected_message(text) or "(no reply text captured)"
    return f"<reply from=\"{name}\">{body}</reply>"


def build_system_message(text: str) -> str:
    return f"<system>{normalize_injected_message(text)}</system>"


def capture_pane_text(pane: str, *, lines: int = 160) -> str:
    return run_tmux_text("capture-pane", "-t", pane, "-p", "-S", f"-{lines}")


def wait_for_daemon_start(pid_file: Path, timeout: float = 10.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text.isdigit() and pid_is_alive(int(text)):
            return int(text)
        time.sleep(0.1)
    raise RuntimeError("timed out waiting for codex-team monitor startup")


def agent_setup_status(agent: TeamAgent) -> str:
    if agent.ready:
        return "ready"
    if agent.initial_prompt_sent:
        return "waiting for initial reply"
    if agent.thread_id:
        return f"thread detected ({agent.thread_id[:8]}…)"
    if agent.launch_requested:
        return "waiting for codex process"
    return "starting codex"


def team_is_ready(state: TeamState) -> bool:
    agents = state.all_agents()
    return bool(agents) and all(agent.initial_prompt_sent and agent.ready for agent in agents)


def team_members_ready(state: TeamState) -> bool:
    return all(agent.initial_prompt_sent and agent.ready for agent in state.members)


def log_tail_text(path: Path, *, lines: int = 8) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def pending_completion_for_thread(
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
        event = parse_codex_log_event(line)
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


def collect_pending_completion_events(state: TeamState) -> list[CodexLogEvent]:
    log_path = Path(acw_config.codex_log_path())
    events: list[CodexLogEvent] = []
    for agent in state.all_agents():
        if not agent.thread_id:
            continue
        event = pending_completion_for_thread(
            agent.thread_id,
            agent.last_handled_turn,
            agent.last_handled_thread,
            log_path=log_path,
        )
        if event is not None:
            events.append(event)
    return events


def wait_for_team_ready(
    state_path: Path,
    *,
    timeout: float = 60.0,
    progress_file=None,
    include_leader: bool = True,
) -> TeamState:
    if progress_file is None:
        progress_file = sys.stdout
    seen_status: dict[str, str] = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not state_path.exists():
            time.sleep(0.1)
            continue
        state = load_team_state(state_path)
        agents = state.all_agents() if include_leader else state.members
        for agent in agents:
            status = agent_setup_status(agent)
            if seen_status.get(agent.name) != status:
                print(f"setup: {agent.name} {status}", file=progress_file, flush=True)
                seen_status[agent.name] = status
        if (team_is_ready(state) if include_leader else team_members_ready(state)):
            return state
        if state.monitor_pid and not pid_is_alive(state.monitor_pid):
            detail = log_tail_text(Path(state.log_file))
            if detail:
                raise RuntimeError(f"codex-team monitor exited during setup:\n{detail}")
            raise RuntimeError("codex-team monitor exited during setup")
        time.sleep(0.2)
    detail = ""
    if state_path.exists():
        state = load_team_state(state_path)
        agents = state.all_agents() if include_leader else state.members
        detail = ", ".join(f"{agent.name}={agent_setup_status(agent)}" for agent in agents)
    raise RuntimeError(f"timed out waiting for codex-team setup to finish ({detail})")


def start_monitor(state: TeamState) -> int:
    log_path = Path(state.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "__daemon", state.state_file],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            cwd=str(Path(__file__).resolve().parents[1]),
            env=os.environ.copy(),
            start_new_session=True,
        )
    state.monitor_pid = proc.pid
    save_team_state(state)
    return wait_for_daemon_start(Path(state.pid_file))


def stop_monitor(state: TeamState) -> None:
    pid = state.monitor_pid
    if pid and pid_is_alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if not pid_is_alive(pid):
                break
            time.sleep(0.1)
    Path(state.pid_file).unlink(missing_ok=True)
    state.monitor_pid = 0


def forget_team_state(state: TeamState) -> None:
    Path(state.state_file).unlink(missing_ok=True)


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
        team_id=f"{acw_tmux.tmux_scope_token(preferred_tmux_socket(), preferred_tmux_socket_fn=preferred_tmux_socket)}:{window_id}",
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
    print(
        f"setup: waiting for {len(state.members)} workers to become ready",
        flush=True,
    )
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
    log_path = Path(state.log_file)
    changed = False
    if refresh_agent_threads(state):
        changed = True
    member_names = [member.name for member in state.members]
    for agent in state.all_agents():
        if not pane_exists(agent.pane):
            raise RuntimeError(f"pane disappeared: {agent.pane}")
        if not agent.thread_id:
            current_cmd = pane_current_command(agent.pane)
            if current_cmd in KNOWN_SHELL_COMMANDS:
                now = time.time()
                if not agent.launch_requested or (now - agent.launch_requested_at) >= 5.0:
                    result = tmux_send(
                        agent.pane,
                        initial_launch_command(agent, member_names),
                        ENTER_DELAY_SECS,
                    )
                    if result.status == "ok":
                        agent.launch_requested = True
                        agent.launch_requested_at = now
                        agent.initial_prompt_sent = True
                        agent.ignore_next_completion = True
                        agent.busy = True
                        append_log(
                            log_path,
                            f"launch: started codex in {agent.name} pane={agent.pane} with initial prompt",
                        )
                        changed = True
                    else:
                        append_log(
                            log_path,
                            f"launch: failed to start codex in {agent.name} pane={agent.pane}: {result.detail}",
                        )
    return changed


def deliver_pending(agent: TeamAgent, log_path: Path) -> bool:
    if not agent.pending_messages or not agent.initial_prompt_sent:
        return False
    message = agent.pending_messages.pop(0)
    result = tmux_send(agent.pane, normalize_injected_message(message), ENTER_DELAY_SECS)
    if result.status != "ok":
        agent.pending_messages.insert(0, message)
        append_log(
            log_path,
            f"route: failed to send queued message to {agent.name} pane={agent.pane}: {result.detail}",
        )
        return False
    agent.busy = True
    agent.ready = False
    append_log(log_path, f"route: queued message to {agent.name} pane={agent.pane}")
    return True


def mark_completion_handled(agent: TeamAgent, event: CodexLogEvent) -> None:
    agent.last_handled_turn = event.turn_id
    agent.last_handled_thread = event.thread_id


def process_events(
    state: TeamState,
    events: list[CodexLogEvent],
) -> bool:
    if state.leader is None:
        return False
    changed = False
    log_path = Path(state.log_file)
    by_thread = {
        agent.thread_id: agent
        for agent in state.all_agents()
        if agent.thread_id
    }
    for event in events:
        if event.kind != "completion" or event.detail != "false":
            continue
        agent = by_thread.get(event.thread_id)
        if agent is None:
            continue
        block = extract_last_agent_block(capture_pane_text(agent.pane))
        if agent.ignore_next_completion:
            agent.ignore_next_completion = False
            agent.busy = False
            agent.ready = True
            mark_completion_handled(agent, event)
            changed = True
            append_log(log_path, f"ready: {agent.name} initialized on thread={event.thread_id}")
            continue

        agent.busy = False
        agent.ready = True
        mark_completion_handled(agent, event)
        if agent.role == "leader":
            routed = parse_routed_messages(block)
            if not routed:
                append_log(log_path, f"leader: no routed messages on thread={event.thread_id}")
                changed = True
                continue
            for receiver, message in routed:
                member = state.member_named(receiver)
                if member is None:
                    state.leader.pending_messages.append(
                        build_system_message(
                            f"unknown receiver '{receiver}'. Valid worker names: "
                            + ", ".join(member.name for member in state.members)
                        )
                    )
                    append_log(log_path, f"leader: unknown receiver {receiver}")
                    changed = True
                    continue
                member.pending_messages.append(message)
                append_log(log_path, f"leader: queued message for {receiver}")
                changed = True
        else:
            reply = build_reply_message(agent.name, block)
            state.leader.pending_messages.append(reply)
            append_log(log_path, f"worker: queued reply from {agent.name} to leader")
            changed = True
    return changed


def monitor_loop(state_file: Path) -> int:
    state = load_team_state(state_file)
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
    save_team_state(state)
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    append_log(log_path, f"monitor: started for window={state.window_id}")

    try:
        while not stop_requested:
            if state.leader is None or not pane_exists(state.leader.pane):
                append_log(log_path, "monitor: leader pane disappeared; exiting")
                return 0
            changed = bootstrap_agents(state)
            events = collect_pending_completion_events(state)
            if events and process_events(state, events):
                changed = True
            if state.leader is not None and deliver_pending(state.leader, log_path):
                changed = True
            for member in state.members:
                if deliver_pending(member, log_path):
                    changed = True
            if changed:
                save_team_state(state)
            time.sleep(0.2)
    finally:
        pid_path.unlink(missing_ok=True)
        state.monitor_pid = 0
        try:
            save_team_state(state)
        except OSError:
            pass
        append_log(log_path, f"monitor: stopped for window={state.window_id}")
    return 0


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
