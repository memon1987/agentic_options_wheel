"""Rendering a sweep — and the guardrails that stop it lying (FC-060 D8).

A sweep is a multiple-comparisons machine. Ten arms over six symbols is sixty
numbers, and the arm with the best headline is, more often than not, the arm that
got lucky on one symbol. Everything here exists to make that visible rather than
convenient:

* **The per-symbol grid always renders.** There is no mode that prints one
  blended number per scenario without it. A single mean over six symbols hides
  both the "one symbol carried the arm" case and the "this arm is better
  everywhere by a hair" case, which are opposite findings that deserve opposite
  actions.
* **``insufficient`` is flagged, never averaged.** The verdict means the window
  contained no completed cycle. Rendering it as a return would let "nothing
  happened" contribute a number to a ranking, and rendering it as 0% would make
  it look like a measured flat result. It shows as ``insuf`` and is counted in
  its own column.
* **``low-act`` is flagged the same way.** A cell whose wheel held a position on
  under ``MIN_DAYS_IN_POSITION`` (25%) of decision days is annualising a number
  earned on capital that mostly sat idle — the fewer days deployed, the more one
  lucky trade gets multiplied by 365/days. It is shown with its fraction and
  excluded from every aggregate.
* **In-sample is the DEFAULT, and the default is the dangerous case.** Without a
  ``--holdout-start`` the whole table is a hypothesis chosen on the same data it
  was measured on. That is a banner at the TOP, not a footnote at the bottom.
* **Sign agreement, when a holdout exists.** A scenario that beats the base in
  the fit window and loses to it out of sample has not been validated by the
  holdout — it has been refuted by it, and the column says so per symbol.
* **Deltas are computed over the COMMON measured symbols.** Comparing an arm's
  median over the four symbols it managed to trade against base's median over
  six is not a comparison; it is two different populations with a subtraction
  sign between them. The subset size is printed beside every delta.
* **The bias footer is written FOR THIS REPORT.** ``reporting.report``'s
  ``KNOWN_BIASES`` prose points at a data-quality block, an attribution section
  and a buy-and-hold table that a sweep report does not have, so quoting it
  verbatim sends the reader looking for sections that are not there.
"""

from __future__ import annotations

import json
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..metrics.fitness import MIN_DAYS_IN_POSITION
from .overrides import describe_allowlist
from .runner import BASE_SCENARIO_NAME, ScenarioResult, SweepResult

# The one comparison caveat that is about SWEEPS specifically rather than about
# any single backtest, so it does not live in KNOWN_BIASES.
CROSS_SCENARIO_CAVEAT = (
    "**Comparisons between scenarios that differ in call-leg activity are biased "
    "against the call-heavier one until FC-056 is fixed.** The engine prices "
    "identical call contracts at **last measured 0.676** of the live fill (a ~32% "
    "shortfall, ~5x the put leg's ~7% error) — **FC-056; that figure is stale, "
    "pending the FC-068/078 re-baseline**, so treat it as an order of magnitude "
    "rather than a coefficient. An arm that writes more calls is marked down for "
    "doing so. Rank arms that hold call activity roughly constant; treat a "
    "ranking across arms with very different `calls_sold` as unproven."
)

# Rendered at the TOP of any sweep run without a holdout — which is the default,
# and therefore the case that needs the warning most.
IN_SAMPLE_BANNER = (
    "> ## IN-SAMPLE ONLY — this ranking has not been validated\n"
    ">\n"
    "> Every arm below was measured on the same window it would be chosen from, "
    "over a **single volatility regime** (Alpaca's option history begins "
    "2024-02-01). With 10 arms x 6 symbols there are 60 numbers here, and the "
    "best-looking arm is more often the luckiest one than the best one.\n"
    ">\n"
    "> **Re-run with `--holdout-start` and act on the sign-agreement column, not "
    "on this table.** A ranking that does not survive out of sample has been "
    "refuted, not merely unconfirmed."
)

# What a fit/holdout split does and does not mean. Non-obvious enough that
# omitting it invites the wrong reading of a short holdout.
HOLDOUT_SEMANTICS = (
    "**How the split is run.** The two windows are independent replays, not one "
    "run cut in half: each starts flat with the full `--starting-cash`, carries "
    "no position across the boundary, and derives its own strike anchors from "
    "its own bars. So a holdout does not inherit the fit window's assigned "
    "shares — the wheel begins its cycle again — and the fit window ends the day "
    "BEFORE `--holdout-start`, so the two never overlap. **A short holdout "
    "inflates `insuf`**: a cycle needs a put to be written, held and resolved, "
    "so a window of a few weeks can end with nothing completed on symbols that "
    "traded perfectly well. Read the `insuf` column before reading the medians."
)

