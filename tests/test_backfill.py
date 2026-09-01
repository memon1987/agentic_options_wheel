"""FC-096 Phase A — the data backfill (`src/backtesting/data/backfill.py`).

What is actually at risk here, in the order the plan's reviews put it:

* **The window rule.** `ChainStore` is coverage-monotone: a rebuild that is
  narrower on any bound than the object it would replace is REFUSED. The live
  lake already contains days whose strike window was pushed out by a position
  anchor or an FC-091 merge, so a backfill that rebuilt at a spot-centred window
  would be refused on exactly those days — for ever, every week, silently
  (`chain_lake_overwrite_skipped`), which is the observed SPY/IWM/PFE
  `rejected=231 skipped=231` pattern with a scheduler attached. `TestWindowRule`
  and `TestChurn` are that property.

* **Model parity.** The chains this writes are keyed by a model fingerprint. If
  the backfill constructs its `ChainBuilder` even slightly differently from the
  sweep runner's — a dividend map being the obvious "improvement" — every day it
  writes is invisible to every sweep, and nothing anywhere raises.
  `TestModelParity` compares the fingerprints AND the two call sites'
  source-level shapes.

* **Not dying.** One symbol's split, one day's bad bar, one vendor error must
  not cost the other thirteen symbols their refresh. `TestFailureIsolation`.

Nothing here touches GCS or Alpaca. The lake double is `FakeLake` from
`tests/test_chain_store_lake.py` — a subclass of the real `ChainLake` with only
its three I/O methods overridden, so the store's real counters, real coverage
guard and real merge path all run.
"""

import ast
import inspect
import textwrap
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.backtesting.data import backfill as bf
from src.backtesting.data.chain_builder import (
    STRIKE_WINDOW_PCT,
    UNIVERSE_DTE_BUFFER,
    ChainBuilder,
)
from src.backtesting.data.chain_store import (
    MAX_CONSECUTIVE_LAKE_ERRORS,
    ChainStore,
)
from src.backtesting.data.provider import OptionBar, OptionContract, StockBar
from src.backtesting.scenarios.overrides import MAX_SWEEPABLE_DTE
from tests.test_chain_store_lake import FakeLake


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #
class LadderProvider:
    """A provider with a dense strike ladder and one expiry per week.

    Deliberately generous: the ladder spans 0.3x-2.0x spot so that what the
    chain ends up containing is decided by the *requested window*, not by what
    the double happens to hold. A stingy ladder would let a too-narrow window
    pass unnoticed, which is the exact failure this module exists to prevent.
    """

    def __init__(self, prices, *, symbols=("XYZ",), strike_step=5.0):
        # prices: {date: close}, shared by every symbol unless overridden
        self.prices = dict(prices)
        self.per_symbol = {}
        self.symbols = list(symbols)
        self.strike_step = strike_step
        self.stock_bar_calls = []
        self.universe_calls = []

    def closes_for(self, symbol):
        return self.per_symbol.get(symbol, self.prices)

    def get_stock_bars(self, symbol, start, end):
        self.stock_bar_calls.append((symbol, start, end))
        return [
            StockBar(symbol, d, px, px, px, px, volume=1_000_000)
            for d, px in sorted(self.closes_for(symbol).items())
            if start <= d <= end
        ]

    def _strikes(self, price):
        lo = max(self.strike_step, price * 0.3)
        hi = price * 2.0
        out, k = [], self.strike_step * round(lo / self.strike_step)
        while k <= hi:
            out.append(round(k, 2))
            k += self.strike_step
        return out

    def get_contract_universe(self, underlying, as_of, max_dte, *,
                              strike_gte=None, strike_lte=None):
        self.universe_calls.append(
            (underlying, as_of, max_dte, strike_gte, strike_lte)
        )
        price = self.closes_for(underlying).get(as_of, 100.0)
        out = []
        for offset in (7, 14, 21, 28):
            if offset > max_dte:
                continue
            expiration = as_of + timedelta(days=offset)
            for strike in self._strikes(price):
                if strike_gte is not None and strike < strike_gte:
                    continue
                if strike_lte is not None and strike > strike_lte:
                    continue
                out.append(OptionContract(
                    symbol=_occ(underlying, expiration, "put", strike),
                    underlying=underlying, expiration=expiration,
                    strike=strike, option_type="put",
                ))
        return out

    def get_option_bars(self, symbols, start, end):
        return {
            s: [OptionBar(s, start, 1.0, 1.2, 0.9, 1.0, volume=10, trade_count=3)]
            for s in symbols
        }


def _occ(underlying, exp, opt, strike):
    cp = "P" if opt == "put" else "C"
    return f"{underlying}{exp:%y%m%d}{cp}{int(strike * 1000):08d}"


def _weekdays(first: date, count: int):
    out, day = [], first
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


@pytest.fixture
def days():
    """Ten settled weekday sessions, all safely in the past."""
    return _weekdays(date.today() - timedelta(days=40), 10)


@pytest.fixture
def prices(days):
    return {d: 100.0 + i for i, d in enumerate(days)}


@pytest.fixture
def provider(prices):
    return LadderProvider(prices)


@pytest.fixture
def captured_backfill_events():
    """Every structlog event the backfill emits during one test."""
    import structlog

    seen = []

    def capture(_logger, _name, event_dict):
        seen.append(dict(event_dict))
        return event_dict

    previous = structlog.get_config()
    structlog.configure(
        processors=[capture] + list(previous.get("processors", [])))
    try:
        yield seen
    finally:
        structlog.configure(**previous)


def _store(tmp_path, lake=None, name="cache"):
    return ChainStore(str(tmp_path / name), lake=lake)


def _run(provider, store, symbols, start, end):
    return bf.run_backfill(
        None, symbols, start, end, chain_store=store, bar_provider=provider,
    )


def _stored(store, symbol, day):
    return store.stored_window(symbol, day)


