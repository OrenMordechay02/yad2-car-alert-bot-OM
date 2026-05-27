"""Yad2 car listings scraper.

Strategy: fetch the Yad2 search page and extract listings from the
__NEXT_DATA__ JSON blob that Next.js embeds in every page. No separate
API call needed — the page itself contains all listing data.
"""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, urlencode
from html.parser import HTMLParser

import httpx
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

SEARCH_BASE = "https://www.yad2.co.il/vehicles/cars"
GW_BASE = "https://gw.yad2.co.il/feed-search-legacy/vehicles/cars"

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

GW_HEADERS = {
    **HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.yad2.co.il/vehicles/cars",
    "Origin": "https://www.yad2.co.il",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
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


def _parse_listing(item: dict) -> Listing | None:
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
    vehicle_dates = item.get("vehicleDates") or {}
    year = _str(
        item.get("year") or item.get("manufactureYear")
        or (vehicle_dates.get("yearOfProduction") if isinstance(vehicle_dates, dict) else None)
    )
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


def _extract_km(obj: object, depth: int = 0) -> str:
    if depth > 10:
        return ""
    if isinstance(obj, dict):
        for key in ("km", "kilometers", "mileage", "odometer"):
            val = obj.get(key)
            if val is not None:
                if isinstance(val, (int, float)) and int(val) > 0:
                    return str(int(val))
                if isinstance(val, str) and val.strip() and val not in ("0", ""):
                    return val.strip()
        for v in obj.values():
            if isinstance(v, (dict, list)):
                result = _extract_km(v, depth + 1)
                if result:
                    return result
    elif isinstance(obj, list):
        for item in obj:
            result = _extract_km(item, depth + 1)
            if result:
                return result
    return ""


def fetch_km(token: str) -> str:
    """Fetch km from the individual listing detail page."""
    url = f"https://www.yad2.co.il/item/{token}"
    try:
        with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code != 200:
            return ""
        if "perfdrive.com" in str(response.url) or "validate." in str(response.url):
            logger.warning("Bot protection on detail page for token %s", token)
            return ""
        next_data = _extract_next_data(response.text)
        return _extract_km(next_data)
    except Exception:
        logger.debug("Failed to fetch km for token %s", token)
        return ""


MAX_PAGES = 15  # safety cap — avoids infinite loops


def _fetch_page_gw(params: dict) -> list[dict] | None:
    """Try the yad2 gateway JSON API. Returns None if unavailable."""
    try:
        with httpx.Client(headers=GW_HEADERS, timeout=30, follow_redirects=True) as client:
            response = client.get(GW_BASE, params=params)
        if "perfdrive.com" in str(response.url) or "validate." in str(response.url):
            logger.warning("Bot protection on GW API page %s", params.get("page", 1))
            return None
        if response.status_code != 200:
            logger.warning("GW API status %s on page %s", response.status_code, params.get("page", 1))
            return None
        data = response.json()
        items = _find_feed_items(data)
        if items:
            logger.info("GW API returned %d items on page %s", len(items), params.get("page", 1))
        return items
    except Exception as exc:
        logger.warning("GW API failed on page %s: %s", params.get("page", 1), exc)
        return None


def _fetch_page_playwright(params: dict) -> list[dict]:
    """Fetch a page using Playwright (real Chromium) to bypass bot protection."""
    url = SEARCH_BASE + "?" + urlencode(params)
    logger.info("Fetching via Playwright: page %s", params.get("page", 1))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="he-IL",
                extra_http_headers={"Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8"},
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=45_000)
            content = page.content()
            browser.close()
        next_data = _extract_next_data(content)
        items = _find_feed_items(next_data)
        logger.info("Playwright: %d items on page %s", len(items), params.get("page", 1))
        return items
    except PlaywrightTimeout:
        logger.warning("Playwright timeout on page %s", params.get("page", 1))
        return []
    except Exception as exc:
        logger.warning("Playwright failed on page %s: %s", params.get("page", 1), exc)
        return []


def _fetch_page(params: dict) -> list[dict]:
    # Try gateway API first (JSON, no bot protection)
    gw_items = _fetch_page_gw(params)
    if gw_items:
        return gw_items

    # Fall back to Playwright (real Chrome, bypasses JS bot challenges)
    return _fetch_page_playwright(params)


def scrape_listings(search_url: str) -> list[Listing]:
    base_params = _search_url_to_params(search_url)
    logger.info("Scraping Yad2 with params: %s", base_params)

    all_items: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        params = {**base_params, "page": str(page)}
        try:
            items = _fetch_page(params)
        except BotProtectionError as e:
            logger.warning("Bot protection on page %d — skipping URL: %s", page, e)
            break
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
