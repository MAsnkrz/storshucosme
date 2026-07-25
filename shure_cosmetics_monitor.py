"""
Shure Cosmetics Monitor — Clean Rewrite
Monitors https://store.shure-cosmetics.co.uk/wholesale-cosmetics?all=1

Alerts on:
  🆕 New product listings (in stock only)
  🟢 Back in stock (whole product or individual shade)
  📉 Price drops (>=5% AND >£0.05)

Key fixes over previous version:
  - No more repeat pings — existing products only scraped when needed
  - Options products handled properly — each shade tracked individually
    as a separate snapshot key (handle::shade_name)
  - OOS detection uses scraper logic (disabled attr + text signals)
  - Atomic snapshot saves

Deps: pip install requests beautifulsoup4

Env vars:
  DISCORD_WEBHOOK   required
  CHECK_INTERVAL    seconds between checks (default 1800 = 30 min)
  RUN_ONCE          "true" for GitHub Actions
"""

import json
import os
import re
import time
import random
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from urllib.parse import quote

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL        = "https://store.shure-cosmetics.co.uk"
LISTING_URL     = f"{BASE_URL}/wholesale-cosmetics"
SNAPSHOT_FILE   = "snapshot_shure.json"
BASELINE_FLAG   = "baseline_done_shure.txt"
REQUEST_DELAY   = 2.0
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "1800"))
RUN_ONCE        = os.getenv("RUN_ONCE", "false").lower() == "true"
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

COLOUR_NEW   = 0xE91E8C
COLOUR_BACK  = 0x9B59B6
COLOUR_DROP  = 0x00C853

# ---------------------------------------------------------------------------
# SCRAPING — listing page
# ---------------------------------------------------------------------------

def fetch_all_listing_products():
    """
    Scrape /wholesale-cosmetics?all=1 with pagination.
    Returns flat list of basic product dicts.
    OOS products on the listing are included so we can detect restocks.
    """
    all_products = []
    seen_urls    = set()
    page         = 1

    while True:
        params = {"all": "1"}
        if page > 1:
            params["page"] = str(page)
        try:
            r = SESSION.get(LISTING_URL, params=params, timeout=20)
            if r.status_code == 404:
                break
            r.raise_for_status()
        except Exception as e:
            print(f"  [!] Listing page {page} error: {e}")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        batch = []

        for h3 in soup.find_all("h3"):
            a = h3.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            if not href.startswith("http"):
                href = BASE_URL + href
            if any(x in href for x in ["?", "#", "/account", "/wishlist", "/cart"]):
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            title = a.get_text(strip=True)
            if not title or len(title) < 3:
                continue

            is_options = "(options)" in title.lower() or href.rstrip("/").endswith("-options")

            # Walk up to container for price/stock
            container = h3.parent
            for _ in range(5):
                if container is None:
                    break
                t = container.get_text(" ", strip=True)
                if re.search(r"£[\d.]+", t):
                    break
                container = container.parent

            text = container.get_text(" ", strip=True) if container else ""

            # OOS for non-options products only
            listing_oos = False
            if not is_options:
                if "more stock coming soon" in text.lower():
                    listing_oos = True
                if "out of stock" in text.lower() and "add to" not in text.lower():
                    listing_oos = True

            prices = re.findall(r"£\s*([\d.]+)", text)
            price = prices[0] if prices else ""

            img_el = container.find("img") if container else None
            image = ""
            if img_el:
                src = img_el.get("data-src") or img_el.get("src") or ""
                if src and "logo" not in src.lower():
                    image = src if src.startswith("http") else BASE_URL + src

            batch.append({
                "url":        href,
                "handle":     href.replace(BASE_URL, "").strip("/"),
                "title":      title,
                "price":      price,
                "image":      image,
                "is_options": is_options,
                "in_stock":   not listing_oos,
            })

        all_products.extend(batch)
        print(f"  Listing page {page}: +{len(batch)} (total: {len(all_products)})")

        # Has next page?
        total_m = re.search(r"Showing\s+[\d\s–-]+of\s+([\d,]+)", r.text)
        has_next = False
        if total_m:
            total = int(total_m.group(1).replace(",", ""))
            if page * 30 < total:
                has_next = True
        if not has_next:
            has_next = bool(soup.find("a", href=lambda h: h and f"page={page+1}" in (h or "")))

        if not has_next or not batch:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    return all_products


