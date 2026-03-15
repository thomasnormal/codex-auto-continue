from __future__ import annotations

import io
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "bin"))

import acw_runtime  # noqa: E402
import acw_state  # noqa: E402
import acw_tmux  # noqa: E402
import auto_continue_watchd as acw  # noqa: E402


@dataclass
class MockTurnObservation:
    thread_id: str
    sources: set[str]


@dataclass
class MockPane:
    pane_id: str
    window_index: int
    window_name: str
    shell_pid: str
    thread_id: str = ""
    has_codex_process: bool = False
    last_agent: str = "-"
    last_command: str = ""

    @property
    def window_label(self) -> str:
        return f"0:{self.window_index}:{self.window_name}"


class MockManagerHarness:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._tmpdir = tempfile.TemporaryDirectory(prefix="mock-manager-")
        self.root = Path(self._tmpdir.name)
        self.state_dir = self.root / ".codex"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.tmux_socket = str(self.root / "tmux.sock")
        Path(self.tmux_socket).write_text("", encoding="utf-8")
        self.auth_file = self.state_dir / "auth.json"
        self.auth_file.write_text("{}", encoding="utf-8")
        self.scope = acw_tmux.tmux_scope_key(
            self.tmux_socket,
            preferred_tmux_socket_fn=lambda: self.tmux_socket,
        )
        self.daemon_pid_file = Path(
            acw_tmux.daemon_pid_file_for_scope(
                str(self.state_dir),
                self.tmux_socket,
                preferred_tmux_socket_fn=lambda: self.tmux_socket,
            )
        )
        self.daemon_socket = Path(
            acw_tmux.daemon_socket_file_for_scope(
                self.tmux_socket,
                preferred_tmux_socket_fn=lambda: self.tmux_socket,
            )
        )
        self._tmux_socket_available = True
        self.manager_state_db = self.state_dir / acw_state.STATE_DB_FILENAME
        self._patchers: list[patch] = []
        self._panes: dict[str, MockPane] = {}
        self._current_pane = ""
        self._next_window_index = 0
        self._next_pane_number = 1
        self._next_shell_pid = 4000
        self._thread_counter = 0
        self._runtime_counter = 0
        self._last_turn: Optional[MockTurnObservation] = None
        self._daemon_proc: Optional[subprocess.Popen[str]] = None

    @property
    def pane_id(self) -> str:
        return self._current_pane

    def start(self) -> None:
        self._start_patchers()
        self._create_pane("mock")

    def cleanup(self) -> None:
        self._stop_daemon()
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._patchers.clear()
        self._tmpdir.cleanup()

    def archive_failure_artifacts(self, _test_id: str) -> None:
        return None

    def diagnostics(self) -> str:
        panes = [
            {
                "pane": pane.pane_id,
                "window": pane.window_label,
                "thread_id": pane.thread_id,
                "has_codex_process": pane.has_codex_process,
                "last_agent": pane.last_agent,
            }
            for pane in sorted(self._panes.values(), key=lambda item: item.window_index)
        ]
        sessions = acw_state.load_sessions(state_dir=str(self.state_dir), scope=self.scope)
        runtimes = acw_state.read_runtime_rows(state_dir=str(self.state_dir), scope=self.scope)
        daemon = acw_state.read_daemon_scope_state(state_dir=str(self.state_dir), scope=self.scope)
        return (
            "mock manager diagnostics\n"
            f"panes={json.dumps(panes, sort_keys=True)}\n"
            f"sessions={json.dumps(sessions, sort_keys=True)}\n"
            f"runtimes={json.dumps(runtimes, sort_keys=True)}\n"
            f"daemon={json.dumps(daemon, sort_keys=True)}\n"
            f"daemon_pid_file={self.daemon_pid_file.read_text(encoding='utf-8').strip() if self.daemon_pid_file.exists() else ''}\n"
        )

    def rename_window(self, name: str) -> None:
        self._pane(self._current_pane).window_name = name

    def new_window(self, name: str) -> str:
        return self._create_pane(name)

    def kill_pane(self, pane_id: str) -> None:
        if pane_id not in self._panes:
            return
        del self._panes[pane_id]
        if self._current_pane == pane_id:
            remaining = sorted(self._panes.values(), key=lambda pane: pane.window_index)
            self._current_pane = remaining[0].pane_id if remaining else ""

    def delete_tmux_socket(self) -> None:
        self._tmux_socket_available = False
        Path(self.tmux_socket).unlink(missing_ok=True)

    def recreate_tmux_socket(self) -> None:
        self._tmux_socket_available = True
        Path(self.tmux_socket).write_text("", encoding="utf-8")

    def start_codex(self, prompt: str) -> MockTurnObservation:
        pane = self._pane(self._current_pane)
        pane.thread_id = self._new_thread_id()
        pane.has_codex_process = True
        pane.last_command = f"codex {prompt}"
        pane.last_agent = self._prompt_result(prompt)
        self._last_turn = MockTurnObservation(thread_id=pane.thread_id, sources={"mock_backend"})
        return self._last_turn

    def resume_codex(self, thread_id: str) -> None:
        pane = self._pane(self._current_pane)
        pane.thread_id = thread_id.lower()
        pane.has_codex_process = True
        pane.last_command = f"codex resume {thread_id}"
        pane.last_agent = f"resumed {thread_id[:8]}"
        self._last_turn = MockTurnObservation(thread_id=pane.thread_id, sources={"mock_backend"})

    def wait_for_thread_visible(self, thread_id: str, timeout: float = 15.0) -> None:
        self._wait_for(
            lambda: any(
                pane.thread_id.lower() == thread_id.lower() and pane.has_codex_process
                for pane in self._panes.values()
            ),
            timeout=timeout,
            description=f"thread {thread_id} visible",
        )

    def send_codex_prompt(self, prompt: str) -> None:
        self.send_codex_prompt_to_pane(self._current_pane, prompt)

    def send_codex_prompt_to_pane(self, pane_id: str, prompt: str) -> None:
        pane = self._pane(pane_id)
        if not pane.thread_id:
            raise AssertionError(f"pane {pane_id} has no Codex thread\n{self.diagnostics()}")
        pane.last_command = prompt
        pane.last_agent = self._prompt_result(prompt)
        self._last_turn = MockTurnObservation(thread_id=pane.thread_id, sources={"mock_backend"})

    def reset_codex_log_baseline(self) -> None:
        self._last_turn = None

    def wait_for_first_completed_turn(self, timeout: float = 15.0) -> MockTurnObservation:
        def _probe() -> Optional[MockTurnObservation]:
            return self._last_turn

        turn = self._wait_for_value(_probe, timeout=timeout, description="mock Codex completion")
        self._last_turn = None
        return turn

    def manager_runtime_rows(self) -> list[dict[str, object]]:
        return acw_state.read_runtime_rows(state_dir=str(self.state_dir), scope=self.scope)

    def manager_runtime_row_for_pane(self, pane_id: str) -> Optional[dict[str, object]]:
        rows = [row for row in self.manager_runtime_rows() if str(row.get("pane", "")) == pane_id]
        if len(rows) != 1:
            return None
        return rows[0]

    def manager_runtime_row(self, thread_id: str) -> Optional[dict[str, object]]:
        rows = [
            row
            for row in self.manager_runtime_rows()
            if str(row.get("thread", "")).lower() == thread_id.lower()
        ]
        if len(rows) != 1:
            return None
        return rows[0]

    def manager_session_row(self, thread_id: str) -> Optional[dict[str, object]]:
        row = acw_state.read_session_state(thread_id, state_dir=str(self.state_dir))
        return row or None

    def manager_daemon_pid(self) -> Optional[int]:
        if self._daemon_proc is not None and self._daemon_proc.poll() is None:
            return self._daemon_proc.pid
        return None

    def wait_for_manager_watcher_started(self, thread_id: str, timeout: float = 15.0) -> None:
        self.wait_for_manager_watcher_started_in_pane(self.pane_id, thread_id, timeout=timeout)

    def wait_for_manager_watcher_started_in_pane(
        self,
        pane_id: str,
        thread_id: str,
        timeout: float = 15.0,
    ) -> None:
        log_path = self.manager_watch_log_for_pane(pane_id)

        def _started() -> bool:
            row = self.manager_runtime_row(thread_id)
            if not row:
                return False
            if str(row.get("pane", "")) != pane_id:
                return False
            if str(row.get("runtime_status", "")) != "running":
                return False
            if not log_path.is_file():
                return False
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            return f"watch: pane={pane_id} thread_id={thread_id}" in text

        self._wait_for(_started, timeout=timeout, description="manager runtime startup")

    def wait_for_manager_watcher_stopped_in_pane(self, pane_id: str, timeout: float = 15.0) -> None:
        def _stopped() -> bool:
            return self.manager_runtime_row_for_pane(pane_id) is None

        self._wait_for(_stopped, timeout=timeout, description="manager runtime stopped")

    def wait_for_manager_watcher_paused_in_pane(self, pane_id: str, timeout: float = 15.0) -> None:
        def _paused() -> bool:
            row = self.manager_runtime_row_for_pane(pane_id)
            return bool(row) and str(row.get("paused", "")) == "1"

        self._wait_for(_paused, timeout=timeout, description="manager runtime paused")

    def wait_for_manager_state_contains(self, thread_id: str, needle: str, timeout: float = 15.0) -> str:
        def _probe() -> Optional[str]:
            row = self.manager_session_row(thread_id)
            if not row:
                return None
            text = json.dumps(row, sort_keys=True)
            if needle in text:
                return text
            return None

        return self._wait_for_value(_probe, timeout=timeout, description=f"state containing {needle!r}")

    def make_editor_script(self, new_message: str) -> Path:
        script = self.root / f"editor-{uuid.uuid4().hex[:8]}.sh"
        quoted = shlex.quote(new_message)
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s' {quoted} > \"$1\"\n",
            encoding="utf-8",
        )
        script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return script

    def stop_manager_watcher(self) -> None:
        daemon_pid = self.manager_daemon_pid()
        if daemon_pid is None:
            raise AssertionError(f"mock daemon is not running\n{self.diagnostics()}")
        self._stop_daemon()

    def run_manager(
        self,
        *args: str,
        check: bool = True,
        env_overrides: Optional[dict[str, str]] = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "AUTO_CONTINUE_TMUX_SOCKET": self.tmux_socket,
        }
        if env_overrides:
            env.update(env_overrides)
        argv = ["auto_continue_watchd.py", *args]
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        with patch.object(acw.sys, "argv", argv):
            with patch.dict(acw.os.environ, env, clear=False):
                try:
                    with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                        acw.main()
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        proc = subprocess.CompletedProcess(argv, code, stdout.getvalue(), stderr.getvalue())
        if check and proc.returncode != 0:
            raise AssertionError(
                f"manager command failed: {' '.join(args)}\n"
                f"stdout:\n{proc.stdout}\n"
                f"stderr:\n{proc.stderr}\n"
                f"{self.diagnostics()}"
            )
        return proc

    def run_manager_in_current_pane(
        self,
        *args: str,
        check: bool = True,
        env_overrides: Optional[dict[str, str]] = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {"TMUX_PANE": self.pane_id}
        if env_overrides:
            env.update(env_overrides)
        return self.run_manager(*args, check=check, env_overrides=env)

    def manager_watch_log_for_pane(self, pane_id: str) -> Path:
        key = re.sub(r"[^a-zA-Z0-9._-]", "_", pane_id)
        return self.state_dir / f"auto_continue_logwatch.{key}.log"

    def _start_patchers(self) -> None:
        self._patchers = [
            patch.object(acw, "STATE_DIR", str(self.state_dir)),
            patch.object(acw, "PROJECT_CWD", str(self.repo_root)),
            patch.object(acw, "run_tmux", side_effect=self._run_tmux),
            patch.object(acw, "_build_runtime_snapshot", side_effect=self._build_runtime_snapshot),
            patch.object(acw, "_build_runtime_model", side_effect=self._build_runtime_model),
            patch.object(acw, "ensure_tmux_window_rename_hook", return_value=None),
            patch.object(acw, "_state_dir_is_writable", return_value=(True, str(self.state_dir))),
            patch.object(acw, "_codex_auth_state_available", return_value=(True, str(self.auth_file))),
            patch.object(acw, "_last_agent_snippet_for_pane", side_effect=self._last_agent_snippet_for_pane),
            patch.object(acw, "_thread_times", return_value=("-", "-")),
            patch.object(acw, "_daemon_rpc_upsert_session", side_effect=self._daemon_upsert_session),
            patch.object(acw, "_daemon_rpc_patch_session", side_effect=self._daemon_patch_session),
            patch.object(acw, "_daemon_rpc_remove_session", side_effect=self._daemon_remove_session),
            patch.object(acw, "_daemon_rpc_restart_codex", side_effect=self._daemon_restart_codex),
            patch.object(acw, "_tmux_socket_recovery_hint", side_effect=self._tmux_socket_recovery_hint),
        ]
        for patcher in self._patchers:
            patcher.start()

    def _create_pane(self, window_name: str) -> str:
        pane_id = f"%{self._next_pane_number}"
        self._next_pane_number += 1
        pane = MockPane(
            pane_id=pane_id,
            window_index=self._next_window_index,
            window_name=window_name,
            shell_pid=str(self._next_shell_pid),
        )
        self._next_window_index += 1
        self._next_shell_pid += 1
        self._panes[pane_id] = pane
        self._current_pane = pane_id
        return pane_id

    def _pane(self, pane_id: str) -> MockPane:
        pane = self._panes.get(pane_id)
        if pane is None:
            raise AssertionError(f"unknown pane {pane_id}\n{self.diagnostics()}")
        return pane

    def _new_thread_id(self) -> str:
        self._thread_counter += 1
        return f"019c0000-0000-7000-8000-{self._thread_counter:012x}"

    def _new_runtime_id(self) -> str:
        self._runtime_counter += 1
        return f"{os.getpid()}{self._runtime_counter:06d}"

    def _ensure_daemon_alive(self) -> int:
        if self._daemon_proc is not None and self._daemon_proc.poll() is None:
            return self._daemon_proc.pid
        self.daemon_socket.parent.mkdir(parents=True, exist_ok=True)
        self._daemon_proc = subprocess.Popen(
            ["sleep", "1000"],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.daemon_pid_file.write_text(f"{self._daemon_proc.pid}\n", encoding="utf-8")
        self.daemon_socket.write_text("", encoding="utf-8")
        acw_state.write_daemon_scope_state(
            state_dir=str(self.state_dir),
            scope=self.scope,
            daemon_pid=str(self._daemon_proc.pid),
            socket_path=str(self.daemon_socket),
            heartbeat_ts="2026-03-15 12:00:00",
            started_ts="2026-03-15 12:00:00",
        )
        return self._daemon_proc.pid

    def _stop_daemon(self) -> None:
        if self._daemon_proc is None:
            return
        if self._daemon_proc.poll() is None:
            self._daemon_proc.terminate()
            try:
                self._daemon_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._daemon_proc.kill()
                self._daemon_proc.wait(timeout=5.0)
        if self.daemon_socket.exists():
            self.daemon_socket.unlink()
        if self.daemon_pid_file.exists():
            self.daemon_pid_file.write_text(f"{self._daemon_proc.pid}\n", encoding="utf-8")
        acw_state.write_daemon_scope_state(
            state_dir=str(self.state_dir),
            scope=self.scope,
            daemon_pid=str(self._daemon_proc.pid),
            socket_path=str(self.daemon_socket),
            heartbeat_ts="2026-03-15 12:00:00",
            started_ts="2026-03-15 12:00:00",
        )
        self._daemon_proc = None

    def _daemon_upsert_session(
        self,
        thread_id: str,
        *,
        pane: str,
        name: str,
        message: str,
        paused: bool,
        generation: int,
        extra_session: Optional[dict[str, object]] = None,
        timeout_secs: float = 5.0,
    ) -> dict[str, str]:
        del timeout_secs
        daemon_pid = self._ensure_daemon_alive()
        payload = {
            "thread_id": thread_id.lower(),
            "pane": pane,
            "name": name,
            "message": message,
            "tmux_socket": self.scope,
            "paused": paused,
            "generation": generation,
            "health": "",
            "health_detail": "",
            "health_ts": "",
        }
        if extra_session:
            payload.update(extra_session)
        acw_state.write_session_state(thread_id, payload, state_dir=str(self.state_dir))
        runtime_id = self._new_runtime_id()
        watch_log = self.manager_watch_log_for_pane(pane)
        watch_log.write_text(
            f"watch: pane={pane} thread_id={thread_id.lower()}\n",
            encoding="utf-8",
        )
        acw_state.write_runtime_state(
            thread_id,
            {
                "runtime_id": runtime_id,
                "daemon_pid": str(daemon_pid),
                "pane": pane,
                "state": acw_state.session_state_ref(thread_id),
                "watch": str(watch_log),
                "cwd": str(self.repo_root),
                "msg_inline": message,
                "paused": paused,
                "runtime_status": "paused" if paused else "running",
                "runtime_detail": "",
                "generation": generation,
                "worker_label": runtime_id,
                "heartbeat_ts": "2026-03-15 12:00:00",
            },
            state_dir=str(self.state_dir),
            scope=self.scope,
        )
        return {
            "thread": thread_id.lower(),
            "pane": pane,
            "runtime_status": "paused" if paused else "running",
            "generation": str(generation),
            "paused": "1" if paused else "",
            "runtime_id": runtime_id,
            "daemon_pid": str(daemon_pid),
        }

    def _daemon_patch_session(
        self,
        thread_id: str,
        changes: dict[str, object],
        *,
        wait: Optional[dict[str, object]] = None,
        timeout_secs: float = 5.0,
    ) -> dict[str, str]:
        del timeout_secs
        session = dict(acw_state.read_session_state(thread_id, state_dir=str(self.state_dir)))
        session.update(changes)
        session["thread_id"] = thread_id.lower()
        session["tmux_socket"] = session.get("tmux_socket", self.scope) or self.scope
        acw_state.write_session_state(thread_id, session, state_dir=str(self.state_dir))

        if isinstance(wait, dict) and wait.get("kind") == "absent":
            acw_state.remove_runtime_state(thread_id, state_dir=str(self.state_dir), scope=self.scope)
            return {}

        existing = self.manager_runtime_row(thread_id) or {}
        pane = str(session.get("pane", existing.get("pane", ""))).strip() or str(changes.get("pane", ""))
        daemon_pid = self._ensure_daemon_alive()
        paused = bool(session.get("paused", False))
        statuses = wait.get("statuses", []) if isinstance(wait, dict) else []
        runtime_status = str(statuses[0]).strip() if statuses else ("paused" if paused else "running")
        runtime_id = str(existing.get("runtime_id", "") or existing.get("pid", "")).strip() or self._new_runtime_id()
        watch_log = self.manager_watch_log_for_pane(pane) if pane else self.manager_watch_log_for_pane(self.pane_id)
        if pane:
            watch_log.write_text(
                f"watch: pane={pane} thread_id={thread_id.lower()}\n",
                encoding="utf-8",
            )
        acw_state.write_runtime_state(
            thread_id,
            {
                "runtime_id": runtime_id,
                "daemon_pid": str(daemon_pid),
                "pane": pane,
                "state": acw_state.session_state_ref(thread_id),
                "watch": str(watch_log),
                "cwd": str(self.repo_root),
                "msg_inline": str(session.get("message", "")).strip(),
                "paused": paused,
                "runtime_status": runtime_status,
                "runtime_detail": "",
                "generation": int(session.get("generation", 0) or 0),
                "worker_label": runtime_id,
                "heartbeat_ts": "2026-03-15 12:00:00",
            },
            state_dir=str(self.state_dir),
            scope=self.scope,
        )
        return {
            "thread": thread_id.lower(),
            "pane": pane,
            "runtime_status": runtime_status,
            "generation": str(session.get("generation", 0) or 0),
            "paused": "1" if paused else "",
            "runtime_id": runtime_id,
            "daemon_pid": str(daemon_pid),
        }

    def _daemon_remove_session(self, thread_id: str, *, timeout_secs: float = 2.0) -> None:
        del timeout_secs
        acw_state.remove_runtime_state(thread_id, state_dir=str(self.state_dir), scope=self.scope)
        acw_state.remove_session_state(thread_id, state_dir=str(self.state_dir))

    def _daemon_restart_codex(self, pane: str, thread_id: str, *, timeout_secs: float = 10.0) -> None:
        del timeout_secs
        mock_pane = self._pane(pane)
        mock_pane.thread_id = thread_id.lower()
        mock_pane.has_codex_process = True
        mock_pane.last_command = f"codex resume {thread_id.lower()}"

    def _run_tmux(self, *args: str) -> str | None:
        if not args:
            return None
        if not self._tmux_socket_available:
            return None
        if args[0] == "list-sessions":
            return "mock-session\n"
        if args[:3] == ("display-message", "-p", "-t") and len(args) >= 5:
            pane = self._panes.get(args[3])
            if pane is None:
                return None
            expr = args[4]
            if expr == "#{window_name}":
                return f"{pane.window_name}\n"
            if expr == "#{pane_id}":
                return f"{pane.pane_id}\n"
            if expr == "#{session_name}":
                return "mock-session\n"
            return "\n"
        if args[:3] == ("capture-pane", "-t", self._current_pane) or (len(args) >= 3 and args[0] == "capture-pane" and args[1] == "-t"):
            pane = self._panes.get(args[2])
            if pane is None:
                return None
            return f"• {pane.last_agent}\n"
        if args[:2] == ("list-panes", "-a"):
            fmt = args[-1]
            return self._list_panes_output(fmt)
        if args[:2] == ("list-panes", "-t"):
            target = args[2]
            fmt = args[-1]
            pane = self._panes.get(target)
            if pane is None:
                return None
            if fmt == "#{pane_current_command}\t#{pane_pid}":
                command = "codex" if pane.has_codex_process else "bash"
                return f"{command}\t{pane.shell_pid}\n"
            if fmt == "#{pane_id}":
                return f"{pane.pane_id}\n"
        if args[0] == "send-keys":
            return ""
        return None

    def _list_panes_output(self, fmt: str) -> str:
        rows: list[str] = []
        for pane in sorted(self._panes.values(), key=lambda item: item.window_index):
            pane_active = "1" if pane.pane_id == self._current_pane else "0"
            window_active = pane_active
            if fmt == "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_active}":
                rows.append(f"mock-session\t{pane.window_index}\t{pane.window_name}\t{pane.pane_id}\t{pane_active}")
            elif fmt == "#{session_name}\t#{window_index}\t#{pane_id}\t#{pane_active}\t#{window_active}\t#{pane_index}":
                rows.append(f"mock-session\t{pane.window_index}\t{pane.pane_id}\t{pane_active}\t{window_active}\t0")
            elif fmt == "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_pid}":
                rows.append(f"mock-session\t{pane.window_index}\t{pane.window_name}\t{pane.pane_id}\t{pane.shell_pid}")
            elif fmt == "#{pane_id}\t#{pane_pid}":
                rows.append(f"{pane.pane_id}\t{pane.shell_pid}")
        return "\n".join(rows) + ("\n" if rows else "")

    def _build_runtime_snapshot(self) -> acw.RuntimeSnapshot:
        panes = tuple(
            acw.LivePaneRecord(
                pane_id=pane.pane_id,
                window_label=pane.window_label,
                window_name=pane.window_name,
                shell_pid=pane.shell_pid,
                detected_thread=pane.thread_id.lower(),
                detected_thread_source="pid" if pane.thread_id else "",
                detected_thread_detail="" if pane.thread_id or not pane.has_codex_process else "no live Codex agent detected in pane",
                has_codex_process=pane.has_codex_process,
            )
            for pane in sorted(self._panes.values(), key=lambda item: item.window_index)
        )
        sessions = tuple(
            acw.SavedSessionRecord(
                thread_id=str(row.get("thread_id", "")).lower(),
                name=str(row.get("name", "")),
                message=str(row.get("message", "")),
                pane=str(row.get("pane", "")),
                tmux_socket=str(row.get("tmux_socket", "")),
                paused=bool(row.get("paused", False)),
                generation=int(row.get("generation", 0) or 0),
                state_file=acw_state.session_state_ref(str(row.get("thread_id", ""))),
                health=str(row.get("health", "")),
                health_detail=str(row.get("health_detail", "")),
                health_ts=str(row.get("health_ts", "")),
                last_continue_at=str(row.get("last_continue_at", "")),
            )
            for row in acw_state.load_sessions(state_dir=str(self.state_dir), scope=self.scope)
        )
        watchers = tuple(
            acw.LiveWatcherRecord(
                pid=str(row.get("runtime_id", "") or row.get("pid", "")),
                pane=str(row.get("pane", "")),
                thread_id=str(row.get("thread", "")).lower(),
                state_file=str(row.get("state", "")),
                watch_log=str(row.get("watch", "")),
                cwd=str(row.get("cwd", "")),
                message_inline=str(row.get("msg_inline", "")),
                tmux_socket=str(row.get("tmux_socket", "")),
                paused=str(row.get("paused", "")) == "1",
                runtime_status=str(row.get("runtime_status", "")),
                runtime_detail=str(row.get("runtime_detail", "")),
                generation=int(row.get("generation", 0) or 0),
                worker_label=str(row.get("worker_label", "")),
            )
            for row in acw_state.read_runtime_rows(state_dir=str(self.state_dir), scope=self.scope)
        )
        return acw.RuntimeSnapshot(panes=panes, watchers=watchers, sessions=sessions)

    def _build_runtime_model(
        self,
        snapshot: Optional[acw.RuntimeSnapshot] = None,
    ) -> acw.RuntimeModel:
        return acw_runtime.build_runtime_model(snapshot or self._build_runtime_snapshot())

    def _last_agent_snippet_for_pane(self, pane: str, thread_id: str = "") -> str:
        del thread_id
        record = self._panes.get(pane)
        return record.last_agent if record else ""

    def _prompt_result(self, prompt: str) -> str:
        exact = re.search(r"say exactly ([A-Za-z0-9_:-]+) and nothing else", prompt)
        if exact:
            return exact.group(1)
        word = re.search(r"say the word ([A-Za-z0-9_:-]+) and nothing else", prompt)
        if word:
            return word.group(1)
        return prompt.strip().splitlines()[0][:120] if prompt.strip() else "-"

    def _tmux_socket_recovery_hint(self) -> str:
        if self._tmux_socket_available:
            return ""
        return (
            f"hint: tmux socket '{self.tmux_socket}' is unreachable; "
            "recreate it with `kill -USR1 424242`"
        )

    def _wait_for(self, predicate, *, timeout: float, description: str) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {description}\n{self.diagnostics()}")

    def _wait_for_value(self, probe, *, timeout: float, description: str):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = probe()
            if value is not None:
                return value
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {description}\n{self.diagnostics()}")
