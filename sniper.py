import os
import re
import json
import time
import requests
from datetime import datetime, timedelta
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

MAX_PRICE = 8000
DROP_RATIO = 0.55
HISTORY_BATCH = 40
RECHECK_HOURS = 24
STATE_FILE = "state.json"

BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

SOURCES = [
    ("flipkart", "https://www.flipkart.com/search?q=men+shirts"),
    ("flipkart", "https://www.flipkart.com/search?q=men+tshirts"),
    ("flipkart", "https://www.flipkart.com/search?q=men+pants"),
    ("flipkart", "https://www.flipkart.com/search?q=men+jeans"),
    ("flipkart", "https://www.flipkart.com/search?q=women+shirts"),
    ("flipkart", "https://www.flipkart.com/search?q=women+tshirts"),
    ("flipkart", "https://www.flipkart.com/search?q=women+tops"),
    ("flipkart", "https://www.flipkart.com/search?q=women+dresses"),
    ("flipkart", "https://www.flipkart.com/search?q=women+pants"),
    ("flipkart", "https://www.flipkart.com/search?q=women+jeans"),
    ("flipkart", "https://www.flipkart.com/search?q=shoes"),
    ("flipkart", "https://www.flipkart.com/search?q=sneakers"),
    ("flipkart", "https://www.flipkart.com/search?q=footwear"),
    ("flipkart", "https://www.flipkart.com/search?q=earbuds"),
    ("flipkart", "https://www.flipkart.com/search?q=headphones"),
    ("flipkart", "https://www.flipkart.com/search?q=smartwatch"),
    ("flipkart", "https://www.flipkart.com/search?q=monitor"),
    ("flipkart", "https://www.flipkart.com/search?q=laptop"),
    ("flipkart", "https://www.flipkart.com/search?q=speaker"),
    ("flipkart", "https://www.flipkart.com/search?q=electronics"),

    ("myntra", "https://www.myntra.com/men-shirts"),
    ("myntra", "https://www.myntra.com/men-tshirts"),
    ("myntra", "https://www.myntra.com/men-jeans"),
    ("myntra", "https://www.myntra.com/men-trousers"),
    ("myntra", "https://www.myntra.com/women-tops"),
    ("myntra", "https://www.myntra.com/women-tshirts"),
    ("myntra", "https://www.myntra.com/women-shirts"),
    ("myntra", "https://www.myntra.com/women-dresses"),
    ("myntra", "https://www.myntra.com/women-jeans"),
    ("myntra", "https://www.myntra.com/women-trousers"),
    ("myntra", "https://www.myntra.com/shoes"),
    ("myntra", "https://www.myntra.com/sneakers"),
    ("myntra", "https://www.myntra.com/footwear"),
    ("myntra", "https://www.myntra.com/earphones"),
    ("myntra", "https://www.myntra.com/headphones"),
    ("myntra", "https://www.myntra.com/smart-watches"),
    ("myntra", "https://www.myntra.com/speakers"),
]

def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except:
        s = {}

    if not isinstance(s, dict):
        s = {}

    if not isinstance(s.get("queue"), list):
        s["queue"] = []

    if not isinstance(s.get("checked"), dict):
        s["checked"] = {}

    if not isinstance(s.get("alerts"), dict):
        s["alerts"] = {}

    return s


def save_state(s):
    tmp = STATE_FILE + ".tmp"

    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)

    os.replace(tmp, STATE_FILE)


def telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={
                "chat_id": CHAT,
                "text": msg,
                "disable_web_page_preview": False
            },
            timeout=15
        )
        print("Telegram:", r.status_code)
    except Exception as e:
        print("Telegram error:", e)


def price_value(text):
    if not text:
        return None

    m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", text)

    if not m:
        return None

    try:
        return float(m.group(1).replace(",", ""))
    except:
        return None


def clean_url(url):
    if not url:
        return ""

    return url.split("?")[0].split("#")[0].strip()


def discover(page):
    products = {}

    for store, source in SOURCES:
        print("DISCOVER:", store, source)

        try:
            page.goto(
                source,
                wait_until="domcontentloaded",
                timeout=30000
            )

            page.wait_for_timeout(1200)

            for _ in range(5):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(300)

            links = page.locator("a").all()

            for a in links:
                href = a.get_attribute("href") or ""

                if not href:
                    continue

                if href.startswith("/"):
                    if store == "flipkart":
                        href = urljoin(
                            "https://www.flipkart.com",
                            href
                        )
                    else:
                        href = urljoin(
                            "https://www.myntra.com",
                            href
                        )

                href = clean_url(href)

                if store == "flipkart":
                    if (
                        "flipkart.com" not in href
                        or "/p/" not in href
                    ):
                        continue
                else:
                    if (
                        "myntra.com" not in href
                        or "/buy/" not in href
                    ):
                        continue

                products[href] = store

        except Exception as e:
            print("Discovery error:", e)

    return products


