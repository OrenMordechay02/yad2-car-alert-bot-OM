"""Yad2 car listings scraper.

Flow:
  1. Warmup session on www.yad2.co.il to obtain valid cookies.
  2. Pass those cookies explicitly to gw.yad2.co.il (different subdomain).
  3. Detect bot-protection redirects and raise BotProtectionError.
"""

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import httpx

logger = logging.getLogger(__name__)

WARMUP_URL = "https://www.yad2.co.il/vehicles/cars"
GW_API = "https://gw.yad2.co.il/feed-search-legacy/vehicles/cars"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Connection": "keep-alive",
}


class BotProtectionError(RuntimeError):
    pass


@dataclass
class Listing:
    id: str
    title: str
    price: str
    year: str
    km: str
    hand: str
    location: str
    url: str


def _search_url_to_api_params(search_url: str) -> dict:
    parsed = urlparse(search_url)
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


def _is_bot_protection(response: httpx.Response) -> bool:
    if "perfdrive.com" in str(response.url) or "validate." in str(response.url):
        return True
    ct = response.headers.get("content-type", "")
    if response.status_code == 200 and "json" not in ct and len(response.content) < 1000:
        return True
    return False


def _parse_listing(item: dict) -> Listing:
    info = item.get("row_4", [])
    year = km = hand = ""
    for entry in info:
        label = entry.get("label", "")
        value = str(entry.get("value", ""))
        if "שנה" in label or re.match(r"20\d\d|19\d\d", value):
            year = value
        elif 'ק"מ' in label or "km" in label.lower():
            km = value
        elif "יד" in label:
            hand = value

    listing_id = str(item.get("id") or item.get("order_id") or "")
    title_parts = [
        item.get("manufacturer_he") or item.get("manufacturer") or "",
        item.get("model_he") or item.get("model") or "",
        item.get("sub_model_he") or item.get("sub_model") or "",
    ]
    title = " ".join(p for p in title_parts if p).strip() or item.get("title", "")
    price = str(item.get("price") or item.get("price_n") or "")
    location = item.get("area_text") or item.get("city_text") or ""
    slug = item.get("link") or listing_id
    url = f"https://www.yad2.co.il/vehicles/private-cars/{slug}"

    return Listing(
        id=listing_id, title=title, price=price,
        year=year, km=km, hand=hand, location=location, url=url,
    )


def _fetch_with_session(params: dict) -> dict:
    with httpx.Client(
        headers={**BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*", "Referer": "https://www.google.com/"},
        timeout=30,
        follow_redirects=True,
    ) as client:
        # Step 1: warmup on www — get session cookies
        warmup = client.get(WARMUP_URL, params=params)
        logger.info("Warmup: %s | cookies: %s", warmup.status_code, list(client.cookies.keys()))
        time.sleep(2)

        # Step 2: call gw API — pass cookies explicitly via Cookie header
        cookie_str = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
        api_headers = {
            **BROWSER_HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.yad2.co.il/vehicles/cars",
            "Origin": "https://www.yad2.co.il",
            "Cookie": cookie_str,
        }

        response = client.get(GW_API, params=params, headers=api_headers)
        logger.info(
            "GW API: %s | content-type: %s | url: %s | body[:80]: %s",
            response.status_code,
            response.headers.get("content-type", "?"),
            str(response.url)[:100],
            response.text[:80],
        )

        if _is_bot_protection(response):
            raise BotProtectionError(
                f"Bot protection triggered — redirected to {response.url}. "
                "Cloud IPs are often blocked by Yad2. A residential proxy is needed."
            )

        if response.status_code != 200:
            response.raise_for_status()

        return response.json()


def scrape_listings(search_url: str) -> list[Listing]:
    params = _search_url_to_api_params(search_url)
    logger.info("Scraping Yad2 with params: %s", params)

    data = _fetch_with_session(params)

    feed = (
        data.get("data", {}).get("feed", {}).get("feed_items")
        or data.get("data", {}).get("feed_items")
        or data.get("feed_items")
        or []
    )

    listings = []
    for item in feed:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("ad", "platinum") or "id" in item:
            try:
                listing = _parse_listing(item)
                if listing.id:
                    listings.append(listing)
            except Exception:
                logger.exception("Failed to parse listing: %s", item.get("id"))

    logger.info("Scraped %d listings", len(listings))
    return listings
