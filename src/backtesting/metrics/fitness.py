"""Per-symbol fitness scorecard.

Answers the question FC-032 exists to answer: *is this symbol a good fit for the
wheel as we run it?* — and answers it against the only benchmark that matters,
buy-and-hold of the same symbol over the same window.

Two design commitments, both from the research and both easy to get wrong:

**Attribution is mandatory.** In published SPY wheel studies ~94-99% of total
return came from the stock leg, not premium. A scorecard that reports one blended
number cannot distinguish "this strategy earns premium" from "this symbol went
up". Option and stock P&L are therefore always carried separately.

**A high win rate is not evidence of skill.** Selling 15-delta puts wins ~85% of
the time by construction; the number says almost nothing beyond the delta chosen.
So win rate is reported alongside the tail — worst cycle, max drawdown, and time
spent underwater holding assigned shares — never on its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from ..engine.simulator import DailyState
from .cycles import WheelCycle

TRADING_DAYS_PER_YEAR = 252

# Below this fraction of decision days holding a position, a symbol is judged
# untradeable rather than scored. The wheel sells ~7 DTE, so a symbol it can
# actually trade sits in a position most of the time; 25% is well under a
# continuously-wheeled name (NVDA measured 76%) and well above one that traded
# once or twice (KMI measured 3%).
MIN_DAYS_IN_POSITION = 0.25

# Absolute performance floor. Beating buy-and-hold over a short window is close
# to a coin flip, so a purely relative gate lets a sub-cash result read "fit".
RISK_FREE_RATE = 0.04

# Account-equity drawdown that should draw attention. With max_position_size
# 0.35 and one open position per symbol in evaluate mode, equity simply cannot
# fall 20%, so the old gate could never fire.
MAX_DRAWDOWN_WARN = -0.08

#: FC-096 Phase C §C3. The covered-call ``low_activity`` floor: a call must be
#: open on at least 30% of the days the strategy COULD have written one. The
#: denominator excludes below-basis and earnings-span stand-downs
#: (``simulator.COVERAGE_NOT_A_STAND_DOWN``) — those are the guards working, and
#: counting them here would demote the symbols the cost-basis floor protected.
#: Deliberately NOT ``MIN_DAYS_IN_POSITION``: the wheel's 0.25 is a fraction of
#: DECISION days with any position at all, and a CC replay holds a lot on
#: essentially every day by construction, which would make that test inert.
MIN_COVERED_FRACTION = 0.30


@dataclass
class BuyAndHold:
    """The benchmark: the same capital, in the stock, for the same window.

    The dividend stream is part of the benchmark's return, not a rounding
    detail. Modelling dividends on the wheel leg alone would not remove the old
    both-directions bias — it would invert it, and hand the wheel a free ~15
    points against a 6.5% yielder over a multi-year window. Both legs collect
    dividends or neither does.
    """

    shares: int
    entry_price: float
    exit_price: float
    starting_cash: float
    dividends_per_share: float = 0.0

    @property
    def dividends(self) -> float:
        """Total cash a continuous holder of ``shares`` collects over the window."""
        return self.shares * self.dividends_per_share

    @property
    def price_return(self) -> float:
        """Capital appreciation only — what this benchmark used to report."""
        if self.starting_cash <= 0:
            return 0.0
        return self.shares * (self.exit_price - self.entry_price) / self.starting_cash

    @property
    def final_value(self) -> float:
        return (
            self.starting_cash
            + self.shares * (self.exit_price - self.entry_price)
            + self.dividends
        )

    @property
    def total_return(self) -> float:
        if self.starting_cash <= 0:
            return 0.0
        return (self.final_value - self.starting_cash) / self.starting_cash


@dataclass
class FitnessReport:
    """Everything the evaluate-mode report needs, already computed."""

    symbol: str
    start: date
    end: date
    starting_cash: float
    final_equity: float

    # Attribution — never collapsed into one number.
    option_pnl: float = 0.0  # premium net of buybacks AND fees (fees are inside)
    stock_pnl: float = 0.0  # realized: called away or otherwise disposed
    unrealized_stock_pnl: float = 0.0  # open assigned shares, marked to the last close
    dividends: float = 0.0
    fees: float = 0.0  # memo only; already deducted inside option_pnl

    # Cycle statistics.
    cycles: List[WheelCycle] = field(default_factory=list)
    puts_sold: int = 0
    calls_sold: int = 0
    assignments: int = 0
    rolls: int = 0

    # Risk.
    max_drawdown: float = 0.0
    days_underwater: int = 0
    sharpe: float = 0.0
    sortino: float = 0.0

    # Activity. A symbol the strategy cannot actually trade produces a tiny,
    # flattering return on a handful of days; this is what exposes that.
    days_in_position: int = 0
    avg_collateral: float = 0.0  # mean reserved collateral across decision days
    peak_collateral: float = 0.0

    benchmark: Optional[BuyAndHold] = None
    data_quality: Dict = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # FC-096 Phase C — the covered-call half. `strategy` stays `wheel` and
    # `lot_capital` stays 0.0 on every wheel replay, so `capital_base` below is
    # `starting_cash` exactly and no wheel number moves.
    # ------------------------------------------------------------------ #
    strategy: str = "wheel"
    #: **The two lot aggregations, and why a covered-call replay needs both.**
    #:
    #: Rev 3 §C3 wrote one number — ``starting_cash + Σ seeded lot values`` —
    #: and the 2026-09-08 re-seed decision wrote another, the TIME-WEIGHTED lot
    #: value, for ``premium_yield_on_lot``. Under a chain of lots they are not
    #: interchangeable, and each is correct in exactly one place:
    #:
    #: * ``lot_capital_injected`` (Σ) is the NUMERATOR's. Every seeding event
    #:   puts value into ``final_equity`` without passing through cash, so each
    #:   one must come back out or its whole value reads as profit. A lot seeded
    #:   at $10k, called away at $11k, and replaced by an $11k lot has injected
    #:   $21k; subtracting anything less books the difference as a gain nobody
    #:   made. With Σ, ``total_pnl`` reconciles against the attribution rows
    #:   exactly as it does on the wheel.
    #: * ``lot_capital`` (time-weighted) is the DENOMINATOR's. That programme
    #:   never held $21k at once — it held ~$10.5k throughout — and dividing by
    #:   Σ would understate its return by half. This is also the denominator the
    #:   signed decision names for the headline yield, so the two agree.
    #:
    #: They are EQUAL for a lot seeded on day one and held to the end, which is
    #: the single-lot case both texts were describing.
    lot_capital: float = 0.0
    lot_capital_injected: float = 0.0
    #: Calls closed by the CC monitor leg, and the coverage split, carried for
    #: the report and the verdict. Never collapsed into one ratio.
    calls_closed_early: int = 0
    coverage_by_reason: Dict[str, int] = field(default_factory=dict)
    synthetic_lots_opened: int = 0
    itm_rolls: int = 0
    otm_roll_outs: int = 0

    # ------------------------------------------------------------------ #
    @property
    def is_wheel(self) -> bool:
        return self.strategy == "wheel"

    @property
    def capital_base(self) -> float:
        """The denominator EVERY ratio on this report is taken over.

        ``starting_cash`` for a wheel replay; ``starting_cash + lot_capital``
        for a covered-call one, where ``starting_cash`` is the small stated
        buy-back float (``simulator.CC_CASH_FLOAT``) and ``lot_capital`` is the
        time-weighted lot value.

        This is the C3 fix for the defect round-1 review found: the synthetic
        lot arrives at ``cash_delta = 0`` and is then marked to market, so a
        base of ``starting_cash`` alone would book the ENTIRE lot value as
        profit. A no-trade covered-call window returned "+2,900%" and read as
        the best result in the sweep. Dividing by the capital the premise
        assumed is what makes a no-trade window return 0.0%.
        """
        return self.starting_cash + self.lot_capital

    @property
    def total_pnl(self) -> float:
        """Equity change, with the seeded lot removed.

        The lot never passed through cash, so ``final_equity`` contains its full
        market value while ``starting_cash`` contains none of it. Subtracting
        the assumed capital is the other half of the same fix as
        ``capital_base``: without it the numerator carries the lot as a gain and
        no choice of denominator can make the ratio honest.

        Uses ``lot_capital_injected`` (Σ), NOT ``capital_base``'s time-weighted
        term — see the field docstring. Σ is what actually entered equity, so
        this keeps ``reconciliation_gap`` at zero on a covered-call replay for
        the same reason it is zero on a wheel one.
        """
        return self.final_equity - self.starting_cash - self.lot_capital_injected

    @property
    def total_return(self) -> float:
        if self.capital_base <= 0:
            return 0.0
        return self.total_pnl / self.capital_base

    @property
    def net_premium(self) -> float:
        """Premium received net of buybacks and fees — the CC numerator."""
        return self.option_pnl

    @property
    def premium_yield_on_lot(self) -> Optional[float]:
        """THE covered-call headline: annualized net premium ÷ lot value.

        The denominator is the TIME-WEIGHTED average seeded lot value, which is
        well-formed under both call-away postures: with a single lot it IS that
        lot's value, and across the signed re-seed chain each lot contributes
        its value times the days it was held, over the window's decision days.

        Distinct from ``annualized_return``, which stays the corrected EQUITY
        return (premium plus the lot's own price move plus dividends). Both are
        reported and the footer says which is which: a covered-call programme is
        judged on the premium it harvests against the shares it is holding, not
        on whether the shares happened to go up.

        ``None`` for a wheel replay (no lot) and for a window with no lot-days —
        never 0.0, which would read as "this programme earned nothing".
        """
        if self.lot_capital <= 0 or self.days <= 0:
            return None
        return (self.net_premium / self.lot_capital) * (365.0 / self.days)

    @property
    def net_basis_reduction(self) -> Optional[float]:
        """Cumulative net premium ÷ the lot basis — what CC programmes manage to.

        Un-annualized on purpose: it answers "how much of the shares' cost has
        this programme written off so far", which is a running total and not a
        rate.
        """
        if self.lot_capital <= 0:
            return None
        return self.net_premium / self.lot_capital

    @property
    def coverage_fraction(self) -> Optional[float]:
        """Covered days ÷ days the strategy could have been covered.

        The denominator EXCLUDES ``hold_uncovered`` and ``earnings_span``
        (``COVERAGE_NOT_A_STAND_DOWN``): standing down below basis is the
        cost-basis floor working and an earnings-span day is the FC-013 gate
        working, and a metric that counted either against the strategy would
        demote exactly the symbols its guards protected. It also excludes
        ``post_call_away``, which is not a day the lot existed to be covered.

        ``None`` when the denominator is empty — a window in which the strategy
        was never in a position to write has no coverage ratio, and printing
        0% for it would read as a verdict.
        """
        from ..engine.simulator import (
            COVERAGE_COVERED, COVERAGE_NOT_A_STAND_DOWN, COVERAGE_POST_CALL_AWAY,
        )

        eligible = sum(
            days for reason, days in (self.coverage_by_reason or {}).items()
            if reason not in COVERAGE_NOT_A_STAND_DOWN
            and reason != COVERAGE_POST_CALL_AWAY
        )
        if eligible <= 0:
            return None
        return (self.coverage_by_reason or {}).get(COVERAGE_COVERED, 0) / eligible

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def annualized_return(self) -> float:
        """Linear annualization; short windows are not compounded into fiction."""
        if self.days <= 0:
            return 0.0
        return self.total_return * (365.0 / self.days)

    @property
    def total_stock_pnl(self) -> float:
        """Realized plus unrealized. Both are the stock leg.

        Counting only realized P&L understates the stock leg badly whenever a
        cycle is still open: an NVDA replay finished holding shares assigned at
        177.50 that had appreciated ~$2,000, which showed up in equity (and so
        in the headline return) but nowhere in attribution — the report claimed
        "100% from the option leg" for a result that was two-thirds long stock.
        """
        return self.stock_pnl + self.unrealized_stock_pnl

    @property
    def attribution_total(self) -> float:
        """What the attribution rows sum to. Must equal ``total_pnl``."""
        return self.option_pnl + self.total_stock_pnl + self.dividends

    @property
    def reconciliation_gap(self) -> float:
        """Attribution minus the actual equity change.

        Non-zero means a cash flow escaped the attribution table, which would
        make every share below a lie. Surfaced in the report rather than
        silently absorbed.
        """
        return self.attribution_total - self.total_pnl

    @property
    def option_pnl_share(self) -> Optional[float]:
        """Fraction of gross P&L from the option leg.

        None when the two legs have opposite signs — the ratio is meaningless
        then (e.g. premium +$500 against stock -$5,000 is not "10% from options"),
        and reporting a number anyway is how attribution gets misread.
        """
        stock = self.total_stock_pnl
        gross = self.option_pnl + stock
        if gross == 0 or (self.option_pnl < 0) != (stock < 0):
            return None
        return self.option_pnl / gross

    @property
    def closed_cycles(self) -> List[WheelCycle]:
        return [c for c in self.cycles if not c.is_open]

    @property
    def win_rate(self) -> Optional[float]:
        """Fraction of closed cycles with positive total P&L.

        High by construction at our deltas — read it next to worst_cycle.
        """
        closed = self.closed_cycles
        if not closed:
            return None
        return sum(1 for c in closed if c.total_pnl > 0) / len(closed)

    @property
    def assignment_rate(self) -> Optional[float]:
        """Share of ALL cycles that took assignment, open ones included.

        Counting closed cycles only reported "0% assignment" for a run that
        finished holding assigned shares — irreconcilable, next to it, with
        "12 days holding shares below cost basis".
        """
        if not self.cycles:
            return None
        return sum(1 for c in self.cycles if c.assigned) / len(self.cycles)

    @property
    def days_in_position_fraction(self) -> float:
        """Fraction of decision days on which ANY position was held.

        Named for what it measures. It is a *time* occupancy, not a capital one:
        a symbol tying up 3% of the account every single day scores 1.0 here.
        Read it beside ``avg_collateral`` / ``return_on_collateral``, which are
        the capital-weighted views.

        Its job is to expose the symbol the strategy cannot trade at all. KMI
        managed one 8-day cycle in 273 days — +138% annualized on that cycle,
        +0.06% overall — which is not a performance result, it is an absence of
        one.
        """
        if not self.decision_days:
            return 0.0
        return self.days_in_position / self.decision_days

    @property
    def return_on_collateral(self) -> Optional[float]:
        """Total P&L over average collateral committed — the plan's headline.

        ``total_return`` divides by the whole account, so it is dominated by the
        arbitrary starting-cash choice: one contract against $100k dilutes any
        result by ~5x and makes the wheel structurally lose to buy-and-hold in a
        rising market regardless of merit. Return on the capital actually put at
        risk is the scale-free number.
        """
        if self.avg_collateral <= 0:
            return None
        return self.total_pnl / self.avg_collateral

    @property
    def annualized_return_on_collateral(self) -> Optional[float]:
        roc = self.return_on_collateral
        if roc is None or self.days <= 0:
            return None
        return roc * (365.0 / self.days)

    @property
    def decision_days(self) -> int:
        return int(self.data_quality.get("decision_days") or 0)

    @property
    def worst_cycle(self) -> Optional[WheelCycle]:
        closed = self.closed_cycles
        return min(closed, key=lambda c: c.total_pnl) if closed else None

    @property
    def excess_return(self) -> Optional[float]:
        if self.benchmark is None:
            return None
        return self.total_return - self.benchmark.total_return

    def verdict(self) -> str:
        """fit | marginal | unfit | insufficient.

        ``insufficient`` is deliberately NOT ``unfit``: it means the window did
        not contain enough completed cycles to judge, which implies lengthening
        the window or fixing a threshold — not dropping the symbol.
        """
        reasons = self.verdict_reasons()
        if any(r.startswith("INSUFFICIENT") for r in reasons):
            return "insufficient"
        if any(r.startswith("BLOCK") for r in reasons):
            return "unfit"
        if any(r.startswith("WARN") for r in reasons):
            return "marginal"
        return "fit"

    def verdict_reasons(self) -> List[str]:
        """Explicit, ordered reasons behind the verdict."""
        if not self.is_wheel:
            return self._covered_call_verdict_reasons()
        reasons: List[str] = []

        if not self.closed_cycles:
            # NOT a BLOCK: "no cycle closed" is a statement about the window,
            # not the symbol. A name that made money on every decision day was
            # being recommended for demotion because its cycle happened to
            # straddle the window edge. INSUFFICIENT keeps it out of the
            # demotion list while still refusing to call it fit.
            # These share a label but imply opposite actions: a symbol that
            # never opened a position is a config/data question (can it trade
            # at all?), while one that was deployed almost every day is a
            # window-length question (it just didn't round-trip in time).
            if self.days_in_position == 0:
                reasons.append(
                    "INSUFFICIENT: never opened a position in the window — "
                    "this is a question about whether the strategy CAN trade "
                    "this symbol (premium floor, delta band, gap filter), not "
                    "about how it performed"
                )
            else:
                reasons.append(
                    f"INSUFFICIENT: deployed on {self.days_in_position} of "
                    f"{self.decision_days} decision days but no cycle closed "
                    f"inside the window — lengthen the window to judge it; the "
                    f"return below excludes the open position's outcome"
                )
            return reasons

        # Check activity before performance: a return earned on 3% of the days
        # is not a verdict about the strategy, it is a verdict about whether the
        # strategy can trade this symbol at all.
        if self.decision_days and self.days_in_position_fraction < MIN_DAYS_IN_POSITION:
            reasons.append(
                f"BLOCK: held a position on only {self.days_in_position} of "
                f"{self.decision_days} decision days "
                f"({self.days_in_position_fraction:.0%}) — the strategy cannot "
                f"consistently trade this symbol, so the return below is not a "
                f"meaningful sample"
            )

        if self.total_pnl <= 0:
            reasons.append(f"BLOCK: strategy lost money ({self.total_return:+.2%})")

        # An absolute floor, not just a relative one. Beating buy-and-hold over a
        # short window is close to a coin flip, so without this a symbol can read
        # FIT on a result that loses to cash.
        aroc = self.annualized_return_on_collateral
        if aroc is not None and 0 < self.total_pnl and aroc < RISK_FREE_RATE:
            reasons.append(
                f"BLOCK: {aroc:+.2%} annualized on committed collateral is below "
                f"the {RISK_FREE_RATE:.1%} risk-free rate — the capital would "
                f"have earned more sitting in T-bills"
            )

        if self.excess_return is not None and self.excess_return < 0:
            reasons.append(
                f"WARN: underperformed buy-and-hold by {abs(self.excess_return):.2%}"
            )

        share = self.option_pnl_share
        if share is not None and share < 0.25:
            reasons.append(
                f"WARN: only {share:.0%} of gross P&L came from premium — "
                "this is mostly a long-stock position"
            )

        # Threshold set against what account equity can actually move. With
        # max_position_size 0.35 and one open position per symbol, a -20% gate
        # was arithmetically unreachable — inert, not conservative.
        if self.max_drawdown < MAX_DRAWDOWN_WARN:
            reasons.append(
                f"WARN: max account drawdown {self.max_drawdown:.1%} "
                f"(on peak collateral of ${self.peak_collateral:,.0f})"
            )

        # Both sides of this ratio must be decision days; self.days is calendar
        # days, which understated the fraction by ~30% and made the gate harder
        # to trip on the wheel's self-declared signature failure mode.
        if self.decision_days:
            underwater_fraction = self.days_underwater / self.decision_days
            if underwater_fraction > 0.30:
                reasons.append(
                    f"WARN: held shares below cost basis on {self.days_underwater} "
                    f"of {self.decision_days} decision days "
                    f"({underwater_fraction:.0%})"
                )

        if not reasons:
            reasons.append("OK: profitable, beat buy-and-hold, premium-driven")
        return reasons

    # ------------------------------------------------------------------ #
    # The covered-call verdict (FC-096 Phase C §C3)
    # ------------------------------------------------------------------ #
    def _covered_call_verdict_reasons(self) -> List[str]:
        """The CC branch, with its cutoffs — NOT the wheel's rules re-pointed.

        The wheel's first test is "did a cycle close", and on a covered-call
        replay that INVERTS: a lot that was never called away is the programme
        working perfectly, and the wheel branch would have stamped the best
        outcome ``insufficient`` on every symbol that held its shares. So the
        CC branch never asks that question. Its ``insufficient`` means what the
        word should mean here — the window or the data could not support a
        judgement:

        * fewer decision days than a single call tenor, so not one contract
          could have been written and resolved; or
        * no lot was ever seeded (no traded session to seed at).

        Everything below that is a measurement:

        * BLOCK on net premium <= 0 — a covered-call programme that paid to
          write calls did the one thing it exists not to do.
        * BLOCK on coverage below 30% of the days it COULD have written,
          excluding hold-uncovered and earnings-span days. Standing down below
          basis is the floor working; the exclusion is the point.
        * WARN when the premium yield on the lot trails the same lot's
          dividend-inclusive buy-and-hold. The comparison is against the LOT,
          never a $100k full-invest benchmark.
        * WARN when the shares were called away below basis — the assignment
          terms, which is the loss a CC programme actually fears.
        """
        from ..engine.simulator import COVERAGE_COVERED

        reasons: List[str] = []
        tenor = int(self.data_quality.get("call_target_dte") or 0)
        decision_days = self.decision_days

        if self.synthetic_lots_opened <= 0:
            return ["INSUFFICIENT: no synthetic lot was ever seeded — the "
                    "symbol had no traded session in the window to assume a "
                    "position at, so nothing was measured"]
        if tenor and decision_days and decision_days < tenor:
            return [
                f"INSUFFICIENT: {decision_days} decision days is shorter than "
                f"one {tenor}-day call tenor, so not a single contract could "
                f"have been written and resolved inside the window — lengthen "
                f"it to judge this symbol"
            ]

        coverage = self.coverage_fraction
        if coverage is None:
            reasons.append(
                "INSUFFICIENT: every day in the window was a stand-down the "
                "strategy is supposed to take (below basis, or inside an "
                "earnings span), so there is no day on which its selection was "
                "actually tested")
            return reasons

        if self.net_premium <= 0:
            reasons.append(
                f"BLOCK: net premium was ${self.net_premium:,.2f} — the "
                f"programme paid to write calls rather than being paid to")

        if coverage < MIN_COVERED_FRACTION:
            covered_days = (self.coverage_by_reason or {}).get(COVERAGE_COVERED, 0)
            reasons.append(
                f"BLOCK: a call was open on only {covered_days} of the days the "
                f"strategy could have written one ({coverage:.0%}, excluding "
                f"below-basis and earnings-span stand-downs) — it cannot "
                f"consistently find a contract on this symbol, so the yield "
                f"below is not a meaningful sample")

        yield_on_lot = self.premium_yield_on_lot
        bench = self.benchmark.total_return if self.benchmark else None
        if yield_on_lot is not None and bench is not None:
            bench_annualized = bench * (365.0 / self.days) if self.days > 0 else 0.0
            if yield_on_lot < bench_annualized:
                reasons.append(
                    f"WARN: {yield_on_lot:+.2%} annualized premium yield on the "
                    f"lot trails the SAME lot's buy-and-hold "
                    f"({bench_annualized:+.2%}) — holding the shares and "
                    f"writing nothing would have done better")

        called_below = [
            c for c in self.cycles
            if c.called_away and c.cost_basis is not None
            and c.exit_price is not None and c.exit_price < c.cost_basis
        ]
        if called_below:
            reasons.append(
                f"WARN: {len(called_below)} lot(s) were called away BELOW their "
                f"cost basis — the assignment terms, not the premium, are where "
                f"this programme lost money")

        if self.max_drawdown < MAX_DRAWDOWN_WARN:
            reasons.append(
                f"WARN: max account drawdown {self.max_drawdown:.1%}")

        if not reasons:
            reasons.append(
                f"OK: net premium ${self.net_premium:,.2f} at "
                f"{(yield_on_lot or 0.0):+.2%} annualized on the lot, covered on "
                f"{coverage:.0%} of writable days")
        return reasons


def compute_fitness(
    symbol: str,
    daily: Sequence[DailyState],
    cycles: Sequence[WheelCycle],
    starting_cash: float,
    *,
    benchmark_prices: Optional[Dict[date, float]] = None,
    benchmark_dividends_per_share: float = 0.0,
    data_quality: Optional[Dict] = None,
    rolls: int = 0,
    result: Optional[Any] = None,
) -> FitnessReport:
    """Assemble the scorecard from the equity curve and the cycle table.

    ``result`` is the replay's ``SimulationResult`` (FC-096 Phase C). Optional
    so every existing caller and test keeps working unchanged and a wheel report
    built without it is byte-identical; when it IS passed, the covered-call
    fields come off it rather than being recomputed here from the ledger, so the
    row, the report and the artifact cannot disagree about which lot value the
    engine used.
    """
    if not daily:
        raise ValueError("cannot compute fitness with an empty equity curve")

    report = FitnessReport(
        symbol=symbol,
        start=daily[0].day,
        end=daily[-1].day,
        starting_cash=starting_cash,
        final_equity=daily[-1].equity,
        cycles=list(cycles),
        rolls=rolls,
        data_quality=dict(data_quality or {}),
    )
    if result is not None:
        report.strategy = getattr(result, "strategy", "wheel")
        report.lot_capital = float(getattr(result, "time_weighted_lot_value", 0.0) or 0.0)
        report.coverage_by_reason = dict(
            getattr(result, "coverage_by_reason", None) or {})
        report.calls_closed_early = int(getattr(result, "calls_closed_early", 0) or 0)
        report.synthetic_lots_opened = int(
            getattr(result, "synthetic_lots_opened", 0) or 0)
        report.itm_rolls = int(getattr(result, "itm_rolls", 0) or 0)
        report.otm_roll_outs = int(getattr(result, "otm_roll_outs", 0) or 0)
    # Σ of the seeded lot values, straight off the ledger events rather than
    # off a counter: the events ARE the record of what entered equity without
    # passing through cash, so the numerator's correction cannot drift from
    # what the broker actually did.
    report.lot_capital_injected = sum(
        event.price * event.shares
        for cycle in cycles for event in cycle.events
        if event.kind == "synthetic_lot_open"
    )

    for cycle in cycles:
        report.option_pnl += cycle.option_pnl
        report.stock_pnl += cycle.stock_pnl
        report.dividends += cycle.dividends
        report.fees += cycle.fees
        report.puts_sold += cycle.puts_sold
        report.calls_sold += cycle.calls_sold
        report.assignments += 1 if cycle.assigned else 0

    report.days_in_position = sum(
        1 for d in daily if d.open_options > 0 or any(v > 0 for v in d.shares_held.values())
    )
    report.unrealized_stock_pnl = _unrealized_stock_pnl(daily, cycles, benchmark_prices or {})
    collaterals = [d.reserved_collateral for d in daily]
    deployed = [c for c in collaterals if c > 0]
    report.avg_collateral = sum(deployed) / len(deployed) if deployed else 0.0
    report.peak_collateral = max(collaterals) if collaterals else 0.0

    equity = [d.equity for d in daily]
    report.max_drawdown = _max_drawdown(equity)
    report.sharpe, report.sortino = _risk_ratios(equity)
    report.days_underwater = _days_underwater(daily, cycles, benchmark_prices or {})

    if benchmark_prices:
        # FC-096 Phase C §C3, the lot-sized benchmark. A covered-call replay is
        # compared against THE SAME LOT held and not written against — 100
        # shares, entered at the seeding close — not against a $100k
        # full-investment buy-and-hold that would buy ~30x the shares. With the
        # wrong benchmark `excess_return` compares two different investments and
        # the verdict layer reads the size difference as skill.
        #
        # `_buy_and_hold` derives the share count from the cash it is handed
        # (`int(cash // entry)`), so passing the FIRST lot's value yields
        # exactly that lot's share count at exactly its entry price.
        bench_cash = starting_cash
        if not report.is_wheel and report.lot_capital_injected > 0:
            first_seed = next(
                (event for cycle in cycles for event in cycle.events
                 if event.kind == "synthetic_lot_open"), None)
            if first_seed is not None:
                bench_cash = first_seed.price * first_seed.shares
        report.benchmark = _buy_and_hold(
            daily, benchmark_prices, bench_cash, benchmark_dividends_per_share
        )

    return report


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough decline, as a negative fraction."""
    peak, worst = -math.inf, 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, (value - peak) / peak)
    # Collapse -0.0 (and sub-basis-point noise) to 0.0 so reports don't print
    # "-0.00%", which reads as a real but tiny loss rather than none at all.
    return 0.0 if worst > -1e-9 else worst


