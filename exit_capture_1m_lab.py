from __future__ import annotations

import bisect
import json
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

from adaptive_ls_main import adaptive_v2_db_path
from adaptive_ls_strategy import AdaptiveLongShortStrategy
from adaptive_ls_strict_strategy import StrictAdaptiveLongShortStrategy
from bitvavo_public import BitvavoPublic, INTERVAL_MS
from config import Settings
from models import Candle
from regime_strategy_lab import _sideways_bb_macd_signal
from strategy import BandReentryStrategy

TARGETS = (0.80, 1.00, 1.25, 1.50, 2.00)
STOPS = (0.75, 1.00, 1.25, 1.50, 2.00)
HORIZONS = (60, 240)
MIN_RANK_N = 20
ONE_MINUTE_MS = 60_000
MAX_API_CANDLES = 1440


@dataclass(frozen=True)
class Signal:
    market: str
    entry_ts: int
    entry_price: float
    side: str


@dataclass(frozen=True)
class Result:
    net_pct: float
    reason: str
    ambiguous_1m: bool = False


def _connect_readonly(path: str) -> sqlite3.Connection:
    absolute = Path(path).resolve().as_posix()
    conn = sqlite3.connect(f'file:{absolute}?mode=ro', uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _parse_candles(payload: object) -> list[Candle]:
    if not isinstance(payload, list):
        return []
    parsed: dict[int, Candle] = {}
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


def _fetch_1m(api: BitvavoPublic, market: str, start_ms: int, end_ms: int) -> tuple[list[Candle], int]:
    out: dict[int, Candle] = {}
    requests = 0
    cursor = (start_ms // ONE_MINUTE_MS) * ONE_MINUTE_MS
    final_end = (end_ms // ONE_MINUTE_MS) * ONE_MINUTE_MS
    chunk_ms = MAX_API_CANDLES * ONE_MINUTE_MS
    while cursor < final_end:
        chunk_end = min(final_end, cursor + chunk_ms)
        payload = api._get(
            f'/{market}/candles',
            {'interval': '1m', 'limit': MAX_API_CANDLES, 'start': cursor, 'end': chunk_end},
        )
        requests += 1
        for candle in _parse_candles(payload):
            out[candle.timestamp_ms] = candle
        cursor = chunk_end
    return [out[k] for k in sorted(out)], requests


def _simulate_1m(rows: list[Candle], timestamps: list[int], sig: Signal, minutes: int,
                 target_pct: float, stop_pct: float, cost_pct: float) -> Result | None:
    start = bisect.bisect_left(timestamps, sig.entry_ts)
    end_ts = sig.entry_ts + minutes * ONE_MINUTE_MS
    end = bisect.bisect_left(timestamps, end_ts)
    future = rows[start:end]
    if not future:
        return None

    if sig.side == 'LONG':
        target = sig.entry_price * (1.0 + target_pct / 100.0)
        stop = sig.entry_price * (1.0 - stop_pct / 100.0)
    else:
        target = sig.entry_price * (1.0 - target_pct / 100.0)
        stop = sig.entry_price * (1.0 + stop_pct / 100.0)

    for candle in future:
        if sig.side == 'LONG':
            hit_stop = candle.low <= stop
            hit_target = candle.high >= target
        else:
            hit_stop = candle.high >= stop
            hit_target = candle.low <= target
        if hit_stop and hit_target:
            return Result(-stop_pct - cost_pct, 'STOP', True)
        if hit_stop:
            return Result(-stop_pct - cost_pct, 'STOP')
        if hit_target:
            return Result(target_pct - cost_pct, 'TARGET')

    # Alleen gebruiken als de 1m-data nagenoeg de hele horizon afdekt.
    last = future[-1]
    if last.timestamp_ms < end_ts - 2 * ONE_MINUTE_MS:
        return None
    gross = (
        (last.close / sig.entry_price - 1.0) * 100.0
        if sig.side == 'LONG'
        else (sig.entry_price / last.close - 1.0) * 100.0
    )
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
        if market in by_market:
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
            entry_ts = by_market[market][idx].timestamp_ms + INTERVAL_MS[settings.interval]
            entry_price = by_market[market][idx].close
            if regime == 'BULL':
                d = directional.evaluate_metrics(metrics, regime, bull, bear)
                if d.action == 'LONG':
                    signal_sets['BULL_LONG_DV2'].append(Signal(market, entry_ts, entry_price, 'LONG'))
            elif regime == 'BEAR':
                d = strict.evaluate_metrics(metrics, regime, bull, bear)
                if d.action == 'SHORT':
                    signal_sets['BEAR_SHORT_DV2S'].append(Signal(market, entry_ts, entry_price, 'SHORT'))
            else:
                d = band.evaluate(hist)
                if d.action == 'BUY':
                    signal_sets['SIDEWAYS_A'].append(Signal(market, entry_ts, entry_price, 'LONG'))
                public_signal, _ = _sideways_bb_macd_signal(hist)
                if public_signal:
                    signal_sets['SIDEWAYS_BB_MACD'].append(Signal(market, entry_ts, entry_price, 'LONG'))

    all_signals = [s for rows in signal_sets.values() for s in rows]
    if not all_signals:
        raise RuntimeError('geen signalen voor 1m replay')
    fetch_start = min(s.entry_ts for s in all_signals)
    now_closed = (int(time.time() * 1000) // ONE_MINUTE_MS) * ONE_MINUTE_MS
    fetch_end = min(max(s.entry_ts for s in all_signals) + max(HORIZONS) * ONE_MINUTE_MS, now_closed)

    api = BitvavoPublic(settings.api_base_url, settings.request_timeout_seconds, settings.request_retries)
    one_minute: dict[str, list[Candle]] = {}
    one_minute_ts: dict[str, list[int]] = {}
    request_count = 0
    for market in markets:
        rows, nreq = _fetch_1m(api, market, fetch_start, fetch_end)
        one_minute[market] = rows
        one_minute_ts[market] = [c.timestamp_ms for c in rows]
        request_count += nreq

    cost = 2.0 * settings.taker_fee_pct + 2.0 * settings.slippage_pct + settings.backtest_assumed_spread_pct
    labels = (
        ('BULL_LONG_DV2', 'BULL  | D v2 LONG trend'),
        ('BEAR_SHORT_DV2S', 'BEAR  | D v2S strict SHORT'),
        ('SIDEWAYS_A', 'SIDE  | huidige A band-reentry'),
        ('SIDEWAYS_BB_MACD', 'SIDE  | BB+MACD recovery research'),
    )

    print('=== EXIT CAPTURE 1M LAB | PUBLIC DATA | READ ONLY ===')
    print(f'BRON SIGNALEN     : {source_path}')
    print(f'MARKTEN           : {len(markets)}')
    print(f'PUBLIEKE 1M CALLS : {request_count}')
    print(f'1M CANDLES TOTAAL : {sum(len(v) for v in one_minute.values())}')
    print(f'ROUNDTRIP KOSTEN  : {cost:.2f}%')
    print('AANNAME           : target+stop in dezelfde 1m-candle => STOP eerst')
    print()

    for key, label in labels:
        signals = signal_sets[key]
        print(label)
        print(f'  ruwe signalen   : {len(signals)}')
        if len(signals) < MIN_RANK_N:
            print(f'  onvoldoende voor rangschikking (<{MIN_RANK_N})')
            print()
            continue
        ranked = []
        for minutes in HORIZONS:
            for target in TARGETS:
                for stop in STOPS:
                    results: list[Result] = []
                    for sig in signals:
                        r = _simulate_1m(
                            one_minute[sig.market], one_minute_ts[sig.market], sig,
                            minutes, target, stop, cost,
                        )
                        if r is not None:
                            results.append(r)
                    if len(results) < MIN_RANK_N:
                        continue
                    values = [r.net_pct for r in results]
                    reasons = Counter(r.reason for r in results)
                    avg = fmean(values)
                    win = sum(1 for v in values if v > 0) / len(values) * 100.0
                    pf = _pf(values)
                    ambiguous = sum(1 for r in results if r.ambiguous_1m)
                    ranked.append((avg, pf, minutes, target, stop, win, len(values), reasons['TARGET'], reasons['STOP'], reasons['TIME'], ambiguous))
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        print('  beste combinaties met 1m-volgorde:')
        for avg, pf, minutes, target, stop, win, n, t, s, tm, amb in ranked[:8]:
            pf_text = 'INF' if pf == float('inf') else f'{pf:.3f}'
            print(
                f'  {minutes:3d}m | TP {target:>4.2f}% | SL {stop:>4.2f}% | n={n:3d}'
                f' | gem {avg:+.3f}% | win {win:5.1f}% | PF {pf_text:>5s}'
                f' | T/S/time {t}/{s}/{tm} | 1m-ambigu {amb}'
            )
        print()

    print('LET OP: publieke 1m candle-replay. Alleen als TP en SL binnen dezelfde 1m-candle vallen')
    print('blijft de exacte volgorde onbekend. Er wordt niets naar botdatabases geschreven.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
