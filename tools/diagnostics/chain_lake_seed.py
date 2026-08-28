#!/usr/bin/env python3
"""FC-060 Layer 1 — seed the GCS chain lake from a local chain cache.

Purpose
-------
`ChainStore` mirrors chains to the lake write-through, so the lake fills itself
from here on. It does **not** back-fill: every chain already sitting in a local
`cache/backtest/chains` tree was built before the lake existed and would only
reach GCS by being re-fetched from Alpaca. This tool uploads that existing tree
instead — that is how ~2 years of history becomes the lake's day-one content
rather than a re-fetch we may not be entitled to make later (vendor retention,
tier limits, rate limits).

It is a one-shot bootstrap, kept in the tree because it is exactly what is
needed again after any local cache rebuild, and because a second machine's
cache can be folded into the lake the same way.

How to re-run
-------------
    python tools/diagnostics/chain_lake_seed.py \
        --cache-dir cache/backtest/chains \
        --bucket options-wheel-chain-lake

    # preview without writing anything
    python tools/diagnostics/chain_lake_seed.py --bucket <b> --dry-run

    # re-upload objects that already exist (e.g. after fixing a bad batch)
    python tools/diagnostics/chain_lake_seed.py --bucket <b> --force

Needs write access to the bucket (ADC or GOOGLE_APPLICATION_CREDENTIALS) —
`roles/storage.objectAdmin` on the bucket, and nothing bucket-level: the lake
only ever lists, reads and writes *objects*. If the lake reports itself
unusable the run aborts immediately (exit 2) rather than reporting one failure
per file.

Safety
------
Skip-if-exists is the default, and the report distinguishes the two kinds of
skip by comparing the local file's MD5 with the object's:

  * `skipped_identical` — same bytes; nothing to do.
  * `skipped_differs`  — the object was built from a *different* request. It may
    well be WIDER than the local file (the lake is shared across machines), and
    overwriting it would turn cache hits into misses for everyone. These are
    listed so a human can look before deciding.

`--force` uploads regardless and is the only mode here that can lose coverage.
It exists for one job: replacing an object known to be bad (unreadable parquet),
which nothing else in the system will ever do — `ChainStore` deliberately never
deletes a lake object.

Every upload carries an `if_generation_match` precondition captured at the
metadata read, so two people seeding at once cannot silently clobber each other;
a lost race is reported as `failed`, not as a successful upload.

Note `--dry-run` still issues **one metadata RPC per file** (that is how it can
report identical-vs-differs), so it needs credentials and is not free — it just
never writes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

# Import the real store's lake so the object layout can never drift from the
# one the engine reads.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.backtesting.data.chain_store import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    DEFAULT_LAKE_PREFIX,
    ChainLake,
    ChainLakeUnavailable,
)


@dataclass
class SeedCounts:
    """What the run did. Every scanned file lands in exactly one bucket."""

    scanned: int = 0
    uploaded: int = 0
    skipped_identical: int = 0
    skipped_differs: int = 0
    failed: int = 0
    unparseable: int = 0
    errors: List[str] = field(default_factory=list)
    differs: List[str] = field(default_factory=list)

    @property
    def skipped_existing(self) -> int:
        return self.skipped_identical + self.skipped_differs

    def as_line(self) -> str:
        return (
            f"scanned={self.scanned} uploaded={self.uploaded} "
            f"skipped_identical={self.skipped_identical} "
            f"skipped_differs={self.skipped_differs} "
            f"unparseable={self.unparseable} failed={self.failed}"
        )


def _as_of(path: Path) -> Optional[date]:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def local_md5(path: Path) -> str:
    """Base64 MD5 of a local file, in the same encoding GCS reports."""
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def discover(cache_dir) -> List[Tuple[str, date, Path]]:
    """List ``(underlying, as_of, path)`` for every chain file under a cache.

    Mirrors ``ChainStore._path``: ``<cache_dir>/<UNDERLYING>/<YYYY-MM-DD>.parquet``.
    Temp files from an interrupted write (``*.tmp``) are not chains and are
    excluded by the ``.parquet`` suffix; a ``.parquet`` whose stem is not a date
    is not one either, and is dropped here and counted separately by ``seed``.
    """
    root = Path(cache_dir)
    found = []
    for path in sorted(root.glob("*/*.parquet")):
        as_of = _as_of(path)
        if as_of is not None:
            found.append((path.parent.name.upper(), as_of, path))
    return found


def seed(
    cache_dir,
    lake,
    *,
    force: bool = False,
    dry_run: bool = False,
    progress_every: int = 0,
) -> SeedCounts:
    """Upload every chain under ``cache_dir`` to ``lake``.

    Skips objects that already exist unless ``force``. One file's failure is
    counted and the run continues: a partial seed is useful (the write-through
    store fills in the rest on the next run), an aborted one is not.
    """
    counts = SeedCounts()
    root = Path(cache_dir)
    counts.unparseable = sum(1 for p in root.glob("*/*.parquet") if _as_of(p) is None)
    for i, (underlying, as_of, path) in enumerate(discover(cache_dir), start=1):
        counts.scanned += 1
        label = f"{underlying} {as_of.isoformat()}"
        try:
            existing = lake.stat(underlying, as_of)
            if existing is not None and not force:
                if existing.md5_hash and existing.md5_hash == local_md5(path):
                    counts.skipped_identical += 1
                else:
                    # Different bytes. It may be WIDER than what we hold, and
                    # this tool will not gamble on that — report and move on.
                    counts.skipped_differs += 1
                    counts.differs.append(label)
                continue
            if dry_run:
                counts.uploaded += 1  # what a real run would have uploaded
                continue
            lake.upload(
                path,
                underlying,
                as_of,
                if_generation_match=(0 if existing is None else existing.generation),
            )
            counts.uploaded += 1
        except ChainLakeUnavailable:
            # The lake itself is unusable — bad credentials, wrong bucket, no
            # permission. Every remaining file would fail identically, so
            # reporting 5,400 failures instead of one diagnosis is noise.
            raise
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the seed
            counts.failed += 1
            counts.errors.append(f"{label}: {type(exc).__name__}: {exc}")
        if progress_every and i % progress_every == 0:
            print(f"  ... {counts.as_line()}", flush=True)
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                    help=f"local chain cache root (default: {DEFAULT_CACHE_DIR})")
    ap.add_argument("--bucket", required=True, help="chain-lake GCS bucket name")
    ap.add_argument("--prefix", default=DEFAULT_LAKE_PREFIX,
                    help=f"object-name prefix (default: {DEFAULT_LAKE_PREFIX})")
    ap.add_argument("--force", action="store_true",
                    help="re-upload objects that already exist (see Safety)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be uploaded; write nothing")
    args = ap.parse_args(argv)

    root = Path(args.cache_dir)
    if not root.is_dir():
        print(f"No such cache dir: {root}", file=sys.stderr)
        return 2

    try:
        lake = ChainLake(args.bucket, args.prefix)
    except ValueError as exc:
        print(f"Bad lake configuration: {exc}", file=sys.stderr)
        return 2
    print(f"Seeding gs://{args.bucket}/{lake.prefix} from {root}"
          f"{' (DRY RUN)' if args.dry_run else ''}"
          f"{' (FORCE)' if args.force else ''}")
    try:
        counts = seed(args.cache_dir, lake, force=args.force,
                      dry_run=args.dry_run, progress_every=250)
    except ChainLakeUnavailable as exc:
        print(f"Chain lake unusable ({exc.reason}): {exc}", file=sys.stderr)
        print("Nothing was uploaded. Check the bucket name and that this "
              "identity has roles/storage.objectAdmin on it.", file=sys.stderr)
        return 2
    print(counts.as_line())
    if counts.unparseable:
        print(f"note: {counts.unparseable} file(s) skipped — filename is not a date")
    if counts.differs:
        print(f"note: {len(counts.differs)} object(s) already in the lake differ "
              f"from the local file and were NOT overwritten:")
        for label in counts.differs[:20]:
            print(f"  DIFFERS {label}")
        if len(counts.differs) > 20:
            print(f"  ... and {len(counts.differs) - 20} more")
    for err in counts.errors[:20]:
        print(f"  FAILED {err}", file=sys.stderr)
    if len(counts.errors) > 20:
        print(f"  ... and {len(counts.errors) - 20} more", file=sys.stderr)
    return 1 if counts.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
