"""FC-112 PR-2 — the pre-registered read tool (T-10 .. T-14).

Plan: `docs/plans/fc-112.md`. These tests exist to make ONE thing hard: editing
the decision rule after the numbers have been seen. Everything else here is in
service of that — the rule branches (T-10), the structural VOIDs that stop a
broken measurement being reported as a finding (T-11), the selection shape that
a BigQuery mistake would silently corrupt (T-12), the holdout column's parity
with the engine's own (T-13), and the verdict block's provenance (T-14).

No BigQuery, no GCS, no network: the rule is a pure function over rows, and the
read path is exercised against a fake client that emulates DD-6's documented
selection rules.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

import pytest

from tools.diagnostics import fc112_roll_trigger_read as READ

SYMBOLS = ["AAPL", "AMD", "AMZN", "GOOGL", "IWM", "NVDA", "UNH"]
BASE_ANN = 0.06
BASE_OPT = 3_000.0


def _opt_pnl_for(delta_pp: Optional[float]) -> float:
    """The `option_pnl` a t100 cell needs to show `delta_pp` on the option leg.

    The inverse of `delta_opt_pp`: pp -> dollars over the FIT split.
    """
    if delta_pp is None:
        return BASE_OPT
    dollars = (delta_pp / 100.0) * READ.STARTING_CASH * (
        READ.FIT_WINDOW_DAYS / READ.ANNUALISATION_DAYS)
    return BASE_OPT + dollars


def cell(arm: str, symbol: str, split: str = "fit", **kw: Any) -> READ.Cell:
    """One `scenario_runs` row, with the study's defaults."""
    row: Dict[str, Any] = dict(
        run_id="run_decision", pin_id="pin_decision", window_end="2026-09-19",
        engine_version="fc-112-wheel-roll-reach", engine_identity="e69ba313",
        submitted_via="battery", symbol=symbol, split=split,
        scenario_name=arm, verdict="marginal", measured=True,
        annualized_return=BASE_ANN, total_return=BASE_ANN,
        option_pnl=BASE_OPT, stock_pnl_realized=500.0,
        stock_pnl_unrealized=-100.0,
        rolls_executed=2, itm_rolls=1, otm_roll_outs=1,
        roll_net_credit=300.0, itm_roll_credit=100.0,
        otm_roll_out_credit=200.0, failed_roll_btc_debit=0.0,
        roll_legs_resting=1, roll_legs_marketable=3,
        roll_skips=json.dumps({"no_credit_candidate": 4, "not_itm_enough": 9}),
        roll_fill_mode=READ.ARM_FILL_MODE.get(arm, "limit"),
        assignment_rate=0.2, cycles_completed=3, calls_sold=6,
        max_drawdown=-0.08,
    )
    if arm in READ.T100_ARMS:
        # 1.00 cannot fire below the strike, so the OTM roll-outs are empty by
        # construction and the executed count is the ITM defences alone.
        row.update(otm_roll_outs=0, rolls_executed=1, otm_roll_out_credit=0.0)
    row.update(kw)
    return READ.Cell.from_row(row)


def cells_for(read_deltas: Dict[str, float],
              *, opt_delta_pp: Optional[float] = None,
              symbols: Sequence[str] = tuple(SYMBOLS),
              holdout_symbols: Sequence[str] = tuple(SYMBOLS),
              holdout_delta_pp: Optional[Dict[str, float]] = None,
              ) -> List[READ.Cell]:
    """Cells for all three reads, each arm offset by its read's delta (pp).

    `opt_delta_pp` defaults to the `limit` read's delta, i.e. an effect fully
    located in the option leg — the case DD-1 condition 2 is designed to let
    through.
    """
    if opt_delta_pp is None:
        opt_delta_pp = read_deltas.get(READ.PRIMARY_READ, 0.0)
    out: List[READ.Cell] = []
    for name, base_arm, arm in READ.READS:
        delta = read_deltas.get(name, 0.0)
        for symbol in symbols:
            out.append(cell(base_arm, symbol))
            out.append(cell(
                arm, symbol, annualized_return=BASE_ANN + delta / 100.0,
                total_return=BASE_ANN + delta / 100.0,
                option_pnl=(_opt_pnl_for(opt_delta_pp) if name ==
                            READ.PRIMARY_READ else BASE_OPT)))
    for symbol in holdout_symbols:
        hold = (holdout_delta_pp or {}).get(
            symbol, read_deltas.get(READ.PRIMARY_READ, 0.0))
        out.append(cell(READ.BASE_ARM, symbol, "holdout"))
        out.append(cell("t100", symbol, "holdout",
                        annualized_return=BASE_ANN + hold / 100.0))
    return out


def verdict_for(cells: Sequence[READ.Cell], **kw: Any) -> READ.Verdict:
    return READ.analyse(cells, **kw).verdict


