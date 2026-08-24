#!/usr/bin/env python3
import argparse, csv, gzip, json, math, time
from pathlib import Path
import ccxt

DATA = Path("/var/data")
UNI = DATA / "diamond_dynamic_universe.json"
OUT = DATA / "diamond_1m_history"
STATE = DATA / "diamond_1m_history_state.json"
RETENTION_MS = 72 * 3600 * 1000


def load_rows(path):
    rows = {}
    if not path.exists():
        return rows
    with gzip.open(path, "rt", newline="") as f:
        for r in csv.reader(f):
            if not r or r[0] == "timestamp":
                continue
            try:
                rows[int(r[0])] = r
            except Exception:
                pass
    return rows


def save_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for ts in sorted(rows):
            w.writerow(rows[ts])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=20)
    args = ap.parse_args()

    u = json.loads(UNI.read_text())
    markets = [
        x["market"] for x in u["all_active_eur_markets"]
        if x.get("liquidity_status") in {"PASS", "WATCH"}
    ]
    markets = sorted(set(markets))

    state = {"index": 0}
    if STATE.exists():
        try:
            state.update(json.loads(STATE.read_text()))
        except Exception:
            pass

    start = int(state.get("index", 0)) % max(1, len(markets))
    chosen = [
        markets[(start + i) % len(markets)]
        for i in range(min(args.batch, len(markets)))
    ]

    ex = ccxt.bitvavo({"enableRateLimit": True})
    ex.load_markets()

    ok = err = added = 0
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - RETENTION_MS

    print("DIAMOND 1M HISTORY COLLECTOR v1.0")
    print(f"PASS/WATCH markten : {len(markets)}")
    print(f"Deze run           : {len(chosen)}")
    print("Retentie           : 72 uur")
    print("Orders/private API : NEE")
    print()

    for market in chosen:
        symbol = market.replace("-", "/")
        try:
            candles = ex.fetch_ohlcv(symbol, "1m", limit=1000)
            path = OUT / f"{market}.csv.gz"
            rows = load_rows(path)
            before = len(rows)

            for x in candles:
                ts = int(x[0])
                if ts >= cutoff:
                    rows[ts] = [ts, x[1], x[2], x[3], x[4], x[5]]

            rows = {ts: r for ts, r in rows.items() if ts >= cutoff}
            save_rows(path, rows)

            new = max(0, len(rows) - before)
            added += new
            ok += 1
            print(f"{market:12} candles={len(rows):4} nieuw={new:4}")
        except Exception as e:
            err += 1
            print(f"{market:12} FOUT {type(e).__name__}")

    state = {
        "index": (start + len(chosen)) % max(1, len(markets)),
        "markets": len(markets),
        "last_run_ms": now_ms,
    }
    STATE.write_text(json.dumps(state, indent=2))

    size = sum(p.stat().st_size for p in OUT.glob("*.gz")) if OUT.exists() else 0
    print()
    print(f"RESULTAAT : PASS={ok} FOUT={err} nieuw={added}")
    print(f"OPSLAG    : {size / 1024 / 1024:.2f} MB")
    print(f"VOLGENDE  : index {state['index']}")
    print("LIVE      : ONGEWIJZIGD")


if __name__ == "__main__":
    main()
