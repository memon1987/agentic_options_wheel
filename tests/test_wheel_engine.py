"""Tests for wheel strategy engine.

FC-068 deleted the orchestration half of ``WheelEngine`` — ``run_strategy_cycle``
and everything beneath it. Production stopped calling it on 2025-10-03, three
days before the live account's first fill, and the backtest was its only
surviving caller. The 16 tests that pinned that path are gone with it; the
behaviours worth keeping migrated to the live path's own tests:

* stage-6's naked-call/oversell half → ``ExecutionEngine._available_shares``
  and ``strict_option_type`` tests in ``tests/test_execution_engine.py``.
  (Its *open-order duplicate window* half — an unfilled resting order that a
  positions-based check cannot see — is covered by nothing on the live path
  either, before or after. That is FC-009's standing territory; this deletion
  neither widens nor closes it.)
* the covered-call floor family → ``tests/test_options_scanner.py``
  (scan-time floor) and ``tests/test_call_seller.py`` (FC-050 execute-time
  floor).
* the drawdown pause → **deleted, not migrated** (FC-065 OQ-3: it is not
  ported to the live path).

What remains here is the slimmed constructor plus a pin on the deletion itself.
The two methods production actually calls are exercised elsewhere:
``reconcile_positions`` through the backtest day loop
(``tests/test_backtest_simulator.py``) and ``run_rolling_cycle`` through
``tests/test_backtest_earnings.py`` and the roller suites
(``tests/test_call_roller*.py``). Neither had dedicated coverage here before
this FC either — noted, not introduced by it.
"""

from unittest.mock import Mock, patch

from src.strategy.wheel_engine import WheelEngine
from src.utils.config import Config


class TestWheelEngineConstruction:
    """The slimmed constructor: what it still wires, and what it must not."""

    def _engine(self):
        self.mock_config = Mock(spec=Config)
        self.mock_config.stock_symbols = ['AAPL', 'MSFT', 'GOOGL']

        with patch('src.strategy.wheel_engine.AlpacaClient') as mock_alpaca_cls, \
             patch('src.strategy.wheel_engine.MarketDataManager') as mock_market_cls:
            self.mock_alpaca = Mock()
            self.mock_market_data = Mock()
            mock_alpaca_cls.return_value = self.mock_alpaca
            mock_market_cls.return_value = self.mock_market_data
            return WheelEngine(self.mock_config)

    def test_wheel_engine_initialization(self):
        engine = self._engine()
        assert engine.config is self.mock_config
        assert engine.alpaca is self.mock_alpaca
        # The roller still needs market data of its own.
        assert engine.market_data is self.mock_market_data
        assert engine.wheel_state is not None

    def test_the_deleted_path_is_really_gone(self):
        """A partial deletion — a method left behind with no caller — is the
        state this FC exists to end. Named explicitly so a revert or a merge
        that resurrects one of them fails loudly rather than quietly restoring
        a strategy nobody trades."""
        engine = self._engine()
        for gone in (
            'run_strategy_cycle',
            '_manage_existing_positions',
            '_evaluate_option_position',
            '_can_open_new_positions',
            '_find_new_opportunities',
            '_has_existing_position',
            '_has_existing_option_position',
            'get_strategy_status',
            '_get_stock_position_for_symbol',
            '_log_daily_stock_snapshots',
        ):
            assert not hasattr(engine, gone), f"{gone} is back on WheelEngine"

    def test_the_engine_no_longer_owns_sellers_or_a_gap_detector(self):
        """The engine constructed a PutSeller, a CallSeller (with its own
        CostBasisResolver) and a GapDetector purely to feed the dead path.
        ``/run`` builds its own sellers; the gap detector module itself was
        deleted by FC-069 item 5. Holding them here would keep a second,
        divergent wiring alive."""
        engine = self._engine()
        for gone in ('put_seller', 'call_seller', 'gap_detector',
                     '_pending_underlyings'):
            assert not hasattr(engine, gone), f"{gone} is back on WheelEngine"

    def test_what_production_calls_still_exists(self):
        engine = self._engine()
        assert callable(engine.reconcile_positions)      # /run pre-trade
        assert callable(engine.run_rolling_cycle)        # Friday /roll
        assert callable(engine._extract_underlying_from_option_symbol)

    def test_the_roller_replay_gate_is_still_carried(self):
        """FC-065 Phase 2's gate rides on the engine even though the seller it
        also fed is gone."""
        with patch('src.strategy.wheel_engine.MarketDataManager'):
            engine = WheelEngine(Mock(spec=Config), alpaca_client=Mock(),
                                 allow_bigquery_cost_basis=False)
        assert engine._allow_bigquery_cost_basis is False


# =========================================================================== #
# FC-078 — the roll cycle: terminal-event contract, budget guard, open-order
# fetch (T-11, T-20)
#
# Plan: docs/plans/fc-078.md DD-5 / §4.
# =========================================================================== #

from datetime import date, timedelta  # noqa: E402

from src.strategy import wheel_engine as wheel_engine_module  # noqa: E402


TERMINAL_EVENTS = {
    'call_roll_skipped',
    'call_roll_skipped_cost_basis_unresolved',
    'call_roll_skipped_cost_basis_divergent',
    'call_roll_btc_rejected',
    'call_roll_btc_timeout_canceled',
    'call_roll_naked_exposure',
    'call_roll_completed',
    'call_roll_dry_run',
    'call_roll_execution_error',
}


def _occ(underlying, expiry, strike):
    return (f"{underlying}{expiry.strftime('%y%m%d')}C"
            f"{int(round(strike * 1000)):08d}")


class _CycleFixture:
    """A book of N short calls, each with covering shares."""

    EXPIRY = date(2026, 8, 7)

    def _book(self, symbols):
        positions = []
        for sym in symbols:
            positions.append({'symbol': _occ(sym, self.EXPIRY, 100.0),
                              'qty': '-1', 'asset_class': 'us_option'})
            positions.append({'symbol': sym, 'qty': '100',
                              'asset_class': 'us_equity',
                              'avg_entry_price': '90.00', 'side': 'long'})
        return positions

    def _engine(self, symbols, *, open_orders=None, roller_factory=None):
        config = Mock(spec=Config)
        config.rolling_enabled = True
        config.earnings_enabled = False

        alpaca = Mock()
        alpaca.get_positions.return_value = self._book(symbols)
        alpaca.get_orders.return_value = open_orders if open_orders is not None else []

        with patch('src.strategy.wheel_engine.MarketDataManager'):
            engine = WheelEngine(config, alpaca_client=alpaca,
                                 wheel_state=Mock())
        return engine, alpaca


