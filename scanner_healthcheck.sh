#!/usr/bin/env bash

# Diamond Market Scanner Healthcheck v1.0
# Alleen lezen: dit script wijzigt geen bot-, scanner- of transactiebestanden.

set -u

DATA_DIR="/var/data"
PROJECT_DIR="/opt/render/project/src"

SCANNER_FILE="$PROJECT_DIR/market_scanner.py"
REPORT_FILE="$DATA_DIR/diamond_market_signals.json"
STATE_FILE="$DATA_DIR/diamond_market_scanner_state.json"
SIGNALS_FILE="$DATA_DIR/diamond_market_signals.csv"
SHADOW_TRADES_FILE="$DATA_DIR/diamond_shadow_trades.csv"
RUNNER_LOG="$DATA_DIR/diamond_market_scanner_runner.log"
SCANNER_LOG="$DATA_DIR/diamond_market_scanner.log"

NOW_EPOCH=$(date +%s)
ERRORS=0

echo
echo "========================================================================"
echo " DIAMOND MARKET SCANNER CONTROLE"
echo " $(date)"
echo "========================================================================"
echo

echo "1. PROCES"
echo "------------------------------------------------------------------------"

PROCESS_RESULT=$(
    pgrep -af \
        'python3[[:space:]]+market_scanner\.py[[:space:]]+--loop[[:space:]]+--top[[:space:]]+20([[:space:]]|$)' \
        2>/dev/null \
    || true
)

if [ -n "$PROCESS_RESULT" ]; then
    echo "[OK]    Diamond Market Scanner draait"
    echo "$PROCESS_RESULT" | sed 's/^/        /'
else
    echo "[FOUT]  Diamond Market Scanner draait NIET"
    ERRORS=$((ERRORS + 1))
fi

echo
echo "2. PROGRAMMABESTAND"
echo "------------------------------------------------------------------------"

if [ -f "$SCANNER_FILE" ]; then
    echo "[OK]    market_scanner.py aanwezig"

    if grep -qE '^VERSION = "1\.1"$' "$SCANNER_FILE"; then
        echo "        Versie             : 1.1"
    else
        echo "[FOUT]  Versie 1.1 niet herkend"
        ERRORS=$((ERRORS + 1))
    fi

    if python3 -m py_compile "$SCANNER_FILE" 2>/dev/null; then
        echo "        Pythoncontrole     : OK"
    else
        echo "[FOUT]  Pythoncontrole mislukt"
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "[FOUT]  market_scanner.py ontbreekt"
    ERRORS=$((ERRORS + 1))
fi

echo
echo "3. SCANNERSTATUS"
echo "------------------------------------------------------------------------"

if [ -f "$REPORT_FILE" ] && [ -f "$STATE_FILE" ]; then
    if ! python3 - "$REPORT_FILE" "$STATE_FILE" "$NOW_EPOCH" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

report_file = Path(sys.argv[1])
state_file = Path(sys.argv[2])
now_epoch = int(sys.argv[3])

report = json.loads(report_file.read_text(encoding="utf-8"))
state = json.loads(state_file.read_text(encoding="utf-8"))

generated_at = report.get("generated_at")

try:
    generated = datetime.fromisoformat(
        str(generated_at).replace("Z", "+00:00")
    )
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
except Exception:
    print("[FOUT]  Ongeldige scantijd")
    raise SystemExit(1)

age_minutes = max(
    0.0,
    (
        datetime.fromtimestamp(now_epoch, tz=timezone.utc)
        - generated.astimezone(timezone.utc)
    ).total_seconds()
    / 60.0,
)

mode = report.get("mode") or "-"
safety = report.get("safety") or {}
shadow = report.get("shadow") or {}
totals = shadow.get("totals") or state.get("shadow_totals") or {}
signals = report.get("signals") or []
errors = report.get("errors") or []

positions_raw = shadow.get("positions")
if isinstance(positions_raw, list):
    open_positions = positions_raw
else:
    open_positions = list((state.get("open_positions") or {}).values())

