"""
D'Alembert strategy for 5-minute BTC binary markets.

Unit progression:
  - Start at base_unit (= base_unit_pct × bankroll, floored at min_unit_usd)
  - Loss  → unit += base_unit  (increase by one step)
  - Win   → unit -= base_unit  (decrease by one step, floor at base_unit)
  - Unit is capped at max_unit_cap × base_unit to limit streak exposure
  - State is persisted to disk so restarts don't reset progress mid-session
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from core.market_fetcher import BtcMarket
from core.order_manager import OrderManager, PlacedOrder
from strategies.base import BaseStrategy

# Which outcome to always bet ("Yes" = BTC goes up, "No" = down)
DEFAULT_SIDE = "Yes"


class DAlembert(BaseStrategy):
    def __init__(
        self,
        order_manager: OrderManager,
        base_unit_pct: float = 0.5,
        min_unit_usd: float = 5.0,
        max_unit_cap: int = 12,
        session_reset_hours: float = 24.0,
        state_file: str = "steady_d_state.json",
        side: str = DEFAULT_SIDE,
    ) -> None:
        super().__init__(order_manager)
        self.base_unit_pct = base_unit_pct
        self.min_unit_usd = min_unit_usd
        self.max_unit_cap = max_unit_cap
        self.session_reset_seconds = session_reset_hours * 3600
        self.state_file = Path(state_file)
        self.side = side  # "Yes" or "No"

        self._state = self._load_state()

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "DAlembert"

    def decide(self, market: BtcMarket, bankroll: float) -> Optional[PlacedOrder]:
        self._maybe_reset_session(bankroll)

        token_id = market.yes_token_id if self.side == "Yes" else market.no_token_id

        # Use live midpoint; fall back to market's last known price
        mid = self.order_manager.get_midpoint(token_id)
        if mid is None:
            mid = market.yes_price if self.side == "Yes" else market.no_price
            logger.warning(f"Midpoint unavailable — using market price {mid:.4f}")

        bet_usd = self._current_bet_usd(bankroll)
        logger.info(
            f"Placing {self.side} | unit={self._state['unit_multiplier']}× "
            f"| ${bet_usd:.2f} | mid={mid:.4f}"
        )

        order = self.order_manager.place_limit_order(
            token_id=token_id,
            outcome_label=self.side,
            price=mid,
            usdc_amount=bet_usd,
        )
        return order

    def on_resolution(self, outcome: str, order: PlacedOrder) -> None:
        won = outcome == self.side
        bet_usd = order.size_usd

        if won:
            payout = order.size_shares  # 1 share = $1 at resolution
            profit = payout - bet_usd
            self._state["unit_multiplier"] = max(1, self._state["unit_multiplier"] - 1)
            logger.info(f"WIN  +${profit:.2f} | unit → {self._state['unit_multiplier']}×")
        else:
            self._state["unit_multiplier"] = min(
                self.max_unit_cap, self._state["unit_multiplier"] + 1
            )
            logger.info(f"LOSS -${bet_usd:.2f} | unit → {self._state['unit_multiplier']}×")

        self._save_state()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_base_unit(self, bankroll: float) -> float:
        return max(self.min_unit_usd, bankroll * self.base_unit_pct / 100)

    def _current_bet_usd(self, bankroll: float) -> float:
        return self._current_base_unit(bankroll) * self._state["unit_multiplier"]

    def _maybe_reset_session(self, bankroll: float) -> None:
        elapsed = time.time() - self._state["session_start"]
        if elapsed >= self.session_reset_seconds:
            logger.info("24-hour session reset — resetting D'Alembert unit to 1×")
            self._state["unit_multiplier"] = 1
            self._state["session_start"] = time.time()
            self._save_state()

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception:
                pass
        return {"unit_multiplier": 1, "session_start": time.time()}

    def _save_state(self) -> None:
        self.state_file.write_text(json.dumps(self._state, indent=2))