# Why there is no "why the strategy stood down" column here, when every
# single-symbol report has one. Stated in the report rather than only in the
# code, because its absence would otherwise read as an omission.
TALLY_CAVEAT = (
    "**This report carries no `binding_constraint` column.** It shipped without "
    "one because only the FIRST replay in a process got a working "
    "`RejectionTally`: `setup_logging` sets `cache_logger_on_first_use=True`, a "
    "structlog lazy proxy caches its whole processor chain on first use, and the "
    "`structlog.configure()` the tally used to install itself does not "
    "invalidate that cache — so every strategy logger kept delivering to replay "
    "#1's tally for the life of the process, and replays 2..N reported an empty "
    "`blocked_days_by_reason`, i.e. \"the strategy was never blocked\". **That "
    "defect is fixed** (FC-092, shipped with FC-096 Phase B): the tally binds "
    "through a process-stable dispatch, so every replay in a sweep now gets a "
    "complete, deterministically-ordered tally. Adding the column is a schema "
    "change with its own review and is not part of that fix — so this report "
    "still does not carry one. Rows the monthly screen wrote to `backtest_runs` "
    "before the fix keep their NULL `binding_constraint`; read it as \"not "
    "measured\", never as \"never blocked\". Every other number here comes from "
    "the broker ledger and the equity curve and was never affected."
)

# The engine's biases, rewritten FOR THIS REPORT. `reporting.report.KNOWN_BIASES`
# is the single-symbol report's footer and its prose refers the reader to "the
# data-quality block above", "the attribution section" and "the buy-and-hold
# comparison below" — none of which a sweep report has. Quoting it verbatim (the
# first cut did) sends a reader hunting for sections that do not exist and buries
# the two caveats that actually change how a RANKING is read. Same facts, aimed
# at the question this report answers.
SWEEP_BIASES = [
    ("Every arm is measured by the same biased engine, so DIFFERENCES survive "
     "better than LEVELS", (
         "Premium is understated on both legs — puts by ~7% against 204 real "
         "decisions, calls far worse (see the call caveat above). Spreads come "
         "from a parametric model measured ~2.5x wider than the real book "
         "(FC-051). Greeks are Black-Scholes inversions, not published values. "
         "None of that cancels perfectly between arms, but it cancels far better "
         "than it does in absolute terms: read the ORDER of these rows, and do "
         "not quote any single cell as a forecast of what the strategy would "
         "have earned.")),
    ("One decision per day, and the replay gets the price it saw", (
         "Production scans and executes ~15 minutes apart; the replay does both "
         "on one snapshot, and holds every contract to expiry or assignment. "
         "Early profit-taking closed 52% of real call positions before expiry "
         "and is unmodelled. Arms that would have changed monitor-cycle churn "
         "cannot be distinguished here at all — which is why the monitor knobs "
         "are refused as overrides rather than swept.")),
    ("Dividends come from a static table; ex-dividend early assignment has never "
     "fired on real data", (
         "Both legs collect from the same committed table, so the two stay on one "
         "footing, but a window running past the table's coverage credits nothing "
         "after that point on either. The early-assignment path needs a dividend "
         "payer holding an ITM short call, and this universe's payers are exactly "
         "the symbols that cannot clear the premium floor to open a position — so "
         "it is validated by unit tests only.")),
    ("One vol regime", (
         "Alpaca's option history starts 2024-02-01, so every window here sits "
         "inside a single regime. A shifted start date can flip a marginal "
         "verdict, and it can reorder two arms that are close. This is the "
         "reason the in-sample banner exists.")),
    ("Splits are refused, not modelled; taxes are not modelled", (
         "A window spanning a split is refused outright (`UnadjustedCorporateAction`) "
         "and shows as an errored cell. Wheel income is short-term gains and "
         "buy-and-hold defers to long-term; published estimates put that drag at "
         "~1-2%/yr, which nothing here deducts.")),
]

# --------------------------------------------------------------------------- #
# M1 (review round 1) — the two SWEEP_BIASES lines that were calibrated on the
# WHEEL and are FALSE on a covered-call run. Substituted by title, never by
# index, so reordering the list above cannot silently swap the wrong caveat.
#
# A footer that states an untruth is worse than one that omits a caveat: the
# reader who checks it stops believing the ones that are true.
# --------------------------------------------------------------------------- #
WHEEL_PROFIT_TAKING_TITLE = "One decision per day, and the replay gets the price it saw"
WHEEL_EX_DIV_TITLE = (
    "Dividends come from a static table; ex-dividend early assignment has never "
    "fired on real data"
)

CC_PROFIT_TAKING_BIAS = (
    "One decision per day — but profit-taking IS modelled on this profile", (
        "Production scans and executes ~15 minutes apart; the replay does both "
        "on one snapshot. Unlike the wheel replay, this one runs the /monitor "
        "profit-taking leg, so the 52%-of-calls-closed-early divergence the "
        "wheel footer carries does NOT apply here — see the monitor-leg note "
        "below for the two divergences that remain (the bands are DTE-keyed to "
        "<=7 against a 14-DTE target, and the buy-back limit is the engine's "
        "haircut fill rather than production's ask x 0.95). What is still "
        "unmodelled is intraday churn: a position closed and re-opened between "
        "two decision points is one decision here."),
)

CC_EX_DIV_BIAS = (
    "Dividends come from a static table, and ex-dividend early assignment CAN "
    "fire on this profile", (
        "Both legs collect from the same committed table, so the two stay on "
        "one footing, but a window running past the table's coverage credits "
        "nothing after that point on either. The wheel footer says early "
        "assignment has never fired on real data, and that is a statement about "
        "the WHEEL's universe: its dividend payers are exactly the symbols that "
        "cannot clear the premium floor to open a position. This profile is "
        "different — its universe is whatever the account holds, its calls are "
        "written at 14 DTE, and a held payer with an ITM short call is the "
        "ordinary case rather than an impossible one. Treat a non-zero "
        "`ex_dividend_early_assignments` here as a real event, and a zero as a "
        "fact about the window rather than about the model."),
)


