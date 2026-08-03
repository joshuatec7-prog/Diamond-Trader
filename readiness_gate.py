#!/usr/bin/env python3
"""
Diamond Readiness Gate v1.3

Centrale, uitsluitend lezende gereedheidscontrole voor Diamond Trader.

De gate:
- controleert processen, bestanden, actualiteit en back-ups;
- controleert dry-run-, paper-short- en scannerveiligheid;
- vergelijkt long-, short- en schaduwtestvoortgang;
- blokkeert live-gereedheid zonder eindtest en handmatige goedkeuring;
- schrijft uitsluitend eigen JSON- en tekstrapporten;
- wijzigt nooit bot-, scanner-, test-, controle- of configuratiebestanden;
- kan geen orders plaatsen en maakt geen exchangeverbinding.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


VERSION = "1.3"
MODE = "READ_ONLY_READINESS_GATE"

DATA_DIR = Path(
    os.getenv(
        "DIAMOND_DATA_DIR",
        "/var/data",
    ).strip()
)

PROJECT_DIR = Path(
    os.getenv(
        "DIAMOND_PROJECT_DIR",
        "/opt/render/project/src",
    ).strip()
)

CFG_FILE = Path(
    os.getenv(
        "CFG_FILE",
        str(PROJECT_DIR / "config.yaml"),
    ).strip()
)

STATE_FILE = Path(
    os.getenv(
        "STATE_FILE",
        str(DATA_DIR / "diamond_state.json"),
    ).strip()
)

CONTROL_FILE = Path(
    os.getenv(
        "CONTROL_FILE",
        str(DATA_DIR / "diamond_control.json"),
    ).strip()
)

AGENT_STATE_FILE = Path(
    os.getenv(
        "AGENT_STATE_FILE",
        str(DATA_DIR / "diamond_agent_state.json"),
    ).strip()
)

LONG_BASELINE_FILE = Path(
    os.getenv(
        "TEST_BASELINE_FILE",
        str(DATA_DIR / "diamond_test_baseline.json"),
    ).strip()
)

LONG_REPORT_FILE = Path(
    os.getenv(
        "TEST_REPORT_FILE",
        str(DATA_DIR / "diamond_test_report.json"),
    ).strip()
)

SHORT_BASELINE_FILE = Path(
    os.getenv(
        "SHORT_TEST_BASELINE_FILE",
        str(DATA_DIR / "diamond_short_test_baseline.json"),
    ).strip()
)

SHORT_REPORT_FILE = Path(
    os.getenv(
        "SHORT_TEST_REPORT_FILE",
        str(DATA_DIR / "diamond_short_test_report.json"),
    ).strip()
)

DIAG_STATS_FILE = Path(
    os.getenv(
        "DIAG_STATS_FILE",
        str(DATA_DIR / "diamond_diagnose_stats.json"),
    ).strip()
)

SUPERVISOR_STATE_FILE = Path(
    os.getenv(
        "SUPERVISOR_STATE_FILE",
        str(DATA_DIR / "diamond_supervisor_state.json"),
    ).strip()
)

SCANNER_STATE_FILE = Path(
    os.getenv(
        "MARKET_SCANNER_STATE_FILE",
        str(DATA_DIR / "diamond_market_scanner_state.json"),
    ).strip()
)

SCANNER_REPORT_FILE = Path(
    os.getenv(
        "MARKET_SCANNER_REPORT_FILE",
        str(DATA_DIR / "diamond_market_signals.json"),
    ).strip()
)

PERIODIC_ANALYSIS_STATE_FILE = Path(
    os.getenv(
        "PERIODIC_ANALYSIS_STATE_FILE",
        str(DATA_DIR / "diamond_periodic_analysis_state.json"),
    ).strip()
)

STRATEGY_LAB_FILE = Path(
    os.getenv(
        "STRATEGY_LAB_JSON_FILE",
        str(DATA_DIR / "diamond_strategy_lab.json"),
    ).strip()
)

SHADOW_MILESTONE_20_FILE = Path(
    os.getenv(
        "SHADOW_MILESTONE_20_FILE",
        str(DATA_DIR / "diamond_market_shadow_milestone_20.json"),
    ).strip()
)

FINAL_VALIDATION_FILE = Path(
    os.getenv(
        "FINAL_VALIDATION_FILE",
        str(DATA_DIR / "diamond_final_validation.json"),
    ).strip()
)

LIVE_APPROVAL_FILE = Path(
    os.getenv(
        "LIVE_APPROVAL_FILE",
        str(DATA_DIR / "diamond_live_approval.json"),
    ).strip()
)

REPORT_JSON_FILE = Path(
    os.getenv(
        "READINESS_GATE_JSON_FILE",
        str(DATA_DIR / "diamond_readiness_gate.json"),
    ).strip()
)

REPORT_TEXT_FILE = Path(
    os.getenv(
        "READINESS_GATE_TEXT_FILE",
        str(DATA_DIR / "diamond_readiness_gate.txt"),
    ).strip()
)

TARGET_TRADES = 20
EARLY_READY_TRADES = 10
FINAL_TEST_MIN_DAYS = 7

MAX_AGENT_AGE_MINUTES = 35.0
MAX_DIAGNOSE_AGE_MINUTES = 35.0
MAX_SUPERVISOR_AGE_MINUTES = 35.0
MAX_SCANNER_AGE_MINUTES = 35.0
MAX_STRATEGY_LAB_AGE_MINUTES = 390.0
MAX_BACKUP_AGE_HOURS = 36.0
MIN_FREE_DISK_MB = 100.0

PROCESS_PATTERNS = (
    (
        "Diamond Agent",
        "python3 agent.py",
    ),
    (
        "Diamond Supervisor",
        "python3 supervisor_agent.py",
    ),
    (
        "Diamond Bot",
        "python3 closed_candle_runner.py bot",
    ),
    (
        "Diamond Strategy Lab",
        "python3 strategy_lab.py --loop",
    ),
    (
        "Diamond Periodieke Analyse",
        "python3 periodic_analysis_runner.py",
    ),
)


def now_utc() -> datetime:
    return datetime.now(
        timezone.utc
    )


def to_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        if value in (
            None,
            "",
        ):
            return default

        result = float(
            value
        )

        return (
            result
            if math.isfinite(
                result
            )
            else default
        )

    except (
        TypeError,
        ValueError,
    ):
        return default


def to_int(
    value: Any,
    default: int = 0,
) -> int:
    try:
        return int(
            float(
                value
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        return default


def to_bool(
    value: Any,
    default: bool = False,
) -> bool:
    if isinstance(
        value,
        bool,
    ):
        return value

    if value is None:
        return default

    text = str(
        value
    ).strip().lower()

    if text in {
        "1",
        "true",
        "yes",
        "ja",
        "on",
    }:
        return True

    if text in {
        "0",
        "false",
        "no",
        "nee",
        "off",
    }:
        return False

    return default


def parse_datetime(
    value: Any,
) -> Optional[datetime]:
    if value in (
        None,
        "",
        0,
    ):
        return None

    if isinstance(
        value,
        (
            int,
            float,
        ),
    ):
        try:
            return datetime.fromtimestamp(
                float(
                    value
                ),
                tz=timezone.utc,
            )

        except Exception:
            return None

    try:
        parsed = datetime.fromisoformat(
            str(
                value
            ).replace(
                "Z",
                "+00:00",
            )
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed.astimezone(
            timezone.utc
        )

    except Exception:
        return None


def age_minutes(
    value: Any,
) -> Optional[float]:
    parsed = parse_datetime(
        value
    )

    if parsed is None:
        return None

    return max(
        0.0,
        (
            now_utc()
            - parsed
        ).total_seconds()
        / 60.0,
    )


def load_json(
    path: Path,
) -> Tuple[Dict[str, Any], Optional[str]]:
    if not path.is_file():
        return (
            {},
            f"bestand ontbreekt: {path}",
        )

    try:
        value = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

    except Exception as exc:
        return (
            {},
            f"JSON lezen mislukt: {type(exc).__name__}: {exc}",
        )

    if not isinstance(
        value,
        dict,
    ):
        return (
            {},
            "JSON bevat geen object",
        )

    return (
        value,
        None,
    )


def load_yaml() -> Tuple[Dict[str, Any], Optional[str]]:
    if not CFG_FILE.is_file():
        return (
            {},
            f"config ontbreekt: {CFG_FILE}",
        )

    try:
        value = yaml.safe_load(
            CFG_FILE.read_text(
                encoding="utf-8"
            )
        ) or {}

    except Exception as exc:
        return (
            {},
            f"YAML lezen mislukt: {type(exc).__name__}: {exc}",
        )

    if not isinstance(
        value,
        dict,
    ):
        return (
            {},
            "config bevat geen object",
        )

    return (
        value,
        None,
    )


def save_json_atomic(
    path: Path,
    data: Dict[str, Any],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(
            path.parent
        ),
        delete=False,
    ) as temporary:
        json.dump(
            data,
            temporary,
            indent=2,
            ensure_ascii=False,
        )

        temporary_name = (
            temporary.name
        )

    os.replace(
        temporary_name,
        path,
    )


def save_text_atomic(
    path: Path,
    text: str,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(
            path.parent
        ),
        delete=False,
        newline="\n",
    ) as temporary:
        temporary.write(
            text
        )

        temporary_name = (
            temporary.name
        )

    os.replace(
        temporary_name,
        path,
    )


def process_command_lines() -> List[str]:
    commands: List[
        str
    ] = []

    proc = Path(
        "/proc"
    )

    if not proc.is_dir():
        return commands

    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue

        try:
            raw = (
                entry
                / "cmdline"
            ).read_bytes()

            if not raw:
                continue

            command = raw.replace(
                b"\x00",
                b" ",
            ).decode(
                "utf-8",
                errors="replace",
            ).strip()

            if command:
                commands.append(
                    " ".join(
                        command.split()
                    )
                )

        except Exception:
            continue

    return commands


def process_running(
    commands: List[str],
    pattern: str,
) -> bool:
    normalized_pattern = " ".join(
        pattern.split()
    )

    return any(
        normalized_pattern
        in command
        for command in commands
    )


def add_check(
    checks: List[Dict[str, Any]],
    name: str,
    category: str,
    level: str,
    passed: bool,
    detail: str,
) -> None:
    checks.append({
        "name": name,
        "category": category,
        "level": level,
        "passed": bool(
            passed
        ),
        "detail": detail,
    })


def test_progress(
    baseline: Dict[str, Any],
    state: Dict[str, Any],
    start_key: str,
    target_total_key: str,
    target_new_key: str,
    current_key: str,
) -> Dict[str, Any]:
    start = max(
        0,
        to_int(
            baseline.get(
                start_key
            ),
            0,
        ),
    )

    current = max(
        0,
        to_int(
            state.get(
                current_key
            ),
            0,
        ),
    )

    target_total = max(
        0,
        to_int(
            baseline.get(
                target_total_key
            ),
            0,
        ),
    )

    target_new = max(
        0,
        to_int(
            baseline.get(
                target_new_key
            ),
            (
                target_total
                - start
            ),
        ),
    )

    if target_new <= 0:
        target_new = TARGET_TRADES

    completed = max(
        0,
        current - start,
    )

    return {
        "start": start,
        "current_total": current,
        "completed": completed,
        "target": target_new,
        "remaining": max(
            0,
            target_new - completed,
        ),
        "complete": (
            completed
            >= target_new
        ),
        "completion_pct": round(
            min(
                100.0,
                (
                    100.0
                    * completed
                    / target_new
                )
                if target_new
                else 0.0,
            ),
            1,
        ),
    }


def next_trade_milestone(
    completed: int,
) -> Tuple[int, int]:
    for milestone in (
        5,
        10,
        20,
    ):
        if completed < milestone:
            return (
                milestone,
                milestone - completed,
            )

    return (
        20,
        0,
    )


def final_validation_status(
    data: Dict[str, Any],
) -> Dict[str, Any]:
    passed = (
        to_bool(
            data.get(
                "passed"
            ),
            False,
        )
        and to_bool(
            data.get(
                "settings_frozen"
            ),
            False,
        )
        and to_bool(
            data.get(
                "dry_run"
            ),
            False,
        )
        and to_float(
            data.get(
                "duration_days"
            ),
            0.0,
        )
        >= FINAL_TEST_MIN_DAYS
    )

    return {
        "available": bool(
            data
        ),
        "passed": passed,
        "duration_days": to_float(
            data.get(
                "duration_days"
            ),
            0.0,
        ),
        "settings_frozen": to_bool(
            data.get(
                "settings_frozen"
            ),
            False,
        ),
        "dry_run": to_bool(
            data.get(
                "dry_run"
            ),
            False,
        ),
        "generated_at": data.get(
            "generated_at"
        ),
    }


def live_approval_status(
    data: Dict[str, Any],
) -> Dict[str, Any]:
    approved = (
        to_bool(
            data.get(
                "approved"
            ),
            False,
        )
        and str(
            data.get(
                "scope"
            )
            or ""
        ).strip().upper()
        == "LIMITED_LIVE_START"
    )

    return {
        "available": bool(
            data
        ),
        "approved": approved,
        "scope": data.get(
            "scope"
        ),
        "approved_at": data.get(
            "approved_at"
        ),
    }


def determine_status(
    critical_failures: List[Dict[str, Any]],
    warnings: List[Dict[str, Any]],
    long_completed: int,
    short_completed: int,
    shadow_completed: int,
    reports_ready: bool,
    final_validation_passed: bool,
    live_approved: bool,
) -> Tuple[str, str]:
    if critical_failures:
        return (
            "NIET GEREED",
            "VEILIGHEID OF SYSTEEM HERSTELLEN",
        )

    minimum = min(
        long_completed,
        short_completed,
        shadow_completed,
    )

    if minimum < EARLY_READY_TRADES:
        return (
            "NIET GEREED",
            "TESTEN LOPEN",
        )

    if minimum < TARGET_TRADES:
        return (
            "BIJNA GEREED",
            "TESTEN AFRONDEN",
        )

    if not reports_ready:
        return (
            "BIJNA GEREED",
            "EINDRAPPORTEN AFRONDEN",
        )

    if warnings:
        return (
            "BIJNA GEREED",
            "WAARSCHUWINGEN OPLOSSEN",
        )

    if not final_validation_passed:
        return (
            "GEREED VOOR EINDTEST",
            "EINDTEST NOG UITVOEREN",
        )

    if not live_approved:
        return (
            "GEREED VOOR EINDTEST",
            "WACHT OP HANDMATIGE LIVE-GOEDKEURING",
        )

    return (
        "GEREED VOOR BEPERKTE LIVE-START",
        "HANDMATIG BEPERKT LIVE STARTEN",
    )


def build_report() -> Dict[str, Any]:
    checks: List[
        Dict[str, Any]
    ] = []

    config, config_error = (
        load_yaml()
    )

    state, state_error = load_json(
        STATE_FILE
    )

    control, control_error = load_json(
        CONTROL_FILE
    )

    agent_state, agent_error = load_json(
        AGENT_STATE_FILE
    )

    long_baseline, long_error = load_json(
        LONG_BASELINE_FILE
    )

    short_baseline, short_error = load_json(
        SHORT_BASELINE_FILE
    )

    diagnose, diagnose_error = load_json(
        DIAG_STATS_FILE
    )

    supervisor, supervisor_error = load_json(
        SUPERVISOR_STATE_FILE
    )

    scanner_state, scanner_state_error = load_json(
        SCANNER_STATE_FILE
    )

    scanner_report, scanner_report_error = load_json(
        SCANNER_REPORT_FILE
    )

    periodic_analysis, periodic_analysis_error = load_json(
        PERIODIC_ANALYSIS_STATE_FILE
    )

    strategy_lab, lab_error = load_json(
        STRATEGY_LAB_FILE
    )

    final_validation, _ = load_json(
        FINAL_VALIDATION_FILE
    )

    live_approval, _ = load_json(
        LIVE_APPROVAL_FILE
    )

    add_check(
        checks,
        "config_readable",
        "veiligheid",
        "critical",
        config_error is None,
        config_error or "config.yaml leesbaar",
    )

    add_check(
        checks,
        "bot_state_readable",
        "systeem",
        "critical",
        state_error is None,
        state_error or "bot-state leesbaar",
    )

    add_check(
        checks,
        "agent_state_readable",
        "systeem",
        "critical",
        agent_error is None,
        agent_error or "agent-state leesbaar",
    )

    add_check(
        checks,
        "periodic_analysis_state_readable",
        "systeem",
        "critical",
        periodic_analysis_error is None,
        periodic_analysis_error or "periodieke analyse-state leesbaar",
    )

    risk = (
        config.get(
            "risk"
        )
        or {}
    )

    trading = (
        config.get(
            "trading"
        )
        or {}
    )

    short_config = (
        config.get(
            "short"
        )
        or {}
    )

    dry_run = to_bool(
        risk.get(
            "dry_run"
        ),
        True,
    )

    paper_only = to_bool(
        short_config.get(
            "paper_only"
        ),
        True,
    )

    short_signals = to_bool(
        trading.get(
            "enable_short_signals"
        ),
        False,
    )

    add_check(
        checks,
        "dry_run_active",
        "veiligheid",
        "critical",
        dry_run,
        (
            "dry-run actief"
            if dry_run
            else "dry-run staat uit"
        ),
    )

    add_check(
        checks,
        "paper_short_only",
        "veiligheid",
        "critical",
        paper_only,
        (
            "paper-short is alleen simulatie"
            if paper_only
            else "paper-short kan live zijn"
        ),
    )

    # De scanner bewaart voortgang in het statebestand, maar de
    # modus en veiligheidsverklaring in diamond_market_signals.json.
    scanner_safety = (
        scanner_report.get(
            "safety"
        )
        or {}
    )

    scanner_mode = str(
        scanner_report.get(
            "mode"
        )
        or ""
    ).strip()

    scanner_orders_impossible = (
        scanner_report_error is None
        and scanner_safety.get(
            "orders_possible"
        )
        is False
        and (
            "SHADOW"
            in scanner_mode.upper()
            or "VIRTUAL"
            in scanner_mode.upper()
        )
    )

    add_check(
        checks,
        "scanner_orders_impossible",
        "veiligheid",
        "critical",
        scanner_orders_impossible,
        (
            f"scanner mode={scanner_mode or '-'}; orders_possible="
            f"{scanner_safety.get('orders_possible')}"
            if scanner_report_error is None
            else scanner_report_error
        ),
    )

    lab_safety = (
        strategy_lab.get(
            "safety"
        )
        or {}
    )

    lab_safe = (
        strategy_lab.get(
            "mode"
        )
        == "READ_ONLY_STRATEGY_ANALYSIS"
        and lab_safety.get(
            "orders_possible"
        )
        is False
        and lab_safety.get(
            "exchange_connection_used"
        )
        is False
        and lab_safety.get(
            "bot_state_modified"
        )
        is False
        and lab_safety.get(
            "scanner_state_modified"
        )
        is False
        and lab_safety.get(
            "settings_modified"
        )
        is False
        and lab_safety.get(
            "automatic_strategy_changes"
        )
        is False
    )

    add_check(
        checks,
        "strategy_lab_read_only",
        "veiligheid",
        "critical",
        (
            lab_error is None
            and lab_safe
        ),
        (
            "Strategy Lab volledig alleen-lezen"
            if lab_error is None and lab_safe
            else (
                lab_error
                or "Strategy Lab-veiligheidsvelden zijn niet volledig veilig"
            )
        ),
    )

    skip_processes = to_bool(
        os.getenv(
            "READINESS_SKIP_PROCESS_CHECK"
        ),
        False,
    )

    commands = (
        []
        if skip_processes
        else process_command_lines()
    )

    process_results: Dict[
        str,
        bool
    ] = {}

    for display_name, pattern in PROCESS_PATTERNS:
        running = (
            True
            if skip_processes
            else process_running(
                commands,
                pattern,
            )
        )

        process_results[
            display_name
        ] = running

        add_check(
            checks,
            (
                "process_"
                + display_name.lower().replace(
                    " ",
                    "_",
                )
            ),
            "processen",
            "critical",
            running,
            (
                "proces actief"
                if running
                else f"proces ontbreekt: {pattern}"
            ),
        )

    periodic_tasks = (
        periodic_analysis.get(
            "tasks"
        )
        or {}
    )

    periodic_diagnose = (
        periodic_tasks.get(
            "diagnose"
        )
        or {}
    )

    periodic_scanner = (
        periodic_tasks.get(
            "scanner"
        )
        or {}
    )

    periodic_mode_safe = (
        periodic_analysis_error is None
        and periodic_analysis.get(
            "mode"
        )
        == "SEQUENTIAL_PERIODIC_ANALYSIS"
        and periodic_analysis.get(
            "sequential"
        )
        is True
    )

    add_check(
        checks,
        "periodic_analysis_sequential",
        "processen",
        "critical",
        periodic_mode_safe,
        (
            "periodieke Diagnose en Scanner zijn sequentieel"
            if periodic_mode_safe
            else (
                periodic_analysis_error
                or "periodieke analyse is niet aantoonbaar sequentieel"
            )
        ),
    )

    periodic_task_ages: Dict[
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

    freshness_items = (
        (
            "agent_recent",
            agent_state.get(
                "last_analysis_ts"
            ),
            MAX_AGENT_AGE_MINUTES,
            agent_error,
        ),
        (
            "diagnose_recent",
            (
                diagnose.get(
                    "last_round_at"
                )
                or diagnose.get(
                    "last_round"
                )
            ),
            MAX_DIAGNOSE_AGE_MINUTES,
            diagnose_error,
        ),
        (
            "supervisor_recent",
            supervisor.get(
                "generated_at"
            ),
            MAX_SUPERVISOR_AGE_MINUTES,
            supervisor_error,
        ),
        (
            "scanner_recent",
            (
                scanner_state.get(
                    "last_scan_at"
                )
                or scanner_report.get(
                    "generated_at"
                )
            ),
            MAX_SCANNER_AGE_MINUTES,
            (
                scanner_state_error
                if scanner_state_error is not None
                else scanner_report_error
            ),
        ),
        (
            "strategy_lab_recent",
            strategy_lab.get(
                "generated_at"
            ),
            MAX_STRATEGY_LAB_AGE_MINUTES,
            lab_error,
        ),
    )

    freshness: Dict[
        str,
        Optional[float]
    ] = {}

    for name, timestamp, maximum, error in freshness_items:
        age = age_minutes(
            timestamp
        )

        freshness[
            name
        ] = age

        passed = (
            error is None
            and age is not None
            and age <= maximum
        )

        detail = (
            error
            or (
                f"leeftijd {age:.1f} minuten; maximum {maximum:.1f}"
                if age is not None
                else "geen geldige tijd"
            )
        )

        add_check(
            checks,
            name,
            "actualiteit",
            "critical",
            passed,
            detail,
        )

    backup_age = age_minutes(
        agent_state.get(
            "last_backup_at"
        )
    )

    backup_complete = (
        agent_state.get(
            "last_backup_status"
        )
        == "complete"
        and backup_age is not None
        and backup_age
        <= MAX_BACKUP_AGE_HOURS
        * 60.0
    )

    add_check(
        checks,
        "backup_recent_complete",
        "back-up",
        "critical",
        backup_complete,
        (
            f"status={agent_state.get('last_backup_status') or '-'}; "
            f"leeftijd={(backup_age / 60.0):.1f} uur"
            if backup_age is not None
            else "geen geldige back-uptijd"
        ),
    )

    try:
        disk = shutil.disk_usage(
            DATA_DIR
        )

        free_mb = (
            disk.free
            / 1024.0
            / 1024.0
        )

        disk_error = None

    except Exception as exc:
        free_mb = 0.0
        disk_error = (
            f"{type(exc).__name__}: {exc}"
        )

    add_check(
        checks,
        "disk_space",
        "systeem",
        "critical",
        (
            disk_error is None
            and free_mb
            >= MIN_FREE_DISK_MB
        ),
        (
            f"vrij {free_mb:.1f} MB; minimum {MIN_FREE_DISK_MB:.1f} MB"
            if disk_error is None
            else disk_error
        ),
    )

    scanner_watch_error = str(
        agent_state.get(
            "scanner_watch_last_error"
        )
        or ""
    ).strip()

    add_check(
        checks,
        "scanner_watch_no_error",
        "bewaking",
        "critical",
        not scanner_watch_error,
        (
            "geen scannerwatch-fout"
            if not scanner_watch_error
            else scanner_watch_error
        ),
    )

    scanner_warning_active = to_bool(
        agent_state.get(
            "scanner_watch_alert_active"
        ),
        False,
    )

    add_check(
        checks,
        "scanner_watch_no_active_warning",
        "bewaking",
        "warning",
        not scanner_warning_active,
        (
            "geen actieve scannerwaarschuwing"
            if not scanner_warning_active
            else (
                "actief: "
                + " | ".join(
                    agent_state.get(
                        "scanner_watch_active_conditions"
                    )
                    or [
                        "onbekende scannerwaarschuwing"
                    ]
                )
            )
        ),
    )

    paused = to_bool(
        control.get(
            "paused"
        ),
        False,
    )

    pause_reason = str(
        control.get(
            "pause_reason"
        )
        or "-"
    )

    add_check(
        checks,
        "bot_not_paused",
        "bediening",
        "warning",
        not paused,
        (
            "bot niet gepauzeerd"
            if not paused
            else f"gepauzeerd: {pause_reason}"
        ),
    )

    long_progress = test_progress(
        long_baseline,
        state,
        "start_spot_trades",
        "target_total_trades",
        "target_new_trades",
        "trades",
    )

    short_progress = test_progress(
        short_baseline,
        state,
        "start_short_trades",
        "target_total_short_trades",
        "target_new_trades",
        "short_trades",
    )

    shadow = (
        strategy_lab.get(
            "shadow_trades"
        )
        or {}
    )

    shadow_completed = max(
        0,
        to_int(
            shadow.get(
                "trades"
            ),
            0,
        ),
    )

    shadow_progress = {
        "start": 0,
        "current_total": shadow_completed,
        "completed": shadow_completed,
        "target": TARGET_TRADES,
        "remaining": max(
            0,
            TARGET_TRADES
            - shadow_completed,
        ),
        "complete": (
            shadow_completed
            >= TARGET_TRADES
        ),
        "completion_pct": round(
            min(
                100.0,
                100.0
                * shadow_completed
                / TARGET_TRADES,
            ),
            1,
        ),
        "winrate_pct": to_float(
            shadow.get(
                "winrate_pct"
            ),
            0.0,
        ),
        "net_pnl_eur": to_float(
            shadow.get(
                "net_pnl_eur"
            ),
            0.0,
        ),
        "data_status": shadow.get(
            "data_status"
        )
        or "-",
    }

    long_progress[
        "baseline_readable"
    ] = long_error is None

    short_progress[
        "baseline_readable"
    ] = short_error is None

    short_progress[
        "paper_only"
    ] = paper_only

    short_progress[
        "signals_enabled"
    ] = short_signals

    long_report_ready = (
        not long_progress[
            "complete"
        ]
        or LONG_REPORT_FILE.is_file()
    )

    short_report_ready = (
        not short_progress[
            "complete"
        ]
        or SHORT_REPORT_FILE.is_file()
    )

    shadow_report_ready = (
        not shadow_progress[
            "complete"
        ]
        or SHADOW_MILESTONE_20_FILE.is_file()
    )

    reports_ready = (
        long_report_ready
        and short_report_ready
        and shadow_report_ready
    )

    final_status = final_validation_status(
        final_validation
    )

    approval_status = live_approval_status(
        live_approval
    )

    critical_failures = [
        check
        for check in checks
        if check[
            "level"
        ]
        == "critical"
        and not check[
            "passed"
        ]
    ]

    warnings = [
        check
        for check in checks
        if check[
            "level"
        ]
        == "warning"
        and not check[
            "passed"
        ]
    ]

    status, phase = determine_status(
        critical_failures,
        warnings,
        long_progress[
            "completed"
        ],
        short_progress[
            "completed"
        ],
        shadow_progress[
            "completed"
        ],
        reports_ready,
        final_status[
            "passed"
        ],
        approval_status[
            "approved"
        ],
    )

    milestone_candidates: List[
        Tuple[int, str, int]
    ] = []

    for label, progress in (
        (
            "longtest",
            long_progress,
        ),
        (
            "paper-shorttest",
            short_progress,
        ),
        (
            "schaduwtest",
            shadow_progress,
        ),
    ):
        milestone, remaining = (
            next_trade_milestone(
                int(
                    progress[
                        "completed"
                    ]
                )
            )
        )

        if remaining > 0:
            milestone_candidates.append(
                (
                    remaining,
                    label,
                    milestone,
                )
            )

    if critical_failures:
        next_step = (
            "Herstel eerst de kritieke controle: "
            + critical_failures[0][
                "detail"
            ]
        )

    elif milestone_candidates:
        remaining, label, milestone = min(
            milestone_candidates,
            key=lambda item: (
                item[0],
                item[1],
            ),
        )

        next_step = (
            f"{label} naar {milestone}/20: "
            f"nog {remaining} gesloten trade"
            + (
                ""
                if remaining == 1
                else "s"
            )
        )

    elif not reports_ready:
        next_step = (
            "Laat de ontbrekende eindrapporten genereren en controleren."
        )

    elif warnings:
        next_step = (
            "Los de actieve waarschuwingen op voordat de eindtest start."
        )

    elif not final_status[
        "passed"
    ]:
        next_step = (
            "Voer de vaste dry-run eindtest van minimaal zeven dagen uit."
        )

    elif not approval_status[
        "approved"
    ]:
        next_step = (
            "Handmatige beoordeling en expliciete goedkeuring voor beperkte live-start."
        )

    else:
        next_step = (
            "Beperkte live-start uitsluitend handmatig uitvoeren."
        )

    average_completion = (
        long_progress[
            "completion_pct"
        ]
        + short_progress[
            "completion_pct"
        ]
        + shadow_progress[
            "completion_pct"
        ]
    ) / 3.0

    report = {
        "version": VERSION,
        "mode": MODE,
        "generated_at": now_utc().isoformat(),
        "status": status,
        "phase": phase,
        "next_step": next_step,
        "test_completion_pct": round(
            average_completion,
            1,
        ),
        "critical_failure_count": len(
            critical_failures
        ),
        "warning_count": len(
            warnings
        ),
        "critical_failures": (
            critical_failures
        ),
        "warnings": warnings,
        "checks": checks,
        "test_progress": {
            "long": long_progress,
            "paper_short": short_progress,
            "shadow": shadow_progress,
        },
        "reports": {
            "long_report_ready": long_report_ready,
            "short_report_ready": short_report_ready,
            "shadow_report_ready": shadow_report_ready,
            "all_required_reports_ready": reports_ready,
        },
        "final_validation": final_status,
        "live_approval": approval_status,
        "system": {
            "dry_run": dry_run,
            "paper_only": paper_only,
            "scanner_orders_impossible": scanner_orders_impossible,
            "strategy_lab_read_only": lab_safe,
            "control_paused": paused,
            "pause_reason": pause_reason,
            "scanner_warning_active": scanner_warning_active,
            "processes": process_results,
            "periodic_analysis": {
                "mode": periodic_analysis.get("mode"),
                "sequential": periodic_analysis.get("sequential"),
                "active_task": periodic_analysis.get("active_task"),
                "cycle_count": to_int(periodic_analysis.get("cycle_count"), 0),
                "diagnose_last_status": periodic_diagnose.get("last_status"),
                "diagnose_last_exit_code": periodic_diagnose.get("last_exit_code"),
                "diagnose_age_minutes": periodic_task_ages.get("diagnose"),
                "scanner_last_status": periodic_scanner.get("last_status"),
                "scanner_last_exit_code": periodic_scanner.get("last_exit_code"),
                "scanner_age_minutes": periodic_task_ages.get("scanner"),
            },
            "freshness_minutes": freshness,
            "backup_age_hours": (
                round(
                    backup_age / 60.0,
                    2,
                )
                if backup_age is not None
                else None
            ),
            "free_disk_mb": round(
                free_mb,
                1,
            ),
        },
        "safety": {
            "orders_possible": False,
            "exchange_connection_used": False,
            "bot_state_modified": False,
            "control_state_modified": False,
            "scanner_state_modified": False,
            "settings_modified": False,
            "automatic_live_activation": False,
            "manual_live_approval_required": True,
        },
    }

    return report


def bool_text(
    value: Any,
) -> str:
    return (
        "JA"
        if to_bool(
            value,
            False,
        )
        else "NEE"
    )


def format_test_line(
    label: str,
    progress: Dict[str, Any],
) -> str:
    return (
        f"{label:<24}: "
        f"{int(to_float(progress.get('completed'), 0.0))}/"
        f"{int(to_float(progress.get('target'), TARGET_TRADES))} | "
        f"nog {int(to_float(progress.get('remaining'), 0.0))} | "
        f"{to_float(progress.get('completion_pct'), 0.0):.1f}%"
    )


def format_report(
    report: Dict[str, Any],
) -> str:
    progress = (
        report.get(
            "test_progress"
        )
        or {}
    )

    system = (
        report.get(
            "system"
        )
        or {}
    )

    final_validation = (
        report.get(
            "final_validation"
        )
        or {}
    )

    approval = (
        report.get(
            "live_approval"
        )
        or {}
    )

    lines = [
        "=" * 72,
        "DIAMOND TRADER READINESS GATE",
        "=" * 72,
        f"Versie                  : {report.get('version') or '-'}",
        f"Modus                   : {report.get('mode') or '-'}",
        f"Gegenereerd             : {report.get('generated_at') or '-'}",
        "",
        "CENTRALE STATUS",
        f"Status                  : {report.get('status') or '-'}",
        f"Fase                    : {report.get('phase') or '-'}",
        f"Totale testvoortgang    : {to_float(report.get('test_completion_pct'), 0.0):.1f}%",
        f"Kritieke problemen      : {int(to_float(report.get('critical_failure_count'), 0.0))}",
        f"Waarschuwingen          : {int(to_float(report.get('warning_count'), 0.0))}",
        f"Volgende stap           : {report.get('next_step') or '-'}",
        "",
        "TESTVOORTGANG",
        format_test_line(
            "Longtest",
            progress.get(
                "long"
            )
            or {},
        ),
        format_test_line(
            "Paper-shorttest",
            progress.get(
                "paper_short"
            )
            or {},
        ),
        format_test_line(
            "Schaduwtest",
            progress.get(
                "shadow"
            )
            or {},
        ),
        "",
        "VEILIGHEID",
        f"Dry-run actief          : {bool_text(system.get('dry_run'))}",
        f"Paper-short only        : {bool_text(system.get('paper_only'))}",
        f"Scannerorders onmogelijk: {bool_text(system.get('scanner_orders_impossible'))}",
        f"Strategy Lab alleen-lezen: {bool_text(system.get('strategy_lab_read_only'))}",
        f"Scannerwaarschuwing     : {bool_text(system.get('scanner_warning_active'))}",
        f"Bot gepauzeerd          : {bool_text(system.get('control_paused'))}",
        f"Vrije schijfruimte      : {to_float(system.get('free_disk_mb'), 0.0):.1f} MB",
        "",
        "EINDTEST EN LIVE-SLOT",
        f"Eindtest beschikbaar    : {bool_text(final_validation.get('available'))}",
        f"Eindtest geslaagd       : {bool_text(final_validation.get('passed'))}",
        f"Handmatig goedgekeurd   : {bool_text(approval.get('approved'))}",
        "Automatisch live zetten: NEE",
    ]

    critical = (
        report.get(
            "critical_failures"
        )
        or []
    )

    if critical:
        lines.extend([
            "",
            "KRITIEKE PROBLEMEN",
        ])

        for item in critical:
            lines.append(
                f"- {item.get('name')}: {item.get('detail')}"
            )

    warnings = (
        report.get(
            "warnings"
        )
        or []
    )

    if warnings:
        lines.extend([
            "",
            "WAARSCHUWINGEN",
        ])

        for item in warnings:
            lines.append(
                f"- {item.get('name')}: {item.get('detail')}"
            )

    lines.extend([
        "",
        "SLOT",
        "Deze gate is uitsluitend adviserend en alleen-lezen.",
        "Hij kan geen orders plaatsen en Diamond Trader niet live zetten.",
        "Een beperkte live-start vereist altijd een geslaagde eindtest",
        "én een afzonderlijke handmatige goedkeuring.",
        "=" * 72,
    ])

    return "\n".join(
        lines
    )


def save_report(
    report: Dict[str, Any],
) -> None:
    save_json_atomic(
        REPORT_JSON_FILE,
        report,
    )

    save_text_atomic(
        REPORT_TEXT_FILE,
        format_report(
            report
        )
        + "\n",
    )


def self_test() -> None:
    no_failures: List[
        Dict[str, Any]
    ] = []

    warnings: List[
        Dict[str, Any]
    ] = [{
        "name": "test_warning",
    }]

    cases = [
        (
            determine_status(
                [{
                    "name": "critical",
                }],
                [],
                20,
                20,
                20,
                True,
                True,
                True,
            )[0],
            "NIET GEREED",
        ),
        (
            determine_status(
                no_failures,
                [],
                5,
                4,
                1,
                False,
                False,
                False,
            ),
            (
                "NIET GEREED",
                "TESTEN LOPEN",
            ),
        ),
        (
            determine_status(
                no_failures,
                [],
                10,
                12,
                15,
                False,
                False,
                False,
            )[0],
            "BIJNA GEREED",
        ),
        (
            determine_status(
                no_failures,
                [],
                20,
                20,
                20,
                True,
                False,
                False,
            )[0],
            "GEREED VOOR EINDTEST",
        ),
        (
            determine_status(
                no_failures,
                warnings,
                20,
                20,
                20,
                True,
                False,
                False,
            )[0],
            "BIJNA GEREED",
        ),
        (
            determine_status(
                no_failures,
                [],
                20,
                20,
                20,
                True,
                True,
                True,
            )[0],
            "GEREED VOOR BEPERKTE LIVE-START",
        ),
    ]

    for actual, expected in cases:
        if actual != expected:
            raise RuntimeError(
                f"Self-test mislukt: {actual!r} != {expected!r}"
            )

    print(
        "READINESS_GATE_SELF_TEST_OK"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Diamond Trader Readiness Gate"
        )
    )

    parser.add_argument(
        "--no-print",
        action="store_true",
        help="Schrijf rapporten zonder volledig tekstrapport op stdout.",
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Voer interne statustests uit.",
    )

    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    report = build_report()

    save_report(
        report
    )

    if args.no_print:
        print(
            "Readiness Gate gereed | "
            f"status={report.get('status')} | "
            f"fase={report.get('phase')} | "
            f"long={report['test_progress']['long']['completed']}/20 | "
            f"short={report['test_progress']['paper_short']['completed']}/20 | "
            f"shadow={report['test_progress']['shadow']['completed']}/20 | "
            f"kritiek={report.get('critical_failure_count')} | "
            f"waarschuwingen={report.get('warning_count')}"
        )

    else:
        print(
            format_report(
                report
            )
        )


if __name__ == "__main__":
    main()
