from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable
from typing import TextIO


def sanitize_key(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", value)


def tmux_socket_from_env() -> str:
    tmux_env = os.environ.get("TMUX", "")
    if not tmux_env:
        return ""
    parts = tmux_env.split(",", 1)
    if not parts:
        return ""
    sock = parts[0].strip()
    return sock if sock else ""


def preferred_tmux_socket() -> str:
    sock = os.environ.get("AUTO_CONTINUE_TMUX_SOCKET", "")
    return sock if sock else tmux_socket_from_env()


def is_default_tmux_socket_path(sock: str) -> bool:
    return bool(re.fullmatch(r"/tmp/tmux-\d+/default", sock))


def tmux_scope_key(
    socket_path: str | None = None,
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
) -> str:
    sock = preferred_tmux_socket_fn() if socket_path is None else socket_path
    sock = (sock or "").strip()
    if not sock or is_default_tmux_socket_path(sock):
        return ""
    return sock


def tmux_scope_token(
    socket_path: str | None = None,
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
) -> str:
    scope = tmux_scope_key(socket_path, preferred_tmux_socket_fn=preferred_tmux_socket_fn)
    if not scope:
        return "default"
    sanitized = sanitize_key(scope)
    if len(sanitized) <= 48:
        return sanitized
    label = sanitize_key(os.path.basename(scope)) or "scope"
    digest = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:12]
    return f"{label[:24]}-{digest}"


def tmux_scope_label(
    socket_path: str | None = None,
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
) -> str:
    scope = tmux_scope_key(socket_path, preferred_tmux_socket_fn=preferred_tmux_socket_fn)
    return scope if scope else "(default)"


def session_scope_matches_current(
    data: dict,
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
) -> bool:
    return tmux_scope_key(
        str(data.get("tmux_socket", "")).strip(),
        preferred_tmux_socket_fn=preferred_tmux_socket_fn,
    ) == tmux_scope_key(preferred_tmux_socket_fn=preferred_tmux_socket_fn)


def daemon_pid_file_for_scope(
    state_dir: str,
    socket_path: str | None = None,
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
) -> str:
    return os.path.join(
        state_dir,
        f"acw_daemon.{tmux_scope_token(socket_path, preferred_tmux_socket_fn=preferred_tmux_socket_fn)}.pid",
    )


def daemon_log_file_for_scope(
    state_dir: str,
    socket_path: str | None = None,
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
) -> str:
    return os.path.join(
        state_dir,
        f"acw_daemon.{tmux_scope_token(socket_path, preferred_tmux_socket_fn=preferred_tmux_socket_fn)}.log",
    )


def daemon_socket_root() -> str:
    root = os.environ.get("XDG_RUNTIME_DIR", "").strip() or tempfile.gettempdir()
    path = Path(root) / f"acw-{os.getuid()}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return str(path)


def daemon_socket_file_for_scope(
    socket_path: str | None = None,
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
) -> str:
    return os.path.join(
        daemon_socket_root(),
        f"acw_daemon.{tmux_scope_token(socket_path, preferred_tmux_socket_fn=preferred_tmux_socket_fn)}.sock",
    )


def daemon_runtime_file_for_thread(
    state_db_file: str,
    thread_id: str,
    socket_path: str | None = None,
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
) -> str:
    scope = tmux_scope_key(socket_path, preferred_tmux_socket_fn=preferred_tmux_socket_fn)
    token = tmux_scope_token(socket_path, preferred_tmux_socket_fn=preferred_tmux_socket_fn)
    return f"{state_db_file}#runtime:{token}:{scope}:{thread_id.lower()}"


def daemon_runtime_glob_for_scope(
    state_db_file: str,
    socket_path: str | None = None,
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
) -> str:
    scope = tmux_scope_key(socket_path, preferred_tmux_socket_fn=preferred_tmux_socket_fn)
    token = tmux_scope_token(socket_path, preferred_tmux_socket_fn=preferred_tmux_socket_fn)
    return f"{state_db_file}#runtime:{token}:{scope}:*"