# --------------------------------------------------------------------------- #
# T-10 — the rule's branches. Catches: a rule edited after the data is seen.
# --------------------------------------------------------------------------- #
class TestT10TheRuleBranches:
    def test_the_constants_are_module_level_and_carry_the_signed_values(self):
        """D-1, signed 2026-09-12. Changing any of these is a new SHA."""
        assert READ.TIE_BREAK == "move"
        assert READ.MIN_EFFECT_PP == 0.5
        assert READ.MIN_SIGN_COUNT == {7: 6, 6: 5}
        # TIE_BREAK is load-bearing, not decorative: it IS the default.
        assert READ.DEFAULT_RESOLUTION == "1.00"

    def test_apply_rule_takes_no_threshold_arguments(self):
        """A threshold that can be passed in can be chosen after the read."""
        params = set(inspect.signature(READ.apply_rule).parameters)
        assert params == {"reads", "opt_read", "holdout", "oos",
                          "monitor_flip", "structural"}
        banned = ("min_", "threshold", "tie", "effect", "sign_count", "floor")
        assert not [p for p in params
                    for b in banned if b in p.lower()], params

    def test_a_large_consistent_option_leg_effect_keeps_0_98(self):
        v = verdict_for(cells_for({"limit": -2.0, "haircut": -2.0,
                                   "noimm": -2.0}))
        assert v.label == "KEEP"
        assert v.resolves_to == "0.98"

    def test_the_mirror_image_in_every_read_moves_to_1_00(self):
        v = verdict_for(cells_for({"limit": 2.0, "haircut": 2.0,
                                   "noimm": 2.0}))
        assert v.label == "MOVE"
        assert v.resolves_to == "1.00"

    def test_nothing_crossing_either_threshold_is_mixed_null_and_moves(self):
        """'No measurable difference' is not a reason to differ (D-1)."""
        v = verdict_for(cells_for({"limit": -0.1, "haircut": -0.1,
                                   "noimm": -0.1}))
        assert v.label == "MIXED-NULL"
        assert v.resolves_to == "1.00"
        assert "no_read_crosses_either_threshold" in v.reasons[0]

    def test_a_sign_count_without_the_median_does_not_keep(self):
        """7 negative symbols at 0.2 pp each is $200/yr, not a mechanism."""
        v = verdict_for(cells_for({"limit": -0.2, "haircut": -0.2,
                                   "noimm": -0.2}))
        assert v.label == "MIXED-NULL"

    @pytest.mark.parametrize("reversed_read", ["haircut", "noimm"])
    def test_a_read_reversing_a_keep_is_mixed_conflict_and_stays_at_0_98(
            self, reversed_read):
        """DD-3: a KEEP that holds at one end of the bracket and not the other
        is a conflict, and the plan does not resolve a conflict by rule."""
        deltas = {"limit": -2.0, "haircut": -2.0, "noimm": -2.0}
        deltas[reversed_read] = +2.0
        v = verdict_for(cells_for(deltas))
        assert v.label == "MIXED-CONFLICT"
        assert v.resolves_to == "0.98"
        assert any("read_sign_conflict" in r for r in v.reasons)

    def test_a_refuting_holdout_at_four_comparable_symbols_is_a_conflict(self):
        keep = {"limit": -2.0, "haircut": -2.0, "noimm": -2.0}
        four = SYMBOLS[:4]
        cells = cells_for(keep, holdout_symbols=four,
                          holdout_delta_pp={s: +2.0 for s in four[:3]})
        analysis = READ.analyse(cells)
        assert analysis.holdout.comparable == 4
        assert analysis.holdout.agreeing == 1
        assert analysis.verdict.label == "MIXED-CONFLICT"
        assert any("holdout_refutes" in r for r in analysis.verdict.reasons)

    def test_a_holdout_of_three_is_uninformative_and_does_not_block(self):
        """Today the wheel's holdout measures exactly three cells."""
        keep = {"limit": -2.0, "haircut": -2.0, "noimm": -2.0}
        three = SYMBOLS[:3]
        cells = cells_for(keep, holdout_symbols=three,
                          holdout_delta_pp={s: +2.0 for s in three})
        analysis = READ.analyse(cells)
        assert analysis.holdout.comparable == 3
        assert not analysis.holdout.informative
        assert analysis.verdict.label == "KEEP"

    def test_an_out_of_sample_read_pointing_the_other_way_is_a_conflict(self):
        keep = cells_for({"limit": -2.0, "haircut": -2.0, "noimm": -2.0})
        oos = cells_for({"limit": +2.0, "haircut": +2.0, "noimm": +2.0},
                        symbols=SYMBOLS[:5], holdout_symbols=())
        v = verdict_for(keep, oos_cells=oos)
        assert v.label == "MIXED-CONFLICT"
        assert any("oos_refutes" in r for r in v.reasons)

    def test_a_primary_sign_flip_across_the_monitor_points_is_a_conflict(self):
        """DD-4: four overlapping Saturdays are one read, and a sign flip is
        the one thing they CAN say."""
        keep = cells_for({"limit": -2.0, "haircut": -2.0, "noimm": -2.0})
        v = verdict_for(keep, monitor_medians=[-2.0, -1.8, +0.4, -2.1])
        assert v.label == "MIXED-CONFLICT"
        assert any("monitor_sign_flip" in r for r in v.reasons)

    def test_a_keep_whose_effect_is_not_in_the_option_leg_is_refused(self):
        """The roller's own claim is about the option leg. A 'KEEP' carried by
        `stock_pnl_*` is a call-strike / assignment-timing effect, and
        one-sidedness sends it to the default."""
        cells = cells_for({"limit": -2.0, "haircut": -2.0, "noimm": -2.0},
                          opt_delta_pp=-0.05)
        analysis = READ.analyse(cells)
        assert analysis.reads["limit"].n_neg == 7
        assert analysis.verdict.label == "MIXED-NULL"
        assert analysis.verdict.resolves_to == "1.00"
        assert analysis.verdict.reasons == (
            "keep_refused_option_leg_not_located",)

    def test_five_measured_symbols_is_a_void_not_a_thinner_read(self):
        """M < 6 has no entry in MIN_SIGN_COUNT; inventing one is inventing a
        rule after the fact."""
        v = verdict_for(cells_for({"limit": -2.0, "haircut": -2.0,
                                   "noimm": -2.0}, symbols=SYMBOLS[:5]))
        assert v.label == "VOID"
        assert any("insufficient_measured" in r for r in v.reasons)
        assert v.is_void

    def test_six_measured_symbols_reads_its_own_sign_count(self):
        six = SYMBOLS[:6]
        analysis = READ.analyse(cells_for(
            {"limit": -2.0, "haircut": -2.0, "noimm": -2.0}, symbols=six,
            holdout_symbols=six))
        assert analysis.reads["limit"].m == 6
        assert READ.MIN_SIGN_COUNT[6] == 5
        assert analysis.verdict.label == "KEEP"


