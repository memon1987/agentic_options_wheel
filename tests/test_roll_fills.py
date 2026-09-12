"""FC-116 T3 — the STO ladder driven through the REAL `CallRoller`.

Everything else in the FC-116 suite tests the adapter in isolation. This file
tests the thing that actually matters: that the live roller, unmodified, does
the RIGHT thing when the adapter tells it a leg did not fill. Three properties,
each of which was unreachable before this PR and each of which fails silently:

1. **The replay never sleeps.** `CallRoller._poll_order_fill` returns on its
   first `get_order_by_id` read only while the status is terminal; anything
   else makes it `time.sleep(poll_interval)` up to 120 s PER LEG, and the
   Saturday battery would blow its wall cap. `time.sleep` is monkeypatched to
   RAISE here, so a non-terminal status is a loud failure rather than a slow
   one.

   Note what does NOT pin this: the adapter's `__getattr__` raising
   `UnsupportedBacktestCall` on `cancel_order` proves nothing, because
   `_safe_cancel` swallows every exception — a reached cancel would quietly
   return `False` and settle.

2. **A failure after placement is COUNTED.** The simulator used to drop every
   failed roll record, so `btc_rejected`, a timed-out BTC, and a ladder that
   exhausted after the BTC filled were invisible in every replay: a reader saw
   `rolls_executed` fall and had nothing to attribute it to.

3. **`credit_gone_at_execution` is counted ONCE.** It is already tallied
   through `log_terminal_skip` -> `call_roll_skipped` BEFORE `execute_roll`
   returns it, so a fold that took every failed reason would double it.

The chains here are HAND-BUILT, deliberately. On a lake/model-built chain no
rung-1 roll leg can expire — a base-mode BTC limit is `round(ask, 2) >=
ask - 0.005 > bid` and an imminence one is `mid + 0.05 > bid`, with the STO
mirroring both — so the expiry branch, the post-placement terminals and rungs
>= 2 are simply unreachable there. That is a fact about the model, not evidence
that live rolls always fill, and it is why these paths need a book built by
hand to exercise them at all.
"""

from __future__ import annotations

from datetime import date, datetime, time
from unittest.mock import Mock

import pytest

from src.backtesting.data.chain_builder import ChainQuote, ChainSnapshot
from src.backtesting.data.provider import StockBar
from src.backtesting.engine.alpaca_adapter import BacktestAlpacaClient
from src.backtesting.engine.broker import BacktestBroker
from src.backtesting.engine.simulator import (
    _ALREADY_TALLIED_ROLL_FAILURES,
    _POST_PLACEMENT_ROLL_FAILURES,
    _merge_roll_skips,
)
from src.strategy.call_roller import CallRoller
from src.utils import clock

DAY = date(2024, 6, 3)
EXP_OLD = date(2024, 6, 7)
EXP_NEW = date(2024, 6, 14)
OLD = "XYZ240607C00100000"
NEW = "XYZ240614C00102000"


def _q(symbol, strike, *, bid, ask, expiration=EXP_NEW, spot=101.0):
    return ChainQuote(
        symbol=symbol, underlying="XYZ", as_of=DAY, expiration=expiration,
        strike=strike, option_type="call", dte=(expiration - DAY).days,
        underlying_price=spot, mark=(bid + ask) / 2, bid=bid, ask=ask,
        implied_volatility=0.30, delta=0.45, volume=500,
    )


