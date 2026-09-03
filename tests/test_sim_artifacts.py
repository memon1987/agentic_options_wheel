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
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

import pytest

from src.backtesting.data.chain_store import ChainStore
from src.backtesting.data.provider import StockBar
from src.backtesting.engine.broker import BacktestBroker
from src.backtesting.engine.simulator import DailyState, SimulationResult
from src.backtesting.metrics.cycles import build_cycles
from src.backtesting.metrics.fitness import BuyAndHold
from src.backtesting.reporting import artifact_store as store_module
from src.backtesting.reporting.artifact import (
    ARTIFACT_SCHEMA,
    BARS_SCHEMA,
    BARS_SOURCE,
    MID_FILL_BASIS,
    ArtifactMeta,
    bars_artifact,
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
    bars_object_name,
    parse_artifact_object_name,
    parse_bars_object_name,
    validate_scenario_name,
    validate_symbol,
)
from src.utils.config import Config

from .test_scenarios import _MultiSymbolProvider  # noqa: F401 - fixture plumbing
from .test_backtest_simulator import (
    ScriptedProvider,
    _simulator as _golden_simulator,
    _weekdays,
    dip_then_recovering_window,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sim_artifact_schema_v1.json"


# --------------------------------------------------------------------------- #
# Roll records come from a REAL replay, never from this file's imagination.
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _real_roll_records():
    """Executed-roll records from an actual replay of the golden window.

    **This exists because the first cut of this file invented them.** It hand-
    wrote `{"date", "from_symbol", "to_symbol", "strike_change", ...}` and the
    frozen fixture duly froze the invention: `from_symbol` appears nowhere in
    `src/`, and a real record carries no date at all. A schema pin taken over a
    shape the producer never emits is WORSE than no pin — it passes for ever
    while describing nothing, and Phase E would have been built against it.

    So the shape comes from the producer: `CallRoller.execute_roll`'s success
    dict, as `WheelEngine.run_rolling_cycle` collects it and the simulator
    captures it, plus the `day` the simulator stamps on (FC-096 Phase B).
    `dip_then_recovering_window` is the one window in this suite whose replay
    actually executes a roll; it is imported rather than re-derived.

    Cached for the session — one replay, not one per test.
    """
    days, closes, expirations = dip_then_recovering_window()
    result = _golden_simulator("XYZ", closes, expirations, days).run()
    assert result.roll_records, (
        "the golden window executed no roll, so this pin would freeze an empty "
        "list and assert nothing — fix the window, do not hand-write a record")
    return tuple(dict(record) for record in result.roll_records)


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
        # REAL records from a real replay (see `_real_roll_records`), never a
        # hand-written shape. The ledger and curve above are hand-built because
        # no short replay reliably produces all seven ledger kinds; the roll
        # records are not, because the producer's shape is exactly what the
        # frozen fixture has to pin.
        roll_records=[dict(record) for record in _real_roll_records()],
        earnings_symbols_without_data=["AAA"],
        earnings_symbols_past_horizon=[],
    )


def _bench(**kw) -> BuyAndHold:
    """A benchmark of the shape `_buy_and_hold` produces (FC-096 Phase E PR-1).

    Hand-built rather than replayed here because the frozen-fixture test needs a
    benchmark present on the artifact to pin its key set at all, and the golden
    replay's own benchmark is exercised separately (see the parity tests, which
    use a REAL scored report).
    """
    fields = dict(shares=990, entry_price=101.0, exit_price=105.0,
                  starting_cash=100_000.0, dividends_per_share=0.5)
    fields.update(kw)
    return BuyAndHold(**fields)


