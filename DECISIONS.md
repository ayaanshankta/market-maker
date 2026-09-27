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