# --------------------------------------------------------------------------- #
# The window rule
# --------------------------------------------------------------------------- #
class TestWindowRule:
    def test_fresh_window_is_forty_percent_not_the_live_twenty_five(self):
        """The 7-DTE constant is not reused, and is not edited either."""
        assert bf.BACKFILL_STRIKE_WINDOW_PCT == 0.40
        assert STRIKE_WINDOW_PCT == 0.25, (
            "STRIKE_WINDOW_PCT is the LIVE 7-DTE reach's window, shared with "
            "every other caller; the backfill widens through anchors instead"
        )
        assert bf.fresh_window(100.0) == (60.0, 140.0)

    def test_anchors_back_solve_to_exactly_the_requested_window(self):
        """The inversion is checked against the real `strike_window`.

        If `STRIKE_WINDOW_PCT` or `strike_window`'s shape ever changes, this is
        what fails — rather than the backfill quietly fetching a narrower ladder
        than it stamps on the file.
        """
        for price, gte, lte in [
            (100.0, 60.0, 140.0),
            (100.0, 42.0, 140.0),      # stored window wider on the low bound
            (17.5, 7.0, 30.0),         # a cheap symbol
            (925.25, 400.0, 1400.0),   # an expensive one
        ]:
            cost_basis, low_anchor = bf.window_anchors(price, gte, lte)
            got_gte, got_lte = bf.effective_window(price, cost_basis, low_anchor)
            assert got_gte == pytest.approx(gte, abs=1e-9)
            assert got_lte == pytest.approx(lte, abs=1e-9)

    def test_both_anchors_always_bind(self):
        """A back-solved anchor inside spot would be IGNORED by the builder.

        `strike_window` only honours `cost_basis` above spot and `low_anchor`
        below it. At a 40% fresh window both are guaranteed outside — this pins
        that, because an anchor that silently did not bind would produce a
        +/-25% window stamped as a 40% one.
        """
        cost_basis, low_anchor = bf.window_anchors(100.0, 60.0, 140.0)
        assert cost_basis > 100.0
        assert low_anchor < 100.0

    def test_union_widens_on_each_axis_independently(self):
        assert bf.union_window(100.0, None) == (60.0, 140.0)
        assert bf.union_window(100.0, {"strike_gte": 40.0, "strike_lte": 120.0}) == (
            40.0, 140.0)
        assert bf.union_window(100.0, {"strike_gte": 80.0, "strike_lte": 190.0}) == (
            60.0, 190.0)
        assert bf.union_window(100.0, {"strike_gte": 80.0, "strike_lte": 120.0}) == (
            60.0, 140.0)

    def test_union_ignores_bounds_the_stored_file_cannot_prove(self):
        """Unknown provenance contributes nothing — it cannot be covered."""
        assert bf.union_window(100.0, {"strike_gte": None, "strike_lte": None}) == (
            60.0, 140.0)
        assert bf.union_window(100.0, {}) == (60.0, 140.0)

    def test_written_days_are_stamped_at_universe_dte_22(self, tmp_path, provider,
                                                         days, prices):
        store = _store(tmp_path, lake=FakeLake())
        summary = _run(provider, store, ["XYZ"], days[0], days[-1])

        assert bf.BACKFILL_MAX_DTE == MAX_SWEEPABLE_DTE == 21
        assert bf.BACKFILL_UNIVERSE_DTE == 21 + UNIVERSE_DTE_BUFFER == 22
        assert summary.universe_dte == 22
        row = summary.symbols[0]
        assert row.days_checked == len(days)
        assert row.days_written == len(days)
        assert row.days_replaced_wider == 0    # nothing was there before
        assert row.days_skipped == 0
        assert row.errors == 0

        window = _stored(store, "XYZ", days[3])
        assert window["universe_dte"] == 22
        assert window["strike_gte"] == pytest.approx(prices[days[3]] * 0.60)
        assert window["strike_lte"] == pytest.approx(prices[days[3]] * 1.40)

    def test_the_ladder_actually_reaches_21_dte(self, tmp_path, provider, days):
        """Not just the stamp: the file must hold the longer-dated contracts.

        A provenance column claiming reach 22 over a file built with a 7-DTE
        universe would be a cache HIT that is missing rows — a wrong backtest,
        not a slow one, and the exact failure `_merge_windows` refuses
        `dte_mismatch` to avoid.
        """
        store = _store(tmp_path, lake=FakeLake())
        _run(provider, store, ["XYZ"], days[0], days[-1])

        snap = store.get("XYZ", days[3])
        dtes = sorted({q.dte for q in snap.puts})
        assert dtes == [7, 14, 21], dtes


# --------------------------------------------------------------------------- #
# Churn: the blocker the plan's reviews raised
# --------------------------------------------------------------------------- #
class TestChurn:
    """A day whose stored window is wider on ONE bound must heal, not thrash."""

    def _seed_narrow_dte_wide_strikes(self, tmp_path, provider, day, price):
        """Write a reach-8 object whose LOW bound is wider than the fresh one.

        This is the live shape: `universe_dte = 8` (the 7-DTE target plus the
        buffer) with a strike window pushed down by a `low_anchor` from a run
        that was holding a short put. A spot-centred 40% rebuild is wider on the
        high bound and NARROWER on the low one, so neither file covers the
        other and the monotone guard refuses the upload — for ever.
        """
        seed_store = ChainStore(str(tmp_path / "seed"))
        builder = ChainBuilder(provider, store=seed_store)
        # low_anchor at 0.6x spot -> stored gte = 0.45x spot, well below the
        # backfill's 0.60x fresh bound; cost_basis unset -> stored lte = 1.25x.
        builder.build("XYZ", day, 7, underlying_price=price, low_anchor=price * 0.6)
        stored = seed_store.stored_window("XYZ", day)
        assert stored["universe_dte"] == 8
        assert stored["strike_gte"] < price * 0.60
        assert stored["strike_lte"] < price * 1.40
        return seed_store, stored

    def test_a_wider_on_one_bound_object_is_replaced_not_skipped(
            self, tmp_path, provider, days, prices):
        day, price = days[0], None
        price = prices[day]
        seed_store, seeded = self._seed_narrow_dte_wide_strikes(
            tmp_path, provider, day, price)

        lake = FakeLake()
        lake.put_object("XYZ", day, Path(seed_store._path("XYZ", day)).read_bytes())

        # A FRESH local cache: the Job's filesystem is empty every execution, so
        # the object can only arrive from the lake.
        store = _store(tmp_path, lake=lake, name="job-1")
        summary = _run(provider, store, ["XYZ"], day, day)
        row = summary.symbols[0]

        assert row.days_skipped == 0, (
            "the upload was DECLINED — this is the thrash the union rule "
            "exists to prevent"
        )
        assert row.days_written == 1
        assert row.days_replaced_wider == 1
        assert store.summary()["lake_puts"] == 1
        assert store.summary()["lake_skipped"] == 0

        after = ChainStore(str(tmp_path / "read"), lake=lake).stored_window("XYZ", day)
        assert after["universe_dte"] == 22, "the DTE axis must have widened"
        assert after["strike_gte"] == pytest.approx(seeded["strike_gte"]), (
            "the replacement must keep the OLD low bound — that is the union"
        )
        assert after["strike_lte"] == pytest.approx(price * 1.40)

    def test_a_second_run_writes_nothing(self, tmp_path, provider, days, prices):
        """Idempotence, including over the widened day.

        The weekly Job re-walks the same trailing month every Saturday. If a
        covered day were rebuilt anyway, every run would re-fetch a symbol-month
        from Alpaca and re-upload it — the cost the lake exists to avoid, paid
        weekly and invisibly.
        """
        day, price = days[0], prices[days[0]]
        seed_store, _ = self._seed_narrow_dte_wide_strikes(
            tmp_path, provider, day, price)
        lake = FakeLake()
        lake.put_object("XYZ", day, Path(seed_store._path("XYZ", day)).read_bytes())

        first = _run(provider, _store(tmp_path, lake=lake, name="job-1"),
                     ["XYZ"], day, day)
        assert first.symbols[0].days_written == 1

        store2 = _store(tmp_path, lake=lake, name="job-2")
        before_universe_calls = len(provider.universe_calls)
        second = _run(provider, store2, ["XYZ"], day, day)
        row = second.symbols[0]

        assert row.days_checked == 1
        assert row.days_written == 0
        assert row.days_skipped == 0
        assert row.errors == 0
        assert store2.summary()["lake_puts"] == 0
        assert len(provider.universe_calls) == before_universe_calls, (
            "a covered day must not re-reach the provider at all"
        )

    def test_a_second_run_over_a_fresh_day_also_writes_nothing(
            self, tmp_path, provider, days):
        lake = FakeLake()
        _run(provider, _store(tmp_path, lake=lake, name="job-1"),
             ["XYZ"], days[0], days[-1])
        second = _run(provider, _store(tmp_path, lake=lake, name="job-2"),
                      ["XYZ"], days[0], days[-1])
        assert second.symbols[0].days_written == 0
        assert second.symbols[0].days_skipped == 0

    def test_an_object_wider_on_every_axis_is_simply_a_hit(self, tmp_path,
                                                            provider, days, prices):
        """Nothing is rebuilt, nothing is written, nothing is skipped.

        The union rule makes this the common case once the historical widening
        has run: a stored file that already covers the request short-circuits
        both provider calls, which is the whole economics of the weekly run.
        """
        day, price = days[0], prices[days[0]]
        seed_store = ChainStore(str(tmp_path / "seed"))
        ChainBuilder(provider, store=seed_store).build(
            "XYZ", day, 60, underlying_price=price,
            cost_basis=price * 3.0, low_anchor=price * 0.1)

        lake = FakeLake()
        lake.put_object("XYZ", day, Path(seed_store._path("XYZ", day)).read_bytes())
        store = _store(tmp_path, lake=lake, name="job-1")
        before = len(provider.universe_calls)
        row = _run(provider, store, ["XYZ"], day, day).symbols[0]

        assert (row.days_written, row.days_skipped, row.errors) == (0, 0, 0)
        assert len(provider.universe_calls) == before

    def test_a_declined_upload_is_reported_as_skipped(self, tmp_path, provider,
                                                      days, prices):
        """The store may still refuse — and when it does, the summary says so.

        The shape here is real: a file built at a LONGER reach against a
        different close for the same session (a restated bar, or a raw/adjusted
        mix). `get` refuses it on the price, so the day is rebuilt — and the
        rebuild is narrower on the DTE axis, so the mirror refuses the upload.
        Nothing is written, the wider object survives, and the summary must say
        `skipped` rather than counting it as a write.
        """
        day, price = days[0], prices[days[0]]
        seed_store = ChainStore(str(tmp_path / "seed"))
        builder = ChainBuilder(provider, store=seed_store)
        builder.build("XYZ", day, 60, underlying_price=price * 1.01,
                      cost_basis=price * 3.0, low_anchor=price * 0.1)
        wide = seed_store.stored_window("XYZ", day)
        assert wide["universe_dte"] > bf.BACKFILL_UNIVERSE_DTE

        lake = FakeLake()
        lake.put_object("XYZ", day, Path(seed_store._path("XYZ", day)).read_bytes())
        store = _store(tmp_path, lake=lake, name="job-1")
        summary = _run(provider, store, ["XYZ"], day, day)
        row = summary.symbols[0]

        # The union covers the strike axis but NOT the reach, so the rebuild is
        # narrower on DTE and the guard refuses it. Nothing is written and the
        # wider object survives.
        assert row.days_skipped == 1
        assert row.days_written == 0
        assert store.summary()["lake_skipped"] == 1
        survivor = ChainStore(str(tmp_path / "read"), lake=lake).stored_window("XYZ", day)
        assert survivor["universe_dte"] == wide["universe_dte"]


