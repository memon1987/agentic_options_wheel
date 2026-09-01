"""The dashboard's artifact endpoint (FC-096 Phase B B2).

Same split as ``tests/test_dashboard_sweeps.py`` and for the same reason:
FastAPI is absent from the CI image this suite runs in, so anything worth
getting wrong lives in ``dashboard/backend/services/artifacts.py`` and is
exercised directly here; the endpoint tests are class-scoped-skipped rather
than module-scoped, so a missing FastAPI cannot silently skip the pure tests
above them too.

What is actually at risk:

* a path segment that escapes the object prefix (``../``, a slash, a run id
  that is really a path);
* an object name that disagrees with the WRITER's, which presents as a 404 on
  an artifact that exists — the most confusing possible symptom;
* "absent" and "unreadable" collapsing into one answer, which sends an operator
  to the wrong place;
* a GCS client constructed at import, which would make this module unimportable
  in a test environment with no credentials.
"""

from __future__ import annotations

import gzip
import importlib.util
import json

import pytest

# See tests/_dashboard_path.py: the backend is APPENDED and the repo root kept
# ahead of it, so `import main` still resolves to the CLI.
from tests._dashboard_path import add_dashboard_backend_to_path  # noqa: E402

add_dashboard_backend_to_path()

from services import artifacts as A  # noqa: E402
from services import sweeps as S  # noqa: E402

from src.backtesting.reporting import artifact_store as engine_store  # noqa: E402
from src.backtesting.scenarios import identity as engine_identity  # noqa: E402


PAYLOAD = {"schema": 1, "provenance": {"scenario": "base"}, "daily": []}


@pytest.fixture(autouse=True)
def _no_real_gcs(monkeypatch):
    """No test in this file may build a real GCS client.

    The same class of guard `tests/conftest.py` puts on live BigQuery: a test
    that quietly reached the network would pass on a developer laptop with
    credentials and fail (or, worse, cost money) anywhere else. Every test here
    injects its own fake client; this makes the omission of one loud.
    """
    from google.cloud import storage

    def _refuse(*_a, **_k):
        raise AssertionError(
            "a real GCS client was constructed — inject a fake instead")

    monkeypatch.setattr(storage, "Client", _refuse)


class FakeNotFound(Exception):
    pass


class FakeBlob:
    def __init__(self, data, raises=None):
        self.data = data
        self.raises = raises

    def download_as_bytes(self, timeout=None):
        if self.raises is not None:
            raise self.raises
        if self.data is None:
            from google.cloud.exceptions import NotFound

            raise NotFound("no such object")
        return self.data


class FakeBucket:
    def __init__(self, objects, raises=None):
        self.objects = objects
        self.raises = raises
        self.requested = []

    def blob(self, name):
        self.requested.append(name)
        return FakeBlob(self.objects.get(name), raises=self.raises)


class FakeGCS:
    def __init__(self, objects=None, raises=None):
        self.bucket_obj = FakeBucket(objects or {}, raises=raises)
        self.asked_for = []

    def bucket(self, name):
        self.asked_for.append(name)
        return self.bucket_obj


def _store(objects=None, raises=None, bucket="test-bucket"):
    gcs = FakeGCS(objects, raises=raises)
    return A.ArtifactStore(bucket=bucket, client=gcs), gcs


def _object(run_id="r1", scenario="base", symbol="AAPL", split="all"):
    return engine_identity.artifact_object_name(run_id, scenario, symbol, split)


# --------------------------------------------------------------------------- #
# 1. The name is the WRITER's name
# --------------------------------------------------------------------------- #
class TestTheReaderAndTheWriterAgree:
    def test_the_endpoint_builds_the_object_name_the_engine_writes(self):
        """One function, imported by both sides. A second implementation here
        would drift, and its drift would 404 an artifact that exists."""
        assert A.object_name("r1", "tighter", "AAPL", "holdout") == (
            engine_identity.artifact_object_name("r1", "tighter", "AAPL", "holdout"))
        assert A.object_name("r1", "tighter", "AAPL", "holdout") == (
            "sim-artifacts/v1/r1/tighter__AAPL__holdout.json.gz")

    def test_the_bucket_default_matches_the_writers(self):
        """Duplicated (the engine module imports structlog and the GCS SDK at
        module scope), so pinned equal — the `ENGINE_VERSION` treatment."""
        assert A.DEFAULT_ARTIFACT_BUCKET == engine_store.DEFAULT_ARTIFACT_BUCKET
        assert A.ARTIFACT_BUCKET_ENV == engine_store.ARTIFACT_BUCKET_ENV

    def test_the_splits_are_the_runners_three(self):
        """A fourth split would build a name that can only 404."""
        from src.backtesting.scenarios.runner import _windows
        from datetime import date

        produced = {w[0] for w in _windows(date(2024, 1, 1), date(2024, 6, 1), None)}
        produced |= {w[0] for w in _windows(date(2024, 1, 1), date(2024, 6, 1),
                                            date(2024, 4, 1))}
        assert produced == set(A.SPLITS)


