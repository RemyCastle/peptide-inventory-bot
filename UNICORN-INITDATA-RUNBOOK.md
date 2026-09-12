# Unicorn Mini App `initData` checkout runbook

**Bot / API:** https://unicornfartzz-bot.onrender.com (poller: `@UnicornMagicFactory2Bot`)
**Mini App (Cloudflare Pages, not this repo):** https://remy-miniapp-demos.pages.dev/unicorn/
**Never:** wipe `/data/inventory.db` on Render. Laptop `inventory.db` is not the live catalog.

This runbook is the reference for **why Mini App checkout can fail** and **how to
fix it** without touching stock or the DB. Checkout is the one place where the
storefront stops being a read-only view: `POST /order` authenticates the buyer
from Telegram `initData` before it calls `create_order`. Everything below is
about that auth boundary and the payment rails the buyer sees afterward.

Related notes: [`VENDOR-ONBOARDING.md`](VENDOR-ONBOARDING.md) (full add-a-vendor
recipe), [`UNICORN-MINIAPP-PARITY.md`](UNICORN-MINIAPP-PARITY.md) (Mini App vs
bot surface parity).

---

## The checkout auth flow (one pass)

1. Buyer opens the store from a Telegram bot. Telegram injects
   `Telegram.WebApp.initData` — a signed query string
   (`user=…&auth_date=…&hash=…`) that proves which Telegram user opened which
   bot's Web App.
2. The Pages store loads the catalog from
   `GET /storefront?invite=<24hex>` (public, read-only) and, on checkout, POSTs
   the cart + `initData` to `POST /order`.
3. `spbc_notify.api_order` (`spbc_notify.py:1010`):
   - resolves `invite → shop_chat_id`;
   - gathers **every** bot token bound to that shop via
     `vendor_stores.get_bot_tokens_for_shop` (`vendor_stores.py:1145`) —
     primary polling bot **plus** `extra_tokens` HMAC aliases;
   - validates `initData` against those tokens with
     `validate_webapp_init_data_any` (`vendor_stores.py:1201`);
   - on success, calls `db.create_order` **exactly once** and returns the
     payment code, payment methods, and shipping total.

### HMAC algorithm (what "signed" means)

`validate_webapp_init_data` (`vendor_stores.py:263`), per Telegram spec:

```
secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token)
expected   = HMAC_SHA256(key=secret_key, msg=data_check_string)   # hex
data_check_string = "\n".join(sorted "k=v" pairs, excluding hash)
```

- Compared with `hmac.compare_digest` (constant-time).
- Then a **24 h replay guard**: `auth_date` must be within
  `INIT_DATA_MAX_AGE_SEC` (24 h) and not more than 60 s in the future
  (`vendor_stores.py:76`, `:323`).
- **The token that signs `initData` is the bot the buyer opened the store
  from — not necessarily the polling bot.** That is the whole reason
  `extra_tokens` exists (see next section).

### Manually verify an `initData` string (offline, no DB)

When a buyer reports `bad_hash` and you need to know *which* token (if any)
signed their session, verify it offline — this never touches stock or the DB
and answers "wrong bot vs. empty/tampered initData" definitively.

**Grab the raw string** from the buyer's client: in the open Mini App, run
`Telegram.WebApp.initData` in the devtools console (desktop Telegram → right-
click → Inspect), or read it from the server log line — `api_order` logs
`initData_len=…` but never the value itself. It looks like
`user=%7B…%7D&chat_instance=…&auth_date=1700000000&hash=abcd…`.

**Reproduce the exact server check** (`validate_webapp_init_data`,
`vendor_stores.py:263`). Run each candidate token — primary + every
`extra_tokens`/alias — through it:

```python
import hmac, hashlib
from urllib.parse import parse_qsl

def check(init_data: str, bot_token: str) -> bool:
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    exp = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(exp, got)
```

- `True` for some token → that bot signed it. If the server still says
  `bad_hash`, that token isn't in the shop's set → add it to `extra_tokens`.
