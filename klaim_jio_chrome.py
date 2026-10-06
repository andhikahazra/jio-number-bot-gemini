import asyncio
import sys
import time
import os
from pathlib import Path

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from server.grizzly_client import GrizzlySMSClient
from server.bot_engine import (
    TIER_1_PREFIXES, TIER_2_PREFIXES, TIER_3_PREFIXES,
    extract_national_number, find_matching_prefix
)
from server.cleanup_manager import BackgroundCleanupManager
from playwright.async_api import async_playwright

# Load API Key from .env
env_file = Path(__file__).resolve().parent / ".env"
API_KEY = ""
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("GRIZZLY_API_KEY="):
                API_KEY = line.strip().split("=", 1)[1].strip().strip('"').strip("'")

if not API_KEY:
    API_KEY = input("Masukkan API Key Grizzly SMS: ").strip()

ALL_JIO_PREFIXES = TIER_1_PREFIXES + TIER_2_PREFIXES + TIER_3_PREFIXES

async def main():
    print("\n" + "="*65)
    print("🚀 AUTOMATOR KLAIM JIO DI GOOGLE CHROME (OPSI B)")
    print("="*65)
    
    client = GrizzlySMSClient(api_key=API_KEY)
    cleanup_manager = BackgroundCleanupManager(client, cooldown_delay=120)
    cleanup_manager.start()

    bal, _ = client.get_balance()
    print(f"Saldo Grizzly SMS Anda : ${bal:.2f} USD")
    print(f"Target Prefix          : {len(ALL_JIO_PREFIXES)} Prefix Jio Resmi")
    print(f"Mode Operasi           : Visual Chrome (Jendela tetap terbuka untuk klaim)")
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
                    print("   ⏳ Stok sedang berputar (No numbers). Mencoba lagi dalam 3 detik...", end="\r")
                elif raw == "NO_BALANCE":
                    pending = len(cleanup_manager.get_pending_list())
                    print(f"   ⏳ Saldo menunggu {pending} nomor dalam antrean refund...          ", end="\r")
                else:
                    print(f"   ⏳ Respon: {raw or err}...                                          ", end="\r")
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

        # Jalankan Chrome Visual via Playwright
        aid = matched_candidate["act_id"]
        nat_num = matched_candidate["nat_num"]
        print(f"\n🖥️ Membuka Google Chrome langsung di monitor Anda...")

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(channel="chrome", headless=False, args=["--start-maximized"])
            except Exception:
                browser = await p.chromium.launch(headless=False, args=["--start-maximized"])

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                no_viewport=True
            )
            page = await context.new_page()

            print("🌐 Membuka halaman Jio Selfcare...")
            await page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1.5)

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

            # Cek apakah nomor zonk
            await asyncio.sleep(1.5)
            is_zonk = False
            error_el = page.locator("[class*='errorContainer']")
            if await error_el.count() > 0:
                txt = (await error_el.first.inner_text()).strip()
                if "non-Jio" in txt or "invalid" in txt.lower():
                    is_zonk = True

            if is_zonk:
                print(f"❌ Nomor +91 {nat_num} ditolak Jio (Non-Jio). Auto-refund...")
                cleanup_manager.add(aid, nat_num, requested_at=matched_candidate["t_req"])
                await browser.close()
                print("🔄 Mencari nomor kandidat berikutnya...")
                time.sleep(2)
                continue

            print("\n⏳ Menunggu SMS OTP dari Grizzly SMS...")
            got_otp = False
            otp_code = None

            for _ in range(30):
                res, _ = client.get_status(aid)
                if res.startswith("STATUS_OK:"):
                    otp_code = res.split(":", 1)[1].strip()
                    got_otp = True
                    break
                await asyncio.sleep(2.5)

            if got_otp and otp_code:
                print(f"\n🎉 OTP DITERIMA: {otp_code}!")
                client.set_status(aid, 6) # Selesaikan di Grizzly
                
                # Auto-fill OTP ke 6 kotak
                print(f"🤖 Mengisi OTP {otp_code} ke kotak login Jio...")
                boxes = page.locator("input[maxlength='1']")
                cnt = await boxes.count()
                if cnt >= len(otp_code):
                    for i, digit in enumerate(otp_code):
                        await boxes.nth(i).fill(digit)
                
                await asyncio.sleep(1.0)
                
                # Klik tombol submit jika ada
                submit_btn = page.locator("button:has-text('Submit'), button[type='submit']")
                if await submit_btn.count() > 0:
                    await submit_btn.first.click()

                print("\n" + "="*65)
                print("✅ LOGIN SUKSES! JENDELA CHROME DIBIARKAN TERBUKA UNTUK ANDA")
                print("="*65)
                print(f"Nomor : +91 {nat_num}")
                print(f"OTP   : {otp_code}")
                print("\n👉 Silakan langsung lanjutkan KLAIM VOUCHER / PROMO Anda")
                print("   pada jendela Google Chrome yang terbuka di layar Anda.")
                print("   Jendela ini TIDAK AKAN DITUTUP otomatis.")
                print("="*65)

                # Biarkan terbuka sampai user sendiri yang menutup
                while not page.is_closed():
                    await asyncio.sleep(2)
                break
            else:
                print("⚠️ OTP tidak kunjung tiba dalam 75 detik. Nomor diantrekan refund.")
                cleanup_manager.add(aid, nat_num, requested_at=matched_candidate["t_req"])
                await browser.close()
                print("🔄 Melanjutkan pencarian nomor baru...")
                time.sleep(2)
                continue

if __name__ == "__main__":
    asyncio.run(main())
