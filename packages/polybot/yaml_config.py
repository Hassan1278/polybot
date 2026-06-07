"""Hot-reloading YAML config reader with per-mode profile support.

Reads files in /app/config or REPO_ROOT/config. Each config can have an
optional `modes:` block alongside `defaults:` (or just be flat at the
top level for back-compat):

    # Mode-aware example (config/risk.yaml):
    defaults:
      position:
        max_position_usdc: 25
        max_open_positions: 200
    modes:
      paper:
        position:
          max_position_usdc: 25
      live:
        position:
          max_position_usdc: 50
          max_open_positions: 30
        drawdown:
          max_daily_loss_usdc: 100

    # Flat (legacy) — also still supported:
    position:
      max_position_usdc: 25

`HotConfig.get(mode=None)` resolves like:
  1. Read file (cached for `ttl_seconds`).
  2. If the file has a `defaults` and/or `modes` section, deep-merge:
     `defaults <- modes[mode]`. Otherwise return the file as-is.
  3. If `mode` is None, fall back to `settings.trading_mode`.

This keeps every existing caller working (`cfg.get()` still returns the
old shape because most files don't have a `modes:` section yet) and lets
us opt files into per-mode as we touch them.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

import yaml

from polybot.config import REPO_ROOT, settings

CONFIG_DIR = (REPO_ROOT / "config").resolve()


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge. Lists are replaced, scalars are replaced,
    dicts are merged. patch wins on conflict."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class HotConfig:
    def __init__(self, filename: str, *, ttl_seconds: float = 30.0):
        self.path = CONFIG_DIR / filename
        self.ttl = ttl_seconds
        self._raw: dict[str, Any] = {}
        self._loaded_at: float = 0.0

    def _read(self) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._loaded_at > self.ttl or not self._raw:
            self._raw = yaml.safe_load(self.path.read_text()) or {}
            self._loaded_at = now
        return self._raw

    def get(self, mode: str | None = None) -> dict[str, Any]:
        """Effective config for the given mode.

        Backward-compatible: callers that don't pass `mode` get the same
        flat shape they used to (mode=None → settings.trading_mode, but
        if the file has no `modes:` section we just return it flat).
        """
        raw = self._read()
        if "defaults" not in raw and "modes" not in raw:
            return raw  # flat / legacy format — no mode resolution
        effective_mode = mode or settings.trading_mode
        defaults = raw.get("defaults") or {}
        per_mode = (raw.get("modes") or {}).get(effective_mode) or {}
        return _deep_merge(copy.deepcopy(defaults), per_mode)


categories_cfg = HotConfig("categories.yaml", ttl_seconds=300.0)
gates_cfg = HotConfig("gates.yaml", ttl_seconds=60.0)
risk_cfg = HotConfig("risk.yaml", ttl_seconds=30.0)
