# Diamond Trader — Master Architecture & Roadmap

Status: projectbesluit / langetermijnarchitectuur
Datum: 2026-08-22

## Hoofddoel

Diamond Trader moet uitgroeien tot een zo compleet mogelijke, veilige, meetbare en uitbreidbare tradingbot die meerdere marktomstandigheden tegelijk kan herkennen en per coin de juiste strategie kan kiezen.

De bot wordt nadrukkelijk niet één strategie die altijd hetzelfde doet. De uiteindelijke architectuur bestaat uit lagen: algemene marktstatus, coin-regime, strategie-selectie, execution intelligence, exits, risk management, portfolio/capital allocation, exchange-safety, monitoring en research/shadow-validatie.

Nieuwe functies gaan niet rechtstreeks live. Nieuwe logica wordt eerst read-only/research of shadow, daarna eventueel paper en pas na duidelijke positieve resultaten via een beperkte canary naar live.

## Kernprincipes

1. Fail-closed bij twijfel, ontbrekende kritieke data of safety-afwijkingen.
2. Geen nieuwe live-risico's zonder expliciete approval.
3. Strategie en execution gescheiden houden.
4. Kosten, spread, slippage, liquiditeit en eventuele borrow-kosten altijd meenemen.
5. Prospectieve resultaten wegen zwaarder dan mooie historische backtests.
6. Verschillende coins mogen tegelijkertijd in verschillende regimes zitten.
7. Een globale crash/risk-off laag mag individuele signalen overrulen.
8. Alleen posities sluiten die Diamond Trader zelf heeft geopend.
9. Kapitaal later breed maar gecontroleerd benutten zodra kwaliteit en safety bewezen zijn.
10. Geen oude testgates kunstmatig als permanente blocker hergebruiken; actuele live-architectuur en recente prospectieve kwaliteit zijn leidend.
11. Elke live-order moet reproduceerbaar, gelogd en achteraf reconcilieerbaar zijn.
12. Nieuwe features mogen de bestaande bewezen safety niet verzwakken.

## 1. Algemene marktlaag

Boven alle individuele coins komt een globale marktstatus. Deze laag kijkt naar brede marktrisico's en geeft geen richting op zichzelf, maar kan risico verlagen of nieuwe entries blokkeren.

Gewenste globale toestanden:

- NORMAL
- RISK_ON
- RISK_OFF
- BROAD_BULL
- BROAD_BEAR
- CRASH / PANIC
- LIQUIDITY_STRESS
- EXCHANGE_STRESS
- UNKNOWN

Mogelijke inputs:

- BTC en ETH korte- en middellange-termijnbeweging
- percentage gevolgde coins dat stijgt/daalt
- marktbreedte
- volatility spikes
- spreadverbreding
- orderboekdiepte
- liquiditeitsverlies
- plotselinge correlatiesprong tussen coins
- API-latency/fouten
- exchange-status
- funding/borrow/margin-condities indien relevant

### REALTIME_MARKET_CRASH_GUARD

Gepland als eerstvolgende algemene veiligheidslaag na de huidige canary.

Doel: onmiddellijk nieuwe LONG-entries blokkeren bij een snelle brede marktval, ook wanneer een individueel SELECTIVE-signaal nog een ouder BULLISH-label draagt.

Mogelijke signalen:

- BTC/ETH sterke daling op 1m/5m/15m
- groot deel van de gevolgde coins tegelijk negatief
- plotselinge spreadverbreding
- depth/liquiditeitsverlies
- volatility spike
- orderboekverslechtering
- technische/exchange-stress

Bestaande posities blijven onder stop/trailing-management vallen.

## 2. Coin-specifieke regimes

Elke coin krijgt onafhankelijk een actuele regimeclassificatie.

Gewenste regimes:

- BULLISH_TREND
- BEARISH_TREND
- SIDEWAYS / RANGE
- BREAKOUT_UP
- BREAKOUT_DOWN
- PULLBACK_IN_TREND
- REVERSAL_CANDIDATE
- HIGH_VOLATILITY
- CHOPPY
- LOW_VOLATILITY
- LOW_LIQUIDITY
- EVENT_DRIVEN
- UNKNOWN

UNKNOWN of onvoldoende betrouwbare classificatie is fail-closed voor nieuwe live entries.

Voorbeeld van gelijktijdige regimes:

- XRP = BULLISH_TREND → LONG-strategie mogelijk
- ADA = SIDEWAYS → range/mean-reversion mogelijk
- SOL = BEARISH_TREND → toekomstige SHORT-strategie mogelijk
- coin D = CHOPPY → niets doen
- coin E = LOW_LIQUIDITY → overslaan

## 3. Strategieën per regime

### 3.1 LONG

