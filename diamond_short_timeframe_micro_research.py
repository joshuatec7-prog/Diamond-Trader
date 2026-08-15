#!/usr/bin/env python3
"""
Diamond Trader Short-Timeframe Micro Profit Research v1.0

Doel
----
Onderzoekt of we met bestaande Market Scanner-signalen informatie/kansen missen
door alleen 15m-candles te gebruiken.

Dit script verandert NIETS aan de bestaande 15m SELECTIVE/Execution-test.
Het gebruikt bestaande scanner-signalen en publieke Bitvavo candles om
1m en 5m achteraf naast elkaar te leggen.

Cohorten
--------
ALL_ELIGIBLE
    Alle signalen die de bestaande scanner al shadow_eligible=True gaf.

SELECTIVE
    Exact dezelfde SELECTIVE-keuze als scanner_selective_shadow_lab.py:
    - LONG + trend_breakout
    - SHORT + BEARISH_WEAK
    - SHORT + momentum
    - SHORT + pullback_retest

Micro-doelen
------------
Netto doel na gesimuleerde taker-fees en spread:
€0.05, €0.25, €0.50, €1.00 op €130 research-stake.

Voor elk signaal:
- huidige signaal-entry/spread wordt gebruikt;
- originele stop-loss blijft staan;
- alleen VOLLEDIGE 1m/5m candles NA detected_at worden gebruikt;
- bij target+stop in dezelfde candle wint conservatief de stop;
- als geen target/stop binnen horizon valt: time-exit op candle-close.

Horizons
--------
15m, 30m, 60m, 120m.

Veiligheid
----------
- read-only t.o.v. bot/scanner/trades/config;
- publieke Bitvavo REST candles;
- geen API keys;
- geen private API;
- geen orders;
- geen live/config/filter/stake wijziging;
- schrijft uitsluitend eigen JSON-rapport.

Gebruik
-------
python3 diamond_short_timeframe_micro_research.py --self-test
python3 diamond_short_timeframe_micro_research.py
python3 diamond_short_timeframe_micro_research.py --limit-per-cohort 80
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
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "1.0"
MODE = "READ_ONLY_SHORT_TIMEFRAME_MICRO_RESEARCH"

DATA = Path(os.getenv("DIAMOND_DATA_DIR", "/var/data"))
SIGNALS = DATA / "diamond_market_signals.csv"
REPORT = DATA / "diamond_short_timeframe_micro_research.json"

BASE_URL = "https://api.bitvavo.com/v2"
RESEARCH_STAKE_EUR = 130.0
FEE_PCT_PER_SIDE = 0.25

TIMEFRAMES = {
    "1m": 60_000,
    "5m": 300_000,
}
HORIZONS_MIN = (15, 30, 60, 120)
NET_TARGETS_EUR = (0.05, 0.25, 0.50, 1.00)

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


def load_signals() -> List[Dict[str, Any]]:
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

        rows = []
        for raw in reader:
            if not to_bool(raw.get("shadow_eligible")):
                continue
            side = str(raw.get("side") or "").upper()
            if side not in {"LONG", "SHORT"}:
                continue
            if min(
                f(raw.get("entry_price")),
                f(raw.get("stop_loss")),
            ) <= 0:
                continue
            detected_ms = dt_ms(raw.get("detected_at"))
            if detected_ms <= 0:
                continue

            row = dict(raw)
            row["_detected_ms"] = detected_ms
            row["_score"] = f(raw.get("score"))
            row["_selective"] = selective_accepts(raw)
            rows.append(row)
        return rows


def dedupe_for_cohort(
    rows: Iterable[Dict[str, Any]],
    selective_only: bool,
) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        if selective_only and not row["_selective"]:
            continue
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


def ceil_interval(ms: int, interval_ms: int) -> int:
    return ((ms + interval_ms - 1) // interval_ms) * interval_ms


def market_name(symbol: str) -> str:
    return str(symbol).upper().replace("/", "-")


def fetch_candles(
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
) -> List[List[Any]]:
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
                    "User-Agent": "DiamondTraderResearch/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, list):
                raise RuntimeError("Candles response is geen lijst")

            rows: List[List[Any]] = []
            for item in payload:
                if not isinstance(item, list) or len(item) < 6:
                    continue
                try:
                    rows.append([
                        int(item[0]),
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
                time.sleep(0.4 * (attempt + 1))

    raise RuntimeError(
        f"{symbol} {timeframe}: {type(last_exc).__name__}: {last_exc}"
    )


def build_position(row: Dict[str, Any]) -> Dict[str, float | str]:
    raw_entry = f(row.get("entry_price"))
    raw_stop = f(row.get("stop_loss"))
    spread_pct = max(0.0, f(row.get("spread_pct")))
    half_spread = spread_pct / 200.0
    side = str(row.get("side") or "").upper()

    if side == "LONG":
        entry_exec = raw_entry * (1.0 + half_spread)
    else:
        entry_exec = raw_entry * (1.0 - half_spread)

    delta = entry_exec - raw_entry
    stop_raw = raw_stop + delta
    amount = RESEARCH_STAKE_EUR / entry_exec
    entry_fee = RESEARCH_STAKE_EUR * FEE_PCT_PER_SIDE / 100.0

    return {
        "side": side,
        "entry_exec": entry_exec,
        "stop_raw": stop_raw,
        "spread_pct": spread_pct,
        "half_spread": half_spread,
        "amount": amount,
        "entry_fee": entry_fee,
    }


def raw_target_for_net(
    position: Dict[str, Any],
    net_target_eur: float,
) -> float:
    side = str(position["side"])
    entry_exec = float(position["entry_exec"])
    amount = float(position["amount"])
    half_spread = float(position["half_spread"])
    fee = FEE_PCT_PER_SIDE / 100.0

    if side == "LONG":
        exit_exec = (
            net_target_eur / amount + entry_exec * (1.0 + fee)
        ) / (1.0 - fee)
        return exit_exec / (1.0 - half_spread)

    exit_exec = (
        entry_exec * (1.0 - fee) - net_target_eur / amount
    ) / (1.0 + fee)
    return exit_exec / (1.0 + half_spread)


def net_for_raw_exit(
    position: Dict[str, Any],
    raw_exit: float,
) -> float:
    side = str(position["side"])
    entry_exec = float(position["entry_exec"])
    amount = float(position["amount"])
    half_spread = float(position["half_spread"])
    entry_fee = float(position["entry_fee"])
    fee = FEE_PCT_PER_SIDE / 100.0

    if side == "LONG":
        exit_exec = raw_exit * (1.0 - half_spread)
        gross = (exit_exec - entry_exec) * amount
    else:
        exit_exec = raw_exit * (1.0 + half_spread)
        gross = (entry_exec - exit_exec) * amount

    exit_fee = amount * exit_exec * fee
    return gross - entry_fee - exit_fee


def evaluate_window(
    position: Dict[str, Any],
    candles: List[List[Any]],
    target_net: float,
    horizon_min: int,
    start_ms: int,
) -> Optional[Dict[str, Any]]:
    target_raw = raw_target_for_net(position, target_net)
    stop_raw = float(position["stop_raw"])
    side = str(position["side"])
    end_ms = start_ms + horizon_min * 60_000

    relevant = [
        c for c in candles
        if start_ms <= int(c[0]) < end_ms
    ]
    if not relevant:
        return None

    for candle in relevant:
        high = float(candle[2])
        low = float(candle[3])

        if side == "LONG":
            stop_hit = low <= stop_raw
            target_hit = high >= target_raw
        else:
            stop_hit = high >= stop_raw
            target_hit = low <= target_raw

        # Conservatief bij onduidelijke intra-candle volgorde.
        if stop_hit:
            return {
                "outcome": "STOP",
                "net_pnl_eur": net_for_raw_exit(position, stop_raw),
                "target_raw": target_raw,
                "exit_raw": stop_raw,
                "exit_candle_ms": int(candle[0]),
            }

        if target_hit:
            return {
                "outcome": "TARGET",
                "net_pnl_eur": net_for_raw_exit(position, target_raw),
                "target_raw": target_raw,
                "exit_raw": target_raw,
                "exit_candle_ms": int(candle[0]),
            }

    last_close = float(relevant[-1][4])
    return {
        "outcome": "TIME",
        "net_pnl_eur": net_for_raw_exit(position, last_close),
        "target_raw": target_raw,
        "exit_raw": last_close,
        "exit_candle_ms": int(relevant[-1][0]),
    }


def pf(values: List[float]) -> Optional[float]:
    gp = sum(x for x in values if x > 0)
    gl = abs(sum(x for x in values if x < 0))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def summarize(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnl = [f(x.get("net_pnl_eur")) for x in outcomes]
    targets = sum(x.get("outcome") == "TARGET" for x in outcomes)
    stops = sum(x.get("outcome") == "STOP" for x in outcomes)
    times = sum(x.get("outcome") == "TIME" for x in outcomes)
    wins = sum(x > 0 for x in pnl)
    losses = sum(x < 0 for x in pnl)
    p = pf(pnl)

    return {
        "n": len(outcomes),
        "target_hits": targets,
        "stop_hits": stops,
        "time_exits": times,
        "wins": wins,
        "losses": losses,
        "target_hit_pct": round(100 * targets / len(outcomes), 1) if outcomes else 0.0,
        "positive_pct": round(100 * wins / len(outcomes), 1) if outcomes else 0.0,
        "pnl_eur": round(sum(pnl), 4),
        "average_trade_eur": round(sum(pnl) / len(pnl), 4) if pnl else None,
        "profit_factor": (
            None if p is None
            else "INF" if math.isinf(p)
            else round(p, 4)
        ),
    }


def run(limit_per_cohort: int) -> Dict[str, Any]:
    rows = load_signals()
    all_rows = dedupe_for_cohort(rows, selective_only=False)[:limit_per_cohort]
    sel_rows = dedupe_for_cohort(rows, selective_only=True)[:limit_per_cohort]

    cohorts = {
        "ALL_ELIGIBLE": all_rows,
        "SELECTIVE": sel_rows,
    }

    # Union zodat dezelfde market/tijd maar één keer per timeframe wordt opgehaald.
    union: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for items in cohorts.values():
        for row in items:
            union[
                (str(row.get("symbol") or "").upper(), int(row["_detected_ms"]))
            ] = row

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    max_horizon_ms = max(HORIZONS_MIN) * 60_000
    candle_cache: Dict[Tuple[str, int, str], List[List[Any]]] = {}
    errors: List[str] = []

    for row in union.values():
        symbol = str(row.get("symbol") or "").upper()
        detected_ms = int(row["_detected_ms"])

        # Alleen signalen met volledige 120m historie meenemen.
        if detected_ms + max_horizon_ms > now_ms:
            continue

        for tf, tf_ms in TIMEFRAMES.items():
            start_ms = ceil_interval(detected_ms, tf_ms)
            end_ms = start_ms + max_horizon_ms
            key = (symbol, detected_ms, tf)
            try:
                candle_cache[key] = fetch_candles(
                    symbol,
                    tf,
                    start_ms,
                    end_ms,
                )
            except Exception as exc:
                errors.append(str(exc))
            time.sleep(0.03)

    details: Dict[str, Any] = {}
    for cohort_name, cohort_rows in cohorts.items():
        cohort_report: Dict[str, Any] = {}
        for tf, tf_ms in TIMEFRAMES.items():
            tf_report: Dict[str, Any] = {}
            for horizon in HORIZONS_MIN:
                horizon_report: Dict[str, Any] = {}
                for target in NET_TARGETS_EUR:
                    outcomes = []
                    for row in cohort_rows:
                        detected_ms = int(row["_detected_ms"])
                        if detected_ms + max_horizon_ms > now_ms:
                            continue

                        key = (
                            str(row.get("symbol") or "").upper(),
                            detected_ms,
                            tf,
                        )
                        candles = candle_cache.get(key)
                        if not candles:
                            continue

                        start_ms = ceil_interval(detected_ms, tf_ms)
                        result = evaluate_window(
                            build_position(row),
                            candles,
                            target,
                            horizon,
                            start_ms,
                        )
                        if result is None:
                            continue
                        outcomes.append({
                            **result,
                            "symbol": str(row.get("symbol") or "").upper(),
                            "side": str(row.get("side") or "").upper(),
                            "strategy": str(row.get("strategy") or ""),
                            "detected_at": str(row.get("detected_at") or ""),
                            "spread_pct": f(row.get("spread_pct")),
                        })

                    horizon_report[f"{target:.2f}"] = {
                        **summarize(outcomes),
                        "target_net_eur": target,
                    }
                tf_report[f"{horizon}m"] = horizon_report
            cohort_report[tf] = tf_report
        details[cohort_name] = cohort_report

    return {
        "version": VERSION,
        "mode": MODE,
        "generated_at": now_iso(),
        "source": str(SIGNALS),
        "eligible_rows_in_source": len(rows),
        "limit_per_cohort": limit_per_cohort,
        "selected_rows": {
            "ALL_ELIGIBLE": len(all_rows),
            "SELECTIVE": len(sel_rows),
        },
        "research_settings": {
            "stake_eur": RESEARCH_STAKE_EUR,
            "fee_pct_per_side": FEE_PCT_PER_SIDE,
            "exit_spread_assumption": "entry spread reused",
            "timeframes": list(TIMEFRAMES),
            "horizons_minutes": list(HORIZONS_MIN),
            "net_targets_eur": list(NET_TARGETS_EUR),
            "same_candle_target_stop": "STOP wins conservatively",
            "start_rule": "first full lower-timeframe candle after detected_at",
            "original_stop_loss_preserved": True,
        },
        "cohorts": details,
        "network_errors": errors[-20:],
        "network_error_count": len(errors),
        "safety": SAFETY,
        "limitations": [
            "Historische screening; nog geen prospectieve bevestiging.",
            "Een micro target kan vaak raken maar alsnog slechte PF hebben door grotere stops.",
            "Spread op exit wordt conservatief gelijkgesteld aan entry-spread.",
            "Geen orderboekdiepte of echte slippage in deze eerste screening.",
            "Geen wijziging aan bestaande 15m SELECTIVE/Execution-test.",
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
    print("=" * 96)
    print(f" DIAMOND SHORT-TIMEFRAME MICRO RESEARCH v{VERSION}")
    print("=" * 96)
    print(
        f"Bron eligible={report['eligible_rows_in_source']} | "
        f"sample ALL={report['selected_rows']['ALL_ELIGIBLE']} | "
        f"SELECTIVE={report['selected_rows']['SELECTIVE']}"
    )
    print(
        f"Stake €{RESEARCH_STAKE_EUR:.0f} | fee {FEE_PCT_PER_SIDE:.2f}%/kant | "
        "1m + 5m | targets netto €0.05/0.25/0.50/1.00"
    )
    print()

    # Compact: SELECTIVE 30m en 60m zijn de eerste relevante vergelijking.
    print("=== SELECTIVE | 30m ===")
    for tf in ("1m", "5m"):
        for target in NET_TARGETS_EUR:
            row = (
                report["cohorts"]["SELECTIVE"][tf]["30m"]
                .get(f"{target:.2f}", {})
            )
            print(
                f"{tf} target €{target:>4.2f} | "
                f"n={int(row.get('n') or 0):>2} "
                f"T/S/X={int(row.get('target_hits') or 0)}/"
                f"{int(row.get('stop_hits') or 0)}/"
                f"{int(row.get('time_exits') or 0)} "
                f"hit={f(row.get('target_hit_pct')):>5.1f}% "
                f"PnL=€{f(row.get('pnl_eur')):+8.3f} "
                f"PF={fmt_pf(row.get('profit_factor'))}"
            )

    print()
    print("=== SELECTIVE | 60m ===")
    for tf in ("1m", "5m"):
        for target in NET_TARGETS_EUR:
            row = (
                report["cohorts"]["SELECTIVE"][tf]["60m"]
                .get(f"{target:.2f}", {})
            )
            print(
                f"{tf} target €{target:>4.2f} | "
                f"n={int(row.get('n') or 0):>2} "
                f"T/S/X={int(row.get('target_hits') or 0)}/"
                f"{int(row.get('stop_hits') or 0)}/"
                f"{int(row.get('time_exits') or 0)} "
                f"hit={f(row.get('target_hit_pct')):>5.1f}% "
                f"PnL=€{f(row.get('pnl_eur')):+8.3f} "
                f"PF={fmt_pf(row.get('profit_factor'))}"
            )

    print()
    print("=== VEILIGHEID ===")
    print("Bestaande 15m test gewijzigd : NEE")
    print("Orders/private API           : NEE")
    print("Config/filter/live           : NEE")
    print("Alleen publieke candles      : JA")
    print(f"Netwerkfouten                 : {report['network_error_count']}")
    print(f"Volledig rapport              : {REPORT}")


def self_test() -> None:
    base = {
        "shadow_eligible": "True",
        "side": "LONG",
        "strategy": "trend_breakout",
        "market_regime": "BULLISH",
        "entry_price": "100",
        "stop_loss": "98",
        "spread_pct": "0.10",
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
    assert not selective_accepts({
        **base,
        "shadow_eligible": "False",
    })

    p = build_position(base)
    for target in NET_TARGETS_EUR:
        raw = raw_target_for_net(p, target)
        net = net_for_raw_exit(p, raw)
        assert abs(net - target) < 1e-7, (target, net)

    # LONG: target in eerste 1m.
    start = 1_000_000
    target_raw = raw_target_for_net(p, 0.50)
    candles = [[
        start,
        100.0,
        target_raw * 1.001,
        99.9,
        target_raw,
        1.0,
    ]]
    result = evaluate_window(p, candles, 0.50, 15, start)
    assert result and result["outcome"] == "TARGET"
    assert abs(float(result["net_pnl_eur"]) - 0.50) < 1e-6

    # Beide geraakt => conservatief STOP.
    candles2 = [[
        start,
        100.0,
        target_raw * 1.001,
        float(p["stop_raw"]) * 0.999,
        100.0,
        1.0,
    ]]
    result2 = evaluate_window(p, candles2, 0.50, 15, start)
    assert result2 and result2["outcome"] == "STOP"

    # SHORT target algebra.
    short = {
        **base,
        "side": "SHORT",
        "strategy": "momentum",
        "entry_price": "100",
        "stop_loss": "102",
    }
    ps = build_position(short)
    for target in NET_TARGETS_EUR:
        raw = raw_target_for_net(ps, target)
        net = net_for_raw_exit(ps, raw)
        assert abs(net - target) < 1e-7, (target, net)

    assert ceil_interval(61_001, 60_000) == 120_000
    assert SAFETY["orders"] is False
    assert SAFETY["private_api"] is False
    assert SAFETY["live_change"] is False

    print("SHORT_TIMEFRAME_MICRO_RESEARCH_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diamond short-timeframe micro profit research"
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--limit-per-cohort",
        type=int,
        default=50,
        help="Laatste N unieke signalen per cohort (standaard 50).",
    )
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    limit = max(10, min(150, int(args.limit_per_cohort)))
    try:
        report = run(limit)
        atomic_json(REPORT, report)
        print_report(report)
        return 0
    except Exception as exc:
        print("=" * 96)
        print(f" DIAMOND SHORT-TIMEFRAME MICRO RESEARCH v{VERSION}")
        print("=" * 96)
        print(f"STATUS: FOUT | {type(exc).__name__}: {exc}")
        print("Orders/private API/config/live: NEE")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
