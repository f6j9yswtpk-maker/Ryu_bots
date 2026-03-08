"""
OrderManager — places and tracks orders using the correct py-clob-client API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY

from utils.helpers import shares_for_usdc, usdc_raw_to_float


@dataclass
class PlacedOrder:
    order_id: str
    token_id: str
    side: str       # "Yes" or "No"
    price: float
    size_shares: float
    size_usd: float
    status: str     # "live" | "matched" | "unmatched"


class OrderManager:
    def __init__(self, client: ClobClient, dry_run: bool = True) -> None:
        self._client = client
        self.dry_run = dry_run

    def get_usdc_balance(self) -> float:
        """
        Return current USDC balance in dollars.

        Tries in order:
          1. BANKROLL_USD env var override (manual / browser-wallet mode)
          2. Polygon wallet balance via public RPC
          3. CLOB deposit balance
        """
        import os

        # 1. Manual override
        override = os.getenv("BANKROLL_USD", "")
        if override:
            try:
                return float(override)
            except ValueError:
                pass

        # 2. Polygon wallet USDC balance (works for browser-based betting)
        wallet_bal = self._wallet_usdc_balance()
        if wallet_bal > 0:
            return wallet_bal

        # 3. CLOB deposit balance
        try:
            raw = self._client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            balance_str = raw.get("balance", "0")
            bal = usdc_raw_to_float(balance_str)
            if bal > 0:
                return bal
        except Exception as exc:
            logger.debug(f"CLOB balance check: {exc}")

        logger.warning("All balance checks returned 0 — set BANKROLL_USD in .env to override")
        return 0.0

    def _wallet_usdc_balance(self) -> float:
        """Fetch live USDC balance from Polygon via public RPC using WALLET_ADDRESS."""
        import os
        import requests

        wallet = os.getenv("WALLET_ADDRESS", "")
        if not wallet:
            return 0.0

        # Both USDC variants on Polygon (Polymarket uses USDC.e)
        contracts = [
            "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC.e
            "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # native USDC
        ]
        padded = wallet.lower().replace("0x", "").zfill(64)
        data = "0x70a08231" + padded
        rpc = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")

        total = 0.0
        for contract in contracts:
            try:
                resp = requests.post(
                    rpc,
                    json={"jsonrpc": "2.0", "method": "eth_call",
                          "params": [{"to": contract, "data": data}, "latest"], "id": 1},
                    timeout=8,
                )
                result = resp.json().get("result", "0x0")
                total += int(result, 16) / 1_000_000
            except Exception as exc:
                logger.debug(f"RPC balance ({contract[:10]}…): {exc}")

        if total > 0:
            logger.debug(f"Wallet USDC (Polygon RPC): ${total:.4f}")
        return total

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Return the current mid-market price for a token."""
        try:
            resp = self._client.get_midpoint(token_id)
            return float(resp["mid"])
        except Exception as exc:
            logger.error(f"Failed to get midpoint for {token_id}: {exc}")
            return None

    def place_limit_order(
        self,
        token_id: str,
        outcome_label: str,
        price: float,
        usdc_amount: float,
    ) -> Optional[PlacedOrder]:
        """
        Place a GTC limit buy order.

        price       — limit price between tick_size and (1 - tick_size)
        usdc_amount — dollars to spend; converted to shares via price
        """
        size_shares = shares_for_usdc(usdc_amount, price)

        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would buy {size_shares:.4f} {outcome_label} shares "
                f"@ {price:.4f} (${usdc_amount:.2f})"
            )
            return PlacedOrder(
                order_id="dry-run",
                token_id=token_id,
                side=outcome_label,
                price=price,
                size_shares=size_shares,
                size_usd=usdc_amount,
                status="dry_run",
            )

        # Polymarket tick size is 0.01 — round price to 2 decimal places
        price = round(round(price / 0.01) * 0.01, 2)
        price = max(0.01, min(0.99, price))
        size_shares = shares_for_usdc(usdc_amount, price)

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size_shares,
                side=BUY,
            )
            signed = self._client.create_order(order_args)
            resp = self._client.post_order(signed, OrderType.GTC)

            order_id = resp.get("orderID", resp.get("id", "unknown"))
            status = resp.get("status", "unknown")

            logger.info(
                f"Order placed: id={order_id} side={outcome_label} "
                f"price={price:.2f} shares={size_shares:.4f} status={status}"
            )
            return PlacedOrder(
                order_id=order_id,
                token_id=token_id,
                side=outcome_label,
                price=price,
                size_shares=size_shares,
                size_usd=usdc_amount,
                status=status,
            )
        except Exception as exc:
            logger.error(f"Order placement failed: {repr(exc)}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run:
            logger.info(f"[DRY RUN] Would cancel order {order_id}")
            return True
        try:
            self._client.cancel(order_id)
            return True
        except Exception as exc:
            logger.error(f"Cancel failed for {order_id}: {exc}")
            return False
