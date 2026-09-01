"""RejectionTally must be able to name every stage that can block a day.

FC-057: stage 1 was missing. A symbol blocked on the price band or volume floor
produced NO tally entry, so a screen reported it as simply having done nothing —
`insufficient`, 0% days in position, no reason given. That is how a $400
`max_stock_price` silently excluded SPY, QQQ and AMD for months while their
verdicts were read as strategy results rather than config exclusions.

The event was always emitted with full detail (`stock_rejected_filter`, carrying
the price and the band it missed). Nothing counted it.

FC-068 is the same failure mode, caught before it shipped. Repointing the replay
onto the production pipeline changed the vocabulary the tally listens for: the
old table counted `no_suitable_puts`, whose ONLY emitter lived inside the
deleted `put_seller.find_put_opportunity`, while the scanner-path event for an
empty put chain — `stage_7_complete_not_found` — was unmapped. Built without the
correction, "no put cleared delta/DTE/premium" (the constraint that makes
F/PFE/KMI/VZ untradeable, and a `binding_constraint` value in `backtest_runs`)
would have produced zero tally entries. Its call-side twin
`stage_8_complete_not_found` is mapped for the same reason.
"""

import pytest

from src.backtesting.engine.rejections import (
    _REASONS,
    _SELECTION_DROP_REASONS,
    RejectionTally,
)
from src.strategy.execution_engine import DROP_REASONS
from src.utils import clock
from datetime import datetime


# Every stage the LIVE pipeline can block at, and the event it emits when it
# does. A stage missing from _REASONS is invisible in every report the engine
# writes. Each entry names the emitter so a future deletion has to confront it.
STAGE_EVENTS = {
    # market_data.filter_suitable_stocks
    "stock_rejected_filter": "stage 1 — price/volume band",
    # market_data.find_suitable_puts — the put leg's binding constraint
    "stage_7_complete_not_found": "stage 7 — no put cleared the bands",
    # market_data.find_suitable_calls — the call leg's
    "stage_8_complete_not_found": "stage 8 — no call cleared the bands",
    # put_seller._calculate_position_size
    "stage_8_blocked": "stage 8 — position sizing",
    # options_scanner.scan_for_call_opportunities
    "call_scan_skipped_cost_basis_unresolved": "scan — floor unresolved",
    "call_scan_skipped_cost_basis_divergent": "scan — floor divergent",
    "call_scan_skipped_quote_unavailable": "scan — quote unusable",
    # execution_engine.execute_batch
    "naked_call_blocked": "execution — insufficient shares",
    "unroutable_opportunity": "execution — unroutable",
}

# Deleted with the engine path (FC-068). Left in _REASONS these would read as
# coverage while counting nothing — a table row with no emitter is worse than
# an absent one, because it looks like the stage is watched.
RETIRED_EVENTS = [
    "stock_filtered_by_gap_risk",
    "rejected_high_gap_frequency",
    "stage_4_blocked",
    "stage_5_blocked",
    "stage_6_blocked",
    "put_blocked_by_wheel_state",
    "covered_call_drawdown_pause",
    "no_suitable_puts",
    "position_size_validation_failed",
]


class TestEveryBlockingStageIsNameable:
    @pytest.mark.parametrize("event,stage", sorted(STAGE_EVENTS.items()))
    def test_stage_has_a_tally_reason(self, event, stage):
        assert event in _REASONS, (
            f"{stage} emits {event!r} but RejectionTally cannot name it — a day "
            f"blocked there shows as 'no reason', which is how the price-band "
            f"exclusion of SPY/QQQ/AMD stayed invisible (FC-057)"
        )

    def test_stage_1_is_the_one_that_was_missing(self):
        """Pins the specific regression rather than only the general rule."""
        assert _REASONS["stock_rejected_filter"] == "price/volume band (stage 1)"

    @pytest.mark.parametrize("event", RETIRED_EVENTS)
    def test_a_dead_event_is_not_still_listed(self, event):
        assert event not in _REASONS, (
            f"{event!r} has no emitter after FC-068 but is still in the tally "
            f"table — it will never fire, and its presence implies a stage is "
            f"being watched that no longer exists"
        )


