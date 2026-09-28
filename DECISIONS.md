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
