#!/usr/bin/env python3
"""Read-only market opportunity research for Diamond Trader.

Compares LONG variants without changing scanner, SELECTIVE, config or LIVE:
- CURRENT
- SPREAD_015
- SPREAD_020
- LONG_MOMENTUM
- MOMENTUM_SPREAD_020

Method:
- source: /var/data/diamond_market_signals.csv
- window: last 7 days by default
- only LONG trend_breakout/momentum signals
- alternative variants may relax only the spread rejection; every other current
  scanner rejection must still pass
- 15m public Bitvavo candles are used to replay TP/SL
- same-candle TP+SL is conservatively counted as stop-loss
- entry and exit both reuse the signal spread assumption
- fees use current config, default 0.25% per side
- max hold uses current scanner setting, default 48 hours
- one simulated position per symbol per variant at a time

No private API, orders, state writes, config changes or LIVE changes.
"""

from __future__ import annotations

import argparse
import csv
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ccxt
import yaml

SIGNALS_FILE = Path("/var/data/diamond_market_signals.csv")
CONFIG_FILE = Path("/opt/render/project/src/config.yaml")
TIMEFRAME_MS = 15 * 60 * 1000
DEFAULT_DAYS = 7
DEFAULT_MAX_HOLD_MINUTES = 2880

VARIANTS = (
    "CURRENT",
    "SPREAD_015",
    "SPREAD_020",
    "LONG_MOMENTUM",
    "MOMENTUM_SPREAD_020",
)


def f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def b(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "ja", "on"}


