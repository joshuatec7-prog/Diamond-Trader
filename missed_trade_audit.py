from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import time
from pathlib import Path
from statistics import fmean, median
from typing import Any

from bitvavo_public import INTERVAL_MS
from config import Settings


logger = logging.getLogger('cryptobot_missed_audit')
STRATEGY_A = 'A_MEAN_REVERSION'
STRATEGY_B = 'B_TREND_V2'
HORIZON_BARS = (
    ('r15m_pct', '15m', 1),
    ('r1h_pct', '1h', 4),
    ('r4h_pct', '4h', 16),
    ('r12h_pct', '12h', 48),
)


def trend_v2_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_trend_v2{suffix}'))


def audit_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_missed_audit{suffix}'))


def estimated_roundtrip_cost_floor_pct(s: Settings) -> float:
    """Baseline kosten zonder spread: fee + slippage aan beide kanten."""
    return 2.0 * (s.taker_fee_pct + s.slippage_pct)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _connect_source_readonly(path: str) -> sqlite3.Connection:
    absolute = Path(path).resolve().as_posix()
    conn = sqlite3.connect(f'file:{absolute}?mode=ro', uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _init_audit_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        '''
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS skip_audit (
            strategy TEXT NOT NULL,
            market TEXT NOT NULL,
            decision_ts INTEGER NOT NULL,
            reason TEXT NOT NULL,
            reference_close REAL NOT NULL,
            r15m_pct REAL,
            r1h_pct REAL,
            r4h_pct REAL,
            r12h_pct REAL,
            mfe12h_pct REAL,
            mae12h_pct REAL,
            imported_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY (strategy, market, decision_ts)
        );

        CREATE INDEX IF NOT EXISTS idx_skip_audit_strategy_pending
        ON skip_audit(strategy, r12h_pct, decision_ts);
        '''
    )
    conn.commit()


def _state_get(conn: sqlite3.Connection, key: str, default: str = '') -> str:
    row = conn.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return default if row is None else str(row['value'])


