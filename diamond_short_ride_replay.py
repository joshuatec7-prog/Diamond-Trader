#!/usr/bin/env python3
"""
Diamond Trader SHORT_RIDE_LONG Replay v1.0

Research-only historical replay for short upward rides on Bitvavo EUR spot.
No API keys, private endpoints, orders, live-state writes, or config changes.

Purpose:
- test whether short 1m/5m price accelerations can be captured earlier than the
  existing 15m SELECTIVE LONG route;
- use fixed, pre-declared FAST / BALANCED / STRICT variants;
- enter only on the NEXT 1m candle after a signal (no look-ahead);
- use conservative same-candle exit ordering;
- include 0.25% fee per side plus 0.10% assumed roundtrip spread;
- report independent trades AND a max-1-open-position portfolio view.

Important limitation:
Historical bid/ask spread is not available from OHLCV. The replay assumes
0.10% roundtrip spread for every trade. Results therefore remain research-only.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import ccxt
import pandas as pd

VERSION = "1.0"
UTC = timezone.utc
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
DATA = Path("/var/data")
DEFAULT_JSON = DATA / "diamond_short_ride_replay.json"

STAKE_EUR = 130.0
FEE_PER_SIDE_PCT = 0.25
ASSUMED_ROUNDTRIP_SPREAD_PCT = 0.10
STRESS_EXTRA_FRICTION_PCT = 0.10
LOOKBACK_MINUTES = 360
FETCH_LIMIT = 1000

EXCLUDED_BASES = {"EUR", "USDT", "USDC", "DAI", "TUSD", "FDUSD"}
LEVERAGED_SUFFIXES = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")


@dataclass(frozen=True)
class Variant:
    name: str
    ret5_min_pct: float
    ret15_min_pct: float
    ret5_max_pct: float
    volume_accel_min: float
    quote_volume_60m_min: float
    rsi_min: float
    rsi_max: float
    target_pct: float
    stop_pct: float
    trailing_trigger_pct: float
    trailing_pullback_pct: float
    max_hold_minutes: int
    cooldown_minutes: int = 15


VARIANTS = (
    Variant(
        name="FAST",
        ret5_min_pct=0.60,
        ret15_min_pct=0.80,
        ret5_max_pct=2.50,
        volume_accel_min=1.80,
        quote_volume_60m_min=10_000.0,
        rsi_min=55.0,
        rsi_max=78.0,
        target_pct=1.20,
        stop_pct=0.70,
        trailing_trigger_pct=0.80,
        trailing_pullback_pct=0.35,
        max_hold_minutes=20,
    ),
    Variant(
        name="BALANCED",
        ret5_min_pct=0.80,
        ret15_min_pct=1.00,
        ret5_max_pct=2.50,
        volume_accel_min=2.00,
        quote_volume_60m_min=10_000.0,
        rsi_min=58.0,
        rsi_max=76.0,
        target_pct=1.50,
        stop_pct=0.80,
        trailing_trigger_pct=1.00,
        trailing_pullback_pct=0.40,
        max_hold_minutes=30,
    ),
    Variant(
        name="STRICT",
        ret5_min_pct=1.00,
        ret15_min_pct=1.30,
        ret5_max_pct=2.40,
        volume_accel_min=2.50,
        quote_volume_60m_min=15_000.0,
        rsi_min=58.0,
        rsi_max=74.0,
        target_pct=1.80,
        stop_pct=0.80,
        trailing_trigger_pct=1.20,
        trailing_pullback_pct=0.45,
        max_hold_minutes=30,
    ),
)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def leveraged(base: str) -> bool:
    return any(base.endswith(suffix) for suffix in LEVERAGED_SUFFIXES)


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(UTC)


def default_window() -> Tuple[datetime, datetime]:
    now_local = datetime.now(LOCAL_TZ)
    end_local = floor_minute(now_local)
    start_local = (now_local - timedelta(days=1)).replace(
        hour=22, minute=0, second=0, microsecond=0
    )
    if start_local > end_local:
        start_local -= timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def public_exchange() -> ccxt.Exchange:
    exchange = ccxt.bitvavo({
        "enableRateLimit": True,
        "timeout": 30_000,
        "options": {"fetchMarkets": {"types": ["spot"]}},
    })
    exchange.load_markets()
    return exchange


def active_eur_symbols(exchange: ccxt.Exchange) -> List[str]:
    out: List[str] = []
    for symbol, market in exchange.markets.items():
        if not isinstance(market, dict):
            continue
        if str(market.get("quote") or "").upper() != "EUR":
            continue
        if market.get("spot") is False or market.get("active") is False:
            continue
        base = str(market.get("base") or "").upper()
        if not base or base in EXCLUDED_BASES or leveraged(base):
            continue
        out.append(symbol)
    return sorted(set(out))


def frame_from_ohlcv(rows: List[List[Any]]) -> pd.DataFrame:
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    if df.empty:
        return df
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(inplace=True)
    df.sort_values("timestamp", inplace=True)
    df.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
    df["timestamp"] = df["timestamp"].astype("int64")
    df.reset_index(drop=True, inplace=True)
    return df


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    diff = series.diff()
    gain = diff.clip(lower=0.0)
    loss = -diff.clip(upper=0.0)
    avg_gain = gain.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()
    avg_loss = loss.ewm(
        alpha=1 / length,
        adjust=False,
        min_periods=length,
    ).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    data["quote_value"] = data["volume"] * data["close"]
    data["ret3_pct"] = (data["close"] / data["close"].shift(3) - 1.0) * 100.0
    data["ret5_pct"] = (data["close"] / data["close"].shift(5) - 1.0) * 100.0
    data["ret15_pct"] = (data["close"] / data["close"].shift(15) - 1.0) * 100.0
    data["qv5"] = data["quote_value"].rolling(5).sum()
    previous_30 = data["quote_value"].shift(5).rolling(30).sum()
    data["baseline_qv5"] = previous_30 / 6.0
    data["volume_accel"] = data["qv5"] / data["baseline_qv5"].replace(0.0, float("nan"))
    data["qv60"] = data["quote_value"].rolling(60).sum()
    data["ema20"] = data["close"].ewm(span=20, adjust=False).mean()
    data["ema60"] = data["close"].ewm(span=60, adjust=False).mean()
    data["ema20_prev5"] = data["ema20"].shift(5)
    data["rsi14"] = rsi(data["close"], 14)
    return data


def signal_ok(row: pd.Series, variant: Variant) -> bool:
    ret5 = finite(row.get("ret5_pct"), -999.0)
    ret15 = finite(row.get("ret15_pct"), -999.0)
    accel = finite(row.get("volume_accel"), 0.0)
    qv60 = finite(row.get("qv60"), 0.0)
    rsi14 = finite(row.get("rsi14"), 50.0)
    close = finite(row.get("close"), 0.0)
    ema20 = finite(row.get("ema20"), 0.0)
    ema60 = finite(row.get("ema60"), 0.0)
    ema20_prev5 = finite(row.get("ema20_prev5"), 0.0)

    return (
        variant.ret5_min_pct <= ret5 <= variant.ret5_max_pct
        and ret15 >= variant.ret15_min_pct
        and accel >= variant.volume_accel_min
        and qv60 >= variant.quote_volume_60m_min
        and variant.rsi_min <= rsi14 <= variant.rsi_max
        and close > ema20 > ema60
        and ema20 > ema20_prev5 > 0.0
    )


def quality_score(row: pd.Series, variant: Variant) -> float:
    ret5 = finite(row.get("ret5_pct"), 0.0)
    ret15 = finite(row.get("ret15_pct"), 0.0)
    accel = finite(row.get("volume_accel"), 0.0)
    qv60 = finite(row.get("qv60"), 0.0)
    return round(
        (ret5 / max(variant.ret5_min_pct, 0.01)) * 20.0
        + (ret15 / max(variant.ret15_min_pct, 0.01)) * 15.0
        + min(accel, 6.0) * 8.0
        + min(math.log10(max(qv60, 1.0)), 7.0) * 4.0,
        3,
    )


def trade_net_eur(entry: float, exit_price: float, extra_friction_pct: float = 0.0) -> float:
    if entry <= 0 or exit_price <= 0:
        return 0.0
    amount = STAKE_EUR / entry
    gross_exit = amount * exit_price
    buy_fee = STAKE_EUR * FEE_PER_SIDE_PCT / 100.0
    sell_fee = gross_exit * FEE_PER_SIDE_PCT / 100.0
    spread_cost = STAKE_EUR * ASSUMED_ROUNDTRIP_SPREAD_PCT / 100.0
    extra_cost = STAKE_EUR * extra_friction_pct / 100.0
    return gross_exit - STAKE_EUR - buy_fee - sell_fee - spread_cost - extra_cost


def simulate_trade(data: pd.DataFrame, signal_idx: int, variant: Variant, symbol: str) -> Optional[Dict[str, Any]]:
    entry_idx = signal_idx + 1
    if entry_idx >= len(data):
        return None

    entry_row = data.iloc[entry_idx]
    entry = finite(entry_row["open"], 0.0)
    if entry <= 0:
        return None

    signal_row = data.iloc[signal_idx]
    signal_ms = int(signal_row["timestamp"] + 60_000)
    entry_ms = int(entry_row["timestamp"])
    hard_stop = entry * (1.0 - variant.stop_pct / 100.0)
    target = entry * (1.0 + variant.target_pct / 100.0)
    peak = entry
    outcome = "NO_EXIT"
    exit_price = entry
    exit_ms = entry_ms
    hold_minutes = 0
    max_end_idx = min(len(data) - 1, entry_idx + variant.max_hold_minutes - 1)

    for idx in range(entry_idx, max_end_idx + 1):
        candle = data.iloc[idx]
        high = finite(candle["high"], entry)
        low = finite(candle["low"], entry)
        close = finite(candle["close"], entry)
        peak = max(peak, high)
        active_stop = hard_stop
        peak_gain_pct = (peak / entry - 1.0) * 100.0
        trailing_active = peak_gain_pct >= variant.trailing_trigger_pct
        if trailing_active:
            trail = peak * (1.0 - variant.trailing_pullback_pct / 100.0)
            active_stop = max(active_stop, trail)

        hit_stop = low <= active_stop
        hit_target = high >= target
        if hit_stop:
            outcome = "TRAILING_STOP" if active_stop > hard_stop else "STOP_LOSS"
            exit_price = active_stop
            exit_ms = int(candle["timestamp"] + 60_000)
            hold_minutes = idx - entry_idx + 1
            break
        if hit_target:
            outcome = "TAKE_PROFIT"
            exit_price = target
            exit_ms = int(candle["timestamp"] + 60_000)
            hold_minutes = idx - entry_idx + 1
            break
        if idx == max_end_idx:
            outcome = "TIME_EXIT"
            exit_price = close
            exit_ms = int(candle["timestamp"] + 60_000)
            hold_minutes = idx - entry_idx + 1

    net = trade_net_eur(entry, exit_price, 0.0)
    stress_net = trade_net_eur(entry, exit_price, STRESS_EXTRA_FRICTION_PCT)
    gross_move_pct = (exit_price / entry - 1.0) * 100.0
    return {
        "variant": variant.name,
        "symbol": symbol,
        "signal_time_utc": datetime.fromtimestamp(signal_ms / 1000, tz=UTC).isoformat(),
        "entry_time_utc": datetime.fromtimestamp(entry_ms / 1000, tz=UTC).isoformat(),
        "exit_time_utc": datetime.fromtimestamp(exit_ms / 1000, tz=UTC).isoformat(),
        "signal_ms": signal_ms,
        "entry_ms": entry_ms,
        "exit_ms": exit_ms,
        "entry": entry,
        "exit": exit_price,
        "outcome": outcome,
        "hold_minutes": hold_minutes,
        "gross_move_pct": round(gross_move_pct, 4),
        "net_pnl_eur": round(net, 4),
        "stress_net_pnl_eur": round(stress_net, 4),
        "ret3_pct": round(finite(signal_row.get("ret3_pct")), 4),
        "ret5_pct": round(finite(signal_row.get("ret5_pct")), 4),
        "ret15_pct": round(finite(signal_row.get("ret15_pct")), 4),
        "volume_accel": round(finite(signal_row.get("volume_accel")), 3),
        "quote_volume_60m": round(finite(signal_row.get("qv60")), 2),
        "rsi14": round(finite(signal_row.get("rsi14")), 2),
        "quality_score": quality_score(signal_row, variant),
    }


def candidate_trades(df: pd.DataFrame, variant: Variant, symbol: str, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    data = add_features(df)
    trades: List[Dict[str, Any]] = []
    next_allowed_signal_ms = start_ms
    for idx in range(65, len(data) - 1):
        row = data.iloc[idx]
        signal_ms = int(row["timestamp"] + 60_000)
        if signal_ms < start_ms or signal_ms > end_ms:
            continue
        if signal_ms < next_allowed_signal_ms:
            continue
        if not signal_ok(row, variant):
            continue
        trade = simulate_trade(data, idx, variant, symbol)
        if not trade:
            continue
        trades.append(trade)
        next_allowed_signal_ms = int(trade["exit_ms"]) + variant.cooldown_minutes * 60_000
    return trades


def profit_factor(trades: Iterable[Dict[str, Any]], key: str = "net_pnl_eur") -> float:
    vals = [finite(t.get(key)) for t in trades]
    gains = sum(x for x in vals if x > 0)
    losses = -sum(x for x in vals if x < 0)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def summarize(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    vals = [finite(t["net_pnl_eur"]) for t in trades]
    stress_vals = [finite(t["stress_net_pnl_eur"]) for t in trades]
    wins = sum(v > 0 for v in vals)
    losses = sum(v < 0 for v in vals)
    breakeven = len(vals) - wins - losses
    holds = [int(t["hold_minutes"]) for t in trades]
    outcomes: Dict[str, int] = {}
    for t in trades:
        outcomes[t["outcome"]] = outcomes.get(t["outcome"], 0) + 1
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "winrate_pct": round(wins / len(trades) * 100.0, 2) if trades else 0.0,
        "net_pnl_eur": round(sum(vals), 4),
        "stress_net_pnl_eur": round(sum(stress_vals), 4),
        "profit_factor": profit_factor(trades),
        "avg_pnl_eur": round(statistics.mean(vals), 4) if vals else 0.0,
        "median_hold_minutes": round(statistics.median(holds), 1) if holds else 0.0,
        "outcomes": outcomes,
    }


def max_one_portfolio(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(
        trades,
        key=lambda t: (int(t["signal_ms"]), -finite(t.get("quality_score")), str(t.get("symbol"))),
    )
    chosen: List[Dict[str, Any]] = []
    busy_until = -1
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for trade in ordered:
        grouped.setdefault(int(trade["signal_ms"]), []).append(trade)
    for signal_ms in sorted(grouped):
        if signal_ms < busy_until:
            continue
        options = sorted(grouped[signal_ms], key=lambda t: (-finite(t.get("quality_score")), str(t.get("symbol"))))
        trade = options[0]
        chosen.append(trade)
        busy_until = int(trade["exit_ms"])
    return chosen


def fmt_pf(value: float) -> str:
    return "INF" if math.isinf(value) else f"{value:.2f}"


def parse_window(args: argparse.Namespace) -> Tuple[datetime, datetime]:
    default_start, default_end = default_window()
    start = parse_datetime(args.start) or default_start
    end = parse_datetime(args.end) or default_end
    if end <= start:
        raise ValueError("end moet na start liggen")
    if (end - start) > timedelta(hours=12):
        raise ValueError("maximale replay-window is 12 uur")
    return start, end


def run(start: datetime, end: datetime, progress_every: int) -> Dict[str, Any]:
    exchange = public_exchange()
    symbols = active_eur_symbols(exchange)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    since_ms = int((start - timedelta(minutes=LOOKBACK_MINUTES)).timestamp() * 1000)
    all_trades: Dict[str, List[Dict[str, Any]]] = {v.name: [] for v in VARIANTS}
    processed = 0
    insufficient = 0
    errors: List[Dict[str, str]] = []

    print("=" * 100, flush=True)
    print(f" DIAMOND SHORT_RIDE_LONG REPLAY v{VERSION}", flush=True)
    print("=" * 100, flush=True)
    print(
        f"Periode NL          : {start.astimezone(LOCAL_TZ):%d-%m %H:%M} -> {end.astimezone(LOCAL_TZ):%d-%m %H:%M}",
        flush=True,
    )
    print(f"Actieve EUR-markten: {len(symbols)}", flush=True)
    print("Kostenmodel         : 0.25% BUY fee + 0.25% SELL fee + 0.10% aangenomen spread", flush=True)
    print("Entry               : volgende 1m candle na signaal", flush=True)
    print("Orders/private API  : NEE", flush=True)
    print("", flush=True)

    for n, symbol in enumerate(symbols, 1):
        try:
            rows = exchange.fetch_ohlcv(symbol, timeframe="1m", since=since_ms, limit=FETCH_LIMIT)
            df = frame_from_ohlcv(rows)
            if len(df) < 120:
                insufficient += 1
                continue
            first_needed = start_ms - LOOKBACK_MINUTES * 60_000
            available_start = int(df.iloc[0]["timestamp"])
            available_end = int(df.iloc[-1]["timestamp"] + 60_000)
            if available_start > first_needed + 30 * 60_000 or available_end < start_ms:
                insufficient += 1
                continue
            for variant in VARIANTS:
                all_trades[variant.name].extend(candidate_trades(df, variant, symbol, start_ms, end_ms))
            processed += 1
        except Exception as exc:
            errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})

        if n % max(1, progress_every) == 0 or n == len(symbols):
            counts = " ".join(f"{v.name}={len(all_trades[v.name])}" for v in VARIANTS)
            print(
                f"Voortgang {n:>3}/{len(symbols)} | verwerkt={processed} onvoldoende={insufficient} fouten={len(errors)} | {counts}",
                flush=True,
            )

    variants_report: Dict[str, Any] = {}
    for variant in VARIANTS:
        trades = all_trades[variant.name]
        portfolio = max_one_portfolio(trades)
        variants_report[variant.name] = {
            "parameters": asdict(variant),
            "independent": summarize(trades),
            "max_one_portfolio": summarize(portfolio),
            "trades": trades,
            "portfolio_trades": portfolio,
        }

    return {
        "summary": {
            "version": VERSION,
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
            "stake_eur": STAKE_EUR,
            "fee_per_side_pct": FEE_PER_SIDE_PCT,
            "assumed_roundtrip_spread_pct": ASSUMED_ROUNDTRIP_SPREAD_PCT,
            "stress_extra_friction_pct": STRESS_EXTRA_FRICTION_PCT,
            "historical_spread_limitation": "Exact historical bid/ask spread is unavailable from OHLCV. The replay assumes 0.10% roundtrip spread for every trade.",
            "lookahead_control": "Signal on closed 1m candle; entry at next 1m open.",
            "same_candle_policy": "Protective stop/trailing wins over target on ambiguity.",
        },
        "variants": variants_report,
        "errors": errors,
    }


def print_report(report: Dict[str, Any]) -> None:
    s = report["summary"]
    print("\n" + "=" * 100, flush=True)
    print(" RESULTAAT SHORT_RIDE_LONG REPLAY", flush=True)
    print("=" * 100, flush=True)
    print(
        f"Markten verwerkt    : {s['markets_processed']}/{s['markets_total']} | onvoldoende={s['markets_insufficient_history']} | fouten={s['market_errors']}",
        flush=True,
    )
    print("Let op              : historische spread onbekend; overal 0.10% aangenomen", flush=True)

    print("\n=== VARIANTEN | LOSSE SIGNALEN ===", flush=True)
    for name, row in report["variants"].items():
        x = row["independent"]
        print(
            f"{name:<9} n={x['trades']:>3} W/L/BE={x['wins']}/{x['losses']}/{x['breakeven']} WR={x['winrate_pct']:>5.1f}% PnL=€{x['net_pnl_eur']:+7.2f} PF={fmt_pf(x['profit_factor'])} stress=€{x['stress_net_pnl_eur']:+7.2f} hold={x['median_hold_minutes']:>4.1f}m",
            flush=True,
        )

    print("\n=== VARIANTEN | MAX 1 POSITIE TEGELIJK ===", flush=True)
    for name, row in report["variants"].items():
        x = row["max_one_portfolio"]
        print(
            f"{name:<9} n={x['trades']:>3} W/L/BE={x['wins']}/{x['losses']}/{x['breakeven']} WR={x['winrate_pct']:>5.1f}% PnL=€{x['net_pnl_eur']:+7.2f} PF={fmt_pf(x['profit_factor'])} stress=€{x['stress_net_pnl_eur']:+7.2f}",
            flush=True,
        )

    all_portfolio: List[Dict[str, Any]] = []
    for name, row in report["variants"].items():
        for trade in row["portfolio_trades"]:
            copy = dict(trade)
            copy["_variant"] = name
            all_portfolio.append(copy)

    winners = sorted(all_portfolio, key=lambda t: finite(t["net_pnl_eur"]), reverse=True)
    losers = sorted(all_portfolio, key=lambda t: finite(t["net_pnl_eur"]))

    print("\n=== BESTE KORTE RITTEN (MAX1-SELECTIES) ===", flush=True)
    for t in winners[:12]:
        local = datetime.fromisoformat(t["signal_time_utc"]).astimezone(LOCAL_TZ)
        print(
            f"{local:%H:%M} {t['symbol']:<12} {t['_variant']:<9} 5m={finite(t['ret5_pct']):>+5.2f}% volx={finite(t['volume_accel']):>4.1f} {t['outcome']:<13} {t['hold_minutes']:>2}m €{finite(t['net_pnl_eur']):>+6.2f}",
            flush=True,
        )

    print("\n=== SLECHTSTE KORTE RITTEN (MAX1-SELECTIES) ===", flush=True)
    for t in losers[:10]:
        local = datetime.fromisoformat(t["signal_time_utc"]).astimezone(LOCAL_TZ)
        print(
            f"{local:%H:%M} {t['symbol']:<12} {t['_variant']:<9} 5m={finite(t['ret5_pct']):>+5.2f}% volx={finite(t['volume_accel']):>4.1f} {t['outcome']:<13} {t['hold_minutes']:>2}m €{finite(t['net_pnl_eur']):>+6.2f}",
            flush=True,
        )

    print("\nInterpretatiegrens:", flush=True)
    print("- interessant voor vervolg alleen als MAX1 PnL > 0, PF > 1.20 EN stress-PnL > 0;", flush=True)
    print("- één nacht is nooit voldoende voor LIVE-toelating; dit is alleen een eerste falsificatietest.", flush=True)
    print("AUTO LIVE 5 gewijzigd: NEE", flush=True)
    print("Orders/private API   : NEE", flush=True)


def self_test() -> None:
    start = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)
    rows = []
    price = 100.0
    for i in range(150):
        ts = int((start - timedelta(minutes=90) + timedelta(minutes=i)).timestamp() * 1000)
        if 100 <= i < 106:
            price *= 1.0018
            volume = 800.0
        elif 106 <= i < 112:
            price *= 1.0025
            volume = 900.0
        else:
            price *= 1.00005
            volume = 30.0
        rows.append([ts, price * 0.9998, price * 1.001, price * 0.999, price, volume])

    df = frame_from_ohlcv(rows)
    featured = add_features(df)
    assert len(featured) == 150
    assert trade_net_eur(100, 101.5) > 0
    variant = VARIANTS[0]
    found = False
    for idx in range(65, len(featured) - 1):
        if signal_ok(featured.iloc[idx], variant):
            found = True
            trade = simulate_trade(featured, idx, variant, "TEST/EUR")
            assert trade is not None
            break
    assert found, "synthetische FAST-kans niet gevonden"
    print("DIAMOND_SHORT_RIDE_REPLAY_SELF_TEST_OK")
    print("Orders/private API: NEE")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--start", default=None, help="ISO datetime; default vorige dag 22:00 NL")
    parser.add_argument("--end", default=None, help="ISO datetime; default huidige minuut")
    parser.add_argument("--json", default=str(DEFAULT_JSON))
    parser.add_argument("--progress-every", type=int, default=25)
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    start, end = parse_window(args)
    report = run(start, end, max(1, args.progress_every))
    output = Path(args.json)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(output)
    print_report(report)
    print(f"\nJSON: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
