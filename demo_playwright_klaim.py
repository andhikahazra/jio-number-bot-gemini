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

async def run_full_demo(phone_number: str = "9820012345"):
    print("\n" + "="*65)
    print("🚀 DEMO LENGKAP PLAYWRIGHT - ALUR KLAIM JIO DENGAN AUTO-HOLD")
    print("="*65)
    print("Demo ini mendemonstrasikan:")
    print(" 1. Membuka browser Playwright visual (Headed Mode) di monitor Anda")
    print(" 2. Mengetikkan nomor Jio yang valid")
    print(" 3. Men-generate OTP")
    print(" 4. Mengisi 6 digit OTP ke dalam kolom")
    print(" 5. Mengklik tombol Submit / Verify")
    print(" 6. AUTO-HOLD: Browser TIDAK DITUTUP agar Anda bisa klaim promo!")
    print("="*65 + "\n")

    async with async_playwright() as p:
        print("[Langkah 1/6] Membuka browser Chrome visual di monitor Anda...")
        try:
            # Gunakan Chrome resmi dengan bypass flag otomasi
            browser = await p.chromium.launch(
                channel="chrome",
                headless=False,
                args=[
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
        except Exception:
            # Fallback ke Chromium default
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled"
                ]
            )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            no_viewport=True
        )
        page = await context.new_page()

        print("[Langkah 2/6] Membuka halaman login Jio Selfcare...")
        t0 = time.time()
        await page.goto("https://www.jio.com/selfcare/login/", wait_until="networkidle", timeout=30000)
        print(f"             ✓ Berhasil dimuat ({round(time.time() - t0, 2)} detik)")
        await asyncio.sleep(1.0)

        print(f"\n[Langkah 3/6] Mengetikkan nomor Jio valid: +91 {phone_number}...")
        input_field = page.locator("#inputField")
        await input_field.click()
        await input_field.fill("")
        for ch in phone_number:
            await input_field.type(ch, delay=80)
        await asyncio.sleep(0.5)

        print("[Langkah 4/6] Mengklik tombol 'Generate OTP'...")
        btn = page.locator("button:has-text('Generate OTP')")
        await btn.click()

        print("             ⏳ Menunggu kotak 6-digit OTP muncul di layar...")
        otp_boxes = page.locator("input[maxlength='1']")
        
        # Tunggu sampai kotak OTP muncul
        found_otp_screen = False
        for _ in range(20):
            await asyncio.sleep(0.5)
            cnt = await otp_boxes.count()
            if cnt >= 6:
                found_otp_screen = True
                break

        if not found_otp_screen:
            # Cek jika ada error non-Jio atau limit
            is_zonk = False
            zonk_msg = ""
            error_locators = [
                page.locator("[class*='errorContainer']"),
                page.locator("[class*='errorText']"),
                page.get_by_text("non-Jio", exact=False),
                page.get_by_text("invalid", exact=False)
            ]
            for loc in error_locators:
                if await loc.count() > 0 and await loc.first.is_visible():
                    is_zonk = True
                    zonk_msg = (await loc.first.inner_text()).strip().replace("\n", " ")
                    break

            print("\n" + "="*65)
            print("❌ REJECT: NOMOR DITOLAK OLEH JIO (NON-JIO)!")
            print("="*65)
            print(f"👉 Pesan dari Jio : \"{zonk_msg or 'Layar OTP tidak muncul'}\"")
            print("👉 Aksi Bot Asli  : ⚡ LANGSUNG STOP & AUTO-REFUND 100%!")
            print("                   Bot TIDAK AKAN PERNAH lanjut mengisi OTP.")
            print("                   Tab langsung ditutup dan mencari nomor berikutnya.")
            print("="*65 + "\n")
            await asyncio.sleep(4)
            await browser.close()
            return

        print("             ✓ Layar OTP telah terbuka & terverifikasi valid Jio.")

        # Simulasi kode OTP yang diterima bot
        demo_otp = "852963"
        print(f"\n[Langkah 5/6] Bot menerima SMS OTP ({demo_otp}) dan mengisi ke kotak input...")
        cnt = await otp_boxes.count()
        if cnt >= len(demo_otp):
            for i, digit in enumerate(demo_otp):
                await otp_boxes.nth(i).fill(digit)
                await asyncio.sleep(0.1)
        else:
            # Fallback jika bentuknya single input
            fallback_inp = page.locator("#otpInput, input[type='number'], input[type='tel']")
            if await fallback_inp.count() > 0:
                await fallback_inp.first.fill(demo_otp)

        await asyncio.sleep(1.0)

        print("[Langkah 6/6] Mengklik tombol 'Submit / Verify' login Jio...")
        submit_btn = page.locator("button:has-text('Submit'), button:has-text('Proceed'), button:has-text('Verify'), button[type='submit']")
        if await submit_btn.count() > 0:
            try:
                await submit_btn.first.click()
                print("             ✓ Tombol Submit diklik!")
            except Exception:
                pass

        print("\n" + "="*65)
        print("🎉 TAHAP KRUSIAL: FITUR AUTO-HOLD DIAKTIFKAN!")
        print("="*65)
        print("👉 PERHATIKAN LAYAR MONITOR ANDA SEKARANG:")
        print("   - Jendela browser Playwright ini TIDAK DI-CLOSE dan TIDAK HILANG!")
        print("   - Browser tetap berada di halaman Jio dengan nomor dan OTP yang terisi.")
        print("   - Anda bebas mengklik tombol klaim promo Gemini / Google One langsung")
        print("     di jendela browser yang terbuka ini.")
        print("="*65)
        print("\n⏳ Browser ini akan tetap ditahan terbuka selama 45 detik...")
        print("   (Silakan klik atau berinteraksi langsung dengan jendela browser tersebut)\n")

        for s in range(45, 0, -5):
            print(f"Sisa waktu penahanan: {s} detik (browser tetap terbuka)...", flush=True)
            await asyncio.sleep(5)

        print("\nWaktu demonstrasi selesai. Menutup browser Playwright.")
        await browser.close()

if __name__ == "__main__":
    phone = sys.argv[1] if len(sys.argv) > 1 else "9820012345"
    asyncio.run(run_full_demo(phone))
