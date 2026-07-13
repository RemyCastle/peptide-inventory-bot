# UnicornFartzzBot — How to Use

Step-by-step guide for **customers**, **shop admins**, and **owners**.

---

## First-time setup (you, the owner)

### 1. Start the bot
- Double-click `start.bat`, **or**
- Run `python bot.py` from the project folder (with venv active)

The laptop/PC must stay on while the bot is running.

### 2. Open the bot in Telegram
1. Open Telegram
2. Search for your bot username (the one you created with @BotFather)
3. Tap **Start** or send `/start`

### 3. Your shop is created
If you’re the first person to `/start` (or your ID is in `OWNER_IDS`), the bot creates a **personal shop** and opens the main menu.

### 4. Configure the shop (Admin Panel)
Tap **⚙️ Admin Panel** or send `/admin`, then do this in order:

| Step | What to do |
|------|------------|
| **1. Products** | **➕ Add product** → name → price → stock qty → description |
| **1b. COA** | Product → **📄 Upload COA (PDF/photo)** → send PDF or image (not a Janoshik link) |
| **2. Payments** | **💳 Payments** → **➕ Add method** → e.g. Cash App / Zelle / BTC + pay instructions |
| **3. Shipping** | **🚚 Shipping** → set flat fee + free-shipping threshold (or turn off) |
| **4. Admins** (optional) | **👥 Admins** → **➕ Add admin** → their numeric Telegram user ID |
| **5. Share link** | **🔗 Shop link** → copy and send to customers |

### 5. Group shop (optional)
1. Create or open a Telegram group  
2. Add UnicornFartzzBot to the group  
3. In the group, send `/setup` (owner only)  
4. Manage inventory in a **DM with the bot** via `/admin`  
5. Share the shop link from Admin → **Shop link**

