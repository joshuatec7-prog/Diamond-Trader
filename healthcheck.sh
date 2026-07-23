#!/usr/bin/env bash

# Diamond Trader Healthcheck
# Alleen lezen: dit script verandert niets aan de bot of instellingen.

set -u

DATA_DIR="/var/data"
PROJECT_DIR="/opt/render/project/src"

STATE_FILE="$DATA_DIR/diamond_state.json"
CONTROL_FILE="$DATA_DIR/diamond_control.json"
AGENT_STATE_FILE="$DATA_DIR/diamond_agent_state.json"
DIAG_STATS_FILE="$DATA_DIR/diamond_diagnose_stats.json"
SUPERVISOR_FILE="$DATA_DIR/diamond_supervisor_state.json"
TRADES_FILE="$DATA_DIR/diamond_transactions.csv"
TEST_BASELINE_FILE="$DATA_DIR/diamond_test_baseline.json"
TEST_REPORT_FILE="$DATA_DIR/diamond_test_report.json"
SHORT_TEST_BASELINE_FILE="$DATA_DIR/diamond_short_test_baseline.json"
SHORT_TEST_REPORT_FILE="$DATA_DIR/diamond_short_test_report.json"
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

check_file() {
    local file_path="$1"
    local display_name="$2"
    local required="${3:-false}"

    if [ -f "$file_path" ]; then
        local modified_epoch
        local age_seconds
        local age_minutes
        local size_bytes

        modified_epoch=$(stat -c %Y "$file_path" 2>/dev/null || echo 0)
        age_seconds=$((NOW_EPOCH - modified_epoch))
        age_minutes=$((age_seconds / 60))
        size_bytes=$(stat -c %s "$file_path" 2>/dev/null || echo 0)

        echo "[OK]    $display_name aanwezig"
        echo "        Bestand: $file_path"
        echo "        Grootte: $size_bytes bytes"
        echo "        Laatst gewijzigd: $age_minutes minuten geleden"
    else
        if [ "$required" = "true" ]; then
            echo "[FOUT]  $display_name ontbreekt"
            ERRORS=$((ERRORS + 1))
        else
            echo "[INFO]  $display_name nog niet aanwezig"
        fi

        echo "        Bestand: $file_path"
    fi
}

