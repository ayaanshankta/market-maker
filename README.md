# market-maker

Market-making research on real crypto order books: record the data, prove it's correct, backtest
quoting strategies with realistic queue-position fills, and find out honestly whether any of them
have an edge.

**Status (2026-09-28):** recording runs 24/7 (since 2026-09-26). Phases 1–6 are built and tested.
The tuning day (2026-09-27) is analysed; the untouched test day (2026-09-28) runs next. Every
decision, bug and dead end is in [DECISIONS.md](DECISIONS.md).

## 1. Data you can trust

| | |
|---|---|
| Venues / pairs | Coinbase Advanced Trade + Kraken v2 × BTC, ETH, SOL (public WebSockets, no keys) |
| Depth | Coinbase full book (~40k levels); Kraken top 100 |
| Volume | ~51M book updates/day across 6 streams; ~0.55 GB/day Parquet (zstd) |
| Integrity | **15,500,031 / 15,500,031 Kraken CRC32 checksums matched** on the first full day; 0 crossed books, 0 gaps > 5 s |
| Ops | Windows supervisor with graceful stop; reconnect on sequence gap or checksum failure; timer-forced 00:00 UTC re-snapshot so each day verifies on its own; daily check + append-only backup to a second disk + health log |

`recorder.py` → `check.py` (streams one file at a time: ~15 GB → 84 MB peak memory) → `tob.py`
(top-of-book once per day) → `eda.py` / `backtest.py` / `regime.py` / `sensitivity.py`.

## 2. What the data says (2026-09-27, full day)

- **SOL** has the widest relative tick (median spread 1 tick = 0.82 bps; wider than 1 tick ~48% of
  the time on Coinbase), so it's the asset we quote. BTC and ETH are signals.
- **Order-flow imbalance explains 8–44% of same-second mid moves but ≤ 1.6% of the next second.**
  Most of the headline R² is contemporaneous; a quoter can only use the predictive part.
- Top-of-book imbalance → next-1 s mid move: corr 0.08–0.19.

## 3. Backtesting: why the first backtest lies

Event-driven replay of the raw book and trades, 50 ms order/cancel latency. Two fill models:
**naive** (filled whenever a trade touches your price) vs **queue** (join the back of the queue; trades
and a stated share of cancels move you forward).

Touch quoter, 1 SOL, full day 2026-09-27, 0 fees:

| | naive fills | queue fills |
|---|---|---|
| Coinbase SOL PnL | −$25 | **−$193** |
| Coinbase SOL spread captured | **+$142** | −$43 |
| 1 s markout | −0.03 bps | **−0.75 bps** |

With realistic queues you reach the front exactly when the level is being consumed, i.e. right before
the price moves through you. Naive fills hide that adverse selection almost entirely.

Avellaneda–Stoikov (k fitted from where trades print relative to the mid) loses far less, but only by
trading less. Its fills are *more* toxic than the touch quoter's (1 s markout −0.90 vs −0.75 bps),
because quotes away from the touch only get hit by sweeps. **At a 1-tick spread with 50 ms latency,
passive quoting on SOL has no edge by itself.**

## 4. In progress

- Test day 2026-09-28 (nothing tuned on it): A-S across gamma, the HMM-regime + imbalance-skew ablation,
  and sensitivity to latency / fees / cancel assumptions.
- The HMM is fit offline, but live quoting uses only a forward filter. hmmlearn's `predict_proba` is
  smoothed (forward-backward) and would leak the future; `test_regime.py` demonstrates the difference.

## Run
```
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
python recorder.py                          # runs forever, reconnects itself
python check.py 2026-09-27                  # rebuild books, verify checksums, report gaps
python tob.py 2026-09-27 && python eda.py 2026-09-27
python backtest.py 2026-09-28 coinbase SOL-USD calib_day=2026-09-27 gamma=3
python regime.py 2026-09-27 2026-09-28 coinbase SOL-USD gamma=3
python sensitivity.py 2026-09-28 coinbase SOL-USD calib_day=2026-09-27 gamma=3
python test_book.py && python test_backtest.py && python test_regime.py
```
Data lands in `data/{venue}/{symbol}/{book,trades}/date=.../hour=.../*.parquet` (gitignored).
