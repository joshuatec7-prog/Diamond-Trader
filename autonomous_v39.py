from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from autonomous_v37 import (
    V37Settings,
    _connect,
    _meta,
    _set_meta,
    build_report as build_v37_report,
    ensure_observer,
    evaluate_new_five_minute_cycle,
    recheck_candidates,
)
from bitvavo_public import BitvavoPublic


logger = logging.getLogger('cryptobot_autonomous_v39')
STOP = False
HOUR_MS = 3_600_000
COLLECTION_HOURS = 72
SETTLEMENT_HOURS = 4
MIN_DISCOVERY_SCORE = 65.0
MAX_DISCOVERY_CANDIDATES = 10
QUALIFICATION_COOLDOWN_MS = 4 * HOUR_MS
MAX_QUALIFIED_PER_CYCLE = 2
OUTCOME_HORIZONS_MINUTES = (15, 60, 240)
MIN_QUALIFIED_OPPORTUNITIES = 20
MIN_MATURE_240M_OUTCOMES = 15
MIN_DATA_COMPLETENESS_PCT = 95.0
MIN_PROFIT_FACTOR = 1.15
MIN_WIN_RATE_PCT = 45.0


def _data_path(filename: str) -> str:
    data = Path('/var/data')
    if data.exists() and os.access(data, os.W_OK):
        return str(data / filename)
    return str(Path('data') / filename)


def settings_from_env() -> V37Settings:
    base = V37Settings.from_env()
    return replace(
        base,
        mode='OBSERVE_ONLY',
        universe_size=MAX_DISCOVERY_CANDIDATES,
        maximum_l2_candidates=5,
        db_path=os.getenv('V39_DB_PATH', _data_path('cryptobot_autonomous_v39.db')),
        report_path=os.getenv('V39_REPORT_PATH', _data_path('cryptobot_autonomous_v39.json')),
    )


