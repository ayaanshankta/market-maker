"""Record BTC/ETH/SOL-USD L2 order books + trades from Coinbase and Kraken to hourly Parquet files.

Run:  python recorder.py            (Ctrl-C / kill flushes and exits)
Out:  data/{venue}/{symbol}/{book,trades}/date=YYYY-MM-DD/hour=HH/<recv_ns>.parquet
"""
import asyncio, json, logging, signal, time, zlib
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import websockets
from sortedcontainers import SortedDict

DATA = Path(__file__).parent / "data"
FLUSH_SECS = 60  # ponytail: a crash loses at most this much; lower it if that matters
KRAKEN_DEPTH = 100  # levels per side; market making lives near the touch. Max 1000.
SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD"]  # SOL is the one we quote; BTC/ETH are lead signals
# Kraken price decimals, from its `instrument` channel on 2026-09-26 (qty is 8 for all).
# If Kraken changes these, every checksum fails loudly -- re-read the instrument channel.
KRAKEN_PRICE_DP = {"BTC-USD": 1, "ETH-USD": 2, "SOL-USD": 2}

log = logging.getLogger("recorder")
TS = pa.timestamp("ns", "UTC")
SCHEMAS = {
    # conn_id resets the book on every reconnect; seq groups rows into messages.
    # checksum is Kraken's CRC32 of the top-10 book after the message (null for Coinbase).
    "book": pa.schema([("recv_ns", pa.int64()), ("conn_id", pa.int64()), ("seq", pa.int64()),
                       ("msg_ts", TS), ("is_snapshot", pa.bool_()), ("side", pa.string()),
                       ("price", pa.float64()), ("qty", pa.float64()), ("event_ts", TS),
                       ("checksum", pa.int64())]),
    "trades": pa.schema([("recv_ns", pa.int64()), ("conn_id", pa.int64()), ("seq", pa.int64()),
                         ("msg_ts", TS), ("is_snapshot", pa.bool_()), ("trade_id", pa.int64()),
                         ("side", pa.string()), ("price", pa.float64()), ("size", pa.float64()),
                         ("event_ts", TS)]),
}
buf = defaultdict(list)  # (venue, symbol, kind) -> rows


class Resync(Exception):
    """Our book can no longer be trusted; reconnect for a fresh snapshot."""


def utc_day(ns):
    return ns // 86_400_000_000_000


def rollover(recv_ns, conn_id):
    # Reconnect at 00:00 UTC so every date= partition starts with a snapshot and can be
    # checked/backtested on its own. Costs ~1 s of data per day. forever() also forces this on a
    # timer; this per-message guard only catches a message that lands after midnight first.
    if utc_day(recv_ns) != utc_day(conn_id):
        raise Resync("daily UTC rollover")


def next_midnight_ns(ns):
    return (utc_day(ns) + 1) * 86_400_000_000_000


def flush():
    for (venue, sym, kind), rows in buf.items():
        if not rows:
            continue
        schema = SCHEMAS[kind]
        arrays = [pa.array(c).cast(f.type) for c, f in zip(zip(*rows), schema)]
        # Partition by the hour of the first row; a file can spill a minute past the hour, which is fine.
        t = datetime.fromtimestamp(rows[0][0] / 1e9, timezone.utc)
        out = DATA / venue / sym / kind / f"date={t:%Y-%m-%d}" / f"hour={t:%H}"
        out.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_arrays(arrays, schema=schema), out / f"{rows[0][0]}.parquet",
                       compression="zstd")
        log.info("%s %s: flushed %d %s rows", venue, sym, len(rows), kind)
        rows.clear()


def kraken_checksum(bids, asks, price_dp):
    """Kraken v2 book checksum: CRC32 over top 10 asks (low->high) then top 10 bids (high->low),
    each level as price+qty with the '.' and leading zeros removed."""
    fmt = lambda x, dp: f"{x:.{dp}f}".replace(".", "").lstrip("0")
    levels = list(asks.items()[:10]) + list(reversed(bids.items()[-10:]))
    return zlib.crc32("".join(fmt(p, price_dp) + fmt(q, 8) for p, q in levels).encode())


def apply(book, price, qty):
    if qty == 0:
        book.pop(price, None)
    else:
        book[price] = qty


def truncate(bids, asks, depth):
    # Kraken only maintains `depth` levels; stale levels beyond it must be dropped to match its checksum.
    while len(bids) > depth:
        bids.popitem(0)
    while len(asks) > depth:
        asks.popitem(-1)


