#!/usr/bin/env python3
"""FC-096 Phase A — is the backfill's strike window wide enough at longer DTE?

Purpose
-------
`chain_builder.STRIKE_WINDOW_PCT = 0.25` was measured on NVDA's 0.10-0.20 delta
band **at 7 DTE**, where that band sits between 0.917x and 1.054x spot. The
backfill stores chains reaching 21 DTE, and the band moves outward with sqrt(T):
a window that clips it does not produce a slow backtest, it produces a *silently
different* one — a nearer strike substituted for the one the strategy would
actually have sold, baked into a file every later run reads.

This is the check that says how far out the band goes, using the engine's OWN
Black-Scholes (`src/backtesting/data/greeks.py`), so the numbers are the ones
`ChainBuilder` would compute rather than a second implementation's.

    PYTHONPATH=. python tools/diagnostics/band_edge_probe.py
    PYTHONPATH=. python tools/diagnostics/band_edge_probe.py --dte 30

Result as of 2026-08-31 (r=0.04, q=0, put delta -0.10, IV 20%-120%):

    IV      7 DTE    21 DTE
    0.40    0.934     0.890
    0.90    0.860     0.778     <- the plan's stress case
    1.20    0.820     0.722     <- worst tested

  * The 0.25 window's floor is 0.750, and the 21-DTE band edge crosses it at
    roughly IV 95%. That is the defect `BACKFILL_STRIKE_WINDOW_PCT = 0.40`
    exists to fix.
  * The 0.40 window's floor is 0.600 and covers the band edge at every IV
    tested, with the tightest margin (0.722 vs 0.600) at IV 120%.

SCOPE LIMIT — read this before quoting it. This is a MODEL probe: it says where
the 0.10-delta strike sits for a given IV, not what IV each live symbol actually
printed. The empirical half of FC-096 Phase A's pre-freeze probe — re-running
this against real 21-DTE chains across the live universe — needs vendor data and
is a precondition of the first historical-widening execution, not of the merge.
If any symbol's measured band edge lands below the 0.40 floor, widen
`BACKFILL_STRIKE_WINDOW_PCT` **before** widening history: a window is stamped
into every file it writes, and narrowing one later is exactly what the
coverage-monotone guard refuses.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.backtesting.data import greeks  # noqa: E402
from src.backtesting.data.chain_builder import STRIKE_WINDOW_PCT  # noqa: E402

SPOT = 100.0
RATE = 0.04
DIVIDEND = 0.0
IVS = (0.20, 0.30, 0.40, 0.50, 0.60, 0.80, 0.90, 1.00, 1.20)


def band_edge(delta: float, dte: int, iv: float):
    """K/S of the put whose delta is ``-delta``, by bisection on the strike."""
    T = greeks.year_fraction(dte)
    lo, hi = 0.01, 2.0 * SPOT
    for _ in range(200):
        mid = (lo + hi) / 2.0
        d = greeks.bs_delta(SPOT, mid, T, RATE, iv, DIVIDEND, "put")
        if d is None:
            return None
        if abs(d) < delta:
            lo = mid          # further OTM than asked for -> raise the strike
        else:
            hi = mid
    return (lo + hi) / 2.0 / SPOT


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dte", type=int, default=21,
                        help="the reach under test (default 21)")
    parser.add_argument("--delta", type=float, default=0.10,
                        help="the band edge, as |delta| (default 0.10)")
    parser.add_argument("--window", type=float, default=0.40,
                        help="the candidate window half-width (default 0.40)")
    args = parser.parse_args()

    print(f"put delta -{args.delta:.2f}, r={RATE}, q={DIVIDEND}\n")
    print(f"{'IV':>6} {'7 DTE':>9} {args.dte:>6} DTE")
    worst = None
    for iv in IVS:
        short = band_edge(args.delta, 7, iv)
        long_ = band_edge(args.delta, args.dte, iv)
        worst = long_ if worst is None else min(worst, long_)
        print(f"{iv:>6.2f} {short:>9.3f} {long_:>10.3f}")

    live_floor = 1.0 - STRIKE_WINDOW_PCT
    floor = 1.0 - args.window
    print(f"\nlowest K/S at {args.dte} DTE: {worst:.3f}")
    print(f"  live {STRIKE_WINDOW_PCT:.2f} window floor {live_floor:.3f} "
          f"-> covers: {worst >= live_floor}")
    print(f"  backfill {args.window:.2f} window floor {floor:.3f} "
          f"-> covers: {worst >= floor}")
    return 0 if worst >= floor else 1


if __name__ == "__main__":
    raise SystemExit(main())
