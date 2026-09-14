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
from v40_human_engine import (
    evaluate_dynamic_l2_challenger,
    evaluate_exit,
    evaluate_human_challenger,
    resolve_market_regime,
)
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
PAPER_HISTORY_RETENTION_MS = 365 * DAY_MS
NOTIFICATION_RETENTION_MS = 30 * DAY_MS
# Een bevestigde nieuwe opbouw mag na drie uur opnieuw worden beoordeeld.
# De L2-hercontrole en de blokkade op een reeds open positie blijven leidend.
CANDIDATE_COOLDOWN_MS = 3 * HOUR_MS
FOLLOW_NOTIFICATION_COOLDOWN_MS = 4 * HOUR_MS
CANDIDATE_MAX_AGE_MS = 7 * MINUTE_MS
MINIMUM_L2_SAMPLES = 3
MINIMUM_L2_SPAN_MS = 60_000
MAXIMUM_L2_SPREAD_PCT = 0.25
MINIMUM_MEDIAN_IMBALANCE = -0.10
MINIMUM_WORST_IMBALANCE = -0.35
MAXIMUM_BUY_VWAP_DRIFT_PCT = 0.40
PAPER_FEE_PCT = 0.25


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
    notification_path: str = ''

    @classmethod
    def from_env(cls) -> 'V40RuntimeSettings':
        return cls(
            db_path=os.getenv('V40_DB_PATH', _data_path('cryptobot_autonomous_v40.db')),
            report_path=os.getenv('V40_REPORT_PATH', _data_path('cryptobot_autonomous_v40.json')),
            notification_path=os.getenv(
                'V40_NOTIFICATION_PATH', _data_path('cryptobot_autonomous_v40_notifications.json')
            ),
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
      CREATE TABLE IF NOT EXISTS v40_paper_account(
        id INTEGER PRIMARY KEY CHECK(id=1), cash_eur REAL NOT NULL,
        realized_pnl_eur REAL NOT NULL, started_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS v40_paper_positions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id INTEGER NOT NULL UNIQUE,
        market TEXT NOT NULL, route TEXT NOT NULL, opened_ms INTEGER NOT NULL,
        entry_vwap REAL NOT NULL, initial_base REAL NOT NULL, remaining_base REAL NOT NULL,
        invested_eur REAL NOT NULL, highest_price REAL NOT NULL, protected_stop REAL NOT NULL,
        partial_taken INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
        closed_ms INTEGER, realized_pnl_eur REAL NOT NULL DEFAULT 0,
        FOREIGN KEY(alert_id) REFERENCES v40_alerts(id));
      CREATE TABLE IF NOT EXISTS v40_paper_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL,
        event_ms INTEGER NOT NULL, event_type TEXT NOT NULL, base_amount REAL NOT NULL,
        price REAL NOT NULL, cash_change_eur REAL NOT NULL, reason TEXT NOT NULL,
        FOREIGN KEY(position_id) REFERENCES v40_paper_positions(id));
      CREATE TABLE IF NOT EXISTS v40_notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
        event_ms INTEGER NOT NULL, notification_type TEXT NOT NULL, market TEXT NOT NULL,
        message TEXT NOT NULL, payload_json TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS v40_human_reviews(
        cycle_ms INTEGER NOT NULL, market TEXT NOT NULL, review_action TEXT NOT NULL,
        evidence_strength TEXT NOT NULL, confidence_score REAL NOT NULL,
        regime TEXT NOT NULL, vetoes_json TEXT NOT NULL, uncertainty_json TEXT NOT NULL,
        thesis_json TEXT NOT NULL, l2_review_json TEXT NOT NULL DEFAULT '{}',
        active_paper_changed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(cycle_ms,market));
      CREATE TABLE IF NOT EXISTS v40_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS idx_v40_candidate_status ON v40_candidates(status,created_ms);
      CREATE INDEX IF NOT EXISTS idx_v40_alert_market_time ON v40_alerts(market,event_ms);
      CREATE INDEX IF NOT EXISTS idx_v40_notification_time ON v40_notifications(event_ms);
      CREATE INDEX IF NOT EXISTS idx_v40_human_review_time ON v40_human_reviews(cycle_ms);
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