print("[OK]    Scannerstatus leesbaar")
print(f"        Versie             : {report.get('version') or state.get('version') or '-'}")
print(f"        Modus              : {mode}")
print(f"        Laatste scan       : {generated_at}")
print(f"        Leeftijd scan      : {age_minutes:.1f} minuten")
print(f"        Scans totaal       : {int(state.get('scan_count', 0) or 0)}")
print(f"        Markten onderzocht : {int(report.get('analysed_count', 0) or 0)}")
print(f"        Signalen ronde     : {len(signals)}")
print(f"        Signalen totaal    : {int(state.get('total_unique_signals', 0) or 0)}")
print(f"        Open schaduw       : {len(open_positions)}")
print(f"        Gesloten schaduw   : {int(totals.get('closed', 0) or 0)}")
print(
    f"        Winst/verlies/neut.: "
    f"{int(totals.get('wins', 0) or 0)}/"
    f"{int(totals.get('losses', 0) or 0)}/"
    f"{int(totals.get('neutral', 0) or 0)}"
)
print(f"        Nettoresultaat     : €{float(totals.get('net_pnl_eur', 0) or 0):+.4f}")
print(f"        Totale kosten      : €{float(totals.get('total_fees_eur', 0) or 0):.4f}")
print(f"        Analysefouten      : {len(errors)}")
print(f"        Echte orders       : {'MOGELIJK' if safety.get('orders_possible') else 'ONMOGELIJK'}")

if signals:
    print("        Beste signalen:")

    for signal in signals[:5]:
        economics = signal.get("economics") or {}
        rejections = signal.get("shadow_rejection_reasons") or []

        if signal.get("shadow_eligible"):
            status = "SCHADUWTRADE"
        elif rejections:
            status = f"AFGEWEZEN: {rejections[0]}"
        else:
            status = "AFGEWEZEN"

        print(
            f"          - {signal.get('symbol', '-')}: "
            f"{signal.get('side', '-')} "
            f"{signal.get('strategy', '-')} | "
            f"score={float(signal.get('score', 0) or 0):.1f} | "
            f"RR={float(economics.get('reward_risk', 0) or 0):.2f} | "
            f"{status}"
        )

if mode != "VIRTUAL_SHADOW_TRADING":
    print(f"[FOUT]  Onverwachte modus: {mode}")
    raise SystemExit(1)

if safety.get("orders_possible") is not False:
    print("[FOUT]  Veiligheidsstatus meldt dat orders mogelijk zijn")
    raise SystemExit(1)

if age_minutes > 35.0:
    print("[FOUT]  Laatste scan is ouder dan 35 minuten")
    raise SystemExit(1)

print("[OK]    Scanner draait actueel en veilig")
PY
    then
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "[FOUT]  Scannerstatusbestanden ontbreken"
    [ -f "$REPORT_FILE" ] || echo "        Ontbreekt          : $REPORT_FILE"
    [ -f "$STATE_FILE" ] || echo "        Ontbreekt          : $STATE_FILE"
    ERRORS=$((ERRORS + 1))
fi

echo
echo "4. RESULTAATBESTANDEN"
echo "------------------------------------------------------------------------"

for item in \
    "$SIGNALS_FILE|Signalenhistorie|false" \
    "$SHADOW_TRADES_FILE|Schaduwtrades|false" \
    "$RUNNER_LOG|Runnerlog|true" \
    "$SCANNER_LOG|Scannerlog|true"
do
    IFS='|' read -r path label required <<< "$item"

    if [ -f "$path" ]; then
        size=$(stat -c %s "$path" 2>/dev/null || echo 0)
        modified=$(stat -c %Y "$path" 2>/dev/null || echo 0)
        age_minutes=$(( (NOW_EPOCH - modified) / 60 ))

        echo "[OK]    $label aanwezig"
        echo "        Bestand            : $path"
        echo "        Grootte            : $size bytes"
        echo "        Laatst gewijzigd   : $age_minutes minuten geleden"
    else
        if [ "$required" = "true" ]; then
            echo "[FOUT]  $label ontbreekt"
            ERRORS=$((ERRORS + 1))
        else
            echo "[INFO]  $label nog niet aanwezig"
        fi
        echo "        Bestand            : $path"
    fi
done

if [ -f "$SHADOW_TRADES_FILE" ]; then
    echo
    echo "        Laatste drie schaduwtrades:"
    tail -n 3 "$SHADOW_TRADES_FILE" | sed 's/^/        /'
fi

echo
echo "5. EINDCONTROLE"
echo "------------------------------------------------------------------------"

if [ "$ERRORS" -eq 0 ]; then
    echo "[OK]    Diamond Market Scanner is gezond en veilig."
    EXIT_CODE=0
else
    echo "[FOUT]  Er zijn $ERRORS scannerproblemen gevonden."
    EXIT_CODE=1
fi

echo
echo "========================================================================"
echo " CONTROLE AFGEROND"
echo "========================================================================"
echo

exit "$EXIT_CODE"
