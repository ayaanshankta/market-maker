# Exercises: learn the data and the backtester by rebuilding things yourself

Put your own scripts in `learn/`. Don't edit the main files while learning: copy them instead.
Use `../.venv/bin/python`. Every exercise has a **check**: a number to match, so you know you're right.

## The data you have

| Path | What | One row = |
|---|---|---|
| `derived/{venue}/{sym}/tob/date=D.parquet` | top of book, once per change | `recv_ns, conn_id, bid_px, bid_sz, ask_px, ask_sz, bid_sz5, ask_sz5` |
| `data/{venue}/{sym}/trades/date=D/hour=H/*.parquet` | every trade | `recv_ns, price, size, side, is_snapshot, ...` |
| `data/{venue}/{sym}/book/date=D/hour=H/*.parquet` | raw L2 deltas (rebuild with `book.replay`) | one price level change |
| `reports/eda-D.md` (desktop branch) | the official EDA numbers per day | |

`recv_ns` = nanoseconds since 1970 (UTC), our receive time. `conn_id` changes on every reconnect:
**never compute anything across a conn_id change.** Skip `is_snapshot` trades (they're history).

## 1. Look at it
Load `derived/coinbase/SOL-USD/tob/date=2026-10-01.parquet` with pandas. Plot the mid price and the
spread (in ticks) over the day. Mark each reconnect (conn_id change).
**Check:** the median spread is 1 tick ($0.01).

## 2. Rederive a published number
Sample the book every 100 ms (last row at or before each tick), compute top-of-book imbalance
`(bid_sz − ask_sz) / (bid_sz + ask_sz)` and the mid change over the next 1 s. Correlate them.
**Check:** match `corr_obi1_1s` for coinbase SOL-USD in `reports/eda-2026-10-01.md` to ~3 decimals.
If you're off, find out why before moving on. That's the whole skill.

## 3. Your first strategy
A strategy is a callable `(t, bids, asks, inv) -> {BID: price or None, ASK: price or None}` with a
`.size` attribute (see `Touch` in `backtest.py`). Write one that quotes one tick *behind* the best bid
and ask. Run it with `Sim(..., queue=True)` and `run()` on the 2026-09-28 hour-00 sample, then `summary()`.
**Check:** it should fill far less than `Touch`, with a different markout. Explain why in one sentence.

## 4. Break the simulator on purpose
Same strategy, change one thing at a time: `queue=False`, `latency_ns=0`, `latency_ns=250_000_000`,
`cancels="front"` / `"back"`, `fee_bps=1`. Make a table.
**Check:** naive fills (queue=False) should look much better than queue fills. Write down why.

## 5. Your own signal, tested honestly
Idea: BTC often moves before SOL. Build "BTC mid moved up in the last 1 s" from
`derived/coinbase/BTC-USD/tob`, and use it to skew your SOL quotes.
Rules: **design and tune on 2026-10-01 only. Then run once on 2026-10-04 and accept the answer.**
Write down your hypothesis *before* the test run (commit it, so the timestamp proves it).
**Check:** none. This one is real research.

When you're stuck, open `DECISIONS.md`: most mistakes you'll make are already in there.
