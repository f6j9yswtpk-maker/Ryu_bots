"""
Polymarket D'Alembert Bot — Mission Control.

Cyberpunk 90s-style TUI dashboard with multi-bot support.
Polls every minute, bets once per 5-minute window per coin.
"""

import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger
from rich.console import Console
from rich.live import Live
from rich.text import Text

import config
from config import assert_env
from core.automator import Automator
from web import server as web_server
from core.browser import current_5min_ts
from core.client import build_client
from core.cycle_db import CycleDB
from core.market_fetcher import find_next_5min_market, check_window_outcome
from core.order_manager import OrderManager, PlacedOrder
from core.price_feed import get_coin_direction, get_coin_price
from core.risk_manager import RiskManager
from strategies.custom.dalembert import DAlembert
from strategies.custom.paroli import Paroli
from utils.logger import setup_logger
from utils.notifications import notify
from reporting.stats_db import StatsDB
from reporting.reporter import start_daily_scheduler


# ---------------------------------------------------------------------------
# Multi-bot config
# ---------------------------------------------------------------------------

COINS = [
    ("btc", "D'Alembert BTC"),
    ("eth", "D'Alembert ETH"),
    ("sol", "D'Alembert SOL"),
    ("xrp", "D'Alembert XRP"),
]

PAPER_BANKROLL = 500.0   # starting paper bankroll per bot
PAPER_UNIT_USD = 5.0     # $5 per unit
PAPER_MAX_CAP  = 20      # D'Alembert cap at 20×
LIVE_LIMIT_PRICE = 0.50  # Always place LIMIT at 50c


# ---------------------------------------------------------------------------
# Bot state dataclass — one per bot, TUI renders all of them
# ---------------------------------------------------------------------------

@dataclass
class BotState:
    name: str
    coin_id: str = "btc"
    status: str = "IDLE"
    wins: int = 0
    losses: int = 0
    bankroll: float = 0.0
    last_activity: str = "--"
    cycle_count: int = 0
    btc_price: Optional[float] = None
    btc_direction: str = "?"
    unit_size_usd: float = PAPER_UNIT_USD
    last_bet_window: int = 0
    bet_pending: bool = False
    paused: bool = False        # Set via web UI to stop placing new bets
    strategy_name: str = "dalembert"
    market_slug: str = ""
    market_question: str = ""
    dalembert_unit: int = 1     # Current unit multiplier
    positions: list = field(default_factory=list)
    log_lines: deque = field(default_factory=lambda: deque(maxlen=12))


@dataclass
class BotBundle:
    """Groups all per-coin objects for one bot instance."""
    coin: str
    bot: BotState
    dalembert: DAlembert
    paroli: Paroli
    cycle_db: CycleDB
    risk_manager: RiskManager
    last_cycle_ts: float = 0.0   # for DRY_RUN staggered timing


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

_bots: list[BotState] = []
_bundles: list[BotBundle] = []
_console = Console()

# Stats database — records every resolved bet for daily reports
_stats_db: Optional[StatsDB] = None

# Shared log feed (all bots)
_feed: deque = deque(maxlen=14)

# Serialises concurrent resolution callbacks
_resolution_lock = threading.Lock()


def _tui_sink(message) -> None:
    record = message.record
    level = record["level"].name
    msg = record["message"]
    ts = record["time"].strftime("%H:%M:%S")
    _feed.append((ts, level, msg))
    if _bots:
        _bots[0].log_lines.append((ts, level, msg))


# ---------------------------------------------------------------------------
# Cyberpunk TUI renderer
# ---------------------------------------------------------------------------

_C = {
    "border":    "bright_cyan",
    "header":    "bold bright_magenta",
    "label":     "bright_cyan",
    "value":     "bold bright_green",
    "dim":       "bright_black",
    "live":      "bold bright_green",
    "idle":      "bright_black",
    "win":       "bold bright_green",
    "loss":      "bold bright_red",
    "time":      "bright_yellow",
    "INFO":      "bright_green",
    "WARNING":   "bright_yellow",
    "ERROR":     "bold bright_red",
    "DEBUG":     "bright_black",
    "SUCCESS":   "bold bright_green",
}

BOX_W = 72  # inner width


def _hline(left: str = "├", right: str = "┤", fill: str = "─") -> str:
    return f"{left}{fill * BOX_W}{right}"


