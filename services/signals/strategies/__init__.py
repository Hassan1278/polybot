"""Pluggable signal strategies.

A SignalStrategy turns a window of recent tracked-wallet trades into a list
of candidate trade-ideas that downstream gates evaluate. The strategy
abstraction lets the operator swap out the "smart-money mirror" with a
"whale follower" or future ML model without touching correlation_loop,
the gate chain, the executor, or any persistence code.

Selection: env var `SIGNAL_STRATEGY` (default `smart_money_mirror`)
maps to one of the entries in `_REGISTRY`. To add a new strategy:

    1. Subclass SignalStrategy in services/signals/strategies/<name>.py
    2. Register it: `_REGISTRY["<name>"] = MyStrategy`

See base.py for the protocol and Candidate dataclass.
"""

from __future__ import annotations

import os

from polybot.logging import get_logger

from services.signals.strategies.base import Candidate, SignalStrategy
from services.signals.strategies.smart_money_mirror import SmartMoneyMirror
from services.signals.strategies.whale_follower import WhaleFollower

log = get_logger(__name__)

# Registry maps env-var-friendly name → constructor.
# New strategies plug in here without touching correlation_loop or engine.
_REGISTRY: dict[str, type[SignalStrategy]] = {
    "smart_money_mirror": SmartMoneyMirror,
    "whale_follower": WhaleFollower,
}


def load_strategy(name: str | None = None) -> SignalStrategy:
    """Resolve the configured strategy. Falls back to the default
    smart-money-mirror on bad config so the bot doesn't crash on a typo —
    just logs a warning."""
    name = (name or os.environ.get("SIGNAL_STRATEGY") or "smart_money_mirror").strip()
    cls = _REGISTRY.get(name)
    if cls is None:
        log.warning(
            "strategy_unknown",
            requested=name,
            available=list(_REGISTRY),
            falling_back_to="smart_money_mirror",
        )
        cls = SmartMoneyMirror
    inst = cls()
    log.info("strategy_loaded", strategy=inst.name)
    return inst


__all__ = ["Candidate", "SignalStrategy", "load_strategy"]
