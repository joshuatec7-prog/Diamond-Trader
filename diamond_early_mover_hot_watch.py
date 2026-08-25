#!/usr/bin/env python3
import argparse
import csv
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import market_scanner as ms

from diamond_early_mover_deep_scan import (
    BRIDGE_HEADER,
    HOT_WATCH_QUEUE,
    bridge_row,
    parse_dt,
)
from diamond_selective_rules import (
    selective_accepts,
    selective_candidate_key,
)

SIGNALS = Path(
    "/var/data/diamond_hot_watch_selective_signals.csv"
)
STATE = Path(
    "/var/data/diamond_early_mover_hot_watch_state.json"
)
STATUS = Path(
    "/var/data/diamond_early_mover_hot_watch_status.json"
)

POLL_SECONDS = 10
REVALIDATE_SECONDS = 60


def atomic_json(path,data):
    fd,tmp=tempfile.mkstemp(
        dir=str(path.parent),
        prefix="."+path.name,
    )
    try:
        with os.fdopen(
            fd,"w",encoding="utf-8"
        ) as f:
            json.dump(
                data,f,indent=2,
                ensure_ascii=False
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path):
    try:
        d=json.loads(path.read_text())
        return d if isinstance(d,dict) else {}
    except Exception:
        return {}


def append_signal_atomic(signal):
    row=bridge_row(signal)

    existing=[]
    if SIGNALS.exists():
        with SIGNALS.open(
            newline="",
            encoding="utf-8",
        ) as f:
            existing=list(csv.DictReader(f))

    fd,tmp=tempfile.mkstemp(
        dir=str(SIGNALS.parent),
        prefix="."+SIGNALS.name,
    )

    try:
        with os.fdopen(
            fd,"w",
            encoding="utf-8",
            newline="",
        ) as f:
            w=csv.DictWriter(
                f,
                fieldnames=BRIDGE_HEADER,
                extrasaction="ignore",
            )
            w.writeheader()
            for old in existing:
                w.writerow(old)
            w.writerow(row)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp,SIGNALS)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fully_valid_tb_long(signal):
    return bool(
        selective_accepts(signal)
        and str(
            signal.get("side") or ""
        ).upper()=="LONG"
        and str(
            signal.get("strategy") or ""
        )=="trend_breakout"
    )


def active_symbols(queue,now):
    latest={}

    for item in queue.get("items",[]) or []:
        if not isinstance(item,dict):
            continue

        symbol=str(
            item.get("symbol") or ""
        ).upper()

        expiry=parse_dt(
            item.get("expires_at")
        )

        if not symbol or not expiry or expiry <= now:
            continue

        current=latest.get(symbol)
        if (
            current is None
            or str(item.get("last_seen_at") or "")
            > str(current.get("last_seen_at") or "")
        ):
            latest[symbol]=item

    return latest


