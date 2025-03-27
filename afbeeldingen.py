import asyncio
import requests
import cv2
import numpy as np
import urllib.parse
import json
import aiohttp
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from collections import defaultdict
from io import BytesIO
from PIL import Image, UnidentifiedImageError
import sys

# ✅ Webhooks
URLS_WEBHOOK = "https://script.google.com/macros/s/AKfycbxHw1J2asNBEdd5LHZj2LqTjwKVsjKufYhMSSeq6nRhY65mTVeuDai_oSt_lWRB_MkE/exec"
GOOGLE_SHEET_WEBHOOK = "https://script.google.com/macros/s/AKfycbzQkG9loPCfTLpFD63Fn1NkDx5vQmS7_JwynhIdXt1Nah6py7ox7fJiOEtZD06y1ZDZ/exec"

# Instellingen
STANDARD_SIZE = (256, 256)
PLACEHOLDER_KEYWORDS = ["placeholder", "small_image", "default_image", "no_image"]
EXCLUDED_DOMAINS = ["storage.googleapis.com"]
MIN_WIDTH = 1200
MIN_HEIGHT = 1200
BLUR_THRESHOLD = 100

async def get_urls_from_webhook():
    async with aiohttp.ClientSession() as session:
        async with session.get(URLS_WEBHOOK) as response:
            data = await response.json()
            print("\U0001F50D Opgehaalde URLs:", json.dumps(data, indent=2))
            return data.get("urls", [])

async def fetch_image(session, image_url):
    async with session.get(image_url, timeout=10) as response:
        response.raise_for_status()
        return await response.read()

async def is_blurry(image_url, session):
    if "paypal" in image_url.lower() or "storage.googleapis.com" in image_url:
        return {
            "image_url": image_url,
            "excluded": True
        }

    try:
        image_data = await fetch_image(session, image_url)
        img_array = np.frombuffer(image_data, dtype=np.uint8)
        image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        if image is None:
            raise Exception("Image decoding failed")

        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()

        blurry = laplacian_var < BLUR_THRESHOLD
        too_small = width < MIN_WIDTH or height < MIN_HEIGHT

        return {
            "image_url": image_url,
            "width": width,
            "height": height,
            "blur_score": round(laplacian_var, 2),
            "sharpness": "Blurry" if blurry else "Sharp",
            "resolution_check": "Too Small" if too_small else "OK"
        }
    except Exception as e:
        return {"image_url": image_url, "error": str(e)}

async def analyze_images_on_page(page_url, website_domain, session, semaphore, collected_results):
    async with semaphore:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()

            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                if "ERR_CERT_COMMON_NAME_INVALID" in str(e):
                    print(f"⚠️ SSL-fout genegeerd op: {page_url}")
                else:
                    print(f"❌ Fout bij openen van {page_url}: {e}")
                    await page.close()
                    await context.close()
                    await browser.close()
                    return

            content = await page.content()
            await page.close()
            await context.close()
            await browser.close()

        soup = BeautifulSoup(content, "html.parser")
        image_tags = soup.find_all("img")
        image_urls = [img.get("src") for img in image_tags if img.get("src")]

        absolute_image_urls = []
        for url in image_urls:
            if ".svg" in url.lower():
                continue
            if website_domain not in url and "cdn" in url:
                continue
            if not url.startswith("http"):
                url = f"https://{website_domain}/{url.lstrip('/')}"
            absolute_image_urls.append(url)

        broken_images = set()
        placeholder_images = set()
        image_arrays = {}
        blur_tasks = []

        def get_image_array(url):
            try:
                response = requests.get(url, stream=True, timeout=10)
                response.raise_for_status()
                if any(keyword in url.lower() for keyword in PLACEHOLDER_KEYWORDS):
                    placeholder_images.add(url)
                    return None
                img = Image.open(BytesIO(response.content))
                img = img.resize(STANDARD_SIZE, Image.LANCZOS)
                return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
            except UnidentifiedImageError:
                broken_images.add(url)
                return None
            except requests.exceptions.RequestException:
                broken_images.add(url)
                return None

        for url in absolute_image_urls:
            image_arrays[url] = get_image_array(url)
            blur_tasks.append(asyncio.create_task(is_blurry(url, session)))

        duplicates = defaultdict(set)
        checked = set()
        for url1, img1 in image_arrays.items():
            if img1 is None or any(domain in url1 for domain in EXCLUDED_DOMAINS):
                continue
            for url2, img2 in image_arrays.items():
                if url1 == url2 or (url2, url1) in checked:
                    continue
                if img2 is None or any(domain in url2 for domain in EXCLUDED_DOMAINS):
                    continue
                diff = cv2.absdiff(img1, img2)
                if np.sum(diff) == 0:
                    duplicates[url1].add(url2)
                    checked.add((url1, url2))

        blurry_results = await asyncio.gather(*blur_tasks)

        result_data = {
            "url": page_url,
            "broken": ", ".join(broken_images) if broken_images else "Geen",
            "placeholder": ", ".join(placeholder_images) if placeholder_images else "Geen",
            "duplicate": ", ".join({img for dupes in duplicates.values() for img in dupes}) if duplicates else "Geen",
            "blur_details": blurry_results
        }

        collected_results.append(result_data)

        # Logging
        has_issues = (
            result_data["broken"] != "Geen" or
            result_data["placeholder"] != "Geen" or
            result_data["duplicate"] != "Geen" or
            any(r.get("sharpness") == "Blurry" for r in blurry_results if not r.get("excluded"))
        )

        if has_issues:
            print(f"⚠️ Problemen gevonden op: {page_url}")
        else:
            print(f"✅ Geen problemen op: {page_url}")

async def main():
    urls = await get_urls_from_webhook()
    if not urls:
        print("❌ Geen URLs opgehaald!")
        return

    semaphore = asyncio.Semaphore(5)
    BATCH_SIZE = 10
    total = len(urls)
    collected_results = []

    print(f"🔍 Totaal aantal URLs om te controleren: {total}")

    async with aiohttp.ClientSession() as session:
        for i in range(0, total, BATCH_SIZE):
            batch = urls[i:i + BATCH_SIZE]
            tasks = []

            for idx, full_url in enumerate(batch):
                parsed = urllib.parse.urlparse(full_url)
                domain = parsed.netloc
                absolute_index = i + idx + 1
                print(f"➡️ ({absolute_index}/{total}) Start controle: {full_url}")

                task = asyncio.wait_for(
                    analyze_images_on_page(full_url, domain, session, semaphore, collected_results),
                    timeout=120
                )
                tasks.append(task)

            try:
                await asyncio.gather(*tasks)
            except asyncio.TimeoutError as e:
                print(f"⏰ Timeout in batch {i // BATCH_SIZE + 1}: {e}")

            print(f"✅ Batch {i // BATCH_SIZE + 1} afgerond.\n")

    if collected_results:
        try:
            response = requests.post(GOOGLE_SHEET_WEBHOOK, json=collected_results)
            print(f"\n📨 Verzonden naar Google Sheets ({len(collected_results)} items): {response.text}")
        except requests.exceptions.RequestException as e:
            print(f"❌ Fout bij verzenden naar Google Sheets: {e}")
    else:
        print("✅ Geen resultaten om te verzenden.")

    print("🏁 Alle batches verwerkt.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
        print("✅ Script succesvol afgerond.")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Fout tijdens uitvoeren: {e}")
        sys.exit(1)
