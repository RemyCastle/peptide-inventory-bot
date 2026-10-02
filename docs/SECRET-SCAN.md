# Secret scan — peptide-inventory-bot

Date: 2026-10-02. Report only. Git history was not rewritten, and no tokens were changed or revoked from this scan.

This repository is a reference copy. The live Unicorn bot runs on Cloudflare from a different codebase. Do not deploy this repo to rotate a secret. Set new values on the host that actually runs the bot, then revoke the old value in the provider.

## How it was scanned

- `gitleaks` 8.28.0, `gitleaks detect --source . --log-opts=--all`. It scanned 178 commits and about 2.19 MB. Default rules reported **0 leaks**.
- A separate pass over `git log -p --all` looked for Telegram bot-token shape, 24-hex catalog keys, 32-hex API hashes next to secret-like words, private keys, AWS access keys, GitHub PATs, Slack tokens, and Stripe live keys. Assignments to known env names were classified as empty, placeholder, or a real-looking value.
- No `.env` file appears in history. Only `.env.example` files were committed.

Gitleaks' default rules do not flag a 24-hex catalog invite or a low-entropy test token, so the manual pass is the one that matters.

## Findings

Values below are masked. Do not reconstruct them into a new commit.

| What | Masked value | Where (current tree and history) | Treat as |
|---|---|---|---|
| Pages Mini App catalog invite | `dd6d…8657` (24 hex) | `unicorn_shop.py`, `.env.example`, and `.ai/decisions.md`, first committed 2026-09-11 (sample commits `16c65477e3`, `2df34d33e4`, `31c32c5e45`, `5229585f43`) | Live catalog key. This change removes it from code and `.env.example`. It is still in git history and in the 2026-09-11 decision log. |
| Telegram-token-shaped test fixture | id `1234…6789`, secret `AAHd…Dsaw` (sha256 prefix `a86ae86369fd`) | `tests/test_catalog_shop_orders.py`, `tests/test_order_http.py`, `tests/test_unicorn_spbc_cut.py` | Same shape as a BotFather token. Committed as a fixture, not as `.env`. Confirm it was never a live bot. |
| Synthetic test token | id `9999…9999` | `tests/test_order_http.py` | Obvious test value. |
| Synthetic test token | id `9990…0111`, secret ending `est` | `tests/test_panel_orders.py` | Obvious test value. |
| Venmo handle hardcoded as a payment default | `@win…oos` | Was the Unicorn seed in `unicorn_shop.py`. Still in older docs' history and in test fixtures that pass a handle string. | Payment destination, not an API token. Code no longer defaults to it. |
| PayPal address hardcoded as a payment default | `uni…@proton.me` | Same as the Venmo handle. | Same. |
| Platform-fee Venmo default | `@rem…tle` | `config.py` and `franchise.py` used this when `MASTER_VENMO` was unset. Decision log mentions it. | Same. Code now leaves the handle empty until the env var is set. |

Not found: committed `.env`, private keys, AWS keys, GitHub PATs, Slack tokens, Stripe live keys, or a filled-in `TG_API_HASH` / `NOTIFY_SECRET` / `BACKUP_PASSPHRASE` / `TELEGRAM_BOT_TOKEN`. Those names exist only as `os.getenv(...)` reads or empty placeholders.

## What this change did

- `pages_storefront_key()` reads `UNICORN_STOREFRONT_KEY` only. A missing or malformed value does not fall back to the old invite.
- Unicorn Venmo/PayPal seed reads `UNICORN_VENMO_HANDLE` and `UNICORN_PAYPAL_EMAIL`.
- `MASTER_VENMO` has no built-in handle.
- `.env.example` lists names and does not contain the catalog invite.

## Rotations to do yourself

1. **Catalog invite (`dd6d…8657`).** If that 24-hex value still opens the Mini App catalog, generate a new invite, put it in the host secret `UNICORN_STOREFRONT_KEY`, and update the Pages app that sends `?invite=`. The old value stays in git history, so treat it as public. Do not rewrite history to delete it.
2. **Bot token fixture (`1234…6789` / `AAHd…Dsaw`).** In BotFather, check whether that token was ever issued. If it was, revoke it and set the new token only in the host environment. If it was only a copy-pasted example, no rotation is required.
3. **Payment handles.** These are not API credentials. Leave the Venmo and PayPal accounts as they are unless you want new public handles. Set `UNICORN_VENMO_HANDLE`, `UNICORN_PAYPAL_EMAIL`, and `MASTER_VENMO` on the host. Do not commit the values.
4. **Anything you set in Render, Cloudflare, or Telegram** for this old Python bot is outside this scan. The live Unicorn process is the Cloudflare codebase, which is not in this repository, so this scan cannot see those secrets.
