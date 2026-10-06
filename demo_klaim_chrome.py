import asyncio
import sys
import time

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.async_api import async_playwright

async def run_claim_demo():
    print("\n" + "="*60)
    print("🌐 DEMO OPSI B: KLAIM LANGSUNG DI GOOGLE CHROME PC")
    print("="*60)
    print("\n[Langkah 1] Membuka Google Chrome langsung di monitor Anda...")

    async with async_playwright() as p:
        # Coba buka Google Chrome resmi yang terinstall di Windows
        try:
            browser = await p.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--start-maximized"]
            )
        except Exception:
            # Fallback ke bundled Chromium jika channel chrome gagal
            browser = await p.chromium.launch(
                headless=False,
                args=["--start-maximized"]
            )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            no_viewport=True
        )
        page = await context.new_page()

        print("[Langkah 2] Menavigasi ke halaman login Jio Selfcare...")
        await page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(1.5)

        dummy_number = "9301267513"
        print(f"\n[Langkah 3] Mengetikkan nomor Jio: +91 {dummy_number}...")
        input_field = page.locator("#inputField")
        await input_field.click()
        await input_field.fill("")

        # Ketik satu per satu
        for digit in dummy_number:
            await input_field.type(digit, delay=120)

        await asyncio.sleep(1.0)
        print("            ✓ Nomor berhasil diketik di kolom input Jio!")

        print("\n" + "="*60)
        print("🎉 JENDELA CHROME SUDAH TERBUKA DI LAYAR MONITOR ANDA!")
        print("="*60)
        print("👉 Pada alur sebenarnya:")
        print("   1. Bot akan mencari nomor Jio ber-prefix bagus di Grizzly.")
        print("   2. Chrome akan otomatis terbuka di layar PC Anda seperti ini.")
        print("   3. Nomor diketikkan otomatis.")
        print("   4. Anda tinggal klik 'Generate OTP' dan OTP otomatis terisi.")
        print("   5. Jendela Chrome TIDAK AKAN DITUTUP agar Anda bisa langsung")
        print("      klik tombol KLAIM PROMO / VOUCHER Anda!")
        print("="*60)
        print("\nJendela ini akan tetap terbuka selama 45 detik agar Anda bisa mencoba-coba.")
        print("Silakan lihat dan klik jendela Google Chrome di monitor Anda sekarang...\n")

        # Tetap buka selama 45 detik agar user puas melihat dan berinteraksi
        for remaining in range(45, 0, -5):
            print(f"Sisa waktu demo: {remaining} detik (Chrome tetap terbuka)...")
            await asyncio.sleep(5)

        print("\nWaktu demo selesai. Menutup browser.")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_claim_demo())
