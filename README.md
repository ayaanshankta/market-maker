# market-maker

Phase 1 of a market-making research project: record live L2 order books + trades for
BTC/ETH/SOL-USD from Coinbase and Kraken (public feeds, no API keys), then backtest
market-making strategies on that data.

## Run
```
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
.venv/bin/python recorder.py                    # runs forever, reconnects itself
.venv/bin/python check.py [YYYY-MM-DD]          # rebuild books, verify, report gaps
```
Data lands in `data/{venue}/{symbol}/{book,trades}/date=.../hour=.../*.parquet` (gitignored).

## What "healthy" means
`check.py` exits 0: no crossed books, 100% Kraken checksums matched, and only short gaps
(each reconnect is logged in `recorder.log` with its reason).