- `True` for **none** → the string is empty, truncated, or re-encoded (the
  trailing-slash gotcha strips the fragment on iOS; a copy/paste can mangle the
  `%`-encoding). `hash` matching but `auth_date` old is a separate `expired`, not
  `bad_hash`.
- Use the **exact** query string — do not re-URL-decode `user`; the check is
  over the raw `k=v` pairs, `hash` excluded, sorted lexicographically.

---

## Multi-bot signing: `extra_tokens` / `UNICORN_EXTRA_BOT_TOKENS`

The live poller is `@UnicornMagicFactory2Bot`, but some buyers still open the
store from the older `@UnicornMagicFactoryBot`. Telegram signs `initData` with
**whichever bot they opened**, so the server must accept **both** tokens for the
one Unicorn catalog shop.

- `get_bot_tokens_for_shop` returns primary + `extra_tokens` + the always-on
  Unicorn HMAC aliases (`_unicorn_env_hmac_tokens`, `vendor_stores.py:1112`).
- These are **HMAC aliases only** — they do **not** start a second poller and
  are **never** used to send DMs (`get_bot_token_for_shop`,
  `vendor_stores.py:1188`). Order DMs always go out on the primary/live token.
- `TELEGRAM_BOT_TOKEN` and `BOT_TOKENS` are **always** aliases for the Unicorn
  catalog shop regardless of config.

**Where to set the extra token:**

| Place | How |
|-------|-----|
| Render env (legacy Unicorn) | `UNICORN_EXTRA_BOT_TOKENS` = comma-separated tokens |
| `VENDOR_STORES_JSON` entry | `"extra_tokens": ["123456:ABC…"]` on the shop object |

Put `@UnicornMagicFactoryBot`'s token in `extra_tokens` when the JSON `token`
is `@MagicFactory2Bot`. To actually **poll both** bots (receive orders sent via
Telegram `web_app_data` on either), add a **second** `VENDOR_STORES_JSON` entry
with its own `token` and the **same** `shop_chat_id`.

---

## The trailing-slash gotcha (top cause of "empty initData")

Cloudflare Pages **308-redirects** `/unicorn` → `/unicorn/`. On iOS WKWebView
that redirect **strips the URL fragment** (`#tgWebAppData=…`), so
`Telegram.WebApp.initData` comes back **empty** and every checkout fails with
`bad_hash` (empty initData → no signer).

Fix already in the code — always hand Telegram the trailing-slash URL:

- `_normalize_store_url` (`vendor_stores.py:84`) force-appends `/` to any
  `…/unicorn` Web App / Menu Button URL.
- `cache_bust_store_url` (`vendor_stores.py:104`) appends `?v=<STORE_URL_CACHE_BUST>`
  so Telegram fetches fresh HTML after a Pages deploy. Bump
  `STORE_URL_CACHE_BUST` (`vendor_stores.py:80`) when the Pages checkout JS
  changes.

**Manual checks:**
- BotFather → Bot Settings → Menu Button → URL must end in `/unicorn/`
  (trailing slash), and the `store_url` in `VENDOR_STORES_JSON` /
  `UNICORN_STORE_URL` must too.
- If a buyer reports a blank/looping checkout after a Pages deploy, bump the
  cache-bust version and re-open the bot.

---

## `POST /order` error codes → what to do

The endpoint returns `{"ok": false, "error": <code>}` with an HTTP status.
Map from `initdata_error_code` (`vendor_stores.py:138`) and `api_order`:

| HTTP | `error` | Meaning | Fix |
|------|---------|---------|-----|
| 404 | `unknown storefront` | `invite` did not resolve to a shop | Confirm the Pages store's `?invite=` matches the bound 24-hex; re-check `_bind_vendor_miniapps` ran (boot log) |
| 401 | `no_vendor_token` | No bot token is bound to that shop | Set `token` in `VENDOR_STORES_JSON` or legacy `UNICORN_BOT_TOKEN`; restart |
| 401 | `bad_hash` | HMAC failed for **every** candidate token | Buyer's bot token isn't in the shop's token set → add it to `extra_tokens`; **or** `initData` was empty (trailing-slash gotcha above) |
| 401 | `expired` | HMAC matched but `auth_date` > 24 h old | Buyer sat on the checkout page > 24 h — tell them to re-open the store; not a config bug |
| 400 | `bad payload` / `empty cart` | Cart failed to parse or was empty | Client bug; check the Pages checkout JS |
| 409 | stock error | `create_order` rejected (sold out / stock race) | Expected when stock ran out mid-checkout; server re-checks authoritatively |

**Why `bad_hash` vs `expired` is trustworthy:** once the HMAC matches a token,
later field errors (expired, missing user) carry `hash_ok=True`
(`vendor_stores.py:132`), and `validate_webapp_init_data_any` stops trying
other tokens (`vendor_stores.py:1224`) so it never masks the real reason with
"bad hash". So `expired` genuinely means "signed but stale," not "wrong bot."

---

## Diagnosing a live checkout failure (order of operations)

1. **`GET /health`** — confirm `ok: true` and note `git_sha`. Never edit the
   DB from a laptop while cloud is live.
2. **Reproduce and read the log line.** `api_order` logs
   `POST /order invite=… initData_len=… items=…` then either
   `auth ok shop=…` or `initData rejected shop=… reason=… error=…`
   (`spbc_notify.py:1031`, `:1062`). The `reason` is the internal message
   (never a secret); the `error` is the code the buyer got.
   - `initData_len=0` → **empty initData** → trailing-slash gotcha.
   - `reason=bad hash`, non-zero len → **wrong/missing token** → `extra_tokens`.
   - `reason=expired auth_date` → buyer's page is stale; re-open.
3. **Confirm the token set.** In the boot log, `unicorn storefront bind`
   shows the shop; `get_bot_tokens_for_shop` should include the bot the buyer
   used. If not, add it to `extra_tokens` and restart.
4. **Confirm the invite bind.** `GET /storefront?invite=<hex>` must return the
   Unicorn catalog (not the SPBC main catalog). If it's wrong,
   `_bind_vendor_miniapps` rebinds on restart.

---

## Payment methods setup

After auth passes and the order is created, the buyer needs to actually pay.
`POST /order` returns a `payments` array (`spbc_notify.py:1212`,
`vendor_stores.payment_display_lines` at `vendor_stores.py:433`) that the Mini
App pay screen renders. **If no active methods exist, that array is empty and
the buyer has no way to pay.** Seed at least one.

Payment rows live in the `payment_methods` table, scoped per shop
(`db.py:316`). Typed rails come from `payment_templates.py` (`METHOD_TYPES`:
`cashapp`, `venmo`, `paypal`, `zelle`, `apple_cash`, `crypto`, `custom`). Every
row carries a `method_type` so it can be edited structurally and deduped on
seed.

There are **three** ways to add methods — all write the same table.

### A. Boot seed (Unicorn only, idempotent)

On Unicorn boot, `run_cloud.py:153` calls
`webpanel.ensure_shop_payments` with defaults:

```python
[
  {"method_type": "venmo",  "handle": "@wineboos"},
  {"method_type": "paypal", "handle": "unicornfartzz@proton.me",
   "network_note": "friends_family"},
]
```

- **Idempotent by `method_type`**: `ensure_shop_payments`
  (`webpanel.py:1429`) only inserts a type that isn't already present. It reads
  the shop's rows with `active_only=False` (`webpanel.py:1437`), so a row of that
  type blocks a re-seed **even when it is paused/inactive**. An existing
  Venmo/PayPal row (edited or paused by the vendor) is **left alone**.
- **The delete-then-reboot trap.** Because the seed keys off "type present at
  all," **deleting** a seeded Venmo/PayPal row (`🗑` in the wizard, or a panel
  delete) means it is **absent** on the next boot and gets **re-created** with
  the `run_cloud.py:155` default handle. To make a default go away and *stay*
  gone, **pause it** (`⏸`, sets `active=0`) instead of deleting — the paused row
  still counts as present and is never shown to buyers. Deleting only sticks if
  you also drop it from the `run_cloud.py:155` list (a redeploy).
