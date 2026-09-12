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

DTE_REACH_BIAS = ('Arms reaching past 7 DTE are measured on THINNER data, and the thinness biases '
 'selection rather than merely adding noise',
 'These are not extrapolated prices: a 14- or 21-DTE quote here is a real print of a '
 "real contract, with implied vol solved from that contract's own daily trade bar. The "
 'problem is WHICH contracts survive. Longer-dated contracts trade thinly, and the '
 'chain builder drops any contract with no trade that day — so a hole in the ladder is '
 'indistinguishable from a strike that never existed, and the strategy picks from '
 'whatever happened to trade rather than from the real ladder. That biases SELECTION; '
 'it is not a wider error bar around the same choice. Two further limits ride along: '
 'the spread model (FC-051) was measured on short-dated OTM puts only and is '
 'unvalidated at these tenors, and the premium shortfalls quoted above were measured '
 "at 7 DTE. Read a long-DTE arm's RANK against other long-DTE arms; a "
 'long-versus-short comparison carries this on top of every bias listed here.\n'
 '\n'
 'WHICH LEG reaches past 7 is not the same question on both strategies, and this '
 "caveat fires on the run's materialisation reach rather than on its entries. On a "
 "WHEEL run with no DTE arm (since FC-112) the reach is 21 because the ROLLER's "
 'replacement search needs it — `call_target_dte` 7 + `rolling.max_extension_days` 14 '
 '— while the scanner still caps every ENTRY at 7. So the thinness above bites the '
 "wheel's roll candidates, not its put or call entries: read it against "
 '`otm_roll_outs`, `roll_net_credit` and `roll_skips.no_credit_candidate`, not against '
 '`puts_sold` — a thin long-dated print is one the roller CAN see and cannot make a '
 'credit out of, so it fails the credit screen and lands in that counter; '
 '`no_suitable_replacement` is the earlier gate, where nothing passed the '
 'horizon/strike/delta filter at all. A wheel arm that overrides a DTE key reaches '
 'past 7 on entries too, and then the caveat applies to both. `WHEEL_ROLL_REACH_NOTE` '
 "carries the rest of the wheel's roll-reach story: what the 21 does NOT cover, and "
 'the version boundary.')

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

# --------------------------------------------------------------------------- #
# FC-096 Phase C — the covered-call footer, copied from `report.py`.
#
# Emitted conditionally on BOTH sides and by different routes, exactly as
# DTE_REACH_BIAS is: the CLI reads `SweepResult.strategy`, and the dashboard
# reads the persisted `spec_json.strategy` (absent -> wheel). The parity test
# pins the WORDS; each side owns its own condition.
#
# The two WHEEL_* titles are the KEYS of the M1 substitution — the two
# SWEEP_BIASES lines that are false on a covered-call run are swapped by
# title, never by index, on both sides.
# --------------------------------------------------------------------------- #
SYNTHETIC_LOT_BIAS = ('The stock leg is ASSUMED, not bought — every number here is relative to a '
 'lot the engine created',
 'A covered-call cell seeds 100 shares at the window-start close and, when '
 "they are called away, seeds a fresh lot at the NEXT session's close "
 '(signed decision, 2026-09-08). So the stock leg is a CHAIN of lots, each '
 'with its own basis, and the window is measured end to end on every symbol '
 'rather than truncating at the first call-away — which would have biased '
 'against exactly the names that ran up fastest. Consequences to read with: '
 'the capital base is the lot, not the `starting_cash` on the spec (that is '
 'a small stated buy-back float, not a stake); the buy-and-hold benchmark is '
 'THE SAME LOT held and never written against, so `excess_return` compares '
 'two uses of one position; `premium_yield_on_lot` — the headline — is '
 'annualized net premium over the TIME-WEIGHTED lot value, while '
 "`annualized_return` is the equity return and therefore carries the shares' "
 'own price move. A symbol whose shares fell can show a strong yield and a '
 'negative return at once; both are true and they answer different '
 'questions. Finally, because the lot is assumed, this says nothing about '
 'entry: a real programme had to buy those shares somewhere, and 12-month '
 'windows on names that survived to be candidates carry the usual selection '
 'bias on top. Re-entry is at the NEXT CLOSE AFTER the call-away, which is '
 'typically ABOVE the strike just surrendered — so the lot chain is '
 'momentum-following by construction and its time-weighted basis resets '
 'upward after every assignment. Read a chain of several lots as a series of '
 'forced re-entries at rising prices, not as one position. Finally, the '
 'coverage split attributes a day to `hold_uncovered` only when the chain '
 'offered NO strike above the basis inside the delta band; every other '
 'stand-down lands in `gate_rejected`, which is IN the coverage denominator '
 '— so a misclassification makes the coverage ratio harsher, never '
 'flattering.')

