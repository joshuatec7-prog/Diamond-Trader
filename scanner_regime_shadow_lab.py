#!/usr/bin/env python3
"""
Diamond Trader Scanner Regime Shadow Lab v1.0
Read-only prospectieve regime-test.
Geen orders en geen live wijzigingen.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import scanner_session_shadow_lab as base

VERSION = "1.0"
MODE = "READ_ONLY_SCANNER_REGIME_SHADOW"

DATA = Path("/var/data")
SIGNALS = DATA / "diamond_market_signals.csv"
BASELINE = DATA / "diamond_scanner_regime_shadow_baseline.json"
STATE = DATA / "diamond_scanner_regime_shadow_state.json"
REPORT = DATA / "diamond_scanner_regime_shadow_report.json"
TRADES = DATA / "diamond_scanner_regime_shadow_trades.csv"

TARGET = 20
MAX_KEYS = 30000
BTC_NEUTRAL_PCT = 0.10

VARIANTS = (
    "CURRENT",
    "COMPRESSION",
    "EXPANSION",
    "HIGH_VOL_CHOP",
    "BTC_ALIGNED",
    "BTC_OPPOSITE",
)

SAFETY = {
    "orders_possible": False,
    "private_exchange_calls": False,
    "api_keys_passed_to_exchange": False,
    "config_modified": False,
    "diamond_state_modified": False,
    "automatic_live_changes": False,
}

HEADER = list(base.TRADE_HEADER) + [
    "atr_pct",
    "volume_ratio",
    "btc_move_1h_pct",
]


def now_iso():
    return datetime.now(timezone.utc).isoformat()

def q(values: List[float], p: float) -> float:
    values = sorted(
        v for v in values
        if math.isfinite(v)
    )

    if not values:
        return 0.0

    x = (len(values) - 1) * p
    lo = int(math.floor(x))
    hi = int(math.ceil(x))

    if lo == hi:
        return values[lo]

    return (
        values[lo]
        + (values[hi] - values[lo])
        * (x - lo)
    )

def rows() -> List[Dict[str, str]]:
    if not SIGNALS.exists():
        return []

    if SIGNALS.stat().st_size == 0:
        return []

    with SIGNALS.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        return list(csv.DictReader(f))


def eligible(row):
    return base.to_bool(
        row.get("shadow_eligible"),
        False,
    )

def make_baseline(all_rows):
    old = (
        json.loads(BASELINE.read_text())
        if BASELINE.exists()
        else {}
    )

    if old.get("started_at"):
        return old

    started = now_iso()
    started_dt = base.parse_datetime(started)

    prior = [
        row for row in all_rows
        if eligible(row)
        and (
            base.parse_datetime(
                row.get("detected_at")
            )
            or started_dt
        ) < started_dt
    ]

    atr = [
        base.to_float(row.get("atr_pct"))
        for row in prior
        if base.to_float(row.get("atr_pct")) > 0
    ]

    vol = [
        base.to_float(row.get("volume_ratio"))
        for row in prior
        if base.to_float(row.get("volume_ratio")) > 0
    ]

    baseline = {
        "version": VERSION,
        "mode": MODE,
        "started_at": started,
        "history_rows_used": len(prior),

        "thresholds": {
            "atr_q25": round(q(atr, 0.25), 6),
            "atr_q75": round(q(atr, 0.75), 6),
            "volume_q50": round(q(vol, 0.50), 6),
            "volume_q75": round(q(vol, 0.75), 6),
            "btc_neutral_band_pct": BTC_NEUTRAL_PCT,
        },

        "rules": {
            "COMPRESSION": "lage ATR + laag volume",
            "EXPANSION": "hoge ATR + hoog volume",
            "HIGH_VOL_CHOP": "NEUTRAL + hoge ATR",
            "BTC_ALIGNED": "BTC beweegt mee met signaal",
            "BTC_OPPOSITE": "BTC beweegt tegen signaal",
        },

        "safety": SAFETY,
    }

    base.save_json_atomic(
        BASELINE,
        baseline,
    )

    return baseline


def empty_state(started):
    return {
        "version": VERSION,
        "mode": MODE,
        "started_at": started,
        "last_update_at": None,
        "processed_signal_keys": [],
        "eligible_signals_seen": 0,

        "variants": {
            variant: {
                "open_positions": {},
                "accepted_signals": 0,
            }
            for variant in VARIANTS
        },

        "last_errors": [],
        "safety": SAFETY,
    }


def load_state(started):
    state = (
        json.loads(STATE.read_text())
        if STATE.exists()
        else empty_state(started)
    )

    state["started_at"] = started
    state["version"] = VERSION
    state["mode"] = MODE

    for variant in VARIANTS:
        state.setdefault(
            "variants",
            {},
        ).setdefault(
            variant,
            {
                "open_positions": {},
                "accepted_signals": 0,
            },
        )

    return state


def btc_move(exchange, candle_ms, cache):
    if candle_ms in cache:
        return cache[candle_ms]

    since = max(
        0,
        candle_ms - 3 * base.TIMEFRAME_MS,
    )

    candles = exchange.fetch_ohlcv(
        "BTC/EUR",
        "15m",
        since=since,
        limit=4,
    ) or []

    candles = [
        candle
        for candle in candles
        if len(candle) >= 5
        and int(candle[0]) <= candle_ms
    ]

    if (
        len(candles) < 4
        or base.to_float(candles[0][1]) <= 0
    ):
        cache[candle_ms] = None
        return None

    move = (
        base.to_float(candles[-1][4])
        / base.to_float(candles[0][1])
        - 1
    ) * 100

    cache[candle_ms] = move
    return move


def accepts(name, row, thresholds, btc):
    if not eligible(row):
        return False

    atr = base.to_float(
        row.get("atr_pct")
    )

    volume = base.to_float(
        row.get("volume_ratio")
    )

    regime = str(
        row.get("market_regime") or ""
    ).upper()

    side = str(
        row.get("side") or ""
    ).upper()

    if name == "CURRENT":
        return True

    if name == "COMPRESSION":
        return (
            atr > 0
            and atr <= thresholds["atr_q25"]
            and volume > 0
            and volume <= thresholds["volume_q50"]
        )

    if name == "EXPANSION":
        return (
            atr >= thresholds["atr_q75"]
            and volume >= thresholds["volume_q75"]
        )

    if name == "HIGH_VOL_CHOP":
        return (
            regime == "NEUTRAL"
            and atr >= thresholds["atr_q75"]
        )

    if btc is None:
        return False

    aligned = (
        side == "LONG"
        and btc >= BTC_NEUTRAL_PCT
    ) or (
        side == "SHORT"
        and btc <= -BTC_NEUTRAL_PCT
    )

    opposite = (
        side == "LONG"
        and btc <= -BTC_NEUTRAL_PCT
    ) or (
        side == "SHORT"
        and btc >= BTC_NEUTRAL_PCT
    )

    if name == "BTC_ALIGNED":
        return aligned

    if name == "BTC_OPPOSITE":
        return opposite

    return False


def append_trade(row):
    TRADES.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    need_header = (
        not TRADES.exists()
        or TRADES.stat().st_size == 0
    )

    with TRADES.open(
        "a",
        encoding="utf-8",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=HEADER,
        )

        if need_header:
            writer.writeheader()

        writer.writerow({
            key: row.get(key, "")
            for key in HEADER
        })


def ingest(
    state,
    all_rows,
    baseline,
    settings,
):
    started = base.parse_datetime(
        baseline["started_at"]
    )

    processed = set(
        map(
            str,
            state["processed_signal_keys"],
        )
    )

    grouped = {
        variant: {}
        for variant in VARIANTS
    }

    exchange = None
    btc_cache = {}
    thresholds = baseline["thresholds"]

    for row in all_rows:
        detected = base.parse_datetime(
            row.get("detected_at")
        )

        key = base.candidate_key(row)

        if (
            detected is None
            or detected < started
            or key in processed
        ):
            continue

        processed.add(key)

        if not eligible(row):
            continue

        state["eligible_signals_seen"] += 1

        candle_ms = base.datetime_ms(
            row.get("candle_timestamp")
        )

        btc = None

        if candle_ms > 0:
            try:
                if exchange is None:
                    exchange = (
                        base.create_public_exchange()
                    )

                btc = btc_move(
                    exchange,
                    candle_ms,
                    btc_cache,
                )

            except Exception as exc:
                state["last_errors"] = (
                    state.get("last_errors") or []
                )[-19:] + [
                    f"BTC {key}: {exc}"
                ]

        row = dict(row)
        row["_btc_move_1h_pct"] = btc

        group = (
            str(row.get("symbol") or "").upper(),
            str(row.get("candle_timestamp") or ""),
        )

        for variant in VARIANTS:
            if not accepts(
                variant,
                row,
                thresholds,
                btc,
            ):
                continue

            previous = grouped[
                variant
            ].get(group)

            if (
                previous is None
                or base.to_float(row.get("score"))
                > base.to_float(previous.get("score"))
            ):
                grouped[variant][group] = row

    for variant in VARIANTS:
        item = state["variants"][variant]

        sorted_rows = sorted(
            grouped[variant].values(),
            key=lambda row: base.datetime_ms(
                row.get("candle_timestamp")
            ),
        )

        for row in sorted_rows:
            position = base.build_position(
                variant,
                row,
                settings,
            )

            if not position:
                continue

            position["atr_pct"] = base.to_float(
                row.get("atr_pct")
            )

            position["volume_ratio"] = base.to_float(
                row.get("volume_ratio")
            )

            position["btc_move_1h_pct"] = row.get(
                "_btc_move_1h_pct"
            )

            key = position["candidate_key"]

            if key not in item["open_positions"]:
                item["open_positions"][key] = position
                item["accepted_signals"] += 1

    state["processed_signal_keys"] = (
        list(processed)[-MAX_KEYS:]
    )


def update_positions(
    state,
    settings,
):
    by_symbol = defaultdict(list)

    for variant in VARIANTS:
        for key, position in (
            state["variants"][variant]
            ["open_positions"].items()
        ):
            by_symbol[position["symbol"]].append(
                (
                    variant,
                    key,
                    position,
                )
            )

    if not by_symbol:
        return

    exchange = base.create_public_exchange()

    done = []
    errors = []

    for symbol, items in by_symbol.items():
        since = min(
            int(
                position[
                    "entry_candle_timestamp_ms"
                ]
            )
            for _, _, position in items
        )

        try:
            candles = base.fetch_closed_candles(
                exchange,
                symbol,
                since,
            )

        except Exception as exc:
            errors.append(
                f"{symbol}: {exc}"
            )
            continue

        for variant, key, position in items:
            closed = base.evaluate(
                position,
                candles,
                settings,
            )

            if not closed:
                continue

            closed["atr_pct"] = position.get(
                "atr_pct"
            )

            closed["volume_ratio"] = position.get(
                "volume_ratio"
            )

            closed["btc_move_1h_pct"] = position.get(
                "btc_move_1h_pct"
            )

            append_trade(closed)

            done.append(
                (
                    variant,
                    key,
                )
            )

    for variant, key in done:
        state["variants"][variant][
            "open_positions"
        ].pop(
            key,
            None,
        )

    state["last_errors"] = (
        state.get("last_errors")
        or []
    )[-15:] + errors[-5:]


def summaries(state):
    data = []

    if (
        TRADES.exists()
        and TRADES.stat().st_size
    ):
        with TRADES.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as f:
            data = list(
                csv.DictReader(f)
            )

    result = {}

    for variant in VARIANTS:
        rows_for_variant = [
            row
            for row in data
            if row.get("variant") == variant
        ]

        pnl = [
            base.to_float(
                row.get("net_pnl_eur")
            )
            for row in rows_for_variant
        ]

        gains = sum(
            max(0, value)
            for value in pnl
        )

        losses = sum(
            abs(min(0, value))
            for value in pnl
        )

        wins = sum(
            value > 0
            for value in pnl
        )

        loss_count = sum(
            value < 0
            for value in pnl
        )

        result[variant] = {
            "accepted": int(
                state["variants"][variant].get(
                    "accepted_signals",
                    0,
                )
            ),

            "open": len(
                state["variants"][variant][
                    "open_positions"
                ]
            ),

            "closed": len(rows_for_variant),
            "wins": wins,
            "losses": loss_count,

            "winrate_pct": (
                round(
                    100 * wins / len(rows_for_variant),
                    2,
                )
                if rows_for_variant
                else 0.0
            ),

            "net_pnl_eur": round(
                sum(pnl),
                6,
            ),

            "profit_factor": (
                round(
                    gains / losses,
                    4,
                )
                if losses
                else (
                    "inf"
                    if gains
                    else 0.0
                )
            ),

            "target": TARGET,

            "remaining": max(
                0,
                TARGET - len(rows_for_variant),
            ),
        }

    return result


def write_report(
    state,
    baseline,
):
    report = {
        "version": VERSION,
        "mode": MODE,
        "generated_at": now_iso(),
        "started_at": baseline["started_at"],

        "eligible_signals_seen": state[
            "eligible_signals_seen"
        ],

        "thresholds": baseline["thresholds"],
        "rules": baseline["rules"],
        "variants": summaries(state),
        "safety": SAFETY,

        "last_errors": state.get(
            "last_errors"
        ) or [],
    }

    base.save_json_atomic(
        REPORT,
        report,
    )

    return report


def print_report(report):
    print("=" * 88)
    print(" DIAMOND TRADER REGIME SHADOW")
    print("=" * 88)

    print(
        "Gestart:",
        report["started_at"],
    )

    print(
        "Eligible gezien:",
        report["eligible_signals_seen"],
    )

    print(
        "Drempels:",
        report["thresholds"],
    )

    print()

    for variant in VARIANTS:
        item = report["variants"][variant]

        print(
            f"{variant:<14} "
            f"accepted={item['accepted']:3d} "
            f"closed={item['closed']:2d}/{TARGET} "
            f"W/L={item['wins']:2d}/{item['losses']:2d} "
            f"open={item['open']:2d} "
            f"pnl=€{item['net_pnl_eur']:+.4f} "
            f"PF={item['profit_factor']}"
        )

    print()

    print(
        "Orders mogelijk: NEE | "
        "Private API: NEE | "
        "Live/config wijziging: NEE"
    )

    print("=" * 88)


def run():
    all_rows = rows()

    baseline = make_baseline(
        all_rows
    )

    state = load_state(
        baseline["started_at"]
    )

    settings = base.load_settings()

    ingest(
        state,
        all_rows,
        baseline,
        settings,
    )

    update_positions(
        state,
        settings,
    )

    state["last_update_at"] = now_iso()

    base.save_json_atomic(
        STATE,
        state,
    )

    return write_report(
        state,
        baseline,
    )


def self_test():
    thresholds = {
        "atr_q25": 0.35,
        "atr_q75": 0.70,
        "volume_q50": 1.0,
        "volume_q75": 1.5,
    }

    row = {
        "shadow_eligible": "True",
        "atr_pct": "0.30",
        "volume_ratio": "0.80",
        "market_regime": "NEUTRAL",
        "side": "LONG",
    }

    assert accepts(
        "COMPRESSION",
        row,
        thresholds,
        0.20,
    )

    row["atr_pct"] = "0.80"
    row["volume_ratio"] = "1.80"

    assert accepts(
        "EXPANSION",
        row,
        thresholds,
        0.20,
    )

    assert accepts(
        "HIGH_VOL_CHOP",
        row,
        thresholds,
        0.20,
    )

    assert accepts(
        "BTC_ALIGNED",
        row,
        thresholds,
        0.20,
    )

    assert not accepts(
        "BTC_OPPOSITE",
        row,
        thresholds,
        0.20,
    )

    row["side"] = "SHORT"

    assert accepts(
        "BTC_OPPOSITE",
        row,
        thresholds,
        0.20,
    )

    assert not SAFETY[
        "orders_possible"
    ]

    assert not SAFETY[
        "private_exchange_calls"
    ]

    assert not SAFETY[
        "automatic_live_changes"
    ]

    print(
        "SCANNER_REGIME_SHADOW_SELF_TEST_OK"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--self-test",
        action="store_true",
    )

    parser.add_argument(
        "--update",
        action="store_true",
    )

    parser.add_argument(
        "--status",
        action="store_true",
    )

    parser.add_argument(
        "--no-print",
        action="store_true",
    )

    args = parser.parse_args()

    if args.self_test:
        self_test()

    elif args.status and REPORT.exists():
        print_report(
            json.loads(
                REPORT.read_text()
            )
        )

    else:
        report = run()

        if not args.no_print:
            print_report(report)
