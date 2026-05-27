"""
सादा Traffic Server — Vercel Serverless Edition
================================================
Wraps all Shopify proxy logic (traffic, inventory, sales, orders)
as a Flask WSGI app that Vercel runs as a serverless function.

Environment variables (set in Vercel Project Settings):
    SHOP_DOMAIN         saadaa-design.myshopify.com
    ADMIN_ACCESS_TOKEN  shpat_...
"""

from __future__ import annotations
import os
import re
import json
import sys

# Make sure project root is on path so imports work in Vercel
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, request, send_from_directory
import requests

# ── Credentials ──────────────────────────────────────────────────────────────
# Vercel injects these from Project Settings → Environment Variables.
# For local dev, python-dotenv loads from .env automatically when you
# run `flask run` or `python api/index.py`.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
except ImportError:
    pass

SHOP_DOMAIN          = os.environ.get("SHOP_DOMAIN", "")
ADMIN_ACCESS_TOKEN   = os.environ.get("ADMIN_ACCESS_TOKEN", "")
SUPABASE_URL         = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()

# Second Supabase project — holds `sessions`, `orders`, `order_line_items`.
# When SAADAA_VAR / SAADAA_KEY are set, /api/traffic and /api/orders read
# from this project first and fall back to Shopify only on failure.
SAADAA_VAR = (os.environ.get("SAADAA_VAR") or "").strip().rstrip("/")
SAADAA_KEY = (os.environ.get("SAADAA_KEY") or "").strip()

app = Flask(__name__, static_folder=None)

# ── CORS helper ───────────────────────────────────────────────────────────────

def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


@app.after_request
def add_cors(resp):
    return _cors(resp)


@app.route("/api/<path:_>", methods=["OPTIONS"])
@app.route("/api/", methods=["OPTIONS"])
def preflight(**_):
    return _cors(app.make_response(("", 204)))

# ── Shopify helpers ───────────────────────────────────────────────────────────

SHOPIFY_GQL = f"https://{SHOP_DOMAIN}/admin/api/2025-10/graphql.json"
SHOPIFY_HEADERS = lambda: {
    "Content-Type": "application/json",
    "X-Shopify-Access-Token": ADMIN_ACCESS_TOKEN,
}

SIZES_ORDER = ['XS', 'S', 'M', 'L', 'XL', '2XL', '3XL', '4XL', '5XL']
SIZE_MAP = {
    'XXS': 'XS', 'EXTRA SMALL': 'XS',
    'SMALL': 'S',
    'MEDIUM': 'M', 'MED': 'M',
    'LARGE': 'L',
    'EXTRA LARGE': 'XL',
    'XXL': '2XL', 'XXXL': '3XL', 'XXXXL': '4XL', 'XXXXXL': '5XL',
}


def _gql(query: str, variables: dict = None, timeout: int = 55) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    r = requests.post(SHOPIFY_GQL, headers=SHOPIFY_HEADERS(), json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise ValueError(f"GraphQL error: {data['errors']}")
    return data


def normalize_size(val: str) -> str:
    v = val.strip().upper()
    return SIZE_MAP.get(v, v)


def extract_color(variant: dict) -> str:
    for opt in variant.get("selectedOptions", []):
        if opt.get("name", "").lower() in ("color", "colour"):
            return opt.get("value", "").strip()
    return ""


def extract_size(variant: dict) -> str:
    for opt in variant.get("selectedOptions", []):
        if opt.get("name", "").lower() == "size":
            return normalize_size(opt.get("value", ""))
    return ""


def sku_prefix_to_gender(sku: str) -> str:
    s = sku.upper()[:2] if sku else ""
    if s == "SD": return "Women"
    if s == "SM": return "Men"
    if s == "SU": return "Unisex"
    return ""


def strip_size_from_sku(sku: str) -> str:
    if not sku:
        return sku
    for sep in ['_', '-']:
        if sep in sku:
            parts = sku.rsplit(sep, 1)
            suffix = parts[-1].upper().strip()
            if suffix in ('XS', 'S', 'M', 'L', 'XL', '2XL', '3XL', '4XL', '5XL',
                          'XXS', 'XXL', 'XXXL', 'SMALL', 'MEDIUM', 'LARGE',
                          'FREE', 'FREESIZE', 'FREE SIZE', 'OS', 'ONESIZE'):
                return parts[0]
    return sku


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


def rows_to_dicts(columns: list, raw_rows: list) -> list:
    """Convert ShopifyQL array-of-arrays rows to list of dicts."""
    if not raw_rows:
        return []
    if isinstance(raw_rows[0], dict):
        return raw_rows
    if not columns:
        return []
    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(columns)]
    result = []
    for row in raw_rows:
        if isinstance(row, list):
            result.append({col_names[i]: v for i, v in enumerate(row) if i < len(col_names)})
    return result

