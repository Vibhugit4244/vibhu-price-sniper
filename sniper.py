import os,re,json,time,asyncio,traceback,requests
from urllib.parse import quote,urlparse
from playwright.async_api import async_playwright

MAX_PRICE=8000
MIN_DROP=45
MIN_RATING=4.0
BATCH_SIZE=150
MAX_CONCURRENT=6
RECHECK_HOURS=6
STATE_FILE="state.json"
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")

FLIPKART_TERMS=["men shirts","men t shirts","men jeans","men trousers","men pants","men shoes","sneakers","sports shoes","sandals"]
MYNTRA_TERMS=["men-shirts","men-tshirts","men-jeans","men-trousers","men-pants","men-shoes","sneakers","sports-shoes","sandals"]

WOMEN={"women","woman","womens","women's","ladies","lady","female","girls","girl","kurti","kurtis","saree","sari","salwar","dupatta","lehenga","anarkali","gown","skirt","bralette","maternity","nighty","nightdress","shapewear"}

def store(url):
 h=urlparse(url).netloc.lower()
 return "flipkart" if "flipkart.com" in h else "myntra" if "myntra.com" in h else None

def clean(url):
 try:
  p=urlparse(url)
  return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/") if p.scheme and p.netloc else None
 except:return None

def is_product(url):
 u=clean(url)
 if not u:return False
 p=urlparse(u).path;s=store(u)
 return "/p/" in p if s=="flipkart" else "/buy/" in p if s=="myntra" else False

def get_price(v):
 try:
  m=re.search(r"(?:₹|Rs\.?\s*)?\s*([\d,]+(?:\.\d+)?)",str(v))
  n=float(m.group(1).replace(",","")) if m else 0
  return n if 1<=n<=1000000 else None
 except:return None

def save(s):
 try:
  with open(STATE_FILE+".tmp","w") as f:json.dump(s,f,indent=2)
  os.replace(STATE_FILE+".tmp",STATE_FILE)
 except Exception as e:print("STATE:",e)

def load():
 try:
  with open(STATE_FILE) as f:r=json.load(f)
 except:r={}
 q={}
 for k,v in (r.get("queue",{}) if isinstance(r.get("queue",{}),dict) else {}).items():
  u=v.get("url",k) if isinstance(v,dict) else k
  if u and is_product(u):q[u]={"url":u,"store":store(u),"added":v.get("added",time.time()) if isinstance(v,dict) else time.time()}
 return {"queue":q,"checked":r.get("checked",{}) if isinstance(r.get("checked",{}),dict) else {},"sent":r.get("sent",[]) if isinstance(r.get("sent",[]),list) else []}

def telegram(msg):
 if not TOKEN or not CHAT_ID:return False
 try:
  r=requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data={"chat_id":CHAT_ID,"text":msg},timeout=20)
  r.raise_for_status();return True
 except Exception as e:print("TELEGRAM:",e);return False

async def discover_search(page,s,url,state):
 try:
  await page.goto(url,wait_until="domcontentloaded",timeout=30000)
  await page.wait_for_timeout(800)
  links=await page.locator("a[href]").evaluate_all("(x)=>x.map(a=>a.href)")
  n=0
  for raw in links:
   u=clean(raw)
   if u and is_product(u) and store(u)==s and u not in state["queue"]:
    state["queue"][u]={"url":u,"store":s,"added":time.time()};n+=1
  return n
 except Exception as e:print("DISCOVERY:",e);return 0

async def discover(page,state):
 for terms,s,base in [(FLIPKART_TERMS,"flipkart","https://www.flipkart.com/search?q="),(MYNTRA_TERMS,"myntra","https://www.myntra.com/")]:
  for t in terms:
   n=await discover_search(page,s,base+quote(t),state)
   print(s,t,n)
   save(state)

async def info(page,url):
 try:
  await page.goto(url,wait_until="domcontentloaded",timeout=25000)
  await page.wait_for_timeout(400)
  title=await page.title()
  try:
   h=page.locator("h1").first
   if await h.count():title=(await h.inner_text()).strip() or title
  except:pass

  price_now=None;rating=None
  scripts=await page.locator('script[type="application/ld+json"]').all_text_contents()

  for raw in scripts:
   try:d=json.loads(raw)
   except:continue
   objs=d if isinstance(d,list) else [d]
   if isinstance(d,dict) and isinstance(d.get("@graph"),list):objs+=d["@graph"]
   for o in objs:
    if not isinstance(o,dict):continue
    typ=o.get("@type")
    if typ=="Product" or isinstance(typ,list) and "Product" in typ:
     offers=o.get("offers",[])
     offers=offers if isinstance(offers,list) else [offers]
     vals=[get_price(x.get("price")) for x in offers if isinstance(x,dict)]
     vals=[x for x in vals if x]
     if vals:price_now=min(vals)
     a=o.get("aggregateRating")
     if isinstance(a,dict):
      try:rating=float(a.get("ratingValue"))
      except:pass

  if not price_now:
   for sel in ['meta[property="product:price:amount"]','meta[property="og:price:amount"]','meta[itemprop="price"]']:
    x=page.locator(sel).first
    if await x.count():
     price_now=get_price(await x.get_attribute("content"))
     if price_now:break

  return title,price_now,rating
 except Exception as e:print("PRODUCT:",e);return None,None,None

