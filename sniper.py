import os,re,json,requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE="https://pricehistoryapp.com"
FEEDS=["/deals/store/flipkart","/deals/store/myntra"]
DROP=.40
MAX_ALERTS=10
H={"User-Agent":"Mozilla/5.0"}

S=requests.Session()
S.headers.update(H)

def get(url):
    try:
        return S.get(url,timeout=15)
    except:
        return None

def money(x):
    try:
        return float(re.sub(r"[^\d.]","",x.replace(",","")))
    except:
        return 0

def current_price(s):
    t=s.get_text(" ",strip=True)

    patterns=[
        r"Current\s*(?:Price)?\s*[:\-]?\s*₹\s*([\d,]+)",
        r"Today\s*[:\-]?\s*₹\s*([\d,]+)"
    ]

    for p in patterns:
        m=re.search(p,t,re.I)
        if m:
            return money(m.group(1))

    return 0

def avg30(s):
    t=s.get_text(" ",strip=True)
    m=re.search(r"30d\s*Average\s*₹\s*([\d,]+)",t,re.I)
    return money(m.group(1)) if m else 0

def score(s):
    t=s.get_text(" ",strip=True)
    m=re.search(r"Deal\s*Score\s*(\d+)\s*/\s*100",t,re.I)
    return int(m.group(1)) if m else 0

def buy_url(s,page):
    for a in s.select("a[href]"):
        txt=a.get_text(" ",strip=True).lower()
        href=a.get("href","")

        if ("view deal" in txt or "buy" in txt) and (
            "flipkart" in txt or "myntra" in txt
        ):
            return urljoin(page,href)

    for a in s.select("a[href]"):
        href=a.get("href","")
        if "flipkart.com" in href or "myntra.com" in href:
            return href

    return page

def scan():
    deals=[]
    seen=set()

    for feed in FEEDS:
        r=get(BASE+feed)
        if not r:
            continue

        s=BeautifulSoup(r.text,"html.parser")

        for a in s.select("a[href]"):
            href=urljoin(BASE,a["href"])
            text=a.get_text(" ",strip=True)

            if href in seen:
                continue

            if "pricehistoryapp.com" not in href:
                continue

            if not any(x in text.lower() for x in
                       ["flipkart","myntra"]):
                continue

            seen.add(href)

            p=get(href)
            if not p:
                continue

            ps=BeautifulSoup(p.text,"html.parser")

            cur=current_price(ps)
            avg=avg30(ps)
            sc=score(ps)

            if not cur or not avg:
                continue

            drop=(avg-cur)/avg

            # EXACT RULE:
            # current price must be at least 40% below 30-day average
            if drop < DROP:
                continue

            buy=buy_url(ps,href)

            deals.append({
                "title":ps.title.get_text(strip=True)
                        if ps.title else "Deal",
                "cur":cur,
                "avg":avg,
                "drop":round(drop*100),
                "score":sc,
                "url":buy
            })

    return sorted(
        deals,
        key=lambda x:(x["drop"],x["score"]),
        reverse=True
    )

def send(x):
    token=os.getenv("TELEGRAM_BOT_TOKEN")
    chat=os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat:
        return

    msg=f"""🔥 REAL PRICE DROP

{x["title"]}

💰 Current: ₹{x["cur"]:,.0f}
📊 30-Day Average: ₹{x["avg"]:,.0f}
📉 Below 30-Day Avg: {x["drop"]}%
⭐ Deal Score: {x["score"]}/100

🛒 BUY NOW:
{x["url"]}"""

    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id":chat,
            "text":msg,
            "disable_web_page_preview":False
        },
        timeout=10
    )

def main():
    deals=scan()

    try:
        with open("state.json") as f:
            state=json.load(f)
    except:
        state={}

    sent=0

    print("Qualified deals:",len(deals))

    for x in deals[:MAX_ALERTS]:

        key=x["url"]

        if key in state:
            continue

        send(x)
        state[key]=1
        sent+=1

        print(
            f"🔥 {x['drop']}% below 30d avg | "
            f"₹{x['cur']:,.0f} | {x['title']}"
        )

    with open("state.json","w") as f:
        json.dump(state,f,indent=2)

    print("Alerts sent:",sent)

if __name__=="__main__":
    main()