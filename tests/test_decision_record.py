"""FC-065 Phase 4 — the per-symbol decision record.

Every guard here is shown able to fire, per the plan's standing rule:
  * the outcome enum is CLOSED — an unknown outcome raises, and the recorder
    refuses the row rather than writing an "unknown" one;
  * the dedup key is `(run_id, symbol, stage)`, is carried as the streaming
    insertId, and an unkeyed row is refused outright;
  * `run_id` minted at /scan survives the blob into /run;
  * `underwater_pct` / `uncovered_days` compute from realistic inputs, and
    "could not tell" stays distinguishable from zero.
"""

from datetime import datetime, timezone

import pytest

from src.data.decision_record import (
    ALLOWED_REASONS,
    OUTCOMES,
    OUTCOME_BLOCKED,
    OUTCOME_DROPPED,
    OUTCOME_NOT_ELIGIBLE,
    OUTCOME_NO_CANDIDATES,
    OUTCOME_SOLD,
    REASON_ALREADY_POSITIONED,
    REASON_EXECUTION_FAILED,
    REASON_FLOOR_DIVERGENT,
    REASON_FLOOR_UNRESOLVED,
    REASON_INSUFFICIENT_SHARES,
    REASON_NOT_SELECTED,
    REASON_NO_QUALIFYING_STRIKES,
    REASON_PREVIOUSLY_FAILED,
    STAGE_RUN,
    STAGE_SCAN,
    DecisionRecorder,
    RunDecisionFlusher,
    UncoveredDaysResolver,
    UnknownDecisionOutcome,
    build_decision_row,
    covered_underlyings,
    decision_labels,
    dedup_key,
    is_run_id,
    mint_run_id,
    position_mark_price,
    record_run_stage,
    run_id_from_opportunities,
    run_ts_from_run_id,
    stamp_decision_labels,
    stamp_run_id,
    uncovered_days_sql,
    underwater_pct,
    validate_outcome,
)


class _CapturingWriter:
    def __init__(self):
        self.batches = []

    def write_decision_events(self, rows):
        self.batches.append(list(rows))


# ======================================================================
# The closed enum
# ======================================================================


class TestOutcomeEnumIsClosed:
    def test_every_outcome_has_a_reason_vocabulary(self):
        assert set(ALLOWED_REASONS) == set(OUTCOMES)

    def test_paused_is_deliberately_absent(self):
        # APPROVED DEVIATION (FC-065 Phase 4): the plan's enum text predates
        # OQ-3 and still lists `paused{drawdown, duration_days}`. The operator
        # removed the pause gate entirely, so no code path can produce it. A
        # value no producer can emit is a trap for whoever queries this table,
        # not a state. Re-adding it requires re-adding a gate.
        assert "paused" not in OUTCOMES

    def test_unknown_outcome_raises(self):
        with pytest.raises(UnknownDecisionOutcome):
            validate_outcome("paused", "drawdown")
        with pytest.raises(UnknownDecisionOutcome):
            validate_outcome("skipped")

    def test_reason_is_scoped_to_its_outcome(self):
        # `floor_unresolved` is a blocked-reason. Attaching it to `dropped`
        # would let two different questions share one answer column.
        validate_outcome(OUTCOME_BLOCKED, REASON_FLOOR_UNRESOLVED)
        with pytest.raises(UnknownDecisionOutcome):
            validate_outcome(OUTCOME_DROPPED, REASON_FLOOR_UNRESOLVED)

    def test_sold_takes_no_reason(self):
        validate_outcome(OUTCOME_SOLD, "")
        with pytest.raises(UnknownDecisionOutcome):
            validate_outcome(OUTCOME_SOLD, "because")

    def test_fc038_drop_reasons_are_accepted_verbatim(self):
        from src.strategy.execution_engine import DROP_REASONS

        for reason in DROP_REASONS:
            validate_outcome(OUTCOME_DROPPED, reason)

    def test_unknown_stage_raises(self):
        with pytest.raises(UnknownDecisionOutcome):
            build_decision_row(run_id="r", run_ts=None, stage="monitor",
                               symbol="AAPL", outcome=OUTCOME_SOLD)

    def test_recorder_refuses_an_unknown_outcome_rather_than_writing_it(self):
        writer = _CapturingWriter()
        rec = DecisionRecorder("20260801T140000Z-abcdef12", STAGE_SCAN, writer=writer)
        rec.record("NVDA", "paused", "drawdown")
        assert len(rec) == 0, "an unknown outcome must not become a row"
        rec.flush()
        assert writer.batches == [[]]


# ======================================================================
# Idempotency
# ======================================================================