# --------------------------------------------------------------------------- #
# Model parity
# --------------------------------------------------------------------------- #
class TestModelParity:
    def test_fingerprints_match_the_runner_construction(self, provider, tmp_path):
        """Same fingerprint, symbol by symbol — dividend payers included.

        A dividend map is the "improvement" this is guarding against: wiring
        `load_default_schedule()` into the builder here would change `q` for
        every dividend-paying symbol and fork the lake into a second, invisible
        model whose files no sweep can ever read.
        """
        store = ChainStore(str(tmp_path / "cache"))
        mine = bf.build_chain_builder(provider, store)
        runner_style = ChainBuilder(provider, store=store)
        for symbol in ("XYZ", "AAPL", "MSFT", "KMI", "VZ", "PFE", "SPY"):
            assert mine._model_fingerprint(symbol) == runner_style._model_fingerprint(symbol)

    def test_the_two_call_sites_are_structurally_identical(self):
        """Source-level, not just behavioural.

        The fingerprint test above passes if BOTH sites are wrong in the same
        way. This one pins the actual construction in `scenarios/runner.py`
        against the one in `backfill.build_chain_builder`, so a future edit to
        either has to change both — or fail here.
        """
        def keywords_of(source):
            tree = ast.parse(textwrap.dedent(source))
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "ChainBuilder"
            ]
            assert len(calls) == 1, f"expected one ChainBuilder(...) call, got {len(calls)}"
            call = calls[0]
            return len(call.args), sorted(kw.arg for kw in call.keywords)

        from src.backtesting.scenarios import runner

        runner_src = inspect.getsource(runner.run_sweep)
        mine_src = inspect.getsource(bf.build_chain_builder)
        assert keywords_of(mine_src) == keywords_of(runner_src), (
            "the backfill must construct ChainBuilder byte-identically to the "
            "sweep runner: every default (risk_free_rate, dividend_yields, "
            "spread_model) is hashed into the cache key"
        )

    def test_a_model_mismatch_is_a_skip_not_a_silent_overwrite(
            self, tmp_path, provider, days, prices):
        """A file under another model is never replaced by this one.

        `_window_regression` calls that `model_changed`, and it is right to: a
        different model is a different answer, not a wider one.
        """
        day, price = days[0], prices[days[0]]
        seed_store = ChainStore(str(tmp_path / "seed"))
        other_model = ChainBuilder(provider, store=seed_store, risk_free_rate=0.09)
        other_model.build("XYZ", day, 21, underlying_price=price,
                          cost_basis=price * 1.5, low_anchor=price * 0.4)

        lake = FakeLake()
        lake.put_object("XYZ", day, Path(seed_store._path("XYZ", day)).read_bytes())
        store = _store(tmp_path, lake=lake, name="job-1")
        summary = _run(provider, store, ["XYZ"], day, day)

        assert summary.symbols[0].days_skipped == 1
        assert summary.symbols[0].days_written == 0