# --------------------------------------------------------------------------- #
# 2. Path validation
# --------------------------------------------------------------------------- #
class TestPathValidation:
    def test_the_happy_path_normalises_the_symbol(self):
        assert A.validate_path("r1", "base", "aapl", "all") == (
            "r1", "base", "AAPL", "all")

    @pytest.mark.parametrize("run_id", [
        "", "a/b", "../etc", "a" * 65, "r?1", "r 1", "r.1",
    ])
    def test_a_run_id_that_is_really_a_path_is_refused(self, run_id):
        """This value becomes a path segment in an object name. A charset check,
        not just a length check, is what stops a request escaping the prefix."""
        with pytest.raises(A.ArtifactPathError, match="run_id"):
            A.validate_path(run_id, "base", "AAPL", "all")

    @pytest.mark.parametrize("scenario", [
        "bad__name", "-leading", "has space", "a/b", "x" * 41, "",
    ])
    def test_the_scenario_is_checked_by_the_engines_own_validator(self, scenario):
        """Including the `__` rule: a name that could not have been SUBMITTED
        cannot be requested either, so the object-name parser's assumption holds
        on both ends of the pipe."""
        with pytest.raises(A.ArtifactPathError):
            A.validate_path("r1", scenario, "AAPL", "all")

    @pytest.mark.parametrize("symbol", ["", "aa pl", "TOOLONGSYMBOL1234", "A/B",
                                        "1AAP"])
    def test_a_bad_symbol_is_refused(self, symbol):
        with pytest.raises(A.ArtifactPathError, match="symbol"):
            A.validate_path("r1", "base", symbol, "all")

    @pytest.mark.parametrize("symbol", ["AAPL", "BRK.B", "RDS-A", "F"])
    def test_real_tickers_are_accepted(self, symbol):
        assert A.validate_path("r1", "base", symbol, "all")[2] == symbol

    @pytest.mark.parametrize("split", ["", "ALL", "everything", "../all"])
    def test_an_unknown_split_is_refused(self, split):
        with pytest.raises(A.ArtifactPathError, match="split"):
            A.validate_path("r1", "base", "AAPL", split)

    def test_the_dashboards_spec_validator_refuses_a_double_underscore_too(self):
        """The other copy of the rule: `services/sweeps.validate_spec` runs the
        same imported validator at submit time, so the two ends cannot drift."""
        spec = {
            "symbols": ["AAPL"], "start": "2025-08-01", "end": "2026-07-31",
            "scenarios": [{"name": "bad__name", "overrides": {}}],
        }
        with pytest.raises(Exception) as exc:
            S.validate_spec(spec)
        assert "__" in str(exc.value)

    def test_a_single_underscore_still_submits_and_still_addresses(self):
        spec = {
            "symbols": ["AAPL"], "start": "2025-08-01", "end": "2026-07-31",
            "scenarios": [{"name": "long_dte", "overrides": {}}],
        }
        S.validate_spec(spec)
        assert A.object_name("r1", "long_dte", "AAPL", "all").endswith(
            "long_dte__AAPL__all.json.gz")


