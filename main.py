"""Main entry point for Options Wheel Strategy."""

import argparse
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
import json

from src.utils.config import Config
from src.data.analytics_writer import configure_analytics_writer
from src.utils.logger import setup_logging, get_logger
from src.data.options_scanner import OptionsScanner
from src.data.portfolio_tracker import PortfolioTracker
from src.api.alpaca_client import AlpacaClient
from src.api.market_data import MarketDataManager


def main():
    """Main application entry point."""
    parser = argparse.ArgumentParser(description='Options Wheel Strategy')
    parser.add_argument('--config', default='config/settings.yaml', help='Configuration file path')
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='Logging level')
    parser.add_argument('--command', required=True,
                       choices=['scan', 'status', 'report', 'backtest', 'screen',
                                'sweep', 'backfill', 'battery'],
                       help='Command to execute')

    # backtest (FC-032 evaluate mode)
    parser.add_argument('--symbol', help='backtest: symbol to evaluate')
    parser.add_argument('--start', help='backtest: window start, YYYY-MM-DD')
    parser.add_argument('--end', help='backtest: window end, YYYY-MM-DD (default: today)')
    parser.add_argument('--starting-cash', type=float, default=100_000.0,
                        help='backtest: starting capital (default 100000)')
    parser.add_argument('--fill-haircut', type=float, default=0.25,
                        help='backtest: 0=mid, 1=bid (default 0.25)')
    parser.add_argument('--no-sensitivity', action='store_true',
                        help='backtest: skip the bid-fill sensitivity replay')
    parser.add_argument('--no-persist', action='store_true',
                        help='screen: do not write results to BigQuery')
    parser.add_argument('--out', help='backtest/screen/sweep: write the markdown report here')
    parser.add_argument('--json-out', help='backtest/sweep: write the JSON report here')

    # sweep (FC-060 Layer 2 — scenario runner)
    parser.add_argument('--scenarios',
                        help='sweep: YAML file of scenarios (see docs/BACKTEST_ENGINE.md)')
    parser.add_argument('--symbols',
                        help='sweep/backfill: comma-separated universe (default: '
                             'sweep = config stocks.symbols; backfill = '
                             'stocks.symbols + stocks.candidates)')
    parser.add_argument('--holdout-start',
                        help='sweep: split the window here into fit/holdout, YYYY-MM-DD')
    # FC-060 Layer 3 — the Cloud Run Job's entry point. The spec arrives as a
    # per-execution env override rather than a file because the dashboard has no
    # writable shared filesystem with the Job and the override is atomic with
    # the execution (D2). The file path stays for the CLI, unchanged.
    parser.add_argument('--spec-env',
                        help='sweep: read the JSON spec from this environment '
                             'variable instead of --scenarios (Job mode; implies '
                             '--persist)')
    parser.add_argument('--persist', action='store_true',
                        help='sweep: write results to '
                             'options_wheel.scenario_sweeps / scenario_runs '
                             '(NEVER backtest_runs)')

    # backfill (FC-096 Phase A — the data-backfill Cloud Run Job)
    parser.add_argument('--history-days', type=int, default=None,
                        help='backfill: trailing calendar days to cover when '
                             'no --start is given (default 30)')

    args = parser.parse_args()
    
    try:
        # Setup logging first so Config init messages are captured
        setup_logging(args.log_level)
        logger = get_logger(__name__)

        # Load configuration
        config = Config(args.config)

        # FC-075 Seam 4: hand this process's profile to the AnalyticsWriter
        # singleton before anything can reach for it. The CLI selects its
        # profile with --config, NOT STRATEGY_CONFIG, which is why the
        # singleton cannot resolve its own dataset from the environment.
        configure_analytics_writer(
            dataset_id=config.bigquery_dataset,
            strategy_id=config.strategy_id,
        )

        logger.info("Starting Options Wheel Strategy",
                   event_category="system",
                   event_type="application_started",
                   command=args.command,
                   config_file=args.config)
        
        # Backtesting builds its own data stack and must not touch the live
        # trading client, so it dispatches before those are constructed.
        if args.command == 'backtest':
            run_backtest(args, config, logger)
            logger.info("Command completed successfully",
                        event_category="system", event_type="command_completed")
            return

        if args.command == 'sweep':
            rc = run_sweep_cmd(args, config, logger)
            logger.info("Command completed",
                        event_category="system", event_type="command_completed")
            if rc:
                sys.exit(rc)
            return

        if args.command == 'backfill':
            # FC-096 Phase B B4 — the composed Saturday: backfill, then measure
            # against the lake it just refreshed, in ONE execution.
            #
            # **The exit-code boundary is STRUCTURAL here, not conventional.**
            # This branch exits with the BACKFILL's code and nothing else can
            # move it: the battery's own return value is discarded, and every
            # way it can fail — including a crash, and including a `SystemExit`
            # from one of its own argument checks — is caught below and turned
            # into a `battery_degraded` log. A stale trend chart must never
            # fire the `data-backfill` Job-failure page, and "we were careful
            # to return 0" is not a guarantee; a `try` is.
            battery_requested = battery_after_backfill_requested()
            if battery_requested:
                # Resolve the wall cap BEFORE any data work. A typo'd
                # `BATTERY_MAX_SECONDS` is a configuration error, and the
                # honest place to fail on one is immediately, non-zero, with
                # nothing half-done — not six hours later, where the guard
                # above would (correctly) swallow it and the operator would
                # see a degraded battery rather than the typo they made.
                battery_max_seconds()

            backfill_started = time.monotonic()
            rc = run_backfill_cmd(args, config, logger)
            backfill_seconds = time.monotonic() - backfill_started

            if battery_requested and rc == 0:
                try:
                    # The backfill's wall time goes IN: the cap is a budget for
                    # the execution, which the two of them share.
                    run_battery_cmd(args, config, logger,
                                    elapsed_seconds=backfill_seconds)
                except KeyboardInterrupt:
                    raise
                except BaseException as exc:  # noqa: BLE001 - see the comment above
                    logger.error(
                        f"Weekly battery CRASHED after a successful backfill "
                        f"({type(exc).__name__}: {exc}). The data is fine and "
                        f"this execution still succeeds; no trend points were "
                        f"recorded.",
                        event_category="backtest",
                        event_type="battery_degraded",
                        reason="battery_crashed",
                        error=f"{type(exc).__name__}: {exc}"[:500],
                        measured=0, failed=0, skipped=0)
            elif battery_requested:
                logger.error(
                    "Weekly battery SKIPPED: the backfill it rides did not "
                    "complete, so the lake is not what a measurement would be "
                    "taken against. Fix the backfill; the battery runs with "
                    "the next execution.",
                    event_category="backtest",
                    event_type="battery_skipped_backfill_failed",
                    backfill_exit_code=rc)
            logger.info("Command completed",
                        event_category="system", event_type="command_completed")
            if rc:
                sys.exit(rc)
            return

        if args.command == 'battery':
            rc = run_battery_cmd(args, config, logger)
            logger.info("Command completed",
                        event_category="system", event_type="command_completed")
            if rc:
                sys.exit(rc)
            return

        if args.command == 'screen':
            rc = run_screen_cmd(args, config, logger)
            logger.info("Command completed",
                        event_category="system", event_type="command_completed")
            if rc:
                sys.exit(rc)
            return

        # Initialize components
        alpaca_client = AlpacaClient(config)
        market_data = MarketDataManager(alpaca_client, config)
        portfolio_tracker = PortfolioTracker(alpaca_client, config)
        scanner = OptionsScanner(alpaca_client, market_data, config)
        
        # Execute command. FC-068 removed `--command run`: it drove
        # WheelEngine.run_strategy_cycle() -- a code path production abandoned
        # in 2025 -- against the LIVE account. `--command scan` is the
        # read-only equivalent that stays; execution lives on the Cloud Run
        # server, which is the only thing that trades.
        if args.command == 'scan':
            scan_opportunities(scanner, logger)
        elif args.command == 'status':
            show_status(portfolio_tracker, logger)
        elif args.command == 'report':
            generate_report(portfolio_tracker, logger)
        
        logger.info("Command completed successfully", event_category="system", event_type="command_completed")
        
    except KeyboardInterrupt:
        logger.info("Application interrupted by user", event_category="system", event_type="application_interrupted")
        sys.exit(1)
    except Exception as e:
        logger.error("Application failed", event_category="error", event_type="application_failed", error=str(e))
        # Also surface it on the console. structlog output is easy to miss or
        # filter, so a failed command otherwise prints its banner and then
        # nothing -- e.g. `--command backtest` over a stock split printed
        # "Replaying ..." and exited 1, hiding a diagnostic that names the exact
        # date to avoid. Console-only; control flow and exit code unchanged.
        print(f"\n{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


def scan_opportunities(scanner: OptionsScanner, logger):
    """Scan for trading opportunities."""
    logger.info("Scanning for trading opportunities", event_category="system", event_type="scan_started")
    
    try:
        # Get market overview
        market_overview = scanner.get_market_overview()
        
        # Scan for opportunities
        opportunities = scanner.scan_all_opportunities()
        
        # Print results
        print("\n" + "="*60)
        print("OPTIONS OPPORTUNITIES SCAN")
        print("="*60)
        print(f"Scan Time: {opportunities['scan_timestamp']}")
        print(f"Total Opportunities: {opportunities['total_opportunities']}")
        
        # Market overview
        print(f"\nMarket Overview:")
        print(f"  Configured Stocks: {market_overview.get('configured_stocks', 0)}")
        print(f"  Suitable Stocks: {market_overview.get('suitable_stocks', 0)}")
        print(f"  Market Conditions: {market_overview.get('market_conditions', 'unknown')}")
        
        # Put opportunities
        puts = opportunities.get('puts', [])
        if puts:
            print(f"\nTop Put Opportunities ({len(puts)}):")
            print("  Rank | Symbol | Strike | Premium | DTE | Annual Return | Score")
            print("  -----|--------|--------|---------|-----|---------------|------")
            for i, put in enumerate(puts[:10], 1):
                print(f"  {i:2d}   | {put['symbol']:6s} | ${put['strike_price']:6.0f} | "
                     f"${put['premium']:5.2f}  | {put['dte']:3d} | "
                     f"{put['annual_return_percent']:8.1f}%   | {put['attractiveness_score']:5.1f}")
        
        # Call opportunities
        calls = opportunities.get('calls', [])
        if calls:
            print(f"\nTop Call Opportunities ({len(calls)}):")
            print("  Rank | Symbol | Strike | Premium | DTE | Annual Return | Score")
            print("  -----|--------|--------|---------|-----|---------------|------")
            for i, call in enumerate(calls[:10], 1):
                print(f"  {i:2d}   | {call['symbol']:6s} | ${call['strike_price']:6.0f} | "
                     f"${call['premium']:5.2f}  | {call['dte']:3d} | "
                     f"{call['annual_premium_return_percent']:8.1f}%   | {call['attractiveness_score']:5.1f}")
        
        if not puts and not calls:
            print("\nNo suitable opportunities found at this time.")
        
    except Exception as e:
        logger.error("Opportunity scan failed", event_category="error", event_type="scan_failed", error=str(e))
        print(f"\nOpportunity scan failed: {str(e)}")


def show_status(tracker: PortfolioTracker, logger):
    """Show current portfolio status."""
    logger.info("Getting portfolio status", event_category="system", event_type="status_requested")
    
    try:
        snapshot = tracker.get_current_portfolio_snapshot()
        
        print("\n" + "="*60)
        print("PORTFOLIO STATUS")
        print("="*60)
        print(f"Snapshot Time: {snapshot['timestamp']}")
        
        # Account info
        account = snapshot.get('account', {})
        print(f"\nAccount:")
        print(f"  Portfolio Value: ${account.get('portfolio_value', 0):,.2f}")
        print(f"  Cash: ${account.get('cash', 0):,.2f} ({account.get('cash', 0)/account.get('portfolio_value', 1)*100:.1f}%)")
        print(f"  Buying Power: ${account.get('buying_power', 0):,.2f}")
        print(f"  Equity: ${account.get('equity', 0):,.2f}")
        
        # Positions
        positions = snapshot.get('positions', {})
        print(f"\nPositions:")
        print(f"  Total Positions: {positions.get('total_count', 0)}")
        print(f"  Stock Positions: {positions.get('stock_positions', 0)}")
        print(f"  Option Positions: {positions.get('option_positions', 0)}")
        print(f"  Total Value: ${positions.get('total_value', 0):,.2f}")
        
        # Performance
        performance = snapshot.get('performance', {})
        total_pl = performance.get('total_unrealized_pl', 0)
        pl_percent = performance.get('unrealized_pl_percent', 0)
        print(f"\nPerformance:")
        print(f"  Unrealized P&L: ${total_pl:,.2f} ({pl_percent:+.2f}%)")
        
        # Wheel metrics
        wheel_metrics = snapshot.get('wheel_metrics', {})
        print(f"\nWheel Strategy:")
        print(f"  Active Wheels: {wheel_metrics.get('active_wheels', 0)}")
        print(f"  Cash Secured Puts: {wheel_metrics.get('cash_secured_puts', 0)}")
        print(f"  Assigned Stocks: {wheel_metrics.get('assigned_stocks', 0)}")
        print(f"  Covered Calls: {wheel_metrics.get('covered_calls', 0)}")
        
        # Underlying positions
        underlying = snapshot.get('underlying_positions', {})
        if underlying:
            print(f"\nPositions by Underlying:")
            for symbol, data in underlying.items():
                stage = data.get('wheel_stage', 'unknown')
                value = data.get('total_value', 0)
                pl = data.get('total_pl', 0)
                print(f"  {symbol}: {stage} (${value:,.0f}, P&L: ${pl:+,.0f})")
        
    except Exception as e:
        logger.error("Status retrieval failed", event_category="error", event_type="status_failed", error=str(e))
        print(f"\nStatus retrieval failed: {str(e)}")


def generate_report(tracker: PortfolioTracker, logger):
    """Generate comprehensive performance report."""
    logger.info("Generating performance report", event_category="system", event_type="report_generating")
    
    try:
        report = tracker.generate_performance_report()
        
        print("\n" + "="*60)
        print("PERFORMANCE REPORT")
        print("="*60)
        print(f"Report Date: {report['report_date']}")
        
        # Current portfolio summary
        current = report.get('current_portfolio', {})
        account = current.get('account', {})
        print(f"\nCurrent Portfolio:")
        print(f"  Value: ${account.get('portfolio_value', 0):,.2f}")
        print(f"  Cash: ${account.get('cash', 0):,.2f}")
        
        # 30-day performance
        perf_30d = report.get('performance_30d', {})
        if 'error' not in perf_30d:
            print(f"\n30-Day Performance:")
            print(f"  Total Return: ${perf_30d.get('total_return', 0):,.2f} ({perf_30d.get('total_return_percent', 0):+.2f}%)")
            print(f"  Annualized Return: {perf_30d.get('annualized_return_percent', 0):+.2f}%")
            print(f"  Volatility: {perf_30d.get('volatility_percent', 0):.2f}%")
            print(f"  Sharpe Ratio: {perf_30d.get('sharpe_ratio', 0):.2f}")
            print(f"  Max Drawdown: {perf_30d.get('max_drawdown_percent', 0):.2f}%")
        else:
            print(f"\n30-Day Performance: {perf_30d['error']}")
        
        # Risk summary
        risk_summary = report.get('risk_summary', {})
        print(f"\nRisk Summary:")
        print(f"  Cash Percentage: {risk_summary.get('cash_percentage', 0):.1f}%")
        print(f"  Max Concentration: {risk_summary.get('max_concentration', 0):.1f}% ({risk_summary.get('max_concentration_symbol', 'N/A')})")
        print(f"  Total Positions: {risk_summary.get('total_positions', 0)}")
        print(f"  Risk Level: {risk_summary.get('risk_level', 'Unknown')}")
        
        # Recent activity
        activity = report.get('recent_activity', {})
        print(f"\nRecent Activity:")
        print(f"  Recent Trades: {activity.get('total_trades', 0)}")
        
        # Recommendations
        recommendations = report.get('recommendations', [])
        if recommendations:
            print(f"\nRecommendations:")
            for i, rec in enumerate(recommendations, 1):
                print(f"  {i}. {rec}")
        
        # Export option
        print(f"\nExporting detailed data...")
        filename = tracker.export_performance_data()
        if filename:
            print(f"Data exported to: {filename}")
        
    except Exception as e:
        logger.error("Report generation failed", event_category="error", event_type="report_failed", error=str(e))
        print(f"\nReport generation failed: {str(e)}")


def run_backtest(args, config: Config, logger):
    """Evaluate one symbol's wheel fitness over a historical window (FC-032)."""
    from datetime import date, datetime

    from src.backtesting.evaluate import evaluate_symbol
    from src.backtesting.reporting.report import render_json, render_markdown

    if not args.symbol or not args.start:
        raise SystemExit("backtest requires --symbol and --start (YYYY-MM-DD)")

    start = datetime.strptime(args.start, '%Y-%m-%d').date()
    end = datetime.strptime(args.end, '%Y-%m-%d').date() if args.end else date.today()
    if end <= start:
        raise SystemExit(f"--end ({end}) must be after --start ({start})")

    symbol = args.symbol.upper()
    print(f"\nReplaying {symbol} {start} -> {end} "
          f"(${args.starting_cash:,.0f}, fill haircut {args.fill_haircut})...\n")

    report, sensitivity = evaluate_symbol(
        symbol, start, end,
        config=config,
        starting_cash=args.starting_cash,
        fill_haircut=args.fill_haircut,
        run_sensitivity=not args.no_sensitivity,
    )

    markdown = render_markdown(report)
    if sensitivity:
        markdown += _sensitivity_section(sensitivity)
    print(markdown)

    if args.out:
        with open(args.out, 'w') as fh:
            fh.write(markdown)
        print(f"\nMarkdown report -> {args.out}")
    if args.json_out:
        with open(args.json_out, 'w') as fh:
            fh.write(render_json(report, sensitivity=sensitivity))
        print(f"JSON report -> {args.json_out}")

    logger.info("Backtest completed",
                event_category="backtest", event_type="backtest_completed",
                symbol=symbol, verdict=report.verdict(),
                total_return=report.total_return)


def _backfill_env(name: str) -> str:
    """One optional Job-mode environment override, trimmed."""
    return (os.environ.get(name) or "").strip()


def _backfill_date(value: str, var: str):
    """Parse a YYYY-MM-DD backfill bound, or refuse with the source named."""
    from datetime import datetime as _dt

    try:
        return _dt.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise SystemExit(
            f"backfill: {var} must be YYYY-MM-DD, got {value!r}"
        )


def backfill_symbols(args, config: Config) -> list:
    """The universe to backfill: CLI, else Job env, else live + candidates.

    The default is the union the weekly Job exists to keep current — the live
    trading universe (`stocks.symbols`) plus the evaluation-only candidates
    (`stocks.candidates`, FC-096 A1). Order is preserved and duplicates
    collapse, so a symbol promoted from candidate to live during a config PR
    cannot be backfilled twice.

    `--symbols` beats `BACKFILL_SYMBOLS` beats the config, because the CLI is
    the human in the room: an operator running a one-off widening on a Job image
    that already carries an env default must get the symbols they typed.
    """
    raw = (args.symbols or '').strip() or _backfill_env('BACKFILL_SYMBOLS')
    if raw:
        names = [s.strip().upper() for s in raw.split(',')]
    else:
        names = [*config.stock_symbols, *config.candidate_symbols]
    out, seen = [], set()
    for name in names:
        cleaned = str(name).strip().upper()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def run_backfill_cmd(args, config: Config, logger) -> int:
    """Keep bars + the chain lake current for a symbol set (FC-096 Phase A).

    Two entry shapes, one code path — the sweep Job's arrangement, for the same
    reason (an operator and a scheduler must not be able to ask for different
    things):

    * the operator CLI (`--symbols`, `--history-days`, `--start/--end`), used
      for the one-time historical widening a chunk at a time;
    * the `data-backfill` Cloud Run Job, whose weekly execution passes no
      arguments at all and whose per-execution overrides arrive as
      `BACKFILL_SYMBOLS` / `BACKFILL_HISTORY_DAYS` / `BACKFILL_START` /
      `BACKFILL_END`. All four are optional: a bare execution backfills the
      trailing 30 days of live + candidate symbols, which is the weekly job.

    Writes nothing to BigQuery — the output is parquet objects in the shared
    chain lake and the bars cache, plus the summary below.

    Returns an exit code: non-zero if any symbol failed, or if a CONFIGURED
    chain lake died mid-run, because a run that half-happened looks exactly
    like one that worked (the FC-081 lesson) and the Job-failure alert policy
    is what turns that into a page. A run with no lake configured at all is a
    legitimate local build and exits 0.
    """
    from src.backtesting.data.backfill import (
        DEFAULT_HISTORY_DAYS,
        run_backfill,
        resolve_window,
    )
    from src.backtesting.data.chain_store import ChainStore

    symbols = backfill_symbols(args, config)
    if not symbols:
        raise SystemExit(
            "backfill has no symbols: pass --symbols / BACKFILL_SYMBOLS, or "
            "configure stocks.symbols / stocks.candidates"
        )

    raw_start = (args.start or '').strip() or _backfill_env('BACKFILL_START')
    raw_end = (args.end or '').strip() or _backfill_env('BACKFILL_END')
    start = _backfill_date(raw_start, '--start/BACKFILL_START') if raw_start else None
    end = _backfill_date(raw_end, '--end/BACKFILL_END') if raw_end else None

    history_days = args.history_days
    if history_days is None:
        raw_days = _backfill_env('BACKFILL_HISTORY_DAYS')
        if raw_days:
            try:
                history_days = int(raw_days)
            except ValueError:
                raise SystemExit(
                    f"backfill: BACKFILL_HISTORY_DAYS must be an integer, "
                    f"got {raw_days!r}"
                )
    if history_days is None:
        history_days = DEFAULT_HISTORY_DAYS
    if history_days < 1:
        raise SystemExit(
            f"backfill: history days must be >= 1, got {history_days}"
        )

    start, end = resolve_window(history_days=history_days, start=start, end=end)
    if end < start:
        raise SystemExit(f"backfill: --end ({end}) must not precede --start ({start})")

    print(f"\nBackfilling {len(symbols)} symbol(s) over {start} -> {end}...\n")

    summary = None
    try:
        # THE SIGNAL HANDLER GOES ON FIRST, before any work — the sweep Job's
        # rule, and for the same reason: `ChainStore.from_env()` probes the
        # bucket before a single day is built, and a cancel landing in that
        # window would otherwise kill the process with nothing said.
        with terminate_on_sigterm(logger, what="backfill"):
            chain_store = ChainStore.from_env()
            summary = run_backfill(
                config, symbols, start, end, chain_store=chain_store,
            )
    finally:
        # SIG_IGN around the terminal write: `terminate_on_sigterm` restores the
        # default handler exactly as this begins, and a second SIGTERM landing
        # here would kill the process mid-summary — losing the one record of
        # what a half-finished run actually wrote.
        with ignore_sigterm_while_finalising(logger, what="backfill"):
            if summary is None:
                # Terminated, or raised, before `run_backfill` returned. The
                # per-symbol progress lines are the record; say so rather than
                # printing nothing.
                logger.error(
                    "Backfill produced no summary — the run did not finish; "
                    "the per-symbol backfill_symbol_complete log lines are the "
                    "record of what was written",
                    event_category="backtest_data",
                    event_type="backfill_unfinished",
                    symbols=symbols, start=start.isoformat(),
                    end=end.isoformat(),
                )
            else:
                logger.info(
                    "Backfill complete",
                    event_category="backtest_data",
                    event_type="backfill_completed",
                    **summary.as_log(),
                )
                print(summary.render())

    failed = summary.failed_symbols()
    if failed:
        print(
            f"\nWARNING: {len(failed)} symbol(s) did not complete cleanly: "
            f"{', '.join(failed)}"
        )
    if summary.lake_failed:
        # A CONFIGURED lake that died mid-run fails the execution even if every
        # symbol looks clean. The store degrades to local-only on purpose — the
        # right answer for a backtest, which still needs its chains — but this
        # process exists to put objects in a bucket, and its filesystem is
        # destroyed when the task exits. Exiting 0 here would report a widened
        # lake to an operator who would move on to the next chunk.
        health = summary.lake_health()
        print(
            f"\nWARNING: the chain lake was configured "
            f"({health['lake_bucket']}) and was DISABLED mid-run "
            f"(reason={health['lake_disabled_reason']}, "
            f"lake_puts={health['lake_puts']}, "
            f"lake_errors={health['lake_errors']}). Days built after that "
            f"point never left this container. Re-run this window."
        )
    return 1 if summary.failed() else 0


# --------------------------------------------------------------------------- #
# FC-096 Phase B B4 — the weekly battery.
#
# One command that re-measures the standing set and every ACTIVE pin, riding the
# Saturday `data-backfill` execution so the numbers are taken against a lake
# that was refreshed minutes earlier.
#
# Three postures are load-bearing and each of them exists because of a way this
# could otherwise fail quietly:
#
# * **Per-pin isolation.** One bad pin must cost its own row and nothing else.
#   A loop that let a refusal escape would end the battery at whichever pin
#   happened to be first, and the standing set would go unmeasured for a week
#   with a Job that exited 0.
# * **Exit-code classes.** DATA failure (the backfill) is a page: the lake is
#   the substrate everything else reads. MEASUREMENT failure (the battery) is a
#   nag: a stale trend chart is not an outage, and paging for one is how the
#   channel gets filtered. So the battery exits 0 and says `battery_degraded`.
# * **A wall cap.** The battery shares a 6 h Job execution with the backfill.
#   Without a cap, an engine-change week (every stored key invalidated, so every
#   sweep genuinely replays) could run the execution into its task timeout —
#   and a SIGKILL at the timeout would take the BACKFILL's exit code with it,
#   turning a successful data run into a page.
# --------------------------------------------------------------------------- #

# Wall-clock budget for the battery, in seconds, measured across the whole
# EXECUTION rather than from this command's own start — the composed Saturday
# runs the backfill first and hands its elapsed time in (`elapsed_seconds`).
# Measuring only from here would let a 5-hour widening chunk give the battery a
# fresh 4 hours inside a 6-hour task timeout, and the SIGKILL at that timeout
# would take the BACKFILL's exit code with it: a successful data run reported
# as a Job failure, which is the exact cross-contamination the exit classes
# exist to prevent.
#
# 4 h against the Job's 6 h `--task-timeout`. The budget it has to fit:
#   longest supervised backfill chunk (measured ~66 min)
#   + this cap (14400 s)
#   + the ONE sweep that may be in flight when the cap passes
# The last term is bounded by `BATTERY_MAX_PIN_CELLS` and the lake being warm
# (the backfill just ran): a 12-symbol, 2-split pin is 24 materialisations at
# the measured ~40 s warm = ~16 min. 66 + 240 + 16 = ~322 min against 360.
# `test_the_execution_budget_fits_the_job_timeout` holds that arithmetic.
#
# It bounds when a NEW sweep may START; the one in flight is never interrupted.
# Killing it would leave a `running` row to age out and throw away a replay
# that was nearly done.
BATTERY_MAX_SECONDS = 14_400

# The most cells one PIN may ask for. Deliberately far below the API's
# per-submission ceiling (`services/sweeps.MAX_CELLS` = 240), because a pin is
# not a submission: it runs every Saturday for ever. Two 240-cell pins would
# consume the wall cap between them and everything after them would be skipped
# week after week, with nothing but the skip list to say so.
#
# 60 = e.g. 5 arms x 6 symbols x 2 splits, or a 12-symbol base pin with a
# holdout — comfortably more than the standing set asks per symbol, and small
# enough that the in-flight bound above stays true.
BATTERY_MAX_PIN_CELLS = 60

# The trailing window's holdout, in calendar days. 90 rather than the API's
# 60-day floor (`services/sweeps.MIN_HOLDOUT_DAYS`): the holdout has to contain
# enough complete wheel cycles that a symbol which traded normally is not
# reported `insuf`, and a quarter is the smallest window that reliably does at
# 7-DTE puts. The fit window is then ~9 months of the trailing year.
BATTERY_HOLDOUT_DAYS = 90

# `submitted_via` on every row the battery writes. Free-form on the column, and
# distinct on purpose: the trend queries this phase exists to feed must be able
# to select the weekly re-measurements without picking up an operator's ad-hoc
# runs, a smoke row, or vice versa.
BATTERY_SUBMITTED_VIA = 'battery'

# How many CONSECUTIVE battery attempts must have refused a pin before it earns
# a nag. Three, i.e. three weeks: one refusal is a rule that moved under a pin
# nobody has looked at yet, and nagging on the first would make the event
# meaningless by the second.
BATTERY_NAG_RUNS = 3

# The `error` prefix that marks a row as "this pin was REFUSED", as opposed to
# "this pin ran and something broke". Both are `failed` rows; only the first
# counts towards the nag, because the nag's message is "come and fix your pin"
# and a vendor outage is not something the operator can fix by editing it.
BATTERY_PIN_INVALID_PREFIX = 'pin invalid: '


def battery_max_seconds() -> int:
    """The wall cap, with the `BATTERY_MAX_SECONDS` env override applied.

    An override is how an operator runs a deliberately long catch-up battery
    (the first Saturday after an engine change replays everything) without a
    deploy. Refused rather than silently defaulted when it is not a positive
    integer: a typo that fell back to 4 h would be indistinguishable from the
    override having worked.
    """
    raw = _backfill_env('BATTERY_MAX_SECONDS')
    if not raw:
        return BATTERY_MAX_SECONDS
    try:
        seconds = int(raw)
    except ValueError:
        raise SystemExit(
            f"battery: BATTERY_MAX_SECONDS must be an integer number of "
            f"seconds, got {raw!r}")
    if seconds < 1:
        raise SystemExit(
            f"battery: BATTERY_MAX_SECONDS must be >= 1, got {seconds}")
    return seconds


def battery_after_backfill_requested() -> bool:
    """Whether this backfill execution should run the battery after itself.

    `BACKFILL_THEN_BATTERY` is set on the Job DEFINITION (`cloudbuild.yaml`), not
    per execution, so the Saturday scheduler needs no change: a bare execution
    backfills and then measures. An operator's chunked historical widening —
    which passes per-execution `BACKFILL_*` overrides — gets the battery too,
    and that is correct: it is the same freshly-widened lake.

    A closed set of true-ish spellings. Anything else, including `"1.0"` or a
    stray character, is FALSE and the run simply backfills, because the
    alternative — treating an unrecognised value as true — would start a
    four-hour measurement pass off a typo.
    """
    return _backfill_env('BACKFILL_THEN_BATTERY').lower() in (
        '1', 'true', 'yes', 'on')


def battery_standing_specs(config: Config, *, today=None) -> list:
    """The standing set: base config, one spec per live symbol, trailing year.

    One sweep PER SYMBOL rather than one sweep over all fourteen, which is the
    non-obvious half. Per symbol, a materialisation that fails (a corporate
    action the engine will not model, a symbol whose lake day is missing) costs
    that symbol's row and leaves the other thirteen measured; in one sweep it
    is one `failed` run and a week with no trend point at all. The dedup is
    also per key, so a symbol whose window did not move is free either way.

    `end` is `last_settled_day` — the SAME function the backfill resolves its
    own window with, so the battery never asks for a session the run that just
    preceded it could not have written. `end = date.today()` would ask for a
    day the lake is structurally incapable of holding (today's chain is still
    forming), which is the trailing-gap shape PR-c's coverage review found.

    Wheel-only until Phase C: the replay has no profile awareness yet, so a
    covered-call standing set would be the wheel's numbers under another name.
    """
    from datetime import timedelta

    from src.backtesting.data.bar_store import last_settled_day
    from src.backtesting.scenarios.identity import DEFAULT_STARTING_CASH
    from src.backtesting.screen import DEFAULT_LOOKBACK_DAYS

    end = last_settled_day(today)
    start = end - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    holdout_start = end - timedelta(days=BATTERY_HOLDOUT_DAYS)
    specs = []
    for symbol in config.stock_symbols:
        name = str(symbol).strip().upper()
        if not name:
            continue
        specs.append({
            'symbols': [name],
            'start': start.isoformat(),
            'end': end.isoformat(),
            # Holdout ON. An in-sample-only trend line is a record of what the
            # engine fitted, and the whole point of a weekly series is to watch
            # a config hold up out of sample.
            'holdout_start': holdout_start.isoformat(),
            'starting_cash': float(DEFAULT_STARTING_CASH),
            # OFF. The bid-fill replay doubles every cell for a scalar that
            # matters on the arm you finally choose; fourteen symbols a week is
            # the wrong place to pay for it.
            'run_sensitivity': False,
            'scenarios': [],
        })
    return specs


def battery_cell_count(spec: dict) -> int:
    """Cells a battery spec will produce: arms (incl. base) x symbols x splits.

    The same arithmetic ``services/sweeps.cell_count`` does. Duplicated rather
    than imported because the engine image ships no dashboard module, and it is
    four lines whose two consumers are pinned equal by a test.
    """
    arms = len(spec.get('scenarios') or []) + 1
    splits = 2 if spec.get('holdout_start') else 1
    return arms * len(spec.get('symbols') or []) * splits


def battery_pin_spec(pin: dict, *, today=None):
    """``(spec, dropped)`` for one pin — RE-ANCHORED to this week's window.

    This is where a pin stops being the historical window an operator typed and
    becomes the rolling one FC-096 D1 signed off. The stored ``spec_json`` is
    the record of what they asked for; ``window_days`` / ``holdout_days`` are
    the SHAPE, and both are re-anchored here to ``last_settled_day()`` — the
    same edge the backfill that just ran resolved its own window to.

    Without this the pin is frozen: its answer cannot change, the
    engine-identity dedup hits on the second Saturday and every one after it,
    and its "trend series" holds exactly one point for ever. Nothing in the UI
    would say so, which is why it is worth this much prose.

    ``dropped`` names fields removed from the stored spec — today only
    ``force``. A hand-written pin row carrying it would bypass the dedup every
    single week, for ever, on a spec whose answer the store already has; the
    API refuses it at create time and this is the belt for a row that arrived
    another way. Stripped rather than refused: the QUESTION is legitimate and
    measuring it correctly is free, whereas refusing would cost a real trend
    point over a field that only ever wasted money.

    Every failure here raises ``ValueError`` naming the problem, and the caller
    turns that into the pin's own ``failed`` row. A corrupted or hand-written
    row must not be able to end the battery for the other pins.
    """
    from datetime import timedelta

    from src.backtesting.data.bar_store import last_settled_day

    raw = pin.get('spec_json')
    if not raw:
        raise ValueError("the pin row carries no spec_json")
    try:
        spec = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"spec_json is not valid JSON ({exc})")
    if not isinstance(spec, dict):
        raise ValueError(
            f"spec_json is a {type(spec).__name__}, not a JSON object")

    window_days = pin.get('window_days')
    if window_days is None:
        raise ValueError(
            "the pin carries no window_days, so its window cannot be "
            "re-anchored. A pin without one can only have been written by "
            "hand; running it as a FIXED window would deduplicate against "
            "itself every week and record one trend point for ever, which is "
            "the failure rolling pins exist to prevent. Re-create it through "
            "POST /api/v2/sims/pins.")
    try:
        window_days = int(window_days)
    except (TypeError, ValueError):
        raise ValueError(f"window_days is not a number ({window_days!r})")
    if window_days < 1:
        raise ValueError(f"window_days must be >= 1, got {window_days}")

    holdout_days = pin.get('holdout_days')
    if holdout_days is not None:
        try:
            holdout_days = int(holdout_days)
        except (TypeError, ValueError):
            raise ValueError(f"holdout_days is not a number ({holdout_days!r})")
        if not 0 < holdout_days < window_days:
            raise ValueError(
                f"holdout_days must fall inside (0, window_days) — got "
                f"{holdout_days} against a {window_days}-day window; one of "
                f"the two splits would be empty")

    end = last_settled_day(today)
    spec['start'] = (end - timedelta(days=window_days)).isoformat()
    spec['end'] = end.isoformat()
    spec['holdout_start'] = (
        None if holdout_days is None
        else (end - timedelta(days=holdout_days)).isoformat())

    dropped = tuple(field for field in ('force',) if spec.pop(field, None))
    return spec, dropped


