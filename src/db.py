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
    """Return only listings whose id is not already in the DB."""
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
        {
            "id": l.id,
            "title": l.title,
            "price": l.price,
            "year": l.year,
            "km": l.km,
            "hand": l.hand,
            "location": l.location,
            "url": l.url,
            "first_seen_at": now,
            "last_seen_at": now,
        }
        for l in listings
    ]

    client.table(TABLE).upsert(rows, on_conflict="id").execute()
    logger.info("Saved %d listings to DB", len(rows))
