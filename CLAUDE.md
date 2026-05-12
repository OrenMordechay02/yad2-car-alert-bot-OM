# Yad2 Car Alert Bot — Project Context

## מה הפרויקט עושה
בוט שמנטר חיפוש ביד2, מזהה מודעות רכב חדשות, ושולח התראה לטלגרם.
רץ אוטומטית כל 30 דקות ב-GitHub Actions, ללא שרת וללא מחשב אישי.

## ארכיטקטורה
```
GitHub Actions (cron כל 30 דק, 07:00–00:00 שעון ישראל)
  → src/scraper.py   — שולף את דף החיפוש של יד2, מחלץ מודעות מ-__NEXT_DATA__
  → src/db.py        — משווה מול Supabase, מחזיר רק חדשים
  → src/telegram.py  — שולח הודעה לטלגרם על כל מודעה חדשה
```

## מבנה קבצים
```
yad2-car-alert-bot/
├── .github/workflows/check-yad2.yml   ← GitHub Actions cron
├── src/
│   ├── main.py       ← orchestration: scrape → filter → save → notify
│   ├── scraper.py    ← Yad2 HTML scraper עם pagination (עד 15 עמודים)
│   ├── db.py         ← Supabase: filter_new + save_listings
│   ├── telegram.py   ← שליחת הודעות עם rate limit handling
│   └── config.py     ← validates env vars בעת הפעלה
├── supabase/schema.sql
├── requirements.txt
└── .env.example
```

## GitHub Secrets הנדרשים
| Secret | תיאור |
|--------|--------|
| `SUPABASE_URL` | `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | JWT מסוג service_role (לא anon, לא sb_secret_) |
| `TELEGRAM_BOT_TOKEN` | טוקן מ-@BotFather |
| `TELEGRAM_CHAT_ID` | מזהה הצ'אט/ערוץ |
| `YAD2_SEARCH_URL` | URL חיפוש מיד2 (עם כל הפילטרים) |

## איך הסקרייפר עובד
- יד2 הוא Next.js — כל דף HTML מכיל `<script id="__NEXT_DATA__">` עם כל ה-data
- המודעות נמצאות בתוך React Query dehydrated state תחת keys: `private`, `platinum`, `boost`, `solo`
- ה-URL של כל מודעה: `https://www.yad2.co.il/item/{token}` (השדה `token` מתוך הdata)
- ה-ID הייחודי לDB: שדה `orderId`
- pagination: מוסיף `&page=N` ל-URL, עוצר כשעמוד מחזיר 0 פריטים חדשים

## לוגיקת dedup
1. שולף את כל ה-IDs הקיימים מ-Supabase
2. מסנן — רק מודעות שה-`orderId` שלהן לא קיים ב-DB
3. שומר חדשים + שולח לטלגרם

## נקודות תשומת לב
- **Bot protection**: יד2 משתמשים ב-perfdrive.com להגנה. הסקרייפר שולח headers מלאים כמו Chrome. אם מחזיר redirect ל-perfdrive → `BotProtectionError`.
- **Telegram rate limit**: 429 מטופל עם retry + `sleep(0.35)` בין הודעות.
- **Supabase key**: חייב להיות `service_role` JWT (הפורמט הישן `eyJ...`). הפורמט החדש `sb_secret_` לא נתמך ב-supabase-py v2.4.4.
- **שעות פעילות**: cron רץ `0,30 5-21 * * *` UTC = 07:00–00:00 ישראל (קיץ/חורף).

## Supabase schema
```sql
create table if not exists listings (
  id            text        primary key,  -- orderId מיד2
  title         text,
  price         text,
  year          text,
  km            text,
  hand          text,
  location      text,
  url           text,                     -- https://www.yad2.co.il/item/{token}
  source        text        not null default 'yad2',
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now()
);
```

## src/scraper.py
```python
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
    if val is None:
        return ""
    if isinstance(val, dict):
        return str(val.get("text") or val.get("name") or val.get("value") or "")
    return str(val)


def _parse_listing(item: dict) -> "Listing | None":
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
    token = item.get("token") or listing_id
    url = f"https://www.yad2.co.il/item/{token}"

    return Listing(id=listing_id, title=title, price=price,
                   year=year, km=km, hand=hand, location=location, url=url)


LISTING_CATEGORY_KEYS = ("private", "platinum", "boost", "solo")


def _looks_like_listing(item: object) -> bool:
    return isinstance(item, dict) and bool(
        item.get("id") or item.get("orderId") or item.get("order_id")
    )


def _extract_from_react_query_data(data: dict) -> list[dict]:
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
    if depth > 12:
        return []
    if isinstance(obj, dict):
        if "queries" in obj:
            for q in (obj["queries"] or []):
                data = (q.get("state") or {}).get("data") or {}
                if not isinstance(data, dict):
                    continue
                logger.info("React Query data keys: %s", list(data.keys()))
                items = _extract_from_react_query_data(data)
                if items:
                    return items
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


MAX_PAGES = 15


def _fetch_page(params: dict) -> list[dict]:
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        response = client.get(SEARCH_BASE, params=params)
    if "perfdrive.com" in str(response.url) or "validate." in str(response.url):
        raise BotProtectionError(f"Bot protection triggered — {response.url}")
    if response.status_code != 200:
        response.raise_for_status()
    try:
        next_data = _extract_next_data(response.text)
    except ValueError:
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
        if new_on_page == 0:
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
```

