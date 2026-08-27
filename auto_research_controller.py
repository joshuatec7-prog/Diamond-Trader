from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from audit_all import STRATEGY_B, STRATEGY_C, continuation_db_path, trend_db_path
from bitvavo_public import INTERVAL_MS
from config import Settings
from missed_trade_audit import STRATEGY_A, audit_db_path

logger = logging.getLogger('cryptobot_auto_research_controller')

RUN_INTERVAL_SECONDS = 3600
HORIZONS_MINUTES = (15, 60, 240)
MIN_CLOSED_FOR_REVIEW = 10
MIN_1H_REBOUNDS_FOR_REVIEW = 8
REBOUND_TRIGGER_PCT = 1.50
MODE = 'OBSERVE_ANALYSE_ONLY'


def research_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_research_controller{suffix}'))


def research_report_path(primary_path: str) -> str:
    p = Path(primary_path)
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_research_controller_report.json'))


def _connect_readonly(path: str) -> sqlite3.Connection:
    absolute = Path(path).resolve().as_posix()
    conn = sqlite3.connect(f'file:{absolute}?mode=ro', uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _connect_research(path: str) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=10000')
    conn.executescript(
        '''
        CREATE TABLE IF NOT EXISTS strategy_snapshots (
            bucket_ms INTEGER NOT NULL,
            strategy TEXT NOT NULL,
            source_db TEXT NOT NULL,
            cash_eur REAL,
            open_positions INTEGER NOT NULL,
            closed_trades INTEGER NOT NULL,
            wins INTEGER NOT NULL,
            losses INTEGER NOT NULL,
            breakeven INTEGER NOT NULL,
            net_pnl REAL NOT NULL,
            gross_profit REAL NOT NULL,
            gross_loss REAL NOT NULL,
            profit_factor REAL,
            profit_factor_infinite INTEGER NOT NULL,
            data_status TEXT NOT NULL,
            recorded_at_ms INTEGER NOT NULL,
            PRIMARY KEY (bucket_ms, strategy)
        );

        CREATE TABLE IF NOT EXISTS missed_snapshots (
            bucket_ms INTEGER NOT NULL,
            strategy TEXT NOT NULL,
            horizon_min INTEGER NOT NULL,
            mature_n INTEGER NOT NULL,
            avg_return_pct REAL,
            positive_pct REAL,
            recorded_at_ms INTEGER NOT NULL,
            PRIMARY KEY (bucket_ms, strategy, horizon_min)
        );

        CREATE TABLE IF NOT EXISTS stop_rebounds (
            strategy TEXT NOT NULL,
            trade_id INTEGER NOT NULL,
            market TEXT NOT NULL,
            closed_at_ms INTEGER NOT NULL,
            horizon_min INTEGER NOT NULL,
            exit_price REAL NOT NULL,
            end_return_pct REAL NOT NULL,
            max_up_pct REAL NOT NULL,
            max_down_pct REAL NOT NULL,
            evaluated_at_ms INTEGER NOT NULL,
            PRIMARY KEY (strategy, trade_id, horizon_min)
        );

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        '''
    )
    conn.commit()
    return conn


def _state_value(source: sqlite3.Connection, key: str, default: str = '') -> str:
    row = source.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return default if row is None else str(row['value'])


def _snapshot_source(source: sqlite3.Connection) -> dict[str, Any]:
    trade = source.execute(
        '''SELECT
               COUNT(*) closed,
               SUM(CASE WHEN pnl_eur > 0 THEN 1 ELSE 0 END) wins,
               SUM(CASE WHEN pnl_eur < 0 THEN 1 ELSE 0 END) losses,
               SUM(CASE WHEN pnl_eur = 0 THEN 1 ELSE 0 END) breakeven,
               COALESCE(SUM(pnl_eur),0) net_pnl,
               COALESCE(SUM(CASE WHEN pnl_eur > 0 THEN pnl_eur ELSE 0 END),0) gross_profit,
               COALESCE(-SUM(CASE WHEN pnl_eur < 0 THEN pnl_eur ELSE 0 END),0) gross_loss
           FROM trades'''
    ).fetchone()
    opened = source.execute('SELECT COUNT(*) n FROM positions').fetchone()

    closed = int(trade['closed'] or 0)
    wins = int(trade['wins'] or 0)
    losses = int(trade['losses'] or 0)
    breakeven = int(trade['breakeven'] or 0)
    net_pnl = float(trade['net_pnl'] or 0.0)
    gross_profit = float(trade['gross_profit'] or 0.0)
    gross_loss = float(trade['gross_loss'] or 0.0)
    pf_infinite = gross_profit > 0 and gross_loss <= 1e-12
    profit_factor = None if pf_infinite else (gross_profit / gross_loss if gross_loss > 0 else 0.0)

    cash_raw = _state_value(source, 'cash_eur', '')
    try:
        cash = float(cash_raw)
    except (TypeError, ValueError):
        cash = None

    return {
        'cash_eur': cash,
        'open_positions': int(opened['n'] or 0),
        'closed_trades': closed,
        'wins': wins,
        'losses': losses,
        'breakeven': breakeven,
        'net_pnl': net_pnl,
        'gross_profit': gross_profit,
        'gross_loss': gross_loss,
        'profit_factor': profit_factor,
        'profit_factor_infinite': pf_infinite,
        'data_status': _state_value(source, 'data_status', 'UNKNOWN').upper(),
    }


def _save_snapshot(
    research: sqlite3.Connection,
    bucket_ms: int,
    strategy: str,
    source_db: str,
    snapshot: dict[str, Any],
    now_ms: int,
) -> None:
    research.execute(
        '''INSERT INTO strategy_snapshots(
               bucket_ms,strategy,source_db,cash_eur,open_positions,closed_trades,
               wins,losses,breakeven,net_pnl,gross_profit,gross_loss,
               profit_factor,profit_factor_infinite,data_status,recorded_at_ms
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(bucket_ms,strategy) DO UPDATE SET
               source_db=excluded.source_db,
               cash_eur=excluded.cash_eur,
               open_positions=excluded.open_positions,
               closed_trades=excluded.closed_trades,
               wins=excluded.wins,
               losses=excluded.losses,
               breakeven=excluded.breakeven,
               net_pnl=excluded.net_pnl,
               gross_profit=excluded.gross_profit,
               gross_loss=excluded.gross_loss,
               profit_factor=excluded.profit_factor,
               profit_factor_infinite=excluded.profit_factor_infinite,
               data_status=excluded.data_status,
               recorded_at_ms=excluded.recorded_at_ms''',
        (
            bucket_ms,
            strategy,
            source_db,
            snapshot['cash_eur'],
            snapshot['open_positions'],
            snapshot['closed_trades'],
            snapshot['wins'],
            snapshot['losses'],
            snapshot['breakeven'],
            snapshot['net_pnl'],
            snapshot['gross_profit'],
            snapshot['gross_loss'],
            snapshot['profit_factor'],
            1 if snapshot['profit_factor_infinite'] else 0,
            snapshot['data_status'],
            now_ms,
        ),
    )


def _evaluate_stop_rebounds(
    source: sqlite3.Connection,
    research: sqlite3.Connection,
    strategy: str,
    interval: str,
    now_ms: int,
) -> int:
    if interval not in INTERVAL_MS:
        raise ValueError(f'interval niet ondersteund: {interval}')
    interval_ms = INTERVAL_MS[interval]

    trades = source.execute(
        '''SELECT id,market,closed_at_ms,exit_price
           FROM trades
           WHERE exit_reason='stop_loss'
           ORDER BY id'''
    ).fetchall()
    inserted = 0

    for trade in trades:
        trade_id = int(trade['id'])
        market = str(trade['market'])
        closed_at_ms = int(trade['closed_at_ms'])
        exit_price = float(trade['exit_price'])
        if not math.isfinite(exit_price) or exit_price <= 0:
            continue

        latest = source.execute(
            '''SELECT MAX(timestamp_ms) mx
               FROM candles
               WHERE market=? AND interval=?''',
            (market, interval),
        ).fetchone()
        latest_ts = 0 if latest is None or latest['mx'] is None else int(latest['mx'])

        for horizon_min in HORIZONS_MINUTES:
            exists = research.execute(
                '''SELECT 1 FROM stop_rebounds
                   WHERE strategy=? AND trade_id=? AND horizon_min=?''',
                (strategy, trade_id, horizon_min),
            ).fetchone()
            if exists is not None:
                continue

            target_ms = closed_at_ms + horizon_min * 60_000
            if latest_ts + interval_ms < target_ms:
                continue

            rows = source.execute(
                '''SELECT timestamp_ms,high,low,close
                   FROM candles
                   WHERE market=? AND interval=?
                     AND timestamp_ms + ? > ?
                     AND timestamp_ms < ?
                   ORDER BY timestamp_ms''',
                (market, interval, interval_ms, closed_at_ms, target_ms),
            ).fetchall()
            if not rows:
                continue

            highs = [float(r['high']) for r in rows]
            lows = [float(r['low']) for r in rows]
            end_close = float(rows[-1]['close'])
            if not all(math.isfinite(x) and x > 0 for x in highs + lows + [end_close]):
                continue

            end_return = (end_close / exit_price - 1.0) * 100.0
            max_up = (max(highs) / exit_price - 1.0) * 100.0
            max_down = (min(lows) / exit_price - 1.0) * 100.0

            research.execute(
                '''INSERT OR IGNORE INTO stop_rebounds(
                       strategy,trade_id,market,closed_at_ms,horizon_min,exit_price,
                       end_return_pct,max_up_pct,max_down_pct,evaluated_at_ms
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (
                    strategy,
                    trade_id,
                    market,
                    closed_at_ms,
                    horizon_min,
                    exit_price,
                    end_return,
                    max_up,
                    max_down,
                    now_ms,
                ),
            )
            inserted += 1

    return inserted


def _missed_summary(
    audit_path: str,
    strategy: str,
) -> dict[int, dict[str, float | int | None]]:
    columns = {
        15: 'r15m_pct',
        60: 'r1h_pct',
        240: 'r4h_pct',
    }
    result: dict[int, dict[str, float | int | None]] = {}
    if not Path(audit_path).exists():
        for horizon in columns:
            result[horizon] = {'n': 0, 'avg_return_pct': None, 'positive_pct': None}
        return result

    audit = _connect_readonly(audit_path)
    try:
        for horizon, column in columns.items():
            row = audit.execute(
                f'''SELECT COUNT({column}) n,
                           AVG({column}) avg_return,
                           AVG(CASE WHEN {column} > 0 THEN 1.0 ELSE 0.0 END) positive_ratio
                    FROM skip_audit
                    WHERE strategy=? AND {column} IS NOT NULL''',
                (strategy,),
            ).fetchone()
            n = int(row['n'] or 0)
            result[horizon] = {
                'n': n,
                'avg_return_pct': None if n == 0 else float(row['avg_return']),
                'positive_pct': None if n == 0 else float(row['positive_ratio']) * 100.0,
            }
    finally:
        audit.close()
    return result


def _save_missed_snapshot(
    research: sqlite3.Connection,
    bucket_ms: int,
    strategy: str,
    missed: dict[int, dict[str, float | int | None]],
    now_ms: int,
) -> None:
    for horizon, values in missed.items():
        research.execute(
            '''INSERT INTO missed_snapshots(
                   bucket_ms,strategy,horizon_min,mature_n,avg_return_pct,positive_pct,recorded_at_ms
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(bucket_ms,strategy,horizon_min) DO UPDATE SET
                   mature_n=excluded.mature_n,
                   avg_return_pct=excluded.avg_return_pct,
                   positive_pct=excluded.positive_pct,
                   recorded_at_ms=excluded.recorded_at_ms''',
            (
                bucket_ms,
                strategy,
                horizon,
                int(values['n'] or 0),
                values['avg_return_pct'],
                values['positive_pct'],
                now_ms,
            ),
        )


def _rebound_summary(
    research: sqlite3.Connection,
    strategy: str,
    horizon_min: int,
) -> dict[str, float | int | None]:
    row = research.execute(
        '''SELECT COUNT(*) n,
                  AVG(end_return_pct) avg_end,
                  AVG(max_up_pct) avg_max_up,
                  AVG(max_down_pct) avg_max_down,
                  AVG(CASE WHEN max_up_pct >= ? THEN 1.0 ELSE 0.0 END) trigger_ratio
           FROM stop_rebounds
           WHERE strategy=? AND horizon_min=?''',
        (REBOUND_TRIGGER_PCT, strategy, horizon_min),
    ).fetchone()
    n = int(row['n'] or 0)
    if n == 0:
        return {
            'n': 0,
            'avg_end_return_pct': None,
            'avg_max_up_pct': None,
            'avg_max_down_pct': None,
            'recovered_1_5pct_pct': None,
        }
    return {
        'n': n,
        'avg_end_return_pct': float(row['avg_end']),
        'avg_max_up_pct': float(row['avg_max_up']),
        'avg_max_down_pct': float(row['avg_max_down']),
        'recovered_1_5pct_pct': float(row['trigger_ratio']) * 100.0,
    }


def _recommendation(
    strategy: str,
    snapshot: dict[str, Any],
    rebound_1h: dict[str, float | int | None],
) -> str:
    if snapshot.get('missing'):
        return 'WACHT | bronbestand ontbreekt'
    if snapshot['data_status'] not in {'READY', 'PARTIAL'}:
        return f"WACHT | data-status {snapshot['data_status']}"

    closed = int(snapshot['closed_trades'])
    if strategy == STRATEGY_A:
        if closed < MIN_CLOSED_FOR_REVIEW:
            return f'OBSERVE | gesloten trades {closed}/{MIN_CLOSED_FOR_REVIEW}'
        return 'HOLD | automatisch meten; geen parameterwijziging'

    if closed < MIN_CLOSED_FOR_REVIEW:
        return f'VERZAMELEN | gesloten trades {closed}/{MIN_CLOSED_FOR_REVIEW}'

    rebound_n = int(rebound_1h['n'] or 0)
    if rebound_n < MIN_1H_REBOUNDS_FOR_REVIEW:
        return (
            'VERZAMELEN | 1h stop-rebounds '
            f'{rebound_n}/{MIN_1H_REBOUNDS_FOR_REVIEW}'
        )

    recovered = float(rebound_1h['recovered_1_5pct_pct'] or 0.0)
    avg_end = float(rebound_1h['avg_end_return_pct'] or 0.0)
    avg_max_up = float(rebound_1h['avg_max_up_pct'] or 0.0)

    if recovered >= 60.0 and avg_max_up >= REBOUND_TRIGGER_PCT:
        return (
            'REVIEW | 1.0% stop mogelijk te krap; '
            f'{recovered:.1f}% herstelt binnen 1h minstens +{REBOUND_TRIGGER_PCT:.1f}%'
        )
    if recovered <= 25.0 and avg_end <= 0.0:
        return 'HOLD | 1.0% stop lijkt doorgaans verdere zwakte te vermijden'

    pf = snapshot['profit_factor']
    if closed >= 20 and not snapshot['profit_factor_infinite'] and pf is not None and float(pf) < 0.80:
        return 'REVIEW | prestaties zwak; onderzoek nodig, geen automatische wijziging'

    return 'HOLD | gemengd bewijs; geen overtuigende reden voor wijziging'


def _write_report(path: str, report: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
    tmp.replace(p)


def run_once(
    settings: Settings,
    *,
    now_ms: int | None = None,
    controller_path: str | None = None,
    report_path: str | None = None,
    source_paths: dict[str, str] | None = None,
    missed_path: str | None = None,
) -> dict[str, Any]:
    settings.validate()
    now = time.time_ns() // 1_000_000 if now_ms is None else int(now_ms)
    bucket_ms = (now // 3_600_000) * 3_600_000
    controller_path = controller_path or research_db_path(settings.db_path)
    report_path = report_path or research_report_path(settings.db_path)
    missed_path = missed_path or audit_db_path(settings.db_path)
    source_paths = source_paths or {
        STRATEGY_A: settings.db_path,
        STRATEGY_B: trend_db_path(settings.db_path),
        STRATEGY_C: continuation_db_path(settings.db_path),
    }

    research = _connect_research(controller_path)
    snapshots: dict[str, dict[str, Any]] = {}
    missed: dict[str, dict[int, dict[str, float | int | None]]] = {}
    inserted_rebounds = 0

    try:
        for strategy in (STRATEGY_A, STRATEGY_B, STRATEGY_C):
            source_path = source_paths[strategy]
            if not Path(source_path).exists():
                snapshot = {
                    'missing': True,
                    'source_db': source_path,
                    'cash_eur': None,
                    'open_positions': 0,
                    'closed_trades': 0,
                    'wins': 0,
                    'losses': 0,
                    'breakeven': 0,
                    'net_pnl': 0.0,
                    'gross_profit': 0.0,
                    'gross_loss': 0.0,
                    'profit_factor': 0.0,
                    'profit_factor_infinite': False,
                    'data_status': 'MISSING',
                }
            else:
                source = _connect_readonly(source_path)
                try:
                    snapshot = _snapshot_source(source)
                    snapshot['missing'] = False
                    snapshot['source_db'] = source_path
                    if strategy in {STRATEGY_B, STRATEGY_C}:
                        inserted_rebounds += _evaluate_stop_rebounds(
                            source,
                            research,
                            strategy,
                            settings.interval,
                            now,
                        )
                finally:
                    source.close()

            snapshots[strategy] = snapshot
            _save_snapshot(research, bucket_ms, strategy, source_path, snapshot, now)

            missed_values = _missed_summary(missed_path, strategy)
            missed[strategy] = missed_values
            _save_missed_snapshot(research, bucket_ms, strategy, missed_values, now)

        rebound = {
            STRATEGY_B: {
                horizon: _rebound_summary(research, STRATEGY_B, horizon)
                for horizon in HORIZONS_MINUTES
            },
            STRATEGY_C: {
                horizon: _rebound_summary(research, STRATEGY_C, horizon)
                for horizon in HORIZONS_MINUTES
            },
        }

        recommendations = {
            STRATEGY_A: _recommendation(
                STRATEGY_A,
                snapshots[STRATEGY_A],
                {'n': 0},
            ),
            STRATEGY_B: _recommendation(
                STRATEGY_B,
                snapshots[STRATEGY_B],
                rebound[STRATEGY_B][60],
            ),
            STRATEGY_C: _recommendation(
                STRATEGY_C,
                snapshots[STRATEGY_C],
                rebound[STRATEGY_C][60],
            ),
        }

        report = {
            'mode': MODE,
            'run_interval_minutes': RUN_INTERVAL_SECONDS // 60,
            'auto_modify_strategy': False,
            'auto_deploy': False,
            'generated_at_ms': now,
            'bucket_ms': bucket_ms,
            'controller_db': controller_path,
            'source_databases': source_paths,
            'inserted_rebound_measurements': inserted_rebounds,
            'strategies': snapshots,
            'missed_trade_summary': missed,
            'stop_rebound_summary': rebound,
            'recommendations': recommendations,
        }

        research.execute(
            '''INSERT INTO state(key,value) VALUES('last_run_ms',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
            (str(now),),
        )
        research.execute(
            '''INSERT INTO state(key,value) VALUES('mode',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value''',
            (MODE,),
        )
        research.commit()
        _write_report(report_path, report)
        return report
    finally:
        research.close()


def _pf_text(snapshot: dict[str, Any]) -> str:
    if snapshot.get('profit_factor_infinite'):
        return 'INF'
    value = snapshot.get('profit_factor')
    return '-' if value is None else f'{float(value):.3f}'


def print_report(report: dict[str, Any]) -> None:
    print('=== AUTO RESEARCH CONTROLLER ===')
    print(f"MODE            : {report['mode']}")
    print(f"INTERVAL        : {report['run_interval_minutes']} minuten")
    print('AUTO WIJZIGEN   : NEE')
    print('AUTO DEPLOY     : NEE')
    print(f"CONTROLLER DB   : {report['controller_db']}")
    print(f"LAATSTE RUN     : {report['generated_at_ms']}")

    labels = {
        STRATEGY_A: 'A',
        STRATEGY_B: 'B v7',
        STRATEGY_C: 'C v6',
    }
    for strategy in (STRATEGY_A, STRATEGY_B, STRATEGY_C):
        s = report['strategies'][strategy]
        print(f"\n--- {labels[strategy]} ---")
        print(
            f"STATUS {s['data_status']} | closed {s['closed_trades']} | "
            f"W/L {s['wins']}/{s['losses']} | PnL €{s['net_pnl']:+.2f} | PF {_pf_text(s)}"
        )
        print(f"ADVIES          : {report['recommendations'][strategy]}")

        miss = report['missed_trade_summary'][strategy]
        for horizon in (15, 60, 240):
            m = miss[horizon]
            if int(m['n'] or 0) > 0:
                print(
                    f"MISSED {horizon:3}m    : n={m['n']} | "
                    f"gem {float(m['avg_return_pct']):+.2f}% | >0 {float(m['positive_pct']):.1f}%"
                )

        if strategy in {STRATEGY_B, STRATEGY_C}:
            for horizon in HORIZONS_MINUTES:
                r = report['stop_rebound_summary'][strategy][horizon]
                if int(r['n'] or 0) > 0:
                    print(
                        f"STOP REBOUND {horizon:3}m: n={r['n']} | "
                        f"eind {float(r['avg_end_return_pct']):+.2f}% | "
                        f"max-up {float(r['avg_max_up_pct']):+.2f}% | "
                        f">=+1.5% {float(r['recovered_1_5pct_pct']):.1f}%"
                    )


def _restore_horizon_keys(report: dict[str, Any]) -> dict[str, Any]:
    for section_name in ('missed_trade_summary', 'stop_rebound_summary'):
        section = report.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for _strategy, values in section.items():
            if not isinstance(values, dict):
                continue
            for key in list(values.keys()):
                if isinstance(key, str) and key.isdigit():
                    values[int(key)] = values.pop(key)
    return report


def load_report(path: str) -> dict[str, Any] | None:
    p = Path(path)
    if not p.exists():
        return None
    value = json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise RuntimeError('research-controller rapport is ongeldig')
    return _restore_horizon_keys(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Uurlijkse read-only PAPER research controller'
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

    report_path = research_report_path(settings.db_path)
    if args.report:
        report = load_report(report_path)
        if report is None:
            print('AUTO RESEARCH CONTROLLER: nog geen uurrapport')
            return 0
        print_report(report)
        return 0

    if args.once:
        print_report(run_once(settings))
        return 0

    logger.info(
        'gestart | %s | interval=%ss | auto_modify=NEE | auto_deploy=NEE',
        MODE,
        RUN_INTERVAL_SECONDS,
    )
    while True:
        started = time.monotonic()
        try:
            report = run_once(settings)
            logger.info(
                'uurmeting gereed | B=%s | C=%s | nieuwe_rebounds=%s',
                report['recommendations'][STRATEGY_B],
                report['recommendations'][STRATEGY_C],
                report['inserted_rebound_measurements'],
            )
        except Exception:
            logger.exception('uurmeting mislukt; bronstrategieën blijven onaangeroerd')

        elapsed = time.monotonic() - started
        time.sleep(max(60.0, RUN_INTERVAL_SECONDS - elapsed))


if __name__ == '__main__':
    raise SystemExit(main())
