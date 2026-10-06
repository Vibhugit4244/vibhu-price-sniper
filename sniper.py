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

# Maximum tracker product pages opened during one run.
# This keeps the scanner reasonably polite to the public trackers.
MAX_PRODUCT_PAGES = 180

REQUEST_DELAY = 0.25
TIMEOUT = 25

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
# PUBLIC PRICE-HISTORY SOURCES
# ============================================================

SOURCE_PAGES = [
    (
        "PriceHistoryApp",
        "https://pricehistoryapp.com/latest-deals",
    ),
    (
        "PriceTrail",
        "https://pricehistorytracker.in/",
    ),
    (
        "PriceDropy",
        "https://pricedropy.com/",
    ),
    (
        "PriceDiff",
        "https://pricediff.in/top-deals/",
    ),
]


# We only want these two shopping sites.
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
        re.I,
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
            STATE_FILE.read_text()
        )
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            sort_keys=True,
        )
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
# FIND PRODUCT LINKS ON TRACKER PAGES
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

        path = parsed.path.lower()
        host = parsed.netloc.lower()

        if (
            "/product/" not in path
            and "/products/" not in path
        ):
            continue

        if href in seen:
            continue

        text = " ".join(
            link.stripped_strings
        )

        if not text:
            continue

        # Only accept links belonging to
        # our historical-price sources.
        if not any(
            domain in host
            for domain in (
                "pricehistoryapp.com",
                "pricehistorytracker.in",
                "pricedropy.com",
                "pricediff.in",
            )
        ):
            continue

        seen.add(href)

        found.append(
            (
                href,
                text,
            )
        )

    return found


def category_links_from_page(
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

        text = " ".join(
            link.stripped_strings
        )

        if (
            not text
            or href in seen
        ):
            continue

        path = urlparse(
            href
        ).path.lower()

        if (
            "/category/" in path
            or "/categories/" in path
        ):
            seen.add(href)
            links.append(href)

    return links


# ============================================================
# DISCOVER PRODUCTS
# ============================================================

def discover_candidates(
    source_name,
    start_url,
    max_candidates=250,
):
    candidates = []
    seen = set()

    try:
        soup = get_soup(
            start_url
        )
    except Exception as error:
        print(
            f"[{source_name}] "
            f"discovery failed: {error}"
        )
        return candidates

    def add_links(
        page_soup,
        page_url,
    ):
        for (
            href,
            anchor_text,
        ) in product_links_from_page(
            page_soup,
            page_url,
        ):

            if href in seen:
                continue

            store = normalise_store(
                anchor_text
            )

            if store is None:
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

            if (
                len(candidates)
                >= max_candidates
            ):
                return

    # First scan the main page.
    add_links(
        soup,
        start_url,
    )

    # Then scan category pages when available.
    if (
        len(candidates)
        < max_candidates
    ):
        categories = (
            category_links_from_page(
                soup,
                start_url,
            )
        )

        for category_url in categories[
            :30
        ]:

            try:
                category_soup = get_soup(
                    category_url
                )

                add_links(
                    category_soup,
                    category_url,
                )

            except Exception as error:
                print(
                    f"[{source_name}] "
                    f"category failed: "
                    f"{category_url}: "
                    f"{error}"
                )

            if (
                len(candidates)
                >= max_candidates
            ):
                break

            time.sleep(
                REQUEST_DELAY
            )

    return candidates


# ============================================================
# FIND DIRECT FLIPKART / MYNTRA BUTTON
# ============================================================

def extract_store_buy_link(
    soup,
    store,
):
    wanted = store.lower()

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

        href_lower = href.lower()

        if (
            store == "Flipkart"
            and "flipkart.com"
            in href_lower
        ):
            return href

        if (
            store == "Myntra"
            and "myntra.com"
            in href_lower
        ):
            return href

    return None


# ============================================================
# READ HISTORICAL PRICE DATA
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

    # --------------------------
    # Product title
    # --------------------------

    title = ""

    heading = soup.find("h1")

    if heading:
        title = " ".join(
            heading.stripped_strings
        )

    if not title:
        title = "Unknown product"

    # --------------------------
    # Store
    # --------------------------

    store = (
        expected_store
        or normalise_store(page_text)
    )

    if store not in (
        "Flipkart",
        "Myntra",
    ):
        return None

    # --------------------------
    # Current price
    # --------------------------

    current = None

    current_patterns = [
        r"Current\s*:?\s*"
        r"(?:₹|Rs\.?|INR)\s*"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)",

        r"Current price\s*"
        r"(?:₹|Rs\.?|INR)\s*"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)",
    ]

    for pattern in current_patterns:

        match = re.search(
            pattern,
            page_text,
            re.I,
        )

        if match:
            current = float(
                match.group(1)
                .replace(",", "")
            )
            break

    # Fallback:
    # Look around the H1.
    if (
        current is None
        and heading
    ):
        parent_text = " ".join(
            heading.parent.stripped_strings
        )

        prices = re.findall(
            r"(?:₹|Rs\.?|INR)\s*"
            r"[0-9][0-9,]*"
            r"(?:\.[0-9]+)?",
            parent_text,
            re.I,
        )

        if prices:
            current = money(
                prices[0]
            )

    # --------------------------
    # Historical average
    # --------------------------

    average = None
    average_days = None

    # Prefer 30-day average.
    patterns_30 = [
        r"30d Average\s*"
        r"(?:₹|Rs\.?|INR)\s*"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)",

        r"30-day average\s*"
        r"(?:₹|Rs\.?|INR)\s*"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)",

        r"30 day average\s*"
        r"(?:₹|Rs\.?|INR)\s*"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)",
    ]

    for pattern in patterns_30:

        match = re.search(
            pattern,
            page_text,
            re.I,
        )

        if match:
            average = float(
                match.group(1)
                .replace(",", "")
            )

            average_days = 30
            break

    # Fallback to 90-day average.
    if average is None:

        patterns_90 = [
            r"90d Average\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",

            r"90-day average\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",

            r"90 day average\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",
        ]

        for pattern in patterns_90:

            match = re.search(
                pattern,
                page_text,
                re.I,
            )

            if match:
                average = float(
                    match.group(1)
                    .replace(",", "")
                )

                average_days = 90
                break

    # Last fallback.
    if average is None:

        match = re.search(
            r"Average price\s*"
            r"(?:₹|Rs\.?|INR)\s*"
            r"([0-9][0-9,]*(?:\.[0-9]+)?)",
            page_text,
            re.I,
        )

        if match:
            average = float(
                match.group(1)
                .replace(",", "")
            )

            average_days = 0

    if (
        current is None
        or average is None
        or average <= 0
    ):
        return None

    # --------------------------
    # Direct shopping URL
    # --------------------------

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
# DUPLICATE PRODUCTS
# ============================================================

