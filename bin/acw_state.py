from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path


STATE_DB_FILENAME = "acw_state.sqlite"
SESSION_REF_PREFIX = "sqlite:session:"
THREAD_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

_SESSION_DEFAULTS: dict[str, object] = {
    "thread_id": "",
    "pane": "",
    "name": "",
    "message": "",
    "tmux_socket": "",
    "paused": False,
    "generation": 0,
    "health": "",
    "health_detail": "",
    "health_ts": "",
    "last_continue_at": "",
    "last_handled_turn": "",
    "last_handled_thread": "",
}

_RUNTIME_DEFAULTS: dict[str, object] = {
    "thread": "",
    "runtime_id": "",
    "daemon_pid": "",
    "pane": "",
    "state": "",
    "watch": "",
    "cwd": "",
    "msg_inline": "",
    "tmux_socket": "",
    "paused": False,
    "runtime_status": "",
    "runtime_detail": "",
    "generation": 0,
    "worker_label": "",
    "heartbeat_ts": "",
}


def is_thread_id(value: str) -> bool:
    return bool(THREAD_ID_RE.fullmatch(value or ""))


def state_db_path(state_dir: str) -> str:
    return os.path.join(state_dir, STATE_DB_FILENAME)


def session_state_ref(thread_id: str) -> str:
    return f"{SESSION_REF_PREFIX}{thread_id.lower()}"


def thread_id_from_state_ref(ref: str) -> str:
    if not ref.startswith(SESSION_REF_PREFIX):
        return ""
    thread_id = ref[len(SESSION_REF_PREFIX) :].strip().lower()
    return thread_id if is_thread_id(thread_id) else ""


