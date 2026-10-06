import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# SETTINGS
# ============================================================

MAX_PRICE = 5000.0
DROP_RATIO = 0.50

# Number of product pages we actually want to CHECK.
TARGET_PRODUCTS = 1000

# Discover more than required so duplicates/failures don't
# reduce the final number too much.
DISCOVERY_TARGET = 5000

REQUEST_DELAY = 0.15
TIMEOUT = 15

# Maximum listing pages to discover from each starting page.
MAX_PAGES_PER_SOURCE = 100

STATE_FILE = Path("state.json")


# ============================================================
# HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
}


# ============================================================
# SOURCE PAGES
# ============================================================

SOURCE_PAGES = [
    # -------------------------
    # PriceHistoryApp
    # -------------------------
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/latest-deals",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/store/flipkart",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/store/myntra",
    ),

    # -------------------------
    # PriceDropy
    # -------------------------
    (
        "PriceDropy",
        "https://pricedropy.com/",
    ),
    (
        "PriceDropy",
        "https://pricedropy.com/flipkart-price-history",
    ),
    (
        "PriceDropy",
        "https://pricedropy.com/myntra-price-history",
    ),

    # -------------------------
    # PriceTrail
    # -------------------------
    (
        "PriceTrail",
        "https://pricehistorytracker.in/",
    ),
    (
        "PriceTrail",
        "https://pricehistorytracker.in/latest-deals",
    ),
]


# ============================================================
# OPTIONAL CATEGORY PAGES
#
# These provide additional discovery routes.
# ============================================================

EXTRA_SOURCE_PAGES = [
    # PriceHistoryApp categories
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/category/clothing",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/category/footwear",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/category/electronics",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/category/home",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/category/kitchen",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/category/mobiles-accessories",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/category/bags-accessories",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/category/jewellery-watches",
    ),
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals/category/sports-outdoors",
    ),

    # PriceTrail categories
    (
        "PriceTrail",
        "https://pricehistorytracker.in/latest-deals?category=fashion",
    ),
    (
        "PriceTrail",
        "https://pricehistorytracker.in/latest-deals?category=footwear",
    ),
    (
        "PriceTrail",
        "https://pricehistorytracker.in/latest-deals?category=electronics",
    ),
    (
        "PriceTrail",
        "https://pricehistorytracker.in/latest-deals?category=home-kitchen",
    ),
    (
        "PriceTrail",
        "https://pricehistorytracker.in/latest-deals?category=mobiles",
    ),
]


# ============================================================
# PAGINATION PATTERNS
# ============================================================

PAGE_PATTERNS = [
    "?page={}",
    "?page={}&store=flipkart",
    "?page={}&store=myntra",
    "&page={}",
    "/page/{}",
]


# ============================================================
# SESSION
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# STATE
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


# ============================================================
# HTTP
# ============================================================

def get_soup(url):
    response = session.get(
        url,
        timeout=TIMEOUT,
        allow_redirects=True,
    )

    response.raise_for_status()

    return BeautifulSoup(
        response.text,
        "html.parser",
    )


# ============================================================
# STORE DETECTION
# ============================================================

def normalise_store(text):
    text = (text or "").lower()

    if "flipkart" in text:
        return "Flipkart"

    if "myntra" in text:
        return "Myntra"

    return None


# ============================================================
# URL CLEANING
# ============================================================

def clean_url(url):
    parsed = urlparse(url)

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
            "",
            "",
            "",
        )
    )


# ============================================================
# PRODUCT URL DETECTION
# ============================================================

def looks_like_product_url(url):
    path = urlparse(url).path.lower()

    product_words = [
        "/product/",
        "/products/",
        "/item/",
        "/price-history/",
        "/pricehistory/",
        "/track/",
        "/tracker/",
        "/price-history",
        "/pricehistory",
        "/product",
    ]

    return any(
        word in path
        for word in product_words
    )


# ============================================================
# TRACKER DOMAIN
# ============================================================

def is_tracker_domain(url):
    host = urlparse(url).netloc.lower()

    domains = [
        "pricehistoryapp.com",
        "pricedropy.com",
        "pricehistorytracker.in",
    ]

    return any(
        domain in host
        for domain in domains
    )


# ============================================================
# PRODUCT LINK DETECTION
# ============================================================

