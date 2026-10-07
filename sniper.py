import os
import re
import json
import time
import traceback
from urllib.parse import quote, urlparse

import requests
from playwright.sync_api import sync_playwright


MAX_PRICE = 8000
MIN_DROP = 45
BATCH_SIZE = 50
RECHECK_HOURS = 24
STATE_FILE = "state.json"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


FLIPKART_TERMS = [
    "men shirts", "men t shirts", "men jeans", "men trousers",
    "men pants", "women tops", "women shirts", "women t shirts",
    "women dresses", "women jeans", "women trousers", "women pants",
    "shoes", "sneakers", "sports shoes", "sandals", "flats", "heels"
]

MYNTRA_TERMS = [
    "men-shirts", "men-tshirts", "men-jeans", "men-trousers",
    "men-pants", "women-tops", "women-shirts", "women-tshirts",
    "women-dresses", "women-jeans", "women-trousers", "women-pants",
    "men-shoes", "women-shoes", "sneakers", "sports-shoes",
    "sandals", "flats", "heels"
]


def store_of(url):
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


def product_url(url):
    u = clean_url(url)
    if not u:
        return False
    s = store_of(u)
    if s == "flipkart":
        return "/p/" in urlparse(u).path
    if s == "myntra":
        return "/buy/" in urlparse(u).path
    return False


def price(value):
    if value is None:
        return None
    try:
        m = re.search(
            r"(?:₹|Rs\.?\s*)?\s*([\d,]+(?:\.\d+)?)",
            str(value)
        )
        if not m:
            return None
        n = float(m.group(1).replace(",", ""))
        if 1 <= n <= 1000000:
            return n
    except Exception:
        pass
    return None


# --------------------------------------------------
# STATE
# --------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}

    if not isinstance(raw, dict):
        raw = {}

    q = raw.get("queue", {})
    checked = raw.get("checked", {})
    sent = raw.get("sent", [])

    new_q = {}

    if isinstance(q, dict):
        for k, v in q.items():
            if isinstance(v, dict):
                u = v.get("url") or k
            else:
                u = k

            if u and product_url(u):
                new_q[u] = {
                    "url": u,
                    "store": store_of(u),
                    "added": (
                        v.get("added", time.time())
                        if isinstance(v, dict)
                        else time.time()
                    )
                }

    elif isinstance(q, list):
        for item in q:
            if isinstance(item, str):
                u = item
            elif isinstance(item, dict):
                u = item.get("url")
            else:
                continue

            if u and product_url(u):
                new_q[u] = {
                    "url": u,
                    "store": store_of(u),
                    "added": time.time()
                }

    if not isinstance(checked, dict):
        checked = {}

    if not isinstance(sent, list):
        sent = []

    return {
        "queue": new_q,
        "checked": checked,
        "sent": sent
    }


def save_state(state):
    try:
        with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e:
        print("STATE SAVE ERROR:", repr(e))


# --------------------------------------------------
# TELEGRAM
# --------------------------------------------------

def telegram(text):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM SECRETS NOT FOUND")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": False
            },
            timeout=20
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print("TELEGRAM ERROR:", repr(e))
        return False


# --------------------------------------------------
# DISCOVERY
# --------------------------------------------------

def discover_search(page, store, url, state):
    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000
        )
        page.wait_for_timeout(1500)

        links = page.locator("a[href]").evaluate_all(
            "(x)=>x.map(a=>a.href)"
        )

        count = 0

        for raw in links:
            try:
                u = clean_url(raw)

                if not u or not product_url(u):
                    continue

                if store_of(u) != store:
                    continue

                if u in state["queue"]:
                    continue

                state["queue"][u] = {
                    "url": u,
                    "store": store,
                    "added": time.time()
                }

                count += 1

            except Exception:
                continue

        return count

    except Exception as e:
        print("DISCOVERY ERROR:", repr(e))
        return 0


