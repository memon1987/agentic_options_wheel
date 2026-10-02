"""Screen mode (FC-032 Phase 5).

Screen output feeds a decision about which symbols keep real capital, so these
tests are about the ways a screening run can quietly mislead: a symbol that
errored looking like a pass, a partial run looking complete, or a demotion
recommendation reading as an action already taken.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from src.backtesting.engine.rejections import HOLDS_UNDERLYING_REASON
from src.backtesting.reporting.bq_writer import build_row, config_hash
from src.backtesting.screen import (
    ScreenResult,
    SymbolResult,
    render_screen_summary,
    run_screen,
)


class _FakeReport:
    """Minimal stand-in for FitnessReport."""

    def __init__(self, symbol="XYZ", verdict="fit", total_return=0.05):
        self.symbol = symbol
        self._verdict = verdict
        self.total_return = total_return
        self.start, self.end = date(2025, 1, 1), date(2025, 12, 31)
        self.starting_cash, self.final_equity = 100_000.0, 105_000.0
        self.annualized_return = 0.05
        self.annualized_return_on_collateral = 0.12
        self.excess_return = 0.01
        self.option_pnl, self.stock_pnl, self.unrealized_stock_pnl = 1000.0, 0.0, 0.0
        self.dividends = 0.0
        self.option_pnl_share, self.reconciliation_gap = 1.0, 0.0
        self.decision_days, self.days_in_position = 250, 200
        self.days_in_position_fraction = 0.8
        self.cycles, self.puts_sold, self.calls_sold = [], 12, 3
        self.win_rate, self.assignment_rate = 0.9, 0.1
        self.max_drawdown, self.days_underwater, self.avg_collateral = -0.03, 4, 9000.0
        self.benchmark = None
        self.data_quality = {"blocked_days_by_reason": {"gap-risk filter (stage 2)": 20}}

    @property
    def closed_cycles(self):
        return []

    def verdict(self):
        return self._verdict

    def verdict_reasons(self):
        return ["OK: profitable"] if self._verdict == "fit" else ["BLOCK: lost money"]


# --------------------------------------------------------------------------- #
class TestFailuresAreNotPasses:
    def test_a_failed_symbol_still_produces_a_row(self):
        """Dropping it makes the run look like it screened fewer symbols."""
        row = build_row(run_id="r1", symbol="BAD", report=None, sensitivity=None,
                        cfg_hash="abc", engine_version="v", error="boom")
        assert row["symbol"] == "BAD"
        assert row["verdict"] is None
        assert row["demote"] is None
        assert "boom" in row["error"]

    def test_a_failed_symbol_is_not_a_demotion_candidate(self):
        r = ScreenResult(run_id="r", start=date(2025, 1, 1), end=date(2025, 12, 31))
        r.results = [SymbolResult("BAD", error="boom"),
                     SymbolResult("UGLY", _FakeReport(verdict="unfit"))]
        assert r.demote_candidates == ["UGLY"]
        assert r.failures == ["BAD"]

    def test_summary_says_failures_were_never_checked(self):
        r = ScreenResult(run_id="r", start=date(2025, 1, 1), end=date(2025, 12, 31))
        r.results = [SymbolResult("BAD", error="boom")]
        md = render_screen_summary(r)
        assert "never checked" in md
        assert "not** implicitly fine" in md

    def test_corporate_action_is_a_skip_not_a_verdict(self):
        """A split in the window says nothing about the symbol's fitness."""
        from src.backtesting.data.alpaca_provider import UnadjustedCorporateAction

        with patch("src.backtesting.screen.evaluate_symbol",
                   side_effect=UnadjustedCorporateAction("NVDA moved 0.101x")):
            result = run_screen(symbols=["NVDA"], persist=False,
                                run_sensitivity=False,
                                start=date(2025, 1, 1), end=date(2025, 6, 30))
        assert result.demote_candidates == []
        assert result.failures == ["NVDA"]
        assert "corporate_action" in result.results[0].error


