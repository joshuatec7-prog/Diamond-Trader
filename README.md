# CryptoBot Clean-Room v1

Zelfstandige paper-trading bot met een harde scheiding tussen marktdata, strategie, paper-execution en opslag.

## Veiligheidsgrens

- `RUN_MODE=PAPER` is verplicht; iedere andere mode wordt geweigerd.
- De runtime bevat geen private Bitvavo-authenticatie en geen echte orderfunctie.
- Alleen publieke marktdata wordt benaderd.
- De SQLite-database is zelfstandig en gebruikt op Render standaard `/var/data/cryptobot_cleanroom.db`.
- De eerste marktselectie is dynamisch op publiek EUR-volume en wordt daarna vastgezet.

## Strategie

De eerste, vooraf vastgezette strategie is een long-only statistische lower-band re-entry op 1-uurscandles. De strategie gebruikt een rollend gemiddelde en standaarddeviatie. Stop, winstdoel en maximale houdtijd zijn vooraf vastgezet.

## Lokale controles zonder netwerk

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python offline_check.py
python readiness.py
```

`offline_check.py` doorloopt deterministisch de volledige keten van synthetische marktdata → signaal → paper entry → paper exit → trade → database, zonder internet.

## Runtime-commando's

```bash
python main.py --status
python main.py --readiness
python main.py --report
python main.py --once
```

Voor Render:

```bash
./start.sh
```

## Readiness

Readiness maakt bewust onderscheid tussen:

- lokale veiligheid/configuratie/database;
- bereikbaarheid van marktdata;
- gereedheid om de prospectieve paper-observatie te starten.

Als marktdata geblokkeerd is, blijft de worker actief zonder te handelen en registreert hij de datastatus in SQLite en de diagnose in de logs.
