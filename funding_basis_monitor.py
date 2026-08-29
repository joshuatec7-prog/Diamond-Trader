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

logger = logging.getLogger('cryptobot_funding_basis_v1')
STOP = False

KRAKEN_FUTURES_URL = 'https://futures.kraken.com/derivatives/api/v3'
FUTURES_TAKER_FEE_PCT = 0.05
BITVAVO_USDC_TAKER_FEE_PCT = 0.05
EXECUTION_BUFFER_PCT = 0.15
TOTAL_ROUNDTRIP_BUFFER_PCT = (
    2.0 * FUTURES_TAKER_FEE_PCT
    + 2.0 * BITVAVO_USDC_TAKER_FEE_PCT
    + EXECUTION_BUFFER_PCT
)
MIN_VOLUME_QUOTE_USD = 1_000_000.0
MAX_FUTURES_SPREAD_PCT = 0.12
MIN_HISTORY_SAMPLES = 24
MIN_POSITIVE_SHARE = 0.75


def _data_path(filename: str) -> Path:
    data = Path('/var/data')
    if data.exists() and os.access(data, os.W_OK):
        return data / filename
    return Path('data') / filename


def _report_path() -> Path:
    raw = os.getenv('FUNDING_MONITOR_REPORT_PATH')
    return Path(raw) if raw else _data_path('cryptobot_funding_basis_v1.json')


def _db_path() -> Path:
    raw = os.getenv('FUNDING_MONITOR_DB_PATH')
    return Path(raw) if raw else _data_path('cryptobot_funding_basis_v1.db')


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
    if b <= 0:
        return 0.0
    return (a / b - 1.0) * 100.0


