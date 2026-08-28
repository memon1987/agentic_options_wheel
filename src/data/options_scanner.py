"""Options scanning utilities for wheel strategy opportunity identification."""

from datetime import date
from typing import Dict, List, Any, Optional, Tuple
import pandas as pd
import structlog
import numpy as np

from ..api.alpaca_client import AlpacaClient
from ..api.earnings_calendar import (
    EARNINGS_CLEAR,
    EARNINGS_KNOWN,
    EARNINGS_UNKNOWN,
)
from ..api.market_data import MarketDataManager
from ..utils.config import Config
from ..utils import clock
from ..utils.logging_events import log_performance_metric, log_error_event
from ..utils.option_symbols import parse_option_symbol
from ..utils.positions import get_stock_positions
from ..strategy.cost_basis import CostBasisResolver, SOURCE_DIVERGENT
from .decision_record import (
    OUTCOME_BLOCKED,
    OUTCOME_NOT_ELIGIBLE,
    REASON_EXCLUDED_BY_CONFIG,
    OUTCOME_NO_CANDIDATES,
    REASON_EARNINGS_BLACKOUT,
    REASON_EARNINGS_UNKNOWN,
    REASON_FLOOR_DIVERGENT,
    REASON_FLOOR_UNRESOLVED,
    REASON_INSUFFICIENT_SHARES,
    REASON_NO_QUALIFYING_STRIKES,
    REASON_OPPORTUNITY_BUILD_FAILED,
    REASON_QUOTE_UNAVAILABLE,
    STAGE_SCAN,
    DecisionRecorder,
    UncoveredDaysResolver,
    covered_underlyings,
    mint_run_id,
    position_mark_price,
    stamp_decision_labels,
    underwater_pct,
)

logger = structlog.get_logger(__name__)