class TestTheTerminalEventContract(_CycleFixture):
    """T-11. Every short call evaluated in a cycle emits EXACTLY ONE terminal
    event.

    *Catches:* any reintroduced silent path. FC-066's whole diagnosis was that
    five Fridays of production reported ``rolls_evaluated > 0,
    rolls_executed = 0`` and nothing else — three ``return None`` branches with
    no event between them.
    """

    def _run_and_collect(self, engine, side_effects):
        """Drive the cycle with a stub roller whose evaluations are scripted."""
        emitted = []

        class _StubRoller:
            def __init__(self, *a, **kw):
                pass

            def log_terminal_skip(self, option_symbol, underlying, reason, **kw):
                emitted.append(('call_roll_skipped', option_symbol, reason))

            def evaluate_roll_opportunity(self, call_pos, stock_pos, open_syms=None):
                behaviour = side_effects.pop(0)
                if behaviour == 'skip':
                    emitted.append(('call_roll_skipped',
                                    call_pos['symbol'], 'not_itm_enough'))
                    return None
                if behaviour == 'raise':
                    raise RuntimeError("boom")
                return {'symbol': call_pos['symbol'], 'behaviour': behaviour}

            def execute_roll(self, opportunity):
                if opportunity['behaviour'] == 'complete':
                    emitted.append(('call_roll_completed',
                                    opportunity['symbol'], None))
                    return {'success': True}
                emitted.append(('call_roll_naked_exposure',
                                opportunity['symbol'], None))
                return {'success': False, 'reason': 'stc_failed_naked_exposure'}

        with patch('src.strategy.wheel_engine.CallRoller', _StubRoller):
            with patch('src.strategy.wheel_engine.log_error_event') as err:
                results = engine.run_rolling_cycle()
        for c in err.call_args_list:
            emitted.append((c.kwargs['error_type'], c.kwargs.get('symbol'), None))
        return results, emitted

    def test_every_evaluated_position_emits_exactly_one_terminal(self):
        symbols = ['AAA', 'BBB', 'CCC', 'DDD']
        engine, _ = self._engine(symbols)

        results, emitted = self._run_and_collect(
            engine, ['skip', 'complete', 'naked', 'raise'])

        assert results['rolls_evaluated'] == 4
        terminals = [e for e in emitted if e[0] in TERMINAL_EVENTS]
        assert len(terminals) == 4, terminals
        # One per position, no position counted twice.
        assert len({t[1] for t in terminals}) == 4

    def test_an_exception_still_produces_a_terminal(self):
        """Per-position try/except keeps one bad symbol from killing the cycle
        — but "exactly one terminal per position" has to hold on the paths
        nobody planned for too."""
        engine, _ = self._engine(['AAA'])

        results, emitted = self._run_and_collect(engine, ['raise'])

        assert results['rolls_evaluated'] == 1
        assert [e[0] for e in emitted if e[0] in TERMINAL_EVENTS] == \
            ['call_roll_skipped']

    def test_one_bad_symbol_does_not_kill_the_cycle(self):
        engine, _ = self._engine(['AAA', 'BBB'])

        results, _ = self._run_and_collect(engine, ['raise', 'complete'])

        assert results['rolls_evaluated'] == 2
        assert results['rolls_executed'] == 1


class TestTheOpenOrderFetch(_CycleFixture):
    """DD-4 / FC-043. ``status`` is not a value filter over Alpaca's default
    page: ``'open'`` is a QUERY TOKEN, and ``AlpacaClient.get_orders`` routes it
    to ``QueryOrderStatus.OPEN``. Filtering ``status.value == 'open'`` — what
    FC-043 found in production — matched nothing at all.
    """

    def test_the_cycle_asks_for_the_open_bucket_once(self):
        engine, alpaca = self._engine(['AAA', 'BBB'])

        with patch('src.strategy.wheel_engine.CallRoller'):
            engine.run_rolling_cycle()

        alpaca.get_orders.assert_called_once_with(status='open')

    def test_open_symbols_are_forwarded_to_the_roller(self):
        conflicting = _occ('AAA', self.EXPIRY, 100.0)
        engine, _ = self._engine(
            ['AAA'], open_orders=[{'symbol': conflicting, 'status': 'new'}])

        with patch('src.strategy.wheel_engine.CallRoller') as roller_cls:
            roller = roller_cls.return_value
            roller.evaluate_roll_opportunity.return_value = None
            engine.run_rolling_cycle()

        _, kwargs = roller.evaluate_roll_opportunity.call_args
        args, _ = roller.evaluate_roll_opportunity.call_args
        assert conflicting in args[2]

    def test_an_unreadable_open_order_book_fails_closed(self):
        """Without the open-order picture the guard cannot protect against the
        double buy-to-close it exists for, so nothing is rolled."""
        engine, alpaca = self._engine(['AAA', 'BBB'])
        alpaca.get_orders.side_effect = RuntimeError("alpaca down")

        with patch('src.strategy.wheel_engine.CallRoller') as roller_cls:
            roller = roller_cls.return_value
            results = engine.run_rolling_cycle()

        assert results['rolls_skipped'] == 2
        roller.evaluate_roll_opportunity.assert_not_called()
        assert roller.log_terminal_skip.call_count == 2
        reasons = {c.args[2] for c in roller.log_terminal_skip.call_args_list}
        assert reasons == {'open_orders_unavailable'}


class TestUncoveredShortCalls(_CycleFixture):
    """Trader L-1. A short call with no matching stock position was dropped
    before the loop, silently — so the one position shape nobody wants, a
    genuinely naked short call, was the one the roll cycle never mentioned."""

    def _naked_book(self):
        return [
            {'symbol': _occ('AAA', self.EXPIRY, 100.0), 'qty': '-1',
             'asset_class': 'us_option'},
            # no AAA shares
        ]

    def test_a_short_call_with_no_covering_shares_emits_a_terminal(self):
        engine, alpaca = self._engine([])
        alpaca.get_positions.return_value = self._naked_book()

        with patch('src.strategy.wheel_engine.CallRoller') as roller_cls:
            roller = roller_cls.return_value
            results = engine.run_rolling_cycle()

        assert results['rolls_evaluated'] == 1
        assert results['rolls_skipped'] == 1
        roller.evaluate_roll_opportunity.assert_not_called()
        assert roller.log_terminal_skip.call_count == 1
        assert roller.log_terminal_skip.call_args.args[2] == 'no_covering_shares'

    def test_covered_calls_are_unaffected(self):
        engine, _ = self._engine(['AAA'])

        with patch('src.strategy.wheel_engine.CallRoller') as roller_cls:
            roller = roller_cls.return_value
            roller.evaluate_roll_opportunity.return_value = None
            results = engine.run_rolling_cycle()

        assert results['rolls_evaluated'] == 1
        roller.evaluate_roll_opportunity.assert_called_once()
        roller.log_terminal_skip.assert_not_called()


