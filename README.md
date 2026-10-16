# market-maker

Can a retail-speed trader make money market making crypto?

- Records every order-book update from Coinbase and Kraken (BTC, ETH, SOL) 24/7, about 51M updates a day.
- Verifies the data against Kraken's checksums: 15.5M of 15.5M matched.
- Backtests quoting strategies with realistic queue-position fills and 50 ms latency.
- Finding: naive backtests show profit, realistic fills show a loss. Latency decides everything, and at retail speed nothing I tested is profitable.

Details: [RESULTS.md](RESULTS.md) · decision log: [DECISIONS.md](DECISIONS.md)
