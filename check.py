"""Sanity-check recorded data: rebuild each venue's book from the files, confirm it never crosses,
re-verify Kraken's checksums offline, and report recording gaps.

Run:  python check.py                (all data)
      python check.py 2026-09-27     (one day)
"""
import sys
from pathlib import Path

import pyarrow.dataset as ds
from sortedcontainers import SortedDict  # O(log n) best bid/ask; a plain dict max() is too slow at full depth

from recorder import DATA, KRAKEN_DEPTH, KRAKEN_PRICE_DP, apply, kraken_checksum, truncate

QUIET_GAP_S = 5  # BTC-USD always has book traffic; silence longer than this means we weren't recording


def load(path, day):
    d = ds.dataset(path, format="parquet", partitioning="hive")
    # Arrow's sort is stable, so rows within a message keep their original order.
    return d.to_table(filter=(ds.field("date") == day) if day else None).sort_by([("recv_ns", "ascending")])


def check(venue, sym, day):
    book = load(DATA / venue / sym / "book", day).to_pydict()
    n = len(book["recv_ns"])
    print(f"\n== {venue} {sym}: book rows {n:,}  trade rows {load(DATA / venue / sym / 'trades', day).num_rows:,}")

    bids, asks = SortedDict(), SortedDict()
    conn = last_ns = None
    crossed = checked = bad = 0
    quiet = []
    for i in range(n):
        c, s, t = book["conn_id"][i], book["seq"][i], book["recv_ns"][i]
        if c != conn or (book["is_snapshot"][i] and (i == 0 or book["seq"][i - 1] != s)):
            conn = c  # new connection or new snapshot -> book restarts
            bids.clear(); asks.clear()
        if last_ns and (t - last_ns) / 1e9 > QUIET_GAP_S:
            quiet.append((last_ns, t))
        apply(bids if book["side"][i] == "bid" else asks, book["price"][i], book["qty"][i])
        last_ns = t
        if i + 1 < n and book["seq"][i + 1] == s and book["conn_id"][i + 1] == c:
            continue  # checks run once per message, after its last row
        if book["checksum"][i] is not None:
            truncate(bids, asks, KRAKEN_DEPTH)
            checked += 1
            bad += kraken_checksum(bids, asks, KRAKEN_PRICE_DP[sym]) != book["checksum"][i]
        if bids and asks and bids.peekitem(-1)[0] >= asks.peekitem(0)[0]:
            crossed += 1

    print(f"connections {len(set(book['conn_id']))}  crossed-book messages {crossed}")
    if checked:
        print(f"checksums matched {checked - bad:,}/{checked:,}")
    print(f"recording gaps > {QUIET_GAP_S}s: {len(quiet)}")
    for a, b in quiet[:20]:
        print(f"  {a / 1e9:.0f} -> {b / 1e9:.0f}  ({(b - a) / 1e9:.0f}s)")
    if bids and asks:
        bb, ba = bids.peekitem(-1)[0], asks.peekitem(0)[0]
        print(f"final book: {len(bids)} bids / {len(asks)} asks, best {bb} / {ba}, spread {ba - bb:.2f}")
    return crossed == 0 and bad == 0


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else None
    ok = all([check(p.parent.name, p.name, day) for p in sorted(DATA.glob("*/*")) if p.is_dir()])
    sys.exit(0 if ok else "FAILED: crossed book or checksum mismatch -> recorder or rebuild bug")
