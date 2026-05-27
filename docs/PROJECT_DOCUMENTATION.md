# Saadaa Ops Dashboard — Full System Reference

Internal operations dashboard for the Saadaa storefront. Bundles inventory health, traffic / ad / sales analytics, order management, and UTM attribution into a single-page web app.

---

## Project Snapshot

| | |
|---|---|
| **Project Name** | Saadaa Ops Dashboard |
| **Platform** | Python backend (local dev + Vercel serverless) + Single-file SPA frontend |
| **Backend Stack** | Python 3.12, Flask (Vercel), `http.server` (local), `requests`, `python-dotenv` |
| **Frontend Stack** | Vanilla JS, plain HTML/CSS in one file. Chart.js for charts. XLSX.js for Excel export. Fonts: Inter (body), Space Grotesk (headings/values), JetBrains Mono (labels). |
| **Backend Files** | `server.py` (local dev `http://localhost:5000`), `api/index.py` (Vercel Flask app), `snapshot_inventory.py` (daily inventory snapshot cron) |
| **Frontend File** | `dashboard_final.html` — single self-contained SPA (~10k lines) |
| **Data Sources** | Shopify Admin GraphQL 2025-10 (ShopifyQL + Orders/Products), Supabase REST (two projects — ads + sessions/orders) |
| **Databases** | **Ads project** Supabase: `primary_table` (Meta ads per-ad-per-day), `inventory_snapshots` (daily stock) · **SAADAA project** Supabase: `sessions`, `orders`, `order_line_items` |
| **Deployment** | GitHub `master` → Vercel auto-deploy at `https://landing-page-three-blush.vercel.app/` |
| **Env Vars** | `SHOP_DOMAIN`, `ADMIN_ACCESS_TOKEN`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SAADAA_VAR`, `SAADAA_KEY` |

---

## 1. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  dashboard_final.html  (browser)                                │
│  └─ fetch(/api/*) ─────────────────────────┐                    │
└────────────────────────────────────────────┼────────────────────┘
                                             │
              ┌──────────────────────────────┼────────────────────┐
              │                              │                    │
        ┌─────▼─────┐                  ┌─────▼─────┐         (same)
        │ server.py │ localhost:5000   │api/index.py│ Vercel
        │ BaseHTTP  │                  │ Flask     │
        └─────┬─────┘                  └─────┬─────┘
              │                              │
        ┌─────▼──────────────────────────────▼─────┐
        │                                          │
   ┌────▼────────────┐   ┌───────────────────┐    │
   │ Shopify Admin   │   │ Supabase (ads)    │    │
   │ GraphQL 2025-10 │   │ primary_table     │    │
   │ ShopifyQL +     │   │ inventory_snaps   │    │
   │ Orders/Products │   │                   │    │
   └─────────────────┘   └───────────────────┘    │
                                                  │
                         ┌────────────────────────▼───┐
                         │ Supabase (SAADAA project)  │
                         │ sessions, orders,          │
                         │ order_line_items           │
                         └────────────────────────────┘
```

**Path selection logic**:
- `/api/traffic` and `/api/orders` try **SAADAA Supabase first**, fall back to Shopify Admin if env vars missing or call fails. Response carries `_source` tag (`supabase`, `shopify:no-credentials`, `shopify:supabase-empty`, `shopify:supabase-error:<msg>`).
- `/api/sales` always goes to **Shopify ShopifyQL** (no Supabase mirror exists).
- `/api/ads` always goes to **ads-project Supabase `primary_table`** (Meta data is synced there by the Creative Testing Dashboard pipeline).
- `/api/products`, `/api/inventory` go to Shopify Admin GraphQL.

---

## 2. Backend Endpoints

Both `server.py` and `api/index.py` expose identical HTTP API surface. Frontend doesn't care which one is serving.

### `GET /api/health`
Liveness check. Returns `{status: "ok", shop: <SHOP_DOMAIN>}`.

### `GET /api/_env_probe`
Returns presence + length (never values) of every env var the app depends on. Diagnostic only.

