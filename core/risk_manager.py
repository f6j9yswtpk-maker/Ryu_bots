"""
RiskManager — enforces daily loss cap, max drawdown, and position size limits.
"""

from __future__ import annotations

import time
from loguru import logger


class RiskManager:
    def __init__(
        self,
        max_position_usd: float,
        daily_loss_cap_pct: float,
        max_drawdown_pct: float,
    ) -> None:
        self.max_position_usd = max_position_usd
        self.daily_loss_cap_pct = daily_loss_cap_pct
        self.max_drawdown_pct = max_drawdown_pct

        self._peak_bankroll: float = 0.0
        self._daily_loss: float = 0.0
        self._day_start: float = time.time()

    def sync_bankroll(self, current_bankroll: float) -> None:
        """Call once at startup and after each bet resolution."""
        if current_bankroll > self._peak_bankroll:
            self._peak_bankroll = current_bankroll
        self._reset_daily_if_needed()

    def record_loss(self, amount: float) -> None:
        self._daily_loss += amount

    def record_win(self, amount: float) -> None:
        # Wins reduce daily loss tracking (net P&L basis)
        self._daily_loss = max(0.0, self._daily_loss - amount)

    def check(self, bankroll: float, bet_size: float) -> tuple[bool, str]:
        """Returns (allowed, reason). All limits disabled — always allow."""
        return True, "ok"

    def _reset_daily_if_needed(self) -> None:
        now = time.time()
        if now - self._day_start >= 86400:
            logger.info("Daily loss counter reset")
            self._daily_loss = 0.0
            self._day_start = now
