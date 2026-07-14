#!/usr/bin/env python3
"""
simulate_target.py — Wat zou een andere sell-target hebben opgeleverd?

Leest je grid_transactions.csv, haalt de echte 1m-prijshistorie van Bitvavo op,
en simuleert per kandidaat-target: is die target geraakt VOOR de stop-loss?

Draaien:  python3 tools/simulate_target.py

Belangrijk: dit simuleert alleen de trades die je bot daadwerkelijk heeft
geopend. Tweede-orde effecten (langer open = minder ruimte binnen max 4
posities = minder trades) zitten er NIET in. Zie de disclaimer onderaan.
"""

import csv
import os
import sys
import time
from datetime import datetime, timezone

import ccxt

# ---------------------------------------------------------------- CONFIG
CSV_PATH   = "/var/data/grid_transactions.csv"
STAKE      = 125.0          # inleg per trade
STOP_PCT   = 3.0            # huidige stop-loss
FEE_PCT    = 0.25           # Bitvavo fee per kant (koop + verkoop)
TARGETS    = [0.8, 1.2, 1.5, 2.0, 2.5]   # kandidaat sell-targets in %
TIMEFRAME  = "1m"

# Bij twijfel (target en stop in dezelfde candle) nemen we de stop.
# Pessimistisch = eerlijk. Anders lieg je tegen jezelf.
PESSIMISTIC = True

# ------------------------------------------------- CSV KOLOM-HERKENNING
# We weten je exacte kolomnamen niet, dus we proberen varianten.
ALIASES = {
    "ts":     ["timestamp", "time", "datetime", "datum", "tijd", "date"],
    "pair":   ["pair", "symbol", "market", "markt", "coin"],
    "side":   ["side", "action", "type", "actie", "kant"],
    "price":  ["price", "prijs", "koers"],
}


def find_col(header, key):
    """Zoek welke kolomnaam bij 'key' hoort."""
    low = [h.strip().lower() for h in header]
    for alias in ALIASES[key]:
        if alias in low:
            return header[low.index(alias)]
    return None