# ── Fetch functions ───────────────────────────────────────────────────────────

def fetch_traffic(since: str, until: str) -> dict:
    """Traffic source — Supabase `sessions` table first, Shopify fallback.

    The response is tagged with `_source` so we can tell at a glance which
    branch served it: "supabase", "shopify:no-credentials" (env vars not
    set), "shopify:supabase-empty", or "shopify:supabase-error:<msg>".
    """
    if not (SAADAA_VAR and SAADAA_KEY):
        out = _fetch_traffic_from_shopify(since, until)
        out["_source"] = "shopify:no-credentials"
        return out
    try:
        payload = fetch_traffic_from_supabase(since, until)
        if payload.get("byPath") or payload.get("rows"):
            return payload
        out = _fetch_traffic_from_shopify(since, until)
        out["_source"] = "shopify:supabase-empty"
        return out
    except Exception as e:
        print(f"  [Traffic] Supabase fetch failed ({e}); fallback to Shopify…")
        out = _fetch_traffic_from_shopify(since, until)
        out["_source"] = f"shopify:supabase-error:{str(e)[:120]}"
        return out


def _fetch_traffic_from_shopify(since: str, until: str) -> dict:
    """Two-query traffic fetch:

      • byPath  — GROUP BY landing_page_type, landing_page_path, day.
                  Authoritative per-landing-page totals; ~99% coverage of
                  the true day total (Shopify's 1000-row cap rarely bites
                  with only 3 dims).
      • rows    — full 9-dim breakdown for UTM / city drill-downs. Capped
                  at 1000 rows by Shopify, so the totals here are partial.

    Bounce rate is computed locally (bounces / sessions × 100) on every row.
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
    query = """
    query($ql: String!) {
      shopifyqlQuery(query: $ql) {
        tableData { columns { name dataType } rows }
        parseErrors
      }
    }
    """

    def _run(ql):
        data = _gql(query, {"ql": ql})
        sqr = data.get("data", {}).get("shopifyqlQuery", {})
        if sqr.get("parseErrors"):
            raise ValueError(f"ShopifyQL parse error: {sqr['parseErrors']}")
        tbl = sqr.get("tableData", {})
        result = rows_to_dicts(tbl.get("columns", []), tbl.get("rows", []))
        for row in result:
            bounces = float(row.get('bounces') or 0)
            sessions = float(row.get('sessions') or 0)
            row['bounce_rate'] = round(bounces / sessions * 100, 2) if sessions > 0 else 0
        return result

    # Authoritative day totals — no landing_page filter, no GROUP BY.
    # This is what Shopify Analytics shows as the day's visitor / session /
    # checkout numbers; the dashboard's KPI cards should display these
    # rather than summing per-landing-page rows (which double-count
    # visitors and miss checkouts whose session had a null landing_page).
    ql_truth = (
        "FROM sessions "
        "SHOW online_store_visitors, sessions, sessions_with_cart_additions, "
        "bounces, average_session_duration, pageviews_per_session, "
        "sessions_that_reached_checkout, added_to_cart_rate "
        "WHERE human_or_bot_session IN ('human', 'bot') "
        f"SINCE {since} UNTIL {until}"
    )
    truth_rows = []
    try:
        truth_rows = _run(ql_truth)
    except Exception:
        truth_rows = []
    truth = truth_rows[0] if truth_rows else {}

    return {"byPath": _run(ql_path), "rows": _run(ql_detail), "totals": truth}


def fetch_inventory() -> list:
    query = """
    query($cursor: String) {
      products(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id title handle status productType tags onlineStoreUrl createdAt
          variants(first: 100) {
            nodes { id title sku inventoryQuantity selectedOptions { name value } }
          }
        }
      }
    }
    """
    all_products = []
    cursor = None
    for page in range(40):
        variables = {"cursor": cursor} if cursor else {}
        data = _gql(query, variables)
        pdata = data.get("data", {}).get("products", {})
        nodes = pdata.get("nodes", [])
        page_info = pdata.get("pageInfo", {})
        all_products.extend(nodes)
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break

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

        category = product_type or ""
        is_discontinued = False
        tags_lower = [t.lower().strip() for t in tags]
        for tag in tags:
            tl = tag.lower().strip()
            if tl in ["topwear", "bottomwear", "dress", "co-ord", "kurta", "shirt",
                       "pant", "women topwear", "women bottomwear", "men topwear",
                       "men bottomwear", "women dress"]:
                category = tag
        if any(t in ["discontinued", "disc", "discontinue", "disc."] for t in tags_lower):
            is_discontinued = True
        is_excluded = any(t in ["old saadaa product", "old saadaa", "old product",
                                  "archive", "archived", "hidden", "exclude",
                                  "excluded", "old"] for t in tags_lower)

        color_groups: dict[str, dict] = {}
        for v in variants:
            color = extract_color(v) or "Default"
            size = extract_size(v)
            sku = v.get("sku") or ""
            qty = v.get("inventoryQuantity", 0) or 0
            if color not in color_groups:
                color_groups[color] = {"variants": [], "sku": ""}
            color_groups[color]["variants"].append({"size": size, "qty": qty, "sku": sku})
            if sku and not color_groups[color]["sku"]:
                color_groups[color]["sku"] = sku

        if not color_groups:
            color_groups["Default"] = {"variants": [], "sku": ""}
            for v in variants:
                sku = v.get("sku") or ""
                qty = v.get("inventoryQuantity", 0) or 0
                color_groups["Default"]["variants"].append(
                    {"size": extract_size(v), "qty": qty, "sku": sku})
                if sku and not color_groups["Default"]["sku"]:
                    color_groups["Default"]["sku"] = sku

        for color, group in color_groups.items():
            color_sku = strip_size_from_sku(group["sku"])
            gender = sku_prefix_to_gender(color_sku)
            sizes = {s: None for s in SIZES_ORDER}
            for var in group["variants"]:
                sz = var["size"]
                if sz and sz in sizes:
                    sizes[sz] = (sizes[sz] or 0) + var["qty"]
            active_sizes = [s for s in SIZES_ORDER if sizes[s] is not None]
            total_stock = sum(sizes[s] for s in active_sizes) if active_sizes else 0
            stock_status = compute_stock_status(sizes)
            display_name = f"{title} - {color}" if color != "Default" else title
            result.append({
                "slug": handle, "name": display_name, "colorSku": color_sku,
                "color": color, "gender": gender, "productStatus": status,
                "category": category, "productLink": url, "sizes": sizes,
                "totalStock": total_stock, "stockStatus": stock_status,
                "createdAt": created_at, "hasInvData": len(active_sizes) > 0,
                "discontinued": is_discontinued, "excluded": is_excluded, "tags": tags,
            })
    return result


def fetch_refunds_in_range(since: str, until: str) -> dict:
    """
    DEPRECATED — fetch_sales now uses ShopifyQL directly which matches Shopify
    Analytics' returns column exactly. Kept only because some older code paths
    might import it; do not call from new code.
    """
    from datetime import datetime, timedelta
    try:
        until_buf = (datetime.fromisoformat(until) + timedelta(days=1)).date().isoformat()
    except Exception:
        until_buf = until

    refund_by_product: dict[str, dict] = {}
    cursor = None
    max_pages = 80

    for page in range(max_pages):
        after_clause = f', after: "{cursor}"' if cursor else ""
        query_filter = f"updated_at:>={since} updated_at:<={until_buf}"
        gql = (
            'query { orders(first: 50, query: "' + query_filter +
            '", sortKey: UPDATED_AT, reverse: true' + after_clause +
            ') { pageInfo { hasNextPage endCursor } nodes { id cancelledAt '
            'refunds { id createdAt totalRefundedSet { shopMoney { amount } } '
            'refundLineItems(first: 50) { nodes { quantity '
            'subtotalSet { shopMoney { amount } } '
            'lineItem { title sku } } } } } } }'
        )
        try:
            resp = requests.post(
                f"https://{SHOP_DOMAIN}/admin/api/2025-10/graphql.json",
                headers={"Content-Type": "application/json", "X-Shopify-Access-Token": ADMIN_ACCESS_TOKEN},
                json={"query": gql}, timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            break

        if data.get("errors"):
            break

        orders_data = data.get("data", {}).get("orders", {}) or {}
        nodes = orders_data.get("nodes", []) or []
        page_info = orders_data.get("pageInfo", {}) or {}

        for o in nodes:
            if o.get("cancelledAt"):
                continue
            for r in (o.get("refunds") or []):
                created = (r.get("createdAt") or "")[:10]
                if not (since <= created <= until):
                    continue
                rlis = ((r.get("refundLineItems") or {}).get("nodes")) or []
                for rli in rlis:
                    li = rli.get("lineItem") or {}
                    title = (li.get("title") or "").strip()
                    if not title:
                        continue
                    qty = int(rli.get("quantity") or 0)
                    try:
                        amt = abs(float(((rli.get("subtotalSet") or {}).get("shopMoney") or {}).get("amount") or 0))
                    except Exception:
                        amt = 0.0
                    bucket = refund_by_product.setdefault(title, {"qty": 0, "amount": 0.0})
                    bucket["qty"] += qty
                    bucket["amount"] += amt

        if not page_info.get("hasNextPage", False):
            break
        cursor = page_info.get("endCursor")

    return refund_by_product


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
    # Session-level utm_* are NOT exposed in the Admin GraphQL sales dataset.
    # Shopify's report editor resolves them via a hidden session join the API
    # doesn't surface. order_utm_* IS exposed and carries the last-click value
    # Shopify stamps on the order at checkout.
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


def _shopifyql_table(ql: str) -> dict:
    """Run a ShopifyQL query and return the tableData dict."""
    gql = """
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
        json={"query": gql, "variables": {"ql": ql}},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise ValueError(f"ShopifyQL GraphQL error: {data['errors']}")
    sqr = (data.get("data") or {}).get("shopifyqlQuery") or {}
    if sqr.get("parseErrors"):
        raise ValueError(f"ShopifyQL parse error: {sqr['parseErrors']}")
    return sqr.get("tableData") or {}