class TestTheCycleBudgetGuard(_CycleFixture):
    """T-20. A position is never STARTED without the full per-position worst
    case remaining, because a kill between a filled BTC and an unplaced STO is
    the worst seam there is — shares uncovered, no event, no alert.

    *Mutation:* drop the pre-position budget check → these fail.
    """

    def test_a_fresh_budget_proceeds(self):
        engine, _ = self._engine(['AAA'])

        with patch('src.strategy.wheel_engine.CallRoller') as roller_cls:
            roller = roller_cls.return_value
            roller.evaluate_roll_opportunity.return_value = None
            engine.run_rolling_cycle()

        roller.evaluate_roll_opportunity.assert_called_once()

    def test_an_exhausted_budget_defers_the_position(self, monkeypatch):
        engine, _ = self._engine(['AAA', 'BBB'])

        # Clock: start, then far enough past that only the worst case of one
        # position fits, then past the budget entirely.
        start = date(2026, 8, 4)
        times = iter([
            _dt(start, 0),       # cycle start
            _dt(start, 10),      # position 1 budget check — fine
            _dt(start, 1200),    # position 2 budget check — 300s left < 600s
            _dt(start, 1201),    # cycle duration
        ])
        monkeypatch.setattr(wheel_engine_module.clock, 'now', lambda: next(times))

        with patch('src.strategy.wheel_engine.CallRoller') as roller_cls:
            roller = roller_cls.return_value
            roller.evaluate_roll_opportunity.return_value = None
            results = engine.run_rolling_cycle()

        assert results['rolls_evaluated'] == 2
        assert roller.evaluate_roll_opportunity.call_count == 1
        skips = [c.args[2] for c in roller.log_terminal_skip.call_args_list]
        assert skips == ['cycle_budget_exhausted']

    def test_the_guard_reserves_the_full_per_position_worst_case(self):
        """1500 s budget, 600 s per position — the constants are the contract,
        so they are asserted rather than left to drift."""
        assert wheel_engine_module._CYCLE_BUDGET_SECONDS == 1500
        assert wheel_engine_module._PER_POSITION_BUDGET_SECONDS == 600
        # The scheduler attempt-deadline is 1800s; the budget must leave room.
        assert wheel_engine_module._CYCLE_BUDGET_SECONDS < 1800


def _dt(day, seconds):
    from datetime import datetime, time
    return datetime.combine(day, time(15, 30)) + timedelta(seconds=seconds)


# =========================================================================== #
# FC-069 S2 (item 10) — reconcile_positions after the wheel_cycles writes.
#
# Both call-assignment branches wrapped an `analytics.write_wheel_cycle` call
# in a swallow-all `try/except`, so the write was never observable to the
# caller — which is exactly what makes its deletion inert. What *is* observable
# in those branches is the wheel-state transition and the trade event; these
# tests pin that, so a regression while removing the write (or a partial
# revert) fails loudly instead of silently dropping an assignment.
#
# Plan: docs/plans/fc-069.md item 10.
# =========================================================================== #

from datetime import datetime  # noqa: E402


class TestReconcileCallAssignmentAfterWheelCyclesRemoval:

    ASSIGNED_CALL = 'MSFT260116C00190000'   # MSFT, call, strike 190.0

    def _engine(self, *, activities, positions, state):
        config = Mock(spec=Config)

        alpaca = Mock()
        alpaca.get_account_activities.return_value = activities
        alpaca.get_positions.return_value = positions

        wheel_state = Mock()
        wheel_state.symbol_states = state
        wheel_state.get_position_summary.side_effect = lambda sym: {
            'symbol': sym,
            'stock_shares': state.get(sym, {}).get('stock_shares', 0),
            'stock_cost_basis': state.get(sym, {}).get('stock_cost_basis', 0),
            'active_puts': state.get(sym, {}).get('active_puts', 0),
            'active_calls': state.get(sym, {}).get('active_calls', 0),
        }

        with patch('src.strategy.wheel_engine.MarketDataManager'):
            engine = WheelEngine(config, alpaca_client=alpaca,
                                 wheel_state=wheel_state)
        return engine, wheel_state

    def _reconcile(self, engine):
        events = []
        with patch('src.strategy.wheel_engine.log_trade_event',
                   side_effect=lambda logger, **kw: events.append(kw)):
            stats = engine.reconcile_positions()
        assert 'error' not in stats, stats
        return stats, events

    # -- activity-driven branch (the OPASN path) ---------------------------- #

    def _activity_engine(self):
        activity = {
            'id': 'act-1',
            'activity_type': 'OPASN',
            'symbol': self.ASSIGNED_CALL,
            'qty': '1',
            'date': '2026-08-03',
            'net_amount': '19000',
        }
        return self._engine(activities=[activity], positions=[], state={})

    def test_activity_branch_still_transitions_wheel_state(self):
        engine, wheel_state = self._activity_engine()
        self._reconcile(engine)

        wheel_state.handle_call_assignment.assert_called_once()
        kwargs = wheel_state.handle_call_assignment.call_args.kwargs
        assert kwargs['symbol'] == 'MSFT'
        assert kwargs['shares'] == 100          # 1 contract -> 100 shares
        assert kwargs['strike_price'] == 190.0
        assert kwargs['assignment_date'] == datetime(2026, 8, 3)

    def test_activity_branch_still_logs_the_assignment_event(self):
        engine, _ = self._activity_engine()
        stats, events = self._reconcile(engine)

        assigned = [e for e in events
                    if e.get('event_type') == 'call_assignment_from_activity']
        assert len(assigned) == 1
        assert assigned[0]['underlying'] == 'MSFT'
        assert assigned[0]['shares'] == 100
        assert assigned[0]['strike_price'] == 190.0
        assert assigned[0]['activity_id'] == 'act-1'
        assert stats['activities_assignments_detected'] == 1

    # -- position-diff branch (the fallback path) --------------------------- #

    def _diff_engine(self):
        # Alpaca: 100 shares and 1 short call left. State: 200 shares, 2 calls.
        # -> calls decreased AND shares decreased => call assignment.
        positions = [
            {'symbol': 'MSFT', 'qty': '100', 'asset_class': 'us_equity'},
            {'symbol': self.ASSIGNED_CALL, 'qty': '-1',
             'asset_class': 'us_option'},
        ]
        state = {'MSFT': {'stock_shares': 200, 'stock_cost_basis': 180.0,
                          'active_puts': 0, 'active_calls': 2}}
        return self._engine(activities=[], positions=positions, state=state)

    def test_position_diff_branch_still_transitions_wheel_state(self):
        engine, wheel_state = self._diff_engine()
        self._reconcile(engine)

        wheel_state.handle_call_assignment.assert_called_once()
        kwargs = wheel_state.handle_call_assignment.call_args.kwargs
        assert kwargs['symbol'] == 'MSFT'
        assert kwargs['shares'] == 100          # 200 tracked - 100 actual
        assert kwargs['strike_price'] == 190.0  # read off the surviving leg
        assert isinstance(kwargs['assignment_date'], datetime)

    def test_position_diff_branch_still_logs_and_syncs_state(self):
        engine, wheel_state = self._diff_engine()
        stats, events = self._reconcile(engine)

        detected = [e for e in events
                    if e.get('event_type') == 'call_assignment_detected']
        assert len(detected) == 1
        assert detected[0]['contracts'] == 1
        assert detected[0]['shares_called'] == 100
        assert stats['call_assignments_detected'] == 1
        # State sync to Alpaca is the other observable half of the branch.
        assert wheel_state.symbol_states['MSFT']['stock_shares'] == 100
        assert wheel_state.symbol_states['MSFT']['active_calls'] == 1

    # -- the removal itself ------------------------------------------------- #

    def test_reconcile_never_reaches_the_analytics_writer(self):
        """The wheel_cycles table was dropped in FC-069; a resurrected write
        would target a table that no longer exists (and would re-duplicate on
        every cold start, which is why it was dropped)."""
        assert not hasattr(wheel_engine_module, 'get_analytics_writer'), (
            "wheel_engine re-imported the analytics writer; the wheel_cycles "
            "writes it fed were removed in FC-069 item 10")

        for factory in (self._activity_engine, self._diff_engine):
            engine, _ = factory()
            with patch('src.data.analytics_writer.get_analytics_writer') as gw:
                self._reconcile(engine)
            assert gw.call_count == 0, (
                "reconcile_positions built an analytics writer")


