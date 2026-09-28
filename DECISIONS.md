# Decision log

Every choice, bug, surprise and dead end. This is the interview material.

## 2026-09-26 — Phase 1 recorder

- **Venues: Coinbase Advanced Trade + Kraken v2, both public, no keys.** Coinbase: full-depth
  book, most USD volume, connection-wide `sequence_num` -> gap = reconnect. Kraken: CRC32
  checksum of the top 10 levels on every update, so the rebuilt book is *provably* identical to
  the exchange's. Coinbase Exchange's free `level2_batch` was rejected: it silently became
  `level2_50` (50 levels only).
- **Symbols: BTC, ETH, SOL.** Measured median spreads (45 s, 2026-09-26): BTC 0.001 bps,
  ETH 0.04 bps, SOL 0.83 bps. All are 1 tick. SOL is the quoted asset: the largest relative tick,
  so queue position matters most. BTC/ETH are recorded as lead-lag signals.
- **Fees dominate spread for all three at retail tiers** (maker ~0-40 bps vs <1 bps spread).
  The backtest will assume a pro tier (~0 bps maker) and report fee sensitivity rather than hide this.
- **Kraken depth 100** (not 1000): market making lives near the touch; saves disk.
- **Kraken checksum needs exact decimals**: JSON parsed with `Decimal`; per-pair price decimals
  from the `instrument` channel (BTC 1, ETH 2, SOL 2; qty 8). Offline re-check works from
  float64 because 8-dp quantities fit in float64 exactly after formatting.
- **Two timestamps per row**: exchange event time + local `recv_ns`. Needed for latency modelling.
- **Runs on the always-on Windows desktop**, not the laptop (sleep = gaps).

## 2026-09-26 — Running on the desktop

- **Live data on C: (internal NVMe, NTFS), daily backup to D: (external USB, exFAT).** D: first
  looked like the obvious home (1.3 TB free), but exFAT has no journal and USB can drop out.
  Cutover gap: 1.03 s.
- **Measured rate: 0.55 GB/day** (Saturday evening; weekdays likely higher). The 75 s smoke test
  suggested 1.55 GB/day. A short sample over-extrapolated.
- **First hour: 447,142/447,142 Kraken checksums matched, 0 crossed books, 0 reconnects.**
- **check.py streams file by file.** The whole-day `to_pydict()` version would have needed ~15 GB
  RAM on a shared machine. Streaming peaks at 84 MiB.
- **Bug (caught by the desktop session): checking one day started from an empty book.** Unless the
  day began with a reconnect, the book was wrong and Kraken checksums failed (7,523/9,441), a false
  alarm on healthy data. Fix: the recorder reconnects at 00:00 UTC, so every day starts with a snapshot
  and is self-contained for the backtester too (~1 s/day cost). check.py now skips and reports any
  rows it has no snapshot for, instead of failing on them.

## 2026-09-27 — Phase 2 groundwork

- **Coinbase can re-send a snapshot mid-connection** (ETH-USD, 14:32:48 UTC, no reconnect). Any
  snapshot must reset the book. replay() already does this.
- **A mid-day hour can't be rebuilt on its own**: its snapshot is at connection start. Samples for
  development come from hour 00 (right after the daily rollover).
- **Top-of-book is materialised once per day** (`tob.py`, one row per change in the top 5 levels):
  replaying the raw book costs minutes per day, so EDA and the backtester read `derived/` instead.
- First look, Coinbase ETH-USD (28 min): spread is 1 tick ($0.01) at the median but $0.21 at p90.
  It widens often, which matters for when quoting is worthwhile.
- **First rollover (2026-09-28): Coinbase gap 1.5 s, Kraken 12.1 s.** The rollover fired on the
  first message after midnight, and Kraken sent nothing for 10 s. Fix: forever() forces the
  reconnect on a timer at 00:00:00 UTC; the per-message check stays as a guard. Cause of the 10 s
  of silence is unknown (Kraken-side or ours). The timer shows which: if the new connection
  snapshots within ~2 s, the old socket was the problem.
- **Kraken drops the connection ~4x/day** (ConnectionClosedError, 2026-09-27); Coinbase 0. Each
  drop is a few seconds of Kraken data. Tracked in the daily health line.

## 2026-09-28 — Phase 2 first look (1 hour: 00:00–01:00 UTC, a quiet Sunday evening; indicative only)

- **Order flow imbalance explains ~23–46% of same-second mid moves (R², 1 s buckets)**, matching
  Cont–Kukanov–Stoikov's range, but only **0.3–3% of the NEXT second**. Most of what OFI "explains"
  happens at the same time as the move. A market maker needs the predictive number, not the headline one.
- **Top-of-book imbalance vs the next 1 s mid move: corr 0.13–0.30** (Kraken BTC highest). At a
  1-tick spread part of this is mechanical: the thin side empties and the mid ticks. Still usable for
  quote skew, since that mechanical move is exactly the adverse selection a quote suffers.
- **Micro-price beats mid only weakly** (corr 0.04–0.14), weaker than raw imbalance here.
- **SOL spread: 2 ticks at the median (1.64 bps), 1 tick only 46–48% of the time**, so there's more room
  to quote inside the spread than BTC (1 tick 81–98% of the time).
- **Coinbase BTC trades are dust-dominated**: median taker event $0.19 vs ~$50 on Kraken. Trade-count
  signals on Coinbase BTC need a size filter.
- Pandas trap caught in review: `df.size` is the element count, not the `size` column (it produced
  "$8.7B median trade").

