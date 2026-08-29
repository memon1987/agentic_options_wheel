"""Main entry point for Options Wheel Strategy."""

import argparse
import sys
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
                                'sweep'],
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
                        help='sweep: comma-separated universe (default: config stocks.symbols)')
    parser.add_argument('--holdout-start',
                        help='sweep: split the window here into fit/holdout, YYYY-MM-DD')

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

    from src.backtesting.scenarios import Scenario

    with open(path) as fh:
        payload = yaml.safe_load(fh) or {}
    raw = payload.get('scenarios')
    if raw is None:
        raise SystemExit(
            f"{path}: expected a top-level 'scenarios:' list "
            f"(got keys: {sorted(payload) or 'an empty file'})"
        )
    if not isinstance(raw, list):
        raise SystemExit(f"{path}: 'scenarios' must be a list, got {type(raw).__name__}")

    scenarios = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SystemExit(f"{path}: scenario #{i + 1} must be a mapping")
        name = entry.get('name')
        if not name:
            raise SystemExit(f"{path}: scenario #{i + 1} has no 'name'")
        overrides = entry.get('overrides') or {}
        if not isinstance(overrides, dict):
            raise SystemExit(
                f"{path}: scenario '{name}' has a non-mapping 'overrides'"
            )
        haircut = entry.get('fill_haircut')
        if haircut is not None:
            haircut = float(haircut)
            if not 0.0 <= haircut <= 1.0:
                raise SystemExit(
                    f"{path}: scenario '{name}' fill_haircut={haircut} is outside "
                    "[0, 1] (0 = mid, 1 = at the bid)"
                )
        unknown = set(entry) - {'name', 'overrides', 'fill_haircut'}
        if unknown:
            raise SystemExit(
                f"{path}: scenario '{name}' has unknown field(s) "
                f"{sorted(unknown)}. A misspelled field would silently do nothing."
            )
        scenarios.append(Scenario(str(name), dict(overrides), haircut))
    return scenarios


def run_sweep_cmd(args, config: Config, logger) -> int:
    """Replay many scenarios over many symbols (FC-060 Layer 2). Returns an exit code."""
    from datetime import date, datetime, timedelta

    from src.backtesting.scenarios import run_sweep
    from src.backtesting.scenarios.report import render_json, render_markdown
    from src.backtesting.screen import DEFAULT_LOOKBACK_DAYS

    if not args.scenarios:
        raise SystemExit("sweep requires --scenarios <yaml>")

    scenarios = load_scenarios(args.scenarios)
    end = datetime.strptime(args.end, '%Y-%m-%d').date() if args.end else date.today()
    start = (datetime.strptime(args.start, '%Y-%m-%d').date() if args.start
             else end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    if end <= start:
        raise SystemExit(f"--end ({end}) must be after --start ({start})")
    holdout_start = (datetime.strptime(args.holdout_start, '%Y-%m-%d').date()
                     if args.holdout_start else None)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    elif args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = list(config.stock_symbols)
    if not symbols:
        raise SystemExit("sweep has no symbols: pass --symbols or configure stocks.symbols")

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

    result = run_sweep(
        config, scenarios, symbols, start, end,
        holdout_start=holdout_start,
        starting_cash=args.starting_cash,
        run_sensitivity=not args.no_sensitivity,
    )

    markdown = render_markdown(result)
    print(markdown)
    if args.out:
        with open(args.out, 'w') as fh:
            fh.write(markdown)
        print(f"\nMarkdown report -> {args.out}")
    if args.json_out:
        with open(args.json_out, 'w') as fh:
            fh.write(render_json(result))
        print(f"JSON report -> {args.json_out}")

    # Exit non-zero when any cell never produced a verdict. A sweep that
    # half-ran reads as a complete one, and a missing cell is exactly where a
    # reader's eye supplies "probably like the others".
    if result.errors:
        failed = sorted({f"{r.scenario}/{r.symbol}" for r in result.errors})
        print(f"\nWARNING: {len(result.errors)} cell(s) produced no verdict: "
              f"{', '.join(failed[:12])}{' ...' if len(failed) > 12 else ''}")
        return 1
    return 0


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