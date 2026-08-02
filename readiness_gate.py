#!/usr/bin/env python3
# Veilige upgrade van Diamond Readiness Gate v1.2 naar v1.3.
#
# Wijziging:
# - "BEZIG" is gezond alleen als active_task exact dezelfde taak is.
# - Losse/stale "BEZIG" blijft fout.
# - "OK" blijft alleen geldig bij exitcode 0 en recente voltooiing.
# - Self-test wordt uitgebreid met deze gevallen.
#
# Geen strategie-, config-, bot-state-, scanner- of transactiebestanden
# worden gewijzigd.

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PROJECT_DIR = Path("/opt/render/project/src")
TARGET = PROJECT_DIR / "readiness_gate.py"
BACKUP = PROJECT_DIR / "readiness_gate_v1_2_backup.py"

OLD_TITLE = "Diamond Readiness Gate v1.2"
NEW_TITLE = "Diamond Readiness Gate v1.3"
OLD_VERSION = 'VERSION = "1.2"'
NEW_VERSION = 'VERSION = "1.3"'


def fail(message: str) -> None:
    raise SystemExit(f"UPGRADE AFGEBROKEN: {message}")


if not TARGET.is_file():
    fail(f"{TARGET} ontbreekt")

text = TARGET.read_text(encoding="utf-8")

if (
    NEW_TITLE in text
    and NEW_VERSION in text
    and "def periodic_task_health(" in text
):
    print("READINESS_GATE_V1_3_ALREADY_INSTALLED")
    raise SystemExit(0)

for marker, label in (
    (OLD_TITLE, "titel v1.2"),
    (OLD_VERSION, "VERSION 1.2"),
    ("def build_report() -> Dict[str, Any]:", "build_report"),
    ("periodic_task_ages: Dict[", "periodieke taakcontrole"),
    ("freshness_items = (", "freshness"),
    ("READINESS_GATE_SELF_TEST_OK", "self-test"),
):
    if marker not in text:
        fail(f"verwachte marker ontbreekt: {label}")

if not BACKUP.exists():
    shutil.copy2(TARGET, BACKUP)
    print(f"Back-up gemaakt: {BACKUP}")
else:
    print(f"Back-up bestaat al: {BACKUP}")


helper = r'''
def periodic_task_health(
    task_name: str,
    task_data: Dict[str, Any],
    active_task: Any,
    maximum_age_minutes: float,
    state_error: Optional[str] = None,
) -> Tuple[bool, Optional[float], str]:
    if state_error is not None:
        return False, None, state_error

    status = str(
        task_data.get("last_status") or ""
    ).strip().upper()

    active_name = str(
        active_task or ""
    ).strip()

    is_active = (
        active_name == task_name
    )

    raw_exit = task_data.get(
        "last_exit_code"
    )

    exit_code: Optional[int]

    if raw_exit is None:
        exit_code = None
    else:
        exit_code = to_int(
            raw_exit,
            -1,
        )

    task_age = age_minutes(
        task_data.get(
            "last_completed_at"
        )
    )

    if status == "BEZIG":
        previous_exit_ok = (
            exit_code is None
            or exit_code == 0
        )

        recent_previous_completion = (
            task_age is None
            or task_age <= maximum_age_minutes
        )

        passed = (
            is_active
            and previous_exit_ok
            and recent_previous_completion
        )

        detail = (
            f"status=BEZIG; active_task={active_name or '-'}; "
            f"vorige exit={exit_code}; "
            + (
                f"vorige voltooiing {task_age:.1f} minuten geleden; "
                f"maximum {maximum_age_minutes:.1f}"
                if task_age is not None
                else "nog geen vorige voltooiing"
            )
        )

        return passed, task_age, detail

    if status == "OK":
        passed = (
            not is_active
            and exit_code == 0
            and task_age is not None
            and task_age <= maximum_age_minutes
        )

        detail = (
            f"status=OK; active_task={active_name or '-'}; "
            f"exit={exit_code}; "
            + (
                f"leeftijd {task_age:.1f} minuten; "
                f"maximum {maximum_age_minutes:.1f}"
                if task_age is not None
                else "geen geldige voltooiingstijd"
            )
        )

        return passed, task_age, detail

    detail = (
        f"status={status or '-'}; active_task={active_name or '-'}; "
        f"exit={exit_code}; "
        + (
            f"leeftijd {task_age:.1f} minuten; "
            f"maximum {maximum_age_minutes:.1f}"
            if task_age is not None
            else "geen geldige voltooiingstijd"
        )
    )

    return False, task_age, detail


'''

text = text.replace(
    "def build_report() -> Dict[str, Any]:",
    helper + "def build_report() -> Dict[str, Any]:",
    1,
)

start_marker = '''    periodic_task_ages: Dict[
        str,
        Optional[float]
    ] = {}
'''