class TestDedup:
    def test_key_is_run_id_symbol_stage(self):
        assert dedup_key("R1", "NVDA", STAGE_SCAN) == "R1|NVDA|scan"

    def test_same_key_written_twice_yields_one_row(self):
        rec = DecisionRecorder("R1", STAGE_SCAN, writer=_CapturingWriter())
        rec.record("NVDA", OUTCOME_NO_CANDIDATES, REASON_NO_QUALIFYING_STRIKES,
                   uncovered_days=3)
        rec.record("NVDA", OUTCOME_NO_CANDIDATES, REASON_NO_QUALIFYING_STRIKES,
                   uncovered_days=4)
        assert len(rec) == 1
        # Last write wins: the later verdict is the more terminal one.
        assert rec.rows[0]["uncovered_days"] == 4

    def test_different_stages_are_different_rows(self):
        rec_scan = DecisionRecorder("R1", STAGE_SCAN, writer=_CapturingWriter())
        rec_run = DecisionRecorder("R1", STAGE_RUN, writer=_CapturingWriter())
        rec_scan.record("NVDA", OUTCOME_NO_CANDIDATES, REASON_NO_QUALIFYING_STRIKES)
        rec_run.record("NVDA", OUTCOME_SOLD, "")
        assert rec_scan.rows[0]["dedup_key"] != rec_run.rows[0]["dedup_key"]

    def test_writer_uses_the_key_as_insert_id(self):
        from src.data.analytics_writer import AnalyticsWriter

        writer = AnalyticsWriter.__new__(AnalyticsWriter)
        writer._enabled = True
        writer._tables = {"decision_events": "TABLEREF"}
        writer._strategy_id = "wheel"   # FC-075 Seam 4: stamped onto every row
        captured = {}

        class _Client:
            def insert_rows_json(self, table, rows, **kwargs):
                captured["table"] = table
                captured["rows"] = rows
                captured["row_ids"] = kwargs.get("row_ids")
                return []

        writer._client = _Client()
        writer.write_decision_events([
            {"dedup_key": "R1|NVDA|scan", "symbol": "NVDA"},
            {"dedup_key": "R1|AAPL|scan", "symbol": "AAPL"},
        ])
        assert captured["row_ids"] == ["R1|NVDA|scan", "R1|AAPL|scan"], (
            "without insertId, a Cloud Scheduler retry inside the streaming "
            "window duplicates every row")
        assert all("timestamp" in r for r in captured["rows"])

    def test_writer_refuses_unkeyed_rows(self):
        from src.data.analytics_writer import AnalyticsWriter

        writer = AnalyticsWriter.__new__(AnalyticsWriter)
        writer._enabled = True
        writer._tables = {"decision_events": "TABLEREF"}
        calls = []

        class _Client:
            def insert_rows_json(self, table, rows, **kwargs):
                calls.append(rows)
                return []

        writer._client = _Client()
        writer.write_decision_events([{"symbol": "NVDA"}])
        assert calls == [], "an unkeyed decision row must not be written at all"


# ======================================================================
# run_id threading
# ======================================================================


class TestRunId:
    def test_minted_id_is_recognisable_and_unique(self):
        ts = datetime(2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc)
        a, b = mint_run_id(ts), mint_run_id(ts)
        assert is_run_id(a) and is_run_id(b)
        assert a.startswith("20260803T140000Z-")
        assert a != b, "two scans in the same second must not collide"

    def test_naive_scan_time_is_treated_as_utc_shaped(self):
        assert is_run_id(mint_run_id(datetime(2026, 8, 3, 14, 0, 0)))

    def test_non_ids_are_rejected(self):
        for value in (None, "", "run-1", 42, "20260803-abcdef12"):
            assert not is_run_id(value)

    def test_read_back_from_opportunities(self):
        run_id = mint_run_id()
        assert run_id_from_opportunities([{"symbol": "NVDA", "run_id": run_id}]) == run_id

    def test_absent_on_a_pre_phase4_blob(self):
        assert run_id_from_opportunities([{"symbol": "NVDA"}]) is None
        assert run_id_from_opportunities([]) is None

    def test_stamping_then_reading_back_is_a_round_trip(self):
        """The whole threading contract, in the two pure halves that carry it.

        /scan mints -> `stamp_run_id` writes it onto every opportunity ->
        /run recovers it from the opportunity list, which is the ONLY thing
        `get_pending_opportunities` hands over.
        """
        run_id = mint_run_id()
        opportunities = [
            {"symbol": "NVDA", "option_symbol": "NVDA260807C00230000"},
            {"symbol": "IWM", "option_symbol": "IWM260807P00285000"},
        ]
        assert stamp_run_id(opportunities, run_id) == 2
        assert all(o["run_id"] == run_id for o in opportunities)
        assert run_id_from_opportunities(opportunities) == run_id

    def test_stamping_is_a_no_op_without_an_id(self):
        opportunities = [{"symbol": "NVDA"}]
        assert stamp_run_id(opportunities, "") == 0
        assert "run_id" not in opportunities[0]


# ======================================================================
# The two labels
# ======================================================================


