#!/usr/bin/env python3
"""
Diamond Trader RR 0.80-1.20 Shadow Analysis v1.0

Doel
----
Onderzoekt uitsluitend scannersignalen die door de bestaande Market Scanner
zijn afgewezen ALLEEN omdat reward/risk lager was dan 1.20, terwijl RR wel
tussen 0.80 en 1.20 lag.

Veiligheid
----------
- plaatst nooit orders;
- gebruikt geen API-sleutels en geen private API;
- wijzigt config.yaml niet;
- wijzigt diamond_state.json niet;
- wijzigt diamond_transactions.csv niet;
- wijzigt Market Scanner-state niet;
- schrijft geen bestanden in /var/data;
- is uitsluitend een eenmalige, read-only analyse.

Simulatie
---------
- gebruikt het originele signaal-entry-, TP- en SL-niveau;
- gebruikt dezelfde half-spread entrycorrectie als Shadow V2;
- gebruikt de entry-spread opnieuw als conservatieve exit-spreadproxy;
- rekent taker-kosten per zijde mee;
- maximaal 48 uur vasthouden, tenzij config.yaml anders aangeeft;
- als TP en SL in dezelfde 15m-candle worden geraakt, telt stop-loss;
- ieder uniek signaal wordt onafhankelijk beoordeeld; dit is geen
  portefeuillesimulatie.

Gebruik
-------
    python3 rr_080_120_shadow_analysis.py --self-test
    python3 rr_080_120_shadow_analysis.py
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ccxt
import yaml


VERSION = "1.0"
MODE = "READ_ONLY_RR_080_120_ANALYSIS"

PROJECT_DIR = Path("/opt/render/project/src")
DATA_DIR = Path("/var/data")
CONFIG_FILE = PROJECT_DIR / "config.yaml"
SIGNALS_FILE = DATA_DIR / "diamond_market_signals.csv"
PERIODIC_STATE_FILE = DATA_DIR / "diamond_periodic_analysis_state.json"

RR_MIN = 0.80
RR_MAX = 1.20
TIMEFRAME = "15m"
TIMEFRAME_MS = 15 * 60 * 1000

SAFETY = {
    "orders_possible": False,
    "private_api": False,
    "api_keys_used": False,
    "config_modified": False,
    "bot_state_modified": False,
    "transactions_modified": False,
    "scanner_state_modified": False,
    "var_data_writes": False,
}


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        return number if math.isfinite(number) else default
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


def load_settings() -> Dict[str, Any]:
    settings = {
        "stake_eur": 120.0,
        "fee_pct_per_side": 0.25,
        "max_hold_minutes": 2880,
    }

    if not CONFIG_FILE.exists():
        return settings

    try:
        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        scanner = data.get("market_scanner") or {}
        risk = data.get("risk") or {}
        fees = data.get("fees") or {}

        settings["stake_eur"] = max(
            5.0,
            to_float(
                scanner.get("stake_eur", risk.get("fixed_stake_quote", 120.0)),
                120.0,
            ),
        )
        settings["fee_pct_per_side"] = max(
            0.0,
            to_float(
                scanner.get(
                    "fee_pct_per_side",
                    fees.get("taker_fee_pct", 0.25),
                ),
                0.25,
            ),
        )
        settings["max_hold_minutes"] = max(
            60,
            int(to_float(scanner.get("max_hold_minutes", 2880), 2880)),
        )
    except Exception:
        pass

    return settings


def read_signals() -> List[Dict[str, str]]:
    if not SIGNALS_FILE.exists():
        raise FileNotFoundError(f"Bronbestand ontbreekt: {SIGNALS_FILE}")

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
        "score",
        "entry_price",
        "take_profit",
        "stop_loss",
        "spread_pct",
        "reward_risk",
        "expected_profit_eur",
        "shadow_eligible",
        "shadow_rejection_reasons",
    }
    missing = required - set(rows[0].keys())
    if missing:
        raise ValueError(
            "Signalenbestand mist kolommen: " + ", ".join(sorted(missing))
        )

    return rows


def rejection_parts(row: Dict[str, str]) -> List[str]:
    raw = str(row.get("shadow_rejection_reasons") or "").strip()
    return [part.strip() for part in raw.split("|") if part.strip()]


def rr_only_candidate(row: Dict[str, str]) -> bool:
    if to_bool(row.get("shadow_eligible"), False):
        return False

    rr = to_float(row.get("reward_risk"), -1.0)
    if not (RR_MIN <= rr < RR_MAX):
        return False

    parts = rejection_parts(row)
    if not parts:
        return False

    # Alleen kandidaat wanneer risico/winst de enige originele blokkade was.
    return all(part.lower().startswith("risico/winst") for part in parts)


def candidate_key(row: Dict[str, str]) -> str:
    return "|".join([
        str(row.get("symbol") or "").upper(),
        str(row.get("strategy") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("candle_timestamp") or ""),
    ])


def select_candidates(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    selected: Dict[str, Dict[str, str]] = {}

    for row in rows:
        if not rr_only_candidate(row):
            continue

        key = candidate_key(row)
        # Bij dubbele detectie van hetzelfde candle-signaal de eerste bewaren.
        selected.setdefault(key, row)

    return sorted(
        selected.values(),
        key=lambda r: datetime_ms(r.get("candle_timestamp")),
    )


def create_public_exchange() -> ccxt.Exchange:
    exchange = ccxt.bitvavo({
        "enableRateLimit": True,
        "timeout": 30_000,
        "options": {
            "fetchMarkets": {
                "types": ["spot"],
            },
        },
    })
    exchange.load_markets()

    if not exchange.has.get("fetchOHLCV"):
        raise RuntimeError("Bitvavo ondersteunt fetch_ohlcv niet")

    return exchange


def fetch_candles_range(
    exchange: ccxt.Exchange,
    symbol: str,
    since_ms: int,
    until_ms: int,
) -> List[List[Any]]:
    candles: List[List[Any]] = []
    cursor = max(0, since_ms)
    attempts_without_progress = 0

    while cursor <= until_ms:
        batch = exchange.fetch_ohlcv(
            symbol,
            timeframe=TIMEFRAME,
            since=cursor,
            limit=500,
        )

        if not batch:
            break

        new_rows = [
            candle
            for candle in batch
            if candle and int(candle[0]) <= until_ms
        ]

        if new_rows:
            candles.extend(new_rows)

        last_ms = int(batch[-1][0])
        next_cursor = last_ms + TIMEFRAME_MS

        if next_cursor <= cursor:
            attempts_without_progress += 1
            if attempts_without_progress >= 2:
                break
            next_cursor = cursor + TIMEFRAME_MS
        else:
            attempts_without_progress = 0

        cursor = next_cursor

        if len(batch) < 500:
            break

    # Duplicaten verwijderen.
    unique: Dict[int, List[Any]] = {}
    for candle in candles:
        if candle:
            unique[int(candle[0])] = candle

    return [unique[k] for k in sorted(unique)]


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
        entry = raw_entry * (1.0 + half_spread)
    else:
        entry = raw_entry * (1.0 - half_spread)

    # Exact dezelfde TP/SL-afstand behouden na entry-spreadcorrectie.
    delta = entry - raw_entry
    tp = raw_tp + delta
    sl = raw_sl + delta

    stake = float(settings["stake_eur"])
    amount = stake / entry
    entry_fee = stake * float(settings["fee_pct_per_side"]) / 100.0

    return {
        "key": candidate_key(row),
        "symbol": str(row.get("symbol") or "").upper(),
        "strategy": str(row.get("strategy") or ""),
        "side": side,
        "score": to_float(row.get("score"), 0.0),
        "rr": to_float(row.get("reward_risk"), 0.0),
        "expected_profit_eur": to_float(row.get("expected_profit_eur"), 0.0),
        "spread_pct": spread,
        "detected_at": str(row.get("detected_at") or ""),
        "candle_timestamp": str(row.get("candle_timestamp") or ""),
        "entry_candle_ms": candle_ms,
        "entry_price": entry,
        "take_profit": tp,
        "stop_loss": sl,
        "stake_eur": stake,
        "amount": amount,
        "entry_fee_eur": entry_fee,
    }


def close_result(
    position: Dict[str, Any],
    raw_exit: float,
    exit_reason: str,
    exit_ms: int,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    spread = float(position["spread_pct"])
    half_spread = spread / 200.0
    side = position["side"]

    if side == "LONG":
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
    exit_fee = (
        exit_notional * float(settings["fee_pct_per_side"]) / 100.0
    )
    total_fees = float(position["entry_fee_eur"]) + exit_fee
    net = gross - total_fees

    return {
        **position,
        "status": "CLOSED",
        "exit_reason": exit_reason,
        "exit_price": exit_price,
        "exit_ms": exit_ms,
        "gross_pnl_eur": gross,
        "total_fees_eur": total_fees,
        "net_pnl_eur": net,
        "duration_minutes": max(
            0.0,
            (exit_ms - int(position["entry_candle_ms"])) / 60_000,
        ),
    }


def evaluate_position(
    position: Dict[str, Any],
    candles: Iterable[List[Any]],
    settings: Dict[str, Any],
    now_ms: int,
) -> Dict[str, Any]:
    entry_ms = int(position["entry_candle_ms"])
    max_hold_ms = int(settings["max_hold_minutes"]) * 60_000
    horizon_ms = entry_ms + max_hold_ms
    last_close: Optional[Tuple[int, float]] = None

    for candle in candles:
        if len(candle) < 5:
            continue

        candle_ms = int(candle[0])
        if candle_ms <= entry_ms:
            continue
        if candle_ms > min(horizon_ms, now_ms):
            break

        high = to_float(candle[2], 0.0)
        low = to_float(candle[3], 0.0)
        close = to_float(candle[4], 0.0)

        if close > 0:
            last_close = (candle_ms, close)

        tp = float(position["take_profit"])
        sl = float(position["stop_loss"])

        if position["side"] == "LONG":
            tp_hit = high >= tp
            sl_hit = low <= sl
        else:
            tp_hit = low <= tp
            sl_hit = high >= sl

        # Conservatief: als beide in dezelfde 15m-candle geraakt zijn, telt SL.
        if sl_hit:
            return close_result(
                position,
                sl,
                "stop_loss",
                candle_ms,
                settings,
            )
        if tp_hit:
            return close_result(
                position,
                tp,
                "take_profit",
                candle_ms,
                settings,
            )

    # Alleen time-exit wanneer de volledige houdtijd verstreken is.
    if now_ms >= horizon_ms and last_close is not None:
        return close_result(
            position,
            last_close[1],
            "time_exit",
            last_close[0],
            settings,
        )

    return {
        **position,
        "status": "OPEN",
        "exit_reason": "",
        "net_pnl_eur": 0.0,
        "total_fees_eur": float(position["entry_fee_eur"]),
        "duration_minutes": max(
            0.0,
            (now_ms - entry_ms) / 60_000,
        ),
    }


def profit_factor(results: List[Dict[str, Any]]) -> float:
    gains = sum(
        max(0.0, to_float(r.get("net_pnl_eur"), 0.0))
        for r in results
    )
    losses = sum(
        abs(min(0.0, to_float(r.get("net_pnl_eur"), 0.0)))
        for r in results
    )

    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def summary_line(label: str, rows: List[Dict[str, Any]]) -> str:
    closed = [r for r in rows if r.get("status") == "CLOSED"]
    wins = sum(to_float(r.get("net_pnl_eur")) > 0 for r in closed)
    losses = sum(to_float(r.get("net_pnl_eur")) < 0 for r in closed)
    net = sum(to_float(r.get("net_pnl_eur")) for r in closed)
    pf = profit_factor(closed)
    pf_text = "inf" if math.isinf(pf) else f"{pf:.2f}"

    return (
        f"{label:<20} closed={len(closed):3d} "
        f"W/L={wins:3d}/{losses:3d} "
        f"winrate={(wins / len(closed) * 100 if closed else 0):5.1f}% "
        f"net=€{net:+8.4f} PF={pf_text}"
    )


def run_analysis() -> int:
    rows = read_signals()
    candidates = select_candidates(rows)
    settings = load_settings()

    print("=" * 78)
    print(" DIAMOND TRADER RR 0.80-1.20 SHADOW ANALYSIS")
    print("=" * 78)
    print(f"Versie                 : {VERSION}")
    print(f"Modus                  : {MODE}")
    print(f"Signaalregels totaal   : {len(rows)}")
    print(f"RR-only kandidaten     : {len(candidates)}")
    print(f"RR-band                : {RR_MIN:.2f} <= RR < {RR_MAX:.2f}")
    print(f"Stake simulatie        : €{settings['stake_eur']:.2f}")
    print(f"Fee per zijde          : {settings['fee_pct_per_side']:.3f}%")
    print(f"Max houdtijd           : {settings['max_hold_minutes']} minuten")
    print()
    print("VEILIGHEID")
    print("-" * 78)
    print("Orders mogelijk        : NEE")
    print("Private API            : NEE")
    print("API-sleutels gebruikt  : NEE")
    print("Config/state gewijzigd : NEE")
    print("/var/data geschreven   : NEE")

    if not candidates:
        print()
        print("Geen RR-only kandidaten in deze band gevonden.")
        return 0

    by_symbol: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in candidates:
        by_symbol[str(row.get("symbol") or "").upper()].append(row)

    exchange = create_public_exchange()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    max_hold_ms = int(settings["max_hold_minutes"]) * 60_000

    results: List[Dict[str, Any]] = []
    errors: List[str] = []

    print()
    print("HISTORISCHE CANDLES OPHALEN")
    print("-" * 78)

    symbols = sorted(by_symbol)
    for index, symbol in enumerate(symbols, start=1):
        symbol_rows = by_symbol[symbol]
        positions = [
            build_position(row, settings)
            for row in symbol_rows
        ]
        positions = [p for p in positions if p is not None]

        if not positions:
            continue

        since_ms = min(int(p["entry_candle_ms"]) for p in positions) + TIMEFRAME_MS
        until_ms = min(
            now_ms,
            max(int(p["entry_candle_ms"]) + max_hold_ms for p in positions),
        )

        try:
            candles = fetch_candles_range(
                exchange,
                symbol,
                since_ms,
                until_ms,
            )

            print(
                f"[{index:02d}/{len(symbols):02d}] "
                f"{symbol:<12} kandidaten={len(positions):3d} "
                f"candles={len(candles):4d}"
            )

            for position in positions:
                results.append(
                    evaluate_position(
                        position,
                        candles,
                        settings,
                        now_ms,
                    )
                )

        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            print(
                f"[{index:02d}/{len(symbols):02d}] "
                f"{symbol:<12} FOUT: {type(exc).__name__}: {exc}"
            )

        # Kleine rust tussen markten; publieke API en rate limiter blijven leidend.
        time.sleep(0.05)

    closed = [r for r in results if r.get("status") == "CLOSED"]
    open_rows = [r for r in results if r.get("status") == "OPEN"]

    print()
    print("TOTAALRESULTAAT")
    print("-" * 78)
    print(summary_line("RR 0.80-1.20", results))
    print(f"Nog open/onvolledig   : {len(open_rows)}")
    print(
        f"Totale kosten gesloten: "
        f"€{sum(to_float(r.get('total_fees_eur')) for r in closed):.4f}"
    )

    print()
    print("PER RICHTING")
    print("-" * 78)
    for side in ("LONG", "SHORT"):
        subset = [r for r in results if r.get("side") == side]
        print(summary_line(side, subset))

    print()
    print("PER RR-DREMPEL")
    print("-" * 78)
    print("Dit toont wat overblijft wanneer de scannergrens hypothetisch lager staat.")
    for threshold in (0.80, 0.90, 1.00, 1.10):
        subset = [
            r for r in results
            if to_float(r.get("rr")) >= threshold
        ]
        print(summary_line(f"RR >= {threshold:.2f}", subset))

    print()
    print("PER STRATEGIE")
    print("-" * 78)
    strategies = sorted({str(r.get("strategy") or "-") for r in results})
    for strategy in strategies:
        subset = [r for r in results if r.get("strategy") == strategy]
        print(summary_line(strategy[:20], subset))

    print()
    print("LAATSTE 15 KANDIDATEN")
    print("-" * 78)
    for r in sorted(
        results,
        key=lambda x: int(x.get("entry_candle_ms", 0)),
    )[-15:]:
        pnl = (
            f"€{to_float(r.get('net_pnl_eur')):+.4f}"
            if r.get("status") == "CLOSED"
            else "OPEN"
        )
        print(
            f"{r.get('candle_timestamp','-')[:19]:19s} "
            f"{r.get('symbol','-'):<11} "
            f"{r.get('side','-'):<5} "
            f"{r.get('strategy','-'):<16} "
            f"RR={to_float(r.get('rr')):4.2f} "
            f"verw=€{to_float(r.get('expected_profit_eur')):5.2f} "
            f"{r.get('exit_reason') or 'open':<11} "
            f"{pnl}"
        )

    if errors:
        print()
        print("FOUTEN")
        print("-" * 78)
        for error in errors:
            print("-", error)

    print()
    print("INTERPRETATIE")
    print("-" * 78)
    print(
        "Dit resultaat is alleen een onafhankelijke signaalkwaliteitstest. "
        "Het verlaagt de scanner-RR niet en verandert geen handelsinstelling."
    )
    print(
        "Een lagere RR-grens is pas interessant wanneer voldoende GESLOTEN "
        "kandidaten na kosten positief blijven en niet door enkele uitschieters "
        "worden gedragen."
    )
    print("=" * 78)

    return 0


def self_test() -> None:
    base = {
        "detected_at": "2026-08-01T10:01:00+00:00",
        "candle_timestamp": "2026-08-01T09:45:00+00:00",
        "symbol": "TEST/EUR",
        "strategy": "momentum",
        "side": "LONG",
        "score": "95",
        "entry_price": "100",
        "take_profit": "102",
        "stop_loss": "99",
        "spread_pct": "0.10",
        "reward_risk": "0.90",
        "expected_profit_eur": "1.50",
        "shadow_eligible": "False",
        "shadow_rejection_reasons": "risico/winst 0.900 lager dan 1.200",
    }

    assert rr_only_candidate(base)
    assert not rr_only_candidate({
        **base,
        "shadow_rejection_reasons":
            "risico/winst 0.900 lager dan 1.200 | verwachte winst €0.50 lager dan €1.00",
    })
    assert not rr_only_candidate({**base, "reward_risk": "1.20"})
    assert not rr_only_candidate({**base, "reward_risk": "0.79"})
    assert not rr_only_candidate({**base, "shadow_eligible": "True"})

    settings = {
        "stake_eur": 120.0,
        "fee_pct_per_side": 0.25,
        "max_hold_minutes": 2880,
    }
    position = build_position(base, settings)
    assert position is not None

    entry_ms = int(position["entry_candle_ms"])
    both = [[entry_ms + TIMEFRAME_MS, 100, 103, 98, 100, 1]]
    result = evaluate_position(
        position,
        both,
        settings,
        entry_ms + TIMEFRAME_MS,
    )
    assert result["status"] == "CLOSED"
    assert result["exit_reason"] == "stop_loss"

    assert SAFETY["orders_possible"] is False
    assert SAFETY["private_api"] is False
    assert SAFETY["api_keys_used"] is False
    assert SAFETY["var_data_writes"] is False

    print("RR_080_120_SHADOW_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diamond Trader read-only RR 0.80-1.20 analyse"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Interne test zonder netwerk en zonder bestandswijzigingen.",
    )
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    return run_analysis()


if __name__ == "__main__":
    sys.exit(main())
