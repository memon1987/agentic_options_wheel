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

**A lake object is coverage-monotone, not immutable.** A *chain* for a settled
session is immutable, but the *file* is not: it records the window it was built
under, and the window is path-dependent (``cost_basis`` and ``low_anchor`` move
it, so a machine holding different positions builds the same session under a
different window, and so does this machine on a later run once the price range
has moved). So an
object may only ever be replaced by a file whose request is a **superset** —
same model, at least the DTE reach, at least the strike window. A narrowing is
refused and logged (``chain_lake_overwrite_skipped``) because the wider file is
shared: losing it turns hits into misses for every machine, and the coverage it
proved is not recoverable without re-fetching from the vendor. Uploads carry an
``if_generation_match`` precondition so two writers cannot resolve that race by
luck.

**Coverage-monotone via merge (FC-091).** Refusing the narrowing is correct but
not sufficient: a window can move so that a rebuild is wider on one bound and
narrower on the other, in which case *neither* file covers the other and the
lake never heals — the first production run of the lake left SPY, IWM and PFE
in exactly that state (``rejected=231 skipped=231`` each, cold on every monthly
run, forever). So when a downloaded object is rejected by ``_covers`` this run,
``get`` remembers the frame and the rebuild's ``put`` merges the two: the union
of contracts keyed by OCC symbol (the new build wins on duplicates), under the
union of the two **strike** windows at their shared DTE reach. The merged file
is a superset of the object it replaces, so the monotone rule accepts it and
the day is warm from then on.

The rule that keeps that honest is that **only one axis may widen**. A build
window is a rectangle in (DTE reach x strike range) and the union of two
rectangles is an L, but the provenance columns can only describe a rectangle —
so a merge across two different DTE reaches would claim the bounding box,
including a corner neither file ever fetched (``dte_mismatch``). For the same
reason a merge refuses two strike windows that do not overlap at all
(``chain_lake_merge_gap``, which uploads nothing), and two fetches that
disagree about the strikes they both cover (``overlap_conflict`` — one of them
is incomplete, and a union would absorb that rather than surface it). Never
claim coverage the file does not contain.

The heal reaches only the **downloaded** path: a frame is remembered when it
came off the lake, so the ephemeral Job heals on every chain-day while a
developer machine with a warm local cache keeps its own files as before.

**Nothing here ever deletes.** See ``ChainLake``'s docstring: an unreadable
local file is a reader-side event as often as it is corruption, and the lake
exists precisely because the vendor may not serve these chains again.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional

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

# The provenance columns are constant within a file and are rewritten wholesale
# when two files are merged; everything else belongs to one quote and travels
# with it. Split out so the merge can copy quote rows between files without
# carrying the source file's window claim along with them.
_PROVENANCE_COLUMNS = ("universe_dte", "strike_gte", "strike_lte", "model")
_QUOTE_COLUMNS = [c for c in _COLUMNS if c not in _PROVENANCE_COLUMNS]

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
# plus the provenance columns) *is* the format contract for these objects, and
# the ``model`` provenance column is part of it: bump this prefix whenever the
# schema OR the pricing model changes, so two generations of file can never be
# read as each other. An object is never overwritten by a file carrying a
# different model fingerprint (see ``_window_regression``), so without a prefix
# bump a model change simply stops populating the lake.
DEFAULT_LAKE_PREFIX = "chains/v1"

# Env wiring. Deployment configuration, not strategy configuration — the
# backtest Job is configured by env, so this deliberately is not a settings.yaml
# key (see docs/plans/fc-060-chain-lake.md).
LAKE_BUCKET_ENV = "CHAIN_LAKE_BUCKET"
LAKE_PREFIX_ENV = "CHAIN_LAKE_PREFIX"

# Per-RPC ceiling and overall retry deadline. Without these, google-cloud-storage
# retries a stalled request until the *task* timeout: a degraded GCS turns a
# 1h47m screen into a killed execution, which is precisely the failure the
# degrade-to-local-only policy exists to prevent. A timeout is what makes that
# policy reachable.
LAKE_TIMEOUT_S = 30.0
LAKE_RETRY_DEADLINE_S = 60.0

# Consecutive failed lake operations before the lake is switched off for the
# rest of the process. A backtest with a dead lake should pay the outage once,
# not once per symbol-day: 5,400 timeouts x 30s is a run that never finishes.
MAX_CONSECUTIVE_LAKE_ERRORS = 5

_RETRY = None
_NOT_FOUND = None
_PRECONDITION_FAILED = None


def _storage_client():
    """Build a GCS client.

    Indirected through a module-level function for two reasons: the import is
    deferred so a process with no lake configured never imports the GCS stack,
    and tests can monkeypatch this name to assert that no client is constructed
    on paths that must not touch the network.
    """
    from google.cloud import storage  # imported lazily; see docstring

    return storage.Client()


def _retry():
    """Bounded retry policy, resolved lazily (see ``_storage_client``)."""
    global _RETRY
    if _RETRY is None:
        from google.cloud.storage.retry import DEFAULT_RETRY

        _RETRY = (
            DEFAULT_RETRY.with_timeout(LAKE_RETRY_DEADLINE_S)
            if hasattr(DEFAULT_RETRY, "with_timeout")
            else DEFAULT_RETRY.with_deadline(LAKE_RETRY_DEADLINE_S)
        )
    return _RETRY


def _not_found():
    global _NOT_FOUND
    if _NOT_FOUND is None:
        from google.api_core.exceptions import NotFound

        _NOT_FOUND = NotFound
    return _NOT_FOUND


def _precondition_failed():
    global _PRECONDITION_FAILED
    if _PRECONDITION_FAILED is None:
        from google.api_core.exceptions import PreconditionFailed

        _PRECONDITION_FAILED = PreconditionFailed
    return _PRECONDITION_FAILED


