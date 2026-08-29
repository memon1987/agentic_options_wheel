"""Uncovered-position alerting tests (FC-030, repointed by FC-065 Phase 4).

Covers the threshold boundary, the single-line digest format (the marker a
Cloud Monitoring log-based policy matches on), the degraded path — a
live-proxy outage must NOT read as "nothing is uncovered" — and the new
"uncovered_days could not be derived" state, which must not read as covered
either.

The alert's *data source* moved from an OPASN-strike price inference to the
bot's own decision records; its *marker strings and deployment surface* did
not. Both halves are pinned here.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# See tests/_dashboard_path.py: the backend is APPENDED and the repo root
# kept ahead of it, so `import main` still resolves to the CLI rather than
# to dashboard/backend/main.py for every module collected after this one.
from tests._dashboard_path import add_dashboard_backend_to_path  # noqa: E402

add_dashboard_backend_to_path()

from services.pause_alert import (  # noqa: E402
    ALERT_MARKER,
    CHECK_FAILED_MARKER,
    format_uncovered_alert,
    select_alertable_uncovered,
)


def uncovered(symbol, days, underwater=-0.06, floor=250.0):
    return {
        "symbol": symbol,
        "shares": 100.0,
        "cost_basis_per_share": floor,
        "last_price": floor * (1 + underwater),
        "underwater_pct": underwater,
        "uncovered_days": days,
        "outcome": "no_candidates",
        "reason": "no_qualifying_strikes",
        "run_id": "20260801T140000Z-abcdef12",
        "last_decision_at": "2026-08-01T14:00:00+00:00",
    }


class TestSelectAlertableUncovered:
    def test_below_threshold_is_silent(self):
        # The whole point: a 1-day gap is normal operation, not an alert.
        assert select_alertable_uncovered(
            [uncovered("AMZN", 1), uncovered("GOOGL", 6)], 7) == []

    def test_at_threshold_alerts(self):
        # Boundary is inclusive — 7 days means 7 days, not 8.
        out = select_alertable_uncovered([uncovered("AMZN", 7)], 7)
        assert [r["symbol"] for r in out] == ["AMZN"]

    def test_filters_mixed_set_and_sorts_longest_first(self):
        out = select_alertable_uncovered(
            [uncovered("AMZN", 9), uncovered("GOOGL", 3), uncovered("UNH", 21)], 7)
        assert [r["symbol"] for r in out] == ["UNH", "AMZN"]

    def test_empty_list(self):
        assert select_alertable_uncovered([], 7) == []

    def test_missing_day_count_does_not_crash(self):
        # Defensive: a malformed row must not take down the scheduled check.
        assert select_alertable_uncovered([{"symbol": "AMD"}], 7) == []

    def test_undecidable_days_never_alert(self):
        # None is "we could not tell". It must not be coerced into an alert
        # (nor into silence — the endpoint reports it via unknown_total).
        row = uncovered("NVDA", None)
        assert select_alertable_uncovered([row], 7) == []

    def test_threshold_is_configurable(self):
        rows = [uncovered("AMZN", 10)]
        assert select_alertable_uncovered(rows, 14) == []
        assert len(select_alertable_uncovered(rows, 10)) == 1


class TestFormatUncoveredAlert:
    def test_carries_marker_and_is_single_line(self):
        msg = format_uncovered_alert([uncovered("AMZN", 9, -0.0585, 262.5)], 7)
        # Marker is the deployed policy's match string. FC-065 Phase 4 changed
        # the alert's data source, NOT its marker — renaming it would silently
        # disarm the operator's only escalation control for idle capital.
        assert msg.startswith(ALERT_MARKER)
        assert ALERT_MARKER == "DRAWDOWN_PAUSE_ALERT", "policy filter depends on this literal"
        assert CHECK_FAILED_MARKER == "DRAWDOWN_PAUSE_ALERT_CHECK_FAILED"
        assert "\n" not in msg, "must be one line so it yields one email"

    def test_includes_symbol_days_and_depth(self):
        msg = format_uncovered_alert([uncovered("AMZN", 9, -0.0585, 262.5)], 7)
        assert "AMZN" in msg
        assert "9d" in msg
        assert "-5.9%" in msg, "sign is load-bearing: negative means below the floor"
        assert "262.50" in msg

    def test_above_floor_renders_with_a_plus(self):
        # A symbol can be uncovered while ABOVE its floor (no strike met the
        # delta/premium bands). Rendering that as "5.9% below" would be a lie.
        msg = format_uncovered_alert([uncovered("AAPL", 8, 0.021, 303.5)], 7)
        assert "+2.1%" in msg

    def test_missing_depth_is_stated_not_guessed(self):
        row = uncovered("AMZN", 9)
        row["underwater_pct"] = None
        row["cost_basis_per_share"] = None
        msg = format_uncovered_alert([row], 7)
        assert "depth unknown" in msg

    def test_multiple_symbols_in_one_digest(self):
        msg = format_uncovered_alert(
            [uncovered("UNH", 21, -0.08, 300.0), uncovered("AMZN", 9, -0.06, 262.5)], 7)
        assert "2 symbol(s)" in msg
        assert "UNH" in msg and "AMZN" in msg
        assert "\n" not in msg

    def test_threshold_stated_in_message(self):
        assert ">=7 trading days" in format_uncovered_alert([uncovered("AMZN", 9)], 7)
        assert ">=14 trading days" in format_uncovered_alert([uncovered("AMZN", 20)], 14)


class TestUncoveredDecisionsQuery:
    """The reading side of the dedup contract.

    Write-time ``insertId`` dedup is best-effort over a short streaming
    window; a Cloud Scheduler retry minutes later inserts a second copy. If
    the reading query does not dedup, a retried cycle double-counts — which is
    exactly how 36 duplicate ``wheel_cycles`` rows per assignment happened.
    """

    def _sql(self):
        from services.bigquery import uncovered_decisions_sql
        return uncovered_decisions_sql("proj.options_wheel")

    def test_dedups_on_the_write_key(self):
        sql = self._sql()
        assert "PARTITION BY dedup_key" in sql, (
            "reading decision_events without deduping on dedup_key double-counts "
            "a Cloud Scheduler retry")
        assert "QUALIFY ROW_NUMBER()" in sql

    def test_takes_one_row_per_symbol(self):
        assert "PARTITION BY symbol" in self._sql()

    def test_is_bounded_and_parameterised(self):
        sql = self._sql()
        assert "@lookback_days" in sql
        assert "@symbols" in sql
        assert "proj.options_wheel.decision_events" in sql

    def test_a_sold_row_outranks_a_later_row_under_the_same_key(self):
        """A /run retry after a failed mark_executed re-derives the SAME
        run_id (it comes off the blob) and writes
        `dropped/already_positioned` at a later timestamp — the position it
        finds is the one it just opened. Under a pure timestamp ordering that
        row permanently shadows the real `sold` row, here and in every future
        reader (FC-044's grid, the OQ-1 in-gap retro-analysis)."""
        sql = self._sql()
        assert "IF(outcome = 'sold', 0, 1)" in sql, (
            "a write that reached the broker is the more terminal fact and "
            "must win its dedup key regardless of write order")


class TestGetUncoveredSymbols:
    """Hermetic: BigQueryService built via __new__ with _run_query stubbed —
    the established pattern from test_dashboard_reconciliation.py."""

    @staticmethod
    def _service(decision_rows):
        from services.bigquery import BigQueryService

        svc = BigQueryService.__new__(BigQueryService)
        svc.dataset = "test.options_wheel"

        def fake_run_query(query, params=None):
            if "fc018_per_symbol_scorecard" in query:
                return []
            if "decision_events" in query:
                return decision_rows
            raise AssertionError(f"unexpected query: {query[:80]}")

        svc._run_query = fake_run_query
        return svc

    @staticmethod
    def _row(symbol, days, underwater=-0.089, floor=218.43):
        return {
            "symbol": symbol, "run_id": "20260801T140000Z-abcdef12",
            "run_ts": "2026-08-01T14:00:00+00:00", "stage": "scan",
            "outcome": "no_candidates", "reason": "no_qualifying_strikes",
            "shares": 100, "cost_basis_per_share": floor,
            "current_price": floor * (1 + underwater),
            "underwater_pct": underwater, "uncovered_days": days,
        }

    def test_uncovered_rows_are_reported_with_live_shares(self):
        svc = self._service([self._row("NVDA", 9)])
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "NVDA", "qty": 100}], threshold_days=7)
        assert [r["symbol"] for r in out["uncovered"]] == ["NVDA"]
        assert out["uncovered"][0]["shares"] == 100.0
        assert out["uncovered"][0]["uncovered_days"] == 9
        assert out["source"] == "decision_events"

    def test_covered_symbol_is_not_listed(self):
        svc = self._service([self._row("AAPL", 0)])
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "AAPL", "qty": 100}], threshold_days=7)
        assert out["uncovered"] == []

    def test_undecidable_days_go_to_their_own_bucket(self):
        svc = self._service([self._row("GOOGL", None)])
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "GOOGL", "qty": 100}], threshold_days=7)
        assert out["uncovered"] == []
        assert [r["symbol"] for r in out["unknown_uncovered_days"]] == ["GOOGL"]

    def test_records_for_symbols_no_longer_held_are_ignored(self):
        svc = self._service([self._row("NVDA", 9), self._row("TSLA", 30)])
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "NVDA", "qty": 100}], threshold_days=7)
        assert [r["symbol"] for r in out["uncovered"]] == ["NVDA"]

    def test_no_holdings_returns_empty_without_querying(self):
        svc = self._service([self._row("NVDA", 9)])
        out = svc.get_uncovered_symbols(live_positions=[], threshold_days=7)
        assert out["uncovered"] == []
        assert out["threshold_days"] == 7

    def test_sorted_longest_uncovered_first(self):
        svc = self._service([self._row("NVDA", 3), self._row("AMZN", 12)])
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "NVDA", "qty": 100},
                            {"symbol": "AMZN", "qty": 100}], threshold_days=7)
        assert [r["symbol"] for r in out["uncovered"]] == ["AMZN", "NVDA"]


