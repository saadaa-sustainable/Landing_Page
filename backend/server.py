\
"""
saadaa traffic server
─────────────────────
Reads credentials from .env, fetches ShopifyQL data, serves it to the dashboard.

Usage:
    pip install requests python-dotenv
    python server.py

Then in the dashboard:
    Traffic → ⚙ Data Source → Vercel API URL → http://localhost:5000
    Pick a date → Fetch →
"""

import json
import re
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import requests
from dotenv import load_dotenv

# Force UTF-8 output so Hindi/emoji chars don't crash on Windows cp1252 terminals
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── Load credentials from .env at the PROJECT ROOT (one level above backend/) ──
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

SHOP_DOMAIN          = os.getenv("SHOP_DOMAIN")
ADMIN_ACCESS_TOKEN   = os.getenv("ADMIN_ACCESS_TOKEN")
SUPABASE_URL         = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()

# Second Supabase project (different project in the same org) — holds the
# `sessions`, `orders`, and `order_line_items` tables. When SAADAA_VAR /
# SAADAA_KEY are set, fetch_traffic / fetch_orders read from this project
# first and fall back to Shopify only if the call fails (so the dashboard
# never goes blank). Names chosen to avoid colliding with the existing
# SUPABASE_* vars that already point at the ads project (primary_table).
SAADAA_VAR = (os.getenv("SAADAA_VAR") or "").strip().rstrip("/")
SAADAA_KEY = (os.getenv("SAADAA_KEY") or "").strip()

if not SHOP_DOMAIN or not ADMIN_ACCESS_TOKEN:
    raise SystemExit(
        "❌  Missing credentials.\n"
        "    Add SHOP_DOMAIN and ADMIN_ACCESS_TOKEN to your .env file."
    )


# ── Shopify fetch ──────────────────────────────────────────────────────────────

def fetch_traffic(since: str, until: str) -> dict:
    """Traffic data source — Supabase `sessions` table first, Shopify fallback.

    Source order:
      1. If SAADAA_VAR + SAADAA_KEY are set, query the
         Supabase `sessions` table (filtered by session_date BETWEEN since
         AND until). Cheaper, faster, no Shopify rate limits.
      2. On any failure (creds missing, network, table empty), fall back to
         the ShopifyQL direct path below.

    Returns { byPath, rows, totals, _source }. See _from_shopify path below
    for the dimension-by-dimension query semantics.
    """
    if SAADAA_VAR and SAADAA_KEY:
        try:
            payload = fetch_traffic_from_supabase(since, until)
            if payload.get("byPath") or payload.get("rows"):
                return payload
            print("  [Traffic] Supabase returned empty, falling back to Shopify…")
        except Exception as e:
            print(f"  [Traffic] Supabase fetch failed ({e}), falling back to Shopify…")
    return _fetch_traffic_from_shopify(since, until)


def _fetch_traffic_from_shopify(since: str, until: str) -> dict:
    """Two-query traffic fetch:

      • byPath  -- GROUP BY landing_page_type, landing_page_path, day
                   3 dims only, so ~700 landing pages × N days fits inside
                   Shopify's 1000-row response cap. This is the authoritative
                   per-landing-page total (covers 99%+ of true day totals).

      • rows    -- GROUP BY landing_page_type, landing_page_path, day,
                   utm_source, utm_medium, utm_campaign, utm_content,
                   utm_term, session_city. The full 9-dim breakdown used
                   for UTM / city drill-down modals. Capped at 1000 rows
                   by Shopify so totals are NOT reliable here — only use
                   for relative comparisons inside a single page.

    Returns { "byPath": [...], "rows": [...], "totals": {...} }.
    """

    ql_path = (
        "FROM sessions "
        "SHOW online_store_visitors, sessions, sessions_with_cart_additions, "
        "added_to_cart_rate, bounces, average_session_duration, "
        "pageviews_per_session, sessions_that_reached_checkout "
        "WHERE landing_page_path IS NOT NULL "
        "AND human_or_bot_session IN ('human', 'bot') "
        "GROUP BY landing_page_type, landing_page_path, day "
        "WITH TOTALS "
        f"SINCE {since} UNTIL {until} "
        "ORDER BY sessions DESC"
    )

    ql_detail = (
        "FROM sessions "
        "SHOW online_store_visitors, sessions, sessions_with_cart_additions, "
        "added_to_cart_rate, bounces, average_session_duration, "
        "pageviews_per_session, sessions_that_reached_checkout "
        "WHERE landing_page_path IS NOT NULL "
        "AND human_or_bot_session IN ('human', 'bot') "
        "GROUP BY landing_page_type, landing_page_path, day, "
        "utm_source, utm_medium, utm_campaign, utm_content, utm_term, "
        "session_city "
        "WITH TOTALS "
        f"SINCE {since} UNTIL {until} "
        "ORDER BY sessions DESC"
    )

    def _run(ql: str):
        query = """
        query($ql: String!) {
          shopifyqlQuery(query: $ql) {
            tableData { columns { name dataType } rows }
            parseErrors
          }
        }
        """
        resp = requests.post(
            f"https://{SHOP_DOMAIN}/admin/api/2025-10/graphql.json",
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": ADMIN_ACCESS_TOKEN,
            },
            json={"query": query, "variables": {"ql": ql}},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise ValueError(f"GraphQL error: {data['errors']}")
        sqr = data.get("data", {}).get("shopifyqlQuery", {})
        if sqr.get("parseErrors"):
            raise ValueError(f"ShopifyQL parse error: {sqr['parseErrors']}")
        table = sqr.get("tableData", {})
        columns = table.get("columns", [])
        raw_rows = table.get("rows", [])
        if not raw_rows:
            return []
        if isinstance(raw_rows[0], dict):
            result = list(raw_rows)
        else:
            if not columns:
                return []
            col_names = [c.get("name", f"col_{i}") for i, c in enumerate(columns)]
            result = [
                {col_names[i]: v for i, v in enumerate(row) if i < len(col_names)}
                for row in raw_rows if isinstance(row, list)
            ]
        # Compute bounce_rate from bounces / sessions instead of using Shopify's value
        for row in result:
            bounces = float(row.get('bounces') or 0)
            sessions = float(row.get('sessions') or 0)
            row['bounce_rate'] = round(bounces / sessions * 100, 2) if sessions > 0 else 0
        return result

    by_path = _run(ql_path)
    detail = _run(ql_detail)

    # Authoritative day-level totals — one tiny query, no GROUP BY, no
    # landing_page_path filter. This is the truth the dashboard's KPI cards
    # should display (avoids over-counting visitors via per-page de-dup
    # quirks and under-counting checkouts whose session had a null
    # landing_page_path: cart-recovery emails, customer-account flows, etc.).
    truth = _run_truth_query(since, until)
    print(f"  [Traffic←Shopify] byPath={len(by_path)} rows · detail={len(detail)} rows · totals={'yes' if truth else 'no'}")
    return {"byPath": by_path, "rows": detail, "totals": truth, "_source": "shopify"}


