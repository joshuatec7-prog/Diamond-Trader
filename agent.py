#!/usr/bin/env python3
"""
Diamond Agent v6.4

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
- Maakt en mailt het paper-shortrapport na 20 gesloten shorts.
"""

import csv
import json
import logging
import os
import smtplib
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

# Veiligheidsgrenzen
MAX_DAY_LOSS_PCT = 1.5
BTC_DROP_LIMIT_PCT = -8.0
BTC_RECOVERY_PCT = 4.0

DEFAULT_TOTAL_CAPITAL = 3000.0


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
            "reason": trade_reason(
                row
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
        "by_reason": summarize_test_group(
            selected_trades,
            "reason",
        ),
        "trades": selected_trades,
    }

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

    lines = [
        "=" * 60,
        "DIAMOND TRADER PAPER-SHORT EINDRAPPORT",
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
        f"RSI verkoopmaximum      : {to_float(settings.get('rsi_sell_max'), 0.0):.1f}",
        f"Minimum nettowinst      : €{to_float(settings.get('min_profit_eur'), 0.0):.2f}",
        f"Minimum ATR             : {to_float(settings.get('min_atr_pct'), 0.0):.2f}%",
        f"Timeframe               : {settings.get('timeframe')}",
        "",
        f"JSON-rapport            : {SHORT_TEST_REPORT_FILE}",
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
) -> bool:
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


def build_report(
    exchange: ccxt.Exchange,
) -> str:
    state = load_bot_state()
    control = load_control()
    trades = load_trades()

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
        "LET OP",
        "De automatische wekelijkse verhoging van de inzet",
        "wordt in de volgende stap toegevoegd.",
        "=" * 60,
    ]

    return "\n".join(lines)


# ============================================================
# Automatische dry-run teststop
# ============================================================

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
        "Diamond Agent v6.4 gestart"
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
        "Rapporttijden: 06:00, 10:00, 14:00, 18:00 en 22:00"
    )

    while True:
        try:
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
