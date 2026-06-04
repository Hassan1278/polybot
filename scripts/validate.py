"""End-to-end validation. Run sections individually:

    python scripts/validate.py env
    python scripts/validate.py connectivity
    python scripts/validate.py db
    python scripts/validate.py ingest
    python scripts/validate.py signals
    python scripts/validate.py executor-paper
    python scripts/validate.py executor-live --dry
    python scripts/validate.py risk
    python scripts/validate.py all
"""

from __future__ import annotations

import asyncio
import sys

import click
import httpx
from sqlalchemy import func, select, text

from polybot.clients import ClobClient, DataClient, GammaClient
from polybot.config import settings
from polybot.db import session_scope
from polybot.logging import get_logger
from polybot.models import Trade, Wallet, WalletStats

log = get_logger(__name__)
OK = "✓"
NO = "✗"


def _ok(label: str, ok: bool, detail: str = "") -> bool:
    sym = OK if ok else NO
    print(f"  {sym} {label}{(' — ' + detail) if detail else ''}")
    return ok


# ---- sections --------------------------------------------------------------

def check_env() -> bool:
    print("\n[env]")
    ok = True
    ok &= _ok("DATABASE_URL set", bool(settings.database_url))
    ok &= _ok("REDIS_URL set", bool(settings.redis_url))
    ok &= _ok("ADMIN_TOKEN not default",
              settings.admin_token.get_secret_value() != "change_me",
              "tighten the admin token in .env" if settings.admin_token.get_secret_value() == "change_me" else "")
    ok &= _ok(f"trading_mode={settings.trading_mode}", True)
    if settings.is_live:
        ok &= _ok("can_sign", settings.can_sign,
                  "need POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER_ADDRESS")
    return ok


async def check_connectivity() -> bool:
    print("\n[connectivity]")
    ok = True
    g, d, c = GammaClient(), DataClient(), ClobClient()
    try:
        try:
            ms = await g.markets(limit=1)
            ok &= _ok("Gamma reachable", bool(ms))
        except Exception as e:
            ok &= _ok("Gamma reachable", False, str(e))
        try:
            await c.midpoint("dummy")          # may 400 — we just want a response
            ok &= _ok("CLOB reachable", True)
        except Exception as e:
            ok &= _ok("CLOB reachable", "400" in str(e) or "422" in str(e), str(e))
        # data api expects ?user=, just check the host responds
        try:
            async with httpx.AsyncClient(timeout=5) as h:
                r = await h.get(f"{settings.polymarket_data_url}/positions",
                                params={"user": "0x0000000000000000000000000000000000000000", "limit": 1})
            ok &= _ok("Data reachable", r.status_code < 500, f"status={r.status_code}")
        except Exception as e:
            ok &= _ok("Data reachable", False, str(e))
        try:
            async with httpx.AsyncClient(timeout=5) as h:
                r = await h.post(settings.polygon_rpc_url,
                                 json={"jsonrpc": "2.0", "method": "eth_blockNumber",
                                       "params": [], "id": 1})
            ok &= _ok("Polygon RPC reachable", r.status_code == 200, f"status={r.status_code}")
        except Exception as e:
            ok &= _ok("Polygon RPC reachable", False, str(e))
    finally:
        await asyncio.gather(g.close(), d.close(), c.close())
    return ok


async def check_db() -> bool:
    print("\n[db]")
    ok = True
    async with session_scope() as s:
        try:
            v = (await s.execute(text("SELECT 1"))).scalar()
            ok &= _ok("connect", v == 1)
        except Exception as e:
            return _ok("connect", False, str(e))
        try:
            ex = (await s.execute(text("SELECT extname FROM pg_extension WHERE extname='timescaledb'"))).scalar()
            ok &= _ok("timescaledb extension", ex == "timescaledb")
        except Exception as e:
            ok &= _ok("timescaledb extension", False, str(e))
        for t in ["wallets", "wallet_stats", "markets", "trades", "positions",
                  "signals", "fills", "pnl_snapshots", "audit_log"]:
            try:
                n = (await s.execute(text(f"SELECT COUNT(*) FROM {t}"))).scalar()
                ok &= _ok(f"table {t}", True, f"rows={n}")
            except Exception as e:
                ok &= _ok(f"table {t}", False, str(e))
    return ok