show_json_summary() {
    local file_path="$1"
    local summary_type="$2"

    if [ ! -f "$file_path" ]; then
        return
    fi

    if ! python3 - "$file_path" "$summary_type" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
summary_type = sys.argv[2]

try:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
except Exception as exc:
    print(f"        [FOUT] JSON lezen mislukt: {exc}")
    raise SystemExit(1)


def format_timestamp(value):
    if value in (None, "", 0):
        return "-"

    try:
        return datetime.fromtimestamp(
            float(value),
            tz=timezone.utc,
        ).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


if summary_type == "bot":
    positions = data.get("positions") or {}
    shorts = data.get("short_positions") or {}

    print(f"        Open spotposities : {len(positions)}")
    print(f"        Open shorts       : {len(shorts)}")
    print(f"        Spot trades       : {data.get('trades', 0)}")
    print(f"        Spot winsttrades  : {data.get('wins', 0)}")
    print(
        f"        Spot PnL          : "
        f"{float(data.get('pnl_quote', 0) or 0):+.2f} EUR"
    )
    print(
        f"        Dry-run saldo     : "
        f"{float(data.get('simulated_free_quote', 0) or 0):.2f} EUR"
    )

    if positions:
        print("        Posities:")

        for symbol, position in positions.items():
            amount = float(position.get("amount", 0) or 0)
            quote_amount = float(position.get("quote_amount", 0) or 0)
            opened_by_bot = bool(position.get("opened_by_bot", False))

            print(
                f"          - {symbol}: {amount:.8f} "
                f"(€{quote_amount:.2f}) | door bot={opened_by_bot}"
            )

elif summary_type == "control":
    print(f"        Gepauzeerd        : {bool(data.get('paused', False))}")
    print(f"        Reden             : {data.get('pause_reason') or '-'}")
    print(f"        Gepauzeerd sinds  : {data.get('paused_at') or '-'}")

elif summary_type == "diagnose":
    print(f"        Diagnoserondes    : {data.get('total_rounds', 0)}")
    print(f"        Laatste ronde     : {data.get('last_round_at') or '-'}")

    symbols = data.get("symbols") or {}

    for symbol, stats in sorted(symbols.items()):
        checks = int(stats.get("checks", 0) or 0)
        near = int(stats.get("near_signals", 0) or 0)
        signals = int(stats.get("technical_signals", 0) or 0)
        score = float(stats.get("last_score_pct", 0) or 0)

        print(
            f"          - {symbol}: controles={checks}, "
            f"bijna={near}, signalen={signals}, "
            f"laatste score={score:.0f}%"
        )

elif summary_type == "supervisor":
    spot_open = int(data.get("open_spot_positions", 0) or 0)
    short_open = int(data.get("open_short_positions", 0) or 0)

    print(f"        Gegenereerd op    : {data.get('generated_at') or '-'}")
    print(f"        Modus             : {data.get('mode') or '-'}")
    print(
        f"        Diagnoserondes    : "
        f"{data.get('total_diagnose_rounds', 0)}"
    )
    print(f"        Open spotposities : {spot_open}")
    print(f"        Open shorts       : {short_open}")
    print(f"        Open totaal       : {spot_open + short_open}")
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

elif summary_type == "agent":
    last_analysis = data.get("last_analysis_ts", 0)

    print(
        f"        Laatste analyse   : "
        f"{format_timestamp(last_analysis)}"
    )

    sent_reports = (
        data.get("sent_reports")
        or data.get("sent_daily_reports")
        or []
    )
    weekly_reports = data.get("sent_weekly_reports") or []

    print(f"        Statusmails       : {len(sent_reports)}")
    print(f"        Weekrapporten     : {len(weekly_reports)}")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
}

