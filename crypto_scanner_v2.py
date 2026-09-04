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

from adaptive_ls_strategy import AdaptiveLongShortStrategy
from adaptive_ls_strict_strategy import StrictAdaptiveLongShortStrategy
from bitvavo_public import BitvavoPublic
from config import Settings
from crypto_scanner import (
    _direction_score,
    _fmt_price,
    _movement_proxy_pct,
    _price_plan,
    _reasons,
    _sideways_score,
)
from strategy import BandReentryStrategy

logger = logging.getLogger('cryptobot_scanner_v2')
STOP = False

EUR_TAKER_FEE_PCT = 0.25
USDC_TAKER_FEE_PCT = 0.05
USDC_EUR_TAKER_FEE_PCT = 0.10
TRADE_GRADE_SCORE = 80.0
TRADE_GRADE_COST_MULTIPLE = 3.0
TRADE_GRADE_MIN_NET_RR = 1.50
WATCH_SCORE = 65.0
WATCH_COST_MULTIPLE = 2.0
TRADE_GRADE_MAX_SPREAD_PCT = 0.20
REPORT_STALE_SECONDS = 35 * 60
SIGNAL_MAX_HOLD_MS = 24 * 60 * 60 * 1000
AUDIT_WINDOW_MS = 24 * 60 * 60 * 1000


def _report_path() -> Path:
    raw = os.getenv('SCANNER_V3_REPORT_PATH') or os.getenv('SCANNER_V2_REPORT_PATH')
    if raw:
        return Path(raw)
    data = Path('/var/data')
    if data.exists() and os.access(data, os.W_OK):
        return data / 'cryptobot_scanner_v3.json'
    return Path('data') / 'cryptobot_scanner_v3.json'


def _db_path() -> Path:
    raw = os.getenv('SCANNER_V3_DB_PATH')
    if raw:
        return Path(raw)
    data = Path('/var/data')
    if data.exists() and os.access(data, os.W_OK):
        return data / 'cryptobot_scanner_v3.db'
    return Path('data') / 'cryptobot_scanner_v3.db'


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def _base_asset(market: str) -> str:
    return market.rsplit('-', 1)[0].upper()


def _quote_asset(market: str) -> str:
    return market.rsplit('-', 1)[-1].upper()


def _taker_fee_pct(quote: str) -> float:
    if quote == 'USDC':
        return USDC_TAKER_FEE_PCT
    return EUR_TAKER_FEE_PCT


def _roundtrip_cost_pct(
    settings: Settings,
    execution_spread_pct: float,
    quote: str,
    conversion_cost_pct: float = 0.0,
) -> float:
    return (
        2.0 * _taker_fee_pct(quote)
        + 2.0 * settings.slippage_pct
        + max(0.0, execution_spread_pct)
        + max(0.0, conversion_cost_pct)
    )


def _conversion_cost_pct(settings: Settings, conversion_book: dict[str, float] | None) -> float:
    if conversion_book is None:
        return 0.0
    return (
        2.0 * USDC_EUR_TAKER_FEE_PCT
        + 2.0 * settings.slippage_pct
        + max(0.0, float(conversion_book['execution_spread_pct']))
    )


def _net_reward_risk(
    *, entry: float, stop: float, target: float, side: str, roundtrip_cost_pct: float
) -> tuple[float, float, float]:
    if min(entry, stop, target) <= 0.0:
        return -999.0, 999.0, 0.0
    if side == 'SHORT':
        gross_reward = (entry - target) / entry * 100.0
        gross_risk = (stop - entry) / entry * 100.0
    else:
        gross_reward = (target - entry) / entry * 100.0
        gross_risk = (entry - stop) / entry * 100.0
    net_reward = gross_reward - roundtrip_cost_pct
    total_risk = gross_risk + roundtrip_cost_pct
    ratio = net_reward / total_risk if net_reward > 0.0 and total_risk > 0.0 else 0.0
    return net_reward, total_risk, ratio


def _grade_action(
    regime: str,
    decision_action: str,
    score: float,
    cost_multiple: float,
    spread_pct: float,
    *,
    price_in_zone: bool = True,
    net_reward_risk: float = TRADE_GRADE_MIN_NET_RR,
) -> str:
    desired = 'LONG' if regime == 'BULL' else 'SHORT' if regime == 'BEAR' else ''
    if not desired:
        return 'GEEN TRADE'
    if (
        decision_action == desired
        and score >= TRADE_GRADE_SCORE
        and cost_multiple >= TRADE_GRADE_COST_MULTIPLE
        and spread_pct <= TRADE_GRADE_MAX_SPREAD_PCT
        and price_in_zone
        and net_reward_risk >= TRADE_GRADE_MIN_NET_RR
    ):
        return f'{desired} TRADE-GRADE'
    if decision_action == desired and score >= WATCH_SCORE and cost_multiple >= WATCH_COST_MULTIPLE:
        return f'{desired} WATCH'
    return 'GEEN TRADE'


