from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import main as core
from bitvavo_public import BitvavoPublic
from config import Settings
from paper_trader import PaperTrader
from readiness import print_readiness
from report import print_report
from status import print_status
from storage import Storage
from trend_strategy import TrendMomentumStrategy

logger = logging.getLogger('cryptobot_cleanroom_trend')
STOP = False


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    core.STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def trend_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_trend{suffix}'))


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


def main() -> int:
    parser = argparse.ArgumentParser(description='CryptoBot Clean-Room Strategy B - trend/momentum PAPER')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--readiness', action='store_true')
    args = parser.parse_args()

    primary_s = Settings()
    primary_s.validate()
    core.setup_logging(primary_s.log_level)
    trend_s = replace(primary_s, db_path=trend_db_path(primary_s.db_path))
    trend_db = Storage(trend_s.db_path, trend_s.paper_start_eur)

    try:
        if args.status:
            print('=== STRATEGY B | TREND MOMENTUM ===')
            print_status(trend_db, trend_s)
            return 0
        if args.report:
            print('=== STRATEGY B | TREND MOMENTUM ===')
            print_report(trend_db, trend_s)
            return 0
        if args.readiness:
            print('=== STRATEGY B | TREND MOMENTUM ===')
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
            'gestart | STRATEGY B TREND | PAPER ONLY | interval=%s | universe=%s | db=%s',
            trend_s.interval, ','.join(markets), trend_s.db_path,
        )

        loop = not args.once and trend_s.loop_enabled
        consecutive_total_failures = 0
        while True:
            ok, failed, last_error = core.run_cycle(
                api, trend_db, strategy, trader, trend_s, markets
            )
            if ok == len(markets) and failed == 0:
                consecutive_total_failures = 0
                trend_db.set_data_health('READY', f'volledige trend-cyclus ok={ok}')
            elif ok > 0:
                consecutive_total_failures = 0
                trend_db.set_data_health('PARTIAL', f'trend-cyclus ok={ok} failed={failed}; {last_error}')
            else:
                consecutive_total_failures += 1
                trend_db.set_data_health('DEGRADED', last_error or f'alle {failed} trend-marktcycli mislukt')

            if consecutive_total_failures >= trend_s.max_consecutive_failed_cycles:
                logger.error('trend-marktdata volledig onbereikbaar gedurende %s cycli', consecutive_total_failures)
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