class TestUnderwaterPct:
    def test_nvda_shaped_underwater_position(self):
        # Live NVDA lot: floor 218.43 (Alpaca avg_entry_price, FC-065 P1).
        # Sign convention is the plan's: NEGATIVE is underwater.
        assert underwater_pct(199.0, 218.43) == pytest.approx(-0.088953, abs=1e-6)

    def test_above_floor_is_positive(self):
        assert underwater_pct(310.0, 303.50) == pytest.approx(0.021417, abs=1e-6)

    def test_at_the_floor_is_zero(self):
        assert underwater_pct(368.34, 368.34) == 0.0

    def test_missing_inputs_are_none_not_zero(self):
        # 0.0 would render as "exactly at the floor" — a different claim.
        assert underwater_pct(None, 218.43) is None
        assert underwater_pct(199.0, None) is None
        assert underwater_pct(199.0, 0) is None
        assert underwater_pct("garbage", 218.43) is None


class TestPositionMarkPrice:
    def test_derived_from_market_value_over_qty(self):
        assert position_mark_price(
            {"qty": 100, "market_value": 19900.0}) == pytest.approx(199.0)

    def test_multi_lot_position(self):
        assert position_mark_price(
            {"qty": 300, "market_value": 59700.0}) == pytest.approx(199.0)

    def test_unusable_position_is_none(self):
        assert position_mark_price({"qty": 0, "market_value": 100}) is None
        assert position_mark_price({"qty": 100, "market_value": 0}) is None
        assert position_mark_price({}) is None


class TestCoveredUnderlyings:
    def test_short_call_covers_its_underlying(self):
        assert covered_underlyings([
            {"asset_class": "us_option", "symbol": "NVDA260807C00230000", "qty": -1},
        ]) == {"NVDA"}

    def test_long_call_is_not_coverage(self):
        assert covered_underlyings([
            {"asset_class": "us_option", "symbol": "NVDA260807C00230000", "qty": 1},
        ]) == set()

    def test_short_put_is_not_coverage(self):
        assert covered_underlyings([
            {"asset_class": "us_option", "symbol": "NVDA260807P00200000", "qty": -1},
        ]) == set()

    def test_equity_and_garbage_symbols_are_ignored(self):
        # Never substring-match an OCC symbol (FC-041/043/045/048/052).
        assert covered_underlyings([
            {"asset_class": "us_equity", "symbol": "NVDA", "qty": 100},
            {"asset_class": "us_option", "symbol": "NOT_AN_OCC", "qty": -1},
        ]) == set()


class TestUncoveredDaysResolver:
    def test_a_covered_symbol_is_zero_without_touching_bigquery(self):
        resolver = UncoveredDaysResolver(dataset_id="options_wheel")

        def _explode(symbols):
            raise AssertionError("must not query for a symbol we know is covered")

        resolver._lookup_uncovered_days = _explode
        assert resolver.resolve(["NVDA"], covered={"NVDA"}) == {"NVDA": 0}

    def test_uncovered_symbols_take_the_looked_up_day_count(self):
        resolver = UncoveredDaysResolver(dataset_id="options_wheel")
        resolver._lookup_uncovered_days = lambda symbols: {"NVDA": 9, "AMZN": 2}
        out = resolver.resolve(["NVDA", "AMZN", "AAPL"], covered={"AAPL"})
        assert out == {"AAPL": 0, "NVDA": 9, "AMZN": 2}

    def test_lookup_failure_is_none_not_zero(self):
        # "We could not tell" must never render as "covered today".
        resolver = UncoveredDaysResolver(dataset_id="options_wheel")
        resolver._lookup_uncovered_days = lambda symbols: None
        assert resolver.resolve(["NVDA"]) == {"NVDA": None}

    def test_symbol_with_no_history_is_none(self):
        resolver = UncoveredDaysResolver(dataset_id="options_wheel")
        resolver._lookup_uncovered_days = lambda symbols: {"AMZN": 4}
        assert resolver.resolve(["NVDA", "AMZN"]) == {"NVDA": None, "AMZN": 4}

    def test_batches_one_query_for_all_uncovered_symbols(self):
        calls = []
        resolver = UncoveredDaysResolver(dataset_id="options_wheel")

        def _lookup(symbols):
            calls.append(list(symbols))
            return {s: 1 for s in symbols}

        resolver._lookup_uncovered_days = _lookup
        resolver.resolve(["NVDA", "AMZN", "GOOGL"])
        assert len(calls) == 1, "one batched query per scan, not one per symbol"
        assert sorted(calls[0]) == ["AMZN", "GOOGL", "NVDA"]

    def test_bigquery_gate_off_returns_no_comparison(self):
        assert UncoveredDaysResolver(allow_bigquery=False, dataset_id="options_wheel").resolve(["NVDA"]) == {
            "NVDA": None}


# ======================================================================
# Row shape
# ======================================================================


