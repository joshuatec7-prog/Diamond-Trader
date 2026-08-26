from __future__ import annotations

from config import Settings
from report import performance, verdict
from storage import Storage


def print_status(db: Storage, s: Settings) -> None:
    p = performance(db, s.paper_start_eur)
    result, _ = verdict(p, s)
    health = db.health()
    data_status, data_detail, _ = db.data_health()
    print('=== CRYPTOBOT CLEAN-ROOM v1 ===')
    print(f'MODE            : {s.run_mode} ONLY')
    print(f'UNIVERSE        : {", ".join(db.universe()) or "nog niet gekozen"}')
    print(f'INTERVAL        : {s.interval}')
    print(f'PAPER CASH      : €{db.cash_eur():.2f}')
    print(f'OPEN POSITIONS  : {len(db.all_positions())}/{s.max_open_positions}')
    print(f'CLOSED TRADES   : {p.trades}')
    print(f'VERDICT         : {result}')
    print(f'DATA STATUS     : {data_status}')
    print(f'DATA DETAIL     : {data_detail}')
    print(f'DATABASE        : {"PASS" if health["ok"] else "FAIL"} | sqlite={health["sqlite"]} | schema={health["schema_version"]}')
    print(f'DB              : {s.db_path}')