def tmux_client_env_healthy(
    *,
    check_output_fn: Callable[..., str] = subprocess.check_output,
) -> bool:
    if not os.environ.get("TMUX", ""):
        return False
    try:
        check_output_fn(
            ["tmux", "display-message", "-p", "#{session_name}"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def tmux_server_pids(
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
    check_output_fn: Callable[..., str] = subprocess.check_output,
) -> list[str]:
    sock = preferred_tmux_socket_fn()
    try:
        ps_out = check_output_fn(
            ["ps", "-u", str(os.getuid()), "-o", "pid=,args="],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    socket_matches: list[str] = []
    pids: list[str] = []
    for line in ps_out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid_str, args = parts
        if not pid_str.isdigit() or "tmux" not in args:
            continue
        if sock and sock in args:
            socket_matches.append(pid_str)
            continue
        pids.append(pid_str)
    if socket_matches:
        return socket_matches
    return pids


def tmux_socket_recovery_hint(
    *,
    preferred_tmux_socket_fn: Callable[[], str] = preferred_tmux_socket,
    tmux_server_pids_fn: Callable[[], list[str]] | None = None,
) -> str:
    sock = preferred_tmux_socket_fn()
    if not sock or os.path.exists(sock):
        return ""

    server_pids_fn = tmux_server_pids if tmux_server_pids_fn is None else tmux_server_pids_fn
    pids = server_pids_fn()
    if not pids:
        return ""
    if len(pids) == 1:
        return (
            f"hint: tmux socket '{sock}' is unreachable; "
            f"recreate it with `kill -USR1 {pids[0]}`"
        )
    joined = ", ".join(pids)
    return (
        f"hint: tmux socket '{sock}' is unreachable; "
        f"if the right server is still running, recreate its socket with "
        f"`kill -USR1 <pid>` (tmux pids: {joined})"
    )


def print_tmux_unavailable_hint(
    target: str,
    *,
    tmux_socket_recovery_hint_fn: Callable[[], str],
    file: TextIO = sys.stderr,
) -> None:
    print(
        f"error: tmux server is unavailable; cannot resolve window target '{target}'",
        file=file,
    )
    print(
        "hint: start/reconnect tmux, or use a pane id if you already know it (for example '%6')",
        file=file,
    )
    recovery = tmux_socket_recovery_hint_fn()
    if recovery:
        print(recovery, file=file)


def run_tmux(
    *args: str,
    preferred_tmux_socket_fn: Callable[[], str],
    tmux_client_env_healthy_fn: Callable[[], bool],
    check_output_fn: Callable[..., str] = subprocess.check_output,
) -> str | None:
    cmd: list[str] = ["tmux"]
    sock = preferred_tmux_socket_fn()
    if sock:
        cmd += ["-S", sock]
    cmd += list(args)
    try:
        return check_output_fn(cmd, stderr=subprocess.DEVNULL, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    if not sock and os.environ.get("TMUX", ""):
        if tmux_client_env_healthy_fn():
            return None
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        try:
            return check_output_fn(
                ["tmux"] + list(args),
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    elif sock:
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        try:
            return check_output_fn(
                ["tmux"] + list(args),
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return None


def resolve_pane_from_window_target(
    target: str,
    *,
    run_tmux_fn: Callable[..., str | None],
    is_pane_id_fn: Callable[[str], bool],
    print_tmux_unavailable_hint_fn: Callable[[str], None],
    quiet: bool = False,
) -> str | None:
    match = re.fullmatch(r"(\d+)", target)
    if match:
        requested_session = ""
        requested_window = match.group(1)
    else:
        match = re.fullmatch(r"([^:]+):(\d+)", target)
        if match:
            requested_session = match.group(1)
            requested_window = match.group(2)
        else:
            return None

    fmt = "#{session_name}\t#{window_index}\t#{pane_id}\t#{pane_active}\t#{window_active}\t#{pane_index}"
    listing = run_tmux_fn("list-panes", "-a", "-F", fmt)
    if listing is None:
        if not quiet:
            print_tmux_unavailable_hint_fn(target)
        return None

    rows: list[dict[str, str]] = []
    session_seen: set[str] = set()
    for line in listing.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        sess, widx, pid_, pactive, wactive, pidx = parts[:6]
        if not pid_ or widx != requested_window:
            continue
        if requested_session and sess != requested_session:
            continue
        rows.append(
            dict(
                session=sess,
                window_index=widx,
                pane_id=pid_,
                pane_active=pactive,
                window_active=wactive,
                pane_index=pidx,
            )
        )
        session_seen.add(sess)

    if not rows:
        return None

    if not requested_session and len(session_seen) > 1:
        active_session = ""
        active_conflict = False
        for row in rows:
            if row["window_active"] != "1":
                continue
            if not active_session:
                active_session = row["session"]
            elif active_session != row["session"]:
                active_conflict = True
                break

        if active_session and not active_conflict:
            requested_session = active_session
        else:
            csv = ",".join(sorted(session_seen))
            if not quiet:
                print(
                    f"error: window '{requested_window}' is ambiguous across tmux sessions: {csv}",
                    file=sys.stderr,
                )
                print(
                    f"hint: use 'session:window' (for example '0:{requested_window}') or a pane id (for example '%6')",
                    file=sys.stderr,
                )
            return None

    selected = ""
    fallback = ""
    for row in rows:
        if requested_session and row["session"] != requested_session:
            continue
        if not fallback:
            fallback = row["pane_id"]
        if row["pane_active"] == "1":
            selected = row["pane_id"]
            break

    if not selected:
        selected = fallback
    return selected if is_pane_id_fn(selected) else None


def resolve_pane_from_window_name(
    name: str,
    *,
    run_tmux_fn: Callable[..., str | None],
    is_pane_id_fn: Callable[[str], bool],
    quiet: bool = False,
) -> str | None:
    fmt = "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_active}"
    listing = run_tmux_fn("list-panes", "-a", "-F", fmt)
    if not listing:
        return None

    matches: list[tuple[str, str, str, str]] = []
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        sess, widx, wname, pid_, pactive = parts[:5]
        if wname == name:
            matches.append((sess, widx, pid_, pactive))

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0][2] if is_pane_id_fn(matches[0][2]) else None

    unique_windows = {(session, window) for session, window, _, _ in matches}
    if len(unique_windows) > 1:
        labels = ", ".join(f"{session}:{window}" for session, window in sorted(unique_windows))
        if not quiet:
            print(
                f"error: window name '{name}' is ambiguous across windows: {labels}",
                file=sys.stderr,
            )
        return None

    for _session, _window, pane_id, pane_active in matches:
        if pane_active == "1" and is_pane_id_fn(pane_id):
            return pane_id
    return matches[0][2] if is_pane_id_fn(matches[0][2]) else None


def resolve_pane_target(
    target: str,
    *,
    is_pane_id_fn: Callable[[str], bool],
    resolve_pane_from_window_target_fn: Callable[[str], str | None],
    resolve_pane_from_window_name_fn: Callable[[str], str | None],
    tmux_socket_recovery_hint_fn: Callable[[], str],
    print_tmux_unavailable_hint_fn: Callable[[str], None],
) -> str:
    if is_pane_id_fn(target):
        return target

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target_fn(target)
        if pane and is_pane_id_fn(pane):
            return pane
        print(f"error: could not resolve window target '{target}'", file=sys.stderr)
        raise SystemExit(1)

    pane = resolve_pane_from_window_name_fn(target)
    if pane and is_pane_id_fn(pane):
        return pane

    if tmux_socket_recovery_hint_fn():
        print_tmux_unavailable_hint_fn(target)
        raise SystemExit(1)

    print(f"error: no managed session or window found for '{target}'", file=sys.stderr)
    print(
        "hint: use a pane id ('%6'), window name, or window index ('2')",
        file=sys.stderr,
    )
    raise SystemExit(1)


def resolve_live_pane_target(
    target: str,
    *,
    is_pane_id_fn: Callable[[str], bool],
    resolve_pane_from_window_target_fn: Callable[[str], str | None],
    resolve_pane_from_window_name_fn: Callable[[str], str | None],
) -> str:
    if is_pane_id_fn(target):
        return target
    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target_fn(target)
        return pane if pane and is_pane_id_fn(pane) else ""
    pane = resolve_pane_from_window_name_fn(target)
    return pane if pane and is_pane_id_fn(pane) else ""


def resolve_start_pane_target(
    target: str,
    *,
    is_pane_id_fn: Callable[[str], bool],
    is_thread_id_fn: Callable[[str], bool],
    resolve_pane_from_window_target_fn: Callable[[str], str | None],
    resolve_pane_from_window_name_fn: Callable[[str], str | None],
    tmux_socket_recovery_hint_fn: Callable[[], str],
    print_tmux_unavailable_hint_fn: Callable[[str], None],
) -> str:
    if is_pane_id_fn(target):
        return target

    if re.fullmatch(r"\d+", target) or re.fullmatch(r"[^:]+:\d+", target):
        pane = resolve_pane_from_window_target_fn(target)
        if pane and is_pane_id_fn(pane):
            return pane
        print(f"error: could not resolve window target '{target}'", file=sys.stderr)
        raise SystemExit(1)

    if is_thread_id_fn(target):
        print(
            "error: start target must be a pane id, window index, session:window, or tmux window name",
            file=sys.stderr,
        )
        print(
            "hint: pass the thread id as the second positional argument: "
            "acw start <target> <thread-id>",
            file=sys.stderr,
        )
        raise SystemExit(2)

    pane = resolve_pane_from_window_name_fn(target)
    if pane and is_pane_id_fn(pane):
        return pane

    if tmux_socket_recovery_hint_fn():
        print_tmux_unavailable_hint_fn(target)
        raise SystemExit(1)

    print(f"error: could not resolve start target '{target}'", file=sys.stderr)
    print(
        "hint: use a pane id ('%6'), window name, or window index ('2')",
        file=sys.stderr,
    )
    raise SystemExit(1)