# --------------------------------------------------------------------------- #
# T-11 — the structural VOIDs. A MIXED is a statement about the market; a VOID
# is a statement about the measurement, and collapsing the two would let a
# broken arm be reported as a finding about the roller.
# --------------------------------------------------------------------------- #
KEEP_DELTAS = {"limit": -2.0, "haircut": -2.0, "noimm": -2.0}


def _void_names(verdict: READ.Verdict) -> List[str]:
    return [r.split(":", 1)[1] for r in verdict.reasons
            if r.startswith("structural:")]


class TestT11StructuralChecks:
    def test_an_otm_roll_out_on_a_1_00_row_voids_the_read(self):
        """At 1.00 the trigger cannot fire below the strike, so the count is
        empty by construction. A non-zero one means the override never reached
        the engine — and the arm is not the arm it claims to be."""
        cells = cells_for(KEEP_DELTAS)
        cells.append(cell("t100", "AAPL", "fit", otm_roll_outs=1,
                          rolls_executed=2))
        v = verdict_for(cells)
        assert v.label == "VOID"
        assert "t100_arms_have_no_otm_roll_outs" in _void_names(v)

    def test_a_row_whose_fill_mode_contradicts_its_arm_voids_the_read(self):
        """The `haircut` read IS the other end of DD-3's spread bracket; a
        haircut arm that filled at the limit is not that end."""
        cells = [c for c in cells_for(KEEP_DELTAS)
                 if not (c.scenario_name == "t100_haircut"
                         and c.symbol == "AMD" and c.split == "fit")]
        cells.append(cell("t100_haircut", "AMD", "fit", roll_fill_mode="limit"))
        v = verdict_for(cells)
        assert v.label == "VOID"
        assert "roll_fill_mode_matches_its_arm" in _void_names(v)

    def test_a_pre_fc116_row_voids_the_read(self):
        """`roll_fill_mode IS NULL` means the row was written by an engine
        before `fc-116-roll-limit-fills` — the haircut model, with no
        `limit`/`haircut` bracket to be either end of."""
        cells = cells_for(KEEP_DELTAS)
        cells.append(cell("base", "UNH", "fit", roll_fill_mode=None))
        v = verdict_for(cells)
        assert v.label == "VOID"
        assert "no_pre_fc116_rows" in _void_names(v)

    def test_a_roll_count_that_does_not_split_voids_the_read(self):
        cells = cells_for(KEEP_DELTAS)
        cells.append(cell("base", "IWM", "fit", rolls_executed=9, itm_rolls=1,
                          otm_roll_outs=1))
        v = verdict_for(cells)
        assert v.label == "VOID"
        assert "rolls_executed_equals_itm_plus_otm" in _void_names(v)

    def test_a_placebo_arm_voids_the_read(self):
        """Two arms that come back identical read, to an unwary reader, as
        'the trigger does not matter'. It is the most dangerous null this study
        can produce (FC-096-a), so it is a VOID, never a MIXED-NULL."""
        cells: List[READ.Cell] = []
        for name, base_arm, arm in READ.READS:
            for symbol in SYMBOLS:
                cells.append(cell(base_arm, symbol))
                # Identical on every column the placebo gate compares.
                cells.append(cell(arm, symbol, otm_roll_outs=1,
                                  rolls_executed=2))
        v = verdict_for(cells)
        assert v.label == "VOID"
        assert "placebo_gate" in _void_names(v)

    def test_a_read_that_straddled_a_deploy_voids(self):
        for column in ("engine_version", "engine_identity"):
            cells = cells_for(KEEP_DELTAS)
            cells.append(cell("base", "NVDA", "fit", **{column: "other"}))
            v = verdict_for(cells)
            assert v.label == "VOID", column
            assert f"{column}_uniform" in _void_names(v)

    def test_a_364_day_pin_is_caught_by_the_standing_base_cell(self):
        """The GOOGL proof pin is 364 days and its fit cell differs from the
        standing one by one day of option P&L — 0.085731 vs 0.085773,
        `option_pnl` 4,151 vs 4,178. A pin meant to ask the standing question
        and asking a one-day-shorter one would put that $27 inside every delta
        the study reads."""
        cells = [c for c in cells_for(KEEP_DELTAS)
                 if not (c.scenario_name == "base" and c.symbol == "GOOGL"
                         and c.split == "fit")]
        cells.append(cell("base", "GOOGL", "fit",
                          annualized_return=0.085731, option_pnl=4151.0))
        standing = [cell("base", "GOOGL", "fit", pin_id=None,
                         annualized_return=0.085773, option_pnl=4178.0)]
        v = verdict_for(cells, standing=standing)
        assert v.label == "VOID"
        assert "pin_base_equals_standing_base" in _void_names(v)

    def test_a_365_day_pin_passes_the_same_check(self):
        cells = cells_for(KEEP_DELTAS)
        standing = [cell(READ.BASE_ARM, s, "fit", pin_id=None) for s in SYMBOLS]
        analysis = READ.analyse(cells, standing)
        check = [c for c in analysis.structural
                 if c.name == "pin_base_equals_standing_base"][0]
        assert check.passed
        assert "7 pin base cells" in check.detail
        assert analysis.verdict.label == "KEEP"

    def test_a_dedup_target_that_never_finished_voids_the_read(self):
        """The VOID comes from `resolve_dedup`, not the query: the cell query's
        `status = 'done'` would just drop the pin, and a read that silently
        loses a pin changes MIN_SIGN_COUNT under the reader's feet."""
        v = verdict_for(cells_for(KEEP_DELTAS),
                        dedup_voids=["dedup_target_not_done:aaa->bbb"])
        assert v.label == "VOID"
        assert any("dedup_target_not_done" in r for r in v.reasons)


