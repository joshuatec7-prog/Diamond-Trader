from __future__ import annotations

from config import Settings
from storage import Storage


def print_status(db: Storage, settings: Settings) -> None:
    s = db.summary()
    ok = int(db.get_state("last_cycle_ok_markets", "0") or "0")
    failed = int(db.get_state("last_cycle_failed_markets", "0") or "0")
    failed_streak = int(db.get_state("consecutive_failed_cycles", "0") or "0")

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
    print(f"LAST CYCLE      : OK={ok} FAIL={failed}")
    print(f"DATA FAIL STREAK: {failed_streak}/{settings.max_consecutive_failed_cycles}")
    print(f"DB              : {settings.db_path}")
