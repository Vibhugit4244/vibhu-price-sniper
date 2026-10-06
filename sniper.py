import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SOURCE = "https://pricehistoryapp.com/flipkart-price-history"
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

def money(s):
    if not s:
        return None
    m = re.search(r"(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", s, re.I)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))

def pct(s):
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    return float(m.group(1)) if m else None

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))

def fetch():
    r = requests.get(SOURCE, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def extract_cards(soup):
    """
    PriceHistory currently publishes a live Flipkart price-drop list.
    This parser intentionally uses visible text rather than private endpoints.
    Because public HTML can change, it has several fallback patterns.
    """
    cards = []

    # Prefer article / card-like containers.
    candidates = soup.select("article, [class*='card'], [class*='product'], [class*='deal']")
    seen = set()

    for node in candidates:
        text = " ".join(node.stripped_strings)
        if "Flipkart" not in text and "₹" not in text:
            continue

        link = node.find("a", href=True)
        href = urljoin(SOURCE, link["href"]) if link else None
        if not href:
            continue

        prices = [money(x) for x in re.findall(r"(?:₹|Rs\.?|INR)\s*[0-9][0-9,]*(?:\.[0-9]+)?", text, re.I)]
        prices = [x for x in prices if x is not None]

        if len(prices) < 2:
            continue

        # Usually first is current and second is reference/original.
        current = prices[0]
        reference = prices[1]

        if current <= 0 or reference <= 0:
            continue

        title = ""
        h = node.find(["h1", "h2", "h3", "h4", "h5"])
        if h:
            title = " ".join(h.stripped_strings)

        if not title:
            title = text[:180]

        key = href.split("?")[0]
        if key in seen:
            continue
        seen.add(key)

        cards.append({
            "title": title,
            "url": href,
            "current": current,
            "reference": reference,
            "discount": (1 - current / reference) * 100,
            "raw": text[:1000],
        })

    return cards

def telegram(method, payload):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def send_alert(item):
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    current = item["current"]
    ref = item["reference"]
    drop = (1 - current / ref) * 100

    text = (
        "🚨 <b>MEGA FLIPKART DEAL</b>\n\n"
        f"🛍 <b>{escape(item['title'])}</b>\n\n"
        f"💰 Current: <b>₹{current:,.0f}</b>\n"
        f"📊 Reference: <b>₹{ref:,.0f}</b>\n"
        f"📉 Below reference: <b>{drop:.1f}%</b>\n\n"
        "✅ Under ₹5,000\n"
        "✅ At least 50% below reference\n\n"
        "⚠️ Verify seller, variant, delivery and final checkout price on Flipkart."
    )

    telegram("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "🛒 BUY ON FLIPKART", "url": item["url"]}]
            ]
        },
    })

def escape(s):
    return (
        str(s).replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def main():
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
    if not os.environ.get("TELEGRAM_CHAT_ID"):
        raise SystemExit("Missing TELEGRAM_CHAT_ID")

    state = load_state()
    soup = fetch()
    cards = extract_cards(soup)

    qualified = []
    for x in cards:
        if x["current"] <= MAX_PRICE and x["current"] <= x["reference"] * DROP_RATIO:
            qualified.append(x)

    sent = 0
    now = int(time.time())

    for item in qualified:
        key = item["url"].split("?")[0]
        old = state.get(key)

        # Alert only when this deal hasn't been alerted recently at the same/lower price.
        if old and old.get("price") <= item["current"] and now - old.get("time", 0) < 7 * 86400:
            continue

        send_alert(item)
        state[key] = {"price": item["current"], "time": now}
        sent += 1

    # Keep state bounded.
    if len(state) > 3000:
        state = dict(sorted(state.items(), key=lambda kv: kv[1].get("time", 0), reverse=True)[:2000])

    save_state(state)
    print(f"Scanned {len(cards)} cards; {len(qualified)} qualified; {sent} alerts sent.")

if __name__ == "__main__":
    main()