def extract_product_links(
    soup,
    base_url,
    source_name,
):
    results = []
    seen = set()

    for link in soup.find_all(
        "a",
        href=True,
    ):
        href = urljoin(
            base_url,
            link["href"],
        )

        if not is_tracker_domain(href):
            continue

        href = clean_url(href)

        if href in seen:
            continue

        text = " ".join(
            link.stripped_strings
        )

        combined = (
            href.lower()
            + " "
            + text.lower()
        )

        # Must look like an actual product page.
        if not looks_like_product_url(href):
            continue

        # Determine store from URL/text.
        store = normalise_store(
            combined
        )

        if store not in (
            "Flipkart",
            "Myntra",
        ):
            continue

        seen.add(href)

        results.append(
            {
                "tracker_url": href,
                "store": store,
                "anchor_text": text,
                "source": source_name,
            }
        )

    return results


# ============================================================
# PAGINATION LINK DETECTION
# ============================================================

def extract_pagination_links(
    soup,
    base_url,
):
    links = []
    seen = set()

    for link in soup.find_all(
        "a",
        href=True,
    ):
        href = urljoin(
            base_url,
            link["href"],
        )

        if not is_tracker_domain(href):
            continue

        text = " ".join(
            link.stripped_strings
        ).lower()

        href_lower = href.lower()

        is_pagination = (
            "page=" in href_lower
            or "/page/" in href_lower
            or text in (
                "next",
                "next page",
                "older",
                ">",
                "»",
                "›",
            )
        )

        if not is_pagination:
            continue

        href = clean_url(href)

        if href in seen:
            continue

        seen.add(href)

        links.append(href)

    return links


# ============================================================
# DISCOVER ONE PAGE
# ============================================================

def discover_page(
    source_name,
    url,
):
    try:
        soup = get_soup(url)

    except Exception as error:
        print(
            f"[{source_name}] FAILED: "
            f"{url} -> {error}"
        )

        return [], []

    products = extract_product_links(
        soup,
        url,
        source_name,
    )

    pagination = extract_pagination_links(
        soup,
        url,
    )

    return products, pagination


# ============================================================
# CONVENTIONAL PAGE URL
# ============================================================

def generate_page_urls(
    source_name,
    base_url,
):
    urls = []

    parsed = urlparse(base_url)

    # Don't generate nonsense page combinations
    # for obviously non-listing product URLs.
    if looks_like_product_url(base_url):
        return urls

    for page_number in range(
        2,
        MAX_PAGES_PER_SOURCE + 1,
    ):
        for pattern in PAGE_PATTERNS:

            candidate = (
                base_url.rstrip("/")
                + pattern.format(
                    page_number
                )
            )

            # Keep source identity.
            urls.append(
                (
                    source_name,
                    candidate,
                )
            )

    return urls


# ============================================================
# MASS DISCOVERY
# ============================================================

