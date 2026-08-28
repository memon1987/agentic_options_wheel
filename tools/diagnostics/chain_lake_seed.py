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
`roles/storage.objectAdmin`.

Safety
------
Skip-if-exists is the default. An object in the lake is either identical to the
local file or built from a *wider* request than the local one (the lake is
shared across machines), and blindly overwriting a wider file with a narrower
one would turn cache hits into misses for everyone. `--force` overrides that
and is the only mode that can lose coverage; use it deliberately.
"""

from __future__ import annotations

import argparse
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
)


@dataclass
class SeedCounts:
    """What the run did. Every scanned file lands in exactly one bucket."""

    scanned: int = 0
    uploaded: int = 0
    skipped_existing: int = 0
    failed: int = 0
    unparseable: int = 0
    errors: List[str] = field(default_factory=list)

    def as_line(self) -> str:
        return (
            f"scanned={self.scanned} uploaded={self.uploaded} "
            f"skipped_existing={self.skipped_existing} "
            f"unparseable={self.unparseable} failed={self.failed}"
        )


def _as_of(path: Path) -> Optional[date]:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


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
        try:
            if not force and lake.exists(underlying, as_of):
                counts.skipped_existing += 1
            elif dry_run:
                counts.uploaded += 1  # what a real run would have uploaded
            else:
                lake.upload(path, underlying, as_of)
                counts.uploaded += 1
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the seed
            counts.failed += 1
            counts.errors.append(f"{underlying} {as_of.isoformat()}: {exc}")
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

    lake = ChainLake(args.bucket, args.prefix)
    print(f"Seeding gs://{args.bucket}/{lake.prefix} from {root}"
          f"{' (DRY RUN)' if args.dry_run else ''}"
          f"{' (FORCE)' if args.force else ''}")
    counts = seed(args.cache_dir, lake, force=args.force,
                  dry_run=args.dry_run, progress_every=250)
    print(counts.as_line())
    if counts.unparseable:
        print(f"note: {counts.unparseable} file(s) skipped — filename is not a date")
    for err in counts.errors[:20]:
        print(f"  FAILED {err}", file=sys.stderr)
    if len(counts.errors) > 20:
        print(f"  ... and {len(counts.errors) - 20} more", file=sys.stderr)
    return 1 if counts.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
