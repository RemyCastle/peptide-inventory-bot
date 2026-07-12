# Peptide Inventory Telegram Bot

Multi-shop inventory bot: catalog, cart, checkout, payment options, per-shop admins, and **inventory deduction only after admin confirms payment**. Shipping fee is auto-added at checkout.

---

## Features

| Who | What |
|-----|------|
| **Customers** | Browse catalog, cart, checkout, pick payment method, enter shipping, mark “I've paid” |
| **Admins (per shop)** | Products (price/stock), payment methods, shipping rules, confirm/reject orders, add admins |
| **Owners** | `OWNER_IDS` in `.env` — full access to every shop; can `/setup` group shops |

### Order lifecycle

1. Customer checks out → order status `pending_payment` (**stock not reduced**)
2. Customer pays using instructions → taps **I've paid** → `awaiting_confirmation`
3. Admin **Confirm paid** → status `paid` → **stock reduced**
4. Or admin **Reject** / customer cancel → no stock change

### Shipping auto add-on

- Flat fee per shop (default `$8`)
- Optional free-shipping threshold (default `$150`)
- Toggle shipping on/off in Admin → Shipping

---

## Quick start (Windows)

### 1. Create a bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the token

### 2. Get your Telegram user ID

Message [@userinfobot](https://t.me/userinfobot) and copy your ID.

### 3. Configure

```powershell
cd C:\Users\Remy\peptide_inventory_bot
copy .env.example .env
```

Edit `.env`:

```
TELEGRAM_BOT_TOKEN=123456:ABC...
OWNER_IDS=YOUR_TELEGRAM_USER_ID
BRAND_NAME=UnicornFartzzBot
```

### 4. Run

Double-click `start.bat`, or:

```powershell
cd C:\Users\Remy\peptide_inventory_bot
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

### 5. First-time setup

**Option A — personal shop (DM)**  
Open the bot → `/start` as owner → personal shop is created → **Admin Panel**.

**Option B — group shop**  
1. Add the bot to your Telegram group  
2. In the group: `/setup` (owner only)  
3. DM the bot → `/start` → Admin Panel  
4. Share the **Shop link** from Admin with customers

### 6. Stock the shop

In Admin Panel:

1. **Add product** (name, price, stock, description)
2. **Payments** → add Cash App / Zelle / crypto + instructions
3. **Shipping** → set fee and free threshold
4. **Admins** → add other admins by Telegram user ID

---

## Customer commands

| Command | Description |
|---------|-------------|
| `/start` | Menu (or `?start=shop_<id>` deep link) |
| `/catalog` | Product list |
| `/cart` | View cart / checkout |
| `/orders` | Your order history |
| `/help` | Help |

---

## Admin commands

| Command | Description |
|---------|-------------|
| `/admin` | Admin panel |
| `/setup` | Initialize shop in a group (owner) |
| `/cancel` | Abort multi-step input |

---

## Project layout

```
peptide_inventory_bot/
├── bot.py           # Telegram handlers
├── db.py            # SQLite models & order logic
├── config.py        # Env config
├── inventory.db     # Created on first run
├── requirements.txt
├── .env.example
├── start.bat
└── README.md
```

---

## Notes

- **Multi-shop**: each Telegram group (or owner DM) is a separate shop with its own products, prices, inventory, admins, and payment methods.
- **Soft stock check**: stock is checked at order creation and again at payment confirm. Only confirm deducts.
- Run only **one** instance of the bot (Telegram allows a single long-poll client per token).

---

## Disclaimer

For research-product inventory tooling only. You are responsible for compliance with Telegram terms and all applicable laws in your jurisdiction.
