"""
Shure Cosmetics Monitor — Doofinder API Edition
Monitors https://store.shure-cosmetics.co.uk/wholesale-cosmetics

Uses Doofinder search API (bypasses Cloudflare 403 on GitHub Actions).
Returns all products including availability, price, EAN/GTIN, image, brand.

Alerts on:
  🆕 New listings (in stock only)
  🟢 Back in stock
  📉 Price drops (>=5% AND >£0.05)

No HTML scraping — zero Cloudflare issues.

Env vars:
  DISCORD_WEBHOOK   required
  CHECK_INTERVAL    seconds (default 1800 = 30 min)
  RUN_ONCE          "true" for GitHub Actions

Deps: pip install requests
"""

import json
import os
import re
import time
import requests
from datetime import datetime, timezone
from urllib.parse import quote

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Doofinder credentials (public, embedded in Shure Cosmetics website)
DOOFINDER_HASH  = "7dcae9e1f0ff2d10b5a890c05b9345b4"
DOOFINDER_URL   = f"https://eu1-search.doofinder.com/5/search"
RESULTS_PER_PAGE = 100

SNAPSHOT_FILE   = "snapshot_shure.json"
BASELINE_FLAG   = "baseline_done_shure.txt"
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "1800"))
RUN_ONCE        = os.getenv("RUN_ONCE", "false").lower() == "true"
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

COLOUR_NEW   = 0xE91E8C
COLOUR_BACK  = 0x9B59B6
COLOUR_DROP  = 0x00C853

# ---------------------------------------------------------------------------
# DOOFINDER API — no Cloudflare, no scraping
# ---------------------------------------------------------------------------

def fetch_doofinder_page(page):
    """Fetch one page of products from Doofinder."""
    try:
        r = requests.get(
            DOOFINDER_URL,
            params={
                "hashid": DOOFINDER_HASH,
                "query":  "",
                "rpp":    RESULTS_PER_PAGE,
                "page":   page,
            },
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("results", []), data.get("total", 0)
    except Exception as e:
        print(f"  [!] Doofinder error (page {page}): {e}")
        return [], 0


def fetch_all_products():
    """Fetch all products from Doofinder API."""
    results, total = fetch_doofinder_page(1)
    if not results:
        return []

    total_pages = -(-total // RESULTS_PER_PAGE)  # ceiling div
    print(f"  Total: {total} products across {total_pages} pages")
    all_products = [parse_product(r) for r in results]

    for page in range(2, total_pages + 1):
        results, _ = fetch_doofinder_page(page)
        all_products.extend([parse_product(r) for r in results])
        time.sleep(0.5)

    return all_products


def parse_product(item):
    """Parse a Doofinder result into a clean product dict."""
    title = item.get("title", "")
    price = item.get("price") or item.get("best_price")

    # Extract per-unit price from title: "(£1.50/each)"
    pu_m = re.search(r"\(£([\d.]+)/each\)", title, re.IGNORECASE)
    per_unit = pu_m.group(1) if pu_m else ""

    # Clean title — remove trailing codes like "(9790)"
    clean_title = re.sub(r"\s*\([\d]+\)\s*$", "", title).strip()
    clean_title = re.sub(r"\s*\(£[\d.]+/each\)\s*", " ", clean_title).strip()

    # Pack qty from title: "6pcs", "X 6", "12 units"
    pack_m = re.search(r"\((\d+)pcs?\)", title, re.IGNORECASE) or \
             re.search(r"\bX\s*(\d+)\b", title, re.IGNORECASE)
    pack_qty = pack_m.group(1) if pack_m else ""

    return {
        "id":        item.get("id", ""),
        "title":     clean_title,
        "brand":     item.get("brand", ""),
        "url":       item.get("link", ""),
        "image":     item.get("image_link", ""),
        "ean":       item.get("gtin", ""),
        "price":     round(float(price), 2) if price else None,
        "per_unit":  per_unit,
        "pack_qty":  pack_qty,
        "in_stock":  item.get("availability", "") == "in stock",
        "category":  ", ".join(item.get("categories", [])),
    }

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def vat(price):
    f = safe_float(price)
    return f"{f * 1.2:.2f}" if f else str(price or "")


def sas_ean(ean, cost):
    if not ean:
        return None
    return f"https://sas.selleramp.com/sas/lookup/?search_term={ean}&sas_cost_price={vat(cost)}"


def sas_title(title, cost):
    return f"https://sas.selleramp.com/sas/lookup/?search_term={quote(title)}&sas_cost_price={vat(cost)}"

# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------

def _send(payload):
    if not DISCORD_WEBHOOK:
        return
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 5)) + 0.5
            time.sleep(wait)
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        else:
            r.raise_for_status()
    except Exception as e:
        print(f"  [!] Discord error: {e}")


