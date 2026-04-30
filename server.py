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

# ── Load credentials from .env ──
load_dotenv()

SHOP_DOMAIN         = os.getenv("SHOP_DOMAIN")
ADMIN_ACCESS_TOKEN  = os.getenv("ADMIN_ACCESS_TOKEN")

if not SHOP_DOMAIN or not ADMIN_ACCESS_TOKEN:
    raise SystemExit(
        "❌  Missing credentials.\n"
        "    Add SHOP_DOMAIN and ADMIN_ACCESS_TOKEN to your .env file."
    )


# ── Shopify fetch ──────────────────────────────────────────────────────────────

def fetch_traffic(date: str) -> list:
    """Fetch all page sessions from ShopifyQL for a single date with UTM data."""

    ql = (
        "FROM sessions "
        "SHOW online_store_visitors, sessions, sessions_with_cart_additions, "
        "added_to_cart_rate, bounces, average_session_duration, "
        "pageviews_per_session, sessions_that_reached_checkout "
        "WHERE landing_page_path IS NOT NULL "
        "AND human_or_bot_session IN ('human', 'bot') "
        "GROUP BY landing_page_type, landing_page_path, "
        "utm_source, utm_medium, utm_campaign, day "
        "WITH TOTALS "
        f"SINCE {date} UNTIL {date} "
        "ORDER BY sessions DESC "
        "LIMIT 10000"
    )

    query = """
    query($ql: String!) {
      shopifyqlQuery(query: $ql) {
        tableData {
          columns { name dataType }
          rows
        }
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
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("errors"):
        raise ValueError(f"GraphQL error: {data['errors']}")

    sqr = data.get("data", {}).get("shopifyqlQuery", {})

    parse_errors = sqr.get("parseErrors", [])
    if parse_errors:
        raise ValueError(f"ShopifyQL parse error: {parse_errors}")

    table = sqr.get("tableData", {})
    columns = table.get("columns", [])
    raw_rows = table.get("rows", [])

    if not raw_rows:
        return []

    # If rows are already dicts (Shopify sometimes returns them this way), pass through
    if raw_rows and isinstance(raw_rows[0], dict):
        result = raw_rows
    else:
        # If rows are arrays, map column indices to names
        if not columns:
            return []

        col_names = [c.get("name", f"col_{i}") for i, c in enumerate(columns)]

        result = []
        for row in raw_rows:
            if isinstance(row, list):
                entry = {}
                for i, val in enumerate(row):
                    if i < len(col_names):
                        entry[col_names[i]] = val
                result.append(entry)

    # Compute bounce_rate from bounces / sessions instead of using Shopify's value
    for row in result:
        bounces = float(row.get('bounces') or 0)
        sessions = float(row.get('sessions') or 0)
        row['bounce_rate'] = round(bounces / sessions * 100, 2) if sessions > 0 else 0

    return result


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

def fetch_sales(since: str, until: str) -> list:
    """
    Fetch product-level sales data from Shopify using ShopifyQL.
    Returns: [{product_title, product_vendor, product_type, net_items_sold, gross_sales, discounts, returns, net_sales, taxes, total_sales}]
    """

    ql = (
        "FROM sales "
        "SHOW net_items_sold, gross_sales, discounts, returns, net_sales, taxes, total_sales "
        "WHERE product_title IS NOT NULL "
        "GROUP BY product_title, product_vendor, product_type WITH TOTALS "
        f"SINCE {since} UNTIL {until} "
        "ORDER BY total_sales DESC "
        "LIMIT 1000"
    )

    query = """
    query($ql: String!) {
      shopifyqlQuery(query: $ql) {
        tableData {
          columns { name dataType }
          rows
        }
        parseErrors
      }
    }
    """

    print(f"  [Sales] ShopifyQL: {ql}")

    sales_resp = requests.post(
        f"https://{SHOP_DOMAIN}/admin/api/2025-10/graphql.json",
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": ADMIN_ACCESS_TOKEN,
        },
        json={"query": query, "variables": {"ql": ql}},
        timeout=30,
    )
    sales_resp.raise_for_status()
    body = sales_resp.json()

    if body.get("errors"):
        print(f"  [Sales] GraphQL errors: {body['errors']}")
        raise ValueError(f"GraphQL error: {body['errors']}")

    sqr = body.get("data", {}).get("shopifyqlQuery", {})

    parse_errors = sqr.get("parseErrors") or []
    if parse_errors:
        print(f"  [Sales] Parse errors: {parse_errors}")
        raise ValueError(f"ShopifyQL parse error: {parse_errors}")

    table = sqr.get("tableData") or {}
    if not table:
        print(f"  [Sales] No tableData. Response: {str(sqr)[:500]}")
        return []

    columns = table.get("columns", [])
    raw_rows = table.get("rows", [])
    print(f"  [Sales] Got {len(columns)} columns, {len(raw_rows)} rows")

    if not raw_rows:
        return []

    # If rows are already dicts, pass through
    if raw_rows and isinstance(raw_rows[0], dict):
        return raw_rows

    if not columns:
        return []

    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(columns)]
    result = []
    for row in raw_rows:
        if isinstance(row, list):
            entry = {}
            for i, val in enumerate(row):
                if i < len(col_names):
                    entry[col_names[i]] = val
            result.append(entry)

    return result


# ── HTTP server ────────────────────────────────────────────────────────────────

# ── Orders fetch ──────────────────────────────────────────────────────────────

def fetch_orders(since: str, until: str) -> list:
    """
    Fetch orders from Shopify Admin GraphQL API with full details.
    Returns list of order dicts with line items, customer, custom attributes.
    """
    all_orders = []
    current_cursor = None
    max_pages = 40  # 50 * 40 = 2000 orders max

    for page in range(max_pages):
        after_clause = f', after: "{current_cursor}"' if current_cursor else ""

        # Shopify query filter: use simple date strings
        query_filter = f"created_at:>={since} created_at:<={until}"

        gql = """
query {
  orders(first: 50, query: \"""" + query_filter + """\", sortKey: CREATED_AT, reverse: true""" + after_clause + """) {
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

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # ── GET / → serve dashboard_final.html ──
        if parsed.path == "/":
            html_path = os.path.join(os.path.dirname(__file__), "dashboard_final.html")
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    content = f.read().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_json(404, {"error": "dashboard_final.html not found"})
            return

        # ── GET /api/health ──
        if parsed.path == "/api/health":
            self.send_json(200, {"status": "ok", "shop": SHOP_DOMAIN})
            return

        # ── GET /api/traffic?date=YYYY-MM-DD ──
        if parsed.path == "/api/traffic":
            date_param = params.get("date", [None])[0]

            if not date_param or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_param):
                self.send_json(400, {"error": "Missing or invalid date. Use ?date=YYYY-MM-DD"})
                return

            try:
                rows = fetch_traffic(date_param)
                self.send_json(200, rows)
            except requests.HTTPError as e:
                self.send_json(502, {"error": f"Shopify HTTP error: {e}"})
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
            date_param = get_param("date")
            if not date_param or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_param):
                self.send_json(400, {"error": "Missing or invalid date. Use ?date=YYYY-MM-DD"})
                return
            try:
                rows = fetch_traffic(date_param)
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