class TestRowShape:
    def _row(self, **kw):
        base = dict(run_id="R1", run_ts=None, stage=STAGE_SCAN, symbol="NVDA",
                    outcome=OUTCOME_NO_CANDIDATES,
                    reason=REASON_NO_QUALIFYING_STRIKES)
        base.update(kw)
        return build_decision_row(**base)

    def test_reason_counts_is_a_repeated_record_not_a_json_string(self):
        # 2026-04-07 hard lesson: never string-ify structures into a BQ column.
        row = self._row(reason_counts={"delta_out_of_range": 12, "premium_too_low": 3})
        assert row["reason_counts"] == [
            {"reason": "delta_out_of_range", "count": 12},
            {"reason": "premium_too_low", "count": 3},
        ]
        assert not isinstance(row["reason_counts"], str)

    def test_zero_counts_are_dropped(self):
        assert self._row(reason_counts={"no_liquidity": 0})["reason_counts"] == []

    def test_endpoint_is_derived_from_stage(self):
        assert self._row()["endpoint"] == "/scan"
        assert self._row(stage=STAGE_RUN, outcome=OUTCOME_SOLD,
                         reason="")["endpoint"] == "/run"

    def test_run_ts_is_utc_isoformat(self):
        row = self._row(run_ts=datetime(2026, 8, 3, 14, 0, 0))
        assert row["run_ts"].startswith("2026-08-03T14:00:00")
        assert row["run_ts"].endswith("+00:00")

    def test_every_schema_column_is_present(self):
        from src.data.analytics_writer import _SCHEMAS, _HAS_BIGQUERY

        if not _HAS_BIGQUERY:
            pytest.skip("bigquery not installed")
        columns = {f.name for f in _SCHEMAS["decision_events"]}
        # `timestamp` and `strategy_id` are stamped by the writer at insert
        # time (FC-075 Seam 4 stamps the strategy at the chokepoint, so no
        # producer can forget it), so the producer row carries neither.
        assert set(self._row()) | {"timestamp", "strategy_id"} == columns

    def test_run_id_and_symbol_are_required(self):
        with pytest.raises(UnknownDecisionOutcome):
            self._row(run_id="")
        with pytest.raises(UnknownDecisionOutcome):
            self._row(symbol="")


# ======================================================================
# The /run stage
# ======================================================================


def _call_opp(symbol="NVDA", contracts=1, **kw):
    opp = {
        "type": "call",
        "symbol": symbol,
        "option_symbol": f"{symbol}260807C00230000",
        "strike_price": 230.0,
        "premium": 1.20,
        "contracts": contracts,
        "shares_owned": 100,
        "max_contracts": 1,
        "cost_basis_per_share": 218.43,
        "current_stock_price": 225.0,
    }
    opp.update(kw)
    return opp