# --------------------------------------------------------------------------- #
# Splits
# --------------------------------------------------------------------------- #
class TestSplits:
    def test_split_days_finds_every_move_not_only_the_first(self, days):
        bars = [
            StockBar("XYZ", d, p, p, p, p, volume=1)
            for d, p in zip(days, [100, 100, 10, 10, 10, 100, 100, 100, 100, 100])
        ]
        found = bf.split_days(bars)
        assert sorted(found) == [days[2], days[5]], (
            "alpaca_provider.detect_split stops at the first move by design; a "
            "multi-year widening window can span two corporate actions"
        )

    def test_a_split_day_is_skipped_and_reported_but_the_symbol_continues(
            self, tmp_path, provider, days):
        prices = {d: 100.0 for d in days}
        for d in days[4:]:
            prices[d] = 10.0        # a 10:1 split on days[4]
        provider = LadderProvider(prices)

        store = _store(tmp_path, lake=FakeLake())
        summary = _run(provider, store, ["XYZ"], days[0], days[-1])
        row = summary.symbols[0]

        assert row.days_checked == len(days)
        assert row.days_skipped_corporate_action == 1, "the split day, and only it"
        assert row.errors == 0, (
            "a split is market data, not a fault — it must not reach the "
            "counter the exit code reads"
        )
        assert row.days_written == len(days) - 1, (
            "every other session must still be backfilled — a split must not "
            "cost a symbol its whole month"
        )
        assert store.stored_window("XYZ", days[4]) is None
        assert store.stored_window("XYZ", days[5]) is not None

    def test_the_skip_is_visible_in_the_summary_and_the_logs(
            self, tmp_path, days, captured_backfill_events):
        """Not failing must not mean not saying. A silent hole is the worse bug.

        The skip has to be legible in three places, because each is read by a
        different person at a different time: the terminal summary an operator
        reads after a manual run, the structured per-symbol log line that
        survives a killed container, and the per-day event that names the date.
        """
        prices = {d: 100.0 for d in days}
        for d in days[4:]:
            prices[d] = 10.0
        summary = _run(LadderProvider(prices), _store(tmp_path, lake=FakeLake()),
                       ["XYZ"], days[0], days[-1])

        assert summary.as_log()["days_skipped_corporate_action"] == 1
        assert "corp_act" in summary.render()
        assert summary.symbols[0].as_log()["days_skipped_corporate_action"] == 1

        skips = [e for e in captured_backfill_events
                 if e.get("event_type") == "backfill_day_skipped"]
        assert len(skips) == 1
        assert skips[0]["reason"] == "corporate_action"
        assert skips[0]["as_of"] == days[4].isoformat()

    def test_a_split_on_the_windows_first_day_is_still_seen(
            self, tmp_path, provider, days):
        """It needs a predecessor bar, which is why extra history is fetched.

        Without the lookback the first day of every window is a blind spot, and
        the weekly window slides — so a split would be caught for three runs and
        missed on the fourth.
        """
        earlier = _weekdays(days[0] - timedelta(days=10), 4)[:3]
        prices = {d: 100.0 for d in earlier}
        prices.update({d: 10.0 for d in days})
        provider = LadderProvider(prices)

        store = _store(tmp_path, lake=FakeLake())
        summary = _run(provider, store, ["XYZ"], days[0], days[-1])
        assert summary.symbols[0].days_skipped_corporate_action == 1
        assert summary.symbols[0].errors == 0
        assert store.stored_window("XYZ", days[0]) is None

    def test_a_split_does_not_fail_the_run(self, tmp_path, days):
        """The alert-fatigue decision, pinned.

        A stock split recurs across a 14-symbol universe and the trailing window
        slides, so failing here would page the Job-failure alert every Saturday
        for the month it takes the split to age out — on a condition nobody can
        act on. An alarm layer that cries wolf gets muted, and then it is not
        watching the failures that matter either.
        """
        prices = {d: 100.0 for d in days}
        for d in days[4:]:
            prices[d] = 10.0
        summary = _run(LadderProvider(prices), _store(tmp_path, lake=FakeLake()),
                       ["XYZ"], days[0], days[-1])
        assert summary.failed_symbols() == []
        assert summary.total("days_skipped_corporate_action") == 1

    def test_a_split_and_a_real_error_on_one_symbol_still_fails(
            self, tmp_path, days):
        """The split must not MASK a genuine failure on the same symbol."""
        prices = {d: 100.0 for d in days}
        for d in days[4:]:
            prices[d] = 10.0
        bad_day = days[7]

        class Flaky(LadderProvider):
            def get_contract_universe(self, underlying, as_of, *a, **kw):
                if as_of == bad_day:
                    raise RuntimeError("truncated response")
                return super().get_contract_universe(underlying, as_of, *a, **kw)

        summary = _run(Flaky(prices), _store(tmp_path, lake=FakeLake()),
                       ["XYZ"], days[0], days[-1])
        assert summary.symbols[0].days_skipped_corporate_action == 1
        assert summary.symbols[0].errors == 1
        assert summary.failed_symbols() == ["XYZ"]


# --------------------------------------------------------------------------- #
# Failure isolation and the exit contract
# --------------------------------------------------------------------------- #
class TestFailureIsolation:
    def test_one_symbols_failure_does_not_cost_the_others(self, tmp_path, days,
                                                          prices):
        class Flaky(LadderProvider):
            def get_stock_bars(self, symbol, start, end):
                if symbol == "BOOM":
                    raise RuntimeError("vendor said no")
                return super().get_stock_bars(symbol, start, end)

        provider = Flaky(prices)
        store = _store(tmp_path, lake=FakeLake())
        summary = bf.run_backfill(
            None, ["AAA", "BOOM", "ZZZ"], days[0], days[-1],
            chain_store=store, bar_provider=provider,
        )

        by_symbol = {s.symbol: s for s in summary.symbols}
        assert set(by_symbol) == {"AAA", "BOOM", "ZZZ"}
        assert by_symbol["BOOM"].error is not None
        assert "vendor said no" in by_symbol["BOOM"].error
        assert by_symbol["AAA"].days_written == len(days)
        assert by_symbol["ZZZ"].days_written == len(days)
        assert summary.failed_symbols() == ["BOOM"]

    def test_one_days_failure_does_not_cost_the_symbol(self, tmp_path, days,
                                                       prices):
        bad_day = days[3]

        class Flaky(LadderProvider):
            def get_contract_universe(self, underlying, as_of, *a, **kw):
                if as_of == bad_day:
                    raise RuntimeError("truncated response")
                return super().get_contract_universe(underlying, as_of, *a, **kw)

        summary = _run(Flaky(prices), _store(tmp_path, lake=FakeLake()),
                       ["XYZ"], days[0], days[-1])
        row = summary.symbols[0]
        assert row.errors == 1
        assert row.days_written == len(days) - 1
        assert row.days_checked == len(days)

    def test_a_clean_run_reports_no_failures(self, tmp_path, provider, days):
        summary = _run(provider, _store(tmp_path, lake=FakeLake()),
                       ["XYZ"], days[0], days[-1])
        assert summary.failed_symbols() == []
        assert summary.total("days_written") == len(days)

    def test_symbols_are_deduplicated_and_normalised(self, tmp_path, provider, days):
        summary = bf.run_backfill(
            None, [" xyz ", "XYZ", "", "xYz"], days[0], days[-1],
            chain_store=_store(tmp_path, lake=FakeLake()), bar_provider=provider,
        )
        assert [s.symbol for s in summary.symbols] == ["XYZ"]


