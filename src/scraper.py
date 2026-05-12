"""Yad2 car listings scraper.

Yad2 renders its feed via a JSON API endpoint. We derive the API URL from
the user-facing search URL and call it directly — no browser needed.

If Yad2 changes their API in the future the fallback is to switch this
module to Playwright; the interface (scrape_listings) stays the same.
"""

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import httpx

logger = logging.getLogger(__name__)

BASE_API = "https://gw.yad2.co.il/feed-search-legacy/vehicles/cars"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.yad2.co.il/",
}


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
    """Extract query params from a Yad2 browser search URL."""
    parsed = urlparse(search_url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    return params


def _parse_listing(item: dict) -> Listing:
    info = item.get("row_4", [])
    year = km = hand = ""
    for entry in info:
        label = entry.get("label", "")
        value = entry.get("value", "")
        if "שנה" in label or re.match(r"20\d\d|19\d\d", str(value)):
            year = str(value)
        elif "ק\"מ" in label or "km" in label.lower():
            km = str(value)
        elif "יד" in label:
            hand = str(value)

    listing_id = str(item.get("id") or item.get("order_id") or "")
    title_parts = [
        item.get("manufacturer_he") or item.get("manufacturer") or "",
        item.get("model_he") or item.get("model") or "",
        item.get("sub_model_he") or item.get("sub_model") or "",
    ]
    title = " ".join(p for p in title_parts if p).strip() or item.get("title", "")
    price = str(item.get("price") or item.get("price_n") or "")
    location = item.get("area_text") or item.get("city_text") or ""
    slug = item.get("link") or item.get("id") or listing_id
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


def scrape_listings(search_url: str) -> list[Listing]:
    params = _search_url_to_api_params(search_url)
    params.setdefault("forceLdLoad", "1")

    logger.info("Fetching Yad2 API with params: %s", params)

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        response = client.get(BASE_API, params=params)

    if response.status_code != 200:
        logger.error("Yad2 API returned %s: %s", response.status_code, response.text[:300])
        response.raise_for_status()

    data = response.json()
    feed = (
        data.get("data", {}).get("feed", {}).get("feed_items")
        or data.get("data", {}).get("feed_items")
        or []
    )

    listings = []
    for item in feed:
        if item.get("type") in ("ad", "platinum") or "id" in item:
            try:
                listings.append(_parse_listing(item))
            except Exception:
                logger.exception("Failed to parse listing: %s", item.get("id"))

    logger.info("Scraped %d listings", len(listings))
    return listings