# --------------------------------------------------------------------------- #
# 3. Reading
# --------------------------------------------------------------------------- #
class TestTheStore:
    def test_it_decompresses_the_stored_gzip(self):
        name = _object()
        store, gcs = _store({name: gzip.compress(json.dumps(PAYLOAD).encode())})
        got = store.fetch("r1", "base", "AAPL", "all")
        assert json.loads(got) == PAYLOAD
        assert gcs.bucket_obj.requested == [name]

    def test_a_plain_json_object_is_served_as_is(self):
        """Tolerated rather than 500'd: an artifact written by a future writer
        that stopped compressing is still readable JSON, and serving it beats
        refusing it."""
        name = _object()
        store, _ = _store({name: json.dumps(PAYLOAD).encode()})
        assert json.loads(store.fetch("r1", "base", "AAPL", "all")) == PAYLOAD

    def test_absence_is_none_and_nothing_else_is(self):
        """`None` is reserved for "not there". A permissions failure or a dead
        bucket RAISES, because the two send an operator to different places."""
        store, _ = _store({})
        assert store.fetch("r1", "base", "AAPL", "all") is None

        boom, _ = _store({}, raises=RuntimeError("403 forbidden"))
        with pytest.raises(RuntimeError, match="403"):
            boom.fetch("r1", "base", "AAPL", "all")

    def test_an_empty_bucket_env_disables_the_store(self, monkeypatch):
        monkeypatch.setenv(A.ARTIFACT_BUCKET_ENV, "")
        assert A.artifact_bucket() is None
        assert A.ArtifactStore().enabled is False

    def test_an_unset_bucket_env_is_the_default_bucket(self, monkeypatch):
        monkeypatch.delenv(A.ARTIFACT_BUCKET_ENV, raising=False)
        assert A.artifact_bucket() == A.DEFAULT_ARTIFACT_BUCKET

    def test_importing_the_module_constructs_no_client(self, monkeypatch):
        """`services/bigquery.py`'s idiom. A client at import time makes this
        module unimportable wherever there are no credentials — which is every
        test environment, and therefore no tests."""
        A._reset_for_tests()
        store = A.get_artifact_store()
        assert store._client is None
        assert A.get_artifact_store() is store
        A._reset_for_tests()

    def test_the_decoder_recognises_gzip_by_its_magic_number(self):
        assert A.decode(gzip.compress(b"{}")) == b"{}"
        assert A.decode(b"{}") == b"{}"


