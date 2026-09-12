# Unicorn Mini App ↔ bot catalog parity

**Bot / API:** https://unicornfartzz-bot.onrender.com  
**Mini App (Cloudflare Pages, not this repo):** https://remy-miniapp-demos.pages.dev/unicorn/  
**Never:** wipe `/data/inventory.db` on Render. Laptop `inventory.db` is not the live catalog.

The Mini App is a view. Stock, price, and checkout stay server-authoritative (`create_order`). This repo does not deploy Pages.

## What already matches

| Surface | Bot / `/storefront` | Mini App |
|---|---|---|
| Catalog source | `GET /storefront?invite=` (public storefront key) | `loadLiveCatalog()` |
| Checkout | `POST /order` | `submitOrder()` |
| Order lookup | `GET /order-status?invite=&code=` | “Check my order” field |
| Buyer names | `display_product_name` (no `$price` / `(vial)` / `Anav@r`) | Same JSON `name` |
| Kit price | `kit_price` on the vial row | Ultimate Potion / kit add |
| Sold out | `create_order` 409 | Client caps qty; server re-checks |
| Shipping | `shipping_fee` / `free_shipping_above` | Checkout sheet |

## Gaps (do not “fix” by wiping stock)

1. **Pages copy is still a mockup.** Live Mini App HTML still says “sample data” / “make-believe”. Catalog JSON is live; the chrome copy is not. Edit `miniapp-demos/unicorn/` + Pages, not this repo.
2. **Shop title.** Live `/storefront` used to return `Shop`. Boot now persists `Unicorn Magic Factory` when the catalog shop title is generic. Mini App hero title is hardcoded separately.
3. **SKU / variants.** API returns `sku`, `variant_group`, `variant_label`. Mini App cards do not show them yet.
4. **Duplicate sibling cards.** MT1 / MT2 / Oxytocin were two prices with the same cleaned name. This ship appends `$price` (or a description snippet) so both stay for sale and stay distinct. **Does not delete.**
5. **Snap 8.** `$8` vial vs `$50` (250mg) must not fold into a kit. Kit merge is `$8` + `$75` only. Already-merged live rows are not auto-restored.
6. **Payment methods.** `/storefront` `payments` can still be empty if the owner paused every row. Mini App `POST /order` now returns 409 `no_payment_methods` instead of creating a code with no rail. Boot seeds Venmo+PayPal on the catalog shop even without `UNICORN_CLAIM_TOKEN`. Seed/edit in Admin → Payments or `/webpanel`, never by resetting the DB.
7. **Search / tile-list / favorites.** Client-only in the Mini App. Telegram catalog has its own search + 64-char button labels.
8. **Photos.** Storefront only exposes `http` photo URLs. Telegram `file_id` photos stay in the bot.

## Admin panel (this repo)

`/webpanel` catalog tab: find-in-catalog, show-hidden, SKU field, duplicate-name flag, “buyers see: …” when the stored name still has an import tail.

## Out of scope here

- Pushing `inventory.db`
- Editing Cloudflare Pages from this git remote
- Hard-delete of products
- SPBC back-room paths (Unicorn is cut over)
