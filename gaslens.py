"""
GasLens — Multi-chain gas price tracker.

FastAPI service. Polls public RPCs for current gas prices, fetches native
token USD prices from CoinGecko, computes USD cost estimates for common
tx types (transfer, ERC20, swap, NFT mint).

No login. No API key. No wallet. Pure utility.
"""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from web3 import Web3

# ============== CONFIG ==============

CHAINS = {
    "ethereum": {
        "name": "Ethereum",
        "rpc": "https://ethereum-rpc.publicnode.com",
        "native": "ETH",
        "coingecko_id": "ethereum",
        "explorer": "https://etherscan.io",
        "color": "#627eea",
    },
    "base": {
        "name": "Base",
        "rpc": "https://mainnet.base.org",
        "native": "ETH",
        "coingecko_id": "ethereum",
        "explorer": "https://basescan.org",
        "color": "#0052ff",
    },
    "arbitrum": {
        "name": "Arbitrum",
        "rpc": "https://arbitrum-one-rpc.publicnode.com",
        "native": "ETH",
        "coingecko_id": "ethereum",
        "explorer": "https://arbiscan.io",
        "color": "#28a0f0",
    },
    "optimism": {
        "name": "Optimism",
        "rpc": "https://optimism-rpc.publicnode.com",
        "native": "ETH",
        "coingecko_id": "ethereum",
        "explorer": "https://optimistic.etherscan.io",
        "color": "#ff0420",
    },
    "polygon": {
        "name": "Polygon",
        "rpc": "https://polygon-bor-rpc.publicnode.com",
        "native": "POL",
        "coingecko_id": "polygon-ecosystem-token",
        "explorer": "https://polygonscan.com",
        "color": "#8247e5",
    },
    "bsc": {
        "name": "BNB Chain",
        "rpc": "https://bsc-rpc.publicnode.com",
        "native": "BNB",
        "coingecko_id": "binancecoin",
        "explorer": "https://bscscan.com",
        "color": "#f0b90b",
    },
    "avalanche": {
        "name": "Avalanche",
        "rpc": "https://avalanche-c-chain-rpc.publicnode.com",
        "native": "AVAX",
        "coingecko_id": "avalanche-2",
        "explorer": "https://snowtrace.io",
        "color": "#e84142",
    },
    "linea": {
        "name": "Linea",
        "rpc": "https://linea-rpc.publicnode.com",
        "native": "ETH",
        "coingecko_id": "ethereum",
        "explorer": "https://lineascan.build",
        "color": "#000000",
    },
}

# Typical gas units per tx type
TX_TYPES = {
    "transfer":  {"label": "Native transfer", "gas": 21_000,  "icon": "↗"},
    "erc20":     {"label": "ERC-20 transfer", "gas": 65_000,  "icon": "≡"},
    "swap":      {"label": "Token swap",      "gas": 200_000, "icon": "⇄"},
    "nft_mint":  {"label": "NFT mint",        "gas": 150_000, "icon": "★"},
    "nft_xfer":  {"label": "NFT transfer",    "gas": 85_000,  "icon": "→"},
    "approve":   {"label": "Token approve",   "gas": 46_000,  "icon": "✓"},
}

# Simple in-memory cache (5s TTL)
_cache = {"data": None, "ts": 0}
CACHE_TTL = 5

# ============== ON-CHAIN ==============

def fetch_gas_price(rpc: str, timeout: float = 8.0) -> dict:
    """Return {gas_price_wei, base_fee, priority_fee} or {error}."""
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": timeout}))
        gas_price = w3.eth.gas_price  # legacy
        # Try EIP-1559
        try:
            block = w3.eth.get_block("latest")
            base = block.get("baseFeePerGas", 0)
            # Estimate priority fee (max-min from recent blocks)
            priority = max(1_000_000_000, gas_price - base) if base else 0
            return {
                "gas_price_wei": int(gas_price),
                "base_fee_wei": int(base) if base else None,
                "priority_fee_wei": int(priority) if base else None,
            }
        except Exception:
            return {"gas_price_wei": int(gas_price), "base_fee_wei": None, "priority_fee_wei": None}
    except Exception as e:
        return {"error": str(e)[:120]}


def fetch_all_gas_prices() -> dict:
    """Parallel fetch across all chains."""
    out = {}
    with ThreadPoolExecutor(max_workers=len(CHAINS)) as pool:
        futures = {pool.submit(fetch_gas_price, cfg["rpc"]): name for name, cfg in CHAINS.items()}
        for fut in futures:
            name = futures[fut]
            try:
                out[name] = fut.result(timeout=10)
            except Exception as e:
                out[name] = {"error": str(e)[:120]}
    return out


# ============== PRICES ==============

async def fetch_native_prices() -> dict:
    """Fetch native token USD prices via CoinGecko."""
    ids = list({cfg["coingecko_id"] for cfg in CHAINS.values()})
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params={"ids": ",".join(ids), "vs_currencies": "usd"})
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}


# ============== AGGREGATOR ==============

def gwei(wei: int) -> float:
    return round(wei / 1e9, 4) if wei else 0.0


def usd_cost(gas_wei: int, gas_units: int, native_price: float) -> float:
    return (gas_wei * gas_units) / 1e18 * native_price


async def build_response() -> dict:
    """Aggregate gas + prices + USD cost matrix."""
    if _cache["data"] and (time.time() - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    # Parallel: gas prices (sync) + token prices (async)
    loop = asyncio.get_event_loop()
    gas_task = loop.run_in_executor(None, fetch_all_gas_prices)
    price_task = asyncio.create_task(fetch_native_prices())
    gas_results, prices = await asyncio.gather(gas_task, price_task)

    chains = []
    for name, cfg in CHAINS.items():
        g = gas_results.get(name, {})
        cg_id = cfg["coingecko_id"]
        native_usd = prices.get(cg_id, {}).get("usd", 0)

        if "error" in g:
            chains.append({
                "id": name,
                "name": cfg["name"],
                "native": cfg["native"],
                "native_usd": native_usd,
                "color": cfg["color"],
                "explorer": cfg["explorer"],
                "error": g["error"],
            })
            continue

        gas_wei = g["gas_price_wei"]
        cost_matrix = {
            tx_id: round(usd_cost(gas_wei, info["gas"], native_usd), 6)
            for tx_id, info in TX_TYPES.items()
        }

        chains.append({
            "id": name,
            "name": cfg["name"],
            "native": cfg["native"],
            "native_usd": native_usd,
            "color": cfg["color"],
            "explorer": cfg["explorer"],
            "gas_gwei": gwei(gas_wei),
            "base_fee_gwei": gwei(g.get("base_fee_wei") or 0),
            "priority_gwei": gwei(g.get("priority_fee_wei") or 0),
            "cost_usd": cost_matrix,
        })

    # Sort: cheapest swap first
    chains.sort(key=lambda c: c.get("cost_usd", {}).get("swap", float("inf")))

    payload = {
        "ts": int(time.time()),
        "tx_types": TX_TYPES,
        "chains": chains,
    }
    _cache["data"] = payload
    _cache["ts"] = time.time()
    return payload


# ============== APP ==============

app = FastAPI(title="GasLens", description="Multi-chain gas price tracker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok", "chains": list(CHAINS.keys()), "tx_types": list(TX_TYPES.keys())}


@app.get("/api/gas")
async def gas():
    return await build_response()


STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
