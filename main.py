"""Main entry point for Options Wheel Strategy."""

import argparse
import os
import sys
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
                                'sweep', 'backfill'],
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
            rc = run_backfill_cmd(args, config, logger)
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


def run_sweep_cmd(args, config: Config, logger) -> int:
    """Replay many scenarios over many symbols (FC-060 Layers 2/3).

    Two entry shapes, one code path:

    * ``--scenarios <yaml>`` — the operator CLI. Unchanged by Layer 3: it does
      not persist unless ``--persist`` is passed, so the byte-identical-report
      contract holds.
    * ``--spec-env <VAR>`` — the ``backtest-sweep`` Cloud Run Job. The spec and
      the run id arrive as per-execution env overrides, and persistence is
      implied: an execution whose results nobody can read is an execution nobody
      should have launched.

    Returns an exit code.
    """
    import uuid
    from datetime import date, datetime, timedelta, timezone

    from src.backtesting.data.chain_store import ChainStore
    from src.backtesting.reporting.bq_writer import config_hash
    from src.backtesting.scenarios import run_sweep
    from src.backtesting.scenarios import persist as sweep_store
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

    if spec_env:
        spec = load_spec_from_env(spec_env)
        scenarios = scenarios_from_spec(spec)
        end = _spec_date(spec, 'end') or date.today()
        start = (_spec_date(spec, 'start')
                 or end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
        holdout_start = _spec_date(spec, 'holdout_start')
        symbols = [str(x).strip().upper() for x in (spec.get('symbols') or [])
                   if str(x).strip()]
        starting_cash = float(spec.get('starting_cash') or args.starting_cash)
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

    persist = bool(spec_env) or bool(getattr(args, 'persist', False))
    # `force` is an instruction about THIS submission, not a property of the
    # question being asked, so it is excluded from `sweep_key`
    # (identity.NON_IDENTITY_FIELDS). Including it would give a forced re-run a
    # different key from the run it deliberately reproduces.
    force = bool((spec or {}).get('force', False)) if spec_env else False
    run_id = os.environ.get('SWEEP_RUN_ID') or uuid.uuid4().hex[:16]
    submitted_at = (os.environ.get('SWEEP_SUBMITTED_AT')
                    or datetime.now(timezone.utc).isoformat())
    git_commit = os.environ.get('GIT_COMMIT') or None
    key = compute_sweep_key(spec_payload, engine_version=ENGINE_VERSION,
                            git_commit=git_commit)
    snapshot = sweep_store.base_config_snapshot(config)
    effective_hash = sweep_store.base_config_hash(snapshot)
    provenance = dict(
        run_id=run_id,
        submitted_at=submitted_at,
        sweep_key=key,
        submitted_via=(os.environ.get('SWEEP_SUBMITTED_VIA')
                       or ('dashboard' if spec_env else 'cli')),
        engine_version=ENGINE_VERSION,
        git_commit=git_commit,
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
    )

    writer = None
    if persist:
        # Profile-derived, never hardcoded (the FC-075 DD-4 lesson): a
        # covered-call profile running a sweep writes to its own dataset rather
        # than into the wheel's store.
        writer = sweep_store.ScenarioRunWriter(dataset_id=config.bigquery_dataset)
        if not writer.enabled and spec_env:
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
                         writer.find_done_sweep(key,
                                                base_config_hash=effective_hash))
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
                provenance=provenance, deduplicated_to=deduplicated_to,
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
                           git_commit, provenance, deduplicated_to=None):
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
            git_commit=git_commit, provenance=provenance,
            deduplicated_to=deduplicated_to,
            sweep_store=sweep_store,
            accumulate_lake_summary=accumulate_lake_summary,
            datetime=datetime, timezone=timezone)


def _finalise_sweep_status_inner(*, writer, logger, result, failure, chain_store,
                                 started_at, run_id, submitted_at,
                                 engine_version, git_commit, provenance,
                                 deduplicated_to, sweep_store,
                                 accumulate_lake_summary, datetime, timezone):
    """The body of `_finalise_sweep_status`, run with SIGTERM ignored."""
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
                engine_version=engine_version, git_commit=git_commit)
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