class TestTallyCountsStageOne:
    def test_a_price_band_rejection_becomes_a_named_reason(self):
        tally = RejectionTally()
        with clock.frozen(datetime(2026, 3, 2, 16, 0)):
            tally.processor(None, "info", {
                "event_type": "stock_rejected_filter",
                "symbol": "SPY",
                "reasons": ["price $736.47 outside $10.0-$400.0"],
            })

        summary = tally.summary()
        assert summary.get("price/volume band (stage 1)") == 1, (
            f"stage-1 block not counted; summary={summary!r}"
        )

    def test_an_unmapped_event_is_still_ignored(self):
        """The tally names known stages; it must not invent reasons."""
        tally = RejectionTally()
        with clock.frozen(datetime(2026, 3, 2, 16, 0)):
            tally.processor(None, "info", {"event_type": "some_unrelated_event"})

        assert tally.summary() == {}


class TestTheChainEmptyEventsAreNamed:
    """FC-068's headline taxonomy correction.

    These two events are the most common no-trade outcomes in production — the
    $0.50 premium floor on the put side, the cost-basis floor on the call side.
    Both were emitted on every scan; neither was counted.
    """

    def _one(self, event_type):
        tally = RejectionTally()
        with clock.frozen(datetime(2026, 3, 2, 16, 0)):
            tally.processor(None, "info", {"event_type": event_type,
                                           "symbol": "PFE"})
        return tally.summary()

    def test_an_empty_put_chain_becomes_a_named_put_side_reason(self):
        """Fails against the pre-FC-068 table, where this event was unmapped
        and `no_suitable_puts` (emitter deleted) held the bucket."""
        summary = self._one("stage_7_complete_not_found")
        assert summary == {"no put cleared delta/DTE/premium (stage 7)"
                           : 1}, f"summary={summary!r}"

    def test_an_empty_call_chain_becomes_a_named_call_side_reason(self):
        summary = self._one("stage_8_complete_not_found")
        assert summary == {
            "no call cleared floor/delta/DTE/premium (stage 8, scan)": 1
        }, f"summary={summary!r}"

    def test_candidate_days_still_track_the_found_case(self):
        """`stage_7_complete_found` is the positive twin and must keep working
        — the scanner drives `find_suitable_puts` just as the seller did."""
        tally = RejectionTally()
        with clock.frozen(datetime(2026, 3, 2, 16, 0)):
            tally.processor(None, "info", {"event_type": "stage_7_complete_found"})
        assert tally.candidate_days == 1


class TestSelectionDropsAreBucketedByReason:
    """FC-068 made batch selection the stage that decides what trades.

    `selection_dropped` is ONE event carrying a `reason` field, so a flat
    _REASONS row would collapse every drop into a single bucket and the tally
    would be blind to *why* the deciding stage decided — the FC-057 failure
    mode at a new stage.
    """

    def _drop(self, reason):
        tally = RejectionTally()
        with clock.frozen(datetime(2026, 3, 2, 16, 0)):
            tally.processor(None, "info", {
                "event_type": "selection_dropped",
                "reason": reason,
                "stage": "select_batch",
                "symbol": "AAPL",
            })
        return tally.summary()

    def test_selection_drop_reasons_are_tallied(self):
        assert self._drop("insufficient_buying_power") == {
            "selection: insufficient buying power": 1
        }

    @pytest.mark.parametrize("reason", DROP_REASONS)
    def test_every_closed_enum_value_has_a_bucket(self, reason):
        """The enum is exhaustive by construction in execution_engine; the
        tally must track it or a new drop reason goes silently uncounted."""
        assert reason in _SELECTION_DROP_REASONS
        assert len(self._drop(reason)) == 1

    def test_two_reasons_on_one_day_are_two_buckets(self):
        tally = RejectionTally()
        with clock.frozen(datetime(2026, 3, 2, 16, 0)):
            for reason in ("insufficient_buying_power", "duplicate_underlying"):
                tally.processor(None, "info", {"event_type": "selection_dropped",
                                               "reason": reason})
        assert set(tally.summary()) == {
            "selection: insufficient buying power",
            "selection: duplicate underlying",
        }

    def test_a_selection_drop_with_no_reason_is_not_invented(self):
        """A malformed event must not become a mystery bucket."""
        tally = RejectionTally()
        with clock.frozen(datetime(2026, 3, 2, 16, 0)):
            tally.processor(None, "info", {"event_type": "selection_dropped"})
        assert tally.summary() == {}