# The reach above which a sweep's chains stop being the ones this engine was
# measured on. 7 is the live `put_target_dte` and the reach every fidelity
# figure quoted in `SWEEP_BIASES` was measured at.
DTE_REACH_BIAS_THRESHOLD = 7

# FC-096 Phase A PR-2. Appended to the footer ONLY when a run's effective reach
# exceeds `DTE_REACH_BIAS_THRESHOLD` — a 7-reach sweep has not earned this
# caveat, and a footer that warns about something the run did not do is a footer
# people stop reading.
#
# The wording is deliberately NOT "these quotes are extrapolated". They are not:
# a 14- or 21-DTE quote here is a real print of a real contract, with IV solved
# from that contract's own daily trade bar. Saying otherwise would be a stronger
# claim than the truth and misleading in its own direction — the reader would
# discount a number that is real. What IS true is thinner, and worse in a
# specific way, so the caveat says exactly that.
DTE_REACH_BIAS = (
    "Arms reaching past 7 DTE are measured on THINNER data, and the thinness "
    "biases selection rather than merely adding noise", (
        "These are not extrapolated prices: a 14- or 21-DTE quote here is a real "
        "print of a real contract, with implied vol solved from that contract's "
        "own daily trade bar. The problem is WHICH contracts survive. "
        "Longer-dated contracts trade thinly, and the chain builder drops any "
        "contract with no trade that day — so a hole in the ladder is "
        "indistinguishable from a strike that never existed, and the strategy "
        "picks from whatever happened to trade rather than from the real ladder. "
        "That biases SELECTION; it is not a wider error bar around the same "
        "choice. Two further limits ride along: the spread model (FC-051) was "
        "measured on short-dated OTM puts only and is unvalidated at these "
        "tenors, and the premium shortfalls quoted above were measured at 7 DTE. "
        "Read a long-DTE arm's RANK against other long-DTE arms; a long-versus-"
        "short comparison carries this on top of every bias listed here."),
)

# --------------------------------------------------------------------------- #
# FC-096 Phase C — the covered-call footer. Appended ONLY to a covered-call run,
# on the same conditional-footer mechanism as DTE_REACH_BIAS: a footer that
# warns a wheel reader about a synthetic lot nobody seeded is a footer people
# stop reading.
# --------------------------------------------------------------------------- #
SYNTHETIC_LOT_BIAS = (
    "The stock leg is ASSUMED, not bought — every number here is relative to a "
    "lot the engine created", (
        "A covered-call cell seeds 100 shares at the window-start close and, "
        "when they are called away, seeds a fresh lot at the NEXT session's "
        "close (signed decision, 2026-09-08). So the stock leg is a CHAIN of "
        "lots, each with its own basis, and the window is measured end to end "
        "on every symbol rather than truncating at the first call-away — which "
        "would have biased against exactly the names that ran up fastest. "
        "Consequences to read with: the capital base is the lot, not the "
        "`starting_cash` on the spec (that is a small stated buy-back float, "
        "not a stake); the buy-and-hold benchmark is THE SAME LOT held and "
        "never written against, so `excess_return` compares two uses of one "
        "position; `premium_yield_on_lot` — the headline — is annualized net "
        "premium over the TIME-WEIGHTED lot value, while `annualized_return` "
        "is the equity return and therefore carries the shares' own price "
        "move. A symbol whose shares fell can show a strong yield and a "
        "negative return at once; both are true and they answer different "
        "questions. Finally, because the lot is assumed, this says nothing "
        "about entry: a real programme had to buy those shares somewhere, and "
        "12-month windows on names that survived to be candidates carry the "
        "usual selection bias on top. Re-entry is at the NEXT CLOSE AFTER the "
        "call-away, which is typically ABOVE the strike just surrendered — so "
        "the lot chain is momentum-following by construction and its "
        "time-weighted basis resets upward after every assignment. Read a "
        "chain of several lots as a series of forced re-entries at rising "
        "prices, not as one position. Finally, the coverage split attributes a "
        "day to `hold_uncovered` only when the chain offered NO strike above "
        "the basis inside the delta band; every other stand-down lands in "
        "`gate_rejected`, which is IN the coverage denominator — so a "
        "misclassification makes the coverage ratio harsher, never "
        "flattering."),
)

MODEL_SPREAD_BIAS = (
    "The bid/ask spread GATE was suspended for this run, because the modelled "
    "spread rejects every contract by construction", (
        "`universe.max_spread_pct` is read off MODELLED bid/ask (FC-051), whose "
        "half-spread is at least 5% of mark for an OTM contract — so the "
        "covered-call profile's 0.10 rejected 10 of 10 premium-floor-clearing "
        "calls in the probe, and the arm would have reported 'this strategy "
        "never found a candidate' when what it never found was a spread the "
        "model could produce. The gate is suspended HERE ONLY; the live "
        "service still applies it. Read this as an OPTIMISTIC bias of unknown "
        "size: the replay writes calls the live inventory validator might have "
        "refused for illiquidity, and the modelled spread measures ~2.46x wider "
        "than the real book, so the direction is not even reliably one way. A "
        "test pins the suspension to the spread model, so the day real spreads "
        "arrive this fails loudly and the gate is restored deliberately rather "
        "than staying off because nobody remembered it was."),
)

