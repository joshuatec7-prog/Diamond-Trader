#!/usr/bin/env python3
import csv, json
from datetime import datetime, timezone
from pathlib import Path

CSV=Path("/var/data/diamond_short_execution.csv")
STATE=Path("/var/data/diamond_short_trigger_shadow_state.json")

def rows():
    out=[]
    with CSV.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                float(r["net_pnl_quote"])
                out.append(r)
            except:
                pass
    return out

def save(d):
    STATE.write_text(json.dumps(d,indent=2))

def init():
    r=rows()
    d={
        "version":"1.0",
        "started_at":datetime.now(timezone.utc).isoformat(),
        "baseline":len(r),
        "CURRENT":{"closed":0,"pnl":0.0},
        "BREAKOUT_ONLY":{"closed":0,"pnl":0.0},
    }
    save(d)
    return d

def update():
    d=json.loads(STATE.read_text()) if STATE.exists() else init()
    new=rows()[d["baseline"]:]
    d["CURRENT"]={"closed":len(new),"pnl":round(sum(float(x["net_pnl_quote"]) for x in new),4)}
    b=[x for x in new if x["entry_trigger"]=="bearish_breakout"]
    d["BREAKOUT_ONLY"]={"closed":len(b),"pnl":round(sum(float(x["net_pnl_quote"]) for x in b),4)}
    save(d)
    return d

def show(d):
    print("SHORT TRIGGER SHADOW v1.0")
    print("Baseline       :",d["baseline"])
    for k in ("CURRENT","BREAKOUT_ONLY"):
        print(f"{k:15s}: closed={d[k]['closed']} pnl=€{d[k]['pnl']:+.2f}")
    print("Orders         : NEE")

if __name__=="__main__":
    import sys
    if "--init" in sys.argv: show(init())
    elif "--update" in sys.argv: show(update())
    else: show(json.loads(STATE.read_text()) if STATE.exists() else init())