class TestTheEarningsGateEventsAreNamed:
    """FC-013 §5 — test 18.

    A replay day where the earnings gate blocked everything must say so. The
    FC-057 failure mode is a verdict that reads "this symbol did nothing" when
    the truth is "a filter excluded it" — and an earnings-week symbol is
    excluded on every scan of that week.
    """

    def _one(self, event_type, symbol="AMZN"):
        tally = RejectionTally()
        with clock.frozen(datetime(2026, 7, 30, 16, 0)):
            tally.processor(None, "info", {"event_type": event_type,
                                           "symbol": symbol})
        return tally.summary()

    def test_a_put_side_blackout_becomes_a_named_reason(self):
        """Fails against the pre-FC-013 table, where this event was unmapped."""
        assert self._one("put_scan_skipped_earnings_blackout") == {
            "earnings blackout (scan, put)": 1}

    def test_a_call_side_span_emptied_chain_becomes_a_named_reason(self):
        """The label says what the event now MEANS post-rev-2.2: the span
        predicate removed every qualifying strike, not "the symbol is within
        N days"."""
        assert self._one("call_scan_skipped_earnings_blackout") == {
            "earnings span emptied the chain (scan, call)": 1}

    @pytest.mark.parametrize("event", [
        "put_scan_skipped_earnings_unknown",
        "call_scan_skipped_earnings_unknown",
    ])
    def test_the_unknown_events_are_deliberately_unmapped(self, event):
        """Not an oversight — do not "fix" this.

        In a replay the historical calendar never returns unknown (table-backed,
        fail-open on gaps and reported instead), so a mapping would be an entry
        with no replay emitter: coverage that counts nothing, which is exactly
        what FC-068's rewrite of this table removed.
        """
        assert event not in _REASONS
        assert self._one(event) == {}


class TestTheExistingPositionSkipIsNamed:
    """FC-069 item 12 ride-along.

    BACKTEST_ENGINE.md's accepted gap #7: the put-side "we already hold this
    underlying" skip was silent, so a replay day it blocked read as a symbol
    that did nothing — the FC-057 failure mode. The live scanner now emits, so
    the replay (same scanner since FC-068) emits too, and it gets a bucket.
    """

    def _one(self, event_dict):
        tally = RejectionTally()
        with clock.frozen(datetime(2026, 7, 30, 16, 0)):
            tally.processor(None, "info", event_dict)
        return tally.summary()

    def test_the_skip_becomes_a_named_reason(self):
        """Fails against the pre-FC-069 table, where this event did not exist
        at all — neither emitted nor mapped."""
        assert self._one({"event_type": "put_scan_skipped_existing_position",
                          "symbol": "F", "reason": "option_position"}) == {
            "already holds this underlying (scan, put)": 1}

    def test_the_api_error_sibling_is_deliberately_unmapped(self):
        """Do not "fix" this either: `position_check_failed` means the
        positions call failed, not that a position exists. Sharing a bucket
        would file an outage under "we already hold it"."""
        assert "position_check_failed" not in _REASONS
        assert self._one({"event_type": "position_check_failed",
                          "symbol": "F"}) == {}


