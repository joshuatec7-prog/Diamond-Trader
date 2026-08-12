#!/usr/bin/env python3
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

DATA = Path("/var/data")
SIGNALS = DATA / "diamond_market_signals.csv"
TRADES = DATA / "diamond_scanner_selective_shadow_trades.csv"

BASELINE = DATA / "diamond_master_decision_shadow_baseline.json"
REPORT = DATA / "diamond_master_decision_shadow_report.json"

BTC_SAMPLES = (
    DATA /
    "diamond_btc_event_confirmation" /
    "coinbase_btc_samples.csv"
)

VOL_EXEC = 2.389331
QUOTE_EXEC = 1441684.12

TB_SCORE = 96.80
TB_VOLUME = 1.7316

BTC_WINDOW = 30
BTC_THRESHOLD = 0.02315


def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat()


def read_csv(path):
    if not path.exists():
        return []

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return 0.0


def truth(v):
    return str(v).strip().lower() in {
        "1", "true", "yes", "ja"
    }


def parse_dt(v):
    if not v:
        return None

    s = str(v).strip()

    try:
        x = float(s)
        if x > 1e12:
            x /= 1000
        if x > 1e9:
            return datetime.fromtimestamp(
                x,
                tz=timezone.utc,
            )
    except Exception:
        pass

    try:
        d = datetime.fromisoformat(
            s.replace("Z", "+00:00")
        )
        if d.tzinfo is None:
            d = d.replace(
                tzinfo=timezone.utc
            )
        return d
    except Exception:
        return None

def first(row, names):
    for name in names:
        v = row.get(name)
        if v not in (None, ""):
            return v
    return None


def candidate_key(row):
    explicit = first(row, [
        "candidate_key",
        "trade_key",
        "signal_key",
        "signal_id",
    ])

    if explicit:
        return str(explicit)

    symbol = str(
        row.get("symbol") or ""
    ).upper()

    stamp = str(first(row, [
        "candle_timestamp",
        "signal_timestamp",
        "entry_candle_timestamp",
    ]) or "")

    return f"{symbol}|{stamp}"


def signal_time(row):
    return parse_dt(first(row, [
        "candle_timestamp",
        "signal_timestamp",
        "entry_candle_timestamp",
        "created_at",
    ]))


def load_baseline():
    if BASELINE.exists():
        return json.loads(
            BASELINE.read_text()
        )

    data = {
        "version": "1.0",
        "started_at": now_iso(),
        "rules": {
            "good_regimes": [
                "BULLISH",
                "BULLISH_WEAK",
            ],
            "tb_score_min": TB_SCORE,
            "tb_volume_min": TB_VOLUME,
            "execution_volume_min": VOL_EXEC,
            "execution_quote_min": QUOTE_EXEC,
            "btc_window_seconds": BTC_WINDOW,
            "btc_threshold_pct": BTC_THRESHOLD,
        },
    }

    BASELINE.write_text(
        json.dumps(data, indent=2)
    )

    return data


def long_tb_sv(row):
    return (
        truth(row.get("shadow_eligible"))
        and str(
            row.get("side") or ""
        ).upper() == "LONG"
        and str(
            row.get("strategy") or ""
        ) == "trend_breakout"
        and num(row.get("score")) >= TB_SCORE
        and num(row.get("volume_ratio")) >= TB_VOLUME
    )


def good_regime(row):
    return str(
        row.get("market_regime") or ""
    ).upper() in {
        "BULLISH",
        "BULLISH_WEAK",
    }


def execution_flags(row):
    vol = num(row.get("volume_ratio"))

    quote = num(first(row, [
        "quote_volume",
        "quote_volume_24h",
        "volume_quote",
    ]))

    return (
        vol >= VOL_EXEC,
        vol >= VOL_EXEC
        and quote >= QUOTE_EXEC,
    )

baseline = load_baseline()
started = parse_dt(
    baseline["started_at"]
)

signals = read_csv(SIGNALS)
trades = read_csv(TRADES)

selective = {}

for row in trades:
    if str(
        row.get("variant") or ""
    ).upper() != "SELECTIVE":
        continue

    selective[
        candidate_key(row)
    ] = row


candidates = []

for row in signals:
    dt = signal_time(row)

    if (
        dt is None
        or dt < started
    ):
        continue

    key = candidate_key(row)

    sel = key in selective
    regime_ok = good_regime(row)
    tbsv = long_tb_sv(row)

    core = (
        (sel and regime_ok)
        or tbsv
    )

    if not core:
        continue

    exec_hv, exec_hvq = (
        execution_flags(row)
    )

    trade = selective.get(
        key,
        {}
    )

    candidates.append({
        "key": key,
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "strategy": row.get("strategy"),
        "regime": row.get("market_regime"),
        "selective": sel,
        "good_regime": regime_ok,
        "long_tb_score_volume": tbsv,
        "execution_high_volume": exec_hv,
        "execution_high_volume_quote": exec_hvq,
        "net_pnl_eur": trade.get(
            "net_pnl_eur"
        ),
    })


closed = [
    x for x in candidates
    if x["net_pnl_eur"]
    not in (None, "")
]

pnl = sum(
    num(x["net_pnl_eur"])
    for x in closed
)

report = {
    "version": "1.0",
    "started_at": baseline["started_at"],
    "updated_at": now_iso(),
    "accepted": len(candidates),
    "closed": len(closed),
    "target": 20,
    "net_pnl_eur": pnl,
    "candidates": candidates,
}

REPORT.write_text(
    json.dumps(
        report,
        indent=2,
    )
)

print("=" * 88)
print(" DIAMOND TRADER MASTER DECISION SHADOW v1.0")
print("=" * 88)
print()
print("Baseline :", baseline["started_at"])
print()
print(
    "CORE = SELECTIVE + gunstig regime "
    "OF LONG_TB_SCORE_VOLUME"
)
print(
    "BTC/Execution = alleen observatie, "
    "nog GEEN harde gate"
)
print()
print(
    f"Accepted : {len(candidates)}"
)
print(
    f"Closed   : {len(closed)}/20"
)
print(
    f"PnL      : €{pnl:+.4f}"
)

print()
print("LAAGVERDELING")

for name, field in [
    ("SELECTIVE_GOOD_REGIME", "good_regime"),
    ("LONG_TB_SCORE_VOLUME", "long_tb_score_volume"),
    ("EXEC_HIGH_VOLUME", "execution_high_volume"),
    ("EXEC_HV_QUOTE", "execution_high_volume_quote"),
]:
    n = sum(
        truth(x.get(field))
        for x in candidates
    )
    print(f"{name:<24}: {n}")

print()
print(
    "Orders: NEE | Config: NEE | "
    "Live wijziging: NEE"
)
