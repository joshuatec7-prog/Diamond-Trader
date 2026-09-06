from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bitvavo_public import BitvavoPublic
from v36_decision import (
    FIFTEEN_MINUTES_MS,
    FIVE_MINUTES_MS,
    ONE_HOUR_MS,
    candle_quality,
    evaluate_jury,
    timeframe_features,
)


logger = logging.getLogger('cryptobot_autonomous_v36')
STOP = False
REPORT_LOCK = threading.Lock()
POSITION_MONITOR_SECONDS = 30
CANDIDATE_RECHECK_SECONDS = 60
TECHNICAL_CHECK_SECONDS = 20
MAX_CANDIDATE_RECHECKS = 5
MAX_TECHNICAL_CONTEXT_AGE_MS = 7 * 60 * 1000
REENTRY_COOLDOWN_MS = 4 * 60 * 60 * 1000
MAX_HOLD_MS = 48 * 60 * 60 * 1000
STOP_NET_PCT = -3.00
TRAIL_ACTIVATE_NET_PCT = 1.00
TRAIL_GIVEBACK_PCT = 1.00
MINIMUM_LOCK_NET_PCT = 0.25
MISSED_HORIZON_MS = 60 * 60 * 1000


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None else float(raw)


def _data_path(filename: str) -> str:
    data = Path('/var/data')
    if data.exists() and os.access(data, os.W_OK):
        return str(data / filename)
    return str(Path('data') / filename)


@dataclass(frozen=True)
class V36Settings:
    mode: str = 'PAPER'
    api_base_url: str = 'https://api.bitvavo.com/v2'
    request_timeout_seconds: int = 10
    request_retries: int = 3
    universe_size: int = 20
    paper_start_eur: float = 3000.0
    weekly_deposit_eur: float = 50.0
    weekly_deposit_anchor: str = '2026-09-06'
    reserve_eur: float = 200.0
    position_eur: float = 500.0
    max_open_positions: int = 5
    entry_score_min: float = 72.0
    max_execution_spread_pct: float = 0.25
    minimum_net_reward_risk: float = 1.15
    taker_fee_pct: float = 0.25
    slippage_pct: float = 0.08
    db_path: str = 'data/cryptobot_autonomous_v36.db'
    report_path: str = 'data/cryptobot_autonomous_v36.json'

    @classmethod
    def from_env(cls) -> 'V36Settings':
        return cls(
            mode=os.getenv('V36_MODE', 'PAPER').upper(),
            api_base_url=os.getenv('BITVAVO_API_BASE_URL', 'https://api.bitvavo.com/v2'),
            request_timeout_seconds=_env_int('REQUEST_TIMEOUT_SECONDS', 10),
            request_retries=_env_int('REQUEST_RETRIES', 3),
            universe_size=_env_int('UNIVERSE_SIZE', 20),
            paper_start_eur=_env_float('V36_PAPER_START_EUR', 3000.0),
            weekly_deposit_eur=_env_float('V36_WEEKLY_DEPOSIT_EUR', 50.0),
            weekly_deposit_anchor=os.getenv('V36_WEEKLY_DEPOSIT_ANCHOR', '2026-09-06'),
            reserve_eur=_env_float('V36_RESERVE_EUR', 200.0),
            position_eur=_env_float('V36_POSITION_EUR', 500.0),
            max_open_positions=_env_int('V36_MAX_OPEN_POSITIONS', 5),
            entry_score_min=_env_float('V36_ENTRY_SCORE_MIN', 72.0),
            max_execution_spread_pct=_env_float('V36_MAX_EXECUTION_SPREAD_PCT', 0.25),
            minimum_net_reward_risk=_env_float('V36_MIN_NET_RR', 1.15),
            taker_fee_pct=_env_float('TAKER_FEE_PCT', 0.25),
            slippage_pct=_env_float('SLIPPAGE_PCT', 0.08),
            db_path=os.getenv('V36_DB_PATH', _data_path('cryptobot_autonomous_v36.db')),
            report_path=os.getenv(
                'V36_REPORT_PATH', _data_path('cryptobot_autonomous_v36.json')
            ),
        )

    def validate(self) -> None:
        if self.mode != 'PAPER':
            raise ValueError('CryptoBot v3.6 staat uitsluitend PAPER-modus toe')
        if not self.api_base_url.startswith('https://'):
            raise ValueError('De publieke API-basis moet HTTPS gebruiken')
        numbers = (
            self.paper_start_eur,
            self.weekly_deposit_eur,
            self.reserve_eur,
            self.position_eur,
            self.entry_score_min,
            self.max_execution_spread_pct,
            self.minimum_net_reward_risk,
            self.taker_fee_pct,
            self.slippage_pct,
        )
        if not all(math.isfinite(value) for value in numbers):
            raise ValueError('v3.6-configuratie bevat een niet-eindige waarde')
        if self.paper_start_eur <= 0.0 or self.position_eur <= 0.0:
            raise ValueError('PAPER-bedragen moeten positief zijn')
        if self.weekly_deposit_eur < 0.0 or self.reserve_eur < 0.0:
            raise ValueError('PAPER-storting en reserve mogen niet negatief zijn')
        if not 1 <= self.universe_size <= 20:
            raise ValueError('v3.6-universum moet 1 tot en met 20 markten bevatten')
        if not 1 <= self.max_open_positions <= 10:
            raise ValueError('v3.6 maximaal open posities is buiten bereik')
        if self.position_eur * self.max_open_positions > self.paper_start_eur - self.reserve_eur:
            raise ValueError('Maximale PAPER-blootstelling is groter dan het startvermogen')
        if not 50.0 <= self.entry_score_min <= 100.0:
            raise ValueError('v3.6 minimale juryscore is buiten bereik')
        if not 0.01 <= self.max_execution_spread_pct <= 1.0:
            raise ValueError('v3.6 maximale L2-spread is buiten bereik')
        date.fromisoformat(self.weekly_deposit_anchor)


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def _connect(settings: V36Settings) -> sqlite3.Connection:
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(
        '''
        CREATE TABLE IF NOT EXISTS v36_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS v36_ledger (
            reference TEXT PRIMARY KEY,
            event_ms INTEGER NOT NULL,
            kind TEXT NOT NULL,
            amount_eur REAL NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS v36_cycles (
            cycle_ms INTEGER PRIMARY KEY,
            evaluated_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            regime TEXT NOT NULL,
            bull_breadth_pct REAL NOT NULL,
            bear_breadth_pct REAL NOT NULL,
            universe_json TEXT NOT NULL,
            valid_markets INTEGER NOT NULL,
            errors_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS v36_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_key TEXT NOT NULL UNIQUE,
            cycle_ms INTEGER NOT NULL,
            evaluated_ms INTEGER NOT NULL,
            evaluation_kind TEXT NOT NULL,
            market TEXT NOT NULL,
            action TEXT NOT NULL,
            eligible INTEGER NOT NULL,
            active_candidate INTEGER NOT NULL,
            score REAL NOT NULL,
            trigger_name TEXT NOT NULL,
            data_quality_status TEXT NOT NULL,
            buy_vwap REAL,
            sell_vwap REAL,
            roundtrip_cost_pct REAL,
            blockers_json TEXT NOT NULL,
            jury_json TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS v36_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_decision_id INTEGER NOT NULL UNIQUE,
            entry_cycle_ms INTEGER NOT NULL,
            opened_ms INTEGER NOT NULL,
            market TEXT NOT NULL,
            entry_price REAL NOT NULL,
            notional_eur REAL NOT NULL,
            base_quantity REAL NOT NULL,
            non_book_cost_pct REAL NOT NULL,
            status TEXT NOT NULL,
            max_net_return_pct REAL NOT NULL DEFAULT 0,
            trailing_floor_pct REAL,
            last_mark_ms INTEGER,
            last_sell_price REAL,
            last_net_return_pct REAL,
            closed_ms INTEGER,
            exit_price REAL,
            exit_reason TEXT,
            net_return_pct REAL,
            pnl_eur REAL,
            FOREIGN KEY(entry_decision_id) REFERENCES v36_decisions(id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_v36_one_open_market
            ON v36_positions(market) WHERE status='OPEN';
        CREATE TABLE IF NOT EXISTS v36_paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            event_ms INTEGER NOT NULL,
            position_id INTEGER NOT NULL,
            market TEXT NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            requested_notional_eur REAL NOT NULL,
            base_quantity REAL NOT NULL,
            executable_price REAL NOT NULL,
            details_json TEXT NOT NULL,
            FOREIGN KEY(position_id) REFERENCES v36_positions(id)
        );
        CREATE TABLE IF NOT EXISTS v36_marks (
            position_id INTEGER NOT NULL,
            mark_ms INTEGER NOT NULL,
            sell_vwap REAL NOT NULL,
            net_return_pct REAL NOT NULL,
            PRIMARY KEY(position_id, mark_ms),
            FOREIGN KEY(position_id) REFERENCES v36_positions(id)
        );
        CREATE TABLE IF NOT EXISTS v36_missed_moves (
            cycle_ms INTEGER NOT NULL,
            market TEXT NOT NULL,
            decision_id INTEGER NOT NULL,
            baseline_price REAL NOT NULL,
            roundtrip_cost_pct REAL NOT NULL,
            max_high REAL NOT NULL,
            min_low REAL NOT NULL,
            last_price REAL NOT NULL,
            best_net_move_pct REAL NOT NULL DEFAULT 0,
            adverse_move_pct REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            finalized_ms INTEGER,
            PRIMARY KEY(cycle_ms, market),
            FOREIGN KEY(decision_id) REFERENCES v36_decisions(id)
        );
        CREATE TABLE IF NOT EXISTS v36_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_ms INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            market TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_v36_decisions_time
            ON v36_decisions(evaluated_ms);
        CREATE INDEX IF NOT EXISTS idx_v36_positions_status
            ON v36_positions(status);
        CREATE INDEX IF NOT EXISTS idx_v36_missed_status
            ON v36_missed_moves(status);
        '''
    )
    conn.commit()
    return conn


