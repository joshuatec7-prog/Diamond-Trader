#!/usr/bin/env python3
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import scanner_session_shadow_lab as base

DATA = Path("/var/data")
SIGNALS = DATA / "diamond_market_signals.csv"
BASELINE = DATA / "diamond_long_quality_shadow_baseline.json"

SCORE_MIN = 96.80
VOLUME_MIN = 1.7316
SPREAD_MAX = 0.0814
TARGET = 20

VARIANTS = (
    "ALL_ELIGIBLE",
    "TREND_BREAKOUT",
    "TB_HIGH_SCORE",
    "TB_HIGH_VOLUME",
    "TB_LOW_SPREAD",
    "TB_SCORE_VOLUME",
    "PULLBACK_CONTROL",
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def truth(v):
    return str(v).strip().lower() in {
        "1", "true", "yes", "ja"
    }


def rows():
    with SIGNALS.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        return list(csv.DictReader(f))


def baseline():
    if BASELINE.exists():
        return json.loads(
            BASELINE.read_text()
        )

    data = {
        "started_at": now_iso(),
        "target": TARGET,
        "fixed_thresholds": {
            "rr_min": 1.20,
            "score_min": SCORE_MIN,
            "volume_min": VOLUME_MIN,
            "spread_max": SPREAD_MAX,
        },
    }

    BASELINE.write_text(
        json.dumps(data, indent=2)
    )

    return data


def accepts(name, row):
    if not truth(row.get("shadow_eligible")):
        return False

    if str(row.get("side") or "").upper() != "LONG":
        return False

    strategy = str(
        row.get("strategy") or ""
    )

    score = base.to_float(
        row.get("score")
    )

    volume = base.to_float(
        row.get("volume_ratio")
    )

    spread = base.to_float(
        row.get("spread_pct")
    )

    if name == "ALL_ELIGIBLE":
        return True

    if name == "PULLBACK_CONTROL":
        return strategy == "pullback_retest"

    if strategy != "trend_breakout":
        return False

    if name == "TREND_BREAKOUT":
        return True

    if name == "TB_HIGH_SCORE":
        return score >= SCORE_MIN

    if name == "TB_HIGH_VOLUME":
        return volume >= VOLUME_MIN

    if name == "TB_LOW_SPREAD":
        return 0 < spread <= SPREAD_MAX

    if name == "TB_SCORE_VOLUME":
        return (
            score >= SCORE_MIN
            and volume >= VOLUME_MIN
        )

    return False


def parse_dt(value):
    return datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )


b = baseline()
started = parse_dt(b["started_at"])

all_rows = [
    row for row in rows()
    if row.get("detected_at")
    and parse_dt(row["detected_at"]) >= started
]

groups = {}

for variant in VARIANTS:
    best = {}

    for row in all_rows:
        if not accepts(variant, row):
            continue

        group = (
            str(row.get("symbol") or "").upper(),
            str(row.get("candle_timestamp") or ""),
        )

        old = best.get(group)

        if (
            old is None
            or base.to_float(row.get("score"))
            > base.to_float(old.get("score"))
        ):
            best[group] = row

    groups[variant] = list(best.values())


def evaluate(candidates):
    settings = base.load_settings()
    exchange = base.create_public_exchange()

    closed = []
    open_count = 0

    by_symbol = {}

    for row in candidates:
        pos = base.build_position(
            "LONG_QUALITY",
            row,
            settings,
        )

        if not pos:
            continue

        by_symbol.setdefault(
            pos["symbol"], []
        ).append(pos)

    for symbol, positions in by_symbol.items():
        since = min(
            int(p["entry_candle_timestamp_ms"])
            for p in positions
        )

        candles = base.fetch_closed_candles(
            exchange,
            symbol,
            since,
        )

        for pos in positions:
            result = base.evaluate(
                pos,
                candles,
                settings,
            )

            if result:
                closed.append(result)
            else:
                open_count += 1

    pnl = [
        base.to_float(x.get("net_pnl_eur"))
        for x in closed
    ]

    wins = sum(x > 0 for x in pnl)
    losses = sum(x < 0 for x in pnl)

    gain = sum(max(0, x) for x in pnl)
    loss = sum(abs(min(0, x)) for x in pnl)

    pf = (
        round(gain / loss, 4)
        if loss
        else ("inf" if gain else 0.0)
    )

    return (
        len(candidates),
        len(closed),
        open_count,
        wins,
        losses,
        sum(pnl),
        pf,
    )


print("=" * 92)
print(" DIAMOND TRADER LONG QUALITY SHADOW")
print("=" * 92)
print("Gestart :", b["started_at"])
print("R/R     : minimaal 1.20 - ONGEWIJZIGD")
print()

for variant in VARIANTS:
    a, c, o, w, l, pnl, pf = evaluate(
        groups[variant]
    )

    print(
        f"{variant:<18} "
        f"accepted={a:2d} "
        f"closed={c:2d}/{TARGET} "
        f"W/L={w:2d}/{l:2d} "
        f"open={o:2d} "
        f"pnl=€{pnl:+.4f} "
        f"PF={pf}"
    )

print()
print(
    "Orders: NEE | Private API: NEE | "
    "Live/config wijziging: NEE"
)
print("=" * 92)
