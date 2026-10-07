import os
import re
import json
import time
from urllib.parse import quote, urlparse

import requests
from playwright.sync_api import sync_playwright


# =========================
# SETTINGS
# =========================

MAX_PRICE = 8000
MIN_DROP_PERCENT = 45
BATCH_SIZE = 50
RECHECK_HOURS = 24
STATE_FILE = "state.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# =========================
# SEARCHES
# =========================

FLIPKART_SEARCHES = [
    "men shirts",
    "men t shirts",
    "men jeans",
    "men trousers",
    "men pants",
    "women tops",
    "women shirts",
    "women t shirts",
    "women dresses",
    "women jeans",
    "women trousers",
    "women pants",
    "shoes",
    "sneakers",
    "sports shoes",
    "sandals",
    "flats",
    "heels",
]

MYNTRA_SEARCHES = [
    "men-shirts",
    "men-tshirts",
    "men-jeans",
    "men-trousers",
    "men-pants",
    "women-tops",
    "women-shirts",
    "women-tshirts",
    "women-dresses",
    "women-jeans",
    "women-trousers",
    "women-pants",
    "men-shoes",
    "women-shoes",
    "sneakers",
    "sports-shoes",
    "sandals",
    "flats",
    "heels",
]


# =========================
# STATE
# =========================

def load_state():
    """
    Loads both the new state format and old formats.
    This prevents crashes caused by older state.json files.
    """

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}

    if not isinstance(state, dict):
        state = {}

    queue = state.get("queue", {})
    checked = state.get("checked", {})
    sent = state.get("sent", [])

    # Old queue may be a LIST.
    if isinstance(queue, list):
        new_queue = {}

        for item in queue:
            if isinstance(item, str):
                url = item
                store = detect_store(url)

                if store:
                    new_queue[url] = {
                        "url": url,
                        "store": store,
                        "added": time.time(),
                    }

            elif isinstance(item, dict):
                url = item.get("url")

                if url:
                    store = item.get("store") or detect_store(url)

                    if store:
                        new_queue[url] = {
                            "url": url,
                            "store": store,
                            "added": item.get("added", time.time()),
                        }

        queue = new_queue

    elif not isinstance(queue, dict):
        queue = {}

    if not isinstance(checked, dict):
        checked = {}

    if not isinstance(sent, list):
        sent = []

    state["queue"] = queue
    state["checked"] = checked
    state["sent"] = sent

    return state


def save_state(state):
    temp = STATE_FILE + ".tmp"

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    os.replace(temp, STATE_FILE)


# =========================
# HELPERS
# =========================

def detect_store(url):
    host = urlparse(url).netloc.lower()

    if "flipkart.com" in host:
        return "flipkart"

    if "myntra.com" in host:
        return "myntra"

    return None


def clean_url(url):
    try:
        p = urlparse(url)

        if not p.scheme or not p.netloc:
            return None

        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")

    except Exception:
        return None


def valid_product_url(url):
    url = clean_url(url)

    if not url:
        return False

    store = detect_store(url)

    if store == "flipkart":
        return "/p/" in urlparse(url).path

    if store == "myntra":
        return "/buy/" in urlparse(url).path

    return False


def parse_price(value):
    if value is None:
        return None

    text = str(value)
    text = text.replace(",", "")

    match = re.search(r"(?:₹|Rs\.?\s*)?(\d+(?:\.\d{1,2})?)", text)

    if not match:
        return None

    try:
        price = float(match.group(1))

        if 1 <= price <= 1000000:
            return price

    except Exception:
        pass

    return None


# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM: secrets missing")
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": False,
            },
            timeout=20,
        )

        response.raise_for_status()
        return True

    except Exception as e:
        print("TELEGRAM ERROR:", str(e)[:200])
        return False


# =========================
# DISCOVERY
# =========================

def add_products_from_page(page, store, search_url, state):
    try:
        page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=35000,
        )

        page.wait_for_timeout(1800)

        links = page.locator("a[href]").evaluate_all(
            "(els) => els.map(a => a.href)"
        )

        added = 0

        for raw_url in links:
            url = clean_url(raw_url)

            if not url:
                continue

            if detect_store(url) != store:
                continue

            if not valid_product_url(url):
                continue

            if url in state["queue"]:
                continue

            state["queue"][url] = {
                "url": url,
                "store": store,
                "added": time.time(),
            }

            added += 1

        return added

    except Exception as e:
        print("DISCOVERY ERROR:", str(e)[:180])
        return 0


