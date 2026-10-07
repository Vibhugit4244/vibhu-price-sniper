import os,re,json,time,requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor,as_completed
from urllib.parse import urljoin,urlparse

MAX_RATIO=.50
MAX_ALERTS=10
WORKERS=10
TIMEOUT=12
STATE="state.json"
BOT=os.getenv("TELEGRAM_BOT_TOKEN")
CHAT=os.getenv("TELEGRAM_CHAT_ID")

SOURCES=[
"https://pricehistoryapp.com/deals",
"https://pricehistoryapp.com/latest-deals",
"https://pricehistoryapp.com/deals/store/flipkart",
"https://pricehistoryapp.com/deals/store/myntra",
"https://pricehistoryapp.com/featured-deals",
"https://pricedropy.com/flipkart-price-history",
"https://pricedropy.com/myntra-price-history"
]

HEAD={"User-Agent":"Mozilla/5.0 Chrome/131 Safari/537.36"}

CATS={
"Clothing":"shirt tshirt jeans trouser pants jacket hoodie dress kurta kurti saree blazer cargo shorts clothing",
"Footwear":"shoe shoes sneaker sneakers slider sliders sandal sandals boots loafer clog footwear",
"Electronics":"earbud earbuds headphone headset speaker laptop computer monitor keyboard mouse tablet mobile phone smartphone charger powerbank ssd camera tv television printer router smartwatch watch trimmer gaming electronic"
}

def get(u,t=TIMEOUT):
    try:
        r=requests.get(u,headers=HEAD,timeout=t)
        return r.text if r.status_code==200 else ""
    except: return ""

def store(u):
    h=urlparse(u).netloc.lower()
    if "flipkart.com" in h:return"Flipkart"
    if "myntra.com" in h:return"Myntra"
    return""

def money(x):
    m=re.search(r"(\d[\d,]*(?:\.\d+)?)",str(x or ""))
    return float(m.group(1).replace(",","")) if m else None

def ld(s):
    out=[]
    for x in s.find_all("script",type="application/ld+json"):
        try:
            y=json.loads(x.string or x.get_text())
            out+=y if isinstance(y,list) else[y]
        except:pass
    return out

def price(s):
    for x in ld(s):
        if isinstance(x,dict):
            o=x.get("offers")
            if isinstance(o,list):o=o[0] if o else None
            if isinstance(o,dict) and money(o.get("price")):
                return money(o["price"])
            if money(x.get("price")):return money(x["price"])
    x=s.find(attrs={"itemprop":"price"})
    return money(x.get("content") or x.get_text()) if x else None

def name(s):
    for x in ld(s):
        if isinstance(x,dict) and x.get("name"):
            return str(x["name"]).strip()
    x=s.find("meta",property="og:title")
    return x.get("content","").strip() if x else (
        s.title.get_text(strip=True) if s.title else "Unknown"
    )

def avg(s):
    t=s.get_text(" ",strip=True)
    for p in[
        r"(?:30|90|180|365)[-\s]?day\s+average[^₹]*₹\s*([\d,]+)",
        r"historical\s+average[^₹]*₹\s*([\d,]+)",
        r"average\s+price[^₹]*₹\s*([\d,]+)",
        r"average[^₹]*₹\s*([\d,]+)"
    ]:
        m=re.search(p,t,re.I)
        if m:return money(m.group(1))
    return None

def cat(n):
    n=n.lower()
    for c,w in CATS.items():
        if any(x in n for x in w.split()):
            return c

def buy(s,u):
    for a in s.find_all("a",href=True):
        x=urljoin(u,a["href"])
        if store(x):
            return x
    return ""

def check(u):
    p=get(u)
    if not p:return
    s=BeautifulSoup(p,"html.parser")
    n=name(s); a=avg(s); tracked=price(s); b=buy(s,u)
    if not b or not a or not tracked:return
    st=store(b); c=cat(n)
    if st not in("Flipkart","Myntra") or not c:return

    rp=get(b)
    actual=None
    if rp:
        actual=price(BeautifulSoup(rp,"html.parser"))

    cur=actual or tracked

    if not cur or cur<=0 or cur/a>MAX_RATIO:return

    return {
        "name":n,"cat":c,"store":st,
        "current":cur,"avg":a,
        "discount":(1-cur/a)*100,
        "buy":b
    }

def discover():
    out=set()
    for u in SOURCES:
        p=get(u)
        if not p:continue
        s=BeautifulSoup(p,"html.parser")
        for a in s.find_all("a",href=True):
            x=urljoin(u,a["href"]).split("#")[0]
            if any(z in urlparse(x).path.lower()
                   for z in["/product/","/products/",
                             "/price-history/","/track/","/tracker/"]):
                out.add(x)
    return list(out)

def send(x):
    try:
        r=requests.post(
            f"https://api.telegram.org/bot{BOT}/sendMessage",
            data={"chat_id":CHAT,"text":x,
                  "disable_web_page_preview":False},
            timeout=15)
        return r.ok
    except:return False

def load():
    try:
        return json.load(open(STATE))
    except:return {}

def save(x):
    json.dump(x,open(STATE,"w"),indent=2)

def main():
    urls=discover()
    print("Discovered:",len(urls))

    deals=[]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fs=[ex.submit(check,u) for u in urls]
        for f in as_completed(fs):
            try:
                x=f.result()
                if x:deals.append(x)
            except:pass

    deals.sort(key=lambda x:x["discount"],reverse=True)

    print("Real deals:",len(deals))

    state=load()

    for d in deals[:MAX_ALERTS]:
        key=d["store"]+"|"+d["buy"]
        if key in state:continue

        msg=(
            f"🔥 {d['discount']:.0f}% BELOW HISTORY\n\n"
            f"🛍️ {d['name']}\n"
            f"📂 {d['cat']}\n"
            f"🏪 {d['store']}\n\n"
            f"💰 Now: ₹{d['current']:,.0f}\n"
            f"📊 Historical avg: ₹{d['avg']:,.0f}\n"
            f"📉 Drop: {d['discount']:.0f}%\n\n"
            f"🛒 BUY NOW:\n{d['buy']}"
        )

        if send(msg):
            state[key]={"sent":int(time.time())}
            print("Sent:",d["name"][:60])

    save(state)
    print("Done")

if __name__=="__main__":
    main()