# =========================================================================== #
# FC-069 S6 (item 8 stage 2) — the untracked-position sweep, end to end, on a
# REAL WheelStateManager.
#
# Every reconcile test above hands the engine a Mock wheel_state, which makes
# the *seeding dict* in reconcile's loop 2 unfalsifiable: a Mock accepts any
# shape, so dropping or renaming a key there passes the whole suite while
# breaking production. The S6 adversarial review demonstrated exactly that —
# deleting `'stock_cost_basis': 0.0` from that dict survived all 1258 tests.
#
# It is not a cosmetic gap. The seeded entry is what a LATER reconcile reads
# when the same instance sees those shares called away, and the consumer sits
# inside a swallow-all `try/except`:
#
#     handle_call_assignment -> state['stock_cost_basis']   # KeyError
#     -> caught -> logger.warning(call_assignment_state_update_failed)
#     -> NO call_assignment event, NO wheel_cycle_complete event
#
# i.e. plan item 8(e)'s named failure mode — "a shrink bug breaks pre-trade
# reconciliation" — arriving silently, as a missing log line rather than an
# error. So this class drives the real object through both halves and asserts
# the seeded shape exactly, key for key.
#
# Plan: docs/plans/fc-069.md item 8.
# =========================================================================== #

from src.strategy.wheel_state_manager import WheelStateManager  # noqa: E402