## src/db.py
```python
"""Supabase persistence layer."""

import logging
from datetime import datetime, timezone

from supabase import create_client, Client
from src.scraper import Listing

logger = logging.getLogger(__name__)
TABLE = "listings"


def get_client(url: str, key: str) -> Client:
    return create_client(url, key)


def filter_new(client: Client, listings: list[Listing]) -> list[Listing]:
    if not listings:
        return []
    ids = [l.id for l in listings]
    response = client.table(TABLE).select("id").in_("id", ids).execute()
    existing_ids = {row["id"] for row in (response.data or [])}
    new = [l for l in listings if l.id not in existing_ids]
    logger.info("%d new out of %d total listings", len(new), len(listings))
    return new


def save_listings(client: Client, listings: list[Listing]) -> None:
    if not listings:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"id": l.id, "title": l.title, "price": l.price, "year": l.year,
         "km": l.km, "hand": l.hand, "location": l.location, "url": l.url,
         "first_seen_at": now, "last_seen_at": now}
        for l in listings
    ]
    client.table(TABLE).upsert(rows, on_conflict="id").execute()
    logger.info("Saved %d listings to DB", len(rows))
```

## src/telegram.py
```python
"""Telegram notification sender."""

import logging
import time

import httpx
from src.scraper import Listing

logger = logging.getLogger(__name__)
SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
INTER_MESSAGE_DELAY = 0.35


def _format_message(listing: Listing) -> str:
    lines = [f"🚗 *{listing.title}*"]
    if listing.price:
        lines.append(f"💰 {listing.price} ₪")
    details = []
    if listing.year:
        details.append(f"שנה: {listing.year}")
    if listing.km:
        details.append(f"ק\"מ: {listing.km}")
    if listing.hand:
        details.append(f"יד: {listing.hand}")
    if details:
        lines.append(" | ".join(details))
    if listing.location:
        lines.append(f"📍 {listing.location}")
    lines.append(f"[לפרסום ביד2]({listing.url})")
    return "\n".join(lines)


def send_listings(bot_token: str, chat_id: str, listings: list[Listing]) -> None:
    if not listings:
        return
    url = SEND_URL.format(token=bot_token)
    with httpx.Client(timeout=15) as client:
        for listing in listings:
            text = _format_message(listing)
            payload = {"chat_id": chat_id, "text": text,
                       "parse_mode": "Markdown", "disable_web_page_preview": False}
            response = client.post(url, json=payload)
            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                logger.warning("Telegram rate limit — sleeping %ss", retry_after)
                time.sleep(retry_after)
                response = client.post(url, json=payload)
            if response.status_code != 200:
                logger.error("Telegram error for listing %s: %s %s",
                             listing.id, response.status_code, response.text[:200])
            else:
                logger.info("Sent listing %s to Telegram", listing.id)
            time.sleep(INTER_MESSAGE_DELAY)
```

## src/main.py
```python
"""Entry point: scrape → filter new → save → notify."""

import logging
import sys

import src.config as cfg
from src.scraper import scrape_listings
from src.db import get_client, filter_new, save_listings
from src.telegram import send_listings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Starting Yad2 car alert check")
    listings = scrape_listings(cfg.YAD2_SEARCH_URL)
    if not listings:
        logger.info("No listings returned from scraper – nothing to do")
        return
    db = get_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
    new_listings = filter_new(db, listings)
    if new_listings:
        save_listings(db, new_listings)
        send_listings(cfg.TELEGRAM_BOT_TOKEN, cfg.TELEGRAM_CHAT_ID, new_listings)
    else:
        logger.info("No new listings found")
    logger.info("Done")


if __name__ == "__main__":
    main()
```

## src/config.py
```python
import os


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Missing required environment variable: {key}")
    return value


SUPABASE_URL: str = _require("SUPABASE_URL")
SUPABASE_KEY: str = _require("SUPABASE_SERVICE_ROLE_KEY")
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _require("TELEGRAM_CHAT_ID")
YAD2_SEARCH_URL: str = _require("YAD2_SEARCH_URL")
```

## .github/workflows/check-yad2.yml
```yaml
name: Check Yad2 Listings

on:
  schedule:
    # Every 30 min, 05:00–21:30 UTC = ~07:00–00:00 Israel time (UTC+2/+3)
    - cron: "0,30 5-21 * * *"
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run scraper
        run: python -m src.main
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          YAD2_SEARCH_URL: ${{ secrets.YAD2_SEARCH_URL }}
```

## requirements.txt
```
httpx==0.27.0
supabase==2.4.4
python-dotenv==1.0.1
```

## GitHub repo
https://github.com/OrenMordechay02/yad2-car-alert-bot-OM