def _risk_ratios(equity: Sequence[float]) -> tuple:
    """Annualized Sharpe and Sortino from the daily equity curve (rf = 0)."""
    if len(equity) < 3:
        return 0.0, 0.0
    returns = [
        (equity[i] - equity[i - 1]) / equity[i - 1]
        for i in range(1, len(equity))
        if equity[i - 1] > 0
    ]
    if len(returns) < 2:
        return 0.0, 0.0

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    stdev = math.sqrt(variance)
    scale = math.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = (mean / stdev) * scale if stdev > 0 else 0.0

    downside = [r for r in returns if r < 0]
    if downside:
        dvar = sum(r * r for r in downside) / len(downside)
        dstd = math.sqrt(dvar)
        sortino = (mean / dstd) * scale if dstd > 0 else 0.0
    else:
        sortino = 0.0
    return sharpe, sortino


def _days_underwater(
    daily: Sequence[DailyState],
    cycles: Sequence[WheelCycle],
    prices: Dict[date, float],
) -> int:
    """Days holding assigned shares while the price sits below cost basis.

    The wheel's signature failure mode: assignment converts a premium trade into
    a long position that can sit underwater for months, unable to sell calls
    above basis. AMZN's 62-day drawdown pause is the live existence proof.
    """
    if not prices:
        return 0
    # Keyed by (underlying, day), not day: a multi-symbol run holding two
    # assigned names on the same date would otherwise have one basis silently
    # overwrite the other. `prices` is single-symbol today, so only days where
    # that symbol is held can count.
    underwater_days = set()
    for cycle in cycles:
        if cycle.cost_basis is None:
            continue
        stop = cycle.end or daily[-1].day
        for state in daily:
            if not (cycle.start <= state.day <= stop):
                continue
            if state.shares_held.get(cycle.underlying, 0) <= 0:
                continue
            price = prices.get(state.day)
            if price is not None and price < cycle.cost_basis:
                underwater_days.add((cycle.underlying, state.day))

    return len(underwater_days)


