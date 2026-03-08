"""
BTC price momentum signal.

Primary:  Binance public REST API (1-min candles)
Fallback: browser page scrape (no external API token needed)
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

import requests
from loguru import logger

if TYPE_CHECKING:
    from core.browser import PolymarketBrowser

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "polymarket-bot/1.0"

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


def get_btc_direction() -> str:
    """
    Returns 'Yes' (BTC going up) or 'No' (BTC going down) based on
    recent 1-minute candle momentum. Defaults to 'Yes' on any error.
    """
    try:
        resp = _SESSION.get(
            BINANCE_KLINES,
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": 6},
            timeout=5,
        )
        resp.raise_for_status()
        candles = resp.json()

        # Drop the current forming candle — use last 5 completed ones
        completed = candles[:-1]
        if len(completed) < 3:
            return "Yes"

        opens  = [float(c[1]) for c in completed]
        closes = [float(c[4]) for c in completed]

        # Weighted vote: recent candles count more
        weights = list(range(1, len(completed) + 1))
        bull_score = sum(w for w, o, c in zip(weights, opens, closes) if c > o)
        bear_score = sum(w for w, o, c in zip(weights, opens, closes) if c <= o)

        direction = "Yes" if bull_score >= bear_score else "No"

        last_close  = closes[-1]
        last_open   = opens[-1]
        pct_move    = (last_close - last_open) / last_open * 100
        logger.info(
            f"BTC momentum: {direction} | last candle {pct_move:+.3f}% "
            f"| bull={bull_score} bear={bear_score}"
        )
        return direction

    except Exception as exc:
        logger.warning(f"Price feed error: {exc} — defaulting to Yes")
        return "Yes"


_COIN_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}


def get_coin_direction(coin: str = "btc") -> str:
    """Generic version of get_btc_direction() for any Binance symbol."""
    symbol = _COIN_SYMBOLS.get(coin.lower(), "BTCUSDT")
    try:
        resp = _SESSION.get(
            BINANCE_KLINES,
            params={"symbol": symbol, "interval": "1m", "limit": 6},
            timeout=5,
        )
        resp.raise_for_status()
        candles = resp.json()
        completed = candles[:-1]
        if len(completed) < 3:
            return "Yes"
        opens  = [float(c[1]) for c in completed]
        closes = [float(c[4]) for c in completed]
        weights = list(range(1, len(completed) + 1))
        bull_score = sum(w for w, o, c in zip(weights, opens, closes) if c > o)
        bear_score = sum(w for w, o, c in zip(weights, opens, closes) if c <= o)
        return "Yes" if bull_score >= bear_score else "No"
    except Exception:
        return "Yes"


def get_coin_price(coin: str = "btc") -> Optional[float]:
    """Return the latest price for a coin from Binance."""
    symbol = _COIN_SYMBOLS.get(coin.lower(), "BTCUSDT")
    try:
        resp = _SESSION.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=5,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception:
        return None


def get_coin_window_outcome(window_ts: int, coin: str = "btc") -> Optional[str]:
    """
    Returns 'Yes' (coin closed up) or 'No' (coin closed down) for the completed
    5-minute window that started at `window_ts` (Unix seconds).

    Works for btc, eth, sol, xrp — any symbol in _COIN_SYMBOLS.
    Uses Binance klines with no Gamma lag. Returns None if candle hasn't closed.
    """
    symbol = _COIN_SYMBOLS.get(coin.lower(), "BTCUSDT")
    try:
        start_ms = window_ts * 1000
        resp = _SESSION.get(
            BINANCE_KLINES,
            params={"symbol": symbol, "interval": "5m", "startTime": start_ms, "limit": 1},
            timeout=5,
        )
        resp.raise_for_status()
        candles = resp.json()
        if not candles:
            return None

        c = candles[0]
        open_time_ms  = int(c[0])
        close_time_ms = int(c[6])
        open_price    = float(c[1])
        close_price   = float(c[4])

        if open_time_ms != start_ms:
            logger.debug(f"[{coin.upper()}] Candle mismatch: expected {start_ms}, got {open_time_ms}")
            return None

        if close_time_ms >= int(time.time() * 1000):
            return None  # still forming

        result = "Yes" if close_price > open_price else "No"
        pct = (close_price - open_price) / open_price * 100
        logger.info(
            f"Binance 5m {coin.upper()} candle {window_ts}: {result} | {pct:+.3f}% "
            f"(open={open_price:.4f} close={close_price:.4f})"
        )
        return result

    except Exception as exc:
        logger.debug(f"get_coin_window_outcome({coin}, {window_ts}): {exc}")
        return None


def get_btc_window_outcome(window_ts: int) -> Optional[str]:
    """Backward-compatible wrapper — use get_coin_window_outcome instead."""
    return get_coin_window_outcome(window_ts, "btc")


def get_btc_price(browser: "PolymarketBrowser | None" = None) -> float | None:
    """Return current BTC/USDT price. Tries Binance first, browser fallback."""
    # Binance API
    try:
        resp = _SESSION.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=5,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception:
        pass

    # Browser fallback — extract price from Polymarket event page
    if browser and browser.ready:
        return browser.get_btc_price_from_page()

    return None
