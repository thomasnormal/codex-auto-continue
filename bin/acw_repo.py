from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Callable


_BRANCH_CACHE: dict[str, tuple[float, str]] = {}
_DEFAULT_BRANCH_TTL_SECS = 5.0
_MAX_BRANCH_LABEL_LEN = 20


def clear_branch_cache() -> None:
    _BRANCH_CACHE.clear()


def shorten_branch_label(branch: str, *, max_len: int = _MAX_BRANCH_LABEL_LEN) -> str:
    branch = (branch or "").strip()
    if not branch:
        return "-"
    if len(branch) <= max_len:
        return branch
    return branch[: max_len - 1] + "…"


def _branch_for_cwd_uncached(
    cwd: str,
    *,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    if not cwd or not Path(cwd).is_dir():
        return "-"
    try:
        rc = run_fn(
            ["git", "-C", cwd, "symbolic-ref", "--quiet", "--short", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return "-"
    if rc.returncode == 0:
        branch = rc.stdout.strip()
        if branch:
            return shorten_branch_label(branch)

    detached = run_fn(
        ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if detached.returncode == 0:
        head = detached.stdout.strip()
        if head:
            return shorten_branch_label(f"@{head}")
    return "-"


def branch_for_cwd(
    cwd: str,
    *,
    ttl_secs: float = _DEFAULT_BRANCH_TTL_SECS,
    time_fn: Callable[[], float] = time.time,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    normalized = os.path.realpath(cwd) if cwd else ""
    if not normalized:
        return "-"
    now = time_fn()
    cached = _BRANCH_CACHE.get(normalized)
    if cached and ttl_secs > 0 and now - cached[0] < ttl_secs:
        return cached[1]
    branch = _branch_for_cwd_uncached(normalized, run_fn=run_fn)
    _BRANCH_CACHE[normalized] = (now, branch)
    return branch
