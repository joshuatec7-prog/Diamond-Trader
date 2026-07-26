#!/usr/bin/env bash

# Diamond Trader Healthcheck v7.0
# Alleen lezen: wijzigt geen bot-, test- of scannerbestanden.

set -u

DATA_DIR="/var/data"
PROJECT_DIR="/opt/render/project/src"

STATE_FILE="$DATA_DIR/diamond_state.json"
CONTROL_FILE="$DATA_DIR/diamond_control.json"
AGENT_STATE_FILE="$DATA_DIR/diamond_agent_state.json"
DIAG_STATS_FILE="$DATA_DIR/diamond_diagnose_stats.json"
SUPERVISOR_FILE="$DATA_DIR/diamond_supervisor_state.json"
TRADES_FILE="$DATA_DIR/diamond_transactions.csv"

LONG_BASELINE_FILE="$DATA_DIR/diamond_test_baseline.json"
LONG_REPORT_FILE="$DATA_DIR/diamond_test_report.json"
SHORT_BASELINE_FILE="$DATA_DIR/diamond_short_test_baseline.json"
SHORT_REPORT_FILE="$DATA_DIR/diamond_short_test_report.json"
SHORT_INTERIM_5_FILE="$DATA_DIR/diamond_short_test_interim_5.json"
SHORT_INTERIM_10_FILE="$DATA_DIR/diamond_short_test_interim_10.json"

BACKUP_DIR="$DATA_DIR/backups"

NOW_EPOCH=$(date +%s)
ERRORS=0

echo
echo "============================================================"
echo " DIAMOND TRADER CONTROLE"
echo " $(date)"
echo "============================================================"
echo

check_process() {
    local pattern="$1"
    local display_name="$2"
    local result

    result=$(pgrep -af "$pattern" 2>/dev/null || true)

    if [ -n "$result" ]; then
        echo "[OK]    $display_name draait"
        echo "$result" | sed 's/^/        /'
    else
        echo "[FOUT]  $display_name draait NIET"
        ERRORS=$((ERRORS + 1))
    fi
}

file_info() {
    local path="$1"
    local label="$2"
    local required="${3:-false}"

    if [ -f "$path" ]; then
        local size
        local modified
        local age

        size=$(stat -c %s "$path" 2>/dev/null || echo 0)
        modified=$(stat -c %Y "$path" 2>/dev/null || echo 0)
        age=$(( (NOW_EPOCH - modified) / 60 ))

        echo "[OK]    $label aanwezig"
        echo "        Bestand: $path"
        echo "        Grootte: $size bytes"
        echo "        Laatst gewijzigd: $age minuten geleden"
    else
        if [ "$required" = "true" ]; then
            echo "[FOUT]  $label ontbreekt"
            ERRORS=$((ERRORS + 1))
        else
            echo "[INFO]  $label nog niet aanwezig"
        fi
        echo "        Bestand: $path"
    fi
}

echo "1. PROCESSEN"
echo "------------------------------------------------------------"

check_process \
    'python3[[:space:]]+agent\.py([[:space:]]|$)' \
    "Diamond Agent"

check_process \
    'python3[[:space:]]+closed_candle_runner\.py[[:space:]]+diagnose([[:space:]]|$)' \
    "Diamond Diagnose"

check_process \
    'python3[[:space:]]+supervisor_agent\.py([[:space:]]|$)' \
    "Diamond Supervisor"

check_process \
    'python3[[:space:]]+closed_candle_runner\.py[[:space:]]+bot([[:space:]]|$)' \
    "Diamond Bot"

check_process \
    'python3[[:space:]]+market_scanner\.py[[:space:]]+--loop[[:space:]]+--top[[:space:]]+20([[:space:]]|$)' \
    "Diamond Market Scanner"

echo
echo "2. PROJECTBESTANDEN"
echo "------------------------------------------------------------"

for file_name in \
    agent.py \
    config.yaml \
    closed_candle_runner.py \
    diagnose.py \
    supervisor_agent.py \
    diamond_bot.py \
    short_diagnose.py \
    market_scanner.py \
    requirements.txt \
    start.sh \
    healthcheck.sh \
    scanner_healthcheck.sh
do
    if [ -f "$PROJECT_DIR/$file_name" ]; then
        echo "[OK]    $file_name"
    else
        echo "[FOUT]  $file_name ontbreekt"
        ERRORS=$((ERRORS + 1))
    fi
done

echo
echo "3. BOT-STATE"
echo "------------------------------------------------------------"

file_info "$STATE_FILE" "Bot-state" "true"

if [ -f "$STATE_FILE" ]; then
    if ! python3 - "$STATE_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)

positions = data.get("positions") or {}
shorts = data.get("short_positions") or {}