def _connect(state_dir: str) -> sqlite3.Connection:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_db_path(state_dir), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(conn)
    _maybe_migrate_legacy_json(conn, state_dir)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS acw_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            thread_id TEXT PRIMARY KEY,
            pane TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            tmux_socket TEXT NOT NULL DEFAULT '',
            paused INTEGER NOT NULL DEFAULT 0,
            generation INTEGER NOT NULL DEFAULT 0,
            health TEXT NOT NULL DEFAULT '',
            health_detail TEXT NOT NULL DEFAULT '',
            health_ts TEXT NOT NULL DEFAULT '',
            last_continue_at TEXT NOT NULL DEFAULT '',
            last_handled_turn TEXT NOT NULL DEFAULT '',
            last_handled_thread TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS sessions_by_scope_name
        ON sessions (tmux_socket, name);

        CREATE INDEX IF NOT EXISTS sessions_by_scope_pane
        ON sessions (tmux_socket, pane);

        CREATE TABLE IF NOT EXISTS runtimes (
            thread_id TEXT PRIMARY KEY,
            runtime_id TEXT NOT NULL DEFAULT '',
            daemon_pid TEXT NOT NULL DEFAULT '',
            pane TEXT NOT NULL DEFAULT '',
            state_ref TEXT NOT NULL DEFAULT '',
            watch_log TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            message_inline TEXT NOT NULL DEFAULT '',
            tmux_socket TEXT NOT NULL DEFAULT '',
            paused INTEGER NOT NULL DEFAULT 0,
            runtime_status TEXT NOT NULL DEFAULT '',
            runtime_detail TEXT NOT NULL DEFAULT '',
            generation INTEGER NOT NULL DEFAULT 0,
            worker_label TEXT NOT NULL DEFAULT '',
            heartbeat_ts TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS runtimes_by_scope_pane
        ON runtimes (tmux_socket, pane);

        CREATE INDEX IF NOT EXISTS runtimes_by_scope_runtime_id
        ON runtimes (tmux_socket, runtime_id);
        """
    )


def _migration_done(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM acw_meta WHERE key = 'json_migration_v1'").fetchone()
    return bool(row and str(row[0]) == "1")


def _mark_migration_done(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO acw_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("json_migration_v1", "1"),
    )


def _maybe_migrate_legacy_json(conn: sqlite3.Connection, state_dir: str) -> None:
    if _migration_done(conn):
        return

    state_root = Path(state_dir)
    session_paths = sorted(state_root.glob("acw_session.*.json"))
    runtime_paths = sorted(state_root.glob("acw_runtime.*.json"))

    with conn:
        for path in session_paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            thread_id = str(data.get("thread_id", "")).strip().lower()
            if not is_thread_id(thread_id):
                continue
            merged = _SESSION_DEFAULTS | {
                "thread_id": thread_id,
                "pane": str(data.get("pane", "")).strip(),
                "name": str(data.get("name", "")).strip(),
                "message": str(data.get("message", "")).strip(),
                "tmux_socket": str(data.get("tmux_socket", "")).strip(),
                "paused": bool(data.get("paused", False)),
                "generation": _coerce_int(data.get("generation", 0)),
                "health": str(data.get("health", "")).strip(),
                "health_detail": str(data.get("health_detail", "")).strip(),
                "health_ts": str(data.get("health_ts", "")).strip(),
                "last_continue_at": str(data.get("last_continue_at", "")).strip(),
                "last_handled_turn": str(data.get("last_handled_turn", "")).strip(),
                "last_handled_thread": str(data.get("last_handled_thread", "")).strip(),
            }
            _upsert_session(conn, merged)

        for path in runtime_paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            thread_id = str(data.get("thread", "")).strip().lower()
            if not is_thread_id(thread_id):
                continue
            merged = _RUNTIME_DEFAULTS | {
                "thread": thread_id,
                "runtime_id": str(data.get("runtime_id", "")).strip()
                or str(data.get("pid", "")).strip(),
                "daemon_pid": str(data.get("daemon_pid", "")).strip(),
                "pane": str(data.get("pane", "")).strip(),
                "state": session_state_ref(thread_id),
                "watch": str(data.get("watch", "")).strip(),
                "cwd": str(data.get("cwd", "")).strip(),
                "msg_inline": str(data.get("msg_inline", "")).strip(),
                "tmux_socket": str(data.get("tmux_socket", "")).strip(),
                "paused": bool(data.get("paused", False)),
                "runtime_status": str(data.get("runtime_status", "")).strip(),
                "runtime_detail": str(data.get("runtime_detail", "")).strip(),
                "generation": _coerce_int(data.get("generation", 0)),
                "worker_label": str(data.get("worker_label", "")).strip()
                or str(data.get("runtime_id", "")).strip()
                or str(data.get("pid", "")).strip(),
                "heartbeat_ts": str(data.get("heartbeat_ts", "")).strip(),
            }
            _upsert_runtime(conn, merged)

        _mark_migration_done(conn)

    for path in [*session_paths, *runtime_paths]:
        path.unlink(missing_ok=True)


def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _session_from_row(row: sqlite3.Row | None) -> dict[str, object]:
    data = dict(_SESSION_DEFAULTS)
    if row is None:
        return data
    keys = set(row.keys())
    data.update(
        {
            "thread_id": str(row["thread_id"]).lower() if "thread_id" in keys else "",
            "pane": str(row["pane"]) if "pane" in keys else "",
            "name": str(row["name"]) if "name" in keys else "",
            "message": str(row["message"]) if "message" in keys else "",
            "tmux_socket": str(row["tmux_socket"]) if "tmux_socket" in keys else "",
            "paused": bool(row["paused"]) if "paused" in keys else False,
            "generation": _coerce_int(row["generation"]) if "generation" in keys else 0,
            "health": str(row["health"]) if "health" in keys else "",
            "health_detail": str(row["health_detail"]) if "health_detail" in keys else "",
            "health_ts": str(row["health_ts"]) if "health_ts" in keys else "",
            "last_continue_at": str(row["last_continue_at"]) if "last_continue_at" in keys else "",
            "last_handled_turn": str(row["last_handled_turn"]) if "last_handled_turn" in keys else "",
            "last_handled_thread": str(row["last_handled_thread"]) if "last_handled_thread" in keys else "",
        }
    )
    return data


def _runtime_from_row(row: sqlite3.Row | None) -> dict[str, object]:
    data = dict(_RUNTIME_DEFAULTS)
    if row is None:
        return data
    keys = set(row.keys())
    runtime_id = str(row["runtime_id"]).strip() if "runtime_id" in keys else ""
    data.update(
        {
            "thread": str(row["thread_id"]).lower() if "thread_id" in keys else "",
            "runtime_id": runtime_id,
            "pid": runtime_id,
            "daemon_pid": str(row["daemon_pid"]).strip() if "daemon_pid" in keys else "",
            "pane": str(row["pane"]).strip() if "pane" in keys else "",
            "state": str(row["state_ref"]).strip() if "state_ref" in keys else "",
            "watch": str(row["watch_log"]).strip() if "watch_log" in keys else "",
            "cwd": str(row["cwd"]).strip() if "cwd" in keys else "",
            "msg_inline": str(row["message_inline"]).strip() if "message_inline" in keys else "",
            "tmux_socket": str(row["tmux_socket"]).strip() if "tmux_socket" in keys else "",
            "paused": bool(row["paused"]) if "paused" in keys else False,
            "runtime_status": str(row["runtime_status"]).strip() if "runtime_status" in keys else "",
            "runtime_detail": str(row["runtime_detail"]).strip() if "runtime_detail" in keys else "",
            "generation": _coerce_int(row["generation"]) if "generation" in keys else 0,
            "worker_label": str(row["worker_label"]).strip() if "worker_label" in keys else "",
            "heartbeat_ts": str(row["heartbeat_ts"]).strip() if "heartbeat_ts" in keys else "",
        }
    )
    return data


def _upsert_session(conn: sqlite3.Connection, data: dict[str, object]) -> None:
    conn.execute(
        """
        INSERT INTO sessions(
            thread_id, pane, name, message, tmux_socket, paused, generation,
            health, health_detail, health_ts, last_continue_at,
            last_handled_turn, last_handled_thread
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            pane = excluded.pane,
            name = excluded.name,
            message = excluded.message,
            tmux_socket = excluded.tmux_socket,
            paused = excluded.paused,
            generation = excluded.generation,
            health = excluded.health,
            health_detail = excluded.health_detail,
            health_ts = excluded.health_ts,
            last_continue_at = excluded.last_continue_at,
            last_handled_turn = excluded.last_handled_turn,
            last_handled_thread = excluded.last_handled_thread
        """,
        (
            str(data["thread_id"]).lower(),
            str(data["pane"]).strip(),
            str(data["name"]).strip(),
            str(data["message"]).strip(),
            str(data["tmux_socket"]).strip(),
            1 if data["paused"] else 0,
            _coerce_int(data["generation"]),
            str(data["health"]).strip(),
            str(data["health_detail"]).strip(),
            str(data["health_ts"]).strip(),
            str(data["last_continue_at"]).strip(),
            str(data["last_handled_turn"]).strip(),
            str(data["last_handled_thread"]).strip(),
        ),
    )


def _upsert_runtime(conn: sqlite3.Connection, data: dict[str, object]) -> None:
    conn.execute(
        """
        INSERT INTO runtimes(
            thread_id, runtime_id, daemon_pid, pane, state_ref, watch_log, cwd,
            message_inline, tmux_socket, paused, runtime_status, runtime_detail,
            generation, worker_label, heartbeat_ts
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            runtime_id = excluded.runtime_id,
            daemon_pid = excluded.daemon_pid,
            pane = excluded.pane,
            state_ref = excluded.state_ref,
            watch_log = excluded.watch_log,
            cwd = excluded.cwd,
            message_inline = excluded.message_inline,
            tmux_socket = excluded.tmux_socket,
            paused = excluded.paused,
            runtime_status = excluded.runtime_status,
            runtime_detail = excluded.runtime_detail,
            generation = excluded.generation,
            worker_label = excluded.worker_label,
            heartbeat_ts = excluded.heartbeat_ts
        """,
        (
            str(data["thread"]).lower(),
            str(data["runtime_id"]).strip(),
            str(data["daemon_pid"]).strip(),
            str(data["pane"]).strip(),
            str(data["state"]).strip(),
            str(data["watch"]).strip(),
            str(data["cwd"]).strip(),
            str(data["msg_inline"]).strip(),
            str(data["tmux_socket"]).strip(),
            1 if data["paused"] else 0,
            str(data["runtime_status"]).strip(),
            str(data["runtime_detail"]).strip(),
            _coerce_int(data["generation"]),
            str(data["worker_label"]).strip(),
            str(data["heartbeat_ts"]).strip(),
        ),
    )


def read_session_state(thread_id: str, *, state_dir: str) -> dict[str, object]:
    thread = (thread_id or "").strip().lower()
    if not is_thread_id(thread):
        return {}
    conn = _connect(state_dir)
    try:
        row = conn.execute(
            """
            SELECT thread_id, pane, name, message, tmux_socket, paused, generation,
                   health, health_detail, health_ts, last_continue_at,
                   last_handled_turn, last_handled_thread
            FROM sessions WHERE thread_id = ?
            """,
            (thread,),
        ).fetchone()
        if row is None:
            return {}
        return _session_from_row(row)
    finally:
        conn.close()


def write_session_state(thread_id: str, data: dict[str, object], *, state_dir: str) -> None:
    thread = (thread_id or "").strip().lower()
    if not is_thread_id(thread):
        return
    conn = _connect(state_dir)
    try:
        existing = _session_from_row(
            conn.execute(
                """
                SELECT thread_id, pane, name, message, tmux_socket, paused, generation,
                       health, health_detail, health_ts, last_continue_at,
                       last_handled_turn, last_handled_thread
                FROM sessions WHERE thread_id = ?
                """,
                (thread,),
            ).fetchone()
        )
        merged = dict(_SESSION_DEFAULTS)
        merged.update(existing)
        merged.update(data)
        merged["thread_id"] = thread
        with conn:
            _upsert_session(conn, merged)
    finally:
        conn.close()


def remove_session_state(thread_id: str, *, state_dir: str) -> bool:
    thread = (thread_id or "").strip().lower()
    if not is_thread_id(thread):
        return False
    conn = _connect(state_dir)
    try:
        with conn:
            deleted = conn.execute("DELETE FROM sessions WHERE thread_id = ?", (thread,)).rowcount
        return bool(deleted)
    finally:
        conn.close()


def load_sessions(*, state_dir: str, scope: str) -> list[dict[str, object]]:
    conn = _connect(state_dir)
    try:
        rows = conn.execute(
            """
            SELECT thread_id, pane, name, message, tmux_socket, paused, generation,
                   health, health_detail, health_ts, last_continue_at
            FROM sessions
            WHERE tmux_socket = ?
            ORDER BY name, thread_id
            """,
            (scope,),
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            session = _session_from_row(row)
            session["state_file"] = session_state_ref(str(session["thread_id"]))
            result.append(session)
        return result
    finally:
        conn.close()


def write_runtime_state(
    thread_id: str,
    data: dict[str, object],
    *,
    state_dir: str,
    scope: str,
) -> None:
    thread = (thread_id or "").strip().lower()
    if not is_thread_id(thread):
        return
    conn = _connect(state_dir)
    try:
        existing = _runtime_from_row(
            conn.execute(
                """
                SELECT thread_id, runtime_id, daemon_pid, pane, state_ref, watch_log, cwd,
                       message_inline, tmux_socket, paused, runtime_status, runtime_detail,
                       generation, worker_label, heartbeat_ts
                FROM runtimes WHERE thread_id = ?
                """,
                (thread,),
            ).fetchone()
        )
        merged = dict(_RUNTIME_DEFAULTS)
        merged.update(existing)
        merged.update(data)
        merged["thread"] = thread
        merged["tmux_socket"] = scope
        merged["state"] = str(merged.get("state", "")).strip() or session_state_ref(thread)
        merged["runtime_id"] = str(merged.get("runtime_id", "")).strip() or str(merged.get("pid", "")).strip()
        merged["worker_label"] = str(merged.get("worker_label", "")).strip() or str(merged["runtime_id"]).strip()
        with conn:
            _upsert_runtime(conn, merged)
    finally:
        conn.close()


def remove_runtime_state(thread_id: str, *, state_dir: str, scope: str = "") -> None:
    thread = (thread_id or "").strip().lower()
    if not is_thread_id(thread):
        return
    conn = _connect(state_dir)
    try:
        with conn:
            if scope:
                conn.execute("DELETE FROM runtimes WHERE thread_id = ? AND tmux_socket = ?", (thread, scope))
            else:
                conn.execute("DELETE FROM runtimes WHERE thread_id = ?", (thread,))
    finally:
        conn.close()


def read_runtime_rows(*, state_dir: str, scope: str) -> list[dict[str, object]]:
    conn = _connect(state_dir)
    try:
        rows = conn.execute(
            """
            SELECT thread_id, runtime_id, daemon_pid, pane, state_ref, watch_log, cwd,
                   message_inline, tmux_socket, paused, runtime_status, runtime_detail,
                   generation, worker_label, heartbeat_ts
            FROM runtimes
            WHERE tmux_socket = ?
            ORDER BY pane, thread_id
            """,
            (scope,),
        ).fetchall()
        result: list[dict[str, object]] = []
        stale_threads: list[str] = []
        for row in rows:
            runtime = _runtime_from_row(row)
            daemon_pid = str(runtime.get("daemon_pid", "")).strip()
            if daemon_pid and daemon_pid.isdigit():
                try:
                    os.kill(int(daemon_pid), 0)
                except OSError:
                    stale_threads.append(str(runtime["thread"]))
                    continue
            runtime["paused"] = "1" if runtime["paused"] else ""
            result.append(runtime)
        if stale_threads:
            with conn:
                conn.executemany("DELETE FROM runtimes WHERE thread_id = ?", [(thread,) for thread in stale_threads])
        return result
    finally:
        conn.close()


def read_state_ref(ref: str, *, state_dir: str) -> dict[str, object]:
    thread_id = thread_id_from_state_ref(ref)
    if thread_id:
        return read_session_state(thread_id, state_dir=state_dir)
    return {}


def cleanup_rows(
    *,
    state_dir: str,
    scope: str,
    active_threads: set[str],
    daemon_pid_alive,
) -> tuple[int, int]:
    conn = _connect(state_dir)
    try:
        removed_runtimes = 0
        removed_sessions = 0
        runtime_rows = conn.execute(
            "SELECT thread_id, daemon_pid FROM runtimes WHERE tmux_socket = ?",
            (scope,),
        ).fetchall()
        stale_threads: list[str] = []
        for row in runtime_rows:
            daemon_pid = str(row["daemon_pid"]).strip()
            if daemon_pid and daemon_pid_alive(daemon_pid):
                continue
            stale_threads.append(str(row["thread_id"]).lower())
        with conn:
            if stale_threads:
                removed_runtimes = conn.executemany(
                    "DELETE FROM runtimes WHERE thread_id = ?",
                    [(thread,) for thread in stale_threads],
                ).rowcount
            removable_sessions = [
                row[0]
                for row in conn.execute(
                    "SELECT thread_id FROM sessions WHERE tmux_socket = ?",
                    (scope,),
                ).fetchall()
                if str(row[0]).lower() not in active_threads
            ]
            if removable_sessions:
                removed_sessions = conn.executemany(
                    "DELETE FROM sessions WHERE thread_id = ?",
                    [(thread,) for thread in removable_sessions],
                ).rowcount
        return removed_sessions, removed_runtimes
    finally:
        conn.close()
