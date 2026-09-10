from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any

from bitvavo_public import BitvavoPublic
from v38_discovery import human_discovery_decision, movement_features, proposed_paper_size


STOP = False
MINUTE_MS = 60_000
PRICE_RETENTION_MS = 65 * MINUTE_MS
DECISION_RETENTION_MS = 6 * 60 * MINUTE_MS


def _data_path(name: str) -> str:
    root = Path('/var/data') if Path('/var/data').exists() else Path('data')
    return str(root / name)


DB_PATH = os.getenv('V38_DB_PATH', _data_path('cryptobot_autonomous_v38.db'))
REPORT_PATH = os.getenv('V38_REPORT_PATH', _data_path('cryptobot_autonomous_v38.json'))


def _connect() -> sqlite3.Connection:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS v38_prices(
        captured_ms INTEGER NOT NULL, market TEXT NOT NULL, price REAL NOT NULL,
        volume_quote REAL NOT NULL, PRIMARY KEY(captured_ms,market));
      CREATE TABLE IF NOT EXISTS v38_decisions(
        evaluated_ms INTEGER NOT NULL, market TEXT NOT NULL, action TEXT NOT NULL,
        score REAL NOT NULL, proposed_paper_eur REAL NOT NULL, details_json TEXT NOT NULL,
        PRIMARY KEY(evaluated_ms,market));
      CREATE TABLE IF NOT EXISTS v38_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    ''')
    return conn


def scan_once(api: BitvavoPublic | None = None, now_ms: int | None = None) -> dict[str, Any]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    market_api = api or BitvavoPublic('https://api.bitvavo.com/v2')
    tickers = market_api.quote_market_tickers('EUR')
    conn = _connect()
    decisions = []
    try:
        conn.execute('BEGIN IMMEDIATE')
        for ticker in tickers:
            conn.execute(
                'INSERT OR REPLACE INTO v38_prices VALUES (?,?,?,?)',
                (current, ticker['market'], ticker['last'], ticker['volume_quote']),
            )
        cutoff = current - PRICE_RETENTION_MS
        for ticker in tickers:
            rows = conn.execute(
                'SELECT captured_ms,price FROM v38_prices WHERE market=? AND captured_ms>=? ORDER BY captured_ms',
                (ticker['market'], cutoff),
            ).fetchall()
            features = movement_features([(r[0], r[1]) for r in rows], current)
            decision = human_discovery_decision(
                str(ticker['market']), features, volume_quote=float(ticker['volume_quote'])
            )
            decision['proposed_paper_eur'] = proposed_paper_size(decision)
            decisions.append(decision)
            conn.execute(
                'INSERT OR REPLACE INTO v38_decisions VALUES (?,?,?,?,?,?)',
                (current, ticker['market'], decision['action'], decision['discovery_score'],
                 decision['proposed_paper_eur'], json.dumps(decision, ensure_ascii=False)),
            )
        conn.execute('DELETE FROM v38_prices WHERE captured_ms<?', (cutoff,))
        conn.execute(
            'DELETE FROM v38_decisions WHERE evaluated_ms<?',
            (current - DECISION_RETENTION_MS,),
        )
        conn.execute('INSERT OR REPLACE INTO v38_meta VALUES (?,?)', ('last_scan_ms', str(current)))
        conn.commit()
    finally:
        conn.close()
    selected = sorted(
        (d for d in decisions if d['action'] == 'DOOR_NAAR_MENSELIJKE_JURY'),
        key=lambda d: (-d['discovery_score'], d['market']),
    )
    report = {
        'version': '3.8', 'component': 'FULL_EUR_HUMAN_DISCOVERY',
        'generated_at_ms': current, 'mode': 'OBSERVE_ONLY',
        'paper_execution': 'UIT', 'live_orders': 'UIT / TECHNISCH ONMOGELIJK',
        'execution_enabled': False, 'markets_seen': len(tickers),
        'ready_for_human_jury': selected[:10],
        'note': 'Voorselectie is geen koopbesluit; v3.7-poorten en L2 blijven verplicht.',
    }
    path = Path(REPORT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)
    return report


def compact_copy(target_path: str) -> dict[str, Any]:
    """Maak buiten /var/data een kleine, gecontroleerde kopie met recente data."""
    source = Path(DB_PATH)
    target = Path(target_path)
    if not source.exists():
        raise RuntimeError(f'v3.8-database ontbreekt: {source}')
    if target.exists():
        raise RuntimeError(f'doelbestand bestaat al: {target}')
    source_conn = sqlite3.connect(
        f'file:{source}?mode=ro&immutable=1', uri=True, timeout=30
    )
    source_conn.row_factory = sqlite3.Row
    target.parent.mkdir(parents=True, exist_ok=True)
    target_conn = sqlite3.connect(target, timeout=30)
    try:
        target_conn.executescript('''
          PRAGMA journal_mode=DELETE;
          CREATE TABLE v38_prices(
            captured_ms INTEGER NOT NULL, market TEXT NOT NULL, price REAL NOT NULL,
            volume_quote REAL NOT NULL, PRIMARY KEY(captured_ms,market));
          CREATE TABLE v38_decisions(
            evaluated_ms INTEGER NOT NULL, market TEXT NOT NULL, action TEXT NOT NULL,
            score REAL NOT NULL, proposed_paper_eur REAL NOT NULL, details_json TEXT NOT NULL,
            PRIMARY KEY(evaluated_ms,market));
          CREATE TABLE v38_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        latest = int(source_conn.execute(
            'SELECT COALESCE(MAX(evaluated_ms),0) FROM v38_decisions'
        ).fetchone()[0])
        if latest <= 0:
            raise RuntimeError('v3.8-database bevat geen beslissingen')
        price_cutoff = latest - PRICE_RETENTION_MS
        decision_cutoff = latest - DECISION_RETENTION_MS
        target_conn.executemany(
            'INSERT INTO v38_prices VALUES (?,?,?,?)',
            source_conn.execute(
                '''SELECT captured_ms,market,price,volume_quote FROM v38_prices
                   WHERE captured_ms>=? ORDER BY captured_ms,market''',
                (price_cutoff,),
            ),
        )
        target_conn.executemany(
            'INSERT INTO v38_decisions VALUES (?,?,?,?,?,?)',
            source_conn.execute(
                '''SELECT evaluated_ms,market,action,score,proposed_paper_eur,details_json
                   FROM v38_decisions WHERE evaluated_ms>=?
                   ORDER BY evaluated_ms,market''',
                (decision_cutoff,),
            ),
        )
        target_conn.executemany(
            'INSERT INTO v38_meta VALUES (?,?)',
            source_conn.execute('SELECT key,value FROM v38_meta ORDER BY key'),
        )
        target_conn.commit()
        integrity = str(target_conn.execute('PRAGMA integrity_check').fetchone()[0])
        if integrity != 'ok':
            raise RuntimeError(f'compacte database is ongeldig: {integrity}')
        prices = int(target_conn.execute('SELECT COUNT(*) FROM v38_prices').fetchone()[0])
        decisions = int(target_conn.execute('SELECT COUNT(*) FROM v38_decisions').fetchone()[0])
    finally:
        target_conn.close()
        source_conn.close()
    return {
        'target': str(target),
        'prices': prices,
        'decisions': decisions,
        'bytes': target.stat().st_size,
        'integrity': 'ok',
    }


