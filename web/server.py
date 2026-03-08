"""
Tool Ryū — Web UI server.

FastAPI app serving the single-page cyberpunk dashboard.
Streams real-time bot state over WebSocket at /ws.
REST endpoints let the UI control bots (pause/resume/claim/mode).
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from loguru import logger

# ---------------------------------------------------------------------------
# Persistent settings
# ---------------------------------------------------------------------------

_SETTINGS_FILE = Path(__file__).parent.parent / "bot_settings.json"

def _load_settings() -> dict:
    try:
        if _SETTINGS_FILE.exists():
            return json.loads(_SETTINGS_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_settings() -> None:
    try:
        _SETTINGS_FILE.write_text(json.dumps({
            "strategy": _strategy_name,
            "strategy_by_coin": _strategy_by_coin,
            "max_unit_cap": _max_unit_cap,
            "unit_size_usd": _unit_size_usd,
        }, indent=2))
    except Exception as exc:
        logger.warning(f"Could not save settings: {exc}")

_s = _load_settings()

# ---------------------------------------------------------------------------
# Shared state — written by main.py, read by handlers
# ---------------------------------------------------------------------------

_bots: list = []
_cycle_dbs: list = []          # one CycleDB per bot, aligned with _bots
_feed: Optional[deque] = None
_automator = None
_mode: str = "DRY RUN"
_stopped: bool = False
_strategy_name: str = _s.get("strategy", "dalembert")
_strategy_by_coin: dict[str, str] = {
    str(k).lower(): str(v).lower()
    for k, v in (_s.get("strategy_by_coin", {}) or {}).items()
    if str(v).lower() in ("dalembert", "paroli")
}
_max_unit_cap: int = _s.get("max_unit_cap", 20)
_unit_size_usd: float = _s.get("unit_size_usd", 5.0)

_clients: set[WebSocket] = set()
_clients_lock = asyncio.Lock()


def strategy_for_coin(coin_id: str) -> str:
    """Return effective strategy for a given coin."""
    coin = (coin_id or "").lower()
    return _strategy_by_coin.get(coin, _strategy_name)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Tool Ryū Mission Control")
_STATIC = Path(__file__).parent / "static"


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse((_STATIC / "index.html").read_text())


# ---------------------------------------------------------------------------
# WebSocket — pushes state every second
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    async with _clients_lock:
        _clients.add(ws)
    try:
        while True:
            await ws.send_json(_build_payload())
            await asyncio.sleep(1)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        async with _clients_lock:
            _clients.discard(ws)


def _build_payload() -> dict:
    bots_data = []
    for i, bot in enumerate(_bots):
        cdb = _cycle_dbs[i] if i < len(_cycle_dbs) else None
        if cdb:
            records = cdb.all_records()
            db_wins   = sum(1 for r in records if r.get("outcome") == r.get("side"))
            db_losses = sum(1 for r in records if r.get("outcome") is not None and r.get("outcome") != r.get("side"))
            cycles = list(reversed(records))[:15]
        else:
            db_wins   = bot.wins
            db_losses = bot.losses
            cycles    = []

        log_lines = list(bot.log_lines)[-15:]
        bots_data.append({
            "name":           bot.name,
            "coin_id":        getattr(bot, "coin_id", "btc"),
            "status":         bot.status,
            "wins":           db_wins,
            "losses":         db_losses,
            "bankroll":       round(bot.bankroll, 2),
            "bet_pending":    bot.bet_pending,
            "coin_price":     bot.btc_price,
            "coin_direction": bot.btc_direction,
            "cycle_count":    bot.cycle_count,
            "last_activity":  bot.last_activity,
            "paused":         getattr(bot, "paused", False),
            "market_slug":    getattr(bot, "market_slug", ""),
            "market_question":getattr(bot, "market_question", ""),
            "dalembert_unit": getattr(bot, "dalembert_unit", 1),
            "unit_size_usd":  getattr(bot, "unit_size_usd", 5.0),
            "log_lines":      [[ts, lvl, msg] for ts, lvl, msg in log_lines],
            "cycles":         cycles,
            "strategy":       strategy_for_coin(getattr(bot, "coin_id", "btc")),
        })

    return {
        "bots":        bots_data,
        "feed":        [[ts, lvl, msg] for ts, lvl, msg in list(_feed or [])[-30:]],
        "mode":        _mode,
        "ts":          time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "stopped":     _stopped,
        "strategy":    _strategy_name,
        "strategy_by_coin": dict(_strategy_by_coin),
        "max_unit_cap":_max_unit_cap,
        "unit_size_usd": _unit_size_usd,
    }


# ---------------------------------------------------------------------------
# Control endpoints
# ---------------------------------------------------------------------------

@app.post("/api/bot/{bot_name}/reset")
async def reset_bot(bot_name: str) -> dict:
    for i, bot in enumerate(_bots):
        if bot.name == bot_name:
            if i < len(_cycle_dbs):
                _cycle_dbs[i].reset()
            bot.wins = 0
            bot.losses = 0
            bot.last_bet_window = 0
            bot.dalembert_unit = 1
            logger.info(f"[WEB] Bot '{bot_name}' cycle DB reset")
            return {"ok": True}
    return {"ok": False, "error": "bot not found"}


@app.post("/api/bot/{bot_name}/pause")
async def pause_bot(bot_name: str) -> dict:
    for bot in _bots:
        if bot.name == bot_name:
            bot.paused = True
            logger.info(f"[WEB] Bot '{bot_name}' paused")
            return {"ok": True}
    return {"ok": False, "error": "bot not found"}


@app.post("/api/bot/{bot_name}/resume")
async def resume_bot(bot_name: str) -> dict:
    for bot in _bots:
        if bot.name == bot_name:
            bot.paused = False
            logger.info(f"[WEB] Bot '{bot_name}' resumed")
            return {"ok": True}
    return {"ok": False, "error": "bot not found"}


@app.post("/api/claim")
async def trigger_claim() -> dict:
    if _automator and _automator.ready:
        _automator.claim_requested.set()
        logger.info("[WEB] Manual claim requested")
        return {"ok": True}
    return {"ok": False, "error": "automator not ready"}


@app.post("/api/settings")
async def update_settings(req: Request) -> dict:
    global _max_unit_cap, _unit_size_usd
    body = await req.json()
    result: dict = {"ok": True}

    if "unit_size_usd" in body:
        try:
            unit = float(body["unit_size_usd"])
            if unit < 1 or unit > 500:
                return {"ok": False, "error": "unit_size_usd must be 1–500"}
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid unit_size_usd"}
        _unit_size_usd = unit
        coins = body.get("coins")   # optional list of coin_ids to target
        for bot in _bots:
            if coins is None or getattr(bot, "coin_id", "btc") in coins:
                bot.unit_size_usd = unit
        logger.info(f"[WEB] Unit size → ${unit:.2f} (coins={coins or 'all'})")
        result["unit_size_usd"] = unit

    if "max_unit_cap" in body:
        try:
            cap = int(body["max_unit_cap"])
            if cap < 1 or cap > 20:
                return {"ok": False, "error": "max_unit_cap must be 1–20"}
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid max_unit_cap"}
        _max_unit_cap = cap
        logger.info(f"[WEB] Max unit cap → {cap}×")
        result["max_unit_cap"] = cap

    if len(result) == 1:
        return {"ok": False, "error": "no recognized field"}
    _save_settings()
    return result


@app.post("/api/mode")
async def set_mode(req: Request) -> dict:
    global _mode
    import config as _cfg
    body = await req.json()
    paper = body.get("paper", True)
    _cfg.DRY_RUN = paper
    _mode = "DRY RUN" if paper else "LIVE"
    logger.info(f"[WEB] Mode switched → {_mode}")
    return {"ok": True, "mode": _mode}


@app.post("/api/stop")
async def stop_bot() -> dict:
    global _stopped
    _stopped = True
    logger.info("[WEB] Bot STOPPED via web UI")
    return {"ok": True}


@app.post("/api/start")
async def start_bot() -> dict:
    global _stopped
    _stopped = False
    logger.info("[WEB] Bot STARTED via web UI")
    return {"ok": True}


@app.post("/api/strategy")
async def set_strategy(req: Request) -> dict:
    global _strategy_name
    body = await req.json()
    name = body.get("name", "").strip().lower()
    if name not in ("dalembert", "paroli"):
        return {"ok": False, "error": "unknown strategy"}

    coins = body.get("coins")
    if isinstance(coins, list) and coins:
        applied: list[str] = []
        for c in coins:
            coin = str(c).strip().lower()
            if not coin:
                continue
            _strategy_by_coin[coin] = name
            applied.append(coin)
        if not applied:
            return {"ok": False, "error": "no valid coins provided"}
        logger.info(f"[WEB] Strategy ({name}) applied to coins: {', '.join(applied)}")
    else:
        _strategy_name = name
        # Preserve previous one-click behavior: set all known bots to this strategy.
        for b in _bots:
            coin = getattr(b, "coin_id", "")
            if coin:
                _strategy_by_coin[str(coin).lower()] = name
        logger.info(f"[WEB] Default strategy → {name} (applied to all known coins)")

    _save_settings()
    return {
        "ok": True,
        "strategy": _strategy_name,
        "strategy_by_coin": dict(_strategy_by_coin),
    }


@app.post("/api/restart")
async def restart_bot() -> dict:
    import os, sys
    logger.info("[WEB] Bot RESTARTING")
    def _do() -> None:
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}


@app.get("/api/state")
async def get_state() -> dict:
    return _build_payload()


@app.get("/api/screenshot")
async def get_screenshot():
    import os
    shots = sorted(
        (Path(__file__).parent.parent / "screenshots").glob("*.png"),
        key=os.path.getmtime, reverse=True,
    )
    if not shots:
        raise HTTPException(status_code=404, detail="No screenshots")
    return FileResponse(str(shots[0]), media_type="image/png")


@app.get("/api/chain")
async def get_chain() -> list:
    try:
        from core.chain_verifier import get_recent_bets
        import config
        return get_recent_bets(config.WALLET_ADDRESS, limit=10)
    except Exception as exc:
        logger.warning(f"chain endpoint: {exc}")
        return []


# ---------------------------------------------------------------------------
# Startup helper — called from main.py
# ---------------------------------------------------------------------------

def start(
    bots: list,
    feed: deque,
    automator=None,
    cycle_dbs: list | None = None,
    cycle_db=None,             # backward compat (single CycleDB)
    mode: str = "DRY RUN",
    host: str = "0.0.0.0",
    port: int = 8080,
) -> threading.Thread:
    global _bots, _feed, _automator, _cycle_dbs, _mode
    _bots = bots
    _feed = feed
    _automator = automator
    _mode = mode

    if cycle_dbs:
        _cycle_dbs = cycle_dbs
    elif cycle_db:
        _cycle_dbs = [cycle_db]
    else:
        _cycle_dbs = []

    import uvicorn

    def _run() -> None:
        uvicorn.run(app, host=host, port=port, log_level="warning")

    t = threading.Thread(target=_run, name="web-server", daemon=True)
    t.start()

    import socket
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)

    logger.info(f"Web UI ready → http://localhost:{port}")
    return t
