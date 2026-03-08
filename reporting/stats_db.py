"""
SQLite database for resolved bets.

Records every bet outcome so we can query 24h / 7d / 14d / 30d windows.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_DEFAULT_PATH = Path(__file__).parent.parent / "reporting_stats.db"


class StatsDB:
    def __init__(self, path: str | Path = _DEFAULT_PATH):
        self._path = str(path)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    resolved_at INTEGER NOT NULL,
                    coin        TEXT    NOT NULL,
                    side        TEXT    NOT NULL,
                    bet_usd     REAL    NOT NULL,
                    outcome     TEXT    NOT NULL,
                    won         INTEGER NOT NULL,
                    profit_usd  REAL    NOT NULL,
                    unit        INTEGER NOT NULL DEFAULT 1,
                    price       REAL    NOT NULL DEFAULT 0.5,
                    slug        TEXT    NOT NULL DEFAULT ''
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_resolved_at ON bets (resolved_at)"
            )
            conn.commit()

    def record_bet(
        self,
        coin: str,
        side: str,
        bet_usd: float,
        outcome: str,
        profit_usd: float,
        unit: int = 1,
        price: float = 0.5,
        slug: str = "",
    ) -> None:
        """Insert a resolved bet into the database."""
        won = 1 if outcome == side else 0
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """INSERT INTO bets
                   (resolved_at, coin, side, bet_usd, outcome, won, profit_usd, unit, price, slug)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(time.time()), coin, side, bet_usd, outcome, won,
                 profit_usd, unit, price, slug),
            )
            conn.commit()

    def stats_for_window(self, hours: int) -> dict:
        """Return aggregate stats for the last `hours` hours."""
        since = int(time.time()) - hours * 3600
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM bets WHERE resolved_at >= ?", (since,)
            ).fetchall()

        if not rows:
            return {
                "hours": hours, "total": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "pnl": 0.0, "wagered": 0.0, "roi": 0.0,
                "coins": {},
            }

        total    = len(rows)
        wins     = sum(r["won"] for r in rows)
        losses   = total - wins
        pnl      = sum(r["profit_usd"] for r in rows)
        wagered  = sum(r["bet_usd"] for r in rows)
        win_rate = wins / total * 100 if total else 0.0
        roi      = pnl / wagered * 100 if wagered else 0.0

        # Per-coin breakdown
        coins: dict[str, dict] = {}
        for r in rows:
            c = r["coin"]
            if c not in coins:
                coins[c] = {"wins": 0, "losses": 0, "pnl": 0.0, "wagered": 0.0}
            coins[c]["wins"]   += r["won"]
            coins[c]["losses"] += 1 - r["won"]
            coins[c]["pnl"]    += r["profit_usd"]
            coins[c]["wagered"] += r["bet_usd"]

        for c_data in coins.values():
            t = c_data["wins"] + c_data["losses"]
            c_data["win_rate"] = c_data["wins"] / t * 100 if t else 0.0
            c_data["roi"] = (
                c_data["pnl"] / c_data["wagered"] * 100 if c_data["wagered"] else 0.0
            )

        return {
            "hours":    hours,
            "total":    total,
            "wins":     wins,
            "losses":   losses,
            "win_rate": win_rate,
            "pnl":      pnl,
            "wagered":  wagered,
            "roi":      roi,
            "coins":    coins,
        }
