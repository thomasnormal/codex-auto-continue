from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from pathlib import Path

import acw_config
import acw_tmux


STATE_DIR = acw_config.state_dir_path()


class TeamAgentPhase(StrEnum):
    SHELL = "shell"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"


@dataclass
class TeamAgent:
    name: str
    pane: str
    role: str
    thread_id: str = ""
    phase: TeamAgentPhase = TeamAgentPhase.SHELL
    launch_requested_at: float = 0.0
    pending_messages: list[str] = field(default_factory=list)
    last_handled_turn: str = ""
    last_handled_thread: str = ""

    @property
    def ready(self) -> bool:
        return self.phase == TeamAgentPhase.READY

    @property
    def busy(self) -> bool:
        return self.phase in (TeamAgentPhase.STARTING, TeamAgentPhase.BUSY)

    @property
    def launch_requested(self) -> bool:
        return self.phase == TeamAgentPhase.STARTING

    @property
    def initial_prompt_sent(self) -> bool:
        return self.phase != TeamAgentPhase.SHELL

    @property
    def ignore_next_completion(self) -> bool:
        return self.phase == TeamAgentPhase.STARTING


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


def short_window_token(window_id: str) -> str:
    return acw_tmux.sanitize_key(window_id) or "window"


def team_state_path(window_id: str, *, preferred_tmux_socket_fn) -> Path:
    scope = acw_tmux.tmux_scope_token(
        preferred_tmux_socket_fn(),
        preferred_tmux_socket_fn=preferred_tmux_socket_fn,
    )
    return STATE_DIR / f"codex_team.{scope}.{short_window_token(window_id)}.json"


def team_log_path(window_id: str, *, preferred_tmux_socket_fn) -> Path:
    scope = acw_tmux.tmux_scope_token(
        preferred_tmux_socket_fn(),
        preferred_tmux_socket_fn=preferred_tmux_socket_fn,
    )
    return STATE_DIR / f"codex_team.{scope}.{short_window_token(window_id)}.log"


def team_pid_path(window_id: str, *, preferred_tmux_socket_fn) -> Path:
    scope = acw_tmux.tmux_scope_token(
        preferred_tmux_socket_fn(),
        preferred_tmux_socket_fn=preferred_tmux_socket_fn,
    )
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
        leader=_team_agent_from_payload(leader_raw) if leader_raw else None,
        members=[_team_agent_from_payload(item) for item in members_raw if item],
    )
    return state


def save_team_state(state: TeamState) -> None:
    payload = {
        "team_id": state.team_id,
        "tmux_socket": state.tmux_socket,
        "window_id": state.window_id,
        "created_at": state.created_at,
        "state_file": state.state_file,
        "log_file": state.log_file,
        "pid_file": state.pid_file,
        "monitor_pid": state.monitor_pid,
        "leader": _team_agent_payload(state.leader) if state.leader is not None else None,
        "members": [_team_agent_payload(agent) for agent in state.members],
    }
    write_json(Path(state.state_file), payload)


def forget_team_state(state: TeamState) -> None:
    Path(state.state_file).unlink(missing_ok=True)


def log_tail_text(path: Path, *, lines: int = 8) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def _team_agent_from_payload(payload: dict) -> TeamAgent:
    phase_raw = str(payload.get("phase", "")).strip().lower()
    if phase_raw:
        try:
            phase = TeamAgentPhase(phase_raw)
        except ValueError:
            phase = TeamAgentPhase.SHELL
    else:
        phase = _phase_from_legacy_flags(payload)

    return TeamAgent(
        name=str(payload.get("name", "")),
        pane=str(payload.get("pane", "")),
        role=str(payload.get("role", "")),
        thread_id=str(payload.get("thread_id", "")),
        phase=phase,
        launch_requested_at=float(payload.get("launch_requested_at", 0.0) or 0.0),
        pending_messages=list(payload.get("pending_messages") or []),
        last_handled_turn=str(payload.get("last_handled_turn", "")),
        last_handled_thread=str(payload.get("last_handled_thread", "")),
    )


def _team_agent_payload(agent: TeamAgent) -> dict:
    return {
        "name": agent.name,
        "pane": agent.pane,
        "role": agent.role,
        "thread_id": agent.thread_id,
        "phase": str(agent.phase),
        "ready": agent.ready,
        "busy": agent.busy,
        "launch_requested": agent.launch_requested,
        "launch_requested_at": agent.launch_requested_at,
        "initial_prompt_sent": agent.initial_prompt_sent,
        "ignore_next_completion": agent.ignore_next_completion,
        "pending_messages": list(agent.pending_messages),
        "last_handled_turn": agent.last_handled_turn,
        "last_handled_thread": agent.last_handled_thread,
    }


def _phase_from_legacy_flags(payload: dict) -> TeamAgentPhase:
    if payload.get("ready"):
        return TeamAgentPhase.READY
    if payload.get("ignore_next_completion") or payload.get("launch_requested"):
        return TeamAgentPhase.STARTING
    if payload.get("busy"):
        return TeamAgentPhase.BUSY
    if payload.get("initial_prompt_sent"):
        return TeamAgentPhase.READY
    return TeamAgentPhase.SHELL
