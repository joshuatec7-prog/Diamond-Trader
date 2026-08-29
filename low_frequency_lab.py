from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

from adaptive_ls_main import adaptive_v2_db_path
from bitvavo_public import BitvavoPublic
from config import Settings
from models import Candle

LOOKBACK_DAYS = 180
INTERVAL = '4h'
MAX_HOLD_BARS = 12  # 48 uur
COOLDOWN_BARS = 6   # 24 uur per markt/variant na een entry
TARGET_STOP = {
    'TREND_BREAKOUT_LONG': (5.0, 3.0),
    'TREND_PULLBACK_LONG': (4.0, 3.0),
    'TREND_BREAKDOWN_SHORT': (5.0, 3.0),
    'SIDEWAYS_REENTRY_LONG': (3.0, 3.0),
    'SIDEWAYS_REENTRY_SHORT': (3.0, 3.0),
}


@dataclass(frozen=True)
class Trade:
    market: str
    variant: str
    side: str
    entry_ts: int
    exit_ts: int
    gross_pct: float
    net_pct: float
    reason: str


def _readonly(path: str) -> sqlite3.Connection:
    absolute = Path(path).resolve().as_posix()
    conn = sqlite3.connect(f'file:{absolute}?mode=ro', uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return float('nan')
    alpha = 2.0 / (period + 1.0)
    value = fmean(values[:period])
    for x in values[period:]:
        value = alpha * x + (1.0 - alpha) * value
    return value


def _atr(rows: list[Candle], period: int = 14) -> float:
    if len(rows) < period + 1:
        return float('nan')
    trs: list[float] = []
    for prev, cur in zip(rows[-period-1:-1], rows[-period:]):
        trs.append(max(cur.high-cur.low, abs(cur.high-prev.close), abs(cur.low-prev.close)))
    return fmean(trs)


def _bands(closes: list[float], window: int = 20) -> tuple[float, float, float]:
    sample = closes[-window:]
    if len(sample) < window:
        return float('nan'), float('nan'), float('nan')
    mid = fmean(sample)
    var = fmean([(x-mid)**2 for x in sample])
    sigma = math.sqrt(max(0.0, var))
    return mid-2*sigma, mid, mid+2*sigma


def _signals(rows: list[Candle], i: int) -> list[tuple[str, str]]:
    if i < 60:
        return []
    hist = rows[:i+1]
    closes = [c.close for c in hist]
    c = rows[i]
    p = rows[i-1]
    ema20 = _ema(closes[-80:], 20)
    ema50 = _ema(closes[-100:], 50)
    atr = _atr(hist, 14)
    if not all(math.isfinite(x) and x > 0 for x in (ema20, ema50, atr, c.close)):
        return []
    atr_pct = atr / c.close * 100.0
    if not (0.8 <= atr_pct <= 10.0):
        return []

    out: list[tuple[str, str]] = []
    prior_high = max(x.high for x in rows[i-20:i])
    prior_low = min(x.low for x in rows[i-20:i])
    trend_gap = abs(ema20 / ema50 - 1.0) * 100.0

    if ema20 > ema50 * 1.005 and c.close > prior_high and c.close > c.open:
        out.append(('TREND_BREAKOUT_LONG', 'LONG'))

    if ema20 > ema50 * 1.005 and p.low <= ema20 * 1.01 and p.close <= ema20 * 1.01 and c.close > ema20 and c.close > p.close:
        out.append(('TREND_PULLBACK_LONG', 'LONG'))

    if ema20 < ema50 * 0.995 and c.close < prior_low and c.close < c.open:
        out.append(('TREND_BREAKDOWN_SHORT', 'SHORT'))

    if trend_gap <= 1.5:
        prev_closes = closes[:-1]
        prev_lower, _, prev_upper = _bands(prev_closes, 20)
        lower, mid, upper = _bands(closes, 20)
        if all(math.isfinite(x) for x in (prev_lower, prev_upper, lower, mid, upper)):
            if p.close < prev_lower and c.close >= lower and c.close < mid and c.close > p.close:
                out.append(('SIDEWAYS_REENTRY_LONG', 'LONG'))
            if p.close > prev_upper and c.close <= upper and c.close > mid and c.close < p.close:
                out.append(('SIDEWAYS_REENTRY_SHORT', 'SHORT'))
    return out


def _simulate(rows: list[Candle], i: int, variant: str, side: str, cost: float) -> Trade | None:
    if i + 1 >= len(rows):
        return None
    target_pct, stop_pct = TARGET_STOP[variant]
    entry = rows[i].close
    if entry <= 0:
        return None
    if side == 'LONG':
        target = entry * (1 + target_pct/100)
        stop = entry * (1 - stop_pct/100)
    else:
        target = entry * (1 - target_pct/100)
        stop = entry * (1 + stop_pct/100)

    end = min(len(rows)-1, i + MAX_HOLD_BARS)
    for j in range(i+1, end+1):
        c = rows[j]
        if side == 'LONG':
            hit_stop = c.low <= stop
            hit_target = c.high >= target
        else:
            hit_stop = c.high >= stop
            hit_target = c.low <= target
        if hit_stop:
            gross = -stop_pct
            return Trade('', variant, side, rows[i].timestamp_ms, c.timestamp_ms, gross, gross-cost, 'STOP')
        if hit_target:
            gross = target_pct
            return Trade('', variant, side, rows[i].timestamp_ms, c.timestamp_ms, gross, gross-cost, 'TARGET')

    exit_close = rows[end].close
    gross = (exit_close/entry - 1)*100 if side == 'LONG' else (entry/exit_close - 1)*100
    return Trade('', variant, side, rows[i].timestamp_ms, rows[end].timestamp_ms, gross, gross-cost, 'TIME')


def _fetch_history(api: BitvavoPublic, market: str, start_ms: int, end_ms: int) -> list[Candle]:
    # Bitvavo maximaal 1440 candles per call. 4h over 180 dagen past ruim in één call.
    payload = api._get(f'/{market}/candles', {'interval': INTERVAL, 'limit': 1440, 'start': start_ms, 'end': end_ms})
    parsed: dict[int, Candle] = {}
    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            try:
                c = Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
            except (TypeError, ValueError, OverflowError):
                continue
            if c.is_valid:
                parsed[c.timestamp_ms] = c
    return [parsed[k] for k in sorted(parsed)]


def _pf(values: list[float]) -> float:
    wins = sum(x for x in values if x > 0)
    losses = -sum(x for x in values if x < 0)
    if losses <= 1e-12:
        return float('inf') if wins > 0 else 0.0
    return wins / losses


def main() -> int:
    s = Settings(); s.validate()
    db_path = adaptive_v2_db_path(s.db_path)
    conn = _readonly(db_path)
    try:
        row = conn.execute("SELECT value FROM state WHERE key='universe_json'").fetchone()
        if row is None:
            raise RuntimeError('universe ontbreekt')
        markets = [str(x) for x in json.loads(str(row['value']))]
    finally:
        conn.close()

    end_ms = int(time.time()*1000)
    start_ms = end_ms - LOOKBACK_DAYS*24*60*60*1000
    api = BitvavoPublic(s.api_base_url, s.request_timeout_seconds, s.request_retries)
    cost = 2*s.taker_fee_pct + 2*s.slippage_pct + s.backtest_assumed_spread_pct

    data: dict[str, list[Candle]] = {}
    for market in markets:
        try:
            rows = _fetch_history(api, market, start_ms, end_ms)
            if len(rows) >= 80:
                data[market] = rows
        except Exception as exc:
            print(f'[WARN] {market}: {type(exc).__name__}: {exc}')

    trades_by_variant: dict[str, list[Trade]] = defaultdict(list)
    for market, rows in data.items():
        last_entry_index: dict[str, int] = {}
        for i in range(60, len(rows)-1):
            for variant, side in _signals(rows, i):
                if i - last_entry_index.get(variant, -10_000) < COOLDOWN_BARS:
                    continue
                t = _simulate(rows, i, variant, side, cost)
                if t is None:
                    continue
                trades_by_variant[variant].append(Trade(market, t.variant, t.side, t.entry_ts, t.exit_ts, t.gross_pct, t.net_pct, t.reason))
                last_entry_index[variant] = i

    print('=== LOW FREQUENCY VIABILITY LAB | PUBLIC 4H DATA | READ ONLY ===')
    print(f'PERIODE           : laatste {LOOKBACK_DAYS} dagen')
    print(f'MARKTEN MET DATA  : {len(data)}/{len(markets)}')
    print(f'INTERVAL          : {INTERVAL}')
    print(f'MAX HOLD          : {MAX_HOLD_BARS*4} uur')
    print(f'COOLDOWN          : {COOLDOWN_BARS*4} uur per markt/variant')
    print(f'ROUNDTRIP KOSTEN  : {cost:.2f}%')
    print()

    labels = (
        ('TREND_BREAKOUT_LONG', 'BULL  | 4h breakout LONG | TP5 / SL3'),
        ('TREND_PULLBACK_LONG', 'BULL  | 4h pullback LONG | TP4 / SL3'),
        ('TREND_BREAKDOWN_SHORT', 'BEAR  | 4h breakdown SHORT | TP5 / SL3'),
        ('SIDEWAYS_REENTRY_LONG', 'SIDE  | 4h band reentry LONG | TP3 / SL3'),
        ('SIDEWAYS_REENTRY_SHORT', 'SIDE  | 4h band reentry SHORT | TP3 / SL3'),
    )
    any_positive = False
    for key, label in labels:
        trades = trades_by_variant[key]
        vals = [t.net_pct for t in trades]
        reasons = defaultdict(int)
        for t in trades:
            reasons[t.reason] += 1
        print(label)
        if not trades:
            print('  trades 0')
            continue
        avg = fmean(vals)
        total = sum(vals)
        win = sum(1 for x in vals if x > 0)/len(vals)*100
        pf = _pf(vals)
        pf_text = 'INF' if pf == float('inf') else f'{pf:.3f}'
        print(f'  trades {len(vals):4d} | gem netto {avg:+.3f}% | som {total:+.2f}% | win {win:5.1f}% | PF {pf_text}')
        print(f"  exits  TARGET {reasons['TARGET']} | STOP {reasons['STOP']} | TIME {reasons['TIME']}")
        if len(vals) >= 30 and avg > 0 and pf > 1.10:
            any_positive = True

    print()
    if any_positive:
        print('EINDINDICATIE      : VERDER ONDERZOEKEN | minstens één low-frequency variant toont voorlopige positieve edge')
    else:
        print('EINDINDICATIE      : GEEN OVERTUIGENDE EDGE | autonome trading als hoofdrichting heroverwegen')
    print('LET OP             : research-backtest, geen live bewijs; dezelfde markt kan in meerdere varianten voorkomen.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
