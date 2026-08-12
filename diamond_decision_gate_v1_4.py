#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path

ROOT = Path("/opt/render/project/src")
DATA = Path("/var/data")


def run(script):
    try:
        r = subprocess.run(
            ["python3", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return f"FOUT: {exc}"


# Eerst volledige bestaande Decision Gate v1.3
print(run("diamond_decision_gate_v1_3.py").rstrip())

print()
print("=" * 92)
print(" MASTER DECISION SHADOW")
print("=" * 92)

# Master prospectieve combinatie actualiseren
master_output = run(
    "diamond_master_decision_shadow.py"
)

report_file = (
    DATA /
    "diamond_master_decision_shadow_report.json"
)

if report_file.exists():
    d = json.loads(
        report_file.read_text()
    )

    accepted = int(
        d.get("accepted", 0)
    )
    closed = int(
        d.get("closed", 0)
    )
    pnl = float(
        d.get("net_pnl_eur", 0) or 0
    )

    if closed >= 20:
        status = "EINDANALYSE MOGELIJK"
    elif closed >= 10:
        status = "10/20 MIJLPAAL"
    elif closed >= 5:
        status = "5/20 MIJLPAAL"
    else:
        status = "LOOPT"

    candidates = d.get(
        "candidates",
        [],
    )

    def count(field):
        return sum(
            bool(x.get(field))
            for x in candidates
        )

    print(
        f"Baseline : "
        f"{d.get('started_at')}"
    )
    print(
        f"Accepted : {accepted}"
    )
    print(
        f"Closed   : {closed}/20 "
        f"[{status}]"
    )
    print(
        f"PnL      : €{pnl:+.4f}"
    )

    print()
    print("MASTER LAGEN")
    print(
        " SELECTIVE_GOOD_REGIME :",
        count("good_regime"),
    )
    print(
        " LONG_TB_SCORE_VOLUME  :",
        count("long_tb_score_volume"),
    )
    print(
        " EXEC_HIGH_VOLUME      :",
        count("execution_high_volume"),
    )
    print(
        " EXEC_HV_QUOTE         :",
        count("execution_high_volume_quote"),
    )
else:
    print(
        "Master Decision rapport "
        "niet gevonden."
    )

print()
print("MASTER CORE:")
print(
    "SELECTIVE + BULLISH/BULLISH_WEAK"
)
print("OF LONG_TB_SCORE_VOLUME")
print()
print(
    "BTC/Execution blijven observatie "
    "tot eigen prospectieve bevestiging."
)
print()
print("Live-goedkeuring      : NEE")
print("Automatische livegang : NEE")
