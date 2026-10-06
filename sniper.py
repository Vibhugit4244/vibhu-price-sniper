import os
import re
import json
import html
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# SETTINGS
# ============================================================

MAX_PRICE = 5000
MAX_AVERAGE_RATIO = 0.50

TARGET_PRODUCTS = 1000
WORKERS = 12

REQUEST_TIMEOUT = 12
LISTING_TIMEOUT = 15

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"


# ============================================================
# PUBLIC DEAL / HISTORY SOURCES
# ============================================================

SOURCE_PAGES = [
    "https://pricehistoryapp.com/deals",
    "https://pricehistoryapp.com/latest-deals",
    "https://pricehistoryapp.com/deals/store/flipkart",
    "https://pricehistoryapp.com/deals/store/myntra",

    "https://pricedropy.com/",
    "https://pricedropy.com/flipkart-price-history",
    "https://pricedropy.com/myntra-price-history",

    "https://pricehistorytracker.in/",
    "https://pricehistorytracker.in/latest-deals",
]


# ============================================================
# HTTP
# ============================================================

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
})


def fetch(url, timeout=REQUEST_TIMEOUT):
    try:
        r = SESSION.get(
            url,
            timeout=timeout,
            allow_redirects=True
        )

        if r.status_code != 200:
            return None

        return r.text

    except Exception:
        return None


# ============================================================
# URL HELPERS
# ============================================================

def clean_url(url):
    if not url:
        return None

    url = html.unescape(str(url)).strip()

    if url.startswith("//"):
        url = "https:" + url

    if not url.startswith("http"):
        return None

    parsed = urlparse(url)

    return parsed._replace(
        query="",
        fragment=""
    ).geturl().rstrip("/")


def is_flipkart(url):
    return "flipkart.com" in urlparse(url).netloc.lower()


def is_myntra(url):
    return "myntra.com" in urlparse(url).netloc.lower()


def retailer(url):
    if is_flipkart(url):
        return "Flipkart"

    if is_myntra(url):
        return "Myntra"

    return None


# ============================================================
# RETAILER PRODUCT DETECTION
# ============================================================

def is_retailer_product(url):

    r = retailer(url)

    if not r:
        return False

    path = urlparse(url).path.lower()

    if r == "Flipkart":
        return (
            "/p/" in path
            or "/product/" in path
            or "/item/" in path
        )

    if r == "Myntra":
        return (
            "/buy" in path
            or "/product/" in path
            or bool(re.search(r"-\d{5,}$", path))
        )

    return False


# ============================================================
# TRACKER PRODUCT PAGE DETECTION
# ============================================================

def is_tracker_page(url):

    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()

    if "pricehistoryapp.com" in host:
        return (
            "/product/" in path
            or "/price-history/" in path
            or "/flipkart/" in path
            or "/myntra/" in path
            or "/track/" in path
        )

    if "pricedropy.com" in host:
        return (
            "/product/" in path
            or "/price-history/" in path
            or "/flipkart/" in path
            or "/myntra/" in path
            or "/track/" in path
        )

    if "pricehistorytracker.in" in host:
        return (
            "/product/" in path
            or "/price-history/" in path
            or "/flipkart/" in path
            or "/myntra/" in path
            or "/track/" in path
            or "/tracker/" in path
        )

    return False


# ============================================================
# LINK EXTRACTION
# ============================================================

def extract_links(base_url, source_html):

    soup = BeautifulSoup(source_html, "html.parser")

    links = set()

    for a in soup.find_all("a", href=True):

        href = a.get("href")

        if not href:
            continue

        full = clean_url(urljoin(base_url, href))

        if not full:
            continue

        if is_retailer_product(full) or is_tracker_page(full):
            links.add(full)

    # Also search raw HTML.
    patterns = [
        r'https?://[^"\'>\s]+',
        r'["\'](/[^"\']+)["\']'
    ]

    for pattern in patterns:

        for match in re.findall(pattern, source_html):

            if match.startswith("/"):
                full = clean_url(urljoin(base_url, match))
            else:
                full = clean_url(match)

            if not full:
                continue

            if is_retailer_product(full) or is_tracker_page(full):
                links.add(full)

    return links


# ============================================================
# DISCOVERY
# ============================================================