def _run_truth_query(since: str, until: str) -> dict:
    """Account-level day totals across ALL sessions (no landing_page filter,
    no GROUP BY). Returns a single-row dict with the 8 SHOW fields."""
    ql = (
        "FROM sessions "
        "SHOW online_store_visitors, sessions, sessions_with_cart_additions, "
        "bounces, average_session_duration, pageviews_per_session, "
        "sessions_that_reached_checkout, added_to_cart_rate "
        "WHERE human_or_bot_session IN ('human', 'bot') "
        f"SINCE {since} UNTIL {until}"
    )
    query = """
    query($ql: String!) {
      shopifyqlQuery(query: $ql) {
        tableData { columns { name dataType } rows }
        parseErrors
      }
    }
    """
    try:
        resp = requests.post(
            f"https://{SHOP_DOMAIN}/admin/api/2025-10/graphql.json",
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": ADMIN_ACCESS_TOKEN,
            },
            json={"query": query, "variables": {"ql": ql}},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            return {}
        sqr = data.get("data", {}).get("shopifyqlQuery", {})
        if sqr.get("parseErrors"):
            return {}
        rows = (sqr.get("tableData") or {}).get("rows") or []
        if not rows:
            return {}
        row = rows[0] if isinstance(rows[0], dict) else {}
        bounces = float(row.get('bounces') or 0)
        sessions = float(row.get('sessions') or 0)
        row['bounce_rate'] = round(bounces / sessions * 100, 2) if sessions > 0 else 0
        return row
    except Exception:
        return {}


# ── Inventory fetch ──────────────────────────────────────────────────────────

SIZES_ORDER = ['XS', 'S', 'M', 'L', 'XL', '2XL', '3XL', '4XL', '5XL']

SIZE_MAP = {
    'XXS': 'XS', 'EXTRA SMALL': 'XS',
    'SMALL': 'S',
    'MEDIUM': 'M', 'MED': 'M',
    'LARGE': 'L',
    'EXTRA LARGE': 'XL',
    'XXL': '2XL', 'XXXL': '3XL', 'XXXXL': '4XL', 'XXXXXL': '5XL',
}

def normalize_size(val: str) -> str:
    v = val.strip().upper()
    return SIZE_MAP.get(v, v)

def extract_color_from_options(variant: dict) -> str:
    """Extract color option value from a variant's selectedOptions."""
    for opt in variant.get("selectedOptions", []):
        name = opt.get("name", "").lower()
        if name == "color" or name == "colour":
            return opt.get("value", "").strip()
    return ""

def extract_size_from_options(variant: dict) -> str:
    """Extract size option value from a variant's selectedOptions."""
    for opt in variant.get("selectedOptions", []):
        if opt.get("name", "").lower() == "size":
            return normalize_size(opt.get("value", ""))
    return ""

def sku_prefix_to_gender(sku: str) -> str:
    """Map SKU prefix to gender: SD→Women, SM→Men, SU→Unisex."""
    s = sku.upper()[:2] if sku else ""
    if s == "SD": return "Women"
    if s == "SM": return "Men"
    if s == "SU": return "Unisex"
    return ""

def compute_stock_status(sizes: dict) -> str:
    active = [s for s in SIZES_ORDER if sizes.get(s) is not None]
    if not active:
        return "unknown"
    total = sum(sizes[s] for s in active)
    zeros = sum(1 for s in active if sizes[s] == 0)
    if zeros == len(active):
        return "out_of_stock"
    if zeros >= len(active) / 2:
        return "broken_stock"
    if total > 0:
        return "in_stock"
    return "unknown"


def strip_size_from_sku(sku: str) -> str:
    """Remove size suffix from SKU. E.g. SDCPBL_S → SDCPBL, SDCPBL-M → SDCPBL."""
    if not sku:
        return sku
    # Try stripping after underscore or hyphen
    for sep in ['_', '-']:
        if sep in sku:
            parts = sku.rsplit(sep, 1)
            suffix = parts[-1].upper().strip()
            if suffix in ('XS', 'S', 'M', 'L', 'XL', '2XL', '3XL', '4XL', '5XL',
                          'XXS', 'XXL', 'XXXL', 'SMALL', 'MEDIUM', 'LARGE',
                          'FREE', 'FREESIZE', 'FREE SIZE', 'OS', 'ONESIZE'):
                return parts[0]
    return sku