def parse_ts(value):
    """Timestamp kan ISO-string of epoch (sec/ms) zijn. Beide aankunnen."""
    v = str(value).strip()
    # epoch?
    try:
        num = float(v)
        if num > 1e12:      # milliseconden
            return int(num)
        if num > 1e9:       # seconden
            return int(num * 1000)
    except ValueError:
        pass
    # ISO-achtig
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.strptime(v[:26], fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def load_buys(path):
    """Haal alle koop-events uit de CSV."""
    if not os.path.exists(path):
        sys.exit(f"FOUT: {path} bestaat niet.")

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        if not header:
            sys.exit("FOUT: CSV heeft geen header.")

        cols = {k: find_col(header, k) for k in ALIASES}
        missing = [k for k, v in cols.items() if v is None]
        if missing:
            print("FOUT: kon deze kolommen niet vinden:", missing)
            print("Gevonden header:", header)
            print("Pas ALIASES bovenaan dit script aan.")
            sys.exit(1)

        buys = []
        for row in reader:
            side = str(row[cols["side"]]).strip().lower()
            if side not in ("buy", "koop", "b"):
                continue
            ts = parse_ts(row[cols["ts"]])
            try:
                price = float(row[cols["price"]])
            except (TypeError, ValueError):
                continue
            if ts is None or price <= 0:
                continue
            buys.append({
                "pair":  str(row[cols["pair"]]).strip().upper(),
                "ts":    ts,
                "price": price,
            })
    return buys


def fetch_candles(ex, pair, since_ms, until_ms):
    """Haal candles op in blokken van 1440. Eén keer per coin, dan cachen."""
    out, cur = [], since_ms
    tf_ms = ex.parse_timeframe(TIMEFRAME) * 1000
    while cur < until_ms:
        try:
            batch = ex.fetch_ohlcv(pair, TIMEFRAME, since=cur, limit=1440)
        except Exception as e:
            print(f"  ! fetch-fout {pair}: {e}")
            break
        if not batch:
            break
        out.extend(batch)
        nxt = batch[-1][0] + tf_ms
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(ex.rateLimit / 1000)
    return out


def simulate(entry, entry_ms, candles, target_pct):
    """Loop door de candles: raakt target of stop het eerst?"""
    tp = entry * (1 + target_pct / 100)
    sl = entry * (1 - STOP_PCT / 100)
    for ts, o, h, l, c, v in candles:
        if ts < entry_ms:
            continue
        hit_stop, hit_tp = l <= sl, h >= tp
        if hit_stop and hit_tp:
            return ("stop", sl) if PESSIMISTIC else ("target", tp)
        if hit_stop:
            return "stop", sl
        if hit_tp:
            return "target", tp
    return "open", None


def pnl(entry, exit_price):
    """Netto PnL na fees aan beide kanten."""
    f = FEE_PCT / 100
    amount = STAKE / entry
    proceeds = amount * exit_price
    return proceeds * (1 - f) - STAKE * (1 + f)


def main():
    buys = load_buys(CSV_PATH)
    if not buys:
        sys.exit("Geen koop-events gevonden in de CSV.")

    pairs = sorted({b["pair"] for b in buys})
    now_ms = int(time.time() * 1000)
    print(f"{len(buys)} koop-events over {len(pairs)} coins gevonden.\n")

    ex = ccxt.bitvavo({"enableRateLimit": True})

    # Candles ophalen: één keer per coin, vanaf de eerste koop tot nu.
    cache = {}
    for p in pairs:
        first = min(b["ts"] for b in buys if b["pair"] == p)
        print(f"Candles ophalen {p} ...", flush=True)
        cache[p] = fetch_candles(ex, p, first, now_ms)
        print(f"  {len(cache[p])} candles")
    print()

    # Break-even winrate per target uitrekenen (puur wiskundig).
    print("=== BREAK-EVEN WINRATE PER TARGET ===")
    for t in TARGETS:
        win = pnl(100.0, 100.0 * (1 + t / 100))
        loss = abs(pnl(100.0, 100.0 * (1 - STOP_PCT / 100)))
        be = loss / (loss + win) * 100
        print(f"  target {t:>4.1f}% | win €{win:+.2f} | verlies €{-loss:.2f} "
              f"| break-even winrate {be:.1f}%")
    print()

    # De echte simulatie.
    print("=== SIMULATIE OP ECHTE PRIJSHISTORIE ===")
    results = {}
    for t in TARGETS:
        per_coin = {}
        for b in buys:
            candles = cache.get(b["pair"], [])
            if not candles:
                continue
            outcome, exit_price = simulate(b["price"], b["ts"], candles, t)
            d = per_coin.setdefault(b["pair"], {"win": 0, "stop": 0, "open": 0, "pnl": 0.0})
            if outcome == "open":
                d["open"] += 1
            else:
                d["win" if outcome == "target" else "stop"] += 1
                d["pnl"] += pnl(b["price"], exit_price)
        results[t] = per_coin

        tot_w = sum(d["win"] for d in per_coin.values())
        tot_s = sum(d["stop"] for d in per_coin.values())
        tot_o = sum(d["open"] for d in per_coin.values())
        tot_p = sum(d["pnl"] for d in per_coin.values())
        closed = tot_w + tot_s
        wr = (tot_w / closed * 100) if closed else 0
        flag = "  <-- huidige instelling" if abs(t - 0.8) < 0.01 else ""
        print(f"\n  TARGET {t}%{flag}")
        print(f"    gesloten: {closed} | wins: {tot_w} | stops: {tot_s} "
              f"| nog open: {tot_o}")
        print(f"    winrate: {wr:.1f}% | totaal PnL: €{tot_p:+.2f}")
        for coin in sorted(per_coin):
            d = per_coin[coin]
            c = d["win"] + d["stop"]
            cwr = (d["win"] / c * 100) if c else 0
            print(f"      {coin:<9} trades={c:<4} winrate={cwr:>5.1f}% "
                  f"pnl=€{d['pnl']:+.2f}")

    print("\n" + "=" * 60)
    print("SANITY CHECK: vergelijk de regel voor target 0.8% met je")
    print("echte cijfers (83.1% winrate, -€147.93 over 308 trades).")
    print("Wijkt dat ver af? Dan klopt het model niet en zijn de andere")
    print("targets ook onbetrouwbaar. Eerst dat oplossen.")
    print()
    print("NIET meegesimuleerd: bij een hogere target blijven posities")
    print("langer open, dus de max-4-limiet blokkeert vaker nieuwe buys.")
    print("Het echte aantal trades zal dus lager liggen dan hier.")
    print("=" * 60)


if __name__ == "__main__":
    main()
