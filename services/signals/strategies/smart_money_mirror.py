"""Smart-money-mirror strategy.

This is the strategy that's been running since day one — wraps the
existing `polybot.stats.cluster_active_wallets` (kept as the algorithmic
implementation) inside the SignalStrategy adapter.

Why a thin wrapper rather than moving the math?
  - cluster_active_wallets is also used by `compute_correlations.py` and
    might be reused by future analytics. Don't fork the math.
  - This file is the SOLID boundary: gates + executor talk to
    SignalStrategy, the actual algorithm stays where it is.

Behaviour: scan a 30-minute window of tracked-wallet trades, cluster by
(market_id, outcome, side), score by wallet-count × notional × time-decay,
emit one Candidate per cluster that passes min_wallets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from polybot.logging import get_logger
from polybot.stats import cluster_active_wallets
from services.signals.strategies.base import Candidate, SignalStrategy

if TYPE_CHECKING:
    import pandas as pd

log = get_logger(__name__)


class SmartMoneyMirror(SignalStrategy):
    name = "smart_money_mirror"

    async def generate_candidates(
        self,
        recent_trades: "pd.DataFrame",
        **knobs: Any,
    ) -> list[Candidate]:
        if recent_trades is None or recent_trades.empty:
            return []
        try:
            raw = cluster_active_wallets(
                recent_trades,
                window_minutes=knobs.get("window_minutes", 30),
                min_wallets=knobs.get("min_wallets", 3),
                half_life_seconds=knobs.get("half_life_seconds", 300.0),
                k_wallets=knobs.get("k_wallets", 2.5),
                k_notional=knobs.get("k_notional", 2000.0),
            )
        except Exception:  # noqa: BLE001
            log.exception("smart_money_mirror_cluster_failed")
            return []

        out: list[Candidate] = []
        for c in raw:
            # cluster_active_wallets emits "correlation_score" (not "score") —
            # an earlier key-mismatch silently set score=0.0 on every
            # candidate, which made the correlation_score gate fail every
            # signal with `score=0.000<0.05`. Read the canonical key.
            try:
                out.append(Candidate(
                    market_id=c["market_id"],
                    outcome=c.get("outcome", "YES"),
                    side=c["side"],
                    score=float(c.get("correlation_score", c.get("score", 0.0))),
                    avg_price=float(c.get("avg_price", 0.0)),
                    extra={
                        "wallets": c.get("wallets", []),
                        "notional_usdc": float(c.get("notional_usdc", c.get("notional", 0.0)) or 0.0),
                    },
                ))
            except (KeyError, TypeError, ValueError):
                log.warning("smart_money_mirror_bad_cluster", payload=c)
        return out