ROLL_REACH_BIAS = (
    "Covered-call ROLL candidates are truncated at 21 DTE, so roll counts and "
    "credits are biased DOWN — and the fill model biases credits UP", (
        "The roller's replacement search is bounded by `old_expiry + "
        "rolling.max_extension_days` and by nothing else. On this profile that "
        "is 14 + 14 = 28 DTE of chain for a full candidate set. The replay "
        "materialises to 21 DTE — the roll horizon, capped at what the lake "
        "stores (`universe_dte = 22`, FC-096 Phase A) — so the top 7 days of "
        "that horizon are absent from every roll decision. Concretely: a "
        "replacement can be extended only to about `21 - k` days past the old "
        "expiry, where `k` is the days the old call still has to run, so the "
        "shortfall is largest exactly when the roller is most useful — early in "
        "a freshly-written call's life, which is when an ITM move is most "
        "likely to need defending. Direction: FEWER candidates, never more. "
        "Roll counts and captured credits are FLOORS, not estimates. "
        "\n\nIn the OPPOSITE direction, and not netted against it: the live "
        "roller places its buy-to-close at the old contract's ASK and its "
        "sell-to-open at the candidate's BID, while this engine fills every "
        "order at its haircut price from the mark. A modelled roll therefore "
        "captures MORE credit than the same roll would live, on both legs. "
        "Neither bias is measured, so they are both named rather than "
        "combined into a single number that would look like an estimate. "
        "\n\nThe WHEEL carries the same truncation, unmeasured and unfixed "
        "here: its horizon is 7 + 14 = 21 against a 7-DTE materialisation. "
        "Widening it would move every stored wheel number at once, so it is "
        "left to FC-112 — which already owns the wheel's roll-trigger study — "
        "rather than changed as a side effect of a covered-call release. "
        "\n\nRead `roll_skips` beside the roll counts before concluding "
        "anything about roller activity: a credit-only roller declining 40 "
        "evaluations and a roller that could not price a single one both "
        "report the same `rolls_executed`, and only the skip reasons separate "
        "them."),
)

CC_ROLL_SPLIT_NOTE = (
    "Rolls are split into ITM defences and OTM roll-outs, and only the first "
    "is defence", (
        "An ITM roll (stock/strike >= 1.0) acts when the stock is through the "
        "strike and assignment is the alternative. An OTM roll-out (0.98-1.0) "
        "is the roller re-writing a call that was never threatened — buying it "
        "back at the ask and selling a higher strike up to 14 days further out "
        "at any delta <= 0.60, which bypasses the delta band, the DTE ceiling, "
        "the premium floor and the spread gate that the entry path applies. "
        "The covered-call profile's `itm_trigger_ratio` is 1.00, so the second "
        "bucket should be EMPTY here and a non-zero count is a finding. The "
        "wheel's is 0.98, where the distinction is load-bearing (FC-112)."),
)

MONITOR_LEG_NOTE = (
    "The covered-call replay runs the /monitor profit-taking leg; the wheel "
    "replay does not", (
        "52% of real covered calls are closed early at a DTE-banded profit "
        "target rather than held to expiry, so a replay without that leg "
        "measures a strategy nobody runs. It is modelled here for the "
        "covered-call profile using `CallSeller.should_close_call_early` — the "
        "real predicate, over the profile's own bands. Two divergences remain, "
        "both named rather than corrected: the live bands are DTE-keyed to <=7 "
        "while this profile writes at a 14-DTE target, so a fresh call sits "
        "above the top band for its first week (FC-086); and production prices "
        "the buy-back limit at ask x 0.95 while this engine fills at its "
        "haircut price, because a one-decision-per-day replay has no intraday "
        "path along which to test whether a limit was touched. The WHEEL "
        "replay is deliberately untouched — adding the leg there would move "
        "every stored wheel result at once, which needs its own FC and its own "
        "re-baseline."),
)


def sweep_biases(result: SweepResult) -> List[Tuple[str, str]]:
    """``SWEEP_BIASES``, plus the caveats THIS run actually earned.

    One function so the markdown footer and the JSON ``known_biases`` cannot
    disagree about which caveats this run carries — the dashboard derives the
    same condition from the persisted spec (`services/sweeps.py`).

    Every conditional is on a fact of the RUN, never on a possibility: the
    covered-call lines appear iff a covered-call sweep produced them, and
    ``MODEL_SPREAD_BIAS`` appears iff the gate was actually suspended.
    """
    biases = list(SWEEP_BIASES)
    if int(getattr(result, "effective_max_dte", 0) or 0) > DTE_REACH_BIAS_THRESHOLD:
        biases.append(DTE_REACH_BIAS)
    if str(getattr(result, "strategy", "wheel") or "wheel") != "wheel":
        # M1: swap the two wheel-calibrated lines for their covered-call
        # counterparts, matched on TITLE so a reorder of SWEEP_BIASES cannot
        # substitute the wrong one.
        substitutions = {
            WHEEL_PROFIT_TAKING_TITLE: CC_PROFIT_TAKING_BIAS,
            WHEEL_EX_DIV_TITLE: CC_EX_DIV_BIAS,
        }
        biases = [substitutions.get(title, (title, detail))
                  for title, detail in biases]
        biases.append(SYNTHETIC_LOT_BIAS)
        biases.append(MONITOR_LEG_NOTE)
        biases.append(ROLL_REACH_BIAS)
        biases.append(CC_ROLL_SPLIT_NOTE)
        if getattr(result, "spread_gate_suspended", False):
            biases.append(MODEL_SPREAD_BIAS)
    return biases


