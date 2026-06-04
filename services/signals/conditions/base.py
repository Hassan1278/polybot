"""Gate contract — every condition implements `Gate`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class GateContext:
    """Everything a gate may need to make a decision."""
    candidate: dict[str, Any]                  # {market_id, side, outcome, wallets[], avg_price, correlation_score, ...}
    session: AsyncSession
    redis: Any                                  # redis client
    now_ts: float
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateResult:
    name: str
    type: str                                   # "hard" | "soft"
    passed: bool
    reason: str = ""
    score_adjust: float = 0.0                   # for soft gates


class Gate(Protocol):
    name: str
    type: str
    enabled: bool

    async def evaluate(self, ctx: GateContext) -> GateResult: ...
