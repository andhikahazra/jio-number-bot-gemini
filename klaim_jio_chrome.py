import asyncio
import os
import sys
import time
import subprocess
from pathlib import Path

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests
from playwright.async_api import async_playwright
from server.grizzly_client import GrizzlySMSClient
from server.bot_engine import (
    TIER_1_PREFIXES, TIER_2_PREFIXES, TIER_3_PREFIXES,
    extract_national_number, find_matching_prefix
)
from server.cleanup_manager import BackgroundCleanupManager

CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\hazra\AppData\Local"), "Google", "Chrome", "User Data", "JioBotProfile")

# Load API Key from .env
env_file = Path(__file__).resolve().parent / ".env"
API_KEY = ""
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("GRIZZLY_API_KEY="):
                API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")

if not API_KEY or API_KEY == "your_api_key_here":
    API_KEY = input("Masukkan API Key Grizzly SMS Anda: ").strip()

ALL_JIO_PREFIXES = TIER_1_PREFIXES + TIER_2_PREFIXES + TIER_3_PREFIXES

def is_cdp_available() -> bool:
    try:
        r = requests.get(f"{CDP_URL}/json/version", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False

def ensure_chrome_cdp():
    if is_cdp_available():
        return

    print(f"[*] Menjalankan Google Chrome resmi dengan port debugging {CDP_PORT}...")
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
            print("[*] Google Chrome siap!\n")
            return
        time.sleep(0.5)
    print("[!] Catatan: Chrome mungkin sudah terbuka tanpa flag remote-debugging.")

async def main():
    print("\n" + "="*65)
    print("🚀 AUTOMATOR KLAIM JIO DI GOOGLE CHROME (CDP)")
    print("="*65)
    
    ensure_chrome_cdp()
    
    client = GrizzlySMSClient(api_key=API_KEY)
    cleanup_manager = BackgroundCleanupManager(client, cooldown_delay=120)
    cleanup_manager.start()

    bal, err = client.get_balance()
    if bal is None:
        print(f"❌ Gagal mengambil saldo Grizzly SMS: {err}")
        print("Pastikan API Key di .env sudah benar.")
        return

    print(f"Saldo Grizzly SMS Anda : ${bal:.2f} USD")
    print(f"Target Prefix          : {len(ALL_JIO_PREFIXES)} Prefix Jio Resmi")
    print(f"Mode Operasi           : Google Chrome Resmi (Tab Baru - Bebas Bot Block)")
    print("="*65 + "\n")

    while True:
        print("\n🔍 Sedang mencari nomor Jio di Grizzly SMS...")
        matched_candidate = None

        while not matched_candidate:
            bal, _ = client.get_balance()
            req_price = min(0.30, round(bal, 2)) if bal and bal > 0 else 0.30

            t_req = time.time()
            aid, ph, raw, err = client.request_number(service="jio", country="22", max_price=req_price)

            if not aid or not ph:
                if raw == "NO_NUMBERS":
                    print("   ⏳ Stok nomor berputar. Mencoba lagi dalam 3 detik...", end="\r")
                elif raw == "NO_BALANCE":
                    pending = len(cleanup_manager.get_pending_list())
                    print(f"   ⏳ Saldo menunggu {pending} nomor dalam antrean refund...          ", end="\r")
                else:
                    print(f"   ⏳ Respon API: {raw or err}...                                       ", end="\r")
                time.sleep(3.5)
                continue

            nat_num = extract_national_number(ph, "91")
            match_p = find_matching_prefix(nat_num, ALL_JIO_PREFIXES)

            if not match_p:
                print(f"\n❌ Discard +91 {nat_num} (Bukan prefix Jio). Antre refund 120s...")
                cleanup_manager.add(aid, nat_num, requested_at=t_req)
                time.sleep(2)
            else:
                print(f"\n🎉 NOMOR COCOK DITEMUKAN: +91 {nat_num} (Prefix: {match_p})!")
                matched_candidate = {"act_id": aid, "phone": ph, "nat_num": nat_num, "prefix": match_p, "t_req": t_req}
                break

        aid = matched_candidate["act_id"]
        nat_num = matched_candidate["nat_num"]

        print(f"\n🖥️ Membuka tab baru di Google Chrome Anda...")
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(CDP_URL)
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
            except Exception as e:
                print(f"[!] Gagal connect CDP, fallback ke launch Chrome: {e}")
                browser = await p.chromium.launch(channel="chrome", headless=False, args=["--start-maximized"])
                context = await browser.new_context()
                page = await context.new_page()

            try:
                await page.bring_to_front()
            except Exception:
                pass

            print("🌐 Membuka halaman Jio Selfcare...")
            await page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1.0)

            print(f"⌨️ Mengetik nomor +91 {nat_num} ke kolom login...")
            input_field = page.locator("#inputField")
            await input_field.click()
            await input_field.fill("")
            for ch in nat_num:
                await input_field.type(ch, delay=80)

            await asyncio.sleep(0.5)
            print("🖱️ Mengklik tombol 'Generate OTP'...")
            btn = page.locator("button:has-text('Generate OTP')")
            await btn.click()

            print("⏳ Memverifikasi ke server Jio (mendeteksi respon)...")
            is_zonk = False
            zonk_reason = ""
            otp_screen_ready = False

            # Polling aktif hingga 8 detik (langsung tembus jika respon cepat)
            for _ in range(25):
                await asyncio.sleep(0.3)

                # 1. Cek Pesan Error / Non-Jio
                error_locators = [
                    page.locator("[class*='errorContainer']"),
                    page.locator("[class*='errorText']"),
                    page.locator("[class*='errorMessage']"),
                    page.get_by_text("non-Jio", exact=False),
                    page.get_by_text("invalid", exact=False),
                    page.get_by_text("not a Jio", exact=False),
                    page.get_by_text("try again with a Jio", exact=False),
                ]

                for loc in error_locators:
                    try:
                        if await loc.count() > 0 and await loc.first.is_visible():
                            txt = (await loc.first.inner_text()).strip()
                            if any(k in txt.lower() for k in ["non-jio", "invalid", "not a jio", "try again", "enter a valid"]):
                                is_zonk = True
                                zonk_reason = txt.replace("\n", " ")
                                break
                    except Exception:
                        pass

                if is_zonk:
                    break

                # 2. Cek apakah layar kotak OTP sudah benar-benar terbuka
                otp_boxes = page.locator("input[maxlength='1']")
                try:
                    if await otp_boxes.count() >= 6 and await otp_boxes.first.is_visible():
                        otp_screen_ready = True
                        break
                except Exception:
                    pass

            if is_zonk:
                print(f"❌ REJECT: Nomor +91 {nat_num} BUKAN nomor Jio!")
                print(f"   Pesan Jio : \"{zonk_reason}\"")
                print(f"   Aksi Bot  : ⚡ Auto-Refund saldo & langsung cari nomor berikutnya!")
                cleanup_manager.add(aid, nat_num, requested_at=matched_candidate["t_req"])
                try:
                    await page.close()
                except Exception:
                    pass
                time.sleep(1.5)
                continue

            if not otp_screen_ready:
                print(f"⚠️ REJECT: Layar OTP tidak muncul dalam 8 detik untuk +91 {nat_num}.")
                print(f"   Aksi Bot  : ⚡ Dianggap nomor tidak valid / timeout. Langsung cari nomor berikutnya!")
                cleanup_manager.add(aid, nat_num, requested_at=matched_candidate["t_req"])
                try:
                    await page.close()
                except Exception:
                    pass
                time.sleep(1.5)
                continue

            print(f"✅ VALID: Nomor +91 {nat_num} terdaftar sebagai pelanggan Jio!")
            print(f"   Layar OTP Jio sudah muncul. Sekarang menunggu SMS OTP...")
            print("\n⏳ Menunggu SMS OTP dari Grizzly SMS...")
            got_otp = False
            otp_code = None

            for _ in range(35):
                res, _ = client.get_status(aid)
                if res.startswith("STATUS_OK:"):
                    otp_code = res.split(":", 1)[1].strip()
                    got_otp = True
                    break
                await asyncio.sleep(2.5)

            if got_otp and otp_code:
                print(f"\n🎉 OTP DITERIMA: {otp_code}!")
                client.set_status(aid, 6) # Selesaikan aktivasi di Grizzly

                print(f"🤖 Mengisi OTP {otp_code} ke kotak login Jio...")
                boxes = page.locator("input[maxlength='1']")
                cnt = await boxes.count()
                if cnt >= len(otp_code):
                    for i, digit in enumerate(otp_code):
                        await boxes.nth(i).fill(digit)
                else:
                    inp = page.locator("#otpInput, input[type='number'], input[type='tel']")
                    if await inp.count() > 0:
                        await inp.first.fill(otp_code)

                await asyncio.sleep(1.0)

                # Klik tombol Submit / Verify jika ada
                submit_btn = page.locator("button:has-text('Submit'), button:has-text('Proceed'), button:has-text('Verify'), button[type='submit']")
                if await submit_btn.count() > 0:
                    try:
                        await submit_btn.first.click()
                        print("🖱️ Mengklik tombol Submit login Jio...")
                    except Exception:
                        pass

                await asyncio.sleep(2.0)

                print("\n" + "="*65)
                print("✅ LOGIN SUKSES! TAB GOOGLE CHROME DIBIARKAN TERBUKA UNTUK ANDA")
                print("="*65)
                print(f"Nomor : +91 {nat_num}")
                print(f"OTP   : {otp_code}")
                print("\n👉 Silakan langsung lakukan KLAIM PROMO GEMINI di tab Chrome Anda.")
                print("   Tab ini TIDAK AKAN DITUTUP oleh bot.")
                print("="*65)

                # Tunggu sinyal user di terminal agar tidak hilang
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, input, "\n👉 Tekan [ENTER] di terminal ini jika sudah selesai klaim untuk mencari nomor berikutnya...")
                print("🔄 Melanjutkan ke nomor berikutnya...")
                # Disconnect CDP tanpa menutup browser Chrome
            else:
                print("⚠️ OTP tidak kunjung tiba dalam 85 detik. Nomor diantrekan refund.")
                cleanup_manager.add(aid, nat_num, requested_at=matched_candidate["t_req"])
                try:
                    await page.close()
                except Exception:
                    pass
                time.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
