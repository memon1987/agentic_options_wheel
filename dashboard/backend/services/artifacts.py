"""Serving one sweep cell's detail artifact out of GCS (FC-096 Phase B B2).

The engine writes ``gs://<bucket>/sim-artifacts/v1/<run_id>/
<scenario>__<symbol>__<split>.json.gz`` as it replays; this reads one back for
the console. Phase E renders it — this PR only makes it reachable.

**Pure functions here, a thin router there**, the rule ``services/sweeps.py``
states. Note the JUSTIFICATION has changed: FastAPI is no longer absent from the
CI image — the root ``requirements.txt`` pins ``fastapi``/``starlette``/``httpx``
and the ``_HAS_FASTAPI``-guarded router tests run there — but the rule stands,
because a pure module is testable without wiring a router and a rule stated in
two places drifts. Everything a mistake would cost — a path segment that escapes
the prefix, an object name that disagrees with the writer's, a missing artifact
rendered as an empty one — is decided in this file.

Since FC-096 Phase E PR-1 this module serves TWO object families out of the same
prefix: the per-CELL detail artifact (``<scenario>__<symbol>__<split>``) and the
per-WINDOW **bars sidecar** (``bars/<SYMBOL>__<split>``). The sidecar has no
scenario segment on purpose — the bars are the window, every arm replayed
against them, and its buy-and-hold curve is the base arm's SCORED benchmark.
Both names come from ``scenario_identity``; neither parser accepts the other's
name.

**The object name is IMPORTED, not restated.** ``scenario_identity`` (the flat
copy of ``src/backtesting/scenarios/identity.py`` that
``dashboard/Dockerfile`` ships) owns ``artifact_object_name`` and the
"no ``__`` in a scenario name" rule that makes its ``rsplit('__', 2)`` inverse
sound. A second implementation of the name here would drift, and its drift would
present as a 404 on an artifact that exists — the most confusing possible
symptom.

**The response is decompressed server-side**, and returned as
``application/json``. The alternative — passing the gzip through with
``Content-Encoding: gzip`` — has two ways to be silently wrong: GCS applies
*decompressive transcoding* to any object whose metadata declares that encoding
(so the "gzip" body would already be plain bytes), and an intermediary that
re-encodes an already-encoded body double-wraps it. Artifacts are tens to
hundreds of kilobytes; the bandwidth this gives up is not worth either failure
mode. The stored object stays gzipped — this is a transport decision only.

**No GCS client is constructed at import.** ``services/bigquery.py``'s idiom: a
module-level singleton built on first use, so the module stays importable in a
test environment with no credentials.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

try:  # repo / test environment
    from src.backtesting.scenarios.identity import (
        SYMBOL_RE, artifact_object_name, bars_object_name,
        validate_scenario_name, validate_symbol,
    )
except ImportError:  # dashboard image: the same file, copied flat
    from scenario_identity import (  # type: ignore
        SYMBOL_RE, artifact_object_name, bars_object_name,
        validate_scenario_name, validate_symbol,
    )

logger = logging.getLogger(__name__)

# Same env override and default as the writer
# (``src/backtesting/reporting/artifact_store.py``). Duplicated rather than
# imported because that module is engine-side and imports ``structlog`` and the
# GCS SDK at module scope; the VALUE is pinned equal by a test, the same
# treatment ``ENGINE_VERSION`` gets.
ARTIFACT_BUCKET_ENV = "SIM_ARTIFACT_BUCKET"
DEFAULT_ARTIFACT_BUCKET = "gen-lang-client-0607444019-options-data"

# What a run id may be. The engine mints ``uuid4().hex[:16]`` and the API mints
# the same shape, so this is generous rather than narrow — but it is a CHARSET
# check, not just a length check, because this value becomes a path segment in
# an object name. `_` and `-` are allowed for hand-made ids; `/`, `.` and `..`
# are not, which is what stops a request escaping the prefix.
#
# **`\Z`, never `$`** — here, in `SYMBOL_RE`, and in
# `identity.SCENARIO_NAME_RE`. Python's `$` also matches immediately
# BEFORE a trailing newline, so `"r1\n"` satisfied every one of these
# checks end to end and only failed deep inside the GCS client, where a
# newline in an object name is a header-splitting character. Every segment
# here becomes part of an object name, and an object name is a single line
# by construction.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")

# `SYMBOL_RE` / `validate_symbol` are IMPORTED, not restated — one addressing
# rule, in the module the dashboard image copies flat, enforced by the CLI
# (`main.py --symbols`), by the writer and here. Note this is a different
# question from `services/sweeps.SYMBOL_RE`, which is what may be SUBMITTED: that
# one is deliberately narrower, and a test pins it as a strict subset so the API
# can never accept a symbol this endpoint could not then address.

# `runner._windows` produces exactly these three. A closed set rather than a
# pattern: there is no fourth split, and accepting one would build a name that
# can only 404.
SPLITS = ("all", "fit", "holdout")


class ArtifactPathError(ValueError):
    """A path segment that cannot address an artifact. Carries its own reason."""


class ArtifactBucketError(RuntimeError):
    """The bucket itself could not be resolved — configuration or IAM.

    Its own type because it must never be confused with a missing object: GCS
    raises ``NotFound`` for a missing bucket and a missing object alike, and the
    two are a 502 and a 404 respectively.
    """


class ArtifactReadError(RuntimeError):
    """The object exists but could not be served as an artifact (e.g. empty)."""


def artifact_bucket() -> Optional[str]:
    """The configured bucket, or ``None`` when artifacts are switched off.

    Mirrors ``artifact_store.artifact_bucket``: unset means "the default
    bucket", explicitly empty means "there are no artifacts", and the two are
    different states on purpose.
    """
    raw = os.environ.get(ARTIFACT_BUCKET_ENV)
    if raw is None:
        return DEFAULT_ARTIFACT_BUCKET
    raw = raw.strip()
    return raw or None


def validate_path(run_id: str, scenario: str, symbol: str,
                  split: str) -> Tuple[str, str, str, str]:
    """Check the four segments and return them normalised.

    Raises ``ArtifactPathError`` with a reason a human can act on. The scenario
    is checked by the ENGINE's own ``validate_scenario_name`` — the same
    function that refused the name at submit time — so a name that could not
    have been submitted cannot be requested either, and the ``__`` rule that
    makes the object name parseable is enforced on both ends by one rule.
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ArtifactPathError(
            f"invalid run_id {str(run_id)[:32]!r}: expected 1-64 characters "
            f"from [A-Za-z0-9_-]")
    try:
        validate_scenario_name(scenario, "artifact path")
    except ValueError as exc:
        raise ArtifactPathError(str(exc)) from exc
    symbol = (symbol or "").upper()
    if not SYMBOL_RE.match(symbol):
        raise ArtifactPathError(
            f"invalid symbol {str(symbol)[:32]!r}: expected an uppercase "
            f"ticker of at most 16 characters")
    if split not in SPLITS:
        raise ArtifactPathError(
            f"invalid split {str(split)[:32]!r}: expected one of "
            f"{', '.join(SPLITS)}")
    return run_id, scenario, symbol, split


