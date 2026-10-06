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

# Product checking concurrency
WORKERS = 10

REQUEST_TIMEOUT = 12
RETAILER_TIMEOUT = 10

# Extremely cheap prices need extra validation.
# We do NOT automatically reject cheap products because
# genuine ₹49/₹99/₹199 products exist.
SUSPICIOUS_PRICE = 100


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = "state.json"


# ============================================================
# SOURCES
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
# HTTP SESSION
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
        response = SESSION.get(
            url,
            timeout=timeout,
            allow_redirects=True
        )

        if response.status_code != 200:
            return None

        return response.text

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


def retailer(url):

    host = urlparse(url).netloc.lower()

    if "flipkart.com" in host:
        return "Flipkart"

    if "myntra.com" in host:
        return "Myntra"

    return None


def is_retailer_product(url):

    store = retailer(url)

    if not store:
        return False

    path = urlparse(url).path.lower()

    if store == "Flipkart":
        return (
            "/p/" in path
            or "/product/" in path
            or "/item/" in path
        )

    if store == "Myntra":
        return (
            "/buy" in path
            or "/product/" in path
            or bool(re.search(r"-\d{5,}$", path))
        )

    return False


def is_tracker_page(url):

    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()

    if "pricehistoryapp.com" in host:
        return (
            "/product/" in path
            or "/price-history/" in path
            or "/track/" in path
        )

    if "pricedropy.com" in host:
        return (
            "/product/" in path
            or "/price-history/" in path
            or "/track/" in path
        )

    if "pricehistorytracker.in" in host:
        return (
            "/product/" in path
            or "/price-history/" in path
            or "/track/" in path
            or "/tracker/" in path
        )

    return False


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


def price_from_string(value):

    if value is None:
        return None

    try:
        number = float(
            str(value)
            .replace(",", "")
            .replace("₹", "")
            .strip()
        )

        if 0 < number <= 10000000:
            return number

    except Exception:
        pass

    return money(value)


# ============================================================
# JSON-LD
# ============================================================

def get_json_ld(soup):

    objects = []

    for script in soup.find_all(
        "script",
        attrs={"type": re.compile(r"ld\+json", re.I)}
    ):

        try:

            data = json.loads(
                script.string or script.get_text()
            )

            if isinstance(data, list):
                objects.extend(data)
            else:
                objects.append(data)

        except Exception:
            pass

    return objects


# ============================================================
# EXTRACT STRUCTURED CURRENT PRICE
# ============================================================

def structured_current_price(soup):

    # --------------------------------------------------------
    # JSON-LD Product / Offer
    # --------------------------------------------------------

    for obj in get_json_ld(soup):

        if not isinstance(obj, dict):
            continue

        offers = obj.get("offers")

        if isinstance(offers, dict):

            for key in (
                "price",
                "lowPrice"
            ):

                if offers.get(key) is not None:

                    value = price_from_string(
                        offers.get(key)
                    )

                    if value:
                        return value

        elif isinstance(offers, list):

            for offer in offers:

                if not isinstance(offer, dict):
                    continue

                if offer.get("price") is not None:

                    value = price_from_string(
                        offer.get("price")
                    )

                    if value:
                        return value

    # --------------------------------------------------------
    # Common meta price fields
    # --------------------------------------------------------

    meta_patterns = [
        {
            "property": "product:price:amount"
        },
        {
            "property": "og:price:amount"
        },
        {
            "name": "price"
        },
        {
            "name": "product:price"
        },
    ]

    for attrs in meta_patterns:

        tag = soup.find(
            "meta",
            attrs=attrs
        )

        if tag and tag.get("content"):

            value = price_from_string(
                tag.get("content")
            )

            if value:
                return value

    # --------------------------------------------------------
    # Itemprop price
    # --------------------------------------------------------

    tag = soup.find(
        attrs={"itemprop": "price"}
    )

    if tag:

        value = price_from_string(
            tag.get("content")
            or tag.get_text(" ", strip=True)
        )

        if value:
            return value

    return None


# ============================================================
# EXTRACT ALL CURRENCY PRICES
# ============================================================

