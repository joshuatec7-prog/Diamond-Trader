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
from bitvavo_public import BitvavoPublic, INTERVAL_MS
from config import Settings
from market_data import MarketDataSource
from models import Decision
from paper_trader import PaperTrader
from readiness import print_readiness
from report import print_report
from status import print_status
from storage import Storage
from trend_strategy import TrendMomentumStrategy

logger = logging.getLogger('cryptobot_cleanroom_trend')
STOP = False
TREND_MAX_OPEN_POSITIONS = 3


@dataclass(frozen=True)
class TrendCandidate:
    market: str
    candle_ts: int
    decision: Decision
    score: float


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    core.STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def trend_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_trend_v2{suffix}'))


def build_trend_settings(primary_s: Settings) -> Settings:
    trend_s = replace(
        primary_s,
        db_path=trend_db_path(primary_s.db_path),
        max_open_positions=TREND_MAX_OPEN_POSITIONS,
    )
    trend_s.validate()
    return trend_s


def shared_universe(primary_s: Settings, trend_db: Storage, once: bool) -> list[str] | None:
    while not STOP:
        primary_db = Storage(primary_s.db_path, primary_s.paper_start_eur)
        try:
            markets = primary_db.universe()
        finally:
            primary_db.close()

        if markets:
            existing = trend_db.universe()
            if existing and existing != markets:
                raise RuntimeError('trend-universe wijkt af van primaire vaste universe')
            if not existing:
                trend_db.set_universe(markets)
            trend_db.set_data_health('UNIVERSE_READY', f'gedeelde universe beschikbaar: {len(markets)} markten')
            return markets

        trend_db.set_data_health('STARTING', 'wacht op primaire vaste universe')
        if once:
            return None
        for _ in range(5):
            if STOP:
                return None
            time.sleep(1)
    return None


def scan_market_for_candidate(
    market: str,
    api: MarketDataSource,
    db: Storage,
    strategy: TrendMomentumStrategy,
    trader: PaperTrader,
    s: Settings,
) -> TrendCandidate | None:
    candles = api.closed_candles(market, s.interval, s.candle_limit)
    if len(candles) < strategy.required_candles():
        raise RuntimeError(f'{market}: onvoldoende gesloten candles voor trendstrategie')
    db.save_candles(market, s.interval, candles)

    last_done = db.last_processed(market)
    if last_done == 0:
        new_candles = [candles[-1]]
    else:
        new_candles = [c for c in candles if c.timestamp_ms > last_done]

    for candle in new_candles:
        event = trader.process_candle(market, candle)
        if event:
            logger.info(
                '%s %s @ %.8f | %s | pnl=%s',
                event.market,
                event.kind,
                event.price,
                event.reason,
                '-' if event.pnl_eur is None else f'€{event.pnl_eur:+.2f}',
            )
        db.set_last_processed(market, candle.timestamp_ms)

    if db.get_position(market) is not None:
        live_book = api.book(market)
        event = trader.process_book(market, live_book)
        if event:
            logger.info(
                '%s %s @ %.8f | %s | pnl=%s | intracycle',
                event.market,
                event.kind,
                event.price,
                event.reason,
                '-' if event.pnl_eur is None else f'€{event.pnl_eur:+.2f}',
            )

    if not new_candles:
        return None

    latest = candles[-1]
    decision = strategy.evaluate(candles)
    now_ms = int(time.time() * 1000)
    close_time = latest.timestamp_ms + INTERVAL_MS[s.interval]
    age_seconds = max(0.0, (now_ms - close_time) / 1000.0)

    if decision.action == 'BUY' and age_seconds > s.max_signal_age_seconds:
        decision = Decision(
            'SKIP',
            'signaal_te_oud',
            {**decision.metrics, 'age_seconds': age_seconds},
        )

    if decision.action == 'BUY' and db.get_position(market) is not None:
        decision = Decision('SKIP', 'positie_bestaat_al', decision.metrics)

    if decision.action != 'BUY':
        db.save_decision(market, latest.timestamp_ms, decision)
        logger.info('%s SKIP | %s', market, decision.reason)
        return None

    score = strategy.rank_score(decision)
    if not math.isfinite(score):
        decision = Decision('SKIP', 'ongeldige_trend_score', decision.metrics)
        db.save_decision(market, latest.timestamp_ms, decision)
        logger.info('%s SKIP | %s', market, decision.reason)
        return None

    return TrendCandidate(market, latest.timestamp_ms, decision, score)


