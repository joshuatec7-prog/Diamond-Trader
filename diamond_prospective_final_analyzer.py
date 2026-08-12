#!/usr/bin/env python3
# Diamond Trader Prospective Final Analyzer v1.4

import csv
import json
import hashlib
import subprocess
from pathlib import Path

DATA = Path("/var/data")
RULES_PATH = Path("diamond_prospective_decision_rules.json")

RULES = json.loads(
    RULES_PATH.read_text(encoding="utf-8")
)

TARGET = int(RULES["target_closed"])
MILESTONES = RULES["milestones"]
STATUSES = RULES["statuses"]
FINAL = RULES["final_pass_rules"]

STRESS_SIDE = (
    float(RULES["stress"]["extra_cost_per_side_pct"])
    / 100.0
)

def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

def read_csv(path):
    try:
        return list(csv.DictReader(
            path.open(
                encoding="utf-8-sig",
                newline=""
            )
        ))
    except Exception:
        return []

def pftext(v):
    return (
        "inf"
        if v == float("inf")
        else f"{v:.3f}"
    )

def milestone(n):
    if n >= TARGET:
        return STATUSES[
            "missing_trade_level_data"
        ]
    if n >= int(MILESTONES["promising"]):
        return STATUSES["from_10"]
    if n >= int(MILESTONES["first"]):
        return STATUSES["from_5"]
    return STATUSES["under_5"]

def metrics(
    rows,
    pnl_field="net_pnl_eur",
    stress=False
):
    vals=[]

    for r in rows:
        pnl=num(r.get(pnl_field))

        if stress:
            stake=(
                num(r.get("stake_eur"))
                or 130.0
            )
            pnl -= stake * STRESS_SIDE * 2

        vals.append(pnl)

    gain=sum(x for x in vals if x > 0)
    loss=abs(sum(x for x in vals if x < 0))
    pf=gain/loss if loss else float("inf")

    equity=peak=dd=0.0
    streak=max_streak=0

    for x in vals:
        equity += x
        peak=max(peak,equity)
        dd=max(dd,peak-equity)

        if x < 0:
            streak += 1
            max_streak=max(
                max_streak,
                streak
            )
        else:
            streak=0

    return {
        "n":len(vals),
        "w":sum(x > 0 for x in vals),
        "l":sum(x < 0 for x in vals),
        "pnl":sum(vals),
        "pf":pf,
        "dd":dd,
        "streak":max_streak,
    }

def passes(m):
    return (
        m["pnl"] >
        float(FINAL["normal_pnl_gt"])
        and
        m["pf"] >
        float(FINAL["normal_pf_gt"])
    )

def full_section(label, rows):
    print("\n" + "-"*72)
    print(label)

    if not rows:
        print("Status : GEEN TRADES")
        return

    normal=metrics(rows)
    stress=metrics(rows, stress=True)

    best=max(
        rows,
        key=lambda r:num(
            r.get("net_pnl_eur")
        )
    )

    nobest=metrics(
        [r for r in rows if r is not best]
    )

    print(
        f"Normaal: {normal['n']}/{TARGET} "
        f"W/L={normal['w']}/{normal['l']} "
        f"PnL=€{normal['pnl']:+.4f} "
        f"PF={pftext(normal['pf'])}"
    )
    print(
        f"Stress : PnL=€{stress['pnl']:+.4f} "
        f"PF={pftext(stress['pf'])}"
    )
    print(
        f"Risico : DD=€{normal['dd']:.2f} "
        f"verliesreeks={normal['streak']}"
    )
    print(
        "Zonder beste: "
        f"PnL=€{nobest['pnl']:+.4f} "
        f"PF={pftext(nobest['pf'])}"
    )

    if normal["n"] < TARGET:
        status=milestone(normal["n"])
    else:
        good=(
            passes(normal)
            and stress["pnl"] >
            float(FINAL["stress_pnl_gt"])
            and stress["pf"] >
            float(FINAL["stress_pf_gt"])
            and nobest["pnl"] >
            float(FINAL[
                "without_best_trade_pnl_gt"
            ])
            and nobest["pf"] >
            float(FINAL[
                "without_best_trade_pf_gt"
            ])
        )
        status=(
            STATUSES["passed_20"]
            if good
            else STATUSES["failed_20"]
        )

    print("STATUS :",status)

print("="*72)
print(
    " DIAMOND TRADER PROSPECTIVE "
    "FINAL ANALYZER v1.4"
)
print("="*72)

checksum=hashlib.sha256(
    RULES_PATH.read_bytes()
).hexdigest()

print(
    f"Beslisregels : v{RULES['version']} "
    f"| {checksum}"
)

sel=read_csv(
    DATA /
    "diamond_scanner_selective_shadow_trades.csv"
)

full_section(
    "SELECTIVE",
    [
        r for r in sel
        if str(r.get("variant","")).upper()
        == "SELECTIVE"
        and r.get("net_pnl_eur")
        not in (None,"")
    ]
)

full_section(
    "STRONG",
    [
        r for r in sel
        if str(r.get("variant","")).upper()
        == "STRONG"
        and r.get("net_pnl_eur")
        not in (None,"")
    ]
)

