"""Phase 3: event-driven market-making backtester on recorded L2 books + trades.

Two fill models, because the gap between them is the first result:
  naive  - filled in full whenever a trade prints at or through our price.
  queue  - we join the BACK of the queue at our price. Trades at our price eat the queue ahead of
           us first; unexplained drops in the level's size are cancellations, assumed to come from
           ahead of us in proportion to our position (ahead / level size). A trade through our
           price, or the opposite side crossing it, fills us in full.
Orders and cancels reach the exchange `latency_ns` after we decide. Our own orders are assumed too
small to move the market. Trade direction isn't read from the feed's side field: a trade at or below
our bid can only be a sell into the bids, since our bid is below the best ask.

Run:  python backtest.py 2026-09-28 coinbase SOL-USD
Out:  table on stdout + reports/backtest-<day>-<venue>-<sym>.png
"""
import heapq
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from book import files, replay

REPORTS = Path(__file__).parent / "reports"
BID, ASK = 1, -1


@dataclass
class Order:
    side: int
    px: float
    size: float
    live_at: int            # ns when it reaches the exchange
    ahead: float = None     # queue ahead of us; None until live
    level: float = 0.0      # level size (excluding us) when last seen
    traded: float = 0.0     # volume traded at our price since `level` was seen
    cancel_at: int = None   # ns when our cancel reaches the exchange


class Sim:
    def __init__(self, strategy, queue=True, latency_ns=50_000_000, fee_bps=0.0, cancels="prorata"):
        # cancels: which share of unexplained level shrinkage was ahead of us.
        #   "prorata" ahead/level (default) | "front" all of it (optimistic) | "back" none (pessimistic)
        assert cancels in ("prorata", "front", "back"), cancels
        self.strategy, self.queue, self.lat, self.fee = strategy, queue, latency_ns, fee_bps / 1e4
        self.cancels = cancels
        self.orders, self.fills = [], []
        self.cash = self.inv = 0.0
        self.mid = self.bids = self.asks = None
        self.t_hist, self.mid_hist, self.pnl_hist = [], [], []

    def fill(self, o, q, t):
        q = min(q, o.size)
        o.size -= q
        self.inv += o.side * q
        self.cash -= o.side * q * o.px + q * o.px * self.fee
        self.fills.append((t, o.side, o.px, q, self.mid))
        if o.size <= 1e-12:
            self.orders.remove(o)

    def activate(self, t):
        """Orders whose latency has elapsed join the queue as the book stands now; due cancels take effect."""
        for o in list(self.orders):
            if o.cancel_at is not None and t >= o.cancel_at:
                self.orders.remove(o)
            elif o.ahead is None and t >= o.live_at:
                o.level = (self.bids if o.side == BID else self.asks).get(o.px, 0.0)
                o.ahead, o.traded = (o.level if self.queue else 0.0), 0.0

    def on_book(self, t, bids, asks):
        self.bids, self.asks = bids, asks
        bb, ba = bids.peekitem(-1)[0], asks.peekitem(0)[0]
        self.mid = (bb + ba) / 2
        self.activate(t)
        for o in list(self.orders):
            side_book = bids if o.side == BID else asks
            if o.ahead is None:
                continue
            if (ba <= o.px) if o.side == BID else (bb >= o.px):
                self.fill(o, o.size, t)  # the other side crossed our price: we were taken
                continue
            if self.queue:
                new = side_book.get(o.px, 0.0)
                expected = o.level - o.traded
                if expected > 0 and expected - new > 0 and self.cancels != "back":
                    gone = expected - new
                    o.ahead -= gone if self.cancels == "front" else gone * o.ahead / expected  # pro rata by position
                o.ahead = max(0.0, min(o.ahead, new))
                o.level, o.traded = new, 0.0
        self.requote(t, bids, asks)
        self.activate(t)  # zero latency: live at once
        self.t_hist.append(t)
        self.mid_hist.append(self.mid)
        self.pnl_hist.append(self.cash + self.inv * self.mid)

    def on_trade(self, t, px, size):
        if self.bids is None:
            return
        self.activate(t)
        for o in list(self.orders):
            if o.ahead is None:
                continue  # not live yet
            through = px < o.px if o.side == BID else px > o.px
            if not (through or px == o.px):
                continue
            if through or not self.queue:
                self.fill(o, o.size, t)
                continue
            o.traded += size
            left = size - o.ahead
            o.ahead = max(0.0, o.ahead - size)
            if left > 0:
                self.fill(o, left, t)

    def requote(self, t, bids, asks):
        want = self.strategy(t, bids, asks, self.inv)
        for side, px in want.items():
            live = [o for o in self.orders if o.side == side and o.cancel_at is None]
            if live and live[0].px == px:
                continue
            for o in live:
                o.cancel_at = t + self.lat
            if px is not None:
                self.orders.append(Order(side, px, self.strategy.size, t + self.lat))


