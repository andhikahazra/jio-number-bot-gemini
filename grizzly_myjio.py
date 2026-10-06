#!/usr/bin/env python3
"""
Grizzly SMS Automation Script for MyJio (Target Jio prefixes: 600, 939, 700, 701, 702)
Designed with polite rate-limiting ("do not harsh") to prevent API bans and balance wastage.
"""

import argparse
import atexit
import html
import io
import json
import os
import queue
import re
import select
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import requests

# Reconfigure stdout/stderr for UTF-8 in VPS/Docker environments
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API_BASE_URL = "https://api.grizzlysms.com/stubs/handler_api.php"

# ANSI Colors for terminal output
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

ANSI_REGEX = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def strip_ansi(text: str) -> str:
    """Remove ANSI terminal color escape sequences."""
    return ANSI_REGEX.sub('', text)


class PauseController:
    """
    Thread-safe controller to pause and resume automator execution.
    Allows user to pause anytime via Telegram (/pause or button) or terminal ('p' / 'pause')
    and resume whenever desired (/resume, /start, button, or [Enter] / 'r' in terminal).
    """
    def __init__(self):
        self._paused = threading.Event()  # cleared = running, set = paused
        self._lock = threading.Lock()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def pause(self):
        with self._lock:
            self._paused.set()

    def resume(self):
        with self._lock:
            self._paused.clear()

    def wait_if_paused(self, stop_event: Optional[threading.Event] = None) -> bool:
        """
        If paused, block until resumed or until stop_event is set.
        Returns True if stop_event was triggered, False if resumed or not paused.
        """
        if not self.is_paused():
            return False

        ev = stop_event or global_stop_event
        with state_lock:
            prev_state = current_state_info.get("state_name", "Running")
            current_state_info["state_name"] = "⏸️ PAUSED (Tap Resume or send /resume)"
            current_state_info["is_paused"] = True

        print(f"\n{BOLD}{YELLOW}=================================================={RESET}")
        print(f"{BOLD}⏸️  AUTOMATOR PAUSED{RESET}")
        if is_headless():
            print(f"👉 Automator is paused. Tap {CYAN}▶️ Resume Automator{RESET} or send {BOLD}/resume{RESET} (or {BOLD}/start{RESET}) in Telegram to start again.")
        else:
            print(f"👉 Automator is paused. Press {BOLD}[Enter]{RESET} or {BOLD}[r]{RESET} in terminal, or tap {CYAN}▶️ Resume Automator{RESET} in Telegram to resume.")
        print(f"{BOLD}{YELLOW}=================================================={RESET}\n", flush=True)

        while self.is_paused() and not ev.is_set():
            # 1. Check Telegram action queue
            if telegram_notifier and telegram_notifier.enabled:
                act = telegram_notifier.get_action(timeout=0.1)
                if act in ("resume", "start"):
                    self.resume()
                    break
                elif act in ("quit", "stop"):
                    if ev:
                        ev.set()
                    return True

            # 2. Check terminal stdin if interactive
            if not is_headless():
                user_in = check_stdin_input(timeout=0.2)
                if user_in is not None:
                    val = user_in.strip().lower()
                    if val in ("q", "quit", "exit"):
                        if ev:
                            ev.set()
                        return True
                    else:  # [Enter], 'r', 'resume', 'start', etc.
                        print(f"\n{GREEN}▶️ Resuming automator from terminal input...{RESET}", flush=True)
                        self.resume()
                        if telegram_notifier and telegram_notifier.enabled:
                            telegram_notifier.notify_resumed()
                        break
            else:
                time.sleep(0.2)

        with state_lock:
            current_state_info["state_name"] = prev_state
            current_state_info["is_paused"] = False

        if not self.is_paused():
            print(f"{GREEN}▶️ Automator resumed. Continuing search...{RESET}\n", flush=True)

        return ev.is_set()