def all_currency_prices(text):

    prices = []

    patterns = [
        r"₹\s*([\d,]+(?:\.\d+)?)",
        r"Rs\.?\s*([\d,]+(?:\.\d+)?)",
        r"INR\s*([\d,]+(?:\.\d+)?)",
    ]

    for pattern in patterns:

        for value in re.findall(
            pattern,
            text,
            re.I
        ):

            number = money(value)

            if number is not None:
                prices.append(number)

    return prices


# ============================================================
# TITLE
# ============================================================

def extract_title(soup):

    tag = soup.find(
        "meta",
        attrs={"property": "og:title"}
    )

    if tag and tag.get("content"):
        return tag["content"].strip()

    tag = soup.find("title")

    if tag:
        return tag.get_text(
            " ",
            strip=True
        )

    return "Unknown product"


# ============================================================
# HISTORICAL AVERAGE
# ============================================================

def extract_average(soup, raw_html):

    text = soup.get_text(
        " ",
        strip=True
    )

    # More specific labels first.
    labels = [
        "30d average",
        "30 day average",
        "30-day average",
        "90d average",
        "90 day average",
        "90-day average",
        "historical average",
        "average selling price",
        "average price",
        "typical selling price",
        "typical price",
    ]

    for source in (text, raw_html):

        lower = source.lower()

        for label in labels:

            pattern = (
                re.escape(label)
                + r".{0,100}?"
                r"(?:₹|rs\.?|inr)?\s*"
                r"([\d,]+(?:\.\d+)?)"
            )

            match = re.search(
                pattern,
                lower,
                re.I
            )

            if match:

                value = money(
                    match.group(1)
                )

                if value and value > 0:
                    return value

    return None


# ============================================================
# FIND DIRECT RETAILER URL
# ============================================================

def find_buy_url(base_url, soup, raw_html):

    # First inspect actual anchors.
    for a in soup.find_all(
        "a",
        href=True
    ):

        href = a.get("href")

        full = clean_url(
            urljoin(base_url, href)
        )

        if full and is_retailer_product(full):
            return full

    # Then inspect raw HTML.
    patterns = [
        r'https?://[^"\'>\s]+flipkart\.com[^"\'>\s]*',
        r'https?://[^"\'>\s]+myntra\.com[^"\'>\s]*',
    ]

    for pattern in patterns:

        for match in re.findall(
            pattern,
            raw_html,
            re.I
        ):

            full = clean_url(match)

            if full and is_retailer_product(full):
                return full

    return None


# ============================================================
# RETAILER PAGE PRICE EXTRACTION
# ============================================================

def extract_retailer_price(soup):

    # 1. Structured data is the strongest signal.
    value = structured_current_price(soup)

    if value is not None:
        return value

    # 2. Explicit itemprop.
    itemprop = soup.find(
        attrs={"itemprop": "price"}
    )

    if itemprop:

        value = price_from_string(
            itemprop.get("content")
            or itemprop.get_text(" ", strip=True)
        )

        if value:
            return value

    # 3. Meta fields.
    for attrs in [
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
    ]:

        tag = soup.find(
            "meta",
            attrs=attrs
        )

        if tag:

            value = price_from_string(
                tag.get("content")
            )

            if value:
                return value

    return None


# ============================================================
# IMPORTANT PRICE VALIDATION
# ============================================================

def validate_current_price(
    tracker_price,
    retailer_price,
    average,
    title
):

    if tracker_price is None:
        return None, "No tracker current price"

    if tracker_price <= 0:
        return None, "Invalid tracker price"

    if average is None or average <= 0:
        return None, "No historical average"

    # --------------------------------------------------------
    # Basic sanity.
    # --------------------------------------------------------

    if tracker_price > MAX_PRICE:
        return tracker_price, "Above price limit"

    # --------------------------------------------------------
    # If the actual retailer page gives us a price,
    # prefer that over an ambiguous tracker number.
    # --------------------------------------------------------

    if retailer_price is not None:

        # If both agree closely, excellent.
        difference = abs(
            retailer_price - tracker_price
        ) / max(retailer_price, 1)

        if difference <= 0.15:
            return retailer_price, "Retailer + tracker agree"

        # If tracker says something absurdly cheap,
        # but retailer says ₹1,000+, DO NOT use tracker price.
        if tracker_price < SUSPICIOUS_PRICE:

            return None, (
                "Rejected suspicious tracker price: "
                f"₹{tracker_price:.0f} vs retailer "
                f"₹{retailer_price:.0f}"
            )

        # If tracker and retailer disagree substantially,
        # use retailer price as the safer current price.
        return retailer_price, "Retailer price used"

    # --------------------------------------------------------
    # NO RETAILER PRICE AVAILABLE
    # --------------------------------------------------------

    # Very low prices require independent confirmation.
    # This specifically prevents false ₹25 / ₹49 / ₹99 alerts.
    if tracker_price < SUSPICIOUS_PRICE:

        return None, (
            "Rejected suspiciously low price "
            f"₹{tracker_price:.0f} without retailer confirmation"
        )

    # --------------------------------------------------------
    # Historical-average sanity check.
    #
    # If a price is 99% below history, demand stronger
    # validation. We don't want parser errors becoming alerts.
    # --------------------------------------------------------

    ratio = tracker_price / average

    if ratio < 0.05:

        if retailer_price is None:
            return None, (
                "Rejected extreme price/history ratio"
            )

    return tracker_price, "Validated"


