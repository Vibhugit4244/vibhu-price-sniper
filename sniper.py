import os, re, json, time
from urllib.parse import quote, urlparse
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

MAX_PRICE = 8000
DROP_RATIO = 0.55
BATCH_SIZE = 40
RECHECK_HOURS = 24
STATE_FILE = "state.json"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SEARCHES = [
    ("flipkart", "https://www.flipkart.com/search?q=",
     ["men shirts", "men t shirts", "men jeans",
      "men trousers", "women tops", "women shirts",
      "women dresses", "women jeans", "women trousers",
      "shoes", "sneakers", "sports shoes", "sandals",
      "women footwear", "men footwear"]),
    ("myntra", "https://www.myntra.com/",
     ["men-shirts", "men-tshirts", "men-jeans",
      "men-trousers", "women-tops", "women-shirts",
      "women-dresses", "women-jeans", "women-trousers",
      "men-shoes", "women-shoes", "sneakers",
      "sports-shoes", "sandals", "flats", "heels"])
]

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("queue", {})
    s.setdefault("checked", {})
    s.setdefault("sent", [])
    return s

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)

def telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram secrets missing")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": message,
              "disable_web_page_preview": False},
        timeout=20
    )
    r.raise_for_status()

def valid_product_url(url, store):
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        if store == "flipkart":
            return "flipkart.com" in host and "/p/" in p.path
        return "myntra.com" in host and "/buy/" in p.path
    except Exception:
        return False

def discover(page, state):
    found = 0
    for store, base, terms in SEARCHES:
        for term in terms:
            url = base + quote(term) if store == "flipkart" else base + term
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=35000)
                page.wait_for_timeout(1800)
                links = page.locator("a[href]").evaluate_all(
                    "(els) => els.map(a => a.href)"
                )
                for link in links:
                    link = link.split("?")[0]
                    if valid_product_url(link, store):
                        key = link.rstrip("/")
                        if key not in state["queue"]:
                            state["queue"][key] = {
                                "url": key, "store": store, "added": time.time()
                            }
                            found += 1
            except Exception as e:
                print("DISCOVERY ERROR:", store, term, str(e)[:160])
            save_state(state)
    print("DISCOVERED:", found)

def money(value):
    if value is None:
        return None
    try:
        value = str(value).replace(",", "")
        m = re.search(r"\d+(?:\.\d{1,2})?", value)
        if not m:
            return None
        n = float(m.group())
        return n if 1 <= n <= 1000000 else None
    except Exception:
        return None

