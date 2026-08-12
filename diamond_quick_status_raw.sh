#!/usr/bin/env bash

python3 diamond_master_status.py | grep -E \
'Status  :|Samples :|Current :|oom_kill|SELECTIVE  accepted|STRONG     accepted|CURRENT    accepted|WAIT_15M|WAIT15_050|OFF_HOURS|DAYTIME|SECOND_CHANCE  :|BREAKOUT_ONLY|STRONG_QUALITY|Volgende stap|Longtest|Paper-shorttest'

echo "---- EXTRA VOORTGANG ----"

python3 - <<'PY'
import json
from datetime import datetime, timezone

p="/var/data/diamond_market_lead_btc/btc_market_lead_state_v1_1.json"
try:
    s=json.load(open(p))
    start=datetime.fromisoformat(s["started_at"].replace("Z","+00:00"))
    hours=(datetime.now(timezone.utc)-start).total_seconds()/3600
    print(f"BTC 24H        : {hours:.1f}/24.0 uur | {min(100,hours/24*100):.1f}%")
except Exception:
    print("BTC 24H        : status niet beschikbaar")
PY

python3 long_combo_shadow_lab_v2.py --status 2>/dev/null | \
sed 's/Nieuwe LONG-signalen/Long Combo v2 signalen/' | \
grep -E 'Long Combo v2 signalen|WAIT15_050' | head -2

echo "---- TELLERS MET BRON ----"
python3 long_entry_shadow_lab.py --status 2>/dev/null | \
awk -F: '/Nieuwe LONG-signalen/{gsub(/^ +| +$/,"",$2); print "Long Entry Shadow      : "$2" | bron: long_entry_shadow"}'

python3 readiness_gate.py 2>/dev/null | \
awk -F: '/Bot LONG-test/{gsub(/^ +| +$/,"",$2); print "Readiness Bot LONG     : "$2" | bron: readiness_gate"}'