class TestPartialRunsAreVisible:
    def test_unpersisted_run_is_flagged_in_the_summary(self):
        r = ScreenResult(run_id="r", start=date(2025, 1, 1), end=date(2025, 12, 31))
        r.results = [SymbolResult("XYZ", _FakeReport())]
        r.persisted = False
        md = render_screen_summary(r)
        assert "persisted: **NO**" in md
        assert "were not persisted to BigQuery" in md
        assert "only record of the run" in md

    def test_one_bad_symbol_does_not_kill_the_run(self):
        def _side_effect(symbol, *a, **kw):
            if symbol == "BAD":
                raise RuntimeError("kaboom")
            return _FakeReport(symbol), None

        with patch("src.backtesting.screen.evaluate_symbol", side_effect=_side_effect):
            result = run_screen(symbols=["AAA", "BAD", "CCC"], persist=False,
                                run_sensitivity=False,
                                start=date(2025, 1, 1), end=date(2025, 6, 30))
        assert len(result.results) == 3
        assert result.failures == ["BAD"]
        assert result.tally()["fit"] == 2


class TestDemotionIsARecommendation:
    def test_summary_states_no_action_was_taken(self):
        r = ScreenResult(run_id="r", start=date(2025, 1, 1), end=date(2025, 12, 31))
        r.results = [SymbolResult("UGLY", _FakeReport(verdict="unfit"))]
        md = render_screen_summary(r)
        assert "recommendation for a human" in md
        assert "not an action" in md

    def test_demote_flag_tracks_the_unfit_verdict_only(self):
        for verdict, expected in [("fit", False), ("marginal", False), ("unfit", True)]:
            row = build_row(run_id="r", symbol="X", report=_FakeReport(verdict=verdict),
                            sensitivity=None, cfg_hash="h", engine_version="v")
            assert row["demote"] is expected, verdict


class TestProvenance:
    def test_config_hash_changes_when_a_threshold_changes(self):
        """A verdict is uninterpretable without the config that produced it."""
        class _Cfg:
            put_target_dte = 7
            put_delta_range = [0.10, 0.20]
            min_put_premium = 0.50

        a = _Cfg()
        h1 = config_hash(a)
        a.min_put_premium = 0.25
        h2 = config_hash(a)
        assert h1 != h2
        assert len(h1) == 16

    def test_config_hash_is_stable_for_identical_config(self):
        class _Cfg:
            put_target_dte = 7
            min_put_premium = 0.50

        assert config_hash(_Cfg()) == config_hash(_Cfg())

    def test_row_carries_known_biases(self):
        row = build_row(run_id="r", symbol="X", report=_FakeReport(),
                        sensitivity=None, cfg_hash="h", engine_version="v")
        assert row["known_biases"]
        assert any("Dividends" in b for b in row["known_biases"])

    def test_row_carries_the_binding_constraint(self):
        row = build_row(run_id="r", symbol="X", report=_FakeReport(),
                        sensitivity=None, cfg_hash="h", engine_version="v")
        assert row["binding_constraint"] == "gap-risk filter (stage 2)"

    def test_deployment_never_becomes_the_binding_constraint(self):
        """FC-069 item 12, review fix 1 — the honest-metric guard.

        "already holds this underlying (scan, put)" fires on nearly every day
        of a *healthy* deployment (a golden-path fixture puts it at 40 of 45),
        so it dominates `blocked_days_by_reason` by count and would otherwise
        be stamped as the binding constraint on almost every row. A demotion
        read off that column would be reading "the wheel held positions" as
        "the wheel could not trade" — the FC-057 dishonest-metric class,
        reintroduced from the other side.

        MUTATION CHECK: drop the `stand_down_reasons` filter from
        `_binding()` and this test fails with
        `'already holds this underlying (scan, put)' != 'no put cleared ...'`.
        """
        report = _FakeReport()
        report.data_quality = {"blocked_days_by_reason": {
            HOLDS_UNDERLYING_REASON: 40,
            "no put cleared delta/DTE/premium (stage 7)": 5,
        }}

        row = build_row(run_id="r", symbol="X", report=report,
                        sensitivity=None, cfg_hash="h", engine_version="v")

        assert row["binding_constraint"] == (
            "no put cleared delta/DTE/premium (stage 7)")

    def test_a_run_blocked_only_by_deployment_has_no_binding_constraint(self):
        """None is the honest answer — the label would say the strategy was
        blocked when it was invested."""
        report = _FakeReport()
        report.data_quality = {
            "blocked_days_by_reason": {HOLDS_UNDERLYING_REASON: 40}}

        row = build_row(run_id="r", symbol="X", report=report,
                        sensitivity=None, cfg_hash="h", engine_version="v")

        assert row["binding_constraint"] is None

    def test_fill_sensitivity_flip_is_recorded(self):
        row = build_row(run_id="r", symbol="X", report=_FakeReport(),
                        sensitivity={"bid_return": 0.01, "verdict_flips": True},
                        cfg_hash="h", engine_version="v")
        assert row["verdict_flips_on_fill"] is True
        assert row["bid_fill_return"] == 0.01


