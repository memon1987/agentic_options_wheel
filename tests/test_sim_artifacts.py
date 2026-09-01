"""Per-cell detail artifacts (FC-096 Phase B B2).

An artifact is EVIDENCE. Every test here is about a way a correct-looking
artifact could tell a reader something false:

* a ledger serialised under one fill assumption sitting next to a row's
  ``bid_fill_return`` from another, with nothing on the object saying so;
* a DTE-7 arm's artifact stamped with the reach some OTHER arm in the same sweep
  asked for, describing a chain this cell never saw;
* a cycle table re-derived here rather than by ``build_cycles``, quietly
  disagreeing with the scorecard the row came from;
* an errored cell whose missing artifact makes a healthy run look incomplete —
  or a genuinely incomplete set that nothing flags;
* a sink failure taking down the cell whose evidence it was.

The frozen-fixture test is the schema contract: adding a field is additive and
updates the fixture, removing or renaming one is a schema bump.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from src.backtesting.data.chain_store import ChainStore
from src.backtesting.engine.broker import BacktestBroker
from src.backtesting.engine.simulator import DailyState, SimulationResult
from src.backtesting.metrics.cycles import build_cycles
from src.backtesting.reporting import artifact_store as store_module
from src.backtesting.reporting.artifact import (
    ARTIFACT_SCHEMA,
    MID_FILL_BASIS,
    ArtifactMeta,
    cell_artifact,
)
from src.backtesting.reporting.artifact_store import (
    DEFAULT_ARTIFACT_BUCKET,
    ArtifactWriter,
    artifact_bytes,
)
from src.backtesting.scenarios import Scenario, run_sweep
from src.backtesting.scenarios.identity import (
    artifact_object_name,
    parse_artifact_object_name,
    validate_scenario_name,
)
from src.utils.config import Config

from .test_scenarios import _MultiSymbolProvider  # noqa: F401 - fixture plumbing
from .test_backtest_simulator import ScriptedProvider, _weekdays

FIXTURE = Path(__file__).parent / "fixtures" / "sim_artifact_schema_v1.json"


# --------------------------------------------------------------------------- #
# A hand-built result that exercises EVERY ledger kind.
# --------------------------------------------------------------------------- #
def _hand_built_result() -> SimulationResult:
    """One underlying driven through every branch the broker can record.

    Hand-built rather than replayed because the frozen-fixture test has to see
    all seven ``LedgerEvent`` kinds in one object, and no short replay reliably
    produces an early assignment AND an expiry AND a buy-to-close. The broker is
    the real one, so the ``detail`` payloads are the real ones.
    """
    broker = BacktestBroker(100_000.0, fees_per_contract=0.04, fill_haircut=0.25)

    # 1. sell_put_open, then 2. buy_to_close.
    put_a = "AAA240607P00100000"
    broker.sell_put_to_open(put_a, "AAA", 100.0, date(2024, 6, 7), 1,
                            mark=2.0, bid=1.8, opened=date(2024, 6, 3))
    broker.buy_to_close(put_a, 1, mark=1.0, ask=1.2, close_date=date(2024, 6, 4))

    # 3. put_assignment (settles ITM).
    put_b = "AAA240614P00100000"
    broker.sell_put_to_open(put_b, "AAA", 100.0, date(2024, 6, 14), 1,
                            mark=2.5, bid=2.3, opened=date(2024, 6, 5))
    broker.settle_expirations(date(2024, 6, 14), {"AAA": 90.0})

    # 4. sell_call_open, 5. dividend, 6. expire_worthless (settles OTM).
    call_a = "AAA240621C00105000"
    broker.sell_call_to_open(call_a, "AAA", 105.0, date(2024, 6, 21), 1,
                             mark=1.5, bid=1.3, opened=date(2024, 6, 17))
    broker.credit_dividend("AAA", 0.5, date(2024, 6, 18))
    broker.settle_expirations(date(2024, 6, 21), {"AAA": 100.0})

    # 7. call_assignment, via the ex-dividend early-assignment path.
    call_b = "AAA240628C00104000"
    broker.sell_call_to_open(call_b, "AAA", 104.0, date(2024, 6, 28), 1,
                             mark=1.2, bid=1.0, opened=date(2024, 6, 24))
    broker.assign_call_early(call_b, date(2024, 6, 26), reason="ex_dividend")

    daily = [
        DailyState(day=date(2024, 6, 3), equity=100_000.0, cash=100_000.0,
                   reserved_collateral=10_000.0, open_options=1,
                   shares_held={}),
        DailyState(day=date(2024, 6, 14), equity=99_500.0, cash=89_800.0,
                   reserved_collateral=0.0, open_options=0,
                   shares_held={"AAA": 100}),
        DailyState(day=date(2024, 6, 26), equity=100_200.0, cash=100_200.0,
                   reserved_collateral=0.0, open_options=0,
                   shares_held={"AAA": 0}),
    ]
    return SimulationResult(
        symbols=["AAA"], start=date(2024, 6, 3), end=date(2024, 6, 28),
        starting_cash=100_000.0, daily=daily, broker=broker,
        # Ranked, as `RejectionTally.summary()` returns it.
        rejections={"premium_below_floor": 5, "delta_out_of_range": 2},
        candidate_days=9,
        dividends_credited=50.0,
        early_assignments=1,
        unpriced_ex_div_calls=1,
        rolls_evaluated=3,
        rolls_executed=1,
        roll_records=[{
            "date": date(2024, 6, 24), "underlying": "AAA",
            "from_symbol": call_a, "to_symbol": call_b,
            "net_credit": 0.35, "strike_change": -1.0,
        }],
        earnings_symbols_without_data=["AAA"],
        earnings_symbols_past_horizon=[],
    )


def _meta(**kw) -> ArtifactMeta:
    base = dict(
        run_id="run0123456789ab", scenario="base", symbol="AAA", split="all",
        scenario_hash="s" * 16, config_hash="c" * 16,
        engine_identity="e" * 16, arm_max_dte=7, sweep_max_dte=21,
        window_start=date(2024, 6, 3), window_end=date(2024, 6, 28),
        fill_haircut=0.25, starting_cash=100_000.0, git_commit="deadbeef",
    )
    base.update(kw)
    return ArtifactMeta(**base)


# --------------------------------------------------------------------------- #
# 1. Round trip
# --------------------------------------------------------------------------- #
class TestTheArtifactRoundTrips:
    def test_gzipped_json_decodes_back_to_the_same_object(self):
        """The bytes on the object are the payload, exactly.

        MUTATION CHECK: make `artifact_bytes` drop `default=str` and this fails
        on the `roll_records` date rather than silently writing an object no
        reader can parse.
        """
        payload = cell_artifact(_hand_built_result(), _meta())
        raw = artifact_bytes(payload)
        assert raw[:2] == b"\x1f\x8b", "the object must be gzipped"
        back = json.loads(gzip.decompress(raw))
        assert back == json.loads(json.dumps(payload, sort_keys=True, default=str))

    def test_the_bytes_are_deterministic_for_one_payload(self):
        """No wall-clock inside the gzip header: two writes of one payload are
        byte-identical, so an artifact can be compared or hashed."""
        payload = cell_artifact(_hand_built_result(), _meta())
        assert artifact_bytes(payload) == artifact_bytes(payload)

    def test_every_number_is_finite_or_null(self):
        """`NaN`/`Infinity` are not legal JSON: ONE of them would make the whole
        artifact unparseable, not one field wrong."""
        raw = json.dumps(cell_artifact(_hand_built_result(), _meta()))
        assert "NaN" not in raw and "Infinity" not in raw


# --------------------------------------------------------------------------- #
# 2. The builders are REUSED, not reimplemented
# --------------------------------------------------------------------------- #
class TestTheCycleTableIsTheScorecardsCycleTable:
    def test_cycles_equal_build_cycles_on_the_same_ledger(self):
        """`evaluate._score` scores what `build_cycles(result.broker.ledger)`
        returns; the artifact serialises the same call on the same ledger. A
        second implementation here would drift and the console would quietly
        contradict the row it was opened from.

        MUTATION CHECK: re-derive `option_pnl` in `_cycle_rows` from the ledger
        and this fails on the fee treatment.
        """
        result = _hand_built_result()
        payload = cell_artifact(result, _meta())
        expected = build_cycles(result.broker.ledger)

        assert len(payload["cycles"]) == len(expected)
        for row, cycle in zip(payload["cycles"], expected):
            assert row["underlying"] == cycle.underlying
            assert row["start"] == cycle.start.isoformat()
            assert row["end"] == (cycle.end.isoformat() if cycle.end else None)
            assert row["option_pnl"] == pytest.approx(cycle.option_pnl)
            assert row["stock_pnl"] == pytest.approx(cycle.stock_pnl)
            assert row["dividends"] == pytest.approx(cycle.dividends)
            assert row["fees"] == pytest.approx(cycle.fees)
            assert row["total_pnl"] == pytest.approx(cycle.total_pnl)
            assert row["outcome"] == cycle.outcome()
            assert row["days"] == cycle.days
            assert row["event_count"] == len(cycle.events)

    def test_the_evaluate_scorer_reads_the_same_objects(self):
        """Belt and braces on the claim above: the count the fitness report
        would see and the count the artifact carries are the same number."""
        from src.backtesting import evaluate as evaluate_module

        result = _hand_built_result()
        payload = cell_artifact(result, _meta())
        quality = evaluate_module._data_quality(
            result, build_cycles(result.broker.ledger))
        assert quality["ledger_events"] == payload["counters"]["ledger_events"]
        assert quality["cycles_still_open_at_end"] == sum(
            1 for c in payload["cycles"] if c["is_open"])

    def test_the_ledger_carries_every_event_in_order(self):
        result = _hand_built_result()
        payload = cell_artifact(result, _meta())
        assert [e["kind"] for e in payload["ledger"]] == [
            e.kind for e in result.broker.ledger]
        assert len(payload["ledger"]) == len(result.broker.ledger)


# --------------------------------------------------------------------------- #
# 3. The frozen schema
# --------------------------------------------------------------------------- #
def _key_sets(payload) -> dict:
    detail_kinds: dict = {}
    for event in payload["ledger"]:
        detail_kinds.setdefault(event["kind"], set()).update(event["detail"])
    return {
        "schema": payload["schema"],
        "top_level": sorted(payload),
        "provenance": sorted(payload["provenance"]),
        "provenance.window": sorted(payload["provenance"]["window"]),
        "provenance.masked_reach": sorted(payload["provenance"]["masked_reach"]),
        "provenance.fill": sorted(payload["provenance"]["fill"]),
        "daily": sorted(payload["daily"][0]),
        "ledger": sorted(payload["ledger"][0]),
        "cycles": sorted(payload["cycles"][0]),
        "rejections": sorted(payload["rejections"][0]),
        "counters": sorted(payload["counters"]),
        "earnings_coverage": sorted(payload["earnings_coverage"]),
        "roll_records": sorted(payload["roll_records"][0]),
        "ledger_detail_by_kind": {k: sorted(v) for k, v in sorted(detail_kinds.items())},
    }


class TestTheSchemaIsFrozen:
    def test_the_full_key_set_matches_the_fixture(self):
        """The artifact IS an interface — Phase E's console reads it and a
        stored object outlives the code that wrote it.

        A failure here is not automatically a bug: ADDING a field is additive,
        so update the fixture. REMOVING or RENAMING one breaks every stored
        object, so bump `ARTIFACT_SCHEMA` and `identity.ARTIFACT_PREFIX` too.
        """
        payload = cell_artifact(_hand_built_result(), _meta())
        expected = json.loads(FIXTURE.read_text())
        got = _key_sets(payload)
        assert got["schema"] == expected["schema"] == ARTIFACT_SCHEMA
        for section in sorted(expected):
            assert got[section] == expected[section], section
        assert sorted(got) == sorted(expected), "a whole SECTION appeared or vanished"

    def test_all_seven_ledger_kinds_are_covered_by_the_fixture(self):
        """The `detail` pin is only worth anything if the fixture actually sees
        every kind — a fixture built from a run that never assigned would pin
        four kinds and wave three through."""
        expected = json.loads(FIXTURE.read_text())["ledger_detail_by_kind"]
        assert set(expected) == {
            "sell_put_open", "buy_to_close", "put_assignment", "sell_call_open",
            "dividend", "expire_worthless", "call_assignment",
        }

    def test_shares_held_is_on_every_daily_row(self):
        """M9's item: equity alone cannot tell a cash account from an assigned
        one holding the same dollars, which is the first thing a reader needs
        when looking at a wheel drawdown."""
        payload = cell_artifact(_hand_built_result(), _meta())
        assert all("shares_held" in row for row in payload["daily"])
        assert payload["daily"][1]["shares_held"] == {"AAA": 100}
        # An empty dict, not a missing key: "flat that day" is a fact.
        assert payload["daily"][0]["shares_held"] == {}


# --------------------------------------------------------------------------- #
# 4. Provenance honesty: the fill stamp and the masked reach
# --------------------------------------------------------------------------- #
class TestTheFillAssumptionIsStamped:
    def test_the_stamp_says_mid_and_carries_the_haircut(self):
        """The row's `bid_fill_return` comes from a replay this object is NOT.
        Without the stamp, a reader comparing the two is comparing two different
        runs with no way to know it."""
        payload = cell_artifact(_hand_built_result(), _meta(fill_haircut=0.4))
        assert payload["provenance"]["fill"] == {
            "basis": MID_FILL_BASIS, "fill_haircut": 0.4}
        assert MID_FILL_BASIS == "mid"


class TestTheMaskedReachIsTheArmsNotTheSweeps:
    def test_the_stamp_is_the_arms_reach_and_its_real_cutoff(self):
        payload = cell_artifact(_hand_built_result(),
                                _meta(arm_max_dte=7, sweep_max_dte=21))
        reach = payload["provenance"]["masked_reach"]
        assert reach["max_dte"] == 7
        assert reach["dte_buffer"] == 1
        assert reach["dte_cutoff"] == 8, "7 + UNIVERSE_DTE_BUFFER"
        assert reach["sweep_max_dte"] == 21
        assert reach["dte_cutoff"] not in (21, 22)


# --------------------------------------------------------------------------- #
# 5. The tally
# --------------------------------------------------------------------------- #
class TestTheRejectionTallyIsCompleteAndRanked:
    def test_the_ranking_survives_as_a_list(self):
        """A dict loses the order through any reader that does not preserve
        insertion order, and the order IS the answer: the first reason is the
        binding constraint."""
        payload = cell_artifact(_hand_built_result(), _meta())
        assert payload["rejections"] == [
            {"reason": "premium_below_floor", "days": 5},
            {"reason": "delta_out_of_range", "days": 2},
        ]
        assert payload["binding_constraint"] == "premium_below_floor"

    def test_no_rejections_is_an_empty_list_and_a_null_constraint(self):
        result = _hand_built_result()
        result.rejections = {}
        payload = cell_artifact(result, _meta())
        assert payload["rejections"] == []
        assert payload["binding_constraint"] is None


# --------------------------------------------------------------------------- #
# 6. Object naming, and the `__` rule that makes it parseable
# --------------------------------------------------------------------------- #
class TestTheObjectName:
    def test_the_name_is_the_documented_one(self):
        assert artifact_object_name("run1", "tighter", "AAPL", "holdout") == (
            "sim-artifacts/v1/run1/tighter__AAPL__holdout.json.gz")

    def test_it_round_trips_through_the_rsplit_parser(self):
        name = artifact_object_name("run1", "a.b-c_d", "BRK.B", "fit")
        assert parse_artifact_object_name(name) == {
            "run_id": "run1", "scenario": "a.b-c_d",
            "symbol": "BRK.B", "split": "fit"}

    @pytest.mark.parametrize("bad", ["tighter__AAPL__all.json",
                                     "sim-artifacts/v1/r/only__two.json.gz",
                                     "sim-artifacts/v1/r/.json.gz"])
    def test_a_name_that_is_not_one_of_ours_raises(self, bad):
        with pytest.raises(ValueError):
            parse_artifact_object_name(bad)

    @pytest.mark.parametrize("name", ["a__b", "__lead", "trail__", "x__y__z"])
    def test_a_double_underscore_is_refused_by_the_engine(self, name):
        """`__` is the object-name separator, parsed with `rsplit('__', 2)`. A
        scenario carrying it makes that parse silently wrong, so the NAME rule
        forbids it rather than the writer sanitising it (which would map two
        arms onto one object)."""
        with pytest.raises(ValueError, match="__"):
            validate_scenario_name(name, "test")

    def test_a_single_underscore_is_still_fine(self):
        validate_scenario_name("long_dte", "test")
        validate_scenario_name("a_b_c", "test")

    def test_the_cli_yaml_path_applies_the_same_rule(self):
        """`main._scenarios_from_entries` shares the validator, so a `--persist`
        sweep cannot land a name the API would have refused — and vice versa."""
        import main as main_module

        with pytest.raises(SystemExit, match="__"):
            main_module._scenarios_from_entries(
                [{"name": "bad__name", "overrides": {}}], "scenarios.yaml")


# --------------------------------------------------------------------------- #
# 7. The GCS writer
# --------------------------------------------------------------------------- #
class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name

    def upload_from_string(self, data, content_type=None, timeout=None):
        self.bucket.objects[self.name] = data
        self.bucket.uploads.append(
            {"name": self.name, "content_type": content_type, "timeout": timeout})


class FakeBucket:
    def __init__(self, name, raises=None):
        self.name = name
        self.objects: dict = {}
        self.uploads: list = []
        self.raises = raises

    def blob(self, name):
        if self.raises is not None:
            raise self.raises
        return FakeBlob(self, name)


class FakeGCS:
    def __init__(self, raises=None):
        self.buckets: dict = {}
        self.raises = raises

    def bucket(self, name):
        if name not in self.buckets:
            self.buckets[name] = FakeBucket(name, raises=self.raises)
        return self.buckets[name]


class TestTheArtifactWriter:
    def test_it_writes_the_gzip_at_the_documented_address(self):
        gcs = FakeGCS()
        writer = ArtifactWriter("run1", bucket="b", client=gcs)
        payload = cell_artifact(_hand_built_result(),
                                _meta(run_id="run1", scenario="tighter",
                                      symbol="AAA", split="fit"))
        assert writer.write(payload) is True
        assert writer.written == 1 and writer.failed == 0
        name = "sim-artifacts/v1/run1/tighter__AAA__fit.json.gz"
        assert name in gcs.buckets["b"].objects
        assert json.loads(gzip.decompress(
            gcs.buckets["b"].objects[name]))["schema"] == ARTIFACT_SCHEMA

    def test_the_object_is_opaque_gzip_with_no_content_encoding(self):
        """Declaring `Content-Encoding: gzip` on the OBJECT turns on GCS
        decompressive transcoding, so a download silently returns plain bytes
        and every reader's gzip handling becomes a lie that happens to work."""
        gcs = FakeGCS()
        ArtifactWriter("run1", bucket="b", client=gcs).write(
            cell_artifact(_hand_built_result(), _meta(run_id="run1")))
        upload = gcs.buckets["b"].uploads[0]
        assert upload["content_type"] == "application/gzip"
        assert upload["timeout"] == store_module.ARTIFACT_TIMEOUT_S

    def test_the_address_comes_from_the_payloads_own_provenance(self):
        """The name and the contents cannot disagree about which cell this is —
        the failure that would put arm A's ledger at arm B's address."""
        gcs = FakeGCS()
        writer = ArtifactWriter("ignored", bucket="b", client=gcs)
        writer.write(cell_artifact(_hand_built_result(),
                                   _meta(run_id="real", scenario="s",
                                         symbol="ZZZ", split="all")))
        assert list(gcs.buckets["b"].objects) == [
            "sim-artifacts/v1/real/s__ZZZ__all.json.gz"]

    def test_a_gcs_failure_is_counted_and_swallowed(self):
        """MUTATION CHECK: remove the `except` and a bucket outage kills the
        sweep whose results are already computed."""
        gcs = FakeGCS(raises=RuntimeError("bucket on fire"))
        writer = ArtifactWriter("run1", bucket="b", client=gcs)
        assert writer.write(cell_artifact(_hand_built_result(), _meta())) is False
        assert writer.written == 0 and writer.failed == 1
        assert "bucket on fire" in writer.last_error

    def test_an_empty_bucket_env_disables_it_and_builds_no_client(self, monkeypatch):
        monkeypatch.setenv(store_module.ARTIFACT_BUCKET_ENV, "")
        monkeypatch.setattr(store_module, "_storage_client", _no_client)
        writer = ArtifactWriter("run1")
        assert writer.enabled is False
        assert writer.write(cell_artifact(_hand_built_result(), _meta())) is False
        assert writer.written == 0 and writer.failed == 0

    def test_an_unset_bucket_env_means_the_default_bucket(self, monkeypatch):
        """Unset and empty are DIFFERENT states: the first is the normal
        deployment, the second is the off switch. Collapsing them would make
        the off switch unreachable."""
        monkeypatch.delenv(store_module.ARTIFACT_BUCKET_ENV, raising=False)
        assert store_module.artifact_bucket() == DEFAULT_ARTIFACT_BUCKET
        assert ArtifactWriter("run1").enabled is True

    def test_no_client_is_constructed_until_the_first_write(self, monkeypatch):
        """Constructing a writer must touch nothing: `run_sweep_cmd` builds one
        before it knows whether the sweep will dedup away without replaying a
        single cell.

        The write then DOES reach for a client — and, this being a best-effort
        sink, records the refusal rather than raising it. Both halves are the
        assertion: lazy construction, and a failure that is counted rather than
        propagated.
        """
        monkeypatch.setattr(store_module, "_storage_client", _no_client)
        ArtifactWriter("run1", bucket="b")  # constructing one touches nothing

        writer = ArtifactWriter("run1", bucket="b")
        assert writer.write(
            {"provenance": {"scenario": "s", "symbol": "A", "split": "all"}}
        ) is False
        assert writer.failed == 1
        assert "must not be constructed" in writer.last_error


