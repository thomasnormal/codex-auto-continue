from __future__ import annotations

from dataclasses import dataclass
import re


PROMPT_LINE_RE = re.compile(r"^\s*›(?:\s|$)")
DOC_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_./-]+\.md)\b")
NON_DOC_PATH_RE = re.compile(
    r"\b[A-Za-z0-9_./-]+\.(?:c|cc|cpp|cxx|h|hpp|hh|inc|py|sh|sv|v|td|mlir|tcl|json|ya?ml|toml|rst|txt)\b",
    re.IGNORECASE,
)
CHECKPOINT_MARKER_RE = re.compile(
    r"\b(?:updated|committed|checkpoint|what changed|what is now pinned|"
    r"percent done|frontier|acceptance criteria|what moved materially)\b",
    re.IGNORECASE,
)
CONTINUE_DOC_RE = re.compile(r"^\s*continue\s+by\s+([A-Za-z0-9_./-]+\.md)\s*$", re.IGNORECASE)
LOOP_PAUSE_THRESHOLD = 4


@dataclass(frozen=True)
class LoopObservation:
    signature: str = ""
    detail: str = ""


@dataclass(frozen=True)
class LoopState:
    signature: str = ""
    score: int = 0
    should_pause: bool = False
    reason: str = ""


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


def docs_only_checkpoint_observation(block: str, continue_message: str) -> LoopObservation:
    block = (block or "").strip()
    if not block or not CHECKPOINT_MARKER_RE.search(block):
        return LoopObservation()
    if NON_DOC_PATH_RE.search(block):
        return LoopObservation()

    paths = sorted({match.group(1) for match in DOC_PATH_RE.finditer(block)})
    if not paths:
        normalized = " ".join((continue_message or "").split())
        match = CONTINUE_DOC_RE.match(normalized)
        if match:
            paths = [match.group(1)]
    if not paths:
        return LoopObservation()

    signature = "docs-only:" + "|".join(paths[:6])
    detail = ", ".join(paths[:3])
    if len(paths) > 3:
        detail += ", ..."
    return LoopObservation(signature=signature, detail=detail)


def loop_observation_from_pane_text(text: str, continue_message: str) -> LoopObservation:
    return docs_only_checkpoint_observation(extract_last_agent_block(text), continue_message)


def advance_loop_state(
    previous_signature: str,
    previous_score: int,
    observation: LoopObservation,
    *,
    threshold: int = LOOP_PAUSE_THRESHOLD,
) -> LoopState:
    if not observation.signature:
        return LoopState()

    score = previous_score + 1 if previous_signature == observation.signature else 1
    should_pause = score >= threshold
    reason = ""
    if should_pause:
        reason = (
            f"suspected loop: repeated docs-only checkpoint turns ({score}x)"
            + (f" on {observation.detail}" if observation.detail else "")
        )
    return LoopState(
        signature=observation.signature,
        score=score,
        should_pause=should_pause,
        reason=reason,
    )
