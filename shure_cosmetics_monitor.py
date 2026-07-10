"""
Shure Cosmetics Monitor
Monitors https://store.shure-cosmetics.co.uk/ (EKM platform — no public API,
so this uses HTML scraping like the wholesale-cosmetics/central-cosmetics
monitors).

There is no dedicated "New Arrivals" page on this storefront, so the
monitor crawls every top-level category using the `?all=1` query param,
which loads the entire category on a single page (confirmed working)
instead of paginating. New listings are detected purely by comparing
each run's full product set against the saved snapshot.

Detects (Discord alerts fire ONLY for these):
  - New product listings (in stock only)
  - Price drops (decreased >1% and >£0.02)
  - Restocks (stock increased meaningfully, if a quantity is shown) /
    Back in stock (was OOS — page showed "notify me" form — now in stock)

Does NOT alert on: price increases, stock decreases, going OOS.

Deps: pip install requests beautifulsoup4
"""

import json
import os
import re
import time
import random
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BASE_URL       = "https://store.shure-cosmetics.co.uk"
SNAPSHOT_FILE  = "snapshot_shure.json"
BASELINE_FLAG  = "baseline_done_shure.txt"
REQUEST_DELAY  = 2.0
RUN_ONCE       = os.getenv("RUN_ONCE", "false").lower() == "true"
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1800"))  # 30 min

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")

# Only monitor products from these brands (case-insensitive title match)
MONITORED_BRANDS = {"maybelline", "loreal", "l'oreal", "l oreal", "rimmel", "revolution"}
FNF_ROLE_ID = "1019772687528235099"
FNF_MENTION  = f"<@&{FNF_ROLE_ID}>"

def is_monitored_brand(title):
    """Return True if the product title contains one of the monitored brands."""
    title_lower = title.lower()
    return any(brand in title_lower for brand in MONITORED_BRANDS)