class BatteryItem:
    """One thing the battery measures: a spec, and what to call it in a log."""

    __slots__ = ('label', 'spec', 'pin_id', 'note')

    def __init__(self, *, label: str, spec, pin_id=None, note=None):
        self.label = label
        self.spec = spec
        self.pin_id = pin_id
        self.note = note

    @property
    def is_pin(self) -> bool:
        return self.pin_id is not None


class BatteryWriter:
    """The store, plus a memory of which ``run_id``s already have a status row.

    A thin delegate rather than a change to ``ScenarioRunWriter``, because the
    question is the BATTERY's: after an item raises, did the sweep get far
    enough to record itself? `run_sweep_cmd` writes its `running` row before it
    can fail in any way that leaves a replay behind, so "no row for this
    run_id" means the attempt failed BEFORE the store — and the battery owes
    that attempt a row of its own, or the pin's week vanishes without trace and
    the nag never counts it.

    Asking BigQuery instead would make that decision depend on how quickly a
    streamed row becomes visible to a query, which is not a property anything
    here should be sensitive to.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.recorded = set()

    def write_status(self, row) -> bool:
        run_id = row.get('run_id')
        if run_id:
            self.recorded.add(run_id)
        return self._inner.write_status(row)

    def wrote_status(self, run_id) -> bool:
        return run_id in self.recorded

    def __getattr__(self, name):
        # Everything else — `enabled`, `pins_enabled`, `write_runs`,
        # `find_done_sweep`, `list_pins`, `recent_pin_statuses` — is the real
        # writer's, unwrapped. Only `write_status` is observed.
        return getattr(self._inner, name)


def _battery_failed_row(sweep_store, *, run_id, submitted_at, item, reason,
                        engine_version, git_commit, engine_identity, logger):
    """The `failed` status row for an item that never reached the store.

    Built with the offending spec on it where that is possible, and WITHOUT it
    where it is not: `status_row` reads `spec['scenarios'][i]['name']` to count
    arms, and a pin whose stored spec is malformed enough to be refused is
    exactly the pin most likely to make that read raise. A row with no spec is
    still a row an operator can find by `pin_id`; an exception here would cost
    the pin its only record of having been refused.
    """
    common = dict(
        run_id=run_id, status=sweep_store.STATUS_FAILED,
        submitted_at=submitted_at, sweep_key=None,
        submitted_via=BATTERY_SUBMITTED_VIA,
        engine_version=engine_version, git_commit=git_commit,
        engine_identity=engine_identity,
        execution_name=os.environ.get('CLOUD_RUN_EXECUTION'),
        started_at=submitted_at, finished_at=submitted_at,
        error=f"{BATTERY_PIN_INVALID_PREFIX}{reason}",
        pin_id=item.pin_id,
    )
    try:
        return sweep_store.status_row(
            spec=item.spec if isinstance(item.spec, dict) else None, **common)
    except Exception:  # noqa: BLE001 - a malformed spec must not cost the row
        logger.warning(
            "Could not derive the scope columns from a refused pin's spec; "
            "writing the failed row without them",
            event_category="backtest", event_type="battery_pin_row_degraded",
            pin_id=item.pin_id, run_id=run_id, exc_info=True)
        return sweep_store.status_row(spec=None, **common)


def _battery_nag(writer, logger, *, item, run_id) -> bool:
    """Emit ``battery_pin_nag`` when this refusal is the third in a row.

    The current attempt is counted HERE rather than read back: the row was
    inserted a moment ago and asking BigQuery for it would make the nag depend
    on streaming-buffer visibility, which is not a property anything should be
    sensitive to. So the query asks for the previous ``BATTERY_NAG_RUNS - 1``
    attempts and every one of them must also have been a refusal.

    A short history does NOT nag: a pin created last week and refused once has
    one prior attempt, and "three consecutive weeks" is the promise this event
    makes.
    """
    if not item.is_pin:
        return False
    history = writer.recent_pin_statuses(
        item.pin_id, limit=BATTERY_NAG_RUNS - 1, exclude_run_id=run_id)
    if len(history) < BATTERY_NAG_RUNS - 1:
        return False
    if not all((row.get('error') or '').startswith(BATTERY_PIN_INVALID_PREFIX)
               for row in history):
        return False
    logger.error(
        f"Pin {item.pin_id} has been refused {BATTERY_NAG_RUNS} weeks running "
        f"— it will never be measured again until it is edited or removed",
        event_category="backtest", event_type="battery_pin_nag",
        pin_id=item.pin_id, note=item.note, weeks=BATTERY_NAG_RUNS,
        prior_run_ids=[row.get('run_id') for row in history])
    return True


def run_battery_cmd(args, config: Config, logger, *,
                    elapsed_seconds: float = 0.0) -> int:
    """Re-measure the standing set and every active pin (FC-096 Phase B B4).

    **Always returns 0.** A battery that measured nothing is a stale trend
    chart, and on the composed path this process shares its execution with the
    backfill, whose non-zero exit is a real page (the `data-backfill`
    Job-failure policy). The two failure classes are kept apart deliberately:
    the loud half of a degraded battery is the `battery_degraded` ERROR log and
    the 24 h nag policy watching it, not the exit code. See this section's
    header.

    Every submission goes through `run_sweep_cmd` — the same validators, the
    same dedup, the same store, the same artifacts. That reuse IS the
    revalidation the plan asks for: `run_sweep_cmd` refuses a spec whose
    overrides are no longer allowlisted, and it does so BEFORE it writes
    anything. A refusal therefore costs one deliberate `failed` row here and no
    orphaned `running` row — and the battery knows WHICH case it is in from
    `BatteryWriter.wrote_status`, rather than from the exception's class, so a
    pre-store failure that is not a `SystemExit` (a `ValueError` out of the
    spec parse, say) still leaves the attempt on the record.

    ``elapsed_seconds`` is time already spent by this EXECUTION before the
    battery started — the backfill's, on the composed path. The wall cap is a
    budget for the execution, not for this function: measuring only from here
    would let a 5-hour backfill hand the battery a fresh 4 hours inside a 6-hour
    task timeout, and the SIGKILL at that timeout would take the BACKFILL's
    exit code with it.
    """
    import copy
    import time
    import uuid
    from datetime import datetime, timezone

    from src.backtesting.scenarios import persist as sweep_store
    from src.backtesting.scenarios.engine_identity import engine_identity
    from src.backtesting.screen import ENGINE_VERSION

    # `--out` / `--json-out` are neutralised for the whole battery. They name
    # ONE file, and thirty sweeps writing to it in turn would leave the last
    # one's report there looking like the battery's — a silent overwrite of
    # twenty-nine results. The store is the battery's record; the files are the
    # single-sweep CLI's.
    sweep_args = copy.copy(args)
    sweep_args.out = None
    sweep_args.json_out = None

    cap_seconds = battery_max_seconds()
    started = time.monotonic() - max(float(elapsed_seconds or 0.0), 0.0)
    identity = engine_identity()
    git_commit = os.environ.get('GIT_COMMIT') or None

    inner = sweep_store.ScenarioRunWriter(dataset_id=config.bigquery_dataset)
    if not inner.enabled:
        # Nothing else is worth attempting: every sweep below would refuse to
        # replay for want of somewhere to put its rows (`run_sweep_cmd` returns
        # 2 in Job mode), and fourteen identical refusals is a worse log than
        # one explanation.
        logger.error(
            "Weekly battery could not run: the scenario store is unavailable "
            "(no BigQuery client, no GCP project, or the tables could not be "
            "reconciled). No trend points were recorded this week.",
            event_category="backtest", event_type="battery_degraded",
            reason="store_unavailable", dataset=config.bigquery_dataset,
            measured=0, failed=0, skipped=0)
        print("\nBATTERY DEGRADED: the scenario store is unavailable; nothing "
              "was measured.")
        return 0
    writer = BatteryWriter(inner)

    standing = battery_standing_specs(config)
    items = [BatteryItem(label=f"standing:{spec['symbols'][0]}", spec=spec)
             for spec in standing]
    pins = writer.list_pins(active_only=True)
    if len(pins) > sweep_store.MAX_ACTIVE_PINS:
        # NOT truncated. The cap is a write-time rule the API enforces, and a
        # store that somehow holds more active pins than that is a bug in the
        # API, not a licence to stop measuring some of them silently. The wall
        # cap is what bounds the execution.
        logger.warning(
            f"{len(pins)} active pins exceeds the cap of "
            f"{sweep_store.MAX_ACTIVE_PINS}; running all of them (the wall cap "
            f"bounds this execution) — the API should have refused the extras",
            event_category="backtest", event_type="battery_pin_cap_exceeded",
            active_pins=len(pins), cap=sweep_store.MAX_ACTIVE_PINS)
    for pin in pins:
        pin_id = pin.get('pin_id')
        try:
            spec, dropped = battery_pin_spec(pin)
        except ValueError as exc:
            # Kept as an item with a non-dict spec so it takes the ordinary
            # refusal path below: one place writes the `failed` row, counts the
            # nag and logs the event, whether the spec was unreadable, could
            # not be re-anchored, or is merely no longer legal.
            spec, dropped = str(exc), ()
        if dropped:
            logger.warning(
                f"Pin {pin_id} carried {list(dropped)}, which a pin may not: "
                f"stripped for this run. `force` on a standing weekly question "
                f"would bypass the dedup every week for ever.",
                event_category="backtest",
                event_type="battery_pin_field_stripped",
                pin_id=pin_id, dropped=list(dropped))
        items.append(BatteryItem(label=f"pin:{pin_id}", spec=spec,
                                 pin_id=pin_id, note=pin.get('note')))

    print(f"\nWeekly battery: {len(standing)} standing + {len(pins)} pinned "
          f"spec(s), wall cap {cap_seconds}s"
          f"{f' ({elapsed_seconds:.0f}s already spent this execution)' if elapsed_seconds else ''}"
          f"...\n")
    logger.info(
        "Weekly battery starting",
        event_category="backtest", event_type="battery_started",
        standing=len(standing), pins=len(pins), max_seconds=cap_seconds,
        elapsed_seconds=round(float(elapsed_seconds or 0.0), 1),
        engine_identity=identity)

    measured, failures, skipped, oversized, nags = 0, [], [], [], 0
    terminated = False

    def _refuse(item, run_id, reason):
        """One `failed` row, one loud event, and the nag — the single path."""
        nonlocal nags
        writer.write_status(_battery_failed_row(
            sweep_store, run_id=run_id,
            submitted_at=datetime.now(timezone.utc).isoformat(), item=item,
            reason=reason, engine_version=ENGINE_VERSION,
            git_commit=git_commit, engine_identity=identity, logger=logger))
        logger.error(
            f"Battery item {item.label} was REFUSED: {reason}",
            event_category="backtest", event_type="battery_pin_failed",
            label=item.label, pin_id=item.pin_id, run_id=run_id,
            reason=reason, invalid=True)
        failures.append(item.label)
        if _battery_nag(writer, logger, item=item, run_id=run_id):
            nags += 1

    try:
        # SIGTERM between items is Cloud Run reclaiming the container, and on
        # the composed path the BACKFILL has already succeeded. Converting it
        # here and summarising is what stops that reclaim from reading as a
        # data failure: without this the exception escapes `main()` and the
        # execution exits non-zero, firing the Job-failure page for a
        # measurement pass that was simply cut short. `run_sweep_cmd` installs
        # its own handler INSIDE this one for the duration of each sweep (and
        # restores this one after), so an item in flight still writes its
        # terminal row first.
        with terminate_on_sigterm(logger, what="battery"):
            for item in items:
                elapsed = time.monotonic() - started
                if elapsed >= cap_seconds:
                    # REFUSE TO START, never interrupt. The sweep in flight
                    # finishes; everything after it is skipped and named,
                    # because a battery that silently measured nine of thirty
                    # would look exactly like one that measured thirty.
                    skipped.append(item.label)
                    continue

                run_id = uuid.uuid4().hex[:16]
                if not isinstance(item.spec, dict):
                    _refuse(item, run_id, item.spec)
                    continue

                cells = battery_cell_count(item.spec)
                if cells > BATTERY_MAX_PIN_CELLS:
                    # A pin runs EVERY WEEK for ever, so its size is a standing
                    # commitment rather than one submission's cost. The API's
                    # own ceiling (240 cells) is sized for a one-off; at that
                    # size two pins would fill the wall cap on their own and
                    # everything after them would be skipped week after week,
                    # with only the skip list to say so.
                    _refuse(item, run_id,
                            f"{cells} cells exceeds the per-pin battery cap of "
                            f"{BATTERY_MAX_PIN_CELLS}. A pin is a weekly "
                            f"commitment, not one submission: split it into "
                            f"narrower pins, or run it once through POST "
                            f"/api/v2/sweeps.")
                    oversized.append(item.label)
                    continue

                try:
                    rc = run_sweep_cmd(sweep_args, config, logger,
                                       spec_override=item.spec,
                                       submitted_via=BATTERY_SUBMITTED_VIA,
                                       run_id=run_id, pin_id=item.pin_id,
                                       writer_override=writer)
                except (SweepTerminated, KeyboardInterrupt):
                    # Not this item's failure. The container is going away, and
                    # the outer handler summarises.
                    raise
                except BaseException as exc:  # noqa: BLE001 - one item, not the battery
                    reason = str(exc) or type(exc).__name__
                    if writer.wrote_status(run_id):
                        # It reached the store, so `run_sweep_cmd`'s `finally`
                        # already wrote this run's terminal row with the reason
                        # on it. A second row would be a duplicate record of one
                        # attempt.
                        logger.error(
                            f"Battery item {item.label} FAILED: "
                            f"{type(exc).__name__}: {exc}",
                            event_category="backtest",
                            event_type="battery_pin_failed",
                            label=item.label, pin_id=item.pin_id,
                            run_id=run_id,
                            reason=f"{type(exc).__name__}: {exc}",
                            invalid=False)
                        failures.append(item.label)
                    else:
                        # Nothing was written, so the failure happened before
                        # the store section — which is, by construction, a
                        # refusal to accept the spec, whatever class it arrived
                        # as. `SystemExit` from a validator and `ValueError`
                        # from the spec parse are the same event to an operator
                        # and must count towards the same nag.
                        _refuse(item, run_id, reason)
                    continue

                if rc:
                    # A non-zero exit means the run is not trustworthy as a
                    # record — errored cells, or nothing persisted. Its row is
                    # already in the store saying so; this counts it so the
                    # summary is honest.
                    logger.error(
                        f"Battery item {item.label} completed with exit code {rc}",
                        event_category="backtest",
                        event_type="battery_pin_failed",
                        label=item.label, pin_id=item.pin_id, run_id=run_id,
                        reason=f"exit code {rc}", invalid=False)
                    failures.append(item.label)
                else:
                    measured += 1
    except (SweepTerminated, KeyboardInterrupt) as exc:
        terminated = True
        remaining = [i.label for i in items
                     if i.label not in failures and i.label not in skipped]
        logger.warning(
            f"Weekly battery TERMINATED mid-run ({exc}); {measured} item(s) "
            f"were measured before the container was reclaimed",
            event_category="backtest", event_type="battery_terminated",
            measured=measured, failed=len(failures),
            not_started=max(len(remaining) - measured, 0))

    wall = round(time.monotonic() - started, 1)
    summary = (f"battery: {measured} measured, {len(failures)} failed, "
               f"{len(skipped)} skipped in {wall}s")
    print(f"\n{summary}")
    if failures or skipped or terminated:
        if terminated:
            reason = "terminated"
        elif oversized and not skipped and len(oversized) == len(failures):
            reason = "pin_too_large"
        elif skipped and not failures:
            reason = "wall_cap"
        else:
            reason = "items_failed"
        logger.error(
            f"Weekly battery DEGRADED — {summary}. Trend series for the "
            f"affected specs have no point this week; the data backfill that "
            f"preceded this is unaffected.",
            event_category="backtest", event_type="battery_degraded",
            reason=reason,
            measured=measured, failed=len(failures), skipped=len(skipped),
            failed_labels=failures[:20], skipped_labels=skipped[:20],
            oversized_labels=oversized[:20],
            nags=nags, wall_seconds=wall, max_seconds=cap_seconds)
        if skipped:
            print(f"WALL CAP {cap_seconds}s reached — not started: "
                  f"{', '.join(skipped)}")
    else:
        logger.info(
            "Weekly battery complete",
            event_category="backtest", event_type="battery_completed",
            measured=measured, wall_seconds=wall, max_seconds=cap_seconds)
    # ALWAYS 0, including after a SIGTERM. See the docstring: a stale trend
    # chart is not a page, and the backfill's exit code must not be able to
    # inherit this one's opinion.
    return 0



def run_screen_cmd(args, config: Config, logger) -> int:
    """Screen the whole universe (FC-032 Phase 5). Returns a process exit code."""
    from datetime import date, datetime

    from src.backtesting.screen import run_screen, render_screen_summary

    start = datetime.strptime(args.start, '%Y-%m-%d').date() if args.start else None
    end = datetime.strptime(args.end, '%Y-%m-%d').date() if args.end else date.today()
    symbols = [args.symbol.upper()] if args.symbol else None

    result = run_screen(
        config=config, symbols=symbols, start=start, end=end,
        starting_cash=args.starting_cash,
        persist=not args.no_persist,
        run_sensitivity=not args.no_sensitivity,
    )

    summary = render_screen_summary(result)
    print(summary)
    if args.out:
        with open(args.out, 'w') as fh:
            fh.write(summary)
        print(f"\nSummary -> {args.out}")

    # Exit non-zero when the run is not trustworthy as a record: a symbol that
    # never got a verdict, or results that never reached BigQuery. A screen that
    # silently half-ran reads as a complete one.
    if result.failures:
        print(f"\nWARNING: {len(result.failures)} symbol(s) produced no verdict: "
              f"{', '.join(result.failures)}")
        return 1
    if not args.no_persist and not result.persisted:
        print("\nWARNING: results were NOT persisted to BigQuery.")
        return 1
    return 0


def load_scenarios(path: str):
    """Parse a scenario YAML into ``Scenario`` objects.

    Shape::

        scenarios:
          - name: tighter_puts
            overrides:
              strategy.put_delta_range: [0.08, 0.15]
          - name: at_the_bid
            fill_haircut: 1.0

    The ``base`` scenario (no overrides) is implicit and always runs first — the
    runner prepends it — so a file never has to declare the comparator, and every
    other row in the report is read relative to it.

    Every field is validated here rather than at replay time: a typo in the tenth
    scenario must fail in milliseconds, not after nine arms have been replayed.
    """
    import yaml

    with open(path) as fh:
        payload = yaml.safe_load(fh) or {}
    raw = payload.get('scenarios')
    if raw is None:
        raise SystemExit(
            f"{path}: expected a top-level 'scenarios:' list "
            f"(got keys: {sorted(payload) or 'an empty file'})"
        )
    return _scenarios_from_entries(raw, path)


def _scenarios_from_entries(raw, where: str):
    """Validate a list of scenario mappings into ``Scenario`` objects.

    ``where`` names the source in every message — a YAML path for the CLI, the
    literal ``'sweep spec'`` for the Job. Shared between the two entry shapes on
    purpose (FC-060 Layer 3): the API and the file must be held to one standard,
    or the dashboard eventually accepts an arm the Job then refuses three minutes
    into a container start.
    """
    from src.backtesting.scenarios import Scenario
    from src.backtesting.scenarios.identity import validate_scenario_name

    if not isinstance(raw, list):
        raise SystemExit(f"{where}: 'scenarios' must be a list, got {type(raw).__name__}")

    scenarios = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SystemExit(f"{where}: scenario #{i + 1} must be a mapping")
        name = entry.get('name')
        if not name:
            raise SystemExit(f"{where}: scenario #{i + 1} has no 'name'")
        # The SAME rule the API applies (identity.validate_scenario_name).
        # Without it a `--persist` CLI sweep could land a name the API would have
        # refused, and the results view would then have to render a column header
        # it was designed never to receive.
        try:
            validate_scenario_name(str(name), where)
        except ValueError as exc:
            raise SystemExit(str(exc))
        overrides = entry.get('overrides') or {}
        if not isinstance(overrides, dict):
            raise SystemExit(
                f"{where}: scenario '{name}' has a non-mapping 'overrides'"
            )
        haircut = entry.get('fill_haircut')
        if haircut is not None:
            try:
                haircut = float(haircut)
            except (TypeError, ValueError):
                raise SystemExit(
                    f"{where}: scenario '{name}' fill_haircut={haircut!r} is not "
                    "a number"
                )
            if not 0.0 <= haircut <= 1.0:
                raise SystemExit(
                    f"{where}: scenario '{name}' fill_haircut={haircut} is outside "
                    "[0, 1] (0 = mid, 1 = at the bid)"
                )
        unknown = set(entry) - {'name', 'overrides', 'fill_haircut'}
        if unknown:
            raise SystemExit(
                f"{where}: scenario '{name}' has unknown field(s) "
                f"{sorted(unknown)}. A misspelled field would silently do nothing."
            )
        scenarios.append(Scenario(str(name), dict(overrides), haircut))
    return scenarios


# The sweep spec's top-level fields (FC-060 D2). A misspelled field must fail
# loudly rather than be silently dropped: a spec whose `holdout_start` was typed
# `holdout` would run IN-SAMPLE ONLY and report itself as validated.
SPEC_FIELDS = frozenset({
    'symbols', 'start', 'end', 'holdout_start', 'starting_cash',
    'run_sensitivity', 'scenarios', 'force',
})

# Cloud Run caps a container's whole environment at 32 KiB and the spec is one
# variable among several, so it gets a conservative slice of that. The dashboard
# enforces the same ceiling before it launches; this is the Job-side backstop for
# a spec that arrived some other way.
MAX_SPEC_BYTES = 24 * 1024


def load_spec_from_env(var: str) -> dict:
    """Parse the JSON sweep spec out of environment variable ``var`` (D2).

    Job mode. The spec travels as a per-execution ``containerOverrides`` env
    entry rather than as a file because the dashboard shares no writable
    filesystem with the Job, and because the override is atomic with the
    execution — there is no window in which a launched execution has no spec, or
    somebody else's.
    """
    raw = os.environ.get(var)
    if not raw or not raw.strip():
        raise SystemExit(
            f"--spec-env {var}: the environment variable is unset or empty. In "
            f"Job mode the spec arrives as a per-execution override; an "
            f"execution launched without one has nothing to replay."
        )
    if len(raw.encode('utf-8')) > MAX_SPEC_BYTES:
        raise SystemExit(
            f"--spec-env {var}: spec is {len(raw.encode('utf-8'))} bytes, over "
            f"the {MAX_SPEC_BYTES}-byte limit."
        )
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--spec-env {var}: not valid JSON ({exc})")
    if not isinstance(spec, dict):
        raise SystemExit(
            f"--spec-env {var}: expected a JSON object, got {type(spec).__name__}"
        )
    unknown = set(spec) - SPEC_FIELDS
    if unknown:
        raise SystemExit(
            f"--spec-env {var}: unknown field(s) {sorted(unknown)}. "
            f"Known fields: {sorted(SPEC_FIELDS)}."
        )
    return spec


def scenarios_from_spec(spec: dict):
    """``Scenario`` objects from the JSON spec's ``scenarios`` list.

    Deliberately routed through the same validator as the YAML path
    (``_scenarios_from_entries``) so a spec the dashboard accepted and a file an
    operator wrote are held to one standard. The alternative — two parsers — is
    how an API ends up accepting an arm the Job then refuses, three minutes into
    a container start.
    """
    raw = spec.get('scenarios')
    if raw is None:
        raise SystemExit("sweep spec: expected a 'scenarios' list")
    return _scenarios_from_entries(raw, 'sweep spec')


def _spec_date(spec: dict, field: str):
    """One ISO date out of the spec, or None. Raises SystemExit on a bad one."""
    from datetime import datetime as _dt

    value = spec.get(field)
    if value in (None, ''):
        return None
    try:
        return _dt.strptime(str(value), '%Y-%m-%d').date()
    except ValueError:
        raise SystemExit(
            f"sweep spec: '{field}' must be YYYY-MM-DD, got {value!r}"
        )


class SweepTerminated(BaseException):
    """Cloud Run asked this task to stop. Raised from the SIGTERM handler.

    A ``BaseException`` rather than an ``Exception`` on purpose: it must not be
    swallowed by any `except Exception` inside the replay loop — `run_sweep`
    catches per-scenario failures broadly so one bad arm does not cost the others
    their results, and a termination caught there would be recorded as "this arm
    errored" while the container died around it.
    """


@contextmanager
def terminate_on_sigterm(logger, *, what: str = "sweep"):
    """Turn SIGTERM into an exception so the store's ``finally`` still runs.

    Cloud Run sends SIGTERM and then SIGKILL **10 seconds later** — on a task
    timeout, on a cancelled execution, on an infrastructure eviction. Python's
    default SIGTERM handler exits the interpreter immediately: no `finally`, no
    terminal status row. The sweep would then sit in the store as `running`
    forever, block the next submission until the lock expires, and tell an
    operator nothing about why.

    **The ten seconds is the budget for the `finally`, not for this block.** A
    signal arriving here raises immediately and unwinds; what has to fit inside
    the grace period is `_finalise_sweep_status` — two streaming inserts, which
    fit comfortably. That is the whole reason converting the signal is worth
    doing rather than accepting the loss.

    **Entered before the `running` row is written**, not just around the replay
    (round-2 fix 2). The `running` insert, the dedup query (up to 60 s) and
    `ChainStore.from_env()`'s bucket probe all happen before a single day is
    replayed, and a cancel landing in that window used to kill the process
    outright — leaving exactly the orphaned `running` row this mechanism exists
    to prevent, in the minutes when a cancel is most likely.

    Restores the previous handler on the way out, and no-ops off the main thread
    (`signal.signal` raises there) so tests and library callers are unaffected.

    ``what`` names the command in the log event only (``sweep_sigterm``,
    ``backfill_sigterm``). FC-096 Phase A gave the backfill Job the same
    mechanism rather than a second copy of it — the shape is identical (long
    Job, terminal write that must survive the grace period), and two copies is
    how one of them stops getting the next fix. The default keeps the sweep's
    events byte-identical.
    """
    import signal

    def _raise(signum, _frame):
        logger.warning(
            f"SIGTERM received — recording a terminal {what} status before the "
            "container is killed",
            event_category="backtest", event_type=f"{what}_sigterm",
            signal=signum)
        raise SweepTerminated(
            f"the container received signal {signum} (Cloud Run sends SIGTERM "
            f"then SIGKILL 10s later): task timeout, cancelled execution, or "
            f"eviction")

    try:
        previous = signal.signal(signal.SIGTERM, _raise)
    except (ValueError, OSError, AttributeError):
        # Not the main thread, or a platform without SIGTERM. Nothing to install
        # and nothing to restore; the sweep runs exactly as before.
        yield None
        return
    try:
        yield _raise
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def ignore_sigterm_while_finalising(logger, *, what: str = "sweep"):
    """Hold SIGTERM off for the duration of the terminal writes.

    `terminate_on_sigterm` restores the previous handler when its block exits —
    which is exactly when `_finalise_sweep_status` begins. A SIGTERM landing in
    that window hits the default handler and kills the process mid-insert, with
    no terminal row: the precise outcome the whole mechanism exists to prevent,
    displaced by a few milliseconds into the one stretch of code that must not
    be interrupted (round-2 fix 3).

    SIG_IGN rather than another raising handler: there is nothing useful left to
    do about a second signal, and the writes finish well inside Cloud Run's
    10-second grace. If SIGKILL arrives anyway the row is lost either way, and
    ignoring costs nothing.

    ``what`` names the command in the restore-failure event only; see
    ``terminate_on_sigterm``.
    """
    import signal

    try:
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (ValueError, OSError, AttributeError):
        yield
        return
    try:
        yield
    finally:
        try:
            signal.signal(signal.SIGTERM, previous)
        except (ValueError, OSError):  # pragma: no cover - defensive
            logger.warning("could not restore the SIGTERM handler",
                           event_category="backtest",
                           event_type=f"{what}_sigterm_restore_failed")


class SweepPersistence:
    """Where a sweep was stored, handed to the renderers so they tell the truth.

    Before FC-060 Layer 3 the report asserted "this report is the only record of
    the run" unconditionally and the JSON hardcoded ``"persisted": false``. Both
    became false statements the moment `--persist` existed, in the two places a
    reader trusts most.
    """

    def __init__(self, *, persisted: bool, run_id: str, sweep_key: str,
                 dataset: str) -> None:
        self.persisted = persisted
        self.run_id = run_id
        self.sweep_key = sweep_key
        self.dataset = dataset


def run_sweep_cmd(args, config: Config, logger, *,
                  spec_override=None, submitted_via=None, run_id=None,
                  pin_id=None, writer_override=None) -> int:
    """Replay many scenarios over many symbols (FC-060 Layers 2/3).

    Three entry shapes, one code path:

    * ``--scenarios <yaml>`` — the operator CLI. Unchanged by Layer 3: it does
      not persist unless ``--persist`` is passed, so the byte-identical-report
      contract holds.
    * ``--spec-env <VAR>`` — the ``backtest-sweep`` Cloud Run Job. The spec and
      the run id arrive as per-execution env overrides, and persistence is
      implied: an execution whose results nobody can read is an execution nobody
      should have launched.
    * ``spec_override=<dict>`` — an IN-PROCESS caller holding a spec, which is
      the weekly battery (FC-096 Phase B B4). It behaves exactly like Job mode
      (persistence implied, ``force`` honoured, the same validators) and differs
      in one respect that matters: it takes its ``run_id`` and its clock from
      the CALLER, never from ``SWEEP_RUN_ID`` / ``SWEEP_SUBMITTED_AT``. One
      execution submits many sweeps, and every one of them inheriting the
      execution's run id would collapse thirty runs into one.

    ``submitted_via`` / ``pin_id`` are provenance the in-process caller stamps
    on every row of the run (``'battery'``, and the pin being re-measured).
    They are parameters rather than env reads for the same reason: they vary
    per sweep within one process.

    ``writer_override`` lets an in-process caller supply the store. The battery
    does, for two reasons: one writer per EXECUTION instead of one per item
    (thirty `ScenarioRunWriter` constructions is thirty BigQuery clients and
    thirty schema reconciles for one execution), and — the load-bearing half —
    it is how the battery knows whether THIS run got a status row. A failure
    before the first write leaves no record of the attempt, and the battery has
    to write one itself; asking BigQuery afterwards would make that decision
    depend on how fast a streamed row becomes visible to a query.

    Returns an exit code.
    """
    import uuid
    from datetime import date, datetime, timedelta, timezone

    from src.backtesting.data.chain_store import ChainStore
    from src.backtesting.reporting.artifact_store import ArtifactWriter
    from src.backtesting.reporting.bq_writer import config_hash
    from src.backtesting.scenarios import run_sweep
    from src.backtesting.scenarios import persist as sweep_store
    from src.backtesting.scenarios.engine_identity import engine_identity
    from src.backtesting.scenarios.identity import sweep_key as compute_sweep_key
    from src.backtesting.scenarios.report import render_json, render_markdown
    from src.backtesting.screen import DEFAULT_LOOKBACK_DAYS, ENGINE_VERSION

    spec_env = getattr(args, 'spec_env', None)
    spec = None
    if spec_env and args.scenarios:
        raise SystemExit(
            "sweep: pass either --scenarios <yaml> or --spec-env <VAR>, not "
            "both — two sources of truth for one run is one too many."
        )
    if spec_override is not None and (spec_env or args.scenarios):
        raise SystemExit(
            "sweep: an in-process spec cannot be combined with --scenarios or "
            "--spec-env — two sources of truth for one run is one too many."
        )

    if spec_override is not None:
        # The battery's shape. A COPY, because this function mutates nothing of
        # the caller's but the caller keeps the pin's spec for its own logging
        # and a shared dict is how one of them ends up describing the other.
        spec = dict(spec_override)
    elif spec_env:
        spec = load_spec_from_env(spec_env)

    # From here the two spec-carrying shapes are one path: the Job's spec and
    # the battery's go through the SAME validators, which is what makes a pin
    # that would be refused by the Job refused at battery time instead of three
    # minutes into a replay.
    job_mode = spec is not None
    if job_mode:
        scenarios = scenarios_from_spec(spec)
        end = _spec_date(spec, 'end') or date.today()
        start = (_spec_date(spec, 'start')
                 or end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
        holdout_start = _spec_date(spec, 'holdout_start')
        symbols = [str(x).strip().upper() for x in (spec.get('symbols') or [])
                   if str(x).strip()]
        # Explicit None check, never `or`: a spec that asked for
        # `starting_cash: 0` would otherwise silently become the CLI
        # default and the run would report a capital base nobody chose.
        # (0 is refused downstream, but it must be refused as 0.)
        raw_cash = spec.get('starting_cash')
        starting_cash = float(args.starting_cash if raw_cash is None
                              else raw_cash)
        run_sensitivity = bool(spec.get('run_sensitivity', False))
    else:
        if not args.scenarios:
            raise SystemExit("sweep requires --scenarios <yaml> or --spec-env <VAR>")
        scenarios = load_scenarios(args.scenarios)
        end = datetime.strptime(args.end, '%Y-%m-%d').date() if args.end else date.today()
        start = (datetime.strptime(args.start, '%Y-%m-%d').date() if args.start
                 else end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
        holdout_start = (datetime.strptime(args.holdout_start, '%Y-%m-%d').date()
                         if args.holdout_start else None)
        if args.symbols:
            symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
        elif args.symbol:
            symbols = [args.symbol.upper()]
        else:
            symbols = list(config.stock_symbols)
        # The SAME rule the artifact endpoint applies at serve time
        # (identity.validate_symbol / services/artifacts.SYMBOL_RE). Hand-typed
        # `--symbols` is the one path by which a symbol containing `__`, a
        # slash or whitespace could reach the artifact writer and land an object
        # whose name nothing can parse or request back. Checked here, before
        # anything is materialised, so it is an immediate refusal rather than a
        # sweep that completes and produces unreadable evidence.
        from src.backtesting.scenarios.identity import validate_symbol

        for symbol in symbols:
            try:
                validate_symbol(symbol, '--symbols')
            except ValueError as exc:
                raise SystemExit(str(exc))
        starting_cash = args.starting_cash
        run_sensitivity = not args.no_sensitivity

    if end <= start:
        raise SystemExit(f"--end ({end}) must be after --start ({start})")
    if not symbols:
        raise SystemExit("sweep has no symbols: pass --symbols or configure stocks.symbols")

    # Validate every override BEFORE anything is written or launched.
    # `run_sweep` validates too, but by then a `running` row exists and the
    # operator has a failed sweep in the store for what is a typo. This costs
    # microseconds and turns that into an immediate, explained refusal.
    #
    # **This is also the battery's revalidation** (FC-096 Phase B B4). EVERY
    # refusal this function can raise — here, and every `SystemExit` above —
    # happens before the first write, and `run_battery_cmd` depends on that: it
    # catches the refusal and writes the pin's own `failed` row, which would be
    # a duplicate (or worse, would leave an orphaned `running` row behind) if a
    # refusal could ever land after the store section began.
    from src.backtesting.scenarios.overrides import validate_overrides

    for scenario in scenarios:
        validate_overrides(scenario.overrides)

    # `base` is implicit UNLESS the file already declares it, so the banner must
    # not blindly add one — a file with an explicit `base` would be announced as
    # running one more arm than it does, and the count is the first thing an
    # operator sanity-checks against their YAML.
    from src.backtesting.scenarios import BASE_SCENARIO_NAME

    declares_base = any(s.name == BASE_SCENARIO_NAME for s in scenarios)
    total_arms = len(scenarios) + (0 if declares_base else 1)
    extra = total_arms - 1
    print(f"\nSweeping {total_arms} scenarios (base + {extra}) "
          f"over {len(symbols)} symbols, {start} -> {end}"
          f"{f' (holdout from {holdout_start})' if holdout_start else ''}...\n")
    if not holdout_start:
        # Said before the run as well as in the report: an operator who walks
        # away from a two-minute sweep should already know the answer is
        # in-sample.
        print("  NOTE: no --holdout-start, so this ranking will be IN-SAMPLE "
              "ONLY and unvalidated.\n")

    # ---------------------------------------------------------------- store --
    # ONE canonical spec payload whichever entry shape was used, so a sweep run
    # from a YAML file and the same sweep submitted from the dashboard produce
    # the SAME `sweep_key` and dedup against each other. Building it from the
    # RESOLVED values rather than from the raw input is what makes that true:
    # defaults are already filled in on both paths.
    spec_payload = {
        'symbols': symbols,
        'start': start.isoformat(),
        'end': end.isoformat(),
        'holdout_start': holdout_start.isoformat() if holdout_start else None,
        'starting_cash': starting_cash,
        'run_sensitivity': run_sensitivity,
        'scenarios': [
            {'name': s.name, 'overrides': dict(s.overrides),
             'fill_haircut': s.fill_haircut}
            for s in scenarios
        ],
    }

    persist = job_mode or bool(getattr(args, 'persist', False))
    # `force` is an instruction about THIS submission, not a property of the
    # question being asked, so it is excluded from `sweep_key`
    # (identity.NON_IDENTITY_FIELDS). Including it would give a forced re-run a
    # different key from the run it deliberately reproduces.
    force = bool((spec or {}).get('force', False)) if job_mode else False
    if spec_override is not None:
        # The IN-PROCESS caller owns the identity of each submission, and the
        # environment is deliberately not consulted. `SWEEP_RUN_ID` and
        # `SWEEP_SUBMITTED_AT` are per-EXECUTION overrides: one battery
        # execution submits the standing set plus every pin, and honouring them
        # here would write all of those runs under one run_id and one partition
        # — thirty runs collapsed into an unreadable timeline.
        run_id = run_id or uuid.uuid4().hex[:16]
        submitted_at = datetime.now(timezone.utc).isoformat()
    else:
        run_id = run_id or os.environ.get('SWEEP_RUN_ID') or uuid.uuid4().hex[:16]
        submitted_at = (os.environ.get('SWEEP_SUBMITTED_AT')
                        or datetime.now(timezone.utc).isoformat())
    git_commit = os.environ.get('GIT_COMMIT') or None
    # FC-096 Phase B: the dedup key is the CONTENT of `src/**`, not the commit.
    # A merge that cannot change a replay (docs, dashboard, build config) leaves
    # every stored result valid; a one-byte change to `put_seller.py` invalidates
    # them all. `git_commit` is still stamped on every row as provenance.
    identity = engine_identity()
    key = compute_sweep_key(spec_payload, engine_version=ENGINE_VERSION,
                            engine_identity=identity)
    snapshot = sweep_store.base_config_snapshot(config)
    effective_hash = sweep_store.base_config_hash(snapshot)
    provenance = dict(
        run_id=run_id,
        submitted_at=submitted_at,
        sweep_key=key,
        submitted_via=(submitted_via
                       or os.environ.get('SWEEP_SUBMITTED_VIA')
                       or ('dashboard' if spec_env else 'cli')),
        engine_version=ENGINE_VERSION,
        git_commit=git_commit,
        engine_identity=identity,
        # Cloud Run stamps this on every Job task. Stored for operator debugging
        # only: D3 makes status BigQuery-based precisely because
        # `run.executions.get` is unproven for the dashboard's service account.
        execution_name=os.environ.get('CLOUD_RUN_EXECUTION'),
        spec=spec_payload,
        base_config=snapshot,
        # The hash of the EFFECTIVE snapshot, not `config_hash`. `config_hash`
        # covers nine strategy keys and cannot see an operator flipping
        # EARNINGS_ENABLED or ROLLER_ENABLED on the Job between two otherwise
        # identical submissions — and the dedup reads this column, so that blind
        # spot would have served one experiment's numbers as another's. The
        # engine hash is still stored, separately, for the `backtest_runs`
        # linkage.
        base_config_hash=sweep_store.base_config_hash(snapshot),
        engine_config_hash=config_hash(config),
        # FC-096 Phase B B4. NULL on every run that is not a pin's; the battery
        # passes the pin it is re-measuring, and it is what makes that pin's
        # weekly history — and therefore the 3-week nag — queryable.
        pin_id=pin_id,
    )

    # FC-096 Phase B B2. Detail artifacts ride the SAME gate as BigQuery
    # persistence, which gives the two contracts the plan asks for without a
    # second flag: the Job always persists (`persist` is implied by `--spec-env`)
    # so it always writes artifacts, and a CLI sweep without `--persist` writes
    # NOTHING ANYWHERE — no rows, no objects, and no GCS client is even
    # constructed, because `ArtifactWriter` is never built.
    artifact_writer = ArtifactWriter(run_id) if persist else None
    if artifact_writer is not None and not artifact_writer.enabled:
        # The explicit off switch (`SIM_ARTIFACT_BUCKET=""`). Said once, because
        # a run whose evidence is silently not being stored is exactly the
        # "silence reads as all-clear" failure this project keeps relearning.
        logger.warning(
            "Detail artifacts are switched off for this run (no artifact "
            "bucket configured); results are unaffected",
            event_category="backtest", event_type="sim_artifacts_disabled",
            run_id=run_id)

    writer = None
    if persist:
        # Profile-derived, never hardcoded (the FC-075 DD-4 lesson): a
        # covered-call profile running a sweep writes to its own dataset rather
        # than into the wheel's store. An in-process caller may supply its own
        # (the battery does — see `writer_override` in the docstring).
        writer = (writer_override if writer_override is not None
                  else sweep_store.ScenarioRunWriter(
                      dataset_id=config.bigquery_dataset))
        if not writer.enabled and job_mode:
            # FAIL BEFORE REPLAYING, in Job mode only. An execution launched from
            # the dashboard exists solely to put rows in the store: eight minutes
            # of 1-vCPU compute whose output goes to a log nobody reads is worse
            # than an immediate, loud failure, and the submitter would sit on
            # `submitted` until the lock expired with no explanation anywhere.
            # `--persist` from the CLI still degrades to "report only" — there a
            # human is watching the report come out.
            logger.error(
                "Sweep store unavailable — refusing to replay in Job mode",
                event_category="backtest",
                event_type="sweep_store_unavailable",
                run_id=run_id, dataset=config.bigquery_dataset)
            print("\nFAILED: the scenario store is unavailable (no BigQuery "
                  "client, no GCP project, or the tables could not be "
                  "reconciled). Refusing to replay: a Job execution exists to "
                  "produce rows, and there is nowhere to put them.")
            return 2

    started_at = datetime.now(timezone.utc).isoformat()
    result = None
    failure = None
    chain_store = None
    rows_persisted = None
    deduplicated_to = None
    try:
        # THE SIGNAL HANDLER GOES ON FIRST, before anything is written (round-2
        # fix 2). The `running` insert, the dedup query (up to 60 s) and
        # `ChainStore.from_env()`'s bucket probe all run before a single day is
        # replayed, and a cancel landing in that window used to kill the process
        # outright — leaving the orphaned `running` row this whole mechanism
        # exists to prevent, in the minutes when a cancel is most likely.
        #
        # Everything from here is inside the try, so any raise reaches the
        # `finally` and produces a terminal row.
        with terminate_on_sigterm(logger):
            if writer is not None:
                # `running` FIRST, before the dedup query (D3). A `submitted` row
                # with no `running` row after 10 minutes is the dashboard's
                # "stuck — check the execution" signal, and a dedup lookup that
                # hung would otherwise sit inside exactly that window and read as
                # a dead container.
                writer.write_status(sweep_store.status_row(
                    status=sweep_store.STATUS_RUNNING, started_at=started_at,
                    **provenance))

                # THE dedup. The API deliberately does not do this (it cannot
                # compute the effective config); it launches, and this is where
                # the decision is actually made, against the config this process
                # is holding.
                prior = (None if force else
                         writer.find_done_sweep(
                             key, base_config_hash=effective_hash,
                             engine_identity=identity))
                if prior is not None and prior.get('run_id') != run_id:
                    deduplicated_to = prior['run_id']

            if deduplicated_to is None:
                # `ChainStore.from_env()` builds a GCS client and probes the
                # bucket, so it can raise; inside the try, that becomes a
                # `failed` row rather than a silent orphan.
                chain_store = ChainStore.from_env()
                result = run_sweep(
                    config, scenarios, symbols, start, end,
                    holdout_start=holdout_start,
                    starting_cash=starting_cash,
                    run_sensitivity=run_sensitivity,
                    chain_store=chain_store,
                    artifact_sink=(artifact_writer.write
                                   if artifact_writer is not None
                                   and artifact_writer.enabled else None),
                    # FC-096 Phase E PR-1. The SAME enabled-gate as the cell
                    # artifacts: `artifact_writer` is only constructed under
                    # `--persist`, so a CLI run without it writes nothing
                    # anywhere, sidecars included.
                    bars_sink=(artifact_writer.write_bars
                               if artifact_writer is not None
                               and artifact_writer.enabled else None),
                    run_id=run_id,
                    engine_identity=identity,
                    # Provenance only — `engine_identity` is the identity. It
                    # was already in scope here and simply never reached the
                    # artifact, which is why every stored object carried
                    # `provenance.git_commit: null`.
                    git_commit=git_commit,
                )
    except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if writer is not None:
            rows_persisted = _finalise_sweep_status(
                writer=writer, logger=logger, result=result, failure=failure,
                chain_store=chain_store, started_at=started_at,
                run_id=run_id, submitted_at=submitted_at,
                engine_version=ENGINE_VERSION, git_commit=git_commit,
                engine_identity=identity,
                provenance=provenance, deduplicated_to=deduplicated_to,
                artifact_writer=artifact_writer,
            )

    if deduplicated_to is not None:
        print(f"\nDEDUPLICATED: sweep_key {key} already completed as run "
              f"{deduplicated_to}. Nothing was replayed; read that run's rows "
              f"instead.")
        logger.info("Sweep deduplicated against a completed run",
                    event_category="backtest", event_type="sweep_deduplicated",
                    run_id=run_id, sweep_key=key,
                    deduplicated_to=deduplicated_to)
        return 0

    persistence = SweepPersistence(
        persisted=bool(writer is not None and rows_persisted is not None),
        run_id=run_id, sweep_key=key, dataset=config.bigquery_dataset)

    markdown = render_markdown(result, persistence)
    print(markdown)
    if args.out:
        with open(args.out, 'w') as fh:
            fh.write(markdown)
        print(f"\nMarkdown report -> {args.out}")
    if args.json_out:
        with open(args.json_out, 'w') as fh:
            fh.write(render_json(result, persistence))
        print(f"JSON report -> {args.json_out}")
    if persist:
        if persistence.persisted:
            print(f"\nStored as run_id {run_id} (sweep_key {key}) in "
                  f"{config.bigquery_dataset}.{sweep_store.SWEEPS_TABLE} / "
                  f"{sweep_store.RUNS_TABLE}.")
        else:
            # A `done` row is never written in this state (see
            # `_finalise_sweep_status`), so the store and this message agree.
            print(f"\nWARNING: results were NOT persisted for run_id {run_id}. "
                  f"This report is the only record of the run.")
            return 1

    # Exit non-zero when any cell never produced a verdict. A sweep that
    # half-ran reads as a complete one, and a missing cell is exactly where a
    # reader's eye supplies "probably like the others".
    if result.errors:
        failed = sorted({f"{r.scenario}/{r.symbol}" for r in result.errors})
        print(f"\nWARNING: {len(result.errors)} cell(s) produced no verdict: "
              f"{', '.join(failed[:12])}{' ...' if len(failed) > 12 else ''}")
        return 1
    return 0


def _finalise_sweep_status(*, writer, logger, result, failure, chain_store,
                           started_at, run_id, submitted_at, engine_version,
                           git_commit, provenance, deduplicated_to=None,
                           engine_identity=None, artifact_writer=None):
    """Write the cells and the terminal status row. Returns rows persisted, or None.

    Extracted from the `finally` because **every step in here can itself raise**,
    and a raise inside a `finally` replaces the original exception with a
    confusing one AND skips whatever the `finally` had not reached yet. The one
    thing that must survive is the terminal status row: without it the sweep is
    `running` for ever, blocks the next submission until the lock expires, and
    says nothing about why.

    So the lake summary and the cell write are each guarded independently, and
    the status write is attempted last with its own guard. Worst case the row
    says `failed` with a truthful reason; the unacceptable case is no row at all.

    **`done` requires the cells to have landed** (review round 1). Previously the
    status was chosen from `failure is None` alone, so a `write_runs` that
    returned False still produced `done` — a run the dedup would then serve as a
    cached answer whose grid is empty. `done` now means "the process finished AND
    its rows are in the table"; anything else is `failed`.
    """
    from datetime import datetime, timezone

    from src.backtesting.scenarios import persist as sweep_store
    from src.backtesting.screen import accumulate_lake_summary

    with ignore_sigterm_while_finalising(logger):
        return _finalise_sweep_status_inner(
            writer=writer, logger=logger, result=result, failure=failure,
            chain_store=chain_store, started_at=started_at, run_id=run_id,
            submitted_at=submitted_at, engine_version=engine_version,
            git_commit=git_commit, engine_identity=engine_identity,
            provenance=provenance,
            deduplicated_to=deduplicated_to,
            artifact_writer=artifact_writer,
            sweep_store=sweep_store,
            accumulate_lake_summary=accumulate_lake_summary,
            datetime=datetime, timezone=timezone)


def _finalise_sweep_status_inner(*, writer, logger, result, failure, chain_store,
                                 started_at, run_id, submitted_at,
                                 engine_version, git_commit, provenance,
                                 deduplicated_to, sweep_store,
                                 accumulate_lake_summary, datetime, timezone,
                                 engine_identity=None, artifact_writer=None):
    """The body of `_finalise_sweep_status`, run with SIGTERM ignored."""
    artifacts_complete = _artifacts_complete(artifact_writer, result, logger,
                                             run_id=run_id)
    lake_summary = None
    try:
        if chain_store is not None:
            summary = chain_store.summary()
            lake_totals: dict = {}
            accumulate_lake_summary(lake_totals, summary)
            lake_summary = dict(summary)
            lake_summary.update(lake_totals)
    except Exception:  # noqa: BLE001 - cosmetic beside the status row
        logger.warning("Could not summarise chain-lake usage for this sweep",
                       event_category="backtest",
                       event_type="sweep_lake_summary_failed", exc_info=True)

    # Cells BEFORE the terminal status row: a reader that sees `done` must be
    # able to trust the rows are already there. The reverse order leaves a window
    # in which a finished sweep has no results.
    rows_ok = False
    rows_written = 0
    if result is not None:
        try:
            rows = sweep_store.rows_from_sweep(
                result, run_id=run_id, submitted_at=submitted_at,
                engine_version=engine_version, git_commit=git_commit,
                engine_identity=engine_identity)
            rows_ok = writer.write_runs(rows)
            rows_written = len(rows) if rows_ok else 0
        except Exception as exc:  # noqa: BLE001
            logger.error("Sweep cell rows could not be written",
                         event_category="backtest",
                         event_type="sweep_rows_write_failed",
                         run_id=run_id, error=str(exc)[:300])
            failure = failure or f"cell rows not persisted: {type(exc).__name__}: {exc}"

    if result is not None and not rows_ok and failure is None:
        failure = ("cell rows not persisted: the sweep completed but its "
                   "scenario_runs insert did not land, so there are no results "
                   "to read")

    if deduplicated_to is not None and failure is None:
        # The Job found a completed run under this key and did not replay. It is
        # still a terminal row, and it still goes through this one path so a
        # cancel arriving during the dedup lookup cannot leave the run orphaned.
        status = sweep_store.STATUS_DEDUPLICATED
    elif failure is None and result is not None and rows_ok:
        status = sweep_store.STATUS_DONE
    else:
        status = sweep_store.STATUS_FAILED
    try:
        writer.write_status(sweep_store.status_row(
            status=status, started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            result=result, error=failure, lake_summary=lake_summary,
            rows_persisted=(rows_written if rows_ok else None),
            artifacts_complete=artifacts_complete,
            deduplicated_to=(deduplicated_to
                             if status == sweep_store.STATUS_DEDUPLICATED
                             else None),
            **provenance))
    except Exception:  # noqa: BLE001 - nothing left to fall back to
        logger.error("Terminal sweep status row could not be written — this run "
                     "will read as `running` until the lock expires",
                     event_category="backtest",
                     event_type="sweep_status_write_failed",
                     run_id=run_id, status=status, exc_info=True)
        return None
    return rows_written if status == sweep_store.STATUS_DONE else None


def _artifacts_complete(artifact_writer, result, logger, *, run_id):
    """Whether every NON-ERRORED cell of this run also stored its artifact.

    Three values, and the difference between them is the whole point:

    * ``True``  — every non-errored cell has an artifact; the console can open
      any cell of this run.
    * ``False`` — some do not; an empty ledger in the console is a STORAGE
      failure, not a replay that did nothing.
    * ``None``  — the question does not apply. No writer, artifacts switched
      off, a dedup hit with no result, **a run with no non-errored cell at all**
      (nothing was ever supposed to be written, so "complete" is vacuous), or a
      run that CRASHED before producing a result while having written nothing.

    ``None`` is not a soft ``False``. "This run stored no evidence and was never
    meant to" and "this run's evidence is incomplete" send an operator to
    different places, and the vacuous case is the one that used to answer
    ``0 == 0`` -> ``True``: a sweep whose every arm errored would have claimed a
    complete artifact set it does not have a single object of.

    **The crashed path is the subtle one.** When the replay raises there is no
    ``result``, so the arithmetic has no denominator — but the writer may
    already have stored objects for the cells that finished before the crash.
    Stamping ``None`` there would say "this run wrote nothing" while orphaned
    objects sit in the bucket, so a crashed run that wrote at least one artifact
    is ``False``: incomplete, which is exactly what it is.

    Errored cells are excluded from BOTH sides of the comparison: an errored
    cell has no replay to serialise, so ``_replay_one`` never calls the sink for
    it. Counting it would make every sweep with one bad arm also report missing
    artifacts, folding two unrelated problems into one flag.
    """
    if artifact_writer is None or not getattr(artifact_writer, "enabled", False):
        return None
    written = int(getattr(artifact_writer, "written", 0))
    if result is None:
        # Crashed, or deduplicated away without replaying. Objects already in
        # the bucket are orphans of a run that has no cell rows to pair them
        # with — incomplete, never "wrote nothing".
        return False if written > 0 else None
    cells = getattr(result, "rows", []) or []
    expected = sum(1 for cell in cells if not getattr(cell, "error", None))
    if expected == 0:
        # Nothing was ever supposed to be written. `0 == 0` is True and would be
        # a lie about a run with no evidence in it at all.
        return None
    complete = written == expected
    if not complete:
        logger.warning(
            "Detail artifacts are INCOMPLETE for this run — some cells have "
            "results but no evidence",
            event_category="backtest",
            event_type="sim_artifacts_incomplete",
            run_id=run_id, expected=expected, written=written,
            failed=int(getattr(artifact_writer, "failed", 0)),
            last_error=getattr(artifact_writer, "last_error", None))
    return complete


def _sensitivity_section(s: dict) -> str:
    """Mid-vs-bid fill comparison, appended to the markdown report."""
    lines = [
        "",
        "## Fill sensitivity (mid vs bid)",
        "",
        "| fill assumption | total return | verdict |",
        "|---|---:|---|",
        f"| mid − {s['mid_haircut']:.2f}×half-spread (headline) | "
        f"{s['mid_return']:+.2%} | {s['mid_verdict']} |",
        f"| at the bid (worst case) | {s['bid_return']:+.2%} | {s['bid_verdict']} |",
        "",
    ]
    if s["verdict_flips"]:
        lines += [
            f"> **The verdict flips on the fill assumption** "
            f"({s['mid_verdict']} → {s['bid_verdict']}). Treat this symbol as "
            f"unproven: the result depends on getting better than bid fills.",
            "",
        ]
    else:
        lines += [
            f"Verdict holds at both ends ({s['bid_return'] - s['mid_return']:+.2%} "
            f"at the bid), so it does not rest on the fill assumption.",
            "",
        ]
    return "\n".join(lines)


if __name__ == '__main__':
    main()