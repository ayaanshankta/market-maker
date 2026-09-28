"""Phase 5: HMM regime detection + order-book-imbalance skew on top of Avellaneda-Stoikov.

The HMM is FIT offline on a training day (hmmlearn, EM), but at quote time only the FORWARD
filter is used: P(state_t | observations up to t). hmmlearn's predict_proba is forward-backward
(smoothed), which uses future observations -- lookahead bias. Never use it for trading.

The same Features accumulator produces training rows and live observations, so train/serve can't drift.

Run:  python regime.py <train_day> <test_day> <venue> <sym> [gamma=3] [obi_ticks=1] [wide_ticks=2]
      -> ablation table: A-S, +skew, +HMM, +both on the test day (queue fills).
"""
import pickle
import sys
from pathlib import Path

import numpy as np
from scipy.special import logsumexp

from backtest import ASK, BID, AvellanedaStoikov, calibrate_k, run, summary, Sim
from book import replay

MODELS = Path(__file__).parent / "derived" / "models"
FEATURES = ["log_dmid", "log_spread", "log_abs_ofi", "log_updates"]


class Features:
    """Book-only per-second features. update() returns the finished previous second's row, or None."""

    def __init__(self, tick):
        self.tick, self.sec = tick, None

    def update(self, t, bids, asks):
        bb, ba = bids.peekitem(-1), asks.peekitem(0)
        mid, sec, row = (bb[0] + ba[0]) / 2, t // 10**9, None
        if self.sec is None:
            self.sec, self.mid0, self.ofi, self.n, self.prev = sec, mid, 0.0, 0, None
        elif sec != self.sec:  # close the previous second: its mid change runs from the end of the second before
            row = np.array([np.log1p(abs(self.mid - self.mid0) / self.tick),
                            np.log(max(self.spread, self.tick) / self.tick),
                            np.log1p(abs(self.ofi)), np.log1p(self.n)])
            self.sec, self.mid0, self.ofi, self.n = sec, self.mid, 0.0, 0
        top = (bb, ba)
        if self.prev is not None and top != self.prev:  # Cont-Kukanov-Stoikov OFI at the best level
            (pb, qb), (pa, qa) = self.prev
            self.ofi += (bb[0] >= pb) * bb[1] - (bb[0] <= pb) * qb - (ba[0] <= pa) * ba[1] + (ba[0] >= pa) * qa
            self.n += 1
        self.prev, self.mid, self.spread = top, mid, ba[0] - bb[0]
        return row


def training_rows(venue, sym, day, tick):
    f, rows = Features(tick), []
    for t, _, bids, asks, _ in replay(venue, sym, day, {}):
        if bids and asks and (r := f.update(t, bids, asks)) is not None:
            rows.append(r)
    return np.array(rows)


def fit(venue, sym, day, tick, ks=(2, 3, 4, 5, 6), seed=0):
    """Gaussian HMM (diag cov); number of states by BIC. The 'toxic' state has the largest mean mid move."""
    from hmmlearn.hmm import GaussianHMM
    X = training_rows(venue, sym, day, tick)
    best = None
    for k in ks:
        m = GaussianHMM(n_components=k, covariance_type="diag", n_iter=200, random_state=seed).fit(X)
        params = k * k - 1 + 2 * k * X.shape[1]  # transitions + start + means + variances
        bic = -2 * m.score(X) + params * np.log(len(X))
        print(f"  K={k}: BIC {bic:,.0f}  converged {m.monitor_.converged}")
        if best is None or bic < best[0]:
            best = (bic, m)
    m = best[1]
    model = {"startprob": m.startprob_, "transmat": m.transmat_, "means": m.means_,
             "vars": np.array([np.diag(c) for c in m.covars_]), "tick": tick,
             "toxic": int(np.argmax(m.means_[:, 0])), "train": f"{venue} {sym} {day}", "n": len(X)}
    MODELS.mkdir(parents=True, exist_ok=True)
    with open(MODELS / f"hmm-{venue}-{sym}-{day}.pkl", "wb") as fh:
        pickle.dump(model, fh)
    print(f"chose K={len(m.means_)}; toxic state {model['toxic']}; means (cols {FEATURES}):\n{np.round(m.means_, 2)}")
    return model


