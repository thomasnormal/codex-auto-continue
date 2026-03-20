from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from acw_ids import is_thread_id


STATE_DB_FILENAME = "acw_state.sqlite"
SESSION_REF_PREFIX = "sqlite:session:"
_JSON_MIGRATION_KEY = "json_migration_v1"
_SQLITE_MIGRATION_KEY = "sqlite_agent_model_v1"
_SQLITE_WAL_KEY = "sqlite_wal_v1"
_INITIALIZED_DB_PATHS: set[str] = set()

_AGENT_DEFAULTS: dict[str, object] = {
    "agent_id": "",
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
    "continue_count": 0,
    "last_handled_turn": "",
    "last_handled_thread": "",
    "loop_signature": "",
    "loop_score": 0,
}

_RUNTIME_DEFAULTS: dict[str, object] = {
    "agent_id": "",
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

_DAEMON_SCOPE_DEFAULTS: dict[str, object] = {
    "tmux_socket": "",
    "daemon_pid": "",
    "socket_path": "",
    "heartbeat_ts": "",
    "started_ts": "",
}
def state_db_path(state_dir: str) -> str:
    return os.path.join(state_dir, STATE_DB_FILENAME)


def clear_connection_init_cache() -> None:
    _INITIALIZED_DB_PATHS.clear()


def session_state_ref(thread_id: str) -> str:
    return f"{SESSION_REF_PREFIX}{thread_id.lower()}"


def thread_id_from_state_ref(ref: str) -> str:
    if not ref.startswith(SESSION_REF_PREFIX):
        return ""
    thread_id = ref[len(SESSION_REF_PREFIX) :].strip().lower()
    return thread_id if is_thread_id(thread_id) else ""


def _connect(state_dir: str) -> sqlite3.Connection:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    db_path = state_db_path(state_dir)
    db_key = os.path.realpath(db_path)
    needs_init = db_key not in _INITIALIZED_DB_PATHS or not Path(db_path).exists()
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if needs_init:
        _ensure_schema(conn)
        _maybe_enable_wal(conn)
        _maybe_migrate_legacy_json(conn, state_dir)
        _maybe_migrate_legacy_sqlite_tables(conn)
        _INITIALIZED_DB_PATHS.add(db_key)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS acw_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
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
            continue_count INTEGER NOT NULL DEFAULT 0,
            last_handled_turn TEXT NOT NULL DEFAULT '',
            last_handled_thread TEXT NOT NULL DEFAULT '',
            loop_signature TEXT NOT NULL DEFAULT '',
            loop_score INTEGER NOT NULL DEFAULT 0,
            CHECK(agent_id <> ''),
            CHECK(thread_id <> '')
        );

        CREATE UNIQUE INDEX IF NOT EXISTS agents_by_scope_thread
        ON agents (tmux_socket, thread_id);

        CREATE UNIQUE INDEX IF NOT EXISTS agents_by_scope_pane
        ON agents (tmux_socket, pane)
        WHERE pane <> '';

        CREATE INDEX IF NOT EXISTS agents_by_scope_name
        ON agents (tmux_socket, name);

        CREATE TABLE IF NOT EXISTS agent_runtimes (
            agent_id TEXT PRIMARY KEY,
            runtime_id TEXT NOT NULL DEFAULT '',
            daemon_pid TEXT NOT NULL DEFAULT '',
            state_ref TEXT NOT NULL DEFAULT '',
            watch_log TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            message_inline TEXT NOT NULL DEFAULT '',
            paused INTEGER NOT NULL DEFAULT 0,
            runtime_status TEXT NOT NULL DEFAULT '',
            runtime_detail TEXT NOT NULL DEFAULT '',
            generation INTEGER NOT NULL DEFAULT 0,
            worker_label TEXT NOT NULL DEFAULT '',
            heartbeat_ts TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX IF NOT EXISTS agent_runtimes_by_runtime_id
        ON agent_runtimes (runtime_id)
        WHERE runtime_id <> '';

        CREATE TABLE IF NOT EXISTS daemon_scopes (
            tmux_socket TEXT PRIMARY KEY,
            daemon_pid TEXT NOT NULL DEFAULT '',
            socket_path TEXT NOT NULL DEFAULT '',
            heartbeat_ts TEXT NOT NULL DEFAULT '',
            started_ts TEXT NOT NULL DEFAULT ''
        );
        """
    )
    _ensure_column(conn, "agents", "continue_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "agents", "loop_signature", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "agents", "loop_score", "INTEGER NOT NULL DEFAULT 0")


def _coerce_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {
        str(row[1]).strip()
        for row in conn.execute(f"PRAGMA table_info({name})").fetchall()
        if len(row) > 1 and str(row[1]).strip()
    }


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column in _table_columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _meta_value(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM acw_meta WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else ""


def _set_meta_value(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO acw_meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _migration_done(conn: sqlite3.Connection, key: str) -> bool:
    return _meta_value(conn, key) == "1"


def _mark_migration_done(conn: sqlite3.Connection, key: str) -> None:
    _set_meta_value(conn, key, "1")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _maybe_enable_wal(conn: sqlite3.Connection) -> None:
    if _meta_value(conn, _SQLITE_WAL_KEY) == "1":
        return
    try:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    except sqlite3.OperationalError:
        return
    mode = str(row[0]).strip().lower() if row else ""
    if mode != "wal":
        return
    with conn:
        _set_meta_value(conn, _SQLITE_WAL_KEY, "1")


def _agent_id_for_thread(thread_id: str) -> str:
    return thread_id.lower()


def _agent_from_row(row: sqlite3.Row | None) -> dict[str, object]:
    data = dict(_AGENT_DEFAULTS)
    if row is None:
        return data
    keys = set(row.keys())
    data.update(
        {
            "agent_id": str(row["agent_id"]).strip() if "agent_id" in keys else "",
            "thread_id": str(row["thread_id"]).lower() if "thread_id" in keys else "",
            "pane": str(row["pane"]).strip() if "pane" in keys else "",
            "name": str(row["name"]).strip() if "name" in keys else "",
            "message": str(row["message"]).strip() if "message" in keys else "",
            "tmux_socket": str(row["tmux_socket"]).strip() if "tmux_socket" in keys else "",
            "paused": bool(row["paused"]) if "paused" in keys else False,
            "generation": _coerce_int(row["generation"]) if "generation" in keys else 0,
            "health": str(row["health"]).strip() if "health" in keys else "",
            "health_detail": str(row["health_detail"]).strip() if "health_detail" in keys else "",
            "health_ts": str(row["health_ts"]).strip() if "health_ts" in keys else "",
            "last_continue_at": str(row["last_continue_at"]).strip() if "last_continue_at" in keys else "",
            "continue_count": _coerce_int(row["continue_count"]) if "continue_count" in keys else 0,
            "last_handled_turn": str(row["last_handled_turn"]).strip() if "last_handled_turn" in keys else "",
            "last_handled_thread": str(row["last_handled_thread"]).strip()
            if "last_handled_thread" in keys
            else "",
            "loop_signature": str(row["loop_signature"]).strip() if "loop_signature" in keys else "",
            "loop_score": _coerce_int(row["loop_score"]) if "loop_score" in keys else 0,
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
            "agent_id": str(row["agent_id"]).strip() if "agent_id" in keys else "",
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


def _daemon_scope_from_row(row: sqlite3.Row | None) -> dict[str, object]:
    data = dict(_DAEMON_SCOPE_DEFAULTS)
    if row is None:
        return data
    keys = set(row.keys())
    data.update(
        {
            "tmux_socket": str(row["tmux_socket"]).strip() if "tmux_socket" in keys else "",
            "daemon_pid": str(row["daemon_pid"]).strip() if "daemon_pid" in keys else "",
            "socket_path": str(row["socket_path"]).strip() if "socket_path" in keys else "",
            "heartbeat_ts": str(row["heartbeat_ts"]).strip() if "heartbeat_ts" in keys else "",
            "started_ts": str(row["started_ts"]).strip() if "started_ts" in keys else "",
        }
    )
    return data


def _agent_query() -> str:
    return """
        SELECT agent_id, thread_id, pane, name, message, tmux_socket, paused, generation,
               health, health_detail, health_ts, last_continue_at, continue_count,
               last_handled_turn, last_handled_thread, loop_signature, loop_score
        FROM agents
    """


def _runtime_query() -> str:
    return """
        SELECT a.agent_id, a.thread_id, a.pane, a.tmux_socket,
               r.runtime_id, r.daemon_pid, r.state_ref, r.watch_log, r.cwd,
               r.message_inline, r.paused, r.runtime_status, r.runtime_detail,
               r.generation, r.worker_label, r.heartbeat_ts
        FROM agent_runtimes r
        JOIN agents a ON a.agent_id = r.agent_id
    """


def _agent_row_for_thread(conn: sqlite3.Connection, thread_id: str) -> dict[str, object]:
    row = conn.execute(_agent_query() + " WHERE thread_id = ?", (thread_id.lower(),)).fetchone()
    return _agent_from_row(row)


def _runtime_row_for_thread(conn: sqlite3.Connection, thread_id: str) -> dict[str, object]:
    row = conn.execute(_runtime_query() + " WHERE a.thread_id = ?", (thread_id.lower(),)).fetchone()
    return _runtime_from_row(row)


def _upsert_agent(conn: sqlite3.Connection, data: dict[str, object]) -> None:
    conn.execute(
        """
        INSERT INTO agents(
            agent_id, thread_id, pane, name, message, tmux_socket, paused, generation,
            health, health_detail, health_ts, last_continue_at, continue_count,
            last_handled_turn, last_handled_thread, loop_signature, loop_score
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            thread_id = excluded.thread_id,
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
            continue_count = excluded.continue_count,
            last_handled_turn = excluded.last_handled_turn,
            last_handled_thread = excluded.last_handled_thread,
            loop_signature = excluded.loop_signature,
            loop_score = excluded.loop_score
        """,
        (
            str(data["agent_id"]).strip(),
            str(data["thread_id"]).strip().lower(),
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
            _coerce_int(data["continue_count"]),
            str(data["last_handled_turn"]).strip(),
            str(data["last_handled_thread"]).strip(),
            str(data["loop_signature"]).strip(),
            _coerce_int(data["loop_score"]),
        ),
    )


def _upsert_runtime(conn: sqlite3.Connection, data: dict[str, object]) -> None:
    conn.execute(
        """
        INSERT INTO agent_runtimes(
            agent_id, runtime_id, daemon_pid, state_ref, watch_log, cwd,
            message_inline, paused, runtime_status, runtime_detail,
            generation, worker_label, heartbeat_ts
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id) DO UPDATE SET
            runtime_id = excluded.runtime_id,
            daemon_pid = excluded.daemon_pid,
            state_ref = excluded.state_ref,
            watch_log = excluded.watch_log,
            cwd = excluded.cwd,
            message_inline = excluded.message_inline,
            paused = excluded.paused,
            runtime_status = excluded.runtime_status,
            runtime_detail = excluded.runtime_detail,
            generation = excluded.generation,
            worker_label = excluded.worker_label,
            heartbeat_ts = excluded.heartbeat_ts
        """,
        (
            str(data["agent_id"]).strip(),
            str(data["runtime_id"]).strip(),
            str(data["daemon_pid"]).strip(),
            str(data["state"]).strip(),
            str(data["watch"]).strip(),
            str(data["cwd"]).strip(),
            str(data["msg_inline"]).strip(),
            1 if data["paused"] else 0,
            str(data["runtime_status"]).strip(),
            str(data["runtime_detail"]).strip(),
            _coerce_int(data["generation"]),
            str(data["worker_label"]).strip(),
            str(data["heartbeat_ts"]).strip(),
        ),
    )


def _upsert_daemon_scope(conn: sqlite3.Connection, data: dict[str, object]) -> None:
    conn.execute(
        """
        INSERT INTO daemon_scopes(
            tmux_socket, daemon_pid, socket_path, heartbeat_ts, started_ts
        ) VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(tmux_socket) DO UPDATE SET
            daemon_pid = excluded.daemon_pid,
            socket_path = excluded.socket_path,
            heartbeat_ts = excluded.heartbeat_ts,
            started_ts = excluded.started_ts
        """,
        (
            str(data["tmux_socket"]).strip(),
            str(data["daemon_pid"]).strip(),
            str(data["socket_path"]).strip(),
            str(data["heartbeat_ts"]).strip(),
            str(data["started_ts"]).strip(),
        ),
    )


def _ensure_agent(conn: sqlite3.Connection, thread_id: str, *, pane: str = "", scope: str = "") -> dict[str, object]:
    existing = _agent_row_for_thread(conn, thread_id)
    if existing.get("agent_id"):
        changed = False
        if pane and not existing.get("pane"):
            existing["pane"] = pane
            changed = True
        if scope and not existing.get("tmux_socket"):
            existing["tmux_socket"] = scope
            changed = True
        if changed:
            _upsert_agent(conn, existing)
        return existing

    created = dict(_AGENT_DEFAULTS)
    created.update(
        {
            "agent_id": _agent_id_for_thread(thread_id),
            "thread_id": thread_id.lower(),
            "pane": pane,
            "tmux_socket": scope,
        }
    )
    _upsert_agent(conn, created)
    return created


def _migrate_session_payload(conn: sqlite3.Connection, payload: dict[str, object]) -> None:
    thread_id = str(payload.get("thread_id", "")).strip().lower()
    if not is_thread_id(thread_id):
        return
    merged = dict(_AGENT_DEFAULTS)
    merged.update(
        {
            "agent_id": _agent_id_for_thread(thread_id),
            "thread_id": thread_id,
            "pane": str(payload.get("pane", "")).strip(),
            "name": str(payload.get("name", "")).strip(),
            "message": str(payload.get("message", "")).strip(),
            "tmux_socket": str(payload.get("tmux_socket", "")).strip(),
            "paused": bool(payload.get("paused", False)),
            "generation": _coerce_int(payload.get("generation", 0)),
            "health": str(payload.get("health", "")).strip(),
            "health_detail": str(payload.get("health_detail", "")).strip(),
            "health_ts": str(payload.get("health_ts", "")).strip(),
            "last_continue_at": str(payload.get("last_continue_at", "")).strip(),
            "continue_count": _coerce_int(payload.get("continue_count", 0)),
            "last_handled_turn": str(payload.get("last_handled_turn", "")).strip(),
            "last_handled_thread": str(payload.get("last_handled_thread", "")).strip(),
            "loop_signature": str(payload.get("loop_signature", "")).strip(),
            "loop_score": _coerce_int(payload.get("loop_score", 0)),
        }
    )
    _upsert_agent(conn, merged)


def _migrate_runtime_payload(conn: sqlite3.Connection, payload: dict[str, object]) -> None:
    thread_id = str(payload.get("thread", payload.get("thread_id", ""))).strip().lower()
    if not is_thread_id(thread_id):
        return
    pane = str(payload.get("pane", "")).strip()
    scope = str(payload.get("tmux_socket", "")).strip()
    agent = _ensure_agent(conn, thread_id, pane=pane, scope=scope)
    merged = dict(_RUNTIME_DEFAULTS)
    merged.update(
        {
            "agent_id": str(agent["agent_id"]).strip(),
            "thread": thread_id,
            "runtime_id": str(payload.get("runtime_id", payload.get("pid", ""))).strip(),
            "daemon_pid": str(payload.get("daemon_pid", "")).strip(),
            "pane": pane or str(agent.get("pane", "")).strip(),
            "state": str(payload.get("state", payload.get("state_ref", ""))).strip() or session_state_ref(thread_id),
            "watch": str(payload.get("watch", payload.get("watch_log", ""))).strip(),
            "cwd": str(payload.get("cwd", "")).strip(),
            "msg_inline": str(payload.get("msg_inline", payload.get("message_inline", ""))).strip(),
            "tmux_socket": scope or str(agent.get("tmux_socket", "")).strip(),
            "paused": bool(payload.get("paused", False)),
            "runtime_status": str(payload.get("runtime_status", "")).strip(),
            "runtime_detail": str(payload.get("runtime_detail", "")).strip(),
            "generation": _coerce_int(payload.get("generation", 0)),
            "worker_label": str(payload.get("worker_label", "")).strip()
            or str(payload.get("runtime_id", payload.get("pid", ""))).strip(),
            "heartbeat_ts": str(payload.get("heartbeat_ts", "")).strip(),
        }
    )
    if pane and pane != agent.get("pane", ""):
        agent = dict(agent)
        agent["pane"] = pane
        _upsert_agent(conn, agent)
    _upsert_runtime(conn, merged)


def _maybe_migrate_legacy_json(conn: sqlite3.Connection, state_dir: str) -> None:
    if _migration_done(conn, _JSON_MIGRATION_KEY):
        return

    state_root = Path(state_dir)
    session_paths = sorted(state_root.glob("acw_session.*.json"))
    runtime_paths = sorted(state_root.glob("acw_runtime.*.json"))

    with conn:
        for path in session_paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                _migrate_session_payload(conn, payload)

        for path in runtime_paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                _migrate_runtime_payload(conn, payload)

        _mark_migration_done(conn, _JSON_MIGRATION_KEY)

    for path in [*session_paths, *runtime_paths]:
        path.unlink(missing_ok=True)


def _maybe_migrate_legacy_sqlite_tables(conn: sqlite3.Connection) -> None:
    if _migration_done(conn, _SQLITE_MIGRATION_KEY):
        return

    has_sessions = _table_exists(conn, "sessions")
    has_runtimes = _table_exists(conn, "runtimes")
    if not has_sessions and not has_runtimes:
        with conn:
            _mark_migration_done(conn, _SQLITE_MIGRATION_KEY)
        return

    with conn:
        if has_sessions:
            rows = conn.execute(
                """
                SELECT thread_id, pane, name, message, tmux_socket, paused, generation,
                       health, health_detail, health_ts, last_continue_at,
                       last_handled_turn, last_handled_thread
                FROM sessions
                """
            ).fetchall()
            for row in rows:
                _migrate_session_payload(conn, dict(row))

        if has_runtimes:
            rows = conn.execute(
                """
                SELECT thread_id, runtime_id, daemon_pid, pane, state_ref, watch_log, cwd,
                       message_inline, tmux_socket, paused, runtime_status, runtime_detail,
                       generation, worker_label, heartbeat_ts
                FROM runtimes
                """
            ).fetchall()
            for row in rows:
                payload = dict(row)
                payload["thread"] = payload.pop("thread_id", "")
                payload["state"] = payload.pop("state_ref", "")
                payload["watch"] = payload.pop("watch_log", "")
                payload["msg_inline"] = payload.pop("message_inline", "")
                _migrate_runtime_payload(conn, payload)

        if has_runtimes:
            conn.execute("DROP TABLE IF EXISTS runtimes")
        if has_sessions:
            conn.execute("DROP TABLE IF EXISTS sessions")
        _mark_migration_done(conn, _SQLITE_MIGRATION_KEY)


def read_session_state(thread_id: str, *, state_dir: str) -> dict[str, object]:
    thread = (thread_id or "").strip().lower()
    if not is_thread_id(thread):
        return {}
    conn = _connect(state_dir)
    try:
        row = conn.execute(_agent_query() + " WHERE thread_id = ?", (thread,)).fetchone()
        if row is None:
            return {}
        return _agent_from_row(row)
    finally:
        conn.close()


def write_session_state(thread_id: str, data: dict[str, object], *, state_dir: str) -> None:
    thread = (thread_id or "").strip().lower()
    if not is_thread_id(thread):
        return
    conn = _connect(state_dir)
    try:
        existing = _agent_row_for_thread(conn, thread)
        merged = dict(_AGENT_DEFAULTS)
        merged.update(existing)
        merged.update(data)
        merged["thread_id"] = thread
        merged["agent_id"] = str(merged.get("agent_id", "")).strip() or str(existing.get("agent_id", "")).strip()
        if not merged["agent_id"]:
            merged["agent_id"] = _agent_id_for_thread(thread)
        with conn:
            _upsert_agent(conn, merged)
    finally:
        conn.close()


def remove_session_state(thread_id: str, *, state_dir: str) -> bool:
    thread = (thread_id or "").strip().lower()
    if not is_thread_id(thread):
        return False
    conn = _connect(state_dir)
    try:
        with conn:
            deleted = conn.execute("DELETE FROM agents WHERE thread_id = ?", (thread,)).rowcount
        return bool(deleted)
    finally:
        conn.close()


def load_sessions(*, state_dir: str, scope: str) -> list[dict[str, object]]:
    conn = _connect(state_dir)
    try:
        rows = conn.execute(
            _agent_query() + " WHERE tmux_socket = ? ORDER BY name, thread_id",
            (scope,),
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            session = _agent_from_row(row)
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
        existing = _runtime_row_for_thread(conn, thread)
        pane = str(data.get("pane", existing.get("pane", ""))).strip()
        agent = _ensure_agent(conn, thread, pane=pane, scope=scope)
        if pane and pane != agent.get("pane", ""):
            agent = dict(agent)
            agent["pane"] = pane
            with conn:
                _upsert_agent(conn, agent)

        merged = dict(_RUNTIME_DEFAULTS)
        merged.update(existing)
        merged.update(data)
        merged["agent_id"] = str(agent["agent_id"]).strip()
        merged["thread"] = thread
        merged["pane"] = str(agent.get("pane", "")).strip()
        merged["tmux_socket"] = str(agent.get("tmux_socket", "")).strip() or scope
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
        agent = _agent_row_for_thread(conn, thread)
        if not agent.get("agent_id"):
            return
        if scope and str(agent.get("tmux_socket", "")).strip() != scope:
            return
        with conn:
            conn.execute("DELETE FROM agent_runtimes WHERE agent_id = ?", (str(agent["agent_id"]),))
    finally:
        conn.close()


def read_runtime_rows(*, state_dir: str, scope: str) -> list[dict[str, object]]:
    conn = _connect(state_dir)
    try:
        rows = conn.execute(
            _runtime_query() + " WHERE a.tmux_socket = ? ORDER BY a.pane, a.thread_id",
            (scope,),
        ).fetchall()
        result: list[dict[str, object]] = []
        stale_agent_ids: list[str] = []
        for row in rows:
            runtime = _runtime_from_row(row)
            daemon_pid = str(runtime.get("daemon_pid", "")).strip()
            if daemon_pid and daemon_pid.isdigit():
                try:
                    os.kill(int(daemon_pid), 0)
                except OSError:
                    stale_agent_ids.append(str(runtime["agent_id"]).strip())
                    continue
            runtime["paused"] = "1" if runtime["paused"] else ""
            result.append(runtime)
        if stale_agent_ids:
            with conn:
                conn.executemany(
                    "DELETE FROM agent_runtimes WHERE agent_id = ?",
                    [(agent_id,) for agent_id in stale_agent_ids],
                )
        return result
    finally:
        conn.close()


def write_daemon_scope_state(
    *,
    state_dir: str,
    scope: str,
    daemon_pid: str,
    socket_path: str,
    heartbeat_ts: str,
    started_ts: str = "",
) -> None:
    conn = _connect(state_dir)
    try:
        merged = dict(_DAEMON_SCOPE_DEFAULTS)
        merged.update(
            _daemon_scope_from_row(
                conn.execute(
                    """
                    SELECT tmux_socket, daemon_pid, socket_path, heartbeat_ts, started_ts
                    FROM daemon_scopes
                    WHERE tmux_socket = ?
                    """,
                    (scope,),
                ).fetchone()
            )
        )
        merged.update(
            {
                "tmux_socket": scope,
                "daemon_pid": daemon_pid,
                "socket_path": socket_path,
                "heartbeat_ts": heartbeat_ts,
            }
        )
        if started_ts:
            merged["started_ts"] = started_ts
        with conn:
            _upsert_daemon_scope(conn, merged)
    finally:
        conn.close()


def read_daemon_scope_state(*, state_dir: str, scope: str) -> dict[str, object]:
    conn = _connect(state_dir)
    try:
        row = conn.execute(
            """
            SELECT tmux_socket, daemon_pid, socket_path, heartbeat_ts, started_ts
            FROM daemon_scopes
            WHERE tmux_socket = ?
            """,
            (scope,),
        ).fetchone()
        return _daemon_scope_from_row(row)
    finally:
        conn.close()


def clear_daemon_scope_state(*, state_dir: str, scope: str) -> None:
    conn = _connect(state_dir)
    try:
        with conn:
            conn.execute("DELETE FROM daemon_scopes WHERE tmux_socket = ?", (scope,))
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
            """
            SELECT a.agent_id, a.thread_id, r.daemon_pid
            FROM agent_runtimes r
            JOIN agents a ON a.agent_id = r.agent_id
            WHERE a.tmux_socket = ?
            """,
            (scope,),
        ).fetchall()
        stale_agent_ids: list[str] = []
        for row in runtime_rows:
            daemon_pid = str(row["daemon_pid"]).strip()
            if daemon_pid and daemon_pid_alive(daemon_pid):
                continue
            stale_agent_ids.append(str(row["agent_id"]).strip())
        with conn:
            if stale_agent_ids:
                removed_runtimes = conn.executemany(
                    "DELETE FROM agent_runtimes WHERE agent_id = ?",
                    [(agent_id,) for agent_id in stale_agent_ids],
                ).rowcount
            removable_agents = [
                str(row["agent_id"]).strip()
                for row in conn.execute(
                    "SELECT agent_id, thread_id FROM agents WHERE tmux_socket = ?",
                    (scope,),
                ).fetchall()
                if str(row["thread_id"]).lower() not in active_threads
            ]
            if removable_agents:
                removed_sessions = conn.executemany(
                    "DELETE FROM agents WHERE agent_id = ?",
                    [(agent_id,) for agent_id in removable_agents],
                ).rowcount
        return removed_sessions, removed_runtimes
    finally:
        conn.close()