class TestTheUntrackedPositionSweepSeedsAUsableEntry:

    HELD_CALL = 'MSFT260116C00190000'   # MSFT, call, strike 190.0

    # The contract. Not a subset: an entry missing a key breaks a consumer,
    # and an entry with a *surplus* key means the shrink grew something back.
    SEEDED_KEYS = {
        'stock_shares',
        'stock_cost_basis',
        'acquisition_date',
        'active_puts',
        'active_calls',
        'wheel_cycle_start',
    }

    def _engine(self, positions):
        """A real WheelStateManager, deliberately — see the class comment."""
        config = Mock(spec=Config)
        alpaca = Mock()
        alpaca.get_account_activities.return_value = []
        alpaca.get_positions.return_value = positions

        state = WheelStateManager()
        with patch('src.strategy.wheel_engine.MarketDataManager'):
            engine = WheelEngine(config, alpaca_client=alpaca, wheel_state=state)
        return engine, state, alpaca

    def _held_stock_and_call(self):
        return [
            {'symbol': 'MSFT', 'qty': '100', 'asset_class': 'us_equity',
             'cost_basis': '18000', 'current_price': '185.0'},
            {'symbol': self.HELD_CALL, 'qty': '-1', 'asset_class': 'us_option'},
        ]

    # -- half 1: the seeding itself ---------------------------------------- #

    def test_an_untracked_alpaca_position_is_seeded_with_the_exact_key_set(self):
        engine, state, _ = self._engine(self._held_stock_and_call())

        stats = engine.reconcile_positions()
        assert 'error' not in stats, stats

        entry = state.symbol_states['MSFT']
        assert set(entry) == self.SEEDED_KEYS, (
            "the untracked-position seeding dict drifted from the shape its "
            "consumers read; see reconcile_positions loop 2")
        assert entry == {
            'stock_shares': 100,
            'stock_cost_basis': 0.0,
            'acquisition_date': None,
            'active_puts': 0,
            'active_calls': 1,
            'wheel_cycle_start': None,
        }
        assert stats['state_updates'] == 1
        assert stats['discrepancies_found'] == 1

    def test_the_seeded_entry_survives_a_round_trip_through_both_read_paths(self):
        """Loops 1 and 3 both read the seeded entry back — loop 1 through
        `get_position_summary` plus direct `symbol_states` writes, loop 3
        through the summary again. Neither read is inside a try/except, so a
        shape mismatch raises out to reconcile's outer handler and the whole
        pass returns `{'error': ...}` — silently, since `/run` ignores the
        return value. Hence the `'error' not in stats` assertion.

        Observed while writing this (pre-existing, not introduced by the
        shrink): loop 3's *clearing* branch is unreachable. Loop 1 runs first
        over every tracked symbol, including ones Alpaca no longer reports, and
        zeroes shares/puts/calls on any mismatch — so by the time loop 3 asks
        `has_anything`, the answer is always False and
        `orphaned_state_entries_cleared` can never increment. Recorded, not
        fixed here: it is engine logic, outside this PR's test-only scope.
        """
        engine, state, alpaca = self._engine([
            {'symbol': 'MSFT', 'qty': '100', 'asset_class': 'us_equity',
             'cost_basis': '18000', 'current_price': '185.0'},
        ])
        engine.reconcile_positions()
        assert state.symbol_states['MSFT']['stock_shares'] == 100

        # Alpaca now reports nothing at all.
        alpaca.get_positions.return_value = []
        stats = engine.reconcile_positions()

        assert 'error' not in stats, stats
        assert state.symbol_states['MSFT']['stock_shares'] == 0
        assert state.symbol_states['MSFT']['active_puts'] == 0
        assert state.symbol_states['MSFT']['active_calls'] == 0
        # Key set unchanged by the sync-back writes.
        assert set(state.symbol_states['MSFT']) == self.SEEDED_KEYS
        # The dead branch, pinned as dead so a future fix is a deliberate act.
        assert stats['orphaned_state_entries_cleared'] == 0

    # -- half 2: a call assignment driven through the seeded entry ---------- #

    def _reconcile_capturing_events(self, engine):
        """Collect the state manager's own position events, and any warning
        the engine's swallow-all handlers emit."""
        events = []
        with patch('src.strategy.wheel_state_manager.log_position_update',
                   side_effect=lambda logger, **kw: events.append(kw)), \
             patch.object(wheel_engine_module, 'logger') as engine_logger:
            stats = engine.reconcile_positions()

        self._last_warnings = [call.kwargs
                               for call in engine_logger.warning.call_args_list]
        swallowed = [
            w.get('event_type') for w in self._last_warnings
            if str(w.get('event_type', '')).endswith('_state_update_failed')
        ]
        return stats, events, swallowed

    def _warned(self, event_type):
        return [w for w in getattr(self, '_last_warnings', [])
                if w.get('event_type') == event_type]

    def test_a_call_away_with_a_surviving_leg_still_emits_its_events(self):
        """The end-to-end path: reconcile seeds the entry from Alpaca, then a
        later reconcile on the same instance sees shares called away and must
        transition that entry — emitting the assignment telemetry rather than
        dying inside the swallowed try/except.

        FC-079 changed this fixture. It used to have Alpaca return NOTHING on
        the second pass, which left the strike estimate with no source at all;
        the engine passed 0.0 through and this test asserted the transition
        happened anyway. That is now refused (see
        `test_a_call_away_with_no_strike_source_refuses_the_transition`), so
        the fixture keeps one surviving leg — a partial call-away, 2 contracts
        down to 1 — which is what the branch is actually for and which leaves a
        real strike to read.
        """
        engine, state, alpaca = self._engine(self._held_stock_and_call())
        engine.reconcile_positions()
        assert state.symbol_states['MSFT']['active_calls'] == 1

        # A second contract is written, then one of the two is exercised: 200
        # shares -> 100, 2 calls -> 1. The surviving leg carries the strike.
        state.symbol_states['MSFT']['active_calls'] = 2
        state.symbol_states['MSFT']['stock_shares'] = 200
        alpaca.get_positions.return_value = [
            {'symbol': 'MSFT', 'qty': '100', 'asset_class': 'us_equity',
             'cost_basis': '18000', 'current_price': '185.0'},
            {'symbol': self.HELD_CALL, 'qty': '-1', 'asset_class': 'us_option'},
        ]
        stats, events, swallowed = self._reconcile_capturing_events(engine)

        assert 'error' not in stats, stats
        assert swallowed == [], (
            f"reconcile swallowed a state-update failure: {swallowed}. The "
            "seeded entry is missing a key handle_call_assignment reads.")
        assert self._warned('call_assignment_strike_unresolved') == []

        assigned = [e for e in events if e['event_type'] == 'call_assignment']
        assert len(assigned) == 1, events
        assert assigned[0]['shares'] == 100
        assert assigned[0]['remaining_shares'] == 100
        assert assigned[0]['phase_before'] == 'selling_calls'
        # Seeded entries carry no wheel_cycle_start (reconcile cannot know when
        # the lot was acquired), so the cycle event correctly does NOT fire.
        assert assigned[0]['cycle_duration_days'] == 0
        assert not [e for e in events
                    if e['event_type'] == 'wheel_cycle_complete']

        assert stats['call_assignments_detected'] == 1

    def test_a_call_away_with_no_strike_source_refuses_the_transition(self):
        """FC-079 review fix: never transition on a fabricated 0.0 strike.

        The shape: the last lot is called away, so Alpaca reports neither a
        surviving call leg nor a stock position — there is nothing to estimate
        the strike from. The old code passed `strike_price=0.0` into
        `handle_call_assignment`, which writes `exit_price=0.0` into
        `wheel_cycle_complete` — the one wheel event with a live BigQuery
        consumer — i.e. a fabricated 100% loss on the cycle, indistinguishable
        from a real one downstream.

        The transition is now skipped and the refusal is named. State stays in
        SELLING_CALLS: visibly wrong, and therefore fixable, rather than
        silently wrong and permanent.
        """
        engine, state, alpaca = self._engine(self._held_stock_and_call())
        engine.reconcile_positions()
        assert state.symbol_states['MSFT']['active_calls'] == 1

        # Everything vanishes from Alpaca: no call leg, no shares.
        alpaca.get_positions.return_value = []
        stats, events, _swallowed = self._reconcile_capturing_events(engine)

        assert 'error' not in stats, stats

        warned = self._warned('call_assignment_strike_unresolved')
        assert len(warned) == 1, self._last_warnings
        assert warned[0]['symbol'] == 'MSFT'
        assert warned[0]['shares_called'] == 100
        assert stats['strike_unresolved'] == 1

        # The detection still happened and is still counted — what is refused
        # is the state transition built on a made-up number.
        assert stats['call_assignments_detected'] == 1
        assert [e['event_type'] for e in events
                if e['event_type'] in ('call_assignment',
                                       'wheel_cycle_complete')] == []
        assert not [e for e in events
                    if e['event_type'] == 'wheel_cycle_complete'
                    and e.get('exit_price') == 0.0]

    def test_a_call_away_after_a_put_assignment_completes_the_cycle(self):
        """The other seeding route — `handle_put_assignment` creates the entry
        — must produce an entry of the same shape, and one that DOES carry a
        cycle start, so `wheel_cycle_complete` fires. That is the event with a
        live BigQuery consumer (`options_wheel_logs.wheel_cycles`).
        """
        engine, state, alpaca = self._engine([])
        state.handle_put_assignment('MSFT', 200, 180.0, datetime(2026, 7, 1))
        assert set(state.symbol_states['MSFT']) == self.SEEDED_KEYS, (
            "handle_put_assignment and reconcile's seeding dict must agree on "
            "the entry shape — reconcile reads both through the same code")
        state.symbol_states['MSFT']['active_calls'] = 2

        # FC-079: one of two contracts is exercised, so a call leg survives to
        # supply the strike. Draining Alpaca entirely would now (correctly)
        # refuse the transition rather than complete the cycle at exit_price
        # 0.0 — see
        # `test_a_call_away_with_no_strike_source_refuses_the_transition`.
        alpaca.get_positions.return_value = [
            {'symbol': self.HELD_CALL, 'qty': '-1', 'asset_class': 'us_option'},
        ]
        stats, events, swallowed = self._reconcile_capturing_events(engine)

        assert 'error' not in stats, stats
        assert swallowed == []
        assert self._warned('call_assignment_strike_unresolved') == []
        cycle = [e for e in events if e['event_type'] == 'wheel_cycle_complete']
        assert len(cycle) == 1, events
        assert cycle[0]['cost_basis'] == 180.0
        # The strike came off the surviving leg, not a fallback or a zero.
        assert cycle[0]['exit_price'] == 190.0


