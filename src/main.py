"""Entry point: scrape → filter new → save → notify."""

import logging
import sys

import src.config as cfg
from src.scraper import scrape_listings
from src.db import get_client, filter_new, save_listings
from src.telegram import send_listings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Starting Yad2 car alert check (%d search URLs)", len(cfg.YAD2_SEARCH_URLS))

    db = get_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)

    all_listings = []
    for url in cfg.YAD2_SEARCH_URLS:
        listings = scrape_listings(url)
        logger.info("URL %s → %d listings", url[:60], len(listings))
        all_listings.extend(listings)

    if not all_listings:
        logger.info("No listings returned from scraper – nothing to do")
        return

    new_listings = filter_new(db, all_listings)

    if new_listings:
        save_listings(db, new_listings)
        send_listings(cfg.TELEGRAM_BOT_TOKEN, cfg.TELEGRAM_CHAT_ID, new_listings)
    else:
        logger.info("No new listings found")

    logger.info("Done")


if __name__ == "__main__":
    main()
