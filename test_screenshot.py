import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page()
        await page.goto('https://www.jio.com/selfcare/login/', wait_until='networkidle')
        await page.locator('#inputField').fill('9820012345')
        await page.locator("button:has-text('Generate OTP')").click()
        await asyncio.sleep(1.5)
        await page.screenshot(path='web/public/live_jio.png')
        print('Screenshot saved successfully!')
        await b.close()

if __name__ == '__main__':
    asyncio.run(main())
