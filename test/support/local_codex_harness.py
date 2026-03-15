from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

from test.support.real_codex_harness import RealCodexHarness


def local_codex_tests_enabled() -> bool:
    return os.environ.get("AUTO_CONTINUE_RUN_LOCAL_CODEX_TESTS") == "1"


def require_local_codex_prereqs() -> str | None:
    if not local_codex_tests_enabled():
        return "set AUTO_CONTINUE_RUN_LOCAL_CODEX_TESTS=1 to run local Codex integration tests"
    if not sys.platform.startswith("linux"):
        return "local Codex integration tests require Linux /proc support"
    if not os.access("/proc/self/comm", os.R_OK):
        return "local Codex integration tests require /proc comm support"
    from shutil import which

    if which("tmux") is None:
        return "tmux is required for local Codex integration tests"
    if which("python3") is None:
        return "python3 is required for local Codex integration tests"
    return None


class LocalCodexHarness(RealCodexHarness):
    def __init__(self, repo_root: Path):
        super().__init__(repo_root)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.bin_dir.chmod(0o700)
        self._install_fake_codex()

    @property
    def process_env(self) -> dict[str, str]:
        env = super().process_env
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        return env

    def _seed_codex_auth_state(self) -> None:
        self.codex_dir.mkdir(parents=True, exist_ok=True)
        self.codex_dir.chmod(0o700)
        for name, content in {
            "auth.json": "{}\n",
            "config.toml": "",
            "version.json": "{}\n",
            "models_cache.json": "{}\n",
        }.items():
            path = self.codex_dir / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o600)

    def _install_fake_codex(self) -> None:
        wrapper = self.bin_dir / "codex"
        target = self.repo_root / "test" / "support" / "fake_codex.py"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"exec python3 {target} \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    def _maybe_accept_directory_trust_prompt(self, timeout: float = 15.0) -> None:
        return None