class TestRecordRunStage:
    def _recorder(self):
        return DecisionRecorder("R1", STAGE_RUN, writer=_CapturingWriter())

    def test_a_filled_call_is_sold(self):
        rec = self._recorder()
        opp = _call_opp()
        record_run_stage(
            rec, call_opportunities=[opp], selected=[opp],
            execution_results=[{"opportunity": opp, "success": True,
                                "result": {"success": True, "order_id": "abc",
                                           "contracts": 1}}])
        row = rec.rows[0]
        assert (row["outcome"], row["reason"]) == (OUTCOME_SOLD, "")
        assert row["order_id"] == "abc"
        assert row["contracts"] == 1
        assert row["strike_price"] == 230.0
        assert row["underwater_pct"] == pytest.approx(0.030078, abs=1e-6)

    def test_a_selected_call_whose_order_failed_is_dropped_execution_failed(self):
        rec = self._recorder()
        opp = _call_opp()
        record_run_stage(
            rec, call_opportunities=[opp], selected=[opp],
            execution_results=[{"opportunity": opp, "success": False,
                                "result": {"success": False}}])
        assert (rec.rows[0]["outcome"], rec.rows[0]["reason"]) == (
            OUTCOME_DROPPED, REASON_EXECUTION_FAILED)

    def test_fc038_drop_reason_is_carried_through(self):
        rec = self._recorder()
        opp = _call_opp()
        record_run_stage(rec, call_opportunities=[opp], selected=[],
                         execution_results=[],
                         drop_reasons={"NVDA": "insufficient_available_shares"})
        assert rec.rows[0]["reason"] == "insufficient_available_shares"

    def test_idempotency_filter_is_no_longer_silent(self):
        rec = self._recorder()
        opp = _call_opp()
        record_run_stage(rec, call_opportunities=[opp], selected=[],
                         execution_results=[], already_positioned={"NVDA"})
        assert rec.rows[0]["reason"] == REASON_ALREADY_POSITIONED

    def test_non_retryable_filter_is_no_longer_silent(self):
        rec = self._recorder()
        opp = _call_opp()
        record_run_stage(rec, call_opportunities=[opp], selected=[],
                         execution_results=[], previously_failed={"NVDA"})
        assert rec.rows[0]["reason"] == REASON_PREVIOUSLY_FAILED

    def test_unaccounted_disappearance_still_produces_a_row(self):
        # No selection, no drop reason, no filter — the symbol vanished for a
        # reason nobody recorded. It must still be a row, not a silence.
        rec = self._recorder()
        record_run_stage(rec, call_opportunities=[_call_opp()], selected=[],
                         execution_results=[])
        assert rec.rows[0]["reason"] == REASON_NOT_SELECTED

    def test_a_fill_beats_every_other_signal(self):
        rec = self._recorder()
        opp = _call_opp()
        record_run_stage(
            rec, call_opportunities=[opp], selected=[opp],
            execution_results=[{"opportunity": opp, "success": True,
                                "result": {"success": True, "order_id": "x"}}],
            drop_reasons={"NVDA": "duplicate_underlying"},
            already_positioned={"NVDA"}, previously_failed={"NVDA"})
        assert rec.rows[0]["outcome"] == OUTCOME_SOLD

    def test_a_put_is_refused_even_if_the_caller_passes_one(self):
        """Defence in depth, not caller discipline.

        The caller filters by `is_call_opportunity`, but if that filter is
        ever removed a put reaching here would be recorded as a covered-call
        verdict for a symbol we may hold no shares in.
        """
        rec = self._recorder()
        put = {"type": "put", "symbol": "IWM",
               "option_symbol": "IWM260807P00285000", "contracts": 1}
        record_run_stage(rec, call_opportunities=[put], selected=[],
                         execution_results=[])
        assert rec.rows == []

    def test_a_put_FILL_is_never_recorded_as_a_sold_covered_call(self):
        # The dangerous half: a fabricated `sold` row that the OQ-1 in-gap
        # retro-analysis and FC-044's grid would both read as a real call.
        rec = self._recorder()
        put = {"type": "put", "symbol": "IWM", "strike_price": 285.0,
               "option_symbol": "IWM260807P00285000", "contracts": 1}
        record_run_stage(
            rec, call_opportunities=[put], selected=[put],
            execution_results=[{"opportunity": put, "success": True,
                                "result": {"success": True, "order_id": "p1"}}])
        assert rec.rows == []

    def test_one_row_per_underlying_even_with_three_candidates(self):
        rec = self._recorder()
        opps = [_call_opp(option_symbol=f"NVDA260807C0023{i}000") for i in range(3)]
        record_run_stage(rec, call_opportunities=opps, selected=[],
                         execution_results=[])
        assert len(rec.rows) == 1


class TestFlush:
    def test_rows_are_handed_to_the_writer(self):
        writer = _CapturingWriter()
        rec = DecisionRecorder("R1", STAGE_SCAN, writer=writer)
        rec.record("NVDA", OUTCOME_NO_CANDIDATES, REASON_NO_QUALIFYING_STRIKES)
        rec.record("AAPL", OUTCOME_BLOCKED, REASON_FLOOR_DIVERGENT)
        assert rec.flush() == 2
        assert {r["symbol"] for r in writer.batches[0]} == {"NVDA", "AAPL"}

    def test_a_failing_writer_never_propagates(self):
        class _Boom:
            def write_decision_events(self, rows):
                raise RuntimeError("BigQuery down")

        rec = DecisionRecorder("R1", STAGE_SCAN, writer=_Boom())
        rec.record("NVDA", OUTCOME_NOT_ELIGIBLE, REASON_INSUFFICIENT_SHARES)
        assert rec.flush() == 1  # must not raise: telemetry cannot fail a cycle


# ======================================================================
# Review round 1 — the two BLOCKERS and the HIGH/MEDIUM findings
# ======================================================================


