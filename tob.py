"""Materialise top-of-book (best 5 levels, summarised) from the raw book, once per day.

Replaying a day's raw book takes minutes; everything in Phase 2+ reads this output instead.
One row per message that changes the top 5 levels (deep-book churn is dropped).

Run:  python tob.py 2026-09-28            (all venues/symbols for that UTC day)
Out:  derived/{venue}/{symbol}/tob/date=YYYY-MM-DD.parquet
"""
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from book import replay
from recorder import DATA

OUT = Path(__file__).parent / "derived"
LEVELS = 5
SCHEMA = pa.schema([("recv_ns", pa.int64()), ("conn_id", pa.int64()),
                    ("bid_px", pa.float64()), ("bid_sz", pa.float64()),
                    ("ask_px", pa.float64()), ("ask_sz", pa.float64()),
                    ("bid_sz5", pa.float64()), ("ask_sz5", pa.float64())])  # total size in the top 5 levels


def tob(venue, sym, day, data=None, out=OUT):
    rows, prev, stats = [], None, {}
    for t, c, bids, asks, _ in replay(venue, sym, day, stats, data):
        if not bids or not asks:
            continue
        b, a = bids.items()[-LEVELS:], asks.items()[:LEVELS]
        key = (c, tuple(b), tuple(a))
        if key == prev:
            continue  # change was deeper than the top 5
        prev = key
        rows.append((t, c, b[-1][0], b[-1][1], a[0][0], a[0][1], sum(q for _, q in b), sum(q for _, q in a)))
    dest = out / venue / sym / "tob" / f"date={day}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_arrays([pa.array(col, f.type) for col, f in zip(zip(*rows), SCHEMA)] if rows
                                        else SCHEMA.empty_table().columns, schema=SCHEMA), dest, compression="zstd")
    print(f"{venue} {sym} {day}: {stats['n']:,} book rows -> {len(rows):,} tob rows"
          + (f"  (UNVERIFIED {stats['unseeded']:,} skipped)" if stats["unseeded"] else ""))
    return dest


if __name__ == "__main__":
    for p in sorted(DATA.glob("*/*")):
        if p.is_dir():
            tob(p.parent.name, p.name, sys.argv[1])
