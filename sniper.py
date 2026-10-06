import os
import re
import json
import html
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

MAX_PRICE = 5000
MAX_AVERAGE_RATIO = 0.50
TARGET_PRODUCTS = 1000
WORKERS = 10
REQUEST_TIMEOUT = 12
RETAILER_TIMEOUT = 10
SUSPICIOUS_PRICE = 100

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = "state.json"

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 Chrome/131 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


def fetch(url, timeout=REQUEST_TIMEOUT):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ""


def clean_url(url):
    return url.split("#")[0].split("?")[0].rstrip("/")


def retailer(url):
    host = urlparse(url).netloc.lower()

    if "flipkart.com" in host:
        return "Flipkart"

    if "myntra.com" in host:
        return "Myntra"

    return ""


def is_retailer_product(url):
    return retailer(url) != ""


def is_tracker_page(url):
    p = urlparse(url).path.lower()

    return any(x in p for x in [
        "/product/",
        "/products/",
        "/price-history/",
        "/track/",
        "/tracker/",
        "/price-history"
    ])


def money(value):
    if value is None:
        return None

    value = str(value).replace(",", "").replace("₹", "").strip()

    m = re.search(r"(\d+(?:\.\d+)?)", value)

    if not m:
        return None

    try:
        return float(m.group(1))
    except Exception:
        return None


def price_from_string(text):
    if not text:
        return None

    patterns = [
        r"₹\s*([\d,]+(?:\.\d+)?)",
        r"Rs\.?\s*([\d,]+(?:\.\d+)?)",
        r"INR\s*([\d,]+(?:\.\d+)?)",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, re.I)

        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except Exception:
                pass

    return None


def get_json_ld(soup):
    data = []

    for script in soup.find_all(
        "script",
        type="application/ld+json"
    ):
        try:
            obj = json.loads(script.string or script.get_text())

            if isinstance(obj, list):
                data.extend(obj)
            else:
                data.append(obj)

        except Exception:
            pass

    return data


def structured_current_price(soup):
    for obj in get_json_ld(soup):

        if not isinstance(obj, dict):
            continue

        offers = obj.get("offers")

        if isinstance(offers, list):
            offers = offers[0] if offers else None

        if isinstance(offers, dict):
            p = money(offers.get("price"))

            if p:
                return p

        p = money(obj.get("price"))

        if p:
            return p

    for tag in soup.find_all(
        ["meta", "span", "div"],
        attrs={
            "itemprop": "price"
        }
    ):
        p = money(
            tag.get("content") or tag.get_text(" ", strip=True)
        )

        if p:
            return p

    return None


def all_currency_prices(soup):
    prices = []

    for tag in soup.find_all(
        ["meta", "span", "div", "p", "strong"]
    ):
        text = tag.get("content") or tag.get_text(
            " ", strip=True
        )

        if "₹" not in text:
            continue

        p = price_from_string(text)

        if p:
            prices.append(p)

    return prices


def extract_title(soup):
    for obj in get_json_ld(soup):

        if isinstance(obj, dict) and obj.get("name"):
            return html.unescape(
                str(obj["name"])
            ).strip()

    og = soup.find(
        "meta",
        property="og:title"
    )

    if og and og.get("content"):
        return html.unescape(
            og["content"]
        ).strip()

    if soup.title:
        return soup.title.get_text(
            " ",
            strip=True
        )

    return "Unknown product"


def extract_average(soup):
    text = soup.get_text(
        " ",
        strip=True
    )

    patterns = [
        r"(?:30|90|180|365)[-\s]?day\s+average[^₹]*₹\s*([\d,]+(?:\.\d+)?)",
        r"historical\s+average[^₹]*₹\s*([\d,]+(?:\.\d+)?)",
        r"average\s+price[^₹]*₹\s*([\d,]+(?:\.\d+)?)",
        r"average[^₹]*₹\s*([\d,]+(?:\.\d+)?)",
    ]

    for pattern in patterns:
        m = re.search(
            pattern,
            text,
            re.I
        )

        if m:
            try:
                return float(
                    m.group(1).replace(",", "")
                )
            except Exception:
                pass

    return None


def find_buy_url(soup, base_url):
    candidates = []

    for a in soup.find_all("a", href=True):

        href = urljoin(
            base_url,
            a["href"]
        )

        if not is_retailer_product(href):
            continue

        candidates.append(href)

        text = a.get_text(
            " ",
            strip=True
        ).lower()

        if any(x in text for x in [
            "buy",
            "shop",
            "view",
            "flipkart",
            "myntra"
        ]):
            return href

    return candidates[0] if candidates else ""


def extract_retailer_price(url):
    page = fetch(
        url,
        timeout=RETAILER_TIMEOUT
    )

    if not page:
        return None

    soup = BeautifulSoup(
        page,
        "html.parser"
    )

    p = structured_current_price(soup)

    if p and p > 0:
        return p

    return None


