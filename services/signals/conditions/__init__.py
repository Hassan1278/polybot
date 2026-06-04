from services.signals.conditions.base import Gate, GateContext, GateResult
from services.signals.conditions.category import CategoryMatch
from services.signals.conditions.cooldown import Cooldown
from services.signals.conditions.correlation_score import CorrelationScore
from services.signals.conditions.liquidity import Liquidity
from services.signals.conditions.opposing import OpposingSmartMoney
from services.signals.conditions.risk_reward import RiskReward
from services.signals.conditions.timeframe import Timeframe
from services.signals.conditions.wallet_quality import WalletQuality

REGISTRY: dict[str, type[Gate]] = {
    "category_match":     CategoryMatch,
    "wallet_quality":     WalletQuality,
    "liquidity":          Liquidity,
    "risk_reward":        RiskReward,
    "timeframe":          Timeframe,
    "correlation_score":  CorrelationScore,
    "cooldown":           Cooldown,
    "opposing_smart_money": OpposingSmartMoney,
}

__all__ = ["Gate", "GateContext", "GateResult", "REGISTRY"]