# =========================================================================== #
# FC-079 — the reconcile path stops classifying OCC symbols by substring.
#
# `reconcile_positions` counted legs with `'P' in option_symbol` / `elif 'C' in
# option_symbol`, over the WHOLE contract string. Every configured underlying
# whose root contains a 'P' — AAPL, SPY, PFE — therefore had its *calls*
# counted as puts, because the put branch tested first and matched on the root.
# The counts feed the position-diff assignment fallback, so on those symbols a
# call assignment could not be detected on that path at all. The strike
# estimate on the called-away transition had the same defect plus a blind
# `[-8:]` slice.
#
# These tests fail on pre-FC-079 code. `tests/test_no_occ_substring.py` is the
# structural half — it stops the idiom coming back.
#
# Plan: docs/plans/fc-079.md
# =========================================================================== #


class TestReconcileClassifiesLegsStrictly:
    """The counting loop (`_count_option_legs`) and what reconcile does with it."""

    def _engine(self, positions, *, state=None):
        """A real WheelStateManager: the seeded entry is the observable."""
        config = Mock(spec=Config)
        alpaca = Mock()
        alpaca.get_account_activities.return_value = []
        alpaca.get_positions.return_value = positions

        wheel_state = WheelStateManager()
        if state:
            wheel_state.symbol_states.update(state)
        with patch('src.strategy.wheel_engine.MarketDataManager'):
            engine = WheelEngine(config, alpaca_client=alpaca,
                                 wheel_state=wheel_state)
        return engine, wheel_state

    # -- test 1: the headline regression ------------------------------------ #

    def test_an_aapl_call_is_counted_as_a_call_not_a_put(self):
        """AAPL contains a 'P'. Pre-fix this asserted active_puts == 1.

        End-to-end through `reconcile_positions`: the untracked-position sweep
        seeds wheel state straight off the leg counts, so the seeded entry is
        the counting loop's verdict made observable.
        """
        engine, state = self._engine([
            {'symbol': 'AAPL', 'qty': '100', 'asset_class': 'us_equity',
             'cost_basis': '23000', 'current_price': '232.0'},
            {'symbol': 'AAPL260918C00230000', 'qty': '-1',
             'asset_class': 'us_option'},
        ])

        stats = engine.reconcile_positions()

        assert 'error' not in stats, stats
        assert state.symbol_states['AAPL']['active_calls'] == 1
        assert state.symbol_states['AAPL']['active_puts'] == 0

    def test_count_option_legs_returns_the_exact_leg_dict(self):
        """The plan's literal assertion, on the extracted helper."""
        engine, _ = self._engine([])

        counts = engine._count_option_legs([
            {'symbol': 'AAPL260918C00230000', 'qty': '-1'},
        ])

        assert counts == {'AAPL': {'puts': 0, 'calls': 1, 'unclassified': 0}}

    # -- test 2: the rest of the affected roots ----------------------------- #

    def test_pfe_put_counts_as_a_put_and_spy_call_counts_as_a_call(self):
        """PFE and SPY both carry a 'P' in the root, like AAPL."""
        engine, _ = self._engine([])

        counts = engine._count_option_legs([
            {'symbol': 'PFE260918P00025000', 'qty': '-2'},
            {'symbol': 'SPY260918C00600000', 'qty': '-3'},
        ])

        assert counts == {
            'PFE': {'puts': 2, 'calls': 0, 'unclassified': 0},
            'SPY': {'puts': 0, 'calls': 3, 'unclassified': 0},
        }

    def test_a_root_with_neither_letter_is_unchanged(self):
        """The MSFT/GOOGL/NVDA half of the universe must not move."""
        engine, _ = self._engine([])

        counts = engine._count_option_legs([
            {'symbol': 'MSFT260918C00500000', 'qty': '-1'},
            {'symbol': 'GOOGL260918P00180000', 'qty': '-1'},
        ])

        assert counts == {
            'MSFT': {'puts': 0, 'calls': 1, 'unclassified': 0},
            'GOOGL': {'puts': 1, 'calls': 0, 'unclassified': 0},
        }

    # -- test 3: adjusted contracts are counted, never guessed --------------- #

    def test_an_adjusted_symbol_lands_in_unclassified_with_one_warning(self):
        """`AAPL1…` (post-split adjusted root) is not a strict OCC contract.

        It must not be dropped (the position would vanish from the diff, which
        reads as an assignment that did not happen) and must not be guessed
        into a leg (which fabricates one).
        """
        engine, _ = self._engine([])
        adjusted = 'AAPL1260918C00230000'

        with patch.object(wheel_engine_module, 'logger') as log:
            counts = engine._count_option_legs([
                {'symbol': adjusted, 'qty': '-1'},
            ])

        assert counts['AAPL']['unclassified'] == 1
        assert counts['AAPL']['puts'] == 0
        assert counts['AAPL']['calls'] == 0

        warnings = [c.kwargs for c in log.warning.call_args_list
                    if c.kwargs.get('event_type')
                    == 'reconcile_unclassifiable_option']
        assert len(warnings) == 1, log.warning.call_args_list
        assert warnings[0]['symbol'] == adjusted
        assert warnings[0]['underlying'] == 'AAPL'

    def test_the_unclassifiable_warning_fires_once_per_symbol_per_run(self):
        """Two legs of the same adjusted contract: one warning, count of 2."""
        engine, _ = self._engine([])
        adjusted = 'AAPL1260918C00230000'

        with patch.object(wheel_engine_module, 'logger') as log:
            counts = engine._count_option_legs([
                {'symbol': adjusted, 'qty': '-1'},
                {'symbol': adjusted, 'qty': '-1'},
            ])

        assert counts['AAPL']['unclassified'] == 2
        warnings = [c.kwargs for c in log.warning.call_args_list
                    if c.kwargs.get('event_type')
                    == 'reconcile_unclassifiable_option']
        assert len(warnings) == 1

    def test_the_untracked_position_events_carry_the_unclassified_count(self):
        """FC-079 review fix (G5). `untracked_position_found` and its
        `reconciliation_untracked_position` twin are the only telemetry that
        carries the leg counts. Without `unclassified`, a symbol whose legs did
        not classify reads as `puts=0 calls=0` — identical to a bare equity
        holding, on exactly the positions where the operator needs to know the
        engine abstained.
        """
        engine, _state = self._engine([
            {'symbol': 'AAPL', 'qty': '100', 'asset_class': 'us_equity',
             'cost_basis': '23000', 'current_price': '232.0'},
            {'symbol': 'AAPL1260918C00230000', 'qty': '-1',
             'asset_class': 'us_option'},
        ])

        system_events = []
        with patch.object(wheel_engine_module, 'logger') as log, \
             patch('src.strategy.wheel_engine.log_system_event',
                   side_effect=lambda logger, **kw: system_events.append(kw)):
            stats = engine.reconcile_positions()

        assert 'error' not in stats, stats

        found = [c.kwargs for c in log.warning.call_args_list
                 if c.kwargs.get('event_type') == 'untracked_position_found']
        assert len(found) == 1, log.warning.call_args_list
        assert found[0]['symbol'] == 'AAPL'
        assert (found[0]['puts'], found[0]['calls']) == (0, 0)
        assert found[0]['unclassified'] == 1

        twin = [e for e in system_events
                if e.get('event_type') == 'reconciliation_untracked_position']
        assert len(twin) == 1, system_events
        assert twin[0]['unclassified'] == 1

    def test_an_adjusted_symbol_does_not_break_reconciliation(self):
        """No exception, and the pass still completes."""
        engine, state = self._engine([
            {'symbol': 'AAPL', 'qty': '100', 'asset_class': 'us_equity',
             'cost_basis': '23000', 'current_price': '232.0'},
            {'symbol': 'AAPL1260918C00230000', 'qty': '-1',
             'asset_class': 'us_option'},
        ])

        stats = engine.reconcile_positions()

        assert 'error' not in stats, stats
        # Neither leg was credited — the adjusted contract is not a known leg.
        assert state.symbol_states['AAPL']['active_calls'] == 0
        assert state.symbol_states['AAPL']['active_puts'] == 0
        assert state.symbol_states['AAPL']['stock_shares'] == 100