def _padline(content: str, raw_len: int | None = None) -> str:
    """Wrap content in box borders, padding to BOX_W using raw_len for non-markup length."""
    if raw_len is None:
        raw_len = len(content)
    pad = BOX_W - raw_len
    if pad < 0:
        pad = 0
    return f"│ {content}{' ' * (pad - 1)}│"


def _render_dashboard() -> Text:
    now = datetime.now(timezone.utc)
    ts_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")

    elapsed_in_win = int(time.time()) % 300
    win_remaining = 300 - elapsed_in_win
    win_mins, win_secs = divmod(win_remaining, 60)

    mode = "LIVE" if not config.DRY_RUN else "DRY RUN"

    lines: list[tuple[str, str]] = []

    lines.append((f"┌{'─' * BOX_W}┐", _C["border"]))
    lines.append((_padline(f"  MISSION CONTROL v1.0  [{mode}]", 39 + len(mode)), _C["header"]))
    trigger_str = f"window {win_mins}:{win_secs:02d} left"
    lines.append((_padline(f"  [{ts_str}]  {trigger_str}", 25 + len(ts_str)), _C["dim"]))
    lines.append((_hline(), _C["border"]))

    hdr = f" {'BOT':<20}{'STATUS':^9}{'W/L':^9}{'BANKROLL':^11}{'UNIT':^7}{'LAST':>5} "
    lines.append((_padline(hdr, len(hdr)), _C["label"]))
    lines.append((_hline(), _C["border"]))

    max_slots = max(4, len(_bots))
    for i in range(max_slots):
        if i < len(_bots):
            b = _bots[i]
            indicator = "*" if b.status in ("LIVE", "DRY RUN") else "o"
            status_str = b.status[:7]
            wl = f"{b.wins}/{b.losses}"
            bank = f"${b.bankroll:,.0f}" if b.bankroll else "--"
            unit = f"{b.dalembert_unit}×"
            last = b.last_activity
            row = f" {indicator} {b.name:<18}{status_str:^9}{wl:^9}{bank:^11}{unit:^7}{last:>5} "
        else:
            row = f" o {'(empty slot)':<18}{'--':^9}{'--':^9}{'--':^11}{'--':^7}{'--':>5} "
        lines.append((_padline(row, len(row)), ""))

    lines.append((_hline(), _C["border"]))

    # Coin price lines — 2 per row
    price_bots = [b for b in _bots if b.btc_price]
    for i in range(0, len(price_bots), 2):
        parts = []
        for b in price_bots[i:i+2]:
            coin = b.coin_id.upper()
            price_s = f"${b.btc_price:,.2f}"
            dir_s = "UP" if b.btc_direction == "Yes" else "DN" if b.btc_direction == "No" else "?"
            parts.append(f"{coin}: {price_s} {dir_s}")
        price_line = "   |   ".join(parts)
        lines.append((_padline(f" {price_line}", len(price_line) + 1), _C["value"]))
    if price_bots:
        lines.append((_hline(), _C["border"]))

    feed_hdr = " >> LIVE FEED"
    lines.append((_padline(feed_hdr, len(feed_hdr)), _C["label"]))

    feed_list = list(_feed)
    if not feed_list:
        lines.append((_padline("   (awaiting first cycle...)", 28), _C["dim"]))
    else:
        for ts_s, lvl, msg in feed_list[-8:]:
            max_msg = BOX_W - 22
            if len(msg) > max_msg:
                msg = msg[: max_msg - 1] + "~"
            entry = f" {ts_s}  {lvl:<7}  {msg}"
            lines.append((_padline(entry, len(entry)), ""))

    lines.append((f"└{'─' * BOX_W}┘", _C["border"]))

    out = Text()
    for markup, style in lines:
        color = style if style else _C["dim"]
        if not style and "INFO" in markup:
            color = _C["INFO"]
        elif not style and "WARNING" in markup:
            color = _C["WARNING"]
        elif not style and "ERROR" in markup:
            color = _C["ERROR"]
        elif not style and "DEBUG" in markup:
            color = _C["DEBUG"]
        if not style and markup.startswith("│ *"):
            color = _C["live"]
        elif not style and markup.startswith("│ o"):
            color = _C["idle"]
        out.append(markup + "\n", style=color)

    return out


