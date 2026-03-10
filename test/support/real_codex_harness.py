from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


THREAD_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
SHELL_SNAPSHOT_RE = re.compile(r"session_init:shell_snapshot\{thread_id=([0-9a-fA-F\-]+)\}")
POST_SAMPLING_RE = re.compile(
    r"session_loop\{thread_id=([0-9a-fA-F\-]+)\}.*post sampling token usage "
    r"turn_id=([^ ]+).*needs_follow_up=false"
)
TASK_CLOSE_RE = re.compile(
    r"session_loop\{thread_id=([0-9a-fA-F\-]+)\}.*"
    r"turn\{[^}]*turn.id=([^ ]+)[^}]*\}: codex_core::tasks: close\b"
)


@dataclass
class TurnObservation:
    thread_id: str
    sources: set[str]


def real_codex_tests_enabled() -> bool:
    return os.environ.get("AUTO_CONTINUE_RUN_REAL_CODEX_TESTS") == "1"


def require_real_codex_prereqs() -> Optional[str]:
    if not real_codex_tests_enabled():
        return "set AUTO_CONTINUE_RUN_REAL_CODEX_TESTS=1 to run real Codex integration tests"
    for binary in ("tmux", "codex"):
        if shutil.which(binary) is None:
            return f"{binary} is required for real Codex integration tests"
    return None


def _repo_env_candidates(repo_root: Path) -> list[Path]:
    return [
        repo_root / ".env.local",
        repo_root / ".env",
        Path.cwd() / ".env.local",
        Path.cwd() / ".env",
    ]


def load_real_codex_env(repo_root: Path) -> tuple[dict[str, str], Optional[Path]]:
    env = dict(os.environ)
    env_file = env.get("AUTO_CONTINUE_E2E_ENV_FILE", "")
    candidate_path: Optional[Path] = None
    if env_file:
        candidate_path = Path(env_file).expanduser()
    else:
        for candidate in _repo_env_candidates(repo_root):
            if candidate.is_file():
                candidate_path = candidate
                break

    if candidate_path is None:
        return env, None
    if not candidate_path.is_file():
        raise FileNotFoundError(f"env file not found: {candidate_path}")

    script = f"set -a; source {shlex.quote(str(candidate_path))}; env -0"
    proc = subprocess.run(
        ["bash", "-lc", script],
        env=env,
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"failed to source {candidate_path}: {stderr}")

    loaded: dict[str, str] = {}
    for entry in proc.stdout.decode("utf-8", errors="ignore").split("\0"):
        if not entry or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        loaded[key] = value
    return loaded, candidate_path


def _completion_sources_for_thread(lines: list[str], thread_id: str) -> set[str]:
    sources: set[str] = set()
    for line in lines:
        post = POST_SAMPLING_RE.search(line)
        if post and post.group(1) == thread_id:
            sources.add("codex_log_post_sampling")
        close = TASK_CLOSE_RE.search(line)
        if close and close.group(1) == thread_id:
            sources.add("codex_log_task_close")
    return sources


def _rollout_sources_for_thread(home_dir: Path, thread_id: str) -> set[str]:
    sources: set[str] = set()
    sessions_dir = home_dir / ".codex" / "sessions"
    if not sessions_dir.is_dir():
        return sources
    for path in sessions_dir.glob("*/*/*/rollout-*.jsonl"):
        if thread_id.lower() not in path.name.lower():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    obj = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if obj.get("type") != "event_msg":
                    continue
                payload = obj.get("payload")
                if isinstance(payload, dict) and payload.get("type") == "task_complete":
                    sources.add("rollout_task_complete")
                    return sources
        except OSError:
            continue
    return sources


def _copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if dst.is_dir():
        dst.chmod(0o700)


