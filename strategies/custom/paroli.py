"""
Paroli (anti-martingale) strategy for 5-minute BTC binary markets.

Unit progression:
  - Start at 1×
  - Win  → double the unit (1× → 2× → 4×) — ride the streak
  - Loss → reset to 1×
  - After max_streak consecutive wins → reset to 1× and lock the profit
  - Unit is capped at max_cap× to prevent runaway
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from core.market_fetcher import BtcMarket
from core.order_manager import OrderManager, PlacedOrder
from strategies.base import BaseStrategy


class Paroli(BaseStrategy):
    """Anti-martingale: double on win, reset on loss, reset after max_streak wins."""

    def __init__(
        self,
        order_manager: OrderManager,
        min_unit_usd: float = 5.0,
        max_streak: int = 3,
        max_cap: int = 8,
        side: str = "Yes",
    ) -> None:
        super().__init__(order_manager)
        self.min_unit_usd = min_unit_usd
        self.max_streak = max_streak
        self.max_cap = max_cap
        self.side = side
        self._state: dict = {"unit_multiplier": 1, "streak": 0}

    @property
    def name(self) -> str:
        return "Paroli"

    def decide(self, market: BtcMarket, bankroll: float) -> Optional[PlacedOrder]:
        token_id = market.yes_token_id if self.side == "Yes" else market.no_token_id
        mid = self.order_manager.get_midpoint(token_id)
        if mid is None:
            mid = market.yes_price if self.side == "Yes" else market.no_price
            logger.warning(f"Midpoint unavailable — using market price {mid:.4f}")

        bet_usd = self.min_unit_usd * self._state["unit_multiplier"]
        logger.info(
            f"[Paroli] {self.side} | unit={self._state['unit_multiplier']}× "
            f"| streak={self._state['streak']} | ${bet_usd:.2f} | mid={mid:.4f}"
        )
        return self.order_manager.place_limit_order(
            token_id=token_id,
            outcome_label=self.side,
            price=mid,
            usdc_amount=bet_usd,
        )

    def on_resolution(self, outcome: str, order: PlacedOrder) -> None:
        won = outcome == self.side
        if won:
            self._state["streak"] += 1
            if self._state["streak"] >= self.max_streak:
                logger.info(
                    f"[Paroli] WIN streak={self._state['streak']} — profit locked, reset 1×"
                )
                self._state["unit_multiplier"] = 1
                self._state["streak"] = 0
            else:
                self._state["unit_multiplier"] = min(
                    self.max_cap, self._state["unit_multiplier"] * 2
                )
                logger.info(
                    f"[Paroli] WIN — unit → {self._state['unit_multiplier']}× "
                    f"| streak={self._state['streak']}"
                )
        else:
            logger.info(f"[Paroli] LOSS — reset 1× | streak was {self._state['streak']}")
            self._state["unit_multiplier"] = 1
            self._state["streak"] = 0

    @staticmethod
    def next_unit_from_records(
        records: list,
        max_streak: int = 3,
        max_cap: int = 8,
    ) -> int:
        """Derive current Paroli unit from cycle_db records (LIVE mode).

        Pending bets are treated conservatively as losses (reset streak/unit).
        """
        unit = 1
        streak = 0
        for r in sorted(records, key=lambda x: x.get("window_ts", 0)):
            if r.get("outcome") is None:
                # Pending — conservative: treat as loss (reset)
                unit = 1
                streak = 0
            elif r["outcome"] == r["side"]:
                streak += 1
                if streak >= max_streak:
                    unit = 1
                    streak = 0
                else:
                    unit = min(max_cap, unit * 2)
            else:
                unit = 1
                streak = 0
        return unit