# --------------------------------------------------------------------------- #
# T-12 — the read shape, against a fake client.
#
# The fake is not a mock of the tool: it EMULATES the query the tool sends,
# applying DD-6's documented selection rules to raw rows the way BigQuery
# would. The SQL text itself is pinned separately and byte-for-byte, so the two
# halves of the claim — "the tool sends this SQL" and "the tool consumes what
# that SQL returns" — are both held down.
#
# Catches: every selection mistake found in plan section "Found while planning"
# and while writing DD-6.
# --------------------------------------------------------------------------- #
STATUS_RANK = {"submitted": 0, "running": 1, "deduplicated": 2, "failed": 3,
               "done": 4}
CELL_COLUMNS = (
    "symbol", "split", "scenario_name", "verdict", "measured",
    "annualized_return", "total_return", "option_pnl", "stock_pnl_realized",
    "stock_pnl_unrealized", "rolls_executed", "itm_rolls", "otm_roll_outs",
    "roll_net_credit", "itm_roll_credit", "otm_roll_out_credit",
    "failed_roll_btc_debit", "roll_legs_resting", "roll_legs_marketable",
    "roll_skips", "roll_fill_mode") + READ.SECONDARY_METRIC_COLUMNS


class _FakeJob:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def result(self):
        return list(self._rows)


class FakeBigQuery:
    """DD-6's selection rules, applied to raw rows the way BigQuery would."""

    def __init__(self, sweeps: List[Dict[str, Any]],
                 cells: List[Dict[str, Any]]):
        self.sweeps = sweeps
        self.cells = cells
        self.seen: List[str] = []

    # -- the `latest` and `resolved` CTEs ---------------------------------- #
    def _latest(self) -> Dict[str, Dict[str, Any]]:
        best: Dict[str, Any] = {}
        for row in self.sweeps:
            rank = (row.get("written_at", ""),
                    STATUS_RANK.get(row.get("status"), -1))
            current = best.get(row["run_id"])
            if current is None or rank > current[0]:
                best[row["run_id"]] = (rank, row)
        return {k: v[1] for k, v in best.items()}

    def _resolved(self) -> List[Dict[str, Any]]:
        latest = self._latest()
        out = []
        for row in latest.values():
            spec = json.loads(row.get("spec_json") or "{}")
            if spec.get("strategy", "wheel") != "wheel":
                continue  # IFNULL(..., 'wheel'): an ABSENT key means wheel
            target = (latest.get(row.get("deduplicated_to"))
                      if row.get("status") == "deduplicated" else None)
            out.append({
                "source_run_id": row["run_id"],
                "source_status": row.get("status"),
                "deduplicated_to": row.get("deduplicated_to"),
                "pin_id": row.get("pin_id"),
                "window_end": row.get("window_end"),
                "submitted_via": row.get("submitted_via"),
                "resolved_run_id": (target or row)["run_id"],
                "resolved_status": (target or row).get("status"),
                "engine_version": (target or row).get("engine_version"),
                "engine_identity": (target or row).get("engine_identity"),
            })
        return out

    @staticmethod
    def _matches(row: Dict[str, Any], sql: str,
                 params: Dict[str, Any]) -> bool:
        if "pin_id IN UNNEST(@pin_ids)" in sql:
            if row.get("pin_id") not in params.get("pin_ids", []):
                return False
        elif "pin_id IS NULL" in sql:
            if row.get("pin_id") is not None:
                return False
            if row.get("submitted_via") != "battery":
                return False
        elif "run_id IN UNNEST(@run_ids)" in sql:
            key = ("resolved_run_id" if "scenario_runs" in sql
                   else "source_run_id")
            if row.get(key) not in params.get("run_ids", []):
                return False
        if "window_end = @window_end" in sql:
            if str(row.get("window_end")) != str(params.get("window_end")):
                return False
        return True

    def query(self, sql: str, job_config=None):
        self.seen.append(sql)
        params: Dict[str, Any] = {}
        for p in (getattr(job_config, "query_parameters", None) or []):
            params[p.name] = getattr(p, "value", None) or getattr(
                p, "values", None)
        resolved = self._resolved()
        if "source_run_id" in sql:                      # the sweep projection
            return _FakeJob([r for r in resolved
                             if self._matches(r, sql, params)])
        rows = []
        for sweep in resolved:
            if sweep["resolved_status"] != "done":      # s.status = 'done'
                continue
            if not self._matches(sweep, sql, params):
                continue
            for raw in self.cells:
                if raw["run_id"] != sweep["resolved_run_id"]:
                    continue
                row = {"pin_id": sweep["pin_id"],
                       "window_end": sweep["window_end"],
                       "engine_version": sweep["engine_version"],
                       "engine_identity": sweep["engine_identity"],
                       "submitted_via": sweep["submitted_via"],
                       "run_id": sweep["resolved_run_id"]}
                row.update({c: raw.get(c) for c in CELL_COLUMNS})
                rows.append(row)
        return _FakeJob(rows)