# ==========================================================================
# FC-092 — the tally must survive `cache_logger_on_first_use=True`
# ==========================================================================
class TestNSequentialRunsInOneProcessEachGetACompleteTally:
    """The measured defect, and the contract that replaces it.

    Measured on the pre-fix tree at 7087007, two `evaluate_symbol` calls in one
    process::

        call #1: blocked_days_by_reason={'already holds this underlying...': 16,
                                         'selection: duplicate underlying': 4}
        call #2: blocked_days_by_reason={}

    The mechanism: `setup_logging` sets `cache_logger_on_first_use=True`, a
    `BoundLoggerLazyProxy` caches its entire processor chain the first time it
    is used, and `structlog.configure()` — which was how `RejectionTally`
    installed itself — does not invalidate that cache. Every strategy logger
    therefore bound run #1's tally instance and delivered to it for the life of
    the process. The monthly screen replays 14 symbols in one process, so 13 of
    every 14 `backtest_runs` rows carried an empty tally and a NULL
    `binding_constraint`.

    These tests reproduce the caching flag EXACTLY — a run with
    `cache_logger_on_first_use=False` passes against the broken code and proves
    nothing — and restore the configuration in a `finally`, per
    `conftest._logging_config_is_not_leaked`.
    """

    EXPECTED = {
        "no put cleared delta/DTE/premium (stage 7)": 1,
        "selection: duplicate underlying": 1,
    }

    @staticmethod
    def _caching_structlog_config():
        """`setup_logging`'s chain, including the flag, WITHOUT its side effects.

        Not `setup_logging()` itself: that installs a root FileHandler on
        `logs/options_wheel.log` and never restores it (see
        `tests/test_scenarios.py::_cli_structlog_config` for the incident).
        What is reproduced is the part the defect needs — the dispatch at the
        front, `filter_by_level` behind it, and the caching flag.
        """
        import contextlib

        import structlog

        from src.utils.logger import tally_dispatch

        @contextlib.contextmanager
        def _ctx():
            previous = structlog.get_config()
            try:
                structlog.configure(
                    processors=[
                        tally_dispatch,
                        structlog.contextvars.merge_contextvars,
                        structlog.stdlib.filter_by_level,
                        structlog.stdlib.add_log_level,
                        structlog.processors.JSONRenderer(),
                    ],
                    context_class=dict,
                    logger_factory=structlog.stdlib.LoggerFactory(),
                    wrapper_class=structlog.stdlib.BoundLogger,
                    cache_logger_on_first_use=True,
                )
                yield
            finally:
                structlog.configure(**previous)

        return _ctx()

    @staticmethod
    def _emit(logger):
        logger.info("blocked", event_type="stage_7_complete_not_found", symbol="F")
        logger.info("dropped", event_type="selection_dropped",
                    reason="duplicate_underlying", symbol="F")

    @classmethod
    def _one_run(cls, logger, day):
        """One replay's worth of events through a cached logger proxy."""
        tally = RejectionTally()
        with clock.frozen(day):
            with tally:
                cls._emit(logger)
        return tally

    def test_two_runs_in_one_process_get_equal_non_empty_tallies(self):
        """THE contract from the plan: N sequential `run_sweep` calls in one
        process each get their own complete tally.

        The second run's `{}` is the exact measured symptom; equality of two
        non-empty summaries is the exact repair.
        """
        import logging
        import uuid

        import structlog

        name = f"src.fc092_probe_{uuid.uuid4().hex}"
        try:
            with self._caching_structlog_config():
                # ONE proxy, used across both runs — this is the strategy
                # module's `logger = structlog.get_logger(__name__)`, which is
                # created at import and caches its chain on first use.
                proxy = structlog.get_logger(name)
                first = self._one_run(proxy, datetime(2026, 7, 30, 16, 0))
                second = self._one_run(proxy, datetime(2026, 7, 31, 16, 0))

            assert first.summary() == self.EXPECTED
            assert second.summary() == self.EXPECTED, (
                "the SECOND run in this process got a different tally — the "
                "cached logger proxy is still bound to run #1's tally instance "
                "(FC-092 item 1). 13 of every 14 monthly screen rows depend on "
                "this."
            )
        finally:
            logging.Logger.manager.loggerDict.pop(name, None)

    def test_the_fifth_run_is_as_complete_as_the_first(self):
        """The screen replays 14 symbols, not 2. Two runs could pass on a fix
        that merely swapped a single cached binding once."""
        import logging
        import uuid

        import structlog

        name = f"src.fc092_probe_{uuid.uuid4().hex}"
        try:
            with self._caching_structlog_config():
                proxy = structlog.get_logger(name)
                summaries = [
                    self._one_run(proxy, datetime(2026, 7, 20 + i, 16, 0)).summary()
                    for i in range(5)
                ]
            assert summaries == [self.EXPECTED] * 5, summaries
        finally:
            logging.Logger.manager.loggerDict.pop(name, None)

    def test_a_tally_stops_counting_once_it_exits(self):
        """The binding is scoped, not merely swapped. An event emitted between
        two replays must land in neither tally, or a sweep's per-cell numbers
        absorb the runner's own housekeeping.

        The stray event is on a DIFFERENT simulated day from the run's, so a
        leak shows up as a count of 2 rather than being absorbed by the tally's
        (day, reason) de-duplication.
        """
        import logging
        import uuid

        import structlog

        name = f"src.fc092_probe_{uuid.uuid4().hex}"
        try:
            with self._caching_structlog_config():
                proxy = structlog.get_logger(name)
                tally = self._one_run(proxy, datetime(2026, 7, 30, 16, 0))
                with clock.frozen(datetime(2026, 7, 31, 16, 0)):
                    self._emit(proxy)
            assert tally.summary() == self.EXPECTED
        finally:
            logging.Logger.manager.loggerDict.pop(name, None)

    def test_the_dispatch_sits_ahead_of_filter_by_level(self):
        """D11's claim, restated for the new mechanism: a quieted sweep raises
        the strategy loggers to WARNING, and the tally must still count the INFO
        events the stdlib level then drops."""
        import logging
        import uuid

        import structlog

        name = f"src.fc092_probe_{uuid.uuid4().hex}"
        probe = logging.getLogger(name)
        probe.setLevel(logging.WARNING)
        try:
            with self._caching_structlog_config():
                tally = self._one_run(structlog.get_logger(name),
                                      datetime(2026, 7, 30, 16, 0))
            assert tally.summary() == self.EXPECTED
        finally:
            probe.setLevel(logging.NOTSET)
            logging.Logger.manager.loggerDict.pop(name, None)

    def test_setup_logging_installs_the_dispatch_first(self):
        """Production's chain, asserted by construction rather than by replay.

        The dispatch must be in the chain `setup_logging` builds — otherwise a
        strategy logger that logged once before the first replay caches a chain
        without it and is blind for the life of the process — and it must be
        FIRST, ahead of `filter_by_level`.
        """
        import inspect

        from src.utils import logger as logger_module

        source = inspect.getsource(logger_module.setup_logging)
        chain = source.split("processors=[", 1)[1]
        first = chain.split(",", 1)[0].strip()
        assert first == "tally_dispatch", (
            f"setup_logging's first processor is {first!r}; the tally dispatch "
            "must be first, or a quieted sweep counts nothing"
        )

    def test_a_tally_that_never_configured_structlog_restores_what_it_found(self):
        """The tally must not leak a reconfiguration into the process.

        `ensure_tally_dispatch` reconfigures only when the dispatch is absent —
        a test process, mostly — and `__exit__` puts back what it found.
        `conftest._logging_config_is_not_leaked` would catch a leak here, but it
        would blame whichever test ran next.
        """
        import structlog

        before = structlog.get_config()
        with RejectionTally():
            pass
        assert structlog.get_config() == before


