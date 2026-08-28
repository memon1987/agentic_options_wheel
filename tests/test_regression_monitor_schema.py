"""Schema + dataset pins for the regression monitor's BigQuery reads (FC-082).

`/regression` is a deployed detective control invoked hourly by Cloud
Scheduler, so a query it can never execute is not a dev-tool annoyance — it is
an alarm that reports `warn` forever and gets muted by habit. That is exactly
what happened: `check_trade_execution` filtered the trades table on
`timestamp_iso`, a column that has never existed in
`src/data/trade_journal._TABLE_SCHEMA` (the field is `timestamp`, TIMESTAMP).

Two things are pinned here:

1. **Schema/monitor drift fails the suite, not production.** Every bare column
   identifier in the trade-execution SQL is checked against the code-defined
   `_TABLE_SCHEMA` field names. Rename or drop a column in the journal and this
   goes red in CI instead of warn-degrading hourly in Cloud Run.
2. **The dataset is the running profile's**, not a hardcoded `"options_wheel"`.
   The fold-in half of FC-082: a covered-call service's `/regression` would
   otherwise validate the *wheel's* tables and report pass on them.

Deliberately NOT pinned here: `check_performance_baseline`, whose two queries
read `event_category` / `metric_name` / `metric_value` off the trades table.
Those columns exist in no code-defined schema in this repo and the metrics have
no BigQuery table at all (they are Cloud Logging only), so that check is dead
at the data-source level rather than the column level — see the KNOWN DEAD note
on the method. Asserting its current SQL would pin a broken check in place.
"""

import re

import pytest

import tools.testing.regression_monitor as rm
from tools.testing.regression_monitor import RegressionMonitor, resolve_bq_dataset


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

# SQL grammar, not column references. `timestamp` is deliberately absent: it is
# a real column and must be validated against the schema like any other.
_SQL_GRAMMAR_TOKENS = {
    "select", "from", "where", "order", "by", "asc", "desc", "limit",
    "and", "or", "not", "null", "is", "as", "on",
    "timestamp_sub", "timestamp_add", "current_timestamp", "interval",
    "second", "minute", "hour", "day",
}


def _referenced_columns(sql: str) -> set:
    """Bare column identifiers in `sql`, ignoring grammar and the table ref.

    The backticked `project.dataset.table` name is stripped first so the
    fully-qualified reference does not masquerade as a column.
    """
    body = re.sub(r"`[^`]*`", " ", sql)
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body)
    return {t for t in tokens if t.lower() not in _SQL_GRAMMAR_TOKENS}


class _FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


@pytest.fixture
def captured_sql(monkeypatch):
    """Record every SQL string the monitor sends, returning zero rows.

    Zero rows makes `check_trade_execution` return after its query, so the
    check touches no HTTP endpoint and no live BigQuery (conftest forbids both).
    """
    bigquery = pytest.importorskip("google.cloud.bigquery")
    queries = []

    class _RecordingClient:
        def __init__(self, *args, **kwargs):
            pass

        def query(self, sql):
            queries.append(sql)
            return _FakeQueryJob([])

    monkeypatch.setattr(bigquery, "Client", _RecordingClient)
    return queries


def _trade_execution_sql(captured_sql, dataset="options_wheel"):
    monitor = RegressionMonitor(
        service_url="http://test", api_key="k", bq_dataset=dataset,
    )
    monitor.check_trade_execution()
    assert len(captured_sql) == 1, f"expected one trade query, got {captured_sql}"
    return captured_sql[0]


# ---------------------------------------------------------------------------
# 1. The trade-execution check's SQL matches the trades schema
# ---------------------------------------------------------------------------

def test_trade_execution_sql_only_references_real_columns(captured_sql):
    """Every column the check filters/sorts on exists in `_TABLE_SCHEMA`."""
    from src.data.trade_journal import _TABLE_SCHEMA, _HAS_BIGQUERY

    if not _HAS_BIGQUERY:
        pytest.skip("BigQuery not installed — _TABLE_SCHEMA is empty by design")

    schema_columns = {f.name for f in _TABLE_SCHEMA}
    referenced = _referenced_columns(_trade_execution_sql(captured_sql))

    assert referenced, "SQL references no columns — the extractor is broken"
    unknown = referenced - schema_columns
    assert not unknown, (
        f"regression monitor queries columns absent from the trades schema: "
        f"{sorted(unknown)}. Either the column was renamed in "
        f"src/data/trade_journal._TABLE_SCHEMA or the monitor drifted — the "
        f"live symptom is an hourly 'BigQuery trade query failed' warn."
    )


def test_trade_execution_filters_on_the_timestamp_column(captured_sql):
    """The FC-082 defect itself: `timestamp_iso` does not exist."""
    sql = _trade_execution_sql(captured_sql)

    assert "timestamp_iso" not in sql, (
        "timestamp_iso is not a column in the trades table; the check "
        "warn-degraded on every hourly run for as long as it was there"
    )
    assert "timestamp" in _referenced_columns(sql), (
        "the check must still bound its window on the timestamp column"
    )
    # Compared as a TIMESTAMP, not as an ISO string — the column's real type.
    assert "TIMESTAMP_SUB" in sql.upper()


