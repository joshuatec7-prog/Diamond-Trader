from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import main as core
import trend_main as engine
from bitvavo_public import BitvavoPublic
from config import Settings
from profit_protect_trader import ProfitProtectPaperTrader
from readiness import print_readiness
from report import print_report
from status import print_status
from storage import Storage
from trend_strategy import TrendMomentumStrategy

logger = logging.getLogger('cryptobot_cleanroom_trend_v4')
TREND_MAX_OPEN_POSITIONS = 3
TREND_TAKE_PROFIT_PCT = 3.5
PROFIT_TRIGGER_PCT = 1.50
LOCK_PROFIT_EUR = 0.50
TRAIL_DISTANCE_PCT = 0.75


def trend_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_trend_v4{suffix}'))


def build_trend_settings(primary_s: Settings) -> Settings:
    trend_s = replace(
        primary_s,
        db_path=trend_db_path(primary_s.db_path),
        max_open_positions=TREND_MAX_OPEN_POSITIONS,
        take_profit_pct=TREND_TAKE_PROFIT_PCT,
    )
    trend_s.validate()
    return trend_s


def build_trader(s: Settings, db: Storage) -> ProfitProtectPaperTrader:
    return ProfitProtectPaperTrader(
        s,
        db,
        entry_reason='trend_breakout',
        trigger_pct=PROFIT_TRIGGER_PCT,
        lock_profit_eur=LOCK_PROFIT_EUR,
        trail_distance_pct=TRAIL_DISTANCE_PCT,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description='CryptoBot Clean-Room Strategy B v4 - trend + profit protect PAPER'
    )
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
            print('=== STRATEGY B v4 | RANKED TREND | TP 3.5% | PROFIT PROTECT ===')
            print_status(trend_db, trend_s)
            return 0
        if args.report:
            print('=== STRATEGY B v4 | RANKED TREND | TP 3.5% | PROFIT PROTECT ===')
            print_report(trend_db, trend_s)
            return 0
        if args.readiness:
            print('=== STRATEGY B v4 | RANKED TREND | TP 3.5% | PROFIT PROTECT ===')
            print_readiness(trend_db, trend_s)
            return 0

        engine.STOP = False
        core.STOP = False
        signal.signal(signal.SIGTERM, engine._stop)
        signal.signal(signal.SIGINT, engine._stop)

        markets = engine.shared_universe(primary_s, trend_db, args.once)
        if markets is None:
            return 2 if args.once else 0

        api = BitvavoPublic(
            trend_s.api_base_url,
            trend_s.request_timeout_seconds,
            trend_s.request_retries,
        )
        strategy = TrendMomentumStrategy(trend_s)
        trader = build_trader(trend_s, trend_db)

        logger.info(
            'gestart | STRATEGY B v4 | PAPER ONLY | interval=%s | universe=%s | '
            'max_open=%s | stop=%.2f%% | take=%.2f%% | protect=%.2f%%->€%.2f | '
            'trail=%.2f%% | db=%s',
            trend_s.interval,
            ','.join(markets),
            trend_s.max_open_positions,
            trend_s.stop_loss_pct,
            trend_s.take_profit_pct,
            PROFIT_TRIGGER_PCT,
            LOCK_PROFIT_EUR,
            TRAIL_DISTANCE_PCT,
            trend_s.db_path,
        )

        loop = not args.once and trend_s.loop_enabled
        consecutive_total_failures = 0
        while True:
            ok, failed, last_error = engine.run_trend_cycle(
                api, trend_db, strategy, trader, trend_s, markets
            )
            if ok == len(markets) and failed == 0:
                consecutive_total_failures = 0
                trend_db.set_data_health(
                    'READY', f'volledige ranked-trend-v4-cyclus ok={ok}'
                )
            elif ok > 0:
                consecutive_total_failures = 0
                trend_db.set_data_health(
                    'PARTIAL',
                    f'ranked-trend-v4-cyclus ok={ok} failed={failed}; {last_error}',
                )
            else:
                consecutive_total_failures += 1
                trend_db.set_data_health(
                    'DEGRADED',
                    last_error or f'alle {failed} trend-v4-marktcycli mislukt',
                )

            if consecutive_total_failures >= trend_s.max_consecutive_failed_cycles:
                logger.error(
                    'trend-v4-marktdata volledig onbereikbaar gedurende %s cycli',
                    consecutive_total_failures,
                )
                core.log_public_probe(api)
                consecutive_total_failures = 0

            if not loop or engine.STOP:
                return 0 if ok > 0 else 2

            for _ in range(trend_s.poll_seconds):
                if engine.STOP:
                    break
                time.sleep(1)
    finally:
        trend_db.close()
        logger.info('gestopt')


if __name__ == '__main__':
    sys.exit(main())
