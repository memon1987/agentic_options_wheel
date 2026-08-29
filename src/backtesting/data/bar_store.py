"""Local parquet cache for settled daily underlying bars (FC-060 Layer 2, D4).

Chains have had a cache since FC-042 Track A. **Bars never did.** Every replay
re-fetched them from Alpaca, which cost four network round-trips per symbol per
`evaluate_symbol` call (``Simulator._load_stock_bars`` twice, ``evaluate._closes``
twice) and — much worse for a scenario sweep — meant a warm run was never
actually offline: the chains came off disk and then the very first bar fetch went
to the network, so a socket-blocked run died at step one.

A settled daily bar is immutable, which is what makes this safe. The same
``_is_settled`` rule the chain cache uses applies here: today's session is still
forming, so it is never stored and never claimed as covered.

**Coverage is proven, never assumed.** The parquet carries the *request* window
that produced it (``covered_from``/``covered_to``) alongside the bars, exactly as
``ChainStore`` carries its strike/DTE provenance, and a read is a hit only when
that window is a superset of the request. The hit test cannot be the stored
dates themselves: a weekday with no row is indistinguishable between "a market
holiday" and "we never asked for that range", so a store that inferred coverage
would permanently miss any window whose first or last day is a holiday.

**But the request window is a claim about what was ASKED, and the bars are what
the vendor ANSWERED — the two are not the same thing, and the gap is where this
cache can lie.** So:

* **An empty answer never becomes a coverage claim.** Alpaca returns HTTP 200
  with an empty payload for an unknown symbol, for a window outside the
  account's entitlement, and for a transient outage — all three identical on the
  wire. Recording that as "proven empty" would freeze a vendor hiccup into the
  cache forever, and the only escape would be ``use_cache=False`` or deleting the
  file by hand. ``put`` therefore refuses to widen the window at all when it is
  handed no bars.
* **A TRUNCATED answer is the same class and cannot be detected from inside.**
  If the vendor returns January when a year was asked for, nothing here can tell
  that from "the symbol only traded in January". So the file records
  ``data_from``/``data_to`` — the span of the bars it actually HOLDS, after
  merging any earlier fetch with this one — alongside the request window, and an
  auditor can see the two diverge in one query. The hit test still uses the
  request window, because it has to. **What you can rely on is the data span.**
  When the two disagree by more than a plausible market closure, the file is
  suspect — delete it (``rm cache/backtest/bars/<SYMBOL>.parquet``) and re-run.
* **There is no restatement guard.** A settled daily bar is treated as immutable;
  a vendor that later revises one will not be noticed. Same recipe applies.

Coverage is kept as a SINGLE interval. A request that is disjoint from the stored
window fetches their union rather than appending a second range, so the invariant
"one file, one contiguous covered window" holds with no interval algebra. Bars
are one API call per symbol; the extra days are free.

**No lake mirroring.** Deliberately out of scope (see the plan): bars are one
call per symbol per run on the Cloud Run Job, they are always re-fetchable from
the vendor, and the chain lake exists because chains may not be.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import pandas as pd
import structlog

from .provider import StockBar

logger = structlog.get_logger(__name__)

# Default cache root, and the env var that moves it. Deployment configuration,
# not strategy configuration — same reasoning as CHAIN_LAKE_BUCKET.
DEFAULT_BARS_CACHE_DIR = "cache/backtest/bars"
BARS_CACHE_DIR_ENV = "BACKTEST_BARS_CACHE_DIR"

# Bar columns, then the request-provenance columns (constant within a file).
_BAR_COLUMNS = ["symbol", "bar_date", "open", "high", "low", "close", "volume"]
# `covered_*` is the REQUEST window (what the hit test compares against);
# `data_*` is the span the vendor actually returned. They differ legitimately
# (holidays at the edges) and illegitimately (a truncated response), and only
# recording the first would hide the second entirely. See the module docstring.
_PROVENANCE_COLUMNS = ["covered_from", "covered_to", "data_from", "data_to"]
_COLUMNS = _BAR_COLUMNS + _PROVENANCE_COLUMNS


def _is_settled(bar_date: date) -> bool:
    """Whether a daily bar is a completed session.

    Deliberately a local copy of ``alpaca_provider._is_settled`` rather than an
    import: this module must apply the rule to any provider's output, including
    a test double that has never heard of Alpaca. The two are asserted equal by
    ``tests/test_bar_store.py`` so they cannot drift.
    """
    return bar_date < date.today()


def last_settled_day(today: Optional[date] = None) -> date:
    """The most recent date whose daily bar is final."""
    return (today or date.today()) - timedelta(days=1)


class BarStore:
    """Parquet-per-symbol cache of settled daily bars."""

    def __init__(self, root: Optional[str] = None) -> None:
        self._root = Path(root or os.environ.get(BARS_CACHE_DIR_ENV)
                          or DEFAULT_BARS_CACHE_DIR)

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, symbol: str) -> Path:
        return self._root / f"{symbol.upper()}.parquet"

    def has(self, symbol: str) -> bool:
        """Whether a file exists at all (says nothing about coverage)."""
        return self._path(symbol).exists()

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def _read(self, symbol: str) -> Optional[pd.DataFrame]:
        """The stored frame, or None when there is nothing usable on disk.

        An unreadable file is deleted and treated as a miss, for the same reason
        ``ChainStore.get`` does it: the caller can always rebuild from the
        provider, while propagating the error would wedge every future run on
        one corrupt byte.
        """
        path = self._path(symbol)
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
        except Exception:
            logger.warning(
                "Discarding an unreadable bar-cache file",
                event_category="backtest_data",
                event_type="bar_cache_corrupt",
                symbol=symbol, path=str(path),
            )
            path.unlink(missing_ok=True)
            return None
        for column in _COLUMNS:
            if column not in df.columns:
                logger.warning(
                    "Discarding a bar-cache file with an unexpected schema",
                    event_category="backtest_data",
                    event_type="bar_cache_schema_mismatch",
                    symbol=symbol, path=str(path),
                    missing=column,
                )
                path.unlink(missing_ok=True)
                return None
        return df

    def covered_window(self, symbol: str) -> Optional["tuple[date, date]"]:
        """The request window the stored file proves it covers, or None."""
        df = self._read(symbol)
        return None if df is None else self._window(df)

    @staticmethod
    def _window(df: pd.DataFrame) -> Optional["tuple[date, date]"]:
        if df.empty:
            return None
        lo, hi = df["covered_from"].iloc[0], df["covered_to"].iloc[0]
        if pd.isna(lo) or pd.isna(hi):
            return None
        try:
            return date.fromisoformat(str(lo)), date.fromisoformat(str(hi))
        except ValueError:
            return None

    def get(self, symbol: str, start: date, end: date) -> Optional[List[StockBar]]:
        """Bars over ``[start, end]``, or None when coverage cannot be proven.

        ``end`` is first clamped to the last settled session: a request that runs
        into today can be fully answered by a cache that stops at yesterday,
        because that is all the provider would return either.
        """
        effective_end = min(end, last_settled_day())
        if effective_end < start:
            # Nothing settled in the window. The provider would return [] too,
            # so answering from here costs a round-trip and changes nothing.
            return []
        df = self._read(symbol)
        if df is None:
            return None
        window = self._window(df)
        if window is None:
            return None
        covered_from, covered_to = window
        if covered_from > start or covered_to < effective_end:
            return None
        return self._decode(df, symbol, start, effective_end)

    def _decode(
        self, df: pd.DataFrame, symbol: str, start: date, end: date
    ) -> Optional[List[StockBar]]:
        """``_to_bars``, with a malformed frame treated as a miss.

        A file whose schema passed but whose CONTENT is garbage — a ``bar_date``
        that is not an ISO date, a volume that will not cast — must behave
        exactly like the unreadable-parquet case one layer up: discard, log,
        refetch. Raising instead would wedge every future run on one bad cell,
        which is the failure this cache's durability story exists to prevent.
        """
        try:
            return self._to_bars(df, symbol, start, end)
        except Exception:
            logger.warning(
                "Discarding a bar-cache file with unreadable contents",
                event_category="backtest_data",
                event_type="bar_cache_unreadable_rows",
                symbol=symbol, path=str(self._path(symbol)),
            )
            self._path(symbol).unlink(missing_ok=True)
            return None

    @staticmethod
    def _to_bars(
        df: pd.DataFrame, symbol: str, start: date, end: date
    ) -> List[StockBar]:
        out: List[StockBar] = []
        for row in df.itertuples(index=False):
            raw = str(row.bar_date)
            if not raw:
                continue  # legacy metadata-only row; no longer written
            bar_date = date.fromisoformat(raw)
            if bar_date < start or bar_date > end:
                continue
            out.append(
                StockBar(
                    symbol=str(row.symbol),
                    bar_date=bar_date,
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=int(row.volume),
                )
            )
        out.sort(key=lambda b: b.bar_date)
        return out

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #
    def put(
        self,
        symbol: str,
        bars: Sequence[StockBar],
        *,
        covered_from: date,
        covered_to: date,
    ) -> None:
        """Merge ``bars`` into the symbol's file and widen its covered window.

        **An empty answer is never recorded as coverage.** ``bars`` carrying no
        settled row leaves the file exactly as it was — no new claim, no
        metadata-only placeholder — so the next run asks the vendor again.
        Alpaca answers an unknown symbol, an unentitled window and a transient
        outage identically (HTTP 200, empty payload), and a cache that believed
        any of them would serve "this symbol has no history" forever, with
        ``use_cache=False`` or a manual delete as the only escape. Re-asking
        costs one call per run; believing costs a silently empty backtest.

        Unsettled bars are dropped and ``covered_to`` is clamped to the last
        settled session, so a run started mid-session can never leave a file
        claiming to cover a day whose close had not happened yet.

        The write is atomic (temp file in the same directory, then
        ``os.replace``), for the same reason ``ChainStore.put`` is: an interrupted
        write otherwise leaves a truncated parquet that every later run trips on.
        """
        settled_to = last_settled_day()
        covered_to = min(covered_to, settled_to)
        if covered_to < covered_from:
            return  # nothing settled in the requested window; nothing to record

        fresh = [b for b in bars if _is_settled(b.bar_date)]
        if not fresh:
            # See the docstring: an empty answer is indistinguishable from an
            # outage, so it buys no coverage. Nothing is written at all — not
            # even a widening of an existing window, which would extend a claim
            # over days the vendor never answered for.
            logger.info(
                "Bar fetch returned nothing settled; recording no coverage claim",
                event_category="backtest_data",
                event_type="bar_cache_empty_response",
                symbol=symbol,
                requested=f"{covered_from}..{covered_to}",
            )
            return

        merged: Dict[date, StockBar] = {}
        existing = self._read(symbol)
        existing_window = None if existing is None else self._window(existing)
        if existing is not None:
            previous = self._decode(existing, symbol, date.min, settled_to)
            if previous is None:
                # The old file's schema passed but its CONTENT would not parse,
                # so `_decode` discarded it. Its window has to go with it: keeping
                # it would let the file we are about to write claim the old
                # coverage while holding only `fresh`, which is the "claim
                # without data behind it" failure this whole store is built to
                # avoid — and worse than the empty-response case, because it
                # would look like a healthy file.
                existing_window = None
            else:
                for bar in previous:
                    merged[bar.bar_date] = bar
        for bar in fresh:
            merged[bar.bar_date] = bar

        if existing_window is not None:
            covered_from = min(covered_from, existing_window[0])
            covered_to = max(covered_to, min(existing_window[1], settled_to))

        ordered = sorted(merged.values(), key=lambda x: x.bar_date)
        # The span this file actually HOLDS, after merging what was already
        # stored with what just arrived — not "what the vendor answered on this
        # call", which is only the `fresh` half. Recorded beside the request
        # window so the two can be compared in one read: a stored span far
        # narrower than the claimed window is the truncation signal, and the
        # module docstring carries the recipe.
        data_from, data_to = ordered[0].bar_date, ordered[-1].bar_date
        rows = [
            {
                "symbol": b.symbol,
                "bar_date": b.bar_date.isoformat(),
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": int(b.volume),
                "covered_from": covered_from.isoformat(),
                "covered_to": covered_to.isoformat(),
                "data_from": data_from.isoformat(),
                "data_to": data_to.isoformat(),
            }
            for b in ordered
        ]

        path = self._path(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            pd.DataFrame(rows, columns=_COLUMNS).to_parquet(tmp, index=False)
            os.replace(tmp, path)  # atomic within a filesystem
        finally:
            if tmp.exists():
                tmp.unlink()


class CachedBarProvider:
    """A data provider whose ``get_stock_bars`` is served from a ``BarStore``.

    Composition, not a change to ``AlpacaDataProvider``: the cache is a property
    of *this run*, not of the vendor client, and wrapping keeps the provider's
    own per-process contract memo intact. Everything other than
    ``get_stock_bars`` delegates untouched.

    ``fetches`` counts the round-trips that actually reached the wrapped
    provider. It is what the scenario runner asserts is zero during replays —
    a log line would let a regression pass unnoticed.
    """

    def __init__(self, provider, store: Optional[BarStore] = None) -> None:
        self._provider = provider
        self.store = store if store is not None else BarStore()
        self.hits = 0
        self.fetches = 0

    # -- delegation ---------------------------------------------------- #
    def get_contract_universe(self, *args, **kwargs):
        return self._provider.get_contract_universe(*args, **kwargs)

    def get_option_bars(self, *args, **kwargs):
        return self._provider.get_option_bars(*args, **kwargs)

    def __getattr__(self, name):
        # Anything else on the wrapped provider (`_universe_cache`, feed
        # settings, vendor-specific helpers) stays reachable.
        return getattr(self._provider, name)

    # -- the cached call ----------------------------------------------- #
    def get_stock_bars(self, symbol: str, start: date, end: date) -> List[StockBar]:
        cached = self.store.get(symbol, start, end)
        if cached is not None:
            self.hits += 1
            return cached

        # One fetch for the whole range: the union of what is asked for and what
        # is already stored, so coverage stays a single contiguous interval.
        fetch_from, fetch_to = start, end
        window = self.store.covered_window(symbol)
        if window is not None:
            fetch_from = min(fetch_from, window[0])
            fetch_to = max(fetch_to, window[1])

        self.fetches += 1
        bars = self._provider.get_stock_bars(symbol, fetch_from, fetch_to)
        self.store.put(symbol, bars, covered_from=fetch_from, covered_to=fetch_to)
        return [b for b in bars if start <= b.bar_date <= end]