def discover_products():

    queue = []
    queued = set()

    # Start with normal source pages.
    all_sources = (
        SOURCE_PAGES
        + EXTRA_SOURCE_PAGES
    )

    for source_name, url in all_sources:

        if url in queued:
            continue

        queue.append(
            (
                source_name,
                url,
            )
        )

        queued.add(url)

    discovered = {}
    processed_pages = set()

    print("")
    print("=" * 60)
    print("STARTING HIGH-COVERAGE DISCOVERY")
    print(f"Target products: {TARGET_PRODUCTS}")
    print(f"Discovery target: {DISCOVERY_TARGET}")
    print("=" * 60)

    while (
        queue
        and len(discovered)
        < DISCOVERY_TARGET
    ):

        source_name, url = queue.pop(0)

        if url in processed_pages:
            continue

        processed_pages.add(url)

        products, pagination = discover_page(
            source_name,
            url,
        )

        # ----------------------------------------------------
        # SAVE PRODUCTS
        # ----------------------------------------------------

        for product in products:

            product_url = product[
                "tracker_url"
            ]

            if product_url not in discovered:
                discovered[
                    product_url
                ] = product

            else:
                # Product found on multiple sources.
                # Preserve source information.
                old = discovered[
                    product_url
                ]

                old_sources = set(
                    old.get(
                        "sources",
                        [],
                    )
                )

                old_sources.add(
                    product.get(
                        "source",
                        source_name,
                    )
                )

                old[
                    "sources"
                ] = sorted(
                    old_sources
                )

        # ----------------------------------------------------
        # FOLLOW REAL PAGINATION
        # ----------------------------------------------------

        for next_url in pagination:

            if next_url in queued:
                continue

            if next_url in processed_pages:
                continue

            queued.add(next_url)

            queue.append(
                (
                    source_name,
                    next_url,
                )
            )

        # ----------------------------------------------------
        # ADD CONVENTIONAL PAGINATION
        #
        # Only do this for source/listing pages.
        # ----------------------------------------------------

        if (
            len(discovered)
            < DISCOVERY_TARGET
        ):

            generated = generate_page_urls(
                source_name,
                url,
            )

            for (
                generated_source,
                candidate,
            ) in generated:

                if candidate in queued:
                    continue

                if candidate in processed_pages:
                    continue

                queued.add(candidate)

                queue.append(
                    (
                        generated_source,
                        candidate,
                    )
                )

                # Prevent an enormous queue.
                if len(queue) >= 250:
                    break

        print(
            f"[DISCOVERY] "
            f"{source_name} | "
            f"products={len(products)} | "
            f"total={len(discovered)} | "
            f"pages={len(processed_pages)} | "
            f"queue={len(queue)}"
        )

        time.sleep(
            REQUEST_DELAY
        )

    products = list(
        discovered.values()
    )

    # --------------------------------------------------------
    # RANDOMISE SOURCE ORDER SLIGHTLY
    #
    # This prevents one source from completely dominating
    # the first 1,000 products.
    # --------------------------------------------------------

    flipkart = [
        p for p in products
        if p.get("store") == "Flipkart"
    ]

    myntra = [
        p for p in products
        if p.get("store") == "Myntra"
    ]

    # Interleave the two stores.
    balanced = []

    max_len = max(
        len(flipkart),
        len(myntra),
    )

    for i in range(max_len):

        if i < len(flipkart):
            balanced.append(
                flipkart[i]
            )

        if i < len(myntra):
            balanced.append(
                myntra[i]
            )

    products = balanced

    print("")
    print("=" * 60)
    print("DISCOVERY FINISHED")
    print("=" * 60)
    print(
        f"Pages visited: "
        f"{len(processed_pages)}"
    )
    print(
        f"Unique products discovered: "
        f"{len(products)}"
    )
    print(
        f"Flipkart discovered: "
        f"{len(flipkart)}"
    )
    print(
        f"Myntra discovered: "
        f"{len(myntra)}"
    )

    return products


# ============================================================
# PRICE PARSING
# ============================================================

def find_current_price(text):

    patterns = [
        r"Current\s*Price\s*:?\s*"
        r"(?:₹|Rs\.?|INR)\s*"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)",

        r"Current\s*:?\s*"
        r"(?:₹|Rs\.?|INR)\s*"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)",

        r"Current price\s*:?\s*"
        r"(?:₹|Rs\.?|INR)\s*"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            return float(
                match.group(1)
                .replace(",", "")
            )

    return None


def find_average(text):

    patterns = [

        # 30-day
        (
            30,
            r"30d\s*Average\s*:?\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",
        ),

        (
            30,
            r"30-day\s*average\s*:?\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",
        ),

        (
            30,
            r"30\s*day\s*average\s*:?\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",
        ),

        # 90-day
        (
            90,
            r"90d\s*Average\s*:?\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",
        ),

        (
            90,
            r"90-day\s*average\s*:?\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",
        ),

        (
            90,
            r"90\s*day\s*average\s*:?\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",
        ),

        # Generic
        (
            0,
            r"Average\s*price\s*:?\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",
        ),
    ]

    for days, pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:

            return (
                float(
                    match.group(1)
                    .replace(",", "")
                ),
                days,
            )

    return None, None


# ============================================================
# DIRECT STORE LINK
# ============================================================

def find_buy_link(
    soup,
    store,
):

    store_domain = (
        "flipkart.com"
        if store == "Flipkart"
        else "myntra.com"
    )

    # Prefer BUY / SHOP links.
    for link in soup.find_all(
        "a",
        href=True,
    ):

        href = link["href"]

        if store_domain not in href.lower():
            continue

        text = " ".join(
            link.stripped_strings
        ).lower()

        if (
            store.lower() in text
            or "buy" in text
            or "shop" in text
        ):
            return href

    # Fallback.
    for link in soup.find_all(
        "a",
        href=True,
    ):

        href = link["href"]

        if store_domain in href.lower():
            return href

    return None


# ============================================================
# READ PRODUCT
# ============================================================

