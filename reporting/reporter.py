"""
Daily report orchestration.

Builds 24h/7d/14d/30d performance summaries and sends them via
email (ProtonMail) and X (Twitter) once per day at 00:05 UTC.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from loguru import logger

from reporting.stats_db import StatsDB
from reporting.emailer import send_report
from reporting.twitter import post_tweet

_COIN_LABEL = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP"}


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def _coin_line(coin: str, data: dict) -> str:
    label = _COIN_LABEL.get(coin, coin.upper())
    sign  = "+" if data["pnl"] >= 0 else ""
    tick  = "✅" if data["pnl"] >= 0 else "❌"
    return (
        f"{label} {sign}${data['pnl']:.2f} "
        f"({data['wins']}W/{data['losses']}L) "
        f"{data['win_rate']:.0f}% {tick}"
    )


def _window_block(s: dict, label: str) -> str:
    if s["total"] == 0:
        return f"  {label}: No data yet\n"
    sign  = "+" if s["pnl"] >= 0 else ""
    lines = [f"  {label}  ({s['total']} bets)"]
    lines.append(f"    P&L:     {sign}${s['pnl']:.2f}  |  ROI: {sign}{s['roi']:.1f}%")
    lines.append(f"    W/L:     {s['wins']}/{s['losses']}  ({s['win_rate']:.1f}% win rate)")
    lines.append(f"    Wagered: ${s['wagered']:.2f}")
    for coin in ("btc", "eth", "sol", "xrp"):
        if coin in s["coins"]:
            cd   = s["coins"][coin]
            cs   = "+" if cd["pnl"] >= 0 else ""
            lines.append(
                f"    {coin.upper()}: {cs}${cd['pnl']:.2f}  "
                f"W/L {cd['wins']}/{cd['losses']}  ROI {cs}{cd['roi']:.1f}%"
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def build_tweet(stats_24h: dict) -> str:
    """Build a tweet ≤ 280 chars summarising the last 24 h."""
    signup_link = os.getenv("SIGNUP_LINK", "https://polymarket.com")
    sign = "+" if stats_24h["pnl"] >= 0 else ""

    lines = ["📊 Tool Ryū — 24h Paper Trading Report"]
    for coin in ("btc", "eth", "sol", "xrp"):
        if coin in stats_24h["coins"]:
            lines.append(_coin_line(coin, stats_24h["coins"][coin]))
    lines.append(
        f"Total P&L: {sign}${stats_24h['pnl']:.2f} | ROI: {sign}{stats_24h['roi']:.1f}%"
    )
    lines.append(f"🚀 {signup_link}")

    tweet = "\n".join(lines)
    if len(tweet) > 280:
        tweet = tweet[:277] + "..."
    return tweet


def build_email(db: StatsDB, mode: str) -> tuple[str, str]:
    """Return (subject, plain-text body) for the daily email."""
    signup_link = os.getenv("SIGNUP_LINK", "https://polymarket.com")
    s24  = db.stats_for_window(24)
    s7   = db.stats_for_window(24 * 7)
    s14  = db.stats_for_window(24 * 14)
    s30  = db.stats_for_window(24 * 30)
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sep  = "-" * 52

    subject = f"[Tool Ryū] Daily Report — {now} | P&L ${s24['pnl']:+.2f}"

    body = f"""Tool Ryū — Daily Performance Report
{sep}
Mode      : {mode}
Generated : {now}
{sep}

{_window_block(s24, "Last 24 Hours")}
{_window_block(s7,  "Last 7 Days")}
{_window_block(s14, "Last 14 Days")}
{_window_block(s30, "Last 30 Days")}
{sep}
Want to try Tool Ryū? → {signup_link}
"""
    return subject, body


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def send_daily_report(db: StatsDB, mode: str = "PAPER") -> None:
    logger.info("[Reporter] Generating daily report…")
    try:
        subject, body = build_email(db, mode)
        send_report(subject, body)

        stats_24h = db.stats_for_window(24)
        post_tweet(build_tweet(stats_24h))
    except Exception as exc:
        logger.error(f"[Reporter] Daily report error: {exc}")


# ---------------------------------------------------------------------------
# Scheduler — fires at 00:05 UTC every day
# ---------------------------------------------------------------------------

def start_daily_scheduler(
    db: StatsDB,
    mode_fn: Optional[Callable[[], str]] = None,
) -> None:
    """Start a daemon thread that delivers the daily report at 00:05 UTC."""

    def _loop() -> None:
        while True:
            now       = datetime.now(timezone.utc)
            next_fire = now.replace(hour=0, minute=5, second=0, microsecond=0)
            if now >= next_fire:
                next_fire += timedelta(days=1)
            wait = (next_fire - now).total_seconds()
            logger.info(f"[Reporter] Next daily report in {wait / 3600:.1f} h")
            time.sleep(wait)
            mode = mode_fn() if mode_fn else "PAPER"
            send_daily_report(db, mode)

    t = threading.Thread(target=_loop, name="daily-reporter", daemon=True)
    t.start()