class ChainLakeUnavailable(Exception):
    """The lake cannot be used at all — stop trying for the rest of the run.

    Distinct from an operation failing: bad credentials or a missing bucket do
    not get better on the next symbol-day, so retrying them 5,400 times is pure
    latency. Carries the reason that goes into ``chain_lake_disabled``.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ChainLakePreconditionFailed(Exception):
    """Another writer changed the object since we read its generation.

    Not an error: it means the write-once precondition did its job. The upload
    is skipped rather than retried — the object under us was written by another
    process running this same code, so it is already coverage-checked; blindly
    re-uploading over it would be exactly the clobber the precondition exists
    to prevent.
    """


@dataclass
class LakeObject:
    """What one metadata read tells us about an object in the lake."""

    generation: Optional[int] = None
    md5_hash: Optional[str] = None


class ChainLake:
    """GCS mirror of the local chain cache (FC-060 Layer 1).

    Object layout mirrors the local one exactly:
    ``gs://<bucket>/<prefix>/<UNDERLYING>/<YYYY-MM-DD>.parquet``. One local file
    is one object; nothing is repacked, so a lake round-trip cannot change a
    chain — it only moves the file the local store would have read anyway, and
    the store's coverage check then applies to it unchanged.

    The client is constructed lazily on first use, so building a ``ChainLake``
    is free and credential-less: unit tests and local runs that never reach a
    lake operation never touch ``google.cloud.storage``. First use also probes
    the bucket once, because a typo'd or unreachable bucket otherwise reads as
    an *empty lake* — every read a clean miss, every run silently paying the
    full cold cost while the logs say nothing is wrong.

    This class holds the availability state (disabled / reason) because that
    state belongs to the bucket, not to any one ``ChainStore``: a screen builds
    one store per symbol over a single shared lake, and a dead bucket must be
    discovered once for the run rather than fourteen times.

    Operations are allowed to raise, and *how a failure is absorbed* is
    ``ChainStore``'s job — it counts, logs and continues local-only, in one
    place, so no call site can accidentally make a lake outage fatal. What this
    class does own is *availability*: once disabled, every operation refuses
    immediately with ``ChainLakeUnavailable`` rather than issuing an RPC. That
    matters for direct callers such as the seed tool, which have no
    ``ChainStore`` in front of them to check the flag.

    There is deliberately **no delete**. An unreadable local file is a
    reader-side event (``pyarrow`` version skew, ``MemoryError``, a hit
    file-descriptor limit) at least as often as it is genuine corruption, and
    deleting the remote object on that signal would destroy history that may not
    be re-fetchable. Overwrite-on-rebuild, guarded by the coverage rule, is the
    only mutation path this class offers; ``chain_lake_seed.py --force`` is the
    deliberate, human-driven escape hatch for a genuinely bad object.
    """

    def __init__(self, bucket: str, prefix: str = DEFAULT_LAKE_PREFIX) -> None:
        bucket = (bucket or "").strip()
        if not bucket:
            raise ValueError("ChainLake requires a bucket name")
        prefix = (prefix or "").strip().strip("/")
        if not prefix:
            # An empty prefix would scatter bare <SYMBOL>/<date>.parquet objects
            # across the bucket root with no format version left to bump.
            raise ValueError("ChainLake requires a non-empty prefix")
        self.bucket_name = bucket
        self.prefix = prefix
        self._client: Optional[Any] = None  # google.cloud.storage.Client
        self._client_failed = False
        self._probed = False
        self.disabled = False
        self.disabled_reason: Optional[str] = None
        self._consecutive_errors = 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ChainLake(bucket={self.bucket_name!r}, prefix={self.prefix!r})"

    # ------------------------------ availability ------------------------- #
    def disable(self, reason: str) -> None:
        """Switch the lake off for the rest of its life. Logs exactly once.

        Sticky and one-way: every subsequent ``stat``/``download``/``upload``
        raises ``ChainLakeUnavailable`` without an RPC (see ``_bucket``). There
        is no re-enable — a lake that failed its probe or burned through the
        error budget does not recover inside one process, and retrying it is
        the latency this exists to avoid.
        """
        if self.disabled:
            return
        self.disabled = True
        self.disabled_reason = reason
        logger.warning(
            "Chain lake disabled for the rest of this run; continuing local-only",
            event_category="backtest_data",
            event_type="chain_lake_disabled",
            reason=reason,
            bucket=self.bucket_name,
            prefix=self.prefix,
        )

    def note_success(self) -> None:
        self._consecutive_errors = 0

    def note_failure(self) -> None:
        """Count a failed operation; trip the breaker at the threshold."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= MAX_CONSECUTIVE_LAKE_ERRORS:
            self.disable("consecutive_errors")

    # -------------------------------- plumbing --------------------------- #
    def object_name(self, underlying: str, as_of: date) -> str:
        return f"{self.prefix}/{underlying.upper()}/{as_of.isoformat()}.parquet"

    def _bucket(self):
        """Client + one-time startup probe. Raises ChainLakeUnavailable if dead.

        Sticky in both directions: a lake that has been disabled, or whose
        client construction has already failed, refuses every operation from
        here on. That guard lives at this chokepoint rather than in each method
        so a direct caller (the seed tool) gets the same protection the store's
        ``_lake_active`` gate gives the engine.
        """
        self._ensure_usable()
        if self._client is None:
            try:
                self._client = _storage_client()
            except Exception as exc:
                # Never retried: no credential appears mid-run, and re-running
                # ADC discovery per symbol-day is pure latency.
                self._client_failed = True
                self.disable("credentials")
                raise ChainLakeUnavailable(
                    "credentials", f"could not build a GCS client: {exc}"
                )
        bucket = self._client.bucket(self.bucket_name)
        if not self._probed:
            self._probed = True
            self._probe(bucket)
        return bucket

    def _ensure_usable(self) -> None:
        """Refuse, without an RPC, once this lake is known to be unusable.

        Both latches are one-way. ``disabled`` is set by the probe, by a
        credentials failure or by the error budget; ``_client_failed`` is set
        only by the former and is checked separately so that it survives even
        if something clears ``disabled`` — they are independent guards on
        purpose, because the store's own ``_lake_active`` check is a third and
        a direct caller (the seed tool) has neither.
        """
        if self.disabled:
            raise ChainLakeUnavailable(
                self.disabled_reason or "disabled",
                f"chain lake is disabled ({self.disabled_reason})",
            )
        if self._client_failed:
            raise ChainLakeUnavailable(
                "credentials", "GCS client construction already failed"
            )

    def _probe(self, bucket) -> None:
        """One object-scoped RPC to prove the lake is actually reachable.

        **Deliberately not ``bucket.exists()``.** That is ``GET /b/<bucket>``
        and needs ``storage.buckets.get``, which ``roles/storage.objectAdmin``
        — the grant the Job's service account and ``claude-operator`` get —
        does NOT carry. The probe would 403 on the first call of every run and
        disable the lake permanently. Listing one object needs only
        ``storage.objects.list``, which objectAdmin does carry, so this asks
        the same question ("can I reach this bucket?") within the permissions
        the lake is meant to run under. Widening IAM to make a health check
        work would be the wrong fix.

        A missing bucket surfaces as ``NotFound`` from the list; anything else
        (403, DNS, TLS, timeout) is ``bucket_unreachable``. Zero objects is a
        healthy *empty* lake — the day-one state before the seed runs.
        """
        try:
            next(
                iter(
                    self._client.list_blobs(
                        bucket,
                        prefix=f"{self.prefix}/",
                        max_results=1,
                        timeout=LAKE_TIMEOUT_S,
                        retry=_retry(),
                    )
                ),
                None,
            )
        except _not_found():
            self._unavailable("bucket_missing", "bucket does not exist")
        except Exception as exc:
            self._unavailable("bucket_unreachable", f"{type(exc).__name__}: {exc}")

    def _unavailable(self, reason: str, detail: str):
        logger.error(
            "Chain lake bucket is not usable; disabling the lake",
            event_category="backtest_data",
            event_type="chain_lake_unavailable",
            reason=reason,
            bucket=self.bucket_name,
            prefix=self.prefix,
            detail=detail[:300],
        )
        self.disable(reason)
        raise ChainLakeUnavailable(reason, f"{self.bucket_name}: {detail}")

    # ------------------------------- operations -------------------------- #
    def stat(self, underlying: str, as_of: date) -> Optional[LakeObject]:
        """One metadata RPC. None when the object does not exist.

        ``get_blob`` rather than ``exists``: the generation it returns is what
        makes the upload precondition possible, and the md5 is what lets the
        seed tool tell "already there, identical" from "already there, and
        different from what I hold".
        """
        blob = self._bucket().get_blob(
            self.object_name(underlying, as_of),
            timeout=LAKE_TIMEOUT_S,
            retry=_retry(),
        )
        if blob is None:
            return None
        return LakeObject(generation=blob.generation, md5_hash=blob.md5_hash)

    def download(
        self, underlying: str, as_of: date, local_path
    ) -> Optional[LakeObject]:
        """Fetch the object into ``local_path``; None if it does not exist.

        Attempts the download directly and treats ``NotFound`` as the miss
        rather than probing with ``exists`` first: half the RPCs on the miss
        path, which is the common path on a cold run, and no exists→download
        race.

        Written to a temp file in the destination directory and then
        ``os.replace``d, for the same reason ``ChainStore.put`` does — a partial
        download must never be visible as a cache file, because the reader
        cannot repair one and every later run would die on the same day.
        """
        blob = self._bucket().blob(self.object_name(underlying, as_of))
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_name(f"{local_path.name}.{os.getpid()}.lake.tmp")
        try:
            try:
                blob.download_to_filename(
                    str(tmp), timeout=LAKE_TIMEOUT_S, retry=_retry()
                )
            except _not_found():
                return None
            os.replace(tmp, local_path)  # atomic within a filesystem
        finally:
            if tmp.exists():
                tmp.unlink()
        return LakeObject(generation=blob.generation, md5_hash=blob.md5_hash)

    def upload(
        self,
        local_path,
        underlying: str,
        as_of: date,
        *,
        if_generation_match: Optional[int],
    ) -> None:
        """Mirror a completed local file, guarded by a generation precondition.

        ``if_generation_match=0`` means "create only"; any other value means
        "the object must still be the one whose provenance I checked". Without
        it, two machines replaying the same window would race and the loser's
        wider file could be replaced by the winner's narrower one *after* the
        coverage check had already passed.
        """
        blob = self._bucket().blob(self.object_name(underlying, as_of))
        try:
            blob.upload_from_filename(
                str(local_path),
                if_generation_match=if_generation_match,
                timeout=LAKE_TIMEOUT_S,
                retry=_retry(),
                checksum="crc32c",
            )
        except _precondition_failed() as exc:
            raise ChainLakePreconditionFailed(str(exc))


