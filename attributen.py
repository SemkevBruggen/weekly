import aiohttp
import asyncio
import json
import os
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ✅ Webhooks
URLS_WEBHOOK = "https://script.google.com/macros/s/AKfycbxHw1J2asNBEdd5LHZj2LqTjwKVsjKufYhMSSeq6nRhY65mTVeuDai_oSt_lWRB_MkE/exec"  # Webhook voor het ophalen van URLs
RESULTS_WEBHOOK = "https://script.google.com/macros/s/AKfycbwB3HUYBo-pXCl8GvRrGVMPvV-oXsfotRwVKvgI-MxOIwF41zjUAPi-khbT7sVqHN0H/exec"  # Webhook voor het verzenden van resultaten

# ✅ Beperk het aantal gelijktijdige Playwright-verzoeken
semaphore = asyncio.Semaphore(5)  # Maximaal 5 tegelijk

async def get_urls_from_webhook():
    async with aiohttp.ClientSession() as session:
        async with session.get(URLS_WEBHOOK) as response:
            data = await response.json()
            print("🔍 Opgehaalde URLs:", json.dumps(data, indent=2))
            return data.get("urls", [])

async def scrape_page(session, url):
    async with semaphore:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"❌ Fout bij laden van {url}: {e}")
                await browser.close()
                return {
                    "url": url,
                    "is_target_price": False,
                    "has_usp": False,
                    "has_faq": False,
                    "configurator_app_present": False
                }

            await page.wait_for_timeout(3000)

            try:
                await page.wait_for_selector("div.mt-6.w-full.space-y-1", timeout=5000)
            except:
                print(f"⚠️ USP-container niet gevonden op {url}")

            content = await page.content()

            # ✅ Check configurator-app met Playwright direct
            configurator = await page.query_selector("#configurator-app")
            has_configurator_app = configurator is not None

            await browser.close()

        soup = BeautifulSoup(content, "html.parser")

        price_element = soup.find("span", class_="text-4xl font-extrabold")
        is_target_price = False
        if price_element:
            price_text = price_element.get_text(strip=True).replace("€", "").replace(",", ".")
            try:
                price_value = float(price_text)
                is_target_price = price_value in [0.00, 1.00]
            except ValueError:
                is_target_price = False

        usp_container = soup.find("div", class_="mt-6 w-full space-y-1")
        has_usp = usp_container is not None and usp_container.find("span") is not None

        vragen_en_antwoorden = (
            soup.find("div", id="vragen-en-antwoorden") is not None or
            soup.find("div", class_="w-full h-fit sticky top-0 lg:border lg:border-gray-ultralight rounded-xl lg:p-5 lg:min-h-[350px]") is not None
        )

        return {
            "url": url,
            "is_target_price": is_target_price,
            "has_usp": has_usp,
            "has_faq": vragen_en_antwoorden,
            "configurator_app_present": has_configurator_app
        }

async def main():
    urls = await get_urls_from_webhook()
    if not urls:
        print("❌ Geen URLs opgehaald!")
        return

    async with aiohttp.ClientSession() as session:
        tasks = [scrape_page(session, url) for url in urls]
        results = await asyncio.gather(*tasks)

        json_output = {
            "type": "attributen",
            "data": results
        }

        with open("results.json", "w") as f:
            json.dump(json_output, f, indent=2)

        if not os.path.exists("results.json"):
            print("❌ results.json is niet gegenereerd! Controleer scraper.")
            exit(1)

        print("📤 Verzonden JSON:", json.dumps(json_output, indent=2))

        async with session.post(RESULTS_WEBHOOK, json=json_output) as resp:
            response_text = await resp.text()
            print("📡 Webhook response:", response_text)

if __name__ == "__main__":
    asyncio.run(main())