MODEL_SPREAD_BIAS = ('The bid/ask spread GATE was suspended for this run, because the modelled '
 'spread rejects every contract by construction',
 '`universe.max_spread_pct` is read off MODELLED bid/ask (FC-051), whose '
 'half-spread is at least 5% of mark for an OTM contract — so the '
 "covered-call profile's 0.10 rejected 10 of 10 premium-floor-clearing calls "
 "in the probe, and the arm would have reported 'this strategy never found a "
 "candidate' when what it never found was a spread the model could produce. "
 'The gate is suspended HERE ONLY; the live service still applies it. The '
 'bias is of UNKNOWN SIGN, not merely unknown size, and the two halves pull '
 'opposite ways: suspending the gate is optimistic (the replay writes calls '
 'the live inventory validator might have refused for illiquidity), while '
 'the model itself measures ~2.46x WIDER than the real book, which is '
 'pessimistic on every price it feeds. Do not net them; do not assume this '
 'run flatters the strategy. A test pins the suspension to the spread model, '
 'so the day real spreads arrive this fails loudly and the gate is restored '
 'deliberately rather than staying off because nobody remembered it was.')

ROLL_REACH_BIAS = ('Covered-call ROLL candidates are truncated at 21 DTE, so roll counts and credits are '
 'biased DOWN',
 "The roller's replacement search is bounded by `old_expiry + "
 'rolling.max_extension_days` and by nothing else. On this profile that is 14 + 14 = '
 '28 DTE of chain for a full candidate set. The replay materialises to 21 DTE — the '
 'roll horizon, capped at what the lake stores (`universe_dte = 22`, FC-096 Phase A) — '
 'so the top 7 days of that horizon are absent from every roll decision. Concretely: a '
 'replacement can be extended only to about `21 - k` days past the old expiry, where '
 '`k` is the days the old call still has to run, so the shortfall is largest exactly '
 "when the roller is most useful — early in a freshly-written call's life, which is "
 'when an ITM move is most likely to need defending. Direction: FEWER candidates, '
 'never more. Roll counts and captured credits are FLOORS, not estimates. \n'
 '\n'
 'The WHEEL carries the SAME truncation, one roll later. Its FIRST roll is fully '
 'materialised since FC-112 — a call entered at 7 DTE has a horizon of at most 21, '
 'which is what the replay builds — but a call already rolled OUT is held at roughly '
 '15-22 DTE, so ITS next horizon is `old_expiry + 14` = 29-36 days against the same '
 '21-DTE ladder. Every chained roll after the first therefore chooses from a truncated '
 'set, in this same direction. Wheel ENTRIES are still capped at 7 by the scanner, and '
 'wheel roll candidates past 7 DTE carry the thin-print caveat as well '
 '(`DTE_REACH_BIAS`). Wheel readers get this in their own footer '
 '(`WHEEL_ROLL_REACH_NOTE`), which is also where the `fc-112-wheel-roll-reach` version '
 'boundary is stated. \n'
 '\n'
 'Read `roll_skips` beside the roll counts before concluding anything about roller '
 'activity: a credit-only roller declining 40 evaluations and a roller that could not '
 'price a single one both report the same `rolls_executed`, and only the skip reasons '
 'separate them.')

