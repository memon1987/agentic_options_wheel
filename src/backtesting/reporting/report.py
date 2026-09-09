"""Markdown + JSON rendering of a fitness report.

The report's job is to be *hard to misread*. Three things it always does:

- shows option-leg and stock-leg P&L apart, never only their sum;
- states the buy-and-hold result next to the strategy result;
- carries a footer of known biases, so a reader never has to guess whether a
  number is measured, modeled, or unmodeled.

Numbers come pre-computed from ``metrics.fitness``; nothing is calculated here
beyond formatting.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import List, Optional

from ..engine.rejections import NOT_A_STAND_DOWN, stand_down_reasons
from ..engine.simulator import (
    SYNTHETIC_LOT_PREMISE, SYNTHETIC_LOT_SHARES,
)
from ..metrics.cycles import WheelCycle
from ..metrics.fitness import FitnessReport

# Biases we know about and have chosen not to model. Printed on every report:
# an unlisted caveat is one the reader will assume does not exist.
KNOWN_BIASES = [
    ("Premium understated — and much more on the CALL leg", (
        "Option marks come from daily bar closes (trade prints), while the live "
        "bot fills intraday against real quotes. The two legs are NOT equally "
        "faithful, and the call leg is the weaker one: "
        "PUTS, over 204 real decisions — identical contracts price ~7% below "
        "live's fill, rising to ~20% once selection differs; 81% strike "
        "reproduction. "
        "CALLS, over 80 real decisions — identical contracts price at only "
        "0.676 of live (a ~32% shortfall, roughly 5x the put leg's error), with "
        "55.2% strike reproduction. Delta-band accuracy is 100% on both legs, "
        "so the engine always picks a correct-RISK contract; it is the price, "
        "and on calls the strike, that drift. The call figure was unmeasurable "
        "until FC-048 — before that the engine could not produce a covered call "
        "at all. Cause of the call gap is unknown (DTE mix was tested and "
        "disconfirmed); see FC-056 and docs/investigations/fc-032-call-parity.md. "
        "Direction is conservative on both legs: reported returns are a FLOOR, "
        "not a forecast. Do not quote an absolute call-leg return as accurate.")),
    ("Bid/ask is modeled, not calibrated", (
        "Alpaca sells no historical option quotes at any price, so spreads come "
        "from a parametric model running on its DEFAULT parameters — "
        "SpreadModel.calibrate() exists but is never invoked on this path. "
        "Measured against 524 live OTM put quotes DURING regular trading hours "
        "(2026-07-29 11:39 ET, AAPL/AMD/IWM/NVDA) by "
        "tools/diagnostics/spread_model_check.py --require-rth (re-runnable): "
        "median real half-spread $0.0250 vs $0.0614 modeled — the model is "
        "~2.5x wider, and wider on 86% of contracts, which errs against the "
        "strategy. This RETIRES the earlier caveat: an after-hours sample had "
        "shown 2.1x and warned the gap might close intraday, leaving "
        "'conservative' unproven. It does not close — intraday the model is "
        "MORE conservative, not less. What remains uncalibrated is the model's "
        "SHAPE: a fit on that same RTH sample scores R^2 = 0.03 and per-symbol "
        "base_frac spans 15x (AMD 0.0050 .. AAPL 0.0773), so a single pooled "
        "parameter set is not usable and none is committed. See FC-051.")),
    ("Greeks are computed", (
        "IV and delta are Black-Scholes inversions from the bar close, not "
        "published values. Dividend yield is treated as zero for every symbol, "
        "which is immaterial at ~7 DTE but not free.")),
    ("Dividends are modeled from a static table", (
        "Both legs collect dividends WHEN THE TABLE COVERS THE RUN — the wheel "
        "is credited on each ex-date it holds assigned shares, and the "
        "buy-and-hold benchmark collects the whole stream over the window "
        "(FC-042 Track C). Check the `dividend_coverage` line in the "
        "data-quality block above before relying on that: a symbol absent from "
        "the table, an unreadable table, or a window running past the table's "
        "coverage all produce $0 on both legs, and the attribution section says "
        "so explicitly when it happens. Amounts come from a "
        "committed table built from Alpaca's corporate-actions endpoint — "
        "as-declared rates, deliberately NOT split-adjusted, so they stay in "
        "the same units as the unadjusted bars and as-listed strikes. What "
        "remains is table staleness, not direction: the table is a snapshot "
        "(see its `generated` date), so a window running past it credits "
        "nothing after that point, and a rate revised after the fact is read at "
        "its revised value. Re-run tools/backtesting/fetch_dividend_table.py "
        "when extending a window forward. ETF rows (SPY/QQQ/IWM) are fund "
        "distributions rather than declared corporate dividends — same cash "
        "flow, lumpier composition, and year-end capital-gains distributions "
        "are included. Neither leg REINVESTS: the benchmark's dividends are "
        "added as flat cash at the end rather than buying more shares, while "
        "the wheel's land in cash on the ex-date where they can back the next "
        "put. That asymmetry favours the wheel whenever the stock rises, and is "
        "small relative to the stream itself.")),
    ("Ex-dividend early assignment is modeled but has NEVER FIRED in a real replay", (
        "A short ITM call whose remaining extrinsic value is below the next "
        "day's dividend is assigned the evening before the ex-date, which is "
        "the dominant real-world early-assignment trigger (FC-042 Track C). "
        "Two residuals. First, extrinsic is measured off that day's chain mark; "
        "when the contract did not trade there is no mark and the position is "
        "left alone rather than assigned — see `ex_div_calls_with_no_mark` in "
        "the data-quality block above, which is the count of times that "
        "happened, and is optimistic when non-zero. Second, early assignment "
        "for reasons other than a dividend (deep-ITM pin risk, hard-to-borrow "
        "recalls) is still unmodeled — rare, and also optimistic. "
        "THIRD, AND READ THIS ONE: the path has never executed on real data. It "
        "requires a dividend payer holding an ITM short call, and the payers in "
        "this universe (F, PFE, KMI, VZ) are precisely the symbols that cannot "
        "clear the premium floor to open a position at all (FC-034), while the "
        "symbols that do trade pay little or nothing. Before FC-048 no replay "
        "produced a covered call whatsoever, so `early_assignments` has read 0 "
        "in every run this engine has ever done. The logic is validated by unit "
        "tests against hand-built broker state, NOT by any historical replay. "
        "Treat it as untested-in-anger.")),
    ("One decision per day, with zero decision-to-fill latency", (
        "The live bot scans three times daily; the replay decides once at the "
        "close. Production also splits scanning from execution across separate "
        "invocations minutes-to-hours apart, whereas the replay executes at the "
        "same instant and price it decided on — so the replay always gets the "
        "price it saw. Optimistic.")),
    ("Splits and other corporate actions are NOT modeled", (
        "Underlying bars are deliberately unadjusted, because historical option "
        "strikes are as-listed-at-the-time and never retroactively adjusted; "
        "adjusted bars would price a post-split underlying against pre-split "
        "strikes. A run whose window spans a split is REFUSED outright rather "
        "than reported.")),
    ("Single vol regime", (
        "Alpaca's option history starts 2024-02-01, so results cover one "
        "market regime. A shifted start date can flip a marginal verdict.")),
    ("Taxes not modeled", (
        "Wheel income is short-term gains; buy-and-hold defers to long-term. "
        "Published estimates put the drag at ~1-2%/yr, which the buy-and-hold "
        "comparison below does NOT deduct.")),
]


def render_markdown(report: FitnessReport) -> str:
    """Full human-readable report."""
    out: List[str] = []
    a = out.append

    # M3 (FC-096 Phase C, review round 1). A covered-call report is not a
    # "Wheel fitness" report and does not have a "starting capital": its capital
    # base is the synthetic lot, and its `starting_cash` is a $5,000 liquidity
    # reserve deliberately excluded from every ratio. Printing the wheel's
    # header over covered-call numbers mislabels the whole page.
    if report.is_wheel:
        a(f"# Wheel fitness: {report.symbol}")
        a("")
        a(f"**{report.start} → {report.end}** ({report.days} days) · "
          f"starting capital ${report.starting_cash:,.0f}")
    else:
        a(f"# Covered-call fitness: {report.symbol}")
        a("")
        a(f"**{report.start} → {report.end}** ({report.days} days) · "
          f"capital base ${report.capital_base:,.0f} "
          f"(a SYNTHETIC {SYNTHETIC_LOT_SHARES}-share lot; "
          f"${report.starting_cash:,.0f} cash reserve excluded from every "
          f"ratio)")
        a("")
        a(f"_{SYNTHETIC_LOT_PREMISE}_")
    a("")

    verdict = report.verdict()
    # "insufficient" was missing here and raised KeyError, so rendering a report
    # for any symbol that closed no cycle in the window crashed outright —
    # which is precisely the income names (F/PFE/KMI/VZ) this FC exists to make
    # judgeable. Unknown verdicts degrade to the raw string rather than a crash:
    # a report is the artifact you reach for when something looks wrong, so it
    # must not be the thing that fails.
    badge = {
        "fit": "FIT",
        "marginal": "MARGINAL",
        "unfit": "UNFIT",
        "insufficient": "INSUFFICIENT EVIDENCE",
    }.get(verdict, verdict.upper())
    a(f"## Verdict: {badge}")
    a("")
    for reason in report.verdict_reasons():
        a(f"- {reason}")
    a("")

    # Why the strategy stood down. A verdict that hides its own binding
    # constraint invites the wrong action: "this symbol is unfit" reads very
    # differently from "our gap filter excludes this symbol in high-vol regimes".
    #
    # FC-069 item 12 (review fix 1): "already holds this underlying" is counted
    # by the tally but is NOT a stand-down — it is the wheel deployed, which on
    # a healthy symbol is most days in the window. Leading this table with it,
    # and calling it the binding constraint, would report the strategy working
    # as the strategy blocked. It gets its own line below the table instead.
    all_blocked = report.data_quality.get("blocked_days_by_reason") or {}
    blocked = stand_down_reasons(all_blocked)
    deployed_days = sum(count for reason, count in all_blocked.items()
                        if reason in NOT_A_STAND_DOWN)
    if blocked:
        candidates = report.data_quality.get("days_with_a_qualifying_candidate")
        a("### Why the strategy stood down")
        a("")
        a("| blocking reason | days |")
        a("|---|---:|")
        for reason, count in blocked.items():
            a(f"| {reason} | {count} |")
        a("")
        top = next(iter(blocked))
        a(f"The binding constraint was **{top}**.")
        if candidates is not None:
            a("")
            a(f"The chain was actually *examined* on **{candidates}** of "
              f"{report.decision_days} decision days and offered a qualifying "
              f"candidate on those. On the rest an earlier stage blocked first, "
              f"so this is NOT a count of days a tradeable option existed — "
              f"where the binding constraint sits upstream of stage 7, the "
              f"chain was never looked at.")
        a("")

    if deployed_days:
        a("### Capital deployment (not a stand-down)")
        a("")
        a(f"On **{deployed_days}** of {report.decision_days} decision days the "
          f"put scan skipped this symbol because a position was already open on "
          f"it. That is the wheel working — one position per underlying is the "
          f"design — so it is reported here rather than in the stand-down table "
          f"above, and it is never stamped as the binding constraint. A high "
          f"number here means high deployment, not high blockage.")
        a("")

    # ---- Headline: strategy vs the only benchmark that matters --------------
    a("## Strategy vs buy-and-hold")
    a("")
    a("| | total return | final value |")
    a("|---|---:|---:|")
    if report.is_wheel:
        a(f"| Wheel | {report.total_return:+.2%} | ${report.final_equity:,.0f} |")
    else:
        # M3: `final_equity` is NOT comparable to the benchmark's final value on
        # a covered-call cell — it carries the cash reserve and the whole seeded
        # lot chain, so the reviewer's rally cell printed $54,767 against
        # $21,700 with $43,500 of injected lots. The lot-based figure is the one
        # that means anything beside a lot-based benchmark.
        a(f"| Covered call on the lot | {report.total_return:+.2%} | "
          f"${report.capital_base * (1 + report.total_return):,.0f} |")
    if report.benchmark:
        b = report.benchmark
        a(f"| Buy & hold ({b.shares} sh @ ${b.entry_price:,.2f}) | "
          f"{b.total_return:+.2%} | ${b.final_value:,.0f} |")
        excess = report.excess_return or 0.0
        verdict_word = "ahead of" if excess >= 0 else "behind"
        a(f"| **Difference** | **{excess:+.2%}** | |")
        a("")
        leg = "wheel" if report.is_wheel else "covered-call programme"
        a(f"The {leg} finished **{abs(excess):.2%} {verdict_word}** simply owning "
          f"{report.symbol} over the same window.")
        if not report.is_wheel:
            a("")
            a(f"Net premium **${report.net_premium:,.2f}** — a "
              f"**{(report.premium_yield_on_lot or 0.0):+.2%}** annualized "
              f"yield on the lot, and a "
              f"**{(report.net_basis_reduction or 0.0):+.2%}** reduction of its "
              f"basis so far. That yield is a STATEMENT, not the verdict: the "
              f"comparison above is total-return against total-return, because "
              f"premium alone cannot be measured against a benchmark that "
              f"includes the shares' price move.")
        if b.dividends:
            a("")
            a(f"Buy-and-hold's return includes **${b.dividends:,.0f}** of "
              f"dividends (${b.dividends_per_share:.4f}/share over {b.shares} "
              f"shares); its price return alone was {b.price_return:+.2%}. The "
              f"wheel collected ${report.dividends:,.0f} on the days it actually "
              f"held shares. Comparing a wheel with dividends against a "
              f"benchmark without them is the single easiest way to make this "
              f"table lie, so both carry them.")
    else:
        a("")
        a("_Buy-and-hold benchmark unavailable (no price at window edges)._")
    a("")
    a(f"Annualized on the whole account: {report.annualized_return:+.2%}")
    aroc = report.annualized_return_on_collateral
    if aroc is not None:
        a("")
        a(f"**Annualized on committed collateral: {aroc:+.2%}** "
          f"(${report.avg_collateral:,.0f} average). This is the scale-free "
          f"number — the account figure above is diluted by the arbitrary "
          f"${report.starting_cash:,.0f} allocation and mostly measures idle cash.")
    a("")

    # ---- Attribution: the flagship number ----------------------------------
    a("## Attribution — where the money came from")
    a("")
    a("| leg | P&L |")
    a("|---|---:|")
    a(f"| Option premium (net of buybacks and fees) | ${report.option_pnl:+,.2f} |")
    a(f"| Stock — realized (called away) | ${report.stock_pnl:+,.2f} |")
    a(f"| Stock — unrealized (still held) | ${report.unrealized_stock_pnl:+,.2f} |")
    a(f"| Dividends (collected while holding assigned shares) | "
      f"${report.dividends:+,.2f} |")
    a(f"| **Total** | **${report.attribution_total:+,.2f}** |")
    a("")
    a(f"_Memo: ${report.fees:,.2f} of fees is already deducted inside the option "
      f"row. Equity change over the window was ${report.total_pnl:+,.2f}._")

    # A $0 dividend row means four very different things — pays nothing, not in
    # the table, table unreadable, or never held shares. The bias footer below
    # states flatly that both legs collect dividends, so where that is NOT true
    # for this run it has to be said here, next to the number.
    coverage = report.data_quality.get("dividend_coverage") or {}
    if coverage and coverage.get("status") != "complete for this window":
        a("")
        status = str(coverage["status"]).rstrip(".")
        a(f"> **Dividend data is incomplete for this run:** {status}. "
          f"The dividend row above and the benchmark's dividend line below are "
          f"understated by an unknown amount, and the 'dividends are modeled' "
          f"note in the bias list does not fully hold here.")
    if abs(report.reconciliation_gap) > 0.01:
        a("")
        a(f"> **Attribution does not reconcile** — the rows above sum to "
          f"${report.attribution_total:+,.2f} but equity moved "
          f"${report.total_pnl:+,.2f}, a gap of "
          f"${report.reconciliation_gap:+,.2f}. Some cash flow is unaccounted "
          f"for, so treat the split below as unreliable.")
    a("")
    share = report.option_pnl_share
    if share is None:
        a("Option and stock legs have opposite signs, so a percentage split would "
          "mislead — read the two rows above directly.")
    else:
        a(f"**{share:.0%} of gross P&L came from the option leg**, "
          f"{1 - share:.0%} from the stock leg.")
        if share < 0.25:
            a("")
            a("> This is mostly a long-stock position wearing a wheel costume. "
              "Published wheel studies find the same thing; the point of showing "
              "it is that it should change how you read the headline return.")
    a("")

    # ---- Cycles ------------------------------------------------------------
    a("## Cycles")
    a("")
    closed = report.closed_cycles
    a(f"- Completed cycles: **{len(closed)}** "
      f"({len(report.cycles) - len(closed)} still open at window end)")
    a(f"- Puts sold: {report.puts_sold} · calls sold: {report.calls_sold} · "
      f"rolls: {report.rolls}")
    if report.decision_days:
        a(f"- Held a position on **{report.days_in_position} of "
          f"{report.decision_days}** decision days "
          f"(**{report.days_in_position_fraction:.0%}**)")
        if report.avg_collateral > 0:
            a(f"- Average collateral committed while in a position: "
              f"**${report.avg_collateral:,.0f}** (peak ${report.peak_collateral:,.0f})")
    if report.win_rate is not None:
        a(f"- Win rate: **{report.win_rate:.0%}** "
          f"(expected to be high at 0.10-0.20 delta — read with the worst cycle below)")
    if report.assignment_rate is not None:
        a(f"- Assignment rate: {report.assignment_rate:.0%}")
    a("")
    a(_cycle_table(report.cycles))
    a("")

    worst = report.worst_cycle
    if worst is not None:
        a("### Worst cycle")
        a("")
        a(f"**{worst.underlying}**, {worst.start} → {worst.end} ({worst.days} days), "
          f"total **${worst.total_pnl:+,.2f}**")
        a("")
        a(f"- Option leg ${worst.option_pnl:+,.2f}, stock leg ${worst.stock_pnl:+,.2f}")
        if worst.assigned:
            a(f"- Assigned at ${worst.cost_basis:,.2f}"
              + (f", called away at ${worst.exit_price:,.2f}" if worst.called_away
                 else ", still holding at window end"))
        a("")

    # ---- Risk --------------------------------------------------------------
    a("## Risk")
    a("")
    a(f"- Max drawdown: **{report.max_drawdown:.2%}** _(of total account equity, "
      f"which is mostly idle cash — not of the position)_")
    a(f"- Sharpe {report.sharpe:.2f} · Sortino {report.sortino:.2f} "
      f"_(also on total equity; flattered by the idle-cash denominator, "
      f"so treat as relative-only)_")
    underwater_pct = (
        report.days_underwater / report.decision_days if report.decision_days else 0.0
    )
    a(f"- Days holding shares below cost basis: **{report.days_underwater}** of "
      f"{report.decision_days} decision days ({underwater_pct:.0%}) — the "
      f"wheel's signature failure mode")
    a("")

    if report.data_quality:
        a("## Data quality")
        a("")
        for key, value in sorted(report.data_quality.items()):
            a(f"- {key}: {value}")
        a("")

    # ---- Caveats -----------------------------------------------------------
    a("## Known biases in these numbers")
    a("")
    for title, detail in KNOWN_BIASES:
        a(f"- **{title}.** {detail}")
    a("")
    return "\n".join(out)


def _cycle_table(cycles: List[WheelCycle]) -> str:
    if not cycles:
        return "_No cycles._"
    rows = [
        "| # | start | end | days | puts | calls | option P&L | stock P&L | total | ann. | outcome |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, c in enumerate(cycles, 1):
        rows.append(
            f"| {i} | {c.start} | {c.end or '—'} | {c.days} | {c.puts_sold} | "
            f"{c.calls_sold} | ${c.option_pnl:+,.0f} | ${c.stock_pnl:+,.0f} | "
            f"${c.total_pnl:+,.0f} | {c.annualized_return:+.0%} | {c.outcome()} |"
        )
    return "\n".join(rows)


def render_json(report: FitnessReport, *, sensitivity: Optional[dict] = None) -> str:
    """Machine-readable form; the same numbers the markdown renders."""
    payload = {
        "symbol": report.symbol,
        "start": report.start.isoformat(),
        "end": report.end.isoformat(),
        "days": report.days,
        "verdict": report.verdict(),
        "verdict_reasons": report.verdict_reasons(),
        "starting_cash": report.starting_cash,
        "final_equity": report.final_equity,
        "total_pnl": report.total_pnl,
        "total_return": report.total_return,
        "annualized_return": report.annualized_return,
        "attribution": {
            "option_pnl": report.option_pnl,
            "stock_pnl_realized": report.stock_pnl,
            "stock_pnl_unrealized": report.unrealized_stock_pnl,
            "stock_pnl_total": report.total_stock_pnl,
            "dividends": report.dividends,
            "fees_memo": report.fees,
            "attribution_total": report.attribution_total,
            "reconciliation_gap": report.reconciliation_gap,
            "option_pnl_share": report.option_pnl_share,
        },
        "cycles": {
            "completed": len(report.closed_cycles),
            "open": len(report.cycles) - len(report.closed_cycles),
            "puts_sold": report.puts_sold,
            "calls_sold": report.calls_sold,
            "rolls": report.rolls,
            "win_rate": report.win_rate,
            "assignment_rate": report.assignment_rate,
            "table": [_cycle_json(c) for c in report.cycles],
        },
        "risk": {
            "max_drawdown": report.max_drawdown,
            "sharpe": report.sharpe,
            "sortino": report.sortino,
            "days_underwater": report.days_underwater,
        },
        "activity": {
            "decision_days": report.decision_days,
            "days_in_position": report.days_in_position,
            "days_in_position_fraction": report.days_in_position_fraction,
            "avg_collateral": report.avg_collateral,
            "peak_collateral": report.peak_collateral,
        },
        "benchmark": (
            {
                "shares": report.benchmark.shares,
                "entry_price": report.benchmark.entry_price,
                "exit_price": report.benchmark.exit_price,
                "total_return": report.benchmark.total_return,
                "price_return": report.benchmark.price_return,
                "dividends": report.benchmark.dividends,
                "dividends_per_share": report.benchmark.dividends_per_share,
                "final_value": report.benchmark.final_value,
                "excess_return": report.excess_return,
            }
            if report.benchmark
            else None
        ),
        "data_quality": report.data_quality,
        "known_biases": [{"title": t, "detail": d} for t, d in KNOWN_BIASES],
    }
    if sensitivity:
        payload["fill_sensitivity"] = sensitivity
    return json.dumps(payload, indent=2, default=_fallback)


def _cycle_json(c: WheelCycle) -> dict:
    return {
        "underlying": c.underlying,
        "start": c.start.isoformat(),
        "end": c.end.isoformat() if c.end else None,
        "days": c.days,
        "puts_sold": c.puts_sold,
        "calls_sold": c.calls_sold,
        "option_pnl": c.option_pnl,
        "stock_pnl": c.stock_pnl,
        "dividends": c.dividends,
        "total_pnl": c.total_pnl,
        "assigned": c.assigned,
        "called_away": c.called_away,
        "cost_basis": c.cost_basis,
        "exit_price": c.exit_price,
        "capital_at_risk": c.capital_at_risk,
        "annualized_return": c.annualized_return,
        "outcome": c.outcome(),
    }


def _fallback(obj):
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)
