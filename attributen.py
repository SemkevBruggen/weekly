import aiohttp
import asyncio
import json
import os
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# ✅ Webhooks
URLS_WEBHOOK = "https://script.google.com/macros/s/AKfycbxHw1J2asNBEdd5LHZj2LqTjwKVsjKufYhMSSeq6nRhY65mTVeuDai_oSt_lWRB_MkE/exec"  # Webhook voor het ophalen van URLs
RESULTS_WEBHOOK = "https://script.google.com/macros/s/AKfycbzOmgc243Qvf58eoonMpBoVyclmLmfexmiz_zRWTZLg1AIHYGovUGEtcCIU59PvU1Ke/exec"  # Webhook voor het verzenden van resultaten

# ✅ Beperk het aantal gelijktijdige Playwright-verzoeken
semaphore = asyncio.Semaphore(5)  # Maximaal 5 tegelijk

async def get_urls_from_webhook():
    """Haalt de URLs op via de webhook van Google Sheets."""
    async with aiohttp.ClientSession() as session:
        async with session.get(URLS_WEBHOOK) as response:
            data = await response.json()
            print("🔍 Opgehaalde URLs:", json.dumps(data, indent=2))  # Debug print
            return data.get("urls", [])

async def fetch(session, url):
    """Haalt de HTML op en controleert of een bepaalde div aanwezig is."""
    try:
        async with session.get(url, timeout=10) as response:
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            has_div = bool(soup.find("div", id="configurator-app"))
            return {"url": url, "configurator_app_present": has_div}
    except Exception as e:
        return {"url": url, "error": str(e)}

async def scrape_page(session, url):
    """Laadt een pagina met Playwright en controleert de prijs, USP's en de Vragen & Antwoorden-sectie."""
    async with semaphore:  # Wacht tot er een plek vrij is
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
                    "has_faq": False
                }

            await page.wait_for_timeout(3000)

            try:
                await page.wait_for_selector("div.mt-6.w-full.space-y-1", timeout=5000)
            except:
                print(f"⚠️ USP-container niet gevonden op {url}")

            content = await page.content()
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

        vragen_en_antwoorden = soup.find("div", id="vragen-en-antwoorden") is not None

        return {
            "url": url,
            "is_target_price": is_target_price,
            "has_usp": has_usp,
            "has_faq": vragen_en_antwoorden
        }

async def main():
    """Haalt URLs op via webhook, scrapet en stuurt resultaten naar Google Sheets."""
    urls = await get_urls_from_webhook()
    if not urls:
        print("❌ Geen URLs opgehaald!")
        return

    async with aiohttp.ClientSession() as session:
        tasks = [scrape_page(session, url) for url in urls]
        playwright_results = await asyncio.gather(*tasks)

        # ✅ Voeg BeautifulSoup-gebaseerde scraping toe
        tasks_bs = [fetch(session, url) for url in urls]
        bs_results = await asyncio.gather(*tasks_bs)

        # ✅ Combineer resultaten
        combined_results = []
        for p_result, bs_result in zip(playwright_results, bs_results):
            combined_entry = {**p_result, **bs_result}
            combined_results.append(combined_entry)

        if not combined_results:
            print("❌ Geen resultaten gevonden, mogelijk een scraping-fout!")
            return

        json_output = {
            "type": "attributen",
            "data": combined_results
        }

        # ✅ Fix: Sla results.json op voordat de webhook wordt aangeroepen
        with open("results.json", "w") as f:
            json.dump(json_output, f, indent=2)

        # ✅ Controleer of results.json correct is opgeslagen
        if not os.path.exists("results.json"):
            print("❌ results.json is niet gegenereerd! Controleer scraper.")
            exit(1)

        print("📤 Verzonden JSON:", json.dumps(json_output, indent=2))

        # ✅ Verstuur resultaten naar webhook
        async with session.post(RESULTS_WEBHOOK, json=json_output) as resp:
            response_text = await resp.text()
            print("📡 Webhook response:", response_text)

if __name__ == "__main__":
    asyncio.run(main())