def fetch_inventory() -> list:
    """
    Fetch ALL products from Shopify, split into one row per color variant.
    Each row has: product name + color, colorSku, per-size stock, gender, category.
    """

    query = """
    query($cursor: String) {
      products(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          title
          handle
          status
          productType
          tags
          onlineStoreUrl
          createdAt
          variants(first: 100) {
            nodes {
              id
              title
              sku
              inventoryQuantity
              selectedOptions { name value }
            }
          }
        }
      }
    }
    """

    all_products = []
    cursor = None
    page = 0

    while True:
        page += 1
        variables = {"cursor": cursor} if cursor else {}

        resp = requests.post(
            f"https://{SHOP_DOMAIN}/admin/api/2025-10/graphql.json",
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": ADMIN_ACCESS_TOKEN,
            },
            json={"query": query, "variables": variables},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("errors"):
            raise ValueError(f"GraphQL error: {data['errors']}")

        products_data = data.get("data", {}).get("products", {})
        nodes = products_data.get("nodes", [])
        page_info = products_data.get("pageInfo", {})

        all_products.extend(nodes)
        print(f"  [Inventory] Page {page}: {len(nodes)} products (total: {len(all_products)})")

        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break

        if page >= 40:
            print("  [Inventory] Hit 40-page limit.")
            break

    # ── Group variants by color within each product ──
    result = []
    for p in all_products:
        handle = p.get("handle", "")
        title = p.get("title", "")
        status = p.get("status", "ACTIVE")
        product_type = p.get("productType", "")
        tags = p.get("tags", [])
        created_at = (p.get("createdAt") or "")[:10]
        url = p.get("onlineStoreUrl") or ""
        variants = p.get("variants", {}).get("nodes", [])

        # Determine category from productType or tags
        category = product_type or ""
        is_discontinued = False
        tags_lower = [t.lower().strip() for t in tags]
        for tag in tags:
            tl = tag.lower().strip()
            if tl in ["topwear", "bottomwear", "dress", "co-ord", "kurta",
                       "shirt", "pant", "women topwear", "women bottomwear",
                       "men topwear", "men bottomwear", "women dress"]:
                category = tag
        # Check for discontinued tag
        if any(t in ["discontinued", "disc", "discontinue", "disc."] for t in tags_lower):
            is_discontinued = True

        # Check for excluded/old products that shouldn't appear in counts
        is_excluded = any(t in [
            "old saadaa product", "old saadaa", "old product", "archive",
            "archived", "hidden", "exclude", "excluded", "old"
        ] for t in tags_lower)

        # Group variants by color
        # color_groups = { "Black": [ {sku, size, qty}, ... ], "White": [...] }
        color_groups = {}
        for v in variants:
            color = extract_color_from_options(v) or "Default"
            size = extract_size_from_options(v)
            sku = v.get("sku") or ""
            qty = v.get("inventoryQuantity", 0) or 0

            if color not in color_groups:
                color_groups[color] = {"variants": [], "sku": ""}
            color_groups[color]["variants"].append({
                "size": size, "qty": qty, "sku": sku
            })
            # Use first non-empty SKU for this color group
            if sku and not color_groups[color]["sku"]:
                color_groups[color]["sku"] = sku

        # If no color option exists, treat entire product as one row
        if not color_groups:
            color_groups["Default"] = {"variants": [], "sku": ""}
            for v in variants:
                size = extract_size_from_options(v)
                sku = v.get("sku") or ""
                qty = v.get("inventoryQuantity", 0) or 0
                color_groups["Default"]["variants"].append({
                    "size": size, "qty": qty, "sku": sku
                })
                if sku and not color_groups["Default"]["sku"]:
                    color_groups["Default"]["sku"] = sku

        # Create one row per color
        for color, group in color_groups.items():
            color_sku = strip_size_from_sku(group["sku"])
            gender = sku_prefix_to_gender(color_sku)

            # Build per-size stock for this color
            sizes = {s: None for s in SIZES_ORDER}
            for var in group["variants"]:
                sz = var["size"]
                if sz and sz in sizes:
                    sizes[sz] = (sizes[sz] or 0) + var["qty"]

            active_sizes = [s for s in SIZES_ORDER if sizes[s] is not None]
            total_stock = sum(sizes[s] for s in active_sizes) if active_sizes else 0
            stock_status = compute_stock_status(sizes)

            # Product name with color
            display_name = f"{title} - {color}" if color != "Default" else title

            result.append({
                "slug": handle,
                "name": display_name,
                "colorSku": color_sku,
                "color": color,
                "gender": gender,
                "productStatus": status,
                "category": category,
                "productLink": url,
                "sizes": sizes,
                "totalStock": total_stock,
                "stockStatus": stock_status,
                "createdAt": created_at,
                "hasInvData": len(active_sizes) > 0,
                "discontinued": is_discontinued,
                "excluded": is_excluded,
                "tags": tags,
            })

    print(f"  [Inventory] Done. {len(all_products)} products → {len(result)} color variants.")
    return result


# ── Sales fetch ──────────────────────────────────────────────────────────────

SALES_NUMERIC_FIELDS = (
    "net_items_sold",
    "gross_sales",
    "discounts",
    "returns",
    "net_sales",
    "taxes",
    "total_sales",
)

SALES_DIMENSION_FIELDS = (
    "day",
    "product_title",
    "product_type",
    # Session-level utm_* columns are NOT exposed by the Admin GraphQL sales
    # dataset (Shopify's report editor resolves them via a session join that
    # the API doesn't surface). The order-level order_utm_* columns ARE
    # available and carry the last-click attribution that's stamped on the
    # order at checkout — which is what the dashboard reads via field aliases.
    "order_utm_campaign",
    "order_utm_content",
    "order_utm_medium",
    "order_utm_source",
    "order_utm_term",
    "new_or_returning_customer",
    # __totals columns Shopify appends because of WITH TOTALS
    "net_items_sold__totals",
    "gross_sales__totals",
    "discounts__totals",
    "returns__totals",
    "net_sales__totals",
    "taxes__totals",
    "total_sales__totals",
)

def _shopifyql(ql: str) -> dict:
    """Run a ShopifyQL query through the GraphQL Admin API and return tableData."""
    query = """
    query($ql: String!) {
      shopifyqlQuery(query: $ql) {
        tableData { columns { name dataType } rows }
        parseErrors
      }
    }
    """
    resp = requests.post(
        f"https://{SHOP_DOMAIN}/admin/api/2025-10/graphql.json",
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": ADMIN_ACCESS_TOKEN,
        },
        json={"query": query, "variables": {"ql": ql}},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise ValueError(f"GraphQL error: {data['errors']}")
    sqr = (data.get("data") or {}).get("shopifyqlQuery") or {}
    parse_errors = sqr.get("parseErrors") or []
    if parse_errors:
        raise ValueError(f"ShopifyQL parse error: {parse_errors}")
    return sqr.get("tableData") or {}


def _shopifyql_rows(table: dict) -> list:
    columns = table.get("columns") or []
    raw_rows = table.get("rows") or []
    if not raw_rows:
        return []
    if isinstance(raw_rows[0], dict):
        return list(raw_rows)
    if not columns:
        return []
    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(columns)]
    out = []
    for row in raw_rows:
        if isinstance(row, list):
            out.append({col_names[i]: v for i, v in enumerate(row) if i < len(col_names)})
    return out