def object_name(run_id: str, scenario: str, symbol: str, split: str) -> str:
    """The validated object name for one cell's artifact."""
    run_id, scenario, symbol, split = validate_path(run_id, scenario, symbol, split)
    return artifact_object_name(run_id, scenario, symbol, split)


def validate_bars_path(run_id: str, symbol: str,
                       split: str) -> Tuple[str, str, str]:
    """Check the THREE segments of a bars sidecar path and return them normalised.

    FC-096 Phase E PR-1. The same rules as ``validate_path`` minus the scenario:
    a sidecar is one per (run, symbol, split), because the bars ARE the window
    and every arm of it replayed against them. Written as its own function
    rather than as ``validate_path(run_id, BASE, symbol, split)`` so that a
    reader of a URL cannot conclude a sidecar is per-arm — and so the error
    messages name the segments this route actually has.
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ArtifactPathError(
            f"invalid run_id {str(run_id)[:32]!r}: expected 1-64 characters "
            f"from [A-Za-z0-9_-]")
    symbol = (symbol or "").upper()
    if not SYMBOL_RE.match(symbol):
        raise ArtifactPathError(
            f"invalid symbol {str(symbol)[:32]!r}: expected an uppercase "
            f"ticker of at most 16 characters")
    if split not in SPLITS:
        raise ArtifactPathError(
            f"invalid split {str(split)[:32]!r}: expected one of "
            f"{', '.join(SPLITS)}")
    return run_id, symbol, split


def bars_name(run_id: str, symbol: str, split: str) -> str:
    """The validated object name for one window's bars sidecar."""
    run_id, symbol, split = validate_bars_path(run_id, symbol, split)
    return bars_object_name(run_id, symbol, split)


def decode(raw: bytes) -> bytes:
    """The stored object's bytes -> the JSON bytes a client should get.

    Tolerates an object that is NOT gzipped rather than 500-ing on it: an
    artifact written by a future writer that stopped compressing, or one
    re-uploaded by hand, is still readable JSON and serving it is strictly
    better than refusing it. The gzip magic number is the discriminator.
    """
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