def _pair_snapshot(
    *,
    api: BitvavoPublic,
    settings: Settings,
    analyzer: StrictAdaptiveLongShortStrategy,
    directional: AdaptiveLongShortStrategy,
    band: BandReentryStrategy,
    market: str,
    regime: str,
    bull_breadth: float,
    bear_breadth: float,
    candle_limit: int,
    cached_candles: list | None = None,
    cached_metrics: dict[str, float] | None = None,
    conversion_book: dict[str, float] | None = None,
) -> dict[str, Any]:
    quote = _quote_asset(market)
    candles = cached_candles or api.closed_candles(market, settings.interval, candle_limit)
    metrics = cached_metrics or analyzer.analyze(candles)
    if not metrics:
        raise RuntimeError('onvoldoende analyse-data')

    depth = api.depth_book(market, settings.position_eur)
    spread_pct = float(depth['spread_pct'])
    execution_spread_pct = float(depth['execution_spread_pct'])
    conversion_cost_pct = _conversion_cost_pct(settings, conversion_book) if quote == 'USDC' else 0.0
    cost_pct = _roundtrip_cost_pct(
        settings,
        execution_spread_pct,
        quote,
        conversion_cost_pct=conversion_cost_pct,
    )
    movement_proxy = _movement_proxy_pct(metrics)
    cost_multiple = movement_proxy / cost_pct if cost_pct > 0 else 0.0
    close = float(metrics['close'])
    atr_pct = float(metrics['atr_pct'])

    side = 'NONE'
    score = 0.0
    decision_action = 'SKIP'
    decision_reason = ''
    action = 'GEEN TRADE'

    if regime == 'BULL':
        side = 'LONG'
        score = _direction_score(metrics, side, cost_pct, execution_spread_pct, settings.max_spread_pct)
        decision = directional.evaluate_metrics(metrics, regime, bull_breadth, bear_breadth)
        decision_action = decision.action
        decision_reason = decision.reason
    elif regime == 'BEAR':
        side = 'SHORT'
        score = _direction_score(metrics, side, cost_pct, execution_spread_pct, settings.max_spread_pct)
        decision = analyzer.evaluate_metrics(metrics, regime, bull_breadth, bear_breadth)
        decision_action = decision.action
        decision_reason = decision.reason
    elif regime == 'SIDEWAYS':
        side = 'LONG'
        band_decision = band.evaluate(candles)
        score = _sideways_score(
            band_decision.metrics,
            movement_proxy,
            cost_pct,
            execution_spread_pct,
            settings.max_spread_pct,
        )
        decision_action = band_decision.action
        decision_reason = band_decision.reason
        if band_decision.action == 'BUY' and score >= 55.0 and cost_multiple >= WATCH_COST_MULTIPLE:
            action = 'SIDEWAYS WATCH'

    plan = _price_plan(close, atr_pct, cost_pct, side if side != 'NONE' else 'LONG')
    executable_entry = float(depth['sell_vwap'] if side == 'SHORT' else depth['buy_vwap'])
    price_in_zone = float(plan['entry_zone_low']) <= executable_entry <= float(plan['entry_zone_high'])
    net_reward_pct, total_risk_pct, net_reward_risk = _net_reward_risk(
        entry=executable_entry,
        stop=float(plan['stop_hint']),
        target=float(plan['target_hint']),
        side=side if side != 'NONE' else 'LONG',
        roundtrip_cost_pct=cost_pct,
    )
    if regime in {'BULL', 'BEAR'}:
        action = _grade_action(
            regime,
            decision_action,
            score,
            cost_multiple,
            execution_spread_pct,
            price_in_zone=price_in_zone,
            net_reward_risk=net_reward_risk,
        )
    reasons = _reasons(metrics, side, cost_multiple, execution_spread_pct)
    if quote == 'USDC':
        reasons.insert(0, 'USDC-route inclusief EUR↔USDC-omwisseling')
    if not price_in_zone:
        reasons.insert(0, 'actuele uitvoerprijs buiten besliszone')
    elif net_reward_risk < TRADE_GRADE_MIN_NET_RR:
        reasons.insert(0, f'netto R/R {net_reward_risk:.2f} < {TRADE_GRADE_MIN_NET_RR:.2f}')
    if regime == 'SIDEWAYS' and action == 'SIDEWAYS WATCH':
        reasons.insert(0, 'sideways alleen observatie')

    return {
        'market': market,
        'base': _base_asset(market),
        'quote': quote,
        'action': action,
        'side': side,
        'score': round(score, 1),
        'decision_action': decision_action,
        'decision_reason': decision_reason,
        'spread_pct': round(spread_pct, 4),
        'execution_spread_pct': round(execution_spread_pct, 4),
        'shadow_notional_eur': round(settings.position_eur, 2),
        'best_bid': round(float(depth['bid']), 8),
        'best_ask': round(float(depth['ask']), 8),
        'sell_vwap': round(float(depth['sell_vwap']), 8),
        'buy_vwap': round(float(depth['buy_vwap']), 8),
        'executable_entry': round(executable_entry, 8),
        'bid_depth_quote': round(float(depth['bid_depth_quote']), 2),
        'ask_depth_quote': round(float(depth['ask_depth_quote']), 2),
        'price_in_zone': price_in_zone,
        'taker_fee_pct': _taker_fee_pct(quote),
        'conversion_cost_pct': round(conversion_cost_pct, 4),
        'roundtrip_cost_pct': round(cost_pct, 4),
        'movement_proxy_pct': round(movement_proxy, 4),
        'cost_multiple': round(cost_multiple, 2),
        'three_x_cost_margin_pct': round(movement_proxy - 3.0 * cost_pct, 4),
        'atr_pct': round(atr_pct, 4),
        'momentum_pct': round(float(metrics.get('momentum_pct', 0.0)), 4),
        'net_reward_pct': round(net_reward_pct, 4),
        'total_risk_pct': round(total_risk_pct, 4),
        'net_reward_risk': round(net_reward_risk, 3),
        'latest_candle_ms': int(candles[-1].timestamp_ms),
        'latest_candle_high': round(float(candles[-1].high), 8),
        'latest_candle_low': round(float(candles[-1].low), 8),
        'latest_candle_close': round(float(candles[-1].close), 8),
        '_outcome_candles': [
            [int(c.timestamp_ms), float(c.high), float(c.low), float(c.close)]
            for c in candles[-100:]
        ],
        'reasons': reasons[:5],
        **{key: round(value, 8) for key, value in plan.items()},
    }


