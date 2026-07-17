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
| **1b. COA** | Product → **📄 Set COA** → send PDF/photo **or** paste an `https://` link (or both) |
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
7. **Verify shipping** — review name/address and tap **Yes, looks correct**  
8. You’ll get an order number, total, pay instructions, and a **payment code**  

### Pay and confirm
1. Pay using the instructions (Cash App, Zelle, etc.)  
2. Put the **payment code** in the payment app’s **memo/notes** field  
3. In the bot, tap **✅ I've Paid**  
4. Upload a **screenshot** of the payment (or Skip)  
5. Wait for a shop admin to confirm — you’ll get a **push message** when confirmed  
6. Tracking appears in that message if the seller added it  

---

## Payment code & proof

| Item | Purpose |
|------|---------|
| **Payment code** | Auto-generated (e.g. `UF12-AB3K9Z`) — put in Cash App/Zelle/Venmo notes so the seller can match your payment |
| **Screenshot** | Optional but recommended after “I've Paid” — sent to admins with your claim |
| **Address verify** | Checkout won’t place the order until you confirm shipping details |

### Track orders
- **📦 My Orders** on the menu, or `/orders`  
- You can cancel an unpaid order from the order view  

### What customers should know
- **Shipping** is added automatically (or free if your cart is over the shop’s free-shipping amount)  
- **Inventory is not removed** until an admin confirms payment  
- If stock runs out before confirm, the admin may reject the order  
- **📄 COA** sends the lab report **file and/or link** (whatever the seller attached)

---

## COA (Certificate of Analysis)

| Who | How |
|-----|-----|
| **Admin only** | Admin Panel → Products → pick item → **Set COA** → PDF/photo **and/or** `https://` link |
| **Everyone** | Catalog / product → **📄 COA** → bot sends the file (if set) and/or the link |

- You can store **both** a file and a link; 📄 COA delivers both.  
- Lab sites (e.g. Janoshik) sometimes block Telegram’s in-app browser — file upload still works offline.  
- **Admin Panel is admin-only.** Buyers only download COA, not edit products.  
- Remove COA anytime: product → **🗑 Remove COA**.

---

## Admin guide

Open **⚙️ Admin Panel** (`/admin`).

### Products
| Action | How |
|--------|-----|
| Add | **➕ Add product** — name, price, stock, description |
| **Import from file** | Admin → **📥 Import inventory** → optional template → send a `.txt` document |
| Change price | Products → pick item → **💲 Price** → send number (e.g. `45.00`) |
| Change stock | Products → pick item → **📊 Stock** → absolute number (`10`) or adjust (`+5`, `-2`) |
| Hide from catalog | Products → item → **⏸ Deactivate** |
| Delete | Products → item → **🗑 Delete** |

#### Import file layout (`.txt`)
```text
# comments start with #
name | price | stock | description (optional)
Tren Ace | 45.00 | 10 | acetate blend
Test E | 30 | 5
```
- **Add-only**: creates new products; existing names are **skipped** (price/stock not changed).
- Nothing is deleted. Re-uploading the same file is safe.

### Orders & payment confirmation
1. **⏳ Needs confirm** — customers who tapped “I've paid” (and often attached a screenshot)  
2. Or **📋 Orders** — full recent list  
3. Open an order:  
   - **🖼 View proof** — see the payment screenshot  
   - **✅ Confirm + tracking** → enter tracking (or `-` for none) → order marked **paid**, stock deducted, **customer is messaged**  
   - **❌ Reject** → order closed, **stock unchanged**, customer notified  
   - On paid orders without tracking: **📦 Add tracking** → customer gets a tracking update  

Admins also get Telegram notifications when:
- A new order is placed (includes **payment code**)  
- A customer reports payment (includes proof screenshot when provided)  

Match payments using the **payment code** in the transfer memo.

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

### Rename shop / move to another group
Shop admins only (Admin Panel):

| Action | How |
|--------|-----|
| **✏️ Rename shop** | Admin → **Rename shop** → send the new display name |
| **🚚 Move to group** | Admin → **Move to group** → open the link → in the *destination* group send `/claim_transfer <token>` |

- **Move** transfers the whole shop (catalog, stock, orders, payments, admins). It is *not* a clone.  
- Destination group must be empty (no products/orders yet). Bot must already be a member.  
- Old customer links (`shop_<old_id>`) still open the moved shop.  
- After a move you get a new shop link to share.

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
5. In the bot → **✅ Confirm + tracking** (enter tracking or `-`) or **Reject**  
6. Customer gets an automatic confirmation message (with tracking if provided)

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