def fetch_sales(since: str, until: str) -> dict:
    """
    Direct ShopifyQL fetch — returns BOTH:
      * rows: per-(day × product × order_utm × customer × line_item) detail
        (the granularity the user wants for UTM Analysis + raw breakdown).
      * byProduct: server-aggregated per-product totals (one row per product,
        no LIMIT issues — matches Shopify's "Total sales by product" report exactly).
    Dashboard uses byProduct for the unfiltered per-product table so the numbers
    always match the Shopify Analytics screen; raw rows feed UTM Analysis and
    re-aggregation when the user applies dimension filters.
    """
    print(f"  [Sales] Fetching ShopifyQL sales {since}..{until}...")

    # Matches Shopify's "Total sales by product" report semantics.
    # Notes vs the report-editor query:
    #   - utm_source/medium/campaign/content/term are session-level fields not
    #     exposed by the sales dataset in the Admin GraphQL API. The Prakash
    #     editor resolves them via a hidden session join; the API doesn't.
    #   - LAST_CLICK_ATTRIBUTION is dropped because the API requires an
    #     attributable metric to be SELECTed, and none of the seven metrics in
    #     SHOW are selectable in their __last_click form. Attribution still
    #     comes through via order_utm_* (the UTMs Shopify stamps on the order
    #     at checkout — i.e. last-click already baked in).
    # Detail query — slim 9-dim GROUP BY + explicit LIMIT 10000. The previous
    # 13-dim version (line_item_id, customer_id, customer_last_order_date,
    # customer_number_of_orders) was a row-count bomb that silently hit the
    # default 1000-row cap on busy days. Probe on 2026-05-19 confirmed:
    #   - 13 dims, no LIMIT  → 1000 rows (CAPPED, totals 40% short)
    #   - 13 dims, LIMIT 10k → 2546 rows (totals exact)
    #   -  9 dims, no LIMIT  →  490 rows (totals exact, 5x smaller)
    # The four dropped dims aren't consumed downstream — they were just
    # row-multipliers. Keeping LIMIT 10000 as a safety net.
    ql_detail = (
        "FROM sales "
        "SHOW net_items_sold, gross_sales, discounts, returns, net_sales, taxes, total_sales "
        "WHERE product_title IS NOT NULL "
        "AND sales_channel != 'Return Prime: Order Return' "
        "GROUP BY day, product_title, product_type, "
        "order_utm_campaign, order_utm_content, order_utm_medium, order_utm_source, order_utm_term, "
        "new_or_returning_customer "
        "WITH TOTALS "
        f"SINCE {since} UNTIL {until} "
        "ORDER BY day ASC "
        "LIMIT 10000 "
    )

    # Per-product totals query — server-aggregates so the per-product table
    # always matches Shopify exactly, regardless of how big the detail set is.
    ql_byproduct = (
        "FROM sales "
        "SHOW net_items_sold, gross_sales, discounts, returns, net_sales, taxes, total_sales "
        "WHERE product_title IS NOT NULL "
        "AND sales_channel != 'Return Prime: Order Return' "
        "GROUP BY product_title, product_type "
        "WITH TOTALS "
        f"SINCE {since} UNTIL {until} "
        "ORDER BY total_sales DESC "
    )

    # Grand-totals query — UNFILTERED so it matches Shopify's "Total sales
    # over time" report exactly. The per-product query above filters out
    # orphan rows (product_title NULL: custom line items, gift cards) and
    # Return Prime refunds, which keeps the table clean but undershoots the
    # Shopify total by however much those rows contributed. The UI uses this
    # for top-line KPIs and labels the gap so it's visible.
    ql_grand = (
        "FROM sales "
        "SHOW gross_sales, discounts, returns, net_sales, taxes, total_sales, net_items_sold "
        f"SINCE {since} UNTIL {until} "
    )

    detail_rows = _shopifyql_rows(_shopifyql(ql_detail))
    by_product_rows = _shopifyql_rows(_shopifyql(ql_byproduct))
    grand_rows = _shopifyql_rows(_shopifyql(ql_grand))

    def _cast(rows):
        for row in rows:
            for f in SALES_NUMERIC_FIELDS:
                v = row.get(f)
                if v is None or v == "":
                    row[f] = 0
                    continue
                try:
                    row[f] = float(v)
                except (TypeError, ValueError):
                    row[f] = 0
            # Also cast any __totals columns Shopify appends
            for f in SALES_NUMERIC_FIELDS:
                t = row.get(f + "__totals")
                if t is None or t == "":
                    row[f + "__totals"] = 0
                else:
                    try:
                        row[f + "__totals"] = float(t)
                    except (TypeError, ValueError):
                        row[f + "__totals"] = 0
            for f in SALES_DIMENSION_FIELDS:
                if f in row and row[f] is None:
                    row[f] = ""

    _cast(detail_rows)
    _cast(by_product_rows)
    _cast(grand_rows)

    grand_totals = {f: 0.0 for f in SALES_NUMERIC_FIELDS}
    if grand_rows:
        for f in SALES_NUMERIC_FIELDS:
            grand_totals[f] = float(grand_rows[0].get(f) or 0)
    byp_sums = {f: sum(float(r.get(f) or 0) for r in by_product_rows) for f in SALES_NUMERIC_FIELDS}
    excluded = {f: round(grand_totals[f] - byp_sums[f], 2) for f in SALES_NUMERIC_FIELDS}

    print(f"  [Sales] detail={len(detail_rows)} rows · byProduct={len(by_product_rows)} rows · grand_total_sales={grand_totals.get('total_sales', 0):,.2f} · excluded_total={excluded.get('total_sales', 0):,.2f}")
    return {
        "rows": detail_rows,
        "byProduct": by_product_rows,
        "grandTotals": grand_totals,
        "excludedFromBreakdown": excluded,
    }


# ── HTTP server ────────────────────────────────────────────────────────────────

# ── Orders fetch ──────────────────────────────────────────────────────────────

def fetch_orders(since: str, until: str) -> list:
    """Orders data source — Supabase `orders` + `order_line_items` first,
    Shopify Admin GraphQL fallback. Same output shape regardless of source
    so the dashboard JS doesn't have to care which one ran.
    """
    if SAADAA_VAR and SAADAA_KEY:
        try:
            rows = fetch_orders_from_supabase(since, until)
            if rows:
                return rows
            print("  [Orders] Supabase returned 0 rows — falling back to Shopify just in case…")
        except Exception as e:
            print(f"  [Orders] Supabase fetch failed ({e}), falling back to Shopify…")
    return _fetch_orders_from_shopify(since, until)


