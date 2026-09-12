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
rung-1 roll leg can expire: the roller prices its limit off the SAME snapshot
the adapter then fills it against, and the adapter quantises that book to cents
first (T1), so a base-mode BTC limit is exactly `ask_c` and a base-mode STO
limit exactly `bid_c` — both marketable — while an imminence limit (`mid +/-
0.05` against a half-spread floored at 0.02) lies strictly inside the spread.
The expiry branch, the post-placement terminals and rungs >= 2 are therefore
unreachable there, and the books below carry limits the roller could not have
derived from them. That is a fact about the model, not evidence that live rolls
always fill, and it is why these paths need a book built by hand to exercise
them at all.
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


class TestAnStoLimitOutsideTheBookExpires:
    """(b) The expensive failure: the BTC fills, no new call is written.

    Live, this is `stc_failed_naked_exposure` — money left the account to close
    the old call and the shares are genuinely uncovered until the next day's
    scan re-covers them through the ENTRY gates (never through the roll path,
    which bypasses the delta band, the DTE ceiling and the premium floor).

    It is exercised at the ADAPTER here rather than through `execute_roll`,
    because the class below shows the roller cannot produce such a limit on a
    single-snapshot chain. The branch still has to be right: it is the one the
    live roller's disposition code is written against.
    """

    def test_the_order_expires_and_moves_no_money(self):
        broker = _covered_broker()
        # The old call is closed first, exactly as a roll's BTC leg would.
        client = _client(broker, [
            _q(OLD, 100.0, bid=2.00, ask=2.20, expiration=EXP_OLD),
            _q(NEW, 102.0, bid=2.40, ask=2.45),
        ])
        with _frozen(), client.order_intent("roll"):
            btc = client.place_option_order(OLD, 1, "buy", limit_price=2.20)
            assert btc["status"] == "filled"
            ledger_after_btc = len(broker.ledger)
            # 2.60 is above BOTH quantised sides of the candidate's book.
            sto = client.place_option_order(NEW, 1, "sell", limit_price=2.60)

        assert sto["success"] is True, (
            "live Alpaca ACCEPTS this order — the roller's own terminal "
            "dispositions are what must run, not a placement rejection")
        assert sto["status"] == "expired"
        assert client.get_order_by_id(sto["order_id"])["filled_qty"] == 0
        assert len(broker.ledger) == ledger_after_btc, "an unfilled leg moved money"
        assert broker.uncovered_shares("XYZ") == 100, (
            "the shares are uncovered — the sim must not paper over it")
        # `failed_roll_btc_debit` is exactly the BTC leg's cash, post-fee.
        closes = [e for e in broker.ledger if e.kind == "buy_to_close"]
        assert closes[-1].price == pytest.approx(2.20), "at the quantised ask"
        assert -closes[-1].cash_delta == pytest.approx(
            2.20 * 100 + broker.fees_per_contract)

    def test_the_reason_is_in_the_post_placement_allowlist(self):
        """It is a LIVE outcome even though the replay cannot reach it, so it
        must still be folded into `roll_skips` rather than dropped."""
        assert 'stc_failed_naked_exposure' in _POST_PLACEMENT_ROLL_FAILURES


class TestTheLadderHasExactlyOneReachableRung:
    """(c) Why `rolling.fallback_strike_attempts` stays refused (E2).

    The claim is STRUCTURAL, not a measurement: the roller re-derives rung 1's
    sell-to-open limit from the SAME quote the adapter then fills it against
    (`_stc_limit_from_quote` -> `round(bid, 2)`), and the adapter quantises the
    book to cents before comparing (T1), so the limit IS `bid_c` and is always
    marketable. A later rung is asked for only when an earlier one fails to
    fill, so rung >= 2 is never placed and the knob cannot move a replay.

    The book below is INVERTED (`bid > ask`) on purpose: even that does not
    expire the leg, which is the strongest form of the claim.
    """

    def test_rung_one_fills_even_on_an_inverted_book(self, roller_for):
        broker = _covered_broker()
        client = _client(broker, [
            _q(OLD, 100.0, bid=2.00, ask=2.20, expiration=EXP_OLD),
            _q(NEW, 102.0, bid=2.596, ask=2.55),
        ])
        roller = roller_for(client)
        with _frozen(), client.order_intent("roll"):
            # A deliberately STALE 2.60 limit from selection: the roller
            # discards it and re-prices off the quote before placing.
            result = roller.execute_roll(
                _opportunity(btc_limit=2.25, stc_limit=2.60))

        assert result['success'] is True, result
        sells = [o for o in client.get_orders() if o["side"] == "sell"]
        assert len(sells) == 1, (
            "rung 2 was placed, so rung 1 did not fill — the refusal reason "
            f"for fallback_strike_attempts is no longer true: "
            f"{[(o['limit_price'], o['status']) for o in sells]}")
        assert sells[0]["status"] == "filled"
        assert sells[0]["limit_price"] == pytest.approx(2.60), (
            "round(bid=2.596, 2) — the re-derived limit, not the stale 2.60 "
            "that happens to equal it")
        assert sells[0]["filled_avg_price"] == pytest.approx(2.60), (
            "a sell limit at the quantised bid is marketable and fills there")

        closes = [e for e in broker.ledger if e.kind == "buy_to_close"]
        assert closes[-1].price == pytest.approx(2.20), (
            "the BTC is book-capped at the quantised ask, BETTER than its "
            "2.25 limit — the book caps in the order's favour, never against")
        opens = [e for e in broker.ledger if e.kind == "sell_call_open"
                 and e.symbol == NEW]
        assert len(opens) == 1, "exactly one STO may ever fill"
        assert opens[0].price - closes[-1].price >= 0.05 - 1e-9

    def test_a_base_mode_sto_limit_is_the_quantised_bid_by_construction(self):
        """The one line the whole refusal rests on, pinned directly."""
        for bid, ask in ((2.4815, 2.55), (0.031, 0.049), (12.3349, 12.99)):
            limit = CallRoller._stc_limit_from_quote(bid, ask, False)
            assert limit == round(bid, 2), (bid, ask)
            assert limit <= round(bid, 2), "marketable against the quantised bid"


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