regime=read_csv(
    DATA /
    "diamond_scanner_regime_shadow_trades.csv"
)

full_section(
    "REGIME BTC_ALIGNED",
    [
        r for r in regime
        if str(r.get("variant","")).upper()
        == "BTC_ALIGNED"
        and r.get("net_pnl_eur")
        not in (None,"")
    ]
)

print("\n" + "-"*72)
print("PAPER-SHORT V3")

shorts=read_csv(
    DATA/"diamond_short_execution.csv"
)

v3=[
    r for r in shorts
    if str(r.get("event","")).upper()
    == "CLOSE"
    and str(r.get("strategy_version",""))
    == "short_breakout_v3"
]

normal=metrics(
    v3,
    pnl_field="net_pnl_quote"
)

best=max(
    v3,
    key=lambda r:num(
        r.get("net_pnl_quote")
    ),
    default=None
)

nobest=metrics(
    [r for r in v3 if r is not best],
    pnl_field="net_pnl_quote"
)

print(
    f"Gesloten: {normal['n']}/{TARGET} "
    f"W/L={normal['w']}/{normal['l']} "
    f"PnL=€{normal['pnl']:+.4f} "
    f"PF={pftext(normal['pf'])}"
)

print(
    "Zonder beste: "
    f"PnL=€{nobest['pnl']:+.4f} "
    f"PF={pftext(nobest['pf'])}"
)

if normal["n"] < TARGET:
    paper_status=milestone(normal["n"])
elif (
    not passes(normal)
    or nobest["pnl"] <=
    float(FINAL[
        "without_best_trade_pnl_gt"
    ])
    or nobest["pf"] <=
    float(FINAL[
        "without_best_trade_pf_gt"
    ])
):
    paper_status=STATUSES["failed_20"]
else:
    paper_status=STATUSES[
        "missing_trade_level_data"
    ]

print("STATUS :",paper_status)
print(
    "Stress : N.V.T. "
    "- stake ontbreekt in execution-bron"
)

print("\n" + "-"*72)
print("EXECUTION QUALITY")

ep=(
    DATA /
    "diamond_execution_quality_shadow_report.json"
)

if ep.exists():
    report=json.load(
        open(ep,encoding="utf-8")
    )

    for name,g in report.get(
        "groups",{}
    ).items():
        n=int(g.get("closed",0))
        pnl=num(g.get("pnl"))
        pfx=num(g.get("profit_factor"))

        if n < TARGET:
            status=milestone(n)
        elif pnl <= 0 or pfx <= 1:
            status=STATUSES["failed_20"]
        else:
            status=STATUSES[
                "missing_trade_level_data"
            ]

        print(
            f"{name:<22} "
            f"{n}/{TARGET} "
            f"W/L={g.get('wins',0)}/"
            f"{g.get('losses',0)} "
            f"PnL=€{pnl:+.4f} "
            f"PF={g.get('profit_factor')} "
            f"| {status}"
        )
else:
    print("Execution report ontbreekt")

print("\n" + "-"*72)
print("CENTRALE PROSPECTIEVE GATES")

gate=subprocess.run(
    [
        "python3",
        "diamond_decision_gate_v1_4.py"
    ],
    capture_output=True,
    text=True,
    timeout=60
).stdout

section=None

for line in gate.splitlines():
    s=line.strip()

    if s=="LONG QUALITY":
        section="LONG"
        print("\nLONG QUALITY")
        continue

    if s=="SHORT QUALITY":
        section="SHORT"
        print("\nSHORT QUALITY")
        continue

    if s in (
        "REGIME SHADOW",
        "EXECUTION QUALITY",
        "BTC EVENT CONFIRMATION"
    ):
        section=None

    if (
        section=="LONG"
        and (
            s.startswith("ALL_ELIGIBLE")
            or s.startswith("TB_SCORE_VOLUME")
        )
    ):
        print(s)

    if (
        section=="SHORT"
        and (
            s.startswith("ALL_ELIGIBLE")
            or s.startswith("MBW_HIGH_VOLUME")
        )
    ):
        print(s)

master=(
    DATA /
    "diamond_master_decision_shadow_report.json"
)

print("\nMASTER DECISION")

if master.exists():
    m=json.load(open(master,encoding="utf-8"))
    candidates=[
        x for x in m.get("candidates",[])
        if x.get("net_pnl_eur")
        not in (None,"")
    ]

    print(
        f"Accepted={m.get('accepted',0)} "
        f"Closed={m.get('closed',0)}/"
        f"{m.get('target',TARGET)} "
        f"PnL=€{num(m.get('net_pnl_eur')):+.4f}"
    )

    if candidates:
        full_section(
            "MASTER TRADE-LEVEL",
            candidates
        )
    else:
        print(
            "STATUS :",
            milestone(
                int(m.get("closed",0))
            )
        )

print("\nLIVE-GOEDKEURING      : NEE")
print("AUTOMATISCHE LIVEGANG : NEE")
print("KLAAR")