# --------------------------------------------------------------------------- #
# Counters and window resolution
# --------------------------------------------------------------------------- #
class TestSummaryAccounting:
    def test_counts_come_from_the_stores_public_counters(self, tmp_path,
                                                         provider, days):
        """The summary must describe outcomes, not intentions.

        A store that declines every upload is the case that separates the two:
        the module still *asked* for ten writes, and the summary must say zero.
        """
        class Refusing(FakeLake):
            def upload(self, *a, **kw):
                self._record("upload", a[1], a[2])
                raise Exception("bucket is read-only today")

        store = _store(tmp_path, lake=Refusing())
        summary = _run(provider, store, ["XYZ"], days[0], days[-1])
        row = summary.symbols[0]
        assert row.days_written == 0
        assert store.summary()["lake_errors"] > 0
        assert row.reconciles
        assert summary.failed()

    def test_every_checked_day_lands_in_exactly_one_bucket(self, tmp_path,
                                                            provider, days):
        """The reconciliation invariant, on the run that broke it.

        A lake operation that fails *before* the circuit breaker trips moves
        `lake_errors` and nothing else — and `lake_errors` was not sampled, so
        the day fell into no bucket at all. The observed shape was a five-day
        run reporting "4 written" with the fifth day mentioned nowhere: a hole
        in the lake that the summary, the logs and the exit code all agreed did
        not exist.
        """
        fail_on = {days[2], days[5]}

        class Flaky(FakeLake):
            def upload(self, local_path, underlying, as_of, **kw):
                self._record("upload", underlying, as_of)
                if as_of in fail_on:
                    raise Exception("503 backend error")
                return super().upload(local_path, underlying, as_of, **kw)

        store = _store(tmp_path, lake=Flaky())
        summary = _run(provider, store, ["XYZ"], days[0], days[-1])
        row = summary.symbols[0]

        assert row.days_checked == len(days)
        assert row.errors == 2
        assert row.days_written == len(days) - 2
        assert row.reconciles, (
            f"{row.days_checked} checked != "
            f"{[getattr(row, b) for b in row.DAY_BUCKETS]}"
        )
        assert sum(getattr(row, b) for b in row.DAY_BUCKETS) == row.days_checked
        assert summary.failed_symbols() == ["XYZ"], (
            "a day the lake refused to take is a failure, not a footnote"
        )

    @pytest.mark.parametrize("scenario", ["clean", "covered", "split", "declined"])
    def test_the_buckets_reconcile_on_every_shape(self, tmp_path, provider,
                                                  days, prices, scenario):
        lake = FakeLake()
        symbols = ["XYZ"]
        if scenario == "split":
            prices = {d: 100.0 for d in days}
            for d in days[4:]:
                prices[d] = 10.0
            provider = LadderProvider(prices)
        if scenario == "declined":
            day, price = days[0], prices[days[0]]
            seed = ChainStore(str(tmp_path / "seed"))
            ChainBuilder(provider, store=seed).build(
                "XYZ", day, 60, underlying_price=price * 1.01,
                cost_basis=price * 3.0, low_anchor=price * 0.1)
            lake.put_object("XYZ", day,
                            Path(seed._path("XYZ", day)).read_bytes())

        store = _store(tmp_path, lake=lake, name="run-1")
        summary = _run(provider, store, symbols, days[0], days[-1])
        if scenario == "covered":
            summary = _run(provider, _store(tmp_path, lake=lake, name="run-2"),
                           symbols, days[0], days[-1])
            assert summary.symbols[0].days_covered == len(days)

        for row in summary.symbols:
            assert row.reconciles, (row.symbol, row.as_log())
        assert summary.as_log()["days_checked"] == sum(
            summary.total(b) for b in bf.SymbolBackfill.DAY_BUCKETS)

    def test_a_pass_that_cannot_account_for_its_days_is_a_failure(self):
        """If the arithmetic does not close, the window is not covered.

        Constructed directly rather than provoked: the point is the posture, not
        the mechanism that would produce it. A future counter added to
        `_classify_day` and forgotten in `DAY_BUCKETS` lands exactly here.
        """
        row = bf.SymbolBackfill(symbol="XYZ", days_checked=5, days_written=4)
        assert not row.reconciles
        assert row.failed
        assert row.as_log()["reconciles"] is False

    def test_a_lakeless_run_still_reports_writes(self, tmp_path, provider, days):
        """No lake means `lake_puts` cannot move; local writes still count.

        The Job always has a lake — this is the developer path, and a summary
        that reported "0 written" for a run that just built ten chain-days
        would train an operator to distrust the number that matters.
        """
        store = _store(tmp_path)
        summary = _run(provider, store, ["XYZ"], days[0], days[-1])
        assert store.lake is None
        assert summary.symbols[0].days_written == len(days)
        assert _run(provider, store, ["XYZ"], days[0], days[-1]) \
            .symbols[0].days_written == 0

class TestDeadLake:
    """A configured lake that dies mid-run is the run failing, not degrading.

    `ChainStore` degrades to local-only by design — the right answer for a
    backtest, which still needs its chains. It is exactly wrong here: this
    process exists to put objects in a bucket, and the container filesystem it
    would fall back to is destroyed when the task exits. The failure mode being
    closed off is a green exit-0 execution reporting a widened lake with
    `lake_puts = 0`, after which the operator moves on to the next chunk.
    """

    class _DeadOnUpload(FakeLake):
        def upload(self, *a, **kw):
            self._record("upload", a[1], a[2])
            raise Exception("permission denied")

    def _run_until_dead(self, tmp_path, provider, days):
        store = _store(tmp_path, lake=self._DeadOnUpload())
        summary = _run(provider, store, ["XYZ"], days[0], days[-1])
        assert store.summary()["lake_errors"] == len(days)
        assert store.summary()["lake_puts"] == 0
        return summary, store

    def test_the_breaker_does_not_trip_under_this_workload(self, tmp_path,
                                                           provider, days):
        """The finding that shaped `lake_failed`, pinned so it cannot regress.

        `ChainLake`'s breaker counts CONSECUTIVE failures and `note_success()`
        resets it — and a read is a success, a miss included (the RPC reached
        GCS). The backfill interleaves a provenance read and a write on every
        day, so each read resets the counter the write just incremented.

        A `lake_failed` keyed on `lake_disabled` alone would therefore have
        reported "lake: ok" on a run where every single upload failed. This
        test is the reason it is not keyed that way.
        """
        summary, store = self._run_until_dead(tmp_path, provider, days)
        assert not store.lake.disabled, (
            "if the breaker starts tripping here, re-read `lake_failed` — its "
            "second clause exists precisely because it does not"
        )
        assert store.lake._consecutive_errors < MAX_CONSECUTIVE_LAKE_ERRORS
        assert summary.lake_failed, (
            "and the run must still be a failure despite the healthy-looking "
            "lake state"
        )

    def test_local_only_fallback_writes_are_not_counted_as_written(
            self, tmp_path, provider, days):
        summary, _ = self._run_until_dead(tmp_path, provider, days)
        row = summary.symbols[0]
        assert row.days_written == 0, (
            "every day after the breaker tripped was written to a container "
            "filesystem that is about to be destroyed; calling that 'written' "
            "is the silent failure this phase exists to kill"
        )
        assert row.days_checked == len(days)
        assert row.errors == len(days)
        assert row.reconciles

    def test_the_run_exits_non_zero(self, tmp_path, provider, days):
        summary, _ = self._run_until_dead(tmp_path, provider, days)
        assert summary.lake_configured
        assert summary.lake_failed
        assert summary.failed()

    def test_a_tripped_breaker_is_also_a_failure(self, tmp_path, provider, days):
        """The other half: a lake switched off before the run even starts.

        `ChainStore` refuses every operation without an RPC once the lake is
        disabled, so no counter moves at all — no errors, no puts, nothing.
        Only the health flag distinguishes this from a run where every day was
        already covered.
        """
        lake = FakeLake()
        lake.disable("bucket_missing")
        store = _store(tmp_path, lake=lake)
        summary = _run(provider, store, ["XYZ"], days[0], days[-1])

        assert summary.lake_configured and summary.lake_failed
        assert summary.failed()
        assert summary.symbols[0].days_written == 0
        assert summary.symbols[0].errors == len(days)
        assert summary.symbols[0].reconciles
        assert "DISABLED mid-run" in summary.render()

    def test_a_dead_lake_fails_the_run_even_with_no_failed_symbol(self):
        """The two conditions are independent, and the summary carries both."""
        summary = bf.BackfillSummary(
            start=date(2026, 1, 1), end=date(2026, 1, 31), universe_dte=22,
            lake={"lake_enabled": True, "lake_disabled": True,
                  "lake_disabled_reason": "consecutive_errors",
                  "lake_bucket": "b", "lake_puts": 0, "lake_errors": 5},
        )
        summary.symbols.append(bf.SymbolBackfill(
            symbol="XYZ", days_checked=3, days_covered=3))
        assert summary.failed_symbols() == []
        assert summary.failed()

    def test_lake_health_is_in_the_log_and_the_render(self, tmp_path, provider,
                                                      days):
        summary, _ = self._run_until_dead(tmp_path, provider, days)
        log = summary.as_log()
        assert log["lake_enabled"] is True
        assert log["lake_disabled"] is False, (
            "the breaker does not trip under this workload — see "
            "test_the_breaker_does_not_trip_under_this_workload"
        )
        assert log["lake_failed"] is True
        assert log["lake_puts"] == 0
        assert log["lake_errors"] > 0
        rendered = summary.render()
        assert "lake: ERRORS" in rendered
        assert "breaker did NOT trip" in rendered
        assert "did not do its job" in rendered

    def test_a_lakeless_by_config_run_is_healthy_and_exits_zero(
            self, tmp_path, provider, days):
        """No bucket configured is a developer build, not a broken Job.

        The distinction is the whole reason `lake_failed` asks whether the lake
        was CONFIGURED rather than whether it is usable.
        """
        store = _store(tmp_path)
        summary = _run(provider, store, ["XYZ"], days[0], days[-1])
        assert store.lake is None
        assert not summary.lake_configured
        assert not summary.lake_failed
        assert not summary.failed()
        assert summary.symbols[0].days_written == len(days)
        assert "NOT CONFIGURED" in summary.render()

    def test_a_healthy_lake_says_so(self, tmp_path, provider, days):
        summary = _run(provider, _store(tmp_path, lake=FakeLake()), ["XYZ"],
                       days[0], days[-1])
        assert summary.lake_configured and not summary.lake_failed
        assert "lake: ok" in summary.render()
        assert summary.as_log()["lake_puts"] == len(days)


