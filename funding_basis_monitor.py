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

logger = logging.getLogger('cryptobot_funding_basis_v4')
STOP = False

KRAKEN_FUTURES_URL = 'https://futures.kraken.com/derivatives/api/v3'
KRAKEN_SPOT_URL = 'https://api.kraken.com/0/public'
BITVAVO_URL = 'https://api.bitvavo.com/v2'
KRAKEN_FUTURES_BOOK_RESOURCE = 'orderbook'

FUTURES_TAKER_FEE_PCT = 0.05
BITVAVO_USDC_TAKER_FEE_PCT = 0.05
CROSS_EXCHANGE_EXECUTION_BUFFER_PCT = 0.15
# V4 meet altijd een concrete schaduworder tegen de publieke orderboeken. De cross-exchange
# route blijft fail-closed: hij verzamelt wel geldige metingen, maar kan geen kanslabel geven.
MEASUREMENT_GENERATION = 4
CROSS_EXCHANGE_WATCH_ENABLED = False
DEFAULT_SHADOW_NOTIONAL_USD = 200.0
MIN_SHADOW_NOTIONAL_USD = 25.0
MAX_SHADOW_NOTIONAL_USD = 10_000.0
ORDER_BOOK_DEPTH = 100
MAX_MEASUREMENT_SKEW_MS = 30_000
KRAKEN_STABLECOIN_PAIR = 'USDCUSD'
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
MIN_WATCH_SAMPLES_72H = 260
MIN_WATCH_SPAN_HOURS = 71.5
MAX_WATCH_GAP_MINUTES = 30.5
REPORT_STALE_SECONDS = 35 * 60
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


def _shadow_notional_usd() -> float:
    raw = os.getenv('FUNDING_SHADOW_NOTIONAL_USD', str(DEFAULT_SHADOW_NOTIONAL_USD))
    value = _finite(raw, -1.0)
    if not MIN_SHADOW_NOTIONAL_USD <= value <= MAX_SHADOW_NOTIONAL_USD:
        raise RuntimeError(
            'FUNDING_SHADOW_NOTIONAL_USD moet tussen '
            f'{MIN_SHADOW_NOTIONAL_USD:.0f} en {MAX_SHADOW_NOTIONAL_USD:.0f} liggen'
        )
    return value


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


def _parse_book_levels(levels: Any, *, side: str, size_multiplier: float = 1.0) -> list[tuple[float, float]]:
    if not isinstance(levels, list):
        raise RuntimeError(f'orderboek {side} ontbreekt')
    parsed: list[tuple[float, float]] = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = _finite(level[0], -1.0)
        size = _finite(level[1], -1.0) * size_multiplier
        if price > 0.0 and size > 0.0:
            parsed.append((price, size))
    if not parsed:
        raise RuntimeError(f'orderboek {side} bevat geen geldige niveaus')
    parsed.sort(key=lambda value: value[0], reverse=side == 'bids')
    return parsed


def _vwap_for_quote(levels: list[tuple[float, float]], notional_quote: float) -> tuple[float, float]:
    remaining = notional_quote
    total_quote = 0.0
    total_base = 0.0
    for price, available_base in levels:
        quote_at_level = price * available_base
        used_quote = min(remaining, quote_at_level)
        total_quote += used_quote
        total_base += used_quote / price
        remaining -= used_quote
        if remaining <= max(1e-9, notional_quote * 1e-9):
            break
    if remaining > max(1e-9, notional_quote * 1e-9) or total_base <= 0.0:
        available = sum(price * size for price, size in levels)
        raise RuntimeError(
            f'onvoldoende orderboekdiepte: {available:.2f} beschikbaar voor {notional_quote:.2f}'
        )
    return total_quote / total_base, sum(price * size for price, size in levels)


def _order_book_metrics(
    bids: Any,
    asks: Any,
    *,
    notional_quote: float,
    size_multiplier: float = 1.0,
) -> dict[str, float]:
    if notional_quote <= 0.0 or not math.isfinite(notional_quote):
        raise RuntimeError('orderboeknotional moet positief en eindig zijn')
    parsed_bids = _parse_book_levels(bids, side='bids', size_multiplier=size_multiplier)
    parsed_asks = _parse_book_levels(asks, side='asks', size_multiplier=size_multiplier)
    bid = parsed_bids[0][0]
    ask = parsed_asks[0][0]
    if ask < bid:
        raise RuntimeError('gekruist orderboek: ask ligt onder bid')
    sell_vwap, bid_depth_quote = _vwap_for_quote(parsed_bids, notional_quote)
    buy_vwap, ask_depth_quote = _vwap_for_quote(parsed_asks, notional_quote)
    mid = (bid + ask) / 2.0
    spread_pct = _pct_change(ask, bid)
    execution_spread_pct = _pct_change(buy_vwap, sell_vwap)
    return {
        'bid': bid,
        'ask': ask,
        'mid': mid,
        'spread_pct': spread_pct,
        'sell_vwap': sell_vwap,
        'buy_vwap': buy_vwap,
        'execution_spread_pct': execution_spread_pct,
        'bid_depth_quote': bid_depth_quote,
        'ask_depth_quote': ask_depth_quote,
        'notional_quote': notional_quote,
    }