Voor bullish/trending omstandigheden:

- trend breakout
- momentum continuation
- pullback/retest in trend
- volatility expansion breakout
- eventueel event-driven continuation indien bewezen

Huidige live-richting blijft SELECTIVE LONG execution.

Actieve/bedoelde bescherming:

- spreadlimiet
- fee/slippagebewaking
- stop-loss
- snelle live trailing-monitoring
- minimum netto winstlogica
- approval/canary-limieten
- BEARISH_REGIME_LONG_BLOCK
- later REALTIME_MARKET_CRASH_GUARD

### 3.2 SIDEWAYS / RANGE

Nog niet live.

Doel: in een echte zijwaartse markt niet blind trend-breakouts najagen, maar uitsluitend trades nemen waarvan de range groot genoeg is na alle kosten.

Te onderzoeken strategieën:

- range mean reversion
- buy near support / exit bij midpoint of bovenzijde range
- Bollinger/volatility compression context
- RSI mean reversion uitsluitend binnen bevestigd range-regime
- micro-breakout na langdurige compressie

Belangrijke voorwaarden:

- voldoende range-breedte na fees en slippage
- geen entry vlak vóór waarschijnlijke trendbreak
- hogere eisen aan spread/liquiditeit
- maximum holding time
- snelle regime-switch wanneer range verandert in trend

Geplande gate:

### SIDEWAYS_REGIME_FILTER

Niet alles blokkeren; alleen trades toestaan waarvan verwachte netto beweging na kosten voldoende is voor het gekozen sideways-model.

### 3.3 SHORT

Huidige short_breakout_v3 is afgewezen voor live.

Bekende evaluatie:

- 20 gesloten
- 1 winst / 19 verlies
- winrate 5%
- netto negatief
- PF ongeveer 0.05

Daarom niet opschalen of live activeren.

Nieuwe short-richting later opnieuw ontwerpen rond bestaande SELECTIVE SHORT-signalen en bearish regime-informatie.

Toekomstige live-shortarchitectuur moet apart bevatten:

- aparte execution-route
- aparte approval
- maximaal één short in eerste canary
- kleine canary-stake
- Bitvavo leveraged/margin-account health check
- borrow/leenkosten meenemen
- liquidation-distance bewaken
- margin ratio fail-closed
- aparte stop-loss
- aparte trailing
- maximum holding time
- adverse-move protection
- nood-close bij API/safety-afwijkingen

Short alleen gebruiken wanneer het bearish signaal aantoonbaar positief is na alle kosten.

## 4. Execution Intelligence

Strategie bepaalt waarom een trade interessant is. Execution bepaalt of en wanneer die trade werkelijk verstandig uitvoerbaar is.

### DYNAMIC_COST_GATE

Research/shadow.

Expected move moet groter zijn dan:

- trading fees
- actuele spread
- geschatte slippage
- eventuele borrow-kosten
- veiligheidsmarge

### LIQUIDITY_STATE_FIRST

Eerst liquiditeit beoordelen, daarna pas entry.

Inputs:

- spread
- top-of-book depth
- depth meerdere niveaus
- handelsvolume
- orderboekstabiliteit
- recente execution-slippage
- impact van gewenste ordergrootte

### ORDER_BOOK_IMBALANCE_ENTRY_GATE

Research/shadow.

Meet circa 5–30 seconden vlak voor execution:

- bid/ask depth
- imbalance
- spread
- order flow
- adverse selection

Label bijvoorbeeld ORDER_BOOK_ALIGNED wanneer korte-termijn orderboekdruk de bestaande LONG/SHORT-richting ondersteunt.

Geen nieuwe richtingsstrategie; alleen executionfilter op bestaand signaal.

### ROUND_NUMBER_EXECUTION_AWARENESS

Research/shadow.

Meet afstand tot psychologische/round levels en positie eromheen:

- whole
- half
- quarter
- 0.10
- 0.05
- aangepaste granulariteit voor goedkope coins

Vergelijk:

- spread
- slippage
- adverse move na entry
- fill quality
- netto PnL
- profit factor
- mogelijke wachttijd

Geen richting bepalen op round numbers alleen.

### QUARTER_HOUR_ENTRY_TIMING

Research/shadow.

Vergelijk dezelfde signalen bij:

- immediate execution
- rond/na 15m-grens
- rond/na 30m-grens

Doel: voorkomen dat een goed signaal op een structureel slecht executionmoment wordt gekocht.

### EVENT_RATE_REGIME_GATE

Research/shadow.

Meet verandering in frequentie/tijd tussen significante upward/downward prijs-events rond 15m en 1h.

Mogelijke labels:

- EVENT_RATE_NORMAL
- TRENDING_UP
- TRENDING_DOWN
- ACCELERATING
- DECELERATING