_VERDICT_GLYPH = {
    "fit": "+",
    "marginal": "~",
    "unfit": "-",
    "insufficient": "?",
}


def _pct(value: Optional[float], width: str = "+.1%") -> str:
    """A missing number renders as an em dash, never as 0%.

    "+0.0%" reads as "measured, and exactly flat", which is a different claim
    from "we have no number here" — the same reason ``render_screen_summary``
    refuses to print a zero benchmark.
    """
    return "—" if value is None else format(value, width)


def _cell(row: Optional[ScenarioResult]) -> str:
    if row is None:
        return "—"
    if row.error:
        return "**err**"
    if row.insufficient:
        return "`insuf`"
    if row.low_activity:
        # The fraction is printed, not just the flag: "low-act 4%" and
        # "low-act 24%" are very different amounts of evidence, and collapsing
        # them into one label would hide which cells are nearly usable.
        return f"`low-act {row.days_in_position_fraction:.0%}`"
    glyph = _VERDICT_GLYPH.get(row.verdict or "", "?")
    return f"{_pct(row.annualized_return)} {glyph}"


def _measured(rows: Sequence[ScenarioResult]) -> List[ScenarioResult]:
    """Rows carrying a number worth ranking.

    Excludes errors, `insufficient` (no completed cycle) and `low-act` (the
    wheel held a position on under MIN_DAYS_IN_POSITION of decision days). All
    three are counted in their own columns; none contributes to a median.
    """
    return [r for r in rows if r.measured]


def _scenario_rows(result: SweepResult, scenario: str, split: str) -> List[ScenarioResult]:
    return [
        r for r in result.rows
        if r.scenario == scenario and r.split == split
    ]


# The persistence note, in both of its true forms (FC-060 Layer 3). It used to
# have only one, asserting that nothing was stored — which stopped being true the
# moment `--persist` and the `backtest-sweep` Job existed, and a report that says
# "this report is the only record of the run" while the rows sit in BigQuery is
# not a caveat, it is a false statement in the place a reader trusts most.
_NOT_PERSISTED = (
    "> **Results are not persisted.** This run was not given `--persist`, so "
    "this report is the only record of it. A sweep never writes to "
    "`options_wheel.backtest_runs` under any flag: that table's \"current "
    "demotion candidates\" query takes the latest `run_kind='full'` row, so a "
    "persisted sweep would displace the production screen with a hypothetical."
)

_PERSISTED = (
    "> **Results are persisted** to `{dataset}.scenario_sweeps` / "
    "`{dataset}.scenario_runs` as run `{run_id}`{key}. **Never to "
    "`options_wheel.backtest_runs`**: that table's \"current demotion "
    "candidates\" query takes the latest `run_kind='full'` row, and neither "
    "sweep table has a `run_kind` column, so a hypothetical cannot displace the "
    "production screen. See `docs/bigquery/scenario_runs.md`."
)

_COLD_WINDOW_NOTE = (
    "(Either way, a sweep over a COLD window does write chains to the local "
    "cache, and to the GCS chain lake when `CHAIN_LAKE_BUCKET` is set — that is "
    "the shared chain mirror doing its job, and it is independent of anything "
    "about these results.)"
)


def persistence_note(persistence) -> str:
    """The stored-or-not paragraph, matching what actually happened."""
    if persistence is None or not getattr(persistence, "persisted", False):
        return f"{_NOT_PERSISTED} {_COLD_WINDOW_NOTE}"
    key = (f" (`sweep_key` `{persistence.sweep_key}`)"
           if getattr(persistence, "sweep_key", None) else "")
    return (_PERSISTED.format(dataset=persistence.dataset,
                              run_id=persistence.run_id, key=key)
            + " " + _COLD_WINDOW_NOTE)


