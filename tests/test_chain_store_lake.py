"""FC-060 Layer 1 — the GCS chain lake behind ChainStore.

Every test here runs against an in-memory fake lake (a dict keyed by object
name). Nothing in this file may construct a real ``google.cloud.storage.Client``
— the two tests that could reach one monkeypatch the client factory to raise, so
"no client was constructed" is asserted rather than assumed.

The properties under test, in the order the plan lists them:

1. no env  -> no lake, no lake call
2. local miss + lake hit  -> downloaded, served, counted; second get is local
3. local miss + lake miss -> plain miss
4. put  -> local file AND identical object in the lake
5. a raising lake -> local-only behaviour, counted, logged, no exception
6. corrupt local file -> local unlinked AND the mirror deleted
7. the coverage check applies to a lake-sourced file exactly as to a local one
8. the seed tool: skip-existing by default, --force re-uploads
9. from_env builds a lake without constructing a client
"""

from datetime import date
from pathlib import Path

import pytest
import structlog

from src.backtesting.data import chain_store as chain_store_module
from src.backtesting.data.chain_builder import ChainQuote, ChainSnapshot
from src.backtesting.data.chain_store import (
    DEFAULT_LAKE_PREFIX,
    ChainLake,
    ChainStore,
)

AS_OF = date(2025, 1, 6)
EXP = date(2025, 1, 10)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeLake:
    """In-memory stand-in for ChainLake with the same surface.

    Records every call so a test can assert that a code path made *no* lake
    call at all — the thing that distinguishes "lake disabled" from "lake
    enabled and happened to miss".
    """

    def __init__(self, prefix: str = DEFAULT_LAKE_PREFIX, bucket: str = "fake-bucket"):
        self.bucket_name = bucket
        self.prefix = prefix
        self.objects = {}          # object name -> bytes
        self.calls = []            # (op, underlying, as_of)
        self.raise_on = set()      # ops that should blow up

    # -- same object layout as the real lake --------------------------------
    def object_name(self, underlying, as_of):
        return f"{self.prefix}/{underlying.upper()}/{as_of.isoformat()}.parquet"

    def _record(self, op, underlying, as_of):
        self.calls.append((op, underlying.upper(), as_of))
        if op in self.raise_on:
            raise RuntimeError(f"fake lake failure on {op}")

    def exists(self, underlying, as_of):
        self._record("exists", underlying, as_of)
        return self.object_name(underlying, as_of) in self.objects

    def download(self, underlying, as_of, local_path):
        self._record("download", underlying, as_of)
        blob = self.objects.get(self.object_name(underlying, as_of))
        if blob is None:
            return False
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(blob)
        return True

    def upload(self, local_path, underlying, as_of):
        self._record("upload", underlying, as_of)
        self.objects[self.object_name(underlying, as_of)] = Path(local_path).read_bytes()

    def delete(self, underlying, as_of):
        self._record("delete", underlying, as_of)
        return self.objects.pop(self.object_name(underlying, as_of), None) is not None

    # -- helpers ------------------------------------------------------------
    def ops(self):
        return [c[0] for c in self.calls]


def _snapshot(strikes=(90.0, 95.0, 100.0), price=100.0, underlying="XYZ", as_of=AS_OF):
    puts = [
        ChainQuote(
            symbol=f"{underlying}{as_of:%y%m%d}P{int(k * 1000):08d}",
            underlying=underlying,
            as_of=as_of,
            expiration=EXP,
            strike=k,
            option_type="put",
            dte=(EXP - as_of).days,
            underlying_price=price,
            mark=1.0,
            bid=0.9,
            ask=1.1,
            implied_volatility=0.3,
            delta=-0.2,
            volume=10,
            modeled_spread=True,
            modeled_greeks=True,
        )
        for k in strikes
    ]
    return ChainSnapshot(underlying, as_of, price, puts, [])


