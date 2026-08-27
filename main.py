from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from bitvavo_public import BitvavoPublic, INTERVAL_MS
from config import Settings
from market_data import MarketDataSource
from models import Decision
from paper_trader import PaperTrader
from report import print_report
from storage import Storage
from strategy import BandReentryStrategy

logger = logging.getLogger('cryptobot_cleanroom')
STOP = False


def _stop(signum: int, frame: object) -> None:
    global STOP
    STOP = True
    logger.info('stop-signaal ontvangen: %s', signum)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )


def ensure_universe(api: MarketDataSource, db: Storage, s: Settings) -> list[str]:
    existing = db.universe()
    if existing:
        return existing
    selected = api.top_markets_by_quote_volume(s.quote_currency, s.universe_size)
    db.set_universe(selected)
    logger.info('universe vastgezet op basis van actueel 24h EUR-volume: %s', ','.join(selected))
    return selected


def log_public_probe(api: BitvavoPublic) -> None:
    results = api.probe_public_endpoints()
    results.append(api.probe_websocket_handshake())
    for result in results:
        logger.error(
            'BITVAVO PUBLIC PROBE | %s | status=%s | %s',
            result['name'], result['status'], result['body'] or '-',
        )


def acquire_universe(api: BitvavoPublic, db: Storage, s: Settings,
                     once: bool) -> list[str] | None:
    db.set_data_health('STARTING', 'marktuniversum initialiseren')
    while not STOP:
        try:
            markets = ensure_universe(api, db, s)
            db.set_data_health('UNIVERSE_READY', f'universe beschikbaar: {len(markets)} markten')
            return markets
        except Exception as exc:
            detail = f'{type(exc).__name__}: {exc}'
            db.set_data_health('BLOCKED', detail)
            logger.error('universe initialisatie mislukt: %s', exc)
            log_public_probe(api)
            if once:
                return None
            wait_seconds = s.degraded_retry_seconds
            logger.warning(
                'worker blijft actief zonder trading; nieuwe publieke API-poging over %s seconden',
                wait_seconds,
            )
            for _ in range(wait_seconds):
                if STOP:
                    return None
                time.sleep(1)
    return None


def process_market(market: str, api: MarketDataSource, db: Storage,
                   strategy: BandReentryStrategy, trader: PaperTrader,
                   s: Settings) -> bool:
    candles = api.closed_candles(market, s.interval, s.candle_limit)
    if len(candles) < s.band_window + 1:
        raise RuntimeError(f'{market}: onvoldoende gesloten candles')
    db.save_candles(market, s.interval, candles)

    last_done = db.last_processed(market)
    if last_done == 0:
        new_candles = [candles[-1]]
    else:
        new_candles = [c for c in candles if c.timestamp_ms > last_done]

    # Eerst alle nieuwe gesloten candles verwerken. Hierdoor blijven bar-based
    # max-hold en dezelfde-candle stop/take deterministisch en herstartveilig.
    for candle in new_candles:
        event = trader.process_candle(market, candle)
        if event:
            logger.info('%s %s @ %.8f | %s | pnl=%s', event.market, event.kind, event.price,
                        event.reason, '-' if event.pnl_eur is None else f'€{event.pnl_eur:+.2f}')
        db.set_last_processed(market, candle.timestamp_ms)

    # Een open paperpositie wordt iedere poll (standaard 120s) ook met de
    # actuele bied/laat gecontroleerd. Zo hoeft stop/take niet tot de volgende
    # 15m candle-close te wachten.
    if db.get_position(market) is not None:
        live_book = api.book(market)
        event = trader.process_book(market, live_book)
        if event:
            logger.info('%s %s @ %.8f | %s | pnl=%s | intracycle',
                        event.market, event.kind, event.price, event.reason,
                        '-' if event.pnl_eur is None else f'€{event.pnl_eur:+.2f}')

    if not new_candles:
        return True

    latest = candles[-1]
    decision = strategy.evaluate(candles)
    now_ms = int(time.time()*1000)
    close_time = latest.timestamp_ms + INTERVAL_MS[s.interval]
    age_seconds = max(0.0, (now_ms-close_time)/1000.0)

    if decision.action == 'BUY' and age_seconds > s.max_signal_age_seconds:
        decision = Decision('SKIP', 'signaal_te_oud', {**decision.metrics, 'age_seconds': age_seconds})

    if decision.action == 'BUY':
        book = api.book(market)
        allowed, block_reason = trader.can_open(market, book)
        if not allowed:
            decision = Decision('SKIP', block_reason, {**decision.metrics, 'spread_pct': book.spread_pct})
        else:
            event = trader.open_long(market, book, latest.timestamp_ms)
            if event:
                logger.info('%s OPEN @ %.8f | spread=%.4f%%', market, event.price, book.spread_pct)

    db.save_decision(market, latest.timestamp_ms, decision)
    if decision.action == 'SKIP':
        logger.info('%s SKIP | %s', market, decision.reason)
    return True


def run_cycle(api: MarketDataSource, db: Storage, strategy: BandReentryStrategy,
              trader: PaperTrader, s: Settings, markets: list[str]) -> tuple[int, int, str]:
    ok = 0
    failed = 0
    last_error = ''
    for market in markets:
        if STOP:
            break
        try:
            process_market(market, api, db, strategy, trader, s)
            ok += 1
        except Exception as exc:
            failed += 1
            last_error = f'{market}: {type(exc).__name__}: {exc}'
            logger.exception('%s: cyclus mislukt', market)
    return ok, failed, last_error


def main() -> int:
    parser = argparse.ArgumentParser(description='CryptoBot Clean-Room v1 - paper only')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--report', action='store_true')
    parser.add_argument('--readiness', action='store_true')
    args = parser.parse_args()

    s = Settings()
    s.validate()
    setup_logging(s.log_level)
    db = Storage(s.db_path, s.paper_start_eur)
    try:
        if args.status:
            from status import print_status
            print_status(db, s)
            return 0
        if args.report:
            print_report(db, s)
            return 0
        if args.readiness:
            from readiness import print_readiness
            print_readiness(db, s)
            return 0

        api = BitvavoPublic(s.api_base_url, s.request_timeout_seconds, s.request_retries)
        strategy = BandReentryStrategy(s)
        trader = PaperTrader(s, db)
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        markets = acquire_universe(api, db, s, args.once)
        if markets is None:
            return 2 if args.once else 0

        logger.info('gestart | PAPER ONLY | interval=%s | universe=%s | db=%s', s.interval, ','.join(markets), s.db_path)

        loop = not args.once and s.loop_enabled
        consecutive_total_failures = 0
        while True:
            ok, failed, last_error = run_cycle(api, db, strategy, trader, s, markets)
            if ok == len(markets) and failed == 0:
                consecutive_total_failures = 0
                db.set_data_health('READY', f'volledige cyclus ok={ok}')
            elif ok > 0:
                consecutive_total_failures = 0
                db.set_data_health('PARTIAL', f'cyclus ok={ok} failed={failed}; {last_error}')
            else:
                consecutive_total_failures += 1
                db.set_data_health('DEGRADED', last_error or f'alle {failed} marktcycli mislukt')
            if consecutive_total_failures >= s.max_consecutive_failed_cycles:
                logger.error('marktdata volledig onbereikbaar gedurende %s cycli; publieke API-diagnose volgt', consecutive_total_failures)
                log_public_probe(api)
                consecutive_total_failures = 0
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