def _no_client(*_a, **_k):
    raise AssertionError("a GCS client must not be constructed here")


# --------------------------------------------------------------------------- #
# 8. The sink: ordering, error cases, and the masked reach end to end
# --------------------------------------------------------------------------- #
@pytest.fixture
def one_symbol():
    """AAA over 30 sessions with weekly expiries — long enough for a 21-DTE arm
    to have contracts to choose from, short enough to replay in a unit test."""
    warmup = _weekdays(date(2024, 3, 25), 45)
    days = _weekdays(date(2024, 6, 3), 30)
    closes = {d: 100.0 for d in warmup}
    for i, d in enumerate(days):
        closes[d] = 100.0 - min(i, 10) * 3.0
    expirations = [d for d in days if d.weekday() == 4]
    return days, _MultiSymbolProvider({"AAA": ScriptedProvider("AAA", closes, expirations)})


@pytest.fixture
def one_symbol_config():
    config = Config()
    config._config["stocks"]["symbols"] = ["AAA"]
    return config


def _sweep(tmp_path, one_symbol, config, scenarios, **kw):
    days, provider = one_symbol
    return run_sweep(
        config, scenarios, ["AAA"], days[0], days[-1],
        starting_cash=50_000.0,
        chain_store=ChainStore(str(tmp_path)),
        bar_provider=provider, quiet_logs=False, **kw,
    )