def get_retailer_price(page, store, url):
    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=25000
        )

        page.wait_for_timeout(1000)

        # ---------------------------------------------
        # 1. JSON-LD PRODUCT DATA
        # ---------------------------------------------
        scripts = page.locator(
            'script[type="application/ld+json"]'
        ).all()

        for script in scripts:
            try:
                raw = script.inner_text()

                data = json.loads(raw)

                items = data if isinstance(data, list) else [data]

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    if item.get("@type") == "Product":
                        offers = item.get("offers")

                        if isinstance(offers, dict):
                            p = offers.get("price")

                            try:
                                p = float(str(p).replace(",", ""))
                            except:
                                p = None

                            if p and 50 <= p < MAX_PRICE:
                                return p

            except:
                pass

        # ---------------------------------------------
        # 2. META PRODUCT PRICE
        # ---------------------------------------------
        selectors = [
            'meta[property="product:price:amount"]',
            'meta[itemprop="price"]',
            'meta[name="twitter:data1"]'
        ]

        for selector in selectors:
            try:
                for el in page.locator(selector).all():
                    value = (
                        el.get_attribute("content")
                        or el.get_attribute("value")
                        or ""
                    )

                    try:
                        p = float(value.replace(",", ""))
                    except:
                        continue

                    if 50 <= p < MAX_PRICE:
                        return p

            except:
                pass

        # ---------------------------------------------
        # 3. KNOWN RETAILER PRICE ELEMENTS
        # ---------------------------------------------
        if store == "myntra":
            selectors = [
                '[class*="pdp-price"]',
                '[class*="selling-price"]',
                '[class*="discounted-price"]',
                '[class*="pdp-discount-container"]'
            ]
        else:
            selectors = [
                '[class*="Nx9bqj"]',
                '[class*="CxhGGd"]',
                '[class*="dyC4hf"]'
            ]

        candidates = []

        for selector in selectors:
            try:
                for el in page.locator(selector).all():
                    txt = el.inner_text().strip()
                    p = price_value(txt)

                    if p and 50 <= p < MAX_PRICE:
                        candidates.append(p)
            except:
                pass

        # Only accept if a specific price element gave us a value.
        if candidates:
            return min(candidates)

        # ---------------------------------------------
        # 4. FAIL SAFE
        # ---------------------------------------------
        print("PRICE NOT CONFIDENT:", url)
        return None

    except Exception as e:
        print("Retailer error:", e)
        return None


def history_lookup(page, retailer_url):
    try:
        page.goto(
            "https://pricehistoryapp.com/",
            wait_until="domcontentloaded",
            timeout=25000
        )

        page.wait_for_timeout(800)

        box = None

        for inp in page.locator("input").all():
            placeholder = (
                inp.get_attribute("placeholder")
                or ""
            ).lower()

            typ = (
                inp.get_attribute("type")
                or ""
            ).lower()

            if (
                "product" in placeholder
                or "link" in placeholder
                or "paste" in placeholder
                or typ == "url"
            ):
                box = inp
                break

        if box is None:
            print("HISTORY INPUT NOT FOUND")
            return None

        box.fill(retailer_url)

        clicked = False

        for button in page.locator("button").all():
            try:
                text = button.inner_text().strip().lower()

                if (
                    "track price" in text
                    or text == "track"
                    or "check" in text
                ):
                    button.click()
                    clicked = True
                    break
            except:
                pass

        if not clicked:
            box.press("Enter")

        page.wait_for_timeout(2500)

        # ---------------------------------------------
        # Find actual PriceHistoryApp product page
        # ---------------------------------------------
        if "/product/" not in page.url:
            for a in page.locator("a").all():
                href = a.get_attribute("href") or ""

                if "/product/" not in href:
                    continue

                if href.startswith("/"):
                    href = urljoin(
                        "https://pricehistoryapp.com",
                        href
                    )

                try:
                    page.goto(
                        href,
                        wait_until="domcontentloaded",
                        timeout=20000
                    )

                    page.wait_for_timeout(700)

                    if "/product/" in page.url:
                        break

                except:
                    pass

        if "/product/" not in page.url:
            print("HISTORY DID NOT RESOLVE:", retailer_url)
            return None

        text = page.locator("body").inner_text()

        # ---------------------------------------------
        # 30-DAY AVERAGE
        # ---------------------------------------------
        avg = None

        patterns = [
            r"30d\s*Average\s*[:₹\s]*([\d,]+(?:\.\d+)?)",
            r"30\s*day\s*Average\s*[:₹\s]*([\d,]+(?:\.\d+)?)",
            r"30-day\s*average\s*[:₹\s]*([\d,]+(?:\.\d+)?)"
        ]

        for pattern in patterns:
            m = re.search(pattern, text, re.I)

            if m:
                avg = price_value("₹" + m.group(1))
                break

        if not avg or avg <= 0:
            print("NO 30D AVG:", page.url)
            return None

        # ---------------------------------------------
        # ALL-TIME LOW
        # ---------------------------------------------
        low = None

        m = re.search(
            r"All-time Low\s*[:₹\s]*([\d,]+(?:\.\d+)?)",
            text,
            re.I
        )

        if m:
            low = price_value("₹" + m.group(1))

        # ---------------------------------------------
        # TITLE
        # ---------------------------------------------
        title = ""

        try:
            title = page.locator("h1").first.inner_text().strip()
        except:
            pass

        return {
            "average30": avg,
            "alltime_low": low,
            "title": title,
            "history_url": page.url
        }

    except Exception as e:
        print("History error:", e)
        return None


