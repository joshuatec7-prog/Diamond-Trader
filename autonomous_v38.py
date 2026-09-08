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
        cutoff = current - 65 * 60_000
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
    args = parser.parse_args()
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