# ============================================================
# PARSE TRACKER PRODUCT
# ============================================================

def parse_tracker(url):

    page = fetch(url)

    if not page:
        return None

    soup = BeautifulSoup(
        page,
        "html.parser"
    )

    title = extract_title(soup)

    store = None

    buy_url = find_buy_url(
        url,
        soup,
        page
    )

    if buy_url:
        store = retailer(buy_url)

    # --------------------------------------------------------
    # Tracker current price
    # --------------------------------------------------------

    tracker_current = structured_current_price(
        soup
    )

    # --------------------------------------------------------
    # Historical average
    # --------------------------------------------------------

    average = extract_average(
        soup,
        page
    )

    # --------------------------------------------------------
    # If structured current price wasn't available,
    # look for explicitly labelled current price.
    # --------------------------------------------------------

    if tracker_current is None:

        text = soup.get_text(
            " ",
            strip=True
        )

        labels = [
            "current price",
            "current",
            "selling price",
            "sale price",
            "today's price",
            "price today",
            "live price",
        ]

        lower = text.lower()

        for label in labels:

            pattern = (
                re.escape(label)
                + r".{0,100}?"
                r"(?:₹|rs\.?|inr)?\s*"
                r"([\d,]+(?:\.\d+)?)"
            )

            match = re.search(
                pattern,
                lower,
                re.I
            )

            if match:

                value = money(
                    match.group(1)
                )

                if value:
                    tracker_current = value
                    break

    if tracker_current is None:
        return None

    if average is None:
        return None

    # --------------------------------------------------------
    # Retailer cross-check
    # --------------------------------------------------------

    retailer_current = None

    if buy_url:

        retailer_html = fetch(
            buy_url,
            timeout=RETAILER_TIMEOUT
        )

        if retailer_html:

            retailer_soup = BeautifulSoup(
                retailer_html,
                "html.parser"
            )

            retailer_current = (
                extract_retailer_price(
                    retailer_soup
                )
            )

    # --------------------------------------------------------
    # Validate current price.
    # --------------------------------------------------------

    validated_price, reason = validate_current_price(
        tracker_current,
        retailer_current,
        average,
        title
    )

    if validated_price is None:

        print(
            f"REJECTED: {title[:60]} | "
            f"{reason}"
        )

        return None

    # --------------------------------------------------------
    # Store must be Flipkart or Myntra.
    # --------------------------------------------------------

    if store not in ("Flipkart", "Myntra"):

        return None

    # --------------------------------------------------------
    # Final deal rule.
    # --------------------------------------------------------

    qualifies = (
        validated_price <= MAX_PRICE
        and validated_price <= (
            average * MAX_AVERAGE_RATIO
        )
    )

    if qualifies:

        print(
            f"QUALIFIED: {title[:60]} | "
            f"{store} | "
            f"₹{validated_price:.0f} | "
            f"avg ₹{average:.0f}"
        )

    return {
        "title": title,
        "current": validated_price,
        "tracker_current": tracker_current,
        "retailer_current": retailer_current,
        "average": average,
        "store": store,
        "tracker_url": url,
        "buy_url": buy_url,
        "qualifies": qualifies,
    }


# ============================================================
# DISCOVERY
# ============================================================