def discover(page, state):
    total = 0

    for term in FLIPKART_SEARCHES:
        url = (
            "https://www.flipkart.com/search?q="
            + quote(term)
        )

        added = add_products_from_page(
            page,
            "flipkart",
            url,
            state,
        )

        total += added
        print("FLIPKART:", term, "+", added)

        save_state(state)

    for term in MYNTRA_SEARCHES:
        url = "https://www.myntra.com/" + term

        added = add_products_from_page(
            page,
            "myntra",
            url,
            state,
        )

        total += added
        print("MYNTRA:", term, "+", added)

        save_state(state)

    print("NEW PRODUCTS:", total)
    print("TOTAL QUEUE:", len(state["queue"]))


# =========================
# LIVE RETAILER PRICE
# =========================

def get_jsonld_price(page):
    try:
        scripts = page.locator(
            'script[type="application/ld+json"]'
        ).all_text_contents()

        for raw in scripts:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            objects = []

            if isinstance(data, list):
                objects.extend(data)
            elif isinstance(data, dict):
                objects.append(data)

                graph = data.get("@graph")

                if isinstance(graph, list):
                    objects.extend(graph)

            for obj in objects:
                if not isinstance(obj, dict):
                    continue

                object_type = obj.get("@type")

                if isinstance(object_type, list):
                    is_product = "Product" in object_type
                else:
                    is_product = object_type == "Product"

                if not is_product:
                    continue

                offers = obj.get("offers")

                if isinstance(offers, dict):
                    offers = [offers]

                if not isinstance(offers, list):
                    continue

                prices = []

                for offer in offers:
                    if not isinstance(offer, dict):
                        continue

                    price = parse_price(offer.get("price"))

                    if price:
                        prices.append(price)

                if prices:
                    return min(prices)

    except Exception:
        pass

    return None


def get_meta_price(page):
    selectors = [
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[itemprop="price"]',
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector).first

            if locator.count():
                value = (
                    locator.get_attribute("content")
                    or locator.get_attribute("value")
                )

                price = parse_price(value)

                if price:
                    return price

        except Exception:
            pass

    return None


def get_retailer_price(page, store, url):
    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=35000,
        )

        page.wait_for_timeout(1500)

        # First choice: structured product data.
        price = get_jsonld_price(page)

        if price and price < MAX_PRICE:
            return price

        # Second choice: product price metadata.
        price = get_meta_price(page)

        if price and price < MAX_PRICE:
            return price

    except Exception as e:
        print("LIVE PRICE ERROR:", str(e)[:180])

    return None


# =========================
# PRICEHISTORYAPP
# =========================

def find_history_values(text):
    """
    Extracts the 30-day average and all-time low
    only from labelled sections.
    """

    average_patterns = [
        r"30\s*day\s*average.{0,80}?₹\s*([\d,]+(?:\.\d+)?)",
        r"30\s*day\s*avg.{0,80}?₹\s*([\d,]+(?:\.\d+)?)",
        r"30d\s*average.{0,80}?₹\s*([\d,]+(?:\.\d+)?)",
    ]

    low_patterns = [
        r"all[\s-]*time\s*low.{0,80}?₹\s*([\d,]+(?:\.\d+)?)",
        r"all[\s-]*time\s*lowest.{0,80}?₹\s*([\d,]+(?:\.\d+)?)",
    ]

    average = None
    low = None

    for pattern in average_patterns:
        match = re.search(pattern, text, re.I | re.S)

        if match:
            average = parse_price(match.group(1))
            break

    for pattern in low_patterns:
        match = re.search(pattern, text, re.I | re.S)

        if match:
            low = parse_price(match.group(1))
            break

    return average, low


def history_lookup(page, retailer_url):
    try:
        page.goto(
            "https://pricehistoryapp.com/",
            wait_until="domcontentloaded",
            timeout=35000,
        )

        page.wait_for_timeout(1200)

        inputs = page.locator("input")

        if inputs.count() == 0:
            return None

        target = None

        for i in range(inputs.count()):
            inp = inputs.nth(i)

            placeholder = (
                inp.get_attribute("placeholder") or ""
            ).lower()

            aria = (
                inp.get_attribute("aria-label") or ""
            ).lower()

            input_type = (
                inp.get_attribute("type") or ""
            ).lower()

            combined = (
                placeholder + " " + aria + " " + input_type
            )

            if (
                "url" in combined
                or "link" in combined
                or "product" in combined
            ):
                target = inp
                break

        if target is None:
            target = inputs.first

        target.fill(retailer_url)

        buttons = page.locator("button")

        clicked = False

        for i in range(buttons.count()):
            button = buttons.nth(i)

            try:
                text = button.inner_text().strip().lower()
            except Exception:
                text = ""

            if (
                "track" in text
                or "check" in text
                or "search" in text
            ):
                button.click()
                clicked = True
                break

        if not clicked:
            target.press("Enter")

        page.wait_for_timeout(4000)

        # If the homepage returned a product-history link,
        # follow it.
        if "/product/" not in page.url:
            links = page.locator('a[href*="/product/"]')

            if links.count():
                href = links.first.get_attribute("href")

                if href:
                    if href.startswith("/"):
                        href = (
                            "https://pricehistoryapp.com"
                            + href
                        )

                    page.goto(
                        href,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )

                    page.wait_for_timeout(1200)

        if "/product/" not in page.url:
            return None

        text = page.locator("body").inner_text(
            timeout=10000
        )

        average, low = find_history_values(text)

        if not average:
            return None

        return {
            "avg30": average,
            "low": low,
            "history_url": page.url,
        }

    except Exception as e:
        print("HISTORY ERROR:", str(e)[:180])
        return None