def _table_to_rows(table: dict) -> list:
    columns = table.get("columns") or []
    raw_rows = table.get("rows") or []
    if not raw_rows:
        return []
    if isinstance(raw_rows[0], dict):
        return list(raw_rows)
    if not columns:
        return []
    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(columns)]
    return [
        {col_names[i]: v for i, v in enumerate(row) if i < len(col_names)}
        for row in raw_rows if isinstance(row, list)
    ]


def fetch_sales(since: str, until: str) -> dict:
    """
    Returns BOTH the per-(day × product × order_utm × customer × line_item)
    detail rows AND a separate server-aggregated per-product totals array.
    The detail rows feed UTM Analysis / raw-row mode and are capped at
    no LIMIT cap; byProduct is one row per product (no cap issue) so the
    main Sales tab always matches Shopify "Total sales by product" exactly.
    """
    # Detail query — used for raw-row mode + UTM Analysis. The previous
    # 13-dim GROUP BY (including line_item_id, customer_id,
    # customer_last_order_date, customer_number_of_orders) was a row-count
    # bomb: a single day could blow past the default 1000-row cap silently.
    # Probe confirmed those four dims are not consumed anywhere downstream
    # — dropping them cuts row count ~5x (2,546 → 490 on 2026-05-19) and
    # the totals still match the byProduct grand total to the rupee.
    # Explicit LIMIT 10000 is a belt-and-suspenders safety net for huge
    # date ranges.
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

    # Grand-totals query — UNFILTERED. Matches Shopify's "Total sales over
    # time" report row-for-row. We need this because the per-product query
    # above intentionally drops:
    #   - rows with product_title IS NULL (custom line items, gift cards,
    #     shipping adjustments — can't be grouped by product without a
    #     phantom "—" row)
    #   - the Return Prime: Order Return sales_channel
    # Those filters keep the per-product table clean but make the summed
    # values undershoot Shopify by the value of those orphan rows. This
    # query exists solely to feed the top-line KPIs so they reconcile to
    # the rupee with Shopify; we also return the gap explicitly so the UI
    # can label what the per-product view excluded.
    ql_grand = (
        "FROM sales "
        "SHOW gross_sales, discounts, returns, net_sales, taxes, total_sales, net_items_sold "
        f"SINCE {since} UNTIL {until} "
    )

    detail_rows = _table_to_rows(_shopifyql_table(ql_detail))
    by_product_rows = _table_to_rows(_shopifyql_table(ql_byproduct))
    grand_rows = _table_to_rows(_shopifyql_table(ql_grand))

    def _cast(rows):
        for row in rows:
            for f in SALES_NUMERIC_FIELDS:
                v = row.get(f)
                row[f] = float(v) if v not in (None, "") else 0
                t = row.get(f + "__totals")
                row[f + "__totals"] = float(t) if t not in (None, "") else 0
            for f in SALES_DIMENSION_FIELDS:
                if f in row and row[f] is None:
                    row[f] = ""

    _cast(detail_rows)
    _cast(by_product_rows)
    _cast(grand_rows)

    # Collapse the single grand-totals row into a flat dict the UI can read
    # directly. Compute the per-field gap between grand totals (Shopify
    # truth) and the per-product sum (what makes it into the table).
    grand_totals = {f: 0.0 for f in SALES_NUMERIC_FIELDS}
    if grand_rows:
        for f in SALES_NUMERIC_FIELDS:
            grand_totals[f] = float(grand_rows[0].get(f) or 0)
    byp_sums = {f: sum(float(r.get(f) or 0) for r in by_product_rows) for f in SALES_NUMERIC_FIELDS}
    excluded = {f: round(grand_totals[f] - byp_sums[f], 2) for f in SALES_NUMERIC_FIELDS}

    return {
        "rows": detail_rows,
        "byProduct": by_product_rows,
        "grandTotals": grand_totals,
        "excludedFromBreakdown": excluded,
    }


