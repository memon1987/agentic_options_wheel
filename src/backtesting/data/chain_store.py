"""Parquet cache for point-in-time chains.

Cold-fetching a symbol-year of chains from Alpaca is slow (one contract-discovery
call plus a bars call per decision day, at 200 req/min on the free tier). We
persist each built ChainSnapshot to a local parquet file so re-runs — parameter
sweeps, report regeneration, the parity check — hit disk instead of the API.

Layout: ``<cache_dir>/<UNDERLYING>/<YYYY-MM-DD>.parquet``, one file per chain.
Each row is a ChainQuote; the underlying price rides along as a column (constant
within a file) so a snapshot round-trips without a sidecar.

**Why there is no TTL.** A chain for a settled past session is immutable: it is
derived from that day's final daily bars, which Alpaca does not restate. The
entry can therefore live forever. What the key ``(underlying, as_of)`` does NOT
capture is how *much* of that day was fetched — the DTE reach and the strike
window both narrow the result — so each file also records the request it
answers, and a read is a hit only when the file provably covers the new request
(``_covers``) and is narrowed back to it. Getting that wrong would not be a slow
backtest but a wrong one: a chain missing the strikes the caller asked for looks
exactly like a day on which those strikes did not trade.

Today's session is excluded from the cache upstream — see
``chain_builder._is_cacheable``.

**The lake (FC-060 Layer 1).** The local cache is thrown away every month: the
``backtest-screen`` Cloud Run Job runs on an ephemeral filesystem, so every
monthly screen is cold. ``ChainLake`` mirrors each file to a GCS object at
``gs://<bucket>/<prefix>/<UNDERLYING>/<YYYY-MM-DD>.parquet``, and ``ChainStore``
uses it write-through: a local miss tries the lake before the provider, and a
newly built chain is uploaded after it lands on disk. The lake is optional and
purely additive — a lake failure is logged and degraded to local-only, never
raised, because a GCS hiccup must not turn a two-hour screen into a failed
execution. With no ``CHAIN_LAKE_BUCKET`` configured, no GCS client is ever
constructed and behaviour is byte-identical to the pre-lake store.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import structlog

from .chain_builder import ChainQuote, ChainSnapshot

logger = structlog.get_logger(__name__)

_COLUMNS = [
    "symbol",
    "underlying",
    "as_of",
    "expiration",
    "strike",
    "option_type",
    "dte",
    "underlying_price",
    "mark",
    "bid",
    "ask",
    "implied_volatility",
    "delta",
    "volume",
    "modeled_spread",
    "modeled_greeks",
    # Provenance of the *request* that produced this file, not of any one quote.
    # Constant within a file; see the coverage check in ``get``.
    "universe_dte",
    "strike_gte",
    "strike_lte",
    # Identity of the pricing model that COMPUTED bid/ask/delta/iv above. Those
    # are derived, not fetched, so a file priced under a different model answers
    # a different question — see ChainBuilder._model_fingerprint.
    "model",
]

# Written into the provenance columns when a caller persists a snapshot without
# declaring what window it was built under. Such a file can still be read back
# wholesale, but it can never satisfy a *bounded* request: we cannot prove it
# covers one. Fail closed — a false cache hit is a silently wrong backtest.
_UNKNOWN = float("nan")

# Strike bounds are floats recomputed per run; compare them with a tolerance
# well below one strike increment (the tightest ladders are $0.50 wide).
_BOUND_TOL = 1e-6

# The underlying close is the anchor every delta in the file was computed
# against, so it is compared far more strictly than the window bounds.
_PRICE_TOL = 1e-9


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _PRICE_TOL * max(1.0, abs(a), abs(b))


# Default cache root. Named so ``ChainStore`` and ``from_env`` cannot drift.
DEFAULT_CACHE_DIR = "cache/backtest/chains"

# Object-name prefix inside the lake bucket. The parquet schema (``_COLUMNS``
# plus the provenance columns) *is* the format contract for these objects: bump
# this prefix if it ever changes incompatibly, so old and new files can never be
# read as each other.
DEFAULT_LAKE_PREFIX = "chains/v1"

# Env wiring. Deployment configuration, not strategy configuration — the
# backtest Job is configured by env, so this deliberately is not a settings.yaml
# key (see docs/plans/fc-060-chain-lake.md).
LAKE_BUCKET_ENV = "CHAIN_LAKE_BUCKET"
LAKE_PREFIX_ENV = "CHAIN_LAKE_PREFIX"


def _storage_client():
    """Build a GCS client.

    Indirected through a module-level function for two reasons: the import is
    deferred so a process with no lake configured never imports the GCS stack,
    and tests can monkeypatch this name to assert that no client is constructed
    on paths that must not touch the network.
    """
    from google.cloud import storage  # imported lazily; see docstring

    return storage.Client()


class ChainLake:
    """GCS mirror of the local chain cache (FC-060 Layer 1).

    Object layout mirrors the local one exactly:
    ``gs://<bucket>/<prefix>/<UNDERLYING>/<YYYY-MM-DD>.parquet``. One local file
    is one object; nothing is repacked, so a lake round-trip cannot change a
    chain — it only moves the file the local store would have read anyway, and
    the store's coverage check then applies to it unchanged.

    The client is constructed lazily on first use, so building a ``ChainLake``
    is free and credential-less: unit tests and local runs that never reach a
    lake operation never touch ``google.cloud.storage``.

    Every method here is allowed to raise. Failure policy lives in
    ``ChainStore``, which counts and logs the failure and continues local-only —
    a single place, so no call site can accidentally make a lake outage fatal.
    """

    def __init__(self, bucket: str, prefix: str = DEFAULT_LAKE_PREFIX) -> None:
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")
        self._client: Optional[Any] = None  # google.cloud.storage.Client

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ChainLake(bucket={self.bucket_name!r}, prefix={self.prefix!r})"

    def object_name(self, underlying: str, as_of: date) -> str:
        return f"{self.prefix}/{underlying.upper()}/{as_of.isoformat()}.parquet"

    def _blob(self, underlying: str, as_of: date):
        if self._client is None:
            self._client = _storage_client()
        return self._client.bucket(self.bucket_name).blob(
            self.object_name(underlying, as_of)
        )

    def exists(self, underlying: str, as_of: date) -> bool:
        return bool(self._blob(underlying, as_of).exists())

    def download(self, underlying: str, as_of: date, local_path) -> bool:
        """Fetch the object into ``local_path``. Returns False if absent.

        Written to a temp file in the destination directory and then
        ``os.replace``d, for the same reason ``ChainStore.put`` does: a partial
        download must never be visible as a cache file, because the reader
        cannot repair one and every later run would die on the same day.
        """
        blob = self._blob(underlying, as_of)
        if not blob.exists():
            return False
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_name(f"{local_path.name}.{os.getpid()}.lake.tmp")
        try:
            blob.download_to_filename(str(tmp))
            os.replace(tmp, local_path)  # atomic within a filesystem
        finally:
            if tmp.exists():
                tmp.unlink()
        return True

    def upload(self, local_path, underlying: str, as_of: date) -> None:
        """Mirror a completed local file to the lake (overwrites in place).

        Overwrite is the same semantics ``put`` already has locally: a rebuild
        with a wider window replaces the narrower file.
        """
        self._blob(underlying, as_of).upload_from_filename(str(local_path))

    def delete(self, underlying: str, as_of: date) -> bool:
        """Remove the object; returns False if there was nothing to remove.

        Only ever called from the corrupt-file path. Existence is checked first
        rather than catching ``NotFound`` so this module needs no
        ``google.api_core`` import, and so a missing object is a plain False
        instead of an exception the caller would count as a lake error.
        """
        blob = self._blob(underlying, as_of)
        if not blob.exists():
            return False
        blob.delete()
        return True


class ChainStore:
    """Local parquet cache of ChainSnapshots, optionally mirrored to a lake."""

    def __init__(
        self,
        cache_dir: str = DEFAULT_CACHE_DIR,
        lake: Optional[ChainLake] = None,
    ) -> None:
        self._root = Path(cache_dir)
        self.lake = lake
        # Counters, not metrics plumbing: the first monthly warm run has to be
        # measurable ("hits >> puts and the runtime dropped") from the Job's own
        # logs, and there is no metrics sink in this process.
        self.lake_hits = 0
        self.lake_misses = 0
        self.lake_puts = 0
        self.lake_errors = 0

    @classmethod
    def from_env(cls, cache_dir: str = DEFAULT_CACHE_DIR) -> "ChainStore":
        """Build a store, wiring the lake from the environment if configured.

        ``CHAIN_LAKE_BUCKET`` unset or empty => ``lake is None`` and behaviour is
        identical to a plain ``ChainStore(cache_dir)``: no GCS client is ever
        constructed and no lake event is ever logged.
        """
        bucket = os.environ.get(LAKE_BUCKET_ENV, "").strip()
        if not bucket:
            return cls(cache_dir)
        prefix = os.environ.get(LAKE_PREFIX_ENV, "").strip() or DEFAULT_LAKE_PREFIX
        return cls(cache_dir, lake=ChainLake(bucket, prefix))

    def summary(self) -> dict:
        """Lake usage for this store's lifetime; safe to call with no lake."""
        return {
            "lake_enabled": self.lake is not None,
            "lake_bucket": getattr(self.lake, "bucket_name", None),
            "lake_prefix": getattr(self.lake, "prefix", None),
            "lake_hits": self.lake_hits,
            "lake_misses": self.lake_misses,
            "lake_puts": self.lake_puts,
            "lake_errors": self.lake_errors,
        }

    def _lake_call(self, op: str, underlying: str, as_of: date, fn):
        """Run one lake operation, turning any failure into a counted no-op.

        Returns ``(ok, value)``. A lake outage must degrade a backtest to
        local-only, never fail it — a two-hour screen cannot be lost to a GCS
        hiccup — so this catches broadly on purpose. The counter is what keeps
        "degraded silently" from being indistinguishable from "worked".
        """
        try:
            return True, fn()
        except Exception as exc:  # noqa: BLE001 - see docstring
            self.lake_errors += 1
            logger.warning(
                "Chain lake operation failed; continuing local-only",
                event_category="backtest_data",
                event_type="chain_lake_error",
                op=op,
                symbol=underlying,
                as_of=as_of.isoformat(),
                bucket=getattr(self.lake, "bucket_name", None),
                error=str(exc),
                error_class=type(exc).__name__,
            )
            return False, None

    def _pull_from_lake(self, underlying: str, as_of: date, path: Path) -> bool:
        """Try to satisfy a local miss from the lake. True if the file landed."""
        if self.lake is None:
            return False
        ok, landed = self._lake_call(
            "download",
            underlying,
            as_of,
            lambda: self.lake.download(underlying, as_of, path),
        )
        if not ok:
            return False
        if landed and path.exists():
            self.lake_hits += 1
            return True
        self.lake_misses += 1
        return False

    def _path(self, underlying: str, as_of: date) -> Path:
        return self._root / underlying.upper() / f"{as_of.isoformat()}.parquet"

    def has(self, underlying: str, as_of: date) -> bool:
        """Whether the chain is present *locally*. Never consults the lake.

        A lake probe is a network round-trip, and every caller of ``has`` wants
        the cheap local question. ``get`` is the one that reaches for the lake.
        """
        return self._path(underlying, as_of).exists()

    def put(
        self,
        snapshot: ChainSnapshot,
        *,
        universe_dte: Optional[int] = None,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
        model: str = "",
    ) -> None:
        """Persist a snapshot (overwrites any existing file for that day).

        ``universe_dte``/``strike_gte``/``strike_lte``/``model`` record the
        request the snapshot answers, so ``get`` can later prove the file covers
        a new request instead of assuming it.

        The write is atomic: a temp file in the same directory, then
        ``os.replace``. Without that, an interrupted or concurrent write leaves
        a truncated parquet behind, and since the reader cannot repair one, every
        later run would die on the same day until someone found and deleted it
        by hand. Parallel study runs over overlapping symbols (FC-042 Track B
        runs B1 and B2 concurrently) make that a routine event, not an exotic one.

        When a lake is configured the completed file is then mirrored to it —
        after ``os.replace``, so a torn write is never uploaded.
        """
        path = self._path(snapshot.underlying, snapshot.as_of)
        path.parent.mkdir(parents=True, exist_ok=True)
        provenance = {
            "universe_dte": _UNKNOWN if universe_dte is None else float(universe_dte),
            "strike_gte": _UNKNOWN if strike_gte is None else float(strike_gte),
            "strike_lte": _UNKNOWN if strike_lte is None else float(strike_lte),
            "model": model,
        }
        rows = [{**self._quote_to_row(q), **provenance} for q in snapshot.all_quotes()]
        if not rows:
            # A real "no contracts traded" day: write one sentinel row (empty
            # symbol) carrying the underlying price so a later read distinguishes
            # an empty chain from a cache miss.
            rows = [{**self._empty_row(snapshot), **provenance}]
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            pd.DataFrame(rows, columns=_COLUMNS).to_parquet(tmp, index=False)
            os.replace(tmp, path)  # atomic within a filesystem
        finally:
            if tmp.exists():
                tmp.unlink()
        # Mirror to the lake AFTER os.replace, never from the temp file: what is
        # uploaded is exactly the bytes a local reader would read, so a torn
        # write can never become a permanently poisoned object.
        if self.lake is not None:
            ok, _ = self._lake_call(
                "upload",
                snapshot.underlying,
                snapshot.as_of,
                lambda: self.lake.upload(path, snapshot.underlying, snapshot.as_of),
            )
            if ok:
                self.lake_puts += 1

    def get(
        self,
        underlying: str,
        as_of: date,
        *,
        universe_dte: Optional[int] = None,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
        underlying_price: Optional[float] = None,
        model: Optional[str] = None,
    ) -> Optional[ChainSnapshot]:
        """Load a cached snapshot, or None on a cache miss.

        A file is a hit only if it *covers* the request: built with at least as
        long a DTE reach, at least as wide a strike window, and under the same
        pricing model. A covering file is then narrowed to exactly the requested
        window, so that reading from cache and building from the provider return
        the identical chain — the property that makes the cache invisible to
        results rather than merely fast.

        Passing no bounds asks for the file as written, with no coverage claim.

        An unreadable file is treated as a miss and deleted rather than raised:
        it is a corrupt cache entry, and the caller can always rebuild from the
        provider. Propagating the error instead would wedge every future run.
        When a lake is configured the mirror is deleted too, so the corrupt
        bytes are not simply re-downloaded on the next run.

        **With a lake configured**, a local miss first tries to download the
        day's object. The coverage check, the narrowing and the corrupt-file
        handling below then apply to the downloaded file exactly as they do to
        a local one — which is what makes a lake-warmed run and a cold run
        return the identical chain.
        """
        path = self._path(underlying, as_of)
        # The lake is consulted BEFORE the read path, never instead of it: a
        # downloaded file is then interrogated exactly like a local one.
        # ``_pull_from_lake`` returns True only once the file is on disk.
        if not path.exists() and not self._pull_from_lake(underlying, as_of, path):
            return None
        try:
            df = pd.read_parquet(path)
        except Exception:
            logger.warning(
                "Discarding an unreadable chain-cache file",
                event_category="backtest_data",
                event_type="chain_cache_corrupt",
                symbol=underlying,
                as_of=as_of.isoformat(),
                path=str(path),
            )
            path.unlink(missing_ok=True)
            # Delete the mirror too. Otherwise a bad object is re-downloaded on
            # every run forever: the local copy is discarded, the next run pulls
            # the same corrupt bytes back, and the cache never self-heals.
            if self.lake is not None:
                ok, removed = self._lake_call(
                    "delete",
                    underlying,
                    as_of,
                    lambda: self.lake.delete(underlying, as_of),
                )
                if ok:
                    logger.warning(
                        "Deleted the lake mirror of an unreadable chain file",
                        event_category="backtest_data",
                        event_type="chain_lake_corrupt_delete",
                        symbol=underlying,
                        as_of=as_of.isoformat(),
                        bucket=self.lake.bucket_name,
                        object_existed=bool(removed),
                    )
            return None
        if df.empty:
            return None
        if not self._covers(df, universe_dte, strike_gte, strike_lte, model):
            return None

        cached_price = float(df["underlying_price"].iloc[0])
        if underlying_price is not None and not _close(cached_price, underlying_price):
            # The file was built against a different close for the same session
            # (a restated bar, or a raw/adjusted mix). Every delta in it was
            # computed against that price, so it cannot answer this request.
            return None

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
            q = self._row_to_quote(r)
            (puts if q.option_type == "put" else calls).append(q)
        puts.sort(key=lambda x: x.strike)
        calls.sort(key=lambda x: x.strike)
        return ChainSnapshot(underlying, as_of, cached_price, puts, calls)

    @staticmethod
    def _covers(
        df,
        universe_dte: Optional[int],
        strike_gte: Optional[float],
        strike_lte: Optional[float],
        model: Optional[str] = None,
    ) -> bool:
        """Whether the file's build window is a superset of the request."""
        if model is not None:
            # Not a width comparison: a different model is a different answer,
            # never a wider one. Absent column => pre-fingerprint file => miss.
            if "model" not in df.columns or str(df["model"].iloc[0]) != model:
                return False
        if universe_dte is not None:
            stored = df["universe_dte"].iloc[0] if "universe_dte" in df.columns else _UNKNOWN
            if pd.isna(stored) or int(stored) < universe_dte:
                return False
        if strike_gte is not None or strike_lte is not None:
            if "strike_gte" not in df.columns or "strike_lte" not in df.columns:
                return False
            lo, hi = df["strike_gte"].iloc[0], df["strike_lte"].iloc[0]
            if pd.isna(lo) or pd.isna(hi):
                return False
            # Tolerance: the bounds are recomputed from a float close each run,
            # so an exact-equality test would miss on the last bit and refetch
            # every single day for no reason.
            if strike_gte is not None and float(lo) > strike_gte + _BOUND_TOL:
                return False
            if strike_lte is not None and float(hi) < strike_lte - _BOUND_TOL:
                return False
        return True

    # ------------------------------------------------------------------ #
    @staticmethod
    def _quote_to_row(q: ChainQuote) -> dict:
        return {
            "symbol": q.symbol,
            "underlying": q.underlying,
            "as_of": q.as_of.isoformat(),
            "expiration": q.expiration.isoformat(),
            "strike": q.strike,
            "option_type": q.option_type,
            "dte": q.dte,
            "underlying_price": q.underlying_price,
            "mark": q.mark,
            "bid": q.bid,
            "ask": q.ask,
            "implied_volatility": q.implied_volatility,
            "delta": q.delta,
            "volume": q.volume,
            "modeled_spread": q.modeled_spread,
            "modeled_greeks": q.modeled_greeks,
        }

    @staticmethod
    def _empty_row(snap: ChainSnapshot) -> dict:
        # Sentinel row: empty symbol marks "no contracts", carries metadata.
        return {
            "symbol": "",
            "underlying": snap.underlying,
            "as_of": snap.as_of.isoformat(),
            "expiration": snap.as_of.isoformat(),
            "strike": 0.0,
            "option_type": "",
            "dte": 0,
            "underlying_price": snap.underlying_price,
            "mark": 0.0,
            "bid": 0.0,
            "ask": 0.0,
            "implied_volatility": None,
            "delta": None,
            "volume": 0,
            "modeled_spread": True,
            "modeled_greeks": True,
        }

    @staticmethod
    def _row_to_quote(r) -> ChainQuote:
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