def _seed_lake_from(tmp_path, lake, **put_kwargs):
    """Write a snapshot through a throwaway store so the lake holds real bytes."""
    donor_dir = tmp_path / "donor"
    donor = ChainStore(str(donor_dir), lake=lake)
    donor.put(_snapshot(**put_kwargs.pop("snapshot_kwargs", {})), **put_kwargs)
    return donor


@pytest.fixture
def captured_events():
    """Collect every structlog event_type emitted inside the block."""
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


# --------------------------------------------------------------------------- #
# 1. No env -> no lake at all
# --------------------------------------------------------------------------- #
class TestNoLakeConfigured:
    def test_from_env_without_bucket_has_no_lake_and_makes_no_lake_call(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("CHAIN_LAKE_BUCKET", raising=False)
        monkeypatch.delenv("CHAIN_LAKE_PREFIX", raising=False)

        store = ChainStore.from_env(str(tmp_path))
        assert store.lake is None

        # A miss must not reach for anything: with lake None there is nothing to
        # reach for, and the client factory above would fail loudly if it tried.
        assert store.get("XYZ", AS_OF) is None
        assert store.summary()["lake_enabled"] is False
        assert store.summary()["lake_hits"] == 0

    def test_empty_bucket_env_is_treated_as_unset(self, tmp_path, monkeypatch):
        # A Cloud Run env var cleared to "" must not create a lake pointed at "".
        monkeypatch.setenv("CHAIN_LAKE_BUCKET", "   ")
        assert ChainStore.from_env(str(tmp_path)).lake is None

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
        _seed_lake_from(tmp_path, lake)

        store = ChainStore(str(tmp_path / "cold"), lake=lake)
        assert not store.has("XYZ", AS_OF)

        snap = store.get("XYZ", AS_OF)
        assert snap is not None
        assert snap.underlying_price == 100.0
        assert [q.strike for q in snap.puts] == [90.0, 95.0, 100.0]
        assert store.lake_hits == 1 and store.lake_misses == 0
        # The download landed on the local path, so it is a local file now.
        assert store.has("XYZ", AS_OF)

        before = list(lake.ops())
        again = store.get("XYZ", AS_OF)
        assert again is not None
        assert store.lake_hits == 1, "second read must be a local hit"
        assert lake.ops() == before, "second read must not touch the lake"

    def test_local_miss_and_lake_miss_is_a_plain_miss(self, tmp_path):
        lake = FakeLake()
        store = ChainStore(str(tmp_path), lake=lake)

        assert store.get("XYZ", AS_OF) is None
        assert store.lake_misses == 1
        assert store.lake_hits == 0 and store.lake_errors == 0
        assert lake.ops() == ["download"]

    def test_a_downloaded_file_is_byte_identical_to_the_source(self, tmp_path):
        """The lake moves files; it must not repack them."""
        lake = FakeLake()
        _seed_lake_from(tmp_path, lake)
        source = (tmp_path / "donor" / "XYZ" / f"{AS_OF.isoformat()}.parquet").read_bytes()

        store = ChainStore(str(tmp_path / "cold"), lake=lake)
        store.get("XYZ", AS_OF)
        landed = (tmp_path / "cold" / "XYZ" / f"{AS_OF.isoformat()}.parquet").read_bytes()
        assert landed == source


# --------------------------------------------------------------------------- #
# 4. Write-through
# --------------------------------------------------------------------------- #
class TestWriteThrough:
    def test_put_writes_local_and_uploads_identical_bytes(self, tmp_path):
        lake = FakeLake()
        store = ChainStore(str(tmp_path), lake=lake)
        store.put(_snapshot(), universe_dte=10, strike_gte=80.0, strike_lte=120.0,
                  model="m1")

        local = tmp_path / "XYZ" / f"{AS_OF.isoformat()}.parquet"
        assert local.exists()
        name = lake.object_name("XYZ", AS_OF)
        assert name in lake.objects, "put must mirror to the lake"
        assert lake.objects[name] == local.read_bytes()
        assert store.lake_puts == 1 and store.lake_errors == 0

    def test_upload_carries_the_provenance_a_later_coverage_check_needs(self, tmp_path):
        """A mirrored file must be able to satisfy a bounded request later.

        Uploading before the atomic replace, or uploading a re-serialised frame,
        would break this: the object would carry no usable provenance and every
        bounded read of it would miss forever.
        """
        lake = FakeLake()
        ChainStore(str(tmp_path / "a"), lake=lake).put(
            _snapshot(), universe_dte=10, strike_gte=80.0, strike_lte=120.0, model="m1"
        )
        cold = ChainStore(str(tmp_path / "b"), lake=lake)
        got = cold.get("XYZ", AS_OF, universe_dte=7, strike_gte=85.0,
                       strike_lte=115.0, underlying_price=100.0, model="m1")
        assert got is not None
        assert cold.lake_hits == 1

    def test_no_temp_files_survive_a_put(self, tmp_path):
        lake = FakeLake()
        ChainStore(str(tmp_path), lake=lake).put(_snapshot())
        assert not list((tmp_path / "XYZ").glob("*.tmp"))


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
        assert store.lake_errors == 1
        assert store.lake_hits == 0
        errors = [e for e in captured_events if e.get("event_type") == "chain_lake_error"]
        assert len(errors) == 1
        assert errors[0]["op"] == "download"
        assert errors[0]["symbol"] == "XYZ"
        assert errors[0]["as_of"] == AS_OF.isoformat()

    def test_upload_failure_still_leaves_a_good_local_file(
        self, tmp_path, captured_events
    ):
        lake = FakeLake()
        lake.raise_on.add("upload")
        store = ChainStore(str(tmp_path), lake=lake)

        store.put(_snapshot())  # must not raise

        assert store.lake_puts == 0
        assert store.lake_errors == 1
        assert store.get("XYZ", AS_OF) is not None, "local cache must be unaffected"
        errors = [e for e in captured_events if e.get("event_type") == "chain_lake_error"]
        assert [e["op"] for e in errors] == ["upload"]

    def test_a_failing_lake_does_not_stop_a_replay_from_completing(self, tmp_path):
        """The whole point: a GCS outage costs speed, never a run."""
        lake = FakeLake()
        lake.raise_on.update({"download", "upload", "delete"})
        store = ChainStore(str(tmp_path), lake=lake)

        assert store.get("XYZ", AS_OF) is None
        store.put(_snapshot())
        assert store.get("XYZ", AS_OF) is not None
        assert store.lake_errors == 2
        assert store.summary()["lake_errors"] == 2


# --------------------------------------------------------------------------- #
# 6. Corrupt file self-heals on both sides
# --------------------------------------------------------------------------- #
class TestCorruptFile:
    def test_corrupt_local_file_unlinks_locally_and_deletes_the_mirror(
        self, tmp_path, captured_events
    ):
        lake = FakeLake()
        store = ChainStore(str(tmp_path), lake=lake)
        store.put(_snapshot())
        assert lake.object_name("XYZ", AS_OF) in lake.objects

        path = tmp_path / "XYZ" / f"{AS_OF.isoformat()}.parquet"
        path.write_bytes(b"not a parquet file")

        assert store.get("XYZ", AS_OF) is None
        assert not path.exists(), "corrupt local file must be unlinked"
        assert "delete" in lake.ops()
        assert lake.object_name("XYZ", AS_OF) not in lake.objects, (
            "a corrupt mirror would be re-downloaded forever"
        )
        assert any(e.get("event_type") == "chain_lake_corrupt_delete"
                   for e in captured_events)

    def test_a_corrupt_download_does_not_wedge_the_next_run(self, tmp_path):
        """The bad bytes came FROM the lake; the lake copy must go too."""
        lake = FakeLake()
        lake.objects[lake.object_name("XYZ", AS_OF)] = b"garbage"
        store = ChainStore(str(tmp_path), lake=lake)

        assert store.get("XYZ", AS_OF) is None
        assert store.lake_hits == 1  # the download itself succeeded
        assert lake.objects == {}

    def test_a_failing_delete_on_the_corrupt_path_is_counted_not_raised(self, tmp_path):
        lake = FakeLake()
        store = ChainStore(str(tmp_path), lake=lake)
        store.put(_snapshot())
        (tmp_path / "XYZ" / f"{AS_OF.isoformat()}.parquet").write_bytes(b"bad")
        lake.raise_on.add("delete")

        assert store.get("XYZ", AS_OF) is None
        assert store.lake_errors == 1


# --------------------------------------------------------------------------- #
# 7. Coverage still governs a lake-sourced file
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
        wide = _snapshot(strikes=(85.0, 95.0, 100.0, 200.0))
        ChainStore(str(tmp_path / "donor"), lake=lake).put(
            wide, universe_dte=14, strike_gte=80.0, strike_lte=225.0, model="m1"
        )

    def test_a_narrower_request_is_served_from_the_lake_and_narrowed(self, tmp_path):
        lake = FakeLake()
        self._seed_wide(tmp_path, lake)

        store = ChainStore(str(tmp_path / "cold"), lake=lake)
        got = store.get("XYZ", AS_OF, universe_dte=7, strike_gte=90.0,
                        strike_lte=110.0, underlying_price=100.0, model="m1")
        assert got is not None
        assert store.lake_hits == 1
        assert [q.strike for q in got.puts] == [95.0, 100.0], (
            "the lake-sourced file must be narrowed on read, not served whole"
        )

    def test_a_wider_request_against_a_lake_file_is_a_miss(self, tmp_path):
        lake = FakeLake()
        self._seed_wide(tmp_path, lake)

        store = ChainStore(str(tmp_path / "cold2"), lake=lake)
        got = store.get("XYZ", AS_OF, universe_dte=7, strike_gte=50.0,
                        strike_lte=300.0, underlying_price=100.0, model="m1")
        assert got is None, "an uncovered lake file must fail closed, not be served"
        assert store.lake_hits == 1, "the download happened; the coverage check rejected it"

    def test_a_different_model_fingerprint_from_the_lake_is_a_miss(self, tmp_path):
        lake = FakeLake()
        self._seed_wide(tmp_path, lake)
        store = ChainStore(str(tmp_path / "cold3"), lake=lake)
        assert store.get("XYZ", AS_OF, universe_dte=7, underlying_price=100.0,
                         model="m2") is None

    def test_a_different_underlying_close_from_the_lake_is_a_miss(self, tmp_path):
        lake = FakeLake()
        self._seed_wide(tmp_path, lake)
        store = ChainStore(str(tmp_path / "cold4"), lake=lake)
        assert store.get("XYZ", AS_OF, universe_dte=7, underlying_price=101.0,
                         model="m1") is None


# --------------------------------------------------------------------------- #
# 8. The seed tool
# --------------------------------------------------------------------------- #
class TestSeedTool:
    def _cache_with(self, tmp_path, days=(6, 7, 8)):
        root = tmp_path / "cache"
        for d in days:
            as_of = date(2025, 1, d)
            ChainStore(str(root)).put(_snapshot(as_of=as_of))
        return root

    def test_skips_objects_that_already_exist(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path)
        lake = FakeLake()
        already = date(2025, 1, 7)
        lake.objects[lake.object_name("XYZ", already)] = b"pre-existing"

        counts = seed(root, lake)
        assert (counts.scanned, counts.uploaded, counts.skipped_existing) == (3, 2, 1)
        assert counts.failed == 0
        assert lake.objects[lake.object_name("XYZ", already)] == b"pre-existing", (
            "skip must mean skip — an existing object is never overwritten"
        )
        assert len(lake.objects) == 3

    def test_force_uploads_everything(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path)
        lake = FakeLake()
        already = date(2025, 1, 7)
        lake.objects[lake.object_name("XYZ", already)] = b"pre-existing"

        counts = seed(root, lake, force=True)
        assert (counts.scanned, counts.uploaded, counts.skipped_existing) == (3, 3, 0)
        assert lake.objects[lake.object_name("XYZ", already)] != b"pre-existing"
        assert "exists" not in lake.ops(), "--force must not bother probing"

    def test_dry_run_writes_nothing(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path)
        lake = FakeLake()
        counts = seed(root, lake, dry_run=True)
        assert counts.uploaded == 3
        assert lake.objects == {}

    def test_one_bad_file_does_not_abort_the_seed(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path)
        lake = FakeLake()
        lake.raise_on.add("upload")

        counts = seed(root, lake)
        assert counts.scanned == 3 and counts.failed == 3 and counts.uploaded == 0
        assert len(counts.errors) == 3

    def test_non_date_filenames_are_reported_not_uploaded(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path, days=(6,))
        (root / "XYZ" / "notadate.parquet").write_bytes(b"x")
        lake = FakeLake()

        counts = seed(root, lake)
        assert counts.scanned == 1 and counts.uploaded == 1
        assert counts.unparseable == 1

    def test_object_layout_matches_the_store(self, tmp_path):
        """The seed and the engine must agree on the object name, exactly."""
        from tools.diagnostics.chain_lake_seed import seed

        root = self._cache_with(tmp_path, days=(6,))
        lake = FakeLake()
        seed(root, lake)
        assert list(lake.objects) == ["chains/v1/XYZ/2025-01-06.parquet"]

    def test_missing_cache_dir_is_a_clean_exit_not_a_traceback(self, tmp_path):
        from tools.diagnostics.chain_lake_seed import main

        rc = main(["--cache-dir", str(tmp_path / "nope"), "--bucket", "b"])
        assert rc == 2


# --------------------------------------------------------------------------- #
# 9. Lazy client construction
# --------------------------------------------------------------------------- #
class TestLazyClient:
    def test_from_env_builds_a_lake_without_constructing_a_client(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("CHAIN_LAKE_BUCKET", "some-bucket")
        monkeypatch.delenv("CHAIN_LAKE_PREFIX", raising=False)

        # The autouse fixture already makes _storage_client raise; if from_env
        # constructed one eagerly this line would fail.
        store = ChainStore.from_env(str(tmp_path))

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

    def test_the_client_is_only_built_when_an_operation_runs(self, tmp_path):
        """And when it is, the failure is contained by the store's wrapper."""
        lake = ChainLake("some-bucket")
        store = ChainStore(str(tmp_path), lake=lake)

        assert store.get("XYZ", AS_OF) is None  # would raise AssertionError uncaught
        assert store.lake_errors == 1, "client construction failed => counted lake error"

    def test_object_name_is_the_local_layout(self):
        lake = ChainLake("b", "chains/v1")
        assert lake.object_name("xyz", AS_OF) == "chains/v1/XYZ/2025-01-06.parquet"


# --------------------------------------------------------------------------- #
# evaluate.py wiring
# --------------------------------------------------------------------------- #
class TestEvaluateWiring:
    def test_evaluate_uses_from_env(self):
        """The Job is configured by env; a hardcoded ChainStore() ignores it."""
        import inspect

        from src.backtesting import evaluate

        src = inspect.getsource(evaluate.evaluate_symbol)
        assert "ChainStore.from_env()" in src
        assert "chain_lake_summary" in inspect.getsource(evaluate)

    def test_summary_shape_is_loggable(self, tmp_path):
        store = ChainStore(str(tmp_path), lake=FakeLake())
        summary = store.summary()
        assert set(summary) == {
            "lake_enabled", "lake_bucket", "lake_prefix",
            "lake_hits", "lake_misses", "lake_puts", "lake_errors",
        }
        assert "symbol" not in summary, "must not collide with the log's symbol key"
