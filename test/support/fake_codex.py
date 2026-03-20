#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import termios
import time
import uuid


THREAD_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _set_process_name() -> None:
    try:
        libc = ctypes.CDLL(None)
        libc.prctl(15, ctypes.c_char_p(b"codex"), 0, 0, 0)
    except Exception:
        pass


def _new_id() -> str:
    return str(uuid.uuid4()).lower()


def _now_parts() -> tuple[int, int]:
    now_ns = time.time_ns()
    return now_ns // 1_000_000_000, now_ns % 1_000_000_000


def _log_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}Z INFO {line}\n")


def _ensure_state_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "create table if not exists threads ("
        "id text primary key, "
        "title text, "
        "first_user_message text, "
        "created_at integer, "
        "updated_at integer, "
        "rollout_path text not null default '', "
        "cwd text not null default '', "
        "git_branch text, "
        "git_sha text)"
    )
    columns = {row[1] for row in conn.execute("pragma table_info(threads)")}
    for name, ddl in (
        ("rollout_path", "alter table threads add column rollout_path text not null default ''"),
        ("cwd", "alter table threads add column cwd text not null default ''"),
        ("git_branch", "alter table threads add column git_branch text"),
        ("git_sha", "alter table threads add column git_sha text"),
    ):
        if name not in columns:
            conn.execute(ddl)
    conn.execute(
        "create table if not exists logs ("
        "thread_id text, "
        "process_uuid text, "
        "ts integer, "
        "ts_nanos integer)"
    )
    conn.commit()
    return conn


def _git_repo_state(cwd: Path) -> tuple[str, str]:
    branch = ""
    sha = ""
    try:
        branch_proc = subprocess.run(
            ["git", "-C", str(cwd), "symbolic-ref", "--quiet", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if branch_proc.returncode == 0:
            branch = branch_proc.stdout.strip()
        sha_proc = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if sha_proc.returncode == 0:
            sha = sha_proc.stdout.strip()
    except FileNotFoundError:
        return "", ""
    return branch, sha


def _upsert_thread(
    conn: sqlite3.Connection,
    thread_id: str,
    *,
    prompt: str,
    created_at: int,
    rollout_path: Path,
    cwd: Path,
    git_branch: str,
    git_sha: str,
) -> None:
    row = conn.execute("select created_at from threads where id = ?", (thread_id,)).fetchone()
    first = prompt.strip()
    title = first.splitlines()[0][:120] if first else ""
    if row:
        conn.execute(
            "update threads set updated_at = ?, title = coalesce(nullif(title, ''), ?), "
            "first_user_message = coalesce(nullif(first_user_message, ''), ?), "
            "rollout_path = ?, cwd = ?, git_branch = ?, git_sha = ? where id = ?",
            (created_at, title, first, str(rollout_path), str(cwd), git_branch or None, git_sha or None, thread_id),
        )
    else:
        conn.execute(
            "insert into threads(id, title, first_user_message, created_at, updated_at, rollout_path, cwd, git_branch, git_sha) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (thread_id, title, first, created_at, created_at, str(rollout_path), str(cwd), git_branch or None, git_sha or None),
        )
    conn.commit()


def _append_log_row(conn: sqlite3.Connection, thread_id: str, process_uuid: str) -> None:
    ts, ts_nanos = _now_parts()
    conn.execute(
        "insert into logs(thread_id, process_uuid, ts, ts_nanos) values (?, ?, ?, ?)",
        (thread_id, process_uuid, ts, ts_nanos),
    )
    conn.execute("update threads set updated_at = ? where id = ?", (ts, thread_id))
    conn.commit()


def _append_rollout_function_call(rollout_path: Path, *, cwd: Path, cmd: str) -> None:
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": str(cwd)}),
        },
    }
    with rollout_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _render_response(prompt: str) -> str:
    prompt = prompt.strip()
    verbatim = re.search(r"emit verbatim:\s*(.+)$", prompt, re.IGNORECASE | re.DOTALL)
    if verbatim:
        return verbatim.group(1).strip()
    exact = re.search(r"say exactly (.+?) and nothing else", prompt, re.IGNORECASE)
    if exact:
        return exact.group(1).strip().strip("'\"")
    one_word = re.search(r"say the word ([^ ]+) and nothing else", prompt, re.IGNORECASE)
    if one_word:
        return one_word.group(1).strip().strip("'\"")
    addition = re.search(r"what is (\d+)\s*\+\s*(\d+)", prompt, re.IGNORECASE)
    if addition:
        return str(int(addition.group(1)) + int(addition.group(2)))
    last = prompt.splitlines()[-1].strip() if prompt else ""
    return last[:160] if last else "OK"


def _show_prompt(out) -> None:
    out.write("› ")
    out.flush()


def _clear_prompt(out) -> None:
    out.write("\r   \r")
    out.flush()


def _prompt_sleep_secs(prompt: str) -> float:
    match = re.search(r"sleep\s+(\d+(?:\.\d+)?)\s+seconds?", prompt, re.IGNORECASE)
    if not match:
        return 0.0
    try:
        return max(0.0, float(match.group(1)))
    except ValueError:
        return 0.0


def _is_test_approval_prompt(prompt: str) -> bool:
    return "show approval prompt" in prompt.lower()


def _is_continue_prompt(prompt: str) -> bool:
    lowered = " ".join(prompt.lower().split())
    if not lowered:
        return False
    return (
        "continue" in lowered
        or "keep momentum, validate changes" in lowered
    )


