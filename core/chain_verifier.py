"""
On-chain bet verifier for Polymarket (Polygon).

Uses a free public RPC to check whether a USDC transfer was sent from the
wallet in a given 5-minute window. This is ground truth — if the chain shows
a transfer, the bet happened regardless of what the browser reported.

Used in run_cycle to prevent double-betting on crash/restart.

Polygon block time ≈ 2s, so 5 minutes ≈ 150 blocks.
We scan 200 blocks for safety margin.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx
from loguru import logger

# Free public Polygon RPCs — tried in order, first success wins
_RPC_ENDPOINTS = [
    "https://polygon-bor-rpc.publicnode.com",   # fastest, most reliable
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
    "https://rpc-mainnet.maticvigil.com",
]

# USDC.e (bridged USDC) — the token Polymarket uses on Polygon
USDC_CONTRACT = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Polymarket's main USDC receiver contract
POLYMARKET_CONTRACT = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"

# Blocks to scan per query (200 blocks ≈ 6.5 minutes on Polygon @ ~2s/block)
SCAN_BLOCKS = 200

# Simple in-memory cache: (block_number, logs) — refreshed when stale
_cache: dict = {}
_CACHE_TTL = 60  # seconds


def _rpc(method: str, params: list, timeout: int = 4) -> dict:
    """Try each RPC endpoint in turn; return the first successful response."""
    last_exc: Exception = RuntimeError("no RPC endpoints configured")
    for url in _RPC_ENDPOINTS:
        try:
            r = httpx.post(
                url,
                json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                timeout=timeout,
            )
            if r.status_code != 200 or not r.content:
                logger.debug(f"chain_verifier: {url} → HTTP {r.status_code}")
                continue
            data = r.json()
            if "error" not in data:
                return data
        except Exception as exc:
            last_exc = exc
            logger.debug(f"chain_verifier: {url} failed — {type(exc).__name__}")
    raise last_exc


def _get_latest_block() -> int:
    result = _rpc("eth_blockNumber", [])
    return int(result["result"], 16)


def _get_block_timestamp(block_number: int) -> int:
    result = _rpc("eth_getBlockByNumber", [hex(block_number), False])
    return int(result["result"]["timestamp"], 16)


def _get_outgoing_transfers(wallet: str, from_block: int) -> list[dict]:
    """Fetch USDC.e Transfer logs where `from` == wallet."""
    wallet_lower = wallet.lower()
    wallet_topic = "0x000000000000000000000000" + wallet_lower[2:]

    result = _rpc("eth_getLogs", [{
        "address": USDC_CONTRACT,
        "topics": [TRANSFER_TOPIC, wallet_topic],
        "fromBlock": hex(from_block),
        "toBlock": "latest",
    }])
    return result.get("result") or []


def _cached_logs(wallet: str) -> tuple[int, list[dict]]:
    """Return (latest_block, logs) using a 30s cache to avoid hammering the RPC."""
    now = time.time()
    if _cache.get("ts", 0) + _CACHE_TTL > now:
        return _cache["latest"], _cache["logs"]

    try:
        latest = _get_latest_block()
        from_block = max(0, latest - SCAN_BLOCKS)
        logs = _get_outgoing_transfers(wallet, from_block)
        _cache["latest"] = latest
        _cache["logs"] = logs
        _cache["ts"] = now
        return latest, logs
    except Exception as exc:
        have_cache = "ts" in _cache
        log = logger.debug if have_cache else logger.warning
        log(f"chain_verifier: all RPCs failed ({type(exc).__name__}) — {'using stale cache' if have_cache else 'no cache'}")
        return _cache.get("latest", 0), _cache.get("logs", [])


def has_bet_in_window(window_ts: int, wallet: str) -> bool:
    """
    Return True if the wallet sent USDC.e to Polymarket during the
    5-minute window starting at `window_ts`.

    window_ts is a Unix timestamp aligned to the 5-minute boundary
    (as returned by current_5min_ts).
    """
    window_end = window_ts + 300
    wallet_lower = wallet.lower()
    polymarket_lower = POLYMARKET_CONTRACT.lower()

    try:
        latest, logs = _cached_logs(wallet)
        if not logs:
            return False

        # We need block timestamps to compare with window_ts.
        # Batch: collect unique block numbers in the logs.
        block_nums = list({int(log["blockNumber"], 16) for log in logs})

        # Fetch timestamps for those blocks (they're recent so fast)
        ts_map: dict[int, int] = {}
        for bn in block_nums:
            try:
                ts_map[bn] = _get_block_timestamp(bn)
            except Exception:
                pass

        for log in logs:
            bn = int(log["blockNumber"], 16)
            block_ts = ts_map.get(bn, 0)
            if block_ts == 0:
                continue

            if not (window_ts <= block_ts < window_end):
                continue

            # Confirm recipient is Polymarket
            to_addr = "0x" + log["topics"][2][-40:]
            if to_addr.lower() == polymarket_lower:
                amount = int(log["data"], 16) / 1e6
                logger.info(
                    f"On-chain bet confirmed in window {window_ts}: "
                    f"${amount:.2f} USDC at block {bn} (ts={block_ts})"
                )
                return True

        return False

    except Exception as exc:
        logger.warning(f"chain_verifier.has_bet_in_window error: {exc}")
        return False  # Fail open — don't block a bet if verification fails


def get_recent_bets(wallet: str, limit: int = 10) -> list[dict]:
    """
    Return the most recent USDC.e transfers to Polymarket from the wallet.
    Each entry: {block, timestamp, amount_usdc, tx_hash}
    """
    try:
        latest, logs = _cached_logs(wallet)
        polymarket_lower = POLYMARKET_CONTRACT.lower()

        results = []
        seen_blocks: dict[int, int] = {}

        for log in reversed(logs):  # oldest first
            to_addr = "0x" + log["topics"][2][-40:]
            if to_addr.lower() != polymarket_lower:
                continue

            bn = int(log["blockNumber"], 16)
            if bn not in seen_blocks:
                try:
                    seen_blocks[bn] = _get_block_timestamp(bn)
                except Exception:
                    seen_blocks[bn] = 0

            results.append({
                "block": bn,
                "timestamp": seen_blocks[bn],
                "amount_usdc": int(log["data"], 16) / 1e6,
                "tx_hash": log["transactionHash"],
            })
            if len(results) >= limit:
                break

        return list(reversed(results))  # newest first

    except Exception as exc:
        logger.warning(f"chain_verifier.get_recent_bets error: {exc}")
        return []