### 6. Lock in your owner ID (recommended)
1. Message [@userinfobot](https://t.me/userinfobot) → copy your **Id**  
2. Edit `.env` and set:
   ```
   OWNER_IDS=123456789
   ```
   (use your real ID; multiple owners: `111,222`)  
3. Restart the bot  

---

## Customer guide

### Browse and order
1. Open the bot (or use the shop link someone sent you)  
2. `/start` → **🧬 Catalog**  
3. Tap a product → **＋1 / ＋2 / ＋5** to add to cart  
4. **🛒 Cart** → check items → **✅ Checkout**  
5. Choose a **payment method**  
6. Enter **full name**, **shipping address**, and optional **notes**  
7. You’ll get an order number, total (items + shipping), and pay instructions  

### Pay and confirm
1. Pay using the instructions (Cash App, Zelle, etc.)  
2. In the bot, tap **✅ I've paid**  
3. Wait for a shop admin to confirm  
4. You’ll get a message when payment is confirmed  

### Track orders
- **📦 My Orders** on the menu, or `/orders`  
- You can cancel an unpaid order from the order view  

### What customers should know
- **Shipping** is added automatically (or free if your cart is over the shop’s free-shipping amount)  
- **Inventory is not removed** until an admin confirms payment  
- If stock runs out before confirm, the admin may reject the order  
- **📄 COA** sends a PDF/photo of the lab report **in chat** (no external website login)

---

## COA (Certificate of Analysis)

| Who | How |
|-----|-----|
| **Admin only** | Admin Panel → Products → pick item → **Upload COA (PDF/photo)** → send file |
| **Everyone** | Catalog / product → **📄 COA** → bot sends the file in Telegram |

- Prefer a **PDF export or screenshot** of the COA, not a Janoshik web link (lab sites often block browsers).  
- **Admin Panel and product edit controls are only visible to shop admins/owners.** Buyers never see price/stock edit buttons.  
- Remove COA anytime: product → **🗑 Remove COA**.

---

## Admin guide

Open **⚙️ Admin Panel** (`/admin`).

### Products
| Action | How |
|--------|-----|
| Add | **➕ Add product** — name, price, stock, description |
| Change price | Products → pick item → **💲 Price** → send number (e.g. `45.00`) |
| Change stock | Products → pick item → **📊 Stock** → absolute number (`10`) or adjust (`+5`, `-2`) |
| Hide from catalog | Products → item → **⏸ Deactivate** |
| Delete | Products → item → **🗑 Delete** |

### Orders & payment confirmation
1. **⏳ Needs confirm** — customers who tapped “I've paid”  
2. Or **📋 Orders** — full recent list  
3. Open an order:  
   - **✅ Confirm paid** → order marked paid **and stock is deducted**  
   - **❌ Reject** → order closed, **stock unchanged**  

Admins also get Telegram notifications when:
- A new order is placed  
- A customer reports payment  

### Payment methods
**💳 Payments** → **➕ Add method**

Example:
- **Name:** `Cash App`  
- **Instructions:**  
  `$YourCashtag`  
  Send exact order total. Memo: order number.

You can pause or delete methods anytime.

### Shipping (auto add-on)
**🚚 Shipping**

| Setting | Meaning |
|---------|---------|
| **Toggle ON/OFF** | Include shipping at checkout or not |
| **Flat fee** | Amount added to every order (e.g. `$8.00`) |
| **Free above** | Subtotal at/above this = `$0` shipping (`0` = never free) |

Customers see shipping on the cart and final order total automatically.

### Admins (per shop)
**👥 Admins** → **➕ Add admin** → paste their Telegram **user ID** (from @userinfobot).

- Admins can change prices, stock, payments, shipping, and confirm orders for **that shop**  
- Only **owners** can remove other admins (admins can remove themselves)  

---

## Commands cheat sheet

| Command | Who | What |
|---------|-----|------|
| `/start` | Everyone | Main menu / open shop |
| `/catalog` | Customers | Browse products |
| `/cart` | Customers | View cart & checkout |
| `/orders` | Customers | Your order history |
| `/admin` | Admins | Admin panel |
| `/setup` | Owner | Create shop in a group |
| `/help` | Everyone | Short help |
| `/cancel` | Everyone | Abort multi-step input (adding product, checkout, etc.) |

---

## Order statuses

| Status | Meaning |
|--------|---------|
| `pending_payment` | Order placed; customer has not reported payment yet |
| `awaiting_confirmation` | Customer tapped “I've paid”; admin should verify |
| `paid` | Admin confirmed; **inventory reduced** |
| `cancelled` | Customer cancelled; stock unchanged |
| `rejected` | Admin rejected; stock unchanged |

---

## Everyday workflow (seller)

1. Keep the bot running (`start.bat`)  
2. Stock products and set prices in Admin  
3. Share the **Shop link** with buyers  
4. When you get a payment notification → check the money in Cash App/Zelle/etc.  
5. In the bot → **✅ Confirm paid** (or **Reject** if wrong/missing)  
6. Ship to the address on the order  

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Bot doesn’t reply | Make sure `start.bat` / `python bot.py` is running; check token in `.env` |
| “No shop selected” | Send `/start` again, or open the shop link |
| “Admin only” | Get your user ID, have owner add you under **Admins**, or set `OWNER_IDS` in `.env` |
| “No payment methods” | Admin must add at least one under **💳 Payments** |
| Cart says stock issue | Reduce qty or wait for restock; stock was checked at checkout |
| Confirm fails (insufficient stock) | Restock the product, then confirm — or reject the order |
| Two bots / conflicts | Only run **one** instance of this bot at a time |

---

## Files you’ll use

| File | Purpose |
|------|---------|
| `start.bat` | Start the bot on Windows |
| `.env` | Token, owner IDs, brand name, default shipping |
| `inventory.db` | All shops, products, and orders (created automatically) |
| `HOW_TO_USE.md` | This guide |
| `README.md` | Install / technical overview |

---

## Privacy & safety tips

- Never share your **bot token** publicly; if leaked, revoke it in @BotFather and update `.env`  
- Confirm payment **out of band** (app balance/history) before tapping Confirm  
- Keep `OWNER_IDS` set so random users can’t take over admin after bootstrap  

---

**UnicornFartzzBot** — catalog, cart, payments, shipping, admin-confirmed inventory.
