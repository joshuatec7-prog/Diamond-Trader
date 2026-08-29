from __future__ import annotations

import bisect
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean, pstdev

from adaptive_ls_main import adaptive_v2_db_path
from adaptive_ls_strategy import AdaptiveLongShortStrategy
from adaptive_ls_strict_strategy import StrictAdaptiveLongShortStrategy
from config import Settings
from models import Candle
from strategy import BandReentryStrategy

HORIZONS = {60: 4, 240: 16}


def _connect_readonly(path: str) -> sqlite3.Connection:
    absolute = Path(path).resolve().as_posix()
    conn = sqlite3.connect(f'file:{absolute}?mode=ro', uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _sideways_bb_macd_signal(candles: list[Candle]) -> tuple[bool, dict[str, float]]:
    if len(candles) < 40:
        return False, {}
    closes = [c.close for c in candles]
    sample = closes[-20:]
    mid = fmean(sample)
    sigma = pstdev(sample)
    if mid <= 0 or sigma <= 0:
        return False, {}
    lower = mid - 2.0 * sigma
    upper = mid + 2.0 * sigma
    width = upper - lower
    if width <= 0:
        return False, {}
    percent_b = (closes[-1] - lower) / width

    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_series = [a - b for a, b in zip(ema12, ema26)]
    signal_series = _ema_series(macd_series, 9)
    macd = macd_series[-1]
    hist = macd - signal_series[-1]

    metrics = {
        'percent_b': percent_b,
        'macd': macd,
        'macd_hist': hist,
        'close': closes[-1],
        'prev_close': closes[-2],
    }
    signal = percent_b < 0.20 and hist > 0.0 and macd < 0.0 and closes[-1] > closes[-2]
    return signal, metrics


def _future_return(candles: list[Candle], current_index: int, bars: int, side: str) -> float | None:
    future_index = current_index + bars
    if current_index < 0 or future_index >= len(candles):
        return None
    current = candles[current_index].close
    future = candles[future_index].close
    if current <= 0 or future <= 0:
        return None
    if side == 'LONG':
        return (future / current - 1.0) * 100.0
    return (current / future - 1.0) * 100.0


def _summary(values: list[float], cost_pct: float) -> tuple[int, float, float, float]:
    if not values:
        return 0, 0.0, 0.0, 0.0
    gross = fmean(values)
    net_values = [v - cost_pct for v in values]
    return len(values), gross, fmean(net_values), sum(1 for v in net_values if v > 0) / len(net_values) * 100.0


def _fmt_ts(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


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

    router = StrictAdaptiveLongShortStrategy(settings)
    directional = AdaptiveLongShortStrategy(settings)
    band = BandReentryStrategy(settings)
    required = max(router.required_candles(), settings.band_window + 1, 40)

    starts = [rows[required - 1].timestamp_ms for rows in by_market.values() if len(rows) >= required]
    if len(starts) < router.MIN_REGIME_MARKETS:
        raise RuntimeError('te weinig markten met voldoende historie')
    start_ts = max(sorted(starts)[:router.MIN_REGIME_MARKETS])
    end_ts = max(rows[-1].timestamp_ms for rows in by_market.values() if rows)
    timeline = sorted({c.timestamp_ms for rows in by_market.values() for c in rows if start_ts <= c.timestamp_ms <= end_ts})

    regimes = Counter()
    signals: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    signal_counts = Counter()

    roundtrip_cost_pct = 2.0 * settings.taker_fee_pct + 2.0 * settings.slippage_pct + settings.backtest_assumed_spread_pct

    for ts in timeline:
        histories: dict[str, list[Candle]] = {}
        current_indexes: dict[str, int] = {}
        metrics_by_market: dict[str, dict[str, float]] = {}
        for market, rows in by_market.items():
            idx = bisect.bisect_right(by_ts[market], ts) - 1
            if idx < required - 1:
                continue
            hist = rows[max(0, idx - settings.candle_limit + 1):idx + 1]
            metrics = router.analyze(hist)
            if not metrics:
                continue
            histories[market] = hist
            current_indexes[market] = idx
            metrics_by_market[market] = metrics

        regime, bull, bear = router.market_regime(metrics_by_market)
        regimes[regime] += 1
        if regime not in {'BULL', 'BEAR', 'SIDEWAYS'}:
            continue

        for market, metrics in metrics_by_market.items():
            hist = histories[market]
            idx = current_indexes[market]
            rows = by_market[market]

            if regime == 'BULL':
                decision = directional.evaluate_metrics(metrics, regime, bull, bear)
                if decision.action == 'LONG':
                    key = 'BULL_LONG_DV2_LOGIC'
                    signal_counts[key] += 1
                    for minutes, bars in HORIZONS.items():
                        value = _future_return(rows, idx, bars, 'LONG')
                        if value is not None:
                            signals[key][minutes].append(value)

            elif regime == 'BEAR':
                decision = router.evaluate_metrics(metrics, regime, bull, bear)
                if decision.action == 'SHORT':
                    key = 'BEAR_SHORT_DV2S_STRICT'
                    signal_counts[key] += 1
                    for minutes, bars in HORIZONS.items():
                        value = _future_return(rows, idx, bars, 'SHORT')
                        if value is not None:
                            signals[key][minutes].append(value)

            else:
                a_decision = band.evaluate(hist)
                if a_decision.action == 'BUY':
                    key = 'SIDEWAYS_A_BAND_REENTRY'
                    signal_counts[key] += 1
                    for minutes, bars in HORIZONS.items():
                        value = _future_return(rows, idx, bars, 'LONG')
                        if value is not None:
                            signals[key][minutes].append(value)

                public_signal, _ = _sideways_bb_macd_signal(hist)
                if public_signal:
                    key = 'SIDEWAYS_BB_MACD_RECOVERY'
                    signal_counts[key] += 1
                    for minutes, bars in HORIZONS.items():
                        value = _future_return(rows, idx, bars, 'LONG')
                        if value is not None:
                            signals[key][minutes].append(value)

    benchmark_returns = []
    btc_return = None
    for market, rows in by_market.items():
        first_i = bisect.bisect_left(by_ts[market], start_ts)
        last_i = bisect.bisect_right(by_ts[market], end_ts) - 1
        if first_i >= len(rows) or last_i <= first_i:
            continue
        ret = (rows[last_i].close / rows[first_i].close - 1.0) * 100.0
        benchmark_returns.append(ret)
        if market == 'BTC-EUR':
            btc_return = ret

    print('=== REGIME STRATEGY LAB | READ ONLY ===')
    print(f'BRON DB           : {source_path}')
    print(f'PERIODE UTC       : {_fmt_ts(start_ts)} -> {_fmt_ts(end_ts)}')
    print(f'MARKTEN           : {len(markets)}')
    print(f'ROUNDTRIP KOSTEN  : {roundtrip_cost_pct:.2f}% (fee+slippage+spread-aanname)')
    print()
    print('=== REGIME VERDELING ===')
    for name in ('BULL', 'BEAR', 'SIDEWAYS', 'UNKNOWN'):
        print(f'{name:9s}: {regimes[name]}')
    print()
    print('=== MARKT BENCHMARK ===')
    print(f'20-COIN GEMIDDELD : {fmean(benchmark_returns):+.2f}%' if benchmark_returns else '20-COIN GEMIDDELD : n/a')
    print(f'BTC-EUR           : {btc_return:+.2f}%' if btc_return is not None else 'BTC-EUR           : n/a')
    print('CASH              : +0.00%')
    print()
    print('=== SIGNAALKWALITEIT PER REGIME ===')
    labels = (
        ('BULL_LONG_DV2_LOGIC', 'BULL  | D v2 LONG trend'),
        ('BEAR_SHORT_DV2S_STRICT', 'BEAR  | D v2S strict SHORT'),
        ('SIDEWAYS_A_BAND_REENTRY', 'SIDE  | huidige A band-reentry'),
        ('SIDEWAYS_BB_MACD_RECOVERY', 'SIDE  | BB+MACD recovery research'),
    )
    for key, label in labels:
        print(label)
        print(f'  ruwe signalen   : {signal_counts[key]}')
        for minutes in HORIZONS:
            n, gross, net, positive = _summary(signals[key][minutes], roundtrip_cost_pct)
            print(f'  +{minutes:3d}m n={n:4d} | bruto {gross:+.3f}% | na kosten {net:+.3f}% | >0 {positive:5.1f}%')
    print()
    print('LET OP: dit is geen trade-backtest. Het meet alleen forward-return na signalen,')
    print('zonder stop/trailing/portfolio-overlap. Er wordt niets naar de botdatabases geschreven.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
