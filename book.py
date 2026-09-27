"""Replay recorded L2 data into an order book, one message at a time. Shared by check.py and tob.py."""
from pathlib import Path

import pyarrow.parquet as pq
from sortedcontainers import SortedDict  # O(log n) best bid/ask; a plain dict max() is too slow at full depth

from recorder import DATA, KRAKEN_DEPTH, apply, truncate

QUIET_GAP_S = 5  # BTC-USD always has book traffic; silence longer than this means we weren't recording
COLS = ["recv_ns", "conn_id", "seq", "is_snapshot", "side", "price", "qty", "checksum"]


def files(venue, sym, kind, day, data=None):
    # Each file is one flush, written in arrival order, and a message never spans two files.
    # Sorting by name (first recv_ns) gives chronological order without loading everything.
    return sorted(Path(data or DATA).joinpath(venue, sym, kind).glob(f"date={day or '*'}/hour=*/*.parquet"),
                  key=lambda f: int(f.stem))


def replay(venue, sym, day, stats, data=None):
    """Yield (recv_ns, conn_id, bids, asks, checksum) after each fully applied message.

    Streams one file at a time, so memory stays at one 60 s flush, not a whole day (~15 GB as pydict).
    Messages before a connection's first snapshot can't be rebuilt; they're counted in
    stats["unseeded"] and not yielded. `bids`/`asks` are live SortedDicts: copy what you keep.
    """
    bids, asks = SortedDict(), SortedDict()
    conn = last_ns = prev_seq = None
    seeded = False
    stats.update(n=0, unseeded=0, conns=set(), quiet=[])
    for f in files(venue, sym, "book", day, data):
        # Only the columns we need: skips tz-aware timestamps (which need tzdata on Windows).
        book = pq.read_table(f, columns=COLS).to_pydict()
        m = len(book["recv_ns"])
        stats["n"] += m
        stats["conns"].update(book["conn_id"])
        for i in range(m):
            c, s, t = book["conn_id"][i], book["seq"][i], book["recv_ns"][i]
            if c != conn or (book["is_snapshot"][i] and prev_seq != s):
                conn = c  # new connection or new snapshot -> book restarts
                bids.clear(); asks.clear()
                # A connection that began before this day's files has no snapshot here: skip it rather than fail.
                seeded = book["is_snapshot"][i]
            if last_ns and (t - last_ns) / 1e9 > QUIET_GAP_S:
                stats["quiet"].append((last_ns, t))
            apply(bids if book["side"][i] == "bid" else asks, book["price"][i], book["qty"][i])
            last_ns, prev_seq = t, s
            if not seeded:
                stats["unseeded"] += 1
                continue
            if i + 1 < m and book["seq"][i + 1] == s and book["conn_id"][i + 1] == c:
                continue  # message not finished yet
            if book["checksum"][i] is not None:  # Kraken: it only keeps KRAKEN_DEPTH levels
                truncate(bids, asks, KRAKEN_DEPTH)
            yield t, c, bids, asks, book["checksum"][i]