def _display_action(action: object) -> str:
    text = str(action or 'GEEN TRADE')
    if text == 'LONG TRADE-GRADE':
        return 'ZELDZAME KANS LONG'
    if text == 'SHORT TRADE-GRADE':
        return 'ZELDZAME KANS SHORT'
    return text


def _rank_key(row: dict[str, Any]) -> tuple[float, float, float, float]:
    action = str(row.get('action', ''))
    grade = 2.0 if 'TRADE-GRADE' in action else 1.0 if 'WATCH' in action else 0.0
    return (
        grade,
        float(row.get('three_x_cost_margin_pct', -999.0)),
        float(row.get('score', 0.0)),
        float(row.get('cost_multiple', 0.0)),
    )


def scan_once(settings: Settings) -> dict[str, object]:
    api = BitvavoPublic(
        settings.api_base_url,
        settings.request_timeout_seconds,
        settings.request_retries,
    )
    analyzer = StrictAdaptiveLongShortStrategy(settings)
    directional = AdaptiveLongShortStrategy(settings)
    band = BandReentryStrategy(settings)

    eur_markets = api.top_markets_by_quote_volume('EUR', settings.universe_size)
    usdc_markets = set(api.trading_markets('USDC'))
    candle_limit = max(settings.candle_limit, analyzer.required_candles() + 8)

    eur_candles: dict[str, list] = {}
    eur_metrics: dict[str, dict[str, float]] = {}
    errors: list[str] = []
    conversion_book: dict[str, float] | None = None
    if usdc_markets:
        try:
            conversion_book = api.depth_book('USDC-EUR', settings.position_eur)
        except Exception as exc:
            errors.append(f'USDC-EUR omwisseling: {type(exc).__name__}: {exc}')

    for market in eur_markets:
        try:
            candles = api.closed_candles(market, settings.interval, candle_limit)
            metrics = analyzer.analyze(candles)
            if not metrics:
                errors.append(f'{market}: onvoldoende analyse-data')
                continue
            eur_candles[market] = candles
            eur_metrics[market] = metrics
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    regime, bull_breadth, bear_breadth = analyzer.market_regime(eur_metrics)
    chosen: list[dict[str, Any]] = []
    all_pairs: list[dict[str, Any]] = []
    usdc_available = 0

    for eur_market in eur_markets:
        if eur_market not in eur_metrics:
            continue
        base = _base_asset(eur_market)
        pair_options = [eur_market]
        usdc_market = f'{base}-USDC'
        if usdc_market in usdc_markets and conversion_book is not None:
            pair_options.append(usdc_market)

        rows: list[dict[str, Any]] = []
        for market in pair_options:
            try:
                if market == eur_market:
                    row = _pair_snapshot(
                        api=api,
                        settings=settings,
                        analyzer=analyzer,
                        directional=directional,
                        band=band,
                        market=market,
                        regime=regime,
                        bull_breadth=bull_breadth,
                        bear_breadth=bear_breadth,
                        candle_limit=candle_limit,
                        cached_candles=eur_candles[market],
                        cached_metrics=eur_metrics[market],
                    )
                else:
                    row = _pair_snapshot(
                        api=api,
                        settings=settings,
                        analyzer=analyzer,
                        directional=directional,
                        band=band,
                        market=market,
                        regime=regime,
                        bull_breadth=bull_breadth,
                        bear_breadth=bear_breadth,
                        candle_limit=candle_limit,
                        conversion_book=conversion_book,
                    )
                rows.append(row)
                all_pairs.append(row)
            except Exception as exc:
                errors.append(f'{market}: {type(exc).__name__}: {exc}')

        if any(str(row.get('quote')) == 'USDC' for row in rows):
            usdc_available += 1
        if rows:
            best = max(rows, key=_rank_key)
            best = {**best, 'pair_options': [row['market'] for row in rows]}
            chosen.append(best)

    chosen.sort(key=_rank_key, reverse=True)
    trade_grade = [row for row in chosen if 'TRADE-GRADE' in str(row.get('action', ''))]
    generated_ms = int(time.time() * 1000)
    return {
        'version': '3.0',
        'mode': 'STRICT_L2_READ_ONLY_SCANNER',
        'generated_at_ms': generated_ms,
        'generated_at_utc': datetime.fromtimestamp(generated_ms / 1000.0, tz=timezone.utc).isoformat(),
        'interval': settings.interval,
        'reference_universe': eur_markets,
        'regime': regime,
        'bull_breadth_pct': round(bull_breadth, 1),
        'bear_breadth_pct': round(bear_breadth, 1),
        'valid_reference_markets': len(eur_metrics),
        'usdc_available_for_reference_assets': usdc_available,
        'trade_grade_count': len(trade_grade),
        'trade_grade': trade_grade,
        'rare_opportunity_count': len(trade_grade),
        'rare_opportunities': trade_grade,
        'top3': chosen[:3],
        'candidates': chosen,
        'all_pair_snapshots': all_pairs,
        'errors': errors,
        'rules': {
            'trade_grade_score_min': TRADE_GRADE_SCORE,
            'trade_grade_cost_multiple_min': TRADE_GRADE_COST_MULTIPLE,
            'trade_grade_max_spread_pct': TRADE_GRADE_MAX_SPREAD_PCT,
            'trade_grade_min_net_reward_risk': TRADE_GRADE_MIN_NET_RR,
            'price_must_be_in_zone': True,
            'shadow_notional_eur': settings.position_eur,
            'eur_taker_fee_pct': EUR_TAKER_FEE_PCT,
            'usdc_taker_fee_pct': USDC_TAKER_FEE_PCT,
            'usdc_eur_taker_fee_pct': USDC_EUR_TAKER_FEE_PCT,
        },
        'note': 'Zeldzame kansen zijn een strenge beslisfilter, geen koop- of verkoopadvies. De scanner opent nooit orders.',
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


def _scanner_db_connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS snapshots (
            generated_ms INTEGER NOT NULL,
            market TEXT NOT NULL,
            action TEXT NOT NULL,
            score REAL NOT NULL,
            executable_entry REAL NOT NULL,
            roundtrip_cost_pct REAL NOT NULL,
            net_reward_risk REAL NOT NULL,
            price_in_zone INTEGER NOT NULL,
            regime TEXT NOT NULL DEFAULT 'UNKNOWN',
            side TEXT NOT NULL DEFAULT 'NONE',
            decision_action TEXT NOT NULL DEFAULT 'SKIP',
            decision_reason TEXT NOT NULL DEFAULT '',
            cost_multiple REAL NOT NULL DEFAULT 0,
            execution_spread_pct REAL NOT NULL DEFAULT 0,
            reasons_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (generated_ms, market)
        )'''
    )
    existing_snapshot_columns = {
        str(row[1]) for row in conn.execute('PRAGMA table_info(snapshots)').fetchall()
    }
    snapshot_migrations = {
        'regime': "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        'side': "TEXT NOT NULL DEFAULT 'NONE'",
        'decision_action': "TEXT NOT NULL DEFAULT 'SKIP'",
        'decision_reason': "TEXT NOT NULL DEFAULT ''",
        'cost_multiple': 'REAL NOT NULL DEFAULT 0',
        'execution_spread_pct': 'REAL NOT NULL DEFAULT 0',
        'reasons_json': "TEXT NOT NULL DEFAULT '[]'",
    }
    for column, declaration in snapshot_migrations.items():
        if column not in existing_snapshot_columns:
            conn.execute(f'ALTER TABLE snapshots ADD COLUMN {column} {declaration}')
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS signals (
            generated_ms INTEGER NOT NULL,
            market TEXT NOT NULL,
            side TEXT NOT NULL,
            signal_candle_ms INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            stop_price REAL NOT NULL,
            target_price REAL NOT NULL,
            roundtrip_cost_pct REAL NOT NULL,
            net_reward_risk REAL NOT NULL,
            status TEXT NOT NULL,
            evaluated_candle_ms INTEGER,
            exit_price REAL,
            outcome TEXT,
            net_return_pct REAL,
            PRIMARY KEY (generated_ms, market)
        )'''
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_scanner_one_open_signal "
        "ON signals(market) WHERE status='OPEN'"
    )
    conn.commit()
    return conn


