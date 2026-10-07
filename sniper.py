import os,re,json,requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

MAX_PRICE=8000
DROP=.45
MAX_ALERTS=10
H={"User-Agent":"Mozilla/5.0"}

S=requests.Session()
S.headers.update(H)

PAGES=[
 "https://www.myntra.com/men-clothing",
 "https://www.myntra.com/women-clothing",
 "https://www.myntra.com/shoes",
 "https://www.myntra.com/electronics",
 "https://www.flipkart.com/search?q=clothing",
 "https://www.flipkart.com/search?q=shoes",
 "https://www.flipkart.com/search?q=electronics"
]

def get(u):
    try:
        r=S.get(u,timeout=15)
        return r if r.ok else None
    except:
        return None

def money(x):
    try:return float(re.sub(r"[^\d.]","",x.replace(",","")))
    except:return 0

def retailer_products(url):
    r=get(url)
    if not r:return []

    s=BeautifulSoup(r.text,"html.parser")
    out=[]

    for a in s.select("a[href]"):
        h=urljoin(url,a.get("href",""))

        if "myntra.com/" in h:
            if re.search(r"/[^/]+/\d+/\d+",h):
                out.append(h.split("?")[0])

        elif "flipkart.com/" in h and "/p/" in h:
            out.append(h.split("?")[0])

    return list(dict.fromkeys(out))

def price(url):
    r=get(url)
    if not r:return 0

    s=BeautifulSoup(r.text,"html.parser")

    # Prefer structured current selling price
    for x in s.select('script[type="application/ld+json"]'):
        try:
            d=json.loads(x.string or "")
            if isinstance(d,list):
                ds=d
            else:
                ds=[d]

            for z in ds:
                if not isinstance(z,dict):continue
                o=z.get("offers",{})
                if isinstance(o,list):
                    o=o[0] if o else {}
                p=o.get("price")
                if p:
                    p=money(str(p))
                    if 0<p<MAX_PRICE:
                        return p
        except:
            pass

    return 0

def tracker(url):
    # PriceHistoryApp public URL lookup
    r=get("https://pricehistoryapp.com/")
    if not r:return None

    s=BeautifulSoup(r.text,"html.parser")

    for f in s.select("form"):
        inp=f.select_one("input")
        if not inp:continue

        name=inp.get("name","url")
        action=urljoin("https://pricehistoryapp.com/",f.get("action",""))

        try:
            q=S.post(
                action,
                data={name:url},
                timeout=15
            )

            if q.ok and "30d Average" in q.text:
                return BeautifulSoup(q.text,"html.parser")
        except:
            pass

    return None

def avg30(s):
    if not s:return 0

    t=s.get_text(" ",strip=True)

    m=re.search(
        r"30d\s*Average\s*₹\s*([\d,]+)",
        t,
        re.I
    )

    return money(m.group(1)) if m else 0

def title(s):
    if not s:return "Deal"

    h=s.select_one("h1")
    if h:return h.get_text(" ",strip=True)

    return s.title.get_text(" ",strip=True) if s.title else "Deal"

def main():

    urls=[]

    for page in PAGES:
        urls+=retailer_products(page)

    urls=list(dict.fromkeys(urls))

    print("Products discovered:",len(urls))

    under=0
    checked=0
    deals=[]

    for i,u in enumerate(urls,1):

        # FIRST FILTER: actual current selling price
        p=price(u)

        if not p or p>=MAX_PRICE:
            continue

        under+=1

        # SECOND STEP: historical-price check
        tr=tracker(u)

        if not tr:
            continue

        checked+=1

        avg=avg30(tr)

        if not avg:
            continue

        drop=(avg-p)/avg

        # FINAL RULE: >=45% below 30-day average
        if drop< DROP:
            continue

        deals.append({
            "url":u,
            "price":p,
            "avg":avg,
            "drop":round(drop*100),
            "title":title(tr)
        })

        print(
            f"🔥 DEAL {round(drop*100)}% | "
            f"₹{p:,.0f} | {title(tr)}"
        )

        if i%25==0:
            print(f"Progress: {i}/{len(urls)}")

    deals.sort(
        key=lambda x:x["drop"],
        reverse=True
    )

    print("Under ₹8,000:",under)
    print("History checked:",checked)
    print("Qualified:",len(deals))

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

        msg=f"""🔥 BLOCKBUSTER DEAL

{x["title"]}

💰 Current: ₹{x["price"]:,.0f}
📊 30-Day Average: ₹{x["avg"]:,.0f}
📉 Below 30-Day Avg: {x["drop"]}%

🛒 BUY NOW:
{x["url"]}"""

        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id":chat,
                    "text":msg
                },
                timeout=10
            )

            state[x["url"]]=1
            sent+=1

        except:
            pass

    with open("state.json","w") as f:
        json.dump(state,f,indent=2)

    print("Alerts sent:",sent)

if __name__=="__main__":
    main()