class ForwardFilter:
    """Online P(state | past observations only). Log-space for stability."""

    def __init__(self, model):
        self.logT = np.log(model["transmat"])
        self.mu, self.var = model["means"], model["vars"]
        self.log_alpha = np.log(np.maximum(model["startprob"], 1e-300))  # a state may never start
        self.started = False

    def update(self, x):
        ll = -0.5 * (np.log(2 * np.pi * self.var) + (x - self.mu) ** 2 / self.var).sum(axis=1)
        prior = self.log_alpha if not self.started else logsumexp(self.log_alpha[:, None] + self.logT, axis=0)
        self.started = True
        a = prior + ll
        self.log_alpha = a - logsumexp(a)
        return np.exp(self.log_alpha)


class Regime:
    """A-S plus optional imbalance skew and HMM-driven widening, applied on top of A-S's quotes."""

    def __init__(self, base: AvellanedaStoikov, model=None, obi_ticks=0.0, wide_ticks=0, threshold=0.5):
        self.base, self.size, self.tick, self.dp = base, base.size, base.tick, base.dp
        self.filter = ForwardFilter(model) if model else None
        self.toxic = model["toxic"] if model else None
        self.features = Features(base.tick)
        self.obi_ticks, self.wide, self.threshold = obi_ticks, wide_ticks, threshold
        self.p_toxic, self.toxic_secs, self.secs = 0.0, 0, 0

    def __call__(self, t, bids, asks, inv):
        q = self.base(t, bids, asks, inv)
        row = self.features.update(t, bids, asks)
        if self.filter is not None and row is not None:
            self.p_toxic = self.filter.update(row)[self.toxic]
            self.secs += 1
            self.toxic_secs += self.p_toxic > self.threshold
        shift = 0.0
        if self.obi_ticks:
            (pb, qb), (pa, qa) = bids.peekitem(-1), asks.peekitem(0)
            shift = round(self.obi_ticks * (qb - qa) / (qb + qa)) * self.tick  # lean toward the heavy side's push
        wide = self.wide * self.tick if (self.filter is not None and self.p_toxic > self.threshold) else 0.0
        bb, ba = bids.peekitem(-1)[0], asks.peekitem(0)[0]
        out = {}
        if q[BID] is not None:
            out[BID] = round(min(q[BID] + shift - wide, ba - self.tick), self.dp)
        else:
            out[BID] = None
        if q[ASK] is not None:
            out[ASK] = round(max(q[ASK] + shift + wide, bb + self.tick), self.dp)
        else:
            out[ASK] = None
        return out


def main(train_day, test_day, venue, sym, gamma=3.0, obi_ticks=1.0, wide_ticks=2, size=1.0, max_inv=5.0):
    # CLI kwargs arrive as strings: cast every numeric one here.
    gamma, obi_ticks, wide_ticks, size, max_inv = float(gamma), float(obi_ticks), int(wide_ticks), float(size), float(max_inv)
    _, k, tick = calibrate_k(venue, sym, train_day)
    print(f"train {train_day}: k={k:.1f}/$ tick={tick}; fitting HMM")
    model = fit(venue, sym, train_day, tick)
    make = lambda: AvellanedaStoikov(size, max_inv, gamma, k, tick=tick)
    configs = {"as": Regime(make()), "as+skew": Regime(make(), obi_ticks=obi_ticks),
               "as+hmm": Regime(make(), model, wide_ticks=wide_ticks),
               "as+both": Regime(make(), model, obi_ticks=obi_ticks, wide_ticks=wide_ticks)}
    rows = {}
    for name, strat in configs.items():
        rows[name] = summary(run(Sim(strat, queue=True), venue, sym, test_day))
        if strat.filter is not None:
            rows[name]["toxic_time_%"] = 100 * strat.toxic_secs / max(strat.secs, 1)
    tag = " (IN-SAMPLE: train == test)" if train_day == test_day else ""
    print(f"\nablation on {test_day}{tag}, {venue} {sym}, gamma {gamma}, obi_ticks {obi_ticks}, wide_ticks {wide_ticks}")
    keys = list(rows["as"])
    print(f"{'':22}" + "".join(f"{n:>12}" for n in rows))
    for key in keys + ["toxic_time_%"]:
        print(f"{key:22}" + "".join(f"{rows[n].get(key, float('nan')):12.4f}" for n in rows))
    return rows


if __name__ == "__main__":
    main(*sys.argv[1:5], **dict(a.split("=", 1) for a in sys.argv[5:]))
