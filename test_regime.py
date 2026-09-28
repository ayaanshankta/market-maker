"""Checks for regime.py.  Run: python test_regime.py"""
import numpy as np
from hmmlearn.hmm import GaussianHMM
from sortedcontainers import SortedDict

from regime import Features, ForwardFilter

# Forward filter == hmmlearn at the LAST step (where filtered and smoothed coincide), and differs
# from smoothed earlier on (the lookahead the filter avoids).
rng = np.random.default_rng(0)
states = np.repeat(rng.integers(0, 2, 40), 25)
X = np.column_stack([rng.normal(3 * states, 1), rng.normal(-states, .5)])
m = GaussianHMM(2, covariance_type="diag", n_iter=100, random_state=0).fit(X)
model = {"startprob": m.startprob_, "transmat": m.transmat_, "means": m.means_,
         "vars": np.array([np.diag(c) for c in m.covars_])}
f = ForwardFilter(model)
filtered = np.array([f.update(x) for x in X])
smoothed = m.predict_proba(X)
assert np.allclose(filtered[-1], smoothed[-1], atol=1e-6), (filtered[-1], smoothed[-1])
assert np.abs(filtered - smoothed).max() > 0.05  # smoothing really does use the future
assert np.allclose(filtered.sum(axis=1), 1)

# Features: one row per closed second; mid change measured across seconds; OFI counted.
ft = Features(tick=0.01)
b = lambda px, q: SortedDict({px: q})
S = 10**9
assert ft.update(0, b(100.00, 1.0), b(100.01, 1.0)) is None
assert ft.update(S // 2, b(100.00, 3.0), b(100.01, 1.0)) is None       # bid size +2 -> OFI +2
row = ft.update(S, b(100.01, 1.0), b(100.02, 1.0))                     # new second closes second 0
assert row is not None and row[0] == 0.0 and abs(row[2] - np.log1p(2.0)) < 1e-12 and abs(row[3] - np.log1p(1)) < 1e-12, row
row = ft.update(2 * S, b(100.01, 1.0), b(100.02, 1.0))                  # closes second 1: mid moved 1 tick
assert abs(row[0] - np.log1p(1.0)) < 1e-9, row

print("ALL REGIME TESTS PASS")