class TestWindow:
    def test_window_is_clamped_to_alpacas_history_floor(self):
        from src.backtesting.data.alpaca_provider import ALPACA_OPTIONS_HISTORY_START

        with patch("src.backtesting.screen.evaluate_symbol",
                   return_value=(_FakeReport(), None)):
            result = run_screen(symbols=["XYZ"], persist=False, run_sensitivity=False,
                                start=date(2020, 1, 1), end=date(2025, 6, 30))
        assert result.start == ALPACA_OPTIONS_HISTORY_START


class TestSchemaContract:
    """The schema and build_row must not drift apart.

    insert_rows_json rejects the WHOLE request when any row carries an unknown
    field — not just the offending row — so a single drifted key means a run
    writes zero rows. This is the cheapest possible guard against that.
    """

    def _schema_names(self):
        pytest.importorskip("google.cloud.bigquery")
        from src.backtesting.reporting.bq_writer import _schema
        return {f.name for f in _schema()}

    def test_success_row_keys_all_exist_in_the_schema(self):
        names = self._schema_names()
        row = build_row(run_id="r", symbol="X", report=_FakeReport(),
                        sensitivity={"bid_return": 0.01, "verdict_flips": False},
                        cfg_hash="h", engine_version="v")
        assert set(row) <= names, f"row keys not in schema: {set(row) - names}"

    def test_failure_row_keys_all_exist_in_the_schema(self):
        names = self._schema_names()
        row = build_row(run_id="r", symbol="X", report=None, sensitivity=None,
                        cfg_hash="h", engine_version="v", error="boom",
                        window_start=date(2025, 1, 1), window_end=date(2025, 6, 30))
        assert set(row) <= names, f"row keys not in schema: {set(row) - names}"

    def test_failure_row_keeps_its_window(self):
        """A window-scoped query would otherwise drop the never-checked symbols."""
        row = build_row(run_id="r", symbol="X", report=None, sensitivity=None,
                        cfg_hash="h", engine_version="v", error="boom",
                        window_start=date(2025, 1, 1), window_end=date(2025, 6, 30))
        assert row["window_start"] == "2025-01-01"
        assert row["window_end"] == "2025-06-30"


class TestRunScope:
    """An ad-hoc subset must never be readable as the state of the universe."""

    def test_full_universe_run_is_marked_full(self):
        from unittest.mock import patch as _p

        from src.utils.config import Config
        cfg = Config()
        with _p("src.backtesting.screen.evaluate_symbol",
                return_value=(_FakeReport(), None)):
            r = run_screen(config=cfg, persist=False, run_sensitivity=False,
                           start=date(2025, 1, 1), end=date(2025, 6, 30))
        assert r.run_kind == "full"
        assert r.universe_size == len(cfg.stock_symbols)

    def test_subset_run_is_marked_adhoc(self):
        from unittest.mock import patch as _p

        with _p("src.backtesting.screen.evaluate_symbol",
                return_value=(_FakeReport(), None)):
            r = run_screen(symbols=["NVDA"], persist=False, run_sensitivity=False,
                           start=date(2025, 1, 1), end=date(2025, 6, 30))
        assert r.run_kind == "adhoc"
        assert r.universe_size == 1

    def test_adhoc_scope_is_stated_in_the_summary(self):
        r = ScreenResult(run_id="r", start=date(2025, 1, 1), end=date(2025, 12, 31),
                         run_kind="adhoc", universe_size=1)
        r.results = [SymbolResult("NVDA", _FakeReport())]
        md = render_screen_summary(r)
        assert "ad-hoc subset" in md
        assert "not a picture of the whole universe" in md

    def test_row_carries_the_scope(self):
        row = build_row(run_id="r", symbol="X", report=_FakeReport(),
                        sensitivity=None, cfg_hash="h", engine_version="v",
                        run_kind="adhoc", universe_size=2)
        assert row["run_kind"] == "adhoc"
        assert row["universe_size"] == 2