show_test_progress() {
    if [ ! -f "$TEST_BASELINE_FILE" ]; then
        echo "[FOUT]  Longtestbaseline ontbreekt"
        echo "        Bestand: $TEST_BASELINE_FILE"
        ERRORS=$((ERRORS + 1))
        return
    fi

    if [ ! -f "$SHORT_TEST_BASELINE_FILE" ]; then
        echo "[FOUT]  Paper-shortbaseline ontbreekt"
        echo "        Bestand: $SHORT_TEST_BASELINE_FILE"
        ERRORS=$((ERRORS + 1))
        return
    fi

    if ! python3 - \
        "$PROJECT_DIR" \
        "$TEST_BASELINE_FILE" \
        "$TEST_REPORT_FILE" \
        "$SHORT_TEST_BASELINE_FILE" \
        "$SHORT_TEST_REPORT_FILE" <<'PY'
import json
import sys
from pathlib import Path

project_dir = Path(sys.argv[1])
long_baseline_file = Path(sys.argv[2])
long_report_file = Path(sys.argv[3])
short_baseline_file = Path(sys.argv[4])
short_report_file = Path(sys.argv[5])

sys.path.insert(
    0,
    str(project_dir),
)

try:
    import agent
except Exception as exc:
    print(f"[FOUT]  agent.py importeren mislukt: {exc}")
    raise SystemExit(1)

required_functions = (
    "get_test_target_status",
    "check_test_target",
    "build_test_report",
    "get_short_test_target_status",
    "check_short_test_target",
    "build_short_test_report",
)

missing_functions = [
    name
    for name in required_functions
    if not hasattr(agent, name)
]

if missing_functions:
    print(
        "[FOUT]  Testfuncties ontbreken: "
        + ", ".join(missing_functions)
    )
    raise SystemExit(1)


def print_report_status(
    report_file: Path,
    target_reached: bool,
    target_total: int,
    label: str,
) -> None:
    if report_file.exists():
        try:
            with report_file.open(
                "r",
                encoding="utf-8",
            ) as file:
                report = json.load(file)

            print(f"[OK]    {label}rapport aanwezig")
            print(f"        Bestand           : {report_file}")
            print(
                f"        Test compleet     : "
                f"{'JA' if report.get('test_complete') else 'NEE'}"
            )
            print(
                f"        Trades in rapport : "
                f"{report.get('included_new_trades', 0)}"
            )
            print(
                f"        Gegenereerd op    : "
                f"{report.get('generated_at') or '-'}"
            )

        except Exception as exc:
            print(
                f"[WAARSCHUWING] {label}rapport lezen mislukt: {exc}"
            )
    else:
        if target_reached:
            print(
                f"[WAARSCHUWING] {label}doel bereikt, maar "
                "eindrapport nog niet aanwezig"
            )
            print(
                "        De agent maakt dit normaal binnen één minuut."
            )
        else:
            print(
                f"[INFO]  {label}eindrapport wordt gemaakt zodra "
                f"trade {target_total} is bereikt"
            )


try:
    long_status = agent.get_test_target_status()
except Exception as exc:
    print(f"[FOUT]  Longteststatus opvragen mislukt: {exc}")
    raise SystemExit(1)

if not long_status.get("enabled", False):
    print("[FOUT]  Longtestbaseline is niet geldig of niet actief")
    print(
        f"        Reden             : "
        f"{long_status.get('reason') or 'onbekend'}"
    )
    raise SystemExit(1)

long_start = int(
    long_status.get("start_trades", 0)
    or 0
)
long_target_total = int(
    long_status.get("target_total_trades", 0)
    or 0
)
long_current = int(
    long_status.get("current_trades", 0)
    or 0
)
long_new = int(
    long_status.get("new_trades", 0)
    or 0
)
long_remaining = int(
    long_status.get("remaining_trades", 0)
    or 0
)
long_target_new = max(
    0,
    long_target_total - long_start,
)
long_dry_run = bool(
    long_status.get("dry_run", False)
)
long_reached = bool(
    long_status.get("target_reached", False)
)

print("LONGTEST")
print("[OK]    Longtestbaseline actief")
print(f"        Bestand           : {long_baseline_file}")
print(f"        Start trades      : {long_start}")
print(f"        Huidige trades    : {long_current}")
print(
    f"        Nieuwe testtrades : "
    f"{long_new}/{long_target_new}"
)
print(f"        Nog nodig         : {long_remaining}")
print(f"        Doel totaal       : {long_target_total}")
print(
    f"        Dry-run           : "
    f"{'JA' if long_dry_run else 'NEE'}"
)
print(
    f"        Teststop actief   : "
    f"{'JA' if long_dry_run else 'NEE'}"
)
print(
    f"        Doel bereikt      : "
    f"{'JA' if long_reached else 'NEE'}"
)

print_report_status(
    long_report_file,
    long_reached,
    long_target_total,
    "Longtest",
)

try:
    short_status = agent.get_short_test_target_status()
except Exception as exc:
    print(f"[FOUT]  Paper-shortstatus opvragen mislukt: {exc}")
    raise SystemExit(1)

if not short_status.get("enabled", False):
    print("[FOUT]  Paper-shorttest is niet geldig of niet actief")
    print(
        f"        Reden             : "
        f"{short_status.get('reason') or 'onbekend'}"
    )
    raise SystemExit(1)

short_start = int(
    short_status.get("start_short_trades", 0)
    or 0
)
short_target_total = int(
    short_status.get("target_total_short_trades", 0)
    or 0
)
short_current = int(
    short_status.get("current_short_trades", 0)
    or 0
)
short_new = int(
    short_status.get("new_short_trades", 0)
    or 0
)
short_remaining = int(
    short_status.get("remaining_short_trades", 0)
    or 0
)
short_target_new = max(
    0,
    short_target_total - short_start,
)
short_reached = bool(
    short_status.get("target_reached", False)
)

print("")
print("PAPER-SHORTTEST")
print("[OK]    Paper-shortbaseline actief")
print(f"        Bestand           : {short_baseline_file}")
print(f"        Start shorts      : {short_start}")
print(f"        Huidige shorts    : {short_current}")
print(
    f"        Nieuwe shorts     : "
    f"{short_new}/{short_target_new}"
)
print(f"        Nog nodig         : {short_remaining}")
print(f"        Doel totaal       : {short_target_total}")
print("        Paper only        : JA")
print("        Maximaal open     : 1")
print("        Hefboom           : 1x")
print(
    f"        Doel bereikt      : "
    f"{'JA' if short_reached else 'NEE'}"
)

print_report_status(
    short_report_file,
    short_reached,
    short_target_total,
    "Paper-short",
)

if not long_dry_run:
    print(
        "[FOUT]  Longteststop is niet actief; "
        "dry-run moet aanstaan"
    )
    raise SystemExit(1)
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
}


