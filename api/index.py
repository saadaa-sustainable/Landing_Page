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

def fetch_traffic(since: str, until: str) -> list:
    """Fetch page sessions for a date range. Each returned row carries the full
    UTM breakdown (source/medium/campaign/content/term) plus the day, so the
    dashboard can build per-landing-page UTM detail tables without a re-fetch.
    """
    ql = (
        "FROM sessions "
        "SHOW online_store_visitors, sessions, sessions_with_cart_additions, "
        "added_to_cart_rate, bounces, average_session_duration, "
        "pageviews_per_session, sessions_that_reached_checkout "
        "WHERE landing_page_path IS NOT NULL "
        "AND human_or_bot_session IN ('human', 'bot') "
        "GROUP BY landing_page_type, landing_page_path, day, "
        "utm_source, utm_medium, utm_campaign, utm_content, utm_term "
        "WITH TOTALS "
        f"SINCE {since} UNTIL {until} "
        "ORDER BY sessions DESC "
        "LIMIT 10000"
    )
    query = """
    query($ql: String!) {
      shopifyqlQuery(query: $ql) {
        tableData { columns { name dataType } rows }
        parseErrors
      }
    }
    """
    data = _gql(query, {"ql": ql})
    sqr = data.get("data", {}).get("shopifyqlQuery", {})
    if sqr.get("parseErrors"):
        raise ValueError(f"ShopifyQL parse error: {sqr['parseErrors']}")
    table = sqr.get("tableData", {})
    result = rows_to_dicts(table.get("columns", []), table.get("rows", []))

    # Compute bounce_rate from bounces / sessions (do not use Shopify's value directly)
    for row in result:
        bounces = float(row.get('bounces') or 0)
        sessions = float(row.get('sessions') or 0)
        row['bounce_rate'] = round(bounces / sessions * 100, 2) if sessions > 0 else 0

    return result


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
    "line_item_id",
    "customer_id",
    "new_or_returning_customer",
    "customer_last_order_date",
    "customer_number_of_orders",
    # __totals columns Shopify appends because of WITH TOTALS
    "net_items_sold__totals",
    "gross_sales__totals",
    "discounts__totals",
    "returns__totals",
    "net_sales__totals",
    "taxes__totals",
    "total_sales__totals",
)


def fetch_sales(since: str, until: str) -> list:
    """
    Direct ShopifyQL fetch — returns one row per
    day × product × order_utm × customer combination, plus the __totals columns
    Shopify appends. No custom aggregation: the dashboard collapses raw rows
    however the user wants. Numbers match Shopify's "Total sales by product"
    report exactly (including the LAST_CLICK_ATTRIBUTION semantics, which we
    get via order_utm_* — Shopify stamps last-click onto the order at checkout).
    """
    ql = (
        "FROM sales "
        "SHOW net_items_sold, gross_sales, discounts, returns, net_sales, taxes, total_sales "
        "WHERE product_title IS NOT NULL "
        "AND sales_channel != 'Return Prime: Order Return' "
        "GROUP BY day, product_title, product_type, "
        "order_utm_campaign, order_utm_content, order_utm_medium, order_utm_source, order_utm_term, "
        "line_item_id, customer_id, new_or_returning_customer, "
        "customer_last_order_date, customer_number_of_orders "
        "WITH TOTALS "
        f"SINCE {since} UNTIL {until} "
        "ORDER BY day ASC "
        "LIMIT 10000"
    )

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
    parse_errors = sqr.get("parseErrors") or []
    if parse_errors:
        raise ValueError(f"ShopifyQL parse error: {parse_errors}")

    table = sqr.get("tableData") or {}
    columns = table.get("columns") or []
    raw_rows = table.get("rows") or []
    if not raw_rows:
        return []

    if isinstance(raw_rows[0], dict):
        result = list(raw_rows)
    else:
        if not columns:
            return []
        col_names = [c.get("name", f"col_{i}") for i, c in enumerate(columns)]
        result = []
        for row in raw_rows:
            if isinstance(row, list):
                result.append({col_names[i]: v for i, v in enumerate(row) if i < len(col_names)})

    # Cast SHOW + __totals fields to numbers; pass dimensions through verbatim.
    for row in result:
        for f in SALES_NUMERIC_FIELDS:
            v = row.get(f)
            row[f] = float(v) if v not in (None, "") else 0
            t = row.get(f + "__totals")
            row[f + "__totals"] = float(t) if t not in (None, "") else 0
        for f in SALES_DIMENSION_FIELDS:
            if f in row and row[f] is None:
                row[f] = ""

    return result


def fetch_orders(since: str, until: str) -> list:
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
    """GET from Supabase REST. `params` is a list of (key, value) tuples
    so the same key (e.g. computed_at) can appear multiple times."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("Supabase not configured. Add SUPABASE_URL and SUPABASE_SERVICE_KEY env vars.")
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_ads(since: str = "", until: str = "") -> list:
    params = [
        ("select", "*"),
        ("order", "total_spend.desc"),
        ("limit", "500"),
    ]
    if since:
        params.append(("computed_at", f"gte.{since}T00:00:00"))
    if until:
        params.append(("computed_at", f"lte.{until}T23:59:59"))
    rows = _supabase_get("results_table", params)
    if (not rows) and (since or until):
        rows = _supabase_get("results_table", [
            ("select", "*"), ("order", "total_spend.desc"), ("limit", "500"),
        ])
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

@app.route("/")
def serve_dashboard():
    """Serve the dashboard HTML from the project root."""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return send_from_directory(root_dir, "dashboard_final.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "shop": SHOP_DOMAIN})


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