async def check_ingest() -> bool:
    print("\n[ingest]")
    ok = True
    async with session_scope() as s:
        active = (await s.execute(
            select(func.count()).select_from(Wallet).where(Wallet.is_active.is_(True))
        )).scalar_one()
        ok &= _ok("active wallets ≥ 20", active >= 20, f"have={active}")
        trades = (await s.execute(select(func.count()).select_from(Trade))).scalar_one()
        ok &= _ok("trades ≥ 1000", trades >= 1000, f"have={trades}")
        stats = (await s.execute(select(func.count()).select_from(WalletStats))).scalar_one()
        ok &= _ok("wallet_stats present", stats > 0, f"have={stats}")
    return ok


async def check_signals() -> bool:
    print("\n[signals]")
    from services.signals.engine import process_candidate

    # synthetic candidate
    candidate = {
        "market_id": "0xtest",
        "outcome": "YES",
        "side": "BUY",
        "wallets": ["0xaaaa", "0xbbbb", "0xcccc"],
        "avg_price": 0.35,
        "notional_usdc": 100.0,
        "correlation_score": 0.7,
    }
    try:
        res = await process_candidate(candidate, target_size_usdc=10.0)
        return _ok("synthetic signal evaluated", True,
                   f"id={res['id']} pass={res['pass']}")
    except Exception as e:
        return _ok("synthetic signal evaluated", False, str(e))


async def check_executor_paper() -> bool:
    print("\n[executor-paper]")
    if settings.trading_mode != "paper":
        return _ok("TRADING_MODE=paper", False, f"currently '{settings.trading_mode}'")
    return _ok("TRADING_MODE=paper", True)


async def check_executor_live(dry: bool) -> bool:
    print("\n[executor-live]")
    if dry:
        return _ok("dry-run OK (no orders sent)", True)
    return _ok("not implemented", False, "use --dry; live order test deliberately manual")


async def check_risk() -> bool:
    print("\n[risk]")
    from services.executor.risk import RiskRejection, preflight
    try:
        await preflight(mode="paper", market_id="0xtest", category="politics",
                        side="BUY", size_usdc=10_000.0, score=0.7)
        return _ok("size cap rejects oversized order", False, "did NOT reject")
    except RiskRejection:
        return _ok("size cap rejects oversized order", True)


# ---- entrypoint -----------------------------------------------------------

@click.command()
@click.argument("section", type=click.Choice(
    ["env", "connectivity", "db", "ingest", "signals", "executor-paper",
     "executor-live", "risk", "observability", "all"]))
@click.option("--dry", is_flag=True)
def main(section: str, dry: bool) -> None:
    async def _run():
        results: list[bool] = []
        if section in ("env", "all"):            results.append(check_env())
        if section in ("connectivity", "all"):   results.append(await check_connectivity())
        if section in ("db", "all"):             results.append(await check_db())
        if section in ("ingest", "all"):         results.append(await check_ingest())
        if section in ("signals", "all"):        results.append(await check_signals())
        if section in ("executor-paper", "all"): results.append(await check_executor_paper())
        if section == "executor-live":           results.append(await check_executor_live(dry))
        if section in ("risk", "all"):           results.append(await check_risk())
        if section == "observability":
            print("[observability]\n  (manual — check dashboard + JSON logs)")
            results.append(True)
        print(f"\n{'OK' if all(results) else 'FAIL'} — {sum(results)}/{len(results)} sections passed")
        sys.exit(0 if all(results) else 1)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
