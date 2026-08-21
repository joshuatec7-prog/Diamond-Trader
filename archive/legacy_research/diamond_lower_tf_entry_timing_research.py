#!/usr/bin/env python3
"""
Diamond Trader Lower-Timeframe Entry Timing Research v1.0

Doel
----
Onderzoekt of een geldig 15m SELECTIVE-signaal beter uitgevoerd kan worden
door daarna 1m/5m te gebruiken voor entry-timing.

BELANGRIJK:
- de bestaande 15m SELECTIVE- en Execution-tests worden NIET gewijzigd;
- dit is puur historische research;
- originele stop-loss en take-profit van het 15m-signaal blijven staan;
- fees + signaalspread worden meegerekend;
- geen orders, private API, config- of live-wijzigingen.

Varianten
---------
IMMEDIATE
    Huidige signaal-entry als benchmark.

PULLBACK_010
    Maximaal 30m wachten op 0.10% gunstiger entry.

PULLBACK_020
    Maximaal 30m wachten op 0.20% gunstiger entry.

PULLBACK_030
    Maximaal 30m wachten op 0.30% gunstiger entry.

CONFIRM_CLOSE
    Maximaal 30m wachten op eerste lagere-timeframe candle die:
    LONG : groen sluit én boven oorspronkelijke entry sluit
    SHORT: rood sluit én onder oorspronkelijke entry sluit
    Entry op die candle-close; exits pas vanaf volgende candle.

Timeframes
----------
1m en 5m.

Exit
----
Originele 15m take-profit / stop-loss.
Maximale evaluatiehorizon: 12 uur na signaaldetectie.
Als TP en SL in dezelfde candle geraakt worden, telt conservatief de STOP.
Als na 12 uur geen exit is geraakt, time-exit op laatste candle-close.

Gebruik
-------
python3 diamond_lower_tf_entry_timing_research.py --self-test
python3 diamond_lower_tf_entry_timing_research.py --limit 50
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "1.0"
MODE = "READ_ONLY_LOWER_TF_ENTRY_TIMING"

DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SIGNALS = DATA / "diamond_market_signals.csv"
REPORT = DATA / "diamond_lower_tf_entry_timing_research.json"

BASE_URL = "https://api.bitvavo.com/v2"
STAKE_EUR = 130.0
FEE_PCT_PER_SIDE = 0.25

TIMEFRAMES = {
    "1m": 60_000,
    "5m": 300_000,
}
WAIT_MINUTES = 30
HORIZON_MINUTES = 12 * 60

VARIANTS = {
    "IMMEDIATE": None,
    "PULLBACK_010": 0.10,
    "PULLBACK_020": 0.20,
    "PULLBACK_030": 0.30,
    "CONFIRM_CLOSE": None,
}

SAFETY = {
    "orders": False,
    "private_api": False,
    "api_keys": False,
    "config_change": False,
    "strategy_change": False,
    "filter_change": False,
    "stake_change": False,
    "live_change": False,
    "source_files_modified": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def to_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        "1", "true", "yes", "ja", "on"
    }


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def dt_ms(value: Any) -> int:
    dt = parse_dt(value)
    return int(dt.timestamp() * 1000) if dt else 0


def ceil_interval(ms: int, interval_ms: int) -> int:
    return ((ms + interval_ms - 1) // interval_ms) * interval_ms


def market_name(symbol: str) -> str:
    return str(symbol).upper().replace("/", "-")


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(payload, h, ensure_ascii=False, indent=2)
            h.write("\n")
            h.flush()
            os.fsync(h.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def selective_accepts(row: Dict[str, Any]) -> bool:
    """
    Zelfde SELECTIVE-regels als de bestaande shadow-lab route.
    """
    if not to_bool(row.get("shadow_eligible")):
        return False

    side = str(row.get("side") or "").upper()
    strategy = str(row.get("strategy") or "")
    regime = str(row.get("market_regime") or "").upper()

    if side == "LONG" and strategy == "trend_breakout":
        return True
    if side == "SHORT" and regime == "BEARISH_WEAK":
        return True
    if side == "SHORT" and strategy in {"momentum", "pullback_retest"}:
        return True
    return False


def load_selective_signals() -> List[Dict[str, Any]]:
    if not SIGNALS.is_file():
        raise FileNotFoundError(SIGNALS)

    with SIGNALS.open("r", encoding="utf-8-sig", newline="") as h:
        reader = csv.DictReader(h)
        required = {
            "detected_at", "candle_timestamp", "symbol", "strategy", "side",
            "market_regime", "score", "entry_price", "take_profit",
            "stop_loss", "spread_pct", "reward_risk", "shadow_eligible",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(
                "Signalenbestand mist kolommen: " + ", ".join(sorted(missing))
            )

        rows: List[Dict[str, Any]] = []
        for raw in reader:
            if not selective_accepts(raw):
                continue
            if min(
                f(raw.get("entry_price")),
                f(raw.get("take_profit")),
                f(raw.get("stop_loss")),
            ) <= 0:
                continue
            detected = dt_ms(raw.get("detected_at"))
            if detected <= 0:
                continue

            row = dict(raw)
            row["_detected_ms"] = detected
            row["_score"] = f(raw.get("score"))
            rows.append(row)

    # Eén SELECTIVE-kandidaat per symbol+candle; hoogste score wint.
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("symbol") or "").upper(),
            str(row.get("candle_timestamp") or ""),
        )
        old = grouped.get(key)
        if old is None or row["_score"] > old["_score"]:
            grouped[key] = row

    return sorted(
        grouped.values(),
        key=lambda r: int(r["_detected_ms"]),
        reverse=True,
    )


def fetch_candles(
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
) -> List[List[float]]:
    params = urllib.parse.urlencode({
        "interval": timeframe,
        "start": start_ms,
        "end": end_ms,
        "limit": 1440,
    })
    url = (
        f"{BASE_URL}/{urllib.parse.quote(market_name(symbol), safe='-')}"
        f"/candles?{params}"
    )

    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "DiamondTraderEntryTimingResearch/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))

            if not isinstance(payload, list):
                raise RuntimeError("Candles response is geen lijst")

            rows: List[List[float]] = []
            for item in payload:
                if not isinstance(item, list) or len(item) < 6:
                    continue
                try:
                    rows.append([
                        float(int(item[0])),
                        float(item[1]),
                        float(item[2]),
                        float(item[3]),
                        float(item[4]),
                        float(item[5]),
                    ])
                except Exception:
                    continue

            rows.sort(key=lambda x: x[0])
            return rows

        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))

    raise RuntimeError(
        f"{symbol} {timeframe}: {type(last_exc).__name__}: {last_exc}"
    )


def entry_exec(raw_entry: float, side: str, spread_pct: float) -> float:
    half = max(0.0, spread_pct) / 200.0
    if side == "LONG":
        return raw_entry * (1.0 + half)
    return raw_entry * (1.0 - half)


def exit_exec(raw_exit: float, side: str, spread_pct: float) -> float:
    half = max(0.0, spread_pct) / 200.0
    if side == "LONG":
        return raw_exit * (1.0 - half)
    return raw_exit * (1.0 + half)


def trade_net_pnl(
    raw_entry: float,
    raw_exit: float,
    side: str,
    spread_pct: float,
) -> float:
    e = entry_exec(raw_entry, side, spread_pct)
    x = exit_exec(raw_exit, side, spread_pct)
    amount = STAKE_EUR / e
    entry_fee = STAKE_EUR * FEE_PCT_PER_SIDE / 100.0
    exit_value = amount * x
    exit_fee = exit_value * FEE_PCT_PER_SIDE / 100.0

    if side == "LONG":
        gross = (x - e) * amount
    else:
        gross = (e - x) * amount

    return gross - entry_fee - exit_fee


def find_entry(
    row: Dict[str, Any],
    candles: List[List[float]],
    timeframe_ms: int,
    variant: str,
) -> Optional[Dict[str, Any]]:
    side = str(row.get("side") or "").upper()
    original_entry = f(row.get("entry_price"))
    detected_ms = int(row["_detected_ms"])
    start_ms = ceil_interval(detected_ms, timeframe_ms)
    wait_end = start_ms + WAIT_MINUTES * 60_000

    waiting = [
        c for c in candles
        if start_ms <= int(c[0]) < wait_end
    ]

    if variant == "IMMEDIATE":
        return {
            "raw_entry": original_entry,
            "entry_ms": start_ms,
            "exit_search_ms": start_ms,
            "wait_minutes": 0.0,
        }

    if variant.startswith("PULLBACK_"):
        pct = float(variant.split("_")[1]) / 10_000.0
        if side == "LONG":
            trigger = original_entry * (1.0 - pct)
        else:
            trigger = original_entry * (1.0 + pct)

        for candle in waiting:
            high = float(candle[2])
            low = float(candle[3])
            hit = low <= trigger if side == "LONG" else high >= trigger
            if hit:
                entry_ms = int(candle[0])
                return {
                    "raw_entry": trigger,
                    "entry_ms": entry_ms,
                    # Conservatief: exits mogen in dezelfde candle optreden.
                    "exit_search_ms": entry_ms,
                    "wait_minutes": (entry_ms - start_ms) / 60_000.0,
                }
        return None

    if variant == "CONFIRM_CLOSE":
        for candle in waiting:
            ts = int(candle[0])
            op = float(candle[1])
            close = float(candle[4])

            if side == "LONG":
                confirmed = close > op and close > original_entry
            else:
                confirmed = close < op and close < original_entry

            if confirmed:
                return {
                    "raw_entry": close,
                    "entry_ms": ts + timeframe_ms,
                    # Entry is op candle-close, dus exits vanaf volgende candle.
                    "exit_search_ms": ts + timeframe_ms,
                    "wait_minutes": (
                        ts + timeframe_ms - start_ms
                    ) / 60_000.0,
                }
        return None

    raise ValueError(f"Onbekende variant: {variant}")


def evaluate_trade(
    row: Dict[str, Any],
    candles: List[List[float]],
    timeframe_ms: int,
    variant: str,
) -> Dict[str, Any]:
    side = str(row.get("side") or "").upper()
    tp = f(row.get("take_profit"))
    sl = f(row.get("stop_loss"))
    spread = max(0.0, f(row.get("spread_pct")))
    detected_ms = int(row["_detected_ms"])

    entry = find_entry(row, candles, timeframe_ms, variant)
    if entry is None:
        return {
            "status": "NO_FILL",
            "variant": variant,
            "net_pnl_eur": 0.0,
        }

    raw_entry = float(entry["raw_entry"])
    search_ms = int(entry["exit_search_ms"])
    horizon_end = detected_ms + HORIZON_MINUTES * 60_000

    post = [
        c for c in candles
        if search_ms <= int(c[0]) < horizon_end
    ]

    if not post:
        return {
            "status": "NO_DATA_AFTER_FILL",
            "variant": variant,
            "net_pnl_eur": 0.0,
        }

    for candle in post:
        ts = int(candle[0])
        high = float(candle[2])
        low = float(candle[3])

        if side == "LONG":
            stop_hit = low <= sl
            tp_hit = high >= tp
        else:
            stop_hit = high >= sl
            tp_hit = low <= tp

        # Conservatief bij intra-candle ambiguïteit.
        if stop_hit:
            return {
                "status": "STOP",
                "variant": variant,
                "raw_entry": raw_entry,
                "raw_exit": sl,
                "net_pnl_eur": trade_net_pnl(
                    raw_entry, sl, side, spread
                ),
                "wait_minutes": float(entry["wait_minutes"]),
                "exit_ms": ts,
            }

        if tp_hit:
            return {
                "status": "TP",
                "variant": variant,
                "raw_entry": raw_entry,
                "raw_exit": tp,
                "net_pnl_eur": trade_net_pnl(
                    raw_entry, tp, side, spread
                ),
                "wait_minutes": float(entry["wait_minutes"]),
                "exit_ms": ts,
            }

    last = post[-1]
    raw_exit = float(last[4])
    return {
        "status": "TIME",
        "variant": variant,
        "raw_entry": raw_entry,
        "raw_exit": raw_exit,
        "net_pnl_eur": trade_net_pnl(
            raw_entry, raw_exit, side, spread
        ),
        "wait_minutes": float(entry["wait_minutes"]),
        "exit_ms": int(last[0]),
    }


def profit_factor(values: List[float]) -> Optional[float]:
    gross_profit = sum(x for x in values if x > 0)
    gross_loss = abs(sum(x for x in values if x < 0))
    if gross_loss > 0:
        return gross_profit / gross_loss
    if gross_profit > 0:
        return math.inf
    return None


def max_drawdown(values: List[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def summarize(results: List[Dict[str, Any]], signals_total: int) -> Dict[str, Any]:
    filled = [
        r for r in results
        if r.get("status") in {"TP", "STOP", "TIME"}
    ]
    pnl = [f(r.get("net_pnl_eur")) for r in filled]
    p = profit_factor(pnl)
    waits = [f(r.get("wait_minutes")) for r in filled]

    return {
        "signals_total": signals_total,
        "filled": len(filled),
        "fill_rate_pct": round(
            100.0 * len(filled) / signals_total, 1
        ) if signals_total else 0.0,
        "tp": sum(r.get("status") == "TP" for r in filled),
        "stop": sum(r.get("status") == "STOP" for r in filled),
        "time": sum(r.get("status") == "TIME" for r in filled),
        "wins": sum(x > 0 for x in pnl),
        "losses": sum(x < 0 for x in pnl),
        "pnl_eur": round(sum(pnl), 4),
        "avg_filled_trade_eur": round(
            sum(pnl) / len(pnl), 4
        ) if pnl else None,
        "avg_per_original_signal_eur": round(
            sum(pnl) / signals_total, 4
        ) if signals_total else None,
        "profit_factor": (
            None if p is None
            else "INF" if math.isinf(p)
            else round(p, 4)
        ),
        "max_drawdown_eur": round(max_drawdown(pnl), 4),
        "avg_wait_minutes": round(
            sum(waits) / len(waits), 2
        ) if waits else None,
    }


def run(limit: int) -> Dict[str, Any]:
    signals = load_selective_signals()
    sample = signals[:limit]

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    mature = [
        r for r in sample
        if int(r["_detected_ms"]) + HORIZON_MINUTES * 60_000 <= now_ms
    ]

    candle_cache: Dict[Tuple[str, int, str], List[List[float]]] = {}
    network_errors: List[str] = []

    for row in mature:
        symbol = str(row.get("symbol") or "").upper()
        detected_ms = int(row["_detected_ms"])

        for tf, tf_ms in TIMEFRAMES.items():
            start_ms = ceil_interval(detected_ms, tf_ms)
            end_ms = detected_ms + HORIZON_MINUTES * 60_000
            key = (symbol, detected_ms, tf)

            try:
                candle_cache[key] = fetch_candles(
                    symbol,
                    tf,
                    start_ms,
                    end_ms,
                )
            except Exception as exc:
                network_errors.append(str(exc))
            time.sleep(0.04)

    by_tf: Dict[str, Any] = {}
    for tf, tf_ms in TIMEFRAMES.items():
        tf_rows: Dict[str, Any] = {}
        for variant in VARIANTS:
            results: List[Dict[str, Any]] = []
            for row in mature:
                key = (
                    str(row.get("symbol") or "").upper(),
                    int(row["_detected_ms"]),
                    tf,
                )
                candles = candle_cache.get(key)
                if not candles:
                    continue

                result = evaluate_trade(
                    row,
                    candles,
                    tf_ms,
                    variant,
                )
                results.append({
                    **result,
                    "symbol": str(row.get("symbol") or "").upper(),
                    "side": str(row.get("side") or "").upper(),
                    "strategy": str(row.get("strategy") or ""),
                    "detected_at": str(row.get("detected_at") or ""),
                })

            tf_rows[variant] = {
                "summary": summarize(results, len(mature)),
                "results": results,
            }
        by_tf[tf] = tf_rows

    return {
        "version": VERSION,
        "mode": MODE,
        "generated_at": now_iso(),
        "source": str(SIGNALS),
        "selective_signals_available": len(signals),
        "sample_requested": limit,
        "mature_signals": len(mature),
        "settings": {
            "stake_eur": STAKE_EUR,
            "fee_pct_per_side": FEE_PCT_PER_SIDE,
            "timeframes": list(TIMEFRAMES),
            "wait_minutes": WAIT_MINUTES,
            "horizon_minutes": HORIZON_MINUTES,
            "variants": list(VARIANTS),
            "spread_model": "signal spread reused at entry and exit",
            "tp_sl": "original 15m signal TP/SL",
            "same_candle_tp_sl": "STOP wins conservatively",
        },
        "timeframes": by_tf,
        "network_error_count": len(network_errors),
        "network_errors": network_errors[-20:],
        "safety": SAFETY,
        "limitations": [
            "Historische research, dus nog geen prospectieve bevestiging.",
            "Touch-fill bij pullback is een simulatie, geen echte orderboekfill.",
            "Spread bij exit wordt gelijkgesteld aan signaalspread.",
            "Geen echte slippage/orderboekdiepte in deze eerste timingtest.",
            "Varianten worden alleen vergeleken; geen strategie wordt aangepast.",
        ],
    }


def fmt_pf(value: Any) -> str:
    if value is None:
        return "n/a"
    if str(value).upper() == "INF":
        return "INF"
    try:
        return f"{float(value):.3f}"
    except Exception:
        return "n/a"


def print_report(report: Dict[str, Any]) -> None:
    print("=" * 100)
    print(f" DIAMOND LOWER-TF ENTRY TIMING RESEARCH v{VERSION}")
    print("=" * 100)
    print(
        f"SELECTIVE beschikbaar={report['selective_signals_available']} | "
        f"sample={report['sample_requested']} | "
        f"12h compleet={report['mature_signals']}"
    )
    print(
        f"Stake €{STAKE_EUR:.0f} | fee {FEE_PCT_PER_SIDE:.2f}%/kant | "
        f"wait={WAIT_MINUTES}m | horizon={HORIZON_MINUTES//60}h"
    )
    print("Originele 15m TP/SL blijven staan.")
    print()

    for tf in ("1m", "5m"):
        print(f"=== SELECTIVE ENTRY TIMING | {tf} ===")
        for variant in VARIANTS:
            s = report["timeframes"][tf][variant]["summary"]
            print(
                f"{variant:<14} "
                f"fill={int(s['filled']):>2}/{int(s['signals_total']):<2} "
                f"({f(s['fill_rate_pct']):>5.1f}%) | "
                f"TP/S/T={int(s['tp'])}/{int(s['stop'])}/{int(s['time'])} | "
                f"PnL=€{f(s['pnl_eur']):+8.3f} | "
                f"PF={fmt_pf(s['profit_factor'])} | "
                f"DD=€{f(s['max_drawdown_eur']):.2f} | "
                f"wait={f(s['avg_wait_minutes']):.1f}m"
            )
        print()

    print("=== VEILIGHEID ===")
    print("15m SELECTIVE/Execution gewijzigd : NEE")
    print("Orders/private API               : NEE")
    print("Config/filter/stake/live         : NEE")
    print("Publieke 1m/5m candles           : JA")
    print(f"Netwerkfouten                     : {report['network_error_count']}")
    print(f"Volledig rapport                  : {REPORT}")


def self_test() -> None:
    base = {
        "shadow_eligible": "True",
        "side": "LONG",
        "strategy": "trend_breakout",
        "market_regime": "BULLISH",
        "entry_price": "100",
        "take_profit": "104",
        "stop_loss": "98",
        "spread_pct": "0.10",
        "_detected_ms": 0,
    }
    assert selective_accepts(base)
    assert not selective_accepts({
        **base,
        "strategy": "momentum",
    })
    assert selective_accepts({
        **base,
        "side": "SHORT",
        "strategy": "momentum",
        "market_regime": "BEARISH",
    })

    # Entry algebra / costs.
    net_long = trade_net_pnl(
        100.0, 104.0, "LONG", 0.10
    )
    net_short = trade_net_pnl(
        100.0, 96.0, "SHORT", 0.10
    )
    assert net_long > 0
    assert net_short > 0

    # Pullback long raakt 0.20% trigger.
    tf_ms = 60_000
    base["_detected_ms"] = 60_000
    candles = [
        [120_000, 100.0, 100.1, 99.9, 100.0, 1.0],
        [180_000, 100.0, 100.0, 99.7, 99.8, 1.0],
    ]
    ent = find_entry(base, candles, tf_ms, "PULLBACK_020")
    assert ent is not None
    assert abs(float(ent["raw_entry"]) - 99.8) < 1e-8

    # Confirmation long op groene close boven originele entry.
    candles2 = [
        [120_000, 99.9, 100.0, 99.8, 99.95, 1.0],
        [180_000, 99.95, 100.3, 99.9, 100.2, 1.0],
    ]
    ent2 = find_entry(base, candles2, tf_ms, "CONFIRM_CLOSE")
    assert ent2 is not None
    assert abs(float(ent2["raw_entry"]) - 100.2) < 1e-8
    assert int(ent2["exit_search_ms"]) == 240_000

    # Conservatief: stop en TP in dezelfde candle => STOP.
    base["_detected_ms"] = 60_000
    both = [
        [120_000, 100.0, 105.0, 97.0, 101.0, 1.0],
    ]
    r = evaluate_trade(base, both, tf_ms, "IMMEDIATE")
    assert r["status"] == "STOP"

    assert ceil_interval(61_001, 60_000) == 120_000
    assert SAFETY["orders"] is False
    assert SAFETY["private_api"] is False
    assert SAFETY["live_change"] is False

    print("LOWER_TF_ENTRY_TIMING_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diamond lower-timeframe entry timing research"
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Laatste N unieke SELECTIVE-signalen (standaard 50).",
    )
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    limit = max(10, min(100, int(args.limit)))
    try:
        report = run(limit)
        atomic_json(REPORT, report)
        print_report(report)
        return 0
    except Exception as exc:
        print("=" * 100)
        print(f" DIAMOND LOWER-TF ENTRY TIMING RESEARCH v{VERSION}")
        print("=" * 100)
        print(f"STATUS: FOUT | {type(exc).__name__}: {exc}")
        print("Orders/private API/config/live: NEE")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