### `GET /api/traffic?since=YYYY-MM-DD&until=YYYY-MM-DD`
Sessions data — visitors, sessions, bounces, cart-adds, checkouts. Tries SAADAA Supabase first.

Response:
```js
{
  byPath: [{landing_page_path, landing_page_type, day, online_store_visitors, sessions, bounces, ...}],
  rows:   [{...same fields plus utm_source, utm_medium, utm_campaign, utm_content, utm_term, session_city}],
  totals: {online_store_visitors, sessions, bounces, sessions_with_cart_additions, sessions_that_reached_checkout, ...},
  _source: "supabase" | "shopify:..."
}
```

**Shopify query strategy** (when falling back): two ShopifyQL calls split because of the 1000-row cap:
- `byPath` — `GROUP BY landing_page_type, landing_page_path, day` (3 dims, fits)
- `rows` — full 9-dim breakdown for UTM/city drilldown (capped, treat as partial)
- `totals` — single unsegmented query with no GROUP BY for authoritative day totals

### `GET /api/orders?since=YYYY-MM-DD&until=YYYY-MM-DD`
Full Shopify orders for the date range. Tries SAADAA Supabase first.

Response is a flat array of orders, each with:
```js
{
  id, name, createdAt, cancelled, total, subtotal, totalDiscounts, totalShipping, totalTax,
  financial_status, tags, source, paymentGateway, discountCodes,
  customAttributes: { utm_source, utm_medium, utm_campaign, utm_content, utm_term, full_url, ... },
  shipping: { address1, city, province, country, ... },
  lineItems: [{id, title, variant, sku, quantity, price, imageUrl}, ...]
}
```

**Implementation note**: SAADAA Supabase `orders` table has no index on `created_at`, so any server-side `ORDER BY created_at` triggers Postgres statement_timeout (5s). The query intentionally drops `ORDER BY`; results are sorted client-side after fetch.

### `GET /api/sales?since=YYYY-MM-DD&until=YYYY-MM-DD`
Shopify ShopifyQL "sales" dataset. Returns three datasets:

```js
{
  rows:                 [...detail rows — 9-dim GROUP BY incl. UTM + customer type, LIMIT 10000],
  byProduct:            [...per-product aggregates — server-aggregated, no row cap],
  grandTotals:          {gross_sales, total_sales, discounts, returns, net_sales, taxes, net_items_sold},
  excludedFromBreakdown: {gross_sales, total_sales, ...}  // grand - byProduct sum
}
```

**`grandTotals` is the authoritative reconciler** — comes from an UNFILTERED ShopifyQL query that matches Shopify's "Total sales over time" report row-for-row. The per-product table filters out:
- rows with `product_title IS NULL` (custom line items / gift cards / shipping adjustments)
- `sales_channel = 'Return Prime: Order Return'` (refunds masquerading as sales-channel rows)

`excludedFromBreakdown` exposes the gap so the UI can call out exactly what didn't make it into the per-product table.

### `GET /api/ads?since=YYYY-MM-DD&until=YYYY-MM-DD`
Meta ads data — one row per ad per day. Direct pass-through of Supabase `primary_table` (ads project).

Returns flat array of rows with 37 metric columns: `account_name`, `ad_id`, `ad_name`, `ad_link`, `campaign_name`, `campaign_id`, `date`, `impressions`, `reach`, `amount_spent_inr`, `outbound_clicks`, `inline_link_clicks`, `purchases`, `conversion_value`, `three_sec_video_plays`, `thruplays`, `add_to_cart`, `initiate_checkout`, `checkout_completion`, plus computed columns (ctr, cpc, cpm, ftewv_count, ncp_count, ltv_reach, ltv_frequency, etc.).

### `GET /api/products`
Full Shopify product catalog with variants, stock, status, vendor, product_type, tags. Paginated via GraphQL cursor. Used by Inventory and Settings tabs.

