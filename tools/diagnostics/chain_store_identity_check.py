#!/usr/bin/env python3
"""FC-060 Layer 2 (D5) — prove the fast row conversion equals the old one, on REAL files.

PURPOSE
-------
``ChainStore.get`` used to build ``ChainQuote``s with ``df.iterrows()`` plus a
per-cell label lookup; FC-060 Layer 2 replaced that with a vectorised narrowing
mask and ``itertuples``, measured at 5.1x (AAPL) / 5.6x (NVDA) over a
symbol-year. The speed is worthless if the output moved by a bit — the cache's
whole contract is that it is *invisible to results*, not merely fast.

``tests/test_backtest_data.py::TestRowConversionIsIdenticalToTheLegacyLoop``
pins that on fixtures. Fixtures cannot pin all of it: they go through this
repo's own writer, so they can only contain shapes the writer emits. Real vendor
files carry shapes nobody in a test constructs — a column dtype promoted by a
whole-file NaN, an integer volume that landed as a float, a session whose entire
IV column failed to solve. **This walks the developer's actual chain cache and
compares the two converters file by file, field by field, including the Python
TYPE of every field.**

It lives here rather than in the suite deliberately. It reads
``cache/backtest/chains``, which is gitignored and absent in CI, so as a test it
was either skipped (proving nothing where it matters) or silently dependent on
one developer's disk — the same "a test that depends on ambient state" defect
this repo has fixed four times (live BigQuery, missing FastAPI, missing Alpaca
credentials, the chain lake). As a diagnostic it is honest about what it needs
and it is re-runnable on demand, which is what a proof of this shape is for.

USAGE
-----
    python tools/diagnostics/chain_store_identity_check.py                # all files
    python tools/diagnostics/chain_store_identity_check.py --symbol NVDA
    python tools/diagnostics/chain_store_identity_check.py --sample 200
    python tools/diagnostics/chain_store_identity_check.py --cache-dir /some/cache

Exit code 0 = every file matched. Non-zero = a mismatch, with the file, the
narrowing and the first differing quote printed.

Plan: docs/plans/fc-060-scenario-runner.md (D5). Companion:
docs/BACKTEST_ENGINE.md §"Scenario sweeps".
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from src.backtesting.data.chain_builder import ChainQuote  # noqa: E402
from src.backtesting.data.chain_store import ChainStore  # noqa: E402

DEFAULT_CACHE_DIR = "cache/backtest/chains"


# --------------------------------------------------------------------------- #
# The pre-FC-060 conversion, verbatim. This is the ORACLE — do not "improve" it.
# --------------------------------------------------------------------------- #
def row_to_quote_legacy(r) -> ChainQuote:
    """``ChainStore._row_to_quote`` as it stood before FC-060 Layer 2."""
    return ChainQuote(
        symbol=r["symbol"],
        underlying=r["underlying"],
        as_of=date.fromisoformat(r["as_of"]),
        expiration=date.fromisoformat(r["expiration"]),
        strike=float(r["strike"]),
        option_type=r["option_type"],
        dte=int(r["dte"]),
        underlying_price=float(r["underlying_price"]),
        mark=float(r["mark"]),
        bid=float(r["bid"]),
        ask=float(r["ask"]),
        implied_volatility=(
            None if pd.isna(r["implied_volatility"]) else float(r["implied_volatility"])
        ),
        delta=(None if pd.isna(r["delta"]) else float(r["delta"])),
        volume=int(r["volume"]),
        modeled_spread=bool(r["modeled_spread"]),
        modeled_greeks=bool(r["modeled_greeks"]),
    )


def legacy_narrow_and_convert(df, universe_dte=None, strike_gte=None, strike_lte=None):
    """The narrow-and-convert loop out of the pre-FC-060 ``ChainStore.get``."""
    puts, calls = [], []
    for _, r in df.iterrows():
        if not r["symbol"]:  # sentinel row for an empty chain
            continue
        strike = float(r["strike"])
        if strike_gte is not None and strike < strike_gte:
            continue
        if strike_lte is not None and strike > strike_lte:
            continue
        if universe_dte is not None and int(r["dte"]) > universe_dte:
            continue
        q = row_to_quote_legacy(r)
        (puts if q.option_type == "put" else calls).append(q)
    puts.sort(key=lambda x: x.strike)
    calls.sort(key=lambda x: x.strike)
    return puts, calls


# --------------------------------------------------------------------------- #
def _bounds_for(df):
    """The narrowings a real run actually asks for, plus the unbounded read."""
    spot = float(df["underlying_price"].iloc[0])
    return (
        {},
        {"universe_dte": 7},
        {"universe_dte": 8},
        {"strike_gte": spot * 0.95},
        {"strike_lte": spot * 1.05},
        {"universe_dte": 7, "strike_gte": spot * 0.75, "strike_lte": spot * 1.25},
    )


def _first_difference(old, new):
    """A message naming the field and the types, not just 'they differ'."""
    if len(old) != len(new):
        return f"count {len(old)} -> {len(new)}"
    for a, b in zip(old, new):
        if a != b:
            for name, value in a.__dict__.items():
                other = getattr(b, name)
                if value != other or type(value) is not type(other):
                    return (f"{a.symbol}.{name}: {value!r} ({type(value).__name__}) "
                            f"-> {other!r} ({type(other).__name__})")
            return f"{a.symbol}: unequal but no field differs (dataclass __eq__?)"
    for a, b in zip(old, new):
        for name, value in a.__dict__.items():
            other = getattr(b, name)
            if type(value) is not type(other):
                return (f"{a.symbol}.{name}: type {type(value).__name__} -> "
                        f"{type(other).__name__} (values equal)")
    return None


def check_file(path: Path) -> list:
    """Every mismatch in one cached file, as printable strings."""
    problems = []
    df = pd.read_parquet(path)
    for bounds in _bounds_for(df):
        new_puts, new_calls = ChainStore._rows_to_quotes(
            df, bounds.get("universe_dte"), bounds.get("strike_gte"),
            bounds.get("strike_lte"),
        )
        new_puts = sorted(new_puts, key=lambda x: x.strike)
        new_calls = sorted(new_calls, key=lambda x: x.strike)
        old_puts, old_calls = legacy_narrow_and_convert(df, **bounds)
        for leg, old, new in (("puts", old_puts, new_puts),
                              ("calls", old_calls, new_calls)):
            diff = _first_difference(old, new)
            if diff is not None:
                problems.append(f"{path} {bounds} {leg}: {diff}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                        help=f"chain cache root (default {DEFAULT_CACHE_DIR})")
    parser.add_argument("--symbol", help="check one underlying only")
    parser.add_argument("--sample", type=int, default=0,
                        help="check an evenly-spaced sample of N files (0 = all)")
    args = parser.parse_args()

    root = Path(args.cache_dir)
    if not root.is_dir():
        print(f"no chain cache at {root} — nothing to check. This tool reads the "
              f"developer's local cache, which is gitignored.", file=sys.stderr)
        return 2

    pattern = f"{args.symbol.upper()}/*.parquet" if args.symbol else "*/*.parquet"
    files = sorted(root.glob(pattern))
    if not files:
        print(f"no parquet files under {root}/{pattern}", file=sys.stderr)
        return 2
    total = len(files)
    if args.sample and args.sample < total:
        step = max(1, total // args.sample)
        files = files[::step][:args.sample]

    print(f"checking {len(files)} of {total} cached sessions under {root}")
    problems, rows = [], 0
    for i, path in enumerate(files, 1):
        try:
            rows += len(pd.read_parquet(path, columns=["symbol"]))
            problems.extend(check_file(path))
        except Exception as exc:  # noqa: BLE001 - a reader failure is a finding
            problems.append(f"{path}: could not be read — {type(exc).__name__}: {exc}")
        if i % 500 == 0:
            print(f"  ...{i}/{len(files)}")

    print(f"\n{len(files)} files, {rows} rows, "
          f"{len(_bounds_for(pd.read_parquet(files[0])))} narrowings each")
    if problems:
        print(f"\nMISMATCHES: {len(problems)}")
        for line in problems[:40]:
            print(f"  {line}")
        if len(problems) > 40:
            print(f"  ... and {len(problems) - 40} more")
        return 1
    print("IDENTICAL — the rewrite changed the clock and nothing else.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
