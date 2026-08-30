# CryptoBot Clean-Room

Clean-room crypto trading research project for Bitvavo public EUR market data.

## Safety

- PAPER ONLY.
- Geen private API-authenticatie.
- Geen echte orderfunctie.
- Geen bestaande Bitvavo-balansen worden gebruikt.

## Nieuwe read-only onderzoekslaag

`crypto_scanner_v2.py` blijft de strenge scanner voor zeldzame handmatige kansen. Daarnaast draait nu
`crypto_research_v4.py`: een trage, prospectieve schaduwtest die uitsluitend BTC-USDC en ETH-USDC volgt.

V4 bevriest iedere zondag om 00:00 UTC één beslissing voor de hele week:

- alleen een long-schaduwpositie wanneer de laatste volledige dagslotkoers boven SMA65 ligt;
- anders blijft dat deel in USDC-cash;
- actieve munten krijgen een gelijk basisgewicht;
- bij een gemiddelde 20-daagse gerealiseerde volatiliteit boven 80% wordt de totale blootstelling verlaagd;
- geen short, leverage, tussentijdse herweging of echte orderfunctie.

De database vergelijkt v4 vanaf de start met USDC-cash, 50/50 BTC/ETH buy-and-hold en wekelijkse DCA.
Ook worden 2x- en 3x-kostenstresstests bijgehouden. Een eerste oordeel volgt pas na minimaal 26 volledige weken.

Controle:

```bash
python3 crypto_research_v4.py --status
```

`funding_basis_monitor.py` blijft versie 3, maar vereist nu minimaal 72 uur stabiele historie voor een
`CARRY WATCH`. Het rapport toont daarnaast 30- en 90-daagse gemiddelden, tekenwisselingen, fundingverval,
2x transactiekosten, een ongunstige basisschok van 1% en een expliciete waarschuwing dat marge- en
beursrisico altijd handmatig moeten worden beoordeeld.

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

