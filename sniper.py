import os, re, json, requests
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

MAX_PRICE = 8000
DROP_RATIO = 0.55
BATCH_SIZE = 60
RECHECK_HOURS = 24
STATE_FILE = "state.json"

BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]

SOURCES = [
("flipkart","https://www.flipkart.com/search?q=men+shirts"),
("flipkart","https://www.flipkart.com/search?q=men+tshirts"),
("flipkart","https://www.flipkart.com/search?q=men+jeans"),
("flipkart","https://www.flipkart.com/search?q=women+tops"),
("flipkart","https://www.flipkart.com/search?q=women+dresses"),
("flipkart","https://www.flipkart.com/search?q=women+jeans"),
("flipkart","https://www.flipkart.com/search?q=shoes"),
("flipkart","https://www.flipkart.com/search?q=sneakers"),
("flipkart","https://www.flipkart.com/search?q=footwear"),
("flipkart","https://www.flipkart.com/search?q=earbuds"),
("flipkart","https://www.flipkart.com/search?q=headphones"),
("flipkart","https://www.flipkart.com/search?q=smartwatch"),
("flipkart","https://www.flipkart.com/search?q=laptop"),
("flipkart","https://www.flipkart.com/search?q=monitor"),
("flipkart","https://www.flipkart.com/search?q=electronics"),
("myntra","https://www.myntra.com/men-shirts"),
("myntra","https://www.myntra.com/men-tshirts"),
("myntra","https://www.myntra.com/men-jeans"),
("myntra","https://www.myntra.com/women-tops"),
("myntra","https://www.myntra.com/women-dresses"),
("myntra","https://www.myntra.com/women-jeans"),
("myntra","https://www.myntra.com/shoes"),
("myntra","https://www.myntra.com/sneakers"),
("myntra","https://www.myntra.com/footwear"),
("myntra","https://www.myntra.com/bags"),
]

def load():
    try:
        with open(STATE_FILE) as f:
            s=json.load(f)
    except:
        s={}
    if not isinstance(s,dict): s={}
    s.setdefault("queue",[])
    s.setdefault("checked",{})
    s.setdefault("alerts",{})
    return s

def save(s):
    with open(STATE_FILE,"w") as f:
        json.dump(s,f,indent=2)

def telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={"chat_id":CHAT,"text":msg},
            timeout=15
        )
    except Exception as e:
        print("Telegram:",e)

def money(x):
    if not x: return None
    x=x.replace(",","").replace("₹","").strip()
    m=re.search(r"\d+(?:\.\d+)?",x)
    return float(m.group()) if m else None

def retailer_price(page,store,url):
    try:
        page.goto(url,wait_until="domcontentloaded",timeout=25000)
        page.wait_for_timeout(1200)

        text=page.locator("body").inner_text()

        vals=[]

        for x in re.findall(r"₹\s*[\d,]+(?:\.\d+)?",text):
            p=money(x)
            if p and 50 <= p < MAX_PRICE:
                vals.append(p)

        if store=="myntra":
            for sel in [
                '[class*="pdp-price"]',
                '[class*="selling-price"]',
                '[class*="discounted-price"]'
            ]:
                try:
                    for el in page.locator(sel).all():
                        p=money(el.inner_text())
                        if p and 50 <= p < MAX_PRICE:
                            vals.append(p)
                except:
                    pass

        if store=="flipkart":
            for sel in [
                '[class*="Nx9bqj"]',
                '[class*="CxhGGd"]',
                '[class*="dyC4hf"]'
            ]:
                try:
                    for el in page.locator(sel).all():
                        p=money(el.inner_text())
                        if p and 50 <= p < MAX_PRICE:
                            vals.append(p)
                except:
                    pass

        if not vals:
            return None

        return min(vals)

    except Exception as e:
        print("Retailer error:",e)
        return None

def discover(page):
    found={}

    for store,url in SOURCES:
        print("DISCOVER:",store,url)
        try:
            page.goto(url,wait_until="domcontentloaded",timeout=30000)
            page.wait_for_timeout(1200)

            for _ in range(5):
                page.mouse.wheel(0,2200)
                page.wait_for_timeout(300)

            for a in page.locator("a").all():
                h=a.get_attribute("href") or ""
                if not h: continue

                if h.startswith("/"):
                    h="https://www."+store+".com"+h

                if store=="flipkart":
                    if "flipkart.com" not in h or "/p/" not in h:
                        continue
                else:
                    if "myntra.com" not in h or "/buy/" not in h:
                        continue

                h=h.split("?")[0]

                if h not in found:
                    found[h]=store

        except Exception as e:
            print("Discovery error:",e)

    return found