# --------------------------------------------------------------------------- #
# 4. `artifacts_complete` is the SECOND additive column, on the same terms
# --------------------------------------------------------------------------- #
class TestTheAdditiveColumnDegrade:
    """PR-a shipped an `engine_identity`-shaped degrade; PR-b adds a column.

    The window is identical and so is the failure mode: the Job's writer owns
    the schema, its reconcile adds the column, and between a dashboard deploy
    and that reconcile the live table does not have it —
    `insert_rows_json` then rejects the WHOLE request over one unknown key and
    EVERY submit 500s. A degrade keyed on the literal string `engine_identity`
    would not have covered `artifacts_complete`, so the predicate is a closed
    SET now.
    """

    @staticmethod
    def _service(client):
        """A `BigQueryService` with no constructor run — no credentials, no
        client build, just the two attributes this path reads."""
        from services import bigquery as bq_mod

        service = bq_mod.BigQueryService.__new__(bq_mod.BigQueryService)
        service.dataset = "p.d"
        service.client = client
        return service

    class _Unmigrated:
        """A table missing `missing`; it names the first one it trips over."""

        def __init__(self, missing):
            self.missing = list(missing)
            self.rows = []

        def insert_rows_json(self, table, rows):
            self.rows.append(dict(rows[0]))
            for column in self.missing:
                if column in rows[0]:
                    return [{"index": 0, "errors": [
                        {"message": f"no such field: {column}."}]}]
            return []

    def test_the_predicate_names_either_additive_column(self):
        assert S.missing_optional_column(
            Exception("400 no such field: artifacts_complete.")
        ) == "artifacts_complete"
        assert S.missing_optional_column(
            Exception("400 Unrecognized name: engine_identity at [3:1]")
        ) == "engine_identity"

    def test_the_predicate_stays_narrow(self):
        """A degrade that caught everything would turn a permissions failure, a
        missing table and a typo'd column into a silently narrowed row."""
        for other in (
            Exception("403 Access Denied: Table scenario_sweeps"),
            Exception("404 Not found: Table x.y.scenario_sweeps"),
            Exception("400 no such field: artifacts_complte."),
            Exception("artifacts_complete is fine but the network died"),
        ):
            assert S.missing_optional_column(other) is None, other

    def test_a_table_without_artifacts_complete_still_lands_the_row(self, caplog):
        """MUTATION CHECK: revert the closed-set generalisation and this fails —
        which is exactly the outage PR-a had to be patched for."""
        import logging

        client = self._Unmigrated(["artifacts_complete"])
        service = self._service(client)
        row = S.submitted_row(
            run_id="r", spec=S.validate_spec(_spec()), sweep_key_value="k",
            submitted_at="2026-09-01T12:00:00+00:00", git_commit="abc",
            engine_identity="0123456789abcdef")
        assert "artifacts_complete" in row

        S._reset_engine_identity_warning()
        with caplog.at_level(logging.ERROR, logger=S.__name__):
            service.insert_sweep_status(row)   # must NOT raise

        assert len(client.rows) == 2, "one failed insert, then one retry"
        assert "artifacts_complete" not in client.rows[1]
        # ...and nothing ELSE was shed. A retry that dropped more than the one
        # unsupported field would write a row the results view renders blank.
        assert set(client.rows[1]) == set(row) - {"artifacts_complete"}
        assert client.rows[1]["engine_identity"] == "0123456789abcdef"
        assert any("artifacts_complete_column_missing" in r.getMessage()
                   for r in caplog.records), caplog.text

    def test_a_table_missing_BOTH_columns_sheds_both(self, caplog):
        """The bound is one retry per additive column, so a dataset that
        predates both migrations still submits."""
        import logging

        client = self._Unmigrated(["engine_identity", "artifacts_complete"])
        service = self._service(client)
        row = S.submitted_row(
            run_id="r", spec=S.validate_spec(_spec()), sweep_key_value="k",
            submitted_at="2026-09-01T12:00:00+00:00", git_commit="abc",
            engine_identity="0123456789abcdef")

        S._reset_engine_identity_warning()
        with caplog.at_level(logging.ERROR, logger=S.__name__):
            service.insert_sweep_status(row)

        assert len(client.rows) == 3
        assert set(client.rows[-1]) == set(row) - {"engine_identity",
                                                   "artifacts_complete"}
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "engine_identity_column_missing" in messages
        assert "artifacts_complete_column_missing" in messages

    def test_it_does_not_spin_when_the_table_keeps_naming_a_gone_field(self):
        """A table that answers with the same missing column after it has
        already been removed must surface, not loop."""
        calls = []

        class AlwaysBroken:
            def insert_rows_json(self, table, rows):
                calls.append(dict(rows[0]))
                return [{"index": 0, "errors": [
                    {"message": "no such field: artifacts_complete."}]}]

        service = self._service(AlwaysBroken())
        S._reset_engine_identity_warning()
        with pytest.raises(Exception, match="insert failed"):
            service.insert_sweep_status({"run_id": "r",
                                         "artifacts_complete": None})
        assert len(calls) == 2

    def test_an_unrelated_insert_error_still_raises(self):
        class Denied:
            def insert_rows_json(self, table, rows):
                return [{"index": 0, "errors": [
                    {"message": "no such field: sweep_ky."}]}]

        service = self._service(Denied())
        with pytest.raises(Exception, match="insert failed"):
            service.insert_sweep_status({"run_id": "r"})

    def test_every_named_column_is_a_real_nullable_column(self):
        """A closed set that named a column the schema does not have would be a
        degrade path nothing can ever reach — and a column that is REQUIRED
        could not be dropped from a row at all."""
        pytest.importorskip("google.cloud.bigquery")
        from src.backtesting.scenarios import persist as store

        declared = {f.name: f for f in store._sweeps_schema()}
        for column, column_type in S.ADDITIVE_OPTIONAL_COLUMNS:
            assert column in declared, column
            assert store._canonical_type(declared[column].field_type) == (
                store._canonical_type(column_type)), column
            assert store._canonical_mode(declared[column].mode) == "NULLABLE"

    def test_the_row_the_api_writes_carries_the_flag_as_null(self):
        """The API has run nothing, so it cannot know — but the column must be
        PRESENT, or the two writers diverge by whichever one knew a field."""
        row = S.submitted_row(
            run_id="r", spec=S.validate_spec(_spec()), sweep_key_value="k",
            submitted_at="2026-09-01T12:00:00+00:00", git_commit="abc")
        assert row["artifacts_complete"] is None