def discover():

    tracker_pages = set()
    retailer_pages = set()

    print()
    print("==========================================")
    print("DISCOVERY")
    print("==========================================")

    for source in SOURCE_PAGES:

        print()
        print("Source:", source)

        page = fetch(
            source,
            timeout=LISTING_TIMEOUT
        )

        if not page:
            print("Could not read source")
            continue

        links = extract_links(
            source,
            page
        )

        before_tracker = len(tracker_pages)
        before_retailer = len(retailer_pages)

        for link in links:

            if is_tracker_page(link):
                tracker_pages.add(link)

            elif is_retailer_product(link):
                retailer_pages.add(link)

        print(
            "New tracker pages:",
            len(tracker_pages) - before_tracker
        )

        print(
            "New retailer pages:",
            len(retailer_pages) - before_retailer
        )

        print(
            "Total tracker pages:",
            len(tracker_pages)
        )

        print(
            "Total retailer pages:",
            len(retailer_pages)
        )

        if len(tracker_pages) >= TARGET_PRODUCTS:
            break

    print()
    print("Discovery complete")
    print("Tracker pages:", len(tracker_pages))
    print("Retailer pages:", len(retailer_pages))

    return list(tracker_pages), list(retailer_pages)


# ============================================================
# MONEY
# ============================================================

def money(value):

    if value is None:
        return None

    value = str(value)

    match = re.search(
        r"(?:₹|Rs\.?|INR)?\s*([\d,]+(?:\.\d+)?)",
        value,
        re.I
    )

    if not match:
        return None

    try:
        return float(
            match.group(1).replace(",", "")
        )
    except Exception:
        return None


# ============================================================
# JSON-LD
# ============================================================

def json_ld(soup):

    result = []

    for script in soup.find_all(
        "script",
        attrs={"type": re.compile("ld\\+json", re.I)}
    ):

        try:
            data = json.loads(
                script.string or script.get_text()
            )

            if isinstance(data, list):
                result.extend(data)
            else:
                result.append(data)

        except Exception:
            pass

    return result


# ============================================================
# NUMBER EXTRACTION
# ============================================================

def all_prices(text):

    values = []

    patterns = [
        r"₹\s*([\d,]+(?:\.\d+)?)",
        r"Rs\.?\s*([\d,]+(?:\.\d+)?)",
        r"INR\s*([\d,]+(?:\.\d+)?)",
    ]

    for pattern in patterns:

        for x in re.findall(
            pattern,
            text,
            re.I
        ):

            v = money(x)

            if v is not None and 1 <= v <= 1000000:
                values.append(v)

    return values


# ============================================================
# FIND LABELLED PRICE
# ============================================================

def labelled_price(text, labels):

    lower = text.lower()

    for label in labels:

        pattern = (
            re.escape(label)
            + r".{0,120}?"
            r"(?:₹|rs\.?|inr)?\s*"
            r"([\d,]+(?:\.\d+)?)"
        )

        m = re.search(
            pattern,
            lower,
            re.I
        )

        if m:

            value = money(m.group(1))

            if value is not None:
                return value

    return None


# ============================================================
# PARSE TRACKER PAGE
# ============================================================

def parse_tracker(url):

    page = fetch(url)

    if not page:
        return None

    soup = BeautifulSoup(
        page,
        "html.parser"
    )

    text = soup.get_text(
        " ",
        strip=True
    )

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    title = None

    og = soup.find(
        "meta",
        attrs={"property": "og:title"}
    )

    if og and og.get("content"):
        title = og["content"].strip()

    if not title:

        tag = soup.find("title")

        if tag:
            title = tag.get_text(
                " ",
                strip=True
            )

    # --------------------------------------------------------
    # DIRECT RETAILER LINK
    # --------------------------------------------------------

    buy_url = None

    for a in soup.find_all(
        "a",
        href=True
    ):

        href = a.get("href")

        full = clean_url(
            urljoin(url, href)
        )

        if not full:
            continue

        if is_retailer_product(full):

            buy_url = full
            break

    # Search raw HTML too.
    if not buy_url:

        for match in re.findall(
            r'https?://[^"\'>\s]+',
            page
        ):

            full = clean_url(match)

            if full and is_retailer_product(full):

                buy_url = full
                break

    # --------------------------------------------------------
    # RETAILER
    # --------------------------------------------------------

    store = retailer(
        buy_url or ""
    )

    if store not in ("Flipkart", "Myntra"):
        return None

    # --------------------------------------------------------
    # CURRENT PRICE
    # --------------------------------------------------------

    current = labelled_price(
        text,
        [
            "current price",
            "current",
            "selling price",
            "sale price",
            "today's price",
            "price today",
            "live price",
        ]
    )

    # JSON-LD price
    if current is None:

        for obj in json_ld(soup):

            if not isinstance(obj, dict):
                continue

            offers = obj.get("offers")

            if isinstance(offers, dict):

                if offers.get("price"):
                    current = money(
                        offers["price"]
                    )

            if current is not None:
                break

    # --------------------------------------------------------
    # HISTORICAL AVERAGE
    # --------------------------------------------------------

    average = labelled_price(
        text,
        [
            "30d average",
            "30 day average",
            "30-day average",
            "90d average",
            "90 day average",
            "90-day average",
            "historical average",
            "average price",
            "typical price",
            "typical selling price",
        ]
    )

    # Same search in raw HTML.
    if average is None:

        average = labelled_price(
            page,
            [
                "30d average",
                "30 day average",
                "30-day average",
                "90d average",
                "90 day average",
                "90-day average",
                "historical average",
                "average price",
                "typical price",
                "typical selling price",
            ]
        )

    # --------------------------------------------------------
    # FALLBACK:
    #
    # Some public deal pages expose:
    #
    # current price + comparison/history price
    #
    # If we have no labelled average, DON'T blindly assume
    # MRP is historical average.
    # --------------------------------------------------------

    if current is None or average is None:
        return None

    if current <= 0 or average <= 0:
        return None

    qualifies = (
        current <= MAX_PRICE
        and current <= average * MAX_AVERAGE_RATIO
    )

    return {
        "title": title or "Unknown product",
        "current": current,
        "average": average,
        "store": store,
        "tracker_url": url,
        "buy_url": buy_url,
        "qualifies": qualifies,
    }


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(deal):

    if not TELEGRAM_BOT_TOKEN:
        return False

    if not TELEGRAM_CHAT_ID:
        return False

    current = deal["current"]
    average = deal["average"]

    discount = round(
        (1 - current / average) * 100,
        1
    )

    message = (
        "🔥 <b>VIBHU PRICE SNIPER</b>\n\n"
        f"<b>{html.escape(deal['title'])}</b>\n\n"
        f"🏪 {deal['store']}\n"
        f"💰 Current: ₹{current:,.0f}\n"
        f"📊 Historical average: ₹{average:,.0f}\n"
        f"📉 Below average: {discount}%\n\n"
        "✅ Current ≤ ₹5,000\n"
        "✅ Current ≤ 50% of historical average"
    )

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "🛒 BUY NOW",
                    "url": deal["buy_url"]
                }
            ]
        ]
    }

    endpoint = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:

        r = SESSION.post(
            endpoint,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(
                    keyboard
                ),
                "disable_web_page_preview": False,
            },
            timeout=15
        )

        return r.ok

    except Exception:
        return False