def parse_product(candidate):

    url = candidate[
        "tracker_url"
    ]

    expected_store = candidate[
        "store"
    ]

    soup = get_soup(url)

    text = " ".join(
        soup.stripped_strings
    )

    title = ""

    heading = soup.find("h1")

    if heading:
        title = " ".join(
            heading.stripped_strings
        )

    if not title:
        title = "Unknown product"

    store = (
        expected_store
        or normalise_store(text)
        or normalise_store(url)
    )

    if store not in (
        "Flipkart",
        "Myntra",
    ):
        return None

    current = find_current_price(
        text
    )

    average, average_days = find_average(
        text
    )

    if (
        current is None
        or average is None
        or average <= 0
    ):
        return None

    buy_url = find_buy_link(
        soup,
        store,
    )

    if not buy_url:
        buy_url = url

    return {
        "title": title,
        "store": store,
        "current": current,
        "average": average,
        "average_days": average_days,
        "tracker_url": url,
        "buy_url": buy_url,
    }


# ============================================================
# PRODUCT KEY
# ============================================================

def product_key(item):

    title = re.sub(
        r"[^a-z0-9]+",
        " ",
        item["title"].lower(),
    ).strip()

    return (
        item["store"].lower()
        + "::"
        + title
    )


# ============================================================
# TELEGRAM
# ============================================================

def escape_html(text):

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_telegram(
    item,
    evidence,
):

    token = os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID"
    )

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN missing"
        )

    if not chat_id:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID missing"
        )

    current = item[
        "current"
    ]

    average = item[
        "average"
    ]

    discount = (
        1
        - current / average
    ) * 100

    title = escape_html(
        item["title"]
    )

    sources = ", ".join(
        sorted(evidence)
    )

    if item[
        "average_days"
    ]:
        average_label = (
            f"{item['average_days']}-day average"
        )
    else:
        average_label = (
            "Historical average"
        )

    message = (
        "🚨 <b>MEGA DEAL FOUND</b>\n\n"
        f"🛍 <b>{title}</b>\n"
        f"🏪 {item['store']}\n\n"
        f"💰 Current: "
        f"<b>₹{current:,.0f}</b>\n"
        f"📊 {average_label}: "
        f"<b>₹{average:,.0f}</b>\n"
        f"📉 Below average: "
        f"<b>{discount:.1f}%</b>\n"
        f"🔎 Sources: {sources}\n\n"
        "✅ Current price ≤ ₹5,000\n"
        "✅ Current price ≤ 50% of average\n\n"
        "⚠️ Check seller, size/variant "
        "and final checkout price."
    )

    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": (
                            "🛒 BUY ON "
                            + item["store"].upper()
                        ),
                        "url": item[
                            "buy_url"
                        ],
                    }
                ],
                [
                    {
                        "text": "📈 PRICE HISTORY",
                        "url": item[
                            "tracker_url"
                        ],
                    }
                ],
            ],
        },
    }

    response = session.post(
        "https://api.telegram.org/"
        f"bot{token}/sendMessage",
        json=payload,
        timeout=TIMEOUT,
    )

    response.raise_for_status()


# ============================================================
# MAIN
# ============================================================

