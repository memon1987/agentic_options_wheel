"""Engine identity — the content hash of the code a replay actually executes.

**Stdlib only, and runnable standalone.** This module is invoked by
``cloudbuild.yaml`` inside a bare ``python:3.11-slim`` step, with no
``requirements.txt`` installed, so that the dashboard image can be stamped with
the SAME value the Job computes. Anything imported here that is not in the
standard library breaks the deploy, not a test.

Why a content hash rather than ``git_commit`` (FC-096 Phase B, B1)
-----------------------------------------------------------------
``sweep_key`` used to carry the commit SHA. That is *sound* — a different commit
is a different engine — but it is far too coarse: **every** merge to ``main``
invalidates **every** stored sweep result, including the hundreds of merges that
cannot possibly change a replay's numbers (a README edit, a dashboard CSS tweak,
a `cloudbuild.yaml` flag). Results that are still valid get re-measured, which
under Phase B's weekly battery is the difference between a cheap Saturday and an
expensive one.

So the key asks the narrower question: *did the code the replay executes
change?* The answer is the hash of ``src/**``.

What is in the hash, and why the boundary is exactly there
----------------------------------------------------------
**Every file under ``src/``, of every type.** Not just ``*.py``, and not just
the backtesting package:

* the replay drives the LIVE strategy — ``src/strategy/**``, ``src/api/**``,
  ``src/data/**``, ``src/utils/**`` are all executed inside a simulated day, so
  a one-byte change to ``put_seller.py`` changes what a replay does;
* ``src/backtesting/data/earnings_dates.json`` and ``dividend_history.json`` are
  committed *inputs* to the replay (the FC-013 earnings gate and the dividend
  credit read them). A vendor-table correction changes results without changing
  a line of code, and a ``*.py``-only hash would serve the pre-correction
  numbers for ever.

**Nothing outside ``src/``.** ``docs/``, ``dashboard/``, ``deploy/``,
``tests/``, ``cloudbuild.yaml`` cannot change a replay's output, and including
them would reintroduce exactly the over-invalidation the commit SHA had.

**Content, never metadata.** No mtime, no inode, no directory listing order:
the Job's checkout and a dashboard build's checkout are different files on
different machines with different timestamps, and they must agree bit-for-bit.
Compiled artefacts are skipped (``__pycache__`` trees, ``*.pyc``) — they are
derived, not source, and their presence depends on whether anything happened to
import the tree first.

Provenance is NOT lost: ``git_commit`` is still stored on every sweep row. It
simply no longer decides identity.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ``src.backtesting.screen.ENGINE_VERSION``. DUPLICATED rather than imported,
# for the same reason ``dashboard/backend/services/sweeps.py`` duplicates it:
# ``screen.py`` imports the engine (pandas, the Alpaca SDK, the simulator), and
# this module has to be importable — and runnable — with nothing but the
# standard library. Pinned byte-equal by
# ``tests/test_engine_identity.py::TestTheEngineVersionIsNotAFork``.
#
# It is in the digest even though ``screen.py``'s bytes are already hashed,
# because the two answer different questions: the file hash says "the code
# changed", this says "the operator declared a new engine generation" — and an
# operator who bumps it deliberately wants the cache invalidated.
ENGINE_VERSION = "fc-069-scanner-rewire"

# Versioned so a future change to the digest recipe (a different separator, a
# different boundary) is a deliberate, visible invalidation rather than a silent
# one that looks like a code change.
DIGEST_SCHEME = "engine-identity/v1"

# Non-source noise, skipped DETERMINISTICALLY: the rule is a constant here, not
# a judgement call at walk time, so the Job's tree (fresh checkout, no bytecode)
# and a developer's tree (imported a hundred times, `__pycache__` everywhere)
# produce the same digest.
SKIP_DIR_NAMES = frozenset({"__pycache__"})
SKIP_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd"})
SKIP_FILE_NAMES = frozenset({".DS_Store"})

# ``<repo>/src`` — this file is ``<repo>/src/backtesting/scenarios/engine_identity.py``.
SRC_DIR = Path(__file__).resolve().parents[2]

_CACHED: Optional[str] = None


def _walk(src_dir: Path) -> List[Tuple[str, Path]]:
    """``(relative posix path, absolute path)`` for every hashed file, sorted.

    Sorted on the RELATIVE path so the ordering is a property of the tree's
    contents rather than of the filesystem's directory order, which differs
    between machines and between ext4 and APFS.
    """
    entries: List[Tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        # Pruned in place — `os.walk` reads this list back, so removing a name
        # skips the whole subtree rather than just the directory entry.
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        for name in filenames:
            if name in SKIP_FILE_NAMES:
                continue
            path = Path(dirpath) / name
            if path.suffix in SKIP_SUFFIXES:
                continue
            rel = path.relative_to(src_dir).as_posix()
            entries.append((f"src/{rel}", path))
    entries.sort(key=lambda pair: pair[0])
    return entries


def compute_identity(src_dir: Path, engine_version: str = ENGINE_VERSION) -> str:
    """The 16-hex digest of one ``src`` tree. Pure — no cache, no globals.

    Separated from :func:`engine_identity` so the sensitivity tests can hash a
    COPY of the tree with one byte changed. Nothing in production calls it with
    a directory other than :data:`SRC_DIR`.

    The path, the byte length and the contents are all fed in, each
    NUL-separated: length-prefixing is what stops two files whose contents run
    together from colliding with one file holding the concatenation (the classic
    unframed-hash defect), and hashing the path is what makes a pure RENAME —
    same bytes, different module — a different engine, which it is.
    """
    digest = hashlib.sha256()
    digest.update(DIGEST_SCHEME.encode("utf-8") + b"\0")
    digest.update(engine_version.encode("utf-8") + b"\0")
    for rel, path in _walk(src_dir):
        data = path.read_bytes()
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def engine_identity() -> str:
    """16 hex characters identifying the engine source this process is running.

    Cached for the life of the process. A sweep computes it once and stamps it
    on every row; the Job may ask for it repeatedly and must not re-read 69
    files each time. The tree cannot change under a running process in any
    deployment that exists (a container image is immutable, and a dev machine
    editing mid-sweep would be measuring two engines anyway), so a stale cache
    is not a hazard the code has to defend against.
    """
    global _CACHED
    if _CACHED is None:
        _CACHED = compute_identity(SRC_DIR)
    return _CACHED


def identity_manifest(src_dir: Optional[Path] = None) -> Dict[str, int]:
    """``{relative path: byte length}`` for everything in the digest.

    Diagnostics only, and deliberately not part of the digest: when two
    environments disagree on the identity, the first question is always "which
    file differs?", and answering it by hand means re-deriving the walk rules.
    """
    return {rel: path.stat().st_size for rel, path in _walk(src_dir or SRC_DIR)}


if __name__ == "__main__":
    # `python src/backtesting/scenarios/engine_identity.py` — the form
    # `cloudbuild.yaml` uses to stamp the dashboard image.
    #
    # Running the FILE rather than importing `src.backtesting.scenarios.…` is
    # load-bearing: that package's `__init__.py` imports the runner, which
    # imports pandas and the Alpaca SDK, so the import form needs the engine's
    # whole dependency set installed. Executing the file as `__main__` runs the
    # same bytes with nothing but the standard library, which is what lets the
    # build step be a bare `python:3.11-slim`. Pinned by
    # `tests/test_cloudbuild_contract.py`.
    sys.stdout.write(engine_identity() + "\n")
