# CryptoBot Clean-Room

Clean-room crypto trading research project for Bitvavo public EUR market data.

## Safety

- PAPER ONLY.
- Geen private API-authenticatie.
- Geen echte orderfunctie.
- Geen bestaande Bitvavo-balansen worden gebruikt.

## Nieuwe read-only onderzoekslaag

`crypto_scanner_v2.py` bevat nu scanner v3.1. Een handmatig kanslabel vereist een uitvoerbare €200-L2-VWAP,
een actuele uitvoerprijs binnen de besliszone en een netto risico/opbrengst van minimaal 1,50. Een USDC-route
wordt inclusief EUR↔USDC-omwisseling beoordeeld. Alle kanslabels en hun latere stop/target/timeout-uitkomst
worden prospectief opgeslagen in `cryptobot_scanner_v3.db`. De bestaande kandidaat-snapshots worden nu ook
in de status uitgelezen: WATCH-momenten, zeldzame kansen en overlappende afwijsredenen zijn over de laatste
24 uur zichtbaar. Vanaf v3.1 worden daarvoor ook regime, strategiebeslissing, kostenruimte en spread bewaard.

`crypto_research_v4.py` blijft als bewaarde broncode beschikbaar, maar draait niet meer in de lean runtime.

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

`funding_basis_monitor.py` is nu versie 4.1. De meetlaag gebruikt voor iedere schaduwleg echte publieke
L2-orderboeken en berekent uitvoerbare VWAP-prijzen voor standaard $200. Bitvavo USDC-spot, Kraken
perpetuals en de USDC/USD-conversie worden afzonderlijk gemeten; ontbrekende of te dunne orderboeken
leveren geen snapshot op. Orderboeken die meer dan 30 seconden uiteenliggen worden eveneens verworpen.
De oude v3-indexreferenties tellen door nieuwe route-ID's niet mee.

De Bitvavo↔Kraken-route blijft fail-closed geblokkeerd voor `CARRY WATCH` en verzamelt alleen onderzoekdata.
Alleen bestaand BTC/ETH-bezit op Kraken kan na minimaal 72 uur een handmatig kanslabel krijgen. Daarvoor
zijn minimaal 260 geldige samples nodig, mag geen meetpauze langer dan 30,5 minuten zijn en moeten zowel
de 2x-kostenstress als de -1%-basisstress positief blijven. De stress gebruikt het gemiddelde en hoogste
uitvoeringskostenniveau uit de volledige 72 uur. Orders blijven onmogelijk.

## Runtime

- Interval: 15 minuten.
- Universe: 20 liquide EUR-markten, eenmaal geselecteerd op actueel 24-uurs quotevolume en daarna vastgezet.
- Open paperposities worden tussen candle-closes door bewaakt met actuele bid/ask.
- Alleen scanner v3 en fundingmonitor v4.1 draaien; de oude PAPER-strategieën blijven bewaard maar gestopt.

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

Alle runtime-processen worden gestart en bewaakt door `supervisor.py` via `start.sh`. De supervisor bewaakt
zowel het proces als de leeftijd en geldigheid van ieder rapport en herstart een ongezonde monitor.

