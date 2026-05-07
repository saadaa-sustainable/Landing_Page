# Saadaa Dashboard — Session Changes Summary

Reference document capturing all code changes made in this session. Use this when updating the codebase to understand the current architecture and key decisions.

---

## Files touched

| File | Purpose | Status |
| --- | --- | --- |
| `dashboard_final.html` | Single-file frontend (~7400 lines) | Heavily modified |
| `server.py` | Local Python HTTP server (BaseHTTPRequestHandler) | Modified |
| `api/index.py` | Vercel serverless Flask app — mirrors server.py | Modified |
| `.env` | `SHOP_DOMAIN`, `ADMIN_ACCESS_TOKEN`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Already present, do not read |

---

## Architecture: same-origin, no client-side credentials

The dashboard is **served by the same Python backend that proxies the data**, so the frontend uses **relative URLs only** (`/api/...`). All Shopify and Supabase credentials live in `.env` and are only ever read server-side.

**Frontend never:**
- Asks the user for a server URL
- Asks for Supabase URL/key
- Calls Supabase directly

**Backend endpoints** (both `server.py` and `api/index.py`):

| Endpoint | Reads | Source |
| --- | --- | --- |
| `GET /` | — | serves `dashboard_final.html` |
| `GET /api/health` | — | `{status, shop}` |
| `GET /api/traffic?since=&until=` (or `?date=`) | `SHOP_DOMAIN`, `ADMIN_ACCESS_TOKEN` | Shopify Analytics ShopifyQL ⚠ currently rejected by token |
| `GET /api/inventory` | Shopify | All products + variants → flattened color rows |
| `GET /api/sales?since=&until=` | Shopify | **Aggregated from orders** (NOT ShopifyQL) — see Sales fix below |
| `GET /api/orders?since=&until=` | Shopify | Admin GraphQL `orders` query |
| `GET /api/ads?since=&until=` | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` | Proxies Supabase `results_table` |
| `GET /api/inventory-snapshot?date=` | Supabase | Proxies `inventory_snapshots`, falls back to latest with `isFallback: true` |

### `_supabase_get(table, params)` helper

Both `server.py` and `api/index.py` share the same Supabase proxy pattern. `params` is a list of (key, value) tuples (so PostgREST filters like `computed_at=gte.X & computed_at=lte.Y` work).

### Sales fix (critical — non-obvious bug)

ShopifyQL is unavailable for this access token (`Field 'shopifyqlQuery' doesn't exist on type 'QueryRoot'`). `fetch_sales()` was rewritten in **both** `server.py` and `api/index.py` to aggregate from `fetch_orders()` results:

- Reads the **flattened** order shape (`financialStatus`, `cancelled`, `discounts`, `refunded`, `tax`, `lineItems[].title/quantity/price`) — NOT raw GraphQL nodes.
- Skips cancelled / VOIDED orders.
- Distributes order-level discounts / refunds / tax across line items proportionally to gross share.
- Returns: `[{product_title, product_vendor:"", product_type:"", net_items_sold, gross_sales, discounts, returns, net_sales, taxes, total_sales}]`.

Verified live: ~300 products returned for typical date ranges.

### Inventory snapshot fallback

`fetch_inventory_snapshot(date)` returns `{rows, date, requestedDate, isFallback}`. If the requested date has no rows, it queries the latest snapshot date and returns that with `isFallback: true`. Frontend shows a yellow warning when fallback fires.

---

## Frontend architecture (`dashboard_final.html`)

### Master date range — single control

Location: navbar (`<div class="nav-date">`), button labelled `📅 06 May 2026 – 12 May 2026 ▾`.