def execute_ranked_candidates(
    candidates: list[TrendCandidate],
    api: MarketDataSource,
    db: Storage,
    trader: PaperTrader,
    s: Settings,
) -> int:
    """Voer de beste gelijktijdige trendkandidaten uit tot de PAPER-slots vol zijn.

    Een kandidaat met te hoge spread of ander executionblok verbruikt geen slot;
    de volgende gerangschikte kandidaat krijgt dan de kans. De functie retourneert
    het aantal entry-book fouten zodat de data-health niet ten onrechte READY wordt.
    """
    ranked = sorted(candidates, key=lambda c: (-c.score, c.market))
    slots = max(0, s.max_open_positions - len(db.all_positions()))
    entry_failures = 0

    for rank, candidate in enumerate(ranked, start=1):
        metrics = {
            **candidate.decision.metrics,
            'rank_score': candidate.score,
            'candidate_rank': rank,
            'candidate_count': len(ranked),
        }

        if slots <= 0:
            decision = Decision('SKIP', 'rank_buiten_top_slots', metrics)
            db.save_decision(candidate.market, candidate.candle_ts, decision)
            logger.info(
                '%s SKIP | rank_buiten_top_slots | rank=%s score=%.4f',
                candidate.market,
                rank,
                candidate.score,
            )
            continue

        try:
            book = api.book(candidate.market)
        except Exception as exc:
            entry_failures += 1
            decision = Decision(
                'SKIP',
                'entry_book_error',
                {**metrics, 'error_type': type(exc).__name__},
            )
            db.save_decision(candidate.market, candidate.candle_ts, decision)
            logger.exception('%s: orderboek voor gerangschikte entry mislukt', candidate.market)
            continue

        allowed, block_reason = trader.can_open(candidate.market, book)
        if not allowed:
            decision = Decision(
                'SKIP',
                block_reason,
                {**metrics, 'spread_pct': book.spread_pct},
            )
            db.save_decision(candidate.market, candidate.candle_ts, decision)
            logger.info('%s SKIP | %s | rank=%s', candidate.market, block_reason, rank)
            continue

        event = trader.open_long(candidate.market, book, candidate.candle_ts)
        if event is None:
            decision = Decision('SKIP', 'open_mislukt', metrics)
            db.save_decision(candidate.market, candidate.candle_ts, decision)
            logger.error('%s SKIP | open_mislukt | rank=%s', candidate.market, rank)
            continue

        decision = Decision('BUY', candidate.decision.reason, {**metrics, 'spread_pct': book.spread_pct})
        db.save_decision(candidate.market, candidate.candle_ts, decision)
        slots -= 1
        logger.info(
            '%s OPEN @ %.8f | rank=%s/%s score=%.4f spread=%.4f%%',
            candidate.market,
            event.price,
            rank,
            len(ranked),
            candidate.score,
            book.spread_pct,
        )

    return entry_failures


def run_trend_cycle(
    api: MarketDataSource,
    db: Storage,
    strategy: TrendMomentumStrategy,
    trader: PaperTrader,
    s: Settings,
    markets: list[str],
) -> tuple[int, int, str]:
    ok = 0
    failed = 0
    last_error = ''
    candidates: list[TrendCandidate] = []

    for market in markets:
        if STOP:
            break
        try:
            candidate = scan_market_for_candidate(market, api, db, strategy, trader, s)
            if candidate is not None:
                candidates.append(candidate)
            ok += 1
        except Exception as exc:
            failed += 1
            last_error = f'{market}: {type(exc).__name__}: {exc}'
            logger.exception('%s: trend-scan mislukt', market)

    if candidates and not STOP:
        entry_failures = execute_ranked_candidates(candidates, api, db, trader, s)
        if entry_failures:
            ok = max(0, ok - entry_failures)
            failed += entry_failures
            last_error = f'{entry_failures} gerangschikte entry-orderboek request(s) mislukt'

    return ok, failed, last_error


def main() -> int:
    parser = argparse.ArgumentParser(description='CryptoBot Clean-Room Strategy B v2 - ranked trend/momentum PAPER')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--readiness', action='store_true')
    args = parser.parse_args()

    primary_s = Settings()
    primary_s.validate()
    core.setup_logging(primary_s.log_level)
    trend_s = build_trend_settings(primary_s)
    trend_db = Storage(trend_s.db_path, trend_s.paper_start_eur)

    try:
        if args.status:
            print('=== STRATEGY B v2 | RANKED TREND MOMENTUM ===')
            print_status(trend_db, trend_s)
            return 0
        if args.report:
            print('=== STRATEGY B v2 | RANKED TREND MOMENTUM ===')
            print_report(trend_db, trend_s)
            return 0
        if args.readiness:
            print('=== STRATEGY B v2 | RANKED TREND MOMENTUM ===')
            print_readiness(trend_db, trend_s)
            return 0

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        core.STOP = False

        markets = shared_universe(primary_s, trend_db, args.once)
        if markets is None:
            return 2 if args.once else 0

        api = BitvavoPublic(
            trend_s.api_base_url,
            trend_s.request_timeout_seconds,
            trend_s.request_retries,
        )
        strategy = TrendMomentumStrategy(trend_s)
        trader = PaperTrader(trend_s, trend_db, entry_reason='trend_breakout')

        logger.info(
            'gestart | STRATEGY B v2 RANKED TREND | PAPER ONLY | interval=%s | universe=%s | max_open=%s | db=%s',
            trend_s.interval,
            ','.join(markets),
            trend_s.max_open_positions,
            trend_s.db_path,
        )

        loop = not args.once and trend_s.loop_enabled
        consecutive_total_failures = 0
        while True:
            ok, failed, last_error = run_trend_cycle(
                api, trend_db, strategy, trader, trend_s, markets
            )
            if ok == len(markets) and failed == 0:
                consecutive_total_failures = 0
                trend_db.set_data_health(
                    'READY',
                    f'volledige ranked-trend-cyclus ok={ok}',
                )
            elif ok > 0:
                consecutive_total_failures = 0
                trend_db.set_data_health(
                    'PARTIAL',
                    f'ranked-trend-cyclus ok={ok} failed={failed}; {last_error}',
                )
            else:
                consecutive_total_failures += 1
                trend_db.set_data_health(
                    'DEGRADED',
                    last_error or f'alle {failed} trend-marktcycli mislukt',
                )

            if consecutive_total_failures >= trend_s.max_consecutive_failed_cycles:
                logger.error(
                    'trend-marktdata volledig onbereikbaar gedurende %s cycli',
                    consecutive_total_failures,
                )
                core.log_public_probe(api)
                consecutive_total_failures = 0

            if not loop or STOP:
                return 0 if ok > 0 else 2

            for _ in range(trend_s.poll_seconds):
                if STOP:
                    break
                time.sleep(1)
    finally:
        trend_db.close()
        logger.info('gestopt')


if __name__ == '__main__':
    sys.exit(main())
