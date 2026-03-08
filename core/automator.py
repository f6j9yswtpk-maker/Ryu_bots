"""
Polymarket PWA Automator — the main hub for all bot trading.

Launches the installed Polymarket PWA (Chrome for Testing in --app mode)
with the persistent profile at ~/.polybot-profile/. This is the same profile
the PWA uses, so login sessions, cookies, and the embedded wallet persist.

The bot connects to the PWA via Chrome DevTools Protocol (CDP) on a debug
port, then controls it with Playwright. Video recording captures every
session to recordings/.

Architecture:
  Chrome for Testing  ──► --app=https://polymarket.com/
                       ──► --user-data-dir=~/.polybot-profile
                       ──► --remote-debugging-port=9222
  Playwright          ──► connects via CDP ws://127.0.0.1:9222
  Bot                 ──► navigates, clicks outcomes, enters amounts, trades
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

POLYMARKET = "https://polymarket.com"
PROFILE_DIR = str(Path.home() / ".polybot-profile")
RECORDINGS_DIR = str(Path(__file__).resolve().parent.parent / "recordings")
SCREENSHOTS_DIR = str(Path(__file__).resolve().parent.parent / "screenshots")
CDP_PORT = 9222

# Playwright's bundled Chrome for Testing (same binary the PWA was installed from)
CHROME_BIN = str(
    Path.home()
    / "Library/Caches/ms-playwright/chromium-1208/chrome-mac-x64"
    / "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
)

# PWA app ID (from the installed Polymarket.app bundle)
PWA_APP_ID = "mmalionclfilbbblncockcpagjklblpk"

# Amount buttons available on Polymarket UI
AMOUNT_BUTTONS = [100, 10, 5, 1]


def _decompose_amount(target: int) -> list[int]:
    """Break target USD into clicks of +$100, +$10, +$5, +$1."""
    remaining = target
    clicks = []
    for btn in AMOUNT_BUTTONS:
        while remaining >= btn:
            clicks.append(btn)
            remaining -= btn
    return clicks


class Automator:
    """
    Polymarket PWA hub — launches the PWA and controls it via Playwright/CDP.

    All bots route their trades through this single browser instance.
    The PWA window stays open between trades, maintaining wallet connection.
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._chrome_proc = None
        self._lock = threading.Lock()
        self.ready = False
        # Set by web server; consumed by main loop on the Playwright thread
        self.claim_requested = threading.Event()
        # Slug confirmed from the browser URL after each bet — ground truth
        self.last_event_slug: str = ""
        # Actual fill price parsed from order history (0.0 = unknown)
        self.last_fill_price: float = 0.0
        # Outcome-check handshake (background thread → main thread → back)
        self._oc_slug: str = ""
        self._oc_cid: str = ""
        self.check_outcome_request: threading.Event = threading.Event()
        self.check_outcome_done: threading.Event = threading.Event()
        self.check_outcome_result: Optional[str] = None

    def start(self) -> None:
        """Launch the Polymarket PWA and connect Playwright via CDP."""
        from playwright.sync_api import sync_playwright

        Path(RECORDINGS_DIR).mkdir(parents=True, exist_ok=True)
        Path(SCREENSHOTS_DIR).mkdir(parents=True, exist_ok=True)

        # Kill any leftover Chrome holding our debug port
        self._kill_stale_chrome()

        # Launch Chrome for Testing with the PWA profile + debug port.
        # --app-id opens the installed PWA (same as clicking Polymarket.app).
        # --remote-debugging-port lets Playwright connect via CDP.
        chrome_args = [
            CHROME_BIN,
            f"--profile-directory=Default",
            f"--user-data-dir={PROFILE_DIR}",
            f"--app-id={PWA_APP_ID}",
            f"--remote-debugging-port={CDP_PORT}",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size=1366,768",
            # Force dark mode — Polymarket respects prefers-color-scheme
            "--force-dark-mode",
            "--enable-features=WebContentsForceDark",
        ]

        logger.info(f"Launching Polymarket PWA (app-id={PWA_APP_ID})")
        logger.info(f"Profile: {PROFILE_DIR} | CDP port: {CDP_PORT}")

        self._chrome_proc = subprocess.Popen(
            chrome_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for CDP to be ready
        self._wait_for_cdp(timeout=20)

        # Connect Playwright to the running PWA
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}",
        )

        # Get the PWA's context and page
        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError("No browser contexts found after connecting to PWA")

        self._ctx = contexts[0]

        # Find or create the main page
        if self._ctx.pages:
            self._page = self._ctx.pages[0]
        else:
            self._page = self._ctx.new_page()

        # Force dark colour-scheme so Polymarket renders in dark mode
        try:
            self._page.emulate_media(color_scheme="dark")
        except Exception:
            pass

        self.ready = True
        logger.info(f"Connected to Polymarket PWA | page={self._page.url}")
        logger.info(f"Recordings: {RECORDINGS_DIR} | Screenshots: {SCREENSHOTS_DIR}")


    def _kill_stale_chrome(self) -> None:
        """Kill any Chrome for Testing or PWA processes using our profile.

        Chrome locks the profile dir — only one instance can use it at a time.
        If the Polymarket PWA (app_mode_loader) or a previous bot run left
        Chrome running, we must kill it before launching with --remote-debugging-port.
        """
        killed = False

        # 1. Kill anything on our CDP port
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{CDP_PORT}"],
                capture_output=True, text=True, timeout=5,
            )
            for pid in result.stdout.strip().split():
                if pid:
                    logger.warning(f"Killing stale process on port {CDP_PORT}: PID {pid}")
                    os.kill(int(pid), signal.SIGTERM)
                    killed = True
        except Exception:
            pass

        # 2. Kill any Chrome for Testing using our profile dir
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"user-data-dir={PROFILE_DIR}"],
                capture_output=True, text=True, timeout=5,
            )
            for pid in result.stdout.strip().split():
                if pid:
                    logger.warning(f"Killing Chrome with our profile: PID {pid}")
                    os.kill(int(pid), signal.SIGTERM)
                    killed = True
        except Exception:
            pass

        # 3. Kill the PWA app_mode_loader (it spawns Chrome for Testing)
        try:
            result = subprocess.run(
                ["pgrep", "-f", "Polymarket.app.*app_mode_loader"],
                capture_output=True, text=True, timeout=5,
            )
            for pid in result.stdout.strip().split():
                if pid:
                    logger.warning(f"Killing Polymarket PWA loader: PID {pid}")
                    os.kill(int(pid), signal.SIGTERM)
                    killed = True
        except Exception:
            pass

        if killed:
            time.sleep(2)  # Wait for profile lock to release

    def _wait_for_cdp(self, timeout: int = 20) -> None:
        """Poll until Chrome's CDP endpoint is reachable."""
        import urllib.request
        import urllib.error

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                req = urllib.request.urlopen(
                    f"http://127.0.0.1:{CDP_PORT}/json/version",
                    timeout=2,
                )
                req.close()
                logger.debug("CDP endpoint ready")
                return
            except (urllib.error.URLError, ConnectionRefusedError, OSError):
                time.sleep(0.5)

        raise RuntimeError(f"Chrome CDP not ready after {timeout}s — is port {CDP_PORT} blocked?")

    def is_logged_in(self) -> bool:
        """Check if user is logged in (no 'Log In' button visible)."""
        try:
            count = self._page.locator("button:has-text('Log In')").count()
            return count == 0
        except Exception:
            return False

    def wait_for_login(self, timeout: int = 300) -> bool:
        """
        Check if already logged in. If not, pause for manual login in the PWA.
        Session persists in the profile dir for all future runs.
        """
        with self._lock:
            # Ensure we're on Polymarket
            if POLYMARKET not in self._page.url:
                self._page.goto(POLYMARKET, wait_until="domcontentloaded", timeout=30_000)
            self._page.wait_for_timeout(3000)

            if self.is_logged_in():
                logger.info("Already logged in — session restored from PWA profile")
                return True

            logger.warning("=" * 50)
            logger.warning("NOT LOGGED IN — Please log in in the Polymarket PWA window")
            logger.warning(f"You have {timeout}s to complete login")
            logger.warning("=" * 50)

            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(5)
                try:
                    # Don't reload — let user interact naturally
                    if self.is_logged_in():
                        logger.info("Login detected — session saved to PWA profile")
                        return True
                except Exception:
                    pass
                remaining = int(deadline - time.time())
                if remaining % 30 == 0:
                    logger.info(f"Waiting for login... {remaining}s remaining")

            logger.error("Login timeout — bot cannot trade without login")
            return False

    def place_bet(self, event_slug: str, side: str, amount_usd: float) -> bool:
        """
        Place a bet via the Polymarket PWA UI.

        1. Navigate to event page
        2. Click Up/Down outcome button
        3. Click amount quick-select buttons (+$1, +$5, +$10, +$100)
        4. Click Trade
        5. Wait for embedded wallet to sign
        """
        if not self.ready:
            logger.error("Automator not ready")
            return False

        with self._lock:
            try:
                url = f"{POLYMARKET}/event/{event_slug}"
                logger.info(f"Automator: {side} ${amount_usd:.2f} -> {url}")

                # 1. Navigate only if not already on this event page.
                import re as _re
                fresh_nav = False
                if event_slug not in self._page.url:
                    # "commit" fires as soon as the server responds — much faster
                    # than "domcontentloaded" which waits for the full SPA parse.
                    self._page.goto(url, wait_until="commit", timeout=20_000)
                    fresh_nav = True
                else:
                    logger.debug("Already on event page — skipping navigation")

                # Extract confirmed slug from live URL
                _m = _re.search(r"/event/([^/?#]+)", self._page.url)
                if _m:
                    self.last_event_slug = _m.group(1)
                    if self.last_event_slug != event_slug:
                        logger.info(f"Slug updated: {event_slug} → {self.last_event_slug}")
                else:
                    self.last_event_slug = event_slug

                # Wait for the actual outcome button we need (smart wait, no fixed sleep)
                _outcome_labels = ["Up", "Yes"] if side == "Yes" else ["Down", "No"]
                _sel = ", ".join(f"button:has-text('{l}')" for l in _outcome_labels)
                try:
                    self._page.wait_for_selector(_sel, state="visible", timeout=8_000)
                except Exception:
                    self._page.wait_for_timeout(500)

                # Only force Live tab if we were already on the page (may be on Past tab)
                if not fresh_nav:
                    self._ensure_live_tab()

                self._screenshot("01_loaded")

                # 2. Click outcome (Up/Down for BTC markets)
                if not self._click_outcome(side):
                    return False
                self._page.wait_for_timeout(300)
                self._screenshot("02_outcome")

                # 3. Enter amount (type exact decimal, fall back to buttons)
                if not self._enter_amount(amount_usd):
                    return False
                self._page.wait_for_timeout(300)
                self._screenshot("03_amount")

                # 4. Click Trade
                if not self._click_trade():
                    return False

                # 5. Wait for embedded wallet signing + immediate signal check
                logger.info("Trade clicked — monitoring for confirmation or failure...")
                self._page.wait_for_timeout(1000)
                quick = self._check_result()
                self._screenshot("04_result")

                # 6. Double-check via order history (ground truth)
                confirmed = self._verify_order_history(
                    self.last_event_slug, side, amount_usd
                )
                if confirmed:
                    return True
                if quick:
                    # Quick check said yes but history didn't confirm —
                    # trust quick check (wallet may still be indexing)
                    logger.warning("History check inconclusive — trusting quick result")
                    return True
                return False

            except Exception as exc:
                logger.error(f"Automator bet error: {exc}")
                self._screenshot("error")
                return False

    def claim_winnings(self) -> int:
        """
        Navigate to the portfolio page and click all visible Redeem buttons.

        Polymarket shows a "Redeem" button on each resolved winning position.
        Returns the number of positions claimed.
        """
        if not self.ready:
            return 0

        with self._lock:
            try:
                logger.info("Navigating to portfolio to claim winnings...")
                self._page.goto(
                    f"{POLYMARKET}/portfolio",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                # Wait for portfolio content to render
                self._page.wait_for_timeout(3000)
                self._screenshot("portfolio_before_claim")

                claimed = 0
                # Keep clicking Redeem buttons until none remain.
                # Flow per position: click Redeem (1) → modal → click Redeem (2) → click Done
                for attempt in range(10):
                    redeem_btns = self._page.locator(
                        "button:has-text('Redeem'), button:has-text('Claim'), button:has-text('Collect')"
                    )
                    count = redeem_btns.count()
                    if count == 0:
                        break

                    logger.info(f"Found {count} Redeem button(s) — clicking first (step 1)")
                    try:
                        # Step 1: initial Redeem click
                        redeem_btns.first.click(timeout=8_000)
                        self._page.wait_for_timeout(2000)

                        # Step 2: confirm click inside modal (second Redeem/Confirm button)
                        try:
                            confirm_btn = self._page.locator(
                                "button:has-text('Redeem'), button:has-text('Confirm'), button:has-text('Claim')"
                            ).last
                            confirm_btn.wait_for(state="visible", timeout=8_000)
                            logger.info("Clicking confirm button (step 2)")
                            confirm_btn.click(timeout=8_000)
                            self._page.wait_for_timeout(3000)
                        except Exception:
                            logger.debug("Confirm button not found — modal may have auto-dismissed")
                            self._page.wait_for_timeout(2000)

                        # Step 3: click Done to close modal
                        try:
                            done_btn = self._page.locator("button:has-text('Done')")
                            if done_btn.count() > 0:
                                logger.info("Clicking Done (step 3)")
                                done_btn.first.click(timeout=5_000)
                                self._page.wait_for_timeout(1500)
                        except Exception:
                            pass

                        claimed += 1
                        self._screenshot(f"portfolio_claimed_{claimed}")
                    except Exception as exc:
                        logger.debug(f"Redeem step error: {exc}")
                        break

                if claimed:
                    logger.info(f"Claimed {claimed} winning position(s)")
                else:
                    logger.info("No Redeem buttons found on portfolio")

                return claimed

            except Exception as exc:
                logger.error(f"claim_winnings error: {exc}")
                self._screenshot("portfolio_error")
                return 0

    # ------------------------------------------------------------------
    # Outcome check — called from main thread on behalf of resolve_async
    # ------------------------------------------------------------------

    def request_outcome_check(self, slug: str, condition_id: str, timeout: int = 90) -> Optional[str]:
        """
        Called from a BACKGROUND thread.
        Signals the main (Playwright) thread to navigate to the event page
        and read the resolution.  Blocks until the main thread responds
        (max `timeout` seconds).  Returns 'Yes', 'No', or None.
        """
        self._oc_slug = slug
        self._oc_cid = condition_id
        self.check_outcome_result = None
        self.check_outcome_done.clear()
        self.check_outcome_request.set()
        if self.check_outcome_done.wait(timeout=timeout):
            return self.check_outcome_result
        logger.warning("Outcome check timed out waiting for main thread")
        return None

    def do_outcome_check(self) -> None:
        """
        Called from the MAIN (Playwright) thread in response to
        check_outcome_request.  Navigates to the event page, calls the
        Gamma API from browser JS context (same network path, fewer
        rate-limit issues), and stores the result.
        """
        slug = self._oc_slug
        cid  = self._oc_cid
        result: Optional[str] = None
        with self._lock:
            try:
                target = f"{POLYMARKET}/event/{slug}"
                if slug not in self._page.url:
                    self._page.goto(target, wait_until="domcontentloaded", timeout=20_000)
                    self._page.wait_for_timeout(3000)
                else:
                    self._page.reload(wait_until="domcontentloaded", timeout=20_000)
                    self._page.wait_for_timeout(2000)

                self._screenshot("outcome_check")

                # Call Gamma API from JS — uses browser network stack
                raw = self._page.evaluate("""
                    async (cid) => {
                        try {
                            const r = await fetch(
                                `https://gamma-api.polymarket.com/markets?conditionId=${cid}&limit=1`
                            );
                            const data = await r.json();
                            const m = data?.[0];
                            if (!m) return null;
                            if (m.resolution) {
                                const v = m.resolution.toLowerCase();
                                if (['yes','up','1','true'].includes(v)) return 'Yes';
                                if (['no','down','0','false'].includes(v)) return 'No';
                            }
                            if (m.closed) {
                                const prices = typeof m.outcomePrices === 'string'
                                    ? JSON.parse(m.outcomePrices) : m.outcomePrices;
                                if (Number(prices?.[0]) >= 0.99) return 'Yes';
                                if (Number(prices?.[1]) >= 0.99) return 'No';
                            }
                        } catch(e) {}
                        return null;
                    }
                """, cid)
                if raw in ("Yes", "No"):
                    result = raw
                    logger.info(f"Browser outcome check → {result}")
                else:
                    logger.info("Browser outcome check: market not resolved yet")
            except Exception as exc:
                logger.warning(f"do_outcome_check error: {exc}")

        self.check_outcome_result = result
        self.check_outcome_done.set()

    def stop(self) -> None:
        """Disconnect Playwright and kill the PWA Chrome process."""
        self.ready = False

        # Kill Chrome first (fast, no async issues)
        if self._chrome_proc and self._chrome_proc.poll() is None:
            self._chrome_proc.terminate()
            try:
                self._chrome_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._chrome_proc.kill()

        # Clean up Playwright (best-effort, may fail during KeyboardInterrupt)
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

        self._page = None
        self._ctx = None
        self._browser = None
        logger.info("Automator stopped — PWA closed")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _screenshot(self, name: str) -> None:
        try:
            path = str(Path(SCREENSHOTS_DIR) / f"{name}.png")
            self._page.screenshot(path=path, full_page=False)
            logger.debug(f"Screenshot: {path}")
        except Exception as exc:
            logger.debug(f"Screenshot failed ({name}): {exc}")

    def get_remaining_seconds(self) -> Optional[int]:
        """
        Read the countdown timer from the current Polymarket page.

        Returns seconds REMAINING in the current 5-min window (0–300),
        or None if the timer isn't visible (wrong page, browser busy, etc.).

        Called from the main loop every 0.5 s — must be fast.  Uses a plain
        JS DOM text-walker with no network request; typically <20 ms.
        """
        if not self.ready:
            return None
        try:
            val = self._page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null, false
                    );
                    let node;
                    while ((node = walker.nextNode())) {
                        const t = (node.textContent || '').trim();
                        const m = t.match(/^(\d{1,2}):(\d{2})$/);
                        if (m) {
                            const mins = parseInt(m[1], 10);
                            const secs = parseInt(m[2], 10);
                            if (mins <= 4 && secs < 60)
                                return mins * 60 + secs;
                        }
                    }
                    return null;
                }
            """)
            if isinstance(val, (int, float)) and 0 <= val <= 300:
                return int(val)
        except Exception:
            pass
        return None

    def _ensure_live_tab(self) -> None:
        """
        Force the page onto the Live/Active market tab.

        Polymarket BTC event pages show both a 'Live' tab (current open
        market) and a 'Past' tab (resolved history).  After get_past_outcomes
        the page may be stuck on 'Past'.  We must switch back before betting.
        """
        for label in ["Live", "Active", "Open", "Current", "Upcoming"]:
            try:
                btn = self._page.locator(
                    f"button:has-text('{label}'), [role='tab']:has-text('{label}')"
                ).first
                if btn.is_visible():
                    btn.click()
                    self._page.wait_for_timeout(400)
                    logger.debug(f"Switched to '{label}' tab before placing bet")
                    return
            except Exception:
                continue
        logger.debug("No Live/Active tab found — assuming already on live market")

    def _click_outcome(self, side: str) -> bool:
        """
        Click the outcome button (Up/Down/Yes/No) for a LIVE market only.

        Polymarket renders ended markets with identical button labels but
        they are inside a closed/resolved container.  We filter those out
        by requiring the button to NOT be inside a container that contains
        any ended-state text.
        """
        labels = (
            ["Up", "Yes", "Higher", "Above"]
            if side == "Yes"
            else ["Down", "No", "Lower", "Below"]
        )

        # Words that indicate the surrounding market row is ended — skip it
        ENDED_KEYWORDS = ("ended", "resolved", "closed", "expired", "final")

        for label in labels:
            try:
                candidates = self._page.locator(f"button:has-text('{label}')").all()
                for btn in candidates:
                    try:
                        # Walk up to a reasonable ancestor and check its text
                        # for ended-state indicators
                        container_text = ""
                        try:
                            # Get the closest section/article/div ancestor text
                            container_text = btn.evaluate(
                                """el => {
                                    let node = el;
                                    for (let i = 0; i < 6; i++) {
                                        node = node.parentElement;
                                        if (!node) break;
                                        const tag = node.tagName.toLowerCase();
                                        if (['section','article','li','form'].includes(tag))
                                            return node.innerText || '';
                                    }
                                    return '';
                                }"""
                            ).lower()
                        except Exception:
                            pass

                        if any(kw in container_text for kw in ENDED_KEYWORDS):
                            logger.debug(f"Skipping '{label}' button in ended container")
                            continue

                        if not btn.is_visible():
                            continue

                        btn.click()
                        logger.info(f"Clicked outcome: {label}")
                        return True
                    except Exception:
                        continue
            except Exception:
                continue

        self._screenshot("err_no_outcome")
        buttons = self._page.locator("button").all_text_contents()
        logger.error(f"No live outcome button found | visible buttons: {buttons[:20]}")
        return False

    def _enter_amount(self, amount_usd: float) -> bool:
        """
        Enter bet amount with full cent precision.

        Primary: type the exact decimal into the input field (e.g. "4.35").
        Fallback: click quick-select buttons (+$1/+$5/+$10/+$100) — integer only.
        """
        amount_str = f"{amount_usd:.2f}"
        selectors = [
            "input[inputmode='decimal']",
            "input[inputmode='numeric']",
            "input[type='number']",
            "input[placeholder*='$']",
            "input[placeholder*='Amount']",
            "input[placeholder*='amount']",
        ]
        for sel in selectors:
            try:
                inp = self._page.locator(sel).first
                inp.wait_for(state="visible", timeout=3000)
                inp.click()
                inp.fill(amount_str)
                self._page.wait_for_timeout(300)
                actual = inp.input_value()
                if actual and float(actual) > 0:
                    logger.info(f"Typed ${amount_usd:.2f} → input shows '{actual}'")
                    return True
            except Exception:
                continue

        # Fallback: button clicks (integer precision only)
        logger.warning(f"Input field not found — falling back to button clicks (${int(amount_usd)})")
        return self._click_amount_buttons(int(amount_usd))

    def _click_amount_buttons(self, target_usd: int) -> bool:
        """Fallback: click +$1/+$5/+$10/+$100 quick-select buttons."""
        clicks = _decompose_amount(target_usd)
        if not clicks:
            self._screenshot("err_no_amount")
            logger.error(f"Cannot decompose amount: ${target_usd}")
            return False

        logger.info(f"Button plan: ${target_usd} = {[f'+${c}' for c in clicks]}")
        for amt in clicks:
            label = f"+${amt}"
            try:
                btn = self._page.locator(f"button:has-text('{label}')").first
                btn.wait_for(state="visible", timeout=3000)
                btn.click()
                self._page.wait_for_timeout(300)
            except Exception:
                self._screenshot("err_no_amount")
                logger.error(f"Button '{label}' not found and no input fallback available")
                return False

        logger.info(f"Amount entered: ${target_usd} via button clicks")
        return True

    def _click_trade(self) -> bool:
        """Click the Buy Up / Buy Down / Trade submit button."""
        # Polymarket renders "Buy Up" or "Buy Down" as the final CTA button.
        # Try exact matches first, then any button containing "Buy".
        labels = ["Buy Up", "Buy Down", "Buy Yes", "Buy No", "Trade", "Place Order", "Confirm"]
        for label in labels:
            try:
                btn = self._page.locator(f"button:has-text('{label}')").first
                btn.wait_for(state="visible", timeout=3000)
                btn.click()
                logger.info(f"Clicked trade button: {label}")
                return True
            except Exception:
                continue

        # Final fallback: any visible button containing "Buy"
        try:
            btn = self._page.locator("button:has-text('Buy')").last
            btn.wait_for(state="visible", timeout=3000)
            text = btn.text_content()
            btn.click()
            logger.info(f"Clicked trade button (fallback): {text}")
            return True
        except Exception:
            pass

        self._screenshot("err_no_trade")
        buttons = self._page.locator("button").all_text_contents()
        logger.error(f"No trade button found | visible buttons: {buttons[:20]}")
        return False

    def _verify_order_history(
        self, event_slug: str, side: str, amount_usd: float, max_attempts: int = 3
    ) -> bool:
        """
        Wait 5 s then check the order history under the order book on the
        event page. Repeats every 8 s until confirmed or max_attempts reached.

        Looks for the "My Orders" / "Orders" tab in the order-book panel, clicks
        it, then searches the visible text for evidence of our filled order.
        """
        logger.info("Waiting 5 s before checking order history…")
        self._page.wait_for_timeout(5_000)

        # Navigate back to event page if we've drifted
        url = f"{POLYMARKET}/event/{event_slug}"
        if event_slug not in (self._page.url or ""):
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                self._page.wait_for_timeout(2000)
            except Exception as exc:
                logger.warning(f"History nav failed: {exc}")

        # Tab labels that reveal order history on the event page
        HISTORY_TABS = ["My Orders", "Orders", "Order History", "Activity", "My Bets", "Positions"]
        side_keyword = "up" if side == "Yes" else "down"
        amt_short = f"${amount_usd:.2f}"
        amt_int   = f"${amount_usd:.0f}"

        for attempt in range(1, max_attempts + 1):
            try:
                # Try to surface the order history tab
                for label in HISTORY_TABS:
                    try:
                        tab = self._page.locator(
                            f"button:has-text('{label}'), "
                            f"[role='tab']:has-text('{label}'), "
                            f"a:has-text('{label}')"
                        ).first
                        if tab.count() and tab.is_visible(timeout=500):
                            tab.click()
                            self._page.wait_for_timeout(800)
                            break
                    except Exception:
                        continue

                # Read visible page text
                try:
                    page_text = self._page.inner_text("body")[:8000].lower()
                except Exception:
                    page_text = ""

                hits: list[str] = []
                if "filled" in page_text or "matched" in page_text:
                    hits.append("filled/matched")
                if side_keyword in page_text:
                    hits.append(f"side:{side_keyword}")
                if "shares" in page_text or "shares @" in page_text:
                    hits.append("shares")
                if amt_short.lower() in page_text or amt_int.lower() in page_text:
                    hits.append(f"amount:{amt_short}")

                self._screenshot(f"history_check_{attempt}")

                if len(hits) >= 2:
                    # Extract actual fill price, e.g. "bought 1.39 up at 72¢"
                    import re as _re
                    _pm = _re.search(r'at\s+(\d+)¢', page_text)
                    if _pm:
                        self.last_fill_price = int(_pm.group(1)) / 100.0
                        logger.info(
                            f"Order confirmed in history (attempt {attempt}/{max_attempts}): {hits} "
                            f"| fill price={self.last_fill_price:.2f}"
                        )
                    else:
                        logger.info(f"Order confirmed in history (attempt {attempt}/{max_attempts}): {hits}")
                    return True

                logger.info(
                    f"History check {attempt}/{max_attempts} — found: {hits or 'nothing'}"
                    f" — retrying in 8 s…"
                )

            except Exception as exc:
                logger.warning(f"History check error (attempt {attempt}): {exc}")

            if attempt < max_attempts:
                self._page.wait_for_timeout(8_000)

        logger.warning("Order not found in history after all attempts")
        return False

    def _check_result(self) -> bool:
        """
        Wait up to 10 s for a clear success or failure signal after clicking Buy.

        Failure signals (return False immediately):
          - Error/rejection toast text visible
          - Buy button reappears within the window

        Success signals (return True immediately):
          - "Position added", "Trade confirmed", "Order submitted" etc.
          - Amount input clears to 0 / placeholder reappears

        If neither fires within 12 s, assume submitted (on-chain will verify).
        """
        FAIL_PHRASES = [
            "trade failed",          # Polymarket toast: "Trade failed"
            "transaction failed", "transaction rejected", "user rejected",
            "insufficient", "try again", "something went wrong",
            "error occurred", "not enough", "denied",
        ]
        SUCCESS_PHRASES = [
            "position added", "trade confirmed", "order filled",
            "order submitted", "submitted", "success", "shares purchased",
            "shares @",          # Polymarket toast: "4.37 shares @ 23¢"
            "on up",             # Polymarket toast: "Buy $1 on Up"
            "on down",           # Polymarket toast: "Buy $1 on Down"
            "bought",            # Polymarket toast: "Bought 2.50 shares @ 48¢"
        ]

        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                page_text = self._page.inner_text("body")[:8000].lower()
            except Exception:
                page_text = ""

            for phrase in FAIL_PHRASES:
                if phrase in page_text:
                    logger.error(f"Transaction failure detected: '{phrase}'")
                    self._screenshot("err_trade_failed")
                    return False

            for phrase in SUCCESS_PHRASES:
                if phrase in page_text:
                    logger.info(f"Trade confirmed: '{phrase}'")
                    return True

            # Also check for the bottom-right notification toast via locator
            # (may be in a portal outside the main DOM tree)
            try:
                toast = self._page.locator("[class*='toast'], [class*='notification'], [class*='alert']").filter(has_text="shares")
                if toast.count() > 0:
                    logger.info("Trade confirmed: toast notification with 'shares' detected")
                    return True
            except Exception:
                pass

            self._page.wait_for_timeout(500)

        # Final heuristic: if the amount field cleared to 0/empty the trade was accepted
        # (Polymarket resets the input after a successful submission; "Buy Up" stays visible)
        try:
            amt_inputs = self._page.locator("input[type='number'], input[placeholder*='$'], input[placeholder*='amount']")
            for i in range(amt_inputs.count()):
                val = amt_inputs.nth(i).input_value(timeout=500)
                if val in ("", "0", "0.00"):
                    logger.info("Trade submitted (amount field cleared after submission)")
                    return True
        except Exception:
            pass

        logger.warning("Trade result inconclusive after 10 s — treating as submitted; on-chain will verify")
        return True  # optimistic: no failure detected → treat as placed, history will confirm

    def get_past_outcomes(self, event_slug: str, n: int = 10) -> list[str]:
        """
        Navigate to the event page, click the 'Past' tab, and return the last
        n resolved outcomes as a list of 'Yes' or 'No' (most recent first).

        Used to sync D'Alembert state from real market history on the site —
        survives crashes and restarts since it reads from Polymarket directly.

        Returns an empty list if scraping fails (caller falls back to internal state).
        """
        if not self.ready:
            return []

        with self._lock:
            try:
                url = f"{POLYMARKET}/event/{event_slug}"
                # Navigate if we're not already there
                if event_slug not in self._page.url:
                    self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    self._page.wait_for_timeout(2500)

                # Click the "Past" / "Resolved" tab
                clicked_past = False
                for label in ["Past", "Resolved", "Ended", "History", "Closed"]:
                    try:
                        btn = self._page.locator(
                            f"button:has-text('{label}'), [role='tab']:has-text('{label}')"
                        ).first
                        if btn.is_visible():
                            btn.click()
                            self._page.wait_for_timeout(2000)
                            logger.debug(f"Clicked '{label}' tab for past outcomes")
                            clicked_past = True
                            break
                    except Exception:
                        continue

                if not clicked_past:
                    logger.debug("No 'Past' tab found — skipping history scrape")
                    return []

                self._screenshot("past_markets")

                # --- Scrape outcomes from the rendered list ---
                outcomes: list[str] = []

                # Strategy A: look for explicit outcome badge elements
                for sel in [
                    "[class*='outcome']", "[class*='result']", "[class*='resolved']",
                    "[data-outcome]", "[class*='status']",
                ]:
                    try:
                        els = self._page.locator(sel).all()
                        for el in els[:n * 2]:
                            txt = (el.text_content() or "").strip().upper()
                            if txt in ("YES", "UP", "HIGHER", "ABOVE"):
                                outcomes.append("Yes")
                            elif txt in ("NO", "DOWN", "LOWER", "BELOW"):
                                outcomes.append("No")
                        if outcomes:
                            break
                    except Exception:
                        continue

                # Strategy B: scan list items for resolution keywords
                if not outcomes:
                    try:
                        items = self._page.locator(
                            "li, [role='listitem'], [class*='market-row'], [class*='market-card']"
                        ).all()
                        for item in items[:n * 2]:
                            try:
                                txt = (item.text_content() or "").upper()
                                if "YES" in txt and any(k in txt for k in ("RESOLVED", "WON", "CLOSED")):
                                    outcomes.append("Yes")
                                elif "NO" in txt and any(k in txt for k in ("RESOLVED", "WON", "CLOSED")):
                                    outcomes.append("No")
                            except Exception:
                                continue
                    except Exception:
                        pass

                # Strategy C: read full page text and look for YES/NO patterns
                if not outcomes:
                    try:
                        page_text = self._page.inner_text("body")
                        import re
                        # Match lines like "Yes  $91,450  Resolved" or "No • Resolved"
                        for m in re.finditer(r'\b(Yes|No|YES|NO)\b.{0,60}(Resolved|Closed|Ended)', page_text):
                            word = m.group(1).capitalize()
                            if word in ("Yes", "No"):
                                outcomes.append(word)
                            if len(outcomes) >= n:
                                break
                    except Exception:
                        pass

                outcomes = outcomes[:n]
                logger.info(f"Past outcomes from website ({len(outcomes)}): {outcomes}")

                # Navigate back to Live tab — critical so the next bet
                # clicks on the live market, not an ended one.
                returned_to_live = False
                for label in ["Live", "Active", "Open", "Current", "Upcoming"]:
                    try:
                        btn = self._page.locator(
                            f"button:has-text('{label}'), [role='tab']:has-text('{label}')"
                        ).first
                        if btn.is_visible():
                            btn.click()
                            self._page.wait_for_timeout(1500)
                            returned_to_live = True
                            logger.debug(f"Returned to '{label}' tab after reading past outcomes")
                            break
                    except Exception:
                        continue
                if not returned_to_live:
                    logger.warning(
                        "Could not find Live tab after reading past outcomes — "
                        "place_bet will force it via _ensure_live_tab()"
                    )

                return outcomes

            except Exception as exc:
                logger.warning(f"get_past_outcomes failed: {exc}")
                return []
