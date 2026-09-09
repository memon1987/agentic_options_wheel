"""FC-100 — the daily credit-only roller, wired for the covered-call profile.

The roller code is live-proven on the wheel (first roll 2026-08-04, daily
cycles since). What had never executed before this FC is the **roll path on
the covered-call profile** — a different account, a different BigQuery dataset
(`covered_call`), the CC profile's earnings/universe keys, and operator-bought
lots whose cost-basis cross-check finds no assignment history.

So these tests drive the **real** `CallRoller`, through the real
`WheelEngine.run_rolling_cycle`, against the **real shipped**
`config/covered_call.yaml` — a Mock config would assert nothing about the
profile, which is the only thing FC-100 changes. Only the broker and the chain
are faked.

Test IDs map to docs/plans/fc-100.md §Tests (T-3, T-4, T-6).
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.risk.risk_manager import RiskManager
from src.strategy.wheel_engine import WheelEngine
from src.utils.config import Config

REPO = Path(__file__).resolve().parent.parent
CC_YAML = str(REPO / "config" / "covered_call.yaml")
WHEEL_YAML = str(REPO / "config" / "settings.yaml")
POLICY_DIR = REPO / "deploy" / "monitoring"

# The old contract's expiry. Every horizon in this file is expiry-relative
# (FC-078 DD-3): the replacement bound is OLD expiry + 14, never today + 14.
OLD_EXPIRY = date(2026, 10, 16)
IN_HORIZON = OLD_EXPIRY + timedelta(days=14)
BASIS = 352.00  # > the 350 old strike, so the cost-basis floor BINDS


def occ(underlying: str, expiry: date, strike: float, kind: str = "C") -> str:
    """Build an OCC symbol. Never hand-roll this format inline."""
    return (f"{underlying}{expiry.strftime('%y%m%d')}{kind}"
            f"{int(round(strike * 1000)):08d}")


def candidate(strike, bid, ask, *, delta=0.55, expiry=IN_HORIZON,
              underlying="GOOGL"):
    """A replacement-call dict shaped like find_suitable_calls output."""
    return {
        "symbol": occ(underlying, expiry, strike),
        "strike_price": strike,
        "delta": delta,
        "bid": bid,
        "ask": ask,
        "mid_price": round((bid + ask) / 2, 2),
        "expiration_date": expiry.isoformat(),
        "volume": 100,
        "open_interest": 500,
    }


class _Book:
    """A CC book of one covered short call, plus the fake broker behind it.

    `avg_entry_price` on the equity leg is the ONLY cost-basis input on this
    profile's happy path: the lots are operator-bought, so the BigQuery
    cross-check finds no assignment rows and returns `no_assignment_history`
    (expected, not a divergence — see T-3(e)).
    """

    def __init__(self, *, underlying="GOOGL", strike=350.0, expiry=OLD_EXPIRY,
                 shares=100, basis=BASIS, stock_bid=359.00, stock_ask=359.20,
                 old_bid=9.80, old_ask=10.00, candidates=None,
                 avg_entry_price=True):
        self.old_symbol = occ(underlying, expiry, strike)
        equity = {"symbol": underlying, "qty": str(shares),
                  "asset_class": "us_equity", "side": "long"}
        if avg_entry_price:
            equity["avg_entry_price"] = f"{basis:.2f}"
        self.positions = [
            {"symbol": self.old_symbol, "qty": "-1",
             "asset_class": "us_option"},
            equity,
        ]
        self.candidates = candidates if candidates is not None else [
            candidate(355.0, bid=10.50, ask=10.70)]

        quotes = {self.old_symbol: {"bid": old_bid, "ask": old_ask}}
        for c in self.candidates:
            quotes[c["symbol"]] = {"bid": c["bid"], "ask": c["ask"]}

        self.alpaca = Mock()
        self.alpaca.get_positions.return_value = self.positions
        self.alpaca.get_orders.return_value = []
        self.alpaca.get_stock_quote.return_value = {"bid": stock_bid,
                                                    "ask": stock_ask}
        self.alpaca.get_option_quote.side_effect = \
            lambda symbol: dict(quotes.get(symbol, {}))

        self.market_data = Mock()
        self.market_data.find_suitable_calls.return_value = self.candidates

        self.earnings = Mock()
        # KNOWN and far away: the span floor exists (so T-4 can assert the
        # kwarg) but excludes nothing inside the 14-day horizon.
        self.earnings.next_earnings_info.return_value = (
            "known", OLD_EXPIRY + timedelta(days=40))
        self.earnings.get_earnings_proximity.return_value = {}

    def run(self, config_path=CC_YAML):
        """Run one real rolling cycle; return (results, events)."""
        events = []

        def _capture(logger, event_type, **kwargs):
            events.append((event_type, kwargs))

        config = Config(config_path)
        with patch("src.strategy.wheel_engine.MarketDataManager",
                   return_value=self.market_data):
            engine = WheelEngine(config, alpaca_client=self.alpaca,
                                 earnings_calendar=self.earnings)
        with patch("src.strategy.call_roller.log_trade_event", _capture):
            results = engine.run_rolling_cycle()
        return results, events

    @staticmethod
    def event(events, event_type):
        matches = [kw for et, kw in events if et == event_type]
        assert matches, f"{event_type} never fired; got {[e for e, _ in events]}"
        return matches[0]

    @staticmethod
    def types(events):
        return [et for et, _ in events]


@pytest.fixture(autouse=True)
def _dry_run(monkeypatch):
    """Every cycle in this file runs with ROLLER_DRY_RUN=true.

    The point is not convenience: the CC service's first `/roll` traffic is a
    supervised dry-run session (fc-100 §Rollout O5-O7), and these tests are the
    proof that the dry-run branch reaches its event *without* touching an order
    method. `ROLLER_ENABLED` is deleted so an ambient env cannot disable the
    cycle under test and turn every assertion vacuous.
    """
    monkeypatch.setenv("ROLLER_DRY_RUN", "true")
    monkeypatch.delenv("ROLLER_ENABLED", raising=False)


class TestTheCCRollCycle:
    """T-3. The real CallRoller on the real covered-call profile.

    *Catches:* any CC-profile key the roll path hard-indexes and the profile
    lacks (the `stock_metrics_error` class that kept the CC engine from writing
    a single call until PR #107), a resolver pointed at the wheel dataset
    (FC-075 DD-4), dry-run placing an order, and a non-binding cost-basis floor.
    """

    def test_a_itm_roll_prices_both_legs_and_the_floor_binds(self):
        """(a) Stock 359.10 vs strike 350 → ratio 1.026, over the CC's 1.00.

        The floor is the load-bearing assertion: basis 352.00 exceeds
        `old_strike + 0.01` = 350.01, so `min_strike_price` must be **352.00**.
        A roller that passed 350.01 would let the replacement be sold below
        what the shares cost — the one thing the FC-065 floor exists to stop.
        """
        book = _Book()
        results, events = book.run()

        assert results["rolls_evaluated"] == 1
        assert "call_roll_evaluated" in book.types(events)

        dry = book.event(events, "call_roll_dry_run")
        assert dry["would_be_btc_limit"] == 10.00   # the old call's ask
        assert dry["would_be_stc_limit"] == 10.50   # the candidate's bid
        assert dry["net_credit"] == 50.0            # ($10.50 - $10.00) x 100

        # Dry run means NOTHING is placed. Asserted on the client, not on the
        # return value: a return of `dry_run` with an order behind it is the
        # failure this exists to catch.
        assert not book.alpaca.place_option_order.called
        assert not book.alpaca.cancel_order.called

        _, kwargs = book.market_data.find_suitable_calls.call_args
        assert kwargs["min_strike_price"] == 352.00, (
            "the cost-basis floor did not bind — the roller passed "
            f"{kwargs['min_strike_price']} instead of the 352.00 basis")

    def test_b_a_candidate_that_nets_no_credit_is_refused(self):
        """(b) Candidate bid 9.90 against a 10.00 BTC limit = a $0.10 debit.

        Credit-only is the invariant the whole FC rests on; it is enforced on
        the *placed limits*, so a screen that let this through would place a
        live two-leg order pair that loses money on fill."""
        book = _Book(candidates=[candidate(355.0, bid=9.90, ask=10.10)])
        _, events = book.run()

        skip = book.event(events, "call_roll_skipped")
        assert skip["skip_reason"] == "no_credit_candidate"
        assert "call_roll_dry_run" not in book.types(events)

    def test_c_a_098_ratio_is_not_a_roll_on_the_cc_profile(self):
        """(c) The operator's D-A decision, executable.

        GOOGL 338.50 against its own C342.5 is ratio 0.988 — eligible under
        the wheel's 0.98, *not* under the CC's 1.00. (The live instance of this
        book on 2026-09-08 was a call sold that same session; the fixture pins
        a fixed `OLD_EXPIRY` instead, because every horizon assertion in this
        file is expiry-relative and a today-relative fixture would drift.)
        Under 0.98 the roller would buy back the engine's own fresh call at the
        ask and re-sell up to 14 days further out at any delta <= 0.60,
        bypassing the delta band, the DTE ceiling, the premium floor and the
        spread gate that the entry path applies.
        """
        book = _Book(strike=342.5, expiry=OLD_EXPIRY,
                     stock_bid=338.40, stock_ask=338.60)
        _, events = book.run()

        skip = book.event(events, "call_roll_skipped")
        assert skip["skip_reason"] == "not_itm_enough"
        assert skip["itm_trigger_ratio"] == 1.00
        assert 0.98 < skip["itm_ratio"] < 1.00, skip["itm_ratio"]
        # No chain was even fetched: the gate is upstream of the candidate
        # search, which is what makes it cheap as well as correct.
        assert not book.market_data.find_suitable_calls.called

    def test_c_the_same_book_reaches_candidates_on_the_wheel_profile(self):
        """The contrast half of (c). Identical book, wheel config: 0.988 clears
        0.98 and the roller goes looking for a replacement.

        Without this, `not_itm_enough` above could be caused by anything in the
        fixture rather than by the trigger ratio."""
        book = _Book(strike=342.5, expiry=OLD_EXPIRY,
                     stock_bid=338.40, stock_ask=338.60)
        _, events = book.run(config_path=WHEEL_YAML)

        assert book.market_data.find_suitable_calls.called, (
            "the wheel profile did not reach the candidate search — the "
            "contrast that proves 1.00 is what stopped the CC cycle is gone")
        assert "not_itm_enough" not in [
            kw.get("skip_reason") for _, kw in events]

    def test_d_a_position_with_no_broker_basis_fails_closed(self):
        """(d) The silent-inert mode: Alpaca reports no `avg_entry_price`.

        There is no floor to price against, so the roller must skip — but
        loudly, on its own event, with the reason carried. FC-029 saw Alpaca
        return 0 for assigned positions; if that recurs here every position
        skips and the roller does nothing at all, with no error anywhere. This
        event and the alert wired to it (cc_cost_basis_alert_policy.json) are
        the only way that state is visible.
        """
        book = _Book(avg_entry_price=False)
        results, events = book.run()

        assert results["rolls_evaluated"] == 1
        skip = book.event(events, "call_roll_skipped_cost_basis_unresolved")
        assert skip["skip_reason"] == "cost_basis_unresolved"
        assert skip["cross_check_reason"] == "no_broker_basis"
        assert "call_roll_dry_run" not in book.types(events)
        assert not book.market_data.find_suitable_calls.called

    @pytest.mark.real_bq_lookup  # opt out of the conftest hermeticity stub
    def test_e_the_cross_check_queries_the_covered_call_dataset(self):
        """(e) The dataset, captured — not asserted off the config.

        `CostBasisResolver` hardcoded `options_wheel` until FC-075 DD-4; on a
        shared symbol that made the CC service reconstruct the WHEEL's
        assignment lots, read SOURCE_DIVERGENT, and block the symbol forever.
        An assertion on `config.bigquery_dataset` would have passed throughout
        that bug, so this captures the SQL the resolver actually sends.

        The marker is load-bearing: without it conftest's autouse stub replaces
        `_lookup_assignment_basis` wholesale, no `bigquery.Client` is ever
        constructed, and `cross_check.reason` can never be
        `no_assignment_history`.
        """
        captured = {}

        class _FakeJob:
            def result(self, *a, **k):
                return []      # no assignment rows — an operator-bought lot

        class _FakeClient:
            def query(self, sql, **kw):
                captured["sql"] = sql
                return _FakeJob()

        resolutions = []
        from src.strategy.cost_basis import CostBasisResolver
        real_resolve = CostBasisResolver.resolve_detailed

        def _spy(self, symbol, stock_position, shares_owned):
            out = real_resolve(self, symbol, stock_position, shares_owned)
            resolutions.append(out)
            return out

        book = _Book()
        with patch("google.cloud.bigquery.Client", return_value=_FakeClient()), \
             patch("google.cloud.bigquery.QueryJobConfig", return_value=Mock()), \
             patch("google.cloud.bigquery.ScalarQueryParameter",
                   return_value=Mock()), \
             patch.object(CostBasisResolver, "resolve_detailed", _spy):
            _, events = book.run()

        assert "covered_call.trades_from_activities" in captured["sql"]
        assert "options_wheel." not in captured["sql"]

        assert resolutions, "the resolver was never called"
        cross_check = resolutions[0]["cross_check"]
        assert cross_check["status"] == "unavailable"
        assert cross_check["reason"] == "no_assignment_history", (
            "on this account the lots are operator-bought, so 'no assignment "
            "history' is the EXPECTED cross-check state — anything else means "
            "the query found rows it should not have")
        # And the roll still happens: an unavailable cross-check is not a veto.
        assert "call_roll_dry_run" in book.types(events)


class TestTheGateNamingContract:
    """T-4. The Phase C hand-off table (fc-100.md), made executable.

    The FC-096 Phase C replay is told to share *these* gates by name so that
    replay == live. A roller change that stopped passing one of them would
    leave the replay measuring a policy the live service does not run, and
    nothing else in the suite would notice.
    """

    @staticmethod
    def _search_kwargs():
        book = _Book()
        book.run()
        _, kwargs = book.market_data.find_suitable_calls.call_args
        return kwargs

    def test_the_roll_search_carries_every_named_gate(self):
        kwargs = self._search_kwargs()
        # The 'roll' criteria profile: no DTE cap, no premium floor, no entry
        # delta band, and (FC-110) no excluded_symbols. Passing 'call' here
        # would apply the entry gates to a defensive roll and make shallow-ITM
        # rescue illegal — the trap FC-078 DD-3 removed.
        assert kwargs["criteria_profile"] == "roll"
        # The cost-basis floor, binding over old_strike + 0.01.
        assert kwargs["min_strike_price"] == 352.00
        # Expiry-relative horizon: OLD expiry + 14, not today + 14.
        assert kwargs["include_expiry_on_or_before"] == \
            OLD_EXPIRY + timedelta(days=14)
        # The FC-013 span gate, fail-closed, applied to the REPLACEMENT.
        assert kwargs["exclude_expiry_on_or_after"] == \
            OLD_EXPIRY + timedelta(days=40)

    @pytest.mark.parametrize("bad,reason_fragment", [
        (candidate(360.0, bid=11.0, ask=11.2, delta=0.65), "rail"),
        (candidate(351.0, bid=11.0, ask=11.2), "cost basis"),
        (candidate(360.0, bid=11.0, ask=11.2,
                   expiry=OLD_EXPIRY + timedelta(days=15)), "horizon"),
    ])
    def test_validate_roll_on_the_cc_config_rejects_each_illegal_leg(
            self, bad, reason_fragment):
        """The three rails, on the CC profile's own numbers.

        The strike-351 case is the floor one: 351 clears `old_strike + 0.01`
        (350.01) and is a legal roll-UP, yet it is below the 352.00 basis. If
        `validate_roll` stopped enforcing the basis, the search-kwarg assertion
        above would still pass while a candidate under the basis got sold.
        """
        rm = RiskManager(Config(CC_YAML))
        valid, reason = rm.validate_roll(
            bad, current_strike=350.0, cost_basis_per_share=BASIS,
            max_expiry=OLD_EXPIRY + timedelta(days=14))
        assert valid is False
        assert reason_fragment in reason.lower(), reason

    def test_a_legal_leg_still_passes(self):
        """Guard the guard: three rejections prove nothing if everything is
        rejected."""
        rm = RiskManager(Config(CC_YAML))
        valid, _ = rm.validate_roll(
            candidate(355.0, bid=10.5, ask=10.7), current_strike=350.0,
            cost_basis_per_share=BASIS,
            max_expiry=OLD_EXPIRY + timedelta(days=14))
        assert valid is True


class TestTheCCAlertPolicies:
    """T-6. The two policies FC-100 ships or patches.

    These are the CC roll path's ONLY detective layer: /regression's log checks
    are hardcoded to the wheel's service name and filter a severity the app
    never emits (FC-111), and a cut cycle is a scheduler 504 nothing watches
    (FC-107/FC-108). A twin still pointed at the wheel would be worse than no
    policy — it would look deployed.
    """

    FILES = ("cc_roll_executed_alert_policy.json",
             "cc_cost_basis_alert_policy.json")

    @staticmethod
    def _doc(name):
        return json.loads((POLICY_DIR / name).read_text())

    @pytest.mark.parametrize("name", FILES)
    def test_every_condition_filter_names_the_cc_service(self, name):
        doc = self._doc(name)
        for condition in doc["conditions"]:
            matched = condition.get("conditionMatchedLog") or {}
            assert 'resource.labels.service_name="covered-call-engine"' in \
                matched["filter"], name

    @pytest.mark.parametrize("name", FILES)
    def test_neither_the_wheel_service_nor_its_dataset_is_mentioned(self, name):
        """Raw text, filters AND runbook. A byte-copied twin would tell a 2am
        responder to kill the wheel's roller and check the wheel's fills."""
        raw = (POLICY_DIR / name).read_text()
        for wheel_substring in ("options-wheel-strategy", "options_wheel"):
            assert wheel_substring not in raw, (
                f"{name} still mentions {wheel_substring!r} — this policy "
                f"watches the covered-call service and its runbook is read "
                f"while an order pair is live on the OTHER account")

    def test_the_cost_basis_policy_watches_the_roll_pair_too(self):
        """It watched the scan pair only — harmless while the CC roller was
        inert, a gap the moment FC-100 wired it."""
        f = self._doc("cc_cost_basis_alert_policy.json")[
            "conditions"][0]["conditionMatchedLog"]["filter"]
        for leg in ("scan", "roll"):
            for verdict in ("unresolved", "divergent"):
                event = f"call_{leg}_skipped_cost_basis_{verdict}"
                assert f'jsonPayload.event_type="{event}"' in f, event
                assert f'textPayload:"{event}"' in f, (
                    f"{event}: Cloud Run captures the app's stderr as PLAIN "
                    f"TEXT, so a jsonPayload-only clause can match nothing")

    def test_the_roll_policy_watches_the_two_cycle_level_errors(self):
        """`roll_cycle_error` (the /roll handler's except) and
        `roll_position_error` (per-position evaluate/execute) are not in the
        wheel's policy — its gap, recorded under FC-108. Without them a /roll
        that 500s every day at 15:30 is invisible on this service."""
        f = self._doc("cc_roll_executed_alert_policy.json")[
            "conditions"][0]["conditionMatchedLog"]["filter"]
        for event in ("roll_cycle_error", "roll_position_error"):
            assert event in f, event
        for event in ("call_roll_completed", "call_roll_naked_exposure",
                      "call_roll_partial_naked_exposure",
                      "call_roll_unknown_disposition",
                      "call_roll_stc_disposition_unknown",
                      "call_roll_execution_error",
                      "call_roll_order_refetch_failed"):
            assert event in f, f"{event} dropped from the CC twin"

    def test_the_triage_query_can_actually_see_the_cycle_events(self):
        """R1. `roll_cycle_started` / `roll_cycle_completed` are emitted by
        `log_system_event`, which passes the name as the structlog **message**
        (`logger.info(event_type, ...)`,
        `src/utils/logging_events.py:270-271`). They therefore land under
        `jsonPayload.event` and carry **no `event_type` field at all**.

        A triage command filtering only on `jsonPayload.event_type` returns
        every `call_roll_*` row and ZERO cycle rows — verified live on two wheel
        cycles: 24 `call_roll_*`, no `roll_cycle_*`. That pair is precisely what
        distinguishes a completed cycle from one cut by the Cloud Run timeout,
        so the runbook's first command has to reach it.

        *Catches:* the `jsonPayload.event` clause being "tidied away" as
        redundant by someone reading the two filters as duplicates.
        """
        content = self._doc(
            "cc_roll_executed_alert_policy.json")["documentation"]["content"]
        assert 'jsonPayload.event=~"^roll_cycle_"' in content, (
            "the triage command cannot see roll_cycle_started/completed — they "
            "have no event_type field, only jsonPayload.event")

    def test_the_roll_runbook_is_written_for_this_service(self):
        """The runbook is the deliverable, not the filter. Every command in it
        has to name the CC service, the CC dataset and the CC job."""
        content = self._doc(
            "cc_roll_executed_alert_policy.json")["documentation"]["content"]
        for needle in ("covered-call-engine", "cc-roll-daily",
                       "covered_call.trades_from_activities",
                       "ROLLER_ENABLED=false"):
            assert needle in content, needle
