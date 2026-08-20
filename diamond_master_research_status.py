import subprocess,re
def run(f):
 try:return subprocess.run(["python3",f],capture_output=True,text=True,timeout=120).stdout
 except:return ""
best=None
for f in ("long_entry_shadow_lab.py","long_min_profit_shadow_lab.py",
          "long_combo_shadow_lab.py","long_combo_shadow_lab_v2.py"):
 for l in run(f).splitlines():
  if "closed=" not in l or "delta_" in l or " vs " in l: continue
  p=re.search(r"pnl=€([+-]?\d+(?:\.\d+)?)",l); n=re.search(r"closed=\s*(\d+)",l)
  if p and n and (best is None or float(p.group(1))>best[0]):
   best=(float(p.group(1)),n.group(1),l.split()[0])
print("\n=== RESEARCH OVERZICHT ===")
print(f"LONG BEST : {best[2]} | {best[1]}/20 | PnL=€{best[0]:+.4f}" if best else "LONG BEST : geen data")
import glob,json,os
F=glob.glob("/var/data/*market*lead*.json")
if F:
 f=max(F,key=os.path.getmtime)
 try:
  d=json.load(open(f))
  x=[]
  for k,v in d.items():
   if any(q in k.lower() for q in ("status","event","early","qualif","binance","kraken")):
    if isinstance(v,(list,dict)): v=len(v)
    x.append(f"{k}={v}")
  age=(__import__("time").time()-os.path.getmtime(f))/60
  flag="STALE" if age>180 else "OK"
  print("MARKET LEAD :", (" | ".join(x[:7]) or "ACTIEF")+f" | age={age:.0f}m | {flag}")
 except: print("MARKET LEAD : ACTIEF")
else:
 print("MARKET LEAD : COLLECTING")

o=run("diamond_selective_outcome_anatomy.py")
s=next((l for l in o.splitlines() if l.startswith("BULLISH_WEAK")),None)
print("SIDEWAYS :",s or "BULLISH_WEAK | nog geen data")