def discover(page, state):
    total = 0

    for term in FLIPKART_TERMS:
        try:
            url = (
                "https://www.flipkart.com/search?q="
                + quote(term)
            )

            n = discover_search(
                page, "flipkart", url, state
            )

            print("FLIPKART:", term, "+", n)
            total += n
            save_state(state)

        except Exception as e:
            print("FLIPKART TERM ERROR:", term, repr(e))

    for term in MYNTRA_TERMS:
        try:
            url = "https://www.myntra.com/" + term

            n = discover_search(
                page, "myntra", url, state
            )

            print("MYNTRA:", term, "+", n)
            total += n
            save_state(state)

        except Exception as e:
            print("MYNTRA TERM ERROR:", term, repr(e))

    print("NEW PRODUCTS:", total)
    print("TOTAL QUEUE:", len(state["queue"]))


# --------------------------------------------------
# LIVE PRICE
# --------------------------------------------------

def jsonld_price(page):
    try:
        scripts = page.locator(
            'script[type="application/ld+json"]'
        ).all_text_contents()

        for raw in scripts:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            objs = []

            if isinstance(data, dict):
                objs.append(data)
                if isinstance(data.get("@graph"), list):
                    objs.extend(data["@graph"])

            elif isinstance(data, list):
                objs.extend(data)

            for obj in objs:
                if not isinstance(obj, dict):
                    continue

                typ = obj.get("@type")

                if isinstance(typ, list):
                    ok = "Product" in typ
                else:
                    ok = typ == "Product"

                if not ok:
                    continue

                offers = obj.get("offers")

                if isinstance(offers, dict):
                    offers = [offers]

                if not isinstance(offers, list):
                    continue

                vals = []

                for offer in offers:
                    if isinstance(offer, dict):
                        p = price(offer.get("price"))
                        if p:
                            vals.append(p)

                if vals:
                    return min(vals)

    except Exception:
        pass

    return None


def meta_price(page):
    for selector in [
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[itemprop="price"]'
    ]:
        try:
            x = page.locator(selector).first

            if x.count():
                p = price(
                    x.get_attribute("content")
                )

                if p:
                    return p

        except Exception:
            pass

    return None


def live_price(page, store, url):
    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000
        )

        page.wait_for_timeout(1200)

        p = jsonld_price(page)

        if p and p < MAX_PRICE:
            return p

        p = meta_price(page)

        if p and p < MAX_PRICE:
            return p

    except Exception as e:
        print("LIVE ERROR:", repr(e))

    return None


# --------------------------------------------------
# PRICE HISTORY
# --------------------------------------------------

def history_values(text):
    avg = None
    low = None

    patterns_avg = [
        r"30\s*day\s*average.{0,100}?₹\s*([\d,]+)",
        r"30\s*day\s*avg.{0,100}?₹\s*([\d,]+)",
        r"30d\s*average.{0,100}?₹\s*([\d,]+)"
    ]

    patterns_low = [
        r"all[\s-]*time\s*low.{0,100}?₹\s*([\d,]+)",
        r"all[\s-]*time\s*lowest.{0,100}?₹\s*([\d,]+)"
    ]

    for pat in patterns_avg:
        m = re.search(pat, text, re.I | re.S)
        if m:
            avg = price(m.group(1))
            break

    for pat in patterns_low:
        m = re.search(pat, text, re.I | re.S)
        if m:
            low = price(m.group(1))
            break

    return avg, low


