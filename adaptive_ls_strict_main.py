from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import adaptive_ls_main as base
import main as core
from adaptive_ls_strict_strategy import StrictAdaptiveLongShortStrategy
from adaptive_ls_trader import AdaptiveLongShortPaperTrader
from bitvavo_public import BitvavoPublic
from config import Settings
from readiness import print_readiness
from report import print_report
from status import print_status
from storage import Storage

logger = logging.getLogger('cryptobot_cleanroom_adaptive_trend_v2_strict')
STOP = False
MAX_OPEN = 3


def strict_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_adaptive_trend_v2_strict{suffix}'))


def build_settings(primary: Settings) -> Settings:
    out = replace(
        primary,
        db_path=strict_db_path(primary.db_path),
        max_open_positions=MAX_OPEN,
        stop_loss_pct=1.25,
        take_profit_pct=30.0,
    )
    out.validate()
    return out


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    base.STOP = True
    core.STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='CryptoBot D v2 STRICT SHORT research runner PAPER ONLY'
    )
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--readiness', action='store_true')
    args = parser.parse_args()

    primary = Settings()
    primary.validate()
    core.setup_logging(primary.log_level)
    base.logger = logger
    base.STOP = False

    s = build_settings(primary)
    db = Storage(s.db_path, s.paper_start_eur)
    trader = AdaptiveLongShortPaperTrader(s, db)

    try:
        header = '=== STRATEGY D v2S | STRICT SHORT RESEARCH | ATR RUNNER | PAPER ONLY ==='
        if args.status:
            print(header)
            print_status(db, s)
            base._print_extra(db, trader)
            return 0
        if args.report:
            print(header)
            print_report(db, s)
            base._print_extra(db, trader)
            return 0
        if args.readiness:
            print(header)
            print_readiness(db, s)
            return 0

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        markets = base._universe(primary, db, args.once)
        if markets is None:
            return 2 if args.once else 0

        for p in db.all_positions():
            if trader.position_side(p.market) not in {'LONG', 'SHORT'}:
                raise RuntimeError(f'{p.market}: bestaande D v2S positie zonder side')

        api = BitvavoPublic(
            s.api_base_url,
            s.request_timeout_seconds,
            s.request_retries,
        )
        strategy = StrictAdaptiveLongShortStrategy(s)
        logger.info(
            'gestart | D v2S STRICT SHORT PAPER | bear>=%.0f%% | breakdown-only | max_open=%s | db=%s',
            strategy.STRICT_BEAR_BREADTH_PCT,
            s.max_open_positions,
            s.db_path,
        )

        loop = not args.once and s.loop_enabled
        consecutive = 0
        while True:
            ok, failed, last = base.run_cycle(api, db, strategy, trader, s, markets)
            if ok == len(markets) and failed == 0:
                consecutive = 0
                db.set_data_health('READY', f'volledige adaptive-v2-strict-cyclus ok={ok}')
            elif ok > 0:
                consecutive = 0
                db.set_data_health('PARTIAL', f'adaptive-v2-strict-cyclus ok={ok} failed={failed}; {last}')
            else:
                consecutive += 1
                db.set_data_health('DEGRADED', last or f'alle {failed} D v2S marktcycli mislukt')
            if consecutive >= s.max_consecutive_failed_cycles:
                logger.error('D v2S marktdata volledig onbereikbaar gedurende %s cycli', consecutive)
                consecutive = 0
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