# The endpoint tests import routers.v2, which needs FastAPI — present in
# the dashboard image but NOT in the bot CI image where this suite runs.
# Skip is deliberately CLASS-scoped: a module-level pytest.importorskip
# would abort collection of the whole file and silently skip the pure
# tests above too (verified — it skipped all 14), turning CI green while
# testing nothing.
_HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None


@pytest.mark.skipif(not _HAS_FASTAPI,
                    reason="FastAPI only present in the dashboard image")
class TestPauseAlertCheckEndpoint:
    """Endpoint behavior — especially that a degraded evaluation is never
    silently reported as 'nothing is uncovered'."""

    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_proxy_outage_reports_degraded_not_ok(self, monkeypatch):
        import routers.v2 as v2

        async def fake_eval():
            # get_uncovered_symbols returns an empty list when live positions
            # are unavailable — indistinguishable from "all clear" unless
            # positions_available is honored.
            return {"uncovered": [], "positions_available": False}

        monkeypatch.setattr(v2, "_evaluate_uncovered_symbols", fake_eval)
        out = self._run(v2.pause_alert_check())
        assert out["status"] == "degraded"
        assert "unavailable" in out["reason"]

    def test_evaluation_exception_is_contained(self, monkeypatch):
        import routers.v2 as v2

        async def boom():
            raise RuntimeError("BigQuery exploded")

        monkeypatch.setattr(v2, "_evaluate_uncovered_symbols", boom)
        # Must not raise — the scheduler would otherwise just log a 500 and
        # the failure would be as silent as the thing we are alerting on.
        out = self._run(v2.pause_alert_check())
        assert out["status"] == "degraded"
        assert "BigQuery exploded" in out["reason"]

    def test_healthy_sub_threshold_run_is_quiet(self, monkeypatch):
        import routers.v2 as v2

        async def fake_eval():
            return {"uncovered": [uncovered("AMZN", 1)],
                    "positions_available": True}

        monkeypatch.setattr(v2, "_evaluate_uncovered_symbols", fake_eval)
        out = self._run(v2.pause_alert_check())
        assert out["status"] == "ok"
        assert out["alerted"] is False
        assert out["uncovered_total"] == 1

    def test_extended_gap_alerts(self, monkeypatch):
        import routers.v2 as v2

        async def fake_eval():
            return {"uncovered": [uncovered("AMZN", 9)],
                    "positions_available": True}

        monkeypatch.setattr(v2, "_evaluate_uncovered_symbols", fake_eval)
        out = self._run(v2.pause_alert_check())
        assert out["status"] == "ok"
        assert out["alerted"] is True
        assert [s["symbol"] for s in out["alert_symbols"]] == ["AMZN"]

    def test_undecidable_days_are_reported_not_swallowed(self, monkeypatch):
        import routers.v2 as v2

        async def fake_eval():
            return {"uncovered": [], "positions_available": True,
                    "unknown_uncovered_days": [uncovered("NVDA", None)]}

        monkeypatch.setattr(v2, "_evaluate_uncovered_symbols", fake_eval)
        out = self._run(v2.pause_alert_check())
        assert out["alerted"] is False
        assert out["unknown_total"] == 1