def fetch_orders(since: str, until: str) -> list:
    """Orders source — Supabase `orders` + `order_line_items` first,
    Shopify Admin GraphQL fallback."""
    if SAADAA_VAR and SAADAA_KEY:
        try:
            rows = fetch_orders_from_supabase(since, until)
            if rows:
                return rows
            print("  [Orders] Supabase returned 0 rows — falling back to Shopify…")
        except Exception as e:
            print(f"  [Orders] Supabase fetch failed ({e}); fallback to Shopify…")
    return _fetch_orders_from_shopify(since, until)


def _fetch_orders_from_shopify(since: str, until: str) -> list:
    all_orders = []
    cursor = None
    query_filter = f"created_at:>={since} created_at:<={until}"

    for page in range(40):
        after = f', after: "{cursor}"' if cursor else ""
        gql = (
            'query { orders(first: 50, query: "' + query_filter +
            '", sortKey: CREATED_AT, reverse: true' + after + ''') {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name createdAt displayFinancialStatus displayFulfillmentStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      subtotalPriceSet { shopMoney { amount } }
      totalDiscountsSet { shopMoney { amount } }
      totalShippingPriceSet { shopMoney { amount } }
      totalTaxSet { shopMoney { amount } }
      totalRefundedSet { shopMoney { amount } }
      discountCodes note tags cancelledAt
      shippingAddress { firstName lastName phone city province country zip }
      lineItems(first: 20) {
        nodes {
          title variantTitle sku quantity
          originalUnitPriceSet { shopMoney { amount } }
          image { url }
        }
      }
      customAttributes { key value }
      paymentGatewayNames sourceName
    }
  }
}'''
        )
        try:
            data = _gql(gql)
        except Exception as e:
            raise RuntimeError(f"[Orders p{page+1}] {e}") from e

        orders_data = data.get("data", {}).get("orders", {})
        if not orders_data:
            break

        nodes = orders_data.get("nodes", [])
        page_info = orders_data.get("pageInfo", {})

        def _money(o, field):
            try:
                return float(o.get(field, {}).get("shopMoney", {}).get("amount", 0))
            except Exception:
                return 0

        for o in nodes:
            items = []
            for li in o.get("lineItems", {}).get("nodes", []):
                try:
                    items.append({
                        "title": li.get("title", ""),
                        "variantTitle": li.get("variantTitle", ""),
                        "sku": li.get("sku", ""),
                        "quantity": li.get("quantity", 0),
                        "price": float((li.get("originalUnitPriceSet") or {})
                                       .get("shopMoney", {}).get("amount", 0)),
                        "image": (li.get("image") or {}).get("url", ""),
                    })
                except Exception:
                    pass
            cust = o.get("shippingAddress") or {}
            attrs = {}
            for a in (o.get("customAttributes") or []):
                try:
                    attrs[a.get("key", "")] = a.get("value", "")
                except Exception:
                    pass
            all_orders.append({
                "id": o.get("id", ""), "name": o.get("name", ""),
                "createdAt": o.get("createdAt", ""),
                "financialStatus": o.get("displayFinancialStatus", ""),
                "fulfillmentStatus": o.get("displayFulfillmentStatus", ""),
                "total": _money(o, "totalPriceSet"),
                "subtotal": _money(o, "subtotalPriceSet"),
                "discounts": _money(o, "totalDiscountsSet"),
                "shipping": _money(o, "totalShippingPriceSet"),
                "tax": _money(o, "totalTaxSet"),
                "refunded": _money(o, "totalRefundedSet"),
                "currency": ((o.get("totalPriceSet") or {}).get("shopMoney") or {})
                             .get("currencyCode", "INR"),
                "discountCodes": o.get("discountCodes", []),
                "note": o.get("note", ""),
                "tags": o.get("tags", []),
                "cancelled": o.get("cancelledAt") is not None,
                "customer": {
                    "name": f"{cust.get('firstName','')} {cust.get('lastName','')}".strip(),
                    "email": "", "phone": cust.get("phone", ""), "ordersCount": 0,
                },
                "address": {
                    "city": cust.get("city", ""), "province": cust.get("province", ""),
                    "country": cust.get("country", ""), "zip": cust.get("zip", ""),
                },
                "lineItems": items,
                "itemCount": sum(li["quantity"] for li in items),
                "customAttributes": attrs,
                "paymentGateway": ", ".join(o.get("paymentGatewayNames", [])),
                "source": o.get("sourceName", ""),
            })

        if not page_info.get("hasNextPage", False):
            break
        cursor = page_info.get("endCursor")

    return all_orders

