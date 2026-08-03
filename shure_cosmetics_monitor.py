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
            headers={
                "Origin":  "https://store.shure-cosmetics.co.uk",
                "Referer": "https://store.shure-cosmetics.co.uk/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
    """
    Parse a Doofinder result into a clean product dict.

    On Shure Cosmetics, Doofinder price = TOTAL price paid:
      - Single item:  price = unit price  (e.g. Rimmel Foundation = £3.25/bottle)
      - Multi-pack:   price = pack price  (e.g. Lip Oil 16pcs = £20.80 total)

    We calculate per_unit from pack quantity when available.
    SAS links use per_unit (inc-VAT) as cost price.
    """
    title = item.get("title", "")
    price_raw = item.get("price") or item.get("best_price")
    price = round(float(price_raw), 2) if price_raw else None

    # Clean title — remove trailing numeric codes like "(9790)"
    clean_title = re.sub(r"\s*\(\d{3,6}\)\s*$", "", title).strip()

    # Pack qty from title: "(16pcs)", "(12 units)" etc.
    pack_m = re.search(r"\((\d+)\s*pcs?\)", clean_title, re.IGNORECASE) or \
             re.search(r"\((\d+)\s*units?\)", clean_title, re.IGNORECASE)
    pack_qty = pack_m.group(1) if pack_m else ""

    # Per unit calculation:
    # - If pack qty found → per_unit = price / qty (this is a pack, divide it)
    # - If no pack qty → per_unit = price (Shure sells single units, price IS unit price)
    per_unit = ""
    if price:
        if pack_qty:
            try:
                per_unit = f"{price / int(pack_qty):.2f}"
            except (ValueError, ZeroDivisionError):
                per_unit = ""
        else:
            # Single unit — price is already the unit price
            per_unit = f"{price:.2f}"

    return {
        "id":        item.get("id", ""),
        "title":     clean_title,
        "brand":     item.get("brand", ""),
        "url":       item.get("link", ""),
        "image":     item.get("image_link", ""),
        "ean":       item.get("gtin", ""),
        "price":     price,
        "per_unit":  per_unit,
        "pack_qty":  pack_qty,
        "in_stock":  item.get("availability", "") == "in stock",
        "category":  ", ".join(item.get("categories", [])),
    }

# ---------------------------------------------------------------------------
# OPTIONS SCRAPING — get per-shade stock for (Options) products
# ---------------------------------------------------------------------------

OPTIONS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Referer": "https://store.shure-cosmetics.co.uk/wholesale-cosmetics",
}

OPTIONS_SESSION = requests.Session()
OPTIONS_SESSION.headers.update(OPTIONS_HEADERS)
_options_warmed = False


def _warmup():
    global _options_warmed
    if _options_warmed:
        return
    try:
        OPTIONS_SESSION.get("https://store.shure-cosmetics.co.uk/", timeout=15)
        _options_warmed = True
    except Exception:
        pass


def _is_oos_html(html):
    """Detect OOS signals in ShopWired page HTML."""
    html_lower = html.lower()
    return (
        "more-stock-coming-soon" in html_lower or
        "more stock coming soon" in html_lower or
        "out-of-stock" in html_lower or
        "outofstock" in html_lower or
        "notify me when in stock" in html_lower
    )


def fetch_options_shades(product):
    """
    Scrape an (Options) product page to get all shades with per-shade
    stock status. Returns list of shade dicts, or [] if scraping fails.
    """
    _warmup()
    url = product["url"]

    try:
        r = OPTIONS_SESSION.get(url, timeout=15)
        if not r.ok:
            return []
        html = r.text
    except Exception as e:
        print(f"    [!] Options page error: {e}")
        return []

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select")
    if not select:
        return []

    shades = []
    for option in select.find_all("option"):
        opt_text  = option.get_text(strip=True)
        opt_val   = option.get("value", "").strip()
        if not opt_val or opt_text.lower() in ("please select...", "select", ""):
            continue

        # Extract shade name — "010 Light Porcelain (3637)" → "010 Light Porcelain"
        shade_name = re.sub(r"\s*\(\d{3,6}\)\s*$", "", opt_text).strip()
        opt_oos    = option.has_attr("disabled") or "out of stock" in opt_text.lower()

        # Verify stock via ?variant=ID fetch (ShopWired serves per-variant HTML)
        if not opt_oos:
            try:
                vr = OPTIONS_SESSION.get(f"{url}?variant={opt_val}", timeout=10)
                if vr.ok:
                    opt_oos = _is_oos_html(vr.text)
                time.sleep(0.4)
            except Exception:
                pass

        shades.append({
            "shade":     shade_name,
            "variant_id":opt_val,
            "in_stock":  not opt_oos,
        })

    return shades