class TelegramNotifier:
    """
    Asynchronous Telegram notification dispatcher and interactive command handler.
    Sends formatted HTML messages, numbers with inline buttons, OTP codes, and receives commands.
    Runs dedicated background threads for outgoing message delivery and incoming command polling,
    ensuring real-time responsiveness and full state synchronization.
    """
    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        all_messages: bool = False,
        silent: bool = False,
        timeout: int = 15,
        start_worker: bool = True,
        default_max_price: float = 0.3,
    ):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.all_messages = all_messages
        self.silent = silent
        self.timeout = timeout
        self.enabled = bool(self.bot_token and self.chat_id)
        self._queue: queue.Queue = queue.Queue()
        self.action_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._poller_thread: Optional[threading.Thread] = None
        self._last_sent_time = 0.0
        self._last_log_msg: Optional[str] = None
        self._repeat_count = 0
        self._lock = threading.RLock()
        self._warned_errors: Set[str] = set()
        self._last_update_id: Optional[int] = None
        self._session = requests.Session()

        # Context hooks for live interaction
        self.app_client: Optional[Any] = None
        self.cleanup_manager: Optional[Any] = None
        self.global_stop_event: Optional[threading.Event] = None
        self.get_state_fn: Optional[Callable[[], dict]] = None
        self.active_act_id_fn: Optional[Callable[[], Optional[str]]] = None
        self.get_max_price_fn: Optional[Callable[[], float]] = None
        self.set_max_price_fn: Optional[Callable[[float], None]] = None
        self.pause_controller: Optional[Any] = None
        self._max_price: float = float(default_max_price)

        if self.enabled and start_worker:
            self.start()

    def is_paused(self) -> bool:
        """Check whether the automator is currently paused."""
        if self.pause_controller:
            try:
                return bool(self.pause_controller.is_paused())
            except Exception:
                pass
        try:
            return bool(pause_controller.is_paused())
        except Exception:
            return False

    def pause(self):
        """Pause automator execution."""
        if self.pause_controller:
            try:
                self.pause_controller.pause()
                return
            except Exception:
                pass
        try:
            pause_controller.pause()
        except Exception:
            pass

    def resume(self):
        """Resume automator execution."""
        if self.pause_controller:
            try:
                self.pause_controller.resume()
                return
            except Exception:
                pass
        try:
            pause_controller.resume()
        except Exception:
            pass

    def get_max_price(self) -> float:
        """Fetch the current max price from context hook or internal state."""
        if self.get_max_price_fn:
            try:
                return float(self.get_max_price_fn())
            except Exception:
                pass
        return self._max_price

    def set_max_price(self, price: float):
        """Update max price both locally and through context hook."""
        self._max_price = round(float(price), 2)
        if self.set_max_price_fn:
            try:
                self.set_max_price_fn(self._max_price)
            except Exception:
                pass

    def set_app_context(
        self,
        client: Any,
        cleanup_manager: Any,
        global_stop_event: threading.Event,
        get_state_fn: Callable[[], dict],
        active_act_id_fn: Optional[Callable[[], Optional[str]]] = None,
        get_max_price_fn: Optional[Callable[[], float]] = None,
        set_max_price_fn: Optional[Callable[[float], None]] = None,
        pause_controller: Optional[Any] = None,
    ):
        """Bind runtime components to enable live control from Telegram."""
        self.app_client = client
        self.cleanup_manager = cleanup_manager
        self.global_stop_event = global_stop_event
        self.get_state_fn = get_state_fn
        self.active_act_id_fn = active_act_id_fn
        if get_max_price_fn:
            self.get_max_price_fn = get_max_price_fn
        if set_max_price_fn:
            self.set_max_price_fn = set_max_price_fn
        if pause_controller:
            self.pause_controller = pause_controller

    def start(self):
        """Start both the outgoing sender worker and the incoming command poller."""
        if not self.enabled:
            return
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="TgSenderThread")
            self._worker_thread.start()
        if self._poller_thread is None or not self._poller_thread.is_alive():
            self._poller_thread = threading.Thread(target=self._poller_loop, daemon=True, name="TgPollerThread")
            self._poller_thread.start()

    def _internal_warn(self, err_msg: str):
        """Print warning directly without invoking log functions to prevent recursion."""
        if err_msg not in self._warned_errors:
            self._warned_errors.add(err_msg)
            print(f"\n{YELLOW}[Telegram Warning]{RESET} {err_msg}", flush=True)

    def _send_api_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict] = None
    ) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "Telegram not enabled"

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4000],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if self.silent:
            payload["disable_notification"] = True

        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code == 200 and data.get("ok"):
                return True, "OK"
            elif resp.status_code == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 3)
                time.sleep(float(retry_after))
                resp2 = self._session.post(url, json=payload, timeout=self.timeout)
                return resp2.status_code == 200, resp2.text
            else:
                desc = data.get("description", resp.text)
                self._internal_warn(f"Failed to send to Telegram ({resp.status_code}): {desc}")
                return False, f"Telegram API Error ({resp.status_code}): {desc}"
        except Exception as e:
            self._internal_warn(f"Network error communicating with Telegram: {e}")
            return False, f"Network error sending to Telegram: {e}"

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None):
        """Acknowledge an inline keyboard callback query immediately so the button spinner stops."""
        if not self.enabled or not callback_query_id:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            self._session.post(url, json=payload, timeout=5)
        except Exception:
            pass

    def build_control_keyboard(
        self,
        include_action_buttons: bool = False,
        act_id: Optional[str] = None,
        national_num: Optional[str] = None,
        remaining: int = 0,
    ) -> dict:
        """Construct interactive inline keyboard containing quick copy, action, and control buttons."""
        keyboard = []

        if include_action_buttons and national_num:
            # Row 1: Quick Copy
            keyboard.append([
                {"text": f"📋 Copy Number ({national_num})", "copy_text": {"text": national_num}},
            ])
            # Row 2: Verification Actions
            next_label = f"⏩ Cancel / Next ({remaining} left)" if remaining > 0 else "⏩ Cancel / Next Number"
            cb_yes = f"yes:{act_id}" if act_id else "yes"
            cb_skip = f"skip:{act_id}" if act_id else "skip"
            keyboard.append([
                {"text": "✅ Working (Wait OTP)", "callback_data": cb_yes},
                {"text": next_label, "callback_data": cb_skip},
            ])

        # Control Row 1: Sweep & Status
        keyboard.append([
            {"text": "🧹 Sweep Uncancelled", "callback_data": "sweep"},
            {"text": "📊 Live Status", "callback_data": "status"},
        ])

        # Control Row 2: Max Price & Kill/Resume Cancellations
        cancel_killed = self.cleanup_manager.is_killed() if self.cleanup_manager else False
        cancel_btn_text = "▶️ Resume Cancellations" if cancel_killed else "⛔ Kill Cancellations"
        cancel_cb = "resume_cancel" if cancel_killed else "kill_cancel"
        curr_price = self.get_max_price()

        keyboard.append([
            {"text": f"💵 Max Price: ${curr_price:.2f}", "callback_data": "price_menu"},
            {"text": cancel_btn_text, "callback_data": cancel_cb},
        ])

        # Control Row 3: Pause/Resume Automator & Emergency Stop
        paused = self.is_paused()
        pause_btn_text = "▶️ Resume Automator" if paused else "⏸️ Pause Automator"
        pause_cb = "resume" if paused else "pause"
        keyboard.append([
            {"text": pause_btn_text, "callback_data": pause_cb},
            {"text": "🛑 Stop Automator", "callback_data": "quit"},
        ])

        return {"inline_keyboard": keyboard}

    def build_price_keyboard(self) -> dict:
        """Construct interactive inline keyboard for choosing max price preset."""
        curr = self.get_max_price()
        presets = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.75, 1.00]
        keyboard = []
        row = []
        for p in presets:
            label = f"✓ ${p:.2f}" if abs(p - curr) < 0.005 else f"${p:.2f}"
            row.append({"text": label, "callback_data": f"set_price:{p:.2f}"})
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([{"text": "🔙 Back to Controls", "callback_data": "menu"}])
        return {"inline_keyboard": keyboard}

    def init_updates_offset(self):
        """Advance the update offset to the newest update so older messages are ignored."""
        if not self.enabled or self._last_update_id is not None:
            return
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
            resp = self._session.get(url, params={"offset": -1, "timeout": 2}, timeout=5)
            data = resp.json()
            if data.get("ok") and data.get("result"):
                last_up = data["result"][-1]["update_id"]
                self._last_update_id = last_up + 1
            else:
                self._last_update_id = 0
        except Exception:
            self._last_update_id = 0

    def _poller_loop(self):
        """
        Continuously polls for incoming user commands and button clicks via long polling.
        Dispatches all events in real time so the CLI and Telegram remain 100% in sync.
        """
        self.init_updates_offset()
        while not self._stop_event.is_set():
            try:
                url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
                params = {
                    "offset": self._last_update_id if self._last_update_id is not None else -1,
                    "timeout": 2,
                    "limit": 20,
                    "allowed_updates": json.dumps(["message", "channel_post", "callback_query"])
                }
                resp = self._session.get(url, params=params, timeout=10)
                try:
                    data = resp.json()
                except Exception:
                    data = {}

                if not data.get("ok"):
                    time.sleep(1.0)
                    continue

                updates = data.get("result", [])
                for upd in updates:
                    upd_id = upd.get("update_id", 0)
                    if self._last_update_id is None or upd_id >= self._last_update_id:
                        self._last_update_id = upd_id + 1

                    self._handle_update(upd)

            except requests.exceptions.RequestException:
                time.sleep(2.0)
            except Exception:
                time.sleep(1.0)

    def _handle_update(self, upd: dict):
        """Process incoming callback query or message and trigger synchronous response."""
        # 1. Handle Inline Button Clicks
        if "callback_query" in upd:
            cq = upd["callback_query"]
            cb_id = cq.get("id")
            sender_id = str(cq.get("from", {}).get("id", ""))
            msg_chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))

            if sender_id != self.chat_id and msg_chat != self.chat_id:
                self.answer_callback_query(cb_id, text="Unauthorized.")
                return

            raw_data = str(cq.get("data", "")).strip().lower()
            self._dispatch_command(raw_data, cb_id=cb_id)

        # 2. Handle Text Messages (direct message, group message, or channel post)
        elif "message" in upd or "channel_post" in upd:
            msg = upd.get("message") or upd.get("channel_post", {})
            sender_id = str(msg.get("chat", {}).get("id", ""))
            if sender_id != self.chat_id:
                return

            text = str(msg.get("text", "")).strip().lower()
            if text:
                self._dispatch_command(text, cb_id=None)

    def _dispatch_command(self, raw_cmd: str, cb_id: Optional[str] = None):
        """Dispatch commands and execute actions synchronously."""
        cmd = raw_cmd.strip().lower()
        base_cmd = cmd.split(":")[0].strip()

        # A. Working / Yes
        if base_cmd in ("yes", "/yes", "y", "/working", "working"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Marked as working. Waiting for OTP...")
            self.action_queue.put("yes")
            self.send("✅ <b>Number confirmed working!</b> Waiting for OTP code...", high_priority=True)

        # B. Skip / Next / Cancel current number
        elif base_cmd in ("skip", "/skip", "s", "/next", "next", "/no", "no", "n", "/cancel", "cancel", "c"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Skipping/cancelling number...")
            self.action_queue.put("skip")
            self.send("⏩ <b>Skipping number...</b> Loading next candidate.", high_priority=True)

        # C. Sweep All Uncancelled Numbers
        elif base_cmd in ("sweep", "/sweep", "/sweep_all", "sweep_all"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Sweeping uncancelled numbers...")
            threading.Thread(target=self._exec_sweep, daemon=True, name="TgSweepWorker").start()

        # D. Kill Switch of Cancellation Process
        elif base_cmd in ("kill_cancel", "/kill_cancel", "killcancel", "/killcleanup", "/kill_cleanup"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Cancellation worker killed!")
            self._exec_kill_cancel()

        # E. Resume Cancellation Process
        elif base_cmd in ("resume_cancel", "/resume_cancel", "resumecancel", "/resumecleanup"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Cancellation worker resumed!")
            self._exec_resume_cancel()

        # F. Status
        elif base_cmd in ("status", "/status", "stat", "/stat"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Fetching status...")
            self._exec_status()

        # G. Max Price Settings Menu
        elif base_cmd in ("price_menu", "prices", "/prices", "pricing", "/pricing") or cmd in ("price", "/price", "maxprice", "/maxprice", "max_price", "/max_price"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Opening Max Price settings...")
            self._exec_price_menu()

        # H. Set Max Price via Callback Button (e.g. set_price:0.35)
        elif cmd.startswith("set_price:"):
            val_part = cmd.split("set_price:")[1].strip()
            try:
                new_p = float(val_part.replace(",", "."))
                if cb_id:
                    self.answer_callback_query(cb_id, text=f"Max price set to ${new_p:.2f}!")
                self._exec_set_price(new_p)
            except ValueError:
                if cb_id:
                    self.answer_callback_query(cb_id, text="Invalid price value.")

        # I. Set Max Price via Text Command (e.g. /price 0.35, /maxprice 0.40, price 0.25)
        elif (price_match := re.match(r'^(?:/(?:max_?price|set_?price|price)|(?:max_?price|set_?price|set\s+price|price))\s+\$?([0-9]+(?:[.,][0-9]+)?)\$?$', cmd)):
            try:
                new_p = float(price_match.group(1).replace(",", "."))
                if cb_id:
                    self.answer_callback_query(cb_id, text=f"Max price set to ${new_p:.2f}!")
                self._exec_set_price(new_p)
            except ValueError:
                if cb_id:
                    self.answer_callback_query(cb_id, text="Invalid price value.")

        # J. Catch malformed price commands (e.g. /price abc, /price -1)
        elif any(cmd.startswith(p) for p in ("/price", "/maxprice", "/max_price", "/setprice", "price ", "maxprice ", "setprice ", "set price ", "max price ")):
            if cb_id:
                self.answer_callback_query(cb_id, text="Invalid price format.")
            self.send(
                "⚠️ <b>Invalid Price format.</b>\n\n"
                "Please specify a positive dollar amount, for example:\n"
                "• <code>/price 0.35</code>\n"
                "• <code>/maxprice 0.40</code>\n\n"
                "Or choose a preset below:",
                high_priority=True,
                reply_markup=self.build_price_keyboard()
            )

        # K. Pause Automator
        elif base_cmd in ("pause", "/pause"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Pausing automator...")
            self._exec_pause()

        # L. Resume Automator
        elif base_cmd in ("resume", "/resume", "unpause", "/unpause"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Resuming automator...")
            self._exec_resume()

        # M. Start command: if paused, resumes automator; otherwise opens controls
        elif base_cmd in ("start", "/start"):
            if self.is_paused():
                if cb_id:
                    self.answer_callback_query(cb_id, text="Resuming automator...")
                self._exec_resume()
            else:
                if cb_id:
                    self.answer_callback_query(cb_id, text="Opening controls...")
                self._exec_help()

        # N. Help / Menu
        elif base_cmd in ("help", "/help", "menu", "/menu"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Opening controls...")
            self._exec_help()

        # O. Emergency Stop / Kill Automator
        elif base_cmd in ("quit", "/quit", "stop", "/stop", "q", "kill", "/kill"):
            if cb_id:
                self.answer_callback_query(cb_id, text="Stopping automator...")
            self.send(
                "🚨 <b>Emergency Stop Triggered!</b>\n\nAborting operations immediately and securing account...",
                high_priority=True
            )
            self.action_queue.put("quit")
            if self.global_stop_event:
                self.global_stop_event.set()

        else:
            if cb_id:
                self.answer_callback_query(cb_id, text=f"Unknown command: {raw_cmd}")

    def _exec_pause(self):
        """Pause the automator and halt requesting new numbers."""
        if self.is_paused():
            self.send(
                "ℹ️ <b>Automator is already paused.</b>\n\nTap <b>▶️ Resume Automator</b> or send <code>/resume</code> to start again.",
                high_priority=True,
                reply_markup=self.build_control_keyboard()
            )
            return
        self.pause()
        self.action_queue.put("pause")
        print(f"\n{YELLOW}[Telegram]{RESET} Automator paused by user.", flush=True)
        markup = self.build_control_keyboard()
        msg = (
            "⏸️ <b>Automator Paused!</b>\n\n"
            "• Number searches and requests have been paused.\n"
            "• Active number (if any) was cancelled and queued for refund.\n"
            "• Background cancellation worker remains active.\n\n"
            "<i>Tap <b>▶️ Resume Automator</b> or send <code>/resume</code> (or <code>/start</code>) whenever you want to start again.</i>"
        )
        self.send(msg, high_priority=True, reply_markup=markup)

    def _exec_resume(self):
        """Resume the automator and continue requesting numbers."""
        if not self.is_paused():
            self.send(
                "ℹ️ <b>Automator is already running.</b>",
                high_priority=True,
                reply_markup=self.build_control_keyboard()
            )
            return
        self.resume()
        self.action_queue.put("resume")
        print(f"\n{GREEN}[Telegram]{RESET} Automator resumed by user.", flush=True)
        markup = self.build_control_keyboard()
        msg = (
            "▶️ <b>Automator Resumed!</b>\n\n"
            "• Number searches and requests are active again.\n\n"
            "<i>Continuing to search for matching numbers...</i>"
        )
        self.send(msg, high_priority=True, reply_markup=markup)

    def _exec_price_menu(self):
        """Display interactive max price selection menu."""
        curr = self.get_max_price()
        markup = self.build_price_keyboard()
        msg = (
            f"💵 <b>Grizzly SMS Max Price Configuration</b>\n\n"
            f"• <b>Current Max Price:</b> <code>${curr:.2f}</code>\n"
            f"• <b>Rule:</b> Numbers priced higher than this amount will be rejected/skipped by Grizzly SMS.\n\n"
            f"<i>Tap a quick preset button below or send a custom command:</i>\n"
            f"<code>/price 0.35</code> (or <code>/maxprice 0.35</code>)"
        )
        self.send(msg, high_priority=True, reply_markup=markup)

    def _exec_set_price(self, new_price: float):
        """Update max price setting and notify user."""
        if new_price <= 0:
            self.send("⚠️ <b>Invalid Price:</b> Max price must be greater than $0.00.", high_priority=True)
            return

        old_price = self.get_max_price()
        self.set_max_price(new_price)
        print(f"\n{GREEN}[Telegram]{RESET} Max price updated: ${old_price:.2f} -> ${new_price:.2f}", flush=True)

        markup = self.build_control_keyboard()
        msg = (
            f"✅ <b>Max Price Updated!</b>\n\n"
            f"• <b>Previous Price:</b> <s>${old_price:.2f}</s>\n"
            f"• <b>New Max Price:</b> <b>${new_price:.2f}</b>\n\n"
            f"<i>All upcoming Grizzly SMS number requests will use max price ${new_price:.2f}.</i>"
        )
        self.send(msg, high_priority=True, reply_markup=markup)

    def _exec_sweep(self):
        """Execute sweep across Grizzly SMS API and cleanup queue."""
        if not self.app_client or not self.cleanup_manager:
            self.send("⚠️ <i>Sweep unavailable: Client is still initializing.</i>", high_priority=True)
            return

        self.send("🧹 <i>Sweeping all uncancelled numbers on your account...</i>", high_priority=True)
        active_id = self.active_act_id_fn() if self.active_act_id_fn else None
        report_text = sweep_all_uncancelled(
            client=self.app_client,
            cleanup_manager=self.cleanup_manager,
            preserve_act_id=active_id
        )
        markup = self.build_control_keyboard()
        self.send(report_text, high_priority=True, reply_markup=markup)

    def _exec_kill_cancel(self):
        """Activate the kill switch for the cancellation process."""
        if not self.cleanup_manager:
            self.send("⚠️ <i>Cleanup manager is not initialized yet.</i>", high_priority=True)
            return

        self.cleanup_manager.kill_cancellations()
        pending = self.cleanup_manager.pending_count()
        markup = self.build_control_keyboard()
        msg = (
            f"⛔ <b>Cancellation Process KILLED (Paused)</b>\n\n"
            f"• Auto-cancellation background worker has been halted.\n"
            f"• Discarded numbers will <b>NOT</b> be cancelled automatically.\n"
            f"• Pending in cooldown queue: <b>{pending}</b> number(s).\n\n"
            f"<i>Tap 'Resume Cancellations' below or send <code>/resume_cancel</code> to re-enable auto-refunds.</i>"
        )
        self.send(msg, high_priority=True, reply_markup=markup)

    def _exec_resume_cancel(self):
        """Resume background cancellation worker."""
        if not self.cleanup_manager:
            self.send("⚠️ <i>Cleanup manager is not initialized yet.</i>", high_priority=True)
            return

        self.cleanup_manager.resume_cancellations()
        pending = self.cleanup_manager.pending_count()
        markup = self.build_control_keyboard()
        msg = (
            f"▶️ <b>Cancellation Process Resumed</b>\n\n"
            f"• Auto-cancellation background worker is now <b>ACTIVE</b>.\n"
            f"• Pending numbers will be cancelled and refunded as cooldowns elapse.\n"
            f"• Pending in cooldown queue: <b>{pending}</b> number(s)."
        )
        self.send(msg, high_priority=True, reply_markup=markup)

    def _exec_status(self):
        """Display real-time system status and balance."""
        state = self.get_state_fn() if self.get_state_fn else {}
        bal = self.app_client.get_balance() if self.app_client else None
        bal_str = f"${bal:.2f}" if bal is not None else "Unknown"
        max_p_str = f"${self.get_max_price():.2f}"

        pending = self.cleanup_manager.pending_count() if self.cleanup_manager else 0
        is_killed = self.cleanup_manager.is_killed() if self.cleanup_manager else False
        worker_status = "⛔ KILLED (Paused)" if is_killed else f"✅ Active ({pending} pending)"
        next_cd = self.cleanup_manager.get_next_cooldown_remaining() if self.cleanup_manager else 0.0
        cd_str = f"{int(next_cd)}s" if pending > 0 and next_cd > 0 else "None"

        active_num = state.get("active_number", "None")
        active_id = state.get("active_id", "None")
        current_state = "⏸️ PAUSED (Tap Resume or send /resume)" if self.is_paused() else state.get("state_name", "Running")
        attempt_str = state.get("attempt_str", "1")
        otps_count = state.get("otps_count", 0)
        env_mode = "Docker / VPS Headless" if is_headless() else "Interactive Terminal"

        markup = self.build_control_keyboard(
            include_action_buttons=bool(active_id and active_id != "None"),
            act_id=active_id if active_id != "None" else None,
            national_num=active_num if active_num != "None" else None,
            remaining=state.get("remaining_in_queue", 0)
        )

        msg = (
            f"📊 <b>Grizzly MyJio Live Status</b>\n\n"
            f"• <b>Status:</b> {current_state}\n"
            f"• <b>Balance:</b> <b>{bal_str}</b>\n"
            f"• <b>Max Price:</b> <b>{max_p_str}</b>\n"
            f"• <b>OTPs Captured:</b> <b>{otps_count}</b>\n"
            f"• <b>Current Attempt:</b> {attempt_str}\n"
            f"• <b>Active Number:</b> <code>{active_num}</code> (ID: <code>{active_id}</code>)\n"
            f"• <b>Cancellation Worker:</b> {worker_status}\n"
            f"• <b>Next Cooldown:</b> {cd_str}\n"
            f"• <b>Environment:</b> {env_mode}\n\n"
            f"<i>Tap below to control:</i>"
        )
        self.send(msg, high_priority=True, reply_markup=markup)

    def _exec_help(self):
        """Display help and interactive control buttons."""
        markup = self.build_control_keyboard()
        msg = (
            f"🤖 <b>Grizzly MyJio Bot Controls & Commands:</b>\n\n"
            f"• <b>⏸️ Pause Automator:</b> Tap button or send <code>/pause</code> to pause number searches anytime.\n"
            f"• <b>▶️ Resume Automator:</b> Tap button or send <code>/resume</code> or <code>/start</code> to start again whenever you want.\n"
            f"• <b>💵 Max Price:</b> Tap button or send <code>/price &lt;amount&gt;</code> (e.g. <code>/price 0.35</code>) to view/change max price limit.\n"
            f"• <b>🧹 Sweep Uncancelled:</b> Tap button or send <code>/sweep</code> to find & refund all uncancelled numbers on your account.\n"
            f"• <b>⛔ Kill Cancellations:</b> Tap button or send <code>/kill_cancel</code> to halt auto-cancellations.\n"
            f"• <b>▶️ Resume Cancellations:</b> Tap button or send <code>/resume_cancel</code> to re-enable auto-cleanups.\n"
            f"• <b>📊 Live Status:</b> Tap button or send <code>/status</code> for balance, max price, OTPs count & queue status.\n"
            f"• <b>✅ Working:</b> Send <code>/yes</code> when number is entered into MyJio to wait for OTP.\n"
            f"• <b>⏩ Skip:</b> Send <code>/skip</code>, <code>/next</code>, or <code>/cancel</code> to skip to next number.\n"
            f"• <b>🛑 Emergency Stop:</b> Tap button or send <code>/stop</code> or <code>/kill</code> to stop the automator safely."
        )
        self.send(msg, high_priority=True, reply_markup=markup)

    def get_action(self, timeout: float = 0.2) -> Optional[str]:
        """Fetch the next user command action ('yes', 'skip', 'quit') from the action queue."""
        try:
            return self.action_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear_action_queue(self):
        """Clear any residual action events so new numbers start with a clean state."""
        while not self.action_queue.empty():
            try:
                self.action_queue.get_nowait()
            except queue.Empty:
                break

    def poll_command(self) -> Optional[str]:
        """Legacy polling compatibility: returns any ready action."""
        return self.get_action(timeout=0.01)

    def _worker_loop(self):
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # Respect Telegram rate limit (~0.8s between messages)
            now = time.time()
            elapsed = now - self._last_sent_time
            if elapsed < 0.8:
                time.sleep(0.8 - elapsed)

            self._send_api_message(
                item["text"],
                item.get("parse_mode", "HTML"),
                reply_markup=item.get("reply_markup")
            )
            self._last_sent_time = time.time()
            self._queue.task_done()

    def send(
        self,
        text: str,
        parse_mode: str = "HTML",
        high_priority: bool = False,
        reply_markup: Optional[dict] = None
    ):
        if not self.enabled:
            return
        if high_priority:
            self._flush_repeat_log()
        self._queue.put({
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup
        })

    def _flush_repeat_log(self):
        with self._lock:
            if self._repeat_count > 1 and self._last_log_msg:
                escaped = html.escape(self._last_log_msg)
                self._queue.put({
                    "text": f"🔄 <i>Last message repeated {self._repeat_count} times:</i>\n{escaped}",
                    "parse_mode": "HTML"
                })
            self._repeat_count = 0
            self._last_log_msg = None

    def log(self, level: str, raw_msg: str):
        if not self.enabled or not self.all_messages:
            return

        clean = strip_ansi(raw_msg).strip()
        if not clean:
            return

        # Suppress duplicate consecutive log lines to avoid chat flooding
        with self._lock:
            if clean == self._last_log_msg:
                self._repeat_count += 1
                if self._repeat_count % 10 == 0:
                    escaped = html.escape(clean)
                    self._queue.put({
                        "text": f"⏳ <i>(Repeated {self._repeat_count}x)</i> {escaped}",
                        "parse_mode": "HTML"
                    })
                return
            else:
                self._flush_repeat_log()
                self._last_log_msg = clean
                self._repeat_count = 1

        prefix_icons = {
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "WARN": "⚠️",
            "ERROR": "❌",
            "CLEANUP": "🧹",
        }
        icon = prefix_icons.get(level.upper(), "📝")
        escaped = html.escape(clean)
        self.send(f"{icon} {escaped}")

    def notify_started(
        self,
        service: str,
        country: str,
        balance: float,
        active_prefixes_count: int,
        preset_name: str,
        retry_delay: int,
        max_retries: int,
        batch_size: int = 2,
        max_price: Optional[float] = None,
    ):
        retries_str = "Unlimited (forever)" if max_retries <= 0 else str(max_retries)
        price_val = max_price if max_price is not None else self.get_max_price()
        msg = (
            f"🚀 <b>Grizzly MyJio Automator Started</b>\n\n"
            f"• <b>Service:</b> <code>{html.escape(service)}</code>\n"
            f"• <b>Country:</b> {country} (India +91)\n"
            f"• <b>Balance:</b> <b>${balance:.2f}</b>\n"
            f"• <b>Max Price:</b> <b>${price_val:.2f}</b>\n"
            f"• <b>Batch Size:</b> <b>{batch_size} requests</b> per attempt\n"
            f"• <b>Active Prefixes:</b> {active_prefixes_count} active ({html.escape(preset_name)})\n"
            f"• <b>Polite Delay:</b> {retry_delay}s\n"
            f"• <b>Max Retries:</b> {retries_str}\n\n"
            f"<i>Searching for target Jio mobile numbers...</i>"
        )
        markup = self.build_control_keyboard()
        self.send(msg, high_priority=True, reply_markup=markup)

    def notify_batch_found(self, count: int, numbers: List[str]):
        """Announce that multiple matching numbers were found in a batch and queued."""
        items_str = "\n".join(f"• <code>{n}</code>" for n in numbers)
        msg = (
            f"📥 <b>Batch Result: Found {count} Matching Numbers!</b>\n\n"
            f"All {count} numbers have been queued for testing:\n"
            f"{items_str}\n\n"
            f"<i>Presenting the first number now...</i>"
        )
        markup = self.build_control_keyboard()
        self.send(msg, high_priority=True, reply_markup=markup)

    def notify_number_found(
        self,
        phone: str,
        national_num: str,
        prefix: str,
        act_id: str,
        queue_index: int = 1,
        queue_total: int = 1,
    ):
        queue_badge = f" (Queue: {queue_index}/{queue_total})" if queue_total > 1 else ""
        queue_tip = (
            f"\n💡 <i>Tip: Another matching number is queued after this one! Tap 'Cancel / Next' to test it if this one is not eligible.</i>\n"
            if queue_total > 1 and queue_index < queue_total
            else ""
        )
        msg = (
            f"🎯 <b>Target Jio Number Found!</b>{queue_badge}\n\n"
            f"📱 <b>10-Digit Mobile:</b> <code>{national_num}</code>\n"
            f"🌐 <b>International:</b> <code>+91 {national_num}</code>\n"
            f"🏷️ <b>Prefix:</b> {prefix} | 🆔 <b>ID:</b> {act_id}\n\n"
            f"👉 <b>Action in MyJio:</b>\n"
            f"1. Connect VPN to <b>India</b>.\n"
            f"2. Paste <code>{national_num}</code> into MyJio and request OTP.\n\n"
            f"❓ <b>Options:</b>\n"
            f"• Tap <b>📋 Copy Number</b> below to copy to clipboard\n"
            f"• Tap <b>✅ Working (Wait OTP)</b> or reply <code>/yes</code>\n"
            f"• Tap <b>⏩ Cancel / Next</b> or reply <code>/skip</code>\n"
            f"• Tap <b>⏸️ Pause Automator</b> or reply <code>/pause</code>\n"
            f"• Tap <b>🧹 Sweep Uncancelled</b> to refund old numbers\n"
            f"• Tap <b>🛑 Stop Automator</b> to emergency stop"
            f"{queue_tip}"
        )
        reply_markup = self.build_control_keyboard(
            include_action_buttons=True,
            act_id=act_id,
            national_num=national_num,
            remaining=(queue_total - queue_index),
        )
        self.send(msg, high_priority=True, reply_markup=reply_markup)
        # Dedicated quick-copy message containing only the 10-digit number
        self.send(f"<code>{national_num}</code>", high_priority=True)

    def notify_otp_received(self, otp: str, phone: str, national_num: str, act_id: str):
        msg = (
            f"🎉 <b>YOUR OTP CODE IS HERE!</b> 🎉\n\n"
            f"🔑 <b>OTP Code:</b> <code>{otp}</code>\n"
            f"📱 <b>Phone:</b> <code>+91 {national_num}</code>\n"
            f"🆔 <b>Activation ID:</b> <code>{act_id}</code>\n"
            f"✅ <b>Status:</b> Completed (Status 6)\n\n"
            f"<i>Tap Copy OTP below to claim your Gemini Pro access! Automator will continue searching for numbers.</i>"
        )
        pause_btn_text = "▶️ Resume Automator" if self.is_paused() else "⏸️ Pause Automator"
        pause_cb = "resume" if self.is_paused() else "pause"
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": f"📋 Copy OTP ({otp})", "copy_text": {"text": str(otp)}},
                ],
                [
                    {"text": "🧹 Sweep Uncancelled", "callback_data": "sweep"},
                    {"text": "📊 Live Status", "callback_data": "status"},
                ],
                [
                    {"text": pause_btn_text, "callback_data": pause_cb},
                    {"text": "🛑 Stop Automator", "callback_data": "quit"},
                ]
            ]
        }
        self.send(msg, high_priority=True, reply_markup=reply_markup)
        # Dedicated quick-copy message containing only the OTP
        self.send(f"<code>{otp}</code>", high_priority=True)

    def notify_number_skipped(self, national_num: str, prefix: str, has_next: bool = False):
        if has_next:
            msg = (
                f"⏩ <b>Skipped {national_num}</b> (prefix <code>{prefix}</code>). Loading next queued number..."
            )
        else:
            msg = (
                f"⏩ <b>Skipped {national_num}</b> (prefix <code>{prefix}</code>). Requesting next batch..."
            )
        self.send(msg, high_priority=True)

    def notify_balance_waiting(self, balance: float, min_balance: float, pending_count: int):
        msg = (
            f"⏳ <b>Balance Waiting</b>\n\n"
            f"• <b>Current Balance:</b> ${balance:.2f}\n"
            f"• <b>Required Minimum:</b> ${min_balance:.2f}\n"
            f"• <b>Pending Refunds:</b> {pending_count} number(s) in cooldown\n\n"
            f"<i>Pausing search until background cleanups refund balance...</i>"
        )
        markup = self.build_control_keyboard()
        self.send(msg, high_priority=True, reply_markup=markup)

    def notify_balance_restored(self, balance: float):
        msg = (
            f"💰 <b>Balance Restored!</b>\n\n"
            f"Current balance is <b>${balance:.2f}</b>. Resuming number search!"
        )
        markup = self.build_control_keyboard()
        self.send(msg, high_priority=True, reply_markup=markup)

    def notify_paused(self, reason: str = ""):
        reason_str = f"\n<b>Reason:</b> {html.escape(reason)}" if reason else ""
        msg = (
            f"⏸️ <b>Grizzly MyJio Automator Paused</b>{reason_str}\n\n"
            f"• Number searches and requests are paused.\n"
            f"• Background cancellation worker remains active.\n\n"
            f"<i>Tap <b>▶️ Resume Automator</b> or send <code>/resume</code> (or <code>/start</code>) whenever you want to start again.</i>"
        )
        markup = self.build_control_keyboard()
        self.send(msg, high_priority=True, reply_markup=markup)

    def notify_resumed(self):
        msg = (
            f"▶️ <b>Grizzly MyJio Automator Resumed!</b>\n\n"
            f"• Number searches and requests have resumed.\n\n"
            f"<i>Searching for matching numbers...</i>"
        )
        markup = self.build_control_keyboard()
        self.send(msg, high_priority=True, reply_markup=markup)

    def notify_stopped(self, reason: str):
        msg = (
            f"🛑 <b>Grizzly MyJio Stopped</b>\n\n"
            f"<b>Reason:</b> {html.escape(reason)}"
        )
        markup = self.build_control_keyboard()
        self.send(msg, high_priority=True, reply_markup=markup)

    def test_connection(self) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "Bot token or Chat ID is missing."

        try:
            resp = requests.get(f"https://api.telegram.org/bot{self.bot_token}/getMe", timeout=self.timeout)
            data = resp.json()
            if resp.status_code != 200 or not data.get("ok"):
                return False, f"Invalid Bot Token: {data.get('description', resp.text)}"
            bot_username = data.get("result", {}).get("username", "UnknownBot")
        except Exception as e:
            return False, f"Failed to reach Telegram API: {e}"

        test_text = (
            f"🤖 <b>Telegram Notification Connected!</b>\n\n"
            f"Your Grizzly SMS MyJio automator is successfully connected via @{bot_username}.\n"
            f"You can control the automator using the buttons below."
        )
        markup = self.build_control_keyboard()
        ok, err = self._send_api_message(test_text, reply_markup=markup)
        if not ok:
            return False, f"Failed to send test message: {err}"

        return True, f"Verified as @{bot_username} and delivered test message."

    def flush_and_stop(self, wait_timeout: float = 3.0):
        if not self.enabled:
            return
        self._flush_repeat_log()
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=wait_timeout)
        if self._poller_thread and self._poller_thread.is_alive():
            self._poller_thread.join(timeout=1.0)



telegram_notifier: Optional[TelegramNotifier] = None


def log_info(msg: str):
    print(f"{CYAN}[INFO]{RESET} {msg}", flush=True)
    if telegram_notifier and telegram_notifier.all_messages:
        telegram_notifier.log("INFO", msg)


def log_success(msg: str):
    print(f"{GREEN}[SUCCESS]{RESET} {BOLD}{msg}{RESET}", flush=True)
    if telegram_notifier and telegram_notifier.all_messages:
        telegram_notifier.log("SUCCESS", msg)


def log_warn(msg: str):
    print(f"{YELLOW}[WARN]{RESET} {msg}", flush=True)
    if telegram_notifier and telegram_notifier.all_messages:
        telegram_notifier.log("WARN", msg)


def log_error(msg: str):
    print(f"{RED}[ERROR]{RESET} {msg}", flush=True)
    if telegram_notifier and telegram_notifier.all_messages:
        telegram_notifier.log("ERROR", msg)


def log_cleanup(msg: str):
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
    print(f"{GREEN}[CLEANUP]{RESET} {msg}", flush=True)
    if telegram_notifier and telegram_notifier.all_messages:
        telegram_notifier.log("CLEANUP", msg)


def load_env_file(filepath: Optional[str] = None):
    """Load simple key=value pairs from .env if present (resolved relative to script directory)."""
    if filepath is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(script_dir, ".env")
        filepath = candidate if os.path.exists(candidate) else ".env"
    if not os.path.exists(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            else:
                if " #" in val:
                    val = val.split(" #", 1)[0].strip()
                val = val.strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = val


# Load .env file at module import time so all global defaults can reference environment variables
load_env_file()



SERVICE_ALIASES = {
    "myjio": "jio",
    "jio": "jio",
    "jiomart": "aay",
    "ajio": "bgc",
}

# ---------------------------------------------------------------------------
# Jio Prefix Definitions (Department of Telecommunications / RJIL Allocations)
# ---------------------------------------------------------------------------
# Tier 1: Primary nationwide Jio-exclusive allocations (highest success rate for promos)
TIER_1_PREFIXES: List[str] = [
    "600", "939", "700", "701", "702"
]

# Tier 2: Popular & widespread Jio series (fast matching on busy days)
TIER_2_PREFIXES: List[str] = [
    # 6-series
    "620", "626", "628", "629", "630", "635", "636", "637", "638", "639",
    # 7-series (nationwide circle allocations)
    "703", "704", "705", "706", "707", "708", "709",
    "790", "797", "798", "799",
    # 93-series (Reliance Jio 4G LTE)
    "930", "931", "932", "933", "934", "935", "936", "937", "938"
]

# Tier 3: Extended Jio series (circle-specific allocations & converted blocks)
TIER_3_PREFIXES: List[str] = [
    # 92-series (Reliance CDMA to Jio LTE migrated blocks)
    "920", "921", "922", "923", "924", "925", "926", "927", "928", "929",
    # 7-series circle allocations
    "740", "741", "742",
    "750", "751", "752",
    "760", "761", "762", "763", "764", "765", "766", "767",
    "786", "787", "788", "789",
    # 8-series Jio LTE allocations
    "821", "822", "824", "827", "828", "829",
    "843", "844", "846", "847", "848", "849",
    "877", "878", "879",
    "896", "897", "898", "899"
]

PREFIX_PRESETS = {
    "tier1": TIER_1_PREFIXES,
    "primary": TIER_1_PREFIXES,
    "strict": TIER_1_PREFIXES,
    "tier2": TIER_1_PREFIXES + TIER_2_PREFIXES,
    "popular": TIER_1_PREFIXES + TIER_2_PREFIXES,
    "moderate": TIER_1_PREFIXES + TIER_2_PREFIXES,
    "tier3": TIER_1_PREFIXES + TIER_2_PREFIXES + TIER_3_PREFIXES,
    "extended": TIER_1_PREFIXES + TIER_2_PREFIXES + TIER_3_PREFIXES,
    "all": TIER_1_PREFIXES + TIER_2_PREFIXES + TIER_3_PREFIXES,
}


def resolve_target_prefixes(prefixes_arg: str, preset_arg: Optional[str] = None) -> List[str]:
    """
    Resolve prefixes from preset name or custom comma-separated list.
    Supports expanding preset keywords if passed directly in prefixes string.
    """
    if preset_arg and preset_arg.lower().strip() in PREFIX_PRESETS:
        return list(PREFIX_PRESETS[preset_arg.lower().strip()])

    result = []
    tokens = [t.strip().lower() for t in prefixes_arg.split(",") if t.strip()]
    for token in tokens:
        if token in PREFIX_PRESETS:
            result.extend(PREFIX_PRESETS[token])
        else:
            result.append(token)

    # De-duplicate while preserving order
    seen = set()
    deduped = []
    for p in result:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped or list(TIER_1_PREFIXES)


def print_prefix_catalog():
    """Print an organized list of all known Jio prefixes and presets."""
    print(f"\n{BOLD}{CYAN}=================================================={RESET}")
    print(f"{BOLD} 📋 Jio Mobile Number Prefixes Catalog 📋{RESET}")
    print(f"{BOLD}{CYAN}=================================================={RESET}")

    print(f"\n{BOLD}{GREEN}1. Tier 1 (Strict / Primary Jio Series - {len(TIER_1_PREFIXES)} prefixes):{RESET}")
    print(f"   Best for: Guaranteed Jio promo eligibility")
    print(f"   Prefixes: {CYAN}{', '.join(TIER_1_PREFIXES)}{RESET}")

    tier2_total = len(TIER_1_PREFIXES) + len(TIER_2_PREFIXES)
    print(f"\n{BOLD}{YELLOW}2. Tier 2 (Popular / Moderate Series - {tier2_total} prefixes total):{RESET}")
    print(f"   Best for: Faster number allocation on busy days")
    print(f"   Added in Tier 2:")
    print(f"     • 6-series  : 620, 626, 628, 629, 630, 635, 636, 637, 638, 639")
    print(f"     • 7-series  : 703, 704, 705, 706, 707, 708, 709, 790, 797, 798, 799")
    print(f"     • 93-series : 930, 931, 932, 933, 934, 935, 936, 937, 938")

    tier3_total = tier2_total + len(TIER_3_PREFIXES)
    print(f"\n{BOLD}{CYAN}3. Tier 3 (Extended / All Known Jio Allocations - {tier3_total} prefixes total):{RESET}")
    print(f"   Best for: Maximum number availability")
    print(f"   Added in Tier 3:")
    print(f"     • 92-series (Reliance CDMA to LTE) : 920-929 (920, 921, 922, 923, 924, 925, 926, 927, 928, 929)")
    print(f"     • 7-series circle allocations       : 740-742, 750-752, 760-767, 786-789")
    print(f"     • 8-series Jio LTE allocations      : 821, 822, 824, 827-829, 843-844, 846-849, 877-879, 896-899")

    print(f"\n{BOLD}Available CLI & Env Presets:{RESET}")
    print(f"   • {CYAN}--preset tier1{RESET} / {CYAN}--preset primary{RESET}  (Tier 1)")
    print(f"   • {CYAN}--preset tier2{RESET} / {CYAN}--preset popular{RESET}  (Tier 1 + Tier 2)")
    print(f"   • {CYAN}--preset tier3{RESET} / {CYAN}--preset all{RESET}      (Tier 1 + Tier 2 + Tier 3)")
    print(f"\n{CYAN}=================================================={RESET}\n")

def find_matching_prefix(national_number: str, prefixes: List[str]) -> Optional[str]:
    """Find matching prefix from list of prefixes, preferring the longest match."""
    for p in sorted(prefixes, key=len, reverse=True):
        if national_number.startswith(p.strip()):
            return p.strip()
    return None






class GrizzlySMSClient:
    def __init__(self, api_key: str, timeout: int = 60):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; GrizzlyMyJioClient/1.0)"
        })
        self._lock = threading.RLock()

    def _request(self, action: str, params: Optional[dict] = None) -> str:
        query = {"api_key": self.api_key, "action": action}
        if params:
            query.update(params)

        try:
            resp = self.session.get(API_BASE_URL, params=query, timeout=self.timeout)
            resp.raise_for_status()
            return resp.text.strip()
        except requests.exceptions.Timeout:
            log_error(f"Network request timed out after {self.timeout}s for action '{action}'. Grizzly SMS server is taking longer than expected to allocate a number.")
            return "ERROR_TIMEOUT"
        except requests.RequestException as e:
            log_error(f"Network request failed for action '{action}': {e}")
            return "ERROR_NETWORK"


    def get_balance(self) -> Optional[float]:
        """Fetch account balance in USD / account currency."""
        res = self._request("getBalance")
        if res.startswith("ACCESS_BALANCE:"):
            try:
                return float(res.split(":", 1)[1])
            except ValueError:
                return None
        elif res == "BAD_KEY":
            log_error("Invalid API Key! Please verify your Grizzly SMS API key.")
        else:
            log_error(f"Failed to fetch balance: {res}")
        return None

    def request_number(
        self,
        service: str = "jio",
        country: str = "22",
        max_price: Optional[float] = 0.3,
    ) -> Tuple[Optional[str], Optional[str], str]:
        """
        Request a virtual number.
        Returns (activation_id, phone_number, raw_response)
        """
        service_code = SERVICE_ALIASES.get(service.lower().strip(), service.strip())
        params = {"service": service_code, "country": country}
        if max_price is not None and max_price > 0:
            params["maxPrice"] = str(max_price)

        res = self._request("getNumber", params)
        if res.startswith("ACCESS_NUMBER:"):
            parts = res.split(":")
            if len(parts) >= 3:
                activation_id = parts[1]
                phone_number = parts[2]
                return activation_id, phone_number, res
        return None, None, res

    def set_status(self, activation_id: str, status: int) -> str:
        """
        Status codes:
        1: Number ready (SMS sent / waiting)
        3: Request another code
        6: Finish activation
        8: Cancel activation (Refund)
        """
        return self._request("setStatus", {"id": activation_id, "status": str(status)})

    def get_status(self, activation_id: str) -> str:
        """
        Check activation status:
        STATUS_WAIT_CODE: waiting for SMS
        STATUS_OK:<code>: SMS received
        STATUS_CANCEL: cancelled
        """
        return self._request("getStatus", {"id": activation_id})

    def get_active_activations(self) -> List[dict]:
        """
        Query active activations on the account.
        Returns list of dicts: [{'activation_id': str, 'phone_number': str, ...}]
        """
        res = self._request("getActiveActivations")
        if not res or res in ("NO_ACTIVATIONS", "BAD_KEY", "NO_KEY", "ERROR_NETWORK", "ERROR_TIMEOUT"):
            return []
        try:
            data = json.loads(res)
            raw_items = []
            if isinstance(data, dict):
                if data.get("status") == "success" and "activeAct" in data:
                    raw_items = data["activeAct"]
                elif "activeAct" in data:
                    raw_items = data["activeAct"]
                elif "data" in data and isinstance(data["data"], list):
                    raw_items = data["data"]
            elif isinstance(data, list):
                raw_items = data

            items = []
            for item in raw_items:
                act_id = str(item.get("activationId") or item.get("id") or "")
                phone = str(item.get("phoneNumber") or item.get("phone") or item.get("number") or "")
                if act_id:
                    items.append({
                        "activation_id": act_id,
                        "phone_number": phone,
                        "raw": item,
                    })
            return items
        except Exception:
            return []


class PendingActivation:
    def __init__(self, activation_id: str, phone_number: str, requested_at: float, cancel_delay: float = 120.0):
        self.activation_id = str(activation_id)
        self.phone_number = str(phone_number)
        self.requested_at = requested_at
        self.cancel_at = requested_at + cancel_delay
        self.attempts = 0
        self.status = "pending"


class BackgroundCleanupManager:
    """
    Manages unneeded numbers that require a 2-minute cooldown before Grizzly SMS allows cancellation.
    Runs in a background thread to cancel numbers and restore account balance while the main process
    continues searching for new numbers. Includes a kill switch to pause/halt background cancellations.
    """
    def __init__(self, client: GrizzlySMSClient, cleanup_delay: float = 122.0):
        self.client = client
        self.cleanup_delay = cleanup_delay
        self._pending: Dict[str, PendingActivation] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._refund_event = threading.Event()
        self._killed = False
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="BgCleanupWorker")
        self._worker_thread.start()

    def add(self, activation_id: str, phone_number: str, requested_at: Optional[float] = None):
        if not activation_id:
            return
        now = time.time()
        if requested_at is None:
            requested_at = now

        # If already elapsed the cleanup delay, attempt immediate cancellation
        if now - requested_at >= self.cleanup_delay:
            res = self.client.set_status(activation_id, 8)
            if res.startswith("ACCESS_CANCEL") or res in ("STATUS_CANCEL", "ACCESS_CANCEL_ALREADY"):
                log_cleanup(f"Number {phone_number} (ID: {activation_id}) cancelled & refunded immediately.")
                self._refund_event.set()
                return
            elif res != "EARLY_CANCEL_DENIED":
                log_info(f"Cancellation for ID {activation_id} result: {res}")
                return

        with self._lock:
            if activation_id not in self._pending:
                self._pending[activation_id] = PendingActivation(
                    activation_id=activation_id,
                    phone_number=phone_number,
                    requested_at=requested_at,
                    cancel_delay=self.cleanup_delay
                )
                rem = max(0, int((requested_at + self.cleanup_delay) - now))
                log_info(f"⏳ Number {phone_number} (ID: {activation_id}) queued for background cancellation in {rem}s.")

    def remove(self, activation_id: str):
        with self._lock:
            self._pending.pop(str(activation_id), None)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def get_all_pending(self) -> Dict[str, str]:
        with self._lock:
            return {item.activation_id: item.phone_number for item in self._pending.values()}

    def kill_cancellations(self, clear_pending: bool = False):
        """Kill switch: Halts the background cancellation worker."""
        with self._lock:
            self._killed = True
            if clear_pending:
                self._pending.clear()
            log_warn("[Background Cleanup] ⛔ Kill switch activated: Auto-cancellations suspended.")

    def resume_cancellations(self):
        """Resume background auto-cancellations."""
        with self._lock:
            self._killed = False
            log_success("[Background Cleanup] ▶️ Cancellation process resumed.")

    def is_killed(self) -> bool:
        with self._lock:
            return self._killed

    def get_next_cooldown_remaining(self) -> float:
        with self._lock:
            if not self._pending:
                return 0.0
            now = time.time()
            return max(0.0, min(item.cancel_at - now for item in self._pending.values()))

    def adopt_stale_activations(
        self,
        current_active_id: Optional[str] = None,
        ignore_ids: Optional[Set[str]] = None,
    ) -> int:
        """Query Grizzly SMS for active activations and queue any stale numbers not yet in queue."""
        try:
            active_list = self.client.get_active_activations()
        except Exception:
            return 0

        adopted = 0
        now = time.time()
        for act in active_list:
            act_id = act.get("activation_id")
            phone = act.get("phone_number", "Unknown")
            if not act_id or act_id == current_active_id:
                continue
            if ignore_ids and act_id in ignore_ids:
                continue

            with self._lock:
                if act_id in self._pending:
                    continue
                # For discovered stale numbers from prior runs, schedule cancel immediately
                self._pending[act_id] = PendingActivation(
                    activation_id=act_id,
                    phone_number=phone,
                    requested_at=now - self.cleanup_delay,
                    cancel_delay=0.0
                )
                adopted += 1
        return adopted

    def _worker_loop(self):
        while not self._stop_event.wait(timeout=1.0):
            if self._killed:
                continue

            now = time.time()
            to_cancel = []
            with self._lock:
                for act_id, item in list(self._pending.items()):
                    if now >= item.cancel_at and item.status != "cancelling":
                        item.status = "cancelling"
                        to_cancel.append(item)

            for item in to_cancel:
                item.attempts += 1
                try:
                    res = self.client.set_status(item.activation_id, 8)
                except Exception as e:
                    res = f"ERROR: {e}"

                if res.startswith("ACCESS_CANCEL") or res in ("STATUS_CANCEL", "ACCESS_CANCEL_ALREADY"):
                    log_cleanup(f"Number {item.phone_number} (ID: {item.activation_id}) cancelled & refunded successfully!")
                    with self._lock:
                        self._pending.pop(item.activation_id, None)
                    self._refund_event.set()
                elif res == "EARLY_CANCEL_DENIED":
                    # Still too early, retry in 5s
                    with self._lock:
                        if item.activation_id in self._pending:
                            item.status = "pending"
                            item.cancel_at = time.time() + 5.0
                elif res in ("BAD_STATUS", "NO_ACTIVATION", "WRONG_ACTIVATION_ID"):
                    # Activation already cancelled or closed on server
                    with self._lock:
                        self._pending.pop(item.activation_id, None)
                else:
                    if item.attempts >= 5:
                        log_warn(f"[Background Cleanup] Giving up on cancelling ID {item.activation_id} after {item.attempts} attempts: {res}")
                        with self._lock:
                            self._pending.pop(item.activation_id, None)
                    else:
                        with self._lock:
                            if item.activation_id in self._pending:
                                item.status = "pending"
                                item.cancel_at = time.time() + 10.0

    def cancel_all_now(self):
        """Attempt immediate cancellation on all pending numbers during shutdown."""
        with self._lock:
            items = list(self._pending.values())
        for item in items:
            try:
                res = self.client.set_status(item.activation_id, 8)
                if res.startswith("ACCESS_CANCEL") or res in ("STATUS_CANCEL", "ACCESS_CANCEL_ALREADY"):
                    log_cleanup(f"Number {item.phone_number} (ID: {item.activation_id}) cancelled & refunded.")
                elif res == "EARLY_CANCEL_DENIED":
                    log_warn(f"Number ID {item.activation_id} still in 2-min cooldown; auto-refunds on Grizzly SMS.")
                else:
                    log_info(f"Cancellation for ID {item.activation_id} result: {res}")
            except Exception:
                pass
        self._stop_event.set()


state_lock = threading.RLock()
global_stop_event = threading.Event()
pause_controller: PauseController = PauseController()
_cleanup_in_progress = False

active_activation_id: Optional[str] = None
active_activations: Dict[str, dict] = {}
client_instance: Optional[GrizzlySMSClient] = None
cleanup_manager_instance: Optional[BackgroundCleanupManager] = None

current_state_info: Dict[str, Any] = {
    "state_name": "Starting",
    "attempt_str": "0",
    "active_number": "None",
    "active_id": "None",
    "remaining_in_queue": 0,
    "otps_count": 0,
    "max_price": 0.3,
    "is_paused": False,
}


def get_current_state() -> dict:
    with state_lock:
        return dict(current_state_info)


def get_active_act_id() -> Optional[str]:
    with state_lock:
        return active_activation_id


def is_headless() -> bool:
    """Check if the process is running in a non-interactive headless environment (VPS, systemd, Docker)."""
    try:
        return not sys.stdin.isatty()
    except Exception:
        return True


def interruptible_sleep(duration: float, stop_event: Optional[threading.Event] = None, check_pause: bool = True) -> bool:
    """
    Sleep for duration seconds or until stop_event is set.
    Wakes early if automator is paused or if global_stop_event is triggered.
    Returns True if stopped early (stop_event triggered), False otherwise.
    """
    ev = stop_event or global_stop_event
    if duration <= 0:
        return ev.is_set()

    step = 0.2
    elapsed = 0.0
    while elapsed < duration:
        if ev.is_set():
            return True
        if check_pause and pause_controller and pause_controller.is_paused():
            return False
        # If terminal input is available, check for pause/quit keystroke
        if not is_headless():
            in_val = check_stdin_input(timeout=0.0)
            if in_val is not None:
                v = in_val.strip().lower()
                if v in ("p", "pause"):
                    if pause_controller:
                        pause_controller.pause()
                        if telegram_notifier and telegram_notifier.enabled:
                            telegram_notifier.notify_paused("Terminal input")
                    return False
                elif v in ("q", "quit", "exit"):
                    if ev:
                        ev.set()
                    return True
        to_wait = min(step, duration - elapsed)
        if ev.wait(timeout=to_wait):
            return True
        elapsed += to_wait

    return ev.is_set()


class PidLock:
    """Manages PID file creation and checking to ensure only one instance runs safely on VPS."""
    def __init__(self, pid_file: str):
        self.pid_file = os.path.abspath(pid_file)
        self.acquired = False

    def acquire(self) -> bool:
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file, "r") as f:
                    old_pid = int(f.read().strip())
                # Check if old_pid is alive
                os.kill(old_pid, 0)
                # If no OSError raised, process is still running!
                return False
            except (OSError, ValueError):
                # Process is dead or invalid PID file, overwrite it
                pass

        try:
            os.makedirs(os.path.dirname(self.pid_file), exist_ok=True)
            with open(self.pid_file, "w") as f:
                f.write(str(os.getpid()))
            self.acquired = True
            return True
        except Exception:
            return True

    def release(self):
        if self.acquired and os.path.exists(self.pid_file):
            try:
                with open(self.pid_file, "r") as f:
                    pid_in_file = int(f.read().strip())
                if pid_in_file == os.getpid():
                    os.remove(self.pid_file)
            except Exception:
                pass
            self.acquired = False


pid_lock_instance: Optional[PidLock] = None


def sweep_all_uncancelled(
    client: GrizzlySMSClient,
    cleanup_manager: BackgroundCleanupManager,
    preserve_act_id: Optional[str] = None,
) -> str:
    """
    Sweeps and cancels all uncancelled and stale numbers from Grizzly SMS API and cleanup manager.
    Cancels and refunds any numbers whose 2-minute cooldown has passed.
    Preserves preserve_act_id if a number is actively being tested in MyJio.
    Returns formatted HTML summary string.
    """
    log_info("🧹 [Sweep] Querying account for all uncancelled numbers...")

    # 1. Fetch remote active activations from Grizzly SMS API
    remote_acts = client.get_active_activations()

    # 2. Fetch local pending activations from cleanup manager
    pending_local = cleanup_manager.get_all_pending()

    # Combine into dict {activation_id: phone_number}
    targets: Dict[str, str] = {}
    for act in remote_acts:
        aid = str(act.get("activation_id") or "")
        ph = str(act.get("phone_number") or "Unknown")
        if aid:
            targets[aid] = ph

    for aid, ph in pending_local.items():
        if aid not in targets:
            targets[aid] = ph

    global active_activations
    with state_lock:
        for aid, info in active_activations.items():
            if aid not in targets:
                targets[aid] = info.get("phone", "Unknown")

    total_found = len(targets)
    if total_found == 0:
        bal = client.get_balance()
        bal_str = f"${bal:.2f}" if bal is not None else "Unknown"
        log_info("🧹 [Sweep] No uncancelled numbers found on account.")
        return (
            f"🧹 <b>Sweep Completed: All Clean!</b>\n\n"
            f"• No uncancelled or pending numbers found on your account.\n"
            f"• Current Balance: <b>{bal_str}</b>"
        )

    log_info(f"🧹 [Sweep] Discovered {total_found} uncancelled activation(s). Attempting cancellation...")

    cancelled: List[str] = []
    cooldown: List[str] = []
    closed: List[str] = []
    preserved: List[str] = []
    errors: List[str] = []

    for aid, ph in list(targets.items()):
        if preserve_act_id and aid == preserve_act_id:
            preserved.append(f"{ph} (ID: {aid})")
            continue

        try:
            res = client.set_status(aid, 8)
        except Exception as e:
            res = f"ERROR: {e}"

        if res.startswith("ACCESS_CANCEL") or res in ("STATUS_CANCEL", "ACCESS_CANCEL_ALREADY"):
            cancelled.append(ph)
            cleanup_manager.remove(aid)
            with state_lock:
                active_activations.pop(aid, None)
            cleanup_manager._refund_event.set()
            log_cleanup(f"Sweep cancelled & refunded: {ph} (ID: {aid})")
        elif res == "EARLY_CANCEL_DENIED":
            cooldown.append(ph)
            cleanup_manager.add(aid, ph)
        elif res in ("BAD_STATUS", "NO_ACTIVATION", "WRONG_ACTIVATION_ID"):
            closed.append(ph)
            cleanup_manager.remove(aid)
            with state_lock:
                active_activations.pop(aid, None)
        else:
            errors.append(f"{ph} (ID: {aid}): {res}")

    new_bal = client.get_balance()
    new_bal_str = f"${new_bal:.2f}" if new_bal is not None else "Unknown"

    summary_lines = [
        f"🧹 <b>Sweep Results</b>\n",
        f"• <b>Total Uncancelled Discovered:</b> {total_found}",
        f"• <b>Successfully Cancelled & Refunded:</b> <b>{len(cancelled)}</b>",
        f"• <b>In 2-Minute Cooldown:</b> <b>{len(cooldown)}</b>" + (" (will auto-refund shortly)" if len(cooldown) > 0 else ""),
    ]
    if closed:
        summary_lines.append(f"• <b>Already Closed on Server:</b> {len(closed)}")
    if preserved:
        summary_lines.append(f"• <b>Preserved Active Testing Number:</b> {len(preserved)}")
    if errors:
        summary_lines.append(f"• <b>Errors:</b> {len(errors)}")

    summary_lines.append(f"\n💰 <b>Updated Account Balance:</b> <b>{new_bal_str}</b>")

    log_success(
        f"Sweep summary: {len(cancelled)} refunded, {len(cooldown)} in cooldown. Balance: {new_bal_str}"
    )

    return "\n".join(summary_lines)


def cleanup_on_exit(signum=None, frame=None):
    """Graceful cleanup handler for SIGINT (Ctrl+C), SIGTERM, and SIGHUP."""
    global active_activation_id, active_activations, client_instance, cleanup_manager_instance
    global telegram_notifier, global_stop_event, _cleanup_in_progress, pid_lock_instance

    if _cleanup_in_progress:
        return
    _cleanup_in_progress = True
    print()
    sig_name = "SIGTERM" if signum == signal.SIGTERM else ("SIGINT" if signum == signal.SIGINT else "Stop signal")
    log_warn(f"Process received {sig_name}. Performing safe cleanup...")
    global_stop_event.set()

    with state_lock:
        to_cancel = dict(active_activations)
        if active_activation_id and active_activation_id not in to_cancel:
            to_cancel[active_activation_id] = {'phone': 'active number', 'req_time': time.time()}

    if to_cancel and client_instance:
        for act_id, info in list(to_cancel.items()):
            phone = info.get('phone', '')
            log_info(f"Cancelling active number {phone} (ID: {act_id}) to protect your balance...")
            try:
                res = client_instance.set_status(act_id, 8)
                if res.startswith("ACCESS_CANCEL") or res in ("STATUS_CANCEL", "ACCESS_CANCEL_ALREADY"):
                    log_success(f"Number {phone} (ID: {act_id}) cancelled and refunded.")
                elif res == "EARLY_CANCEL_DENIED":
                    log_warn(f"Number ID {act_id} still in 2-min cooldown; auto-refunds on Grizzly SMS.")
                    if cleanup_manager_instance:
                        cleanup_manager_instance.add(act_id, phone, requested_at=info.get('req_time', time.time()))
                else:
                    log_info(f"Cancellation response: {res}")
            except Exception:
                pass

    if cleanup_manager_instance:
        pending = cleanup_manager_instance.pending_count()
        if pending > 0:
            log_info(f"Attempting to cancel {pending} pending number(s) in cleanup queue...")
            cleanup_manager_instance.cancel_all_now()

    if telegram_notifier and telegram_notifier.enabled:
        telegram_notifier.notify_stopped(f"Process stopped cleanly via {sig_name}. Cleaned up activations.")
        telegram_notifier.flush_and_stop(wait_timeout=2.0)

    if pid_lock_instance:
        pid_lock_instance.release()

    log_info("Exiting cleanly. Bye!")
    sys.exit(0)


def handle_balance_wait(
    client: GrizzlySMSClient,
    cleanup_manager: BackgroundCleanupManager,
    min_balance: Union[float, Callable[[], float]],
    current_active_id: Optional[str] = None,
    ignore_ids: Optional[Set[str]] = None,
    poll_interval: int = 3
) -> bool:
    """
    Called when balance is depleted or exceeded.
    Discovers stale numbers and waits for background cleanups to finish and refund.
    Returns True when balance is sufficient (>= min_balance) to resume search.
    Supports dynamic min_balance (callable) so user can lower max_price via Telegram live.
    """
    global telegram_notifier, global_stop_event
    get_min_bal = min_balance if callable(min_balance) else (lambda: float(min_balance))

    stale_found = cleanup_manager.adopt_stale_activations(current_active_id=current_active_id, ignore_ids=ignore_ids)
    if stale_found > 0:
        log_info(f"🔍 Discovered {stale_found} stale uncancelled number(s) on your account. Added to cleanup queue.")

    if cleanup_manager.pending_count() == 0:
        if interruptible_sleep(2.0):
            return False
        bal = client.get_balance()
        if bal is not None and bal >= get_min_bal():
            return True
        log_error(f"Balance depleted (${bal if bal is not None else 0:.2f}) and no pending or stale numbers to refund.")
        return False

    bal_now = client.get_balance() or 0.0
    if telegram_notifier and telegram_notifier.enabled:
        telegram_notifier.notify_balance_waiting(bal_now, get_min_bal(), cleanup_manager.pending_count())

    log_warn(
        f"Balance exceeded / depleted! {cleanup_manager.pending_count()} number(s) waiting "
        f"in background for 2-minute cancellation & refund."
    )
    log_info("Pausing search and waiting for stale numbers to be cleaned up...")

    while cleanup_manager.pending_count() > 0:
        if global_stop_event.is_set():
            return False
        if not pause_controller.wait_if_paused():
            return False

        rem = cleanup_manager.get_next_cooldown_remaining()
        pending = cleanup_manager.pending_count()
        target_bal = get_min_bal()

        if cleanup_manager.is_killed():
            sys.stdout.write(
                f"\r\033[K{YELLOW}⏳ Balance depleted, but cancellation worker is KILLED! Send /resume_cancel or tap Sweep.{RESET} "
            )
        else:
            sys.stdout.write(
                f"\r\033[K{YELLOW}⏳ Waiting for cleanup/refund ({pending} pending, next cancel in {int(rem)}s)...{RESET} "
            )
        sys.stdout.flush()

        # Wait on refund event or timeout
        cleanup_manager._refund_event.wait(timeout=min(float(poll_interval), max(1.0, rem)))
        cleanup_manager._refund_event.clear()

        bal = client.get_balance()
        if bal is not None and bal >= get_min_bal():
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            log_success(f"🎉 Balance restored! Current balance: ${bal:.2f}. Resuming search...")
            if telegram_notifier and telegram_notifier.enabled:
                telegram_notifier.notify_balance_restored(bal)
            return True

    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
    bal = client.get_balance()
    if bal is not None and bal >= get_min_bal():
        log_success(f"🎉 Balance restored! Current balance: ${bal:.2f}. Resuming search...")
        if telegram_notifier and telegram_notifier.enabled:
            telegram_notifier.notify_balance_restored(bal)
        return True

    log_error(f"All pending numbers cleaned up, but balance is ${bal if bal is not None else 0:.2f} (required: ${get_min_bal():.2f}).")
    return False


def extract_national_number(phone: str, country_prefix: str = "91") -> str:
    """Strip non-digits and country dial code if present to get 10-digit national number."""
    digits = re.sub(r'\D', '', str(phone or ""))
    if digits.startswith(country_prefix) and len(digits) > len(country_prefix):
        return digits[len(country_prefix):]
    return digits


def matches_prefix(national_number: str, target_prefixes: List[str]) -> bool:
    return find_matching_prefix(national_number, target_prefixes) is not None


def check_stdin_input(timeout: float = 0.2) -> Optional[str]:
    """
    Check if Enter key or text was entered on standard input without blocking.
    Safely disabled in headless / VPS / Docker mode to prevent EOF busy loops and false skips.
    """
    if is_headless():
        return None

    try:
        import msvcrt
        start = time.time()
        buf = []
        while time.time() - start < timeout:
            if msvcrt.kbhit():
                char = msvcrt.getch()
                if char in (b"\r", b"\n"):
                    return "".join(buf).strip()
                elif char == b"\x08":  # backspace
                    if buf:
                        buf.pop()
                else:
                    try:
                        buf.append(char.decode("utf-8", errors="ignore"))
                    except Exception:
                        pass
            time.sleep(0.05)
        return None
    except ImportError:
        try:
            rlist, _, _ = select.select([sys.stdin], [], [], timeout)
            if rlist:
                line = sys.stdin.readline()
                if not line:  # EOF detected!
                    return None
                return line.strip()
            return None
        except (ValueError, io.UnsupportedOperation, OSError):
            return None


def check_stdin_enter(timeout: float = 0.2) -> bool:
    """Check if Enter key was pressed on standard input without blocking."""
    return check_stdin_input(timeout=timeout) is not None


def flush_stdin():
    """Flush any pending input in stdin so old keystrokes don't trigger actions."""
    if is_headless():
        return
    try:
        import termios
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
            return
    except Exception:
        pass

    try:
        while True:
            rlist, _, _ = select.select([sys.stdin], [], [], 0)
            if not rlist:
                break
            line = sys.stdin.readline()
            if not line:
                break
    except Exception:
        pass


def prompt_working_status(phone_number: str, prefix: str, remaining_in_queue: int = 0) -> str:
    """
    Prompt user whether the number is working in MyJio or if we should skip to next number.
    Listens to both terminal input and Telegram commands.
    """
    global telegram_notifier, global_stop_event, pause_controller
    flush_stdin()
    skip_label = f"Skip & test next queued number ({remaining_in_queue} left)" if remaining_in_queue > 0 else "Skip to next number"
    print(f"\n{BOLD}{YELLOW}--------------------------------------------------{RESET}")
    print(f"{BOLD}❓ Number Verification: {phone_number} (Prefix: {prefix}){RESET}")
    if is_headless():
        print(f"   {CYAN}Managed via Telegram ({skip_label} / Working / Pause / Quit){RESET}")
    else:
        print(f"   {BOLD}[Enter / s]{RESET} = {skip_label} {CYAN}(Default){RESET}")
        print(f"   {BOLD}[y]{RESET}       = Working in MyJio -> {GREEN}Resume waiting for OTP{RESET}")
        print(f"   {BOLD}[p]{RESET}       = Pause Automator")
        print(f"   {BOLD}[q]{RESET}       = Quit")
    print(f"{BOLD}{YELLOW}--------------------------------------------------{RESET}")
    print(f"Waiting for choice via Telegram or Terminal...")

    while not global_stop_event.is_set():
        # Check Telegram synchronized action queue
        if telegram_notifier and telegram_notifier.enabled:
            act = telegram_notifier.get_action(timeout=0.1)
            if act == "yes":
                log_info("📱 Telegram command: Yes / Working selected.")
                return "y"
            elif act in ("skip", "no", "cancel"):
                log_info("📱 Telegram command: Skip to next number selected.")
                return "s"
            elif act == "pause":
                return "pause"
            elif act in ("quit", "stop"):
                return "q"

        # Check terminal stdin only if not headless
        user_in = check_stdin_input(timeout=0.2)
        if user_in is not None:
            line = user_in.strip().lower()
            if not line or line in ("s", "skip", "n", "no", "c", "cancel"):
                return "s"
            if line in ("y", "yes", "w", "working"):
                return "y"
            if line in ("p", "pause"):
                if pause_controller:
                    pause_controller.pause()
                return "pause"
            if line in ("q", "quit"):
                return "q"
            print("Please enter 's' (skip to next number), 'y' (working), 'p' (pause), or 'q' (quit).")

        time.sleep(0.05)

    return "q"


def wait_for_otp(
    client: GrizzlySMSClient,
    activation_id: str,
    timeout_sec: int = 1200,
    poll_interval: int = 4,
    matched_prefix: str = "",
    national_num: str = "",
) -> Tuple[Optional[str], Optional[str]]:
    """
    Poll for incoming OTP code gently without aggressive hammering.
    Allows user to send Telegram commands (/yes, /skip, /next, /cancel, buttons)
    or press Enter / type cancel in terminal at any time to skip to next number.
    Waits for timeout_sec (default 1200s / 20 min) or indefinitely if timeout_sec <= 0,
    until OTP is received, user enters/cancels, or activation expires on Grizzly SMS.

    Returns:
        (otp_code, action)
        where action can be None (if OTP arrived), 'skip', 'quit', 'cancelled', or 'timeout'.
    """
    global telegram_notifier, global_stop_event, pause_controller
    if timeout_sec > 0:
        timeout_desc = f"{timeout_sec}s ({timeout_sec // 60}m)" if timeout_sec >= 60 else f"{timeout_sec}s"
    else:
        timeout_desc = "indefinite (until Enter or cancel)"

    log_info(f"Waiting for SMS OTP ({timeout_desc}, polling every {poll_interval}s)...")
    if is_headless():
        log_info(f"Tap {CYAN}Next Number / Cancel in Telegram{RESET} or send <code>/skip</code> to skip, or <code>/pause</code> to pause.")
    else:
        log_info(f"Press {BOLD}[Enter]{RESET} to skip, {BOLD}[p]{RESET} to pause, or control via Telegram.")

    start_time = time.time()
    last_poll_time = 0.0

    while (timeout_sec <= 0 or (time.time() - start_time < timeout_sec)) and not global_stop_event.is_set():
        now = time.time()
        elapsed = int(now - start_time)

        # 1. Check Telegram action queue
        if telegram_notifier and telegram_notifier.enabled:
            act = telegram_notifier.get_action(timeout=0.05)
            if act == "yes":
                log_info("📱 Telegram command received: Confirmed working! Continuing to wait for OTP...")
            elif act in ("skip", "no", "cancel"):
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                log_info(f"📱 Telegram command received: Skipping/cancelling number {national_num}.")
                return None, "skip"
            elif act == "pause":
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                log_info("📱 Telegram command received: Pause requested.")
                return None, "pause"
            elif act in ("quit", "stop"):
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                log_info("📱 Telegram command received: Stop/Quit requested.")
                return None, "quit"

        # 2. Poll Grizzly SMS API every poll_interval seconds
        if now - last_poll_time >= poll_interval:
            last_poll_time = now
            res = client.get_status(activation_id)

            if res.startswith("STATUS_OK:"):
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                otp_code = res.split(":", 1)[1]
                return otp_code, None
            elif res == "STATUS_WAIT_CODE":
                time_display = f"{elapsed}s / {timeout_sec}s" if timeout_sec > 0 else f"{elapsed}s"
                cancel_hint = "(Telegram: /skip or /pause)" if is_headless() else "([Enter]=skip, p=pause, Telegram)"
                sys.stdout.write(
                    f"\r\033[K{YELLOW}⏳ Waiting for OTP... [{time_display}] "
                    f"{CYAN}{cancel_hint}{RESET} "
                )
                sys.stdout.flush()
            elif res == "STATUS_CANCEL":
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                print()
                log_error("Activation was cancelled externally (or expired on Grizzly SMS).")
                return None, "cancelled"
            else:
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
                print()
                log_warn(f"Unexpected status response: {res}")

        # 3. Check for terminal input (only if not headless)
        user_input = check_stdin_input(timeout=0.1)
        if user_input is not None:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            val = user_input.lower()
            if val in ("q", "quit", "exit"):
                log_info("⌨️ Terminal input: Quit requested.")
                return None, "quit"
            elif val in ("p", "pause"):
                log_info("⌨️ Terminal input: Pause requested.")
                if pause_controller:
                    pause_controller.pause()
                return None, "pause"
            elif val in ("y", "yes", "w", "working"):
                log_info("⌨️ Terminal input: Confirmed working! Continuing to wait for OTP...")
                if telegram_notifier and telegram_notifier.enabled:
                    telegram_notifier.send(f"✅ <b>Number confirmed working!</b> Waiting for OTP on <code>{national_num}</code>...")
            else:
                # Enter key alone ("") or 's' / 'skip' / 'c' / 'cancel' / 'n' / 'no'
                log_info(f"⌨️ Terminal input: [Enter]/Cancel received. Skipping number {national_num}.")
                return None, "skip"

    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
    print()
    if global_stop_event.is_set():
        return None, "quit"
    log_warn(f"Timeout of {timeout_sec}s reached without receiving OTP.")
    return None, "timeout"



def handle_get_chat_id(bot_token: str):
    """Helper to detect Telegram Chat ID by listening for messages sent to the bot."""
    token = bot_token.strip()
    if not token:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        token = input("Enter your Telegram Bot Token (from @BotFather): ").strip()
    if not token:
        log_error("Bot token is required to query Chat ID!")
        sys.exit(1)

    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = resp.json()
        if not data.get("ok"):
            log_error(f"Invalid Bot Token: {data.get('description', resp.text)}")
            sys.exit(1)
        bot_username = data.get("result", {}).get("username", "UnknownBot")
    except Exception as e:
        log_error(f"Failed to connect to Telegram API: {e}")
        sys.exit(1)

    print(f"\n{BOLD}{CYAN}=================================================={RESET}")
    print(f"{BOLD} 🤖 Telegram Chat ID Detection (@{bot_username}) 🤖{RESET}")
    print(f"{BOLD}{CYAN}=================================================={RESET}")

    for attempt in range(1, 11):
        try:
            upd_resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
            upd_data = upd_resp.json()
        except Exception as e:
            log_error(f"Error checking updates: {e}")
            break

        results = []
        seen = set()
        for item in upd_data.get("result", []):
            msg = item.get("message") or item.get("channel_post") or item.get("my_chat_member", {}).get("chat")
            if not msg:
                continue
            chat = msg.get("chat") if isinstance(msg, dict) and "chat" in msg else msg
            if not chat or not isinstance(chat, dict):
                continue
            cid = str(chat.get("id"))
            if cid and cid not in seen:
                seen.add(cid)
                results.append(chat)

        if results:
            print(f"\n{BOLD}{GREEN}✅ Found Telegram Chat(s):{RESET}")
            for c in results:
                cid = c.get("id")
                ctype = c.get("type", "private")
                user = c.get("username") or c.get("first_name") or c.get("title") or "User"
                print(f" • {BOLD}Chat ID:{RESET} {CYAN}{cid}{RESET} ({ctype}) - {user}")
            latest_id = results[-1].get("id")
            print(f"\n👉 {BOLD}Add these to your .env file:{RESET}")
            print(f"TELEGRAM_BOT_TOKEN={token}")
            print(f"TELEGRAM_CHAT_ID={latest_id}")
            print(f"{CYAN}=================================================={RESET}\n")
            return

        if attempt == 1:
            print(f"{YELLOW}⏳ No messages received by @{bot_username} yet.{RESET}")
            print(f"👉 Open Telegram, search for {BOLD}@{bot_username}{RESET}, and click {BOLD}Start{RESET} (or send any message).")
            print("Listening for incoming message...")

        sys.stdout.write(f"\r\033[K{YELLOW}⏳ Waiting for message from Telegram... ({attempt * 3}s / 30s){RESET} ")
        sys.stdout.flush()
        time.sleep(3)

    sys.stdout.write("\r\033[K")
    sys.stdout.flush()
    print()
    log_warn(f"No message received after 30s. Please open Telegram, send a message to @{bot_username}, and run again with --get-chat-id.")


def main():
    global active_activation_id, client_instance, cleanup_manager_instance
    global telegram_notifier, global_stop_event, pid_lock_instance

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    def _sig_handler(signum, frame):
        cleanup_on_exit(signum, frame)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _sig_handler)

    # Auto load .env if exists
    load_env_file()

    parser = argparse.ArgumentParser(
        description="Grizzly SMS MyJio automated number finder & OTP receiver (with expanded Jio prefix support)"
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("GRIZZLY_API_KEY", ""),
        help="Grizzly SMS API Key (default: from GRIZZLY_API_KEY env)",
    )
    parser.add_argument(
        "--service",
        default=os.getenv("GRIZZLY_SERVICE", "jio"),
        help="Service code in Grizzly SMS (e.g. 'jio' for MyJio, default: 'jio')",
    )
    parser.add_argument(
        "--country",
        default=os.getenv("GRIZZLY_COUNTRY", "22"),
        help="Country code in Grizzly SMS (22 = India, default: 22)",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=float(os.getenv("GRIZZLY_MAX_PRICE", "0.3")),
        help="Max price per number in USD (default: 0.3)",
    )
    parser.add_argument(
        "--prefixes",
        default=os.getenv("TARGET_PREFIXES", "600,939,700,701,702"),
        help="Target number prefixes separated by comma or preset name ('tier1', 'popular', 'all') (default: '600,939,700,701,702')",
    )
    parser.add_argument(
        "--preset",
        choices=["tier1", "primary", "strict", "tier2", "popular", "moderate", "tier3", "extended", "all"],
        default=os.getenv("PREFIX_PRESET", None),
        help="Preset tier of Jio prefixes: tier1/strict, tier2/popular, tier3/all",
    )
    parser.add_argument(
        "--list-prefixes",
        action="store_true",
        help="Display all available Jio prefix tiers and catalog, then exit",
    )
    parser.add_argument(
        "--batch-size",
        "--requests-per-attempt",
        dest="batch_size",
        type=int,
        default=int(os.getenv("BATCH_SIZE", os.getenv("REQUESTS_PER_ATTEMPT", "2"))),
        help="Number of virtual number requests to make per attempt (default: 2)",
    )
    parser.add_argument(
        "--parallel",
        dest="parallel",
        action="store_true",
        default=os.getenv("PARALLEL_REQUESTS", "true").lower() in ("true", "1", "yes"),
        help="Initiate batch requests concurrently in parallel (default: true)",
    )
    parser.add_argument(
        "--sequential",
        dest="parallel",
        action="store_false",
        help="Initiate batch requests sequentially with a delay instead of in parallel",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=int(os.getenv("RETRY_DELAY", "4")),
        help="Polite delay between attempts in seconds (default: 4)",
    )
    parser.add_argument(
        "--cleanup-delay",
        type=int,
        default=int(os.getenv("CLEANUP_DELAY", "120")),
        help="Cooldown in seconds before cancelling discarded numbers on Grizzly SMS (default: 120 / 2 min)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=int(os.getenv("MAX_RETRIES", "0")),
        help="Max retry attempts to find matching prefix (0 for unlimited / forever, default: 0)",
    )
    parser.add_argument(
        "--sms-timeout",
        type=int,
        default=int(os.getenv("SMS_TIMEOUT", "1200")),
        help="Timeout in seconds when waiting for OTP (default: 1200 / 20 min, 0 for unlimited until Enter or cancel)",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=int(os.getenv("REQUEST_TIMEOUT", "60")),
        help="HTTP request timeout in seconds for Grizzly SMS API calls (default: 60)",
    )
    parser.add_argument(
        "--pid-file",
        default=os.getenv("PID_FILE", ".grizzly_myjio.pid"),
        help="Path to PID lock file to prevent duplicate instances (default: .grizzly_myjio.pid)",
    )
    parser.add_argument(
        "--no-pid-lock",
        action="store_true",
        help="Disable PID lock file check",
    )
    parser.add_argument(
        "--start-paused",
        action="store_true",
        default=os.getenv("START_PAUSED", "false").lower() in ("true", "1", "yes"),
        help="Start the automator in paused mode, waiting for user resume signal (via Telegram or terminal)",
    )

    parser.add_argument(
        "--telegram-bot-token",
        default=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram Bot Token from @BotFather for notifications",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=os.getenv("TELEGRAM_CHAT_ID", ""),
        help="Telegram Chat ID or Channel ID for notifications",
    )
    parser.add_argument(
        "--telegram-all-messages",
        dest="telegram_all_messages",
        action="store_true",
        default=os.getenv("TELEGRAM_ALL_MESSAGES", "false").lower() in ("true", "1", "yes"),
        help="Connect all messages and logs to Telegram (default: false, only send matched numbers, commands, and OTPs)",
    )
    parser.add_argument(
        "--telegram-events-only",
        dest="telegram_all_messages",
        action="store_false",
        help="Only send key event notifications (number found, copy button, OTP, balance) to Telegram",
    )
    parser.add_argument(
        "--telegram-silent",
        action="store_true",
        default=os.getenv("TELEGRAM_SILENT", "false").lower() in ("true", "1", "yes"),
        help="Send Telegram notifications silently without sound",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test message to Telegram to verify bot token and chat ID, then exit",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch the Web Dashboard and REST/WebSocket API server",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for Web Dashboard (default: 8000)",
    )

    args = parser.parse_args()

    if args.web:
        import uvicorn
        from server.api import app as web_app, engine as bot_engine
        if args.api_key:
            bot_engine.update_config(api_key=args.api_key)
        if args.preset:
            bot_engine.update_config(preset=args.preset)
        if args.prefixes:
            bot_engine.update_config(custom_prefixes=args.prefixes)
        if args.max_price:
            bot_engine.update_config(max_price=args.max_price)
        if args.batch_size:
            bot_engine.update_config(batch_size=args.batch_size)
        if args.retry_delay:
            bot_engine.update_config(retry_delay=args.retry_delay)

        print(f"\n{BOLD}{GREEN}=================================================={RESET}")
        print(f"{BOLD}🌐 STARTING JIO NUMBER BOT WEB DASHBOARD{RESET}")
        print(f"👉 Local URL : {CYAN}http://localhost:{args.port}{RESET}")
        print(f"👉 Network   : {CYAN}http://0.0.0.0:{args.port}{RESET}")
        print(f"{BOLD}{GREEN}=================================================={RESET}\n")
        uvicorn.run(web_app, host="0.0.0.0", port=args.port)
        sys.exit(0)

    if args.get_chat_id:
        handle_get_chat_id(args.telegram_bot_token)
        sys.exit(0)

    # Acquire PID file lock for safe VPS/Docker hosting
    if not args.no_pid_lock:
        pid_lock_instance = PidLock(args.pid_file)
        if not pid_lock_instance.acquire():
            log_error(f"Another instance of grizzly_myjio is already running (PID in {args.pid_file}). Exiting to prevent duplicate balance usage.")
            sys.exit(1)
        atexit.register(pid_lock_instance.release)

    # Initialize Telegram Notifier
    telegram_notifier = TelegramNotifier(
        bot_token=args.telegram_bot_token,
        chat_id=args.telegram_chat_id,
        all_messages=args.telegram_all_messages,
        silent=args.telegram_silent,
        timeout=15,
        start_worker=not args.test_telegram,
        default_max_price=args.max_price,
    )

    if args.test_telegram:
        if not telegram_notifier.enabled:
            log_error("Telegram is not configured! Provide --telegram-bot-token and --telegram-chat-id or set them in .env")
            sys.exit(1)
        log_info("Testing Telegram connection...")
        ok, res = telegram_notifier.test_connection()
        if ok:
            log_success(f"Telegram connected successfully! {res}")
            sys.exit(0)
        else:
            log_error(f"Telegram test failed: {res}")
            sys.exit(1)

    if args.list_prefixes:
        print_prefix_catalog()
        sys.exit(0)

    api_key = args.api_key.strip()
    if not api_key:
        api_key = input("Enter your Grizzly SMS API Key: ").strip()

    if not api_key or api_key == "your_api_key_here":
        log_error("API key is missing! Provide via --api-key or .env file.")
        sys.exit(1)

    target_prefixes = resolve_target_prefixes(args.prefixes, args.preset)
    if not target_prefixes:
        log_error("No target prefixes configured! Provide --prefixes or select a --preset.")
        sys.exit(1)
    active_prefixes = target_prefixes

    retries_display = "Unlimited (forever)" if args.max_retries <= 0 else str(args.max_retries)

    if telegram_notifier.enabled:
        tg_mode = "All Messages & Logs" if telegram_notifier.all_messages else "Events Only"
        tg_display = f"{GREEN}Connected{RESET} (Chat ID: {telegram_notifier.chat_id}, Mode: {tg_mode})"
    else:
        tg_display = f"{YELLOW}Disabled{RESET} (set TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID in .env)"

    print(f"\n{BOLD}{CYAN}=================================================={RESET}")
    print(f"{BOLD} 🚀 Grizzly SMS Automation for MyJio Promo 🚀{RESET}")
    print(f"{BOLD}{CYAN}=================================================={RESET}")
    print(f"Service         : {args.service}")
    print(f"Country ID      : {args.country} (India)")
    print(f"Max Price       : ${args.max_price:.2f}")
    print(f"Request Timeout : {args.request_timeout}s")
    sms_timeout_str = f"{args.sms_timeout}s ({args.sms_timeout // 60}m)" if args.sms_timeout > 0 else "Until Enter / Cancel (unlimited)"
    print(f"SMS OTP Wait   : {sms_timeout_str}")
    mode_str = "Parallel (simultaneous)" if args.parallel else "Sequential (with delay)"
    print(f"Batch Mode      : {args.batch_size} requests per attempt ({mode_str})")
    print(f"Background Clean: Auto (cancels & refunds after {args.cleanup_delay}s cooldown, with Kill Switch)")
    print(f"Telegram Alerts : {tg_display}")
    pause_status_str = f"{YELLOW}Paused on start (send /resume or type 'resume' to begin){RESET}" if pause_controller.is_paused() else f"{GREEN}Running{RESET}"
    print(f"Automator State : {pause_status_str}")
    if is_headless():
        print(f"Environment     : {CYAN}Docker / VPS Headless Mode{RESET} (terminal stdin disabled, managed via Telegram)")
    else:
        print(f"Environment     : {GREEN}Interactive Terminal Mode{RESET} (both terminal and Telegram commands active)")
    if args.preset:
        print(f"Prefix Preset   : {args.preset} ({len(target_prefixes)} configured)")
    else:
        print(f"Configured Pfx  : {len(target_prefixes)} prefixes")
    print(f"Active Prefixes : {len(active_prefixes)} prefixes active")
    if len(active_prefixes) <= 12:
        print(f"Active List     : {active_prefixes}")
    else:
        prefix_preview = ", ".join(active_prefixes[:10]) + f", ... (+{len(active_prefixes) - 10} more)"
        print(f"Active List     : [{prefix_preview}]")
    print(f"Polite Delay    : {args.retry_delay}s (anti-ban / not harsh)")
    print(f"Max Retries     : {retries_display}")
    print(f"{CYAN}--------------------------------------------------{RESET}\n")

    client = GrizzlySMSClient(api_key=api_key, timeout=args.request_timeout)
    client_instance = client
    cleanup_manager = BackgroundCleanupManager(client, cleanup_delay=args.cleanup_delay)
    cleanup_manager_instance = cleanup_manager

    # Wire up TelegramNotifier context hooks
    def get_max_price() -> float:
        return args.max_price

    def set_max_price(new_price: float):
        args.max_price = new_price
        with state_lock:
            current_state_info["max_price"] = new_price
        if cleanup_manager_instance:
            cleanup_manager_instance._refund_event.set()

    with state_lock:
        current_state_info["max_price"] = args.max_price

    if telegram_notifier.enabled:
        telegram_notifier.set_app_context(
            client=client,
            cleanup_manager=cleanup_manager,
            global_stop_event=global_stop_event,
            get_state_fn=get_current_state,
            active_act_id_fn=get_active_act_id,
            get_max_price_fn=get_max_price,
            set_max_price_fn=set_max_price,
            pause_controller=pause_controller,
        )

    if args.start_paused:
        pause_controller.pause()
        log_info("⏸️ Started in PAUSED mode. Waiting for user resume signal (via Telegram or terminal)...")

    # 1. Check account balance
    log_info("Checking Grizzly SMS account balance...")
    balance = client.get_balance()
    if balance is None:
        log_error("Could not retrieve account balance. Please check your API key or internet connection.")
        if telegram_notifier and telegram_notifier.enabled:
            telegram_notifier.flush_and_stop(wait_timeout=2.0)
        sys.exit(1)

    log_info(f"Current Balance: {BOLD}${balance:.2f}{RESET}")
    if balance < args.max_price:
        log_warn(f"Current balance (${balance:.2f}) is below max price (${args.max_price:.2f}).")
        log_info("Checking if there are stale uncancelled numbers on your account...")
        if not handle_balance_wait(client, cleanup_manager, get_max_price):
            log_error(f"Insufficient funds! You need at least ${args.max_price:.2f}, but current balance is ${balance:.2f}.")
            if telegram_notifier and telegram_notifier.enabled:
                telegram_notifier.flush_and_stop(wait_timeout=2.0)
            sys.exit(1)

    if telegram_notifier.enabled:
        telegram_notifier.notify_started(
            service=args.service,
            country=args.country,
            balance=balance,
            active_prefixes_count=len(active_prefixes),
            preset_name=args.preset or "custom",
            retry_delay=args.retry_delay,
            max_retries=args.max_retries,
            batch_size=args.batch_size,
            max_price=args.max_price,
        )

    # 2. Main loop: request numbers, capture OTPs, and continue continuously without shutting down
    attempt = 0
    while not global_stop_event.is_set():
        if pause_controller.wait_if_paused():
            break

        # Check balance before starting attempt
        curr_bal = client.get_balance()
        if curr_bal is not None and curr_bal < args.max_price:
            log_warn(f"Balance (${curr_bal:.2f}) is below max price (${args.max_price:.2f}).")
            with state_lock:
                current_state_info["state_name"] = "Waiting for balance refunds..."
            if not handle_balance_wait(client, cleanup_manager, get_max_price, ignore_ids=set(active_activations.keys())):
                log_error("Cannot continue due to insufficient balance.")
                break

        attempt += 1
        if args.max_retries > 0 and attempt > args.max_retries:
            log_error(f"Reached maximum retries ({args.max_retries}) without completing activation.")
            log_info("Tip: You can rerun the script or expand target prefixes.")
            if telegram_notifier.enabled:
                telegram_notifier.notify_stopped(f"Reached maximum retries ({args.max_retries}).")
                telegram_notifier.flush_and_stop(wait_timeout=2.0)
            sys.exit(1)

        attempt_str = f"Attempt {attempt}" if args.max_retries <= 0 else f"Attempt {attempt}/{args.max_retries}"
        plural_req = f"{args.batch_size} virtual numbers" if args.batch_size > 1 else "virtual number"
        log_info(f"{attempt_str}: Requesting {plural_req} for {args.service}...")

        with state_lock:
            current_state_info["attempt_str"] = attempt_str
            current_state_info["state_name"] = f"Requesting numbers ({attempt_str})..."
            current_state_info["active_number"] = "None"
            current_state_info["active_id"] = "None"
            current_state_info["remaining_in_queue"] = 0

        batch_candidates = []

        if args.parallel and args.batch_size > 1:
            log_info(f"⚡ Firing {args.batch_size} requests simultaneously in parallel...")

            def _fetch_number(idx: int):
                t_req = time.time()
                aid, rphone, rresp = client.request_number(
                    service=args.service,
                    country=args.country,
                    max_price=args.max_price,
                )
                return idx, aid, rphone, rresp, t_req

            with ThreadPoolExecutor(max_workers=args.batch_size) as executor:
                futures = [executor.submit(_fetch_number, i) for i in range(1, args.batch_size + 1)]
                for future in as_completed(futures):
                    try:
                        req_idx, act_id, raw_phone, raw_resp, req_time = future.result()
                    except Exception as e:
                        log_error(f"Error fetching number in parallel worker: {e}")
                        continue

                    req_tag = f"[{req_idx}/{args.batch_size}] "

                    if not act_id or not raw_phone:
                        if raw_resp == "NO_NUMBERS":
                            log_warn(f"{req_tag}No numbers currently available at this price.")
                        elif raw_resp == "ERROR_TIMEOUT":
                            log_warn(f"{req_tag}Server allocation took longer than {args.request_timeout}s.")
                        elif raw_resp == "NO_BALANCE":
                            log_warn(f"{req_tag}Grizzly SMS returned NO_BALANCE (balance limit reached or depleted).")
                        elif raw_resp == "BAD_ACTION":
                            log_error(f"{req_tag}Grizzly SMS returned BAD_ACTION for service '{args.service}'. Please use 'jio' or check service codes.")
                        else:
                            log_warn(f"{req_tag}Request response: {raw_resp}")
                        continue

                    national_num = extract_national_number(raw_phone, country_prefix="91")
                    matched_prefix = find_matching_prefix(national_num, active_prefixes)
                    with state_lock:
                        active_activations[act_id] = {'phone': raw_phone, 'national': national_num, 'req_time': req_time}

                    if global_stop_event.is_set() or pause_controller.is_paused():
                        log_info(f"{req_tag}Received number: {raw_phone} (ID: {act_id}) -> Queued for cleanup due to pause/stop.")
                        cleanup_manager.add(act_id, national_num, requested_at=req_time)
                        with state_lock:
                            active_activations.pop(act_id, None)
                    elif not matched_prefix:
                        log_info(f"{req_tag}Received number: {raw_phone} (National: {national_num}, ID: {act_id}) -> Does not match target prefixes.")
                        cleanup_manager.add(act_id, national_num, requested_at=req_time)
                        with state_lock:
                            active_activations.pop(act_id, None)
                    else:
                        log_success(f"{req_tag}Received number: {raw_phone} (National: {national_num}, ID: {act_id}) -> 🎉 MATCH prefix '{matched_prefix}'!")
                        batch_candidates.append({
                            'act_id': act_id,
                            'raw_phone': raw_phone,
                            'national_num': national_num,
                            'prefix': matched_prefix,
                            'req_time': req_time,
                        })

        else:
            for req_idx in range(1, args.batch_size + 1):
                if global_stop_event.is_set() or pause_controller.is_paused():
                    break

                curr_bal = client.get_balance()
                if curr_bal is not None and curr_bal < args.max_price:
                    if req_idx == 1 and not batch_candidates:
                        log_warn(f"Balance (${curr_bal:.2f}) is below max price (${args.max_price:.2f}).")
                        if not handle_balance_wait(client, cleanup_manager, get_max_price, ignore_ids=set(active_activations.keys())):
                            log_error("Cannot continue due to insufficient balance.")
                            sys.exit(1)
                    else:
                        log_warn(f"Balance (${curr_bal:.2f}) too low for request {req_idx}/{args.batch_size}. Proceeding with obtained numbers.")
                        break

                if req_idx > 1:
                    if interruptible_sleep(1.0):
                        break

                req_time = time.time()
                act_id, raw_phone, raw_resp = client.request_number(
                    service=args.service,
                    country=args.country,
                    max_price=args.max_price,
                )

                req_tag = f"[{req_idx}/{args.batch_size}] " if args.batch_size > 1 else ""

                if not act_id or not raw_phone:
                    if raw_resp == "NO_NUMBERS":
                        log_warn(f"{req_tag}No numbers currently available at this price. Waiting before retry...")
                    elif raw_resp == "ERROR_TIMEOUT":
                        log_warn(f"{req_tag}Server allocation took longer than {args.request_timeout}s.")
                    elif raw_resp == "NO_BALANCE":
                        log_warn(f"{req_tag}Grizzly SMS returned NO_BALANCE (balance limit reached or depleted).")
                        if req_idx == 1 and not batch_candidates:
                            if not handle_balance_wait(client, cleanup_manager, get_max_price, ignore_ids=set(active_activations.keys())):
                                log_error("Exiting due to insufficient balance.")
                                sys.exit(1)
                        break
                    elif raw_resp == "BAD_ACTION":
                        log_error(f"{req_tag}Grizzly SMS returned BAD_ACTION for service '{args.service}'. Please use 'jio' or check service codes.")
                        sys.exit(1)
                    else:
                        log_warn(f"{req_tag}Request response: {raw_resp}")
                    continue

                national_num = extract_national_number(raw_phone, country_prefix="91")
                matched_prefix = find_matching_prefix(national_num, active_prefixes)
                with state_lock:
                    active_activations[act_id] = {'phone': raw_phone, 'national': national_num, 'req_time': req_time}

                if not matched_prefix:
                    log_info(f"{req_tag}Received number: {raw_phone} (National: {national_num}, ID: {act_id}) -> Does not match target prefixes.")
                    cleanup_manager.add(act_id, national_num, requested_at=req_time)
                    with state_lock:
                        active_activations.pop(act_id, None)
                else:
                    log_success(f"{req_tag}Received number: {raw_phone} (National: {national_num}, ID: {act_id}) -> 🎉 MATCH prefix '{matched_prefix}'!")
                    batch_candidates.append({
                        'act_id': act_id,
                        'raw_phone': raw_phone,
                        'national_num': national_num,
                        'prefix': matched_prefix,
                        'req_time': req_time,
                    })

        if global_stop_event.is_set():
            break

        if pause_controller.wait_if_paused():
            break

        if not batch_candidates:
            if interruptible_sleep(args.retry_delay):
                break
            continue

        total_candidates = len(batch_candidates)
        if total_candidates > 1:
            log_success(f"🎉 {total_candidates} numbers matched target prefixes! Queued for testing:")
            for c_i, c_item in enumerate(batch_candidates, 1):
                log_info(f"   [{c_i}/{total_candidates}] {c_item['national_num']} (Prefix: {c_item['prefix']}, ID: {c_item['act_id']})")
            if telegram_notifier.enabled:
                telegram_notifier.notify_batch_found(
                    count=total_candidates,
                    numbers=[f"{c['national_num']} (Prefix: {c['prefix']})" for c in batch_candidates]
                )

        # Iterate through candidate queue sequentially
        for q_idx, cand in enumerate(batch_candidates, 1):
            if global_stop_event.is_set():
                break

            if pause_controller.is_paused():
                log_info("⏸️ Automator paused. Cancelling remaining candidates in queue...")
                for rem_i in range(q_idx - 1, total_candidates):
                    rem_cand = batch_candidates[rem_i]
                    cleanup_manager.add(rem_cand['act_id'], rem_cand['national_num'], requested_at=rem_cand['req_time'])
                    with state_lock:
                        active_activations.pop(rem_cand['act_id'], None)
                with state_lock:
                    active_activation_id = None
                    current_state_info["active_number"] = "None"
                    current_state_info["active_id"] = "None"
                pause_controller.wait_if_paused()
                break

            act_id = cand['act_id']
            raw_phone = cand['raw_phone']
            national_num = cand['national_num']
            matched_prefix = cand['prefix']
            req_time = cand['req_time']
            with state_lock:
                active_activation_id = act_id
            remaining_in_queue = total_candidates - q_idx

            with state_lock:
                current_state_info["state_name"] = f"Testing number +91 {national_num}"
                current_state_info["active_number"] = national_num
                current_state_info["active_id"] = act_id
                current_state_info["remaining_in_queue"] = remaining_in_queue

            if telegram_notifier.enabled:
                telegram_notifier.clear_action_queue()
                telegram_notifier.notify_number_found(
                    phone=raw_phone,
                    national_num=national_num,
                    prefix=matched_prefix,
                    act_id=act_id,
                    queue_index=q_idx,
                    queue_total=total_candidates,
                )

            # Present number to user
            queue_str = f" (Queue: {q_idx}/{total_candidates})" if total_candidates > 1 else ""
            print(f"\n{BOLD}{GREEN}=================================================={RESET}")
            print(f"{BOLD}✅ SUCCESS! USE THIS NUMBER IN MYJIO APP{queue_str}:{RESET}")
            print(f"10-Digit National  : {BOLD}{national_num}{RESET}")
            print(f"Full International : {BOLD}+91 {national_num}{RESET}")
            print(f"Matched Prefix     : {BOLD}{matched_prefix}{RESET}")
            print(f"Activation ID      : {act_id}")
            if remaining_in_queue > 0:
                print(f"Queue Status       : {BOLD}{remaining_in_queue} more matching number(s) waiting in queue{RESET}")
            print(f"{BOLD}{GREEN}=================================================={RESET}\n")

            print(f"{YELLOW}👉 Action Required:{RESET}")
            print(f"1. Make sure your VPN is connected to {BOLD}India{RESET}.")
            print(f"2. Open {BOLD}MyJio{RESET} app.")
            print(f"3. Enter the number: {BOLD}{national_num}{RESET} and request OTP.")
            print(f"4. The script will automatically capture your OTP code.")
            if is_headless():
                print(f"💡 {BOLD}Tap 'Next / Cancel' in Telegram or send /skip to test next number!{RESET}\n")
            elif remaining_in_queue > 0:
                print(f"💡 {BOLD}If not working, press [Enter] in terminal or tap 'Next / Cancel' in Telegram to test next queued number!{RESET}\n")
            else:
                print(f"💡 {BOLD}If not working, press [Enter] in terminal or tap 'Next / Cancel' in Telegram to cancel and skip!{RESET}\n")

            flush_stdin()

            # Wait for OTP or command to skip
            number_resolved = False
            while not number_resolved and not global_stop_event.is_set():
                try:
                    otp, action = wait_for_otp(
                        client,
                        act_id,
                        timeout_sec=args.sms_timeout,
                        poll_interval=4,
                        matched_prefix=matched_prefix,
                        national_num=national_num,
                    )

                    if otp:
                        print(f"\n{BOLD}{GREEN}=================================================={RESET}")
                        print(f"{BOLD}🎉 YOUR OTP CODE IS: {CYAN}{otp}{GREEN} 🎉{RESET}")
                        print(f"{BOLD}{GREEN}=================================================={RESET}\n")

                        # Complete activation on Grizzly SMS
                        client.set_status(act_id, 6)
                        with state_lock:
                            active_activations.pop(act_id, None)
                            active_activation_id = None
                            current_state_info["otps_count"] += 1
                            current_state_info["active_number"] = "None"
                            current_state_info["active_id"] = "None"

                        log_success("Activation marked as completed (Status 6). OTP received successfully!")
                        if telegram_notifier.enabled:
                            telegram_notifier.notify_otp_received(otp, raw_phone, national_num, act_id)

                        # Clean up remaining unused numbers in this candidate batch
                        for rem_i in range(q_idx, total_candidates):
                            rem_cand = batch_candidates[rem_i]
                            log_info(f"Queuing unused number {rem_cand['national_num']} (ID: {rem_cand['act_id']}) for background cleanup & refund...")
                            cleanup_manager.add(rem_cand['act_id'], rem_cand['national_num'], requested_at=rem_cand['req_time'])
                            with state_lock:
                                active_activations.pop(rem_cand['act_id'], None)

                        number_resolved = True
                        log_info("🔄 OTP captured successfully! Continuing automator to search for more numbers...")
                        # Small polite pause before starting the next cycle
                        if interruptible_sleep(args.retry_delay):
                            break
                        break

                    # User command or action received
                    if action == "quit":
                        log_info("Exiting on user quit request / Emergency Stop...")
                        for rem_i in range(q_idx - 1, total_candidates):
                            rem_cand = batch_candidates[rem_i]
                            cleanup_manager.add(rem_cand['act_id'], rem_cand['national_num'], requested_at=rem_cand['req_time'])
                            with state_lock:
                                active_activations.pop(rem_cand['act_id'], None)
                        with state_lock:
                            active_activation_id = None
                        if telegram_notifier.enabled:
                            telegram_notifier.notify_stopped("User requested quit / Emergency Stop.")
                            telegram_notifier.flush_and_stop(wait_timeout=2.0)
                        if pid_lock_instance:
                            pid_lock_instance.release()
                        sys.exit(0)

                    elif action == "pause":
                        log_info(f"⏸️ Pausing automator. Cancelling active number {national_num}...")
                        cleanup_manager.add(act_id, national_num, requested_at=req_time)
                        for rem_i in range(q_idx, total_candidates):
                            rem_cand = batch_candidates[rem_i]
                            log_info(f"Queuing unused candidate {rem_cand['national_num']} (ID: {rem_cand['act_id']}) for background cleanup & refund...")
                            cleanup_manager.add(rem_cand['act_id'], rem_cand['national_num'], requested_at=rem_cand['req_time'])
                            with state_lock:
                                active_activations.pop(rem_cand['act_id'], None)
                        with state_lock:
                            active_activations.pop(act_id, None)
                            active_activation_id = None
                            current_state_info["active_number"] = "None"
                            current_state_info["active_id"] = "None"
                        number_resolved = True
                        pause_controller.wait_if_paused()
                        break

                    elif action in ("skip", "no", "cancel", "enter"):
                        has_next = (remaining_in_queue > 0)
                        if telegram_notifier.enabled:
                            telegram_notifier.notify_number_skipped(national_num, matched_prefix, has_next=has_next)
                        cleanup_manager.add(act_id, national_num, requested_at=req_time)
                        with state_lock:
                            active_activations.pop(act_id, None)
                            active_activation_id = None
                            current_state_info["active_number"] = "None"
                            current_state_info["active_id"] = "None"
                        number_resolved = True
                        if has_next:
                            log_info(f"⏩ Skipping/cancelling number {national_num}. Loading next queued number ({q_idx + 1}/{total_candidates})...")
                        else:
                            log_info(f"⏩ Skipping/cancelling number {national_num}. Queue completed. Requesting next batch...")
                            if interruptible_sleep(args.retry_delay):
                                break
                        break

                    elif action == "timeout":
                        status = prompt_working_status(national_num, matched_prefix, remaining_in_queue=remaining_in_queue)
                        if status == "y":
                            log_info(f"Number marked as working. Resuming OTP wait for {national_num}...")
                            continue
                        elif status == "q":
                            log_info("Exiting on user quit request / Emergency Stop...")
                            for rem_i in range(q_idx - 1, total_candidates):
                                rem_cand = batch_candidates[rem_i]
                                cleanup_manager.add(rem_cand['act_id'], rem_cand['national_num'], requested_at=rem_cand['req_time'])
                                with state_lock:
                                    active_activations.pop(rem_cand['act_id'], None)
                            with state_lock:
                                active_activation_id = None
                            if telegram_notifier.enabled:
                                telegram_notifier.notify_stopped("User requested quit / Emergency Stop.")
                                telegram_notifier.flush_and_stop(wait_timeout=2.0)
                            if pid_lock_instance:
                                pid_lock_instance.release()
                            sys.exit(0)
                        elif status == "pause":
                            log_info(f"⏸️ Pausing automator. Cancelling active number {national_num}...")
                            cleanup_manager.add(act_id, national_num, requested_at=req_time)
                            for rem_i in range(q_idx, total_candidates):
                                rem_cand = batch_candidates[rem_i]
                                log_info(f"Queuing unused candidate {rem_cand['national_num']} (ID: {rem_cand['act_id']}) for background cleanup & refund...")
                                cleanup_manager.add(rem_cand['act_id'], rem_cand['national_num'], requested_at=rem_cand['req_time'])
                                with state_lock:
                                    active_activations.pop(rem_cand['act_id'], None)
                            with state_lock:
                                active_activations.pop(act_id, None)
                                active_activation_id = None
                                current_state_info["active_number"] = "None"
                                current_state_info["active_id"] = "None"
                            number_resolved = True
                            pause_controller.wait_if_paused()
                            break
                        else:  # "s" / skip
                            has_next = (remaining_in_queue > 0)
                            if telegram_notifier.enabled:
                                telegram_notifier.notify_number_skipped(national_num, matched_prefix, has_next=has_next)
                            cleanup_manager.add(act_id, national_num, requested_at=req_time)
                            with state_lock:
                                active_activations.pop(act_id, None)
                                active_activation_id = None
                                current_state_info["active_number"] = "None"
                                current_state_info["active_id"] = "None"
                            number_resolved = True
                            if has_next:
                                log_info(f"⏩ Skipping number {national_num}. Loading next queued number ({q_idx + 1}/{total_candidates})...")
                            else:
                                log_info(f"⏩ Skipping number {national_num}. Queue completed. Requesting next batch...")
                                if interruptible_sleep(args.retry_delay):
                                    break
                            break

                    elif action == "cancelled":
                        cleanup_manager.add(act_id, national_num, requested_at=req_time)
                        with state_lock:
                            active_activations.pop(act_id, None)
                            active_activation_id = None
                            current_state_info["active_number"] = "None"
                            current_state_info["active_id"] = "None"
                        number_resolved = True
                        if remaining_in_queue > 0:
                            log_info(f"Number cancelled externally. Loading next queued number ({q_idx + 1}/{total_candidates})...")
                        else:
                            if interruptible_sleep(args.retry_delay):
                                break
                        break
                except KeyboardInterrupt:
                    cleanup_on_exit(None, None)

            # If an OTP was captured, break out to start a fresh batch
            if number_resolved and otp:
                break

    if telegram_notifier and telegram_notifier.enabled:
        telegram_notifier.notify_stopped("Automator stopped.")
        telegram_notifier.flush_and_stop(wait_timeout=2.0)

    if pid_lock_instance:
        pid_lock_instance.release()


if __name__ == "__main__":
    main()

