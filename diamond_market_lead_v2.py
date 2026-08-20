#!/usr/bin/env python3
import ccxt,json
from pathlib import Path
from datetime import datetime,timezone

V="1.0"; D=Path("/var/data")
S=D/"diamond_market_lead_v2_state.json"; R=D/"diamond_market_lead_v2_report.json"
A=("BTC","ETH","SOL","XRP","ADA"); L=(1,3,5); M=.08; E=.04
P={"bitvavo":("{}/EUR",),"binance":("{}/USDT","{}/USDC"),"kraken":("{}/EUR","{}/USD")}

def now():return datetime.now(timezone.utc).isoformat()
def load(p,d):
 try:return json.loads(p.read_text())
 except:return d
def save(p,x):
 t=p.with_suffix(".tmp");t.write_text(json.dumps(x,indent=2));t.replace(p)
def blank():return {"n":0,"hit":0,"early":0,"ehit":0,"sum":0.0,"esum":0.0}
def pair(e,x,a):
 for q in P[x]:
  q=q.format(a)
  if q in e.markets:return q
def close(rows):
 return {int(r[0]//60000*60000):float(r[4]) for r in rows if r and r[4]}
def ret(a,b):return (b/a-1)*100 if a else 0
def main():
 st=load(S,{"version":V,"started_at":now(),"pairs":{}})
 ex={"bitvavo":ccxt.bitvavo({"enableRateLimit":True,"timeout":15000}),
     "binance":ccxt.binance({"enableRateLimit":True,"timeout":15000}),
     "kraken":ccxt.kraken({"enableRateLimit":True,"timeout":15000})}
 ok={}
 for k,e in ex.items():
  try:e.load_markets();ok[k]=True
  except:ok[k]=False
 new=0
 for a in A:
  if not ok["bitvavo"]:break
  q=pair(ex["bitvavo"],"bitvavo",a)
  if not q:continue
  try:b=close(ex["bitvavo"].fetch_ohlcv(q,"1m",limit=180))
  except:continue
  for x in ("binance","kraken"):
   if not ok[x]:continue
   q=pair(ex[x],x,a)
   if not q:continue
   try:c=close(ex[x].fetch_ohlcv(q,"1m",limit=180))
   except:continue
   k=f"{a}:{x}"; p=st["pairs"].setdefault(k,{"last":None,"symbol":q,"lags":{str(l):blank() for l in L}})
   p["symbol"]=q
   ts=[t for t in sorted(set(b)&set(c)) if t-60000 in b and t-60000 in c and t+300000 in b]
   if not ts:continue
   if p["last"] is None:p["last"]=ts[-1];continue
   todo=[t for t in ts if t>p["last"]]
   for t in todo:
    z=ret(c[t-60000],c[t]); bv=ret(b[t-60000],b[t])
    if abs(z)<M:continue
    sg=1 if z>0 else -1
    for l in L:
     if t not in b or (t+l*60000) not in b:
         continue
     s=p["lags"][str(l)]; y=sg*ret(b[t],b[t+l*60000])
     s["n"]+=1;s["hit"]+=y>0;s["sum"]+=y
     if sg*bv<E:s["early"]+=1;s["ehit"]+=y>0;s["esum"]+=y
   if todo:new+=len(todo);p["last"]=todo[-1]
 rows=[];ev=early=0
 for k,p in st["pairs"].items():
  a,x=k.split(":")
  for l in L:
   s=p["lags"][str(l)];ev+=s["n"];early+=s["early"]
   rows.append({"asset":a,"exchange":x,"lag_min":l,"events":s["n"],
    "hit_rate":round(100*s["hit"]/s["n"],1) if s["n"] else None,
    "early_events":s["early"],"early_hit_rate":round(100*s["ehit"]/s["early"],1) if s["early"] else None,
    "avg_follow_pct":round(s["sum"]/s["n"],4) if s["n"] else None,
    "avg_early_follow_pct":round(s["esum"]/s["early"],4) if s["early"] else None})
 best=[r for r in rows if r["early_events"]>=20]
 best.sort(key=lambda r:(r["early_hit_rate"] or 0,r["avg_early_follow_pct"] or -9),reverse=True)
 rep={"version":V,"generated_at":now(),"started_at":st["started_at"],"research_only":True,
  "orders_possible":False,"private_api":False,"settings":{"timeframe":"1m","lags_min":L,"move_min_pct":M,"early_max_pct":E},
  "exchange_status":ok,"new_minutes":new,"events":ev,"early_events":early,"qualified":best[:10],"all":rows}
 save(S,st);save(R,rep)
 print(f"MARKET LEAD V2 | new={new} events={ev} early={early} qualified={len(best)}")
 print("BINANCE:", "PASS" if ok["binance"] else "FAIL","| KRAKEN:","PASS" if ok["kraken"] else "FAIL")
 if best:
  q=best[0];print(f"BEST: {q['exchange']} {q['asset']} +{q['lag_min']}m | n={q['early_events']} hit={q['early_hit_rate']}% follow={q['avg_early_follow_pct']:+.4f}%")
 else:print("STATUS: COLLECTING | grens 20 early-events per combinatie")
if __name__=="__main__":main()
