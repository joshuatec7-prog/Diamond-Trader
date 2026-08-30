from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger('cryptobot_funding_basis_v3')
STOP = False

KRAKEN_FUTURES_URL = 'https://futures.kraken.com/derivatives/api/v3'
KRAKEN_SPOT_URL = 'https://api.kraken.com/0/public'
BITVAVO_URL = 'https://api.bitvavo.com/v2'

FUTURES_TAKER_FEE_PCT = 0.05
BITVAVO_USDC_TAKER_FEE_PCT = 0.05
CROSS_EXCHANGE_EXECUTION_BUFFER_PCT = 0.15
TOTAL_ROUNDTRIP_BUFFER_PCT = (
    2.0 * FUTURES_TAKER_FEE_PCT
    + 2.0 * BITVAVO_USDC_TAKER_FEE_PCT
    + CROSS_EXCHANGE_EXECUTION_BUFFER_PCT
)

# Bestaand BTC/ETH-bezit op Kraken hoeft voor een hedge niet opnieuw spot gekocht/verkocht te worden.
# Daarom rekenen we hier alleen futures heen+terug plus een extra uitvoeringsbuffer.
NATIVE_EXISTING_HOLDING_BUFFER_PCT = 2.0 * FUTURES_TAKER_FEE_PCT + 0.10

MIN_VOLUME_QUOTE_USD = 1_000_000.0
MAX_FUTURES_SPREAD_PCT = 0.12
MIN_POSITIVE_SHARE = 0.75
MIN_WATCH_SAMPLES_72H = 144
MIN_WATCH_SPAN_HOURS = 71.5
HISTORY_RETENTION_DAYS = 100
MAX_ABS_FUNDING_HOUR_PCT = 0.50
BASE_ALIASES = {'XBT': 'BTC', 'XDG': 'DOGE'}
KRAKEN_NATIVE_SPOT_PAIRS = {'BTC': 'XBTUSD', 'ETH': 'ETHUSD'}
DEFAULT_NATIVE_HOLDINGS = ('BTC', 'ETH')


def _data_path(filename: str) -> Path:
    data = Path('/var/data')
    if os.name != 'nt' and data.exists() and os.access(data, os.W_OK):
        return data / filename
    return Path('data') / filename


def _report_path() -> Path:
    raw = os.getenv('FUNDING_MONITOR_REPORT_PATH')
    return Path(raw) if raw else _data_path('cryptobot_funding_basis_v3.json')


def _db_path() -> Path:
    raw = os.getenv('FUNDING_MONITOR_DB_PATH')
    return Path(raw) if raw else _data_path('cryptobot_funding_basis_v3.db')


