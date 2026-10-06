import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

DEALS_URL = "https://pricehistoryapp.com/deals/store/flipkart"

MAX_PRICE = 5000
DROP_RATIO = 0.50

STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


def money(text):
    if not text:
        return None

    match = re.search(
        r"(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        text,
        re.I,
    )

    if not match:
        return None

    return float(match.group(1).replace(",", ""))


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True)
    )


def get_soup(url):
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
    )

    response.raise_for_status()

    return BeautifulSoup(
        response.text,
        "html.parser",
    )


def extract_deal_links(soup):
    deals = []
    seen = set()

    for link in soup.find_all("a", href=True):

        href = urljoin(DEALS_URL, link["href"])

        if "/product/" not in href:
            continue

        if href in seen:
            continue

        text = " ".join(link.stripped_strings)

        if not text:
            continue

        seen.add(href)

        deals.append({
            "url": href,
            "text": text,
        })

    return deals


def extract_product(product_url):
    soup = get_soup(product_url)

    page_text = " ".join(soup.stripped_strings)

    title = ""

    heading = soup.find("h1")

    if heading:
        title = " ".join(
            heading.stripped_strings
        )

    if not title:
        title = "Flipkart product"

    # Current price
    current = None

    # Look for the price near the product title.
    # PriceHistory pages contain the current price
    # immediately before the MRP.

    title_node = soup.find("h1")

    if title_node:
        parent_text = " ".join(
            title_node.parent.stripped_strings
        )

        prices = re.findall(
            r"(?:₹|Rs\.?|INR)\s*[0-9][0-9,]*(?:\.[0-9]+)?",
            parent_text,
            re.I,
        )

        if prices:
            current = money(prices[0])

    # Fallback: use the page's Current: ₹... value.
    if current is None:
        match = re.search(
            r"Current:\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
            page_text,
            re.I,
        )

        if match:
            current = float(
                match.group(1).replace(",", "")
            )

    # PriceHistory currently exposes a 30-day average.
    average = None

    match = re.search(
        r"30d Average\s*(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        page_text,
        re.I,
    )

    if match:
        average = float(
            match.group(1).replace(",", "")
        )

    if current is None or average is None:
        return None

    return {
        "title": title,
        "url": product_url,
        "current": current,
        "average": average,
    }


def telegram(method, payload):
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    url = (
        f"https://api.telegram.org/"
        f"bot{token}/{method}"
    )

    response = requests.post(
        url,
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


def escape_html(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_alert(item):
    current = item["current"]
    average = item["average"]

    drop = (
        1 - (current / average)
    ) * 100

    text = (
        "🚨 <b>MEGA FLIPKART DEAL</b>\n\n"
        f"🛍 <b>{escape_html(item['title'])}</b>\n\n"
        f"💰 Current: <b>₹{current:,.0f}</b>\n"
        f"📊 30-day average: <b>₹{average:,.0f}</b>\n"
        f"📉 Below average: <b>{drop:.1f}%</b>\n\n"
        "✅ Under ₹5,000\n"
        "✅ At least 50% below average\n\n"
        "⚠️ Verify seller, variant and final checkout price."
    )

    telegram(
        "sendMessage",
        {
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "🛒 BUY ON FLIPKART",
                            "url": item["url"],
                        }
                    ]
                ]
            },
        },
    )


def main():

    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN"
        )

    if not os.environ.get("TELEGRAM_CHAT_ID"):
        raise SystemExit(
            "Missing TELEGRAM_CHAT_ID"
        )

    state = load_state()

    soup = get_soup(DEALS_URL)

    deals = extract_deal_links(soup)

    scanned = 0
    qualified = 0
    sent = 0

    for deal in deals:

        try:
            item = extract_product(
                deal["url"]
            )

        except Exception as error:
            print(
                f"ERROR reading {deal['url']}: {error}"
            )
            continue

        if not item:
            continue

        scanned += 1

        current = item["current"]
        average = item["average"]

        print(
            f"{item['title']} | "
            f"₹{current:,.0f} | "
            f"30d avg ₹{average:,.0f}"
        )

        # YOUR EXACT DEAL RULE
        if (
            current <= MAX_PRICE
            and current <= average * DROP_RATIO
        ):
            qualified += 1

            key = item["url"].split("?")[0]

            old = state.get(key)

            now = int(time.time())

            if (
                old
                and old.get("price") <= current
                and now - old.get("time", 0)
                < 7 * 86400
            ):
                continue

            send_alert(item)

            state[key] = {
                "price": current,
                "time": now,
            }

            sent += 1

        # Small pause so we don't hammer the website.
        time.sleep(0.5)

    save_state(state)

    print(
        f"Scanned {scanned} products; "
        f"{qualified} qualified; "
        f"{sent} alerts sent."
    )


if __name__ == "__main__":
    main()
