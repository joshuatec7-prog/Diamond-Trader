#!/usr/bin/env python3
"""
Diamond Trader Shadow V2 Signal Lab v2.0

Doel
----
De oorspronkelijke Shadow V2-filter keek alleen naar reeds gesloten
schaduwtrades van de Market Scanner. Daardoor bleef de test op 0/20 staan
wanneer de scanner wel signalen vond, maar die door score/spread/risico-winst
niet als bestaande schaduwtrade opende.

Deze v2.0 volgt daarom de SIGNALEN zelf uit:
    /var/data/diamond_market_signals.csv

V2-regels
---------
- alleen trend_breakout en range_breakout;
- PUMP/EUR en SHIB/EUR uitsluiten;
- originele scanner-afwijzingen (zoals RR/spread) worden NIET gebruikt om de
  V2-signaalsimulatie te blokkeren; ze worden wel bewaard voor analyse;
- ieder geselecteerd uniek signaal wordt onafhankelijk virtueel gevolgd;
- TP/SL-afstanden komen rechtstreeks uit het originele scannersignaal;
- conservatief: als TP en SL in dezelfde 15m-candle worden geraakt, telt SL;
- maximale houdtijd blijft standaard 2880 minuten (48 uur), gelijk aan de
  scannerinstelling tenzij config.yaml iets anders opgeeft.

Veiligheid
----------
- geen orders;
- geen private exchange-methoden;
- geen API-sleutels worden aan ccxt doorgegeven;
- diamond_state.json en diamond_transactions.csv worden nooit gewijzigd;
- Market Scanner-state wordt nooit gewijzigd;
- uitsluitend eigen Shadow V2-bestanden in /var/data worden geschreven.

Gebruik
-------
    python3 shadow_v2_filter.py --self-test
    python3 shadow_v2_filter.py --update
    python3 shadow_v2_filter.py --status

De bestaande baseline /var/data/diamond_shadow_v2_baseline.json wordt
hergebruikt. Daardoor worden signalen vanaf de oorspronkelijke Shadow V2-start
gereconstrueerd in plaats van opnieuw vanaf nul te beginnen.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "2.0"
MODE = "READ_ONLY_SHADOW_V2_SIGNAL_LAB"

PROJECT_DIR = Path(os.getenv("DIAMOND_PROJECT_DIR", "/opt/render/project/src"))
DATA_DIR = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
CONFIG_FILE = Path(os.getenv("CFG_FILE", str(PROJECT_DIR / "config.yaml")))
SIGNALS_FILE = DATA_DIR / "diamond_market_signals.csv"
BASELINE_FILE = DATA_DIR / "diamond_shadow_v2_baseline.json"
STATE_FILE = DATA_DIR / "diamond_shadow_v2_signal_state.json"
REPORT_FILE = DATA_DIR / "diamond_shadow_v2_report.json"
TRADES_FILE = DATA_DIR / "diamond_shadow_v2_signal_trades.csv"

TARGET_TRADES = 20
ALLOWED_STRATEGIES = {"trend_breakout", "range_breakout"}
EXCLUDED_SYMBOLS = {"PUMP/EUR", "SHIB/EUR"}
TIMEFRAME_MS = 15 * 60 * 1000
MAX_SIGNAL_KEYS = 20_000

TRADE_HEADER = [
    "candidate_key",
    "detected_at",
    "opened_at",
    "closed_at",
    "symbol",
    "strategy",
    "side",
    "market_regime",
    "signal_score",
    "original_shadow_eligible",
    "original_shadow_rejection_reasons",
    "original_reward_risk",
    "original_spread_pct",
    "entry_price",
    "exit_price",
    "stake_eur",
    "amount",
    "entry_fee_eur",
    "exit_fee_eur",
    "total_fees_eur",
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
    "diamond_state_modified": False,
    "diamond_transactions_modified": False,
    "market_scanner_state_modified": False,
    "strategy_settings_modified": False,
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
    if dt is None:
        return 0
    return int(dt.timestamp() * 1000)


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
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
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ja", "on"}:
        return True
    if text in {"0", "false", "no", "nee", "off"}:
        return False
    return default


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return default.copy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default.copy()
    except Exception:
        return default.copy()


def save_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
    ) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        temp_name = tmp.name
    os.replace(temp_name, path)


def load_config_settings() -> Dict[str, Any]:
    settings = {
        "stake_eur": 120.0,
        "fee_pct_per_side": 0.25,
        "max_hold_minutes": 2880,
    }

    if not CONFIG_FILE.exists():
        return settings

    try:
        import yaml

        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return settings

        market_scanner = data.get("market_scanner") or {}
        risk = data.get("risk") or {}
        fees = data.get("fees") or {}

        if not isinstance(market_scanner, dict):
            market_scanner = {}
        if not isinstance(risk, dict):
            risk = {}
        if not isinstance(fees, dict):
            fees = {}

        settings["stake_eur"] = max(
            5.0,
            to_float(
                market_scanner.get("stake_eur", risk.get("fixed_stake_quote", 120)),
                120.0,
            ),
        )
        settings["fee_pct_per_side"] = max(
            0.0,
            to_float(
                market_scanner.get("fee_pct_per_side", fees.get("taker_fee_pct", 0.25)),
                0.25,
            ),
        )
        settings["max_hold_minutes"] = max(
            60,
            to_int(market_scanner.get("max_hold_minutes", 2880), 2880),
        )

    except Exception:
        # Config-read is alleen voor simulatieparameters; veilige defaults blijven geldig.
        pass

    return settings


def default_baseline() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "mode": MODE,
        "started_at": now_iso(),
        "target_trades": TARGET_TRADES,
        "rules": {
            "allowed_strategies": sorted(ALLOWED_STRATEGIES),
            "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
        },
        "safety": SAFETY,
    }


def ensure_baseline() -> Dict[str, Any]:
    baseline = load_json(BASELINE_FILE, {})
    if not baseline.get("started_at"):
        baseline = default_baseline()
        save_json_atomic(BASELINE_FILE, baseline)
    return baseline


def default_totals() -> Dict[str, Any]:
    return {
        "signals_seen_since_baseline": 0,
        "candidate_signals": 0,
        "skipped_strategy": 0,
        "skipped_symbol": 0,
        "invalid_candidates": 0,
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
        "last_signal_file_mtime": None,
        "processed_signal_keys": [],
        "open_positions": {},
        "totals": default_totals(),
        "last_errors": [],
        "settings": {},
        "rules": {
            "allowed_strategies": sorted(ALLOWED_STRATEGIES),
            "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
            "original_scanner_rejections_block_v2": False,
            "independent_signal_simulation": True,
        },
        "safety": SAFETY,
    }


def load_state(started_at: str) -> Dict[str, Any]:
    state = load_json(STATE_FILE, default_state(started_at))
    state.setdefault("processed_signal_keys", [])
    state.setdefault("open_positions", {})
    state.setdefault("totals", default_totals())
    state.setdefault("last_errors", [])
    state.setdefault("rules", {})
    state["version"] = VERSION
    state["mode"] = MODE
    state["started_at"] = started_at
    state["rules"].update({
        "allowed_strategies": sorted(ALLOWED_STRATEGIES),
        "excluded_symbols": sorted(EXCLUDED_SYMBOLS),
        "original_scanner_rejections_block_v2": False,
        "independent_signal_simulation": True,
    })
    state["safety"] = SAFETY

    totals = state["totals"]
    for key, value in default_totals().items():
        totals.setdefault(key, value)

    if not isinstance(state["processed_signal_keys"], list):
        state["processed_signal_keys"] = []
    if not isinstance(state["open_positions"], dict):
        state["open_positions"] = {}

    return state


def read_signal_rows() -> List[Dict[str, str]]:
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
        "entry_price",
        "take_profit",
        "stop_loss",
        "spread_pct",
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


def row_after_baseline(row: Dict[str, str], baseline_dt: datetime) -> bool:
    detected = parse_datetime(row.get("detected_at"))
    return detected is not None and detected >= baseline_dt


def selection_reason(row: Dict[str, str]) -> Optional[str]:
    symbol = str(row.get("symbol") or "").upper()
    strategy = str(row.get("strategy") or "")

    if symbol in EXCLUDED_SYMBOLS:
        return "excluded_symbol"
    if strategy not in ALLOWED_STRATEGIES:
        return "excluded_strategy"
    return None


def build_position(
    row: Dict[str, str],
    settings: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
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
        entry_price = raw_entry * (1.0 + half_spread)
    else:
        entry_price = raw_entry * (1.0 - half_spread)

    # De originele TP/SL-afstanden blijven exact gelijk; alleen dezelfde
    # half-spread entrycorrectie als de bestaande Market Scanner wordt toegepast.
    delta = entry_price - raw_entry
    take_profit = raw_tp + delta
    stop_loss = raw_sl + delta

    stake = float(settings["stake_eur"])
    fee_pct = float(settings["fee_pct_per_side"])
    amount = stake / entry_price
    entry_fee = stake * fee_pct / 100.0

    return {
        "candidate_key": candidate_key(row),
        "detected_at": str(row.get("detected_at") or ""),
        "opened_at": str(row.get("detected_at") or now_iso()),
        "symbol": str(row.get("symbol") or "").upper(),
        "strategy": str(row.get("strategy") or ""),
        "side": side,
        "market_regime": str(row.get("market_regime") or "-"),
        "signal_score": to_float(row.get("score"), 0.0),
        "original_shadow_eligible": to_bool(row.get("shadow_eligible"), False),
        "original_shadow_rejection_reasons": str(
            row.get("shadow_rejection_reasons") or ""
        ),
        "original_reward_risk": to_float(row.get("reward_risk"), 0.0),
        "original_spread_pct": spread,
        "entry_price": entry_price,
        "amount": amount,
        "stake_eur": stake,
        "entry_fee_eur": entry_fee,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "entry_candle_timestamp_ms": candle_ms,
        "last_checked_candle_ms": candle_ms,
    }


def append_trade(row: Dict[str, Any]) -> None:
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not TRADES_FILE.exists() or TRADES_FILE.stat().st_size == 0

    with TRADES_FILE.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_HEADER)
        if needs_header:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in TRADE_HEADER})


def close_position_row(
    position: Dict[str, Any],
    raw_exit_price: float,
    exit_reason: str,
    exit_candle_ms: int,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    spread = float(position["original_spread_pct"])
    half_spread = spread / 200.0

    if position["side"] == "LONG":
        exit_price = raw_exit_price * (1.0 - half_spread)
        gross_pnl = (
            exit_price - float(position["entry_price"])
        ) * float(position["amount"])
    else:
        exit_price = raw_exit_price * (1.0 + half_spread)
        gross_pnl = (
            float(position["entry_price"]) - exit_price
        ) * float(position["amount"])

    exit_notional = float(position["amount"]) * exit_price
    exit_fee = exit_notional * float(settings["fee_pct_per_side"]) / 100.0
    total_fees = float(position["entry_fee_eur"]) + exit_fee
    net_pnl = gross_pnl - total_fees
    stake = float(position["stake_eur"])
    return_pct = net_pnl / stake * 100.0 if stake > 0 else 0.0
    duration = max(
        0.0,
        (exit_candle_ms - int(position["entry_candle_timestamp_ms"])) / 60_000,
    )

    closed_at = datetime.fromtimestamp(
        exit_candle_ms / 1000,
        tz=timezone.utc,
    ).isoformat()

    return {
        **position,
        "closed_at": closed_at,
        "exit_price": round(exit_price, 12),
        "exit_fee_eur": round(exit_fee, 6),
        "total_fees_eur": round(total_fees, 6),
        "exit_reason": exit_reason,
        "gross_pnl_eur": round(gross_pnl, 6),
        "net_pnl_eur": round(net_pnl, 6),
        "return_pct": round(return_pct, 6),
        "duration_minutes": round(duration, 2),
        "exit_candle_timestamp_ms": exit_candle_ms,
        "exit_spread_assumption": "entry_spread_reused",
    }


def evaluate_position(
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

        if position["side"] == "LONG":
            stop_hit = low <= float(position["stop_loss"])
            target_hit = high >= float(position["take_profit"])
        else:
            stop_hit = high >= float(position["stop_loss"])
            target_hit = low <= float(position["take_profit"])

        # Conservatief: SL wint bij een candle waarin beide niveaus geraakt zijn.
        if stop_hit:
            return close_position_row(
                position,
                float(position["stop_loss"]),
                "stop_loss",
                candle_ms,
                settings,
            )

        if target_hit:
            return close_position_row(
                position,
                float(position["take_profit"]),
                "take_profit",
                candle_ms,
                settings,
            )

        held_minutes = (
            candle_ms - int(position["entry_candle_timestamp_ms"])
        ) / 60_000

        if held_minutes >= int(settings["max_hold_minutes"]) and close > 0:
            return close_position_row(
                position,
                close,
                "time_exit",
                candle_ms,
                settings,
            )

    return None


def create_public_exchange() -> Any:
    # Lazy import houdt --status en --self-test licht in geheugen.
    import ccxt

    exchange = ccxt.bitvavo({
        "enableRateLimit": True,
        "timeout": 30_000,
    })
    exchange.load_markets()
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


def rejection_category(text: str) -> str:
    value = (text or "").lower()
    if not value:
        return "geen"
    if "risico/winst" in value:
        return "risico_winst"
    if "spread" in value:
        return "spread"
    if "score" in value:
        return "score"
    if "nettoverwachting" in value:
        return "nettoverwachting"
    if "verwachte winst" in value:
        return "verwachte_winst"
    return "overig"


def ingest_signals(
    state: Dict[str, Any],
    rows: List[Dict[str, str]],
    baseline_dt: datetime,
    settings: Dict[str, Any],
) -> None:
    processed = {str(item) for item in state["processed_signal_keys"]}
    totals = state["totals"]

    # Chronologisch verwerken. Als meerdere V2-strategieën op exact dezelfde
    # munt/candle staan, bewaren we alleen de hoogste score om dubbele sterk
    # gecorreleerde kandidaten te vermijden.
    eligible_rows: Dict[Tuple[str, str], Dict[str, str]] = {}

    for row in rows:
        if not row_after_baseline(row, baseline_dt):
            continue

        key = candidate_key(row)
        if key in processed:
            continue

        processed.add(key)
        totals["signals_seen_since_baseline"] += 1

        reason = selection_reason(row)
        if reason == "excluded_symbol":
            totals["skipped_symbol"] += 1
            continue
        if reason == "excluded_strategy":
            totals["skipped_strategy"] += 1
            continue

        group_key = (
            str(row.get("symbol") or "").upper(),
            str(row.get("candle_timestamp") or ""),
        )
        previous = eligible_rows.get(group_key)
        if previous is None or to_float(row.get("score"), 0.0) > to_float(
            previous.get("score"), 0.0
        ):
            eligible_rows[group_key] = row

    for row in sorted(
        eligible_rows.values(),
        key=lambda item: datetime_ms(item.get("candle_timestamp")),
    ):
        position = build_position(row, settings)
        if position is None:
            totals["invalid_candidates"] += 1
            continue

        key = position["candidate_key"]
        if key in state["open_positions"]:
            continue

        state["open_positions"][key] = position
        totals["candidate_signals"] += 1
        totals["opened"] += 1

    state["processed_signal_keys"] = list(processed)[-MAX_SIGNAL_KEYS:]


def update_open_positions(
    state: Dict[str, Any],
    settings: Dict[str, Any],
) -> None:
    if not state["open_positions"]:
        return

    exchange = create_public_exchange()
    errors: List[str] = []

    by_symbol: Dict[str, List[Tuple[str, Dict[str, Any]]]] = defaultdict(list)
    for key, position in state["open_positions"].items():
        by_symbol[str(position["symbol"])].append((key, position))

    closed_keys: List[str] = []

    for symbol, positions in by_symbol.items():
        earliest = min(
            int(position.get("last_checked_candle_ms", 0)) + TIMEFRAME_MS
            for _, position in positions
        )

        try:
            candles = fetch_closed_candles(exchange, symbol, earliest)
        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue

        for key, position in positions:
            closed = evaluate_position(position, candles, settings)
            if closed is None:
                continue

            append_trade(closed)
            closed_keys.append(key)

            totals = state["totals"]
            totals["closed"] += 1
            totals["net_pnl_eur"] = round(
                to_float(totals.get("net_pnl_eur"), 0.0)
                + to_float(closed.get("net_pnl_eur"), 0.0),
                6,
            )
            totals["total_fees_eur"] = round(
                to_float(totals.get("total_fees_eur"), 0.0)
                + to_float(closed.get("total_fees_eur"), 0.0),
                6,
            )

            pnl = to_float(closed.get("net_pnl_eur"), 0.0)
            if pnl > 0.000001:
                totals["wins"] += 1
            elif pnl < -0.000001:
                totals["losses"] += 1
            else:
                totals["neutral"] += 1

    for key in closed_keys:
        state["open_positions"].pop(key, None)

    state["last_errors"] = errors[-20:]


def read_trades() -> List[Dict[str, str]]:
    if not TRADES_FILE.exists() or TRADES_FILE.stat().st_size == 0:
        return []
    with TRADES_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def grouped_summary(rows: List[Dict[str, str]], key_name: str) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "losses": 0, "net_pnl_eur": 0.0}
    )

    for row in rows:
        key = str(row.get(key_name) or "-")
        pnl = to_float(row.get("net_pnl_eur"), 0.0)
        group = groups[key]
        group["trades"] += 1
        group["wins"] += int(pnl > 0)
        group["losses"] += int(pnl < 0)
        group["net_pnl_eur"] += pnl

    result: Dict[str, Any] = {}
    for key, group in sorted(groups.items()):
        trades = group["trades"]
        result[key] = {
            **group,
            "winrate_pct": round(group["wins"] / trades * 100, 2) if trades else 0.0,
            "net_pnl_eur": round(group["net_pnl_eur"], 6),
        }
    return result


def build_report(state: Dict[str, Any]) -> Dict[str, Any]:
    trades = read_trades()
    closed = len(trades)
    wins = sum(1 for row in trades if to_float(row.get("net_pnl_eur"), 0.0) > 0)
    losses = sum(1 for row in trades if to_float(row.get("net_pnl_eur"), 0.0) < 0)
    net_pnl = sum(to_float(row.get("net_pnl_eur"), 0.0) for row in trades)
    fees = sum(to_float(row.get("total_fees_eur"), 0.0) for row in trades)
    gross_profit = sum(
        max(0.0, to_float(row.get("net_pnl_eur"), 0.0)) for row in trades
    )
    gross_loss = sum(
        min(0.0, to_float(row.get("net_pnl_eur"), 0.0)) for row in trades
    )
    profit_factor: Any = (
        round(gross_profit / abs(gross_loss), 4)
        if gross_loss < 0
        else ("inf" if gross_profit > 0 else 0.0)
    )

    rejection_groups: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "losses": 0, "net_pnl_eur": 0.0}
    )
    for row in trades:
        cat = rejection_category(str(row.get("original_shadow_rejection_reasons") or ""))
        pnl = to_float(row.get("net_pnl_eur"), 0.0)
        group = rejection_groups[cat]
        group["trades"] += 1
        group["wins"] += int(pnl > 0)
        group["losses"] += int(pnl < 0)
        group["net_pnl_eur"] += pnl

    rejection_summary = {}
    for key, value in sorted(rejection_groups.items()):
        n = value["trades"]
        rejection_summary[key] = {
            **value,
            "winrate_pct": round(value["wins"] / n * 100, 2) if n else 0.0,
            "net_pnl_eur": round(value["net_pnl_eur"], 6),
        }

    original_rejected = sum(
        1 for row in trades if not to_bool(row.get("original_shadow_eligible"), False)
    )

    return {
        "version": VERSION,
        "mode": MODE,
        "generated_at": now_iso(),
        "started_at": state.get("started_at"),
        "target_trades": TARGET_TRADES,
        "progress": {
            "closed_trades": closed,
            "target": TARGET_TRADES,
            "remaining": max(0, TARGET_TRADES - closed),
            "progress_pct": round(min(1.0, closed / TARGET_TRADES) * 100, 1),
            "target_reached": closed >= TARGET_TRADES,
            "open_candidates": len(state.get("open_positions") or {}),
        },
        "signals": {
            **state.get("totals", {}),
            "processed_unique_keys": len(state.get("processed_signal_keys") or []),
        },
        "summary": {
            "trades": closed,
            "wins": wins,
            "losses": losses,
            "neutral": closed - wins - losses,
            "winrate_pct": round(wins / closed * 100, 2) if closed else 0.0,
            "net_pnl_eur": round(net_pnl, 6),
            "total_fees_eur": round(fees, 6),
            "average_pnl_eur": round(net_pnl / closed, 6) if closed else 0.0,
            "profit_factor": profit_factor,
            "originally_rejected_trades": original_rejected,
            "by_strategy": grouped_summary(trades, "strategy"),
            "by_original_rejection": rejection_summary,
        },
        "rules": state.get("rules"),
        "settings": state.get("settings"),
        "safety": SAFETY,
        "last_errors": state.get("last_errors", []),
        "limitations": [
            "Dit is een onafhankelijke signaalkwaliteitstest, geen portefeuillesimulatie.",
            "Originele scanner-afwijzingen worden geregistreerd maar blokkeren V2 niet.",
            "Voor historische exits wordt de entry-spread opnieuw gebruikt als exit-spreadproxy.",
            "Bij TP en SL in dezelfde 15m-candle wordt conservatief stop-loss aangenomen.",
        ],
    }


def run_update() -> Dict[str, Any]:
    baseline = ensure_baseline()
    baseline_dt = parse_datetime(baseline.get("started_at"))
    if baseline_dt is None:
        raise ValueError("Shadow V2-baseline heeft geen geldige started_at")

    settings = load_config_settings()
    state = load_state(baseline_dt.isoformat())
    state["settings"] = settings

    rows = read_signal_rows()
    ingest_signals(state, rows, baseline_dt, settings)
    update_open_positions(state, settings)

    state["last_update_at"] = now_iso()
    if SIGNALS_FILE.exists():
        state["last_signal_file_mtime"] = datetime.fromtimestamp(
            SIGNALS_FILE.stat().st_mtime,
            tz=timezone.utc,
        ).isoformat()

    save_json_atomic(STATE_FILE, state)
    report = build_report(state)
    save_json_atomic(REPORT_FILE, report)
    return report


def load_report() -> Dict[str, Any]:
    return load_json(REPORT_FILE, {})


def print_report(report: Dict[str, Any]) -> None:
    if not report:
        print("Shadow V2 Signal Lab heeft nog geen rapport. Voer --update uit.")
        return

    progress = report.get("progress") or {}
    summary = report.get("summary") or {}
    signals = report.get("signals") or {}

    print("=" * 72)
    print(" DIAMOND TRADER SHADOW V2 SIGNAL LAB")
    print("=" * 72)
    print(f"Versie                 : {report.get('version', '-')}")
    print(f"Modus                  : {report.get('mode', '-')}")
    print(f"Gestart                : {report.get('started_at', '-')}")
    print(f"Laatste update         : {report.get('generated_at', '-')}")
    print()
    print("SIGNAALSTROOM")
    print("-" * 72)
    print(f"Gezien sinds baseline  : {int(signals.get('signals_seen_since_baseline', 0) or 0)}")
    print(f"V2-kandidaten          : {int(signals.get('candidate_signals', 0) or 0)}")
    print(f"Overgeslagen strategie: {int(signals.get('skipped_strategy', 0) or 0)}")
    print(f"Overgeslagen munt      : {int(signals.get('skipped_symbol', 0) or 0)}")
    print(f"Open kandidaten        : {int(progress.get('open_candidates', 0) or 0)}")
    print(f"Gesloten / doel        : {int(progress.get('closed_trades', 0) or 0)}/{int(progress.get('target', TARGET_TRADES) or TARGET_TRADES)}")
    print()
    print("RESULTAAT")
    print("-" * 72)
    print(f"Winst / verlies        : {int(summary.get('wins', 0) or 0)} / {int(summary.get('losses', 0) or 0)}")
    print(f"Winrate                : {to_float(summary.get('winrate_pct'), 0.0):.2f}%")
    print(f"Nettoresultaat         : €{to_float(summary.get('net_pnl_eur'), 0.0):+.4f}")
    print(f"Profit factor          : {summary.get('profit_factor', 0.0)}")
    print(f"Kosten                 : €{to_float(summary.get('total_fees_eur'), 0.0):.4f}")
    print(f"Origineel afgewezen    : {int(summary.get('originally_rejected_trades', 0) or 0)} gesloten V2-trades")
    print()
    print("V2-REGELS")
    print("-" * 72)
    print(f"Strategieën            : {', '.join(sorted(ALLOWED_STRATEGIES))}")
    print(f"Uitgesloten munten     : {', '.join(sorted(EXCLUDED_SYMBOLS))}")
    print("Scanner-afwijzing blokt: NEE (alleen geregistreerd voor vergelijking)")
    print()
    print("VEILIGHEID")
    print("-" * 72)
    print("Orders mogelijk        : NEE")
    print("Private API gebruikt   : NEE")
    print("Bot/scanner gewijzigd  : NEE")

    errors = report.get("last_errors") or []
    if errors:
        print()
        print("LAATSTE FOUTEN")
        print("-" * 72)
        for error in errors[-5:]:
            print(f"- {error}")

    print("=" * 72)


def self_test() -> None:
    settings = {
        "stake_eur": 120.0,
        "fee_pct_per_side": 0.25,
        "max_hold_minutes": 2880,
    }

    base = {
        "detected_at": "2026-08-01T10:01:00+00:00",
        "candle_timestamp": "2026-08-01T09:45:00+00:00",
        "symbol": "UNI/EUR",
        "strategy": "trend_breakout",
        "side": "LONG",
        "market_regime": "BULLISH",
        "score": "95.0",
        "entry_price": "100",
        "take_profit": "104",
        "stop_loss": "98",
        "spread_pct": "0.10",
        "reward_risk": "0.9",
        "shadow_eligible": "False",
        "shadow_rejection_reasons": "risico/winst 0.900 lager dan 1.200",
    }

    assert selection_reason(base) is None
    assert selection_reason({**base, "symbol": "PUMP/EUR"}) == "excluded_symbol"
    assert selection_reason({**base, "strategy": "momentum"}) == "excluded_strategy"

    position = build_position(base, settings)
    assert position is not None
    assert position["original_shadow_eligible"] is False

    entry_ms = int(position["entry_candle_timestamp_ms"])
    # Candle raakt alleen TP.
    candles = [[entry_ms + TIMEFRAME_MS, 100, 105, 99, 104.5, 1]]
    closed = evaluate_position(position.copy(), candles, settings)
    assert closed is not None
    assert closed["exit_reason"] == "take_profit"

    # Als TP en SL in dezelfde candle geraakt worden, moet SL winnen.
    position2 = build_position(base, settings)
    assert position2 is not None
    both = [[entry_ms + TIMEFRAME_MS, 100, 105, 97, 100, 1]]
    closed2 = evaluate_position(position2, both, settings)
    assert closed2 is not None
    assert closed2["exit_reason"] == "stop_loss"

    assert SAFETY["orders_possible"] is False
    assert SAFETY["private_exchange_calls"] is False
    print("SHADOW_V2_SIGNAL_LAB_SELF_TEST_OK")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diamond Trader Shadow V2 Signal Lab"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Verwerk nieuwe scannersignalen en werk virtuele V2-posities bij.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Toon alleen het laatst opgeslagen rapport; gebruikt geen netwerk.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Interne test zonder netwerk of /var/data-wijzigingen.",
    )
    parser.add_argument(
        "--no-print",
        action="store_true",
        help="Werk bij zonder volledig rapport naar stdout.",
    )
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    if args.status:
        print_report(load_report())
        return

    # Default is update, zodat handmatig `python3 shadow_v2_filter.py` ook werkt.
    report = run_update()
    if not args.no_print:
        print_report(report)
    else:
        progress = report.get("progress") or {}
        summary = report.get("summary") or {}
        print(
            "Shadow V2 bijgewerkt | "
            f"closed={progress.get('closed_trades', 0)}/{TARGET_TRADES} | "
            f"open={progress.get('open_candidates', 0)} | "
            f"pnl=€{to_float(summary.get('net_pnl_eur'), 0.0):+.4f}"
        )


if __name__ == "__main__":
    main()