show_backup_status() {
    if ! python3 - "$BACKUP_DIR" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

backup_root = Path(sys.argv[1])

if not backup_root.is_dir():
    print("[FOUT]  Back-upmap ontbreekt")
    print(f"        Map                : {backup_root}")
    raise SystemExit(1)

directories = sorted(
    [
        path
        for path in backup_root.iterdir()
        if (
            path.is_dir()
            and not path.name.startswith(".")
        )
    ],
    key=lambda path: path.name,
)

if not directories:
    print("[FOUT]  Nog geen dagelijkse back-up aanwezig")
    print(f"        Map                : {backup_root}")
    raise SystemExit(1)

latest = directories[-1]
manifest_path = latest / "manifest.json"

if not manifest_path.is_file():
    print("[FOUT]  Manifest ontbreekt in nieuwste back-up")
    print(f"        Map                : {latest}")
    raise SystemExit(1)

try:
    with manifest_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        manifest = json.load(file)
except Exception as exc:
    print(f"[FOUT]  Back-upmanifest lezen mislukt: {exc}")
    raise SystemExit(1)

created_raw = str(
    manifest.get("created_at")
    or ""
)

try:
    created = datetime.fromisoformat(
        created_raw.replace(
            "Z",
            "+00:00",
        )
    )

    if created.tzinfo is None:
        created = created.replace(
            tzinfo=timezone.utc,
        )

    age_hours = max(
        0.0,
        (
            datetime.now(
                timezone.utc
            )
            - created.astimezone(
                timezone.utc
            )
        ).total_seconds()
        / 3600.0,
    )
except ValueError:
    print(
        f"[FOUT]  Ongeldige back-uptijd: "
        f"{created_raw or '-'}"
    )
    raise SystemExit(1)

if manifest.get("status") != "complete":
    print("[FOUT]  Nieuwste back-up is niet compleet")
    print(
        f"        Status             : "
        f"{manifest.get('status') or '-'}"
    )
    raise SystemExit(1)

required_missing = (
    manifest.get("required_missing")
    or []
)

if required_missing:
    print("[FOUT]  Vereiste bestanden ontbreken in de back-up")
    for item in required_missing:
        print(f"          - {item}")
    raise SystemExit(1)

copied_files = (
    manifest.get("copied_files")
    or []
)

if not copied_files:
    print("[FOUT]  Back-up bevat geen gekopieerde bestanden")
    raise SystemExit(1)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        while True:
            chunk = file.read(
                1024 * 1024
            )

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


integrity_errors = []

for item in copied_files:
    name = str(
        item.get("name")
        or ""
    )

    expected_hash = str(
        item.get("sha256")
        or ""
    )

    expected_size = int(
        item.get("size_bytes")
        or 0
    )

    path = latest / name

    if not path.is_file():
        integrity_errors.append(
            f"{name}: ontbreekt"
        )
        continue

    actual_size = path.stat().st_size

    if actual_size != expected_size:
        integrity_errors.append(
            f"{name}: grootte {actual_size}, verwacht {expected_size}"
        )
        continue

    if expected_hash and sha256_file(path) != expected_hash:
        integrity_errors.append(
            f"{name}: SHA256 komt niet overeen"
        )

if integrity_errors:
    print("[FOUT]  Integriteitscontrole back-up mislukt")
    for item in integrity_errors:
        print(f"          - {item}")
    raise SystemExit(1)

names = {
    str(item.get("name") or "")
    for item in copied_files
}

core_names = {
    "config.yaml",
    "diamond_state.json",
    "diamond_transactions.csv",
    "diamond_control.json",
    "diamond_agent_state.json",
    "diamond_test_baseline.json",
    "diamond_short_test_baseline.json",
}

missing_core = sorted(
    core_names - names
)

if missing_core:
    print("[FOUT]  Kernbestanden ontbreken in de back-up")
    for item in missing_core:
        print(f"          - {item}")
    raise SystemExit(1)

print("[OK]    Dagelijkse back-up aanwezig en gecontroleerd")
print(f"        Map                : {latest}")
print(f"        Gemaakt op         : {created_raw}")
print(f"        Leeftijd           : {age_hours:.1f} uur")
print(
    f"        Bestanden          : "
    f"{len(copied_files)}"
)
print(
    f"        Totale grootte     : "
    f"{int(manifest.get('total_bytes') or 0)} bytes"
)
print(
    f"        Bewaartermijn      : "
    f"{int(manifest.get('retention_days') or 0)} dagen"
)
print("        Integriteit        : OK")
print(f"        Back-ups aanwezig  : {len(directories)}")

if age_hours > 36.0:
    print(
        "[FOUT]  Nieuwste back-up is ouder dan 36 uur"
    )
    raise SystemExit(1)
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
}



