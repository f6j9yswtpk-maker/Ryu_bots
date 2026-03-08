"""
Central configuration for the Polymarket bot.

All tuneable values live here. Sensitive values (private key, API creds,
Telegram token) are loaded from .env only — never hardcoded.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Safety — DRY_RUN must be visible in logs on every startup
# ---------------------------------------------------------------------------
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Risk limits
# ---------------------------------------------------------------------------
MAX_POSITION_USD: float = float(os.getenv("MAX_POSITION_USD", "500"))
DAILY_LOSS_CAP_PCT: float = float(os.getenv("DAILY_LOSS_CAP_PCT", "100.0"))
MAX_DRAWDOWN_PCT: float = float(os.getenv("MAX_DRAWDOWN_PCT", "100.0"))
BANKROLL_STOP: float = float(os.getenv("BANKROLL_STOP", "10.0"))    # hard floor — stop betting below this

# ---------------------------------------------------------------------------
# D'Alembert parameters
# ---------------------------------------------------------------------------
DALEMBERT_BASE_UNIT_PCT: float = 0.5   # % of bankroll per base unit (unused when fixed unit set)
DALEMBERT_MIN_UNIT_USD: float = 1.0    # absolute floor per bet — also the default unit size
DALEMBERT_MAX_UNIT_CAP: int = 20       # cap at 20× base (survives 19-loss streak)
DALEMBERT_SESSION_RESET_HOURS: float = 24.0
DALEMBERT_STATE_FILE: str = str(Path(__file__).parent / "steady_d_state.json")
DALEMBERT_SIDE: str = os.getenv("BET_SIDE", "Yes")  # "Yes" = BTC up, "No" = BTC down

# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
BET_INTERVAL_MINUTES: int = 5

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = str(Path(__file__).parent / "logs" / "bot.log")


WALLET_ADDRESS: str = os.getenv("WALLET_ADDRESS", "0x6dDa9aF8af7073a228b0F247978AD025cB4D8aFA")


def assert_env() -> None:
    """Raise early if required env vars are missing."""
    if not os.getenv("PRIVATE_KEY"):
        raise RuntimeError("PRIVATE_KEY is not set. Copy .env.example to .env and fill it in.")
