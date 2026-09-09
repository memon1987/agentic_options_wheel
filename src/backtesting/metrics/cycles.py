"""Per-wheel-cycle analytics, reconstructed from the broker ledger.

A *cycle* is one full turn of the wheel on one underlying: it opens when the
first option is sold while flat in that name, and closes when the name returns to
flat — no shares, no open options. In between it may contain several puts
(expired worthless, re-sold), an assignment, a run of covered calls, rolls, and
dividends.

Everything here is arithmetic over ``BacktestBroker.ledger``. Nothing is
re-derived from strategy logic, so a cycle table cannot disagree with the cash
that actually moved.

The separation that matters most is **option P&L vs stock P&L**. Published wheel
studies find ~94-99% of total return comes from the stock leg; a report that
blends them hides whether the strategy is earning premium or just being long. So
each cycle carries both, and they are never summed into a single number without
also being shown apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional

from ..engine.broker import LedgerEvent

# Ledger kinds that move option premium.
_OPTION_KINDS = {"sell_put_open", "sell_call_open", "buy_to_close"}
# Ledger kinds that move shares. `synthetic_lot_open` is the covered-call
# replay's seeding event (FC-096 Phase C): it moves shares with no cash, so it
# belongs here and nowhere near the option kinds.
_STOCK_KINDS = {"put_assignment", "call_assignment", "synthetic_lot_open"}


@dataclass
class WheelCycle:
    """One complete turn of the wheel on one underlying."""

    underlying: str
    start: date
    end: Optional[date] = None  # None while still open at the end of the run

    puts_sold: int = 0
    calls_sold: int = 0
    contracts_closed: int = 0
    rolls: int = 0

    option_pnl: float = 0.0  # premium received net of buybacks and fees
    stock_pnl: float = 0.0  # realized only: proceeds - cost basis when called away
    dividends: float = 0.0
    fees: float = 0.0

    assigned: bool = False
    called_away: bool = False
    #: Synthetic lots seeded into this cycle (FC-096 Phase C). Deliberately NOT
    #: folded into ``assigned``: a seeded lot is an assumption of the
    #: measurement, and counting it as an assignment would put the covered-call
    #: replay's ``assignment_rate`` at 100% on every symbol while saying nothing
    #: about how often a put was actually put to it.
    synthetic_lots: int = 0
    shares_acquired: int = 0
    cost_basis: Optional[float] = None
    exit_price: Optional[float] = None
    max_collateral: float = 0.0

    events: List[LedgerEvent] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def is_open(self) -> bool:
        return self.end is None

    @property
    def days(self) -> int:
        """Calendar days the cycle was live (0 for a same-day open/close)."""
        if self.end is None:
            return 0
        return (self.end - self.start).days

    @property
    def total_pnl(self) -> float:
        return self.option_pnl + self.stock_pnl + self.dividends

    @property
    def capital_at_risk(self) -> float:
        """Peak collateral committed — the denominator for return on capital."""
        return self.max_collateral

    @property
    def return_on_capital(self) -> float:
        if self.capital_at_risk <= 0:
            return 0.0
        return self.total_pnl / self.capital_at_risk

    @property
    def annualized_return(self) -> float:
        """Return on capital scaled to a year. 0 for cycles shorter than a day.

        Deliberately linear rather than compounded: a 3-day cycle compounded to a
        year produces a headline number that says more about the exponent than
        the strategy.
        """
        if self.days <= 0 or self.capital_at_risk <= 0:
            return 0.0
        return self.return_on_capital * (365.0 / self.days)

    def outcome(self) -> str:
        """One-word description of how the cycle ended."""
        if self.is_open:
            return "open"
        if self.called_away:
            return "called_away"
        if self.assigned:
            return "holding_shares"  # closed the run still long (should not happen)
        return "expired_worthless"


def build_cycles(ledger: Iterable[LedgerEvent]) -> List[WheelCycle]:
    """Reconstruct wheel cycles per underlying from an ordered ledger.

    Args:
        ledger: broker events, chronological.

    Returns:
        Cycles in start order. A cycle still open at the end of the run is
        returned with ``end=None`` rather than silently dropped — an unfinished
        cycle is a real state (shares still held) and hiding it would overstate
        how cleanly the strategy round-trips.
    """
    open_cycles: Dict[str, WheelCycle] = {}
    finished: List[WheelCycle] = []
    # Live position state per underlying, so we know when a name returns to flat.
    open_contracts: Dict[str, int] = {}
    shares: Dict[str, int] = {}

    for event in ledger:
        underlying = event.underlying
        if not underlying:
            continue

        cycle = open_cycles.get(underlying)
        if cycle is None:
            cycle = WheelCycle(underlying=underlying, start=event.event_date)
            open_cycles[underlying] = cycle
        cycle.events.append(event)
        cycle.fees += event.fees

        kind = event.kind
        if kind == "sell_put_open":
            cycle.puts_sold += 1
            cycle.option_pnl += event.cash_delta
            open_contracts[underlying] = open_contracts.get(underlying, 0) + event.contracts
            collateral = float(event.detail.get("collateral", 0.0))
            cycle.max_collateral = max(cycle.max_collateral, collateral)
        elif kind == "sell_call_open":
            cycle.calls_sold += 1
            cycle.option_pnl += event.cash_delta
            open_contracts[underlying] = open_contracts.get(underlying, 0) + event.contracts
        elif kind == "buy_to_close":
            cycle.contracts_closed += 1
            cycle.option_pnl += event.cash_delta
            open_contracts[underlying] = open_contracts.get(underlying, 0) - event.contracts
        elif kind == "expire_worthless":
            open_contracts[underlying] = open_contracts.get(underlying, 0) - event.contracts
        elif kind == "synthetic_lot_open":
            # The covered-call premise, booked exactly as an assignment books a
            # lot -- share-weighted basis, share count, cycle left OPEN -- and
            # differing in the two ways that matter: no contracts are consumed
            # (nothing was assigned) and `assigned` stays False.
            #
            # Recording it is what makes the rest of the cycle table true for a
            # CC replay: `cost_basis` is the seeding close, so `call_assignment`
            # below books REAL stock P&L against it instead of skipping the
            # branch on a None basis; and because shares are now held, the
            # flat-test at the bottom leaves the cycle open until the lot is
            # called away. Under the signed re-seed posture the stock leg is a
            # CHAIN of lots: call-away closes the cycle, the next session's
            # seeding opens a fresh one, and each lot shows in the table with
            # its own basis and its own `synthetic_lot_open`.
            cycle.synthetic_lots += 1
            prior_shares = cycle.shares_acquired
            prior_basis = cycle.cost_basis or 0.0
            cycle.shares_acquired = prior_shares + event.shares
            cycle.cost_basis = (
                (prior_basis * prior_shares + event.price * event.shares)
                / cycle.shares_acquired
            ) if cycle.shares_acquired else event.price
            shares[underlying] = shares.get(underlying, 0) + event.shares
        elif kind == "put_assignment":
            cycle.assigned = True
            # Share-weighted, not last-wins. Two assignments at different
            # strikes previously kept only the last basis and applied it to
            # every called-away share: assigned 100@100 then 100@90, called
            # away 200@105 reported $3,000 of stock P&L against a true $2,000
            # — a 50% overstatement of the stock leg.
            prior_shares = cycle.shares_acquired
            prior_basis = cycle.cost_basis or 0.0
            cycle.shares_acquired = prior_shares + event.shares
            cycle.cost_basis = (
                (prior_basis * prior_shares + event.price * event.shares)
                / cycle.shares_acquired
            ) if cycle.shares_acquired else event.price
            open_contracts[underlying] = open_contracts.get(underlying, 0) - event.contracts
            shares[underlying] = shares.get(underlying, 0) + event.shares
        elif kind == "call_assignment":
            cycle.called_away = True
            cycle.exit_price = event.price
            # Realized stock P&L: proceeds at strike minus the weighted basis.
            if cycle.cost_basis is not None:
                cycle.stock_pnl += (event.price - cycle.cost_basis) * event.shares
                # Disposal consumes shares, so a later assignment re-weights
                # against what actually remains.
                cycle.shares_acquired = max(0, cycle.shares_acquired - event.shares)
            open_contracts[underlying] = open_contracts.get(underlying, 0) - event.contracts
            shares[underlying] = shares.get(underlying, 0) - event.shares
        elif kind == "dividend":
            cycle.dividends += event.cash_delta

        # A cycle closes when the name is flat: no shares, no open contracts.
        if open_contracts.get(underlying, 0) <= 0 and shares.get(underlying, 0) <= 0:
            cycle.end = event.event_date
            finished.append(cycle)
            del open_cycles[underlying]
            open_contracts[underlying] = 0
            shares[underlying] = 0

    # Cycles still running at the end of the window stay open, reported as such.
    finished.extend(open_cycles.values())
    finished.sort(key=lambda c: (c.start, c.underlying))
    return finished


def count_rolls(cycles: Iterable[WheelCycle]) -> int:
    """A roll is a same-day buy-to-close plus call-sell on one underlying."""
    total = 0
    for cycle in cycles:
        by_day: Dict[date, set] = {}
        for e in cycle.events:
            by_day.setdefault(e.event_date, set()).add(e.kind)
        total += sum(
            1 for kinds in by_day.values()
            if "buy_to_close" in kinds and "sell_call_open" in kinds
        )
    return total
