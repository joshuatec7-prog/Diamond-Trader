from __future__ import annotations

import argparse
import json
import math
import sqlite3
import tempfile
from bisect import bisect_right
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from adaptive_ls_strict_strategy import StrictAdaptiveLongShortStrategy
from adaptive_ls_trader import AdaptiveLongShortPaperTrader
from config import Settings
from models import Book, Candle, Decision
from storage import Storage

ASSUMED_SPREAD_PCT = 0.12
INTERVAL_MS = 900_000


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


def _book(close: float, spread_pct: float = ASSUMED_SPREAD_PCT) -> Book:
    half = spread_pct / 200.0
    return Book(close * (1.0 - half), close * (1.0 + half))


def _summary(rows) -> dict[str, float | int]:
    pnls = [float(r['pnl_eur']) for r in rows]
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss = -sum(x for x in pnls if x < 0)
    return {
        'trades': len(rows),
        'wins': sum(1 for x in pnls if x > 0),
        'losses': sum(1 for x in pnls if x < 0),
        'breakeven': sum(1 for x in pnls if x == 0),
        'pnl': sum(pnls),
        'pf': math.inf if gross_profit > 0 and gross_loss <= 1e-12 else (
            gross_profit / gross_loss if gross_loss > 0 else 0.0
        ),
    }


def _open_pnl(trader: AdaptiveLongShortPaperTrader, p, side: str, reference: float) -> float:
    if side == 'LONG':
        exit_price = reference * (1.0 - trader.slippage_rate)
        exit_notional = p.amount * exit_price
        exit_fee = exit_notional * trader.fee_rate
        return exit_notional - exit_fee - p.entry_notional - p.entry_fee
    exit_price = reference * (1.0 + trader.slippage_rate)
    exit_notional = p.amount * exit_price
    exit_fee = exit_notional * trader.fee_rate
    gross = p.amount * (p.entry_price - exit_price)
    return gross - p.entry_fee - exit_fee


