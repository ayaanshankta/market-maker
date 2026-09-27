"""Sanity-check recorded data: rebuild each venue's book from the files, confirm it never crosses,
re-verify Kraken's checksums offline, and report recording gaps.

Run:  python check.py                (all data)
      python check.py 2026-09-27     (one day)
"""
import sys
from pathlib import Path

import pyarrow.parquet as pq
from sortedcontainers import SortedDict  # O(log n) best bid/ask; a plain dict max() is too slow at full depth

from recorder import DATA, KRAKEN_DEPTH, KRAKEN_PRICE_DP, apply, kraken_checksum, truncate

QUIET_GAP_S = 5  # BTC-USD always has book traffic; silence longer than this means we weren't recording


COLS = ["recv_ns", "conn_id", "seq", "is_snapshot", "side", "price", "qty", "checksum"]


def files(venue, sym, kind, day):
    # Each file is one flush, written in arrival order, and a message never spans two files.
    # Sorting by name (first recv_ns) gives chronological order without loading everything.
    return sorted((DATA / venue / sym / kind).glob(f"date={day or '*'}/hour=*/*.parquet"), key=lambda f: int(f.stem))


def check(venue, sym, day):
    # Streams one file at a time: memory stays at one 60 s flush, not a whole day (~15 GB as pydict).
    bids, asks = SortedDict(), SortedDict()
    conn = last_ns = prev_seq = None
    n = crossed = checked = bad = unseeded = 0
    seeded = False
    conns, quiet = set(), []
    for f in files(venue, sym, "book", day):
        # Only the columns we need: skips tz-aware timestamps (which need tzdata on Windows).
        book = pq.read_table(f, columns=COLS).to_pydict()
        m = len(book["recv_ns"])
        n += m
        conns.update(book["conn_id"])
        for i in range(m):
            c, s, t = book["conn_id"][i], book["seq"][i], book["recv_ns"][i]
            if c != conn or (book["is_snapshot"][i] and prev_seq != s):
                conn = c  # new connection or new snapshot -> book restarts
                bids.clear(); asks.clear()
                # A connection that began before this day's files has no snapshot here: skip it rather than fail.
                seeded = book["is_snapshot"][i]
            if last_ns and (t - last_ns) / 1e9 > QUIET_GAP_S:
                quiet.append((last_ns, t))
            apply(bids if book["side"][i] == "bid" else asks, book["price"][i], book["qty"][i])
            last_ns, prev_seq = t, s
            if not seeded:
                unseeded += 1
                continue
            if i + 1 < m and book["seq"][i + 1] == s and book["conn_id"][i + 1] == c:
                continue  # checks run once per message, after its last row
            if book["checksum"][i] is not None:
                truncate(bids, asks, KRAKEN_DEPTH)
                checked += 1
                bad += kraken_checksum(bids, asks, KRAKEN_PRICE_DP[sym]) != book["checksum"][i]
            if bids and asks and bids.peekitem(-1)[0] >= asks.peekitem(0)[0]:
                crossed += 1

    trades = sum(pq.read_metadata(f).num_rows for f in files(venue, sym, "trades", day))
    print(f"\n== {venue} {sym}: book rows {n:,}  trade rows {trades:,}")
    print(f"connections {len(conns)}  crossed-book messages {crossed}")
    if unseeded:
        print(f"UNVERIFIED rows {unseeded:,} (connection began before this day, no snapshot to start from)")
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