# ============================================================
# STATE
# ============================================================

def load_state():

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

            if isinstance(data, dict):
                return data

    except Exception:
        pass

    return {"sent": []}


def save_state(state):

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

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

    started = time.time()

    if not TELEGRAM_BOT_TOKEN:
        print("Telegram bot token secret missing.")
        return

    if not TELEGRAM_CHAT_ID:
        print("Telegram chat ID secret missing.")
        return

    tracker_pages, retailer_pages = discover()

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Historical-price checking is performed on TRACKER pages.
    # Direct retailer pages are retained only as a fallback
    # discovery pool.
    # --------------------------------------------------------

    candidates = tracker_pages[:TARGET_PRODUCTS]

    print()
    print("==========================================")
    print("PRICE HISTORY CHECK")
    print("==========================================")

    print(
        "Tracker pages to check:",
        len(candidates)
    )

    results = []

    checked = 0
    qualified = 0

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {
            executor.submit(
                parse_tracker,
                url
            ): url
            for url in candidates
        }

        for future in as_completed(futures):

            checked += 1

            try:
                result = future.result()
            except Exception:
                result = None

            if result:

                results.append(result)

                if result["qualifies"]:
                    qualified += 1

            if (
                checked % 25 == 0
                or checked == len(candidates)
            ):

                print(
                    f"Checked {checked}/"
                    f"{len(candidates)} | "
                    f"Valid history: {len(results)} | "
                    f"Qualified: {qualified}"
                )

    # --------------------------------------------------------
    # ALERTS
    # --------------------------------------------------------

    state = load_state()

    sent = set(
        state.get(
            "sent",
            []
        )
    )

    alerts = 0

    for deal in results:

        if not deal["qualifies"]:
            continue

        key = deal["buy_url"]

        if not key:
            continue

        if key in sent:
            continue

        if send_telegram(deal):

            sent.add(key)
            alerts += 1

    state["sent"] = list(sent)[-5000:]

    save_state(state)

    elapsed = round(
        time.time() - started,
        1
    )

    print()
    print("==========================================")
    print("SCAN COMPLETE")
    print("==========================================")
    print(
        "Tracker products discovered:",
        len(tracker_pages)
    )
    print(
        "Direct retailer products discovered:",
        len(retailer_pages)
    )
    print(
        "Tracker products checked:",
        checked
    )
    print(
        "Valid history records:",
        len(results)
    )
    print(
        "Qualified deals:",
        qualified
    )
    print(
        "New Telegram alerts:",
        alerts
    )
    print(
        "Time:",
        elapsed,
        "seconds"
    )
    print("==========================================")


if __name__ == "__main__":
    main()