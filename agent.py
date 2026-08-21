#!/usr/bin/env python3
"""
Diamond Agent v7.5

Functies:
- Stuurt statusmails om 06:00, 10:00, 14:00, 18:00 en 22:00.
- Stuurt zondag om 22:00 een uitgebreider weekrapport.
- Leest de botposities en transacties.
- Schrijft nooit in diamond_state.json.
- Gebruikt diamond_control.json voor veiligheidsstops.
- Pauzeert alleen nieuwe aankopen.
- Open posities blijven door diamond_bot.py bewaakt.
- Pauzeert automatisch wanneer het ingestelde dry-run testdoel is bereikt.
- Maakt automatisch een eindrapport van uitsluitend de nieuwe longtesttrades.
- Bewaakt daarnaast een volledig afzonderlijke paper-shorttest.
- Maakt en mailt paper-shorttussenrapporten na 5 en 10 gesloten shorts.
- Maakt en mailt het paper-shorteindrapport na 20 gesloten shorts.
- Maakt dagelijks een controleerbare back-up op de permanente schijf.
- Neemt de Market Scanner-signalen, scanner-state en schaduwtrades mee in de back-up.
- Neemt Market Scanner-status en schaduwresultaten op in status- en weekmails.
- Stuurt direct een e-mail wanneer een Market Scanner-schaduwtrade opent of sluit.
- Maakt en mailt vaste schaduwmijlpaalrapporten na 5, 10 en 20 gesloten trades.
- Ververst Strategy Lab direct zodra een schaduwpositie opent of sluit.
- Neemt Strategy Lab-resultaten op in statusmails en weekrapporten.
- Waarschuwt bij langdurig geen geschikte schaduwtrade of een dominant afwijzingsfilter.
- Draait een centrale, alleen-lezen Readiness Gate en mailt statuswijzigingen.
- Neemt Strategy Lab-, schaduwmijlpaal- en Readiness Gate-rapporten mee in de back-up.
- Bewaart dagelijkse back-ups 30 dagen en verwijdert alleen oude back-upmappen.
"""

import csv
import json
import hashlib
import logging
import os
import shutil
import smtplib
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import ccxt
import yaml
from dotenv import load_dotenv


# ============================================================
# Omgevingsvariabelen laden
# ============================================================

load_dotenv()


# ============================================================
# Logging
# ============================================================

LOG = logging.getLogger("diamond_agent")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# Algemene instellingen
# ============================================================

LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

STATE_FILE = os.getenv(
    "STATE_FILE",
    "/var/data/diamond_state.json",
).strip()

TRADES_FILE = os.getenv(
    "TRADES_FILE",
    "/var/data/diamond_transactions.csv",
).strip()

AGENT_STATE_FILE = os.getenv(
    "AGENT_STATE_FILE",
    "/var/data/diamond_agent_state.json",
).strip()

CONTROL_FILE = os.getenv(
    "CONTROL_FILE",
    "/var/data/diamond_control.json",
).strip()

CFG_FILE = os.getenv(
    "CFG_FILE",
    "/opt/render/project/src/config.yaml",
).strip()

TEST_BASELINE_FILE = os.getenv(
    "TEST_BASELINE_FILE",
    "/var/data/diamond_test_baseline.json",
).strip()

TEST_REPORT_FILE = os.getenv(
    "TEST_REPORT_FILE",
    "/var/data/diamond_test_report.json",
).strip()

SHORT_TEST_BASELINE_FILE = os.getenv(
    "SHORT_TEST_BASELINE_FILE",
    "/var/data/diamond_short_test_baseline.json",
).strip()

SHORT_TEST_REPORT_FILE = os.getenv(
    "SHORT_TEST_REPORT_FILE",
    "/var/data/diamond_short_test_report.json",
).strip()

SHORT_TEST_INTERIM_MILESTONES = (
    5,
    10,
)

DIAG_STATS_FILE = os.getenv(
    "DIAG_STATS_FILE",
    "/var/data/diamond_diagnose_stats.json",
).strip()

SUPERVISOR_STATE_FILE = os.getenv(
    "SUPERVISOR_STATE_FILE",
    "/var/data/diamond_supervisor_state.json",
).strip()

MARKET_SIGNALS_JSON_FILE = os.getenv(
    "MARKET_SIGNALS_JSON_FILE",
    "/var/data/diamond_market_signals.json",
).strip()

MARKET_SIGNALS_CSV_FILE = os.getenv(
    "MARKET_SIGNALS_CSV_FILE",
    "/var/data/diamond_market_signals.csv",
).strip()

MARKET_SCANNER_STATE_FILE = os.getenv(
    "MARKET_SCANNER_STATE_FILE",
    "/var/data/diamond_market_scanner_state.json",
).strip()

SHADOW_TRADES_FILE = os.getenv(
    "SHADOW_TRADES_FILE",
    "/var/data/diamond_shadow_trades.csv",
).strip()

STRATEGY_LAB_JSON_FILE = os.getenv(
    "STRATEGY_LAB_JSON_FILE",
    "/var/data/diamond_strategy_lab.json",
).strip()

STRATEGY_LAB_TEXT_FILE = os.getenv(
    "STRATEGY_LAB_TEXT_FILE",
    "/var/data/diamond_strategy_lab.txt",
).strip()

STRATEGY_LAB_GROUPS_FILE = os.getenv(
    "STRATEGY_LAB_GROUPS_FILE",
    "/var/data/diamond_strategy_lab_groups.csv",
).strip()

STRATEGY_LAB_SCRIPT_FILE = os.getenv(
    "STRATEGY_LAB_SCRIPT_FILE",
    "/opt/render/project/src/strategy_lab.py",
).strip()

READINESS_GATE_SCRIPT_FILE = os.getenv(
    "READINESS_GATE_SCRIPT_FILE",
    "/opt/render/project/src/readiness_gate.py",
).strip()

READINESS_GATE_JSON_FILE = os.getenv(
    "READINESS_GATE_JSON_FILE",
    "/var/data/diamond_readiness_gate.json",
).strip()

READINESS_GATE_TEXT_FILE = os.getenv(
    "READINESS_GATE_TEXT_FILE",
    "/var/data/diamond_readiness_gate.txt",
).strip()

FINAL_VALIDATION_FILE = os.getenv(
    "FINAL_VALIDATION_FILE",
    "/var/data/diamond_final_validation.json",
).strip()

LIVE_APPROVAL_FILE = os.getenv(
    "LIVE_APPROVAL_FILE",
    "/var/data/diamond_live_approval.json",
).strip()

BACKUP_DIR = os.getenv(
    "BACKUP_DIR",
    "/var/data/backups",
).strip()

GMAIL_USER = os.getenv(
    "GMAIL_USER",
    "joshuatec7@gmail.com",
).strip()

GMAIL_PASS = os.getenv(
    "GMAIL_APP_PASSWORD",
    "",
).strip()

BITVAVO_API_KEY = os.getenv(
    "BITVAVO_API_KEY",
    "",
).strip()

BITVAVO_API_SECRET = os.getenv(
    "BITVAVO_API_SECRET",
    "",
).strip()


# Rapporttijden in Nederlandse tijd
REPORT_HOURS = {
    6,
    10,
    14,
    18,
    22,
}

# Zondag
WEEKLY_REPORT_WEEKDAY = 6

# Veiligheidsanalyse iedere 15 minuten
ANALYZE_INTERVAL_SECONDS = 15 * 60

# Agent controleert iedere minuut of er werk moet gebeuren
LOOP_SLEEP_SECONDS = 60

# Dagelijkse back-upinstellingen in Nederlandse tijd.
BACKUP_HOUR_LOCAL = 3
BACKUP_RETENTION_DAYS = 30
BACKUP_MAX_AGE_HOURS = 36

# Veiligheidsgrenzen
MAX_DAY_LOSS_PCT = 1.5
BTC_DROP_LIMIT_PCT = -8.0
BTC_RECOVERY_PCT = 4.0

DEFAULT_TOTAL_CAPITAL = 3000.0

# Market Scanner-schaduwmeldingen
SHADOW_NOTIFICATION_HISTORY_LIMIT = 250
SHADOW_NOTIFICATION_RETRY_MINUTES = 15

# Vaste evaluatiemomenten van de Market Scanner-schaduwtest.
SHADOW_MILESTONE_REPORTS = (
    5,
    10,
    20,
)

SHADOW_MILESTONE_STAKES = (
    120.0,
    125.0,
    130.0,
    135.0,
)

# Directe Strategy Lab-verversing bij een gewijzigde schaduwstand.
STRATEGY_LAB_REFRESH_TIMEOUT_SECONDS = 120
STRATEGY_LAB_REFRESH_RETRY_MINUTES = 15

# Market Scanner-bewaking.
# De bewaking is uitsluitend adviserend en wijzigt geen filters.
SCANNER_WATCH_CHECK_INTERVAL_SECONDS = 15 * 60
SCANNER_WATCH_ANALYSIS_HOURS = 24
SCANNER_WATCH_STAGNATION_HOURS = 24
SCANNER_WATCH_ALERT_COOLDOWN_HOURS = 24
SCANNER_WATCH_RETRY_MINUTES = 15
SCANNER_WATCH_MIN_REJECTED_SIGNALS = 10
SCANNER_WATCH_DOMINANT_MIN_COUNT = 8
SCANNER_WATCH_DOMINANT_SHARE_PCT = 60.0

# Centrale gereedheidscontrole.
READINESS_GATE_CHECK_INTERVAL_SECONDS = 15 * 60
READINESS_GATE_TIMEOUT_SECONDS = 120
READINESS_GATE_RETRY_MINUTES = 15


# ============================================================
# Algemene hulpfuncties
# ============================================================

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        if value is None or value == "":
            return default

        return float(value)

    except (TypeError, ValueError):
        return default


def to_bool(
    value: Any,
    default: bool = False,
) -> bool:
    if isinstance(value, bool):
        return value

    if value is None:
        return default

    normalized = str(value).strip().lower()

    if normalized in {
        "1",
        "true",
        "yes",
        "ja",
        "aan",
        "on",
    }:
        return True

    if normalized in {
        "0",
        "false",
        "no",
        "nee",
        "uit",
        "off",
    }:
        return False

    return default


def ensure_parent(path_str: str) -> None:
    Path(path_str).parent.mkdir(
        parents=True,
        exist_ok=True,
    )


def config_dry_run() -> bool:
    """
    Leest de actuele dry-runinstelling uit config.yaml.

    Bij een lees- of YAML-fout wordt veilig aangenomen dat dry-run actief is.
    """
    try:
        with Path(CFG_FILE).open(
            "r",
            encoding="utf-8",
        ) as file:
            config = yaml.safe_load(file) or {}

        if not isinstance(config, dict):
            raise ValueError(
                "config.yaml bevat geen geldige dictionary"
            )

        risk = config.get("risk") or {}

        if not isinstance(risk, dict):
            risk = {}

        return to_bool(
            risk.get("dry_run"),
            True,
        )

    except Exception as exc:
        LOG.warning(
            "Dry-runstatus lezen mislukt; veilige standaard true gebruikt: %s",
            exc,
        )

        return True


def load_json(
    path_str: str,
    default: Dict[str, Any],
) -> Dict[str, Any]:
    path = Path(path_str)

    if not path.exists():
        return default.copy()

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if isinstance(data, dict):
            result = default.copy()
            result.update(data)
            return result

    except Exception as exc:
        LOG.error(
            "JSON lezen mislukt voor %s: %s",
            path_str,
            exc,
        )

    return default.copy()


def save_json_atomic(
    path_str: str,
    data: Dict[str, Any],
) -> None:
    ensure_parent(path_str)

    target = Path(path_str)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        delete=False,
    ) as temporary:
        json.dump(
            data,
            temporary,
            indent=2,
            ensure_ascii=False,
        )

        temporary_name = temporary.name

    os.replace(
        temporary_name,
        target,
    )


