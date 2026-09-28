"""Phase 6: does the result survive worse assumptions? One knob at a time around a base config.

Run:  python sensitivity.py <test_day> <venue> <sym> calib_day=<train_day> gamma=<g>
Out:  one line per variant: pnl, fills, spread capture, 1 s markout.
"""
import sys

from backtest import AvellanedaStoikov, Sim, calibrate_k, run, summary

VARIANTS = [("base", {}),
            ("latency 0 ms", {"latency_ns": 0}), ("latency 100 ms", {"latency_ns": 100_000_000}),
            ("latency 250 ms", {"latency_ns": 250_000_000}),
            ("fee 1 bps", {"fee_bps": 1.0}), ("fee 2.5 bps", {"fee_bps": 2.5}),
            ("cancels front (optimistic)", {"cancels": "front"}), ("cancels back (pessimistic)", {"cancels": "back"})]


def main(day, venue, sym, calib_day, gamma=3.0, size=1.0, max_inv=5.0):
    size, max_inv, gamma = float(size), float(max_inv), float(gamma)  # CLI kwargs arrive as strings
    if calib_day == day:
        print("WARNING: calib_day == test day -> in-sample")
    _, k, tick = calibrate_k(venue, sym, calib_day)
    print(f"{venue} {sym} test {day}, k={k:.1f} from {calib_day}, gamma {gamma}; base = 50 ms, 0 fee, prorata cancels")
    print(f"{'variant':28}{'pnl_usd':>10}{'fills':>8}{'spread_cap':>12}{'mko_1s_bps':>12}")
    for name, kw in VARIANTS:
        kw = {"latency_ns": 50_000_000, **kw}
        r = summary(run(Sim(AvellanedaStoikov(size, max_inv, gamma, k, tick=tick), queue=True, **kw), venue, sym, day))
        print(f"{name:28}{r['pnl_usd']:10.2f}{r['fills']:8.0f}{r['spread_capture_usd']:12.2f}{r['markout_1s_bps']:12.3f}")


if __name__ == "__main__":
    main(*sys.argv[1:4], **dict(a.split("=", 1) for a in sys.argv[4:]))
