"""Phase 2 EDA for one UTC day: spreads, trades, and whether book signals predict the next mid move.

Reads derived/ (run tob.py first) and raw trades. Signals are sampled on a 100 ms clock grid (not
per event, which would overweight busy moments) and never across a reconnect.

Run:  python eda.py 2026-09-28
Out:  reports/eda-2026-09-28.md + reports/eda-2026-09-28-*.png
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from book import files
from tob import OUT as DERIVED

REPORTS = Path(__file__).parent / "reports"
GRID_NS = 100_000_000  # 100 ms
HORIZONS = {"100ms": 1, "1s": 10, "10s": 100}  # in grid steps


def load_tob(venue, sym, day):
    t = pd.read_parquet(DERIVED / venue / sym / "tob" / f"date={day}.parquet")
    t["mid"] = (t.bid_px + t.ask_px) / 2
    t["spread"] = t.ask_px - t.bid_px
    # Order flow imbalance at the best level (Cont, Kukanov & Stoikov 2014), per book change.
    same = t.conn_id.eq(t.conn_id.shift())
    pb, qb, pa, qa = (t[c].shift() for c in ("bid_px", "bid_sz", "ask_px", "ask_sz"))
    e = ((t.bid_px >= pb) * t.bid_sz - (t.bid_px <= pb) * qb
         - (t.ask_px <= pa) * t.ask_sz + (t.ask_px >= pa) * qa)
    t["ofi"] = e.where(same, 0.0)
    return t


def grid(t):
    """State of the book at every 100 ms tick (last update at or before it), plus OFI summed per tick."""
    ticks = np.arange(t.recv_ns.iloc[0] // GRID_NS + 1, t.recv_ns.iloc[-1] // GRID_NS + 1) * GRID_NS
    idx = np.searchsorted(t.recv_ns.values, ticks, side="right") - 1
    g = t.iloc[idx].reset_index(drop=True)
    g["tick"] = ticks
    cum = t.ofi.cumsum().values[idx]
    g["ofi_tick"] = np.diff(cum, prepend=cum[0])
    g["obi1"] = (g.bid_sz - g.ask_sz) / (g.bid_sz + g.ask_sz)
    g["obi5"] = (g.bid_sz5 - g.ask_sz5) / (g.bid_sz5 + g.ask_sz5)
    micro = (g.ask_px * g.bid_sz + g.bid_px * g.ask_sz) / (g.bid_sz + g.ask_sz)  # Stoikov-style micro-price
    g["micro_edge"] = micro - g.mid
    for h, k in HORIZONS.items():
        fut = g.mid.shift(-k)
        ok = g.conn_id.eq(g.conn_id.shift(-k))  # never measure across a reconnect
        g[f"dmid_{h}"] = (fut - g.mid).where(ok)
    return g


def trades(venue, sym, day):
    fs = files(venue, sym, "trades", day)
    t = pd.concat([pq.read_table(f, columns=["recv_ns", "is_snapshot", "side", "price", "size"]).to_pandas() for f in fs])
    return t[~t.is_snapshot].sort_values("recv_ns")  # Coinbase's connect-time trade snapshot is history, not flow


def analyse(venue, sym, day):
    t, tr = load_tob(venue, sym, day), trades(venue, sym, day)
    g = grid(t)
    mid = g.mid.median()
    tick = t.spread[t.spread > 0].min()
    row = {"venue": venue, "sym": sym, "hours": (t.recv_ns.iloc[-1] - t.recv_ns.iloc[0]) / 3.6e12,
           "mid": mid, "tick": tick,
           "spread_med_bps": g.spread.median() / mid * 1e4, "spread_p90_bps": g.spread.quantile(.9) / mid * 1e4,
           "at_1_tick_%": (g.spread <= tick * 1.5).mean() * 100,
           # One taker order filling several makers arrives as several trades in one message: count it once.
           "taker_events/min": tr.recv_ns.nunique() / ((tr.recv_ns.iloc[-1] - tr.recv_ns.iloc[0]) / 6e10),
           "taker_usd_med": (tr.price * tr["size"]).groupby(tr.recv_ns).sum().median(),  # tr.size = element count!
           "inter_taker_ms_med": np.median(np.diff(np.unique(tr.recv_ns))) / 1e6}
    for h in HORIZONS:
        d = g[f"dmid_{h}"]
        v = d.notna()
        row[f"moved_{h}_%"] = (d[v] != 0).mean() * 100
        for s in ("obi1", "obi5", "micro_edge"):
            row[f"corr_{s}_{h}"] = g.loc[v, s].corr(d[v])
    # OFI: contemporaneous (CKS's headline) vs predictive (what a market maker can actually use), 1 s buckets
    b = g.assign(bucket=g.tick // 1_000_000_000).groupby(["conn_id", "bucket"]).agg(ofi=("ofi_tick", "sum"), m0=("mid", "first"), m1=("mid", "last"))
    b["dmid"] = b.m1 - b.m0
    row["ofi_R2_same_1s"] = b.ofi.corr(b.dmid) ** 2
    row["ofi_R2_next_1s"] = b.ofi.corr(b.dmid.groupby(level=0).shift(-1)) ** 2
    return row, g


def plot_obi(g, venue, sym, day):
    d = g.dropna(subset=["dmid_1s"])
    d = d.assign(dec=pd.qcut(d.obi1.rank(method="first"), 10, labels=False))
    m = d.groupby("dec").dmid_1s.mean() / d.mid.median() * 1e4
    fig, ax = plt.subplots(figsize=(5, 3))
    ax.bar(m.index + 1, m.values, color="#4C72B0")
    ax.axhline(0, color="k", lw=.5)
    ax.set(xlabel="top-of-book imbalance decile (1 = ask-heavy)", ylabel="mean mid move next 1 s (bps)",
           title=f"{venue} {sym} {day}")
    fig.tight_layout()
    p = REPORTS / f"eda-{day}-obi-{venue}-{sym}.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return p.name


def main(day):
    REPORTS.mkdir(exist_ok=True)
    rows, pics = [], []
    for p in sorted(DERIVED.glob("*/*")):
        if (p / "tob" / f"date={day}.parquet").exists():
            r, g = analyse(p.parent.name, p.name, day)
            rows.append(r)
            pics.append(plot_obi(g, p.parent.name, p.name, day))
    df = pd.DataFrame(rows).set_index(["venue", "sym"])
    out = REPORTS / f"eda-{day}.md"
    out.write_text(f"# EDA {day}\n\n100 ms clock grid; correlations are with the future mid change; "
                   f"nothing measured across a reconnect.\n\n{df.T.round(4).to_markdown()}\n\n"
                   + "".join(f"![]({p})\n" for p in pics))
    print(df.T.round(4).to_string())
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main(sys.argv[1])
