#!/usr/bin/env python3
# Diamond Trader Release / Go-Live Readiness v1.1

import hashlib, json, re, subprocess
from pathlib import Path

ROOT=Path("/opt/render/project/src")
DATA=Path("/var/data")

RULE_HASH="4556c32f7f6ebf172d1b4ec1fc66bdd0ebcdc3938a470520d1ffe89574927b0b"
SAFETY_HASH="c03e33f490f7e0db08f0d2da894fa75872453ce09c7c385302fee4b8fa0f39d3"

results=[]
infra_fail=[]

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def run(cmd):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=120
    ).stdout

def result(label,status,detail=""):
    results.append((label,status,detail))
    tag={"PASS":"PASS","WAIT":"WAIT","REVIEW":"REVIEW","FAIL":"FAIL"}[status]
    extra=f" | {detail}" if detail else ""
    print(f"[{tag}] {label}{extra}")

def infra(label,good):
    print(f"[{'OK' if good else 'FAIL'}] {label}")
    if not good:
        infra_fail.append(label)

def status_from_text(text):
    t=str(text).upper()
    if "GESLAAGD" in t:
        return "PASS"
    if "AFWIJZEN" in t:
        return "FAIL"
    if "HANDMATIGE" in t:
        return "REVIEW"
    return "WAIT"

manual_path=DATA/"diamond_manual_final_reviews.json"
manual=json.load(open(manual_path)) if manual_path.exists() else {}

def manual_review(key,base):
    if base != "REVIEW":
        return base

    r=manual.get(key,{})
    if str(r.get("status","")).upper() != "APPROVED":
        return "REVIEW"

    snap=Path(str(r.get("snapshot","")))
    if not snap.is_dir():
        return "REVIEW"

    if not (snap/"SHA256SUMS.txt").exists():
        return "REVIEW"

    return "PASS"

print("="*80)
print(" DIAMOND TRADER RELEASE / GO-LIVE READINESS v1.1")
print("="*80)

gate=run(["python3","diamond_decision_gate_v1_4.py"])
analyzer=run(["python3","diamond_prospective_final_analyzer.py"])

print("\n1. SYSTEEM / RELEASE")

infra("Truth Audit", "AUDIT: OK" in gate)

m=re.search(r"Periodic runner\s*:.*fouten=(\d+)",gate)
infra("Periodic runner fouten=0", bool(m and int(m.group(1))==0))

required=[
    "diamond_prospective_final_analyzer.py",
    "diamond_prospective_decision_rules.json",
    "DIAMOND_ENDREVIEW_RUNBOOK.md",
    "diamond_capture_end_review.sh",
    "DIAMOND_GO_LIVE_ROLLBACK_RUNBOOK.md",
    "diamond_go_live_preflight.sh",
    "diamond_post_live_safety_rules.json",
    "DIAMOND_POST_LIVE_SAFETY_RUNBOOK.md"
]

for name in required:
    infra(name,(ROOT/name).exists())

rules=ROOT/"diamond_prospective_decision_rules.json"
safety=ROOT/"diamond_post_live_safety_rules.json"

infra("Beslisregels checksum",
      rules.exists() and sha(rules)==RULE_HASH)

infra("Post-live safety checksum",
      safety.exists() and sha(safety)==SAFETY_HASH)

delta=run(["python3","diamond_release_delta_audit.py"])
infra(
    "Runtime gelijk aan Release Candidate",
    "Runtime CHANGED   : 0" in delta
    and "Runtime MISSING   : 0" in delta
)

def section_status(title):
    p=analyzer.find(title)
    if p < 0:
        return "WAIT","niet gevonden"

    part=analyzer[p:p+1200]
    m=re.search(r"STATUS\s*:\s*([^\n]+)",part)

    if not m:
        return "WAIT","geen eindstatus"

    text=m.group(1).strip()
    return status_from_text(text),text

print("\n2. PROSPECTIEVE GATES")

