from __future__ import annotations
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"


@dataclass
class Config:
    codex_bin: str = DEFAULT_BIN
    codex_home: str = str(Path.home() / ".codex")
    data_dir: str = str(Path.home() / ".codex-resume")
    port: int = 8765
    weekly_guard: int = 90
    max_attempts: int = 3
    approvals: str = "guardian"  # "guardian" or "decline"
    default_message: str = "continue"
    grace_seconds: int = 60
    tick_seconds: int = 60
    desktop_wait_seconds: int = 90
    turn_timeout_seconds: int = 14400
    retry_cooldown_seconds: int = 600

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        cfg = cls()
        p = Path(path) if path else Path(cfg.data_dir) / "config.json"
        if p.exists():
            for key, value in json.loads(p.read_text()).items():
                if key in cfg.__dataclass_fields__:
                    setattr(cfg, key, value)
        if not Path(cfg.codex_bin).exists():
            found = shutil.which("codex")
            if found:
                cfg.codex_bin = found
        return cfg