# ── Supabase proxy fetch ─────────────────────────────────────────────────────

def _supabase_get(table: str, params: list) -> list:
    """GET from Supabase REST, paginated.

    Supabase enforces a server-side row cap (1000 by default on the Free
    tier — set via PostgREST's max-rows config) that neither a Range header
    nor ?limit= can exceed. So we loop with Range: <offset>-<offset+999>
    until the page returns fewer than 1000 rows, then concatenate.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("Supabase not configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY env vars.")
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
    """Paginated GET against the SECOND Supabase project (sessions / orders /
    order_line_items). Raises ValueError if env vars aren't set."""
    if not SAADAA_VAR or not SAADAA_KEY:
        raise ValueError("Second Supabase project not configured. "
                         "Add SAADAA_VAR and SAADAA_KEY env vars.")
    url = f"{SAADAA_VAR}/rest/v1/{table}"
    base_headers = {
        "apikey": SAADAA_KEY,
        "Authorization": f"Bearer {SAADAA_KEY}",
        "Content-Type": "application/json",
        "Range-Unit": "items",
    }
    PAGE = 1000
    out, offset = [], 0
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
    """Pull session rows from Supabase and reshape into the same
    { byPath, rows, totals } shape Shopify direct returns."""
    rows = _supabase_data_get("sessions", [
        ("select", "*"),
        ("session_date", f"gte.{since}"),
        ("session_date", f"lte.{until}"),
        ("order", "sessions.desc"),
    ])

    def _norm(r):
        out = dict(r)
        out["day"] = r.get("session_date") or ""
        try:
            br = float(r.get("bounce_rate") or 0)
            out["bounce_rate"] = round(br * 100, 2) if br < 1.5 and br > 0 else round(br, 2)
        except Exception:
            out["bounce_rate"] = 0
        return out

    detail_rows = [_norm(r) for r in rows]

    byPath = {}
    for r in detail_rows:
        key = (r.get("landing_page_type") or "", r.get("landing_page_path") or "", r.get("day") or "")
        if not key[1]:
            continue
        b = byPath.setdefault(key, {
            "landing_page_type": key[0], "landing_page_path": key[1], "day": key[2],
            "online_store_visitors": 0, "sessions": 0, "sessions_with_cart_additions": 0,
            "bounces": 0, "sessions_that_reached_checkout": 0,
            "_bounce_weighted": 0.0, "_dur_weighted": 0.0, "_pps_weighted": 0.0, "_weight": 0,
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

    return {"byPath": byPath_rows, "rows": detail_rows, "totals": tot, "_source": "supabase"}


def fetch_orders_from_supabase(since: str, until: str) -> list:
    """Pull from orders + order_line_items, reshape to the legacy /api/orders
    flat-dict-per-order shape so the dashboard JS doesn't change."""
    # No ORDER BY — the user's Supabase `orders` table doesn't have an
    # index on created_at, so any server-side sort triggers a full table
    # scan that exceeds Postgres's 5s statement_timeout (error 57014).
    # Sorting happens client-side after fetch.
    # Permanent fix: `CREATE INDEX orders_created_at_idx ON orders (created_at);`
    orders = _supabase_data_get("orders", [
        ("select", "*"),
        ("created_at", f"gte.{since}T00:00:00"),
        ("created_at", f"lte.{until}T23:59:59"),
    ])
    if not orders:
        return []
    orders.sort(key=lambda o: o.get("created_at") or "", reverse=True)

    order_ids = [o.get("id") for o in orders if o.get("id")]
    line_items_by_order = {}
    if order_ids:
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
            "title": li.get("title") or "", "variantTitle": li.get("variant_title") or "",
            "sku": li.get("sku") or "", "quantity": int(li.get("quantity") or 0),
            "price": _f(li.get("unit_price")), "image": li.get("image_url") or "",
        } for li in items_raw]
        attrs = _parse_attrs(o.get("custom_attributes"))
        for k in ("utm_source","utm_medium","utm_campaign","utm_content","utm_term",
                  "full_url","gokwik_cid","cart_token","user_agent","customer_ip"):
            v = o.get(k)
            if v and not attrs.get(k):
                attrs[k] = v
        result.append({
            "id": o.get("id") or "", "name": o.get("name") or "",
            "createdAt": o.get("created_at") or "",
            "financialStatus": (o.get("financial_status") or "").upper(),
            "fulfillmentStatus": (o.get("fulfillment_status") or "").upper(),
            "total": _f(o.get("total_price")), "subtotal": _f(o.get("subtotal")),
            "discounts": _f(o.get("total_discounts")), "shipping": _f(o.get("total_shipping")),
            "tax": _f(o.get("total_tax")), "refunded": _f(o.get("total_refunded")),
            "currency": o.get("currency") or "INR",
            "discountCodes": _split_csv(o.get("discount_codes")),
            "note": o.get("note") or "",
            "tags": _split_csv(o.get("tags")),
            "cancelled": o.get("cancelled_at") is not None,
            "customer": {
                "name": f"{o.get('shipping_first_name','') or ''} {o.get('shipping_last_name','') or ''}".strip(),
                "email": "", "phone": o.get("shipping_phone") or "", "ordersCount": 0,
            },
            "address": {
                "city": o.get("shipping_city") or "",
                "province": o.get("shipping_province") or "",
                "country": o.get("shipping_country") or "",
                "zip": o.get("shipping_zip") or "",
            },
            "lineItems": items,
            "itemCount": sum(li["quantity"] for li in items),
            "customAttributes": attrs,
            "paymentGateway": o.get("payment_gateway") or o.get("gateway") or "",
            "source": o.get("source_name") or "",
            "utm_source": o.get("utm_source") or "",
            "utm_medium": o.get("utm_medium") or "",
            "utm_campaign": o.get("utm_campaign") or "",
            "utm_content": o.get("utm_content") or "",
            "utm_term": o.get("utm_term") or "",
        })
    return result