def deal_strength(current, average, low):
    drop = (1 - current / average) * 100

    if low and low > 0:
        low_gap = ((current - low) / low) * 100
    else:
        low_gap = 999

    return drop, low_gap


def main():
    state = load_state()

    now = datetime.utcnow()

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox"
            ]
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36"
            )
        )

        page = context.new_page()

        # =============================================
        # DISCOVERY
        # =============================================
        products = discover(page)

        print("DISCOVERED:", len(products))

        queued_urls = {
            x["url"]
            for x in state["queue"]
            if isinstance(x, dict) and "url" in x
        }

        # =============================================
        # LIVE PRICE FILTER
        # =============================================
        added = 0

        for url, store in products.items():

            if url in queued_urls:
                continue

            old = state["checked"].get(url)

            if old:
                try:
                    last = datetime.fromisoformat(
                        old["time"]
                    )

                    if (
                        now - last
                        < timedelta(hours=RECHECK_HOURS)
                    ):
                        continue

                except:
                    pass

            price = get_retailer_price(
                page,
                store,
                url
            )

            if price is None:
                continue

            print(
                "LIVE:",
                round(price),
                url
            )

            # STRICT CURRENT PRICE RULE
            if price >= MAX_PRICE:
                continue

            state["queue"].append({
                "url": url,
                "store": store,
                "price": price
            })

            queued_urls.add(url)
            added += 1

            # Save continuously so a crash doesn't
            # destroy progress.
            if added % 10 == 0:
                save_state(state)

        save_state(state)

        print("NEW QUEUE ITEMS:", added)
        print("QUEUE:", len(state["queue"]))

        # =============================================
        # HISTORY BATCH
        # =============================================
        batch = state["queue"][:HISTORY_BATCH]

        print(
            "HISTORY BATCH:",
            len(batch)
        )

        completed = []

        for i, item in enumerate(batch, 1):

            url = item["url"]
            current = item["price"]

            print(
                f"[{i}/{len(batch)}]",
                "HISTORY:",
                url
            )

            result = history_lookup(
                page,
                url
            )

            if result is None:
                continue

            avg = result["average30"]
            low = result["alltime_low"]

            print(
                "CURRENT:",
                round(current),
                "AVG30:",
                round(avg),
                "LOW:",
                round(low) if low else "?"
            )

            state["checked"][url] = {
                "time": now.isoformat(),
                "current": current,
                "average30": avg,
                "alltime_low": low,
                "history_url": result["history_url"]
            }

            completed.append(url)

            # =========================================
            # DEAL RULE
            # =========================================
            if current > avg * DROP_RATIO:
                save_state(state)
                continue

            drop, low_gap = deal_strength(
                current,
                avg,
                low
            )

            # =========================================
            # STRENGTH LABEL
            # =========================================
            if drop >= 75:
                level = "🔥🔥🔥 UNPRECEDENTED"
            elif drop >= 65:
                level = "🔥🔥 EXCEPTIONAL"
            elif drop >= 55:
                level = "🔥 VERY STRONG"
            else:
                level = "🔥 STRONG"

            if low and low_gap <= 5:
                low_text = (
                    f"All-time low: ₹{low:,.0f}\n"
                    f"Only {low_gap:.1f}% above ATL"
                )
            elif low:
                low_text = (
                    f"All-time low: ₹{low:,.0f}\n"
                    f"{low_gap:.1f}% above ATL"
                )
            else:
                low_text = "All-time low: unavailable"

            signature = (
                f"{round(current)}:"
                f"{round(avg)}:"
                f"{round(low or 0)}"
            )

            if state["alerts"].get(url) == signature:
                save_state(state)
                continue

            store = item["store"].upper()

            message = (
                f"{level}\n\n"
                f"Store: {store}\n"
                f"Current: ₹{current:,.0f}\n"
                f"30-day average: ₹{avg:,.0f}\n"
                f"Below 30-day average: {drop:.1f}%\n"
                f"{low_text}\n\n"
                f"{url}"
            )

            telegram(message)

            state["alerts"][url] = signature

            print(
                "🚨 ALERT:",
                url,
                f"{drop:.1f}% below average"
            )

            save_state(state)

        # =============================================
        # REMOVE SUCCESSFULLY CHECKED ITEMS
        # =============================================
        done = set(completed)

        state["queue"] = [
            x
            for x in state["queue"]
            if x["url"] not in done
        ]

        save_state(state)

        browser.close()

    print()
    print("==============================")
    print("RUN COMPLETE")
    print("Catalogue found:", len(products))
    print("History checked:", len(completed))
    print("Queue remaining:", len(state["queue"]))
    print("==============================")


if __name__ == "__main__":
    main()