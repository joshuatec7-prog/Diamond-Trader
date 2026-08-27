from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from bitvavo_public import INTERVAL_MS
from config import Settings
from missed_trade_audit import (
    STRATEGY_A,
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
)

logger = logging.getLogger('cryptobot_missed_audit_all')
STRATEGY_B = 'B_TREND_V4'
STRATEGY_C = 'C_CONTINUATION_V3'


def trend_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_trend_v4{suffix}'))


def continuation_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_continuation_v3{suffix}'))


def _update_source(
    settings: Settings,
    strategy: str,
    source_path: str,
) -> dict[str, int | str]:
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
        old_decision_sig = _state_get(audit, f'decision_sig:{strategy}', '')
        old_max_candle = int(_state_get(audit, f'max_candle:{strategy}', '0') or 0)

        if decision_sig != old_decision_sig or max_candle_ts != old_max_candle:
            with audit:
                imported += _import_new_skips(source, audit, strategy)
                updated += _update_pending(
                    source,
                    audit,
                    strategy,
                    settings.interval,
                    interval_ms,
                )
                _state_set(audit, f'decision_sig:{strategy}', decision_sig)
                _state_set(audit, f'max_candle:{strategy}', max_candle_ts)
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
    if settings.interval not in INTERVAL_MS:
        raise ValueError(f'interval niet ondersteund voor audit: {settings.interval}')

    results = [
        _update_source(settings, STRATEGY_A, settings.db_path),
        _update_source(settings, STRATEGY_B, trend_db_path(settings.db_path)),
        _update_source(settings, STRATEGY_C, continuation_db_path(settings.db_path)),
    ]

    return {
        'sources': sum(int(r['sources']) for r in results),
        'imported': sum(int(r['imported']) for r in results),
        'updated': sum(int(r['updated']) for r in results),
        'db_path': audit_db_path(settings.db_path),
    }


def print_all_audit_report(settings: Settings) -> None:
    path = audit_db_path(settings.db_path)
    cost_floor = estimated_roundtrip_cost_floor_pct(settings)

    print('=== MISSED-TRADE AUDIT | READ-ONLY PAPER RESEARCH ===')
    print(f'AUDIT DB        : {path}')
    print(f'INTERVAL        : {settings.interval}')
    print(f'KOSTENVLOER     : {cost_floor:.2f}% + werkelijke spread')
    print('HORIZONS        : 15m, 1h, 4h, 12h')
    print('LET OP          : >kosten is marktbeweging, geen bewezen uitvoerbare winst')

    if not Path(path).exists():
        print('STATUS          : nog geen auditdatabase')
        return

    conn = _connect(path)
    try:
        _print_strategy_report(
            conn,
            STRATEGY_A,
            'STRATEGIE A | MEAN REVERSION SKIPS',
            cost_floor,
        )
        _print_strategy_report(
            conn,
            STRATEGY_B,
            'STRATEGIE B v4 | TREND + PROFIT PROTECT SKIPS',
            cost_floor,
        )
        _print_strategy_report(
            conn,
            STRATEGY_C,
            'STRATEGIE C v3 | CONTINUATION + PROFIT PROTECT SKIPS',
            cost_floor,
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Clean-Room missed-trade audit voor actuele PAPER strategie A/B/C'
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
        'gestart | READ-ONLY PAPER MISSED-TRADE AUDIT A/Bv4/Cv3 | interval=%s',
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
