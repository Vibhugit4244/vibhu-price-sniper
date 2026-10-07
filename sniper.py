import os, re, json, time
from datetime import datetime
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from playwright.sync_api import sync_playwright

MAX_PRICE = 8000
MAX_PER_RUN = 100
WORKERS = 4
STATE_FILE = "state.json"

BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

SEARCHES = [
    "men shirts", "men t shirts", "men jeans",
    "women tops", "women dresses", "women jeans",
    "shoes", "sneakers", "footwear",
    "earbuds", "headphones", "smartwatch",
    "electronics", "laptop", "monitor",
    "bags", "wallets"
]


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
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


def telegram(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={
                "chat_id": CHAT,
                "text": text
            },
            timeout=15
        )
    except Exception as e:
        print("Telegram error:", e)


def discover():
    found = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for store in ["flipkart.com", "myntra.com"]:

            for query in SEARCHES:

                print("SEARCH:", store, query)

                try:
                    url = (
                        "https://www.google.com/search?q="
                        + quote(f"site:{store} {query}")
                    )

                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=12000
                    )

                    page.wait_for_timeout(700)

                    for a in page.locator("a").all():

                        href = a.get_attribute("href") or ""

                        if store not in href:
                            continue

                        if store == "flipkart.com":
                            valid = "/p/" in href
                        else:
                            valid = "/buy/" in href

                        if valid:
                            href = href.split("&")[0]
                            found.add(href)

                except Exception as e:
                    print("Search skipped:", e)

        browser.close()

    return list(found)


def check_history(url):
    try:

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage"]
            )

            page = browser.new_page()

            page.goto(
                "https://pricehistoryapp.com/",
                wait_until="domcontentloaded",
                timeout=15000
            )

            page.wait_for_timeout(800)

            inputs = page.locator("input").all()

            box = None

            for inp in inputs:

                typ = (inp.get_attribute("type") or "").lower()
                ph = (inp.get_attribute("placeholder") or "").lower()

                if typ in ["text", "search", ""]:
                    if (
                        "url" in ph
                        or "link" in ph
                        or "product" in ph
                        or ph == ""
                    ):
                        box = inp
                        break

            if box is None:
                browser.close()
                return None

            box.fill(url)
            box.press("Enter")

            page.wait_for_timeout(1800)

            text = page.locator("body").inner_text()

            current = None
            average = None

            m = re.search(
                r"Current:\s*₹\s*([\d,]+)",
                text,
                re.I
            )

            if m:
                current = float(
                    m.group(1).replace(",", "")
                )

            m = re.search(
                r"30d\s*Average\s*₹?\s*([\d,]+)",
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

    # -----------------------------
    # DISCOVERY
    # -----------------------------

    print("Starting discovery...")

    discovered = discover()

    print("Discovered:", len(discovered))

    known = set(state["queue"])
    known.update(state["checked"].keys())

    added = 0

    for url in discovered:

        if url not in known:

            state["queue"].append(url)
            known.add(url)
            added += 1

    print("New URLs:", added)
    print("Queue:", len(state["queue"]))

    save_state(state)

    # -----------------------------
    # TAKE BATCH
    # -----------------------------

    batch = state["queue"][:MAX_PER_RUN]

    if not batch:

        print("Nothing to check.")
        return

    print("Checking:", len(batch))

    # Remove batch immediately.
    # Results are saved individually below.
    state["queue"] = state["queue"][len(batch):]

    save_state(state)

    # -----------------------------
    # PARALLEL HISTORY CHECK
    # -----------------------------

    results = {}

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as executor:

        jobs = {
            executor.submit(check_history, url): url
            for url in batch
        }

        for n, future in enumerate(
            as_completed(jobs), 1
        ):

            url = jobs[future]

            try:
                result = future.result()
            except:
                result = None

            results[url] = result

            print(
                f"[{n}/{len(batch)}]",
                result,
                url
            )

    # -----------------------------
    # PROCESS RESULTS
    # -----------------------------

    checked_time = datetime.utcnow().isoformat()

    for url in batch:

        result = results.get(url)

        state["checked"][url] = {
            "time": checked_time,
            "result": result
        }

        if not result:
            continue

        current, average = result

        if current >= MAX_PRICE:
            continue

        if average <= 0:
            continue

        ratio = current / average

        # Must be at least 45% below average
        if ratio > 0.55:
            continue

        drop = (1 - ratio) * 100

        signature = (
            str(round(current)) +
            ":" +
            str(round(average))
        )

        if state["alerts"].get(url) == signature:
            continue

        message = (
            "🚨 BLOCKBUSTER DEAL\n\n"
            f"Current: ₹{current:,.0f}\n"
            f"30-day average: ₹{average:,.0f}\n"
            f"Below 30d average: {drop:.1f}%\n\n"
            f"{url}"
        )

        telegram(message)

        state["alerts"][url] = signature

        print("🔥 ALERT SENT:", url)

    save_state(state)

    print()
    print("========== DONE ==========")
    print("Checked:", len(batch))
    print("Qualified:", sum(
        1 for x in results.values()
        if x and x[0] < MAX_PRICE
        and x[1] > 0
        and x[0] / x[1] <= 0.55
    ))
    print("Remaining queue:", len(state["queue"]))
    print("===========================")


if __name__ == "__main__":
    main()