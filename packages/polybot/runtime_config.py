"""Runtime-mutable settings via Redis overrides.

Two-layer pattern (yaml = shipped baseline, Redis = live overrides):

    runtime_value = deep_merge(
        yaml_config.<scope>_cfg.get(mode),    # baseline + per-mode profile
        get_overrides("<scope>", mode),       # dashboard-driven overrides
    )

Scopes: "risk", "categories", "gates", "mode".

Why Redis and not the YAML file:
  - No TTL latency for the dashboard's "I just toggled this" feedback loop.
  - No race conditions when two operators edit at once (Redis is the
    serialisation point; YAML on Docker volumes is not).
  - Overrides ARE the audit-log payload — every set call writes to
    audit_log with actor + payload diff for forensic visibility.
  - YAML stays the canonical "checked into git" baseline that survives a
    full reset (`FLUSHDB` clears overrides → bot reverts to YAML).

Override keys:
    polybot:overrides:risk:{mode}        → JSON dict, merged into risk_cfg(mode)
    polybot:overrides:categories:{mode}  → JSON dict, merged into categories_cfg(mode)
    polybot:overrides:gates:{mode}       → JSON dict, merged into gates_cfg(mode)
    polybot:overrides:mode               → "paper" | "live" (current effective mode)

Notes:
  - Overrides are NOT scoped per-user; this is single-admin.
  - Overrides survive container restarts because they're in Redis AOF
    (volumes/redis-data is persistent).
  - The merge is a SHALLOW per-key dict-update — nested dicts get
    replaced wholesale (not deep-merged) by design. Nested dicts in our
    config are rare (only `position.*`, `execution.*`) and we want
    "patch X.Y" to NOT silently inherit the unspecified siblings of X.Y.
    Callers that want deep-patch must read-modify-write at the dict root.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from polybot.config import settings
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import AuditLog
from polybot.redis_bus import client as _redis
from polybot.yaml_config import categories_cfg, gates_cfg, risk_cfg

log = get_logger(__name__)

_OVERRIDE_KEY = "polybot:overrides:{scope}:{mode}"
_MODE_KEY = "polybot:overrides:mode"
_MODES_SET_KEY = "polybot:overrides:enabled_modes"


async def enabled_modes() -> set[str]:
    """Set of currently-active trading modes.

    Returns one of:
      {"paper"}        – default; pure paper-trading.
      {"paper", "live"} – PARALLEL mode: every gate-passing signal produces
                         one paper Fill row AND one live Fill row, with
                         their own independent risk preflight + caps. Lets
                         the operator run a live experiment without losing
                         the paper control group.
      {"live"}         – live-only (rare; the paper shadow is usually worth
                         keeping for comparison).

    Source of truth: Redis JSON array at `polybot:overrides:enabled_modes`.
    Falls back to legacy `current_mode()` for backward compat when the
    set hasn't been initialised — so existing deployments switch to the
    new semantics seamlessly.
    """
    raw = await _redis().get(_MODES_SET_KEY)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                cleaned = {m for m in data if m in ("paper", "live")}
                if cleaned:
                    return cleaned
        except json.JSONDecodeError:
            log.warning("runtime_config_bad_enabled_modes_repairing", raw=raw)
            # Self-repair: a corrupted JSON value would otherwise wedge
            # the system in legacy mode forever. Delete + fall through
            # to the legacy derivation so a sane default is re-derived.
            try:
                await _redis().delete(_MODES_SET_KEY)
            except Exception:  # noqa: BLE001
                pass
    # Backward compat: derive from the single-mode key.
    legacy = await current_mode()
    return {legacy}


async def set_enabled_modes(modes: set[str] | list[str], *, actor: str = "admin") -> set[str]:
    """Atomically replace the active mode set.

    Raises if the set is empty or contains unknown values.
    Writes an AuditLog entry with the old/new diff.
    """
    cleaned = {m for m in modes if m in ("paper", "live")}
    if not cleaned:
        raise ValueError("enabled_modes must contain at least one of paper, live")
    old = await enabled_modes()
    await _redis().set(_MODES_SET_KEY, json.dumps(sorted(cleaned)))
    await _audit(
        "enabled_modes_changed",
        {"old": sorted(old), "new": sorted(cleaned)},
        actor=actor,
    )
    log.info("runtime_config_enabled_modes_changed",
             old=sorted(old), new=sorted(cleaned), actor=actor)
    return cleaned


async def current_mode() -> str:
    """The PRIMARY trading mode for backward compat with code that needs
    one canonical mode string.

    Resolution order:
      1. Explicit single-mode override at `polybot:overrides:mode`
         (legacy — set by old /admin/settings/mode endpoint).
      2. `enabled_modes` set: if "live" is in the set, returns "live";
         otherwise "paper". So a parallel paper+live deployment still
         reports "live" to dashboards that show a single mode badge.
      3. Boot-time `settings.trading_mode` env.
    """
    raw = await _redis().get(_MODE_KEY)
    if raw and raw in ("paper", "live"):
        return raw
    # No legacy override — derive from the modes set if it exists.
    modes_raw = await _redis().get(_MODES_SET_KEY)
    if modes_raw:
        try:
            data = json.loads(modes_raw)
            if isinstance(data, list):
                if "live" in data:
                    return "live"
                if "paper" in data:
                    return "paper"
        except json.JSONDecodeError:
            pass
    return settings.trading_mode


async def set_mode(mode: str, *, actor: str = "admin") -> None:
    """Legacy single-mode switch.

    Kept for backward compat with the existing /admin/settings/mode
    endpoint. ALSO MERGES into `enabled_modes` rather than replacing it,
    so an operator who hits POST /mode {paper} no longer silently
    clobbers a running parallel {paper, live} configuration. The
    PARALLEL set is canonical now — this just adds/keeps the requested
    mode on. To DISABLE a mode, use PATCH /mode/enabled.
    """
    if mode not in ("paper", "live"):
        raise ValueError(f"bad mode: {mode!r}")
    old = await current_mode()
    await _redis().set(_MODE_KEY, mode)
    # Merge — don't replace — so legacy callers don't drop a running
    # live shadow. The PATCH /mode/enabled endpoint is the explicit
    # way to turn something off.
    current_set = await enabled_modes()
    merged = current_set | {mode}
    await _redis().set(_MODES_SET_KEY, json.dumps(sorted(merged)))
    await _audit("mode_switched",
                 {"old": old, "new": mode, "merged_enabled": sorted(merged)},
                 actor=actor)
    log.info("runtime_config_mode_changed", old=old, new=mode,
             enabled=sorted(merged), actor=actor)


async def get_overrides(scope: str, mode: str | None = None) -> dict[str, Any]:
    """Return the Redis override payload for `scope` (risk|categories|gates).

    `mode` defaults to the current effective mode. Returns {} on missing
    key or bad JSON.
    """
    mode = mode or await current_mode()
    raw = await _redis().get(_OVERRIDE_KEY.format(scope=scope, mode=mode))
    if not raw:
        return {}
    try:
        return json.loads(raw) or {}
    except json.JSONDecodeError:
        log.warning("runtime_config_bad_json", scope=scope, mode=mode)
        return {}


async def set_overrides(
    scope: str, patch: dict[str, Any], *, actor: str = "admin", mode: str | None = None
) -> dict[str, Any]:
    """Shallow-merge `patch` into the existing override dict for (scope, mode).

    Returns the new effective override dict. Writes an AuditLog entry
    with the diff so the dashboard's "what changed and by whom" view has
    real data.
    """
    if scope not in ("risk", "categories", "gates"):
        raise ValueError(f"bad scope: {scope!r}")
    mode = mode or await current_mode()
    current = await get_overrides(scope, mode)
    new = {**current, **patch}
    await _redis().set(
        _OVERRIDE_KEY.format(scope=scope, mode=mode),
        json.dumps(new, default=str),
    )
    await _audit(
        "settings_changed",
        {"scope": scope, "mode": mode, "patch": patch, "before": current, "after": new},
        actor=actor,
    )
    log.info(
        "runtime_config_overrides_set",
        scope=scope, mode=mode, patch_keys=list(patch), actor=actor,
    )
    return new


async def clear_overrides(scope: str, *, actor: str = "admin", mode: str | None = None) -> None:
    if scope not in ("risk", "categories", "gates"):
        raise ValueError(f"bad scope: {scope!r}")
    mode = mode or await current_mode()
    await _redis().delete(_OVERRIDE_KEY.format(scope=scope, mode=mode))
    await _audit("settings_cleared", {"scope": scope, "mode": mode}, actor=actor)


# ---- Merged read-path (drop-in replacement for *_cfg.get()) ----------------


def _shallow_merge(base: dict, patch: dict) -> dict:
    out = {**base}
    for k, v in patch.items():
        out[k] = v
    return out


async def merged_risk(mode: str | None = None) -> dict[str, Any]:
    mode = mode or await current_mode()
    base = risk_cfg.get(mode=mode)
    overrides = await get_overrides("risk", mode)
    return _shallow_merge(base, overrides)


async def merged_categories(mode: str | None = None) -> dict[str, Any]:
    """Returns the same shape as `categories_cfg.get()['categories']` —
    i.e. a dict of `{name: {tags, enabled, top_n, min_win_rate}}`.

    Override patch can:
      - Toggle existing: `{"crypto": {"enabled": false}}`
      - Update params:   `{"crypto": {"top_n": 50}}`
      - Add new:         `{"newcat": {"tags": ["foo"], "enabled": true, ...}}`
      - Remove:          override with `{"enabled": false}` (we don't hard-delete)

    For nested dicts we do a per-category merge: existing tags/params
    preserved unless explicitly patched.
    """
    mode = mode or await current_mode()
    # Triple-verify HIGH-4: was `(risk_cfg.get() if False else categories_cfg.get())`
    # — leftover debugging artifact. Replaced with the obvious direct call.
    base = (categories_cfg.get(mode=mode) or {}).get("categories", {}) or {}
    overrides = await get_overrides("categories", mode)
    out: dict[str, Any] = {k: {**v} for k, v in base.items()}
    for name, patch in overrides.items():
        if name in out:
            out[name] = {**out[name], **patch}
        else:
            out[name] = patch
    return out


async def merged_gates(mode: str | None = None) -> dict[str, Any]:
    mode = mode or await current_mode()
    base = gates_cfg.get(mode=mode)
    overrides = await get_overrides("gates", mode)
    return _shallow_merge(base, overrides)


# ---- Audit ----------------------------------------------------------------


async def _audit(event: str, payload: dict[str, Any], *, actor: str) -> None:
    try:
        async with session_scope() as s:
            s.add(AuditLog(
                ts=datetime.now(tz=timezone.utc),
                actor=actor, event=event, payload=payload,
            ))
    except Exception:  # noqa: BLE001
        log.exception("runtime_config_audit_failed", event=event)