# Top-level category pages to crawl for full-catalogue coverage.
# Confirmed top nav: Cosmetics, Skin Care, Fragrances, Nails, Hair,
# Home Essentials, Gift Sets, Seasonal, Vegan, Sales.
# (EKM has no public products API or working "New Arrivals" page, so we
# crawl every top-level category with ?all=1, which loads the entire
# category on a single page instead of paginating.)
CATEGORY_PAGES = [
    "wholesale-cosmetics",
    "skin-care",
    "fragrance",
    "wholesale-nail",
    "hair",
    "home-care",
    "gift-sets",
    "self-tan-and-suntan",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Discord embed colours
COLOUR_NEW     = 0xE91E8C   # pink — new listing
COLOUR_RESTOCK = 0x3498DB   # blue — restock
COLOUR_BACK    = 0x9B59B6   # purple — back in stock
# Price drop colours are tiered by severity — see notify_price_change()

# ---------------------------------------------------------------------------
# HTTP HELPERS
# ---------------------------------------------------------------------------

def get_soup(url, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"  [!] Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"  [!] Fetch error ({url}): {e} — attempt {attempt+1}/{retries}")
            if attempt < retries - 1:
                time.sleep(4 * (attempt + 1))
    return None

# ---------------------------------------------------------------------------
# SCRAPING — LISTING PAGES (category + new arrivals)
# ---------------------------------------------------------------------------

def scrape_listing_page(url):
    """
    Scrape one listing/category page. Returns (products, has_next_page_url).

    Confirmed EKM theme structure for this storefront:
      <article class="item-box product-box">
        <div class="item-image">
          <a href="...">
            <img class="item-img lazy" data-src="..." />
          </a>
        </div>
        <div class="item-title-box">
          <h3 class="item-title"><a href="...">Title (code) (code)</a></h3>
          <div class="box-data grid">
            <span class="price">£X.XX</span>
            <span class="item-in-stock item-stock">In stock</span>
              (or item-out-of-stock / "Out of stock")
            <a class="wishlist-button" href="/wishlist?action=add&product_id=N">
          </div>
        </div>
      </article>
    """
    soup = get_soup(url)
    if not soup:
        return [], None

    products = []
    seen = set()

    cards = soup.find_all("article", class_=re.compile(r"\bproduct-box\b"))

    for card in cards:
        title_el = card.find("h3", class_="item-title")
        if not title_el:
            continue
        link = title_el.find("a", href=True)
        if not link:
            continue

        href = link["href"]
        path = href.replace(BASE_URL, "").strip("/")
        if not path or path in seen:
            continue
        seen.add(path)

        title = link.get_text(strip=True)

        price_el = card.find("span", class_="price")
        price = ""
        if price_el:
            price_m = re.search(r"([\d]+\.[\d]{2})", price_el.get_text())
            if price_m:
                price = price_m.group(1)

        stock_el = card.find("span", class_=re.compile(r"item-(in|out-of)-stock"))
        if stock_el:
            stock_classes = " ".join(stock_el.get("class", []))
            in_stock = "out-of-stock" not in stock_classes and "out of stock" not in stock_el.get_text(strip=True).lower()
        else:
            card_text = card.get_text(" ", strip=True)
            in_stock = "out of stock" not in card_text.lower()

        img = card.find("img")
        image = ""
        if img:
            image = img.get("data-src") or img.get("src") or ""
            if image and not image.startswith("http"):
                image = "https:" + image if image.startswith("//") else BASE_URL + image

        # Product ID from the wishlist link, if present (useful as a stable key)
        product_id = ""
        wishlist_a = card.find("a", class_=re.compile(r"wishlist-button"))
        if wishlist_a and wishlist_a.get("href"):
            pid_m = re.search(r"product_id=(\d+)", wishlist_a["href"])
            if pid_m:
                product_id = pid_m.group(1)

        products.append({
            "handle":     path,
            "product_id": product_id,
            "title":      title,
            "url":        f"{BASE_URL}/{path}",
            "price":      price,
            "in_stock":   in_stock,
            "image":      image,
        })

    # Pagination — not used when called with ?all=1, but kept as a fallback
    next_link = soup.find("a", rel="next") or soup.find("a", string=re.compile(r"Next", re.IGNORECASE))
    next_url = None
    if next_link and next_link.get("href"):
        href = next_link["href"]
        next_url = href if href.startswith("http") else BASE_URL + href

    return products, next_url


def scrape_category(handle):
    """
    Fetch one category using ?all=1, which loads the entire category
    on a single page (confirmed working) instead of paginating through
    the default page size.
    """
    url = f"{BASE_URL}/{handle}?all=1"
    products, _ = scrape_listing_page(url)
    return products


def scrape_all_categories():
    """Crawl every configured category page for full-catalogue coverage."""
    all_products = []
    seen_handles = set()
    for handle in CATEGORY_PAGES:
        print(f"  Crawling category: {handle}")
        products = scrape_category(handle)
        for p in products:
            if p["handle"] not in seen_handles:
                seen_handles.add(p["handle"])
                all_products.append(p)
        print(f"    {len(products)} products found in {handle} (running total: {len(all_products)})")
        time.sleep(REQUEST_DELAY + random.uniform(0, 1))
    return all_products

# ---------------------------------------------------------------------------
# SCRAPING — PRODUCT DETAIL PAGE
# ---------------------------------------------------------------------------

def scrape_product_detail(product):
    """
    Fetch the product page for barcode, exact stock (if shown), sale price,
    and accurate in-stock detection (EKM shows a "notify me when available"
    form for OOS products, which is a reliable signal beyond a stock badge).
    """
    url = product["url"]
    soup = get_soup(url)
    if not soup:
        return product

    text = soup.get_text(" ", strip=True)

    # Title
    h1 = soup.find("h1")
    if h1:
        product["title"] = h1.get_text(strip=True)

    # Barcode — confirmed label on this storefront is "Barcode (GTIN/EAN):"
    # in the "Further Info" / "Identification" table on the product page.
    label_m = re.search(r"Barcode\s*\(GTIN/EAN\)[:\s]+(\d{8,14})", text, re.IGNORECASE)
    if not label_m:
        # Fall back to a looser label match, then a bare 12-14 digit number
        label_m = re.search(r"(?:Barcode|GTIN|EAN)[:\s]+(\d{8,14})", text, re.IGNORECASE)
    if label_m:
        product["barcode"] = label_m.group(1)
    else:
        bc_m = re.search(r"\b(\d{12,14})\b", text)
        if bc_m:
            product["barcode"] = bc_m.group(1)

    # Price — prefer the confirmed product price element (same EKM theme
    # component used on listing pages: <span class="price">£X.XX</span>).
    # A blind regex over the whole page text was unreliable — it could
    # match an unrelated £ amount first (delivery banners, basket
    # subtotal showing "£0.00", etc.) before reaching the real price.
    # For options products EKM renders "£0.00 £2.75" — the first value
    # is the basket price placeholder, the second is the real unit price.
    # We find ALL price values in the price element and take the first
    # non-zero one, which is always the real selling price.
    price_el = soup.find("span", class_="price")
    if price_el:
        price_text = price_el.get_text(" ", strip=True)
        sale_m = re.search(r"WAS\s*£\s*([\d.]+)\s*NOW\s*£\s*([\d.]+)", price_text, re.IGNORECASE)
        if sale_m:
            product["compare_price"] = sale_m.group(1)
            product["price"] = sale_m.group(2)
        else:
            # Find all prices and use the first non-zero one
            all_prices = re.findall(r"([\d]+\.[\d]{2})", price_text)
            for p in all_prices:
                if float(p) > 0:
                    product["price"] = p
                    break
    else:
        sale_m = re.search(r"WAS\s*£\s*([\d.]+)\s*NOW\s*£\s*([\d.]+)", text, re.IGNORECASE)
        if sale_m:
            product["compare_price"] = sale_m.group(1)
            product["price"] = sale_m.group(2)
        else:
            # Find all £ amounts and use first non-zero
            all_prices = re.findall(r"£\s*([\d]+\.[\d]{2})", text)
            for p in all_prices:
                if float(p) > 0:
                    product["price"] = p
                    break

    # Sanity guard: a scraped price of exactly 0 is virtually always a
    # scraping error (no wholesale cosmetics product is free), not a
    # real price. Discard it so check_changes() falls back to the last
    # known good price from the snapshot instead.
    if product.get("price") in ("0", "0.0", "0.00"):
        product["price"] = ""

    # Exact stock count, if the theme exposes it (varies by product/theme block)
    stock_m = re.search(r"(\d+)\s+(?:in stock|available|units? available)", text, re.IGNORECASE)
    if stock_m:
        product["stock"] = int(stock_m.group(1))
    else:
        product.setdefault("stock", None)

    # In-stock detection — prefer the same stock indicator span used on
    # listing pages; fall back to the "notify me when available" signal,
    # then to plain "out of stock" text matching.
    stock_span = soup.find("span", class_=re.compile(r"item-(in|out-of)-stock"))
    if stock_span:
        stock_classes = " ".join(stock_span.get("class", []))
        product["in_stock"] = "out-of-stock" not in stock_classes
    elif "notify" in text.lower() and "available" in text.lower() and "out of stock" in text.lower():
        product["in_stock"] = False
    elif "out of stock" in text.lower():
        product["in_stock"] = False
    else:
        product["in_stock"] = True

    # Image — prefer og:image
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        product["image"] = og_img["content"]

    # Variant/shade options — products with "(Options)" in the title are
    # sold as a single wholesale assortment covering multiple shades
    # (e.g. "Options: 040 Tan, 050 Rich" — one price/stock for the whole
    # mixed pack, not separate variants with their own price/stock/URL).
    # We surface the shade list on the alert so it's visible, but don't
    # track each shade as a separate snapshot entry since there's no
    # separate price/stock data to compare per shade on this storefront.
    options_m = re.search(r"Options:\s*([^.]+?)(?:\.\s|Please note|$)", text)
    if options_m:
        shades = options_m.group(1).strip().rstrip(",")
        product["variant_options"] = shades
    else:
        product.setdefault("variant_options", "")

    return product

# ---------------------------------------------------------------------------
# PRICING HELPERS
# ---------------------------------------------------------------------------

def vat_price(price_str):
    try:
        return f"{float(price_str) * 1.2:.2f}"
    except (ValueError, TypeError):
        return price_str


def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def selleramp_url_ean(barcode, cost_price_str):
    if not barcode:
        return None
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={barcode}&sas_cost_price={vat_price(cost_price_str)}"
    )


def selleramp_url_title(title, cost_price_str):
    if not title:
        return None
    from urllib.parse import quote as _q
    return (
        f"https://sas.selleramp.com/sas/lookup/"
        f"?search_term={_q(title)}&sas_cost_price={vat_price(cost_price_str)}"
    )

# ---------------------------------------------------------------------------
# DISCORD EMBEDS
# ---------------------------------------------------------------------------

def _base_fields(product):
    barcode  = product.get("barcode", "")
    title    = product.get("title", "")
    stock    = product.get("stock")
    in_stock = product.get("in_stock", True)
    price    = product.get("price", "")
    sas_ean   = selleramp_url_ean(barcode, price)
    sas_title = selleramp_url_title(title, price)

    if stock is not None:
        stock_val = f"**{stock}** units"
    elif in_stock:
        stock_val = "✅ In stock"
    else:
        stock_val = "❌ Out of stock"

    fields = [
        {"name": "🔢 Barcode", "value": f"`{barcode}`" if barcode else "-", "inline": True},
        {"name": "📊 Stock",   "value": stock_val,                          "inline": True},
    ]

    shades = product.get("variant_options", "")
    if shades:
        display_shades = shades if len(shades) <= 300 else shades[:297] + "..."
        fields.append({"name": "🎨 Shades in this pack", "value": display_shades, "inline": False})

    if sas_title:
        fields.append({"name": "🔍 SAS Title", "value": f"[Search by title]({sas_title})", "inline": True})
    if sas_ean:
        fields.append({"name": "🔍 SAS EAN", "value": f"[Search by barcode]({sas_ean})", "inline": True})
    return fields


def _send_embed(embed, content=None):
    payload = {"embeds": [embed]}
    if content:
        payload["content"] = content
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


def _thumbnail(product):
    image = product.get("image", "")
    return {"url": image} if image else None


def _price_display(product):
    price   = product.get("price", "")
    compare = product.get("compare_price", "")
    if price in ("0", "0.0", "0.00"):
        price = ""
    if compare and compare != price:
        return f"£{compare} -> **£{price}**" if price else "-"
    return f"**£{price}**" if price else "-"


def notify_new(product):
    price = product.get("price", "")
    if price in ("0", "0.0", "0.00"):
        price = ""
    fields = [
        {"name": "💰 Price (ex. VAT)",  "value": _price_display(product),                "inline": True},
        {"name": "💷 Price (inc. VAT)", "value": f"£{vat_price(price)}" if price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🆕  NEW LISTING — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_NEW,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Shure Cosmetics Monitor • shure-cosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: NEW — {product.get('title', '')[:60]}")


def notify_price_change(product, old_price, new_price, pct_change):
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)
    diff  = f"£{abs(new_f - old_f):.2f}" if old_f and new_f else "?"
    pct_display = f"{pct_change * 100:.1f}%"

    if pct_change >= 0.20:
        colour = 0x00C853
        tier   = "🔥"
    elif pct_change >= 0.10:
        colour = 0x2ECC71
        tier   = "💰"
    else:
        colour = 0x82E0AA
        tier   = "💵"

    fields = [
        {"name": "💰 Old Price", "value": f"£{old_price}",     "inline": True},
        {"name": "💰 New Price", "value": f"**£{new_price}**", "inline": True},
        {"name": "📉 Drop",      "value": f"↓ {diff} (**{pct_display}**)", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"{tier}  PRICE DROP -{pct_display} — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     colour,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Shure Cosmetics Monitor • shure-cosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    mention = FNF_MENTION if pct_change >= 0.25 else None
    _send_embed(embed, content=mention)
    print(f"  Discord: PRICE DROP -{pct_display} — {product.get('title', '')[:50]}")


def notify_stock_change(product, old_stock, new_stock):
    """Restock only — stock decreases are no longer tracked."""
    diff = (new_stock - old_stock) if (new_stock is not None and old_stock is not None) else "?"
    fields = [
        {"name": "📊 Old Stock", "value": f"{old_stock} units",     "inline": True},
        {"name": "📊 New Stock", "value": f"**{new_stock} units**", "inline": True},
        {"name": "📈 Change",    "value": f"↑ +{diff} units" if isinstance(diff, int) else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  RESTOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_RESTOCK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Shure Cosmetics Monitor • shure-cosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: RESTOCK — {product.get('title', '')[:50]}")


def notify_back_in_stock(product):
    price = product.get("price", "")
    if price in ("0", "0.0", "0.00"):
        price = ""
    fields = [
        {"name": "💰 Price (ex. VAT)",  "value": _price_display(product),                "inline": True},
        {"name": "💷 Price (inc. VAT)", "value": f"£{vat_price(price)}" if price else "-", "inline": True},
    ] + _base_fields(product)

    embed = {
        "title":     f"🟢  BACK IN STOCK — {product.get('title', '')}",
        "url":       product.get("url", BASE_URL),
        "color":     COLOUR_BACK,
        "fields":    fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer":    {"text": "Shure Cosmetics Monitor • shure-cosmetics.co.uk"},
    }
    t = _thumbnail(product)
    if t: embed["thumbnail"] = t
    _send_embed(embed)
    print(f"  Discord: BACK IN STOCK — {product.get('title', '')[:60]}")

# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------

def load_snapshot():
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"  [!] Snapshot file is corrupted ({e}) — backing it up and starting fresh.")
            try:
                backup_name = f"{SNAPSHOT_FILE}.corrupted.{int(time.time())}"
                os.rename(SNAPSHOT_FILE, backup_name)
                print(f"  [!] Corrupted file saved as {backup_name}")
            except OSError as backup_err:
                print(f"  [!] Could not back up corrupted file: {backup_err}")
            return {}
    return {}


def save_snapshot(data):
    """Write atomically — write to a temp file then rename, so a crash
    mid-write never leaves a corrupted snapshot.json behind."""
    tmp_file = f"{SNAPSHOT_FILE}.tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_file, SNAPSHOT_FILE)


def snapshot_entry(product):
    return {
        "title":           product.get("title", ""),
        "url":             product.get("url", ""),
        "image":           product.get("image", ""),
        "barcode":         product.get("barcode", ""),
        "price":           product.get("price", ""),
        "compare_price":   product.get("compare_price", ""),
        "in_stock":        product.get("in_stock", True),
        "stock":           product.get("stock"),
        "variant_options": product.get("variant_options", ""),
        "shade_stock":     product.get("shade_stock", {}),
        "first_seen":      product.get("first_seen", datetime.now(timezone.utc).isoformat()),
        "last_updated":    datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# CHANGE DETECTION
# ---------------------------------------------------------------------------

def check_changes(product, old):
    """
    Only fires alerts for:
      - Back in stock (whole product or individual shades for options)
      - Restock (stock count increased meaningfully)
      - Price drop (>=5% AND >£0.05)
    No alerts for: price increases, stock decreases, going OOS.
    """
    old_price    = old.get("price", "")
    old_stock    = old.get("stock")
    new_stock    = product.get("stock")
    was_in_stock = old.get("in_stock", True)
    now_in_stock = product.get("in_stock", True)

    # Backfill missing fields from snapshot
    for key in ("image", "barcode", "price", "variant_options"):
        if not product.get(key):
            product[key] = old.get(key, "")

    new_price = product.get("price", "")
    old_f = safe_float(old_price)
    new_f = safe_float(new_price)

    # --- Per-shade back-in-stock for options products ---
    old_shade_stock = old.get("shade_stock", {})
    new_shade_stock = product.get("shade_stock", {})

    if old_shade_stock and new_shade_stock:
        # Find shades that were OOS and are now in stock
        newly_available = [
            shade for shade, in_stock in new_shade_stock.items()
            if in_stock and not old_shade_stock.get(shade, True)
        ]
        if newly_available:
            # Build a modified product showing only the newly available shades
            p = dict(product)
            p["variant_options"] = f"✅ NOW IN STOCK: {', '.join(newly_available)}"
            notify_back_in_stock(p)
            time.sleep(1)
    elif not was_in_stock and now_in_stock:
        # Whole-product back in stock (no per-shade data)
        notify_back_in_stock(product)
        time.sleep(1)
        return

    if old_f and new_f and old_f > 0:
        pct_change = (old_f - new_f) / old_f
        abs_change = old_f - new_f
        if pct_change >= 0.05 and abs_change > 0.05:  # 5%+ AND £0.05+ absolute
            notify_price_change(product, old_price, new_price, pct_change)
            time.sleep(1)

    if old_stock is not None and new_stock is not None and was_in_stock and now_in_stock:
        threshold = max(5, int(old_stock * 0.2))
        if new_stock > old_stock + threshold:
            notify_stock_change(product, old_stock, new_stock)
            time.sleep(1)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_check():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Checking Shure Cosmetics...")

    snapshot      = load_snapshot()
    known_handles = set(snapshot.keys())
    baseline_done = os.path.exists(BASELINE_FLAG)
    is_first_run  = not baseline_done

    print("  Crawling full catalogue across all categories...")
    all_products = scrape_all_categories()
    if not all_products:
        print("  [!] No products scraped — possible site issue")
        return

    current_handles = {p["handle"] for p in all_products}
    new_handles      = current_handles - known_handles

    if is_first_run:
        print(f"  First run — building baseline from {len(all_products)} products (no alerts)...")
    else:
        print(f"  {len(all_products)} products found, {len(new_handles)} new")

    # Filter to monitored brands only before processing
    all_products = [p for p in all_products if is_monitored_brand(p.get("title", ""))]
    print(f"  {len(all_products)} products from monitored brands (Maybelline, L'Oréal, Rimmel, Revolution)")

    for i, product in enumerate(all_products, 1):
        handle = product["handle"]

        # Enrich with detail page scrape for: baseline (in-stock only),
        # new listings, and every existing product (to catch price/stock
        # changes and reliable back-in-stock detection via the notify form)
        should_scrape = (
            (is_first_run and product.get("in_stock")) or
            (handle in new_handles and product.get("in_stock")) or
            (not is_first_run and handle not in new_handles)
        )
        if should_scrape:
            time.sleep(REQUEST_DELAY + random.uniform(0, 1))
            product = scrape_product_detail(product)

        if is_first_run:
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[handle] = entry
        elif handle in new_handles:
            if product.get("in_stock", True):
                print(f"  -> NEW: {product['title'][:60]}")
                notify_new(product)
                time.sleep(1.5)
            entry = snapshot_entry(product)
            entry["first_seen"] = datetime.now(timezone.utc).isoformat()
            snapshot[handle] = entry
        else:
            old = snapshot[handle]
            check_changes(product, old)
            entry = snapshot_entry(product)
            entry["first_seen"] = old.get("first_seen", entry["first_seen"])
            snapshot[handle] = entry

        if i % 50 == 0:
            save_snapshot(snapshot)
            print(f"  Auto-saved at {i}/{len(all_products)}")

    save_snapshot(snapshot)

    if is_first_run:
        with open(BASELINE_FLAG, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        print(f"  Baseline complete — {len(snapshot)} products recorded. No alerts sent.")
    else:
        print(f"  Snapshot saved ({len(snapshot)} products tracked)")


def main():
    print("=" * 55)
    print("  Shure Cosmetics Monitor — Maybelline | L'Oréal | Rimmel | Revolution")
    print(f"  Watching: {BASE_URL}")
    print("  Tracking: new listings, price drops, restocks")
    print("=" * 55)

    if RUN_ONCE:
        run_check()
    else:
        while True:
            try:
                run_check()
            except Exception as e:
                print(f"  [!] Unexpected error: {e}")
            print(f"  Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