def save_text_atomic(
    path_str: str,
    text: str,
) -> None:
    ensure_parent(
        path_str
    )

    target = Path(
        path_str
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(
            target.parent
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
        target,
    )


def load_test_baseline() -> Optional[Dict[str, Any]]:
    """
    Leest de nulmeting voor de actuele dry-run test.

    Zonder geldig baselinebestand is de automatische teststop uitgeschakeld.
    """
    path = Path(TEST_BASELINE_FILE)

    if not path.exists():
        return None

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            baseline = json.load(file)

        if not isinstance(baseline, dict):
            raise ValueError(
                "baseline bevat geen JSON-object"
            )

        return baseline

    except Exception as exc:
        LOG.error(
            "Testbaseline lezen mislukt voor %s: %s",
            TEST_BASELINE_FILE,
            exc,
        )

        return None


def get_test_target_status() -> Dict[str, Any]:
    """
    Geeft de voortgang van de ingestelde dry-run test terug.
    """
    baseline = load_test_baseline()
    state = load_bot_state()

    if baseline is None:
        return {
            "enabled": False,
            "reason": "geen_geldige_baseline",
        }

    start_trades = int(
        to_float(
            baseline.get("start_spot_trades"),
            0,
        )
    )

    target_total = int(
        to_float(
            baseline.get("target_total_trades"),
            0,
        )
    )

    current_trades = int(
        to_float(
            state.get("trades"),
            0,
        )
    )

    valid = (
        start_trades >= 0
        and target_total > start_trades
    )

    return {
        "enabled": valid,
        "dry_run": config_dry_run(),
        "start_trades": start_trades,
        "target_total_trades": target_total,
        "current_trades": current_trades,
        "new_trades": max(
            0,
            current_trades - start_trades,
        ),
        "remaining_trades": max(
            0,
            target_total - current_trades,
        ),
        "target_reached": (
            valid
            and current_trades >= target_total
        ),
    }


def load_short_test_baseline() -> Optional[Dict[str, Any]]:
    """
    Leest de afzonderlijke nulmeting van de paper-shorttest.
    """
    path = Path(
        SHORT_TEST_BASELINE_FILE
    )

    if not path.exists():
        return None

    try:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            baseline = json.load(file)

        if not isinstance(
            baseline,
            dict,
        ):
            raise ValueError(
                "shortbaseline bevat geen JSON-object"
            )

        return baseline

    except Exception as exc:
        LOG.error(
            "Paper-shortbaseline lezen mislukt voor %s: %s",
            SHORT_TEST_BASELINE_FILE,
            exc,
        )

        return None


def config_short_test_enabled() -> bool:
    try:
        with Path(CFG_FILE).open(
            "r",
            encoding="utf-8",
        ) as file:
            config = yaml.safe_load(file) or {}

        trading = config.get(
            "trading"
        ) or {}

        short = config.get(
            "short"
        ) or {}

        return (
            to_bool(
                trading.get(
                    "enable_short_signals"
                ),
                False,
            )
            and to_bool(
                short.get(
                    "enabled"
                ),
                False,
            )
            and to_bool(
                short.get(
                    "paper_only"
                ),
                True,
            )
        )

    except Exception as exc:
        LOG.warning(
            "Paper-shortconfig lezen mislukt: %s",
            exc,
        )

        return False


def get_short_test_target_status() -> Dict[str, Any]:
    """
    Geeft de voortgang van de afzonderlijke paper-shorttest terug.
    """
    baseline = load_short_test_baseline()
    state = load_bot_state()

    if baseline is None:
        return {
            "enabled": False,
            "reason": "geen_geldige_shortbaseline",
        }

    start = int(
        to_float(
            baseline.get(
                "start_short_trades"
            ),
            0,
        )
    )

    target_total = int(
        to_float(
            baseline.get(
                "target_total_short_trades"
            ),
            0,
        )
    )

    current = int(
        to_float(
            state.get(
                "short_trades"
            ),
            0,
        )
    )

    valid = (
        start >= 0
        and target_total > start
    )

    return {
        "enabled": (
            valid
            and config_short_test_enabled()
        ),
        "paper_only": True,
        "start_short_trades": start,
        "target_total_short_trades": target_total,
        "current_short_trades": current,
        "new_short_trades": max(
            0,
            current - start,
        ),
        "remaining_short_trades": max(
            0,
            target_total - current,
        ),
        "target_reached": (
            valid
            and current >= target_total
        ),
    }


# ============================================================
# Standaardbestanden
# ============================================================

def default_bot_state() -> Dict[str, Any]:
    return {
        "positions": {},
        "short_positions": {},
        "pnl_quote": 0.0,
        "short_pnl_quote": 0.0,
        "trades": 0,
        "wins": 0,
        "short_trades": 0,
        "short_wins": 0,
        "simulated_free_quote": None,
    }


def default_control() -> Dict[str, Any]:
    return {
        "paused": False,
        "pause_reason": "",
        "paused_at": None,
        "pause_date": None,
        "pause_btc_price": None,
    }


def default_agent_state() -> Dict[str, Any]:
    return {
        "last_analysis_ts": 0.0,
        "sent_reports": [],
        "sent_weekly_reports": [],
        "notified_shadow_open_keys": [],
        "notified_shadow_close_keys": [],
        "shadow_open_notifications_sent": 0,
        "shadow_close_notifications_sent": 0,
        "last_shadow_open_email_at": "",
        "last_shadow_open_symbol": "",
        "last_shadow_close_email_at": "",
        "last_shadow_close_symbol": "",
        "last_shadow_open_attempt_key": "",
        "last_shadow_open_attempt_at": "",
        "last_shadow_close_attempt_key": "",
        "last_shadow_close_attempt_at": "",
        "last_strategy_lab_input_fingerprint": "",
        "last_strategy_lab_attempt_fingerprint": "",
        "last_strategy_lab_refresh_attempt_at": "",
        "last_strategy_lab_refresh_at": "",
        "last_strategy_lab_refresh_status": "",
        "last_strategy_lab_refresh_error": "",
        "strategy_lab_refresh_count": 0,
        "last_scanner_watch_ts": 0.0,
        "scanner_watch_checks": 0,
        "scanner_watch_last_check_at": "",
        "scanner_watch_last_status": "",
        "scanner_watch_last_suitable_at": "",
        "scanner_watch_hours_without_suitable": 0.0,
        "scanner_watch_signals_window": 0,
        "scanner_watch_eligible_window": 0,
        "scanner_watch_rejected_window": 0,
        "scanner_watch_dominant_filter": "",
        "scanner_watch_dominant_count": 0,
        "scanner_watch_dominant_share_pct": 0.0,
        "scanner_watch_active_conditions": [],
        "scanner_watch_alert_active": False,
        "scanner_watch_alert_fingerprint": "",
        "scanner_watch_last_alert_at": "",
        "scanner_watch_last_attempt_fingerprint": "",
        "scanner_watch_last_attempt_at": "",
        "scanner_watch_alert_count": 0,
        "scanner_watch_last_recovery_at": "",
        "scanner_watch_recovery_count": 0,
        "scanner_watch_last_error": "",
        "last_readiness_gate_ts": 0.0,
        "readiness_gate_runs": 0,
        "readiness_gate_last_run_at": "",
        "readiness_gate_last_status": "",
        "readiness_gate_last_phase": "",
        "readiness_gate_last_next_step": "",
        "readiness_gate_test_completion_pct": 0.0,
        "readiness_gate_critical_count": 0,
        "readiness_gate_warning_count": 0,
        "readiness_gate_last_error": "",
        "readiness_gate_current_fingerprint": "",
        "readiness_gate_notified_fingerprint": "",
        "readiness_gate_last_attempt_fingerprint": "",
        "readiness_gate_last_attempt_at": "",
        "readiness_gate_last_email_at": "",
        "readiness_gate_email_count": 0,
        "last_backup_date": "",
        "last_backup_at": "",
        "last_backup_path": "",
        "last_backup_status": "",
        "last_backup_file_count": 0,
        "last_backup_total_bytes": 0,
        "last_backup_error": "",
        "last_backup_error_date": "",
    }


# ============================================================
# Bot-state lezen
# ============================================================

def load_bot_state() -> Dict[str, Any]:
    state = load_json(
        STATE_FILE,
        default_bot_state(),
    )

    if not isinstance(
        state.get("positions"),
        dict,
    ):
        state["positions"] = {}

    if not isinstance(
        state.get("short_positions"),
        dict,
    ):
        state["short_positions"] = {}

    return state


# ============================================================
# Controlebestand
# ============================================================

def load_control() -> Dict[str, Any]:
    control = load_json(
        CONTROL_FILE,
        default_control(),
    )

    control["paused"] = to_bool(
        control.get("paused"),
        False,
    )

    return control


def save_control(
    paused: bool,
    reason: str = "",
    extra_values: Optional[Dict[str, Any]] = None,
) -> None:
    control = load_control()

    control["paused"] = paused
    control["pause_reason"] = reason

    if paused:
        control["paused_at"] = now_utc().isoformat()
    else:
        control["paused_at"] = None

    if extra_values:
        control.update(extra_values)

    save_json_atomic(
        CONTROL_FILE,
        control,
    )


# ============================================================
# Agent-state
# ============================================================

def load_agent_state() -> Dict[str, Any]:
    state = load_json(
        AGENT_STATE_FILE,
        default_agent_state(),
    )

    if not isinstance(
        state.get("sent_reports"),
        list,
    ):
        state["sent_reports"] = []

    if not isinstance(
        state.get("sent_weekly_reports"),
        list,
    ):
        state["sent_weekly_reports"] = []

    if not isinstance(
        state.get("notified_shadow_open_keys"),
        list,
    ):
        state["notified_shadow_open_keys"] = []

    if not isinstance(
        state.get("notified_shadow_close_keys"),
        list,
    ):
        state["notified_shadow_close_keys"] = []

    if not isinstance(
        state.get("scanner_watch_active_conditions"),
        list,
    ):
        state["scanner_watch_active_conditions"] = []

    defaults = default_agent_state()

    for key, value in defaults.items():
        state.setdefault(
            key,
            value,
        )

    return state


def save_agent_state(
    state: Dict[str, Any],
) -> None:
    save_json_atomic(
        AGENT_STATE_FILE,
        state,
    )


# ============================================================
# Transacties lezen
# ============================================================

def load_trades() -> List[Dict[str, str]]:
    path = Path(TRADES_FILE)

    if not path.exists():
        return []

    try:
        with path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as file:
            return list(
                csv.DictReader(file)
            )

    except Exception as exc:
        LOG.error(
            "Transactiebestand lezen mislukt: %s",
            exc,
        )

        return []


def trade_pnl(
    row: Dict[str, str],
) -> float:
    if row.get("net_pnl_quote") not in {
        None,
        "",
    }:
        return to_float(
            row.get("net_pnl_quote"),
            0.0,
        )

    return to_float(
        row.get("pnl"),
        0.0,
    )


def is_closed_spot_trade(
    row: Dict[str, str],
) -> bool:
    return (
        str(row.get("side", "")).upper()
        == "SELL"
    )


def is_closed_short_trade(
    row: Dict[str, str],
) -> bool:
    return (
        str(row.get("side", "")).upper()
        == "SHORT_CLOSE"
    )


def parse_trade_datetime(
    row: Dict[str, str],
) -> Optional[datetime]:
    raw = str(
        row.get("ts", "")
    ).strip()

    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(
            raw.replace(
                "Z",
                "+00:00",
            )
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc,
            )

        return parsed.astimezone(
            LOCAL_TZ
        )

    except ValueError:
        pass

    try:
        parsed = datetime.strptime(
            raw,
            "%Y-%m-%d %H:%M:%S",
        )

        return parsed.replace(
            tzinfo=timezone.utc,
        ).astimezone(
            LOCAL_TZ
        )

    except ValueError:
        return None


def get_day_pnl(
    trades: List[Dict[str, str]],
) -> float:
    today = now_local().date()
    total = 0.0

    for row in trades:
        if not is_closed_spot_trade(row):
            continue

        trade_time = parse_trade_datetime(row)

        if (
            trade_time
            and trade_time.date() == today
        ):
            total += trade_pnl(row)

    return total


def get_week_trades(
    trades: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    cutoff = (
        now_local()
        - timedelta(days=7)
    )

    result = []

    for row in trades:
        if not is_closed_spot_trade(row):
            continue

        trade_time = parse_trade_datetime(row)

        if (
            trade_time
            and trade_time >= cutoff
        ):
            result.append(row)

    return result


# ============================================================
# Automatisch testrapport
# ============================================================

def trade_market(
    row: Dict[str, str],
) -> str:
    return str(
        row.get("market")
        or row.get("symbol")
        or "ONBEKEND"
    ).strip().upper()


def trade_reason(
    row: Dict[str, str],
) -> str:
    return str(
        row.get("reason")
        or "onbekend"
    ).strip() or "onbekend"


def build_spot_round_trips(
    rows: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """
    Koppelt iedere spotverkoop aan de bijbehorende koopkosten.

    De bot heeft normaal maximaal één positie per markt. De berekening kan
    ook een gedeeltelijke verkoop verwerken door de koopkosten evenredig
    over het verkochte aantal te verdelen.
    """
    open_buys: Dict[str, List[Dict[str, Any]]] = {}
    round_trips: List[Dict[str, Any]] = []

    for transaction_index, row in enumerate(rows):
        side = str(
            row.get("side", "")
        ).strip().upper()

        market = trade_market(row)

        if side == "BUY":
            base_amount = max(
                0.0,
                to_float(
                    row.get("base_amount"),
                    0.0,
                ),
            )

            buy_fee = max(
                0.0,
                to_float(
                    row.get("fees_quote"),
                    0.0,
                ),
            )

            open_buys.setdefault(
                market,
                [],
            ).append({
                "remaining_base": base_amount,
                "remaining_fee": buy_fee,
                "row": row,
            })

            continue

        if side != "SELL":
            continue

        sell_base = max(
            0.0,
            to_float(
                row.get("base_amount"),
                0.0,
            ),
        )

        sell_fee = max(
            0.0,
            to_float(
                row.get("fees_quote"),
                0.0,
            ),
        )

        remaining_sell = sell_base
        allocated_buy_fee = 0.0
        matched_buy_rows: List[Dict[str, str]] = []
        queue = open_buys.setdefault(
            market,
            [],
        )

        while (
            remaining_sell > 1e-12
            and queue
        ):
            lot = queue[0]
            lot_base = max(
                0.0,
                to_float(
                    lot.get("remaining_base"),
                    0.0,
                ),
            )
            lot_fee = max(
                0.0,
                to_float(
                    lot.get("remaining_fee"),
                    0.0,
                ),
            )

            if lot_base <= 1e-12:
                queue.pop(0)
                continue

            matched_base = min(
                remaining_sell,
                lot_base,
            )

            fraction = matched_base / lot_base
            fee_part = lot_fee * fraction

            allocated_buy_fee += fee_part
            matched_buy_rows.append(
                lot["row"]
            )

            lot["remaining_base"] = max(
                0.0,
                lot_base - matched_base,
            )
            lot["remaining_fee"] = max(
                0.0,
                lot_fee - fee_part,
            )

            remaining_sell = max(
                0.0,
                remaining_sell - matched_base,
            )

            if lot["remaining_base"] <= 1e-12:
                queue.pop(0)

        round_trips.append({
            "transaction_index": transaction_index,
            "sell_row": row,
            "matched_buy_rows": matched_buy_rows,
            "buy_fees_quote": allocated_buy_fee,
            "sell_fees_quote": sell_fee,
            "total_fees_quote": (
                allocated_buy_fee
                + sell_fee
            ),
            "unmatched_sell_base": remaining_sell,
        })

    return round_trips


def summarize_test_group(
    trades: List[Dict[str, Any]],
    key_name: str,
) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}

    for trade in trades:
        key = str(
            trade.get(key_name)
            or "onbekend"
        )

        groups.setdefault(
            key,
            [],
        ).append(trade)

    result: Dict[str, Dict[str, Any]] = {}

    for key, items in groups.items():
        pnl_values = [
            to_float(
                item.get("net_pnl_quote"),
                0.0,
            )
            for item in items
        ]

        wins = sum(
            1
            for value in pnl_values
            if value > 0
        )

        losses = sum(
            1
            for value in pnl_values
            if value < 0
        )

        total_pnl = sum(pnl_values)
        total_fees = sum(
            to_float(
                item.get("total_fees_quote"),
                0.0,
            )
            for item in items
        )

        result[key] = {
            "trades": len(items),
            "wins": wins,
            "losses": losses,
            "winrate_pct": round(
                100.0 * wins / len(items),
                2,
            ) if items else 0.0,
            "net_pnl_quote": round(
                total_pnl,
                8,
            ),
            "average_pnl_quote": round(
                total_pnl / len(items),
                8,
            ) if items else 0.0,
            "total_fees_quote": round(
                total_fees,
                8,
            ),
        }

    return result


def maximum_loss_streak(
    trades: List[Dict[str, Any]],
) -> int:
    current = 0
    maximum = 0

    for trade in trades:
        pnl = to_float(
            trade.get("net_pnl_quote"),
            0.0,
        )

        if pnl < 0:
            current += 1
            maximum = max(
                maximum,
                current,
            )
        else:
            current = 0

    return maximum


def build_test_report(
    require_complete: bool = True,
) -> Dict[str, Any]:
    """
    Bouwt het rapport van exact de nieuwe trades uit de nulmeting.

    require_complete=True wordt gebruikt bij de automatische teststop.
    Met False kan veilig een tussentijds voorbeeld worden gemaakt.
    """
    baseline = load_test_baseline()

    if baseline is None:
        raise RuntimeError(
            "Geen geldige testbaseline beschikbaar"
        )

    start_trades = int(
        to_float(
            baseline.get("start_spot_trades"),
            0,
        )
    )

    target_total = int(
        to_float(
            baseline.get("target_total_trades"),
            0,
        )
    )

    target_new = int(
        to_float(
            baseline.get("target_new_trades"),
            target_total - start_trades,
        )
    )

    if (
        start_trades < 0
        or target_new <= 0
        or target_total != start_trades + target_new
    ):
        raise RuntimeError(
            "Testbaseline bevat ongeldige tradegrenzen"
        )

    transaction_rows = load_trades()
    round_trips = build_spot_round_trips(
        transaction_rows
    )

    available_new = max(
        0,
        len(round_trips) - start_trades,
    )

    selected_round_trips = round_trips[
        start_trades:target_total
    ]

    if (
        require_complete
        and len(selected_round_trips) < target_new
    ):
        raise RuntimeError(
            "Transactiebestand bevat nog maar "
            f"{len(selected_round_trips)} van "
            f"{target_new} nieuwe gesloten trades"
        )

    selected_trades: List[Dict[str, Any]] = []

    for test_number, round_trip in enumerate(
        selected_round_trips,
        start=1,
    ):
        row = round_trip["sell_row"]
        pnl = trade_pnl(row)
        market = trade_market(row)
        reason = trade_reason(row)

        selected_trades.append({
            "test_trade_number": test_number,
            "absolute_trade_number": (
                start_trades
                + test_number
            ),
            "timestamp": str(
                row.get("ts")
                or ""
            ),
            "market": market,
            "reason": reason,
            "price": round(
                to_float(
                    row.get("price"),
                    0.0,
                ),
                12,
            ),
            "base_amount": round(
                to_float(
                    row.get("base_amount"),
                    0.0,
                ),
                12,
            ),
            "quote_amount": round(
                to_float(
                    row.get("quote_amount"),
                    0.0,
                ),
                8,
            ),
            "net_pnl_quote": round(
                pnl,
                8,
            ),
            "holding_time_min": round(
                to_float(
                    row.get("holding_time_min"),
                    0.0,
                ),
                2,
            ),
            "buy_fees_quote": round(
                to_float(
                    round_trip.get("buy_fees_quote"),
                    0.0,
                ),
                8,
            ),
            "sell_fees_quote": round(
                to_float(
                    round_trip.get("sell_fees_quote"),
                    0.0,
                ),
                8,
            ),
            "total_fees_quote": round(
                to_float(
                    round_trip.get("total_fees_quote"),
                    0.0,
                ),
                8,
            ),
            "buy_match_complete": (
                to_float(
                    round_trip.get("unmatched_sell_base"),
                    0.0,
                )
                <= 1e-10
            ),
            "dry_run": to_bool(
                row.get("dry_run"),
                True,
            ),
        })

    pnl_values = [
        to_float(
            trade.get("net_pnl_quote"),
            0.0,
        )
        for trade in selected_trades
    ]

    winning_values = [
        value
        for value in pnl_values
        if value > 0
    ]

    losing_values = [
        value
        for value in pnl_values
        if value < 0
    ]

    neutral_count = sum(
        1
        for value in pnl_values
        if value == 0
    )

    trade_count = len(selected_trades)
    wins = len(winning_values)
    losses = len(losing_values)
    total_pnl = sum(pnl_values)
    gross_profit = sum(winning_values)
    gross_loss = sum(losing_values)
    total_fees = sum(
        to_float(
            trade.get("total_fees_quote"),
            0.0,
        )
        for trade in selected_trades
    )

    holding_values = [
        to_float(
            trade.get("holding_time_min"),
            0.0,
        )
        for trade in selected_trades
    ]

    best_trade = max(
        selected_trades,
        key=lambda item: to_float(
            item.get("net_pnl_quote"),
            0.0,
        ),
        default=None,
    )

    worst_trade = min(
        selected_trades,
        key=lambda item: to_float(
            item.get("net_pnl_quote"),
            0.0,
        ),
        default=None,
    )

    fixed_stake = to_float(
        (baseline.get("settings") or {}).get(
            "fixed_stake_quote"
        ),
        0.0,
    )

    traded_stake_volume = (
        fixed_stake * trade_count
    )

    report = {
        "report_version": 1,
        "generated_at": now_utc().isoformat(),
        "test_started_at": baseline.get(
            "started_at"
        ),
        "test_complete": (
            trade_count >= target_new
        ),
        "start_spot_trades": start_trades,
        "target_new_trades": target_new,
        "target_total_trades": target_total,
        "available_new_closed_trades": available_new,
        "included_new_trades": trade_count,
        "remaining_new_trades": max(
            0,
            target_new - trade_count,
        ),
        "settings": baseline.get(
            "settings"
        ) or {},
        "summary": {
            "trades": trade_count,
            "wins": wins,
            "losses": losses,
            "neutral": neutral_count,
            "winrate_pct": round(
                100.0 * wins / trade_count,
                2,
            ) if trade_count else 0.0,
            "net_pnl_quote": round(
                total_pnl,
                8,
            ),
            "gross_profit_quote": round(
                gross_profit,
                8,
            ),
            "gross_loss_quote": round(
                gross_loss,
                8,
            ),
            "profit_factor": round(
                gross_profit / abs(gross_loss),
                4,
            ) if gross_loss < 0 else None,
            "average_pnl_quote": round(
                total_pnl / trade_count,
                8,
            ) if trade_count else 0.0,
            "average_win_quote": round(
                gross_profit / wins,
                8,
            ) if wins else 0.0,
            "average_loss_quote": round(
                gross_loss / losses,
                8,
            ) if losses else 0.0,
            "total_fees_quote": round(
                total_fees,
                8,
            ),
            "average_holding_time_min": round(
                sum(holding_values) / trade_count,
                2,
            ) if trade_count else 0.0,
            "maximum_loss_streak": maximum_loss_streak(
                selected_trades
            ),
            "stake_volume_quote": round(
                traded_stake_volume,
                2,
            ),
            "return_on_stake_volume_pct": round(
                100.0 * total_pnl / traded_stake_volume,
                4,
            ) if traded_stake_volume > 0 else None,
            "buy_fee_matches_complete": all(
                to_bool(
                    trade.get("buy_match_complete"),
                    False,
                )
                for trade in selected_trades
            ) if selected_trades else True,
        },
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "by_market": summarize_test_group(
            selected_trades,
            "market",
        ),
        "by_reason": summarize_test_group(
            selected_trades,
            "reason",
        ),
        "trades": selected_trades,
        "email_sent_at": None,
        "last_email_attempt_at": None,
    }

    return report


def build_short_round_trips(
    rows: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    """
    Koppelt iedere SHORT_CLOSE aan de oudste nog open SHORT_OPEN
    van dezelfde munt. De bot laat in deze test maximaal één short toe.
    """
    pending: Dict[
        str,
        List[Dict[str, str]],
    ] = {}

    result: List[
        Dict[str, Any]
    ] = []

    for row in rows:
        side = str(
            row.get("side")
            or ""
        ).upper()

        market = trade_market(
            row
        )

        if side == "SHORT_OPEN":
            pending.setdefault(
                market,
                [],
            ).append(
                row
            )

        elif side == "SHORT_CLOSE":
            opens = pending.get(
                market
            ) or []

            open_row = (
                opens.pop(0)
                if opens
                else {}
            )

            open_fee = to_float(
                open_row.get(
                    "fees_quote"
                ),
                0.0,
            )

            close_fee = to_float(
                row.get(
                    "fees_quote"
                ),
                0.0,
            )

            result.append({
                "open_row": open_row,
                "close_row": row,
                "open_fees_quote": open_fee,
                "close_fees_quote": close_fee,
                "total_fees_quote": (
                    open_fee
                    + close_fee
                ),
            })

    return result


def build_short_test_report(
    require_complete: bool = True,
    max_new_trades: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Bouwt uitsluitend het rapport van de nieuwe paper-shorts
    vanaf de automatisch vastgelegde shortnulmeting.
    """
    baseline = load_short_test_baseline()

    if baseline is None:
        raise RuntimeError(
            "Geen geldige paper-shortbaseline beschikbaar"
        )

    start = int(
        to_float(
            baseline.get(
                "start_short_trades"
            ),
            0,
        )
    )

    target_total = int(
        to_float(
            baseline.get(
                "target_total_short_trades"
            ),
            0,
        )
    )

    target_new = int(
        to_float(
            baseline.get(
                "target_new_trades"
            ),
            target_total - start,
        )
    )

    if (
        start < 0
        or target_new <= 0
        or target_total
        != start + target_new
    ):
        raise RuntimeError(
            "Paper-shortbaseline bevat "
            "ongeldige tradegrenzen"
        )

    round_trips = build_short_round_trips(
        load_trades()
    )

    available_new = max(
        0,
        len(round_trips) - start,
    )

    selected_round_trips = round_trips[
        start:target_total
    ]

    if max_new_trades is not None:
        cap = max(
            0,
            int(
                max_new_trades
            ),
        )

        selected_round_trips = (
            selected_round_trips[
                :cap
            ]
        )

    if (
        require_complete
        and len(selected_round_trips)
        < target_new
    ):
        raise RuntimeError(
            "Transactiebestand bevat nog maar "
            f"{len(selected_round_trips)} van "
            f"{target_new} nieuwe gesloten paper-shorts"
        )

    selected_trades: List[
        Dict[str, Any]
    ] = []

    for test_number, round_trip in enumerate(
        selected_round_trips,
        start=1,
    ):
        row = round_trip[
            "close_row"
        ]

        open_row = (
            round_trip.get(
                "open_row"
            )
            or {}
        )

        pnl = trade_pnl(
            row
        )

        selected_trades.append({
            "test_trade_number": test_number,
            "absolute_short_trade_number": (
                start
                + test_number
            ),
            "timestamp": str(
                row.get("ts")
                or ""
            ),
            "market": trade_market(
                row
            ),
            "entry_reason": trade_reason(
                open_row
            ),
            "close_reason": trade_reason(
                row
            ),
            "reason": trade_reason(
                row
            ),
            "entry_price": round(
                to_float(
                    open_row.get(
                        "price"
                    ),
                    0.0,
                ),
                12,
            ),
            "price": round(
                to_float(
                    row.get(
                        "price"
                    ),
                    0.0,
                ),
                12,
            ),
            "base_amount": round(
                to_float(
                    row.get(
                        "base_amount"
                    ),
                    0.0,
                ),
                12,
            ),
            "quote_amount": round(
                to_float(
                    row.get(
                        "quote_amount"
                    ),
                    0.0,
                ),
                8,
            ),
            "net_pnl_quote": round(
                pnl,
                8,
            ),
            "holding_time_min": round(
                to_float(
                    row.get(
                        "holding_time_min"
                    ),
                    0.0,
                ),
                2,
            ),
            "open_fees_quote": round(
                to_float(
                    round_trip.get(
                        "open_fees_quote"
                    ),
                    0.0,
                ),
                8,
            ),
            "close_fees_quote": round(
                to_float(
                    round_trip.get(
                        "close_fees_quote"
                    ),
                    0.0,
                ),
                8,
            ),
            "total_fees_quote": round(
                to_float(
                    round_trip.get(
                        "total_fees_quote"
                    ),
                    0.0,
                ),
                8,
            ),
            "open_match_complete": bool(
                round_trip.get(
                    "open_row"
                )
            ),
            "paper_only": True,
            "dry_run": True,
        })

    pnl_values = [
        to_float(
            trade.get(
                "net_pnl_quote"
            ),
            0.0,
        )
        for trade in selected_trades
    ]

    winning_values = [
        value
        for value in pnl_values
        if value > 0
    ]

    losing_values = [
        value
        for value in pnl_values
        if value < 0
    ]

    trade_count = len(
        selected_trades
    )

    wins = len(
        winning_values
    )

    losses = len(
        losing_values
    )

    neutral = sum(
        1
        for value in pnl_values
        if value == 0
    )

    total_pnl = sum(
        pnl_values
    )

    gross_profit = sum(
        winning_values
    )

    gross_loss = sum(
        losing_values
    )

    total_fees = sum(
        to_float(
            trade.get(
                "total_fees_quote"
            ),
            0.0,
        )
        for trade in selected_trades
    )

    holding_values = [
        to_float(
            trade.get(
                "holding_time_min"
            ),
            0.0,
        )
        for trade in selected_trades
    ]

    best_trade = max(
        selected_trades,
        key=lambda item: to_float(
            item.get(
                "net_pnl_quote"
            ),
            0.0,
        ),
        default=None,
    )

    worst_trade = min(
        selected_trades,
        key=lambda item: to_float(
            item.get(
                "net_pnl_quote"
            ),
            0.0,
        ),
        default=None,
    )

    settings = baseline.get(
        "settings"
    ) or {}

    margin = to_float(
        settings.get(
            "margin_per_trade"
        ),
        0.0,
    )

    margin_volume = (
        margin
        * trade_count
    )

    report = {
        "report_version": 1,
        "report_type": "paper_short_test",
        "generated_at": now_utc().isoformat(),
        "test_started_at": baseline.get(
            "started_at"
        ),
        "test_complete": (
            trade_count >= target_new
        ),
        "start_short_trades": start,
        "target_new_trades": target_new,
        "target_total_short_trades": target_total,
        "available_new_closed_shorts": available_new,
        "included_new_trades": trade_count,
        "remaining_new_trades": max(
            0,
            target_new - trade_count,
        ),
        "settings": settings,
        "summary": {
            "trades": trade_count,
            "wins": wins,
            "losses": losses,
            "neutral": neutral,
            "winrate_pct": round(
                100.0 * wins / trade_count,
                2,
            ) if trade_count else 0.0,
            "net_pnl_quote": round(
                total_pnl,
                8,
            ),
            "gross_profit_quote": round(
                gross_profit,
                8,
            ),
            "gross_loss_quote": round(
                gross_loss,
                8,
            ),
            "profit_factor": round(
                gross_profit / abs(
                    gross_loss
                ),
                4,
            ) if gross_loss < 0 else None,
            "average_pnl_quote": round(
                total_pnl / trade_count,
                8,
            ) if trade_count else 0.0,
            "average_win_quote": round(
                gross_profit / wins,
                8,
            ) if wins else 0.0,
            "average_loss_quote": round(
                gross_loss / losses,
                8,
            ) if losses else 0.0,
            "total_fees_quote": round(
                total_fees,
                8,
            ),
            "average_holding_time_min": round(
                sum(holding_values)
                / len(holding_values),
                2,
            ) if holding_values else 0.0,
            "maximum_loss_streak": maximum_loss_streak(
                selected_trades
            ),
            "margin_volume_quote": round(
                margin_volume,
                8,
            ),
            "return_on_margin_volume_pct": round(
                100.0
                * total_pnl
                / margin_volume,
                4,
            ) if margin_volume > 0 else None,
            "open_fee_matches_complete": all(
                bool(
                    trade.get(
                        "open_match_complete"
                    )
                )
                for trade in selected_trades
            ),
        },
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "by_market": summarize_test_group(
            selected_trades,
            "market",
        ),
        "by_entry_reason": summarize_test_group(
            selected_trades,
            "entry_reason",
        ),
        "by_reason": summarize_test_group(
            selected_trades,
            "reason",
        ),
        "trades": selected_trades,
    }

    return report


def short_interim_report_file(
    milestone: int,
) -> str:
    path = Path(
        SHORT_TEST_REPORT_FILE
    )

    return str(
        path.with_name(
            f"diamond_short_test_interim_{int(milestone)}.json"
        )
    )


def load_existing_short_interim_report(
    milestone: int,
) -> Dict[str, Any]:
    report = load_json(
        short_interim_report_file(
            milestone
        ),
        {},
    )

    if not isinstance(
        report,
        dict,
    ):
        return {}

    return report


def save_short_interim_report(
    milestone: int,
    report: Dict[str, Any],
) -> None:
    save_json_atomic(
        short_interim_report_file(
            milestone
        ),
        report,
    )


def build_short_interim_report(
    milestone: int,
) -> Dict[str, Any]:
    report = build_short_test_report(
        require_complete=False,
        max_new_trades=milestone,
    )

    included = int(
        to_float(
            report.get(
                "included_new_trades"
            ),
            0.0,
        )
    )

    if included < milestone:
        raise RuntimeError(
            "Transactiebestand bevat nog maar "
            f"{included} van {milestone} shorts "
            "voor het tussenrapport"
        )

    report[
        "report_type"
    ] = "paper_short_interim"

    report[
        "interim_milestone"
    ] = milestone

    report[
        "test_complete"
    ] = False

    report[
        "email_sent_at"
    ] = None

    report[
        "last_email_attempt_at"
    ] = None

    return report


def save_short_test_report(
    report: Dict[str, Any],
) -> None:
    save_json_atomic(
        SHORT_TEST_REPORT_FILE,
        report,
    )


def format_short_test_report(
    report: Dict[str, Any],
) -> str:
    summary = report.get(
        "summary"
    ) or {}

    settings = report.get(
        "settings"
    ) or {}

    best = report.get(
        "best_trade"
    ) or {}

    worst = report.get(
        "worst_trade"
    ) or {}

    milestone = int(
        to_float(
            report.get(
                "interim_milestone"
            ),
            0.0,
        )
    )

    report_title = (
        f"DIAMOND TRADER PAPER-SHORT TUSSENRAPPORT NA {milestone} SHORTS"
        if milestone > 0
        else "DIAMOND TRADER PAPER-SHORT EINDRAPPORT"
    )

    report_file = (
        short_interim_report_file(
            milestone
        )
        if milestone > 0
        else SHORT_TEST_REPORT_FILE
    )

    lines = [
        "=" * 60,
        report_title,
        now_local().strftime(
            "%d-%m-%Y %H:%M Nederlandse tijd"
        ),
        "=" * 60,
        "",
        "TESTGRENS",
        f"Start shorttrades       : {report.get('start_short_trades', 0)}",
        f"Nieuwe shorttrades      : {report.get('included_new_trades', 0)}/{report.get('target_new_trades', 0)}",
        f"Doel totaal             : {report.get('target_total_short_trades', 0)}",
        f"Test compleet           : {'JA' if report.get('test_complete') else 'NEE'}",
        "",
        "RESULTAAT",
        f"Winsttrades             : {summary.get('wins', 0)}",
        f"Verliestrades           : {summary.get('losses', 0)}",
        f"Neutrale trades         : {summary.get('neutral', 0)}",
        f"Winrate                 : {to_float(summary.get('winrate_pct'), 0.0):.1f}%",
        f"Netto PnL               : €{to_float(summary.get('net_pnl_quote'), 0.0):+.2f}",
        f"Profit factor           : {summary.get('profit_factor')}",
        f"Gemiddelde per short    : €{to_float(summary.get('average_pnl_quote'), 0.0):+.2f}",
        f"Totale handelskosten    : €{to_float(summary.get('total_fees_quote'), 0.0):.2f}",
        f"Max. verliesreeks       : {int(to_float(summary.get('maximum_loss_streak'), 0.0))}",
        f"Gem. looptijd           : {to_float(summary.get('average_holding_time_min'), 0.0):.1f} minuten",
        "",
        "BESTE EN SLECHTSTE SHORT",
        (
            f"Beste                   : {best.get('market', '-')} "
            f"€{to_float(best.get('net_pnl_quote'), 0.0):+.2f} "
            f"({best.get('reason', '-')})"
        ),
        (
            f"Slechtste               : {worst.get('market', '-')} "
            f"€{to_float(worst.get('net_pnl_quote'), 0.0):+.2f} "
            f"({worst.get('reason', '-')})"
        ),
        "",
        "RESULTAAT PER MUNT",
    ]

    by_market = report.get(
        "by_market"
    ) or {}

    for market in sorted(
        by_market
    ):
        item = by_market[
            market
        ]

        lines.append(
            f"{market:<10} trades={item.get('trades', 0):>2} | "
            f"winrate={to_float(item.get('winrate_pct'), 0.0):>5.1f}% | "
            f"pnl=€{to_float(item.get('net_pnl_quote'), 0.0):+7.2f}"
        )

    lines.extend([
        "",
        "RESULTAAT PER INSTAPREDEN",
    ])

    by_entry_reason = report.get(
        "by_entry_reason"
    ) or {}

    for reason in sorted(
        by_entry_reason
    ):
        item = by_entry_reason[
            reason
        ]

        lines.append(
            f"{reason:<32} trades={item.get('trades', 0):>2} | "
            f"winrate={to_float(item.get('winrate_pct'), 0.0):>5.1f}% | "
            f"pnl=€{to_float(item.get('net_pnl_quote'), 0.0):+7.2f}"
        )

    lines.extend([
        "",
        "RESULTAAT PER SLUITREDEN",
    ])

    by_reason = report.get(
        "by_reason"
    ) or {}

    for reason in sorted(
        by_reason
    ):
        item = by_reason[
            reason
        ]

        lines.append(
            f"{reason:<24} trades={item.get('trades', 0):>2} | "
            f"pnl=€{to_float(item.get('net_pnl_quote'), 0.0):+7.2f}"
        )

    lines.extend([
        "",
        "TESTINSTELLINGEN",
        f"Paper only              : {settings.get('paper_only')}",
        f"Margin per trade        : €{to_float(settings.get('margin_per_trade'), 0.0):.2f}",
        f"Hefboom                 : {to_float(settings.get('leverage'), 1.0):.1f}x",
        f"Maximaal open shorts    : {int(to_float(settings.get('max_open_positions'), 0.0))}",
        f"Strategieversie         : {settings.get('strategy_version')}",
        f"RSI verkoopminimum      : {to_float(settings.get('rsi_sell_min'), 0.0):.1f}",
        f"RSI verkoopmaximum      : {to_float(settings.get('rsi_sell_max'), 0.0):.1f}",
        f"Breakout terugkijk      : {int(to_float(settings.get('breakout_lookback_candles'), 0.0))} candles",
        f"Minimum nettowinst      : €{to_float(settings.get('min_profit_eur'), 0.0):.2f}",
        f"Minimum ATR             : {to_float(settings.get('min_atr_pct'), 0.0):.2f}%",
        f"Timeframe               : {settings.get('timeframe')}",
        "",
        f"JSON-rapport            : {report_file}",
        "=" * 60,
    ])

    return "\n".join(
        lines
    )


def load_existing_short_test_report() -> Dict[str, Any]:
    report = load_json(
        SHORT_TEST_REPORT_FILE,
        {},
    )

    if not isinstance(
        report,
        dict,
    ):
        return {}

    return report


def save_test_report(
    report: Dict[str, Any],
) -> None:
    save_json_atomic(
        TEST_REPORT_FILE,
        report,
    )


def format_test_report(
    report: Dict[str, Any],
) -> str:
    summary = report.get("summary") or {}
    settings = report.get("settings") or {}

    best = report.get("best_trade") or {}
    worst = report.get("worst_trade") or {}

    lines = [
        "=" * 60,
        "DIAMOND TRADER TESTRAPPORT",
        "=" * 60,
        f"Gegenereerd             : {report.get('generated_at')}",
        f"Test gestart            : {report.get('test_started_at')}",
        f"Trades opgenomen        : {summary.get('trades', 0)}",
        f"Test compleet           : {'JA' if report.get('test_complete') else 'NEE'}",
        "",
        "RESULTATEN",
        f"Winsttrades             : {summary.get('wins', 0)}",
        f"Verliestrades           : {summary.get('losses', 0)}",
        f"Neutrale trades         : {summary.get('neutral', 0)}",
        f"Winrate                 : {to_float(summary.get('winrate_pct'), 0.0):.1f}%",
        f"Nettoresultaat          : €{to_float(summary.get('net_pnl_quote'), 0.0):+.2f}",
        f"Gemiddelde per trade    : €{to_float(summary.get('average_pnl_quote'), 0.0):+.2f}",
        f"Gemiddelde winst        : €{to_float(summary.get('average_win_quote'), 0.0):+.2f}",
        f"Gemiddeld verlies       : €{to_float(summary.get('average_loss_quote'), 0.0):+.2f}",
        f"Totale handelskosten    : €{to_float(summary.get('total_fees_quote'), 0.0):.2f}",
        f"Max. verliesreeks       : {int(to_float(summary.get('maximum_loss_streak'), 0.0))}",
        f"Gem. looptijd           : {to_float(summary.get('average_holding_time_min'), 0.0):.1f} minuten",
        "",
        "BESTE EN SLECHTSTE TRADE",
        (
            f"Beste                   : {best.get('market', '-')} "
            f"€{to_float(best.get('net_pnl_quote'), 0.0):+.2f} "
            f"({best.get('reason', '-')})"
        ),
        (
            f"Slechtste               : {worst.get('market', '-')} "
            f"€{to_float(worst.get('net_pnl_quote'), 0.0):+.2f} "
            f"({worst.get('reason', '-')})"
        ),
        "",
        "RESULTAAT PER MUNT",
    ]

    by_market = report.get("by_market") or {}

    for market in sorted(by_market):
        item = by_market[market]
        lines.append(
            f"{market:<10} trades={item.get('trades', 0):>2} | "
            f"winrate={to_float(item.get('winrate_pct'), 0.0):>5.1f}% | "
            f"pnl=€{to_float(item.get('net_pnl_quote'), 0.0):+7.2f}"
        )

    lines.extend([
        "",
        "RESULTAAT PER VERKOOPREDEN",
    ])

    by_reason = report.get("by_reason") or {}

    for reason in sorted(by_reason):
        item = by_reason[reason]
        lines.append(
            f"{reason:<22} trades={item.get('trades', 0):>2} | "
            f"pnl=€{to_float(item.get('net_pnl_quote'), 0.0):+7.2f}"
        )

    lines.extend([
        "",
        "TESTINSTELLINGEN",
        f"Dry-run                 : {settings.get('dry_run')}",
        f"Inzet per trade         : €{to_float(settings.get('fixed_stake_quote'), 0.0):.2f}",
        f"Minimum ATR             : {to_float(settings.get('min_atr_pct'), 0.0):.2f}%",
        f"Timeframe               : {settings.get('timeframe')}",
        "",
        f"JSON-rapport            : {TEST_REPORT_FILE}",
        "=" * 60,
    ])

    return "\n".join(lines)


def load_existing_test_report() -> Dict[str, Any]:
    report = load_json(
        TEST_REPORT_FILE,
        {},
    )

    if not isinstance(report, dict):
        return {}

    return report


def email_retry_allowed(
    report: Dict[str, Any],
) -> bool:
    if report.get("email_sent_at"):
        return False

    raw_attempt = str(
        report.get("last_email_attempt_at")
        or ""
    ).strip()

    if not raw_attempt:
        return True

    try:
        attempted_at = datetime.fromisoformat(
            raw_attempt.replace(
                "Z",
                "+00:00",
            )
        )

        if attempted_at.tzinfo is None:
            attempted_at = attempted_at.replace(
                tzinfo=timezone.utc,
            )

        return (
            now_utc() - attempted_at
        ).total_seconds() >= 15 * 60

    except ValueError:
        return True


# ============================================================
# Bitvavo
# ============================================================

def create_exchange() -> ccxt.Exchange:
    exchange = ccxt.bitvavo({
        "apiKey": BITVAVO_API_KEY,
        "secret": BITVAVO_API_SECRET,
        "enableRateLimit": True,
        "options": {
            "fetchMarkets": {
                "types": ["spot"],
            },
        },
    })

    exchange.load_markets()

    return exchange


def fetch_free_eur(
    exchange: ccxt.Exchange,
) -> float:
    try:
        balance = exchange.fetch_balance()
        free = balance.get("free") or {}

        return to_float(
            free.get("EUR"),
            0.0,
        )

    except Exception as exc:
        LOG.warning(
            "Vrij EUR-saldo ophalen mislukt: %s",
            exc,
        )

        return 0.0


def fetch_btc_price(
    exchange: ccxt.Exchange,
) -> float:
    try:
        ticker = exchange.fetch_ticker(
            "BTC/EUR"
        )

        return to_float(
            ticker.get("last")
            or ticker.get("close"),
            0.0,
        )

    except Exception as exc:
        LOG.warning(
            "BTC-prijs ophalen mislukt: %s",
            exc,
        )

        return 0.0


def fetch_btc_24h_change(
    exchange: ccxt.Exchange,
) -> float:
    try:
        ticker = exchange.fetch_ticker(
            "BTC/EUR"
        )

        percentage = ticker.get("percentage")

        if percentage not in {
            None,
            "",
        }:
            return to_float(
                percentage,
                0.0,
            )

    except Exception as exc:
        LOG.warning(
            "BTC 24-uursverandering ophalen mislukt: %s",
            exc,
        )

    return 0.0


# ============================================================
# E-mail
# ============================================================

def send_email(
    subject: str,
    body: str,
    bypass_mute: bool = False,
) -> bool:
    # MAIL_MUTE_UNTIL_LIVE_V1
    # Algemene/status/shadow-mails blijven gedempt wanneer de flag bestaat.
    # Echte LIVE BUY/SELL-meldingen mogen gericht door deze mute heen.
    if (
        not bypass_mute
        and __import__("os").path.exists(
            "/var/data/diamond_mail_mute_until_live.flag"
        )
    ):
        return True

    if not GMAIL_PASS:
        LOG.warning(
            "GMAIL_APP_PASSWORD ontbreekt"
        )

        return False

    try:
        message = MIMEText(
            body,
            "plain",
            "utf-8",
        )

        message["Subject"] = subject
        message["From"] = GMAIL_USER
        message["To"] = GMAIL_USER

        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            timeout=30,
        ) as smtp:
            smtp.login(
                GMAIL_USER,
                GMAIL_PASS,
            )

            smtp.send_message(
                message
            )

        LOG.info(
            "E-mail verstuurd: %s",
            subject,
        )

        return True

    except Exception as exc:
        LOG.error(
            "E-mail versturen mislukt: %s",
            exc,
        )

        return False


# ============================================================
# Rapportage
# ============================================================

def position_value(
    position: Dict[str, Any],
) -> float:
    return to_float(
        position.get("quote_amount"),
        0.0,
    )


def parse_iso_datetime(
    value: Any,
) -> Optional[datetime]:
    """
    Leest een ISO-datum en zet die om naar Nederlandse tijd.
    """
    raw = str(
        value
        or ""
    ).strip()

    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(
            raw.replace(
                "Z",
                "+00:00",
            )
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc,
            )

        return parsed.astimezone(
            LOCAL_TZ
        )

    except ValueError:
        return None


def load_market_scanner_summary() -> Dict[str, Any]:
    """
    Leest uitsluitend de eigen bestanden van de Market Scanner.

    Er worden geen orders geplaatst en er worden geen bestanden gewijzigd.
    Een ontbrekend scannerbestand blokkeert de gewone botrapportage niet.
    """
    report = load_json(
        MARKET_SIGNALS_JSON_FILE,
        {},
    )

    scanner_state = load_json(
        MARKET_SCANNER_STATE_FILE,
        {},
    )

    if not report and not scanner_state:
        return {
            "available": False,
            "healthy": False,
            "status": "NIET BESCHIKBAAR",
            "version": "-",
            "mode": "-",
            "last_scan_text": "-",
            "age_minutes": None,
            "scan_count": 0,
            "analysed_count": 0,
            "signals_this_scan": 0,
            "new_signals_this_scan": 0,
            "total_unique_signals": 0,
            "open_positions_count": 0,
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "neutral": 0,
            "net_pnl_eur": 0.0,
            "total_fees_eur": 0.0,
            "orders_possible": False,
            "errors": 0,
            "top_signals": [],
        }

    generated_raw = (
        report.get("generated_at")
        or scanner_state.get("last_scan_at")
    )

    generated = parse_iso_datetime(
        generated_raw
    )

    age_minutes: Optional[float] = None

    if generated is not None:
        age_minutes = max(
            0.0,
            (
                now_local()
                - generated
            ).total_seconds()
            / 60.0,
        )

    mode = str(
        report.get("mode")
        or "-"
    )

    safety = (
        report.get("safety")
        or {}
    )

    shadow = (
        report.get("shadow")
        or {}
    )

    totals = (
        shadow.get("totals")
        or scanner_state.get("shadow_totals")
        or {}
    )

    raw_open_positions = shadow.get(
        "open_positions"
    )

    if isinstance(
        raw_open_positions,
        (list, dict),
    ):
        open_positions_count = len(
            raw_open_positions
        )
    else:
        state_positions = (
            scanner_state.get("open_positions")
            or {}
        )

        open_positions_count = (
            len(state_positions)
            if isinstance(
                state_positions,
                (list, dict),
            )
            else 0
        )

    signals = (
        report.get("signals")
        or []
    )

    if not isinstance(
        signals,
        list,
    ):
        signals = []

    top_signals: List[
        Dict[str, Any]
    ] = []

    for signal in signals[:3]:
        if not isinstance(
            signal,
            dict,
        ):
            continue

        economics = (
            signal.get("economics")
            or {}
        )

        rejection_reasons = (
            signal.get(
                "shadow_rejection_reasons"
            )
            or []
        )

        top_signals.append({
            "symbol": str(
                signal.get("symbol")
                or "-"
            ),
            "side": str(
                signal.get("side")
                or "-"
            ),
            "strategy": str(
                signal.get("strategy")
                or "-"
            ),
            "score": to_float(
                signal.get("score"),
                0.0,
            ),
            "reward_risk": to_float(
                economics.get("reward_risk"),
                0.0,
            ),
            "expected_profit_eur": to_float(
                economics.get(
                    "expected_profit_eur"
                ),
                0.0,
            ),
            "shadow_eligible": to_bool(
                signal.get("shadow_eligible"),
                False,
            ),
            "rejection_reason": str(
                rejection_reasons[0]
                if rejection_reasons
                else ""
            ),
        })

    errors = (
        report.get("errors")
        or []
    )

    error_count = (
        len(errors)
        if isinstance(
            errors,
            list,
        )
        else 0
    )

    orders_possible = to_bool(
        safety.get("orders_possible"),
        False,
    )

    healthy = (
        generated is not None
        and age_minutes is not None
        and age_minutes <= 35.0
        and mode == "VIRTUAL_SHADOW_TRADING"
        and not orders_possible
        and error_count == 0
    )

    if healthy:
        status = "ACTUEEL EN VEILIG"
    elif generated is None:
        status = "SCANTIJD ONBEKEND"
    elif (
        age_minutes is not None
        and age_minutes > 35.0
    ):
        status = "VEROUDERD"
    elif orders_possible:
        status = "VEILIGHEIDSWAARSCHUWING"
    elif error_count:
        status = (
            f"{error_count} ANALYSEFOUTEN"
        )
    else:
        status = "CONTROLEREN"

    return {
        "available": True,
        "healthy": healthy,
        "status": status,
        "version": str(
            report.get("version")
            or scanner_state.get("version")
            or "-"
        ),
        "mode": mode,
        "last_scan_text": (
            generated.strftime(
                "%d-%m-%Y %H:%M"
            )
            if generated is not None
            else "-"
        ),
        "age_minutes": age_minutes,
        "scan_count": int(
            to_float(
                scanner_state.get("scan_count"),
                0.0,
            )
        ),
        "analysed_count": int(
            to_float(
                report.get("analysed_count"),
                0.0,
            )
        ),
        "signals_this_scan": len(
            signals
        ),
        "new_signals_this_scan": int(
            to_float(
                report.get(
                    "new_signals_this_scan"
                ),
                0.0,
            )
        ),
        "total_unique_signals": int(
            to_float(
                report.get(
                    "total_unique_signals",
                    scanner_state.get(
                        "total_unique_signals"
                    ),
                ),
                0.0,
            )
        ),
        "open_positions_count": (
            open_positions_count
        ),
        "closed": int(
            to_float(
                totals.get("closed"),
                0.0,
            )
        ),
        "wins": int(
            to_float(
                totals.get("wins"),
                0.0,
            )
        ),
        "losses": int(
            to_float(
                totals.get("losses"),
                0.0,
            )
        ),
        "neutral": int(
            to_float(
                totals.get("neutral"),
                0.0,
            )
        ),
        "net_pnl_eur": to_float(
            totals.get("net_pnl_eur"),
            0.0,
        ),
        "total_fees_eur": to_float(
            totals.get("total_fees_eur"),
            0.0,
        ),
        "orders_possible": orders_possible,
        "errors": error_count,
        "top_signals": top_signals,
    }


def load_market_scanner_week_activity() -> Dict[str, Any]:
    """
    Berekent scanneractiviteit over de afgelopen zeven dagen.

    Signalen komen uit diamond_market_signals.csv.
    Gesloten schaduwtrades komen uit diamond_shadow_trades.csv.
    """
    cutoff = (
        now_local()
        - timedelta(days=7)
    )

    result: Dict[str, Any] = {
        "signals": 0,
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "neutral": 0,
        "net_pnl_eur": 0.0,
        "total_fees_eur": 0.0,
        "best_trade": None,
        "worst_trade": None,
    }

    signals_path = Path(
        MARKET_SIGNALS_CSV_FILE
    )

    if signals_path.exists():
        try:
            with signals_path.open(
                "r",
                encoding="utf-8",
                newline="",
            ) as file:
                for row in csv.DictReader(
                    file
                ):
                    detected = parse_iso_datetime(
                        row.get("detected_at")
                    )

                    if (
                        detected is not None
                        and detected >= cutoff
                    ):
                        result["signals"] += 1

        except Exception as exc:
            LOG.warning(
                "Scanner-signalen voor weekrapport lezen mislukt: %s",
                exc,
            )

    shadow_path = Path(
        SHADOW_TRADES_FILE
    )

    if not shadow_path.exists():
        return result

    try:
        with shadow_path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as file:
            for row in csv.DictReader(
                file
            ):
                closed_at = parse_iso_datetime(
                    row.get("closed_at")
                )

                if (
                    closed_at is None
                    or closed_at < cutoff
                ):
                    continue

                pnl = to_float(
                    row.get("net_pnl_eur"),
                    0.0,
                )

                fees = to_float(
                    row.get("total_fees_eur"),
                    0.0,
                )

                result["closed"] += 1
                result["net_pnl_eur"] += pnl
                result["total_fees_eur"] += fees

                if pnl > 0:
                    result["wins"] += 1
                elif pnl < 0:
                    result["losses"] += 1
                else:
                    result["neutral"] += 1

                trade = {
                    "symbol": str(
                        row.get("symbol")
                        or "-"
                    ),
                    "strategy": str(
                        row.get("strategy")
                        or "-"
                    ),
                    "side": str(
                        row.get("side")
                        or "-"
                    ),
                    "net_pnl_eur": pnl,
                    "exit_reason": str(
                        row.get("exit_reason")
                        or "-"
                    ),
                }

                best = result.get(
                    "best_trade"
                )

                worst = result.get(
                    "worst_trade"
                )

                if (
                    best is None
                    or pnl
                    > to_float(
                        best.get("net_pnl_eur"),
                        0.0,
                    )
                ):
                    result["best_trade"] = trade

                if (
                    worst is None
                    or pnl
                    < to_float(
                        worst.get("net_pnl_eur"),
                        0.0,
                    )
                ):
                    result["worst_trade"] = trade

    except Exception as exc:
        LOG.warning(
            "Schaduwtrades voor weekrapport lezen mislukt: %s",
            exc,
        )

    result["net_pnl_eur"] = round(
        to_float(
            result.get("net_pnl_eur"),
            0.0,
        ),
        8,
    )

    result["total_fees_eur"] = round(
        to_float(
            result.get("total_fees_eur"),
            0.0,
        ),
        8,
    )

    return result


def append_market_scanner_status(
    lines: List[str],
    scanner: Dict[str, Any],
) -> None:
    """
    Voegt het scannerblok toe aan een statusmail.
    """
    age_text = "-"

    if scanner.get(
        "age_minutes"
    ) is not None:
        age_text = (
            f"{to_float(scanner.get('age_minutes'), 0.0):.1f} minuten"
        )

    milestone_progress = get_shadow_milestone_progress(
        int(
            to_float(
                scanner.get(
                    "closed"
                ),
                0.0,
            )
        )
    )

    next_milestone = milestone_progress.get(
        "next_milestone"
    )

    if next_milestone is None:
        milestone_text = "20/20 bereikt"
    else:
        milestone_text = (
            f"{milestone_progress.get('closed_trades', 0)}/"
            f"{next_milestone} "
            f"(nog {milestone_progress.get('remaining', 0)})"
        )

    lines.extend([
        "",
        "MARKET SCANNER",
        f"Status                  : {scanner.get('status', '-')}",
        f"Versie                  : {scanner.get('version', '-')}",
        f"Modus                   : {scanner.get('mode', '-')}",
        f"Laatste scan            : {scanner.get('last_scan_text', '-')}",
        f"Leeftijd scan           : {age_text}",
        f"Scans totaal            : {int(to_float(scanner.get('scan_count'), 0.0))}",
        f"Markten laatste scan    : {int(to_float(scanner.get('analysed_count'), 0.0))}",
        f"Signalen laatste scan   : {int(to_float(scanner.get('signals_this_scan'), 0.0))}",
        f"Nieuwe signalen ronde   : {int(to_float(scanner.get('new_signals_this_scan'), 0.0))}",
        f"Unieke signalen totaal  : {int(to_float(scanner.get('total_unique_signals'), 0.0))}",
        f"Open schaduwposities    : {int(to_float(scanner.get('open_positions_count'), 0.0))}",
        f"Gesloten schaduwtrades  : {int(to_float(scanner.get('closed'), 0.0))}",
        f"Volgende mijlpaal       : {milestone_text}",
        (
            "Winst/verlies/neutraal  : "
            f"{int(to_float(scanner.get('wins'), 0.0))}/"
            f"{int(to_float(scanner.get('losses'), 0.0))}/"
            f"{int(to_float(scanner.get('neutral'), 0.0))}"
        ),
        f"Netto schaduwresultaat  : €{to_float(scanner.get('net_pnl_eur'), 0.0):+.4f}",
        f"Schaduwkosten           : €{to_float(scanner.get('total_fees_eur'), 0.0):.4f}",
        (
            "Echte scannerorders     : "
            + (
                "MOGELIJK - CONTROLEREN"
                if to_bool(
                    scanner.get("orders_possible"),
                    False,
                )
                else "ONMOGELIJK"
            )
        ),
        "",
        "BESTE SCANNERSIGNALEN",
    ])

    top_signals = (
        scanner.get("top_signals")
        or []
    )

    if not top_signals:
        lines.append(
            "Geen technisch signaal in de laatste scan"
        )

        return

    for signal in top_signals:
        if to_bool(
            signal.get("shadow_eligible"),
            False,
        ):
            status = "SCHADUWTRADE"
        else:
            reason = str(
                signal.get("rejection_reason")
                or "afgewezen"
            )

            status = (
                f"AFGEWEZEN: {reason}"
            )

        lines.append(
            f"{signal.get('symbol', '-')}: "
            f"{signal.get('side', '-')} "
            f"{signal.get('strategy', '-')} | "
            f"score={to_float(signal.get('score'), 0.0):.1f} | "
            f"RR={to_float(signal.get('reward_risk'), 0.0):.2f} | "
            f"verwacht=€{to_float(signal.get('expected_profit_eur'), 0.0):+.3f} | "
            f"{status}"
        )


def format_strategy_lab_profit_factor(
    value: Any,
) -> str:
    if value is None:
        return "n.v.t."

    return f"{to_float(value, 0.0):.2f}"


def strategy_lab_best_group(
    groups: Dict[str, Any],
) -> Dict[str, Any]:
    candidates: List[
        Dict[str, Any]
    ] = []

    for name, raw_summary in (
        groups
        or {}
    ).items():
        if not isinstance(
            raw_summary,
            dict,
        ):
            continue

        summary = dict(
            raw_summary
        )

        summary[
            "name"
        ] = str(
            name
        )

        candidates.append(
            summary
        )

    if not candidates:
        return {}

    return max(
        candidates,
        key=lambda item: (
            to_float(
                item.get(
                    "net_pnl_eur"
                ),
                0.0,
            ),
            to_float(
                item.get(
                    "profit_factor"
                ),
                0.0,
            ),
            to_float(
                item.get(
                    "winrate_pct"
                ),
                0.0,
            ),
            int(
                to_float(
                    item.get(
                        "trades"
                    ),
                    0.0,
                )
            ),
        ),
    )


def load_strategy_lab_email_summary() -> Dict[str, Any]:
    """
    Leest het alleen-lezen Strategy Lab-rapport voor status- en weekmails.

    Een ontbrekend, oud of ongeldig rapport blokkeert de gewone
    Diamond Trader-rapportage niet.
    """
    result: Dict[
        str,
        Any
    ] = {
        "available": False,
        "status": "NIET BESCHIKBAAR",
        "version": "-",
        "mode": "-",
        "generated_at": None,
        "generated_text": "-",
        "age_minutes": None,
        "safe": False,
        "errors": [],
        "data_status": "-",
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "neutral": 0,
        "winrate_pct": 0.0,
        "net_pnl_eur": 0.0,
        "total_fees_eur": 0.0,
        "profit_factor": None,
        "average_pnl_eur": 0.0,
        "average_return_pct": 0.0,
        "average_duration_minutes": 0.0,
        "maximum_loss_streak": 0,
        "stake_scenarios": {},
        "best_trade": {},
        "worst_trade": {},
        "best_strategy": {},
        "best_symbol": {},
        "best_side": {},
        "best_market_regime": {},
        "recommendations": [],
    }

    path = Path(
        STRATEGY_LAB_JSON_FILE
    )

    if not path.is_file():
        return result

    report = load_json(
        STRATEGY_LAB_JSON_FILE,
        {},
    )

    if not report:
        result[
            "status"
        ] = "ONLEESBAAR"

        return result

    generated = parse_iso_datetime(
        report.get(
            "generated_at"
        )
    )

    age_minutes: Optional[
        float
    ] = None

    if generated is not None:
        age_minutes = max(
            0.0,
            (
                now_local()
                - generated
            ).total_seconds()
            / 60.0,
        )

    safety = (
        report.get(
            "safety"
        )
        or {}
    )

    safe = (
        report.get(
            "mode"
        )
        == "READ_ONLY_STRATEGY_ANALYSIS"
        and safety.get(
            "orders_possible"
        )
        is False
        and safety.get(
            "exchange_connection_used"
        )
        is False
        and safety.get(
            "bot_state_modified"
        )
        is False
        and safety.get(
            "scanner_state_modified"
        )
        is False
        and safety.get(
            "settings_modified"
        )
        is False
        and safety.get(
            "automatic_strategy_changes"
        )
        is False
    )

    errors = (
        report.get(
            "errors"
        )
        or []
    )

    if not isinstance(
        errors,
        list,
    ):
        errors = [
            str(
                errors
            )
        ]

    if not safe:
        status = "VEILIGHEID CONTROLEREN"
    elif errors:
        status = "RAPPORTFOUTEN"
    elif age_minutes is None:
        status = "GEEN GELDIGE TIJD"
    elif age_minutes > 390.0:
        status = "VEROUDERD"
    else:
        status = "ACTUEEL EN VEILIG"

    shadow = (
        report.get(
            "shadow_trades"
        )
        or {}
    )

    groups = (
        report.get(
            "groups"
        )
        or {}
    )

    result.update({
        "available": True,
        "status": status,
        "version": report.get(
            "version"
        )
        or "-",
        "mode": report.get(
            "mode"
        )
        or "-",
        "generated_at": report.get(
            "generated_at"
        ),
        "generated_text": (
            generated.strftime(
                "%d-%m-%Y %H:%M"
            )
            if generated is not None
            else "-"
        ),
        "age_minutes": age_minutes,
        "safe": safe,
        "errors": errors,
        "data_status": shadow.get(
            "data_status"
        )
        or "-",
        "trades": int(
            to_float(
                shadow.get(
                    "trades"
                ),
                0.0,
            )
        ),
        "wins": int(
            to_float(
                shadow.get(
                    "wins"
                ),
                0.0,
            )
        ),
        "losses": int(
            to_float(
                shadow.get(
                    "losses"
                ),
                0.0,
            )
        ),
        "neutral": int(
            to_float(
                shadow.get(
                    "neutral"
                ),
                0.0,
            )
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
        "total_fees_eur": to_float(
            shadow.get(
                "total_fees_eur"
            ),
            0.0,
        ),
        "profit_factor": shadow.get(
            "profit_factor"
        ),
        "average_pnl_eur": to_float(
            shadow.get(
                "average_pnl_eur"
            ),
            0.0,
        ),
        "average_return_pct": to_float(
            shadow.get(
                "average_return_pct"
            ),
            0.0,
        ),
        "average_duration_minutes": to_float(
            shadow.get(
                "average_duration_minutes"
            ),
            0.0,
        ),
        "maximum_loss_streak": int(
            to_float(
                shadow.get(
                    "maximum_loss_streak"
                ),
                0.0,
            )
        ),
        "stake_scenarios": (
            shadow.get(
                "stake_scenarios"
            )
            or {}
        ),
        "best_trade": (
            shadow.get(
                "best_trade"
            )
            or {}
        ),
        "worst_trade": (
            shadow.get(
                "worst_trade"
            )
            or {}
        ),
        "best_strategy": strategy_lab_best_group(
            groups.get(
                "strategy"
            )
            or {}
        ),
        "best_symbol": strategy_lab_best_group(
            groups.get(
                "symbol"
            )
            or {}
        ),
        "best_side": strategy_lab_best_group(
            groups.get(
                "side"
            )
            or {}
        ),
        "best_market_regime": strategy_lab_best_group(
            groups.get(
                "market_regime"
            )
            or {}
        ),
        "recommendations": (
            report.get(
                "recommendations"
            )
            or []
        ),
    })

    return result


def strategy_lab_group_line(
    label: str,
    group: Dict[str, Any],
) -> str:
    if not group:
        return (
            f"{label:<24}: nog geen gesloten trades"
        )

    return (
        f"{label:<24}: "
        f"{group.get('name', '-')} | "
        f"trades={int(to_float(group.get('trades'), 0.0))} | "
        f"winrate={to_float(group.get('winrate_pct'), 0.0):.1f}% | "
        f"pnl=€{to_float(group.get('net_pnl_eur'), 0.0):+.4f}"
    )


def append_strategy_lab_status(
    lines: List[str],
    lab: Dict[str, Any],
) -> None:
    age_text = "-"

    if lab.get(
        "age_minutes"
    ) is not None:
        age_text = (
            f"{to_float(lab.get('age_minutes'), 0.0):.1f} minuten"
        )

    progress = get_shadow_milestone_progress(
        int(
            to_float(
                lab.get(
                    "trades"
                ),
                0.0,
            )
        )
    )

    next_milestone = progress.get(
        "next_milestone"
    )

    if next_milestone is None:
        milestone_text = "20/20 bereikt"
    else:
        milestone_text = (
            f"{progress.get('closed_trades', 0)}/"
            f"{next_milestone} "
            f"(nog {progress.get('remaining', 0)})"
        )

    lines.extend([
        "",
        "STRATEGY LAB",
        f"Status                  : {lab.get('status', '-')}",
        f"Versie                  : {lab.get('version', '-')}",
        f"Laatste verwerking      : {lab.get('generated_text', '-')}",
        f"Leeftijd rapport        : {age_text}",
        f"Datastatus              : {lab.get('data_status', '-')}",
        f"Gesloten schaduwtrades  : {int(to_float(lab.get('trades'), 0.0))}",
        f"Volgende mijlpaal       : {milestone_text}",
        (
            "Winst/verlies/neutraal  : "
            f"{int(to_float(lab.get('wins'), 0.0))}/"
            f"{int(to_float(lab.get('losses'), 0.0))}/"
            f"{int(to_float(lab.get('neutral'), 0.0))}"
        ),
        f"Winrate                 : {to_float(lab.get('winrate_pct'), 0.0):.2f}%",
        f"Nettoresultaat          : €{to_float(lab.get('net_pnl_eur'), 0.0):+.4f}",
        f"Totale kosten           : €{to_float(lab.get('total_fees_eur'), 0.0):.4f}",
        f"Profit factor           : {format_strategy_lab_profit_factor(lab.get('profit_factor'))}",
        f"Gemiddeld rendement     : {to_float(lab.get('average_return_pct'), 0.0):+.4f}%",
        strategy_lab_group_line(
            "Beste strategie",
            lab.get(
                "best_strategy"
            )
            or {},
        ),
        strategy_lab_group_line(
            "Beste munt",
            lab.get(
                "best_symbol"
            )
            or {},
        ),
    ])


def append_strategy_lab_weekly(
    lines: List[str],
    lab: Dict[str, Any],
) -> None:
    lines.extend([
        "",
        "STRATEGY LAB - ACTUELE TOTAALANALYSE",
        f"Labstatus               : {lab.get('status', '-')}",
        f"Datastatus              : {lab.get('data_status', '-')}",
        f"Gesloten schaduwtrades  : {int(to_float(lab.get('trades'), 0.0))}",
        (
            "Winst/verlies/neutraal  : "
            f"{int(to_float(lab.get('wins'), 0.0))}/"
            f"{int(to_float(lab.get('losses'), 0.0))}/"
            f"{int(to_float(lab.get('neutral'), 0.0))}"
        ),
        f"Winrate                 : {to_float(lab.get('winrate_pct'), 0.0):.2f}%",
        f"Nettoresultaat          : €{to_float(lab.get('net_pnl_eur'), 0.0):+.4f}",
        f"Totale kosten           : €{to_float(lab.get('total_fees_eur'), 0.0):.4f}",
        f"Profit factor           : {format_strategy_lab_profit_factor(lab.get('profit_factor'))}",
        f"Gemiddelde per trade    : €{to_float(lab.get('average_pnl_eur'), 0.0):+.4f}",
        f"Gemiddeld rendement     : {to_float(lab.get('average_return_pct'), 0.0):+.4f}%",
        f"Gemiddelde looptijd     : {to_float(lab.get('average_duration_minutes'), 0.0):.1f} minuten",
        f"Max. verliesreeks       : {int(to_float(lab.get('maximum_loss_streak'), 0.0))}",
        "",
        "STRATEGY LAB - BESTE GROEPEN",
        strategy_lab_group_line(
            "Beste strategie",
            lab.get(
                "best_strategy"
            )
            or {},
        ),
        strategy_lab_group_line(
            "Beste munt",
            lab.get(
                "best_symbol"
            )
            or {},
        ),
        strategy_lab_group_line(
            "Beste richting",
            lab.get(
                "best_side"
            )
            or {},
        ),
        strategy_lab_group_line(
            "Beste marktregime",
            lab.get(
                "best_market_regime"
            )
            or {},
        ),
        "",
        "STRATEGY LAB - INZETVERGELIJKING",
    ])

    scenarios = (
        lab.get(
            "stake_scenarios"
        )
        or {}
    )

    for stake in (
        120,
        125,
        130,
        135,
    ):
        lines.append(
            f"€{stake:>3} per trade          : "
            f"€{to_float(scenarios.get(str(stake)), 0.0):+.4f}"
        )

    recommendations = (
        lab.get(
            "recommendations"
        )
        or []
    )

    if recommendations:
        lines.extend([
            "",
            "STRATEGY LAB - BEOORDELING",
        ])

        for recommendation in recommendations[
            :3
        ]:
            lines.append(
                f"- {recommendation}"
            )


# ============================================================
# Diamond Readiness Gate
# ============================================================

def load_readiness_gate_summary() -> Dict[str, Any]:
    report = load_json(
        READINESS_GATE_JSON_FILE,
        {},
    )

    if not isinstance(
        report,
        dict,
    ):
        report = {}

    generated = parse_iso_datetime(
        report.get(
            "generated_at"
        )
    )

    age = (
        max(
            0.0,
            (
                now_local()
                - generated
            ).total_seconds()
            / 60.0,
        )
        if generated is not None
        else None
    )

    progress = (
        report.get(
            "test_progress"
        )
        or {}
    )

    return {
        "available": bool(
            report
        ),
        "version": report.get(
            "version"
        )
        or "-",
        "mode": report.get(
            "mode"
        )
        or "-",
        "generated_at": report.get(
            "generated_at"
        ),
        "generated_text": (
            generated.strftime(
                "%d-%m-%Y %H:%M"
            )
            if generated is not None
            else "-"
        ),
        "age_minutes": age,
        "status": report.get(
            "status"
        )
        or "NOG NIET BESCHIKBAAR",
        "phase": report.get(
            "phase"
        )
        or "-",
        "next_step": report.get(
            "next_step"
        )
        or "-",
        "test_completion_pct": to_float(
            report.get(
                "test_completion_pct"
            ),
            0.0,
        ),
        "critical_count": int(
            to_float(
                report.get(
                    "critical_failure_count"
                ),
                0.0,
            )
        ),
        "warning_count": int(
            to_float(
                report.get(
                    "warning_count"
                ),
                0.0,
            )
        ),
        "long": (
            progress.get(
                "long"
            )
            or {}
        ),
        "paper_short": (
            progress.get(
                "paper_short"
            )
            or {}
        ),
        "shadow": (
            progress.get(
                "shadow"
            )
            or {}
        ),
        "final_validation": (
            report.get(
                "final_validation"
            )
            or {}
        ),
        "live_approval": (
            report.get(
                "live_approval"
            )
            or {}
        ),
        "safety": (
            report.get(
                "safety"
            )
            or {}
        ),
    }


def readiness_gate_fingerprint(
    report: Dict[str, Any],
) -> str:
    critical = sorted(
        str(
            item.get(
                "name"
            )
            or ""
        )
        for item in (
            report.get(
                "critical_failures"
            )
            or []
        )
    )

    warnings = sorted(
        str(
            item.get(
                "name"
            )
            or ""
        )
        for item in (
            report.get(
                "warnings"
            )
            or []
        )
    )

    payload = {
        "status": report.get(
            "status"
        ),
        "phase": report.get(
            "phase"
        ),
        "critical": critical,
        "warnings": warnings,
    }

    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
        ).encode(
            "utf-8"
        )
    ).hexdigest()


def readiness_gate_retry_allowed(
    agent_state: Dict[str, Any],
    fingerprint: str,
) -> bool:
    if (
        agent_state.get(
            "readiness_gate_last_attempt_fingerprint"
        )
        != fingerprint
    ):
        return True

    attempted = parse_iso_datetime(
        agent_state.get(
            "readiness_gate_last_attempt_at"
        )
    )

    if attempted is None:
        return True

    return (
        now_local()
        - attempted
    ).total_seconds() >= (
        READINESS_GATE_RETRY_MINUTES
        * 60
    )


def format_readiness_gate_email(
    report: Dict[str, Any],
) -> str:
    text_path = Path(
        READINESS_GATE_TEXT_FILE
    )

    if text_path.is_file():
        try:
            return text_path.read_text(
                encoding="utf-8"
            )

        except Exception:
            pass

    progress = (
        report.get(
            "test_progress"
        )
        or {}
    )

    long_progress = (
        progress.get(
            "long"
        )
        or {}
    )

    short_progress = (
        progress.get(
            "paper_short"
        )
        or {}
    )

    shadow_progress = (
        progress.get(
            "shadow"
        )
        or {}
    )

    return "\n".join([
        "=" * 68,
        "DIAMOND TRADER READINESS GATE",
        "=" * 68,
        f"Status                  : {report.get('status') or '-'}",
        f"Fase                    : {report.get('phase') or '-'}",
        f"Testvoortgang           : {to_float(report.get('test_completion_pct'), 0.0):.1f}%",
        f"Longtest                : {int(to_float(long_progress.get('completed'), 0.0))}/20",
        f"Paper-shorttest         : {int(to_float(short_progress.get('completed'), 0.0))}/20",
        f"Schaduwtest             : {int(to_float(shadow_progress.get('completed'), 0.0))}/20",
        f"Kritieke problemen      : {int(to_float(report.get('critical_failure_count'), 0.0))}",
        f"Waarschuwingen          : {int(to_float(report.get('warning_count'), 0.0))}",
        f"Volgende stap           : {report.get('next_step') or '-'}",
        "",
        "Deze controle is uitsluitend adviserend en alleen-lezen.",
        "Diamond Trader wordt nooit automatisch live gezet.",
        "=" * 68,
    ])


def refresh_readiness_gate(
    agent_state: Dict[str, Any],
    force: bool = False,
) -> bool:
    last_run_ts = to_float(
        agent_state.get(
            "last_readiness_gate_ts"
        ),
        0.0,
    )

    if (
        not force
        and time.time()
        - last_run_ts
        < READINESS_GATE_CHECK_INTERVAL_SECONDS
    ):
        return False

    agent_state[
        "last_readiness_gate_ts"
    ] = time.time()

    agent_state[
        "readiness_gate_last_run_at"
    ] = now_local().isoformat()

    save_agent_state(
        agent_state
    )

    script = Path(
        READINESS_GATE_SCRIPT_FILE
    )

    if not script.is_file():
        error_text = (
            f"Readiness Gate-script ontbreekt: {script}"
        )

        agent_state[
            "readiness_gate_last_error"
        ] = error_text

        save_agent_state(
            agent_state
        )

        LOG.error(
            "%s",
            error_text,
        )

        return False

    try:
        result = subprocess.run(
            [
                "python3",
                str(
                    script
                ),
                "--no-print",
            ],
            cwd=str(
                script.parent
            ),
            capture_output=True,
            text=True,
            timeout=(
                READINESS_GATE_TIMEOUT_SECONDS
            ),
            check=False,
        )

    except subprocess.TimeoutExpired:
        error_text = (
            "Readiness Gate duurde langer dan "
            f"{READINESS_GATE_TIMEOUT_SECONDS} seconden"
        )

        agent_state[
            "readiness_gate_last_error"
        ] = error_text

        save_agent_state(
            agent_state
        )

        LOG.error(
            "%s",
            error_text,
        )

        return False

    except Exception as exc:
        error_text = (
            f"{type(exc).__name__}: {exc}"
        )

        agent_state[
            "readiness_gate_last_error"
        ] = error_text

        save_agent_state(
            agent_state
        )

        LOG.exception(
            "Readiness Gate uitvoeren mislukt: %s",
            exc,
        )

        return False

    if result.returncode != 0:
        output = (
            result.stderr
            or result.stdout
            or "geen fouttekst"
        ).strip()

        error_text = (
            f"exitcode {result.returncode}: "
            f"{output[-1000:]}"
        )

        agent_state[
            "readiness_gate_last_error"
        ] = error_text

        save_agent_state(
            agent_state
        )

        LOG.error(
            "Readiness Gate uitvoeren mislukt | %s",
            error_text,
        )

        return False

    report = load_json(
        READINESS_GATE_JSON_FILE,
        {},
    )

    if not report:
        error_text = (
            "Readiness Gate heeft geen leesbaar JSON-rapport gemaakt"
        )

        agent_state[
            "readiness_gate_last_error"
        ] = error_text

        save_agent_state(
            agent_state
        )

        LOG.error(
            "%s",
            error_text,
        )

        return False

    fingerprint = readiness_gate_fingerprint(
        report
    )

    agent_state[
        "readiness_gate_runs"
    ] = int(
        to_float(
            agent_state.get(
                "readiness_gate_runs"
            ),
            0.0,
        )
    ) + 1

    agent_state[
        "readiness_gate_last_status"
    ] = report.get(
        "status"
    ) or "-"

    agent_state[
        "readiness_gate_last_phase"
    ] = report.get(
        "phase"
    ) or "-"

    agent_state[
        "readiness_gate_last_next_step"
    ] = report.get(
        "next_step"
    ) or "-"

    agent_state[
        "readiness_gate_test_completion_pct"
    ] = to_float(
        report.get(
            "test_completion_pct"
        ),
        0.0,
    )

    agent_state[
        "readiness_gate_critical_count"
    ] = int(
        to_float(
            report.get(
                "critical_failure_count"
            ),
            0.0,
        )
    )

    agent_state[
        "readiness_gate_warning_count"
    ] = int(
        to_float(
            report.get(
                "warning_count"
            ),
            0.0,
        )
    )

    agent_state[
        "readiness_gate_last_error"
    ] = ""

    agent_state[
        "readiness_gate_current_fingerprint"
    ] = fingerprint

    save_agent_state(
        agent_state
    )

    LOG.info(
        "Readiness Gate ververst | status=%s | fase=%s | voortgang=%.1f%%",
        report.get(
            "status"
        ),
        report.get(
            "phase"
        ),
        to_float(
            report.get(
                "test_completion_pct"
            ),
            0.0,
        ),
    )

    if (
        agent_state.get(
            "readiness_gate_notified_fingerprint"
        )
        == fingerprint
    ):
        return True

    if not readiness_gate_retry_allowed(
        agent_state,
        fingerprint,
    ):
        return True

    agent_state[
        "readiness_gate_last_attempt_fingerprint"
    ] = fingerprint

    agent_state[
        "readiness_gate_last_attempt_at"
    ] = now_local().isoformat()

    save_agent_state(
        agent_state
    )

    sent = send_email(
        (
            "Diamond Readiness Gate - "
            f"{report.get('status') or 'ONBEKEND'}"
        ),
        format_readiness_gate_email(
            report
        ),
    )

    if not sent:
        return True

    agent_state[
        "readiness_gate_notified_fingerprint"
    ] = fingerprint

    agent_state[
        "readiness_gate_last_email_at"
    ] = now_local().isoformat()

    agent_state[
        "readiness_gate_email_count"
    ] = int(
        to_float(
            agent_state.get(
                "readiness_gate_email_count"
            ),
            0.0,
        )
    ) + 1

    save_agent_state(
        agent_state
    )

    return True


def append_readiness_gate_status(
    lines: List[str],
    readiness: Dict[str, Any],
) -> None:
    age_text = (
        f"{to_float(readiness.get('age_minutes'), 0.0):.1f} minuten"
        if readiness.get(
            "age_minutes"
        )
        is not None
        else "-"
    )

    long_progress = (
        readiness.get(
            "long"
        )
        or {}
    )

    short_progress = (
        readiness.get(
            "paper_short"
        )
        or {}
    )

    shadow_progress = (
        readiness.get(
            "shadow"
        )
        or {}
    )

    lines.extend([
        "",
        "READINESS GATE",
        f"Status                  : {readiness.get('status') or '-'}",
        f"Fase                    : {readiness.get('phase') or '-'}",
        f"Laatste controle        : {readiness.get('generated_text') or '-'}",
        f"Leeftijd rapport        : {age_text}",
        f"Totale testvoortgang    : {to_float(readiness.get('test_completion_pct'), 0.0):.1f}%",
        f"Longtest                : {int(to_float(long_progress.get('completed'), 0.0))}/20",
        f"Paper-shorttest         : {int(to_float(short_progress.get('completed'), 0.0))}/20",
        f"Schaduwtest             : {int(to_float(shadow_progress.get('completed'), 0.0))}/20",
        f"Kritieke problemen      : {int(to_float(readiness.get('critical_count'), 0.0))}",
        f"Waarschuwingen          : {int(to_float(readiness.get('warning_count'), 0.0))}",
        f"Volgende stap           : {readiness.get('next_step') or '-'}",
        "Automatisch live zetten: NEE",
    ])


def build_report(
    exchange: ccxt.Exchange,
) -> str:
    state = load_bot_state()
    control = load_control()
    trades = load_trades()
    scanner = load_market_scanner_summary()
    strategy_lab = load_strategy_lab_email_summary()
    scanner_watch = scanner_watch_summary_from_state()
    readiness = load_readiness_gate_summary()

    spot_sells = [
        row
        for row in trades
        if is_closed_spot_trade(row)
    ]

    short_closes = [
        row
        for row in trades
        if is_closed_short_trade(row)
    ]

    total_spot_pnl = sum(
        trade_pnl(row)
        for row in spot_sells
    )

    total_short_pnl = sum(
        trade_pnl(row)
        for row in short_closes
    )

    spot_wins = sum(
        1
        for row in spot_sells
        if trade_pnl(row) > 0
    )

    spot_losses = (
        len(spot_sells)
        - spot_wins
    )

    spot_winrate = (
        spot_wins
        / len(spot_sells)
        * 100.0
        if spot_sells
        else 0.0
    )

    positions = (
        state.get("positions")
        or {}
    )

    short_positions = (
        state.get("short_positions")
        or {}
    )

    invested = sum(
        position_value(position)
        for position in positions.values()
    )

    free_eur = fetch_free_eur(
        exchange
    )

    btc_change = fetch_btc_24h_change(
        exchange
    )

    day_pnl = get_day_pnl(
        trades
    )

    simulated_free = to_float(
        state.get("simulated_free_quote"),
        0.0,
    )

    paused = to_bool(
        control.get("paused"),
        False,
    )

    dry_run = config_dry_run()

    pause_reason = str(
        control.get("pause_reason")
        or "-"
    )

    lines = [
        "=" * 60,
        "DIAMOND BOT STATUSRAPPORT",
        now_local().strftime(
            "%d-%m-%Y %H:%M Nederlandse tijd"
        ),
        "=" * 60,
        "",
        "BOTSTATUS",
        f"Status                  : {'GEPAUZEERD' if paused else 'ACTIEF'}",
        f"Reden pauze             : {pause_reason}",
        f"Testmodus                : {'JA' if dry_run else 'NEE'}",
        "",
        "SALDO",
        f"Vrij EUR bij Bitvavo    : €{free_eur:.2f}",
        f"Gesimuleerd vrij saldo  : €{simulated_free:.2f}",
        f"Bot geïnvesteerd        : €{invested:.2f}",
        "",
        "MARKT",
        f"BTC laatste 24 uur      : {btc_change:+.2f}%",
        "",
        "VANDAAG",
        f"Dagresultaat            : €{day_pnl:+.2f}",
        "",
        "SPOTRESULTATEN",
        f"Open posities           : {len(positions)}",
        f"Gesloten trades         : {len(spot_sells)}",
        f"Winsttrades             : {spot_wins}",
        f"Verliestrades           : {spot_losses}",
        f"Winrate                 : {spot_winrate:.1f}%",
        f"Totale gerealiseerde PnL: €{total_spot_pnl:+.2f}",
        "",
        "PAPER SHORT",
        f"Open shortposities      : {len(short_positions)}",
        f"Gesloten shorts         : {len(short_closes)}",
        f"Totale short PnL        : €{total_short_pnl:+.2f}",
        "",
        "OPEN SPOTPOSITIES",
    ]

    if positions:
        for symbol, position in positions.items():
            entry_price = to_float(
                position.get("entry_price"),
                0.0,
            )

            amount = to_float(
                position.get("amount"),
                0.0,
            )

            quote_amount = position_value(
                position
            )

            lines.append(
                f"{symbol}: "
                f"€{quote_amount:.2f} | "
                f"aantal={amount:.8f} | "
                f"instap={entry_price:.8f}"
            )
    else:
        lines.append(
            "Geen open spotposities"
        )

    append_market_scanner_status(
        lines,
        scanner,
    )

    append_strategy_lab_status(
        lines,
        strategy_lab,
    )

    append_scanner_watch_status(
        lines,
        scanner_watch,
    )

    append_readiness_gate_status(
        lines,
        readiness,
    )

    lines.extend([
        "",
        "=" * 60,
        (
            "De bot draait in dry-run en plaatst geen echte orders."
            if dry_run
            else "WAARSCHUWING: de bot draait LIVE en kan echte orders plaatsen."
        ),
        "=" * 60,
    ])

    return "\n".join(lines)


def build_weekly_report(
    exchange: ccxt.Exchange,
) -> str:
    state = load_bot_state()
    control = load_control()
    trades = load_trades()
    scanner = load_market_scanner_summary()
    scanner_week = load_market_scanner_week_activity()
    strategy_lab = load_strategy_lab_email_summary()
    scanner_watch = scanner_watch_summary_from_state()
    readiness = load_readiness_gate_summary()

    week_trades = get_week_trades(
        trades
    )

    week_pnl = sum(
        trade_pnl(row)
        for row in week_trades
    )

    week_wins = sum(
        1
        for row in week_trades
        if trade_pnl(row) > 0
    )

    week_losses = (
        len(week_trades)
        - week_wins
    )

    week_winrate = (
        week_wins
        / len(week_trades)
        * 100.0
        if week_trades
        else 0.0
    )

    positions = (
        state.get("positions")
        or {}
    )

    invested = sum(
        position_value(position)
        for position in positions.values()
    )

    free_eur = fetch_free_eur(
        exchange
    )

    dry_run = config_dry_run()

    lines = [
        "=" * 60,
        "DIAMOND BOT WEEKRAPPORT",
        now_local().strftime(
            "%d-%m-%Y"
        ),
        "=" * 60,
        "",
        "AFGELOPEN ZEVEN DAGEN",
        f"Gesloten trades         : {len(week_trades)}",
        f"Winsttrades             : {week_wins}",
        f"Verliestrades           : {week_losses}",
        f"Winrate                 : {week_winrate:.1f}%",
        f"Weekresultaat           : €{week_pnl:+.2f}",
        "",
        "HUIDIGE STAND",
        f"Modus                   : {'DRY-RUN' if dry_run else 'LIVE'}",
        f"Vrij EUR bij Bitvavo    : €{free_eur:.2f}",
        f"Bot geïnvesteerd        : €{invested:.2f}",
        f"Open posities           : {len(positions)}",
        f"Botstatus               : {'GEPAUZEERD' if control.get('paused') else 'ACTIEF'}",
        f"Pauzereden              : {control.get('pause_reason') or '-'}",
        "",
        "MARKET SCANNER - AFGELOPEN ZEVEN DAGEN",
        f"Scannerstatus           : {scanner.get('status', '-')}",
        f"Signalen geregistreerd  : {int(to_float(scanner_week.get('signals'), 0.0))}",
        f"Gesloten schaduwtrades  : {int(to_float(scanner_week.get('closed'), 0.0))}",
        (
            "Winst/verlies/neutraal  : "
            f"{int(to_float(scanner_week.get('wins'), 0.0))}/"
            f"{int(to_float(scanner_week.get('losses'), 0.0))}/"
            f"{int(to_float(scanner_week.get('neutral'), 0.0))}"
        ),
        f"Netto weekresultaat     : €{to_float(scanner_week.get('net_pnl_eur'), 0.0):+.4f}",
        f"Handelskosten week      : €{to_float(scanner_week.get('total_fees_eur'), 0.0):.4f}",
        "",
        "MARKET SCANNER - TOTAAL",
        f"Scans totaal            : {int(to_float(scanner.get('scan_count'), 0.0))}",
        f"Unieke signalen totaal  : {int(to_float(scanner.get('total_unique_signals'), 0.0))}",
        f"Open schaduwposities    : {int(to_float(scanner.get('open_positions_count'), 0.0))}",
        f"Gesloten schaduwtrades  : {int(to_float(scanner.get('closed'), 0.0))}",
        f"Netto schaduwresultaat  : €{to_float(scanner.get('net_pnl_eur'), 0.0):+.4f}",
        f"Schaduwkosten totaal    : €{to_float(scanner.get('total_fees_eur'), 0.0):.4f}",
    ]

    best_shadow = (
        scanner_week.get("best_trade")
        or {}
    )

    worst_shadow = (
        scanner_week.get("worst_trade")
        or {}
    )

    if best_shadow:
        lines.extend([
            "",
            "BESTE EN SLECHTSTE SCHADUWTRADE DEZE WEEK",
            (
                f"Beste                   : "
                f"{best_shadow.get('symbol', '-')} "
                f"{best_shadow.get('side', '-')} "
                f"{best_shadow.get('strategy', '-')} | "
                f"€{to_float(best_shadow.get('net_pnl_eur'), 0.0):+.4f} | "
                f"{best_shadow.get('exit_reason', '-')}"
            ),
            (
                f"Slechtste               : "
                f"{worst_shadow.get('symbol', '-')} "
                f"{worst_shadow.get('side', '-')} "
                f"{worst_shadow.get('strategy', '-')} | "
                f"€{to_float(worst_shadow.get('net_pnl_eur'), 0.0):+.4f} | "
                f"{worst_shadow.get('exit_reason', '-')}"
            ),
        ])

    append_strategy_lab_weekly(
        lines,
        strategy_lab,
    )

    append_scanner_watch_status(
        lines,
        scanner_watch,
    )

    append_readiness_gate_status(
        lines,
        readiness,
    )

    lines.extend([
        "",
        "TESTBELEID",
        "De inzet blijft tijdens de lopende long-, short- en",
        "schaduwtests ongewijzigd, zodat resultaten vergelijkbaar blijven.",
        "Er worden geen instellingen automatisch aangepast.",
        "=" * 60,
    ])

    return "\n".join(lines)


# ============================================================
# Automatische dry-run teststop
# ============================================================

def check_short_test_interim_reports(
    exchange: ccxt.Exchange,
) -> bool:
    """
    Maakt en mailt exact één tussenrapport na 5 en 10
    gesloten paper-shorts. De test en open posities lopen door.
    """
    del exchange

    status = get_short_test_target_status()

    if not status.get(
        "enabled",
        False,
    ):
        return False

    baseline = (
        load_short_test_baseline()
        or {}
    )

    target_new = int(
        to_float(
            baseline.get(
                "target_new_trades"
            ),
            0.0,
        )
    )

    completed = int(
        to_float(
            status.get(
                "new_short_trades"
            ),
            0.0,
        )
    )

    handled_any = False

    for milestone in SHORT_TEST_INTERIM_MILESTONES:
        if (
            milestone >= target_new
            or completed < milestone
        ):
            continue

        handled_any = True

        existing_report = (
            load_existing_short_interim_report(
                milestone
            )
        )

        same_test = (
            existing_report.get(
                "test_started_at"
            )
            == baseline.get(
                "started_at"
            )
        )

        if (
            same_test
            and existing_report.get(
                "email_sent_at"
            )
        ):
            continue

        try:
            report = build_short_interim_report(
                milestone
            )

        except Exception as exc:
            LOG.warning(
                "Paper-shorttussenrapport %d nog niet compleet; "
                "volgende minuut opnieuw: %s",
                milestone,
                exc,
            )

            continue

        if same_test:
            report["email_sent_at"] = (
                existing_report.get(
                    "email_sent_at"
                )
            )

            report["last_email_attempt_at"] = (
                existing_report.get(
                    "last_email_attempt_at"
                )
            )

        save_short_interim_report(
            milestone,
            report,
        )

        summary = (
            report.get(
                "summary"
            )
            or {}
        )

        LOG.info(
            "Paper-shorttussenrapport opgeslagen | "
            "mijlpaal=%d | bestand=%s | pnl=%+.2f EUR",
            milestone,
            short_interim_report_file(
                milestone
            ),
            to_float(
                summary.get(
                    "net_pnl_quote"
                ),
                0.0,
            ),
        )

        if not email_retry_allowed(
            report
        ):
            continue

        report[
            "last_email_attempt_at"
        ] = now_utc().isoformat()

        save_short_interim_report(
            milestone,
            report,
        )

        email_ok = send_email(
            (
                "Diamond Trader PAPER-SHORT "
                f"TUSSENRAPPORT {milestone}/{target_new}"
            ),
            (
                f"{format_short_test_report(report)}\n\n"
                "De paper-shorttest loopt ongewijzigd door.\n"
                "Er zijn geen instellingen aangepast en er is "
                "geen veiligheidspauze geactiveerd."
            ),
        )

        if email_ok:
            report[
                "email_sent_at"
            ] = now_utc().isoformat()

            save_short_interim_report(
                milestone,
                report,
            )

    return handled_any


def check_short_test_target(
    exchange: ccxt.Exchange,
) -> bool:
    """
    Maakt en mailt het afzonderlijke paper-shortrapport zodra
    de shorttest het doel heeft bereikt.

    De bot zelf weigert daarna nieuwe paper-shorts. De longtest
    blijft volledig onafhankelijk doorlopen.
    """
    status = get_short_test_target_status()

    if not status.get(
        "enabled",
        False,
    ):
        return False

    if not status.get(
        "target_reached",
        False,
    ):
        return False

    baseline = (
        load_short_test_baseline()
        or {}
    )

    existing_report = (
        load_existing_short_test_report()
    )

    same_test = (
        existing_report.get(
            "test_started_at"
        )
        == baseline.get(
            "started_at"
        )
    )

    if (
        same_test
        and existing_report.get(
            "test_complete"
        )
        and existing_report.get(
            "email_sent_at"
        )
    ):
        return True

    try:
        report = build_short_test_report(
            require_complete=True,
        )

    except Exception as exc:
        LOG.warning(
            "Paper-shortrapport nog niet compleet; "
            "volgende minuut opnieuw: %s",
            exc,
        )

        return True

    if same_test:
        report["email_sent_at"] = (
            existing_report.get(
                "email_sent_at"
            )
        )

        report["last_email_attempt_at"] = (
            existing_report.get(
                "last_email_attempt_at"
            )
        )

    save_short_test_report(
        report
    )

    LOG.info(
        "Paper-shortrapport opgeslagen | "
        "bestand=%s | trades=%d | pnl=%+.2f EUR",
        SHORT_TEST_REPORT_FILE,
        int(
            to_float(
                (
                    report.get(
                        "summary"
                    )
                    or {}
                ).get(
                    "trades"
                ),
                0.0,
            )
        ),
        to_float(
            (
                report.get(
                    "summary"
                )
                or {}
            ).get(
                "net_pnl_quote"
            ),
            0.0,
        ),
    )

    if not email_retry_allowed(
        report
    ):
        return True

    report["last_email_attempt_at"] = (
        now_utc().isoformat()
    )

    save_short_test_report(
        report
    )

    email_ok = send_email(
        "Diamond Trader PAPER-SHORTTEST KLAAR",
        (
            f"{format_short_test_report(report)}\n\n"
            "Er worden geen nieuwe paper-shorts geopend.\n"
            "De longtest blijft afzonderlijk doorlopen."
        ),
    )

    if email_ok:
        report["email_sent_at"] = (
            now_utc().isoformat()
        )

        save_short_test_report(
            report
        )

    return True


def check_test_target(
    exchange: ccxt.Exchange,
) -> bool:
    """
    Pauzeert nieuwe aankopen en maakt het automatische eindrapport.

    Als de bot-state al op het doel staat maar de laatste CSV-regel nog wordt
    geschreven, blijft de agent het rapport iedere minuut opnieuw proberen.
    """
    status = get_test_target_status()

    if not status.get("enabled", False):
        return False

    # Deze automatische teststop hoort uitsluitend bij dry-run.
    if not status.get("dry_run", True):
        return False

    if not status.get("target_reached", False):
        return False

    target_total = int(
        status["target_total_trades"]
    )

    current_trades = int(
        status["current_trades"]
    )

    start_trades = int(
        status["start_trades"]
    )

    new_trades = int(
        status["new_trades"]
    )

    reached_at = now_utc().isoformat()
    pause_reason = (
        f"testdoel_{target_total}_trades_bereikt"
    )

    control = load_control()

    if not to_bool(
        control.get("paused"),
        False,
    ):
        save_control(
            paused=True,
            reason=pause_reason,
            extra_values={
                "pause_date": None,
                "pause_btc_price": None,
                "test_target_total_trades": target_total,
                "test_target_reached_at": reached_at,
            },
        )

        LOG.warning(
            "TESTDOEL BEREIKT | start=%d | huidig=%d | "
            "nieuwe_trades=%d | nieuwe aankopen gepauzeerd",
            start_trades,
            current_trades,
            new_trades,
        )

    else:
        # Bestaande veiligheidsreden behouden, maar het bereikte testdoel
        # wel vastleggen in hetzelfde controlebestand.
        changed = False

        if not control.get(
            "test_target_reached_at"
        ):
            control["test_target_reached_at"] = reached_at
            changed = True

        if control.get(
            "test_target_total_trades"
        ) != target_total:
            control["test_target_total_trades"] = target_total
            changed = True

        if changed:
            save_json_atomic(
                CONTROL_FILE,
                control,
            )

    existing_report = load_existing_test_report()
    same_test = (
        existing_report.get("test_started_at")
        == (
            load_test_baseline()
            or {}
        ).get("started_at")
    )

    if (
        same_test
        and existing_report.get("test_complete")
        and existing_report.get("email_sent_at")
    ):
        return True

    try:
        report = build_test_report(
            require_complete=True,
        )

    except Exception as exc:
        LOG.warning(
            "Testrapport nog niet compleet; volgende minuut opnieuw: %s",
            exc,
        )

        return True

    if same_test:
        report["email_sent_at"] = (
            existing_report.get(
                "email_sent_at"
            )
        )
        report["last_email_attempt_at"] = (
            existing_report.get(
                "last_email_attempt_at"
            )
        )

    save_test_report(
        report
    )

    LOG.info(
        "Testrapport opgeslagen | bestand=%s | trades=%d | pnl=%+.2f EUR",
        TEST_REPORT_FILE,
        int(
            to_float(
                (report.get("summary") or {}).get(
                    "trades"
                ),
                0.0,
            )
        ),
        to_float(
            (report.get("summary") or {}).get(
                "net_pnl_quote"
            ),
            0.0,
        ),
    )

    if not email_retry_allowed(
        report
    ):
        return True

    report["last_email_attempt_at"] = (
        now_utc().isoformat()
    )

    save_test_report(
        report
    )

    state = load_bot_state()
    open_spot = len(
        state.get("positions")
        or {}
    )
    open_shorts = len(
        state.get("short_positions")
        or {}
    )

    email_text = (
        f"{format_test_report(report)}\n\n"
        "TESTSTOP\n"
        f"Open spotposities       : {open_spot}\n"
        f"Open paper-shorts       : {open_shorts}\n\n"
        "Nieuwe aankopen en nieuwe paper-shorts zijn gepauzeerd.\n"
        "Eventuele open posities blijven bewaakt en kunnen normaal sluiten."
    )

    sent = send_email(
        "Diamond Trader TESTRAPPORT KLAAR",
        email_text,
    )

    if sent:
        report["email_sent_at"] = (
            now_utc().isoformat()
        )

        save_test_report(
            report
        )

    return True


# ============================================================
# Veiligheidsanalyse
# ============================================================

def get_total_capital(
    state: Dict[str, Any],
    exchange: ccxt.Exchange,
) -> float:
    simulated_free = to_float(
        state.get("simulated_free_quote"),
        0.0,
    )

    invested = sum(
        position_value(position)
        for position in (
            state.get("positions")
            or {}
        ).values()
    )

    if simulated_free > 0:
        return (
            simulated_free
            + invested
        )

    free_eur = fetch_free_eur(
        exchange
    )

    if free_eur > 0:
        return (
            free_eur
            + invested
        )

    return DEFAULT_TOTAL_CAPITAL


def analyze_and_act(
    exchange: ccxt.Exchange,
) -> None:
    state = load_bot_state()
    control = load_control()
    trades = load_trades()

    day_pnl = get_day_pnl(
        trades
    )

    btc_change = fetch_btc_24h_change(
        exchange
    )

    total_capital = get_total_capital(
        state,
        exchange,
    )

    max_day_loss = (
        total_capital
        * (
            MAX_DAY_LOSS_PCT
            / 100.0
        )
    )

    paused = to_bool(
        control.get("paused"),
        False,
    )

    reason = str(
        control.get("pause_reason")
        or ""
    )

    if not paused:
        if day_pnl <= -max_day_loss:
            save_control(
                paused=True,
                reason=(
                    f"dagverlies_"
                    f"{day_pnl:.2f}_EUR"
                ),
                extra_values={
                    "pause_date": (
                        now_local()
                        .date()
                        .isoformat()
                    ),
                    "pause_btc_price": None,
                },
            )

            LOG.warning(
                "Nieuwe aankopen gepauzeerd door dagverlies: %.2f EUR",
                day_pnl,
            )

            send_email(
                "Diamond Bot GEPAUZEERD - dagverlies",
                (
                    "Nieuwe aankopen zijn gepauzeerd.\n\n"
                    f"Dagverlies: €{day_pnl:.2f}\n"
                    f"Daglimiet: €{max_day_loss:.2f}\n\n"
                    f"{build_report(exchange)}"
                ),
            )

        elif btc_change <= BTC_DROP_LIMIT_PCT:
            btc_price = fetch_btc_price(
                exchange
            )

            save_control(
                paused=True,
                reason=(
                    f"btc_daling_"
                    f"{btc_change:.2f}_pct"
                ),
                extra_values={
                    "pause_btc_price": btc_price,
                    "pause_date": (
                        now_local()
                        .date()
                        .isoformat()
                    ),
                },
            )

            LOG.warning(
                "Nieuwe aankopen gepauzeerd door BTC-daling: %.2f%%",
                btc_change,
            )

            send_email(
                "Diamond Bot GEPAUZEERD - BTC-daling",
                (
                    "Nieuwe aankopen zijn gepauzeerd.\n\n"
                    f"BTC 24-uursverandering: {btc_change:.2f}%\n"
                    f"BTC-prijs bij pauze: €{btc_price:.2f}\n\n"
                    f"{build_report(exchange)}"
                ),
            )

    else:
        if reason.startswith(
            "dagverlies_"
        ):
            pause_date = str(
                control.get("pause_date")
                or ""
            )

            today = (
                now_local()
                .date()
                .isoformat()
            )

            if (
                pause_date
                and pause_date != today
            ):
                save_control(
                    paused=False,
                    reason="",
                    extra_values={
                        "pause_date": None,
                        "pause_btc_price": None,
                    },
                )

                LOG.info(
                    "Nieuwe aankopen hervat: nieuwe kalenderdag"
                )

                send_email(
                    "Diamond Bot HERVAT",
                    (
                        "Nieuwe aankopen zijn hervat na de dagverliespauze.\n\n"
                        f"{build_report(exchange)}"
                    ),
                )

        elif reason.startswith(
            "btc_daling_"
        ):
            pause_price = to_float(
                control.get(
                    "pause_btc_price"
                ),
                0.0,
            )

            current_price = fetch_btc_price(
                exchange
            )

            if (
                pause_price > 0
                and current_price > 0
            ):
                recovery = (
                    (
                        current_price
                        - pause_price
                    )
                    / pause_price
                    * 100.0
                )

                if recovery >= BTC_RECOVERY_PCT:
                    save_control(
                        paused=False,
                        reason="",
                        extra_values={
                            "pause_btc_price": None,
                            "pause_date": None,
                        },
                    )

                    LOG.info(
                        "Nieuwe aankopen hervat na BTC-herstel van %.2f%%",
                        recovery,
                    )

                    send_email(
                        "Diamond Bot HERVAT na BTC-herstel",
                        (
                            f"BTC is {recovery:.2f}% hersteld sinds de pauze.\n"
                            "Nieuwe aankopen zijn weer vrijgegeven.\n\n"
                            f"{build_report(exchange)}"
                        ),
                    )

    current_control = load_control()

    LOG.info(
        "Veiligheidsanalyse | "
        "dag_pnl=%+.2f EUR | "
        "btc_24u=%+.2f%% | "
        "limiet=-%.2f EUR | "
        "paused=%s",
        day_pnl,
        btc_change,
        max_day_loss,
        current_control.get(
            "paused",
            False,
        ),
    )


# ============================================================
# Market Scanner-schaduwmijlpaalrapporten
# ============================================================

def shadow_milestone_report_file(
    milestone: int,
    extension: str = "json",
) -> str:
    extension = str(
        extension
        or "json"
    ).strip().lower().lstrip(
        "."
    )

    if extension not in {
        "json",
        "txt",
    }:
        raise ValueError(
            "Alleen json en txt zijn geldige rapportformaten"
        )

    return str(
        Path(
            SHADOW_TRADES_FILE
        ).with_name(
            (
                "diamond_market_shadow_milestone_"
                f"{int(milestone)}.{extension}"
            )
        )
    )


def get_shadow_milestone_progress(
    closed_trades: int,
) -> Dict[str, Any]:
    closed = max(
        0,
        int(
            closed_trades
        ),
    )

    next_milestone: Optional[
        int
    ] = None

    for milestone in SHADOW_MILESTONE_REPORTS:
        if closed < milestone:
            next_milestone = milestone
            break

    return {
        "closed_trades": closed,
        "next_milestone": next_milestone,
        "remaining": (
            max(
                0,
                next_milestone - closed,
            )
            if next_milestone is not None
            else 0
        ),
        "all_milestones_reached": (
            next_milestone is None
        ),
    }


def shadow_trade_result(
    row: Dict[str, Any],
) -> float:
    return to_float(
        row.get(
            "net_pnl_eur"
        ),
        0.0,
    )


def shadow_group_summary(
    rows: List[Dict[str, Any]],
    key_name: str,
) -> Dict[str, Dict[str, Any]]:
    groups: Dict[
        str,
        List[
            Dict[str, Any]
        ]
    ] = {}

    for row in rows:
        key = str(
            row.get(
                key_name
            )
            or "ONBEKEND"
        )

        groups.setdefault(
            key,
            [],
        ).append(
            row
        )

    result: Dict[
        str,
        Dict[str, Any]
    ] = {}

    for key, items in groups.items():
        pnl_values = [
            shadow_trade_result(
                item
            )
            for item in items
        ]

        wins = sum(
            1
            for value in pnl_values
            if value > 0.000001
        )

        losses = sum(
            1
            for value in pnl_values
            if value < -0.000001
        )

        total = sum(
            pnl_values
        )

        fees = sum(
            max(
                0.0,
                to_float(
                    item.get(
                        "total_fees_eur"
                    ),
                    0.0,
                ),
            )
            for item in items
        )

        result[key] = {
            "trades": len(
                items
            ),
            "wins": wins,
            "losses": losses,
            "neutral": (
                len(
                    items
                )
                - wins
                - losses
            ),
            "winrate_pct": round(
                (
                    100.0
                    * wins
                    / len(
                        items
                    )
                )
                if items
                else 0.0,
                2,
            ),
            "net_pnl_eur": round(
                total,
                8,
            ),
            "average_pnl_eur": round(
                (
                    total
                    / len(
                        items
                    )
                )
                if items
                else 0.0,
                8,
            ),
            "total_fees_eur": round(
                fees,
                8,
            ),
        }

    return dict(
        sorted(
            result.items(),
            key=lambda item: (
                -int(
                    item[1].get(
                        "trades",
                        0,
                    )
                ),
                -to_float(
                    item[1].get(
                        "net_pnl_eur"
                    ),
                    0.0,
                ),
                item[0],
            ),
        )
    )


def shadow_maximum_loss_streak(
    rows: List[Dict[str, Any]],
) -> int:
    current = 0
    maximum = 0

    for row in rows:
        pnl = shadow_trade_result(
            row
        )

        if pnl < -0.000001:
            current += 1
            maximum = max(
                maximum,
                current,
            )
        else:
            current = 0

    return maximum


def public_shadow_trade(
    row: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not row:
        return None

    return {
        "opened_at": row.get(
            "opened_at"
        ),
        "closed_at": row.get(
            "closed_at"
        ),
        "symbol": row.get(
            "symbol"
        ),
        "strategy": row.get(
            "strategy"
        ),
        "side": row.get(
            "side"
        ),
        "market_regime": row.get(
            "market_regime"
        ),
        "signal_score": round(
            to_float(
                row.get(
                    "signal_score"
                ),
                0.0,
            ),
            2,
        ),
        "stake_eur": round(
            to_float(
                row.get(
                    "stake_eur"
                ),
                0.0,
            ),
            2,
        ),
        "exit_reason": row.get(
            "exit_reason"
        ),
        "gross_pnl_eur": round(
            to_float(
                row.get(
                    "gross_pnl_eur"
                ),
                0.0,
            ),
            8,
        ),
        "total_fees_eur": round(
            to_float(
                row.get(
                    "total_fees_eur"
                ),
                0.0,
            ),
            8,
        ),
        "net_pnl_eur": round(
            shadow_trade_result(
                row
            ),
            8,
        ),
        "return_pct": round(
            to_float(
                row.get(
                    "return_pct"
                ),
                0.0,
            ),
            8,
        ),
        "duration_minutes": round(
            to_float(
                row.get(
                    "duration_minutes"
                ),
                0.0,
            ),
            2,
        ),
    }


def build_shadow_milestone_report(
    milestone: int,
) -> Dict[str, Any]:
    target = int(
        milestone
    )

    if target not in SHADOW_MILESTONE_REPORTS:
        raise ValueError(
            f"Onbekende schaduwmijlpaal: {target}"
        )

    all_rows = load_shadow_closed_trades()

    if len(
        all_rows
    ) < target:
        raise RuntimeError(
            (
                "Nog maar "
                f"{len(all_rows)} van {target} "
                "gesloten schaduwtrades beschikbaar"
            )
        )

    selected = all_rows[
        :target
    ]

    pnl_values = [
        shadow_trade_result(
            row
        )
        for row in selected
    ]

    wins_values = [
        value
        for value in pnl_values
        if value > 0.000001
    ]

    losses_values = [
        value
        for value in pnl_values
        if value < -0.000001
    ]

    trade_count = len(
        selected
    )

    wins = len(
        wins_values
    )

    losses = len(
        losses_values
    )

    gross_profit = sum(
        wins_values
    )

    gross_loss = sum(
        losses_values
    )

    net_pnl = sum(
        pnl_values
    )

    total_fees = sum(
        max(
            0.0,
            to_float(
                row.get(
                    "total_fees_eur"
                ),
                0.0,
            ),
        )
        for row in selected
    )

    durations = [
        max(
            0.0,
            to_float(
                row.get(
                    "duration_minutes"
                ),
                0.0,
            ),
        )
        for row in selected
    ]

    returns = [
        to_float(
            row.get(
                "return_pct"
            ),
            0.0,
        )
        for row in selected
    ]

    scanner_state = load_json(
        MARKET_SCANNER_STATE_FILE,
        {},
    )

    scanner_started_at = (
        scanner_state.get(
            "started_at"
        )
        or (
            selected[0].get(
                "opened_at"
            )
            if selected
            else None
        )
    )

    best_trade = max(
        selected,
        key=shadow_trade_result,
        default=None,
    )

    worst_trade = min(
        selected,
        key=shadow_trade_result,
        default=None,
    )

    stake_scenarios = {
        str(
            int(
                stake
            )
        ): round(
            sum(
                stake
                * value
                / 100.0
                for value in returns
            ),
            8,
        )
        for stake in SHADOW_MILESTONE_STAKES
    }

    if gross_loss < -0.000001:
        profit_factor: Optional[
            float
        ] = (
            gross_profit
            / abs(
                gross_loss
            )
        )
    elif gross_profit > 0.000001:
        profit_factor = None
    else:
        profit_factor = 0.0

    report = {
        "report_version": 1,
        "report_type": "market_shadow_milestone",
        "generated_at": now_utc().isoformat(),
        "scanner_started_at": scanner_started_at,
        "milestone": target,
        "maximum_milestone": max(
            SHADOW_MILESTONE_REPORTS
        ),
        "included_closed_trades": trade_count,
        "summary": {
            "trades": trade_count,
            "wins": wins,
            "losses": losses,
            "neutral": (
                trade_count
                - wins
                - losses
            ),
            "winrate_pct": round(
                (
                    100.0
                    * wins
                    / trade_count
                )
                if trade_count
                else 0.0,
                2,
            ),
            "net_pnl_eur": round(
                net_pnl,
                8,
            ),
            "gross_profit_eur": round(
                gross_profit,
                8,
            ),
            "gross_loss_eur": round(
                gross_loss,
                8,
            ),
            "profit_factor": (
                round(
                    profit_factor,
                    4,
                )
                if profit_factor is not None
                else None
            ),
            "average_pnl_eur": round(
                (
                    net_pnl
                    / trade_count
                )
                if trade_count
                else 0.0,
                8,
            ),
            "average_win_eur": round(
                (
                    gross_profit
                    / wins
                )
                if wins
                else 0.0,
                8,
            ),
            "average_loss_eur": round(
                (
                    gross_loss
                    / losses
                )
                if losses
                else 0.0,
                8,
            ),
            "total_fees_eur": round(
                total_fees,
                8,
            ),
            "average_return_pct": round(
                (
                    sum(
                        returns
                    )
                    / trade_count
                )
                if trade_count
                else 0.0,
                8,
            ),
            "average_duration_minutes": round(
                (
                    sum(
                        durations
                    )
                    / trade_count
                )
                if trade_count
                else 0.0,
                2,
            ),
            "maximum_loss_streak": shadow_maximum_loss_streak(
                selected
            ),
            "stake_scenarios": stake_scenarios,
        },
        "best_trade": public_shadow_trade(
            best_trade
        ),
        "worst_trade": public_shadow_trade(
            worst_trade
        ),
        "by_strategy": shadow_group_summary(
            selected,
            "strategy",
        ),
        "by_symbol": shadow_group_summary(
            selected,
            "symbol",
        ),
        "by_side": shadow_group_summary(
            selected,
            "side",
        ),
        "by_market_regime": shadow_group_summary(
            selected,
            "market_regime",
        ),
        "by_exit_reason": shadow_group_summary(
            selected,
            "exit_reason",
        ),
        "trades": [
            public_shadow_trade(
                row
            )
            for row in selected
        ],
        "email_sent_at": None,
        "last_email_attempt_at": None,
    }

    return report


def format_shadow_milestone_report(
    report: Dict[str, Any],
) -> str:
    summary = (
        report.get(
            "summary"
        )
        or {}
    )

    best = (
        report.get(
            "best_trade"
        )
        or {}
    )

    worst = (
        report.get(
            "worst_trade"
        )
        or {}
    )

    milestone = int(
        to_float(
            report.get(
                "milestone"
            ),
            0.0,
        )
    )

    maximum = int(
        to_float(
            report.get(
                "maximum_milestone"
            ),
            20.0,
        )
    )

    lines = [
        "=" * 72,
        (
            "DIAMOND MARKET SCANNER "
            f"SCHADUWRAPPORT {milestone}/{maximum}"
        ),
        "=" * 72,
        f"Gegenereerd             : {report.get('generated_at')}",
        f"Scanner gestart         : {report.get('scanner_started_at')}",
        "",
        "RESULTATEN",
        f"Gesloten schaduwtrades  : {summary.get('trades', 0)}",
        f"Winst/verlies/neutraal  : "
        f"{summary.get('wins', 0)}/"
        f"{summary.get('losses', 0)}/"
        f"{summary.get('neutral', 0)}",
        f"Winrate                 : {to_float(summary.get('winrate_pct'), 0.0):.2f}%",
        f"Nettoresultaat          : €{to_float(summary.get('net_pnl_eur'), 0.0):+.4f}",
        f"Brutowinst              : €{to_float(summary.get('gross_profit_eur'), 0.0):+.4f}",
        f"Brutoverlies            : €{to_float(summary.get('gross_loss_eur'), 0.0):+.4f}",
        f"Totale kosten           : €{to_float(summary.get('total_fees_eur'), 0.0):.4f}",
        f"Profit factor           : {summary.get('profit_factor')}",
        f"Gemiddelde per trade    : €{to_float(summary.get('average_pnl_eur'), 0.0):+.4f}",
        f"Gemiddelde winst        : €{to_float(summary.get('average_win_eur'), 0.0):+.4f}",
        f"Gemiddeld verlies       : €{to_float(summary.get('average_loss_eur'), 0.0):+.4f}",
        f"Gemiddeld rendement     : {to_float(summary.get('average_return_pct'), 0.0):+.4f}%",
        f"Gemiddelde looptijd     : {to_float(summary.get('average_duration_minutes'), 0.0):.1f} minuten",
        f"Max. verliesreeks       : {int(to_float(summary.get('maximum_loss_streak'), 0.0))}",
        "",
        "BESTE EN SLECHTSTE SCHADUWTRADE",
        (
            f"Beste                   : "
            f"{best.get('symbol', '-')} "
            f"{best.get('side', '-')} "
            f"{best.get('strategy', '-')} | "
            f"€{to_float(best.get('net_pnl_eur'), 0.0):+.4f} | "
            f"{best.get('exit_reason', '-')}"
        ),
        (
            f"Slechtste               : "
            f"{worst.get('symbol', '-')} "
            f"{worst.get('side', '-')} "
            f"{worst.get('strategy', '-')} | "
            f"€{to_float(worst.get('net_pnl_eur'), 0.0):+.4f} | "
            f"{worst.get('exit_reason', '-')}"
        ),
        "",
        "INZETSCENARIO'S MET DEZELFDE RETURNS",
    ]

    scenarios = (
        summary.get(
            "stake_scenarios"
        )
        or {}
    )

    for stake in SHADOW_MILESTONE_STAKES:
        key = str(
            int(
                stake
            )
        )

        lines.append(
            f"€{stake:>6.0f} per trade       : "
            f"€{to_float(scenarios.get(key), 0.0):+.4f}"
        )

    for title, group_key in (
        (
            "RESULTAAT PER STRATEGIE",
            "by_strategy",
        ),
        (
            "RESULTAAT PER MUNT",
            "by_symbol",
        ),
        (
            "RESULTAAT PER RICHTING",
            "by_side",
        ),
        (
            "RESULTAAT PER MARKTREGIME",
            "by_market_regime",
        ),
        (
            "RESULTAAT PER SLUITREDEN",
            "by_exit_reason",
        ),
    ):
        lines.extend([
            "",
            title,
        ])

        groups = (
            report.get(
                group_key
            )
            or {}
        )

        if not groups:
            lines.append(
                "Geen gegevens"
            )

            continue

        for name, item in groups.items():
            lines.append(
                f"{name:<28} "
                f"trades={int(to_float(item.get('trades'), 0.0)):>2} | "
                f"winrate={to_float(item.get('winrate_pct'), 0.0):>6.1f}% | "
                f"pnl=€{to_float(item.get('net_pnl_eur'), 0.0):+8.4f}"
            )

    if milestone < 10:
        conclusion = (
            "Eerste indicatie. Nog geen instellingen aanpassen."
        )
    elif milestone < 20:
        conclusion = (
            "Voorlopige vergelijking. Wacht op 20 trades voor de eerste beoordeling."
        )
    else:
        conclusion = (
            "Eerste volledige evaluatiemijlpaal bereikt. Wijzigingen alleen na handmatige beoordeling."
        )

    lines.extend([
        "",
        "BEOORDELING",
        conclusion,
        "Geen instellingen zijn automatisch gewijzigd.",
        "De scanner blijft virtueel en plaatst geen echte orders.",
        "",
        f"JSON-rapport            : {shadow_milestone_report_file(milestone, 'json')}",
        f"Tekstrapport            : {shadow_milestone_report_file(milestone, 'txt')}",
        "=" * 72,
    ])

    return "\n".join(
        lines
    )


def load_existing_shadow_milestone_report(
    milestone: int,
) -> Dict[str, Any]:
    report = load_json(
        shadow_milestone_report_file(
            milestone,
            "json",
        ),
        {},
    )

    if not isinstance(
        report,
        dict,
    ):
        return {}

    return report


def save_shadow_milestone_report(
    milestone: int,
    report: Dict[str, Any],
) -> None:
    save_json_atomic(
        shadow_milestone_report_file(
            milestone,
            "json",
        ),
        report,
    )

    save_text_atomic(
        shadow_milestone_report_file(
            milestone,
            "txt",
        ),
        format_shadow_milestone_report(
            report
        )
        + "\n",
    )


def check_shadow_milestone_reports() -> int:
    """
    Maakt en mailt exact één rapport na 5, 10 en 20 gesloten
    Market Scanner-schaduwtrades.
    """
    closed_rows = load_shadow_closed_trades()
    closed_count = len(
        closed_rows
    )

    if closed_count < min(
        SHADOW_MILESTONE_REPORTS
    ):
        return 0

    scanner_state = load_json(
        MARKET_SCANNER_STATE_FILE,
        {},
    )

    scanner_started_at = (
        scanner_state.get(
            "started_at"
        )
        or (
            closed_rows[0].get(
                "opened_at"
            )
            if closed_rows
            else None
        )
    )

    sent_count = 0

    for milestone in SHADOW_MILESTONE_REPORTS:
        if closed_count < milestone:
            continue

        existing = load_existing_shadow_milestone_report(
            milestone
        )

        same_test = (
            existing.get(
                "scanner_started_at"
            )
            == scanner_started_at
        )

        if (
            same_test
            and existing.get(
                "email_sent_at"
            )
        ):
            continue

        try:
            report = build_shadow_milestone_report(
                milestone
            )

        except Exception as exc:
            LOG.warning(
                "Schaduwmijlpaalrapport %d nog niet beschikbaar: %s",
                milestone,
                exc,
            )

            continue

        if same_test:
            report[
                "email_sent_at"
            ] = existing.get(
                "email_sent_at"
            )

            report[
                "last_email_attempt_at"
            ] = existing.get(
                "last_email_attempt_at"
            )

        save_shadow_milestone_report(
            milestone,
            report,
        )

        if not email_retry_allowed(
            report
        ):
            continue

        report[
            "last_email_attempt_at"
        ] = now_utc().isoformat()

        save_shadow_milestone_report(
            milestone,
            report,
        )

        maximum = max(
            SHADOW_MILESTONE_REPORTS
        )

        subject_prefix = (
            "EINDRAPPORT"
            if milestone == maximum
            else "MIJLPAALRAPPORT"
        )

        sent = send_email(
            (
                "Diamond Scanner "
                f"{subject_prefix} {milestone}/{maximum}"
            ),
            format_shadow_milestone_report(
                report
            ),
        )

        if not sent:
            continue

        report[
            "email_sent_at"
        ] = now_utc().isoformat()

        save_shadow_milestone_report(
            milestone,
            report,
        )

        sent_count += 1

        LOG.info(
            "Schaduwmijlpaalrapport verstuurd | %d/%d | pnl=%+.4f EUR",
            milestone,
            maximum,
            to_float(
                (
                    report.get(
                        "summary"
                    )
                    or {}
                ).get(
                    "net_pnl_eur"
                ),
                0.0,
            ),
        )

    return sent_count


# ============================================================
# Dagelijkse back-up
# ============================================================

def backup_source_files() -> List[Dict[str, Any]]:
    """
    Bestanden die in iedere dagelijkse back-up worden opgenomen.

    Vereiste bestanden laten de back-up mislukken als ze ontbreken.
    Rapporten zijn optioneel omdat ze pas na het testdoel ontstaan.
    """
    return [
        {
            "source": CFG_FILE,
            "name": "config.yaml",
            "required": True,
        },
        {
            "source": STATE_FILE,
            "name": "diamond_state.json",
            "required": True,
        },
        {
            "source": TRADES_FILE,
            "name": "diamond_transactions.csv",
            "required": True,
        },
        {
            "source": CONTROL_FILE,
            "name": "diamond_control.json",
            "required": True,
        },
        {
            "source": AGENT_STATE_FILE,
            "name": "diamond_agent_state.json",
            "required": True,
        },
        {
            "source": TEST_BASELINE_FILE,
            "name": "diamond_test_baseline.json",
            "required": True,
        },
        {
            "source": SHORT_TEST_BASELINE_FILE,
            "name": "diamond_short_test_baseline.json",
            "required": True,
        },
        {
            "source": DIAG_STATS_FILE,
            "name": "diamond_diagnose_stats.json",
            "required": False,
        },
        {
            "source": SUPERVISOR_STATE_FILE,
            "name": "diamond_supervisor_state.json",
            "required": False,
        },
        {
            "source": MARKET_SIGNALS_JSON_FILE,
            "name": "diamond_market_signals.json",
            "required": False,
        },
        {
            "source": MARKET_SIGNALS_CSV_FILE,
            "name": "diamond_market_signals.csv",
            "required": False,
        },
        {
            "source": MARKET_SCANNER_STATE_FILE,
            "name": "diamond_market_scanner_state.json",
            "required": False,
        },
        {
            "source": SHADOW_TRADES_FILE,
            "name": "diamond_shadow_trades.csv",
            "required": False,
        },
        {
            "source": STRATEGY_LAB_JSON_FILE,
            "name": "diamond_strategy_lab.json",
            "required": False,
        },
        {
            "source": STRATEGY_LAB_TEXT_FILE,
            "name": "diamond_strategy_lab.txt",
            "required": False,
        },
        {
            "source": STRATEGY_LAB_GROUPS_FILE,
            "name": "diamond_strategy_lab_groups.csv",
            "required": False,
        },
        {
            "source": READINESS_GATE_JSON_FILE,
            "name": "diamond_readiness_gate.json",
            "required": False,
        },
        {
            "source": READINESS_GATE_TEXT_FILE,
            "name": "diamond_readiness_gate.txt",
            "required": False,
        },
        {
            "source": FINAL_VALIDATION_FILE,
            "name": "diamond_final_validation.json",
            "required": False,
        },
        {
            "source": LIVE_APPROVAL_FILE,
            "name": "diamond_live_approval.json",
            "required": False,
        },
        {
            "source": shadow_milestone_report_file(5, "json"),
            "name": "diamond_market_shadow_milestone_5.json",
            "required": False,
        },
        {
            "source": shadow_milestone_report_file(5, "txt"),
            "name": "diamond_market_shadow_milestone_5.txt",
            "required": False,
        },
        {
            "source": shadow_milestone_report_file(10, "json"),
            "name": "diamond_market_shadow_milestone_10.json",
            "required": False,
        },
        {
            "source": shadow_milestone_report_file(10, "txt"),
            "name": "diamond_market_shadow_milestone_10.txt",
            "required": False,
        },
        {
            "source": shadow_milestone_report_file(20, "json"),
            "name": "diamond_market_shadow_milestone_20.json",
            "required": False,
        },
        {
            "source": shadow_milestone_report_file(20, "txt"),
            "name": "diamond_market_shadow_milestone_20.txt",
            "required": False,
        },
        {
            "source": TEST_REPORT_FILE,
            "name": "diamond_test_report.json",
            "required": False,
        },
        {
            "source": SHORT_TEST_REPORT_FILE,
            "name": "diamond_short_test_report.json",
            "required": False,
        },
        {
            "source": short_interim_report_file(5),
            "name": "diamond_short_test_interim_5.json",
            "required": False,
        },
        {
            "source": short_interim_report_file(10),
            "name": "diamond_short_test_interim_10.json",
            "required": False,
        },
    ]


def sha256_file(
    path: Path,
) -> str:
    digest = hashlib.sha256()

    with path.open(
        "rb",
    ) as file:
        while True:
            chunk = file.read(
                1024 * 1024
            )

            if not chunk:
                break

            digest.update(
                chunk
            )

    return digest.hexdigest()


def validate_backup_copy(
    path: Path,
) -> None:
    """
    Controleert JSON, YAML en CSV nadat een bestand is gekopieerd.
    """
    suffix = path.suffix.lower()

    if suffix == ".json":
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            json.load(
                file
            )

    elif suffix in {
        ".yaml",
        ".yml",
    }:
        with path.open(
            "r",
            encoding="utf-8",
        ) as file:
            config = yaml.safe_load(
                file
            )

        if not isinstance(
            config,
            dict,
        ):
            raise ValueError(
                f"{path.name} bevat geen geldige YAML-dictionary"
            )

    elif suffix == ".csv":
        raw = path.read_bytes()

        if raw and not raw.endswith(
            b"\n"
        ):
            raise ValueError(
                f"{path.name} eindigt niet op een volledige CSV-regel"
            )

        with path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as file:
            reader = csv.reader(
                file
            )

            header = next(
                reader,
                [],
            )

        if not header:
            raise ValueError(
                f"{path.name} heeft geen CSV-header"
            )


def copy_backup_file(
    source: Path,
    target: Path,
) -> None:
    """
    Kopieert een bestand en probeert opnieuw als een gelijktijdige
    schrijfopdracht tijdelijk een onvolledige kopie oplevert.
    """
    last_error: Optional[Exception] = None

    for attempt in range(
        1,
        4,
    ):
        try:
            shutil.copy2(
                source,
                target,
            )

            validate_backup_copy(
                target
            )

            return

        except Exception as exc:
            last_error = exc

            try:
                target.unlink(
                    missing_ok=True
                )
            except OSError:
                pass

            if attempt < 3:
                time.sleep(
                    0.25 * attempt
                )

    raise RuntimeError(
        f"Back-upkopie mislukt voor {source}: {last_error}"
    )


def backup_directories() -> List[Path]:
    root = Path(
        BACKUP_DIR
    )

    if not root.exists():
        return []

    return sorted(
        [
            path
            for path in root.iterdir()
            if (
                path.is_dir()
                and not path.name.startswith(
                    "."
                )
                and len(path.name) >= 10
                and path.name[:10].count(
                    "-"
                ) == 2
            )
        ],
        key=lambda path: path.name,
    )


def backup_exists_for_date(
    date_key: str,
) -> bool:
    return any(
        path.name.startswith(
            f"{date_key}_"
        )
        for path in backup_directories()
    )


def prune_old_backups(
    current: datetime,
) -> List[str]:
    """
    Verwijdert uitsluitend back-upmappen die ouder zijn dan de
    ingestelde bewaartermijn.
    """
    removed: List[str] = []

    cutoff_date = (
        current.date()
        - timedelta(
            days=BACKUP_RETENTION_DAYS,
        )
    )

    for path in backup_directories():
        try:
            backup_date = datetime.strptime(
                path.name[:10],
                "%Y-%m-%d",
            ).date()

        except ValueError:
            continue

        if backup_date < cutoff_date:
            shutil.rmtree(
                path
            )

            removed.append(
                path.name
            )

    return removed


def create_daily_backup(
    current: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Maakt een controleerbare, atomair gepubliceerde dagelijkse back-up.
    """
    current = current or now_local()

    root = Path(
        BACKUP_DIR
    )

    root.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = current.strftime(
        "%Y-%m-%d_%H%M%S"
    )

    final_dir = root / timestamp

    if final_dir.exists():
        raise FileExistsError(
            f"Back-upmap bestaat al: {final_dir}"
        )

    temporary_dir = Path(
        tempfile.mkdtemp(
            prefix=".backup_tmp_",
            dir=str(root),
        )
    )

    copied_files: List[
        Dict[str, Any]
    ] = []

    skipped_files: List[
        Dict[str, Any]
    ] = []

    required_missing: List[str] = []

    try:
        for item in backup_source_files():
            source = Path(
                str(
                    item["source"]
                )
            )

            target = temporary_dir / str(
                item["name"]
            )

            required = bool(
                item["required"]
            )

            if not source.is_file():
                skipped_files.append({
                    "name": target.name,
                    "source": str(source),
                    "required": required,
                    "reason": "bestand_ontbreekt",
                })

                if required:
                    required_missing.append(
                        str(source)
                    )

                continue

            copy_backup_file(
                source,
                target,
            )

            copied_files.append({
                "name": target.name,
                "source": str(source),
                "size_bytes": target.stat().st_size,
                "sha256": sha256_file(
                    target
                ),
                "source_modified_at": datetime.fromtimestamp(
                    source.stat().st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
            })

        if required_missing:
            raise FileNotFoundError(
                "Vereiste back-upbestanden ontbreken: "
                + ", ".join(
                    required_missing
                )
            )

        total_bytes = sum(
            int(
                item["size_bytes"]
            )
            for item in copied_files
        )

        manifest = {
            "backup_version": 1,
            "status": "complete",
            "created_at": current.isoformat(),
            "created_at_utc": now_utc().isoformat(),
            "backup_name": timestamp,
            "retention_days": BACKUP_RETENTION_DAYS,
            "file_count": len(
                copied_files
            ),
            "total_bytes": total_bytes,
            "required_missing": [],
            "copied_files": copied_files,
            "skipped_files": skipped_files,
        }

        manifest_path = temporary_dir / "manifest.json"

        with manifest_path.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                manifest,
                file,
                indent=2,
                ensure_ascii=False,
            )

        validate_backup_copy(
            manifest_path
        )

        os.replace(
            temporary_dir,
            final_dir,
        )

        removed = prune_old_backups(
            current
        )

        manifest["removed_old_backups"] = removed

        return {
            "path": str(
                final_dir
            ),
            "created_at": current.isoformat(),
            "file_count": len(
                copied_files
            ),
            "total_bytes": total_bytes,
            "removed_old_backups": removed,
        }

    except Exception:
        shutil.rmtree(
            temporary_dir,
            ignore_errors=True,
        )

        raise


def handle_daily_backup(
    agent_state: Dict[str, Any],
) -> None:
    """
    Maakt bij een nieuwe installatie direct een back-up en daarna
    eenmaal per kalenderdag na 03:00 Nederlandse tijd.
    """
    current = now_local()
    date_key = current.strftime(
        "%Y-%m-%d"
    )

    already_exists = backup_exists_for_date(
        date_key
    )

    if already_exists:
        if agent_state.get(
            "last_backup_date"
        ) != date_key:
            agent_state[
                "last_backup_date"
            ] = date_key

            agent_state[
                "last_backup_status"
            ] = "complete"

            save_agent_state(
                agent_state
            )

        return

    existing_backups = backup_directories()

    due = (
        not existing_backups
        or (
            agent_state.get(
                "last_backup_date"
            ) != date_key
            and current.hour >= BACKUP_HOUR_LOCAL
        )
    )

    if not due:
        return

    try:
        result = create_daily_backup(
            current
        )

        agent_state[
            "last_backup_date"
        ] = date_key

        agent_state[
            "last_backup_at"
        ] = result[
            "created_at"
        ]

        agent_state[
            "last_backup_path"
        ] = result[
            "path"
        ]

        agent_state[
            "last_backup_status"
        ] = "complete"

        agent_state[
            "last_backup_file_count"
        ] = result[
            "file_count"
        ]

        agent_state[
            "last_backup_total_bytes"
        ] = result[
            "total_bytes"
        ]

        agent_state[
            "last_backup_error"
        ] = ""

        save_agent_state(
            agent_state
        )

        LOG.info(
            "DAGELIJKSE BACK-UP GESLAAGD | "
            "map=%s | bestanden=%d | grootte=%d bytes | "
            "oude_backups_verwijderd=%d",
            result["path"],
            result["file_count"],
            result["total_bytes"],
            len(
                result[
                    "removed_old_backups"
                ]
            ),
        )

    except Exception as exc:
        error_text = (
            f"{type(exc).__name__}: {exc}"
        )

        LOG.exception(
            "Dagelijkse back-up mislukt: %s",
            exc,
        )

        agent_state[
            "last_backup_status"
        ] = "failed"

        agent_state[
            "last_backup_error"
        ] = error_text

        notify = (
            agent_state.get(
                "last_backup_error_date"
            )
            != date_key
        )

        agent_state[
            "last_backup_error_date"
        ] = date_key

        save_agent_state(
            agent_state
        )

        if notify:
            send_email(
                "Diamond Trader BACK-UP MISLUKT",
                (
                    "De dagelijkse Diamond Trader-back-up is mislukt.\n\n"
                    f"Datum: {current.isoformat()}\n"
                    f"Fout: {error_text}\n"
                    f"Back-upmap: {BACKUP_DIR}\n\n"
                    "De bot blijft draaien. Controleer de Render-schijf "
                    "en voer healthcheck.sh uit."
                ),
            )



# ============================================================
# Market Scanner-schaduwtrade meldingen
# ============================================================

def shadow_event_key(
    event_type: str,
    row: Dict[str, Any],
) -> str:
    """
    Maakt een stabiele sleutel waarmee dubbele e-mails worden voorkomen.
    """
    if event_type == "open":
        timestamp = row.get(
            "opened_at"
        )
    else:
        timestamp = row.get(
            "closed_at"
        )

    raw = "|".join([
        event_type,
        str(
            timestamp
            or ""
        ),
        str(
            row.get("symbol")
            or ""
        ),
        str(
            row.get("strategy")
            or ""
        ),
        str(
            row.get("side")
            or ""
        ),
    ])

    return hashlib.sha256(
        raw.encode(
            "utf-8"
        )
    ).hexdigest()


def format_shadow_event_time(
    value: Any,
) -> str:
    parsed = parse_iso_datetime(
        value
    )

    if parsed is None:
        return str(
            value
            or "-"
        )

    return parsed.strftime(
        "%d-%m-%Y %H:%M Nederlandse tijd"
    )


def load_shadow_open_positions() -> List[Dict[str, Any]]:
    scanner_state = load_json(
        MARKET_SCANNER_STATE_FILE,
        {},
    )

    raw_positions = (
        scanner_state.get(
            "open_positions"
        )
        or {}
    )

    if isinstance(
        raw_positions,
        dict,
    ):
        positions = [
            value
            for value
            in raw_positions.values()
            if isinstance(
                value,
                dict,
            )
        ]

    elif isinstance(
        raw_positions,
        list,
    ):
        positions = [
            value
            for value
            in raw_positions
            if isinstance(
                value,
                dict,
            )
        ]

    else:
        positions = []

    return sorted(
        positions,
        key=lambda item: str(
            item.get(
                "opened_at"
            )
            or ""
        ),
    )


def load_shadow_closed_trades() -> List[Dict[str, str]]:
    path = Path(
        SHADOW_TRADES_FILE
    )

    if not path.is_file():
        return []

    try:
        with path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as file:
            rows = list(
                csv.DictReader(
                    file
                )
            )

        return sorted(
            rows,
            key=lambda item: str(
                item.get(
                    "closed_at"
                )
                or ""
            ),
        )

    except Exception as exc:
        LOG.warning(
            "Schaduwtransacties voor meldingen lezen mislukt: %s",
            exc,
        )

        return []


def strategy_lab_input_fingerprint() -> str:
    """
    Maakt een stabiele vingerafdruk van de actuele schaduwstand.

    De vingerafdruk verandert uitsluitend wanneer:
    - een schaduwpositie opent;
    - een schaduwpositie sluit;
    - de historische sluitlijst inhoudelijk verandert.
    """
    open_positions = load_shadow_open_positions()
    closed_trades = load_shadow_closed_trades()

    open_keys = sorted(
        shadow_event_key(
            "open",
            position,
        )
        for position in open_positions
    )

    closed_keys = [
        shadow_event_key(
            "close",
            trade,
        )
        for trade in closed_trades
    ]

    payload = {
        "open_keys": open_keys,
        "closed_count": len(
            closed_keys
        ),
        "last_closed_key": (
            closed_keys[-1]
            if closed_keys
            else ""
        ),
        "closed_keys_digest": hashlib.sha256(
            "|".join(
                closed_keys
            ).encode(
                "utf-8"
            )
        ).hexdigest(),
    }

    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(
            ",",
            ":",
        ),
    )

    return hashlib.sha256(
        serialized.encode(
            "utf-8"
        )
    ).hexdigest()


def strategy_lab_refresh_retry_allowed(
    agent_state: Dict[str, Any],
    fingerprint: str,
) -> bool:
    if (
        agent_state.get(
            "last_strategy_lab_attempt_fingerprint"
        )
        != fingerprint
    ):
        return True

    attempted = parse_iso_datetime(
        agent_state.get(
            "last_strategy_lab_refresh_attempt_at"
        )
    )

    if attempted is None:
        return True

    return (
        now_utc()
        - attempted
    ).total_seconds() >= (
        STRATEGY_LAB_REFRESH_RETRY_MINUTES
        * 60
    )


def refresh_strategy_lab_if_needed(
    agent_state: Dict[str, Any],
) -> bool:
    """
    Ververst Strategy Lab direct na een gewijzigde schaduwstand.

    De normale Strategy Lab-loop van zes uur blijft bestaan als vangnet.
    Bij een fout wordt niets aan scanner- of botbestanden gewijzigd en
    volgt na vijftien minuten een nieuwe poging.
    """
    fingerprint = (
        strategy_lab_input_fingerprint()
    )

    if (
        agent_state.get(
            "last_strategy_lab_input_fingerprint"
        )
        == fingerprint
        and agent_state.get(
            "last_strategy_lab_refresh_status"
        )
        == "complete"
    ):
        return False

    if not strategy_lab_refresh_retry_allowed(
        agent_state,
        fingerprint,
    ):
        return False

    attempt_at = now_utc().isoformat()

    agent_state[
        "last_strategy_lab_attempt_fingerprint"
    ] = fingerprint

    agent_state[
        "last_strategy_lab_refresh_attempt_at"
    ] = attempt_at

    save_agent_state(
        agent_state
    )

    script = Path(
        STRATEGY_LAB_SCRIPT_FILE
    )

    if not script.is_file():
        error_text = (
            f"Strategy Lab-script ontbreekt: {script}"
        )

        agent_state[
            "last_strategy_lab_refresh_status"
        ] = "failed"

        agent_state[
            "last_strategy_lab_refresh_error"
        ] = error_text

        save_agent_state(
            agent_state
        )

        LOG.error(
            "%s",
            error_text,
        )

        return False

    try:
        result = subprocess.run(
            [
                "python3",
                str(
                    script
                ),
                "--no-print",
            ],
            cwd=str(
                script.parent
            ),
            capture_output=True,
            text=True,
            timeout=(
                STRATEGY_LAB_REFRESH_TIMEOUT_SECONDS
            ),
            check=False,
        )

    except subprocess.TimeoutExpired:
        error_text = (
            "Strategy Lab directe verversing "
            f"duurde langer dan {STRATEGY_LAB_REFRESH_TIMEOUT_SECONDS} seconden"
        )

        agent_state[
            "last_strategy_lab_refresh_status"
        ] = "failed"

        agent_state[
            "last_strategy_lab_refresh_error"
        ] = error_text

        save_agent_state(
            agent_state
        )

        LOG.error(
            "%s",
            error_text,
        )

        return False

    except Exception as exc:
        error_text = (
            f"{type(exc).__name__}: {exc}"
        )

        agent_state[
            "last_strategy_lab_refresh_status"
        ] = "failed"

        agent_state[
            "last_strategy_lab_refresh_error"
        ] = error_text

        save_agent_state(
            agent_state
        )

        LOG.exception(
            "Strategy Lab directe verversing mislukt: %s",
            exc,
        )

        return False

    if result.returncode != 0:
        output = (
            result.stderr
            or result.stdout
            or "geen fouttekst"
        ).strip()

        error_text = (
            f"exitcode {result.returncode}: "
            f"{output[-1000:]}"
        )

        agent_state[
            "last_strategy_lab_refresh_status"
        ] = "failed"

        agent_state[
            "last_strategy_lab_refresh_error"
        ] = error_text

        save_agent_state(
            agent_state
        )

        LOG.error(
            "Strategy Lab directe verversing mislukt | %s",
            error_text,
        )

        return False

    agent_state[
        "last_strategy_lab_input_fingerprint"
    ] = fingerprint

    agent_state[
        "last_strategy_lab_refresh_at"
    ] = now_utc().isoformat()

    agent_state[
        "last_strategy_lab_refresh_status"
    ] = "complete"

    agent_state[
        "last_strategy_lab_refresh_error"
    ] = ""

    agent_state[
        "strategy_lab_refresh_count"
    ] = int(
        to_float(
            agent_state.get(
                "strategy_lab_refresh_count"
            ),
            0.0,
        )
    ) + 1

    save_agent_state(
        agent_state
    )

    log_output = (
        result.stdout
        or result.stderr
        or ""
    ).strip()

    if log_output:
        LOG.info(
            "Strategy Lab direct ververst | %s",
            log_output.splitlines()[-1][
                -500:
            ],
        )
    else:
        LOG.info(
            "Strategy Lab direct ververst"
        )

    return True


# ============================================================
# Market Scanner-bewaking
# ============================================================

def scanner_rejection_category(
    reason: Any,
) -> str:
    text = str(
        reason
        or ""
    ).strip()

    lowered = text.lower()

    if not lowered:
        return "onbekend"

    if "spread" in lowered:
        return "spread"

    if (
        "risico/winst" in lowered
        or "reward/risk" in lowered
        or "reward risk" in lowered
        or "rr " in lowered
    ):
        return "risico/winst"

    if (
        "verwachte winst" in lowered
        or "expected profit" in lowered
        or "min_profit" in lowered
    ):
        return "verwachte winst"

    if "atr" in lowered:
        return "ATR"

    if "rsi" in lowered:
        return "RSI"

    if "score" in lowered:
        return "score"

    if (
        "volume" in lowered
        or "liquiditeit" in lowered
        or "liquidity" in lowered
    ):
        return "liquiditeit"

    if (
        "trend" in lowered
        or "marktregime" in lowered
        or "market regime" in lowered
    ):
        return "trend/marktregime"

    return text[
        :80
    ]


def split_scanner_rejection_reasons(
    raw: Any,
) -> List[str]:
    text = str(
        raw
        or ""
    ).strip()

    if not text:
        return []

    return [
        item.strip()
        for item in text.split(
            "|"
        )
        if item.strip()
    ]


def load_scanner_signal_rows() -> List[Dict[str, str]]:
    path = Path(
        MARKET_SIGNALS_CSV_FILE
    )

    if not path.is_file():
        return []

    try:
        with path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as file:
            return list(
                csv.DictReader(
                    file
                )
            )

    except Exception as exc:
        LOG.warning(
            "Scanner-signalen voor bewaking lezen mislukt: %s",
            exc,
        )

        return []


def latest_shadow_suitable_time(
    signal_rows: List[Dict[str, str]],
    scanner_state: Dict[str, Any],
) -> Optional[datetime]:
    candidates: List[
        datetime
    ] = []

    started = parse_iso_datetime(
        scanner_state.get(
            "started_at"
        )
    )

    if started is not None:
        candidates.append(
            started
        )

    for row in signal_rows:
        if not to_bool(
            row.get(
                "shadow_eligible"
            ),
            False,
        ):
            continue

        detected = parse_iso_datetime(
            row.get(
                "detected_at"
            )
        )

        if detected is not None:
            candidates.append(
                detected
            )

    for position in load_shadow_open_positions():
        opened = parse_iso_datetime(
            position.get(
                "opened_at"
            )
        )

        if opened is not None:
            candidates.append(
                opened
            )

    for trade in load_shadow_closed_trades():
        opened = parse_iso_datetime(
            trade.get(
                "opened_at"
            )
        )

        if opened is not None:
            candidates.append(
                opened
            )

    if not candidates:
        return None

    return max(
        candidates
    )


def analyse_scanner_watch() -> Dict[str, Any]:
    now_value = now_local()
    cutoff = (
        now_value
        - timedelta(
            hours=(
                SCANNER_WATCH_ANALYSIS_HOURS
            )
        )
    )

    scanner = load_market_scanner_summary()
    scanner_state = load_json(
        MARKET_SCANNER_STATE_FILE,
        {},
    )

    rows = load_scanner_signal_rows()

    signals_window = 0
    eligible_window = 0
    rejected_window = 0

    category_counts: Dict[
        str,
        int
    ] = {}

    category_examples: Dict[
        str,
        List[str]
    ] = {}

    for row in rows:
        detected = parse_iso_datetime(
            row.get(
                "detected_at"
            )
        )

        if (
            detected is None
            or detected < cutoff
        ):
            continue

        signals_window += 1

        eligible = to_bool(
            row.get(
                "shadow_eligible"
            ),
            False,
        )

        if eligible:
            eligible_window += 1
            continue

        rejected_window += 1

        raw_reasons = (
            row.get(
                "shadow_rejection_reasons"
            )
            or row.get(
                "rejection_reasons"
            )
            or ""
        )

        reasons = split_scanner_rejection_reasons(
            raw_reasons
        )

        categories_for_signal = set()

        for reason in reasons:
            category = scanner_rejection_category(
                reason
            )

            categories_for_signal.add(
                category
            )

            examples = category_examples.setdefault(
                category,
                [],
            )

            if (
                reason not in examples
                and len(
                    examples
                ) < 3
            ):
                examples.append(
                    reason
                )

        for category in categories_for_signal:
            category_counts[
                category
            ] = (
                category_counts.get(
                    category,
                    0,
                )
                + 1
            )

    dominant_filter = ""
    dominant_count = 0
    dominant_share_pct = 0.0
    dominant_examples: List[
        str
    ] = []

    if category_counts:
        dominant_filter, dominant_count = max(
            category_counts.items(),
            key=lambda item: (
                item[1],
                item[0],
            ),
        )

        dominant_share_pct = (
            100.0
            * dominant_count
            / rejected_window
            if rejected_window
            else 0.0
        )

        dominant_examples = (
            category_examples.get(
                dominant_filter,
                [],
            )
        )

    started_at = parse_iso_datetime(
        scanner_state.get(
            "started_at"
        )
    )

    running_hours = (
        max(
            0.0,
            (
                now_value
                - started_at
            ).total_seconds()
            / 3600.0,
        )
        if started_at is not None
        else 0.0
    )

    last_suitable = latest_shadow_suitable_time(
        rows,
        scanner_state,
    )

    hours_without_suitable = (
        max(
            0.0,
            (
                now_value
                - last_suitable
            ).total_seconds()
            / 3600.0,
        )
        if last_suitable is not None
        else running_hours
    )

    open_positions = int(
        to_float(
            scanner.get(
                "open_positions_count"
            ),
            0.0,
        )
    )

    scanner_healthy = bool(
        scanner.get(
            "healthy"
        )
    )

    stagnation = (
        scanner_healthy
        and open_positions == 0
        and running_hours
        >= SCANNER_WATCH_STAGNATION_HOURS
        and hours_without_suitable
        >= SCANNER_WATCH_STAGNATION_HOURS
    )

    dominant_rejection = (
        scanner_healthy
        and rejected_window
        >= SCANNER_WATCH_MIN_REJECTED_SIGNALS
        and dominant_count
        >= SCANNER_WATCH_DOMINANT_MIN_COUNT
        and dominant_share_pct
        >= SCANNER_WATCH_DOMINANT_SHARE_PCT
    )

    conditions: List[
        str
    ] = []

    if stagnation:
        conditions.append(
            "LANG GEEN GESCHIKTE SCHADUWTRADE"
        )

    if dominant_rejection:
        conditions.append(
            (
                "DOMINANT AFWIJZINGSFILTER: "
                f"{dominant_filter}"
            )
        )

    if not scanner.get(
        "available"
    ):
        status = "SCANNER NIET BESCHIKBAAR"
    elif not scanner_healthy:
        status = "SCANNER NIET ACTUEEL"
    elif conditions:
        status = "WAARSCHUWING"
    elif rejected_window < (
        SCANNER_WATCH_MIN_REJECTED_SIGNALS
    ):
        status = "NORMAAL - NOG WEINIG AFWIJZINGSDATA"
    else:
        status = "NORMAAL"

    fingerprint_source = "|".join(
        conditions
    )

    fingerprint = (
        hashlib.sha256(
            fingerprint_source.encode(
                "utf-8"
            )
        ).hexdigest()
        if fingerprint_source
        else ""
    )

    return {
        "checked_at": now_value.isoformat(),
        "status": status,
        "scanner_healthy": scanner_healthy,
        "scanner_status": scanner.get(
            "status"
        )
        or "-",
        "running_hours": round(
            running_hours,
            2,
        ),
        "last_suitable_at": (
            last_suitable.isoformat()
            if last_suitable is not None
            else ""
        ),
        "hours_without_suitable": round(
            hours_without_suitable,
            2,
        ),
        "open_positions": open_positions,
        "signals_window": signals_window,
        "eligible_window": eligible_window,
        "rejected_window": rejected_window,
        "dominant_filter": dominant_filter,
        "dominant_count": dominant_count,
        "dominant_share_pct": round(
            dominant_share_pct,
            2,
        ),
        "dominant_examples": dominant_examples,
        "stagnation": stagnation,
        "dominant_rejection": (
            dominant_rejection
        ),
        "conditions": conditions,
        "fingerprint": fingerprint,
    }


def format_scanner_watch_email(
    analysis: Dict[str, Any],
) -> str:
    lines = [
        "=" * 68,
        "DIAMOND MARKET SCANNER - BEWAKINGSWAARSCHUWING",
        "=" * 68,
        "",
        f"Controle uitgevoerd      : {analysis.get('checked_at') or '-'}",
        f"Scannerstatus            : {analysis.get('scanner_status') or '-'}",
        f"Scanner actief           : {to_float(analysis.get('running_hours'), 0.0):.1f} uur",
        f"Open schaduwposities     : {int(to_float(analysis.get('open_positions'), 0.0))}",
        f"Laatste geschikt moment  : {analysis.get('last_suitable_at') or '-'}",
        f"Uren zonder geschikt     : {to_float(analysis.get('hours_without_suitable'), 0.0):.1f}",
        "",
        f"ANALYSE AFGELOPEN {SCANNER_WATCH_ANALYSIS_HOURS} UUR",
        f"Signalen                 : {int(to_float(analysis.get('signals_window'), 0.0))}",
        f"Geschikt voor schaduw    : {int(to_float(analysis.get('eligible_window'), 0.0))}",
        f"Afgewezen                : {int(to_float(analysis.get('rejected_window'), 0.0))}",
        "",
        "VASTGESTELDE WAARSCHUWINGEN",
    ]

    conditions = (
        analysis.get(
            "conditions"
        )
        or []
    )

    for condition in conditions:
        lines.append(
            f"- {condition}"
        )

    if analysis.get(
        "dominant_rejection"
    ):
        lines.extend([
            "",
            "DOMINANT FILTER",
            f"Filter                   : {analysis.get('dominant_filter') or '-'}",
            f"Aantal getroffen signalen: {int(to_float(analysis.get('dominant_count'), 0.0))}",
            f"Aandeel afwijzingen      : {to_float(analysis.get('dominant_share_pct'), 0.0):.1f}%",
        ])

        examples = (
            analysis.get(
                "dominant_examples"
            )
            or []
        )

        if examples:
            lines.append(
                "Voorbeelden:"
            )

            for example in examples:
                lines.append(
                    f"- {example}"
                )

    lines.extend([
        "",
        "ACTIE",
        "Dit bericht is uitsluitend adviserend.",
        "Er zijn geen filters, inzetten, stops of strategieën gewijzigd.",
        "De Market Scanner blijft virtueel en kan geen echte orders plaatsen.",
        "Beoordeling gebeurt pas op basis van voldoende gesloten schaduwtrades.",
        "=" * 68,
    ])

    return "\n".join(
        lines
    )


def format_scanner_watch_recovery_email(
    analysis: Dict[str, Any],
) -> str:
    return "\n".join([
        "=" * 68,
        "DIAMOND MARKET SCANNER - BEWAKING HERSTELD",
        "=" * 68,
        "",
        f"Controle uitgevoerd      : {analysis.get('checked_at') or '-'}",
        f"Scannerstatus            : {analysis.get('scanner_status') or '-'}",
        f"Laatste geschikt moment  : {analysis.get('last_suitable_at') or '-'}",
        f"Uren zonder geschikt     : {to_float(analysis.get('hours_without_suitable'), 0.0):.1f}",
        f"Signalen laatste 24 uur  : {int(to_float(analysis.get('signals_window'), 0.0))}",
        f"Afgewezen laatste 24 uur : {int(to_float(analysis.get('rejected_window'), 0.0))}",
        "",
        "De eerdere waarschuwingssituatie is niet meer actief.",
        "Er zijn geen instellingen automatisch gewijzigd.",
        "=" * 68,
    ])


def scanner_watch_retry_allowed(
    agent_state: Dict[str, Any],
    fingerprint: str,
) -> bool:
    if (
        agent_state.get(
            "scanner_watch_last_attempt_fingerprint"
        )
        != fingerprint
    ):
        return True

    attempted = parse_iso_datetime(
        agent_state.get(
            "scanner_watch_last_attempt_at"
        )
    )

    if attempted is None:
        return True

    return (
        now_local()
        - attempted
    ).total_seconds() >= (
        SCANNER_WATCH_RETRY_MINUTES
        * 60
    )


def scanner_watch_cooldown_elapsed(
    agent_state: Dict[str, Any],
) -> bool:
    sent_at = parse_iso_datetime(
        agent_state.get(
            "scanner_watch_last_alert_at"
        )
    )

    if sent_at is None:
        return True

    return (
        now_local()
        - sent_at
    ).total_seconds() >= (
        SCANNER_WATCH_ALERT_COOLDOWN_HOURS
        * 3600
    )


def save_scanner_watch_analysis(
    agent_state: Dict[str, Any],
    analysis: Dict[str, Any],
) -> None:
    agent_state[
        "last_scanner_watch_ts"
    ] = time.time()

    agent_state[
        "scanner_watch_checks"
    ] = int(
        to_float(
            agent_state.get(
                "scanner_watch_checks"
            ),
            0.0,
        )
    ) + 1

    agent_state[
        "scanner_watch_last_check_at"
    ] = analysis.get(
        "checked_at"
    ) or now_local().isoformat()

    agent_state[
        "scanner_watch_last_status"
    ] = analysis.get(
        "status"
    ) or "-"

    agent_state[
        "scanner_watch_last_suitable_at"
    ] = analysis.get(
        "last_suitable_at"
    ) or ""

    agent_state[
        "scanner_watch_hours_without_suitable"
    ] = to_float(
        analysis.get(
            "hours_without_suitable"
        ),
        0.0,
    )

    agent_state[
        "scanner_watch_signals_window"
    ] = int(
        to_float(
            analysis.get(
                "signals_window"
            ),
            0.0,
        )
    )

    agent_state[
        "scanner_watch_eligible_window"
    ] = int(
        to_float(
            analysis.get(
                "eligible_window"
            ),
            0.0,
        )
    )

    agent_state[
        "scanner_watch_rejected_window"
    ] = int(
        to_float(
            analysis.get(
                "rejected_window"
            ),
            0.0,
        )
    )

    agent_state[
        "scanner_watch_dominant_filter"
    ] = analysis.get(
        "dominant_filter"
    ) or ""

    agent_state[
        "scanner_watch_dominant_count"
    ] = int(
        to_float(
            analysis.get(
                "dominant_count"
            ),
            0.0,
        )
    )

    agent_state[
        "scanner_watch_dominant_share_pct"
    ] = to_float(
        analysis.get(
            "dominant_share_pct"
        ),
        0.0,
    )

    agent_state[
        "scanner_watch_active_conditions"
    ] = list(
        analysis.get(
            "conditions"
        )
        or []
    )

    agent_state[
        "scanner_watch_last_error"
    ] = ""

    save_agent_state(
        agent_state
    )


def handle_scanner_watch_alerts(
    agent_state: Dict[str, Any],
    force: bool = False,
) -> int:
    """
    Controleert scannerstilte en dominante afwijzingsfilters.

    Er wordt maximaal één gecombineerde waarschuwingsmail per controle
    verstuurd. Dezelfde situatie wordt pas na 24 uur opnieuw gemeld.
    """
    last_check_ts = to_float(
        agent_state.get(
            "last_scanner_watch_ts"
        ),
        0.0,
    )

    if (
        not force
        and time.time()
        - last_check_ts
        < SCANNER_WATCH_CHECK_INTERVAL_SECONDS
    ):
        return 0

    try:
        analysis = analyse_scanner_watch()

    except Exception as exc:
        agent_state[
            "last_scanner_watch_ts"
        ] = time.time()

        agent_state[
            "scanner_watch_last_check_at"
        ] = now_local().isoformat()

        agent_state[
            "scanner_watch_last_status"
        ] = "FOUT"

        agent_state[
            "scanner_watch_last_error"
        ] = (
            f"{type(exc).__name__}: {exc}"
        )

        save_agent_state(
            agent_state
        )

        LOG.exception(
            "Scannerbewaking mislukt: %s",
            exc,
        )

        return 0

    save_scanner_watch_analysis(
        agent_state,
        analysis,
    )

    fingerprint = str(
        analysis.get(
            "fingerprint"
        )
        or ""
    )

    conditions = (
        analysis.get(
            "conditions"
        )
        or []
    )

    sent_count = 0

    if conditions:
        same_successful_alert = (
            agent_state.get(
                "scanner_watch_alert_fingerprint"
            )
            == fingerprint
        )

        should_send = (
            not same_successful_alert
            or scanner_watch_cooldown_elapsed(
                agent_state
            )
        )

        if not should_send:
            agent_state[
                "scanner_watch_alert_active"
            ] = True

            save_agent_state(
                agent_state
            )

            return 0

        if not scanner_watch_retry_allowed(
            agent_state,
            fingerprint,
        ):
            return 0

        agent_state[
            "scanner_watch_last_attempt_fingerprint"
        ] = fingerprint

        agent_state[
            "scanner_watch_last_attempt_at"
        ] = now_local().isoformat()

        save_agent_state(
            agent_state
        )

        sent = send_email(
            "Diamond Scanner BEWAKINGSWAARSCHUWING",
            format_scanner_watch_email(
                analysis
            ),
        )

        if not sent:
            return 0

        agent_state[
            "scanner_watch_alert_active"
        ] = True

        agent_state[
            "scanner_watch_alert_fingerprint"
        ] = fingerprint

        agent_state[
            "scanner_watch_last_alert_at"
        ] = now_local().isoformat()

        agent_state[
            "scanner_watch_alert_count"
        ] = int(
            to_float(
                agent_state.get(
                    "scanner_watch_alert_count"
                ),
                0.0,
            )
        ) + 1

        save_agent_state(
            agent_state
        )

        LOG.warning(
            "Scannerbewakingswaarschuwing verstuurd | %s",
            " | ".join(
                conditions
            ),
        )

        return 1

    if to_bool(
        agent_state.get(
            "scanner_watch_alert_active"
        ),
        False,
    ):
        recovery_fingerprint = (
            "RECOVERY:"
            + str(
                agent_state.get(
                    "scanner_watch_alert_fingerprint"
                )
                or "unknown"
            )
        )

        if not scanner_watch_retry_allowed(
            agent_state,
            recovery_fingerprint,
        ):
            return 0

        agent_state[
            "scanner_watch_last_attempt_fingerprint"
        ] = recovery_fingerprint

        agent_state[
            "scanner_watch_last_attempt_at"
        ] = now_local().isoformat()

        save_agent_state(
            agent_state
        )

        sent = send_email(
            "Diamond Scanner BEWAKING HERSTELD",
            format_scanner_watch_recovery_email(
                analysis
            ),
        )

        if not sent:
            return 0

        agent_state[
            "scanner_watch_alert_active"
        ] = False

        agent_state[
            "scanner_watch_alert_fingerprint"
        ] = ""

        agent_state[
            "scanner_watch_last_recovery_at"
        ] = now_local().isoformat()

        agent_state[
            "scanner_watch_recovery_count"
        ] = int(
            to_float(
                agent_state.get(
                    "scanner_watch_recovery_count"
                ),
                0.0,
            )
        ) + 1

        save_agent_state(
            agent_state
        )

        LOG.info(
            "Scannerbewaking hersteld; herstelmail verstuurd"
        )

        sent_count += 1

    return sent_count


def scanner_watch_summary_from_state(
    agent_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    state = (
        agent_state
        if isinstance(
            agent_state,
            dict,
        )
        else load_agent_state()
    )

    return {
        "status": state.get(
            "scanner_watch_last_status"
        )
        or "NOG NIET GECONTROLEERD",
        "last_check_at": state.get(
            "scanner_watch_last_check_at"
        )
        or "-",
        "last_suitable_at": state.get(
            "scanner_watch_last_suitable_at"
        )
        or "-",
        "hours_without_suitable": to_float(
            state.get(
                "scanner_watch_hours_without_suitable"
            ),
            0.0,
        ),
        "signals_window": int(
            to_float(
                state.get(
                    "scanner_watch_signals_window"
                ),
                0.0,
            )
        ),
        "eligible_window": int(
            to_float(
                state.get(
                    "scanner_watch_eligible_window"
                ),
                0.0,
            )
        ),
        "rejected_window": int(
            to_float(
                state.get(
                    "scanner_watch_rejected_window"
                ),
                0.0,
            )
        ),
        "dominant_filter": state.get(
            "scanner_watch_dominant_filter"
        )
        or "-",
        "dominant_count": int(
            to_float(
                state.get(
                    "scanner_watch_dominant_count"
                ),
                0.0,
            )
        ),
        "dominant_share_pct": to_float(
            state.get(
                "scanner_watch_dominant_share_pct"
            ),
            0.0,
        ),
        "conditions": list(
            state.get(
                "scanner_watch_active_conditions"
            )
            or []
        ),
        "alert_active": to_bool(
            state.get(
                "scanner_watch_alert_active"
            ),
            False,
        ),
        "alert_count": int(
            to_float(
                state.get(
                    "scanner_watch_alert_count"
                ),
                0.0,
            )
        ),
        "recovery_count": int(
            to_float(
                state.get(
                    "scanner_watch_recovery_count"
                ),
                0.0,
            )
        ),
        "last_error": state.get(
            "scanner_watch_last_error"
        )
        or "-",
    }


def append_scanner_watch_status(
    lines: List[str],
    watch: Dict[str, Any],
) -> None:
    lines.extend([
        "",
        "SCANNERBEWAKING",
        f"Status                  : {watch.get('status') or '-'}",
        f"Laatste controle        : {watch.get('last_check_at') or '-'}",
        f"Laatste geschikt moment : {watch.get('last_suitable_at') or '-'}",
        f"Uren zonder geschikt    : {to_float(watch.get('hours_without_suitable'), 0.0):.1f}",
        f"Signalen laatste 24 uur : {int(to_float(watch.get('signals_window'), 0.0))}",
        f"Geschikt laatste 24 uur : {int(to_float(watch.get('eligible_window'), 0.0))}",
        f"Afgewezen laatste 24 uur: {int(to_float(watch.get('rejected_window'), 0.0))}",
        (
            "Dominant filter         : "
            f"{watch.get('dominant_filter') or '-'} | "
            f"{to_float(watch.get('dominant_share_pct'), 0.0):.1f}%"
        ),
        f"Waarschuwing actief     : {'JA' if watch.get('alert_active') else 'NEE'}",
        f"Waarschuwingsmails      : {int(to_float(watch.get('alert_count'), 0.0))}",
        f"Herstelmails            : {int(to_float(watch.get('recovery_count'), 0.0))}",
    ])


def trim_shadow_notification_history(
    agent_state: Dict[str, Any],
) -> None:
    agent_state[
        "notified_shadow_open_keys"
    ] = (
        agent_state.get(
            "notified_shadow_open_keys",
            [],
        )[
            -SHADOW_NOTIFICATION_HISTORY_LIMIT:
        ]
    )

    agent_state[
        "notified_shadow_close_keys"
    ] = (
        agent_state.get(
            "notified_shadow_close_keys",
            [],
        )[
            -SHADOW_NOTIFICATION_HISTORY_LIMIT:
        ]
    )


def shadow_notification_retry_allowed(
    agent_state: Dict[str, Any],
    event_type: str,
    event_key: str,
) -> bool:
    key_field = (
        f"last_shadow_{event_type}_attempt_key"
    )

    time_field = (
        f"last_shadow_{event_type}_attempt_at"
    )

    if (
        agent_state.get(
            key_field
        )
        != event_key
    ):
        return True

    attempted = parse_iso_datetime(
        agent_state.get(
            time_field
        )
    )

    if attempted is None:
        return True

    return (
        now_utc()
        - attempted
    ).total_seconds() >= (
        SHADOW_NOTIFICATION_RETRY_MINUTES
        * 60
    )


def record_shadow_notification_attempt(
    agent_state: Dict[str, Any],
    event_type: str,
    event_key: str,
) -> None:
    agent_state[
        f"last_shadow_{event_type}_attempt_key"
    ] = event_key

    agent_state[
        f"last_shadow_{event_type}_attempt_at"
    ] = now_utc().isoformat()

    save_agent_state(
        agent_state
    )


def format_shadow_open_email(
    position: Dict[str, Any],
) -> str:
    return "\n".join([
        "=" * 60,
        "DIAMOND MARKET SCANNER - SCHADUWTRADE GEOPEND",
        "=" * 60,
        "",
        f"Geopend                 : {format_shadow_event_time(position.get('opened_at'))}",
        f"Munt                    : {position.get('symbol') or '-'}",
        f"Richting                : {position.get('side') or '-'}",
        f"Strategie               : {position.get('strategy') or '-'}",
        f"Marktregime             : {position.get('market_regime') or '-'}",
        f"Signaalscore            : {to_float(position.get('signal_score'), 0.0):.1f}",
        f"Virtuele inzet          : €{to_float(position.get('stake_eur'), 0.0):.2f}",
        f"Instapprijs             : {to_float(position.get('entry_price'), 0.0):.12f}",
        f"Take-profit             : {to_float(position.get('take_profit'), 0.0):.12f}",
        f"Stop-loss               : {to_float(position.get('stop_loss'), 0.0):.12f}",
        f"ATR                     : {to_float(position.get('atr_pct'), 0.0):.4f}%",
        f"Spread bij instap       : {to_float(position.get('entry_spread_pct'), 0.0):.4f}%",
        "",
        "VEILIGHEID",
        "Dit is uitsluitend een virtuele schaduwtrade.",
        "Er is geen Bitvavo-order geplaatst.",
        "Bestaande munten en botposities zijn niet gewijzigd.",
        "=" * 60,
    ])


def format_shadow_close_email(
    trade: Dict[str, Any],
) -> str:
    pnl = to_float(
        trade.get(
            "net_pnl_eur"
        ),
        0.0,
    )

    if pnl > 0.000001:
        result = "WINST"
    elif pnl < -0.000001:
        result = "VERLIES"
    else:
        result = "NEUTRAAL"

    return "\n".join([
        "=" * 60,
        "DIAMOND MARKET SCANNER - SCHADUWTRADE GESLOTEN",
        "=" * 60,
        "",
        f"Resultaat               : {result}",
        f"Geopend                 : {format_shadow_event_time(trade.get('opened_at'))}",
        f"Gesloten                : {format_shadow_event_time(trade.get('closed_at'))}",
        f"Munt                    : {trade.get('symbol') or '-'}",
        f"Richting                : {trade.get('side') or '-'}",
        f"Strategie               : {trade.get('strategy') or '-'}",
        f"Marktregime             : {trade.get('market_regime') or '-'}",
        f"Sluitreden              : {trade.get('exit_reason') or '-'}",
        f"Virtuele inzet          : €{to_float(trade.get('stake_eur'), 0.0):.2f}",
        f"Instapprijs             : {to_float(trade.get('entry_price'), 0.0):.12f}",
        f"Uitstapprijs            : {to_float(trade.get('exit_price'), 0.0):.12f}",
        f"Brutoresultaat          : €{to_float(trade.get('gross_pnl_eur'), 0.0):+.4f}",
        f"Totale kosten           : €{to_float(trade.get('total_fees_eur'), 0.0):.4f}",
        f"Nettoresultaat          : €{pnl:+.4f}",
        f"Rendement               : {to_float(trade.get('return_pct'), 0.0):+.4f}%",
        f"Looptijd                : {to_float(trade.get('duration_minutes'), 0.0):.1f} minuten",
        "",
        "VEILIGHEID",
        "Dit was uitsluitend een virtuele schaduwtrade.",
        "Er is geen Bitvavo-order geplaatst.",
        "Het Strategy Lab verwerkt deze trade automatisch.",
        "=" * 60,
    ])


def handle_live_trade_notifications(
    agent_state: Dict[str, Any],
) -> int:
    """
    Stuurt precies één e-mail per nieuwe ECHTE BUY/SELL-transactie.

    Veiligheid:
    - dry-run transacties worden nooit gemaild;
    - bestaande transacties worden bij eerste initialisatie alleen
      als gezien opgeslagen;
    - verzonden transacties worden persistent onthouden;
    - restart/deploy veroorzaakt daardoor geen dubbele meldingen;
    - bij mailfout wordt de transactie niet als verzonden gemarkeerd,
      zodat later opnieuw geprobeerd kan worden.
    """
    path = Path(TRADES_FILE)

    if not path.exists():
        return 0

    try:
        with path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:
            rows = list(csv.DictReader(file))
    except Exception as exc:
        LOG.error(
            "Live transacties lezen mislukt: %s",
            exc,
        )
        return 0

    live_rows: List[Dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        side = str(
            row.get("side") or ""
        ).strip().upper()

        if side not in {"BUY", "SELL"}:
            continue

        # Fail-safe: alleen rijen met expliciete dry_run-kolom.
        if "dry_run" not in row:
            continue

        if to_bool(
            row.get("dry_run"),
            True,
        ):
            continue

        live_rows.append(row)

    def event_key(
        row: Dict[str, Any],
    ) -> str:
        raw = "|".join(
            [
                str(row.get("ts") or ""),
                str(row.get("market") or ""),
                str(row.get("side") or ""),
                str(row.get("price") or ""),
                str(row.get("amount") or ""),
                str(row.get("quote_amount") or ""),
                str(row.get("fees_quote") or ""),
                str(row.get("reason") or ""),
            ]
        )

        return hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

    existing_keys = [
        event_key(row)
        for row in live_rows
    ]

    # Eerste start na installatie:
    # alle reeds bestaande echte trades als gezien markeren.
    if not to_bool(
        agent_state.get(
            "live_trade_notifications_initialized"
        ),
        False,
    ):
        agent_state[
            "notified_live_trade_keys"
        ] = existing_keys[-500:]

        agent_state[
            "live_trade_notifications_initialized"
        ] = True

        agent_state.setdefault(
            "live_trade_notifications_sent",
            0,
        )

        save_agent_state(
            agent_state
        )

        LOG.info(
            "Live trade mail baseline gestart | "
            "bestaande echte transacties=%d",
            len(existing_keys),
        )

        return 0

    history = set(
        str(value)
        for value in (
            agent_state.get(
                "notified_live_trade_keys"
            )
            or []
        )
    )

    agent_state.setdefault(
        "notified_live_trade_keys",
        [],
    )
    agent_state.setdefault(
        "live_trade_notifications_sent",
        0,
    )

    sent_count = 0

    for row in live_rows:
        key = event_key(row)

        if key in history:
            continue

        side = str(
            row.get("side") or ""
        ).strip().upper()

        market = str(
            row.get("market") or "ONBEKEND"
        )

        action = (
            "AANKOOP"
            if side == "BUY"
            else "VERKOOP"
        )

        subject = (
            f"Diamond Trader LIVE {action} | {market}"
        )

        lines = [
            "DIAMOND TRADER LIVE TRANSACTIE",
            "=" * 40,
            f"Type       : {action}",
            f"Markt      : {market}",
            f"Tijd       : {row.get('ts') or '-'}",
            f"Prijs      : {row.get('price') or '-'}",
            f"Aantal     : {row.get('amount') or '-'}",
            f"Bedrag     : {row.get('quote_amount') or '-'}",
            f"Kosten     : {row.get('fees_quote') or '-'}",
            f"Netto PnL  : {row.get('net_pnl_quote') or '-'}",
            f"Reden      : {row.get('reason') or '-'}",
            "=" * 40,
            "Dit is een echte Bitvavo-transactie.",
        ]

        sent = send_email(
            subject,
            "\n".join(lines),
            bypass_mute=True,
        )

        if not sent:
            LOG.warning(
                "Live trade e-mail mislukt | %s | %s",
                side,
                market,
            )
            continue

        agent_state[
            "notified_live_trade_keys"
        ].append(key)

        history.add(key)

        agent_state[
            "notified_live_trade_keys"
        ] = agent_state[
            "notified_live_trade_keys"
        ][-500:]

        agent_state[
            "live_trade_notifications_sent"
        ] = int(
            to_float(
                agent_state.get(
                    "live_trade_notifications_sent"
                ),
                0,
            )
        ) + 1

        agent_state[
            "last_live_trade_email_at"
        ] = now_local().isoformat()

        agent_state[
            "last_live_trade_email_market"
        ] = market

        agent_state[
            "last_live_trade_email_side"
        ] = side

        # Meteen persistent opslaan om dubbele mails na restart
        # zo veel mogelijk uit te sluiten.
        save_agent_state(
            agent_state
        )

        LOG.info(
            "Live trade e-mail verzonden | %s | %s",
            side,
            market,
        )

        sent_count += 1

    return sent_count


def handle_shadow_trade_notifications(
    agent_state: Dict[str, Any],
) -> int:
    """
    Shadow trade OPEN/CLOSE e-mails zijn bewust gedempt.

    De shadow trades, scanner en Strategy Lab blijven ongewijzigd draaien.
    Nieuwe open/sluit-gebeurtenissen worden wel stil als gezien opgeslagen,
    zodat opnieuw inschakelen later geen oude e-mailachterstand veroorzaakt.

    MAILFLOOD_GUARD_V1
    """
    open_history = set(
        str(value)
        for value in (
            agent_state.get("notified_shadow_open_keys")
            or []
        )
    )

    close_history = set(
        str(value)
        for value in (
            agent_state.get("notified_shadow_close_keys")
            or []
        )
    )

    agent_state.setdefault("notified_shadow_open_keys", [])
    agent_state.setdefault("notified_shadow_close_keys", [])

    new_open_seen = 0
    new_close_seen = 0

    for trade in load_shadow_closed_trades()[-50:]:
        event_key = shadow_event_key("close", trade)
        if event_key in close_history:
            continue
        agent_state["notified_shadow_close_keys"].append(event_key)
        close_history.add(event_key)
        new_close_seen += 1

    for position in load_shadow_open_positions():
        event_key = shadow_event_key("open", position)
        if event_key in open_history:
            continue
        agent_state["notified_shadow_open_keys"].append(event_key)
        open_history.add(event_key)
        new_open_seen += 1

    if new_open_seen or new_close_seen:
        trim_shadow_notification_history(agent_state)
        save_agent_state(agent_state)
        LOG.info(
            "Shadow trade e-mailmeldingen gedempt | "
            "open stil verwerkt=%d | close stil verwerkt=%d",
            new_open_seen,
            new_close_seen,
        )

    return 0


# ============================================================
# Rapportplanning
# ============================================================

def clean_agent_history(
    agent_state: Dict[str, Any],
) -> None:
    agent_state["sent_reports"] = (
        agent_state.get(
            "sent_reports",
            [],
        )[-50:]
    )

    agent_state["sent_weekly_reports"] = (
        agent_state.get(
            "sent_weekly_reports",
            [],
        )[-12:]
    )

    trim_shadow_notification_history(
        agent_state
    )


def handle_scheduled_reports(
    exchange: ccxt.Exchange,
    agent_state: Dict[str, Any],
) -> None:
    current = now_local()

    if current.hour not in REPORT_HOURS:
        return

    report_key = current.strftime(
        "%Y-%m-%d-%H"
    )

    if report_key not in agent_state["sent_reports"]:
        subject = (
            "Diamond Bot status "
            + current.strftime(
                "%d-%m-%Y %H:%M"
            )
        )

        sent = send_email(
            subject,
            build_report(exchange),
        )

        if sent:
            agent_state["sent_reports"].append(
                report_key
            )

            clean_agent_history(
                agent_state
            )

            save_agent_state(
                agent_state
            )

    # Zondag om 22:00 ook een weekrapport
    if (
        current.weekday()
        == WEEKLY_REPORT_WEEKDAY
        and current.hour == 22
    ):
        week_key = current.strftime(
            "%G-W%V"
        )

        if (
            week_key
            not in agent_state["sent_weekly_reports"]
        ):
            sent = send_email(
                (
                    "Diamond Bot WEEKRAPPORT "
                    + current.strftime(
                        "%d-%m-%Y"
                    )
                ),
                build_weekly_report(
                    exchange
                ),
            )

            if sent:
                agent_state[
                    "sent_weekly_reports"
                ].append(
                    week_key
                )

                clean_agent_history(
                    agent_state
                )

                save_agent_state(
                    agent_state
                )


# ============================================================
# Hoofdprogramma
# ============================================================

def main() -> None:
    if (
        not BITVAVO_API_KEY
        or not BITVAVO_API_SECRET
    ):
        raise RuntimeError(
            "BITVAVO_API_KEY of "
            "BITVAVO_API_SECRET ontbreekt"
        )

    for path in (
        STATE_FILE,
        TRADES_FILE,
        AGENT_STATE_FILE,
        CONTROL_FILE,
        TEST_BASELINE_FILE,
        TEST_REPORT_FILE,
        SHORT_TEST_BASELINE_FILE,
        SHORT_TEST_REPORT_FILE,
        DIAG_STATS_FILE,
        SUPERVISOR_STATE_FILE,
        MARKET_SIGNALS_JSON_FILE,
        MARKET_SIGNALS_CSV_FILE,
        MARKET_SCANNER_STATE_FILE,
        SHADOW_TRADES_FILE,
        STRATEGY_LAB_JSON_FILE,
        STRATEGY_LAB_TEXT_FILE,
        STRATEGY_LAB_GROUPS_FILE,
        READINESS_GATE_JSON_FILE,
        READINESS_GATE_TEXT_FILE,
        FINAL_VALIDATION_FILE,
        LIVE_APPROVAL_FILE,
    ):
        ensure_parent(path)

    if not Path(
        CONTROL_FILE
    ).exists():
        save_json_atomic(
            CONTROL_FILE,
            default_control(),
        )

    exchange = create_exchange()
    agent_state = load_agent_state()

    LOG.info(
        "Diamond Agent v7.5 gestart"
    )

    LOG.info(
        "State-bestand: %s",
        STATE_FILE,
    )

    LOG.info(
        "Transactiebestand: %s",
        TRADES_FILE,
    )

    LOG.info(
        "Controlebestand: %s",
        CONTROL_FILE,
    )

    LOG.info(
        "Testbaseline: %s",
        TEST_BASELINE_FILE,
    )

    LOG.info(
        "Testrapport: %s",
        TEST_REPORT_FILE,
    )

    LOG.info(
        "Paper-shortbaseline: %s",
        SHORT_TEST_BASELINE_FILE,
    )

    LOG.info(
        "Paper-shortrapport: %s",
        SHORT_TEST_REPORT_FILE,
    )

    LOG.info(
        "Paper-shorttussenrapporten: %s en %s",
        short_interim_report_file(5),
        short_interim_report_file(10),
    )

    LOG.info(
        "Dagelijkse back-up: %s | na %02d:00 | bewaren=%d dagen",
        BACKUP_DIR,
        BACKUP_HOUR_LOCAL,
        BACKUP_RETENTION_DAYS,
    )

    LOG.info(
        "Market Scanner-back-up: signalen JSON/CSV, scanner-state en schaduwtrades"
    )

    LOG.info(
        "Strategy Lab-back-up: JSON-, tekst- en groepenrapport"
    )

    LOG.info(
        "Schaduwtrade-e-mails: direct bij openen en sluiten"
    )

    LOG.info(
        "Schaduwmijlpaalrapporten: 5, 10 en 20 gesloten trades"
    )

    LOG.info(
        "Strategy Lab directe verversing: bij openen en sluiten"
    )

    LOG.info(
        "Strategy Lab e-mailintegratie: statusmail en weekrapport"
    )

    LOG.info(
        "Scannerbewaking: 24 uur stilte en dominant afwijzingsfilter"
    )

    LOG.info(
        "Readiness Gate: centrale alleen-lezen gereedheidscontrole"
    )

    LOG.info(
        "Readiness Gate-rapporten: %s en %s",
        READINESS_GATE_JSON_FILE,
        READINESS_GATE_TEXT_FILE,
    )

    LOG.info(
        "Rapporttijden: 06:00, 10:00, 14:00, 18:00 en 22:00"
    )

    while True:
        try:
            handle_daily_backup(
                agent_state
            )

            handle_live_trade_notifications(
                agent_state
            )

            handle_shadow_trade_notifications(
                agent_state
            )

            refresh_strategy_lab_if_needed(
                agent_state
            )

            handle_scanner_watch_alerts(
                agent_state
            )

            refresh_readiness_gate(
                agent_state
            )

            check_shadow_milestone_reports()

            check_short_test_interim_reports(
                exchange
            )

            check_short_test_target(
                exchange
            )

            check_test_target(
                exchange
            )

            handle_scheduled_reports(
                exchange,
                agent_state,
            )

            last_analysis = to_float(
                agent_state.get(
                    "last_analysis_ts"
                ),
                0.0,
            )

            if (
                time.time()
                - last_analysis
                >= ANALYZE_INTERVAL_SECONDS
            ):
                analyze_and_act(
                    exchange
                )

                agent_state[
                    "last_analysis_ts"
                ] = time.time()

                save_agent_state(
                    agent_state
                )

        except Exception as exc:
            LOG.exception(
                "Agent-hoofdloop fout: %s",
                exc,
            )

        time.sleep(
            LOOP_SLEEP_SECONDS
        )


if __name__ == "__main__":
    main()