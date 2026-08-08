#!/usr/bin/env python3
import csv,json,sys
from datetime import datetime
from pathlib import Path

CSV=Path("/var/data/diamond_scanner_selective_shadow_trades.csv")
STATE=Path("/var/data/diamond_selective_session_shadow_state.json")

def rows():
    with CSV.open(encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r["variant"]=="SELECTIVE"]

def init():
    r=rows()
    STATE.write_text(json.dumps({"baseline":len(r)},indent=2))

def show():
    d=json.loads(STATE.read_text())
    new=rows()[d["baseline"]:]
    groups={"ALL_SELECTIVE":[],"OFF_HOURS":[],"DAYTIME":[]}

    for r in new:
        h=datetime.fromisoformat(r["opened_at"]).hour
        groups["ALL_SELECTIVE"].append(r)
        groups["OFF_HOURS" if h<6 or h>=18 else "DAYTIME"].append(r)

    print("SELECTIVE SESSION SHADOW v1.0")
    for name,a in groups.items():
        pnl=sum(float(x["net_pnl_eur"])*130/float(x["stake_eur"]) for x in a)
        wins=sum(float(x["net_pnl_eur"])>0 for x in a)
        print(f"{name:14s}: {len(a)} trades {wins}W/{len(a)-wins}L pnl130=€{pnl:+.2f}")
    print("Orders        : NEE")

if "--init" in sys.argv:
    init()
show()
