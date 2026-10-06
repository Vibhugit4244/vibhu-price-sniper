import os
import re
import json
import time
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup


# ============================================================
# SETTINGS
# ============================================================

MAX_PRICE = 5000
DISCOUNT_FROM_AVERAGE = 0.50

# We aim to inspect up to this many UNIQUE product pages.
TARGET_PRODUCTS = 1000

# Number of product pages checked at the same time.
WORKERS = 12

# Never wait too long for one website request.
REQUEST_TIMEOUT = 12

# Small pause between listing-page requests.
LISTING_DELAY = 0.5

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"


# ============================================================
# HTTP
# ============================================================

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Connection": "keep-alive",
})


# ============================================================
# SOURCES
#
# These are real public listing/deal pages.
# We do NOT blindly generate thousands of page numbers.
# ============================================================

SOURCE_PAGES = [
    # PriceHistoryApp
    "https://pricehistoryapp.com/deals",
    "https://pricehistoryapp.com/deals/store/flipkart",
    "https://pricehistoryapp.com/deals/store/myntra",

    # PriceDropy
    "https://pricedropy.com/",
    "https://pricedropy.com/flipkart-price-history",
    "https://pricedropy.com/myntra-price-history",

    # PriceTrail / PriceHistoryTracker
    "https://pricehistorytracker.in/",
    "https://pricehistorytracker.in/latest-deals",
]


# ============================================================
# HELPERS
# ============================================================

def clean_url(url):
    if not url:
        return None

    url = html.unescape(url).strip()

    if url.startswith("//"):
        url = "https:" + url

    if not url.startswith("http"):
        return None

    parsed = urlparse(url)

    # Remove tracking/query parameters.
    clean = parsed._replace(query="", fragment="").geturl()

    return clean.rstrip("/")


