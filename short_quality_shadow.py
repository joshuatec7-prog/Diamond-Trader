#!/usr/bin/env python3
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import scanner_session_shadow_lab as base

DATA = Path("/var/data")
SIGNALS = DATA / "diamond_market_signals.csv"
BASELINE = DATA / "diamond_short_quality_shadow_baseline.json"

TARGET = 20
SCORE_MIN = 93.70
VOLUME_MIN = 2.4685
SPREAD_MAX = 0.0525

VARIANTS = (
    "ALL_ELIGIBLE",
    "MOMENTUM_BEARISH_WEAK",
    "MBW_HIGH_SCORE",
    "MBW_HIGH_VOLUME",
    "MBW_LOW_SPREAD",
    "TREND_BREAKOUT_CONTROL",
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def truth(v):
    return str(v).strip().lower() in {
        "1", "true", "yes", "ja"
    }


def parse_dt(value):
    return datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )


def load_rows():
    with SIGNALS.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        return list(csv.DictReader(f))


def load_baseline():
    if BASELINE.exists():
        return json.loads(
            BASELINE.read_text()
        )

    data = {
        "started_at": now_iso(),
        "target": TARGET,
        "score_min": SCORE_MIN,
        "volume_min": VOLUME_MIN,
        "spread_max": SPREAD_MAX,
    }

    BASELINE.write_text(
        json.dumps(data, indent=2)
    )

    return data


def accepts(name, row):
    if not truth(row.get("shadow_eligible")):
        return False

    if str(row.get("side") or "").upper() != "SHORT":
        return False

    strategy = str(
        row.get("strategy") or ""
    )

    regime = str(
        row.get("market_regime") or ""
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

    if name == "TREND_BREAKOUT_CONTROL":
        return strategy == "trend_breakout"

    mbw = (
        strategy == "momentum"
        and regime == "BEARISH_WEAK"
    )

    if name == "MOMENTUM_BEARISH_WEAK":
        return mbw

    if name == "MBW_HIGH_SCORE":
        return mbw and score >= SCORE_MIN

    if name == "MBW_HIGH_VOLUME":
        return mbw and volume >= VOLUME_MIN

    if name == "MBW_LOW_SPREAD":
        return (
            mbw
            and 0 < spread <= SPREAD_MAX
        )

    return False


baseline = load_baseline()
started = parse_dt(
    baseline["started_at"]
)

rows = [
    row for row in load_rows()
    if (
        row.get("detected_at")
        and parse_dt(row["detected_at"])
        >= started
    )
]


def evaluate(candidates):
    settings = base.load_settings()
    exchange = base.create_public_exchange()

    closed = []
    open_count = 0

    for row in candidates:
        pos = base.build_position(
            "SHORT_QUALITY",
            row,
            settings,
        )

        if not pos:
            continue

        candles = base.fetch_closed_candles(
            exchange,
            pos["symbol"],
            int(pos["entry_candle_timestamp_ms"]),
        )

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


print("=" * 90)
print(" DIAMOND TRADER SHORT QUALITY SHADOW")
print("=" * 90)
print("Gestart :", baseline["started_at"])
print()

for variant in VARIANTS:
    candidates = [
        row for row in rows
        if accepts(variant, row)
    ]

    a, c, o, w, l, pnl, pf = evaluate(
        candidates
    )

    print(
        f"{variant:<24} "
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
