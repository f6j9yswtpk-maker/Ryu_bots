import json


def parse_clob_token_ids(raw) -> list[str]:
    """Gamma API sometimes returns clobTokenIds as a stringified JSON list."""
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw)


def parse_outcome_prices(raw) -> list[float]:
    """Gamma API sometimes returns outcomePrices as a stringified JSON list."""
    if isinstance(raw, str):
        return [float(p) for p in json.loads(raw)]
    return [float(p) for p in raw]


def usdc_raw_to_float(raw_balance: str) -> float:
    """USDC has 6 decimals on Polygon."""
    return int(raw_balance) / 1_000_000


def shares_for_usdc(usdc_amount: float, price: float) -> float:
    """How many shares does $usdc_amount buy at limit price."""
    if price <= 0:
        raise ValueError(f"Invalid price: {price}")
    return round(usdc_amount / price, 4)
