from __future__ import annotations

import argparse
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import main as core
from adaptive_ls_strategy import AdaptiveLongShortStrategy
from adaptive_ls_trader import AdaptiveLongShortPaperTrader
from bitvavo_public import BitvavoPublic, INTERVAL_MS
from config import Settings
from market_data import MarketDataSource
from models import Decision
from readiness import print_readiness
from report import print_report
from status import print_status
from storage import Storage

logger = logging.getLogger('cryptobot_cleanroom_adaptive_trend_v2')
STOP = False
MAX_OPEN = 3


@dataclass
class Context:
    market: str
    latest_ts: int
    has_new_candle: bool
    had_position: bool
    metrics: dict[str, float]


@dataclass(frozen=True)
class Candidate:
    market: str
    candle_ts: int
    side: str
    decision: Decision
    score: float
    atr_pct: float


def adaptive_v2_db_path(primary_path: str) -> str:
    p = Path(primary_path)
    suffix = p.suffix or '.db'
    stem = p.stem if p.suffix else p.name
    return str(p.with_name(f'{stem}_adaptive_trend_v2{suffix}'))


def build_settings(primary: Settings) -> Settings:
    out = replace(primary, db_path=adaptive_v2_db_path(primary.db_path), max_open_positions=MAX_OPEN,
                  stop_loss_pct=1.25, take_profit_pct=30.0)
    out.validate()
    return out


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    core.STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def _universe(primary_s: Settings, db: Storage, once: bool) -> list[str] | None:
    while not STOP:
        primary = Storage(primary_s.db_path, primary_s.paper_start_eur)
        try:
            markets = primary.universe()
        finally:
            primary.close()
        if markets:
            existing = db.universe()
            if existing and existing != markets:
                raise RuntimeError('D v2 universe wijkt af van primaire universe')
            if not existing:
                db.set_universe(markets)
            db.set_data_health('UNIVERSE_READY', f'gedeelde universe beschikbaar: {len(markets)} markten')
            return markets
        db.set_data_health('STARTING', 'wacht op primaire vaste universe')
        if once:
            return None
        time.sleep(5)
    return None


def _log(event, suffix: str = '') -> None:
    if event is not None:
        logger.info('%s %s @ %.8f | %s | pnl=%s%s', event.market, event.kind, event.price,
                    event.reason, '-' if event.pnl_eur is None else f'€{event.pnl_eur:+.2f}', suffix)


def _scan(market: str, api: MarketDataSource, db: Storage, strategy: AdaptiveLongShortStrategy,
          trader: AdaptiveLongShortPaperTrader, s: Settings) -> Context:
    candles = api.closed_candles(market, s.interval, s.candle_limit)
    if len(candles) < strategy.required_candles():
        raise RuntimeError(f'{market}: onvoldoende candles voor D v2')
    db.save_candles(market, s.interval, candles)
    had_position = db.get_position(market) is not None
    last_done = db.last_processed(market)
    new = [candles[-1]] if last_done == 0 else [c for c in candles if c.timestamp_ms > last_done]
    for candle in new:
        _log(trader.process_candle(market, candle))
        db.set_last_processed(market, candle.timestamp_ms)
    metrics = strategy.analyze(candles)
    if not metrics:
        raise RuntimeError(f'{market}: D v2 indicatoren ontbreken')
    return Context(market, candles[-1].timestamp_ms, bool(new), had_position, metrics)


def _manage(ctx: Context, regime: str, api: MarketDataSource, db: Storage,
            strategy: AdaptiveLongShortStrategy, trader: AdaptiveLongShortPaperTrader) -> int:
    if db.get_position(ctx.market) is None:
        return 0
    side = trader.position_side(ctx.market)
    if side not in {'LONG','SHORT'}:
        raise RuntimeError(f'{ctx.market}: positie zonder geldige side')
    if ctx.has_new_candle:
        reason = strategy.exit_reason(ctx.metrics, regime, side)
        if reason:
            _log(trader.close_trend_break(ctx.market, ctx.metrics['close'], reason), ' | trend-regime')
            return 0
    try:
        book = api.book(ctx.market)
    except Exception:
        logger.exception('%s: D v2 orderboek open positie mislukt', ctx.market)
        return 1
    _log(trader.process_book(ctx.market, book, ctx.metrics['atr_pct']), ' | intracycle')
    return 0


