from __future__ import annotations

import argparse
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import main as core
from adaptive_trend_strategy import AdaptiveTrendStrategy
from adaptive_trend_trader import AdaptiveTrendPaperTrader
from bitvavo_public import BitvavoPublic, INTERVAL_MS
from config import Settings
from market_data import MarketDataSource
from models import Candle, Decision
from readiness import print_readiness
from report import print_report
from status import print_status
from storage import Storage

logger = logging.getLogger('cryptobot_cleanroom_adaptive_trend_v1')
STOP = False
ADAPTIVE_MAX_OPEN_POSITIONS = 3


@dataclass
class MarketContext:
    market: str
    candles: list[Candle]
    latest_ts: int
    has_new_candle: bool
    had_position: bool
    metrics: dict[str, float]


@dataclass(frozen=True)
class AdaptiveCandidate:
    market: str
    candle_ts: int
    decision: Decision
    score: float
    atr_pct: float


def adaptive_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_adaptive_trend_v1{suffix}'))


def build_adaptive_settings(primary_s: Settings) -> Settings:
    adaptive = replace(
        primary_s,
        db_path=adaptive_db_path(primary_s.db_path),
        max_open_positions=ADAPTIVE_MAX_OPEN_POSITIONS,
        stop_loss_pct=1.25,
        take_profit_pct=30.0,
    )
    adaptive.validate()
    return adaptive


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    core.STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def shared_universe(primary_s: Settings, db: Storage, once: bool) -> list[str] | None:
    while not STOP:
        primary = Storage(primary_s.db_path, primary_s.paper_start_eur)
        try:
            markets = primary.universe()
        finally:
            primary.close()

        if markets:
            existing = db.universe()
            if existing and existing != markets:
                raise RuntimeError('adaptive universe wijkt af van primaire vaste universe')
            if not existing:
                db.set_universe(markets)
            db.set_data_health('UNIVERSE_READY', f'gedeelde universe beschikbaar: {len(markets)} markten')
            return markets

        db.set_data_health('STARTING', 'wacht op primaire vaste universe')
        if once:
            return None
        for _ in range(5):
            if STOP:
                return None
            time.sleep(1)
    return None


def _log_event(event, suffix: str = '') -> None:
    if event is None:
        return
    logger.info(
        '%s %s @ %.8f | %s | pnl=%s%s',
        event.market,
        event.kind,
        event.price,
        event.reason,
        '-' if event.pnl_eur is None else f'€{event.pnl_eur:+.2f}',
        suffix,
    )


def _scan_market(
    market: str,
    api: MarketDataSource,
    db: Storage,
    strategy: AdaptiveTrendStrategy,
    trader: AdaptiveTrendPaperTrader,
    s: Settings,
) -> MarketContext:
    candles = api.closed_candles(market, s.interval, s.candle_limit)
    if len(candles) < strategy.required_candles():
        raise RuntimeError(f'{market}: onvoldoende gesloten candles voor adaptive trend')
    db.save_candles(market, s.interval, candles)

    had_position = db.get_position(market) is not None
    last_done = db.last_processed(market)
    if last_done == 0:
        new_candles = [candles[-1]]
    else:
        new_candles = [c for c in candles if c.timestamp_ms > last_done]

    for candle in new_candles:
        _log_event(trader.process_candle(market, candle))
        db.set_last_processed(market, candle.timestamp_ms)

    metrics = strategy.analyze(candles)
    if not metrics:
        raise RuntimeError(f'{market}: adaptive indicatoren konden niet worden berekend')

    return MarketContext(
        market=market,
        candles=candles,
        latest_ts=candles[-1].timestamp_ms,
        has_new_candle=bool(new_candles),
        had_position=had_position,
        metrics=metrics,
    )


def _manage_open_position(
    ctx: MarketContext,
    regime: str,
    api: MarketDataSource,
    db: Storage,
    strategy: AdaptiveTrendStrategy,
    trader: AdaptiveTrendPaperTrader,
) -> int:
    if db.get_position(ctx.market) is None:
        return 0

    if ctx.has_new_candle:
        reason = strategy.exit_reason(ctx.metrics, regime)
        if reason:
            _log_event(
                trader.close_trend_break(ctx.market, ctx.metrics['close'], reason),
                ' | trend-regime',
            )
            return 0

    try:
        book = api.book(ctx.market)
    except Exception:
        logger.exception('%s: adaptive orderboek voor open positie mislukt', ctx.market)
        return 1

    _log_event(
        trader.process_book(ctx.market, book, ctx.metrics['atr_pct']),
        ' | intracycle',
    )
    return 0