class TestSilenceIsNeverAllClear:
    """BLOCKER (both reviewers): three separate paths returned `status: ok`
    while knowing nothing.

    The decision table is now the alert's source of truth, so a table that
    cannot be read, or that nobody is writing to, must read as UNKNOWN. This
    is the FC-046 failure shape — that sink died silently for eight months
    because its silence was indistinguishable from "nothing to report"."""

    @staticmethod
    def _service(decision_rows=None, raise_on_decisions=False):
        from services.bigquery import BigQueryService

        svc = BigQueryService.__new__(BigQueryService)
        svc.dataset = "test.options_wheel"

        def fake_run_query(query, params=None):
            if "fc018_per_symbol_scorecard" in query:
                return []
            if "decision_events" in query:
                if raise_on_decisions:
                    raise Exception("BigQuery unavailable")
                return decision_rows or []
            raise AssertionError(f"unexpected query: {query[:80]}")

        svc._run_query = fake_run_query
        return svc

    @staticmethod
    def _row(symbol, days, outcome="no_candidates", reason="no_qualifying_strikes"):
        return {
            "symbol": symbol, "run_id": "20260801T140000Z-abcdef12",
            "run_ts": "2026-08-01T14:00:00+00:00", "stage": "scan",
            "outcome": outcome, "reason": reason,
            "shares": 100, "cost_basis_per_share": 218.43,
            "current_price": 199.0, "underwater_pct": -0.089,
            "uncovered_days": days,
        }

    def test_a_query_failure_is_flagged_not_swallowed(self):
        svc = self._service(raise_on_decisions=True)
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "NVDA", "qty": 100}], threshold_days=7)
        assert out["decision_source_available"] is False

    def test_a_healthy_read_is_flagged_available(self):
        svc = self._service([self._row("NVDA", 9)])
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "NVDA", "qty": 100}], threshold_days=7)
        assert out["decision_source_available"] is True

    def test_a_held_symbol_with_no_rows_at_all_is_unknown(self):
        """The dead-writer case: rollback to a pre-Phase-4 revision, a failed
        table auto-create, a write-side BigQuery outage. The symbol used to
        vanish from BOTH lists and read as covered."""
        svc = self._service([self._row("NVDA", 9)])
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "NVDA", "qty": 100},
                            {"symbol": "AAPL", "qty": 100}], threshold_days=7)
        assert [r["symbol"] for r in out["unknown_uncovered_days"]] == ["AAPL"]
        assert out["unknown_uncovered_days"][0]["reason"] == "no_decision_records"

    def test_total_write_silence_makes_every_held_symbol_unknown(self):
        svc = self._service([])
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "NVDA", "qty": 100},
                            {"symbol": "AAPL", "qty": 100}], threshold_days=7)
        assert out["uncovered"] == []
        assert sorted(r["symbol"] for r in out["unknown_uncovered_days"]) == [
            "AAPL", "NVDA"]

    def test_a_sold_symbol_is_covered_not_unknown(self):
        # Belt and braces alongside the run-stage's uncovered_days=0: whatever
        # the label says, a symbol we just wrote a call on is not "unknown".
        svc = self._service([self._row("NVDA", None, outcome="sold", reason="")])
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "NVDA", "qty": 100}], threshold_days=7)
        assert out["uncovered"] == []
        assert out["unknown_uncovered_days"] == []

    def test_a_dropped_symbol_with_a_real_count_is_reported(self):
        svc = self._service([self._row("NVDA", 9, outcome="dropped",
                                       reason="insufficient_available_shares")])
        out = svc.get_uncovered_symbols(
            live_positions=[{"symbol": "NVDA", "qty": 100}], threshold_days=7)
        assert [r["symbol"] for r in out["uncovered"]] == ["NVDA"]


@pytest.mark.skipif(not importlib.util.find_spec("fastapi"),
                    reason="FastAPI only present in the dashboard image")
class TestEndpointHonoursTheDegradedFlag:
    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_an_unreadable_decision_table_is_degraded_not_ok(self, monkeypatch):
        import routers.v2 as v2

        async def fake_eval():
            return {"uncovered": [], "positions_available": True,
                    "decision_source_available": False}

        monkeypatch.setattr(v2, "_evaluate_uncovered_symbols", fake_eval)
        out = self._run(v2.pause_alert_check())
        assert out["status"] == "degraded"
        assert "decision records" in out["reason"]

    def test_a_missing_flag_defaults_to_available(self, monkeypatch):
        # Backwards compatibility with any caller/fixture predating the flag.
        import routers.v2 as v2

        async def fake_eval():
            return {"uncovered": [], "positions_available": True}

        monkeypatch.setattr(v2, "_evaluate_uncovered_symbols", fake_eval)
        assert self._run(v2.pause_alert_check())["status"] == "ok"
