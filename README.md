# market-maker

Market-making research on real crypto order books: record the data, prove it's correct, backtest
quoting strategies with realistic queue-position fills, and find out honestly whether any of them
have an edge.

**Status (2026-09-29):** recording runs 24/7 (since 2026-09-26). Phases 1–6 are built and tested.
First out-of-sample day done; walk-forward (train D−1, test D) now runs daily. Every decision, bug
and dead end is in [DECISIONS.md](DECISIONS.md).

**Headline so far:** at a 1-tick spread with 50 ms latency and zero fees, no quoting strategy is reliably
profitable on SOL. Naive fill models hide most of the adverse selection. **Latency is the lever:**
at 0 ms one venue turns positive, at 250 ms both lose 4–12× more, and 1 bps of fees wipes out the rest.

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

Avellaneda–Stoikov (k fitted from where trades print relative to the mid) loses far less, mostly by
trading less. Whether its fills are less toxic is **unresolved**: on 09-27 they were worse than the
touch quoter's (1 s markout −0.90 vs −0.75 bps; quotes away from the touch only get hit by sweeps),
while on 09-28 they were better (−0.38 vs −0.81). Two days disagree, so day-to-day noise dominates.
**At a 1-tick spread with 50 ms latency, passive quoting on SOL has no edge by itself.**

## 4. Out of sample (tuned on 2026-09-27, tested once on 2026-09-28)

| SOL-USD, 09-28 | touch (queue fills) | Avellaneda–Stoikov, gamma 3 (fixed a priori) |
|---|---|---|
| Coinbase PnL / traded | −$452.52 / $4.79M (−0.94 bps) | −$2.94 / $68k (−0.43 bps) |
| Kraken PnL / traded | −$329.75 / $3.43M (−0.96 bps) | −$1.08 / $26k (−0.42 bps) |

Sensitivity of A-S on the test day (one knob at a time):

| | Coinbase | Kraken |
|---|---|---|
| base (50 ms, 0 fee) | −$2.94 | −$1.08 |
| 0 ms latency | −$0.71 | **+$2.33** |
| 250 ms latency | −$12.01 | −$13.76 |
| 1 bps fee | −$9.78 | −$3.66 |
| cancels ahead of us / behind us | −$0.03 / −$4.93 | −$0.73 / −$1.13 |

The imbalance skew helped on both venues (+$0.73, +$0.59) and the HMM regime filter was mixed.
Every difference is ~$1/day against $3–17/day of inventory noise, so no single day can decide it.
Walk-forward over many days (`walkforward.py summary`: mean ± 95% CI, paired vs A-S) is where that
gets settled.

The HMM is fit offline, but live quoting uses only a forward filter. hmmlearn's `predict_proba` is
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
python walkforward.py 2026-09-28 coinbase SOL-USD && python walkforward.py summary
python test_book.py && python test_backtest.py && python test_regime.py
```
Data lands in `data/{venue}/{symbol}/{book,trades}/date=.../hour=.../*.parquet` (gitignored).