def _db_connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS snapshots (
            generated_ms INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            pair TEXT NOT NULL,
            funding_hour_pct REAL NOT NULL,
            predicted_funding_hour_pct REAL NOT NULL,
            basis_pct REAL NOT NULL,
            spread_pct REAL NOT NULL,
            volume_quote REAL NOT NULL,
            open_interest REAL NOT NULL,
            score REAL NOT NULL,
            action TEXT NOT NULL,
            PRIMARY KEY (generated_ms, symbol)
        )
        '''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_time ON snapshots(symbol, generated_ms)')
    conn.commit()
    return conn


def _history_summary(conn: sqlite3.Connection, symbol: str, now_ms: int) -> dict[str, float]:
    cutoff = now_ms - 24 * 60 * 60 * 1000
    rows = conn.execute(
        '''
        SELECT funding_hour_pct, predicted_funding_hour_pct
        FROM snapshots
        WHERE symbol=? AND generated_ms>=?
        ORDER BY generated_ms
        ''',
        (symbol, cutoff),
    ).fetchall()
    if not rows:
        return {'samples_24h': 0.0, 'positive_share_24h': 0.0, 'avg_funding_hour_pct_24h': 0.0}
    funding = [float(row[0]) for row in rows]
    positive = sum(1 for value in funding if value > 0.0)
    return {
        'samples_24h': float(len(rows)),
        'positive_share_24h': positive / len(rows),
        'avg_funding_hour_pct_24h': sum(funding) / len(funding),
    }


def _score_candidate(
    *,
    funding_hour_pct: float,
    predicted_hour_pct: float,
    spread_pct: float,
    volume_quote: float,
    basis_pct: float,
    history: dict[str, float],
) -> tuple[float, str, float]:
    gross_7d_pct = max(0.0, funding_hour_pct) * 24.0 * 7.0
    net_7d_snapshot_pct = gross_7d_pct - TOTAL_ROUNDTRIP_BUFFER_PCT
    samples = int(history.get('samples_24h', 0.0))
    positive_share = float(history.get('positive_share_24h', 0.0))
    avg_24h = float(history.get('avg_funding_hour_pct_24h', 0.0))

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
    if samples >= MIN_HISTORY_SAMPLES:
        score += min(10.0, positive_share * 10.0)

    score = round(max(0.0, min(100.0, score)), 1)
    action = 'VERZAMELEN'
    if (
        samples >= MIN_HISTORY_SAMPLES
        and funding_hour_pct > 0.0
        and predicted_hour_pct > 0.0
        and avg_24h > 0.0
        and positive_share >= MIN_POSITIVE_SHARE
        and net_7d_snapshot_pct > 0.50
        and volume_quote >= MIN_VOLUME_QUOTE_USD
        and spread_pct <= MAX_FUTURES_SPREAD_PCT
    ):
        action = 'CARRY WATCH'
    if action == 'CARRY WATCH' and net_7d_snapshot_pct >= 1.00 and positive_share >= 0.80:
        action = 'STERKE CARRY WATCH'
    return score, action, net_7d_snapshot_pct


def _fetch_tickers(timeout: int = 10) -> list[dict[str, Any]]:
    response = requests.get(f'{KRAKEN_FUTURES_URL}/tickers', timeout=timeout, headers={'Accept': 'application/json'})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('result') not in {None, 'success'}:
        raise RuntimeError('ongeldig Kraken Futures tickers-antwoord')
    tickers = payload.get('tickers', [])
    if not isinstance(tickers, list):
        raise RuntimeError('Kraken Futures tickers ontbreken')
    return [row for row in tickers if isinstance(row, dict)]


def scan_once() -> dict[str, object]:
    now_ms = int(time.time() * 1000)
    tickers = _fetch_tickers()
    conn = _db_connect()
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        rows = []
        for ticker in tickers:
            symbol = str(ticker.get('symbol', '')).upper()
            tag = str(ticker.get('tag', '')).lower()
            if not symbol.startswith('PF_') or tag != 'perpetual' or bool(ticker.get('suspended', False)):
                continue

            pair = str(ticker.get('pair', symbol)).upper()
            mark = _finite(ticker.get('markPrice'))
            index = _finite(ticker.get('indexPrice'))
            bid = _finite(ticker.get('bid'))
            ask = _finite(ticker.get('ask'))
            funding_raw = _finite(ticker.get('fundingRate'))
            predicted_raw = _finite(ticker.get('fundingRatePrediction'))
            volume_quote = _finite(ticker.get('volumeQuote'))
            open_interest = _finite(ticker.get('openInterest'))
            if min(mark, index, bid, ask) <= 0 or ask < bid:
                continue

            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else 999.0
            basis_pct = _pct_change(mark, index)
            funding_hour_pct = funding_raw * 100.0
            predicted_hour_pct = predicted_raw * 100.0
            history = _history_summary(conn, symbol, now_ms)
            score, action, net_7d = _score_candidate(
                funding_hour_pct=funding_hour_pct,
                predicted_hour_pct=predicted_hour_pct,
                spread_pct=spread_pct,
                volume_quote=volume_quote,
                basis_pct=basis_pct,
                history=history,
            )
            row = {
                'symbol': symbol,
                'pair': pair,
                'mark_price': mark,
                'index_price': index,
                'basis_pct': round(basis_pct, 5),
                'funding_hour_pct': round(funding_hour_pct, 7),
                'predicted_funding_hour_pct': round(predicted_hour_pct, 7),
                'gross_funding_7d_snapshot_pct': round(max(0.0, funding_hour_pct) * 24.0 * 7.0, 4),
                'net_7d_snapshot_pct': round(net_7d, 4),
                'spread_pct': round(spread_pct, 5),
                'volume_quote': round(volume_quote, 2),
                'open_interest': round(open_interest, 4),
                'samples_24h': int(history['samples_24h']),
                'positive_share_24h_pct': round(history['positive_share_24h'] * 100.0, 1),
                'avg_funding_hour_pct_24h': round(history['avg_funding_hour_pct_24h'], 7),
                'score': score,
                'action': action,
            }
            rows.append(row)

        rows.sort(key=lambda row: (float(row['volume_quote']), float(row['open_interest'])), reverse=True)
        rows = rows[:30]

        for row in rows:
            conn.execute(
                '''
                INSERT OR REPLACE INTO snapshots (
                    generated_ms,symbol,pair,funding_hour_pct,predicted_funding_hour_pct,
                    basis_pct,spread_pct,volume_quote,open_interest,score,action
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ''',
                (
                    now_ms,
                    row['symbol'],
                    row['pair'],
                    row['funding_hour_pct'],
                    row['predicted_funding_hour_pct'],
                    row['basis_pct'],
                    row['spread_pct'],
                    row['volume_quote'],
                    row['open_interest'],
                    row['score'],
                    row['action'],
                ),
            )
        conn.commit()

        # Herbereken history inclusief huidige snapshot.
        for row in rows:
            history = _history_summary(conn, str(row['symbol']), now_ms)
            score, action, net_7d = _score_candidate(
                funding_hour_pct=float(row['funding_hour_pct']),
                predicted_hour_pct=float(row['predicted_funding_hour_pct']),
                spread_pct=float(row['spread_pct']),
                volume_quote=float(row['volume_quote']),
                basis_pct=float(row['basis_pct']),
                history=history,
            )
            row['samples_24h'] = int(history['samples_24h'])
            row['positive_share_24h_pct'] = round(history['positive_share_24h'] * 100.0, 1)
            row['avg_funding_hour_pct_24h'] = round(history['avg_funding_hour_pct_24h'], 7)
            row['score'] = score
            row['action'] = action
            row['net_7d_snapshot_pct'] = round(net_7d, 4)
            candidates.append(row)

        candidates.sort(
            key=lambda row: (
                2 if row['action'] == 'STERKE CARRY WATCH' else 1 if row['action'] == 'CARRY WATCH' else 0,
                float(row['score']),
                float(row['net_7d_snapshot_pct']),
                float(row['volume_quote']),
            ),
            reverse=True,
        )
    except Exception as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
        raise
    finally:
        conn.close()

    generated = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).isoformat()
    return {
        'version': '1.0',
        'mode': 'READ_ONLY_PUBLIC_DATA',
        'generated_at_ms': now_ms,
        'generated_at_utc': generated,
        'source': 'Kraken Futures public tickers',
        'roundtrip_buffer_pct': TOTAL_ROUNDTRIP_BUFFER_PCT,
        'assumptions': {
            'kraken_futures_taker_fee_per_side_pct': FUTURES_TAKER_FEE_PCT,
            'bitvavo_usdc_taker_fee_per_side_pct': BITVAVO_USDC_TAKER_FEE_PCT,
            'execution_buffer_pct': EXECUTION_BUFFER_PCT,
            'snapshot_horizon_days': 7,
        },
        'eligible_perpetuals': len(candidates),
        'watch_count': sum(1 for row in candidates if 'CARRY WATCH' in str(row['action'])),
        'top5': candidates[:5],
        'candidates': candidates,
        'errors': errors,
        'note': 'Funding extrapolatie veronderstelt tijdelijk gelijkblijvend tarief; monitor opent geen posities.',
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
    print('=== FUNDING / BASIS MONITOR v1 | READ ONLY ===')
    print(f"UTC             : {report.get('generated_at_utc', 'n/a')}")
    print(f"PERPETUALS      : {report.get('eligible_perpetuals', 0)}")
    print(f"CARRY WATCH     : {report.get('watch_count', 0)}")
    print(f"KOSTENBUFFER    : {float(report.get('roundtrip_buffer_pct', 0.0)):.2f}% roundtrip")
    print('ORDERS          : ONMOGELIJK | alleen publieke marktdata')
    print()
    print('=== TOP 5 ===')
    top5 = report.get('top5', [])
    if not isinstance(top5, list) or not top5:
        print('geen kandidaten')
    else:
        for index, row in enumerate(top5, 1):
            if not isinstance(row, dict):
                continue
            print(
                f"{index}. {str(row.get('symbol','?')):12s} | {str(row.get('action','VERZAMELEN')):18s}"
                f" | score {float(row.get('score',0.0)):5.1f}/100"
                f" | funding {float(row.get('funding_hour_pct',0.0)):+.6f}%/u"
                f" | voorspeld {float(row.get('predicted_funding_hour_pct',0.0)):+.6f}%/u"
            )
            print(
                f"   basis {float(row.get('basis_pct',0.0)):+.3f}%"
                f" | spread {float(row.get('spread_pct',0.0)):.3f}%"
                f" | 7d snapshot netto {float(row.get('net_7d_snapshot_pct',0.0)):+.2f}%"
                f" | positief24h {float(row.get('positive_share_24h_pct',0.0)):.1f}%"
                f" | samples {int(row.get('samples_24h',0))}"
            )
    print()
    print('LET OP: 7d snapshot is extrapolatie van huidig fundingtarief, geen gegarandeerde opbrengst.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Funding/Basis Monitor v1 - public data, read only')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    if args.status:
        report = _load_report()
        if report is None:
            print('=== FUNDING / BASIS MONITOR v1 | READ ONLY ===')
            print('STATUS          : nog geen rapport beschikbaar')
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
            logger.exception('funding-monitor-cyclus mislukt: %s', exc)
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
