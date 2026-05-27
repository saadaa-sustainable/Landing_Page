"""
snapshot_inventory.py
─────────────────────
Fetches inventory from your local server.py (which already has valid Shopify credentials)
and saves a daily snapshot to Supabase for STR calculations.

Usage:
    pip install supabase requests python-dotenv
    
    # Make sure server.py is running first:
    python server.py
    
    # Then in a new terminal:
    python snapshot_inventory.py
"""

import requests
import os
from datetime import date
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# ── CONFIG ───────────────────────────────────────────────────────────────────
SERVER_URL    = os.environ.get("SERVER_URL", "http://localhost:5000")   # Your running server.py
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")             # Service role key for writes
# ─────────────────────────────────────────────────────────────────────────────


def fetch_inventory_from_server() -> list:
    """
    Call server.py's /api/inventory endpoint — it already has valid Shopify credentials.
    Returns list of color variant dicts.
    """
    print(f"Calling {SERVER_URL}/api/inventory ...")
    resp = requests.get(f"{SERVER_URL}/api/inventory", timeout=120)
    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, dict) and data.get("error"):
        raise ValueError(f"Server error: {data['error']}")

    if not isinstance(data, list):
        raise ValueError(f"Unexpected response format: {type(data)}")

    print(f"  Got {len(data)} color variants from server")
    return data


def fetch_inventory_direct() -> list:
    """
    Fetch directly from Shopify by importing server.py's fetch_inventory().
    No HTTP server required — this is the path used by the scheduled cron job
    so it works even if server.py isn't running at 12:10am.
    """
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "_srv_for_snapshot", os.path.join(here, "server.py")
    )
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    print("Fetching inventory directly from Shopify (no local server needed)...")
    data = srv.fetch_inventory()
    print(f"  Got {len(data)} color variants from Shopify")
    return data


def fetch_inventory_smart() -> list:
    """
    Try the local server first (fast cache when it's already up); on any failure
    fall back to a direct Shopify call. Cron path tolerates either.
    """
    try:
        return fetch_inventory_from_server()
    except Exception as e:
        print(f"  Local server unavailable ({e}); falling back to direct fetch.")
        return fetch_inventory_direct()


def aggregate_by_product(variants: list) -> list:
    """
    Aggregate all color variants into product-level totals.
    Also keeps individual color_sku rows for granular STR tracking.
    
    Returns both:
      - product-level rows  (color_sku = None, total = sum of all colors)
      - color-level rows    (color_sku = SDCPBL, total = that color's stock)
    """
    product_totals = {}   # product_title → total_stock
    color_rows = []       # individual color variant rows

    for v in variants:
        # Skip combos/unknown
        if not v.get("hasInvData") or v.get("stockStatus") == "unknown":
            continue
        if not v.get("colorSku"):
            continue

        title = v.get("name", "")
        # Strip the " - Color" suffix to get base product title
        # server.py sets name as "Product Title - Color"
        base_title = title
        color = v.get("color", "")
        if color and color != "Default" and title.endswith(f" - {color}"):
            base_title = title[: -(len(color) + 3)]

        sku = v.get("colorSku", "")
        stock = v.get("totalStock", 0) or 0

        # Accumulate product-level total
        if base_title not in product_totals:
            product_totals[base_title] = 0
        product_totals[base_title] += stock

        # Color-level row
        color_rows.append({
            "product_title": base_title.lower().strip(),
            "color_sku": sku,
            "total_stock": stock,
        })

    # Product-level rows (for dashboard STR which matches by product_title)
    product_rows = [
        {
            "product_title": title.lower().strip(),
            # Product-level rows use an empty string (not NULL) so the table
            # view doesn't show "null" cells. Frontend's STR computation reads
            # `color_sku || ''` so both null and "" are treated as product-level.
            "color_sku": "",
            "total_stock": total,
        }
        for title, total in product_totals.items()
    ]

    print(f"  Aggregated: {len(product_rows)} products, {len(color_rows)} color variants")
    return product_rows + color_rows


def save_snapshot(supabase: Client, rows: list, today: date):
    """
    Insert today's snapshot rows. Re-running this on the same day appends a
    second set of rows for snapshot_date=today; clean up duplicates manually
    in Supabase if needed.
    """
    today_str = today.isoformat()
    print(f"Saving snapshot for {today_str} ...")

    # Add snapshot_date to all rows
    for row in rows:
        row["snapshot_date"] = today_str

    # Batch insert in chunks of 500
    chunk_size = 500
    inserted = 0
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        supabase.table("inventory_snapshots").insert(chunk).execute()
        inserted += len(chunk)
        print(f"  Inserted {inserted}/{len(rows)} rows...")

    print(f"✓ Snapshot saved: {len(rows)} rows for {today_str}")


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit(
            "❌ Missing Supabase credentials.\n"
            "   Add SUPABASE_URL and SUPABASE_SERVICE_KEY to your .env file."
        )

    print(f"\n[{date.today()}] Starting inventory snapshot...")

    # 1. Fetch from server.py if it's running, else go direct to Shopify.
    variants = fetch_inventory_smart()

    # 2. Aggregate into product + color rows
    rows = aggregate_by_product(variants)

    # 3. Save to Supabase
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    save_snapshot(supabase, rows, date.today())

    print("\nDone ✓\n")


if __name__ == "__main__":
    main()