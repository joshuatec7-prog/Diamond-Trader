# CryptoBot Clean-Room v1

Een kleine, zelfstandige Bitvavo paper-trading bot.

## Eigenschappen

- uitsluitend publieke Bitvavo-marktdata;
- geen API-key nodig;
- geen echte orderfunctie aanwezig;
- dynamische eerste marktselectie op actueel EUR-volume;
- gekozen marktuniversum wordt daarna vastgezet;
- één eenvoudige lower-band re-entry strategie;
- SQLite-opslag;
- fees, spread en slippage in paperresultaten;
- vaste evaluatiegrenzen vóór de meetperiode.

## Start

```bash
python -m pip install -r requirements.txt
python main.py --once
python main.py --status
python main.py --report
```

Voor Render:

```bash
./start.sh
```

De database wordt automatisch `/var/data/cryptobot_cleanroom.db` als `/var/data` beschikbaar en schrijfbaar is.