class RealCodexHarness:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.temp_root = Path(
            os.environ.get(
                "AUTO_CONTINUE_E2E_TMPDIR",
                str(Path.home() / ".codex" / "auto-continue-e2e-tmp"),
            )
        )
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.base_env, self.env_file = load_real_codex_env(repo_root)
        self._tmpdir = tempfile.TemporaryDirectory(prefix="real-codex-", dir=str(self.temp_root))
        self.root = Path(self._tmpdir.name)
        self.root.chmod(0o700)
        self.isolated_home = self.env_file is not None
        if self.isolated_home:
            self.home_dir = self.root / "home"
            self.home_dir.mkdir(parents=True, exist_ok=True)
            self.home_dir.chmod(0o700)
            self.codex_dir = self.home_dir / ".codex"
            self.codex_dir.mkdir(parents=True, exist_ok=True)
            self.codex_dir.chmod(0o700)
            self.log_dir = self.codex_dir / "log"
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.log_dir.chmod(0o700)
            self.sessions_dir = self.codex_dir / "sessions"
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            self.sessions_dir.chmod(0o700)
            self.shell_snapshot_dir = self.codex_dir / "shell_snapshots"
            self.shell_snapshot_dir.mkdir(parents=True, exist_ok=True)
            self.shell_snapshot_dir.chmod(0o700)
        else:
            self.home_dir = Path.home()
            self.codex_dir = self.home_dir / ".codex"
            self.log_dir = self.codex_dir / "log"
            self.sessions_dir = self.codex_dir / "sessions"
            self.shell_snapshot_dir = self.codex_dir / "shell_snapshots"
        self.project_cwd = self.root / "project"
        self.project_cwd.mkdir(parents=True, exist_ok=True)
        self.project_cwd.chmod(0o700)
        self.state_dir = self.project_cwd / ".codex"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.tmux_socket_dir = self.root / "tmux"
        self.tmux_socket_dir.mkdir(parents=True, exist_ok=True)
        self.tmux_socket_dir.chmod(0o700)
        self.tmux_socket = self.tmux_socket_dir / "tmux.sock"
        self.session_name = f"real-codex-{uuid.uuid4().hex[:10]}"
        self.codex_log = self.log_dir / "codex-tui.log"
        self.watcher_proc: Optional[subprocess.Popen[str]] = None
        self.pane_id = ""
        self._codex_log_baseline = 0
        self._seed_codex_auth_state()

    @property
    def process_env(self) -> dict[str, str]:
        env = dict(self.base_env)
        env["HOME"] = str(self.home_dir)
        env["AUTO_CONTINUE_TMUX_SOCKET"] = str(self.tmux_socket)
        return env

    @property
    def watch_log(self) -> Path:
        if not self.pane_id:
            raise RuntimeError("pane not initialized")
        pane_key = re.sub(r"[^a-zA-Z0-9._-]", "_", self.pane_id)
        return self.state_dir / f"auto_continue_logwatch.{pane_key}.log"

    def start(self) -> None:
        self.tmux("new-session", "-d", "-s", self.session_name, "-x", "200", "-y", "50")
        panes = self.tmux_stdout("list-panes", "-t", self.session_name, "-F", "#{pane_id}")
        self.pane_id = panes.splitlines()[0]
        self._codex_log_baseline = self._line_count(self.codex_log)

    def cleanup(self) -> None:
        if self.watcher_proc is not None:
            if self.watcher_proc.poll() is None:
                self.watcher_proc.terminate()
                try:
                    self.watcher_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.watcher_proc.kill()
                    self.watcher_proc.wait(timeout=5)
            for pipe in (self.watcher_proc.stdout, self.watcher_proc.stderr):
                if pipe is not None:
                    pipe.close()
        try:
            self.tmux("kill-session", "-t", self.session_name, check=False)
            self.tmux("kill-server", check=False)
        finally:
            self._tmpdir.cleanup()

    def tmux(self, *args: str, check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["tmux", "-S", str(self.tmux_socket), *args],
            env=self.process_env,
            text=True,
            capture_output=capture_output,
            check=False,
            preexec_fn=lambda: os.umask(0o077),
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(f"tmux {' '.join(args)} failed: {detail}")
        return proc

    def tmux_stdout(self, *args: str) -> str:
        return self.tmux(*args).stdout.strip()

    def send_keys(self, *keys: str, literal: bool = False) -> None:
        cmd = ["send-keys", "-t", self.pane_id]
        if literal:
            cmd.append("-l")
        cmd.extend(keys)
        self.tmux(*cmd)

    def start_codex(self, prompt: str) -> None:
        self.send_keys(f"codex {shlex.quote(prompt)}", "C-m")

    def send_codex_prompt(self, prompt: str) -> None:
        self.send_keys(prompt, literal=True)
        time.sleep(0.2)
        self.send_keys("C-m")

    def start_watcher(self, thread_id: str, message: str = "test continue") -> Path:
        state_file = self.state_dir / f"acw_session.{thread_id}.json"
        cmd = [
            "python3",
            str(self.repo_root / "bin" / "auto_continue_logwatch.py"),
            "--cwd",
            str(self.project_cwd),
            "--pane",
            self.pane_id,
            "--thread-id",
            thread_id,
            "--message",
            message,
            "--cooldown-secs",
            "0.5",
            "--send-delay-secs",
            "0.1",
            "--enter-delay-secs",
            "0.1",
            "--state-file",
            str(state_file),
            "--watch-log",
            str(self.watch_log),
        ]
        self.watch_log.parent.mkdir(parents=True, exist_ok=True)
        self.watcher_proc = subprocess.Popen(
            cmd,
            env=self.process_env,
            cwd=str(self.repo_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=lambda: os.umask(0o077),
        )
        return state_file

    def wait_for_watcher_started(self, thread_id: str, timeout: float = 15.0) -> None:
        self._wait_for(
            lambda: self.watch_log.is_file()
            and f"watch: pane={self.pane_id} thread_id={thread_id}"
            in self.watch_log.read_text(encoding="utf-8", errors="ignore"),
            timeout=timeout,
            description="watcher startup",
        )

    def wait_for_continue_sent(self, timeout: float = 90.0) -> str:
        def _ready() -> bool:
            self._assert_watcher_alive()
            return self.watch_log.is_file() and "continue: sent" in self.watch_log.read_text(
                encoding="utf-8",
                errors="ignore",
            )

        self._wait_for(_ready, timeout=timeout, description="watcher continue send")
        return self.watch_log.read_text(encoding="utf-8", errors="ignore")

    def wait_for_first_completed_turn(self, timeout: float = 60.0) -> TurnObservation:
        def _probe() -> Optional[TurnObservation]:
            lines = self.new_codex_log_lines()
            thread_id = self._discover_thread_id(lines)
            if not thread_id:
                return None
            sources = _completion_sources_for_thread(lines, thread_id)
            sources.update(_rollout_sources_for_thread(self.home_dir, thread_id))
            if not sources:
                return None
            return TurnObservation(thread_id=thread_id, sources=sources)

        return self._wait_for_value(_probe, timeout=timeout, description="first completed Codex turn")

    def capture_pane(self, lines: int = 80) -> str:
        return self.tmux_stdout("capture-pane", "-t", self.pane_id, "-p", "-S", f"-{lines}")

    def recent_codex_log(self, lines: int = 120) -> str:
        if not self.codex_log.is_file():
            return ""
        content = self.codex_log.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(content[-lines:])

    def new_codex_log_lines(self) -> list[str]:
        if not self.codex_log.is_file():
            return []
        lines = self.codex_log.read_text(encoding="utf-8", errors="ignore").splitlines()
        return lines[self._codex_log_baseline :]

    def diagnostics(self) -> str:
        parts = [
            f"env_file={self.env_file}" if self.env_file else "env_file=(none)",
            f"home_dir={self.home_dir}",
            f"pane_id={self.pane_id}",
            f"codex_log={self.codex_log}",
            "=== watch log ===",
            self.watch_log.read_text(encoding="utf-8", errors="ignore") if self.watch_log.is_file() else "(none)",
            "=== recent codex log ===",
            self.recent_codex_log(),
            "=== pane capture ===",
            self.capture_pane(),
        ]
        if self.watcher_proc is not None:
            watcher_rc = self.watcher_proc.poll()
            parts.extend(
                [
                    f"watcher_rc={watcher_rc}",
                    "=== watcher stderr ===",
                    self._read_pipe(self.watcher_proc.stderr) if watcher_rc is not None else "(watcher still running)",
                    "=== watcher stdout ===",
                    self._read_pipe(self.watcher_proc.stdout) if watcher_rc is not None else "(watcher still running)",
                ]
            )
        return "\n".join(parts)

    def _read_pipe(self, pipe) -> str:
        if pipe is None:
            return ""
        try:
            return pipe.read()
        except Exception:
            return ""

    def _seed_codex_auth_state(self) -> None:
        if not self.isolated_home:
            return
        real_codex_dir = Path.home() / ".codex"
        for name in ("auth.json", "config.toml", "version.json", "models_cache.json"):
            _copy_if_exists(real_codex_dir / name, self.codex_dir / name)
        rules_dir = real_codex_dir / "rules"
        if rules_dir.is_dir():
            target = self.codex_dir / "rules"
            shutil.copytree(rules_dir, target, dirs_exist_ok=True)
            for path in [target, *target.rglob("*")]:
                if path.is_dir():
                    path.chmod(0o700)

    def _discover_thread_id(self, lines: list[str]) -> str:
        for line in reversed(lines):
            match = TASK_CLOSE_RE.search(line)
            if match:
                return match.group(1)
            match = POST_SAMPLING_RE.search(line)
            if match:
                return match.group(1)
        for line in lines:
            match = SHELL_SNAPSHOT_RE.search(line)
            if match:
                return match.group(1)
        sessions_dir = self.home_dir / ".codex" / "sessions"
        if sessions_dir.is_dir():
            for path in sorted(sessions_dir.glob("*/*/*/rollout-*.jsonl")):
                match = THREAD_ID_RE.search(path.name)
                if match:
                    return match.group(0)
        return ""

    def _line_count(self, path: Path) -> int:
        if not path.is_file():
            return 0
        return len(path.read_text(encoding="utf-8", errors="ignore").splitlines())

    def _assert_watcher_alive(self) -> None:
        if self.watcher_proc is None:
            raise RuntimeError("watcher process not started")
        if self.watcher_proc.poll() is not None:
            raise AssertionError(f"watcher exited early\n{self.diagnostics()}")

    def _wait_for(self, predicate, timeout: float, description: str) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(1.0)
        raise AssertionError(f"timed out waiting for {description}\n{self.diagnostics()}")

    def _wait_for_value(self, callback, timeout: float, description: str):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = callback()
            if value is not None:
                return value
            time.sleep(1.0)
        raise AssertionError(f"timed out waiting for {description}\n{self.diagnostics()}")
