"""Serving one sweep cell's detail artifact out of GCS (FC-096 Phase B B2).

The engine writes ``gs://<bucket>/sim-artifacts/v1/<run_id>/
<scenario>__<symbol>__<split>.json.gz`` as it replays; this reads one back for
the console. Phase E renders it — this PR only makes it reachable.

**Pure functions here, a thin router there**, the rule
``services/sweeps.py`` states: FastAPI is absent from the CI image that actually
runs this suite, so any rule living in ``routers/v2.py`` is a rule nobody checks.
Everything a mistake would cost — a path segment that escapes the prefix, an
object name that disagrees with the writer's, a missing artifact rendered as an
empty one — is decided in this file.

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
        artifact_object_name, validate_scenario_name,
    )
except ImportError:  # dashboard image: the same file, copied flat
    from scenario_identity import (  # type: ignore
        artifact_object_name, validate_scenario_name,
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
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Underlyings, as the engine upper-cases them (`run_sweep` does `s.upper()`).
# Dots and dashes appear in real tickers (BRK.B, RDS-A).
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}$")

# `runner._windows` produces exactly these three. A closed set rather than a
# pattern: there is no fourth split, and accepting one would build a name that
# can only 404.
SPLITS = ("all", "fit", "holdout")


class ArtifactPathError(ValueError):
    """A path segment that cannot address an artifact. Carries its own reason."""


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

    @property
    def enabled(self) -> bool:
        return bool(self.bucket_name)

    def _bucket(self):
        if self._client is None:
            from google.cloud import storage  # imported lazily; see docstring

            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def fetch(self, run_id: str, scenario: str, symbol: str,
              split: str) -> Optional[bytes]:
        """The artifact's JSON bytes, or ``None`` when the object is absent.

        ``None`` is reserved for absence. Every other failure (permissions, a
        dead bucket, a corrupt object) raises, because "we could not read it"
        and "it is not there" lead an operator to different places, and a
        blanket ``None`` would send every one of them to the wrong one.
        """
        from google.cloud.exceptions import NotFound

        name = object_name(run_id, scenario, symbol, split)
        blob = self._bucket().blob(name)
        try:
            raw = blob.download_as_bytes(timeout=30.0)
        except NotFound:
            return None
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
