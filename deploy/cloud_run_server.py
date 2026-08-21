#!/usr/bin/env python3
"""
Cloud Run compatible web server for Options Wheel Strategy.
Provides HTTP endpoints to trigger and monitor the trading strategy.
"""

import os
import threading
import time
import json
import traceback
import hmac
import uuid
from functools import wraps
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
import structlog
import structlog.contextvars

# Add src to path
import sys
sys.path.append('/app/src')

from src.utils.config import Config
from src.utils.logging_events import log_system_event, log_performance_metric, log_error_event, log_trade_event
from src.utils.option_symbols import strict_option_type
from src.data.decision_record import (
    RunDecisionFlusher,
    is_call_opportunity,
    mint_run_id,
    run_id_from_opportunities,
)

logger = structlog.get_logger(__name__)


def _underlyings_removed(before, after):
    """Underlyings of call opportunities present in ``before`` but not ``after``."""
    remaining = {o.get('option_symbol') for o in after if isinstance(o, dict)}
    return {
        o.get('symbol') for o in before
        if isinstance(o, dict) and is_call_opportunity(o)
        and o.get('option_symbol') not in remaining and o.get('symbol')
    }


def _call_only_opportunities(opportunities, strategy_id, log):
    """Drop non-call opportunities (FC-075 Phase 2 DD-2, defense in depth).

    A covered-call (non-wheel) service must NEVER execute a put. The put scan is
    gated and the blob is strategy-keyed, so this only fires on a hand-written or
    corrupted blob (expected live count: 0). Returns the call-only subset and
    logs each refusal. The caller applies the result to BOTH the working list and
    the blob snapshot, so a refused put is not then mislabeled `previously_failed`
    by the ``_underlyings_removed`` diff. Extracted so both directions are unit-
    testable (the covered-call service's ``/run`` is otherwise write-isolated off
    until Seam 4).
    """
    kept = []
    for opp in opportunities:
        if strict_option_type(opp.get('option_symbol') or '') == 'call':
            kept.append(opp)
        else:
            log_error_event(
                log,
                error_type="non_call_opportunity_refused",
                error_message=f"Non-call opportunity refused on {strategy_id} profile",
                component="cloud_run_server",
                recoverable=True,
                option_symbol=opp.get('option_symbol'),
                symbol=opp.get('symbol'),
            )
    return kept


def _maybe_alert_long_exposure(config, alpaca_client, log):
    """Alert (never block) when long exposure exceeds equity (FC-075 Phase 2 DD-5).

    4x-margin discipline for a manual-entry book. Only runs when
    ``sizing_basis == 'equity'`` (the wheel defaults to 'buying_power', so this is
    inert for it and never fetches the account). Fully exception-isolated — a
    get_account() blip must not turn an alert into a scan-killer. Extracted so the
    alert and its get_account-raises path are unit-testable.
    """
    if config.sizing_basis != 'equity':
        return
    try:
        acct = alpaca_client.get_account()
        lmv = float(acct.get('long_market_value') or 0.0)
        equity = float(acct.get('equity') or 0.0)
        threshold = config.max_long_market_value_pct_of_equity
        # equity <= 0 with any long exposure is the worst case on a 4x-margin
        # account — alert, don't let the guard fall silent at the extreme.
        over = (lmv > equity * threshold) if equity > 0 else (lmv > 0)
        if over:
            log_error_event(
                log,
                error_type="long_exposure_exceeds_equity",
                error_message=(f"long_market_value {lmv:.2f} exceeds "
                               f"equity {equity:.2f} x {threshold}"),
                component="cloud_run_server",
                recoverable=True,
                long_market_value=lmv,
                equity=equity,
                threshold=threshold,
            )
    except Exception as e:
        log_error_event(
            log,
            error_type="long_exposure_check_failed",
            error_message=str(e),
            component="cloud_run_server",
            recoverable=True,
        )

app = Flask(__name__)


@app.after_request
def _clear_request_context(response):
    structlog.contextvars.clear_contextvars()
    return response


@app.before_request
def _bind_request_id():
    """Generate a unique request ID and bind it to the structlog context.

    The ID is propagated automatically to every log entry emitted during the
    request thanks to the ``merge_contextvars`` processor in the structlog
    pipeline.  If the caller supplies an ``X-Request-ID`` header we reuse it
    so that correlation works across services.
    """
    request_id = request.headers.get('X-Request-ID', str(uuid.uuid4()))
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)


# Global state
strategy_status = {
    'status': 'idle',
    'last_run': None,
    'last_scan': None,
    'positions': 0,
    'pnl': 0.0,
    'errors': []
}

strategy_lock = threading.Lock()

# Track symbols that have been closed today to prevent repeated close attempts.
# Cleared on Cloud Run cold start (naturally daily for scale-to-zero services).
_closed_today: set = set()


def require_api_key(f):
    """Decorator that enforces API key authentication as defense-in-depth.

    Checks for a valid key in either the X-API-Key header or the
    Authorization: Bearer <token> header.  The expected key is read from
    the STRATEGY_API_KEY environment variable.

    If STRATEGY_API_KEY is not set, authentication is skipped so that
    existing deployments without the key configured are not broken.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        expected_key = os.environ.get('STRATEGY_API_KEY')

        # If no key is configured, skip auth (don't break existing deployments)
        if not expected_key:
            return f(*args, **kwargs)

        # Try X-API-Key header first, then Authorization: Bearer <token>
        provided_key = request.headers.get('X-API-Key')
        if not provided_key:
            auth_header = request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                provided_key = auth_header[7:]

        if not provided_key:
            logger.warning("API request rejected: missing authentication",
                          event_category="security",
                          event_type="auth_missing",
                          endpoint=request.path,
                          remote_addr=request.remote_addr)
            return jsonify({
                'error': 'Authentication required',
                'message': 'Provide API key via X-API-Key header or Authorization: Bearer <token>'
            }), 401

        if not hmac.compare_digest(provided_key, expected_key):
            logger.warning("API request rejected: invalid API key",
                          event_category="security",
                          event_type="auth_invalid",
                          endpoint=request.path,
                          remote_addr=request.remote_addr)
            return jsonify({
                'error': 'Invalid API key',
                'message': 'The provided API key is not valid'
            }), 401

        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Strategy config + account-number interlock (FC-075 Seam 2)
# ---------------------------------------------------------------------------
# One process runs one strategy profile, selected by STRATEGY_CONFIG. These
# module-level caches are process-scoped; tests reset them via
# reset_strategy_state().
_CONFIG_CACHE = None
_INTERLOCK_VERDICT = None  # None = unchecked, True = account matches, False = mismatch


def strategy_config():
    """Return this process's Config, selected by STRATEGY_CONFIG (cached).

    Replaces the former bare ``Config()`` calls so a service can run a non-wheel
    profile (STRATEGY_CONFIG=config/covered_call.yaml) and so the config is
    parsed once per process instead of once per request. NOT named ``get_config``
    — that is the ``/config`` HTTP handler.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        _CONFIG_CACHE = Config(os.environ.get("STRATEGY_CONFIG", "config/settings.yaml"))
        # FC-075 Seam 4: hand this process's profile to the AnalyticsWriter
        # singleton, which has no config in scope of its own. Every route
        # resolves config through the decorators before a handler writes, so
        # the singleton is configured before its first use on any request path.
        from src.data.analytics_writer import configure_analytics_writer
        configure_analytics_writer(
            dataset_id=_CONFIG_CACHE.bigquery_dataset,
            strategy_id=_CONFIG_CACHE.strategy_id,
        )
    return _CONFIG_CACHE


def _account_interlock_ok(config):
    """Verify the live Alpaca account matches ``config.expected_account_number``.

    The single control that makes "right code, wrong credentials" impossible
    across separate-account strategies. Checked lazily on first use (not at
    import — an Alpaca outage must not fail a deploy) and the verdict is cached
    for the process lifetime.

    A genuine result (we obtained an account number, match or mismatch) latches:
    a real mismatch permanently disables trading until redeploy. A *check
    failure* (could not reach Alpaca) does NOT latch — it returns False for this
    request only, so a transient outage on the first request self-heals.
    """
    global _INTERLOCK_VERDICT
    if _INTERLOCK_VERDICT is not None:
        return _INTERLOCK_VERDICT
    try:
        from src.api.alpaca_client import AlpacaClient
        actual = AlpacaClient(config).get_account().get('account_number')
    except Exception as e:
        log_error_event(
            logger,
            error_type="account_interlock_check_failed",
            error_message=f"Could not verify account number: {e}",
            component="cloud_run_server",
            recoverable=True,  # not cached; retried next request
        )
        return False
    # A missing account number means we did NOT obtain a genuine result (e.g. an
    # Alpaca SDK shape change) — treat it as a check failure, not a mismatch, so
    # it does not latch a permanent "mismatch" halt on a transient/shape problem.
    if actual is None:
        log_error_event(
            logger,
            error_type="account_interlock_check_failed",
            error_message="get_account() returned no account_number",
            component="cloud_run_server",
            recoverable=True,  # not cached; retried next request
        )
        return False
    expected = config.expected_account_number
    verdict = (actual == expected)
    _INTERLOCK_VERDICT = verdict
    if not verdict:
        log_error_event(
            logger,
            error_type="account_interlock_mismatch",
            error_message=(
                f"Live Alpaca account {actual!r} != expected {expected!r} for "
                f"strategy_id={config.strategy_id!r}; trading disabled"
            ),
            component="cloud_run_server",
            recoverable=False,
            actual_account=actual,
            expected_account=expected,
            strategy_id=config.strategy_id,
        )
    return verdict


