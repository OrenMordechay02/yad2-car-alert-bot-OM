# Yad2 Car Alert Bot

Sends a Telegram message for every new car listing that matches your Yad2 search URL.
Runs every 30 minutes via GitHub Actions. No server needed.

## Flow

```
GitHub Actions (cron)
  → src/scraper.py  — fetch Yad2 API
  → src/db.py       — compare against Supabase
  → src/telegram.py — send new listings
```

## Setup

### 1. Supabase

1. Create a free project at [supabase.com](https://supabase.com).
2. Open the SQL editor and run `supabase/schema.sql`.
3. Copy your **Project URL** and **service_role** key from *Settings → API*.

### 2. Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`.
2. Copy the **bot token**.
3. Start a conversation with the bot and send any message.
4. Get your **chat ID**:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

### 3. GitHub Secrets

In your repo → *Settings → Secrets and variables → Actions*, add:

| Secret | Value |
|--------|-------|
| `SUPABASE_URL` | `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | `eyJ...` |
| `TELEGRAM_BOT_TOKEN` | `123456:AAF...` |
| `TELEGRAM_CHAT_ID` | `123456789` |
| `YAD2_SEARCH_URL` | your Yad2 search URL |

### 4. Push and enable

Push to GitHub. The workflow runs every 30 minutes automatically.
You can also trigger it manually from *Actions → Check Yad2 Listings → Run workflow*.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
python -m src.main
```

## Notes

- The scraper calls Yad2's internal JSON API directly (no browser needed).
- If Yad2 blocks the API or changes its structure, swap `src/scraper.py` for a
  Playwright-based implementation — the `scrape_listings(url) → list[Listing]`
  interface stays the same.
- Duplicate suppression is handled by Supabase's primary key — safe to re-run.