end_marker = '''    freshness_items = (
'''

start = text.find(start_marker)
end = text.find(end_marker, start)

if start == -1 or end == -1 or end <= start:
    fail("kon periodieke taakcontrole niet veilig afbakenen")

new_task_block = r'''    periodic_task_ages: Dict[
        str,
        Optional[float]
    ] = {}

    active_periodic_task = (
        periodic_analysis.get(
            "active_task"
        )
    )

    for task_name, task_data, maximum in (
        (
            "diagnose",
            periodic_diagnose,
            MAX_DIAGNOSE_AGE_MINUTES,
        ),
        (
            "scanner",
            periodic_scanner,
            MAX_SCANNER_AGE_MINUTES,
        ),
    ):
        task_ok, task_age, task_detail = (
            periodic_task_health(
                task_name,
                task_data,
                active_periodic_task,
                maximum,
                periodic_analysis_error,
            )
        )

        periodic_task_ages[
            task_name
        ] = task_age

        add_check(
            checks,
            f"periodic_{task_name}_ok_recent",
            "actualiteit",
            "critical",
            task_ok,
            task_detail,
        )

'''

text = text[:start] + new_task_block + text[end:]

text = text.replace(OLD_TITLE, NEW_TITLE, 1)
text = text.replace(OLD_VERSION, NEW_VERSION, 1)

selftest_marker = '''    print(
        "READINESS_GATE_SELF_TEST_OK"
    )
'''

if selftest_marker not in text:
    fail("self-test printmarker niet gevonden")

extra_selftests = r'''    healthy_ok, _, _ = periodic_task_health(
        "scanner",
        {
            "last_status": "OK",
            "last_exit_code": 0,
            "last_completed_at": now_utc().isoformat(),
        },
        None,
        35.0,
        None,
    )

    if not healthy_ok:
        raise RuntimeError(
            "Self-test mislukt: recente OK-taak moet gezond zijn"
        )

    healthy_busy, _, _ = periodic_task_health(
        "scanner",
        {
            "last_status": "BEZIG",
            "last_exit_code": 0,
            "last_completed_at": now_utc().isoformat(),
        },
        "scanner",
        35.0,
        None,
    )

    if not healthy_busy:
        raise RuntimeError(
            "Self-test mislukt: actieve BEZIG-taak moet gezond zijn"
        )

    stale_busy, _, _ = periodic_task_health(
        "scanner",
        {
            "last_status": "BEZIG",
            "last_exit_code": 0,
            "last_completed_at": now_utc().isoformat(),
        },
        None,
        35.0,
        None,
    )

    if stale_busy:
        raise RuntimeError(
            "Self-test mislukt: losse BEZIG-status mag niet gezond zijn"
        )

    wrong_busy, _, _ = periodic_task_health(
        "scanner",
        {
            "last_status": "BEZIG",
            "last_exit_code": 0,
            "last_completed_at": now_utc().isoformat(),
        },
        "diagnose",
        35.0,
        None,
    )

    if wrong_busy:
        raise RuntimeError(
            "Self-test mislukt: BEZIG moet exact bij active_task horen"
        )

'''

text = text.replace(
    selftest_marker,
    extra_selftests + selftest_marker,
    1,
)

with tempfile.NamedTemporaryFile(
    mode="w",
    encoding="utf-8",
    dir=str(PROJECT_DIR),
    prefix=".readiness_gate_v1_3_",
    suffix=".py",
    delete=False,
) as tmp:
    tmp.write(text)
    tmp_path = Path(tmp.name)

try:
    compile_check = subprocess.run(
        ["python3", "-m", "py_compile", str(tmp_path)],
        text=True,
        capture_output=True,
    )

    if compile_check.returncode != 0:
        detail = (
            compile_check.stderr.strip()
            or compile_check.stdout.strip()
            or "onbekende Python-syntaxfout"
        )
        fail("v1.3 compileert niet: " + detail)

    os.replace(tmp_path, TARGET)

finally:
    if tmp_path.exists():
        tmp_path.unlink()

final = TARGET.read_text(encoding="utf-8")

required = (
    NEW_TITLE,
    NEW_VERSION,
    "def periodic_task_health(",
    "active_periodic_task",
    "actieve BEZIG-taak moet gezond zijn",
    "losse BEZIG-status mag niet gezond zijn",
)

missing = [
    marker
    for marker in required
    if marker not in final
]

if missing:
    fail("eindcontrole mist: " + ", ".join(missing))

print()
print("READINESS_GATE_V1_3_UPGRADE_OK")
print(f"Actief bestand : {TARGET}")
print(f"Back-up        : {BACKUP}")
print("Python-syntax  : OK")
print("Wijziging      : BEZIG alleen OK wanneer active_task exact overeenkomt")
print("Strategie/config/bot-state/transacties: ONGEWIJZIGD")