# ---------------------------------------------------------------------------
# D'Alembert sync from website history
# ---------------------------------------------------------------------------

def _dalembert_unit_from_outcomes(
    outcomes: list[str],
    our_side: str = "Yes",
    max_cap: int = 12,
) -> int:
    unit = 1
    for outcome in reversed(outcomes):
        if outcome == our_side:
            unit = max(1, unit - 1)
        else:
            unit += 1
    return min(unit, max_cap)


# ---------------------------------------------------------------------------
# Resolution poller — called every ~60 s from the main loop (LIVE only)
# ---------------------------------------------------------------------------

def _resolve_pending(
    bot: BotState,
    coin_id: str,
    risk_manager: RiskManager,
    automator: Optional[Automator],
    cycle_db: CycleDB,
) -> None:
    """
    Resolve any pending cycles using real Binance candle data.
    Works for both LIVE and paper trading (DRY_RUN).
    """
    for pending in cycle_db.all_pending():
        outcome = check_window_outcome(pending["window_ts"], coin_id)
        if not outcome:
            logger.debug(f"[{coin_id.upper()}] Window {pending['window_ts']} not yet resolved")
            continue
        cycle_db.close_cycle(pending["window_ts"], outcome)
        won = outcome == pending["side"]
        with _resolution_lock:
            bet = pending["bet_usd"]
            p = max(0.01, min(0.99, pending.get("price", 0.5)))
            p_c = int(round(p * 100))
            actual_profit = bet * (1.0 - p) / p
            actual_payout = bet / p
            if won:
                risk_manager.record_win(bet)
                if config.DRY_RUN:
                    bot.bankroll = max(0.0, bot.bankroll + actual_profit)
                    logger.info(
                        f"[PAPER] [{coin_id.upper()}] WIN +${actual_profit:.2f} "
                        f"@ {p_c}¢ | bankroll ${bot.bankroll:.2f} | unit={pending['unit']}×"
                    )
                else:
                    logger.info(
                        f"WIN  +${actual_profit:.2f} (paid ${actual_payout:.2f}) "
                        f"| bet ${bet:.2f} @ {p_c}¢ | unit={pending['unit']}×"
                    )
                    if automator:
                        automator.claim_requested.set()
                    notify(f"WIN +${actual_profit:.2f}")
            else:
                risk_manager.record_loss(bet)
                if config.DRY_RUN:
                    bot.bankroll = max(0.0, bot.bankroll - bet)
                    logger.info(
                        f"[PAPER] [{coin_id.upper()}] LOSS -${bet:.2f} "
                        f"@ {p_c}¢ | bankroll ${bot.bankroll:.2f} | unit={pending['unit']}×"
                    )
                else:
                    logger.info(
                        f"LOSS -${bet:.2f} @ {p_c}¢ | unit={pending['unit']}×"
                    )
                    notify(f"LOSS -${bet:.2f}")

            # Record in stats DB for daily reports
            if _stats_db:
                try:
                    _stats_db.record_bet(
                        coin=coin_id,
                        side=pending["side"],
                        bet_usd=bet,
                        outcome=outcome,
                        profit_usd=actual_profit if won else -bet,
                        unit=pending.get("unit", 1),
                        price=p,
                        slug=pending.get("slug", ""),
                    )
                except Exception as _db_exc:
                    logger.debug(f"stats_db.record_bet error: {_db_exc}")
    records = cycle_db.all_records()
    bot.wins = sum(1 for r in records if r.get("outcome") == r.get("side"))
    bot.losses = sum(
        1 for r in records
        if r.get("outcome") is not None and r.get("outcome") != r.get("side")
    )


# ---------------------------------------------------------------------------
# Cycle logic
# ---------------------------------------------------------------------------