class Touch:
    """Baseline: join the best bid and ask with a fixed size; stop adding to a side at the inventory limit."""

    def __init__(self, size, max_inv):
        self.size, self.max_inv = size, max_inv

    def __call__(self, t, bids, asks, inv):
        return {BID: bids.peekitem(-1)[0] if inv < self.max_inv else None,
                ASK: asks.peekitem(0)[0] if inv > -self.max_inv else None}


class AvellanedaStoikov:
    """Avellaneda & Stoikov (2008) with a rolling horizon (crypto never closes).

    reservation r = mid - inv * gamma * var * tau        (skew away from inventory)
    half-spread   = gamma * var * tau / 2 + ln(1 + gamma / k) / gamma
    var is an EWMA of squared 1 s mid changes ($^2/s); k comes from calibrate_k().
    Quotes are rounded outward to the tick and never cross the book. Quotes at the touch until
    volatility has warmed up.
    """

    def __init__(self, size, max_inv, gamma, k, tau_s=60.0, tick=0.01, halflife_s=300.0, warmup_s=60):
        self.size, self.max_inv, self.gamma, self.k, self.tau = size, max_inv, gamma, k, tau_s
        self.tick, self.dp = tick, max(0, -int(np.floor(np.log10(tick) + 1e-9)))
        self.alpha = 1 - 0.5 ** (1 / halflife_s)
        self.var = self.sec = self.sec_mid = self.start = None
        self.warmup = warmup_s

    def _vol(self, t, mid):
        sec = t // 10**9
        if self.sec is None:
            self.sec, self.sec_mid, self.start = sec, mid, sec
        elif sec > self.sec:  # once per new second: squared change since the last one, spread over the gap
            dv = (mid - self.sec_mid) ** 2 / (sec - self.sec)
            self.var = dv if self.var is None else self.var + self.alpha * (dv - self.var)
            self.sec, self.sec_mid = sec, mid

    def __call__(self, t, bids, asks, inv):
        bb, ba = bids.peekitem(-1)[0], asks.peekitem(0)[0]
        mid = (bb + ba) / 2
        self._vol(t, mid)
        if self.var is None or self.sec - self.start < self.warmup:
            bid, ask = bb, ba
        else:
            r = mid - inv * self.gamma * self.var * self.tau
            half = self.gamma * self.var * self.tau / 2 + np.log1p(self.gamma / self.k) / self.gamma
            bid = min(np.floor((r - half) / self.tick + 1e-9) * self.tick, ba - self.tick)
            ask = max(np.ceil((r + half) / self.tick - 1e-9) * self.tick, bb + self.tick)
        # Round to the price grid so the price matches the book's keys exactly.
        return {BID: round(bid, self.dp) if inv < self.max_inv else None,
                ASK: round(ask, self.dp) if inv > -self.max_inv else None}


def calibrate_k(venue, sym, day, min_count=20):
    """Fit lambda(delta) = A exp(-k delta): rate of trades printing at least delta from the mid.

    Standard A-S calibration proxy for how fill intensity decays with quote distance.
    Returns (A per second, k per $, tick).
    """
    import pandas as pd
    from tob import OUT
    tob = pd.read_parquet(OUT / venue / sym / "tob" / f"date={day}.parquet")
    tt, mid = tob.recv_ns.values, ((tob.bid_px + tob.ask_px) / 2).values
    tick = (tob.ask_px - tob.bid_px)[lambda x: x > 0].min()
    px, tr_t = [], []
    for t, _, (p, _) in trade_events(venue, sym, day):
        tr_t.append(t); px.append(p)
    i = np.searchsorted(tt, np.array(tr_t), side="right") - 1
    ok = i >= 0
    d = np.abs(np.array(px)[ok] - mid[i[ok]])
    T = (tt[-1] - tt[0]) / 1e9
    deltas = np.arange(1, 40) * tick / 2  # half-tick grid
    lam = np.array([(d >= x - 1e-9).sum() for x in deltas])
    use = lam >= min_count
    slope, intercept = np.polyfit(deltas[use], np.log(lam[use] / T), 1)
    return float(np.exp(intercept)), float(-slope), float(round(tick, 10))