- To change the seeded defaults, edit the list at `run_cloud.py:155`. To add a
  brand-new rail permanently, prefer the Telegram/panel path below so you don't
  redeploy for a handle change.

### B. Telegram admin wizard (fastest for the vendor)

Owner/admin of the shop, in the bot:

1. `/webpanel`-adjacent admin menu → **💳 Payments** button
   (`callback_data="adm_pays"`, rendered by `cb_adm_pays` at `bot.py:5418`).
2. **Quick add** buttons: `➕ Cash App`, `➕ Venmo`, `➕ Crypto`, `➕ Zelle`,
   `➕ Custom`, `➕ Freeform`.
3. Typed quick-adds ask the structured prompts from
   `payment_templates.template_prompts` (`payment_templates.py:181`):
   - Cash App → `$Cashtag`
   - Venmo → `@handle`
   - PayPal → email or `@username` (seeded as **friends & family**)
   - Apple Cash → phone number
   - Zelle → email or phone
   - Crypto → coin, wallet address, optional network note (`-` to skip)
4. **Freeform** (`adm_addpay`) collects a name + multi-line instructions and
   stores a `custom` row (`bot.py:5603`).
5. In the Payments list, `⏸/▶️` toggles active (`togglem:`), `🗑` deletes
   (`delm:`). **Only active rows appear in `/storefront` and on the pay
   screen** (`list_payment_methods(..., active_only=True)`).

### C. Web panel / API (structured, no redeploy)

If `PANEL_BASE_URL` is set, `/webpanel` issues a 3-day admin link
(`bot.py:6526`). The panel's Payments tab POSTs typed fields that route through
`payment_templates.render_from_fields` (`payment_templates.py:228`) and
`db.add_payment_from_template`. Fields accepted: `method_type`, `handle`,
`address`, `chain`, `network_note`, `cashtag`, `name`, `instructions`.

**Which field each `method_type` actually reads** (`render_from_fields`,
`payment_templates.py:240`) — send the primary field; the rest are ignored:

| `method_type` | Primary field(s) | Notes |
|---------------|------------------|-------|
| `cashapp`     | `cashtag` (or `handle`) | `$` auto-prepended |
| `venmo`       | `handle` (or `cashtag`) | `@` auto-prepended |
| `paypal`      | `handle` (or `address`) | `network_note` sets the label (see below) |
| `zelle`       | `handle` (or `address`) | email **or** phone |
| `apple_cash`  | `handle` (or `address`) | phone number |
| `crypto`      | `chain` (coin) + `address` | `network_note` shown as "Network: …" |
| `custom`      | `instructions` + `name`    | free text, rendered verbatim |

`network_note = "friends_family"` (default) vs `"goods_services"` controls the
PayPal label (`payment_templates.py:142`); for PayPal it accepts the fuzzy
truthy set `ff/friends/1/true/yes/""` → friends & family
(`payment_templates.py:246`). For crypto it is a free-text network hint.

All payment writes go to **one** endpoint: `POST /panel/api/payment`
(`api_payment`, `webpanel.py:1363`; routed from `spbc_notify` /panel*). Auth is
the magic-link token in the query string (`?t=<issued token>`) — the handler
derives `chat_id` from the token, so a link only ever edits its own shop.

**Add / update / delete on the one endpoint:**
- **Add** — POST with **no `id`** and a `name` (`webpanel.py:1385`). Structured
  types are validated: `crypto` needs an `address`; `venmo/paypal/zelle/
  apple_cash/cashapp` need a `handle`/`cashtag` (`webpanel.py:1390`), else `400`.
- **Update** — POST with an existing `id`; it updates in place. A partial update
  carrying only `name`/`instructions`/`active` (no structured fields) is allowed
  and will **not** wipe the stored `handle`/`address` (`webpanel.py:1411`) — use
  it to just rename or pause a row. Set `active` to `0`/`1` to pause/enable;
  buyers only ever see `active = 1` rows.
