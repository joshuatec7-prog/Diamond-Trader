from __future__ import annotations

import bisect
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean, median

from adaptive_ls_main import adaptive_v2_db_path
from adaptive_ls_strategy import AdaptiveLongShortStrategy
from adaptive_ls_strict_strategy import StrictAdaptiveLongShortStrategy
from config import Settings
from models import Candle
from regime_strategy_lab import _sideways_bb_macd_signal
from strategy import BandReentryStrategy

HORIZONS = {60: 4, 240: 16}


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


def _excursion(candles: list[Candle], current_index: int, bars: int, side: str) -> tuple[float, float] | None:
    end = current_index + bars
    if current_index < 0 or end >= len(candles):
        return None
    entry = candles[current_index].close
    if entry <= 0:
        return None
    future = candles[current_index + 1:end + 1]
    if not future:
        return None
    highest = max(c.high for c in future)
    lowest = min(c.low for c in future)
    if side == 'LONG':
        mfe = (highest / entry - 1.0) * 100.0
        mae = (entry / lowest - 1.0) * 100.0
    else:
        mfe = (1.0 - lowest / entry) * 100.0
        mae = (highest / entry - 1.0) * 100.0
    return max(0.0, mfe), max(0.0, mae)


def _summary(rows: list[tuple[float, float]], cost: float) -> dict[str, float]:
    if not rows:
        return {
            'n': 0, 'avg_mfe': 0.0, 'med_mfe': 0.0, 'avg_mae': 0.0,
            'reach_cost': 0.0, 'reach_1': 0.0, 'reach_15': 0.0,
        }
    mfes = [x[0] for x in rows]
    maes = [x[1] for x in rows]
    n = len(rows)
    return {
        'n': float(n),
        'avg_mfe': fmean(mfes),
        'med_mfe': median(mfes),
        'avg_mae': fmean(maes),
        'reach_cost': sum(1 for x in mfes if x >= cost) / n * 100.0,
        'reach_1': sum(1 for x in mfes if x >= 1.0) / n * 100.0,
        'reach_15': sum(1 for x in mfes if x >= 1.5) / n * 100.0,
    }


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

    cost = 2.0 * settings.taker_fee_pct + 2.0 * settings.slippage_pct + settings.backtest_assumed_spread_pct
    regimes = Counter()
    counts = Counter()
    excursions: dict[str, dict[int, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))

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
        regimes[regime] += 1
        if regime not in {'BULL', 'BEAR', 'SIDEWAYS'}:
            continue

        for market, metrics in metrics_by_market.items():
            hist = histories[market]
            idx = indexes[market]
            side = ''
            key = ''

            if regime == 'BULL':
                decision = directional.evaluate_metrics(metrics, regime, bull, bear)
                if decision.action == 'LONG':
                    key, side = 'BULL_LONG_DV2_LOGIC', 'LONG'
            elif regime == 'BEAR':
                decision = strict.evaluate_metrics(metrics, regime, bull, bear)
                if decision.action == 'SHORT':
                    key, side = 'BEAR_SHORT_DV2S_STRICT', 'SHORT'
            else:
                a_decision = band.evaluate(hist)
                if a_decision.action == 'BUY':
                    key, side = 'SIDEWAYS_A_BAND_REENTRY', 'LONG'
                public_signal, _ = _sideways_bb_macd_signal(hist)
                if public_signal:
                    pkey = 'SIDEWAYS_BB_MACD_RECOVERY'
                    counts[pkey] += 1
                    for minutes, bars in HORIZONS.items():
                        value = _excursion(by_market[market], idx, bars, 'LONG')
                        if value is not None:
                            excursions[pkey][minutes].append(value)

            if key:
                counts[key] += 1
                for minutes, bars in HORIZONS.items():
                    value = _excursion(by_market[market], idx, bars, side)
                    if value is not None:
                        excursions[key][minutes].append(value)

    print('=== SIGNAL EXCURSION LAB | READ ONLY ===')
    print(f'BRON DB           : {source_path}')
    print(f'PERIODE UTC       : {_fmt_ts(start_ts)} -> {_fmt_ts(end_ts)}')
    print(f'MARKTEN           : {len(markets)}')
    print(f'ROUNDTRIP KOSTEN  : {cost:.2f}%')
    print()
    print('MFE = maximale gunstige beweging na het signaal')
    print('MAE = maximale ongunstige beweging na het signaal')
    print()

    labels = (
        ('BULL_LONG_DV2_LOGIC', 'BULL  | D v2 LONG trend'),
        ('BEAR_SHORT_DV2S_STRICT', 'BEAR  | D v2S strict SHORT'),
        ('SIDEWAYS_A_BAND_REENTRY', 'SIDE  | huidige A band-reentry'),
        ('SIDEWAYS_BB_MACD_RECOVERY', 'SIDE  | BB+MACD recovery research'),
    )
    for key, label in labels:
        print(label)
        print(f'  ruwe signalen   : {counts[key]}')
        for minutes in HORIZONS:
            s = _summary(excursions[key][minutes], cost)
            print(
                f"  +{minutes:3d}m n={int(s['n']):4d} | MFE gem {s['avg_mfe']:+.3f}% med {s['med_mfe']:+.3f}%"
                f" | MAE gem {s['avg_mae']:+.3f}% | MFE>=kosten {s['reach_cost']:5.1f}%"
                f" | >=1.0% {s['reach_1']:5.1f}% | >=1.5% {s['reach_15']:5.1f}%"
            )
    print()
    print('LET OP: diagnostiek, geen trade-backtest. Beste/slechtste intraperiode-beweging wordt achteraf gemeten;')
    print('volgorde binnen een 15m-candle is niet bekend. Er wordt niets naar botdatabases geschreven.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
