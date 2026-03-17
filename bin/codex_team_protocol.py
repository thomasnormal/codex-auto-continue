from __future__ import annotations

import re
import shlex
from typing import Iterable


MESSAGE_RE = re.compile(r"<message\s+to=\"([^\"]+)\">(.*?)</message>", re.IGNORECASE | re.DOTALL)
PROMPT_LINE_RE = re.compile(r"^\s*›(?:\s|$)")
WORKER_REASONING_EFFORT = "medium"


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


def initial_launch_command(agent, member_names: Iterable[str]) -> str:
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
