import time
import threading
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Callable
from collections import deque

from .grizzly_client import GrizzlySMSClient
from .cleanup_manager import BackgroundCleanupManager

# Tier definitions matching Jio cellular circle allocations
TIER_1_PREFIXES = ["600", "939", "700", "701", "702"]

TIER_2_PREFIXES = [
    "620", "626", "628", "629", "630", "635", "636", "637", "638", "639",
    "703", "704", "705", "706", "707", "708", "709",
    "790", "797", "798", "799",
    "930", "931", "932", "933", "934", "935", "936", "937", "938"
]

TIER_3_PREFIXES = [
    "920", "921", "922", "923", "924", "925", "926", "927", "928", "929",
    "740", "741", "742",
    "750", "751", "752",
    "760", "761", "762", "763", "764", "765", "766", "767",
    "786", "787", "788", "789",
    "821", "822", "824", "827", "828", "829",
    "843", "844", "846", "847", "848", "849",
    "877", "878", "879",
    "896", "897", "898", "899"
]

PREFIX_PRESETS = {
    "tier1": TIER_1_PREFIXES,
    "strict": TIER_1_PREFIXES,
    "tier2": TIER_1_PREFIXES + TIER_2_PREFIXES,
    "popular": TIER_1_PREFIXES + TIER_2_PREFIXES,
    "tier3": TIER_1_PREFIXES + TIER_2_PREFIXES + TIER_3_PREFIXES,
    "all": TIER_1_PREFIXES + TIER_2_PREFIXES + TIER_3_PREFIXES,
}

def extract_national_number(raw_phone: str, country_prefix: str = "91") -> str:
    cleaned = "".join(c for c in str(raw_phone) if c.isdigit())
    if cleaned.startswith(country_prefix) and len(cleaned) == len(country_prefix) + 10:
        return cleaned[len(country_prefix):]
    if len(cleaned) > 10:
        return cleaned[-10:]
    return cleaned

def find_matching_prefix(national_num: str, target_prefixes: List[str]) -> Optional[str]:
    for p in target_prefixes:
        if national_num.startswith(p):
            return p
    return None