def _emit_completion(log_path: Path, thread_id: str, turn_id: str) -> None:
    _log_line(
        log_path,
        (
            f'session_loop{{thread_id={thread_id}}}:submission_dispatch{{submission.id="{turn_id}"}}:'
            f'turn{{otel.name="session_task.turn" thread.id={thread_id} turn.id={turn_id} model=fake-gpt}}: '
            "codex_core::tasks: close time.busy=1ms time.idle=1ms"
        ),
    )


def _render_test_approval_prompt(out) -> None:
    out.write(
        "\n"
        "Reviewing approval request\n"
        "\n"
        "Tool call needs your approval. Reason: network access outside the allowlist.\n"
        "Do you want to approve network access to \"https://example.com\"?\n"
        "\n"
        "› 1. Yes, allow\n"
        "  2. No, continue without permissions\n"
        "\n"
        "  ctrl + p / ctrl + n change field\n"
    )
    out.flush()


def _handle_prompt(
    prompt: str,
    *,
    conn: sqlite3.Connection,
    log_path: Path,
    rollout_path: Path,
    thread_id: str,
    process_uuid: str,
    cwd: Path,
    git_branch: str,
    git_sha: str,
    out,
) -> bool:
    prompt = prompt.strip()
    if not prompt:
        return True
    if _is_continue_prompt(prompt):
        return True
    ts, _ = _now_parts()
    _append_rollout_function_call(rollout_path, cwd=cwd, cmd="fake_codex_prompt")
    _upsert_thread(
        conn,
        thread_id,
        prompt=prompt,
        created_at=ts,
        rollout_path=rollout_path,
        cwd=cwd,
        git_branch=git_branch,
        git_sha=git_sha,
    )
    _append_log_row(conn, thread_id, process_uuid)
    if _is_test_approval_prompt(prompt):
        _render_test_approval_prompt(out)
        return False
    out.write("\nworking...\n")
    out.flush()
    sleep_secs = _prompt_sleep_secs(prompt)
    if sleep_secs > 0.0:
        time.sleep(sleep_secs)
    response = _render_response(prompt)
    out.write(f"\n• {response}\n\n")
    out.flush()
    _emit_completion(log_path, thread_id, _new_id())
    return True


def main(argv: list[str]) -> int:
    _set_process_name()
    tty = None
    tty_restore = None

    def _stop(_signum, _frame) -> None:
        raise SystemExit(130)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    home = Path.home() / ".codex"
    log_path = home / "log" / "codex-tui.log"
    sessions_dir = home / "sessions"
    state_db = home / "state_fake.sqlite"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    args = argv[1:]
    prompt = ""
    if args[:1] == ["resume"] and len(args) >= 2 and THREAD_RE.fullmatch(args[1].lower()):
        thread_id = args[1].lower()
        if len(args) > 2:
            prompt = " ".join(args[2:])
    else:
        thread_id = _new_id()
        prompt = " ".join(args)

    rollout_path = sessions_dir / f"rollout-{thread_id}.jsonl"
    cwd = Path.cwd()
    git_branch, git_sha = _git_repo_state(cwd)
    process_uuid = f"pid:{os.getpid()}:fakecodex"
    conn = _ensure_state_db(state_db)
    session_path = sessions_dir / f"{thread_id}.json"
    session_path.write_text(f'{{"thread_id":"{thread_id}"}}\n', encoding="utf-8")
    session_handle = session_path.open("a+", encoding="utf-8")
    try:
        try:
            tty = open("/dev/tty", "r", encoding="utf-8", errors="ignore")
            tty_fd = tty.fileno()
            tty_restore = termios.tcgetattr(tty_fd)
            tty_flags = termios.tcgetattr(tty_fd)
            tty_flags[3] &= ~termios.ECHO
            termios.tcsetattr(tty_fd, termios.TCSANOW, tty_flags)
        except OSError:
            tty = sys.stdin
            tty_restore = None
        ts, _ = _now_parts()
        _upsert_thread(
            conn,
            thread_id,
            prompt=prompt,
            created_at=ts,
            rollout_path=rollout_path,
            cwd=cwd,
            git_branch=git_branch,
            git_sha=git_sha,
        )
        _log_line(log_path, f"session_init:shell_snapshot{{thread_id={thread_id}}}: codex_core::shell_snapshot: new")
        show_prompt = True
        if prompt:
            show_prompt = _handle_prompt(
                prompt,
                conn=conn,
                log_path=log_path,
                rollout_path=rollout_path,
                thread_id=thread_id,
                process_uuid=process_uuid,
                cwd=cwd,
                git_branch=git_branch,
                git_sha=git_sha,
                out=sys.stdout,
            )
        if show_prompt:
            _show_prompt(sys.stdout)
        while True:
            line = tty.readline()
            if line == "":
                break
            _clear_prompt(sys.stdout)
            show_prompt = _handle_prompt(
                line,
                conn=conn,
                log_path=log_path,
                rollout_path=rollout_path,
                thread_id=thread_id,
                process_uuid=process_uuid,
                cwd=cwd,
                git_branch=git_branch,
                git_sha=git_sha,
                out=sys.stdout,
            )
            if show_prompt:
                _show_prompt(sys.stdout)
    finally:
        if tty is not None and tty_restore is not None:
            try:
                termios.tcsetattr(tty.fileno(), termios.TCSANOW, tty_restore)
            except OSError:
                pass
        if tty is not None and tty is not sys.stdin:
            tty.close()
        session_handle.close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
