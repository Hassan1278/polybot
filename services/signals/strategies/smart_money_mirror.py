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
from polybot.stats import (
    DEFAULT_HALF_LIFE_SECONDS,
    DEFAULT_K_NOTIONAL,
    DEFAULT_K_WALLETS,
    cluster_active_wallets,
)
from services.signals.strategies.base import Candidate, SignalStrategy

if TYPE_CHECKING:
    import pandas as pd

log = get_logger(__name__)

# Categories whose smart-money agreement accrues over HOURS, not minutes —
# they get the wide/slow correlation window. Everything else (sports/worldcup/
# unknown) uses the tight/fast window for live-event bursts.
_SLOW_CATEGORIES = {"crypto", "politics", "macro", "weather"}


class SmartMoneyMirror(SignalStrategy):
    name = "smart_money_mirror"

    async def generate_candidates(
        self,
        recent_trades: "pd.DataFrame",
        **knobs: Any,
    ) -> list[Candidate]:
        if recent_trades is None or recent_trades.empty:
            return []

        # Defaults track polybot.stats so the wrapper doesn't silently override
        # the production-tuned constants.
        min_w = max(2, int(knobs.get("min_wallets", 2)))
        k_w = knobs.get("k_wallets", DEFAULT_K_WALLETS)
        k_n = knobs.get("k_notional", DEFAULT_K_NOTIONAL)
        fast_window = knobs.get("window_minutes", 30)
        fast_hl = knobs.get("half_life_seconds", DEFAULT_HALF_LIFE_SECONDS)
        # Slow bucket falls back to the fast values if not supplied.
        slow_window = knobs.get("slow_window_minutes", fast_window)
        slow_hl = knobs.get("slow_half_life_seconds", fast_hl)
        cat_map = knobs.get("market_category") or {}

        def _cluster(df: Any, window: int, hl: float) -> list[dict]:
            return cluster_active_wallets(
                df, window_minutes=window, min_wallets=min_w,
                half_life_seconds=hl, k_wallets=k_w, k_notional=k_n,
            )

        try:
            if cat_map:
                # Per-category windows: slow categories (crypto/politics/macro/
                # weather) cluster over the wide window; everything else over
                # the tight one. cluster_active_wallets re-filters each subset
                # to its own window internally.
                cats = recent_trades["market_id"].map(lambda m: cat_map.get(str(m)))
                slow_mask = cats.isin(list(_SLOW_CATEGORIES))
                df_slow = recent_trades[slow_mask]
                df_fast = recent_trades[~slow_mask]
                raw: list[dict] = []
                if not df_slow.empty:
                    raw += _cluster(df_slow, slow_window, slow_hl)
                if not df_fast.empty:
                    raw += _cluster(df_fast, fast_window, fast_hl)
            else:
                # Fallback (no category map) — legacy single-window behaviour.
                raw = _cluster(recent_trades, fast_window, fast_hl)
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