def run_once(ex,cfg):
    now=datetime.now(timezone.utc)
    queue=read_json(HOT_WATCH_QUEUE)
    watch=active_symbols(queue,now)

    state=read_json(STATE)
    seen_order=list(dict.fromkeys(
        str(x)
        for x in state.get("emitted_keys",[])
        if str(x)
    ))
    seen=set(seen_order)

    symbol_state=state.setdefault(
        "symbols",{}
    )

    try:
        tickers=ex.fetch_tickers()
    except Exception as e:
        raise RuntimeError(
            f"fetch_tickers:{type(e).__name__}"
        )

    spread_ready=0
    revalidated=0
    published=[]

    for symbol,item in watch.items():
        rec=ms.market_record_for_symbol(
            ex,tickers,symbol
        )
        if rec is None:
            continue

        volume=float(
            rec.get("quote_volume") or 0
        )
        spread=float(
            rec.get("spread_pct") or 999
        )

        st=symbol_state.setdefault(
            symbol,{}
        )
        st["last_checked_at"]=now.isoformat()
        st["last_spread_pct"]=spread
        st["last_quote_volume"]=volume

        # Volume-eis blijft exact bestaan.
        if volume < float(
            cfg["min_quote_volume"]
        ):
            st["status"]="WAIT_VOLUME"
            continue

        # Alleen wachten tot de BESTAANDE
        # 0,10%-spreadgrens veilig is.
        if spread > float(
            cfg["trade_max_spread_pct"]
        ):
            st["status"]="WAIT_SPREAD"
            continue

        spread_ready += 1

        previous=parse_dt(
            st.get("last_revalidated_at")
        )
        if (
            previous is not None
            and (
                now-previous
            ).total_seconds()
            < REVALIDATE_SECONDS
        ):
            st["status"]="SAFE_SPREAD_COOLDOWN"
            continue

        st["last_revalidated_at"]=now.isoformat()

        # De spread is nu veilig.
        # Nu ALLE zware strategievoorwaarden opnieuw.
        analysis=ms.analyse_symbol(
            ex,rec,cfg
        )
        revalidated += 1

        valid=[]

        for signal in (
            analysis.get("signals",[]) or []
        ):
            signal["selection_reason"] = (
                "EARLY_MOVER_HOT_WATCH"
            )

            if fully_valid_tb_long(signal):
                valid.append(signal)

        if not valid:
            st["status"]="REVALIDATED_NOT_ELIGIBLE"
            continue

        st["status"]="REVALIDATED_ELIGIBLE"

        for signal in valid:
            key=selective_candidate_key(
                signal
            )
            if not key or key in seen:
                continue

            append_signal_atomic(signal)
            seen.add(key)
            seen_order.append(key)
            published.append({
                "symbol":symbol,
                "candidate_key":key,
                "spread_pct":spread,
                "detected_at":
                    signal.get("detected_at"),
            })

    state["version"]=1
    state["last_run_at"]=now.isoformat()
    state["emitted_keys"]=seen_order[-30000:]
    state["emitted_total"]=len(seen_order)
    state["symbols"]=symbol_state
    atomic_json(STATE,state)

    report={
        "generated_at":now.isoformat(),
        "watched":len(watch),
        "spread_ready":spread_ready,
        "revalidated":revalidated,
        "published":len(published),
        "published_signals":published,
        "poll_seconds":POLL_SECONDS,
        "revalidate_seconds":
            REVALIDATE_SECONDS,
        "feeds_auto_pipeline":True,
        "orders_placed_by_this_process":False,
    }
    atomic_json(STATUS,report)

    return report


def self_test():
    blocked={
        "symbol":"TEST/EUR",
        "strategy":"trend_breakout",
        "side":"LONG",
        "market_regime":"BULLISH",
        "shadow_eligible":False,
    }
    assert not fully_valid_tb_long(blocked)

    good=dict(blocked)
    good["shadow_eligible"]=True
    assert fully_valid_tb_long(good)

    now=datetime.now(timezone.utc)
    q={
        "items":[
            {
                "symbol":"AAA/EUR",
                "expires_at":
                    "2099-01-01T00:00:00+00:00",
                "last_seen_at":
                    "2026-08-25T10:00:00+00:00",
            },
            {
                "symbol":"AAA/EUR",
                "expires_at":
                    "2099-01-01T00:00:00+00:00",
                "last_seen_at":
                    "2026-08-25T10:01:00+00:00",
            },
        ]
    }

    active=active_symbols(q,now)
    assert list(active)==["AAA/EUR"]
    assert active["AAA/EUR"]["last_seen_at"].endswith(
        "10:01:00+00:00"
    )

    print("HOT WATCH SELF TEST: PASS")


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument(
        "--self-test",
        action="store_true",
    )
    ap.add_argument(
        "--loop",
        action="store_true",
    )
    args=ap.parse_args()

    if args.self_test:
        self_test()
        return

    ex=ccxt.bitvavo({
        "enableRateLimit":True,
        "timeout":30000,
    })
    ex.load_markets()

    cfg=ms.settings(
        ms.load_yaml(ms.CFG_FILE),
        20,
    )

    if not args.loop:
        print(run_once(ex,cfg))
        return

    cycle=0
    while True:
        cycle += 1
        try:
            r=run_once(ex,cfg)

            if (
                r["published"] > 0
                or cycle % 6 == 0
            ):
                print(
                    "HOT WATCH | "
                    f"watch={r['watched']} "
                    f"spread_safe={r['spread_ready']} "
                    f"revalidated={r['revalidated']} "
                    f"published={r['published']}",
                    flush=True,
                )
        except Exception as e:
            print(
                "HOT WATCH FOUT:",
                type(e).__name__,
                e,
                flush=True,
            )

        time.sleep(POLL_SECONDS)


if __name__=="__main__":
    main()
