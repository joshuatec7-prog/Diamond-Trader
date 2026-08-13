#!/usr/bin/env python3
# Diamond Trader Release / Go-Live Readiness v1.2

import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path("/opt/render/project/src")
DATA = Path("/var/data")

RULE_HASH = "4556c32f7f6ebf172d1b4ec1fc66bdd0ebcdc3938a470520d1ffe89574927b0b"
SAFETY_HASH = "c03e33f490f7e0db08f0d2da894fa75872453ce09c7c385302fee4b8fa0f39d3"

results = []
infra_fail = []


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(cmd):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
        ).stdout
    except Exception:
        return ""


def result(label, status, detail="", blocking=True):
    results.append((label, status, detail, blocking))
    tag = {
        "PASS": "PASS",
        "WAIT": "WAIT",
        "REVIEW": "REVIEW",
        "FAIL": "FAIL",
        "ARCHIVED": "ARCHIVED",
    }[status]
    extra = f" | {detail}" if detail else ""
    suffix = "" if blocking else " | research"
    print(f"[{tag}] {label}{extra}{suffix}")


def infra(label, good):
    print(f"[{'OK' if good else 'FAIL'}] {label}")
    if not good:
        infra_fail.append(label)


def status_from_text(text):
    t = str(text).upper()
    if "GESLAAGD" in t:
        return "PASS"
    if "AFWIJZEN" in t:
        return "FAIL"
    if "HANDMATIGE" in t or "EINDREVIEW" in t:
        return "REVIEW"
    return "WAIT"