# FC-112, review round 1 (T2/E2). The WHEEL's own roll-reach footer — the
# wheel half of ROLL_REACH_BIAS, which is emitted on covered-call runs only and
# so was addressed to a reader who could never see it. Emitted on EVERY wheel
# run on both sides, at both reaches: a pre-FC-112 row earns no DTE_REACH_BIAS
# and is exactly the row whose reader most needs the boundary sentence.
WHEEL_ROLL_REACH_NOTE = ('Wheel ROLL candidates are truncated after the FIRST roll of a chain, and wheel roll '
 'numbers are NOT comparable across the `fc-112-wheel-roll-reach` boundary',
 "Since `fc-112-wheel-roll-reach` a wheel window materialises to the ROLLER's horizon "
 '— `call_target_dte` 7 + `rolling.max_extension_days` 14 = 21 — instead of to the '
 "scanner's 7. That is the whole horizon for the FIRST roll of a chain: a call entered "
 'at 7 DTE has at most 8 days to run and cannot want a replacement past 21. It is not '
 'the whole horizon for any roll after that one. A call that has ALREADY been rolled '
 'out is held at roughly 15-22 DTE, so ITS next horizon is `old_expiry + 14` = 29-36 '
 'days, against a ladder that stops at `MAX_SWEEPABLE_DTE` = 21 (the lake stores '
 '`universe_dte = 22`). So every chained roll past the first chooses from a truncated '
 "candidate set, exactly as the covered-call profile's rolls do, and in the same "
 'direction: FEWER and SHORTER replacements, never more. Roll counts and captured '
 'credits on an arm that chains rolls are FLOORS, not estimates.\n'
 '\n'
 'WHICH ARM it costs: the one that rolls MORE. A lower `rolling.itm_trigger_ratio` '
 'authorises OTM roll-OUTS, and roll-outs are the rolls that chain — so the truncation '
 'lands on the arm doing the thing under test and biases AGAINST it, rather than '
 'cancelling between arms. A cheap tell in the data before reading any roll contrast: '
 'on a roll record, a replacement whose DTE sits AT the cutoff is a search that hit '
 'the edge, not one that chose. Count those.\n'
 '\n'
 'THE VERSION BOUNDARY. Wheel roll counts and credits written before `engine_version = '
 "'fc-112-wheel-roll-reach'` were measured on a ladder cut off at 8 days, where the "
 'roller could not reach an OTM roll-out at all; they are NOT comparable with later '
 'ones. Because an executed roll changes the position, the non-comparability is not '
 'confined to the roll columns — `puts_sold`, `calls_sold`, `cycles_completed`, '
 '`option_pnl` and the returns move too on any window that rolls. Partition a wheel '
 'trend on `engine_version` before reading across that boundary.')

ROLL_FILL_RULE = ("Roll legs fill at the placed limit against the day's modeled book, or not at all",
 'The live roller is credit-only AT ITS PLACED LIMITS, so the replay prices roll legs '
 'the same way. A buy-to-close whose limit is at or through the ask fills AT THE ASK '
 '(never at a worse limit); a sell-to-open whose limit is at or through the bid fills '
 'at the BID. That is base mode. In imminence mode the roller rests both legs at `mid '
 '+/- $0.05`, and a limit resting inside the modeled spread is ASSUMED to fill at its '
 "limit within the leg's 120-second window — the replay has one modeled book per "
 'decision day and no intraday tape. A limit outside the book does not fill: the order '
 "expires and the roller's own ladder and terminal dispositions apply, which "
 '`roll_skips` now carries. Entry legs and the covered-call monitor leg are NOT priced '
 'this way; they still fill at the haircut price (FC-072, FC-086).\n'
 '\n'
 'Roll credits therefore now pay the FULL modeled spread on both legs — base-mode '
 '`credit = (mark_new - mark_old) - (hs_new + hs_old)`, where `hs` is the modeled '
 'half-spread (5% of mark, widened OTM and for cheap contracts, floor $0.02). The '
 'spread model measures ~2.46x WIDER than the real book, so replayed roll credits AND '
 'roll counts (a credit invariant tested on wider spreads fails more often) are biased '
 'DOWN versus live.\n'
 '\n'
 'The model has exactly ONE residual, and it is measured rather than estimated: an '
 'imminence-mode leg rests `hs - $0.05` per share inside the far quote, which is NOT '
 'small on a high-mark chain (an `hs` of $0.40-1.00 rests $0.35-0.95 per share per leg '
 'on the assumption). It cuts BOTH ways against the old model: an imminence roll on a '
 'chain with `hs > $0.20` carries MORE credit under this rule than the haircut model '
 'gave it. `roll_legs_resting` on each row counts how many legs rest on the '
 'assumption, and every roll-leg ledger event carries `fill_rule` and `limit_price`, '
 'so a reader can discount a row rather than guess at it.\n'
 '\n'
 'Two things a zero here does NOT mean. On lake/model-built chains no rung-1 roll leg '
 'can expire — every such limit lies inside the book by construction — so a zero in '
 "`roll_skips`' post-placement terminals is the model, not evidence that live rolls "
 'always fill. And the replay attempts every roll the roller wants, whereas live '
 'per-position and cycle budgets can truncate a ladder or skip a position entirely; '
 'replayed roll counts are an upper bound on live ATTEMPTS, independent of the fill '
 'rule.')

