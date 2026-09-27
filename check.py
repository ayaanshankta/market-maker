"""Sanity-check recorded data: rebuild each venue's book from the files, confirm it never crosses,
re-verify Kraken's checksums offline, and report recording gaps.

Run:  python check.py                (all data)
      python check.py 2026-09-27     (one day)
"""
import sys

import pyarrow.parquet as pq

from book import QUIET_GAP_S, files, replay
from recorder import DATA, KRAKEN_PRICE_DP, kraken_checksum


def check(venue, sym, day):
    stats = {}
    crossed = checked = bad = 0
    bids = asks = None
    for _, _, bids, asks, checksum in replay(venue, sym, day, stats):
        if checksum is not None:
            checked += 1
            bad += kraken_checksum(bids, asks, KRAKEN_PRICE_DP[sym]) != checksum
        if bids and asks and bids.peekitem(-1)[0] >= asks.peekitem(0)[0]:
            crossed += 1

    trades = sum(pq.read_metadata(f).num_rows for f in files(venue, sym, "trades", day))
    print(f"\n== {venue} {sym}: book rows {stats['n']:,}  trade rows {trades:,}")
    print(f"connections {len(stats['conns'])}  crossed-book messages {crossed}")
    if stats["unseeded"]:
        print(f"UNVERIFIED rows {stats['unseeded']:,} (connection began before this day, no snapshot to start from)")
    if checked:
        print(f"checksums matched {checked - bad:,}/{checked:,}")
    print(f"recording gaps > {QUIET_GAP_S}s: {len(stats['quiet'])}")
    for a, b in stats["quiet"][:20]:
        print(f"  {a / 1e9:.0f} -> {b / 1e9:.0f}  ({(b - a) / 1e9:.0f}s)")
    if bids and asks:
        bb, ba = bids.peekitem(-1)[0], asks.peekitem(0)[0]
        print(f"final book: {len(bids)} bids / {len(asks)} asks, best {bb} / {ba}, spread {ba - bb:.2f}")
    return crossed == 0 and bad == 0


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else None
    ok = all([check(p.parent.name, p.name, day) for p in sorted(DATA.glob("*/*")) if p.is_dir()])
    sys.exit(0 if ok else "FAILED: crossed book or checksum mismatch -> recorder or rebuild bug")
