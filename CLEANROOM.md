# Clean-room verklaring

Deze codebasis is opnieuw opgebouwd vanuit drie toegestane bestaande randvoorwaarden:

- hosting op Render;
- broncodebeheer via GitHub;
- Bitvavo als exchange.

Geen eerdere handelsstrategie, eerder symbooluniversum, eerdere indicatorinstellingen, eerdere stake-instellingen, eerdere onderzoeksresultaten of eerdere evaluatieresultaten zijn als input gebruikt voor deze codebasis.

## Nieuwe keuzes

- De marktselectie is niet hard-coded. Bij de eerste start worden actieve EUR-markten via de publieke Bitvavo-marktenlijst opgehaald en gerangschikt op actueel 24-uurs quotevolume. De gekozen universe wordt daarna in SQLite vastgezet.
- De eerste strategie is een statistische lower-band re-entry op 1-uurscandles.
- De strategie gebruikt alleen een rollend gemiddelde en standaarddeviatie. Er zijn geen aanvullende richtingsfilters.
- Exitregels zijn vooraf vastgezet: vaste procentuele stop, vaste procentuele winstdoelstelling en een maximale houdtijd.
- Paper trading gebruikt een eigen fictief startkapitaal en heeft geen koppeling met een bestaand rekeningbedrag.
- Evaluatiegrenzen zijn vóór de eerste paper trade vastgezet.

## Bronnen voor exchangegedrag

Alle exchange-endpoints in deze code komen uit de actuele publieke Bitvavo REST-documentatie. De standaard taker fee voor EUR-markten gebruikt de publiek vermelde eerste volumetier en blijft configureerbaar.