def extract_links(base_url, page):

    soup = BeautifulSoup(
        page,
        "html.parser"
    )

    found = set()

    for a in soup.find_all(
        "a",
        href=True
    ):

        full = clean_url(
            urljoin(
                base_url,
                a.get("href")
            )
        )

        if not full:
            continue

        if (
            is_tracker_page(full)
            or is_retailer_product(full)
        ):

            found.add(full)

    return found


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
            timeout=REQUEST_TIMEOUT
        )

        if not page:
            print("  Could not fetch source")
            continue

        links = extract_links(page, source)

        print("  Product links found:", len(links))

        for link in links:
            if len(products) >= TARGET_PRODUCTS:
                break

            link = clean_url(link)

            if link in seen:
                continue

            if not is_tracker_page(link):
                continue

            seen.add(link)
            products.append(link)

        print("  Total discovered:", len(products))

        if len(products) >= TARGET_PRODUCTS:
            break

    print()
    print("==========================================")
    print("PRODUCT CHECK")
    print("==========================================")

    print("Products discovered:", len(products))

    results = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:

        futures = {
            executor.submit(parse_tracker, url): url
            for url in products
        }

        completed = 0

        for future in as_completed(futures):

            url = futures[future]
            completed += 1

            try:
                result = future.result()

                if result:
                    results.append(result)

                    print(
                        f"[{completed}/{len(products)}] "
                        f"{result.get('title', 'Unknown')[:70]} "
                        f"| ₹{result.get('current', 0):,.0f} "
                        f"| avg ₹{result.get('average', 0):,.0f} "
                        f"| {result.get('store', 'Unknown')}"
                    )

                else:
                    print(
                        f"[{completed}/{len(products)}] "
                        f"Rejected / invalid: {url}"
                    )

            except Exception as e:

                print(
                    f"[{completed}/{len(products)}] "
                    f"ERROR: {url} -> {e}"
                )

    print()
    print("==========================================")
    print("RESULTS")
    print("==========================================")

    print("Products checked:", len(products))
    print("Valid products:", len(results))

    qualified = []

    for item in results:

        current = item["current"]
        average = item["average"]

        if current <= MAX_PRICE and average > 0:

            ratio = current / average

            if ratio <= MAX_AVERAGE_RATIO:

                item["discount_from_average"] = (
                    (1 - ratio) * 100
                )

                qualified.append(item)

    print("Products qualified:", len(qualified))

    if not qualified:
        print()
        print("No products passed the final deal rule.")
        print()
        return

    print()
    print("==========================================")
    print("QUALIFIED DEALS")
    print("==========================================")

    for item in qualified:

        print()
        print("Product:", item["title"])
        print("Store:", item["store"])
        print("Current:", f"₹{item['current']:,.0f}")
        print("Average:", f"₹{item['average']:,.0f}")
        print(
            "Below average:",
            f"{item['discount_from_average']:.1f}%"
        )
        print("Buy:", item["buy_url"])

    print()
    print("==========================================")
    print("TELEGRAM")
    print("==========================================")

    state = load_state()

    alerts_sent = 0

    for item in qualified:

        key = (
            item["store"]
            + "|"
            + item["buy_url"]
        )

        if key in state:
            print("Already alerted:", item["title"][:60])
            continue

        message = (
            "🔥 VERIFIED PRICE DEAL\n\n"
            f"🛍️ {item['title']}\n\n"
            f"🏪 Store: {item['store']}\n"
            f"💰 Current price: ₹{item['current']:,.0f}\n"
            f"📊 Historical average: ₹{item['average']:,.0f}\n"
            f"📉 Below average: "
            f"{item['discount_from_average']:.1f}%\n\n"
            "✅ Current price validated\n"
            "✅ Meets ₹5,000 limit\n"
            "✅ Below 50% of historical average\n\n"
            f"🛒 BUY NOW:\n{item['buy_url']}"
        )

        if send_telegram(message):

            state[key] = {
                "title": item["title"],
                "store": item["store"],
                "current": item["current"],
                "average": item["average"],
                "sent_at": int(time.time())
            }

            alerts_sent += 1

            print(
                "Alert sent:",
                item["title"][:70]
            )

        else:

            print(
                "Telegram failed:",
                item["title"][:70]
            )

    save_state(state)

    print()
    print("==========================================")
    print("DONE")
    print("==========================================")

    print("Alerts sent:", alerts_sent)


if __name__ == "__main__":
    main()
