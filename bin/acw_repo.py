from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Callable


_BRANCH_CACHE: dict[str, tuple[float, str]] = {}
_REPO_ROOT_CACHE: dict[str, tuple[float, str]] = {}
_DEFAULT_BRANCH_TTL_SECS = 5.0
_MAX_BRANCH_LABEL_LEN = 20


def clear_branch_cache() -> None:
    _BRANCH_CACHE.clear()
    _REPO_ROOT_CACHE.clear()


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


def _normalize_repo_path(path: str) -> Path | None:
    if not path:
        return None
    candidate = Path(os.path.expanduser(path)).resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists():
        return None
    if candidate.is_file():
        candidate = candidate.parent
    return candidate if candidate.is_dir() else None


def _repo_root_for_path_uncached(
    path: str,
    *,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    candidate = _normalize_repo_path(path)
    if candidate is None:
        return ""
    try:
        rc = run_fn(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    if rc.returncode != 0:
        return ""
    root = rc.stdout.strip()
    if not root:
        return ""
    normalized = _normalize_repo_path(root)
    return str(normalized) if normalized is not None else ""


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


def repo_root_for_path(
    path: str,
    *,
    ttl_secs: float = _DEFAULT_BRANCH_TTL_SECS,
    time_fn: Callable[[], float] = time.time,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    normalized = os.path.realpath(os.path.expanduser(path)) if path else ""
    if not normalized:
        return ""
    now = time_fn()
    cached = _REPO_ROOT_CACHE.get(normalized)
    if cached and ttl_secs > 0 and now - cached[0] < ttl_secs:
        return cached[1]
    root = _repo_root_for_path_uncached(normalized, run_fn=run_fn)
    _REPO_ROOT_CACHE[normalized] = (now, root)
    return root


def branch_for_path(
    path: str,
    *,
    ttl_secs: float = _DEFAULT_BRANCH_TTL_SECS,
    time_fn: Callable[[], float] = time.time,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    root = repo_root_for_path(path, ttl_secs=ttl_secs, time_fn=time_fn, run_fn=run_fn)
    if not root:
        return "-"
    return branch_for_cwd(root, ttl_secs=ttl_secs, time_fn=time_fn, run_fn=run_fn)