def trade_events(venue, sym, day):
    for f in files(venue, sym, "trades", day):
        d = pq.read_table(f, columns=["recv_ns", "is_snapshot", "price", "size"]).to_pydict()
        for t, snap, px, q in zip(d["recv_ns"], d["is_snapshot"], d["price"], d["size"]):
            if not snap:  # connect-time trade snapshots are history, not flow
                yield t, 0, (px, q)


def book_events(venue, sym, day):
    for t, _, bids, asks, _ in replay(venue, sym, day, {}):
        if bids and asks:
            yield t, 1, (bids, asks)


def run(sim, venue, sym, day):
    # At equal timestamps trades go first: a trade usually precedes the book update that shows it.
    for t, kind, x in heapq.merge(trade_events(venue, sym, day), book_events(venue, sym, day), key=lambda e: e[:2]):
        if kind == 0:
            sim.on_trade(t, *x)
        else:
            sim.on_book(t, *x)
    return sim


def summary(sim):
    t, mid = np.array(sim.t_hist), np.array(sim.mid_hist)
    f = np.array(sim.fills) if sim.fills else np.zeros((0, 5))
    ft, side, px, q, fmid = f.T
    notional = (px * q).sum()
    spread = (side * (fmid - px) * q).sum()          # bought below / sold above the mid
    fees = notional * sim.fee
    total = sim.cash + sim.inv * mid[-1]
    out = {"fills": len(f), "notional_usd": notional, "pnl_usd": total, "spread_capture_usd": spread,
           "fees_usd": fees, "inventory_pnl_usd": total - spread + fees, "final_inv": sim.inv,
           "max_abs_inv": np.abs(np.cumsum(side * q)).max() if len(f) else 0.0}
    for h in (1, 10, 60):  # markout: mid h seconds after the fill vs fill price, per unit notional
        later = mid[np.minimum(np.searchsorted(t, ft + h * 10**9), len(mid) - 1)]
        out[f"markout_{h}s_bps"] = (side * (later - px) * q).sum() / notional * 1e4 if notional else 0.0
    return out


def main(day, venue, sym, size=1.0, max_inv=5.0, latency_ms=50, gamma=3.0, calib_day=None):
    """Touch quoter under both fill models, then Avellaneda-Stoikov under the queue model.
    k is calibrated on `calib_day` (default: the same day, i.e. IN-SAMPLE; say so when reporting)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # CLI kwargs arrive as strings: cast once here, not at each use.
    size, max_inv, latency_ms, gamma = float(size), float(max_inv), int(latency_ms), float(gamma)
    A, k, tick = calibrate_k(venue, sym, calib_day or day)
    print(f"k calibration on {calib_day or day}{' (IN-SAMPLE)' if not calib_day else ''}: "
          f"A={A:.3f}/s k={k:.1f}/$ tick={tick}")
    runs = {"touch_naive": (Touch(size, max_inv), False), "touch_queue": (Touch(size, max_inv), True),
            "as_queue": (AvellanedaStoikov(size, max_inv, gamma, k, tick=tick), True)}
    rows, fig = {}, plt.figure(figsize=(7, 3.5))
    for name, (strat, queue) in runs.items():
        sim = run(Sim(strat, queue=queue, latency_ns=latency_ms * 10**6), venue, sym, day)
        rows[name] = summary(sim)
        hrs = (np.array(sim.t_hist) - sim.t_hist[0]) / 3.6e12
        plt.plot(hrs, sim.pnl_hist, label=name)
    plt.axhline(0, color="k", lw=.5)
    plt.gca().set(xlabel="hours", ylabel="PnL (USD, marked to mid)",
                  title=f"{venue} {sym} {day}: size {size}, max inv {max_inv}, {latency_ms} ms, gamma {gamma}")
    plt.legend()
    plt.tight_layout()
    REPORTS.mkdir(exist_ok=True)
    png = REPORTS / f"backtest-{day}-{venue}-{sym}-g{gamma:g}.png"  # gamma in the name: sweeps must not overwrite
    fig.savefig(png, dpi=120)
    print(f"{'':22}" + "".join(f"{n:>18}" for n in rows))
    for key in rows["touch_naive"]:
        print(f"{key:22}" + "".join(f"{rows[n][key]:18,.4f}" for n in rows))
    print(f"\nwrote {png}")
    return rows


if __name__ == "__main__":
    main(*sys.argv[1:4], **dict(a.split("=", 1) for a in sys.argv[4:]))  # e.g. gamma=5 calib_day=2026-09-27
