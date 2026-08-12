#!/usr/bin/env python3
import subprocess
from pathlib import Path

ROOT = Path("/opt/render/project/src")

def run(cmd):
    try:
        r = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"FOUT: {e}"

def find_line(text, contains):
    for line in text.splitlines():
        if contains.lower() in line.lower():
            return line.strip()
    return "geen actuele regel gevonden"

def milestone(value, target=20):
    if value >= target:
        return "BEVESTIGD"
    if value >= 10:
        return "VEELBELOVEND"
    if value >= 5:
        return "EERSTE MIJLPAAL"
    return "LOOPT"

quick = run(["bash", "diamond_quick_status.sh"])
audit = run(["python3", "diamond_truth_audit.py"])
ranking = run([
    "python3",
    "diamond_quality_ranking_v1_2.py",
])

print("=" * 92)
print(" DIAMOND TRADER CENTRALE DECISION GATE")
print("=" * 92)
print()

print("SYSTEEM")
print(find_line(audit, "AUDIT:"))
print(find_line(audit, "Periodic runner"))
print(find_line(audit, "Geheugen"))
print()

print("HOOFDSTRATEGIE")
print(find_line(quick, "Selective SELECTIVE"))
print(find_line(quick, "Selective STRONG"))
print()

print("BESTAANDE GATES")
print(find_line(audit, "Paper-short V3"))
print(find_line(audit, "Long Entry"))
print(find_line(audit, "Long Min Profit"))
print(find_line(audit, "Long Combo v1"))
print(find_line(audit, "Long Combo v2"))
print()

print("CENTRALE QUALITY RANKING")
for line in ranking.splitlines():
    if line[:2].strip(" .").isdigit():
        print(line)

print()
print("HISTORISCH BESTE CONSTRUCTIE")
print("GUNSTIG REGIME")
print("  -> SELECTIVE")
print("  -> + LONG_TB_SCORE_VOLUME")
print()
print("Historisch robuust bevonden op:")
print("- extra frictie/slippage")
print("- drawdown en verliesreeksen")
print("- tijdsverdeling")
print("- portfolio-overlap")
print("- regime")
print("- concentratierisico")
print("- Monte Carlo")

print()
print("PROSPECTIEVE BESLISREGEL")
print("- 5/20  = eerste mijlpaal")
print("- 10/20 = veelbelovend")
print("- 20/20 = pas serieus kandidaat voor eindbesluit")
print("- kleine samples wijzigen NOOIT automatisch live")
print()

print("LIVE STATUS")
if "AUDIT: OK" in audit:
    print("Systeem              : OK")
else:
    print("Systeem              : CONTROLEREN")

print("Historisch onderzoek : VOLDOENDE")
print("Prospectief bewijs    : LOOPT")
print("Live-goedkeuring      : NEE")
print()
print(
    "Orders: NEE | Config: NEE | "
    "Automatische livegang: NEE"
)
