"""BacktestAlpacaClient (FC-032 Phase 3).

The adapter is where a replay can silently lie: serve tomorrow's price, quietly
reach for production, or present a chain more permissive than the live one. Each
of those gets a test here.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from src.backtesting.data.chain_builder import ChainQuote, ChainSnapshot
from src.backtesting.data.provider import StockBar
from src.backtesting.engine.alpaca_adapter import (
    BacktestAlpacaClient,
    UnsupportedBacktestCall,
)
from src.backtesting.engine.broker import BacktestBroker
from src.strategy.limit_pricing import refresh_quote, sell_limit_price
from src.utils import clock

D1 = date(2024, 6, 3)
D2 = date(2024, 6, 4)
EXP = date(2024, 6, 7)
PUT = "XYZ240607P00090000"
CALL = "XYZ240607C00110000"


def _quote(symbol, opt_type, strike, *, as_of, mark, bid, ask, delta, spot=100.0, vol=25):
    return ChainQuote(
        symbol=symbol, underlying="XYZ", as_of=as_of, expiration=EXP, strike=strike,
        option_type=opt_type, dte=(EXP - as_of).days, underlying_price=spot,
        mark=mark, bid=bid, ask=ask, implied_volatility=0.30, delta=delta, volume=vol,
    )


def _snapshot(as_of, spot=100.0, *, puts=None, calls=None):
    return ChainSnapshot("XYZ", as_of, spot, puts or [], calls or [])


def _bars(*specs):
    return [
        StockBar(symbol="XYZ", bar_date=d, open=c, high=c, low=c, close=c, volume=1_000_000)
        for d, c in specs
    ]


@pytest.fixture
def setup():
    broker = BacktestBroker(starting_cash=50_000.0)
    put_q = _quote(PUT, "put", 90.0, as_of=D1, mark=1.00, bid=0.90, ask=1.10, delta=-0.15)
    call_q = _quote(CALL, "call", 110.0, as_of=D1, mark=0.80, bid=0.70, ask=0.90, delta=0.18)
    chains = {"XYZ": {
        D1: _snapshot(D1, 100.0, puts=[put_q], calls=[call_q]),
        D2: _snapshot(D2, 104.0,
                      puts=[_quote(PUT, "put", 90.0, as_of=D2, mark=0.40, bid=0.35, ask=0.45,
                                   delta=-0.08, spot=104.0)]),
    }}
    stock = {"XYZ": _bars((D1, 100.0), (D2, 104.0), (date(2024, 6, 5), 108.0))}
    client = BacktestAlpacaClient(broker, chains=chains, stock_bars=stock)
    return broker, client


def _at(day: date):
    return clock.frozen(datetime.combine(day, time(16, 0)))


class TestNoLookahead:
    def test_stock_bars_never_return_a_future_bar(self, setup):
        _, client = setup
        with _at(D1):
            df = client.get_stock_bars("XYZ", days=30)
        assert list(df.index.date) == [D1]
        assert df["close"].iloc[-1] == 100.0

    def test_stock_bars_grow_as_the_simulation_advances(self, setup):
        _, client = setup
        with _at(D1):
            assert len(client.get_stock_bars("XYZ", days=30)) == 1
        with _at(D2):
            df = client.get_stock_bars("XYZ", days=30)
        assert list(df.index.date) == [D1, D2]

    def test_stock_bars_index_is_tz_aware_utc_like_live(self, setup):
        """Index must match live's dtype/stamp so dates derive identically.

        Live returns datetime64[ns, UTC] stamped 04:00 (midnight ET). The
        property that matters is that each bar's index date equals its trading
        date under both the 04:00 and 05:00 stamps — established when the
        FC-036 gap gate selected the previous close by calendar date
        (``idx.date()``), and still required by every date-based consumer of
        this index after FC-069 deleted that gate.
        """
        import pandas as pd

        _, client = setup
        with _at(D2):
            df = client.get_stock_bars("XYZ", days=30)
        assert str(df.index.dtype) == "datetime64[ns, UTC]"
        assert df.index[0].hour == 4
        # Date-based selection must exclude D2 itself.
        df_dates = pd.Series([idx.date() for idx in df.index], index=df.index)
        assert list(df.loc[df_dates < D2].index.date) == [D1]

    def test_quote_is_the_simulated_days_close(self, setup):
        _, client = setup
        with _at(D1):
            assert client.get_stock_quote("XYZ")["bid"] == 100.0
        with _at(D2):
            assert client.get_stock_quote("XYZ")["bid"] == 104.0

    def test_chain_is_the_simulated_days_chain(self, setup):
        _, client = setup
        with _at(D1):
            syms = {o["symbol"] for o in client.get_options_chain("XYZ")}
        assert syms == {PUT, CALL}
        with _at(D2):
            day2 = client.get_options_chain("XYZ")
        assert {o["symbol"] for o in day2} == {PUT}  # the call did not trade on D2
        assert day2[0]["last_price"] == 0.40


class TestFailsLoud:
    def test_unimplemented_attribute_raises(self, setup):
        _, client = setup
        with pytest.raises(UnsupportedBacktestCall, match="cancel_order"):
            client.cancel_order

    def test_raw_trading_client_reachthrough_raises(self, setup):
        """wheel_engine.py reaches for .trading_client; it must not silently work."""
        _, client = setup
        with pytest.raises(UnsupportedBacktestCall, match="trading_client"):
            client.trading_client

    def test_using_the_adapter_without_a_frozen_clock_raises(self, setup):
        _, client = setup
        assert not clock.is_frozen()
        with pytest.raises(UnsupportedBacktestCall, match="frozen clock"):
            client.get_stock_quote("XYZ")


class TestMirrorsLiveChainShape:
    def test_open_interest_is_zero_like_live(self, setup):
        """Live hardcodes 0, making its liquidity gate 'volume must be non-zero'.

        Fabricating OI would make the backtest more permissive than production.
        """
        _, client = setup
        with _at(D1):
            chain = client.get_options_chain("XYZ")
        assert all(o["open_interest"] == 0 for o in chain)
        assert all(o["volume"] > 0 for o in chain)

    def test_contracts_with_unsolved_iv_are_dropped_not_passed_as_none(self, setup):
        """Live does abs(opt.get('delta', 0)); a None delta would TypeError."""
        broker = BacktestBroker(starting_cash=50_000.0)
        bad = _quote(PUT, "put", 90.0, as_of=D1, mark=1.0, bid=0.9, ask=1.1, delta=None)
        client = BacktestAlpacaClient(
            broker, chains={"XYZ": {D1: _snapshot(D1, puts=[bad])}},
            stock_bars={"XYZ": _bars((D1, 100.0))},
        )
        with _at(D1):
            chain = client.get_options_chain("XYZ")
        assert chain == []
        # And the value live would have crashed on is never produced.
        assert all(o["delta"] is not None for o in chain)

    def test_greeks_live_code_ignores_are_zero(self, setup):
        _, client = setup
        with _at(D1):
            o = client.get_options_chain("XYZ")[0]
        assert (o["gamma"], o["theta"], o["vega"]) == (0.0, 0.0, 0.0)
        assert o["delta"] != 0.0  # the one greek that is actually consumed


class TestOrdersMoveTheBroker:
    def test_sell_put_reserves_collateral_and_credits_premium(self, setup):
        broker, client = setup
        with _at(D1):
            res = client.place_option_order(PUT, 1, "sell", limit_price=1.00)
        assert res["success"] is True
        # mark 1.00, bid 0.90, haircut 0.25 -> 1.00 - 0.25*0.10 = 0.975
        assert broker.options[PUT].entry_price == pytest.approx(0.975)
        assert broker.reserved_collateral == pytest.approx(9_000.0)
        assert broker.available_cash == pytest.approx(50_000 + 97.5 - 0.04 - 9_000)

    def test_sell_put_rejected_when_collateral_unavailable(self):
        broker = BacktestBroker(starting_cash=1_000.0)
        q = _quote(PUT, "put", 90.0, as_of=D1, mark=1.0, bid=0.9, ask=1.1, delta=-0.15)
        client = BacktestAlpacaClient(
            broker, chains={"XYZ": {D1: _snapshot(D1, puts=[q])}},
            stock_bars={"XYZ": _bars((D1, 100.0))},
        )
        with _at(D1):
            res = client.place_option_order(PUT, 1, "sell", limit_price=1.0)
        assert res["success"] is False
        assert res["error_type"] == "insufficient_collateral"
        assert broker.options == {}

    def test_order_for_a_contract_that_did_not_trade_is_rejected(self, setup):
        _, client = setup
        with _at(D2):  # the call has no D2 bar
            res = client.place_option_order(CALL, 1, "sell", limit_price=0.8)
        assert res["success"] is False and res["error_type"] == "no_quote"

    def test_filled_order_is_retrievable(self, setup):
        _, client = setup
        with _at(D1):
            res = client.place_option_order(PUT, 1, "sell", limit_price=1.0)
            got = client.get_order_by_id(res["order_id"])
            assert got["status"] == "filled"
            assert got["filled_avg_price"] == pytest.approx(0.975)
            assert len(client.get_orders(status="filled")) == 1


class TestPositionsAndAccount:
    def test_short_put_reports_negative_qty_and_us_option_class(self, setup):
        _, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
            positions = client.get_positions()
        opt = [p for p in positions if p["asset_class"] == "us_option"][0]
        assert opt["qty"] == -1.0 and opt["side"] == "short"
        assert opt["market_value"] == pytest.approx(-100.0)  # mark 1.00 * 100

    def test_short_put_gains_as_premium_decays(self, setup):
        _, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
        with _at(D2):  # mark falls 0.975 -> 0.40
            opt = [p for p in client.get_positions() if p["asset_class"] == "us_option"][0]
        assert opt["unrealized_pl"] == pytest.approx((0.975 - 0.40) * 100)

    def test_buying_power_excludes_reserved_collateral_no_margin(self, setup):
        broker, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
            acct = client.get_account()
        assert acct["buying_power"] == pytest.approx(broker.available_cash)
        assert acct["buying_power"] < acct["cash"]  # collateral is locked away

    def test_assigned_shares_appear_as_long_equity(self, setup):
        broker, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
        # Underlying closes below the strike at expiry -> assigned.
        broker.settle_expirations(EXP, {"XYZ": 85.0})
        with _at(EXP):
            stock = [p for p in client.get_positions() if p["asset_class"] == "us_equity"]
        assert len(stock) == 1
        assert stock[0]["symbol"] == "XYZ" and stock[0]["qty"] == 100.0

    def test_assigned_shares_carry_avg_entry_price(self, setup):
        """FC-065 contract test: the simulated broker must emit the field the
        covered-call floor reads, or a replay fails closed on every symbol
        while production does not.

        FC-068 closed the interim gap this test used to record. The basis is
        now **premium-netted** — ``strike − the assigning put's fill premium``
        — which is Alpaca's ``avg_entry_price`` semantics, verified to the
        penny on all four live lots (FC-065 Phase 1). Booking it at the bare
        strike left the simulated floor one premium ABOVE production's; on
        IWM's $1 strike grid that is a full rung.
        """
        broker, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
        broker.settle_expirations(EXP, {"XYZ": 85.0})
        with _at(EXP):
            stock = [p for p in client.get_positions()
                     if p["asset_class"] == "us_equity"][0]

        assert "avg_entry_price" in stock, (
            "the covered-call floor field is missing from the simulated broker")
        # mark 1.00, bid 0.90, haircut 0.25 -> fill 0.975; 90.00 - 0.975.
        assert stock["avg_entry_price"] == pytest.approx(89.025)
        assert stock["avg_entry_price"] == pytest.approx(
            stock["cost_basis"] / stock["qty"])
        # And the cash ledger still moved at the strike — the two numbers are
        # deliberately different (FC-068 §7).
        event = [e for e in broker.ledger if e.kind == "put_assignment"][0]
        assert event.price == pytest.approx(90.0)
        assert event.cash_delta == pytest.approx(-9000.0)

    def test_a_multi_lot_stock_position_reports_the_weighted_average(self, setup):
        """Alpaca's own semantic — and what FC-064's mixed-lot case needed."""
        broker, client = setup
        broker._add_stock("XYZ", 100, 90.0, D1)
        broker._add_stock("XYZ", 100, 110.0, D2)
        with _at(D2):
            stock = [p for p in client.get_positions()
                     if p["asset_class"] == "us_equity"][0]

        assert stock["qty"] == 200.0
        assert stock["avg_entry_price"] == pytest.approx(100.0)

    def test_a_short_option_carries_its_entry_premium(self, setup):
        _, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
            opt = [p for p in client.get_positions()
                   if p["asset_class"] == "us_option"][0]

        # Positive, as Alpaca reports it for a short — unlike cost_basis.
        assert opt["avg_entry_price"] == pytest.approx(0.975)


class TestActivities:
    def test_assignment_surfaces_as_an_opasn_activity(self, setup):
        broker, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
        broker.settle_expirations(EXP, {"XYZ": 85.0})
        with _at(EXP):
            acts = client.get_account_activities("OPASN,OPEXP", after="2024-06-01")
        assert len(acts) == 1
        assert acts[0]["activity_type"] == "OPASN"
        assert acts[0]["symbol"] == PUT

    def test_worthless_expiry_surfaces_as_opexp(self, setup):
        broker, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
        broker.settle_expirations(EXP, {"XYZ": 120.0})  # far OTM -> expires
        with _at(EXP):
            acts = client.get_account_activities("OPASN,OPEXP", after="2024-06-01")
        assert [a["activity_type"] for a in acts] == ["OPEXP"]

    def test_activity_ids_are_stable_across_calls(self, setup):
        """wheel_engine dedupes by id; unstable ids would double-count assignments."""
        broker, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
        broker.settle_expirations(EXP, {"XYZ": 85.0})
        with _at(EXP):
            first = client.get_account_activities("OPASN", after="2024-06-01")
            second = client.get_account_activities("OPASN", after="2024-06-01")
        assert [a["id"] for a in first] == [a["id"] for a in second]

    def test_activities_filter_by_type_and_after_date(self, setup):
        broker, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
        broker.settle_expirations(EXP, {"XYZ": 85.0})
        with _at(EXP):
            assert client.get_account_activities("OPEXP", after="2024-06-01") == []
            after_cutoff = client.get_account_activities(
                "OPASN", after=(EXP + timedelta(days=1)).isoformat()
            )
        assert after_cutoff == []

    def test_ledger_events_after_the_simulated_date_are_not_visible(self, setup):
        """Even our own ledger must not leak the future into a decision."""
        broker, client = setup
        with _at(D1):
            client.place_option_order(PUT, 1, "sell", limit_price=1.0)
        broker.settle_expirations(EXP, {"XYZ": 85.0})
        with _at(D2):  # D2 < EXP: the assignment has not happened yet, in sim time
            assert client.get_account_activities("OPASN", after="2024-06-01") == []


class TestExecuteTimeRequoteStaysOfflineFC072:
    """FC-072 rev 2 gave both sellers an execute-time re-quote. In a replay
    that re-quote must never reach the network, and must never move the result.

    It does not, structurally, and the mechanism is worth stating because the
    plan proposed a different one. ``BacktestAlpacaClient.get_option_quote``
    already existed and reads the **frozen daily chain snapshot** — the same
    object the opportunity's own bid/ask came from. So in a replay the "fresh"
    quote IS the scan-time quote: offline, deterministic, and numerically
    identical.

    The plan asked instead for the adapter's ``get_option_quote`` to return
    something unusable so replay would take the blob fallback. That would have
    been actively wrong here: ``CallRoller`` calls ``get_option_quote`` three
    times and the simulator runs the roll cycle every simulated day, so
    starving it would change ``rolls_executed`` — the opposite of leaving the
    replay unchanged. The offline-by-construction property is what the plan
    actually wanted; see the PR for the write-up.
    """

    def test_the_adapter_serves_the_frozen_snapshot_not_a_live_quote(self, setup):
        _, client = setup
        with _at(D1):
            quote = client.get_option_quote(PUT)
        assert (quote["bid"], quote["ask"]) == (0.90, 1.10)

    def test_the_requote_resolves_through_the_adapter_and_is_usable(self, setup):
        """`refresh_quote` handed the adapter returns source="live" — "live"
        means "the client's current book", and the backtest client's current
        book is the day's snapshot."""
        _, client = setup
        opportunity = {"symbol": "XYZ", "bid": 0.90, "ask": 1.10, "premium": 1.00}
        with _at(D1):
            quote = refresh_quote(client, PUT, opportunity)
        assert quote.source == "live"
        assert (quote.bid, quote.ask) == (0.90, 1.10)

    def test_the_requoted_book_is_identical_to_the_opportunitys_own(self, setup):
        """THE determinism argument: re-quoting cannot change a replay's price
        because it returns the same numbers the opportunity carries."""
        _, client = setup
        opportunity = {"symbol": "XYZ", "bid": 0.90, "ask": 1.10, "premium": 1.00}
        with _at(D1):
            quote = refresh_quote(client, PUT, opportunity)
        assert (quote.bid, quote.ask) == (opportunity["bid"], opportunity["ask"])

    def test_a_contract_absent_from_the_day_falls_back_rather_than_reaching_out(
            self, setup):
        """D2's snapshot has no calls. The adapter returns {} — it does not go
        looking for the contract anywhere else."""
        _, client = setup
        opportunity = {"symbol": "XYZ", "bid": 0.70, "ask": 0.90, "premium": 0.80}
        with _at(D2):
            quote = refresh_quote(client, CALL, opportunity)
        assert quote.source == "blob"
        assert (quote.bid, quote.ask) == (0.70, 0.90)

    def test_reaching_for_a_live_client_raises_rather_than_reaching_out(self, setup):
        """The structural guarantee behind "replay never issues a live quote":
        the adapter's ``__getattr__`` refuses everything it does not simulate,
        loudly. `get_option_quote` IS simulated, so the re-quote resolves
        offline; anything a future change reached for instead would blow up
        here rather than quietly opening a socket."""
        _, client = setup
        for attr in ("trading_client", "option_data_client", "stock_data_client"):
            with pytest.raises(UnsupportedBacktestCall):
                getattr(client, attr)
        assert callable(client.get_option_quote)

    def test_the_requote_is_priced_the_same_from_either_source(self, setup):
        """Belt and braces on the byte-identity claim: price the same order off
        the re-quote and off the blob and get the same limit."""
        _, client = setup
        opportunity = {"symbol": "XYZ", "bid": 0.90, "ask": 1.10, "premium": 1.00}
        with _at(D1):
            quote = refresh_quote(client, PUT, opportunity)
        live_priced = sell_limit_price(quote.bid, quote.ask, 1.00, 0.10, "XYZ")
        blob_priced = sell_limit_price(opportunity["bid"], opportunity["ask"],
                                       1.00, 0.10, "XYZ")
        assert live_priced.limit_price == blob_priced.limit_price
