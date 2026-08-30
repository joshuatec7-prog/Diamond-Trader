from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bitvavo_public import BitvavoPublic


logger = logging.getLogger('cryptobot_v4_research')
STOP = False

MARKETS = ('BTC-USDC', 'ETH-USDC')
DAY_MS = 86_400_000
SMA_DAYS = 65
VOLATILITY_DAYS = 20
DEFAULT_VOLATILITY_LIMIT_PCT = 80.0
USDC_TAKER_FEE_PCT = 0.05
DEFAULT_SLIPPAGE_BUFFER_PCT = 0.05
MIN_RESEARCH_WEEKS = 26


def _data_path(filename: str) -> Path:
    data = Path('/var/data')
    if os.name != 'nt' and data.exists() and os.access(data, os.W_OK):
        return data / filename
    return Path('data') / filename


def _report_path() -> Path:
    raw = os.getenv('V4_RESEARCH_REPORT_PATH')
    return Path(raw) if raw else _data_path('cryptobot_v4_research.json')


def _db_path() -> Path:
    raw = os.getenv('V4_RESEARCH_DB_PATH')
    return Path(raw) if raw else _data_path('cryptobot_v4_research.db')


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def _weekly_cutoff_ms(now_ms: int) -> int:
    now = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_sunday = (midnight.weekday() + 1) % 7
    sunday = midnight - timedelta(days=days_since_sunday)
    return int(sunday.timestamp() * 1000)


def _sma(values: list[float], window: int) -> float:
    if window <= 0 or len(values) < window:
        raise ValueError('onvoldoende waarden voor gemiddelde')
    return sum(values[-window:]) / window


def _realized_volatility_pct(closes: list[float], window: int = VOLATILITY_DAYS) -> float:
    if len(closes) < window + 1:
        raise ValueError('onvoldoende slotkoersen voor volatiliteit')
    selected = closes[-(window + 1):]
    returns = [math.log(selected[index] / selected[index - 1]) for index in range(1, len(selected))]
    if len(returns) < 2:
        return 0.0
    return statistics.stdev(returns) * math.sqrt(365.0) * 100.0


def _target_weights(rows: list[dict[str, Any]], volatility_limit_pct: float) -> tuple[dict[str, float], float]:
    if not math.isfinite(volatility_limit_pct) or volatility_limit_pct <= 0.0:
        raise ValueError('volatiliteitslimiet moet positief zijn')
    active = [row for row in rows if bool(row.get('long_signal'))]
    if not active:
        return {str(row['market']): 0.0 for row in rows}, 1.0
    average_volatility = sum(float(row['volatility_20d_pct']) for row in active) / len(active)
    exposure_scale = min(1.0, volatility_limit_pct / average_volatility) if average_volatility > 0.0 else 1.0
    weight = exposure_scale / len(active)
    weights = {str(row['market']): (weight if row in active else 0.0) for row in rows}
    return weights, max(0.0, 1.0 - sum(weights.values()))


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS weekly_snapshots (
            decision_week_ms INTEGER NOT NULL,
            recorded_ms INTEGER NOT NULL,
            market TEXT NOT NULL,
            decision_close_ms INTEGER NOT NULL,
            close REAL NOT NULL,
            sma_65 REAL NOT NULL,
            volatility_20d_pct REAL NOT NULL,
            long_signal INTEGER NOT NULL,
            target_weight REAL NOT NULL,
            measured_spread_pct REAL NOT NULL,
            PRIMARY KEY (decision_week_ms, market)
        )'''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_v4_week ON weekly_snapshots(decision_week_ms)')
    conn.commit()


def _db_connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    _ensure_schema(conn)
    return conn


def _store_week(conn: sqlite3.Connection, week_ms: int, recorded_ms: int, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        conn.execute(
            '''INSERT OR IGNORE INTO weekly_snapshots
               (decision_week_ms,recorded_ms,market,decision_close_ms,close,sma_65,
                volatility_20d_pct,long_signal,target_weight,measured_spread_pct)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (
                week_ms,
                recorded_ms,
                row['market'],
                row['decision_close_ms'],
                row['close'],
                row['sma_65'],
                row['volatility_20d_pct'],
                1 if row['long_signal'] else 0,
                row['target_weight'],
                row['measured_spread_pct'],
            ),
        )
    conn.commit()