class TestTheSink:
    def test_one_artifact_per_non_errored_cell(self, tmp_path, one_symbol,
                                               one_symbol_config):
        seen = []
        result = _sweep(tmp_path, one_symbol, one_symbol_config,
                        [Scenario("tighter", {"strategy.min_put_premium": 0.30})],
                        artifact_sink=seen.append, run_id="R1",
                        engine_identity="EID")
        assert len(result.rows) == 2 and not result.errors
        assert len(seen) == 2
        assert {a["provenance"]["scenario"] for a in seen} == {"base", "tighter"}
        for art in seen:
            assert art["provenance"]["run_id"] == "R1"
            assert art["provenance"]["engine_identity"] == "EID"
            assert art["provenance"]["symbol"] == "AAA"
            assert art["provenance"]["split"] == "all"
            assert art["schema"] == ARTIFACT_SCHEMA

    def test_no_sink_means_no_artifact_work_at_all(self, tmp_path, one_symbol,
                                                   one_symbol_config):
        """The default path must not pay for a module it never calls."""
        with patch("src.backtesting.reporting.artifact.cell_artifact") as built:
            _sweep(tmp_path, one_symbol, one_symbol_config, [Scenario("a", {})])
        built.assert_not_called()

    def test_the_sink_runs_AFTER_the_bid_sensitivity_pass(
            self, tmp_path, one_symbol, one_symbol_config):
        """The plan's ordering requirement, asserted rather than asserted-about.

        `_score` is called once for the mid replay and once for the bid replay.
        A sink that fires before the sensitivity pass would see 1; firing last
        it must see 2. MUTATION CHECK: move the `_emit_artifact` call above the
        `if run_sensitivity:` block and this fails on every cell.
        """
        from src.backtesting.scenarios import runner as runner_module

        scores = []
        real_score = runner_module._score

        def counting_score(*a, **kw):
            scores.append(1)
            return real_score(*a, **kw)

        seen_at = []
        with patch.object(runner_module, "_score", counting_score):
            _sweep(tmp_path, one_symbol, one_symbol_config, [Scenario("a", {})],
                   run_sensitivity=True,
                   artifact_sink=lambda _art: seen_at.append(len(scores)))
        assert seen_at, "the sink was never called"
        # Two `_score` calls per cell, and the sink saw both of its own cell's.
        assert seen_at == [2, 4], seen_at

    def test_only_the_mid_replay_is_serialised(self, tmp_path, one_symbol,
                                               one_symbol_config):
        """One artifact per cell even with sensitivity on, and its stamp says
        `mid`. The bid replay deliberately gets no object."""
        seen = []
        result = _sweep(tmp_path, one_symbol, one_symbol_config,
                        [Scenario("a", {})], run_sensitivity=True,
                        artifact_sink=seen.append)
        assert len(seen) == len(result.rows)
        assert all(a["provenance"]["fill"]["basis"] == "mid" for a in seen)
        assert all(a["provenance"]["fill"]["fill_haircut"] == 0.25 for a in seen)

    def test_a_per_arm_haircut_reaches_the_stamp(self, tmp_path, one_symbol,
                                                 one_symbol_config):
        seen = []
        _sweep(tmp_path, one_symbol, one_symbol_config,
               [Scenario("wide", {}, fill_haircut=0.6)],
               artifact_sink=seen.append)
        by_arm = {a["provenance"]["scenario"]: a for a in seen}
        assert by_arm["wide"]["provenance"]["fill"]["fill_haircut"] == 0.6
        assert by_arm["base"]["provenance"]["fill"]["fill_haircut"] == 0.25

    def test_an_errored_cell_never_calls_the_sink(self, tmp_path, one_symbol,
                                                  one_symbol_config):
        """What makes `artifacts_complete` arithmetic true: an errored cell has
        no replay to serialise, so it is excluded from BOTH sides."""
        from src.backtesting.engine.simulator import Simulator

        real_replay = Simulator.replay

        def boom(self, materialised, **kw):
            if self.config.min_put_premium == 99.0:
                raise RuntimeError("arm blew up")
            return real_replay(self, materialised, **kw)

        seen = []
        with patch.object(Simulator, "replay", boom):
            result = _sweep(tmp_path, one_symbol, one_symbol_config,
                            [Scenario("boom", {"strategy.min_put_premium": 99.0})],
                            artifact_sink=seen.append)
        assert len(result.errors) == 1
        assert len(seen) == 1
        assert {a["provenance"]["scenario"] for a in seen} == {"base"}

    def test_a_raising_sink_does_not_error_the_row(self, tmp_path, one_symbol,
                                                   one_symbol_config):
        """MUTATION CHECK: drop the `try` in `_emit_artifact` and a GCS outage
        turns every completed cell into an error row."""
        def explode(_art):
            raise RuntimeError("sink is down")

        result = _sweep(tmp_path, one_symbol, one_symbol_config,
                        [Scenario("a", {})], artifact_sink=explode)
        assert not result.errors
        assert all(row.total_return is not None for row in result.rows)

    def test_a_serialiser_failure_is_also_swallowed(self, tmp_path, one_symbol,
                                                    one_symbol_config):
        """A bug in `cell_artifact` itself must not cost a run either — it is
        inside the same guarded block."""
        with patch("src.backtesting.reporting.artifact.cell_artifact",
                   side_effect=ValueError("serialiser bug")):
            result = _sweep(tmp_path, one_symbol, one_symbol_config,
                            [Scenario("a", {})], artifact_sink=lambda _a: None)
        assert not result.errors

    def test_the_dte_7_arm_stamps_its_own_reach_not_the_sweeps(
            self, tmp_path, one_symbol, one_symbol_config):
        """The headline masked-reach test, end to end through a MIXED sweep.

        `base` reaches 7; the `longdte` arm reaches 21, so the window is
        materialised at 21 and `base` replays against a view masked back to 7.
        An artifact stamped with 21 (or the 22-day chain cutoff that goes with
        it) would describe a chain `base` never saw — the exact confusion
        FC-096 Phase A PR-2 exists to prevent.

        MUTATION CHECK: pass `materialised.max_dte` instead of the arm's
        `max_dte` into `_emit_artifact` and this fails on the `base` row.
        """
        seen = []
        result = _sweep(tmp_path, one_symbol, one_symbol_config,
                        [Scenario("longdte", {"strategy.put_target_dte": 21})],
                        artifact_sink=seen.append)
        assert not result.errors
        reach = {a["provenance"]["scenario"]: a["provenance"]["masked_reach"]
                 for a in seen}
        assert reach["base"]["max_dte"] == 7
        assert reach["base"]["dte_cutoff"] == 8
        assert reach["base"]["sweep_max_dte"] == 21
        assert reach["longdte"]["max_dte"] == 21
        assert reach["longdte"]["dte_cutoff"] == 22

    def test_a_homogeneous_sweep_stamps_the_same_number_both_ways(
            self, tmp_path, one_symbol, one_symbol_config):
        """With no DTE arm the mask removes nothing, and the arm's reach and the
        sweep's are the same number — which is what makes the mixed case above
        the interesting one rather than the only correct-looking one."""
        seen = []
        _sweep(tmp_path, one_symbol, one_symbol_config, [Scenario("a", {})],
               artifact_sink=seen.append)
        for art in seen:
            reach = art["provenance"]["masked_reach"]
            assert reach["max_dte"] == reach["sweep_max_dte"] == 7

    def test_the_artifact_carries_the_rows_own_hashes(
            self, tmp_path, one_symbol, one_symbol_config):
        """An artifact that cannot be joined back to its `scenario_runs` row is
        evidence for nothing."""
        seen = []
        result = _sweep(tmp_path, one_symbol, one_symbol_config,
                        [Scenario("a", {})], artifact_sink=seen.append)
        by_arm = {a["provenance"]["scenario"]: a for a in seen}
        for row in result.rows:
            prov = by_arm[row.scenario]["provenance"]
            assert prov["scenario_hash"] == row.scenario_hash
            assert prov["config_hash"] == row.config_hash
            assert prov["window"]["start"] == row.start.isoformat()
            assert prov["window"]["end"] == row.end.isoformat()


