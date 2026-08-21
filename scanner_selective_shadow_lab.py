#!/usr/bin/env python3
"""
Diamond Trader Scanner Selective Shadow Lab v1.0

Prospectieve, read-only vergelijking vanaf een nieuwe baseline.

Bron
----
Alleen nieuwe Market Scanner-signalen uit:
    /var/data/diamond_market_signals.csv

Belangrijk
----------
Alle varianten gebruiken UITSLUITEND signalen die de huidige Market Scanner
zelf al als shadow_eligible=True heeft beoordeeld. De bestaande score-, spread-,
verwachte-winst- en RR-filters worden dus NIET versoepeld.

Varianten
---------
CURRENT
    Alle huidige scanner-eligible signalen.

SELECTIVE
    Alleen historische combinaties die in de eerste kwaliteitsanalyse potentie
    lieten zien:
    - LONG + trend_breakout
    - SHORT + BEARISH_WEAK
    - SHORT + momentum
    - SHORT + pullback_retest

STRONG
    Strengere hypothese:
    - trend_breakout met score >= 95
    OF
    - SHORT + BEARISH_WEAK

Dit is bewust een NIEUWE prospectieve test. Oude resultaten worden niet
teruggevuld.

Veiligheid
----------
- geen orders;
- geen private API;
- geen API-sleutels aan ccxt;
- geen wijziging aan config.yaml;
- geen wijziging aan diamond_state.json;
- geen wijziging aan diamond_transactions.csv;
- geen wijziging aan Market Scanner-state;
- schrijft uitsluitend eigen Selective Shadow-bestanden in /var/data.

Gebruik
-------
    python3 scanner_selective_shadow_lab.py --self-test
    python3 scanner_selective_shadow_lab.py --update
    python3 scanner_selective_shadow_lab.py --status
    python3 scanner_selective_shadow_lab.py --update --no-print
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "1.0"
MODE = "READ_ONLY_SCANNER_SELECTIVE_SHADOW"

PROJECT_DIR = Path(os.getenv("DIAMOND_PROJECT_DIR", "/opt/render/project/src"))
DATA_DIR = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
CONFIG_FILE = Path(os.getenv("CFG_FILE", str(PROJECT_DIR / "config.yaml")))

SIGNALS_FILE = DATA_DIR / "diamond_market_signals.csv"
BASELINE_FILE = DATA_DIR / "diamond_scanner_selective_shadow_baseline.json"
STATE_FILE = DATA_DIR / "diamond_scanner_selective_shadow_state.json"
REPORT_FILE = DATA_DIR / "diamond_scanner_selective_shadow_report.json"
TRADES_FILE = DATA_DIR / "diamond_scanner_selective_shadow_trades.csv"
SIGNAL_MEASUREMENTS_FILE = DATA_DIR / "diamond_signal_measurements.jsonl"
EXECUTION_MEASUREMENTS_FILE = DATA_DIR / "diamond_selective_execution_measurements.jsonl"

TARGET_CLOSED = 20
TIMEFRAME_MS = 15 * 60 * 1000
MAX_SIGNAL_KEYS = 30_000

VARIANTS = ("CURRENT", "SELECTIVE", "STRONG")

TRADE_HEADER = [
    "variant",
    "candidate_key",
    "detected_at",
    "opened_at",
    "closed_at",
    "symbol",
    "strategy",
    "side",
    "market_regime",
    "signal_score",
    "reward_risk",
    "entry_price",
    "exit_price",
    "stake_eur",
    "amount",
    "entry_fee_eur",
    "exit_fee_eur",
    "total_fees_eur",
    "entry_spread_pct",
    "take_profit",
    "stop_loss",
    "exit_reason",
    "gross_pnl_eur",
    "net_pnl_eur",
    "return_pct",
    "duration_minutes",
    "entry_candle_timestamp_ms",
    "exit_candle_timestamp_ms",
    "exit_spread_assumption",
]

SAFETY = {
    "orders_possible": False,
    "private_exchange_calls": False,
    "api_keys_passed_to_exchange": False,
    "config_modified": False,
    "diamond_state_modified": False,
    "diamond_transactions_modified": False,
    "market_scanner_state_modified": False,
    "automatic_live_changes": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def datetime_ms(value: Any) -> int:
    dt = parse_datetime(value)
    return int(dt.timestamp() * 1000) if dt else 0


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "ja", "on"}:
        return True
    if text in {"0", "false", "no", "nee", "off"}:
        return False
    return default


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return default.copy()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default.copy()
    except Exception:
        return default.copy()


def save_json_atomic(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as tmp:
        json.dump(value, tmp, indent=2, ensure_ascii=False)
        tmp_name = tmp.name
    os.replace(tmp_name, path)


def load_settings() -> Dict[str, Any]:
    result = {
        "stake_eur": 120.0,
        "fee_pct_per_side": 0.25,
        "max_hold_minutes": 2880,
    }

    if not CONFIG_FILE.exists():
        return result

    try:
        import yaml
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        scanner = cfg.get("market_scanner") or {}
        risk = cfg.get("risk") or {}
        fees = cfg.get("fees") or {}

        if not isinstance(scanner, dict):
            scanner = {}
        if not isinstance(risk, dict):
            risk = {}
        if not isinstance(fees, dict):
            fees = {}

        result["stake_eur"] = max(
            5.0,
            to_float(scanner.get("stake_eur", risk.get("fixed_stake_quote", 120)), 120),
        )
        result["fee_pct_per_side"] = max(
            0.0,
            to_float(scanner.get("fee_pct_per_side", fees.get("taker_fee_pct", 0.25)), 0.25),
        )
        result["max_hold_minutes"] = max(
            60,
            to_int(scanner.get("max_hold_minutes", 2880), 2880),
        )
    except Exception:
        pass

    return result


def default_baseline() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "mode": MODE,
        "started_at": now_iso(),
        "target_closed_per_variant": TARGET_CLOSED,
        "variants": list(VARIANTS),
        "safety": SAFETY,
    }


def ensure_baseline() -> Dict[str, Any]:
    baseline = load_json(BASELINE_FILE, {})
    if not baseline.get("started_at"):
        baseline = default_baseline()
        save_json_atomic(BASELINE_FILE, baseline)
    return baseline


def blank_totals() -> Dict[str, Any]:
    return {
        "accepted_signals": 0,
        "opened": 0,
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "neutral": 0,
        "net_pnl_eur": 0.0,
        "total_fees_eur": 0.0,
    }


def default_state(started_at: str) -> Dict[str, Any]:
    return {
        "version": VERSION,
        "mode": MODE,
        "started_at": started_at,
        "last_update_at": None,
        "processed_signal_keys": [],
        "eligible_signals_seen": 0,
        "variants": {
            name: {
                "open_positions": {},
                "totals": blank_totals(),
            }
            for name in VARIANTS
        },
        "settings": {},
        "last_errors": [],
        "safety": SAFETY,
    }


def load_state(started_at: str) -> Dict[str, Any]:
    state = load_json(STATE_FILE, default_state(started_at))
    state["version"] = VERSION
    state["mode"] = MODE
    state["started_at"] = started_at
    state.setdefault("processed_signal_keys", [])
    state.setdefault("eligible_signals_seen", 0)
    state.setdefault("variants", {})
    state.setdefault("settings", {})
    state.setdefault("last_errors", [])
    state["safety"] = SAFETY

    if not isinstance(state["processed_signal_keys"], list):
        state["processed_signal_keys"] = []

    for name in VARIANTS:
        item = state["variants"].setdefault(name, {})
        item.setdefault("open_positions", {})
        item.setdefault("totals", blank_totals())
        if not isinstance(item["open_positions"], dict):
            item["open_positions"] = {}
        for key, value in blank_totals().items():
            item["totals"].setdefault(key, value)

    return state


def read_signals() -> List[Dict[str, str]]:
    if not SIGNALS_FILE.exists() or SIGNALS_FILE.stat().st_size == 0:
        return []

    with SIGNALS_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return []

    required = {
        "detected_at",
        "candle_timestamp",
        "symbol",
        "strategy",
        "side",
        "market_regime",
        "score",
        "entry_price",
        "take_profit",
        "stop_loss",
        "spread_pct",
        "reward_risk",
        "shadow_eligible",
    }
    missing = required - set(rows[0].keys())
    if missing:
        raise ValueError(
            "Signalenbestand mist kolommen: " + ", ".join(sorted(missing))
        )

    return rows


def candidate_key(row: Dict[str, str]) -> str:
    return "|".join([
        str(row.get("symbol") or "").upper(),
        str(row.get("strategy") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("candle_timestamp") or ""),
    ])



def load_signal_measurements() -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if not SIGNAL_MEASUREMENTS_FILE.exists():
        return result

    with SIGNAL_MEASUREMENTS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            key = "|".join([
                str(row.get("symbol") or "").upper(),
                str(row.get("strategy") or ""),
                str(row.get("side") or "").upper(),
                str(row.get("candle_timestamp") or ""),
            ])
            result[key] = row

    return result


def append_execution_measurement(row: Dict[str, Any]) -> None:
    data = {
        "candidate_key": row.get("candidate_key"),
        "closed_at": row.get("closed_at"),
        "symbol": row.get("symbol"),
        "strategy": row.get("strategy"),
        "side": row.get("side"),
        "selection_reason": row.get("selection_reason", "UNKNOWN"),
        "candle_entry_price": row.get("candle_entry_price"),
        "legacy_entry_price": row.get("entry_price"),
        "detection_quote_at": row.get("detection_quote_at"),
        "detection_bid": row.get("detection_bid"),
        "detection_ask": row.get("detection_ask"),
        "detection_spread_pct": row.get("detection_spread_pct"),
        "executable_entry_price": row.get("executable_entry_price"),
        "entry_gap_pct": row.get("entry_gap_pct"),
        "adverse_entry_gap_pct": row.get("adverse_entry_gap_pct"),
        "quote_source": row.get("quote_source"),
        "measurement_available": row.get("measurement_available"),
        "mae_pct": row.get("mae_pct"),
        "mfe_pct": row.get("mfe_pct"),
        "exec_mae_pct": row.get("exec_mae_pct"),
        "exec_mfe_pct": row.get("exec_mfe_pct"),
        "exit_price": row.get("exit_price"),
        "exit_reason": row.get("exit_reason"),
        "net_pnl_eur": row.get("net_pnl_eur"),
        "total_fees_eur": row.get("total_fees_eur"),
        "duration_minutes": row.get("duration_minutes"),
    }

    with EXECUTION_MEASUREMENTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def after_baseline(row: Dict[str, str], baseline_dt: datetime) -> bool:
    detected = parse_datetime(row.get("detected_at"))
    return detected is not None and detected >= baseline_dt


def variant_accepts(name: str, row: Dict[str, str]) -> bool:
    if not to_bool(row.get("shadow_eligible"), False):
        return False

    side = str(row.get("side") or "").upper()
    strategy = str(row.get("strategy") or "")
    regime = str(row.get("market_regime") or "").upper()
    score = to_float(row.get("score"), 0.0)

    if name == "CURRENT":
        return True

    if name == "SELECTIVE":
        if side == "LONG" and strategy == "trend_breakout":
            return True
        if side == "SHORT" and regime == "BEARISH_WEAK":
            return True
        if side == "SHORT" and strategy in {"momentum", "pullback_retest"}:
            return True
        return False

    if name == "STRONG":
        return (
            (strategy == "trend_breakout" and score >= 95.0)
            or (side == "SHORT" and regime == "BEARISH_WEAK")
        )

    return False


def build_position(
    variant: str,
    row: Dict[str, str],
    settings: Dict[str, Any],
    measurement: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    measurement = measurement or {}
    raw_entry = to_float(row.get("entry_price"), 0.0)
    raw_tp = to_float(row.get("take_profit"), 0.0)
    raw_sl = to_float(row.get("stop_loss"), 0.0)
    spread = max(0.0, to_float(row.get("spread_pct"), 0.0))
    side = str(row.get("side") or "").upper()
    candle_ms = datetime_ms(row.get("candle_timestamp"))

    if side not in {"LONG", "SHORT"}:
        return None
    if min(raw_entry, raw_tp, raw_sl) <= 0 or candle_ms <= 0:
        return None

    half_spread = spread / 200.0

    if side == "LONG":
        entry = raw_entry * (1.0 + half_spread)
    else:
        entry = raw_entry * (1.0 - half_spread)

    delta = entry - raw_entry
    take_profit = raw_tp + delta
    stop_loss = raw_sl + delta

    stake = float(settings["stake_eur"])
    fee_pct = float(settings["fee_pct_per_side"])
    amount = stake / entry
    entry_fee = stake * fee_pct / 100.0

    return {
        "variant": variant,
        "candidate_key": candidate_key(row),
        "detected_at": str(row.get("detected_at") or ""),
        "opened_at": str(row.get("detected_at") or now_iso()),
        "symbol": str(row.get("symbol") or "").upper(),
        "strategy": str(row.get("strategy") or ""),
        "side": side,
        "market_regime": str(row.get("market_regime") or "-"),
        "signal_score": to_float(row.get("score"), 0.0),
        "reward_risk": to_float(row.get("reward_risk"), 0.0),
        "selection_reason": measurement.get("selection_reason", "UNKNOWN"),
        "candle_entry_price": measurement.get("candle_entry_price", raw_entry),
        "detection_quote_at": measurement.get("detection_quote_at"),
        "detection_bid": measurement.get("detection_bid"),
        "detection_ask": measurement.get("detection_ask"),
        "detection_spread_pct": measurement.get("detection_spread_pct"),
        "executable_entry_price": measurement.get("executable_entry_price"),
        "entry_gap_pct": measurement.get("entry_gap_pct"),
        "adverse_entry_gap_pct": measurement.get("adverse_entry_gap_pct"),
        "quote_source": measurement.get("quote_source"),
        "measurement_available": bool(
            to_float(measurement.get("executable_entry_price"), 0.0) > 0
        ),
        "mae_pct": 0.0,
        "mfe_pct": 0.0,
        "exec_mae_pct": 0.0,
        "exec_mfe_pct": 0.0,
        "entry_price": entry,
        "amount": amount,
        "stake_eur": stake,
        "entry_fee_eur": entry_fee,
        "entry_spread_pct": spread,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "entry_candle_timestamp_ms": candle_ms + TIMEFRAME_MS,
        "last_checked_candle_ms": candle_ms + TIMEFRAME_MS,
    }


def append_trade(row: Dict[str, Any]) -> None:
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    header_needed = not TRADES_FILE.exists() or TRADES_FILE.stat().st_size == 0

    with TRADES_FILE.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_HEADER)
        if header_needed:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in TRADE_HEADER})


def close_row(
    position: Dict[str, Any],
    raw_exit: float,
    exit_reason: str,
    exit_ms: int,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    spread = float(position["entry_spread_pct"])
    half_spread = spread / 200.0

    if position["side"] == "LONG":
        exit_price = raw_exit * (1.0 - half_spread)
        gross = (
            exit_price - float(position["entry_price"])
        ) * float(position["amount"])
    else:
        exit_price = raw_exit * (1.0 + half_spread)
        gross = (
            float(position["entry_price"]) - exit_price
        ) * float(position["amount"])

    exit_notional = float(position["amount"]) * exit_price
    exit_fee = exit_notional * float(settings["fee_pct_per_side"]) / 100.0
    total_fees = float(position["entry_fee_eur"]) + exit_fee
    net = gross - total_fees
    stake = float(position["stake_eur"])
    return_pct = net / stake * 100.0 if stake > 0 else 0.0

    return {
        **position,
        "closed_at": datetime.fromtimestamp(
            exit_ms / 1000,
            tz=timezone.utc,
        ).isoformat(),
        "exit_price": round(exit_price, 12),
        "exit_fee_eur": round(exit_fee, 6),
        "total_fees_eur": round(total_fees, 6),
        "exit_reason": exit_reason,
        "gross_pnl_eur": round(gross, 6),
        "net_pnl_eur": round(net, 6),
        "return_pct": round(return_pct, 6),
        "duration_minutes": round(
            max(
                0.0,
                (exit_ms - int(position["entry_candle_timestamp_ms"])) / 60_000,
            ),
            2,
        ),
        "exit_candle_timestamp_ms": exit_ms,
        "exit_spread_assumption": "entry_spread_reused",
    }


def evaluate(
    position: Dict[str, Any],
    candles: Iterable[List[Any]],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    for candle in candles:
        if len(candle) < 5:
            continue

        candle_ms = to_int(candle[0], 0)
        if candle_ms <= int(position.get("last_checked_candle_ms", 0)):
            continue

        position["last_checked_candle_ms"] = candle_ms
        high = to_float(candle[2], 0.0)
        low = to_float(candle[3], 0.0)
        close = to_float(candle[4], 0.0)

        entry = float(position["entry_price"])
        if entry > 0:
            if position["side"] == "LONG":
                adverse = max(0.0, (entry - low) / entry * 100.0)
                favorable = max(0.0, (high - entry) / entry * 100.0)
            else:
                adverse = max(0.0, (high - entry) / entry * 100.0)
                favorable = max(0.0, (entry - low) / entry * 100.0)

            position["mae_pct"] = max(
                to_float(position.get("mae_pct")), adverse
            )
            position["mfe_pct"] = max(
                to_float(position.get("mfe_pct")), favorable
            )

        exec_entry = to_float(
            position.get("executable_entry_price"), 0.0
        )
        if exec_entry > 0:
            if position["side"] == "LONG":
                exec_adverse = max(
                    0.0, (exec_entry - low) / exec_entry * 100.0
                )
                exec_favorable = max(
                    0.0, (high - exec_entry) / exec_entry * 100.0
                )
            else:
                exec_adverse = max(
                    0.0, (high - exec_entry) / exec_entry * 100.0
                )
                exec_favorable = max(
                    0.0, (exec_entry - low) / exec_entry * 100.0
                )

            position["exec_mae_pct"] = max(
                to_float(position.get("exec_mae_pct")), exec_adverse
            )
            position["exec_mfe_pct"] = max(
                to_float(position.get("exec_mfe_pct")), exec_favorable
            )

        if position["side"] == "LONG":
            stop_hit = low <= float(position["stop_loss"])
            target_hit = high >= float(position["take_profit"])
        else:
            stop_hit = high >= float(position["stop_loss"])
            target_hit = low <= float(position["take_profit"])

        # Conservatief: stop-loss wint wanneer beide niveaus in dezelfde
        # afgesloten 15m-candle worden geraakt.
        if stop_hit:
            return close_row(
                position,
                float(position["stop_loss"]),
                "stop_loss",
                candle_ms + TIMEFRAME_MS,
                settings,
            )

        if target_hit:
            return close_row(
                position,
                float(position["take_profit"]),
                "take_profit",
                candle_ms + TIMEFRAME_MS,
                settings,
            )

        held = (
            candle_ms - int(position["entry_candle_timestamp_ms"])
        ) / 60_000

        if held >= int(settings["max_hold_minutes"]) and close > 0:
            return close_row(
                position,
                close,
                "time_exit",
                candle_ms + TIMEFRAME_MS,
                settings,
            )

    return None


def create_public_exchange() -> Any:
    import ccxt

    exchange = ccxt.bitvavo({
        "enableRateLimit": True,
        "timeout": 30_000,
    })
    exchange.load_markets()

    if not exchange.has.get("fetchOHLCV"):
        raise RuntimeError("Bitvavo ondersteunt fetchOHLCV niet")

    return exchange


def fetch_closed_candles(
    exchange: Any,
    symbol: str,
    since_ms: int,
) -> List[List[Any]]:
    rows = exchange.fetch_ohlcv(
        symbol,
        timeframe="15m",
        since=max(0, since_ms),
        limit=500,
    ) or []

    now_ms = int(exchange.milliseconds())
    return [
        row
        for row in rows
        if len(row) >= 5
        and to_int(row[0], 0) + TIMEFRAME_MS <= now_ms
    ]


def ingest(
    state: Dict[str, Any],
    rows: List[Dict[str, str]],
    baseline_dt: datetime,
    settings: Dict[str, Any],
) -> None:
    processed_order = list(dict.fromkeys(
        str(x) for x in state["processed_signal_keys"]
    ))
    processed = set(processed_order)
    measurements = load_signal_measurements()

    # Per variant/munt/candle maximaal één signaal, met de hoogste score.
    grouped: Dict[str, Dict[Tuple[str, str], Dict[str, str]]] = {
        name: {} for name in VARIANTS
    }

    for row in rows:
        if not after_baseline(row, baseline_dt):
            continue

        key = candidate_key(row)
        if key in processed:
            continue

        processed.add(key)
        processed_order.append(key)

        if not to_bool(row.get("shadow_eligible"), False):
            continue

        state["eligible_signals_seen"] = (
            int(state.get("eligible_signals_seen", 0) or 0) + 1
        )

        symbol_candle = (
            str(row.get("symbol") or "").upper(),
            str(row.get("candle_timestamp") or ""),
        )

        for name in VARIANTS:
            if not variant_accepts(name, row):
                continue

            previous = grouped[name].get(symbol_candle)
            if previous is None or to_float(row.get("score")) > to_float(
                previous.get("score")
            ):
                grouped[name][symbol_candle] = row

    for name in VARIANTS:
        variant = state["variants"][name]
        totals = variant["totals"]

        for row in sorted(
            grouped[name].values(),
            key=lambda x: datetime_ms(x.get("candle_timestamp")),
        ):
            position = build_position(
                name,
                row,
                settings,
                measurements.get(candidate_key(row)),
            )
            if position is None:
                continue

            key = position["candidate_key"]
            if key in variant["open_positions"]:
                continue

            variant["open_positions"][key] = position
            totals["accepted_signals"] += 1
            totals["opened"] += 1

    state["processed_signal_keys"] = processed_order[-MAX_SIGNAL_KEYS:]


def update_open_positions(
    state: Dict[str, Any],
    settings: Dict[str, Any],
) -> None:
    all_positions: Dict[str, List[Tuple[str, str, Dict[str, Any]]]] = defaultdict(list)

    for name in VARIANTS:
        for key, position in state["variants"][name]["open_positions"].items():
            all_positions[str(position["symbol"])].append((name, key, position))

    if not all_positions:
        state["last_errors"] = []
        return

    exchange = create_public_exchange()
    errors: List[str] = []
    closed_refs: List[Tuple[str, str]] = []

    for symbol, positions in all_positions.items():
        earliest = min(
            int(position.get("last_checked_candle_ms", 0)) + TIMEFRAME_MS
            for _, _, position in positions
        )

        try:
            candles = fetch_closed_candles(exchange, symbol, earliest)
        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue

        for name, key, position in positions:
            closed = evaluate(position, candles, settings)
            if closed is None:
                continue

            append_trade(closed)

            if name == "SELECTIVE":
                try:
                    append_execution_measurement(closed)
                except Exception as exc:
                    errors.append(
                        f"measurement {key}: {type(exc).__name__}: {exc}"
                    )

            closed_refs.append((name, key))

            totals = state["variants"][name]["totals"]
            totals["closed"] += 1
            totals["net_pnl_eur"] = round(
                to_float(totals.get("net_pnl_eur"))
                + to_float(closed.get("net_pnl_eur")),
                6,
            )
            totals["total_fees_eur"] = round(
                to_float(totals.get("total_fees_eur"))
                + to_float(closed.get("total_fees_eur")),
                6,
            )

            pnl = to_float(closed.get("net_pnl_eur"))
            if pnl > 0.000001:
                totals["wins"] += 1
            elif pnl < -0.000001:
                totals["losses"] += 1
            else:
                totals["neutral"] += 1

    for name, key in closed_refs:
        state["variants"][name]["open_positions"].pop(key, None)

    state["last_errors"] = errors[-20:]


def read_trades() -> List[Dict[str, str]]:
    if not TRADES_FILE.exists() or TRADES_FILE.stat().st_size == 0:
        return []
    with TRADES_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def variant_summary(
    name: str,
    state: Dict[str, Any],
    trades: List[Dict[str, str]],
) -> Dict[str, Any]:
    rows = [r for r in trades if str(r.get("variant") or "") == name]
    closed = len(rows)
    wins = sum(to_float(r.get("net_pnl_eur")) > 0 for r in rows)
    losses = sum(to_float(r.get("net_pnl_eur")) < 0 for r in rows)
    net = sum(to_float(r.get("net_pnl_eur")) for r in rows)
    fees = sum(to_float(r.get("total_fees_eur")) for r in rows)

    gains = sum(max(0.0, to_float(r.get("net_pnl_eur"))) for r in rows)
    loss_sum = sum(abs(min(0.0, to_float(r.get("net_pnl_eur")))) for r in rows)
    if loss_sum > 0:
        pf: Any = round(gains / loss_sum, 4)
    else:
        pf = "inf" if gains > 0 else 0.0

    item = state["variants"][name]
    totals = item["totals"]

    return {
        "accepted_signals": int(totals.get("accepted_signals", 0) or 0),
        "opened": int(totals.get("opened", 0) or 0),
        "open": len(item.get("open_positions") or {}),
        "closed": closed,
        "wins": wins,
        "losses": losses,
        "neutral": closed - wins - losses,
        "winrate_pct": round(wins / closed * 100, 2) if closed else 0.0,
        "net_pnl_eur": round(net, 6),
        "total_fees_eur": round(fees, 6),
        "profit_factor": pf,
        "target": TARGET_CLOSED,
        "remaining": max(0, TARGET_CLOSED - closed),
        "target_reached": closed >= TARGET_CLOSED,
    }


def build_report(state: Dict[str, Any]) -> Dict[str, Any]:
    trades = read_trades()
    summaries = {
        name: variant_summary(name, state, trades)
        for name in VARIANTS
    }

    return {
        "version": VERSION,
        "mode": MODE,
        "generated_at": now_iso(),
        "started_at": state.get("started_at"),
        "target_closed_per_variant": TARGET_CLOSED,
        "eligible_signals_seen": int(state.get("eligible_signals_seen", 0) or 0),
        "variants": summaries,
        "selection_rules": {
            "CURRENT": "alleen huidige scanner shadow_eligible=True",
            "SELECTIVE": (
                "scanner-eligible EN (LONG trend_breakout OF SHORT BEARISH_WEAK "
                "OF SHORT momentum OF SHORT pullback_retest)"
            ),
            "STRONG": (
                "scanner-eligible EN ((trend_breakout en score>=95) "
                "OF (SHORT en BEARISH_WEAK))"
            ),
        },
        "settings": state.get("settings") or {},
        "safety": SAFETY,
        "last_errors": state.get("last_errors") or [],
        "limitations": [
            "Prospectieve signaalkwaliteitstest; geen live- of portefeuillesimulatie.",
            "Alleen signalen die de huidige scanner al als shadow_eligible=True markeert.",
            "Per variant/munt/candle wordt alleen het hoogste-score signaal gebruikt.",
            "Signalen worden onafhankelijk gevolgd; overlap tussen open posities is toegestaan.",
            "Entry-spread wordt conservatief opnieuw gebruikt als exit-spreadproxy.",
            "Bij TP en SL in dezelfde 15m-candle wint de stop-loss.",
        ],
    }


def run_update() -> Dict[str, Any]:
    baseline = ensure_baseline()
    baseline_dt = parse_datetime(baseline.get("started_at"))
    if baseline_dt is None:
        raise ValueError("Ongeldige Selective Shadow baseline")

    settings = load_settings()
    state = load_state(baseline_dt.isoformat())
    state["settings"] = settings

    rows = read_signals()
    ingest(state, rows, baseline_dt, settings)
    update_open_positions(state, settings)

    state["last_update_at"] = now_iso()
    save_json_atomic(STATE_FILE, state)

    report = build_report(state)
    save_json_atomic(REPORT_FILE, report)
    return report


def load_report() -> Dict[str, Any]:
    return load_json(REPORT_FILE, {})


def print_report(report: Dict[str, Any]) -> None:
    if not report:
        print("Selective Shadow heeft nog geen rapport. Voer --update uit.")
        return

    print("=" * 82)
    print(" DIAMOND TRADER SCANNER SELECTIVE SHADOW LAB")
    print("=" * 82)
    print(f"Versie                 : {report.get('version', '-')}")
    print(f"Modus                  : {report.get('mode', '-')}")
    print(f"Gestart                : {report.get('started_at', '-')}")
    print(f"Laatste update         : {report.get('generated_at', '-')}")
    print(f"Scanner-eligible gezien: {int(report.get('eligible_signals_seen', 0) or 0)}")
    print()

    print("VARIANTEN")
    print("-" * 82)
    variants = report.get("variants") or {}

    for name in VARIANTS:
        item = variants.get(name) or {}
        print(
            f"{name:<10} "
            f"accepted={int(item.get('accepted_signals',0) or 0):3d} "
            f"closed={int(item.get('closed',0) or 0):2d}/{TARGET_CLOSED} "
            f"W/L={int(item.get('wins',0) or 0):2d}/{int(item.get('losses',0) or 0):2d} "
            f"open={int(item.get('open',0) or 0):2d} "
            f"pnl=€{to_float(item.get('net_pnl_eur')):+8.4f} "
            f"PF={item.get('profit_factor',0)}"
        )

    current = variants.get("CURRENT") or {}
    selective = variants.get("SELECTIVE") or {}
    strong = variants.get("STRONG") or {}

    print()
    print("VERSCHILLEN")
    print("-" * 82)
    print(
        f"SELECTIVE vs CURRENT  : "
        f"delta_pnl=€{to_float(selective.get('net_pnl_eur')) - to_float(current.get('net_pnl_eur')):+.4f}"
    )
    print(
        f"STRONG vs CURRENT     : "
        f"delta_pnl=€{to_float(strong.get('net_pnl_eur')) - to_float(current.get('net_pnl_eur')):+.4f}"
    )

    print()
    print("VEILIGHEID")
    print("-" * 82)
    print("Orders mogelijk        : NEE")
    print("Private API            : NEE")
    print("Scannerfilters gewijzigd: NEE")
    print("Bot/config gewijzigd   : NEE")

    errors = report.get("last_errors") or []
    if errors:
        print()
        print("LAATSTE FOUTEN")
        print("-" * 82)
        for error in errors[-5:]:
            print("-", error)

    print("=" * 82)


def self_test() -> None:
    base = {
        "detected_at": "2026-08-04T10:01:00+00:00",
        "candle_timestamp": "2026-08-04T09:45:00+00:00",
        "symbol": "TEST/EUR",
        "strategy": "trend_breakout",
        "side": "LONG",
        "market_regime": "BULLISH",
        "score": "96",
        "entry_price": "100",
        "take_profit": "103",
        "stop_loss": "98",
        "spread_pct": "0.10",
        "reward_risk": "1.30",
        "shadow_eligible": "True",
    }

    assert variant_accepts("CURRENT", base)
    assert variant_accepts("SELECTIVE", base)
    assert variant_accepts("STRONG", base)

    long_momentum = {**base, "strategy": "momentum"}
    assert variant_accepts("CURRENT", long_momentum)
    assert not variant_accepts("SELECTIVE", long_momentum)
    assert not variant_accepts("STRONG", long_momentum)

    short_weak = {
        **base,
        "side": "SHORT",
        "strategy": "pullback_retest",
        "market_regime": "BEARISH_WEAK",
        "score": "90",
        "take_profit": "97",
        "stop_loss": "102",
    }
    assert variant_accepts("SELECTIVE", short_weak)
    assert variant_accepts("STRONG", short_weak)

    rejected = {**base, "shadow_eligible": "False"}
    assert not variant_accepts("CURRENT", rejected)
    assert not variant_accepts("SELECTIVE", rejected)
    assert not variant_accepts("STRONG", rejected)

    settings = {
        "stake_eur": 120.0,
        "fee_pct_per_side": 0.25,
        "max_hold_minutes": 2880,
    }
    position = build_position("CURRENT", base, settings)
    assert position is not None

    entry_ms = int(position["entry_candle_timestamp_ms"])
    both_hit = [[
        entry_ms + TIMEFRAME_MS,
        100.0,
        104.0,
        97.0,
        101.0,
        1.0,
    ]]
    closed = evaluate(position, both_hit, settings)
    assert closed is not None
    assert closed["exit_reason"] == "stop_loss"

    assert SAFETY["orders_possible"] is False
    assert SAFETY["private_exchange_calls"] is False
    assert SAFETY["api_keys_passed_to_exchange"] is False
    assert SAFETY["automatic_live_changes"] is False

    print("SCANNER_SELECTIVE_SHADOW_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diamond Trader Scanner Selective Shadow Lab"
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--no-print", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    if args.update:
        report = run_update()
        if not args.no_print:
            print_report(report)
        return 0

    if args.status:
        print_report(load_report())
        return 0

    print_report(load_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