def _unrealized_stock_pnl(
    daily: Sequence[DailyState],
    cycles: Sequence[WheelCycle],
    prices: Dict[date, float],
) -> float:
    """Mark still-held assigned shares to the final close.

    A cycle open at the end of the window holds real shares whose gain or loss
    is already inside ``final_equity``. Leaving it out of attribution makes the
    rows disagree with the headline return — and understates the stock leg
    exactly when it matters most.
    """
    if not prices or not daily:
        return 0.0
    final_day = daily[-1].day
    final_prices = prices
    total = 0.0
    for cycle in cycles:
        if not cycle.is_open or cycle.cost_basis is None:
            continue
        shares = daily[-1].shares_held.get(cycle.underlying, 0)
        if shares <= 0:
            continue
        price = final_prices.get(final_day)
        if price is None:
            continue
        total += (price - cycle.cost_basis) * shares
    return total


def _buy_and_hold(
    daily: Sequence[DailyState],
    prices: Dict[date, float],
    starting_cash: float,
    dividends_per_share: float = 0.0,
) -> Optional[BuyAndHold]:
    """Whole shares bought at the first close, held to the last, dividends kept.

    ``dividends_per_share`` must already be scoped to the holding period the
    caller is modelling — see ``DividendSchedule.total_between``, which excludes
    an ex-date falling on the entry day (a buyer at that close does not receive
    it) and includes one falling on the exit day.
    """
    entry_day, exit_day = daily[0].day, daily[-1].day
    entry = prices.get(entry_day)
    exit_ = prices.get(exit_day)
    if not entry or not exit_ or entry <= 0:
        return None
    shares = int(starting_cash // entry)
    if shares <= 0:
        return None
    return BuyAndHold(
        shares=shares,
        entry_price=entry,
        exit_price=exit_,
        starting_cash=starting_cash,
        dividends_per_share=dividends_per_share,
    )
