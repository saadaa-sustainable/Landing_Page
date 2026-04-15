# सादा Traffic Server

Fetches Shopify product page traffic directly from ShopifyQL
and serves it to the ops dashboard. No cloud needed.

## Setup

**1. Fill in your credentials**

Edit `.env`:
```
SHOP_DOMAIN=saadaa-design.myshopify.com
ADMIN_ACCESS_TOKEN=shpat_your_token_here
```

Get your token: Shopify Admin → Settings → Apps →
Develop apps → Your app → API credentials → Admin API access token
Required scope: `read_analytics`

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Run the server**

```bash
python server.py
```

You'll see:
```
  सादा Traffic Server
  ───────────────────
  Running on  →  http://localhost:5000
  Shop        →  saadaa-design.myshopify.com
```

**4. Connect the dashboard**

Open `dashboard_final.html` in your browser.

Traffic tab → ⚙ Data Source → paste `http://localhost:5000` → pick a date → **Fetch →**

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/traffic?date=YYYY-MM-DD` | Fetch product traffic for a date |
| `GET /api/health` | Check server is running |

## Notes

- The server only runs while the terminal is open
- Your `ADMIN_ACCESS_TOKEN` never leaves your machine
- The `.env` file is for local use only — never commit it to git