# ---------------------------------------------------------------------------
# SCRAPING — product detail page (options-aware)
# ---------------------------------------------------------------------------

def _extract_ean(html, soup):
    m = re.search(r"Barcode\s*\(?GTIN/EAN\)?\s*:?\s*([0-9]{7,14})", html, re.IGNORECASE)
    if m:
        return m.group(1)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                ean = item.get("gtin13") or item.get("gtin") or item.get("gtin8") or ""
                if ean and re.match(r"^\d{7,14}$", str(ean)):
                    return str(ean)
        except Exception:
            pass
    text = soup.get_text(" ", strip=True)
    for pat in [r"EAN\s*:\s*([0-9]{7,14})", r"Barcode\s*:\s*([0-9]{7,14})"]:
        m2 = re.search(pat, text, re.IGNORECASE)
        if m2:
            return m2.group(1)
    return ""


def _is_oos_page(text):
    oos_signals = ["more stock coming soon", "notify me when in stock",
                   "notify me when available", "currently out of stock"]
    if any(s in text.lower() for s in oos_signals):
        return True
    if "out of stock" in text.lower() and "add to basket" not in text.lower():
        return True
    return False


def scrape_product_detail(url, is_options):
    """
    Scrape a product detail page.
    Returns a list of variant dicts — one entry per in-stock shade for
    Options products, or a single entry for regular products.

    Each dict: {handle_key, shade, price, ean, image, in_stock}
    handle_key = "handle" for single products,
                 "handle::shade_name" for options variants.
    """
    handle = url.replace(BASE_URL, "").strip("/")
    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        html = r.text
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        print(f"  [!] Detail fetch error: {e}")
        return []

    og_img = soup.find("meta", property="og:image")
    image  = og_img["content"] if og_img else ""

    price_els = soup.find_all(class_=lambda c: c and "price" in str(c).lower())
    price = ""
    for el in price_els:
        m = re.search(r"£\s*([\d.]+)", el.get_text())
        if m:
            try:
                if float(m.group(1)) > 0:
                    price = m.group(1)
                    break
            except ValueError:
                pass

    ean  = _extract_ean(html, soup)
    text = soup.get_text(" ", strip=True)

    if not is_options:
        return [{
            "handle_key": handle,
            "shade":      "",
            "price":      price,
            "ean":        ean,
            "image":      image,
            "in_stock":   not _is_oos_page(text),
        }]

    # --- Options product: parse select dropdown ---
    select = soup.find("select")
    if not select:
        # No dropdown found — treat as single
        return [{
            "handle_key": handle,
            "shade":      "",
            "price":      price,
            "ean":        ean,
            "image":      image,
            "in_stock":   not _is_oos_page(text),
        }]

    results = []
    for option in select.find_all("option"):
        opt_text = option.get_text(strip=True)
        opt_val  = option.get("value", "").strip()

        if not opt_val or opt_text.lower() in ("please select...", "select", ""):
            continue

        # OOS detection: disabled attr or "out of stock" / "unavailable" in text
        is_oos = (
            option.has_attr("disabled") or
            "out of stock" in opt_text.lower() or
            "unavailable" in opt_text.lower()
        )

        # Extract shade name — format: "SHADE_NAME (CODE)" or "SHADE_NAME (Out of Stock)"
        m_code = re.search(r"^(.+?)\s*\((\d{3,6})\)\s*$", opt_text)
        shade_name = m_code.group(1).strip() if m_code else re.sub(r"\s*\([^)]*\)\s*$", "", opt_text).strip()

        results.append({
            "handle_key": f"{handle}::{shade_name}",
            "shade":      shade_name,
            "price":      price,
            "ean":        ean,
            "image":      image,
            "in_stock":   not is_oos,
        })

    return results


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


