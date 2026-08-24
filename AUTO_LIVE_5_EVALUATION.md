# AUTO LIVE 5 evaluatiepunten

Doel: deze punten pas beoordelen nadat de lopende AUTO LIVE 5-proef 5 bevestigde automatische BUYs heeft afgerond. Tijdens de proef geen strategie-, stake- of exitwijzigingen doen, tenzij er een echte safety- of technische fout wordt gevonden.

## Vast evaluatiepunt: stopafstand bij volatiele coins

Vergelijk per trade de procentuele afstand tussen entry en daadwerkelijke stop/verkoop.

Bekende vergelijking:
- XRP/EUR, 22 aug 2026: aankoop circa EUR 130,39 rond 1,2842; verkoop circa EUR 128,55 rond 1,2724. Koersdaling circa 0,92%; totaalverlies ongeveer EUR 1,84 inclusief fees.
- PUMP/EUR, 23 aug 2026: aankoop circa EUR 130,33 rond 0,004573; verkoop circa EUR 126,41 rond 0,004458. Koersdaling circa 2,51%; totaalverlies ongeveer EUR 3,92 inclusief fees.

Onderzoek na 5/5 expliciet of de ATR-gebaseerde stop-loss bij zeer volatiele coins te ruim wordt en daardoor onnodig grote euroverliezen veroorzaakt. Vergelijk dit met minder volatiele trades zoals XRP.

## Nacht-audit 23/24 augustus 2026: volledige Bitvavo EUR-replay

Research-only historische replay uitgevoerd over alle actieve Bitvavo EUR-markten om te controleren of sterke stijgers winstgevende kansen opleverden die Diamond Trader niet had opgeslagen of niet als SELECTIVE toeliet.

Resultaat volledige replay:
- actieve EUR-markten: 426
- volledig verwerkt: 389
- onvoldoende historie: 37
- API/datafouten: 0
- robuuste LONG-kandidaten: 36
- coverage-misses: 24
- daarvan huidige SELECTIVE LONG trend_breakout: 3
- daarvan niet-SELECTIVE: 21
- gemiste kandidaten winners/losers: 1/23
- losse som van alle gemiste signalen: circa EUR -61,35; dit is nadrukkelijk geen portfolio-PnL omdat signalen kunnen overlappen

De drie gemiste huidige SELECTIVE LONG-signalen waren PORTAL/EUR (2x) en ENA/EUR (1x). Een aparte 1m-eindcontrole bevestigde voor alle drie een stop-loss:
- PORTAL/EUR: RR 1,25; stop-loss; circa EUR -2,56 bij EUR 130
- PORTAL/EUR: RR 1,22; stop-loss; circa EUR -2,49 bij EUR 130
- ENA/EUR: RR 1,27; stop-loss; circa EUR -2,62 bij EUR 130

Conclusie voor deze nacht: ondanks veel sterke 24h-stijgers is in de replay geen winstgevende huidige SELECTIVE LONG-kans gevonden die door scannerdekking is gemist. Het simpelweg toelaten van alle momentum/pullback LONG-signalen werd door deze nacht evenmin ondersteund: van de 24 coverage-misses was slechts één kandidaat winstgevend.

Belangrijke beperking: voor signalen die destijds niet door de scanner zijn opgeslagen is de exacte historische bid/ask-spread niet beschikbaar. Replay-kandidaten moesten daarom economisch geldig blijven bij de huidige maximale trade-spread van 0,10%; een kandidaat is alleen werkelijk uitvoerbaar geweest als de echte spread op dat moment ook <= 0,10% was.

Tijdens AUTO LIVE 5 niets aan de strategie aanpassen op basis van deze ene nacht. Gebruik dit resultaat als evaluatiebewijs na 5/5 en vergelijk met andere marktregimes/dagen voordat een extra LONG-route of scannerwijziging wordt overwogen.

## Na 5/5 minimaal rapporteren

- totaal netto PnL
- W/L
- profit factor
- fees
- slippage
- signal-to-BUY timing
- exit reason per trade
- holding time per trade
- entry-to-stop afstand in procenten
- resultaat per coin/regime/route
- safety/recovery gebeurtenissen
- coverage van sterke movers versus gemiste geldige SELECTIVE-signalen

Daarna pas beslissen of ATR-stop, maximale stopafstand, extra LONG-routes, scannerdekking of andere entry/exitregels aangepast moeten worden.