def _set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        '''INSERT INTO v36_meta(key,value) VALUES (?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
        (key, str(value)),
    )


def _meta_int(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute('SELECT value FROM v36_meta WHERE key=?', (key,)).fetchone()
    try:
        return int(row['value']) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def _event(
    conn: sqlite3.Connection,
    event_ms: int,
    event_type: str,
    *,
    market: str = '',
    details: object = None,
) -> None:
    conn.execute(
        'INSERT INTO v36_events(event_ms,event_type,market,details_json) VALUES (?,?,?,?)',
        (event_ms, event_type, market, json.dumps(details or {}, ensure_ascii=False)),
    )


def ensure_portfolio(settings: V36Settings, now_ms: int | None = None) -> None:
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    conn = _connect(settings)
    try:
        conn.execute(
            '''INSERT OR IGNORE INTO v36_ledger(reference,event_ms,kind,amount_eur,note)
               VALUES ('INITIAL_V36',?,'INITIAL',?,'Afzonderlijk v3.6 PAPER-startvermogen')''',
            (current_ms, settings.paper_start_eur),
        )
        _set_meta(conn, 'portfolio_version', '3.6')
        _set_meta(conn, 'paper_only', '1')
        conn.commit()
    finally:
        conn.close()


def record_deposit(
    settings: V36Settings,
    *,
    amount_eur: float,
    reference: str,
    event_ms: int | None = None,
    note: str = 'Afzonderlijke PAPER-storting',
) -> bool:
    if not math.isfinite(amount_eur) or amount_eur <= 0.0:
        raise ValueError('PAPER-storting moet positief en eindig zijn')
    if not reference.strip():
        raise ValueError('PAPER-storting vereist een unieke referentie')
    current_ms = int(time.time() * 1000) if event_ms is None else int(event_ms)
    conn = _connect(settings)
    try:
        cursor = conn.execute(
            '''INSERT OR IGNORE INTO v36_ledger(reference,event_ms,kind,amount_eur,note)
               VALUES (?,?,'DEPOSIT',?,?)''',
            (reference.strip(), current_ms, amount_eur, note),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def apply_scheduled_deposits(settings: V36Settings, today: date | None = None) -> int:
    if settings.weekly_deposit_eur <= 0.0:
        return 0
    current_date = date.today() if today is None else today
    anchor = date.fromisoformat(settings.weekly_deposit_anchor)
    scheduled = anchor + timedelta(days=7)
    added = 0
    while scheduled <= current_date:
        event_ms = int(datetime.combine(
            scheduled, datetime.min.time(), tzinfo=timezone.utc
        ).timestamp() * 1000)
        if record_deposit(
            settings,
            amount_eur=settings.weekly_deposit_eur,
            reference=f'WEEKLY_{scheduled.isoformat()}',
            event_ms=event_ms,
            note='Geplande wekelijkse v3.6 PAPER-storting',
        ):
            added += 1
        scheduled += timedelta(days=7)
    return added


def _paper_net_return(entry_price: float, sell_vwap: float, non_book_cost_pct: float) -> float:
    if entry_price <= 0.0 or sell_vwap <= 0.0:
        return -999.0
    return (sell_vwap / entry_price - 1.0) * 100.0 - max(0.0, non_book_cost_pct)


def _capital_rows(conn: sqlite3.Connection) -> tuple[float, float, float, float]:
    row = conn.execute(
        '''SELECT
             COALESCE(SUM(CASE WHEN kind='INITIAL' THEN amount_eur ELSE 0 END),0),
             COALESCE(SUM(CASE WHEN kind='DEPOSIT' THEN amount_eur ELSE 0 END),0),
             COALESCE(SUM(CASE WHEN kind='TRADE_PNL' THEN amount_eur ELSE 0 END),0)
           FROM v36_ledger'''
    ).fetchone()
    open_notional = float(conn.execute(
        "SELECT COALESCE(SUM(notional_eur),0) FROM v36_positions WHERE status='OPEN'"
    ).fetchone()[0])
    return float(row[0]), float(row[1]), float(row[2]), open_notional


def _decision_upsert(
    conn: sqlite3.Connection,
    *,
    decision_key: str,
    cycle_ms: int,
    evaluated_ms: int,
    evaluation_kind: str,
    decision: dict[str, Any],
    context: dict[str, Any],
) -> int:
    details = {'decision': decision, 'context': context}
    conn.execute(
        '''INSERT INTO v36_decisions
           (decision_key,cycle_ms,evaluated_ms,evaluation_kind,market,action,eligible,
            active_candidate,score,trigger_name,data_quality_status,buy_vwap,sell_vwap,
            roundtrip_cost_pct,blockers_json,jury_json,details_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(decision_key) DO UPDATE SET
             evaluated_ms=excluded.evaluated_ms,
             action=excluded.action,
             eligible=excluded.eligible,
             active_candidate=excluded.active_candidate,
             score=excluded.score,
             trigger_name=excluded.trigger_name,
             data_quality_status=excluded.data_quality_status,
             buy_vwap=excluded.buy_vwap,
             sell_vwap=excluded.sell_vwap,
             roundtrip_cost_pct=excluded.roundtrip_cost_pct,
             blockers_json=excluded.blockers_json,
             jury_json=excluded.jury_json,
             details_json=excluded.details_json''',
        (
            decision_key,
            cycle_ms,
            evaluated_ms,
            evaluation_kind,
            str(decision.get('market', context.get('market', ''))),
            str(decision.get('action', 'AFWIJZEN')),
            int(bool(decision.get('eligible'))),
            int(bool(decision.get('active_candidate'))),
            float(decision.get('score', 0.0)),
            str(decision.get('trigger', 'geen')),
            str(decision.get('data_quality_status', 'BLOCK')),
            float(decision.get('buy_vwap', 0.0) or 0.0),
            float(decision.get('sell_vwap', 0.0) or 0.0),
            float(decision.get('roundtrip_cost_pct', 0.0) or 0.0),
            json.dumps(decision.get('blockers', []), ensure_ascii=False),
            json.dumps(decision.get('jury', {}), ensure_ascii=False),
            json.dumps(details, ensure_ascii=False),
        ),
    )
    return int(conn.execute(
        'SELECT id FROM v36_decisions WHERE decision_key=?', (decision_key,)
    ).fetchone()[0])


def _error_decision(market: str, reason: str) -> dict[str, Any]:
    return {
        'market': market,
        'action': 'AFWIJZEN',
        'eligible': False,
        'active_candidate': False,
        'score': 0.0,
        'trigger': 'geen',
        'data_quality_status': 'BLOCK',
        'buy_vwap': 0.0,
        'sell_vwap': 0.0,
        'roundtrip_cost_pct': 0.0,
        'blockers': [reason],
        'jury': {},
    }


def open_paper_position(
    settings: V36Settings,
    *,
    decision_id: int,
    decision: dict[str, Any],
    context: dict[str, Any],
    now_ms: int,
) -> bool:
    """Open één idempotente PAPER-positie binnen één SQLite-transactie."""
    settings.validate()
    market = str(decision.get('market', context.get('market', '')))
    entry_price = float(decision.get('buy_vwap', 0.0) or 0.0)
    sell_price = float(decision.get('sell_vwap', 0.0) or 0.0)
    if not bool(decision.get('eligible')) or not market or entry_price <= 0.0:
        return False
    conn = _connect(settings)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute(
            'SELECT 1 FROM v36_positions WHERE entry_decision_id=?', (decision_id,)
        ).fetchone() is not None:
            conn.rollback()
            return False
        if int(conn.execute(
            "SELECT COUNT(*) FROM v36_positions WHERE status='OPEN'"
        ).fetchone()[0]) >= settings.max_open_positions:
            _event(conn, now_ms, 'ENTRY_BLOCKED_MAX_POSITIONS', market=market)
            conn.commit()
            return False
        if conn.execute(
            "SELECT 1 FROM v36_positions WHERE market=? AND status='OPEN'", (market,)
        ).fetchone() is not None:
            conn.rollback()
            return False
        last_exit = conn.execute(
            "SELECT MAX(closed_ms) FROM v36_positions WHERE market=? AND status='CLOSED'",
            (market,),
        ).fetchone()[0]
        if last_exit is not None and 0 <= now_ms - int(last_exit) < REENTRY_COOLDOWN_MS:
            _event(conn, now_ms, 'ENTRY_BLOCKED_COOLDOWN', market=market)
            conn.commit()
            return False
        initial, deposits, trade_pnl, open_notional = _capital_rows(conn)
        available_cash = initial + deposits + trade_pnl - open_notional
        if available_cash - settings.position_eur + 1e-9 < settings.reserve_eur:
            _event(conn, now_ms, 'ENTRY_BLOCKED_CASH', market=market)
            conn.commit()
            return False
        base_quantity = settings.position_eur / entry_price
        non_book_cost = float(decision.get(
            'non_book_cost_pct', 2.0 * settings.taker_fee_pct + 2.0 * settings.slippage_pct
        ))
        initial_net = _paper_net_return(entry_price, sell_price, non_book_cost)
        cursor = conn.execute(
            '''INSERT INTO v36_positions
               (entry_decision_id,entry_cycle_ms,opened_ms,market,entry_price,notional_eur,
                base_quantity,non_book_cost_pct,status,max_net_return_pct,last_mark_ms,
                last_sell_price,last_net_return_pct)
               VALUES (?,?,?,?,?,?,?,?,'OPEN',?,?,?,?)''',
            (
                decision_id,
                int(context.get('cycle_ms', 0)),
                now_ms,
                market,
                entry_price,
                settings.position_eur,
                base_quantity,
                non_book_cost,
                max(0.0, initial_net),
                now_ms,
                sell_price,
                initial_net,
            ),
        )
        position_id = int(cursor.lastrowid)
        conn.execute(
            '''INSERT INTO v36_paper_orders
               (idempotency_key,event_ms,position_id,market,side,status,reason,
                requested_notional_eur,base_quantity,executable_price,details_json)
               VALUES (?,?,?,?,?,'PAPER_FILLED',?,?,?,?,?)''',
            (
                f'BUY_DECISION_{decision_id}',
                now_ms,
                position_id,
                market,
                'BUY',
                str(decision.get('trigger', 'jury_goedgekeurd')),
                settings.position_eur,
                base_quantity,
                entry_price,
                json.dumps(decision, ensure_ascii=False),
            ),
        )
        conn.execute(
            "UPDATE v36_decisions SET action='PAPER BUY',eligible=1 WHERE id=?",
            (decision_id,),
        )
        conn.execute(
            "UPDATE v36_missed_moves SET status='CONVERTED' WHERE cycle_ms=? AND market=?",
            (int(context.get('cycle_ms', 0)), market),
        )
        _event(conn, now_ms, 'PAPER_BUY', market=market, details={'position_id': position_id})
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    finally:
        conn.close()


def _mark_unexecuted_entry(
    settings: V36Settings,
    *,
    decision_id: int,
    decision: dict[str, Any],
    context: dict[str, Any],
    reason: str,
) -> None:
    """Maak een technisch goedgekeurde maar niet uitvoerbare kans zichtbaar als afwijzing."""
    blockers = list(decision.get('blockers', []))
    if reason not in blockers:
        blockers.append(reason)
    amended = {**decision, 'action': 'AFWIJZEN', 'eligible': False, 'blockers': blockers}
    conn = _connect(settings)
    try:
        row = conn.execute(
            'SELECT details_json FROM v36_decisions WHERE id=?', (decision_id,)
        ).fetchone()
        details = {'decision': amended, 'context': context}
        if row is not None:
            try:
                previous = json.loads(str(row['details_json']))
                if isinstance(previous, dict):
                    details = {**previous, 'decision': amended, 'context': context}
            except (TypeError, ValueError):
                pass
        conn.execute(
            '''UPDATE v36_decisions SET action='AFWIJZEN',eligible=0,
               blockers_json=?,details_json=? WHERE id=?''',
            (
                json.dumps(blockers, ensure_ascii=False),
                json.dumps(details, ensure_ascii=False),
                decision_id,
            ),
        )
        _insert_missed_move(
            conn,
            cycle_ms=int(context.get('cycle_ms', 0)),
            market=str(decision.get('market', context.get('market', ''))),
            decision_id=decision_id,
            decision=amended,
            context=context,
        )
        conn.commit()
    finally:
        conn.close()


def _decision_already_has_position(settings: V36Settings, decision_id: int) -> bool:
    conn = _connect(settings)
    try:
        return conn.execute(
            'SELECT 1 FROM v36_positions WHERE entry_decision_id=?', (decision_id,)
        ).fetchone() is not None
    finally:
        conn.close()


def _restore_executed_decision_status(settings: V36Settings, decision_id: int) -> None:
    conn = _connect(settings)
    try:
        conn.execute(
            "UPDATE v36_decisions SET action='PAPER BUY',eligible=1 WHERE id=?",
            (decision_id,),
        )
        conn.commit()
    finally:
        conn.close()


def monitor_positions(
    settings: V36Settings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    conn = _connect(settings)
    try:
        positions = [dict(row) for row in conn.execute(
            '''SELECT id,opened_ms,market,entry_price,notional_eur,base_quantity,
                      non_book_cost_pct,max_net_return_pct,trailing_floor_pct
               FROM v36_positions WHERE status='OPEN' ORDER BY id'''
        ).fetchall()]
        _set_meta(conn, 'position_monitor_attempted_ms', current_ms)
        conn.commit()
    finally:
        conn.close()

    market_api = api or BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    marks: dict[int, float] = {}
    errors: list[str] = []
    for position in positions:
        try:
            value = market_api.sell_vwap_for_base(
                str(position['market']), float(position['base_quantity'])
            )
            marks[int(position['id'])] = float(value['sell_vwap'])
        except Exception as exc:
            errors.append(f"{position['market']}: {type(exc).__name__}: {exc}")

    conn = _connect(settings)
    try:
        conn.execute('BEGIN IMMEDIATE')
        for position in positions:
            position_id = int(position['id'])
            sell_vwap = marks.get(position_id)
            if sell_vwap is None:
                _event(
                    conn,
                    current_ms,
                    'POSITION_L2_ERROR',
                    market=str(position['market']),
                    details={'errors': errors},
                )
                continue
            net_return = _paper_net_return(
                float(position['entry_price']), sell_vwap, float(position['non_book_cost_pct'])
            )
            maximum = max(float(position['max_net_return_pct']), net_return)
            floor = (
                None if position['trailing_floor_pct'] is None
                else float(position['trailing_floor_pct'])
            )
            if maximum >= TRAIL_ACTIVATE_NET_PCT:
                candidate_floor = max(MINIMUM_LOCK_NET_PCT, maximum - TRAIL_GIVEBACK_PCT)
                floor = candidate_floor if floor is None else max(floor, candidate_floor)
            reason = ''
            if net_return <= STOP_NET_PCT:
                reason = 'STOP'
            elif floor is not None and net_return <= floor:
                reason = 'TRAIL'
            elif floor is None and current_ms - int(position['opened_ms']) >= MAX_HOLD_MS:
                reason = 'TIME'
            conn.execute(
                '''INSERT OR REPLACE INTO v36_marks(position_id,mark_ms,sell_vwap,net_return_pct)
                   VALUES (?,?,?,?)''',
                (position_id, current_ms, sell_vwap, net_return),
            )
            if not reason:
                conn.execute(
                    '''UPDATE v36_positions SET max_net_return_pct=?,trailing_floor_pct=?,
                       last_mark_ms=?,last_sell_price=?,last_net_return_pct=? WHERE id=?''',
                    (maximum, floor, current_ms, sell_vwap, net_return, position_id),
                )
                continue
            pnl_eur = net_return * float(position['notional_eur']) / 100.0
            conn.execute(
                '''UPDATE v36_positions SET status='CLOSED',max_net_return_pct=?,
                   trailing_floor_pct=?,last_mark_ms=?,last_sell_price=?,last_net_return_pct=?,
                   closed_ms=?,exit_price=?,exit_reason=?,net_return_pct=?,pnl_eur=? WHERE id=?''',
                (
                    maximum,
                    floor,
                    current_ms,
                    sell_vwap,
                    net_return,
                    current_ms,
                    sell_vwap,
                    reason,
                    net_return,
                    pnl_eur,
                    position_id,
                ),
            )
            conn.execute(
                '''INSERT OR IGNORE INTO v36_paper_orders
                   (idempotency_key,event_ms,position_id,market,side,status,reason,
                    requested_notional_eur,base_quantity,executable_price,details_json)
                   VALUES (?,?,?,?,?,'PAPER_FILLED',?,?,?,?,?)''',
                (
                    f'SELL_POSITION_{position_id}',
                    current_ms,
                    position_id,
                    str(position['market']),
                    'SELL',
                    reason,
                    float(position['notional_eur']),
                    float(position['base_quantity']),
                    sell_vwap,
                    json.dumps({'net_return_pct': net_return, 'pnl_eur': pnl_eur}),
                ),
            )
            conn.execute(
                '''INSERT OR IGNORE INTO v36_ledger(reference,event_ms,kind,amount_eur,note)
                   VALUES (?,?,'TRADE_PNL',?,?)''',
                (
                    f'POSITION_{position_id}_PNL',
                    current_ms,
                    pnl_eur,
                    f"{position['market']} {reason}",
                ),
            )
            _event(
                conn,
                current_ms,
                'PAPER_SELL',
                market=str(position['market']),
                details={'reason': reason, 'pnl_eur': pnl_eur},
            )
        if marks or not positions:
            _set_meta(conn, 'position_monitor_generated_ms', current_ms)
        _set_meta(conn, 'position_monitor_errors', json.dumps(errors, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return {'positions': len(positions), 'priced': len(marks), 'errors': errors}


def _quality_and_features(
    candles: list,
    *,
    interval_ms: int,
    now_ms: int,
    allow_one_small_gap: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    quality = candle_quality(
        candles,
        interval_ms=interval_ms,
        now_ms=now_ms,
        allow_one_small_gap=allow_one_small_gap,
    )
    features = timeframe_features(candles) if quality['valid'] else {
        'valid': False,
        'reason': quality['reason'],
    }
    if quality['valid'] and not bool(features.get('valid')):
        quality = {
            **quality,
            'valid': False,
            'status': 'BLOCK',
            'reason': str(features.get('reason', 'indicatoren_onbetrouwbaar')),
            'score': 0.0,
        }
    return quality, features


def _market_context(
    api: BitvavoPublic,
    market: str,
    now_ms: int,
) -> dict[str, Any]:
    five_candles = api.closed_candles(market, '5m', 80, now_ms=now_ms)
    fifteen_candles = api.closed_candles(market, '15m', 80, now_ms=now_ms)
    hour_candles = api.closed_candles(market, '1h', 80, now_ms=now_ms)
    quality_five, five = _quality_and_features(
        five_candles,
        interval_ms=FIVE_MINUTES_MS,
        now_ms=now_ms,
        allow_one_small_gap=True,
    )
    quality_fifteen, fifteen = _quality_and_features(
        fifteen_candles,
        interval_ms=FIFTEEN_MINUTES_MS,
        now_ms=now_ms,
        allow_one_small_gap=False,
    )
    quality_hour, hour = _quality_and_features(
        hour_candles,
        interval_ms=ONE_HOUR_MS,
        now_ms=now_ms,
        allow_one_small_gap=False,
    )
    return {
        'market': market,
        'five': five,
        'fifteen': fifteen,
        'hour': hour,
        'quality': {
            'five': quality_five,
            'fifteen': quality_fifteen,
            'hour': quality_hour,
        },
        '_five_candles': five_candles,
    }


def _update_missed_moves(
    conn: sqlite3.Connection,
    *,
    market: str,
    candles: list,
    current_cycle_ms: int,
    now_ms: int,
) -> None:
    for row in conn.execute(
        '''SELECT cycle_ms,baseline_price,roundtrip_cost_pct,max_high,min_low
           FROM v36_missed_moves WHERE market=? AND status='PENDING' AND cycle_ms<?''',
        (market, current_cycle_ms),
    ).fetchall():
        source_ms = int(row['cycle_ms'])
        later = [candle for candle in candles if source_ms < int(candle.timestamp_ms) <= current_cycle_ms]
        if not later:
            continue
        baseline = float(row['baseline_price'])
        maximum = max(float(row['max_high']), *(float(candle.high) for candle in later))
        minimum = min(float(row['min_low']), *(float(candle.low) for candle in later))
        last_price = float(later[-1].close)
        best_net = (maximum / baseline - 1.0) * 100.0 - float(row['roundtrip_cost_pct'])
        adverse = (minimum / baseline - 1.0) * 100.0
        finalized = current_cycle_ms - source_ms >= MISSED_HORIZON_MS
        conn.execute(
            '''UPDATE v36_missed_moves SET max_high=?,min_low=?,last_price=?,
               best_net_move_pct=?,adverse_move_pct=?,status=?,finalized_ms=?
               WHERE cycle_ms=? AND market=?''',
            (
                maximum,
                minimum,
                last_price,
                best_net,
                adverse,
                'FINAL' if finalized else 'PENDING',
                now_ms if finalized else None,
                source_ms,
                market,
            ),
        )


def _insert_missed_move(
    conn: sqlite3.Connection,
    *,
    cycle_ms: int,
    market: str,
    decision_id: int,
    decision: dict[str, Any],
    context: dict[str, Any],
) -> None:
    baseline = float(decision.get('buy_vwap', 0.0) or 0.0)
    if baseline <= 0.0:
        baseline = float(context.get('five', {}).get('close', 0.0) or 0.0)
    if baseline <= 0.0:
        return
    conn.execute(
        '''INSERT OR IGNORE INTO v36_missed_moves
           (cycle_ms,market,decision_id,baseline_price,roundtrip_cost_pct,max_high,
            min_low,last_price,status) VALUES (?,?,?,?,?,?,?,?,'PENDING')''',
        (
            cycle_ms,
            market,
            decision_id,
            baseline,
            float(decision.get('roundtrip_cost_pct', 0.0) or 0.0),
            baseline,
            baseline,
            baseline,
        ),
    )


def evaluate_new_five_minute_cycle(
    settings: V36Settings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Beoordeel alle 20 EUR-markten één keer per nieuw gesloten 5m-interval."""
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    market_api = api or BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    conn = _connect(settings)
    try:
        _set_meta(conn, 'cycle_attempted_ms', current_ms)
        conn.commit()
    finally:
        conn.close()

    try:
        universe = market_api.top_markets_by_quote_volume('EUR', settings.universe_size)
        bitcoin_candles = market_api.closed_candles('BTC-EUR', '5m', 80, now_ms=current_ms)
    except Exception as exc:
        conn = _connect(settings)
        try:
            _event(conn, current_ms, 'UNIVERSE_OR_BTC_API_ERROR', details={
                'error': f'{type(exc).__name__}: {exc}'
            })
            _set_meta(conn, 'cycle_errors', json.dumps([str(exc)], ensure_ascii=False))
            conn.commit()
        finally:
            conn.close()
        return {'evaluated': False, 'reason': 'universe_of_bitcoin_api_error'}

    cycle_ms = (
        int(bitcoin_candles[-1].timestamp_ms)
        if bitcoin_candles
        else current_ms // FIVE_MINUTES_MS * FIVE_MINUTES_MS - FIVE_MINUTES_MS
    )
    conn = _connect(settings)
    try:
        existing = conn.execute(
            "SELECT status FROM v36_cycles WHERE cycle_ms=?", (cycle_ms,)
        ).fetchone()
        if existing is not None and str(existing['status']) == 'COMPLETE':
            return {'evaluated': False, 'reason': 'cycle_al_voltooid', 'cycle_ms': cycle_ms}
        conn.execute(
            '''INSERT INTO v36_cycles
               (cycle_ms,evaluated_ms,status,regime,bull_breadth_pct,bear_breadth_pct,
                universe_json,valid_markets,errors_json)
               VALUES (?,?,'RUNNING','UNKNOWN',0,0,?,0,'[]')
               ON CONFLICT(cycle_ms) DO UPDATE SET evaluated_ms=excluded.evaluated_ms,
                 status='RUNNING',universe_json=excluded.universe_json''',
            (cycle_ms, current_ms, json.dumps(universe, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()

    btc_quality, bitcoin = _quality_and_features(
        bitcoin_candles,
        interval_ms=FIVE_MINUTES_MS,
        now_ms=current_ms,
        allow_one_small_gap=True,
    )
    contexts: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for market in universe:
        try:
            contexts[market] = _market_context(market_api, market, current_ms)
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    trend_valid = [
        context for context in contexts.values()
        if bool(context['fifteen'].get('valid')) and bool(context['hour'].get('valid'))
    ]
    bull_count = sum(
        bool(context['fifteen'].get('trend_up')) and bool(context['hour'].get('trend_up'))
        for context in trend_valid
    )
    bear_count = sum(
        bool(context['fifteen'].get('trend_down')) and bool(context['hour'].get('trend_down'))
        for context in trend_valid
    )
    denominator = max(1, len(trend_valid))
    bull_breadth = bull_count / denominator * 100.0
    bear_breadth = bear_count / denominator * 100.0
    if len(trend_valid) < max(1, math.ceil(settings.universe_size * 0.80)):
        regime = 'DATA_UNCERTAIN'
    elif bull_breadth >= 60.0:
        regime = 'BULL'
    elif bear_breadth >= 60.0:
        regime = 'BEAR'
    else:
        regime = 'SIDEWAYS'

    depths: dict[str, dict[str, Any] | None] = {}
    for market in universe:
        if market not in contexts:
            continue
        try:
            depths[market] = market_api.depth_book(market, settings.position_eur)
        except Exception as exc:
            depths[market] = None
            errors.append(f'{market} L2: {type(exc).__name__}: {exc}')

    evaluated: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    conn = _connect(settings)
    try:
        conn.execute('BEGIN IMMEDIATE')
        for market in universe:
            context = contexts.get(market)
            if context is None:
                reason = next(
                    (item for item in errors if item.startswith(f'{market}:')),
                    f'{market}: marktdata_ontbreekt',
                )
                decision = _error_decision(market, reason)
                context = {'market': market, 'cycle_ms': cycle_ms}
                decision_id = _decision_upsert(
                    conn,
                    decision_key=f'5M:{cycle_ms}:{market}',
                    cycle_ms=cycle_ms,
                    evaluated_ms=current_ms,
                    evaluation_kind='NEW_5M',
                    decision=decision,
                    context=context,
                )
                _event(conn, current_ms, 'MARKET_DATA_ERROR', market=market, details={'reason': reason})
                continue
            context['cycle_ms'] = cycle_ms
            context['regime'] = regime
            context['bull_breadth_pct'] = round(bull_breadth, 1)
            context['bear_breadth_pct'] = round(bear_breadth, 1)
            context['bitcoin'] = bitcoin
            context['quality']['bitcoin'] = btc_quality
            _update_missed_moves(
                conn,
                market=market,
                candles=context['_five_candles'],
                current_cycle_ms=cycle_ms,
                now_ms=current_ms,
            )
            context.pop('_five_candles', None)
            depth = depths.get(market)
            if depth is None:
                _event(
                    conn,
                    current_ms,
                    'ENTRY_L2_ERROR',
                    market=market,
                    details={'error': next(
                        (item for item in errors if item.startswith(f'{market} L2:')),
                        'L2-data ontbreekt',
                    )},
                )
            decision = evaluate_jury(
                context=context,
                depth=depth,
                entry_score_min=settings.entry_score_min,
                max_spread_pct=settings.max_execution_spread_pct,
                minimum_rr=settings.minimum_net_reward_risk,
                taker_fee_pct=settings.taker_fee_pct,
                slippage_pct=settings.slippage_pct,
                stop_net_pct=STOP_NET_PCT,
                now_ms=current_ms,
            )
            decision_id = _decision_upsert(
                conn,
                decision_key=f'5M:{cycle_ms}:{market}',
                cycle_ms=cycle_ms,
                evaluated_ms=current_ms,
                evaluation_kind='NEW_5M',
                decision=decision,
                context=context,
            )
            evaluated.append((decision_id, decision, context))
            if not bool(decision.get('eligible')):
                _insert_missed_move(
                    conn,
                    cycle_ms=cycle_ms,
                    market=market,
                    decision_id=decision_id,
                    decision=decision,
                    context=context,
                )
        conn.execute(
            '''UPDATE v36_cycles SET evaluated_ms=?,status='DECIDED',regime=?,
               bull_breadth_pct=?,bear_breadth_pct=?,valid_markets=?,errors_json=?
               WHERE cycle_ms=?''',
            (
                current_ms,
                regime,
                bull_breadth,
                bear_breadth,
                len(trend_valid),
                json.dumps(errors, ensure_ascii=False),
                cycle_ms,
            ),
        )
        _set_meta(conn, 'cycle_errors', json.dumps(errors, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()

    opened = 0
    for decision_id, decision, context in sorted(
        evaluated, key=lambda item: float(item[1].get('score', 0.0)), reverse=True
    ):
        if bool(decision.get('eligible')):
            if open_paper_position(
                settings,
                decision_id=decision_id,
                decision=decision,
                context=context,
                now_ms=current_ms,
            ):
                opened += 1
            elif _decision_already_has_position(settings, decision_id):
                _restore_executed_decision_status(settings, decision_id)
            else:
                _mark_unexecuted_entry(
                    settings,
                    decision_id=decision_id,
                    decision=decision,
                    context=context,
                    reason='paper_portefeuillegrens_of_dubbele_instap',
                )
    conn = _connect(settings)
    try:
        conn.execute(
            "UPDATE v36_cycles SET status='COMPLETE' WHERE cycle_ms=?",
            (cycle_ms,),
        )
        _set_meta(conn, 'cycle_generated_ms', int(time.time() * 1000) if now_ms is None else current_ms)
        conn.commit()
    finally:
        conn.close()
    return {
        'evaluated': True,
        'cycle_ms': cycle_ms,
        'universe_count': len(universe),
        'valid_markets': len(trend_valid),
        'decisions': len(evaluated),
        'opened': opened,
        'errors': errors,
    }


def recheck_active_candidates(
    settings: V36Settings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    conn = _connect(settings)
    try:
        _set_meta(conn, 'candidate_recheck_attempted_ms', current_ms)
        cycle = conn.execute(
            "SELECT cycle_ms FROM v36_cycles WHERE status='COMPLETE' ORDER BY cycle_ms DESC LIMIT 1"
        ).fetchone()
        conn.commit()
        if cycle is None:
            return {'checked': 0, 'opened': 0, 'errors': ['nog geen volledige 5m-cyclus']}
        cycle_ms = int(cycle['cycle_ms'])
        if current_ms - (cycle_ms + FIVE_MINUTES_MS) > MAX_TECHNICAL_CONTEXT_AGE_MS:
            return {'checked': 0, 'opened': 0, 'errors': ['5m-context verouderd']}
        rows = conn.execute(
            '''SELECT id,market,score,details_json FROM v36_decisions
               WHERE cycle_ms=? AND evaluation_kind='NEW_5M' AND active_candidate=1
               ORDER BY score DESC LIMIT ?''',
            (cycle_ms, MAX_CANDIDATE_RECHECKS),
        ).fetchall()
    finally:
        conn.close()

    market_api = api or BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    errors: list[str] = []
    checked = 0
    opened = 0
    minute_bucket = current_ms // 60_000
    for row in rows:
        market = str(row['market'])
        conn = _connect(settings)
        try:
            if conn.execute(
                "SELECT 1 FROM v36_positions WHERE market=? AND status='OPEN'", (market,)
            ).fetchone() is not None:
                continue
            key = f'L2:{cycle_ms}:{minute_bucket}:{market}'
            if conn.execute(
                'SELECT 1 FROM v36_decisions WHERE decision_key=?', (key,)
            ).fetchone() is not None:
                continue
        finally:
            conn.close()
        try:
            details = json.loads(str(row['details_json']))
            context = details['context']
            depth = market_api.depth_book(market, settings.position_eur)
            decision = evaluate_jury(
                context=context,
                depth=depth,
                entry_score_min=settings.entry_score_min,
                max_spread_pct=settings.max_execution_spread_pct,
                minimum_rr=settings.minimum_net_reward_risk,
                taker_fee_pct=settings.taker_fee_pct,
                slippage_pct=settings.slippage_pct,
                stop_net_pct=STOP_NET_PCT,
                now_ms=current_ms,
            )
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')
            decision = _error_decision(market, f'l2_hercontrole_fout: {type(exc).__name__}: {exc}')
            context = {'market': market, 'cycle_ms': cycle_ms}
        conn = _connect(settings)
        try:
            decision_id = _decision_upsert(
                conn,
                decision_key=f'L2:{cycle_ms}:{minute_bucket}:{market}',
                cycle_ms=cycle_ms,
                evaluated_ms=current_ms,
                evaluation_kind='L2_RECHECK',
                decision=decision,
                context=context,
            )
            conn.commit()
        finally:
            conn.close()
        checked += 1
        if bool(decision.get('eligible')):
            if open_paper_position(
                settings,
                decision_id=decision_id,
                decision=decision,
                context=context,
                now_ms=current_ms,
            ):
                opened += 1
            elif _decision_already_has_position(settings, decision_id):
                _restore_executed_decision_status(settings, decision_id)
            else:
                _mark_unexecuted_entry(
                    settings,
                    decision_id=decision_id,
                    decision=decision,
                    context=context,
                    reason='paper_portefeuillegrens_of_dubbele_instap',
                )

    conn = _connect(settings)
    try:
        if checked or not rows:
            _set_meta(conn, 'candidate_recheck_generated_ms', current_ms)
        _set_meta(conn, 'candidate_recheck_errors', json.dumps(errors, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return {'checked': checked, 'opened': opened, 'errors': errors}


def build_report(settings: V36Settings, now_ms: int | None = None) -> dict[str, Any]:
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    conn = _connect(settings)
    try:
        initial, deposits, trade_pnl, open_notional = _capital_rows(conn)
        open_pnl = float(conn.execute(
            '''SELECT COALESCE(SUM(last_net_return_pct*notional_eur/100.0),0)
               FROM v36_positions WHERE status='OPEN' AND last_net_return_pct IS NOT NULL'''
        ).fetchone()[0])
        counts = conn.execute(
            '''SELECT COUNT(*),
                      SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='CLOSED' AND pnl_eur>0 THEN 1 ELSE 0 END),
                      COALESCE(SUM(CASE WHEN status='CLOSED' AND pnl_eur>0 THEN pnl_eur ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN status='CLOSED' AND pnl_eur<0 THEN -pnl_eur ELSE 0 END),0)
               FROM v36_positions'''
        ).fetchone()
        total = int(counts[0] or 0)
        open_count = int(counts[1] or 0)
        closed = int(counts[2] or 0)
        wins = int(counts[3] or 0)
        positive = float(counts[4] or 0.0)
        negative = float(counts[5] or 0.0)
        profit_factor = positive / negative if negative > 0.0 else (999.0 if positive > 0.0 else 0.0)
        open_positions = [dict(row) for row in conn.execute(
            '''SELECT market,opened_ms,notional_eur,base_quantity,last_sell_price,
                      last_net_return_pct,max_net_return_pct,trailing_floor_pct,last_mark_ms
               FROM v36_positions WHERE status='OPEN' ORDER BY opened_ms'''
        ).fetchall()]
        for position in open_positions:
            position['age_hours'] = round(
                max(0.0, current_ms - int(position['opened_ms'])) / 3_600_000.0, 2
            )
            position['pnl_eur'] = (
                None if position['last_net_return_pct'] is None
                else round(float(position['last_net_return_pct']) * float(position['notional_eur']) / 100.0, 2)
            )
        latest_cycle_row = conn.execute(
            '''SELECT cycle_ms,evaluated_ms,status,regime,bull_breadth_pct,bear_breadth_pct,
                      universe_json,valid_markets,errors_json
               FROM v36_cycles ORDER BY cycle_ms DESC LIMIT 1'''
        ).fetchone()
        latest_cycle = dict(latest_cycle_row) if latest_cycle_row is not None else {}
        if latest_cycle:
            latest_cycle['universe'] = json.loads(latest_cycle.pop('universe_json'))
            latest_cycle['errors'] = json.loads(latest_cycle.pop('errors_json'))
        latest_decisions: list[dict[str, Any]] = []
        for row in conn.execute(
            '''SELECT evaluated_ms,evaluation_kind,market,action,score,trigger_name,
                      data_quality_status,blockers_json,jury_json
               FROM v36_decisions ORDER BY evaluated_ms DESC,score DESC LIMIT 8'''
        ).fetchall():
            item = dict(row)
            item['blockers'] = json.loads(item.pop('blockers_json'))
            item['jury'] = json.loads(item.pop('jury_json'))
            latest_decisions.append(item)
        cutoff = current_ms - 24 * 60 * 60 * 1000
        decision_count = int(conn.execute(
            'SELECT COUNT(*) FROM v36_decisions WHERE evaluated_ms>=?', (cutoff,)
        ).fetchone()[0])
        blocker_counts: dict[str, int] = {}
        for row in conn.execute(
            'SELECT blockers_json FROM v36_decisions WHERE evaluated_ms>=?', (cutoff,)
        ).fetchall():
            try:
                blockers = json.loads(str(row[0]))
            except (TypeError, ValueError):
                blockers = []
            for blocker in set(str(value) for value in blockers if isinstance(blockers, list)):
                blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
        top_blockers = [
            {'reason': reason, 'count': count}
            for reason, count in sorted(blocker_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
        ]
        missed = conn.execute(
            '''SELECT COUNT(*),
                      SUM(CASE WHEN status='FINAL' THEN 1 ELSE 0 END),
                      COALESCE(AVG(CASE WHEN status='FINAL' THEN best_net_move_pct END),0),
                      SUM(CASE WHEN status='FINAL' AND best_net_move_pct>0 THEN 1 ELSE 0 END)
               FROM v36_missed_moves'''
        ).fetchone()
        deposits_list = [dict(row) for row in conn.execute(
            '''SELECT reference,event_ms,amount_eur,note FROM v36_ledger
               WHERE kind='DEPOSIT' ORDER BY event_ms DESC LIMIT 5'''
        ).fetchall()]
        balance = initial
        peak = balance
        max_drawdown = 0.0
        for row in conn.execute(
            "SELECT pnl_eur FROM v36_positions WHERE status='CLOSED' ORDER BY closed_ms,id"
        ).fetchall():
            balance += float(row[0])
            peak = max(peak, balance)
            if peak > 0.0:
                max_drawdown = max(max_drawdown, (peak - balance) / peak * 100.0)
        first_open = conn.execute('SELECT MIN(opened_ms) FROM v36_positions').fetchone()[0]
        span_days = 0.0 if first_open is None else max(0.0, current_ms - int(first_open)) / 86_400_000.0
        evaluation = 'VERZAMELEN'
        if closed >= 40 and span_days >= 14.0:
            evaluation = (
                'PAPER KANDIDAAT'
                if profit_factor >= 1.25 and max_drawdown <= 10.0 and trade_pnl > 0.0
                else 'ONVOLDOENDE'
            )
        report = {
            'version': '3.6',
            'component': 'AUTONOMOUS_PAPERBOT',
            'generated_at_ms': current_ms,
            'generated_at_utc': datetime.fromtimestamp(
                current_ms / 1000.0, tz=timezone.utc
            ).isoformat(),
            'modes': {
                'scanner_advice': 'HANDMATIG / GEEN ORDERS',
                'autonomous_paperbot': 'AUTOMATISCHE PAPER-INSTAP EN -UITSTAP',
                'live_orders': 'UIT / TECHNISCH ONMOGELIJK',
            },
            'portfolio': {
                'start_eur': round(initial, 2),
                'deposits_eur': round(deposits, 2),
                'trading_pnl_eur': round(trade_pnl, 2),
                'open_pnl_eur': round(open_pnl, 2),
                'cash_eur': round(initial + deposits + trade_pnl - open_notional, 2),
                'reserve_eur': round(settings.reserve_eur, 2),
                'available_above_reserve_eur': round(max(
                    0.0,
                    initial + deposits + trade_pnl - open_notional - settings.reserve_eur,
                ), 2),
                'equity_eur': round(initial + deposits + trade_pnl + open_pnl, 2),
                'open_notional_eur': round(open_notional, 2),
                'entries': total,
                'open': open_count,
                'closed': closed,
                'wins': wins,
                'losses': closed - wins,
                'profit_factor': round(profit_factor, 3),
                'max_drawdown_pct': round(max_drawdown, 3),
                'test_span_days': round(span_days, 2),
                'evaluation': evaluation,
                'open_positions': open_positions,
                'recent_deposits': deposits_list,
            },
            'latest_cycle': latest_cycle,
            'jury': {
                'decisions_24h': decision_count,
                'latest_decisions': latest_decisions,
                'top_blockers_24h': top_blockers,
            },
            'missed_moves': {
                'tracked': int(missed[0] or 0),
                'finalized': int(missed[1] or 0),
                'average_best_net_pct': round(float(missed[2] or 0.0), 3),
                'positive_after_rejection': int(missed[3] or 0),
                'horizon_minutes': 60,
            },
            'heartbeat': {
                'cycle_attempted_ms': _meta_int(conn, 'cycle_attempted_ms'),
                'cycle_generated_ms': _meta_int(conn, 'cycle_generated_ms'),
                'candidate_recheck_attempted_ms': _meta_int(conn, 'candidate_recheck_attempted_ms'),
                'candidate_recheck_generated_ms': _meta_int(conn, 'candidate_recheck_generated_ms'),
                'position_monitor_attempted_ms': _meta_int(conn, 'position_monitor_attempted_ms'),
                'position_monitor_generated_ms': _meta_int(conn, 'position_monitor_generated_ms'),
            },
            'rules': {
                'universe': f'top {settings.universe_size} EUR-markten op 24u-volume',
                'full_universe_every_new_closed_5m': True,
                'context_intervals': ['15m', '1h'],
                'entry_interval': '5m',
                'candidate_recheck_seconds': CANDIDATE_RECHECK_SECONDS,
                'position_monitor_seconds': POSITION_MONITOR_SECONDS,
                'position_eur': settings.position_eur,
                'max_open_positions': settings.max_open_positions,
                'minimum_cash_reserve_eur': settings.reserve_eur,
                'position_source': 'ALLEEN NIEUWE PAPER-KOOP VANUIT EUR-CASH',
                'existing_coins': 'UITGESLOTEN / WORDEN NIET GEBRUIKT',
                'entry_score_min': settings.entry_score_min,
                'stop_net_pct': STOP_NET_PCT,
                'trail_activate_net_pct': TRAIL_ACTIVATE_NET_PCT,
                'trail_giveback_pct': TRAIL_GIVEBACK_PCT,
                'minimum_locked_net_pct': MINIMUM_LOCK_NET_PCT,
                'reentry_cooldown_hours': 4,
                'one_small_5m_gap': 'TOEGESTAAN MET 4 PUNTEN STRAF',
                'multiple_or_large_gaps': 'BLOKKEREN',
                'news': 'NIET ACTIEF',
            },
        }
        return report
    finally:
        conn.close()


def write_report(settings: V36Settings, report: dict[str, Any]) -> None:
    with REPORT_LOCK:
        path = Path(settings.report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)


def load_report(settings: V36Settings) -> dict[str, Any] | None:
    path = Path(settings.report_path)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def print_status(report: dict[str, Any]) -> None:
    portfolio = report.get('portfolio', {})
    cycle = report.get('latest_cycle', {})
    jury = report.get('jury', {})
    rules = report.get('rules', {})
    missed = report.get('missed_moves', {})
    modes = report.get('modes', {})
    age = max(0.0, time.time() * 1000.0 - int(report.get('generated_at_ms', 0))) / 1000.0
    print('=== CRYPTOBOT v3.6 | AUTONOME PAPERBOT ===')
    print(f"UTC                   : {report.get('generated_at_utc', 'n/a')}")
    print(f'RAPPORT               : {"ACTUEEL" if age <= 300 else "VEROUDERD"} | {age:.0f} sec oud')
    print(f"SCANNERADVIES         : {modes.get('scanner_advice', 'HANDMATIG / GEEN ORDERS')}")
    print(f"AUTONOME PAPERBOT     : {modes.get('autonomous_paperbot', 'AUTOMATISCHE PAPER-INSTAP EN -UITSTAP')}")
    print(f"LIVE ORDERS           : {modes.get('live_orders', 'UIT / TECHNISCH ONMOGELIJK')}")
    print()
    print('=== AFZONDERLIJKE v3.6 PAPER-PORTEFEUILLE ===')
    print(
        f"start €{float(portfolio.get('start_eur',0)):.2f}"
        f" | stortingen €{float(portfolio.get('deposits_eur',0)):.2f}"
        f" | handels-PnL €{float(portfolio.get('trading_pnl_eur',0)):+.2f}"
    )
    print(
        f"cash €{float(portfolio.get('cash_eur',0)):.2f}"
        f" | equity €{float(portfolio.get('equity_eur',0)):.2f}"
        f" | open inzet €{float(portfolio.get('open_notional_eur',0)):.2f}"
    )
    print(
        f"vaste reserve €{float(portfolio.get('reserve_eur',0)):.2f}"
        f" | beschikbaar boven reserve €{float(portfolio.get('available_above_reserve_eur',0)):.2f}"
    )
    print(
        f"entries {int(portfolio.get('entries',0))} | open {int(portfolio.get('open',0))}"
        f" | gesloten {int(portfolio.get('closed',0))}"
        f" | W/L {int(portfolio.get('wins',0))}/{int(portfolio.get('losses',0))}"
        f" | PF {float(portfolio.get('profit_factor',0)):.3f}"
    )
    print(f"evaluatie             : {portfolio.get('evaluation','VERZAMELEN')}")
    for position in portfolio.get('open_positions', []):
        net = position.get('last_net_return_pct')
        net_text = 'geen actuele L2-prijs' if net is None else f"{float(net):+.3f}% / €{float(position.get('pnl_eur',0)):+.2f}"
        floor = position.get('trailing_floor_pct')
        print(
            f"  OPEN {str(position.get('market','?')):12s} | netto {net_text}"
            f" | max {float(position.get('max_net_return_pct',0)):+.3f}%"
            f" | trailing {'uit' if floor is None else f'{float(floor):+.3f}%'}"
        )
    print()
    print('=== VOLLEDIG EUR-UNIVERSUM EN BESLISJURY ===')
    universe = cycle.get('universe', []) if isinstance(cycle, dict) else []
    print(
        f"laatste 5m-cyclus      : {cycle.get('cycle_ms','nog geen')} | {cycle.get('status','WACHTEN')}"
        f" | regime {cycle.get('regime','UNKNOWN')}"
    )
    print(
        f"volledig beoordeeld   : {len(universe)}/{len(universe)} EUR-markten"
    )
    print(
        f"betrouwbare context   : {int(cycle.get('valid_markets',0))}/{len(universe)} markten"
        f" | BULL {float(cycle.get('bull_breadth_pct',0)):.1f}%"
        f" | BEAR {float(cycle.get('bear_breadth_pct',0)):.1f}%"
    )
    print(
        f"jurybesluiten 24u      : {int(jury.get('decisions_24h',0))}"
        f" | gemiste bewegingen {int(missed.get('finalized',0))}/{int(missed.get('tracked',0))} afgerond"
    )
    for item in jury.get('latest_decisions', [])[:5]:
        blockers = item.get('blockers', [])
        reason = ', '.join(str(value) for value in blockers[:2]) if isinstance(blockers, list) else ''
        print(
            f"  {str(item.get('market','?')):12s} | {str(item.get('action','AFWIJZEN')):10s}"
            f" | score {float(item.get('score',0)):4.1f}"
            f" | data {item.get('data_quality_status','BLOCK')}"
            + (f' | {reason}' if reason else '')
        )
    latest_with_jury = next(
        (item for item in jury.get('latest_decisions', []) if item.get('jury')),
        None,
    )
    if isinstance(latest_with_jury, dict):
        print(f"jury laatste besluit   : {latest_with_jury.get('market','?')}")
        labels = {
            'markt_en_trend': 'markt + 15m/1h',
            'timing_en_momentum': '5m timing',
            'volume_en_bevestiging': 'volume',
            'bitcoin_en_marktschok': 'Bitcoin/schok',
            'orderboek_en_spread': 'L2/spread',
            'netto_risico_opbrengst': 'netto R/R + kosten',
            'anti_pump': 'anti-pump',
            'datakwaliteit': 'datakwaliteit',
        }
        for key, vote in latest_with_jury.get('jury', {}).items():
            print(
                f"  {labels.get(key,key):20s} | {vote.get('vote','TEGEN'):5s}"
                f" | {float(vote.get('score',0)):.1f}/{float(vote.get('maximum',0)):.0f}"
                f" | {vote.get('reason','')}"
            )
    top_blockers = jury.get('top_blockers_24h', [])
    if top_blockers:
        print('belangrijkste blokkades:')
        for item in top_blockers[:5]:
            print(f"  {item.get('reason','onbekend')}: {int(item.get('count',0))}")
    print()
    print(
        f"regels                : €{float(rules.get('position_eur',0)):.0f} per positie"
        f" | max {int(rules.get('max_open_positions',0))} tegelijk"
        f" | reserve €{float(rules.get('minimum_cash_reserve_eur',0)):.0f}"
        f" | juryscore ≥ {float(rules.get('entry_score_min',0)):.0f}"
    )
    print('bestaande munten      : UITGESLOTEN; alleen nieuwe PAPER-koop vanuit EUR-cash')
    print('5m volledig universum | kandidaten 60 sec L2 | open posities 30 sec exacte muntomvang')
    print('één klein 5m-gat: scorestraf | meerdere/grote gaten of stale data: blokkeren')
    print('nieuwslaag             : NIET ACTIEF')


def _run_once(settings: V36Settings, api: BitvavoPublic | None = None) -> None:
    now_ms = int(time.time() * 1000)
    ensure_portfolio(settings, now_ms)
    apply_scheduled_deposits(settings)
    monitor_positions(settings, api=api, now_ms=now_ms)
    evaluate_new_five_minute_cycle(settings, api=api, now_ms=now_ms)
    recheck_active_candidates(settings, api=api, now_ms=now_ms)
    write_report(settings, build_report(settings, now_ms))


def _refresh_report(settings: V36Settings) -> None:
    write_report(settings, build_report(settings))


def _periodic_worker(
    settings: V36Settings,
    *,
    name: str,
    interval_seconds: int,
    action,
) -> None:
    market_api = BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    next_run = 0.0
    while not STOP:
        now_monotonic = time.monotonic()
        if now_monotonic < next_run:
            time.sleep(min(0.5, next_run - now_monotonic))
            continue
        next_run = now_monotonic + interval_seconds
        try:
            action(market_api)
        except Exception as exc:
            logger.exception('v3.6 %s-workeractie mislukt: %s', name, exc)
        try:
            _refresh_report(settings)
        except Exception as exc:
            logger.exception('v3.6 rapport na %s mislukt: %s', name, exc)


def main() -> int:
    parser = argparse.ArgumentParser(description='CryptoBot v3.6 autonome PAPER-worker')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--paper-deposit', type=float)
    parser.add_argument('--deposit-reference')
    args = parser.parse_args()
    settings = V36Settings.from_env()
    settings.validate()
    logging.basicConfig(
        level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    ensure_portfolio(settings)
    if args.paper_deposit is not None:
        if not args.deposit_reference:
            parser.error('--paper-deposit vereist --deposit-reference')
        added = record_deposit(
            settings,
            amount_eur=args.paper_deposit,
            reference=args.deposit_reference,
        )
        write_report(settings, build_report(settings))
        print('PAPER-storting geboekt' if added else 'PAPER-storting bestond al; niet dubbel geboekt')
        return 0
    if args.status:
        report = load_report(settings)
        if report is None:
            print('=== CRYPTOBOT v3.6 | AUTONOME PAPERBOT ===')
            print('STATUS                : nog geen rapport beschikbaar')
            return 1
        print_status(report)
        age_ms = int(time.time() * 1000) - int(report.get('generated_at_ms', 0))
        return 2 if age_ms > 5 * 60 * 1000 else 0

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if args.once:
        try:
            market_api = BitvavoPublic(
                settings.api_base_url,
                settings.request_timeout_seconds,
                settings.request_retries,
            )
            _run_once(settings, market_api)
            print_status(build_report(settings))
            return 0
        except Exception as exc:
            logger.exception('eenmalige v3.6-cyclus mislukt: %s', exc)
            write_report(settings, build_report(settings))
            return 2

    workers = [
        threading.Thread(
            target=_periodic_worker,
            kwargs={
                'settings': settings,
                'name': 'positiebewaking',
                'interval_seconds': POSITION_MONITOR_SECONDS,
                'action': lambda api: monitor_positions(settings, api=api),
            },
            name='v36-position-monitor',
        ),
        threading.Thread(
            target=_periodic_worker,
            kwargs={
                'settings': settings,
                'name': 'volledig-5m-universum',
                'interval_seconds': TECHNICAL_CHECK_SECONDS,
                'action': lambda api: (
                    apply_scheduled_deposits(settings),
                    evaluate_new_five_minute_cycle(settings, api=api),
                ),
            },
            name='v36-full-universe',
        ),
        threading.Thread(
            target=_periodic_worker,
            kwargs={
                'settings': settings,
                'name': 'kandidaat-hercontrole',
                'interval_seconds': CANDIDATE_RECHECK_SECONDS,
                'action': lambda api: recheck_active_candidates(settings, api=api),
            },
            name='v36-candidate-recheck',
        ),
    ]
    for worker in workers:
        worker.start()
    while not STOP:
        time.sleep(0.5)
    for worker in workers:
        worker.join(timeout=15.0)
    write_report(settings, build_report(settings))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
