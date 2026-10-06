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

async def run_demo(dummy_phone: str = "9820012345"):
    print("\n" + "="*60)
    print("🚀 DEMO PLAYWRIGHT VISUAL - JIO NUMBER VERIFICATION")
    print("="*60)
    print(f"\n[Langkah 1/5] Membuka browser Chromium Playwright (Headed Mode)...")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--start-maximized"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            no_viewport=True
        )
        page = await context.new_page()

        print(f"[Langkah 2/5] Membuka halaman login Jio Selfcare...")
        t0 = time.time()
        await page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle", timeout=30000)
        print(f"             ✓ Halaman Jio Selfcare berhasil dimuat ({round(time.time() - t0, 2)}s)")
        await asyncio.sleep(1.5)

        print(f"\n[Langkah 3/5] Mengetikkan nomor dummy: +91 {dummy_phone} ke input box...")
        input_field = page.locator("#inputField")
        await input_field.click()
        await input_field.fill("")
        
        # Ketik satu per satu dengan delay agar terlihat di layar
        for digit in dummy_phone:
            await input_field.type(digit, delay=90)
        
        await asyncio.sleep(1.0)

        print(f"[Langkah 4/5] Mengklik tombol 'Generate OTP'...")
        btn = page.locator("button:has-text('Generate OTP')")
        await btn.click()

        print(f"[Langkah 5/5] Mendeteksi respon dari halaman Jio...")
        detected_error = None
        for _ in range(12):
            await asyncio.sleep(0.4)
            # Cek error container
            error_el = page.locator("[class*='errorContainer']")
            if await error_el.count() > 0:
                txt = (await error_el.first.inner_text()).strip()
                if "non-Jio" in txt or "invalid" in txt.lower():
                    detected_error = txt.replace("\n", " ")
                    break
            
            # Cek fallback text
            text_el = page.get_by_text("non-Jio number")
            if await text_el.count() > 0:
                detected_error = "It seems you have entered a non-Jio number"
                break
            
            # Cek apakah masuk screen OTP
            otp_box = page.locator("input[maxlength='1']")
            if await otp_box.count() > 0:
                break

        print("\n" + "-"*60)
        if detected_error:
            print(f"❌ HASIL: ZONK / BUKAN NOMOR JIO")
            print(f"   Pesan Jio : \"{detected_error}\"")
            print(f"   Aksi Bot  : Discard otomatis & refund saldo Grizzly SMS 100%!")
        else:
            print(f"✅ HASIL: NOMOR JIO VALID!")
            print(f"   Status    : OTP berhasil dikirim oleh Jio.")
            print(f"   Aksi Bot  : Menunggu SMS OTP dari Grizzly lalu auto-fill.")
        print("-" * 60)

        print("\nJendela browser dibiarkan terbuka selama 8 detik agar Anda bisa melihat hasilnya...")
        await asyncio.sleep(8)
        print("Demo selesai. Menutup browser Chromium.\n")
        await browser.close()

if __name__ == "__main__":
    phone = sys.argv[1] if len(sys.argv) > 1 else "9820012345"
    asyncio.run(run_demo(phone))
