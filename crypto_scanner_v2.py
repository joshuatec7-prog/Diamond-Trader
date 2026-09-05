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
from human_decision import (
    HUMAN_ENTRY_SCORE,
    HUMAN_SHORTLIST_SIZE,
    HUMAN_TRIGGER_INTERVAL,
    HUMAN_TRIGGER_POLL_SECONDS,
    evaluate_human_entry,
    five_minute_features,
)

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
PRACTICAL_MAX_HOLD_MS = 48 * 60 * 60 * 1000
PRACTICAL_STOP_NET_PCT = -3.00
PRACTICAL_TRAIL_ACTIVATE_NET_PCT = 1.00
PRACTICAL_TRAIL_GIVEBACK_PCT = 1.00
PRACTICAL_MIN_LOCK_NET_PCT = 0.25
PRACTICAL_MONITOR_SECONDS = 30
PRACTICAL_REENTRY_COOLDOWN_MS = 4 * 60 * 60 * 1000


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

    market_notional_quote = settings.position_eur
    if quote == 'USDC' and conversion_book is not None:
        conversion_buy = float(conversion_book.get('buy_vwap', 0.0))
        if conversion_buy <= 0.0:
            raise RuntimeError('ongeldige USDC-EUR uitvoerprijs')
        market_notional_quote = settings.position_eur / conversion_buy
    depth = api.depth_book(market, market_notional_quote)
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
        'shadow_notional_quote': round(market_notional_quote, 8),
        'base_quantity': round(market_notional_quote / float(depth['buy_vwap']), 12),
        'best_bid': round(float(depth['bid']), 8),
        'best_ask': round(float(depth['ask']), 8),
        'sell_vwap': round(float(depth['sell_vwap']), 8),
        'buy_vwap': round(float(depth['buy_vwap']), 8),
        'executable_entry': round(executable_entry, 8),
        'bid_depth_quote': round(float(depth['bid_depth_quote']), 2),
        'ask_depth_quote': round(float(depth['ask_depth_quote']), 2),
        'near_bid_depth_quote': round(float(depth.get('near_bid_depth_quote', 0.0)), 2),
        'near_ask_depth_quote': round(float(depth.get('near_ask_depth_quote', 0.0)), 2),
        'near_book_imbalance': round(float(depth.get('near_book_imbalance', 0.0)), 4),
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
        'version': '3.5',
        'mode': 'BASELINE_PLUS_HUMAN_CONTEXT_PAPER_SCANNER',
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
            'paper_start_eur': settings.paper_start_eur,
            'max_open_positions': settings.max_open_positions,
            'eval_min_trades': settings.eval_min_trades,
            'eval_min_span_days': settings.eval_min_span_days,
            'eval_min_profit_factor': settings.eval_min_profit_factor,
            'eval_max_drawdown_pct': settings.eval_max_drawdown_pct,
            'human_trigger_interval': HUMAN_TRIGGER_INTERVAL,
            'human_trigger_poll_seconds': HUMAN_TRIGGER_POLL_SECONDS,
            'human_shortlist_size': HUMAN_SHORTLIST_SIZE,
            'human_entry_score_min': HUMAN_ENTRY_SCORE,
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
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS practical_signals (
            generated_ms INTEGER NOT NULL,
            market TEXT NOT NULL,
            base_asset TEXT NOT NULL,
            signal_action TEXT NOT NULL,
            entry_price REAL NOT NULL,
            non_book_cost_pct REAL NOT NULL,
            notional_eur REAL NOT NULL DEFAULT 200,
            base_quantity REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            max_net_return_pct REAL NOT NULL DEFAULT 0,
            trailing_floor_pct REAL,
            last_mark_ms INTEGER,
            last_sell_price REAL,
            last_net_return_pct REAL,
            evaluated_ms INTEGER,
            exit_price REAL,
            outcome TEXT,
            net_return_pct REAL,
            PRIMARY KEY (generated_ms, market)
        )'''
    )
    existing_practical_columns = {
        str(row[1]) for row in conn.execute('PRAGMA table_info(practical_signals)').fetchall()
    }
    practical_migrations = {
        'notional_eur': 'REAL NOT NULL DEFAULT 200',
        'base_quantity': 'REAL NOT NULL DEFAULT 0',
        'last_mark_ms': 'INTEGER',
        'last_sell_price': 'REAL',
        'last_net_return_pct': 'REAL',
    }
    for column, declaration in practical_migrations.items():
        if column not in existing_practical_columns:
            conn.execute(f'ALTER TABLE practical_signals ADD COLUMN {column} {declaration}')
    conn.execute(
        '''UPDATE practical_signals
           SET notional_eur=200
           WHERE notional_eur IS NULL OR notional_eur<=0'''
    )
    conn.execute(
        '''UPDATE practical_signals
           SET base_quantity=notional_eur/entry_price
           WHERE (base_quantity IS NULL OR base_quantity<=0) AND entry_price>0'''
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_scanner_one_open_practical_base "
        "ON practical_signals(base_asset) WHERE status='OPEN'"
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS human_decisions (
            evaluated_ms INTEGER NOT NULL,
            context_generated_ms INTEGER NOT NULL,
            market TEXT NOT NULL,
            action TEXT NOT NULL,
            score REAL NOT NULL,
            trigger_name TEXT NOT NULL,
            blockers_json TEXT NOT NULL,
            details_json TEXT NOT NULL,
            PRIMARY KEY (evaluated_ms, market)
        )'''
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS human_signals (
            generated_ms INTEGER NOT NULL,
            market TEXT NOT NULL,
            base_asset TEXT NOT NULL,
            signal_action TEXT NOT NULL,
            decision_score REAL NOT NULL,
            decision_details_json TEXT NOT NULL,
            entry_price REAL NOT NULL,
            non_book_cost_pct REAL NOT NULL,
            notional_eur REAL NOT NULL DEFAULT 200,
            base_quantity REAL NOT NULL,
            status TEXT NOT NULL,
            max_net_return_pct REAL NOT NULL DEFAULT 0,
            trailing_floor_pct REAL,
            last_mark_ms INTEGER,
            last_sell_price REAL,
            last_net_return_pct REAL,
            evaluated_ms INTEGER,
            exit_price REAL,
            outcome TEXT,
            net_return_pct REAL,
            paper_slot INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (generated_ms, market)
        )'''
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_scanner_one_open_human_slot "
        "ON human_signals(paper_slot) WHERE status='OPEN'"
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


def _practical_net_return(entry: float, executable_sell: float, non_book_cost_pct: float) -> float:
    if entry <= 0.0 or executable_sell <= 0.0:
        return -999.0
    return (executable_sell / entry - 1.0) * 100.0 - max(0.0, non_book_cost_pct)


def _human_shortlist(report: dict[str, object]) -> list[dict[str, Any]]:
    candidates = report.get('all_pair_snapshots', report.get('candidates', []))
    if not isinstance(candidates, list):
        return []
    eligible: list[dict[str, Any]] = []
    for value in candidates:
        if (
            not isinstance(value, dict)
            or str(value.get('side', '')) != 'LONG'
            or str(value.get('quote', _quote_asset(str(value.get('market', ''))))) != 'EUR'
        ):
            continue
        action = str(value.get('action', ''))
        decision_action = str(value.get('decision_action', ''))
        if (
            action not in {'LONG WATCH', 'LONG TRADE-GRADE', 'SIDEWAYS WATCH'}
            and decision_action not in {'LONG', 'BUY'}
        ):
            continue
        if float(value.get('score', 0.0)) < 55.0:
            continue
        if float(value.get('cost_multiple', 0.0)) < 2.0:
            continue
        eligible.append(value)
    eligible.sort(key=_rank_key, reverse=True)
    unique: list[dict[str, Any]] = []
    seen_bases: set[str] = set()
    for row in eligible:
        base = str(row.get('base') or _base_asset(str(row.get('market', ''))))
        if base in seen_bases:
            continue
        seen_bases.add(base)
        unique.append(row)
        if len(unique) >= HUMAN_SHORTLIST_SIZE:
            break
    return unique


def _human_stats(settings: Settings, generated_ms: int) -> dict[str, object]:
    paper_start_eur = float(getattr(settings, 'paper_start_eur', 5000.0))
    conn = _scanner_db_connect()
    try:
        (
            total,
            open_count,
            closed,
            wins,
            realized_pnl_eur,
            positive_eur,
            negative_eur,
            stops,
            trails,
            times,
            first_ms,
        ) = conn.execute(
            '''SELECT COUNT(*),
                      SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='CLOSED' AND net_return_pct>0 THEN 1 ELSE 0 END),
                      COALESCE(SUM(CASE WHEN status='CLOSED'
                           THEN net_return_pct*notional_eur/100.0 ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN status='CLOSED' AND net_return_pct>0
                           THEN net_return_pct*notional_eur/100.0 ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN status='CLOSED' AND net_return_pct<0
                           THEN -net_return_pct*notional_eur/100.0 ELSE 0 END),0),
                      SUM(CASE WHEN outcome='STOP' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN outcome='TRAIL' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN outcome='TIME' THEN 1 ELSE 0 END),
                      MIN(generated_ms)
               FROM human_signals'''
        ).fetchone()
        open_pnl_eur = 0.0
        open_notional_eur = 0.0
        unpriced_open = 0
        open_positions: list[dict[str, object]] = []
        for row in conn.execute(
            '''SELECT generated_ms,market,decision_score,notional_eur,base_quantity,
                      max_net_return_pct,trailing_floor_pct,last_mark_ms,last_sell_price,
                      last_net_return_pct
               FROM human_signals WHERE status='OPEN' ORDER BY market'''
        ).fetchall():
            (
                opened_ms,
                market,
                decision_score,
                notional_eur,
                base_quantity,
                max_net,
                trailing_floor,
                last_mark_ms,
                last_sell_price,
                last_net,
            ) = row
            notional = float(notional_eur)
            open_notional_eur += notional
            current_net = None if last_net is None else float(last_net)
            if current_net is None:
                unpriced_open += 1
            else:
                open_pnl_eur += current_net * notional / 100.0
            open_positions.append({
                'market': str(market),
                'decision_score': round(float(decision_score), 1),
                'age_hours': round(max(0.0, (generated_ms - int(opened_ms)) / 3_600_000.0), 2),
                'notional_eur': round(notional, 2),
                'base_quantity': round(float(base_quantity), 12),
                'current_sell_price': (
                    None if last_sell_price is None else round(float(last_sell_price), 8)
                ),
                'current_net_pct': None if current_net is None else round(current_net, 3),
                'current_pnl_eur': (
                    None if current_net is None
                    else round(current_net * notional / 100.0, 2)
                ),
                'max_net_pct': round(float(max_net), 3),
                'trailing_floor_pct': (
                    None if trailing_floor is None else round(float(trailing_floor), 3)
                ),
                'last_mark_age_seconds': (
                    None if last_mark_ms is None
                    else round(max(0.0, (generated_ms - int(last_mark_ms)) / 1000.0), 1)
                ),
            })

        decision_cutoff = generated_ms - AUDIT_WINDOW_MS
        decision_total, entry_decisions = conn.execute(
            '''SELECT COUNT(*),SUM(CASE WHEN action='PAPER ENTRY' THEN 1 ELSE 0 END)
               FROM human_decisions WHERE evaluated_ms>=?''',
            (decision_cutoff,),
        ).fetchone()
        blocker_counts: dict[str, int] = {}
        for (raw_blockers,) in conn.execute(
            'SELECT blockers_json FROM human_decisions WHERE evaluated_ms>=?',
            (decision_cutoff,),
        ).fetchall():
            try:
                blockers = json.loads(str(raw_blockers))
            except (TypeError, ValueError):
                blockers = []
            if isinstance(blockers, list):
                for blocker in set(str(value) for value in blockers):
                    blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
        top_blockers = [
            {'reason': reason, 'count': count}
            for reason, count in sorted(
                blocker_counts.items(), key=lambda item: (-item[1], item[0])
            )[:5]
        ]
        latest_decisions: list[dict[str, object]] = []
        for evaluated_ms, market, action, score, trigger, blockers_json in conn.execute(
            '''SELECT evaluated_ms,market,action,score,trigger_name,blockers_json
               FROM human_decisions ORDER BY evaluated_ms DESC,score DESC LIMIT 5'''
        ).fetchall():
            try:
                blockers = json.loads(str(blockers_json))
            except (TypeError, ValueError):
                blockers = []
            latest_decisions.append({
                'evaluated_ms': int(evaluated_ms),
                'market': str(market),
                'action': str(action),
                'score': round(float(score), 1),
                'trigger': str(trigger),
                'blockers': blockers if isinstance(blockers, list) else [],
            })

        profit_factor = (
            float(positive_eur) / float(negative_eur)
            if float(negative_eur) > 0.0
            else (999.0 if float(positive_eur) > 0.0 else 0.0)
        )
        balance = paper_start_eur
        peak = balance
        max_drawdown_pct = 0.0
        for net_pct, notional in conn.execute(
            '''SELECT net_return_pct,notional_eur FROM human_signals
               WHERE status='CLOSED' ORDER BY evaluated_ms,generated_ms,market'''
        ).fetchall():
            balance += float(net_pct) * float(notional) / 100.0
            peak = max(peak, balance)
            if peak > 0.0:
                max_drawdown_pct = max(max_drawdown_pct, (peak - balance) / peak * 100.0)
        test_span_days = (
            0.0 if first_ms is None
            else max(0.0, (generated_ms - int(first_ms)) / 86_400_000.0)
        )
        eval_min_trades = int(getattr(settings, 'eval_min_trades', 40))
        eval_min_span_days = float(getattr(settings, 'eval_min_span_days', 14.0))
        enough_history = int(closed or 0) >= eval_min_trades and test_span_days >= eval_min_span_days
        if not enough_history:
            evaluation = 'VERZAMELEN'
        elif (
            profit_factor >= float(getattr(settings, 'eval_min_profit_factor', 1.25))
            and max_drawdown_pct <= float(getattr(settings, 'eval_max_drawdown_pct', 10.0))
            and float(realized_pnl_eur) > 0.0
        ):
            evaluation = 'PAPER KANDIDAAT'
        else:
            evaluation = 'ONVOLDOENDE'

        return {
            'human_decisions_24h': int(decision_total or 0),
            'human_entry_decisions_24h': int(entry_decisions or 0),
            'human_top_blockers_24h': top_blockers,
            'human_latest_decisions': latest_decisions,
            'human_total': int(total or 0),
            'human_open': int(open_count or 0),
            'human_closed': int(closed or 0),
            'human_wins': int(wins or 0),
            'human_losses': int((closed or 0) - (wins or 0)),
            'human_pnl_eur': round(float(realized_pnl_eur), 2),
            'human_open_pnl_eur': round(open_pnl_eur, 2),
            'human_paper_cash_eur': round(
                paper_start_eur + float(realized_pnl_eur) - open_notional_eur, 2
            ),
            'human_paper_equity_eur': round(
                paper_start_eur + float(realized_pnl_eur) + open_pnl_eur, 2
            ),
            'human_profit_factor': round(profit_factor, 3),
            'human_max_drawdown_pct': round(max_drawdown_pct, 3),
            'human_test_span_days': round(test_span_days, 2),
            'human_evaluation': evaluation,
            'human_open_positions': open_positions,
            'human_unpriced_open': unpriced_open,
            'human_outcomes': {
                'stop': int(stops or 0),
                'trail': int(trails or 0),
                'time': int(times or 0),
            },
            'human_rules': {
                'entry_score_min': HUMAN_ENTRY_SCORE,
                'shortlist_size': HUMAN_SHORTLIST_SIZE,
                'trigger_interval': HUMAN_TRIGGER_INTERVAL,
                'trigger_poll_seconds': HUMAN_TRIGGER_POLL_SECONDS,
                'position_monitor_seconds': PRACTICAL_MONITOR_SECONDS,
                'stop_net_pct': PRACTICAL_STOP_NET_PCT,
                'trail_activate_net_pct': PRACTICAL_TRAIL_ACTIVATE_NET_PCT,
                'trail_giveback_pct': PRACTICAL_TRAIL_GIVEBACK_PCT,
                'minimum_locked_net_pct': PRACTICAL_MIN_LOCK_NET_PCT,
                'news_check': 'NIET GEAUTOMATISEERD',
            },
        }
    finally:
        conn.close()


def _monitor_human_positions(
    settings: Settings,
    *,
    api: BitvavoPublic | None = None,
    generated_ms: int | None = None,
    monitor_interval_seconds: int = PRACTICAL_MONITOR_SECONDS,
) -> dict[str, object]:
    now_ms = int(time.time() * 1000) if generated_ms is None else int(generated_ms)
    conn = _scanner_db_connect()
    try:
        positions = [
            (int(row[0]), str(row[1]), float(row[2]))
            for row in conn.execute(
                '''SELECT generated_ms,market,base_quantity FROM human_signals
                   WHERE status='OPEN' ORDER BY market'''
            ).fetchall()
        ]
    finally:
        conn.close()

    market_api = api or BitvavoPublic(
        settings.api_base_url,
        settings.request_timeout_seconds,
        settings.request_retries,
    )
    marks: dict[tuple[int, str], float] = {}
    errors: list[str] = []
    for opened_ms, market, base_quantity in positions:
        try:
            depth = market_api.sell_vwap_for_base(market, base_quantity)
            marks[(opened_ms, market)] = float(depth['sell_vwap'])
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    conn = _scanner_db_connect()
    try:
        for row in conn.execute(
            '''SELECT generated_ms,market,entry_price,non_book_cost_pct,
                      max_net_return_pct,trailing_floor_pct
               FROM human_signals WHERE status='OPEN' '''
        ).fetchall():
            opened_ms, market, entry, non_book_cost, stored_max, stored_floor = row
            executable_sell = marks.get((int(opened_ms), str(market)))
            if executable_sell is None:
                continue
            net_return = _practical_net_return(
                float(entry), executable_sell, float(non_book_cost)
            )
            max_net = max(float(stored_max), net_return)
            trailing_floor = None if stored_floor is None else float(stored_floor)
            if max_net >= PRACTICAL_TRAIL_ACTIVATE_NET_PCT:
                new_floor = max(
                    PRACTICAL_MIN_LOCK_NET_PCT,
                    max_net - PRACTICAL_TRAIL_GIVEBACK_PCT,
                )
                trailing_floor = (
                    new_floor if trailing_floor is None else max(trailing_floor, new_floor)
                )
            outcome = ''
            if net_return <= PRACTICAL_STOP_NET_PCT:
                outcome = 'STOP'
            elif trailing_floor is not None and net_return <= trailing_floor:
                outcome = 'TRAIL'
            elif trailing_floor is None and now_ms - int(opened_ms) >= PRACTICAL_MAX_HOLD_MS:
                outcome = 'TIME'
            if outcome:
                conn.execute(
                    '''UPDATE human_signals
                       SET status='CLOSED',max_net_return_pct=?,trailing_floor_pct=?,
                           last_mark_ms=?,last_sell_price=?,last_net_return_pct=?,
                           evaluated_ms=?,exit_price=?,outcome=?,net_return_pct=?
                       WHERE generated_ms=? AND market=?''',
                    (
                        max_net, trailing_floor, now_ms, executable_sell, net_return,
                        now_ms, executable_sell, outcome, net_return, opened_ms, market,
                    ),
                )
            else:
                conn.execute(
                    '''UPDATE human_signals
                       SET max_net_return_pct=?,trailing_floor_pct=?,last_mark_ms=?,
                           last_sell_price=?,last_net_return_pct=?
                       WHERE generated_ms=? AND market=?''',
                    (
                        max_net, trailing_floor, now_ms, executable_sell, net_return,
                        opened_ms, market,
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    stats = _human_stats(settings, now_ms)
    stats.update({
        'human_monitor_interval_seconds': monitor_interval_seconds,
        'human_monitor_attempted_ms': now_ms,
        'human_monitor_generated_ms': now_ms if marks or not positions else 0,
        'human_monitor_position_count': len(positions),
        'human_monitor_success_count': len(marks),
        'human_monitor_errors': errors,
    })
    return stats


def _monitor_human_entries(
    settings: Settings,
    report: dict[str, object],
    *,
    api: BitvavoPublic | None = None,
    generated_ms: int | None = None,
) -> dict[str, object]:
    now_ms = int(time.time() * 1000) if generated_ms is None else int(generated_ms)
    stats = _human_stats(settings, now_ms)
    stats.update({
        'human_entry_attempted_ms': now_ms,
        'human_entry_generated_ms': 0,
        'human_shortlist_count': 0,
        'human_entry_errors': [],
    })
    if _report_is_stale(report, now_ms):
        stats['human_entry_errors'] = ['15m-context is verouderd; geen PAPER-instap']
        return stats
    if int(stats.get('human_open', 0)) > 0:
        stats['human_entry_generated_ms'] = now_ms
        stats['human_entry_status'] = 'POSITIE OPEN; nieuwe instap stand-by'
        return stats

    shortlist = _human_shortlist(report)
    stats['human_shortlist_count'] = len(shortlist)
    if not shortlist:
        stats['human_entry_generated_ms'] = now_ms
        stats['human_entry_status'] = 'GEEN 15M-SHORTLIST'
        return stats

    market_api = api or BitvavoPublic(
        settings.api_base_url,
        settings.request_timeout_seconds,
        settings.request_retries,
    )
    errors: list[str] = []
    try:
        bitcoin_candles = market_api.closed_candles(
            'BTC-EUR', HUMAN_TRIGGER_INTERVAL, 40, now_ms=now_ms
        )
        bitcoin_features = five_minute_features(bitcoin_candles)
    except Exception as exc:
        bitcoin_features = {'valid': False, 'reason': f'{type(exc).__name__}: {exc}'}
        errors.append(f'BTC-EUR 5m: {type(exc).__name__}: {exc}')

    decisions: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for context in shortlist:
        market = str(context.get('market', ''))
        try:
            quote_notional = float(
                context.get('shadow_notional_quote', getattr(settings, 'position_eur', 200.0))
            )
            candles = market_api.closed_candles(
                market, HUMAN_TRIGGER_INTERVAL, 40, now_ms=now_ms
            )
            depth = market_api.depth_book(market, quote_notional)
            adjusted_context = {
                **context,
                'execution_spread_pct': float(depth['execution_spread_pct']),
                'roundtrip_cost_pct': max(
                    0.0,
                    float(context.get('roundtrip_cost_pct', 0.0))
                    - float(context.get('execution_spread_pct', 0.0)),
                ) + float(depth['execution_spread_pct']),
            }
            features = five_minute_features(candles, live_price=float(depth['buy_vwap']))
            decision = evaluate_human_entry(
                context=adjusted_context,
                regime=str(report.get('regime', 'UNKNOWN')),
                five_minute=features,
                bitcoin_five_minute=bitcoin_features,
                depth=depth,
            )
            decisions.append((decision, adjusted_context))
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    context_generated_ms = int(report.get('generated_at_ms', 0))
    conn = _scanner_db_connect()
    try:
        for decision, _ in decisions:
            conn.execute(
                '''INSERT OR REPLACE INTO human_decisions
                   (evaluated_ms,context_generated_ms,market,action,score,trigger_name,
                    blockers_json,details_json) VALUES (?,?,?,?,?,?,?,?)''',
                (
                    now_ms,
                    context_generated_ms,
                    str(decision.get('market', '')),
                    str(decision.get('action', 'WACHTEN')),
                    float(decision.get('score', 0.0)),
                    str(decision.get('trigger', 'geen')),
                    json.dumps(decision.get('blockers', []), ensure_ascii=False),
                    json.dumps(decision, ensure_ascii=False),
                ),
            )
        best = max(
            (
                item for item in decisions if bool(item[0].get('eligible'))
            ),
            key=lambda item: float(item[0].get('score', 0.0)),
            default=None,
        )
        opened = False
        if best is not None:
            decision, context = best
            market = str(decision.get('market', ''))
            base_asset = str(context.get('base') or _base_asset(market))
            last_exit = conn.execute(
                '''SELECT MAX(evaluated_ms) FROM human_signals
                   WHERE base_asset=? AND status='CLOSED' ''',
                (base_asset,),
            ).fetchone()[0]
            cooldown_ok = (
                last_exit is None
                or now_ms - int(last_exit) >= PRACTICAL_REENTRY_COOLDOWN_MS
                or now_ms - int(last_exit) < 0
            )
            if cooldown_ok:
                entry_price = float(decision.get('buy_vwap', 0.0))
                sell_price = float(decision.get('sell_vwap', 0.0))
                quote_notional = float(
                    context.get('shadow_notional_quote', getattr(settings, 'position_eur', 200.0))
                )
                non_book_cost = max(
                    0.0,
                    float(context.get('roundtrip_cost_pct', 0.0))
                    - float(context.get('execution_spread_pct', 0.0)),
                )
                base_quantity = quote_notional / entry_price if entry_price > 0.0 else 0.0
                initial_net = (
                    _practical_net_return(entry_price, sell_price, non_book_cost)
                    if sell_price > 0.0 else None
                )
                if entry_price > 0.0 and base_quantity > 0.0:
                    cursor = conn.execute(
                        '''INSERT OR IGNORE INTO human_signals
                           (generated_ms,market,base_asset,signal_action,decision_score,
                            decision_details_json,entry_price,non_book_cost_pct,notional_eur,
                            base_quantity,status,max_net_return_pct,last_mark_ms,last_sell_price,
                            last_net_return_pct,paper_slot)
                           VALUES (?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,1)''',
                        (
                            now_ms,
                            market,
                            base_asset,
                            str(decision.get('trigger', '5m')),
                            float(decision.get('score', 0.0)),
                            json.dumps(decision, ensure_ascii=False),
                            entry_price,
                            non_book_cost,
                            float(getattr(settings, 'position_eur', 200.0)),
                            base_quantity,
                            max(0.0, initial_net or 0.0),
                            now_ms if initial_net is not None else None,
                            sell_price if initial_net is not None else None,
                            initial_net,
                        ),
                    )
                    opened = cursor.rowcount > 0
        retention_cutoff = now_ms - 30 * 24 * 60 * 60 * 1000
        conn.execute('DELETE FROM human_decisions WHERE evaluated_ms<?', (retention_cutoff,))
        conn.commit()
    finally:
        conn.close()

    stats = _human_stats(settings, now_ms)
    stats.update({
        'human_entry_attempted_ms': now_ms,
        'human_entry_generated_ms': now_ms if decisions or not errors else 0,
        'human_shortlist_count': len(shortlist),
        'human_entry_evaluated_count': len(decisions),
        'human_entry_opened': opened,
        'human_entry_errors': errors,
        'human_entry_status': 'PAPER-POSITIE GEOPEND' if opened else 'WACHTEN OP BEVESTIGING',
    })
    return stats


def _persist_signal_research(report: dict[str, object]) -> dict[str, object]:
    generated_ms = int(report.get('generated_at_ms', 0))
    rules = report.get('rules', {})
    shadow_notional_eur = (
        float(rules.get('shadow_notional_eur', 200.0)) if isinstance(rules, dict) else 200.0
    )
    paper_start_eur = (
        float(rules.get('paper_start_eur', 5000.0)) if isinstance(rules, dict) else 5000.0
    )
    max_open_positions = (
        max(1, int(rules.get('max_open_positions', 1))) if isinstance(rules, dict) else 1
    )
    eval_min_trades = (
        max(1, int(rules.get('eval_min_trades', 40))) if isinstance(rules, dict) else 40
    )
    eval_min_span_days = (
        max(0.0, float(rules.get('eval_min_span_days', 14.0)))
        if isinstance(rules, dict) else 14.0
    )
    eval_min_profit_factor = (
        max(0.0, float(rules.get('eval_min_profit_factor', 1.25)))
        if isinstance(rules, dict) else 1.25
    )
    eval_max_drawdown_pct = (
        max(0.0, float(rules.get('eval_max_drawdown_pct', 10.0)))
        if isinstance(rules, dict) else 10.0
    )
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

        closed_practical_bases: set[str] = set()
        open_practical = conn.execute(
            '''SELECT generated_ms,market,base_asset,entry_price,non_book_cost_pct,
                      max_net_return_pct,trailing_floor_pct
               FROM practical_signals WHERE status='OPEN' '''
        ).fetchall()
        for practical in open_practical:
            (
                practical_ms,
                market,
                base_asset,
                entry_price,
                non_book_cost_pct,
                stored_max_net,
                stored_trailing_floor,
            ) = practical
            row = pair_by_market.get(str(market))
            if row is None:
                continue
            executable_sell = float(row.get('sell_vwap', 0.0))
            net_return = _practical_net_return(
                float(entry_price), executable_sell, float(non_book_cost_pct)
            )
            max_net = max(float(stored_max_net), net_return)
            trailing_floor = (
                float(stored_trailing_floor) if stored_trailing_floor is not None else None
            )
            if max_net >= PRACTICAL_TRAIL_ACTIVATE_NET_PCT:
                new_floor = max(
                    PRACTICAL_MIN_LOCK_NET_PCT,
                    max_net - PRACTICAL_TRAIL_GIVEBACK_PCT,
                )
                trailing_floor = max(trailing_floor, new_floor) if trailing_floor is not None else new_floor

            outcome = ''
            if net_return <= PRACTICAL_STOP_NET_PCT:
                outcome = 'STOP'
            elif trailing_floor is not None and net_return <= trailing_floor:
                outcome = 'TRAIL'
            elif (
                trailing_floor is None
                and generated_ms - int(practical_ms) >= PRACTICAL_MAX_HOLD_MS
            ):
                outcome = 'TIME'

            if outcome:
                conn.execute(
                    '''UPDATE practical_signals
                       SET status='CLOSED',max_net_return_pct=?,trailing_floor_pct=?,
                           last_mark_ms=?,last_sell_price=?,last_net_return_pct=?,
                           evaluated_ms=?,exit_price=?,outcome=?,net_return_pct=?
                       WHERE generated_ms=? AND market=?''',
                    (
                        max_net,
                        trailing_floor,
                        generated_ms,
                        executable_sell,
                        net_return,
                        generated_ms,
                        executable_sell,
                        outcome,
                        net_return,
                        practical_ms,
                        market,
                    ),
                )
                closed_practical_bases.add(str(base_asset))
            else:
                conn.execute(
                    '''UPDATE practical_signals
                       SET max_net_return_pct=?,trailing_floor_pct=?,last_mark_ms=?,
                           last_sell_price=?,last_net_return_pct=?
                       WHERE generated_ms=? AND market=?''',
                    (
                        max_net,
                        trailing_floor,
                        generated_ms,
                        executable_sell,
                        net_return,
                        practical_ms,
                        market,
                    ),
                )

        if isinstance(snapshots, list):
            open_position_count, open_notional_eur = conn.execute(
                '''SELECT COUNT(*),COALESCE(SUM(notional_eur),0)
                   FROM practical_signals WHERE status='OPEN' '''
            ).fetchone()
            for row in snapshots:
                if not isinstance(row, dict):
                    continue
                market = str(row.get('market', ''))
                base_asset = str(row.get('base') or _base_asset(market)) if market else ''
                action = str(row.get('action', ''))
                if (
                    not market
                    or not base_asset
                    or base_asset in closed_practical_bases
                    or action not in {'LONG WATCH', 'LONG TRADE-GRADE', 'SIDEWAYS WATCH'}
                    or str(row.get('side', 'LONG')) != 'LONG'
                    or int(open_position_count or 0) >= max_open_positions
                    or float(open_notional_eur or 0.0) + shadow_notional_eur > paper_start_eur
                ):
                    continue
                last_exit = conn.execute(
                    '''SELECT MAX(evaluated_ms) FROM practical_signals
                       WHERE base_asset=? AND status='CLOSED' ''',
                    (base_asset,),
                ).fetchone()[0]
                if (
                    last_exit is not None
                    and 0 <= generated_ms - int(last_exit) < PRACTICAL_REENTRY_COOLDOWN_MS
                ):
                    continue
                entry_price = float(row.get('buy_vwap', row.get('executable_entry', 0.0)))
                quote_notional = float(row.get('shadow_notional_quote', shadow_notional_eur))
                base_quantity = float(
                    row.get('base_quantity', quote_notional / entry_price if entry_price > 0 else 0.0)
                )
                non_book_cost = max(
                    0.0,
                    float(row.get('roundtrip_cost_pct', 0.0))
                    - float(row.get('execution_spread_pct', 0.0)),
                )
                initial_sell = float(row.get('sell_vwap', 0.0))
                if entry_price <= 0.0 or base_quantity <= 0.0:
                    continue
                initial_net = (
                    _practical_net_return(entry_price, initial_sell, non_book_cost)
                    if initial_sell > 0.0 else None
                )
                cursor = conn.execute(
                    '''INSERT OR IGNORE INTO practical_signals
                       (generated_ms,market,base_asset,signal_action,entry_price,non_book_cost_pct,
                        notional_eur,base_quantity,status,max_net_return_pct,last_mark_ms,
                        last_sell_price,last_net_return_pct)
                       VALUES (?,?,?,?,?,?,?,?,'OPEN',?,?,?,?)''',
                    (
                        generated_ms,
                        market,
                        base_asset,
                        action,
                        entry_price,
                        non_book_cost,
                        shadow_notional_eur,
                        base_quantity,
                        max(0.0, initial_net or 0.0),
                        generated_ms if initial_net is not None else None,
                        initial_sell if initial_net is not None else None,
                        initial_net,
                    ),
                )
                if cursor.rowcount > 0:
                    open_position_count = int(open_position_count or 0) + 1
                    open_notional_eur = float(open_notional_eur or 0.0) + shadow_notional_eur

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
        (
            practical_total,
            practical_open_count,
            practical_closed,
            practical_wins,
            practical_net_sum,
            practical_realized_pnl_eur,
            practical_positive_eur,
            practical_negative_eur,
            practical_stops,
            practical_trails,
            practical_times,
            practical_first_ms,
        ) = conn.execute(
            '''SELECT COUNT(*),
                      SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN status='CLOSED' AND net_return_pct>0 THEN 1 ELSE 0 END),
                      COALESCE(SUM(CASE WHEN status='CLOSED' THEN net_return_pct ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN status='CLOSED'
                           THEN net_return_pct*notional_eur/100.0 ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN status='CLOSED' AND net_return_pct>0
                           THEN net_return_pct*notional_eur/100.0 ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN status='CLOSED' AND net_return_pct<0
                           THEN -net_return_pct*notional_eur/100.0 ELSE 0 END),0),
                      SUM(CASE WHEN outcome='STOP' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN outcome='TRAIL' THEN 1 ELSE 0 END),
                      SUM(CASE WHEN outcome='TIME' THEN 1 ELSE 0 END),
                      MIN(generated_ms)
               FROM practical_signals'''
        ).fetchone()
        practical_open_net_sum = 0.0
        practical_open_pnl_eur = 0.0
        practical_open_notional_eur = 0.0
        practical_unpriced_open = 0
        practical_open_positions: list[dict[str, object]] = []
        for (
            practical_ms,
            market,
            entry_price,
            notional_eur,
            base_quantity,
            max_net_return,
            trailing_floor,
            last_mark_ms,
            last_sell_price,
            last_net_return,
        ) in conn.execute(
            '''SELECT generated_ms,market,entry_price,notional_eur,base_quantity,
                      max_net_return_pct,trailing_floor_pct,last_mark_ms,last_sell_price,
                      last_net_return_pct
               FROM practical_signals WHERE status='OPEN' '''
        ).fetchall():
            position_notional = float(notional_eur)
            practical_open_notional_eur += position_notional
            current_net = None if last_net_return is None else float(last_net_return)
            if current_net is None:
                practical_unpriced_open += 1
            else:
                practical_open_net_sum += current_net
                practical_open_pnl_eur += current_net * position_notional / 100.0
            practical_open_positions.append({
                'market': str(market),
                'age_hours': round((generated_ms - int(practical_ms)) / 3_600_000.0, 2),
                'notional_eur': round(position_notional, 2),
                'base_quantity': round(float(base_quantity), 12),
                'current_sell_price': (
                    None if last_sell_price is None else round(float(last_sell_price), 8)
                ),
                'current_net_pct': None if current_net is None else round(current_net, 3),
                'current_pnl_eur': (
                    None if current_net is None
                    else round(current_net * position_notional / 100.0, 2)
                ),
                'max_net_pct': round(float(max_net_return), 3),
                'trailing_floor_pct': (
                    None if trailing_floor is None else round(float(trailing_floor), 3)
                ),
                'last_mark_age_seconds': (
                    None if last_mark_ms is None
                    else round(max(0.0, (generated_ms - int(last_mark_ms)) / 1000.0), 1)
                ),
            })
        profit_factor = float(positive_sum) / float(negative_sum) if float(negative_sum) > 0 else 0.0
        practical_profit_factor = (
            float(practical_positive_eur) / float(practical_negative_eur)
            if float(practical_negative_eur) > 0
            else (999.0 if float(practical_positive_eur) > 0 else 0.0)
        )
        balance = paper_start_eur
        peak = balance
        max_drawdown_pct = 0.0
        for net_pct, notional in conn.execute(
            '''SELECT net_return_pct,notional_eur FROM practical_signals
               WHERE status='CLOSED' ORDER BY evaluated_ms,generated_ms,market'''
        ).fetchall():
            balance += float(net_pct) * float(notional) / 100.0
            peak = max(peak, balance)
            if peak > 0.0:
                max_drawdown_pct = max(max_drawdown_pct, (peak - balance) / peak * 100.0)
        test_span_days = (
            0.0 if practical_first_ms is None
            else max(0.0, (generated_ms - int(practical_first_ms)) / 86_400_000.0)
        )
        enough_history = (
            int(practical_closed or 0) >= eval_min_trades
            and test_span_days >= eval_min_span_days
        )
        if not enough_history:
            evaluation_status = 'VERZAMELEN'
        elif (
            practical_profit_factor >= eval_min_profit_factor
            and max_drawdown_pct <= eval_max_drawdown_pct
            and float(practical_realized_pnl_eur) > 0.0
        ):
            evaluation_status = 'PAPER KANDIDAAT'
        else:
            evaluation_status = 'ONVOLDOENDE'
        paper_cash_eur = (
            paper_start_eur
            + float(practical_realized_pnl_eur)
            - practical_open_notional_eur
        )
        paper_equity_eur = (
            paper_start_eur
            + float(practical_realized_pnl_eur)
            + practical_open_pnl_eur
        )
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
            'practical_total': int(practical_total or 0),
            'practical_open': int(practical_open_count or 0),
            'practical_closed': int(practical_closed or 0),
            'practical_wins': int(practical_wins or 0),
            'practical_losses': int((practical_closed or 0) - (practical_wins or 0)),
            'practical_net_return_sum_pct': round(float(practical_net_sum), 3),
            'practical_open_net_return_sum_pct': round(float(practical_open_net_sum), 3),
            'practical_pnl_eur': round(float(practical_realized_pnl_eur), 2),
            'practical_open_pnl_eur': round(practical_open_pnl_eur, 2),
            'practical_open_notional_eur': round(practical_open_notional_eur, 2),
            'practical_unpriced_open': practical_unpriced_open,
            'practical_paper_cash_eur': round(paper_cash_eur, 2),
            'practical_paper_equity_eur': round(paper_equity_eur, 2),
            'practical_open_positions': practical_open_positions,
            'practical_profit_factor': round(practical_profit_factor, 3),
            'practical_max_drawdown_pct': round(max_drawdown_pct, 3),
            'practical_test_span_days': round(test_span_days, 2),
            'practical_evaluation': {
                'status': evaluation_status,
                'min_closed_trades': eval_min_trades,
                'min_span_days': eval_min_span_days,
                'min_profit_factor': eval_min_profit_factor,
                'max_drawdown_pct': eval_max_drawdown_pct,
            },
            'practical_outcomes': {
                'stop': int(practical_stops or 0),
                'trail': int(practical_trails or 0),
                'time': int(practical_times or 0),
            },
            'practical_rules': {
                'entry': 'LONG WATCH, SIDEWAYS WATCH of zeldzame LONG tegen L2-buy-VWAP',
                'exit': 'open posities iedere 30 seconden tegen L2-sell-VWAP',
                'stop_net_pct': PRACTICAL_STOP_NET_PCT,
                'trail_activate_net_pct': PRACTICAL_TRAIL_ACTIVATE_NET_PCT,
                'trail_giveback_pct': PRACTICAL_TRAIL_GIVEBACK_PCT,
                'minimum_locked_net_pct': PRACTICAL_MIN_LOCK_NET_PCT,
                'max_hold_hours': PRACTICAL_MAX_HOLD_MS // (60 * 60 * 1000),
                'max_hold_only_without_trailing': True,
                'shadow_notional_eur': shadow_notional_eur,
                'paper_start_eur': paper_start_eur,
                'max_open_positions': max_open_positions,
                'reentry_cooldown_hours': (
                    PRACTICAL_REENTRY_COOLDOWN_MS // (60 * 60 * 1000)
                ),
            },
        }
    finally:
        conn.close()


def _monitor_practical_positions(
    settings: Settings,
    *,
    api: BitvavoPublic | None = None,
    generated_ms: int | None = None,
    monitor_interval_seconds: int = PRACTICAL_MONITOR_SECONDS,
) -> dict[str, object]:
    now_ms = int(time.time() * 1000) if generated_ms is None else int(generated_ms)
    conn = _scanner_db_connect()
    try:
        positions = [
            (str(row[0]), float(row[1]), float(row[2]))
            for row in conn.execute(
                '''SELECT market,base_quantity,notional_eur
                   FROM practical_signals WHERE status='OPEN' ORDER BY market'''
            ).fetchall()
        ]
    finally:
        conn.close()

    market_api = api or BitvavoPublic(
        settings.api_base_url,
        settings.request_timeout_seconds,
        settings.request_retries,
    )
    current_rows: list[dict[str, object]] = []
    errors: list[str] = []
    for market, base_quantity, notional_eur in positions:
        try:
            if hasattr(market_api, 'sell_vwap_for_base'):
                depth = market_api.sell_vwap_for_base(market, base_quantity)
            else:
                depth = market_api.depth_book(market, notional_eur)
            current_rows.append({
                'market': market,
                'sell_vwap': float(depth['sell_vwap']),
            })
        except Exception as exc:
            errors.append(f'{market}: {type(exc).__name__}: {exc}')

    research = _persist_signal_research({
        'generated_at_ms': now_ms,
        'regime': 'UNKNOWN',
        'rules': {
            'shadow_notional_eur': settings.position_eur,
            'paper_start_eur': getattr(settings, 'paper_start_eur', 5000.0),
            'max_open_positions': getattr(settings, 'max_open_positions', 1),
            'eval_min_trades': getattr(settings, 'eval_min_trades', 40),
            'eval_min_span_days': getattr(settings, 'eval_min_span_days', 14.0),
            'eval_min_profit_factor': getattr(settings, 'eval_min_profit_factor', 1.25),
            'eval_max_drawdown_pct': getattr(settings, 'eval_max_drawdown_pct', 10.0),
        },
        'candidates': [],
        'all_pair_snapshots': current_rows,
        'rare_opportunities': [],
    })
    research['practical_monitor_interval_seconds'] = monitor_interval_seconds
    research['practical_monitor_attempted_ms'] = now_ms
    research['practical_monitor_generated_ms'] = now_ms if current_rows or not positions else 0
    research['practical_monitor_position_count'] = len(positions)
    research['practical_monitor_success_count'] = len(current_rows)
    research['practical_monitor_errors'] = errors
    return research


def _write_practical_monitor_to_report(research: dict[str, object]) -> None:
    report = _load_report()
    if report is None:
        return
    report['signal_research'] = research
    _write_report(report)


def _write_human_research_to_report(research: dict[str, object]) -> None:
    report = _load_report()
    if report is None:
        return
    existing = report.get('human_research', {})
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(research)
    report['human_research'] = merged
    _write_report(report)


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
    print('=== CRYPTO SCANNER v3.5 | BASIS + MENSELIJKE BESLISLAAG | READ ONLY ===')
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
        print('=== PRAKTISCHE WATCH-PAPERTEST ===')
        print(
            f"entries {int(research.get('practical_total',0))}"
            f" | open {int(research.get('practical_open',0))}"
            f" | gesloten {int(research.get('practical_closed',0))}"
            f" | W/L {int(research.get('practical_wins',0))}/{int(research.get('practical_losses',0))}"
        )
        print(
            f"gesloten PnL €{float(research.get('practical_pnl_eur',0.0)):+.2f}"
            f" | open indicatie €{float(research.get('practical_open_pnl_eur',0.0)):+.2f}"
            f" | PF {'∞' if float(research.get('practical_profit_factor',0.0)) >= 999.0 else f'{float(research.get('practical_profit_factor',0.0)):.3f}'}"
        )
        print(
            f"paper cash €{float(research.get('practical_paper_cash_eur',0.0)):.2f}"
            f" | equity €{float(research.get('practical_paper_equity_eur',0.0)):.2f}"
            f" | max drawdown {float(research.get('practical_max_drawdown_pct',0.0)):.2f}%"
        )
        evaluation = research.get('practical_evaluation', {})
        if isinstance(evaluation, dict):
            print(
                f"evaluatie {evaluation.get('status','VERZAMELEN')}"
                f" | vereist {int(evaluation.get('min_closed_trades',40))} gesloten trades"
                f" en {float(evaluation.get('min_span_days',14.0)):.0f} dagen"
            )
        outcomes = research.get('practical_outcomes', {})
        if isinstance(outcomes, dict):
            print(
                f"uitgangen stop {int(outcomes.get('stop',0))}"
                f" | trailing {int(outcomes.get('trail',0))}"
                f" | tijd {int(outcomes.get('time',0))}"
            )
        practical_rules = research.get('practical_rules', {})
        if not isinstance(practical_rules, dict):
            practical_rules = {}
        print(
            f"regels: €{float(practical_rules.get('shadow_notional_eur',shadow)):.0f} per kans"
            f" | max {int(practical_rules.get('max_open_positions',1))} tegelijk"
            ' | alleen LONG/SIDEWAYS WATCH'
        )
        print(
            f"exacte muntomvang iedere "
            f"{int(research.get('practical_monitor_interval_seconds',PRACTICAL_MONITOR_SECONDS))} sec via L2"
            f" | herinstap na {int(practical_rules.get('reentry_cooldown_hours',4))} uur"
        )
        print(
            'stop netto -3,00% | winstbeveiliging vanaf +1,00%'
            ' | max 48 uur vervalt zodra trailing actief is'
        )
        attempted_ms = int(research.get('practical_monitor_attempted_ms', 0) or 0)
        monitor_ms = int(research.get('practical_monitor_generated_ms', 0) or 0)
        if int(research.get('practical_open', 0)) <= 0:
            print('winstmonitor: stand-by; geen open PAPER-positie')
        elif monitor_ms > 0:
            monitor_age = max(0.0, (time.time() * 1000.0 - monitor_ms) / 1000.0)
            print(f'winstmonitor: laatste geldige L2-meting {monitor_age:.0f} sec geleden')
        elif attempted_ms > 0:
            print('winstmonitor: draait, maar laatste L2-meting is mislukt')
        else:
            print('winstmonitor: wacht op eerste 30-secondenmeting')
        practical_positions = research.get('practical_open_positions', [])
        if isinstance(practical_positions, list):
            for item in practical_positions:
                if not isinstance(item, dict):
                    continue
                current_net = item.get('current_net_pct')
                current_pnl = item.get('current_pnl_eur')
                current_text = (
                    'geen actuele L2-prijs'
                    if current_net is None or current_pnl is None
                    else f"netto {float(current_net):+.3f}% / €{float(current_pnl):+.2f}"
                )
                floor = item.get('trailing_floor_pct')
                floor_text = 'uit' if floor is None else f'{float(floor):+.3f}%'
                mark_age = item.get('last_mark_age_seconds')
                mark_text = 'geen meting' if mark_age is None else f'meting {float(mark_age):.0f}s oud'
                print(
                    f"  {str(item.get('market','?')):12s} | {current_text}"
                    f" | max {float(item.get('max_net_pct',0.0)):+.3f}%"
                    f" | trailing {floor_text} | {mark_text} | {float(item.get('age_hours',0.0)):.2f}u"
                )
        monitor_errors = research.get('practical_monitor_errors', [])
        if isinstance(monitor_errors, list) and monitor_errors:
            print(f'winstmonitor-waarschuwingen: {len(monitor_errors)}')
            for monitor_error in monitor_errors[:3]:
                print(f'  - {monitor_error}')
    human = report.get('human_research', {})
    if isinstance(human, dict):
        print()
        print('=== MENSELIJKE BESLISLAAG — 5M PAPER CHALLENGER ===')
        print(
            f"shortlist {int(human.get('human_shortlist_count',0))}"
            f" | controles 24u {int(human.get('human_decisions_24h',0))}"
            f" | instapbesluiten 24u {int(human.get('human_entry_decisions_24h',0))}"
        )
        print(
            f"entries {int(human.get('human_total',0))}"
            f" | open {int(human.get('human_open',0))}"
            f" | gesloten {int(human.get('human_closed',0))}"
            f" | W/L {int(human.get('human_wins',0))}/{int(human.get('human_losses',0))}"
        )
        human_pf = float(human.get('human_profit_factor', 0.0))
        print(
            f"gesloten PnL €{float(human.get('human_pnl_eur',0.0)):+.2f}"
            f" | open indicatie €{float(human.get('human_open_pnl_eur',0.0)):+.2f}"
            f" | PF {'∞' if human_pf >= 999.0 else f'{human_pf:.3f}'}"
        )
        print(
            f"paper equity €{float(human.get('human_paper_equity_eur',0.0)):.2f}"
            f" | max drawdown {float(human.get('human_max_drawdown_pct',0.0)):.2f}%"
            f" | evaluatie {human.get('human_evaluation','VERZAMELEN')}"
        )
        print('alleen EUR-routes | context 15m/1h | timing 5m iedere 60 sec')
        print('volume + BTC + L2-orderboekdruk | eigen PAPER-kapitaal, max 1 positie')
        print(
            'anti-pump + marktschokveto actief | nieuws nog niet betrouwbaar geautomatiseerd'
        )
        human_status = str(human.get('human_entry_status', 'WACHT OP EERSTE CONTROLE'))
        print(f'beslisstatus: {human_status}')
        latest = human.get('human_latest_decisions', [])
        if isinstance(latest, list):
            seen: set[str] = set()
            for item in latest:
                if not isinstance(item, dict):
                    continue
                market = str(item.get('market', '?'))
                if market in seen:
                    continue
                seen.add(market)
                blockers = item.get('blockers', [])
                reason = ', '.join(str(value) for value in blockers[:3]) if isinstance(blockers, list) else ''
                print(
                    f"  {market:12s} | {str(item.get('action','WACHTEN')):11s}"
                    f" | score {float(item.get('score',0.0)):4.1f}"
                    f" | {item.get('trigger','geen')}"
                    + (f" | blokkade: {reason}" if reason else '')
                )
                if len(seen) >= 3:
                    break
        human_positions = human.get('human_open_positions', [])
        if isinstance(human_positions, list):
            for item in human_positions:
                if not isinstance(item, dict):
                    continue
                current_net = item.get('current_net_pct')
                current = (
                    'geen actuele L2-prijs' if current_net is None
                    else f"netto {float(current_net):+.3f}% / €{float(item.get('current_pnl_eur',0.0)):+.2f}"
                )
                floor = item.get('trailing_floor_pct')
                print(
                    f"  OPEN {str(item.get('market','?')):7s} | {current}"
                    f" | max {float(item.get('max_net_pct',0.0)):+.3f}%"
                    f" | trailing {'uit' if floor is None else f'{float(floor):+.3f}%'}"
                )
        human_errors = human.get('human_entry_errors', [])
        monitor_errors = human.get('human_monitor_errors', [])
        combined_errors = []
        if isinstance(human_errors, list):
            combined_errors.extend(human_errors)
        if isinstance(monitor_errors, list):
            combined_errors.extend(monitor_errors)
        if combined_errors:
            print(f'menselijke-laag waarschuwingen: {len(combined_errors)}')
            for human_error in combined_errors[:3]:
                print(f'  - {human_error}')
    if isinstance(research, dict):
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
    print(
        f'LET OP: alleen actuele €{float(shadow):.0f}-L2-uitvoer, prijs in zone en '
        'netto R/R ≥ 1,50 kunnen een kanslabel geven.'
    )


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
    monitor_seconds = max(
        10,
        int(os.getenv('SCANNER_PRACTICAL_MONITOR_SECONDS', str(PRACTICAL_MONITOR_SECONDS))),
    )
    human_seconds = max(
        30,
        int(os.getenv('SCANNER_HUMAN_TRIGGER_SECONDS', str(HUMAN_TRIGGER_POLL_SECONDS))),
    )
    try:
        research = _monitor_practical_positions(
            settings, monitor_interval_seconds=monitor_seconds
        )
        _write_practical_monitor_to_report(research)
    except Exception as exc:
        logger.exception('eerste praktische 30s-monitor mislukt: %s', exc)
    try:
        human_research = _monitor_human_positions(
            settings, monitor_interval_seconds=monitor_seconds
        )
        _write_human_research_to_report(human_research)
    except Exception as exc:
        logger.exception('eerste menselijke PAPER-positiemonitor mislukt: %s', exc)

    while not STOP:
        try:
            previous_report = _load_report()
            report = scan_once(settings)
            report['signal_research'] = _persist_signal_research(report)
            previous_research = (
                previous_report.get('signal_research', {})
                if isinstance(previous_report, dict) else {}
            )
            if isinstance(previous_research, dict):
                for key in (
                    'practical_monitor_interval_seconds',
                    'practical_monitor_attempted_ms',
                    'practical_monitor_generated_ms',
                    'practical_monitor_position_count',
                    'practical_monitor_success_count',
                    'practical_monitor_errors',
                ):
                    if key in previous_research:
                        report['signal_research'][key] = previous_research[key]
            human_research = _monitor_human_entries(settings, report)
            previous_human = (
                previous_report.get('human_research', {})
                if isinstance(previous_report, dict) else {}
            )
            if isinstance(previous_human, dict):
                for key in (
                    'human_monitor_interval_seconds',
                    'human_monitor_attempted_ms',
                    'human_monitor_generated_ms',
                    'human_monitor_position_count',
                    'human_monitor_success_count',
                    'human_monitor_errors',
                ):
                    if key in previous_human:
                        human_research[key] = previous_human[key]
            report['human_research'] = human_research
            _strip_internal_research_data(report)
            _write_report(report)
            print_report(report)
        except Exception as exc:
            logger.exception('scanner-v2-cyclus mislukt: %s', exc)
            if args.once:
                return 2
        if args.once:
            return 0
        next_monitor_at = time.monotonic() + monitor_seconds
        next_human_at = time.monotonic() + human_seconds
        for _ in range(poll_seconds):
            if STOP:
                break
            if time.monotonic() >= next_monitor_at:
                try:
                    research = _monitor_practical_positions(
                        settings, monitor_interval_seconds=monitor_seconds
                    )
                    _write_practical_monitor_to_report(research)
                except Exception as exc:
                    logger.exception('praktische 30s-monitor mislukt: %s', exc)
                try:
                    human_research = _monitor_human_positions(
                        settings, monitor_interval_seconds=monitor_seconds
                    )
                    _write_human_research_to_report(human_research)
                except Exception as exc:
                    logger.exception('menselijke PAPER-positiemonitor mislukt: %s', exc)
                next_monitor_at = time.monotonic() + monitor_seconds
            if time.monotonic() >= next_human_at:
                try:
                    current_report = _load_report()
                    if current_report is not None:
                        human_research = _monitor_human_entries(settings, current_report)
                        _write_human_research_to_report(human_research)
                except Exception as exc:
                    logger.exception('menselijke 5m-beslislaag mislukt: %s', exc)
                next_human_at = time.monotonic() + human_seconds
            time.sleep(1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