class TestDayLevelFailures:
    def test_a_non_positive_close_is_an_error_not_a_skip(
            self, tmp_path, days, captured_backfill_events):
        """Broken vendor data must not hide in the corporate-action counter.

        Note the shape needed to reach this branch at all, which is itself the
        interesting part: a lone zero close is a >40% move, so the split
        detector claims it first. The second consecutive zero is what lands
        here — `split_days` skips a pair whose predecessor close is already
        non-positive. So one run exercises both branches and proves they are
        counted apart, which is the property under test.

        A skip query ("which days is the lake missing, and why") must not sweep
        in failures, so this logs under `backfill_day_failed`, not
        `backfill_day_skipped`.
        """
        prices = {d: 100.0 for d in days}
        prices[days[3]] = 0.0   # reads as a corporate action (100 -> 0)
        prices[days[4]] = 0.0   # no usable close, and no ratio to judge it by
        summary = _run(LadderProvider(prices),
                       _store(tmp_path, lake=FakeLake()), ["XYZ"],
                       days[0], days[-1])
        row = summary.symbols[0]

        assert row.errors == 1
        assert row.days_skipped_corporate_action == 1
        assert row.days_written == len(days) - 2
        assert row.reconciles
        assert summary.failed(), "an unusable close fails the run"

        failures = [e for e in captured_backfill_events
                    if e.get("event_type") == "backfill_day_failed"]
        assert [e["reason"] for e in failures] == ["no_underlying_price"]
        assert failures[0]["as_of"] == days[4].isoformat()

        skips = [e for e in captured_backfill_events
                 if e.get("event_type") == "backfill_day_skipped"]
        assert [e["reason"] for e in skips] == ["corporate_action"], (
            "the failure must NOT appear in a skip query"
        )
        assert skips[0]["as_of"] == days[3].isoformat()

    def test_a_symbol_with_no_bars_in_the_window_fails(self, tmp_path, days):
        """The typo'd-candidate case, which three documents already promise.

        Alpaca answers an unknown ticker with an empty bar list, not an error.
        As a silent zero-day pass it would report success for ever, and the
        first symptom would be a sweep against a symbol that has no data at all.
        """
        provider = LadderProvider({d: 100.0 for d in days})
        provider.per_symbol["TYPO"] = {}
        summary = bf.run_backfill(
            None, ["XYZ", "TYPO"], days[0], days[-1],
            chain_store=_store(tmp_path, lake=FakeLake()), bar_provider=provider,
        )
        by_symbol = {s.symbol: s for s in summary.symbols}

        assert by_symbol["TYPO"].error is not None
        assert "no settled sessions" in by_symbol["TYPO"].error
        assert summary.failed_symbols() == ["TYPO"]
        assert summary.failed()
        assert by_symbol["XYZ"].days_written == len(days), (
            "and the good symbol still completes"
        )

    def test_an_empty_window_is_still_not_a_symbol_failure(self, tmp_path,
                                                           provider):
        """A window with no sessions is refused BEFORE any symbol is walked.

        The zero-bars rule is about a symbol with no data, not about a caller
        asking for a window that contains no sessions at all — that is caught
        once, up front, rather than reported as fourteen broken symbols.
        """
        summary = _run(provider, _store(tmp_path, lake=FakeLake()), ["XYZ"],
                       date.today() + timedelta(days=3),
                       date.today() + timedelta(days=9))
        assert summary.symbols == []
        assert not summary.failed()


class TestSummaryAccountingExtra:
    def test_totals_and_render(self, tmp_path, provider, days, prices):
        summary = bf.run_backfill(
            None, ["AAA", "ZZZ"], days[0], days[-1],
            chain_store=_store(tmp_path, lake=FakeLake()), bar_provider=provider,
        )
        assert summary.total("days_checked") == 2 * len(days)
        rendered = summary.render()
        assert "AAA" in rendered and "ZZZ" in rendered
        assert "universe_dte=22" in rendered
        log = summary.as_log()
        assert log["universe_dte"] == 22
        assert log["failed_symbols"] == []


class TestWindowResolution:
    def test_default_window_is_thirty_days_back_from_the_last_settled_session(self):
        today = date(2026, 8, 31)
        start, end = bf.resolve_window(today=today)
        assert end == date(2026, 8, 30)
        assert (end - start).days == bf.DEFAULT_HISTORY_DAYS == 30

    def test_explicit_bounds_win(self):
        start, end = bf.resolve_window(
            start=date(2024, 3, 1), end=date(2024, 6, 1), history_days=5)
        assert (start, end) == (date(2024, 3, 1), date(2024, 6, 1))

    def test_end_is_clamped_to_the_last_settled_session(self, tmp_path, provider,
                                                        days):
        """Today's chain is still forming; a run may not persist it."""
        summary = _run(provider, _store(tmp_path, lake=FakeLake()),
                       ["XYZ"], days[0], date.today() + timedelta(days=5))
        assert summary.end < date.today()

    def test_an_empty_window_does_nothing_rather_than_raising(self, tmp_path,
                                                              provider):
        summary = _run(provider, _store(tmp_path, lake=FakeLake()), ["XYZ"],
                       date.today() + timedelta(days=3),
                       date.today() + timedelta(days=9))
        assert summary.symbols == []
        assert summary.failed_symbols() == []


