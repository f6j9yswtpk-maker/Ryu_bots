"""Abstract base class for all trading strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from core.market_fetcher import BtcMarket
from core.order_manager import OrderManager, PlacedOrder


class BaseStrategy(ABC):
    """
    Subclasses implement decide() and on_resolution().

    decide()        — called before each 5-min window; returns a PlacedOrder or None
    on_resolution() — called after the market resolves with the outcome ("Yes"/"No")
    """

    def __init__(self, order_manager: OrderManager) -> None:
        self.order_manager = order_manager

    @abstractmethod
    def decide(self, market: BtcMarket, bankroll: float) -> Optional[PlacedOrder]:
        """Return a placed order, or None to skip this round."""
        ...

    @abstractmethod
    def on_resolution(self, outcome: str, order: PlacedOrder) -> None:
        """Update internal state after a market resolves."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...
