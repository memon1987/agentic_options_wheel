"""Verbatim copies of the sweep report's operator-facing prose (FC-060 D11).

**This file is a COPY, and the copy is deliberate.** The dashboard renders the
same bias footer, in-sample banner and holdout note the CLI report renders, so a
reader of `/sims` and a reader of `sweep.md` are warned in the same words. The
originals live in `src/backtesting/scenarios/report.py`, which the dashboard
image cannot import: `report.py` imports `runner.py`, and `runner.py` imports the
simulator, the Alpaca provider and pandas — none of which this image has, and
none of which it should acquire to print a paragraph.

D11 sanctions exactly this ("otherwise re-implement with a shared test fixture
asserting equality"). The guard is
`tests/test_dashboard_sweeps.py::TestTheReportProseIsNotAFork`, which compares
every constant below against its original, byte for byte. That test runs as step
1 of every Cloud Build, so prose drift fails the build rather than shipping a
dashboard that quietly disagrees with the CLI about what a number means.

Edit `report.py` FIRST, then copy the new value across here in the same
commit. The test names the exact constant that diverged.

**One constant here is deliberately NOT a copy.** `FORECAST_CAVEAT` (FC-096
Phase E PR-1) has no original in `report.py`, because the forecast is a
DASHBOARD component: `report.py` and the CLI markdown do not render one, and
adding the prose there to satisfy a symmetry nothing reads would be a second
place to keep it true. The fork test knows the exception by name
(`DASHBOARD_ONLY_PROSE`) and asserts the set is exactly that one constant, so a
future addition here cannot quietly join it. If the CLI report ever prints the
forecast, this constant moves to `report.py` and the exception set empties.
"""

BASE_SCENARIO_NAME = 'base'

# The activity floor below which an annualised number rests on idle
# capital. Copied from `src.backtesting.metrics.fitness`.
MIN_DAYS_IN_POSITION = 0.25

CROSS_SCENARIO_CAVEAT = ('**Comparisons between scenarios that differ in call-leg activity are '
 'biased against the call-heavier one until FC-056 is fixed.** The engine '
 'prices identical call contracts at **last measured 0.676** of the live '
 "fill (a ~32% shortfall, ~5x the put leg's ~7% error) — **FC-056; that "
 'figure is stale, pending the FC-068/078 re-baseline**, so treat it as an '
 'order of magnitude rather than a coefficient. An arm that writes more '
 'calls is marked down for doing so. Rank arms that hold call activity '
 'roughly constant; treat a ranking across arms with very different '
 '`calls_sold` as unproven.')

IN_SAMPLE_BANNER = ('> ## IN-SAMPLE ONLY — this ranking has not been validated\n'
 '>\n'
 '> Every arm below was measured on the same window it would be chosen '
 "from, over a **single volatility regime** (Alpaca's option history "
 'begins 2024-02-01). With 10 arms x 6 symbols there are 60 numbers here, '
 'and the best-looking arm is more often the luckiest one than the best '
 'one.\n'
 '>\n'
 '> **Re-run with `--holdout-start` and act on the sign-agreement column, '
 'not on this table.** A ranking that does not survive out of sample has '
 'been refuted, not merely unconfirmed.')

HOLDOUT_SEMANTICS = ('**How the split is run.** The two windows are independent replays, not '
 'one run cut in half: each starts flat with the full `--starting-cash`, '
 'carries no position across the boundary, and derives its own strike '
 'anchors from its own bars. So a holdout does not inherit the fit '
 "window's assigned shares — the wheel begins its cycle again — and the "
 'fit window ends the day BEFORE `--holdout-start`, so the two never '
 'overlap. **A short holdout inflates `insuf`**: a cycle needs a put to be '
 'written, held and resolved, so a window of a few weeks can end with '
 'nothing completed on symbols that traded perfectly well. Read the '
 '`insuf` column before reading the medians.')

TALLY_CAVEAT = ('**This report carries no `binding_constraint` column.** It shipped without '
 'one because only the FIRST replay in a process got a working '
 '`RejectionTally`: `setup_logging` sets `cache_logger_on_first_use=True`, a '
 'structlog lazy proxy caches its whole processor chain on first use, and the '
 '`structlog.configure()` the tally used to install itself does not '
 'invalidate that cache — so every strategy logger kept delivering to replay '
 "#1's tally for the life of the process, and replays 2..N reported an empty "
 '`blocked_days_by_reason`, i.e. "the strategy was never blocked". **That '
 'defect is fixed** (FC-092, shipped with FC-096 Phase B): the tally binds '
 'through a process-stable dispatch, so every replay in a sweep now gets a '
 'complete, deterministically-ordered tally. Adding the column is a schema '
 'change with its own review and is not part of that fix — so this report '
 'still does not carry one. Rows the monthly screen wrote to `backtest_runs` '
 'before the fix keep their NULL `binding_constraint`; read it as "not '
 'measured", never as "never blocked". Every other number here comes from the '
 'broker ledger and the equity curve and was never affected.')

