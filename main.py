import os
import asyncio
import aiohttp
from fastapi import FastAPI, HTTPException
from x402.server import x402ResourceServer
from x402.http import HTTPFacilitatorClient, FacilitatorConfig, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.svm.exact import ExactSvmServerScheme

app = FastAPI(
    title="God's Eye Solana Token Risk & Bundle Telemetry API",
    description="Real-time on-chain Top-10 holder bundle detection and DEX liquidity velocity for AI trading agents.",
    version="2.0.0",
)

SOLANA_PAY_TO = os.environ["SOLANA_WALLET_ADDRESS"]
RPC_ENDPOINT = os.environ.get("SOLANA_RPC_ENDPOINT", "https://api.mainnet-beta.solana.com")
FACILITATOR_URL = os.environ.get("X402_FACILITATOR_URL", "https://facilitator.payai.network")
SOLANA_MAINNET_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

# 1. Configure x402 V2 Resource Server for Solana Mainnet
facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))
resource_server = x402ResourceServer(facilitator)
resource_server.register(SOLANA_MAINNET_CAIP2, ExactSvmServerScheme())

# 2. Gate /v1/audit/* behind a $0.03 USDC Solana micropayment
routes = {
    "GET /v1/audit/*": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                price="$0.03",
                network=SOLANA_MAINNET_CAIP2,
                pay_to=SOLANA_PAY_TO,
            )
        ],
        description="On-chain Solana mint sniper-bundle audit and DEX liquidity check",
        mime_type="application/json",
    )
}

app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=resource_server)

async def inspect_holders(session: aiohttp.ClientSession, mint: str) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint]}
    async with session.post(RPC_ENDPOINT, json=payload, timeout=5) as resp:
        data = await resp.json()
        accounts = data.get("result", {}).get("value", [])
        if len(accounts) < 2:
            return {"is_bundled": False, "top9_non_pool_pct": 0.0, "max_single_holder_pct": 0.0}
        supply = 1_000_000_000.0
        pcts = [(float(a.get("uiAmount") or 0) / supply) * 100.0 for a in accounts[1:10]]
        max_single = round(max(pcts), 2) if pcts else 0.0
        top9_sum = round(sum(pcts), 2)
        return {
            "is_bundled": (max_single > 6.0 or top9_sum > 22.0),
            "top9_non_pool_pct": top9_sum,
            "max_single_holder_pct": max_single,
        }

async def inspect_dex(session: aiohttp.ClientSession, mint: str) -> dict:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    async with session.get(url, timeout=6) as resp:
        data = await resp.json()
        sol_pairs = [p for p in data.get("pairs", []) if p.get("chainId") == "solana"]
        if not sol_pairs:
            return {}
        pair = max(sol_pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0))
        liq = pair.get("liquidity", {}).get("usd", 0)
        fdv = pair.get("fdv", 0)
        txns = pair.get("txns", {}).get("m5", {})
        buys, sells = txns.get("buys", 0), txns.get("sells", 0)
        return {
            "symbol": pair.get("baseToken", {}).get("symbol"),
            "liquidity_usd": liq,
            "fdv_usd": fdv,
            "liq_to_fdv_ratio": round(liq / fdv, 3) if fdv > 0 else 0,
            "m5_buy_sell_ratio": round(buys / sells, 2) if sells > 0 else float(buys),
            "price_change_5m": pair.get("priceChange", {}).get("m5", 0),
            "price_change_1h": pair.get("priceChange", {}).get("h1", 0),
        }

@app.get("/health")
async def health():
    return {"status": "online", "protocol": "x402-v2", "network": SOLANA_MAINNET_CAIP2, "price_usd": 0.03}

@app.get("/v1/audit/{mint}")
async def audit_mint(mint: str):
    async with aiohttp.ClientSession() as session:
        holders, dex = await asyncio.gather(inspect_holders(session, mint), inspect_dex(session, mint))
        if not dex:
            raise HTTPException(status_code=404, detail="Solana DEX pair not found")
        safe_to_trade = (
            not holders["is_bundled"]
            and dex["liquidity_usd"] >= 8000
            and dex["liq_to_fdv_ratio"] >= 0.10
            and dex["m5_buy_sell_ratio"] >= 1.6
        )
        return {
            "mint": mint,
            "verdict": "PASS" if safe_to_trade else "REJECT_HIGH_RISK",
            "holder_bundle_audit": holders,
            "dex_telemetry": dex,
        }