"""FC-060 Layer 1 — the GCS chain lake behind ChainStore.

Nothing here may construct a real ``google.cloud.storage.Client``. Two kinds of
double are used, deliberately:

* ``FakeLake`` — a subclass of the real ``ChainLake`` with only the three I/O
  methods overridden. Inheriting means the fake cannot drift from the real
  object layout, the circuit breaker or the disable/log path, which is where a
  hand-rolled stand-in would have quietly diverged.
* ``FakeClient`` / ``FakeBucket`` / ``FakeBlob`` — injected through
  ``_storage_client`` so the *real* ``ChainLake`` methods (retry/timeout kwargs,
  NotFound handling, the temp-file dance, the precondition) are themselves under
  test rather than assumed.

The properties, grouped by the review finding that demanded them:

L1  a reader-side failure never deletes anything in the lake
L2  an upload may only ever widen coverage; generations guard the race
L3  credentials failure and repeated errors switch the lake off, once
L4  a bucket that is not there is a loud disable, not a silent empty lake
L5  a miss is a NotFound, not an exists() probe
L6  the real ChainLake against a fake client
L7  (tests/conftest.py) the suite can never see a developer's CHAIN_LAKE_BUCKET
L8  counters distinguish miss / rejected / skipped / error
"""

import base64
import dataclasses
import hashlib
import io
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import structlog
from google.api_core.exceptions import Forbidden, NotFound, PreconditionFailed

from src.backtesting.data import chain_store as chain_store_module
from src.backtesting.data.chain_builder import ChainQuote, ChainSnapshot
from src.backtesting.data.chain_store import (
    DEFAULT_LAKE_PREFIX,
    MAX_CONSECUTIVE_LAKE_ERRORS,
    ChainLake,
    ChainLakePreconditionFailed,
    ChainLakeUnavailable,
    ChainStore,
)

AS_OF = date(2025, 1, 6)
EXP = date(2025, 1, 10)
WIDE = dict(universe_dte=14, strike_gte=80.0, strike_lte=225.0, model="m1")
NARROW = dict(universe_dte=7, strike_gte=90.0, strike_lte=110.0, model="m1")


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #
class FakeLake(ChainLake):
    """In-memory lake. Real object naming, real breaker, fake I/O."""

    def __init__(self, bucket="fake-bucket", prefix=DEFAULT_LAKE_PREFIX):
        super().__init__(bucket, prefix)
        self.objects = {}          # name -> (bytes, generation)
        self.calls = []            # (op, underlying, as_of)
        self.raise_on = set()      # ops that should blow up
        self._next_generation = 1

    # -- helpers ------------------------------------------------------------
    def ops(self):
        return [c[0] for c in self.calls]

    def _record(self, op, underlying, as_of):
        # The real class refuses at `_bucket`; the fake never gets there, so it
        # calls the same inherited guard rather than reimplementing it.
        self._ensure_usable()
        self.calls.append((op, underlying.upper(), as_of))
        if op in self.raise_on:
            raise RuntimeError(f"fake lake failure on {op}")

    def put_object(self, underlying, as_of, payload: bytes):
        """Seed an object out-of-band (no call recorded)."""
        name = self.object_name(underlying, as_of)
        self.objects[name] = (payload, self._next_generation)
        self._next_generation += 1
        return self.objects[name][1]

    def bytes_of(self, underlying, as_of):
        entry = self.objects.get(self.object_name(underlying, as_of))
        return None if entry is None else entry[0]

    def generation_of(self, underlying, as_of):
        entry = self.objects.get(self.object_name(underlying, as_of))
        return None if entry is None else entry[1]

    # -- the three operations ----------------------------------------------
    def stat(self, underlying, as_of):
        self._record("stat", underlying, as_of)
        entry = self.objects.get(self.object_name(underlying, as_of))
        if entry is None:
            return None
        return chain_store_module.LakeObject(
            generation=entry[1], md5_hash=_md5(entry[0])
        )

    def download(self, underlying, as_of, local_path):
        self._record("download", underlying, as_of)
        entry = self.objects.get(self.object_name(underlying, as_of))
        if entry is None:
            return None
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(entry[0])
        return chain_store_module.LakeObject(
            generation=entry[1], md5_hash=_md5(entry[0])
        )

    def upload(self, local_path, underlying, as_of, *, if_generation_match):
        self._record("upload", underlying, as_of)
        name = self.object_name(underlying, as_of)
        current = self.objects.get(name)
        live = None if current is None else current[1]
        wanted = if_generation_match
        if wanted is not None and (wanted or None) != live:
            raise ChainLakePreconditionFailed(
                f"generation {wanted} != {live}"
            )
        self.objects[name] = (Path(local_path).read_bytes(), self._next_generation)
        self._next_generation += 1


def _md5(payload: bytes) -> str:
    return base64.b64encode(hashlib.md5(payload).digest()).decode("ascii")


class FakeBlob:
    def __init__(self, bucket, name):
        self._bucket = bucket
        self.name = name
        self.generation = None
        self.md5_hash = None
        self.kwargs = {}

    def _entry(self):
        return self._bucket.objects.get(self.name)

    def download_to_filename(self, filename, **kwargs):
        self.kwargs["download"] = kwargs
        self._bucket.calls.append(("download", self.name))
        if self._bucket.download_raises is not None:
            # Write a partial file first: the point of the temp-file dance is
            # that a half-written download is never visible as a cache file.
            Path(filename).write_bytes(b"half a par")
            raise self._bucket.download_raises
        entry = self._entry()
        if entry is None:
            raise NotFound(self.name)
        Path(filename).write_bytes(entry[0])
        self.generation = entry[1]
        self.md5_hash = _md5(entry[0])

    def upload_from_filename(self, filename, **kwargs):
        self.kwargs["upload"] = kwargs
        self._bucket.calls.append(("upload", self.name))
        entry = self._entry()
        live = None if entry is None else entry[1]
        wanted = kwargs.get("if_generation_match")
        if wanted is not None and (wanted or None) != live:
            raise PreconditionFailed(f"generation {wanted} != {live}")
        gen = self._bucket.next_generation()
        self._bucket.objects[self.name] = (Path(filename).read_bytes(), gen)
        self.generation = gen


class FakeBucket:
    def __init__(self, name, present=True, list_raises=None,
                 download_raises=None):
        self.name = name
        self.present = present
        self.list_raises = list_raises
        self.download_raises = download_raises
        self.objects = {}
        self.calls = []
        self._gen = 100

    def next_generation(self):
        self._gen += 1
        return self._gen

    def exists(self, **kwargs):
        # `Bucket.exists()` is GET /b/<bucket> and needs storage.buckets.get,
        # which roles/storage.objectAdmin does NOT grant. Under the IAM this
        # lake is designed to run with, calling it is a 403 — so the fake makes
        # it one, and any code path that reaches for it fails loudly here
        # instead of silently disabling the lake in production.
        self.calls.append(("bucket_exists", self.name))
        raise Forbidden("storage.buckets.get denied on " + self.name)

    def blob(self, name):
        return FakeBlob(self, name)

    def get_blob(self, name, **kwargs):
        self.calls.append(("get_blob", name))
        entry = self.objects.get(name)
        if entry is None:
            return None
        blob = FakeBlob(self, name)
        blob.generation = entry[1]
        blob.md5_hash = _md5(entry[0])
        return blob


class FakeClient:
    def __init__(self, bucket: FakeBucket):
        self._bucket = bucket
        self.bucket_calls = 0

    def bucket(self, name):
        self.bucket_calls += 1
        return self._bucket

    def list_blobs(self, bucket, prefix=None, max_results=None, **kwargs):
        """The startup probe: storage.objects.list, which objectAdmin grants."""
        bucket.calls.append(("list_blobs", prefix))
        if bucket.list_raises is not None:
            raise bucket.list_raises
        if not bucket.present:
            raise NotFound(bucket.name)
        names = [n for n in bucket.objects if prefix is None or n.startswith(prefix)]
        return iter(names[: max_results or len(names)])


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _snapshot(strikes=(90.0, 95.0, 100.0), price=100.0, underlying="XYZ",
              as_of=AS_OF):
    puts = [
        ChainQuote(
            symbol=f"{underlying}{as_of:%y%m%d}P{int(k * 1000):08d}",
            underlying=underlying, as_of=as_of, expiration=EXP, strike=k,
            option_type="put", dte=(EXP - as_of).days, underlying_price=price,
            mark=1.0, bid=0.9, ask=1.1, implied_volatility=0.3, delta=-0.2,
            volume=10, modeled_spread=True, modeled_greeks=True,
        )
        for k in strikes
    ]
    return ChainSnapshot(underlying, as_of, price, puts, [])


@pytest.fixture
def captured_events():
    seen = []

    def capture(_logger, _name, event_dict):
        seen.append(dict(event_dict))
        return event_dict

    prev = structlog.get_config()
    structlog.configure(processors=[capture] + list(prev.get("processors", [])))
    try:
        yield seen
    finally:
        structlog.configure(**prev)


@pytest.fixture(autouse=True)
def _forbid_real_gcs_client(monkeypatch):
    """No test in this file may construct a real GCS client."""
    def _boom():
        raise AssertionError("a real google.cloud.storage.Client was constructed")

    monkeypatch.setattr(chain_store_module, "_storage_client", _boom)


def _events(captured, event_type):
    return [e for e in captured if e.get("event_type") == event_type]