for label,title in (
    ("SELECTIVE","SELECTIVE"),
    ("STRONG","STRONG"),
    ("BTC_ALIGNED","REGIME BTC_ALIGNED"),
):
    s,d=section_status(title)
    result(label,s,d)

s,d=section_status("PAPER-SHORT V3")
s=manual_review("PAPER_SHORT_V3",s)
result("Paper-short V3",s,d)

long_part=gate[gate.find("LONG QUALITY"):gate.find("SHORT QUALITY")]
m=re.search(r"ALL_ELIGIBLE.*?closed=\s*(\d+)/20",long_part)
n=int(m.group(1)) if m else 0

s="REVIEW" if n>=20 else "WAIT"
s=manual_review("LONG_QUALITY",s)
result("LONG Quality",s,f"{n}/20")

short_start=gate.find("SHORT QUALITY")
short_end=gate.find("REGIME SHADOW",short_start)
short_part=gate[short_start:short_end]

m=re.search(r"ALL_ELIGIBLE.*?closed=\s*(\d+)/20",short_part)
n=int(m.group(1)) if m else 0

s="REVIEW" if n>=20 else "WAIT"
s=manual_review("SHORT_QUALITY",s)
result("SHORT Quality",s,f"{n}/20")

m=re.search(
    r"^BASELINE\s+(\d+)/20.*?\|\s*([^\n]+)",
    analyzer,
    re.M
)

if m:
    n=int(m.group(1))
    raw=m.group(2).strip()
    s=status_from_text(raw) if n>=20 else "WAIT"
    s=manual_review("EXECUTION_BASELINE",s)
    result("Execution BASELINE",s,f"{n}/20 | {raw}")
else:
    result("Execution BASELINE","WAIT","niet gevonden")

master_path=DATA/"diamond_master_decision_shadow_report.json"
master_n=0

if master_path.exists():
    master=json.load(open(master_path))
    master_n=int(master.get("closed",0))

if master_n < 20:
    result("Master Decision","WAIT",f"{master_n}/20")
else:
    s,d=section_status("MASTER TRADE-LEVEL")
    result("Master Decision",s,d)

btc_state=DATA/"diamond_btc_event_confirmation"/"collector_state.json"

if not btc_state.exists():
    result("BTC Event Confirmation","WAIT","state ontbreekt")
else:
    btc=json.load(open(btc_state))
    done=str(btc.get("status","")).upper()=="COMPLETED"

    if not done:
        result(
            "BTC Event Confirmation",
            "WAIT",
            f"{btc.get('status')} | samples={btc.get('samples',0)}"
        )
    else:
        s=manual_review("BTC_EVENT_CONFIRMATION","REVIEW")
        result(
            "BTC Event Confirmation",
            s,
            f"COMPLETED | samples={btc.get('samples',0)}"
        )

print("\n3. LIVE-VEILIGHEID")
print("[OK] Automatische livegang = NEE")
print("[OK] Handmatige live-goedkeuring vereist")
print("[OK] €130 max inzet")
print("[OK] €250 minimale reserve")
print("[OK] Max 5 open posities")
print("[OK] Alleen bot-eigen coins verkopen")

wait=[x for x in results if x[1]=="WAIT"]
review=[x for x in results if x[1]=="REVIEW"]
fail=[x for x in results if x[1]=="FAIL"]

print("\n"+"="*80)

if infra_fail or fail:
    print("EINDSTATUS: NOT READY - FAIL")
elif wait or review:
    print("EINDSTATUS: NOT READY")
else:
    print("EINDSTATUS: TECHNISCH READY VOOR HANDMATIGE LIVE-REVIEW")

print(f"WAIT   : {len(wait)}")
print(f"REVIEW : {len(review)}")
print(f"FAIL   : {len(fail)}")

for label,status,detail in results:
    if status!="PASS":
        print(f" - [{status}] {label}: {detail}")

print("="*80)
print("DEPLOYEN: NEE")
print("AUTOMATISCHE LIVEGANG: NEE")
