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
from readiness import print_readiness
from report import print_report
from staged_runner_trader import StagedRunnerPaperTrader
from status import print_status
from storage import Storage
from trend_strategy import TrendMomentumStrategy

logger = logging.getLogger('cryptobot_cleanroom_trend_v6')
TREND_MAX_OPEN_POSITIONS = 3
RUNNER_REFERENCE_PCT = 3.5
LOCK_TRIGGER_PCT = 1.50
LOCK_PROFIT_EUR = 0.50
WIDE_TRIGGER_PCT = 3.00
WIDE_TRAIL_PCT = 1.25
TIGHT_TRIGGER_PCT = 6.00
TIGHT_TRAIL_PCT = 0.75


def trend_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_trend_v6{suffix}'))


def build_trend_settings(primary_s: Settings) -> Settings:
    trend_s = replace(
        primary_s,
        db_path=trend_db_path(primary_s.db_path),
        max_open_positions=TREND_MAX_OPEN_POSITIONS,
        take_profit_pct=RUNNER_REFERENCE_PCT,
    )
    trend_s.validate()
    return trend_s


def build_trader(s: Settings, db: Storage) -> StagedRunnerPaperTrader:
    return StagedRunnerPaperTrader(
        s,
        db,
        entry_reason='trend_breakout',
        lock_trigger_pct=LOCK_TRIGGER_PCT,
        lock_profit_eur=LOCK_PROFIT_EUR,
        wide_trigger_pct=WIDE_TRIGGER_PCT,
        wide_trail_pct=WIDE_TRAIL_PCT,
        tight_trigger_pct=TIGHT_TRIGGER_PCT,
        tight_trail_pct=TIGHT_TRAIL_PCT,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description='CryptoBot Clean-Room Strategy B v6 - staged trend runner PAPER'
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
            print('=== STRATEGY B v6 | RANKED TREND | STAGED RUNNER | GEEN HARDE TP ===')
            print_status(trend_db, trend_s)
            return 0
        if args.report:
            print('=== STRATEGY B v6 | RANKED TREND | STAGED RUNNER | GEEN HARDE TP ===')
            print_report(trend_db, trend_s)
            return 0
        if args.readiness:
            print('=== STRATEGY B v6 | RANKED TREND | STAGED RUNNER | GEEN HARDE TP ===')
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
            'gestart | STRATEGY B v6 STAGED RUNNER | PAPER ONLY | interval=%s | '
            'universe=%s | max_open=%s | stop=%.2f%% | hard_take=UIT | '
            'lock=%.2f%%->€%.2f | wide=%.2f%%/%.2f%% | tight=%.2f%%/%.2f%% | db=%s',
            trend_s.interval,
            ','.join(markets),
            trend_s.max_open_positions,
            trend_s.stop_loss_pct,
            LOCK_TRIGGER_PCT,
            LOCK_PROFIT_EUR,
            WIDE_TRIGGER_PCT,
            WIDE_TRAIL_PCT,
            TIGHT_TRIGGER_PCT,
            TIGHT_TRAIL_PCT,
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
                    'READY', f'volledige ranked-trend-v6-staged-runner-cyclus ok={ok}'
                )
            elif ok > 0:
                consecutive_total_failures = 0
                trend_db.set_data_health(
                    'PARTIAL',
                    f'ranked-trend-v6-staged-runner-cyclus ok={ok} failed={failed}; {last_error}',
                )
            else:
                consecutive_total_failures += 1
                trend_db.set_data_health(
                    'DEGRADED',
                    last_error or f'alle {failed} trend-v6-marktcycli mislukt',
                )

            if consecutive_total_failures >= trend_s.max_consecutive_failed_cycles:
                logger.error(
                    'trend-v6-marktdata volledig onbereikbaar gedurende %s cycli',
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
