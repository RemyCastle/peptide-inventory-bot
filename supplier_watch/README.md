# Supplier Watch

Watches whitelisted supplier chats in **your personal Telegram** (Telethon user
session — bots can't see supplier DMs), extracts prices with **local Ollama**
(regex fallback), stores history in its own `supplier_watch.db`, and alerts your
**Saved Messages** on new products and price changes.

Completely separate from the shop bot: own process, own DB, own session.
It never touches `inventory.db` or the bot token.

## One-time setup

1. Get API credentials (2 min): https://my.telegram.org → *API development
   tools* → create an app → copy `api_id` and `api_hash`.
2. `copy .env.example .env` in this folder and fill in `TG_API_ID` / `TG_API_HASH`.
3. Install deps: `pip install -r supplier_watch/requirements.txt`
4. Pick your supplier chats:
   ```
   python -m supplier_watch.list_chats
   ```
   First run asks for your phone number + the login code Telegram sends you.
   Copy the chat_ids you want into `supplier_watch/suppliers.json`
   (see `suppliers.json.example`).
5. Start watching:
   ```
   .\supplier_watch\start-watcher.ps1
   ```

## Alerts (Saved Messages)

```
📦 Supplier X
🆕 BPC-157 5mg — $45
📉 Tirzepatide 30mg — $120 → $110 (-8.3%)
```

On-demand cheapest-source digest: `python -m supplier_watch.digest --send`

## Notes

- Session file lives in `%LOCALAPPDATA%\supplier_watch\` — treat it like a
  password; never sync/commit it. Only this process may use it.
- Passive listener only (no history scraping) — safe for account health.
- Parsing is local-only (Ollama on 127.0.0.1); supplier data never leaves the PC.
- `SW_PARSE_MODE=regex_only` in `.env` runs without Ollama entirely.
- Tests: `python -m supplier_watch.test_parser` (add `--llm` to test Ollama).
