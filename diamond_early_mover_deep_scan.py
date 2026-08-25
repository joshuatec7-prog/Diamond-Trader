#!/usr/bin/env python3
import argparse, json, os, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import ccxt
import market_scanner as ms

SOURCE = Path("/var/data/diamond_marketwide_1m_early_movers.json")
OUTPUT = Path("/var/data/diamond_early_mover_deep_scan.json")

NL = ZoneInfo("Europe/Amsterdam")
MAX_MARKETS = 10
MAX_SOURCE_AGE = 180

def atomic(path, data):
    fd,tmp=tempfile.mkstemp(dir=str(path.parent), prefix="."+path.name)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            json.dump(data,f,indent=2,ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def self_test():
    rows=[
        {"symbol":"A/EUR","liquidity":"LOW","priority":9},
        {"symbol":"B/EUR","liquidity":"PASS","priority":3},
        {"symbol":"C/EUR","liquidity":"WATCH","priority":5},
    ]
    chosen=[
        x for x in rows
        if x["liquidity"] in {"PASS","WATCH"}
    ]
    chosen.sort(key=lambda x:x["priority"],reverse=True)
    assert [x["symbol"] for x in chosen] == ["C/EUR","B/EUR"]
    print("SELF TEST: PASS")

def run():
    if not SOURCE.exists():
        raise SystemExit("STOP: early-mover bron ontbreekt")

    source=json.loads(SOURCE.read_text())

    generated=source.get("generated_at_utc")
    if generated:
        dt=datetime.fromisoformat(generated.replace("Z","+00:00"))
        age=(datetime.now(timezone.utc)-dt).total_seconds()
        if age > MAX_SOURCE_AGE:
            raise SystemExit(f"STOP: early-mover bron is {age:.0f}s oud")

    candidates=[
        x for x in source.get("candidates",[])
        if x.get("liquidity") in {"PASS","WATCH"}
    ]
    candidates.sort(
        key=lambda x: float(x.get("priority") or 0),
        reverse=True
    )
    candidates=candidates[:MAX_MARKETS]

    ex=ccxt.bitvavo({
        "enableRateLimit":True,
        "timeout":30000,
    })
    ex.load_markets()
    tickers=ex.fetch_tickers()

    cfg=ms.settings(ms.load_yaml(ms.CFG_FILE),20)

    results=[]
    errors=[]

    for early in candidates:
        symbol=early["symbol"]
        try:
            record=ms.market_record_for_symbol(
                ex,tickers,symbol
            )
            if record is None:
                raise RuntimeError("geen geldige markt/ticker")

            analysis=ms.analyse_symbol(
                ex,
                record,
                cfg,
            )

            signals=[]
            for s in analysis.get("signals",[]):
                eco=s.get("economics") or {}
                signals.append({
                    "strategy":s.get("strategy"),
                    "side":s.get("side"),
                    "score":s.get("score"),
                    "market_regime":s.get("market_regime"),
                    "shadow_eligible":s.get("shadow_eligible"),
                    "rejections":s.get(
                        "shadow_rejection_reasons",[]
                    ),
                    "reward_risk":eco.get("reward_risk"),
                    "expected_profit_eur":
                        eco.get("expected_profit_eur"),
                })

            results.append({
                "symbol":symbol,
                "liquidity":early.get("liquidity"),
                "move_1m_pct":early.get("move_1m_pct"),
                "move_5m_pct":early.get("move_5m_pct"),
                "move_15m_pct":early.get("move_15m_pct"),
                "spread_pct":early.get("spread_pct"),
                "volume_quote_24h":
                    early.get("volume_quote_24h"),
                "market_regime":
                    analysis.get("market_regime"),
                "rsi_15m":analysis.get("rsi_15m"),
                "atr_pct_15m":
                    analysis.get("atr_pct_15m"),
                "signal_count":len(signals),
                "signals":signals,
            })

        except Exception as e:
            errors.append({
                "symbol":symbol,
                "error":f"{type(e).__name__}: {e}",
            })

    now=datetime.now(timezone.utc)
    report={
        "generated_at_utc":now.isoformat(),
        "generated_at_nl":
            now.astimezone(NL).isoformat(),
        "source_candidates":
            source.get("candidate_count",0),
        "pass_watch_candidates":len(candidates),
        "deep_scanned":len(results),
        "errors":errors,
        "research_only":True,
        "live_changed":False,
        "results":results,
    }
    atomic(OUTPUT,report)

    print("--- EARLY MOVER DEEP SCAN ---")
    print("early movers :", report["source_candidates"])
    print("PASS/WATCH   :", len(candidates))
    print("deep scanned :", len(results))
    print("errors       :", len(errors))
    print()

    for r in results:
        sig=[
            f"{x['strategy']}:{x['side']}"
            for x in r["signals"]
        ]
        print(
            f"{r['symbol']:12} "
            f"1m={r['move_1m_pct'] or 0:+.2f}% "
            f"5m={r['move_5m_pct'] or 0:+.2f}% "
            f"15m={r['move_15m_pct'] or 0:+.2f}% "
            f"{r['liquidity']:5} "
            f"signals={len(sig)} "
            f"{','.join(sig) if sig else '-'}"
        )

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--self-test",action="store_true")
    ap.add_argument("--once",action="store_true")
    ap.add_argument("--loop",action="store_true")
    a=ap.parse_args()

    if a.self_test:
        self_test()
        return

    if a.once:
        run()
        return

    while True:
        try:
            run()
        except Exception as e:
            print(
                "DEEP SCAN FOUT:",
                type(e).__name__,
                e,
                flush=True,
            )
        time.sleep(60)

if __name__=="__main__":
    main()