# =========================
# DEAL CALCULATION
# =========================

def deal_message(store, current, average, low, drop, url, history_url):
    if drop >= 75:
        title = "💥 UNPRECEDENTED DEAL"
    elif drop >= 65:
        title = "🔥 EXCEPTIONAL DEAL"
    elif drop >= 55:
        title = "🚨 VERY STRONG DEAL"
    else:
        title = "⚡ STRONG DEAL"

    low_text = (
        f"₹{low:,.0f}"
        if low
        else "Unavailable"
    )

    return (
        f"{title}\n\n"
        f"Store: {store.upper()}\n"
        f"Current price: ₹{current:,.0f}\n"
        f"30-day average: ₹{average:,.0f}\n"
        f"Below 30-day average: {drop:.1f}%\n"
        f"All-time low: {low_text}\n\n"
        f"🛒 Buy: {url}\n"
        f"📊 History: {history_url}"
    )


# =========================
# MAIN
# =========================

def main():
    state = load_state()

    print("===================================")
    print("VIBHU PRICE SNIPER")
    print("===================================")
    print("Queue:", len(state["queue"]))

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            viewport={
                "width": 1365,
                "height": 900,
            },
            locale="en-IN",
        )

        page = context.new_page()

        # -------------------------
        # DISCOVERY
        # -------------------------

        discover(page, state)

        # -------------------------
        # SELECT PRODUCTS TO CHECK
        # -------------------------

        now = time.time()

        ready = []

        for url, item in state["queue"].items():

            last_checked = state["checked"].get(
                url,
                0
            )

            if (
                now - last_checked
                >= RECHECK_HOURS * 3600
            ):
                ready.append((url, item))

        print("READY:", len(ready))

        batch = ready[:BATCH_SIZE]

        print("THIS RUN:", len(batch))

        # -------------------------
        # CHECK PRODUCTS
        # -------------------------

        for number, (url, item) in enumerate(
            batch,
            start=1
        ):

            store = item.get("store") or detect_store(url)

            print()
            print(
                f"[{number}/{len(batch)}]"
                f" {store.upper()}"
            )
            print(url)

            # ---- LIVE PRICE ----

            current = get_retailer_price(
                page,
                store,
                url,
            )

            if not current:

                print(
                    "SKIP → Could not confidently"
                    " identify live price"
                )

                state["checked"][url] = now
                save_state(state)
                continue

            print(
                f"LIVE PRICE → ₹{current:,.0f}"
            )

            # Price must be strictly below ₹8,000.
            if current >= MAX_PRICE:

                print(
                    "SKIP → Price is ₹8,000 or higher"
                )

                state["checked"][url] = now
                save_state(state)
                continue

            # ---- PRICE HISTORY ----

            history = history_lookup(
                page,
                url,
            )

            if not history:

                print(
                    "SKIP → Exact history unavailable"
                )

                state["checked"][url] = now
                save_state(state)
                continue

            average = history["avg30"]
            low = history["low"]

            drop = (
                (average - current)
                / average
                * 100
            )

            print(
                f"30D AVG → ₹{average:,.0f}"
            )

            print(
                f"DROP → {drop:.1f}%"
            )

            if low:
                print(
                    f"ALL-TIME LOW → ₹{low:,.0f}"
                )

            # Mark checked BEFORE sending.
            state["checked"][url] = now
            save_state(state)

            # -------------------------
            # DEAL RULE
            # -------------------------

            if drop < MIN_DROP_PERCENT:

                print("NO DEAL")

                continue

            # -------------------------
            # SEND IMMEDIATELY
            # -------------------------

            message = deal_message(
                store,
                current,
                average,
                low,
                drop,
                url,
                history["history_url"],
            )

            if send_telegram(message):

                print("🚨 ALERT SENT")

                if url not in state["sent"]:
                    state["sent"].append(url)

                save_state(state)

            else:

                print(
                    "ALERT FAILED → product remains"
                    " eligible for future checking"
                )

        browser.close()

    save_state(state)

    print()
    print("===================================")
    print("RUN FINISHED")
    print("QUEUE:", len(state["queue"]))
    print("===================================")


if __name__ == "__main__":
    main()