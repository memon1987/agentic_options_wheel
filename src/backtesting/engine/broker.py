"""Backtest broker — the authoritative cash-and-positions ledger.

This is the only place money and shares move. It is deliberately separate from
``WheelStateManager`` (which tracks *strategy phase*): the broker knows nothing
about the wheel, only about cash, collateral, stock lots, short option
positions, fills, fees, assignment, and dividends. The old engine's fictional
2:1 buying power and parallel bookkeeping are gone — cash-secured means exactly
that, and available cash excludes reserved collateral.

Conventions:
  * The wheel only ever *sells* options (short puts, short calls) and buys them
    back to close, so every OptionPosition is short (``contracts`` > 0).
  * Cash-secured puts reserve ``strike * 100 * contracts`` from cash; covered
    calls reserve nothing (the shares are the cover).
  * Fills interpolate between mark (mid) and the near side of the modeled
    spread by ``fill_haircut``: selling fills toward the bid, buying toward the
    ask. haircut=0 fills at mid (optimistic); haircut=1 fills at the far quote
    (worst case). Default 0.25 per the plan.
  * Assignment at expiration follows OCC exercise-by-exception: a short is
    assigned iff it is in the money by >= $0.01 versus the underlying close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

ITM_THRESHOLD = 0.01  # OCC auto-exercise: in the money by >= 1 cent


@dataclass
class StockLot:
    """A lot of shares acquired at one cost basis (FIFO for disposal)."""

    underlying: str
    shares: int
    cost_basis: float  # per share
    acquired: date


@dataclass
class OptionPosition:
    """A short option position."""

    symbol: str  # OCC
    underlying: str
    option_type: str  # 'put' | 'call'
    strike: float
    expiration: date
    contracts: int  # number of short contracts (> 0)
    entry_price: float  # premium per share received at open
    collateral: float  # cash reserved (puts) or 0 (covered calls)
    opened: date


@dataclass
class LedgerEvent:
    """One money/position event, for audit and metrics."""

    event_date: date
    kind: str
    underlying: str
    symbol: str = ""
    contracts: int = 0
    shares: int = 0
    price: float = 0.0
    cash_delta: float = 0.0
    fees: float = 0.0
    detail: dict = field(default_factory=dict)


class BacktestBroker:
    """Cash + positions ledger with wheel-relevant settlement rules."""

    def __init__(
        self,
        starting_cash: float,
        *,
        fees_per_contract: float = 0.04,
        fill_haircut: float = 0.25,
    ) -> None:
        """
        Args:
            starting_cash: initial account cash.
            fees_per_contract: regulatory/clearing pass-through per contract
                (commission is $0 at Alpaca). Applied on open and close.
            fill_haircut: 0..1 fraction from mid toward the near quote side.
        """
        self.cash = float(starting_cash)
        self.fees_per_contract = fees_per_contract
        self.fill_haircut = fill_haircut
        self.stock_lots: Dict[str, List[StockLot]] = {}
        self.options: Dict[str, OptionPosition] = {}
        self.ledger: List[LedgerEvent] = []

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    @property
    def reserved_collateral(self) -> float:
        return sum(p.collateral for p in self.options.values())

    @property
    def available_cash(self) -> float:
        """Cash not tied up as cash-secured-put collateral."""
        return self.cash - self.reserved_collateral

    def shares(self, underlying: str) -> int:
        return sum(lot.shares for lot in self.stock_lots.get(underlying, []))

    def pledged_shares(self, underlying: str) -> int:
        """Shares already committed as cover for open short calls."""
        return sum(
            100 * p.contracts
            for p in self.options.values()
            if p.underlying == underlying and p.option_type == "call"
        )

    def uncovered_shares(self, underlying: str) -> int:
        """Shares free to back a new covered call."""
        return max(0, self.shares(underlying) - self.pledged_shares(underlying))

    def average_cost_basis(self, underlying: str) -> Optional[float]:
        lots = self.stock_lots.get(underlying, [])
        total = sum(lot.shares for lot in lots)
        if total == 0:
            return None
        return sum(lot.shares * lot.cost_basis for lot in lots) / total

    # ------------------------------------------------------------------ #
    # Fill pricing
    # ------------------------------------------------------------------ #
    def sell_fill(self, mark: float, bid: float) -> float:
        """Price received when selling: mark, haircut-adjusted toward the bid."""
        return max(0.0, mark - self.fill_haircut * (mark - bid))

    def buy_fill(self, mark: float, ask: float) -> float:
        """Price paid when buying: mark, haircut-adjusted toward the ask."""
        return mark + self.fill_haircut * (ask - mark)

    # ------------------------------------------------------------------ #
    # Opening positions
    # ------------------------------------------------------------------ #
    def sell_put_to_open(
        self,
        symbol: str,
        underlying: str,
        strike: float,
        expiration: date,
        contracts: int,
        mark: float,
        bid: float,
        opened: date,
    ) -> Optional[float]:
        """Sell-to-open a cash-secured put. Returns fill price/share, or None if
        collateral can't be covered by available cash."""
        collateral = strike * 100 * contracts
        if collateral > self.available_cash + 1e-9:
            return None
        fill = self.sell_fill(mark, bid)
        premium = fill * 100 * contracts
        fees = self.fees_per_contract * contracts
        self.cash += premium - fees
        self._add_option(
            symbol, underlying, "put", strike, expiration, contracts, fill, collateral, opened
        )
        self._record(
            "sell_put_open", underlying, symbol, contracts=contracts, price=fill,
            cash_delta=premium - fees, fees=fees, event_date=opened,
            detail={"collateral": collateral},
        )
        return fill

    def sell_call_to_open(
        self,
        symbol: str,
        underlying: str,
        strike: float,
        expiration: date,
        contracts: int,
        mark: float,
        bid: float,
        opened: date,
    ) -> Optional[float]:
        """Sell-to-open a covered call. Returns fill/share, or None if there are
        not enough UNPLEDGED shares to cover it.

        Counting total shares is not enough: shares backing an already-open
        short call are spoken for. Ignoring that let the wheel write a second
        call against the same 100 shares — a naked call in a cash account, which
        the live broker rejects outright.
        """
        if self.uncovered_shares(underlying) < 100 * contracts:
            return None
        fill = self.sell_fill(mark, bid)
        premium = fill * 100 * contracts
        fees = self.fees_per_contract * contracts
        self.cash += premium - fees
        self._add_option(
            symbol, underlying, "call", strike, expiration, contracts, fill, 0.0, opened
        )
        self._record(
            "sell_call_open", underlying, symbol, contracts=contracts, price=fill,
            cash_delta=premium - fees, fees=fees, event_date=opened,
        )
        return fill

    # ------------------------------------------------------------------ #
    # Closing positions
    # ------------------------------------------------------------------ #
    def buy_to_close(
        self,
        symbol: str,
        contracts: int,
        mark: float,
        ask: float,
        close_date: date,
    ) -> Optional[float]:
        """Buy-to-close ``contracts`` of a short option.

        Returns the fill per share, or None if the position doesn't exist, has
        too few contracts, or the debit exceeds available cash.

        The cash check matters most in exactly the case rolling exists for: when
        a covered call goes deep ITM the buy-to-close leg is expensive, and cash
        is tight because it went into the assigned shares. Without the check the
        simulated account takes an interest-free margin loan and completes a
        roll a real cash account would have had rejected — letting the sim
        escape losing covered calls that the live bot cannot.

        Collateral released by this close is counted as available, since the two
        settle together; only a genuine shortfall rejects.
        """
        pos = self.options.get(symbol)
        if pos is None or contracts > pos.contracts:
            return None
        fill = self.buy_fill(mark, ask)
        cost = fill * 100 * contracts
        fees = self.fees_per_contract * contracts

        freed = (
            (pos.collateral / pos.contracts) * contracts
            if pos.option_type == "put" else 0.0
        )
        if cost + fees > self.available_cash + freed + 1e-9:
            logger.info(
                "buy-to-close rejected: insufficient cash",
                symbol=symbol, cost=round(cost + fees, 2),
                available=round(self.available_cash + freed, 2),
            )
            return None

        self.cash -= cost + fees
        released = 0.0
        if pos.option_type == "put":
            released = (pos.collateral / pos.contracts) * contracts
        self._reduce_option(symbol, contracts)
        self._record(
            "buy_to_close", pos.underlying, symbol, contracts=contracts, price=fill,
            cash_delta=-(cost + fees), fees=fees, event_date=close_date,
            detail={"collateral_released": released},
        )
        return fill

    # ------------------------------------------------------------------ #
    # Settlement
    # ------------------------------------------------------------------ #
    def settle_expirations(self, on_date: date, underlying_close: Dict[str, float]) -> None:
        """Settle every short option expiring on ``on_date`` against the day's
        underlying closes (OCC exercise-by-exception)."""
        expiring = [p for p in self.options.values() if p.expiration == on_date]
        for pos in expiring:
            close = underlying_close.get(pos.underlying)
            if close is None:
                # No settlement price: conservatively leave the position; the
                # caller should ensure a close exists for every underlying held.
                logger.warning(
                    "no settlement price at expiry", symbol=pos.symbol, date=str(on_date)
                )
                continue
            if pos.option_type == "put":
                itm = (pos.strike - close) >= ITM_THRESHOLD
                if itm:
                    self._assign_put(pos, on_date)
                else:
                    self._expire_worthless(pos, on_date)
            else:  # call
                itm = (close - pos.strike) >= ITM_THRESHOLD
                if itm:
                    self._call_away(pos, on_date, reason="expiration")
                else:
                    self._expire_worthless(pos, on_date)

    def assign_call_early(self, symbol: str, on_date: date, reason: str = "ex_dividend") -> bool:
        """Force early assignment of a short call (e.g. day before ex-dividend
        when extrinsic < dividend). Returns True if a position was assigned."""
        pos = self.options.get(symbol)
        if pos is None or pos.option_type != "call":
            return False
        self._call_away(pos, on_date, reason=reason)
        return True

    def credit_dividend(self, underlying: str, per_share: float, on_date: date) -> float:
        """Credit a cash dividend on held shares. Returns the amount credited."""
        held = self.shares(underlying)
        amount = held * per_share
        if amount:
            self.cash += amount
            self._record(
                "dividend", underlying, shares=held, price=per_share,
                cash_delta=amount, event_date=on_date,
            )
        return amount

    # ------------------------------------------------------------------ #
    # Valuation
    # ------------------------------------------------------------------ #
    def equity(
        self,
        stock_prices: Dict[str, float],
        option_marks: Optional[Dict[str, float]] = None,
    ) -> float:
        """Total account equity: cash + long stock value − short option liability.

        Short options are a liability marked at ``option_marks`` (per share). A
        short with no supplied mark is valued at 0 (assumed near-worthless);
        callers that want an exact NLV should supply marks for open shorts.
        """
        option_marks = option_marks or {}
        equity = self.cash
        for underlying, lots in self.stock_lots.items():
            px = stock_prices.get(underlying)
            if px is not None:
                equity += sum(lot.shares for lot in lots) * px
        for sym, pos in self.options.items():
            mark = option_marks.get(sym, 0.0)
            equity -= mark * 100 * pos.contracts
        return equity

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _add_option(self, symbol, underlying, option_type, strike, expiration, contracts, entry, collateral, opened):
        if symbol in self.options:
            # Adding to an existing short (same contract): weight the entry price.
            p = self.options[symbol]
            total = p.contracts + contracts
            p.entry_price = (p.entry_price * p.contracts + entry * contracts) / total
            p.contracts = total
            p.collateral += collateral
        else:
            self.options[symbol] = OptionPosition(
                symbol, underlying, option_type, strike, expiration, contracts, entry, collateral, opened
            )

    def deposit_shares(self, underlying: str, shares: int, cost_basis: float,
                       on_date: date, *, premise: str) -> None:
        """Open a SYNTHETIC stock lot: shares appear, no cash moves (FC-096 C2).

        The covered-call replay measures what an overwriting programme earns on
        a lot it is HANDED, not on one it bought. There is no purchase to model:
        the lot is the premise of the measurement, so the ledger records its
        arrival with ``cash_delta = 0`` and says, in ``detail['premise']``, that
        it was assumed rather than acquired.

        **Zero cash is only honest alongside a strategy-aware capital base.** A
        lot that costs nothing and is then marked to market makes the whole lot
        value read as profit in ``final_equity`` — the C3 defect. The zero delta
        stands here (a purchase the programme never made must not appear in the
        cash ledger) and ``metrics.fitness`` divides by
        ``starting_cash + Σ seeded lot values`` so the lot can never be counted
        as a return. The two halves are one decision; neither is safe alone.

        The lot itself is an ordinary ``StockLot`` at a REAL basis, which is what
        makes the rest of the machine work unchanged: ``average_cost_basis`` — and
        therefore ``alpaca_adapter.get_positions``' ``avg_entry_price`` — answers
        with a positive number, so the covered-call floor binds and the roller's
        ``CostBasisResolver`` resolves instead of failing closed on
        ``no_broker_basis`` (FC-100 §Phase C hand-off, row 2).

        Args:
            underlying: the equity ticker.
            shares: share count (100 per contract of writable cover).
            cost_basis: per-share basis, i.e. the seeding close.
            on_date: the session whose close ``cost_basis`` is.
            premise: the human sentence describing the assumption, stamped on
                the event so a stored ledger read months later carries it.
        """
        if shares <= 0:
            raise ValueError(f"cannot deposit {shares} shares of {underlying}")
        if cost_basis <= 0:
            # A zero or negative basis silently disables the covered-call floor
            # (`find_suitable_calls` warns and continues on a non-positive
            # min_strike_price) — the same trap `_assign_put` guards.
            raise ValueError(
                f"cannot deposit a {underlying} lot at a non-positive basis "
                f"({cost_basis}): the covered-call floor would be inert.")
        self._add_stock(underlying, shares, float(cost_basis), on_date)
        self._record(
            "synthetic_lot_open", underlying, contracts=0, shares=shares,
            price=float(cost_basis), cash_delta=0.0, event_date=on_date,
            detail={"premise": premise, "shares": shares,
                    "cost_basis": float(cost_basis),
                    "lot_value": round(float(cost_basis) * shares, 2)},
        )

    def _reduce_option(self, symbol: str, contracts: int) -> None:
        pos = self.options[symbol]
        if contracts >= pos.contracts:
            del self.options[symbol]
        else:
            frac_remaining = (pos.contracts - contracts) / pos.contracts
            pos.collateral *= frac_remaining
            pos.contracts -= contracts

    def _add_stock(self, underlying: str, shares: int, cost_basis: float, on_date: date) -> None:
        self.stock_lots.setdefault(underlying, []).append(
            StockLot(underlying, shares, cost_basis, on_date)
        )

    def _remove_shares_fifo(self, underlying: str, shares: int) -> None:
        """Dispose of ``shares`` FIFO across lots."""
        lots = self.stock_lots.get(underlying, [])
        remaining = shares
        while remaining > 0 and lots:
            lot = lots[0]
            if lot.shares <= remaining:
                remaining -= lot.shares
                lots.pop(0)
            else:
                lot.shares -= remaining
                remaining = 0
        if remaining > 0:
            # Never silently dispose of shares that do not exist. Doing so
            # credits proceeds for them and mints cash out of nothing: two
            # covered calls written against 100 shares settled for 200 and
            # produced $10,595 of phantom equity. A ledger that can invent
            # money is worse than one that stops.
            raise ValueError(
                f"cannot dispose {shares} shares of {underlying}: {remaining} "
                f"more than held. The cover check that should have prevented "
                f"this short call is broken."
            )
        if not lots:
            self.stock_lots.pop(underlying, None)

    def _assign_put(self, pos: OptionPosition, on_date: date) -> None:
        """Short put assigned: buy shares at strike, release collateral.

        **Two different numbers, deliberately (FC-068).**

        The *lot basis* is premium-netted — ``strike − the assigning put's
        premium per share`` — because that is what Alpaca's ``avg_entry_price``
        reports, verified to the penny on all four live lots (FC-065 Phase 1),
        and ``avg_entry_price`` is what the covered-call floor reads. Booking
        the lot at the bare strike left the simulated floor one put premium
        ABOVE production's: on IWM's $1 strike grid that is a full rung, so
        every IWM backtest gated calls one strike tighter than production
        would. Multi-lot weighting falls out of the existing lot list and
        ``average_cost_basis``, which is Alpaca's weighted-average semantics
        exactly.

        The *ledger event* is unchanged: ``price = strike``, ``cash_delta =
        −strike × shares``. Downstream attribution is built on that convention
        — ``metrics/cycles.py`` derives the cycle's ``cost_basis``
        share-weighted from ``event.price`` and books ``stock_pnl`` against it
        at call-away, while ``option_pnl`` separately counts the put premium at
        ``sell_to_open``. Netting the premium into ``event.price`` (or the cash
        delta) would double-count it in every assigned cycle: once in
        ``option_pnl``, again through a lowered stock basis. Readers, all via
        ``event.price`` → ``cycle.cost_basis``: ``metrics/cycles.py``,
        ``metrics/fitness.py`` (``underwater_days``), ``reporting/report.py``
        ("Assigned at $…" lines and the JSON rows). Production has the same
        two-number split — Alpaca's floor vs the FC-024 ACB scorekeeping — so
        the ``detail`` dict carries both explicitly.

        One deliberate residual: ``pos.entry_price`` is the *haircut* fill
        premium, so the netting reflects this engine's own fill model rather
        than a hypothetical mid fill. That is consistent with the cash ledger,
        which credited the same haircut premium at open.
        """
        shares = 100 * pos.contracts
        cost = pos.strike * shares
        self.cash -= cost
        # Sanity floor: a premium at or above the strike is not a real fill,
        # and a zero/negative basis would silently disable the covered-call
        # floor entirely (find_suitable_calls warns and continues on a
        # non-positive min_strike_price). Fall back to the strike rather than
        # book an unprotectable lot.
        netted = pos.strike - pos.entry_price
        basis = netted if netted > 0 else pos.strike
        self._add_stock(pos.underlying, shares, basis, on_date)
        self._record(
            "put_assignment", pos.underlying, pos.symbol, contracts=pos.contracts,
            shares=shares, price=pos.strike, cash_delta=-cost, event_date=on_date,
            detail={"strike": pos.strike, "basis": basis,
                    "collateral_released": pos.collateral},
        )
        del self.options[pos.symbol]

    def _call_away(self, pos: OptionPosition, on_date: date, *, reason: str) -> None:
        """Short call assigned: shares sold at strike (FIFO), position closed."""
        shares = 100 * pos.contracts
        proceeds = pos.strike * shares
        self.cash += proceeds
        self._remove_shares_fifo(pos.underlying, shares)
        self._record(
            "call_assignment", pos.underlying, pos.symbol, contracts=pos.contracts,
            shares=shares, price=pos.strike, cash_delta=proceeds, event_date=on_date,
            detail={"reason": reason},
        )
        del self.options[pos.symbol]

    def _expire_worthless(self, pos: OptionPosition, on_date: date) -> None:
        """Option expires OTM: release any collateral, keep premium/shares."""
        self._record(
            "expire_worthless", pos.underlying, pos.symbol, contracts=pos.contracts,
            event_date=on_date, detail={"collateral_released": pos.collateral},
        )
        del self.options[pos.symbol]

    def _record(self, kind: str, underlying: str, symbol: str = "", *, contracts: int = 0,
                shares: int = 0, price: float = 0.0, cash_delta: float = 0.0, fees: float = 0.0,
                event_date: date, detail: Optional[dict] = None) -> None:
        self.ledger.append(
            LedgerEvent(
                event_date=event_date, kind=kind, underlying=underlying, symbol=symbol,
                contracts=contracts, shares=shares, price=price, cash_delta=cash_delta,
                fees=fees, detail=detail or {},
            )
        )
