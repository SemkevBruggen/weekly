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

# ✅ Webhooks
URLS_WEBHOOK = "https://script.google.com/macros/s/AKfycbxHw1J2asNBEdd5LHZj2LqTjwKVsjKufYhMSSeq6nRhY65mTVeuDai_oSt_lWRB_MkE/exec"
GOOGLE_SHEET_WEBHOOK = "https://script.google.com/macros/s/AKfycbxk3E7nQq4CzUl4axl3A695xUjccgovMOkwlQicwVaHxiDIyF2GhlviIzBddYqRlEUj/exec"

# Instellingen
STANDARD_SIZE = (256, 256)
PLACEHOLDER_KEYWORDS = ["placeholder", "small_image", "default_image", "no_image"]
EXCLUDED_DOMAINS = ["storage.googleapis.com"]
MIN_WIDTH = 1200
MIN_HEIGHT = 1200
BLUR_THRESHOLD = 100

semaphore = asyncio.Semaphore(5)

async def get_urls_from_webhook():
    async with aiohttp.ClientSession() as session:
        async with session.get(URLS_WEBHOOK) as response:
            data = await response.json()
            print("🔍 Opgehaalde URLs:", json.dumps(data, indent=2))
            return data.get("urls", [])

async def fetch_image(session, image_url):
    async with session.get(image_url, timeout=10) as response:
        response.raise_for_status()
        return await response.read()

async def is_blurry(image_url, session):
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

async def analyze_images_on_page(page_url, website_domain, session):
    async with semaphore:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
            page = await browser.new_page()
            await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)

            content = await page.content()
            image_elements = await page.query_selector_all('img')
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

        if broken_images or placeholder_images or duplicates or any('Blurry' in r.get('sharpness', '') or 'Too Small' in r.get('resolution_check', '') for r in blurry_results):
            result_data = {
                "url": page_url,
                "broken": ", ".join(broken_images) if broken_images else "Geen",
                "placeholder": ", ".join(placeholder_images) if placeholder_images else "Geen",
                "duplicate": ", ".join({img for dupes in duplicates.values() for img in dupes}) if duplicates else "Geen",
                "blur_details": blurry_results
            }
            print(json.dumps(result_data, indent=2))
            try:
                response = requests.post(GOOGLE_SHEET_WEBHOOK, json=[result_data])
                print(f"📤 Gegevens verzonden naar Google Sheets: {response.text}")
            except requests.exceptions.RequestException as e:
                print(f"❌ Fout bij verzenden naar Google Sheets: {e}")
        else:
            print(f"✅ Geen problemen gevonden op {page_url}, niet verzonden naar Google Sheets.")

async def main():
    urls = await get_urls_from_webhook()
    async with aiohttp.ClientSession() as session:
        tasks = []
        for full_url in urls:
            parsed = urllib.parse.urlparse(full_url)
            domain = parsed.netloc
            tasks.append(analyze_images_on_page(full_url, domain, session))
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
