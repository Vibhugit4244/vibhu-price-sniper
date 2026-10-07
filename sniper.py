import os, re, json, time
from datetime import datetime, timedelta
from urllib.parse import quote
import requests
from playwright.sync_api import sync_playwright

MAX_PRICE = 8000
DROP = 0.45
RECHECK_HOURS = 24
STATE_FILE = "state.json"

BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

TRACKERS = [
    ("PHA", "https://pricehistoryapp.com/"),
    ("PTRAIL", "https://pricehistorytracker.in/"),
    ("PDROPY", "https://pricedropy.com/")
]

QUERIES = [
    "men clothing", "women clothing",
    "shoes", "footwear",
    "electronics", "earbuds", "headphones",
    "smartwatch", "laptop", "mobile", "monitor",
    "bags", "wallet", "accessories"
]

def money(x):
    if not x:
        return None
    x = x.replace(",", "")
    m = re.search(r"(?:₹|Rs\.?|INR)\s*([0-9]+(?:\.[0-9]+)?)", x, re.I)
    return float(m.group(1)) if m else None

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"checked": {}, "alerts": {}}

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

def send(msg):
    requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        data={"chat_id": CHAT, "text": msg},
        timeout=20
    )

def retailer(url):
    return "myntra" if "myntra.com" in url else "flipkart"

def discover(page):
    found = set()

    for q in QUERIES:
        for site in ["flipkart.com", "myntra.com"]:
            url = "https://www.google.com/search?q=" + quote(
                f"site:{site} {q}"
            )

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1200)

                for a in page.locator("a").all():
                    href = a.get_attribute("href") or ""

                    if "flipkart.com" in href or "myntra.com" in href:
                        if "/p/" in href or "/itm" in href:
                            href = href.split("&")[0]
                            found.add(href)

            except:
                pass

    return list(found)

def get_price(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)

        text = page.locator("body").inner_text()

        vals = []
        for m in re.findall(r"(?:₹|Rs\.?)\s*([0-9][0-9,]*)", text):
            try:
                v = float(m.replace(",", ""))
                if 100 <= v <= MAX_PRICE:
                    vals.append(v)
            except:
                pass

        return min(vals) if vals else None
    except:
        return None

def history(page, tracker, product):
    try:
        page.goto(tracker, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1200)

        inputs = page.locator("input").all()

        target = None
        for x in inputs:
            ph = (x.get_attribute("placeholder") or "").lower()
            typ = (x.get_attribute("type") or "").lower()

            if typ in ("text", "search", ""):
                if "link" in ph or "url" in ph or "product" in ph or not ph:
                    target = x
                    break

        if not target:
            return None, None

        target.fill(product)
        target.press("Enter")
        page.wait_for_timeout(2500)

        text = page.locator("body").inner_text()

        current = None
        avg30 = None

        # Current price
        patterns = [
            r"Current\s*(?:Price)?\s*[:\-]?\s*₹\s*([\d,]+)",
            r"Today\s*[:\-]?\s*₹\s*([\d,]+)",
            r"Price\s*[:\-]?\s*₹\s*([\d,]+)"
        ]

        for p in patterns:
            m = re.search(p, text, re.I)
            if m:
                current = float(m.group(1).replace(",", ""))
                break

        # EXACT 30-day average only
        patterns = [
            r"30\s*day\s*Average\s*[:\-]?\s*₹\s*([\d,]+)",
            r"30d\s*Average\s*[:\-]?\s*₹\s*([\d,]+)",
            r"30\s*Days?\s*Average\s*[:\-]?\s*₹\s*([\d,]+)"
        ]

        for p in patterns:
            m = re.search(p, text, re.I)
            if m:
                avg30 = float(m.group(1).replace(",", ""))
                break

        return current, avg30

    except:
        return None, None

def worker(name, tracker, products, results):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for i, product in enumerate(products, 1):
            print(f"{name} [{i}/{len(products)}] {product}")

            current, avg = history(page, tracker, product)

            if current and avg:
                results.append({
                    "url": product,
                    "tracker": name,
                    "current": current,
                    "avg": avg
                })

            time.sleep(0.4)

        browser.close()

def main():
    state = load_state()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print("Discovering Flipkart/Myntra products...")
        products = discover(page)

        browser.close()

    print("Products discovered:", len(products))

    now = datetime.utcnow()

    # Remove products checked during the last 24 hours
    todo = []

    for url in products:
        last = state["checked"].get(url)

        if last:
            try:
                t = datetime.fromisoformat(last)
                if now - t < timedelta(hours=RECHECK_HOURS):
                    continue
            except:
                pass

        todo.append(url)

    print("New products to history-check:", len(todo))

    if not todo:
        print("Nothing new to check.")
        return

    # Split equally between 3 trackers
    buckets = [[], [], []]

    for i, url in enumerate(todo):
        buckets[i % 3].append(url)

    results = [[], [], []]

    import threading

    threads = []

    for i, (name, tracker) in enumerate(TRACKERS):
        t = threading.Thread(
            target=worker,
            args=(name, tracker, buckets[i], results[i])
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    qualified = []

    for group in results:
        for r in group:
            url = r["url"]
            current = r["current"]
            avg = r["avg"]

            state["checked"][url] = now.isoformat()

            if current <= MAX_PRICE and current <= avg * (1 - DROP):
                qualified.append(r)

    print("History results:", sum(len(x) for x in results))
    print("Qualified:", len(qualified))

    # Telegram alerts
    for r in qualified:
        url = r["url"]
        current = r["current"]
        avg = r["avg"]

        drop = (1 - current / avg) * 100

        signature = f"{round(current)}:{round(avg)}"

        if state["alerts"].get(url) == signature:
            continue

        msg = (
            "🚨 BLOCKBUSTER DEAL\n\n"
            f"Current: ₹{current:,.0f}\n"
            f"30-day average: ₹{avg:,.0f}\n"
            f"Below 30d average: {drop:.1f}%\n"
            f"Tracker: {r['tracker']}\n\n"
            f"{url}"
        )

        try:
            send(msg)
            state["alerts"][url] = signature
            print("ALERT:", url)
        except Exception as e:
            print("Telegram error:", e)

    save_state(state)

if __name__ == "__main__":
    main()