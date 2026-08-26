from __future__ import annotations

import tempfile
import time
from dataclasses import replace
from pathlib import Path

from config import Settings
from main import ensure_universe, process_market
from models import Book, Candle
from paper_trader import PaperTrader
from storage import Storage
from strategy import BandReentryStrategy


class SyntheticMarketData:
    """Deterministic no-network source used only to verify the paper pipeline."""

    def __init__(self) -> None:
        interval_ms = 3_600_000
        latest_close_ms = int(time.time()*1000) - 60_000
        first_ts = latest_close_ms - interval_ms * 22
        closes = [100.0] * 20 + [90.0, 95.0]
        self.base = [
            Candle(first_ts + i*interval_ms, v, v+1.0, v-1.0, v, 10.0)
            for i, v in enumerate(closes)
        ]
        next_ts = self.base[-1].timestamp_ms + interval_ms
        self.exit_candle = Candle(next_ts, 95.0, 100.0, 94.0, 99.0, 10.0)
        self.stage = 0

    def top_markets_by_quote_volume(self, quote: str, limit: int) -> list[str]:
        if quote != 'EUR' or limit != 1:
            raise RuntimeError('synthetic universe mismatch')
        return ['SIM-EUR']

    def closed_candles(self, market: str, interval: str, limit: int,
                       now_ms: int | None = None) -> list[Candle]:
        if market != 'SIM-EUR' or interval != '1h':
            raise RuntimeError('synthetic market mismatch')
        values = self.base if self.stage == 0 else self.base + [self.exit_candle]
        return values[-limit:]

    def book(self, market: str) -> Book:
        return Book(95.00, 95.05)

    def advance(self) -> None:
        self.stage = 1


def run_offline_check() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        s = replace(
            Settings(),
            db_path=str(Path(tmp)/'offline.db'),
            universe_size=1,
            candle_limit=60,
            max_signal_age_seconds=7200,
            max_open_positions=1,
        )
        s.validate()
        db = Storage(s.db_path, s.paper_start_eur)
        try:
            source = SyntheticMarketData()
            strategy = BandReentryStrategy(s)
            trader = PaperTrader(s, db)
            markets = ensure_universe(source, db, s)
            process_market(markets[0], source, db, strategy, trader, s)
            if db.get_position(markets[0]) is None:
                raise RuntimeError('offline check: entry niet geopend')
            source.advance()
            process_market(markets[0], source, db, strategy, trader, s)
            if db.get_position(markets[0]) is not None:
                raise RuntimeError('offline check: positie niet gesloten')
            trades = db.trade_rows()
            if len(trades) != 1:
                raise RuntimeError(f'offline check: verwacht 1 trade, kreeg {len(trades)}')
            health = db.health()
            if not health['ok']:
                raise RuntimeError(f'offline check: database niet gezond: {health["errors"]}')
            return {
                'ok': True,
                'market': markets[0],
                'trades': len(trades),
                'exit_reason': str(trades[0]['exit_reason']),
                'pnl_eur': float(trades[0]['pnl_eur']),
                'db_ok': bool(health['ok']),
            }
        finally:
            db.close()


def main() -> int:
    result = run_offline_check()
    print('=== CLEAN-ROOM OFFLINE CHECK ===')
    print(f'PIPELINE        : {"PASS" if result["ok"] else "FAIL"}')
    print(f'MARKET          : {result["market"]}')
    print(f'CLOSED TRADES   : {result["trades"]}')
    print(f'EXIT REASON     : {result["exit_reason"]}')
    print(f'NET PNL         : €{result["pnl_eur"]:+.2f}')
    print(f'DATABASE        : {"PASS" if result["db_ok"] else "FAIL"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