Alleen gebruiken als bevestiging van bestaande richting.

## 5. Exit Intelligence

Exitmanagement moet minstens even geavanceerd worden als entry.

Bestaand:

- hard stop-loss
- trailing stop
- profit trailing trigger
- snelle live position check op actuele bid
- protective exits mogen minimum-profitregel overrulen wanneer bescherming dat vereist

Gepland/onderzoek:

### ADAPTIVE_ATR_TRAILING_EXIT

Research/shadow.

- trailing-afstand aanpassen aan actuele volatiliteit
- vaste vooraf gekozen ATR-multipliers vergelijken
- geen achteraf geoptimaliseerde multiplier per trade

### REGIME_AWARE_EXIT

- trend intact → winst meer ruimte geven
- momentum breekt → sneller beschermen
- overgang naar CHOPPY → exit aanscherpen
- globale CRASH/PANIC → defensieve exitmodus

### TIME_BASED_EXIT

Voor trades die te lang stilstaan:

- opportunity-cost meten
- maximum holding time per strategie/regime
- alleen gebruiken als data aantonen dat lang vasthouden netto slechter is

## 6. Risk Management

Risk management moet centraal staan en onafhankelijk van strategie kunnen ingrijpen.

Gewenste lagen:

- max stake per trade
- max open posities
- max exposure per coin
- max exposure per regime
- max gecorreleerde exposure
- max dagverlies
- max rolling drawdown
- max consecutive losses
- volatility-adjusted position sizing
- spread/slippage hard limits
- liquidity-based stake cap
- reserve in EUR
- emergency kill switch
- recovery-required state
- approval expiry en canary caps

Nooit stake verhogen uitsluitend omdat recente trades gewonnen hebben.

## 7. Portfolio & Capital Allocation

Langetermijndoel: een groot maar gecontroleerd deel van beschikbaar handelskapitaal kunnen benutten zodra strategieën prospectief bewezen zijn.

Capital allocator moet rekening houden met:

- vaste EUR-reserve
- max open posities
- quality score per strategie
- liquiditeit
- spread
- verwachte slippage
- correlatie tussen open posities
- marktregime
- drawdown
- recente executionkwaliteit
- totale portfolio-exposure
- coin concentration

Kapitaal mag niet blind gelijk verdeeld worden.

Mogelijke allocatieklassen:

- A: hoogste bewezen kwaliteit / beste liquidity
- B: goede kwaliteit / normale inzet
- C: beperkte inzet / hogere onzekerheid
- BLOCKED: geen nieuw kapitaal

## 8. Multi-Strategy Concurrentie

Uiteindelijk mogen meerdere strategieën tegelijk draaien zolang portfolio- en safety-limieten dit toestaan.

Voorbeeld:

- LONG op XRP
- SIDEWAYS/range trade op ADA
- SHORT op SOL
- geen trade op choppy coin

Daarvoor is later nodig:

- centrale position registry
- strategie-eigenaarschap per positie
- uniek trade/candidate-id
- portfolio exposure manager
- conflict-resolutie wanneer twee strategieën dezelfde coin willen handelen
- geen dubbele of tegengestelde posities zonder expliciet ontwerp

## 9. Regime Router

Een centrale router wordt uiteindelijk verantwoordelijk voor:

1. globale safety/risk status
2. coin-regime
3. toegestane strategieën voor dat regime
4. strategy-quality
5. execution quality
6. capital allocation
7. uiteindelijke trade permission

Conceptueel:

GLOBAL SAFETY
→ MARKET REGIME
→ COIN REGIME
→ STRATEGY ELIGIBILITY
→ EXECUTION GATES
→ RISK/CAPITAL
→ APPROVAL
→ ORDER

Elke laag mag blokkeren. Geen enkele lagere laag mag een blokkade van een hogere safetylaag omzeilen.

## 10. Data & Research

Elke kandidaat, ook een geblokkeerde trade, moet waar praktisch mogelijk meetbaar blijven zodat we kunnen leren zonder geld te riskeren.

Te bewaren/vergelijkbare data:

- signal timestamp
- candle timestamp
- regime
- strategy
- score
- R/R
- spread
- orderboekkenmerken
- volatility/ATR
- expected move
- fees
- estimated slippage
- actual slippage bij live execution
- adverse move na entry
- favorable excursion
- holding time
- exit reason
- expected versus actual PnL
- gemiste trade-resultaten van gates

Belangrijk: research-gates eerst shadowen op exact dezelfde bestaande signalen. Geen appels-met-peren vergelijking.

## 11. Monitoring & Operations

Vaste operationele controles:

