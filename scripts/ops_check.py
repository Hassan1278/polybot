"""OPS health check — run after 24h / 48h to verify the bot worked correctly
WITHOUT looking at profitability. This is a *did the machinery run* report,
not a *did it make money* report.

Usage (from host):
    docker compose exec api python -m scripts.ops_check

Output: a structured report with green/red status per check. Exit code 0 if
all critical checks pass, 1 if any critical check failed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text

from polybot.db import session_scope
from polybot.models import (
    AuditLog,
    Fill,
    PnLSnapshot,
    Position,
    Signal,
    Trade,
    Wallet,
    WalletStats,
)

# Thresholds — adjust if the bot ran longer than 48h.
LOOKBACK_HOURS = 48
HEARTBEAT_OK_INTERVAL_MIN = 6        # attribution heartbeat should fire ≥ every 5 min
MIN_PNL_SNAPSHOTS = 60               # over 48h we expect ≥1/min × 48h = ~2880, allow 60 min outage
MIN_TRADES_PER_HOUR = 1              # at least 1 wallet trade per hour means ingest is alive
MAX_ZERO_HEARTBEAT_EVENTS = 2        # how many "attribution_zero" alerts is OK


def _bullet(ok: bool, label: str, detail: str = "") -> str:
    icon = "✅" if ok else "❌"
    suffix = f" — {detail}" if detail else ""
    return f"  {icon} {label}{suffix}"


async def main() -> None:
    now = datetime.now(tz=timezone.utc)
    since = now - timedelta(hours=LOOKBACK_HOURS)
    findings: list[tuple[bool, str]] = []
    critical_failures = 0

    print(f"\n=== Polybot OPS check ===")
    print(f"window: last {LOOKBACK_HOURS}h (since {since:%Y-%m-%d %H:%M} UTC)")
    print()

    async with session_scope() as s:
        # 1. Trade ingest is still alive ------------------------------------
        n_trades = int(
            (await s.execute(
                select(func.count(Trade.id)).where(Trade.ts >= since)
            )).scalar_one()
        )
        trade_rate = n_trades / LOOKBACK_HOURS
        ok = trade_rate >= MIN_TRADES_PER_HOUR
        if not ok:
            critical_failures += 1
        findings.append((
            ok,
            f"Trade ingest alive: {n_trades} trades in {LOOKBACK_HOURS}h "
            f"({trade_rate:.1f}/h, expect ≥ {MIN_TRADES_PER_HOUR}/h)",
        ))

        # 2. PnL snapshot loop ran without long outages ---------------------
        n_snapshots = int(
            (await s.execute(
                select(func.count(PnLSnapshot.id)).where(PnLSnapshot.ts >= since)
            )).scalar_one()
        )
        expected_snapshots = LOOKBACK_HOURS * 60   # 1/min
        snapshot_coverage = n_snapshots / expected_snapshots if expected_snapshots else 0.0
        ok = n_snapshots >= MIN_PNL_SNAPSHOTS
        if not ok:
            critical_failures += 1
        findings.append((
            ok,
            f"PnL-loop heartbeat: {n_snapshots} snapshots "
            f"({snapshot_coverage*100:.0f}% of expected {expected_snapshots})",
        ))

        # 3. Attribution heartbeat — should NOT have fired zero-attribution -
        zero_attribution_events = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.event == "attribution_zero",
                    AuditLog.ts >= since,
                )
            )
        ).scalars().all()
        ok = len(zero_attribution_events) <= MAX_ZERO_HEARTBEAT_EVENTS
        if not ok:
            critical_failures += 1
        findings.append((
            ok,
            f"Zero-attribution alerts: {len(zero_attribution_events)} "
            f"(expect ≤ {MAX_ZERO_HEARTBEAT_EVENTS}, > means proxyWallet poll broke)",
        ))

        # 4. Signal evaluation happened -------------------------------------
        n_signals = int(
            (await s.execute(
                select(func.count(Signal.id)).where(Signal.ts >= since)
            )).scalar_one()
        )
        n_pass = int(
            (await s.execute(
                select(func.count(Signal.id))
                .where(Signal.ts >= since)
                .where(Signal.gate_pass.is_(True))
            )).scalar_one()
        )
        pass_rate = (n_pass / n_signals) if n_signals else 0.0
        # signals are noisy; we just want SOME activity (>10 over 48h)
        ok = n_signals >= 10
        findings.append((
            ok,
            f"Signal engine: {n_signals} candidates, {n_pass} passed all gates "
            f"({pass_rate*100:.1f}% pass-rate)",
        ))

        # 5. Fills happened OR at least an honest reason why ----------------
        n_fills = int(
            (await s.execute(
                select(func.count(Fill.id))
                .where(Fill.ts >= since, Fill.status == "filled")
            )).scalar_one()
        )
        n_rejected = int(
            (await s.execute(
                select(func.count(Fill.id))
                .where(Fill.ts >= since, Fill.status == "rejected")
            )).scalar_one()
        )
        risk_rejects = int(
            (await s.execute(
                select(func.count(AuditLog.id))
                .where(AuditLog.event == "risk_rejected", AuditLog.ts >= since)
            )).scalar_one()
        )
        ok = (n_fills + n_rejected + risk_rejects) > 0   # SOMETHING happened
        findings.append((
            ok,
            f"Executor activity: {n_fills} filled · "
            f"{n_rejected} rejected · {risk_rejects} risk-blocked",
        ))

        # 6. Wallet stats are being recomputed ------------------------------
        latest_stat = (
            await s.execute(
                select(func.max(WalletStats.computed_at))
            )
        ).scalar_one()
        stat_lag_min = ((now - latest_stat).total_seconds() / 60.0) if latest_stat else 9999
        ok = stat_lag_min <= 30   # stats loop runs every 5 min, allow 30 min lag
        findings.append((
            ok,
            f"Wallet-stats freshness: last update {stat_lag_min:.0f} min ago "
            f"(expect ≤ 30 min)",
        ))

        # 7. Position bookkeeping intact ------------------------------------
        open_positions = int(
            (await s.execute(
                select(func.count(Position.id))
                .where(Position.size_shares > 0, Position.wallet == "PAPER")
            )).scalar_one()
        )
        total_realized = float(
            (await s.execute(
                select(func.coalesce(func.sum(Position.realized_pnl_usdc), 0.0))
                .where(Position.wallet == "PAPER")
            )).scalar_one()
        )
        # Cross-check with PnL snapshot
        latest_snapshot = (
            await s.execute(
                select(PnLSnapshot)
                .order_by(PnLSnapshot.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        snap_open = latest_snapshot.open_positions if latest_snapshot else None
        ok = snap_open == open_positions   # the two views should agree
        findings.append((
            ok,
            f"Position accounting: {open_positions} open in positions table, "
            f"{snap_open} in latest snapshot — {'consistent' if ok else 'MISMATCH'}",
        ))

        # 8. Error / exception scan in audit log ----------------------------
        risk_breakdown = (
            await s.execute(
                text(
                    """
                    SELECT payload->>'reason' AS reason, COUNT(*) AS n
                    FROM audit_log
                    WHERE event = 'risk_rejected' AND ts >= :since
                    GROUP BY 1 ORDER BY 2 DESC LIMIT 5
                    """
                ),
                {"since": since},
            )
        ).all()

        # 9. Gate-fail breakdown — which gate filtered the most signals -----
        gate_breakdown_rows = (
            await s.execute(
                text(
                    """
                    SELECT key, COUNT(*) AS n
                    FROM (
                        SELECT (jsonb_each_text(gate_results::jsonb)).key AS key,
                               (jsonb_each_text(gate_results::jsonb)).value::jsonb->>'pass' AS pass
                        FROM signals WHERE ts >= :since AND NOT gate_pass
                    ) t
                    WHERE pass = 'false'
                    GROUP BY 1 ORDER BY 2 DESC
                    """
                ),
                {"since": since},
            )
        ).all()

        # 10. Container uptime check — query latest pnl_snapshot age --------
        if latest_snapshot:
            snap_age_s = (now - latest_snapshot.ts).total_seconds()
            ok = snap_age_s < 300   # last snapshot < 5 min ago
            if not ok:
                critical_failures += 1
            findings.append((
                ok,
                f"Executor uptime check: last PnL snapshot {snap_age_s:.0f}s ago "
                f"(expect < 300s)",
            ))
        else:
            findings.append((False, "Executor uptime check: NO pnl_snapshot exists at all"))
            critical_failures += 1

        # 11. Active wallet roster intact -----------------------------------
        n_active = int(
            (await s.execute(
                select(func.count(Wallet.address)).where(Wallet.is_active.is_(True))
            )).scalar_one()
        )
        ok = n_active >= 30
        if not ok:
            critical_failures += 1
        findings.append((
            ok,
            f"Active wallet roster: {n_active} wallets "
            f"(expect ≥ 30 across enabled categories)",
        ))

    # ------------------------------------------------------------------- #
    # Output                                                              #
    # ------------------------------------------------------------------- #
    print("CRITICAL CHECKS")
    print()
    for ok, label in findings:
        print(_bullet(ok, label))

    if risk_breakdown:
        print()
        print("RISK-REJECT BREAKDOWN")
        for reason, n in risk_breakdown:
            print(f"  · {reason}: {n}")
    else:
        print()
        print("RISK-REJECT BREAKDOWN: (none)")

    if gate_breakdown_rows:
        print()
        print("GATE-FAIL BREAKDOWN")
        for gate, n in gate_breakdown_rows:
            print(f"  · {gate}: {n}")
    else:
        print()
        print("GATE-FAIL BREAKDOWN: (no failed signals — that's unusual)")

    print()
    print(
        f"{'OK' if critical_failures == 0 else 'FAIL'} "
        f"({critical_failures} critical check{'s' if critical_failures != 1 else ''} failed)"
    )
    sys.exit(0 if critical_failures == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
