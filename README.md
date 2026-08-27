# CryptoBot Clean-Room

Clean-room crypto trading research project for Bitvavo public EUR market data.

## Safety

- PAPER ONLY.
- Geen private API-authenticatie.
- Geen echte orderfunctie.
- Geen bestaande Bitvavo-balansen worden gebruikt.

## Runtime

- Interval: 15 minuten.
- Universe: 20 liquide EUR-markten, eenmaal geselecteerd op actueel 24-uurs quotevolume en daarna vastgezet.
- Open paperposities worden tussen candle-closes door bewaakt met actuele bid/ask.
- Strategy A en Strategy B draaien parallel, ieder met een eigen PAPER-database en fictief kapitaal.

## Strategy A — Mean Reversion

De oorspronkelijke lower-band re-entry strategie blijft ongewijzigd. Zij wacht op een koers die eerst onder de statistische lower band komt en daarna weer boven die band sluit terwijl de koers nog onder de middle band ligt.

Database: `cryptobot_cleanroom.db`.

## Strategy B — Trend Momentum

Strategy B is bedoeld voor stijgende markten en gebruikt vooraf vaste, eenvoudige regels:

- SMA 12 boven SMA 48;
- SMA 48 stijgt over 8 bars;
- 4-bar momentum tussen +0,30% en +6,00%;
- laatste close breekt boven de hoogste close van de vorige 8 bars.

De bovengrens op momentum voorkomt dat een extreme pump blind wordt nagejaagd. Stake, fees, slippage, spreadfilter, stop-loss, take-profit en maximale houdtijd zijn gelijk aan Strategy A, zodat de entries eerlijker vergelijkbaar blijven.

Database: `cryptobot_cleanroom_trend.db`.

## Controle

Strategy A:

```bash
python3 main.py --status
python3 main.py --report
```

Strategy B:

```bash
python3 trend_main.py --status
python3 trend_main.py --report
```

Alle runtime-processen worden gestart en bewaakt door `supervisor.py` via `start.sh`.
