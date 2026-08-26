# Clean-room verklaring

Deze codebasis is opnieuw opgebouwd vanuit uitsluitend drie infrastructuurrandvoorwaarden: Render-hosting, GitHub-broncodebeheer en Bitvavo als exchange.

Geen eerdere handelsstrategie, eerder symbooluniversum, eerdere indicatorinstellingen, eerdere stake-instellingen, eerdere onderzoeksresultaten of eerdere evaluatieresultaten zijn als input gebruikt.

## Onafhankelijke ontwerpkeuzes

- De runtime accepteert uitsluitend `RUN_MODE=PAPER`.
- Er is geen private API-authenticatie en geen echte orderfunctie aanwezig.
- Marktdata is via `MarketDataSource` losgekoppeld van strategie en paper-execution.
- De eerste marktselectie is niet hard-coded. Publieke EUR-markten worden op actueel 24-uurs quotevolume gerangschikt; de eerste selectie wordt daarna in de eigen SQLite-database vastgezet.
- De eerste strategie is een statistische lower-band re-entry op 1-uurscandles met alleen een rollend gemiddelde en standaarddeviatie.
- Exitregels zijn vooraf vastgezet: procentuele stop, procentueel winstdoel en maximale houdtijd.
- Paper trading gebruikt een eigen fictief startkapitaal en geen bestaand rekeningbedrag.
- Evaluatiegrenzen zijn vóór de eerste paper trade vastgezet.

## Veiligheids- en integriteitscontroles

- inkomende candles en orderboekwaarden worden op eindigheid en prijsstructuur gevalideerd;
- cash en positie-mutaties gebeuren transactioneel in SQLite;
- een positie kan niet tweemaal worden gesloten en paper-cash kan daardoor niet dubbel worden gecrediteerd;
- SQLite `quick_check` en schema-versie zijn onderdeel van readiness/status;
- paper-tradetijden gebruiken de prospectieve runtime-tijd, zodat historische candle-timestamps de minimale observatieperiode niet kunnen versnellen;
- CI scant de runtime op private trading-capabilities;
- `offline_check.py` test de volledige paperketen deterministisch zonder netwerk.

## Marktdata bij storing

Een publieke API-storing of netwerkblokkade veroorzaakt geen crash/restart-lus. De worker blijft veilig zonder trading actief, registreert de datastatus en voert alleen publieke REST/WebSocket-diagnose uit.
