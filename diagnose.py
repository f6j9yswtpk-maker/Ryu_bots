"""Quick diagnostic: test CLOB API connection and find out why orders fail."""
import os
from dotenv import load_dotenv
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, AssetType, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY

CLOB_HOST = "https://clob.polymarket.com"
pk = os.getenv("PRIVATE_KEY")
api_key = os.getenv("CLOB_API_KEY", "")
api_secret = os.getenv("CLOB_API_SECRET", "")
api_passphrase = os.getenv("CLOB_API_PASSPHRASE", "")

print("=" * 60)
print("POLYMARKET CLOB DIAGNOSTIC")
print("=" * 60)

# 1. Build client
if api_key and api_secret and api_passphrase:
    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    client = ClobClient(host=CLOB_HOST, key=pk, chain_id=137, creds=creds, signature_type=0)
    print("✓ Client built with saved credentials")
else:
    client = ClobClient(host=CLOB_HOST, key=pk, chain_id=137, signature_type=0)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    print("✓ Client built with derived credentials")

# 2. Check API key validity
try:
    resp = client.get_api_keys()
    print(f"✓ API keys valid: {resp}")
except Exception as e:
    print(f"✗ API keys invalid: {repr(e)}")

# 3. Check balances
try:
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"  CLOB collateral balance: {bal}")
except Exception as e:
    print(f"  CLOB balance error: {repr(e)}")

try:
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))
    print(f"  CLOB conditional balance: {bal}")
except Exception as e:
    print(f"  CLOB conditional error: {repr(e)}")

# 4. Check allowances
print("\nAllowance check:")
try:
    result = client.set_allowance(asset_type="COLLATERAL")
    print(f"  set_allowance(COLLATERAL): {result}")
except Exception as e:
    print(f"  set_allowance(COLLATERAL) failed: {repr(e)}")

# 5. Try to get a live market midpoint
print("\nMarket test:")
import requests
for offset in (0, 1):
    ts = (int(__import__('time').time()) // 300) * 300 + offset * 300
    slug = f"btc-updown-5m-{ts}"
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events", params={"slug": slug}, timeout=8)
        events = r.json()
        if events:
            for event in events:
                for m in event.get("markets", []):
                    cid = m.get("conditionId", "")
                    token_ids = m.get("clobTokenIds", [])
                    if isinstance(token_ids, str):
                        import json
                        token_ids = json.loads(token_ids)
                    if len(token_ids) >= 2:
                        print(f"  Market: {m.get('question','')}")
                        print(f"  conditionId: {cid}")
                        print(f"  Yes token: {token_ids[0]}")
                        print(f"  No  token: {token_ids[1]}")

                        # Try midpoint
                        try:
                            mid = client.get_midpoint(token_ids[0])
                            print(f"  Yes midpoint: {mid}")
                        except Exception as e:
                            print(f"  Yes midpoint error: {repr(e)}")

                        # Try placing a tiny test order
                        print("\n  Test order (will fail if not allowed):")
                        try:
                            order_args = OrderArgs(
                                token_id=token_ids[0],
                                price=0.50,
                                size=10.0,
                                side=BUY,
                            )
                            signed = client.create_order(order_args)
                            print(f"  ✓ Order signed locally: {type(signed).__name__}")
                            resp = client.post_order(signed, OrderType.GTC)
                            print(f"  ✓ Order posted! {resp}")
                        except Exception as e:
                            print(f"  ✗ Order failed: {repr(e)}")
                            if hasattr(e, 'status_code'):
                                print(f"    status_code: {e.status_code}")
                            if hasattr(e, 'error_msg'):
                                print(f"    error_msg: {e.error_msg}")
                        break
            break
    except Exception as e:
        print(f"  slug {slug}: {e}")

# 6. Try signature_type=2 (Poly Proxy)
print("\n\nTrying signature_type=2 (Poly Proxy):")
try:
    client2 = ClobClient(host=CLOB_HOST, key=pk, chain_id=137, signature_type=2)
    creds2 = client2.create_or_derive_api_creds()
    client2.set_api_creds(creds2)
    print(f"  ✓ Poly Proxy client created")
    keys2 = client2.get_api_keys()
    print(f"  API keys: {keys2}")
except Exception as e:
    print(f"  ✗ Poly Proxy failed: {repr(e)}")

print("\n" + "=" * 60)
print("DONE")
