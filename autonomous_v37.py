from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bitvavo_public import BitvavoPublic
from v36_decision import (
    FIFTEEN_MINUTES_MS,
    FIVE_MINUTES_MS,
    ONE_HOUR_MS,
    candle_quality,
    timeframe_features,
)
from v37_decision import GATE_ORDER, evaluate_human_gates, resolve_regime


logger = logging.getLogger('cryptobot_autonomous_v37')
STOP = False
DAY_MS = 24 * 60 * 60 * 1000


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)


def _data_path(filename: str) -> str:
    data = Path('/var/data')
    if data.exists() and os.access(data, os.W_OK):
        return str(data / filename)
    return str(Path('data') / filename)


@dataclass(frozen=True)
class V37Settings:
    mode: str = 'OBSERVE_ONLY'
    api_base_url: str = 'https://api.bitvavo.com/v2'
    request_timeout_seconds: int = 10
    request_retries: int = 3
    universe_size: int = 20
    paper_start_eur: float = 3000.0
    reserve_eur: float = 200.0
    position_eur: float = 500.0
    max_open_positions: int = 5
    maximum_l2_candidates: int = 5
    minimum_l2_samples: int = 3
    minimum_l2_span_ms: int = 60_000
    maximum_l2_age_ms: int = 90_000
    maximum_candidate_age_ms: int = 7 * 60 * 1000
    max_execution_spread_pct: float = 0.25
    minimum_net_reward_risk: float = 1.20
    max_open_risk_eur: float = 45.0
    max_cluster_positions: int = 2
    max_entries_per_cycle: int = 2
    daily_loss_limit_eur: float = 45.0
    taker_fee_pct: float = 0.25
    slippage_pct: float = 0.08
    cycle_poll_seconds: int = 60
    candidate_recheck_seconds: int = 30
    report_seconds: int = 20
    db_path: str = 'data/cryptobot_autonomous_v37.db'
    report_path: str = 'data/cryptobot_autonomous_v37.json'

    @classmethod
    def from_env(cls) -> 'V37Settings':
        return cls(
            mode=os.getenv('V37_MODE', 'OBSERVE_ONLY').upper(),
            api_base_url=os.getenv('BITVAVO_API_BASE_URL', 'https://api.bitvavo.com/v2'),
            request_timeout_seconds=_env_int('REQUEST_TIMEOUT_SECONDS', 10),
            request_retries=_env_int('REQUEST_RETRIES', 3),
            universe_size=_env_int('UNIVERSE_SIZE', 20),
            paper_start_eur=_env_float('V37_PAPER_START_EUR', 3000.0),
            reserve_eur=_env_float('V37_RESERVE_EUR', 200.0),
            position_eur=_env_float('V37_POSITION_EUR', 500.0),
            max_open_positions=_env_int('V37_MAX_OPEN_POSITIONS', 5),
            maximum_l2_candidates=_env_int('V37_MAX_L2_CANDIDATES', 5),
            minimum_l2_samples=_env_int('V37_MIN_L2_SAMPLES', 3),
            minimum_l2_span_ms=_env_int('V37_MIN_L2_SPAN_SECONDS', 60) * 1000,
            maximum_l2_age_ms=_env_int('V37_MAX_L2_AGE_SECONDS', 90) * 1000,
            maximum_candidate_age_ms=_env_int('V37_MAX_CANDIDATE_AGE_SECONDS', 420) * 1000,
            max_execution_spread_pct=_env_float('V37_MAX_EXECUTION_SPREAD_PCT', 0.25),
            minimum_net_reward_risk=_env_float('V37_MIN_NET_RR', 1.20),
            max_open_risk_eur=_env_float('V37_MAX_OPEN_RISK_EUR', 45.0),
            max_cluster_positions=_env_int('V37_MAX_CLUSTER_POSITIONS', 2),
            max_entries_per_cycle=_env_int('V37_MAX_ENTRIES_PER_CYCLE', 2),
            daily_loss_limit_eur=_env_float('V37_DAILY_LOSS_LIMIT_EUR', 45.0),
            taker_fee_pct=_env_float('TAKER_FEE_PCT', 0.25),
            slippage_pct=_env_float('SLIPPAGE_PCT', 0.08),
            cycle_poll_seconds=_env_int('V37_CYCLE_POLL_SECONDS', 60),
            candidate_recheck_seconds=_env_int('V37_CANDIDATE_RECHECK_SECONDS', 30),
            report_seconds=_env_int('V37_REPORT_SECONDS', 20),
            db_path=os.getenv('V37_DB_PATH', _data_path('cryptobot_autonomous_v37.db')),
            report_path=os.getenv(
                'V37_REPORT_PATH', _data_path('cryptobot_autonomous_v37.json')
            ),
        )

    def validate(self) -> None:
        if self.mode != 'OBSERVE_ONLY':
            raise ValueError('v3.7 fase 1 accepteert uitsluitend OBSERVE_ONLY')
        if self.paper_start_eur != 3000.0:
            raise ValueError('v3.7 PAPER-startvermogen moet exact €3000 zijn')
        if self.position_eur != 500.0:
            raise ValueError('v3.7 positieomvang moet exact €500 zijn')
        if self.reserve_eur < 200.0:
            raise ValueError('v3.7 reserve moet minimaal €200 zijn')
        if not 1 <= self.max_open_positions <= 5:
            raise ValueError('v3.7 maximaal aantal posities moet tussen 1 en 5 liggen')
        if self.position_eur * self.max_open_positions > self.paper_start_eur - self.reserve_eur:
            raise ValueError('v3.7 maximale inzet schendt de vaste reserve')
        if not 1 <= self.maximum_l2_candidates <= self.universe_size:
            raise ValueError('v3.7 aantal L2-kandidaten is ongeldig')
        if self.minimum_l2_samples < 2 or self.minimum_l2_span_ms <= 0:
            raise ValueError('v3.7 L2-meetvenster is ongeldig')
        if self.maximum_l2_age_ms < self.minimum_l2_span_ms:
            raise ValueError('v3.7 L2-maximale leeftijd is korter dan het meetvenster')
        if min(
            self.cycle_poll_seconds,
            self.candidate_recheck_seconds,
            self.report_seconds,
        ) <= 0:
            raise ValueError('v3.7 intervallen moeten positief zijn')

    def invariants(self) -> dict[str, Any]:
        return {
            'mode': self.mode,
            'paper_start_eur': self.paper_start_eur,
            'position_eur': self.position_eur,
            'reserve_eur': self.reserve_eur,
            'max_open_positions': self.max_open_positions,
            'existing_assets_excluded': True,
        }


