#!/usr/bin/env python3
from pathlib import Path
import os
import shutil
import py_compile
import tempfile

path = Path("/opt/render/project/src/readiness_gate.py")
backup = Path("/opt/render/project/src/readiness_gate_v1_2_backup.py")

if not path.is_file():
    raise SystemExit("STOP: readiness_gate.py ontbreekt")

text = path.read_text(encoding="utf-8")

if 'VERSION = "1.3"' in text:
    print("READINESS_GATE_V1_3_ALREADY_INSTALLED")
    raise SystemExit(0)

if 'VERSION = "1.2"' not in text:
    raise SystemExit("STOP: readiness_gate.py is niet v1.2")

if "Diamond Readiness Gate v1.2" not in text:
    raise SystemExit("STOP: titel v1.2 niet gevonden")

if not backup.exists():
    shutil.copy2(path, backup)
    print("Back-up gemaakt:", backup)

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
    raise SystemExit(
        "STOP: bestaande periodieke taakcontrole niet gevonden"
    )

new_block = r'''    periodic_task_ages: Dict[
        str,
        Optional[float]
    ] = {}

    active_periodic_task = str(
        periodic_analysis.get(
            "active_task"
        )
        or ""
    ).strip()

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
        status = str(
            task_data.get(
                "last_status"
            )
            or ""
        ).strip().upper()

        raw_exit = task_data.get(
            "last_exit_code"
        )

        exit_code = (
            None
            if raw_exit is None
            else to_int(
                raw_exit,
                -1,
            )
        )

        completed_age = age_minutes(
            task_data.get(
                "last_completed_at"
            )
        )

        started_age = age_minutes(
            task_data.get(
                "last_started_at"
            )
        )

        if status == "BEZIG":
            task_age = started_age

            task_ok = (
                periodic_analysis_error is None
                and active_periodic_task == task_name
                and (
                    exit_code is None
                    or exit_code == 0
                )
                and started_age is not None
                and started_age <= maximum
            )

            detail = (
                f"status=BEZIG; active_task="
                f"{active_periodic_task or '-'}; "
                f"vorige exit={exit_code}; "
                + (
                    f"actief sinds {started_age:.1f} minuten; "
                    f"maximum {maximum:.1f}"
                    if started_age is not None
                    else "geen geldige starttijd"
                )
            )

        else:
            task_age = completed_age

            task_ok = (
                periodic_analysis_error is None
                and status == "OK"
                and active_periodic_task != task_name
                and exit_code == 0
                and completed_age is not None
                and completed_age <= maximum
            )

            detail = (
                f"status={status or '-'}; "
                f"active_task={active_periodic_task or '-'}; "
                f"exit={exit_code}; "
                + (
                    f"leeftijd {completed_age:.1f} minuten; "
                    f"maximum {maximum:.1f}"
                    if completed_age is not None
                    else "geen geldige voltooiingstijd"
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
            periodic_analysis_error or detail,
        )

'''

text = text[:start] + new_block + text[end:]

text = text.replace(
    "Diamond Readiness Gate v1.2",
    "Diamond Readiness Gate v1.3",
    1,
)

text = text.replace(
    'VERSION = "1.2"',
    'VERSION = "1.3"',
    1,
)

with tempfile.NamedTemporaryFile(
    mode="w",
    encoding="utf-8",
    dir=str(path.parent),
    prefix=".readiness_gate_v1_3_",
    suffix=".py",
    delete=False,
) as tmp:
    tmp.write(text)
    tmp_path = Path(tmp.name)

try:
    py_compile.compile(
        str(tmp_path),
        doraise=True,
    )

    os.replace(
        tmp_path,
        path,
    )
finally:
    if tmp_path.exists():
        tmp_path.unlink()

print("READINESS_GATE_V1_3_PATCH_OK")
print("Actief:", path)
print("Back-up:", backup)
print("Alleen periodieke BEZIG/OK-beoordeling aangepast.")