# --------------------------------------------------------------------------- #
# 9. `artifacts_complete` accounting
# --------------------------------------------------------------------------- #
class _Cell:
    def __init__(self, error=None):
        self.error = error


class _Result:
    def __init__(self, rows):
        self.rows = rows


class _Writer:
    def __init__(self, written, *, enabled=True, failed=0):
        self.written = written
        self.enabled = enabled
        self.failed = failed
        self.last_error = None


class TestArtifactsCompleteAccounting:
    @staticmethod
    def _call(writer, result):
        import structlog

        import main as main_module

        return main_module._artifacts_complete(
            writer, result, structlog.get_logger("t"), run_id="r")

    def test_true_when_every_non_errored_cell_has_one(self):
        assert self._call(_Writer(2), _Result([_Cell(), _Cell()])) is True

    def test_errored_cells_are_excluded_from_both_sides(self):
        """Otherwise one bad arm makes every sweep also report missing
        artifacts — two unrelated problems collapsed into one flag."""
        assert self._call(_Writer(2), _Result([_Cell(), _Cell(), _Cell("boom")])) is True

    def test_false_when_an_artifact_is_missing(self):
        assert self._call(_Writer(1), _Result([_Cell(), _Cell()])) is False

    def test_none_when_nothing_was_supposed_to_be_written(self):
        """`None` and `False` are different facts: "this run stored no evidence
        and was never meant to" must not read as "this run's evidence is
        incomplete"."""
        assert self._call(None, _Result([_Cell()])) is None
        assert self._call(_Writer(0, enabled=False), _Result([_Cell()])) is None
        assert self._call(_Writer(0), None) is None

    def test_the_status_row_carries_the_flag_and_defaults_to_null(self):
        from src.backtesting.scenarios import persist as store

        row = store.status_row(run_id="r", status=store.STATUS_RUNNING,
                               submitted_at="2026-09-01T00:00:00+00:00")
        assert "artifacts_complete" in row
        assert row["artifacts_complete"] is None
        done = store.status_row(run_id="r", status=store.STATUS_DONE,
                                submitted_at="2026-09-01T00:00:00+00:00",
                                artifacts_complete=False)
        assert done["artifacts_complete"] is False

    def test_the_column_is_declared_in_the_schema(self):
        pytest.importorskip("google.cloud.bigquery")
        from src.backtesting.scenarios import persist as store

        field = {f.name: f for f in store._sweeps_schema()}["artifacts_complete"]
        assert store._canonical_type(field.field_type) == "BOOL"
        assert store._canonical_mode(field.mode) == "NULLABLE", (
            "additive columns must be nullable")