### `GET /api/inventory-snapshots?since=...&until=...`
Daily inventory snapshots from Supabase ads project `inventory_snapshots` table. One row per product per day with `qty_available`, used by the Sell-Through Rate calculator.

---

## 3. Frontend — Overview Tab (Home)

Default landing page. Shows catalog health and demand funnel at a glance.

### 3.1 KPI Cards (10 cards in 2 rows)

| # | Card | Formula | Click action |
|---|---|---|---|
| 1 | **Total Products** | `count(products with inventory data, excl. combos & user-disabled)` | Clear all filters |
| 2 | **NPD** 🌱 | `count(lifecycle = 'npd' && ≤45 days old)` | Filter to NPD |
| 3 | **In Stock** | `count(stockStatus = 'in_stock')` + visitor-exposure % | Filter to In Stock |
| 4 | **Low / Broken** | `count(stockStatus ∈ {'low_stock', 'broken_stock'})` | Filter to Low/Broken |
| 5 | **Out of Stock** | `count(stockStatus = 'out_of_stock')` + visitor-exposure % | Filter to OOS |
| 6 | **Ad Leakage** *(hidden by default)* | `count(cartAdds > 0 && no checkout && bad stock)` | Filter to leak |
| 7 | **PDP Visitors** | `Σ visitors across all products` | Open Traffic tab |
| 8 | **Total Inventory** | `Σ stock counts (excl. combos)` | Open Inventory tab |
| 9 | **Cart Adds / ATC%** | `Σ cartAdds, then cartAdds/visitors × 100` | Open Traffic tab |
| 10 | **Draft Products** | `count(productStatus = 'DRAFT')` | Filter to Draft |

**Visitor exposure %** = share of total PDP visitors that landed on pages of that stock status. More important than the headcount because dead SKUs that don't run ads don't actually leak revenue.

### 3.2 Table / Card View

- **Search** by product name
- **View toggle**: Table view (dense grid) ↔ Cards view (visual)
- **View mode**: Product-level ↔ Collection-level
- Per-row: Name, status badge, visitors, sessions, ATC%, units sold, sales ₹, last-click UTM

### 3.3 Top of page filters
Date range (from topbar), lifecycle (NPD/Active/Discontinued), stock state, product status (Active/Draft).

---

## 4. Inventory Tab

Detailed catalog with stock breakdowns. **Filters collapsed behind one `⛃ Filters ▾` button** (with active-count badge).

### 4.1 Filters (8 dropdowns inside collapsible panel)
Gender · Lifecycle · Stock · Category · Status · Color · Tag · Type. `↺ Reset` clears all.

### 4.2 KPI cards
Total Products / In Stock / Low / Out / Total Inventory units / Cart Adds / Lifecycle breakdown.

### 4.3 Modes
- **Name mode** — one row per product
- **SKU mode** — drilldown to per-variant SKU
- **Size mode** — pivot to size availability matrix per product

### 4.4 Sell-Through Rate
Toggle button (▦ Sell-Through Rate). When on, recomputes per-day STR using:
- Multi-day: `Σ units sold ÷ Σ daily available` from `inventory_snapshots` × `ORDERS_DATA`
- Single-day: `units sold ÷ available` on that day
Falls back to range-summed sales ÷ daily snapshot when ORDERS_DATA is empty.

### 4.5 Product detail modal
Click any row to expand: per-variant inventory, last-30-day sales chart, daily snapshots, UTM attribution from orders, color split.

---

## 5. Traffic Tab

Sessions analysis with UTM drilldown.

### 5.1 KPI row
Online Store Visitors · Sessions · Bounces · Cart Adds · Reached Checkout · Bounce Rate · Avg Session Duration · Pages/Session.

### 5.2 Top tables
- Top Landing Pages (by sessions, byPath data)
- Top UTM Sources, Mediums, Campaigns, Contents (each clickable for detail modal)
- City breakdown (when session_city is populated — currently null in SAADAA)

### 5.3 Filters
Lifecycle filter (apply NPD/active categorization to landing pages).

---

## 6. Ad Intelligence Tab

