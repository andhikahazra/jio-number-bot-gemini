import time
import threading
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass

@dataclass
class PendingCancellation:
    activation_id: str
    phone_number: str
    requested_at: float
    retry_count: int = 0
    last_attempt_at: float = 0.0

class BackgroundCleanupManager:
    """
    Manages non-blocking cancellation and refund of discarded virtual numbers.
    Grizzly SMS rejects cancellations made within 2 minutes of acquisition (EARLY_CANCEL_DENIED).
    This manager queues discarded numbers and safely cancels them in a dedicated worker thread
    as soon as their 120-second cooldown expires.
    """
    def __init__(
        self,
        client: Any,
        cooldown_delay: int = 120,
        cleanup_delay: Optional[int] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
        on_refund: Optional[Callable[[str, str], None]] = None,
        **kwargs
    ):
        self.client = client
        self.cooldown_delay = cleanup_delay if cleanup_delay is not None else cooldown_delay
        self.on_log = on_log or (lambda lvl, msg: None)
        self.on_refund = on_refund or (lambda aid, ph: None)

        self._pending: Dict[str, PendingCancellation] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

    def start(self):
        if self._worker is None or not self._worker.is_alive():
            self._stop_event.clear()
            self._worker = threading.Thread(target=self._run_loop, name="BackgroundCleanupWorker", daemon=True)
            self._worker.start()

    def stop(self):
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=3.0)

    def add(self, activation_id: str, phone_number: str, requested_at: Optional[float] = None):
        """Add an activation to the cleanup queue."""
        if not activation_id:
            return
        t_req = requested_at or time.time()
        with self._lock:
            if activation_id not in self._pending:
                self._pending[activation_id] = PendingCancellation(
                    activation_id=activation_id,
                    phone_number=phone_number,
                    requested_at=t_req,
                )
        self.on_log("CLEANUP", f"Queued #{activation_id} ({phone_number}) for auto-refund in {self.cooldown_delay}s cooldown.")

    def remove(self, activation_id: str):
        with self._lock:
            self._pending.pop(activation_id, None)

    def get_pending_list(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            result = []
            for item in self._pending.values():
                elapsed = now - item.requested_at
                remaining = max(0, int(self.cooldown_delay - elapsed))
                result.append({
                    "activation_id": item.activation_id,
                    "phone_number": item.phone_number,
                    "requested_at": item.requested_at,
                    "remaining_cooldown": remaining,
                    "ready": remaining <= 0,
                })
            return sorted(result, key=lambda x: x["remaining_cooldown"])

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def force_refund(self, activation_id: str) -> Dict[str, Any]:
        """Manually trigger immediate refund for a specific activation ID."""
        aid_str = str(activation_id)
        with self._lock:
            item = self._pending.get(aid_str)
            ph = item.phone_number if item else "Unknown"
            req_at = item.requested_at if item else time.time()

        res, err = self.client.set_status(aid_str, 8)
        if res in ("ACCESS_CANCEL", "STATUS_CANCEL", "ACCESS_ACTIVATION_CANCEL"):
            self.on_log("SUCCESS", f"✅ Manual Refund: #{aid_str} ({ph}) berhasil di-refund di Grizzly SMS! Saldo dikembalikan.")
            self.remove(aid_str)
            self.on_refund(aid_str, ph)
            return {"success": True, "message": f"Berhasil refund #{aid_str} ({ph}). Saldo dipulihkan.", "status": res}
        elif res == "EARLY_CANCEL_DENIED":
            elapsed = time.time() - req_at
            remaining = max(1, int(self.cooldown_delay - elapsed))
            self.on_log("WARN", f"⏳ Grizzly SMS menolak refund dini #{aid_str} (EARLY_CANCEL_DENIED). Sisa cooldown: {remaining}s lagi.")
            return {"success": False, "message": f"Grizzly SMS menolak refund dini. Masih dalam batas cooldown (sisa ~{remaining} detik).", "status": res, "remaining": remaining}
        elif res in ("BAD_STATUS", "NO_ACTIVATION", "STATUS_CANCEL_ALREADY"):
            self.remove(aid_str)
            self.on_log("INFO", f"ℹ️ Nomor #{aid_str} ({ph}) sudah dibatalkan sebelumnya ({res}).")
            return {"success": True, "message": f"Nomor #{aid_str} sudah dibatalkan ({res}).", "status": res}
        else:
            self.on_log("ERROR", f"❌ Gagal refund #{aid_str} ({ph}): {res or err}")
            return {"success": False, "message": f"Gagal refund #{aid_str}: {res or err}", "status": res or err}

    def force_refund_all(self) -> Dict[str, Any]:
        """Manually attempt to refund all currently pending numbers."""
        with self._lock:
            ids = list(self._pending.keys())

        if not ids:
            return {"success": True, "message": "Tidak ada nomor dalam antrean refund.", "refunded": 0, "total": 0}

        refunded = 0
        early_denied = 0
        failed = 0
        for aid in ids:
            res = self.force_refund(aid)
            if res.get("success"):
                refunded += 1
            elif res.get("status") == "EARLY_CANCEL_DENIED":
                early_denied += 1
            else:
                failed += 1

        summary = f"Refund massal: {refunded} berhasil"
        if early_denied > 0:
            summary += f", {early_denied} masih cooldown"
        if failed > 0:
            summary += f", {failed} gagal"
        self.on_log("INFO", f"📋 {summary}.")
        return {"success": True, "message": summary, "refunded": refunded, "early_denied": early_denied, "failed": failed, "total": len(ids)}

    def _run_loop(self):
        while not self._stop_event.is_set():
            now = time.time()
            to_process: List[PendingCancellation] = []

            with self._lock:
                for item in list(self._pending.values()):
                    if now - item.requested_at >= self.cooldown_delay:
                        to_process.append(item)

            for item in to_process:
                if self._stop_event.is_set():
                    break
                aid = item.activation_id
                ph = item.phone_number

                res, err = self.client.set_status(aid, 8)
                if res in ("ACCESS_CANCEL", "STATUS_CANCEL", "ACCESS_ACTIVATION_CANCEL"):
                    self.on_log("SUCCESS", f"✅ Refunded #{aid} ({ph}) on Grizzly SMS. Balance restored.")
                    self.remove(aid)
                    self.on_refund(aid, ph)
                elif res == "EARLY_CANCEL_DENIED":
                    # Extend cooldown slightly
                    item.requested_at = time.time() - (self.cooldown_delay - 15)
                    self.on_log("WARN", f"Grizzly SMS early cancel denied for #{aid}. Retrying in 15s...")
                elif res in ("BAD_STATUS", "NO_ACTIVATION", "STATUS_CANCEL_ALREADY"):
                    self.remove(aid)
                else:
                    item.retry_count += 1
                    if item.retry_count > 5:
                        self.on_log("ERROR", f"Giving up refund on #{aid} after 5 attempts. ({res})")
                        self.remove(aid)
                    else:
                        item.requested_at = time.time() - (self.cooldown_delay - 20)

            time.sleep(2.0)