def fetch_ads(since: str = "", until: str = "") -> list:
    """Flat per-ad-per-day rows from Supabase primary_table, filtered on `date`.
    We pass an explicit ?limit=99999 alongside the Range header in
    _supabase_get so PostgREST cannot fall back to its 1000-row default.
    """
    params = [("select", "*"), ("order", "amount_spent_inr.desc"), ("limit", "99999")]
    if since:
        params.append(("date", f"gte.{since}"))
    if until:
        params.append(("date", f"lte.{until}"))
    rows = _supabase_get("primary_table", params)
    if (not rows) and (since or until):
        rows = _supabase_get("primary_table", [("select", "*"), ("order", "amount_spent_inr.desc"), ("limit", "99999")])
    return rows


def fetch_inventory_snapshot(date: str = "") -> dict:
    """Return inventory_snapshots for `date`; fall back to latest if missing."""
    if date:
        rows = _supabase_get("inventory_snapshots", [
            ("select", "*"),
            ("snapshot_date", f"eq.{date}"),
            ("limit", "5000"),
        ])
        if rows:
            return {"rows": rows, "date": date, "requestedDate": date, "isFallback": False}

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


# ── Routes ────────────────────────────────────────────────────────────────────

# Project root and the frontend directory. After the restructure the static
# SPA lives under frontend/ — HTML + css/ + js/ relative paths inside.
PROJECT_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR  = os.path.join(PROJECT_ROOT, "frontend")