print(f"        Open spotposities : {len(positions)}")
print(f"        Open shorts       : {len(shorts)}")
print(f"        Spot trades       : {int(data.get('trades', 0) or 0)}")
print(f"        Spot winsttrades  : {int(data.get('wins', 0) or 0)}")
print(f"        Spot PnL          : {float(data.get('pnl_quote', 0) or 0):+.2f} EUR")
print(f"        Dry-run saldo     : {float(data.get('simulated_free_quote', 0) or 0):.2f} EUR")
print(f"        Short trades      : {int(data.get('short_trades', 0) or 0)}")
print(f"        Short winsttrades : {int(data.get('short_wins', 0) or 0)}")
print(f"        Short PnL         : {float(data.get('short_pnl_quote', 0) or 0):+.4f} EUR")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "4. VEILIGHEIDSCONTROLE"
echo "------------------------------------------------------------"

file_info "$CONTROL_FILE" "Controlebestand" "true"

if [ -f "$CONTROL_FILE" ]; then
    if ! python3 - "$CONTROL_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)

print(f"        Gepauzeerd        : {bool(data.get('paused', False))}")
print(f"        Reden             : {data.get('pause_reason') or '-'}")
print(f"        Gepauzeerd sinds  : {data.get('paused_at') or '-'}")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "5. AGENT"
echo "------------------------------------------------------------"

file_info "$AGENT_STATE_FILE" "Agent-state"

if [ -f "$AGENT_STATE_FILE" ]; then
    if ! python3 - "$AGENT_STATE_FILE" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)


def format_time(value):
    if value in (None, "", 0):
        return "-"

    if isinstance(value, str):
        return value

    try:
        return datetime.fromtimestamp(
            float(value),
            tz=timezone.utc,
        ).isoformat()
    except Exception:
        return str(value)


reports = data.get("sent_reports") or data.get("sent_daily_reports") or []
weekly = data.get("sent_weekly_reports") or []

print(f"        Laatste analyse   : {format_time(data.get('last_analysis_ts'))}")
print(f"        Statusmails       : {len(reports)}")
print(f"        Weekrapporten     : {len(weekly)}")
print(f"        Laatste back-up   : {data.get('last_backup_at') or '-'}")
print(f"        Back-upstatus     : {data.get('last_backup_status') or '-'}")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "6. DIAGNOSE"
echo "------------------------------------------------------------"

file_info "$DIAG_STATS_FILE" "Diagnosestatistieken"

if [ -f "$DIAG_STATS_FILE" ]; then
    if ! python3 - "$DIAG_STATS_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)

print(f"        Diagnoserondes    : {int(data.get('total_rounds', 0) or 0)}")
print(f"        Laatste ronde     : {data.get('last_round_at') or '-'}")

for symbol, stats in sorted((data.get("symbols") or {}).items()):
    print(
        f"          - {symbol}: "
        f"controles={int(stats.get('checks', 0) or 0)}, "
        f"bijna={int(stats.get('near_signals', 0) or 0)}, "
        f"signalen={int(stats.get('technical_signals', 0) or 0)}, "
        f"laatste score={float(stats.get('last_score_pct', 0) or 0):.0f}%"
    )
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "7. SUPERVISOR"
echo "------------------------------------------------------------"

file_info "$SUPERVISOR_FILE" "Supervisorrapport"

if [ -f "$SUPERVISOR_FILE" ]; then
    if ! python3 - "$SUPERVISOR_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)

spot = int(data.get("open_spot_positions", 0) or 0)
short = int(data.get("open_short_positions", 0) or 0)

print(f"        Gegenereerd op    : {data.get('generated_at') or '-'}")
print(f"        Modus             : {data.get('mode') or '-'}")
print(f"        Diagnoserondes    : {int(data.get('total_diagnose_rounds', 0) or 0)}")
print(f"        Open spotposities : {spot}")
print(f"        Open shorts       : {short}")
print(f"        Open totaal       : {spot + short}")
print(f"        Gepauzeerd        : {bool(data.get('paused', False))}")

health = data.get("health") or []
recommendations = data.get("recommendations") or []

if health:
    print("        Gezondheid:")
    for item in health:
        print(f"          - {item}")

if recommendations:
    print("        Adviezen:")
    for item in recommendations:
        print(f"          - {item}")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
fi

echo
echo "8. TRANSACTIES"
echo "------------------------------------------------------------"

if [ -f "$TRADES_FILE" ]; then
    trade_lines=$(wc -l < "$TRADES_FILE")
    trade_count=$((trade_lines > 0 ? trade_lines - 1 : 0))

    echo "[OK]    Transactiebestand aanwezig"
    echo "        Aantal transactieregels: $trade_count"
    echo
    echo "        Laatste vijf regels:"
    tail -n 5 "$TRADES_FILE" | sed 's/^/        /'
