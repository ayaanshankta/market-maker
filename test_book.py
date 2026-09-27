"""Synthetic checks for book replay, check.py and tob.py.  Run: python test_book.py"""
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sortedcontainers import SortedDict

import book, check, recorder, tob

tmp = Path(tempfile.mkdtemp())
book.DATA = tmp  # files() defaults to book.DATA via its module global


def write(venue, sym, ns, rows, day="2026-09-28", kind="book"):
    d = tmp / venue / sym / kind / f"date={day}" / "hour=00"
    d.mkdir(parents=True, exist_ok=True)
    sch = recorder.SCHEMAS[kind]
    pq.write_table(pa.Table.from_arrays([pa.array(c).cast(f.type) for c, f in zip(zip(*rows), sch)], schema=sch),
                   d / f"{ns}.parquet")


def row(ns, seq, snap, side, px, qty, ck=None, conn=1):
    return (ns, conn, seq, None, snap, side, px, qty, None, ck)


def ck(bids, asks):
    return recorder.kraken_checksum(SortedDict(bids), SortedDict(asks), 2)


# rollover raises only across a UTC day boundary
day_ns = 86_400_000_000_000
c = 20_000 * day_ns + 3_600 * 10**9
recorder.rollover(c + 10**9, c)
try:
    recorder.rollover((c // day_ns + 1) * day_ns, c)
    raise SystemExit("rollover did not fire")
except recorder.Resync:
    pass

# Coinbase: snapshot in one file, update in a later file, 12 s recording gap
write("coinbase", "BTC-USD", 2_000, [row(2_000, 0, True, "bid", 100.0, 1.0), row(2_000, 0, True, "ask", 101.0, 1.0)])
write("coinbase", "BTC-USD", 12_000_002_000, [row(12_000_002_000, 5, False, "bid", 100.5, 2.0)])
assert check.check("coinbase", "BTC-USD", None)

# Kraken: day 1 has the snapshot; day 2 continues that connection (unseeded), then a rollover snapshot
b1, a1 = {120.86: 1.5}, {120.87: 2.0}
write("kraken", "SOL-USD", 1_000, [row(1_000, 1, True, "bid", 120.86, 1.5, ck(b1, a1)),
                                   row(1_000, 1, True, "ask", 120.87, 2.0, ck(b1, a1))], "2026-09-28")
write("kraken", "SOL-USD", 2_000, [row(2_000, 2, False, "bid", 120.85, 0.25, ck({**b1, 120.85: 0.25}, a1))], "2026-09-29")
write("kraken", "SOL-USD", 3_000, [row(3_000, 1, True, "bid", 120.86, 1.5, ck(b1, a1), 2),
                                   row(3_000, 1, True, "ask", 120.87, 2.0, ck(b1, a1), 2)], "2026-09-29")
assert check.check("kraken", "SOL-USD", "2026-09-29")  # unseeded rows skipped, not failed
assert check.check("kraken", "SOL-USD", None)          # whole history verifies

# tob: one row per top-5 change, unseeded rows dropped
out = tmp / "derived"
t = pq.read_table(tob.tob("kraken", "SOL-USD", "2026-09-29", out=out)).to_pylist()
assert [r["recv_ns"] for r in t] == [3_000] and t[0]["bid_px"] == 120.86 and t[0]["ask_sz5"] == 2.0, t
t = pq.read_table(tob.tob("coinbase", "BTC-USD", "2026-09-28", out=out)).to_pylist()
assert [(r["bid_px"], r["bid_sz5"]) for r in t] == [(100.0, 1.0), (100.5, 3.0)], t

# crossed book and bad checksum must both fail
write("kraken", "SOL-USD", 4_000, [row(4_000, 3, False, "bid", 121.0, 1.0, 123, 2)], "2026-09-29")
assert not check.check("kraken", "SOL-USD", None)

print("\nALL TESTS PASS")
