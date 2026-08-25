#!/usr/bin/env python3
import argparse, json, math, os, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import ccxt

STATE = Path("/var/data/diamond_marketwide_1m_early_mover_state.json")
OUT   = Path("/var/data/diamond_marketwide_1m_early_movers.json")

INTERVAL = 60
RETENTION = 25 * 60
TRIGGER_RETENTION = 15 * 60

TH_1M  = 0.35
TH_5M  = 1.00
TH_15M = 2.00

NL = ZoneInfo("Europe/Amsterdam")

def num(x, d=0.0):
    try:
        v=float(x)
        return v if math.isfinite(v) else d
    except:
        return d

def pct(old, new):
    return ((new / old) - 1.0) * 100.0 if old > 0 else 0.0

def spread(t):
    bid=num(t.get("bid"))
    ask=num(t.get("ask"))
    if bid <= 0 or ask <= 0:
        return 999.0
    mid=(bid+ask)/2
    return ((ask-bid)/mid)*100 if mid > 0 else 999.0

def volume_quote(t, last):
    q=num(t.get("quoteVolume"))
    if q > 0:
        return q
    return num(t.get("baseVolume")) * last

def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=str(path.parent), prefix="."+path.name)
    try:
        with os.fdopen(fd,"w") as f:
            json.dump(data,f,indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def load():
    try:
        d=json.loads(STATE.read_text())
        return d if isinstance(d,dict) else {}
    except:
        return {}

def old_sample(hist, now, age):
    target=now-age
    rows=[x for x in hist if int(x["t"]) <= target]
    return rows[-1] if rows else None

def liquidity(vol, spr):
    if vol >= 250000 and spr <= 0.25:
        return "PASS"
    if vol >= 100000 and spr <= 0.50:
        return "WATCH"
    return "LOW"

def scan(ex):
    now=int(time.time())
    d=load()
    samples=d.setdefault("samples",{})
    recent=d.setdefault("recent_triggers",{})

    tickers=ex.fetch_tickers()
    candidates=[]
    eur_count=0

    for symbol,t in tickers.items():
        if not symbol.endswith("/EUR"):
            continue

        last=num(t.get("last"))
        if last <= 0:
            continue

        eur_count += 1
        spr=spread(t)
        vol=volume_quote(t,last)

        hist=samples.setdefault(symbol,[])
        hist.append({"t":now,"p":last})
        hist[:]=[
            x for x in hist
            if int(x["t"]) >= now-RETENTION
        ]

        values={}
        for name,age in (("m1",60),("m5",300),("m15",900)):
            old=old_sample(hist,now,age)
            values[name]=pct(num(old["p"]),last) if old else None

        if symbol in recent:
            recent[symbol].update({
                "last":last,
                "move_1m_pct":values["m1"],
                "move_5m_pct":values["m5"],
                "move_15m_pct":values["m15"],
                "volume_quote_24h":round(vol,2),
                "spread_pct":round(spr,4),
                "liquidity":liquidity(vol,spr),
            })

        hit=(
            (values["m1"]  is not None and values["m1"]  >= TH_1M)
            or
            (values["m5"] is not None and values["m5"]  >= TH_5M)
            or
            (values["m15"] is not None and values["m15"] >= TH_15M)
        )

        if hit:
            score=max(
                (values["m1"] or 0)/TH_1M,
                (values["m5"] or 0)/TH_5M,
                (values["m15"] or 0)/TH_15M,
            )

            row={
                "symbol":symbol,
                "last":last,
                "move_1m_pct":values["m1"],
                "move_5m_pct":values["m5"],
                "move_15m_pct":values["m15"],
                "volume_quote_24h":round(vol,2),
                "spread_pct":round(spr,4),
                "liquidity":liquidity(vol,spr),
                "priority":round(score,3),
                "research_only":True,
                "live_eligible":False,
            }

            candidates.append(row)
            recent[symbol]=dict(row, triggered_at=now)

    # Een korte versnelling mag niet verdwijnen voordat de zware
    # scanner hem heeft kunnen beoordelen.
    for symbol in list(recent):
        triggered=int(recent[symbol].get("triggered_at",0) or 0)
        if triggered < now-TRIGGER_RETENTION:
            del recent[symbol]

    current_symbols={x["symbol"] for x in candidates}

    for symbol,saved in recent.items():
        if symbol in current_symbols:
            continue

        row={
            k:v for k,v in saved.items()
            if k != "triggered_at"
        }
        row["retained_trigger"]=True
        row["trigger_age_seconds"]=max(
            0,
            now-int(saved.get("triggered_at",now))
        )
        candidates.append(row)

    candidates.sort(key=lambda x:x["priority"], reverse=True)

    active_count=sum(
        1 for x in candidates
        if not x.get("retained_trigger")
    )
    retained_count=len(candidates)-active_count

    utc=datetime.now(timezone.utc)
    result={
        "generated_at_utc":utc.isoformat(),
        "generated_at_nl":utc.astimezone(NL).isoformat(),
        "eur_markets_seen":eur_count,
        "candidate_count":len(candidates),
        "active_candidate_count":active_count,
        "retained_candidate_count":retained_count,
        "thresholds_pct":{
            "1m":TH_1M,
            "5m":TH_5M,
            "15m":TH_15M,
        },
        "research_only":True,
        "orders_possible":False,
        "candidates":candidates[:100],
    }

    d["last_run_utc"]=utc.isoformat()
    d["market_count"]=eur_count

    atomic(STATE,d)
    atomic(OUT,result)

    print(
        f"MARKET-WIDE 1M | markten={eur_count} "
        f"| early_movers={len(candidates)}"
    )
    for x in candidates[:10]:
        print(
            f"{x['symbol']:12} "
            f"1m={x['move_1m_pct'] if x['move_1m_pct'] is not None else 0:+.2f}% "
            f"5m={x['move_5m_pct'] if x['move_5m_pct'] is not None else 0:+.2f}% "
            f"15m={x['move_15m_pct'] if x['move_15m_pct'] is not None else 0:+.2f}% "
            f"{x['liquidity']}"
        )

def self_test():
    assert round(pct(100,100.5),2) == 0.50
    assert liquidity(300000,0.10) == "PASS"
    assert liquidity(150000,0.30) == "WATCH"
    assert liquidity(50000,0.10) == "LOW"
    print("SELF TEST: PASS")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--once",action="store_true")
    ap.add_argument("--loop",action="store_true")
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()

    if a.self_test:
        self_test()
        return

    ex=ccxt.bitvavo({"enableRateLimit":True})
    ex.load_markets()

    if a.once:
        scan(ex)
        return

    while True:
        try:
            scan(ex)
        except Exception as e:
            print("FOUT:",type(e).__name__,e,flush=True)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