class TestRunRowsCarryRealUncoveredDays:
    """BLOCKER (both reviewers): /run rows had a NULL `uncovered_days`.

    Consequences, both demonstrated by the reviewers with probes:
      * every covered/actively-trading symbol landed in the reader's
        "could not tell" bucket, so the 17:45 check mailed
        DRAWDOWN_PAUSE_ALERT_CHECK_FAILED essentially every trading day —
        alert fatigue kills the one verified notification channel;
      * a genuinely uncovered symbol whose candidates keep getting dropped at
        /run could never accumulate a day count, so the ">= 7 trading days"
        contract was unimplemented for half the outcome enum.
    """

    def _recorder(self):
        return DecisionRecorder("R1", STAGE_RUN, writer=_CapturingWriter())

    @staticmethod
    def _stamped(days, **kw):
        opp = _call_opp(**kw)
        stamp_decision_labels(opp, shares=100, current_price=225.0,
                              underwater=0.030078, uncovered_days=days)
        return opp

    def test_a_dropped_symbol_carries_the_scan_computed_day_count(self):
        rec = self._recorder()
        record_run_stage(rec, call_opportunities=[self._stamped(9)],
                         selected=[], execution_results=[],
                         drop_reasons={"NVDA": "insufficient_available_shares"})
        row = rec.rows[0]
        assert row["uncovered_days"] == 9
        assert row["outcome"] == OUTCOME_DROPPED

    def test_a_dropped_symbol_can_cross_the_alert_threshold(self):
        # The ">= 7" contract must be reachable from the /run half of the enum.
        rec = self._recorder()
        record_run_stage(rec, call_opportunities=[self._stamped(7)],
                         selected=[], execution_results=[])
        assert rec.rows[0]["uncovered_days"] >= 7
        assert rec.rows[0]["reason"] == REASON_NOT_SELECTED

    def test_a_sold_symbol_is_zero_by_definition(self):
        # Whatever the scan measured 15 minutes ago, a call is written now.
        rec = self._recorder()
        opp = self._stamped(9)
        record_run_stage(
            rec, call_opportunities=[opp], selected=[opp],
            execution_results=[{"opportunity": opp, "success": True,
                                "result": {"success": True, "order_id": "x"}}])
        assert rec.rows[0]["outcome"] == OUTCOME_SOLD
        assert rec.rows[0]["uncovered_days"] == 0

    def test_a_covered_symbol_with_candidates_reports_zero(self):
        # The scanner's covered short-circuit stamps 0; the run row must keep
        # it. Previously only the NO-candidates path was covered by a test,
        # which is exactly the path a covered symbol does NOT take.
        rec = self._recorder()
        record_run_stage(rec, call_opportunities=[self._stamped(0)],
                         selected=[], execution_results=[])
        assert rec.rows[0]["uncovered_days"] == 0

    def test_other_labels_ride_along_too(self):
        rec = self._recorder()
        record_run_stage(rec, call_opportunities=[self._stamped(9)],
                         selected=[], execution_results=[])
        row = rec.rows[0]
        assert row["shares"] == 100
        assert row["current_price"] == pytest.approx(225.0)
        assert row["underwater_pct"] == pytest.approx(0.030078)

    def test_an_unstamped_blob_degrades_honestly(self):
        # A blob written before this shipped: derivable labels reconstructed,
        # uncovered_days left NULL rather than invented as 0.
        rec = self._recorder()
        record_run_stage(rec, call_opportunities=[_call_opp()],
                         selected=[], execution_results=[])
        row = rec.rows[0]
        assert row["uncovered_days"] is None
        assert row["current_price"] == pytest.approx(225.0)
        assert row["underwater_pct"] == pytest.approx(0.030078, abs=1e-6)

    def test_stamping_survives_a_json_round_trip(self):
        # The labels travel through the GCS blob, which is JSON.
        import json
        opp = self._stamped(9)
        assert decision_labels(json.loads(json.dumps(opp)))["uncovered_days"] == 9


class TestRunDecisionFlusher:
    """MEDIUM: a crash inside execute_batch produced a 500 with ZERO run rows,
    including for orders that had already reached the broker."""

    def _flusher(self):
        f = RunDecisionFlusher(writer=_CapturingWriter())
        f.run_id = "R1"
        f.opportunities.append(_call_opp())
        return f

    def test_flushes_once(self):
        f = self._flusher()
        assert f.flush(selected=[], execution_results=[]) is True
        assert f.flush(selected=[], execution_results=[]) is False

    def test_no_run_id_is_a_no_op(self):
        f = RunDecisionFlusher(writer=_CapturingWriter())
        assert f.flush() is False

    # -- the two /run early exits -------------------------------------- #
    #
    # REGRESSION (confirmation pass, after the flush-in-finally fix): both
    # exits happen BEFORE selection or execution exist, so they call
    # `flush()` with only their filter kwarg. `record_run_stage` required
    # `selected` and `execution_results`, so both raised TypeError inside the
    # flusher's except — zero rows written — and the once-guard had already
    # set `flushed=True`, so the `finally` could not retry. The two exits are
    # exactly the "a symbol silently vanished between the stages" cases this
    # phase exists to record, so they failed where it mattered most.

    def test_the_all_non_retryable_exit_writes_its_rows(self):
        f = self._flusher()
        assert f.flush(previously_failed={"NVDA"}) is True
        rows = f._writer.batches[0]
        assert [(r["symbol"], r["outcome"], r["reason"]) for r in rows] == [
            ("NVDA", OUTCOME_DROPPED, REASON_PREVIOUSLY_FAILED)]

    def test_the_idempotency_exit_writes_its_rows(self):
        f = self._flusher()
        assert f.flush(already_positioned={"NVDA"},
                       previously_failed=set()) is True
        rows = f._writer.batches[0]
        assert [(r["symbol"], r["outcome"], r["reason"]) for r in rows] == [
            ("NVDA", OUTCOME_DROPPED, REASON_ALREADY_POSITIONED)]

    def test_record_run_stage_defaults_the_execution_arguments(self):
        # The signature itself is the contract the two exits rely on.
        rec = DecisionRecorder("R1", STAGE_RUN, writer=_CapturingWriter())
        record_run_stage(rec, call_opportunities=[_call_opp()],
                         previously_failed={"NVDA"})
        assert rec.rows[0]["reason"] == REASON_PREVIOUSLY_FAILED

    def test_a_crash_after_a_fill_still_records_the_fill(self):
        """The endpoint's control flow, in miniature."""
        f = self._flusher()
        opp = f.opportunities[0]
        results = []
        try:
            results.append({"opportunity": opp, "success": True,
                            "result": {"success": True, "order_id": "abc"}})
            raise RuntimeError("boom inside execute_batch")
        except RuntimeError:
            pass
        finally:
            f.flush(selected=[opp], execution_results=results)
        assert f.flushed
        written = f._writer.batches[0]
        assert [(r["symbol"], r["outcome"], r["order_id"]) for r in written] == [
            ("NVDA", OUTCOME_SOLD, "abc")]

    def test_a_broken_recorder_never_propagates_out_of_a_finally(self):
        class _Boom:
            def write_decision_events(self, rows):
                raise RuntimeError("BigQuery down")

        f = RunDecisionFlusher(writer=_Boom())
        f.run_id = "R1"
        f.opportunities.append(_call_opp())
        assert f.flush(selected=[], execution_results=[]) is True  # must not raise


