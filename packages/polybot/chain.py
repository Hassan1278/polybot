"""Read-only on-chain helpers for Polygon.

Used to show the bot wallet's REAL balances on the dashboard so the
operator doesn't have to trust the synthetic paper number.

Web3 reads only — no private key needed. Functions are sync (web3.py is
sync); callers wrap with `asyncio.to_thread(...)` to stay non-blocking.
"""
from __future__ import annotations

from functools import lru_cache

from web3 import Web3

from polybot.config import settings

# USDC.e (bridged USDC) on Polygon — the token Polymarket settles in.
# 6 decimals (USDC standard, not the 18-decimal ETH norm).
USDC_E_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
USDC_E_DECIMALS = 6

# Native USDC (post-bridge, circle-issued) — minor balance possible if the
# operator funded via a CEX that auto-routes to native.
USDC_NATIVE_ADDRESS = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")

# Minimal ERC20 ABI fragment — just balanceOf(address) → uint256.
_ERC20_BALANCE_ABI = [{
    "constant": True,
    "inputs": [{"name": "_owner", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "balance", "type": "uint256"}],
    "type": "function",
}]


@lru_cache(maxsize=1)
def _w3() -> Web3:
    """Single shared HTTP web3 client. Constructed lazily so unit tests
    that never call chain.* don't open a connection."""
    return Web3(Web3.HTTPProvider(settings.polygon_rpc_url, request_kwargs={"timeout": 5}))


def read_balances(address: str) -> dict:
    """Synchronously read POL (native) + USDC.e + native-USDC balances
    for ``address``. Returns floats in human units (1 POL, 1 USDC).

    Always returns the full dict shape even on partial RPC failures —
    failed reads come back as ``None`` so the caller can render "—".
    """
    out: dict = {"address": address, "pol": None, "usdc_e": None, "usdc_native": None, "error": None}
    try:
        w3 = _w3()
        checksum = Web3.to_checksum_address(address)
        out["pol"] = w3.eth.get_balance(checksum) / 1e18
        for key, contract_addr, decimals in (
            ("usdc_e",      USDC_E_ADDRESS,      USDC_E_DECIMALS),
            ("usdc_native", USDC_NATIVE_ADDRESS, 6),
        ):
            try:
                erc20 = w3.eth.contract(address=contract_addr, abi=_ERC20_BALANCE_ABI)
                raw = erc20.functions.balanceOf(checksum).call()
                out[key] = raw / (10 ** decimals)
            except Exception:  # noqa: BLE001
                out[key] = None
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return out
