import threading
import asyncio
import time
from typing import Tuple, Optional
from pathlib import Path
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

class JioPlaywrightVerifier:
    """
    High-performance Jio Number Validator using Playwright Chromium.
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
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            self._context = await self._browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            self._page = await self._context.new_page()
            await self._page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle", timeout=30000)
            self._is_active = True
        except Exception as e:
            print(f"[JioVerifier] Browser init error: {e}")
            self._is_active = False

    async def _cleanup(self):
        try:
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
            # Ensure on login page
            if not self._page or "/login" not in self._page.url:
                await self._page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle", timeout=15000)

            input_field = self._page.locator("#inputField")
            await input_field.click()
            await input_field.fill("")
            await input_field.fill(clean_phone)

            btn = self._page.locator("button:has-text('Generate OTP')")
            await btn.click()

            # Poll for response for up to 3 seconds
            is_invalid = False
            error_msg = ""

            for _ in range(6):
                await asyncio.sleep(0.4)

                # Check error container
                error_el = self._page.locator("[class*='errorContainer']")
                if await error_el.count() > 0:
                    txt = (await error_el.first.inner_text()).strip()
                    if "non-Jio" in txt or "invalid" in txt.lower():
                        is_invalid = True
                        error_msg = txt.replace("\n", " ")
                        break

                # Fallback text check
                text_el = self._page.get_by_text("non-Jio number")
                if await text_el.count() > 0:
                    is_invalid = True
                    error_msg = "It seems you have entered a non-Jio number"
                    break

                # Check if OTP screen appeared
                otp_screen = self._page.locator("input[maxlength='1']")
                if await otp_screen.count() > 0:
                    break

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
            else:
                return True, f"Valid Jio Subscriber [{elapsed}s]"
        except Exception as e:
            return True, f"Verification exception ({e})"

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
