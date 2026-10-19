import asyncio
import os
import sys
import subprocess
import time

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests
from pathlib import Path
from playwright.async_api import async_playwright

CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\hazra\AppData\Local"), "Google", "Chrome", "User Data", "JioBotProfile")

def is_cdp_available() -> bool:
    try:
        r = requests.get(f"{CDP_URL}/json/version", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False

def ensure_chrome_cdp():
    if is_cdp_available():
        print(f"[*] Google Chrome CDP sudah aktif dan terdeteksi di {CDP_URL}!")
        return

    print(f"[*] Menjalankan Google Chrome asli Anda dengan mode debugging port {CDP_PORT}...")
    os.makedirs(PROFILE_DIR, exist_ok=True)
    
    cmd = [
        CHROME_EXE,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized"
    ]
    # Jalankan Chrome sebagai proses terpisah (Detached) agar tidak mati saat script selesai
    DETACHED = 0x00000008
    subprocess.Popen(cmd, creationflags=DETACHED, close_fds=True)
    
    # Tunggu port terbuka
    print("[*] Menunggu Google Chrome siap...", end="", flush=True)
    for _ in range(25):
        if is_cdp_available():
            print(" Siap!\n")
            return
        print(".", end="", flush=True)
        time.sleep(0.5)
    print("\n[!] Gagal mendeteksi Chrome CDP.")

async def run_cdp_demo():
    print("\n" + "="*65)
    print("🚀 DEMO PILIHAN 1: KONTROL GOOGLE CHROME ASLI VIA CDP")
    print("   (Membuka tab baru di browser Google Chrome resmi Anda)")
    print("="*65 + "\n")

    ensure_chrome_cdp()

    print("[1] Menghubungkan Playwright ke Google Chrome yang sedang aktif...")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"[!] Gagal terhubung ke Chrome via CDP: {e}")
            return

        print("[2] Terhubung! Membuka TAB BARU di Google Chrome Anda...")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()

        print("[3] Menavigasi ke halaman Jio Selfcare login...")
        await page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(1.0)

        dummy_num = "9301267513"
        print(f"[4] Mengetikkan nomor contoh (+91 {dummy_num}) ke input box...")
        input_field = page.locator("#inputField")
        await input_field.click()
        await input_field.fill("")
        for ch in dummy_num:
            await input_field.type(ch, delay=90)

        await asyncio.sleep(1.0)

        print("\n" + "="*65)
        print("🎉 SUKSES! TAB BARU TELAH TERBUKA DI GOOGLE CHROME ANDA!")
        print("="*65)
        print("👉 Silakan lihat jendela Google Chrome Anda:")
        print("   - Tab Jio Selfcare terbuka di sana dan nomor sudah terisi.")
        print("   - Tab ini menggunakan browser Google Chrome resmi Anda.")
        print("   - Anda bisa login akun Google Anda di Chrome ini sekali saja,")
        print("     dan sesi tersebut akan tersimpan permanen tanpa terdeteksi bot!")
        print("   - Saat OTP masuk, bot akan auto-fill di tab ini,")
        print("     lalu Anda bisa langsung klik klaim promo Gemini tanpa blokir.")
        print("="*65 + "\n")

        print("Jendela/Tab ini dibiarkan aktif. Menahan koneksi selama 30 detik untuk demonstrasi...")
        for s in range(30, 0, -5):
            print(f"⏳ Tersisa {s} detik...", flush=True)
            await asyncio.sleep(5)
        print("Demo selesai. Tab tetap berada di Chrome Anda!\n")

if __name__ == "__main__":
    asyncio.run(run_cdp_demo())