class _Window(NamedTuple):
    """The request a chain file answers — its provenance columns, as a tuple."""

    universe_dte: float
    strike_gte: float
    strike_lte: float
    model: str

    def as_log(self) -> dict:
        return {
            "universe_dte": None if pd.isna(self.universe_dte) else int(self.universe_dte),
            "strike_gte": None if pd.isna(self.strike_gte) else round(float(self.strike_gte), 4),
            "strike_lte": None if pd.isna(self.strike_lte) else round(float(self.strike_lte), 4),
            "model": self.model,
        }


def _window_of(df) -> _Window:
    """Read a file's provenance. Missing columns read as unknown."""
    def col(name):
        return df[name].iloc[0] if name in df.columns else _UNKNOWN

    model = df["model"].iloc[0] if "model" in df.columns else ""
    return _Window(
        universe_dte=col("universe_dte"),
        strike_gte=col("strike_gte"),
        strike_lte=col("strike_lte"),
        model="" if pd.isna(model) else str(model),
    )


def _window_regression(new: _Window, old: _Window) -> Optional[str]:
    """Why ``new`` must not overwrite ``old``, or None if it may.

    The rule is *coverage-monotone*: an object may only be replaced by a file
    built from a request that is a superset — same model, at least the DTE
    reach, at least the strike window. Anything else is a narrowing, and a
    narrowing is silent data loss: the wider file is gone, and every future
    bounded request that the wide file used to satisfy becomes a cache miss
    (best case) for every machine sharing the lake.

    Unknown provenance on either side fails closed. A file that cannot prove
    what it covers cannot prove it covers what it would replace.
    """
    if new.model != old.model:
        # Never an overwrite: a different pricing model is a different answer,
        # not a wider one. A model change needs a CHAIN_LAKE_PREFIX bump.
        return "model_changed"
    if _unknown_pair(new.universe_dte, old.universe_dte) or _unknown_pair(
        new.strike_gte, old.strike_gte
    ) or _unknown_pair(new.strike_lte, old.strike_lte):
        return "unknown_provenance"
    if not pd.isna(old.universe_dte) and float(new.universe_dte) < float(old.universe_dte):
        return "narrower_dte"
    if not pd.isna(old.strike_gte) and float(new.strike_gte) > float(old.strike_gte) + _BOUND_TOL:
        return "narrower_strikes"
    if not pd.isna(old.strike_lte) and float(new.strike_lte) < float(old.strike_lte) - _BOUND_TOL:
        return "narrower_strikes"
    return None


def _unknown_pair(new: float, old: float) -> bool:
    """True when exactly one side is unknown — an unprovable comparison."""
    return bool(pd.isna(new)) != bool(pd.isna(old))