def run_cycle(
    bot: BotState,
    coin_id: str,
    order_manager: OrderManager,
    risk_manager: RiskManager,
    dalembert: DAlembert,
    paroli: Paroli,
    automator: Optional[Automator] = None,
    cycle_db: Optional[CycleDB] = None,
) -> None:
    strat_name = web_server.strategy_for_coin(coin_id)
    bot.strategy_name = strat_name
    strategy = dalembert if strat_name != "paroli" else paroli

    # Update coin price + direction
    bot.btc_price = get_coin_price(coin_id)
    direction = get_coin_direction(coin_id)
    bot.btc_direction = direction

    current_window = current_5min_ts(0)
    if bot.paused:
        bot.status = "PAUSED"
        return
    if current_window <= bot.last_bet_window:
        secs_left = current_5min_ts(1) - int(time.time())
        bot.last_activity = f"{secs_left}s"
        bot.status = "LIVE" if not config.DRY_RUN else "DRY RUN"
        return

    # LIVE mode: safety check — only bet in the first ~4 minutes of the window
    if not config.DRY_RUN:
        remaining: Optional[int] = None
        if automator and automator.ready:
            try:
                remaining = automator.get_remaining_seconds()
            except Exception:
                pass
        if remaining is None:
            remaining = 300 - (int(time.time()) % 300)
        if remaining < 60:
            logger.info(f"[{coin_id.upper()}] Window too late ({300 - remaining}s elapsed) — skipping")
            return

    bot.cycle_count += 1
    bot.last_bet_window = current_window  # lock immediately to prevent re-entry

    # --- Find market ---
    market = find_next_5min_market(coin_id)
    if market is None:
        logger.warning(f"[{coin_id.upper()}] No market found this poll — will retry next window")
        bot.last_bet_window = 0  # allow retry
        return

    bot.market_slug = market.slug
    bot.market_question = market.question

    side = "Yes"
    strategy.side = side
    logger.info(f"[{coin_id.upper()}] Signal: {direction} | betting Up always | window={current_window}")

    # --- Bankroll ---
    if config.DRY_RUN:
        bankroll = bot.bankroll
    else:
        bankroll = order_manager.get_usdc_balance() or bot.bankroll
    risk_manager.sync_bankroll(bankroll)
    bot.bankroll = bankroll
    logger.info(f"[{coin_id.upper()}] Bankroll: ${bankroll:.2f} | Market: {market.question}")

    base_unit = bot.unit_size_usd

    # --- PAPER TRADING: resolve previous bet with real data, then record new bet ---
    if config.DRY_RUN:
        # Step P1: resolve any pending paper bets using actual Binance candles.
        # Retry up to 4× (12 s total) — the candle may not be available immediately
        # when the cycle fires right as the previous 5-min window closes.
        if cycle_db:
            for _res_attempt in range(4):
                try:
                    _resolve_pending(bot, coin_id, risk_manager, None, cycle_db)
                except Exception as exc:
                    logger.debug(f"[{coin_id.upper()}] Paper resolution error: {exc}")
                if not cycle_db.all_pending():
                    break
                if _res_attempt < 3:
                    logger.debug(f"[{coin_id.upper()}] Pending bet not yet resolved — retry {_res_attempt+1}/3 in 3s")
                    time.sleep(3)
            if cycle_db.all_pending():
                logger.warning(
                    f"[{coin_id.upper()}] Previous bet still unconfirmed — "
                    "skipping new bet for this window"
                )
                return

        # Step P2: size bet using cycle_db D'Alembert (same logic as LIVE)
        cap = web_server._max_unit_cap
        if cycle_db:
            unit = cycle_db.next_unit(max_cap=cap, include_pending=False)
            bet_usd = round(base_unit * unit, 4)
        else:
            unit = 1
            bet_usd = base_unit
        strategy._state["unit_multiplier"] = unit
        bot.dalembert_unit = unit

        # Step P3: risk check
        allowed, reason = risk_manager.check(bankroll, bet_usd)
        if not allowed:
            logger.warning(f"[{coin_id.upper()}] Risk blocked: {reason}")
            return

        # Step P4: record paper bet (no real transaction)
        bet_price = LIVE_LIMIT_PRICE
        bet_price_c = int(round(bet_price * 100))
        shares = bet_usd / bet_price if bet_price > 0 else 0
        win_profit = shares - bet_usd
        if cycle_db:
            cycle_db.open_cycle(
                current_window, market.slug, side, bet_usd, unit,
                url=f"https://polymarket.com/event/{market.slug}",
                price=bet_price,
            )
        logger.info(
            f"[PAPER] [{coin_id.upper()}] BET {side} ${bet_usd:.2f} @ {bet_price_c}¢ "
            f"| {shares:.2f} shares | potential +${win_profit:.2f} | unit={unit}×"
        )
        bot.last_activity = f"{int(time.time()) % 100:02d}s"
        return

    # --- LIVE: browser automation via Automator (BTC only for now) ---
    if automator is None or not automator.ready:
        logger.error(f"[{coin_id.upper()}] Automator not available — cannot place live bet")
        return

    # --- Step 1.5: Resolve previous window before sizing the next bet ---
    if cycle_db and cycle_db.all_pending():
        bot.status = "CHECKING RESULT"
        for attempt in range(3):
            try:
                _resolve_pending(bot, coin_id, risk_manager, automator, cycle_db)
            except Exception as exc:
                logger.debug(f"Pre-bet resolution: {exc}")
            if not cycle_db.all_pending():
                break
            if attempt < 2:
                logger.info("Previous bet still pending — retrying in 5 s...")
                time.sleep(5)
        if cycle_db.all_pending():
            logger.warning("Previous bet unresolved after 3 attempts — skipping this window")
            return

    # --- Step 2: Derive next bet size ---
    cap = web_server._max_unit_cap
    if strat_name == "paroli":
        if cycle_db:
            bet_usd = cycle_db.next_paroli_bet_usd(base_unit, max_streak=3, max_cap=cap)
        else:
            bet_usd = base_unit
        unit = max(1, round(bet_usd / base_unit))
        strategy._state["unit_multiplier"] = unit
        strategy._state["streak"] = 0
    else:
        if cycle_db:
            unit = cycle_db.next_unit(max_cap=cap, include_pending=False)
            bet_usd = round(base_unit * unit, 4)
        else:
            unit = 1
            bet_usd = base_unit
        strategy._state["unit_multiplier"] = unit
        strategy._save_state()
    bot.dalembert_unit = unit

    # --- Step 3: Risk check ---
    allowed, reason = risk_manager.check(bankroll, bet_usd)
    if not allowed:
        msg = f"Risk blocked: {reason}"
        logger.warning(msg)
        notify(f"Warning: {msg}")
        return

    # --- Pre-bet summary ---
    bet_price = LIVE_LIMIT_PRICE
    _live_token = market.yes_token_id if side == "Yes" else market.no_token_id
    bet_price_c = int(round(bet_price * 100))
    shares = bet_usd / bet_price if bet_price > 0 else 0
    win_profit = shares - bet_usd
    logger.info(
        f"PLACING {side} | ${bet_usd:.2f} @ {bet_price_c}¢ "
        f"| {shares:.2f} shares | win → +${win_profit:.2f} | unit={unit}×"
    )

    # --- Step 4: Place 50c limit order (up to 5 attempts, window-time-aware) ---
    MAX_ATTEMPTS = 5
    placed_order: Optional[PlacedOrder] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        secs_left = current_5min_ts(1) - int(time.time())
        if secs_left < 90:
            logger.warning(f"Window closing in {secs_left}s — stopping retries after attempt {attempt-1}")
            break
        order = order_manager.place_limit_order(
            token_id=_live_token,
            outcome_label=side,
            price=LIVE_LIMIT_PRICE,
            usdc_amount=bet_usd,
        )
        if order is not None:
            placed_order = order
            break
        if attempt < MAX_ATTEMPTS:
            secs_left2 = current_5min_ts(1) - int(time.time())
            if secs_left2 < 90:
                logger.warning(f"Window closing in {secs_left2}s — skipping further retries")
                break
            logger.warning(f"Bet attempt {attempt}/{MAX_ATTEMPTS} failed — retrying in 5s…")
            time.sleep(5)

    if placed_order is None:
        logger.error(f"All bet attempts failed for {side} ${bet_usd:.2f}")
        bot.last_bet_window = 0
        notify(f"FAILED {side} ${bet_usd:.2f} — no successful placement")
        return

    confirmed_slug = market.slug
    url = f"https://polymarket.com/event/{confirmed_slug}"
    fill_price = placed_order.price if placed_order.price > 0 else bet_price
    if cycle_db:
        cycle_db.open_cycle(current_window, confirmed_slug, side, bet_usd, unit, url, price=fill_price)

    fp_c = int(round(fill_price * 100))
    fp_shares = bet_usd / fill_price if fill_price > 0 else shares
    fp_profit = fp_shares - bet_usd
    bot.bet_pending = False
    logger.info(
        f"BET PLACED {side} ${bet_usd:.2f} @ {fp_c}¢ "
        f"| payout ${fp_shares:.2f} | profit ${fp_profit:.2f} | {url}"
    )
    bot.last_activity = f"{int(time.time()) % 100:02d}s"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _stats_db
    assert_env()
    setup_logger(config.LOG_FILE, config.LOG_LEVEL, console=False)
    logger.add(_tui_sink, level=config.LOG_LEVEL)

    mode = "DRY RUN (no real orders)" if config.DRY_RUN else "LIVE TRADING"
    logger.info("=" * 60)
    logger.info(f"  Polymarket D'Alembert Bot — {mode}")
    logger.info(f"  Bots: {', '.join(c.upper() for c, _ in COINS)}")
    logger.info(f"  Paper bankroll: ${PAPER_BANKROLL:.0f} | Unit: ${PAPER_UNIT_USD:.0f} | Cap: {PAPER_MAX_CAP}×")
    logger.info("=" * 60)

    if not config.DRY_RUN:
        notify("Bot started in LIVE mode")

    # --- Stats DB + daily reporter ---
    _stats_db = StatsDB()
    start_daily_scheduler(
        _stats_db,
        mode_fn=lambda: "LIVE" if not config.DRY_RUN else "PAPER",
    )
    logger.info("[Reporter] Daily reporter scheduled (00:05 UTC)")

    client = build_client()
    order_manager = OrderManager(client, dry_run=config.DRY_RUN)

    # --- Automator (browser) for LIVE mode ---
    automator: Optional[Automator] = None
    if not config.DRY_RUN:
        automator = Automator()
        automator.start()
        if not automator.wait_for_login(timeout=300):
            automator.stop()
            raise RuntimeError("Login failed — cannot trade without browser session")
        logger.info("Automator ready — browser logged in")
        automator.claim_winnings()

    # --- Create one bundle per coin ---
    state_dir = Path(config.DALEMBERT_STATE_FILE).parent
    for coin, name in COINS:
        state_file = str(state_dir / f"steady_d_state_{coin}.json")
        db_file    = str(state_dir / f"cycle_db_{coin}.json")

        bot = BotState(
            name=name,
            coin_id=coin,
            status="DRY RUN" if config.DRY_RUN else "LIVE",
            bankroll=PAPER_BANKROLL,
            unit_size_usd=PAPER_UNIT_USD,
        )
        dal = DAlembert(
            order_manager=order_manager,
            base_unit_pct=config.DALEMBERT_BASE_UNIT_PCT,
            min_unit_usd=PAPER_UNIT_USD,
            max_unit_cap=PAPER_MAX_CAP,
            session_reset_hours=config.DALEMBERT_SESSION_RESET_HOURS,
            state_file=state_file,
            side="Yes",
        )
        dal._state["unit_multiplier"] = 1
        dal._state["session_start"] = time.time()
        dal._save_state()

        par = Paroli(order_manager=order_manager, min_unit_usd=PAPER_UNIT_USD, side="Yes")
        cdb = CycleDB(db_file)
        rm  = RiskManager(
            max_position_usd=PAPER_BANKROLL,
            daily_loss_cap_pct=config.DAILY_LOSS_CAP_PCT,
            max_drawdown_pct=config.MAX_DRAWDOWN_PCT,
        )
        bundle = BotBundle(coin=coin, bot=bot, dalembert=dal, paroli=par,
                           cycle_db=cdb, risk_manager=rm)
        _bundles.append(bundle)
        _bots.append(bot)

    # LIVE: skip current mid-stream window for BTC bot
    if not config.DRY_RUN:
        _bundles[0].bot.last_bet_window = current_5min_ts(0)
        _secs = 300 - (int(time.time()) % 300)
        logger.info(f"Chain-aligned start — waiting {_secs}s for next fresh window")

    # DRY_RUN: stagger bot timers (btc=0s, eth=15s, sol=30s, xrp=45s into first minute)
    for i, bundle in enumerate(_bundles):
        bundle.last_cycle_ts = time.time() - 60.0 + i * 15.0

    # Start web UI (background thread — http://localhost:8080)
    web_server.start(
        bots=_bots,
        feed=_feed,
        automator=automator,
        cycle_dbs=[b.cycle_db for b in _bundles],
        mode="LIVE" if not config.DRY_RUN else "DRY RUN",
    )

    logger.info(
        f"4 bots started — strategy={web_server._strategy_name} "
        f"unit=${PAPER_UNIT_USD:.0f} cap={PAPER_MAX_CAP}× | web UI: http://localhost:8080"
    )

    _cycle_due    = threading.Event()   # LIVE mode only (BTC bot)
    _last_resolve = 0.0
    _RESOLVE_INTERVAL = 60

    logger.info("Watching for new 5-min windows — bots staggered 15s apart in DRY RUN")

    try:
        with Live(
            _render_dashboard(),
            console=_console,
            refresh_per_second=2,
            screen=False,
        ) as live:
            while True:
                now_ts = time.time()

                # ---- DRY RUN: fire each bot on its own 60s timer ----
                if config.DRY_RUN:
                    for bundle in _bundles:
                        if now_ts - bundle.last_cycle_ts >= 60.0:
                            bundle.last_cycle_ts = now_ts
                            try:
                                run_cycle(
                                    bundle.bot, bundle.coin, order_manager,
                                    bundle.risk_manager, bundle.dalembert, bundle.paroli,
                                    None, bundle.cycle_db,
                                )
                            except Exception as exc:
                                logger.error(f"[{bundle.coin.upper()}] Cycle error: {exc}")

                # ---- LIVE: BTC bot only, window-synced timing ----
                else:
                    btc = _bundles[0]
                    remaining: Optional[int] = None
                    if automator and automator.ready and not btc.bot.bet_pending:
                        try:
                            remaining = automator.get_remaining_seconds()
                        except Exception:
                            pass
                    if remaining is None:
                        remaining = 300 - (int(now_ts) % 300)

                    elapsed_in_window = int(now_ts) % 300
                    if (current_5min_ts(0) > btc.bot.last_bet_window
                            and elapsed_in_window >= 1
                            and elapsed_in_window <= 20
                            and not btc.bot.paused
                            and not web_server._stopped):
                        _cycle_due.set()

                    if web_server._stopped:
                        btc.bot.status = "STOPPED"
                    elif not btc.bot.bet_pending and not btc.bot.paused and remaining is not None:
                        m, s = divmod(remaining, 60)
                        btc.bot.last_activity = f"{m}:{s:02d} left"

                # Automator event handlers (LIVE)
                if automator and automator.check_outcome_request.is_set():
                    automator.check_outcome_request.clear()
                    try:
                        automator.do_outcome_check()
                    except Exception as exc:
                        logger.error(f"Outcome check error: {exc}")
                        automator.check_outcome_result = None
                        automator.check_outcome_done.set()

                if automator and automator.claim_requested.is_set():
                    automator.claim_requested.clear()
                    try:
                        automator.claim_winnings()
                    except Exception as exc:
                        logger.error(f"Claim error: {exc}")

                # Resolution poll — LIVE only
                if not config.DRY_RUN and not web_server._stopped and now_ts - _last_resolve >= _RESOLVE_INTERVAL:
                    _last_resolve = now_ts
                    for bundle in _bundles:
                        if bundle.cycle_db and bundle.cycle_db.all_pending():
                            try:
                                _resolve_pending(bundle.bot, bundle.coin, bundle.risk_manager, automator, bundle.cycle_db)
                            except Exception as exc:
                                logger.error(f"[{bundle.coin.upper()}] Resolution poll error: {exc}")

                # Fire LIVE cycle (BTC only)
                if _cycle_due.is_set() and not config.DRY_RUN:
                    _cycle_due.clear()
                    btc = _bundles[0]
                    try:
                        run_cycle(
                            btc.bot, btc.coin, order_manager,
                            btc.risk_manager, btc.dalembert, btc.paroli,
                            automator, btc.cycle_db,
                        )
                    except Exception as exc:
                        logger.error(f"Cycle error: {exc}")

                live.update(_render_dashboard())
                time.sleep(0.5)

    except (KeyboardInterrupt, SystemExit):
        if automator:
            automator.stop()
        logger.info("Bot stopped by user")
        notify("Bot stopped")


if __name__ == "__main__":
    main()