def _stop(signum: int, frame: object) -> None:
    del frame
    global STOP
    STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def _connect(settings: V37Settings) -> sqlite3.Connection:
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(
        '''
        CREATE TABLE IF NOT EXISTS v37_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS v37_cycles (
            cycle_ms INTEGER PRIMARY KEY,
            evaluated_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            raw_regime TEXT NOT NULL,
            regime TEXT NOT NULL,
            stable_regime TEXT NOT NULL,
            bull_breadth_pct REAL NOT NULL,
            bear_breadth_pct REAL NOT NULL,
            valid_markets INTEGER NOT NULL,
            universe_json TEXT NOT NULL,
            errors_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS v37_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_key TEXT NOT NULL UNIQUE,
            cycle_ms INTEGER NOT NULL,
            evaluated_ms INTEGER NOT NULL,
            evaluation_kind TEXT NOT NULL,
            market TEXT NOT NULL,
            action TEXT NOT NULL,
            would_enter INTEGER NOT NULL,
            active_candidate INTEGER NOT NULL,
            setup TEXT NOT NULL,
            selection_priority REAL NOT NULL,
            data_quality_status TEXT NOT NULL,
            blockers_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            gates_json TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS v37_candidates (
            cycle_ms INTEGER NOT NULL,
            market TEXT NOT NULL,
            created_ms INTEGER NOT NULL,
            updated_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            setup TEXT NOT NULL,
            context_json TEXT NOT NULL,
            PRIMARY KEY(cycle_ms, market)
        );
        CREATE TABLE IF NOT EXISTS v37_l2_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_ms INTEGER NOT NULL,
            market TEXT NOT NULL,
            captured_at_ms INTEGER NOT NULL,
            buy_vwap REAL NOT NULL,
            sell_vwap REAL NOT NULL,
            execution_spread_pct REAL NOT NULL,
            near_book_imbalance REAL NOT NULL,
            details_json TEXT NOT NULL,
            UNIQUE(cycle_ms, market, captured_at_ms)
        );
        CREATE TABLE IF NOT EXISTS v37_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_ms INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            market TEXT,
            details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_v37_decisions_time
            ON v37_decisions(evaluated_ms);
        CREATE INDEX IF NOT EXISTS idx_v37_candidates_status
            ON v37_candidates(status, created_ms);
        CREATE INDEX IF NOT EXISTS idx_v37_l2_market_time
            ON v37_l2_snapshots(market, captured_at_ms);
        '''
    )
    conn.commit()
    return conn