def render_markdown(result: SweepResult, persistence=None) -> str:
    """The operator-facing sweep report.

    ``persistence`` is an optional ``main.SweepPersistence`` describing where
    this run was stored. Omitted (the default, and the CLI's usual case) the
    report says so; supplied, it names the dataset, the ``run_id`` and the
    ``sweep_key`` so an operator reading a printed report can go and query it.
    Duck-typed rather than imported: this module must stay importable without
    ``main``.
    """
    out: List[str] = []
    a = out.append

    windows = ", ".join(f"**{sp}** {s} → {e}" for sp, s, e in result.windows)
    a("# Scenario sweep")
    a("")
    a(f"{len(result.scenarios)} scenarios × {len(result.symbols)} symbols · "
      f"{windows} · ${result.starting_cash:,.0f} per symbol")
    a("")
    a(f"Base config hash `{result.base_config_hash}` · "
      f"materialise {sum(result.materialise_seconds.values()):.1f}s · "
      f"replays {sum(result.replay_seconds.values()):.1f}s · "
      f"wall {result.wall_seconds:.1f}s · "
      f"provider fetches {result.provider_fetches_total} "
      f"({result.provider_calls_during_replays} during replays), "
      f"bar-cache hits {result.bar_cache_hits}")
    a("")
    if result.in_sample_only:
        # First thing under the header, before a single number. A ranking chosen
        # on the data it was measured on is a hypothesis, and this is the DEFAULT
        # path — the reader has to trip over the caveat, not go looking for it.
        a(IN_SAMPLE_BANNER)
        a("")
    a(persistence_note(persistence))
    a("")

    # FC-096 A4. The FC-013 gate answers "clear" for a symbol it has no row for,
    # which is indistinguishable from a symbol that genuinely has no earnings in
    # the window — so a sweep over a freshly-onboarded candidate would report a
    # gated strategy that was never gated. Stated in the HEADER, above the
    # numbers, because it changes what every row below means for those symbols.
    if result.earnings_symbols_without_data:
        a("> **The earnings gate did not gate "
          f"{', '.join(result.earnings_symbols_without_data)}.** "
          "These symbols are absent from the committed earnings table entirely, "
          "so `EarningsCalendarService` had no date to test and every candidate "
          "on them passed the FC-013 gate by default. Refresh the table and "
          "re-run before reading their rows as a test of anything earnings-"
          "related.")
        a("")

    if result.errors:
        a(f"> **{len(result.errors)} of {len(result.rows)} cells errored.** They are "
          "**not** implicitly fine — they were never measured. See *Errors* below.")
        a("")

    for split, w_start, w_end in result.windows:
        a(f"## Annualized return by scenario × symbol — {split} ({w_start} → {w_end})")
        a("")
        a(_grid(result, split))
        a("")

    a("## Per-scenario summary")
    a("")
    a(_scenario_summary(result))
    a("")
    a(f"Median/min/max are taken over **measured** cells only. A cell that is "
      f"`insuf` (no completed cycle in the window), `low-act` (a position held on "
      f"under {MIN_DAYS_IN_POSITION:.0%} of decision days, so its annualised "
      f"number rests on capital that mostly sat idle) or errored contributes to "
      f"its own count and to nothing else.")
    a("")

    if result.has_holdout:
        a("## Fit vs holdout")
        a("")
        a(_holdout_table(result))
        a("")
        a("`sign agreement` counts the symbols where this scenario's return "
          "**relative to `base`** has the same sign in both windows. A scenario "
          "that beats the base in-sample and loses out of sample has been "
          "refuted by the holdout, not validated by it. Symbols where any of the "
          "four cells is `insuf`, `low-act` or errored are excluded from both the "
          "count and the denominator.")
        a("")
        a("`Δ vs base` is computed over the symbols measured in **both** arms — "
          "the count in brackets — not over each arm's own median. Comparing an "
          "arm's median across the four symbols it managed to trade against "
          "base's median across six is two populations with a minus sign between "
          "them, and it systematically flatters whichever arm traded less.")
        a("")
        a(HOLDOUT_SEMANTICS)
        a("")

    errors = result.errors
    if errors:
        a("## Errors")
        a("")
        a("| scenario | symbol | split | error |")
        a("|---|---|---|---|")
        for row in errors:
            a(f"| {row.scenario} | {row.symbol} | {row.split} | "
              f"{(row.error or '')[:160]} |")
        a("")

    a("## Scenario definitions")
    a("")
    a("| scenario | scenario hash | config hash | fill haircut | overrides |")
    a("|---|---|---|---|---|")
    base_cfg_hash = result.scenario_config_hashes.get(BASE_SCENARIO_NAME)
    for name in result.scenarios:
        overrides = result.scenario_overrides.get(name) or {}
        rendered = ("_(base — no overrides)_" if not overrides
                    else "; ".join(f"`{k}` = `{v}`" for k, v in sorted(overrides.items())))
        cfg = result.scenario_config_hashes.get(name, "")
        # "= base" rather than the repeated hex, because a column of identical
        # hashes reads as a bug. The two hashes answer different questions and
        # the note below says which.
        cfg_cell = ("= base" if name != BASE_SCENARIO_NAME and cfg == base_cfg_hash
                    else f"`{cfg}`")
        haircut = result.scenario_fill_haircuts.get(name)
        haircut_cell = "_(default)_" if haircut is None else f"{haircut:.2f}"
        a(f"| {name} | `{result.scenario_hashes.get(name, '')}` | {cfg_cell} | "
          f"{haircut_cell} | {rendered} |")
    a("")
    a("**`scenario hash` is the identity of the ARM**; `config hash` exists to "
      "line a row up with a `backtest_runs` row and cannot tell two arms apart on "
      "its own. It hashes nine strategy parameters plus the module scoring "
      "constants, so 12 of the 19 allowlisted override keys — every `rolling.*` "
      "and `earnings.*` key, `universe.*`, `min_avg_volume` — do not move it, and "
      "the haircut it hashes is the module default rather than the scenario's. A "
      "`= base` in that column means exactly that: same nine parameters, "
      "different arm.")
    a("")
    a("<details><summary>Overrides are restricted to selection-only keys</summary>")
    a("")
    for line in describe_allowlist():
        a(f"- `{line}`")
    a("")
    a("Both DTE targets are allowed within `1..MAX_SWEEPABLE_DTE` — the reach "
      "the stored chain lake is kept at (FC-096 Phase A). Beyond it the "
      "contracts are simply not in the file, so the arm would read as \"nothing "
      "qualified\" rather than as a test of that reach, which is why the bound "
      "is a property of the DATA and moves only when the lake does. Keys the "
      "replay does not read at all (`risk.profit_taking.*`, the stop-loss "
      "switches — both `/monitor`-only) are refused for the mirror reason: every "
      "arm would come back identical, which reads as \"this knob does not "
      "matter\".")
    a("</details>")
    a("")

    a("## Known biases — read the ranking through these")
    a("")
    a(CROSS_SCENARIO_CAVEAT)
    a("")
    a(TALLY_CAVEAT)
    a("")
    for title, detail in sweep_biases(result):
        a(f"- **{title}.** {detail}")
    a("")
    a("The single-symbol report (`--command backtest`) carries the full "
      "`KNOWN_BIASES` text, with the per-run data-quality, attribution and "
      "buy-and-hold sections those caveats refer to. This list is the same facts "
      "framed for a comparison between arms.")
    a("")
    return "\n".join(out)


