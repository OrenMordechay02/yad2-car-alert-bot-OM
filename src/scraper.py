"""Yad2 car listings scraper.

Strategy: call Yad2's internal JSON API with full browser headers + cookie
session. If Yad2 returns a bot-protection page (validate.perfdrive.com or
non-JSON body) we raise a clear BotProtectionError.

Fallback path: replace scrape_listings() with a Playwright implementation
— the Listing dataclass and return signature stay the same.
"""

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://www.yad2.co.il"
API_PATH = "/api/pre-load/getFeedIndex/vehicles/cars"

# Full browser headers — mimics Chrome on macOS as closely as possible
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.yad2.co.il/vehicles/cars",
    "Origin": "https://www.yad2.co.il",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
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
    if "perfdrive.com" in str(response.url):
        return True
    ct = response.headers.get("content-type", "")
    if "json" not in ct and len(response.content) < 500:
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
        id=listing_id,
        title=title,
        price=price,
        year=year,
        km=km,
        hand=hand,
        location=location,
        url=url,
    )


def _fetch_with_session(params: dict) -> dict:
    """Open a session, warm up cookies on the homepage, then call the API."""
    with httpx.Client(
        headers=HEADERS,
        timeout=30,
        follow_redirects=True,
        http2=False,
    ) as client:
        # Warm up: load the search page so Yad2 sets session cookies
        warmup = client.get(f"{BASE_URL}/vehicles/cars", params=params)
        logger.info("Warmup request: %s %s", warmup.status_code, str(warmup.url)[:80])
        time.sleep(1.5)

        # Now call the API endpoint with the same session cookies
        api_url = BASE_URL + API_PATH
        response = client.get(api_url, params=params)
        logger.info(
            "API response: %s | content-type: %s | url: %s",
            response.status_code,
            response.headers.get("content-type", "?"),
            str(response.url)[:100],
        )

        if _is_bot_protection(response):
            raise BotProtectionError(
                f"Yad2 returned a bot-protection page (redirected to {response.url}). "
                "This usually happens from cloud IPs. Consider adding a residential proxy."
            )

        if response.status_code != 200:
            response.raise_for_status()

        return response.json()


def scrape_listings(search_url: str) -> list[Listing]:
    params = _search_url_to_api_params(search_url)
    logger.info("Scraping Yad2 with params: %s", params)

    try:
        data = _fetch_with_session(params)
    except BotProtectionError as e:
        logger.error("Bot protection triggered: %s", e)
        raise

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