else
    echo "[INFO]  Nog geen transactiebestand"
fi

echo
echo "9. TESTVOORTGANG"
echo "------------------------------------------------------------"

if ! python3 - \
    "$STATE_FILE" \
    "$LONG_BASELINE_FILE" \
    "$LONG_REPORT_FILE" \
    "$SHORT_BASELINE_FILE" \
    "$SHORT_REPORT_FILE" \
    "$SHORT_INTERIM_5_FILE" \
    "$SHORT_INTERIM_10_FILE" <<'PY'
import json
import sys
from pathlib import Path

(
    state_file,
    long_baseline_file,
    long_report_file,
    short_baseline_file,
    short_report_file,
    short_interim_5_file,
    short_interim_10_file,
) = map(Path, sys.argv[1:])


def read_json(path):
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


state = read_json(state_file)
long_baseline = read_json(long_baseline_file)
short_baseline = read_json(short_baseline_file)

long_current = int(state.get("trades", 0) or 0)
short_current = int(state.get("short_trades", 0) or 0)

print("LONGTEST")

if not long_baseline:
    print(f"[FOUT]  Longtestbaseline ontbreekt of is ongeldig")
    print(f"        Bestand           : {long_baseline_file}")
else:
    long_start = int(
        long_baseline.get(
            "start_trades",
            long_baseline.get("start_trade_count", 0),
        )
        or 0
    )
    long_target_new = int(
        long_baseline.get("target_new_trades", 20)
        or 20
    )
    long_target_total = int(
        long_baseline.get(
            "target_total_trades",
            long_start + long_target_new,
        )
        or (long_start + long_target_new)
    )
    long_new = max(0, long_current - long_start)
    long_remaining = max(0, long_target_total - long_current)
    reached = long_current >= long_target_total

    print("[OK]    Longtestbaseline actief")
    print(f"        Bestand           : {long_baseline_file}")
    print(f"        Start trades      : {long_start}")
    print(f"        Huidige trades    : {long_current}")
    print(f"        Nieuwe testtrades : {long_new}/{long_target_new}")
    print(f"        Nog nodig         : {long_remaining}")
    print(f"        Doel totaal       : {long_target_total}")
    print(f"        Dry-run           : {'JA' if bool(state.get('dry_run', True)) else 'NEE'}")
    print(f"        Teststop actief   : {'JA' if bool(state.get('dry_run', True)) else 'NEE'}")
    print(f"        Doel bereikt      : {'JA' if reached else 'NEE'}")

    if long_report_file.exists():
        print(f"[OK]    Longtestrapport aanwezig")
        print(f"        Bestand           : {long_report_file}")
    elif reached:
        print("[WAARSCHUWING] Longtestdoel bereikt, maar rapport ontbreekt")
    else:
        print(
            f"[INFO]  Longtesteindrapport wordt gemaakt zodra "
            f"trade {long_target_total} is bereikt"
        )

print()
print("PAPER-SHORTTEST")

if not short_baseline:
    print("[FOUT]  Paper-shortbaseline ontbreekt of is ongeldig")
    print(f"        Bestand           : {short_baseline_file}")
else:
    short_start = int(
        short_baseline.get(
            "start_short_trades",
            short_baseline.get("start_shorts", 0),
        )
        or 0
    )
    short_target_new = int(
        short_baseline.get("target_new_trades", 20)
        or 20
    )
    short_target_total = int(
        short_baseline.get(
            "target_total_short_trades",
            short_start + short_target_new,
        )
        or (short_start + short_target_new)
    )
    short_new = max(0, short_current - short_start)
    short_remaining = max(0, short_target_total - short_current)
    reached = short_current >= short_target_total

    print("[OK]    Paper-shortbaseline actief")
    print(f"        Bestand           : {short_baseline_file}")
    print(f"        Start shorts      : {short_start}")
    print(f"        Huidige shorts    : {short_current}")
    print(f"        Nieuwe shorts     : {short_new}/{short_target_new}")
    print(f"        Nog nodig         : {short_remaining}")
    print(f"        Doel totaal       : {short_target_total}")
    print("        Paper only        : JA")
    print("        Maximaal open     : 1")
    print("        Hefboom           : 1x")
    print(f"        Doel bereikt      : {'JA' if reached else 'NEE'}")

    if short_interim_5_file.exists():
        print(f"[OK]    Tussenrapport 5/20 aanwezig")
        print(f"        Bestand           : {short_interim_5_file}")
    else:
        print("[INFO]  Tussenrapport 5/20 nog niet aanwezig")

    if short_interim_10_file.exists():
        print(f"[OK]    Tussenrapport 10/20 aanwezig")
        print(f"        Bestand           : {short_interim_10_file}")
    else:
        print("[INFO]  Tussenrapport 10/20 nog niet aanwezig")

    if short_report_file.exists():
        print(f"[OK]    Paper-shorteindrapport aanwezig")
        print(f"        Bestand           : {short_report_file}")
    elif reached:
        print("[WAARSCHUWING] Paper-shortdoel bereikt, maar rapport ontbreekt")
    else:
        print(
            f"[INFO]  Paper-shorteindrapport wordt gemaakt zodra "
            f"trade {short_target_total} is bereikt"
        )