class TestCallAwayStrikeEstimate:
    """Test 4: the strike read on the SELLING_CALLS -> complete transition.

    NOTE ON THE PLAN. `docs/plans/fc-079.md` §Context says this site misfires
    "for `PFE` — that is any PFE option". That is wrong, and it was checked
    against the tree rather than taken on trust: the pre-fix predicate is
    `'C' in opt_sym`, and **PFE contains no C** — nor does any other root in
    either configured universe (AAPL/SPY/PFE are the `'P'` roots, which is what
    breaks the *counting* loop, a separate site). So on today's config this
    site is a LATENT defect, not a live one, and a PFE test cannot demonstrate
    it. What does: any root containing a C. `CVX` below stands in for the day
    someone adds one — the whole point of the FC-041/043/045/048/052/054
    family is that this class of bug arrives with a config change, silently.
    The PFE case is pinned too, as the unaffected control.
    """

    def _engine(self, positions, state):
        config = Mock(spec=Config)
        alpaca = Mock()
        alpaca.get_account_activities.return_value = []
        alpaca.get_positions.return_value = positions

        wheel_state = Mock()
        wheel_state.symbol_states = state
        wheel_state.get_position_summary.side_effect = lambda sym: {
            'symbol': sym,
            'stock_shares': state.get(sym, {}).get('stock_shares', 0),
            'stock_cost_basis': state.get(sym, {}).get('stock_cost_basis', 0),
            'active_puts': state.get(sym, {}).get('active_puts', 0),
            'active_calls': state.get(sym, {}).get('active_calls', 0),
        }
        with patch('src.strategy.wheel_engine.MarketDataManager'):
            engine = WheelEngine(config, alpaca_client=alpaca,
                                 wheel_state=wheel_state)
        return engine, wheel_state

    def _called_away(self, root, extra_positions, price):
        positions = [
            {'symbol': root, 'qty': '100', 'asset_class': 'us_equity',
             'cost_basis': '10000', 'current_price': price},
        ] + extra_positions
        state = {root: {'stock_shares': 200, 'stock_cost_basis': 100.0,
                        'active_puts': 1, 'active_calls': 2}}
        engine, wheel_state = self._engine(positions, state)
        with patch('src.strategy.wheel_engine.log_trade_event'):
            stats = engine.reconcile_positions()
        assert 'error' not in stats, stats
        wheel_state.handle_call_assignment.assert_called_once()
        return wheel_state.handle_call_assignment.call_args.kwargs

    def test_a_c_bearing_root_reads_the_strike_off_the_call_not_the_put(self):
        """Pre-fix this returns 150.0 — the PUT's strike.

        `'C' in 'CVX260918P00150000'` is true (the root supplies the C), so the
        pre-fix loop stopped on the first CVX contract it saw and sliced its
        last eight digits. The put is listed first here deliberately: Alpaca
        promises no ordering, so pre-fix the answer depended on it.
        """
        kwargs = self._called_away('CVX', [
            {'symbol': 'CVX260918P00150000', 'qty': '-1',
             'asset_class': 'us_option'},
            {'symbol': 'CVX260918C00160000', 'qty': '-1',
             'asset_class': 'us_option'},
        ], price='155.0')

        assert kwargs['symbol'] == 'CVX'
        assert kwargs['strike_price'] == 160.0

    def test_pfe_the_unaffected_control_is_unchanged(self):
        """PFE has no C, so this site was already correct for it. Pinned so a
        future 'simplification' cannot regress the roots that did work."""
        kwargs = self._called_away('PFE', [
            {'symbol': 'PFE260918P00025000', 'qty': '-1',
             'asset_class': 'us_option'},
            {'symbol': 'PFE260918C00030000', 'qty': '-1',
             'asset_class': 'us_option'},
        ], price='29.0')

        assert kwargs['strike_price'] == 30.0

    def test_an_adjusted_leg_falls_through_to_the_price_estimate(self):
        """Unclassifiable => skipped, not guessed; the fallback takes over."""
        positions = [
            {'symbol': 'PFE', 'qty': '100', 'asset_class': 'us_equity',
             'cost_basis': '2800', 'current_price': '29.5'},
            {'symbol': 'PFE1260918C00030000', 'qty': '-1',
             'asset_class': 'us_option'},
        ]
        state = {'PFE': {'stock_shares': 200, 'stock_cost_basis': 28.0,
                         'active_puts': 0, 'active_calls': 2}}
        engine, wheel_state = self._engine(positions, state)

        with patch('src.strategy.wheel_engine.log_trade_event'):
            stats = engine.reconcile_positions()

        assert 'error' not in stats, stats
        kwargs = wheel_state.handle_call_assignment.call_args.kwargs
        assert kwargs['strike_price'] == 29.5   # current_price fallback


