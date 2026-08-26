# CryptoBot Fresh v1

Volledig nieuwe, afgescheiden basis voor een Bitvavo cryptobot. Deze map gebruikt geen oude Diamond Trader-modules, databases of strategieën.

## Status

PAPER ONLY. Er zit geen private Bitvavo-API, API-key, balans-opvraag of echte orderfunctie in.

## Datastroom

Bitvavo openbare candles -> gesloten-candle filter -> één trend/breakoutstrategie -> paper execution -> SQLite -> status/backtest.

## Strategie v1

BUY vereist tegelijk:
- EMA20 > EMA50;
- close > EMA20;
- RSI14 tussen 52 en 72;
- close boven hoogste close van vorige 20 candles;
- volume minimaal 1,10x mediane volume van vorige 20 candles.

Exit:
- stop 1,6 x ATR;
- take-profit 2,8 x ATR;
- trailing actief vanaf +1,6 x ATR;
- trailing afstand 1,1 x ATR.

## Kostenmodel

Standaard:
- taker fee 0,25% per zijde;
- slippage 0,05% per zijde;
- entry geblokkeerd boven 0,30% spread;
- backtest gebruikt daarnaast 0,10% roundtrip-spreadaanname.

## Belangrijke simulatieregels

- alleen volledig gesloten candles voor signalen;
- backtest-entry pas op de volgende candle-open;
- als stop en target in dezelfde candle geraakt kunnen zijn: stop eerst;
- trailing die door een candle-high opschuift geldt pas daarna;
- cash en positie worden atomair in SQLite geboekt;
- paper-posities overleven een Render-restart.

## Render

Branch: `fresh-start-20260826`

Start command vanaf repository-root:

```bash
cd fresh_v1 && bash start.sh
```

De database gebruikt automatisch `/var/data/cryptobot_fresh.db` als `/var/data` schrijfbaar is.

## Testen

Vanuit `fresh_v1`:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 main.py --once
python3 main.py --status
python3 backtest.py --limit 1000
```

## Veiligheidsgrens

Geen echte orders en geen API-secrets in v1. Een eventuele live executor wordt later apart gebouwd en alleen na expliciete toestemming.