def _candidate_for_context(
    ctx: MarketContext,
    regime: str,
    breadth_pct: float,
    db: Storage,
    strategy: AdaptiveTrendStrategy,
    s: Settings,
) -> AdaptiveCandidate | None:
    if not ctx.has_new_candle:
        return None

    decision = strategy.evaluate_metrics(ctx.metrics, regime, breadth_pct)
    close_time = ctx.latest_ts + INTERVAL_MS[s.interval]
    age_seconds = max(0.0, (int(time.time() * 1000) - close_time) / 1000.0)
    if decision.action == 'BUY' and age_seconds > s.max_signal_age_seconds:
        decision = Decision('SKIP', 'signaal_te_oud', {**decision.metrics, 'age_seconds': age_seconds})

    if decision.action == 'BUY' and ctx.had_position:
        decision = Decision('SKIP', 'positie_in_deze_cyclus', decision.metrics)
    elif decision.action == 'BUY' and db.get_position(ctx.market) is not None:
        decision = Decision('SKIP', 'positie_bestaat_al', decision.metrics)

    if decision.action != 'BUY':
        db.save_decision(ctx.market, ctx.latest_ts, decision)
        logger.info('%s SKIP | %s | regime=%s breadth=%.1f%%', ctx.market, decision.reason, regime, breadth_pct)
        return None

    score = strategy.rank_score(decision)
    if not math.isfinite(score):
        bad = Decision('SKIP', 'ongeldige_adaptive_score', decision.metrics)
        db.save_decision(ctx.market, ctx.latest_ts, bad)
        return None

    return AdaptiveCandidate(
        market=ctx.market,
        candle_ts=ctx.latest_ts,
        decision=decision,
        score=score,
        atr_pct=float(ctx.metrics['atr_pct']),
    )


def _execute_candidates(
    candidates: list[AdaptiveCandidate],
    api: MarketDataSource,
    db: Storage,
    trader: AdaptiveTrendPaperTrader,
    s: Settings,
) -> int:
    ranked = sorted(candidates, key=lambda c: (-c.score, c.market))
    slots = max(0, s.max_open_positions - len(db.all_positions()))
    failures = 0

    for rank, candidate in enumerate(ranked, start=1):
        metrics = {
            **candidate.decision.metrics,
            'rank_score': candidate.score,
            'candidate_rank': float(rank),
            'candidate_count': float(len(ranked)),
        }
        if slots <= 0:
            db.save_decision(
                candidate.market,
                candidate.candle_ts,
                Decision('SKIP', 'rank_buiten_top_slots', metrics),
            )
            continue

        try:
            book = api.book(candidate.market)
        except Exception as exc:
            failures += 1
            db.save_decision(
                candidate.market,
                candidate.candle_ts,
                Decision('SKIP', 'entry_book_error', {**metrics, 'error_code': 1.0}),
            )
            logger.exception('%s: adaptive entry-orderboek mislukt: %s', candidate.market, type(exc).__name__)
            continue

        allowed, block_reason = trader.can_open(candidate.market, book)
        if not allowed:
            db.save_decision(
                candidate.market,
                candidate.candle_ts,
                Decision('SKIP', block_reason, {**metrics, 'spread_pct': book.spread_pct}),
            )
            continue

        event = trader.open_long_adaptive(
            candidate.market,
            book,
            candidate.candle_ts,
            candidate.atr_pct,
        )
        if event is None:
            db.save_decision(candidate.market, candidate.candle_ts, Decision('SKIP', 'open_mislukt', metrics))
            continue

        db.save_decision(
            candidate.market,
            candidate.candle_ts,
            Decision('BUY', candidate.decision.reason, {**metrics, 'spread_pct': book.spread_pct}),
        )
        slots -= 1
        logger.info(
            '%s OPEN @ %.8f | %s | rank=%s/%s score=%.4f | ATR=%.2f%% | init_stop=%.2f%%',
            candidate.market,
            event.price,
            candidate.decision.reason,
            rank,
            len(ranked),
            candidate.score,
            candidate.atr_pct,
            trader.initial_stop_pct(candidate.atr_pct),
        )

    return failures


def run_adaptive_cycle(
    api: MarketDataSource,
    db: Storage,
    strategy: AdaptiveTrendStrategy,
    trader: AdaptiveTrendPaperTrader,
    s: Settings,
    markets: list[str],
) -> tuple[int, int, str]:
    ok = 0
    failed = 0
    last_error = ''
    contexts: list[MarketContext] = []

    for market in markets:
        if STOP:
            break
        try:
            contexts.append(_scan_market(market, api, db, strategy, trader, s))
            ok += 1
        except Exception as exc:
            failed += 1
            last_error = f'{market}: {type(exc).__name__}: {exc}'
            logger.exception('%s: adaptive scan mislukt', market)

    metrics_by_market = {ctx.market: ctx.metrics for ctx in contexts}
    regime, breadth_pct = strategy.market_regime(metrics_by_market)
    db.set_state('adaptive_regime', regime)
    db.set_state('adaptive_breadth_pct', f'{breadth_pct:.6f}')

    management_failures = 0
    for ctx in contexts:
        management_failures += _manage_open_position(ctx, regime, api, db, strategy, trader)

    candidates: list[AdaptiveCandidate] = []
    for ctx in contexts:
        candidate = _candidate_for_context(ctx, regime, breadth_pct, db, strategy, s)
        if candidate is not None:
            candidates.append(candidate)

    entry_failures = _execute_candidates(candidates, api, db, trader, s) if candidates else 0
    extra_failures = management_failures + entry_failures
    if extra_failures:
        ok = max(0, ok - extra_failures)
        failed += extra_failures
        last_error = f'{extra_failures} adaptive orderboek request(s) mislukt'

    logger.info(
        'adaptive regime=%s | breadth=%.1f%% | candidates=%s | open=%s/%s',
        regime,
        breadth_pct,
        len(candidates),
        len(db.all_positions()),
        s.max_open_positions,
    )
    return ok, failed, last_error