def _load_week(conn: sqlite3.Connection, week_ms: int) -> list[dict[str, Any]]:
    records = conn.execute(
        '''SELECT market,decision_close_ms,close,sma_65,volatility_20d_pct,long_signal,
                  target_weight,measured_spread_pct
           FROM weekly_snapshots WHERE decision_week_ms=? ORDER BY market''',
        (week_ms,),
    ).fetchall()
    return [
        {
            'market': row[0],
            'decision_close_ms': int(row[1]),
            'close': float(row[2]),
            'sma_65': float(row[3]),
            'volatility_20d_pct': float(row[4]),
            'long_signal': bool(row[5]),
            'target_weight': float(row[6]),
            'measured_spread_pct': float(row[7]),
        }
        for row in records
    ]


def _max_drawdown_pct(values: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0.0:
            worst = min(worst, value / peak - 1.0)
    return abs(worst) * 100.0


def _benchmark_summary(
    conn: sqlite3.Connection,
    *,
    slippage_buffer_pct: float = DEFAULT_SLIPPAGE_BUFFER_PCT,
) -> dict[str, float]:
    records = conn.execute(
        '''SELECT decision_week_ms,market,close,target_weight,measured_spread_pct
           FROM weekly_snapshots ORDER BY decision_week_ms,market'''
    ).fetchall()
    grouped: dict[int, dict[str, dict[str, float]]] = {}
    for week_ms, market, close, weight, spread in records:
        grouped.setdefault(int(week_ms), {})[str(market)] = {
            'close': float(close),
            'weight': float(weight),
            'spread': float(spread),
        }
    weeks = [(week, grouped[week]) for week in sorted(grouped) if all(market in grouped[week] for market in MARKETS)]
    if not weeks:
        return {
            'weeks': 0.0,
            'v4_index': 100.0,
            'v4_cost_stress_2x_index': 100.0,
            'v4_cost_stress_3x_index': 100.0,
            'buy_hold_50_50_index': 100.0,
            'weekly_dca_index': 100.0,
            'cash_usdc_index': 100.0,
            'v4_max_drawdown_pct': 0.0,
        }

    first_rows = weeks[0][1]
    first_prices = {market: first_rows[market]['close'] for market in MARKETS}
    first_cost_fraction = sum(
        0.5
        * (USDC_TAKER_FEE_PCT + slippage_buffer_pct + first_rows[market]['spread'] / 2.0)
        / 100.0
        for market in MARKETS
    )
    dca_units = {market: 0.0 for market in MARKETS}
    v4_indices = {1: 100.0, 2: 100.0, 3: 100.0}
    v4_path: list[float] = []
    previous_rows: dict[str, dict[str, float]] | None = None
    contribution = 0.0
    dca_index = 100.0

    for _, current_rows in weeks:
        if previous_rows is not None:
            portfolio_return = 1.0 + sum(
                previous_rows[market]['weight']
                * (current_rows[market]['close'] / previous_rows[market]['close'] - 1.0)
                for market in MARKETS
            )
            for multiplier in v4_indices:
                v4_indices[multiplier] *= max(0.0, portfolio_return)

        old_weights = (
            {market: previous_rows[market]['weight'] for market in MARKETS}
            if previous_rows is not None
            else {market: 0.0 for market in MARKETS}
        )
        rebalance_cost_fraction = sum(
            abs(current_rows[market]['weight'] - old_weights[market])
            * (USDC_TAKER_FEE_PCT + slippage_buffer_pct + current_rows[market]['spread'] / 2.0)
            / 100.0
            for market in MARKETS
        )
        for multiplier in v4_indices:
            v4_indices[multiplier] *= max(0.0, 1.0 - rebalance_cost_fraction * multiplier)
        v4_path.append(v4_indices[1])

        contribution += 1.0
        for market in MARKETS:
            execution_cost_pct = USDC_TAKER_FEE_PCT + slippage_buffer_pct + current_rows[market]['spread'] / 2.0
            dca_units[market] += 0.5 * (1.0 - execution_cost_pct / 100.0) / current_rows[market]['close']
        dca_value = sum(dca_units[market] * current_rows[market]['close'] for market in MARKETS)
        dca_index = dca_value / contribution * 100.0
        previous_rows = current_rows

    latest_rows = weeks[-1][1]
    buy_hold_index = (
        sum(0.5 * latest_rows[market]['close'] / first_prices[market] for market in MARKETS)
        * (1.0 - first_cost_fraction)
        * 100.0
    )
    return {
        'weeks': float(len(weeks)),
        'v4_index': round(v4_indices[1], 4),
        'v4_cost_stress_2x_index': round(v4_indices[2], 4),
        'v4_cost_stress_3x_index': round(v4_indices[3], 4),
        'buy_hold_50_50_index': round(buy_hold_index, 4),
        'weekly_dca_index': round(dca_index, 4),
        'cash_usdc_index': 100.0,
        'v4_max_drawdown_pct': round(_max_drawdown_pct(v4_path), 4),
    }


def scan_once() -> dict[str, object]:
    now_ms = int(time.time() * 1000)
    week_ms = _weekly_cutoff_ms(now_ms)
    volatility_limit = float(os.getenv('V4_VOLATILITY_LIMIT_PCT', str(DEFAULT_VOLATILITY_LIMIT_PCT)))
    slippage_buffer = float(os.getenv('V4_SLIPPAGE_BUFFER_PCT', str(DEFAULT_SLIPPAGE_BUFFER_PCT)))
    api = BitvavoPublic(
        os.getenv('BITVAVO_API_BASE_URL', 'https://api.bitvavo.com/v2'),
        timeout_seconds=max(3, int(os.getenv('REQUEST_TIMEOUT_SECONDS', '10'))),
        retries=max(1, int(os.getenv('REQUEST_RETRIES', '3'))),
    )

    rows: list[dict[str, Any]] = []
    for market in MARKETS:
        candles = api.closed_candles(market, '1d', 120, now_ms=now_ms)
        eligible = [candle for candle in candles if candle.timestamp_ms + DAY_MS <= week_ms]
        if len(eligible) < SMA_DAYS:
            raise RuntimeError(f'{market}: minder dan {SMA_DAYS} volledige dagcandles voor weekbesluit')
        latest = eligible[-1]
        if latest.timestamp_ms + DAY_MS != week_ms:
            raise RuntimeError(f'{market}: laatste volledige weekafsluiting ontbreekt')
        closes = [float(candle.close) for candle in eligible]
        sma_65 = _sma(closes, SMA_DAYS)
        volatility = _realized_volatility_pct(closes, VOLATILITY_DAYS)
        book = api.book(market)
        rows.append({
            'market': market,
            'decision_close_ms': latest.timestamp_ms,
            'close': closes[-1],
            'sma_65': sma_65,
            'distance_to_sma_pct': (closes[-1] / sma_65 - 1.0) * 100.0,
            'volatility_20d_pct': volatility,
            'long_signal': closes[-1] > sma_65,
            'measured_spread_pct': book.spread_pct,
        })

    weights, _ = _target_weights(rows, volatility_limit)
    for row in rows:
        row['target_weight'] = weights[str(row['market'])]

    conn = _db_connect()
    try:
        _store_week(conn, week_ms, now_ms, rows)
        frozen_rows = _load_week(conn, week_ms)
        benchmarks = _benchmark_summary(conn, slippage_buffer_pct=slippage_buffer)
    finally:
        conn.close()

    frozen_cash_weight = max(0.0, 1.0 - sum(float(row['target_weight']) for row in frozen_rows))
    generated = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).isoformat()
    week = datetime.fromtimestamp(week_ms / 1000.0, tz=timezone.utc)
    next_week = week + timedelta(days=7)
    return {
        'version': '4.0-research',
        'mode': 'READ_ONLY_PUBLIC_DATA',
        'generated_at_ms': now_ms,
        'generated_at_utc': generated,
        'decision_week_utc': week.isoformat(),
        'next_decision_utc': next_week.isoformat(),
        'decision_frequency': 'WEEKLY_SUNDAY_00_UTC',
        'markets': list(MARKETS),
        'sma_days': SMA_DAYS,
        'volatility_days': VOLATILITY_DAYS,
        'volatility_limit_pct': volatility_limit,
        'cash_weight_pct': round(frozen_cash_weight * 100.0, 2),
        'signals': [
            {
                **row,
                'close': round(float(row['close']), 8),
                'sma_65': round(float(row['sma_65']), 8),
                'distance_to_sma_pct': round((float(row['close']) / float(row['sma_65']) - 1.0) * 100.0, 3),
                'volatility_20d_pct': round(float(row['volatility_20d_pct']), 2),
                'target_weight_pct': round(float(row['target_weight']) * 100.0, 2),
                'measured_spread_pct': round(float(row['measured_spread_pct']), 4),
            }
            for row in frozen_rows
        ],
        'benchmarks': benchmarks,
        'minimum_research_weeks': MIN_RESEARCH_WEEKS,
        'research_progress_pct': round(min(100.0, benchmarks['weeks'] / MIN_RESEARCH_WEEKS * 100.0), 1),
        'orders_possible': False,
        'note': 'Prospectieve schaduwtest; geen koopadvies, geen orders en nog geen bewezen voordeel.',
    }