def shades_to_snapshot_key(product_id, shade_name):
    return f"{product_id}::{shade_name}"

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
    cost     = per_unit  # per_unit is always the individual unit price

    fields = [
        {"name": "🏷️ Brand",         "value": brand or "-",                          "inline": True},
        {"name": "📦 Pack Qty",       "value": f"{pack_qty} units" if pack_qty else "-","inline": True},
        {"name": "🏷️ Category",      "value": category or "-",                        "inline": True},
        {"name": "🔢 GTIN / EAN",     "value": f"`{ean}`" if ean else "-",             "inline": True},
        {"name": "💷 Unit Price (inc-VAT)", "value": f"£{vat(per_unit)}" if per_unit else "-", "inline": True},
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


def _price_fields(product):
    """Price fields shared across all embed types."""
    price    = product.get("price")
    pu       = product.get("per_unit", "")
    pack_qty = product.get("pack_qty", "")
    if pack_qty:
        return [
            {"name": f"💰 Pack Price (ex-VAT) ×{pack_qty}", "value": f"**£{price:.2f}**" if price else "-", "inline": True},
            {"name": "💷 Unit Price (ex-VAT)", "value": f"£{pu}" if pu else "-", "inline": True},
        ]
    else:
        return [
            {"name": "💰 Unit Price (ex-VAT)", "value": f"**£{pu}**" if pu else f"**£{price:.2f}**" if price else "-", "inline": True},
            {"name": "💷 Unit Price (inc-VAT)", "value": f"£{vat(pu)}" if pu else "-", "inline": True},
        ]


def _shades_field(shades):
    """Format all shades into a single Discord field showing stock status."""
    if not shades:
        return None
    lines = []
    for s in shades:
        icon = "✅" if s["in_stock"] else "❌"
        lines.append(f"{icon} {s['shade']}")
    return {"name": f"🎨 Shades ({len(shades)} total)", "value": "\n".join(lines), "inline": False}


def notify_new(product, shades=None):
    fields = _price_fields(product) + _base_fields(product)
    if shades:
        shade_field = _shades_field(shades)
        if shade_field:
            fields.insert(2, shade_field)
    _send({"embeds": [_embed(f"🆕  NEW — {product['title']}", product["url"], COLOUR_NEW, fields, product)]})
    print(f"  ✅ NEW: {product['title'][:60]}")


def notify_back(product, shades=None):
    fields = _price_fields(product) + _base_fields(product)
    if shades:
        shade_field = _shades_field(shades)
        if shade_field:
            fields.insert(2, shade_field)
    _send({"embeds": [_embed(f"🟢  BACK IN STOCK — {product['title']}", product["url"], COLOUR_BACK, fields, product)]})
    print(f"  ✅ BACK IN STOCK: {product['title'][:55]}")


def notify_options_change(product, shades, change_type="update"):
    """Single embed showing all shades when options product changes."""
    icon = "🆕" if change_type == "new" else "🟢" if change_type == "back" else "🔄"
    label = "NEW" if change_type == "new" else "BACK IN STOCK" if change_type == "back" else "SHADES UPDATE"
    fields = _price_fields(product) + _base_fields(product)
    shade_field = _shades_field(shades)
    if shade_field:
        fields.insert(2, shade_field)
    in_stock_count = sum(1 for s in shades if s["in_stock"])
    title = f"{icon}  {label} — {product['title']} ({in_stock_count}/{len(shades)} shades available)"
    colour = COLOUR_NEW if change_type == "new" else COLOUR_BACK if change_type == "back" else 0x3498DB
    _send({"embeds": [_embed(title, product["url"], colour, fields, product)]})
    print(f"  ✅ OPTIONS {label}: {product['title'][:50]} ({in_stock_count}/{len(shades)} shades)")


def notify_drop(product, old_price, new_price, pct, shades=None):
    pct_str  = f"{pct*100:.1f}%"
    abs_drop = old_price - new_price
    pu       = product.get("per_unit", "")
    icon     = "🔥" if pct >= 0.20 else ("💰" if pct >= 0.10 else "💵")
    colour   = 0x00C853 if pct >= 0.20 else (0x2ECC71 if pct >= 0.10 else 0x82E0AA)
    fields = [
        {"name": "💰 Was",               "value": f"£{old_price:.2f}",               "inline": True},
        {"name": "💰 Now",               "value": f"**£{new_price:.2f}**",            "inline": True},
        {"name": "📉 Drop",              "value": f"↓ £{abs_drop:.2f} (-{pct_str})", "inline": True},
        {"name": "💷 Unit Price (ex-VAT)","value": f"£{pu}" if pu else "-",          "inline": True},
    ] + _base_fields(product)
    if shades:
        shade_field = _shades_field(shades)
        if shade_field:
            fields.insert(4, shade_field)
    _send({"embeds": [_embed(f"{icon}  PRICE DROP -{pct_str} — {product['title']}", product["url"], colour, fields, product)]})
    print(f"  ✅ PRICE DROP -{pct_str}: {product['title'][:45]}")

def notify_shade_change(product, shade, change_type):
    """Alert when a single shade changes status."""
    icon   = "🆕" if change_type == "new" else "🟢"
    label  = "NEW SHADE" if change_type == "new" else "SHADE BACK IN STOCK"
    colour = COLOUR_NEW if change_type == "new" else COLOUR_BACK
    price  = product.get("price")
    pu     = product.get("per_unit", "")
    ean    = product.get("ean", "")
    cost   = pu or str(price or "")

    fields = [
        {"name": "🎨 Shade",              "value": f"**{shade['shade']}**",            "inline": True},
        {"name": "💰 Unit Price (ex-VAT)","value": f"£{pu}" if pu else f"£{price:.2f}" if price else "-", "inline": True},
        {"name": "💷 Unit Price (inc-VAT)","value": f"£{vat(pu or str(price or ''))}", "inline": True},
        {"name": "🏷️ Brand",             "value": product.get("brand", "-"),           "inline": True},
        {"name": "🔢 GTIN / EAN",         "value": f"`{ean}`" if ean else "-",          "inline": True},
        {"name": "📊 Stock",              "value": "✅ In stock",                        "inline": True},
    ]
    ean_url = sas_ean(ean, cost)
    if ean_url:
        fields.append({"name": "🔍 SAS EAN",   "value": f"[Search by barcode]({ean_url})", "inline": True})
    fields.append(    {"name": "🔍 SAS Title", "value": f"[Search by title]({sas_title(product.get('title',''), cost)})", "inline": True})

    title = f"{icon}  {label} — {product['title']} - {shade['shade']}"
    _send({"embeds": [_embed(title, product["url"], colour, fields, product)]})
    print(f"  ✅ {label}: {product['title'][:40]} - {shade['shade']}")


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
        is_options   = "(options)" in product.get("title", "").lower()

        if is_first_run:
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
            continue

        # Fetch per-shade data for ALL options products every cycle
        # This lets us detect per-shade back-in-stock changes
        shades = None
        if is_options and not is_first_run:
            shades = fetch_options_shades(product)
            if shades:
                in_stk = sum(1 for s in shades if s["in_stock"])
                print(f"    {len(shades)} shades ({in_stk} in stock): {product['title'][:40]}...")

                # Check each shade against snapshot for per-shade changes
                for shade in shades:
                    shade_key = shades_to_snapshot_key(pid, shade["shade"])
                    old_shade  = snapshot.get(shade_key, {})
                    shade_was_in_stock = old_shade.get("in_stock", True)
                    shade_is_new       = shade_key not in snapshot

                    if shade_is_new and shade["in_stock"]:
                        # New shade appeared and is in stock
                        notify_shade_change(product, shade, "new")
                        alerts_sent += 1
                        time.sleep(1.0)
                    elif not shade_is_new and not shade_was_in_stock and shade["in_stock"]:
                        # Shade was OOS, now back in stock
                        notify_shade_change(product, shade, "back")
                        alerts_sent += 1
                        time.sleep(1.0)

                    # Update per-shade snapshot
                    snapshot[shade_key] = {
                        "shade":      shade["shade"],
                        "product_id": pid,
                        "title":      product.get("title", ""),
                        "in_stock":   shade["in_stock"],
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    }

        elif is_options and is_first_run:
            # On baseline, record all shades without alerting
            shades = fetch_options_shades(product)
            if shades:
                for shade in shades:
                    shade_key = shades_to_snapshot_key(pid, shade["shade"])
                    snapshot[shade_key] = {
                        "shade":      shade["shade"],
                        "product_id": pid,
                        "title":      product.get("title", ""),
                        "in_stock":   shade["in_stock"],
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                    }

        # NEW (product level)
        if is_new:
            if now_in_stock:
                if is_options and shades:
                    notify_options_change(product, shades, "new")
                else:
                    notify_new(product)
                alerts_sent += 1
                time.sleep(1.5)
            entry = to_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[pid] = entry
            continue

        # BACK IN STOCK (whole product was fully OOS)
        if is_back:
            if is_options and shades:
                notify_options_change(product, shades, "back")
            else:
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
                    notify_drop(product, old_price, new_price, pct, shades)
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
