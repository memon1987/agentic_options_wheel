"""FC-075 Seam 4 — BigQuery write-side dataset threading + the strategy_id column.

Each test names the regression it catches. The seam's whole point is that "a
covered-call process cannot write the wheel's dataset" stops being a promise
(DD-7's interlock, deleted by this plan) and becomes a property of the writers:
required constructor args with no defaults, a fail-closed analytics singleton,
and writers that cannot conjure a dataset. T1 and T7 are the only standing
guards on that property — weakening either re-opens the hole.

T-items map to docs/plans/fc-075-seam-4.md §Test requirements:
  T1  required constructor args          T5b fail-closed pre-provisioning pin
  T2  singleton lifecycle                T6  wheel construction values
  T3  row stamping                       T7  repo-wide lint gate
  T4  schema parity
(T5a — the inverted DD-7 interlock tests — lives in tests/test_fc075_phase2.py,
next to the tests it replaces. T8 is the full suite.)
"""

import importlib
import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# google-cloud-storage is not installed in the test env (only bigquery). The
# server + OpportunityStore import it at module load; stub so imports succeed.
try:  # pragma: no cover
    from google.cloud import storage  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules.setdefault("google", types.ModuleType("google"))
    _cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    _storage = types.ModuleType("google.cloud.storage")
    _storage.Client = Mock
    sys.modules["google.cloud.storage"] = _storage
    _cloud.storage = _storage

from src.data import analytics_writer as aw
from src.data.activities_ingestor import ActivitiesIngestor
from src.data.analytics_writer import AnalyticsWriter
from src.data.portfolio_history_ingestor import PortfolioHistoryIngestor
from src.data.stock_history_ingestor import StockHistoryIngestor
from src.data.trade_journal import TradeJournal
from src.utils.config import Config

REPO = Path(__file__).resolve().parent.parent

#: Every service writer whose dataset must come from config, never a default.
WRITERS = (AnalyticsWriter, TradeJournal, ActivitiesIngestor,
           PortfolioHistoryIngestor, StockHistoryIngestor)

#: The five writer modules T7 sweeps for a re-introduced dataset default.
WRITER_SOURCES = tuple(
    REPO / "src" / "data" / f"{name}.py" for name in (
        "analytics_writer", "trade_journal", "activities_ingestor",
        "portfolio_history_ingestor", "stock_history_ingestor",
    )
)


def _writer_args(cls):
    """Positional args a writer needs before its keyword-only pair."""
    return (MagicMock(),) if cls is not AnalyticsWriter and cls is not TradeJournal else ()


@pytest.fixture
def no_gcp_project(monkeypatch):
    """No ambient project → every writer disables itself at init, no client."""
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)


# --------------------------------------------------------------------------- #
# T1 — required constructor args
# --------------------------------------------------------------------------- #

class TestRequiredConstructorArgs:
    """A silent dataset default is how a second profile writes the wheel's
    tables. Required keyword-only args turn a missed construction site into a
    TypeError at build time instead of contamination at write time."""

    @pytest.mark.parametrize("cls", WRITERS, ids=lambda c: c.__name__)
    def test_construction_without_the_pair_raises(self, cls, no_gcp_project):
        args = _writer_args(cls)
        with pytest.raises(TypeError):
            cls(*args)
        with pytest.raises(TypeError):
            cls(*args, dataset_id="covered_call")
        with pytest.raises(TypeError):
            cls(*args, strategy_id="covered_call")

    @pytest.mark.parametrize("cls", WRITERS, ids=lambda c: c.__name__)
    def test_both_values_are_stored(self, cls, no_gcp_project):
        w = cls(*_writer_args(cls), dataset_id="covered_call",
                strategy_id="covered_call")
        # Stored before the no-project early return: a disabled writer still
        # reports the identity it was constructed with.
        assert w._dataset_id == "covered_call"
        assert w._strategy_id == "covered_call"
        assert w.enabled is False

    def test_dataset_id_is_keyword_only(self, no_gcp_project):
        # Positionally-supplied datasets would let an old call site keep
        # compiling while meaning something else.
        with pytest.raises(TypeError):
            TradeJournal("proj", "trades", "covered_call", "covered_call")


# --------------------------------------------------------------------------- #
# T2 — singleton lifecycle
# --------------------------------------------------------------------------- #