class TestPositionDiffCallAssignmentOnAffectedRoots:
    """Test 5: the S2 position-diff branch, re-run on AAPL instead of MSFT.

    `TestReconcileCallAssignmentAfterWheelCyclesRemoval` above pins this branch
    on MSFT — a root with no P and no C, so it passed throughout the bug's
    life. On AAPL the identical scenario could not fire at all pre-fix: the
    surviving call counted as a put, so `tracked_calls > actual_calls` was
    false and the branch never ran. The MSFT class is deliberately left in
    place; this is the same contract on an affected root.
    """

    ASSIGNED_CALL = 'AAPL260918C00230000'   # AAPL, call, strike 230.0

    def _diff_engine(self):
        config = Mock(spec=Config)
        alpaca = Mock()
        alpaca.get_account_activities.return_value = []
        alpaca.get_positions.return_value = [
            {'symbol': 'AAPL', 'qty': '100', 'asset_class': 'us_equity'},
            {'symbol': self.ASSIGNED_CALL, 'qty': '-1',
             'asset_class': 'us_option'},
        ]

        state = {'AAPL': {'stock_shares': 200, 'stock_cost_basis': 220.0,
                          'active_puts': 0, 'active_calls': 2}}
        wheel_state = Mock()
        wheel_state.symbol_states = state
        wheel_state.get_position_summary.side_effect = lambda sym: {
            'symbol': sym,
            'stock_shares': state.get(sym, {}).get('stock_shares', 0),
            'stock_cost_basis': state.get(sym, {}).get('stock_cost_basis', 0),
            'active_puts': state.get(sym, {}).get('active_puts', 0),
            'active_calls': state.get(sym, {}).get('active_calls', 0),
        }
        with patch('src.strategy.wheel_engine.MarketDataManager'):
            engine = WheelEngine(config, alpaca_client=alpaca,
                                 wheel_state=wheel_state)
        return engine, wheel_state

    def test_position_diff_branch_fires_on_aapl(self):
        engine, wheel_state = self._diff_engine()

        events = []
        with patch('src.strategy.wheel_engine.log_trade_event',
                   side_effect=lambda logger, **kw: events.append(kw)):
            stats = engine.reconcile_positions()

        assert 'error' not in stats, stats
        assert stats['call_assignments_detected'] == 1
        wheel_state.handle_call_assignment.assert_called_once()
        kwargs = wheel_state.handle_call_assignment.call_args.kwargs
        assert kwargs['symbol'] == 'AAPL'
        assert kwargs['shares'] == 100
        assert kwargs['strike_price'] == 230.0

        detected = [e for e in events
                    if e.get('event_type') == 'call_assignment_detected']
        assert len(detected) == 1
        assert detected[0]['contracts'] == 1
        assert wheel_state.symbol_states['AAPL']['active_calls'] == 1


class TestClassShareCallsReachTheRoller(_CycleFixture):
    """FC-041(2) review finding F3 — the roller's stock lookup.

    `run_rolling_cycle` keyed `stock_by_symbol` on the raw equity symbol
    (`BRK.B`) and looked it up by the short call's parsed underlying (`BRKB`).
    A fully covered class-share call therefore landed in `uncovered_calls` and
    was reported `no_covering_shares` — the one position shape that reads as
    "genuinely naked short call" — and was never rolled. 100 shares were
    sitting behind it the whole time.

    MUTATION CHECK: revert the `occ_root` keying and
    `test_a_covered_class_share_call_is_rollable` fails.
    """

    def _class_share_book(self):
        return [
            {'symbol': 'BRKB260807C00450000', 'qty': '-1',
             'asset_class': 'us_option'},
            {'symbol': 'BRK.B', 'qty': '100', 'asset_class': 'us_equity',
             'avg_entry_price': '400.00', 'side': 'long'},
        ]

    def _run(self, positions):
        config = Mock(spec=Config)
        config.rolling_enabled = True
        config.earnings_enabled = False

        alpaca = Mock()
        alpaca.get_positions.return_value = positions
        alpaca.get_orders.return_value = []

        with patch('src.strategy.wheel_engine.MarketDataManager'):
            engine = WheelEngine(config, alpaca_client=alpaca,
                                 wheel_state=Mock())

        seen = {'evaluated': [], 'skipped': []}

        class _StubRoller:
            def __init__(self, *a, **kw):
                pass

            def log_terminal_skip(self, option_symbol, underlying, reason, **kw):
                seen['skipped'].append((option_symbol, underlying, reason))

            def evaluate_roll_opportunity(self, call_pos, stock_pos,
                                          open_syms=None):
                seen['evaluated'].append(
                    (call_pos['symbol'], stock_pos['symbol']))
                return None

            def execute_roll(self, opportunity):   # pragma: no cover
                raise AssertionError("not reached")

        with patch('src.strategy.wheel_engine.CallRoller', _StubRoller):
            with patch('src.strategy.wheel_engine.log_error_event'):
                results = engine.run_rolling_cycle()
        return results, seen

    def test_a_covered_class_share_call_is_rollable(self):
        """THE F3 REGRESSION. Pre-fix `evaluated` is empty and the call is
        reported `no_covering_shares`."""
        _results, seen = self._run(self._class_share_book())

        assert seen['evaluated'] == [('BRKB260807C00450000', 'BRK.B')], (
            "the covered class-share call never reached the roller")
        assert [s for s in seen['skipped'] if s[2] == 'no_covering_shares'] == []

    def test_a_genuinely_naked_class_share_call_is_still_reported(self):
        """The fix must not blind the uncovered path: with no equity leg the
        call is still `no_covering_shares`."""
        _results, seen = self._run([
            {'symbol': 'BRKB260807C00450000', 'qty': '-1',
             'asset_class': 'us_option'},
        ])

        assert seen['evaluated'] == []
        assert [s[2] for s in seen['skipped']] == ['no_covering_shares']

    def test_a_different_share_class_does_not_count_as_coverage(self):
        """BRK.A shares must not make a BRKB call look covered."""
        _results, seen = self._run([
            {'symbol': 'BRKB260807C00450000', 'qty': '-1',
             'asset_class': 'us_option'},
            {'symbol': 'BRK.A', 'qty': '100', 'asset_class': 'us_equity',
             'avg_entry_price': '700000.00', 'side': 'long'},
        ])

        assert seen['evaluated'] == []
        assert [s[2] for s in seen['skipped']] == ['no_covering_shares']

    def test_plain_tickers_still_pair_with_their_shares(self):
        """The behavior contract: nothing moves for the live universe."""
        _results, seen = self._run(self._book(['AAPL']))

        assert seen['evaluated'] == [
            (_occ('AAPL', self.EXPIRY, 100.0), 'AAPL')]
