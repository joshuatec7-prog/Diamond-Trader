#!/usr/bin/env python3
from pathlib import Path
import shutil

target = Path("/opt/render/project/src/scanner_healthcheck.sh")
backup = Path("/opt/render/project/src/scanner_healthcheck_v1_0_backup.sh")

if not target.is_file():
    raise SystemExit("STOP: scanner_healthcheck.sh ontbreekt")

text = target.read_text(encoding="utf-8")

if "# Diamond Market Scanner Healthcheck v1.1" in text:
    print("SCANNER_HEALTHCHECK_V1_1_ALREADY_INSTALLED")
    raise SystemExit(0)

if "# Diamond Market Scanner Healthcheck v1.0" not in text:
    raise SystemExit("STOP: verwacht scanner_healthcheck.sh v1.0")

start_marker = 'echo "1. PROCES"\n'
end_marker = 'echo "2. PROGRAMMABESTAND"\n'

if start_marker not in text or end_marker not in text:
    raise SystemExit("STOP: verwachte sectiemarkers ontbreken")

if not backup.exists():
    shutil.copy2(target, backup)
    print(f"Back-up gemaakt: {backup}")

start = text.index(start_marker)
end = text.index(end_marker)

new_section = r'''echo "1. AANSTURING"
echo "------------------------------------------------------------------------"

PERIODIC_STATE_FILE="$DATA_DIR/diamond_periodic_analysis_state.json"

RUNNER_PROCESS=$(
    pgrep -af 'python3[[:space:]]+periodic_analysis_runner\.py([[:space:]]|$)' 2>/dev/null || true
)

if [ -n "$RUNNER_PROCESS" ]; then
    echo "[OK]    Periodieke analyse-runner draait"
    echo "$RUNNER_PROCESS" | sed 's/^/        /'
else
    echo "[FOUT]  Periodieke analyse-runner draait NIET"
    ERRORS=$((ERRORS + 1))
fi

if [ -f "$PERIODIC_STATE_FILE" ]; then
    if ! python3 - "$PERIODIC_STATE_FILE" "$NOW_EPOCH" <<'PYRUNNER'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
now = datetime.fromtimestamp(int(sys.argv[2]), tz=timezone.utc)
state = json.loads(path.read_text(encoding="utf-8"))
scanner = (state.get("tasks") or {}).get("scanner") or {}
active = state.get("active_task")
status = str(scanner.get("last_status") or "-")
exit_code = scanner.get("last_exit_code")
runs = int(scanner.get("run_count", 0) or 0)
command = [str(x) for x in (scanner.get("command") or [])]

def dt(value):
    if not value:
        return None
    x = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if x.tzinfo is None:
        x = x.replace(tzinfo=timezone.utc)
    return x.astimezone(timezone.utc)

print(f"        Runner-versie      : {state.get('version') or '-'}")
print(f"        Modus              : {state.get('mode') or '-'}")
print(f"        Sequentieel        : {'JA' if state.get('sequential') is True else 'NEE'}")
print(f"        Actieve taak       : {active or 'geen'}")
print(f"        Scanner runs       : {runs}")
print(f"        Scanner status     : {status}")
print(f"        Scanner exitcode   : {exit_code if exit_code is not None else '-'}")

problems = []

if state.get("mode") != "SEQUENTIAL_PERIODIC_ANALYSIS":
    problems.append("onverwachte runner-modus")
if state.get("sequential") is not True:
    problems.append("runner is niet sequentieel")
if not any(x.endswith("market_scanner.py") for x in command):
    problems.append("scanner-command ontbreekt")
if "--top" not in command or "20" not in command:
    problems.append("scanner-command gebruikt niet --top 20")
if runs < 1:
    problems.append("scanner heeft nog geen runs")

if active == "scanner":
    if status != "BEZIG":
        problems.append(f"scanner actief maar status={status}")
    started = dt(scanner.get("last_started_at"))
    if started and (now - started).total_seconds() / 60 > 10:
        problems.append("actieve scanner-run duurt langer dan 10 minuten")
else:
    if status != "OK":
        problems.append(f"laatste scannerstatus={status}, verwacht OK")
    if exit_code != 0:
        problems.append(f"laatste scanner-exitcode={exit_code}, verwacht 0")
    completed = dt(scanner.get("last_completed_at"))
    if not completed:
        problems.append("geen scanner-voltooiingstijd")
    elif (now - completed).total_seconds() / 60 > 35:
        problems.append("laatste scanner-run ouder dan 35 minuten")

if problems:
    for p in problems:
        print(f"[FOUT]  {p}")
    raise SystemExit(1)

if active == "scanner":
    print("[OK]    Market Scanner wordt nu sequentieel uitgevoerd")
else:
    print("[OK]    Market Scanner wordt periodiek en sequentieel uitgevoerd")
PYRUNNER
    then
        ERRORS=$((ERRORS + 1))
    fi
else
    echo "[FOUT]  Periodieke analyse-state ontbreekt"
    ERRORS=$((ERRORS + 1))
fi

echo
'''

text = text[:start] + new_section + text[end:]
text = text.replace(
    "# Diamond Market Scanner Healthcheck v1.0",
    "# Diamond Market Scanner Healthcheck v1.1",
    1,
)

target.write_text(text, encoding="utf-8")
target.chmod(0o755)

print("SCANNER_HEALTHCHECK_V1_1_INSTALLED")