ROLL_FILL_LEGACY = ('This run PREDATES FC-116: its roll legs filled at the haircut price, so roll credits '
 'are biased UP',
 'The engine that produced these numbers filled every order — roll legs included — at `mid '
 '-/+ fill_haircut x half-spread`, ignoring the limit the roller placed. The live roller '
 "places its buy-to-close at the old contract's ASK and its sell-to-open at the candidate's "
 'BID, so a modelled roll here captured MORE credit than the same roll would have live, on '
 'BOTH legs. The bias is not measured, so it is named rather than netted into a number that '
 'would look like an estimate.\n'
 '\n'
 'Runs at or after `fc-116-roll-limit-fills` fill roll legs at the placed limits instead, and '
 'carry a different footer. Roll credits and roll counts from this run are therefore NOT '
 "comparable with a post-FC-116 run's: partition any trend series over roll credits on "
 "`engine_version` (or on the row's `roll_fill_mode`, which is NULL exactly for rows like "
 'these) before reading a level shift as a behaviour change.')

CC_ROLL_SPLIT_NOTE = ('Rolls are split into ITM defences and OTM roll-outs, and only the first is '
 'defence',
 'An ITM roll (stock/strike >= 1.0) acts when the stock is through the '
 'strike and assignment is the alternative. An OTM roll-out (0.98-1.0) is '
 'the roller re-writing a call that was never threatened — buying it back at '
 'the ask and selling a higher strike up to 14 days further out at any delta '
 '<= 0.60, which bypasses the delta band, the DTE ceiling, the premium floor '
 'and the spread gate that the entry path applies. The covered-call '
 "profile's `itm_trigger_ratio` is 1.00, so the second bucket should be "
 "EMPTY here and a non-zero count is a finding. The wheel's is 0.98, where "
 'the distinction is load-bearing (FC-112).')

MONITOR_LEG_NOTE = ('The covered-call replay runs the /monitor profit-taking leg; the wheel '
 'replay does not',
 '52% of real covered calls are closed early at a DTE-banded profit target '
 'rather than held to expiry, so a replay without that leg measures a '
 'strategy nobody runs. It is modelled here for the covered-call profile '
 'using `CallSeller.should_close_call_early` — the real predicate, over the '
 "profile's own bands. Two divergences remain, both named rather than "
 'corrected: the live bands are DTE-keyed to <=7 while this profile writes '
 'at a 14-DTE target, so a fresh call sits above the top band for its first '
 'week (FC-086); and production prices the buy-back limit at ask x 0.95 '
 'while this engine fills at its haircut price, because a '
 'one-decision-per-day replay has no intraday path along which to test '
 'whether a limit was touched. The WHEEL replay is deliberately untouched — '
 'adding the leg there would move every stored wheel result at once, which '
 'needs its own FC and its own re-baseline.')

WHEEL_PROFIT_TAKING_TITLE = 'One decision per day, and the replay gets the price it saw'

WHEEL_EX_DIV_TITLE = ('Dividends come from a static table; ex-dividend early assignment has never '
 'fired on real data')

CC_PROFIT_TAKING_BIAS = ('One decision per day — but profit-taking IS modelled on this profile',
 'Production scans and executes ~15 minutes apart; the replay does both on '
 'one snapshot. Unlike the wheel replay, this one runs the /monitor '
 'profit-taking leg, so the 52%-of-calls-closed-early divergence the wheel '
 'footer carries does NOT apply here — see the monitor-leg note below for '
 'the two divergences that remain (the bands are DTE-keyed to <=7 against a '
 "14-DTE target, and the buy-back limit is the engine's haircut fill rather "
 "than production's ask x 0.95). What is still unmodelled is intraday churn: "
 'a position closed and re-opened between two decision points is one '
 'decision here.')

CC_EX_DIV_BIAS = ('Dividends come from a static table, and ex-dividend early assignment CAN '
 'fire on this profile',
 'Both legs collect from the same committed table, so the two stay on one '
 "footing, but a window running past the table's coverage credits nothing "
 'after that point on either. The wheel footer says early assignment has '
 "never fired on real data, and that is a statement about the WHEEL's "
 'universe: its dividend payers are exactly the symbols that cannot clear '
 'the premium floor to open a position. This profile is different — its '
 'universe is whatever the account holds, its calls are written at 14 DTE, '
 'and a held payer with an ITM short call is the ordinary case rather than '
 'an impossible one. Treat a non-zero `ex_dividend_early_assignments` here '
 'as a real event, and a zero as a fact about the window rather than about '
 'the model.')
