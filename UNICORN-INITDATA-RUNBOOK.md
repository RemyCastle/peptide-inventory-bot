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
     payment code, order total, and the rendered payment methods.

### The `POST /order` success response (what the pay screen reads)

On a 200, `api_order` (`spbc_notify.py:1226`) returns:

```json
{
  "ok": true,
  "code": "UMF-AB12CD",
  "total": 149.00,
  "payments": ["Venmo: 💙 *Venmo*\nSend payment to: `@wineboos`…", …],
  "payment_methods": [
    {"name": "Venmo", "method_type": "venmo", "target": "@wineboos",
     "pay_url": "https://account.venmo.com/pay?…", "line": "Venmo: …"}
  ],
  "message": "…buyer-facing confirmation text…",
  "can_mark_paid": true,
  "mark_paid_hint": "Tap I've paid after you send the money…",
  "invoice_offered": false
}
```

- `code` is the buyer's payment reference — it must ride along on the payment
  (Venmo prefills it as the note; every template's instructions say "include
  your order number"). It's how you reconcile a payment to an order.
- `payments` is the manual-rails array (see [Payment methods setup](#payment-methods-setup));
  empty array → the buyer has no manual rail to pay on.
- `invoice_offered` is `true` **only** when a native Telegram card invoice was
  DM'd to the buyer — i.e. `TELEGRAM_PAYMENT_PROVIDER_TOKEN` is set (see
  [Section D](#d-native-telegram-card-checkout-telegram_payment_provider_token)).
  It's `false` on every deployment that hasn't wired a card provider, which is
  normal — manual rails still carry checkout.

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

### Sign a *test* `initData` to exercise the auth boundary (QA)

To prove `POST /order` accepts a well-formed session without borrowing a real
buyer's device, sign your own `initData` with a token that **is** in the shop's
set (primary or an `extra_tokens` alias). The signing is the inverse of the
check above:

```python
import hmac, hashlib, json, time
from urllib.parse import urlencode

def sign(bot_token: str, user: dict, auth_date: int | None = None) -> str:
    auth_date = auth_date or int(time.time())              # within 24 h, not future
    fields = {"auth_date": str(auth_date),
              "user": json.dumps(user, separators=(",", ":"))}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)   # ready to drop into POST /order as "initData"
```

- **Two warnings.** (1) A *valid* `initData` + a *valid* cart calls
  `create_order` for real — it **decrements stock and DMs the shop owner**. To
  test only the auth layer, send an empty/sold-out cart and expect the `409`
  stock path, or a `400 empty cart`, *after* auth passes — not a `bad_hash`.
  (2) `user` must be a JSON object with a numeric `id` (else the token matches
  but you get `reason=bad user …` → still surfaced as `bad_hash`; see the
  overloaded-`bad_hash` note below).
- Never sign with a token that isn't already in the shop's set — that only
  reproduces `bad_hash`, which you can confirm offline without a POST.

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
| 401 | `empty_initdata` | `initData` missing/empty or no hash | Trailing-slash gotcha, or opened outside Telegram — buyer should re-open from the bot |
| 401 | `bad_payload` | HMAC matched but `user` / `auth_date` is broken | Out-of-date Telegram app or incomplete WebView payload — re-open; **not** an `extra_tokens` fix |
| 401 | `bad_hash` | HMAC failed for **every** candidate token | Buyer's bot token isn't in the shop's token set → add it to `extra_tokens` |
| 401 | `expired` | HMAC matched but `auth_date` > 24 h old | Buyer sat on the checkout page > 24 h — tell them to re-open the store; not a config bug |
| 400 | `bad payload` / `empty cart` | Cart failed to parse or was empty | Client bug; check the Pages checkout JS |
| 409 | `no_payment_methods` | Shop has zero *active* payment rows | Seed/unpause a method (Admin → Payments or panel). Order is **not** created. |
| 409 | stock error | `create_order` rejected (sold out / stock race) | Expected when stock ran out mid-checkout; server re-checks authoritatively |
| 400 | `missing_code` | `POST /order-paid` with empty `code` | Buyer must send the receipt payment code |
| 403 | `not_your_order` | `POST /order-paid` initData user ≠ order buyer | Open from the same Telegram account that placed the order |
| 409 | `already_processed` | `POST /order-paid` on paid/cancelled/rejected | Already confirmed or closed — no status change |

401/409 JSON also includes `message` (buyer-facing, never a secret) and 401 includes `detail` (short `InitDataError.reason`). Mini App should alert `message`, not the raw `error` code. `GET /storefront` includes `message` + `invoices_enabled` (no handles). `GET /order-status` returns the same `payments` / `payment_methods` objects as `POST /order` (`pay_url` + `pay_hint` only while `needs_payment`) plus a status `message`, `can_mark_paid`, and `mark_paid_hint` so “check my order” can show pay links and I've paid. `POST /order-paid` uses the same initData HMAC as checkout and moves `pending_payment` → `awaiting_confirmation` (idempotent if already awaiting). Never confirms payment or deducts stock.

**Why `bad_hash` vs `expired` is trustworthy:** once the HMAC matches a token,
later field errors (expired, missing user) carry `hash_ok=True`
(`vendor_stores.py:132`), and `validate_webapp_init_data_any` stops trying
other tokens (`vendor_stores.py:1224`) so it never masks the real reason with
"bad hash". So `expired` genuinely means "signed but stale," not "wrong bot."

**`error` is no longer only `bad_hash` vs `expired`.** `initdata_error_code`
splits empty/missing initData → `empty_initdata`, signed-but-broken user
fields → `bad_payload`, and a genuine HMAC miss → `bad_hash`. `detail` is the
short `InitDataError.reason` (never a secret). Logs still print `reason=`.

The **log `reason`** still diagnoses trailing-slash vs wrong token:

- `reason=bad hash` → genuinely no candidate token matched → add the buyer's
  bot to `extra_tokens` (or it was the empty-initData redirect).
- `reason=missing user` / `bad user …` / `bad auth_date` → the token matched,
  but the client sent an incomplete `initData`. Usually an out-of-date Telegram
  app, or a plain browser opening the Pages URL directly (no Telegram to inject
  `user`). This is **not** a token-set fix — do not add `extra_tokens`.

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

On Unicorn boot, `run_cloud._seed_unicorn_payments` runs from
`_bind_unicorn_pages_storefront` (the Pages catalog shop) **even when
`UNICORN_CLAIM_TOKEN` is unset**, and again from the claim-bind path if that
env is set. Both call `webpanel.ensure_unicorn_shop_payments` with
`unicorn_shop.DEFAULT_PAYMENT_METHODS`:

```python
[
  {"method_type": "venmo",  "handle": "@wineboos"},
  {"method_type": "paypal", "handle": "unicornfartzz@proton.me",
   "network_note": "friends_family"},
]
```

- **Idempotent by `method_type`**: `ensure_shop_payments`
  only inserts a type that isn't already present. It reads
  the shop's rows with `active_only=False`, so a row of that
  type blocks a re-seed **even when it is paused/inactive**. An existing
  Venmo/PayPal row (edited or paused by the vendor) is **left alone**.
- **The delete-then-reboot trap.** Because the seed keys off "type present at
  all," **deleting** a seeded Venmo/PayPal row (`🗑` in the wizard, or a panel
  delete) means it is **absent** on the next boot and gets **re-created** with
  the default handle. To make a default go away and *stay*
  gone, **pause it** (`⏸`, sets `active=0`) instead of deleting — the paused row
  still counts as present and is never shown to buyers. Deleting only sticks if
  you also drop it from `unicorn_shop.DEFAULT_PAYMENT_METHODS` (a redeploy).
- To change the seeded defaults, edit `unicorn_shop.DEFAULT_PAYMENT_METHODS`. To add a
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

**The wizard always seeds PayPal as Friends & Family.** `render_from_answers`
(`payment_templates.py:213`) hard-codes `friends_and_family=True` for the
`paypal` quick-add — there is **no wizard prompt** for G&S. If the vendor needs
**Goods & Services** (buyer protection, but a fee), you must use the panel/API
path below with `network_note=goods_services`, or edit the row's stored
`network_note` there. The wizard also can't set `sort_order`; it appends to the
end. Both are panel/API-only knobs.

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

**Endpoint responses** (`api_payment`, `webpanel.py:1363`) — the `error` string
tells you which field the panel/curl call missed:

| Status | Body | Cause |
|--------|------|-------|
| 200 | `{"ok":true,"id":<n>,"created":true}` | added |
| 200 | `{"ok":true,"id":<n>,"created":false}` | updated in place |
| 200 | `{"ok":true,"deleted":true}` | deleted |
| 400 | `Name required` | add (no `id`) with no `name` (`webpanel.py:1386`) |
| 400 | `Crypto wallet address required` | `crypto` add with no `address` (`webpanel.py:1390`) |
| 400 | `Payment handle / number required` | venmo/paypal/zelle/apple_cash/cashapp add with no `handle`/`cashtag` (`webpanel.py:1392`) |
| 400 | `Bad payment id` | `id` isn't an integer (`webpanel.py:1370`) |
| 400 | `Payment id required` | `delete: true` with no `id` (`webpanel.py:1377`) |
| 400 | `Nothing to update` | update carried no recognized field (`webpanel.py:1424`) |
| 404 | `Payment method not found in your shop` | `id` is unknown or owned by another shop (`webpanel.py:1373`) |

`name` is truncated to 60 chars, `instructions` to 1000 (`webpanel.py:1382`).

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

Pause / rename / delete on the **same** endpoint (`id` is the row from a prior
add or from the `/storefront` list) — pause is a partial update that never
touches the stored handle:

```bash
# Pause row 7 (buyers stop seeing it; the row and its handle stay put) —
# this is the safe way to retire a *seeded* default (see the reboot trap).
curl -sS -X POST "…/panel/api/payment?t=$T" -H "Content-Type: application/json" \
  -d '{"id":7,"active":0}'                       # → {"ok":true,"id":7,"created":false}

# Re-enable it later
curl -sS -X POST "…/panel/api/payment?t=$T" -H "Content-Type: application/json" \
  -d '{"id":7,"active":1}'

# Rename without disturbing the handle/address
curl -sS -X POST "…/panel/api/payment?t=$T" -H "Content-Type: application/json" \
  -d '{"id":7,"name":"Venmo (fastest)"}'

# Hard delete a *non-seeded* row
curl -sS -X POST "…/panel/api/payment?t=$T" -H "Content-Type: application/json" \
  -d '{"id":9,"delete":true}'                     # → {"ok":true,"deleted":true}
```

### Verify the buyer will see them

```bash
curl -sS "https://unicornfartzz-bot.onrender.com/storefront?invite=YOUR_HEX" | \
  python -c "import sys,json; print(json.load(sys.stdin).get('payments'))"
```

Expect a non-empty list of `"Name: instructions"` lines. Empty list → seed a
method (any path above). Do **not** reset the DB to "fix" an empty pay screen.
That JSON array is the raw `Name: instructions` render (`payment_display_lines`,
`vendor_stores.py:433`); the trailing `": "` is stripped for rows with no
instructions.

### Handle format decides the tap-to-pay button

The `storefront`/`order` JSON is plain text, but the **Telegram receipt** the
buyer gets after ordering renders a one-tap **pay link** for some rails
(`payment_pay_link`, `vendor_stores.py:476`; `_payment_method_html`,
`vendor_stores.py:511`). Whether the buyer gets a button — and whether *you* can
reconcile the payment to an order — depends on the **handle format** you seed:

| Rail | Deep link | Prefilled | Format that produces a link |
|------|-----------|-----------|-----------------------------|
| Venmo | `account.venmo.com/pay` | amount **+ order code in the note** | any `@handle` |
| Cash App | `cash.app/$tag/<amt>` | amount only | any `$Cashtag` |
| PayPal | `paypal.me/<user>/<amt>` | amount only | **username only** — an email gets **no** link (`vendor_stores.py:506`) |
| Zelle · Apple Cash · email-PayPal · crypto · custom | none | — | rendered as a tap-to-copy target only |

Setup takeaways:

- **Venmo reconciles itself.** The order code rides along as the payment note,
  so you can match incoming Venmo payments to orders by note. Prefer Venmo when
  you want hands-off reconciliation.
- **Seed PayPal as a `paypal.me` username, not an email,** if you want the
  one-tap button. An email still works — buyers just copy/paste it — but there's
  no deep link (`paypal.me` can't address emails). The F&F vs G&S warning line
  follows `network_note` (`vendor_stores.py:517`).
- **Crypto** never gets a deep link and always shows the "double-check the
  network — wrong network = lost funds" warning, plus `network_note` as
  "Network: …" (`vendor_stores.py:523`).
- Any row with a resolvable target shows it as a tap-to-copy `<code>` chip;
  **custom/free-text** rows with no detectable target print their `instructions`
  verbatim instead (`vendor_stores.py:527`).

### Reconcile a payment to an order (read-only, no DB edits)

Once the buyer pays a **manual** rail, you match their payment to the order by
the `code` (`UMF-…`) — Venmo carries it in the note automatically; other rails
rely on the buyer pasting it. To check an order's state without opening the DB
or the admin bot, hit the public buyer lookup:

```bash
curl -sS "https://unicornfartzz-bot.onrender.com/order-status?invite=YOUR_HEX&code=UMF-AB12CD"
```

`api_order_status` (`webpanel.py:957`) is **read-only and shop-scoped**: it
resolves the `invite` through `storefront_keys` only (a claim/vendor-invite token
never resolves), and a `code` that belongs to another shop returns
`404 order not found` — so a leaked storefront key can never read a different
shop's orders. It returns `status`, line items, `subtotal`/`shipping_fee`/`total`,
`created_at`, and any `tracking_number`/`tracking_carrier`/`ship_*` set later.

- Use it to confirm a `POST /order` actually created the code you expect, and to
  watch the status move (`pending_payment` → `awaiting_confirmation` once you
  confirm, → tracking fields once shipped).
- It **cannot** confirm, cancel, or mark paid — those are admin-only actions in
  the bot/panel. This endpoint is purely a mirror.

### Confirm what the boot seed actually did

`_seed_unicorn_payments` (`run_cloud.py:22`) logs its result on every Unicorn
boot — grep the deploy log for `unicorn payments seed:`:

```
unicorn payments seed: {'ok': True, 'created': ['venmo', 'paypal'], 'total': 2}
```

- `created` lists **only the types newly inserted this boot**. On a fresh shop
  it's `['venmo', 'paypal']`; on every boot after, it's `[]` because both types
  already exist (the idempotency in [Section A](#a-boot-seed-unicorn-only-idempotent)).
- `total` is the count of distinct `method_type`s the shop has **after** seeding
  (`ensure_shop_payments`, `webpanel.py:1483`), active or paused. `total` climbing
  without `created` growing means the vendor added rails by hand — expected.
- `created` non-empty on a shop that *should* already be seeded → a seeded row was
  **deleted** (not paused) and just got re-created with the default handle. That's
  the delete-then-reboot trap in Section A; pause instead.

### D. Native Telegram card checkout (`TELEGRAM_PAYMENT_PROVIDER_TOKEN`)

Everything above is **manual rails** — the buyer copies a handle and pays out of
band. There is a **second, optional** rail: Telegram's built-in card checkout
(`tg_payments.py`). It is **additive and off by default** — the whole send path
is a no-op unless a provider token is set (`invoices_enabled`,
`tg_payments.py:28`). It does **not** replace the manual rails; when enabled it
runs *alongside* them, and the buyer gets a real in-Telegram card invoice DM'd
right after `POST /order` succeeds.

**Never Telegram Stars (XTR).** This path is fiat-only for physical goods —
`invoice_currency` (`tg_payments.py:32`) coerces `XTR`/empty to `USD`, and
`build_invoice_body`/`validate_pre_checkout` bail on `XTR`
(`tg_payments.py:79`, `:161`). Stars are never accepted.

**Turn it on:**

1. In **BotFather → your bot → Payments**, connect a real card provider (e.g.
   Stripe) and copy the **provider token** it issues (format `NNNNN:LIVE:…` or
   `…:TEST:…`).
2. Set env `TELEGRAM_PAYMENT_PROVIDER_TOKEN=<that token>` on Render and restart.
   Also set `CURRENCY` (or the shop's `currency`) to a 3-letter fiat like `USD`.
3. Handlers register unconditionally at boot
   (`register_payment_handlers`, `tg_payments.py:232`), so no code change is
   needed — the token flips the send path on. **Never log this token.**

**The flow once on:**

- After auth + `create_order`, `api_order` calls
  `send_invoice_for_order` (`spbc_notify.py:1217`) which DMs a `sendInvoice` to
  the buyer on the **vendor/live token** and sets `invoice_offered: true` in the
  order response.
- The invoice's `payload` is `umf-order:<order_id>` (`tg_payments.py:40`); title
  is capped at 32 chars, description at 255, and an order `total ≤ 0` sends **no**
  invoice (`tg_payments.py:95`).
- Telegram fires a **pre-checkout query**; `validate_pre_checkout`
  (`tg_payments.py:155`) is **server-authoritative** — it re-checks currency and
  that `total_amount` equals the order's stored cents, and rejects an order that
  is `cancelled`/`rejected`/already `paid`. Amount/currency mismatch → declined.
- On success, `apply_successful_payment` (`tg_payments.py:184`) →
  `db.record_telegram_payment_charge` (`db.py:2481`) stores
  `tg_payment_charge_id` (truncated to 128 chars) and advances the order status
  **only** `pending_payment → awaiting_confirmation` — any other status (e.g.
  already `awaiting_confirmation`, `paid`) is left untouched, and a
  `cancelled`/`rejected` order refuses the charge. **It does not deduct stock or
  mark the order fulfilled** — the admin's normal confirm step still deducts
  stock, same as a manual-rail order. Card capture ≠ shipment.
- **Telling a card-paid order apart:** a non-empty `tg_payment_charge_id` on the
  order row is the marker that a Telegram card charge landed. A manual-rail order
  never has one; it sits in `awaiting_confirmation` purely on the buyer's word
  that they sent the handle. So a card order in `awaiting_confirmation` is
  money-in-hand; a manual one is a claim to verify against your Venmo/PayPal feed.
- **No auto re-issue and no refund automation.** If the invoice DM fails
  (`sendInvoice HTTP 4xx`), nothing retries — re-trigger by having the buyer
  re-open checkout, or fall back to the manual rails they already got in the same
  order response. There is no in-app refund path; refunds are done in the card
  provider's own dashboard (Stripe, etc.), and the order status is unaffected.

**Diagnosing:**

- `invoice_offered: false` on a shop you *did* wire → the provider token is
  unset/empty, the order total is `0`, or `currency` resolved to `XTR`.
- `sendInvoice HTTP 4xx` in the log (`tg_payments.py:128`, token never printed)
  → bad/expired provider token, or the bot isn't payments-enabled in BotFather.
- "Currency mismatch" / "Amount mismatch" at pre-checkout → the invoice was
  built against a different total/currency than the order now has; re-issue.

### Notes / gotchas

- **Payments are per-shop.** Seeding the SPBC main shop does not give the
  Unicorn shop rails, and vice versa.
- **Manual rails and native card checkout are independent.** You can run either,
  both, or neither. `invoice_offered:false` with a non-empty `payments` array is
  a perfectly healthy manual-only shop — don't "fix" it by wiring a card
  provider unless you actually want in-Telegram card capture.
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
- Seed PayPal as a `paypal.me` **username** (not an email) and prefer **Venmo**
  when you want a one-tap pay button and order-code reconciliation.
- **Pause** (`⏸`) a seeded payment default you don't want, rather than deleting
  it — a deleted seeded type is re-created on the next boot.
- Read the `POST /order` log line before changing config — trust the `reason`,
  not the overloaded `bad_hash` buyer code.
- Use `GET /order-status?invite=&code=` (read-only) to check an order's state or
  confirm a code exists — never open the live DB to look up an order.
- Set PayPal **Goods & Services** via the panel/API (`network_note=goods_services`);
  the Telegram wizard only ever produces Friends & Family.

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
| `POST /order` success response | `spbc_notify.py:1226` |
| HMAC validate (one token) | `vendor_stores.py:263` |
| HMAC validate (any bound token) | `vendor_stores.py:1201` |
| Error code map (`bad_hash`/`expired`) | `vendor_stores.py:138` |
| Post-HMAC payload checks (still → `bad_hash`) | `vendor_stores.py:330` |
| Token set for a shop | `vendor_stores.py:1145` |
| Unicorn HMAC aliases | `vendor_stores.py:1112` |
| Trailing-slash normalize | `vendor_stores.py:84` |
| Cache-bust store URL | `vendor_stores.py:104` |
| Payment templates | `payment_templates.py` |
| Field → template routing | `payment_templates.py:228` |
| Idempotent payment seed | `webpanel.ensure_shop_payments` |
| Unicorn boot payment seed | `run_cloud._seed_unicorn_payments` |
| Default Unicorn rails | `unicorn_shop.DEFAULT_PAYMENT_METHODS` |
| Panel payment write API (add/update/delete/seed) | `webpanel.api_payment` |
| Tap-to-pay deep-link builder | `vendor_stores.py:476` |
| Payment method HTML render (receipt) | `vendor_stores.py:511` |
| Buyer-visible list (active-only, sorted) | `db.py:2025` |
| Read-only buyer order lookup (`GET /order-status`) | `webpanel.py:957` |
| Idempotent seed (dedup by `method_type`) | `webpanel.ensure_shop_payments` (`webpanel.py:1451`) |
| Wizard field render (PayPal → forced F&F) | `payment_templates.py:205` |
| Telegram payments menu | `bot.py:5418` |
| `payments` in storefront/order JSON | `vendor_stores.py:433` |
| Native card checkout (optional) | `tg_payments.py` |
| Invoices-enabled gate (provider token) | `tg_payments.py:28` |
| Invoice send after order | `spbc_notify.py:1217` |
| Server-authoritative pre-checkout | `tg_payments.py:155` |
| Record card charge (no stock deduct) | `tg_payments.py:184` |
| Charge → `awaiting_confirmation` transition | `db.record_telegram_payment_charge` (`db.py:2481`) |