def _merge_windows(new: _Window, old: _Window):
    """The union of two build windows, or ``(None, reason)`` if they must not merge.

    Returns ``(window, None)`` on success and ``(None, reason)`` otherwise. On
    success the window is ``(the shared universe_dte, min(strike_gte),
    max(strike_lte), the shared model)``.

    **Only the strike axis widens, and that is the whole correctness argument.**
    A build window is a rectangle in (DTE reach x strike range), and the union
    of two rectangles is not a rectangle: it is an L. The provenance columns can
    only describe a rectangle, so a merged file whose sides both moved would
    claim the bounding box — including the corner *neither* file fetched. With
    ``universe_dte`` pinned equal, the two rectangles share their full height
    and their union along strikes IS a rectangle, exactly the one claimed.

    Four refusals, and they mean different things:

    * ``model_changed`` — the two files answer different questions. Prices and
      greeks in these rows are *computed*, not fetched, so unioning rows priced
      under two models yields a file coherent under neither.
    * ``unknown_provenance`` — a file that cannot say what it covers cannot
      contribute to a coverage claim. Fails closed, as ``_window_regression``
      does.
    * ``dte_mismatch`` — the corner case above. Taking ``max(universe_dte)``
      here would produce a file that PASSES ``_covers`` for the longer reach
      across the newer strikes while silently holding none of the longer-dated
      contracts there: a hit that is missing rows, which is a wrong backtest
      rather than a slow one. Checked before ``strike_gap``, so a pair that
      fails both is reported as this one.
    * ``strike_gap`` — the windows do not overlap, so their union spans strikes
      *neither* file fetched. A merged file claiming that range would look
      exactly like a session on which those strikes did not trade. This is the
      one refusal that also suppresses the upload entirely (see
      ``ChainStore._merge_rejected_lake_frame``).

    The equality rule is a real restriction, not a formality — but it does not
    cost the case this exists for: the observed SPY/IWM/PFE thrash is a pure
    strike-window move at a constant DTE reach (8 on both sides), because
    ``universe_dte`` is ``max_dte + UNIVERSE_DTE_BUFFER`` and neither term is
    path-dependent, while the strike anchors follow the session's price.
    """
    if new.model != old.model:
        return None, "model_changed"
    bounds = (
        new.universe_dte, new.strike_gte, new.strike_lte,
        old.universe_dte, old.strike_gte, old.strike_lte,
    )
    if any(pd.isna(v) for v in bounds):
        return None, "unknown_provenance"
    if int(new.universe_dte) != int(old.universe_dte):
        return None, "dte_mismatch"
    if (
        float(new.strike_gte) > float(old.strike_lte) + _BOUND_TOL
        or float(old.strike_gte) > float(new.strike_lte) + _BOUND_TOL
    ):
        return None, "strike_gap"
    return _Window(
        float(new.universe_dte),  # equal to old's; the height does not move
        min(float(new.strike_gte), float(old.strike_gte)),
        max(float(new.strike_lte), float(old.strike_lte)),
        new.model,
    ), None


def _overlap_conflict(new_rows, old_df, new: _Window, old: _Window):
    """``(old_count, new_count)`` when the two fetches disagree, else ``None``.

    Where the two windows overlap, both files fetched the same question of the
    same settled session, so they must have found the same contracts. If they
    did not, one of them is wrong — a truncated or rate-limited fetch, a vendor
    restatement, a chain that was still forming when it was cached — and the
    union would *absorb* that instead of surfacing it, writing a file that is
    self-consistent, passes every coverage check, and is missing rows nobody
    will ever be told about. Refuse and let the monotone path deal with it.

    Compared by contract symbol rather than by count: two fetches can agree on
    how many contracts exist in the overlap and still disagree on which. DTE
    plays no part here because ``_merge_windows`` has already pinned the two
    reaches equal, so strike containment alone defines the overlap.
    """
    def inside(strike, window) -> bool:
        try:
            k = float(strike)
        except (TypeError, ValueError):
            return False
        if pd.isna(k):
            return False
        return (float(window.strike_gte) - _BOUND_TOL <= k
                <= float(window.strike_lte) + _BOUND_TOL)

    old_in_new = {
        r["symbol"]
        for r in old_df[["symbol", "strike"]].to_dict("records")
        if r.get("symbol") and inside(r["strike"], new)
    }
    new_in_old = {
        r["symbol"] for r in new_rows
        if r.get("symbol") and inside(r["strike"], old)
    }
    if old_in_new == new_in_old:
        return None
    return len(old_in_new), len(new_in_old)


def _union_rows(new_rows, old_df):
    """Union a fresh build's rows with an older file's, keyed by contract symbol.

    ``None`` when the old frame is not schema-compatible — fail closed rather
    than write a file with holes in it.

    The new build wins every collision: it was produced by this process, under
    this code, from the same immutable session, so where the two disagree the
    older file is the one to distrust.

    **Sentinel rows** (empty ``symbol``, written for a genuine "no contracts
    traded" day) are dropped from the union whenever the other side has real
    rows — a sentinel is an assertion about a window, not a contract, and the
    merged file's window claim replaces it. They survive only when both sides
    are sentinels, i.e. the merged day really is empty.

    **Row order is new-first**, which is load-bearing rather than incidental:
    every old row that survives the union lies outside the new build's window
    (anything inside it is either a duplicate the new build wins or a contract
    the new build would itself have fetched), so narrowing the merged file back
    to any request the *unmerged* new file could satisfy drops all of them and
    returns the identical rows in the identical order — provided the two
    fetches agree on the overlap, which ``_overlap_conflict`` verifies before
    this runs. That caveat is the whole reason the check exists: without it,
    "the old file had a contract in the overlap that the new build did not"
    would silently become an extra row in an answer the new file already had.
    """
    missing = [c for c in _QUOTE_COLUMNS if c not in old_df.columns]
    if missing:
        return None
    old_rows = old_df[_QUOTE_COLUMNS].to_dict("records")
    old_real = [r for r in old_rows if r.get("symbol")]
    new_real = [r for r in new_rows if r.get("symbol")]
    if not new_real:
        # This build found nothing: keep the older file's contracts if it has
        # any, else keep the new sentinel (both sides genuinely empty).
        return old_real if old_real else list(new_rows)
    new_symbols = {r["symbol"] for r in new_real}
    return new_real + [r for r in old_real if r["symbol"] not in new_symbols]


