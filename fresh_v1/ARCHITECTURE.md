# Architectuur CryptoBot Fresh v1

## Modules
- `main.py` — hoofdloop en nette shutdown.
- `config.py` — environment-configuratie en validatie.
- `models.py` — datamodellen.
- `indicators.py` — EMA, RSI, ATR en volume-ratio zonder pandas/numpy.
- `bitvavo_market.py` — uitsluitend openbare Bitvavo-marktdata.
- `strategy.py` — precies één entrystrategie.
- `paper_trader.py` — paper execution, stop, take-profit en trailing.
- `storage.py` — SQLite, transacties en restart-persistentie.
- `status.py` — compacte status.
- `backtest.py` — sanity-backtest met next-candle execution.

## Ontwerpkeuzes
- Geen pandas/numpy: laag RAM-gebruik op Render.
- Geen lopende candle: voorkomt instabiele signalen.
- Geen lookahead in backtest: signaal op close, entry volgende open.
- Conservatieve same-candle resolutie: stop vóór target.
- Open/close paperboekingen atomair in SQLite.
- Fout op één markt stopt andere markten niet.
- Geen private API of echte ordercode in v1.