def history_values(txt):
 avg=low=None
 for p in [r"30\s*day\s*(?:average|avg).{0,100}?₹\s*([\d,]+)",r"30d\s*average.{0,100}?₹\s*([\d,]+)"]:
  m=re.search(p,txt,re.I|re.S)
  if m:avg=get_price(m.group(1));break
 for p in [r"all[\s-]*time\s*(?:low|lowest).{0,100}?₹\s*([\d,]+)"]:
  m=re.search(p,txt,re.I|re.S)
  if m:low=get_price(m.group(1));break
 return avg,low

async def history(page,url):
 try:
  await page.goto("https://pricehistoryapp.com/",wait_until="domcontentloaded",timeout=25000)
  await page.wait_for_timeout(600)
  ins=page.locator("input")
  if not await ins.count():return None
  target=ins.first
  for i in range(await ins.count()):
   x=ins.nth(i)
   t=((await x.get_attribute("placeholder") or "")+" "+(await x.get_attribute("aria-label") or "")).lower()
   if "url" in t or "link" in t:target=x;break
  await target.fill(url)
  bs=page.locator("button");clicked=False
  for i in range(await bs.count()):
   b=bs.nth(i)
   try:t=(await b.inner_text()).lower()
   except:t=""
   if any(x in t for x in ["track","check","search"]):
    await b.click();clicked=True;break
  if not clicked:await target.press("Enter")
  await page.wait_for_timeout(2500)
  if "/product/" not in page.url:
   ls=page.locator('a[href*="/product/"]')
   if await ls.count():
    h=await ls.first.get_attribute("href")
    if h:
     if h.startswith("/"):h="https://pricehistoryapp.com"+h
     await page.goto(h,wait_until="domcontentloaded",timeout=25000)
  if "/product/" not in page.url:return None
  avg,low=history_values(await page.locator("body").inner_text())
  return {"avg":avg,"low":low,"url":page.url} if avg else None
 except Exception as e:print("HISTORY:",e);return None

async def check(context,state,url,item,sem):
 async with sem:
  page=await context.new_page()
  try:
   title,current,rating=await info(page,url)
   if any(x in (title or "").lower() for x in WOMEN):
    print("WOMEN SKIP:",title);return
   if not current or current>=MAX_PRICE:return
   if rating is not None and rating<=MIN_RATING:return
   h=await history(page,url)
   if not h:return
   avg,low=h["avg"],h["low"]
   drop=(avg-current)/avg*100
   if drop<MIN_DROP:return
   level="💥 UNPRECEDENTED DEAL" if drop>=75 else "🔥 EXCEPTIONAL DEAL" if drop>=65 else "🚨 VERY STRONG DEAL" if drop>=55 else "⚡ STRONG DEAL"
   msg=f"{level}\n\n{title}\n\nStore: {item['store'].upper()}\nCurrent: ₹{current:,.0f}\n30-day average: ₹{avg:,.0f}\nBelow average: {drop:.1f}%\nAll-time low: ₹{low:,.0f}" if low else f"{level}\n\n{title}\n\nStore: {item['store'].upper()}\nCurrent: ₹{current:,.0f}\n30-day average: ₹{avg:,.0f}\nBelow average: {drop:.1f}%\nAll-time low: Unavailable"
   if rating is not None:msg+=f"\n⭐ Rating: {rating:.1f}"
   msg+=f"\n\n🛒 {url}\n📊 {h['url']}"
   if telegram(msg):
    if url not in state["sent"]:state["sent"].append(url)
    print("ALERT:",title)
  except Exception as e:print("CHECK:",e)
  finally:await page.close()

async def main():
 state=load()
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True)
  context=await browser.new_context(viewport={"width":1365,"height":900},locale="en-IN")
  async def route(r):
   await r.abort() if r.request.resource_type in {"image","media","font"} else await r.continue_()
  await context.route("**/*",route)
  page=await context.new_page()
  await discover(page,state)
  await page.close();save(state)

  now=time.time()
  ready=[(u,v) for u,v in state["queue"].items() if now-float(state["checked"].get(u,0))>=RECHECK_HOURS*3600]
  batch=ready[:BATCH_SIZE]
  print("READY:",len(ready),"PROCESSING:",len(batch))

  for u,_ in batch:state["checked"][u]=now
  save(state)

  sem=asyncio.Semaphore(MAX_CONCURRENT)
  await asyncio.gather(*(check(context,state,u,v,sem) for u,v in batch),return_exceptions=True)
  save(state)
  await browser.close()

if __name__=="__main__":
 try:asyncio.run(main())
 except Exception as e:print("MAIN:",e);traceback.print_exc()