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
from typing import Any, Dict, List, Optional, Sequence

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
    "**This report deliberately carries no `binding_constraint` column.** Only "
    "the FIRST replay in a process gets a working `RejectionTally`: "
    "`setup_logging` sets `cache_logger_on_first_use=True`, a structlog lazy "
    "proxy caches its whole processor chain on first use, and "
    "`structlog.configure()` — which is how the tally installs itself — does not "
    "invalidate that cache, so every strategy logger keeps delivering to replay "
    "#1's tally for the life of the process. Replays 2..N would report an empty "
    "`blocked_days_by_reason`, i.e. \"the strategy was never blocked\". That is a "
    "pre-existing defect (it also empties 13 of every 14 rows the monthly screen "
    "writes to `backtest_runs`), and reporting a column that is NULL by artifact "
    "would launder it. Every other number here comes from the broker ledger and "
    "the equity curve and is unaffected."
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


def render_markdown(result: SweepResult) -> str:
    """The operator-facing sweep report."""
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
    a("> **Results are not persisted.** A sweep never writes to "
      "`options_wheel.backtest_runs`: that table's \"current demotion "
      "candidates\" query takes the latest `run_kind='full'` row, so a persisted "
      "sweep would displace the production screen with a hypothetical. This "
      "report is the only record of the run. (A sweep over a COLD window does "
      "still write chains to the local cache, and to the GCS chain lake when "
      "`CHAIN_LAKE_BUCKET` is set — that is the shared chain mirror doing its "
      "job, and it is independent of anything about these results.)")
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
    a("Keys that change what the chain must CONTAIN (`put_target_dte`, a longer "
      "`call_target_dte`) are refused: the cached chains reach a fixed DTE, so "
      "such an arm would silently measure something else. Keys the replay does "
      "not read at all (`risk.profit_taking.*`, the stop-loss switches — both "
      "`/monitor`-only) are refused for the mirror reason: every arm would come "
      "back identical, which reads as \"this knob does not matter\".")
    a("</details>")
    a("")

    a("## Known biases — read the ranking through these")
    a("")
    a(CROSS_SCENARIO_CAVEAT)
    a("")
    a(TALLY_CAVEAT)
    a("")
    for title, detail in SWEEP_BIASES:
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


def render_json(result: SweepResult) -> str:
    """Machine-readable form: the rows, plus everything needed to trust them."""
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
        "known_biases": [{"title": t, "detail": d} for t, d in SWEEP_BIASES],
        "cross_scenario_caveat": CROSS_SCENARIO_CAVEAT,
        "rejection_tally_caveat": TALLY_CAVEAT,
        "in_sample_banner": IN_SAMPLE_BANNER if result.in_sample_only else None,
        "holdout_semantics": HOLDOUT_SEMANTICS if result.has_holdout else None,
        "persisted": False,
    }
    return json.dumps(payload, indent=2, default=str)