def _embed(title, url, colour, fields, image=""):
    embed = {
        "title":     title,
        "url":       url,
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Shure Cosmetics Monitor • store.shure-cosmetics.co.uk"},
    }
    if image:
        embed["thumbnail"] = {"url": image}
    return embed


def _fields(variant):
    price  = variant.get("price", "")
    ean    = variant.get("ean", "")
    shade  = variant.get("shade", "")
    inc    = f"{float(price)*1.2:.2f}" if price else ""

    rows = []
    if shade:
        rows.append({"name": "🎨 Shade",       "value": shade,                        "inline": True})
    rows.append(    {"name": "💰 Price (ex-VAT)", "value": f"£{price}" if price else "-", "inline": True})
    rows.append(    {"name": "💷 Price (inc-VAT)","value": f"£{inc}" if inc else "-",   "inline": True})
    rows.append(    {"name": "🔢 EAN",           "value": f"`{ean}`" if ean else "-",   "inline": True})

    sas_base = f"https://sas.selleramp.com/sas/lookup/?sas_cost_price={inc}"
    if ean:
        rows.append({"name": "🔍 SAS EAN",   "value": f"[Search by barcode]({sas_base}&search_term={ean})", "inline": True})
    title = variant.get("title", "")
    if title:
        rows.append({"name": "🔍 SAS Title", "value": f"[Search by title]({sas_base}&search_term={quote(title)})", "inline": True})
    return rows


def notify_new(variant):
    shade_tag = f" — {variant['shade']}" if variant.get("shade") else ""
    _send({"embeds": [_embed(
        f"🆕  NEW — {variant['title']}{shade_tag}",
        variant["url"], COLOUR_NEW, _fields(variant), variant.get("image","")
    )]})
    print(f"  ✅ NEW: {variant['title'][:55]}{shade_tag}")


def notify_back(variant):
    shade_tag = f" — {variant['shade']}" if variant.get("shade") else ""
    _send({"embeds": [_embed(
        f"🟢  BACK IN STOCK — {variant['title']}{shade_tag}",
        variant["url"], COLOUR_BACK, _fields(variant), variant.get("image","")
    )]})
    print(f"  ✅ BACK IN STOCK: {variant['title'][:50]}{shade_tag}")


def notify_drop(variant, old_price, new_price, pct):
    pct_str  = f"{pct*100:.1f}%"
    abs_drop = float(old_price) - float(new_price)
    shade_tag = f" — {variant['shade']}" if variant.get("shade") else ""
    extra = [
        {"name": "💰 Was",  "value": f"£{old_price}",                          "inline": True},
        {"name": "💰 Now",  "value": f"**£{new_price}**",                       "inline": True},
        {"name": "📉 Drop", "value": f"↓ £{abs_drop:.2f} (-{pct_str})",        "inline": True},
    ] + _fields(variant)
    _send({"embeds": [_embed(
        f"📉  PRICE DROP -{pct_str} — {variant['title']}{shade_tag}",
        variant["url"], COLOUR_DROP, extra, variant.get("image","")
    )]})
    print(f"  ✅ PRICE DROP -{pct_str}: {variant['title'][:45]}{shade_tag}")


# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
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


