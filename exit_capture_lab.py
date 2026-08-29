from __future__ import annotations

import bisect
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

from adaptive_ls_main import adaptive_v2_db_path
from adaptive_ls_strategy import AdaptiveLongShortStrategy
from adaptive_ls_strict_strategy import StrictAdaptiveLongShortStrategy
from config import Settings
from models import Candle
from regime_strategy_lab import _sideways_bb_macd_signal
from strategy import BandReentryStrategy

TARGETS = (0.80, 1.00, 1.25, 1.50, 2.00)
STOPS = (0.75, 1.00, 1.25, 1.50, 2.00)
HORIZONS = {60: 4, 240: 16}
MIN_RANK_N = 20


@dataclass(frozen=True)
class Signal:
    market: str
    index: int
    side: str


@dataclass(frozen=True)
class Result:
    net_pct: float
    reason: str


def _connect_readonly(path: str) -> sqlite3.Connection:
    absolute = Path(path).resolve().as_posix()
    conn = sqlite3.connect(f'file:{absolute}?mode=ro', uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _fmt_ts(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


def _simulate(rows: list[Candle], index: int, side: str, bars: int,
              target_pct: float, stop_pct: float, cost_pct: float) -> Result | None:
    if index < 0 or index + bars >= len(rows):
        return None
    entry = rows[index].close
    if entry <= 0:
        return None

    if side == 'LONG':
        target = entry * (1.0 + target_pct / 100.0)
        stop = entry * (1.0 - stop_pct / 100.0)
    else:
        target = entry * (1.0 - target_pct / 100.0)
        stop = entry * (1.0 + stop_pct / 100.0)

    for candle in rows[index + 1:index + bars + 1]:
        if side == 'LONG':
            hit_stop = candle.low <= stop
            hit_target = candle.high >= target
        else:
            hit_stop = candle.high >= stop
            hit_target = candle.low <= target

        # Binnen een 15m-candle is de volgorde onbekend. Als beide niveaus
        # geraakt zijn, rekenen we conservatief de stop als eerste.
        if hit_stop:
            return Result(-stop_pct - cost_pct, 'STOP')
        if hit_target:
            return Result(target_pct - cost_pct, 'TARGET')

    close = rows[index + bars].close
    if close <= 0:
        return None
    gross = (close / entry - 1.0) * 100.0 if side == 'LONG' else (entry / close - 1.0) * 100.0
    return Result(gross - cost_pct, 'TIME')


def _pf(values: list[float]) -> float:
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    if losses <= 1e-12:
        return float('inf') if wins > 0 else 0.0
    return wins / losses


def main() -> int:
    settings = Settings()
    settings.validate()
    source_path = adaptive_v2_db_path(settings.db_path)
    source = _connect_readonly(source_path)
    try:
        row = source.execute("SELECT value FROM state WHERE key='universe_json'").fetchone()
        if row is None:
            raise RuntimeError('D v2 universe ontbreekt')
        markets = [str(x) for x in json.loads(str(row['value']))]
        candle_rows = source.execute(
            "SELECT market,timestamp_ms,open,high,low,close,volume FROM candles WHERE interval=? ORDER BY market,timestamp_ms",
            (settings.interval,),
        ).fetchall()
    finally:
        source.close()

    by_market: dict[str, list[Candle]] = {m: [] for m in markets}
    for r in candle_rows:
        market = str(r['market'])
        if market not in by_market:
            continue
        by_market[market].append(Candle(
            int(r['timestamp_ms']), float(r['open']), float(r['high']), float(r['low']),
            float(r['close']), float(r['volume'])
        ))
    by_ts = {m: [c.timestamp_ms for c in rows] for m, rows in by_market.items()}

    strict = StrictAdaptiveLongShortStrategy(settings)
    directional = AdaptiveLongShortStrategy(settings)
    band = BandReentryStrategy(settings)
    required = max(strict.required_candles(), settings.band_window + 1, 40)

    starts = [rows[required - 1].timestamp_ms for rows in by_market.values() if len(rows) >= required]
    if len(starts) < strict.MIN_REGIME_MARKETS:
        raise RuntimeError('te weinig markten met voldoende historie')
    start_ts = max(sorted(starts)[:strict.MIN_REGIME_MARKETS])
    end_ts = max(rows[-1].timestamp_ms for rows in by_market.values() if rows)
    timeline = sorted({c.timestamp_ms for rows in by_market.values() for c in rows if start_ts <= c.timestamp_ms <= end_ts})

    signal_sets: dict[str, list[Signal]] = defaultdict(list)
    for ts in timeline:
        histories: dict[str, list[Candle]] = {}
        indexes: dict[str, int] = {}
        metrics_by_market: dict[str, dict[str, float]] = {}
        for market, rows in by_market.items():
            idx = bisect.bisect_right(by_ts[market], ts) - 1
            if idx < required - 1:
                continue
            hist = rows[max(0, idx - settings.candle_limit + 1):idx + 1]
            metrics = strict.analyze(hist)
            if not metrics:
                continue
            histories[market] = hist
            indexes[market] = idx
            metrics_by_market[market] = metrics

        regime, bull, bear = strict.market_regime(metrics_by_market)
        if regime not in {'BULL', 'BEAR', 'SIDEWAYS'}:
            continue

        for market, metrics in metrics_by_market.items():
            hist = histories[market]
            idx = indexes[market]
            if regime == 'BULL':
                decision = directional.evaluate_metrics(metrics, regime, bull, bear)
                if decision.action == 'LONG':
                    signal_sets['BULL_LONG_DV2'].append(Signal(market, idx, 'LONG'))
            elif regime == 'BEAR':
                decision = strict.evaluate_metrics(metrics, regime, bull, bear)
                if decision.action == 'SHORT':
                    signal_sets['BEAR_SHORT_DV2S'].append(Signal(market, idx, 'SHORT'))
            else:
                decision = band.evaluate(hist)
                if decision.action == 'BUY':
                    signal_sets['SIDEWAYS_A'].append(Signal(market, idx, 'LONG'))
                public_signal, _ = _sideways_bb_macd_signal(hist)
                if public_signal:
                    signal_sets['SIDEWAYS_BB_MACD'].append(Signal(market, idx, 'LONG'))

    cost = 2.0 * settings.taker_fee_pct + 2.0 * settings.slippage_pct + settings.backtest_assumed_spread_pct
    labels = (
        ('BULL_LONG_DV2', 'BULL  | D v2 LONG trend'),
        ('BEAR_SHORT_DV2S', 'BEAR  | D v2S strict SHORT'),
        ('SIDEWAYS_A', 'SIDE  | huidige A band-reentry'),
        ('SIDEWAYS_BB_MACD', 'SIDE  | BB+MACD recovery research'),
    )

    print('=== EXIT CAPTURE LAB | READ ONLY ===')
    print(f'BRON DB           : {source_path}')
    print(f'PERIODE UTC       : {_fmt_ts(start_ts)} -> {_fmt_ts(end_ts)}')
    print(f'MARKTEN           : {len(markets)}')
    print(f'ROUNDTRIP KOSTEN  : {cost:.2f}%')
    print('AANNAME           : target+stop in dezelfde 15m-candle => STOP eerst (conservatief)')
    print()

    for key, label in labels:
        signals = signal_sets[key]
        print(label)
        print(f'  ruwe signalen   : {len(signals)}')
        if len(signals) < MIN_RANK_N:
            print(f'  onvoldoende voor rangschikking (<{MIN_RANK_N})')
            print()
            continue

        ranked: list[tuple[float, float, int, float, float, float, int, int, int, int]] = []
        for minutes, bars in HORIZONS.items():
            for target in TARGETS:
                for stop in STOPS:
                    results: list[Result] = []
                    for sig in signals:
                        result = _simulate(by_market[sig.market], sig.index, sig.side, bars, target, stop, cost)
                        if result is not None:
                            results.append(result)
                    if len(results) < MIN_RANK_N:
                        continue
                    values = [r.net_pct for r in results]
                    reasons = Counter(r.reason for r in results)
                    avg = fmean(values)
                    win = sum(1 for v in values if v > 0) / len(values) * 100.0
                    pf = _pf(values)
                    ranked.append((avg, pf, minutes, target, stop, win, len(values), reasons['TARGET'], reasons['STOP'], reasons['TIME']))

        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        print('  beste combinaties op gemiddelde NETTO return:')
        for avg, pf, minutes, target, stop, win, n, t, s, tm in ranked[:8]:
            pf_text = 'INF' if pf == float('inf') else f'{pf:.3f}'
            print(
                f'  {minutes:3d}m | TP {target:>4.2f}% | SL {stop:>4.2f}% | n={n:3d}'
                f' | gem {avg:+.3f}% | win {win:5.1f}% | PF {pf_text:>5s}'
                f' | T/S/time {t}/{s}/{tm}'
            )
        print()

    print('LET OP: diagnostische candle-replay, geen portfolio-backtest. Signalen mogen overlappen;')
    print('historische orderboekspread en intrabar-volgorde zijn niet exact bekend. Geen botdatabase wordt gewijzigd.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