@dataclass
class _RemoteState:
    """What this store last learned about one object in the lake.

    ``probed`` records that a full metadata+download probe has already been
    paid for this key. Without it, an object whose provenance cannot be read
    (an unreadable remote parquet — the one case nothing in this module will
    overwrite) would be re-stat'd and re-downloaded on every single put for the
    rest of the run, to reach the same refusal every time.
    """

    present: bool
    generation: Optional[int] = None
    window: Optional[_Window] = None
    probed: bool = False


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
        self.lake_hits = 0        # object downloaded and used
        self.lake_misses = 0      # object absent
        self.lake_rejected = 0    # downloaded, then failed the coverage check
        self.lake_puts = 0        # object written
        self.lake_skipped = 0     # upload declined (all reasons; total)
        # Subset of lake_skipped: the remote object's provenance could not be
        # read, so coverage could not be compared. Broken out because the other
        # skips are the guard working as designed, while this one is a poisoned
        # object that only `chain_lake_seed.py --force` can clear.
        self.lake_skipped_unreadable_remote = 0
        self.lake_errors = 0      # operation failed; run continued local-only
        # FC-091. A rebuild whose window neither covers nor is covered by the
        # object it replaces is unioned with it instead of being skipped.
        self.lake_merged = 0      # rebuild unioned with the rejected object
        self.lake_merge_gaps = 0  # windows disjoint: merge refused, nothing sent
        # Merges refused by a correctness check the windows alone do not catch
        # — a differing DTE reach, or two fetches that disagree about the
        # strikes they both cover. Counted apart from ``lake_merge_gaps``
        # because a gap uploads NOTHING while these fall through to the
        # ordinary coverage-monotone path; each carries its own WARNING.
        self.lake_merge_refused = 0
        # Remembers what the lake held for a key, so ``put`` can check coverage
        # against the object it is about to replace without re-downloading it.
        self._lake_seen: Dict[tuple, _RemoteState] = {}
        # FC-091. Frames this run downloaded from the lake and then rejected in
        # ``get`` for not covering the request, kept so the rebuild that follows
        # can merge rather than replace. Popped by the matching ``put``; the
        # only entry that outlives one is a rejected day nothing rebuilds, so
        # the dict is bounded by this run's own symbol-days.
        self._lake_rejected_frames: Dict[tuple, Any] = {}

    @classmethod
    def lake_from_env(cls) -> Optional[ChainLake]:
        """The lake the environment asks for, or None.

        Separate from ``from_env`` so a screen can build ONE lake and share it
        across its per-symbol stores: the client, the bucket probe and the
        circuit breaker are then run-scoped rather than symbol-scoped.
        """
        bucket = os.environ.get(LAKE_BUCKET_ENV, "").strip()
        if not bucket:
            return None
        prefix = os.environ.get(LAKE_PREFIX_ENV, "").strip() or DEFAULT_LAKE_PREFIX
        return ChainLake(bucket, prefix)

    @classmethod
    def from_env(cls, cache_dir: str = DEFAULT_CACHE_DIR) -> "ChainStore":
        """Build a store, wiring the lake from the environment if configured.

        ``CHAIN_LAKE_BUCKET`` unset or empty => ``lake is None`` and behaviour is
        identical to a plain ``ChainStore(cache_dir)``: no GCS client is ever
        constructed and no lake event is ever logged.
        """
        return cls(cache_dir, lake=cls.lake_from_env())

    def summary(self) -> dict:
        """Lake usage for this store's lifetime; safe to call with no lake."""
        return {
            "lake_enabled": self.lake is not None,
            "lake_bucket": getattr(self.lake, "bucket_name", None),
            "lake_prefix": getattr(self.lake, "prefix", None),
            "lake_hits": self.lake_hits,
            "lake_misses": self.lake_misses,
            "lake_rejected": self.lake_rejected,
            "lake_puts": self.lake_puts,
            "lake_skipped": self.lake_skipped,
            "lake_skipped_unreadable_remote": self.lake_skipped_unreadable_remote,
            "lake_merged": self.lake_merged,
            "lake_merge_gaps": self.lake_merge_gaps,
            "lake_merge_refused": self.lake_merge_refused,
            "lake_errors": self.lake_errors,
            "lake_disabled": bool(getattr(self.lake, "disabled", False)),
            "lake_disabled_reason": getattr(self.lake, "disabled_reason", None),
        }

    # --------------------------- lake plumbing --------------------------- #
    def _lake_active(self) -> bool:
        """Whether a lake operation is worth attempting at all."""
        return self.lake is not None and not self.lake.disabled

    def _lake_call(self, op: str, underlying: str, as_of: date, fn):
        """Run one lake operation, turning any failure into a counted no-op.

        Returns ``(ok, value)``. A lake outage must degrade a backtest to
        local-only, never fail it — a two-hour screen cannot be lost to a GCS
        hiccup — so this catches broadly on purpose. The counters are what keep
        "degraded silently" from being indistinguishable from "worked".

        Three outcomes are separated because they mean different things to an
        operator: the lake is *unusable* (stop trying), the write was *declined*
        (the lake is healthy and protected itself), or the operation *failed*
        (count it, and trip the breaker if they keep failing).
        """
        try:
            value = fn()
        except ChainLakeUnavailable:
            # ChainLake has already logged chain_lake_unavailable /
            # chain_lake_disabled and will refuse further work.
            self.lake_errors += 1
            return False, None
        except ChainLakePreconditionFailed as exc:
            self.lake_skipped += 1
            self.lake.note_success()  # the RPC reached GCS; the lake is healthy
            logger.info(
                "Chain lake upload skipped: another writer got there first",
                event_category="backtest_data",
                event_type="chain_lake_overwrite_skipped",
                reason="generation_mismatch",
                symbol=underlying,
                as_of=as_of.isoformat(),
                bucket=self.lake.bucket_name,
                detail=str(exc)[:200],
            )
            return False, None
        except Exception as exc:  # noqa: BLE001 - see docstring
            self.lake_errors += 1
            self.lake.note_failure()
            logger.warning(
                "Chain lake operation failed; continuing local-only",
                event_category="backtest_data",
                event_type="chain_lake_error",
                op=op,
                symbol=underlying,
                as_of=as_of.isoformat(),
                bucket=self.lake.bucket_name,
                error=str(exc),
                error_class=type(exc).__name__,
            )
            return False, None
        self.lake.note_success()
        return True, value

    def _pull_from_lake(self, underlying: str, as_of: date, path: Path) -> bool:
        """Try to satisfy a local miss from the lake. True if the file landed."""
        if not self._lake_active():
            return False
        ok, obj = self._lake_call(
            "download",
            underlying,
            as_of,
            lambda: self.lake.download(underlying, as_of, path),
        )
        if not ok:
            return False
        key = self._key(underlying, as_of)
        if obj is not None and path.exists():
            # Window is filled in by ``get`` once the file has been read.
            self._lake_seen[key] = _RemoteState(True, generation=obj.generation)
            self.lake_hits += 1
            return True
        self._lake_seen[key] = _RemoteState(False)
        self.lake_misses += 1
        return False

    @staticmethod
    def _key(underlying: str, as_of: date) -> tuple:
        return (underlying.upper(), as_of.isoformat())

    def _remember_remote_window(self, underlying: str, as_of: date, df) -> None:
        state = self._lake_seen.get(self._key(underlying, as_of))
        if state is not None and state.present:
            state.window = _window_of(df)

    def _mirror_to_lake(
        self, path: Path, underlying: str, as_of: date, new_window: "_Window"
    ) -> None:
        """Upload a freshly written local file, unless doing so would narrow.

        The strike window is path-dependent — it moves with ``cost_basis`` and
        ``low_anchor``, so the same session legitimately gets one window on a
        machine holding a position and another on a machine that does not, and
        another again once the price range has moved. (It does NOT move between
        the mid and bid passes: FC-060 Layer 2 materialises the data once and
        replays it, so both passes read the same chains.) A blind overwrite
        would therefore let a narrower rebuild delete a wider object that other
        runs depend on.
        """
        if not self._lake_active():
            return
        key = self._key(underlying, as_of)
        seen = self._lake_seen.get(key)
        remote: Optional[_RemoteState]
        if seen is not None and not seen.present:
            remote = None  # we looked this run and it was not there
        elif (
            seen is not None
            and seen.generation is not None
            and (seen.window is not None or seen.probed)
        ):
            # Already known this run: either downloaded (provenance read) or
            # fully probed and found unreadable. Either way, do not pay for it
            # again — a poisoned object would otherwise cost a stat plus a
            # download on every put for the rest of the run.
            remote = seen
        else:
            ok, remote = self._probe_remote(underlying, as_of)
            if not ok:
                return

        generation = 0
        if remote is not None:
            if remote.window is None:
                # Object exists but we could not establish what it covers.
                # Fail closed: never overwrite what we cannot compare against.
                self.lake_skipped_unreadable_remote += 1
                self._skip_overwrite(underlying, as_of, new_window, None,
                                     "remote_provenance_unknown")
                return
            reason = _window_regression(new_window, remote.window)
            if reason is not None:
                self._skip_overwrite(underlying, as_of, new_window,
                                     remote.window, reason)
                return
            generation = remote.generation  # None => no precondition available

        ok, _ = self._lake_call(
            "upload",
            underlying,
            as_of,
            lambda: self.lake.upload(
                path, underlying, as_of, if_generation_match=generation
            ),
        )
        if ok:
            self.lake_puts += 1
            self._lake_seen[key] = _RemoteState(True, generation=None,
                                                window=new_window)

    def _probe_remote(self, underlying: str, as_of: date):
        """Learn what the lake currently holds for a key: ``(ok, state|None)``.

        One metadata RPC; if an object is there, a download to a temp path to
        read its provenance, because the provenance lives inside the parquet
        and there is no cheaper place to read it from. This is the price of
        never clobbering a wider file, and it is paid only when a rebuild
        happens over an object this run has not already downloaded.
        """
        ok, obj = self._lake_call(
            "stat", underlying, as_of, lambda: self.lake.stat(underlying, as_of)
        )
        if not ok:
            return False, None
        if obj is None:
            self._lake_seen[self._key(underlying, as_of)] = _RemoteState(False)
            return True, None

        tmp = self._root / underlying.upper() / (
            f"{as_of.isoformat()}.{os.getpid()}.probe.tmp"
        )
        try:
            ok, got = self._lake_call(
                "download",
                underlying,
                as_of,
                lambda: self.lake.download(underlying, as_of, tmp),
            )
            if not ok:
                return False, None
            window = None
            if got is not None and tmp.exists():
                try:
                    window = _window_of(pd.read_parquet(tmp))
                except Exception:
                    # Unreadable remote object. Not deleted — see ChainLake's
                    # docstring; the seed tool's --force is the escape hatch.
                    window = None
            state = _RemoteState(
                got is not None, generation=obj.generation, window=window,
                probed=True,
            )
        finally:
            if tmp.exists():
                tmp.unlink()
        self._lake_seen[self._key(underlying, as_of)] = state
        return True, (state if state.present else None)

    def _skip_overwrite(self, underlying: str, as_of: date, new: _Window,
                        old: Optional[_Window], reason: str) -> None:
        self.lake_skipped += 1
        remedy = (
            "the existing object could not be read, so coverage could not be "
            "compared; clear it deliberately with "
            "`tools/diagnostics/chain_lake_seed.py --force`"
            if reason == "remote_provenance_unknown"
            else "the existing object covers more than this rebuild does"
        )
        logger.warning(
            "Chain lake upload skipped: it would not cover the object it replaces",
            event_category="backtest_data",
            event_type="chain_lake_overwrite_skipped",
            reason=reason,
            remedy=remedy,
            symbol=underlying,
            as_of=as_of.isoformat(),
            bucket=self.lake.bucket_name,
            new_window=new.as_log(),
            existing_window=None if old is None else old.as_log(),
        )

    def _path(self, underlying: str, as_of: date) -> Path:
        return self._root / underlying.upper() / f"{as_of.isoformat()}.parquet"

    def has(self, underlying: str, as_of: date) -> bool:
        """Whether the chain is present *locally*. Never consults the lake.

        A lake probe is a network round-trip, and every caller of ``has`` wants
        the cheap local question. ``get`` is the one that reaches for the lake.
        """
        return self._path(underlying, as_of).exists()

    def stored_window(self, underlying: str, as_of: date) -> Optional[dict]:
        """What the object this store would serve for ``(underlying, as_of)`` covers.

        FC-096 Phase A. A **read-only** provenance accessor: it answers "what
        window is already stored here", and changes nothing about ``put``,
        ``get``, the coverage rule or the FC-091 merge. It exists because the
        backfill's window rule has to build the UNION of the fresh window and
        the stored one — and the stored one lives inside the parquet, where no
        public method reached.

        Returns ``{"universe_dte", "strike_gte", "strike_lte", "model",
        "underlying_price"}`` (any of the first four ``None`` when the file
        pre-dates that column), or ``None`` when nothing is stored, the file is
        unreadable, or it holds no rows.

        Local first, then the lake — the same order ``get`` uses, and for the
        same reason: on Cloud Run the filesystem is empty every execution, so
        every answer has to come from GCS. A lake pull leaves the file in the
        local cache and remembers the remote window, so the ``get``/``put``
        that follow do not pay for it a second time.

        **A dev-machine caveat, deliberately not hidden.** When a local file
        exists this returns *its* window, not the lake object's, and the two can
        differ on a machine with a persistent cache. A union built on the
        narrower local window can then be refused by the mirror's monotone guard
        (counted as ``lake_skipped``, logged as ``chain_lake_overwrite_skipped``)
        — reported, never silent. The Job, whose filesystem starts empty, cannot
        hit this.
        """
        path = self._path(underlying, as_of)
        pulled = False
        if not path.exists():
            if not self._pull_from_lake(underlying, as_of, path):
                return None
            pulled = True
        try:
            df = pd.read_parquet(path)
        except Exception:
            # Same posture as ``get``: an unreadable file is a miss, not a
            # raise. Nothing is deleted here — ``get`` owns that decision, and
            # a provenance read must not have side effects on the cache.
            return None
        if pulled:
            self._remember_remote_window(underlying, as_of, df)
        if df.empty:
            return None
        window = _window_of(df)
        price = df["underlying_price"].iloc[0] if "underlying_price" in df.columns else _UNKNOWN

        def _num(value):
            return None if pd.isna(value) else float(value)

        dte = _num(window.universe_dte)
        return {
            "universe_dte": None if dte is None else int(dte),
            "strike_gte": _num(window.strike_gte),
            "strike_lte": _num(window.strike_lte),
            "model": window.model or None,
            "underlying_price": _num(price),
        }

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
        after ``os.replace``, so a torn write is never uploaded. If this run
        downloaded an object for this key and rejected it for not covering the
        request (FC-091), the two are unioned first and what lands on disk *and*
        in the lake is the merged superset; see ``_merge_rejected_lake_frame``.
        """
        path = self._path(snapshot.underlying, snapshot.as_of)
        path.parent.mkdir(parents=True, exist_ok=True)
        window = _Window(
            _UNKNOWN if universe_dte is None else float(universe_dte),
            _UNKNOWN if strike_gte is None else float(strike_gte),
            _UNKNOWN if strike_lte is None else float(strike_lte),
            model,
        )
        rows = [self._quote_to_row(q) for q in snapshot.all_quotes()]
        if not rows:
            # A real "no contracts traded" day: write one sentinel row (empty
            # symbol) carrying the underlying price so a later read distinguishes
            # an empty chain from a cache miss.
            rows = [self._empty_row(snapshot)]
        rows, window, mirror = self._merge_rejected_lake_frame(snapshot, rows, window)
        # Provenance is a property of the FILE, not of any row, and is stamped
        # on every row so ``_window_of``/``_covers`` can read it from row 0. A
        # merged file therefore carries the union window on rows from both
        # sources — which is the whole point of the merge.
        provenance = {
            "universe_dte": window.universe_dte,
            "strike_gte": window.strike_gte,
            "strike_lte": window.strike_lte,
            "model": window.model,
        }
        rows = [{**row, **provenance} for row in rows]
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            pd.DataFrame(rows, columns=_COLUMNS).to_parquet(tmp, index=False)
            os.replace(tmp, path)  # atomic within a filesystem
        finally:
            if tmp.exists():
                tmp.unlink()
        # Mirror to the lake AFTER os.replace, never from the temp file: what is
        # uploaded is exactly the bytes a local reader would read, so a torn
        # write can never become a permanently poisoned object. The mirror is
        # coverage-guarded — see ``_mirror_to_lake`` — because the local
        # overwrite above is always safe (this machine rebuilt it) while the
        # remote one is not (other machines read it).
        if mirror:
            self._mirror_to_lake(path, snapshot.underlying, snapshot.as_of, window)

    def _merge_rejected_lake_frame(self, snapshot: ChainSnapshot, rows, window):
        """Union this build with a lake file this run downloaded and rejected.

        Returns ``(rows, window, mirror)``. ``mirror`` is False only for the
        gap case, which must publish nothing at all.

        This is the FC-091 heal. Without it, a symbol whose strike window moved
        so that the rebuild is wider on one bound and narrower on the other can
        never update the lake — every upload is a narrowing somewhere, so the
        guard refuses it forever and the symbol is re-fetched cold on every
        monthly run (observed: SPY, IWM, PFE, ``rejected=231 skipped=231``).

        A merge is refused, and the ordinary monotone path left to decide, when
        the models differ, when either side's provenance is unknown, when the
        old frame's schema is not this one, when the two files were built
        against different closes for the session, when their **DTE reaches
        differ** (``dte_mismatch`` — only the strike axis may widen, see
        ``_merge_windows``) or when they **disagree about the strikes they both
        cover** (``overlap_conflict``, see ``_overlap_conflict``). The last two
        are loud, because each is a case a naive union would have absorbed into
        a file that passes every check and is missing rows.

        The close check is not redundant with ``get``'s: ``_covers`` runs
        *before* the price comparison there, so a frame can be remembered
        without its price ever having been looked at.

        **Only the downloaded path heals.** A frame is remembered by ``get``
        only when it came off the lake this run, so a machine with a warm local
        cache never merges — it rebuilds over its own file, exactly as before.
        The heal is for the Job, whose filesystem is ephemeral and whose every
        chain-day therefore arrives from the lake.
        """
        key = self._key(snapshot.underlying, snapshot.as_of)
        old_df = self._lake_rejected_frames.pop(key, None)
        if old_df is None:
            return rows, window, True

        old_window = _window_of(old_df)
        merged_window, reason = _merge_windows(window, old_window)
        if reason == "strike_gap":
            self.lake_merge_gaps += 1
            logger.warning(
                "Chain lake merge refused: the two strike windows do not overlap",
                event_category="backtest_data",
                event_type="chain_lake_merge_gap",
                remedy=(
                    "neither file covers the strikes between the two windows. "
                    "There is no knob to widen: the strike window is derived "
                    "from the session's bars and the run's positions "
                    "(Simulator._strike_anchors), not from settings.yaml. The "
                    "only lever is a deliberate "
                    "`tools/diagnostics/chain_lake_seed.py --force` from a "
                    "local build that spans both windows"
                ),
                symbol=snapshot.underlying,
                as_of=snapshot.as_of.isoformat(),
                bucket=getattr(self.lake, "bucket_name", None),
                new_window=window.as_log(),
                existing_window=old_window.as_log(),
            )
            # Upload nothing: with a gap there is no file this run can build
            # that safely replaces the object.
            return rows, window, False
        if reason == "dte_mismatch":
            # Loud, because this is the failure the naive `max(universe_dte)`
            # rule would have absorbed: a file claiming the longer reach across
            # the newer strikes while holding none of the longer-dated
            # contracts there. See ``_merge_windows``.
            self.lake_merge_refused += 1
            logger.warning(
                "Chain lake merge refused: the two files have different DTE "
                "reaches, so their union is not a rectangle",
                event_category="backtest_data",
                event_type="chain_lake_merge_refused",
                reason="dte_mismatch",
                remedy=(
                    "only the strike axis may widen on a merge; a file whose "
                    "DTE reach also moved would claim a corner neither fetch "
                    "covered. This day falls back to the coverage-monotone "
                    "path and stays cold until one build's window covers the "
                    "other's"
                ),
                symbol=snapshot.underlying,
                as_of=snapshot.as_of.isoformat(),
                bucket=getattr(self.lake, "bucket_name", None),
                new_universe_dte=window.as_log()["universe_dte"],
                existing_universe_dte=old_window.as_log()["universe_dte"],
                new_window=window.as_log(),
                existing_window=old_window.as_log(),
            )
            return rows, window, True
        if reason is not None:
            # model_changed / unknown_provenance: not mergeable, and already
            # fully explained by the coverage-monotone path's own skip event,
            # which is still entitled to its say.
            return rows, window, True

        try:
            old_price = float(old_df["underlying_price"].iloc[0])
        except Exception:
            return rows, window, True
        if not _close(old_price, float(snapshot.underlying_price)):
            return rows, window, True

        merged_rows = _union_rows(rows, old_df)
        if merged_rows is None:
            # Schema-incompatible old frame. Silent: nothing about it is
            # actionable beyond the skip the monotone path will log.
            return rows, window, True

        conflict = _overlap_conflict(rows, old_df, window, old_window)
        if conflict is not None:
            self.lake_merge_refused += 1
            logger.warning(
                "Chain lake merge refused: the two fetches disagree about the "
                "strikes they both cover",
                event_category="backtest_data",
                event_type="chain_lake_merge_refused",
                reason="overlap_conflict",
                remedy=(
                    "one of the two fetches is incomplete or the vendor "
                    "restated the session; merging would absorb that into a "
                    "file that looks consistent and is missing rows. Compare "
                    "the two builds before forcing either into the lake"
                ),
                symbol=snapshot.underlying,
                as_of=snapshot.as_of.isoformat(),
                bucket=getattr(self.lake, "bucket_name", None),
                existing_contracts_in_overlap=conflict[0],
                new_contracts_in_overlap=conflict[1],
                new_window=window.as_log(),
                existing_window=old_window.as_log(),
            )
            return rows, window, True

        self.lake_merged += 1
        logger.info(
            "Chain lake merge: rebuilt chain unioned with the object it replaces",
            event_category="backtest_data",
            event_type="chain_lake_merged",
            symbol=snapshot.underlying,
            as_of=snapshot.as_of.isoformat(),
            bucket=getattr(self.lake, "bucket_name", None),
            new_window=window.as_log(),
            existing_window=old_window.as_log(),
            merged_window=merged_window.as_log(),
            new_rows=len(rows),
            merged_rows=len(merged_rows),
        )
        return merged_rows, merged_window, True

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
        The deletion is **local only** — the lake object is never touched,
        because this path cannot tell genuine corruption from a reader-side
        failure (pyarrow skew, MemoryError, an exhausted fd limit) and must not
        destroy history on that signal. A rebuild replaces the object through
        the coverage-guarded mirror path instead.

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
        from_lake = False
        if not path.exists():
            if not self._pull_from_lake(underlying, as_of, path):
                return None
            from_lake = True
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
            # LOCAL ONLY. The lake object is deliberately left alone: this
            # ``except`` fires on any reader-side failure — a pyarrow version
            # skew, MemoryError, an exhausted file-descriptor limit — not only
            # on genuine corruption, and one such failure must never delete
            # history that may not be re-fetchable from the vendor. A rebuild
            # replaces the object through the guarded mirror path instead.
            path.unlink(missing_ok=True)
            return None
        if from_lake:
            # Remember what the object claims to cover, so a rebuild later in
            # this run can prove it would not narrow it.
            self._remember_remote_window(underlying, as_of, df)
        if df.empty:
            if from_lake:
                self.lake_rejected += 1
            return None
        if not self._covers(df, universe_dte, strike_gte, strike_lte, model):
            if from_lake:
                self.lake_rejected += 1
                # FC-091. The caller will now rebuild this day and ``put`` it.
                # Hold the rejected frame so that write can be the UNION of the
                # two windows rather than a replacement the monotone guard has
                # to refuse. Remembered only on the coverage rejection: the
                # ``df.empty`` case has nothing to contribute, and the
                # underlying-price rejection below is a disagreement about the
                # session itself, which no union can reconcile.
                self._lake_rejected_frames[self._key(underlying, as_of)] = df
            return None

        cached_price = float(df["underlying_price"].iloc[0])
        if underlying_price is not None and not _close(cached_price, underlying_price):
            # The file was built against a different close for the same session
            # (a restated bar, or a raw/adjusted mix). Every delta in it was
            # computed against that price, so it cannot answer this request.
            if from_lake:
                self.lake_rejected += 1
            return None

        puts, calls = self._rows_to_quotes(df, universe_dte, strike_gte, strike_lte)
        puts.sort(key=lambda x: x.strike)
        calls.sort(key=lambda x: x.strike)
        return ChainSnapshot(underlying, as_of, cached_price, puts, calls)

    @staticmethod
    def _rows_to_quotes(
        df,
        universe_dte: Optional[int],
        strike_gte: Optional[float],
        strike_lte: Optional[float],
    ) -> "tuple[list, list]":
        """Narrow the file to the request and convert it to ``ChainQuote``s.

        FC-060 Layer 2 (D5) rewrote this off ``df.iterrows()``. Row conversion
        was measured at roughly 80% of a warm replay's entire runtime: a
        symbol-year is ~250 files of a few hundred rows each, and ``iterrows``
        materialises a fresh ``Series`` per row and then pays a label lookup per
        cell. ``itertuples`` walks the same data as C-level tuples, and the
        strike/DTE narrowing is done as a vectorised mask so the per-row work is
        only done for rows that survive it.

        **The output is identical, not merely equivalent**, and that is the whole
        contract: the cache must be invisible to results. Row order out of
        ``itertuples`` is file order, exactly as ``iterrows`` gives, and both
        lists are stably sorted by strike afterwards, so ties keep file order
        either way. ``tests/test_backtest_data.py`` pins this against a
        line-for-line copy of the old converter on a real cached file.
        """
        # Each clause is the NEGATION of the old loop's `continue`, not its
        # apparent inverse: `strike < gte` is False for a NaN strike, so the old
        # loop kept such a row. `strike >= gte` would drop it. Identical output
        # includes identical behaviour on degenerate data.
        mask = df["symbol"].astype(bool)
        if strike_gte is not None:
            mask &= ~(df["strike"].astype(float) < strike_gte)
        if strike_lte is not None:
            mask &= ~(df["strike"].astype(float) > strike_lte)
        if universe_dte is not None:
            mask &= ~(df["dte"].astype(int) > universe_dte)
        narrowed = df[mask]

        puts, calls = [], []
        for row in narrowed.itertuples(index=False):
            iv = row.implied_volatility
            delta = row.delta
            quote = ChainQuote(
                symbol=row.symbol,
                underlying=row.underlying,
                as_of=date.fromisoformat(row.as_of),
                expiration=date.fromisoformat(row.expiration),
                strike=float(row.strike),
                option_type=row.option_type,
                dte=int(row.dte),
                underlying_price=float(row.underlying_price),
                mark=float(row.mark),
                bid=float(row.bid),
                ask=float(row.ask),
                implied_volatility=None if pd.isna(iv) else float(iv),
                delta=None if pd.isna(delta) else float(delta),
                volume=int(row.volume),
                modeled_spread=bool(row.modeled_spread),
                modeled_greeks=bool(row.modeled_greeks),
            )
            (puts if quote.option_type == "put" else calls).append(quote)
        return puts, calls

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

    # ``_row_to_quote`` (the per-row ``Series`` converter) was deleted by
    # FC-060 Layer 2 D5 and replaced by ``_rows_to_quotes`` above. A verbatim
    # copy lives in tests/test_backtest_data.py as ``_row_to_quote_legacy``,
    # where it serves as the identity oracle for the rewrite — in src it would
    # be dead code that quietly drifts from the thing it is supposed to pin.