def history(page, retailer_url):
    try:
        page.goto(
            "https://pricehistoryapp.com/",
            wait_until="domcontentloaded",
            timeout=30000
        )

        page.wait_for_timeout(1000)

        inputs = page.locator("input")

        if inputs.count() == 0:
            return None

        target = inputs.first

        for i in range(inputs.count()):
            x = inputs.nth(i)

            text = (
                (x.get_attribute("placeholder") or "")
                + " "
                + (x.get_attribute("aria-label") or "")
            ).lower()

            if "url" in text or "link" in text:
                target = x
                break

        target.fill(retailer_url)

        clicked = False

        buttons = page.locator("button")

        for i in range(buttons.count()):
            b = buttons.nth(i)

            try:
                t = b.inner_text().lower()
            except Exception:
                t = ""

            if (
                "track" in t
                or "check" in t
                or "search" in t
            ):
                b.click()
                clicked = True
                break

        if not clicked:
            target.press("Enter")

        page.wait_for_timeout(3500)

        if "/product/" not in page.url:
            links = page.locator(
                'a[href*="/product/"]'
            )

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
                        timeout=30000
                    )

                    page.wait_for_timeout(1000)

        if "/product/" not in page.url:
            return None

        body = page.locator("body").inner_text(
            timeout=10000
        )

        avg, low = history_values(body)

        if not avg:
            return None

        return {
            "avg": avg,
            "low": low,
            "url": page.url
        }

    except Exception as e:
        print("HISTORY ERROR:", repr(e))
        return None


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def check_one(page, state, url, item):
    try:
        store = item.get("store") or store_of(url)

        if not store:
            return

        print("URL:", url)

        current = live_price(
            page,
            store,
            url
        )

        if not current:
            print("SKIP → LIVE PRICE NOT CONFIRMED")
            return

        print("LIVE:", current)

        if current >= MAX_PRICE:
            print("SKIP → ABOVE ₹8,000")
            return

        h = history(page, url)

        if not h:
            print("SKIP → HISTORY NOT FOUND")
            return

        avg = h["avg"]
        low = h["low"]

        if avg <= 0:
            return

        drop = ((avg - current) / avg) * 100

        print(
            f"AVG: ₹{avg:,.0f} | "
            f"DROP: {drop:.1f}%"
        )

        if low:
            print(f"LOW: ₹{low:,.0f}")

        if drop < MIN_DROP:
            print("NO DEAL")
            return

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
            if low else "Unavailable"
        )

        msg = (
            f"{title}\n\n"
            f"Store: {store.upper()}\n"
            f"Current: ₹{current:,.0f}\n"
            f"30-day average: ₹{avg:,.0f}\n"
            f"Below average: {drop:.1f}%\n"
            f"All-time low: {low_text}\n\n"
            f"🛒 {url}\n"
            f"📊 {h['url']}"
        )

        if telegram(msg):
            print("🚨 ALERT SENT")

            if url not in state["sent"]:
                state["sent"].append(url)

        else:
            print("TELEGRAM FAILED")

    except Exception as e:
        print("PRODUCT FAILED:", url)
        print("ERROR:", repr(e))
        traceback.print_exc()


def main():
    state = load_state()

    print("================================")
    print("VIBHU PRICE SNIPER")
    print("================================")
    print("QUEUE:", len(state["queue"]))

    try:
        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True
            )

            context = browser.new_context(
                viewport={
                    "width": 1365,
                    "height": 900
                },
                locale="en-IN"
            )

            page = context.new_page()

            # Discovery is protected.
            try:
                discover(page, state)
            except Exception as e:
                print("DISCOVERY CRASH:", repr(e))
                traceback.print_exc()

            save_state(state)

            now = time.time()
            ready = []

            for url, item in list(
                state["queue"].items()
            ):
                try:
                    last = float(
                        state["checked"].get(url, 0)
                    )

                    if (
                        now - last
                        >= RECHECK_HOURS * 3600
                    ):
                        ready.append((url, item))

                except Exception:
                    ready.append((url, item))

            print("READY:", len(ready))

            batch = ready[:BATCH_SIZE]

            print(
                "PROCESSING:",
                len(batch)
            )

            for i, (url, item) in enumerate(
                batch,
                1
            ):

                print()
                print(
                    f"========== {i}/{len(batch)} =========="
                )

                # Mark the attempt BEFORE processing.
                # This prevents one broken product from
                # being retried every 15 minutes.
                state["checked"][url] = now
                save_state(state)

                check_one(
                    page,
                    state,
                    url,
                    item
                )

                # Save after EVERY product.
                save_state(state)

            browser.close()

    except Exception as e:
        print()
        print("================================")
        print("MAIN ERROR — STATE SAVED")
        print("================================")
        print(repr(e))
        traceback.print_exc()

    finally:
        save_state(state)

    print()
    print("================================")
    print("SCAN FINISHED")
    print("QUEUE:", len(state["queue"]))
    print("================================")


if __name__ == "__main__":
    main()