# --------------------------------------------------------------------------- #
# 10. Layer 2: no `--persist`, no writes anywhere
# --------------------------------------------------------------------------- #
class TestTheCliWritesNothingWithoutPersist:
    def _invoke(self, tmp_path, one_symbol, argv):
        import main as main_module
        from src.backtesting.scenarios import runner as runner_module

        _days, provider = one_symbol
        store = ChainStore(str(tmp_path / "chains"))

        class _Factory:
            @staticmethod
            def from_config(config):
                return provider

        with patch.object(runner_module, "AlpacaDataProvider", _Factory), \
                patch.object(runner_module.ChainStore, "from_env",
                             classmethod(lambda cls, *a, **kw: store)), \
                patch.object(main_module, "setup_logging", lambda *a, **kw: None), \
                patch("sys.argv", argv):
            try:
                main_module.main()
                return 0
            except SystemExit as exc:
                return exc.code or 0

    def _yaml(self, tmp_path):
        path = tmp_path / "scenarios_fc096b.yaml"
        path.write_text("scenarios:\n  - name: tighter\n    overrides:\n"
                        "      strategy.min_put_premium: 0.30\n")
        return str(path)

    def test_no_writer_is_even_constructed(self, tmp_path, one_symbol,
                                           monkeypatch):
        """Layer-2's contract, pinned at the construction site rather than at
        the network: a CLI sweep without `--persist` writes NOTHING ANYWHERE.

        MUTATION CHECK: build the `ArtifactWriter` unconditionally and this
        fails, because a default-bucket writer would then try GCS on every
        local sweep an operator runs.
        """
        days, _p = one_symbol
        built = []
        real_init = ArtifactWriter.__init__

        def recording_init(self, run_id, **kw):
            built.append(run_id)
            real_init(self, run_id, **kw)

        monkeypatch.setattr(ArtifactWriter, "__init__", recording_init)
        monkeypatch.setattr(store_module, "_storage_client", _no_client)

        code = self._invoke(tmp_path, one_symbol, [
            "main.py", "--command", "sweep", "--scenarios", self._yaml(tmp_path),
            "--symbols", "AAA", "--start", days[0].isoformat(),
            "--end", days[-1].isoformat(), "--starting-cash", "50000",
            "--no-sensitivity", "--out", str(tmp_path / "s.md"),
        ])
        assert code == 0
        assert built == [], "no --persist must construct no artifact writer"

    def test_persist_writes_one_object_per_cell(self, tmp_path, one_symbol,
                                                monkeypatch):
        """The counterpart, so the test above is pinning the FLAG rather than a
        writer that never works."""
        days, _p = one_symbol
        gcs = FakeGCS()
        monkeypatch.setattr(store_module, "_storage_client", lambda: gcs)
        monkeypatch.setenv(store_module.ARTIFACT_BUCKET_ENV, "test-bucket")

        from src.backtesting.scenarios import persist as sweep_store

        class _RecordingWriter:
            """A ScenarioRunWriter that stores nothing.

            BigQuery is unavailable in this suite, and `--persist` from the CLI
            degrades to report-only rather than refusing (main.py refuses only
            in Job mode). What matters here is the ARTIFACT half of `--persist`,
            so the BigQuery half is stubbed rather than skipped — a stub that
            raised would hide the thing under test behind an unrelated failure.
            """

            enabled = True

            def __init__(self, *a, **kw):
                self.status_rows = []

            def write_status(self, row):
                self.status_rows.append(row)
                return True

            def write_runs(self, rows):
                return True

            def find_done_sweep(self, *a, **kw):
                return None

        monkeypatch.setattr(sweep_store, "ScenarioRunWriter", _RecordingWriter)

        code = self._invoke(tmp_path, one_symbol, [
            "main.py", "--command", "sweep", "--scenarios", self._yaml(tmp_path),
            "--symbols", "AAA", "--start", days[0].isoformat(),
            "--end", days[-1].isoformat(), "--starting-cash", "50000",
            "--no-sensitivity", "--persist", "--out", str(tmp_path / "s.md"),
        ])
        assert code == 0
        names = sorted(gcs.buckets["test-bucket"].objects)
        assert len(names) == 2, names
        assert all(n.startswith("sim-artifacts/v1/") for n in names)
        assert any(n.endswith("base__AAA__all.json.gz") for n in names)
        assert any(n.endswith("tighter__AAA__all.json.gz") for n in names)