# --------------------------------------------------------------------------- #
# The provenance accessor the window rule depends on
# --------------------------------------------------------------------------- #
class TestStoredWindow:
    def test_reads_a_local_file(self, tmp_path, provider, days, prices):
        store = ChainStore(str(tmp_path / "cache"))
        ChainBuilder(provider, store=store).build(
            "XYZ", days[0], 7, underlying_price=prices[days[0]])
        window = store.stored_window("XYZ", days[0])
        assert window["universe_dte"] == 8
        assert window["underlying_price"] == pytest.approx(prices[days[0]])
        assert window["model"]

    def test_pulls_from_the_lake_when_the_local_cache_is_empty(
            self, tmp_path, provider, days, prices):
        seed = ChainStore(str(tmp_path / "seed"))
        ChainBuilder(provider, store=seed).build(
            "XYZ", days[0], 7, underlying_price=prices[days[0]])
        lake = FakeLake()
        lake.put_object("XYZ", days[0], Path(seed._path("XYZ", days[0])).read_bytes())

        store = ChainStore(str(tmp_path / "job"), lake=lake)
        assert store.stored_window("XYZ", days[0])["universe_dte"] == 8

    def test_returns_none_for_an_absent_day(self, tmp_path):
        assert ChainStore(str(tmp_path / "cache")).stored_window(
            "XYZ", date(2025, 1, 6)) is None

    def test_an_unreadable_file_reads_as_absent_and_is_not_deleted(self, tmp_path):
        store = ChainStore(str(tmp_path / "cache"))
        path = store._path("XYZ", date(2025, 1, 6))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a parquet")
        assert store.stored_window("XYZ", date(2025, 1, 6)) is None
        assert path.exists(), (
            "a provenance READ must have no side effects on the cache; `get` "
            "owns the discard decision"
        )

    def test_reports_a_missing_provenance_column_as_unknown(self, tmp_path):
        store = ChainStore(str(tmp_path / "cache"))
        path = store._path("XYZ", date(2025, 1, 6))
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{
            "symbol": "X", "underlying": "XYZ", "as_of": "2025-01-06",
            "expiration": "2025-01-10", "strike": 100.0, "option_type": "put",
            "dte": 4, "underlying_price": 100.0, "mark": 1.0, "bid": 0.9,
            "ask": 1.1, "implied_volatility": 0.3, "delta": -0.2, "volume": 1,
            "modeled_spread": True, "modeled_greeks": True,
        }]).to_parquet(path, index=False)
        window = store.stored_window("XYZ", date(2025, 1, 6))
        assert window["universe_dte"] is None
        assert window["strike_gte"] is None
        # ...and a window nothing can prove must not be treated as covering.
        assert not bf.covers(window, strike_gte=60.0, strike_lte=140.0,
                             model="m", underlying_price=100.0)


class TestCoveragePrediction:
    """`covers` decides whether a write was even going to be attempted."""

    BASE = {"universe_dte": 22, "strike_gte": 60.0, "strike_lte": 140.0,
            "model": "m1", "underlying_price": 100.0}

    def _covers(self, **overrides):
        stored = {**self.BASE, **overrides}
        return bf.covers(stored, strike_gte=60.0, strike_lte=140.0, model="m1",
                         underlying_price=100.0)

    def test_an_exact_match_covers(self):
        assert self._covers()

    def test_a_wider_stored_window_covers(self):
        assert self._covers(universe_dte=30, strike_gte=10.0, strike_lte=900.0)

    @pytest.mark.parametrize("overrides", [
        {"universe_dte": 8},
        {"strike_gte": 70.0},
        {"strike_lte": 130.0},
        {"model": "m2"},
        {"underlying_price": 100.5},
        {"universe_dte": None},
        {"strike_gte": None},
    ])
    def test_anything_short_of_a_superset_does_not(self, overrides):
        assert not self._covers(**overrides)

    def test_nothing_stored_never_covers(self):
        assert not bf.covers(None, strike_gte=1.0, strike_lte=2.0, model="m",
                             underlying_price=1.0)

    def test_the_price_tolerance_is_the_stores_relative_one(self):
        """A float wobble on an expensive underlying is not a rebuild.

        `ChainStore._close` scales its tolerance with the price
        (`_PRICE_TOL * max(1, |a|, |b|)`). An absolute 1e-9 here would predict
        "not covered" for a $900 name where the store says covered — the
        accounting would then report a write the store never made, on the one
        symbol class (SPY, QQQ, high-priced tech) the widening cares most
        about.

        Verified against the store's own comparator rather than a hand-picked
        epsilon, so the two cannot drift apart.
        """
        from src.backtesting.data.chain_store import _close

        for price in (5.0, 100.0, 925.25, 6120.0):
            wobble = price * 5e-10          # inside the relative tolerance
            stored = {"universe_dte": 22, "strike_gte": price * 0.5,
                      "strike_lte": price * 1.5, "model": "m1",
                      "underlying_price": price + wobble}
            assert _close(price + wobble, price), "premise: the store agrees"
            assert bf.covers(stored, strike_gte=price * 0.6,
                             strike_lte=price * 1.4, model="m1",
                             underlying_price=price), price

    def test_a_genuinely_different_close_still_does_not_cover(self):
        """The tolerance must not become a licence to ignore a restated bar."""
        from src.backtesting.data.chain_store import _close

        stored = {"universe_dte": 22, "strike_gte": 500.0, "strike_lte": 1500.0,
                  "model": "m1", "underlying_price": 925.25}
        assert not _close(925.26, 925.25), "premise: the store disagrees"
        assert not bf.covers(stored, strike_gte=555.0, strike_lte=1295.0,
                             model="m1", underlying_price=925.26)


# --------------------------------------------------------------------------- #
# `main.py --command backfill` — the CLI and the Job's entry point
# --------------------------------------------------------------------------- #
class _StubConfig:
    """Only what the backfill command reads off a Config."""

    def __init__(self, symbols=("AAPL", "MSFT"), candidates=("TSLA",)):
        self.stock_symbols = list(symbols)
        self.candidate_symbols = list(candidates)