def history(page,retailer_url):
    try:
        # PriceHistoryApp homepage
        page.goto(
            "https://pricehistoryapp.com/",
            wait_until="domcontentloaded",
            timeout=25000
        )
        page.wait_for_timeout(800)

        inputs=page.locator("input").all()
        box=None

        for x in inputs:
            ph=(x.get_attribute("placeholder") or "").lower()
            typ=(x.get_attribute("type") or "").lower()

            if (
                "paste" in ph or
                "product" in ph or
                "link" in ph or
                typ=="url"
            ):
                box=x
                break

        if box is None:
            print("HISTORY BOX NOT FOUND")
            return None

        box.fill(retailer_url)

        # Click the actual Track Price button.
        buttons=page.locator("button").all()
        clicked=False

        for b in buttons:
            try:
                t=(b.inner_text() or "").strip().lower()
                if "track" in t or "check" in t:
                    b.click()
                    clicked=True
                    break
            except:
                pass

        if not clicked:
            box.press("Enter")

        page.wait_for_timeout(2500)

        # If a product page was produced, use that page.
        text=page.locator("body").inner_text()

        if "/product/" not in page.url:
            links=page.locator("a").all()

            for a in links:
                h=a.get_attribute("href") or ""
                if "/product/" not in h:
                    continue

                if h.startswith("/"):
                    h="https://pricehistoryapp.com"+h

                try:
                    page.goto(
                        h,
                        wait_until="domcontentloaded",
                        timeout=20000
                    )
                    page.wait_for_timeout(800)
                    text=page.locator("body").inner_text()

                    if "30d Average" in text:
                        break
                except:
                    pass

        # IMPORTANT:
        # We only accept a real PriceHistoryApp product page.
        if "/product/" not in page.url:
            print("NO PRODUCT PAGE:",retailer_url)
            return None

        avg=None

        m=re.search(
            r"30d\s*Average\s*[:₹\s]*([\d,]+(?:\.\d+)?)",
            text,
            re.I
        )

        if m:
            avg=money(m.group(1))

        if not avg or avg <= 0:
            print("NO 30D AVG:",page.url)
            return None

        # Try to get product title for identity checking.
        title=""
        try:
            title=page.locator("h1").first.inner_text().strip()
        except:
            pass

        return {
            "average30":avg,
            "history_url":page.url,
            "title":title
        }

    except Exception as e:
        print("History error:",e)
        return None

def main():
    state=load()
    now=datetime.utcnow()

    with sync_playwright() as p:
        browser=p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"]
        )

        context=browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36"
            )
        )

        page=context.new_page()

        # -------------------------------------------------
        # 1. DISCOVER
        # -------------------------------------------------
        products=discover(page)

        print("DISCOVERED:",len(products))

        known=set(state["queue"])

        # -------------------------------------------------
        # 2. FILTER BY LIVE RETAILER PRICE
        # -------------------------------------------------
        for url,store in products.items():

            old=state["checked"].get(url)

            if old:
                try:
                    last=datetime.fromisoformat(old["time"])
                    if now-last < timedelta(hours=RECHECK_HOURS):
                        continue
                except:
                    pass

            if url in known:
                continue

            price=retailer_price(page,store,url)

            if price is None:
                print("PRICE FAILED:",url)
                continue

            print("LIVE:",round(price),url)

            # Current retailer price must be below ₹8,000.
            if price >= MAX_PRICE:
                continue

            state["queue"].append({
                "url":url,
                "store":store,
                "price":price
            })

            known.add(url)

        save(state)

        print("QUEUE:",len(state["queue"]))

        # -------------------------------------------------
        # 3. HISTORY CHECK
        # -------------------------------------------------
        batch=state["queue"][:BATCH_SIZE]

        print("HISTORY BATCH:",len(batch))

        successful=[]

        for i,item in enumerate(batch,1):

            url=item["url"]
            current=item["price"]

            print(f"[{i}/{len(batch)}] HISTORY:",url)

            h=history(page,url)

            if not h:
                continue

            avg=h["average30"]

            print(
                "CURRENT:",round(current),
                "AVG30:",round(avg)
            )

            state["checked"][url]={
                "time":now.isoformat(),
                "current":current,
                "average30":avg,
                "history_url":h["history_url"]
            }

            successful.append(item)

            # FINAL DEAL RULE
            if current > avg * DROP_RATIO:
                continue

            drop=(1-current/avg)*100

            signature=f"{round(current)}:{round(avg)}"

            if state["alerts"].get(url)==signature:
                continue

            telegram(
                "🚨 BLOCKBUSTER DEAL\n\n"
                f"Current price: ₹{current:,.0f}\n"
                f"30-day average: ₹{avg:,.0f}\n"
                f"Below 30-day average: {drop:.1f}%\n\n"
                f"{url}"
            )

            state["alerts"][url]=signature

            print("🔥 ALERT:",url)

        # -------------------------------------------------
        # 4. REMOVE SUCCESSFULLY CHECKED PRODUCTS
        # -------------------------------------------------
        done={x["url"] for x in successful}

        state["queue"]=[
            x for x in state["queue"]
            if x["url"] not in done
        ]

        save(state)

        browser.close()

    print()
    print("==============================")
    print("RUN COMPLETE")
    print("Catalogue:",len(products))
    print("History checked:",len(successful))
    print("Queue remaining:",len(state["queue"]))
    print("==============================")

if __name__=="__main__":
    main()