def _set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        '''INSERT INTO v37_meta(key,value) VALUES (?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
        (key, str(value)),
    )


def _meta(conn: sqlite3.Connection, key: str, default: str = '') -> str:
    row = conn.execute('SELECT value FROM v37_meta WHERE key=?', (key,)).fetchone()
    return default if row is None else str(row['value'])


def _meta_int(conn: sqlite3.Connection, key: str) -> int:
    try:
        return int(_meta(conn, key, '0'))
    except ValueError:
        return 0


def _event(
    conn: sqlite3.Connection,
    event_ms: int,
    event_type: str,
    *,
    market: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        'INSERT INTO v37_events(event_ms,event_type,market,details_json) VALUES (?,?,?,?)',
        (event_ms, event_type, market, json.dumps(details or {}, ensure_ascii=False)),
    )


def ensure_observer(settings: V37Settings, now_ms: int | None = None) -> None:
    settings.validate()
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    conn = _connect(settings)
    try:
        _set_meta(conn, 'version', '3.7')
        _set_meta(conn, 'mode', settings.mode)
        _set_meta(conn, 'execution_enabled', '0')
        _set_meta(conn, 'live_orders_possible', '0')
        _set_meta(conn, 'existing_assets_excluded', '1')
        _set_meta(conn, 'initialized_ms', _meta(conn, 'initialized_ms', str(current_ms)))
        conn.commit()
    finally:
        conn.close()


def _quality_and_features(
    candles: list,
    *,
    interval_ms: int,
    now_ms: int,
    allow_one_small_gap: bool,
    interval_label: str,
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
        }
    elif str(quality.get('status')) == 'WARN':
        quality = {
            **quality,
            'reason': f'een_klein_{interval_label}_interval_zonder_handel_of_candle',
            'market_activity_warning': True,
        }
    return quality, features


def _three_timeframe_context(
    api: BitvavoPublic,
    market: str,
    now_ms: int,
) -> dict[str, Any]:
    candles = {
        'five': api.closed_candles(market, '5m', 80, now_ms=now_ms),
        'fifteen': api.closed_candles(market, '15m', 80, now_ms=now_ms),
        'hour': api.closed_candles(market, '1h', 80, now_ms=now_ms),
    }
    quality_five, five = _quality_and_features(
        candles['five'],
        interval_ms=FIVE_MINUTES_MS,
        now_ms=now_ms,
        allow_one_small_gap=True,
        interval_label='5m',
    )
    quality_fifteen, fifteen = _quality_and_features(
        candles['fifteen'],
        interval_ms=FIFTEEN_MINUTES_MS,
        now_ms=now_ms,
        allow_one_small_gap=True,
        interval_label='15m',
    )
    quality_hour, hour = _quality_and_features(
        candles['hour'],
        interval_ms=ONE_HOUR_MS,
        now_ms=now_ms,
        allow_one_small_gap=True,
        interval_label='1h',
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
        '_five_candles': candles['five'],
    }


def _bitcoin_context(api: BitvavoPublic, now_ms: int) -> tuple[dict[str, Any], dict[str, Any]]:
    context = _three_timeframe_context(api, 'BTC-EUR', now_ms)
    bitcoin = {
        'five': context['five'],
        'fifteen': context['fifteen'],
        'hour': context['hour'],
    }
    quality = {
        'bitcoin_five': context['quality']['five'],
        'bitcoin_fifteen': context['quality']['fifteen'],
        'bitcoin_hour': context['quality']['hour'],
    }
    return bitcoin, quality


def _raw_regime(
    contexts: dict[str, dict[str, Any]],
    *,
    universe_size: int,
) -> tuple[str, float, float, int]:
    reliable = [
        item for item in contexts.values()
        if bool(item['fifteen'].get('valid')) and bool(item['hour'].get('valid'))
    ]
    denominator = max(1, len(reliable))
    bull = sum(
        bool(item['fifteen'].get('trend_up')) and bool(item['hour'].get('trend_up'))
        for item in reliable
    ) / denominator * 100.0
    bear = sum(
        bool(item['fifteen'].get('trend_down')) and bool(item['hour'].get('trend_down'))
        for item in reliable
    ) / denominator * 100.0
    if len(reliable) < max(1, math.ceil(universe_size * 0.80)):
        raw = 'DATA_UNCERTAIN'
    elif bull >= 60.0:
        raw = 'BULL'
    elif bear >= 60.0:
        raw = 'BEAR'
    else:
        raw = 'SIDEWAYS'
    return raw, bull, bear, len(reliable)


def _decision_upsert(
    conn: sqlite3.Connection,
    *,
    decision_key: str,
    cycle_ms: int,
    evaluated_ms: int,
    evaluation_kind: str,
    decision: dict[str, Any],
) -> int:
    details = {
        key: value for key, value in decision.items()
        if key not in {'gates', 'blockers', 'warnings'}
    }
    conn.execute(
        '''INSERT INTO v37_decisions
           (decision_key,cycle_ms,evaluated_ms,evaluation_kind,market,action,would_enter,
            active_candidate,setup,selection_priority,data_quality_status,blockers_json,
            warnings_json,gates_json,details_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(decision_key) DO UPDATE SET
             evaluated_ms=excluded.evaluated_ms,evaluation_kind=excluded.evaluation_kind,
             action=excluded.action,would_enter=excluded.would_enter,
             active_candidate=excluded.active_candidate,setup=excluded.setup,
             selection_priority=excluded.selection_priority,
             data_quality_status=excluded.data_quality_status,
             blockers_json=excluded.blockers_json,warnings_json=excluded.warnings_json,
             gates_json=excluded.gates_json,details_json=excluded.details_json''',
        (
            decision_key,
            cycle_ms,
            evaluated_ms,
            evaluation_kind,
            str(decision.get('market', '')),
            str(decision.get('action', 'AFWIJZEN')),
            int(bool(decision.get('would_enter'))),
            int(bool(decision.get('active_candidate'))),
            str(decision.get('setup', 'geen_5m_trigger')),
            float(decision.get('selection_priority', 0.0) or 0.0),
            str(decision.get('data_quality_status', 'BLOCK')),
            json.dumps(decision.get('blockers', []), ensure_ascii=False),
            json.dumps(decision.get('warnings', []), ensure_ascii=False),
            json.dumps(decision.get('gates', {}), ensure_ascii=False),
            json.dumps(details, ensure_ascii=False),
        ),
    )
    row = conn.execute(
        'SELECT id FROM v37_decisions WHERE decision_key=?', (decision_key,)
    ).fetchone()
    return int(row['id'])


def _error_decision(market: str, reason: str) -> dict[str, Any]:
    gates = {
        name: {
            'status': 'BLOCK',
            'hard_gate': True,
            'reasons': [reason] if name == 'data_integriteit' else ['voorafgaande_poort_blokkeert'],
            'evidence': {},
        }
        for name in GATE_ORDER
    }
    return {
        'market': market,
        'action': 'AFWIJZEN',
        'would_enter': False,
        'eligible': False,
        'execution_enabled': False,
        'active_candidate': False,
        'setup': 'geen_5m_trigger',
        'selection_priority': 0.0,
        'gates': gates,
        'blockers': [reason],
        'warnings': [],
        'data_quality_status': 'BLOCK',
        'l2_summary': {},
        'edge': {},
        'thesis': {},
    }


def _capacity_block(decision: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(decision))
    result['action'] = 'AFWIJZEN'
    result['would_enter'] = False
    result['active_candidate'] = False
    result['blockers'] = list(dict.fromkeys([
        *result.get('blockers', []),
        'buiten_l2_selectie_top5',
    ]))
    result['gates']['l2_bevestiging'] = {
        'status': 'BLOCK',
        'hard_gate': True,
        'reasons': ['buiten_l2_selectie_top5'],
        'evidence': {'reden': 'alleen hoogste selectieprioriteiten krijgen duur L2-meetvenster'},
    }
    result['gates']['besliscontroller'] = {
        'status': 'BLOCK',
        'hard_gate': True,
        'reasons': ['een_of_meer_harde_poorten_blokkeren'],
        'evidence': {},
    }
    return result


def _evaluate(
    settings: V37Settings,
    *,
    context: dict[str, Any],
    snapshots: list[dict[str, Any]],
    now_ms: int,
) -> dict[str, Any]:
    result = evaluate_human_gates(
        context=context,
        snapshots=snapshots,
        invariants=settings.invariants(),
        now_ms=now_ms,
        minimum_l2_samples=settings.minimum_l2_samples,
        minimum_l2_span_ms=settings.minimum_l2_span_ms,
        maximum_l2_age_ms=settings.maximum_l2_age_ms,
        max_spread_pct=settings.max_execution_spread_pct,
        taker_fee_pct=settings.taker_fee_pct,
        slippage_pct=settings.slippage_pct,
        minimum_rr=settings.minimum_net_reward_risk,
        max_open_risk_eur=settings.max_open_risk_eur,
        max_cluster_positions=settings.max_cluster_positions,
        max_entries_per_cycle=settings.max_entries_per_cycle,
        daily_loss_limit_eur=settings.daily_loss_limit_eur,
    )
    discovery = context.get('discovery')
    if isinstance(discovery, dict):
        result['discovery'] = discovery
    return result


def _insert_snapshot(
    conn: sqlite3.Connection,
    *,
    cycle_ms: int,
    market: str,
    depth: dict[str, Any],
    fallback_ms: int,
) -> None:
    captured = int(float(depth.get('captured_at_ms', fallback_ms) or fallback_ms))
    conn.execute(
        '''INSERT OR IGNORE INTO v37_l2_snapshots
           (cycle_ms,market,captured_at_ms,buy_vwap,sell_vwap,execution_spread_pct,
            near_book_imbalance,details_json) VALUES (?,?,?,?,?,?,?,?)''',
        (
            cycle_ms,
            market,
            captured,
            float(depth['buy_vwap']),
            float(depth['sell_vwap']),
            float(depth['execution_spread_pct']),
            float(depth['near_book_imbalance']),
            json.dumps(depth, ensure_ascii=False),
        ),
    )


def _snapshots(conn: sqlite3.Connection, cycle_ms: int, market: str) -> list[dict[str, Any]]:
    return [
        dict(row) for row in conn.execute(
            '''SELECT captured_at_ms,buy_vwap,sell_vwap,execution_spread_pct,
                      near_book_imbalance
               FROM v37_l2_snapshots WHERE cycle_ms=? AND market=?
               ORDER BY captured_at_ms''',
            (cycle_ms, market),
        )
    ]


def evaluate_new_five_minute_cycle(
    settings: V37Settings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
    universe_override: list[str] | None = None,
    regime_override: dict[str, Any] | None = None,
    context_overlays: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        universe = (
            market_api.top_markets_by_quote_volume('EUR', settings.universe_size)
            if universe_override is None
            else list(dict.fromkeys(str(market).upper() for market in universe_override))
        )
        bitcoin, bitcoin_quality = _bitcoin_context(market_api, current_ms)
    except Exception as exc:
        conn = _connect(settings)
        try:
            message = f'{type(exc).__name__}: {exc}'
            _event(conn, current_ms, 'UNIVERSE_OR_BITCOIN_ERROR', details={'error': message})
            _set_meta(conn, 'cycle_errors', json.dumps([message], ensure_ascii=False))
            conn.commit()
        finally:
            conn.close()
        return {'evaluated': False, 'reason': 'universe_of_bitcoin_api_error'}

    cycle_ms = int(bitcoin.get('five', {}).get('latest_candle_ms', 0) or 0)
    if cycle_ms <= 0:
        cycle_ms = current_ms // FIVE_MINUTES_MS * FIVE_MINUTES_MS - FIVE_MINUTES_MS
    conn = _connect(settings)
    try:
        existing = conn.execute(
            'SELECT status FROM v37_cycles WHERE cycle_ms=?', (cycle_ms,)
        ).fetchone()
        if existing is not None and str(existing['status']) == 'COMPLETE':
            return {'evaluated': False, 'reason': 'cycle_al_voltooid', 'cycle_ms': cycle_ms}
    finally:
        conn.close()

    contexts: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for market in universe:
        try:
            context = _three_timeframe_context(market_api, market, current_ms)
            context.pop('_five_candles', None)
            contexts[market] = context
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    raw, bull, bear, valid_markets = _raw_regime(
        contexts, universe_size=settings.universe_size
    )
    if regime_override is None:
        conn = _connect(settings)
        try:
            previous = _meta(conn, 'stable_regime', 'UNKNOWN')
            pending = _meta(conn, 'pending_regime', '')
            pending_count = _meta_int(conn, 'pending_regime_count')
        finally:
            conn.close()
        regime_state = resolve_regime(
            previous_regime=previous,
            raw_regime=raw,
            pending_regime=pending,
            pending_count=pending_count,
        )
    else:
        raw = str(regime_override.get('raw_regime', 'DATA_UNCERTAIN')).upper()
        regime = str(regime_override.get('regime', 'DATA_UNCERTAIN')).upper()
        stable = str(regime_override.get('stable_regime', regime)).upper()
        bull = float(regime_override.get('bull_breadth_pct', 0.0) or 0.0)
        bear = float(regime_override.get('bear_breadth_pct', 0.0) or 0.0)
        regime_state = {
            'regime': regime,
            'stable_regime': stable,
            'pending_regime': '',
            'pending_count': 0,
            'transition': regime == 'TRANSITION',
        }

    decisions: dict[str, dict[str, Any]] = {}
    for market, context in contexts.items():
        overlay = (context_overlays or {}).get(market)
        if isinstance(overlay, dict):
            context.update(overlay)
        context['cycle_ms'] = cycle_ms
        context['regime'] = regime_state['regime']
        context['raw_regime'] = raw
        context['stable_regime'] = regime_state['stable_regime']
        context['bull_breadth_pct'] = round(bull, 3)
        context['bear_breadth_pct'] = round(bear, 3)
        context['bitcoin'] = bitcoin
        context['quality'].update(bitcoin_quality)
        decisions[market] = _evaluate(
            settings, context=context, snapshots=[], now_ms=current_ms
        )

    ranked = sorted(
        (
            (market, decision)
            for market, decision in decisions.items()
            if bool(decision.get('active_candidate'))
        ),
        key=lambda item: (-float(item[1].get('selection_priority', 0.0)), item[0]),
    )
    selected = {market for market, _ in ranked[:settings.maximum_l2_candidates]}
    for market, _ in ranked[settings.maximum_l2_candidates:]:
        decisions[market] = _capacity_block(decisions[market])

    initial_depths: dict[str, dict[str, Any]] = {}
    for market in sorted(selected):
        try:
            initial_depths[market] = market_api.depth_book(market, settings.position_eur)
        except Exception as exc:
            errors.append(f'{market} L2: {type(exc).__name__}: {exc}')

    conn = _connect(settings)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            "UPDATE v37_candidates SET status='SUPERSEDED',updated_ms=? "
            "WHERE status='COLLECTING' AND cycle_ms<?",
            (current_ms, cycle_ms),
        )
        conn.execute(
            '''INSERT INTO v37_cycles
               (cycle_ms,evaluated_ms,status,raw_regime,regime,stable_regime,
                bull_breadth_pct,bear_breadth_pct,valid_markets,universe_json,errors_json)
               VALUES (?,?,'RUNNING',?,?,?,?,?,?,?,?)
               ON CONFLICT(cycle_ms) DO UPDATE SET evaluated_ms=excluded.evaluated_ms,
                 status='RUNNING',raw_regime=excluded.raw_regime,regime=excluded.regime,
                 stable_regime=excluded.stable_regime,bull_breadth_pct=excluded.bull_breadth_pct,
                 bear_breadth_pct=excluded.bear_breadth_pct,valid_markets=excluded.valid_markets,
                 universe_json=excluded.universe_json,errors_json=excluded.errors_json''',
            (
                cycle_ms,
                current_ms,
                raw,
                regime_state['regime'],
                regime_state['stable_regime'],
                bull,
                bear,
                valid_markets,
                json.dumps(universe, ensure_ascii=False),
                json.dumps(errors, ensure_ascii=False),
            ),
        )
        for market in universe:
            context = contexts.get(market)
            if context is None:
                reason = next(
                    (item for item in errors if item.startswith(f'{market}:')),
                    f'{market}: marktdata_ontbreekt',
                )
                _decision_upsert(
                    conn,
                    decision_key=f'5M:{cycle_ms}:{market}',
                    cycle_ms=cycle_ms,
                    evaluated_ms=current_ms,
                    evaluation_kind='NEW_5M',
                    decision=_error_decision(market, reason),
                )
                _event(conn, current_ms, 'MARKET_DATA_ERROR', market=market, details={'error': reason})
                continue
            if market in selected:
                conn.execute(
                    '''INSERT INTO v37_candidates
                       (cycle_ms,market,created_ms,updated_ms,status,setup,context_json)
                       VALUES (?,?,?,?,'COLLECTING',?,?)
                       ON CONFLICT(cycle_ms,market) DO UPDATE SET updated_ms=excluded.updated_ms,
                         status='COLLECTING',setup=excluded.setup,context_json=excluded.context_json''',
                    (
                        cycle_ms,
                        market,
                        current_ms,
                        current_ms,
                        str(decisions[market].get('setup', '')),
                        json.dumps(context, ensure_ascii=False),
                    ),
                )
                depth = initial_depths.get(market)
                if depth is not None:
                    _insert_snapshot(
                        conn,
                        cycle_ms=cycle_ms,
                        market=market,
                        depth=depth,
                        fallback_ms=current_ms,
                    )
                    decisions[market] = _evaluate(
                        settings,
                        context=context,
                        snapshots=_snapshots(conn, cycle_ms, market),
                        now_ms=current_ms,
                    )
                else:
                    _event(conn, current_ms, 'L2_INITIAL_ERROR', market=market)
            _decision_upsert(
                conn,
                decision_key=f'5M:{cycle_ms}:{market}',
                cycle_ms=cycle_ms,
                evaluated_ms=current_ms,
                evaluation_kind='NEW_5M',
                decision=decisions[market],
            )
        conn.execute(
            "UPDATE v37_cycles SET status='COMPLETE',errors_json=? WHERE cycle_ms=?",
            (json.dumps(errors, ensure_ascii=False), cycle_ms),
        )
        _set_meta(conn, 'stable_regime', regime_state['stable_regime'])
        _set_meta(conn, 'pending_regime', regime_state['pending_regime'])
        _set_meta(conn, 'pending_regime_count', regime_state['pending_count'])
        _set_meta(conn, 'cycle_generated_ms', current_ms)
        _set_meta(conn, 'cycle_errors', json.dumps(errors, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return {
        'evaluated': True,
        'cycle_ms': cycle_ms,
        'universe_count': len(universe),
        'valid_markets': valid_markets,
        'regime': regime_state['regime'],
        'raw_regime': raw,
        'selected_candidates': len(selected),
        'errors': errors,
    }


def recheck_candidates(
    settings: V37Settings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    market_api = api or BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    conn = _connect(settings)
    try:
        _set_meta(conn, 'candidate_recheck_attempted_ms', current_ms)
        rows = conn.execute(
            "SELECT * FROM v37_candidates WHERE status='COLLECTING' ORDER BY created_ms,market"
        ).fetchall()
        conn.commit()
    finally:
        conn.close()
    errors: list[str] = []
    observed = 0
    checked = 0
    for row in rows:
        cycle_ms = int(row['cycle_ms'])
        market = str(row['market'])
        if current_ms - int(row['created_ms']) > settings.maximum_candidate_age_ms:
            conn = _connect(settings)
            try:
                conn.execute(
                    "UPDATE v37_candidates SET status='EXPIRED',updated_ms=? "
                    'WHERE cycle_ms=? AND market=?',
                    (current_ms, cycle_ms, market),
                )
                _event(conn, current_ms, 'CANDIDATE_EXPIRED', market=market, details={
                    'cycle_ms': cycle_ms
                })
                conn.commit()
            finally:
                conn.close()
            continue
        try:
            depth = market_api.depth_book(market, settings.position_eur)
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')
            continue
        conn = _connect(settings)
        try:
            conn.execute('BEGIN IMMEDIATE')
            _insert_snapshot(
                conn,
                cycle_ms=cycle_ms,
                market=market,
                depth=depth,
                fallback_ms=current_ms,
            )
            context = json.loads(str(row['context_json']))
            decision = _evaluate(
                settings,
                context=context,
                snapshots=_snapshots(conn, cycle_ms, market),
                now_ms=current_ms,
            )
            bucket = current_ms // (settings.candidate_recheck_seconds * 1000)
            _decision_upsert(
                conn,
                decision_key=f'L2:{cycle_ms}:{bucket}:{market}',
                cycle_ms=cycle_ms,
                evaluated_ms=current_ms,
                evaluation_kind='L2_WINDOW',
                decision=decision,
            )
            status = 'COLLECTING'
            if bool(decision.get('would_enter')):
                status = 'OBSERVED'
                observed += 1
                _event(conn, current_ms, 'SHADOW_OPPORTUNITY', market=market, details={
                    'cycle_ms': cycle_ms,
                    'setup': decision.get('setup'),
                    'thesis': decision.get('thesis'),
                    'edge': decision.get('edge'),
                })
            elif decision.get('gates', {}).get('l2_bevestiging', {}).get('status') == 'BLOCK':
                status = 'REJECTED'
            conn.execute(
                'UPDATE v37_candidates SET status=?,updated_ms=? WHERE cycle_ms=? AND market=?',
                (status, current_ms, cycle_ms, market),
            )
            _set_meta(conn, 'candidate_recheck_generated_ms', current_ms)
            conn.commit()
            checked += 1
        finally:
            conn.close()
    conn = _connect(settings)
    try:
        _set_meta(conn, 'candidate_recheck_errors', json.dumps(errors, ensure_ascii=False))
        if not rows:
            _set_meta(conn, 'candidate_recheck_generated_ms', current_ms)
        conn.commit()
    finally:
        conn.close()
    return {'candidates': len(rows), 'checked': checked, 'observed': observed, 'errors': errors}


def build_report(settings: V37Settings, now_ms: int | None = None) -> dict[str, Any]:
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cutoff = current_ms - DAY_MS
    conn = _connect(settings)
    try:
        cycle_row = conn.execute(
            'SELECT * FROM v37_cycles ORDER BY cycle_ms DESC LIMIT 1'
        ).fetchone()
        latest_cycle = dict(cycle_row) if cycle_row is not None else {}
        if latest_cycle:
            latest_cycle['universe'] = json.loads(latest_cycle.pop('universe_json'))
            latest_cycle['errors'] = json.loads(latest_cycle.pop('errors_json'))
        decision_rows = conn.execute(
            '''SELECT evaluated_ms,evaluation_kind,market,action,would_enter,active_candidate,
                      setup,selection_priority,data_quality_status,blockers_json,warnings_json,
                      gates_json,details_json
               FROM v37_decisions ORDER BY evaluated_ms DESC,id DESC LIMIT 10'''
        ).fetchall()
        latest_decisions: list[dict[str, Any]] = []
        for row in decision_rows:
            item = dict(row)
            item['would_enter'] = bool(item['would_enter'])
            item['active_candidate'] = bool(item['active_candidate'])
            item['blockers'] = json.loads(item.pop('blockers_json'))
            item['warnings'] = json.loads(item.pop('warnings_json'))
            item['gates'] = json.loads(item.pop('gates_json'))
            item['details'] = json.loads(item.pop('details_json'))
            latest_decisions.append(item)
        rows_24h = conn.execute(
            'SELECT action,blockers_json FROM v37_decisions WHERE evaluated_ms>=?', (cutoff,)
        ).fetchall()
        actions = Counter(str(row['action']) for row in rows_24h)
        blockers: Counter[str] = Counter()
        for row in rows_24h:
            blockers.update(json.loads(row['blockers_json']))
        candidate_counts = {
            str(row['status']): int(row['amount'])
            for row in conn.execute(
                'SELECT status,COUNT(*) AS amount FROM v37_candidates GROUP BY status'
            )
        }
        l2_count = int(conn.execute(
            'SELECT COUNT(*) FROM v37_l2_snapshots WHERE captured_at_ms>=?', (cutoff,)
        ).fetchone()[0])
        opportunity_count = int(conn.execute(
            '''SELECT COUNT(DISTINCT CAST(cycle_ms AS TEXT)||':'||market)
               FROM v37_decisions WHERE evaluated_ms>=? AND would_enter=1''',
            (cutoff,),
        ).fetchone()[0])
        initialized_ms = _meta_int(conn, 'initialized_ms')
        cycle_summary = conn.execute(
            '''SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status='COMPLETE' THEN 1 ELSE 0 END) AS complete,
                      SUM(CASE WHEN errors_json!='[]' THEN 1 ELSE 0 END) AS with_errors
               FROM v37_cycles WHERE evaluated_ms>=?''',
            (initialized_ms,),
        ).fetchone()
        heartbeat = {
            'cycle_attempted_ms': _meta_int(conn, 'cycle_attempted_ms'),
            'cycle_generated_ms': _meta_int(conn, 'cycle_generated_ms'),
            'candidate_recheck_attempted_ms': _meta_int(conn, 'candidate_recheck_attempted_ms'),
            'candidate_recheck_generated_ms': _meta_int(conn, 'candidate_recheck_generated_ms'),
        }
    finally:
        conn.close()
    return {
        'version': '3.7',
        'component': 'HUMAN_OBSERVER_V37',
        'generated_at_ms': current_ms,
        'generated_at_utc': datetime.fromtimestamp(
            current_ms / 1000.0, tz=timezone.utc
        ).isoformat(),
        'modes': {
            'v36_control': 'ONGEWIJZIGD EN AFZONDERLIJK',
            'v37': 'OBSERVE-ONLY / SCHADUWKANSEN',
            'paper_execution': 'UIT',
            'live_orders': 'UIT / TECHNISCH ONMOGELIJK',
        },
        'safety': {
            'execution_enabled': False,
            'private_api_used': False,
            'existing_assets_excluded': True,
            'separate_database': settings.db_path,
        },
        'capital_model': {
            'paper_start_eur': settings.paper_start_eur,
            'position_eur': settings.position_eur,
            'reserve_eur': settings.reserve_eur,
            'max_open_positions': settings.max_open_positions,
            'paper_positions_opened': 0,
        },
        'latest_cycle': latest_cycle,
        'decisions': {
            'total_24h': len(rows_24h),
            'actions_24h': dict(actions),
            'shadow_opportunities_24h': opportunity_count,
            'top_blockers_24h': blockers.most_common(10),
            'latest': latest_decisions,
        },
        'l2_research': {
            'snapshots_24h': l2_count,
            'minimum_samples': settings.minimum_l2_samples,
            'minimum_span_seconds': settings.minimum_l2_span_ms // 1000,
            'maximum_age_seconds': settings.maximum_l2_age_ms // 1000,
            'candidate_counts': candidate_counts,
        },
        'risk_hypotheses': {
            'max_open_risk_eur': settings.max_open_risk_eur,
            'max_cluster_positions': settings.max_cluster_positions,
            'max_entries_per_cycle': settings.max_entries_per_cycle,
            'daily_loss_limit_eur': settings.daily_loss_limit_eur,
            'status': 'NOG NIET ACTIEF; EERST SCHADUWTOETS',
        },
        'observation_validation': {
            'started_at_ms': initialized_ms,
            'elapsed_hours': round(max(0, current_ms - initialized_ms) / 3_600_000.0, 2)
            if initialized_ms > 0 else 0.0,
            'minimum_hours': 24,
            'complete_cycles': int(cycle_summary['complete'] or 0),
            'cycles_with_errors': int(cycle_summary['with_errors'] or 0),
            'minimum_complete_cycles': 276,
            'ready_for_manual_review': bool(
                initialized_ms > 0
                and current_ms - initialized_ms >= DAY_MS
                and int(cycle_summary['complete'] or 0) >= 276
            ),
            'automatic_paper_activation': False,
        },
        'heartbeat': heartbeat,
        'gate_order': list(GATE_ORDER),
    }


def write_report(settings: V37Settings, report: dict[str, Any]) -> None:
    path = Path(settings.report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def load_report(settings: V37Settings) -> dict[str, Any] | None:
    path = Path(settings.report_path)
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def print_status(report: dict[str, Any]) -> None:
    modes = report.get('modes', {})
    capital = report.get('capital_model', {})
    cycle = report.get('latest_cycle', {})
    decisions = report.get('decisions', {})
    l2 = report.get('l2_research', {})
    validation = report.get('observation_validation', {})
    print('=== CRYPTOBOT v3.7 | MENSELIJKE OBSERVATIELAAG ===')
    print(f"UTC                   : {report.get('generated_at_utc', 'onbekend')}")
    print(f"v3.6 CONTROLEGROEP    : {modes.get('v36_control', 'ONBEKEND')}")
    print(f"v3.7 MODUS            : {modes.get('v37', 'ONBEKEND')}")
    print(f"PAPER-UITVOERING      : {modes.get('paper_execution', 'UIT')}")
    print(f"LIVE ORDERS           : {modes.get('live_orders', 'UIT')}")
    print()
    print('=== VASTE KAPITAALGRENZEN VOOR LATERE PAPERFASE ===')
    print(
        f"start €{float(capital.get('paper_start_eur', 0)):.2f}"
        f" | €{float(capital.get('position_eur', 0)):.0f} per positie"
        f" | max {int(capital.get('max_open_positions', 0))}"
        f" | reserve €{float(capital.get('reserve_eur', 0)):.0f}"
    )
    print('bestaande munten      : UITGESLOTEN')
    print('geopende posities     : 0; observe-only kan niets uitvoeren')
    print()
    print('=== MARKTCONTEXT EN HARDE POORTEN ===')
    universe = cycle.get('universe', []) if isinstance(cycle, dict) else []
    print(
        f"laatste 5m-cyclus      : {cycle.get('cycle_ms', 'nog geen')}"
        f" | {cycle.get('status', 'WACHTEN')}"
    )
    print(
        f"regime                : {cycle.get('regime', 'UNKNOWN')}"
        f" | rauw {cycle.get('raw_regime', 'UNKNOWN')}"
    )
    print(
        f"betrouwbare markten   : {int(cycle.get('valid_markets', 0))}/{len(universe)}"
        f" | BULL {float(cycle.get('bull_breadth_pct', 0)):.1f}%"
        f" | BEAR {float(cycle.get('bear_breadth_pct', 0)):.1f}%"
    )
    print(f"besluiten 24u         : {int(decisions.get('total_24h', 0))}")
    print(f"schaduwkansen 24u     : {int(decisions.get('shadow_opportunities_24h', 0))}")
    print(
        f"L2-snapshots 24u      : {int(l2.get('snapshots_24h', 0))}"
        f" | minimaal {int(l2.get('minimum_samples', 0))} metingen"
        f" over {int(l2.get('minimum_span_seconds', 0))} sec"
    )
    print(
        f"observatie             : {float(validation.get('elapsed_hours', 0)):.1f}/24.0 uur"
        f" | complete cycli {int(validation.get('complete_cycles', 0))}/276"
        f" | controle {'GEREED' if validation.get('ready_for_manual_review') else 'LOOPT'}"
    )
    for item in decisions.get('latest', [])[:5]:
        gate_summary = ', '.join(
            f"{name}:{gate.get('status', '?')}"
            for name, gate in item.get('gates', {}).items()
        )
        print(
            f"  {item.get('market', '?'):<12} | {item.get('action', '?'):<15}"
            f" | {item.get('setup', '?')} | {gate_summary}"
        )
        blockers = item.get('blockers', [])
        if blockers:
            print(f"    blokkades: {', '.join(blockers[:4])}")
    top = decisions.get('top_blockers_24h', [])
    if top:
        print('belangrijkste blokkades:')
        for reason, amount in top[:5]:
            print(f'  {reason}: {amount}')
    print()
    print('v3.7 opent bewust geen PAPER-posities; na 24 uur volgt handmatige beoordeling.')


def _refresh_report(settings: V37Settings, now_ms: int | None = None) -> dict[str, Any]:
    report = build_report(settings, now_ms=now_ms)
    write_report(settings, report)
    return report


def run_once(
    settings: V37Settings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    ensure_observer(settings, current_ms)
    cycle = evaluate_new_five_minute_cycle(settings, api=api, now_ms=current_ms)
    candidates = recheck_candidates(settings, api=api, now_ms=current_ms)
    report = _refresh_report(settings, current_ms)
    return {'cycle': cycle, 'candidates': candidates, 'report': report}


def main() -> int:
    parser = argparse.ArgumentParser(description='CryptoBot v3.7 menselijke observe-only laag')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    settings = V37Settings.from_env()
    settings.validate()
    ensure_observer(settings)
    if args.status:
        report = load_report(settings) or build_report(settings)
        print_status(report)
        return 0
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if args.once:
        result = run_once(settings)
        print_status(result['report'])
        return 0

    api = BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    next_cycle = 0.0
    next_candidate = 0.0
    next_report = 0.0
    while not STOP:
        now = time.time()
        if now >= next_cycle:
            try:
                evaluate_new_five_minute_cycle(
                    settings, api=api, now_ms=int(time.time() * 1000)
                )
            except Exception:
                logger.exception('v3.7 universumcyclus mislukt')
            next_cycle = now + settings.cycle_poll_seconds
        if now >= next_candidate:
            try:
                recheck_candidates(settings, api=api, now_ms=int(time.time() * 1000))
            except Exception:
                logger.exception('v3.7 kandidaatcontrole mislukt')
            next_candidate = now + settings.candidate_recheck_seconds
        if now >= next_report:
            try:
                _refresh_report(settings, int(time.time() * 1000))
            except Exception:
                logger.exception('v3.7 rapportage mislukt')
            next_report = now + settings.report_seconds
        time.sleep(0.5)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
