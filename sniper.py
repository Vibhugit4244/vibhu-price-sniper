import os,re,json,time
from urllib.parse import quote_plus,urljoin
import requests
from playwright.sync_api import sync_playwright

MAX_PRICE=8000
MAX_PAGES=5
DROP=0.45
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
CHAT=os.getenv("TELEGRAM_CHAT_ID")
STATE="state.json"

QUERIES=[
("clothing","men clothing"),("clothing","women clothing"),
("footwear","shoes"),("footwear","sneakers"),("footwear","running shoes"),
("electronics","earbuds"),("electronics","headphones"),
("electronics","smartwatch"),("electronics","speaker"),
("electronics","keyboard"),("electronics","mouse"),("electronics","monitor")
]

def price(s):
    m=re.findall(r"₹\s*([\d,]+)",s or "")
    return float(m[0].replace(",","")) if m else None

def clean(u):
    return u.split("?")[0].rstrip("/")

def telegram(msg):
    if not TOKEN or not CHAT:return False
    r=requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id":CHAT,"text":msg},
        timeout=20
    )
    return r.ok

def load():
    try:
        with open(STATE) as f:return json.load(f)
    except:return {"sent":{}}

def discover(page,store,query,cat):
    out={}
    for n in range(1,MAX_PAGES+1):
        if store=="Myntra":
            u=f"https://www.myntra.com/search?rawQuery={quote_plus(query)}&p={n}"
            selector='a[href*="/buy"],a[href*="/product/"]'
        else:
            u=f"https://www.flipkart.com/search?q={quote_plus(query)}&page={n}"
            selector='a[href*="/p/"]'
        print(f"{store} | {query} | Page {n}")
        try:
            page.goto(u,wait_until="domcontentloaded",timeout=30000)
            page.wait_for_timeout(1800)
            links=page.locator(selector)
            for i in range(links.count()):
                try:
                    a=links.nth(i)
                    href=a.get_attribute("href")
                    if not href:continue
                    href=clean(urljoin(
                        "https://www.myntra.com" if store=="Myntra"
                        else "https://www.flipkart.com",href))
                    if href in out:continue
                    txt=a.inner_text(timeout=1500)
                    p=price(txt)
                    if p is None:
                        try:p=price(a.locator("xpath=..").inner_text(timeout=1500))
                        except:pass
                    if p and p<MAX_PRICE:
                        out[href]={"store":store,"category":cat,
                                   "title":re.sub(r"\s+"," ",txt)[:180],
                                   "price":p,"url":href}
                except:pass
        except Exception as e:print("Page error:",str(e)[:100])
    return out

def history(page,url):
    try:
        page.goto("https://pricehistoryapp.com/",wait_until="domcontentloaded",timeout=30000)
        page.wait_for_timeout(1000)
        ins=page.locator("input")
        box=ins.nth(0)
        box.fill(url)
        box.press("Enter")
        page.wait_for_timeout(2500)
        txt=page.locator("body").inner_text(timeout=10000)
        a=re.search(r"30d\s+Average\s*₹?\s*([\d,]+)",txt,re.I)
        c=re.search(r"Current:\s*₹?\s*([\d,]+)",txt,re.I)
        if not(a and c):return None
        return float(c.group(1).replace(",","")),float(a.group(1).replace(",","")),page.url
    except Exception as e:
        print("History error:",str(e)[:100])
        return None

def main():
    print("=== VIBHU PRICE SNIPER ===")
    print(f"Rule: current < ₹{MAX_PRICE:,} AND ≥45% below 30-day average")
    state=load()
    products={}

    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=["--no-sandbox"])
        ctx=browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
            locale="en-IN"
        )
        page=ctx.new_page()

        for store in ["Myntra","Flipkart"]:
            for cat,q in QUERIES:
                for u,x in discover(page,store,q,cat).items():
                    products[u]=x

        print(f"\nProducts discovered: {len(products)}")
        products=[x for x in products.values() if x["price"]<MAX_PRICE]
        print(f"Under ₹8,000: {len(products)}")

        hp=ctx.new_page()
        checked=qualified=alerts=0

        for x in products:
            checked+=1
            print(f"[{checked}/{len(products)}] {x['store']} ₹{x['price']:.0f}")

            h=history(hp,x["url"])
            if not h:continue

            current,avg,hurl=h
            if current>=MAX_PRICE or avg<=0:continue

            drop=(1-current/avg)*100
            if drop<45:continue

            qualified+=1
            key=x["url"]
            sig=[round(current,2),round(avg,2)]

            if state["sent"].get(key)==sig:
                continue

            msg=(
                "🚨 BLOCKBUSTER DEAL 🚨\n\n"
                f"🛍 {x['store']}\n"
                f"📦 {x['title']}\n\n"
                f"💰 Current: ₹{current:,.0f}\n"
                f"📊 30-day average: ₹{avg:,.0f}\n"
                f"🔥 {drop:.1f}% below average\n\n"
                f"🛒 {x['url']}\n"
                f"📈 {hurl}"
            )

            if telegram(msg):
                state["sent"][key]=sig
                with open(STATE,"w") as f:json.dump(state,f,indent=2)
                alerts+=1
                print("🔥 ALERT SENT")

        browser.close()

    print("\n=== FINAL RESULT ===")
    print("Products discovered:",len(products))
    print("History checked:",checked)
    print("Qualified:",qualified)
    print("Alerts sent:",alerts)

if __name__=="__main__":
    main()