def domain(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def is_flipkart(url):
    return "flipkart.com" in domain(url)


def is_myntra(url):
    return "myntra.com" in domain(url)


def retailer_from_url(url):
    if is_flipkart(url):
        return "Flipkart"

    if is_myntra(url):
        return "Myntra"

    return None


def money(value):
    if value is None:
        return None

    value = str(value)

    # Handle ₹1,499 / Rs 1499 / 1499
    m = re.search(r"(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)", value, re.I)

    if not m:
        return None

    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def get(url):
    try:
        response = SESSION.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

        if response.status_code != 200:
            return None

        return response.text

    except Exception:
        return None


# ============================================================
# DISCOVERY
# ============================================================

def looks_like_retailer_product(url):
    """
    We only accept URLs which point directly to Flipkart or Myntra.

    This prevents us from accidentally treating tracker pages,
    category pages, articles, etc. as products.
    """

    if not (is_flipkart(url) or is_myntra(url)):
        return False

    path = urlparse(url).path.lower()

    if is_flipkart(url):
        # Typical Flipkart product URLs contain /p/
        if "/p/" in path:
            return True

        # Some Flipkart links can use product/item style URLs.
        if "/product/" in path or "/item/" in path:
            return True

    if is_myntra(url):
        # Myntra product URLs normally contain /buy
        # or a product path ending in a numeric product ID.
        if "/buy" in path:
            return True

        if re.search(r"-\d{5,}$", path):
            return True

        if "/product/" in path:
            return True

    return False


def extract_links(page_url, page_html):
    """
    Extract direct Flipkart/Myntra links from a listing page.
    """

    found = set()

    soup = BeautifulSoup(page_html, "html.parser")

    # Normal <a href="">
    for a in soup.find_all("a", href=True):
        href = a.get("href")

        if not href:
            continue

        full = urljoin(page_url, href)
        full = clean_url(full)

        if full and looks_like_retailer_product(full):
            found.add(full)

    # Also inspect raw HTML because some sites put links
    # inside JSON / JavaScript.
    patterns = [
        r'https?://(?:www\.)?flipkart\.com/[^\s"\'<>\\]+',
        r'https?://(?:www\.)?myntra\.com/[^\s"\'<>\\]+',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, page_html, re.I):
            full = clean_url(match)

            if full and looks_like_retailer_product(full):
                found.add(full)

    return found


def discover_products():
    all_products = set()

    print("===================================================")
    print("DISCOVERY STARTED")
    print("===================================================")

    for source in SOURCE_PAGES:

        if len(all_products) >= TARGET_PRODUCTS:
            break

        print(f"\nSource: {source}")

        page_html = get(source)

        if not page_html:
            print("  Could not read source")
            continue

        links = extract_links(source, page_html)

        before = len(all_products)

        for link in links:
            all_products.add(link)

            if len(all_products) >= TARGET_PRODUCTS:
                break

        added = len(all_products) - before

        print(f"  New retailer products: {added}")
        print(f"  Total unique products: {len(all_products)}")

        time.sleep(LISTING_DELAY)

    print("\n===================================================")
    print(f"DISCOVERY COMPLETE: {len(all_products)} products")
    print("===================================================")

    return list(all_products)[:TARGET_PRODUCTS]


# ============================================================
# PRODUCT PARSING
# ============================================================

def json_ld_objects(soup):
    objects = []

    for script in soup.find_all(
        "script",
        attrs={"type": re.compile(r"ld\+json", re.I)}
    ):
        try:
            data = json.loads(script.string or script.get_text())

            if isinstance(data, list):
                objects.extend(data)
            else:
                objects.append(data)

        except Exception:
            pass

    return objects


def find_prices(text):
    """
    Extract possible ₹ prices from visible page text.
    """

    prices = []

    for m in re.findall(
        r"(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)",
        text,
        re.I
    ):
        value = money(m)

        if value is not None and 1 <= value <= 1000000:
            prices.append(value)

    return prices


def parse_product(url):
    page_html = get(url)

    if not page_html:
        return None

    soup = BeautifulSoup(page_html, "html.parser")

    text = soup.get_text(" ", strip=True)

    # --------------------------------------------------------
    # Product name
    # --------------------------------------------------------

    title = None

    og_title = soup.find(
        "meta",
        attrs={"property": "og:title"}
    )

    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    if not title:
        title_tag = soup.find("title")

        if title_tag:
            title = title_tag.get_text(" ", strip=True)

    # --------------------------------------------------------
    # JSON-LD structured data
    # --------------------------------------------------------

    current_price = None

    for obj in json_ld_objects(soup):

        if not isinstance(obj, dict):
            continue

        offers = obj.get("offers")

        if isinstance(offers, dict):
            price = offers.get("price")

            if price:
                current_price = money(price)

        elif isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict) and offer.get("price"):
                    current_price = money(offer["price"])
                    break

        if not title and obj.get("name"):
            title = str(obj["name"]).strip()

    # --------------------------------------------------------
    # Visible prices
    # --------------------------------------------------------

    prices = find_prices(text)

    # Use the lowest sensible price as current price if
    # structured data did not provide it.
    if current_price is None and prices:
        sensible = [p for p in prices if p <= 100000]
        if sensible:
            current_price = min(sensible)

    if current_price is None:
        return None

    # --------------------------------------------------------
    # Historical average
    #
    # Tracker pages may expose average values in text.
    # Look specifically for common average labels first.
    # --------------------------------------------------------

    average = None

    average_patterns = [
        r"30\s*day\s*average.{0,80}?(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)",
        r"30d\s*average.{0,80}?(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)",
        r"90\s*day\s*average.{0,80}?(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)",
        r"90d\s*average.{0,80}?(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)",
        r"historical\s*average.{0,80}?(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)",
        r"average\s*(?:selling\s*)?price.{0,80}?(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)",
    ]

    lower = text.lower()

    for pattern in average_patterns:
        match = re.search(pattern, lower, re.I)

        if match:
            average = money(match.group(1))
            break

    # --------------------------------------------------------
    # If tracker source has an average in embedded text,
    # inspect the raw HTML too.
    # --------------------------------------------------------

    if average is None:
        for pattern in average_patterns:
            match = re.search(pattern, page_html, re.I)

            if match:
                average = money(match.group(1))
                break

    if average is None or average <= 0:
        return None

    retailer = retailer_from_url(url)

    if retailer not in ("Flipkart", "Myntra"):
        return None

    # --------------------------------------------------------
    # Deal calculation
    # --------------------------------------------------------

    qualifies = (
        current_price <= MAX_PRICE
        and current_price <= average * DISCOUNT_FROM_AVERAGE
    )

    return {
        "title": title or "Unknown product",
        "current": current_price,
        "average": average,
        "retailer": retailer,
        "url": url,
        "qualifies": qualifies,
    }


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message, buy_url):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets are missing.")
        return False

    api = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "🛒 BUY NOW",
                    "url": buy_url
                }
            ]
        ]
    }

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard),
        "disable_web_page_preview": False,
    }

    try:
        r = SESSION.post(
            api,
            data=payload,
            timeout=15
        )

        return r.ok

    except Exception as e:
        print("Telegram error:", e)
        return False


