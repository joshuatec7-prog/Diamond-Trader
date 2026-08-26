#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json,math,os,tempfile,time
from datetime import datetime,timezone
from pathlib import Path

VERSION="1.0"; SPREAD_MAX=0.10; TARGET=50; INTERVAL=300
DATA=Path(os.getenv("DIAMOND_DATA_DIR","/var/data"))
SOURCE=DATA/"diamond_scanner_selective_shadow_trades.csv"
BASE=DATA/"diamond_rr_mid_135_150_shadow_state.json"
REPORT=DATA/"diamond_tb_long_prospective_report.json"
SAFETY={"orders":False,"private_api":False,"network":False,"live_change":False,"config_change":False,"strategy_change":False}

RR=[("LT_120",-1e9,1.20),("120_135",1.20,1.35),("135_150",1.35,1.50),("150_170",1.50,1.70),("GE_170",1.70,1e9)]
ATR=[("LT_120",-1e9,1.20),("120_180",1.20,1.80),("180_250",1.80,2.50),("GE_250",2.50,1e9)]
RSI=[("LT_60",-1e9,60),("60_65",60,65),("65_70",65,70),("GE_70",70,1e9)]
VOL=[("LT_15",-1e9,1.5),("15_20",1.5,2),("20_30",2,3),("GE_30",3,1e9)]