def dt(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def settings() -> Dict[str, float]:
    result = {
        "stake": 130.0,
        "fee_pct": 0.25,
        "max_hold_minutes": float(DEFAULT_MAX_HOLD_MINUTES),
    }
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        risk = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
        fees = cfg.get("fees") if isinstance(cfg.get("fees"), dict) else {}
        scanner = cfg.get("market_scanner") if isinstance(cfg.get("market_scanner"), dict) else {}
        result["stake"] = max(5.0, f(scanner.get("stake_eur", risk.get("fixed_stake_quote", 130)), 130.0))
        result["fee_pct"] = max(0.0, f(scanner.get("fee_pct_per_side", fees.get("taker_fee_pct", 0.25)), 0.25))
        result["max_hold_minutes"] = float(max(60, int(f(scanner.get("max_hold_minutes", DEFAULT_MAX_HOLD_MINUTES), DEFAULT_MAX_HOLD_MINUTES))))
    except Exception:
        pass
    return result


def rejection_parts(row: Dict[str, str]) -> List[str]:
    raw = str(row.get("shadow_rejection_reasons") or "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split("|") if part.strip()]


def only_spread_rejected(row: Dict[str, str]) -> bool:
    parts = rejection_parts(row)
    return all(part.lower().startswith("spread ") for part in parts)


def other_filters_pass(row: Dict[str, str]) -> bool:
    return b(row.get("shadow_eligible")) or only_spread_rejected(row)


def candidate_key(row: Dict[str, str]) -> str:
    return "|".join([
        str(row.get("symbol") or "").upper(),
        str(row.get("strategy") or ""),
        str(row.get("side") or "").upper(),
        str(row.get("candle_timestamp") or ""),
    ])


def load_signals(days: int) -> List[Dict[str, str]]:
    if not SIGNALS_FILE.is_file():
        raise FileNotFoundError(SIGNALS_FILE)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    rows: List[Dict[str, str]] = []
    seen = set()

    with SIGNALS_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            detected = dt(raw.get("detected_at"))
            if detected is None or detected < cutoff:
                continue
            if str(raw.get("side") or "").upper() != "LONG":
                continue
            if str(raw.get("strategy") or "") not in {"trend_breakout", "momentum"}:
                continue
            if f(raw.get("entry_price")) <= 0 or f(raw.get("take_profit")) <= 0 or f(raw.get("stop_loss")) <= 0:
                continue
            key = candidate_key(raw)
            if not key or key in seen:
                continue
            seen.add(key)
            row = dict(raw)
            row["_detected"] = detected.isoformat()
            rows.append(row)

    rows.sort(key=lambda row: dt(row.get("detected_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


def accepts(name: str, row: Dict[str, str]) -> bool:
    strategy = str(row.get("strategy") or "")
    spread = f(row.get("spread_pct"), 999.0)

    if name == "CURRENT":
        return strategy == "trend_breakout" and b(row.get("shadow_eligible"))

    if name == "SPREAD_015":
        return strategy == "trend_breakout" and other_filters_pass(row) and spread <= 0.15 + 1e-12

    if name == "SPREAD_020":
        return strategy == "trend_breakout" and other_filters_pass(row) and spread <= 0.20 + 1e-12

    if name == "LONG_MOMENTUM":
        return strategy == "momentum" and other_filters_pass(row) and spread <= 0.10 + 1e-12

    if name == "MOMENTUM_SPREAD_020":
        return strategy == "momentum" and other_filters_pass(row) and spread <= 0.20 + 1e-12

    return False


def fetch_candles(exchange: ccxt.Exchange, symbol: str, since_ms: int, until_ms: int) -> List[List[Any]]:
    rows: List[List[Any]] = []
    cursor = max(0, since_ms)
    while cursor <= until_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe="15m", since=cursor, limit=500) or []
        if not batch:
            break
        for candle in batch:
            if not candle or len(candle) < 6:
                continue
            stamp = int(candle[0])
            if stamp > until_ms:
                break
            rows.append(candle)
        last_ms = int(batch[-1][0])
        next_cursor = last_ms + TIMEFRAME_MS
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 500:
            break
        time.sleep(max(0.0, float(getattr(exchange, "rateLimit", 0) or 0) / 1000.0))

    unique: Dict[int, List[Any]] = {}
    for candle in rows:
        unique[int(candle[0])] = candle
    return [unique[key] for key in sorted(unique)]


def make_position(row: Dict[str, str], cfg: Dict[str, float]) -> Optional[Dict[str, Any]]:
    raw_entry = f(row.get("entry_price"))
    raw_tp = f(row.get("take_profit"))
    raw_sl = f(row.get("stop_loss"))
    spread = max(0.0, f(row.get("spread_pct")))
    candle_dt = dt(row.get("candle_timestamp"))
    if raw_entry <= 0 or raw_tp <= raw_entry or raw_sl >= raw_entry or raw_sl <= 0 or candle_dt is None:
        return None

    half = spread / 200.0
    entry = raw_entry * (1.0 + half)
    tp = entry + (raw_tp - raw_entry)
    sl = entry - (raw_entry - raw_sl)
    stake = cfg["stake"]
    amount = stake / entry

    return {
        "symbol": str(row.get("symbol") or ""),
        "key": candidate_key(row),
        "detected_at": str(row.get("detected_at") or ""),
        "strategy": str(row.get("strategy") or ""),
        "regime": str(row.get("market_regime") or ""),
        "score": f(row.get("score")),
        "reward_risk": f(row.get("reward_risk")),
        "spread": spread,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "stake": stake,
        "amount": amount,
        "entry_ms": int(candle_dt.timestamp() * 1000),
    }


def close_trade(position: Dict[str, Any], raw_exit: float, reason: str, exit_ms: int, cfg: Dict[str, float]) -> Dict[str, Any]:
    half = position["spread"] / 200.0
    exit_price = raw_exit * (1.0 - half)
    gross = (exit_price - position["entry"]) * position["amount"]
    entry_fee = position["stake"] * cfg["fee_pct"] / 100.0
    exit_notional = exit_price * position["amount"]
    exit_fee = exit_notional * cfg["fee_pct"] / 100.0
    net = gross - entry_fee - exit_fee
    return {
        **position,
        "exit_price": exit_price,
        "exit_reason": reason,
        "exit_ms": exit_ms,
        "net_pnl": net,
        "fees": entry_fee + exit_fee,
        "duration_minutes": max(0.0, (exit_ms - position["entry_ms"]) / 60000.0),
    }


def evaluate(position: Dict[str, Any], candles: Iterable[List[Any]], cfg: Dict[str, float], now_ms: int) -> Optional[Dict[str, Any]]:
    start_ms = position["entry_ms"] + TIMEFRAME_MS
    max_hold_ms = int(cfg["max_hold_minutes"] * 60000)
    maturity_ms = position["entry_ms"] + max_hold_ms
    last_close: Optional[Tuple[int, float]] = None

    for candle in candles:
        stamp = int(candle[0])
        if stamp < start_ms:
            continue
        if stamp > maturity_ms:
            break
        high = f(candle[2])
        low = f(candle[3])
        close = f(candle[4])
        if close > 0:
            last_close = (stamp + TIMEFRAME_MS, close)

        hit_sl = low > 0 and low <= position["sl"]
        hit_tp = high > 0 and high >= position["tp"]
        if hit_sl:
            return close_trade(position, position["sl"], "stop_loss", stamp + TIMEFRAME_MS, cfg)
        if hit_tp:
            return close_trade(position, position["tp"], "take_profit", stamp + TIMEFRAME_MS, cfg)

    if now_ms < maturity_ms:
        return None
    if last_close is None:
        return None
    return close_trade(position, last_close[1], "time_exit", last_close[0], cfg)


def profit_factor(trades: List[Dict[str, Any]]) -> Optional[float]:
    gp = sum(max(0.0, trade["net_pnl"]) for trade in trades)
    gl = abs(sum(min(0.0, trade["net_pnl"]) for trade in trades))
    if gl > 0:
        return gp / gl
    if gp > 0:
        return math.inf
    return None


def fmt_pf(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "INF"
    return f"{value:.3f}"


def summarize(name: str, accepted: int, overlap_skips: int, pending: int, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    wins = sum(trade["net_pnl"] > 0 for trade in trades)
    losses = sum(trade["net_pnl"] < 0 for trade in trades)
    pnl = sum(trade["net_pnl"] for trade in trades)
    return {
        "name": name,
        "accepted": accepted,
        "overlap_skips": overlap_skips,
        "pending": pending,
        "closed": len(trades),
        "wins": wins,
        "losses": losses,
        "pnl": pnl,
        "pf": profit_factor(trades),
        "avg": pnl / len(trades) if trades else 0.0,
        "fees": sum(trade["fees"] for trade in trades),
        "tp": sum(trade["exit_reason"] == "take_profit" for trade in trades),
        "sl": sum(trade["exit_reason"] == "stop_loss" for trade in trades),
        "time": sum(trade["exit_reason"] == "time_exit" for trade in trades),
    }


def run(days: int) -> None:
    cfg = settings()
    signals = load_signals(days)
    exchange = ccxt.bitvavo({"enableRateLimit": True})
    exchange.load_markets()
    now_ms = int(exchange.milliseconds())

    selected_any = [row for row in signals if any(accepts(name, row) for name in VARIANTS)]
    symbols = sorted({str(row.get("symbol") or "") for row in selected_any if str(row.get("symbol") or "")})

    if selected_any:
        earliest = min(dt(row.get("candle_timestamp")) for row in selected_any if dt(row.get("candle_timestamp")) is not None)
        since_ms = int(earliest.timestamp() * 1000) - TIMEFRAME_MS
    else:
        since_ms = now_ms - days * 24 * 60 * 60 * 1000

    candles: Dict[str, List[List[Any]]] = {}
    errors: Dict[str, str] = {}
    for index, symbol in enumerate(symbols, start=1):
        try:
            candles[symbol] = fetch_candles(exchange, symbol, since_ms, now_ms)
        except Exception as exc:
            errors[symbol] = f"{type(exc).__name__}: {exc}"
        if index < len(symbols):
            time.sleep(max(0.0, float(getattr(exchange, "rateLimit", 0) or 0) / 1000.0))

    print("=" * 104)
    print(" DIAMOND MARKET OPPORTUNITY RESEARCH | SPREAD + LONG MOMENTUM")
    print("=" * 104)
    print(f"Periode             : laatste {days} dagen")
    print(f"LONG signalen bron  : {len(signals)}")
    print(f"Onderzochte markten : {len(symbols)}")
    print(f"Stake               : €{cfg['stake']:.2f}")
    print(f"Fee per kant        : {cfg['fee_pct']:.3f}%")
    print(f"Max hold            : {cfg['max_hold_minutes'] / 60:.1f} uur")
    print("Spread bij entry én exit conservatief hergebruikt.")

    summaries = []
    trade_sets: Dict[str, List[Dict[str, Any]]] = {}

    for name in VARIANTS:
        accepted = 0
        overlap_skips = 0
        pending = 0
        trades: List[Dict[str, Any]] = []
        open_until: Dict[str, int] = defaultdict(int)

        for row in signals:
            if not accepts(name, row):
                continue
            accepted += 1
            position = make_position(row, cfg)
            if position is None:
                continue
            symbol = position["symbol"]
            if symbol in errors:
                continue
            if position["entry_ms"] < open_until[symbol]:
                overlap_skips += 1
                continue
            result = evaluate(position, candles.get(symbol, []), cfg, now_ms)
            if result is None:
                pending += 1
                continue
            trades.append(result)
            open_until[symbol] = int(result["exit_ms"])

        trade_sets[name] = trades
        summaries.append(summarize(name, accepted, overlap_skips, pending, trades))

    print("\n=== RESULTAAT ===")
    for row in summaries:
        wr = (row["wins"] / row["closed"] * 100.0) if row["closed"] else 0.0
        print(
            f"{row['name']:<22} accepted={row['accepted']:>3} closed={row['closed']:>3} "
            f"pending={row['pending']:>2} overlap={row['overlap_skips']:>2} | "
            f"W/L={row['wins']}/{row['losses']} WR={wr:>5.1f}% | "
            f"PnL=€{row['pnl']:+8.3f} PF={fmt_pf(row['pf']):>6} AVG=€{row['avg']:+.3f}"
        )
        print(
            f"{'':22} TP/SL/TIME={row['tp']}/{row['sl']}/{row['time']} | fees=€{row['fees']:.3f}"
        )

    current = next((row for row in summaries if row["name"] == "CURRENT"), None)
    if current:
        print("\n=== DELTA T.O.V. CURRENT ===")
        for row in summaries:
            if row["name"] == "CURRENT":
                continue
            print(
                f"{row['name']:<22} extra closed={row['closed'] - current['closed']:+d} | "
                f"delta PnL=€{row['pnl'] - current['pnl']:+.3f} | "
                f"PF {fmt_pf(current['pf'])} -> {fmt_pf(row['pf'])}"
            )

    print("\n=== LAATSTE AFGERONDE TRADES PER KANDIDAAT ===")
    for name in VARIANTS:
        rows = trade_sets[name][-5:]
        print(f"\n{name}:")
        if not rows:
            print("  geen afgeronde trades")
            continue
        for trade in rows:
            print(
                f"  {trade['symbol']:<12} {trade['strategy']:<15} "
                f"spread={trade['spread']:.3f}% RR={trade['reward_risk']:.3f} "
                f"{trade['exit_reason']:<11} PnL=€{trade['net_pnl']:+.3f}"
            )

    if errors:
        print("\n=== API-FOUTEN ===")
        for symbol, error in sorted(errors.items()):
            print(f"{symbol}: {error}")

    print("\n=== VEILIGHEID ===")
    print("Orders/private API : NEE")
    print("Config/strategie    : ONGEWIJZIGD")
    print("LIVE                : ONGEWIJZIGD")
    print("Dit is retrospectief onderzoek; geen variant wordt automatisch geactiveerd.")


def self_test() -> None:
    cfg = {"stake": 130.0, "fee_pct": 0.25, "max_hold_minutes": 2880.0}
    base = {
        "symbol": "TEST/EUR",
        "strategy": "trend_breakout",
        "side": "LONG",
        "market_regime": "BULLISH",
        "score": "95",
        "entry_price": "100",
        "take_profit": "102",
        "stop_loss": "99",
        "spread_pct": "0.12",
        "reward_risk": "1.5",
        "shadow_eligible": "false",
        "shadow_rejection_reasons": "spread 0.1200% hoger dan 0.1000%",
        "candle_timestamp": "2026-08-20T00:00:00+00:00",
        "detected_at": "2026-08-20T00:15:01+00:00",
    }
    assert not accepts("CURRENT", base)
    assert accepts("SPREAD_015", base)
    assert accepts("SPREAD_020", base)
    pos = make_position(base, cfg)
    assert pos is not None and pos["entry"] > 100
    candles = [[int(datetime(2026, 8, 20, 0, 15, tzinfo=timezone.utc).timestamp() * 1000), 100, 103, 100, 102, 1]]
    result = evaluate(pos, candles, cfg, int(datetime(2026, 8, 23, tzinfo=timezone.utc).timestamp() * 1000))
    assert result is not None and result["exit_reason"] == "take_profit"

    momentum = dict(base)
    momentum["strategy"] = "momentum"
    momentum["spread_pct"] = "0.08"
    momentum["shadow_eligible"] = "true"
    momentum["shadow_rejection_reasons"] = ""
    assert accepts("LONG_MOMENTUM", momentum)
    assert accepts("MOMENTUM_SPREAD_020", momentum)
    print("DIAMOND_MARKET_OPPORTUNITY_RESEARCH_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    run(max(1, min(30, args.days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