class TestRunEndpointFlushesInAFinally:
    """Source-level, because the alternative is standing up Flask plus GCS.

    The contract is structural: `/run`'s flush must sit in a `finally`, not on
    the success path. The scanner's equivalent is pinned by 11 behavioural
    tests; this pins the one line that makes the same guarantee for /run.
    """

    def test_the_flush_is_inside_a_finally(self):
        from pathlib import Path
        import re

        src = Path(__file__).resolve().parents[1] / "deploy" / "cloud_run_server.py"
        text = src.read_text()
        body = text[text.index("def trigger_strategy("):text.index("def _is_market_open(")]
        assert re.search(r"finally:\s*(#[^\n]*\n\s*)*decisions\.flush\(", body), (
            "/run's decision flush must be in a `finally` — a crash inside "
            "execute_batch otherwise loses the record of orders that already "
            "reached the broker")


class TestUncoveredDaysSql:
    """MEDIUM: the SQL was invisible to all 951 tests.

    The hermeticity guard patches `_lookup_uncovered_days` wholesale, so a
    reviewer's mutation deleting the calendar-symbol join condition — a ~9x
    day-count inflation in production — survived the entire suite.
    """

    def _sql(self):
        return uncovered_days_sql("proj.options_wheel")

    def test_the_calendar_join_is_symbol_scoped(self):
        # Without this the LEFT JOIN matches every ingested symbol's bar for
        # each date, multiplying the day count by the ingested-symbol count.
        assert "b.symbol = @calendar_symbol" in self._sql()

    def test_the_anchor_is_greatest_not_coalesce(self):
        # Verified against real history: AMZN called away 2026-04-23,
        # re-assigned 2026-06-06. Under COALESCE the call date wins whenever it
        # exists, so the NEW lot reports ~30 uncovered days on day one and
        # trips the ">= 7" alert immediately. This is the wheel's normal cycle.
        sql = self._sql()
        assert "GREATEST(" in sql
        assert "COALESCE(last_call_date, last_assignment_date)" not in sql, (
            "COALESCE makes a re-assignment after a call-away inherit the old "
            "lot's clock")

    def test_both_anchor_candidates_are_considered(self):
        sql = self._sql()
        assert "last_call_date" in sql and "last_assignment_date" in sql

    def test_the_window_is_bounded_forward(self):
        assert "b.date > a.anchor_date" in self._sql()
        assert "b.date <= CURRENT_DATE()" in self._sql()

    def test_calendar_staleness_is_compensated(self):
        # Bars ingest at 17:00 ET; an intraday run sees a calendar ending
        # yesterday. Uncompensated, the effective alert threshold drifts to ~9.
        sql = self._sql()
        assert "GENERATE_DATE_ARRAY" in sql
        assert "Saturday" in sql and "Sunday" in sql

    def test_it_is_parameterised_and_scoped_to_the_dataset(self):
        sql = self._sql()
        assert "@symbols" in sql and "@calendar_symbol" in sql
        assert "proj.options_wheel.trades_from_activities" in sql
        assert "proj.options_wheel.stock_history_from_alpaca" in sql

    @pytest.mark.real_bq_lookup
    def test_the_resolver_uses_this_builder(self):
        # A second, inline copy of the SQL would be invisible again. Opts out
        # of the hermeticity stub (which replaces the very method under test)
        # via the established marker; the BigQuery client is injected, so
        # nothing reaches the network.
        resolver = UncoveredDaysResolver(dataset_id="proj.options_wheel")
        seen = {}

        class _Job:
            @staticmethod
            def result(timeout=None):
                return []

        class _Client:
            def query(self, query, job_config=None):
                seen["query"] = query
                return _Job()

        resolver._bq_client = _Client()
        resolver._lookup_uncovered_days(["NVDA"])
        assert seen["query"] == uncovered_days_sql("proj.options_wheel")