def _readonly(path: str) -> sqlite3.Connection:
    absolute = Path(path).resolve().as_posix()
    conn = sqlite3.connect(f'file:{absolute}?mode=ro', uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def run(source_db: str) -> int:
    src = _readonly(source_db)
    try:
        universe_row = src.execute("SELECT value FROM state WHERE key='universe_json'").fetchone()
        if universe_row is None:
            raise RuntimeError('D v2 universe_json ontbreekt')
        universe = [str(x) for x in json.loads(str(universe_row['value']))]

        start_row = src.execute('SELECT MIN(timestamp_ms) AS ts FROM decisions').fetchone()
        last_trade_row = src.execute('SELECT MAX(closed_at_ms) AS ts FROM trades').fetchone()
        if start_row is None or start_row['ts'] is None:
            raise RuntimeError('D v2 heeft nog geen decisions')
        start_ts = int(start_row['ts'])

        if last_trade_row is not None and last_trade_row['ts'] is not None:
            last_closed_ms = int(last_trade_row['ts'])
            end_row = src.execute(
                'SELECT MAX(timestamp_ms) AS ts FROM decisions WHERE timestamp_ms<=?',
                (last_closed_ms,),
            ).fetchone()
        else:
            last_closed_ms = 0
            end_row = src.execute('SELECT MAX(timestamp_ms) AS ts FROM decisions').fetchone()
        if end_row is None or end_row['ts'] is None:
            raise RuntimeError('geen eindtimestamp voor replay')
        end_ts = int(end_row['ts'])

        eval_times = [
            int(r['timestamp_ms'])
            for r in src.execute(
                'SELECT DISTINCT timestamp_ms FROM decisions WHERE timestamp_ms BETWEEN ? AND ? ORDER BY timestamp_ms',
                (start_ts, end_ts),
            )
        ]
        if not eval_times:
            raise RuntimeError('geen evaluatiemomenten voor replay')

        rows = src.execute(
            '''SELECT market,timestamp_ms,open,high,low,close,volume
               FROM candles
               WHERE interval='15m' AND timestamp_ms<=?
               ORDER BY market,timestamp_ms''',
            (end_ts,),
        ).fetchall()

        candles_by_market: dict[str, list[Candle]] = {m: [] for m in universe}
        times_by_market: dict[str, list[int]] = {m: [] for m in universe}
        for r in rows:
            market = str(r['market'])
            if market not in candles_by_market:
                continue
            candle = Candle(
                int(r['timestamp_ms']),
                float(r['open']),
                float(r['high']),
                float(r['low']),
                float(r['close']),
                float(r['volume']),
            )
            candles_by_market[market].append(candle)
            times_by_market[market].append(candle.timestamp_ms)

        primary = Settings()
        primary.validate()

        with tempfile.TemporaryDirectory(prefix='d2s-replay-') as tmp:
            replay_path = str(Path(tmp) / 'strict_replay.db')
            s = replace(
                primary,
                db_path=replay_path,
                max_open_positions=3,
                stop_loss_pct=1.25,
                take_profit_pct=30.0,
            )
            s.validate()
            db = Storage(s.db_path, s.paper_start_eur)
            try:
                db.set_universe(universe)
                strategy = StrictAdaptiveLongShortStrategy(s)
                trader = AdaptiveLongShortPaperTrader(s, db)
                required = strategy.required_candles()
                regime_counts: Counter[str] = Counter()
                candidate_signals = 0
                opened = 0

                for ts in eval_times:
                    contexts: dict[str, dict] = {}

                    for market in universe:
                        market_times = times_by_market[market]
                        idx = bisect_right(market_times, ts)
                        if idx < required:
                            continue
                        window = candles_by_market[market][max(0, idx - s.candle_limit):idx]
                        metrics = strategy.analyze(window)
                        if not metrics:
                            continue
                        latest = window[-1]
                        has_new = latest.timestamp_ms == ts
                        had_position = db.get_position(market) is not None

                        if has_new:
                            trader.process_candle(
                                market,
                                latest,
                                now_ms=ts + INTERVAL_MS,
                            )

                        contexts[market] = {
                            'latest': latest,
                            'metrics': metrics,
                            'has_new': has_new,
                            'had_position': had_position,
                        }

                    regime, bull, bear = strategy.market_regime(
                        {market: ctx['metrics'] for market, ctx in contexts.items()}
                    )
                    regime_counts[regime] += 1

                    for market, ctx in contexts.items():
                        p = db.get_position(market)
                        if p is None:
                            continue
                        side = trader.position_side(market)
                        if ctx['has_new']:
                            reason = strategy.exit_reason(ctx['metrics'], regime, side)
                            if reason:
                                trader.close_trend_break(
                                    market,
                                    float(ctx['metrics']['close']),
                                    reason,
                                    now_ms=ts + INTERVAL_MS,
                                )
                        if db.get_position(market) is not None:
                            trader.process_book(
                                market,
                                _book(float(ctx['metrics']['close'])),
                                float(ctx['metrics']['atr_pct']),
                                now_ms=ts + INTERVAL_MS,
                            )

                    candidates: list[tuple[float, str, str, Decision, float]] = []
                    for market, ctx in contexts.items():
                        if not ctx['has_new']:
                            continue
                        decision = strategy.evaluate_metrics(
                            ctx['metrics'],
                            regime,
                            bull,
                            bear,
                        )
                        if decision.action not in {'LONG', 'SHORT'}:
                            continue
                        candidate_signals += 1
                        if ctx['had_position'] or db.get_position(market) is not None:
                            continue
                        score = strategy.rank_score(decision)
                        if not math.isfinite(score):
                            continue
                        candidates.append((
                            float(score),
                            market,
                            decision.action,
                            decision,
                            float(ctx['metrics']['atr_pct']),
                        ))

                    candidates.sort(key=lambda x: (-x[0], x[1]))
                    slots = max(0, s.max_open_positions - len(db.all_positions()))
                    for score, market, side, decision, atr_pct in candidates:
                        if slots <= 0:
                            break
                        close = float(contexts[market]['metrics']['close'])
                        book = _book(close)
                        allowed, _ = trader.can_open(market, book)
                        if not allowed:
                            continue
                        event = trader.open_directional(
                            side,
                            market,
                            book,
                            ts,
                            atr_pct,
                            now_ms=ts + INTERVAL_MS,
                        )
                        if event is not None:
                            opened += 1
                            slots -= 1

                strict_rows = db.trade_rows()
                strict = _summary(strict_rows)

                actual_query = 'SELECT * FROM trades'
                params: tuple[int, ...] = ()
                if last_closed_ms:
                    actual_query += ' WHERE closed_at_ms<=?'
                    params = (last_closed_ms,)
                actual_query += ' ORDER BY closed_at_ms,id'
                actual_rows = src.execute(actual_query, params).fetchall()
                actual = _summary(actual_rows)

                latest_close: dict[str, float] = {}
                for market in universe:
                    market_times = times_by_market[market]
                    idx = bisect_right(market_times, end_ts)
                    if idx:
                        latest_close[market] = candles_by_market[market][idx - 1].close

                open_rows = []
                open_total = 0.0
                for p in db.all_positions():
                    side = trader.position_side(p.market)
                    last = latest_close.get(p.market)
                    if last is None:
                        continue
                    pnl = _open_pnl(trader, p, side, last)
                    open_total += pnl
                    open_rows.append((p.market, side, p.entry_price, last, p.stop_price, pnl))

                pf_actual = 'INF' if math.isinf(float(actual['pf'])) else f"{float(actual['pf']):.3f}"
                pf_strict = 'INF' if math.isinf(float(strict['pf'])) else f"{float(strict['pf']):.3f}"

                print('=== D v2 vs D v2S STRICT COUNTERFACTUAL ===')
                print(f'BRON DB           : {source_db}')
                print(f'PERIODE UTC       : {_utc(start_ts)} -> {_utc(end_ts)}')
                print(f'MARKTEN           : {len(universe)}')
                print(f'INZET             : EUR {s.position_eur:.2f}')
                print(f'FEE PER SIDE      : {s.taker_fee_pct:.2f}%')
                print(f'SLIPPAGE PER SIDE : {s.slippage_pct:.2f}%')
                print(f'AANGENOMEN SPREAD : {ASSUMED_SPREAD_PCT:.2f}%')
                print()
                print('=== STRICT MARKTREGIMES ===')
                for name in ('BULL', 'BEAR', 'SIDEWAYS', 'UNKNOWN'):
                    print(f'{name:<10}: {regime_counts.get(name, 0)}')
                print(f'ACTIE-SIGNALEN    : {candidate_signals}')
                print(f'GEOPENDE POSITIES : {opened}')
                print()
                print('=== D v2 ACTUEEL IN DEZELFDE PERIODE ===')
                print(
                    f"TRADES {actual['trades']} | W/L/BE {actual['wins']}/{actual['losses']}/{actual['breakeven']} "
                    f"| PnL EUR {float(actual['pnl']):+.2f} | PF {pf_actual}"
                )
                print()
                print('=== D v2S STRICT REPLAY ===')
                print(
                    f"TRADES {strict['trades']} | W/L/BE {strict['wins']}/{strict['losses']}/{strict['breakeven']} "
                    f"| REALIZED EUR {float(strict['pnl']):+.2f} | PF {pf_strict}"
                )
                print(f'OPEN POSITIES     : {len(open_rows)} | OPEN PnL EUR {open_total:+.2f}')
                print(f'TOTAAL EST. PnL   : EUR {float(strict["pnl"]) + open_total:+.2f}')
                print(
                    f'VERSCHIL REALIZED : EUR {float(strict["pnl"]) - float(actual["pnl"]):+.2f} '
                    '(positief = strict beter)'
                )

                if open_rows:
                    print()
                    print('=== STRICT OPEN AAN EINDE ===')
                    print('MARKET       SIDE          ENTRY          LAST          STOP       PnL')
                    for market, side, entry, last, stop, pnl in open_rows:
                        print(f'{market:<12} {side:<5} {entry:13.8f} {last:13.8f} {stop:13.8f} {pnl:+9.2f}')

                print()
                print('=== STRICT GESLOTEN TRADES ===')
                if not strict_rows:
                    print('GEEN')
                else:
                    print('OPEN UTC          CLOSE UTC         MARKET      PnL  REDEN')
                    for r in strict_rows:
                        print(
                            f"{_utc(int(r['opened_at_ms'])):<17} {_utc(int(r['closed_at_ms'])):<17} "
                            f"{str(r['market']):<10} {float(r['pnl_eur']):+7.2f}  {str(r['exit_reason'])}"
                        )

                print()
                print('LET OP: read-only candle-replay. Historische orderboekspread en')
                print('2-minuten intrabar trailing zijn niet exact opgeslagen; spread=0.12% aangenomen.')
            finally:
                db.close()
    finally:
        src.close()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Vergelijk actuele D v2 met D v2S strict via read-only candle replay'
    )
    parser.add_argument(
        '--source-db',
        default='/var/data/cryptobot_cleanroom_adaptive_trend_v2.db',
    )
    args = parser.parse_args()
    return run(args.source_db)


if __name__ == '__main__':
    raise SystemExit(main())