def _native_holdings() -> tuple[str, ...]:
    raw = os.getenv('KRAKEN_NATIVE_HOLDINGS', ','.join(DEFAULT_NATIVE_HOLDINGS))
    values = []
    for part in raw.split(','):
        base = part.strip().upper()
        if base in KRAKEN_NATIVE_SPOT_PAIRS and base not in values:
            values.append(base)
    return tuple(values)


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _pct_change(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0 if b > 0 else 0.0


def _relative_funding_pct(absolute_rate: float, index_price: float) -> float:
    if index_price <= 0:
        return 0.0
    return (absolute_rate / index_price) * 100.0


def _kraken_base(pair: str, symbol: str) -> str:
    raw = pair.split(':', 1)[0].upper().strip() if ':' in pair else ''
    if not raw and symbol.startswith('PF_') and symbol.endswith('USD'):
        raw = symbol[3:-3]
    return BASE_ALIASES.get(raw, raw)


def _fetch_bitvavo_usdc_markets(timeout: int = 10) -> dict[str, str]:
    response = requests.get(f'{BITVAVO_URL}/markets', timeout=timeout, headers={'Accept': 'application/json'})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError('ongeldig Bitvavo markets-antwoord')
    result: dict[str, str] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        if str(row.get('status', '')).lower() != 'trading':
            continue
        if str(row.get('quote', '')).upper() != 'USDC':
            continue
        base = str(row.get('base', '')).upper().strip()
        market = str(row.get('market', '')).upper().strip()
        if base and market:
            result[base] = market
    return result


def _fetch_futures_tickers(timeout: int = 10) -> list[dict[str, Any]]:
    response = requests.get(f'{KRAKEN_FUTURES_URL}/tickers', timeout=timeout, headers={'Accept': 'application/json'})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('result') not in {None, 'success'}:
        raise RuntimeError('ongeldig Kraken Futures tickers-antwoord')
    rows = payload.get('tickers', [])
    if not isinstance(rows, list):
        raise RuntimeError('Kraken Futures tickers ontbreken')
    return [row for row in rows if isinstance(row, dict)]


def _fetch_kraken_spot_ticker(pair: str, timeout: int = 10) -> dict[str, float]:
    response = requests.get(
        f'{KRAKEN_SPOT_URL}/Ticker', params={'pair': pair}, timeout=timeout, headers={'Accept': 'application/json'}
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('error'):
        raise RuntimeError(f'ongeldig Kraken spot ticker-antwoord voor {pair}')
    result = payload.get('result')
    if not isinstance(result, dict) or not result:
        raise RuntimeError(f'Kraken spot ticker ontbreekt voor {pair}')
    row = next(iter(result.values()))
    if not isinstance(row, dict):
        raise RuntimeError(f'ongeldig Kraken spot ticker-record voor {pair}')
    try:
        ask = float(row['a'][0])
        bid = float(row['b'][0])
        last = float(row['c'][0])
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f'ongeldige Kraken spot prijzen voor {pair}') from exc
    if min(ask, bid, last) <= 0 or ask < bid:
        raise RuntimeError(f'ongeldige Kraken spot bid/ask voor {pair}')
    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else 999.0
    return {'bid': bid, 'ask': ask, 'last': last, 'mid': mid, 'spread_pct': spread_pct}


def _db_connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS snapshots (
            generated_ms INTEGER NOT NULL,
            route_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            route_type TEXT NOT NULL,
            spot_market TEXT NOT NULL,
            roundtrip_buffer_pct REAL NOT NULL,
            funding_hour_pct REAL NOT NULL,
            predicted_funding_hour_pct REAL NOT NULL,
            basis_pct REAL NOT NULL,
            futures_spread_pct REAL NOT NULL,
            volume_quote REAL NOT NULL,
            open_interest REAL NOT NULL,
            PRIMARY KEY (generated_ms, route_id)
        )'''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_funding_v3_route_time ON snapshots(route_id, generated_ms)')
    conn.commit()
    return conn


def _window_history(rows: list[tuple[Any, ...]], cutoff_ms: int, suffix: str) -> dict[str, float]:
    selected = [row for row in rows if int(row[0]) >= cutoff_ms]
    if not selected:
        return {
            f'samples_{suffix}': 0.0,
            f'span_hours_{suffix}': 0.0,
            f'positive_share_{suffix}': 0.0,
            f'avg_funding_hour_pct_{suffix}': 0.0,
            f'sign_flips_{suffix}': 0.0,
            f'avg_basis_pct_{suffix}': 0.0,
            f'min_basis_pct_{suffix}': 0.0,
            f'max_basis_pct_{suffix}': 0.0,
        }
    funding = [float(row[1]) for row in selected]
    basis = [float(row[2]) for row in selected]
    nonzero_signs = [1 if value > 0.0 else -1 for value in funding if value != 0.0]
    sign_flips = sum(1 for left, right in zip(nonzero_signs, nonzero_signs[1:]) if left != right)
    span_hours = (int(selected[-1][0]) - int(selected[0][0])) / 3_600_000.0 if len(selected) > 1 else 0.0
    return {
        f'samples_{suffix}': float(len(selected)),
        f'span_hours_{suffix}': span_hours,
        f'positive_share_{suffix}': sum(1 for value in funding if value > 0.0) / len(funding),
        f'avg_funding_hour_pct_{suffix}': sum(funding) / len(funding),
        f'sign_flips_{suffix}': float(sign_flips),
        f'avg_basis_pct_{suffix}': sum(basis) / len(basis),
        f'min_basis_pct_{suffix}': min(basis),
        f'max_basis_pct_{suffix}': max(basis),
    }


def _history_summary(conn: sqlite3.Connection, route_id: str, now_ms: int) -> dict[str, float]:
    cutoff = now_ms - 90 * 24 * 60 * 60 * 1000
    rows = conn.execute(
        '''SELECT generated_ms, funding_hour_pct, basis_pct
           FROM snapshots WHERE route_id=? AND generated_ms>=? ORDER BY generated_ms''',
        (route_id, cutoff),
    ).fetchall()
    if not rows:
        return {
            'samples_24h': 0.0,
            'span_hours_24h': 0.0,
            'positive_share_24h': 0.0,
            'avg_funding_hour_pct_24h': 0.0,
            'samples_72h': 0.0,
            'span_hours_72h': 0.0,
            'positive_share_72h': 0.0,
            'avg_funding_hour_pct_72h': 0.0,
            'funding_decay_ratio_24h_vs_72h': 0.0,
        }
    result: dict[str, float] = {}
    windows = {
        '24h': 24 * 60 * 60 * 1000,
        '72h': 72 * 60 * 60 * 1000,
        '30d': 30 * 24 * 60 * 60 * 1000,
        '90d': 90 * 24 * 60 * 60 * 1000,
    }
    for suffix, duration_ms in windows.items():
        result.update(_window_history(rows, now_ms - duration_ms, suffix))
    avg_24h = result['avg_funding_hour_pct_24h']
    avg_72h = result['avg_funding_hour_pct_72h']
    result['funding_decay_ratio_24h_vs_72h'] = avg_24h / avg_72h if avg_72h > 0.0 else 0.0
    result['history_age_hours'] = (now_ms - int(rows[0][0])) / 3_600_000.0
    return result


def _stress_metrics(
    *,
    average_funding_hour_pct: float,
    roundtrip_buffer_pct: float,
) -> dict[str, float]:
    gross = max(0.0, average_funding_hour_pct) * 24.0 * 7.0
    return {
        'gross_7d_historical_pct': gross,
        'net_7d_historical_pct': gross - roundtrip_buffer_pct,
        'net_7d_cost_stress_2x_pct': gross - 2.0 * roundtrip_buffer_pct,
        'net_7d_basis_shock_1pct_pct': gross - roundtrip_buffer_pct - 1.0,
    }


def _attach_history(row: dict[str, Any], history: dict[str, float]) -> None:
    for suffix in ('24h', '72h', '30d', '90d'):
        row[f'samples_{suffix}'] = int(history.get(f'samples_{suffix}', 0.0))
        row[f'span_hours_{suffix}'] = round(history.get(f'span_hours_{suffix}', 0.0), 2)
        row[f'positive_share_{suffix}_pct'] = round(history.get(f'positive_share_{suffix}', 0.0) * 100.0, 1)
        row[f'avg_funding_hour_pct_{suffix}'] = round(history.get(f'avg_funding_hour_pct_{suffix}', 0.0), 7)
        row[f'sign_flips_{suffix}'] = int(history.get(f'sign_flips_{suffix}', 0.0))
        row[f'avg_basis_pct_{suffix}'] = round(history.get(f'avg_basis_pct_{suffix}', 0.0), 5)
        row[f'min_basis_pct_{suffix}'] = round(history.get(f'min_basis_pct_{suffix}', 0.0), 5)
        row[f'max_basis_pct_{suffix}'] = round(history.get(f'max_basis_pct_{suffix}', 0.0), 5)
    row['history_age_hours'] = round(history.get('history_age_hours', 0.0), 2)
    row['funding_decay_ratio_24h_vs_72h'] = round(
        history.get('funding_decay_ratio_24h_vs_72h', 0.0), 3
    )
    stress = _stress_metrics(
        average_funding_hour_pct=float(history.get('avg_funding_hour_pct_72h', 0.0)),
        roundtrip_buffer_pct=float(row.get('roundtrip_buffer_pct', TOTAL_ROUNDTRIP_BUFFER_PCT)),
    )
    for name, value in stress.items():
        row[name] = round(value, 4)


def _score_candidate(
    *,
    funding_hour_pct: float,
    predicted_hour_pct: float,
    spread_pct: float,
    volume_quote: float,
    basis_pct: float,
    history: dict[str, float],
    roundtrip_buffer_pct: float = TOTAL_ROUNDTRIP_BUFFER_PCT,
) -> tuple[float, str, float]:
    gross_7d_pct = max(0.0, funding_hour_pct) * 24.0 * 7.0
    net_7d_snapshot_pct = gross_7d_pct - roundtrip_buffer_pct
    samples = int(history.get('samples_72h', 0.0))
    span_hours = float(history.get('span_hours_72h', 0.0))
    positive_share = float(history.get('positive_share_72h', 0.0))
    avg_72h = float(history.get('avg_funding_hour_pct_72h', 0.0))
    decay_ratio = float(history.get('funding_decay_ratio_24h_vs_72h', 0.0))
    stress = _stress_metrics(
        average_funding_hour_pct=avg_72h,
        roundtrip_buffer_pct=roundtrip_buffer_pct,
    )

    score = 0.0
    if funding_hour_pct > 0.0:
        score += 20.0
    if predicted_hour_pct > 0.0:
        score += 15.0
    score += min(25.0, max(0.0, net_7d_snapshot_pct) / 1.5 * 25.0)
    score += min(15.0, max(0.0, volume_quote) / 50_000_000.0 * 15.0)
    score += 10.0 if spread_pct <= 0.05 else 5.0 if spread_pct <= MAX_FUTURES_SPREAD_PCT else 0.0
    if basis_pct > 0.0:
        score += min(5.0, basis_pct / 0.5 * 5.0)
    if samples >= MIN_WATCH_SAMPLES_72H and span_hours >= MIN_WATCH_SPAN_HOURS:
        score += min(10.0, positive_share * 10.0)
    score = round(max(0.0, min(100.0, score)), 1)

    stable_history = samples >= MIN_WATCH_SAMPLES_72H and span_hours >= MIN_WATCH_SPAN_HOURS
    action = 'VERZAMELEN'
    if (
        stable_history
        and funding_hour_pct > 0.0
        and predicted_hour_pct > 0.0
        and avg_72h > 0.0
        and positive_share >= MIN_POSITIVE_SHARE
        and stress['net_7d_historical_pct'] > 0.50
        and stress['net_7d_cost_stress_2x_pct'] > 0.0
        and decay_ratio >= 0.50
        and volume_quote >= MIN_VOLUME_QUOTE_USD
        and spread_pct <= MAX_FUTURES_SPREAD_PCT
    ):
        action = 'CARRY WATCH'
    if action == 'CARRY WATCH' and stress['net_7d_historical_pct'] >= 1.00 and positive_share >= 0.80:
        action = 'STERKE CARRY WATCH'
    return score, action, net_7d_snapshot_pct


def _future_record(ticker: dict[str, Any]) -> dict[str, Any] | None:
    symbol = str(ticker.get('symbol', '')).upper()
    tag = str(ticker.get('tag', '')).lower()
    if not symbol.startswith('PF_') or tag != 'perpetual' or bool(ticker.get('suspended', False)):
        return None
    pair = str(ticker.get('pair', symbol)).upper()
    base = _kraken_base(pair, symbol)
    mark = _finite(ticker.get('markPrice'))
    index = _finite(ticker.get('indexPrice'))
    bid = _finite(ticker.get('bid'))
    ask = _finite(ticker.get('ask'))
    if min(mark, index, bid, ask) <= 0 or ask < bid:
        return None
    funding_hour_pct = _relative_funding_pct(_finite(ticker.get('fundingRate')), index)
    predicted_hour_pct = _relative_funding_pct(_finite(ticker.get('fundingRatePrediction')), index)
    if abs(funding_hour_pct) > MAX_ABS_FUNDING_HOUR_PCT or abs(predicted_hour_pct) > MAX_ABS_FUNDING_HOUR_PCT:
        return None
    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else 999.0
    row = {
        'symbol': symbol,
        'pair': pair,
        'base': base,
        'mark': mark,
        'index': index,
        'funding_hour_pct': funding_hour_pct,
        'predicted_hour_pct': predicted_hour_pct,
        'futures_spread_pct': spread_pct,
        'volume_quote': _finite(ticker.get('volumeQuote')),
        'open_interest': _finite(ticker.get('openInterest')),
    }
    return row


def _build_row(
    *,
    future: dict[str, Any],
    route_id: str,
    route_type: str,
    spot_market: str,
    spot_reference: float,
    spot_spread_pct: float,
    roundtrip_buffer_pct: float,
    conn: sqlite3.Connection,
    now_ms: int,
) -> dict[str, Any]:
    basis_pct = _pct_change(float(future['mark']), spot_reference)
    history = _history_summary(conn, route_id, now_ms)
    score, action, net_7d = _score_candidate(
        funding_hour_pct=float(future['funding_hour_pct']),
        predicted_hour_pct=float(future['predicted_hour_pct']),
        spread_pct=float(future['futures_spread_pct']),
        volume_quote=float(future['volume_quote']),
        basis_pct=basis_pct,
        history=history,
        roundtrip_buffer_pct=roundtrip_buffer_pct,
    )
    row = {
        'route_id': route_id,
        'route_type': route_type,
        'symbol': future['symbol'],
        'pair': future['pair'],
        'base': future['base'],
        'spot_market': spot_market,
        'spot_reference': round(spot_reference, 8),
        'spot_spread_pct': round(spot_spread_pct, 5),
        'mark_price': round(float(future['mark']), 8),
        'index_price': round(float(future['index']), 8),
        'basis_pct': round(basis_pct, 5),
        'funding_hour_pct': round(float(future['funding_hour_pct']), 7),
        'predicted_funding_hour_pct': round(float(future['predicted_hour_pct']), 7),
        'gross_funding_7d_snapshot_pct': round(max(0.0, float(future['funding_hour_pct'])) * 168.0, 4),
        'net_7d_snapshot_pct': round(net_7d, 4),
        'futures_spread_pct': round(float(future['futures_spread_pct']), 5),
        'volume_quote': round(float(future['volume_quote']), 2),
        'open_interest': round(float(future['open_interest']), 4),
        'roundtrip_buffer_pct': round(roundtrip_buffer_pct, 4),
        'score': score,
        'action': action,
    }
    _attach_history(row, history)
    return row


def scan_once() -> dict[str, object]:
    now_ms = int(time.time() * 1000)
    usdc_markets = _fetch_bitvavo_usdc_markets()
    tickers = _fetch_futures_tickers()
    futures_by_base: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for ticker in tickers:
        record = _future_record(ticker)
        if record is None:
            continue
        futures_by_base[str(record['base'])] = record

    conn = _db_connect()
    rows: list[dict[str, Any]] = []
    try:
        # Route A: Bitvavo USDC spot + Kraken perpetual.
        for base, spot_market in sorted(usdc_markets.items()):
            future = futures_by_base.get(base)
            if future is None:
                continue
            rows.append(_build_row(
                future=future,
                route_id=f'BITVAVO_USDC_{base}',
                route_type='BITVAVO_USDC_KRAKEN_PERP',
                spot_market=spot_market,
                spot_reference=float(future['index']),
                spot_spread_pct=0.0,
                roundtrip_buffer_pct=TOTAL_ROUNDTRIP_BUFFER_PCT,
                conn=conn,
                now_ms=now_ms,
            ))

        # Route B: bestaand Kraken BTC/ETH-bezit + Kraken perpetual. Spot wordt niet opnieuw verhandeld.
        for base in _native_holdings():
            future = futures_by_base.get(base)
            if future is None:
                errors.append(f'Kraken native {base}: geen bruikbare perpetual')
                continue
            pair = KRAKEN_NATIVE_SPOT_PAIRS[base]
            try:
                spot = _fetch_kraken_spot_ticker(pair)
            except Exception as exc:
                errors.append(f'Kraken native {base}: {type(exc).__name__}: {exc}')
                continue
            rows.append(_build_row(
                future=future,
                route_id=f'KRAKEN_EXISTING_{base}',
                route_type='KRAKEN_EXISTING_HOLDING',
                spot_market=f'KRAKEN:{pair}',
                spot_reference=float(spot['mid']),
                spot_spread_pct=float(spot['spread_pct']),
                roundtrip_buffer_pct=NATIVE_EXISTING_HOLDING_BUFFER_PCT,
                conn=conn,
                now_ms=now_ms,
            ))

        for row in rows:
            conn.execute(
                '''INSERT OR REPLACE INTO snapshots
                   (generated_ms,route_id,symbol,route_type,spot_market,roundtrip_buffer_pct,
                    funding_hour_pct,predicted_funding_hour_pct,basis_pct,futures_spread_pct,
                    volume_quote,open_interest)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    now_ms, row['route_id'], row['symbol'], row['route_type'], row['spot_market'],
                    row['roundtrip_buffer_pct'], row['funding_hour_pct'], row['predicted_funding_hour_pct'],
                    row['basis_pct'], row['futures_spread_pct'], row['volume_quote'], row['open_interest'],
                ),
            )
        conn.commit()
        retention_cutoff = now_ms - HISTORY_RETENTION_DAYS * 24 * 60 * 60 * 1000
        conn.execute('DELETE FROM snapshots WHERE generated_ms<?', (retention_cutoff,))
        conn.commit()

        for row in rows:
            history = _history_summary(conn, str(row['route_id']), now_ms)
            score, action, net_7d = _score_candidate(
                funding_hour_pct=float(row['funding_hour_pct']),
                predicted_hour_pct=float(row['predicted_funding_hour_pct']),
                spread_pct=float(row['futures_spread_pct']),
                volume_quote=float(row['volume_quote']),
                basis_pct=float(row['basis_pct']),
                history=history,
                roundtrip_buffer_pct=float(row['roundtrip_buffer_pct']),
            )
            _attach_history(row, history)
            row['score'] = score
            row['action'] = action
            row['net_7d_snapshot_pct'] = round(net_7d, 4)
    finally:
        conn.close()

    def sort_key(row: dict[str, Any]) -> tuple[float, float, float, float]:
        action_rank = 2 if row['action'] == 'STERKE CARRY WATCH' else 1 if row['action'] == 'CARRY WATCH' else 0
        return (action_rank, float(row['score']), float(row['net_7d_snapshot_pct']), float(row['volume_quote']))

    rows.sort(key=sort_key, reverse=True)
    cross_rows = [row for row in rows if row['route_type'] == 'BITVAVO_USDC_KRAKEN_PERP']
    native_rows = [row for row in rows if row['route_type'] == 'KRAKEN_EXISTING_HOLDING']
    generated = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).isoformat()
    return {
        'version': '3.1',
        'mode': 'READ_ONLY_PUBLIC_DATA',
        'generated_at_ms': now_ms,
        'generated_at_utc': generated,
        'source': 'Kraken Futures + Kraken Spot + Bitvavo public markets',
        'bitvavo_usdc_markets': len(usdc_markets),
        'cross_exchange_routes': len(cross_rows),
        'native_existing_routes': len(native_rows),
        'native_holdings_configured': list(_native_holdings()),
        'watch_count': sum(1 for row in rows if 'CARRY WATCH' in str(row['action'])),
        'top5': rows[:5],
        'cross_exchange': cross_rows,
        'kraken_existing_holdings': native_rows,
        'errors': errors,
        'risk_check': 'MARGE EN BEURSRISICO NIET BEOORDEELD | altijd handmatig controleren',
        'note': 'CARRY WATCH vereist minimaal 72 uur stabiele historie en blijft een handmatige onderzoekskans; geen orders.',
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


def _print_row(index: int, row: dict[str, Any]) -> None:
    route = 'KRAKEN BESTAAND' if row.get('route_type') == 'KRAKEN_EXISTING_HOLDING' else 'BITVAVO↔KRAKEN'
    print(
        f"{index}. {str(row.get('base','?')):5s} | {route:15s} | {str(row.get('action','VERZAMELEN')):18s}"
        f" | score {float(row.get('score',0.0)):5.1f}/100"
    )
    print(
        f"   funding {float(row.get('funding_hour_pct',0.0)):+.6f}%/u"
        f" | voorspeld {float(row.get('predicted_funding_hour_pct',0.0)):+.6f}%/u"
        f" | basis {float(row.get('basis_pct',0.0)):+.3f}%"
        f" | fut.spread {float(row.get('futures_spread_pct',0.0)):.3f}%"
    )
    print(
        f"   buffer {float(row.get('roundtrip_buffer_pct',0.0)):.2f}%"
        f" | 7d snapshot netto {float(row.get('net_7d_snapshot_pct',0.0)):+.2f}%"
        f" | positief24h {float(row.get('positive_share_24h_pct',0.0)):.1f}%"
        f" | samples {int(row.get('samples_24h',0))} | span {float(row.get('span_hours_24h',0.0)):.1f}u"
    )
    print(
        f"   gem.72u {float(row.get('avg_funding_hour_pct_72h',0.0)):+.6f}%/u"
        f" | positief72u {float(row.get('positive_share_72h_pct',0.0)):.1f}%"
        f" | span {float(row.get('span_hours_72h',0.0)):.1f}u"
        f" | omslagen {int(row.get('sign_flips_72h',0))}"
        f" | verval {float(row.get('funding_decay_ratio_24h_vs_72h',0.0)):.2f}x"
    )
    print(
        f"   30d gem {float(row.get('avg_funding_hour_pct_30d',0.0)):+.6f}%/u"
        f" ({float(row.get('span_hours_30d',0.0))/24.0:.1f}d)"
        f" | 90d gem {float(row.get('avg_funding_hour_pct_90d',0.0)):+.6f}%/u"
        f" ({float(row.get('span_hours_90d',0.0))/24.0:.1f}d)"
    )
    print(
        f"   7d op gem.72u {float(row.get('net_7d_historical_pct',0.0)):+.2f}%"
        f" | bij 2x kosten {float(row.get('net_7d_cost_stress_2x_pct',0.0)):+.2f}%"
        f" | bij -1% basis {float(row.get('net_7d_basis_shock_1pct_pct',0.0)):+.2f}%"
        f" | basis72u {float(row.get('min_basis_pct_72h',0.0)):+.2f}%..{float(row.get('max_basis_pct_72h',0.0)):+.2f}%"
    )


def print_report(report: dict[str, object]) -> None:
    print('=== FUNDING / BASIS MONITOR v3 | READ ONLY ===')
    print(f"UTC                 : {report.get('generated_at_utc', 'n/a')}")
    print(f"BITVAVO USDC ROUTES : {report.get('cross_exchange_routes', 0)}")
    print(f"KRAKEN BESTAAND     : {report.get('native_existing_routes', 0)}")
    print(f"NATIVE WATCHLIST    : {', '.join(str(x) for x in report.get('native_holdings_configured', []))}")
    print(f"CARRY WATCH         : {report.get('watch_count', 0)}")
    print('HISTORIE-EIS        : minimaal 72 uur stabiel vóór een kanslabel')
    print(f"MARGE / BEURSRISICO : {report.get('risk_check', 'NIET BEOORDEELD')}")
    print('ORDERS              : ONMOGELIJK | alleen publieke marktdata')

    native = report.get('kraken_existing_holdings', [])
    print()
    print('=== KRAKEN BESTAAND BTC/ETH ===')
    if not isinstance(native, list) or not native:
        print('geen native routes')
    else:
        for index, row in enumerate(native, 1):
            if isinstance(row, dict):
                _print_row(index, row)

    cross = report.get('cross_exchange', [])
    print()
    print('=== BESTE BITVAVO USDC ↔ KRAKEN ===')
    if not isinstance(cross, list) or not cross:
        print('geen cross-exchange routes')
    else:
        for index, row in enumerate(cross[:5], 1):
            if isinstance(row, dict):
                _print_row(index, row)

    errors = report.get('errors', [])
    if isinstance(errors, list) and errors:
        print()
        print(f'WAARSCHUWINGEN       : {len(errors)}')
        for text in errors[:5]:
            print(f'  - {text}')
    print()
    print('LET OP: native route rekent alleen futures hedge-kosten omdat BTC/ETH al bestaan op Kraken.')
    print('CARRY WATCH vereist 72 uur historie, positieve 2x-kostenstress en handmatige risico-controle.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Funding/Basis Monitor v3 - read only')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    if args.status:
        report = _load_report()
        if report is None:
            print('=== FUNDING / BASIS MONITOR v3 | READ ONLY ===')
            print('STATUS          : nog geen rapport')
            print(f'RAPPORT         : {_report_path()}')
            return 1
        print_report(report)
        return 0

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    poll_seconds = max(300, int(os.getenv('FUNDING_MONITOR_POLL_SECONDS', '900')))
    while not STOP:
        try:
            report = scan_once()
            _write_report(report)
            print_report(report)
        except Exception as exc:
            logger.exception('funding-monitor cyclus mislukt: %s', exc)
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

