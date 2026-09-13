from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from bitvavo_public import BitvavoPublic
from v40_offline_scan import scan_all_eur
from v40_replay import DEFAULT_HORIZONS_MINUTES


logger = logging.getLogger('cryptobot_autonomous_v40')
STOP = False
MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS
DECISION_RETENTION_MS = 48 * HOUR_MS
CYCLE_RETENTION_MS = 14 * DAY_MS
HISTORY_RETENTION_MS = 90 * DAY_MS
CANDIDATE_COOLDOWN_MS = 4 * HOUR_MS
CANDIDATE_MAX_AGE_MS = 7 * MINUTE_MS
MINIMUM_L2_SAMPLES = 3
MINIMUM_L2_SPAN_MS = 60_000
MAXIMUM_L2_SPREAD_PCT = 0.25
MINIMUM_MEDIAN_IMBALANCE = -0.10
MINIMUM_WORST_IMBALANCE = -0.35
MAXIMUM_BUY_VWAP_DRIFT_PCT = 0.40


def _data_path(filename: str) -> str:
    root = Path('/var/data')
    if root.exists() and os.access(root, os.W_OK):
        return str(root / filename)
    return str(Path('data') / filename)


@dataclass(frozen=True)
class V40RuntimeSettings:
    mode: str = 'OBSERVE_ONLY'
    paper_start_eur: float = 3600.0
    reserve_eur: float = 200.0
    max_open_positions: int = 5
    api_base_url: str = 'https://api.bitvavo.com/v2'
    request_timeout_seconds: int = 15
    request_retries: int = 3
    scan_seconds: int = 300
    l2_seconds: int = 30
    outcome_seconds: int = 60
    report_seconds: int = 60
    db_path: str = ''
    report_path: str = ''

    @classmethod
    def from_env(cls) -> 'V40RuntimeSettings':
        return cls(
            db_path=os.getenv('V40_DB_PATH', _data_path('cryptobot_autonomous_v40.db')),
            report_path=os.getenv('V40_REPORT_PATH', _data_path('cryptobot_autonomous_v40.json')),
        )

    def validate(self) -> None:
        if self.mode != 'OBSERVE_ONLY':
            raise ValueError('v4.0 staat uitsluitend OBSERVE_ONLY toe')
        if self.paper_start_eur != 3600.0:
            raise ValueError('v4.0 PAPER-startkapitaal moet €3600 zijn')
        if self.reserve_eur < 200.0:
            raise ValueError('v4.0 reserve moet minimaal €200 zijn')
        if self.max_open_positions != 5:
            raise ValueError('v4.0 maximum open posities moet 5 zijn')
        if not self.api_base_url.startswith('https://'):
            raise ValueError('publieke Bitvavo-URL moet HTTPS gebruiken')