def load_json(path: Path, default=None):
    if default is None:
        default = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def config_value(cfg, dotted, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


manual_path = DATA / "diamond_manual_final_reviews.json"
manual = load_json(manual_path)


def manual_review(key, base):
    if base != "REVIEW":
        return base

    review = manual.get(key, {})
    if str(review.get("status", "")).upper() != "APPROVED":
        return "REVIEW"

    snap = Path(str(review.get("snapshot", "")))
    if not snap.is_dir():
        return "REVIEW"

    if not (snap / "SHA256SUMS.txt").exists():
        return "REVIEW"

    return "PASS"


print("=" * 80)
print(" DIAMOND TRADER RELEASE / GO-LIVE READINESS v1.2")
print("=" * 80)

gate = run(["python3", "diamond_decision_gate_v1_4.py"])
analyzer = run(["python3", "diamond_prospective_final_analyzer.py"])

print("\n1. SYSTEEM / RELEASE")

infra("Truth Audit", "AUDIT: OK" in gate)

m = re.search(r"Periodic runner\s*:.*fouten=(\d+)", gate)
infra("Periodic runner fouten=0", bool(m and int(m.group(1)) == 0))

required = [
    "diamond_prospective_final_analyzer.py",
    "diamond_prospective_decision_rules.json",
    "DIAMOND_ENDREVIEW_RUNBOOK.md",
    "diamond_capture_end_review.sh",
    "DIAMOND_GO_LIVE_ROLLBACK_RUNBOOK.md",
    "diamond_go_live_preflight.sh",
    "diamond_post_live_safety_rules.json",
    "DIAMOND_POST_LIVE_SAFETY_RUNBOOK.md",
]

for name in required:
    infra(name, (ROOT / name).exists())

rules = ROOT / "diamond_prospective_decision_rules.json"
safety = ROOT / "diamond_post_live_safety_rules.json"

infra(
    "Beslisregels checksum",
    rules.exists() and sha(rules) == RULE_HASH,
)

infra(
    "Post-live safety checksum",
    safety.exists() and sha(safety) == SAFETY_HASH,
)

delta = run(["python3", "diamond_release_delta_audit.py"])
infra(
    "Runtime gelijk aan Release Candidate",
    "Runtime CHANGED   : 0" in delta
    and "Runtime MISSING   : 0" in delta,
)


def section_status(title):
    pos = analyzer.find(title)
    if pos < 0:
        return "WAIT", "niet gevonden"

    part = analyzer[pos : pos + 1200]
    match = re.search(r"STATUS\s*:\s*([^\n]+)", part)

    if not match:
        return "WAIT", "geen eindstatus"

    text = match.group(1).strip()
    return status_from_text(text), text


print("\n2. RELEASE-GATES")

selective_status, selective_detail = section_status("SELECTIVE")
result("SELECTIVE", selective_status, selective_detail, blocking=True)

m = re.search(
    r"^BASELINE\s+(\d+)/20.*?\|\s*([^\n]+)",
    analyzer,
    re.M,
)

execution_n = 0
execution_status = "WAIT"
execution_detail = "niet gevonden"

if m:
    execution_n = int(m.group(1))
    raw = m.group(2).strip()
    execution_detail = f"{execution_n}/20 | {raw}"

    if execution_n < 20:
        execution_status = "WAIT"
    else:
        derived = status_from_text(raw)
        if derived == "FAIL":
            execution_status = "FAIL"
        elif derived == "PASS":
            execution_status = "PASS"
        else:
            execution_status = "REVIEW"

    execution_status = manual_review(
        "EXECUTION_BASELINE",
        execution_status,
    )

result(
    "Execution BASELINE",
    execution_status,
    execution_detail,
    blocking=True,
)

print("\n3. ONDERZOEK - NIET BLOKKEREND")

for label, title in (
    ("STRONG", "STRONG"),
    ("BTC_ALIGNED", "REGIME BTC_ALIGNED"),
):
    status, detail = section_status(title)
    result(label, status, detail, blocking=False)

paper_status, paper_detail = section_status("PAPER-SHORT V3")
if paper_status == "FAIL":
    result(
        "Paper-short V3",
        "ARCHIVED",
        paper_detail,
        blocking=False,
    )
else:
    paper_status = manual_review("PAPER_SHORT_V3", paper_status)
    result(
        "Paper-short V3",
        paper_status,
        paper_detail,
        blocking=False,
    )

long_part = gate[gate.find("LONG QUALITY") : gate.find("SHORT QUALITY")]
m = re.search(r"ALL_ELIGIBLE.*?closed=\s*(\d+)/20", long_part)
long_n = int(m.group(1)) if m else 0
long_status = "REVIEW" if long_n >= 20 else "WAIT"
long_status = manual_review("LONG_QUALITY", long_status)
result(
    "LONG Quality",
    long_status,
    f"{long_n}/20",
    blocking=False,
)

short_start = gate.find("SHORT QUALITY")
short_end = gate.find("REGIME SHADOW", short_start)
short_part = gate[short_start:short_end]
m = re.search(r"ALL_ELIGIBLE.*?closed=\s*(\d+)/20", short_part)
short_n = int(m.group(1)) if m else 0
short_status = "REVIEW" if short_n >= 20 else "WAIT"
short_status = manual_review("SHORT_QUALITY", short_status)
result(
    "SHORT Quality",
    short_status,
    f"{short_n}/20",
    blocking=False,
)

master_path = DATA / "diamond_master_decision_shadow_report.json"
master = load_json(master_path)
master_n = int(master.get("closed", 0) or 0)

if master_n < 20:
    result(
        "Master Decision",
        "WAIT",
        f"{master_n}/20",
        blocking=False,
    )
else:
    status, detail = section_status("MASTER TRADE-LEVEL")
    result(
        "Master Decision",
        status,
        detail,
        blocking=False,
    )

btc_state = DATA / "diamond_btc_event_confirmation" / "collector_state.json"
btc = load_json(btc_state)
if not btc:
    result(
        "BTC Event Confirmation",
        "WAIT",
        "state ontbreekt",
        blocking=False,
    )
else:
    done = str(btc.get("status", "")).upper() == "COMPLETED"
    if not done:
        result(
            "BTC Event Confirmation",
            "WAIT",
            f"{btc.get('status')} | samples={btc.get('samples', 0)}",
            blocking=False,
        )
    else:
        status = manual_review("BTC_EVENT_CONFIRMATION", "REVIEW")
        result(
            "BTC Event Confirmation",
            status,
            f"COMPLETED | samples={btc.get('samples', 0)}",
            blocking=False,
        )

print("\n4. SAFETY / RECOVERY")

safety_script = DATA / "diamond_safety_recovery_shadow.py"
if safety_script.exists():
    run(["python3", str(safety_script)])

safety_report = load_json(
    DATA / "diamond_safety_recovery_shadow_report.json"
)
safety_checks = safety_report.get("checks", [])
safety_total = len(safety_checks)
safety_passed = sum(
    1
    for item in safety_checks
    if str(item.get("status", "")).upper() == "PASS"
)
safety_ready = safety_total >= 7 and safety_passed == safety_total

print(
    f"[{'PASS' if safety_ready else 'WAIT'}] "
    f"Safety / Recovery | {safety_passed}/{max(7, safety_total)}"
)

config_path = ROOT / "config.yaml"
try:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
except Exception:
    cfg = {}

dry_run = bool(config_value(cfg, "risk.dry_run", True))
reserve = float(config_value(cfg, "risk.eur_reserve", 0) or 0)
max_open = int(config_value(cfg, "risk.max_open_positions", 999) or 999)
max_total = int(config_value(cfg, "trading.max_total_positions", 999) or 999)

state_raw = config_value(
    cfg,
    "files.state_file",
    "/var/data/diamond_state.json",
)
state_path = Path(str(state_raw))
if not state_path.is_absolute():
    state_path = ROOT / state_path

state = load_json(state_path)
pending = state.get("pending_orders", {})
if not isinstance(pending, dict):
    pending = {}

recovery_required = bool(state.get("recovery_required", False))
recovery_reason = str(state.get("recovery_reason", "") or "")

print(f"[{'OK' if dry_run else 'FAIL'}] Dry-run actief")
print(f"[{'OK' if reserve >= 250 else 'FAIL'}] Reserve >= €250 | €{reserve:.2f}")
print(
    f"[{'OK' if max_open <= 5 and max_total <= 5 else 'FAIL'}] "
    f"Max posities <= 5 | spot={max_open} totaal={max_total}"
)
print(
    f"[{'OK' if not pending else 'WAIT'}] "
    f"Pending orders leeg | {len(pending)}"
)
print(
    f"[{'OK' if not recovery_required else 'WAIT'}] "
    f"Recovery vrij | {recovery_reason or 'geen'}"
)

print("\n5. FASE GEREEDHEID")

paper_ready = selective_status == "PASS"

canary_ready = bool(
    paper_ready
    and execution_status == "PASS"
    and safety_ready
    and not infra_fail
    and dry_run
    and reserve >= 250
    and max_open <= 5
    and max_total <= 5
    and not pending
    and not recovery_required
)

approval = load_json(DATA / "diamond_live_approval.json")
approval_ok = bool(
    str(approval.get("status", "")).upper() == "APPROVED"
    or approval.get("approved") is True
)

live_active = bool(
    not dry_run
    and approval_ok
    and safety_ready
    and not pending
    and not recovery_required
)

print(f"PAPER READY  : {'JA' if paper_ready else 'NEE'}")
print(f"CANARY READY : {'JA' if canary_ready else 'NEE'}")
print(f"LIVE ACTIVE  : {'JA' if live_active else 'NEE'}")

if not canary_ready:
    blockers = []
    if not paper_ready:
        blockers.append("SELECTIVE niet PASS")
    if execution_status != "PASS":
        blockers.append(
            f"Execution {execution_n}/20 ({execution_status})"
        )
    if not safety_ready:
        blockers.append(
            f"Safety {safety_passed}/{max(7, safety_total)}"
        )
    if infra_fail:
        blockers.append(
            "Infra: " + ", ".join(infra_fail)
        )
    if not dry_run:
        blockers.append("dry_run moet vóór canary nog actief zijn")
    if reserve < 250:
        blockers.append("reserve < €250")
    if max_open > 5 or max_total > 5:
        blockers.append("meer dan 5 posities toegestaan")
    if pending:
        blockers.append(f"{len(pending)} pending order(s)")
    if recovery_required:
        blockers.append(
            "recovery_required"
            + (f": {recovery_reason}" if recovery_reason else "")
        )

    for blocker in blockers:
        print(f" - {blocker}")

phase_status = {
    "paper_ready": paper_ready,
    "canary_ready": canary_ready,
    "live_active": live_active,
    "selective_status": selective_status,
    "execution_status": execution_status,
    "execution_closed": execution_n,
    "safety_passed": safety_passed,
    "safety_total": max(7, safety_total),
    "pending_orders": len(pending),
    "recovery_required": recovery_required,
}
try:
    (DATA / "diamond_release_phase_status.json").write_text(
        json.dumps(
            phase_status,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
except Exception:
    pass

blocking_wait = [
    x for x in results
    if x[3] and x[1] == "WAIT"
]
blocking_review = [
    x for x in results
    if x[3] and x[1] == "REVIEW"
]
blocking_fail = [
    x for x in results
    if x[3] and x[1] == "FAIL"
]

print("\n" + "=" * 80)

if infra_fail or blocking_fail:
    print("EINDSTATUS: NOT READY - FAIL")
elif canary_ready:
    print("EINDSTATUS: CANARY READY VOOR HANDMATIGE GOEDKEURING")
else:
    print("EINDSTATUS: NOT READY")

print(f"BLOCKING WAIT   : {len(blocking_wait)}")
print(f"BLOCKING REVIEW : {len(blocking_review)}")
print(f"BLOCKING FAIL   : {len(blocking_fail)}")

for label, status, detail, blocking in results:
    if blocking and status != "PASS":
        print(f" - [{status}] {label}: {detail}")

print("=" * 80)
print("DEPLOYEN: NEE")
print("AUTOMATISCHE LIVEGANG: NEE")
