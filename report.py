"""
Saadaa | Orders x Meta Ads - Merge
====================================
Merges Orders with Ads on:
  Orders `Order Date` (from Created At)  ==  Ads `Date`
  Orders `Utm Content` (cleaned)         ==  Ads `Ad Name` (cleaned)

Context-aware fixes applied before matching
-------------------------------------------
Fix 1 - GoKwik single-quote wrapping:
  'SMCP_VRP_US_916...'  ->  SMCP_VRP_US_916...

Fix 2 - Broken UTF-8 em-dash encoding:
  Shopify: ...20/12/25 ae Copy  (mojibake)
  Meta:    ...20/12/25 - Copy   (correct)
  Exact byte sequence found: U+00E2 U+20AC U+201C -> U+2013

Fix 3 - Numeric-only Utm Content values:
  e.g. 120244000000000000 = raw Meta Ad ID, not a name string -> treated as null

Output: Merged_Orders_Ads.csv  (matched rows only)
"""

import os
import re
import pandas as pd

ORDERS_FILE = "order-report-20251231-20260331.csv"   # or Book1.xlsx
ADS_FILE    = "ADS_Data - Meta Account 1 (3).csv"
OUTPUT_FILE = "Merged_Orders_Ads.csv"


def clean_ad_name(s):
    """
    Normalise a UTM Content / Ad Name string so both sides of the join
    use identical text.
    """
    if pd.isna(s):
        return None
    s = str(s).strip().strip("'\"")
    if s in ("", "nan", "NaN", "None", "none"):
        return None
    # Fix 3: pure numeric -> not an ad name string
    if re.fullmatch(r'[\d.eE+\-]+', s):
        return None
    # Fix 2: broken em-dash encoding (confirmed exact byte sequence in your data)
    s = re.sub('\u00e2\u20ac\u201c', '\u2013', s)
    s = re.sub('ae"', '\u2013', s)
    # Collapse multiple spaces
    return re.sub(r'  +', ' ', s).strip()


# Step 1: Load
print("Loading data...")
ext = os.path.splitext(ORDERS_FILE)[1].lower()
orders = (pd.read_excel(ORDERS_FILE) if ext in (".xlsx", ".xls")
          else pd.read_csv(ORDERS_FILE, low_memory=False))
ads = pd.read_csv(ADS_FILE, low_memory=False)
print(f"  Orders loaded : {len(orders):,} rows")
print(f"  Ads loaded    : {len(ads):,} rows")


# Step 2: Parse Orders Created At -> Order Date (YYYY-MM-DD)
orders["Order Date"] = (
    pd.to_datetime(orders["Created At"], errors="coerce")
      .dt.strftime("%Y-%m-%d")
)
bad_dates = orders["Order Date"].isna().sum()
if bad_dates:
    print(f"  [WARN] {bad_dates} orders had unparseable dates")
print(f"  Order Date range : {orders['Order Date'].min()} -> {orders['Order Date'].max()}")


# Step 3: Ensure Ads Date is YYYY-MM-DD
ads["Date"] = (
    pd.to_datetime(ads["Date"], errors="coerce")
      .dt.strftime("%Y-%m-%d")
)
print(f"  Ads Date range   : {ads['Date'].min()} -> {ads['Date'].max()}")


# Step 4: Build cleaned join keys
orders["_utm_content_key"] = orders["Utm Content"].apply(clean_ad_name)
ads["_ad_name_key"]        = ads["Ad Name"].apply(clean_ad_name)

meta_orders = orders[orders["Utm Source"].astype(str).str.strip("'\"").str.upper() == "META"]
print(f"\n  Utm Content cleaning (META orders):")
print(f"    Valid keys ready for matching : {meta_orders['_utm_content_key'].notna().sum()}")
print(f"    Numeric IDs / blanks dropped  : {meta_orders['_utm_content_key'].isna().sum()}")


# Step 5: Left join on [Order Date + Utm Content] == [Date + Ad Name]
print("\nMerging...")
total_before = len(orders)

merged = pd.merge(
    orders,
    ads,
    left_on  = ["Order Date", "_utm_content_key"],
    right_on = ["Date",       "_ad_name_key"],
    how      = "left",
    suffixes = ("_order", "_ads"),
)
merged.drop(columns=["_utm_content_key", "_ad_name_key"], inplace=True)


# Keep only matched rows (rows where an ad was found)
ad_name_col = "Ad Name" if "Ad Name" in merged.columns else "Ad Name_ads"
merged = merged[merged[ad_name_col].notna()].reset_index(drop=True)


# Step 6: Save
merged.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")

print(f"\nDone -> {OUTPUT_FILE}")
print(f"  Total orders        : {total_before:,}")
print(f"  Matched rows saved  : {len(merged):,}  ({len(merged)/total_before*100:.1f}%)")
print(f"  Unmatched dropped   : {total_before - len(merged):,}")
print(f"  Columns in output   : {len(merged.columns)}")