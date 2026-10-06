import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# SETTINGS
# ============================================================

MAX_PRICE = 5000.0
DROP_RATIO = 0.50

MAX_PRODUCT_PAGES = 150
REQUEST_DELAY = 0.5
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
}


# ============================================================
# PRICE HISTORY SOURCES
# ============================================================

SOURCE_PAGES = [
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/deals",
    ),
    (
        "PriceDropy",
        "https://pricedropy.com/",
    ),
    (
        "PriceDiff",
        "https://pricediff.in/top-deals/",
    ),
    (
        "PriceTrail",
        "https://pricehistorytracker.in/",
    ),
]


ALLOWED_STORES = {
    "flipkart": "Flipkart",
    "myntra": "Myntra",
}


session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# BASIC HELPERS
# ============================================================

def money(text):
    if not text:
        return None

    match = re.search(
        r"(?:₹|Rs\.?|INR)\s*"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE,
    )

    if not match:
        return None

    try:
        return float(
            match.group(1).replace(",", "")
        )
    except ValueError:
        return None


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


def normalise_store(text):
    text = (text or "").lower()

    for key, name in ALLOWED_STORES.items():
        if key in text:
            return name

    return None


# ============================================================
# LINK DISCOVERY
# ============================================================

def product_links_from_page(
    soup,
    base_url,
):
    found = []
    seen = set()

    for link in soup.find_all(
        "a",
        href=True,
    ):
        href = urljoin(
            base_url,
            link["href"],
        )

        parsed = urlparse(href)

        if parsed.scheme not in (
            "http",
            "https",
        ):
            continue

        host = parsed.netloc.lower()
        path = parsed.path.lower()

        allowed_domain = any(
            domain in host
            for domain in (
                "pricehistoryapp.com",
                "pricedropy.com",
                "pricediff.in",
                "pricehistorytracker.in",
            )
        )

        if not allowed_domain:
            continue

        # Accept common product URL formats.
        looks_like_product = (
            "/product/" in path
            or "/products/" in path
            or "/item/" in path
            or "price-history" in path
            or "pricehistory" in path
        )

        if not looks_like_product:
            continue

        if href in seen:
            continue

        text = " ".join(
            link.stripped_strings
        )

        if not text:
            continue

        store = normalise_store(
            text
        )

        if store is None:
            # Sometimes store name is in
            # the URL or nearby page data.
            store = normalise_store(
                href
            )

        if store is None:
            continue

        seen.add(href)

        found.append(
            (
                href,
                text,
                store,
            )
        )

    return found


def discover_candidates(
    source_name,
    start_url,
    max_candidates=100,
):
    candidates = []
    seen = set()

    try:
        soup = get_soup(start_url)
    except Exception as error:
        print(
            f"[{source_name}] "
            f"Could not open source: {error}"
        )
        return candidates

    links = product_links_from_page(
        soup,
        start_url,
    )

    for (
        href,
        anchor_text,
        store,
    ) in links:

        if href in seen:
            continue

        seen.add(href)

        candidates.append(
            {
                "source": source_name,
                "tracker_url": href,
                "anchor_text": anchor_text,
                "store": store,
            }
        )

        if len(candidates) >= max_candidates:
            break

    return candidates


# ============================================================
# DIRECT BUY LINK
# ============================================================

def extract_store_buy_link(
    soup,
    store,
):
    wanted = store.lower()

    # First look for obvious BUY buttons.
    for link in soup.find_all(
        "a",
        href=True,
    ):
        text = " ".join(
            link.stripped_strings
        ).lower()

        href = link["href"]

        if wanted not in text:
            continue

        if store == "Flipkart":
            if "flipkart.com" in href.lower():
                return href

        elif store == "Myntra":
            if "myntra.com" in href.lower():
                return href

    # Second attempt: any direct retailer URL.
    for link in soup.find_all(
        "a",
        href=True,
    ):
        href = link["href"]

        if store == "Flipkart":
            if "flipkart.com" in href.lower():
                return href

        elif store == "Myntra":
            if "myntra.com" in href.lower():
                return href

    return None


# ============================================================
# PRICE PARSING
# ============================================================

def find_current_price(page_text):
    patterns = [
        r"Current\s*Price\s*:?\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"Current\s*:?\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"Current price\s*:?\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            page_text,
            re.IGNORECASE,
        )

        if match:
            return float(
                match.group(1).replace(",", "")
            )

    return None