def _write_report(report: dict[str, object]) -> None:
    path = _report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def _load_report() -> dict[str, object] | None:
    path = _report_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def print_report(report: dict[str, object]) -> None:
    print('=== CRYPTOBOT v4 RESEARCH MODE | BTC + ETH | READ ONLY ===')
    print(f"UTC                 : {report.get('generated_at_utc', 'n/a')}")
    print(f"BESLISWEEK          : {report.get('decision_week_utc', 'n/a')}")
    print(f"VOLGEND BESLISMOMENT: {report.get('next_decision_utc', 'n/a')}")
    benchmarks = report.get('benchmarks', {})
    weeks = int(float(benchmarks.get('weeks', 0.0))) if isinstance(benchmarks, dict) else 0
    print(f"ONDERZOEKSHISTORIE  : {weeks}/{report.get('minimum_research_weeks', MIN_RESEARCH_WEEKS)} weken")
    print('BESLISSING           : ALTIJD ZELF | dit is een schaduwtest')
    print('ORDERS               : ONMOGELIJK | alleen publieke marktdata')

    print()
    print('=== BEVROREN WEEKBESLISSING ===')
    signals = report.get('signals', [])
    if not isinstance(signals, list) or not signals:
        print('geen geldige signalen')
    else:
        for row in signals:
            if not isinstance(row, dict):
                continue
            state = 'LONG SCHADUW' if row.get('long_signal') else 'USDC / GEEN POSITIE'
            print(
                f"{str(row.get('market','?')):9s} | {state:20s}"
                f" | gewicht {float(row.get('target_weight_pct',0.0)):5.1f}%"
                f" | koers {float(row.get('close',0.0)):.4f}"
            )
            print(
                f"   SMA65 {float(row.get('sma_65',0.0)):.4f}"
                f" | afstand {float(row.get('distance_to_sma_pct',0.0)):+.2f}%"
                f" | vol20 {float(row.get('volatility_20d_pct',0.0)):.1f}%"
                f" | spread gemeten {float(row.get('measured_spread_pct',0.0)):.3f}%"
            )
    print(f"USDC CASH            : {float(report.get('cash_weight_pct',100.0)):.1f}%")

    print()
    print('=== EERLIJKE VERGELIJKING SINDS START (START = 100) ===')
    if isinstance(benchmarks, dict):
        print(f"v4 na kosten         : {float(benchmarks.get('v4_index',100.0)):.2f}")
        print(f"v4 bij 2x kosten     : {float(benchmarks.get('v4_cost_stress_2x_index',100.0)):.2f}")
        print(f"v4 bij 3x kosten     : {float(benchmarks.get('v4_cost_stress_3x_index',100.0)):.2f}")
        print(f"BTC/ETH buy-and-hold : {float(benchmarks.get('buy_hold_50_50_index',100.0)):.2f}")
        print(f"wekelijkse DCA       : {float(benchmarks.get('weekly_dca_index',100.0)):.2f}")
        print(f"USDC cash            : {float(benchmarks.get('cash_usdc_index',100.0)):.2f}")
        print(f"v4 max drawdown      : {float(benchmarks.get('v4_max_drawdown_pct',0.0)):.2f}%")
    print()
    print('LET OP: pas na minimaal 26 weken kunnen we beoordelen of v4 verder onderzoek verdient.')


def main() -> int:
    parser = argparse.ArgumentParser(description='CryptoBot v4 Research Mode - read only')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    if args.status:
        report = _load_report()
        if report is None:
            print('=== CRYPTOBOT v4 RESEARCH MODE | READ ONLY ===')
            print('STATUS          : nog geen rapport')
            print(f'RAPPORT         : {_report_path()}')
            return 1
        print_report(report)
        return 0

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    poll_seconds = max(3600, int(os.getenv('V4_RESEARCH_POLL_SECONDS', '21600')))
    while not STOP:
        try:
            report = scan_once()
            _write_report(report)
            print_report(report)
        except Exception as exc:
            logger.exception('v4 research-cyclus mislukt: %s', exc)
            if args.once:
                return 2
        if args.once:
            return 0
        for _ in range(poll_seconds):
            if STOP:
                break
            time.sleep(1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