def sweep_row(run_id: str, status: str, written_at: str, **kw: Any
              ) -> Dict[str, Any]:
    row = dict(run_id=run_id, status=status, written_at=written_at,
               submitted_at="2026-09-19T08:00:00Z", pin_id=None,
               window_end="2026-09-19", submitted_via="battery",
               engine_version="fc-112-wheel-roll-reach",
               engine_identity="e69ba313", deduplicated_to=None,
               spec_json=json.dumps({"strategy": "wheel"}))
    row.update(kw)
    return row


def raw_cell(run_id: str, arm: str, symbol: str, split: str = "fit",
             **kw: Any) -> Dict[str, Any]:
    """A raw `scenario_runs` row, with the PER-SPLIT window_end the table
    really stores — a fit cell carries `holdout_start - 1`, not the Saturday."""
    built = cell(arm, symbol, split, **kw)
    row = {c: getattr(built, c) for c in CELL_COLUMNS}
    row["roll_skips"] = json.dumps(built.roll_skips) if built.roll_skips else None
    row["run_id"] = run_id
    row["window_end"] = "2026-06-20" if split == "fit" else "2026-09-19"
    return row


def _since_params(**kw: Any):
    return READ._params("2026-01-01", **kw)


DATASET = "gen-lang-client-0607444019.options_wheel"