show_short_diagnose() {
    local diagnose_script="$PROJECT_DIR/short_diagnose.py"

    if [ ! -f "$diagnose_script" ]; then
        echo "[FOUT]  short_diagnose.py ontbreekt"
        echo "        Bestand: $diagnose_script"
        ERRORS=$((ERRORS + 1))
        return
    fi

    echo "[INFO]  Veilige paper-shortdiagnose wordt uitgevoerd"
    echo "        Alleen lezen: geen orders en geen bestanden gewijzigd"
    echo

    if (
        cd "$PROJECT_DIR"
        python3 short_diagnose.py --compact
    ); then
        echo
        echo "[OK]    Paper-shortdiagnose succesvol afgerond"
    else
        local exit_code=$?
        echo
        echo "[FOUT]  Paper-shortdiagnose mislukt (exitcode $exit_code)"
        ERRORS=$((ERRORS + 1))
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
    requirements.txt \
    start.sh \
    healthcheck.sh
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

check_file "$STATE_FILE" "Bot-state" "true"
show_json_summary "$STATE_FILE" "bot"

echo
echo "4. VEILIGHEIDSCONTROLE"
echo "------------------------------------------------------------"

check_file "$CONTROL_FILE" "Controlebestand" "true"
show_json_summary "$CONTROL_FILE" "control"

echo
echo "5. AGENT"
echo "------------------------------------------------------------"

check_file "$AGENT_STATE_FILE" "Agent-state"
show_json_summary "$AGENT_STATE_FILE" "agent"

echo
echo "6. DIAGNOSE"
echo "------------------------------------------------------------"

check_file "$DIAG_STATS_FILE" "Diagnosestatistieken"
show_json_summary "$DIAG_STATS_FILE" "diagnose"

echo
echo "7. SUPERVISOR"
echo "------------------------------------------------------------"

check_file "$SUPERVISOR_FILE" "Supervisorrapport"
show_json_summary "$SUPERVISOR_FILE" "supervisor"

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

show_test_progress

echo
echo "10. PAPER-SHORTDIAGNOSE"
echo "------------------------------------------------------------"

show_short_diagnose

echo
echo "11. DAGELIJKSE BACK-UP"
echo "------------------------------------------------------------"

show_backup_status

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
