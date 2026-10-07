import os
import re
import json
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from playwright.sync_api import sync_playwright

MAX_PRICE = 8000
BATCH_SIZE = 100
WORKERS = 4
STATE_FILE = "state.json"

BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

SEARCHES = [
    "men shirts",
    "men t shirts",
    "men jeans",
    "women tops",
    "women dresses",
    "women jeans",
    "shoes",
    "sneakers",
    "footwear",
    "earbuds",
    "headphones",
    "smartwatch",
    "electronics",
    "laptop",
    "monitor",
    "bags",
    "wallets"
]


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {
            "queue": [],
            "checked": {},
            "alerts": {}
        }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(message):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={
                "chat_id": CHAT,
                "text": message
            },
            timeout=15
        )
    except Exception as e:
        print("Telegram error:", e)


def discover():

    products = set()

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        page = browser.new_page()

        for store in ["flipkart", "myntra"]:

            for search in SEARCHES:

                print(f"SEARCH: {store} {search}")

                try:

                    q = search.replace(" ", "+")

                    if store == "flipkart":
                        url = (
                            "https://www.flipkart.com/search?"
                            f"q={q}"
                        )
                    else:
                        url = (
                            "https://www.myntra.com/"
                            + search.replace(" ", "-")
                        )

                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=20000
                    )

                    page.wait_for_timeout(1200)

                    # Scroll so lazy-loaded products appear
                    for _ in range(4):
                        page.mouse.wheel(0, 1800)
                        page.wait_for_timeout(400)

                    for a in page.locator("a").all():

                        href = a.get_attribute("href") or ""

                        if store == "flipkart":

                            if "/p/" not in href:
                                continue

                            if not href.startswith("http"):
                                href = "https://www.flipkart.com" + href

                            href = href.split("?")[0]

                        else:

                            if "/buy/" not in href:
                                continue

                            if not href.startswith("http"):
                                href = "https://www.myntra.com" + href

                            href = href.split("?")[0]

                        products.add(href)

                except Exception as e:
                    print("Skipped:", str(e))

        browser.close()

    return list(products)


def price_history(url):

    try:

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True
            )

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
                    inp.get_attribute("type")
                    or ""
                ).lower()

                placeholder = (
                    inp.get_attribute("placeholder")
                    or ""
                ).lower()

                if typ in ["text", "search", ""]:

                    if (
                        "url" in placeholder
                        or "link" in placeholder
                        or "product" in placeholder
                        or not placeholder
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

            return current, average

    except Exception as e:

        print("History error:", str(e))

        return None


def main():

    state = load_state()

    # --------------------------------
    # DISCOVERY
    # --------------------------------

    print("Starting catalogue discovery...")

    found = discover()

    print("Discovered:", len(found))

    known = set(state["queue"])
    known.update(state["checked"].keys())

    added = 0

    for url in found:

        if url not in known:

            state["queue"].append(url)
            known.add(url)
            added += 1

    print("New URLs:", added)
    print("Queue:", len(state["queue"]))

    save_state(state)

    # --------------------------------
    # BATCH
    # --------------------------------

    batch = state["queue"][:BATCH_SIZE]

    if not batch:

        print("No products waiting.")
        return

    print("History checking:", len(batch))

    state["queue"] = state["queue"][len(batch):]

    save_state(state)

    # --------------------------------
    # PARALLEL CHECK
    # --------------------------------

    results = {}

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        jobs = {
            executor.submit(price_history, url): url
            for url in batch
        }

        for number, job in enumerate(
            as_completed(jobs), 1
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

    # --------------------------------
    # DEAL FILTER
    # --------------------------------

    qualified = 0

    for url in batch:

        result = results.get(url)

        state["checked"][url] = {
            "time": datetime.utcnow().isoformat(),
            "result": result
        }

        if not result:
            continue

        current, average = result

        if current >= MAX_PRICE:
            continue

        if average <= 0:
            continue

        # Must be at least 45% below average
        if current > average * 0.55:
            continue

        qualified += 1

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

    save_state(state)

    print()
    print("============================")
    print("RUN COMPLETE")
    print("Checked:", len(batch))
    print("Qualified:", qualified)
    print("Queue remaining:", len(state["queue"]))
    print("============================")


if __name__ == "__main__":
    main()