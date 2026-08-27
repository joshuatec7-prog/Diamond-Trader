from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import continuation_main as engine
import main as core
from bitvavo_public import BitvavoPublic
from config import Settings
from continuation_strategy import TrendContinuationStrategy
from profit_protect_trader import ProfitProtectPaperTrader
from readiness import print_readiness
from report import print_report
from status import print_status
from storage import Storage

logger = logging.getLogger('cryptobot_cleanroom_continuation_v3')
CONTINUATION_MAX_OPEN_POSITIONS = 3
CONTINUATION_TAKE_PROFIT_PCT = 3.5
PROFIT_TRIGGER_PCT = 1.50
LOCK_PROFIT_EUR = 0.50
TRAIL_DISTANCE_PCT = 0.75


def continuation_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_continuation_v3{suffix}'))


def build_continuation_settings(primary_s: Settings) -> Settings:
    continuation_s = replace(
        primary_s,
        db_path=continuation_db_path(primary_s.db_path),
        max_open_positions=CONTINUATION_MAX_OPEN_POSITIONS,
        take_profit_pct=CONTINUATION_TAKE_PROFIT_PCT,
    )
    continuation_s.validate()
    return continuation_s


def build_trader(s: Settings, db: Storage) -> ProfitProtectPaperTrader:
    return ProfitProtectPaperTrader(
        s,
        db,
        entry_reason='trend_pullback_continuation',
        trigger_pct=PROFIT_TRIGGER_PCT,
        lock_profit_eur=LOCK_PROFIT_EUR,
        trail_distance_pct=TRAIL_DISTANCE_PCT,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description='CryptoBot Clean-Room Strategy C v3 - continuation + profit protect PAPER'
    )
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--readiness', action='store_true')
    args = parser.parse_args()

    primary_s = Settings()
    primary_s.validate()
    core.setup_logging(primary_s.log_level)
    continuation_s = build_continuation_settings(primary_s)
    continuation_db = Storage(
        continuation_s.db_path,
        continuation_s.paper_start_eur,
    )

    try:
        if args.status:
            print('=== STRATEGY C v3 | PULLBACK CONTINUATION | TP 3.5% | PROFIT PROTECT ===')
            print_status(continuation_db, continuation_s)
            return 0
        if args.report:
            print('=== STRATEGY C v3 | PULLBACK CONTINUATION | TP 3.5% | PROFIT PROTECT ===')
            print_report(continuation_db, continuation_s)
            return 0
        if args.readiness:
            print('=== STRATEGY C v3 | PULLBACK CONTINUATION | TP 3.5% | PROFIT PROTECT ===')
            print_readiness(continuation_db, continuation_s)
            return 0

        engine.STOP = False
        core.STOP = False
        signal.signal(signal.SIGTERM, engine._stop)
        signal.signal(signal.SIGINT, engine._stop)

        markets = engine.shared_universe(primary_s, continuation_db, args.once)
        if markets is None:
            return 2 if args.once else 0

        api = BitvavoPublic(
            continuation_s.api_base_url,
            continuation_s.request_timeout_seconds,
            continuation_s.request_retries,
        )
        strategy = TrendContinuationStrategy(continuation_s)
        trader = build_trader(continuation_s, continuation_db)

        logger.info(
            'gestart | STRATEGY C v3 | PAPER ONLY | interval=%s | universe=%s | '
            'max_open=%s | stop=%.2f%% | take=%.2f%% | protect=%.2f%%->€%.2f | '
            'trail=%.2f%% | db=%s',
            continuation_s.interval,
            ','.join(markets),
            continuation_s.max_open_positions,
            continuation_s.stop_loss_pct,
            continuation_s.take_profit_pct,
            PROFIT_TRIGGER_PCT,
            LOCK_PROFIT_EUR,
            TRAIL_DISTANCE_PCT,
            continuation_s.db_path,
        )

        loop = not args.once and continuation_s.loop_enabled
        consecutive_total_failures = 0

        while True:
            ok, failed, last_error = engine.run_continuation_cycle(
                api,
                continuation_db,
                strategy,
                trader,
                continuation_s,
                markets,
            )

            if ok == len(markets) and failed == 0:
                consecutive_total_failures = 0
                continuation_db.set_data_health(
                    'READY', f'volledige continuation-v3-cyclus ok={ok}'
                )
            elif ok > 0:
                consecutive_total_failures = 0
                continuation_db.set_data_health(
                    'PARTIAL',
                    f'continuation-v3-cyclus ok={ok} failed={failed}; {last_error}',
                )
            else:
                consecutive_total_failures += 1
                continuation_db.set_data_health(
                    'DEGRADED',
                    last_error or f'alle {failed} continuation-v3-marktcycli mislukt',
                )

            if consecutive_total_failures >= continuation_s.max_consecutive_failed_cycles:
                logger.error(
                    'continuation-v3-marktdata volledig onbereikbaar gedurende %s cycli',
                    consecutive_total_failures,
                )
                core.log_public_probe(api)
                consecutive_total_failures = 0

            if not loop or engine.STOP:
                return 0 if ok > 0 else 2

            for _ in range(continuation_s.poll_seconds):
                if engine.STOP:
                    break
                time.sleep(1)
    finally:
        continuation_db.close()
        logger.info('gestopt')


if __name__ == '__main__':
    sys.exit(main())
