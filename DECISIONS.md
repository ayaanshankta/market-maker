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