class TestT12TheReadShape:
    def test_a_the_latest_status_row_per_run_wins(self):
        """One submission is a SEQUENCE of rows, not one row edited in place."""
        client = FakeBigQuery(
            [sweep_row("r1", "submitted", "2026-09-19T08:00:00Z"),
             sweep_row("r1", "running", "2026-09-19T08:05:00Z"),
             sweep_row("r1", "done", "2026-09-19T08:30:00Z")],
            [raw_cell("r1", "base", "AAPL")])
        sweeps = READ.fetch_sweeps(client, DATASET, READ.SELECT_RUN_IDS,
                                   _since_params(run_ids=["r1"]))
        assert len(sweeps) == 1
        assert sweeps[0]["resolved_status"] == "done"
        cells = READ.fetch_cells(client, DATASET, READ.SELECT_RUN_IDS,
                                 _since_params(run_ids=["r1"]))
        assert [c.symbol for c in cells] == ["AAPL"]

    def test_b_done_outranks_failed_on_a_shared_timestamp(self):
        """The realistic collision: the API writes `failed` because `jobs.run`
        errored, while an execution that in fact started writes `done`. A sweep
        whose cells are in the table is done, whatever a launch-side timeout
        thought — so STATUS_RANK breaks the tie towards `done`."""
        client = FakeBigQuery(
            [sweep_row("r1", "failed", "2026-09-19T08:30:00Z"),
             sweep_row("r1", "done", "2026-09-19T08:30:00Z")],
            [raw_cell("r1", "base", "AAPL")])
        sweeps = READ.fetch_sweeps(client, DATASET, READ.SELECT_RUN_IDS,
                                   _since_params(run_ids=["r1"]))
        assert sweeps[0]["resolved_status"] == "done"

    def test_c_a_deduplicated_pin_point_reads_its_target_under_the_pin(self):
        """A one-shot submitted at the coming Saturday's window dedups the
        pin's FIRST point: the cell key carries no `pin_id` and no
        `submitted_via`. The pin's own `run_id` then has NO cell rows."""
        client = FakeBigQuery(
            [sweep_row("oneshot", "done", "2026-09-17T10:00:00Z",
                       submitted_via="sim-service"),
             sweep_row("pinrun", "deduplicated", "2026-09-19T08:30:00Z",
                       pin_id="pin_decision", deduplicated_to="oneshot")],
            [raw_cell("oneshot", "base", "AAPL")])
        sweeps = READ.fetch_sweeps(client, DATASET, READ.SELECT_PINS,
                                   _since_params(pin_ids=["pin_decision"]))
        mapping, voids = READ.resolve_dedup(sweeps)
        assert mapping["pinrun"] == "oneshot"
        assert voids == []
        cells = READ.fetch_cells(client, DATASET, READ.SELECT_PINS,
                                 _since_params(pin_ids=["pin_decision"]))
        assert [(c.pin_id, c.symbol) for c in cells] == [
            ("pin_decision", "AAPL")]

    def test_c_a_dedup_target_that_is_not_done_is_a_void_from_resolve_dedup(
            self):
        client = FakeBigQuery(
            [sweep_row("oneshot", "failed", "2026-09-17T10:00:00Z"),
             sweep_row("pinrun", "deduplicated", "2026-09-19T08:30:00Z",
                       pin_id="pin_decision", deduplicated_to="oneshot")],
            [raw_cell("oneshot", "base", "AAPL")])
        sweeps = READ.fetch_sweeps(client, DATASET, READ.SELECT_PINS,
                                   _since_params(pin_ids=["pin_decision"]))
        _mapping, voids = READ.resolve_dedup(sweeps)
        assert voids and "dedup_target_not_done" in voids[0]
        # The query alone would have dropped it silently — a read six symbols
        # wide that thinks it is seven.
        assert READ.fetch_cells(client, DATASET, READ.SELECT_PINS,
                                _since_params(pin_ids=["pin_decision"])) == []

    def test_d_googl_is_counted_once_per_selection_not_twice(self):
        """The 09-11 battery holds GOOGL TWICE — the standing item and the
        proof pin. Any per-window aggregate that forgets to partition on
        `pin_id` double-counts it."""
        client = FakeBigQuery(
            [sweep_row("standing", "done", "2026-09-19T08:30:00Z"),
             sweep_row("pinrun", "done", "2026-09-19T08:31:00Z",
                       pin_id="pin_decision")],
            [raw_cell("standing", "base", "GOOGL"),
             raw_cell("pinrun", "base", "GOOGL")])
        pins = READ.fetch_cells(client, DATASET, READ.SELECT_PINS,
                                _since_params(pin_ids=["pin_decision"]))
        standing = READ.fetch_cells(client, DATASET, READ.SELECT_STANDING,
                                    _since_params())
        assert [c.pin_id for c in pins] == ["pin_decision"]
        assert [c.pin_id for c in standing] == [None]
        assert len(pins) == len(standing) == 1

    def test_e_the_window_filter_is_on_the_sweep_row_not_the_runs_row(self):
        """`scenario_runs.window_end` is PER SPLIT: a fit cell carries
        `holdout_start - 1`. Filtering the RUNS row on the Saturday date
        returns the holdout cells only and silently drops every cell the
        decision is made on. It did, while DD-6 was being verified."""
        sql = READ._cell_sql(DATASET, READ.SELECT_PINS.format(a="s")
                             + READ.WINDOW_CLAUSE.format(a="s"))
        assert "s.window_end = @window_end" in sql
        assert "r.window_end" not in sql
        client = FakeBigQuery(
            [sweep_row("pinrun", "done", "2026-09-19T08:30:00Z",
                       pin_id="pin_decision")],
            [raw_cell("pinrun", "base", "AAPL", "fit"),
             raw_cell("pinrun", "base", "AAPL", "holdout")])
        cells = READ.fetch_cells(
            client, DATASET,
            READ.SELECT_PINS + READ.WINDOW_CLAUSE,
            _since_params(pin_ids=["pin_decision"],
                          window_end="2026-09-19"))
        assert sorted(c.split for c in cells) == ["fit", "holdout"]
        # ...and the window the rows report is the SWEEP's, not the split's.
        assert {c.window_end for c in cells} == {"2026-09-19"}

    def test_f_insufficient_cells_leave_the_deltas_but_stay_in_the_economics(
            self):
        """`insufficient` means the window contained no completed cycle.
        Rendering it as 0 % makes 'nothing happened' look like a measured flat
        result — but its ROLLS are real and belong in the record."""
        cells = cells_for(KEEP_DELTAS)
        cells = [c for c in cells if not (c.scenario_name in ("base", "t100")
                                          and c.symbol == "UNH"
                                          and c.split == "fit")]
        cells.append(cell("base", "UNH", "fit", measured=False,
                          verdict="insufficient", annualized_return=None))
        cells.append(cell("t100", "UNH", "fit", measured=False,
                          verdict="insufficient", annualized_return=None))
        analysis = READ.analyse(cells)
        assert analysis.reads["limit"].m == 6
        assert "UNH" not in dict(analysis.reads["limit"].deltas)
        economics = "\n".join(READ.render_economics(cells))
        assert "| base | fit | UNH | insufficient |" in economics

    def test_g_a_spec_with_no_strategy_key_is_a_wheel_run(self):
        """`identity.canonical_spec` omits the wheel case, so an ABSENT key
        means wheel. A bare equality would drop every wheel row ever written."""
        client = FakeBigQuery(
            [sweep_row("r1", "done", "2026-09-19T08:30:00Z",
                       spec_json=json.dumps({"symbols": ["AAPL"]})),
             sweep_row("cc", "done", "2026-09-19T08:31:00Z",
                       spec_json=json.dumps({"strategy": "covered_call"}))],
            [raw_cell("r1", "base", "AAPL"), raw_cell("cc", "base", "AAPL")])
        cells = READ.fetch_cells(client, DATASET, READ.SELECT_RUN_IDS,
                                 _since_params(run_ids=["r1", "cc"]))
        assert [c.run_id for c in cells] == ["r1"]
        assert ("IFNULL(JSON_VALUE(l.spec_json, '$.strategy'), 'wheel') "
                "= 'wheel'") in READ._cell_sql(DATASET, "TRUE")

    def test_the_sql_carries_the_canonical_latest_status_clause_byte_for_byte(
            self):
        """Pinned against `persist.LATEST_STATUS_ORDER_BY` rather than copied
        and hoped for: two sides disagreeing about which row is current is how
        a finished sweep reads as still running."""
        from src.backtesting.scenarios import persist

        assert READ.LATEST_STATUS_ORDER_BY == persist.LATEST_STATUS_ORDER_BY
        assert READ.LATEST_STATUS_ORDER_BY.startswith("written_at DESC")
        assert "submitted_at" not in READ.LATEST_STATUS_ORDER_BY
        for sql in (READ._cell_sql(DATASET, "TRUE"),
                    READ._sweep_sql(DATASET, "TRUE")):
            assert persist.LATEST_STATUS_ORDER_BY in sql

    def test_the_cell_query_reads_only_done_sweeps(self):
        assert "WHERE s.status = 'done'" in READ._cell_sql(DATASET, "TRUE")


