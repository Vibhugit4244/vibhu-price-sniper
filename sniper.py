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

# TARGET: at least 1,000 product pages per run
TARGET_PRODUCTS = 1000

# Discover more than we need so failed/duplicate pages
# do not reduce the final scan below the target.
DISCOVERY_TARGET = 3000

REQUEST_DELAY = 0.20
TIMEOUT = 20

STATE_FILE = Path("state.json")


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
# SOURCES
# ============================================================

SOURCE_PAGES = [
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
    (
        "PriceTrail",
        "https://pricehistorytracker.in/",
    ),
    (
        "PriceTrail",
        "https://pricehistorytracker.in/latest-deals",
    ),
]


# Pages to try.
# Not every source necessarily has every page.
PAGE_PATTERNS = [
    "?page={}",
    "?page={}&store=flipkart",
    "?page={}&store=myntra",
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

    # Remove query and fragment so the same product
    # doesn't appear repeatedly with tracking parameters.
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
# PRODUCT LINK DETECTION
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
    ]

    return any(
        word in path
        for word in product_words
    )


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


def extract_product_links(
    soup,
    base_url,
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

        if not looks_like_product_url(
            href
        ):
            continue

        href = clean_url(href)

        if href in seen:
            continue

        text = " ".join(
            link.stripped_strings
        )

        store = normalise_store(
            text
        )

        if store is None:
            store = normalise_store(
                href
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
            }
        )

    return results


# ============================================================
# DISCOVER PAGINATION LINKS
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
            f"[{source_name}] "
            f"FAILED: {url} -> {error}"
        )

        return [], []

    products = extract_product_links(
        soup,
        url,
    )

    pagination = extract_pagination_links(
        soup,
        url,
    )

    return products, pagination


# ============================================================
# MASS DISCOVERY
# ============================================================

def discover_products():
    queue = []
    queued = set()

    for (
        source_name,
        url,
    ) in SOURCE_PAGES:

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
    print(
        "========================================"
    )
    print(
        "STARTING HIGH-COVERAGE DISCOVERY"
    )
    print(
        "Target products: "
        f"{TARGET_PRODUCTS}"
    )
    print(
        "Discovery target: "
        f"{DISCOVERY_TARGET}"
    )
    print(
        "========================================"
    )

    while queue and len(discovered) < DISCOVERY_TARGET:

        source_name, url = queue.pop(0)

        if url in processed_pages:
            continue

        processed_pages.add(url)

        products, pagination = discover_page(
            source_name,
            url,
        )

        for product in products:

            product_url = product[
                "tracker_url"
            ]

            if product_url not in discovered:
                discovered[
                    product_url
                ] = product

        print(
            f"[DISCOVERY] "
            f"{source_name} | "
            f"products={len(products)} | "
            f"total={len(discovered)} | "
            f"pages={len(processed_pages)}"
        )

        # Follow discovered pagination.
        for next_url in pagination:

            if next_url not in queued:
                queued.add(next_url)

                queue.append(
                    (
                        source_name,
                        next_url,
                    )
                )

        # If a site doesn't expose pagination links,
        # try conventional page-number URLs.
        if len(discovered) < DISCOVERY_TARGET:

            for page_number in range(
                2,
                51,
            ):

                for pattern in PAGE_PATTERNS:

                    candidate = (
                        url.rstrip("/")
                        + pattern.format(
                            page_number
                        )
                    )

                    if candidate not in queued:
                        queued.add(candidate)

                        queue.append(
                            (
                                source_name,
                                candidate,
                            )
                        )

                # Don't flood queue unnecessarily.
                if len(queue) > 150:
                    break

        time.sleep(
            REQUEST_DELAY
        )

    products = list(
        discovered.values()
    )

    print("")
    print(
        "DISCOVERY FINISHED"
    )
    print(
        f"Pages visited: "
        f"{len(processed_pages)}"
    )
    print(
        f"Unique products discovered: "
        f"{len(products)}"
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

    # Prefer a link whose text actually says
    # Flipkart/Myntra/Buy.
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

    # Fallback: any direct retailer URL.
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

def parse_product(
    candidate,
):

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
    print(
        "========================================"
    )
    print(
        "       VIBHU PRICE SNIPER"
    )
    print(
        "       1000+ PRODUCT SCANNER"
    )
    print(
        "========================================"
    )

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

    # --------------------------------------------------------
    # DISCOVERY
    # --------------------------------------------------------

    candidates = discover_products()

    if len(candidates) < TARGET_PRODUCTS:
        print("")
        print(
            "WARNING:"
        )
        print(
            f"Only {len(candidates)} "
            "products were discovered."
        )
        print(
            "The public sources did not expose "
            "1,000 unique products to this run."
        )

    # Take at least the first 1,000 when available.
    candidates = candidates[
        :TARGET_PRODUCTS
    ]

    print("")
    print(
        "========================================"
    )
    print(
        f"PRODUCT PAGES TO CHECK: "
        f"{len(candidates)}"
    )
    print(
        "========================================"
    )

    # --------------------------------------------------------
    # READ PRODUCTS
    # --------------------------------------------------------

    grouped = {}

    successful = 0
    failed = 0

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
            continue

        successful += 1

        # We don't need expensive processing
        # for products already above ₹5,000.
        if item[
            "current"
        ] > MAX_PRICE:
            continue

        key = product_key(
            item
        )

        if key not in grouped:

            grouped[key] = {
                "item": item,
                "evidence": set(),
            }

        grouped[key][
            "evidence"
        ].add(
            candidate[
                "source"
            ]
        )

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

        if (
            index % 25 == 0
        ):
            print(
                f"Progress: "
                f"{index}/{len(candidates)} "
                f"pages checked | "
                f"read={successful} | "
                f"failed={failed}"
            )

        time.sleep(
            REQUEST_DELAY
        )

    # --------------------------------------------------------
    # DEAL FILTER
    # --------------------------------------------------------

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

        # Repeat after 7 days only if price
        # has not become lower.
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

    save_state(
        state
    )

    # --------------------------------------------------------
    # FINAL REPORT
    # --------------------------------------------------------

    print("")
    print(
        "========================================"
    )
    print(
        "             FINAL REPORT"
    )
    print(
        "========================================"
    )

    print(
        f"Products discovered: "
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

    print(
        "========================================"
    )


if __name__ == "__main__":
    main()
      