def test_duplicate_detection_reads_the_timestamp_column():
    """The Python side of the same fix: row keys follow the SQL."""
    from datetime import datetime, timezone

    ts = datetime(2026, 8, 21, 15, 30, tzinfo=timezone.utc)

    assert rm.trade_timestamp({"timestamp": ts}) == ts
    # A JSON/CSV path hands back a string; both parse to the same instant.
    assert rm.trade_timestamp({"timestamp": "2026-08-21T15:30:00+00:00"}) == ts
    assert rm.trade_timestamp({"timestamp": "2026-08-21T15:30:00Z"}) == ts
    # Naive input is read as UTC, matching BigQuery's storage.
    assert rm.trade_timestamp({"timestamp": "2026-08-21T15:30:00"}) == ts
    # The dead column, a missing cell and garbage all read as "unknown" —
    # those rows are skipped, never compared against a fabricated time.
    assert rm.trade_timestamp({"timestamp_iso": "2026-08-21T15:30:00Z"}) is None
    assert rm.trade_timestamp({}) is None
    assert rm.trade_timestamp({"timestamp": "not-a-timestamp"}) is None


# ---------------------------------------------------------------------------
# 2. The dataset is derived from the running profile
# ---------------------------------------------------------------------------

def test_wheel_profile_resolves_to_the_wheel_dataset(monkeypatch):
    monkeypatch.delenv("BQ_DATASET", raising=False)
    monkeypatch.setenv("STRATEGY_CONFIG", "config/settings.yaml")

    assert resolve_bq_dataset() == "options_wheel"


def test_covered_call_profile_resolves_to_its_own_dataset(monkeypatch):
    """The fold-in defect: this used to return the wheel's dataset."""
    monkeypatch.delenv("BQ_DATASET", raising=False)
    monkeypatch.setenv("STRATEGY_CONFIG", "config/covered_call.yaml")

    assert resolve_bq_dataset() == "covered_call"


def test_env_override_wins_over_the_profile(monkeypatch):
    """BQ_DATASET stays an explicit operator override for ad-hoc runs."""
    monkeypatch.setenv("STRATEGY_CONFIG", "config/covered_call.yaml")
    monkeypatch.setenv("BQ_DATASET", "some_other_dataset")

    assert resolve_bq_dataset() == "some_other_dataset"


def test_queries_run_against_the_profiles_dataset(monkeypatch, captured_sql):
    """End to end: the resolved dataset reaches the SQL the monitor sends."""
    monkeypatch.delenv("BQ_DATASET", raising=False)
    monkeypatch.setenv("STRATEGY_CONFIG", "config/covered_call.yaml")

    monitor = RegressionMonitor(service_url="http://test", api_key="k")
    monitor.check_trade_execution()

    sql = captured_sql[0]
    assert ".covered_call.trades`" in sql
    assert "options_wheel" not in sql, (
        "a covered-call service's /regression must not validate the wheel's tables"
    )


def test_module_has_no_hardcoded_dataset_default():
    """The constant this replaced escaped the Seam 4 grep gate (env-wrapped).

    Re-adding `BQ_DATASET = os.environ.get("BQ_DATASET", "options_wheel")` at
    module scope restores the cross-profile read, so it fails here.
    """
    assert not hasattr(rm, "BQ_DATASET"), (
        "module-level BQ_DATASET is back; the dataset must come from "
        "resolve_bq_dataset() so it follows the running profile"
    )


# ---------------------------------------------------------------------------
# 3. Every check group appears in the report
# ---------------------------------------------------------------------------

# The report's `check_groups` keys are the contract that `/regression`'s
# consumers (the hourly scheduler's 500-on-fail semantics, and any human
# reading the JSON) see. A group silently dropped from `run_all_checks` is a
# detective control that stops running with no other symptom — so the roster is
# pinned here rather than left implicit.
EXPECTED_CHECK_GROUPS = [
    "endpoint_health",
    "trade_execution",
    "log_analysis",
    "position_reconciliation",
    "performance_baseline",
    "risk_parameters",
    "deploy_freshness",
]

_GROUP_METHODS = {
    "endpoint_health": "check_endpoints",
    "trade_execution": "check_trade_execution",
    "log_analysis": "check_logs",
    "position_reconciliation": "check_position_reconciliation",
    "performance_baseline": "check_performance_baseline",
    "risk_parameters": "check_risk_parameters",
    "deploy_freshness": "check_deploy_freshness",
}


def test_run_all_checks_reports_every_group(monkeypatch):
    """`run_all_checks` runs — and reports — exactly the expected roster.

    Each check method is stubbed to return no results, so this exercises group
    registration without touching HTTP, BigQuery or GitHub.
    """
    monitor = RegressionMonitor(service_url="http://test", api_key="k")
    for method in _GROUP_METHODS.values():
        monkeypatch.setattr(monitor, method, lambda: [])

    report = monitor.run_all_checks()

    assert list(report["check_groups"].keys()) == EXPECTED_CHECK_GROUPS


def test_deploy_freshness_group_is_registered():
    """FC-081 follow-up: the merged-vs-deployed check must actually be wired.

    Writing `check_deploy_freshness` and forgetting to register it would leave
    the exact silence the check exists to break.
    """
    for group in EXPECTED_CHECK_GROUPS:
        assert hasattr(RegressionMonitor, _GROUP_METHODS[group]), group

    source = __import__("inspect").getsource(RegressionMonitor.run_all_checks)
    assert '"deploy_freshness": self.check_deploy_freshness' in source