def _enqueue_notification(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    event_ms: int,
    notification_type: str,
    market: str,
    message: str,
    payload: dict[str, Any],
) -> bool:
    cursor = conn.execute(
        '''INSERT OR IGNORE INTO v40_notifications
           (event_key,event_ms,notification_type,market,message,payload_json)
           VALUES (?,?,?,?,?,?)''',
        (
            event_key, event_ms, notification_type, market, message,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    return cursor.rowcount == 1


def _prune_history(conn: sqlite3.Connection, cutoff_ms: int) -> int:
    """Verwijder oude signaalketens in FK-veilige volgorde."""
    candidate_ids = [
        int(row[0]) for row in conn.execute(
            '''SELECT c.id FROM v40_candidates c
               WHERE c.created_ms<? AND NOT EXISTS(
                 SELECT 1 FROM v40_alerts a JOIN v40_paper_positions p ON p.alert_id=a.id
                 WHERE a.candidate_id=c.id
               )''',
            (cutoff_ms,),
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


def _prune_paper_history(conn: sqlite3.Connection, cutoff_ms: int) -> int:
    position_ids = [
        int(row[0]) for row in conn.execute(
            "SELECT id FROM v40_paper_positions WHERE status='GESLOTEN' AND closed_ms<?",
            (cutoff_ms,),
        )
    ]
    if not position_ids:
        return 0
    placeholders = ','.join('?' for _ in position_ids)
    conn.execute(
        f'DELETE FROM v40_paper_events WHERE position_id IN ({placeholders})', position_ids
    )
    conn.execute(f'DELETE FROM v40_paper_positions WHERE id IN ({placeholders})', position_ids)
    return len(position_ids)


def ensure_runtime(settings: V40RuntimeSettings, now_ms: int | None = None) -> None:
    settings.validate()
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    conn = _connect(settings)
    try:
        _set_meta(conn, 'version', '4.0-phase-7')
        _set_meta(conn, 'initialized_ms', _meta(conn, 'initialized_ms', str(current)))
        _set_meta(conn, 'mode', 'OBSERVE_ONLY')
        _set_meta(conn, 'execution_enabled', '0')
        _set_meta(conn, 'live_orders_possible', '0')
        _set_meta(conn, 'paper_start_eur', '3600')
        _set_meta(conn, 'reserve_eur', '200')
        _set_meta(conn, 'existing_assets_excluded', '1')
        conn.execute(
            '''INSERT OR IGNORE INTO v40_paper_account
               (id,cash_eur,realized_pnl_eur,started_ms,updated_ms) VALUES (1,?,?,?,?)''',
            (settings.paper_start_eur, 0.0, current, current),
        )
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
            'maximum_hold_hours',
            'execution_enabled', 'live_orders_possible',
        )
    }


def _performance_guard(conn: sqlite3.Connection, current: int) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT closed_ms,realized_pnl_eur FROM v40_paper_positions
           WHERE status='GESLOTEN' AND closed_ms IS NOT NULL
           ORDER BY closed_ms DESC,id DESC LIMIT 20"""
    ).fetchall()
    consecutive_losses = 0
    for row in rows:
        if float(row['realized_pnl_eur']) < 0.0:
            consecutive_losses += 1
        else:
            break
    day_start = current - current % DAY_MS
    daily_pnl = sum(
        float(row['realized_pnl_eur']) for row in rows
        if int(row['closed_ms']) >= day_start
    )
    latest_closed = int(rows[0]['closed_ms']) if rows else 0
    loss_pause_until = latest_closed + 6 * HOUR_MS if consecutive_losses >= 3 else 0
    daily_pause_until = day_start + DAY_MS if daily_pnl <= -45.0 else 0
    pause_until = max(loss_pause_until, daily_pause_until)
    return {
        'consecutive_losses': consecutive_losses,
        'daily_realized_pnl_eur': daily_pnl,
        'pause_until_ms': pause_until,
        'pause_active': current < pause_until,
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
        regime_state = resolve_market_regime(
            decisions,
            previous_regime=_meta(conn, 'human_stable_regime'),
            pending_regime=_meta(conn, 'human_pending_regime'),
            pending_count=int(_meta(conn, 'human_pending_count', '0') or 0),
        )
        performance = _performance_guard(conn, current)
        _set_meta(conn, 'human_stable_regime', regime_state['stable_regime'])
        _set_meta(conn, 'human_visible_regime', regime_state['regime'])
        _set_meta(conn, 'human_pending_regime', regime_state['pending_regime'])
        _set_meta(conn, 'human_pending_count', regime_state['pending_count'])
        _set_meta(conn, 'human_regime_json', json.dumps(regime_state, ensure_ascii=False))
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
            market = str(decision.get('market'))
            action = str(decision.get('action'))
            human_review = evaluate_human_challenger(
                decision,
                regime_state=regime_state,
                consecutive_losses=int(performance['consecutive_losses']),
                pause_active=bool(performance['pause_active']),
                daily_realized_pnl_eur=float(performance['daily_realized_pnl_eur']),
            )
            compact['human_challenger'] = human_review
            conn.execute(
                'INSERT OR REPLACE INTO v40_decisions VALUES (?,?,?,?,?,?,?)',
                (
                    current, market, action,
                    str(decision.get('route')), float(decision.get('score', 0.0)),
                    float(decision.get('proposed_paper_eur', 0.0)),
                    json.dumps(compact, ensure_ascii=False),
                ),
            )
            if action in {'KOOPKANS', 'VOLGEN'} and str(decision.get('route')) != 'GEEN_SETUP':
                conn.execute(
                    '''INSERT OR REPLACE INTO v40_human_reviews
                       (cycle_ms,market,review_action,evidence_strength,confidence_score,
                        regime,vetoes_json,uncertainty_json,thesis_json,l2_review_json,
                        active_paper_changed)
                       VALUES (?,?,?,?,?,?,?,?,?,'{}',0)''',
                    (
                        current, market, str(human_review['review_action']),
                        str(human_review['evidence_strength']),
                        float(human_review['confidence_score']), str(human_review['regime']),
                        json.dumps(human_review['vetoes'], ensure_ascii=False),
                        json.dumps(human_review['uncertainty'], ensure_ascii=False),
                        json.dumps(human_review['thesis'], ensure_ascii=False),
                    ),
                )
            if (
                action == 'VOLGEN'
                and str(decision.get('route')) != 'GEEN_SETUP'
                and float(decision.get('score', 0.0)) >= 65.0
            ):
                recent_notice = conn.execute(
                    '''SELECT 1 FROM v40_notifications
                       WHERE market=? AND notification_type='VOLGEN' AND event_ms>?
                       LIMIT 1''',
                    (market, current - FOLLOW_NOTIFICATION_COOLDOWN_MS),
                ).fetchone()
                if not recent_notice:
                    score = float(decision.get('score', 0.0))
                    _enqueue_notification(
                        conn,
                        event_key=f'VOLGEN:{market}:{current}',
                        event_ms=current,
                        notification_type='VOLGEN',
                        market=market,
                        message=f'VOLGEN {market} | score {score:.1f} | nog geen koop',
                        payload=compact,
                    )
            if action != 'KOOPKANS':
                continue
            recent = conn.execute(
                '''SELECT 1 FROM v40_candidates
                   WHERE market=? AND (status='WACHT_OP_L2' OR created_ms>?) LIMIT 1''',
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
        removed_notifications = conn.execute(
            'DELETE FROM v40_notifications WHERE event_ms<?',
            (current - NOTIFICATION_RETENTION_MS,),
        ).rowcount
        removed_paper = _prune_paper_history(conn, current - PAPER_HISTORY_RETENTION_MS)
        removed_history = _prune_history(conn, current - HISTORY_RETENTION_MS)
        conn.execute('DELETE FROM v40_human_reviews WHERE cycle_ms<?', (current - HISTORY_RETENTION_MS,))
        _set_meta(conn, 'scan_attempted_ms', current)
        _set_meta(conn, 'scan_generated_ms', current)
        _set_meta(conn, 'last_scan_errors', json.dumps(scan.get('errors', []), ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return {
        'decisions': len(decisions), 'stored': len(kept), 'queued_l2': queued,
        'removed_history': removed_history, 'removed_paper': removed_paper,
        'removed_notifications': removed_notifications,
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
                details = json.loads(str(candidate['details_json']))
                l2_review = evaluate_dynamic_l2_challenger(
                    rows,
                    atr_pct=float(details.get('features', {}).get('atr_pct', 0.0)),
                )
                review_row = conn.execute(
                    '''SELECT review_action,vetoes_json FROM v40_human_reviews
                       WHERE cycle_ms=? AND market=?''',
                    (int(candidate['cycle_ms']), str(candidate['market'])),
                ).fetchone()
                if review_row:
                    human_vetoes = json.loads(str(review_row['vetoes_json']))
                    human_vetoes.extend(l2_review.get('vetoes', []))
                    human_action = (
                        'AFZIEN' if human_vetoes else str(review_row['review_action'])
                    )
                    conn.execute(
                        '''UPDATE v40_human_reviews SET review_action=?,vetoes_json=?,
                           l2_review_json=? WHERE cycle_ms=? AND market=?''',
                        (
                            human_action,
                            json.dumps(list(dict.fromkeys(human_vetoes)), ensure_ascii=False),
                            json.dumps(l2_review, ensure_ascii=False),
                            int(candidate['cycle_ms']), str(candidate['market']),
                        ),
                    )
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
                    _enqueue_notification(
                        conn,
                        event_key=f'KOOPKANS:{candidate_id}',
                        event_ms=current,
                        notification_type='KOOPKANS',
                        market=str(candidate['market']),
                        message=(
                            f"KOOPKANS {candidate['market']} | score {float(candidate['score']):.1f}"
                            f" | PAPER €{float(candidate['proposed_paper_eur']):.0f}"
                            f" | instap {buy:.10g}"
                        ),
                        payload={
                            'route': str(candidate['route']), 'score': float(candidate['score']),
                            'paper_eur': float(candidate['proposed_paper_eur']),
                            'entry': buy, 'stop': buy * stop_ratio, 'target': buy * target_ratio,
                            'execution_enabled': False, 'live_orders_possible': False,
                        },
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


def _latest_features(conn: sqlite3.Connection, market: str) -> dict[str, Any]:
    row = conn.execute(
        'SELECT details_json FROM v40_decisions WHERE market=? ORDER BY cycle_ms DESC LIMIT 1',
        (market,),
    ).fetchone()
    if not row:
        return {}
    try:
        details = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return {}
    features = details.get('features', {})
    return features if isinstance(features, dict) else {}


def simulate_paper_portfolio(
    settings: V40RuntimeSettings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Simuleer fills in een geïsoleerde portefeuille; verstuurt nooit een order."""
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    market_api = api or BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    opened: list[str] = []
    partial: list[str] = []
    closed: list[str] = []
    errors: list[str] = []
    fee_ratio = PAPER_FEE_PCT / 100.0

    conn = _connect(settings)
    positions = conn.execute(
        "SELECT * FROM v40_paper_positions WHERE status='OPEN' ORDER BY opened_ms,id"
    ).fetchall()
    conn.close()
    for position in positions:
        try:
            book = market_api.sell_vwap_for_base(
                str(position['market']), float(position['remaining_base'])
            )
            price = float(book['sell_vwap'])
            highest = max(float(position['highest_price']), price)
            conn = _connect(settings)
            try:
                features = _latest_features(conn, str(position['market']))
                decision = evaluate_exit(
                    entry_price=float(position['entry_vwap']), current_price=price,
                    highest_price=highest, initial_stop_price=float(position['protected_stop']),
                    held_hours=(current - int(position['opened_ms'])) / HOUR_MS,
                    current_features=features,
                )
                protected = max(float(position['protected_stop']), float(decision['protected_stop_price']))
                conn.execute(
                    'UPDATE v40_paper_positions SET highest_price=?,protected_stop=? WHERE id=?',
                    (highest, protected, int(position['id'])),
                )
                action = str(decision['action'])
                if action == 'DEEL_VERKOPEN' and int(position['partial_taken']):
                    action = 'VASTHOUDEN'
                if action in {'DEEL_VERKOPEN', 'VERKOPEN'}:
                    remaining = float(position['remaining_base'])
                    quantity = remaining / 2.0 if action == 'DEEL_VERKOPEN' else remaining
                    proceeds = quantity * price * (1.0 - fee_ratio)
                    cost = float(position['invested_eur']) * quantity / float(position['initial_base'])
                    pnl = proceeds - cost
                    new_remaining = max(0.0, remaining - quantity)
                    conn.execute(
                        '''UPDATE v40_paper_account SET cash_eur=cash_eur+?,
                           realized_pnl_eur=realized_pnl_eur+?,updated_ms=? WHERE id=1''',
                        (proceeds, pnl, current),
                    )
                    conn.execute(
                        '''UPDATE v40_paper_positions SET remaining_base=?,partial_taken=?,
                           status=?,closed_ms=?,realized_pnl_eur=realized_pnl_eur+? WHERE id=?''',
                        (
                            new_remaining, 1 if action == 'DEEL_VERKOPEN' else int(position['partial_taken']),
                            'OPEN' if action == 'DEEL_VERKOPEN' else 'GESLOTEN',
                            None if action == 'DEEL_VERKOPEN' else current, pnl, int(position['id']),
                        ),
                    )
                    conn.execute(
                        '''INSERT INTO v40_paper_events
                           (position_id,event_ms,event_type,base_amount,price,cash_change_eur,reason)
                           VALUES (?,?,?,?,?,?,?)''',
                        (
                            int(position['id']), current, action, quantity, price, proceeds,
                            str(decision['reason']),
                        ),
                    )
                    _enqueue_notification(
                        conn,
                        event_key=f'PAPER:{int(position["id"])}:{action}',
                        event_ms=current,
                        notification_type=action,
                        market=str(position['market']),
                        message=(
                            f'{action.replace("_", " ")} {position["market"]}'
                            f' | PAPER {quantity:.8g} stuks | prijs {price:.10g}'
                            f' | resultaat €{pnl:.2f}'
                        ),
                        payload={
                            'position_id': int(position['id']), 'base_amount': quantity,
                            'price': price, 'pnl_eur': pnl, 'reason': str(decision['reason']),
                            'execution_enabled': False, 'live_orders_possible': False,
                        },
                    )
                    (partial if action == 'DEEL_VERKOPEN' else closed).append(str(position['market']))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            errors.append(f"{position['market']} positie: {type(exc).__name__}: {exc}")

    conn = _connect(settings)
    try:
        account = conn.execute('SELECT * FROM v40_paper_account WHERE id=1').fetchone()
        open_count = int(conn.execute(
            "SELECT COUNT(*) FROM v40_paper_positions WHERE status='OPEN'"
        ).fetchone()[0])
        alerts = conn.execute(
            '''SELECT a.* FROM v40_alerts a
               LEFT JOIN v40_paper_positions p ON p.alert_id=a.id
               WHERE p.id IS NULL ORDER BY a.score DESC,a.event_ms,a.id'''
        ).fetchall()
        for alert in alerts:
            if open_count >= settings.max_open_positions:
                break
            same_market = conn.execute(
                "SELECT 1 FROM v40_paper_positions WHERE market=? AND status='OPEN' LIMIT 1",
                (str(alert['market']),),
            ).fetchone()
            if same_market:
                continue
            amount = float(alert['proposed_paper_eur'])
            cash = float(account['cash_eur'])
            if amount <= 0.0 or cash - amount < settings.reserve_eur:
                continue
            price = float(alert['buy_vwap'])
            base = amount * (1.0 - fee_ratio) / price
            cursor = conn.execute(
                '''INSERT INTO v40_paper_positions
                   (alert_id,market,route,opened_ms,entry_vwap,initial_base,remaining_base,
                    invested_eur,highest_price,protected_stop,status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    int(alert['id']), str(alert['market']), str(alert['route']), current,
                    price, base, base, amount, price, float(alert['stop_reference']), 'OPEN',
                ),
            )
            position_id = int(cursor.lastrowid)
            conn.execute(
                'UPDATE v40_paper_account SET cash_eur=cash_eur-?,updated_ms=? WHERE id=1',
                (amount, current),
            )
            conn.execute(
                '''INSERT INTO v40_paper_events
                   (position_id,event_ms,event_type,base_amount,price,cash_change_eur,reason)
                   VALUES (?,?,?,?,?,?,?)''',
                (position_id, current, 'KOPEN', base, price, -amount, 'bevestigde_l2_koopkans'),
            )
            _enqueue_notification(
                conn,
                event_key=f'PAPER:{position_id}:KOPEN',
                event_ms=current,
                notification_type='KOPEN',
                market=str(alert['market']),
                message=(
                    f'KOPEN {alert["market"]} | PAPER €{amount:.0f}'
                    f' | instap {price:.10g} | stop {float(alert["stop_reference"]):.10g}'
                ),
                payload={
                    'position_id': position_id, 'paper_eur': amount, 'entry': price,
                    'stop': float(alert['stop_reference']),
                    'target': float(alert['target_reference']),
                    'execution_enabled': False, 'live_orders_possible': False,
                },
            )
            account = conn.execute('SELECT * FROM v40_paper_account WHERE id=1').fetchone()
            open_count += 1
            opened.append(str(alert['market']))
        _set_meta(conn, 'paper_attempted_ms', current)
        _set_meta(conn, 'paper_errors', json.dumps(errors, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return {'opened': opened, 'partial': partial, 'closed': closed, 'errors': errors}


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


def _human_challenger_summary(conn: sqlite3.Connection, current: int) -> dict[str, Any]:
    rows = conn.execute(
        '''SELECT review_action,COUNT(*) AS samples,AVG(confidence_score) AS confidence
           FROM v40_human_reviews WHERE cycle_ms>=?
           GROUP BY review_action ORDER BY review_action''',
        (current - DAY_MS,),
    ).fetchall()
    outcome_rows = conn.execute(
        '''SELECT r.review_action,o.horizon_minutes,o.net_return_pct
           FROM v40_human_reviews r
           JOIN v40_candidates c ON c.cycle_ms=r.cycle_ms AND c.market=r.market
           JOIN v40_alerts a ON a.candidate_id=c.id
           JOIN v40_outcomes o ON o.alert_id=a.id'''
    ).fetchall()
    outcomes: dict[str, dict[str, list[float]]] = {}
    for row in outcome_rows:
        action = str(row['review_action'])
        horizon = str(int(row['horizon_minutes']))
        outcomes.setdefault(action, {}).setdefault(horizon, []).append(float(row['net_return_pct']))
    calibration_values = [
        (float(row['confidence_score']) / 100.0, float(row['net_return_pct']) > 0.0)
        for row in conn.execute(
            '''SELECT r.confidence_score,o.net_return_pct
               FROM v40_human_reviews r
               JOIN v40_candidates c ON c.cycle_ms=r.cycle_ms AND c.market=r.market
               JOIN v40_alerts a ON a.candidate_id=c.id
               JOIN v40_outcomes o ON o.alert_id=a.id AND o.horizon_minutes=60'''
        )
    ]
    calibration = {
        'status': 'VOLDOENDE_DATA' if len(calibration_values) >= 50 else 'ONVOLDOENDE_DATA',
        'samples': len(calibration_values),
        'minimum_samples': 50,
        'brier_score': round(mean(
            (probability - float(won)) ** 2 for probability, won in calibration_values
        ), 6) if calibration_values else None,
        'used_to_trade': False,
    }
    return {
        'observation_only': True,
        'applied_to_paper_entries': False,
        'reviews_last_24h': {
            str(row['review_action']): {
                'samples': int(row['samples']),
                'average_confidence_score': round(float(row['confidence']), 3),
            }
            for row in rows
        },
        'prospective_comparison': {
            action: {
                horizon: {
                    'samples': len(values),
                    'average_net_pct': round(mean(values), 5),
                }
                for horizon, values in by_horizon.items()
            }
            for action, by_horizon in outcomes.items()
        },
        'probability_calibrated': False,
        'calibration_check': calibration,
        'active_bot_changed': False,
    }


def _prospective_readiness(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        '''SELECT market,opened_ms,closed_ms,realized_pnl_eur
           FROM v40_paper_positions WHERE status='GESLOTEN'
           ORDER BY closed_ms,id'''
    ).fetchall()
    results = [float(row['realized_pnl_eur']) for row in rows]
    wins = [value for value in results if value > 0.0]
    losses = [-value for value in results if value < 0.0]
    profit_factor = sum(wins) / sum(losses) if losses else None
    equity = peak = drawdown = 0.0
    for value in results:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    span_days = (
        (int(rows[-1]['closed_ms']) - int(rows[0]['opened_ms'])) / DAY_MS
        if len(rows) >= 2 else 0.0
    )
    per_market: dict[str, float] = {}
    for row in rows:
        per_market[str(row['market'])] = per_market.get(str(row['market']), 0.0) + max(
            0.0, float(row['realized_pnl_eur'])
        )
    total_positive = sum(per_market.values())
    concentration = max(per_market.values(), default=0.0) / total_positive * 100.0 if total_positive else None
    requirements = {
        'minimum_trades_150': len(rows) >= 150,
        'minimum_weeks_6': span_days >= 42.0,
        'profit_factor_at_least_1_25': profit_factor is not None and profit_factor >= 1.25,
        'drawdown_at_most_5_pct_of_start': drawdown <= 180.0,
        'single_market_profit_at_most_25_pct': concentration is not None and concentration <= 25.0,
        'cost_stress_profit_factor_at_least_1_05': False,
    }
    enough_observation = requirements['minimum_trades_150'] and requirements['minimum_weeks_6']
    return {
        'decision': 'AFWIJZEN' if enough_observation and not all(requirements.values()) else 'VERZAMELEN',
        'closed_trades': len(rows),
        'observation_days': round(span_days, 2),
        'profit_factor': round(profit_factor, 4) if profit_factor is not None else None,
        'maximum_drawdown_eur': round(drawdown, 2),
        'largest_market_profit_share_pct': round(concentration, 2) if concentration is not None else None,
        'requirements': requirements,
        'live_discussion_allowed': False,
        'live_orders_possible': False,
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
            'paper_attempted_ms': int(_meta(conn, 'paper_attempted_ms', '0') or 0),
            'notification_written_ms': int(_meta(conn, 'notification_written_ms', '0') or 0),
        }
        account = conn.execute('SELECT * FROM v40_paper_account WHERE id=1').fetchone()
        paper_positions = [dict(row) for row in conn.execute(
            "SELECT * FROM v40_paper_positions WHERE status='OPEN' ORDER BY opened_ms,id"
        )]
        notifications = [dict(row) for row in conn.execute(
            'SELECT * FROM v40_notifications WHERE event_ms>=? ORDER BY event_ms DESC,id DESC LIMIT 100',
            (current - DAY_MS,),
        )]
        human_challenger = _human_challenger_summary(conn, current)
        prospective_readiness = _prospective_readiness(conn)
        try:
            human_challenger['latest_regime'] = json.loads(_meta(conn, 'human_regime_json', '{}'))
        except ValueError:
            human_challenger['latest_regime'] = {}
    finally:
        conn.close()
    for alert in alerts:
        alert.pop('details_json', None)
    for notice in notifications:
        notice.pop('payload_json', None)
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
        'version': '4.0-phase-7',
        'component': 'FULL_EUR_HUMAN_PAPER_V40',
        'generated_at_ms': current,
        'generated_at_utc': datetime.fromtimestamp(current / 1000, timezone.utc).isoformat(),
        'mode': 'OBSERVE_ONLY',
        'safety': {
            'execution_enabled': False,
            'live_orders_possible': False,
            'existing_assets_excluded': True,
            'paper_simulation_enabled': True,
        },
        'paper_portfolio': {
            'start_eur': settings.paper_start_eur,
            'reserve_eur': settings.reserve_eur,
            'maximum_open_positions': settings.max_open_positions,
            'variable_sizes_eur': [250, 400, 500],
            'maximum_simultaneous_allocation_eur': 2500,
            'buffer_at_maximum_allocation_eur': 1100,
            'cash_eur': round(float(account['cash_eur']), 8),
            'realized_pnl_eur': round(float(account['realized_pnl_eur']), 8),
            'open_positions': len(paper_positions),
            'committed_cost_eur': round(sum(
                float(position['invested_eur']) * float(position['remaining_base'])
                / float(position['initial_base']) for position in paper_positions
            ), 8),
        },
        'latest_cycle': latest_cycle,
        'l2': {'pending_candidates': pending, 'minimum_samples': MINIMUM_L2_SAMPLES},
        'alerts_last_24h': alerts,
        'notifications_last_24h': notifications,
        'prospective_outcomes': outcomes,
        'human_challenger': human_challenger,
        'prospective_readiness': prospective_readiness,
        'heartbeat': heartbeat,
        'storage': {
            'database_bytes': db_bytes,
            'decision_retention_hours': DECISION_RETENTION_MS // HOUR_MS,
            'cycle_retention_days': CYCLE_RETENTION_MS // DAY_MS,
            'signal_history_retention_days': HISTORY_RETENTION_MS // DAY_MS,
            'paper_history_retention_days': PAPER_HISTORY_RETENTION_MS // DAY_MS,
            'notification_retention_days': NOTIFICATION_RETENTION_MS // DAY_MS,
        },
    }


def write_notification_feed(
    settings: V40RuntimeSettings,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Schrijf een lokale, kanaalonafhankelijke feed; verstuurt zelf geen berichten."""
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    conn = _connect(settings)
    try:
        rows = conn.execute(
            '''SELECT id,event_ms,notification_type,market,message,payload_json
               FROM v40_notifications WHERE event_ms>=?
               ORDER BY event_ms DESC,id DESC LIMIT 100''',
            (current - 7 * DAY_MS,),
        ).fetchall()
        notices = []
        for row in rows:
            item = dict(row)
            item['payload'] = json.loads(item.pop('payload_json'))
            notices.append(item)
        _set_meta(conn, 'notification_written_ms', current)
        conn.commit()
    finally:
        conn.close()
    feed = {
        'version': '4.0-phase-7',
        'generated_at_ms': current,
        'generated_at_utc': datetime.fromtimestamp(current / 1000, timezone.utc).isoformat(),
        'delivery': 'LOKALE_FEED; EXTERN_KANAAL_NOG_NIET_GEKOZEN',
        'execution_enabled': False,
        'live_orders_possible': False,
        'notifications': notices,
    }
    path = Path(settings.notification_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    return feed


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
    print('=== CRYPTOBOT v4.0 FASE 7 | MENSELIJKE CHALLENGER (OBSERVATIE) ===')
    print('MODUS                 : OBSERVE-ONLY')
    print('PAPER-SIMULATIE       : AAN (ALLEEN REKENWERK)')
    print('LIVE ORDERS           : UIT / TECHNISCH ONMOGELIJK')
    print(
        f"PAPER-KAPITAAL        : €{float(portfolio.get('start_eur', 0)):.0f}"
        f" | reserve €{float(portfolio.get('reserve_eur', 0)):.0f}"
    )
    print(
        f"PAPER-STAND           : cash €{float(portfolio.get('cash_eur', 0)):.2f}"
        f" | open {int(portfolio.get('open_positions', 0))}"
        f" | gerealiseerd €{float(portfolio.get('realized_pnl_eur', 0)):.2f}"
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
    print(f"MELDINGEN 24U         : {len(report.get('notifications_last_24h', []))}")
    challenger = report.get('human_challenger', {})
    regime = challenger.get('latest_regime', {})
    print(f"MENSELIJK REGIME      : {regime.get('regime', 'ONBEKEND')}")
    print(f"CHALLENGER 24U        : {challenger.get('reviews_last_24h', {})}")
    print('CHALLENGER INVLOED    : GEEN; ALLEEN VERGELIJKEN')
    print('EXTERN MELDKANAAL     : NOG NIET GEKOZEN')


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
        simulate_paper_portfolio(settings, api=api)
        monitor_outcomes(settings, api=api)
        write_notification_feed(settings)
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
            paper = simulate_paper_portfolio(settings, api=api)
            for market in paper['opened']:
                logger.warning('V4 PAPER-POSITIE GEOPEND: %s', market)
            for market in paper['closed']:
                logger.warning('V4 PAPER-POSITIE GESLOTEN: %s', market)
            next_l2 = now + settings.l2_seconds
        if now >= next_outcome:
            monitor_outcomes(settings, api=api)
            next_outcome = now + settings.outcome_seconds
        if now >= next_report:
            write_notification_feed(settings)
            write_report(settings, build_report(settings))
            next_report = now + settings.report_seconds
        time.sleep(1.0)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
