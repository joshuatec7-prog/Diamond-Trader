from __future__ import annotations

import argparse
import logging
import time

import auto_research_controller as base
from adaptive_ls_main import adaptive_v2_db_path
from config import Settings

logger = logging.getLogger('cryptobot_auto_research_controller_d2')
STRATEGY_D2 = 'D_ADAPTIVE_LONG_SHORT_V2'


def _sources(settings: Settings) -> dict[str, str]:
    result = base._default_sources(settings)
    result[STRATEGY_D2] = adaptive_v2_db_path(settings.db_path)
    return result


def _recommendation(strategy: str, snapshot: dict, rebound_1h: dict) -> str:
    if strategy != STRATEGY_D2:
        return base._recommendation(strategy, snapshot, rebound_1h)
    if snapshot.get('missing'):
        return 'WACHT | bronbestand ontbreekt'
    if snapshot['data_status'] not in base.HEALTHY_DATA_STATUSES:
        return f"WACHT | data-status {snapshot['data_status']}"
    closed = int(snapshot['closed_trades'])
    if closed < 10:
        return f'VERZAMELEN | gesloten trades {closed}/10'
    if closed < 20:
        return f'VERZAMELEN | gesloten trades {closed}/20 voor LONG/SHORT beoordeling'
    pf = snapshot['profit_factor']
    if not snapshot['profit_factor_infinite'] and pf is not None and float(pf) < 0.80:
        return 'REVIEW | D v2 prestaties zwak; geen automatische wijziging'
    return 'HOLD | D v2 heeft 20+ trades; LONG/SHORT resultaat handmatig vergelijken'


def run_once(settings: Settings) -> dict:
    original = base._recommendation
    base._recommendation = _recommendation
    try:
        return base.run_once(settings, source_paths=_sources(settings))
    finally:
        base._recommendation = original


def main() -> int:
    parser = argparse.ArgumentParser(description='Research controller A/B/C/Dv1/Dv2 PAPER')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()
    settings = Settings()
    settings.validate()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    report_path = base.research_report_path(settings.db_path)
    if args.report:
        report = base.load_report(report_path)
        if report is None:
            print('AUTO RESEARCH CONTROLLER: nog geen uurrapport')
            return 0
        base.print_report(report)
        return 0
    if args.once:
        base.print_report(run_once(settings))
        return 0

    logger.info('gestart | %s | A/B/C/Dv1/Dv2 | auto_modify=NEE | auto_deploy=NEE', base.MODE)
    while True:
        started = time.monotonic()
        try:
            report = run_once(settings)
            summary = ' | '.join(f'{key}={value}' for key, value in report['recommendations'].items())
            logger.info('uurmeting gereed | %s | nieuwe_rebounds=%s', summary, report['inserted_rebound_measurements'])
        except Exception:
            logger.exception('uurmeting mislukt; bronstrategieën blijven onaangeroerd')
        elapsed = time.monotonic() - started
        time.sleep(max(60.0, base.RUN_INTERVAL_SECONDS - elapsed))


if __name__ == '__main__':
    raise SystemExit(main())
