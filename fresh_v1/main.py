from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass

from bitvavo_market import INTERVAL_MS, BitvavoMarket
from config import Settings
from models import Signal
from paper_trader import PaperTrader
from storage import Storage
from strategy import TrendBreakoutStrategy

logger = logging.getLogger("cryptobot")
STOP_REQUESTED = False


@dataclass(frozen=True)
class CycleStats:
    markets_ok: int = 0
    markets_failed: int = 0


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _handle_stop(signum: int, frame: object) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    logger.info("Stop-signaal ontvangen (%s); nette shutdown aangevraagd", signum)


def process_market(
    market: str,
    settings: Settings,
    api: BitvavoMarket,
    db: Storage,
    strategy: TrendBreakoutStrategy,
    trader: PaperTrader,
) -> bool:
    now_ms = int(time.time() * 1000)
    candles = api.get_closed_candles(
        market=market,
        interval=settings.interval,
        limit=settings.candle_limit,
        now_ms=now_ms,
    )
    if not candles:
        logger.error("%s: geen gesloten candles ontvangen", market)
        return False

    db.save_candles(market, settings.interval, candles)
    last_done = db.last_processed_candle(market)
    new_candles = [c for c in candles if c.timestamp_ms > last_done]
    if not new_candles:
        return True

    # Exits verwerken we op iedere gemiste gesloten candle in tijdsvolgorde.
    for candle in new_candles:
        event = trader.process_candle(market, candle)
        if event:
            logger.info(
                "%s %s @ %.8f | %s | pnl=%s",
                event.market,
                event.kind,
                event.price,
                event.reason,
                "-" if event.pnl_eur is None else f"€{event.pnl_eur:+.4f}",
            )
        db.set_last_processed_candle(market, candle.timestamp_ms)

    latest = candles[-1]
    signal_result = strategy.evaluate(candles)

    # Een BUY is alleen geldig kort na het sluiten van de signaalcandle.
    # Dit voorkomt een late entry na restart, storing of lange netwerkuitval.
    if signal_result.action == "BUY":
        candle_close_ms = latest.timestamp_ms + INTERVAL_MS[settings.interval]
        signal_age_seconds = max(0.0, (now_ms - candle_close_ms) / 1000.0)
        if signal_age_seconds > settings.max_entry_delay_seconds:
            metrics = dict(signal_result.metrics)
            metrics["signal_age_seconds"] = signal_age_seconds
            signal_result = Signal("SKIP", "entry_signal_stale", metrics)

    db.save_signal(market, latest.timestamp_ms, signal_result)

    if signal_result.action != "BUY":
        logger.info("%s SKIP | %s", market, signal_result.reason)
        return True

    if db.get_position(market) is not None:
        logger.info("%s BUY genegeerd | positie_al_open", market)
        return True

    book = api.get_book(market)
    event = trader.open_long(
        market=market,
        book=book,
        atr_value=signal_result.metrics["atr"],
        candle_ts=latest.timestamp_ms,
    )
    if event:
        logger.info(
            "%s OPEN @ %.8f | spread=%.4f%% | stop/take op ATR",
            market,
            event.price,
            book.spread_pct,
        )
    return True


def run_cycle(
    settings: Settings,
    api: BitvavoMarket,
    db: Storage,
    strategy: TrendBreakoutStrategy,
    trader: PaperTrader,
) -> CycleStats:
    ok = 0
    failed = 0
    for market in settings.markets:
        if STOP_REQUESTED:
            break
        try:
            if process_market(market, settings, api, db, strategy, trader):
                ok += 1
            else:
                failed += 1
        except Exception:
            failed += 1
            logger.exception("%s: cyclus mislukt", market)
    return CycleStats(markets_ok=ok, markets_failed=failed)


def print_startup(settings: Settings, db: Storage) -> None:
    summary = db.summary()
    logger.info(
        "CryptoBot Fresh v1 gestart | PAPER ONLY | markets=%s | interval=%s | cash=€%.2f | db=%s",
        ",".join(settings.markets),
        settings.interval,
        summary["cash_eur"],
        settings.db_path,
    )
    logger.info(
        "Kostenmodel | taker=%.3f%% per zijde | slippage=%.3f%% | max spread=%.3f%%",
        settings.taker_fee_pct,
        settings.slippage_pct,
        settings.max_spread_pct,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="CryptoBot Fresh v1 - paper trader")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Draai precies één cyclus")
    mode.add_argument("--loop", action="store_true", help="Blijf draaien")
    parser.add_argument("--status", action="store_true", help="Toon alleen database-status")
    args = parser.parse_args()

    settings = Settings()
    settings.validate()
    setup_logging(settings.log_level)

    db = Storage(settings.db_path, settings.paper_start_eur)
    try:
        if args.status:
            from status import print_status
            print_status(db, settings)
            return 0

        api = BitvavoMarket(
            settings.api_base_url,
            timeout_seconds=settings.request_timeout_seconds,
            retries=settings.request_retries,
        )
        strategy = TrendBreakoutStrategy(settings)
        trader = PaperTrader(settings, db)

        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)
        print_startup(settings, db)

        loop = args.loop or (not args.once and settings.loop_enabled)
        consecutive_failed_cycles = 0
        while True:
            stats = run_cycle(settings, api, db, strategy, trader)
            db.set_state("last_cycle_at_ms", int(time.time() * 1000))
            db.set_state("last_cycle_ok_markets", stats.markets_ok)
            db.set_state("last_cycle_failed_markets", stats.markets_failed)

            if stats.markets_ok == 0 and stats.markets_failed > 0:
                consecutive_failed_cycles += 1
                logger.error(
                    "MARKET DATA UNAVAILABLE | alle %s markten mislukt | reeks=%s/%s",
                    stats.markets_failed,
                    consecutive_failed_cycles,
                    settings.max_consecutive_failed_cycles,
                )
                if not loop or consecutive_failed_cycles >= settings.max_consecutive_failed_cycles:
                    return 2
            else:
                consecutive_failed_cycles = 0
            db.set_state("consecutive_failed_cycles", consecutive_failed_cycles)

            if not loop or STOP_REQUESTED:
                break
            for _ in range(settings.poll_seconds):
                if STOP_REQUESTED:
                    break
                time.sleep(1)
        return 0
    finally:
        db.close()
        logger.info("CryptoBot Fresh gestopt")


if __name__ == "__main__":
    sys.exit(main())
