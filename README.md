# Saadaa Ops Dashboard

Internal operations dashboard for the Saadaa storefront — inventory health, traffic / ad / sales analytics, order management, UTM attribution.

Live: <https://landing-page-three-blush.vercel.app/>

---

## Project Structure

```
landing_page/
├── api/                              ← Vercel serverless function (kept at root per Vercel contract)
│   └── index.py                          Flask app — all /api/* routes
│
├── backend/                          ← Local development + scheduled jobs
│   ├── server.py                         BaseHTTPServer dev server (python backend/server.py → :5000)
│   ├── snapshot_inventory.py             Daily inventory snapshot — Shopify → Supabase inventory_snapshots
│   └── snapshot_cron.bat                 Windows Task Scheduler hook (runs snapshot_inventory.py nightly)
│
├── frontend/                         ← Single-page application
│   ├── index.html                        Markup (was inline inside dashboard_final.html)
│   ├── css/
│   │   └── styles.css                    All styles (was two inline <style> blocks)
│   └── js/
│       └── app.js                        All application JavaScript (was inline <script> block)
│
├── docs/                             ← Documentation
│   ├── BACKEND_GUIDE.md                  Per-endpoint backend reference
│   ├── PROJECT_DOCUMENTATION.md          Full system reference (Markdown)
│   ├── Saadaa_Ops_Dashboard_System_Reference.docx
│                                         Same content, Word format
│   └── CHANGES_SUMMARY.md                Notable change history
│
├── logs/                             ← Runtime logs (snapshot_*.log, etc.)
│
├── vercel.json                       Vercel routing config — builds api/index.py
├── requirements.txt                  Python deps (Flask, requests, python-dotenv, pandas, numpy)
├── .env                              Secrets — gitignored
├── .gitignore
└── README.md                         ← This file
```

---

## Running Locally

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Set env vars in .env at the project root:
#    SHOP_DOMAIN=your-shop.myshopify.com
#    ADMIN_ACCESS_TOKEN=shpat_...
#    SUPABASE_URL=https://xxx.supabase.co        # ads project (primary_table, inventory_snapshots)
#    SUPABASE_SERVICE_KEY=eyJ...
#    SAADAA_VAR=https://yyy.supabase.co          # data project (sessions, orders, order_line_items)
#    SAADAA_KEY=eyJ...

# 3. Start the dev server
python backend/server.py

# 4. Open http://localhost:5000
```

The dev server (`backend/server.py`) serves:
- `GET /` → `frontend/index.html`
- `GET /css/<file>` → `frontend/css/<file>`
- `GET /js/<file>` → `frontend/js/<file>`
- `GET /api/*` → same set of endpoints as Vercel production

---

## Deploying

```bash
git push origin master
```

Vercel auto-deploys from the `master` branch. The build config in `vercel.json` bundles `api/index.py` as a Python serverless function; static assets are served by the same function (it has `/`, `/css/<file>`, `/js/<file>` routes that read from `frontend/`).

Env vars must be set in Vercel Project Settings → Environment Variables (the same six listed above).

---

## Scheduled Jobs

Daily inventory snapshot — pulls every product's variant-level stock from Shopify and writes one row per product into Supabase `inventory_snapshots`.

```bash
# Manual run
python backend/snapshot_inventory.py

# Windows Task Scheduler (recommended): point at backend/snapshot_cron.bat
# Suggested schedule: 00:10 IST daily
```

Logs land in `logs/snapshot_<YYYY-MM-DD>.log`.

---

## Architecture (at a glance)

```
              ┌──────────────────────────────────────┐
   browser ──→│ frontend/ (index.html + css/ + js/)  │
              └──────────────────────────────────────┘
                              │ fetch(/api/*)
              ┌───────────────┴───────────────┐
              ▼                               ▼
   ┌─────────────────┐               ┌─────────────────┐
   │ backend/        │               │ api/index.py    │
   │ server.py       │   (local)     │ (Vercel Flask)  │   (production)
   │ localhost:5000  │               └────────┬────────┘
   └────────┬────────┘                        │
            │                                 │
            └───────┬─────────────────────────┘
                    ▼
       ┌────────────────────────────┐
       │  Shopify Admin GraphQL     │  Sales, Products, Inventory
       │  2025-10                   │  Orders + Traffic (fallback)
       └────────────────────────────┘
       ┌────────────────────────────┐
       │  Supabase — ads project    │  primary_table (Meta ads)
       │  SUPABASE_URL / KEY        │  inventory_snapshots
       └────────────────────────────┘
       ┌────────────────────────────┐
       │  Supabase — SAADAA project │  sessions, orders,
       │  SAADAA_VAR / SAADAA_KEY   │  order_line_items
       └────────────────────────────┘
```

`/api/traffic` and `/api/orders` prefer SAADAA Supabase, fall back to Shopify if env vars missing or call fails. Response carries a `_source` tag indicating which path served.

`/api/sales` always goes to Shopify ShopifyQL. `/api/ads` always goes to ads-project Supabase. `/api/products` + `/api/inventory-snapshot` go to their respective sources.

Full per-endpoint reference: `docs/BACKEND_GUIDE.md`.

---

## Restructured From Single-File SPA

Until this commit the project was a single 10,858-line `dashboard_final.html` at the project root with all CSS + JS inline, plus a few Python files scattered alongside it. Restructure split that monolith into:

- `dashboard_final.html` → `frontend/index.html` (markup only)
- inline `<style>` blocks → `frontend/css/styles.css`
- inline `<script>` block → `frontend/js/app.js`
- backend Python files → `backend/`
- documentation → `docs/`

`api/index.py` and `vercel.json` stay at root because Vercel auto-detects them there. Everything else (env vars, data sources, application behavior) is unchanged.

---

## Documentation

- **[docs/PROJECT_DOCUMENTATION.md](docs/PROJECT_DOCUMENTATION.md)** — full system reference (every tab, every KPI, every quirk)
- **[docs/BACKEND_GUIDE.md](docs/BACKEND_GUIDE.md)** — per-endpoint backend reference with Mermaid diagrams
- **[docs/Saadaa_Ops_Dashboard_System_Reference.docx](docs/Saadaa_Ops_Dashboard_System_Reference.docx)** — Word-format version of the same content