def _seed_lake(tmp_path, lake, window=None, snapshot=None, sub="donor"):
    """Write one snapshot through a throwaway store so the lake holds real bytes."""
    store = ChainStore(str(tmp_path / sub), lake=lake)
    store.put(snapshot or _snapshot(), **(window or {}))
    return store


# --------------------------------------------------------------------------- #
# 1. No lake configured
# --------------------------------------------------------------------------- #
class TestNoLakeConfigured:
    def test_from_env_without_bucket_has_no_lake_and_makes_no_lake_call(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("CHAIN_LAKE_BUCKET", raising=False)
        store = ChainStore.from_env(str(tmp_path))
        assert store.lake is None
        assert store.get("XYZ", AS_OF) is None
        assert store.summary()["lake_enabled"] is False

    def test_empty_bucket_env_is_treated_as_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAIN_LAKE_BUCKET", "   ")
        assert ChainStore.from_env(str(tmp_path)).lake is None
        assert ChainStore.lake_from_env() is None

    def test_a_store_without_a_lake_never_emits_a_lake_event(
        self, tmp_path, captured_events
    ):
        store = ChainStore(str(tmp_path))
        store.put(_snapshot())
        assert store.get("XYZ", AS_OF) is not None
        assert not [e for e in captured_events
                    if str(e.get("event_type", "")).startswith("chain_lake")]


# --------------------------------------------------------------------------- #
# 2/3. Read-through
# --------------------------------------------------------------------------- #
class TestReadThrough:
    def test_local_miss_with_a_lake_hit_is_served_and_counted(self, tmp_path):
        lake = FakeLake()
        _seed_lake(tmp_path, lake)

        store = ChainStore(str(tmp_path / "cold"), lake=lake)
        assert not store.has("XYZ", AS_OF)

        snap = store.get("XYZ", AS_OF)
        assert snap is not None
        assert [q.strike for q in snap.puts] == [90.0, 95.0, 100.0]
        assert store.lake_hits == 1 and store.lake_misses == 0
        assert store.has("XYZ", AS_OF)

        before = list(lake.ops())
        assert store.get("XYZ", AS_OF) is not None
        assert store.lake_hits == 1, "second read must be a local hit"
        assert lake.ops() == before, "second read must not touch the lake"

    def test_local_miss_and_lake_miss_is_a_plain_miss(self, tmp_path):
        lake = FakeLake()
        store = ChainStore(str(tmp_path), lake=lake)
        assert store.get("XYZ", AS_OF) is None
        assert store.lake_misses == 1
        assert (store.lake_hits, store.lake_errors) == (0, 0)
        assert lake.ops() == ["download"], "a miss is one RPC, not exists+download"

    def test_a_downloaded_file_is_byte_identical_to_the_source(self, tmp_path):
        lake = FakeLake()
        _seed_lake(tmp_path, lake)
        source = (tmp_path / "donor" / "XYZ" / "2025-01-06.parquet").read_bytes()
        store = ChainStore(str(tmp_path / "cold"), lake=lake)
        store.get("XYZ", AS_OF)
        assert (tmp_path / "cold" / "XYZ" / "2025-01-06.parquet").read_bytes() == source


# --------------------------------------------------------------------------- #
# 4. Write-through
# --------------------------------------------------------------------------- #
class TestWriteThrough:
    def test_put_writes_local_and_uploads_identical_bytes(self, tmp_path):
        lake = FakeLake()
        store = ChainStore(str(tmp_path), lake=lake)
        store.put(_snapshot(), **NARROW)

        local = tmp_path / "XYZ" / "2025-01-06.parquet"
        assert local.exists()
        assert lake.bytes_of("XYZ", AS_OF) == local.read_bytes()
        assert store.lake_puts == 1 and store.lake_errors == 0

    def test_a_new_object_is_created_with_a_create_only_precondition(self, tmp_path):
        """if_generation_match=0 — two cold machines cannot both 'create' it."""
        lake = FakeLake()
        store = ChainStore(str(tmp_path), lake=lake)
        lake.put_object("XYZ", AS_OF, b"someone got there first")
        # The store has not looked at the lake for this key, so it stats first
        # and finds the object -> this is the overwrite path, not create.
        store.put(_snapshot(), **NARROW)
        assert "stat" in lake.ops()

    def test_upload_carries_the_provenance_a_later_coverage_check_needs(self, tmp_path):
        lake = FakeLake()
        ChainStore(str(tmp_path / "a"), lake=lake).put(_snapshot(), **WIDE)
        cold = ChainStore(str(tmp_path / "b"), lake=lake)
        got = cold.get("XYZ", AS_OF, universe_dte=7, strike_gte=85.0,
                       strike_lte=115.0, underlying_price=100.0, model="m1")
        assert got is not None and cold.lake_hits == 1

    def test_no_temp_files_survive_a_put(self, tmp_path):
        lake = FakeLake()
        ChainStore(str(tmp_path), lake=lake).put(_snapshot())
        assert not list((tmp_path / "XYZ").glob("*.tmp"))


# --------------------------------------------------------------------------- #
# L2. Coverage-monotone overwrites
# --------------------------------------------------------------------------- #
class TestOverwriteIsCoverageMonotone:
    """An upload may only ever widen what the lake holds.

    The strike window is path-dependent — `cost_basis` and `low_anchor` move it,
    so the bid pass of `evaluate_symbol` legitimately rebuilds the same session
    under a different window than the mid pass, and a second machine holding
    different positions gets a third. Without this rule, the last writer wins
    and a narrower file silently destroys coverage every other run depends on.
    """

    def test_a_narrower_rebuild_does_not_overwrite_the_wider_object(
        self, tmp_path, captured_events
    ):
        lake = FakeLake()
        _seed_lake(tmp_path, lake, WIDE)
        wide_bytes = lake.bytes_of("XYZ", AS_OF)
        wide_gen = lake.generation_of("XYZ", AS_OF)

        store = ChainStore(str(tmp_path / "narrow"), lake=lake)
        # Same DTE reach, narrower strikes — isolates the strike-window rule
        # from the DTE rule (which has its own test below).
        store.put(_snapshot(), universe_dte=14, strike_gte=90.0,
                  strike_lte=110.0, model="m1")

        assert lake.bytes_of("XYZ", AS_OF) == wide_bytes, "the wide object survived"
        assert lake.generation_of("XYZ", AS_OF) == wide_gen
        assert store.lake_skipped == 1 and store.lake_puts == 0
        assert store.lake_errors == 0, "a refused narrowing is not an error"
        skipped = _events(captured_events, "chain_lake_overwrite_skipped")
        assert len(skipped) == 1
        assert skipped[0]["reason"] == "narrower_strikes"
        assert skipped[0]["new_window"]["strike_lte"] == 110.0
        assert skipped[0]["existing_window"]["strike_lte"] == 225.0

    def test_the_local_file_is_written_even_when_the_mirror_is_skipped(self, tmp_path):
        """Local behaviour is unchanged by any lake decision."""
        lake = FakeLake()
        _seed_lake(tmp_path, lake, WIDE)
        store = ChainStore(str(tmp_path / "narrow"), lake=lake)
        store.put(_snapshot(), **NARROW)

        local = tmp_path / "narrow" / "XYZ" / "2025-01-06.parquet"
        assert local.exists()
        got = store.get("XYZ", AS_OF, universe_dte=7, strike_gte=90.0,
                        strike_lte=110.0, underlying_price=100.0, model="m1")
        assert got is not None, "the local cache still answers its own request"

    def test_a_shorter_dte_reach_does_not_overwrite(self, tmp_path, captured_events):
        lake = FakeLake()
        _seed_lake(tmp_path, lake, WIDE)
        store = ChainStore(str(tmp_path / "s"), lake=lake)
        store.put(_snapshot(), universe_dte=7, strike_gte=80.0,
                  strike_lte=225.0, model="m1")
        assert store.lake_skipped == 1
        assert _events(captured_events, "chain_lake_overwrite_skipped")[0][
            "reason"] == "narrower_dte"

    def test_a_wider_rebuild_does_overwrite(self, tmp_path):
        lake = FakeLake()
        _seed_lake(tmp_path, lake, NARROW)
        narrow_gen = lake.generation_of("XYZ", AS_OF)

        store = ChainStore(str(tmp_path / "wide"), lake=lake)
        store.put(_snapshot(strikes=(85.0, 95.0, 200.0)), **WIDE)

        assert store.lake_puts == 1 and store.lake_skipped == 0
        assert lake.generation_of("XYZ", AS_OF) != narrow_gen
        assert lake.bytes_of("XYZ", AS_OF) == (
            tmp_path / "wide" / "XYZ" / "2025-01-06.parquet"
        ).read_bytes()

    def test_an_identical_window_uploads_idempotently(self, tmp_path):
        """Equal coverage is a superset of itself; a refresh must not be blocked."""
        lake = FakeLake()
        _seed_lake(tmp_path, lake, NARROW)
        store = ChainStore(str(tmp_path / "same"), lake=lake)
        store.put(_snapshot(), **NARROW)
        assert store.lake_puts == 1 and store.lake_skipped == 0

    def test_a_different_model_never_overwrites(self, tmp_path, captured_events):
        """A model change is a format change: bump the prefix, do not clobber."""
        lake = FakeLake()
        _seed_lake(tmp_path, lake, NARROW)
        store = ChainStore(str(tmp_path / "m2"), lake=lake)
        store.put(_snapshot(), universe_dte=7, strike_gte=80.0,
                  strike_lte=225.0, model="m2")
        assert store.lake_skipped == 1 and store.lake_puts == 0
        assert _events(captured_events, "chain_lake_overwrite_skipped")[0][
            "reason"] == "model_changed"

    def test_unknown_provenance_on_either_side_fails_closed(
        self, tmp_path, captured_events
    ):
        lake = FakeLake()
        _seed_lake(tmp_path, lake, NARROW)
        store = ChainStore(str(tmp_path / "u"), lake=lake)
        store.put(_snapshot())  # no provenance declared at all
        assert store.lake_puts == 0 and store.lake_skipped == 1
        assert _events(captured_events, "chain_lake_overwrite_skipped")[0][
            "reason"] in {"model_changed", "unknown_provenance"}

    def test_an_unreadable_remote_object_is_never_overwritten_blind(
        self, tmp_path, captured_events
    ):
        lake = FakeLake()
        lake.put_object("XYZ", AS_OF, b"not a parquet at all")
        store = ChainStore(str(tmp_path), lake=lake)
        store.put(_snapshot(), **WIDE)
        assert store.lake_puts == 0 and store.lake_skipped == 1
        assert store.lake_skipped_unreadable_remote == 1, (
            "a poisoned object is a different operational problem from a "
            "benign narrowing and must be countable on its own"
        )
        event = _events(captured_events, "chain_lake_overwrite_skipped")[0]
        assert event["reason"] == "remote_provenance_unknown"
        assert "--force" in event["remedy"], "the log must name the escape hatch"
        assert lake.bytes_of("XYZ", AS_OF) == b"not a parquet at all"

    def test_an_unreadable_remote_is_probed_once_not_once_per_put(self, tmp_path):
        """Otherwise a poisoned object costs a stat + a download on every put."""
        lake = FakeLake()
        lake.put_object("XYZ", AS_OF, b"not a parquet at all")
        store = ChainStore(str(tmp_path), lake=lake)

        store.put(_snapshot(), **WIDE)
        after_first = list(lake.ops())
        for _ in range(4):
            store.put(_snapshot(), **WIDE)

        assert lake.ops() == after_first, "the refusal must be remembered"
        assert store.lake_skipped == 5
        assert store.lake_skipped_unreadable_remote == 5

    def test_a_benign_narrowing_does_not_count_as_unreadable(self, tmp_path):
        lake = FakeLake()
        _seed_lake(tmp_path, lake, WIDE)
        store = ChainStore(str(tmp_path / "n"), lake=lake)
        store.put(_snapshot(), **NARROW)
        assert store.lake_skipped == 1
        assert store.lake_skipped_unreadable_remote == 0

    def test_a_generation_race_skips_rather_than_clobbers(self, tmp_path):
        """Someone else wrote between our check and our upload."""
        lake = FakeLake()
        _seed_lake(tmp_path, lake, NARROW)

        store = ChainStore(str(tmp_path / "racer"), lake=lake)
        # Learn the object (download + provenance) at generation g...
        assert store.get("XYZ", AS_OF, universe_dte=7, strike_gte=90.0,
                         strike_lte=110.0, underlying_price=100.0,
                         model="m1") is not None
        # ...then another writer replaces it.
        lake.put_object("XYZ", AS_OF, b"someone else's wider file")
        winner = lake.bytes_of("XYZ", AS_OF)

        store.put(_snapshot(), **WIDE)  # widening, so coverage would allow it
        assert lake.bytes_of("XYZ", AS_OF) == winner, "the race loser must not clobber"
        assert store.lake_skipped == 1
        assert store.lake_errors == 0, "a lost race is not an error"

    def test_a_key_known_to_be_absent_skips_the_stat_rpc(self, tmp_path):
        lake = FakeLake()
        store = ChainStore(str(tmp_path), lake=lake)
        assert store.get("XYZ", AS_OF) is None  # download -> miss, remembered
        store.put(_snapshot(), **NARROW)
        assert lake.ops() == ["download", "upload"], "no redundant stat"
        assert store.lake_puts == 1


# --------------------------------------------------------------------------- #
# L1. A reader-side failure must never destroy history
# --------------------------------------------------------------------------- #
class TestCorruptFileNeverDeletesTheLake:
    """`except Exception` around read_parquet is not a corruption detector.

    It fires on a pyarrow version skew, a MemoryError, an exhausted
    file-descriptor limit — process-local conditions with nothing to say about
    the bytes in GCS. Deleting the object on that signal would destroy chains
    that Alpaca may no longer serve, which is the whole reason the lake exists.
    """

    def test_the_lake_has_no_delete_at_all(self):
        assert not hasattr(ChainLake, "delete")

    def test_an_unreadable_local_file_leaves_the_lake_untouched(
        self, tmp_path, captured_events
    ):
        lake = FakeLake()
        store = ChainStore(str(tmp_path), lake=lake)
        store.put(_snapshot(), **NARROW)
        assert lake.bytes_of("XYZ", AS_OF) is not None

        path = tmp_path / "XYZ" / "2025-01-06.parquet"
        path.write_bytes(b"not a parquet file")
        before = list(lake.ops())

        assert store.get("XYZ", AS_OF) is None
        assert not path.exists(), "the local file is still discarded"
        assert lake.bytes_of("XYZ", AS_OF) is not None, "the object survives"
        assert lake.ops() == before, "no lake call at all on the corrupt path"
        assert _events(captured_events, "chain_cache_corrupt")

    def test_a_reader_side_memory_error_deletes_nothing_remotely(
        self, tmp_path, monkeypatch
    ):
        """The demonstrated failure: 5 good objects, one bad reader, 0 losses."""
        lake = FakeLake()
        days = [date(2025, 1, d) for d in range(6, 11)]
        for d in days:
            ChainStore(str(tmp_path / "seed"), lake=lake).put(
                _snapshot(as_of=d), **NARROW
            )
        assert all(lake.bytes_of("XYZ", d) is not None for d in days)

        store = ChainStore(str(tmp_path / "seed"), lake=lake)
        monkeypatch.setattr(
            chain_store_module.pd, "read_parquet",
            lambda *a, **k: (_ for _ in ()).throw(MemoryError("out of memory")),
        )
        for d in days:
            assert store.get("XYZ", d) is None

        assert all(lake.bytes_of("XYZ", d) is not None for d in days), (
            "a reader-side failure must not cost a single object"
        )


# --------------------------------------------------------------------------- #
# Coverage still governs a lake-sourced file
# --------------------------------------------------------------------------- #
class TestCoverageAppliesToLakeFiles:
    """A lake file is a cache file. It gets the same interrogation.

    This is the correctness property of the whole feature: the lake moves
    bytes, so a file served from it must satisfy ``_covers`` and be narrowed
    exactly as a local file would be. Skipping that check would make a warm
    (lake) run silently different from a cold one — a wrong backtest, not a
    slow one.
    """

    def _seed_wide(self, tmp_path, lake):
        _seed_lake(tmp_path, lake, WIDE,
                   snapshot=_snapshot(strikes=(85.0, 95.0, 100.0, 200.0)))

    def test_a_narrower_request_is_served_from_the_lake_and_narrowed(self, tmp_path):
        lake = FakeLake()
        self._seed_wide(tmp_path, lake)
        store = ChainStore(str(tmp_path / "cold"), lake=lake)
        got = store.get("XYZ", AS_OF, universe_dte=7, strike_gte=90.0,
                        strike_lte=110.0, underlying_price=100.0, model="m1")
        assert got is not None and store.lake_hits == 1
        assert [q.strike for q in got.puts] == [95.0, 100.0], (
            "the lake-sourced file must be narrowed on read, not served whole"
        )
        assert store.lake_rejected == 0

    def test_a_wider_request_against_a_lake_file_is_a_miss(self, tmp_path):
        lake = FakeLake()
        self._seed_wide(tmp_path, lake)
        store = ChainStore(str(tmp_path / "cold2"), lake=lake)
        got = store.get("XYZ", AS_OF, universe_dte=7, strike_gte=50.0,
                        strike_lte=300.0, underlying_price=100.0, model="m1")
        assert got is None, "an uncovered lake file must fail closed"
        assert store.lake_hits == 1, "the download happened; coverage rejected it"
        assert store.lake_rejected == 1

    def test_a_different_model_fingerprint_from_the_lake_is_a_miss(self, tmp_path):
        lake = FakeLake()
        self._seed_wide(tmp_path, lake)
        store = ChainStore(str(tmp_path / "cold3"), lake=lake)
        assert store.get("XYZ", AS_OF, universe_dte=7, underlying_price=100.0,
                         model="m2") is None
        assert store.lake_rejected == 1

    def test_a_different_underlying_close_from_the_lake_is_a_miss(self, tmp_path):
        lake = FakeLake()
        self._seed_wide(tmp_path, lake)
        store = ChainStore(str(tmp_path / "cold4"), lake=lake)
        assert store.get("XYZ", AS_OF, universe_dte=7, underlying_price=101.0,
                         model="m1") is None
        assert store.lake_rejected == 1


# --------------------------------------------------------------------------- #
# 4b. FC-091 — merge-on-put
# --------------------------------------------------------------------------- #
OLD_W = dict(universe_dte=14, strike_gte=90.0, strike_lte=110.0, model="m1")
NEW_W = dict(universe_dte=7, strike_gte=100.0, strike_lte=130.0, model="m1")
# The request the cold run makes: the lake's OLD_W file reaches high enough on
# the low bound but not on the high one, so `_covers` rejects it and neither
# window contains the other — the SPY/IWM/PFE production signature.
NEW_REQUEST = dict(universe_dte=7, strike_gte=100.0, strike_lte=130.0,
                   underlying_price=100.0, model="m1")
OLD_REQUEST = dict(universe_dte=14, strike_gte=90.0, strike_lte=110.0,
                   underlying_price=100.0, model="m1")


def _marked(mark, **kwargs):
    """A snapshot whose quotes carry a distinguishing ``mark``."""
    snap = _snapshot(**kwargs)
    puts = [dataclasses.replace(q, mark=mark) for q in snap.puts]
    return ChainSnapshot(snap.underlying, snap.as_of, snap.underlying_price,
                         puts, [])


def _lake_frame(lake, underlying="XYZ", as_of=AS_OF):
    """The object as it now stands in the lake, parsed back from its bytes."""
    payload = lake.bytes_of(underlying, as_of)
    assert payload is not None
    return pd.read_parquet(io.BytesIO(payload))


def _provenance(df):
    row = df.iloc[0]
    return (float(row["universe_dte"]), float(row["strike_gte"]),
            float(row["strike_lte"]), str(row["model"]))


def _thrash(tmp_path, lake, old_window=None, old_snapshot=None, sub="cold"):
    """Reproduce the thrash: seed the lake, then reject its object on a get.

    Returns the cold store, primed with the rejected frame, ready for the
    rebuild's ``put``.
    """
    _seed_lake(tmp_path, lake, old_window or OLD_W,
               snapshot=old_snapshot if old_snapshot is not None
               else _marked(1.0, strikes=(90.0, 95.0, 100.0)))
    store = ChainStore(str(tmp_path / sub), lake=lake)
    assert store.get("XYZ", AS_OF, **NEW_REQUEST) is None
    assert store.lake_hits == 1 and store.lake_rejected == 1
    return store


class TestMergeOnPut:
    """FC-091. A window that thrashes must heal, not stay cold forever.

    The coverage-monotone guard (Layer 1) refuses a narrowing, which is right,
    but a window can move so that the rebuild is wider on one bound and
    narrower on the other. Then neither file covers the other, every upload is
    refused, and the symbol is re-fetched cold on every monthly run — observed
    in production on SPY, IWM and PFE (`rejected=231 skipped=231` apiece). The
    union is the missing operation.
    """

    # -- test 1 ------------------------------------------------------------
    def test_overlapping_windows_merge_into_the_union(self, tmp_path,
                                                      captured_events):
        lake = FakeLake()
        old_gen = lake.generation_of("XYZ", AS_OF)
        store = _thrash(tmp_path, lake)
        old_gen = lake.generation_of("XYZ", AS_OF)

        store.put(_marked(2.0, strikes=(100.0, 105.0, 130.0)), **NEW_W)

        assert store.lake_merged == 1
        assert store.lake_puts == 1, "the merged file is a superset; it uploads"
        assert store.lake_skipped == 0 and store.lake_merge_gaps == 0
        assert lake.generation_of("XYZ", AS_OF) != old_gen

        merged = _lake_frame(lake)
        assert sorted(merged["strike"]) == [90.0, 95.0, 100.0, 105.0, 130.0]
        # New build wins the collision at strike 100 — same session, same
        # model, but produced by this process under this code.
        by_strike = dict(zip(merged["strike"], merged["mark"]))
        assert by_strike[100.0] == 2.0
        assert by_strike[90.0] == 1.0 and by_strike[130.0] == 2.0
        # dte is a reach, so its union is the longer one, not the new one.
        assert _provenance(merged) == (14.0, 90.0, 130.0, "m1")
        assert merged["universe_dte"].nunique() == 1, (
            "provenance is a property of the file; every row carries the union"
        )

        event = _events(captured_events, "chain_lake_merged")
        assert len(event) == 1
        assert event[0]["merged_window"]["strike_gte"] == 90.0
        assert event[0]["merged_window"]["strike_lte"] == 130.0
        assert event[0]["merged_rows"] == 5 and event[0]["new_rows"] == 3

    def test_the_local_file_is_the_merged_file_too(self, tmp_path):
        """Local and remote must not diverge: the lake mirrors what is on disk."""
        lake = FakeLake()
        store = _thrash(tmp_path, lake)
        store.put(_marked(2.0, strikes=(100.0, 105.0, 130.0)), **NEW_W)
        local = (tmp_path / "cold" / "XYZ" / "2025-01-06.parquet")
        assert local.read_bytes() == lake.bytes_of("XYZ", AS_OF)

    def test_the_merge_uses_the_generation_captured_on_the_download(self, tmp_path):
        """No re-stat, no re-download: the read already learned the generation."""
        lake = FakeLake()
        store = _thrash(tmp_path, lake)
        before = list(lake.ops())
        store.put(_marked(2.0, strikes=(100.0, 105.0, 130.0)), **NEW_W)
        assert [op for op in lake.ops()[len(before):]] == ["upload"], (
            "a merge must not pay for a second probe of the object it just read"
        )
        assert store.lake_puts == 1

    # -- test 2 ------------------------------------------------------------
    def test_disjoint_windows_refuse_the_merge_and_upload_nothing(
        self, tmp_path, captured_events
    ):
        """A gap is strikes NEITHER file fetched. Never claim them."""
        lake = FakeLake()
        gap_old = dict(universe_dte=14, strike_gte=90.0, strike_lte=100.0,
                       model="m1")
        _seed_lake(tmp_path, lake, gap_old,
                   snapshot=_marked(1.0, strikes=(90.0, 95.0, 100.0)))
        old_bytes = lake.bytes_of("XYZ", AS_OF)
        old_gen = lake.generation_of("XYZ", AS_OF)

        store = ChainStore(str(tmp_path / "gap"), lake=lake)
        assert store.get("XYZ", AS_OF, universe_dte=7, strike_gte=120.0,
                         strike_lte=130.0, underlying_price=100.0,
                         model="m1") is None
        assert store.lake_rejected == 1

        before = list(lake.ops())
        store.put(_marked(2.0, strikes=(120.0, 125.0, 130.0)),
                  universe_dte=7, strike_gte=120.0, strike_lte=130.0,
                  model="m1")

        assert store.lake_merge_gaps == 1 and store.lake_merged == 0
        assert store.lake_puts == 0 and store.lake_skipped == 0
        assert store.lake_errors == 0, "a refused merge is not an error"
        assert lake.ops()[len(before):] == [], "nothing was sent to the lake"
        assert lake.bytes_of("XYZ", AS_OF) == old_bytes
        assert lake.generation_of("XYZ", AS_OF) == old_gen

        gap = _events(captured_events, "chain_lake_merge_gap")
        assert len(gap) == 1
        assert gap[0]["existing_window"]["strike_lte"] == 100.0
        assert gap[0]["new_window"]["strike_gte"] == 120.0
        assert gap[0]["symbol"] == "XYZ"

    def test_the_gap_case_still_writes_the_local_file(self, tmp_path):
        lake = FakeLake()
        _seed_lake(tmp_path, lake,
                   dict(universe_dte=14, strike_gte=90.0, strike_lte=100.0,
                        model="m1"))
        store = ChainStore(str(tmp_path / "gap2"), lake=lake)
        store.get("XYZ", AS_OF, universe_dte=7, strike_gte=120.0,
                  strike_lte=130.0, underlying_price=100.0, model="m1")
        store.put(_snapshot(strikes=(120.0, 130.0)), universe_dte=7,
                  strike_gte=120.0, strike_lte=130.0, model="m1")
        got = store.get("XYZ", AS_OF, universe_dte=7, strike_gte=120.0,
                        strike_lte=130.0, underlying_price=100.0, model="m1")
        assert got is not None and [q.strike for q in got.puts] == [120.0, 130.0]

    def test_touching_windows_are_not_a_gap(self, tmp_path):
        """`old.lte == new.gte` share a strike; that is an overlap, not a gap."""
        lake = FakeLake()
        _seed_lake(tmp_path, lake,
                   dict(universe_dte=14, strike_gte=90.0, strike_lte=100.0,
                        model="m1"),
                   snapshot=_marked(1.0, strikes=(90.0, 95.0, 100.0)))
        store = ChainStore(str(tmp_path / "touch"), lake=lake)
        store.get("XYZ", AS_OF, universe_dte=7, strike_gte=100.0,
                  strike_lte=130.0, underlying_price=100.0, model="m1")
        store.put(_marked(2.0, strikes=(100.0, 130.0)), universe_dte=7,
                  strike_gte=100.0, strike_lte=130.0, model="m1")
        assert store.lake_merged == 1 and store.lake_merge_gaps == 0
        assert _provenance(_lake_frame(lake)) == (14.0, 90.0, 130.0, "m1")

    # -- test 3 ------------------------------------------------------------
    def test_a_model_change_is_never_merged(self, tmp_path, captured_events):
        """Prices and greeks are computed; two models are two answers."""
        lake = FakeLake()
        _seed_lake(tmp_path, lake, OLD_W,
                   snapshot=_marked(1.0, strikes=(90.0, 95.0, 100.0)))
        old_bytes = lake.bytes_of("XYZ", AS_OF)
        store = ChainStore(str(tmp_path / "m2"), lake=lake)
        assert store.get("XYZ", AS_OF, universe_dte=7, strike_gte=100.0,
                         strike_lte=130.0, underlying_price=100.0,
                         model="m2") is None
        store.put(_marked(2.0, strikes=(100.0, 130.0)), universe_dte=7,
                  strike_gte=100.0, strike_lte=130.0, model="m2")

        assert store.lake_merged == 0 and store.lake_merge_gaps == 0
        assert store.lake_skipped == 1 and store.lake_puts == 0
        assert lake.bytes_of("XYZ", AS_OF) == old_bytes
        assert _events(captured_events, "chain_lake_overwrite_skipped")[0][
            "reason"] == "model_changed"

    def test_unknown_provenance_on_either_side_is_never_merged(self, tmp_path):
        """A file that cannot say what it covers cannot widen a claim."""
        lake = FakeLake()
        _seed_lake(tmp_path, lake, OLD_W)
        store = ChainStore(str(tmp_path / "u"), lake=lake)
        store.get("XYZ", AS_OF, **NEW_REQUEST)
        store.put(_snapshot(strikes=(100.0, 130.0)), model="m1")  # no bounds
        assert store.lake_merged == 0
        assert store.lake_skipped == 1 and store.lake_puts == 0

    def test_a_different_session_close_is_never_merged(self, tmp_path):
        """Every delta in a file is computed against that file's close.

        `get` checks the price only AFTER `_covers`, so a frame can be
        remembered without its price having been compared at all — the merge
        has to make that comparison itself or it will union two sessions.
        """
        lake = FakeLake()
        _seed_lake(tmp_path, lake, OLD_W,
                   snapshot=_snapshot(strikes=(90.0, 95.0, 100.0), price=100.0))
        store = ChainStore(str(tmp_path / "px"), lake=lake)
        assert store.get("XYZ", AS_OF, universe_dte=7, strike_gte=100.0,
                         strike_lte=130.0, underlying_price=101.0,
                         model="m1") is None
        store.put(_snapshot(strikes=(100.0, 130.0), price=101.0), **NEW_W)
        assert store.lake_merged == 0, "two closes cannot be unioned"

    # -- test 4 ------------------------------------------------------------
    def test_the_merged_file_answers_both_the_old_and_the_new_request(
        self, tmp_path
    ):
        """The whole point: one object, both windows, no further re-fetching."""
        lake = FakeLake()
        store = _thrash(tmp_path, lake)
        store.put(_marked(2.0, strikes=(100.0, 105.0, 130.0)), **NEW_W)

        hits_before = store.lake_hits
        ops_before = list(lake.ops())

        new_side = store.get("XYZ", AS_OF, **NEW_REQUEST)
        assert new_side is not None
        assert [q.strike for q in new_side.puts] == [100.0, 105.0, 130.0]

        old_side = store.get("XYZ", AS_OF, **OLD_REQUEST)
        assert old_side is not None, (
            "the merged file must still cover the window the lake object had"
        )
        assert [q.strike for q in old_side.puts] == [90.0, 95.0, 100.0, 105.0], (
            "MUTATION GUARD: keeping only the new rows and dropping the union "
            "leaves this request covered on paper and empty in fact"
        )

        assert store.lake_hits == hits_before, "both reads are local"
        assert lake.ops()[len(ops_before):] == []

    def test_a_reader_cannot_tell_a_merged_file_from_a_plain_one(self, tmp_path):
        """Row conversion is unchanged: merging moves rows, it never re-prices.

        A request the unmerged build could satisfy gets byte-identical quotes
        out of the merged file, because every row the union added lies outside
        that build's window and the narrowing mask drops it.
        """
        lake = FakeLake()
        store = _thrash(tmp_path, lake)
        rebuild = _marked(2.0, strikes=(100.0, 105.0, 130.0))
        store.put(rebuild, **NEW_W)
        merged = store.get("XYZ", AS_OF, **NEW_REQUEST)

        plain_store = ChainStore(str(tmp_path / "plain"))
        plain_store.put(rebuild, **NEW_W)
        plain = plain_store.get("XYZ", AS_OF, **NEW_REQUEST)

        assert merged is not None and plain is not None
        assert merged.puts == plain.puts and merged.calls == plain.calls
        assert merged.underlying_price == plain.underlying_price

    def test_the_rejected_frame_is_consumed_by_the_put_that_uses_it(self, tmp_path):
        """A second put must not resurrect rows the first one already merged."""
        lake = FakeLake()
        store = _thrash(tmp_path, lake)
        store.put(_marked(2.0, strikes=(100.0, 105.0, 130.0)), **NEW_W)
        store.put(_marked(3.0, strikes=(100.0, 105.0, 130.0)), **NEW_W)
        assert store.lake_merged == 1, "the frame is used once, then dropped"
        assert store._lake_rejected_frames == {}

    # -- test 5 ------------------------------------------------------------
    def test_an_empty_old_chain_contributes_no_sentinel_row(self, tmp_path):
        """A sentinel asserts a window, not a contract. The union replaces it."""
        lake = FakeLake()
        _seed_lake(tmp_path, lake,
                   dict(universe_dte=14, strike_gte=80.0, strike_lte=110.0,
                        model="m1"),
                   snapshot=_snapshot(strikes=()))
        store = ChainStore(str(tmp_path / "e1"), lake=lake)
        assert store.get("XYZ", AS_OF, **NEW_REQUEST) is None
        store.put(_marked(2.0, strikes=(100.0, 105.0, 130.0)), **NEW_W)

        assert store.lake_merged == 1
        merged = _lake_frame(lake)
        assert list(merged["symbol"]).count("") == 0
        assert sorted(merged["strike"]) == [100.0, 105.0, 130.0]
        assert _provenance(merged) == (14.0, 80.0, 130.0, "m1")

    def test_an_empty_new_chain_keeps_the_old_contracts(self, tmp_path):
        lake = FakeLake()
        store = _thrash(tmp_path, lake)
        store.put(_snapshot(strikes=()), **NEW_W)

        assert store.lake_merged == 1
        merged = _lake_frame(lake)
        assert list(merged["symbol"]).count("") == 0
        assert sorted(merged["strike"]) == [90.0, 95.0, 100.0]

    def test_two_empty_chains_stay_one_sentinel(self, tmp_path):
        lake = FakeLake()
        _seed_lake(tmp_path, lake,
                   dict(universe_dte=14, strike_gte=80.0, strike_lte=110.0,
                        model="m1"),
                   snapshot=_snapshot(strikes=()))
        store = ChainStore(str(tmp_path / "e3"), lake=lake)
        assert store.get("XYZ", AS_OF, **NEW_REQUEST) is None
        store.put(_snapshot(strikes=()), **NEW_W)

        merged = _lake_frame(lake)
        assert len(merged) == 1 and merged["symbol"].iloc[0] == ""
        assert _provenance(merged) == (14.0, 80.0, 130.0, "m1")

    # -- test 6 ------------------------------------------------------------
    def test_the_seed_tool_never_merges(self, tmp_path, captured_events):
        """`seed` uploads files; it has no ChainStore and no rejected frames."""
        from tools.diagnostics.chain_lake_seed import seed

        root = tmp_path / "cache"
        ChainStore(str(root)).put(_snapshot(), **NARROW)
        lake = FakeLake()
        lake.put_object("XYZ", AS_OF, b"a different, possibly wider file")

        counts = seed(root, lake)
        assert (counts.uploaded, counts.skipped_differs) == (0, 1)
        assert lake.bytes_of("XYZ", AS_OF) == b"a different, possibly wider file"
        assert not _events(captured_events, "chain_lake_merged")
        assert not _events(captured_events, "chain_lake_merge_gap")

    # -- no lake, no merge -------------------------------------------------
    def test_a_store_without_a_lake_never_remembers_a_frame(self, tmp_path):
        """The merge path exists only where a download happened."""
        store = ChainStore(str(tmp_path))
        store.put(_snapshot(), **OLD_W)
        assert store.get("XYZ", AS_OF, **NEW_REQUEST) is None
        assert store._lake_rejected_frames == {}
        store.put(_snapshot(strikes=(100.0, 130.0)), **NEW_W)
        assert store.lake_merged == 0 and store.lake_merge_gaps == 0
        got = store.get("XYZ", AS_OF, **NEW_REQUEST)
        assert [q.strike for q in got.puts] == [100.0, 130.0], (
            "a local rebuild replaces its own file, exactly as before FC-091"
        )

    def test_a_locally_rejected_file_is_not_a_merge_candidate(self, tmp_path):
        """Only a file the LAKE served can be merged.

        A local file is this machine's own, and `put` has always replaced it.
        Merging one would change local-cache semantics for runs with no lake
        at all.
        """
        lake = FakeLake()
        store = ChainStore(str(tmp_path / "loc"), lake=lake)
        store.put(_snapshot(strikes=(90.0, 95.0, 100.0)), **OLD_W)
        store.lake_puts = 0
        assert store.get("XYZ", AS_OF, **NEW_REQUEST) is None
        assert store._lake_rejected_frames == {}


class TestMergeWindows:
    """The union rule, directly."""

    def _w(self, dte=7.0, gte=90.0, lte=110.0, model="m1"):
        return chain_store_module._Window(dte, gte, lte, model)

    def test_the_union_takes_the_widest_of_each_bound(self):
        window, reason = chain_store_module._merge_windows(
            self._w(dte=7.0, gte=100.0, lte=130.0),
            self._w(dte=14.0, gte=90.0, lte=110.0),
        )
        assert reason is None
        assert (window.universe_dte, window.strike_gte, window.strike_lte) == (
            14.0, 90.0, 130.0)
        assert window.model == "m1"

    def test_a_contained_window_unions_to_the_container(self):
        window, reason = chain_store_module._merge_windows(
            self._w(dte=7.0, gte=95.0, lte=105.0),
            self._w(dte=14.0, gte=90.0, lte=110.0),
        )
        assert reason is None
        assert (window.universe_dte, window.strike_gte, window.strike_lte) == (
            14.0, 90.0, 110.0)

    def test_a_model_change_refuses(self):
        assert chain_store_module._merge_windows(
            self._w(model="m2"), self._w(model="m1"))[1] == "model_changed"

    def test_unknown_on_either_side_refuses(self):
        nan = float("nan")
        assert chain_store_module._merge_windows(
            self._w(gte=nan), self._w())[1] == "unknown_provenance"
        assert chain_store_module._merge_windows(
            self._w(), self._w(dte=nan))[1] == "unknown_provenance"

    def test_disjoint_in_either_direction_is_a_gap(self):
        assert chain_store_module._merge_windows(
            self._w(gte=120.0, lte=130.0), self._w(gte=90.0, lte=100.0),
        )[1] == "strike_gap"
        assert chain_store_module._merge_windows(
            self._w(gte=90.0, lte=100.0), self._w(gte=120.0, lte=130.0),
        )[1] == "strike_gap"

    def test_windows_that_share_one_bound_are_not_a_gap(self):
        window, reason = chain_store_module._merge_windows(
            self._w(gte=100.0, lte=130.0), self._w(gte=90.0, lte=100.0),
        )
        assert reason is None and window.strike_gte == 90.0

    def test_union_rows_refuses_a_frame_with_a_missing_column(self):
        frame = pd.DataFrame([{"symbol": "A", "strike": 1.0}])
        assert chain_store_module._union_rows([{"symbol": "B"}], frame) is None


# --------------------------------------------------------------------------- #
# 5. Failure is never fatal
# --------------------------------------------------------------------------- #
class TestLakeFailuresDegrade:
    def test_download_failure_degrades_to_local_only_and_is_counted(
        self, tmp_path, captured_events
    ):
        lake = FakeLake()
        lake.raise_on.add("download")
        store = ChainStore(str(tmp_path), lake=lake)

        assert store.get("XYZ", AS_OF) is None  # no exception propagates
        assert store.lake_errors == 1 and store.lake_hits == 0
        errors = _events(captured_events, "chain_lake_error")
        assert len(errors) == 1
        assert (errors[0]["op"], errors[0]["symbol"]) == ("download", "XYZ")

    def test_upload_failure_still_leaves_a_good_local_file(
        self, tmp_path, captured_events
    ):
        lake = FakeLake()
        lake.raise_on.add("upload")
        store = ChainStore(str(tmp_path), lake=lake)
        store.put(_snapshot(), **NARROW)  # must not raise

        assert store.lake_puts == 0 and store.lake_errors == 1
        assert store.get("XYZ", AS_OF) is not None, "local cache unaffected"
        assert [e["op"] for e in _events(captured_events, "chain_lake_error")] == [
            "upload"]

    def test_a_failing_lake_does_not_stop_a_replay_from_completing(self, tmp_path):
        lake = FakeLake()
        lake.raise_on.update({"stat", "download", "upload"})
        store = ChainStore(str(tmp_path), lake=lake)

        assert store.get("XYZ", AS_OF) is None
        store.put(_snapshot(), **NARROW)
        assert store.get("XYZ", AS_OF) is not None
        assert store.lake_errors >= 1
        assert store.summary()["lake_errors"] == store.lake_errors


# --------------------------------------------------------------------------- #
# L3. Circuit breaker and permanent disable
# --------------------------------------------------------------------------- #
class TestCircuitBreaker:
    def test_a_permanently_failing_lake_is_tried_at_most_n_times(self, tmp_path):
        """5,400 symbol-days x a 30s timeout is a run that never finishes."""
        lake = FakeLake()
        lake.raise_on.update({"stat", "download", "upload"})
        store = ChainStore(str(tmp_path), lake=lake)

        for i in range(50):
            store.get("XYZ", date(2025, 2, 1 + (i % 20)))

        assert len(lake.calls) <= MAX_CONSECUTIVE_LAKE_ERRORS
        assert lake.disabled and lake.disabled_reason == "consecutive_errors"
        assert store.summary()["lake_disabled"] is True
        assert store.summary()["lake_disabled_reason"] == "consecutive_errors"

    def test_the_disable_is_logged_exactly_once(self, tmp_path, captured_events):
        lake = FakeLake()
        lake.raise_on.add("download")
        store = ChainStore(str(tmp_path), lake=lake)
        for i in range(50):
            store.get("XYZ", date(2025, 2, 1 + (i % 20)))
        assert len(_events(captured_events, "chain_lake_disabled")) == 1

    def test_a_success_resets_the_streak(self, tmp_path):
        lake = FakeLake()
        store = ChainStore(str(tmp_path), lake=lake)
        for i in range(MAX_CONSECUTIVE_LAKE_ERRORS - 1):
            lake.raise_on.add("download")
            store.get("XYZ", date(2025, 3, 1 + i))
        lake.raise_on.clear()
        store.get("XYZ", date(2025, 3, 20))  # clean miss
        lake.raise_on.add("download")
        for i in range(MAX_CONSECUTIVE_LAKE_ERRORS - 1):
            store.get("XYZ", date(2025, 4, 1 + i))
        assert not lake.disabled, "a transient patch of errors must not trip it"

    def test_a_disabled_lake_makes_no_further_calls_but_the_store_still_works(
        self, tmp_path
    ):
        lake = FakeLake()
        lake.raise_on.add("download")
        store = ChainStore(str(tmp_path), lake=lake)
        for i in range(MAX_CONSECUTIVE_LAKE_ERRORS):
            store.get("XYZ", date(2025, 5, 1 + i))
        assert lake.disabled
        calls = len(lake.calls)

        store.put(_snapshot(), **NARROW)
        assert store.get("XYZ", AS_OF) is not None, "local cache keeps working"
        assert len(lake.calls) == calls, "a disabled lake is never called again"
        assert store.lake_puts == 0

    def test_the_client_failure_latch_is_sticky_on_its_own(self, monkeypatch):
        """Pinned independently of `disabled`, which is a second, outer gate.

        Removing `self._client_failed = True` leaves the store's `_lake_active`
        check still suppressing calls, so a store-level test cannot see the
        regression. This one talks to the lake directly and clears `disabled`
        first, so the latch is the only thing that can stop a second ADC
        attempt.
        """
        built = []

        def _boom():
            built.append(1)
            raise RuntimeError("could not determine default credentials")

        monkeypatch.setattr(chain_store_module, "_storage_client", _boom)
        lake = ChainLake("some-bucket")

        with pytest.raises(ChainLakeUnavailable):
            lake.stat("XYZ", AS_OF)
        assert lake._client_failed is True

        # Pretend the outer gate was never set: only the latch is left.
        lake.disabled, lake.disabled_reason = False, None
        with pytest.raises(ChainLakeUnavailable) as exc:
            lake.stat("XYZ", AS_OF)
        assert exc.value.reason == "credentials"
        assert len(built) == 1, "a second ADC attempt means the latch is gone"

    def test_a_disabled_lake_refuses_direct_callers_too(self, tmp_path,
                                                        monkeypatch):
        """The seed tool has no ChainStore in front of it to check the flag."""
        bucket = FakeBucket("b")
        monkeypatch.setattr(chain_store_module, "_storage_client",
                            lambda: FakeClient(bucket))
        lake = ChainLake("b")
        lake.disable("consecutive_errors")

        for op in (
            lambda: lake.stat("XYZ", AS_OF),
            lambda: lake.download("XYZ", AS_OF, tmp_path / "x.parquet"),
            lambda: lake.upload(tmp_path / "x", "XYZ", AS_OF, if_generation_match=0),
        ):
            with pytest.raises(ChainLakeUnavailable):
                op()
        assert bucket.calls == [], "a disabled lake issues no RPCs at all"

    def test_a_failing_client_factory_is_called_exactly_once(self, tmp_path,
                                                             monkeypatch):
        """No credential appears mid-run; retrying ADC 5,400 times is pure latency."""
        built = []

        def _boom():
            built.append(1)
            raise RuntimeError("could not determine default credentials")

        monkeypatch.setattr(chain_store_module, "_storage_client", _boom)
        lake = ChainLake("some-bucket")
        store = ChainStore(str(tmp_path), lake=lake)

        for i in range(50):
            assert store.get("XYZ", date(2025, 6, 1 + (i % 20))) is None

        assert len(built) == 1
        assert lake.disabled and lake.disabled_reason == "credentials"
        assert store.lake_errors == 1, "counted once, then not retried"


# --------------------------------------------------------------------------- #
# L4/L5/L6. The real ChainLake against a fake GCS client
# --------------------------------------------------------------------------- #
class TestRealLakeAgainstAFakeClient:
    """Exercises the code that talks to google-cloud-storage, not a stand-in."""

    def _wire(self, monkeypatch, bucket):
        client = FakeClient(bucket)
        monkeypatch.setattr(chain_store_module, "_storage_client", lambda: client)
        return client

    def test_the_startup_probe_never_calls_bucket_exists(self, tmp_path,
                                                         monkeypatch):
        """`Bucket.exists()` needs storage.buckets.get, which objectAdmin lacks.

        The planned grant for the Job's SA and for claude-operator is
        `roles/storage.objectAdmin` — objects only. A bucket-level health check
        would 403 on the first call of EVERY run and disable the lake
        permanently, i.e. the feature would never once work in production. The
        fake bucket raises Forbidden from `exists()` precisely so that any
        return to that pattern fails here.
        """
        bucket = FakeBucket("b")
        self._wire(monkeypatch, bucket)
        store = ChainStore(str(tmp_path), lake=ChainLake("b"))

        store.put(_snapshot(), **WIDE)
        assert store.lake_puts == 1
        assert store.get("XYZ", AS_OF, universe_dte=7, underlying_price=100.0,
                         model="m1") is not None

        ops = [c[0] for c in bucket.calls]
        assert "bucket_exists" not in ops, (
            "the whole flow must work under object-scoped permissions alone"
        )
        assert ops.count("list_blobs") == 1, "probe once, at startup"

    def test_an_empty_bucket_is_a_healthy_lake_not_a_dead_one(self, tmp_path,
                                                              monkeypatch):
        """Day one, before the seed runs: zero objects, everything still works."""
        bucket = FakeBucket("b")
        self._wire(monkeypatch, bucket)
        lake = ChainLake("b")
        store = ChainStore(str(tmp_path), lake=lake)

        assert store.get("XYZ", AS_OF) is None
        assert not lake.disabled
        assert store.lake_misses == 1, "an empty lake misses; it does not disable"

    def test_a_missing_bucket_disables_the_lake_loudly(
        self, tmp_path, monkeypatch, captured_events
    ):
        """A typo'd bucket must not read as 'the lake is simply empty'."""
        bucket = FakeBucket("typo-bucket", present=False)  # list -> NotFound
        self._wire(monkeypatch, bucket)
        lake = ChainLake("typo-bucket")
        store = ChainStore(str(tmp_path), lake=lake)

        assert store.get("XYZ", AS_OF) is None
        assert lake.disabled and lake.disabled_reason == "bucket_missing"
        assert _events(captured_events, "chain_lake_unavailable")
        assert store.lake_misses == 0, "a dead bucket is not a cache miss"
        assert [c for c in bucket.calls if c[0] == "download"] == []

        store.get("XYZ", AS_OF)
        assert len([c for c in bucket.calls if c[0] == "list_blobs"]) == 1

    def test_an_unreachable_bucket_disables_the_lake(self, tmp_path, monkeypatch):
        bucket = FakeBucket("b", list_raises=RuntimeError("network down"))
        self._wire(monkeypatch, bucket)
        lake = ChainLake("b")
        store = ChainStore(str(tmp_path), lake=lake)
        assert store.get("XYZ", AS_OF) is None
        assert lake.disabled and lake.disabled_reason == "bucket_unreachable"

    def test_a_permission_error_on_the_probe_is_unreachable_not_missing(
        self, tmp_path, monkeypatch
    ):
        """403 != 404. Both disable, but the reason is what an operator acts on."""
        bucket = FakeBucket("b", list_raises=Forbidden("storage.objects.list denied"))
        self._wire(monkeypatch, bucket)
        lake = ChainLake("b")
        assert ChainStore(str(tmp_path), lake=lake).get("XYZ", AS_OF) is None
        assert lake.disabled_reason == "bucket_unreachable"

    def test_not_found_is_a_miss_and_not_an_error(self, tmp_path, monkeypatch):
        bucket = FakeBucket("b")
        self._wire(monkeypatch, bucket)
        store = ChainStore(str(tmp_path), lake=ChainLake("b"))

        assert store.get("XYZ", AS_OF) is None
        assert store.lake_misses == 1 and store.lake_errors == 0
        assert [c[0] for c in bucket.calls] == ["list_blobs", "download"], (
            "the startup probe, then one RPC for the miss — no exists() probe"
        )

    def test_a_download_that_fails_midway_leaves_nothing_behind(
        self, tmp_path, monkeypatch
    ):
        bucket = FakeBucket("b", download_raises=IOError("connection reset"))
        self._wire(monkeypatch, bucket)
        store = ChainStore(str(tmp_path), lake=ChainLake("b"))

        assert store.get("XYZ", AS_OF) is None
        assert store.lake_errors == 1
        d = tmp_path / "XYZ"
        assert not (d / "2025-01-06.parquet").exists(), "no half file as a cache entry"
        assert list(d.glob("*.tmp")) == [], "no temp stragglers"

    def test_a_round_trip_through_the_real_lake_code(self, tmp_path, monkeypatch):
        bucket = FakeBucket("b")
        self._wire(monkeypatch, bucket)
        writer = ChainStore(str(tmp_path / "w"), lake=ChainLake("b"))
        writer.put(_snapshot(), **WIDE)
        assert writer.lake_puts == 1

        reader = ChainStore(str(tmp_path / "r"), lake=ChainLake("b"))
        got = reader.get("XYZ", AS_OF, universe_dte=7, strike_gte=90.0,
                         strike_lte=110.0, underlying_price=100.0, model="m1")
        assert got is not None and reader.lake_hits == 1
        assert [q.strike for q in got.puts] == [90.0, 95.0, 100.0]

    def test_download_rpcs_are_bounded(self, tmp_path, monkeypatch):
        """No timeout means a stalled GCS burns the whole task budget."""
        seen = {}

        class Recording(FakeBucket):
            def blob(self, name):
                b = FakeBlob(self, name)
                original = b.download_to_filename

                def spy(filename, **kwargs):
                    seen.update(kwargs)
                    return original(filename, **kwargs)

                b.download_to_filename = spy
                return b

        bucket = Recording("b")
        self._wire(monkeypatch, bucket)
        ChainStore(str(tmp_path), lake=ChainLake("b")).get("XYZ", AS_OF)

        assert seen["timeout"] == chain_store_module.LAKE_TIMEOUT_S
        assert seen["retry"] is not None, "an unbounded retry is an unbounded run"

    def test_upload_kwargs_carry_timeout_retry_and_checksum(self, tmp_path,
                                                            monkeypatch):
        seen = {}

        class Recording(FakeBucket):
            def blob(self, name):
                b = FakeBlob(self, name)
                original = b.upload_from_filename

                def spy(filename, **kwargs):
                    seen.update(kwargs)
                    return original(filename, **kwargs)

                b.upload_from_filename = spy
                return b

        bucket = Recording("b")
        self._wire(monkeypatch, bucket)
        ChainStore(str(tmp_path), lake=ChainLake("b")).put(_snapshot(), **WIDE)

        assert seen["checksum"] == "crc32c"
        assert seen["timeout"] == chain_store_module.LAKE_TIMEOUT_S
        assert seen["retry"] is not None
        assert seen["if_generation_match"] == 0, "create-only on a fresh object"

    def test_a_precondition_failure_is_a_skip_not_an_error(self, tmp_path,
                                                           monkeypatch):
        bucket = FakeBucket("b")
        self._wire(monkeypatch, bucket)
        store = ChainStore(str(tmp_path), lake=ChainLake("b"))
        # Object appears between the store's "absent" observation and the upload.
        assert store.get("XYZ", AS_OF) is None
        bucket.objects[f"{DEFAULT_LAKE_PREFIX}/XYZ/2025-01-06.parquet"] = (
            b"another writer", 500,
        )
        store.put(_snapshot(), **WIDE)

        assert store.lake_skipped == 1 and store.lake_errors == 0
        assert store.lake_puts == 0
        assert bucket.objects[
            f"{DEFAULT_LAKE_PREFIX}/XYZ/2025-01-06.parquet"][0] == b"another writer"


# --------------------------------------------------------------------------- #
# 8. The seed tool
# --------------------------------------------------------------------------- #
class TestSeedTool:
    def _cache_with(self, tmp_path, days=(6, 7, 8)):
        root = tmp_path / "cache"
        for d in days:
            ChainStore(str(root)).put(_snapshot(as_of=date(2025, 1, d)), **NARROW)
        return root

    def test_identical_objects_are_skipped_as_identical(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path)
        lake = FakeLake()
        already = date(2025, 1, 7)
        lake.put_object("XYZ", already,
                        (root / "XYZ" / "2025-01-07.parquet").read_bytes())

        counts = seed(root, lake)
        assert (counts.scanned, counts.uploaded) == (3, 2)
        assert (counts.skipped_identical, counts.skipped_differs) == (1, 0)
        assert counts.failed == 0 and len(lake.objects) == 3

    def test_a_differing_object_is_reported_and_left_alone(self, tmp_path):
        """It may be WIDER than what we hold; this tool will not gamble."""
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path)
        lake = FakeLake()
        already = date(2025, 1, 7)
        lake.put_object("XYZ", already, b"a different, possibly wider file")

        counts = seed(root, lake)
        assert (counts.uploaded, counts.skipped_identical, counts.skipped_differs) \
            == (2, 0, 1)
        assert counts.differs == ["XYZ 2025-01-07"]
        assert lake.bytes_of("XYZ", already) == b"a different, possibly wider file"

    def test_force_uploads_everything(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path)
        lake = FakeLake()
        already = date(2025, 1, 7)
        lake.put_object("XYZ", already, b"pre-existing")

        counts = seed(root, lake, force=True)
        assert (counts.scanned, counts.uploaded, counts.skipped_existing) == (3, 3, 0)
        assert lake.bytes_of("XYZ", already) != b"pre-existing"

    def test_uploads_carry_a_generation_precondition(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path, days=(6,))
        lake = FakeLake()

        class Racing(FakeLake):
            def upload(self, local_path, underlying, as_of, *, if_generation_match):
                # Whatever generation the seed captured, it is stale.
                raise ChainLakePreconditionFailed("stale generation")

        racing = Racing()
        counts = seed(root, racing)
        assert counts.failed == 1 and counts.uploaded == 0, (
            "a lost race is reported, never counted as a successful upload"
        )
        assert lake.objects == {}

    def test_dry_run_writes_nothing(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path)
        lake = FakeLake()
        counts = seed(root, lake, dry_run=True)
        assert counts.uploaded == 3 and lake.objects == {}
        assert lake.ops() == ["stat"] * 3, "dry run still costs one RPC per file"

    def test_one_bad_file_does_not_abort_the_seed(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path)
        lake = FakeLake()
        lake.raise_on.add("upload")
        counts = seed(root, lake)
        assert counts.scanned == 3 and counts.failed == 3 and counts.uploaded == 0

    def test_non_date_filenames_are_reported_not_uploaded(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path, days=(6,))
        (root / "XYZ" / "notadate.parquet").write_bytes(b"x")
        counts = seed(root, FakeLake())
        assert (counts.scanned, counts.uploaded, counts.unparseable) == (1, 1, 1)

    def test_object_layout_matches_the_store(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path, days=(6,))
        lake = FakeLake()
        seed(root, lake)
        assert list(lake.objects) == ["chains/v1/XYZ/2025-01-06.parquet"]

    def test_an_unusable_lake_aborts_instead_of_failing_every_file(self, tmp_path):
        """One diagnosis beats 5,400 identical failures."""
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path)
        lake = FakeLake()
        lake.disable("credentials")

        with pytest.raises(ChainLakeUnavailable):
            seed(root, lake)

    def test_main_reports_an_unusable_lake_as_exit_2(self, tmp_path, monkeypatch):
        from tools.diagnostics import chain_lake_seed as tool

        root = self._cache_with(tmp_path, days=(6,))

        def _dead(*_a, **_k):
            raise ChainLakeUnavailable("bucket_missing", "no such bucket")

        monkeypatch.setattr(tool, "seed", _dead)
        assert tool.main(["--cache-dir", str(root), "--bucket", "b"]) == 2

    def test_missing_cache_dir_is_a_clean_exit_not_a_traceback(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import main

        assert main(["--cache-dir", str(tmp_path / "nope"), "--bucket", "b"]) == 2

    def test_an_empty_prefix_is_rejected(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import main

        root = self._cache_with(tmp_path, days=(6,))
        rc = main(["--cache-dir", str(root), "--bucket", "b", "--prefix", "/"])
        assert rc == 2, "an empty prefix would scatter objects at the bucket root"
        with pytest.raises(ValueError):
            ChainLake("b", "  ")


# --------------------------------------------------------------------------- #
# 9. Configuration and lazy construction
# --------------------------------------------------------------------------- #
class TestConfiguration:
    def test_from_env_builds_a_lake_without_constructing_a_client(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("CHAIN_LAKE_BUCKET", "some-bucket")
        store = ChainStore.from_env(str(tmp_path))  # factory raises if called
        assert isinstance(store.lake, ChainLake)
        assert store.lake.bucket_name == "some-bucket"
        assert store.lake.prefix == DEFAULT_LAKE_PREFIX
        assert store.lake._client is None

    def test_prefix_env_overrides_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHAIN_LAKE_BUCKET", "some-bucket")
        monkeypatch.setenv("CHAIN_LAKE_PREFIX", "/chains/v2/")
        store = ChainStore.from_env(str(tmp_path))
        assert store.lake.prefix == "chains/v2"
        assert store.lake.object_name("xyz", AS_OF) == "chains/v2/XYZ/2025-01-06.parquet"

    def test_lake_from_env_is_shareable_across_stores(self, tmp_path, monkeypatch):
        """One lake per RUN: one client, one bucket probe, one breaker."""
        monkeypatch.setenv("CHAIN_LAKE_BUCKET", "some-bucket")
        lake = ChainStore.lake_from_env()
        a = ChainStore(str(tmp_path / "a"), lake=lake)
        b = ChainStore(str(tmp_path / "b"), lake=lake)
        assert a.lake is b.lake

    def test_object_name_is_the_local_layout(self):
        assert ChainLake("b", "chains/v1").object_name("xyz", AS_OF) == (
            "chains/v1/XYZ/2025-01-06.parquet")

    def test_a_bucketless_lake_is_refused(self):
        with pytest.raises(ValueError):
            ChainLake("")


# --------------------------------------------------------------------------- #
# Wiring: evaluate + screen
# --------------------------------------------------------------------------- #
class TestWiring:
    def test_evaluate_uses_from_env_and_logs_a_summary(self):
        import inspect

        from src.backtesting import evaluate

        src = inspect.getsource(evaluate)
        assert "ChainStore.from_env()" in src
        assert "chain_lake_summary" in src

    def test_screen_shares_one_lake_and_logs_a_run_summary(self):
        import inspect

        from src.backtesting import screen

        src = inspect.getsource(screen)
        assert "lake_from_env()" in src
        assert "chain_lake_run_summary" in src
        assert "chain_lake_degraded" in src

    def test_screen_run_summary_aggregates_and_warns(self, captured_events):
        from src.backtesting.screen import _accumulate_lake, _log_lake_run_summary

        lake = FakeLake()
        totals = {}
        _accumulate_lake(totals, ChainStore("x", lake=lake).summary())
        store = ChainStore("y", lake=lake)
        store.lake_hits, store.lake_errors = 3, 2
        _accumulate_lake(totals, store.summary())
        assert totals["lake_hits"] == 3 and totals["lake_errors"] == 2

        _log_lake_run_summary(lake, totals, "run-1")
        assert _events(captured_events, "chain_lake_run_summary")
        assert _events(captured_events, "chain_lake_degraded"), (
            "a run that errored must not look clean in the logs"
        )

    def test_no_run_summary_without_a_lake(self, captured_events):
        from src.backtesting.screen import _log_lake_run_summary

        _log_lake_run_summary(None, {}, "run-2")
        assert not [e for e in captured_events
                    if str(e.get("event_type", "")).startswith("chain_lake")]

    def test_summary_shape_is_loggable(self, tmp_path):
        store = ChainStore(str(tmp_path), lake=FakeLake())
        summary = store.summary()
        assert set(summary) == {
            "lake_enabled", "lake_bucket", "lake_prefix",
            "lake_hits", "lake_misses", "lake_rejected",
            "lake_puts", "lake_skipped", "lake_skipped_unreadable_remote",
            "lake_merged", "lake_merge_gaps",
            "lake_errors", "lake_disabled", "lake_disabled_reason",
        }
        assert "symbol" not in summary, "must not collide with the log's symbol key"


# --------------------------------------------------------------------------- #
# Window comparison, directly
# --------------------------------------------------------------------------- #
class TestWindowRegression:
    def _w(self, dte=7.0, gte=90.0, lte=110.0, model="m1"):
        return chain_store_module._Window(dte, gte, lte, model)

    def test_equal_is_allowed(self):
        assert chain_store_module._window_regression(self._w(), self._w()) is None

    def test_strictly_wider_is_allowed(self):
        assert chain_store_module._window_regression(
            self._w(dte=14, gte=80.0, lte=225.0), self._w()) is None

    def test_a_hair_narrower_within_tolerance_is_allowed(self):
        """Bounds are recomputed from a float close; equality must be tolerant."""
        eps = chain_store_module._BOUND_TOL / 2
        assert chain_store_module._window_regression(
            self._w(gte=90.0 + eps, lte=110.0 - eps), self._w()) is None

    def test_narrower_beyond_tolerance_is_refused(self):
        assert chain_store_module._window_regression(
            self._w(gte=90.5), self._w()) == "narrower_strikes"
        assert chain_store_module._window_regression(
            self._w(lte=109.5), self._w()) == "narrower_strikes"
        assert chain_store_module._window_regression(
            self._w(dte=6), self._w()) == "narrower_dte"

    def test_unknown_on_one_side_only_is_refused(self):
        unknown = self._w(dte=float("nan"))
        assert chain_store_module._window_regression(unknown, self._w()) == \
            "unknown_provenance"
        assert chain_store_module._window_regression(self._w(), unknown) == \
            "unknown_provenance"

    def test_unknown_on_both_sides_is_allowed(self):
        nan = float("nan")
        both = self._w(dte=nan, gte=nan, lte=nan, model="")
        assert chain_store_module._window_regression(both, both) is None

    def test_a_model_change_is_never_a_widening(self):
        assert chain_store_module._window_regression(
            self._w(dte=99, gte=0.0, lte=9999.0, model="m2"), self._w()) == \
            "model_changed"

    def test_window_of_reads_a_written_file(self, tmp_path):
        store = ChainStore(str(tmp_path))
        store.put(_snapshot(), **WIDE)
        df = pd.read_parquet(tmp_path / "XYZ" / "2025-01-06.parquet")
        w = chain_store_module._window_of(df)
        assert (int(w.universe_dte), w.strike_gte, w.strike_lte, w.model) == (
            14, 80.0, 225.0, "m1")
