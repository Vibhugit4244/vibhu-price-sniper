import os
import re
import json
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

MAX_PRICE = 8000
DROP_LIMIT = 0.55
BATCH_SIZE = 100
WORKERS = 4
RECHECK_HOURS = 24
STATE_FILE = "state.json"

BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

SOURCES = [
    ("flipkart", "https://www.flipkart.com/search?q=men+shirts"),
    ("flipkart", "https://www.flipkart.com/search?q=men+t+shirts"),
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
    ("flipkart", "https://www.flipkart.com/search?q=electronics"),
    ("flipkart", "https://www.flipkart.com/search?q=laptop"),
    ("flipkart", "https://www.flipkart.com/search?q=monitor"),
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
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            state = {}

        if not isinstance(state.get("queue"), list):
            state["queue"] = []

        if not isinstance(state.get("checked"), dict):
            state["checked"] = {}

        if not isinstance(state.get("alerts"), dict):
            state["alerts"] = {}

        return state

    except Exception:
        return {
            "queue": [],
            "checked": {},
            "alerts": {}
        }


def save_state(state):
    tmp = STATE_FILE + ".tmp"

    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)

    os.replace(tmp, STATE_FILE)


def send_telegram(message):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={
                "chat_id": CHAT,
                "text": message,
                "disable_web_page_preview": False
            },
            timeout=15
        )

        if not r.ok:
            print("Telegram error:", r.text)

    except Exception as e:
        print("Telegram error:", e)


def discover():

    products = {}

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        )

        page = context.new_page()

        for store, url in SOURCES:

            print("SEARCH:", store, url)

            try:

                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=30000
                )

                page.wait_for_timeout(1500)

                for _ in range(5):
                    page.mouse.wheel(0, 1800)
                    page.wait_for_timeout(500)

                links = page.locator("a").all()

                for a in links:

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

                        href = href.split("?")[0]

                    else:

                        if "myntra.com" not in href:
                            continue

                        if "/buy/" not in href:
                            continue

                        href = href.split("?")[0]

                    products[href] = store

            except Exception as e:
                print("Skipped:", e)

        browser.close()

    return products


def get_price(url, page):

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=20000
        )

        page.wait_for_timeout(800)

        text = page.locator("body").inner_text()

        prices = re.findall(
            r"₹\s*([\d,]+)",
            text
        )

        if not prices:
            return None

        values = []

        for x in prices:
            try:
                v = float(x.replace(",", ""))
                if 50 <= v <= 1000000:
                    values.append(v)
            except:
                pass

        if not values:
            return None

        return min(values)

    except:
        return None


def history_worker(url):

    try:

        with sync_playwright() as p:

            browser = p.chromium.launch(headless=True)

            page = browser.new_page()

            page.goto(
                "https://pricehistoryapp.com/",
                wait_until="domcontentloaded",
                timeout=20000
            )

            page.wait_for_timeout(1000)

            inputs = page.locator("input").all()

            box = None

            for inp in inputs:

                typ = (
                    inp.get_attribute("type") or ""
                ).lower()

                placeholder = (
                    inp.get_attribute("placeholder") or ""
                ).lower()

                if typ in ("text", "search", ""):

                    if (
                        "url" in placeholder
                        or "link" in placeholder
                        or "product" in placeholder
                        or placeholder == ""
                    ):
                        box = inp
                        break

            if box is None:
                browser.close()
                return None

            box.fill(url)
            box.press("Enter")

            page.wait_for_timeout(2500)

            text = page.locator("body").inner_text()

            current = None
            average = None

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
                average = float(
                    m.group(1).replace(",", "")
                )

            browser.close()

            if current is None or average is None:
                return None

            if current <= 0 or average <= 0:
                return None

            return current, average

    except Exception as e:

        print("History error:", str(e))

        return None


def main():

    state = load_state()

    print("Starting catalogue discovery...")

    found = discover()

    print("Discovered:", len(found))

    now = datetime.utcnow()

    known = set(state["queue"])

    for url in found:

        if url in known:
            continue

        old = state["checked"].get(url)

        if old:

            try:
                checked_time = datetime.fromisoformat(
                    old["time"]
                )

                if now - checked_time < timedelta(
                    hours=RECHECK_HOURS
                ):
                    continue

            except:
                pass

        state["queue"].append(url)
        known.add(url)

    print("Queue:", len(state["queue"]))

    save_state(state)

    if not state["queue"]:

        print("No products waiting.")
        return

    batch = state["queue"][:BATCH_SIZE]

    print("History checking:", len(batch))

    results = {}

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        jobs = {
            executor.submit(
                history_worker,
                url
            ): url
            for url in batch
        }

        for number, job in enumerate(
            as_completed(jobs),
            1
        ):

            url = jobs[job]

            try:
                result = job.result()
            except:
                result = None

            results[url] = result

            print(
                f"[{number}/{len(batch)}]",
                result
            )

    successful = set()

    for url in batch:

        result = results.get(url)

        if result is None:
            continue

        current, average = result

        state["checked"][url] = {
            "time": now.isoformat(),
            "current": current,
            "average30": average
        }

        successful.add(url)

        if current >= MAX_PRICE:
            continue

        if current > average * DROP_LIMIT:
            continue

        drop = (
            1 - current / average
        ) * 100

        signature = (
            f"{round(current)}:"
            f"{round(average)}"
        )

        if state["alerts"].get(url) == signature:
            continue

        message = (
            "🚨 BLOCKBUSTER DEAL\n\n"
            f"Current: ₹{current:,.0f}\n"
            f"30-day average: ₹{average:,.0f}\n"
            f"Below average: {drop:.1f}%\n\n"
            f"{url}"
        )

        send_telegram(message)

        state["alerts"][url] = signature

        print("🔥 ALERT:", url)

    state["queue"] = [
        url for url in state["queue"]
        if url not in successful
    ]

    save_state(state)

    print()
    print("============================")
    print("RUN COMPLETE")
    print("Discovered:", len(found))
    print("Checked:", len(successful))
    print("Failed:", len(batch) - len(successful))
    print("Queue remaining:", len(state["queue"]))
    print("============================")


if __name__ == "__main__":
    main()