def _meta(**kw) -> ArtifactMeta:
    base = dict(
        run_id="run0123456789ab", scenario="base", symbol="AAA", split="all",
        scenario_hash="s" * 16, config_hash="c" * 16,
        engine_identity="e" * 16, arm_max_dte=7, sweep_max_dte=21,
        window_start=date(2024, 6, 3), window_end=date(2024, 6, 28),
        fill_haircut=0.25, starting_cash=100_000.0, git_commit="deadbeef",
        benchmark=_bench(), capital_base=100_000.0,
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
        "benchmark": sorted(payload["benchmark"]),
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

    def test_the_roll_record_shape_is_the_ROLLERS_shape(self):
        """The pin that the first cut of this file got wrong.

        `CallRoller.execute_roll`'s success dict is the producer, and the
        artifact must carry its keys — not a plausible-looking invention. This
        asserts against the roller's own source rather than against a literal,
        so a field added there fails HERE rather than silently never reaching
        Phase E.
        """
        records = _real_roll_records()
        for record in records:
            assert record["success"] is True
            # Every key `execute_roll` returns on the success path.
            assert {"success", "underlying", "old_strike", "new_strike",
                    "contracts", "net_credit", "btc_order_id",
                    "stc_order_id"} <= set(record), record
            # ...and NOT the invention the first cut froze.
            assert "from_symbol" not in record
            assert "to_symbol" not in record
            assert "strike_change" not in record
            assert "date" not in record

    def test_the_replay_stamps_the_decision_day_onto_each_roll(self):
        """Production's record has no date — the log line's timestamp is the
        date. An artifact read months later has no such context, and "which day
        did this roll happen" is the first question a chart marker asks. The
        capture point is the only place that knows the day.

        MUTATION CHECK: drop the `{'day': ...}` merge in `simulator.py` and this
        fails, as does the frozen fixture.
        """
        from datetime import date as _date

        for record in _real_roll_records():
            assert "day" in record, record
            # A real ISO date, not a repr of something else.
            assert _date.fromisoformat(record["day"])

    def test_the_stamp_cannot_clobber_a_future_roller_field(self):
        """`{'day': ..., **record}` — ours first, the roller's second — so a
        roller that one day emits its own `day` wins rather than being silently
        overwritten by the replay's."""
        import inspect

        from src.backtesting.engine import simulator as sim

        source = inspect.getsource(sim.Simulator.replay)
        assert "{'day': day.isoformat(), **r}" in source, (
            "the merge order is load-bearing: `**r` must come SECOND")

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

    @pytest.mark.parametrize("name", ["tighter\n", "\ntighter", "a\nb"])
    def test_a_newline_in_a_name_is_refused(self, name):
        """`SCENARIO_NAME_RE` is anchored with `\\Z`, not `$`. Python's `$` ALSO
        matches immediately before a trailing newline, so `"tighter\\n"` used to
        satisfy this pattern end to end — accepted by the CLI, carried into an
        env var and a grid header, and (since this PR) into a GCS object name,
        where a newline is a header-splitting character the client rejects at
        serve time.

        MUTATION CHECK: put `$` back and the trailing-newline case passes
        validation.
        """
        with pytest.raises(ValueError):
            validate_scenario_name(name, "test")

    def test_a_single_underscore_is_still_fine(self):
        validate_scenario_name("long_dte", "test")
        validate_scenario_name("a_b_c", "test")

    def test_a_symbol_that_could_not_address_an_object_is_refused(self):
        """The CLI's `--symbols` is hand-typed and reaches the artifact writer
        directly. A symbol carrying `__`, a slash or whitespace would land an
        object whose name nothing can parse or request back — so it is refused
        by the SAME rule the endpoint applies at serve time."""
        from src.backtesting.scenarios.identity import validate_symbol

        for bad in ("A__B", "A/B", "A B", "aapl\n", "", "1AAPL",
                    "TOOLONGSYMBOL1234"):
            with pytest.raises(ValueError, match="symbol"):
                validate_symbol(bad, "--symbols")
        for good in ("AAPL", "BRK.B", "RDS-A", "F"):
            validate_symbol(good, "--symbols")

    def test_the_cli_refuses_an_unaddressable_symbol_before_replaying(
            self, tmp_path, one_symbol):
        """Before anything is materialised — an immediate refusal beats a sweep
        that completes and produces unreadable evidence."""
        import main as main_module

        days, _p = one_symbol
        yaml_path = tmp_path / "s.yaml"
        yaml_path.write_text("scenarios:\n  - name: a\n    overrides: {}\n")
        with patch("sys.argv", [
            "main.py", "--command", "sweep", "--scenarios", str(yaml_path),
            "--symbols", "AAA__B", "--start", days[0].isoformat(),
            "--end", days[-1].isoformat(),
        ]), patch.object(main_module, "setup_logging", lambda *a, **kw: None):
            with pytest.raises(SystemExit) as exc:
                main_module.main()
        # `SystemExit(str)` — the repo's refusal idiom: Python prints the string
        # and exits 1. The message is the assertion, not the numeric code.
        assert "AAA__B" in str(exc.value)
        assert "object nothing can address" in str(exc.value)

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

    def test_an_UNIMPORTABLE_serialiser_is_swallowed_too(
            self, tmp_path, one_symbol, one_symbol_config):
        """The import lives INSIDE the guard, not above it.

        A syntax error in `artifact.py`, or a dependency missing from some
        future build, raises `ImportError` — and outside the guard that would
        propagate into `_replay_one`'s handler and turn every successfully
        replayed cell into an error row. Losing a run's results to a defect in
        its evidence writer is precisely backwards.

        MUTATION CHECK: hoist the import above the `try` in `_emit_artifact` and
        this run comes back with two error rows.
        """
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if "reporting.artifact" in name or name.endswith("artifact"):
                raise ImportError("no module named artifact (simulated)")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", refuse):
            result = _sweep(tmp_path, one_symbol, one_symbol_config,
                            [Scenario("a", {})], artifact_sink=lambda _a: None)
        assert not result.errors, [r.error for r in result.errors]
        assert all(row.total_return is not None for row in result.rows)

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

    def test_a_run_with_no_measured_cell_at_all_is_none_not_true(self):
        """The vacuous case. `0 == 0` is True, and it would be a LIE: a sweep
        whose every arm errored has not one artifact object, and a `done` row
        claiming a complete set is exactly the kind of absence-as-result this
        column exists to prevent.

        MUTATION CHECK: delete the `expected == 0` guard and this returns True.
        """
        assert self._call(_Writer(0), _Result([_Cell("boom"), _Cell("boom")])) is None
        assert self._call(_Writer(0), _Result([])) is None

    def test_a_crashed_run_that_wrote_objects_is_false_not_none(self):
        """The subtle one. A replay that raises leaves no `result`, so there is
        no denominator — but the writer may already have stored objects for the
        cells that finished. `None` there would say "this run wrote nothing"
        while orphaned objects sit in the bucket.

        MUTATION CHECK: return `None` unconditionally when `result is None` and
        this fails.
        """
        assert self._call(_Writer(3), None) is False
        # ...and a crash before ANY artifact landed is still the vacuous None.
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
        # Two cells + ONE bars sidecar for the single (symbol, split) window.
        # FC-096 Phase E PR-1: the sidecar rides the same `--persist` gate, so
        # this is still "the flag, not a writer that never works" — it now pins
        # BOTH object families onto that one flag.
        assert len(names) == 3, names
        assert all(n.startswith("sim-artifacts/v1/") for n in names)
        assert any(n.endswith("base__AAA__all.json.gz") for n in names)
        assert any(n.endswith("tighter__AAA__all.json.gz") for n in names)
        assert sum(1 for n in names if "/bars/" in n) == 1, names
        assert any(n.endswith("bars/AAA__all.json.gz") for n in names)


# =========================================================================== #
# 10. The bars sidecar (FC-096 Phase E PR-1)
#
# The sidecar exists because there is NO durable bar series the dashboard could
# read instead: `BarStore` is a per-container local parquet cache by explicit
# design, no Job or service mounts a GCS volume, neither bucket has a bars
# prefix, and BigQuery's stock history covers the live universe only. So every
# way this object could quietly lie is a way the console's price chart, its
# buy-and-hold curve and its deployment tile quietly lie:
#
# * a curve re-derived from the SPEC's cash instead of the scored benchmark —
#   correct for a wheel replay, and wrong by the whole position size for a
#   covered-call one;
# * a dividend interval off by one day, which hands the benchmark a free quarter
#   on exactly the high-yield names it exists to judge;
# * a clip taken on the REQUESTED window rather than the decision-day bounds,
#   which shifts the curve's first point off the benchmark's entry;
# * a sidecar emitted per ARM rather than per window, inviting a reader to take
#   arm C's chart for its own.
# =========================================================================== #
BARS_FIXTURE = Path(__file__).parent / "fixtures" / "sim_bars_schema_v1.json"


def _bar(day: date, close: float, symbol: str = "AAA") -> StockBar:
    return StockBar(symbol=symbol, bar_date=day, open=close, high=close + 1.0,
                    low=close - 1.0, close=close, volume=5_000_000)


def _states(days) -> list:
    return [DailyState(day=d, equity=1.0, cash=1.0, reserved_collateral=0.0,
                       open_options=0, shares_held={}) for d in days]


@lru_cache(maxsize=1)
def _golden_scored():
    """A REAL replay of the golden window, scored — result, bars, report, days.

    Not hand-built: the parity property this pins is between the sidecar's last
    curve point and a ``BuyAndHold`` the ENGINE produced from its own bars and
    its own dividend schedule. A hand-made benchmark would satisfy the
    arithmetic while proving nothing about the two producers agreeing.
    """
    from src.backtesting import evaluate as evaluate_module

    days, closes, expirations = dip_then_recovering_window()
    result = _golden_simulator("XYZ", closes, expirations, days).run()
    provider = ScriptedProvider("XYZ", closes, expirations)
    bars = provider.get_stock_bars("XYZ", min(closes), max(closes))
    report = evaluate_module._score("XYZ", result, bars, 50_000.0)
    assert report.benchmark is not None, (
        "the golden window produced no benchmark, so the parity test would "
        "assert nothing — fix the window, do not hand-build one")
    return result, bars, report, days


class TestTheBarsSidecarIsTheEnginesOwnBenchmark:
    def test_the_last_curve_point_equals_the_scored_final_value(self):
        """Parity (i), on a REAL scored replay.

        ``daily[-1].value == BuyAndHold.final_value`` holds BY CONSTRUCTION, not
        by coincidence: the clip is the decision-day bounds, so the last bar's
        close IS ``exit_price``, and the dividend term at the exit day is exactly
        the total ``_score`` scoped the benchmark to.

        MUTATION CHECK: clip to the REQUESTED window instead of the decision-day
        bounds and the last point moves onto a warm-up-tail bar; re-derive
        ``shares`` as ``starting_cash // close`` and it moves by whole shares.
        """
        result, bars, report, days = _golden_scored()
        payload = bars_artifact(
            bars, "XYZ", ("all", days[0], days[-1]),
            daily=result.daily, benchmark=report.benchmark, dividends=None)
        curve = payload["buy_and_hold"]["daily"]
        assert curve, "the curve is empty"
        assert curve[-1]["value"] == pytest.approx(report.benchmark.final_value)
        assert curve[-1]["date"] == result.daily[-1].day.isoformat()

    def test_the_clip_IS_the_window_the_report_was_scored_over(self):
        """The unstated simulator invariant the whole construction rests on.

        `bars_artifact` clips to `daily[0].day`/`daily[-1].day`;
        `compute_fitness` takes `report.start`/`report.end` from the same two
        values of the `daily` it is handed. Nothing in `SimulationResult`'s
        contract promises those are the same list — so if a future change
        filtered `daily` on the way into scoring, or handed the artifact a
        padded curve, the sidecar would draw one window while the row's `days`
        denominator measured another, and every parity test above would still
        pass because they all read the same `daily` twice.

        Pinned on the REAL scored replay rather than a fixture, because that is
        where the two producers actually meet.

        **What this does and does not catch, honestly.** On the golden window
        the REQUESTED bounds and the decision-day bounds happen to coincide, so
        a mutation that clipped to the requested window would slip past this
        assertion here — `TestTheClipIsTheDecisionDayBounds` is the test that
        catches that one, on a fixture built with a warm-up tail. What this test
        uniquely covers is the CROSS-PRODUCER equality: the sidecar's stamp
        against a `FitnessReport` the engine scored, which no other test
        compares. The change it exists to fail on is a future divergence between
        "the `daily` the artifact clips to" and "the `daily` the report was
        scored over", which is not expressible as a one-line edit to either
        producer today — that is exactly why it is worth pinning before it is.
        """
        result, bars, report, days = _golden_scored()
        payload = bars_artifact(
            bars, "XYZ", ("all", days[0], days[-1]),
            daily=result.daily, benchmark=report.benchmark, dividends=None)
        prov = payload["provenance"]
        assert prov["first_decision_day"] == report.start.isoformat()
        assert prov["last_decision_day"] == report.end.isoformat()
        # ...and the benchmark the curve is built from entered and exited on
        # exactly that pair, which is what makes the last point reconcile.
        bh = payload["buy_and_hold"]
        assert bh["entry_day"] == report.start.isoformat()
        assert bh["exit_day"] == report.end.isoformat()
        # The equality is a real NARROWING, not "the clip took everything": the
        # materialised span starts months before the first decision day. Without
        # this the assertions above would still pass on a build that had stopped
        # clipping at all.
        assert prov["data_from"] < prov["first_decision_day"]
        assert prov["bars_in_window"] < len(bars)

    def test_the_final_value_is_the_rows_benchmark_return_over_capital(self):
        """The other half of parity (i): the curve reconciles to the SCALAR the
        ``scenario_runs`` row carries, so a console showing both cannot disagree
        with itself."""
        result, bars, report, days = _golden_scored()
        payload = bars_artifact(
            bars, "XYZ", ("all", days[0], days[-1]),
            daily=result.daily, benchmark=report.benchmark, dividends=None)
        bh = payload["buy_and_hold"]
        assert bh["final_value"] == pytest.approx(
            bh["capital_base"] * (1 + report.benchmark.total_return))
        assert bh["daily"][-1]["value"] == pytest.approx(bh["final_value"])

    def test_the_cell_artifacts_benchmark_stamp_equals_the_sidecars(self):
        """The console cross-checks these two numbers and hides the curve on a
        mismatch. They must be the same number for a same-spec run, and they are
        because both are COPIED off one ``BuyAndHold``."""
        result, bars, report, days = _golden_scored()
        sidecar = bars_artifact(
            bars, "XYZ", ("all", days[0], days[-1]),
            daily=result.daily, benchmark=report.benchmark, dividends=None)
        cell = cell_artifact(result, _meta(
            symbol="XYZ", benchmark=report.benchmark, capital_base=50_000.0))
        assert cell["benchmark"]["final_value"] == pytest.approx(
            sidecar["buy_and_hold"]["final_value"])
        assert cell["benchmark"]["shares"] == sidecar["buy_and_hold"]["shares"]
        assert cell["benchmark"]["entry_day"] == sidecar["buy_and_hold"]["entry_day"]
        assert cell["benchmark"]["exit_day"] == sidecar["buy_and_hold"]["exit_day"]
        assert cell["provenance"]["capital_base"] == 50_000.0

    def test_the_CC_HOOK_a_lot_based_benchmark_curves_on_the_lot_value(self):
        """Parity (ii) — the hook Phase C extends (§D-2).

        A covered-call benchmark is 100 shares against a LOT-sized
        ``starting_cash``, not the spec's $100k. The curve builder takes the
        ``BuyAndHold`` INSTANCE and copies ``shares``/``starting_cash`` off it,
        so the lot case works without a line of covered-call code here.

        MUTATION CHECK: make ``_buy_and_hold_block`` derive
        ``shares = int(capital_base // entry_price)`` from a spec-sized cash and
        this test's ``capital_base`` assertion fails by an order of magnitude.
        """
        days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
        bars = [_bar(days[0], 100.0), _bar(days[1], 102.0), _bar(days[2], 104.0)]
        lot_value = 100 * 100.0
        bench = BuyAndHold(shares=100, entry_price=100.0, exit_price=104.0,
                           starting_cash=lot_value, dividends_per_share=0.0)
        payload = bars_artifact(
            bars, "AAA", ("all", days[0], days[-1]),
            daily=_states(days), benchmark=bench, dividends=None)
        bh = payload["buy_and_hold"]
        assert bh["capital_base"] == lot_value == 10_000.0
        assert bh["shares"] == 100
        assert bh["daily"][-1]["value"] == pytest.approx(bench.final_value)
        assert bh["daily"][-1]["value"] == pytest.approx(10_400.0)

    def test_shares_are_COPIED_never_re_derived_from_the_capital_base(self):
        """The contract §D-2 states: the builder takes the ``BuyAndHold``
        INSTANCE and copies its position, rather than computing one.

        Today ``_buy_and_hold`` happens to build ``shares = cash // entry``, so
        a re-derivation would coincide with the copy on every wheel replay AND
        on a lot whose value is `shares × price` — which is why the CC-hook test
        above cannot catch it. This one uses a benchmark where the two
        deliberately diverge (100 shares against a $50k base), so the invariant
        is pinned as a CONTRACT instead of being silently load-bearing the day
        Phase C sizes a lot some other way.

        MUTATION CHECK: `shares = int(capital_base // entry_price)` reports
        $55,000 against the benchmark's own $51,000 — a position five times the
        real one.
        """
        days = [date(2024, 6, 3), date(2024, 6, 4)]
        bars = [_bar(days[0], 100.0), _bar(days[1], 110.0)]
        bench = BuyAndHold(shares=100, entry_price=100.0, exit_price=110.0,
                           starting_cash=50_000.0, dividends_per_share=0.0)
        assert int(bench.starting_cash // bench.entry_price) != bench.shares, (
            "this fixture only tests anything while the two disagree")
        payload = bars_artifact(
            bars, "AAA", ("all", days[0], days[-1]),
            daily=_states(days), benchmark=bench, dividends=None)
        bh = payload["buy_and_hold"]
        assert bh["shares"] == 100
        assert bh["daily"][-1]["value"] == pytest.approx(bench.final_value)
        assert bh["daily"][-1]["value"] == pytest.approx(51_000.0)

    def test_null_benchmark_means_null_buy_and_hold(self):
        """``buy_and_hold`` is null EXACTLY when the report had no benchmark. A
        block of nulls would read as "the benchmark was flat"."""
        days = [date(2024, 6, 3), date(2024, 6, 4)]
        payload = bars_artifact(
            [_bar(days[0], 100.0), _bar(days[1], 101.0)], "AAA",
            ("all", days[0], days[-1]),
            daily=_states(days), benchmark=None, dividends=None)
        assert payload["buy_and_hold"] is None
        assert payload["bars"], "the bars themselves still serve"


class TestTheClipIsTheDecisionDayBounds:
    def test_warm_up_bars_are_excluded_and_the_count_is_stamped(self):
        """MUTATION CHECK: clip to the REQUESTED window and `bars_in_window`
        jumps from 3 to 6, taking the curve's first point with it."""
        window_days = [date(2024, 6, 5), date(2024, 6, 6), date(2024, 6, 7)]
        warmup = [_bar(date(2024, 5, d), 90.0) for d in (1, 2, 3)]
        bars = warmup + [_bar(d, 100.0 + i) for i, d in enumerate(window_days)]
        payload = bars_artifact(
            bars, "AAA", ("all", date(2024, 5, 1), date(2024, 6, 10)),
            daily=_states(window_days), benchmark=None, dividends=None)
        assert [b["date"] for b in payload["bars"]] == [
            d.isoformat() for d in window_days]
        assert payload["provenance"]["bars_in_window"] == 3
        # The FULL materialised span is still stamped, as the freshness fact.
        assert payload["provenance"]["data_from"] == "2024-05-01"
        assert payload["provenance"]["data_to"] == "2024-06-07"
        assert payload["provenance"]["window"] == {
            "start": "2024-05-01", "end": "2024-06-10"}
        assert payload["provenance"]["first_decision_day"] == "2024-06-05"
        assert payload["provenance"]["last_decision_day"] == "2024-06-07"

    def test_the_rows_are_ohlcv_in_date_order(self):
        days = [date(2024, 6, 5), date(2024, 6, 6)]
        payload = bars_artifact(
            [_bar(days[1], 101.0), _bar(days[0], 100.0)], "AAA",
            ("all", days[0], days[-1]), daily=_states(days),
            benchmark=None, dividends=None)
        assert [b["date"] for b in payload["bars"]] == [
            "2024-06-05", "2024-06-06"]
        assert payload["bars"][0] == {
            "date": "2024-06-05", "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0, "volume": 5_000_000}

    def test_an_empty_daily_refuses_rather_than_writing_nulls(self):
        with pytest.raises(ValueError, match="no decision days"):
            bars_artifact([_bar(date(2024, 6, 3), 100.0)], "AAA",
                          ("all", date(2024, 6, 3), date(2024, 6, 3)),
                          daily=[], benchmark=None, dividends=None)


class TestTheDividendIntervalIsHalfOpenAtEntry:
    """``div_ps(entry_day, t]`` — ``DividendSchedule.total_between``'s interval.

    A buyer at the close of the entry day does NOT receive a dividend going ex
    that day; a holder selling at the close on an ex-date DOES. Getting this
    wrong by one day hands the benchmark a free quarter on exactly the
    high-yield names it exists to judge.
    """

    DAYS = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]

    def _payload(self, ex_dates):
        from src.backtesting.data.dividends import Dividend, DividendSchedule

        schedule = DividendSchedule(
            {"AAA": [Dividend(ex_date=d, amount=1.0) for d in ex_dates]})
        bars = [_bar(d, 100.0) for d in self.DAYS]
        bench = BuyAndHold(
            shares=10, entry_price=100.0, exit_price=100.0,
            starting_cash=1_000.0,
            dividends_per_share=schedule.total_between(
                "AAA", self.DAYS[0], self.DAYS[-1]))
        return bars_artifact(bars, "AAA", ("all", self.DAYS[0], self.DAYS[-1]),
                             daily=_states(self.DAYS), benchmark=bench,
                             dividends=schedule), bench

    def test_a_dividend_going_ex_on_the_ENTRY_day_is_the_sellers(self):
        """MUTATION CHECK: make the lower bound inclusive (``after <= ex_date``)
        and every point on this curve gains $10."""
        payload, bench = self._payload([self.DAYS[0]])
        values = [p["value"] for p in payload["buy_and_hold"]["daily"]]
        assert values == [1_000.0, 1_000.0, 1_000.0]
        assert bench.dividends_per_share == 0.0

    def test_a_dividend_going_ex_on_the_EXIT_day_is_collected(self):
        """MUTATION CHECK: make the upper bound exclusive and the last point
        loses $10 while ``BuyAndHold.final_value`` keeps it — parity breaks."""
        payload, bench = self._payload([self.DAYS[-1]])
        values = [p["value"] for p in payload["buy_and_hold"]["daily"]]
        assert values == [1_000.0, 1_000.0, 1_010.0]
        assert values[-1] == pytest.approx(bench.final_value)

    def test_a_mid_window_dividend_steps_the_curve_once(self):
        payload, bench = self._payload([self.DAYS[1]])
        values = [p["value"] for p in payload["buy_and_hold"]["daily"]]
        assert values == [1_000.0, 1_010.0, 1_010.0]
        assert values[-1] == pytest.approx(bench.final_value)


class TestTheBarsSchemaIsFrozen:
    def test_the_full_key_set_matches_the_fixture(self):
        """Same contract as the cell artifact's: ADDING a field updates the
        fixture; removing or renaming one bumps ``BARS_SCHEMA`` and the object
        prefix, because a stored object outlives the code that wrote it."""
        days = [date(2024, 6, 3), date(2024, 6, 4)]
        payload = bars_artifact(
            [_bar(days[0], 100.0), _bar(days[1], 102.0)], "AAA",
            ("all", days[0], days[-1]), daily=_states(days),
            benchmark=BuyAndHold(shares=990, entry_price=100.0,
                                 exit_price=102.0, starting_cash=100_000.0),
            dividends=None, run_id="r", engine_identity="e", git_commit="g")
        got = {
            "schema": payload["schema"],
            "top_level": sorted(payload),
            "provenance": sorted(payload["provenance"]),
            "provenance.window": sorted(payload["provenance"]["window"]),
            "bars": sorted(payload["bars"][0]),
            "buy_and_hold": sorted(payload["buy_and_hold"]),
            "buy_and_hold.daily": sorted(payload["buy_and_hold"]["daily"][0]),
        }
        expected = json.loads(BARS_FIXTURE.read_text())
        assert got["schema"] == expected["schema"] == BARS_SCHEMA
        for section in sorted(expected):
            assert got[section] == expected[section], section
        assert sorted(got) == sorted(expected), "a whole SECTION appeared or vanished"

    def test_the_source_stamp_says_these_are_the_replays_own_bars(self):
        days = [date(2024, 6, 3)]
        payload = bars_artifact([_bar(days[0], 100.0)], "AAA",
                                ("fit", days[0], days[0]),
                                daily=_states(days), benchmark=None,
                                dividends=None)
        assert payload["provenance"]["source"] == BARS_SOURCE == "materialised bars"
        assert payload["provenance"]["split"] == "fit"

    def test_every_number_is_finite_or_null(self):
        days = [date(2024, 6, 3), date(2024, 6, 4)]
        raw = json.dumps(bars_artifact(
            [_bar(days[0], 100.0), _bar(days[1], 102.0)], "AAA",
            ("all", days[0], days[-1]), daily=_states(days),
            benchmark=BuyAndHold(shares=990, entry_price=100.0,
                                 exit_price=102.0, starting_cash=100_000.0),
            dividends=None))
        assert "NaN" not in raw and "Infinity" not in raw

    def test_the_bytes_round_trip_through_the_writers_serialiser(self):
        days = [date(2024, 6, 3)]
        payload = bars_artifact([_bar(days[0], 100.0)], "AAA",
                                ("all", days[0], days[0]),
                                daily=_states(days), benchmark=None,
                                dividends=None)
        raw = artifact_bytes(payload)
        assert raw[:2] == b"\x1f\x8b"
        assert json.loads(gzip.decompress(raw)) == json.loads(
            json.dumps(payload, sort_keys=True, default=str))


class TestTheBarsObjectName:
    def test_the_name_is_the_documented_one(self):
        assert bars_object_name("r1", "GOOGL", "fit") == (
            "sim-artifacts/v1/r1/bars/GOOGL__fit.json.gz")

    def test_it_round_trips(self):
        assert parse_bars_object_name(
            bars_object_name("r1", "BRK.B", "holdout")) == {
                "run_id": "r1", "symbol": "BRK.B", "split": "holdout"}

    def test_the_two_families_do_not_collide(self):
        """A scenario literally named ``bars`` writes an OBJECT called
        ``bars__SYM__split.json.gz``; the sidecar writes into a DIRECTORY called
        ``bars/``. Neither parser accepts the other's name.

        MUTATION CHECK: drop the ``bars`` guard from
        ``parse_artifact_object_name`` and it answers ``run_id='bars'`` for a
        sidecar instead of raising.
        """
        cell = artifact_object_name("r1", "bars", "AAA", "all")
        sidecar = bars_object_name("r1", "AAA", "all")
        assert cell != sidecar
        assert parse_artifact_object_name(cell) == {
            "run_id": "r1", "scenario": "bars", "symbol": "AAA", "split": "all"}
        with pytest.raises(ValueError, match="bars sidecar"):
            parse_artifact_object_name(sidecar)
        with pytest.raises(ValueError, match="directory segment"):
            parse_bars_object_name(cell)

    @pytest.mark.parametrize("bad", [
        "sim-artifacts/v1/r1/bars/AAA__all.json",
        "sim-artifacts/v1/r1/AAA__all.json.gz",
        "bars/AAA__all.json.gz",
        "sim-artifacts/v1/r1/bars/AAAall.json.gz",
        "sim-artifacts/v1/r1/bars/__all.json.gz",
        "",
    ])
    def test_a_name_that_is_not_one_of_ours_raises(self, bad):
        with pytest.raises(ValueError):
            parse_bars_object_name(bad)

    def test_a_trailing_newline_cannot_reach_a_stored_name(self):
        r"""``\Z``, not ``$`` — the lesson ``SCENARIO_NAME_RE`` records. A
        newline in an object name is a header-splitting character the GCS client
        rejects at serve time, i.e. long after the write."""
        with pytest.raises(ValueError):
            validate_symbol("AAA\n", "bars path")


class TestTheBarsWriter:
    def test_it_writes_the_gzip_at_the_documented_address(self):
        gcs = FakeGCS()
        writer = ArtifactWriter("r1", bucket="test-bucket", client=gcs)
        assert writer.write_bars(
            {"schema": 1, "provenance": {"run_id": "r1", "symbol": "AAA",
                                         "split": "fit"}}) is True
        bucket = gcs.buckets["test-bucket"]
        (name, data), = bucket.objects.items()
        assert name == "sim-artifacts/v1/r1/bars/AAA__fit.json.gz"
        # Opaque gzip and NO `content_encoding`: setting the latter turns on
        # GCS decompressive transcoding, so a download would silently return
        # decompressed bytes and every reader's gzip handling would be a lie
        # that happens to work.
        assert bucket.uploads[0]["content_type"] == "application/gzip"
        assert "content_encoding" not in bucket.uploads[0]
        assert json.loads(gzip.decompress(data))["schema"] == 1
        assert writer.bars_written == 1 and writer.bars_failed == 0

    def test_the_bars_counters_are_kept_apart_from_the_cell_counters(self):
        """``artifacts_complete`` is "one artifact per non-errored CELL".
        Folding the sidecar into ``written`` would make that arithmetic wrong on
        any sweep with more than one arm — which is every sweep.

        MUTATION CHECK: increment ``self.written`` in ``write_bars`` and this
        fails.
        """
        gcs = FakeGCS()
        writer = ArtifactWriter("r1", bucket="test-bucket", client=gcs)
        writer.write({"provenance": {"run_id": "r1", "scenario": "base",
                                     "symbol": "AAA", "split": "fit"}})
        writer.write_bars({"provenance": {"run_id": "r1", "symbol": "AAA",
                                          "split": "fit"}})
        assert (writer.written, writer.failed) == (1, 0)
        assert (writer.bars_written, writer.bars_failed) == (1, 0)

    def test_a_gcs_failure_is_counted_and_swallowed(self):
        gcs = FakeGCS(raises=RuntimeError("503 backend error"))
        writer = ArtifactWriter("r1", bucket="test-bucket", client=gcs)
        assert writer.write_bars(
            {"provenance": {"run_id": "r1", "symbol": "AAA",
                            "split": "fit"}}) is False
        assert writer.bars_failed == 1 and writer.bars_written == 0
        assert "503" in writer.last_bars_error

    def test_a_bars_failure_does_not_overwrite_the_CELL_error(self):
        """`main.py`'s `sim_artifacts_incomplete` warning reports `last_error`
        beside the CELL counts, and it is the only place an operator sees WHY a
        cell artifact is missing. A sidecar failing afterwards — the ordinary
        case, since the sidecar is written after the base cell — must not
        replace that message with an unrelated one, especially as a sidecar
        failure never makes a run's artifacts incomplete.

        MUTATION CHECK: alias `last_bars_error` back to `last_error` in
        `write_bars` and the cell's message is gone by the time the warning
        fires.
        """
        gcs = FakeGCS(raises=RuntimeError("CELL upload exploded"))
        writer = ArtifactWriter("r1", bucket="test-bucket", client=gcs)
        writer.write({"provenance": {"run_id": "r1", "scenario": "base",
                                     "symbol": "AAA", "split": "fit"}})
        assert "CELL upload exploded" in writer.last_error
        assert writer.last_bars_error is None

        # The FakeBucket is cached on first use, so the second failure has to
        # be set on the bucket rather than on the client.
        gcs.buckets["test-bucket"].raises = RuntimeError("BARS upload exploded")
        writer.write_bars({"provenance": {"run_id": "r1", "symbol": "AAA",
                                          "split": "fit"}})
        assert "CELL upload exploded" in writer.last_error, (
            "the sidecar failure overwrote the cell failure the incomplete-"
            "artifacts warning quotes")
        assert "BARS upload exploded" in writer.last_bars_error
        assert (writer.failed, writer.bars_failed) == (1, 1)

    def test_an_empty_bucket_env_disables_it(self, monkeypatch):
        monkeypatch.setenv(store_module.ARTIFACT_BUCKET_ENV, "")
        monkeypatch.setattr(store_module, "_storage_client", _no_client)
        writer = ArtifactWriter("r1")
        assert writer.enabled is False
        assert writer.write_bars({"provenance": {"symbol": "AAA"}}) is False
        assert writer.bars_written == 0 and writer.bars_failed == 0


class TestTheBarsSink:
    def test_one_sidecar_per_window_from_the_BASE_arm_only(
            self, tmp_path, one_symbol, one_symbol_config):
        """The headline sink test.

        MUTATION CHECK: drop the ``scenario.name == BASE_SCENARIO_NAME`` guard
        and this sweep emits THREE identical sidecars for one window — which is
        what would let a reader open arm C's chart believing it is arm C's.
        """
        seen = []
        result = _sweep(tmp_path, one_symbol, one_symbol_config,
                        [Scenario("tighter", {"strategy.min_put_premium": 0.30}),
                         Scenario("wider", {"strategy.min_put_premium": 0.10})],
                        artifact_sink=lambda _a: None, bars_sink=seen.append,
                        run_id="R1", engine_identity="EID", git_commit="C0FFEE")
        assert len(result.rows) == 3 and not result.errors
        assert len(seen) == 1, [s["provenance"] for s in seen]
        prov = seen[0]["provenance"]
        assert prov["run_id"] == "R1"
        assert prov["engine_identity"] == "EID"
        assert prov["git_commit"] == "C0FFEE"
        assert prov["symbol"] == "AAA" and prov["split"] == "all"
        assert seen[0]["schema"] == BARS_SCHEMA

    def test_a_base_replay_that_RAISES_leaves_the_window_with_no_sidecar(
            self, tmp_path, one_symbol, one_symbol_config):
        """§Behaviour contract: the other arms' ROWS still persist; only the
        evidence is missing, and the console degrades through the absent-sidecar
        path rather than drawing a curve from another arm.

        MUTATION CHECK: emit the sidecar before the row is built, or from
        whichever arm happens to be first, and this fails.
        """
        from src.backtesting.engine.simulator import Simulator

        real_replay = Simulator.replay

        def boom(self, materialised, **kw):
            if self.config.min_put_premium == 0.50:   # the base profile's floor
                raise RuntimeError("base blew up")
            return real_replay(self, materialised, **kw)

        seen = []
        with patch.object(Simulator, "replay", boom):
            result = _sweep(tmp_path, one_symbol, one_symbol_config,
                            [Scenario("tighter",
                                      {"strategy.min_put_premium": 0.30})],
                            bars_sink=seen.append)
        assert seen == [], "a failed base arm must emit no sidecar"
        assert len(result.errors) == 1
        non_base = [r for r in result.rows if r.scenario != "base"]
        assert len(non_base) == 1 and non_base[0].error is None
        assert non_base[0].total_return is not None

    def test_a_failed_MATERIALISATION_emits_no_sidecar(
            self, tmp_path, one_symbol, one_symbol_config):
        from src.backtesting.scenarios import runner as runner_module

        seen = []
        with patch.object(runner_module, "_materialise_window",
                          side_effect=RuntimeError("no data")):
            result = _sweep(tmp_path, one_symbol, one_symbol_config,
                            [Scenario("a", {})], bars_sink=seen.append)
        assert seen == []
        assert len(result.errors) == len(result.rows) == 2

    def test_a_raising_sink_does_not_error_the_row_or_the_cell_artifact(
            self, tmp_path, one_symbol, one_symbol_config):
        """MUTATION CHECK: drop ``_emit_bars``'s own ``try`` and a GCS outage
        turns every completed base cell into an error row. Its guard is SEPARATE
        from ``_emit_artifact``'s so a sidecar failure cannot cost the cell
        artifact that was already written."""
        cells = []

        def explode(_payload):
            raise RuntimeError("bars sink is down")

        result = _sweep(tmp_path, one_symbol, one_symbol_config,
                        [Scenario("a", {})], artifact_sink=cells.append,
                        bars_sink=explode)
        assert not result.errors
        assert all(row.total_return is not None for row in result.rows)
        assert len(cells) == 2, "the cell artifacts still landed"

    def test_a_serialiser_failure_is_swallowed_too(
            self, tmp_path, one_symbol, one_symbol_config):
        with patch("src.backtesting.reporting.artifact.bars_artifact",
                   side_effect=ValueError("serialiser bug")):
            result = _sweep(tmp_path, one_symbol, one_symbol_config,
                            [Scenario("a", {})], bars_sink=lambda _a: None)
        assert not result.errors

    def test_an_UNIMPORTABLE_serialiser_is_swallowed_too(
            self, tmp_path, one_symbol, one_symbol_config):
        """The import lives INSIDE ``_emit_bars``'s guard, not above it."""
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if "reporting.artifact" in name or name.endswith("artifact"):
                raise ImportError("no module named artifact (simulated)")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", refuse):
            result = _sweep(tmp_path, one_symbol, one_symbol_config,
                            [Scenario("a", {})], bars_sink=lambda _a: None)
        assert not result.errors, [r.error for r in result.errors]

    def test_no_bars_sink_means_no_bars_work_at_all(
            self, tmp_path, one_symbol, one_symbol_config):
        with patch("src.backtesting.reporting.artifact.bars_artifact") as built:
            _sweep(tmp_path, one_symbol, one_symbol_config, [Scenario("a", {})],
                   artifact_sink=lambda _a: None)
        built.assert_not_called()

    def test_one_sidecar_per_SPLIT_when_the_run_has_a_holdout(
            self, tmp_path, one_symbol, one_symbol_config):
        """Two windows, two sidecars — the sidecar is per (symbol, split), which
        is exactly what the route's three path segments address, and the two
        windows must not share a bar."""
        days, provider = one_symbol
        seen = []
        run_sweep(
            one_symbol_config, [Scenario("a", {})], ["AAA"], days[0], days[-1],
            holdout_start=days[20], starting_cash=50_000.0,
            chain_store=ChainStore(str(tmp_path)), bar_provider=provider,
            quiet_logs=False, bars_sink=seen.append, run_id="R2",
        )
        by_split = {s["provenance"]["split"]: s for s in seen}
        assert sorted(by_split) == ["fit", "holdout"]
        fit_dates = {b["date"] for b in by_split["fit"]["bars"]}
        holdout_dates = {b["date"] for b in by_split["holdout"]["bars"]}
        assert fit_dates and holdout_dates
        assert not (fit_dates & holdout_dates), "the two windows must not overlap"


class TestTheGitCommitStampIsPopulated:
    def test_the_runner_passes_it_through_to_every_cell_artifact(
            self, tmp_path, one_symbol, one_symbol_config):
        """§Found while planning 1: ``provenance.git_commit`` was ``null`` on
        EVERY stored artifact, because ``run_sweep`` had no parameter for it
        while both callers held the value.

        MUTATION CHECK: drop ``git_commit=git_commit`` from the
        ``_emit_artifact`` call and every stamp goes back to ``None``.
        """
        seen = []
        _sweep(tmp_path, one_symbol, one_symbol_config,
               [Scenario("a", {})], artifact_sink=seen.append,
               git_commit="abc1234")
        assert seen
        assert all(a["provenance"]["git_commit"] == "abc1234" for a in seen)

    def test_it_is_null_when_the_caller_has_none(
            self, tmp_path, one_symbol, one_symbol_config):
        """Unset ``GIT_COMMIT`` is a real state (a manual ``gcloud builds
        submit``), and it must stamp null rather than an empty string that reads
        as a commit."""
        seen = []
        _sweep(tmp_path, one_symbol, one_symbol_config,
               [Scenario("a", {})], artifact_sink=seen.append)
        assert all(a["provenance"]["git_commit"] is None for a in seen)


class TestTheCapitalBaseAndBenchmarkStamps:
    def test_a_wheel_cell_stamps_its_starting_cash_as_the_capital_base(
            self, tmp_path, one_symbol, one_symbol_config):
        seen = []
        _sweep(tmp_path, one_symbol, one_symbol_config,
               [Scenario("a", {})], artifact_sink=seen.append)
        for art in seen:
            assert art["provenance"]["capital_base"] == 50_000.0
            assert art["provenance"]["starting_cash"] == 50_000.0

    def test_a_stamped_base_is_NOT_overwritten_by_starting_cash(self):
        """The Phase C contract. ``capital_base`` is what every ratio divides
        by; a covered-call artifact's is the lot value, and falling back to the
        spec's float would scale every tile by the wrong number while looking
        entirely plausible.

        MUTATION CHECK: read ``meta.starting_cash`` first in the stamp and this
        fails.
        """
        payload = cell_artifact(_hand_built_result(),
                                _meta(starting_cash=100_000.0,
                                      capital_base=10_000.0))
        assert payload["provenance"]["capital_base"] == 10_000.0
        assert payload["provenance"]["starting_cash"] == 100_000.0

    def test_the_cells_benchmark_is_the_ARMS_own_scored_one(
            self, tmp_path, one_symbol, one_symbol_config):
        """Every arm stamps a benchmark, and it reconciles to that arm's OWN
        row: the benchmark is a property of the window, so two arms of one
        window agree — which is exactly why the sidecar can be written once.

        MUTATION CHECK: stamp `None` and the console loses its cross-check
        against the sidecar entirely.
        """
        seen = []
        result = _sweep(tmp_path, one_symbol, one_symbol_config,
                        [Scenario("a", {})], artifact_sink=seen.append)
        by_arm = {a["provenance"]["scenario"]: a for a in seen}
        for row in result.rows:
            bench = by_arm[row.scenario]["benchmark"]
            assert bench is not None
            assert bench["total_return"] == pytest.approx(row.benchmark_return)
            assert bench["capital_base"] == 50_000.0
        assert (by_arm["a"]["benchmark"]["final_value"]
                == pytest.approx(by_arm["base"]["benchmark"]["final_value"]))

    def test_a_report_without_a_benchmark_stamps_null(self):
        payload = cell_artifact(_hand_built_result(), _meta(benchmark=None))
        assert payload["benchmark"] is None
