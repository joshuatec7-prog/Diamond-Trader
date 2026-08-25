#!/usr/bin/env python3
import argparse
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt
import market_scanner as ms

from diamond_liquidity_gate import evaluate_buy_liquidity

SOURCE = Path(
    "/var/data/diamond_marketwide_1m_early_movers.json"
)
STATE = Path(
    "/var/data/diamond_early_mover_liquidity_watch_state.json"
)
PROMOTIONS = Path(
    "/var/data/diamond_early_mover_liquidity_promotions.json"
)
STATUS = Path(
    "/var/data/diamond_early_mover_liquidity_watch_status.json"
)

POLL_SECONDS = 10
WATCH_TTL_SECONDS = 60 * 60
PROMOTION_RETENTION_SECONDS = 20 * 60
MAX_SOURCE_AGE_SECONDS = 180


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,tmp=tempfile.mkstemp(
        dir=str(path.parent),
        prefix="."+path.name,
    )
    try:
        with os.fdopen(
            fd,"w",encoding="utf-8"
        ) as f:
            json.dump(
                data,
                f,
                indent=2,
                ensure_ascii=False,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load(path):
    try:
        d=json.loads(path.read_text())
        return d if isinstance(d,dict) else {}
    except Exception:
        return {}


def parse_dt(value):
    try:
        dt=datetime.fromisoformat(
            str(value).replace("Z","+00:00")
        )
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def stage(
    volume,
    spread,
    orderbook_allow,
    min_volume,
    max_spread,
):
    if volume < min_volume:
        return "WAIT_VOLUME"
    if spread > max_spread:
        return "WAIT_SPREAD"
    if not orderbook_allow:
        return "WAIT_ORDERBOOK"
    return "PROMOTE"


def settings():
    raw=ms.load_yaml(ms.CFG_FILE)
    scan=ms.settings(raw,20)

    return {
        "min_volume":float(
            scan["min_quote_volume"]
        ),
        "max_spread":float(
            scan["max_spread_pct"]
        ),
        "stake":max(
            5.0,
            ms.to_float(
                ms.get_cfg(
                    raw,
                    "risk.fixed_stake_quote",
                    130,
                ),
                130,
            ),
        ),
        "book_depth":max(
            5,
            min(
                1000,
                int(ms.to_float(
                    ms.get_cfg(
                        raw,
                        "execution.liquidity_orderbook_depth",
                        50,
                    ),
                    50,
                )),
            ),
        ),
        "max_impact":ms.to_float(
            ms.get_cfg(
                raw,
                "execution.liquidity_max_price_impact_pct",
                0.15,
            ),
            0.15,
        ),
        "depth_band":ms.to_float(
            ms.get_cfg(
                raw,
                "execution.liquidity_depth_band_pct",
                0.25,
            ),
            0.25,
        ),
        "min_depth_multiple":ms.to_float(
            ms.get_cfg(
                raw,
                "execution.liquidity_min_depth_multiple",
                2.0,
            ),
            2.0,
        ),
    }


def run_once(ex,cfg):
    now=datetime.now(timezone.utc)

    source=load(SOURCE)
    generated=parse_dt(
        source.get("generated_at_utc")
    )
    if generated is None:
        raise RuntimeError(
            "early-mover bron heeft geen geldige timestamp"
        )

    age=(now-generated).total_seconds()
    if age > MAX_SOURCE_AGE_SECONDS:
        raise RuntimeError(
            f"early-mover bron {age:.0f}s oud"
        )

    state=load(STATE)
    watches=state.get("watches")
    if not isinstance(watches,dict):
        watches={}

    # Verlopen watches verwijderen.
    for symbol in list(watches):
        expiry=parse_dt(
            watches[symbol].get("expires_at")
        )
        if expiry is None or expiry <= now:
            del watches[symbol]

    # Nieuwe early movers met onvoldoende volume
    # maximaal één uur blijven volgen.
    for row in source.get("candidates",[]) or []:
        symbol=str(
            row.get("symbol") or ""
        ).upper()
        volume=ms.to_float(
            row.get("volume_quote_24h"),
            0.0,
        )

        if not symbol or volume <= 0:
            continue

        if volume >= cfg["min_volume"]:
            continue

        item=watches.get(symbol)

        if item is None:
            item={
                "symbol":symbol,
                "first_seen_at":now.isoformat(),
                "expires_at":(
                    now
                    + timedelta(
                        seconds=WATCH_TTL_SECONDS
                    )
                ).isoformat(),
            }
            watches[symbol]=item

        item["last_seen_at"]=now.isoformat()
        item["source_priority"]=row.get(
            "priority",0
        )
        item["source_move_1m_pct"]=row.get(
            "move_1m_pct"
        )
        item["source_move_5m_pct"]=row.get(
            "move_5m_pct"
        )
        item["source_move_15m_pct"]=row.get(
            "move_15m_pct"
        )

    promotions=load(PROMOTIONS)
    promotion_items=promotions.get("items")
    if not isinstance(promotion_items,dict):
        promotion_items={}

    # Oude promoties automatisch verwijderen.
    for symbol in list(promotion_items):
        ready_until=parse_dt(
            promotion_items[symbol].get(
                "ready_until"
            )
        )
        if (
            ready_until is None
            or ready_until <= now
        ):
            del promotion_items[symbol]

    tickers=ex.fetch_tickers()

    wait_volume=0
    wait_spread=0
    wait_orderbook=0
    newly_promoted=[]

    for symbol,item in watches.items():
        record=ms.market_record_for_symbol(
            ex,
            tickers,
            symbol,
        )
        if record is None:
            item["status"]="NO_MARKET_RECORD"
            continue

        volume=ms.to_float(
            record.get("quote_volume"),
            0.0,
        )
        spread=ms.to_float(
            record.get("spread_pct"),
            999.0,
        )

        item["last_checked_at"]=now.isoformat()
        item["current_volume"]=volume
        item["current_spread_pct"]=spread

        if volume < cfg["min_volume"]:
            item["status"]="WAIT_VOLUME"
            wait_volume += 1
            continue

        if spread > cfg["max_spread"]:
            item["status"]="WAIT_SPREAD"
            wait_spread += 1
            continue

        # Reeds gepromoveerd: deep-scan krijgt
        # twintig minuten de tijd om hem mee te nemen.
        if symbol in promotion_items:
            item["status"]="PROMOTED"
            continue

        try:
            book=ex.fetch_order_book(
                symbol,
                cfg["book_depth"],
            )
            liquidity=evaluate_buy_liquidity(
                book,
                cfg["stake"],
                max_price_impact_pct=
                    cfg["max_impact"],
                depth_band_pct=
                    cfg["depth_band"],
                min_depth_multiple=
                    cfg["min_depth_multiple"],
            )
        except Exception as exc:
            item["status"]=(
                "ORDERBOOK_ERROR:"
                + type(exc).__name__
            )
            wait_orderbook += 1
            continue

        item["orderbook_reason"]=liquidity.get(
            "reason"
        )
        item["price_impact_pct"]=liquidity.get(
            "estimated_price_impact_pct"
        )
        item["depth_multiple"]=liquidity.get(
            "depth_multiple"
        )

        current_stage=stage(
            volume,
            spread,
            bool(liquidity.get("allow")),
            cfg["min_volume"],
            cfg["max_spread"],
        )

        if current_stage != "PROMOTE":
            item["status"]=current_stage
            wait_orderbook += 1
            continue

        promoted={
            "symbol":symbol,
            "priority":item.get(
                "source_priority",0
            ),
            "move_1m_pct":item.get(
                "source_move_1m_pct"
            ),
            "move_5m_pct":item.get(
                "source_move_5m_pct"
            ),
            "move_15m_pct":item.get(
                "source_move_15m_pct"
            ),
            "volume_quote_24h":volume,
            "spread_pct":spread,
            "liquidity":"PASS",
            "liquidity_promoted":True,
            "promoted_at":now.isoformat(),
            "ready_until":(
                now
                + timedelta(
                    seconds=
                        PROMOTION_RETENTION_SECONDS
                )
            ).isoformat(),
            "orderbook_reason":
                liquidity.get("reason"),
            "estimated_price_impact_pct":
                liquidity.get(
                    "estimated_price_impact_pct"
                ),
            "depth_multiple":
                liquidity.get("depth_multiple"),
        }

        promotion_items[symbol]=promoted
        item["status"]="PROMOTED"
        item["promoted_at"]=now.isoformat()
        newly_promoted.append(promoted)

    state={
        "version":1,
        "updated_at":now.isoformat(),
        "watch_ttl_seconds":
            WATCH_TTL_SECONDS,
        "watches":watches,
    }
    atomic(STATE,state)

    promotion_state={
        "version":1,
        "updated_at":now.isoformat(),
        "retention_seconds":
            PROMOTION_RETENTION_SECONDS,
        "count":len(promotion_items),
        "items":promotion_items,
    }
    atomic(PROMOTIONS,promotion_state)

    report={
        "generated_at":now.isoformat(),
        "watched":len(watches),
        "wait_volume":wait_volume,
        "wait_spread":wait_spread,
        "wait_orderbook":wait_orderbook,
        "promoted_active":
            len(promotion_items),
        "new_promotions":
            len(newly_promoted),
        "min_volume":
            cfg["min_volume"],
        "max_spread":
            cfg["max_spread"],
        "stake":
            cfg["stake"],
        "orders_placed":False,
    }
    atomic(STATUS,report)

    return report


def self_test():
    assert stage(
        100000,
        0.05,
        True,
        250000,
        0.25,
    ) == "WAIT_VOLUME"

    assert stage(
        300000,
        0.30,
        True,
        250000,
        0.25,
    ) == "WAIT_SPREAD"

    assert stage(
        300000,
        0.05,
        False,
        250000,
        0.25,
    ) == "WAIT_ORDERBOOK"

    assert stage(
        300000,
        0.05,
        True,
        250000,
        0.25,
    ) == "PROMOTE"

    print(
        "LIQUIDITY PROMOTION SELF TEST: PASS"
    )


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

    cfg=settings()

    ex=ccxt.bitvavo({
        "enableRateLimit":True,
        "timeout":30000,
    })
    ex.load_markets()

    if not args.loop:
        print(run_once(ex,cfg))
        return

    cycle=0

    while True:
        cycle += 1
        try:
            r=run_once(ex,cfg)

            if (
                r["new_promotions"] > 0
                or cycle % 6 == 0
            ):
                print(
                    "LIQUIDITY WATCH | "
                    f"watch={r['watched']} "
                    f"volume={r['wait_volume']} "
                    f"spread={r['wait_spread']} "
                    f"orderbook={r['wait_orderbook']} "
                    f"promoted={r['promoted_active']} "
                    f"nieuw={r['new_promotions']}",
                    flush=True,
                )
        except Exception as exc:
            print(
                "LIQUIDITY WATCH FOUT:",
                type(exc).__name__,
                exc,
                flush=True,
            )

        time.sleep(POLL_SECONDS)


if __name__=="__main__":
    main()
