import os
import re
import json
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

MAX_PRICE = 8000
DROP_RATIO = 0.55
BATCH_SIZE = 100
WORKERS = 4
RECHECK_HOURS = 24
STATE_FILE = "state.json"

BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

SOURCES = [
    ("flipkart", "https://www.flipkart.com/search?q=men+shirts"),
    ("flipkart", "https://www.flipkart.com/search?q=men+tshirts"),
    ("flipkart", "https://www.flipkart.com/search?q=men+jeans"),
    ("flipkart", "https://www.flipkart.com/search?q=women+tops"),
    ("flipkart", "https://www.flipkart.com/search?q=women+dresses"),
    ("flipkart", "https://www.flipkart.com/search?q=women+jeans"),
    ("flipkart", "https://www.flipkart.com/search?q=shoes"),
    ("flipkart", "https://www.flipkart.com/search?q=sneakers"),
    ("flipkart", "https://www.flipkart.com/search?q=footwear"),
    ("flipkart", "https://www.flipkart.com/search?q=earbuds"),
    ("flipkart", "https://www.flipkart.com/search?q=headphones"),
    ("flipkart", "https://www.flipkart.com/search?q=smartwatch"),
    ("flipkart", "https://www.flipkart.com/search?q=laptop"),
    ("flipkart", "https://www.flipkart.com/search?q=monitor"),
    ("flipkart", "https://www.flipkart.com/search?q=electronics"),
    ("flipkart", "https://www.flipkart.com/search?q=bags"),
    ("flipkart", "https://www.flipkart.com/search?q=wallets"),

    ("myntra", "https://www.myntra.com/men-shirts"),
    ("myntra", "https://www.myntra.com/men-tshirts"),
    ("myntra", "https://www.myntra.com/men-jeans"),
    ("myntra", "https://www.myntra.com/women-tops"),
    ("myntra", "https://www.myntra.com/women-dresses"),
    ("myntra", "https://www.myntra.com/women-jeans"),
    ("myntra", "https://www.myntra.com/shoes"),
    ("myntra", "https://www.myntra.com/sneakers"),
    ("myntra", "https://www.myntra.com/footwear"),
    ("myntra", "https://www.myntra.com/bags"),
    ("myntra", "https://www.myntra.com/wallets"),
]


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except:
        s = {}

    if not isinstance(s, dict):
        s = {}

    s.setdefault("queue", [])
    s.setdefault("checked", {})
    s.setdefault("alerts", {})

    return s


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={"chat_id": CHAT, "text": msg},
            timeout=15
        )
    except Exception as e:
        print("Telegram:", e)


def extract_store_price(text):
    vals = []

    for x in re.findall(r"₹\s*([\d,]+)", text):
        try:
            n = float(x.replace(",", ""))
            if 50 <= n < 8000:
                vals.append(n)
        except:
            pass

    return min(vals) if vals else None


def discover():
    products = {}

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36"
            )
        )

        page = context.new_page()

        for store, url in SOURCES:

            print("CATALOGUE:", store, url)

            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=30000
                )

                page.wait_for_timeout(1500)

                for _ in range(6):
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(400)

                # Get links from catalogue
                for a in page.locator("a").all():

                    href = a.get_attribute("href") or ""

                    if not href:
                        continue

                    if href.startswith("/"):
                        href = (
                            "https://www." +
                            store +
                            ".com" +
                            href
                        )

                    if store == "flipkart":
                        if "flipkart.com" not in href:
                            continue
                        if "/p/" not in href:
                            continue

                    else:
                        if "myntra.com" not in href:
                            continue
                        if "/buy/" not in href:
                            continue

                    href = href.split("?")[0]

                    products[href] = store

            except Exception as e:
                print("Catalogue skipped:", e)

        browser.close()

    return products