def to_entry(variant):
    return {
        "title":      variant.get("title", ""),
        "url":        variant.get("url", ""),
        "shade":      variant.get("shade", ""),
        "price":      variant.get("price", ""),
        "ean":        variant.get("ean", ""),
        "image":      variant.get("image", ""),
        "in_stock":   variant.get("in_stock", True),
        "first_seen": variant.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# MAIN CHECK
# ---------------------------------------------------------------------------

def run_check():
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n[{now_str}] Checking Shure Cosmetics...")

    snapshot      = load_snapshot()
    known_keys    = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    # Step 1: Scrape listing page (fast — no detail page scrapes yet)
    listing_products = fetch_all_listing_products()
    if not listing_products:
        print("  [!] Nothing scraped — skipping")
        return

    # Step 2: For each listing product, scrape the detail page
    # to get per-shade options, EAN, and accurate stock/price.
    #
    # IMPORTANT: We only scrape detail pages when NEEDED:
    #   - Always for new products (not in snapshot)
    #   - For existing products ONLY if the listing shows a price change
    #     vs snapshot (quick pre-filter saves unnecessary requests)
    #   - First run: scrape everything to build baseline

    all_variants = []   # flat list of {handle_key, title, url, shade, price, ean, image, in_stock}
    alerts_sent  = 0

    for p in listing_products:
        handle = p["handle"]
        url    = p["url"]
        title  = p["title"]
        is_options = p["is_options"]

        # Determine if we need a detail page scrape
        # For options products: always scrape to get per-shade stock
        # For single products: scrape if new OR price might have changed
        existing_keys = [k for k in known_keys if k == handle or k.startswith(f"{handle}::")]
        is_new_product = len(existing_keys) == 0

        listing_price = p.get("price", "")
        snap_price    = snapshot.get(handle, {}).get("price", "") if not is_options else ""
        price_changed = listing_price and snap_price and listing_price != snap_price

        needs_scrape = (
            is_first_run or
            is_new_product or
            is_options or    # always scrape options — shade stock can change without listing change
            price_changed
        )

        if needs_scrape:
            time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
            variants = scrape_product_detail(url, is_options)
        else:
            # Use cached data — no scrape needed
            variants = [{
                "handle_key": handle,
                "shade":      "",
                "price":      listing_price,
                "ean":        snapshot.get(handle, {}).get("ean", ""),
                "image":      p.get("image", "") or snapshot.get(handle, {}).get("image", ""),
                "in_stock":   p.get("in_stock", True),
            }]

        # Enrich each variant with title + url (needed for Discord embeds)
        for v in variants:
            v["title"] = title
            v["url"]   = url
            all_variants.append(v)

    print(f"  {len(all_variants)} variants processed ({len(listing_products)} products)")

    if is_first_run:
        print(f"  First run — building baseline. No alerts will fire.")

    # Step 3: Compare each variant against snapshot
    new_snapshot = dict(snapshot)

    for variant in all_variants:
        key   = variant["handle_key"]
        old   = snapshot.get(key, {})

        # Carry forward EAN/image if not scraped this cycle
        for f in ("ean", "image"):
            if not variant.get(f):
                variant[f] = old.get(f, "")

        if is_first_run:
            entry = to_entry(variant)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            new_snapshot[key] = entry
            continue

        is_new_key    = key not in known_keys
        was_in_stock  = old.get("in_stock", True)
        now_in_stock  = variant.get("in_stock", True)
        old_price     = old.get("price", "")
        new_price     = variant.get("price", "")

        # NEW listing
        if is_new_key:
            if now_in_stock:
                notify_new(variant)
                alerts_sent += 1
                time.sleep(1.5)
            entry = to_entry(variant)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            new_snapshot[key] = entry
            continue

        # BACK IN STOCK
        if not was_in_stock and now_in_stock:
            notify_back(variant)
            alerts_sent += 1
            time.sleep(1.5)

        # PRICE DROP (>=5% AND >£0.05)
        elif now_in_stock and old_price and new_price:
            try:
                old_f = float(old_price)
                new_f = float(new_price)
                if old_f > 0:
                    pct = (old_f - new_f) / old_f
                    if pct >= 0.05 and (old_f - new_f) > 0.05:
                        notify_drop(variant, old_price, new_price, pct)
                        alerts_sent += 1
                        time.sleep(1.5)
            except ValueError:
                pass

        # Update snapshot
        entry = to_entry(variant)
        entry["first_seen"] = old.get("first_seen", entry["first_seen"])
        new_snapshot[key] = entry

    save_snapshot(new_snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline saved — {len(new_snapshot)} variants tracked.")
    else:
        print(f"  Done — {alerts_sent} alert(s) | {len(new_snapshot)} variants tracked.")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    print("=" * 55)
    print("  Shure Cosmetics Monitor")
    print(f"  {LISTING_URL}?all=1")
    print("  Alerts: new listings | back in stock | price drops")
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