def print_status(report: dict[str, Any]) -> None:
    print('=== CRYPTOBOT v3.8 | VOLLEDIGE EUR-ONTDEKKING ===')
    print('MODUS                 : OBSERVE-ONLY')
    print('PAPER-UITVOERING      : UIT')
    print('LIVE ORDERS           : UIT / TECHNISCH ONMOGELIJK')
    print(f"EUR-markten gezien    : {report.get('markets_seen', 0)}")
    candidates = report.get('ready_for_human_jury', [])
    print(f'naar menselijke jury  : {len(candidates)}')
    for item in candidates[:10]:
        f = item.get('features', {})
        print(f"  {item['market']:<12} | score {item['discovery_score']:>5.1f} | 5m {f.get('momentum_5m_pct',0):+.2f}% | 15m {f.get('momentum_15m_pct',0):+.2f}% | voorstel €{item['proposed_paper_eur']:.0f}")
    print('Een voorselectie is nog geen koopbesluit; menselijke jury en L2 blijven verplicht.')


def _stop(*_: object) -> None:
    global STOP
    STOP = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--compact-copy', metavar='PAD')
    args = parser.parse_args()
    if args.compact_copy:
        print(json.dumps(compact_copy(args.compact_copy), indent=2))
        return 0
    if args.status:
        path = Path(REPORT_PATH)
        report = json.loads(path.read_text(encoding='utf-8')) if path.exists() else scan_once()
        print_status(report)
        return 0
    if args.once:
        print_status(scan_once())
        return 0
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not STOP:
        try:
            scan_once()
        except Exception as exc:
            print(f'[v3.8] scan mislukt: {type(exc).__name__}: {exc}', flush=True)
        for _ in range(60):
            if STOP:
                break
            time.sleep(1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
