from __future__ import annotations

from config import Settings
from storage import Storage


def print_status(db: Storage, settings: Settings) -> None:
    s = db.summary()
    print("=== CRYPTOBOT FRESH v1 ===")
    print("MODE            : PAPER ONLY")
    print(f"MARKETS         : {', '.join(settings.markets)}")
    print(f"INTERVAL        : {settings.interval}")
    print(f"PAPER CASH      : €{s['cash_eur']:.2f}")
    print(f"OPEN POSITIONS  : {int(s['open_positions'])}/{settings.max_open_positions}")
    print(f"CLOSED TRADES   : {int(s['trades'])}")
    print(f"W/L             : {int(s['wins'])}/{int(s['losses'])}")
    print(f"REALIZED PNL    : €{s['pnl_eur']:+.4f}")
    pf = s["profit_factor"]
    print(f"PROFIT FACTOR   : {'INF' if pf >= 999 else f'{pf:.3f}'}")
    print(f"DB              : {settings.db_path}")
