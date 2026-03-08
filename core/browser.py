"""
Brave browser session for Polymarket.

Uses the existing Brave profile where the user is logged into Polymarket
via email (Magic wallet). No MetaMask needed — Polymarket's embedded
wallet signs transactions automatically.

Single page reuse: one persistent tab for all navigation, no tab spam.
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

BRAVE_BIN = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
BRAVE_PROFILE = str(Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser")
POLYMARKET = "https://polymarket.com"


# ---------------------------------------------------------------------------
# Human-behaviour helpers
# ---------------------------------------------------------------------------

def _jitter(lo: float = 0.3, hi: float = 1.2) -> None:
    time.sleep(random.uniform(lo, hi))


def _human_move_click(page, selector: str, timeout: int = 6_000) -> None:
    loc = page.locator(selector).first
    loc.wait_for(state="visible", timeout=timeout)
    box = loc.bounding_box()
    if box:
        x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
        y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
        mid_x = x + random.uniform(-40, 40)
        mid_y = y + random.uniform(-30, 30)
        page.mouse.move(mid_x, mid_y)
        _jitter(0.05, 0.2)
        page.mouse.move(x, y)
        _jitter(0.05, 0.15)
        page.mouse.click(x, y)
    else:
        loc.click()


def _human_type(page, selector: str, text: str) -> None:
    _human_move_click(page, selector)
    _jitter(0.1, 0.3)
    page.keyboard.press("Control+a")
    for ch in text:
        page.keyboard.type(ch)
        time.sleep(random.uniform(0.04, 0.18))


def _random_scroll(page) -> None:
    page.mouse.wheel(0, random.randint(100, 400))
    _jitter(0.2, 0.5)


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def current_5min_ts(offset: int = 0) -> int:
    now = int(time.time())
    return (now // 300) * 300 + offset * 300


def btc_event_url(offset: int = 0) -> str:
    return f"{POLYMARKET}/event/btc-updown-5m-{current_5min_ts(offset)}"


# ---------------------------------------------------------------------------
# Browser class
# ---------------------------------------------------------------------------

class PolymarketBrowser:
    """
    Long-lived Brave browser session with single page reuse.
    Logged-in Polymarket session via Magic wallet — no MetaMask needed.
    Thread-safe via internal lock.
    """

    def __init__(self, headless: bool = False) -> None:
        self._headless = headless
        self._pw = None
        self._ctx = None
        self._page = None  # single reused page
        self._lock = threading.Lock()
        self.ready = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()

        if self._headless:
            browser = self._pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            self._ctx = browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
        else:
            # Brave with existing profile — already logged into Polymarket
            self._ctx = self._pw.chromium.launch_persistent_context(
                BRAVE_PROFILE,
                executable_path=BRAVE_BIN,
                headless=False,
                slow_mo=random.randint(40, 80),
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                ignore_default_args=[
                    "--enable-automation",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-default-apps",
                ],
                viewport={
                    "width": random.randint(1280, 1440),
                    "height": random.randint(780, 900),
                },
            )

        # Single reused page for all operations
        self._page = self._ctx.new_page()
        self.ready = True
        logger.info(f"Browser ready | headless={self._headless}")

    def stop(self) -> None:
        self.ready = False
        try:
            if self._page and not self._page.is_closed():
                self._page.close()
        except Exception:
            pass
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = None
        logger.info("Browser stopped")

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    def find_btc_market(self) -> Optional[dict]:
        if not self.ready:
            return None

        for offset in (0, 1, -1, 2):
            url = btc_event_url(offset)
            logger.debug(f"Browser: trying {url}")
            market = self._load_event_page(url)
            if market:
                logger.info(f"Browser found market (offset={offset}): {market.get('question', '')}")
                return market

        logger.warning("Browser: no BTC 5-min market found in adjacent windows")
        return None

    def _load_event_page(self, url: str) -> Optional[dict]:
        captured: list[dict] = []

        with self._lock:
            def on_response(resp) -> None:
                if "polymarket.com" not in resp.url:
                    return
                try:
                    data = resp.json()
                    if isinstance(data, list):
                        captured.extend(data)
                    elif isinstance(data, dict):
                        captured.append(data)
                except Exception:
                    pass

            self._page.on("response", on_response)
            try:
                self._page.goto(url, wait_until="networkidle", timeout=25_000)
                self._page.wait_for_timeout(2_000)
            except Exception:
                pass
            finally:
                self._page.remove_listener("response", on_response)

        for item in captured:
            if item.get("clobTokenIds") and item.get("conditionId"):
                return item
            for m in item.get("markets", []):
                if m.get("clobTokenIds") and m.get("conditionId"):
                    return m

        return self._fetch_event_api(url.split("/event/")[-1])

    def _fetch_event_api(self, slug: str) -> Optional[dict]:
        import requests
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/events",
                params={"slug": slug},
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            for event in r.json() or []:
                for m in event.get("markets", []):
                    if m.get("clobTokenIds") and m.get("conditionId"):
                        return m
        except Exception as exc:
            logger.debug(f"Events API fallback {slug}: {exc}")
        return None

    # ------------------------------------------------------------------
    # BTC price
    # ------------------------------------------------------------------

    def get_btc_price_from_page(self) -> Optional[float]:
        if not self.ready:
            return None

        captured_price: list[float] = []

        with self._lock:
            def on_response(resp) -> None:
                if "polymarket.com" not in resp.url:
                    return
                try:
                    data = resp.json()
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        q = item.get("question", "")
                        m = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", q)
                        if m:
                            price = float(m.group(1).replace(",", ""))
                            captured_price.append(price)
                except Exception:
                    pass

            self._page.on("response", on_response)
            try:
                self._page.goto(btc_event_url(0), wait_until="networkidle", timeout=20_000)
            except Exception:
                pass
            finally:
                self._page.remove_listener("response", on_response)

        return captured_price[0] if captured_price else None

    # ------------------------------------------------------------------
    # Open positions
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict]:
        if not self.ready:
            return []

        positions: list[dict] = []
        with self._lock:
            def on_response(resp) -> None:
                if "positions" in resp.url and "polymarket" in resp.url:
                    try:
                        data = resp.json()
                        if isinstance(data, list):
                            positions.extend(data)
                        elif isinstance(data, dict):
                            positions.extend(data.get("data", []))
                    except Exception:
                        pass

            self._page.on("response", on_response)
            try:
                self._page.goto(f"{POLYMARKET}/portfolio", wait_until="networkidle", timeout=20_000)
                _random_scroll(self._page)
            except Exception:
                pass
            finally:
                self._page.remove_listener("response", on_response)

        logger.debug(f"Browser: {len(positions)} open positions")
        return positions

    # ------------------------------------------------------------------
    # Bet placement — via Polymarket UI (embedded wallet signs auto)
    # ------------------------------------------------------------------

    def place_bet(self, event_slug: str, side: str, amount_usd: float) -> bool:
        """
        Place a bet through the Polymarket UI.
        Polymarket's embedded Magic wallet handles signing — no MetaMask needed.
        """
        if not self.ready:
            logger.error("Browser not ready")
            return False

        SCREENSHOTS = Path.home() / "polymarket-bot" / "screenshots"
        SCREENSHOTS.mkdir(exist_ok=True)

        with self._lock:
            try:
                url = f"{POLYMARKET}/event/{event_slug}"
                logger.info(f"Browser bet: {side} ${amount_usd:.2f} -> {url}")

                self._page.goto(url, wait_until="networkidle", timeout=30_000)
                _jitter(2.0, 3.5)

                self._page.screenshot(path=str(SCREENSHOTS / "01_page_loaded.png"), full_page=False)
                _random_scroll(self._page)
                _jitter(0.5, 1.0)

                # --- Click outcome ---
                labels = (
                    ["Yes", "Up", "Higher", "Above"]
                    if side == "Yes"
                    else ["No", "Down", "Lower", "Below"]
                )
                clicked = False
                for label in labels:
                    try:
                        _human_move_click(self._page, f"button:has-text('{label}')", timeout=3_000)
                        clicked = True
                        logger.info(f"Clicked outcome: {label}")
                        break
                    except Exception:
                        continue

                if not clicked:
                    self._page.screenshot(path=str(SCREENSHOTS / "02_no_outcome_btn.png"), full_page=False)
                    buttons = self._page.locator("button").all_text_contents()
                    logger.error(f"Could not find outcome button | buttons: {buttons[:20]}")
                    return False

                _jitter(0.6, 1.2)
                self._page.screenshot(path=str(SCREENSHOTS / "03_after_outcome.png"))

                # --- Type amount ---
                amount_fields = [
                    "input[placeholder*='$']",
                    "input[placeholder*='Amount']",
                    "input[placeholder*='amount']",
                    "input[type='number']",
                    "input[inputmode='decimal']",
                    "input[inputmode='numeric']",
                ]
                typed = False
                for sel in amount_fields:
                    try:
                        _human_type(self._page, sel, str(int(amount_usd)))
                        typed = True
                        logger.info(f"Typed amount via: {sel}")
                        break
                    except Exception:
                        continue

                if not typed:
                    self._page.screenshot(path=str(SCREENSHOTS / "04_no_amount_input.png"), full_page=False)
                    logger.error("Could not find amount input")
                    return False

                _jitter(0.8, 1.5)
                self._page.screenshot(path=str(SCREENSHOTS / "05_after_amount.png"))

                # --- Click Buy ---
                buy_labels = ["Buy", "Place Order", "Submit", "Trade", "Confirm"]
                bought = False
                for label in buy_labels:
                    try:
                        _human_move_click(self._page, f"button:has-text('{label}')", timeout=3_000)
                        bought = True
                        logger.info(f"Clicked buy: {label}")
                        break
                    except Exception:
                        continue

                if not bought:
                    self._page.screenshot(path=str(SCREENSHOTS / "06_no_buy_btn.png"), full_page=False)
                    buttons = self._page.locator("button").all_text_contents()
                    logger.error(f"Could not find Buy button | buttons: {buttons[:20]}")
                    return False

                # Polymarket's embedded wallet signs automatically — just wait
                logger.info("Buy clicked — waiting for confirmation...")
                _jitter(3.0, 5.0)
                self._page.screenshot(path=str(SCREENSHOTS / "07_after_buy.png"))

                # Check for success indicators
                try:
                    success = self._page.locator("text=/success|confirmed|placed|order/i").first
                    success.wait_for(state="visible", timeout=10_000)
                    logger.info(f"Bet confirmed: {side} ${amount_usd:.2f}")
                except Exception:
                    # No explicit success text — check we didn't get an error
                    error_el = self._page.locator("text=/error|failed|insufficient/i")
                    if error_el.count() > 0:
                        err_text = error_el.first.text_content()
                        logger.error(f"Bet error on page: {err_text}")
                        self._page.screenshot(path=str(SCREENSHOTS / "08_error.png"))
                        return False
                    logger.info(f"Bet submitted (no explicit confirmation): {side} ${amount_usd:.2f}")

                return True

            except Exception as exc:
                logger.error(f"Browser bet error: {exc}")
                try:
                    self._page.screenshot(path=str(SCREENSHOTS / "error.png"))
                except Exception:
                    pass
                return False
