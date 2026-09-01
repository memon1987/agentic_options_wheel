"""Chain-lake freshness: is the weekly backfill Job still feeding the lake?

FC-096 Phase A. The `data-backfill` Cloud Run Job runs on a Cloud Scheduler
trigger, and **`jobs:run` is asynchronous**: the scheduler records success the
moment the API call returns, whether the execution then succeeded, failed, or
never existed at all. A paused scheduler, a deleted scheduler and a Job that
404s all look identical — and identical to a healthy one — from every control
this project had before. That is FC-081's failure shape (the thing that never
ran raises no alarm) transplanted onto a Job.

`check_lake_freshness` is the detective control for it, and its posture is
copied from `check_deploy_freshness` deliberately:

* **`fail` only for "the data really is stale"** — `fail` returns HTTP 500 from
  `/regression`, so a GCS hiccup, a missing credential or an image without
  `google-cloud-storage` must never trip it.
* **No `warn` is silent** — every degraded path emits `lake_freshness_degraded`
  with a `reason`, because a check that reports `warn` for ever is
  indistinguishable from one that is working.

The GCS double answers like the real client: `list_blobs(bucket, prefix=...)`
yielding blob objects with a `.name`, in the lexicographic order the API
returns them.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import tools.testing.regression_monitor as rm
from tools.testing.regression_monitor import RegressionMonitor

BUCKET = "options-wheel-chain-lake"
PREFIX = "chains/v1"
UNIVERSE = ["AAPL", "MSFT", "SPY"]


class _Blob:
    def __init__(self, name):
        self.name = name


class FakeStorageClient:
    """Answers like `google.cloud.storage.Client` for the one call used."""

    def __init__(self, names, raises=None):
        self.names = sorted(names)
        self.raises = raises
        self.calls = []

    def list_blobs(self, bucket, prefix=None, timeout=None, **kwargs):
        self.calls.append((bucket, prefix, timeout))
        if self.raises is not None:
            raise self.raises
        for name in self.names:
            if prefix is None or name.startswith(prefix):
                yield _Blob(name)


def _objects(latest_by_symbol, *, days_of_history=3):
    """Realistic object names: one per symbol per session up to its latest."""
    names = []
    for symbol, latest in latest_by_symbol.items():
        for back in range(days_of_history):
            day = latest - timedelta(days=back)
            names.append(f"{PREFIX}/{symbol}/{day.isoformat()}.parquet")
    return names


@pytest.fixture
def monitor(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAIN_LAKE_BUCKET", BUCKET)
    monkeypatch.delenv("CHAIN_LAKE_PREFIX", raising=False)
    monkeypatch.setattr(rm, "_utcnow",
                        lambda: datetime(2026, 8, 31, 15, 45, tzinfo=timezone.utc))
    return RegressionMonitor(service_url="http://test", api_key="k")


@pytest.fixture
def universe(monkeypatch):
    """Stub the profile lookup — the check must not need a settings file."""
    class _Config:
        def __init__(self, path):
            self.stock_symbols = list(UNIVERSE)

    import src.utils.config as config_module
    monkeypatch.setattr(config_module, "Config", _Config)
    return UNIVERSE


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr(
        rm.logger, "warning",
        lambda msg, **kw: seen.append(("warning", msg, kw)))
    return seen


def _client(monitor, monkeypatch, client):
    monkeypatch.setattr(monitor, "_lake_storage_client", lambda: client)
    return client


def _only(results):
    assert len(results) == 1, f"expected exactly one result, got {results}"
    return results[0]


# --------------------------------------------------------------------------- #
class TestFresh:
    def test_a_current_lake_passes(self, monitor, universe, monkeypatch):
        client = _client(monitor, monkeypatch, FakeStorageClient(
            _objects({s: date(2026, 8, 29) for s in UNIVERSE})))
        result = _only(monitor.check_lake_freshness())

        assert result.status == "pass"
        assert result.name == "lake_freshness"
        assert result.details["latest_chain_day"] == "2026-08-29"
        assert result.details["age_days"] == 2
        assert result.details["symbols_checked"] == 3
        assert len(client.calls) == 3, "one prefix scan per live symbol"
        assert client.calls[0][1] == f"{PREFIX}/AAPL/"

    def test_exactly_at_the_window_is_still_fresh(self, monitor, universe,
                                                  monkeypatch):
        """Strictly greater, like deploy_freshness: the window is a grace period."""
        at_limit = date(2026, 8, 31) - timedelta(days=rm.LAKE_FRESHNESS_MAX_DAYS)
        _client(monitor, monkeypatch, FakeStorageClient(
            _objects({s: at_limit for s in UNIVERSE})))
        result = _only(monitor.check_lake_freshness())
        assert result.status == "pass"
        assert result.details["age_days"] == rm.LAKE_FRESHNESS_MAX_DAYS

    def test_one_stale_symbol_does_not_fail_a_current_lake(self, monitor,
                                                           universe, monkeypatch):
        """The verdict is the newest day ACROSS the universe.

        A single symbol lagging is a data question (it may have been delisted,
        or have no options that month); the Job having stopped is the thing this
        control exists to see, and that shows up on every symbol at once. The
        per-symbol detail is still reported so the narrower question is
        answerable from the same result.
        """
        _client(monitor, monkeypatch, FakeStorageClient(_objects({
            "AAPL": date(2026, 8, 29),
            "MSFT": date(2026, 8, 29),
            "SPY": date(2025, 1, 2),
        })))
        result = _only(monitor.check_lake_freshness())
        assert result.status == "pass"
        assert result.details["per_symbol_latest"]["SPY"] == "2025-01-02"


class TestStale:
    def test_beyond_the_window_fails(self, monitor, universe, monkeypatch, events):
        _client(monitor, monkeypatch, FakeStorageClient(
            _objects({s: date(2026, 8, 10) for s in UNIVERSE})))
        result = _only(monitor.check_lake_freshness())

        assert result.status == "fail", (
            "`fail` is what makes /regression return 500 — this is the case it "
            "is reserved for"
        )
        assert result.name == "lake_freshness_stale"
        assert result.details["age_days"] == 21
        assert "backfill-weekly" in result.message

    def test_an_empty_lake_fails(self, monitor, universe, monkeypatch):
        _client(monitor, monkeypatch, FakeStorageClient([]))
        result = _only(monitor.check_lake_freshness())
        assert result.status == "fail"
        assert result.name == "lake_freshness_stale"
        assert result.details["latest_chain_day"] is None

    def test_objects_outside_the_universe_do_not_make_it_look_fresh(
            self, monitor, universe, monkeypatch):
        """A symbol nobody trades cannot vouch for the live universe."""
        names = _objects({s: date(2026, 8, 10) for s in UNIVERSE})
        names += [f"{PREFIX}/NOTLIVE/2026-08-30.parquet"]
        _client(monitor, monkeypatch, FakeStorageClient(names))
        assert _only(monitor.check_lake_freshness()).status == "fail"

    def test_an_unparseable_object_name_is_ignored_not_read_as_fresh(
            self, monitor, universe, monkeypatch):
        names = _objects({s: date(2026, 8, 10) for s in UNIVERSE})
        names += [
            f"{PREFIX}/AAPL/README.txt",
            f"{PREFIX}/AAPL/tomorrow.parquet",
            f"{PREFIX}/AAPL/2026-13-45.parquet",   # parses as a name, not a date
        ]
        _client(monitor, monkeypatch, FakeStorageClient(names))
        result = _only(monitor.check_lake_freshness())
        assert result.status == "fail"
        assert result.details["latest_chain_day"] == "2026-08-10"


class TestDegraded:
    """Every one of these is a `warn` — and none of them is silent."""

    def test_no_bucket_configured(self, monitor, monkeypatch, events):
        monkeypatch.delenv("CHAIN_LAKE_BUCKET", raising=False)
        result = _only(monitor.check_lake_freshness())
        assert result.status == "warn"
        assert result.name == "lake_freshness_unconfigured"
        assert _reasons(events) == ["no_bucket"]

    def test_a_profile_without_a_universe(self, monitor, monkeypatch, events):
        class _Config:
            def __init__(self, path):
                self.stock_symbols = []

        import src.utils.config as config_module
        monkeypatch.setattr(config_module, "Config", _Config)
        result = _only(monitor.check_lake_freshness())
        assert result.status == "warn"
        assert result.name == "lake_freshness_no_universe"
        assert _reasons(events) == ["no_universe"]

    def test_a_profile_that_will_not_load(self, monitor, monkeypatch, events):
        class _Config:
            def __init__(self, path):
                raise FileNotFoundError(path)

        import src.utils.config as config_module
        monkeypatch.setattr(config_module, "Config", _Config)
        result = _only(monitor.check_lake_freshness())
        assert result.status == "warn"
        assert result.name == "lake_freshness_read_error"
        assert _reasons(events) == ["bad_profile"]

    def test_no_gcs_client(self, monitor, universe, monkeypatch, events):
        def _boom():
            raise ImportError("no google.cloud.storage in this image")

        monkeypatch.setattr(monitor, "_lake_storage_client", _boom)
        result = _only(monitor.check_lake_freshness())
        assert result.status == "warn"
        assert result.name == "lake_freshness_read_error"
        assert _reasons(events) == ["no_client"]

    def test_a_listing_that_raises(self, monitor, universe, monkeypatch, events):
        _client(monitor, monkeypatch,
                FakeStorageClient([], raises=RuntimeError("503 backend error")))
        result = _only(monitor.check_lake_freshness())
        assert result.status == "warn", (
            "a GCS outage says nothing about whether the backfill ran; failing "
            "here would 500 the whole monitor"
        )
        assert result.name == "lake_freshness_read_error"
        assert _reasons(events) == ["list_failed"]

    def test_the_error_message_carries_no_exception_text(self, monitor, universe,
                                                         monkeypatch, events):
        """Only the class, never `str(exc)` — deploy_freshness's rule.

        The listing failure could echo a signed URL or a header value into its
        message, and CheckResults are returned over HTTP and written to Cloud
        Logging.
        """
        secret = "Bearer ya29.SUPERSECRET"
        _client(monitor, monkeypatch,
                FakeStorageClient([], raises=RuntimeError(secret)))
        result = _only(monitor.check_lake_freshness())
        assert "SUPERSECRET" not in result.message
        assert "RuntimeError" in result.message


class TestPrefix:
    def test_a_custom_prefix_is_honoured(self, monitor, universe, monkeypatch):
        monkeypatch.setenv("CHAIN_LAKE_PREFIX", "chains/v2")
        client = _client(monitor, monkeypatch, FakeStorageClient(
            [f"chains/v2/{s}/2026-08-29.parquet" for s in UNIVERSE]))
        result = _only(monitor.check_lake_freshness())
        assert result.status == "pass"
        assert client.calls[0][1] == "chains/v2/AAPL/"

    def test_the_default_prefix_matches_the_stores(self, monitor, universe,
                                                   monkeypatch):
        """A drift here would scan an empty prefix and report a dead lake."""
        from src.backtesting.data.chain_store import DEFAULT_LAKE_PREFIX

        client = _client(monitor, monkeypatch, FakeStorageClient([]))
        monitor.check_lake_freshness()
        assert client.calls[0][1] == f"{DEFAULT_LAKE_PREFIX}/AAPL/"


def _reasons(events):
    return [kw.get("reason") for _, _, kw in events
            if kw.get("event_type") == "lake_freshness_degraded"]
