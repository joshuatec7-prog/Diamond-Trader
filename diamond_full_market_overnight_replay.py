#!/usr/bin/env python3
"""
Diamond Trader Full Market Overnight Replay v1.0

Research-only audit for missed LONG opportunities across every active Bitvavo
EUR spot market. It uses only public OHLCV/market data and never places orders,
uses private endpoints, or changes bot/config/state.

Important limitation:
Bitvavo does not provide historical bid/ask order books through the candle
history used here. Therefore historical spread is unknown for signals that were
not recorded by the live scanner. A candidate is called ROBUST only when it
would pass all scanner economics even at the configured maximum trade spread
(default 0.10%). That still assumes the real spread at that moment was <= that
limit. Existing recorded scanner signals keep their real recorded spread.

The replay:
- loads all active EUR spot markets;
- fetches one public 15m OHLCV history per market;
- reconstructs 1h and 4h candles from 15m data;
- replays the current scanner trigger logic on every 15m close in the window;
- estimates historical 24h quote volume from candles;
- identifies signals that were technically/economically robust but absent from
  diamond_market_signals.csv (coverage misses);
- labels whether each missed signal would be SELECTIVE under current rules;
- simulates TP/SL conservatively on later 15m candles with €130 and taker fees.

No deployment is required to run this as a standalone research tool.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import ccxt
import pandas as pd

import market_scanner as ms
from diamond_selective_rules import selective_accepts

VERSION = "1.0"
DATA_DIR = Path("/var/data")
SIGNALS_PATH = DATA_DIR / "diamond_market_signals.csv"
DEFAULT_JSON = DATA_DIR / "diamond_full_market_overnight_replay.json"
DEFAULT_CSV = DATA_DIR / "diamond_full_market_overnight_replay.csv"
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
UTC = timezone.utc
TIMEFRAME_MS = 15 * 60 * 1000
LOOKBACK_DAYS = 9.6
FETCH_LIMIT = 1000
STAKE_EUR = 130.0


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def parse_dt(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def floor_15m(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def default_window() -> Tuple[datetime, datetime]:
    now_local = datetime.now(LOCAL_TZ)
    start_local = (now_local - timedelta(days=1)).replace(
        hour=22, minute=0, second=0, microsecond=0
    )
    if start_local > now_local:
        start_local -= timedelta(days=1)
    return start_local.astimezone(UTC), floor_15m(now_local.astimezone(UTC))


def parse_window(start_text: Optional[str], end_text: Optional[str]) -> Tuple[datetime, datetime]:
    default_start, default_end = default_window()
    start = parse_dt(start_text) if start_text else default_start
    end = parse_dt(end_text) if end_text else default_end
    if start is None or end is None:
        raise ValueError("ongeldige start/eindtijd")
    start = floor_15m(start)
    end = floor_15m(end)
    if end <= start:
        raise ValueError("eindtijd moet na starttijd liggen")
    return start, end


def public_exchange() -> ccxt.Exchange:
    exchange = ccxt.bitvavo({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"fetchMarkets": {"types": ["spot"]}},
    })
    exchange.load_markets()
    return exchange


def active_eur_symbols(exchange: ccxt.Exchange, quote: str, excluded: Iterable[str]) -> List[str]:
    excluded_set = {str(x).upper() for x in excluded}
    result: List[str] = []
    for symbol, market in exchange.markets.items():
        if not isinstance(market, dict):
            continue
        if str(market.get("quote") or "").upper() != quote:
            continue
        if market.get("spot") is False or market.get("active") is False:
            continue
        base = str(market.get("base") or "").upper()
        if not base or base in excluded_set or ms.leveraged_token(base):
            continue
        result.append(symbol)
    return sorted(set(result))


def ohlcv_frame(rows: List[List[Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    if frame.empty:
        return frame
    for col in frame.columns:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame.dropna(inplace=True)
    frame.sort_values("timestamp", inplace=True)
    frame.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
    frame["timestamp"] = frame["timestamp"].astype("int64")
    frame["timestamp_iso"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame.reset_index(drop=True, inplace=True)
    return frame


def resample_ohlcv(frame15: pd.DataFrame, rule: str) -> pd.DataFrame:
    if frame15.empty:
        return frame15.copy()
    indexed = frame15.set_index("timestamp_iso")
    agg = indexed.resample(rule, origin="epoch", label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    })
    agg.dropna(inplace=True)
    agg.reset_index(inplace=True)
    agg["timestamp"] = (agg["timestamp_iso"].astype("int64") // 1_000_000).astype("int64")
    return agg[["timestamp", "open", "high", "low", "close", "volume", "timestamp_iso"]]


def slice_closed(frame: pd.DataFrame, scan_ms: int, tf_ms: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame[(frame["timestamp"] + tf_ms) <= scan_ms].copy().reset_index(drop=True)


def historical_quote_volume(frame15: pd.DataFrame, scan_ms: int) -> float:
    lo = scan_ms - 24 * 60 * 60 * 1000
    rows = frame15[(frame15["timestamp"] >= lo) & ((frame15["timestamp"] + TIMEFRAME_MS) <= scan_ms)]
    if rows.empty:
        return 0.0
    return float((rows["volume"] * rows["close"]).sum())


def historical_change_24h(frame15: pd.DataFrame, scan_ms: int) -> float:
    complete = frame15[(frame15["timestamp"] + TIMEFRAME_MS) <= scan_ms]
    if complete.empty:
        return 0.0
    last = finite(complete.iloc[-1]["close"])
    cutoff = scan_ms - 24 * 60 * 60 * 1000
    prior = complete[complete["timestamp"] <= cutoff]
    if prior.empty:
        return 0.0
    old = finite(prior.iloc[-1]["close"])
    if old <= 0:
        return 0.0
    return (last / old - 1.0) * 100.0


def signal_identity(symbol: str, strategy: str, side: str, candle_ms: int) -> str:
    return f"{symbol}|{strategy}|{side}|{int(candle_ms)}"


def load_recorded_signals(path: Path) -> Tuple[set[str], Dict[str, Dict[str, Any]]]:
    identities: set[str] = set()
    rows_by_identity: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return identities, rows_by_identity
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "")
            strategy = str(row.get("strategy") or "")
            side = str(row.get("side") or "").upper()
            candle_ms = int(finite(row.get("candle_timestamp_ms"), 0))
            if candle_ms <= 0:
                parsed = parse_dt(row.get("candle_timestamp"))
                candle_ms = int(parsed.timestamp() * 1000) if parsed else 0
            if not symbol or not strategy or not side or candle_ms <= 0:
                continue
            ident = signal_identity(symbol, strategy, side, candle_ms)
            identities.add(ident)
            rows_by_identity[ident] = row
    return identities, rows_by_identity


def signal_map(signals: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in signals:
        result[(str(row.get("strategy") or ""), str(row.get("side") or "").upper())] = row
    return result


def simulate_long(frame15: pd.DataFrame, scan_ms: int, entry: float, tp: float, sl: float, fee_pct: float) -> Dict[str, Any]:
    future = frame15[frame15["timestamp"] >= scan_ms]
    if future.empty or entry <= 0 or tp <= 0 or sl <= 0:
        return {"outcome": "NO_DATA", "net_pnl_eur": 0.0, "exit_ms": None, "exit_price": None}

    outcome = "OPEN_AT_END"
    exit_price = finite(future.iloc[-1]["close"], entry)
    exit_ms = int(future.iloc[-1]["timestamp"] + TIMEFRAME_MS)

    for _, candle in future.iterrows():
        high = finite(candle["high"])
        low = finite(candle["low"])
        hit_tp = high >= tp
        hit_sl = low <= sl
        if hit_tp and hit_sl:
            outcome = "STOP_LOSS_BOTH_15M"
            exit_price = sl
            exit_ms = int(candle["timestamp"] + TIMEFRAME_MS)
            break
        if hit_sl:
            outcome = "STOP_LOSS"
            exit_price = sl
            exit_ms = int(candle["timestamp"] + TIMEFRAME_MS)
            break
        if hit_tp:
            outcome = "TAKE_PROFIT"
            exit_price = tp
            exit_ms = int(candle["timestamp"] + TIMEFRAME_MS)
            break

    amount = STAKE_EUR / entry
    buy_fee = STAKE_EUR * fee_pct / 100.0
    gross_exit = amount * exit_price
    sell_fee = gross_exit * fee_pct / 100.0
    net = gross_exit - sell_fee - STAKE_EUR - buy_fee

    max_high = float(future["high"].max())
    min_low = float(future["low"].min())
    return {
        "outcome": outcome,
        "net_pnl_eur": round(net, 4),
        "exit_ms": exit_ms,
        "exit_price": exit_price,
        "max_up_pct": round((max_high / entry - 1.0) * 100.0, 4),
        "max_down_pct": round((min_low / entry - 1.0) * 100.0, 4),
    }


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    fields = [
        "scan_time_utc", "symbol", "strategy", "market_regime", "score_at_spread_010",
        "historical_quote_volume", "historical_change_24h_pct", "entry_price", "take_profit",
        "stop_loss", "reward_risk_at_spread_010", "recorded_by_scanner", "selective_now",
        "historical_spread_known", "historical_spread_pct", "classification", "outcome",
        "net_pnl_eur", "max_up_pct", "max_down_pct", "exit_time_utc",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def run_replay(start: datetime, end: datetime, progress_every: int) -> Dict[str, Any]:
    config = ms.load_yaml(ms.CFG_FILE)
    cfg = ms.settings(config, top_override=None)
    exchange = public_exchange()
    symbols = active_eur_symbols(exchange, cfg["quote"], cfg["exclude_bases"])
    recorded, recorded_rows = load_recorded_signals(SIGNALS_PATH)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    fetch_since = int((start - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)
    scan_times = list(range(start_ms + TIMEFRAME_MS, end_ms + 1, TIMEFRAME_MS))

    candidates: List[Dict[str, Any]] = []
    processed = 0
    insufficient = 0
    errors: List[Dict[str, str]] = []
    technical_count = 0
    robust_count = 0

    print("=" * 96, flush=True)
    print(f" DIAMOND FULL MARKET OVERNIGHT REPLAY v{VERSION}", flush=True)
    print("=" * 96, flush=True)
    print(f"Periode UTC          : {start.isoformat()} -> {end.isoformat()}", flush=True)
    print(f"Periode NL           : {start.astimezone(LOCAL_TZ):%d-%m %H:%M} -> {end.astimezone(LOCAL_TZ):%d-%m %H:%M}", flush=True)
    print(f"Actieve EUR-markten  : {len(symbols)}", flush=True)
    print("Historische spread   : onbekend indien scanner niets opsloeg", flush=True)
    print("Orders/private API   : NEE", flush=True)
    print("", flush=True)

    for index, symbol in enumerate(symbols, start=1):
        try:
            rows = exchange.fetch_ohlcv(symbol, "15m", since=fetch_since, limit=FETCH_LIMIT)
            frame15 = ohlcv_frame(rows)
            if len(frame15) < 220:
                insufficient += 1
                continue
            frame1h = resample_ohlcv(frame15, "1h")
            frame4h = resample_ohlcv(frame15, "4h")

            for scan_ms in scan_times:
                d15 = slice_closed(frame15, scan_ms, 15 * 60 * 1000)
                d1h = slice_closed(frame1h, scan_ms, 60 * 60 * 1000)
                d4h = slice_closed(frame4h, scan_ms, 4 * 60 * 60 * 1000)
                if len(d15) < 60 or len(d1h) < 60 or len(d4h) < 53:
                    continue

                try:
                    snapshots = {
                        "15m": ms.snapshot(d15, cfg),
                        "1h": ms.snapshot(d1h, cfg),
                        "4h": ms.snapshot(d4h, cfg),
                    }
                except Exception:
                    continue

                qv = historical_quote_volume(frame15, scan_ms)
                if qv < cfg["min_quote_volume"]:
                    continue
                change24 = historical_change_24h(frame15, scan_ms)

                market_best = {
                    "symbol": symbol,
                    "quote_volume": qv,
                    "spread_pct": 0.0,
                    "change_pct_24h": change24,
                    "selection_reason": "FULL_REPLAY",
                }
                market_limit = dict(market_best)
                market_limit["spread_pct"] = cfg["trade_max_spread_pct"]

                best_signals, _, _ = ms.find_signals(market_best, snapshots, cfg)
                limit_signals, _, _ = ms.find_signals(market_limit, snapshots, cfg)
                best_long = signal_map(x for x in best_signals if str(x.get("side")).upper() == "LONG")
                limit_long = signal_map(x for x in limit_signals if str(x.get("side")).upper() == "LONG")
                technical_count += len(best_long)

                for key, robust in limit_long.items():
                    if not bool(robust.get("shadow_eligible")):
                        continue
                    robust_count += 1
                    strategy, side = key
                    candle_ms = int(finite(robust.get("candle_timestamp_ms"), 0))
                    ident = signal_identity(symbol, strategy, side, candle_ms)
                    was_recorded = ident in recorded
                    recorded_row = recorded_rows.get(ident, {})
                    spread_known = was_recorded and recorded_row.get("spread_pct") not in (None, "")
                    real_spread = finite(recorded_row.get("spread_pct"), 0.0) if spread_known else None

                    replay_for_selective = dict(robust)
                    replay_for_selective["shadow_eligible"] = True
                    is_selective = selective_accepts(replay_for_selective)
                    if was_recorded:
                        classification = "RECORDED"
                    elif is_selective:
                        classification = "COVERAGE_MISS_SELECTIVE_IF_SPREAD_OK"
                    else:
                        classification = "COVERAGE_MISS_NONSELECTIVE_IF_SPREAD_OK"

                    simulation = simulate_long(
                        frame15,
                        scan_ms,
                        finite(robust.get("entry_price")),
                        finite(robust.get("take_profit")),
                        finite(robust.get("stop_loss")),
                        cfg["fee_pct_per_side"],
                    )
                    exit_dt = (
                        datetime.fromtimestamp(simulation["exit_ms"] / 1000, tz=UTC).isoformat()
                        if simulation.get("exit_ms")
                        else None
                    )
                    candidates.append({
                        "scan_time_utc": datetime.fromtimestamp(scan_ms / 1000, tz=UTC).isoformat(),
                        "symbol": symbol,
                        "strategy": strategy,
                        "market_regime": robust.get("market_regime"),
                        "score_at_spread_010": finite(robust.get("score")),
                        "historical_quote_volume": round(qv, 2),
                        "historical_change_24h_pct": round(change24, 4),
                        "entry_price": finite(robust.get("entry_price")),
                        "take_profit": finite(robust.get("take_profit")),
                        "stop_loss": finite(robust.get("stop_loss")),
                        "reward_risk_at_spread_010": finite(robust.get("reward_risk")),
                        "recorded_by_scanner": was_recorded,
                        "selective_now": is_selective,
                        "historical_spread_known": spread_known,
                        "historical_spread_pct": real_spread,
                        "classification": classification,
                        **simulation,
                        "exit_time_utc": exit_dt,
                    })

            processed += 1
        except Exception as exc:
            errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

        if index % max(1, progress_every) == 0 or index == len(symbols):
            print(
                f"Voortgang {index:>3}/{len(symbols)} | verwerkt={processed} "
                f"onvoldoende={insufficient} fouten={len(errors)} kandidaten={len(candidates)}",
                flush=True,
            )

    unique: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for row in candidates:
        key = (row["scan_time_utc"], row["symbol"], row["strategy"], row["classification"])
        unique[key] = row
    candidates = sorted(unique.values(), key=lambda r: (r["scan_time_utc"], r["symbol"], r["strategy"]))

    misses = [r for r in candidates if str(r["classification"]).startswith("COVERAGE_MISS")]
    selective_misses = [r for r in misses if r["selective_now"]]
    nonselective_misses = [r for r in misses if not r["selective_now"]]
    winners = [r for r in misses if finite(r.get("net_pnl_eur")) > 0]
    losers = [r for r in misses if finite(r.get("net_pnl_eur")) < 0]

    summary = {
        "version": VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "research_only": True,
        "orders_used": False,
        "private_api_used": False,
        "config_changed": False,
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "markets_total": len(symbols),
        "markets_processed": processed,
        "markets_insufficient_history": insufficient,
        "market_errors": len(errors),
        "technical_long_triggers_best_case_spread": technical_count,
        "robust_long_candidates_if_spread_le_limit": robust_count,
        "candidate_rows": len(candidates),
        "coverage_misses_if_historical_spread_ok": len(misses),
        "coverage_miss_selective": len(selective_misses),
        "coverage_miss_nonselective": len(nonselective_misses),
        "coverage_miss_winners": len(winners),
        "coverage_miss_losers": len(losers),
        "coverage_miss_independent_net_total_eur": round(sum(finite(r.get("net_pnl_eur")) for r in misses), 4),
        "strategy_counts_misses": dict(Counter(r["strategy"] for r in misses)),
        "historical_spread_limitation": (
            "For scanner-absent signals the exact historical bid/ask spread is unavailable. "
            "A replay candidate passed economics at the configured maximum allowed trade spread, "
            "but it is only a real missed candidate if the actual historical spread was <= that limit."
        ),
        "outcome_resolution": "15m conservative; TP+SL in same candle counts as stop-loss",
        "portfolio_warning": "Independent signal PnLs overlap and must not be treated as simultaneously executable portfolio PnL.",
    }

    return {"summary": summary, "candidates": candidates, "errors": errors}


def print_report(report: Dict[str, Any]) -> None:
    s = report["summary"]
    misses = [r for r in report["candidates"] if str(r["classification"]).startswith("COVERAGE_MISS")]
    selective = [r for r in misses if r["selective_now"]]
    winners = sorted(misses, key=lambda r: finite(r.get("net_pnl_eur")), reverse=True)
    losers = sorted(misses, key=lambda r: finite(r.get("net_pnl_eur")))

    print("", flush=True)
    print("=" * 96, flush=True)
    print(" RESULTAAT VOLLEDIGE REPLAY", flush=True)
    print("=" * 96, flush=True)
    print(f"Markten verwerkt       : {s['markets_processed']}/{s['markets_total']}", flush=True)
    print(f"Onvoldoende historie   : {s['markets_insufficient_history']}", flush=True)
    print(f"API/data fouten         : {s['market_errors']}", flush=True)
    print(f"Robuuste LONG kandidaten: {s['robust_long_candidates_if_spread_le_limit']}", flush=True)
    print(f"Coverage-misses         : {s['coverage_misses_if_historical_spread_ok']}", flush=True)
    print(f"  daarvan SELECTIVE     : {s['coverage_miss_selective']}", flush=True)
    print(f"  daarvan niet-SELECTIVE: {s['coverage_miss_nonselective']}", flush=True)
    print(f"Winners / losers        : {s['coverage_miss_winners']} / {s['coverage_miss_losers']}", flush=True)
    print(f"Losse-signaal som       : €{s['coverage_miss_independent_net_total_eur']:+.2f} (NIET portfolio-PnL)", flush=True)
    print(f"Strategieën misses      : {s['strategy_counts_misses']}", flush=True)

    print("\n=== GEMISTE SELECTIVE LONGS (belangrijkst) ===", flush=True)
    if not selective:
        print("Geen robuuste SELECTIVE coverage-misses gevonden.", flush=True)
    else:
        for row in sorted(selective, key=lambda r: finite(r.get("net_pnl_eur")), reverse=True)[:20]:
            local = parse_dt(row["scan_time_utc"]).astimezone(LOCAL_TZ)
            print(
                f"{local:%H:%M} {row['symbol']:<12} {row['strategy']:<16} "
                f"chg24={finite(row['historical_change_24h_pct']):>+6.2f}% "
                f"RR={finite(row['reward_risk_at_spread_010']):>4.2f} "
                f"{row['outcome']:<18} €{finite(row['net_pnl_eur']):>+6.2f}",
                flush=True,
            )

    print("\n=== TOP 15 GEMISTE WINNAARS (alle LONG routes) ===", flush=True)
    for row in winners[:15]:
        local = parse_dt(row["scan_time_utc"]).astimezone(LOCAL_TZ)
        print(
            f"{local:%H:%M} {row['symbol']:<12} {row['strategy']:<16} "
            f"sel={'JA' if row['selective_now'] else 'NEE':<3} "
            f"{row['outcome']:<18} €{finite(row['net_pnl_eur']):>+6.2f}",
            flush=True,
        )

    print("\n=== TOP 10 GEMISTE VERLIEZERS ===", flush=True)
    for row in losers[:10]:
        local = parse_dt(row["scan_time_utc"]).astimezone(LOCAL_TZ)
        print(
            f"{local:%H:%M} {row['symbol']:<12} {row['strategy']:<16} "
            f"sel={'JA' if row['selective_now'] else 'NEE':<3} "
            f"{row['outcome']:<18} €{finite(row['net_pnl_eur']):>+6.2f}",
            flush=True,
        )

    print("\nLET OP: historische spread van niet-opgeslagen signalen is onbekend.", flush=True)
    print("Een miss is daarom kandidaat ALS de werkelijke spread toen <= de limiet was.", flush=True)
    print("Orders/private API     : NEE", flush=True)
    print("Bot/config gewijzigd   : NEE", flush=True)


def self_test() -> None:
    dt = datetime(2026, 8, 24, 5, 7, 41, tzinfo=UTC)
    assert floor_15m(dt) == datetime(2026, 8, 24, 5, 0, tzinfo=UTC)

    rows = [
        [1000, 100, 101, 99, 100, 1],
        [901000, 100, 105, 99, 104, 1],
        [1801000, 104, 104, 95, 96, 1],
    ]
    frame = ohlcv_frame(rows)
    sim = simulate_long(frame, 901000, 100.0, 104.0, 97.0, 0.25)
    assert sim["outcome"] == "TAKE_PROFIT"
    assert sim["net_pnl_eur"] > 0

    ident = signal_identity("PROM/EUR", "momentum", "LONG", 123)
    assert ident == "PROM/EUR|momentum|LONG|123"
    print("DIAMOND_FULL_MARKET_OVERNIGHT_REPLAY_SELF_TEST_OK")
    print("Orders/private API: NEE")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--start", default=None, help="ISO datetime; default vorige dag 22:00 NL")
    parser.add_argument("--end", default=None, help="ISO datetime; default huidige kwartier")
    parser.add_argument("--json", default=str(DEFAULT_JSON))
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--progress-every", type=int, default=25)
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    start, end = parse_window(args.start, args.end)
    report = run_replay(start, end, max(1, args.progress_every))
    atomic_json(Path(args.json), report)
    write_csv(Path(args.csv), report["candidates"])
    print_report(report)
    print(f"\nJSON: {args.json}")
    print(f"CSV : {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