# --------------------------------------------------------------------------- #
# T-13 — the holdout column is the engine's own, re-expressed over BQ rows.
# --------------------------------------------------------------------------- #
class TestT13HoldoutParity:
    def test_the_tool_reproduces_report_sign_agreement_exactly(self):
        """Two implementations of 'does the holdout agree?' that disagree is
        how a record quotes a number the console never showed."""
        from src.backtesting.scenarios.report import sign_agreement
        from src.backtesting.scenarios.runner import ScenarioResult, SweepResult

        # AAA and BBB agree (t100 worse in both windows); CCC flips (worse in
        # fit, better in the holdout); DDD never completed a cycle in the
        # holdout and is comparable in NEITHER numerator nor denominator.
        plan = {
            "AAA": (0.10, 0.08, 0.10, 0.08),
            "BBB": (0.10, 0.07, 0.12, 0.09),
            "CCC": (0.10, 0.08, 0.10, 0.12),
            "DDD": (0.10, 0.08, 0.10, None),
        }
        rows, cells = [], []
        for symbol, (bf, tf, bh, th) in plan.items():
            for scenario, fit, hold in (("base", bf, bh), ("t100", tf, th)):
                for split, ann in (("fit", fit), ("holdout", hold)):
                    verdict = "marginal" if ann is not None else "insufficient"
                    rows.append(ScenarioResult(
                        scenario=scenario, symbol=symbol,
                        start=date(2025, 9, 11), end=date(2026, 6, 12),
                        split=split, config_hash="deadbeefdeadbeef",
                        verdict=verdict, annualized_return=ann,
                        decision_days=200, demote=False))
                    cells.append(cell(scenario, symbol, split,
                                      annualized_return=ann,
                                      verdict=verdict,
                                      measured=ann is not None))
        result = SweepResult(
            rows=rows, scenarios=["base", "t100"], symbols=list(plan),
            windows=[("fit", date(2025, 9, 11), date(2026, 6, 12)),
                     ("holdout", date(2026, 6, 13), date(2026, 9, 11))],
            base_config_hash="basehash00000000",
            scenario_config_hashes={"base": "h1", "t100": "h2"},
            scenario_hashes={"base": "a1", "t100": "a2"},
            scenario_fill_haircuts={"base": None, "t100": None},
            scenario_overrides={"base": {},
                                "t100": {"rolling.itm_trigger_ratio": 1.0}},
            materialise_seconds={}, replay_seconds={}, wall_seconds=1.0,
            starting_cash=READ.STARTING_CASH)

        expected = sign_agreement(result, "t100")
        line = READ.sign_agreement_rows(cells, "base", "t100")
        assert (line.agreeing, line.comparable) == expected == (2, 3)
        assert line.informative is False  # 3 < 4: uninformative, and it says so


# --------------------------------------------------------------------------- #
# T-14 — the verdict block's provenance, and the flags that do not exist.
# --------------------------------------------------------------------------- #
class TestT14VerdictBlockProvenance:
    def test_the_block_prints_the_three_constants_and_a_40_hex_sha(self):
        block = "\n".join(READ.render_provenance("a" * 40))
        assert "TIE_BREAK       : move" in block
        assert "MIN_EFFECT_PP   : 0.5" in block
        assert "MIN_SIGN_COUNT  : {6: 5, 7: 6}" in block
        assert re.search(r"\b[0-9a-f]{40}\b", block)

    def test_the_real_commit_sha_reaches_the_block(self):
        sha = READ.tool_commit_sha()
        if sha == READ.SHA_UNAVAILABLE:
            pytest.skip("no git checkout to read a SHA from")
        assert re.fullmatch(r"[0-9a-f]{40}", sha)
        assert sha in "\n".join(READ.render_provenance())

    def test_the_full_report_carries_the_rule_and_the_verdict(self):
        analysis = READ.analyse(cells_for(KEEP_DELTAS))
        report = READ.render_report(analysis, ["fixture"], sha="b" * 40)
        assert "b" * 40 in report
        assert "MIN_SIGN_COUNT  : {6: 5, 7: 6}" in report
        assert report.rstrip().endswith("=" * 78)
        assert "VERDICT: KEEP(->0.98)" in report

    def test_tie_break_is_not_an_accepted_argument(self):
        """The whole point of the pre-registration: a rule that can be passed
        in is a rule that can be chosen after the numbers are seen."""
        parser = READ.build_parser()
        for flag in ("--tie-break", "--min-effect-pp", "--min-sign-count",
                     "--keep-rule"):
            with pytest.raises(SystemExit) as excinfo:
                parser.parse_args(["--run-id", "r1", flag, "keep"])
            assert excinfo.value.code == 2

    def test_no_flag_may_ever_name_a_constant_of_the_rule(self):
        """`build_parser` asserts this itself, so a later flag fails loudly and
        locally instead of being noticed in a diff."""
        parser = READ.build_parser()
        options = [o for a in parser._actions for o in a.option_strings]
        for option in options:
            for banned in READ.FORBIDDEN_FLAG_SUBSTRINGS:
                assert banned not in option, (option, banned)
        assert "--pin-id" in options and "--artifacts" in options

    def test_a_void_exits_non_zero_and_a_verdict_exits_zero(self):
        assert READ.Verdict("VOID", "", ("x",)).is_void
        assert not READ.Verdict("MIXED-NULL", "1.00").is_void