- **Delete** — POST `{"id": <id>, "delete": true}` (`webpanel.py:1375`). An `id`
  from another shop returns `404` (`webpanel.py:1372`). Remember the
  delete-then-reboot trap above: for seeded types, **pause instead of delete**.

**Reorder the pay screen** by setting `sort_order` — the buyer list is
`ORDER BY sort_order, name` (`db.py:2025`). Lower `sort_order` shows first; ties
break alphabetically by `name`.

Example — add a Cash App rail without a redeploy (`$T` = the `t=` token from the
`/webpanel` admin link):

```bash
curl -sS -X POST "https://unicornfartzz-bot.onrender.com/panel/api/payment?t=$T" \
  -H "Content-Type: application/json" \
  -d '{"name":"Cash App","method_type":"cashapp","cashtag":"unicornfartzz","active":1}'
# → {"ok": true, "id": <n>, "created": true}
```

### Verify the buyer will see them

```bash
curl -sS "https://unicornfartzz-bot.onrender.com/storefront?invite=YOUR_HEX" | \
  python -c "import sys,json; print(json.load(sys.stdin).get('payments'))"
```

Expect a non-empty list of `"Name: instructions"` lines. Empty list → seed a
method (any path above). Do **not** reset the DB to "fix" an empty pay screen.

### Notes / gotchas

- **Payments are per-shop.** Seeding the SPBC main shop does not give the
  Unicorn shop rails, and vice versa.
- **Legacy instructions-only rows** (no `method_type`) still render; the code
  infers the kind and pay target from the name/instructions
  (`_method_kind_and_target`, `vendor_stores.py:451`).
- **Never commit real handles/addresses as secrets** — but note these payment
  handles are buyer-facing by design and live in the DB, not `.env`.

---

## Do / Don't

**Do**
- Add a buyer's bot token to `extra_tokens` when they open the store from a
  different bot than the poller.
- Keep every Menu Button / `store_url` trailing-slashed (`…/unicorn/`).
- Bump `STORE_URL_CACHE_BUST` after a Pages checkout-JS deploy.
- Seed ≥1 active payment method per selling shop.
- **Pause** (`⏸`) a seeded payment default you don't want, rather than deleting
  it — a deleted seeded type is re-created on the next boot.
- Read the `POST /order` log line before changing config.

**Don't**
- Wipe or reset `/data/inventory.db` to "fix" checkout or empty payments.
- Run local `start.bat` / tests against the production DB while cloud is live
  (`DB_PATH` must point at a scratch file for tests).
- Assume `bad_hash` means the buyer is malicious — it's almost always a
  missing `extra_tokens` alias or the empty-initData redirect.
- Add `extra_tokens` expecting a second poller — they are HMAC aliases only.

---

## Quick reference (file:line)

| Piece | Location |
|-------|----------|
| `POST /order` handler | `spbc_notify.py:1010` |
| HMAC validate (one token) | `vendor_stores.py:263` |
| HMAC validate (any bound token) | `vendor_stores.py:1201` |
| Error code map | `vendor_stores.py:138` |
| Token set for a shop | `vendor_stores.py:1145` |
| Unicorn HMAC aliases | `vendor_stores.py:1112` |
| Trailing-slash normalize | `vendor_stores.py:84` |
| Cache-bust store URL | `vendor_stores.py:104` |
| Payment templates | `payment_templates.py` |
| Field → template routing | `payment_templates.py:228` |
| Idempotent payment seed | `webpanel.py:1429` |
| Unicorn boot payment seed | `run_cloud.py:153` |
| Panel payment write API (add/update/delete) | `webpanel.py:1363` |
| Buyer-visible list (active-only, sorted) | `db.py:2025` |
| Telegram payments menu | `bot.py:5418` |
| `payments` in storefront/order JSON | `vendor_stores.py:433` |