def _state_set(conn: sqlite3.Connection, key: str, value: str | int | float) -> None:
    conn.execute(
        '''INSERT INTO state(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
        (key, str(value)),
    )


def _source_signature(source: sqlite3.Connection, interval: str) -> tuple[str, int]:
    row = source.execute(
        "SELECT COUNT(*) n, COALESCE(MAX(timestamp_ms),0) mx FROM decisions"
    ).fetchone()
    decision_sig = f"{int(row['n'])}:{int(row['mx'])}"
    candle_row = source.execute(
        'SELECT COALESCE(MAX(timestamp_ms),0) mx FROM candles WHERE interval=?',
        (interval,),
    ).fetchone()
    return decision_sig, int(candle_row['mx'])


def _import_new_skips(
    source: sqlite3.Connection,
    audit: sqlite3.Connection,
    strategy: str,
) -> int:
    row = audit.execute(
        'SELECT COALESCE(MAX(decision_ts),0) mx FROM skip_audit WHERE strategy=?',
        (strategy,),
    ).fetchone()
    max_imported_ts = int(row['mx'])
    rows = source.execute(
        '''SELECT market,timestamp_ms,reason,metrics_json
           FROM decisions
           WHERE action='SKIP' AND timestamp_ms>=?
           ORDER BY timestamp_ms,market''',
        (max_imported_ts,),
    ).fetchall()

    now_ms = time.time_ns() // 1_000_000
    imported = 0
    for row in rows:
        try:
            metrics = json.loads(str(row['metrics_json']))
            reference_close = float(metrics.get('close'))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not math.isfinite(reference_close) or reference_close <= 0:
            continue
        cur = audit.execute(
            '''INSERT OR IGNORE INTO skip_audit(
                   strategy,market,decision_ts,reason,reference_close,
                   imported_at_ms,updated_at_ms
               ) VALUES(?,?,?,?,?,?,?)''',
            (
                strategy,
                str(row['market']),
                int(row['timestamp_ms']),
                str(row['reason']),
                reference_close,
                now_ms,
                now_ms,
            ),
        )
        imported += max(0, cur.rowcount)
    return imported


def _update_pending(
    source: sqlite3.Connection,
    audit: sqlite3.Connection,
    strategy: str,
    interval: str,
    interval_ms: int,
) -> int:
    pending = audit.execute(
        '''SELECT market,decision_ts,reference_close,
                  r15m_pct,r1h_pct,r4h_pct,r12h_pct
           FROM skip_audit
           WHERE strategy=? AND r12h_pct IS NULL
           ORDER BY market,decision_ts''',
        (strategy,),
    ).fetchall()
    if not pending:
        return 0

    by_market: dict[str, list[sqlite3.Row]] = {}
    for row in pending:
        by_market.setdefault(str(row['market']), []).append(row)

    updated = 0
    now_ms = time.time_ns() // 1_000_000

    for market, records in by_market.items():
        min_ts = min(int(r['decision_ts']) for r in records) + interval_ms
        max_ts = max(int(r['decision_ts']) for r in records) + 48 * interval_ms
        candle_rows = source.execute(
            '''SELECT timestamp_ms,high,low,close
               FROM candles
               WHERE market=? AND interval=? AND timestamp_ms BETWEEN ? AND ?
               ORDER BY timestamp_ms''',
            (market, interval, min_ts, max_ts),
        ).fetchall()
        candles = {
            int(c['timestamp_ms']): (
                float(c['high']),
                float(c['low']),
                float(c['close']),
            )
            for c in candle_rows
        }

        for record in records:
            decision_ts = int(record['decision_ts'])
            reference = float(record['reference_close'])
            assignments: dict[str, float] = {}

            for col, _label, bars in HORIZON_BARS:
                if record[col] is not None:
                    continue
                target_ts = decision_ts + bars * interval_ms
                candle = candles.get(target_ts)
                if candle is None:
                    continue
                close = candle[2]
                if math.isfinite(close) and close > 0:
                    assignments[col] = ((close / reference) - 1.0) * 100.0

            if record['r12h_pct'] is None and 'r12h_pct' in assignments:
                target_ts = decision_ts + 48 * interval_ms
                window = [
                    values
                    for ts, values in candles.items()
                    if decision_ts < ts <= target_ts
                ]
                if window:
                    high = max(v[0] for v in window)
                    low = min(v[1] for v in window)
                    if math.isfinite(high) and math.isfinite(low) and high > 0 and low > 0:
                        assignments['mfe12h_pct'] = ((high / reference) - 1.0) * 100.0
                        assignments['mae12h_pct'] = ((low / reference) - 1.0) * 100.0

            if not assignments:
                continue

            set_sql = ', '.join(f'{key}=?' for key in assignments)
            values: list[Any] = list(assignments.values())
            values.extend([now_ms, strategy, market, decision_ts])
            audit.execute(
                f'''UPDATE skip_audit
                    SET {set_sql}, updated_at_ms=?
                    WHERE strategy=? AND market=? AND decision_ts=?''',
                values,
            )
            updated += 1

    return updated


def update_missed_trade_audit(
    settings: Settings,
    trend_path: str | None = None,
) -> dict[str, int | str]:
    """Werk de read-only missed-trade audit bij voor strategie A en B.

    De strategieën en hun databases worden niet gewijzigd. Alleen de aparte
    auditdatabase krijgt afgeleide resultaten van reeds genomen SKIP-besluiten.
    """
    if settings.interval not in INTERVAL_MS:
        raise ValueError(f'interval niet ondersteund voor audit: {settings.interval}')

    interval_ms = INTERVAL_MS[settings.interval]
    trend_path = trend_path or trend_v2_db_path(settings.db_path)
    out_path = audit_db_path(settings.db_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    audit = _connect(out_path)
    _init_audit_schema(audit)

    imported_total = 0
    updated_total = 0
    processed_sources = 0

    try:
        for strategy, source_path in (
            (STRATEGY_A, settings.db_path),
            (STRATEGY_B, trend_path),
        ):
            if not Path(source_path).exists():
                continue

            source = _connect_source_readonly(source_path)
            try:
                decision_sig, max_candle_ts = _source_signature(source, settings.interval)
                old_decision_sig = _state_get(audit, f'decision_sig:{strategy}', '')
                old_max_candle = int(_state_get(audit, f'max_candle:{strategy}', '0') or 0)

                if decision_sig == old_decision_sig and max_candle_ts == old_max_candle:
                    processed_sources += 1
                    continue

                with audit:
                    imported_total += _import_new_skips(source, audit, strategy)
                    updated_total += _update_pending(
                        source,
                        audit,
                        strategy,
                        settings.interval,
                        interval_ms,
                    )
                    _state_set(audit, f'decision_sig:{strategy}', decision_sig)
                    _state_set(audit, f'max_candle:{strategy}', max_candle_ts)
                processed_sources += 1
            finally:
                source.close()
    finally:
        audit.close()

    return {
        'sources': processed_sources,
        'imported': imported_total,
        'updated': updated_total,
        'db_path': out_path,
    }


def _pct(value: float) -> str:
    return f'{value:+.2f}%'


def _print_strategy_report(
    conn: sqlite3.Connection,
    strategy: str,
    label: str,
    cost_floor_pct: float,
) -> None:
    total = int(
        conn.execute(
            'SELECT COUNT(*) n FROM skip_audit WHERE strategy=?',
            (strategy,),
        ).fetchone()['n']
    )
    print()
    print(f'--- {label} ---')
    print(f'SKIPS GEREGISTREERD : {total}')
    if total == 0:
        print('Nog geen auditdata')
        return

    longest_available: tuple[str, str] | None = None

    for col, horizon_label, _bars in HORIZON_BARS:
        rows = conn.execute(
            f'SELECT {col} value FROM skip_audit WHERE strategy=? AND {col} IS NOT NULL',
            (strategy,),
        ).fetchall()
        values = [float(r['value']) for r in rows]
        if not values:
            print(f'{horizon_label:>4} VOLWASSEN     : 0')
            continue

        longest_available = (col, horizon_label)
        positive = 100.0 * sum(v > 0 for v in values) / len(values)
        cost_beating = 100.0 * sum(v > cost_floor_pct for v in values) / len(values)
        print(
            f'{horizon_label:>4} VOLWASSEN     : {len(values):4d} | '
            f'gem {_pct(fmean(values))} | mediaan {_pct(median(values))} | '
            f'>0 {positive:5.1f}% | >kosten {cost_beating:5.1f}%'
        )

    reason_rows = conn.execute(
        '''SELECT reason,COUNT(*) n,AVG(r4h_pct) avg_return,
                  100.0*SUM(CASE WHEN r4h_pct>? THEN 1 ELSE 0 END)/COUNT(*) cost_hit
           FROM skip_audit
           WHERE strategy=? AND r4h_pct IS NOT NULL
           GROUP BY reason
           ORDER BY avg_return DESC,n DESC''',
        (cost_floor_pct, strategy),
    ).fetchall()
    if reason_rows:
        print('4H PER SKIP-REDEN :')
        for row in reason_rows[:8]:
            print(
                f"  {str(row['reason']):24} n={int(row['n']):4d} | "
                f"gem {_pct(float(row['avg_return']))} | "
                f">kosten {float(row['cost_hit']):5.1f}%"
            )

    if longest_available is not None:
        col, horizon_label = longest_available
        top_rows = conn.execute(
            f'''SELECT market,reason,{col} value
                FROM skip_audit
                WHERE strategy=? AND {col} IS NOT NULL
                ORDER BY {col} DESC
                LIMIT 5''',
            (strategy,),
        ).fetchall()
        print(f'TOP GEMISTE BEWEGINGEN ({horizon_label}) :')
        for row in top_rows:
            print(
                f"  {str(row['market']):14} | {_pct(float(row['value'])):>8} | "
                f"{str(row['reason'])}"
            )

    mfe_rows = conn.execute(
        '''SELECT mfe12h_pct,mae12h_pct
           FROM skip_audit
           WHERE strategy=? AND mfe12h_pct IS NOT NULL AND mae12h_pct IS NOT NULL''',
        (strategy,),
    ).fetchall()
    if mfe_rows:
        mfe = [float(r['mfe12h_pct']) for r in mfe_rows]
        mae = [float(r['mae12h_pct']) for r in mfe_rows]
        print(
            f'12H PAD           : n={len(mfe)} | '
            f'gem max-up {_pct(fmean(mfe))} | gem max-down {_pct(fmean(mae))}'
        )


def print_audit_report(settings: Settings) -> None:
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
            'STRATEGIE B v2 | TREND SKIPS',
            cost_floor,
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Clean-Room missed-trade audit - read-only analyse van PAPER SKIPs'
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
        result = update_missed_trade_audit(settings)
        if args.report:
            print_audit_report(settings)
        else:
            print(
                'MISSED-AUDIT '
                f"sources={result['sources']} imported={result['imported']} "
                f"updated={result['updated']} db={result['db_path']}"
            )
        return 0

    logger.info('gestart | READ-ONLY PAPER MISSED-TRADE AUDIT | interval=%s', settings.interval)
    while True:
        try:
            result = update_missed_trade_audit(settings)
            if int(result['imported']) or int(result['updated']):
                logger.info(
                    'bijgewerkt | imported=%s updated=%s | db=%s',
                    result['imported'],
                    result['updated'],
                    result['db_path'],
                )
        except Exception:
            # Research/audit mag nooit de PAPER-strategieën beïnvloeden.
            logger.exception('audit-cyclus mislukt; volgende cyclus probeert opnieuw')
        time.sleep(settings.poll_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