def _stop(signum: int, frame: object) -> None:
    del frame
    global STOP
    STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def ensure_v39(settings: V37Settings, now_ms: int | None = None) -> None:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    ensure_observer(settings, current)
    conn = _connect(settings)
    try:
        conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS v39_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_event_id INTEGER NOT NULL UNIQUE,
                cycle_ms INTEGER NOT NULL,
                observed_ms INTEGER NOT NULL,
                market TEXT NOT NULL,
                qualified INTEGER NOT NULL,
                qualification_reason TEXT NOT NULL,
                discovery_score REAL NOT NULL,
                discovery_rank INTEGER NOT NULL,
                entry_buy_vwap REAL NOT NULL,
                base_amount REAL NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS v39_outcomes (
                opportunity_id INTEGER NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                measured_ms INTEGER NOT NULL,
                sell_vwap REAL NOT NULL,
                gross_return_pct REAL NOT NULL,
                net_return_pct REAL NOT NULL,
                details_json TEXT NOT NULL,
                PRIMARY KEY(opportunity_id,horizon_minutes),
                FOREIGN KEY(opportunity_id) REFERENCES v39_opportunities(id)
            );
            CREATE INDEX IF NOT EXISTS idx_v39_opportunity_market_time
                ON v39_opportunities(market,observed_ms);
            '''
        )
        initialized = int(_meta(conn, 'v39_initialized_ms', '0') or 0)
        if initialized <= 0:
            initialized = current
            _set_meta(conn, 'v39_initialized_ms', initialized)
            _set_meta(conn, 'v39_collection_end_ms', initialized + COLLECTION_HOURS * HOUR_MS)
            _set_meta(
                conn,
                'v39_decision_at_ms',
                initialized + (COLLECTION_HOURS + SETTLEMENT_HOURS) * HOUR_MS,
            )
        _set_meta(conn, 'version', '3.9')
        _set_meta(conn, 'mode', 'OBSERVE_ONLY')
        _set_meta(conn, 'execution_enabled', '0')
        _set_meta(conn, 'live_orders_possible', '0')
        _set_meta(conn, 'existing_assets_excluded', '1')
        conn.commit()
    finally:
        conn.close()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _regime_override(now_ms: int) -> dict[str, Any]:
    path = Path(os.getenv('V37_REPORT_PATH', _data_path('cryptobot_autonomous_v37.json')))
    report = _read_json(path)
    generated = int(report.get('generated_at_ms', 0) or 0)
    if generated <= 0 or now_ms - generated > 5 * 60_000:
        raise RuntimeError('v3.7-regimebron ontbreekt of is ouder dan vijf minuten')
    cycle = report.get('latest_cycle', {})
    if not isinstance(cycle, dict) or str(cycle.get('status')) != 'COMPLETE':
        raise RuntimeError('v3.7-regimebron heeft geen complete cyclus')
    return {
        'regime': str(cycle.get('regime', 'DATA_UNCERTAIN')),
        'raw_regime': str(cycle.get('raw_regime', 'DATA_UNCERTAIN')),
        'stable_regime': str(cycle.get('regime', 'DATA_UNCERTAIN')),
        'bull_breadth_pct': float(cycle.get('bull_breadth_pct', 0.0) or 0.0),
        'bear_breadth_pct': float(cycle.get('bear_breadth_pct', 0.0) or 0.0),
        'source_generated_ms': generated,
    }


def _discovery_shortlist(now_ms: int) -> tuple[int, list[dict[str, Any]]]:
    path = Path(os.getenv('V38_DB_PATH', _data_path('cryptobot_autonomous_v38.db')))
    if not path.exists():
        raise RuntimeError(f'v3.8 discoverydatabase ontbreekt: {path}')
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute('SELECT MAX(evaluated_ms) AS value FROM v38_decisions').fetchone()
        scan_ms = int(row['value'] or 0)
        if scan_ms <= 0 or now_ms - scan_ms > 3 * 60_000:
            raise RuntimeError('laatste v3.8 discoveryscan ontbreekt of is ouder dan drie minuten')
        rows = conn.execute(
            '''SELECT market,score,details_json FROM v38_decisions
               WHERE evaluated_ms=? AND action='DOOR_NAAR_MENSELIJKE_JURY' AND score>=?
               ORDER BY score DESC,market LIMIT ?''',
            (scan_ms, MIN_DISCOVERY_SCORE, MAX_DISCOVERY_CANDIDATES),
        ).fetchall()
    finally:
        conn.close()
    result: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, 1):
        try:
            details = json.loads(str(row['details_json']))
        except ValueError:
            details = {}
        result.append({
            'market': str(row['market']),
            'score': float(row['score']),
            'rank': rank,
            'scan_ms': scan_ms,
            'features': details.get('features', {}),
            'volume_quote': details.get('volume_quote', 0.0),
        })
    return scan_ms, result


def evaluate_discovery_cycle(
    settings: V37Settings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    try:
        regime = _regime_override(current)
        scan_ms, shortlist = _discovery_shortlist(current)
    except Exception as exc:
        conn = _connect(settings)
        try:
            _set_meta(conn, 'v39_discovery_attempted_ms', current)
            _set_meta(conn, 'v39_discovery_error', f'{type(exc).__name__}: {exc}')
            conn.commit()
        finally:
            conn.close()
        return {'evaluated': False, 'reason': str(exc)}
    markets = [item['market'] for item in shortlist]
    overlays = {
        item['market']: {'discovery': {
            'score': item['score'], 'rank': item['rank'], 'scan_ms': item['scan_ms'],
            'features': item['features'], 'volume_quote': item['volume_quote'],
            'minimum_score': MIN_DISCOVERY_SCORE,
        }}
        for item in shortlist
    }
    result = evaluate_new_five_minute_cycle(
        settings,
        api=api,
        now_ms=current,
        universe_override=markets,
        regime_override=regime,
        context_overlays=overlays,
    )
    conn = _connect(settings)
    try:
        _set_meta(conn, 'v39_discovery_attempted_ms', current)
        _set_meta(conn, 'v39_discovery_generated_ms', current)
        _set_meta(conn, 'v39_discovery_scan_ms', scan_ms)
        _set_meta(conn, 'v39_discovery_error', '')
        _set_meta(conn, 'v39_shortlist_json', json.dumps(shortlist, ensure_ascii=False))
        _set_meta(conn, 'v39_regime_json', json.dumps(regime, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return {**result, 'shortlist_count': len(shortlist), 'discovery_scan_ms': scan_ms}


def _sync_opportunities(settings: V37Settings, now_ms: int) -> int:
    conn = _connect(settings)
    inserted = 0
    try:
        collection_end = int(_meta(conn, 'v39_collection_end_ms', '0') or 0)
        events = conn.execute(
            '''SELECT id,event_ms,market,details_json FROM v37_events
               WHERE event_type='SHADOW_OPPORTUNITY'
                 AND id NOT IN (SELECT source_event_id FROM v39_opportunities)
               ORDER BY id'''
        ).fetchall()
        for event in events:
            details = json.loads(str(event['details_json']))
            cycle_ms = int(details.get('cycle_ms', 0) or 0)
            market = str(event['market'])
            observed_ms = int(event['event_ms'])
            edge = details.get('edge', {}) if isinstance(details, dict) else {}
            entry = float(edge.get('buy_vwap', 0.0) or 0.0)
            decision = conn.execute(
                '''SELECT details_json FROM v37_decisions
                   WHERE cycle_ms=? AND market=? ORDER BY evaluated_ms DESC LIMIT 1''',
                (cycle_ms, market),
            ).fetchone()
            decision_details = json.loads(str(decision['details_json'])) if decision else {}
            discovery = decision_details.get('discovery', {})
            score = float(discovery.get('score', 0.0) or 0.0)
            rank = int(discovery.get('rank', 0) or 0)
            reason = 'GEKWALIFICEERD'
            qualified = 1
            if collection_end > 0 and observed_ms > collection_end:
                qualified, reason = 0, 'BUITEN_VASTE_72U_MEETPERIODE'
            elif entry <= 0.0 or score < MIN_DISCOVERY_SCORE or not 1 <= rank <= 10:
                qualified, reason = 0, 'ONGELDIGE_DISCOVERY_OF_INSTAP'
            elif conn.execute(
                '''SELECT 1 FROM v39_opportunities WHERE market=? AND qualified=1
                   AND observed_ms>? LIMIT 1''',
                (market, observed_ms - QUALIFICATION_COOLDOWN_MS),
            ).fetchone():
                qualified, reason = 0, 'VIER_UUR_MARKTCOOLDOWN'
            elif int(conn.execute(
                'SELECT COUNT(*) FROM v39_opportunities WHERE cycle_ms=? AND qualified=1',
                (cycle_ms,),
            ).fetchone()[0]) >= MAX_QUALIFIED_PER_CYCLE:
                qualified, reason = 0, 'MAX_TWEE_KANSEN_PER_CYCLUS'
            base_amount = settings.position_eur / entry if entry > 0.0 else 0.0
            conn.execute(
                '''INSERT INTO v39_opportunities
                   (source_event_id,cycle_ms,observed_ms,market,qualified,
                    qualification_reason,discovery_score,discovery_rank,
                    entry_buy_vwap,base_amount,details_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    int(event['id']), cycle_ms, observed_ms, market, qualified, reason,
                    score, rank, entry, base_amount,
                    json.dumps(details, ensure_ascii=False),
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def monitor_outcomes(
    settings: V37Settings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    market_api = api or BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    inserted_opportunities = _sync_opportunities(settings, current)
    conn = _connect(settings)
    try:
        _set_meta(conn, 'v39_outcome_attempted_ms', current)
        pending: list[tuple[sqlite3.Row, int]] = []
        rows = conn.execute(
            'SELECT * FROM v39_opportunities WHERE qualified=1 ORDER BY observed_ms,id'
        ).fetchall()
        for row in rows:
            existing = {
                int(item[0]) for item in conn.execute(
                    'SELECT horizon_minutes FROM v39_outcomes WHERE opportunity_id=?',
                    (int(row['id']),),
                )
            }
            for horizon in OUTCOME_HORIZONS_MINUTES:
                if horizon not in existing and current >= int(row['observed_ms']) + horizon * 60_000:
                    pending.append((row, horizon))
        conn.commit()
    finally:
        conn.close()
    measured = 0
    errors: list[str] = []
    for row, horizon in pending:
        try:
            book = market_api.sell_vwap_for_base(str(row['market']), float(row['base_amount']))
            sell = float(book['sell_vwap'])
            entry = float(row['entry_buy_vwap'])
            gross = (sell / entry - 1.0) * 100.0
            net = gross - 2.0 * (settings.taker_fee_pct + settings.slippage_pct)
            conn = _connect(settings)
            try:
                conn.execute(
                    '''INSERT OR IGNORE INTO v39_outcomes
                       (opportunity_id,horizon_minutes,measured_ms,sell_vwap,
                        gross_return_pct,net_return_pct,details_json)
                       VALUES (?,?,?,?,?,?,?)''',
                    (
                        int(row['id']), horizon, current, sell, gross, net,
                        json.dumps(book, ensure_ascii=False),
                    ),
                )
                conn.commit()
                measured += 1
            finally:
                conn.close()
        except Exception as exc:
            errors.append(f"{row['market']} {horizon}m: {type(exc).__name__}: {exc}")
    conn = _connect(settings)
    try:
        _set_meta(conn, 'v39_outcome_generated_ms', current)
        _set_meta(conn, 'v39_outcome_errors', json.dumps(errors, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return {
        'new_opportunities': inserted_opportunities,
        'pending': len(pending),
        'measured': measured,
        'errors': errors,
    }


def _outcome_summary(conn: sqlite3.Connection, horizon: int) -> dict[str, Any]:
    values = [
        float(row[0]) for row in conn.execute(
            'SELECT net_return_pct FROM v39_outcomes WHERE horizon_minutes=?', (horizon,)
        )
    ]
    wins = [value for value in values if value > 0.0]
    losses = [-value for value in values if value < 0.0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    return {
        'samples': len(values),
        'average_net_pct': round(sum(values) / len(values), 5) if values else None,
        'median_net_pct': round(median(values), 5) if values else None,
        'win_rate_pct': round(len(wins) / len(values) * 100.0, 2) if values else None,
        'profit_factor': round(gross_profit / gross_loss, 4) if gross_loss > 0.0 else None,
        'gross_profit_pct_points': round(gross_profit, 5),
        'gross_loss_pct_points': round(gross_loss, 5),
    }


def build_report(settings: V37Settings, now_ms: int | None = None) -> dict[str, Any]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    base = build_v37_report(settings, current)
    conn = _connect(settings)
    try:
        initialized = int(_meta(conn, 'v39_initialized_ms', '0') or 0)
        collection_end = int(_meta(conn, 'v39_collection_end_ms', '0') or 0)
        decision_at = int(_meta(conn, 'v39_decision_at_ms', '0') or 0)
        shortlist = json.loads(_meta(conn, 'v39_shortlist_json', '[]') or '[]')
        regime = json.loads(_meta(conn, 'v39_regime_json', '{}') or '{}')
        qualified = int(conn.execute(
            'SELECT COUNT(*) FROM v39_opportunities WHERE qualified=1'
        ).fetchone()[0])
        rejected = dict(conn.execute(
            '''SELECT qualification_reason,COUNT(*) FROM v39_opportunities
               WHERE qualified=0 GROUP BY qualification_reason'''
        ).fetchall())
        outcomes = {
            str(horizon): _outcome_summary(conn, horizon)
            for horizon in OUTCOME_HORIZONS_MINUTES
        }
        cycles = int(conn.execute(
            '''SELECT COUNT(*) FROM v37_cycles WHERE status='COMPLETE'
               AND evaluated_ms<=?''', (collection_end or current,)
        ).fetchone()[0])
        heartbeat = {
            **base.get('heartbeat', {}),
            'discovery_attempted_ms': int(_meta(conn, 'v39_discovery_attempted_ms', '0') or 0),
            'outcome_attempted_ms': int(_meta(conn, 'v39_outcome_attempted_ms', '0') or 0),
        }
    finally:
        conn.close()
    expected_cycles = COLLECTION_HOURS * 12
    completeness = min(100.0, cycles / expected_cycles * 100.0) if expected_cycles else 0.0
    mature = outcomes['240']
    criteria = {
        'data_completeness': completeness >= MIN_DATA_COMPLETENESS_PCT,
        'qualified_opportunities': qualified >= MIN_QUALIFIED_OPPORTUNITIES,
        'mature_240m_outcomes': int(mature['samples']) >= MIN_MATURE_240M_OUTCOMES,
        'average_240m_net_positive': (
            mature['average_net_pct'] is not None and mature['average_net_pct'] > 0.0
        ),
        'median_240m_net_positive': (
            mature['median_net_pct'] is not None and mature['median_net_pct'] > 0.0
        ),
        'profit_factor_240m': (
            (
                mature['gross_loss_pct_points'] == 0.0
                and mature['gross_profit_pct_points'] > 0.0
            )
            or (
                mature['profit_factor'] is not None
                and mature['profit_factor'] >= MIN_PROFIT_FACTOR
            )
        ),
        'win_rate_240m': (
            mature['win_rate_pct'] is not None and mature['win_rate_pct'] >= MIN_WIN_RATE_PCT
        ),
    }
    if current < collection_end:
        decision = 'LOOPT'
    elif current < decision_at:
        decision = 'AFRONDEN_240M_UITKOMSTEN'
    else:
        decision = 'PAPER_GO' if all(criteria.values()) else 'AFWIJZEN_GEEN_VERLENGING'
    base.pop('observation_validation', None)
    base.update({
        'version': '3.9',
        'component': 'FULL_EUR_HUMAN_PIPELINE_V39',
        'generated_at_ms': current,
        'generated_at_utc': datetime.fromtimestamp(current / 1000, timezone.utc).isoformat(),
        'modes': {
            'v36_control': 'ONGEWIJZIGD EN AFZONDERLIJK',
            'v37_control': 'ONGEWIJZIGD EN AFZONDERLIJK',
            'v38_control': 'ONGEWIJZIGD EN AFZONDERLIJK',
            'v39': 'OBSERVE-ONLY / VOLLEDIGE BREDE MENSELIJKE KETEN',
            'paper_execution': 'UIT',
            'live_orders': 'UIT / TECHNISCH ONMOGELIJK',
        },
        'safety': {
            'execution_enabled': False,
            'positions_or_orders_possible': False,
            'existing_assets_excluded': True,
        },
        'discovery': {
            'minimum_score': MIN_DISCOVERY_SCORE,
            'maximum_shortlist': MAX_DISCOVERY_CANDIDATES,
            'current_shortlist': shortlist,
            'current_shortlist_count': len(shortlist),
            'regime_source': regime,
            'flow': 'v3.8 alle EUR -> score >=65 -> top 10 -> v3.7 poorten -> L2',
        },
        'prospective_results': {
            'qualified_opportunities': qualified,
            'rejected_duplicates_or_limits': rejected,
            'outcomes': outcomes,
            'roundtrip_cost_method': 'exacte L2 VWAP plus 2x takerfee en 2x vaste slippage',
        },
        'fixed_evaluation': {
            'collection_hours': COLLECTION_HOURS,
            'settlement_hours': SETTLEMENT_HOURS,
            'started_at_ms': initialized,
            'collection_end_ms': collection_end,
            'decision_at_ms': decision_at,
            'complete_cycles': cycles,
            'expected_cycles': expected_cycles,
            'data_completeness_pct': round(completeness, 2),
            'minimum_data_completeness_pct': MIN_DATA_COMPLETENESS_PCT,
            'minimum_qualified_opportunities': MIN_QUALIFIED_OPPORTUNITIES,
            'minimum_mature_240m_outcomes': MIN_MATURE_240M_OUTCOMES,
            'minimum_profit_factor': MIN_PROFIT_FACTOR,
            'minimum_win_rate_pct': MIN_WIN_RATE_PCT,
            'criteria_pass': criteria,
            'decision': decision,
            'automatic_paper_activation': False,
            'extension_allowed': False,
        },
        'heartbeat': heartbeat,
    })
    return base


def write_report(settings: V37Settings, report: dict[str, Any]) -> None:
    path = Path(settings.report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def load_report(settings: V37Settings) -> dict[str, Any]:
    return _read_json(Path(settings.report_path))


def print_status(report: dict[str, Any]) -> None:
    discovery = report.get('discovery', {})
    results = report.get('prospective_results', {})
    evaluation = report.get('fixed_evaluation', {})
    modes = report.get('modes', {})
    print('=== CRYPTOBOT v3.9 | VOLLEDIGE MENSELIJKE KETEN ===')
    print(f"UTC                   : {report.get('generated_at_utc', 'onbekend')}")
    print(f"MODUS                 : {modes.get('v39', 'ONBEKEND')}")
    print(f"PAPER-UITVOERING      : {modes.get('paper_execution', 'UIT')}")
    print(f"LIVE ORDERS           : {modes.get('live_orders', 'UIT')}")
    print(f"KETEN                 : {discovery.get('flow', '-')}")
    print(
        f"DISCOVERY             : score >= {float(discovery.get('minimum_score', 0)):.0f}"
        f" | shortlist {int(discovery.get('current_shortlist_count', 0))}/10"
    )
    for item in discovery.get('current_shortlist', [])[:10]:
        print(
            f"  {item.get('market', '?'):<14} | score {float(item.get('score', 0)):.1f}"
            f" | rang {int(item.get('rank', 0))}"
        )
    print(
        f"SCHADUWKANSEN         : {int(results.get('qualified_opportunities', 0))} gekwalificeerd"
    )
    for horizon in OUTCOME_HORIZONS_MINUTES:
        item = results.get('outcomes', {}).get(str(horizon), {})
        average = item.get('average_net_pct')
        pf = item.get('profit_factor')
        print(
            f"  {horizon:>3}m netto          : n={int(item.get('samples', 0))}"
            f" | gem {'-' if average is None else f'{float(average):+.3f}%'}"
            f" | PF {'-' if pf is None else f'{float(pf):.3f}'}"
        )
    print(
        f"VASTE METING          : {float(evaluation.get('data_completeness_pct', 0)):.1f}%"
        f" | besluit {evaluation.get('decision', 'ONBEKEND')}"
    )
    if evaluation.get('decision') == 'LOOPT':
        remaining = max(0, int(evaluation.get('collection_end_ms', 0)) - int(report.get('generated_at_ms', 0)))
        print(f"RESTEREND             : {remaining / HOUR_MS:.1f} uur tot einde dataverzameling")
    print('Geen automatische activatie; na het vaste beslismoment geen verlenging.')


def refresh_report(settings: V37Settings, now_ms: int | None = None) -> dict[str, Any]:
    report = build_report(settings, now_ms)
    write_report(settings, report)
    return report


def run_once(
    settings: V37Settings,
    *,
    api: BitvavoPublic | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    ensure_v39(settings, current)
    cycle = evaluate_discovery_cycle(settings, api=api, now_ms=current)
    candidates = recheck_candidates(settings, api=api, now_ms=current)
    outcomes = monitor_outcomes(settings, api=api, now_ms=current)
    report = refresh_report(settings, current)
    return {'cycle': cycle, 'candidates': candidates, 'outcomes': outcomes, 'report': report}


def main() -> int:
    parser = argparse.ArgumentParser(description='CryptoBot v3.9 brede menselijke observe-only keten')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    settings = settings_from_env()
    settings.validate()
    ensure_v39(settings)
    if args.status:
        report = load_report(settings) or refresh_report(settings)
        print_status(report)
        return 0
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if args.once:
        print_status(run_once(settings)['report'])
        return 0
    api = BitvavoPublic(
        settings.api_base_url, settings.request_timeout_seconds, settings.request_retries
    )
    next_cycle = next_candidate = next_outcome = next_report = 0.0
    while not STOP:
        now = time.time()
        if now >= next_cycle:
            try:
                evaluate_discovery_cycle(settings, api=api, now_ms=int(time.time() * 1000))
            except Exception:
                logger.exception('v3.9 discoverycyclus mislukt')
            next_cycle = now + settings.cycle_poll_seconds
        if now >= next_candidate:
            try:
                recheck_candidates(settings, api=api, now_ms=int(time.time() * 1000))
            except Exception:
                logger.exception('v3.9 L2-kandidaatcontrole mislukt')
            next_candidate = now + settings.candidate_recheck_seconds
        if now >= next_outcome:
            try:
                monitor_outcomes(settings, api=api, now_ms=int(time.time() * 1000))
            except Exception:
                logger.exception('v3.9 uitkomstmeting mislukt')
            next_outcome = now + 30
        if now >= next_report:
            try:
                refresh_report(settings, int(time.time() * 1000))
            except Exception:
                logger.exception('v3.9 rapportage mislukt')
            next_report = now + settings.report_seconds
        time.sleep(0.5)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