- **Hidden inputs** drive everything: `#masterSince`, `#masterUntil`. Legacy inputs (`#masterServerUrl`, `#masterDate`, `#salesSince`, `#salesUntil`, `#ordersSince`, `#ordersUntil`, `#shopifyFetchDate`) are kept hidden so older code paths don't crash; `fetchAll()` syncs values to them.
- **Custom popover** (`#dateRangePopover`): preset sidebar (Today, Yesterday, Last 7 Days, Last 30 Days, This Month, Last Month, Lifetime, Custom Range) + two-month calendar. Apply commits to `masterSince`/`masterUntil` and triggers `fetchAll()`. Cancel / Esc / backdrop close.
- State vars: `_drpPending = {start, end}`, `_drpAnchor` (first-click), `_drpLeftMonth`.
- Helpers: `_drpToISO`, `_drpFromISO`, `_drpStartOfDay`, `_drpFirstOfMonth`, `_drpAddMonths`, `setDrpPreset`, `_drpHighlightActivePreset`, `applyDateRange`, `_updateDateRangeBtnLabel`.

### `fetchAll()` — 6-step orchestrator

Sequence: Traffic → Inventory → Orders → Sales → Ads → Sell-Through Snapshot.

Reads `masterSince`/`masterUntil` only. No baseUrl gate. Each individual fetch function (`fetchShopifyTraffic`, `fetchShopifyInventory`, `fetchShopifyOrders`, `fetchShopifySales`, `fetchSupabaseAds`, `fetchSTRSnapshot`) reads `masterSince`/`masterUntil` directly and calls `/api/<endpoint>` (relative URL).

### Auto-fetch on load

Init code defaults `masterSince`/`masterUntil` to **yesterday** and fires `fetchAll()` after a 100ms `setTimeout`. Embedded synthetic data (`SALES_DATA`, traffic/order metrics on `DATA[]`) is wiped first so the dashboard never shows stale sample numbers.

### Removed UI elements

- Per-tab "Data Source" / "Configure" / "Supabase Config" buttons (date-range Apply is the only fetch trigger now).
- Per-tab `← Overview` / `→ Inventory` / `→ Traffic` cross-nav buttons (the navbar tabs handle navigation).
- All six per-tab `ⓘ Know more` buttons (replaced by a single `.nav-info-btn` in the navbar).
- The `<div class="nav-r">` block with `livePip` and `navMeta` (the "Shopify Inv · 822 products · 1:10:45 PM" status). All `document.getElementById('navMeta').textContent = ...` calls are commented out.
- The original "Master Fetch Bar" inside `#page-home`.
- The "Refresh" button in the date area (auto-fire on Apply).

### Single global "Know More" button

`<button class="nav-info-btn" onclick="openTabInfo(_currentPage)">ⓘ Know more</button>` lives in the navbar. `_currentPage` is updated by `goPage(p)`. `TAB_INFO` map (object) holds `{title, body}` per tab. `showInfoModal(title, body)` injects `#tabInfoModal` lazily into `<body>`.

---

## Brand repaint — warm light ecru theme

`:root` was rebuilt per the brand palette PDF. Key tokens:

```css
--bg: #FAF8F5;            /* main page */
--bg-surface: #F5F1EC;    /* sidebar/panels */
--bg-ecru: #F0EAD6;       /* core Saadaa ecru */
--bg-page-alt: #F6F3F1;
--bg-muted: #F0EDE6;
--bg-white: #FFFFFF;      /* solid modals */
--sf: rgba(255,252,248,0.72);  /* glass-card formula */
--bd: #E7E2D2;            /* default border */
--bd-strong: #C9C2AE;
--cta: #F0C61E;           /* yellow — primary CTA only */
--cta-hover: #D9B01A;
--sand: #C9A882;          /* secondary accent */
--warm-tint: #FAF1DC;
--txt: #161513;
--txt-charcoal: #2C2420;
--mu: #9A9384;            /* tertiary */
--mu2: #6E695E;           /* secondary */
/* status pairs from spec */
--in: #3D9E6B; --in-bg: #ECF1E9; --in-text: #4F7C4D;
--low: #D9922A; --low-bg: #FAF1DC; --low-text: #B57514;
--out: #C94343; --out-bg: #FDECEA; --out-text: #C0392B;
--traf: #3B6FD4;
--cart: #7B4FBF;          /* secondary purple — detail panels only */
```