def _candidate(ctx: Context, regime: str, bull: float, bear: float, db: Storage,
               strategy: AdaptiveLongShortStrategy, s: Settings) -> Candidate | None:
    if not ctx.has_new_candle:
        return None
    decision = strategy.evaluate_metrics(ctx.metrics, regime, bull, bear)
    actionable = decision.action in {'LONG','SHORT'}
    close_time = ctx.latest_ts + INTERVAL_MS[s.interval]
    age = max(0.0, (int(time.time()*1000)-close_time)/1000.0)
    if actionable and age > s.max_signal_age_seconds:
        decision = Decision('SKIP', 'signaal_te_oud', {**decision.metrics, 'age_seconds': age})
        actionable = False
    if actionable and (ctx.had_position or db.get_position(ctx.market) is not None):
        decision = Decision('SKIP', 'positie_in_deze_cyclus', decision.metrics)
        actionable = False
    if not actionable:
        db.save_decision(ctx.market, ctx.latest_ts, decision)
        logger.info('%s SKIP | %s | regime=%s bull=%.1f%% bear=%.1f%%', ctx.market, decision.reason, regime, bull, bear)
        return None
    score = strategy.rank_score(decision)
    if not math.isfinite(score):
        db.save_decision(ctx.market, ctx.latest_ts, Decision('SKIP','ongeldige_adaptive_score',decision.metrics))
        return None
    return Candidate(ctx.market, ctx.latest_ts, decision.action, decision, score, float(ctx.metrics['atr_pct']))


def _execute(candidates: list[Candidate], api: MarketDataSource, db: Storage,
             trader: AdaptiveLongShortPaperTrader, s: Settings) -> int:
    ranked = sorted(candidates, key=lambda c: (-c.score, c.market))
    slots = max(0, s.max_open_positions-len(db.all_positions()))
    failures = 0
    for rank, c in enumerate(ranked, start=1):
        metrics = {**c.decision.metrics, 'rank_score': c.score, 'candidate_rank': float(rank),
                   'candidate_count': float(len(ranked))}
        if slots <= 0:
            db.save_decision(c.market,c.candle_ts,Decision('SKIP','rank_buiten_top_slots',metrics))
            continue
        try:
            book = api.book(c.market)
        except Exception:
            failures += 1
            db.save_decision(c.market,c.candle_ts,Decision('SKIP','entry_book_error',metrics))
            continue
        allowed, reason = trader.can_open(c.market, book)
        if not allowed:
            db.save_decision(c.market,c.candle_ts,Decision('SKIP',reason,{**metrics,'spread_pct':book.spread_pct}))
            continue
        event = trader.open_directional(c.side, c.market, book, c.candle_ts, c.atr_pct)
        if event is None:
            db.save_decision(c.market,c.candle_ts,Decision('SKIP','open_mislukt',metrics))
            continue
        db.save_decision(c.market,c.candle_ts,Decision(c.side,c.decision.reason,{**metrics,'spread_pct':book.spread_pct}))
        slots -= 1
        logger.info('%s %s OPEN @ %.8f | rank=%s/%s score=%.4f | ATR=%.2f%% | stop=%.2f%%',
                    c.market,c.side,event.price,rank,len(ranked),c.score,c.atr_pct,trader.initial_stop_pct(c.atr_pct))
    return failures


def run_cycle(api: MarketDataSource, db: Storage, strategy: AdaptiveLongShortStrategy,
              trader: AdaptiveLongShortPaperTrader, s: Settings, markets: list[str]) -> tuple[int,int,str]:
    contexts: list[Context] = []
    ok = failed = 0
    last_error = ''
    for market in markets:
        try:
            contexts.append(_scan(market,api,db,strategy,trader,s)); ok += 1
        except Exception as exc:
            failed += 1; last_error=f'{market}: {type(exc).__name__}: {exc}'
            logger.exception('%s: D v2 scan mislukt', market)
    regime,bull,bear = strategy.market_regime({c.market:c.metrics for c in contexts})
    db.set_state('adaptive_v2_regime',regime)
    db.set_state('adaptive_v2_bull_breadth_pct',f'{bull:.6f}')
    db.set_state('adaptive_v2_bear_breadth_pct',f'{bear:.6f}')
    extra = sum(_manage(c,regime,api,db,strategy,trader) for c in contexts)
    candidates = [x for c in contexts if (x := _candidate(c,regime,bull,bear,db,strategy,s)) is not None]
    extra += _execute(candidates,api,db,trader,s) if candidates else 0
    if extra:
        ok=max(0,ok-extra); failed+=extra; last_error=f'{extra} D v2 orderboek request(s) mislukt'
    logger.info('D v2 regime=%s | bull=%.1f%% bear=%.1f%% | candidates=%s | open=%s/%s',
                regime,bull,bear,len(candidates),len(db.all_positions()),s.max_open_positions)
    return ok,failed,last_error


