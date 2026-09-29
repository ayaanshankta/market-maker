"""Phase 6 walk-forward: train on day D-1, test once on day D, append one row per config to a CSV.
Over many days this turns single-day deltas (noise) into means with confidence intervals.

Configs (all queue fills, 0 fees): touch, A-S, A-S+skew, A-S+HMM, A-S+both at 50 ms, plus A-S at 0 ms
(the latency lever). Parameters are fixed a priori (gamma 3, obi 1 tick, widen 2 ticks, HMM K=3).

Run:  python walkforward.py <test_day> <venue> <sym>        -> appends to reports/walkforward.csv
      python walkforward.py summary                         -> per-config mean, 95% CI, paired vs A-S
"""
import csv
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from backtest import AvellanedaStoikov, Sim, Touch, calibrate_k, run, summary
from regime import Regime, fit

CSV = Path(__file__).parent / "reports" / "walkforward.csv"
GAMMA, OBI_TICKS, WIDE_TICKS, SIZE, MAX_INV = 3.0, 1.0, 2, 1.0, 5.0
FIELDS = ["test_day", "train_day", "venue", "sym", "config", "commit", "fills", "notional_usd", "pnl_usd",
          "spread_capture_usd", "inventory_pnl_usd", "markout_1s_bps", "markout_10s_bps", "markout_60s_bps"]


def test_day(day, venue, sym):
    train = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    _, k, tick = calibrate_k(venue, sym, train)
    model = fit(venue, sym, train, tick)
    a_s = lambda: AvellanedaStoikov(SIZE, MAX_INV, GAMMA, k, tick=tick)
    configs = {"touch": (Touch(SIZE, MAX_INV), 50),
               "as": (Regime(a_s()), 50),
               "as+skew": (Regime(a_s(), obi_ticks=OBI_TICKS), 50),
               "as+hmm": (Regime(a_s(), model, wide_ticks=WIDE_TICKS), 50),
               "as+both": (Regime(a_s(), model, obi_ticks=OBI_TICKS, wide_ticks=WIDE_TICKS), 50),
               "as@0ms": (Regime(a_s()), 0)}
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                            cwd=Path(__file__).parent).stdout.strip()
    CSV.parent.mkdir(exist_ok=True)
    new = not CSV.exists()
    with open(CSV, "a", newline="") as fh:
        w = csv.DictWriter(fh, FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for name, (strat, lat_ms) in configs.items():
            r = summary(run(Sim(strat, queue=True, latency_ns=lat_ms * 10**6), venue, sym, day))
            w.writerow({**r, "test_day": day, "train_day": train, "venue": venue, "sym": sym,
                        "config": name, "commit": commit})
            fh.flush()
            print(f"{day} {venue} {sym} {name:8} pnl {r['pnl_usd']:9.2f}  fills {r['fills']:6.0f}  "
                  f"mko1s {r['markout_1s_bps']:7.3f}")


def ci95(x):
    """Mean and 95% t-interval half-width (n < 2 -> nan)."""
    from scipy import stats
    x = np.asarray(x, float)
    if len(x) < 2:
        return x.mean() if len(x) else np.nan, np.nan
    return x.mean(), stats.t.ppf(0.975, len(x) - 1) * x.std(ddof=1) / np.sqrt(len(x))


def summarize():
    import pandas as pd
    d = pd.read_csv(CSV).drop_duplicates(["test_day", "venue", "sym", "config"], keep="last")
    for (venue, sym), g in d.groupby(["venue", "sym"]):
        days = sorted(g.test_day.unique())
        print(f"\n== {venue} {sym}: {len(days)} test days ({days[0]} .. {days[-1]})")
        print(f"{'config':10}{'pnl/day':>10}{'±95%':>9}{'bps/notional':>14}{'mko1s':>8}   paired vs as (pnl/day ±95%)")
        base = g[g.config == "as"].set_index("test_day").pnl_usd
        for cfg, c in g.groupby("config", sort=False):
            m, h = ci95(c.pnl_usd)
            bps = c.pnl_usd.sum() / c.notional_usd.sum() * 1e4 if c.notional_usd.sum() else np.nan
            diff = (c.set_index("test_day").pnl_usd - base).dropna()
            dm, dh = ci95(diff) if cfg != "as" else (np.nan, np.nan)
            print(f"{cfg:10}{m:10.2f}{h:9.2f}{bps:14.3f}{c.markout_1s_bps.mean():8.3f}   "
                  + ("" if cfg == "as" else f"{dm:+.2f} ± {dh:.2f}"))


if __name__ == "__main__":
    if sys.argv[1] == "summary":
        summarize()
    else:
        test_day(*sys.argv[1:4])
