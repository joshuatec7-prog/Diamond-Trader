import ccxt,json
from pathlib import Path
from datetime import datetime,timezone

D=Path("/var/data")
S=D/"diamond_binance_1m_lead_state.json"
A=("BTC","ETH","SOL","XRP","ADA")

def load():
 try:return json.loads(S.read_text())
 except:return {"started_at":datetime.now(timezone.utc).isoformat(),"last":{},"events":[]}

def save(x):
 t=S.with_suffix(".tmp");t.write_text(json.dumps(x,indent=2));t.replace(S)

def closes(e,p):
 return {int(r[0]//60000*60000):float(r[4])
         for r in e.fetch_ohlcv(p,"1m",limit=180)}

st=load(); bv=ccxt.bitvavo(); bn=ccxt.binance()
bv.load_markets();bn.load_markets();new=0

for a in A:
 try:
  b=closes(bv,f"{a}/EUR"); c=closes(bn,f"{a}/USDT")
 except:continue
 ts=[t for t in sorted(set(b)&set(c)) if t-60000 in b and t-60000 in c]
 if not ts:continue
 if a not in st["last"]:st["last"][a]=ts[-1];continue

 for t in [x for x in ts if x>st["last"][a]]:
  z=(c[t]/c[t-60000]-1)*100
  m=(b[t]/b[t-60000]-1)*100
  if abs(z)>=0.08:
   sg=1 if z>0 else -1
   if sg*m<0.04:
    st["events"].append({
     "asset":a,"ts_ms":t,
     "direction":"LONG" if sg>0 else "SHORT",
     "binance_move_pct":round(z,4),
     "bitvavo_move_pct":round(m,4)})
    new+=1
 st["last"][a]=ts[-1]

st["events"]=st["events"][-5000:]
save(st)
print(f"BINANCE 1M LEAD | nieuwe_events={new} | totaal={len(st['events'])}")