def main() -> int:
    parser = argparse.ArgumentParser(
        description='CryptoBot Clean-Room Strategy D v1 - Adaptive Trend Follower PAPER'
    )
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--readiness', action='store_true')
    args = parser.parse_args()

    primary_s = Settings()
    primary_s.validate()
    core.setup_logging(primary_s.log_level)
    s = build_adaptive_settings(primary_s)
    db = Storage(s.db_path, s.paper_start_eur)

    try:
        if args.status:
            print('=== STRATEGY D v1 | ADAPTIVE TREND FOLLOWER | ATR RUNNER | PAPER ONLY ===')
            print_status(db, s)
            print(f"MARKTREGIME     : {db.get_state('adaptive_regime', 'UNKNOWN')}")
            print(f"BREADTH         : {float(db.get_state('adaptive_breadth_pct', '0') or 0):.1f}%")
            return 0
        if args.report:
            print('=== STRATEGY D v1 | ADAPTIVE TREND FOLLOWER | ATR RUNNER | PAPER ONLY ===')
            print_report(db, s)
            print(f"MARKTREGIME     : {db.get_state('adaptive_regime', 'UNKNOWN')}")
            print(f"BREADTH         : {float(db.get_state('adaptive_breadth_pct', '0') or 0):.1f}%")
            return 0
        if args.readiness:
            print('=== STRATEGY D v1 | ADAPTIVE TREND FOLLOWER | ATR RUNNER | PAPER ONLY ===')
            print_readiness(db, s)
            return 0

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        core.STOP = False

        markets = shared_universe(primary_s, db, args.once)
        if markets is None:
            return 2 if args.once else 0

        api = BitvavoPublic(s.api_base_url, s.request_timeout_seconds, s.request_retries)
        strategy = AdaptiveTrendStrategy(s)
        trader = AdaptiveTrendPaperTrader(s, db)

        logger.info(
            'gestart | STRATEGY D v1 ADAPTIVE TREND | PAPER ONLY | interval=%s | '
            'max_open=%s | initial_stop=ATR*%.2f [%.2f..%.2f]%% | '
            'protect=+%.2f%% lock=€%.2f | trail=ATR*%.2f [%.2f..%.2f]%% | hard_take=UIT | db=%s',
            s.interval,
            s.max_open_positions,
            trader.INITIAL_ATR_MULT,
            trader.INITIAL_STOP_MIN_PCT,
            trader.INITIAL_STOP_MAX_PCT,
            trader.PROTECT_TRIGGER_PCT,
            trader.LOCK_PROFIT_EUR,
            trader.TRAIL_ATR_MULT,
            trader.TRAIL_MIN_PCT,
            trader.TRAIL_MAX_PCT,
            s.db_path,
        )

        loop = not args.once and s.loop_enabled
        consecutive_total_failures = 0
        while True:
            ok, failed, last_error = run_adaptive_cycle(api, db, strategy, trader, s, markets)
            if ok == len(markets) and failed == 0:
                consecutive_total_failures = 0
                db.set_data_health('READY', f'volledige adaptive-trend-v1-cyclus ok={ok}')
            elif ok > 0:
                consecutive_total_failures = 0
                db.set_data_health('PARTIAL', f'adaptive-trend-v1-cyclus ok={ok} failed={failed}; {last_error}')
            else:
                consecutive_total_failures += 1
                db.set_data_health('DEGRADED', last_error or f'alle {failed} adaptive-marktcycli mislukt')

            if consecutive_total_failures >= s.max_consecutive_failed_cycles:
                logger.error('adaptive marktdata volledig onbereikbaar gedurende %s cycli', consecutive_total_failures)
                core.log_public_probe(api)
                consecutive_total_failures = 0

            if not loop or STOP:
                return 0 if ok > 0 else 2
            for _ in range(s.poll_seconds):
                if STOP:
                    break
                time.sleep(1)
    finally:
        db.close()
        logger.info('gestopt')


if __name__ == '__main__':
    sys.exit(main())
