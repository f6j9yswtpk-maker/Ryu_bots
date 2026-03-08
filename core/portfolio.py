"""
Portfolio history — fetches recent trade history to compute actual P&L.

Priority:
  1. Authenticated CLOB client  (get_trades via py-clob-client)
  2. Polymarket Data API        (data-api.polymarket.com — public, no auth)
  3. cycle_db fallback          (handled in main.py)
"""
from __future__ import annotations

import time
from typing import Optional

import requests
from loguru import logger

DATA_API = "https://data-api.polymarket.com"

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "polymarket-bot/1.0"


# ---------------------------------------------------------------------------
# Fetchers — try authed client first, public Data API second
# ---------------------------------------------------------------------------

def fetch_recent_trades(wallet: str, client=None, limit: int = 20) -> list[dict]:
    """
    Return recent BUY trades oldest-first.
    Tries authenticated CLOB client, then public Data API.
    """
    # 1. Authenticated CLOB client
    if client is not None:
        trades = _fetch_via_client(client, wallet, limit)
        if trades:
            return trades

    # 2. Public Data API
    trades = _fetch_via_data_api(wallet, limit)
    if trades:
        return trades

    logger.warning("No trade history available — will fall back to cycle_db")
    return []


def _fetch_via_client(client, wallet: str, limit: int) -> list[dict]:
    try:
        from py_clob_client.clob_types import TradeParams
        params = TradeParams(maker_address=wallet, limit=limit)
        resp = client.get_trades(params)
        trades = resp if isinstance(resp, list) else (resp or {}).get("data", [])
        if trades:
            logger.debug(f"CLOB client: {len(trades)} trades fetched")
            return list(reversed(trades))   # oldest first
    except Exception as exc:
        logger.debug(f"CLOB client trades failed: {exc}")
    return []


def _fetch_via_data_api(wallet: str, limit: int) -> list[dict]:
    """Polymarket Data API — public, no authentication needed."""
    for path in ("/activity", "/trades"):
        try:
            resp = _SESSION.get(
                f"{DATA_API}{path}",
                params={"user": wallet, "limit": limit},
                timeout=8,
            )
            if resp.status_code != 200:
                logger.debug(f"Data API {path} → HTTP {resp.status_code}")
                continue
            body = resp.json()
            trades = body if isinstance(body, list) else body.get("data", [])
            if trades:
                logger.debug(f"Data API {path}: {len(trades)} records")
                return list(reversed(trades))   # oldest first
        except Exception as exc:
            logger.debug(f"Data API {path} failed: {exc}")
    return []


# ---------------------------------------------------------------------------
# Outcome detection
# ---------------------------------------------------------------------------

def _token_outcome(asset_id: str, order_manager) -> Optional[bool]:
    """
    True = won, False = lost, None = still active / unknown.
    Uses current CLOB midpoint: ≥0.95 → won, ≤0.05 → lost.
    """
    if not asset_id or order_manager is None:
        return None
    try:
        price = order_manager.get_midpoint(asset_id)
        if price is None:
            return None
        if price >= 0.95:
            return True
        if price <= 0.05:
            return False
    except Exception:
        pass
    return None


def _parse_trade(t: dict) -> tuple[float, float, str]:
    """Extract (price, size, asset_id) from a trade record (handles both API formats)."""
    # CLOB format
    price = float(t.get("price", t.get("feeRateBps", 0)) or 0)
    size  = float(t.get("size",  t.get("usdcSize",  0)) or 0)
    asset = t.get("asset_id", t.get("assetId", t.get("outcome_id", ""))) or ""
    # Data API may express cost in USDC directly
    if size == 0:
        size = float(t.get("usdcSize", 0) or 0)
        if size > 0 and price > 0:
            size = size / price  # convert USDC cost → shares
    return price, size, asset


# ---------------------------------------------------------------------------
# Deficit / bet calculators
# ---------------------------------------------------------------------------

def compute_dalembert_deficit(
    trades: list[dict],
    order_manager,
    base_unit: float,
    max_cap: int,
    lookback_seconds: int = 7200,
) -> float:
    """
    D'Alembert dollar deficit from real trade history (oldest first).

      win  → deficit -= actual_profit  (shares × (1 − price))
      loss → deficit += bet_usd        (price × shares)
      pending → deficit += bet_usd     (conservative)

    Returns base_unit + max(0, deficit), capped at max_cap × base_unit.
    """
    cutoff = time.time() - lookback_seconds
    deficit = 0.0

    for t in trades:
        try:
            ts = float(t.get("timestamp", t.get("createdAt", 0)) or 0)
            if ts > 1e12:
                ts /= 1000          # ms → s
            if ts and ts < cutoff:
                continue

            price, size, asset = _parse_trade(t)
            if size <= 0 or price <= 0:
                continue

            bet_usd = price * size
            outcome = _token_outcome(asset, order_manager)

            if outcome is True:
                profit = size * (1.0 - price)
                deficit -= profit
                logger.debug(f"  TRADE WIN  bet=${bet_usd:.2f} profit=${profit:.2f}")
            elif outcome is False:
                deficit += bet_usd
                logger.debug(f"  TRADE LOSS bet=${bet_usd:.2f}")
            else:
                deficit += bet_usd      # pending / unknown → conservative
                logger.debug(f"  TRADE PEND bet=${bet_usd:.2f}")

        except Exception:
            continue

    result = round(min(max_cap * base_unit, base_unit + max(0.0, deficit)), 4)
    logger.info(f"D'Alembert deficit=${deficit:.2f} → next bet=${result:.2f}")
    return result


def compute_paroli_bet(
    trades: list[dict],
    order_manager,
    base_unit: float,
    max_cap: int,
    max_streak: int = 3,
    lookback_seconds: int = 7200,
) -> float:
    """
    Paroli next bet from real trade history (oldest first).

      win  → press: next_bet = current_bet + actual_profit
      loss or max_streak → reset to base_unit
    """
    cutoff = time.time() - lookback_seconds
    bet = base_unit
    streak = 0

    for t in trades:
        try:
            ts = float(t.get("timestamp", t.get("createdAt", 0)) or 0)
            if ts > 1e12:
                ts /= 1000
            if ts and ts < cutoff:
                continue

            price, size, asset = _parse_trade(t)
            if size <= 0 or price <= 0:
                continue

            current_bet = price * size
            outcome = _token_outcome(asset, order_manager)

            if outcome is True:
                profit = size * (1.0 - price)
                streak += 1
                if streak >= max_streak:
                    bet = base_unit
                    streak = 0
                else:
                    bet = min(max_cap * base_unit, current_bet + profit)
            else:
                bet = base_unit
                streak = 0

        except Exception:
            continue

    logger.info(f"Paroli streak={streak} → next bet=${bet:.2f}")
    return round(bet, 4)