class BotEngine:
    """
    Core execution engine for Grizzly MyJio automation.
    Thread-safe, decoupled from UI, reactive event-driven state architecture.
    """
    def __init__(
        self,
        api_key: str,
        service: str = "jio",
        country: str = "22",
        max_price: float = 0.3,
        preset: str = "tier1",
        custom_prefixes: Optional[str] = None,
        batch_size: int = 2,
        retry_delay: int = 4,
        cleanup_delay: int = 120,
        verify_jio: bool = True,
        headed_browser: bool = True,
    ):
        self.api_key = api_key
        self.service = service
        self.country = country
        self.max_price = float(max_price)
        self.preset = preset
        self.custom_prefixes = custom_prefixes or ""
        self.batch_size = max(1, int(batch_size))
        self.retry_delay = max(1, int(retry_delay))
        self.cleanup_delay = max(30, int(cleanup_delay))
        self.verify_jio = verify_jio
        self.headed_browser = headed_browser
        self._verifier = None

        self.client = GrizzlySMSClient(api_key=self.api_key)
        self.cleanup_manager = BackgroundCleanupManager(
            client=self.client,
            cooldown_delay=self.cleanup_delay,
            on_log=self._handle_log,
            on_refund=self._handle_refund,
        )

        self._state_lock = threading.RLock()
        self._is_running = False
        self._is_paused = False
        self._skip_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        self.balance: Optional[float] = None
        self.active_number: Optional[str] = None
        self.active_id: Optional[str] = None
        self.matched_prefix: Optional[str] = None
        self.candidate_queue: List[Dict[str, Any]] = []
        self.latest_otp: Optional[str] = None
        self.status_message: str = "Idle"
        self.otps_count: int = 0
        self.searches_count: int = 0

        data_dir = Path(__file__).resolve().parent.parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        hist_init = []
        logs_init = []
        if (data_dir / "history.json").exists():
            try:
                with open(data_dir / "history.json", "r", encoding="utf-8") as f:
                    hist_init = json.load(f)
            except Exception:
                pass
        if (data_dir / "logs.json").exists():
            try:
                with open(data_dir / "logs.json", "r", encoding="utf-8") as f:
                    logs_init = json.load(f)
            except Exception:
                pass

        self.history: deque = deque(hist_init, maxlen=100)
        self.logs: deque = deque(logs_init, maxlen=200)
        self.last_verified: Optional[Dict[str, Any]] = None
        self.subscribers: List[Callable[[str, Any], None]] = []

    def _save_history(self):
        try:
            data_dir = Path(__file__).resolve().parent.parent / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            with open(data_dir / "history.json", "w", encoding="utf-8") as f:
                json.dump(list(self.history), f, indent=2)
        except Exception:
            pass

    def _save_logs(self):
        try:
            data_dir = Path(__file__).resolve().parent.parent / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            with open(data_dir / "logs.json", "w", encoding="utf-8") as f:
                json.dump(list(self.logs), f, indent=2)
        except Exception:
            pass

    @property
    def verifier(self):
        if self._verifier is None:
            from .jio_verifier import get_jio_verifier
            self._verifier = get_jio_verifier(headless=not self.headed_browser)
        return self._verifier

    def subscribe(self, callback: Callable[[str, Any], None]):
        """Subscribe to real-time events (state_changed, log, otp_received)."""
        with self._state_lock:
            if callback not in self.subscribers:
                self.subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str, Any], None]):
        with self._state_lock:
            if callback in self.subscribers:
                self.subscribers.remove(callback)

    def _broadcast(self, event_type: str, data: Any):
        with self._state_lock:
            subs = list(self.subscribers)
        for cb in subs:
            try:
                cb(event_type, data)
            except Exception:
                pass

    def _handle_log(self, level: str, message: str):
        timestamp = time.strftime("%H:%M:%S")
        entry = {"timestamp": timestamp, "level": level, "message": message}
        with self._state_lock:
            self.logs.append(entry)
        self._save_logs()
        self._broadcast("log", entry)

    def _handle_refund(self, act_id: str, phone: str):
        # Refresh balance after refund
        bal, _ = self.client.get_balance()
        if bal is not None:
            with self._state_lock:
                self.balance = bal
            self._broadcast("state_changed", self.get_state())

    def get_active_prefixes(self) -> List[str]:
        if self.custom_prefixes.strip():
            tokens = [t.strip() for t in self.custom_prefixes.split(",") if t.strip()]
            resolved = []
            for t in tokens:
                if t.lower() in PREFIX_PRESETS:
                    resolved.extend(PREFIX_PRESETS[t.lower()])
                else:
                    resolved.append(t)
            return sorted(list(dict.fromkeys(resolved)))
        return list(PREFIX_PRESETS.get(self.preset.lower(), TIER_1_PREFIXES))

    def get_state(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "is_running": self._is_running,
                "is_paused": self._is_paused,
                "status_message": self.status_message,
                "balance": self.balance,
                "active_number": self.active_number,
                "active_id": self.active_id,
                "matched_prefix": self.matched_prefix,
                "queue_count": len(self.candidate_queue),
                "candidates": list(self.candidate_queue),
                "latest_otp": self.latest_otp,
                "otps_count": self.otps_count,
                "searches_count": self.searches_count,
                "cooldown_pending": self.cleanup_manager.get_pending_list(),
                "last_verified": self.last_verified,
                "config": {
                    "api_key_configured": bool(self.api_key),
                    "preset": self.preset,
                    "custom_prefixes": self.custom_prefixes,
                    "active_prefixes_count": len(self.get_active_prefixes()),
                    "max_price": self.max_price,
                    "batch_size": self.batch_size,
                    "retry_delay": self.retry_delay,
                    "verify_jio": self.verify_jio,
                    "headed_browser": self.headed_browser,
                }
            }

    def update_config(
        self,
        api_key: Optional[str] = None,
        preset: Optional[str] = None,
        custom_prefixes: Optional[str] = None,
        max_price: Optional[float] = None,
        batch_size: Optional[int] = None,
        retry_delay: Optional[int] = None,
        verify_jio: Optional[bool] = None,
        headed_browser: Optional[bool] = None,
    ):
        with self._state_lock:
            if api_key is not None:
                self.api_key = api_key.strip()
                self.client.set_api_key(self.api_key)
            if preset is not None and preset.lower() in PREFIX_PRESETS:
                self.preset = preset.lower()
            if custom_prefixes is not None:
                self.custom_prefixes = custom_prefixes.strip()
            if max_price is not None:
                self.max_price = round(float(max_price), 2)
            if batch_size is not None:
                self.batch_size = max(1, int(batch_size))
            if retry_delay is not None:
                self.retry_delay = max(1, int(retry_delay))
            if verify_jio is not None:
                self.verify_jio = bool(verify_jio)
            if headed_browser is not None:
                self.headed_browser = bool(headed_browser)
                if self._verifier:
                    self._verifier.set_mode(headless=not self.headed_browser)

        self._handle_log("INFO", "Configuration updated.")
        self._broadcast("state_changed", self.get_state())

    def start(self):
        with self._state_lock:
            if self._is_running:
                if self._is_paused:
                    self.resume()
                return
            self._is_running = True
            self._is_paused = False
            self._stop_event.clear()
            self._skip_event.clear()
            self.status_message = "Starting automator..."

        self.cleanup_manager.start()
        self._worker_thread = threading.Thread(target=self._run_loop, name="BotEngineWorker", daemon=True)
        self._worker_thread.start()
        self._handle_log("INFO", "🚀 Bot Engine started.")
        self._broadcast("state_changed", self.get_state())

    def pause(self):
        with self._state_lock:
            if not self._is_running or self._is_paused:
                return
            self._is_paused = True
            self.status_message = "⏸️ Paused"
        self._handle_log("WARN", "Bot execution paused.")
        self._broadcast("state_changed", self.get_state())

    def resume(self):
        with self._state_lock:
            if not self._is_running or not self._is_paused:
                return
            self._is_paused = False
            self.status_message = "Resuming search..."
        self._handle_log("SUCCESS", "▶️ Bot execution resumed.")
        self._broadcast("state_changed", self.get_state())

    def skip(self):
        """Skip current number and proceed to next candidate or search."""
        with self._state_lock:
            if not self.active_id:
                return
        self._handle_log("INFO", f"⏩ User requested skip for #{self.active_id}.")
        self._skip_event.set()

    def stop(self):
        with self._state_lock:
            if not self._is_running:
                return
            self._is_running = False
            self._is_paused = False
            self._stop_event.set()
            self._skip_event.set()
            self.status_message = "Stopped"

        self._handle_log("WARN", "🛑 Bot Engine stopping...")
        self._broadcast("state_changed", self.get_state())

    def _run_loop(self):
        self._handle_log("INFO", "Checking Grizzly SMS account balance...")
        bal, err = self.client.get_balance()
        if bal is not None:
            with self._state_lock:
                self.balance = bal
            self._handle_log("SUCCESS", f"Initial balance: ${bal:.2f} USD")
        elif err:
            self._handle_log("ERROR", f"Balance check error: {err}")

        # Sweep uncancelled activations on startup
        try:
            active_acts = self.client.get_active_activations()
            if active_acts:
                self._handle_log("INFO", f"Adopting {len(active_acts)} stale activations for background refund...")
                for item in active_acts:
                    aid = item["activation_id"]
                    ph = item["phone_number"]
                    self.cleanup_manager.add(aid, ph, requested_at=time.time() - 90)
        except Exception as e:
            self._handle_log("WARN", f"Could not check stale activations: {e}")

        while not self._stop_event.is_set():
            # Check pause
            while self._is_paused and not self._stop_event.is_set():
                time.sleep(0.5)

            if self._stop_event.is_set():
                break

            # Balance check
            bal, _ = self.client.get_balance()
            if bal is not None:
                with self._state_lock:
                    self.balance = bal

            # Balance check: only pause if balance is strictly too low to buy even 1 number (< $0.08)
            min_required = 0.08
            if bal is not None and bal < min_required:
                pending_count = len(self.cleanup_manager.get_pending_list())
                if pending_count > 0:
                    with self._state_lock:
                        self.status_message = f"Waiting for {pending_count} balance refund(s)..."
                    self._handle_log("WARN", f"Balance (${bal:.2f}) low. Waiting for {pending_count} cooldown refund(s)...")
                else:
                    with self._state_lock:
                        self.status_message = "Insufficient balance on Grizzly SMS."
                    self._handle_log("ERROR", f"Insufficient balance (${bal:.2f}). Please top up Grizzly SMS.")
                self._broadcast("state_changed", self.get_state())
                time.sleep(5)
                continue

            # Dynamically size batch to avoid NO_BALANCE on concurrent workers
            effective_batch = self.batch_size
            if bal is not None:
                if bal < 0.25:
                    effective_batch = 1
                elif bal < (self.batch_size * 0.15):
                    effective_batch = max(1, int(bal // 0.12))

            with self._state_lock:
                self.searches_count += 1
                self.status_message = f"Requesting {effective_batch} number(s)..."
                self.active_number = None
                self.active_id = None
                self.matched_prefix = None
                self.candidate_queue = []
            self._broadcast("state_changed", self.get_state())

            active_prefixes = self.get_active_prefixes()
            batch_candidates = []

            # Ensure max_price parameter does not exceed current account balance (Grizzly API requirement)
            req_max_price = self.max_price
            if bal is not None and bal > 0:
                req_max_price = min(self.max_price, round(bal, 2))

            # Concurrent batch request
            def _fetch_one(idx: int):
                t_req = time.time()
                aid, ph, raw, err = self.client.request_number(
                    service=self.service,
                    country=self.country,
                    max_price=req_max_price
                )
                return idx, aid, ph, raw, err, t_req

            with ThreadPoolExecutor(max_workers=effective_batch) as executor:
                futures = [executor.submit(_fetch_one, i) for i in range(1, effective_batch + 1)]
                for fut in as_completed(futures):
                    if self._stop_event.is_set():
                        break
                    try:
                        idx, aid, ph, raw, err, t_req = fut.result()
                    except Exception as e:
                        self._handle_log("ERROR", f"Worker error: {e}")
                        continue

                    if not aid or not ph:
                        if raw == "NO_NUMBERS":
                            self._handle_log("WARN", f"[{idx}/{effective_batch}] No numbers available at ${self.max_price}.")
                        elif raw == "NO_BALANCE":
                            pending_count = len(self.cleanup_manager.get_pending_list())
                            if pending_count > 0:
                                self._handle_log("WARN", f"⏳ Saldo (${bal:.2f}) habis sementara. Menunggu {pending_count} nomor dalam antrean refund...")
                                time.sleep(6)
                            else:
                                self._handle_log("WARN", f"[{idx}/{effective_batch}] Insufficient balance on Grizzly SMS (${bal:.2f}).")
                        else:
                            self._handle_log("WARN", f"[{idx}/{effective_batch}] Request response: {raw or err}")
                        continue

                    nat_num = extract_national_number(ph, "91")
                    match_p = find_matching_prefix(nat_num, active_prefixes)

                    if not match_p or self._stop_event.is_set():
                        # Unmatched -> add to cleanup queue
                        self._handle_log("INFO", f"[{idx}/{self.batch_size}] Discarding {nat_num} (Prefix didn't match).")
                        self.cleanup_manager.add(aid, nat_num, requested_at=t_req)
                    else:
                        self._handle_log("SUCCESS", f"[{idx}/{self.batch_size}] 🎉 MATCH! +91 {nat_num} (Prefix: {match_p})")
                        batch_candidates.append({
                            "act_id": aid,
                            "phone": ph,
                            "national_number": nat_num,
                            "prefix": match_p,
                            "requested_at": t_req,
                        })

            if not batch_candidates:
                time.sleep(self.retry_delay)
                continue

            with self._state_lock:
                self.candidate_queue = list(batch_candidates)
            self._broadcast("state_changed", self.get_state())

            # Sequential testing of candidate queue
            while batch_candidates and not self._stop_event.is_set():
                cand = batch_candidates.pop(0)
                aid = cand["act_id"]
                nat_num = cand["national_number"]
                prefix = cand["prefix"]
                req_t = cand["requested_at"]

                with self._state_lock:
                    self.active_id = aid
                    self.active_number = nat_num
                    self.matched_prefix = prefix
                    self.status_message = f"Testing +91 {nat_num} via Playwright..." if self.verify_jio else f"Active Jio: +91 {nat_num}"
                    self.candidate_queue = list(batch_candidates)
                self._broadcast("state_changed", self.get_state())

                if self.verify_jio:
                    mode_desc = "Visual Chrome window" if self.headed_browser else "Headless background"
                    self._handle_log("INFO", f"🤖 Playwright ({mode_desc}): Verifying +91 {nat_num} on Jio Selfcare...")
                    try:
                        is_valid, reason = self.verifier.verify_number(nat_num)
                    except Exception as e:
                        is_valid = True
                        reason = f"Verification error ({e})"

                    with self._state_lock:
                        self.last_verified = {
                            "number": nat_num,
                            "prefix": prefix,
                            "is_valid": is_valid,
                            "reason": reason,
                            "time": time.strftime("%H:%M:%S"),
                            "timestamp": time.time(),
                        }
                    self._broadcast("screenshot_updated", self.last_verified)

                    if not is_valid:
                        self._handle_log("WARN", f"❌ Playwright: +91 {nat_num} is {reason}. Auto-discarding & queuing refund.")
                        self.cleanup_manager.add(aid, nat_num, requested_at=req_t)
                        self.history.appendleft({
                            "id": aid,
                            "phone": nat_num,
                            "prefix": prefix,
                            "status": "ZONK (Non-Jio)",
                            "otp": None,
                            "time": time.strftime("%H:%M:%S"),
                        })
                        self._save_history()
                        continue

                    self._handle_log("SUCCESS", f"🎉 Playwright: +91 {nat_num} is a VALID Jio subscriber! ({reason})")
                    with self._state_lock:
                        self.status_message = f"Valid Jio: +91 {nat_num} (Awaiting OTP)"
                    self._broadcast("state_changed", self.get_state())
                else:
                    self._handle_log("SUCCESS", f"📱 ACTIVE NUMBER: +91 {nat_num} (Prefix {prefix}). Paste into MyJio!")

                self._skip_event.clear()

                # Poll for OTP until code arrives, skipped, or stopped
                got_otp = False
                otp_code = None
                poll_interval = 3

                while not self._stop_event.is_set():
                    if self._skip_event.is_set():
                        self._handle_log("WARN", f"⏩ Number +91 {nat_num} skipped. Queuing for refund.")
                        self.cleanup_manager.add(aid, nat_num, requested_at=req_t)
                        self.history.appendleft({
                            "id": aid,
                            "phone": nat_num,
                            "prefix": prefix,
                            "status": "SKIPPED",
                            "otp": None,
                            "time": time.strftime("%H:%M:%S"),
                        })
                        self._save_history()
                        break

                    res, _ = self.client.get_status(aid)
                    if res.startswith("STATUS_OK:"):
                        otp_code = res.split(":", 1)[1].strip()
                        got_otp = True
                        break
                    elif res == "STATUS_CANCEL":
                        self._handle_log("WARN", f"Activation #{aid} cancelled remotely.")
                        break

                    time.sleep(poll_interval)

                if got_otp and otp_code:
                    self._handle_log("SUCCESS", f"🎉 OTP RECEIVED: {otp_code} for +91 {nat_num}!")
                    # Auto-fill OTP in Playwright if active
                    if self.verify_jio:
                        try:
                            self.verifier.fill_otp(otp_code)
                            self._handle_log("INFO", f"🤖 Playwright: Auto-filled OTP {otp_code} into browser.")
                        except Exception:
                            pass

                    # Complete activation
                    self.client.set_status(aid, 6)
                    with self._state_lock:
                        self.latest_otp = otp_code
                        self.otps_count += 1
                        self.status_message = f"OTP Received: {otp_code}"
                    self.history.appendleft({
                        "id": aid,
                        "phone": nat_num,
                        "prefix": prefix,
                        "status": "SUCCESS",
                        "otp": otp_code,
                        "time": time.strftime("%H:%M:%S"),
                    })
                    self._save_history()
                    self._broadcast("otp_received", {
                        "otp": otp_code,
                        "phone": nat_num,
                        "activation_id": aid,
                        "prefix": prefix,
                    })
                    self._broadcast("state_changed", self.get_state())

                    # Cancel remaining unused numbers in batch
                    for rem in batch_candidates:
                        self.cleanup_manager.add(rem["act_id"], rem["national_number"], requested_at=rem["requested_at"])
                    batch_candidates = []
                    break

            with self._state_lock:
                self.active_id = None
                self.active_number = None
                self.matched_prefix = None
                self.candidate_queue = []
            self._broadcast("state_changed", self.get_state())
            time.sleep(1)

        with self._state_lock:
            self._is_running = False
            self.status_message = "Stopped"
        self._broadcast("state_changed", self.get_state())