**Brand rules to respect:**
- ECRU is the soul — backgrounds always warm, never cold blue/gray.
- `--cta` yellow is for primary CTAs only: `.ibtn.acc`, brand mark accent strip (`::before`), active tab underline. Don't use it for secondary UI.
- `--sand` (#C9A882) is for secondary accents (hover borders, etc.).
- Modals use solid `--bg-white`, never alpha. Backdrop is `rgba(0,0,0,.55)`.
- `--cart` purple is for detail-panel use only (e.g. attribution chart in product modal).

---

## 19-change PDF (Batches A–E) — what's done

Reference: `MASTER_PROMPT.md.pdf` in Downloads.

| # | Change | Where | Status |
| --- | --- | --- | --- |
| 1 | Stock distribution % inline on hero cards (`47 (78%)`) | `renderHome()` In Stock / Low / Out hero blocks | ✓ |
| 2 | `DD MMM YYYY` date format | `fmtDate(d)` global helper | ✓ |
| 3 | Move date picker to navbar top-right | `.nav-date` block + custom popover | ✓ |
| 4 | Auto-apply on date select; remove Refresh | `applyDateRange()` triggers `fetchAll()` | ✓ |
| 5 | "Know More" per-tab info | Single `.nav-info-btn` in navbar + `TAB_INFO` map + `showInfoModal()` | ✓ (consolidated) |
| 6 | Collection thead aligned to data | `setCollectionThead()` swaps `#homeTheadRow` | ✓ |
| 7 | Aggregated Units Sold + Net Sales per collection | `renderCollectionView()` joins `SALES_DATA` per product | ✓ |
| 8 | Fix Overview filters + drop Stock col | Filter logic in `renderCollectionView`; thead has no Stock col | ✓ |
| 9 | Analytics view collapsed by default | `#analyticsSection style="display:none"` + `toggleAnalytics()` flips icon + label | ✓ |
| 10 | (i) icon next to "Cross-sell Attribution" | Inline `showInfoModal()` button in product detail modal | ✓ |
| 11 | Validation guards in `computeProductAttribution` | Dedupe by order id, filter `quantity > 0`, mismatch uses `validItems` | ✓ |
| 12 | Click-outside-to-close on overlays | `productDetailOverlay`, `adDefOverlay`, `tabInfoModal` all have backdrop click handler | ✓ |
| 13 | Inventory cross-field search | `getINV()` matches name OR colorSku OR color | ✓ |
| 14 | `extractColor(name, variants)` | Optional variants arg, prefers variant option values | ✓ |
| 15 | Tag filter from real product tags (SD/SM/SU prefix) + Type filter | `populateInvTags`, `populateInvProductTypes`, `setIPT`, `invPT` state | ✓ |
| 16 | Card view in Overview / Sales / Orders | `homeViewMode`, `salesViewMode`, `ordersViewMode` + `setHomeView/setSalesView/setOrdersView` toggles + `productCard/saleCard/orderCard` helpers | ✓ |
| 17 | Exact ₹ on Orders KPIs | `fmtRsExact(v)` used in `renderOrders` for Revenue + Avg Order Value | ✓ |
| 18 | Validate `created_at` parsing | `parseOrderDate(raw)` — returns Date or null | ✓ |
| 19 | UI/spacing polish | Global tail CSS at end of `<style>` block | ✓ |

---

## Key globals/vars in dashboard_final.html

```js
let _currentPage = 'home';                              // updated by goPage()
let homF = 'all', homLC = 'all', homCat = 'all';        // Overview filters
let homView = 'product';                                // 'product' | 'collection'
let homeViewMode = 'table';                             // 'table' | 'cards' (Overview)
let salesViewMode = 'table', ordersViewMode = 'table';  // same for Sales / Orders
let invPT = 'all';                                      // Inventory product-type filter
let homeSortKey = 'sessions', homeSortDesc = true;      // Overview table sort
let homCollExpanded = null;                             // Overview drill-into-collection idx
let _drpPending = {start, end}, _drpLeftMonth, _drpAnchor;  // date picker
let _attributionCache = null;                           // cross-sell attribution
let _ordersAutoInterval = null;                         // 60-min orders refresh
const TAB_INFO = {home, inv, traf, ads, sales, orders}; // Know More copy
const COLOR_GROUPS = {...};                             // color-name → group
const SS = { in_stock, low_stock, broken_stock, out_of_stock, unknown }; // stock badges
```

## Key functions to know

| Function | Purpose |
| --- | --- |
| `fetchAll()` | Master orchestrator — 6 sequential fetches |
| `onMasterRangeChange()` | Debounced auto-fetch (used if Apply auto-fires; currently Apply triggers fetchAll directly) |
| `goPage(p)` | Tab switch — also updates `_currentPage` |
| `renderHome()` | Overview product view OR delegates to `renderCollectionView()` |
| `renderCollectionView()` / `renderCollectionProducts(idx)` | Collection list / drill-in |
| `setProductThead()` / `setCollectionThead()` | Swap `#homeTheadRow` between product (8 col) and collection (8 col) layouts |
| `renderInv()`, `renderTraf()`, `renderSales()`, `renderOrders()`, `renderAds()`, `renderSTR()` | Per-tab renderers |
| `renderTable(containerId, rows, view, mode)` | Universal table/card renderer for Inventory + Traffic |
| `renderCards(rows, mode)` | Mode-aware card renderer (`'inv'` / `'traf'` / default) |
| `productCard(r, opts)` / `saleCard(r)` / `orderCard(o, idx)` | Newer card helpers for Overview/Sales/Orders |
| `getINV()` | Filtered inventory rows for current filter state |
| `getTRAF()` | Filtered traffic rows |
| `computeProductAttribution()` | Cross-sell attribution from ORDERS_DATA |
| `getFilteredOrders()` | Orders minus excluded tags (`return_prime, inf, cancelled`) |
| `clearHomeFilters()` | Reset Overview Lifecycle/Stock/Category/search |
| `setHView(v)` | Switch Products ↔ Collections (only toggles `hv-product`/`hv-collection`, leaves Table/Cards alone) |
| `setHomeView/setSalesView/setOrdersView(v)` | Table ↔ Cards toggle per tab |
| `extractColor(name, variants)` | Color group from variant options or name parse |
| `openTabInfo(tab)` / `showInfoModal(title, body)` | Modal-driven Know More |
| `fmtRs(n)` / `fmtRsExact(v)` / `fmtDate(d)` / `parseOrderDate(raw)` | Display helpers |

## Hero panels — all clickable

Every hero panel on Overview has a click action (cursor:pointer + title tooltip):

| Card | Click |
| --- | --- |
| Total Products | `clearHomeFilters()` |
| 🌱 NPD | `setHLC('npd')` |
| In Stock | `setHF('in')` |
| Low / Broken | `setHF('low')` |
| Out of Stock | `setHF('out')` |
| Ad Leakage | `setHF('leak')` (display:none by default) |
| PDP Visitors | `goPage('traf')` |
| Total Inventory | `goPage('inv')` |
| Cart Adds / ATC% | `goPage('traf')` |
| Draft Products | `setHLC('discontinued')` |

Collection view hero: Collections → `setHView('product')`, others → traffic/inventory.

---

## Scroll fix (CSS gotcha)

`.tbl-wrap` / `.prod-tbl-wrap` use `overflow: clip` (not `hidden`) so they keep their rounded corners but don't establish a scroll container. `.tbl-scr` has `overflow-x: auto; overflow-y: clip`. This avoids the double-scrollbar trap that happens when one axis is `auto` (browsers implicitly auto the other).

---

## CSS class reference (most-touched)

| Class | Notes |
| --- | --- |
| `.nav` | Top nav, warm glass `rgba(255,252,248,0.85)`, fixed-height 48px |
| `.brand` | Logo with yellow `::before` accent strip |
| `.ntab.on` | Active tab → `--cta` underline + `--txt` text |
| `.ibtn.acc` | Yellow CTA button — only place yellow appears as a button bg |
| `.nav-info-btn` | Single Know More in navbar |
| `.nav-date-btn` | Date range trigger button |
| `.drp-popover` | Calendar popover (display:grid when `.open`) |
| `.drp-day.range-start/end/in-range` | Calendar selection styling |
| `.kpi` | Solid white card with `--bd` border, top color stripe via `::before` |
| `.hero-panel` | Solid white hero card on Overview |
| `.tbl-wrap` / `.prod-tbl-wrap` / `.tbl-scr` | Table wrappers — use `overflow: clip` |
| `.dtbl` | Main data table |
| `.cards` (legacy) / `.cards-grid` (newer) | Two card-grid systems coexist |
| `.card` (legacy, used by `renderCards`) | Used by Inventory + Traffic card view |
| `.p-card` (newer) | Used by Overview/Sales/Orders card helpers |
| `.info-modal-card` | Solid white modal card per brand spec |
| `.cpanel` | Per-tab config panel (mostly hidden now) |

---

## Things to be careful about

1. **Don't read `.env`** — credentials should be referenced by name only (`SHOP_DOMAIN`, `ADMIN_ACCESS_TOKEN`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`). Project memory at `~/.claude/projects/E--Project-Saadaa-landing-page/memory/feedback_no_env_reads.md`.
2. **Two Python files mirror each other** — `server.py` (local) and `api/index.py` (Vercel). Apply changes to both.
3. **ShopifyQL is rejected by the current access token** — any new endpoint that needs analytics-style aggregation should derive from `fetch_orders()` (or another working source), not ShopifyQL.
4. **Backend mostly survived an earlier file revert; frontend was lost once.** Commit working state regularly. Yesterday's UI work was rebuilt from chat history.
5. **`_currentPage` must be updated by every nav path** — currently only `goPage()` does it. If you add a new way to switch tabs, update it too.
6. **Glass cards (`var(--sf)`) are nearly invisible on the light bg** — use `var(--bg-white)` for solid card backgrounds, `var(--sf)` only for blurred overlays.
7. **`renderTable` and `renderCards`** are shared between Inventory and Traffic. The newer `productCard / saleCard / orderCard` helpers are separate, used by Overview/Sales/Orders. Don't conflate.
8. **`overflow: clip`** is required (not `hidden`) on `.tbl-wrap` / `.prod-tbl-wrap` to avoid double scrollbars. Don't change to `hidden`.
9. **Modal KPIs (`.dsec .krow .kpi`)** have scoped smaller fonts because the product detail modal is narrower than the main page. Don't override globally.
10. **Auto-fetch on init** depends on the backend running on the same origin. If you deploy elsewhere, ensure CORS is configured (already is in both server.py and api/index.py).

---

## Quick "where do I edit X" guide

| To change… | Edit… |
| --- | --- |
| Backend API logic | `server.py` (local) AND `api/index.py` (Vercel) |
| Color tokens | `:root` near top of `<style>` in `dashboard_final.html` |
| Tab info copy | `TAB_INFO` map in `dashboard_final.html` |
| Date picker presets | `setDrpPreset(preset)` switch statement |
| What `fetchAll()` runs | The 6-step block in `fetchAll()` function |
| Hero panel click actions | Inline `onclick` in `renderHome()` HTML strings |
| Collection card content | `renderCollectionView()` cards section |
| Card view stats per mode | `renderCards(rows, mode)` mode branches |
| Status badge colors | `SS = { ... }` constant |
| Number formatting | `fmt`, `fmtRs`, `fmtRsExact`, `fmtDate` near top of script |
| Spacing/polish overrides | The "Global spacing & polish" block at the end of `<style>` |
| Scoped modal KPI fonts | `.dsec .krow .kpi/.kv/.kl/.ks` inside the global polish block |

---

## Recent CSS spacing fixes (last commit-worthy chunk)

- `.kpi { min-width: 0; }` (lets grid items shrink)
- `.kpi .kv { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }`
- `#ordersKpis { grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)) !important; gap: 12px; padding: 14px 22px; }`
- `#ordersKpis .kv { font-size: 1.5rem; }`
- `.dsec .krow .kpi { padding: 12px 14px; }` — modal KPI scope
- `.dsec .krow .kv { font-size: 1.05rem; line-height: 1.15; }`
- `.dsec .krow .kl { font-size: .48rem; }`
- `@media (max-width: 560px) { .dsec .krow.k4/.k5/.k6 → 2 cols }`

---

*Generated at the end of the May 5–6, 2026 session.*