def find_average(page_text):
    # Prefer 30-day average.
    patterns_30 = [
        r"30d\s*Average\s*:?\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"30-day\s*average\s*:?\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"30\s*day\s*average\s*:?\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    ]

    for pattern in patterns_30:
        match = re.search(
            pattern,
            page_text,
            re.IGNORECASE,
        )

        if match:
            return (
                float(
                    match.group(1).replace(",", "")
                ),
                30,
            )

    # Then 90-day average.
    patterns_90 = [
        r"90d\s*Average\s*:?\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"90-day\s*average\s*:?\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"90\s*day\s*average\s*:?\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    ]

    for pattern in patterns_90:
        match = re.search(
            pattern,
            page_text,
            re.IGNORECASE,
        )

        if match:
            return (
                float(
                    match.group(1).replace(",", "")
                ),
                90,
            )

    # Final fallback.
    pattern = (
        r"Average\s*price\s*:?\s*"
        r"(?:₹|Rs\.?|INR)\s*"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)"
    )

    match = re.search(
        pattern,
        page_text,
        re.IGNORECASE,
    )

    if match:
        return (
            float(
                match.group(1).replace(",", "")
            ),
            0,
        )

    return None, None


# ============================================================
# READ PRODUCT PAGE
# ============================================================

def parse_product_page(
    product_url,
    expected_store=None,
):
    soup = get_soup(
        product_url
    )

    page_text = " ".join(
        soup.stripped_strings
    )

    # Product title.
    title = ""

    heading = soup.find("h1")

    if heading:
        title = " ".join(
            heading.stripped_strings
        )

    if not title:
        title = "Unknown product"

    # Store.
    store = (
        expected_store
        or normalise_store(page_text)
        or normalise_store(product_url)
    )

    if store not in (
        "Flipkart",
        "Myntra",
    ):
        return None

    # Current price.
    current = find_current_price(
        page_text
    )

    # Historical average.
    average, average_days = find_average(
        page_text
    )

    if (
        current is None
        or average is None
        or average <= 0
    ):
        return None

    # Direct retailer URL.
    buy_url = extract_store_buy_link(
        soup,
        store,
    )

    if not buy_url:
        buy_url = product_url

    return {
        "title": title,
        "store": store,
        "current": current,
        "average": average,
        "average_days": average_days,
        "tracker_url": product_url,
        "buy_url": buy_url,
    }


# ============================================================
# PRODUCT DEDUPLICATION
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
            "TELEGRAM_BOT_TOKEN is missing"
        )

    if not chat_id:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is missing"
        )

    current = item["current"]
    average = item["average"]

    drop = (
        1 - current / average
    ) * 100

    sources = ", ".join(
        sorted(evidence)
    )

    title = escape_html(
        item["title"]
    )

    if item["average_days"]:
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
        f"<b>{drop:.1f}%</b>\n"
        f"🔎 Found by: {sources}\n\n"
        "✅ Under ₹5,000\n"
        "✅ At least 50% below average\n\n"
        "⚠️ Verify seller, size/variant "
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
                        "url": item["buy_url"],
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
            ]
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
    print(
        "================================"
    )
    print(
        "VIBHU PRICE SNIPER STARTING"
    )
    print(
        "================================"
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

    all_candidates = []

    # --------------------------------------------------------
    # STEP 1: Discover candidates
    # --------------------------------------------------------

    for (
        source_name,
        source_url,
    ) in SOURCE_PAGES:

        found = discover_candidates(
            source_name,
            source_url,
            max_candidates=60,
        )

        print(
            f"[{source_name}] "
            f"found {len(found)} candidates"
        )

        all_candidates.extend(
            found
        )

        time.sleep(
            REQUEST_DELAY
        )

    # --------------------------------------------------------
    # STEP 2: Remove duplicate tracker URLs
    # --------------------------------------------------------

    unique_candidates = {}

    for candidate in all_candidates:
        unique_candidates[
            candidate["tracker_url"]
        ] = candidate

    candidates = list(
        unique_candidates.values()
    )

    candidates = candidates[
        :MAX_PRODUCT_PAGES
    ]

    print(
        f"Unique candidates: "
        f"{len(candidates)}"
    )

    # --------------------------------------------------------
    # STEP 3: Read product pages
    # --------------------------------------------------------

    grouped = {}

    scanned = 0
    errors = 0

    for candidate in candidates:

        try:
            item = parse_product_page(
                candidate["tracker_url"],
                candidate["store"],
            )

        except Exception as error:
            errors += 1

            print(
                "ERROR: "
                f"{candidate['tracker_url']} "
                f"-> {error}"
            )

            continue

        if not item:
            continue

        scanned += 1

        # Ignore products above ₹5,000.
        if item["current"] > MAX_PRICE:
            continue

        key = product_key(item)

        if key not in grouped:
            grouped[key] = {
                "item": item,
                "evidence": set(),
            }

        grouped[key]["evidence"].add(
            candidate["source"]
        )

        old_item = grouped[key]["item"]

        # Prefer 30-day average if available.
        if (
            item["average_days"] == 30
            and old_item["average_days"] != 30
        ):
            grouped[key]["item"] = item

        print(
            f"{item['store']} | "
            f"{item['title']} | "
            f"₹{item['current']:,.0f} | "
            f"avg ₹{item['average']:,.0f} | "
            f"{item['average_days']}d"
        )

        time.sleep(
            REQUEST_DELAY
        )

    # --------------------------------------------------------
    # STEP 4: Apply deal rule
    # --------------------------------------------------------

    qualified = 0
    sent = 0

    for group in grouped.values():

        item = group["item"]
        evidence = group["evidence"]

        current = item["current"]
        average = item["average"]

        qualifies = (
            current <= MAX_PRICE
            and current <= average * DROP_RATIO
        )

        if not qualifies:
            continue

        qualified += 1

        state_key = (
            item["store"]
            + "::"
            + item["title"].lower()
        )

        now = int(
            time.time()
        )

        previous = state.get(
            state_key
        )

        # Do not repeatedly send the same
        # deal for 7 days unless price falls.
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

            state[state_key] = {
                "price": current,
                "time": now,
            }

            sent += 1

            print(
                "ALERT SENT: "
                f"{item['title']}"
            )

        except Exception as error:
            print(
                "TELEGRAM ERROR: "
                f"{error}"
            )

        time.sleep(0.5)

    # --------------------------------------------------------
    # STEP 5: Save state
    # --------------------------------------------------------

    save_state(state)

    print("")
    print(
        "================================"
    )
    print(
        "SCAN COMPLETE"
    )
    print(
        "================================"
    )
    print(
        f"Total candidates: "
        f"{len(all_candidates)}"
    )
    print(
        f"Unique candidates: "
        f"{len(candidates)}"
    )
    print(
        f"Product pages scanned: "
        f"{scanned}"
    )
    print(
        f"Qualified deals: "
        f"{qualified}"
    )
    print(
        f"Telegram alerts sent: "
        f"{sent}"
    )
    print(
        f"Errors: "
        f"{errors}"
    )
    print(
        "================================"
    )


if __name__ == "__main__":
    main()