# close_timeout=1: Kraken stalls ~10 s at 00:00 UTC and websockets' default 10 s close handshake
# waited it out (2026-09-29: Kraken rollover gap 12.0 s vs Coinbase 1.6 s). Don't wait to say goodbye.
async def coinbase(conn_id):
    async with websockets.connect("wss://advanced-trade-ws.coinbase.com", max_size=None, ping_interval=20, close_timeout=1) as ws:
        for ch in ("level2", "market_trades", "heartbeats"):  # heartbeats keep the socket alive
            await ws.send(json.dumps({"type": "subscribe", "product_ids": SYMBOLS, "channel": ch}))
        last_seq = -1
        async for raw in ws:
            recv_ns = time.time_ns()  # local receive time, before parsing
            rollover(recv_ns, conn_id)
            msg = json.loads(raw)
            if msg.get("type") == "error":
                raise Resync(msg)
            seq, ch, ts = msg["sequence_num"], msg.get("channel"), msg.get("timestamp")
            for ev in msg.get("events", []):
                snap = ev.get("type") == "snapshot"
                if ch == "l2_data":
                    for u in ev["updates"]:
                        side = "bid" if u["side"] == "bid" else "ask"  # Coinbase says "offer"
                        buf["coinbase", ev["product_id"], "book"].append((recv_ns, conn_id, seq, ts, snap, side, u["price_level"],
                                                        u["new_quantity"], u["event_time"], None))
                elif ch == "market_trades":
                    for t in ev["trades"]:
                        buf["coinbase", t["product_id"], "trades"].append((recv_ns, conn_id, seq, ts, snap, t["trade_id"],
                                                          t["side"].lower(), t["price"], t["size"], t["time"]))
            if seq != last_seq + 1:
                # Missed messages -> our book is wrong.
                raise Resync(f"sequence gap {last_seq} -> {seq}")
            last_seq = seq


async def kraken(conn_id):
    async with websockets.connect("wss://ws.kraken.com/v2", max_size=None, ping_interval=20, close_timeout=1) as ws:
        await ws.send(json.dumps({"method": "subscribe", "params": {
            "channel": "book", "symbol": [s.replace("-", "/") for s in SYMBOLS], "depth": KRAKEN_DEPTH}}))
        await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "trade", "symbol": [s.replace("-", "/") for s in SYMBOLS]}}))
        books = {s: (SortedDict(), SortedDict()) for s in SYMBOLS}
        seq = 0  # Kraken has no sequence numbers; we number messages ourselves. The checksum catches gaps.
        async for raw in ws:
            recv_ns = time.time_ns()
            rollover(recv_ns, conn_id)
            msg = json.loads(raw, parse_float=Decimal)  # exact decimals, or the checksum strings come out wrong
            if msg.get("success") is False or "error" in msg:
                raise Resync(msg)
            seq += 1
            ch, snap = msg.get("channel"), msg.get("type") == "snapshot"
            if ch == "book":
                for d in msg["data"]:
                    sym = d["symbol"].replace("/", "-")
                    bids, asks = books[sym]
                    if snap:
                        bids.clear(); asks.clear()
                    for side, book in (("bid", bids), ("ask", asks)):
                        for lv in d[side + "s"]:
                            buf["kraken", sym, "book"].append((recv_ns, conn_id, seq, None, snap, side, lv["price"],
                                                          lv["qty"], d.get("timestamp"), d["checksum"]))
                            apply(book, lv["price"], lv["qty"])
                    truncate(bids, asks, KRAKEN_DEPTH)
                    if kraken_checksum(bids, asks, KRAKEN_PRICE_DP[sym]) != d["checksum"]:
                        raise Resync(f"{sym} checksum mismatch at seq {seq}")
            elif ch == "trade":
                for t in msg["data"]:
                    buf["kraken", t["symbol"].replace("/", "-"), "trades"].append((recv_ns, conn_id, seq, None, snap, t["trade_id"], t["side"],
                                                    t["price"], t["qty"], t["timestamp"]))


async def forever(venue, fn):
    backoff = 1
    while True:
        started, conn_id = time.monotonic(), time.time_ns()
        midnight = next_midnight_ns(conn_id)
        log.info("%s: connecting conn_id=%d", venue, conn_id)
        try:
            # Timer, not traffic, triggers the rollover: on 2026-09-28 Kraken sent nothing for 10 s after
            # midnight, so waiting for the first post-midnight message cost a 12 s gap.
            await asyncio.wait_for(fn(conn_id), (midnight - time.time_ns()) / 1e9)
        except Exception as e:  # anything unexpected (a malformed message, ...) must not kill both venues
            if not isinstance(e, (OSError, websockets.WebSocketException, Resync, asyncio.TimeoutError)):
                log.exception("%s: unexpected error, reconnecting", venue)
            left = midnight - time.time_ns()
            if isinstance(e, asyncio.TimeoutError) and 0 < left < 60e9:
                # wait_for runs on the monotonic clock, midnight on the wall clock; if clock sync
                # slewed them apart the timer fires a little early. Finish the wait so it's one rollover.
                await asyncio.sleep(left / 1e9)
                left = 0
            if left <= 0:
                e = Resync("daily UTC rollover")
            log.warning("%s: disconnected: %r", venue, e)
        flush()  # never let one file mix two connections (keeps the day's first file starting at its snapshot)
        if time.monotonic() - started > 60 or time.time_ns() >= midnight:
            backoff = 0  # healthy connection or daily rollover: reconnect immediately
        await asyncio.sleep(backoff)
        backoff = min(max(backoff * 2, 1), 60)


async def flusher():
    while True:
        await asyncio.sleep(FLUSH_SECS)
        flush()


async def main():
    await asyncio.gather(forever("coinbase", coinbase), forever("kraken", kraken), flusher())


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()  # native crashes (e.g. access violation) still print the Python stack to stderr
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(Path(__file__).parent / "recorder.log")])
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # `kill` flushes like Ctrl-C
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        flush()
        log.info("stopped")