def validate_current_price(
    tracker_price,
    retailer_price,
    average
):
    if not tracker_price:
        return None

    if retailer_price:

        difference = abs(
            tracker_price - retailer_price
        ) / max(
            tracker_price,
            retailer_price
        )

        if difference <= 0.15:
            return retailer_price

        return retailer_price

    if tracker_price < SUSPICIOUS_PRICE:
        return None

    if (
        average
        and tracker_price / average < 0.05
    ):
        return None

    return tracker_price


def parse_tracker(url):
    page = fetch(url)

    if not page:
        return None

    soup = BeautifulSoup(
        page,
        "html.parser"
    )

    title = extract_title(soup)
    average = extract_average(soup)
    tracker_price = structured_current_price(soup)
    buy_url = find_buy_url(
        soup,
        url
    )

    if not buy_url:
        return None

    store = retailer(buy_url)

    if store not in [
        "Flipkart",
        "Myntra"
    ]:
        return None

    retailer_price = extract_retailer_price(
        buy_url
    )

    current = validate_current_price(
        tracker_price,
        retailer_price,
        average
    )

    if not current or not average:
        return None

    if current <= 0 or average <= 0:
        return None

    return {
        "title": title,
        "store": store,
        "current": current,
        "average": average,
        "buy_url": buy_url,
    }


def extract_links(page, base_url):
    soup = BeautifulSoup(
        page,
        "html.parser"
    )

    links = set()

    for a in soup.find_all(
        "a",
        href=True
    ):
        href = urljoin(
            base_url,
            a["href"]
        )

        if is_tracker_page(href):
            links.add(clean_url(href))

    return list(links)


def discover():
    products = []
    seen = set()

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
            print("  Fetch failed")
            continue

        links = extract_links(
            page,
            source
        )

        new = 0

        for link in links:

            if link in seen:
                continue

            seen.add(link)
            products.append(link)
            new += 1

            if len(products) >= TARGET_PRODUCTS:
                break

        print(
            "  New:",
            new,
            "Total:",
            len(products)
        )

        if len(products) >= TARGET_PRODUCTS:
            break

    return products


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing")
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": False,
            },
            timeout=15
        )

        return r.ok

    except Exception as e:
        print("Telegram error:", e)
        return False


def load_state():
    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception:
        return {}


def save_state(state):
    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            state,
            f,
            indent=2
        )


def main():

    products = discover()

    print()
    print("==========================================")
    print("PRODUCT CHECK")
    print("==========================================")

    print(
        "Products discovered:",
        len(products)
    )

    results = []

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        futures = {
            executor.submit(
                parse_tracker,
                url
            ): url
            for url in products
        }

        for i, future in enumerate(
            as_completed(futures),
            1
        ):

            try:
                result = future.result()

                if result:

                    results.append(result)

                    print(
                        f"[{i}/{len(products)}] "
                        f"{result['store']} | "
                        f"{result['title'][:55]} | "
                        f"₹{result['current']:,.0f} | "
                        f"avg ₹{result['average']:,.0f}"
                    )

            except Exception as e:

                print(
                    f"[{i}/{len(products)}] Error:",
                    e
                )

    qualified = []

    for item in results:

        current = item["current"]
        average = item["average"]

        if (
            current <= MAX_PRICE
            and average > 0
            and current / average <= MAX_AVERAGE_RATIO
        ):

            item["discount_from_average"] = (
                1 - current / average
            ) * 100

            qualified.append(item)

    print()
    print("==========================================")
    print("RESULTS")
    print("==========================================")

    print(
        "Products checked:",
        len(products)
    )

    print(
        "Valid products:",
        len(results)
    )

    print(
        "Qualified deals:",
        len(qualified)
    )

    state = load_state()
    sent = 0

    for item in qualified:

        key = (
            item["store"]
            + "|"
            + item["buy_url"]
        )

        if key in state:
            continue

        message = (
            "🔥 VERIFIED PRICE DEAL\n\n"
            f"🛍️ {item['title']}\n\n"
            f"🏪 Store: {item['store']}\n"
            f"💰 Current: ₹{item['current']:,.0f}\n"
            f"📊 Historical average: "
            f"₹{item['average']:,.0f}\n"
            f"📉 Below average: "
            f"{item['discount_from_average']:.1f}%\n\n"
            "✅ Current price validated\n"
            "✅ ≤ ₹5,000\n"
            "✅ ≤ 50% of historical average\n\n"
            f"🛒 BUY NOW:\n"
            f"{item['buy_url']}"
        )

        if send_telegram(message):

            state[key] = {
                "title": item["title"],
                "current": item["current"],
                "average": item["average"],
                "sent_at": int(time.time())
            }

            sent += 1

            print(
                "Alert sent:",
                item["title"][:60]
            )

    save_state(state)

    print()
    print("==========================================")
    print("DONE")
    print("==========================================")

    print(
        "Alerts sent:",
        sent
    )


if __name__ == "__main__":
    main()
