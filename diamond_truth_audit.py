#!/usr/bin/env python3
import csv, json
from pathlib import Path

VERSION = "1.2"
D = Path("/var/data")
ROOT = Path("/opt/render/project/src")

def load(name):
    with open(D / name, encoding="utf-8") as f:
        return json.load(f)

print("=== DIAMOND TRADER TRUTH AUDIT v1.2 ===")

baseline = load("diamond_test_baseline.json")
with open(D / "diamond_transactions.csv", newline="") as f:
    tx = list(csv.DictReader(f))

buys = sum(str(r.get("side", "")).upper() == "BUY" for r in tx)
start = int(baseline.get("start_spot_trades", 0))

state = load("diamond_state.json")
completed_spot = int(state.get("trades", 0) or 0)
open_positions = len(state.get("positions") or {})

bot_long = max(0, completed_spot - start)

with open(D / "diamond_short_execution.csv", newline="") as f:
    short = list(csv.DictReader(f))
short_closed = sum(r.get("event") == "CLOSE" for r in short)

print(
    f"Readiness Bot LONG : {bot_long}/20 "
    f"({completed_spot} afgerond - baseline {start} | "
    f"{open_positions} open)"
)
print(f"Paper-short V3     : {short_closed}/20")

long_files = [
    ("Long Entry", "diamond_long_entry_shadow_state.json"),
    ("Long Min Profit", "diamond_long_min_profit_shadow_state.json"),
    ("Long Combo v1", "diamond_long_combo_shadow_state.json"),
    ("Long Combo v2", "diamond_long_combo_shadow_v2_state.json"),
]

for label, name in long_files:
    state = load(name)
    print(f"{label:18}: {len(state.get('signals') or {})}/20")

sel = load("diamond_scanner_selective_shadow_report.json")
variants = sel.get("variants") or {}

for name in ("SELECTIVE", "STRONG"):
    x = variants.get(name) or {}
    print(
        f"{name:18}: closed={int(x.get('closed') or 0)} "
        f"pnl=€{float(x.get('net_pnl_eur') or 0):+.2f}"
    )

scripts = [
    "long_entry_shadow_lab.py",
    "long_min_profit_shadow_lab.py",
    "long_combo_shadow_lab.py",
    "long_combo_shadow_lab_v2.py",
]

guard = sum(
    "30*60*1000" in (ROOT / f).read_text()
    for f in scripts
)
print(f"LONG backfill guard: {guard}/4")

from datetime import datetime

late_total = 0
for label, name in long_files:
    state = load(name)
    late = 0

    for sig in (state.get("signals") or {}).values():
        try:
            closed = datetime.fromisoformat(sig["signal_closed_at"])
            detected = datetime.fromisoformat(sig["detected_at"])
            delay = (detected - closed).total_seconds()

            if delay > 1810:
                late += 1
        except Exception:
            late += 1

    late_total += late
    print(f"{label+' late':18}: {late}")

runner = load("diamond_periodic_analysis_state.json")
bad = [
    name for name, task in (runner.get("tasks") or {}).items()
    if task.get("last_status") not in (None, "OK")
]
print(
    f"Periodic runner     : v{runner.get('version')} "
    f"| fouten={len(bad)}"
)

current = int(Path("/sys/fs/cgroup/memory.current").read_text())
maximum = int(Path("/sys/fs/cgroup/memory.max").read_text())
print(
    f"Geheugen            : {current/1024/1024:.1f}/"
    f"{maximum/1024/1024:.0f} MiB"
)

ok = guard == 4 and late_total == 0 and not bad
print()
print("AUDIT:", "OK" if ok else "CONTROLEREN")
