"""Hand-made event sequences with known correct fills.  Run: python test_backtest.py"""
from sortedcontainers import SortedDict

from backtest import ASK, BID, Order, Sim

MS = 1_000_000


class Fixed:
    """Quote a fixed bid only, once."""
    size = 1.0

    def __init__(self, px):
        self.px = px

    def __call__(self, t, bids, asks, inv):
        return {BID: self.px}


def book(bid_sz, bid_px=100.0, ask_px=101.0):
    return SortedDict({bid_px: bid_sz}), SortedDict({ask_px: 5.0})


def sim(queue, px=100.0, lat=10 * MS):
    return Sim(Fixed(px), queue=queue, latency_ns=lat)


# Queue: join behind 5; trades eat the queue before reaching us.
s = sim(True)
s.on_book(0, *book(5.0))                  # decide at t=0, live at 10 ms
s.on_trade(5 * MS, 100.0, 3.0)            # before we're live: ignored
assert not s.fills
s.on_book(10 * MS, *book(2.0))            # live now: queue ahead = 2 (current level)
o = s.orders[0]
assert o.ahead == 2.0, o
s.on_trade(11 * MS, 100.0, 1.5)           # ahead 2 -> 0.5, no fill
assert not s.fills and abs(o.ahead - 0.5) < 1e-12
s.on_book(12 * MS, *book(0.5))            # the drop is explained by the trade: no cancels
assert abs(o.ahead - 0.5) < 1e-12
s.on_trade(13 * MS, 100.0, 0.8)           # 0.5 clears the queue, 0.3 fills us
assert len(s.fills) == 1 and abs(s.fills[0][3] - 0.3) < 1e-12 and abs(s.inv - 0.3) < 1e-12
s.on_trade(14 * MS, 99.0, 0.01)           # through our price: the rest fills
assert abs(s.inv - 1.0) < 1e-12 and not s.orders

# Queue: unexplained drops are cancels, pro rata by position.
s = sim(True, lat=0)
s.on_book(0, *book(10.0))                 # live immediately, ahead = 10 (we're last)
s.on_book(1, *book(4.0))                  # 6 cancelled, all ahead of us (ahead/level = 1)
assert abs(s.orders[0].ahead - 4.0) < 1e-12
s.on_book(2, *book(8.0))                  # 4 more join behind us: ahead stays 4
s.on_book(3, *book(6.0))                  # 2 cancelled; half the level is ahead of us -> ahead 3
assert abs(s.orders[0].ahead - 3.0) < 1e-12, s.orders[0].ahead

# Naive: any trade at our price fills in full, regardless of the queue.
s = sim(False, lat=0)
s.on_book(0, *book(50.0))
s.on_trade(1, 100.0, 0.01)
assert abs(s.inv - 1.0) < 1e-12

# The ask crossing our bid fills us (both models).
s = sim(True, lat=0)
s.on_book(0, *book(50.0))
s.on_book(1, SortedDict({99.0: 1.0}), SortedDict({100.0: 1.0}))
assert abs(s.inv - 1.0) < 1e-12

# Accounting and fees: buy 1 @100 with 10 bps fee.
s = Sim(Fixed(100.0), queue=False, latency_ns=0, fee_bps=10)
s.on_book(0, *book(1.0))
s.on_trade(1, 100.0, 1.0)
assert abs(s.cash - (-100.0 - 0.1)) < 1e-9

# Cancel/replace: a new price cancels the old order after latency; the old one can still fill before then.
s = Sim(Fixed(100.0), queue=False, latency_ns=10 * MS)
s.on_book(0, *book(1.0))
s.on_book(10 * MS, *book(1.0))            # live
s.strategy.px = 99.0
s.on_book(11 * MS, *book(1.0))            # cancel reaches exchange at 21 ms
s.on_trade(15 * MS, 100.0, 1.0)           # old order still live: fills
assert abs(s.inv - 1.0) < 1e-12

print("ALL BACKTEST TESTS PASS")
