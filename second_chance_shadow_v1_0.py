#!/usr/bin/env python3
import csv, json, sys
from pathlib import Path

CSV=Path("/var/data/diamond_scanner_selective_shadow_trades.csv")
STATE=Path("/var/data/diamond_second_chance_shadow_state.json")
STAKE=130.0

def rows():
    with CSV.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def calc():
    d=json.loads(STATE.read_text())
    r=rows()
    sel={x["candidate_key"] for x in r if x["variant"]=="SELECTIVE"}
    cur=[x for x in r if x["variant"]=="CURRENT"]
    base=set(d["baseline_keys"])
    new=[x for x in cur if x["candidate_key"] not in base]
    non=[x for x in new if x["candidate_key"] not in sel]

    second=[
        x for x in non
        if x["side"]=="SHORT"
        and x["strategy"]=="range_breakout"
        and x["market_regime"]=="NEUTRAL"
        and float(x["reward_risk"])>=1.60
    ]

    def stats(a):
        pnl=sum(
            float(x["net_pnl_eur"]) *
            STAKE/float(x["stake_eur"])
            for x in a
        )
        wins=sum(float(x["net_pnl_eur"])>0 for x in a)
        return len(a),wins,round(pnl,2)

    return stats(non),stats(second)

def init():
    r=rows()
    keys=[x["candidate_key"] for x in r if x["variant"]=="CURRENT"]
    STATE.write_text(json.dumps({"baseline_keys":keys},indent=2))

def show():
    non,sec=calc()
    print("SECOND CHANCE SHADOW v1.0")
    print(f"NON_SELECTIVE  : {non[0]} trades {non[1]}W/{non[0]-non[1]}L pnl130=€{non[2]:+.2f}")
    print(f"SECOND_CHANCE  : {sec[0]} trades {sec[1]}W/{sec[0]-sec[1]}L pnl130=€{sec[2]:+.2f}")
    print("Filter         : SHORT + NEUTRAL + range_breakout + RR>=1.60")
    print("Inzet          : €130")
    print("Orders         : NEE")

if "--init" in sys.argv:
    init()
show()