def history_lookup(url):

    try:

        with sync_playwright() as p:

            browser = p.chromium.launch(headless=True)

            page = browser.new_page()

            page.goto(
                "https://pricehistoryapp.com/",
                wait_until="domcontentloaded",
                timeout=30000
            )

            page.wait_for_timeout(1000)

            box = None

            for inp in page.locator("input").all():

                ph = (
                    inp.get_attribute("placeholder")
                    or ""
                ).lower()

                typ = (
                    inp.get_attribute("type")
                    or ""
                ).lower()

                if (
                    "product" in ph
                    or "link" in ph
                    or typ in ("search", "text")
                ):
                    box = inp
                    break

            if box is None:
                print("NO PRICE HISTORY BOX")
                browser.close()
                return None

            box.fill(url)
            box.press("Enter")

            try:
                page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=15000
                )
            except:
                pass

            page.wait_for_timeout(3000)

            text = page.locator("body").inner_text()

            current = None
            avg30 = None

            m = re.search(
                r"Current\s*:?\s*₹\s*([\d,]+)",
                text,
                re.I
            )

            if m:
                current = float(
                    m.group(1).replace(",", "")
                )

            m = re.search(
                r"30d\s*Average\s*:?\s*₹\s*([\d,]+)",
                text,
                re.I
            )

            if m:
                avg30 = float(
                    m.group(1).replace(",", "")
                )

            # If first page didn't contain the data,
            # open the generated product-history page.
            if current is None or avg30 is None:

                for a in page.locator("a").all():

                    href = a.get_attribute("href") or ""

                    if "/product/" not in href:
                        continue

                    if href.startswith("/"):
                        href = (
                            "https://pricehistoryapp.com"
                            + href
                        )

                    try:
                        page.goto(
                            href,
                            wait_until="domcontentloaded",
                            timeout=20000
                        )

                        page.wait_for_timeout(1200)

                        text = page.locator(
                            "body"
                        ).inner_text()

                        m = re.search(
                            r"Current\s*:?\s*₹\s*([\d,]+)",
                            text,
                            re.I
                        )

                        if m:
                            current = float(
                                m.group(1).replace(",", "")
                            )

                        m = re.search(
                            r"30d\s*Average\s*:?\s*₹\s*([\d,]+)",
                            text,
                            re.I
                        )

                        if m:
                            avg30 = float(
                                m.group(1).replace(",", "")
                            )

                        if current and avg30:
                            break

                    except:
                        pass

            result = None

            if (
                current is not None
                and avg30 is not None
                and current > 0
                and avg30 > 0
            ):
                result = {
                    "current": current,
                    "average30": avg30,
                    "history_url": page.url
                }

            print(
                "HISTORY:",
                current,
                avg30
            )

            browser.close()

            return result

    except Exception as e:

        print("History error:", e)

        return None


def main():

    state = load_state()

    print("Starting catalogue scan...")

    found = discover()

    print("Catalogue products found:", len(found))

    now = datetime.utcnow()

    # Existing URLs
    queued = set(state["queue"])

    # Add only NEW or due-for-recheck products.
    for url in found:

        if url in queued:
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

        state["queue"].append(url)
        queued.add(url)

    save_state(state)

    print("Queue:", len(state["queue"]))

    if not state["queue"]:
        print("Nothing waiting.")
        return

    batch = state["queue"][:BATCH_SIZE]

    print("History checking:", len(batch))

    results = {}

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as pool:

        jobs = {
            pool.submit(history_lookup, url): url
            for url in batch
        }

        for i, job in enumerate(
            as_completed(jobs),
            1
        ):

            url = jobs[job]

            try:
                result = job.result()
            except Exception as e:
                print("Worker error:", e)
                result = None

            results[url] = result

            print(
                f"[{i}/{len(batch)}]",
                result
            )

    successful = set()

    for url in batch:

        result = results.get(url)

        # Don't lose products if history lookup fails.
        if result is None:
            continue

        successful.add(url)

        current = result["current"]
        avg30 = result["average30"]

        state["checked"][url] = {
            "time": now.isoformat(),
            "current": current,
            "average30": avg30,
            "history_url": result["history_url"]
        }

        # Final current-price rule
        if current >= MAX_PRICE:
            continue

        # 45% or more below 30-day average
        if current > avg30 * DROP_RATIO:
            continue

        drop = (1 - current / avg30) * 100

        signature = (
            f"{round(current)}:"
            f"{round(avg30)}"
        )

        if state["alerts"].get(url) == signature:
            continue

        telegram(
            "🚨 BLOCKBUSTER DEAL\n\n"
            f"Current: ₹{current:,.0f}\n"
            f"30-day average: ₹{avg30:,.0f}\n"
            f"Below average: {drop:.1f}%\n\n"
            f"{url}"
        )

        state["alerts"][url] = signature

        print("🔥 ALERT:", url)

    # Remove only successfully history-checked products.
    state["queue"] = [
        u for u in state["queue"]
        if u not in successful
    ]

    save_state(state)

    print()
    print("==============================")
    print("RUN COMPLETE")
    print("Catalogue found:", len(found))
    print("History checked:", len(successful))
    print(
        "History failed:",
        len(batch) - len(successful)
    )
    print("Queue remaining:", len(state["queue"]))
    print("==============================")


if __name__ == "__main__":
    main()