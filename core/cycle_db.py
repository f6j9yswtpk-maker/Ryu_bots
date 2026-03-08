"""
Cycle database — a self-cleaning ring-buffer of the last N bet cycles.

Each record captures everything needed to make the next bet decision:
  window_ts   — 5-min window start (unix)
  slug        — market slug
  url         — full Polymarket URL
  side        — "Yes" / "No"
  bet_usd     — amount wagered
  unit        — D'Alembert display unit at time of bet
  price       — market price at bet time
  outcome     — "Yes" / "No" once resolved, None if still pending
  ts_placed   — unix timestamp when bet was placed
  ts_resolved — unix timestamp when outcome was confirmed

For D'Alembert we use strict unit stepping:
  - loss  -> unit + 1
  - win   -> unit - 1 (floor 1)
  - unit capped at max_cap
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

MAX_CYCLES = 10


class CycleDB:
    """Persistent ring-buffer of the last MAX_CYCLES bet cycles."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._records: list[dict] = []
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_cycle(
        self,
        window_ts: int,
        slug: str,
        side: str,
        bet_usd: float,
        unit: int,
        url: str = "",
        price: float = 0.5,
    ) -> None:
        """Record a new pending bet. Overwrites any existing record for the same window."""
        self._records = [r for r in self._records if r["window_ts"] != window_ts]
        self._records.append({
            "window_ts": window_ts,
            "slug": slug,
            "url": url,
            "side": side,
            "bet_usd": round(bet_usd, 4),
            "unit": unit,
            "price": round(max(0.01, min(0.99, price)), 4),
            "outcome": None,
            "ts_placed": time.time(),
            "ts_resolved": None,
        })
        self._trim()
        self._save()

    def close_cycle(self, window_ts: int, outcome: str) -> None:
        """Mark a cycle as resolved with the given outcome."""
        for r in self._records:
            if r["window_ts"] == window_ts:
                r["outcome"] = outcome
                r["ts_resolved"] = time.time()
                break
        self._save()

    def pending(self) -> Optional[dict]:
        """Return the oldest unresolved cycle, or None."""
        unresolved = sorted(
            [r for r in self._records if r["outcome"] is None],
            key=lambda x: x["window_ts"],
        )
        return unresolved[0] if unresolved else None

    def all_pending(self) -> list[dict]:
        """Return all unresolved cycles, oldest first."""
        return sorted(
            [r for r in self._records if r["outcome"] is None],
            key=lambda x: x["window_ts"],
        )

    def last_resolved(self) -> Optional[dict]:
        """Return the most recently resolved cycle, or None."""
        resolved = [r for r in self._records if r["outcome"] is not None]
        return resolved[-1] if resolved else None

    def next_bet_usd(self, base_unit: float, max_cap: int = 12) -> float:
        """
        Strict D'Alembert sizing from unit progression.

        Next bet = base_unit * next_unit().
        """
        unit = self.next_unit(max_cap=max_cap, include_pending=True)
        return round(base_unit * unit, 4)

    def next_paroli_bet_usd(
        self, base_unit: float, max_streak: int = 3, max_cap: int = 8
    ) -> float:
        """
        Dollar-based Paroli: press actual winnings on each win.

        Win  → next_bet = current_bet + actual_profit  (real payout at that price)
        Loss → reset to base_unit
        After max_streak wins → reset to base_unit (take profit)
        Pending → conservative reset to base_unit

        Capped at base_unit * max_cap.
        """
        bet = base_unit
        streak = 0
        for r in sorted(self._records, key=lambda x: x["window_ts"]):
            side_bet = r.get("bet_usd", base_unit)
            price = max(0.01, min(0.99, r.get("price", 0.5)))
            if r["outcome"] is None:
                bet = base_unit
                streak = 0
            elif r["outcome"] == r["side"]:
                profit = side_bet * (1.0 - price) / price
                streak += 1
                if streak >= max_streak:
                    bet = base_unit
                    streak = 0
                else:
                    bet = min(max_cap * base_unit, side_bet + profit)
            else:
                bet = base_unit
                streak = 0
        return round(bet, 4)

    def next_unit(self, max_cap: int = 12, include_pending: bool = True) -> int:
        """
        Strict D'Alembert unit progression.

        - outcome == side  -> unit - 1
        - outcome != side  -> unit + 1
        - pending          -> optional conservative +1 when include_pending=True
        """
        unit = 1
        for r in sorted(self._records, key=lambda x: x["window_ts"]):
            if r["outcome"] is None:
                if include_pending:
                    unit = min(max_cap, unit + 1)
            elif r["outcome"] == r["side"]:
                unit = max(1, unit - 1)
            else:
                unit = min(max_cap, unit + 1)
        return unit

    def all_records(self) -> list[dict]:
        return list(self._records)

    def reset(self) -> None:
        """Clear all records (called on fresh session start)."""
        self._records = []
        self._save()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _trim(self) -> None:
        if len(self._records) > MAX_CYCLES:
            self._records = self._records[-MAX_CYCLES:]

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._records = json.loads(self._path.read_text())
            except Exception:
                self._records = []

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._records, indent=2))
        except Exception as exc:
            from loguru import logger
            logger.warning(f"CycleDB save failed: {exc}")
