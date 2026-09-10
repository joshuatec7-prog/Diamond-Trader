# CryptoBot Clean-Room

## v3.9 volledige brede menselijke keten

`autonomous_v39.py` koppelt de brede v3.8-ontdekking daadwerkelijk aan alle
v3.7-veiligheids-, markt-, setup-, L2-, kosten- en portefeuillerisicopoorten.
Alleen discovery-scores vanaf 65 en maximaal tien markten per cyclus worden
doorgelaten. De laag blijft observe-only, meet prospectief na 15, 60 en 240
minuten met uitvoerbare publieke L2-prijzen en heeft één vast beslismoment:
72 uur verzamelen plus 4 uur voor de laatste 240m-uitkomsten. Daarna volgt
`PAPER_GO` of `AFWIJZEN_GEEN_VERLENGING`; automatische activatie bestaat niet.

## v3.8 brede menselijke ontdekking

`autonomous_v38.py` volgt iedere minuut alle actieve Bitvavo EUR-markten met één
lichte publieke marktscan. De observe-only laag bewaart 65 minuten prijshistorie,
herkent bevestigde vroege bewegingen, blokkeert illiquide markten en achter een
doorgeschoten pump aanlopen, en maakt maximaal tien kandidaten klaar voor latere
beoordeling door de volledige v3.7-jury en L2-laag. De laag bevat geen positie- of
ordercode en kan geen PAPER- of live-order uitvoeren.

Clean-room crypto trading research project for Bitvavo public EUR market data.

## Safety

- PAPER ONLY.
- Geen private API-authenticatie.
- Geen echte orderfunctie.
- Geen bestaande Bitvavo-balansen worden gebruikt.

## Nieuwe read-only onderzoekslaag

`autonomous_v37.py` is de afzonderlijke v3.7-observatielaag. Fase 1 draait bewust uitsluitend in
`OBSERVE_ONLY`: er bestaan geen positie- of ordertabellen en de laag kan dus geen PAPER- of live-transactie
uitvoeren. v3.6 blijft ondertussen ongewijzigd als controlegroep draaien. v3.7 beoordeelt hetzelfde volledige
EUR-universum via opeenvolgende harde poorten voor veiligheidsgrenzen, dataintegriteit, marktcontext,
setup-specifieke regels, een L2-meetvenster, netto voordeel na kosten en portefeuillerisico. Een blokkade kan
niet door punten uit een ander onderdeel worden gecompenseerd.

Breakout, pullbackhervatting en volumehervatting hebben afzonderlijke voorwaarden. Bitcoin wordt op 5m,
15m en 1h beoordeeld. Een regimewisseling krijgt eerst de zichtbare toestand `TRANSITION` en moet door een
tweede 5m-cyclus worden bevestigd. Maximaal vijf technische kandidaten krijgen drie uitvoerbare
€500-orderboekmetingen verspreid over minimaal 60 seconden; mediane én slechtste spread, orderboekdruk en
VWAP-stabiliteit moeten slagen. Pas daarna kan `SCHADUW-KANS` worden geregistreerd. De eigen database is
`cryptobot_autonomous_v37.db`; het kapitaalmodel blijft €3.000 start, €500 per positie, maximaal vijf en
minimaal €200 reserve. Bestaande munten zijn uitgesloten. De risicogrenzen van €45 open gepland risico,
maximaal twee posities per correlatiecluster, twee nieuwe posities per cyclus en €45 dagverlies zijn in deze
fase alleen onderzoekshypothesen en worden nog niet toegepast op geld of PAPER-posities.

`autonomous_v36.py` is de derde, volledig autonome PAPER-portefeuille. Zij beoordeelt bij iedere nieuw
gesloten 5m-candle alle 20 EUR-markten, gebruikt 15m en 1h als context en laat acht zichtbare juryonderdelen
stemmen over trend, timing, volume, Bitcoin, L2, netto risico/opbrengst, anti-pump en datakwaliteit.
Actieve kandidaten krijgen iedere 60 seconden een nieuwe publieke L2-controle; open posities worden iedere
30 seconden met exact de gekochte PAPER-muntomvang bewaakt. De portefeuille start afzonderlijk met €3.000,
gebruikt €500 per positie, maximaal vijf posities en bewaart altijd minimaal €200 cash. Wekelijkse
PAPER-stortingen van €50 staan apart in het kasboek en tellen nooit als handelswinst. Bestaande munten zijn
uitgesloten: v3.6 simuleert uitsluitend nieuwe aankopen vanuit de eigen EUR-cash. Alle beslissingen,
afwijzingen, PAPER-uitvoeringen, dataproblemen en bewegingen na een afwijzing staan in de eigen database
`cryptobot_autonomous_v36.db`. Live-uitvoering is technisch niet aanwezig.