@app.route("/")
def serve_dashboard():
    """Serve the dashboard HTML from frontend/."""
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/css/<path:fname>")
def serve_css(fname):
    return send_from_directory(os.path.join(FRONTEND_DIR, "css"), fname)


@app.route("/js/<path:fname>")
def serve_js(fname):
    return send_from_directory(os.path.join(FRONTEND_DIR, "js"), fname)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "shop": SHOP_DOMAIN})


@app.route("/api/_env_probe")
def env_probe():
    """Lightweight check of which env vars Vercel has loaded.
    Returns presence + length only (never the values)."""
    def shape(v):
        return {"present": bool(v), "len": len(v or "")}
    return jsonify({
        "SHOP_DOMAIN":          shape(SHOP_DOMAIN),
        "ADMIN_ACCESS_TOKEN":   shape(ADMIN_ACCESS_TOKEN),
        "SUPABASE_URL":         shape(SUPABASE_URL),
        "SUPABASE_SERVICE_KEY": shape(SUPABASE_SERVICE_KEY),
        "SAADAA_VAR":           shape(SAADAA_VAR),
        "SAADAA_KEY":           shape(SAADAA_KEY),
    })


@app.route("/api/traffic")
def traffic():
    since = request.args.get("since", "")
    until = request.args.get("until", "")
    date_param = request.args.get("date", "")
    if date_param and not since and not until:
        since = until = date_param
    DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
    if not re.match(DATE_RE, since or "") or not re.match(DATE_RE, until or ""):
        return jsonify({"error": "Missing or invalid ?since=YYYY-MM-DD&until=YYYY-MM-DD"}), 400
    if not SHOP_DOMAIN or not ADMIN_ACCESS_TOKEN:
        return jsonify({"error": "Server credentials not configured"}), 500
    try:
        return jsonify(fetch_traffic(since, until))
    except requests.HTTPError as e:
        return jsonify({"error": f"Shopify HTTP error: {e}"}), 502
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ads")
def ads():
    since = request.args.get("since", "") or ""
    until = request.args.get("until", "") or ""
    try:
        return jsonify(fetch_ads(since, until))
    except requests.HTTPError as e:
        return jsonify({"error": f"Supabase HTTP error: {e}"}), 502
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/inventory-snapshot")
def inventory_snapshot():
    date = request.args.get("date", "") or ""
    try:
        return jsonify(fetch_inventory_snapshot(date))
    except requests.HTTPError as e:
        return jsonify({"error": f"Supabase HTTP error: {e}"}), 502
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/inventory")
def inventory():
    if not SHOP_DOMAIN or not ADMIN_ACCESS_TOKEN:
        return jsonify({"error": "Server credentials not configured"}), 500
    try:
        return jsonify(fetch_inventory())
    except requests.HTTPError as e:
        return jsonify({"error": f"Shopify HTTP error: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sales")
def sales():
    since = request.args.get("since", "")
    until = request.args.get("until", "")
    if not since or not until:
        return jsonify({"error": "Missing ?since=YYYY-MM-DD&until=YYYY-MM-DD"}), 400
    if not SHOP_DOMAIN or not ADMIN_ACCESS_TOKEN:
        return jsonify({"error": "Server credentials not configured"}), 500
    try:
        return jsonify(fetch_sales(since, until))
    except requests.HTTPError as e:
        return jsonify({"error": f"Shopify HTTP error: {e}"}), 502
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders")
def orders():
    since = request.args.get("since", "")
    until = request.args.get("until", "")
    if not since or not until:
        return jsonify({"error": "Missing ?since=YYYY-MM-DD&until=YYYY-MM-DD"}), 400
    if not SHOP_DOMAIN or not ADMIN_ACCESS_TOKEN:
        return jsonify({"error": "Server credentials not configured"}), 500
    try:
        return jsonify(fetch_orders(since, until))
    except requests.HTTPError as e:
        return jsonify({"error": f"Shopify HTTP error: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Local dev entrypoint ──────────────────────────────────────────────────────
if __name__ == "__main__":
    if not SHOP_DOMAIN or not ADMIN_ACCESS_TOKEN:
        print("❌  Missing credentials in .env")
    else:
        print(f"\n  Saadaa Traffic Server (Flask)")
        print(f"  Running on  ->  http://localhost:5000")
        print(f"  Shop        ->  {SHOP_DOMAIN}\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
