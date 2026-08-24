# Diamond Trader - NEXT PLAN

Datum: 2026-08-24
Doel: schone start voor nieuwe ChatGPT-projectchat; punt voor punt afwerken zonder lopende proef onderweg te wijzigen.

## Huidige vaste LIVE-proef

AUTO LIVE 5 loopt met maximaal 5 bevestigde automatische BUYs totaal.

Vaste grenzen tijdens proef:
- maximaal EUR 130 per BUY
- maximaal 1 open LIVE-positie
- alleen LONG trend_breakout
- geen echte SHORTs
- alle bestaande safety/liquidity/recovery-gates blijven actief
- na 5 bevestigde BUYs hard stoppen en evalueren
- tijdens de 5-trade proef geen strategie-, stake- of exitwijzigingen behalve bij echte safety/technische fout

Laatst bekende AUTO-status:
- 1/5 bevestigd afgerond
- eerste AUTO-trade: PUMP/EUR, netto ongeveer EUR -3.92
- eerdere handmatige LIVE XRP/EUR ongeveer EUR -1.84

AUTO-evaluatiepunten staan in `AUTO_LIVE_5_EVALUATION.md`.

## Recente onderzoeksbevindingen

### Volledige nacht-audit 23-08 22:00 -> 24-08 07:00 NL
- 426 actieve EUR-markten
- 389 markten voldoende 15m-historie
- 36 robuuste LONG-kandidaten
- 24 coverage-misses
- 3 daarvan huidige SELECTIVE LONG trend_breakout
- PORTAL 00:30 -> stop, circa EUR -2.56, RR circa 1.22
- PORTAL 05:00 -> stop, circa EUR -2.49, RR circa 1.25
- ENA 06:30 -> stop, circa EUR -2.62, RR circa 1.27
- 1m-eindcontrole bevestigde alle drie als STOP_LOSS
- conclusie: geen winstgevende huidige SELECTIVE LONG gevonden die die nacht door scannerdekking werd gemist

Research branch: `research/full-market-overnight-replay-20260824`
Script: `diamond_full_market_overnight_replay.py`

### Eerste SHORT_RIDE_LONG test
Doel was korte 1m/5m stijgingsritten pakken door direct momentum te volgen.

Beschikbare 1m-historie: 149/426 markten; 277 onvoldoende historie; 0 API/datafouten.

MAX 1 positie tegelijk:
- FAST: n=28, W/L 9/19, PnL EUR -22.09, PF 0.20, stress EUR -25.73
- BALANCED: n=27, W/L 11/16, PnL EUR -21.27, PF 0.27, stress EUR -24.78
- STRICT: n=21, W/L 8/13, PnL EUR -14.87, PF 0.37, stress EUR -17.60

Conclusie: direct achter snelle 1m/5m stijging aan kopen valt af. Niet verder finetunen op dezelfde nacht om overfitting te voorkomen.

Research branch: `research/short-ride-replay-20260824`
Script: `diamond_short_ride_replay.py`

### Werkelijke fee-audit
De ongeveer EUR 0.08 fees waren bij circa EUR 30 inzet; de circa EUR 0.32-0.33 fees horen bij circa EUR 130 inzet.

Normaal effectieve fee in echte logs rond 0.25% per kant.
Dus bij EUR 130 ongeveer EUR 0.65 roundtrip fees voor BUY + SELL, exclusief spread/slippage.
Eerste oude GALA-logging toont afwijkend circa 0.50%; later GALA is weer circa 0.27%. Behandelen als oud logging/accounting-onderzoekspunt, niet als bewijs voor coin-afhankelijke normale fees.

## Onderzoeksroadmap - deze volgorde aanhouden

1. BURST -> PULLBACK -> RECLAIM
   - snelle stijging detecteren
   - niet direct kopen
   - korte pullback/stabilisatie afwachten
   - pas instappen als stijging opnieuw wordt hervat
   - eerst historische falsificatietest op 1m/5m
   - EUR 130 en volledige kosten meenemen

2. Winnaars versus verliezers onderzoeken
   - PROM-achtige doorzetters vergelijken met terugvallers zoals PENGU/AAVE
   - volumeversnelling, prijsversnelling, highs/lows, ATR/extensie, RSI, 15m/1h-trend, afstand recente high, tijdstip in beweging

3. ORDER_BOOK_IMBALANCE_ENTRY_GATE
   - 5-30 sec orderboekdruk, spread, adverse selection
   - research/shadow-only

4. Andere beurzen als vroege waarschuwing
   - Binance/Kraken versus Bitvavo
   - alleen gebruiken als vooraf aantoonbaar voorspellend

5. EVENT_RATE_REGIME_GATE
   - prijs-eventfrequentie/tijd tussen events op circa 15m en 1h

6. Entry timing
   - direct versus 1m/5m bevestiging, kwartiergrens, pullback versus breakout

7. Exit verbeteren - pas serieus na AUTO LIVE 5
   - ATR-stopafstand per coin
   - maximale procentuele stopafstand
   - trailing/winst vasthouden

8. Kleine netto winsten onderzoeken
   - fee van circa EUR 0.65 roundtrip bij EUR 130 meenemen

9. Maker/limit execution
   - fee/fill verbeteren versus gemiste fills

10. Dynamische kostenfilter opnieuw beoordelen voor nieuwe routes

11. Liquiditeit/spreadstabiliteit per markt

12. Marktdekking verbeteren zonder API-overbelasting

13. Prospectief 1m-data verzamelen voor markten met onvoldoende historische data

14. Prospectieve SHORT_RIDE/BURST collector als punt 1 potentie toont

15. Marktregimes apart beoordelen: bull, sideways, bear

16. AUTO LIVE 5 afronden en daarna volledige 5/5-evaluatie

17. Alleen bewezen strategieen naast elkaar toelaten

18. Capital allocator pas nadat strategieen bewezen zijn

19. Langere prospectieve validatie over verschillende marktregimes

20. Vaste beslisregel voor elk nieuw idee:
   - verdient dit netto geld?
   - blijft het goed met extra kosten/slippage?
   - werkt het prospectief?
   - is het beter dan wat we al hebben?
   - voegt het nieuw risico toe?

## Eerstvolgende actie in nieuwe chat

Begin bij punt 1: `BURST -> PULLBACK -> RECLAIM`.
Maak eerst 1 research-only historische falsificatietest. Geen deploy en geen wijziging aan AUTO LIVE 5.

## Werkwijze voor shell en deploy

- punten 1 voor 1 afwerken
- korte plakblokken
- boven en onder rode marker `===== PLAK VANAF HIER =====` / `===== EINDE TERUGPLAKKEN =====`
- altijd expliciet `DEPLOY: NEE` of `DEPLOY: JA`
- bij deploy doet gebruiker zelf Render `Manual Deploy -> Deploy latest commit`
- geen handmatig zoeken/plakken in bestanden: complete vervangingsfile of automatische patch
- geen live/configwijzigingen zonder expliciete goedkeuring