def _fetch_orders_from_shopify(since: str, until: str) -> list:
    """
    Fetch orders from Shopify Admin GraphQL API with full details.
    Returns list of order dicts with line items, customer, custom attributes.
    """
    all_orders = []
    current_cursor = None
    max_pages = 100  # 50 * 100 = 5000 orders max

    for page in range(max_pages):
        after_clause = f', after: "{current_cursor}"' if current_cursor else ""
    
        # Shopify query filter: use simple date strings
        query_filter = f"created_at:>={since} created_at:<={until}"

        gql = """
query {
  orders(first: 100, query: \"""" + query_filter + """\", sortKey: CREATED_AT, reverse: true""" + after_clause + """) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      name
      createdAt
      displayFinancialStatus
      displayFulfillmentStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      subtotalPriceSet { shopMoney { amount } }
      totalDiscountsSet { shopMoney { amount } }
      totalShippingPriceSet { shopMoney { amount } }
      totalTaxSet { shopMoney { amount } }
      totalRefundedSet { shopMoney { amount } }
      discountCodes
      note
      tags
      cancelledAt
      shippingAddress { firstName lastName phone city province country zip }
      lineItems(first: 20) {
        nodes {
          title
          variantTitle
          sku
          quantity
          originalUnitPriceSet { shopMoney { amount } }
          image { url }
        }
      }
      customAttributes { key value }
      paymentGatewayNames
      sourceName
    }
  }
}"""
        print(f"  [Orders] Fetching page {page+1}...")
        try:
            resp = requests.post(
                f"https://{SHOP_DOMAIN}/admin/api/2025-10/graphql.json",
                headers={"Content-Type": "application/json", "X-Shopify-Access-Token": ADMIN_ACCESS_TOKEN},
                json={"query": gql}, timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [Orders] HTTP error: {e}")
            raise

        if data.get("errors"):
            print(f"  [Orders] GraphQL errors: {data['errors']}")
            raise ValueError(f"GraphQL error: {data['errors']}")

        orders_data = data.get("data", {}).get("orders", {})
        if not orders_data:
            print(f"  [Orders] No orders data in response. Full response keys: {list(data.get('data', {}).keys())}")
            break

        nodes = orders_data.get("nodes", [])
        page_info = orders_data.get("pageInfo", {})

        for o in nodes:
            def _money(order, field):
                try: return float(order.get(field, {}).get("shopMoney", {}).get("amount", 0))
                except: return 0

            items = []
            for li in o.get("lineItems", {}).get("nodes", []):
                try:
                    items.append({
                        "title": li.get("title", ""), "variantTitle": li.get("variantTitle", ""),
                        "sku": li.get("sku", ""), "quantity": li.get("quantity", 0),
                        "price": float((li.get("originalUnitPriceSet") or {}).get("shopMoney", {}).get("amount", 0)),
                        "image": (li.get("image") or {}).get("url", ""),
                    })
                except Exception as e:
                    print(f"  [Orders] Skipping line item: {e}")
            cust = o.get("shippingAddress") or {}
            addr = cust
            attrs = {}
            for a in (o.get("customAttributes") or []):
                try: attrs[a.get("key", "")] = a.get("value", "")
                except: pass

            all_orders.append({
                "id": o.get("id", ""), "name": o.get("name", ""), "createdAt": o.get("createdAt", ""),
                "financialStatus": o.get("displayFinancialStatus", ""),
                "fulfillmentStatus": o.get("displayFulfillmentStatus", ""),
                "total": _money(o, "totalPriceSet"), "subtotal": _money(o, "subtotalPriceSet"),
                "discounts": _money(o, "totalDiscountsSet"), "shipping": _money(o, "totalShippingPriceSet"),
                "tax": _money(o, "totalTaxSet"), "refunded": _money(o, "totalRefundedSet"),
                "currency": ((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("currencyCode", "INR"),
                "discountCodes": o.get("discountCodes", []), "note": o.get("note", ""),
                "tags": o.get("tags", []), "cancelled": o.get("cancelledAt") is not None,
                "customer": {
                    "name": f"{cust.get('firstName', '')} {cust.get('lastName', '')}".strip(),
                    "email": "", "phone": cust.get("phone", ""),
                    "ordersCount": 0,
                },
                "address": {"city": addr.get("city", ""), "province": addr.get("province", ""),
                             "country": addr.get("country", ""), "zip": addr.get("zip", "")},
                "lineItems": items, "itemCount": sum(li["quantity"] for li in items),
                "customAttributes": attrs,
                "paymentGateway": ", ".join(o.get("paymentGatewayNames", [])),
                "source": o.get("sourceName", ""),
            })

        print(f"  [Orders] Page {page+1}: {len(nodes)} orders (total: {len(all_orders)})")
        if not page_info.get("hasNextPage", False):
            break
        current_cursor = page_info.get("endCursor")

    print(f"  [Orders] Done. {len(all_orders)} orders for {since} to {until}.")
    return all_orders


# ── Supabase proxy fetch ─────────────────────────────────────────────────────

def _supabase_get(table: str, params: list) -> list:
    """GET from Supabase REST, paginated.

    Supabase enforces a server-side row cap (PostgREST max-rows = 1000 on
    the Free tier). Neither a Range header nor ?limit= can exceed it.
    Solution: page through with Range: <offset>-<offset+999> until a page
    returns fewer than 1000 rows.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("Supabase not configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY to .env")
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    base_headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Range-Unit": "items",
    }
    PAGE = 1000
    out = []
    offset = 0
    while True:
        headers = dict(base_headers)
        headers["Range"] = f"{offset}-{offset + PAGE - 1}"
        r = requests.get(url, headers=headers, params=params, timeout=60)
        if r.status_code not in (200, 206):
            r.raise_for_status()
        batch = r.json() or []
        if not isinstance(batch, list):
            return batch
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
        if offset >= 100000:  # safety stop
            break
    return out


def _supabase_data_get(table: str, params: list) -> list:
    """Paginated GET against the SECOND Supabase project (the one holding
    `sessions`, `orders`, `order_line_items`). Falls back to raising
    ValueError when env vars aren't set so the caller can degrade gracefully.
    """
    if not SAADAA_VAR or not SAADAA_KEY:
        raise ValueError("Second Supabase project not configured. "
                         "Add SAADAA_VAR and SAADAA_KEY to .env")
    url = f"{SAADAA_VAR}/rest/v1/{table}"
    base_headers = {
        "apikey": SAADAA_KEY,
        "Authorization": f"Bearer {SAADAA_KEY}",
        "Content-Type": "application/json",
        "Range-Unit": "items",
    }
    PAGE = 1000
    out = []
    offset = 0
    while True:
        headers = dict(base_headers)
        headers["Range"] = f"{offset}-{offset + PAGE - 1}"
        r = requests.get(url, headers=headers, params=params, timeout=60)
        if r.status_code not in (200, 206):
            r.raise_for_status()
        batch = r.json() or []
        if not isinstance(batch, list):
            return batch
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
        if offset >= 100000:
            break
    return out


def fetch_traffic_from_supabase(since: str, until: str) -> dict:
    """Pull session rows from the Supabase `sessions` table and reshape into
    the same { byPath, rows, totals } payload that fetch_traffic_from_shopify
    returns. Date filter: session_date between since and until (inclusive).
    """
    rows = _supabase_data_get("sessions", [
        ("select", "*"),
        ("session_date", f"gte.{since}"),
        ("session_date", f"lte.{until}"),
        ("order", "sessions.desc"),
    ])

    # Normalize each row → same keys the rest of the dashboard already uses.
    def _norm(r):
        out = dict(r)
        # Backwards-compat aliases the dashboard reads via rv(r, 'day', 'Day') etc.
        out["day"] = r.get("session_date") or ""
        # bounce_rate is already stored — but ShopifyQL returned it as percent (0..100),
        # while the column might be 0..1 if synced raw. We standardise to 0..100.
        try:
            br = float(r.get("bounce_rate") or 0)
            out["bounce_rate"] = round(br * 100, 2) if br < 1.5 and br > 0 else round(br, 2)
        except Exception:
            out["bounce_rate"] = 0
        return out

    detail_rows = [_norm(r) for r in rows]

    # byPath: aggregate over landing_page_type + landing_page_path + day, ignoring UTMs.
    byPath = {}
    for r in detail_rows:
        key = (r.get("landing_page_type") or "", r.get("landing_page_path") or "", r.get("day") or "")
        if not key[1]:
            continue
        b = byPath.setdefault(key, {
            "landing_page_type": key[0],
            "landing_page_path": key[1],
            "day": key[2],
            "online_store_visitors": 0,
            "sessions": 0,
            "sessions_with_cart_additions": 0,
            "bounces": 0,
            "sessions_that_reached_checkout": 0,
            "_bounce_weighted": 0.0,
            "_dur_weighted": 0.0,
            "_pps_weighted": 0.0,
            "_weight": 0,
        })
        s = float(r.get("sessions") or 0)
        b["online_store_visitors"]      += float(r.get("online_store_visitors") or 0)
        b["sessions"]                   += s
        b["sessions_with_cart_additions"] += float(r.get("sessions_with_cart_additions") or 0)
        b["bounces"]                    += float(r.get("bounces") or 0)
        b["sessions_that_reached_checkout"] += float(r.get("sessions_that_reached_checkout") or 0)
        b["_bounce_weighted"] += float(r.get("bounce_rate") or 0) * s
        b["_dur_weighted"]    += float(r.get("average_session_duration") or 0) * s
        b["_pps_weighted"]    += float(r.get("pageviews_per_session") or 0) * s
        b["_weight"]          += s
    byPath_rows = []
    for b in byPath.values():
        w = b["_weight"] or 1
        byPath_rows.append({
            "landing_page_type": b["landing_page_type"],
            "landing_page_path": b["landing_page_path"],
            "day": b["day"],
            "online_store_visitors": int(b["online_store_visitors"]),
            "sessions": int(b["sessions"]),
            "sessions_with_cart_additions": int(b["sessions_with_cart_additions"]),
            "bounces": int(b["bounces"]),
            "sessions_that_reached_checkout": int(b["sessions_that_reached_checkout"]),
            "bounce_rate": round(b["_bounce_weighted"] / w, 2),
            "average_session_duration": round(b["_dur_weighted"] / w, 2),
            "pageviews_per_session": round(b["_pps_weighted"] / w, 2),
        })
    byPath_rows.sort(key=lambda r: -r["sessions"])

    # totals: sum across every row. NOTE: Supabase only stores sessions with a
    # landing_page_path (per the sync), so this matches byPath's sum — not the
    # Shopify ground-truth which also includes null-landing checkouts.
    tot = {
        "online_store_visitors": sum(int(r.get("online_store_visitors") or 0) for r in detail_rows),
        "sessions":              sum(int(r.get("sessions") or 0)              for r in detail_rows),
        "sessions_with_cart_additions": sum(int(r.get("sessions_with_cart_additions") or 0) for r in detail_rows),
        "bounces":               sum(int(r.get("bounces") or 0)               for r in detail_rows),
        "sessions_that_reached_checkout": sum(int(r.get("sessions_that_reached_checkout") or 0) for r in detail_rows),
    }
    sess_tot = tot["sessions"] or 1
    tot["bounce_rate"]              = round(tot["bounces"] / sess_tot * 100, 2)
    tot["average_session_duration"] = round(sum(float(r.get("average_session_duration") or 0) * float(r.get("sessions") or 0) for r in detail_rows) / sess_tot, 2)
    tot["pageviews_per_session"]    = round(sum(float(r.get("pageviews_per_session") or 0) * float(r.get("sessions") or 0)   for r in detail_rows) / sess_tot, 2)

    print(f"  [Traffic←Supabase] byPath={len(byPath_rows)} · detail={len(detail_rows)}")
    return {"byPath": byPath_rows, "rows": detail_rows, "totals": tot, "_source": "supabase"}


def fetch_orders_from_supabase(since: str, until: str) -> list:
    """Pull rows from the Supabase `orders` table joined with `order_line_items`
    and reshape into the same flat dict shape the dashboard's existing /api/orders
    consumers expect.
    """
    # Inclusive day range on created_at — orders.created_at is a timestamptz so
    # we filter on date boundaries in IST-neutral form (gte midnight, lte 23:59:59).
    # NOTE: no ORDER BY — the orders table doesn't have an index on
    # created_at in the user's Supabase project, and Postgres falls back
    # to a full-table sort that exceeds the 5s statement timeout.
    # Adding `CREATE INDEX orders_created_at_idx ON orders (created_at);`
    # in Supabase SQL editor would let us sort server-side again.
    # For now we sort client-side after the fetch.
    orders = _supabase_data_get("orders", [
        ("select", "*"),
        ("created_at", f"gte.{since}T00:00:00"),
        ("created_at", f"lte.{until}T23:59:59"),
    ])
    if not orders:
        return []
    orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)

    # Pull all line items for these orders in one batched call.
    order_ids = [o.get("id") for o in orders if o.get("id")]
    line_items_by_order: dict[str, list] = {}
    if order_ids:
        # PostgREST `in.(...)` filter — chunk to keep URL under ~8KB
        CHUNK = 200
        for i in range(0, len(order_ids), CHUNK):
            chunk = order_ids[i:i+CHUNK]
            quoted = ",".join('"' + str(x).replace('"', '\\"') + '"' for x in chunk)
            lis = _supabase_data_get("order_line_items", [
                ("select", "*"),
                ("order_id", f"in.({quoted})"),
            ])
            for li in lis:
                line_items_by_order.setdefault(li.get("order_id"), []).append(li)

    # Stitch
    def _f(v):
        try: return float(v or 0)
        except (TypeError, ValueError): return 0.0

    def _split_csv(v):
        if not v: return []
        if isinstance(v, list): return v
        return [x.strip() for x in str(v).split(",") if x.strip()]

    def _parse_attrs(v):
        if not v: return {}
        if isinstance(v, dict): return v
        try:
            return json.loads(v) if isinstance(v, str) else {}
        except Exception:
            return {}

    result = []
    for o in orders:
        items_raw = line_items_by_order.get(o.get("id"), [])
        items = [{
            "title":        li.get("title") or "",
            "variantTitle": li.get("variant_title") or "",
            "sku":          li.get("sku") or "",
            "quantity":     int(li.get("quantity") or 0),
            "price":        _f(li.get("unit_price")),
            "image":        li.get("image_url") or "",
        } for li in items_raw]

        # The orders table has utm_* as direct columns AND an opaque
        # custom_attributes blob. We expose both: utm_* under customAttributes
        # (where the dashboard's existing matching looks) and also at the
        # order root for downstream consumers that want them denormalised.
        attrs = _parse_attrs(o.get("custom_attributes"))
        for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"):
            v = o.get(k)
            if v and not attrs.get(k):
                attrs[k] = v
        # Other denormalised columns the dashboard reads via customAttributes
        for k in ("full_url", "gokwik_cid", "cart_token", "user_agent", "customer_ip"):
            v = o.get(k)
            if v and not attrs.get(k):
                attrs[k] = v

        result.append({
            "id":                o.get("id") or "",
            "name":              o.get("name") or "",
            "createdAt":         o.get("created_at") or "",
            "financialStatus":   (o.get("financial_status") or "").upper(),
            "fulfillmentStatus": (o.get("fulfillment_status") or "").upper(),
            "total":      _f(o.get("total_price")),
            "subtotal":   _f(o.get("subtotal")),
            "discounts":  _f(o.get("total_discounts")),
            "shipping":   _f(o.get("total_shipping")),
            "tax":        _f(o.get("total_tax")),
            "refunded":   _f(o.get("total_refunded")),
            "currency":   o.get("currency") or "INR",
            "discountCodes": _split_csv(o.get("discount_codes")),
            "note":       o.get("note") or "",
            "tags":       _split_csv(o.get("tags")),
            "cancelled":  o.get("cancelled_at") is not None,
            "customer": {
                "name":  f"{o.get('shipping_first_name','') or ''} {o.get('shipping_last_name','') or ''}".strip(),
                "email": "",
                "phone": o.get("shipping_phone") or "",
                "ordersCount": 0,
            },
            "address": {
                "city":     o.get("shipping_city") or "",
                "province": o.get("shipping_province") or "",
                "country":  o.get("shipping_country") or "",
                "zip":      o.get("shipping_zip") or "",
            },
            "lineItems":     items,
            "itemCount":     sum(li["quantity"] for li in items),
            "customAttributes": attrs,
            "paymentGateway":   o.get("payment_gateway") or o.get("gateway") or "",
            "source":           o.get("source_name") or "",
            # Denormalised utm_* at the root (new tables expose these directly)
            "utm_source":   o.get("utm_source") or "",
            "utm_medium":   o.get("utm_medium") or "",
            "utm_campaign": o.get("utm_campaign") or "",
            "utm_content":  o.get("utm_content") or "",
            "utm_term":     o.get("utm_term") or "",
        })

    print(f"  [Orders←Supabase] {len(result)} orders · {sum(len(line_items_by_order.get(o.get('id'),[])) for o in orders)} line items")
    return result


def fetch_ads(since: str = "", until: str = "") -> list:
    """Fetch ad rows from Supabase primary_table — one row per ad per day with
    flat columns (ad_id, ad_name, ad_link, amount_spent_inr, outbound_clicks,
    impressions, …). Filters by the `date` column when since/until are passed.
    No LIMIT — Supabase REST will still page if needed, but the dashboard now
    receives every ad matching the range.
    """
    params = [("select", "*"), ("order", "amount_spent_inr.desc"), ("limit", "99999")]
    if since:
        params.append(("date", f"gte.{since}"))
    if until:
        params.append(("date", f"lte.{until}"))
    rows = _supabase_get("primary_table", params)
    if (not rows) and (since or until):
        # If the date filter wiped everything, fall back to unfiltered so the
        # dashboard still has something to show.
        rows = _supabase_get("primary_table", [("select", "*"), ("order", "amount_spent_inr.desc"), ("limit", "99999")])
    return rows


def fetch_inventory_snapshot(date: str = "") -> dict:
    """Fetch inventory snapshot for a given date.
    Falls back to the latest available snapshot when the requested date has no rows.
    Returns: {rows, date, requestedDate, isFallback}
    """
    if date:
        rows = _supabase_get("inventory_snapshots", [
            ("select", "*"),
            ("snapshot_date", f"eq.{date}"),
            ("limit", "5000"),
        ])
        if rows:
            return {"rows": rows, "date": date, "requestedDate": date, "isFallback": False}

    # Fall back to the latest snapshot date
    latest = _supabase_get("inventory_snapshots", [
        ("select", "snapshot_date"),
        ("order", "snapshot_date.desc"),
        ("limit", "1"),
    ])
    if not latest:
        return {"rows": [], "date": "", "requestedDate": date or "", "isFallback": bool(date)}
    actual = latest[0].get("snapshot_date") or ""
    rows = _supabase_get("inventory_snapshots", [
        ("select", "*"),
        ("snapshot_date", f"eq.{actual}"),
        ("limit", "5000"),
    ])
    return {
        "rows": rows,
        "date": actual,
        "requestedDate": date or "",
        "isFallback": bool(date) and actual != date,
    }


class Handler(BaseHTTPRequestHandler):

    def send_json(self, status: int, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # Allow the dashboard (opened as a local file or any origin) to call this
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> dict:
        """Read and parse the request body as JSON. Returns {} on failure."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                raw = self.rfile.read(length)
                return json.loads(raw.decode("utf-8"))
        except Exception:
            pass
        return {}

    def do_OPTIONS(self):
        # Pre-flight CORS
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, *")
        self.end_headers()

    def _serve_static(self, abs_path, content_type):
        """Read a file from disk and stream it back with the right Content-Type."""
        try:
            with open(abs_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_json(404, {"error": os.path.basename(abs_path) + " not found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # Project layout after restructure:
        #   D:/landing_page/
        #     backend/server.py    ← this file
        #     frontend/
        #       index.html
        #       css/styles.css
        #       js/app.js
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        frontend_dir = os.path.join(project_root, "frontend")

        # ── GET / → serve frontend/index.html ──
        if parsed.path == "/":
            self._serve_static(os.path.join(frontend_dir, "index.html"), "text/html; charset=utf-8")
            return

        # ── GET /css/<file> and /js/<file> → serve static asset ──
        if parsed.path.startswith("/css/") or parsed.path.startswith("/js/"):
            # Strip leading slash, prevent path traversal
            rel = parsed.path.lstrip("/").replace("..", "")
            ctype = "text/css; charset=utf-8" if rel.startswith("css/") else "application/javascript; charset=utf-8"
            self._serve_static(os.path.join(frontend_dir, rel), ctype)
            return

        # ── GET /api/health ──
        if parsed.path == "/api/health":
            self.send_json(200, {"status": "ok", "shop": SHOP_DOMAIN})
            return

        # ── GET /api/traffic?since=YYYY-MM-DD&until=YYYY-MM-DD (or ?date=YYYY-MM-DD) ──
        if parsed.path == "/api/traffic":
            since = params.get("since", [None])[0]
            until = params.get("until", [None])[0]
            date_param = params.get("date", [None])[0]
            if date_param and not since and not until:
                since = until = date_param

            DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
            if not since or not until or not re.match(DATE_RE, since) or not re.match(DATE_RE, until):
                self.send_json(400, {"error": "Missing or invalid date. Use ?since=YYYY-MM-DD&until=YYYY-MM-DD"})
                return

            try:
                rows = fetch_traffic(since, until)
                self.send_json(200, rows)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Shopify HTTP error: {e}"})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── GET /api/ads?since=YYYY-MM-DD&until=YYYY-MM-DD (Supabase proxy) ──
        if parsed.path == "/api/ads":
            since = params.get("since", [""])[0] or ""
            until = params.get("until", [""])[0] or ""
            try:
                rows = fetch_ads(since, until)
                self.send_json(200, rows)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Supabase HTTP error: {e}"})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── GET /api/inventory-snapshot?date=YYYY-MM-DD (Supabase proxy) ──
        if parsed.path == "/api/inventory-snapshot":
            date = params.get("date", [""])[0] or ""
            try:
                payload = fetch_inventory_snapshot(date)
                self.send_json(200, payload)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Supabase HTTP error: {e}"})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── GET /api/inventory → fetch all products from Shopify ──
        if parsed.path == "/api/inventory":
            try:
                products = fetch_inventory()
                self.send_json(200, products)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Shopify HTTP error: {e}"})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── GET /api/sales?since=YYYY-MM-DD&until=YYYY-MM-DD ──
        if parsed.path == "/api/sales":
            since = params.get("since", [None])[0]
            until = params.get("until", [None])[0]

            if not since or not until:
                self.send_json(400, {"error": "Missing since/until. Use ?since=YYYY-MM-DD&until=YYYY-MM-DD"})
                return

            try:
                rows = fetch_sales(since, until)
                self.send_json(200, rows)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Shopify HTTP error: {e}"})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── GET /api/orders?since=YYYY-MM-DD&until=YYYY-MM-DD ──
        if parsed.path == "/api/orders":
            since = params.get("since", [None])[0]
            until = params.get("until", [None])[0]
            if not since or not until:
                self.send_json(400, {"error": "Missing since/until. Use ?since=YYYY-MM-DD&until=YYYY-MM-DD"})
                return
            try:
                orders = fetch_orders(since, until)
                self.send_json(200, orders)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Shopify HTTP error: {e}"})
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_json(500, {"error": str(e)})
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        body = self._read_json_body()

        def get_param(key):
            # Body takes priority, fall back to query string
            if key in body:
                return str(body[key])
            lst = params.get(key)
            return lst[0] if lst else None

        # ── POST /api/sales ──
        if parsed.path == "/api/sales":
            since = get_param("since")
            until = get_param("until")
            if not since or not until:
                self.send_json(400, {"error": "Missing since/until"})
                return
            try:
                rows = fetch_sales(since, until)
                self.send_json(200, rows)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Shopify HTTP error: {e}"})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── POST /api/traffic ──
        if parsed.path == "/api/traffic":
            since = get_param("since")
            until = get_param("until")
            date_param = get_param("date")
            if date_param and not since and not until:
                since = until = date_param
            DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
            if not since or not until or not re.match(DATE_RE, since) or not re.match(DATE_RE, until):
                self.send_json(400, {"error": "Missing or invalid date. Use since/until YYYY-MM-DD"})
                return
            try:
                rows = fetch_traffic(since, until)
                self.send_json(200, rows)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Shopify HTTP error: {e}"})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── POST /api/inventory ──
        if parsed.path == "/api/inventory":
            try:
                products = fetch_inventory()
                self.send_json(200, products)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Shopify HTTP error: {e}"})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── POST /api/orders ──
        if parsed.path == "/api/orders":
            since = get_param("since")
            until = get_param("until")
            if not since or not until:
                self.send_json(400, {"error": "Missing since/until"})
                return
            try:
                orders = fetch_orders(since, until)
                self.send_json(200, orders)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Shopify HTTP error: {e}"})
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_json(500, {"error": str(e)})
            return

        self.send_json(404, {"error": "Not found"})

    def log_message(self, fmt, *args):
        # Clean log output
        print(f"  {args[0]}  {args[1]}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    PORT = 5000
    server = HTTPServer(("localhost", PORT), Handler)
    print(f"\n  Saadaa Ops Server")
    print(f"  -----------------")
    print(f"  Running on  ->  http://localhost:{PORT}")
    print(f"  Shop        ->  {SHOP_DOMAIN}")
    print(f"\n  In the dashboard:")
    print(f"  Traffic -> Data Source -> URL: http://localhost:{PORT}")
    print(f"  Pick a date -> Fetch ->\n")
    print(f"  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")


        