def _spec(**overrides):
    base = {
        "symbols": ["AAPL"],
        "start": "2025-08-01",
        "end": "2026-07-31",
        "scenarios": [{"name": "tighter",
                       "overrides": {"strategy.min_put_premium": 0.75}}],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# 5. The endpoint
# --------------------------------------------------------------------------- #
_HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None


@pytest.mark.skipif(not _HAS_FASTAPI,
                    reason="FastAPI only present in the dashboard image")
class TestTheArtifactEndpoint:
    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.new_event_loop().run_until_complete(coro)

    def _get(self, v2, run_id="r1", scenario="base", symbol="AAPL", split="all"):
        return self._run(v2.sweep_cell_artifact(run_id, scenario, symbol, split))

    @pytest.fixture
    def wired(self, monkeypatch):
        import routers.v2 as v2

        monkeypatch.setenv(A.ARTIFACT_BUCKET_ENV, "test-bucket")
        A._reset_for_tests()
        yield v2
        A._reset_for_tests()

    def test_200_returns_the_decompressed_json(self, wired, monkeypatch):
        """Decompressed server-side and served as `application/json` — see
        `services/artifacts.py` for why the gzip is not passed through."""
        name = _object()
        store, _ = _store({name: gzip.compress(json.dumps(PAYLOAD).encode())})
        monkeypatch.setattr(A, "get_artifact_store", lambda: store)

        response = self._get(wired)
        assert response.status_code == 200
        assert response.media_type == "application/json"
        assert json.loads(response.body) == PAYLOAD
        assert response.headers["x-artifact-object"] == name
        assert response.headers["cache-control"] == "no-store"

    def test_404_when_the_object_is_absent(self, wired, monkeypatch):
        """The normal answer for an errored cell, a pre-artifact run, and a CLI
        run without `--persist` — and the detail says all three."""
        from fastapi import HTTPException

        store, _ = _store({})
        monkeypatch.setattr(A, "get_artifact_store", lambda: store)
        with pytest.raises(HTTPException) as exc:
            self._get(wired)
        assert exc.value.status_code == 404
        assert "--persist" in exc.value.detail

    @pytest.mark.parametrize("kwargs", [
        {"run_id": "../etc"}, {"scenario": "bad__name"}, {"symbol": "a b"},
        {"split": "everything"},
    ])
    def test_400_on_a_path_segment_that_cannot_address_an_artifact(
            self, wired, monkeypatch, kwargs):
        from fastapi import HTTPException

        store, gcs = _store({})
        monkeypatch.setattr(A, "get_artifact_store", lambda: store)
        with pytest.raises(HTTPException) as exc:
            self._get(wired, **kwargs)
        assert exc.value.status_code == 400
        assert gcs.bucket_obj.requested == [], "GCS must not be touched at all"

    def test_503_when_no_bucket_is_configured(self, wired, monkeypatch):
        from fastapi import HTTPException

        # `bucket=""` is the off switch. `bucket=None` means "read the env",
        # which is the DEFAULT, not the disabled state — the same distinction
        # `artifact_bucket()` draws between unset and explicitly empty.
        monkeypatch.setattr(A, "get_artifact_store",
                            lambda: A.ArtifactStore(bucket=""))
        with pytest.raises(HTTPException) as exc:
            self._get(wired)
        assert exc.value.status_code == 503
        assert A.ARTIFACT_BUCKET_ENV in exc.value.detail

    def test_502_when_the_read_itself_fails(self, wired, monkeypatch):
        """Unreadable is not absent. A blanket 404 here would tell an operator
        their sweep never wrote artifacts when in fact the grant is missing."""
        from fastapi import HTTPException

        store, _ = _store({}, raises=RuntimeError("403 forbidden"))
        monkeypatch.setattr(A, "get_artifact_store", lambda: store)
        with pytest.raises(HTTPException) as exc:
            self._get(wired)
        assert exc.value.status_code == 502
        assert "403" in exc.value.detail

    def test_the_route_is_registered_where_the_plan_says(self, wired):
        paths = {r.path for r in wired.router.routes}
        assert ("/sweeps/{run_id}/artifacts/{scenario}/{symbol}/{split}"
                in paths)

    def test_it_does_not_shadow_the_sweep_detail_route(self, wired, monkeypatch):
        """`/sweeps/{run_id}` is registered BEFORE the artifact route, and
        Starlette matches routes in order — so assert the longer path still
        reaches its own handler rather than being swallowed by the shorter one.

        Proven by dispatch, not by reading the table: the artifact handler is
        the only one that can produce this 404 detail.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        store, _ = _store({})
        monkeypatch.setattr(A, "get_artifact_store", lambda: store)

        app = FastAPI()
        app.include_router(wired.router, prefix="/api/v2")
        routes = [r.path for r in app.routes if "sweeps" in getattr(r, "path", "")]
        assert "/api/v2/sweeps/{run_id}" in routes
        assert routes.index("/api/v2/sweeps/{run_id}") < routes.index(
            "/api/v2/sweeps/{run_id}/artifacts/{scenario}/{symbol}/{split}")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v2/sweeps/r1/artifacts/base/AAPL/all")
        assert response.status_code == 404
        assert "--persist" in response.json()["detail"], (
            "a different handler answered — the artifact route is shadowed")
