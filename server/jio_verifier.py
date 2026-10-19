import threading
import asyncio
import time
import os
import subprocess
import requests
from typing import Tuple, Optional
from pathlib import Path
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\hazra\AppData\Local"), "Google", "Chrome", "User Data", "JioBotProfile")

def is_cdp_available() -> bool:
    try:
        r = requests.get(f"{CDP_URL}/json/version", timeout=1.0)
        return r.status_code == 200
    except Exception:
        return False

def ensure_chrome_cdp():
    if is_cdp_available():
        return
    os.makedirs(PROFILE_DIR, exist_ok=True)
    cmd = [
        CHROME_EXE,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized"
    ]
    DETACHED = 0x00000008
    subprocess.Popen(cmd, creationflags=DETACHED, close_fds=True)
    for _ in range(25):
        if is_cdp_available():
            return
        time.sleep(0.4)

class JioPlaywrightVerifier:
    """
    High-performance Jio Number Validator using Playwright Chromium / Real Chrome via CDP.
    Runs inside a dedicated background worker thread with its own asyncio loop,
    ensuring thread-safety and zero interference with FastAPI or Windows asyncio loop.
    """
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._is_active = False
        self._is_cdp = False

    def start(self):
        with self._lock:
            if self._is_active:
                return
            self._ready_event.clear()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="JioVerifierThread", daemon=True)
            self._thread.start()
            # Wait up to 25s for browser initialization
            if not self._ready_event.wait(timeout=25):
                print("[JioVerifier] Warning: Initialization timed out.")

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._init_browser())
        self._ready_event.set()

        # Run loop until stopped
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self._cleanup())
            self._loop.close()

    async def _init_browser(self):
        try:
            self._playwright = await async_playwright().start()
            if not self.headless:
                ensure_chrome_cdp()
                try:
                    self._browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
                    self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
                    self._page = await self._context.new_page()
                    self._is_cdp = True
                except Exception as e:
                    print(f"[JioVerifier] CDP connect failed ({e}), fallback to standalone Chrome...")
                    self._browser = await self._playwright.chromium.launch(
                        channel="chrome",
                        headless=False,
                        args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
                    )
                    self._context = await self._browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                        no_viewport=True
                    )
                    self._page = await self._context.new_page()
                    self._is_cdp = False
            else:
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                self._context = await self._browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800}
                )
                self._page = await self._context.new_page()
                self._is_cdp = False

            await self._page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle", timeout=30000)
            try:
                await self._page.bring_to_front()
            except Exception:
                pass
            self._is_active = True
        except Exception as e:
            print(f"[JioVerifier] Browser init error: {e}")
            self._is_active = False

    async def _cleanup(self):
        try:
            if not self._is_cdp:
                if self._page:
                    await self._page.close()
                if self._context:
                    await self._context.close()
                if self._browser:
                    await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._is_active = False

    def verify_number(self, phone: str) -> Tuple[bool, str]:
        """
        Verify whether a 10-digit number is an active Jio subscriber on Jio Selfcare.
        Returns:
            (True, reason)  -> Valid Jio number
            (False, reason) -> Invalid / Non-Jio (Zonk)
        """
        if not self._is_active or not self._loop or not self._page:
            # Fallback if verifier failed to init
            return True, "Verifier not ready (bypassed)"

        clean_phone = "".join(filter(str.isdigit, str(phone)))[-10:]
        if len(clean_phone) != 10:
            return False, f"Invalid length ({len(clean_phone)} digits)"

        future = asyncio.run_coroutine_threadsafe(self._do_verify(clean_phone), self._loop)
        try:
            return future.result(timeout=12)
        except Exception as e:
            return True, f"Verification timeout ({e})"

    async def _do_verify(self, clean_phone: str) -> Tuple[bool, str]:
        t0 = time.time()
        try:
            if not self._page or self._page.is_closed():
                if self._context:
                    self._page = await self._context.new_page()

            # Ensure on login page
            if not self._page or "/login" not in self._page.url:
                await self._page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle", timeout=15000)

            try:
                await self._page.bring_to_front()
            except Exception:
                pass

            input_field = self._page.locator("#inputField")
            await input_field.click()
            await input_field.fill("")
            await input_field.fill(clean_phone)

            btn = self._page.locator("button:has-text('Generate OTP')")
            await btn.click()

            # Poll for response for up to 8 seconds
            is_invalid = False
            error_msg = ""
            otp_screen_ready = False

            for _ in range(25):
                await asyncio.sleep(0.3)

                # Check error locators
                error_locators = [
                    self._page.locator("[class*='errorContainer']"),
                    self._page.locator("[class*='errorText']"),
                    self._page.locator("[class*='errorMessage']"),
                    self._page.get_by_text("non-Jio", exact=False),
                    self._page.get_by_text("invalid", exact=False),
                    self._page.get_by_text("not a Jio", exact=False),
                    self._page.get_by_text("try again with a Jio", exact=False),
                ]

                for loc in error_locators:
                    try:
                        if await loc.count() > 0 and await loc.first.is_visible():
                            txt = (await loc.first.inner_text()).strip()
                            if any(k in txt.lower() for k in ["non-jio", "invalid", "not a jio", "try again", "enter a valid"]):
                                is_invalid = True
                                error_msg = txt.replace("\n", " ")
                                break
                    except Exception:
                        pass

                if is_invalid:
                    break

                # Check if OTP screen appeared
                otp_screen = self._page.locator("input[maxlength='1']")
                try:
                    if await otp_screen.count() >= 6 and await otp_screen.first.is_visible():
                        otp_screen_ready = True
                        break
                except Exception:
                    pass

            elapsed = round(time.time() - t0, 2)

            # Capture live screenshot for Web Dashboard visual inspector
            try:
                shot_path = Path(__file__).resolve().parent.parent / "web" / "public" / "live_jio.png"
                shot_path.parent.mkdir(parents=True, exist_ok=True)
                await self._page.screenshot(path=str(shot_path), type="png")
            except Exception:
                pass

            if is_invalid:
                return False, f"Non-Jio (Zonk): {error_msg} [{elapsed}s]"
            elif not otp_screen_ready:
                return False, f"Non-Jio: OTP screen did not appear [{elapsed}s]"
            else:
                return True, f"Valid Jio Subscriber [{elapsed}s]"
        except Exception as e:
            return False, f"Verification failed ({e})"

    def fill_otp(self, otp: str):
        """Asynchronously fill OTP into the Jio Selfcare verification boxes."""
        if not self._is_active or not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self._do_fill_otp(str(otp).strip()), self._loop)

    async def _do_fill_otp(self, otp: str):
        try:
            if not self._page:
                return
            boxes = self._page.locator("input[maxlength='1']")
            cnt = await boxes.count()
            if cnt >= len(otp):
                for i, digit in enumerate(otp):
                    await boxes.nth(i).fill(digit)
            else:
                input_field = self._page.locator("#otpInput, input[type='number'], input[type='tel']")
                if await input_field.count() > 0:
                    await input_field.first.fill(otp)

            await asyncio.sleep(0.8)
            submit_btn = self._page.locator("button:has-text('Submit'), button:has-text('Proceed'), button:has-text('Verify'), button[type='submit']")
            if await submit_btn.count() > 0:
                await submit_btn.first.click()

            # Tangkap screenshot setelah OTP di-submit untuk Live View GUI
            await asyncio.sleep(1.5)
            try:
                shot_path = Path(__file__).resolve().parent.parent / "web" / "public" / "live_jio.png"
                await self._page.screenshot(path=str(shot_path), type="png")
            except Exception:
                pass
        except Exception as e:
            print(f"[JioVerifier] Error auto-filling OTP: {e}")

    def set_mode(self, headless: bool):
        """Switch between headless and visual (headed) browser mode."""
        if self.headless == headless:
            return
        self.headless = headless
        if self._is_active:
            self.stop()
            self.start()

    def stop(self):
        with self._lock:
            if not self._is_active:
                return
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            self._is_active = False

# Global verifier instance
verifier_instance: Optional[JioPlaywrightVerifier] = None

def get_jio_verifier(headless: bool = False) -> JioPlaywrightVerifier:
    global verifier_instance
    if verifier_instance is None:
        verifier_instance = JioPlaywrightVerifier(headless=headless)
        verifier_instance.start()
    return verifier_instance