def _fetch_bitvavo_book(market: str, notional_quote: float, timeout: int = 10) -> dict[str, float]:
    response = requests.get(
        f'{BITVAVO_URL}/{market}/book',
        params={'depth': ORDER_BOOK_DEPTH},
        timeout=timeout,
        headers={'Accept': 'application/json'},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f'ongeldig Bitvavo orderboek voor {market}')
    returned_market = str(payload.get('market', market)).upper()
    if returned_market != market.upper():
        raise RuntimeError(f'Bitvavo orderboek hoort bij {returned_market}, niet {market}')
    metrics = _order_book_metrics(payload.get('bids'), payload.get('asks'), notional_quote=notional_quote)
    metrics['captured_at_ms'] = float(int(time.time() * 1000))
    return metrics


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


def _fetch_futures_instruments(timeout: int = 10) -> dict[str, dict[str, Any]]:
    response = requests.get(
        f'{KRAKEN_FUTURES_URL}/instruments', timeout=timeout, headers={'Accept': 'application/json'}
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('result') not in {None, 'success'}:
        raise RuntimeError('ongeldig Kraken Futures instruments-antwoord')
    rows = payload.get('instruments', [])
    if not isinstance(rows, list):
        raise RuntimeError('Kraken Futures instruments ontbreken')
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get('symbol', '')).upper()
        if symbol:
            result[symbol] = row
    return result