SWEEP_BIASES = [('Every arm is measured by the same biased engine, so DIFFERENCES survive '
  'better than LEVELS',
  'Premium is understated on both legs — puts by ~7% against 204 real '
  'decisions, calls far worse (see the call caveat above). Spreads come '
  'from a parametric model measured ~2.5x wider than the real book '
  '(FC-051). Greeks are Black-Scholes inversions, not published values. '
  'None of that cancels perfectly between arms, but it cancels far better '
  'than it does in absolute terms: read the ORDER of these rows, and do '
  'not quote any single cell as a forecast of what the strategy would have '
  'earned.'),
 ('One decision per day, and the replay gets the price it saw',
  'Production scans and executes ~15 minutes apart; the replay does both '
  'on one snapshot, and holds every contract to expiry or assignment. '
  'Early profit-taking closed 52% of real call positions before expiry and '
  'is unmodelled. Arms that would have changed monitor-cycle churn cannot '
  'be distinguished here at all — which is why the monitor knobs are '
  'refused as overrides rather than swept.'),
 ('Dividends come from a static table; ex-dividend early assignment has '
  'never fired on real data',
  'Both legs collect from the same committed table, so the two stay on one '
  "footing, but a window running past the table's coverage credits nothing "
  'after that point on either. The early-assignment path needs a dividend '
  "payer holding an ITM short call, and this universe's payers are exactly "
  'the symbols that cannot clear the premium floor to open a position — so '
  'it is validated by unit tests only.'),
 ('One vol regime',
  "Alpaca's option history starts 2024-02-01, so every window here sits "
  'inside a single regime. A shifted start date can flip a marginal '
  'verdict, and it can reorder two arms that are close. This is the reason '
  'the in-sample banner exists.'),
 ('Splits are refused, not modelled; taxes are not modelled',
  'A window spanning a split is refused outright '
  '(`UnadjustedCorporateAction`) and shows as an errored cell. Wheel '
  'income is short-term gains and buy-and-hold defers to long-term; '
  'published estimates put that drag at ~1-2%/yr, which nothing here '
  'deducts.')]

# FC-096 Phase A PR-2. The DTE-reach caveat, and the threshold that decides
# whether a run has earned it. **The parity test pins the CONSTANT, not its
# emission** — the CLI reads `SweepResult.effective_max_dte` and the dashboard
# derives the same reach from the persisted spec's DTE overrides, so the two
# arrive at the condition by different routes and must not disagree about the
# WORDS.
DTE_REACH_BIAS_THRESHOLD = 7

DTE_REACH_BIAS = ('Arms reaching past 7 DTE are measured on THINNER data, and the thinness '
 'biases selection rather than merely adding noise',
 'These are not extrapolated prices: a 14- or 21-DTE quote here is a real '
 "print of a real contract, with implied vol solved from that contract's own "
 'daily trade bar. The problem is WHICH contracts survive. Longer-dated '
 'contracts trade thinly, and the chain builder drops any contract with no '
 'trade that day — so a hole in the ladder is indistinguishable from a strike '
 'that never existed, and the strategy picks from whatever happened to trade '
 'rather than from the real ladder. That biases SELECTION; it is not a wider '
 'error bar around the same choice. Two further limits ride along: the spread '
 'model (FC-051) was measured on short-dated OTM puts only and is unvalidated '
 'at these tenors, and the premium shortfalls quoted above were measured at 7 '
 "DTE. Read a long-DTE arm's RANK against other long-DTE arms; a "
 'long-versus-short comparison carries this on top of every bias listed here.')

# FC-096 Phase E PR-1. DASHBOARD-ONLY prose — see the module docstring. The
# forecast panel refuses to render on a blank caveat, so this constant is
# load-bearing rather than decorative: it is the sentence that keeps two
# extrapolated run-rates from being read as a confidence interval.
FORECAST_CAVEAT = (
    'Two run-rates, extrapolated. The bounds of each are the fit and holdout '
    "windows' per-calendar-day rates over the row's requested window, scaled "
    'to the horizon; they are NOT a confidence interval — one regime, two '
    'windows, one engine whose premium is understated on both legs (see the '
    'biases below). PREMIUM is cash-basis net option P&L: buybacks and fees '
    'are inside, and options still open at window end are counted at their '
    'sale price, unmarked — a wheel holding assigned shares writes MORE calls '
    'while it loses, so this rate can rise while the strategy fails. TOTAL is '
    'FULLY MARKED: it is the account equity at the last decision day, so the '
    'stock leg is at that close AND every option still open is a liability at '
    'its chain mark (intrinsic on a day it did not trade). The two bases '
    'therefore differ by the OPEN-OPTION marks as well as by the stock leg — a '
    'short call that has run against you is already subtracted from TOTAL and '
    'is not in PREMIUM at all. Each symbol is an '
    'independent replay on its own capital; the portfolio line is a sum over '
    'the symbols measured in both windows, named. An in-sample run has no '
    'forecast.')

# What an `in_sample_only` run gets instead of a forecast. A run measured on the
# window it would be chosen from has no out-of-sample rate to bound anything
# with, so there is no second point and the "range" would be one number twice.
FORECAST_REFUSAL_IN_SAMPLE = (
    'This run is IN-SAMPLE ONLY, so there is no forecast. The range needs two '
    'windows — a fit rate and a holdout rate — and an in-sample run has only '
    'the window it was chosen from. Re-run with a holdout.')
