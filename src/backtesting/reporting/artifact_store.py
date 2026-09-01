"""Where a cell's detail artifact goes: gzipped JSON in GCS.

FC-096 Phase B B2. ``artifact.py`` decides WHAT an artifact is; this decides
where it lands and how a failure to land is accounted for. The split is the one
``chain_store.py`` makes for the same reason — the serialiser is pure and
testable without a network, and the only code that touches GCS is the twenty
lines below.

``gs://<bucket>/sim-artifacts/v1/<run_id>/<scenario>__<symbol>__<split>.json.gz``

The object name is built by ``scenarios.identity.artifact_object_name`` rather
than here, because the dashboard serves these objects and ships no engine: the
writer and the reader must agree bit-for-bit, and ``identity.py`` is the module
that is copied flat into the dashboard image. That is also where the "no ``__``
in a scenario name" rule lives, which is what makes ``rsplit('__', 2)`` a
sufficient parser.

**Write-through, best effort, and counted.** An artifact is evidence, not a
result: the ``scenario_runs`` rows are the run's output and they are written
somewhere else entirely. A GCS hiccup must therefore never fail a cell, and it
must never be silent either — every failure logs ``sim_artifact_write_failed``
and increments ``failed``, and the terminal sweep row carries
``artifacts_complete`` = (written == non-errored cells) so an operator can tell
a complete set from a partial one without listing the bucket.

**No client is constructed until the first write.** ``_storage_client`` is a
module-level indirection (``chain_store``'s idiom) so a process with artifacts
switched off never imports the GCS stack, and tests can monkeypatch the name to
assert that a path which must not touch the network did not.
"""

from __future__ import annotations

import gzip
import json
import os
from typing import Any, Dict, Optional

import structlog

from ..scenarios.identity import artifact_object_name

logger = structlog.get_logger(__name__)

# The bucket the lake already lives in (``chains/v1/`` is its neighbour). An env
# override exists so a second project or a test can point somewhere else;
# setting it to the empty string DISABLES artifact writing entirely, which is
# the switch a "write nothing anywhere" contract can be pinned against.
ARTIFACT_BUCKET_ENV = "SIM_ARTIFACT_BUCKET"
DEFAULT_ARTIFACT_BUCKET = "gen-lang-client-0607444019-options-data"

# Per-RPC ceiling. Same reasoning as ``chain_store.LAKE_TIMEOUT_S``: without one,
# ``google-cloud-storage`` retries a stalled request until the TASK timeout, so a
# degraded GCS would turn "artifacts are best-effort" into "the sweep never
# finished". A timeout is what makes the best-effort policy reachable.
ARTIFACT_TIMEOUT_S = 30.0

# gzip level. 6 is the zlib default and the knee of the curve for JSON; 9 costs
# roughly twice the CPU for low single-digit percent on this shape of payload,
# and this runs inside the replay loop.
GZIP_LEVEL = 6

# `mtime=0` and no filename in the gzip header, so two identical artifacts
# produce identical BYTES. Without it every object carries its write time inside
# the compressed stream and nothing can be compared or deduplicated by hash.
_GZIP_MTIME = 0


def _storage_client():
    """Build a GCS client.

    Indirected through a module-level function so the import is deferred (a
    process with no artifact writing never imports the GCS stack) and so tests
    can monkeypatch this name to prove a path constructed no client.
    """
    from google.cloud import storage  # imported lazily; see docstring

    return storage.Client()


def artifact_bucket() -> Optional[str]:
    """The configured bucket, or ``None`` when artifacts are switched off.

    An UNSET env var means "use the default bucket"; an env var set to the empty
    string means "write nothing". The two are deliberately different: the first
    is the normal deployment, the second is an explicit off switch, and
    collapsing them would make the off switch unreachable.
    """
    raw = os.environ.get(ARTIFACT_BUCKET_ENV)
    if raw is None:
        return DEFAULT_ARTIFACT_BUCKET
    raw = raw.strip()
    return raw or None


def artifact_bytes(payload: Dict[str, Any]) -> bytes:
    """``payload`` as deterministic gzipped UTF-8 JSON.

    ``sort_keys=True`` and a zeroed gzip header make the bytes a pure function
    of the payload — which is what lets a test compare two artifacts, and what
    would let a future reader hash one. ``default=str`` is the same escape hatch
    ``identity.py`` uses: an unexpected leaf must serialise rather than raise
    inside a replay loop.

    Note that ``cell_artifact``'s ``generated_at`` stamp still moves between two
    runs. Determinism here is about THIS payload, not about two replays.
    """
    raw = json.dumps(payload, sort_keys=True, default=str,
                     separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, compresslevel=GZIP_LEVEL, mtime=_GZIP_MTIME)


class ArtifactWriter:
    """Write-through artifact sink for one sweep run. Never raises.

    Usage is one instance per run, handed to ``run_sweep(artifact_sink=...)``
    as its bound ``write`` method. ``written``/``failed`` are what
    ``artifacts_complete`` is computed from.

    Args:
        run_id: the sweep's run id; the object-name directory.
        bucket: override the configured bucket. ``None`` reads the environment.
        client: an injected GCS client (tests). When given, no client is built.
    """

    def __init__(self, run_id: str, *, bucket: Optional[str] = None,
                 client: Any = None) -> None:
        self.run_id = run_id
        self.bucket_name = bucket if bucket is not None else artifact_bucket()
        self._client = client
        self.written = 0
        self.failed = 0
        self.last_error: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"ArtifactWriter(run_id={self.run_id!r}, "
                f"bucket={self.bucket_name!r}, written={self.written}, "
                f"failed={self.failed})")

    @property
    def enabled(self) -> bool:
        """Whether a write would go anywhere. ``False`` => a no-op sink."""
        return bool(self.bucket_name)

    def _bucket(self):
        if self._client is None:
            self._client = _storage_client()
        return self._client.bucket(self.bucket_name)

    def write(self, payload: Dict[str, Any]) -> bool:
        """Store one artifact. Returns whether it landed; never raises.

        The coordinates come out of the payload's own provenance block rather
        than being passed alongside it, so the object name and the object's
        contents cannot disagree about which cell this is — the failure mode
        that would put arm A's ledger at arm B's address.
        """
        if not self.enabled:
            return False
        prov = (payload or {}).get("provenance") or {}
        scenario = str(prov.get("scenario") or "")
        symbol = str(prov.get("symbol") or "")
        split = str(prov.get("split") or "")
        try:
            name = artifact_object_name(
                prov.get("run_id") or self.run_id, scenario, symbol, split)
            blob = self._bucket().blob(name)
            blob.upload_from_string(
                artifact_bytes(payload),
                # `application/gzip`, and deliberately NO `content_encoding`.
                # Setting the latter turns on GCS decompressive transcoding, so
                # a download would silently return the DECOMPRESSED bytes and
                # every reader's gzip handling would be a lie that happens to
                # work. An opaque gzip file is the honest object.
                content_type="application/gzip",
                timeout=ARTIFACT_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - evidence must not fail a cell
            self.failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning(
                "Detail artifact could not be written — the cell's result is "
                "unaffected, but its evidence is missing",
                event_category="backtest",
                event_type="sim_artifact_write_failed",
                run_id=self.run_id, scenario=scenario, symbol=symbol,
                split=split, bucket=self.bucket_name,
                error=self.last_error,
            )
            return False
        self.written += 1
        return True
