"""SignalStrategy protocol + Candidate dataclass.

SOLID applied to signal generation:

  S - Single Responsibility: a Strategy ONLY turns trades→candidates.
      Persistence, gates, risk, execution are someone else's problem.

  O - Open/Closed: add a new strategy by subclassing SignalStrategy in a
      new file + registering it. No edits to correlation_loop, engine,
      or any gate.

  L - Liskov: every Strategy returns the same `Candidate` shape, so
      downstream code is invariant to which strategy is plugged in.

  I - Interface Segregation: the protocol has exactly ONE method,
      `generate_candidates(df, **knobs) -> list[Candidate]`. Knobs are
      passed positionally as kwargs; strategies ignore what they don't
      use. Nothing forces a strategy to implement irrelevant methods.

  D - Dependency Inversion: correlation_loop depends on the abstract
      SignalStrategy, not on `cluster_active_wallets` directly. The
      strategy is injected via env var (Strategy Registry).

The `Candidate` dataclass replaces the previous untyped dict shape — it's
serialised back to a dict via `to_dict()` for the existing engine + Redis
publish pipeline, but inside the strategy code we use the typed object.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pandas as pd


@dataclass
class Candidate:
    """A trade idea produced by a SignalStrategy.

    All money values are float USDC (legacy — repo-wide TODO to migrate
    to Decimal). Timestamps are epoch seconds (int) for JSON friendliness.

    Required fields are the minimum the gate chain expects. The `extra`
    dict carries strategy-specific data (e.g. wallet list for the
    correlation_score gate) without bloating the core schema.
    """
    market_id: str
    outcome: str
    side: str           # "BUY" or "SELL"
    # Score in [0, 1] used by position-sizing + the correlation_score gate.
    score: float
    # Best-effort entry-price hint; engine recomputes via book walk.
    avg_price: float = 0.0
    # Strategy-specific metadata (e.g. wallets=[...], notional=...).
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise into the dict shape engine.process_candidate expects.

        Keeps backward compat with code that hasn't migrated off `dict`
        access — extra fields are merged into the top level so callers
        can still write `c["wallets"]` etc.
        """
        d: dict[str, Any] = {
            "market_id": self.market_id,
            "outcome": self.outcome,
            "side": self.side,
            "score": self.score,
            "avg_price": self.avg_price,
        }
        d.update(self.extra)
        return d


@runtime_checkable
class SignalStrategy(Protocol):
    """A pluggable signal-generation strategy.

    Implementations are stateless and short-lived (one instance per
    correlation_loop, no per-call state). They MUST NOT touch the DB,
    Redis, or the executor — pure compute from a DataFrame of recent
    trades to a list of Candidates.
    """

    name: str

    async def generate_candidates(
        self,
        recent_trades: "pd.DataFrame",
        **knobs: Any,
    ) -> list[Candidate]:
        """Translate recent tracked-wallet trades into candidate ideas.

        `recent_trades` columns: ts, wallet, market_id, outcome, side,
        size_shares, price, notional_usdc.

        `knobs` carry runtime tuning (window_minutes, min_wallets,
        half_life_seconds, etc.). Strategies pick out what they need
        and ignore the rest — no version skew when new knobs are added.

        Empty input → empty output (don't raise). On error: log + return
        empty list; the correlation_loop is paranoid and shouldn't crash
        because a strategy bug.
        """
        ...