class TestAnalyticsSingletonLifecycle:
    """The singleton has no config in scope of its own. Unconfigured must mean
    no-op, never `options_wheel` — and must not even build a client, because a
    real writer against a placeholder dataset would create tables under
    whatever credentials happen to be ambient."""

    def test_unconfigured_returns_disabled_sentinel_and_builds_no_client(self):
        with patch("google.cloud.bigquery.Client") as client_cls, \
             patch.object(aw, "logger") as log:
            writer = aw.get_analytics_writer()
            assert isinstance(writer, aw._DisabledAnalyticsWriter)
            assert writer.enabled is False
            # The load-bearing assertion: _enabled is False alone would also be
            # true of a writer that had already created a junk dataset.
            client_cls.assert_not_called()
            events = [k.get("event_type") for _, k in log.warning.call_args_list]
            assert events == ["analytics_writer_unconfigured"]
            # Warn-once: the sentinel path is uncached, so a per-call warning
            # would be one log line per decision-record flush, forever.
            aw.get_analytics_writer()
            assert [k.get("event_type") for _, k in log.warning.call_args_list] \
                == ["analytics_writer_unconfigured"]

    def test_unconfigured_sentinel_is_not_cached(self):
        with patch("google.cloud.bigquery.Client"):
            aw.get_analytics_writer()
        assert aw._instance is None, (
            "caching the sentinel would make a late configure() a permanent no-op")

    def test_configured_singleton_uses_the_configured_dataset(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT", "test-project")
        with patch("google.cloud.bigquery.Client", return_value=MagicMock()):
            aw.configure_analytics_writer(dataset_id="covered_call",
                                          strategy_id="covered_call")
            writer = aw.get_analytics_writer()
            assert writer.enabled is True
            assert writer.dataset_id == "covered_call"
            assert writer.strategy_id == "covered_call"
            assert aw.get_analytics_writer() is writer  # cached now

    def test_configure_after_construction_with_different_values_raises(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT", "test-project")
        with patch("google.cloud.bigquery.Client", return_value=MagicMock()):
            # Before construction, re-pointing is silent: nothing has been
            # written under the old values.
            aw.configure_analytics_writer(dataset_id="options_wheel",
                                          strategy_id="wheel")
            aw.configure_analytics_writer(dataset_id="covered_call",
                                          strategy_id="covered_call")
            aw.get_analytics_writer()
            # After construction it is a hard error — one process, one profile.
            with pytest.raises(RuntimeError):
                aw.configure_analytics_writer(dataset_id="options_wheel",
                                              strategy_id="wheel")
            # Re-declaring the SAME profile stays legal (idempotent entry points).
            aw.configure_analytics_writer(dataset_id="covered_call",
                                          strategy_id="covered_call")

    def test_thread_local_override_still_wins(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT", "test-project")
        sentinel = object()
        aw.configure_analytics_writer(dataset_id="options_wheel",
                                      strategy_id="wheel")
        previous = aw.set_analytics_writer(sentinel)
        try:
            # A backtest replay must never be routed to the live writer, and a
            # live cycle must never be routed to the replay's recorder.
            assert aw.get_analytics_writer() is sentinel
        finally:
            aw.set_analytics_writer(previous)

    def test_reset_for_tests_clears_configuration_and_instance(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT", "test-project")
        with patch("google.cloud.bigquery.Client", return_value=MagicMock()):
            aw.configure_analytics_writer(dataset_id="covered_call",
                                          strategy_id="covered_call")
            aw.get_analytics_writer()
        aw._reset_for_tests()
        assert aw._instance is None
        assert aw._configured_dataset_id is None
        assert aw._configured_strategy_id is None
        assert aw._warned_unconfigured is False


# --------------------------------------------------------------------------- #
# T3 — row stamping
# --------------------------------------------------------------------------- #

def _analytics_writer(strategy_id="covered_call"):
    w = AnalyticsWriter.__new__(AnalyticsWriter)
    w._enabled = True
    w._strategy_id = strategy_id
    w._dataset_id = "covered_call"
    w._tables = {"errors": "TREF", "decision_events": "TREF"}
    w._client = MagicMock()
    w._client.insert_rows_json.return_value = []
    return w


def _trade_journal(strategy_id="covered_call"):
    tj = TradeJournal.__new__(TradeJournal)
    tj._enabled = True
    tj._strategy_id = strategy_id
    tj._client = MagicMock()
    tj._client.insert_rows_json.return_value = []
    tj._table_ref = "proj.covered_call.trades"
    return tj


def _activities_ingestor(strategy_id="covered_call"):
    ing = ActivitiesIngestor.__new__(ActivitiesIngestor)
    ing.alpaca = MagicMock()
    ing._project_id = "test-project"
    ing._dataset_id = "covered_call"
    ing._strategy_id = strategy_id
    ing._table_ref = MagicMock()
    ing._client = MagicMock()
    ing._client.insert_rows_json.return_value = []
    ing._enabled = True
    ing._read_cursor = MagicMock(return_value=(None, True))
    ing._existing_ids = MagicMock(return_value=set())
    return ing


def _portfolio_ingestor(strategy_id="covered_call"):
    ing = PortfolioHistoryIngestor.__new__(PortfolioHistoryIngestor)
    ing.alpaca = MagicMock()
    ing._project_id = "test-project"
    ing._dataset_id = "covered_call"
    ing._strategy_id = strategy_id
    ing._table_ref = MagicMock()
    ing._client = MagicMock()
    ing._client.insert_rows_json.return_value = []
    ing._enabled = True
    ing._existing_dates = MagicMock(return_value=set())
    return ing


def _stock_ingestor(strategy_id="covered_call"):
    ing = StockHistoryIngestor.__new__(StockHistoryIngestor)
    ing.alpaca = MagicMock()
    ing._project_id = "test-project"
    ing._dataset_id = "covered_call"
    ing._strategy_id = strategy_id
    ing._table_ref = MagicMock()
    ing._client = MagicMock()
    ing._client.insert_rows_json.return_value = []
    ing._enabled = True
    ing._traded_universe = MagicMock(return_value=["AMD"])
    ing._max_date_per_symbol = MagicMock(return_value={})
    return ing


class TestRowStamping:
    """A writer whose rows silently lose attribution is a dataset that cannot
    answer "which strategy wrote this" after the fact."""

    def test_analytics_single_row_stamped(self):
        w = _analytics_writer()
        w.write_error(event_type="boom", error_type="x")
        row = w._client.insert_rows_json.call_args[0][1][0]
        assert row["strategy_id"] == "covered_call"

    def test_analytics_batch_rows_stamped(self):
        w = _analytics_writer()
        w.write_decision_events([
            {"dedup_key": "R1|NVDA|scan", "symbol": "NVDA"},
            {"dedup_key": "R1|AMD|scan", "symbol": "AMD"},
        ])
        args, kwargs = w._client.insert_rows_json.call_args
        assert [r["strategy_id"] for r in args[1]] == ["covered_call"] * 2
        # dedup keys are the streaming insertIds; stamping must not touch them.
        assert kwargs["row_ids"] == ["R1|NVDA|scan", "R1|AMD|scan"]

    def test_trade_journal_row_stamped(self):
        tj = _trade_journal()
        tj.record_trade({"symbol": "META", "type": "call", "contracts": 1,
                         "option_symbol": "META260220C00600000",
                         "strike_price": 600, "premium": 3.10})
        row = tj._client.insert_rows_json.call_args[0][1][0]
        assert row["strategy_id"] == "covered_call"
        assert row["option_type"] == "call"      # FC-067 labeling still holds

    def test_activities_rows_stamped(self):
        ing = _activities_ingestor()
        ing.alpaca.get_account_activities.return_value = [{
            "id": "act-1", "activity_type": "FILL",
            "transaction_time": "2026-08-21T14:30:00.123Z", "date": "2026-08-21",
            "symbol": "META260220C00600000", "side": "sell_short",
            "qty": "1", "price": "3.10", "order_id": "o-1",
        }]
        result = ing.run_once()
        assert result["inserted"] == 1
        args, kwargs = ing._client.insert_rows_json.call_args
        assert args[1][0]["strategy_id"] == "covered_call"
        # activity_id remains the idempotency key.
        assert kwargs["row_ids"] == ["act-1"]

    def test_portfolio_history_rows_stamped(self):
        ing = _portfolio_ingestor()
        ing._fetch = MagicMock(return_value={
            "timestamp": [1750000000], "equity": [101000.0],
            "profit_loss": [100.0], "profit_loss_pct": [0.001],
            "base_value": 100000.0, "base_value_asof": "2026-01-02",
        })
        result = ing.run_once()
        assert result["inserted"] == 1
        args, kwargs = ing._client.insert_rows_json.call_args
        assert args[1][0]["strategy_id"] == "covered_call"
        assert kwargs["row_ids"] == [args[1][0]["date"]]  # date is the natural PK

    def test_stock_history_rows_stamped_and_insert_ids_unchanged(self):
        ing = _stock_ingestor()
        ing._fetch_bars = MagicMock(return_value=[{
            "t": "2026-08-20T04:00:00Z", "o": 150.0, "h": 155.0,
            "l": 149.0, "c": 154.25, "v": 1_234_567,
        }])
        result = ing.run_once()
        assert result["rows_inserted"] == 1
        args, kwargs = ing._client.insert_rows_json.call_args
        assert args[1][0]["strategy_id"] == "covered_call"
        # The dedup key must not change meaning: still (date|symbol), never
        # widened with strategy_id, or every historical row re-inserts.
        assert kwargs["row_ids"] == ["2026-08-20|AMD"]


# --------------------------------------------------------------------------- #
# T4 — schema parity
# --------------------------------------------------------------------------- #

class TestSchemaParity:
    """Phase 3 creates the covered-call tables from these code-defined schemas.
    A schema missing strategy_id would create a table that rejects its own
    writer's rows — BigQuery streaming inserts fail on unknown fields."""

    def _schemas(self):
        from src.data import activities_ingestor, portfolio_history_ingestor
        from src.data import stock_history_ingestor, trade_journal
        schemas = dict(aw._SCHEMAS)
        schemas["trades"] = trade_journal._TABLE_SCHEMA
        schemas["trades_from_activities"] = activities_ingestor._SCHEMA
        schemas["equity_history_from_alpaca"] = portfolio_history_ingestor._SCHEMA
        schemas["stock_history_from_alpaca"] = stock_history_ingestor._SCHEMA
        return schemas

    def test_all_seven_code_defined_schemas_carry_strategy_id(self):
        if not aw._HAS_BIGQUERY:
            pytest.skip("bigquery not installed")
        schemas = self._schemas()
        assert len(schemas) == 7, f"expected 7 code-defined schemas, got {sorted(schemas)}"
        for table, schema in schemas.items():
            fields = {f.name: f for f in schema}
            assert "strategy_id" in fields, f"{table} has no strategy_id column"
            field = fields["strategy_id"]
            assert field.field_type == "STRING", table
            # NULLABLE is what makes the live ALTER additive and pre-Seam-4
            # rows legal; REQUIRED would reject every historical row.
            assert field.mode == "NULLABLE", table


# --------------------------------------------------------------------------- #
# T5b — fail-closed pre-provisioning pin (the property that replaces DD-7)
# --------------------------------------------------------------------------- #

class TestFailClosedBeforeProvisioning:
    """Before Phase 3 provisions the covered_call dataset, a covered-call
    process must be a safe no-op toward BigQuery — with zero possibility of
    writing options_wheel. This is what DD-7's interlock used to buy."""

    def test_client_construction_failure_disables_the_writers(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT", "test-project")
        with patch("google.cloud.bigquery.Client",
                   side_effect=RuntimeError("no credentials")):
            tj = TradeJournal(dataset_id="covered_call", strategy_id="covered_call")
            writer = AnalyticsWriter(dataset_id="covered_call", strategy_id="covered_call")
        assert tj.enabled is False and writer.enabled is False
        tj.record_trade({"symbol": "META", "type": "call", "contracts": 1})
        writer.write_error(event_type="boom")   # must not raise, must not write

    def test_missing_dataset_disables_the_writers_and_writes_nothing(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT", "test-project")
        from google.api_core.exceptions import NotFound

        client = MagicMock()
        client.create_table.side_effect = NotFound("dataset covered_call not found")
        with patch("google.cloud.bigquery.Client", return_value=client):
            tj = TradeJournal(dataset_id="covered_call", strategy_id="covered_call")
            writer = AnalyticsWriter(dataset_id="covered_call", strategy_id="covered_call")

        assert tj.enabled is False and writer.enabled is False
        tj.record_trade({"symbol": "META", "type": "call", "contracts": 1})
        writer.write_error(event_type="boom")
        writer.write_decision_events([{"dedup_key": "R1|META|scan", "symbol": "META"}])
        # No insert was attempted against ANY dataset — least of all the wheel's.
        client.insert_rows_json.assert_not_called()
        # And the writers never tried to conjure the dataset themselves.
        client.create_dataset.assert_not_called()

    def test_no_writer_creates_a_dataset(self):
        # Structural, not mock-level: create_dataset under operator ADC would
        # side-step Phase 3 provisioning and IAM entirely.
        for path in WRITER_SOURCES:
            assert "create_dataset" not in path.read_text(), (
                f"{path.name} creates a BigQuery dataset; datasets are an "
                "operator provisioning step (FC-075 Seam 4, MEDIUM-A)")


# --------------------------------------------------------------------------- #
# T6 — the wheel's construction values are unchanged
# --------------------------------------------------------------------------- #

flask = pytest.importorskip("flask")


@pytest.fixture
def server():
    mod = importlib.import_module("deploy.cloud_run_server")
    mod.reset_strategy_state()
    yield mod
    mod.reset_strategy_state()


class TestWheelConstructionValues:
    """The threading must not invert wheel behavior: settings.yaml carries no
    `bigquery` block, so the wheel's dataset comes from Config's default."""

    def test_settings_yaml_still_resolves_to_the_wheel_dataset(self):
        c = Config(str(REPO / "config" / "settings.yaml"))
        assert (c.bigquery_dataset, c.strategy_id) == ("options_wheel", "wheel")

    def test_execution_engine_journal_built_from_config(self):
        from src.strategy import execution_engine as ee

        config = Config(str(REPO / "config" / "settings.yaml"))
        with patch.object(ee, "TradeJournal") as journal_cls:
            ee.ExecutionEngine(Mock(), config)
        _, kwargs = journal_cls.call_args
        assert kwargs == {"dataset_id": "options_wheel", "strategy_id": "wheel"}

    def test_strategy_config_configures_the_singleton(self, server, monkeypatch):
        monkeypatch.delenv("STRATEGY_CONFIG", raising=False)
        server.reset_strategy_state()
        server.strategy_config()
        assert aw._configured_dataset_id == "options_wheel"
        assert aw._configured_strategy_id == "wheel"

    @pytest.mark.parametrize("endpoint,module,name", [
        ("/ingest-activities", "src.data.activities_ingestor", "ActivitiesIngestor"),
        ("/ingest-portfolio-history", "src.data.portfolio_history_ingestor",
         "PortfolioHistoryIngestor"),
        ("/ingest-stock-history", "src.data.stock_history_ingestor",
         "StockHistoryIngestor"),
    ])
    def test_ingestors_built_from_config(self, server, monkeypatch,
                                         endpoint, module, name):
        monkeypatch.delenv("STRATEGY_CONFIG", raising=False)
        monkeypatch.delenv("STRATEGY_API_KEY", raising=False)
        server.reset_strategy_state()
        alpaca = Mock()
        alpaca.get_account.return_value = {"account_number": "PA3D36DVXSZ2"}
        ingestor_cls = Mock()
        ingestor_cls.return_value.enabled = False   # stop before any real work
        with patch("src.api.alpaca_client.AlpacaClient", return_value=alpaca), \
             patch(f"{module}.{name}", ingestor_cls):
            server.app.test_client().post(endpoint)
        _, kwargs = ingestor_cls.call_args
        assert kwargs["dataset_id"] == "options_wheel"
        assert kwargs["strategy_id"] == "wheel"


# --------------------------------------------------------------------------- #
# T7 — repo-wide lint gate
# --------------------------------------------------------------------------- #

#: Deliberately cross-strategy and out of Seam 4's scope: a local measurement
#: tool run under operator credentials, writing one shared `backtest_runs`
#: table keyed by engine_version. No service endpoint reaches it.
T7_DATASET_DEFAULT_ALLOWLIST = {"src/backtesting/reporting/bq_writer.py"}

T7_ROOTS = ("src", "deploy", "tools", "scripts")

#: `dataset_id = "..."` in a signature or assignment from a literal — the exact
#: shape of the defect this seam removed.
_DATASET_DEFAULT = re.compile(r"""dataset_id\s*(?::\s*[\w\[\], .]+\s*)?=\s*["']""")

_DD7_SYMBOL = re.compile(r"\b(writes_isolated|require_write_isolation)\b")


def _python_sources():
    for root in T7_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            yield path, str(path.relative_to(REPO))


class TestLintGate:
    """DD-7's replacement is an emergent property, not a single control:
    required kwargs + no defaults + the configure hand-off. This gate and T1
    are what keep it emergent rather than accidental."""

    def test_no_dataset_default_survives_outside_the_allowlist(self):
        offenders = []
        for path, rel in _python_sources():
            if rel in T7_DATASET_DEFAULT_ALLOWLIST:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if _DATASET_DEFAULT.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        assert offenders == [], (
            "a defaulted dataset is how a second strategy profile silently "
            "writes the wheel's tables (FC-075 Seam 4, DD-1):\n"
            + "\n".join(offenders))

    def test_the_allowlist_entry_is_real(self):
        # A stale allowlist would silently widen the gate.
        for rel in T7_DATASET_DEFAULT_ALLOWLIST:
            path = REPO / rel
            assert path.exists(), rel
            assert _DATASET_DEFAULT.search(path.read_text()), (
                f"{rel} no longer has a dataset default — drop it from the allowlist")