def _grid(result: SweepResult, split: str) -> str:
    """Scenario × symbol, annualized return plus a verdict glyph."""
    header = "| scenario | " + " | ".join(result.symbols) + " |"
    rule = "|---|" + "---:|" * len(result.symbols)
    lines = [header, rule]
    for name in result.scenarios:
        cells = [_cell(result.cell(name, symbol, split)) for symbol in result.symbols]
        label = f"**{name}**" if name == BASE_SCENARIO_NAME else name
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        f"Glyphs: `+` fit · `~` marginal · `-` unfit · `insuf` no completed cycle "
        f"in the window (**not** a return of zero) · "
        f"`low-act N%` a position held on under {MIN_DAYS_IN_POSITION:.0%} of "
        f"decision days, so the annualised number rests on idle capital · "
        f"`err` never measured. Only the first three contribute to any median.")
    return "\n".join(lines)


def _scenario_summary(result: SweepResult) -> str:
    lines = [
        "| scenario | split | median | min | max | measured | insuf | low-act | "
        "demote-flags | err |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in result.scenarios:
        for split, _s, _e in result.windows:
            rows = _scenario_rows(result, name, split)
            measured = _measured(rows)
            values = [r.annualized_return for r in measured
                      if r.annualized_return is not None]
            lines.append(
                f"| {name} | {split} | "
                f"{_pct(median(values) if values else None)} | "
                f"{_pct(min(values) if values else None)} | "
                f"{_pct(max(values) if values else None)} | "
                f"{len(measured)} | "
                f"{sum(1 for r in rows if r.insufficient)} | "
                f"{sum(1 for r in rows if r.low_activity)} | "
                f"{sum(1 for r in rows if r.demote)} | "
                f"{sum(1 for r in rows if r.error)} |"
            )
    return "\n".join(lines)


def common_delta(
    result: SweepResult, scenario: str, split: str
) -> "tuple[Optional[float], int]":
    """``(median delta vs base, n)`` over the symbols measured in BOTH arms.

    Per-symbol deltas are taken first and the median of THOSE is reported —
    not the difference of the two medians. The two coincide only when both arms
    measure the same symbols, and when they do not, the difference-of-medians
    is comparing an arm's four symbols against base's six and calling the gap a
    result. It flatters whichever arm traded less, which is the wrong direction
    for a sweep to be wrong in: an arm that stops trading looks better.

    ``n`` is reported beside the number so a delta over two symbols cannot be
    read as a delta over six. An empty common subset returns ``(None, 0)``, which
    renders blank rather than as zero.
    """
    deltas = []
    for symbol in result.symbols:
        arm = result.cell(scenario, symbol, split)
        base = result.cell(BASE_SCENARIO_NAME, symbol, split)
        if (arm is None or base is None or not arm.measured or not base.measured
                or arm.annualized_return is None or base.annualized_return is None):
            continue
        deltas.append(arm.annualized_return - base.annualized_return)
    if not deltas:
        return None, 0
    return median(deltas), len(deltas)


def sign_agreement(result: SweepResult, scenario: str) -> "tuple[int, int]":
    """``(agreeing, comparable)`` symbols for one scenario's fit/holdout pair.

    "Agrees" means: this scenario's annualized return MINUS the base scenario's,
    in the fit window, has the same sign as the same difference in the holdout
    window. Measuring the delta against the base rather than the raw return is
    the point — a scenario whose raw return is positive in both windows has shown
    nothing about itself, only that the market went up.

    A symbol is comparable only when all four cells (this scenario and base, both
    windows) are measured — which excludes `insuf` and `low-act` alike, since
    neither carries a number worth taking a sign from. Zero is a legitimate sign
    of its own and counts as agreement only against another zero, which is the
    honest reading of "no difference in either window".
    """
    agreeing = comparable = 0
    for symbol in result.symbols:
        cells = [
            result.cell(scenario, symbol, "fit"),
            result.cell(scenario, symbol, "holdout"),
            result.cell(BASE_SCENARIO_NAME, symbol, "fit"),
            result.cell(BASE_SCENARIO_NAME, symbol, "holdout"),
        ]
        if any(c is None or not c.measured or c.annualized_return is None
               for c in cells):
            continue
        fit_delta = cells[0].annualized_return - cells[2].annualized_return
        hold_delta = cells[1].annualized_return - cells[3].annualized_return
        comparable += 1
        if (fit_delta > 0) == (hold_delta > 0) and (fit_delta < 0) == (hold_delta < 0):
            agreeing += 1
    return agreeing, comparable


def _delta_cell(result: SweepResult, scenario: str, split: str) -> str:
    """``+2.0% (n=5)``, or blank when no symbol is measured in both arms.

    Blank, never ``+0.0%``: "the two arms share no comparable symbol" and "the
    two arms performed identically" are opposite findings, and rendering the
    first as the second is how a sweep reports a tie it never measured.
    """
    if scenario == BASE_SCENARIO_NAME:
        return "—"
    value, n = common_delta(result, scenario, split)
    if value is None:
        return ""
    return f"{_pct(value)} (n={n})"


def _holdout_table(result: SweepResult) -> str:
    lines = [
        "| scenario | fit median | holdout median | Δ vs base (fit) | "
        "Δ vs base (holdout) | sign agreement |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in result.scenarios:
        agreeing, comparable = sign_agreement(result, name)
        agreement = ("—" if name == BASE_SCENARIO_NAME or comparable == 0
                     else f"{agreeing}/{comparable}")
        lines.append(
            f"| {name} | {_pct(_median_of(result, name, 'fit'))} | "
            f"{_pct(_median_of(result, name, 'holdout'))} | "
            f"{_delta_cell(result, name, 'fit')} | "
            f"{_delta_cell(result, name, 'holdout')} | {agreement} |"
        )
    return "\n".join(lines)


def _median_of(result: SweepResult, scenario: str, split: str) -> Optional[float]:
    values = [
        r.annualized_return for r in _measured(_scenario_rows(result, scenario, split))
        if r.annualized_return is not None
    ]
    return median(values) if values else None


def render_json(result: SweepResult, persistence=None) -> str:
    """Machine-readable form: the rows, plus everything needed to trust them.

    ``persisted`` was hardcoded ``False`` here. Since FC-060 Layer 3 it is the
    truth, and a stored run additionally carries ``run_id`` / ``sweep_key`` /
    ``dataset`` so the JSON export and the BigQuery rows can be joined by
    anything that reads both.
    """
    payload: Dict[str, Any] = {
        "scenarios": result.scenarios,
        "symbols": result.symbols,
        "windows": [
            {"split": split, "start": s.isoformat(), "end": e.isoformat()}
            for split, s, e in result.windows
        ],
        "starting_cash": result.starting_cash,
        "run_sensitivity": result.run_sensitivity,
        "base_config_hash": result.base_config_hash,
        "scenario_config_hashes": result.scenario_config_hashes,
        # The identity of each ARM. `config_hash` cannot separate two arms that
        # differ outside its nine strategy keys, which is 12 of the 19
        # allowlisted override keys — see Scenario.scenario_hash.
        "scenario_hashes": result.scenario_hashes,
        "scenario_overrides": result.scenario_overrides,
        "scenario_fill_haircuts": result.scenario_fill_haircuts,
        # THE headline caveat when true, and true by default: no holdout was
        # asked for, so nothing here is validated out of sample.
        "in_sample_only": result.in_sample_only,
        "min_days_in_position": MIN_DAYS_IN_POSITION,
        "timing": {
            "materialise_seconds": result.materialise_seconds,
            "replay_seconds": result.replay_seconds,
            "wall_seconds": result.wall_seconds,
        },
        "provider_calls": {
            # Network round-trips. A bar served from the cache is a
            # `bar_cache_hits`, not one of these.
            "fetches": result.provider_fetches_total,
            "bar_cache_hits": result.bar_cache_hits,
            "during_replays": result.provider_calls_during_replays,
        },
        "rows": [row.as_dict() for row in result.rows],
        "sign_agreement": (
            {name: dict(zip(("agreeing", "comparable"), sign_agreement(result, name)))
             for name in result.scenarios}
            if result.has_holdout else None
        ),
        "delta_vs_base": {
            split: {
                name: dict(zip(("median", "symbols"),
                               common_delta(result, name, split)))
                for name in result.scenarios
            }
            for split, _s, _e in result.windows
        },
        "known_biases": [{"title": t, "detail": d} for t, d in sweep_biases(result)],
        # The reach this run was materialised to. Reported rather than implied:
        # it is what decides whether `known_biases` carries DTE_REACH_BIAS, and a
        # consumer that cannot see the input cannot check the output.
        "effective_max_dte": result.effective_max_dte,
        "earnings_symbols_without_data": list(result.earnings_symbols_without_data),
        "cross_scenario_caveat": CROSS_SCENARIO_CAVEAT,
        "rejection_tally_caveat": TALLY_CAVEAT,
        "in_sample_banner": IN_SAMPLE_BANNER if result.in_sample_only else None,
        "holdout_semantics": HOLDOUT_SEMANTICS if result.has_holdout else None,
        "persisted": bool(getattr(persistence, "persisted", False)),
        "run_id": getattr(persistence, "run_id", None),
        "sweep_key": getattr(persistence, "sweep_key", None),
        "dataset": getattr(persistence, "dataset", None),
    }
    return json.dumps(payload, indent=2, default=str)