def now(): return datetime.now(timezone.utc).isoformat()
def dt(v):
    try:
        x=datetime.fromisoformat(str(v).replace("Z","+00:00"))
        return (x if x.tzinfo else x.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except: return None
def num(v,default=None):
    try:
        x=float(v); return x if math.isfinite(x) else default
    except: return default
def jload(p):
    try:
        x=json.loads(p.read_text()); return x if isinstance(x,dict) else {}
    except: return {}
def writej(p,x):
    fd,tmp=tempfile.mkstemp(prefix="."+p.name+".",suffix=".tmp",dir=str(p.parent))
    try:
        with os.fdopen(fd,"w") as h:
            json.dump(x,h,indent=2); h.write("\n"); h.flush(); os.fsync(h.fileno())
        os.replace(tmp,p)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
def cutoff():
    s=jload(BASE)
    x=dt(s.get("baseline_last_detected_at") or s.get("baseline_cutoff") or s.get("created_at"))
    if not x: raise RuntimeError("RR MID baseline ontbreekt")
    return x
def first(r,*keys):
    for k in keys:
        x=num(r.get(k),None)
        if x is not None: return x
    return None
def load():
    if not SOURCE.is_file(): raise FileNotFoundError(SOURCE)
    c=cutoff(); out=[]
    with SOURCE.open(encoding="utf-8-sig",newline="") as h:
        rd=csv.DictReader(h)
        need={"variant","detected_at","closed_at","symbol","strategy","side","market_regime","entry_spread_pct","reward_risk","net_pnl_eur","total_fees_eur","exit_reason"}
        miss=need-set(rd.fieldnames or [])
        if miss: raise RuntimeError("CSV mist kolommen: "+", ".join(sorted(miss)))
        for r in rd:
            if str(r.get("variant","")).upper()!="CURRENT": continue
            if str(r.get("side","")).upper()!="LONG": continue
            if r.get("strategy")!="trend_breakout": continue
            if num(r.get("entry_spread_pct"),999)>SPREAD_MAX: continue
            d=dt(r.get("detected_at"))
            if not d or d<=c or not r.get("closed_at"): continue
            r=dict(r); r["_pnl"]=num(r.get("net_pnl_eur"),0) or 0; r["_fees"]=num(r.get("total_fees_eur"),0) or 0
            r["_rr"]=num(r.get("reward_risk"),0) or 0
            r["_atr"]=first(r,"atr_pct","atr_percent","atr_percentage")
            r["_rsi"]=first(r,"rsi","rsi_value")
            r["_vol"]=first(r,"volume_ratio","volume_multiple","volume_mult")
            r["_closed"]=dt(r.get("closed_at")); out.append(r)
    return sorted(out,key=lambda r:r["_closed"] or datetime.min.replace(tzinfo=timezone.utc))
def summ(rows):
    p=[r["_pnl"] for r in rows]; gp=sum(x for x in p if x>0); gl=abs(sum(x for x in p if x<0))
    pf=(gp/gl if gl else ("INF" if gp else None))
    return {"closed":len(rows),"wins":sum(x>0 for x in p),"losses":sum(x<0 for x in p),"net_pnl_eur":round(sum(p),4),"profit_factor":("INF" if pf=="INF" else None if pf is None else round(pf,4)),"fees_eur":round(sum(r["_fees"] for r in rows),4)}
def buckets(rows,key,defs):
    return {name:summ([r for r in rows if r.get(key) is not None and lo<=r[key]<hi]) for name,lo,hi in defs}
def regimes(rows):
    labs=sorted({str(r.get("market_regime") or "UNKNOWN").upper() for r in rows})
    return {x:summ([r for r in rows if str(r.get("market_regime") or "UNKNOWN").upper()==x]) for x in labs}
def report(rows):
    return {"version":VERSION,"generated_at":now(),"baseline_cutoff":cutoff().isoformat(),"rule":"CURRENT LONG trend_breakout + spread <= 0.10%; geen RR-filter","target_total_closed":TARGET,"target_reached":len(rows)>=TARGET,"all":summ(rows),"rr_buckets":buckets(rows,"_rr",RR),"atr_buckets":buckets(rows,"_atr",ATR),"rsi_buckets":buckets(rows,"_rsi",RSI),"volume_buckets":buckets(rows,"_vol",VOL),"regime":regimes(rows),"recent_closed":[{"closed_at":r.get("closed_at"),"symbol":r.get("symbol"),"rr":round(r["_rr"],3),"atr":r.get("_atr"),"rsi":r.get("_rsi"),"volume":r.get("_vol"),"pnl":round(r["_pnl"],4)} for r in rows[-10:]],"safety":SAFETY}
def pf(v): return "n/a" if v is None else str(v)
def line(n,x): print(f"{n:10} n={x['closed']:2d} W/L={x['wins']}/{x['losses']} PnL=€{x['net_pnl_eur']:+.2f} PF={pf(x['profit_factor'])}")
def run(show=True):
    x=report(load()); writej(REPORT,x)
    if show:
        print("="*72); print("DIAMOND TB LONG PROSPECTIVE RESEARCH"); print("Baseline :",x["baseline_cutoff"]); line("ALL",x["all"]); print("RR:")
        for n,_,_ in RR: line(n,x["rr_buckets"][n])
        print("Status   : research-only | LIVE/config/orders ongewijzigd")
    return 0
def selftest():
    rows=[{"_pnl":5.0,"_fees":.6,"_rr":1.4,"_atr":1.5,"_rsi":68.0,"_vol":1.8},{"_pnl":-2.0,"_fees":.6,"_rr":1.25,"_atr":2.0,"_rsi":72.0,"_vol":3.2}]
    assert summ(rows)["net_pnl_eur"]==3.0 and buckets(rows,"_rr",RR)["135_150"]["closed"]==1
    print("DIAMOND_TB_LONG_PROSPECTIVE_SELF_TEST_OK"); return 0
def main():
    p=argparse.ArgumentParser(); p.add_argument("--self-test",action="store_true"); p.add_argument("--no-print",action="store_true"); p.add_argument("--loop",action="store_true"); p.add_argument("--interval-seconds",type=int,default=INTERVAL); a=p.parse_args()
    if a.self_test: return selftest()
    if not a.loop: return run(not a.no_print)
    while True:
        try: run(not a.no_print)
        except Exception as e: print(f"{now()} | TB_LONG_RESEARCH_FOUT | {type(e).__name__}: {e}",flush=True)
        time.sleep(max(60,a.interval_seconds))
if __name__=="__main__": raise SystemExit(main())
