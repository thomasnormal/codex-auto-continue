from __future__ import annotations

import json
import socket
import time
import traceback
from pathlib import Path
from typing import Callable


class RpcError(RuntimeError):
    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


def listen(socket_path: str) -> socket.socket:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen()
    server.setblocking(False)
    return server


def call(
    socket_path: str,
    request: dict[str, object],
    *,
    on_progress: Callable[[str], None] | None = None,
    timeout_secs: float = 5.0,
) -> dict[str, object]:
    deadline = time.time() + max(timeout_secs, 0.1)
    last_error: OSError | None = None
    sock: socket.socket | None = None
    while time.time() < deadline:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(max(deadline - time.time(), 0.1))
            sock.connect(socket_path)
            break
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
                sock = None
            time.sleep(0.05)
    if sock is None:
        detail = str(last_error) if last_error else ""
        raise RpcError(f"failed to connect to acw daemon socket {socket_path}", detail=detail)

    reader = None
    writer = None
    try:
        sock.settimeout(max(deadline - time.time(), 0.1))
        reader = sock.makefile("r", encoding="utf-8")
        writer = sock.makefile("w", encoding="utf-8")
        writer.write(json.dumps(request) + "\n")
        writer.flush()
        while True:
            try:
                line = reader.readline()
            except OSError as exc:
                raise RpcError("timed out waiting for daemon RPC response", detail=str(exc)) from exc
            if not line:
                break
            if not line.strip():
                continue
            response = json.loads(line)
            kind = str(response.get("kind", "")).strip()
            if kind == "progress":
                message = str(response.get("message", "")).strip()
                if message and on_progress is not None:
                    on_progress(message)
                continue
            if kind == "result":
                payload = response.get("payload", {})
                return payload if isinstance(payload, dict) else {"value": payload}
            if kind == "error":
                raise RpcError(
                    str(response.get("message", "daemon RPC failed")).strip() or "daemon RPC failed",
                    detail=str(response.get("detail", "")).strip(),
                )
        raise RpcError("daemon closed control connection without returning a result")
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
        sock.close()


def serve_pending(
    server: socket.socket,
    *,
    handle_request: Callable[[dict[str, object], Callable[[str], None]], dict[str, object] | None],
) -> int:
    handled = 0
    while True:
        try:
            conn, _ = server.accept()
        except BlockingIOError:
            break

        handled += 1
        conn.settimeout(1.0)
        reader = conn.makefile("r", encoding="utf-8")
        writer = conn.makefile("w", encoding="utf-8")

        def send(kind: str, **payload: object) -> None:
            writer.write(json.dumps({"kind": kind, **payload}) + "\n")
            writer.flush()

        try:
            line = reader.readline()
            if not line:
                continue
            request = json.loads(line)
            if not isinstance(request, dict):
                raise RpcError("invalid daemon RPC request", detail="request must be a JSON object")

            def send_progress(message: str) -> None:
                send("progress", message=message)

            payload = handle_request(request, send_progress)
            send("result", payload=payload or {})
        except json.JSONDecodeError as exc:
            send("error", message="invalid daemon RPC request", detail=str(exc))
        except RpcError as exc:
            send("error", message=str(exc), detail=exc.detail)
        except Exception as exc:  # pragma: no cover - defensive daemon-side failure reporting
            send("error", message=str(exc) or exc.__class__.__name__, detail=traceback.format_exc())
        finally:
            writer.close()
            reader.close()
            conn.close()
    return handled