def _base_fields(product):
    ean      = product.get("ean", "")
    brand    = product.get("brand", "")
    per_unit = product.get("per_unit", "")
    pack_qty = product.get("pack_qty", "")
    category = product.get("category", "")
    cost     = per_unit or str(product.get("price", ""))

    fields = [
        {"name": "🏷️ Brand",         "value": brand or "-",                          "inline": True},
        {"name": "📦 Pack Qty",       "value": f"{pack_qty} units" if pack_qty else "-","inline": True},
        {"name": "🏷️ Category",      "value": category or "-",                        "inline": True},
        {"name": "🔢 GTIN / EAN",     "value": f"`{ean}`" if ean else "-",             "inline": True},
        {"name": "💷 Per Unit (inc-VAT)", "value": f"£{vat(per_unit)}" if per_unit else "-", "inline": True},
        {"name": "📊 Stock",          "value": "✅ In stock" if product.get("in_stock") else "❌ OOS", "inline": True},
    ]

    ean_url   = sas_ean(ean, cost)
    title_url = sas_title(product.get("title", ""), cost)
    if ean_url:
        fields.append({"name": "🔍 SAS EAN",   "value": f"[Search by barcode]({ean_url})", "inline": True})
    fields.append(    {"name": "🔍 SAS Title", "value": f"[Search by title]({title_url})",  "inline": True})
    return fields


def _embed(title, url, colour, fields, product):
    e = {
        "title":     title,
        "url":       url,
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Shure Cosmetics Monitor • store.shure-cosmetics.co.uk"},
    }
    if product.get("image"):
        e["thumbnail"] = {"url": product["image"]}
    return e


def notify_new(product):
    price = product.get("price")
    pu    = product.get("per_unit", "")
    fields = [
        {"name": "💰 Pack Price (ex-VAT)", "value": f"**£{price:.2f}**" if price else "-", "inline": True},
        {"name": "💷 Per Unit (ex-VAT)",   "value": f"£{pu}" if pu else "-",               "inline": True},
    ] + _base_fields(product)
    _send({"embeds": [_embed(f"🆕  NEW — {product['title']}", product["url"], COLOUR_NEW, fields, product)]})
    print(f"  ✅ NEW: {product['title'][:60]}")


def notify_back(product):
    price = product.get("price")
    pu    = product.get("per_unit", "")
    fields = [
        {"name": "💰 Pack Price (ex-VAT)", "value": f"£{price:.2f}" if price else "-", "inline": True},
        {"name": "💷 Per Unit (ex-VAT)",   "value": f"£{pu}" if pu else "-",           "inline": True},
    ] + _base_fields(product)
    _send({"embeds": [_embed(f"🟢  BACK IN STOCK — {product['title']}", product["url"], COLOUR_BACK, fields, product)]})
    print(f"  ✅ BACK IN STOCK: {product['title'][:55]}")


def notify_drop(product, old_price, new_price, pct):
    pct_str  = f"{pct*100:.1f}%"
    abs_drop = old_price - new_price
    pu       = product.get("per_unit", "")
    icon     = "🔥" if pct >= 0.20 else ("💰" if pct >= 0.10 else "💵")
    colour   = 0x00C853 if pct >= 0.20 else (0x2ECC71 if pct >= 0.10 else 0x82E0AA)
    fields = [
        {"name": "💰 Was",              "value": f"£{old_price:.2f}",                    "inline": True},
        {"name": "💰 Now",              "value": f"**£{new_price:.2f}**",                 "inline": True},
        {"name": "📉 Drop",             "value": f"↓ £{abs_drop:.2f} (-{pct_str})",      "inline": True},
        {"name": "💷 Per Unit (ex-VAT)","value": f"£{pu}" if pu else "-",                "inline": True},
    ] + _base_fields(product)
    _send({"embeds": [_embed(f"{icon}  PRICE DROP -{pct_str} — {product['title']}", product["url"], colour, fields, product)]})
    print(f"  ✅ PRICE DROP -{pct_str}: {product['title'][:45]}")

# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            bak = f"{SNAPSHOT_FILE}.bak.{int(time.time())}"
            print(f"  [!] Snapshot corrupted — backed up to {bak}")
            try:
                os.rename(SNAPSHOT_FILE, bak)
            except OSError:
                pass
    return {}


def save_snapshot(data):
    tmp = SNAPSHOT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SNAPSHOT_FILE)


def to_entry(product):
    return {
        "title":     product.get("title", ""),
        "url":       product.get("url", ""),
        "image":     product.get("image", ""),
        "ean":       product.get("ean", ""),
        "brand":     product.get("brand", ""),
        "price":     product.get("price"),
        "per_unit":  product.get("per_unit", ""),
        "pack_qty":  product.get("pack_qty", ""),
        "in_stock":  product.get("in_stock", True),
        "category":  product.get("category", ""),
        "first_seen":  product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# MAIN CHECK
# ---------------------------------------------------------------------------

def run_check():
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n[{now_str}] Checking Shure Cosmetics via Doofinder API...")

    snapshot      = load_snapshot()
    known_ids     = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    products = fetch_all_products()
    if not products:
        print("  [!] No products returned — skipping")
        return

    current_ids = {p["id"] for p in products if p["id"]}
    new_ids     = current_ids - known_ids
    gone_ids    = known_ids - current_ids

    print(f"  {len(products)} products | {len(new_ids)} new | {len(gone_ids)} gone")

    if is_first_run:
        print("  First run — building baseline. No alerts.")

    alerts_sent = 0

    for product in products:
        pid          = product["id"]
        if not pid:
            continue
        old          = snapshot.get(pid, {})
        is_new       = pid in new_ids
        was_in_stock = old.get("in_stock", True)
        now_in_stock = product.get("in_stock", True)
        is_back      = not was_in_stock and now_in_stock and not is_new

        if is_first_run:
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
            continue

        # NEW
        if is_new:
            if now_in_stock:
                notify_new(product)
                alerts_sent += 1
                time.sleep(1.5)
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
            continue

        # BACK IN STOCK
        if is_back:
            notify_back(product)
            alerts_sent += 1
            time.sleep(1.5)

        # PRICE DROP
        elif now_in_stock:
            old_price = old.get("price")
            new_price = product.get("price")
            if old_price and new_price and old_price > 0:
                pct = (old_price - new_price) / old_price
                if pct >= 0.05 and (old_price - new_price) >= 0.05:
                    notify_drop(product, old_price, new_price, pct)
                    alerts_sent += 1
                    time.sleep(1.5)

        # Update snapshot
        entry = to_entry(product)
        entry["first_seen"] = old.get("first_seen", entry["first_seen"])
        snapshot[pid] = entry

    # Mark gone products as OOS
    for pid in gone_ids:
        if pid in snapshot:
            snapshot[pid]["in_stock"] = False
            snapshot[pid]["last_updated"] = datetime.now(timezone.utc).isoformat()

    save_snapshot(snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline saved — {len(snapshot)} products tracked.")
    else:
        print(f"  Done — {alerts_sent} alert(s) | {len(snapshot)} products tracked.")

# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    print("=" * 55)
    print("  Shure Cosmetics Monitor (Doofinder API)")
    print("  No scraping — immune to Cloudflare 403")
    print("=" * 55)

    if not DISCORD_WEBHOOK:
        print("  ⚠️  DISCORD_WEBHOOK not set")

    if RUN_ONCE:
        run_check()
        return

    while True:
        try:
            run_check()
        except Exception as e:
            print(f"  [!] Unexpected error: {e}")
        print(f"  Sleeping {CHECK_INTERVAL}s...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
