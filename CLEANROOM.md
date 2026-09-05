# CryptoBot Clean-Room

Deze branch is een schone PAPER-only herstart. De runtime bevat geen private Bitvavo-authenticatie en geen echte orderfunctie.

## Vaste uitgangspunten

- `RUN_MODE=PAPER` is verplicht.
- Alleen publieke Bitvavo-marktdata wordt gebruikt.
- De eerste EUR-marktselectie wordt op actueel 24-uurs quotevolume gemaakt en daarna in SQLite vastgezet.
- Het huidige vaste universe bestaat uit 20 liquide EUR-markten.
- Interval is 15 minuten.
- Nieuwe entries worden alleen op gesloten 15m-candles beoordeeld.
- Open PAPER-posities worden iedere 30 seconden met exacte muntomvang en actuele L2-diepte bewaakt.
- Paper startkapitaal en bestaande Bitvavo-saldi staan volledig los van elkaar.
- Fees, slippage en spread worden expliciet meegenomen.

## Strategy A — Mean Reversion

De oorspronkelijke statistische lower-band re-entry blijft ongewijzigd en behoudt zijn bestaande prospectieve database `cryptobot_cleanroom.db`.

Entry:
- vorige close onder de lower band;
- laatste close terug boven de lower band;
- laatste close nog onder de middle band.

## Strategy B — Trend Momentum

Strategy B draait parallel in een aparte prospectieve PAPER-database `cryptobot_cleanroom_trend.db`. De regels zijn vooraf vastgezet:

- SMA 12 boven SMA 48;
- SMA 48 stijgt minimaal 0,15% over 8 bars;
- 4-bar momentum minimaal +0,30% en maximaal +6,00%;
- laatste close breekt boven de hoogste close van de vorige 8 bars.

De maximale momentumgrens voorkomt blind najagen van een extreme pump. Execution, stake, fees, slippage, spreadfilter, stop-loss, take-profit en maximale houdtijd zijn gelijk aan Strategy A, zodat het verschil in eerste instantie uit de entrylogica komt.

## Veiligheid en integriteit

- Geen private API-key/secret in runtimecode.
- Geen `/order`-endpoint of create/cancel-orderfunctie.
- SQLite quick-check en schema-versie zijn onderdeel van status/readiness.
- Een positie kan niet dubbel gesloten en gecrediteerd worden.
- Prospectieve trade-tijden gebruiken wall-clock runtime-tijd.
- Intracycle stop-loss gebruikt de actuele biedprijs als uitvoerbare referentie.
- Strategy A en B hebben ieder eigen cash, posities, trades, beslissingen en performance.
- De lean runtime start alleen scanner v3 en fundingmonitor v4.1; beide zijn read-only.
- `supervisor.py` bewaakt proces én rapportleeftijd en herstart alleen de ongezonde monitor.

## Evaluatie

Beide strategieën worden apart beoordeeld op:

- minimaal 40 gesloten trades;
- minimaal 14 meetdagen als kwaliteitsgrens;
- netto PnL positief;
- profit factor minimaal 1,25;
- maximale drawdown maximaal 10%.

Tussenbeoordelingen kunnen eerder plaatsvinden bij 10 en 20 gesloten trades; de databases worden daarvoor niet gereset.
