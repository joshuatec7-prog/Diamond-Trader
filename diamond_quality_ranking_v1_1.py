#!/usr/bin/env python3
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import scanner_session_shadow_lab as base

DATA = Path("/var/data")
SIGNALS = DATA / "diamond_market_signals.csv"
SEL = DATA / "diamond_scanner_selective_shadow_trades.csv"

LONG_BASE = DATA / "diamond_long_quality_shadow_baseline.json"
SHORT_BASE = DATA / "diamond_short_quality_shadow_baseline.json"

TARGET = 20
FALLBACK_STAKE = 130.0
STRESS_PCT = 0.30


def read_csv(path):
    if not path.exists() or path.stat().st_size == 0:
        return []

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        return list(csv.DictReader(f))


def truth(v):
    return str(v).strip().lower() in {
        "1", "true", "yes", "ja"
    }


def num(v):
    return base.to_float(v)


def parse_dt(v):
    if not v:
        return None

    try:
        return datetime.fromisoformat(
            str(v).replace("Z", "+00:00")
        )
    except ValueError:
        return None


def stake(row):
    for field in (
        "stake_quote",
        "stake_eur",
        "position_size_eur",
        "quote_amount",
        "stake",
    ):
        value = num(row.get(field))

        if value > 0:
            return value

    return FALLBACK_STAKE


signals = read_csv(SIGNALS)
selective_trades = read_csv(SEL)


def unique_candidates(selector, started=None):
    best = {}

    for row in signals:
        detected = parse_dt(
            row.get("detected_at")
        )

        if (
            started is not None
            and (
                detected is None
                or detected < started
            )
        ):
            continue

        if not selector(row):
            continue

        group = (
            str(row.get("symbol") or "").upper(),
            str(row.get("candle_timestamp") or ""),
        )

        old = best.get(group)

        if (
            old is None
            or num(row.get("score"))
            > num(old.get("score"))
        ):
            best[group] = row

    return list(best.values())


LONG_TB = lambda r: (
    truth(r.get("shadow_eligible"))
    and str(r.get("side") or "").upper() == "LONG"
    and str(r.get("strategy") or "") == "trend_breakout"
)

LONG_TB_SV = lambda r: (
    LONG_TB(r)
    and num(r.get("score")) >= 96.80
    and num(r.get("volume_ratio")) >= 1.7316
)

SHORT_MBW_HV = lambda r: (
    truth(r.get("shadow_eligible"))
    and str(r.get("side") or "").upper() == "SHORT"
    and str(r.get("strategy") or "") == "momentum"
    and str(r.get("market_regime") or "") == "BEARISH_WEAK"
    and num(r.get("volume_ratio")) >= 2.4685
)


historical = {
    "LONG_TB_SCORE_VOLUME":
        unique_candidates(LONG_TB_SV),

    "LONG_TREND_BREAKOUT":
        unique_candidates(LONG_TB),

    "SHORT_MBW_HIGH_VOLUME":
        unique_candidates(SHORT_MBW_HV),
}


settings = base.load_settings()


def evaluate_groups(group_map):
    positions = {}
    keys_by_group = {}

    for name, candidates in group_map.items():
        keys = set()

        for row in candidates:
            key = base.candidate_key(row)

            pos = base.build_position(
                name,
                row,
                settings,
            )

            if not pos:
                continue

            keys.add(key)
            positions[key] = pos

        keys_by_group[name] = keys

    by_symbol = defaultdict(list)

    for key, pos in positions.items():
        by_symbol[pos["symbol"]].append(
            (key, pos)
        )

    exchange = base.create_public_exchange()
    outcomes = {}
    errors = []

    for symbol, items in by_symbol.items():
        since = min(
            int(pos["entry_candle_timestamp_ms"])
            for _, pos in items
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

        for key, pos in items:
            result = base.evaluate(
                pos,
                candles,
                settings,
            )

            if result:
                outcomes[key] = result

    result_groups = {}

    for name, keys in keys_by_group.items():
        result_groups[name] = [
            outcomes[key]
            for key in keys
            if key in outcomes
        ]

    return result_groups, errors


groups, errors = evaluate_groups(
    historical
)

for variant in ("SELECTIVE", "STRONG"):
    groups[variant] = [
        row for row in selective_trades
        if str(
            row.get("variant") or ""
        ).upper() == variant
    ]


def metrics(rows, friction_pct=0.0):
    adjusted = []

    for row in rows:
        pnl = num(
            row.get("net_pnl_eur")
        )

        extra = (
            stake(row)
            * 2
            * friction_pct
            / 100
        )

        adjusted.append(
            pnl - extra
        )

    wins = sum(x > 0 for x in adjusted)
    losses = sum(x < 0 for x in adjusted)

    gain = sum(
        max(0.0, x)
        for x in adjusted
    )

    loss = sum(
        abs(min(0.0, x))
        for x in adjusted
    )

    pf = (
        gain / loss
        if loss
        else ("inf" if gain else 0.0)
    )

    return (
        len(adjusted),
        wins,
        losses,
        sum(adjusted),
        pf,
    )


def baseline_start(path):
    if not path.exists():
        return None

    data = json.loads(
        path.read_text()
    )

    return parse_dt(
        data.get("started_at")
    )


long_started = baseline_start(
    LONG_BASE
)

short_started = baseline_start(
    SHORT_BASE
)


prospective_map = {
    "LONG_TB_SCORE_VOLUME":
        unique_candidates(
            LONG_TB_SV,
            long_started,
        ),

    "LONG_TREND_BREAKOUT":
        unique_candidates(
            LONG_TB,
            long_started,
        ),

    "SHORT_MBW_HIGH_VOLUME":
        unique_candidates(
            SHORT_MBW_HV,
            short_started,
        ),
}


prospective_closed, prospect_errors = (
    evaluate_groups(
        prospective_map
    )
)

errors.extend(
    prospect_errors
)


def metrics(rows, friction_pct=0.0):
    adjusted = []

    for row in rows:
        pnl = num(
            row.get("net_pnl_eur")
        )

        extra = (
            stake(row)
            * 2
            * friction_pct
            / 100
        )

        adjusted.append(
            pnl - extra
        )

    wins = sum(x > 0 for x in adjusted)
    losses = sum(x < 0 for x in adjusted)

    gain = sum(
        max(0.0, x)
        for x in adjusted
    )

    loss = sum(
        abs(min(0.0, x))
        for x in adjusted
    )

    pf = (
        gain / loss
        if loss
        else ("inf" if gain else 0.0)
    )

    return (
        len(adjusted),
        wins,
        losses,
        sum(adjusted),
        pf,
    )


def baseline_start(path):
    if not path.exists():
        return None

    data = json.loads(
        path.read_text()
    )

    return parse_dt(
        data.get("started_at")
    )


long_started = baseline_start(
    LONG_BASE
)

short_started = baseline_start(
    SHORT_BASE
)


prospective_map = {
    "LONG_TB_SCORE_VOLUME":
        unique_candidates(
            LONG_TB_SV,
            long_started,
        ),

    "LONG_TREND_BREAKOUT":
        unique_candidates(
            LONG_TB,
            long_started,
        ),

    "SHORT_MBW_HIGH_VOLUME":
        unique_candidates(
            SHORT_MBW_HV,
            short_started,
        ),
}


prospective_closed, prospect_errors = (
    evaluate_groups(
        prospective_map
    )
)

errors.extend(
    prospect_errors
)