class _Args:
    def __init__(self, **kw):
        self.symbols = kw.pop("symbols", None)
        self.start = kw.pop("start", None)
        self.end = kw.pop("end", None)
        self.history_days = kw.pop("history_days", None)
        assert not kw, kw


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("BACKFILL_SYMBOLS", "BACKFILL_HISTORY_DAYS", "BACKFILL_START",
                "BACKFILL_END"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def cli(monkeypatch, clean_env):
    """`main.run_backfill_cmd` with the run itself recorded, not performed."""
    import main as main_module

    calls = []

    def _fake_run(config, symbols, start, end, **kw):
        calls.append({"symbols": list(symbols), "start": start, "end": end,
                      **kw})
        return bf.BackfillSummary(start=start, end=end, universe_dte=22)

    monkeypatch.setattr(bf, "run_backfill", _fake_run)
    monkeypatch.setattr(
        "src.backtesting.data.chain_store.ChainStore.from_env",
        classmethod(lambda cls, cache_dir=None: None))
    return main_module, calls


class TestBackfillCommand:
    def test_the_command_is_selectable(self):
        """`--command backfill` must exist, or the Job's args are a typo."""
        import argparse
        import main as main_module

        source = inspect.getsource(main_module.main)
        assert "'backfill'" in source
        assert "run_backfill_cmd" in source

    def test_defaults_to_the_live_universe_plus_candidates(self, cli):
        main_module, calls = cli
        main_module.run_backfill_cmd(_Args(), _StubConfig(), _NullLogger())
        assert calls[0]["symbols"] == ["AAPL", "MSFT", "TSLA"]

    def test_the_default_window_is_thirty_days(self, cli):
        main_module, calls = cli
        main_module.run_backfill_cmd(_Args(), _StubConfig(), _NullLogger())
        assert (calls[0]["end"] - calls[0]["start"]).days == 30

    def test_the_cli_beats_the_job_env(self, cli, monkeypatch):
        """An operator running a one-off widening must get what they typed."""
        main_module, calls = cli
        monkeypatch.setenv("BACKFILL_SYMBOLS", "GOOGL")
        monkeypatch.setenv("BACKFILL_HISTORY_DAYS", "5")
        main_module.run_backfill_cmd(
            _Args(symbols="nvda,amd", history_days=90), _StubConfig(),
            _NullLogger())
        assert calls[0]["symbols"] == ["NVDA", "AMD"]
        assert (calls[0]["end"] - calls[0]["start"]).days == 90

    def test_the_job_env_beats_the_config(self, cli, monkeypatch):
        main_module, calls = cli
        monkeypatch.setenv("BACKFILL_SYMBOLS", " googl , spy ")
        monkeypatch.setenv("BACKFILL_START", "2024-03-01")
        monkeypatch.setenv("BACKFILL_END", "2024-06-01")
        main_module.run_backfill_cmd(_Args(), _StubConfig(), _NullLogger())
        assert calls[0]["symbols"] == ["GOOGL", "SPY"]
        assert calls[0]["start"] == date(2024, 3, 1)
        assert calls[0]["end"] == date(2024, 6, 1)

    def test_every_job_env_var_is_optional(self, cli):
        """A bare weekly execution must be a complete instruction."""
        main_module, calls = cli
        assert main_module.run_backfill_cmd(
            _Args(), _StubConfig(), _NullLogger()) == 0
        assert calls[0]["symbols"]

    @pytest.mark.parametrize("var,value", [
        ("BACKFILL_START", "01/03/2024"),
        ("BACKFILL_END", "2024-6-1x"),
        ("BACKFILL_HISTORY_DAYS", "thirty"),
    ])
    def test_a_malformed_job_override_is_refused_by_name(self, cli, monkeypatch,
                                                         var, value):
        """Named in the message: a Job env typo is invisible otherwise."""
        main_module, _ = cli
        monkeypatch.setenv(var, value)
        with pytest.raises(SystemExit) as exc:
            main_module.run_backfill_cmd(_Args(), _StubConfig(), _NullLogger())
        assert var in str(exc.value)

    def test_an_empty_universe_is_refused_rather_than_a_no_op(self, cli):
        main_module, _ = cli
        with pytest.raises(SystemExit):
            main_module.run_backfill_cmd(
                _Args(), _StubConfig(symbols=(), candidates=()), _NullLogger())

    def test_an_inverted_window_is_refused(self, cli, monkeypatch):
        main_module, _ = cli
        monkeypatch.setenv("BACKFILL_START", "2024-06-01")
        monkeypatch.setenv("BACKFILL_END", "2024-03-01")
        with pytest.raises(SystemExit):
            main_module.run_backfill_cmd(_Args(), _StubConfig(), _NullLogger())

    def test_a_run_whose_only_gap_is_a_split_exits_zero(self, monkeypatch,
                                                        clean_env):
        """The exit code is what the Job-failure alert policy reads.

        This is the end of the chain the alert-fatigue decision is about: skip
        -> counter -> `failed` -> exit code -> alert. A split must not travel
        down it.
        """
        import main as main_module

        summary = bf.BackfillSummary(
            start=date(2026, 1, 1), end=date(2026, 1, 31), universe_dte=22)
        summary.symbols.append(bf.SymbolBackfill(
            symbol="NVDA", days_checked=20, days_written=19,
            days_skipped_corporate_action=1))
        monkeypatch.setattr(bf, "run_backfill", lambda *a, **kw: summary)
        monkeypatch.setattr(
            "src.backtesting.data.chain_store.ChainStore.from_env",
            classmethod(lambda cls, cache_dir=None: None))

        assert main_module.run_backfill_cmd(
            _Args(), _StubConfig(), _NullLogger()) == 0

    def test_a_failed_symbol_exits_non_zero(self, monkeypatch, clean_env):
        """The Job-failure alert is downstream of this exit code."""
        import main as main_module

        summary = bf.BackfillSummary(
            start=date(2026, 1, 1), end=date(2026, 1, 5), universe_dte=22)
        summary.symbols.append(
            bf.SymbolBackfill(symbol="NVDA", error="RuntimeError: vendor said no"))
        monkeypatch.setattr(bf, "run_backfill", lambda *a, **kw: summary)
        monkeypatch.setattr(
            "src.backtesting.data.chain_store.ChainStore.from_env",
            classmethod(lambda cls, cache_dir=None: None))

        assert main_module.run_backfill_cmd(
            _Args(), _StubConfig(), _NullLogger()) == 1

    def test_the_sigterm_handler_is_armed_before_the_work_starts(
            self, monkeypatch, clean_env):
        """Cloud Run gives 10s between SIGTERM and SIGKILL.

        The handler must already be installed when `run_backfill` is entered —
        `ChainStore.from_env()` probes the bucket first, and a cancel landing
        there used to kill the container outright.
        """
        import signal

        import main as main_module

        seen = {}

        def _fake_run(config, symbols, start, end, **kw):
            seen["handler"] = signal.getsignal(signal.SIGTERM)
            return bf.BackfillSummary(start=start, end=end, universe_dte=22)

        monkeypatch.setattr(bf, "run_backfill", _fake_run)
        monkeypatch.setattr(
            "src.backtesting.data.chain_store.ChainStore.from_env",
            classmethod(lambda cls, cache_dir=None: None))
        before = signal.getsignal(signal.SIGTERM)

        main_module.run_backfill_cmd(_Args(), _StubConfig(), _NullLogger())

        assert callable(seen["handler"])
        assert seen["handler"] is not before
        assert signal.getsignal(signal.SIGTERM) is before, (
            "the previous handler must be restored on the way out"
        )

    def test_a_termination_still_reports_what_is_known(self, monkeypatch,
                                                       clean_env):
        """No summary object exists — say so, do not swallow the failure."""
        import main as main_module

        def _boom(*a, **kw):
            raise main_module.SweepTerminated("SIGTERM")

        errors = []
        monkeypatch.setattr(bf, "run_backfill", _boom)
        monkeypatch.setattr(
            "src.backtesting.data.chain_store.ChainStore.from_env",
            classmethod(lambda cls, cache_dir=None: None))

        logger = _NullLogger()
        logger.errors = errors
        with pytest.raises(main_module.SweepTerminated):
            main_module.run_backfill_cmd(_Args(), _StubConfig(), logger)
        assert any(kw.get("event_type") == "backfill_unfinished"
                   for kw in errors)


class _NullLogger:
    def __init__(self):
        self.errors = []

    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass

    def error(self, *a, **kw):
        self.errors.append(kw)