class TestRunTsIsStableAcrossStages:
    """MEDIUM: each stage defaulted run_ts to its own now(), ~15 minutes
    apart, so the fill-rate query's DISTINCT run_id, run_ts saw two 'cycles'
    per run — while the schema documented it as the stable cycle start."""

    def test_derived_from_the_run_id(self):
        run_id = mint_run_id(datetime(2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc))
        assert run_ts_from_run_id(run_id) == datetime(
            2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc)

    def test_both_stages_agree(self):
        run_id = mint_run_id()
        scan = DecisionRecorder(run_id, STAGE_SCAN, writer=_CapturingWriter())
        run = DecisionRecorder(run_id, STAGE_RUN, writer=_CapturingWriter())
        scan.record("NVDA", OUTCOME_NO_CANDIDATES, REASON_NO_QUALIFYING_STRIKES)
        run.record("AAPL", OUTCOME_SOLD, "")
        assert scan.rows[0]["run_ts"] == run.rows[0]["run_ts"]

    def test_a_garbage_id_falls_back_rather_than_crashing(self):
        assert run_ts_from_run_id("not-a-run-id") is None
        assert DecisionRecorder("legacy", STAGE_RUN,
                                writer=_CapturingWriter()).run_ts is not None


class TestMintRunIdTimezone:
    """LOW: a naive datetime was stamped under a `Z` suffix — local time
    labelled UTC. Harmless on Cloud Run (TZ=UTC), wrong everywhere else, and
    now load-bearing because run_ts is derived back out of the string."""

    def test_a_naive_local_time_is_converted_not_relabelled(self):
        naive = datetime(2026, 8, 3, 14, 0, 0)
        expected = naive.astimezone(timezone.utc)
        assert mint_run_id(naive).startswith(expected.strftime("%Y%m%dT%H%M%S") + "Z-")

    def test_an_aware_time_round_trips(self):
        aware = datetime(2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc)
        assert run_ts_from_run_id(mint_run_id(aware)) == aware


class TestEarningsReasonsAreScopedToBlocked:
    """FC-013 DD-4 — test 17.

    Two reasons, not one. A row that says "earnings blackout" when Finnhub was
    down is a mislabel, and the closed vocabulary exists precisely so rows
    cannot misfile. Both are `blocked` reasons only.
    """

    def test_both_earnings_reasons_are_accepted_under_blocked(self):
        from src.data.decision_record import (
            REASON_EARNINGS_BLACKOUT, REASON_EARNINGS_UNKNOWN)

        validate_outcome(OUTCOME_BLOCKED, REASON_EARNINGS_BLACKOUT)
        validate_outcome(OUTCOME_BLOCKED, REASON_EARNINGS_UNKNOWN)

    def test_cross_pairing_still_raises(self):
        """The closed enum must not silently widen while it is being extended."""
        from src.data.decision_record import (
            OUTCOME_NO_CANDIDATES, REASON_EARNINGS_BLACKOUT,
            REASON_EARNINGS_UNKNOWN)

        with pytest.raises(UnknownDecisionOutcome):
            validate_outcome(OUTCOME_DROPPED, REASON_EARNINGS_BLACKOUT)
        with pytest.raises(UnknownDecisionOutcome):
            validate_outcome(OUTCOME_NO_CANDIDATES, REASON_EARNINGS_UNKNOWN)

    def test_the_pre_existing_floor_reasons_are_unweakened(self):
        from src.data.decision_record import (
            REASON_FLOOR_DIVERGENT, REASON_FLOOR_UNRESOLVED)

        validate_outcome(OUTCOME_BLOCKED, REASON_FLOOR_UNRESOLVED)
        validate_outcome(OUTCOME_BLOCKED, REASON_FLOOR_DIVERGENT)
        with pytest.raises(UnknownDecisionOutcome):
            validate_outcome(OUTCOME_DROPPED, REASON_FLOOR_DIVERGENT)

    def test_the_two_reasons_are_distinct_strings(self):
        """Collapsing them to one value would make a Finnhub outage and a real
        blackout indistinguishable in the table forever."""
        from src.data.decision_record import (
            REASON_EARNINGS_BLACKOUT, REASON_EARNINGS_UNKNOWN)

        assert REASON_EARNINGS_BLACKOUT != REASON_EARNINGS_UNKNOWN