Per-ad performance from Supabase `primary_table`.

### 6.1 KPI cards
Ad Spend · Impressions · Avg ROAS · Total Revenue · Avg Hook Rate · Outbound CTR.

### 6.2 Table — one row per ad with:
- Product (resolved via Supabase `product_breakdown` or matched-order title fallback or catalog name match)
- Ad name, account, campaign
- Impressions, Spend ₹
- Hook Rate (`3-sec video views / impressions`)
- Hold Rate (`thru-plays / 3-sec views`)
- ThruPlay Rate
- CTR (outbound clicks / impressions)
- ROAS (conversion_value / spend)
- Matched orders + matched revenue (joined via utm_content ↔ ad name/id)

### 6.3 Search / Sort
Search by ad name, product, account, ad ID. Sort by Spend / Impressions / ROAS / Hook Rate / CTR.

---

## 7. Sales Tab

Shopify sales dataset (ShopifyQL).

### 7.1 KPI cards (Total Sales / Gross / Units / Discounts / Returns / Net)
- Total Sales carries a **green ✓ SHOPIFY MATCH pill** when the unfiltered `grandTotals` query backed it (reconciles to the rupee with Shopify's "Total sales over time" report)
- Sub-label shows `₹X excluded from per-product table` if there's a gap from custom line items / Return Prime channel filters

### 7.2 Filters (collapsed behind `⛃ Filters ▾`)
Quick (Top 20 / High Discount / High Returns) · Day · Product Type · UTM Source · UTM Medium · UTM Campaign · Customer (New / Returning).

### 7.3 Group-By dropdown
Aggregate rows by Product / Product Type / Day / UTM Source / UTM Medium / UTM Campaign / Customer Type / Raw rows.

### 7.4 Sort + View toggle
Sort: Total Sales / Gross / Units / Discounts / Returns. View: Table / Cards.

### 7.5 Charts
- Top 15 — Total Sales (horizontal bar)
- Discounts vs Returns (Top 15, grouped bar)

### 7.6 Product detail modal
Click row → modal with per-day sales chart, UTM breakdown, returns, discounts.

---

## 8. Orders Tab

Per-order list (Shopify orders).

### 8.1 KPI cards
Total Fetched · Active Orders · Revenue · Avg Order Value · Excluded (return_prime / cancelled split) · Payment Pending · Unfulfilled · 1-item / 2-item / 3-item / 4+ item Order distribution.

### 8.2 Filters
- Tag exclusion (multi-select, default excludes `return_prime`, `inf`, `cancelled`)
- Search by order #, customer name, product, SKU
- Sort by date / total / items
- Date range from topbar

### 8.3 Table columns
Order #, Date, Customer, Items (with line-item preview), Quantity, Subtotal, Discount, Total, UTM Source, City, Status.

### 8.4 Order detail modal
Click row → full order: every line item, UTM trio, shipping, financial summary, full URL.

---

## 9. UTM Analysis Tab

Attribution surface — ties Meta ads → landing pages → Shopify orders.

### 9.1 Single view: Ads × Landing Page

(Products and Collections sub-views were removed in commit `e21cb8b`.)

### 9.2 KPI cards
- **Landing Pages** — count + `Σ adCount` in the table
- **Ad Spend** — sum of every ad in `META_ADS_DATA` (linked + unlinked); sub-label calls out `₹X on unlinked`
- **Sessions** — first-click sessions on landing paths
- **Orders** — count, matched by landing-page URL (sub-label: units count)
- **Sales** — sum of line-item revenue on matched orders
- **ROAS** — sales ÷ total spend

### 9.3 Match logic (the heart of this tab)

**Order → Landing page match by URL path**:
```js
order.customAttributes.full_url  →  strip "?…"  →  pathname  →  "/collections/cotton-trousers"
                                                                  ↑↑↑ map lookup ↑↑↑
landing_page.path (= ad.ad_link path)         ←  "/collections/cotton-trousers"
```
Falls back to `_tsh_landing_url` / `landing_site` if `full_url` empty. Falls back to regex strip if URL parsing fails on malformed value. Orders deduped on `id || name` so a multi-match counts once.

### 9.4 Table — one row per landing page
- Landing path + kind badge (PDP / COLLECTION / HOME / OTHER)
- # Ads pointing to this page
- Spend (with mini bar showing % of max)
- Outbound clicks
- Sessions (first-click)
- ATC%
- Orders (matched by URL)
- Units sold
- Sales ₹
- ROAS (color-coded: ≥2x green, 1-2x amber, <1x red)

### 9.5 Charts
- Top Landing Pages by Ad Spend (left)
- Top Landing Pages by Total Sales (right)

### 9.6 Unlinked Ads section
Ads that Meta didn't supply an `ad_link` for (catalog / carousel / dynamic creatives). Editable input on each row with `<datalist>` autocomplete of known landing paths. Manual assignments stored in `MANUAL_AD_LINKS` localStorage and flow back into the main table on next render. Live preview shows what session data would join in for the chosen path.

### 9.7 Click-row detail modal
- Top-line tiles (spend, clicks, impressions, sessions, cart adds, ATC%, orders, units, sales, ROAS, CPC)
- Per-ad table (every ad pointing to this landing page)
- **Orders landed on this page** section — full list of matched orders with UTM trio + line-item preview

---

## 10. Settings Tab

Master inventory toggle.

- Lists all ~707 products with inventory data
- Per-product **Active / Inactive switch**
- Inactive products are excluded from every count + KPI everywhere in the dashboard without changing anything in Shopify
- Persisted to localStorage (`ops_user_disabled_slugs`)
- Search box + bulk select

---

## 11. Topbar + Sidebar

### 11.1 Sidebar (left)
- Saadaa brand mark (circular sand-orange badge + "Saadaa / Ops Dashboard")
- Dark coffee `#5C4033` surface with cream text
- Hamburger toggle (top-right) — collapses to 76px icon column, state in localStorage
- Section labels: **Analytics** (Overview, Inventory, Traffic, Ad Intelligence), **Commerce** (Sales, Orders, UTM Analysis), **Config** (Settings)
- Each nav item: 32px icon tile + tiny category label + Space Grotesk title
- Active state: cream pane + peach inset ring + pale-yellow title + warm-tint icon tile

### 11.2 Topbar (right of sidebar)
- "ⓘ Know more" button — opens a contextual modal explaining the current tab
- Date range picker — single-click presets (Today / Yesterday / Last 7 / Last 30 / Custom range) + calendar
- Server-fetch status indicator

---

## 12. Inventory Snapshot Cron

`snapshot_inventory.py` runs daily (Windows: `snapshot_cron.bat`).

- Queries Shopify Admin GraphQL for every product's current variant-level stock
- Computes per-product `qty_available` (sum of in-stock variants)
- Writes one row per product to Supabase `inventory_snapshots` with `snapshot_date = today (IST)`
- Logs to `logs/snapshot_<date>.log`
- Existing same-date rows are upserted (idempotent re-runs)
- Used by Inventory STR mode to get per-day available counts

---

## 13. Frontend State Management

All state is held in module-level vars in `dashboard_final.html`:

| Variable | Holds |
|---|---|
| `DATA` | Product catalog (Shopify products + computed lifecycle, ATC, etc.) |
| `INVLIVE` | Live Shopify inventory fetch result |
| `META_ADS_DATA` | Ads from `/api/ads` |
| `ORDERS_DATA` | Orders from `/api/orders` |
| `SALES_RAW` | Sales detail rows from `/api/sales` |
| `SALES_BY_PRODUCT` | Per-product aggregates from `/api/sales` |
| `SALES_DATA` | Currently-grouped sales view (rebuilt by `rebuildSalesData()`) |
| `COLLECTION_DATA` | Per-collection traffic/sales rollup |
| `HOME_DATA` | Home page (`/`) sessions data |
| `MANUAL_AD_LINKS` | localStorage map of adId → manual landing path |
| `window._salesGrandTotals` | Unfiltered Shopify "Total sales over time" totals |
| `window._salesExcluded` | Per-field gap between grandTotals and per-product sum |

`fetchAll()` is the master fetch — fires `/api/traffic`, `/api/orders`, `/api/sales`, `/api/ads`, `/api/products`, `/api/inventory-snapshots` in parallel, populates all state, then triggers re-renders of every tab.

---

## 14. Number Formatting

| Function | Purpose | Example |
|---|---|---|
| `fmt(n)` | Whole-number Indian-locale grouping | `21540 → "21,540"`, `1250000 → "12,50,000"` |
| `fmtRs(n)` | ₹ + Indian-locale, decimals only if non-zero fraction | `500 → "₹500"`, `2547108.7 → "₹25,47,108.70"` |
| `fmtRsExact(n)` | ₹ + always 2 decimals | `100 → "₹100.00"` |
| `fmtDate(d)` | Display date | `"2026-05-19" → "19 May 2026"` |

**No abbreviation anywhere** — never `21.5k`, `1.5L`, etc. All numbers shown in full.

KPI values are wrapped in container-query CSS clamp so they shrink to fit on narrow cards instead of breaking mid-digit:
```css
font-size: clamp(1.15rem, 5.8cqi, 1.95rem);
```

---

## 15. Brand System

### Colors (canonical tokens in `:root`)
- **Backgrounds**: `--bg-base #FAF8F5`, `--bg-surface #F5F1EC`, `--bg-ecru #F0EAD6` (★ core), `--bg-white #FFFFFF`
- **Text**: `--text-primary #161513`, `--text-secondary #6E695E`, `--text-tertiary #9A9384`, `--text-link #3B6FD4`
- **Borders**: `--border-primary #E7E2D2`, `--border-soft`, `--border-warm`, `--border-mid`, `--border-strong`
- **Primary accent (CTA only)**: `--accent-yellow #F0C61E`, `--accent-sand #C9A882`, `--accent-amber #E8C87A`, `--accent-warm-tint #FAF1DC`, `--accent-orange #E8A87C`
- **Status (paired)**: success #4F7C4D / #3D9E6B / #ECF1E9 · warning #B57514 / #D9922A / #FAF1DC · error #C0392B / #C94343 / #FDECEA · info #355C7A / #2C5AB8 / #D6E1F5
- **Secondary accents (detail panels only)**: purple #7B4FBF, pink #B54F7A, indigo #3B6FD4
- **Sidebar**: surface `#5C4033` (coffee), active wash `var(--accent-warm-tint)`

### Typography (3 faces only)
- **Inter** — body, descriptions
- **Space Grotesk** — headings, KPI values, all numbers
- **JetBrains Mono** — tiny uppercase labels, badges, code-like data

### Hard Rules (enforced)
1. Ecru-only backgrounds (no cold blue/gray)
2. Yellow `#F0C61E` is CTA only (primary buttons, brand mark, active nav)
3. Status badges use paired tokens (bg+text+border together)
4. Specific transition properties, never `transition: all`
5. Solid `bg-white` on modal containers (no `bg-white/60` — alpha creates stacking context traps)
6. Touch targets ≥ 40px
7. Secondary accents (purple/pink) reserved for detail panels only

---

## 16. File Structure

```
D:\landing_page\
├── api/
│   └── index.py                       # Vercel Flask app (HTTP API)
├── logs/
│   └── snapshot_*.log                 # Inventory snapshot run logs
├── venv/                              # Python virtualenv (gitignored)
├── server.py                          # Local dev server (BaseHTTPServer)
├── snapshot_inventory.py              # Daily inventory cron
├── snapshot_cron.bat                  # Windows Task Scheduler hook
├── dashboard_final.html               # The SPA (everything in one file)
├── BACKEND_GUIDE.md                   # Per-endpoint backend reference
├── PROJECT_DOCUMENTATION.md           # This file
├── requirements.txt                   # Python deps
├── vercel.json                        # Vercel routing config
├── .env                               # Secrets (gitignored)
└── .gitignore
```

---

## 17. Deployment

- **Local dev**: `python server.py` → `http://localhost:5000`
- **Production**: `git push origin master` → Vercel auto-deploys from GitHub. Live at `https://landing-page-three-blush.vercel.app/`
- **Env vars on Vercel**: Settings → Environment Variables. Six vars needed (see Project Snapshot).
- **Vercel routes**: defined in `vercel.json`. Static `dashboard_final.html` served from `/`, API from `/api/*`.

---

## 18. Known Quirks & Workarounds

| Quirk | Workaround |
|---|---|
| ShopifyQL caps responses at 1,000 rows | Split queries into multiple narrower GROUP BYs; use `WITH TOTALS` for grand totals; `LIMIT 10000` where allowed |
| Sales detail GROUP BY with `line_item_id, customer_id` exploded rows past the cap | Dropped those four dims (probe showed they're unused downstream); detail now stays at ~490 rows on busy days |
| Supabase `orders` table missing `created_at` index | Drop `ORDER BY` from server query; sort client-side after fetch |
| PostgREST 1000-row response cap | Range-header pagination loop (Range: 0-999, 1000-1999, …) in `_supabase_get()` |
| `sales_channel = 'Return Prime: Order Return'` doubled sales | `WHERE sales_channel != 'Return Prime: Order Return'` filter in per-product query |
| `product_title IS NULL` rows (custom line items / gift cards) couldn't be grouped | Filter them out of per-product query; `grandTotals` (unfiltered) reconciles totals |
| Meta uses em-dash, `+`, `" - Copy"` in ad names | `_normUtm()` normalizer applied to both sides of utm_content joins |
| Vercel doesn't have `SAADAA_VAR` set | `/api/_env_probe` shows which vars are missing; `_source` tag indicates which path served the request |
| Numbers wrapping mid-digit on narrow cards | Container queries + `font-size: clamp()` to shrink instead of break |
| Sticky `<th>` ghosting row content through | Switched `.dtbl` to `border-collapse: separate` so the shared border doesn't leak |

---

## 19. Cross-platform Reconciliation Notes

| Comparison | Typical delta | Cause |
|---|---|---|
| Shopify orders count vs Shopify "Total sales" CSV orders | exact match | both query the same `orders` underlying table |
| Shopify orders revenue vs `/api/sales grandTotals` | ~0.1% | Return Prime filter excludes a refund row Shopify CSV includes; intentional |
| Meta-reported revenue vs Shopify total | 4-15% under | Post-iOS14 attribution windows; normal |
| Meta purchases count vs Shopify active orders | 10-20% under | Same — Meta misses orders outside its attribution window |

---

## 20. Versions / History (recent major commits)

| Commit | Change |
|---|---|
| `de4a288` | v3.4 — Various |
| `04bf8f5` | UTM Ad Spend KPI now sums all META_ADS_DATA (was linked-only) |
| `dcf0580` | UTM Analysis: orders match by landing-page URL path (was utm_campaign) |
| `0210019` | UTM: collapse to single Orders + Sales per landing page |
| `e21cb8b` | UTM Analysis: remove Products + Collections sub-views |
| `d9b4581` | Sales: drop 4 row-multiplier dims + LIMIT 10000 |
| `a40f387` | Sales: add unfiltered grandTotals query + ✓ SHOPIFY MATCH pill |
| `e117876` | KPI cards: editorial ledger treatment + Space Grotesk values |
| `e03628a` | Apply Saadaa brand color palette v1.0 |
| `4f98dd4` | Sidebar redesign (light cream, circular badge, hamburger) |
| `48305d9` | Sidebar coffee `#5C4033` + unify font stack (drop Roboto) |
| `5cee4de` | Orders REVENUE truncation fix (clamp + bigger minmax) |

---

*Document maintained alongside the code. Update when adding new tabs, KPIs, endpoints, or significantly changing query strategy.*
