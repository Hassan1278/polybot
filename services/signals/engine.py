"""Signal-Engine: candidates → gate chain → persist + publish."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.market_resolver import ensure_market
from polybot.models import AuditLog, Signal
from polybot.redis_bus import THESIS_DISSOLVING_KEY
from polybot.redis_bus import client as redis_client
from polybot.redis_bus import publish
from polybot.runtime_config import current_mode, merged_gates, merged_risk
from polybot.stats import position_size_from_score
from services.signals.conditions import REGISTRY, GateContext

log = get_logger(__name__)


async def _build_chain():
    # Use the merged read-path (YAML baseline + Redis overrides) so that
    # dashboard PATCHes via /admin/settings/gates take effect immediately.
    # Previously this read yaml_config.gates_cfg directly, which made the
    # entire override layer a no-op for the gate chain — the bug that
    # silently kept the bot strict no matter what the operator changed.
    mode = await current_mode()
    cfg = (await merged_gates(mode)).get("gates", [])
    chain = []
    for g in cfg:
        cls = REGISTRY.get(g["name"])
        if not cls:
            log.warning("gate_unknown", name=g["name"])
            continue
        chain.append(cls(enabled=g.get("enabled", True), params=g.get("params") or {}))
    return chain


async def process_candidate(cand: dict, *, target_size_usdc: float | None = None) -> dict:
    """Run a candidate through the gate chain. Returns the persisted signal record.

    `target_size_usdc` is overridable for back-compat (e.g. the
    `compute_correlations` script passes an explicit size); when left as
    ``None`` the size is derived from the final post-gate score using the
    `sizing` block in `risk.yaml`.
    """
    # Exit-mirror "stop adding": if the entry cluster for this (market, outcome)
    # is dissolving (flagged by the exit_loop), don't open or add to the thesis.
    # Money-safe — this only PREVENTS a new BUY entry. Fails open on a redis hiccup.
    if str(cand.get("side", "")).upper() == "BUY":
        try:
            _dissolving = await redis_client().get(
                THESIS_DISSOLVING_KEY.format(
                    mid=cand["market_id"],
                    oc=str(cand.get("outcome", "YES")).upper()))
        except Exception:  # noqa: BLE001
            _dissolving = None
        if _dissolving:
            log.info("entry_suppressed_thesis_dissolving",
                     market=cand["market_id"], outcome=cand.get("outcome"))
            return {"id": None, "pass": False, "gates": {},
                    "suppressed": "thesis_dissolving"}

    # JIT-resolve the market — most trades from tracked wallets are on long-tail
    # markets that the bulk ingest doesn't cover. We need category metadata for
    # the very first gate, so fetch on demand. Best-effort: if the resolver
    # fails (network blip, unknown market) we keep going with whatever data the
    # candidate already carries rather than dropping the signal on the floor.
    try:
        await ensure_market(cand["market_id"])
    except Exception as exc:  # noqa: BLE001 — resolver can raise any HTTP error
        log.warning("ensure_market_failed", market=cand["market_id"], err=str(exc))

    chain = await _build_chain()
    redis = redis_client()
    now = time.time()
    results: dict[str, dict] = {}
    score = float(cand.get("correlation_score", 0.0))
    gate_pass_hard = True

    # Same fix as above: use merged_risk so per-mode + override changes
    # to base/max/anchor/steepness apply without a restart.
    mode = await current_mode()
    sizing_cfg = (await merged_risk(mode) or {}).get("sizing", {}) or {}
    base = float(sizing_cfg.get("base_usdc", 10.0))
    max_size = float(sizing_cfg.get("max_usdc", 50.0))
    anchor = float(sizing_cfg.get("anchor", 0.5))
    steepness = float(sizing_cfg.get("steepness", 2.5))

    caller_override = target_size_usdc
    # Pre-gate provisional size — gates that key off size see a reasonable
    # value even before we've folded in the soft-gate score adjustments.
    provisional_size = (
        caller_override if caller_override is not None
        else position_size_from_score(score, base, max_size,
                                      anchor=anchor, steepness=steepness)
    )

    async with session_scope() as s:
        ctx = GateContext(
            candidate=cand, session=s, redis=redis, now_ts=now,
            extra={"target_size_usdc": provisional_size},
        )
        for gate in chain:
            res = await gate.evaluate(ctx)
            results[res.name] = {"pass": res.passed, "reason": res.reason,
                                 "type": res.type, "score_adjust": res.score_adjust}
            score = max(0.0, min(1.0, score + res.score_adjust))
            if res.type == "hard" and not res.passed:
                gate_pass_hard = False
                break

        # Final sizing reflects the post-gate score (soft-gate adjustments
        # included). Caller override still wins for back-compat.
        if caller_override is not None:
            final_size = float(caller_override)
        else:
            final_size = position_size_from_score(
                score, base, max_size, anchor=anchor, steepness=steepness,
            )

        sig = Signal(
            ts=datetime.now(tz=timezone.utc),
            market_id=cand["market_id"],
            outcome=cand.get("outcome", "YES"),
            side=cand["side"],
            wallet_count=len(cand.get("wallets") or []),
            wallets=cand.get("wallets") or [],
            avg_win_rate=ctx.extra.get("avg_win_rate", 0.0),
            correlation_score=score,
            target_price=ctx.extra.get("expected_avg_price") or cand.get("avg_price", 0.0),
            target_size_usdc=final_size,
            gate_results=results,
            gate_pass=gate_pass_hard,
        )
        s.add(sig)
        s.add(AuditLog(actor="signals", event="signal_evaluated",
                       payload={"market_id": cand["market_id"], "side": cand["side"],
                                "pass": gate_pass_hard, "score": score,
                                "size_usdc": final_size, "gates": results}))
        await s.flush()
        sid = sig.id

    if gate_pass_hard:
        # Arm the per-market cooldown ONLY now that we know the signal
        # actually fires. Previously the cooldown gate wrote the key
        # during evaluate() — if a later hard or soft gate downgraded
        # the cluster, the cooldown blocked legitimate follow-ups for
        # 30 min while no signal actually existed.
        cd_seconds = ctx.extra.get("cooldown_seconds")
        if cd_seconds:
            from services.signals.conditions.cooldown import KEY as _CD_KEY
            try:
                await redis.set(
                    _CD_KEY.format(mid=cand["market_id"]),
                    "1", ex=int(cd_seconds),
                )
            except Exception:  # noqa: BLE001
                log.warning("cooldown_arm_failed", market=cand["market_id"])

        signal_payload = {
            "id": sid,
            "market_id": cand["market_id"],
            "outcome": cand.get("outcome", "YES"),
            "side": cand["side"],
            "wallets": cand.get("wallets") or [],
            "avg_price": ctx.extra.get("expected_avg_price") or cand.get("avg_price", 0.0),
            "size_usdc": final_size,
            "score": score,
        }
        # B1: durable delivery via Redis Streams. xpublish guarantees that
        # the signal survives a subscriber crash — the executor consumer
        # group only ACKs after a Fill row is written (or DLQ on error).
        # The legacy `publish("signal:new", ...)` pub/sub is kept for the
        # dashboard SSE endpoint to live-stream signals without joining
        # the consumer group.
        from polybot.redis_bus import xpublish
        stream_id = await xpublish("signal:new", signal_payload)
        await publish("signal:new", signal_payload)
        log.info(
            "signal_fired",
            id=sid, stream_id=stream_id, market=cand["market_id"],
            side=cand["side"], score=score, size_usdc=final_size,
        )
    else:
        log.info("signal_gated_out", market=cand["market_id"], side=cand["side"], gates=results)
    return {"id": sid, "pass": gate_pass_hard, "gates": results}