def _fetch_kraken_futures_book(symbol: str, notional_quote: float, timeout: int = 10) -> dict[str, float]:
    response = requests.get(
        f'{KRAKEN_FUTURES_URL}/{KRAKEN_FUTURES_BOOK_RESOURCE}',
        params={'symbol': symbol},
        timeout=timeout,
        headers={'Accept': 'application/json'},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('result') not in {None, 'success'}:
        raise RuntimeError(f'ongeldig Kraken Futures orderboek voor {symbol}')
    book = payload.get('orderBook')
    if not isinstance(book, dict):
        raise RuntimeError(f'Kraken Futures orderboek ontbreekt voor {symbol}')
    metrics = _order_book_metrics(book.get('bids'), book.get('asks'), notional_quote=notional_quote)
    metrics['captured_at_ms'] = float(int(time.time() * 1000))
    return metrics


def _fetch_kraken_spot_book(pair: str, notional_quote: float, timeout: int = 10) -> dict[str, float]:
    response = requests.get(
        f'{KRAKEN_SPOT_URL}/Depth',
        params={'pair': pair, 'count': ORDER_BOOK_DEPTH},
        timeout=timeout,
        headers={'Accept': 'application/json'},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('error'):
        raise RuntimeError(f'ongeldig Kraken spot orderboek voor {pair}')
    result = payload.get('result')
    if not isinstance(result, dict) or not result:
        raise RuntimeError(f'Kraken spot orderboek ontbreekt voor {pair}')
    book = next(iter(result.values()))
    if not isinstance(book, dict):
        raise RuntimeError(f'ongeldig Kraken spot orderboekrecord voor {pair}')
    metrics = _order_book_metrics(book.get('bids'), book.get('asks'), notional_quote=notional_quote)
    metrics['captured_at_ms'] = float(int(time.time() * 1000))
    return metrics


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
            measurement_generation INTEGER NOT NULL DEFAULT 4,
            research_notional_usd REAL,
            measurement_started_at_ms INTEGER,
            measurement_completed_at_ms INTEGER,
            measurement_skew_ms INTEGER,
            roundtrip_buffer_pct REAL NOT NULL,
            funding_hour_pct REAL NOT NULL,
            predicted_funding_hour_pct REAL NOT NULL,
            basis_pct REAL NOT NULL,
            exit_basis_pct REAL,
            futures_spread_pct REAL NOT NULL,
            spot_bid REAL,
            spot_ask REAL,
            spot_buy_vwap REAL,
            spot_sell_vwap REAL,
            spot_bid_depth_quote REAL,
            spot_ask_depth_quote REAL,
            futures_bid REAL,
            futures_ask REAL,
            futures_buy_vwap REAL,
            futures_sell_vwap REAL,
            futures_bid_depth_quote REAL,
            futures_ask_depth_quote REAL,
            stablecoin_buy_usd REAL,
            stablecoin_sell_usd REAL,
            measurement_valid INTEGER NOT NULL DEFAULT 1,
            watch_eligible INTEGER NOT NULL DEFAULT 0,
            volume_quote REAL NOT NULL,
            open_interest REAL NOT NULL,
            PRIMARY KEY (generated_ms, route_id)
        )'''
    )
    existing_columns = {str(row[1]) for row in conn.execute('PRAGMA table_info(snapshots)').fetchall()}
    migrations = {
        'measurement_generation': 'INTEGER NOT NULL DEFAULT 3',
        'research_notional_usd': 'REAL',
        'measurement_started_at_ms': 'INTEGER',
        'measurement_completed_at_ms': 'INTEGER',
        'measurement_skew_ms': 'INTEGER',
        'exit_basis_pct': 'REAL',
        'spot_bid': 'REAL',
        'spot_ask': 'REAL',
        'spot_buy_vwap': 'REAL',
        'spot_sell_vwap': 'REAL',
        'spot_bid_depth_quote': 'REAL',
        'spot_ask_depth_quote': 'REAL',
        'futures_bid': 'REAL',
        'futures_ask': 'REAL',
        'futures_buy_vwap': 'REAL',
        'futures_sell_vwap': 'REAL',
        'futures_bid_depth_quote': 'REAL',
        'futures_ask_depth_quote': 'REAL',
        'stablecoin_buy_usd': 'REAL',
        'stablecoin_sell_usd': 'REAL',
        'measurement_valid': 'INTEGER NOT NULL DEFAULT 1',
        'watch_eligible': 'INTEGER NOT NULL DEFAULT 0',
    }
    for name, definition in migrations.items():
        if name not in existing_columns:
            conn.execute(f'ALTER TABLE snapshots ADD COLUMN {name} {definition}')
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
            f'avg_roundtrip_buffer_pct_{suffix}': 0.0,
            f'max_roundtrip_buffer_pct_{suffix}': 0.0,
            f'max_gap_minutes_{suffix}': 0.0,
        }
    funding = [float(row[1]) for row in selected]
    basis = [float(row[2]) for row in selected]
    buffers = [float(row[3]) for row in selected if len(row) > 3 and row[3] is not None]
    nonzero_signs = [1 if value > 0.0 else -1 for value in funding if value != 0.0]
    sign_flips = sum(1 for left, right in zip(nonzero_signs, nonzero_signs[1:]) if left != right)
    span_hours = (int(selected[-1][0]) - int(selected[0][0])) / 3_600_000.0 if len(selected) > 1 else 0.0
    gaps = [
        (int(right[0]) - int(left[0])) / 60_000.0
        for left, right in zip(selected, selected[1:])
    ]
    return {
        f'samples_{suffix}': float(len(selected)),
        f'span_hours_{suffix}': span_hours,
        f'positive_share_{suffix}': sum(1 for value in funding if value > 0.0) / len(funding),
        f'avg_funding_hour_pct_{suffix}': sum(funding) / len(funding),
        f'sign_flips_{suffix}': float(sign_flips),
        f'avg_basis_pct_{suffix}': sum(basis) / len(basis),
        f'min_basis_pct_{suffix}': min(basis),
        f'max_basis_pct_{suffix}': max(basis),
        f'avg_roundtrip_buffer_pct_{suffix}': sum(buffers) / len(buffers) if buffers else 0.0,
        f'max_roundtrip_buffer_pct_{suffix}': max(buffers) if buffers else 0.0,
        f'max_gap_minutes_{suffix}': max(gaps) if gaps else 0.0,
    }


def _history_summary(conn: sqlite3.Connection, route_id: str, now_ms: int) -> dict[str, float]:
    cutoff = now_ms - 90 * 24 * 60 * 60 * 1000
    rows = conn.execute(
        '''SELECT generated_ms, funding_hour_pct, basis_pct, roundtrip_buffer_pct
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
    stress_buffer_pct: float | None = None,
) -> dict[str, float]:
    gross = max(0.0, average_funding_hour_pct) * 24.0 * 7.0
    stressed_buffer = max(
        roundtrip_buffer_pct,
        roundtrip_buffer_pct if stress_buffer_pct is None else stress_buffer_pct,
    )
    return {
        'gross_7d_historical_pct': gross,
        'net_7d_historical_pct': gross - roundtrip_buffer_pct,
        'net_7d_cost_stress_2x_pct': gross - 2.0 * stressed_buffer,
        'net_7d_basis_shock_1pct_pct': gross - stressed_buffer - 1.0,
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
        row[f'avg_roundtrip_buffer_pct_{suffix}'] = round(
            history.get(f'avg_roundtrip_buffer_pct_{suffix}', 0.0), 5
        )
        row[f'max_roundtrip_buffer_pct_{suffix}'] = round(
            history.get(f'max_roundtrip_buffer_pct_{suffix}', 0.0), 5
        )
        row[f'max_gap_minutes_{suffix}'] = round(history.get(f'max_gap_minutes_{suffix}', 0.0), 2)
    row['history_age_hours'] = round(history.get('history_age_hours', 0.0), 2)
    row['funding_decay_ratio_24h_vs_72h'] = round(
        history.get('funding_decay_ratio_24h_vs_72h', 0.0), 3
    )
    current_buffer = float(row.get('roundtrip_buffer_pct', TOTAL_ROUNDTRIP_BUFFER_PCT))
    average_buffer = float(history.get('avg_roundtrip_buffer_pct_72h', 0.0)) or current_buffer
    stress_buffer = max(
        current_buffer,
        float(history.get('max_roundtrip_buffer_pct_72h', 0.0)),
        average_buffer,
    )
    row['historical_average_buffer_pct_72h'] = round(average_buffer, 5)
    row['historical_stress_buffer_pct_72h'] = round(stress_buffer, 5)
    stress = _stress_metrics(
        average_funding_hour_pct=float(history.get('avg_funding_hour_pct_72h', 0.0)),
        roundtrip_buffer_pct=average_buffer,
        stress_buffer_pct=stress_buffer,
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
    watch_enabled: bool = True,
) -> tuple[float, str, float]:
    gross_7d_pct = max(0.0, funding_hour_pct) * 24.0 * 7.0
    net_7d_snapshot_pct = gross_7d_pct - roundtrip_buffer_pct
    samples = int(history.get('samples_72h', 0.0))
    span_hours = float(history.get('span_hours_72h', 0.0))
    positive_share = float(history.get('positive_share_72h', 0.0))
    avg_72h = float(history.get('avg_funding_hour_pct_72h', 0.0))
    decay_ratio = float(history.get('funding_decay_ratio_24h_vs_72h', 0.0))
    max_gap_minutes = float(history.get('max_gap_minutes_72h', float('inf')))
    average_buffer = float(history.get('avg_roundtrip_buffer_pct_72h', 0.0)) or roundtrip_buffer_pct
    stress_buffer = max(
        roundtrip_buffer_pct,
        float(history.get('max_roundtrip_buffer_pct_72h', 0.0)),
        average_buffer,
    )
    stress = _stress_metrics(
        average_funding_hour_pct=avg_72h,
        roundtrip_buffer_pct=average_buffer,
        stress_buffer_pct=stress_buffer,
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
    if (
        samples >= MIN_WATCH_SAMPLES_72H
        and span_hours >= MIN_WATCH_SPAN_HOURS
        and max_gap_minutes <= MAX_WATCH_GAP_MINUTES
    ):
        score += min(10.0, positive_share * 10.0)
    score = round(max(0.0, min(100.0, score)), 1)

    stable_history = (
        samples >= MIN_WATCH_SAMPLES_72H
        and span_hours >= MIN_WATCH_SPAN_HOURS
        and max_gap_minutes <= MAX_WATCH_GAP_MINUTES
    )
    action = 'VERZAMELEN' if watch_enabled else 'CROSS GEBLOKKEERD'
    if (
        watch_enabled
        and
        stable_history
        and funding_hour_pct > 0.0
        and predicted_hour_pct > 0.0
        and avg_72h > 0.0
        and positive_share >= MIN_POSITIVE_SHARE
        and stress['net_7d_historical_pct'] > 0.50
        and stress['net_7d_cost_stress_2x_pct'] > 0.0
        and stress['net_7d_basis_shock_1pct_pct'] > 0.0
        and decay_ratio >= 0.50
        and volume_quote >= MIN_VOLUME_QUOTE_USD
        and spread_pct <= MAX_FUTURES_SPREAD_PCT
    ):
        action = 'CARRY WATCH'
    if action == 'CARRY WATCH' and stress['net_7d_historical_pct'] >= 1.00 and positive_share >= 0.80:
        action = 'STERKE CARRY WATCH'
    return score, action, net_7d_snapshot_pct


def _future_record(
    ticker: dict[str, Any], instrument: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    symbol = str(ticker.get('symbol', '')).upper()
    tag = str(ticker.get('tag', '')).lower()
    if not symbol.startswith('PF_') or tag != 'perpetual' or bool(ticker.get('suspended', False)):
        return None
    if instrument is not None:
        if str(instrument.get('symbol', '')).upper() != symbol:
            return None
        if str(instrument.get('type', '')).lower() != 'flexible_futures':
            return None
        if not bool(instrument.get('tradeable', False)) or bool(instrument.get('isExpired', False)):
            return None
        # PF-orderboeken gebruiken non-contract units. Een afwijkende contractSize wordt
        # niet stilzwijgend omgerekend: dan stoppen we de meting veilig.
        if not math.isclose(_finite(instrument.get('contractSize'), -1.0), 1.0):
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
    spot_book: dict[str, float],
    futures_book: dict[str, float],
    fixed_roundtrip_buffer_pct: float,
    watch_enabled: bool,
    conn: sqlite3.Connection,
    now_ms: int,
    stablecoin_book: dict[str, float] | None = None,
) -> dict[str, Any]:
    books = [spot_book, futures_book]
    if stablecoin_book is not None:
        books.append(stablecoin_book)
    capture_times = [int(_finite(book.get('captured_at_ms'), float(now_ms))) for book in books]
    measurement_started_at_ms = min(capture_times)
    measurement_completed_at_ms = max(capture_times)
    measurement_skew_ms = measurement_completed_at_ms - measurement_started_at_ms
    if measurement_skew_ms > MAX_MEASUREMENT_SKEW_MS:
        raise RuntimeError(
            f'orderboeken liggen {measurement_skew_ms} ms uiteen; maximum is {MAX_MEASUREMENT_SKEW_MS} ms'
        )
    if stablecoin_book is None:
        stablecoin_buy_usd = 1.0
        stablecoin_sell_usd = 1.0
        stablecoin_execution_spread_pct = 0.0
        spot_entry_reference = float(spot_book['mid'])
        spot_exit_reference = float(spot_book['mid'])
    else:
        stablecoin_buy_usd = float(stablecoin_book['buy_vwap'])
        stablecoin_sell_usd = float(stablecoin_book['sell_vwap'])
        stablecoin_execution_spread_pct = float(stablecoin_book['execution_spread_pct'])
        spot_entry_reference = float(spot_book['buy_vwap']) * stablecoin_buy_usd
        spot_exit_reference = float(spot_book['sell_vwap']) * stablecoin_sell_usd

    futures_entry_reference = float(futures_book['sell_vwap'])
    futures_exit_reference = float(futures_book['buy_vwap'])
    basis_pct = _pct_change(futures_entry_reference, spot_entry_reference)
    exit_basis_pct = _pct_change(futures_exit_reference, spot_exit_reference)
    dynamic_execution_pct = float(futures_book['execution_spread_pct'])
    if stablecoin_book is not None:
        dynamic_execution_pct += float(spot_book['execution_spread_pct'])
        dynamic_execution_pct += stablecoin_execution_spread_pct
    roundtrip_buffer_pct = fixed_roundtrip_buffer_pct + dynamic_execution_pct
    history = _history_summary(conn, route_id, now_ms)
    score, action, net_7d = _score_candidate(
        funding_hour_pct=float(future['funding_hour_pct']),
        predicted_hour_pct=float(future['predicted_hour_pct']),
        spread_pct=float(futures_book['execution_spread_pct']),
        volume_quote=float(future['volume_quote']),
        basis_pct=basis_pct,
        history=history,
        roundtrip_buffer_pct=roundtrip_buffer_pct,
        watch_enabled=watch_enabled,
    )
    row = {
        'measurement_generation': MEASUREMENT_GENERATION,
        'measurement_valid': True,
        'watch_eligible': watch_enabled,
        'route_id': route_id,
        'route_type': route_type,
        'symbol': future['symbol'],
        'pair': future['pair'],
        'base': future['base'],
        'spot_market': spot_market,
        'research_notional_usd': round(float(futures_book['notional_quote']), 2),
        'measurement_started_at_ms': measurement_started_at_ms,
        'measurement_completed_at_ms': measurement_completed_at_ms,
        'measurement_skew_ms': measurement_skew_ms,
        'spot_reference': round(spot_entry_reference, 8),
        'spot_bid': round(float(spot_book['bid']), 8),
        'spot_ask': round(float(spot_book['ask']), 8),
        'spot_buy_vwap': round(float(spot_book['buy_vwap']), 8),
        'spot_sell_vwap': round(float(spot_book['sell_vwap']), 8),
        'spot_spread_pct': round(float(spot_book['spread_pct']), 5),
        'spot_execution_spread_pct': round(float(spot_book['execution_spread_pct']), 5),
        'spot_bid_depth_quote': round(float(spot_book['bid_depth_quote']), 2),
        'spot_ask_depth_quote': round(float(spot_book['ask_depth_quote']), 2),
        'mark_price': round(float(future['mark']), 8),
        'index_price': round(float(future['index']), 8),
        'futures_bid': round(float(futures_book['bid']), 8),
        'futures_ask': round(float(futures_book['ask']), 8),
        'futures_sell_vwap': round(futures_entry_reference, 8),
        'futures_buy_vwap': round(futures_exit_reference, 8),
        'futures_bid_depth_quote': round(float(futures_book['bid_depth_quote']), 2),
        'futures_ask_depth_quote': round(float(futures_book['ask_depth_quote']), 2),
        'futures_execution_spread_pct': round(float(futures_book['execution_spread_pct']), 5),
        'stablecoin_pair': KRAKEN_STABLECOIN_PAIR if stablecoin_book is not None else None,
        'stablecoin_buy_usd': round(stablecoin_buy_usd, 8),
        'stablecoin_sell_usd': round(stablecoin_sell_usd, 8),
        'stablecoin_execution_spread_pct': round(stablecoin_execution_spread_pct, 5),
        'basis_pct': round(basis_pct, 5),
        'entry_basis_pct': round(basis_pct, 5),
        'exit_basis_pct': round(exit_basis_pct, 5),
        'funding_hour_pct': round(float(future['funding_hour_pct']), 7),
        'predicted_funding_hour_pct': round(float(future['predicted_hour_pct']), 7),
        'gross_funding_7d_snapshot_pct': round(max(0.0, float(future['funding_hour_pct'])) * 168.0, 4),
        'net_7d_snapshot_pct': round(net_7d, 4),
        'futures_spread_pct': round(float(future['futures_spread_pct']), 5),
        'volume_quote': round(float(future['volume_quote']), 2),
        'open_interest': round(float(future['open_interest']), 4),
        'fixed_roundtrip_buffer_pct': round(fixed_roundtrip_buffer_pct, 4),
        'dynamic_execution_buffer_pct': round(dynamic_execution_pct, 4),
        'roundtrip_buffer_pct': round(roundtrip_buffer_pct, 4),
        'score': score,
        'action': action,
    }
    _attach_history(row, history)
    return row


def _insert_snapshot(conn: sqlite3.Connection, now_ms: int, row: dict[str, Any]) -> None:
    columns = (
        'generated_ms', 'route_id', 'symbol', 'route_type', 'spot_market',
        'measurement_generation', 'research_notional_usd', 'measurement_started_at_ms',
        'measurement_completed_at_ms', 'measurement_skew_ms', 'roundtrip_buffer_pct',
        'funding_hour_pct', 'predicted_funding_hour_pct', 'basis_pct', 'exit_basis_pct',
        'futures_spread_pct', 'spot_bid', 'spot_ask', 'spot_buy_vwap', 'spot_sell_vwap',
        'spot_bid_depth_quote', 'spot_ask_depth_quote', 'futures_bid', 'futures_ask',
        'futures_buy_vwap', 'futures_sell_vwap', 'futures_bid_depth_quote',
        'futures_ask_depth_quote', 'stablecoin_buy_usd', 'stablecoin_sell_usd',
        'measurement_valid', 'watch_eligible', 'volume_quote', 'open_interest',
    )
    values = (
        now_ms, row['route_id'], row['symbol'], row['route_type'], row['spot_market'],
        row['measurement_generation'], row['research_notional_usd'], row['measurement_started_at_ms'],
        row['measurement_completed_at_ms'], row['measurement_skew_ms'], row['roundtrip_buffer_pct'],
        row['funding_hour_pct'], row['predicted_funding_hour_pct'], row['basis_pct'],
        row['exit_basis_pct'], row['futures_spread_pct'], row['spot_bid'], row['spot_ask'],
        row['spot_buy_vwap'], row['spot_sell_vwap'], row['spot_bid_depth_quote'],
        row['spot_ask_depth_quote'], row['futures_bid'], row['futures_ask'],
        row['futures_buy_vwap'], row['futures_sell_vwap'], row['futures_bid_depth_quote'],
        row['futures_ask_depth_quote'], row['stablecoin_buy_usd'], row['stablecoin_sell_usd'],
        int(bool(row['measurement_valid'])), int(bool(row['watch_eligible'])),
        row['volume_quote'], row['open_interest'],
    )
    placeholders = ','.join('?' for _ in columns)
    conn.execute(
        f"INSERT OR REPLACE INTO snapshots ({','.join(columns)}) VALUES ({placeholders})",
        values,
    )


def scan_once() -> dict[str, object]:
    scan_started_ms = int(time.time() * 1000)
    now_ms = scan_started_ms
    notional_usd = _shadow_notional_usd()
    usdc_markets = _fetch_bitvavo_usdc_markets()
    tickers = _fetch_futures_tickers()
    instruments = _fetch_futures_instruments()
    futures_by_base: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    error_keys: set[str] = set()

    def add_error(message: str) -> None:
        if message not in error_keys:
            error_keys.add(message)
            errors.append(message)

    for ticker in tickers:
        symbol = str(ticker.get('symbol', '')).upper()
        instrument = instruments.get(symbol)
        if instrument is None:
            continue
        record = _future_record(ticker, instrument)
        if record is None:
            continue
        futures_by_base[str(record['base'])] = record

    conn = _db_connect()
    rows: list[dict[str, Any]] = []
    cross_expected = sum(1 for base in usdc_markets if base in futures_by_base)
    try:
        # Route A: echte Bitvavo USDC-uitvoeringsprijzen + Kraken perpetual-orderboek + USDC/USD.
        # Deze route meet opnieuw vanaf nul en blijft geblokkeerd voor kanslabels.
        for base, spot_market in sorted(usdc_markets.items()):
            future = futures_by_base.get(base)
            if future is None:
                continue
            try:
                spot_depth = _fetch_bitvavo_book(spot_market, notional_usd)
                future_depth = _fetch_kraken_futures_book(str(future['symbol']), notional_usd)
                stablecoin_depth = _fetch_kraken_spot_book(KRAKEN_STABLECOIN_PAIR, notional_usd)
                row = _build_row(
                    future=future,
                    route_id=f'BITVAVO_USDC_EXEC_V4_{base}',
                    route_type='BITVAVO_USDC_KRAKEN_PERP',
                    spot_market=spot_market,
                    spot_book=spot_depth,
                    futures_book=future_depth,
                    stablecoin_book=stablecoin_depth,
                    fixed_roundtrip_buffer_pct=TOTAL_ROUNDTRIP_BUFFER_PCT,
                    watch_enabled=CROSS_EXCHANGE_WATCH_ENABLED,
                    conn=conn,
                    now_ms=now_ms,
                )
            except Exception as exc:
                add_error(f'Cross {spot_market}: {type(exc).__name__}: {exc}')
                continue
            rows.append(row)

        # Route B: bestaand Kraken BTC/ETH-bezit. Alleen de futureshedge wordt als trade gemeten.
        for base in _native_holdings():
            future = futures_by_base.get(base)
            if future is None:
                add_error(f'Kraken native {base}: geen gevalideerde flexible perpetual')
                continue
            pair = KRAKEN_NATIVE_SPOT_PAIRS[base]
            try:
                future_depth = _fetch_kraken_futures_book(str(future['symbol']), notional_usd)
                spot_depth = _fetch_kraken_spot_book(pair, notional_usd)
                row = _build_row(
                    future=future,
                    route_id=f'KRAKEN_EXISTING_EXEC_V4_{base}',
                    route_type='KRAKEN_EXISTING_HOLDING',
                    spot_market=f'KRAKEN:{pair}',
                    spot_book=spot_depth,
                    futures_book=future_depth,
                    fixed_roundtrip_buffer_pct=NATIVE_EXISTING_HOLDING_BUFFER_PCT,
                    watch_enabled=True,
                    conn=conn,
                    now_ms=now_ms,
                )
            except Exception as exc:
                add_error(f'Kraken native {base}: {type(exc).__name__}: {exc}')
                continue
            rows.append(row)

        for row in rows:
            _insert_snapshot(conn, now_ms, row)
        conn.commit()
        retention_cutoff = now_ms - HISTORY_RETENTION_DAYS * 24 * 60 * 60 * 1000
        conn.execute('DELETE FROM snapshots WHERE generated_ms<?', (retention_cutoff,))
        conn.commit()

        for row in rows:
            history = _history_summary(conn, str(row['route_id']), now_ms)
            score, action, net_7d = _score_candidate(
                funding_hour_pct=float(row['funding_hour_pct']),
                predicted_hour_pct=float(row['predicted_funding_hour_pct']),
                spread_pct=float(row['futures_execution_spread_pct']),
                volume_quote=float(row['volume_quote']),
                basis_pct=float(row['basis_pct']),
                history=history,
                roundtrip_buffer_pct=float(row['roundtrip_buffer_pct']),
                watch_enabled=bool(row['watch_eligible']),
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
    scan_duration_ms = max(0, int(time.time() * 1000) - scan_started_ms)
    return {
        'version': '4.1',
        'measurement_generation': MEASUREMENT_GENERATION,
        'mode': 'READ_ONLY_PUBLIC_DATA',
        'generated_at_ms': now_ms,
        'generated_at_utc': generated,
        'scan_duration_ms': scan_duration_ms,
        'source': 'Kraken Futures/Spot L2-orderboeken + Bitvavo L2-orderboeken',
        'research_notional_usd': notional_usd,
        'max_measurement_skew_ms': MAX_MEASUREMENT_SKEW_MS,
        'history_requirements': {
            'minimum_samples_72h': MIN_WATCH_SAMPLES_72H,
            'minimum_span_hours': MIN_WATCH_SPAN_HOURS,
            'maximum_gap_minutes': MAX_WATCH_GAP_MINUTES,
            'basis_shock_must_be_positive': True,
            'historical_cost_method': 'gemiddelde kosten; 2x-stress met hoogste 72u-kosten',
        },
        'bitvavo_usdc_markets': len(usdc_markets),
        'cross_exchange_routes_expected': cross_expected,
        'cross_exchange_routes': len(cross_rows),
        'cross_exchange_routes_skipped': max(0, cross_expected - len(cross_rows)),
        'cross_exchange_watch_enabled': CROSS_EXCHANGE_WATCH_ENABLED,
        'cross_exchange_status': 'MEETLAAG ALLEEN | kanslabels fail-closed geblokkeerd',
        'native_existing_routes': len(native_rows),
        'native_holdings_configured': list(_native_holdings()),
        'watch_count': sum(1 for row in rows if 'CARRY WATCH' in str(row['action'])),
        'top5': rows[:5],
        'cross_exchange': cross_rows,
        'kraken_existing_holdings': native_rows,
        'errors': errors,
        'risk_check': 'MARGE EN BEURSRISICO NIET BEOORDEELD | altijd handmatig controleren',
        'history_reset': 'V4 route-id: oude v3 indexreferenties tellen niet mee; 72-uursmeting begint opnieuw.',
        'note': 'Alleen native CARRY WATCH kan na 72 uur ontstaan; cross-exchange blijft geblokkeerd; geen orders.',
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


def _report_age_seconds(report: dict[str, object], now_ms: int | None = None) -> float:
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        generated_ms = int(report.get('generated_at_ms', 0))
    except (TypeError, ValueError, OverflowError):
        return float('inf')
    if generated_ms <= 0:
        return float('inf')
    return max(0.0, (current_ms - generated_ms) / 1000.0)


def _report_is_stale(report: dict[str, object], now_ms: int | None = None) -> bool:
    return _report_age_seconds(report, now_ms) > REPORT_STALE_SECONDS


def _print_row(index: int, row: dict[str, Any]) -> None:
    route = 'KRAKEN BESTAAND' if row.get('route_type') == 'KRAKEN_EXISTING_HOLDING' else 'BITVAVO↔KRAKEN'
    print(
        f"{index}. {str(row.get('base','?')):5s} | {route:15s} | {str(row.get('action','VERZAMELEN')):20s}"
        f" | score {float(row.get('score',0.0)):5.1f}/100"
    )
    print(
        f"   funding {float(row.get('funding_hour_pct',0.0)):+.6f}%/u"
        f" | voorspeld {float(row.get('predicted_funding_hour_pct',0.0)):+.6f}%/u"
        f" | entrybasis {float(row.get('entry_basis_pct',row.get('basis_pct',0.0))):+.3f}%"
        f" | exitbasis {float(row.get('exit_basis_pct',0.0)):+.3f}%"
    )
    print(
        f"   buffer {float(row.get('roundtrip_buffer_pct',0.0)):.2f}%"
        f" (vast {float(row.get('fixed_roundtrip_buffer_pct',0.0)):.2f}%"
        f" + L2 {float(row.get('dynamic_execution_buffer_pct',0.0)):.2f}%)"
        f" | 7d snapshot netto {float(row.get('net_7d_snapshot_pct',0.0)):+.2f}%"
    )
    print(
        f"   schaduw ${float(row.get('research_notional_usd',0.0)):.0f}"
        f" | spot L2 {float(row.get('spot_execution_spread_pct',0.0)):.3f}%"
        f" | futures L2 {float(row.get('futures_execution_spread_pct',0.0)):.3f}%"
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
    print(
        f"   kosten72u gem/max {float(row.get('avg_roundtrip_buffer_pct_72h',0.0)):.2f}%/"
        f"{float(row.get('max_roundtrip_buffer_pct_72h',0.0)):.2f}%"
        f" | grootste meetpauze {float(row.get('max_gap_minutes_72h',0.0)):.1f} min"
    )


def print_report(report: dict[str, object]) -> None:
    age_seconds = _report_age_seconds(report)
    freshness = 'VEROUDERD' if age_seconds > REPORT_STALE_SECONDS else 'ACTUEEL'
    print('=== FUNDING / BASIS MONITOR v4.1 | STRICT HISTORY | READ ONLY ===')
    print(f"UTC                 : {report.get('generated_at_utc', 'n/a')}")
    print(f"RAPPORTSTATUS       : {freshness} | leeftijd {age_seconds/60.0:.1f} min")
    print(f"BITVAVO USDC ROUTES : {report.get('cross_exchange_routes', 0)}")
    print(f"KRAKEN BESTAAND     : {report.get('native_existing_routes', 0)}")
    print(f"NATIVE WATCHLIST    : {', '.join(str(x) for x in report.get('native_holdings_configured', []))}")
    print(f"CARRY WATCH         : {report.get('watch_count', 0)}")
    print(f"SCHADUWOMVANG       : ${float(report.get('research_notional_usd', 0.0)):.0f} per leg")
    print(f"CROSS LABELS        : {'AAN' if report.get('cross_exchange_watch_enabled') else 'GEBLOKKEERD'}")
    print('HISTORIE-EIS        : 72 uur, ≥260 samples, geen meetpauze >30,5 min')
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
    print('LET OP: v4.1 gebruikt L2-VWAP en gemiddelde/hoogste uitvoeringskosten uit de hele 72 uur.')
    print('Native rekent alleen de futureshedge; cross-labels blijven fail-closed geblokkeerd.')
    print('CARRY WATCH vereist ook positieve 2x-kostenstress én positieve -1%-basisstresstest.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Funding/Basis Monitor v4 - executable L2, read only')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    if args.status:
        report = _load_report()
        if report is None:
            print('=== FUNDING / BASIS MONITOR v4.1 | STRICT HISTORY | READ ONLY ===')
            print('STATUS          : nog geen rapport')
            print(f'RAPPORT         : {_report_path()}')
            return 1
        print_report(report)
        return 2 if _report_is_stale(report) else 0

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