`crypto_scanner_v2.py` bevat nu scanner v3.5. Een handmatig kanslabel vereist een uitvoerbare €200-L2-VWAP,
een actuele uitvoerprijs binnen de besliszone en een netto risico/opbrengst van minimaal 1,50. Een USDC-route
wordt inclusief EUR↔USDC-omwisseling beoordeeld. Alle kanslabels en hun latere stop/target/timeout-uitkomst
worden prospectief opgeslagen in `cryptobot_scanner_v3.db`. De bestaande kandidaat-snapshots worden nu ook
in de status uitgelezen: WATCH-momenten, zeldzame kansen en overlappende afwijsredenen zijn over de laatste
24 uur zichtbaar. Vanaf v3.1 worden daarvoor ook regime, strategiebeslissing, kostenruimte en spread bewaard.
Vanaf v3.2 loopt daarnaast een afzonderlijke praktische PAPER-test. Iedere LONG WATCH, SIDEWAYS WATCH of
zeldzame LONG wordt fictief voor €200 tegen de L2-koop-VWAP geopend. Vanaf v3.3 worden open posities los
van de 15-minutenscan iedere 30 seconden via de actuele L2-verkoop-VWAP uit het publieke orderboek bewaakt.
De test gebruikt een netto stop van -3%, activeert winstbeveiliging vanaf +1%
en laat daarna maximaal 1 procentpunt teruglopen met minimaal +0,25% als winstslot. De 48-uurslimiet geldt
alleen zolang trailing nog niet actief is; een actieve runner blijft meelopen tot de winstgrens wordt geraakt.
Fees en vaste slippage worden naast de echte orderboekprijzen afgetrokken; echte orders blijven onmogelijk.

Vanaf v3.5 blijft deze v3.4-portefeuille ongewijzigd als controlegroep en loopt binnen hetzelfde read-only
proces een afzonderlijke menselijke PAPER-challenger. Die maakt uit de 15m/1h-scan maximaal vijf kansrijke
EUR-longs en controleert die iedere minuut opnieuw met gesloten 5m-candles en een actuele L2-uitvoerprijs.
Een PAPER-instap vereist een 5m-breakout, pullbackhervatting of volumehervatting, voldoende volume,
geen extreme koersuitrekking, geen plotselinge Bitcoinmarktschok, acceptabele nabije bied/laatdruk,
voldoende kostenruimte en een actuele netto risico/opbrengst. Iedere instap of blokkade wordt met alle
redenen in SQLite opgeslagen. De challenger heeft een eigen virtueel kapitaal en kan de v3.4-resultaten
daarom eerlijk vergelijken. Nieuws is bewust nog geen automatische factor: zonder betrouwbare bron staat
dit expliciet als ontbrekende menselijke controle in de status in plaats van dat de bot zekerheid simuleert.

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
- De v3.4-basis stapt op de 15m-context in; de v3.5-challenger zoekt iedere minuut een gesloten 5m-trigger.
- Open PAPER-posities van beide portefeuilles worden iedere 30 seconden bewaakt.
- De uitstap-VWAP gebruikt exact hetzelfde aantal munten als bij de PAPER-instap.
- Positieomvang, maximaal aantal gelijktijdige posities en beschikbaar PAPER-geld worden afgedwongen.
- Na een uitstap geldt per munt vier uur afkoeling om kunstmatige kostenchurn te voorkomen.
- De status toont cash, equity, drawdown en of er genoeg trades en testdagen zijn voor beoordeling.
- Scanner v3.5, fundingmonitor v4.1, autonome PAPER-worker v3.6 en v3.7 observe-only draaien; oudere strategieën blijven bewaard maar gestopt.

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

CryptoBot v3.6:

```bash
python3 autonomous_v36.py --status
```

CryptoBot v3.7 observe-only:

```bash
python3 autonomous_v37.py --status
```