# ============================================================
# STATE
# ============================================================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception:
        pass

    return {"sent": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            indent=2,
            ensure_ascii=False
        )


# ============================================================
# MAIN
# ============================================================

def main():

    start = time.time()

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Telegram secrets are not configured.")
        return

    products = discover_products()

    if not products:
        print("No direct Flipkart/Myntra products discovered.")
        return

    print(f"\nChecking {len(products)} product pages concurrently...")
    print(f"Workers: {WORKERS}")

    state = load_state()

    sent = set(state.get("sent", []))

    checked = 0
    qualified = 0
    alerts = 0

    results = []

    # --------------------------------------------------------
    # Concurrent product checking
    # --------------------------------------------------------

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:

        futures = {
            executor.submit(parse_product, url): url
            for url in products
        }

        for future in as_completed(futures):

            url = futures[future]

            try:
                result = future.result()
            except Exception:
                result = None

            checked += 1

            if result:
                results.append(result)

                if result["qualifies"]:
                    qualified += 1

            if checked % 50 == 0 or checked == len(products):
                print(
                    f"Checked {checked}/{len(products)} | "
                    f"Qualified {qualified}"
                )

    # --------------------------------------------------------
    # Send alerts
    # --------------------------------------------------------

    for deal in results:

        if not deal["qualifies"]:
            continue

        # URL itself is the unique alert key.
        key = deal["url"]

        if key in sent:
            continue

        current = deal["current"]
        average = deal["average"]

        discount = 0

        if average > 0:
            discount = round(
                (1 - current / average) * 100,
                1
            )

        message = (
            "🔥 <b>VIBHU PRICE SNIPER DEAL</b>\n\n"
            f"<b>{html.escape(deal['title'])}</b>\n\n"
            f"🏪 {deal['retailer']}\n"
            f"💰 Current: ₹{current:,.0f}\n"
            f"📊 Historical average: ₹{average:,.0f}\n"
            f"📉 Below average: {discount}%\n\n"
            "✅ Current price ≤ ₹5,000\n"
            "✅ Current price ≤ 50% of historical average"
        )

        if telegram_send(message, deal["url"]):
            alerts += 1
            sent.add(key)

    # Keep state reasonably small.
    state["sent"] = list(sent)[-5000:]

    save_state(state)

    elapsed = round(time.time() - start, 1)

    print("\n===================================================")
    print("SCAN COMPLETE")
    print("===================================================")
    print(f"Products discovered : {len(products)}")
    print(f"Products checked    : {checked}")
    print(f"Qualified deals     : {qualified}")
    print(f"New Telegram alerts : {alerts}")
    print(f"Time taken          : {elapsed} seconds")
    print("===================================================")


if __name__ == "__main__":
    main()