- botprocessen actief
- periodic runner gezond
- geen recente echte errors/tracebacks
- safety status
- collectors/market lead niet gestagneerd
- disk usage
- cgroup memory
- pending orders
- recovery state
- approval status
- open positions
- exchange/API health

Stagnatie moet automatisch detecteerbaar worden wanneer collectors/tracker gedurende onredelijk lange tijd geen nieuwe timestamps/events/samples schrijven.

## 12. Execution Audit & Reconciliation

Voor elke echte BUY/SELL:

- order-id
- client-order-id
- reference bid/ask vlak voor submit
- fill price
- fee uit exchange-response zonder dubbeltelling
- spread
- slippage
- hoeveelheid
- quote amount
- expected PnL
- actual PnL
- verschil expected/actual
- holding time
- exit reason
- canary/trade sequence

Bitvavo-transacties zijn de uiteindelijke externe waarheid voor financiële reconciliatie.

## 13. Notifications

Live BUY/SELL-meldingen blijven apart van algemene gemute mailstromen.

Toekomstig uitbreidbaar met:

- safety block
- crash guard active
- recovery required
- abnormal slippage
- exchange/API outage
- margin warning indien shorts live worden
- daily compact summary

Geen mailspam: alleen events die operationeel relevant zijn.

## 14. Safety voor toekomstige shorts/margin

Voordat echte shorts ooit worden toegestaan:

- exchange ondersteunt gekozen markt voor short/margin
- account/margin endpoint betrouwbaar
- borrow availability gecontroleerd
- borrow rate bekend
- liquidation/margin ratio bewaakt
- maximale leverage expliciet begrensd
- aparte short approval
- aparte short canary
- geen automatische overgang van paper naar live
- nood-close getest
- exchange reconciliation getest

## 15. Mogelijke toekomstige intelligentielagen

Alleen invoeren als meetbaar nuttig:

- market breadth score
- correlation regime
- volatility regime
- liquidity regime
- trend strength score
- multi-timeframe alignment
- support/resistance map
- volume profile
- VWAP context
- realized volatility
- volatility compression/expansion
- order-flow imbalance
- microstructure adverse-selection score
- event/news risk flag
- exchange cross-checks
- cross-exchange price dislocation
- portfolio heat
- regime transition probability

AI/ML mag later helpen bij classificatie/ranking, maar mag niet zonder uitlegbare safetygrenzen direct orders afdwingen. Een simpel, bewezen model krijgt voorrang boven een complex model dat niet prospectief aantoonbaar beter is.

## 16. Wat we expliciet niet willen

- overfitting op één korte periode
- tientallen filters stapelen zonder marginale meerwaarde
- tests uitvoeren om alleen maar te blijven testen
- onverklaarbare AI-beslissingen direct live
- orders plaatsen bij ontbrekende safetydata
- automatisch live-short activeren
- stake verhogen voordat execution en risk bewezen zijn
- één globale strategie gebruiken voor alle marktomstandigheden
- cash volledig inzetten zonder reserve/exposure-limieten

## 17. Huidige prioriteitsvolgorde

1. Huidige €30 canary trade 5 afronden.
2. Alle vijf canary-trades gezamenlijk beoordelen op execution, fees, slippage, exits, safety en echte Bitvavo-resultaten.
3. REALTIME_MARKET_CRASH_GUARD ontwerpen en eerst veilig valideren.
4. SIDEWAYS_REGIME aanpak ontwerpen.
5. Regime-classificatie per coin verder structureren.
6. Nieuwe short-strategie researchen; huidige short_breakout_v3 blijft afgewezen.
7. Execution intelligence uitbreiden met kosten/liquiditeit/orderboek/timing waar aantoonbaar nuttig.
8. Exit intelligence verder verbeteren.
9. Na bewezen kwaliteit gecontroleerd stake/capital allocation opschalen richting de geplande grotere inzet.
10. Daarna multi-position/multi-strategy portfolio-architectuur invoeren.

## 18. Definitie van succes

Diamond Trader is niet 'af' wanneer hij veel functies heeft. Hij is succesvol wanneer hij:

- verschillende marktregimes correct genoeg onderscheidt
- per regime alleen geschikte strategieën gebruikt
- slechte omstandigheden actief overslaat
- entries efficiënt uitvoert
- exits actief beschermt
- netto na alle kosten positief presteert
- drawdowns beheerst
- geen ongeautoriseerde of onherleidbare orders plaatst
- storingen fail-closed afhandelt
- kapitaal gecontroleerd en efficiënt inzet
- uitbreidbaar blijft zonder bestaande safety te breken

Dit document is de langetermijnrichting. Concrete implementaties mogen hiervan afwijken wanneer nieuwe data aantonen dat een andere aanpak aantoonbaar veiliger of winstgevender is, maar safety en meetbaarheid blijven altijd leidend.