def main():

    print("")
    print("=" * 60)
    print("       VIBHU PRICE SNIPER")
    print("       1000+ PRODUCT SCANNER")
    print("=" * 60)

    if not os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    ):
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is missing"
        )

    if not os.environ.get(
        "TELEGRAM_CHAT_ID"
    ):
        raise SystemExit(
            "TELEGRAM_CHAT_ID is missing"
        )

    state = load_state()

    # ========================================================
    # DISCOVERY
    # ========================================================

    candidates = discover_products()

    discovered_count = len(
        candidates
    )

    if discovered_count < TARGET_PRODUCTS:

        print("")
        print("WARNING")
        print(
            f"Only {discovered_count} "
            "products were discovered."
        )
        print(
            "The public sources did not expose "
            "1,000 unique products to this run."
        )

    # Only check TARGET_PRODUCTS.
    candidates = candidates[
        :TARGET_PRODUCTS
    ]

    print("")
    print("=" * 60)
    print(
        f"PRODUCT PAGES TO CHECK: "
        f"{len(candidates)}"
    )
    print("=" * 60)

    # ========================================================
    # READ PRODUCTS
    # ========================================================

    grouped = {}

    successful = 0
    failed = 0
    above_price = 0
    unreadable = 0

    for index, candidate in enumerate(
        candidates,
        start=1,
    ):

        try:

            item = parse_product(
                candidate
            )

        except Exception as error:

            failed += 1

            print(
                f"[{index}/{len(candidates)}] "
                f"FAILED: {error}"
            )

            continue

        if not item:

            unreadable += 1

            continue

        successful += 1

        # Products above ₹5,000 do not need
        # further deal processing.
        if item[
            "current"
        ] > MAX_PRICE:

            above_price += 1

            if index % 25 == 0:
                print(
                    f"Progress: "
                    f"{index}/{len(candidates)} "
                    f"| read={successful} "
                    f"| failed={failed} "
                    f"| above ₹5k={above_price}"
                )

            continue

        key = product_key(
            item
        )

        if key not in grouped:

            grouped[key] = {
                "item": item,
                "evidence": set(),
            }

        # ----------------------------------------------------
        # IMPORTANT:
        # Use source safely.
        #
        # The previous version used:
        # candidate["source"]
        #
        # but source was never guaranteed.
        # ----------------------------------------------------

        source_name = candidate.get(
            "source",
            "Unknown",
        )

        grouped[key][
            "evidence"
        ].add(
            source_name
        )

        # Add any additional source evidence.
        for source in candidate.get(
            "sources",
            [],
        ):
            grouped[key][
                "evidence"
            ].add(source)

        old = grouped[key][
            "item"
        ]

        # Prefer 30-day average.
        if (
            item[
                "average_days"
            ] == 30
            and old[
                "average_days"
            ] != 30
        ):

            grouped[key][
                "item"
            ] = item

        if index % 25 == 0:

            print(
                f"Progress: "
                f"{index}/{len(candidates)} "
                f"pages checked | "
                f"read={successful} | "
                f"failed={failed} | "
                f"above ₹5k={above_price} | "
                f"usable={len(grouped)}"
            )

        time.sleep(
            REQUEST_DELAY
        )

    # ========================================================
    # DEAL FILTER
    # ========================================================

    qualified = 0
    alerts = 0

    for group in grouped.values():

        item = group[
            "item"
        ]

        evidence = group[
            "evidence"
        ]

        current = item[
            "current"
        ]

        average = item[
            "average"
        ]

        if not (
            current <= MAX_PRICE
            and current
            <= average * DROP_RATIO
        ):
            continue

        qualified += 1

        state_key = (
            item[
                "store"
            ]
            + "::"
            + item[
                "title"
            ].lower()
        )

        now = int(
            time.time()
        )

        previous = state.get(
            state_key
        )

        # Don't repeat the same deal for 7 days
        # unless the price becomes lower.
        if (
            previous
            and previous.get(
                "price",
                10**12,
            ) <= current
            and (
                now
                - previous.get(
                    "time",
                    0,
                )
                < 7 * 86400
            )
        ):
            continue

        try:

            send_telegram(
                item,
                evidence,
            )

            state[
                state_key
            ] = {
                "price": current,
                "time": now,
            }

            alerts += 1

            print(
                "🚨 ALERT: "
                f"{item['title']} | "
                f"₹{current:,.0f} | "
                f"avg ₹{average:,.0f}"
            )

        except Exception as error:

            print(
                "Telegram error: "
                f"{error}"
            )

        time.sleep(
            0.4
        )

    # ========================================================
    # SAVE STATE
    # ========================================================

    save_state(
        state
    )

    # ========================================================
    # FINAL REPORT
    # ========================================================

    print("")
    print("=" * 60)
    print("             FINAL REPORT")
    print("=" * 60)

    print(
        f"Products discovered: "
        f"{discovered_count}"
    )

    print(
        f"Product pages selected: "
        f"{len(candidates)}"
    )

    print(
        f"Product pages successfully read: "
        f"{successful}"
    )

    print(
        f"Product pages failed: "
        f"{failed}"
    )

    print(
        f"Products unreadable/no price data: "
        f"{unreadable}"
    )

    print(
        f"Products above ₹5,000: "
        f"{above_price}"
    )

    print(
        f"Unique products after filtering: "
        f"{len(grouped)}"
    )

    print(
        f"Qualified mega deals: "
        f"{qualified}"
    )

    print(
        f"Telegram alerts sent: "
        f"{alerts}"
    )

    print("=" * 60)


if __name__ == "__main__":
    main()
