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


def _str(val: object) -> str:
    """Extract a plain string from a value that may be a str, int, or dict."""
    if val is None:
        return ""
    if isinstance(val, dict):
        # Yad2 uses {"text": "...", "value": ...} or {"name": "..."}
        return str(val.get("text") or val.get("name") or val.get("value") or "")
    return str(val)


_logged_sample = False


def _parse_listing(item: dict) -> Listing | None:
    global _logged_sample
    if not _logged_sample:
        logger.info("SAMPLE item keys: %s", list(item.keys()))
        logger.info("SAMPLE item values: %s", {k: item[k] for k in list(item.keys())[:30]})
        _logged_sample = True

    listing_id = str(item.get("id") or item.get("orderId") or item.get("order_id") or "")
    if not listing_id:
        return None

    title_parts = [
        _str(item.get("manufacturer_he") or item.get("manufacturer") or item.get("manufacturerHe")),
        _str(item.get("model_he") or item.get("model") or item.get("modelHe")),
        _str(item.get("sub_model_he") or item.get("subModel") or item.get("subModelHe")),
    ]
    title = " ".join(p for p in title_parts if p).strip() or _str(item.get("title"))

    price = _str(item.get("price") or item.get("price_n") or item.get("priceOnly"))
    year = _str(item.get("year") or item.get("manufactureYear"))
    km = _str(item.get("km") or item.get("kilometers"))
    hand = _str(item.get("hand") or item.get("handNum"))
    location = _str(
        item.get("area_text") or item.get("city_text")
        or item.get("cityText") or item.get("areaText")
        or item.get("city") or item.get("area")
    )

    # token is the URL-visible slug; orderId is the numeric ID used as DB key
    token = item.get("token") or listing_id
    url = f"https://www.yad2.co.il/item/{token}"

    return Listing(
        id=listing_id, title=title, price=price,
        year=year, km=km, hand=hand, location=location, url=url,
    )


LISTING_CATEGORY_KEYS = ("private", "platinum", "boost", "solo")


def _looks_like_listing(item: object) -> bool:
    return isinstance(item, dict) and bool(
        item.get("id") or item.get("orderId") or item.get("order_id")
    )


def _extract_from_react_query_data(data: dict) -> list[dict]:
    """Extract listings from a React Query data payload.

    Yad2 stores listings under category keys:
    private / platinum / boost / solo / commercial
    """
    all_items: list[dict] = []
    for key in LISTING_CATEGORY_KEYS:
        val = data.get(key)
        if isinstance(val, list):
            listings = [i for i in val if _looks_like_listing(i)]
            if listings:
                logger.info("Found %d items under key '%s'", len(listings), key)
                all_items.extend(listings)
    return all_items


def _find_feed_items(obj: object, depth: int = 0) -> list[dict]:
    """Recursively search for listing dicts in Next.js / React Query data."""
    if depth > 12:
        return []
    if isinstance(obj, dict):
        # React Query dehydrated state
        if "queries" in obj:
            for q in (obj["queries"] or []):
                data = (q.get("state") or {}).get("data") or {}
                if not isinstance(data, dict):
                    continue
                logger.info("React Query data keys: %s", list(data.keys()))
                items = _extract_from_react_query_data(data)
                if items:
                    return items
                # fall through to generic search on data
                found = _find_feed_items(data, depth + 1)
                if found:
                    return found

        for key in ("feed_items", "feedItems", "items", "feed", "results"):
            val = obj.get(key)
            if isinstance(val, list) and val and _looks_like_listing(val[0]):
                return val

        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = _find_feed_items(v, depth + 1)
                if found:
                    return found

    elif isinstance(obj, list):
        if obj and _looks_like_listing(obj[0]):
            return obj
        for item in obj:
            found = _find_feed_items(item, depth + 1)
            if found:
                return found
    return []


MAX_PAGES = 15  # safety cap — avoids infinite loops


def _fetch_page(params: dict) -> list[dict]:
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        response = client.get(SEARCH_BASE, params=params)

    if "perfdrive.com" in str(response.url) or "validate." in str(response.url):
        raise BotProtectionError(f"Bot protection triggered — {response.url}")

    if response.status_code != 200:
        logger.error("Unexpected status %s for page %s", response.status_code, params.get("page", 1))
        response.raise_for_status()

    try:
        next_data = _extract_next_data(response.text)
    except ValueError:
        logger.warning("No __NEXT_DATA__ on page %s — stopping pagination", params.get("page", 1))
        return []

    return _find_feed_items(next_data)


def scrape_listings(search_url: str) -> list[Listing]:
    base_params = _search_url_to_params(search_url)
    logger.info("Scraping Yad2 with params: %s", base_params)

    all_items: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        params = {**base_params, "page": str(page)}
        items = _fetch_page(params)
        logger.info("Page %d: %d raw items", page, len(items))

        if not items:
            break

        new_on_page = 0
        for item in items:
            item_id = str(item.get("orderId") or item.get("id") or "")
            if item_id and item_id not in seen_ids:
                seen_ids.add(item_id)
                all_items.append(item)
                new_on_page += 1

        # Stop when a page returns only duplicates or very few items
        if new_on_page == 0:
            logger.info("Page %d returned only duplicates — stopping", page)
            break

    logger.info("Total raw items across all pages: %d", len(all_items))

    listings = []
    for item in all_items:
        if not isinstance(item, dict):
            continue
        try:
            listing = _parse_listing(item)
            if listing:
                listings.append(listing)
        except Exception:
            logger.exception("Failed to parse listing: %s", item.get("orderId"))

    logger.info("Scraped %d listings total", len(listings))
    return listings
