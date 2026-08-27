from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from bitvavo_public import INTERVAL_MS
from config import Settings
from missed_trade_audit import (
    _connect,
    _connect_source_readonly,
    _import_new_skips,
    _init_audit_schema,
    _print_strategy_report,
    _source_signature,
    _state_get,
    _state_set,
    _update_pending,
    audit_db_path,
    estimated_roundtrip_cost_floor_pct,
    print_audit_report,
    update_missed_trade_audit,
)

logger = logging.getLogger('cryptobot_missed_audit_all')
STRATEGY_C = 'C_CONTINUATION_V1'


def continuation_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_continuation_v1{suffix}'))


def update_continuation_audit(settings: Settings) -> dict[str, int | str]:
    if settings.interval not in INTERVAL_MS:
        raise ValueError(f'interval niet ondersteund voor audit: {settings.interval}')

    source_path = continuation_db_path(settings.db_path)
    out_path = audit_db_path(settings.db_path)
    if not Path(source_path).exists():
        return {'sources': 0, 'imported': 0, 'updated': 0, 'db_path': out_path}

    interval_ms = INTERVAL_MS[settings.interval]
    audit = _connect(out_path)
    _init_audit_schema(audit)
    source = _connect_source_readonly(source_path)

    imported = 0
    updated = 0
    try:
        decision_sig, max_candle_ts = _source_signature(source, settings.interval)
        old_decision_sig = _state_get(audit, f'decision_sig:{STRATEGY_C}', '')
        old_max_candle = int(
            _state_get(audit, f'max_candle:{STRATEGY_C}', '0') or 0
        )

        if decision_sig != old_decision_sig or max_candle_ts != old_max_candle:
            with audit:
                imported += _import_new_skips(source, audit, STRATEGY_C)
                updated += _update_pending(
                    source,
                    audit,
                    STRATEGY_C,
                    settings.interval,
                    interval_ms,
                )
                _state_set(audit, f'decision_sig:{STRATEGY_C}', decision_sig)
                _state_set(audit, f'max_candle:{STRATEGY_C}', max_candle_ts)
    finally:
        source.close()
        audit.close()

    return {
        'sources': 1,
        'imported': imported,
        'updated': updated,
        'db_path': out_path,
    }


def update_all_missed_trade_audit(settings: Settings) -> dict[str, int | str]:
    base = update_missed_trade_audit(settings)
    continuation = update_continuation_audit(settings)
    return {
        'sources': int(base['sources']) + int(continuation['sources']),
        'imported': int(base['imported']) + int(continuation['imported']),
        'updated': int(base['updated']) + int(continuation['updated']),
        'db_path': str(base['db_path']),
    }


def print_all_audit_report(settings: Settings) -> None:
    print_audit_report(settings)

    path = audit_db_path(settings.db_path)
    if not Path(path).exists():
        return

    conn = _connect(path)
    try:
        _print_strategy_report(
            conn,
            STRATEGY_C,
            'STRATEGIE C v1 | CONTINUATION SKIPS',
            estimated_roundtrip_cost_floor_pct(settings),
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Clean-Room missed-trade audit voor PAPER strategie A/B/C'
    )
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

    if args.report or args.once:
        result = update_all_missed_trade_audit(settings)
        if args.report:
            print_all_audit_report(settings)
        else:
            print(
                'MISSED-AUDIT-ABC '
                f"sources={result['sources']} imported={result['imported']} "
                f"updated={result['updated']} db={result['db_path']}"
            )
        return 0

    logger.info(
        'gestart | READ-ONLY PAPER MISSED-TRADE AUDIT A/B/C | interval=%s',
        settings.interval,
    )
    while True:
        try:
            result = update_all_missed_trade_audit(settings)
            if int(result['imported']) or int(result['updated']):
                logger.info(
                    'bijgewerkt | sources=%s imported=%s updated=%s | db=%s',
                    result['sources'],
                    result['imported'],
                    result['updated'],
                    result['db_path'],
                )
        except Exception:
            logger.exception('audit-cyclus mislukt; volgende cyclus probeert opnieuw')
        time.sleep(settings.poll_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