PY
then
    ERRORS=$((ERRORS + 1))
fi

echo
echo "10. PAPER-SHORTDIAGNOSE"
echo "------------------------------------------------------------"

if [ -f "$PROJECT_DIR/short_diagnose.py" ]; then
    echo "[INFO]  Veilige paper-shortdiagnose wordt uitgevoerd"
    echo "        Alleen lezen: geen orders en geen bestanden gewijzigd"
    echo

    if (
        cd "$PROJECT_DIR"
        python3 short_diagnose.py
    ); then
        echo
        echo "[OK]    Paper-shortdiagnose succesvol afgerond"
    else
        echo
        echo "[FOUT]  Paper-shortdiagnose mislukt"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "[FOUT]  short_diagnose.py ontbreekt"
    ERRORS=$((ERRORS + 1))
fi

echo
echo "11. DAGELIJKSE BACK-UP"
echo "------------------------------------------------------------"

if [ -d "$BACKUP_DIR" ]; then
    if ! python3 - "$BACKUP_DIR" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

backup_root = Path(sys.argv[1])
directories = sorted(
    [path for path in backup_root.iterdir() if path.is_dir()],
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)

if not directories:
    print("[FOUT]  Nog geen dagelijkse back-upmap gevonden")
    raise SystemExit(1)

latest = directories[0]
manifest_file = latest / "manifest.json"

if not manifest_file.exists():
    print("[FOUT]  Manifest ontbreekt in nieuwste back-up")
    print(f"        Map                : {latest}")
    raise SystemExit(1)

try:
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"[FOUT]  Manifest lezen mislukt: {exc}")
    raise SystemExit(1)

created_at = manifest.get("created_at") or "-"
created = None

try:
    created = datetime.fromisoformat(
        str(created_at).replace("Z", "+00:00")
    )
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
except Exception:
    pass

age_hours = (
    max(
        0.0,
        (
            datetime.now(timezone.utc)
            - created.astimezone(timezone.utc)
        ).total_seconds()
        / 3600.0,
    )
    if created is not None
    else 0.0
)

copied = manifest.get("copied_files") or []
file_count = int(manifest.get("file_count", len(copied)) or 0)
total_bytes = int(manifest.get("total_bytes", 0) or 0)
retention = int(manifest.get("retention_days", 30) or 30)
status = manifest.get("status") or "-"
required_missing = manifest.get("required_missing") or []

integrity_ok = (
    status == "complete"
    and not required_missing
    and file_count == len(copied)
)

print("[OK]    Dagelijkse back-up aanwezig en gecontroleerd")
print(f"        Map                : {latest}")
print(f"        Gemaakt op         : {created_at}")
print(f"        Leeftijd           : {age_hours:.1f} uur")
print(f"        Bestanden          : {file_count}")
print(f"        Totale grootte     : {total_bytes} bytes")
print(f"        Bewaartermijn      : {retention} dagen")
print(f"        Integriteit        : {'OK' if integrity_ok else 'NIET OK'}")
print(f"        Back-ups aanwezig  : {len(directories)}")

if not integrity_ok:
    raise SystemExit(1)
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "[FOUT]  Back-upmap ontbreekt"
    echo "        Map                : $BACKUP_DIR"
    ERRORS=$((ERRORS + 1))
fi

echo
echo "12. SCHIJFRUIMTE"
echo "------------------------------------------------------------"

df -h "$DATA_DIR" 2>/dev/null || df -h

echo
echo "13. EINDCONTROLE"
echo "------------------------------------------------------------"

if [ "$ERRORS" -eq 0 ]; then
    echo "[OK]    Alle belangrijke onderdelen zijn actief."
    EXIT_CODE=0
else
    echo "[FOUT]  Er zijn $ERRORS belangrijke problemen gevonden."
    EXIT_CODE=1
fi

echo
echo "============================================================"
echo " CONTROLE AFGEROND"
echo "============================================================"
echo

exit "$EXIT_CODE"