## 2026-09-28 — First full day (2026-09-27 UTC, 23.4 h) supersedes the 1-hour look

- Integrity: **15,500,031/15,500,031 Kraken checksums**, 0 crossed, 0 gaps > 5 s over 51M book rows.
- **SOL median spread is 1 tick (0.82 bps) over the full day**; the "2 ticks" in the 1-hour sample was
  a quiet-evening effect. The spread is 1 tick 52% (Coinbase) / 86% (Kraken) of the time. One hour
  wasn't representative, which is why the daily runs matter.
- **Book imbalance → next-1 s mid: corr 0.08–0.19** (Kraken BTC 0.19 best). **OFI next-1 s R² ≤ 1.6%**
  while same-second R² is 8–44%. The full day confirms the gap between the two.
- **Coinbase BTC median taker event = $0.015** (~1.8e-7 BTC): the trade tape is dust-dominated.
- Process lesson (desktop): `cmd /c start /wait /b` always returns exit code 0, so exit codes are
  only evidence when the process is launched directly.

## 2026-09-28 — Phase 3 backtester v1 (backtest.py)

- Event-driven: raw book replay + trades merged by recv_ns (trades first on ties), 50 ms order/cancel
  latency, cancel-replace loses queue position, orders become live on ANY event (a test caught
  zero-latency orders being ignored until the next book update).
- Queue model: join at the back; trades at our price eat the queue ahead first; unexplained level
  shrinkage is cancels, taken from ahead of us pro rata (ahead / level). A trade through us, or the
  other side crossing us, fills in full. Trade direction is inferred from price vs our quote, not
  the feed's side field.
- **First result (touch quoter, 1 SOL, max inv 5, 0 fees, 2026-09-28 00:00–01:00 UTC):** the queue
  model fills ~20–25% less AND each fill is ~2x more toxic (1 s markout Coinbase −0.49 → −1.02 bps,
  Kraken −0.39 → −0.88). You reach the front of the queue when the level is being consumed,
  i.e. right before the price moves through you. Spread capture flips from +$7 to −$4 on Coinbase.
- Both models lose even at 0 fees: SOL's half-spread (~0.4 bps) is smaller than the adverse move
  after a fill. The touch quoter is the baseline to beat, not a strategy.
- Known approximations: the book is our recorded view, not the exchange's; "mid at fill" is the last
  book seen, which is stale for through-fills (those show up as negative spread capture).

## 2026-09-28 — Phase 4: Avellaneda–Stoikov (1806d2d)

- Rolling horizon tau = 60 s (crypto never closes). var = EWMA (5 min half-life) of squared 1 s mid
  changes. k fitted from how often trades print ≥ delta from the mid: lambda(delta) = A e^(−k delta).
  Measured k ≈ 46/$ (Coinbase SOL, hour 00 of 09-28). Quotes rounded outward to the tick and
  re-rounded to the price grid: a float like 121.58000000000001 would miss the book's 121.58 key and
  silently join an "empty" level at the front of the queue.
- **1 h, in-sample k, gamma 3, queue model: A-S −$0.78 vs touch −$31.43**, but on 197 fills vs 2,659.
  Per dollar traded it's genuinely better (spread capture +0.99 vs −0.17 bps; 1 s markout −0.39 vs
  −1.02 bps), so it isn't only trading less. Still slightly negative, and in-sample.
- Protocol from here: **09-27 = calibration/tuning day (k, gamma), 09-28 = untouched test day.**
- Test hygiene: removed an assert whose `or` made it impossible to fail.

## 2026-09-28 — Phase 5: regimes + imbalance skew (regime.py)

- Per-second book-only features (|mid move|, spread, |OFI|, top-of-book updates), produced by ONE
  accumulator used for both HMM training and live quoting, so train and live can't drift apart.
- HMM fit with hmmlearn (EM, diag Gaussian, K by BIC over 2–6). **Live quoting uses a hand-written
  forward filter only.** hmmlearn's predict_proba is forward-backward (smoothed) and uses future
  observations. Test: the filter equals hmmlearn exactly at the last step, and differs from smoothed by
  >5% earlier, which is the lookahead we're avoiding.
- Policy: P(toxic) > 0.5 → widen both quotes by `wide_ticks`; imbalance skew shifts both quotes by
  round(obi_ticks · imbalance) ticks. Ablation: A-S / +skew / +HMM / +both, queue fills.
- Dev run (1 h, in-sample, NOT a result): BIC hit the top of the old 2–4 range, so the range was
  widened; the toxic state (largest mid moves, most updates) covers ~20% of the time.
- Protocol: train k, gamma and the HMM on 2026-09-27; test once on 2026-09-28.

## 2026-09-28 — Phase 6 harness (sensitivity.py)

- Cancel assumption is now a knob: prorata (default) / front (all cancels ahead of us, optimistic) /
  back (none ahead, pessimistic). The sensitivity runner varies one thing at a time: latency 0/100/250 ms,
  fees 1/2.5 bps, cancels front/back.
- Dev run (1 h, in-sample, NOT a result), Coinbase SOL A-S: PnL falls from −$0.78 to −$4.13 as latency
  goes 50 → 250 ms (1 s markout −0.39 → −0.99 bps): slow quotes get picked off. Every 1 bps of fee
  costs ~$1.9 on ~$19k notional, which is already larger than all the spread captured ($1.84). At retail
  fees this doesn't work; the report will say so.
- Floating-point lesson: rewriting x·a/e as x·(a/e) changed 1 fill of 197 (the queue landing exactly
  on 0). Compare runs with tolerances, and keep the arithmetic order fixed.