class OptionsScanner:
    """Scanner for identifying options wheel trading opportunities."""
    
    def __init__(self, alpaca_client: AlpacaClient, market_data: MarketDataManager,
                 config: Config, allow_bigquery: bool = True,
                 earnings_calendar: Optional[object] = None):
        """Initialize options scanner.

        Args:
            alpaca_client: Alpaca API client
            market_data: Market data manager
            config: Configuration instance
            earnings_calendar: FC-013 — the calendar the earnings gate consults.
                Same explicit-injection seam as ``allow_bigquery`` below, and
                for the same reason: a ``clock.is_frozen()``-keyed data-source
                policy is action at a distance, and ``is_frozen`` is
                thread-local.

                **Config supplies policy; injection supplies only data**
                (DD-8). ``config.earnings_enabled`` decides whether the gate
                exists *at all*, even when a calendar was injected — otherwise
                a live rolloff (``earnings.enabled: false``) would leave
                replays gated, a live/replay divergence on exactly the FC-048
                fidelity axis. A what-if replay with the gate forced on or off
                is a config override, like every other knob.

                Enabled + injected -> use it (the simulator passes
                ``HistoricalEarningsCalendar``). Enabled + ``None`` ->
                construct ``EarningsCalendarService``. Disabled -> ``None``,
                and every gate site is a no-op, byte-identical to pre-FC-013.
            allow_bigquery: whether this scanner's BigQuery readers may run.
                Production ``/scan`` passes nothing and gets ``True``. FC-068
                repointed the backtest onto this scanner, and both readers
                below query production data against ``CURRENT_TIMESTAMP()`` —
                the cost-basis divergence cross-check would mix real
                assignments into a replay, and the ``uncovered_days`` lookup
                would issue one query per simulated day. A replay passes
                ``False``: the cross-check reports "unavailable" (the
                simulated broker's floor stands) and ``uncovered_days``
                answers ``None`` ("could not tell"), which are the honest
                labels rather than fabricated ones.

                Deliberately an explicit constructor parameter rather than a
                ``clock.is_frozen()`` check inside the chokepoints: a
                clock-keyed data-access policy is action at a distance,
                ``is_frozen`` is thread-local, and explicit injection is the
                seam every other replay gate here uses (FC-065 P1/P2).
        """
        self.alpaca = alpaca_client
        self.market_data = market_data
        self.config = config
        self.allow_bigquery = allow_bigquery
        # FC-065: the covered-call floor is Alpaca's avg_entry_price for the
        # equity position, with BigQuery running inline as a divergence
        # cross-check that can veto the broker's number but never supply one.
        self.cost_basis_resolver = CostBasisResolver(
            alpaca_client, config, allow_bigquery=allow_bigquery
        )
        # FC-065 Phase 4: the `uncovered_days` label. One batched BigQuery
        # query per scan for every held symbol, behind its own patchable
        # chokepoint (see tests/conftest.py).
        self.uncovered_days_resolver = UncoveredDaysResolver(
            allow_bigquery=allow_bigquery,
            dataset_id=config.bigquery_dataset,  # FC-075 Phase 2 (DD-4): strategy-scoped read
        )
        # FC-013: resolved once, here, so every gate site downstream is a
        # single `if self.earnings_calendar is None` — there is no second place
        # where "is the gate on" gets re-derived and can drift.
        if not getattr(config, 'earnings_enabled', False):
            self.earnings_calendar = None
        elif earnings_calendar is not None:
            self.earnings_calendar = earnings_calendar
        else:
            from ..api.earnings_calendar import EarningsCalendarService
            self.earnings_calendar = EarningsCalendarService(config)

    def scan_for_put_opportunities(self, max_results: int = 10) -> List[Dict[str, Any]]:
        """Scan for cash-secured put opportunities across all configured stocks.
        
        Args:
            max_results: Maximum number of opportunities to return
            
        Returns:
            List of put opportunities sorted by attractiveness
        """
        # FC-075 Phase 2 (DD-2): only the wheel sells puts. Gate at this
        # chokepoint so /scan, the main.py CLI scan, and any future caller are
        # covered. This also avoids touching self.config.stock_symbols below —
        # absent on the covered-call profile, it would raise KeyError, get
        # swallowed by the except, and emit a spurious scan_failure every scan.
        if self.config.strategy_id != 'wheel':
            logger.info("Put scan skipped for non-wheel profile",
                        event_category="system",
                        event_type="put_scan_skipped_non_wheel_profile",
                        strategy_id=self.config.strategy_id)
            return []

        try:
            logger.info("Scanning for put opportunities", event_category="system", event_type="put_scan_started", symbol_count=len(self.config.stock_symbols))
            
            opportunities = []
            
            # Get suitable stocks first
            suitable_stocks = self.market_data.filter_suitable_stocks(self.config.stock_symbols)
            
            for stock in suitable_stocks:
                symbol = stock['symbol']

                # FC-013 put leg: N=2 days-until, symbol-level. Checked BEFORE
                # `_has_existing_position` — both are network-or-cache checks,
                # and gating first makes the event count read as the earnings
                # exposure of the post-`filter_suitable_stocks` candidate set:
                # the symbols that could otherwise have produced puts this
                # scan, held or not. Stage-1 rejects are upstream and never
                # reach the gate.
                #
                # Why N=2 and not span (DD-3): ten months of fills put put-side
                # realized-loss risk in *immediate pre-event entries* (the GOOGL
                # 04-28 shape, days_until=1), while wide-window pre-earnings put
                # income was the book's best bucket — 100% win rate, $301
                # average. A wide put window would forfeit the best trades to
                # block a risk that lives in the last two days. The legs
                # diverging is the deliberate choice, not an oversight.
                if self._put_leg_blocked_by_earnings(symbol):
                    continue

                # Skip if we already have positions in this stock
                if self._has_existing_position(symbol):
                    continue

                # Get put opportunities for this stock
                puts = self.market_data.find_suitable_puts(symbol)
                
                for put in puts[:3]:  # Top 3 puts per stock
                    opportunity = self._create_put_opportunity(put, stock)
                    if opportunity:
                        opportunities.append(opportunity)
            
            # Sort by overall attractiveness score
            opportunities.sort(key=lambda x: x.get('attractiveness_score', 0), reverse=True)

            logger.info("Put scan completed",
                       total_opportunities=len(opportunities),
                       returning=min(max_results, len(opportunities)))

            # Enhanced logging for BigQuery analytics
            log_performance_metric(
                logger,
                metric_name="put_opportunities_discovered",
                metric_value=len(opportunities),
                metric_unit="count",
                symbols_scanned=len(self.config.stock_symbols),
                suitable_stocks=len(suitable_stocks),
                returning=min(max_results, len(opportunities)),
                top_score=opportunities[0].get('attractiveness_score', 0) if opportunities else 0,
                avg_score=sum(opp.get('attractiveness_score', 0) for opp in opportunities) / len(opportunities) if opportunities else 0,
                avg_annual_return=sum(opp.get('annual_return_percent', 0) for opp in opportunities) / len(opportunities) if opportunities else 0
            )

            return opportunities[:max_results]
            
        except Exception as e:
            # Enhanced error logging
            log_error_event(
                logger,
                error_type="scan_failure",
                error_message=str(e),
                component="options_scanner",
                recoverable=True,
                scan_type="put"
            )
            return []
    
    def scan_for_call_opportunities(self, max_results: int = 10,
                                    run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Scan for covered call opportunities on assigned stock positions.

        Args:
            max_results: Maximum number of opportunities to return
            run_id: FC-065 Phase 4 — the identifier joining this scan to the
                ``/run`` that executes from its blob. ``/scan`` mints it and
                passes it here; a caller that omits it (the CLI ``--command
                scan``) gets a self-contained one so its decisions are still
                recorded, they just have no execution stage to join to.

        Returns:
            List of call opportunities sorted by attractiveness
        """
        recorder: Optional[DecisionRecorder] = None
        recorded_symbols: List[str] = []
        try:
            logger.info("Scanning for covered call opportunities")

            opportunities = []

            # Get current stock positions
            positions = self.alpaca.get_positions()
            stock_positions = get_stock_positions(positions)

            # FC-065 Phase 4: one decision record per held symbol per cycle.
            # The scan stage writes the row for every symbol IT terminates
            # (not eligible / blocked / no candidates); symbols that produced
            # candidates are terminated by /run instead, so a held symbol with
            # no row at all for a scan-hour means the cycle did not complete —
            # which is the point of the fill-rate check.
            recorder = DecisionRecorder(run_id or mint_run_id(), STAGE_SCAN)
            held_symbols = [p['symbol'] for p in stock_positions]
            uncovered_days = self.uncovered_days_resolver.resolve(
                held_symbols, covered_underlyings(positions)
            )

            for position in stock_positions:
                symbol = position['symbol']
                shares = int(float(position['qty']))
                mark_price = position_mark_price(position)
                labels = {
                    'shares': shares,
                    'current_price': mark_price,
                    'uncovered_days': uncovered_days.get(symbol),
                }

                # FC-075 Phase 2 (DD-3): operator opt-out. An excluded holding
                # reports as excluded (not under-lotted) and spends zero further
                # work — deliberately before the lot-size check. excluded_symbols
                # is normalized (upper/stripped) in Config.
                if symbol.strip().upper() in self.config.excluded_symbols:
                    recorder.record(symbol, OUTCOME_NOT_ELIGIBLE,
                                    REASON_EXCLUDED_BY_CONFIG, **labels)
                    recorded_symbols.append(symbol)
                    continue

                # Only consider positions with at least 100 shares
                if shares < 100:
                    recorder.record(symbol, OUTCOME_NOT_ELIGIBLE,
                                    REASON_INSUFFICIENT_SHARES, **labels)
                    recorded_symbols.append(symbol)
                    continue

                # FC-013 call leg, part 1 of 2: the symbol-level UNKNOWN skip.
                # Sits before cost-basis resolution deliberately — an
                # unanswerable symbol should not spend a resolver call and a
                # BigQuery cross-check to reach the same skip. There is NO
                # symbol-level blackout skip on this leg (rev 2.2): a symbol
                # reporting in three days may legally sell a call expiring in
                # two, and that legality is DD-3's whole point. Blocking here
                # is only for "we could not tell", where no candidate can be
                # cleared because the span test has no date to test against.
                earnings_status, earnings_date = self._call_leg_earnings(symbol)
                if earnings_status == EARNINGS_UNKNOWN:
                    logger.warning("Skipping call scan - earnings calendar could not answer",
                                   event_category="trade",
                                   event_type="call_scan_skipped_earnings_unknown",
                                   symbol=symbol,
                                   shares=shares,
                                   **self._earnings_enrichment(symbol))
                    recorder.record(symbol, OUTCOME_BLOCKED,
                                    REASON_EARNINGS_UNKNOWN, **labels)
                    recorded_symbols.append(symbol)
                    continue

                # CRITICAL: Resolve cost basis per share for protection.
                # This ensures we never sell calls below cost basis (guaranteed loss).
                # FC-065: the floor is Alpaca's avg_entry_price, verified inline
                # against reconstructed assignment history (BigQuery). Both
                # failure modes below skip the symbol for this scan.
                try:
                    raw_cost_basis = float(position.get('cost_basis') or 0)
                except (TypeError, ValueError):
                    raw_cost_basis = 0.0
                resolution = self.cost_basis_resolver.resolve_detailed(
                    symbol, position, shares
                )
                cost_basis_per_share = resolution['basis']
                cost_basis_source = resolution['source']
                cross_check = resolution.get('cross_check') or {}

                # FAIL CLOSED on a divergent cross-check. The broker reported a
                # number and the assignment history says it is wrong — presence
                # with a wrong value, which the zero check below cannot catch.
                if cost_basis_source == SOURCE_DIVERGENT:
                    logger.warning("Skipping call scan - cost basis divergent",
                                   event_category="trade",
                                   event_type="call_scan_skipped_cost_basis_divergent",
                                   symbol=symbol,
                                   shares=shares,
                                   broker_basis=resolution.get('broker_basis'),
                                   expected_basis=cross_check.get('expected_basis'),
                                   basis_delta=cross_check.get('basis_delta'),
                                   tolerance=cross_check.get('tolerance'),
                                   reconstructed_shares=cross_check.get('reconstructed_shares'),
                                   reason=cross_check.get('reason'))
                    recorder.record(symbol, OUTCOME_BLOCKED, REASON_FLOOR_DIVERGENT,
                                    cost_basis_per_share=resolution.get('broker_basis'),
                                    underwater=underwater_pct(
                                        mark_price, resolution.get('broker_basis')),
                                    **labels)
                    recorded_symbols.append(symbol)
                    continue

                # FAIL CLOSED on an unresolved cost basis. This floor is the
                # first of the two below-basis protections: find_suitable_calls
                # warns and continues when min_strike_price <= 0, so "no basis"
                # must mean "no calls", never "any strike". (FC-050 restored the
                # second one, the execute-time floor in call_seller.)
                if cost_basis_per_share <= 0:
                    logger.warning("Skipping call scan - cost basis unresolved",
                                   event_category="trade",
                                   event_type="call_scan_skipped_cost_basis_unresolved",
                                   symbol=symbol,
                                   shares=shares,
                                   cost_basis=raw_cost_basis,
                                   avg_entry_price=resolution.get('broker_basis'))
                    recorder.record(symbol, OUTCOME_BLOCKED, REASON_FLOOR_UNRESOLVED,
                                    cost_basis_per_share=cost_basis_per_share,
                                    **labels)
                    recorded_symbols.append(symbol)
                    continue

                # FC-065: the cross-check's verdict is logged on every scan of
                # every held symbol, including when it could not run. An
                # "unavailable" run is not an "all clear" — it is the state in
                # which the floor rests on a single broker field with nothing
                # watching it, which is precisely what needs to be visible.
                logger.info("Cost basis cross-check completed",
                            event_category="risk",
                            event_type="cost_basis_cross_check",
                            symbol=symbol,
                            source=cost_basis_source,
                            resolved_basis=cost_basis_per_share,
                            status=cross_check.get('status'),
                            expected_basis=cross_check.get('expected_basis'),
                            basis_delta=cross_check.get('basis_delta'),
                            tolerance=cross_check.get('tolerance'),
                            reason=cross_check.get('reason'))

                # Get call opportunities filtered by cost basis — and, FC-013
                # call leg part 2 of 2, by the earnings SPAN predicate. The
                # date threads into the criteria chain exactly the way the
                # floor does; a 'clear' symbol passes None and is
                # unconstrained, byte-identical to pre-FC-013. The per-
                # candidate test reuses this symbol's single date fetch, so
                # candidate count adds zero Finnhub calls.
                calls = self.market_data.find_suitable_calls(
                    symbol,
                    min_strike_price=cost_basis_per_share,
                    exclude_expiry_on_or_after=earnings_date
                )

                logger.debug("Filtering calls for cost basis protection",
                            symbol=symbol,
                            cost_basis_per_share=cost_basis_per_share,
                            next_earnings_date=(
                                earnings_date.isoformat() if earnings_date else None),
                            calls_found=len(calls))

                floor_labels = dict(labels)
                floor_labels['cost_basis_per_share'] = cost_basis_per_share
                floor_labels['underwater'] = underwater_pct(
                    mark_price, cost_basis_per_share)

                # The span predicate emptied the symbol's candidate set: every
                # qualifying strike expired into the event. That is a policy
                # gate terminating the symbol, so it is `blocked`, not
                # `no_candidates` — the reason a reader gets must survive to
                # the row, or this repeats the NVDA silent-exclusion failure.
                # Carries basis/underwater because the chain had to be fetched
                # to know it emptied (DD-4), matching the no_candidates shape
                # at this ladder position. Empty with ZERO span rejections is
                # the ordinary no_candidates path below, untouched.
                span_rejections = self._symbol_rejection_counts(symbol).get(
                    'expires_into_earnings', 0)
                if not calls and span_rejections > 0:
                    logger.info("Skipping call scan - every qualifying strike spans earnings",
                                event_category="trade",
                                event_type="call_scan_skipped_earnings_blackout",
                                symbol=symbol,
                                shares=shares,
                                cost_basis_per_share=cost_basis_per_share,
                                spanning_candidates_rejected=span_rejections,
                                **self._earnings_enrichment(symbol))
                    recorder.record(symbol, OUTCOME_BLOCKED,
                                    REASON_EARNINGS_BLACKOUT,
                                    candidates=0,
                                    reason_counts=self._symbol_rejection_counts(symbol),
                                    **floor_labels)
                    recorded_symbols.append(symbol)
                    continue

                if not calls:
                    # THE "nothing happened" CASE. Before this record, an
                    # underwater symbol simply produced no output anywhere —
                    # NVDA has ended every scan since 07-30 at
                    # `stage_8_complete_not_found` and nothing downstream said
                    # so. It is now a labelled row.
                    recorder.record(symbol, OUTCOME_NO_CANDIDATES,
                                    REASON_NO_QUALIFYING_STRIKES,
                                    candidates=0,
                                    reason_counts=self._symbol_rejection_counts(symbol),
                                    **floor_labels)
                    recorded_symbols.append(symbol)
                    continue

                created = 0
                for call in calls[:3]:  # Top 3 calls per position
                    opportunity = self._create_call_opportunity(
                        call, position, cost_basis_per_share
                    )
                    if opportunity:
                        # Carry the scan's labels into /run on the blob, the
                        # same way run_id rides. /run has no positions
                        # snapshot and no BigQuery lookup of its own, so
                        # without this every /run-terminated row would carry
                        # a NULL uncovered_days.
                        stamp_decision_labels(opportunity, **floor_labels)
                        opportunities.append(opportunity)
                        created += 1

                if created == 0:
                    # The chain had qualifying strikes but none survived
                    # opportunity construction. Attribute honestly rather than
                    # filing everything under a bad quote: the quote is the
                    # expected cause (fail-closed, FC-065 Phase 1) but an
                    # exception inside the builder lands here too, and a closed
                    # vocabulary that misfiles is worse than one more value.
                    recorder.record(symbol, OUTCOME_NO_CANDIDATES,
                                    self._construction_failure_reason(symbol),
                                    candidates=0,
                                    reason_counts=self._symbol_rejection_counts(symbol),
                                    **floor_labels)
                    recorded_symbols.append(symbol)
                    continue

                # Terminated by /run, not here. Logged so the hand-off is
                # visible even when /run never happens.
                logger.info("Decision deferred to execution stage",
                            event_category="system",
                            event_type="decision_record_deferred",
                            run_id=recorder.run_id,
                            symbol=symbol,
                            candidates=created,
                            uncovered_days=labels['uncovered_days'],
                            underwater_pct=floor_labels['underwater'])


            # Sort by overall attractiveness score
            opportunities.sort(key=lambda x: x.get('attractiveness_score', 0), reverse=True)

            logger.info("Call scan completed",
                       total_opportunities=len(opportunities),
                       returning=min(max_results, len(opportunities)))

            # Enhanced logging for BigQuery analytics
            log_performance_metric(
                logger,
                metric_name="call_opportunities_discovered",
                metric_value=len(opportunities),
                metric_unit="count",
                stock_positions=len(stock_positions),
                positions_with_100_shares=len([p for p in stock_positions if int(float(p['qty'])) >= 100]),
                returning=min(max_results, len(opportunities)),
                top_score=opportunities[0].get('attractiveness_score', 0) if opportunities else 0,
                avg_score=sum(opp.get('attractiveness_score', 0) for opp in opportunities) / len(opportunities) if opportunities else 0,
                avg_annual_return=sum(opp.get('annual_premium_return_percent', 0) for opp in opportunities) / len(opportunities) if opportunities else 0
            )

            return opportunities[:max_results]
            
        except Exception as e:
            # Enhanced error logging
            log_error_event(
                logger,
                error_type="scan_failure",
                error_message=str(e),
                component="options_scanner",
                recoverable=True,
                scan_type="call"
            )
            return []
        finally:
            # Flush in `finally`, not on the happy path: a scan that dies
            # halfway still knows what it decided about the symbols it got
            # to, and losing that is losing exactly the cycles worth
            # investigating.
            if recorder is not None:
                recorder.flush(expected_symbols=recorded_symbols)

    # ------------------------------------------------------------------ #
    # FC-013 — the earnings gate
    # ------------------------------------------------------------------ #
    def _earnings_enrichment(self, symbol: str) -> Dict[str, Any]:
        """Proximity fields for the skip events, or empty when unavailable.

        Best-effort by design: enrichment must never be able to turn a gate
        decision into an exception. A calendar seam that lacks the method (a
        test double, a future implementation) simply logs less.
        """
        try:
            proximity = self.earnings_calendar.get_earnings_proximity(symbol)
        except Exception:
            return {}
        return proximity if isinstance(proximity, dict) else {}

    def _call_leg_earnings(self, symbol: str) -> Tuple[str, Optional[date]]:
        """The call leg's calendar answer: ``(status, date_or_None)``.

        With the gate off this reports ``('clear', None)``, which threads a
        ``None`` span floor into ``find_suitable_calls`` — the pre-FC-013
        behaviour, byte-identical.
        """
        if self.earnings_calendar is None:
            return (EARNINGS_CLEAR, None)
        status, earnings_date = self.earnings_calendar.next_earnings_info(symbol)
        return (status, earnings_date if status == EARNINGS_KNOWN else None)

    def _put_leg_blocked_by_earnings(self, symbol: str) -> bool:
        """Whether the put leg skips ``symbol`` this scan. Emits the event.

        Tri-state, and the third state is the point: ``None`` from
        ``earnings_within`` means the calendar could not answer, and an
        unanswerable symbol is skipped (fail closed) under a *different* event
        name than a real blackout. Blocking on an outage and blocking on
        earnings look identical in a P&L; they need completely different
        operator responses, so they get different events — and only the
        unknown one is alerted (DD-6).
        """
        if self.earnings_calendar is None:
            return False

        within = self.earnings_calendar.earnings_within(
            symbol, self.config.earnings_blackout_days)

        if within is None:
            logger.warning("Skipping put scan - earnings calendar could not answer",
                           event_category="trade",
                           event_type="put_scan_skipped_earnings_unknown",
                           symbol=symbol,
                           blackout_days=self.config.earnings_blackout_days,
                           **self._earnings_enrichment(symbol))
            return True

        if within:
            logger.info("Skipping put scan - earnings blackout",
                        event_category="trade",
                        event_type="put_scan_skipped_earnings_blackout",
                        symbol=symbol,
                        blackout_days=self.config.earnings_blackout_days,
                        **self._earnings_enrichment(symbol))
            return True

        return False

    @staticmethod
    def _call_rejection_counts(stats: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
        """Non-zero chain-rejection counts from a stats dict, or ``{}``.

        Defensive about the shape because ``market_data`` is a ``MagicMock``
        in much of the suite, where attribute access returns another mock
        rather than raising.
        """
        counts = stats if isinstance(stats, dict) else {}
        out: Dict[str, int] = {}
        for key, value in counts.items():
            if key == 'total_rejected':
                continue
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count > 0:
                out[str(key)] = count
        return out

    def _construction_failure_reason(self, symbol: str) -> str:
        """Why every candidate failed to become an opportunity.

        ``_create_call_opportunity`` returns ``None`` for an unusable stock
        quote (the expected cause) *or* on an exception. Re-reading the
        already-cached metrics distinguishes the two for a few microseconds,
        instead of filing a builder crash as a quote problem forever.
        """
        try:
            metrics = self.market_data.get_stock_metrics(symbol) or {}
            price = float(metrics.get('current_price', 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return REASON_QUOTE_UNAVAILABLE
        return (REASON_QUOTE_UNAVAILABLE if price <= 0
                else REASON_OPPORTUNITY_BUILD_FAILED)

    def _symbol_rejection_counts(self, symbol: str) -> Dict[str, int]:
        """Look up ``symbol``'s last chain-rejection breakdown from market data."""
        published = getattr(self.market_data, 'last_call_rejection_stats', None)
        stats = published.get(symbol) if isinstance(published, dict) else None
        return self._call_rejection_counts(stats)

    def scan_all_opportunities(self) -> Dict[str, List[Dict[str, Any]]]:
        """Scan for both put and call opportunities.
        
        Returns:
            Dictionary with 'puts' and 'calls' opportunity lists
        """
        try:
            put_opportunities = self.scan_for_put_opportunities(20)
            call_opportunities = self.scan_for_call_opportunities(10)
            
            return {
                'puts': put_opportunities,
                'calls': call_opportunities,
                'scan_timestamp': clock.now().isoformat(),
                'total_opportunities': len(put_opportunities) + len(call_opportunities)
            }
            
        except Exception as e:
            logger.error("Failed to scan all opportunities", event_category="error", event_type="scan_failed", error=str(e))
            return {'puts': [], 'calls': [], 'error': str(e)}
    
    def _create_put_opportunity(self, put_option: Dict[str, Any], stock_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Create a put opportunity record with scoring.
        
        Args:
            put_option: Put option data
            stock_info: Stock information
            
        Returns:
            Put opportunity with scoring
        """
        try:
            symbol = stock_info['symbol']
            current_price = stock_info['current_price']
            
            # Calculate key metrics
            premium = put_option['mid_price']
            strike = put_option['strike_price']
            dte = put_option['dte']
            delta = abs(put_option.get('delta', 0))
            
            # Calculate returns
            if dte > 0 and strike > 0:
                annual_return = (premium / strike) * (365 / dte) * 100
            else:
                annual_return = 0
            
            # Distance from current price
            otm_percentage = ((current_price - strike) / current_price) * 100 if current_price > 0 else 0
            
            # Liquidity score (basic)
            volume = put_option.get('volume', 0)
            open_interest = put_option.get('open_interest', 0)
            liquidity_score = min(100, (volume * 0.3 + open_interest * 0.7) / 10)
            
            # Calculate attractiveness score (0-100)
            attractiveness_score = self._calculate_put_attractiveness_score(
                annual_return, delta, otm_percentage, liquidity_score, dte
            )
            
            opportunity = {
                'type': 'put',
                'symbol': symbol,
                'option_symbol': put_option['symbol'],
                'current_stock_price': current_price,
                'strike_price': strike,
                'premium': premium,
                'dte': dte,
                'delta': delta,
                'annual_return_percent': annual_return,
                'otm_percentage': otm_percentage,
                'liquidity_score': liquidity_score,
                'attractiveness_score': attractiveness_score,
                'expiration_date': put_option['expiration_date'],
                'bid': put_option.get('bid', 0),
                'ask': put_option.get('ask', 0),
                'volume': volume,
                'open_interest': open_interest,
                'implied_volatility': put_option.get('implied_volatility', 0),
                'scan_timestamp': clock.now().isoformat()
            }
            
            return opportunity
            
        except Exception as e:
            # Enhanced error logging
            log_error_event(
                logger,
                error_type="opportunity_creation_failed",
                error_message=str(e),
                component="options_scanner",
                recoverable=True,
                opportunity_type="put",
                symbol=stock_info.get('symbol', 'unknown')
            )
            return None
    
    def _create_call_opportunity(self, call_option: Dict[str, Any], stock_position: Dict[str, Any],
                                 cost_basis_per_share: float) -> Optional[Dict[str, Any]]:
        """Create a call opportunity record with scoring.

        Args:
            call_option: Call option data
            stock_position: Stock position information
            cost_basis_per_share: Per-share cost basis already resolved by the
                caller (FC-050). Passed in rather than recomputed so the floor
                that filtered the chain, the one carried on the opportunity and
                the one enforced at execution are the same number.

        Returns:
            Call opportunity with scoring
        """
        try:
            symbol = stock_position['symbol']
            shares_owned = int(float(stock_position['qty']))

            # Get current stock price. FC-065 (folded in from the removed
            # Phase 3): a failed quote used to fail OPEN here — the opportunity
            # was still emitted, with every price-relative metric (annualized
            # return, OTM distance, attractiveness score) computed against a
            # current price of 0. Fail closed for this symbol instead;
            # the next cycle re-evaluates in minutes.
            stock_metrics = self.market_data.get_stock_metrics(symbol) or {}
            try:
                current_price = float(stock_metrics.get('current_price', 0) or 0)
            except (TypeError, ValueError):
                current_price = 0.0

            if current_price <= 0:
                logger.warning("Skipping call opportunity - stock quote unavailable",
                               event_category="trade",
                               event_type="call_scan_skipped_quote_unavailable",
                               symbol=symbol,
                               option_symbol=call_option.get('symbol'))
                return None

            # Calculate key metrics
            premium = call_option['mid_price']
            strike = call_option['strike_price']
            dte = call_option['dte']
            delta = abs(call_option.get('delta', 0))
            
            # Calculate maximum contracts
            max_contracts = shares_owned // 100
            
            if max_contracts <= 0:
                return None
            
            # Calculate returns
            premium_income = premium * 100 * max_contracts  # Per contract premium
            
            if dte > 0:
                annual_premium_return = (premium / current_price) * (365 / dte) * 100 if current_price > 0 else 0
            else:
                annual_premium_return = 0
            
            # Calculate total return if assigned
            if strike > cost_basis_per_share:
                capital_gain = (strike - cost_basis_per_share) * 100 * max_contracts
                total_return_if_assigned = premium_income + capital_gain
            else:
                capital_loss = (cost_basis_per_share - strike) * 100 * max_contracts
                total_return_if_assigned = premium_income - capital_loss
            
            # Distance above current price
            otm_percentage = ((strike - current_price) / current_price) * 100 if current_price > 0 else 0
            
            # Liquidity score
            volume = call_option.get('volume', 0)
            open_interest = call_option.get('open_interest', 0)
            liquidity_score = min(100, (volume * 0.3 + open_interest * 0.7) / 10)
            
            # FC-065 Phase 2 / FC-071: ``>=``, matching the gates. Every floor gate
            # rejects only ``strike < floor`` (market_data's chain filter,
            # call_seller's execute-time check, risk_manager.validate_roll),
            # so an at-floor strike is admitted and sold — GOOGL's 370C was.
            # With strict ``>`` this read False on exactly those writes: a warning
            # that gated nothing and contradicted the behaviour. At-floor keeps
            # both premiums and gives up no capital, which is the decided policy.
            #
            # FC-071: single source of truth. This one comparison feeds BOTH the
            # ``above_cost_basis`` scoring input (at-floor earns the 15-pt bonus,
            # not the 5-pt below-basis consolation) and the
            # ``assignment_above_cost_basis`` flag on the opportunity dict below.
            # Do not re-derive either from a separate comparison — that drift is
            # exactly what FC-071 closed.
            assignment_above_cost_basis = strike >= cost_basis_per_share

            # Calculate attractiveness score
            attractiveness_score = self._calculate_call_attractiveness_score(
                annual_premium_return, delta, otm_percentage, liquidity_score, dte,
                assignment_above_cost_basis
            )
            
            opportunity = {
                'type': 'call',
                'symbol': symbol,
                'option_symbol': call_option['symbol'],
                'current_stock_price': current_price,
                'strike_price': strike,
                'premium': premium,
                'dte': dte,
                'delta': delta,
                'shares_owned': shares_owned,
                'max_contracts': max_contracts,
                'cost_basis_per_share': cost_basis_per_share,
                'premium_income': premium_income,
                'annual_premium_return_percent': annual_premium_return,
                'total_return_if_assigned': total_return_if_assigned,
                'otm_percentage': otm_percentage,
                'liquidity_score': liquidity_score,
                'attractiveness_score': attractiveness_score,
                # FC-065 Phase 2 / FC-071: the single comparison computed above,
                # shared with the ``above_cost_basis`` scoring input so the flag
                # and the score can never disagree at equality.
                'assignment_above_cost_basis': assignment_above_cost_basis,
                'expiration_date': call_option['expiration_date'],
                'bid': call_option.get('bid', 0),
                'ask': call_option.get('ask', 0),
                'volume': volume,
                'open_interest': open_interest,
                'implied_volatility': call_option.get('implied_volatility', 0),
                'scan_timestamp': clock.now().isoformat()
            }
            
            return opportunity
            
        except Exception as e:
            # Enhanced error logging
            log_error_event(
                logger,
                error_type="opportunity_creation_failed",
                error_message=str(e),
                component="options_scanner",
                recoverable=True,
                opportunity_type="call",
                symbol=stock_position.get('symbol', 'unknown')
            )
            return None
    
    def _calculate_put_attractiveness_score(self, annual_return: float, delta: float, 
                                          otm_percentage: float, liquidity_score: float, 
                                          dte: int) -> float:
        """Calculate attractiveness score for put opportunity (0-100).
        
        Args:
            annual_return: Annualized return percentage
            delta: Option delta (absolute value)
            otm_percentage: Percentage out of the money
            liquidity_score: Liquidity score (0-100)
            dte: Days to expiration
            
        Returns:
            Attractiveness score (0-100)
        """
        try:
            score = 0
            
            # Return component (40 points max)
            return_score = min(40, annual_return * 2)  # 20% annual = 40 points
            score += return_score
            
            # Delta component (20 points max) - prefer 0.15-0.25 delta
            target_delta = 0.20
            # Ensure delta is in valid range [0, 1]
            delta_capped = max(0, min(1, delta))
            delta_penalty = abs(delta_capped - target_delta) * 100  # Penalty for deviation
            delta_score = max(0, min(20, 20 - delta_penalty))  # Cap between 0 and 20
            score += delta_score
            
            # OTM component (15 points max) - prefer 5-15% OTM
            if 5 <= otm_percentage <= 15:
                otm_score = 15
            elif otm_percentage < 5:
                otm_score = max(0, 15 - (5 - otm_percentage) * 2)
            else:
                otm_score = max(0, 15 - (otm_percentage - 15) * 1)
            score += otm_score
            
            # Liquidity component (15 points max)
            score += min(15, liquidity_score * 0.15)
            
            # DTE component (10 points max) - prefer shorter DTE, max at target
            max_dte = self.config.put_target_dte
            if dte <= max_dte:
                # Score higher for shorter DTE (more time decay)
                dte_score = 10 * (max_dte - dte + 1) / (max_dte + 1)
            else:
                dte_score = 0  # No points if exceeds maximum
            score += dte_score
            
            return min(100, max(0, score))
            
        except Exception as e:
            logger.error("Failed to calculate put attractiveness score", event_category="error", event_type="put_score_calculation_failed", error=str(e))
            return 0
    
    def _calculate_call_attractiveness_score(self, annual_return: float, delta: float, 
                                           otm_percentage: float, liquidity_score: float, 
                                           dte: int, above_cost_basis: bool) -> float:
        """Calculate attractiveness score for call opportunity (0-100).
        
        Args:
            annual_return: Annualized premium return percentage
            delta: Option delta (absolute value)
            otm_percentage: Percentage out of the money
            liquidity_score: Liquidity score (0-100)
            dte: Days to expiration
            above_cost_basis: Whether strike is above cost basis
            
        Returns:
            Attractiveness score (0-100)
        """
        try:
            score = 0
            
            # Return component (35 points max)
            return_score = min(35, annual_return * 3)  # ~12% annual = 35 points
            score += return_score
            
            # Delta component (20 points max) - prefer 0.15-0.25 delta
            target_delta = 0.20
            # Ensure delta is in valid range [0, 1]
            delta_capped = max(0, min(1, delta))
            delta_penalty = abs(delta_capped - target_delta) * 100
            delta_score = max(0, min(20, 20 - delta_penalty))  # Cap between 0 and 20
            score += delta_score

            # OTM component (15 points max) - prefer 2-10% OTM
            if 2 <= otm_percentage <= 10:
                otm_score = 15
            elif otm_percentage < 2:
                otm_score = max(0, 15 - (2 - otm_percentage) * 3)
            else:
                otm_score = max(0, 15 - (otm_percentage - 10) * 1)
            score += otm_score
            
            # Above cost basis bonus (15 points max)
            if above_cost_basis:
                score += 15
            else:
                score += 5  # Small bonus even if below cost basis (still collect premium)
            
            # Liquidity component (10 points max)
            score += min(10, liquidity_score * 0.1)
            
            # DTE component (5 points max) - prefer shorter DTE, max at target
            max_dte = self.config.call_target_dte
            if dte <= max_dte:
                # Score higher for shorter DTE (more time decay)
                dte_score = 5 * (max_dte - dte + 1) / (max_dte + 1)
            else:
                dte_score = 0  # No points if exceeds maximum
            score += dte_score
            
            return min(100, max(0, score))
            
        except Exception as e:
            logger.error("Failed to calculate call attractiveness score", event_category="error", event_type="call_score_calculation_failed", error=str(e))
            return 0
    
    def _option_position_matches(self, position_symbol: str, symbol: str) -> str:
        """Whether an option position is written on ``symbol``.

        Returns one of three verdicts, not a bool (FC-079): ``'match'`` (the
        parsed underlying is ``symbol``), ``'no_match'`` (it is a different
        underlying), or ``'unparseable'`` (no contract structure at all). The
        caller treats ``'unparseable'`` exactly like ``'match'`` — it still
        skips — but the two are now *told apart in the log*, because "we hold
        a put on this symbol" and "we hold something we cannot identify" are
        different operator facts that used to file under one reason string.

        FC-069 item 12. This used to be ``symbol in position['symbol']`` — a
        substring test over the whole OCC contract symbol, the 7th member of
        the OCC-substring bug family (FC-041/043/045/048/052, plus the engine's
        dead copy FC-068 deleted). It over-blocked: a held ``PFE260821P…``
        contains ``'F'``, so it suppressed every F put. The canonical parser
        answers the actual question — same primitive and same idiom as
        ``ExecutionEngine._available_shares``.

        Fail-closed on an unparseable symbol (``'unparseable'``, which the
        caller still skips on): an option
        position we cannot identify might be on ``symbol``, and this method's
        whole error posture is "when in doubt, do not write another position."
        Under-blocking here would break the per-underlying invariant; the cost
        of over-blocking is one skipped put. "Unparseable" means the parser
        found no contract structure at all — no underlying, or no date part.
        ``parse_option_symbol`` degrades gracefully rather than raising, and its
        leading-letters fallback still resolves adjusted roots
        (``AAPL1260821C…`` -> ``AAPL``), which is the answer we want; it is the
        no-structure case (``''``, ``'NOT_AN_OCC'``) where trusting the
        fallback would mean guessing.
        """
        try:
            parsed = parse_option_symbol(position_symbol or '')
            underlying = parsed.get('underlying') or ''
            structured = bool(parsed.get('expiration_date'))
        except (TypeError, ValueError, AttributeError):
            underlying, structured = '', False
        if not underlying or not structured:
            return 'unparseable'
        return 'match' if underlying == symbol else 'no_match'

    def _has_existing_position(self, symbol: str) -> bool:
        """Check if we already have positions in a stock. Emits the skip event.

        Args:
            symbol: Stock symbol

        Returns:
            True if we have existing positions
        """
        try:
            positions = self.alpaca.get_positions()

            # Check for direct stock positions
            for position in positions:
                if position['symbol'] == symbol:
                    self._log_existing_position_skip(
                        symbol, position['symbol'], 'stock_position')
                    return True

                # Option positions — parsed, never substring-matched (item 12).
                if position['asset_class'] == 'us_option':
                    verdict = self._option_position_matches(
                        position['symbol'], symbol)
                    if verdict != 'no_match':
                        self._log_existing_position_skip(
                            symbol, position['symbol'],
                            'option_position' if verdict == 'match'
                            else 'unparseable_position')
                        return True

            return False

        except Exception as e:
            # Fail-closed, unchanged. Deliberately NOT also emitting the skip
            # event: an outage and a genuine holding demand different operator
            # responses (the FC-013 lesson), and a tally that files "the API
            # was down" under "we already hold this symbol" is the kind of
            # dishonest metric FC-057/FC-068 exist to remove. This path has its
            # own named event — `position_check_failed`, below — and that is
            # the one to alert on.
            logger.error("Failed to check existing positions", event_category="error", event_type="position_check_failed", symbol=symbol, error=str(e))
            return True  # Conservative assumption

    @staticmethod
    def _log_existing_position_skip(symbol: str, position_symbol: str,
                                    reason: str) -> None:
        """Emit the put-side existing-position skip (FC-069 item 12 ride-along).

        This skip was silent since inception — FC-068 §5 documented the replay
        tally as blind to it "until FC-069 item 12 rewires that check". A gate
        on the money path that logs nothing cannot be counted, replayed, or
        argued with. ``reason`` says which limb fired: ``stock_position`` (the
        exact equity match), ``option_position`` (the parsed-underlying match),
        or ``unparseable_position`` (FC-079 — an option symbol with no contract
        structure; the skip is the same fail-closed skip, but it is a data
        problem rather than a holding, and conflating the two made the
        fail-closed limb invisible in the tally).
        """
        logger.info("Skipping put scan - existing position",
                    event_category="trade",
                    event_type="put_scan_skipped_existing_position",
                    symbol=symbol,
                    reason=reason,
                    position_symbol=position_symbol)

    def get_market_overview(self) -> Dict[str, Any]:
        """Get market overview for wheel strategy context.
        
        Returns:
            Market overview metrics
        """
        try:
            overview = {
                'scan_timestamp': clock.now().isoformat(),
                'configured_stocks': len(self.config.stock_symbols),
                'suitable_stocks': 0,
                'average_iv_rank': 0,
                'market_conditions': 'neutral'
            }
            
            # Analyze configured stocks
            suitable_count = 0
            iv_ranks = []
            
            for symbol in self.config.stock_symbols[:10]:  # Sample first 10 for performance
                try:
                    stock_metrics = self.market_data.get_stock_metrics(symbol)
                    if stock_metrics.get('suitable_for_wheel', False):
                        suitable_count += 1
                    
                    # Get options data for IV analysis
                    options_data = self.market_data.get_option_chain_with_analysis(symbol)
                    if options_data['puts']:
                        avg_iv = np.mean([opt.get('implied_volatility', 0) for opt in options_data['puts'][:5]])
                        if avg_iv > 0:
                            iv_ranks.append(avg_iv)
                            
                except Exception:
                    continue
            
            overview['suitable_stocks'] = suitable_count
            
            if iv_ranks:
                overview['average_iv_rank'] = np.mean(iv_ranks) * 100  # Convert to percentage
                
                # Simple market condition assessment
                avg_iv = np.mean(iv_ranks)
                if avg_iv > 0.30:
                    overview['market_conditions'] = 'high_volatility'
                elif avg_iv < 0.15:
                    overview['market_conditions'] = 'low_volatility'
                else:
                    overview['market_conditions'] = 'normal'
            
            return overview
            
        except Exception as e:
            logger.error("Failed to get market overview", event_category="error", event_type="market_overview_failed", error=str(e))
            return {'error': str(e)}