import os,re,json,requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

MAX_PRICE=8000
DROP=.45
MAX_ALERTS=10
H={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36"}

S=requests.Session()
S.headers.update(H)

# Store discovery pages
PAGES=[
    # Myntra clothing
    "https://www.myntra.com/men-clothing?rf=Discount%20Range%3A40.0_100.0_40.0%20TO%20100.0",
    "https://www.myntra.com/women-clothing?rf=Discount%20Range%3A40.0_100.0_40.0%20TO%20100.0",

    # Myntra footwear
    "https://www.myntra.com/men-shoes?rf=Discount%20Range%3A40.0_100.0_40.0%20TO%20100.0",
    "https://www.myntra.com/women-shoes?rf=Discount%20Range%3A40.0_100.0_40.0%20TO%20100.0",

    # Myntra electronics
    "https://www.myntra.com/electronics?rf=Discount%20Range%3A40.0_100.0_40.0%20TO%20100.0",

    # Flipkart broad discounted catalogue
    "https://www.flipkart.com/search?q=clothing&otracker=search&sort=discount_desc",
    "https://www.flipkart.com/search?q=shoes&otracker=search&sort=discount_desc",
    "https://www.flipkart.com/search?q=electronics&otracker=search&sort=discount_desc",
]

def get(u):
    try:
        r=S.get(u,timeout=15)
        return r if r.ok else None
    except:
        return None

def money(x):
    try:
        return float(re.sub(r"[^\d.]","",x.replace(",","")))
    except:
        return 0

def products(page):
    r=get(page)
    if not r:return []

    s=BeautifulSoup(r.text,"html.parser")
    out=[]

    for a in s.select("a[href]"):
        href=urljoin(page,a.get("href",""))
        text=a.get_text(" ",strip=True)

        if "myntra.com/" in href:
            if re.search(r"/[^/]+/\d+/\d+/",href):
                out.append(href.split("?")[0])

        elif "flipkart.com/" in href:
            if "/p/" in href:
                out.append(href.split("?")[0])

    return list(dict.fromkeys(out))

def retailer_price(url):
    r=get(url)
    if not r:return 0

    s=BeautifulSoup(r.text,"html.parser")

    # JSON-LD current selling price
    for x in s.select('script[type="application/ld+json"]'):
        try:
            d=json.loads(x.string or "")
            items=d if isinstance(d,list) else [d]
            for z in items:
                if isinstance(z,dict):
                    o=z.get("offers",{})
                    if isinstance(o,list):o=o[0] if o else {}
                    p=o.get("price")
                    if p:
                        p=float(p)
                        if 0<p<MAX_PRICE:
                            return p
        except:
            pass

    # visible current price
    t=s.get_text(" ",strip=True)
    vals=re.findall(r"₹\s*([\d,]+)",t)

    for v in vals:
        p=money(v)
        if 0<p<MAX_PRICE:
            return p

    return 0

def tracker(url):
    # PriceHistoryApp accepts retailer URLs through its product lookup.
    r=get("https://pricehistoryapp.com/")
    if not r:return None

    s=BeautifulSoup(r.text,"html.parser")

    forms=s.select("form")
    for f in forms:
        inp=f.select_one("input")
        if not inp:continue

        action=urljoin("https://pricehistoryapp.com/",f.get("action",""))
        data={inp.get("name","url"):url}

        try:
            q=S.post(action,data=data,timeout=15)
            if q.ok and "30d Average" in q.text:
                return BeautifulSoup(q.text,"html.parser")
        except:
            pass

    return None

def avg30(s):
    if not s:return 0
    t=s.get_text(" ",strip=True)
    m=re.search(r"30d\s*Average\s*₹\s*([\d,]+)",t,re.I)
    return money(m.group(1)) if m else 0

def title(s):
    if not s:return "Deal"
    h=s.select_one("h1")
    if h:return h.get_text(" ",strip=True)
    return s.title.get_text(" ",strip=True) if s.title else "Deal"

def main():
    urls=[]

    for p in PAGES:
        urls += products(p)

    urls=list(dict.fromkeys(urls))
    print("Products discovered:",len(urls))

    deals=[]

    for i,u in enumerate(urls,1):
        price=retailer_price(u)

        # IMPORTANT: current selling price, NOT MRP
        if not price or price>=MAX_PRICE:
            continue

        tr=tracker(u)
        avg=avg30(tr)

        if not avg:
            continue

        drop=(avg-price)/avg

        if drop>=DROP:
            deals.append({
                "url":u,
                "price":price,
                "avg":avg,
                "drop":round(drop*100),
                "title":title(tr)
            })

        if i%25==0:
            print(f"Checked {i}/{len(urls)}")

    deals.sort(key=lambda x:x["drop"],reverse=True)

    try:
        with open("state.json") as f:
            state=json.load(f)
    except:
        state={}

    token=os.getenv("TELEGRAM_BOT_TOKEN")
    chat=os.getenv("TELEGRAM_CHAT_ID")

    sent=0

    for x in deals[:MAX_ALERTS]:
        if x["url"] in state:
            continue

        msg=f"""🔥 REAL DEAL — {x["drop"]}% BELOW 30D AVG

{x["title"]}

💰 Current: ₹{x["price"]:,.0f}
📊 30-Day Average: ₹{x["avg"]:,.0f}
📉 Historical Drop: {x["drop"]}%

🛒 BUY NOW:
{x["url"]}"""

        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id":chat,"text":msg},
                timeout=10
            )
            state[x["url"]]=1
            sent+=1
        except:
            pass

    with open("state.json","w") as f:
        json.dump(state,f,indent=2)

    print("Qualified:",len(deals))
    print("Alerts sent:",sent)

if __name__=="__main__":
    main()