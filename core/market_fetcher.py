"""
Fetches active 5-minute crypto markets (BTC, ETH, SOL, XRP).

Priority order:
  1. Gamma events API  — direct slug lookup ({coin}-updown-5m-{ts}), fastest
  2. Gamma markets API — broad search fallback
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import requests
from loguru import logger

from utils.helpers import parse_clob_token_ids, parse_outcome_prices

if TYPE_CHECKING:
    from core.browser import PolymarketBrowser

GAMMA_BASE = "https://gamma-api.polymarket.com"
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "polymarket-bot/1.0"

# Coin config: id → (slug_prefix, question_keywords)
_COIN_CONFIG: dict[str, tuple[str, list[str]]] = {
    "btc": ("btc-updown-5m", ["Bitcoin"]),
    "eth": ("eth-updown-5m", ["Ethereum"]),
    "sol": ("sol-updown-5m", ["Solana"]),
    "xrp": ("xrp-updown-5m", ["XRP", "Ripple"]),
}


@dataclass
class BtcMarket:
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    end_timestamp: float
    active: bool
    closed: bool
    slug: str = ""             # event slug e.g. btc-updown-5m-1772620800
    resolution: Optional[str] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _yes_idx(m: dict) -> int:
    """
    Return the index (0 or 1) of the Up/Yes outcome in the Gamma API arrays.
    Gamma can return outcomes as ["Up","Down"] OR ["Down","Up"] — we must check.
    Defaults to 0 if ambiguous.
    """
    raw = m.get("outcomes", ["Yes", "No"])
    if isinstance(raw, str):
        import json as _json
        try:
            raw = _json.loads(raw)
        except Exception:
            raw = ["Yes", "No"]
    for i, o in enumerate(raw):
        if str(o).strip().lower() in ("up", "yes"):
            return i
    return 0  # fallback


def _parse_market_dict(
    m: dict, slug: str = "", strict: bool = True, coin_keywords: Optional[list[str]] = None
) -> Optional[BtcMarket]:
    """Convert a raw Gamma market dict into BtcMarket. strict=False skips title filter."""
    question = m.get("question", "")
    if strict:
        kw = coin_keywords or ["Bitcoin"]
        five_min = "5 Minute" in question or "5-Minute" in question
        coin_match = any(k in question for k in kw)
        if not five_min or not coin_match:
            return None

    clob_ids = parse_clob_token_ids(m.get("clobTokenIds", []))
    if len(clob_ids) < 2:
        return None

    prices = parse_outcome_prices(m.get("outcomePrices", ["0.5", "0.5"]))
    end_date = m.get("endDate", "")
    end_ts = _iso_to_unix(end_date) if end_date else 0.0

    if end_ts and (end_ts - time.time()) < 30:
        return None  # too late to bet

    yi = _yes_idx(m)
    ni = 1 - yi

    return BtcMarket(
        condition_id=m.get("conditionId", ""),
        question=question or slug,
        slug=slug,
        yes_token_id=clob_ids[yi],
        no_token_id=clob_ids[ni],
        yes_price=prices[yi] if len(prices) > yi else 0.5,
        no_price=prices[ni] if len(prices) > ni else 0.5,
        end_timestamp=end_ts,
        active=True,
        closed=False,
    )


def _fetch_event_by_slug(slug: str) -> Optional[BtcMarket]:
    """Hit gamma-api /events?slug=... and return the first valid market inside."""
    try:
        resp = _SESSION.get(
            f"{GAMMA_BASE}/events",
            params={"slug": slug},
            timeout=8,
        )
        if resp.status_code != 200:
            logger.debug(f"Events API {slug} → HTTP {resp.status_code}")
            return None
        events = resp.json()
        if not isinstance(events, list) or not events:
            return None
        for event in events:
            for m in event.get("markets", []):
                result = _parse_market_dict(m, slug=slug, strict=False)
                if result:
                    return result
    except Exception as exc:
        logger.debug(f"Events API error for {slug}: {exc}")
    return None


def _fetch_markets_api(params: dict) -> list[dict]:
    try:
        resp = _SESSION.get(f"{GAMMA_BASE}/markets", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error(f"Gamma markets API error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def find_next_5min_market(
    coin: str = "btc",
    browser: Optional["PolymarketBrowser"] = None,
) -> Optional[BtcMarket]:
    """
    Return the next open 5-min market for `coin` (btc/eth/sol/xrp) or None.

    1. Try Gamma events API with computed slug for current + adjacent windows
    2. Fall back to broad markets API search
    """
    from core.browser import current_5min_ts

    coin = coin.lower()
    slug_prefix, coin_keywords = _COIN_CONFIG.get(coin, ("btc-updown-5m", ["Bitcoin"]))

    # --- Primary: events API with slug ---
    for offset in (0, 1, -1, 2):
        ts = current_5min_ts(offset)
        slug = f"{slug_prefix}-{ts}"
        market = _fetch_event_by_slug(slug)
        if market:
            logger.info(f"[{coin.upper()}] Market found via events API: {slug}")
            return market

    # --- Fallback: broad markets search ---
    logger.debug(f"[{coin.upper()}] Events API found nothing — trying broad markets search")
    for m in _fetch_markets_api({"active": "true", "closed": "false", "limit": 100}):
        result = _parse_market_dict(m, strict=True, coin_keywords=coin_keywords)
        if result:
            logger.info(f"[{coin.upper()}] Market found via markets API: {result.question}")
            return result

    logger.warning(f"No {coin.upper()} 5-min market found")
    return None


def find_next_btc_5min_market(
    browser: Optional["PolymarketBrowser"] = None,
) -> Optional[BtcMarket]:
    """Backward-compatible wrapper for BTC."""
    return find_next_5min_market("btc", browser)


def _extract_resolution(m: dict) -> Optional[str]:
    """
    Try every known field pattern to extract a Yes/No resolution from a
    Gamma market dict.  Returns None if the market hasn't resolved yet.
    """
    # 1. Explicit resolution string
    res = str(m.get("resolution") or "").strip().lower()
    if res in ("yes", "1", "true", "up"):
        return "Yes"
    if res in ("no", "0", "false", "down"):
        return "No"

    # 2. closed flag + outcomePrices (≥0.99 threshold handles rounding)
    if m.get("closed"):
        prices = parse_outcome_prices(m.get("outcomePrices", ["0", "0"]))
        yi = _yes_idx(m)
        ni = 1 - yi
        if len(prices) > yi and prices[yi] >= 0.99:
            return "Yes"
        if len(prices) > ni and prices[ni] >= 0.99:
            return "No"

        # 3. winner field (some markets use this)
        w = str(m.get("winner") or "").strip().lower()
        if w in ("yes", "1", "true", "up"):
            return "Yes"
        if w in ("no", "0", "false", "down"):
            return "No"

    return None


def check_window_outcome(window_ts: int, coin: str = "btc") -> Optional[str]:
    """
    Return 'Yes', 'No', or None for the 5-min window that started at `window_ts`.
    Works for btc, eth, sol, xrp.

    Priority:
      1. Binance 5-min candle — available the instant the candle closes (~0 lag)
      2. Gamma API events    — fallback if Binance unavailable
    """
    # --- Fast path: Binance candle ---
    from core.price_feed import get_coin_window_outcome
    result = get_coin_window_outcome(window_ts, coin)
    if result:
        return result

    # --- Fallback: Gamma API ---
    slug_prefix = _COIN_CONFIG.get(coin.lower(), ("btc-updown-5m", []))[0]
    slug = f"{slug_prefix}-{window_ts}"
    try:
        resp = _SESSION.get(
            f"{GAMMA_BASE}/events",
            params={"slug": slug},
            timeout=8,
        )
        if resp.status_code != 200:
            logger.debug(f"check_window_outcome {slug} → HTTP {resp.status_code}")
            return None
        events = resp.json()
        for event in (events if isinstance(events, list) else []):
            for m in event.get("markets", []):
                result = _extract_resolution(m)
                if result:
                    logger.info(f"Gamma API: {coin.upper()} window {window_ts} resolved → {result}")
                    return result
    except Exception as exc:
        logger.debug(f"check_window_outcome({coin}, {window_ts}): {exc}")
    return None


def poll_resolution(
    condition_id: str,
    timeout_seconds: int = 900,
    slug: str = "",
) -> Optional[str]:
    """
    Poll until market resolves.  Returns 'Yes', 'No', or None on timeout.

    Tries three lookup strategies per tick:
      1. Markets API by conditionId (with and without 0x prefix)
      2. Events API by slug (if provided) — often resolves faster
    """
    deadline = time.time() + timeout_seconds
    tick = 0
    # Strip leading 0x so we can try both forms
    cid_bare = condition_id[2:] if condition_id.startswith("0x") else condition_id
    cid_full = "0x" + cid_bare

    while time.time() < deadline:
        # --- Strategy 1: markets API ---
        for cid in (cid_full, cid_bare):
            markets = _fetch_markets_api({"conditionId": cid, "limit": 1})
            if markets:
                result = _extract_resolution(markets[0])
                if result:
                    logger.info(f"Resolved via markets API ({cid[:12]}…): {result}")
                    return result
                break  # got a valid response, don't retry bare form

        # --- Strategy 2: events API by slug ---
        if slug:
            try:
                resp = _SESSION.get(
                    f"{GAMMA_BASE}/events",
                    params={"slug": slug},
                    timeout=8,
                )
                if resp.status_code == 200:
                    events = resp.json()
                    for event in (events if isinstance(events, list) else []):
                        for m in event.get("markets", []):
                            result = _extract_resolution(m)
                            if result:
                                logger.info(f"Resolved via events API ({slug}): {result}")
                                return result
            except Exception as exc:
                logger.debug(f"Events API resolution check failed: {exc}")

        tick += 1
        if tick % 4 == 0:  # every ~60 s
            elapsed = int(time.time() + timeout_seconds - deadline + timeout_seconds - (deadline - time.time()))
            remaining = int(deadline - time.time())
            logger.info(
                f"Awaiting resolution… {remaining}s left "
                f"(condition={condition_id[:14]}… slug={slug or 'n/a'})"
            )
        time.sleep(15)

    logger.warning(f"Resolution timeout for {condition_id}")
    return None


def _iso_to_unix(iso: str) -> float:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0