# --------------------------------------------------------------------------- #
# The artifact-only reported set (DD-1, DD-3, DD-2's tell). Opt-in: a missing
# object degrades these tables, never the verdict.
# --------------------------------------------------------------------------- #
def _artifact(roll_records, ledger):
    return {"roll_records": roll_records, "ledger": ledger}


def _leg(day, underlying, kind, fill_rule, symbol=""):
    return {"event_date": day, "underlying": underlying, "kind": kind,
            "symbol": symbol or underlying,
            "detail": {"fill_rule": fill_rule}}


class TestTheArtifactTables:
    def test_resting_legs_are_split_by_roll_kind(self):
        """DD-3's pre-registered prediction rests on this split: the imminence
        residual sits on ITM DEFENCE, which both arms execute, while the
        roll-outs unique to 0.98 are marketable."""
        art = _artifact(
            [{"day": "2026-03-02", "underlying": "AAPL", "success": True,
              "roll_kind": "itm_defence"},
             {"day": "2026-03-09", "underlying": "AAPL", "success": True,
              "roll_kind": "otm_roll_out"}],
            [_leg("2026-03-02", "AAPL", "buy_to_close", "limit_resting"),
             _leg("2026-03-02", "AAPL", "sell_call_open", "limit_resting"),
             _leg("2026-03-09", "AAPL", "buy_to_close", "limit_marketable"),
             _leg("2026-03-09", "AAPL", "sell_call_open", "limit_marketable"),
             # A non-roll entry on an unrelated day must not be counted.
             _leg("2026-04-01", "AAPL", "sell_call_open", "limit_resting")])
        assert READ.resting_by_kind(art) == {"itm_defence": (2, 2),
                                             "otm_roll_out": (0, 2)}

    def test_a_replacement_at_the_ladders_edge_is_counted_separately(self):
        """DD-2's cheap tell. `unknown` is its own bucket: 'we could not tell'
        and 'it chose freely' are opposite findings."""
        art = _artifact(
            [{"day": "2026-03-02", "underlying": "AAPL", "success": True,
              "roll_kind": "otm_roll_out"},
             {"day": "2026-03-09", "underlying": "AAPL", "success": True,
              "roll_kind": "otm_roll_out"},
             {"day": "2026-03-16", "underlying": "AAPL", "success": True,
              "roll_kind": "otm_roll_out"}],
            [_leg("2026-03-02", "AAPL", "sell_call_open", "limit_marketable",
                  "AAPL260323C00230000"),     # 21 DTE — the cutoff
             _leg("2026-03-09", "AAPL", "sell_call_open", "limit_marketable",
                  "AAPL260316C00230000")])    # 7 DTE — chose freely
        assert READ.replacement_dte_tell(art) == {
            "at_cutoff": 1, "below_cutoff": 1, "unknown": 1}

    def test_an_occ_symbol_yields_its_expiry(self):
        assert READ.occ_expiry("AAPL260918C00230000") == date(2026, 9, 18)
        assert READ.occ_expiry("") is None
        assert READ.occ_expiry("NOTANOCC") is None

    def test_the_paired_event_table_reports_what_the_un_rolled_call_did(self):
        """The table is what the verdict MEANS in trades: each day 0.98 rolled
        out, and what the same call did on the arm that could not."""
        base = _artifact(
            [{"day": "2026-03-09", "underlying": "AAPL", "success": True,
              "roll_kind": "otm_roll_out", "net_credit": 87.0,
              "old_strike": 230.0, "new_strike": 235.0, "itm_ratio": 0.987},
             {"day": "2026-04-06", "underlying": "AAPL", "success": True,
              "roll_kind": "itm_defence", "net_credit": 12.0}],
            [])
        t100 = _artifact([], [
            _leg("2026-03-02", "AAPL", "sell_call_open", "limit_marketable",
                 "AAPL260320C00230000"),
            _leg("2026-03-20", "AAPL", "call_assignment", "haircut",
                 "AAPL260320C00230000")])
        rows = READ.paired_events(base, t100)
        assert len(rows) == 1, "only the OTM roll-outs pair"
        assert rows[0]["day"] == "2026-03-09"
        assert rows[0]["net_credit"] == 87.0
        assert rows[0]["t100_outcome"] == "call_assignment"
        assert rows[0]["t100_outcome_day"] == "2026-03-20"
        rendered = "\n".join(READ.render_artifacts(
            {"paired_events": rows, "resting_by_kind": {}, "missing": []}))
        assert "230.00 -> 235.00" in rendered
        assert "call_assignment 2026-03-20" in rendered