class TestTheSummaryOrderIsDeterministic:
    """FC-092 item 2. `summary()` iterated a SET into a Counter, and
    `most_common()` preserves insertion order among equal counts — so
    `PYTHONHASHSEED` decided which of two equally-blocking reasons was reported
    as `binding_constraint` and which led the stood-down table. FC-060 Layer 2's
    identity proof had to pin `PYTHONHASHSEED=0` to work around it.
    """

    @staticmethod
    def _tally_with(pairs):
        tally = RejectionTally()
        for day, reason in pairs:
            tally._seen.add((datetime(2026, 7, day, 16, 0).date(), reason))
        return tally

    def test_ties_break_alphabetically_not_by_insertion_order(self):
        pairs = [(1, "zebra"), (2, "alpha"), (3, "middle")]
        forward = self._tally_with(pairs).summary()
        backward = self._tally_with(list(reversed(pairs))).summary()
        assert list(forward) == ["alpha", "middle", "zebra"]
        assert list(forward) == list(backward)

    def test_counts_still_dominate_the_order(self):
        """The tiebreak must not outrank the count: the reason that blocked the
        most days is still first, whatever it is called."""
        tally = self._tally_with([(1, "zebra"), (2, "zebra"), (3, "alpha")])
        assert list(tally.summary()) == ["zebra", "alpha"]
        assert tally.binding_constraint() == "zebra"

    def test_binding_constraint_agrees_with_the_summary_order(self):
        """They read the same ranking, so the column and the table can never
        name different reasons for the same replay."""
        tally = self._tally_with([(1, "zebra"), (2, "alpha"), (3, "middle")])
        assert tally.binding_constraint() == next(iter(tally.summary()))

    def test_the_order_survives_a_different_hash_seed(self):
        """Asserted across PROCESSES, because that is where the seed changes.

        Two subprocesses with different `PYTHONHASHSEED`s build the same tally
        and must print the same ordering. The pre-fix code alternated between
        two answers here, which is why an identity proof had to pin the seed.
        """
        import json
        import os
        import subprocess
        import sys
        from pathlib import Path

        program = (
            "import json\n"
            "from datetime import datetime\n"
            "from src.backtesting.engine.rejections import RejectionTally\n"
            "t = RejectionTally()\n"
            "for i, r in enumerate(['zebra', 'alpha', 'middle', 'beta']):\n"
            "    t._seen.add((datetime(2026, 7, i + 1, 16, 0).date(), r))\n"
            "print(json.dumps(list(t.summary())))\n"
        )
        repo_root = Path(__file__).resolve().parent.parent
        seen = set()
        for seed in ("0", "1", "9999"):
            env = dict(os.environ, PYTHONHASHSEED=seed,
                       PYTHONDONTWRITEBYTECODE="1")
            proc = subprocess.run([sys.executable, "-B", "-c", program],
                                  cwd=str(repo_root), env=env,
                                  capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr[-2000:]
            seen.add(tuple(json.loads(proc.stdout.strip().splitlines()[-1])))
        assert len(seen) == 1, (
            f"the summary ordering depends on PYTHONHASHSEED: {seen}"
        )
