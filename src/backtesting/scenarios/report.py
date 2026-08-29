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
* **Sign agreement, when a holdout exists.** A scenario that beats the base in
  the fit window and loses to it out of sample has not been validated by the
  holdout — it has been refuted by it, and the column says so per symbol.
* **The engine's biases ride in the footer**, copied from
  ``reporting.report.KNOWN_BIASES`` rather than restated, plus the one caveat
  that only matters when comparing arms: the call leg is understated ~32%
  (FC-056), so an arm that writes more calls is being marked down relative to
  one that writes fewer.
"""

from __future__ import annotations

import json
from statistics import median
from typing import Any, Dict, List, Optional, Sequence

from ..reporting.report import KNOWN_BIASES
from .overrides import describe_allowlist
from .runner import BASE_SCENARIO_NAME, ScenarioResult, SweepResult

# The one comparison caveat that is about SWEEPS specifically rather than about
# any single backtest, so it does not live in KNOWN_BIASES.
CROSS_SCENARIO_CAVEAT = (
    "**Comparisons between scenarios that differ in call-leg activity are biased "
    "against the call-heavier one until FC-056 is fixed.** The engine prices "
    "identical call contracts at 0.676 of the live fill (a ~32% shortfall, ~5x "
    "the put leg's ~7% error), so an arm that writes more calls is marked down "
    "for doing so. Rank arms that hold call activity roughly constant; treat a "
    "ranking across arms with very different `calls_sold` as unproven."
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
    glyph = _VERDICT_GLYPH.get(row.verdict or "", "?")
    return f"{_pct(row.annualized_return)} {glyph}"


def _measured(rows: Sequence[ScenarioResult]) -> List[ScenarioResult]:
    """Rows that carry a real measurement: no error, and a completed cycle."""
    return [r for r in rows if r.ok and not r.insufficient]


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
      f"provider calls {result.provider_calls_total} "
      f"({result.provider_calls_during_replays} during replays)")
    a("")
    a("> **Not persisted.** A sweep never writes to `options_wheel.backtest_runs`: "
      "that table's \"current demotion candidates\" query takes the latest "
      "`run_kind='full'` row, so a persisted sweep would displace the production "
      "screen with a hypothetical. This report is the only record of the run.")
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
    a("Median/min/max are taken over **measured** cells only — a cell that is "
      "`insuf` or errored contributes to its own count and to nothing else.")
    a("")

    if result.has_holdout:
        a("## Fit vs holdout")
        a("")
        a(_holdout_table(result))
        a("")
        a("`sign agreement` counts the symbols where this scenario's return "
          "**relative to `base`** has the same sign in both windows. A scenario "
          "that beats the base in-sample and loses out of sample has been "
          "refuted by the holdout, not validated by it. Symbols where either "
          "window is `insuf` or errored are excluded from both the count and the "
          "denominator.")
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
    a("| scenario | config hash | overrides |")
    a("|---|---|---|")
    for name in result.scenarios:
        overrides = result.scenario_overrides.get(name) or {}
        rendered = ("_(base — no overrides)_" if not overrides
                    else "; ".join(f"`{k}` = `{v}`" for k, v in sorted(overrides.items())))
        a(f"| {name} | `{result.scenario_config_hashes.get(name, '')}` | {rendered} |")
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
    for title, detail in KNOWN_BIASES:
        a(f"- **{title}.** {detail}")
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
    lines.append("Glyphs: `+` fit · `~` marginal · `-` unfit · "
                 "`insuf` no completed cycle in the window (**not** a return of "
                 "zero) · `err` never measured.")
    return "\n".join(lines)


def _scenario_summary(result: SweepResult) -> str:
    lines = [
        "| scenario | split | median | min | max | measured | insuf | demote-flags | err |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
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
                f"{sum(1 for r in rows if r.demote)} | "
                f"{sum(1 for r in rows if r.error)} |"
            )
    return "\n".join(lines)


def sign_agreement(result: SweepResult, scenario: str) -> "tuple[int, int]":
    """``(agreeing, comparable)`` symbols for one scenario's fit/holdout pair.

    "Agrees" means: this scenario's annualized return MINUS the base scenario's,
    in the fit window, has the same sign as the same difference in the holdout
    window. Measuring the delta against the base rather than the raw return is
    the point — a scenario whose raw return is positive in both windows has shown
    nothing about itself, only that the market went up.

    A symbol is comparable only when all four cells (this scenario and base, both
    windows) are measured. Zero is a legitimate sign of its own and counts as
    agreement only against another zero, which is the honest reading of "no
    difference in either window".
    """
    agreeing = comparable = 0
    for symbol in result.symbols:
        cells = [
            result.cell(scenario, symbol, "fit"),
            result.cell(scenario, symbol, "holdout"),
            result.cell(BASE_SCENARIO_NAME, symbol, "fit"),
            result.cell(BASE_SCENARIO_NAME, symbol, "holdout"),
        ]
        if any(c is None or not c.ok or c.insufficient
               or c.annualized_return is None for c in cells):
            continue
        fit_delta = cells[0].annualized_return - cells[2].annualized_return
        hold_delta = cells[1].annualized_return - cells[3].annualized_return
        comparable += 1
        if (fit_delta > 0) == (hold_delta > 0) and (fit_delta < 0) == (hold_delta < 0):
            agreeing += 1
    return agreeing, comparable


def _holdout_table(result: SweepResult) -> str:
    lines = [
        "| scenario | fit median | holdout median | Δ vs base (fit) | "
        "Δ vs base (holdout) | sign agreement |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    base_medians = {
        split: _median_of(result, BASE_SCENARIO_NAME, split)
        for split in ("fit", "holdout")
    }
    for name in result.scenarios:
        fit = _median_of(result, name, "fit")
        hold = _median_of(result, name, "holdout")
        agreeing, comparable = sign_agreement(result, name)
        agreement = ("—" if name == BASE_SCENARIO_NAME or comparable == 0
                     else f"{agreeing}/{comparable}")
        lines.append(
            f"| {name} | {_pct(fit)} | {_pct(hold)} | "
            f"{_pct(_delta(fit, base_medians['fit']))} | "
            f"{_pct(_delta(hold, base_medians['holdout']))} | {agreement} |"
        )
    return "\n".join(lines)


def _median_of(result: SweepResult, scenario: str, split: str) -> Optional[float]:
    values = [
        r.annualized_return for r in _measured(_scenario_rows(result, scenario, split))
        if r.annualized_return is not None
    ]
    return median(values) if values else None


def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if (a is None or b is None) else a - b


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
        "scenario_overrides": result.scenario_overrides,
        "timing": {
            "materialise_seconds": result.materialise_seconds,
            "replay_seconds": result.replay_seconds,
            "wall_seconds": result.wall_seconds,
        },
        "provider_calls": {
            "total": result.provider_calls_total,
            "during_replays": result.provider_calls_during_replays,
        },
        "rows": [row.as_dict() for row in result.rows],
        "sign_agreement": (
            {name: dict(zip(("agreeing", "comparable"), sign_agreement(result, name)))
             for name in result.scenarios}
            if result.has_holdout else None
        ),
        "known_biases": [{"title": t, "detail": d} for t, d in KNOWN_BIASES],
        "cross_scenario_caveat": CROSS_SCENARIO_CAVEAT,
        "rejection_tally_caveat": TALLY_CAVEAT,
        "persisted": False,
    }
    return json.dumps(payload, indent=2, default=str)
