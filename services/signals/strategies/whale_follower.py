"""Single-whale follower — second strategy demonstrating the protocol.

Premise: instead of waiting for a CLUSTER of smart-money wallets to align,
follow ONE big wallet (the "whale") as soon as they place a trade above
a notional threshold. Single-source, low-latency, no correlation needed.

This is the second strategy that proves the abstraction works without
ANY changes to gates, executor, or persistence. To enable:

    SIGNAL_STRATEGY=whale_follower docker compose up

Knobs (from env, with sane defaults):
  WHALE_FOLLOWER_ADDRESS       — wallet address to mirror (no default → no signals)
  WHALE_FOLLOWER_MIN_NOTIONAL  — only mirror trades above this $ (default 1000)
  WHALE_FOLLOWER_LOOKBACK_MIN  — only consider trades fresher than this (default 5)

Notes:
  - Score is fixed at 0.7 — gates downstream still decide pass/fail. If you
    want score-based sizing, set CORRELATION_K_NOTIONAL appropriately in
    risk.yaml or extend this strategy to compute score from notional ratio.
  - No correlation_score gate constraint applies (single wallet), so set
    `correlation_score.min_wallets` to 1 in gates.yaml when running this
    strategy. Documented because the user controls gate config separately.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from polybot.logging import get_logger
from services.signals.strategies.base import Candidate, SignalStrategy

if TYPE_CHECKING:
    import pandas as pd

log = get_logger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class WhaleFollower(SignalStrategy):
    name = "whale_follower"

    def __init__(self) -> None:
        self.address = (os.environ.get("WHALE_FOLLOWER_ADDRESS") or "").lower()
        self.min_notional = _env_float("WHALE_FOLLOWER_MIN_NOTIONAL", 1000.0)
        self.lookback_min = _env_float("WHALE_FOLLOWER_LOOKBACK_MIN", 5.0)
        if not self.address:
            log.warning(
                "whale_follower_no_address",
                msg="WHALE_FOLLOWER_ADDRESS not set; will emit zero signals.",
            )

    async def generate_candidates(
        self,
        recent_trades: "pd.DataFrame",
        **knobs: Any,
    ) -> list[Candidate]:
        if not self.address or recent_trades is None or recent_trades.empty:
            return []
        df = recent_trades
        try:
            fresh = df[df["wallet"].str.lower() == self.address]
            if fresh.empty:
                return []
            # Only the most recent trade per (market_id, outcome, side) —
            # we don't re-emit on every poll for the same trade.
            fresh = fresh.sort_values("ts", ascending=False).drop_duplicates(
                subset=["market_id", "outcome", "side"], keep="first",
            )
            # Drop trades older than lookback window. The trades DataFrame
            # stores `ts` as a tz-aware datetime (matches the SQL column).
            # The old `.astype(float)` either erroneously cast to ns-since-
            # epoch (~10^18) or raised on tz-aware values, making the
            # cutoff comparison a no-op or a crash. Use a datetime cutoff
            # and compare directly — same idiom as cluster_active_wallets.
            cutoff_dt = datetime.now(tz=timezone.utc) - timedelta(minutes=self.lookback_min)
            fresh = fresh[fresh["ts"] >= cutoff_dt]
            # Notional gate.
            fresh = fresh[fresh["notional_usdc"].astype(float) >= self.min_notional]
        except (KeyError, AttributeError):
            log.exception("whale_follower_bad_df")
            return []

        out: list[Candidate] = []
        for _, row in fresh.iterrows():
            try:
                out.append(Candidate(
                    market_id=str(row["market_id"]),
                    outcome=str(row["outcome"]),
                    side=str(row["side"]),
                    score=0.7,
                    avg_price=float(row["price"]),
                    extra={
                        "wallets": [self.address],
                        "notional_usdc": float(row["notional_usdc"]),
                        "strategy_note": "whale_follower",
                    },
                ))
            except (KeyError, TypeError, ValueError):
                log.warning("whale_follower_bad_row")
        return out
