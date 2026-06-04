"""Hot-reloading YAML config reader. Reads files in /app/config or REPO_ROOT/config."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from polybot.config import REPO_ROOT

CONFIG_DIR = (REPO_ROOT / "config").resolve()


class HotConfig:
    def __init__(self, filename: str, *, ttl_seconds: float = 30.0):
        self.path = CONFIG_DIR / filename
        self.ttl = ttl_seconds
        self._data: dict[str, Any] = {}
        self._loaded_at: float = 0.0

    def get(self) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._loaded_at > self.ttl or not self._data:
            self._data = yaml.safe_load(self.path.read_text()) or {}
            self._loaded_at = now
        return self._data


categories_cfg = HotConfig("categories.yaml", ttl_seconds=300.0)
gates_cfg = HotConfig("gates.yaml", ttl_seconds=60.0)
risk_cfg = HotConfig("risk.yaml", ttl_seconds=30.0)
