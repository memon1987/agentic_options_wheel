"""Point-in-time earnings calendar + rolling wiring (FC-032 Phase 3).

The earnings gate is no longer small in blast radius. FC-013 made the scanner's
entry-side span gate a consumer, and FC-078 deleted the roller's fail-open
``rolling_earnings_blackout_days`` blackout and replaced it with a fail-CLOSED
span check on every roll *replacement*, evaluated daily. Both gates run every
trading day, so a replay that answers the earnings question from anything but
the frozen clock becomes quietly more permissive than production across the
whole window — hence the same treatment as the other seams: answer from the
frozen clock, or refuse.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from unittest.mock import Mock

import pytest

from src.backtesting.engine.historical_earnings import (
    EARNINGS_TABLE_PATH,
    HistoricalEarningsCalendar,
)
from src.strategy.wheel_engine import WheelEngine
from src.strategy.wheel_state_manager import WheelStateManager
from src.utils import clock


def _at(day: date):
    return clock.frozen(datetime.combine(day, time(16, 0)))


CAL = {"XYZ": [date(2025, 2, 20), date(2025, 5, 21), date(2025, 8, 27)]}


class TestPointInTime:
    def test_next_earnings_is_relative_to_the_simulated_date(self):
        cal = HistoricalEarningsCalendar(CAL)
        with _at(date(2025, 1, 10)):
            assert cal.next_earnings_date("XYZ") == date(2025, 2, 20)
        with _at(date(2025, 3, 1)):
            assert cal.next_earnings_date("XYZ") == date(2025, 5, 21)
        with _at(date(2025, 9, 1)):
            assert cal.next_earnings_date("XYZ") is None  # past the table

    def test_earnings_on_the_simulated_day_counts_as_today(self):
        cal = HistoricalEarningsCalendar(CAL)
        with _at(date(2025, 2, 20)):
            assert cal.next_earnings_date("XYZ") == date(2025, 2, 20)
            assert cal.get_earnings_proximity("XYZ")["days_until"] == 0

    def test_blackout_window_is_inclusive_of_both_ends(self):
        cal = HistoricalEarningsCalendar(CAL)
        # Blackout of 2 days: 18th is 2 days out -> blocked; 17th is 3 -> clear.
        with _at(date(2025, 2, 18)):
            assert cal.is_earnings_within_n_days("XYZ", 2) is True
        with _at(date(2025, 2, 17)):
            assert cal.is_earnings_within_n_days("XYZ", 2) is False
        with _at(date(2025, 2, 20)):
            assert cal.is_earnings_within_n_days("XYZ", 2) is True

    def test_unknown_symbol_fails_open_like_the_live_service(self):
        cal = HistoricalEarningsCalendar(CAL)
        with _at(date(2025, 2, 18)):
            assert cal.is_earnings_within_n_days("NOPE", 2) is False
        assert "NOPE" in cal.symbols_without_data

    def test_etf_with_no_earnings_is_known_not_missing(self):
        """An empty list is a real answer; it must not be reported as missing data."""
        cal = HistoricalEarningsCalendar({"SPY": []})
        with _at(date(2025, 2, 18)):
            assert cal.is_earnings_within_n_days("SPY", 2) is False
        assert "SPY" not in cal.symbols_without_data

    def test_refuses_to_answer_without_a_frozen_clock(self):
        cal = HistoricalEarningsCalendar(CAL)
        assert not clock.is_frozen()
        with pytest.raises(RuntimeError, match="frozen clock"):
            cal.next_earnings_date("XYZ")

    def test_proximity_dict_matches_the_live_services_keys(self):
        cal = HistoricalEarningsCalendar(CAL)
        with _at(date(2025, 2, 18)):
            got = cal.get_earnings_proximity("XYZ")
        assert set(got) == {"next_earnings_date", "days_until", "earnings_hour"}
        assert got["next_earnings_date"] == "2025-02-20"
        assert got["days_until"] == 2


class TestCommittedTable:
    def test_table_loads_and_covers_the_traded_universe(self):
        cal = HistoricalEarningsCalendar.from_table()
        traded = ["NVDA", "AMD", "AMZN", "GOOGL", "UNH", "AAPL", "MSFT", "IWM"]
        for symbol in traded:
            assert symbol in cal._earnings, f"{symbol} missing from the earnings table"

    def test_table_spans_the_alpaca_options_window(self):
        with open(EARNINGS_TABLE_PATH) as fh:
            payload = json.load(fh)
        nvda = payload["earnings"]["NVDA"]
        assert nvda[0] <= "2024-02-01", "table must start before Alpaca's history floor"
        assert nvda[-1] >= "2026-01-01"

    def test_etfs_are_present_with_empty_lists(self):
        with open(EARNINGS_TABLE_PATH) as fh:
            payload = json.load(fh)
        for etf in ("SPY", "QQQ", "IWM"):
            assert payload["earnings"].get(etf) == [], f"{etf} should be a known ETF"

    def test_known_nvda_earnings_date_is_correct(self):
        """Spot-check against reality: NVDA reported 2025-02-26."""
        cal = HistoricalEarningsCalendar.from_table()
        with _at(date(2025, 2, 1)):
            assert cal.next_earnings_date("NVDA") == date(2025, 2, 26)


class TestRollingSeam:
    def test_injected_calendar_is_used_by_the_rolling_cycle(self):
        sentinel = Mock(name="HistoricalEarningsCalendar")
        engine = WheelEngine(Mock(), alpaca_client=Mock(),
                             wheel_state=WheelStateManager(),
                             earnings_calendar=sentinel)
        assert engine._injected_earnings_calendar is sentinel

    def test_default_still_constructs_the_live_service(self, monkeypatch):
        built = {}

        class _FakeService:
            def __init__(self, config):
                built["config"] = config

        monkeypatch.setattr("src.strategy.wheel_engine.EarningsCalendarService", _FakeService)
        config = Mock()
        config.rolling_enabled = True
        config.earnings_enabled = True
        # FC-120 PR-2: the cycle derives its per-position budget from these
        # five keys (roll_budget.from_config); a Mock there is a TypeError.
        config.rolling_btc_fill_timeout_seconds = 120
        config.rolling_btc_reprice_attempts = 2
        config.rolling_stc_rung_timeout_seconds = 30
        config.rolling_stc_escalation_rungs = 0
        config.rolling_fallback_strike_attempts = 2
        engine = WheelEngine(config, alpaca_client=Mock(),
                             wheel_state=WheelStateManager())
        assert engine._injected_earnings_calendar is None
        # The live service is constructed lazily inside the roll cycle.
        engine.alpaca.get_positions.return_value = []
        engine.run_rolling_cycle()
        assert built.get("config") is config


# =========================================================================== #
# FC-013 — the scanner gate in replay
#
# Plan: docs/plans/fc-013.md §2 / §4. FC-068 repointed the replay onto the
# scanner pipeline, which created an obligation: a scanner-layer gate only
# applies in replays if the simulator hands the scanner the point-in-time
# calendar. These tests are that obligation, made executable.
# =========================================================================== #

from src.api.earnings_calendar import (
    EARNINGS_CLEAR,
    EARNINGS_KNOWN,
    EARNINGS_UNKNOWN,
    EarningsCalendarService,
)


class TestTristateParity:
    """The replay calendar answers the same shape the live service does."""

    def test_a_date_ahead_of_the_simulated_day_is_known(self):
        cal = HistoricalEarningsCalendar(CAL)
        with _at(date(2025, 1, 10)):
            assert cal.next_earnings_info("XYZ") == (EARNINGS_KNOWN, date(2025, 2, 20))

    def test_an_absent_symbol_is_CLEAR_never_unknown(self):
        """The one deliberate asymmetry with live, and its rationale.

        Live fails closed because an outage means unverifiable risk with real
        money at stake. A replay "outage" is a table gap: answering 'unknown'
        would silently zero out that symbol's whole replay — a pessimistic
        measurement bias corrupting the verdict rather than protecting
        anything. Fail open, and REPORT.
        """
        cal = HistoricalEarningsCalendar(CAL)
        with _at(date(2025, 1, 10)):
            assert cal.next_earnings_info("NOPE") == (EARNINGS_CLEAR, None)
        assert "NOPE" in cal.symbols_without_data

    def test_a_present_but_exhausted_symbol_is_CLEAR_and_flagged(self):
        cal = HistoricalEarningsCalendar(CAL)
        with _at(date(2025, 9, 1)):  # past the table's last XYZ date
            assert cal.next_earnings_info("XYZ") == (EARNINGS_CLEAR, None)
        assert "XYZ" in cal.symbols_past_horizon
        assert "XYZ" not in cal.symbols_without_data

    def test_an_etf_is_clear_and_flagged_as_neither(self):
        """Present-with-empty by design; it has no horizon to exhaust."""
        cal = HistoricalEarningsCalendar({"SPY": []})
        with _at(date(2025, 9, 1)):
            assert cal.next_earnings_info("SPY") == (EARNINGS_CLEAR, None)
        assert cal.symbols_past_horizon == set()
        assert cal.symbols_without_data == set()
        assert cal.horizon_for("SPY") is None

    @pytest.mark.parametrize("days_out,expected", [(0, True), (2, True), (3, False)])
    def test_earnings_within_matches_the_live_derivation(self, days_out, expected):
        cal = HistoricalEarningsCalendar({"XYZ": [date(2025, 2, 20)]})
        with _at(date(2025, 2, 20) - timedelta(days=days_out)):
            assert cal.earnings_within("XYZ", 2) is expected

    def test_horizon_for_reports_the_last_covered_date(self):
        cal = HistoricalEarningsCalendar(CAL)
        assert cal.horizon_for("XYZ") == date(2025, 8, 27)
        assert cal.horizon_for("ABSENT") is None

    def test_the_surface_matches_the_live_services(self):
        """A missing method here is a replay that silently skips the gate."""
        for name in ("next_earnings_info", "earnings_within",
                     "is_earnings_within_n_days", "get_earnings_proximity"):
            assert hasattr(HistoricalEarningsCalendar, name)
            assert hasattr(EarningsCalendarService, name)


class TestTheSimulatorWiresTheScannerGate:
    """Tests 15, 16, 16b, 16c — the replay, end to end."""

    @staticmethod
    def _fixture():
        """Assigns a put, recovers, then sells covered calls — the shape that
        exercises BOTH legs. Mirrors test_backtest_simulator's
        `dip_then_recovering`."""
        from tests.test_backtest_simulator import _weekdays

        warmup = _weekdays(date(2024, 3, 25), 45)
        days = _weekdays(date(2024, 6, 3), 45)
        closes = {d: 100.0 for d in warmup}
        for i, d in enumerate(days):
            if i <= 8:
                closes[d] = 100.0 - i * 3.0
            elif i <= 20:
                closes[d] = 76.0 + (i - 8) * 1.6
            else:
                closes[d] = 95.0 + (i - 20) * 0.9
        expirations = [d for d in days if d.weekday() == 4]
        return days, closes, expirations

    def _run(self, earnings_calendar=None, config=None):
        from src.backtesting.data.chain_builder import ChainBuilder
        from src.backtesting.engine.simulator import Simulator
        from src.utils.config import Config
        from tests.test_backtest_simulator import ScriptedProvider

        days, closes, expirations = self._fixture()
        provider = ScriptedProvider("XYZ", closes, expirations)
        builder = ChainBuilder(provider, risk_free_rate=0.04)
        sim = Simulator(
            config or Config(), provider, builder, ["XYZ"], days[0], days[-1],
            starting_cash=50_000.0, max_dte=7,
            earnings_calendar=earnings_calendar,
        )
        return sim.run(), days

    @staticmethod
    def _opens(result, kind):
        return [e for e in result.broker.ledger if e.kind == kind]

    @staticmethod
    def _expiry_of(occ_symbol):
        return datetime.strptime(occ_symbol[3:9], "%y%m%d").date()

    # The two dates are chosen against the fixture's actual trade schedule
    # (verified by running it ungated), so neither assertion below is vacuous:
    #   * 2024-06-12 sits inside the life of the covered call sold 06-10
    #     expiring 06-14  -> the SPAN case,
    #   * 2024-07-16 is one day after the put sold 07-15  -> the N=2 case
    #     (days_until = 1, the GOOGL incident geometry).
    # `test_replay_honors_earnings_enabled_false` runs the same table with the
    # gate config-disabled and asserts both violations DO occur there, which is
    # what keeps this test honest.
    EARNINGS = [date(2024, 6, 12), date(2024, 7, 16)]

    @classmethod
    def _blocked_days(cls):
        return {e - timedelta(days=n) for e in cls.EARNINGS for n in range(3)}

    @classmethod
    def _violations(cls, result):
        """(puts sold inside an N=2 window, calls sold that span an event)."""
        puts = [e for e in cls._opens(result, "sell_put_open")
                if e.event_date in cls._blocked_days()]
        calls = [e for e in cls._opens(result, "sell_call_open")
                 if any(e.event_date <= earnings <= cls._expiry_of(e.symbol)
                        for earnings in cls.EARNINGS)]
        return puts, calls

    def test_replay_uses_the_point_in_time_calendar_never_finnhub(self, monkeypatch):
        """Test 15 — the FC-068 seam obligation, mutation-style.

        Fails TWO ways by design: if the simulator stops passing the calendar,
        the scanner default-constructs the live service and the patched
        __init__ raises; if the gate is reverted, a put opens inside the N=2
        window and a spanning call is emitted.
        """
        def _explode(self, config):
            raise AssertionError(
                "a replay must never construct the live Finnhub service")

        monkeypatch.setattr(EarningsCalendarService, "__init__", _explode)

        calendar = HistoricalEarningsCalendar({"XYZ": list(self.EARNINGS)})
        result, _ = self._run(earnings_calendar=calendar)

        puts = self._opens(result, "sell_put_open")
        calls = self._opens(result, "sell_call_open")
        assert puts, "fixture opened no put — the put assertion would be vacuous"
        assert calls, "fixture sold no call — the span assertion would be vacuous"

        in_window_puts, spanning_calls = self._violations(result)
        assert in_window_puts == [], (
            "a put opened inside the blackout window: "
            + str([e.event_date for e in in_window_puts]))
        assert spanning_calls == [], (
            "a call spanning earnings was sold: "
            + str([(e.event_date, e.symbol) for e in spanning_calls]))

        # (iii) the symbol still trades on clear days — the gate must not brick
        # the replay, only the exposed days.
        assert [e for e in puts + calls if e.event_date not in self._blocked_days()]

    def test_replay_reports_symbols_missing_from_the_table(self):
        """Test 16 — fail open, but never silently."""
        calendar = HistoricalEarningsCalendar({"OTHER": [date(2024, 7, 1)]})

        result, _ = self._run(earnings_calendar=calendar)

        assert self._opens(result, "sell_put_open"), "fail-open means it trades"
        assert "XYZ" in result.earnings_symbols_without_data

    def test_replay_honors_earnings_enabled_false(self, monkeypatch):
        """Test 16b (DD-8) — config supplies policy, injection supplies data.

        A live rolloff (`earnings.enabled: false`) must disable the gate in
        replays too. Otherwise a what-if measurement diverges from the live
        behaviour it is measuring, on exactly the FC-048 fidelity axis.

        This test doubles as the anti-vacuity proof for test 15: it asserts the
        SAME table, with the gate off, produces both violations.
        """
        from src.utils.config import Config

        calendar = HistoricalEarningsCalendar({"XYZ": list(self.EARNINGS)})

        monkeypatch.setenv("EARNINGS_ENABLED", "false")
        ungated, _ = self._run(earnings_calendar=calendar, config=Config())

        in_window_puts, spanning_calls = self._violations(ungated)
        assert in_window_puts, (
            "with the gate config-disabled the replay must open puts inside the "
            "earnings window exactly as the pre-FC-013 replay did")
        assert spanning_calls, (
            "with the gate config-disabled the replay must sell calls that span "
            "earnings exactly as the pre-FC-013 replay did")

    def test_replay_flags_horizon_exhaustion(self):
        """Test 16c — silent fail-open at the table's edge becomes reportable.

        A symbol PRESENT in the table with only PAST dates otherwise answers
        exactly like known-clear. The 2026-07-18 table had most reporters' last
        dates already behind us by 08-01, and nothing said so.
        """
        calendar = HistoricalEarningsCalendar({
            "XYZ": [date(2024, 1, 15)],   # entirely before the replay window
            "SPY": [],                    # ETF: present-with-empty by design
        })

        result, _ = self._run(earnings_calendar=calendar)

        assert "XYZ" in result.earnings_symbols_past_horizon
        assert "SPY" not in result.earnings_symbols_past_horizon
        assert "XYZ" not in result.earnings_symbols_without_data

    def test_a_healthy_table_reports_neither(self):
        days, _, _ = self._fixture()
        calendar = HistoricalEarningsCalendar({"XYZ": [days[30], date(2024, 12, 1)]})

        result, _ = self._run(earnings_calendar=calendar)

        assert result.earnings_symbols_without_data == []
        assert result.earnings_symbols_past_horizon == []

    def test_the_report_surfaces_both_sets(self):
        """The operator has to be able to SEE it, not just have it in the object."""
        from src.backtesting.evaluate import _data_quality

        calendar = HistoricalEarningsCalendar({"OTHER": [date(2024, 7, 1)]})
        result, _ = self._run(earnings_calendar=calendar)
        quality = _data_quality(result, [])

        assert quality["earnings_symbols_missing_from_table"] == ["XYZ"]
        assert "earnings_symbols_past_table_horizon" in quality