def _connect(settings: V40RuntimeSettings) -> sqlite3.Connection:
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS v40_cycles(
        cycle_ms INTEGER PRIMARY KEY, status TEXT NOT NULL, markets_seen INTEGER NOT NULL,
        markets_evaluated INTEGER NOT NULL, errors INTEGER NOT NULL, counts_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS v40_decisions(
        cycle_ms INTEGER NOT NULL, market TEXT NOT NULL, action TEXT NOT NULL,
        route TEXT NOT NULL, score REAL NOT NULL, proposed_paper_eur REAL NOT NULL,
        details_json TEXT NOT NULL, PRIMARY KEY(cycle_ms,market));
      CREATE TABLE IF NOT EXISTS v40_candidates(
        id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_ms INTEGER NOT NULL, market TEXT NOT NULL,
        created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL, status TEXT NOT NULL,
        route TEXT NOT NULL, score REAL NOT NULL, proposed_paper_eur REAL NOT NULL,
        entry_reference REAL NOT NULL, details_json TEXT NOT NULL,
        UNIQUE(cycle_ms,market));
      CREATE TABLE IF NOT EXISTS v40_l2(
        candidate_id INTEGER NOT NULL, captured_ms INTEGER NOT NULL,
        buy_vwap REAL NOT NULL, sell_vwap REAL NOT NULL, spread_pct REAL NOT NULL,
        imbalance REAL NOT NULL, details_json TEXT NOT NULL,
        PRIMARY KEY(candidate_id,captured_ms),
        FOREIGN KEY(candidate_id) REFERENCES v40_candidates(id));
      CREATE TABLE IF NOT EXISTS v40_alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER NOT NULL UNIQUE,
        event_ms INTEGER NOT NULL, market TEXT NOT NULL, alert_type TEXT NOT NULL,
        route TEXT NOT NULL, score REAL NOT NULL, proposed_paper_eur REAL NOT NULL,
        buy_vwap REAL NOT NULL, base_amount REAL NOT NULL,
        stop_reference REAL NOT NULL, target_reference REAL NOT NULL,
        details_json TEXT NOT NULL,
        FOREIGN KEY(candidate_id) REFERENCES v40_candidates(id));
      CREATE TABLE IF NOT EXISTS v40_outcomes(
        alert_id INTEGER NOT NULL, horizon_minutes INTEGER NOT NULL, measured_ms INTEGER NOT NULL,
        sell_vwap REAL NOT NULL, gross_return_pct REAL NOT NULL, net_return_pct REAL NOT NULL,
        details_json TEXT NOT NULL, PRIMARY KEY(alert_id,horizon_minutes),
        FOREIGN KEY(alert_id) REFERENCES v40_alerts(id));
      CREATE TABLE IF NOT EXISTS v40_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS idx_v40_candidate_status ON v40_candidates(status,created_ms);
      CREATE INDEX IF NOT EXISTS idx_v40_alert_market_time ON v40_alerts(market,event_ms);
    ''')
    return conn


def _set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        'INSERT INTO v40_meta(key,value) VALUES (?,?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, str(value)),
    )


def _meta(conn: sqlite3.Connection, key: str, default: str = '') -> str:
    row = conn.execute('SELECT value FROM v40_meta WHERE key=?', (key,)).fetchone()
    return str(row[0]) if row else default


def _prune_history(conn: sqlite3.Connection, cutoff_ms: int) -> int:
    """Verwijder oude signaalketens in FK-veilige volgorde."""
    candidate_ids = [
        int(row[0]) for row in conn.execute(
            'SELECT id FROM v40_candidates WHERE created_ms<?', (cutoff_ms,)
        )
    ]
    if not candidate_ids:
        return 0
    placeholders = ','.join('?' for _ in candidate_ids)
    alert_ids = [
        int(row[0]) for row in conn.execute(
            f'SELECT id FROM v40_alerts WHERE candidate_id IN ({placeholders})',
            candidate_ids,
        )
    ]
    if alert_ids:
        alert_placeholders = ','.join('?' for _ in alert_ids)
        conn.execute(
            f'DELETE FROM v40_outcomes WHERE alert_id IN ({alert_placeholders})', alert_ids
        )
    conn.execute(f'DELETE FROM v40_alerts WHERE candidate_id IN ({placeholders})', candidate_ids)
    conn.execute(f'DELETE FROM v40_l2 WHERE candidate_id IN ({placeholders})', candidate_ids)
    conn.execute(f'DELETE FROM v40_candidates WHERE id IN ({placeholders})', candidate_ids)
    return len(candidate_ids)


def ensure_runtime(settings: V40RuntimeSettings, now_ms: int | None = None) -> None:
    settings.validate()
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    conn = _connect(settings)
    try:
        _set_meta(conn, 'version', '4.0-phase-3')
        _set_meta(conn, 'initialized_ms', _meta(conn, 'initialized_ms', str(current)))
        _set_meta(conn, 'mode', 'OBSERVE_ONLY')
        _set_meta(conn, 'execution_enabled', '0')
        _set_meta(conn, 'live_orders_possible', '0')
        _set_meta(conn, 'paper_start_eur', '3600')
        _set_meta(conn, 'reserve_eur', '200')
        _set_meta(conn, 'existing_assets_excluded', '1')
        conn.commit()
    finally:
        conn.close()


def _compact_decision(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        key: decision.get(key) for key in (
            'market', 'action', 'route', 'score', 'reasons', 'proposed_paper_eur',
            'entry_reference', 'stop_reference', 'target_reference',
            'gross_stop_pct', 'gross_target_pct', 'estimated_roundtrip_cost_pct',
            'net_reward_risk', 'relative_strength_vs_btc_1h_pct', 'features',
            'execution_enabled', 'live_orders_possible',
        )
    }


def ingest_scan(
    settings: V40RuntimeSettings,
    scan: dict[str, Any],
    *,
    now_ms: int | None = None,
) -> dict[str, int]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    decisions = list(scan.get('decisions', []))
    counts: dict[str, int] = {}
    for decision in decisions:
        action = str(decision.get('action', 'AFWIJZEN'))
        counts[action] = counts.get(action, 0) + 1
    ranked = sorted(decisions, key=lambda item: -float(item.get('score', 0.0)))
    keep_markets = {str(item.get('market')) for item in ranked[:20]}
    kept = [
        item for item in decisions
        if str(item.get('action')) != 'AFWIJZEN' or str(item.get('market')) in keep_markets
    ]
    queued = 0
    conn = _connect(settings)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            'INSERT OR REPLACE INTO v40_cycles VALUES (?,?,?,?,?,?)',
            (
                current, 'COMPLETE', int(scan.get('markets_seen', 0)),
                int(scan.get('markets_evaluated', 0)), len(scan.get('errors', [])),
                json.dumps(counts, ensure_ascii=False),
            ),
        )
        for decision in kept:
            compact = _compact_decision(decision)
            conn.execute(
                'INSERT OR REPLACE INTO v40_decisions VALUES (?,?,?,?,?,?,?)',
                (
                    current, str(decision.get('market')), str(decision.get('action')),
                    str(decision.get('route')), float(decision.get('score', 0.0)),
                    float(decision.get('proposed_paper_eur', 0.0)),
                    json.dumps(compact, ensure_ascii=False),
                ),
            )
            if str(decision.get('action')) != 'KOOPKANS':
                continue
            market = str(decision.get('market'))
            recent = conn.execute(
                'SELECT 1 FROM v40_candidates WHERE market=? AND created_ms>? LIMIT 1',
                (market, current - CANDIDATE_COOLDOWN_MS),
            ).fetchone()
            if recent:
                continue
            conn.execute(
                '''INSERT INTO v40_candidates
                   (cycle_ms,market,created_ms,updated_ms,status,route,score,
                    proposed_paper_eur,entry_reference,details_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (
                    current, market, current, current, 'WACHT_OP_L2',
                    str(decision.get('route')), float(decision.get('score', 0.0)),
                    float(decision.get('proposed_paper_eur', 0.0)),
                    float(decision.get('entry_reference', 0.0)),
                    json.dumps(compact, ensure_ascii=False),
                ),
            )
            queued += 1
        conn.execute('DELETE FROM v40_decisions WHERE cycle_ms<?', (current - DECISION_RETENTION_MS,))
        conn.execute('DELETE FROM v40_cycles WHERE cycle_ms<?', (current - CYCLE_RETENTION_MS,))
        removed_history = _prune_history(conn, current - HISTORY_RETENTION_MS)
        _set_meta(conn, 'scan_attempted_ms', current)
        _set_meta(conn, 'scan_generated_ms', current)
        _set_meta(conn, 'last_scan_errors', json.dumps(scan.get('errors', []), ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return {
        'decisions': len(decisions), 'stored': len(kept), 'queued_l2': queued,
        'removed_history': removed_history,
    }


def scan_once(
    settings: V40RuntimeSettings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    market_api = api or BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    try:
        scan = scan_all_eur(market_api)
    except Exception as exc:
        conn = _connect(settings)
        try:
            _set_meta(conn, 'scan_attempted_ms', current)
            _set_meta(conn, 'last_scan_errors', json.dumps([
                f'{type(exc).__name__}: {exc}'
            ], ensure_ascii=False))
            conn.commit()
        finally:
            conn.close()
        return {'complete': False, 'reason': str(exc)}
    stored = ingest_scan(settings, scan, now_ms=current)
    return {'complete': True, **stored}


def _l2_summary(rows: list[sqlite3.Row]) -> dict[str, float]:
    spreads = [float(row['spread_pct']) for row in rows]
    imbalances = [float(row['imbalance']) for row in rows]
    buys = [float(row['buy_vwap']) for row in rows]
    return {
        'samples': float(len(rows)),
        'span_seconds': (int(rows[-1]['captured_ms']) - int(rows[0]['captured_ms'])) / 1000.0,
        'median_spread_pct': median(spreads),
        'worst_spread_pct': max(spreads),
        'median_imbalance': median(imbalances),
        'worst_imbalance': min(imbalances),
        'buy_vwap': buys[-1],
        'buy_vwap_drift_pct': (max(buys) / min(buys) - 1.0) * 100.0,
    }


def recheck_candidates(
    settings: V40RuntimeSettings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    market_api = api or BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    conn = _connect(settings)
    candidates = conn.execute(
        "SELECT * FROM v40_candidates WHERE status='WACHT_OP_L2' ORDER BY created_ms,id"
    ).fetchall()
    conn.close()
    confirmed = []
    rejected = []
    errors = []
    for candidate in candidates:
        candidate_id = int(candidate['id'])
        if current - int(candidate['created_ms']) > CANDIDATE_MAX_AGE_MS:
            conn = _connect(settings)
            try:
                conn.execute(
                    "UPDATE v40_candidates SET status='AFGEWEZEN_L2_TIMEOUT',updated_ms=? WHERE id=?",
                    (current, candidate_id),
                )
                conn.commit()
            finally:
                conn.close()
            rejected.append(str(candidate['market']))
            continue
        try:
            book = market_api.depth_book(
                str(candidate['market']), float(candidate['proposed_paper_eur'])
            )
            conn = _connect(settings)
            try:
                conn.execute(
                    'INSERT OR REPLACE INTO v40_l2 VALUES (?,?,?,?,?,?,?)',
                    (
                        candidate_id, current, float(book['buy_vwap']), float(book['sell_vwap']),
                        float(book['execution_spread_pct']), float(book['near_book_imbalance']),
                        json.dumps(book, ensure_ascii=False),
                    ),
                )
                conn.execute('UPDATE v40_candidates SET updated_ms=? WHERE id=?', (current, candidate_id))
                rows = conn.execute(
                    'SELECT * FROM v40_l2 WHERE candidate_id=? ORDER BY captured_ms',
                    (candidate_id,),
                ).fetchall()
                if len(rows) < MINIMUM_L2_SAMPLES or (
                    int(rows[-1]['captured_ms']) - int(rows[0]['captured_ms']) < MINIMUM_L2_SPAN_MS
                ):
                    conn.commit()
                    continue
                summary = _l2_summary(rows)
                blockers = []
                if summary['median_spread_pct'] > MAXIMUM_L2_SPREAD_PCT:
                    blockers.append('mediane_l2_spread_te_hoog')
                if summary['worst_spread_pct'] > MAXIMUM_L2_SPREAD_PCT:
                    blockers.append('l2_spread_niet_stabiel')
                if summary['median_imbalance'] < MINIMUM_MEDIAN_IMBALANCE:
                    blockers.append('mediane_orderboekdruk_negatief')
                if summary['worst_imbalance'] < MINIMUM_WORST_IMBALANCE:
                    blockers.append('orderboek_toont_verkooppiek')
                if summary['buy_vwap_drift_pct'] > MAXIMUM_BUY_VWAP_DRIFT_PCT:
                    blockers.append('uitvoerprijs_te_instabiel')
                if blockers:
                    conn.execute(
                        "UPDATE v40_candidates SET status='AFGEWEZEN_L2',updated_ms=? WHERE id=?",
                        (current, candidate_id),
                    )
                    rejected.append(str(candidate['market']))
                else:
                    details = json.loads(str(candidate['details_json']))
                    reference = float(candidate['entry_reference'])
                    buy = float(summary['buy_vwap'])
                    stop_ratio = float(details.get('stop_reference', reference)) / reference
                    target_ratio = float(details.get('target_reference', reference)) / reference
                    base_amount = float(candidate['proposed_paper_eur']) / buy
                    conn.execute(
                        "UPDATE v40_candidates SET status='BEVESTIGDE_KOOPKANS',updated_ms=? WHERE id=?",
                        (current, candidate_id),
                    )
                    conn.execute(
                        '''INSERT OR IGNORE INTO v40_alerts
                           (candidate_id,event_ms,market,alert_type,route,score,proposed_paper_eur,
                            buy_vwap,base_amount,stop_reference,target_reference,details_json)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (
                            candidate_id, current, str(candidate['market']), 'KOOPKANS',
                            str(candidate['route']), float(candidate['score']),
                            float(candidate['proposed_paper_eur']), buy, base_amount,
                            buy * stop_ratio, buy * target_ratio,
                            json.dumps({'l2': summary, 'entry': details}, ensure_ascii=False),
                        ),
                    )
                    confirmed.append(str(candidate['market']))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            errors.append(f"{candidate['market']}: {type(exc).__name__}: {exc}")
    conn = _connect(settings)
    try:
        _set_meta(conn, 'l2_attempted_ms', current)
        _set_meta(conn, 'l2_errors', json.dumps(errors, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return {'pending_checked': len(candidates), 'confirmed': confirmed, 'rejected': rejected, 'errors': errors}


def monitor_outcomes(
    settings: V40RuntimeSettings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    market_api = api or BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    conn = _connect(settings)
    alerts = conn.execute('SELECT * FROM v40_alerts ORDER BY event_ms,id').fetchall()
    pending = []
    for alert in alerts:
        existing = {
            int(row[0]) for row in conn.execute(
                'SELECT horizon_minutes FROM v40_outcomes WHERE alert_id=?', (int(alert['id']),)
            )
        }
        for horizon in DEFAULT_HORIZONS_MINUTES:
            if horizon not in existing and current >= int(alert['event_ms']) + horizon * MINUTE_MS:
                pending.append((alert, horizon))
    conn.close()
    measured = 0
    errors = []
    for alert, horizon in pending:
        try:
            book = market_api.sell_vwap_for_base(str(alert['market']), float(alert['base_amount']))
            sell = float(book['sell_vwap'])
            entry = float(alert['buy_vwap'])
            gross = (sell / entry - 1.0) * 100.0
            net = gross - 0.66
            conn = _connect(settings)
            try:
                conn.execute(
                    'INSERT OR IGNORE INTO v40_outcomes VALUES (?,?,?,?,?,?,?)',
                    (
                        int(alert['id']), horizon, current, sell, gross, net,
                        json.dumps(book, ensure_ascii=False),
                    ),
                )
                conn.commit()
                measured += 1
            finally:
                conn.close()
        except Exception as exc:
            errors.append(f"{alert['market']} {horizon}m: {type(exc).__name__}: {exc}")
    conn = _connect(settings)
    try:
        _set_meta(conn, 'outcome_attempted_ms', current)
        _set_meta(conn, 'outcome_errors', json.dumps(errors, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return {'pending': len(pending), 'measured': measured, 'errors': errors}


def _outcome_summary(conn: sqlite3.Connection, horizon: int) -> dict[str, Any]:
    values = [
        float(row[0]) for row in conn.execute(
            'SELECT net_return_pct FROM v40_outcomes WHERE horizon_minutes=?', (horizon,)
        )
    ]
    wins = [value for value in values if value > 0.0]
    losses = [-value for value in values if value < 0.0]
    return {
        'samples': len(values),
        'average_net_pct': round(mean(values), 5) if values else None,
        'win_rate_pct': round(len(wins) / len(values) * 100.0, 2) if values else None,
        'profit_factor': round(sum(wins) / sum(losses), 4) if losses else None,
    }


def build_report(settings: V40RuntimeSettings, now_ms: int | None = None) -> dict[str, Any]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    conn = _connect(settings)
    try:
        cycle = conn.execute('SELECT * FROM v40_cycles ORDER BY cycle_ms DESC LIMIT 1').fetchone()
        pending = int(conn.execute(
            "SELECT COUNT(*) FROM v40_candidates WHERE status='WACHT_OP_L2'"
        ).fetchone()[0])
        alerts = [dict(row) for row in conn.execute(
            'SELECT * FROM v40_alerts WHERE event_ms>=? ORDER BY event_ms DESC LIMIT 20',
            (current - DAY_MS,),
        )]
        outcomes = {
            str(horizon): _outcome_summary(conn, horizon)
            for horizon in DEFAULT_HORIZONS_MINUTES
        }
        heartbeat = {
            'scan_attempted_ms': int(_meta(conn, 'scan_attempted_ms', '0') or 0),
            'l2_attempted_ms': int(_meta(conn, 'l2_attempted_ms', '0') or 0),
            'outcome_attempted_ms': int(_meta(conn, 'outcome_attempted_ms', '0') or 0),
        }
    finally:
        conn.close()
    for alert in alerts:
        alert.pop('details_json', None)
    db_bytes = Path(settings.db_path).stat().st_size if Path(settings.db_path).exists() else 0
    latest_cycle = {
        'cycle_ms': int(cycle['cycle_ms']),
        'status': str(cycle['status']),
        'markets_seen': int(cycle['markets_seen']),
        'markets_evaluated': int(cycle['markets_evaluated']),
        'errors': int(cycle['errors']),
        'counts': json.loads(str(cycle['counts_json'])),
    } if cycle else {}
    return {
        'version': '4.0-phase-3',
        'component': 'FULL_EUR_HUMAN_OBSERVER_V40',
        'generated_at_ms': current,
        'generated_at_utc': datetime.fromtimestamp(current / 1000, timezone.utc).isoformat(),
        'mode': 'OBSERVE_ONLY',
        'safety': {
            'execution_enabled': False,
            'live_orders_possible': False,
            'existing_assets_excluded': True,
        },
        'paper_portfolio': {
            'start_eur': settings.paper_start_eur,
            'reserve_eur': settings.reserve_eur,
            'maximum_open_positions': settings.max_open_positions,
            'variable_sizes_eur': [250, 400, 500],
            'maximum_simultaneous_allocation_eur': 2500,
            'buffer_at_maximum_allocation_eur': 1100,
        },
        'latest_cycle': latest_cycle,
        'l2': {'pending_candidates': pending, 'minimum_samples': MINIMUM_L2_SAMPLES},
        'alerts_last_24h': alerts,
        'prospective_outcomes': outcomes,
        'heartbeat': heartbeat,
        'storage': {
            'database_bytes': db_bytes,
            'decision_retention_hours': DECISION_RETENTION_MS // HOUR_MS,
            'cycle_retention_days': CYCLE_RETENTION_MS // DAY_MS,
            'signal_history_retention_days': HISTORY_RETENTION_MS // DAY_MS,
        },
    }


def write_report(settings: V40RuntimeSettings, report: dict[str, Any]) -> None:
    path = Path(settings.report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def load_report(settings: V40RuntimeSettings) -> dict[str, Any]:
    try:
        value = json.loads(Path(settings.report_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def print_status(report: dict[str, Any]) -> None:
    cycle = report.get('latest_cycle', {})
    portfolio = report.get('paper_portfolio', {})
    print('=== CRYPTOBOT v4.0 FASE 3 | MENSELIJKE OBSERVER ===')
    print('MODUS                 : OBSERVE-ONLY')
    print('PAPER-UITVOERING      : UIT')
    print('LIVE ORDERS           : UIT / TECHNISCH ONMOGELIJK')
    print(
        f"PAPER-KAPITAAL        : €{float(portfolio.get('start_eur', 0)):.0f}"
        f" | reserve €{float(portfolio.get('reserve_eur', 0)):.0f}"
    )
    print(
        f"LAATSTE SCAN          : {int(cycle.get('markets_evaluated', 0))}/"
        f"{int(cycle.get('markets_seen', 0))} EUR-markten"
    )
    print(f"BESLISSINGEN          : {cycle.get('counts', {})}")
    print(f"WACHT OP L2           : {int(report.get('l2', {}).get('pending_candidates', 0))}")
    alerts = report.get('alerts_last_24h', [])
    print(f"KOOPKANSEN 24U        : {len(alerts)}")
    for alert in alerts[:10]:
        print(
            f"  {alert['market']:<14} | {alert['route']:<22} | score {float(alert['score']):>5.1f}"
            f" | voorstel €{float(alert['proposed_paper_eur']):.0f}"
        )
    print(f"DATABASE              : {int(report.get('storage', {}).get('database_bytes', 0))/1_048_576:.1f} MB")


def _stop(*_: object) -> None:
    global STOP
    STOP = True


def main() -> int:
    parser = argparse.ArgumentParser(description='CryptoBot v4.0 observe-only menselijke worker')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    settings = V40RuntimeSettings.from_env()
    ensure_runtime(settings)
    if args.status:
        report = load_report(settings) or build_report(settings)
        print_status(report)
        return 0
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    api = BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    if args.once:
        scan_once(settings, api=api)
        recheck_candidates(settings, api=api)
        monitor_outcomes(settings, api=api)
        report = build_report(settings)
        write_report(settings, report)
        print_status(report)
        return 0
    next_scan = next_l2 = next_outcome = next_report = 0.0
    while not STOP:
        now = time.time()
        if now >= next_scan:
            scan_once(settings, api=api)
            next_scan = now + settings.scan_seconds
        if now >= next_l2:
            result = recheck_candidates(settings, api=api)
            for market in result['confirmed']:
                logger.warning('V4 KOOPKANS BEVESTIGD: %s', market)
            next_l2 = now + settings.l2_seconds
        if now >= next_outcome:
            monitor_outcomes(settings, api=api)
            next_outcome = now + settings.outcome_seconds
        if now >= next_report:
            write_report(settings, build_report(settings))
            next_report = now + settings.report_seconds
        time.sleep(1.0)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
