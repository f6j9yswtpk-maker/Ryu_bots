"""
PolymarketClient — wraps ClobClient with correct auth flow.

On first run (no CLOB_API_KEY in env): derives API credentials from the
private key on-chain and writes them back to .env so subsequent starts
skip the derivation round-trip.
"""

import os
from pathlib import Path

from dotenv import load_dotenv, set_key
from loguru import logger
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


def build_client() -> ClobClient:
    """
    Return an authenticated Level-2 ClobClient.

    Reads PRIVATE_KEY from environment.  If CLOB_API_KEY / CLOB_API_SECRET /
    CLOB_API_PASSPHRASE are also present, uses them directly; otherwise calls
    create_or_derive_api_creds() and persists the result to .env.
    """
    load_dotenv()

    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("PRIVATE_KEY is not set in .env")

    api_key = os.getenv("CLOB_API_KEY", "")
    api_secret = os.getenv("CLOB_API_SECRET", "")
    api_passphrase = os.getenv("CLOB_API_PASSPHRASE", "")

    # signature_type=1 for Magic/MPC wallets (correct for this account)
    sig_type = int(os.getenv("CLOB_SIG_TYPE", "1"))

    if api_key and api_secret and api_passphrase:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        client = ClobClient(
            host=CLOB_HOST,
            key=private_key,
            chain_id=POLYGON_CHAIN_ID,
            creds=creds,
            signature_type=sig_type,
        )
        logger.debug(f"Loaded CLOB credentials from environment (sig_type={sig_type})")
    else:
        # First-run: derive credentials and persist them
        client = ClobClient(
            host=CLOB_HOST,
            key=private_key,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=sig_type,
        )
        logger.info("Deriving CLOB API credentials (first run) …")
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        env_path = Path(".env")
        if not env_path.exists():
            env_path.write_text("")
        set_key(str(env_path), "CLOB_API_KEY", creds.api_key)
        set_key(str(env_path), "CLOB_API_SECRET", creds.api_secret)
        set_key(str(env_path), "CLOB_API_PASSPHRASE", creds.api_passphrase)
        logger.info("CLOB credentials saved to .env — subsequent starts will be faster")

    # Refresh balance/allowance state on the CLOB
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    try:
        result = client.update_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        logger.info(f"CLOB allowance synced: {result}")
    except Exception as exc:
        logger.warning(f"Allowance sync skipped: {exc}")

    return client
