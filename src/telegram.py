"""Telegram notification sender."""

import logging
import time

import httpx

from src.scraper import Listing

logger = logging.getLogger(__name__)

SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
INTER_MESSAGE_DELAY = 0.35  # seconds — stays under Telegram's 30 msg/sec limit


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
        logger.info("No new listings to send")
        return

    url = SEND_URL.format(token=bot_token)
    with httpx.Client(timeout=15) as client:
        for listing in listings:
            text = _format_message(listing)
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            }
            response = client.post(url, json=payload)
            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                logger.warning("Telegram rate limit — sleeping %ss", retry_after)
                time.sleep(retry_after)
                response = client.post(url, json=payload)

            if response.status_code != 200:
                logger.error(
                    "Telegram error for listing %s: %s %s",
                    listing.id,
                    response.status_code,
                    response.text[:200],
                )
            else:
                logger.info("Sent listing %s to Telegram", listing.id)

            time.sleep(INTER_MESSAGE_DELAY)