class ArtifactStore:
    """Reads artifact objects. One GCS client, built on first use."""

    def __init__(self, bucket: Optional[str] = None, client: Any = None) -> None:
        self.bucket_name = bucket if bucket is not None else artifact_bucket()
        self._client = client
        self._bucket_handle = None

    @property
    def enabled(self) -> bool:
        return bool(self.bucket_name)

    def _bucket(self):
        """The bucket handle, RESOLVED once per process on first use.

        **Resolution is the whole point, not caching.** ``client.bucket(name)``
        is a local constructor that talks to nobody, so a bucket that does not
        exist — or that this service account cannot see — produces a perfectly
        ordinary handle whose first download raises ``NotFound``: **the same
        exception class a missing OBJECT raises**. Catching ``NotFound`` around
        the download alone therefore reports a misconfigured or ungranted bucket
        as "no artifact for this cell", on every cell, for ever. That is the
        exact failure this whole module is written to avoid, arriving through
        the one door that looks benign.

        So the bucket is resolved with ``get_bucket`` — a real RPC — the first
        time it is needed, and a failure there propagates as
        ``ArtifactBucketError``. Once per process, not once per request: the
        answer cannot change without a redeploy, and paying an RPC per artifact
        read would be a tax on the healthy path to detect a startup condition.
        """
        if self._bucket_handle is not None:
            return self._bucket_handle
        if self._client is None:
            from google.cloud import storage  # imported lazily; see docstring

            self._client = storage.Client()
        try:
            self._bucket_handle = self._client.get_bucket(self.bucket_name)
        except Exception as exc:  # noqa: BLE001 - re-raised as our own type
            raise ArtifactBucketError(
                f"the artifact bucket {self.bucket_name!r} could not be "
                f"resolved ({type(exc).__name__}: {exc}). This is a "
                f"configuration or IAM failure, NOT a missing artifact — check "
                f"the bucket name and that this service account has "
                f"storage.buckets.get / storage.objects.get on it."
            ) from exc
        return self._bucket_handle

    def fetch(self, run_id: str, scenario: str, symbol: str,
              split: str) -> Optional[bytes]:
        """The artifact's JSON bytes, or ``None`` when the OBJECT is absent.

        ``None`` is reserved for one fact: this cell has no artifact. Every
        other failure raises, because "we could not read it" and "it is not
        there" lead an operator to different places, and a blanket ``None``
        would send every one of them to the wrong one.

        The bucket is resolved first (see ``_bucket``) precisely so a missing or
        ungranted BUCKET cannot borrow the benign answer — GCS raises the same
        ``NotFound`` for both, and only the order of the two calls tells them
        apart.

        A zero-byte object also raises rather than returning empty bytes: an
        object that exists but holds nothing is a truncated or failed write, and
        serving it as an empty 200 would put "this cell did nothing" in front of
        a reader when the truth is "this cell's evidence was lost".
        """
        from google.cloud.exceptions import NotFound

        name = object_name(run_id, scenario, symbol, split)
        blob = self._bucket().blob(name)
        try:
            raw = blob.download_as_bytes(timeout=30.0)
        except NotFound:
            return None
        if not raw:
            raise ArtifactReadError(
                f"the artifact object {name!r} exists but is EMPTY — a "
                f"truncated or failed write, not a cell that did nothing")
        return decode(raw)

    def fetch_bars(self, run_id: str, symbol: str,
                   split: str) -> Optional[bytes]:
        """One window's bars sidecar, or ``None`` when the OBJECT is absent.

        FC-096 Phase E PR-1. Identical policy to ``fetch``, and identical for a
        reason: "absent" and "unreadable" send an operator to different places
        here too, and a sidecar is absent for a whole population of runs —
        every one replayed before PR-1 deployed — so ``None`` has to keep
        meaning exactly one thing.
        """
        from google.cloud.exceptions import NotFound

        name = bars_name(run_id, symbol, split)
        blob = self._bucket().blob(name)
        try:
            raw = blob.download_as_bytes(timeout=30.0)
        except NotFound:
            return None
        if not raw:
            raise ArtifactReadError(
                f"the bars object {name!r} exists but is EMPTY — a truncated "
                f"or failed write, not a window with no bars")
        return decode(raw)


_store: Optional[ArtifactStore] = None


def get_artifact_store() -> ArtifactStore:
    """The process-wide store singleton, built on first use."""
    global _store
    if _store is None:
        _store = ArtifactStore()
    return _store


def _reset_for_tests() -> None:
    """Drop the singleton so a test can inject a fake client."""
    global _store
    _store = None


def artifact_headers(name: str) -> Dict[str, str]:
    """Response headers for a served artifact.

    ``no-store`` is deliberate rather than lazy: an artifact is immutable once
    written, so caching would be safe — but the run id in the URL already makes
    every URL unique, so a cache buys nothing and a stale 404 (requested while
    the sweep was still replaying, cached, served after it finished) would be a
    real regression.
    """
    return {
        "Cache-Control": "no-store",
        "X-Artifact-Object": name,
    }