def product_key(item):

    title = re.sub(
        r"[^a-z0-9]+",
        " ",
        item["title"].lower(),
    ).strip()

    return (
        f"{item['store'].lower()}::"
        f"{title}"
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
    token = os.environ[
        "TELEGRAM_BOT_TOKEN"
    ]

    chat_id = os.environ[
        "TELEGRAM_CHAT_ID"
    ]

    current = item["current"]
    average = item["average"]

    drop = (
        1
        - current / average
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
            "tracked average"
        )

    text = (
        "🚨 <b>MEGA DEAL FOUND</b>\n\n"

        f"🛍 <b>{title}</b>\n"

        f"🏪 {item['store']}\n\n"

        f"💰 Current: "
        f"<b>₹{current:,.0f}</b>\n"

        f"📊 {average_label}: "
        f"<b>₹{average:,.0f}</b>\n"

        f"📉 Below average: "
        f"<b>{drop:.1f}%</b>\n"

        f"🔎 Found by: "
        f"{sources}\n\n"

        "✅ Under ₹5,000\n"
        "✅ At least 50% below historical average\n\n"

        "⚠️ Verify seller, variant and "
        "final checkout price."
    )

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,

        "reply_markup": {
            "inline_keyboard": [

                [
                    {
                        "text": (
                            f"🛒 BUY ON "
                            f"{item['store'].upper()}"
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
        (
            "https://api.telegram.org/"
            f"bot{token}/sendMessage"
        ),
        json=payload,
        timeout=TIMEOUT,
    )

    response.raise_for_status()


# ============================================================
# MAIN SCANNER
# ============================================================

def main():

    if not os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    ):
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN"
        )

    if not os.environ.get(
        "TELEGRAM_CHAT_ID"
    ):
        raise SystemExit(
            "Missing TELEGRAM_CHAT_ID"
        )

    state = load_state()

    all_candidates = []

    # ----------------------------------------
    # STEP 1 — Discover products from sources
    # ----------------------------------------

    for (
        source_name,
        source_url,
    ) in SOURCE_PAGES:

        found = discover_candidates(
            source_name,
            source_url,
        )

        print(
            f"[{source_name}] "
            f"found {len(found)} "
            f"Flipkart/Myntra candidates"
        )

        all_candidates.extend(
            found
        )

    # ----------------------------------------
    # STEP 2 — Remove duplicate tracker URLs
    # ----------------------------------------

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

    # ----------------------------------------
    # STEP 3 — Read historical data
    # ----------------------------------------

    grouped = {}

    scanned = 0
    errors = 0

    for candidate in candidates:

        try:

            item = parse_product_page(
                candidate[
                    "tracker_url"
                ],
                candidate["store"],
            )

        except Exception as error:

            errors += 1

            print(
                "ERROR reading "
                f"{candidate['tracker_url']}: "
                f"{error}"
            )

            continue

        if not item:
            continue

        scanned += 1

        # Don't waste further processing
        # on products already above ₹5,000.
        if (
            item["current"]
            > MAX_PRICE
        ):
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
            candidate["source"]
        )

        # Prefer an exact 30-day average
        # over a 90-day fallback.
        old_item = grouped[key][
            "item"
        ]

        if (
            item["average_days"]
            == 30
            and old_item[
                "average_days"
            ] != 30
        ):
            grouped[key][
                "item"
            ] = item

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

    # ----------------------------------------
    # STEP 4 — Apply deal rule
    # ----------------------------------------

    qualified = 0
    sent = 0

    for group in grouped.values():

        item = group["item"]
        evidence = group[
            "evidence"
        ]

        current = item[
            "current"
        ]

        average = item[
            "average"
        ]

        qualifies = (
            current <= MAX_PRICE
            and current
            <= average * DROP_RATIO
        )

        if not qualifies:
            continue

        qualified += 1

        state_key = (
            item["tracker_url"]
            .split("?")[0]
        )

        now = int(
            time.time()
        )

        previous = state.get(
            state_key
        )

        # Don't repeatedly send the same
        # deal for seven days unless its
        # price becomes lower.
        if (
            previous
            and previous.get(
                "price",
                10**12,
            ) <= current
            and now
            - previous.get(
                "time",
                0,
            )
            < 7 * 86400
        ):
            continue

        send_telegram(
            item,
            evidence,
        )

        state[state_key] = {
            "price": current,
            "time": now,
        }

        sent += 1

        time.sleep(0.4)

    save_state(state)

    print(
        "\n=============================="
    )

    print(
        f"Total candidates: "
        f"{len
