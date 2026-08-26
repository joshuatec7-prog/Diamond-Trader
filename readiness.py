from __future__ import annotations

from config import Settings
from storage import Storage


def readiness(db: Storage, s: Settings) -> dict[str, object]:
    config_ok = True
    config_error = ''
    try:
        s.validate()
    except Exception as exc:
        config_ok = False
        config_error = str(exc)

    health = db.health()
    data_status, data_detail, data_at_ms = db.data_health()
    universe = db.universe()
    local_ok = config_ok and bool(health['ok']) and s.run_mode == 'PAPER'
    data_ok = data_status == 'READY' and len(universe) == s.universe_size
    return {
        'local_ok': local_ok,
        'data_ok': data_ok,
        'paper_observation_ready': local_ok and data_ok,
        'config_ok': config_ok,
        'config_error': config_error,
        'db_health': health,
        'data_status': data_status,
        'data_detail': data_detail,
        'data_status_at_ms': data_at_ms,
        'universe': universe,
    }


def print_readiness(db: Storage, s: Settings) -> None:
    r = readiness(db, s)
    health = r['db_health']
    print('=== CRYPTOBOT CLEAN-ROOM READINESS ===')
    print(f'RUN MODE          : {s.run_mode}')
    print(f'CONFIG            : {"PASS" if r["config_ok"] else "FAIL"}')
    if r['config_error']:
        print(f'CONFIG ERROR      : {r["config_error"]}')
    print(f'DATABASE          : {"PASS" if health["ok"] else "FAIL"} | sqlite={health["sqlite"]} | schema={health["schema_version"]}')
    print(f'LOCAL SAFETY      : {"PASS" if r["local_ok"] else "FAIL"}')
    print(f'DATA STATUS       : {r["data_status"]}')
    print(f'DATA DETAIL       : {r["data_detail"]}')
    print(f'UNIVERSE          : {", ".join(r["universe"]) or "nog niet gekozen"}')
    print(f'PAPER OBSERVATION : {"READY" if r["paper_observation_ready"] else "WAIT"}')


def main() -> int:
    s = Settings()
    db = Storage(s.db_path, s.paper_start_eur)
    try:
        print_readiness(db, s)
        return 0 if readiness(db, s)['local_ok'] else 2
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
