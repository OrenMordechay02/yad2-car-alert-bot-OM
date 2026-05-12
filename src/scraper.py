"""Yad2 car listings scraper.

Strategy: fetch the Yad2 search page and extract listings from the
__NEXT_DATA__ JSON blob that Next.js embeds in every page. No separate
API call needed — the page itself contains all listing data.
"""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs
from html.parser import HTMLParser

import httpx

logger = logging.getLogger(__name__)

SEARCH_BASE = "https://www.yad2.co.il/vehicles/cars"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
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


class _NextDataParser(HTMLParser):
    """Pull the content of <script id="__NEXT_DATA__"> out of a page."""

    def __init__(self):
        super().__init__()
        self._in_target = False
        self.data = ""

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            attr_dict = dict(attrs)
            if attr_dict.get("id") == "__NEXT_DATA__":
                self._in_target = True

    def handle_data(self, data):
        if self._in_target:
            self.data += data

    def handle_endtag(self, tag):
        if tag == "script" and self._in_target:
            self._in_target = False


def _extract_next_data(html: str) -> dict:
    parser = _NextDataParser()
    parser.feed(html)
    if not parser.data.strip():
        raise ValueError("__NEXT_DATA__ script tag not found in page")
    return json.loads(parser.data)


def _search_url_to_params(search_url: str) -> dict:
    parsed = urlparse(search_url)
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


def _parse_listing(item: dict) -> Listing | None:
    listing_id = str(item.get("id") or item.get("orderId") or item.get("order_id") or "")
    if not listing_id:
        return None

    # title
    title_parts = [
        item.get("manufacturer_he") or item.get("manufacturer") or item.get("manufacturerHe") or "",
        item.get("model_he") or item.get("model") or item.get("modelHe") or "",
        item.get("sub_model_he") or item.get("subModel") or item.get("subModelHe") or "",
    ]
    title = " ".join(p for p in title_parts if p).strip() or item.get("title", "")

    price = str(item.get("price") or item.get("price_n") or "")
    year = str(item.get("year") or item.get("manufactureYear") or "")
    km = str(item.get("km") or item.get("kilometers") or "")
    hand = str(item.get("hand") or item.get("handNum") or "")
    location = item.get("area_text") or item.get("city_text") or item.get("cityText") or item.get("areaText") or ""

    slug = item.get("link") or item.get("adNumber") or listing_id
    url = f"https://www.yad2.co.il/vehicles/private-cars/{slug}"

    return Listing(
        id=listing_id, title=title, price=price,
        year=year, km=km, hand=hand, location=location, url=url,
    )


def _looks_like_listing(item: dict) -> bool:
    return isinstance(item, dict) and bool(
        item.get("id") or item.get("orderId") or item.get("order_id")
    )


def _find_feed_items(obj: object, depth: int = 0) -> list[dict]:
    """Recursively search for a list of listing dicts.

    Handles:
    - Plain feed_items / feedItems / items keys
    - React Query dehydrated state: queries[].state.data.feed_items
    """
    if depth > 12:
        return []
    if isinstance(obj, dict):
        # React Query dehydrated state
        if "queries" in obj:
            queries = obj["queries"]
            if isinstance(queries, list):
                for q in queries:
                    data = (q.get("state") or {}).get("data") or {}
                    logger.info(
                        "React Query entry keys: %s",
                        list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                    )
                    found = _find_feed_items(data, depth + 1)
                    if found:
                        return found

        for key in ("feed_items", "feedItems", "items", "data", "feed", "results"):
            val = obj.get(key)
            if isinstance(val, list) and val and _looks_like_listing(val[0]):
                return val
            if isinstance(val, dict):
                found = _find_feed_items(val, depth + 1)
                if found:
                    return found

        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = _find_feed_items(v, depth + 1)
                if found:
                    return found

    elif isinstance(obj, list):
        # Maybe the list itself is the listings
        if obj and _looks_like_listing(obj[0]):
            return obj
        for item in obj:
            found = _find_feed_items(item, depth + 1)
            if found:
                return found
    return []


def scrape_listings(search_url: str) -> list[Listing]:
    params = _search_url_to_params(search_url)
    logger.info("Fetching Yad2 search page with params: %s", params)

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        response = client.get(SEARCH_BASE, params=params)

    logger.info(
        "Page response: %s | content-type: %s | url: %s",
        response.status_code,
        response.headers.get("content-type", "?"),
        str(response.url)[:100],
    )

    if "perfdrive.com" in str(response.url) or "validate." in str(response.url):
        raise BotProtectionError(f"Bot protection triggered — redirected to {response.url}")

    if response.status_code != 200:
        logger.error("Unexpected status %s: %s", response.status_code, response.text[:200])
        response.raise_for_status()

    html = response.text
    logger.info("Page HTML size: %d bytes", len(html))

    try:
        next_data = _extract_next_data(html)
    except ValueError as e:
        logger.error("Could not extract __NEXT_DATA__: %s | HTML snippet: %s", e, html[:300])
        raise

    feed_items = _find_feed_items(next_data)
    logger.info("Found %d raw feed items in __NEXT_DATA__", len(feed_items))

    listings = []
    for item in feed_items:
        if not isinstance(item, dict):
            continue
        try:
            listing = _parse_listing(item)
            if listing:
                listings.append(listing)
        except Exception:
            logger.exception("Failed to parse listing: %s", item.get("id"))

    logger.info("Scraped %d listings", len(listings))
    return listings