def require_account_match(f):
    """Refuse a trading endpoint if the live account != expected_account_number."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _account_interlock_ok(strategy_config()):
            # Distinguish a latched account mismatch from a transient
            # check-failure (Alpaca unreachable / no account_number) so the HTTP
            # body matches the log events and /health, not just alerting.
            if interlock_status() == 'mismatch':
                error = 'account_interlock_mismatch'
                message = ("Service credentials do not match the expected brokerage "
                           "account; trading is disabled (latched). See "
                           "account_interlock_mismatch log event.")
            else:
                error = 'account_interlock_unverified'
                message = ("Could not verify the brokerage account this request; "
                           "trading refused until a check succeeds. See "
                           "account_interlock_check_failed log event.")
            return jsonify({'status': 'error', 'error': error, 'message': message}), 503
        return f(*args, **kwargs)
    return wrapper


def interlock_status():
    """Report the cached interlock verdict WITHOUT triggering a network check.

    'unchecked' — no trading endpoint has run the check yet (or only transient
    check-failures, which do not latch); 'ok' — account matched; 'mismatch' — a
    genuine account mismatch is latched and trading is disabled. Safe to call
    from health endpoints (no Alpaca call).
    """
    if _INTERLOCK_VERDICT is None:
        return "unchecked"
    return "ok" if _INTERLOCK_VERDICT else "mismatch"


def reset_strategy_state():
    """Reset process-scoped caches. For tests only."""
    global _CONFIG_CACHE, _INTERLOCK_VERDICT
    _CONFIG_CACHE = None
    _INTERLOCK_VERDICT = None
    # The analytics singleton is configured from _CONFIG_CACHE, so it is part
    # of the same process-scoped state: a profile flip that left the old
    # singleton in place would make the next configure call raise.
    from src.data.analytics_writer import _reset_for_tests
    _reset_for_tests()


@app.route('/')
def health_check():
    """Health check endpoint for Cloud Run (liveness).

    Always returns 200 so a latched account-interlock mismatch does not cause
    Cloud Run to loop-restart the instance; the interlock state is surfaced as a
    field for visibility. Detailed status/alerting lives on /health.
    """
    return jsonify({
        'status': 'healthy',
        'service': 'options-wheel-strategy',
        'account_interlock': interlock_status(),
        'timestamp': datetime.now().isoformat()
    })

@app.route('/status')
def get_status():
    """Get current strategy status."""
    return jsonify(strategy_status)

@app.route('/scan', methods=['POST'])
@require_api_key
@require_account_match
def trigger_scan():
    """Trigger a market scan for opportunities."""
    with strategy_lock:
        start_time = datetime.now()
        try:
            # Removed duplicate - already logged below with log_system_event()

            # Log system event for scan trigger
            log_system_event(
                logger,
                event_type="market_scan_triggered",
                status="starting"
            )

            # Initialize trading components
            config = strategy_config()

            from src.api.alpaca_client import AlpacaClient
            from src.api.market_data import MarketDataManager
            from src.data.options_scanner import OptionsScanner

            alpaca_client = AlpacaClient(config)
            market_data = MarketDataManager(alpaca_client, config)
            scanner = OptionsScanner(alpaca_client, market_data, config)

            # FC-065 Phase 4: mint the cycle identifier HERE, once. /scan and
            # /run are separate stateless requests ~15 minutes apart and
            # `request_id` is bound per HTTP request, so nothing joined them
            # before this — the opportunity blob was the only correlator
            # (FC-044's survey). The id rides the blob into /run.
            run_id = mint_run_id(start_time)
            structlog.contextvars.bind_contextvars(run_id=run_id)

            # Perform market scan
            logger.info("Starting options market scan",
                       event_category="system",
                       event_type="market_scan_starting",
                       run_id=run_id)

            # Scan for put opportunities
            put_opportunities = scanner.scan_for_put_opportunities()
            logger.info("Put opportunities found",
                       event_category="system",
                       event_type="put_scan_completed",
                       count=len(put_opportunities))

            # Scan for call opportunities (if we have stock positions)
            call_opportunities = scanner.scan_for_call_opportunities(run_id=run_id)
            logger.info("Call opportunities found",
                       event_category="system",
                       event_type="call_scan_completed",
                       count=len(call_opportunities))

            # FC-075 Phase 2 (DD-5): 4x-margin exposure alert (alert-only, isolated).
            _maybe_alert_long_exposure(config, alpaca_client, logger)

            # Store opportunities for execution
            from src.data.opportunity_store import OpportunityStore
            opportunity_store = OpportunityStore(config)

            all_opportunities = put_opportunities + call_opportunities
            stored = opportunity_store.store_opportunities(
                all_opportunities, start_time, run_id=run_id)

            # Update status with scan results
            strategy_status['last_scan'] = datetime.now().isoformat()
            strategy_status['status'] = 'scan_completed'

            scan_results = {
                'run_id': run_id,
                'put_opportunities': len(put_opportunities),
                'call_opportunities': len(call_opportunities),
                'total_opportunities': len(all_opportunities),
                'stored_for_execution': stored
            }

            # Calculate scan duration
            duration_seconds = (datetime.now() - start_time).total_seconds()

            # Log completion event
            log_system_event(
                logger,
                event_type="market_scan_completed",
                status="completed",
                duration_seconds=duration_seconds,
                put_opportunities=len(put_opportunities),
                call_opportunities=len(call_opportunities),
                total_opportunities=scan_results['total_opportunities'],
                stored_for_execution=stored
            )

            # Log performance metric
            log_performance_metric(
                logger,
                metric_name="market_scan_duration",
                metric_value=duration_seconds,
                metric_unit="seconds",
                opportunities_found=scan_results['total_opportunities']
            )

            # Write analytics execution record
            try:
                from src.data.analytics_writer import get_analytics_writer
                analytics = get_analytics_writer()
                analytics.write_execution(
                    endpoint="/scan",
                    status="completed",
                    duration_seconds=duration_seconds,
                    scan_count=1,
                    opportunities_found=scan_results['total_opportunities'],
                )
            except Exception:
                logger.debug("Analytics write failed for /scan", exc_info=True)

            return jsonify({
                'message': 'Market scan completed successfully',
                'timestamp': datetime.now().isoformat(),
                'results': scan_results
            })

        except Exception as e:
            error_msg = f"Scan failed: {str(e)}"

            # Enhanced error logging
            log_error_event(
                logger,
                error_type="market_scan_exception",
                error_message=str(e),
                component="cloud_run_server",
                recoverable=True
            )

            # Write analytics error + failed execution
            try:
                from src.data.analytics_writer import get_analytics_writer
                analytics = get_analytics_writer()
                analytics.write_error(
                    event_type="market_scan_exception",
                    error_type=type(e).__name__,
                    error_message=str(e),
                    component="cloud_run_server",
                    recoverable=True,
                    stack_trace=traceback.format_exc(),
                )
                analytics.write_execution(
                    endpoint="/scan",
                    status="error",
                    duration_seconds=(datetime.now() - start_time).total_seconds(),
                    errors=1,
                )
            except Exception:
                logger.debug("Analytics write failed for /scan error", exc_info=True)

            strategy_status['errors'].append({
                'timestamp': datetime.now().isoformat(),
                'error': error_msg
            })
            return jsonify({'error': error_msg}), 500

@app.route('/run', methods=['POST'])
@require_api_key
@require_account_match
def trigger_strategy():
    """Trigger strategy execution."""
    with strategy_lock:
        start_time = datetime.now()
        # FC-065 Phase 4 decision-record state, initialised BEFORE the try so
        # the `finally` below can always read it. A crash inside execute_batch
        # used to 500 with zero run-stage rows — including for orders that had
        # already reached the broker, which is the cycle you most want a
        # record of. Mirrors the scanner's flush-in-finally.
        decisions = RunDecisionFlusher()
        blob_opportunities = decisions.opportunities
        selected_opportunities = []
        execution_results = []
        already_positioned_symbols = set()
        previously_failed_symbols = set()

        try:
            # Removed duplicate - already logged below with log_system_event()

            # Log system event for strategy trigger
            log_system_event(
                logger,
                event_type="strategy_execution_triggered",
                status="starting"
            )

            # Initialize trading components
            config = strategy_config()

            from src.api.alpaca_client import AlpacaClient
            from src.api.market_data import MarketDataManager
            from src.data.opportunity_store import OpportunityStore
            from src.strategy.put_seller import PutSeller
            from src.strategy.wheel_engine import WheelEngine
            from src.strategy.execution_engine import ExecutionEngine

            alpaca_client = AlpacaClient(config)
            opportunity_store = OpportunityStore(config)

            # --- Pre-trade housekeeping (wheel only) ---
            # Reconcile positions so wheel state matches Alpaca reality before
            # new trades. (The order-status poll that used to run here was
            # deleted in FC-035: it ran every cycle for ~4 months but never
            # produced a single event or row, and the authoritative
            # fill/expiration record comes from the activities ingestor.)
            #
            # FC-075 Phase 2 (DD-6): gated to the wheel profile. reconcile_positions()
            # feeds a per-request, ephemeral WheelStateManager that nothing on the
            # /run trading path reads (opportunities come from the blob; sizing and
            # idempotency from the positions snapshot). On a covered-call book it is
            # pure liability: it traverses the live FC-054/079 OCC-substring sites in
            # wheel_engine (miscounting P-ticker calls) and emits wheel-shaped trade
            # events from the covered-call account into shared telemetry.
            if config.strategy_id == 'wheel':
                try:
                    engine = WheelEngine(config)

                    reconcile_stats = engine.reconcile_positions()
                    log_system_event(
                        logger,
                        event_type="pre_trade_reconciliation_completed",
                        status="completed",
                        discrepancies_found=reconcile_stats.get('discrepancies_found', 0),
                        state_updates=reconcile_stats.get('state_updates', 0),
                    )
                except Exception as e:
                    # Housekeeping failures should not block trade execution
                    log_error_event(
                        logger,
                        error_type="pre_trade_housekeeping_failed",
                        error_message=str(e),
                        component="cloud_run_server",
                        recoverable=True
                    )
            else:
                log_system_event(
                    logger,
                    event_type="reconcile_skipped_non_wheel_profile",
                    status="skipped",
                    strategy_id=config.strategy_id,
                )

            # Retrieve pending opportunities from Cloud Storage
            opportunities = opportunity_store.get_pending_opportunities(start_time)

            if not opportunities:
                logger.info("No pending opportunities found for execution",
                           event_category="system",
                           event_type="no_opportunities_for_execution",
                           execution_time=start_time.isoformat())

                log_system_event(
                    logger,
                    event_type="strategy_execution_completed",
                    status="no_opportunities",
                    duration_seconds=(datetime.now() - start_time).total_seconds(),
                    opportunities_found=0
                )

                return jsonify({
                    'message': 'No opportunities to execute',
                    'timestamp': datetime.now().isoformat(),
                    'results': {
                        'opportunities_found': 0,
                        'trades_executed': 0
                    }
                })

            # FC-065 Phase 4: the scan's run_id rides in on the opportunities.
            # A blob written before this shipped carries none — mint an orphan
            # id and say so, rather than dropping the cycle's telemetry.
            blob_opportunities[:] = list(opportunities)

            # FC-075 Phase 2 (DD-2): defense in depth — a covered-call (non-wheel)
            # service must NEVER execute a put. The put scan is gated and the blob
            # is strategy-keyed, so this can only fire on a hand-written/corrupted
            # blob (expected live count: 0). Drop non-calls from BOTH the working
            # list and the blob snapshot, so a refused put is not then mislabeled
            # `previously_failed` by the _underlyings_removed diff below.
            if config.strategy_id != 'wheel':
                opportunities = _call_only_opportunities(
                    opportunities, config.strategy_id, logger)
                blob_opportunities[:] = opportunities

            run_id = run_id_from_opportunities(blob_opportunities)
            if not run_id:
                run_id = mint_run_id(start_time)
                logger.warning("Opportunity blob carries no run_id — orphan cycle",
                               event_category="system",
                               event_type="run_id_missing_on_blob",
                               run_id=run_id)
            decisions.run_id = run_id
            structlog.contextvars.bind_contextvars(run_id=run_id)

            logger.info("Retrieved opportunities for execution",
                       event_category="system",
                       event_type="opportunities_retrieved",
                       count=len(opportunities),
                       run_id=run_id,
                       execution_time=start_time.isoformat())

            # Initialize execution engine, put seller, and call seller
            exec_engine = ExecutionEngine(alpaca_client, config, logger)
            market_data = MarketDataManager(alpaca_client, config)
            put_seller = PutSeller(alpaca_client, market_data, config)
            from src.strategy.call_seller import CallSeller
            call_seller = CallSeller(alpaca_client, market_data, config)

            # NON-RETRYABLE FILTER: Skip opportunities that previously failed with
            # non-retryable errors (e.g., "account not eligible", "invalid symbol")
            opportunities, non_retryable_filtered = exec_engine.filter_failed_opportunities(
                opportunities
            )
            previously_failed_symbols = _underlyings_removed(
                blob_opportunities, opportunities)  # noqa: F841 - read in finally
            if non_retryable_filtered > 0 and not opportunities:
                logger.info("All opportunities previously failed with non-retryable errors",
                           event_category="system",
                           event_type="all_opportunities_non_retryable")
                decisions.flush(
                    previously_failed=previously_failed_symbols)
                return jsonify({
                    'message': 'All opportunities previously failed (non-retryable)',
                    'timestamp': datetime.now().isoformat(),
                    'results': {
                        'opportunities_found': non_retryable_filtered,
                        'non_retryable_filtered': non_retryable_filtered,
                        'trades_executed': 0
                    }
                })

            # IDEMPOTENCY CHECK: Filter out opportunities where positions already exist
            # This prevents duplicate trades if execution runs twice (e.g., mark_executed failed)
            already_positioned_symbols = set()
            try:
                existing_positions = alpaca_client.get_option_positions()
                original_count = len(opportunities)
                pre_idempotency = list(opportunities)
                opportunities, filtered_count = exec_engine.filter_duplicate_opportunities(
                    opportunities, existing_positions
                )
                already_positioned_symbols = _underlyings_removed(
                    pre_idempotency, opportunities)

                if filtered_count > 0 and not opportunities:
                    logger.info("All opportunities already have positions - likely duplicate execution",
                               event_category="system",
                               event_type="duplicate_execution_prevented")
                    decisions.flush(
                        already_positioned=already_positioned_symbols,
                        previously_failed=previously_failed_symbols)
                    return jsonify({
                        'message': 'All opportunities already executed (idempotency check)',
                        'timestamp': datetime.now().isoformat(),
                        'results': {
                            'opportunities_found': original_count,
                            'already_executed': filtered_count,
                            'trades_executed': 0
                        }
                    })
            except Exception as e:
                logger.warning("Idempotency check failed, proceeding with caution",
                              event_category="warning",
                              event_type="idempotency_check_failed",
                              error=str(e))

            # Get initial buying power and track locally during execution
            # (Alpaca's API returns stale/incorrect data immediately after trades)
            account_info = alpaca_client.get_account()
            available_buying_power = float(account_info.get('options_buying_power') or account_info['buying_power'])

            # Portfolio equity history is now sourced from Alpaca's
            # /v2/account/portfolio/history endpoint via the daily
            # /ingest-portfolio-history scheduler (FC-012 Phase 2.5).
            # Per-symbol snapshots were dropped entirely — frontend does
            # not consume them and realized P&L is covered by the Alpaca
            # activities view.
            logger.info("Starting execution with buying power",
                       event_category="system",
                       event_type="execution_buying_power_check",
                       initial_buying_power=available_buying_power,
                       buying_power=account_info.get('buying_power'),
                       options_buying_power=account_info.get('options_buying_power'),
                       cash=account_info.get('cash'),
                       portfolio_value=account_info.get('portfolio_value'),
                       equity=account_info.get('equity'))

            # FC-038: ONE positions snapshot for the whole cycle. Sizing and
            # selection must agree on share availability -- re-fetching between
            # the two lets a fill land in between and produce a selection the
            # sizing stage never sanctioned. Fails closed (PositionsUnavailable
            # is an empty list), so a broker outage sells nothing rather than
            # something naked.
            positions_snapshot = exec_engine._positions_snapshot()

            # Rank opportunities: puts sized against buying power, calls
            # against available shares.
            ranked = exec_engine.rank_opportunities(
                opportunities, put_seller, available_buying_power,
                positions=positions_snapshot
            )

            # Select from two budgets: shares for calls, buying power for puts
            selected_opportunities, remaining_bp = exec_engine.select_batch(
                ranked, available_buying_power, positions=positions_snapshot
            )

            # Execute orders sequentially with real-time buying power validation
            execution_results, trades_executed = exec_engine.execute_batch(
                selected_opportunities, put_seller, call_seller=call_seller
            )

            # FC-065 Phase 4: the terminal per-symbol decision for every
            # covered-call candidate this cycle carried. Emitted before the
            # mark-executed step, which has its own 500 exit.
            decisions.flush(
                selected=selected_opportunities,
                execution_results=execution_results,
                drop_reasons=exec_engine.last_call_drop_reasons,
                already_positioned=already_positioned_symbols,
                previously_failed=previously_failed_symbols,
            )

            # Mark opportunities as executed in Cloud Storage
            # CRITICAL: If this fails, opportunities remain pending and could re-execute
            mark_success = opportunity_store.mark_executed(
                execution_time=start_time,
                executed_count=trades_executed,
                results=execution_results
            )

            if not mark_success:
                log_error_event(
                    logger,
                    error_type="mark_executed_failed",
                    error_message="Failed to mark opportunities as executed - risk of re-execution",
                    component="execution_engine",
                    recoverable=False,
                    trades_executed=trades_executed,
                    execution_results_count=len(execution_results)
                )

                try:
                    from src.data.analytics_writer import get_analytics_writer
                    analytics = get_analytics_writer()
                    analytics.write_error(
                        event_type="mark_executed_failed",
                        error_type="mark_executed_failed",
                        error_message="Failed to mark opportunities as executed - risk of re-execution",
                        component="cloud_run_server",
                        recoverable=False,
                    )
                    analytics.write_execution(
                        endpoint="/run",
                        status="error",
                        duration_seconds=(datetime.now() - start_time).total_seconds(),
                        trades_executed=trades_executed,
                        trades_failed=len(opportunities) - trades_executed,
                        buying_power_before=available_buying_power,
                        errors=1,
                    )
                except Exception:
                    logger.debug("Analytics write failed for mark_executed_failed", exc_info=True)

                return jsonify({
                    'error': 'mark_executed_failed',
                    'message': 'Trades executed but failed to mark complete - manual intervention required',
                    'trades_executed': trades_executed,
                    'timestamp': datetime.now().isoformat()
                }), 500

            # Update global status
            strategy_status['last_run'] = datetime.now().isoformat()
            strategy_status['status'] = 'execution_completed'

            # Calculate execution duration
            duration_seconds = (datetime.now() - start_time).total_seconds()

            # Log system event for completion
            log_system_event(
                logger,
                event_type="strategy_execution_completed",
                status="completed",
                duration_seconds=duration_seconds,
                opportunities_evaluated=len(opportunities),
                trades_executed=trades_executed,
                trades_failed=len(opportunities) - trades_executed
            )

            # Log performance metric
            log_performance_metric(
                logger,
                metric_name="strategy_execution_duration",
                metric_value=duration_seconds,
                metric_unit="seconds",
                opportunities_evaluated=len(opportunities),
                trades_executed=trades_executed
            )

            # Write analytics execution record
            try:
                from src.data.analytics_writer import get_analytics_writer
                analytics = get_analytics_writer()
                # Fetch post-trade buying power for comparison
                try:
                    post_account = alpaca_client.get_account()
                    buying_power_after = float(post_account.get('options_buying_power') or post_account.get('buying_power', 0))
                    portfolio_value = float(post_account.get('portfolio_value', 0))
                except Exception:
                    buying_power_after = 0
                    portfolio_value = 0

                analytics.write_execution(
                    endpoint="/run",
                    status="completed",
                    duration_seconds=duration_seconds,
                    trades_executed=trades_executed,
                    trades_failed=len(opportunities) - trades_executed,
                    buying_power_before=available_buying_power,
                    buying_power_after=buying_power_after,
                    portfolio_value=portfolio_value,
                    opportunities_found=len(opportunities),
                )
            except Exception:
                logger.debug("Analytics write failed for /run", exc_info=True)

            return jsonify({
                'message': 'Strategy execution completed successfully',
                'timestamp': datetime.now().isoformat(),
                'results': {
                    'opportunities_evaluated': len(opportunities),
                    'trades_executed': trades_executed,
                    'trades_failed': len(opportunities) - trades_executed,
                    'execution_results': execution_results
                }
            })

        except Exception as e:
            logger.error("Strategy execution failed",
                        event_category="error",
                        event_type="strategy_execution_exception",
                        error=str(e),
                        traceback=traceback.format_exc())

            # Write analytics error + failed execution
            try:
                from src.data.analytics_writer import get_analytics_writer
                analytics = get_analytics_writer()
                analytics.write_error(
                    event_type="strategy_execution_exception",
                    error_type=type(e).__name__,
                    error_message=str(e),
                    component="cloud_run_server",
                    recoverable=True,
                    stack_trace=traceback.format_exc(),
                )
                analytics.write_execution(
                    endpoint="/run",
                    status="error",
                    duration_seconds=(datetime.now() - start_time).total_seconds(),
                    errors=1,
                )
            except Exception:
                logger.debug("Analytics write failed for /run error", exc_info=True)

            strategy_status['errors'].append({
                'timestamp': datetime.now().isoformat(),
                'error': f"Strategy execution failed: {str(e)}"
            })
            return jsonify({'error': f"Strategy execution failed: {str(e)}"}), 500

        finally:
            # A cycle that crashed is the cycle most worth a record — not
            # least because orders may already have reached the broker before
            # the exception. Whatever partial state exists is written; the
            # once-guard makes the normal paths' explicit flush win.
            decisions.flush(
                selected=selected_opportunities,
                execution_results=execution_results,
                already_positioned=already_positioned_symbols,
                previously_failed=previously_failed_symbols,
            )

def _is_market_open() -> bool:
    """Check if the US stock market is currently open.

    Uses a simple time check: market is open 9:30 AM - 4:00 PM ET, Mon-Fri.
    This covers regular sessions and is conservative on half-days (the
    scheduler should not trigger outside these bounds anyway).

    Returns:
        True if market is currently open.
    """
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    # Weekday: Monday=0 ... Friday=4
    if now_et.weekday() > 4:
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close


@app.route('/monitor', methods=['POST'])
@require_api_key
@require_account_match
def monitor_positions():
    """Monitor existing positions and close profitable ones."""
    with strategy_lock:
        start_time = datetime.now()

        # Gate: skip monitoring entirely when market is closed to avoid
        # "options market orders are only allowed during market hours" errors.
        if not _is_market_open():
            logger.info("Market closed - skipping monitor",
                       event_category="system",
                       event_type="monitor_skipped_market_closed")
            return jsonify({
                'message': 'Market closed - skipping monitor',
                'timestamp': datetime.now().isoformat()
            })

        try:
            log_system_event(
                logger,
                event_type="position_monitoring_triggered",
                status="starting"
            )

            # Initialize trading components
            config = strategy_config()

            from src.api.alpaca_client import AlpacaClient
            from src.api.market_data import MarketDataManager
            from src.strategy.put_seller import PutSeller
            from src.strategy.call_seller import CallSeller

            alpaca_client = AlpacaClient(config)
            market_data = MarketDataManager(alpaca_client, config)
            put_seller = PutSeller(alpaca_client, market_data, config)
            call_seller = CallSeller(alpaca_client, market_data, config)

            # Get current positions
            positions = alpaca_client.get_positions()
            option_positions = [p for p in positions if p.get('asset_class') == 'us_option']

            logger.info("Monitoring positions for early close",
                       event_category="system",
                       event_type="position_monitoring_started",
                       total_positions=len(positions),
                       option_positions=len(option_positions))

            closed_positions = []
            errors = []

            # Evaluate each option position
            for position in option_positions:
                try:
                    symbol = position['symbol']
                    qty = int(position['qty'])

                    # FC-045: classify by the OCC contract, not by substring.
                    # `symbol` is a full OCC symbol, so `'P' in symbol` tested
                    # the TICKER's own letters: every AAPL, SPY and PFE option
                    # matched is_put -- and because is_put was checked first,
                    # covered calls on those three underlyings were evaluated by
                    # should_close_put_early and tagged position_type='PUT'.
                    # Same defect family as FC-041/FC-043/FC-048.
                    opt_type = strict_option_type(symbol)
                    is_put = opt_type == 'put'
                    is_call = opt_type == 'call'

                    # Check if position should be closed early
                    should_close = False

                    if is_put:
                        should_close = put_seller.should_close_put_early(position)
                        position_type = 'PUT'
                    elif is_call:
                        should_close = call_seller.should_close_call_early(position)
                        position_type = 'CALL'
                    else:
                        logger.warning("Unknown option type", event_category="error", event_type="unknown_option_type", symbol=symbol)
                        continue

                    if should_close:
                        # --- Dedup: skip if already closed today ---
                        if symbol in _closed_today:
                            logger.debug("Skipping already-closed position",
                                        event_category="filtering",
                                        event_type="close_dedup_skipped",
                                        symbol=symbol)
                            continue

                        logger.info("Position reached profit target - closing",
                                   event_category="execution",
                                   event_type="early_close_triggered",
                                   symbol=symbol,
                                   type=position_type,
                                   unrealized_pl=position.get('unrealized_pl'),
                                   market_value=position.get('market_value'))

                        # Extract underlying symbol from option symbol
                        import re
                        underlying_match = re.match(r'^([A-Z]+)', symbol)
                        underlying = underlying_match.group(1) if underlying_match else symbol[:symbol.find('2') if '2' in symbol else len(symbol)]

                        # --- Get option quote for smart limit order pricing ---
                        # Strategy: Use the option's current bid/ask to set an
                        # intelligent limit. We're buying back a short position, so
                        # we want to pay as little as possible while still filling.
                        #
                        # - Bid: lowest price (best for us, may not fill)
                        # - Ask: highest price (guaranteed fill, worst for us = market order)
                        # - Our limit: ask * 0.95 (5% below ask) — captures most of
                        #   the spread savings while maintaining high fill probability.
                        #   If the spread is $0.50-$0.80, we'd pay ~$0.76 vs $0.80 ask.
                        #   For wider spreads the savings are larger.
                        #
                        # If the limit doesn't fill by EOD (DAY order), the position
                        # stays open and will be re-evaluated next cycle.
                        close_limit_price = None
                        option_bid = None
                        option_ask = None
                        try:
                            option_quote = alpaca_client.get_option_quote(symbol)
                            option_bid = option_quote.get('bid', 0)
                            option_ask = option_quote.get('ask', 0)
                            if option_ask > 0 and option_bid > 0:
                                # Set limit at 95% of ask (saves ~5% of spread)
                                close_limit_price = round(option_ask * 0.95, 2)
                                # Floor: never below the bid (would never fill)
                                close_limit_price = max(close_limit_price, option_bid)
                        except Exception:
                            pass  # Fall back to market order

                        # Place buy-to-close order (limit if quote available, market as fallback)
                        if close_limit_price and close_limit_price > 0:
                            close_result = alpaca_client.place_option_order(
                                symbol=symbol,
                                qty=abs(qty),
                                side='buy',
                                order_type='limit',
                                limit_price=close_limit_price
                            )
                        else:
                            close_result = alpaca_client.place_option_order(
                                symbol=symbol,
                                qty=abs(qty),
                                side='buy',
                                order_type='market'
                            )

                        # --- Capture fill price from Alpaca ---
                        fill_price = None
                        order_id = close_result.get('order_id')
                        if close_result.get('success') and order_id:
                            _closed_today.add(symbol)
                            try:
                                import time as _time
                                _time.sleep(1)  # Brief wait for fill
                                order_detail = alpaca_client.get_order_by_id(order_id)
                                fill_price = order_detail.get('filled_avg_price')
                            except Exception:
                                pass  # Best-effort; authoritative fills come from
                                #      the activities ingestor (trades_from_activities)

                        # Calculate profit/loss percentage
                        unrealized_pl = float(position.get('unrealized_pl', 0))
                        market_value = abs(float(position.get('market_value', 0)))
                        profit_pct = (unrealized_pl / market_value * 100) if market_value > 0 else 0

                        closed_positions.append({
                            'symbol': symbol,
                            'type': position_type,
                            'qty': abs(qty),
                            'unrealized_pl': unrealized_pl,
                            'profit_pct': profit_pct,
                            'close_result': close_result,
                            'limit_price': close_limit_price,
                            'fill_price': fill_price
                        })

                        # Log trade event for BigQuery analytics
                        log_trade_event(
                            logger,
                            event_type="early_close_executed",
                            symbol=symbol,
                            strategy=f"close_{position_type.lower()}",
                            success=close_result.get('success', True),
                            underlying=underlying,
                            option_type=position_type,
                            contracts=abs(qty),
                            unrealized_pl=unrealized_pl,
                            profit_pct=round(profit_pct, 2),
                            order_id=order_id,
                            reason="profit_target_reached",
                            market_value=market_value,
                            entry_price=float(position.get('avg_entry_price', 0)),
                            exit_price=fill_price or float(position.get('current_price', 0)),
                            limit_price=close_limit_price,
                            fill_price=fill_price,
                            bid=option_bid,
                            ask=option_ask
                        )

                        logger.info("Position closed successfully",
                                   event_category="execution",
                                   event_type="position_closed_early",
                                   symbol=symbol,
                                   type=position_type,
                                   unrealized_pl=unrealized_pl,
                                   profit_pct=round(profit_pct, 2),
                                   order_id=order_id,
                                   limit_price=close_limit_price,
                                   fill_price=fill_price,
                                   bid=option_bid,
                                   ask=option_ask)

                except Exception as pos_error:
                    error_msg = f"Failed to evaluate position {position.get('symbol')}: {str(pos_error)}"
                    logger.error("Position monitoring error",
                                event_category="error",
                                event_type="position_monitoring_error",
                                symbol=position.get('symbol'),
                                error=str(pos_error))
                    errors.append(error_msg)

            # Calculate monitoring duration
            duration_seconds = (datetime.now() - start_time).total_seconds()

            # Log completion
            log_system_event(
                logger,
                event_type="position_monitoring_completed",
                status="completed",
                duration_seconds=duration_seconds,
                positions_evaluated=len(option_positions),
                positions_closed=len(closed_positions),
                errors=len(errors)
            )

            # Update global status
            strategy_status['last_monitor'] = datetime.now().isoformat()
            strategy_status['status'] = 'monitor_completed'

            # Write analytics execution record
            try:
                from src.data.analytics_writer import get_analytics_writer
                analytics = get_analytics_writer()
                analytics.write_execution(
                    endpoint="/monitor",
                    status="completed",
                    duration_seconds=duration_seconds,
                    trades_executed=len(closed_positions),
                    errors=len(errors),
                )
            except Exception:
                logger.debug("Analytics write failed for /monitor", exc_info=True)

            return jsonify({
                'message': 'Position monitoring completed',
                'timestamp': datetime.now().isoformat(),
                'results': {
                    'positions_evaluated': len(option_positions),
                    'positions_closed': len(closed_positions),
                    'closed_positions': closed_positions,
                    'errors': errors
                }
            })

        except Exception as e:
            error_msg = f"Position monitoring failed: {str(e)}"
            logger.error("Position monitoring exception",
                        event_category="error",
                        event_type="position_monitoring_exception",
                        error=str(e),
                        traceback=traceback.format_exc())

            log_system_event(
                logger,
                event_type="position_monitoring_failed",
                status="error",
                error=str(e),
                duration_seconds=(datetime.now() - start_time).total_seconds()
            )

            # Write analytics error + failed execution
            try:
                from src.data.analytics_writer import get_analytics_writer
                analytics = get_analytics_writer()
                analytics.write_error(
                    event_type="position_monitoring_exception",
                    error_type=type(e).__name__,
                    error_message=str(e),
                    component="cloud_run_server",
                    recoverable=True,
                    stack_trace=traceback.format_exc(),
                )
                analytics.write_execution(
                    endpoint="/monitor",
                    status="error",
                    duration_seconds=(datetime.now() - start_time).total_seconds(),
                    errors=1,
                )
            except Exception:
                logger.debug("Analytics write failed for /monitor error", exc_info=True)

            return jsonify({'error': error_msg}), 500

@app.route('/roll', methods=['POST'])
@require_api_key
@require_account_match
def trigger_roll():
    """Daily credit-only call rolling cycle (FC-006, revived by FC-078).

    Evaluates short call positions for rolling when the underlying has rallied
    through the strike. Executes BTC -> STO as two sequential limit orders whose
    placed prices must net a credit, so a filled roll can never net a debit.

    Runs every trading day: the Friday-only weekday guard was deleted in FC-078
    (DD-2/DD-4 — a churned book's calls are 5-7 DTE on any given day, and 52% of
    call expiries are mid-week, which a Friday-only job can never see). The
    market-open guard and ``strategy_lock`` stay; the lock is what serializes
    ``/roll`` against ``/monitor`` and ``/run`` so two managers of the same
    positions cannot interleave.
    """
    with strategy_lock:
        start_time = datetime.now()

        if not _is_market_open():
            logger.info("Market closed - skipping roll",
                       event_category="system",
                       event_type="roll_skipped_market_closed")
            return jsonify({
                'message': 'Market closed - skipping roll',
                'timestamp': datetime.now().isoformat()
            })

        try:
            config = strategy_config()

            from src.strategy.wheel_engine import WheelEngine
            engine = WheelEngine(config)
            results = engine.run_rolling_cycle()

            return jsonify({
                'status': 'completed',
                'results': results,
                'timestamp': datetime.now().isoformat(),
                'duration_seconds': round((datetime.now() - start_time).total_seconds(), 2)
            })

        except Exception as e:
            error_msg = f"Rolling cycle error: {str(e)}"
            logger.error(error_msg,
                        event_category="error",
                        event_type="roll_cycle_error",
                        error=str(e), exc_info=True)
            log_error_event(
                logger,
                error_type="roll_cycle_exception",
                error_message=str(e),
                component="cloud_run_server",
                recoverable=True,
            )
            return jsonify({'error': error_msg}), 500


@app.route('/account', methods=['GET'])
@require_api_key
def get_account():
    """Get Alpaca account information."""
    logger.debug("Endpoint called", event_category="system", event_type="get_account", endpoint="/account")
    try:
        config = strategy_config()
        from src.api.alpaca_client import AlpacaClient

        client = AlpacaClient(config)
        account_info = client.get_account()

        # Add timestamp
        account_info['timestamp'] = datetime.now().isoformat()
        account_info['paper_trading'] = config.paper_trading

        return jsonify(account_info)

    except Exception as e:
        logger.error("Failed to get account info",
                    event_category="error",
                    event_type="account_info_failed",
                    error=str(e),
                    traceback=traceback.format_exc())
        return jsonify({'error': f"Failed to get account info: {str(e)}"}), 500

@app.route('/positions', methods=['GET'])
@require_api_key
def get_positions():
    """Get current positions."""
    logger.debug("Endpoint called", event_category="system", event_type="get_positions", endpoint="/positions")
    try:
        config = strategy_config()
        from src.api.alpaca_client import AlpacaClient

        client = AlpacaClient(config)
        positions = client.get_positions()

        return jsonify({
            'positions': positions,
            'count': len(positions),
            'timestamp': datetime.now().isoformat()
        })

    except Exception as e:
        logger.error("Failed to get positions",
                    event_category="error",
                    event_type="positions_fetch_failed",
                    error=str(e),
                    traceback=traceback.format_exc())
        return jsonify({'error': f"Failed to get positions: {str(e)}"}), 500

@app.route('/config', methods=['GET'])
@require_api_key
def get_config():
    """Get current configuration (sanitized)."""
    logger.debug("Endpoint called", event_category="system", event_type="get_config", endpoint="/config")
    try:
        config = strategy_config()

        # Return sanitized config (no secrets)
        config_data = {
            'paper_trading': True,  # Always true for cloud deployment
            'stock_symbols': getattr(config, 'stock_symbols', []),
            # FC-069 S1 removed three fields from this response:
            # `max_positions` (item 1 — the global cap knob is deleted; breadth
            # is bounded by buying power and the ticker universe),
            # `gap_thresholds` (item 5 — GapDetector and its knobs are deleted;
            # gap risk is absent by decision), and
            # `call_drawdown_pause_threshold` (item 9 — there is no drawdown
            # pause; FC-065 P4 already removed the dashboard read). None had a
            # dashboard consumer at the time of removal.
            # FC-031: the dashboard reads the delta ranges so its calibration
            # bands track the live strategy config instead of hardcoding
            # parallel constants.
            'put_delta_range': getattr(config, 'put_delta_range', [0.10, 0.20]),
            'call_delta_range': getattr(config, 'call_delta_range', [0.15, 0.25]),
        }

        return jsonify(config_data)

    except Exception as e:
        logger.error("Config retrieval failed",
                    event_category="error",
                    event_type="config_retrieval_failed",
                    error=str(e))
        return jsonify({'error': f"Config retrieval failed: {str(e)}"}), 500

# A screened symbol replays a year of decisions; measured at roughly a minute
# each, so only a handful fit inside a synchronous request. The full universe
# belongs in a Cloud Run Job.
SCREEN_REQUEST_TIMEOUT_S = 300
SCREEN_SYNC_SYMBOL_LIMIT = 3

# The endpoint is DISABLED by default and must stay that way until two things
# land: the Cloud Run Job (the only primitive that can actually finish a run)
# and ChainStore wiring (measured ~25 min/symbol cold, ~50 with sensitivity —
# no synchronous request can complete even ONE symbol inside the 300s timeout).
#
# It is not dead code: it is the reviewed, tested path that the Job will use,
# and it is useful locally. But shipping a button that cannot work invites
# someone to press it, burn ~25 min of the Alpaca quota the live bot shares,
# and get a timeout with no record. Set ENABLE_SCREEN_ENDPOINT=true to opt in
# once the Job exists.
SCREEN_ENDPOINT_ENV = "ENABLE_SCREEN_ENDPOINT"


@app.route('/backtest/screen', methods=['POST'])
@require_api_key
def backtest_screen():
    """Screen symbols for wheel fitness (FC-032 Phase 5).

    NOT currently on a schedule. `monthly-performance-review` is PAUSED and
    still targets the deleted /backtest/performance-comparison; re-pointing it
    is an outstanding deploy step, and the monthly full-universe run needs a
    Cloud Run Job rather than this endpoint (see the symbol limit below).

    Writes one row per symbol to options_wheel.backtest_runs and returns the
    tally plus demotion candidates.

    Demotion is a RECOMMENDATION. This endpoint never changes the trading
    universe — the plan requires two observed cycles before any automation, and
    the engine's known biases (dividends and early assignment unmodeled, both
    flattering the wheel; a single vol regime) are why.

    Long-running: a full 14-symbol screen replays a year per symbol. Give the
    Cloud Run job a generous timeout and do not point a short-timeout scheduler
    at it without checking.
    """
    logger.info("Endpoint called",
                event_category="system",
                event_type="backtest_screen_endpoint",
                endpoint="/backtest/screen")

    if os.environ.get(SCREEN_ENDPOINT_ENV, "false").lower() != "true":
        return jsonify({
            'status': 'disabled',
            'reason': 'screen_endpoint_not_enabled',
            'detail': (
                'The screen endpoint is intentionally disabled. A screened '
                'symbol takes ~25 min cold (~50 with the sensitivity pass), so '
                f'no synchronous request completes inside this service\'s '
                f'{SCREEN_REQUEST_TIMEOUT_S}s timeout — not even one symbol. '
                'Running it here would burn Alpaca quota the live bot shares '
                'and time out with no record. Use `python main.py --command '
                'screen` locally, or the Cloud Run Job once deployed (see the '
                '"Running a full screen" section of docs/bigquery/'
                f'backtest_runs.md). Set {SCREEN_ENDPOINT_ENV}=true to enable.'
            ),
            'timestamp': datetime.now().isoformat(),
        }), 503

    try:
        from datetime import date as _date, datetime as _datetime

        from src.backtesting.screen import run_screen

        config = strategy_config()

        def _parse(param):
            raw = request.args.get(param)
            return _datetime.strptime(raw, "%Y-%m-%d").date() if raw else None

        symbols_arg = request.args.get('symbols')
        symbols = ([s.strip().upper() for s in symbols_arg.split(',') if s.strip()]
                   if symbols_arg else None)
        # Sensitivity doubles the runtime; allow a scheduler to skip it.
        run_sensitivity = request.args.get('sensitivity', 'true').lower() != 'false'

        # A screened symbol replays a year of decisions. MEASURED cold — the
        # only mode that exists, since ChainStore is not wired in and Cloud
        # Run's filesystem is ephemeral anyway — that is ~25 min per symbol,
        # ~50 with the sensitivity pass. Not the "~1 min" an earlier version of
        # this comment asserted without measuring.
        #
        # Against a 300s request timeout, NO synchronous multi-symbol run is
        # defensible; even one symbol overruns. A timeout does not corrupt the
        # table (persistence is a single write after the loop, so a timeout
        # writes ZERO rows, not partial ones — an earlier version of this
        # comment claimed otherwise) but it does burn ~25 min of Alpaca quota
        # shared with the live bot and leaves no record.
        #
        # So this endpoint is for genuinely small, opt-in probes only. The
        # monthly universe run belongs in the Cloud Run Job.
        requested = symbols or config.stock_symbols
        # Clamp, never trust. ?max_symbols=999 would otherwise disable the guard
        # entirely, and a non-integer would raise into the 500 handler.
        try:
            asked = int(request.args.get('max_symbols', SCREEN_SYNC_SYMBOL_LIMIT))
        except (TypeError, ValueError):
            asked = SCREEN_SYNC_SYMBOL_LIMIT
        budget = max(1, min(asked, SCREEN_SYNC_SYMBOL_LIMIT))
        if len(requested) > budget:
            return jsonify({
                'status': 'refused',
                'reason': 'too_many_symbols_for_synchronous_run',
                'requested_symbols': len(requested),
                'sync_limit': budget,
                'detail': (
                    f'A synchronous screen of {len(requested)} symbols cannot '
                    f'finish: a symbol takes ~25 min cold (~50 with the '
                    f'sensitivity pass) against this service\'s '
                    f'{SCREEN_REQUEST_TIMEOUT_S}s request timeout. A timeout '
                    f'writes NO rows (persistence is a single write after the '
                    f'loop), but it burns ~25 min of the Alpaca quota the live '
                    f'bot shares and leaves no record. Run the full '
                    f'universe as a Cloud Run Job (see the "Running a full screen" '
                    f'section of docs/bigquery/backtest_runs.md), or pass '
                    f'?symbols=A,B for an ad-hoc subset — which is recorded as '
                    f'run_kind=adhoc and will NOT be read as the current state '
                    f'of the universe.'
                ),
                'timestamp': datetime.now().isoformat(),
            }), 413

        result = run_screen(
            config=config,
            symbols=symbols,
            start=_parse('start'),
            end=_parse('end') or _date.today(),
            persist=request.args.get('persist', 'true').lower() != 'false',
            run_sensitivity=run_sensitivity,
        )

        payload = {
            'status': 'ok',
            'run_id': result.run_id,
            'window': {'start': result.start.isoformat(), 'end': result.end.isoformat()},
            'symbols_screened': len(result.results),
            'tally': result.tally(),
            'demote_candidates': result.demote_candidates,
            'failures': result.failures,
            'persisted': result.persisted,
            'note': 'demote_candidates is a recommendation for human review; '
                    'no trading universe change has been made',
            'timestamp': datetime.now().isoformat(),
        }

        # Distinguish three states that a responder must not confuse:
        #   - nothing persisted  -> 500, there is no record of this run
        #   - persisted, but some symbols produced no verdict -> 200 'partial',
        #     the run IS recorded and readable; the gaps are visible in the table
        #   - clean -> 200 'ok'
        # Collapsing the middle case into 5xx makes "recorded and mostly fine"
        # page identically to "no data at all".
        # ?persist=false is an advertised, legitimate mode; reporting 500 for
        # its documented use trains operators to ignore 500s from the one
        # endpoint whose 500 means "no record of this run exists".
        persist_requested = request.args.get('persist', 'true').lower() != 'false'
        if persist_requested and not result.persisted:
            payload['status'] = 'not_persisted'
            return jsonify(payload), 500
        if result.failures:
            payload['status'] = 'partial'
            return jsonify(payload), 200
        return jsonify(payload), 200

    except Exception as e:
        logger.error("Backtest screen failed",
                     event_category="error",
                     event_type="backtest_screen_failed",
                     error=str(e))
        return jsonify({
            'status': 'failed',
            'error': str(e),
            'timestamp': datetime.now().isoformat(),
        }), 500


@app.route('/ingest-activities', methods=['POST'])
@require_api_key
@require_account_match
def ingest_activities():
    """Pull Alpaca account activities and append to BigQuery (FC-012 §2.1).

    Triggered by Cloud Scheduler (15 min market hours, hourly off-hours).
    Append-only; idempotent by activity_id. Safe to call repeatedly.
    """
    logger.info("Endpoint called",
                event_category="system",
                event_type="ingest_activities_endpoint",
                endpoint="/ingest-activities")

    try:
        from src.api.alpaca_client import AlpacaClient
        from src.data.activities_ingestor import ActivitiesIngestor

        config = strategy_config()
        alpaca_client = AlpacaClient(config)
        ingestor = ActivitiesIngestor(
            alpaca_client,
            dataset_id=config.bigquery_dataset,
            strategy_id=config.strategy_id,
        )

        if not ingestor.enabled:
            return jsonify({
                'status': 'disabled',
                'reason': 'ActivitiesIngestor not enabled (check GCP_PROJECT env var and BQ access)',
                'timestamp': datetime.now().isoformat(),
            }), 503

        # Optional ?after=YYYY-MM-DD overrides the cursor for one-off
        # backfills (e.g., FC-019 backfill of new activity types).
        after_override = request.args.get('after')
        result = ingestor.run_once(after_override=after_override)
        result['timestamp'] = datetime.now().isoformat()

        # Map ingest status to HTTP. 'ok' and 'partial' return 200 so the
        # Cloud Scheduler does not storm-retry for per-row BQ insert errors
        # that a retry would not fix. 'failed' (pull or full insert failure)
        # returns 500 so the scheduler backs off and the failure is visible.
        status = result.get('status')
        if status in ('ok', 'disabled', 'partial'):
            http_status = 200
        else:
            http_status = 500
        return jsonify(result), http_status

    except Exception as e:
        logger.error("Activities ingest failed",
                    event_category="error",
                    event_type="ingest_activities_failed",
                    error=str(e),
                    exc_info=True)
        return jsonify({
            'status': 'failed',
            'error': str(e),
            'timestamp': datetime.now().isoformat(),
        }), 500

@app.route('/ingest-portfolio-history', methods=['POST'])
@require_api_key
@require_account_match
def ingest_portfolio_history():
    """Pull Alpaca portfolio/history and append finalized days to BigQuery (FC-012 §2.5).

    Idempotent, append-only. Designed to run once per day (EOD + 30 min).
    """
    logger.info("Endpoint called",
                event_category="system",
                event_type="ingest_portfolio_history_endpoint",
                endpoint="/ingest-portfolio-history")

    try:
        from src.api.alpaca_client import AlpacaClient
        from src.data.portfolio_history_ingestor import PortfolioHistoryIngestor

        config = strategy_config()
        alpaca_client = AlpacaClient(config)
        ingestor = PortfolioHistoryIngestor(
            alpaca_client,
            dataset_id=config.bigquery_dataset,
            strategy_id=config.strategy_id,
        )

        if not ingestor.enabled:
            return jsonify({
                'status': 'disabled',
                'reason': 'PortfolioHistoryIngestor not enabled',
                'timestamp': datetime.now().isoformat(),
            }), 503

        period = request.args.get('period', '1M')
        result = ingestor.run_once(period=period)
        result['timestamp'] = datetime.now().isoformat()

        status = result.get('status')
        if status in ('ok', 'disabled', 'partial'):
            http_status = 200
        else:
            http_status = 500
        return jsonify(result), http_status

    except Exception as e:
        logger.error("Portfolio history ingest failed",
                    event_category="error",
                    event_type="ingest_portfolio_history_failed",
                    error=str(e),
                    exc_info=True)
        return jsonify({
            'status': 'failed',
            'error': str(e),
            'timestamp': datetime.now().isoformat(),
        }), 500

@app.route('/ingest-stock-history', methods=['POST'])
@require_api_key
@require_account_match
def ingest_stock_history():
    """Pull daily Alpaca stock bars for the traded universe and append to BQ (FC-018 PR B).

    Idempotent, append-only, one row per (date, symbol). Universe is derived
    from `trades_with_outcomes.underlying` so we only fetch for symbols
    actually traded. Default backfill window: 365 days on first run; daily
    incremental thereafter.
    """
    logger.info("Endpoint called",
                event_category="system",
                event_type="ingest_stock_history_endpoint",
                endpoint="/ingest-stock-history")

    try:
        from src.api.alpaca_client import AlpacaClient
        from src.data.stock_history_ingestor import StockHistoryIngestor

        config = strategy_config()
        alpaca_client = AlpacaClient(config)
        ingestor = StockHistoryIngestor(
            alpaca_client,
            dataset_id=config.bigquery_dataset,
            strategy_id=config.strategy_id,
        )

        if not ingestor.enabled:
            return jsonify({
                'status': 'disabled',
                'reason': 'StockHistoryIngestor not enabled',
                'timestamp': datetime.now().isoformat(),
            }), 503

        backfill_days = int(request.args.get('backfill_days', 365))
        result = ingestor.run_once(backfill_days=backfill_days)
        result['timestamp'] = datetime.now().isoformat()

        status = result.get('status')
        if status in ('ok', 'disabled', 'partial'):
            http_status = 200
        else:
            http_status = 500
        return jsonify(result), http_status

    except Exception as e:
        logger.error("Stock history ingest failed",
                    event_category="error",
                    event_type="ingest_stock_history_failed",
                    error=str(e),
                    exc_info=True)
        return jsonify({
            'status': 'failed',
            'error': str(e),
            'timestamp': datetime.now().isoformat(),
        }), 500

@app.route('/health')
def detailed_health():
    """Detailed health check including dependencies."""
    health_data = {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'checks': {}
    }

    # Check configuration
    try:
        config = strategy_config()
        health_data['checks']['config'] = 'ok'
    except Exception as e:
        health_data['checks']['config'] = f'error: {str(e)}'
        health_data['status'] = 'unhealthy'

    # Check environment variables
    required_env_vars = ['ALPACA_API_KEY', 'ALPACA_SECRET_KEY']
    missing_vars = []
    for var in required_env_vars:
        if not os.getenv(var):
            missing_vars.append(var)

    if missing_vars:
        health_data['checks']['environment'] = f'missing: {", ".join(missing_vars)}'
        health_data['status'] = 'unhealthy'
    else:
        health_data['checks']['environment'] = 'ok'

    # Account-number interlock (FC-075 Seam 2). A latched mismatch means trading
    # is disabled (all trading endpoints 503) — surface it here so it is not a
    # silent red state. 'unchecked' is not unhealthy (no trading call yet).
    interlock = interlock_status()
    health_data['checks']['account_interlock'] = interlock
    if interlock == 'mismatch':
        health_data['status'] = 'unhealthy'

    return jsonify(health_data)

# ============================================================================
# PERFORMANCE MONITORING DASHBOARD ENDPOINTS
# ============================================================================

@app.route('/dashboard')
def performance_dashboard():
    """Get comprehensive performance dashboard data."""
    try:
        from deploy.monitoring.performance_dashboard import PerformanceMonitor

        monitor = PerformanceMonitor()
        dashboard_data = monitor.generate_dashboard_data()

        return jsonify(dashboard_data)

    except ImportError:
        # Fallback if performance monitor not available
        return jsonify({
            'status': 'limited_dashboard',
            'current_metrics': {
                'portfolio_value': 100000.0,
                'total_return': 0.045,
                'positions_count': 8,
                'win_rate': 0.75,
                'timestamp': datetime.now().isoformat()
            },
            'alerts': {'active_alerts': [], 'alert_count': 0},
            'system_health': {'overall_status': 'healthy'},
            'note': 'Full dashboard requires performance monitoring module'
        })

    except Exception as e:
        logger.error("Dashboard generation failed",
                    event_category="error",
                    event_type="dashboard_generation_failed",
                    error=str(e))
        return jsonify({'error': f"Dashboard generation failed: {str(e)}"}), 500

@app.route('/dashboard/alerts')
def get_active_alerts():
    """Get current active alerts."""
    try:
        from deploy.monitoring.performance_dashboard import PerformanceMonitor

        monitor = PerformanceMonitor()
        current_metrics = monitor.collect_current_metrics()
        alerts = monitor.check_alerts(current_metrics)

        return jsonify({
            'active_alerts': alerts,
            'alert_count': len(alerts),
            'last_check': datetime.now().isoformat(),
            'severity_summary': {
                'high': len([a for a in alerts if a.get('severity') == 'high']),
                'medium': len([a for a in alerts if a.get('severity') == 'medium']),
                'low': len([a for a in alerts if a.get('severity') == 'low'])
            }
        })

    except ImportError:
        return jsonify({
            'active_alerts': [],
            'alert_count': 0,
            'note': 'Alert monitoring requires performance dashboard module'
        })

    except Exception as e:
        logger.error("Alert check failed",
                    event_category="error",
                    event_type="alert_check_failed",
                    error=str(e))
        return jsonify({'error': f"Alert check failed: {str(e)}"}), 500

@app.route('/dashboard/metrics/export')
def export_metrics():
    """Export performance metrics in various formats."""
    try:
        format_type = request.args.get('format', 'json').lower()

        from deploy.monitoring.performance_dashboard import PerformanceMonitor

        monitor = PerformanceMonitor()
        exported_data = monitor.export_metrics(format_type)

        if format_type == 'csv':
            return exported_data, 200, {
                'Content-Type': 'text/csv',
                'Content-Disposition': f'attachment; filename=metrics_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
            }
        else:
            return exported_data, 200, {'Content-Type': 'application/json'}

    except ImportError:
        return jsonify({
            'error': 'Export functionality requires performance monitoring module'
        }), 503

    except Exception as e:
        logger.error("Metrics export failed",
                    event_category="error",
                    event_type="metrics_export_failed",
                    error=str(e))
        return jsonify({'error': f"Metrics export failed: {str(e)}"}), 500

@app.route('/dashboard/health')
def system_health_check():
    """Get detailed system health information."""
    try:
        from deploy.monitoring.performance_dashboard import PerformanceMonitor

        monitor = PerformanceMonitor()
        dashboard_data = monitor.generate_dashboard_data()

        health_info = {
            'system_health': dashboard_data.get('system_health', {}),
            'performance_summary': dashboard_data.get('performance_summary', {}),
            'risk_metrics': dashboard_data.get('risk_metrics', {}),
            'execution_metrics': dashboard_data.get('execution_metrics', {}),
            'timestamp': datetime.now().isoformat()
        }

        return jsonify(health_info)

    except ImportError:
        return jsonify({
            'system_health': {
                'overall_status': 'healthy',
                'components': {
                    'basic_monitoring': 'active',
                    'advanced_monitoring': 'unavailable'
                }
            },
            'note': 'Detailed health monitoring requires performance dashboard module'
        })

    except Exception as e:
        logger.error("Health check failed",
                    event_category="error",
                    event_type="health_check_failed",
                    error=str(e))
        return jsonify({'error': f"Health check failed: {str(e)}"}), 500

@app.route('/dashboard/trends')
def get_performance_trends():
    """Get performance trend analysis."""
    try:
        from deploy.monitoring.performance_dashboard import PerformanceMonitor

        monitor = PerformanceMonitor()
        dashboard_data = monitor.generate_dashboard_data()

        trends_data = {
            'trends': dashboard_data.get('trends', {}),
            'performance_summary': dashboard_data.get('performance_summary', {}),
            'data_points': len(monitor.metrics_history),
            'trend_analysis_period': '24 hours',
            'timestamp': datetime.now().isoformat()
        }

        return jsonify(trends_data)

    except ImportError:
        return jsonify({
            'trends': {'trend_data': 'unavailable'},
            'note': 'Trend analysis requires performance monitoring module'
        })

    except Exception as e:
        logger.error("Trend analysis failed",
                    event_category="error",
                    event_type="trend_analysis_failed",
                    error=str(e))
        return jsonify({'error': f"Trend analysis failed: {str(e)}"}), 500

# ============================================================================
# REGRESSION TESTING ENDPOINT
# ============================================================================

@app.route('/regression', methods=['POST'])
@require_api_key
def run_regression_tests():
    """Run production regression tests and return a report.

    Invoked by Cloud Scheduler during market hours to validate system health.
    Returns 200 if all checks pass, 500 if critical failures are detected.
    """
    start_time = datetime.now()
    try:
        log_system_event(
            logger,
            event_type="regression_test_triggered",
            status="starting"
        )

        from tools.testing.regression_monitor import RegressionMonitor

        # Run in internal mode -- call our own endpoints via localhost
        # to avoid Cloud Run IAM auth requirements on self-calls.
        port = os.environ.get("PORT", "8080")
        service_url = os.environ.get(
            "REGRESSION_SERVICE_URL",
            f"http://localhost:{port}",
        )
        api_key = os.environ.get("STRATEGY_API_KEY", "")

        monitor = RegressionMonitor(
            service_url=service_url,
            api_key=api_key,
            internal=True,
        )
        report = monitor.run_all_checks()

        duration_seconds = (datetime.now() - start_time).total_seconds()

        log_performance_metric(
            logger,
            metric_name="regression_endpoint_duration",
            metric_value=duration_seconds,
            metric_unit="seconds",
            overall_status=report.get("overall_status", "unknown"),
        )

        status_code = 200 if report.get("overall_status") != "fail" else 500
        return jsonify(report), status_code

    except Exception as e:
        log_error_event(
            logger,
            error_type="regression_test_exception",
            error_message=str(e),
            component="cloud_run_server",
            recoverable=True,
        )
        logger.error("Regression test failed",
                    event_category="error",
                    event_type="regression_test_exception",
                    error=str(e),
                    traceback=traceback.format_exc())
        return jsonify({
            'error': f"Regression test failed: {str(e)}",
            'timestamp': datetime.now().isoformat()
        }), 500


def setup_logging():
    """Configure structured logging."""
    import logging
    import sys

    # Configure Python's logging module to output to stderr (Cloud Run captures this)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=logging.INFO
    )

    # Configure structlog to use Python logging
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer()
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

if __name__ == '__main__':
    setup_logging()

    # Get port from environment (Cloud Run sets this)
    port = int(os.environ.get('PORT', 8080))

    logger.info("Starting Options Wheel Strategy Cloud Run server",
               event_category="system",
               event_type="server_starting",
               port=port)

    # Initialize configuration to verify everything works
    try:
        config = strategy_config()
        logger.info("Configuration loaded successfully",
                   event_category="system",
                   event_type="config_loaded")
    except Exception as e:
        logger.error("Failed to load configuration",
                    event_category="error",
                    event_type="config_load_failed",
                    error=str(e))

    # Run the Flask app
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        threaded=True
    )