class TestConfigHashCoversScoring:
    """A threshold change must not be indistinguishable from a symbol change."""

    def test_hash_moves_when_a_scoring_constant_moves(self, monkeypatch):
        from src.utils.config import Config
        from src.backtesting.metrics import fitness as fit

        cfg = Config()
        before = config_hash(cfg)
        monkeypatch.setattr(fit, "RISK_FREE_RATE", 0.045)
        after = config_hash(cfg)
        assert before != after, (
            "changing the risk-free floor flips verdicts but left the hash "
            "identical — a threshold change would read as a symbol change"
        )

    def test_hash_moves_when_the_fill_assumption_moves(self, monkeypatch):
        from src.utils.config import Config
        from src.backtesting import evaluate as ev

        cfg = Config()
        before = config_hash(cfg)
        monkeypatch.setattr(ev, "DEFAULT_FILL_HAIRCUT", 0.5)
        after = config_hash(cfg)
        assert before != after


class TestDemotionArtifactIsHonest:
    """Everything in the table that justifies a demotion must be real."""

    def test_absent_benchmark_never_renders_as_zero(self):
        """'+0.00%' reads as 'exactly matched buy-and-hold' — fabricated evidence."""
        rep = _FakeReport(verdict="unfit")
        rep.excess_return = None
        r = ScreenResult(run_id="r", start=date(2025, 1, 1), end=date(2025, 12, 31))
        r.results = [SymbolResult("XYZ", rep)]
        md = render_screen_summary(r)
        assert "+0.00%" not in md
        assert "| — |" in md

    def test_insufficient_evidence_is_not_a_demotion_candidate(self):
        rep = _FakeReport(verdict="insufficient")
        r = ScreenResult(run_id="r", start=date(2025, 1, 1), end=date(2025, 12, 31))
        r.results = [SymbolResult("XYZ", rep)]
        assert r.demote_candidates == []

    def test_insufficient_row_is_not_flagged_for_demotion(self):
        row = build_row(run_id="r", symbol="X",
                        report=_FakeReport(verdict="insufficient"),
                        sensitivity=None, cfg_hash="h", engine_version="v")
        assert row["demote"] is False
        assert row["verdict"] == "insufficient"

    def test_a_verdict_that_flips_on_fill_is_not_a_demotion(self):
        """The docs say a flipping verdict 'is not a verdict' — so don't act on it."""
        row = build_row(run_id="r", symbol="X", report=_FakeReport(verdict="unfit"),
                        sensitivity={"bid_return": 0.01, "verdict_flips": True},
                        cfg_hash="h", engine_version="v")
        assert row["verdict"] == "unfit"
        assert row["verdict_flips_on_fill"] is True
        assert row["demote"] is False, (
            "a verdict the engine itself calls unstable must not drive capital"
        )

    def test_a_stable_unfit_verdict_still_demotes(self):
        row = build_row(run_id="r", symbol="X", report=_FakeReport(verdict="unfit"),
                        sensitivity={"bid_return": 0.01, "verdict_flips": False},
                        cfg_hash="h", engine_version="v")
        assert row["demote"] is True


class TestAnalyticsIsolationIsThreadLocal:
    """A replay must never redirect a concurrent LIVE cycle's telemetry.

    cloud_run_server runs Flask threaded=True at containerConcurrency 10 with
    maxScale 1 — one process, ten concurrent requests. A process-global swap
    discarded live analytics rows, including the error that warns of
    re-execution risk.
    """

    def test_a_concurrent_thread_never_sees_the_replay_writer(self):
        import threading
        import time

        from src.backtesting.engine.no_op_analytics import NoOpAnalyticsWriter
        from src.data import analytics_writer as aw

        seen, stop = [], threading.Event()

        def live():
            while not stop.is_set():
                seen.append(type(aw.get_analytics_writer()).__name__)
                time.sleep(0.005)

        t = threading.Thread(target=live)
        t.start()
        try:
            time.sleep(0.03)
            prev = aw.set_analytics_writer(NoOpAnalyticsWriter())
            time.sleep(0.05)
            aw.set_analytics_writer(prev)
            time.sleep(0.02)
        finally:
            stop.set()
            t.join()

        assert seen, "probe thread never ran"
        assert "NoOpAnalyticsWriter" not in seen, (
            "a replay redirected a concurrent thread's analytics"
        )