def _print_extra(db: Storage, trader: AdaptiveLongShortPaperTrader) -> None:
    regime=db.get_state('adaptive_v2_regime','UNKNOWN')
    bull=float(db.get_state('adaptive_v2_bull_breadth_pct','0') or 0)
    bear=float(db.get_state('adaptive_v2_bear_breadth_pct','0') or 0)
    sides=[trader.position_side(p.market) for p in db.all_positions()]
    print(f'MARKTREGIME     : {regime}')
    print(f'BULL BREADTH    : {bull:.1f}%')
    print(f'BEAR BREADTH    : {bear:.1f}%')
    print(f'OPEN LONG/SHORT : {sides.count("LONG")}/{sides.count("SHORT")}')


def main() -> int:
    parser=argparse.ArgumentParser(description='CryptoBot Clean-Room Strategy D v2 LONG/SHORT PAPER')
    parser.add_argument('--once',action='store_true'); parser.add_argument('--status',action='store_true')
    parser.add_argument('--report',action='store_true'); parser.add_argument('--readiness',action='store_true')
    args=parser.parse_args()
    primary=Settings(); primary.validate(); core.setup_logging(primary.log_level)
    s=build_settings(primary); db=Storage(s.db_path,s.paper_start_eur); trader=AdaptiveLongShortPaperTrader(s,db)
    try:
        header='=== STRATEGY D v2 | ADAPTIVE LONG/SHORT TREND FOLLOWER | ATR RUNNER | PAPER ONLY ==='
        if args.status:
            print(header); print_status(db,s); _print_extra(db,trader); return 0
        if args.report:
            print(header); print_report(db,s); _print_extra(db,trader); return 0
        if args.readiness:
            print(header); print_readiness(db,s); return 0
        signal.signal(signal.SIGTERM,_stop); signal.signal(signal.SIGINT,_stop); core.STOP=False
        markets=_universe(primary,db,args.once)
        if markets is None: return 2 if args.once else 0
        for p in db.all_positions():
            if trader.position_side(p.market) not in {'LONG','SHORT'}:
                raise RuntimeError(f'{p.market}: bestaande D v2 positie zonder side')
        api=BitvavoPublic(s.api_base_url,s.request_timeout_seconds,s.request_retries)
        strategy=AdaptiveLongShortStrategy(s)
        logger.info('gestart | D v2 LONG/SHORT PAPER | BULL=LONG BEAR=SHORT SIDEWAYS=WACHT | max_open=%s | db=%s',
                    s.max_open_positions,s.db_path)
        loop=not args.once and s.loop_enabled; consecutive=0
        while True:
            ok,failed,last=run_cycle(api,db,strategy,trader,s,markets)
            if ok==len(markets) and failed==0:
                consecutive=0; db.set_data_health('READY',f'volledige adaptive-trend-v2-cyclus ok={ok}')
            elif ok>0:
                consecutive=0; db.set_data_health('PARTIAL',f'adaptive-trend-v2-cyclus ok={ok} failed={failed}; {last}')
            else:
                consecutive+=1; db.set_data_health('DEGRADED',last or f'alle {failed} D v2 marktcycli mislukt')
            if consecutive>=s.max_consecutive_failed_cycles:
                logger.error('D v2 marktdata volledig onbereikbaar gedurende %s cycli',consecutive); consecutive=0
            if not loop or STOP: return 0 if ok>0 else 2
            for _ in range(s.poll_seconds):
                if STOP: break
                time.sleep(1)
    finally:
        db.close(); logger.info('gestopt')


if __name__=='__main__':
    sys.exit(main())