def _client(broker, calls):
    return BacktestAlpacaClient(
        broker,
        chains={"XYZ": {DAY: ChainSnapshot("XYZ", DAY, 101.0, [], calls)}},
        stock_bars={"XYZ": [StockBar(symbol="XYZ", bar_date=DAY, open=101.0,
                                     high=101.0, low=101.0, close=101.0,
                                     volume=1_000_000)]},
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """The load-bearing fixture of this file.

    A non-terminal order status is the ONLY way the roller sleeps, and the only
    way it reaches the cancel-and-settle path. Raising here turns a 120-second
    regression into an immediate failure.
    """
    def _boom(seconds):  # pragma: no cover - the assertion IS the raise
        raise AssertionError(
            f"the replay slept {seconds}s — the adapter returned a NON-TERMINAL "
            f"order status under roll intent. At 120s per leg this is what "
            f"blows the Saturday battery's wall cap."
        )
    monkeypatch.setattr("src.strategy.call_roller.time.sleep", _boom)


@pytest.fixture
def roller_for():
    """Build a `CallRoller` whose `alpaca` IS the backtest adapter."""
    def build(client, *, min_credit=0.05, fallback_attempts=0):
        config = Mock()
        config.roller_dry_run = False
        config.rolling_min_net_credit_per_contract = min_credit
        config.rolling_fallback_strike_attempts = fallback_attempts
        config.rolling_order_poll_seconds = 120
        config.rolling_order_poll_interval_seconds = 5
        config.earnings_enabled = False
        risk = Mock()
        risk.validate_roll.return_value = (True, None)
        return CallRoller(client, Mock(), config, risk, Mock())
    return build


def _opportunity(*, btc_limit, stc_limit, min_credit=0.05, fallbacks=None):
    return {
        'underlying': 'XYZ',
        'old_option_symbol': OLD,
        'new_option_symbol': NEW,
        'old_strike': 100.0,
        'new_strike': 102.0,
        'contracts': 1,
        'btc_limit': btc_limit,
        'stc_limit': stc_limit,
        'min_credit_per_share': min_credit,
        'net_credit_per_contract': (stc_limit - btc_limit) * 100,
        'pricing_mode': 'base',
        'imminent': False,
        'earnings_info': {},
        'fallback_candidates': list(fallbacks or []),
        'cost_basis_per_share': 90.0,
        'max_expiry': EXP_NEW,
    }


def _covered_broker():
    broker = BacktestBroker(starting_cash=50_000.0)
    broker.deposit_shares("XYZ", 100, 90.0, DAY, premise="test cover")
    broker.sell_call_to_open(OLD, "XYZ", 100.0, EXP_OLD, 1,
                             mark=2.0, bid=1.9, opened=DAY)
    return broker


def _frozen():
    return clock.frozen(datetime.combine(DAY, time(16, 0)))


@pytest.fixture
def events():
    """Capture the roller's structlog events.

    Some of what this file asserts is on the EVENT rather than the return dict
    — `disposition` in particular, which is the field that separates "the order
    came back terminal and unfilled" from "we waited 120 s and gave up". In a
    replay it must always be the former.
    """
    import structlog

    seen = []

    def capture(_logger, _name, event_dict):
        seen.append(dict(event_dict))
        return event_dict

    prev = structlog.get_config()
    structlog.configure(processors=[capture] + list(prev.get("processors", [])))
    try:
        yield seen
    finally:
        structlog.configure(**prev)


# --------------------------------------------------------------------------- #
class TestABtcThatNeverFills:
    def test_a_btc_below_the_bid_is_a_terminal_no_fill(self, roller_for, events):
        """(a) The BTC limit sits below the bid, so nothing fills.

        The roller must report `btc_timeout_canceled` with
        `disposition=terminal_no_fill`, place NO sell-to-open (the shares are
        still covered by the old call — an STO here would be the naked-call
        window `_attempt_stc` exists to close), and leave the ledger untouched.
        """
        broker = _covered_broker()
        ledger_before = len(broker.ledger)
        client = _client(broker, [
            _q(OLD, 100.0, bid=2.00, ask=2.20, expiration=EXP_OLD),
            _q(NEW, 102.0, bid=2.50, ask=2.70),
        ])
        roller = roller_for(client)
        with _frozen(), client.order_intent("roll"):
            result = roller.execute_roll(
                _opportunity(btc_limit=1.50, stc_limit=2.50))

        assert result['success'] is False
        assert result['reason'] == 'btc_timeout_canceled'
        # `disposition` rides on the EVENT, not the return dict, and it is the
        # field that matters here: `terminal_no_fill` means the adapter handed
        # back a terminal status on the FIRST read. `timeout_canceled` would
        # mean the poll loop ran to exhaustion — 120 s per leg.
        cancels = [e for e in events
                   if e.get("event_type") == "call_roll_btc_timeout_canceled"]
        assert cancels, "the roller emitted no disposition event"
        assert cancels[-1]["disposition"] == "terminal_no_fill"
        assert len(broker.ledger) == ledger_before, "an unfilled leg moved money"
        assert OLD in broker.options, "the old call must still be open"
        # No sell-to-open was placed: the old call is still live, and writing a
        # second one against the same 100 shares is the naked-call window
        # `_attempt_stc` exists to close.
        assert not [o for o in client.get_orders() if o["side"] == "sell"]

    def test_the_simulator_fold_counts_it(self):
        """D5. Before this, the record was dropped and the failure vanished."""
        merged = _merge_roll_skips({}, {'btc_timeout_canceled': 1})
        assert merged == {'btc_timeout_canceled': 1}


class TestALadderThatExhaustsAfterTheBtcFills:
    def test_shares_are_left_uncovered_and_the_debit_is_real(self, roller_for):
        """(b) BTC fills, rung 1's STO limit is above the ask so it expires,
        and there is no fallback.

        This is the expensive failure: money left the account to close the old
        call and no new call was written. The roller reports
        `stc_failed_naked_exposure`; the shares are genuinely uncovered in the
        sim exactly as they would be live, and the next day's scan re-covers
        them through the ENTRY gates (never through the roll path, which
        bypasses the delta band, the DTE ceiling and the premium floor).

        A base-mode STO limit IS the bid, so on any well-formed book it is at
        or inside the offer and cannot expire — which is exactly the D1 claim
        that no rung-1 leg expires on a model-built chain. Reaching this branch
        at all therefore needs a book where `bid > ask`, built here on purpose.
        `min_credit` is set so the invariant floor lands exactly ON rung 1's
        price, which suppresses rung 2 (`_rungs` yields it only when the floor
        is strictly below) and leaves a one-rung ladder to exhaust.
        """
        broker = _covered_broker()
        client = _client(broker, [
            _q(OLD, 100.0, bid=2.00, ask=2.20, expiration=EXP_OLD),
            # bid 2.596 rounds UP to a 2.60 limit, which is above BOTH
            # sides of this (inverted) book — the only shape in which a
            # base-mode sell-to-open can fail to fill at all.
            _q(NEW, 102.0, bid=2.596, ask=2.55),
        ])
        roller = roller_for(client, min_credit=0.40)
        with _frozen(), client.order_intent("roll"):
            result = roller.execute_roll(
                _opportunity(btc_limit=2.20, stc_limit=2.60, min_credit=0.40))

        assert result['success'] is False
        assert result['reason'] == 'stc_failed_naked_exposure'
        assert result['reason'] in _POST_PLACEMENT_ROLL_FAILURES

        closes = [e for e in broker.ledger if e.kind == "buy_to_close"]
        assert len(closes) == 1, "the BTC leg must have filled"
        assert closes[0].price == pytest.approx(2.20), "at the ask"
        assert broker.uncovered_shares("XYZ") == 100, (
            "the shares are uncovered — the sim must not paper over it")
        # `failed_roll_btc_debit` is exactly this, post-fee.
        assert -closes[0].cash_delta == pytest.approx(
            2.20 * 100 + broker.fees_per_contract)


class TestRungTwoIsReachable:
    def test_a_better_than_limit_btc_fill_opens_the_floor_rung(self, roller_for):
        """(c) `_rungs` yields rung 2 at `round(btc_fill + min_credit, 2)` only
        when the BTC filled BETTER than its limit — otherwise the floor is at or
        above rung 1 and the ladder has one rung.

        Here the BTC limit is 2.25 and the ask is 2.20, so the fill is 2.20
        (book-capped) — five cents better than the limit, which is what opens
        the floor rung at `round(2.20 + 0.05, 2) = 2.25`. Rung 1 at the bid
        expires against a deliberately inverted book (see the test above for
        why that is the only way to expire a base-mode STO), and rung 2 gets
        filled. Before FC-116 this rung was unreachable at all: rung 1 always
        filled, which is what the `fallback_strike_attempts` refusal used to
        say.
        """
        broker = _covered_broker()
        client = _client(broker, [
            _q(OLD, 100.0, bid=2.00, ask=2.20, expiration=EXP_OLD),
            _q(NEW, 102.0, bid=2.596, ask=2.55),
        ])
        roller = roller_for(client)
        with _frozen(), client.order_intent("roll"):
            result = roller.execute_roll(
                _opportunity(btc_limit=2.25, stc_limit=2.60))

        assert result['success'] is True, result
        closes = [e for e in broker.ledger if e.kind == "buy_to_close"]
        assert closes[-1].price == pytest.approx(2.20), (
            "the BTC is book-capped at the ask, BETTER than its 2.25 limit — "
            "that is what makes the floor rung cheaper than rung 1")

        sells = [o for o in client.get_orders() if o["side"] == "sell"]
        assert len(sells) == 2, (
            f"rung 2 was never placed: {[(o['limit_price'], o['status']) for o in sells]}")
        assert sells[0]["status"] == "expired"
        assert sells[0]["limit_price"] == pytest.approx(2.60)
        assert sells[1]["status"] == "filled"
        assert sells[1]["limit_price"] == pytest.approx(2.25), (
            "rung 2 is priced at the invariant FLOOR, btc_fill + min_credit")

        opens = [e for e in broker.ledger if e.kind == "sell_call_open"
                 and e.symbol == NEW]
        assert len(opens) == 1, "exactly one STO may ever fill"
        # The invariant holds on the ACTUAL fills, which is the whole point of
        # re-pricing rung 2 off `btc_filled_price` rather than off the limit.
        assert opens[0].price - closes[-1].price >= 0.05 - 1e-9


class TestCreditGoneIsCountedExactlyOnce:
    def test_it_is_excluded_from_the_post_placement_fold(self, roller_for):
        """(d) The double-count this allowlist exists to prevent.

        `credit_gone_at_execution` goes out as a `call_roll_skipped` event
        BEFORE `execute_roll` returns it, so `RejectionTally` already has it.
        Folding the return reason as well would report two skips for one
        decision — and it is the single most common roll skip there is, so the
        error would be large and would look like roller activity.
        """
        broker = _covered_broker()
        client = _client(broker, [
            _q(OLD, 100.0, bid=2.00, ask=2.20, expiration=EXP_OLD),
            # The re-checked STO limit (the bid, 2.10) minus the BTC limit
            # (2.20) is NEGATIVE: the credit is gone.
            _q(NEW, 102.0, bid=2.10, ask=2.30),
        ])
        roller = roller_for(client)
        with _frozen(), client.order_intent("roll"):
            result = roller.execute_roll(
                _opportunity(btc_limit=2.20, stc_limit=2.60))

        assert result['reason'] == 'credit_gone_at_execution'
        assert result['reason'] not in _POST_PLACEMENT_ROLL_FAILURES
        assert result['reason'] in _ALREADY_TALLIED_ROLL_FAILURES
        assert not broker.ledger[len(_covered_broker().ledger):], (
            "no order should have been placed at all")

        # The tally has it once; the fold adds nothing.
        merged = _merge_roll_skips({'credit_gone_at_execution': 1}, {})
        assert merged == {'credit_gone_at_execution': 1}

    def test_dry_run_is_named_in_the_allowlist_rather_than_forgotten(self):
        """It never occurs in a replay, and if it ever did it would be a
        CONFIGURATION mistake, not a roll outcome. Excluded explicitly so a
        future reader does not 'fix' the omission."""
        assert 'dry_run' in _ALREADY_TALLIED_ROLL_FAILURES
        assert 'dry_run' not in _POST_PLACEMENT_ROLL_FAILURES


class TestTheAllowlistIsTheRollersOwnVocabulary:
    def test_every_reason_is_a_string_the_roller_actually_returns(self):
        """No aliases. The FC text said `btc_timeout` and `stc_timeout`; the
        roller says `btc_timeout_canceled` and `stc_failed_naked_exposure`, and
        a `roll_skips` key nothing in the codebase emits is a key no query will
        ever match.
        """
        source = (
            __import__("pathlib").Path("src/strategy/call_roller.py").read_text()
        )
        for reason in _POST_PLACEMENT_ROLL_FAILURES:
            # The two `*_disposition_unknown` reasons are built as
            # f'{leg}_disposition_unknown', so the literal in the source is the
            # SUFFIX. Matching on that still catches a rename of the suffix,
            # which is the drift that matters.
            needle = (reason.split("_", 1)[1]
                      if reason.endswith("_disposition_unknown") else reason)
            assert f"'{needle}'" in source or f"{needle}'" in source, reason

    def test_an_unclassified_reason_is_not_counted(self, caplog):
        """A future roller reason must be CLASSIFIED, not silently
        double-counted (if it is already a `call_roll_skipped`) or silently
        dropped (if it is not)."""
        from src.backtesting.engine.simulator import Simulator

        sim = object.__new__(Simulator)
        sim._roll_failures = {}
        sim._count_roll_failure("some_future_reason", DAY)
        assert sim._roll_failures == {}

    def test_the_merge_is_ordered_count_desc_then_name_asc(self):
        """The same rule `RejectionTally.roll_skip_summary` uses, so the merged
        dict is as deterministic as the half it came from."""
        merged = _merge_roll_skips(
            {'no_credit_candidate': 3, 'not_itm_enough': 7},
            {'btc_timeout_canceled': 7, 'btc_rejected': 1},
        )
        assert list(merged) == [
            'btc_timeout_canceled', 'not_itm_enough', 'no_credit_candidate',
            'btc_rejected',
        ]
