#!/usr/bin/env python3
import json
import re
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

def mark(line):
    m = re.search(r"closed=\s*(\d+)", line)
    if not m:
        return line
    n = int(m.group(1))
    if n >= 20:
        status = "EINDANALYSE"
    elif n >= 10:
        status = "10/20"
    elif n >= 5:
        status = "5/20"
    else:
        status = "LOOPT"
    return f"{line}  [{status}]"

def section(title, script, names):
    print()
    print(title)
    text = run(script)
    found = []
    for line in text.splitlines():
        u = line.upper()
        if any(name in u for name in names):
            found.append(mark(line.strip()))
    if found:
        for line in found:
            print(" ", line)
    else:
        print("  Nog geen bruikbare gesloten trades.")

base = run("diamond_decision_gate.py")
base = base.replace(
    "20/20 = pas serieus kandidaat voor eindbesluit",
    "20/20 = voldoende sample voor eindanalyse",
)
print(base.rstrip())

print()
print("=" * 92)
print(" PROSPECTIEVE QUALITY LAGEN v1.3")
print("=" * 92)

section(
    "LONG QUALITY",
    "long_quality_shadow.py",
    [
        "ALL_ELIGIBLE", "TREND_BREAKOUT",
        "TB_HIGH_SCORE", "TB_HIGH_VOLUME",
        "TB_LOW_SPREAD", "TB_SCORE_VOLUME",
        "PULLBACK_CONTROL",
    ],
)

section(
    "SHORT QUALITY",
    "short_quality_shadow.py",
    [
        "ALL_ELIGIBLE", "MOMENTUM_BEARISH_WEAK",
        "MBW_HIGH_SCORE", "MBW_HIGH_VOLUME",
        "MBW_LOW_SPREAD", "TREND_BREAKOUT_CONTROL",
    ],
)

section(
    "REGIME SHADOW",
    "scanner_regime_shadow_lab.py",
    [
        "CURRENT", "COMPRESSION",
        "BTC_ALIGNED", "BTC_OPPOSITE",
        "EXPANSION", "HIGH_VOL_CHOP",
    ],
)

section(
    "EXECUTION QUALITY",
    "scanner_execution_quality_shadow.py",
    [
        "BASELINE", "HIGH_VOLUME",
        "HIGH_VOLUME_QUOTE", "LOW_VOLUME",
        "STRONG_HIGH_VOLUME",
    ],
)

print()
print("BTC EVENT CONFIRMATION")

state_file = (
    DATA /
    "diamond_btc_event_confirmation" /
    "collector_state.json"
)

if state_file.exists():
    d = json.loads(state_file.read_text())
    print(
        f"  status={d.get('status')} | "
        f"samples={d.get('samples')} | "
        f"errors={d.get('errors')}"
    )
    print(
        f"  gestart={d.get('started_at')} | "
        f"laatste={d.get('last_sample_at')}"
    )
    print("  BTC-event bewijs: DATA VERZAMELEN")
else:
    print("  Collectorstate niet gevonden.")

print()
print("=" * 92)
print(" CENTRALE BESLISSING")
print("=" * 92)
print("Historisch onderzoek : VOLDOENDE")
print("Prospectieve tests    : LOPEN")
print("Live-goedkeuring      : NEE")
print("Automatische livegang : NEE")