def retailer_price(page, store, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(1200)

        # Prefer structured product offers, not arbitrary page prices.
        data = page.locator('script[type="application/ld+json"]').all_text_contents()
        for raw in data:
            try:
                obj = json.loads(raw)
                objects = obj if isinstance(obj, list) else [obj]
                for item in objects:
                    if not isinstance(item, dict):
                        continue
                    if item.get("@type") == "Product":
                        offers = item.get("offers", {})
                        offers = offers if isinstance(offers, list) else [offers]
                        for offer in offers:
                            if isinstance(offer, dict):
                                p = money(offer.get("price"))
                                if p:
                                    return p
                    graph = item.get("@graph", [])
                    for product in graph:
                        if isinstance(product, dict) and product.get("@type") == "Product":
                            offers = product.get("offers", {})
                            offers = offers if isinstance(offers, list) else [offers]
                            for offer in offers:
                                if isinstance(offer, dict):
                                    p = money(offer.get("price"))
                                    if p:
                                        return p
            except Exception:
                pass

        selectors = (
            ['meta[property="product:price:amount"]',
             'meta[itemprop="price"]',
             'div.Nx9bqj']
            if store == "flipkart" else
            ['meta[property="product:price:amount"]',
             'meta[itemprop="price"]',
             'span.pdp-price strong',
             'div.pdp-price']
        )

        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count():
                    value = loc.get_attribute("content") or loc.inner_text()
                    p = money(value)
                    if p and p < 100000:
                        return p
            except Exception:
                pass

    except Exception as e:
        print("LIVE PRICE ERROR:", str(e)[:150])

    return None

def history_lookup(page, retailer_url):
    try:
        page.goto("https://pricehistoryapp.com/",
                  wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(1000)

        inputs = page.locator("input")
        if not inputs.count():
            return None

        field = None
        for i in range(inputs.count()):
            el = inputs.nth(i)
            hint = ((el.get_attribute("placeholder") or "") + " " +
                    (el.get_attribute("aria-label") or "")).lower()
            if "link" in hint or "url" in hint or "product" in hint:
                field = el
                break
        if field is None:
            field = inputs.first

        field.fill(retailer_url)

        buttons = page.locator("button")
        clicked = False
        for i in range(buttons.count()):
            b = buttons.nth(i)
            text = (b.inner_text() or "").strip().lower()
            if "track" in text or "check" in text or "search" in text:
                b.click()
                clicked = True
                break
        if not clicked:
            field.press("Enter")

        page.wait_for_timeout(3500)

        # Follow a product-history result if the form returns a result link.
        if "/product/" not in page.url:
            links = page.locator('a[href*="/product/"]')
            if links.count():
                href = links.first.get_attribute("href")
                if href:
                    if href.startswith("/"):
                        href = "https://pricehistoryapp.com" + href
                    page.goto(href, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1200)

        if "/product/" not in page.url:
            return None

        text = page.locator("body").inner_text(timeout=10000)

        def labelled(pattern):
            m = re.search(pattern, text, re.I)
            return money(m.group(1)) if m else None

        avg = labelled(
            r"30\s*[- ]?\s*day\s*average[^₹\d]{0,30}₹?\s*([\d,]+(?:\.\d+)?)"
        )
        low = labelled(
            r"all[\s-]*time\s*low[^₹\d]{0,30}₹?\s*([\d,]+(?:\.\d+)?)"
        )

        if not avg:
            return None

        return {
            "avg30": avg,
            "low": low,
            "history_url": page.url
        }

    except Exception as e:
        print("HISTORY ERROR:", str(e)[:150])
        return None

def main():
    state = load_state()
    now = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            locale="en-IN"
        )
        page = context.new_page()

        discover(page, state)

        # Recheck products only after the cooldown period.
        items = list(state["queue"].items())
        ready = []
        for key, item in items:
            last = state["checked"].get(key, 0)
            if now - last >= RECHECK_HOURS * 3600:
                ready.append((key, item))

        print("QUEUE:", len(state["queue"]), "READY:", len(ready))

        # Process a limited batch; remaining products persist for later runs.
        for index, (key, item) in enumerate(ready[:BATCH_SIZE], 1):
            url = item["url"]
            store = item["store"]
            print(f"[{index}/{min(len(ready), BATCH_SIZE)}] {store}: {url}")

            current = retailer_price(page, store, url)
            if not current:
                print("SKIP: live price uncertain")
                state["checked"][key] = now
                save_state(state)
                continue

            if current >= MAX_PRICE:
                print("SKIP: price above limit")
                state["checked"][key] = now
                save_state(state)
                continue

            history = history_lookup(page, url)
            if not history:
                print("SKIP: exact price history unavailable")
                state["checked"][key] = now
                save_state(state)
                continue

            avg = history["avg30"]
            if avg <= 0:
                state["checked"][key] = now
                save_state(state)
                continue

            drop = (avg - current) / avg * 100
            low = history.get("low")
            low_text = f"₹{low:,.0f}" if low else "Unavailable"

            print(f"PRICE ₹{current:,.0f} | AVG ₹{avg:,.0f} | DROP {drop:.1f}%")

            state["checked"][key] = now

            if current <= DROP_RATIO * avg:
                if drop >= 75:
                    band = "💥 UNPRECEDENTED DEAL"
                elif drop >= 65:
                    band = "🔥 EXCEPTIONAL DEAL"
                elif drop >= 55:
                    band = "🚨 VERY STRONG DEAL"
                else:
                    band = "⚡ STRONG DEAL"

                message = (
                    f"{band}\n\n"
                    f"Store: {store.upper()}\n"
                    f"Current price: ₹{current:,.0f}\n"
                    f"30-day average: ₹{avg:,.0f}\n"
                    f"Below average: {drop:.1f}%\n"
                    f"All-time low: {low_text}\n\n"
                    f"Retailer: {url}\n"
                    f"Price history: {history['history_url']}"
                )

                try:
                    telegram(message)
                    print("🚨 ALERT SENT")
                    state["sent"].append(key)
                except Exception as e:
                    print("TELEGRAM ERROR:", str(e)[:150])

            save_state(state)

        browser.close()
    save_state(state)

if __name__ == "__main__":
    main()