def _signal_net_return(side: str, entry: float, exit_price: float, cost_pct: float) -> float:
    gross = (
        (entry - exit_price) / entry * 100.0
        if side == 'SHORT'
        else (exit_price - entry) / entry * 100.0
    )
    return gross - cost_pct


def _persist_signal_research(report: dict[str, object]) -> dict[str, object]:
    generated_ms = int(report.get('generated_at_ms', 0))
    snapshots = report.get('candidates', [])
    all_pairs = report.get('all_pair_snapshots', [])
    rare = report.get('rare_opportunities', [])
    pair_by_market = {
        str(row.get('market')): row
        for row in all_pairs
        if isinstance(row, dict) and row.get('market')
    } if isinstance(all_pairs, list) else {}

    conn = _scanner_db_connect()
    try:
        if isinstance(snapshots, list):
            for row in snapshots:
                if not isinstance(row, dict):
                    continue
                conn.execute(
                    '''INSERT OR REPLACE INTO snapshots
                       (generated_ms,market,action,score,executable_entry,roundtrip_cost_pct,
                        net_reward_risk,price_in_zone,regime,side,decision_action,
                        decision_reason,cost_multiple,execution_spread_pct,reasons_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (
                        generated_ms, str(row.get('market', '')), str(row.get('action', '')),
                        float(row.get('score', 0.0)), float(row.get('executable_entry', 0.0)),
                        float(row.get('roundtrip_cost_pct', 0.0)),
                        float(row.get('net_reward_risk', 0.0)), int(bool(row.get('price_in_zone'))),
                        str(report.get('regime', 'UNKNOWN')), str(row.get('side', 'NONE')),
                        str(row.get('decision_action', 'SKIP')),
                        str(row.get('decision_reason', '')),
                        float(row.get('cost_multiple', 0.0)),
                        float(row.get('execution_spread_pct', 0.0)),
                        json.dumps(row.get('reasons', []), ensure_ascii=False),
                    ),
                )

        open_signals = conn.execute(
            '''SELECT generated_ms,market,side,signal_candle_ms,entry_price,stop_price,
                      target_price,roundtrip_cost_pct FROM signals WHERE status='OPEN' '''
        ).fetchall()
        for signal in open_signals:
            signal_ms, market, side, signal_candle_ms, entry, stop, target, cost = signal
            row = pair_by_market.get(str(market))
            if row is None:
                continue
            outcome = ''
            exit_price = 0.0
            evaluated_candle_ms = 0
            raw_candles = row.get('_outcome_candles', [])
            candles = raw_candles if isinstance(raw_candles, list) else []
            if not candles:
                candles = [[
                    row.get('latest_candle_ms', 0), row.get('latest_candle_high', 0.0),
                    row.get('latest_candle_low', 0.0), row.get('latest_candle_close', 0.0),
                ]]
            for candle in candles:
                if not isinstance(candle, (list, tuple)) or len(candle) < 4:
                    continue
                candle_ms, high, low, close = (
                    int(candle[0]), float(candle[1]), float(candle[2]), float(candle[3])
                )
                if candle_ms <= int(signal_candle_ms):
                    continue
                if str(side) == 'SHORT':
                    stop_hit = high >= float(stop)
                    target_hit = low <= float(target)
                else:
                    stop_hit = low <= float(stop)
                    target_hit = high >= float(target)
                if stop_hit:
                    outcome, exit_price = 'STOP', float(stop)
                elif target_hit:
                    outcome, exit_price = 'TARGET', float(target)
                elif candle_ms - int(signal_candle_ms) >= SIGNAL_MAX_HOLD_MS:
                    outcome, exit_price = 'TIME', close
                if outcome:
                    evaluated_candle_ms = candle_ms
                    break
            if not outcome:
                continue
            net_return = _signal_net_return(str(side), float(entry), exit_price, float(cost))
            conn.execute(
                '''UPDATE signals SET status='CLOSED',evaluated_candle_ms=?,exit_price=?,
                   outcome=?,net_return_pct=? WHERE generated_ms=? AND market=?''',
                (evaluated_candle_ms, exit_price, outcome, net_return, signal_ms, market),
            )

        if isinstance(rare, list):
            for row in rare:
                if not isinstance(row, dict):
                    continue
                conn.execute(
                    '''INSERT OR IGNORE INTO signals
                       (generated_ms,market,side,signal_candle_ms,entry_price,stop_price,
                        target_price,roundtrip_cost_pct,net_reward_risk,status)
                       VALUES (?,?,?,?,?,?,?,?,?,'OPEN')''',
                    (
                        generated_ms, str(row.get('market', '')), str(row.get('side', '')),
                        int(row.get('latest_candle_ms', 0)), float(row.get('executable_entry', 0.0)),
                        float(row.get('stop_hint', 0.0)), float(row.get('target_hint', 0.0)),
                        float(row.get('roundtrip_cost_pct', 0.0)),
                        float(row.get('net_reward_risk', 0.0)),
                    ),
                )

        retention_cutoff = generated_ms - 100 * 24 * 60 * 60 * 1000
        conn.execute('DELETE FROM snapshots WHERE generated_ms<?', (retention_cutoff,))
        conn.commit()
        total, open_count, closed, wins, net_sum, positive_sum, negative_sum = conn.execute(
            '''SELECT COUNT(*),
                      SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='CLOSED' AND net_return_pct>0 THEN 1 ELSE 0 END),
                      COALESCE(SUM(CASE WHEN status='CLOSED' THEN net_return_pct ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN net_return_pct>0 THEN net_return_pct ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN net_return_pct<0 THEN -net_return_pct ELSE 0 END),0)
               FROM signals'''
        ).fetchone()
        audit_cutoff = generated_ms - AUDIT_WINDOW_MS
        (
            audit_cycles,
            audit_candidates,
            audit_watches,
            audit_rare,
            audit_detailed,
            blocked_regime,
            blocked_direction,
            blocked_score,
            blocked_cost_room,
            blocked_spread,
            blocked_zone,
            blocked_net_rr,
        ) = conn.execute(
            '''SELECT
                   COUNT(DISTINCT generated_ms),
                   COUNT(*),
                   SUM(CASE WHEN action LIKE '%WATCH%' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN action LIKE '%TRADE-GRADE%' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN regime IN ('BULL','BEAR','SIDEWAYS') THEN 1 ELSE 0 END),
                   SUM(CASE WHEN regime='SIDEWAYS' THEN 1 ELSE 0 END),
                   SUM(CASE
                         WHEN regime='BULL' AND decision_action!='LONG' THEN 1
                         WHEN regime='BEAR' AND decision_action!='SHORT' THEN 1
                         ELSE 0 END),
                   SUM(CASE WHEN regime IN ('BULL','BEAR') AND score<? THEN 1 ELSE 0 END),
                   SUM(CASE WHEN regime IN ('BULL','BEAR') AND cost_multiple<? THEN 1 ELSE 0 END),
                   SUM(CASE WHEN regime IN ('BULL','BEAR') AND execution_spread_pct>? THEN 1 ELSE 0 END),
                   SUM(CASE WHEN regime IN ('BULL','BEAR') AND price_in_zone!=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN regime IN ('BULL','BEAR') AND net_reward_risk<? THEN 1 ELSE 0 END)
               FROM snapshots WHERE generated_ms>=?''',
            (
                TRADE_GRADE_SCORE,
                TRADE_GRADE_COST_MULTIPLE,
                TRADE_GRADE_MAX_SPREAD_PCT,
                TRADE_GRADE_MIN_NET_RR,
                audit_cutoff,
            ),
        ).fetchone()
        top_decision_reasons = [
            {'reason': str(reason), 'count': int(count)}
            for reason, count in conn.execute(
                '''SELECT decision_reason,COUNT(*)
                   FROM snapshots
                   WHERE generated_ms>=?
                     AND regime IN ('BULL','BEAR','SIDEWAYS')
                     AND decision_reason!=''
                     AND decision_action NOT IN ('LONG','SHORT','BUY')
                   GROUP BY decision_reason
                   ORDER BY COUNT(*) DESC,decision_reason
                   LIMIT 5''',
                (audit_cutoff,),
            ).fetchall()
        ]
        profit_factor = float(positive_sum) / float(negative_sum) if float(negative_sum) > 0 else 0.0
        return {
            'database': str(_db_path()),
            'signals_total': int(total or 0),
            'signals_open': int(open_count or 0),
            'signals_closed': int(closed or 0),
            'wins': int(wins or 0),
            'losses': int((closed or 0) - (wins or 0)),
            'net_return_sum_pct': round(float(net_sum), 3),
            'profit_factor': round(profit_factor, 3),
            'outcome_rule': 'STOP eerst als stop en target in dezelfde 15m-candle liggen',
            'audit_window_hours': 24,
            'audit_cycles': int(audit_cycles or 0),
            'audit_candidates': int(audit_candidates or 0),
            'audit_watch_moments': int(audit_watches or 0),
            'audit_rare_moments': int(audit_rare or 0),
            'audit_detailed_candidates': int(audit_detailed or 0),
            'audit_blockers_overlap': {
                'regime_sideways': int(blocked_regime or 0),
                'strategy_direction': int(blocked_direction or 0),
                'score': int(blocked_score or 0),
                'cost_room': int(blocked_cost_room or 0),
                'spread': int(blocked_spread or 0),
                'price_zone': int(blocked_zone or 0),
                'net_reward_risk': int(blocked_net_rr or 0),
            },
            'audit_top_decision_reasons': top_decision_reasons,
        }
    finally:
        conn.close()


def _strip_internal_research_data(report: dict[str, object]) -> None:
    for value in report.values():
        if not isinstance(value, list):
            continue
        for row in value:
            if isinstance(row, dict):
                row.pop('_outcome_candles', None)


def print_report(report: dict[str, object]) -> None:
    age_seconds = _report_age_seconds(report)
    freshness = 'VEROUDERD' if age_seconds > REPORT_STALE_SECONDS else 'ACTUEEL'
    print('=== CRYPTO SCANNER v3.1 | STRICT L2 + AUDIT | READ ONLY ===')
    print(f"UTC             : {report.get('generated_at_utc', 'n/a')}")
    print(f"RAPPORTSTATUS   : {freshness} | leeftijd {age_seconds/60.0:.1f} min")
    print(f"REGIME          : {report.get('regime', 'UNKNOWN')}")
    print(f"BULL BREADTH    : {float(report.get('bull_breadth_pct', 0.0)):.1f}%")
    print(f"BEAR BREADTH    : {float(report.get('bear_breadth_pct', 0.0)):.1f}%")
    print(f"MARKTEN GELDIG  : {report.get('valid_reference_markets', 0)}/{len(report.get('reference_universe', []))}")
    print(f"USDC BRUIKBAAR  : {report.get('usdc_available_for_reference_assets', 0)}/{len(report.get('reference_universe', []))}")
    print(f"ZELDZAME KANSEN : {report.get('rare_opportunity_count', report.get('trade_grade_count', 0))}")
    rules = report.get('rules', {})
    shadow = rules.get('shadow_notional_eur', 0.0) if isinstance(rules, dict) else 0.0
    print(f"SCHADUWOMVANG   : €{float(shadow):.0f} met volledige orderboekdiepte")
    print('BESLISSING       : ALTIJD ZELF | dit is geen koop- of verkoopadvies')
    print('ORDERS           : ONMOGELIJK | scanner heeft geen private trading-capability')
    print()
    print('=== ZELDZAME KANSEN — ZELF BESLISSEN ===')
    rare = report.get('rare_opportunities', report.get('trade_grade', []))
    if not isinstance(rare, list) or not rare:
        print('geen uitzonderlijke kans; niets doen is nu de standaard')
    else:
        for index, row in enumerate(rare, 1):
            if not isinstance(row, dict):
                continue
            print(
                f"{index}. {str(row.get('market','?')):12s} | {_display_action(row.get('action'))}"
                f" | score {float(row.get('score',0.0)):.1f}/100"
                f" | xkosten {float(row.get('cost_multiple',0.0)):.2f}"
                f" | netto R/R {float(row.get('net_reward_risk',0.0)):.2f}"
            )
            print(
                f"   uitvoerprijs {_fmt_price(row.get('executable_entry'))}"
                f" | L2-spread {float(row.get('execution_spread_pct',0.0)):.3f}%"
                f" | kosten {float(row.get('roundtrip_cost_pct',0.0)):.2f}%"
            )
            print(
                f"   besliszone {_fmt_price(row.get('entry_zone_low'))} - {_fmt_price(row.get('entry_zone_high'))}"
                f" | ongeldig onder/boven {_fmt_price(row.get('stop_hint'))}"
                f" | technisch doel {_fmt_price(row.get('target_hint'))}"
            )
            print('   controleer zelf: actueel nieuws, orderboek, positieomvang en of de koers nog in de besliszone ligt')
    print()
    print('=== TOP 3 OBSERVATIES ===')
    top3 = report.get('top3', [])
    if not isinstance(top3, list) or not top3:
        print('geen kandidaten')
    else:
        for index, row in enumerate(top3, 1):
            if not isinstance(row, dict):
                continue
            print(
                f"{index}. {str(row.get('market','?')):12s} | {_display_action(row.get('action')):20s}"
                f" | score {float(row.get('score',0.0)):5.1f}/100"
                f" | kosten {float(row.get('roundtrip_cost_pct',0.0)):.2f}%"
                f" | beweging {float(row.get('movement_proxy_pct',0.0)):.2f}%"
                f" | netto R/R {float(row.get('net_reward_risk',0.0)):.2f}"
            )
            print(
                f"   opties {', '.join(str(x) for x in row.get('pair_options', []))}"
                f" | uitvoer {_fmt_price(row.get('executable_entry'))}"
                f" | zone {_fmt_price(row.get('entry_zone_low'))} - {_fmt_price(row.get('entry_zone_high'))}"
                f" | stop {_fmt_price(row.get('stop_hint'))} | target {_fmt_price(row.get('target_hint'))}"
            )
            reasons = row.get('reasons', [])
            if isinstance(reasons, list) and reasons:
                print('   reden: ' + '; '.join(str(x) for x in reasons))
    errors = report.get('errors', [])
    if isinstance(errors, list) and errors:
        print()
        print(f'WAARSCHUWINGEN   : {len(errors)}')
        for text in errors[:5]:
            print(f'  - {text}')
    research = report.get('signal_research', {})
    if isinstance(research, dict):
        print()
        print('=== SCANNERHISTORIE LAATSTE 24 UUR ===')
        print(
            f"cycli {int(research.get('audit_cycles',0))}"
            f" | kandidaten {int(research.get('audit_candidates',0))}"
            f" | WATCH {int(research.get('audit_watch_moments',0))}"
            f" | zeldzame kansen {int(research.get('audit_rare_moments',0))}"
        )
        detailed = int(research.get('audit_detailed_candidates', 0))
        blockers = research.get('audit_blockers_overlap', {})
        if detailed > 0 and isinstance(blockers, dict):
            print(f'gedetailleerde blokkades vanaf v3.1: n={detailed}; aantallen kunnen overlappen')
            print(
                f"  regime {int(blockers.get('regime_sideways',0))}"
                f" | richting {int(blockers.get('strategy_direction',0))}"
                f" | score {int(blockers.get('score',0))}"
                f" | xkosten {int(blockers.get('cost_room',0))}"
            )
            print(
                f"  spread {int(blockers.get('spread',0))}"
                f" | prijszone {int(blockers.get('price_zone',0))}"
                f" | netto R/R {int(blockers.get('net_reward_risk',0))}"
            )
        top_reasons = research.get('audit_top_decision_reasons', [])
        if isinstance(top_reasons, list) and top_reasons:
            print('meest voorkomende strategieredenen:')
            for item in top_reasons:
                if isinstance(item, dict):
                    print(f"  {item.get('reason','onbekend')}: {int(item.get('count',0))}")
        print()
        print('=== PROSPECTIEVE ZELDZAME-SIGNAALCONTROLE ===')
        print(
            f"zeldzame signalen {int(research.get('signals_total',0))}"
            f" | open {int(research.get('signals_open',0))}"
            f" | gesloten {int(research.get('signals_closed',0))}"
            f" | W/L {int(research.get('wins',0))}/{int(research.get('losses',0))}"
            f" | PF {float(research.get('profit_factor',0.0)):.3f}"
        )
    print()
    print('LET OP: alleen actuele €200-L2-uitvoer, prijs in zone en netto R/R ≥ 1,50 kunnen een kanslabel geven.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Crypto Scanner v2 - EUR/USDC cost aware, read only')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()

    settings = Settings()
    settings.validate()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    if args.status:
        report = _load_report()
        if report is None:
            print('=== CRYPTO SCANNER v3 | STRICT L2 | READ ONLY ===')
            print('STATUS          : nog geen rapport beschikbaar')
            print(f'RAPPORT         : {_report_path()}')
            return 1
        print_report(report)
        return 2 if _report_is_stale(report) else 0

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    poll_seconds = max(60, int(os.getenv('SCANNER_V2_POLL_SECONDS', '900')))

    while not STOP:
        try:
            report = scan_once(settings)
            report['signal_research'] = _persist_signal_research(report)
            _strip_internal_research_data(report)
            _write_report(report)
            print_report(report)
        except Exception as exc:
            logger.exception('scanner-v2-cyclus mislukt: %s', exc)
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
