"""BacktestAlpacaClient — the seam between live strategy code and the simulator.

`WheelEngine` and everything it builds consume a single object, `AlpacaClient`.
This module implements that object's *consumed surface* against historical data
and `BacktestBroker`, so the live `PutSeller`/`CallSeller`/`CallRoller`/
`MarketDataManager` run unmodified over history.

Three principles, each a defense against a specific way a replay can lie:

**No lookahead.** Every read is served as of the simulator's current date, which
is taken from the frozen `clock`. `get_stock_bars` never returns a bar dated
after that day. A backtest that can peek at tomorrow's close is worthless.

**Fail loud.** Live code reaches for the network in places the plan did not
enumerate. Any attribute this adapter does not implement raises
`UnsupportedBacktestCall` rather than falling through to a real client, so an
unmocked call halts the run instead of quietly hitting production.

**Mirror live, don't improve on it.** Where the live client returns a degenerate
value, so do we. `get_options_chain` hardcodes `open_interest: 0` exactly as
`AlpacaClient.get_options_chain` does — which means the live liquidity filter
(`volume == 0 and open_interest < 10`) reduces to "volume must be non-zero".
Fabricating open interest here would make the backtest strictly more permissive
than production and silently inflate the opportunity set.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterator, List, Optional

import pandas as pd
import structlog

from ...utils import clock
from ...utils.option_symbols import parse_option_symbol
from ..data.chain_builder import ChainSnapshot
from ..data.provider import StockBar
from .broker import (
    FILL_RULE_HAIRCUT,
    FILL_RULE_LIMIT_MARKETABLE,
    FILL_RULE_LIMIT_RESTING,
    ROLL_FILL_MODE_HAIRCUT,
    ROLL_FILL_MODE_LIMIT,
    ROLL_FILL_MODES,
    BacktestBroker,
)

# FC-116 D1b — the adapter sees no order KIND today (only symbol/qty/side/
# type/limit), so the simulator hands it one for the window in which the live
# `CallRoller` is the only order placer.
ROLL_INTENT = "roll"

logger = structlog.get_logger(__name__)


class UnsupportedBacktestCall(RuntimeError):
    """Live code reached for something the adapter does not simulate."""


# Alpaca stamps daily stock bars at MIDNIGHT ET — 04:00 UTC under EDT, 05:00 UTC
# under EST. The adapter reproduces the live stamp rather than inventing one.
# A fixed 04:00 is an hour off for winter dates, which was harmless for the
# historical consumer: the FC-036 gap gate compared CALENDAR DATES
# (``idx.date()``), and both 04:00 and 05:00 UTC on trading day D yield date D.
# (Before FC-036 it compared timestamps, and the stamp is what let the session's
# own bar pass as "previous close" — see docs/plans/fc-036.md. FC-069 has since
# deleted that gate entirely.) Kept fixed for determinism; revisit only if some
# caller ever depends on the exact hour.
_BAR_STAMP = time(4, 0)

# Ledger kinds that Alpaca reports as account activities, and their activity_type.
_LEDGER_TO_ACTIVITY = {
    "put_assignment": "OPASN",
    "call_assignment": "OPASN",
    "expire_worthless": "OPEXP",
}


class BacktestAlpacaClient:
    """Implements the AlpacaClient surface consumed by the strategy components."""

    def __init__(
        self,
        broker: BacktestBroker,
        *,
        chains: Dict[str, Dict[date, ChainSnapshot]],
        stock_bars: Dict[str, List[StockBar]],
        options_approved_level: int = 2,
        roll_fill_mode: str = ROLL_FILL_MODE_LIMIT,
    ) -> None:
        """
        Args:
            broker: the authoritative cash/position ledger.
            chains: symbol -> as_of -> ChainSnapshot, prebuilt for the run.
            stock_bars: symbol -> full ascending daily history. Reads are clipped
                to the simulator's current date, so passing the whole history is
                safe and lets one dict serve every day of the run.
            options_approved_level: mirrors the live account's approval level.
            roll_fill_mode: FC-116 — ``"limit"`` (default) fills roll legs
                against the day's modeled book capped by the placed limit;
                ``"haircut"`` is the pre-FC-116 regression arm and ignores the
                intent entirely.

        Raises:
            ValueError: `roll_fill_mode` is not one of `ROLL_FILL_MODES`
                (E5). Validated rather than degraded: the pricing branch below
                is `!= "limit"`, so an unrecognised spelling such as `"LIMIT"`
                would hash as a distinct sweep arm, silently RUN as haircut,
                and persist `roll_fill_mode="LIMIT"` on the row — an arm whose
                stored label contradicts the model it ran under.
        """
        if roll_fill_mode not in ROLL_FILL_MODES:
            raise ValueError(
                f"roll_fill_mode must be one of {list(ROLL_FILL_MODES)}, "
                f"got {roll_fill_mode!r}"
            )
        self._broker = broker
        self._chains = chains
        self._stock_bars = stock_bars
        self._options_approved_level = options_approved_level
        self._roll_fill_mode = roll_fill_mode
        # Initialised HERE, not lazily: `__getattr__` raises
        # `UnsupportedBacktestCall` on any missing attribute, so a lazily-set
        # `_order_intent` would turn the first haircut-mode read into a crash.
        self._order_intent: Optional[str] = None

        self._orders: Dict[str, Dict[str, Any]] = {}
        self._order_seq = 0
        # Ledger events already surfaced as activities keep stable synthetic ids,
        # because wheel_engine dedupes activities by id across reconciliation runs.
        self._activity_ids: Dict[int, str] = {}

    # ------------------------------------------------------------------ #
    # Simulator time
    # ------------------------------------------------------------------ #
    @property
    def today(self) -> date:
        """The simulated date. Reading the frozen clock keeps one source of truth."""
        if not clock.is_frozen():
            raise UnsupportedBacktestCall(
                "BacktestAlpacaClient used outside a frozen clock: the adapter "
                "would serve wall-clock data into a historical replay."
            )
        return clock.now().date()

    def _snapshot(self, underlying: str) -> Optional[ChainSnapshot]:
        return self._chains.get(underlying, {}).get(self.today)

    def _underlying_close(self, underlying: str) -> Optional[float]:
        snap = self._snapshot(underlying)
        if snap is not None:
            return snap.underlying_price
        for bar in reversed(self._stock_bars.get(underlying, [])):
            if bar.bar_date <= self.today:
                return bar.close
        return None

    # ------------------------------------------------------------------ #
    # Account & positions
    # ------------------------------------------------------------------ #
    def get_account(self) -> Dict[str, Any]:
        equity = self._equity()
        # Cash-secured only: buying power is uncommitted cash, never the old
        # engine's fictional 2:1 margin.
        available = self._broker.available_cash
        return {
            "buying_power": available,
            "cash": self._broker.cash,
            "portfolio_value": equity,
            "equity": equity,
            "options_buying_power": available,
            "options_approved_level": self._options_approved_level,
        }

    def _equity(self) -> float:
        stock_marks: Dict[str, float] = {}
        for underlying in self._broker.stock_lots:
            px = self._underlying_close(underlying)
            if px is not None:
                stock_marks[underlying] = px

        option_marks: Dict[str, float] = {}
        for symbol, pos in self._broker.options.items():
            snap = self._snapshot(pos.underlying)
            quote = _find_quote(snap, symbol) if snap else None
            if quote is not None:
                option_marks[symbol] = quote.mark
            else:
                # No trade that day: fall back to intrinsic against the underlying.
                px = self._underlying_close(pos.underlying)
                if px is None:
                    continue
                intrinsic = (
                    max(0.0, pos.strike - px)
                    if pos.option_type == "put"
                    else max(0.0, px - pos.strike)
                )
                option_marks[symbol] = intrinsic
        return self._broker.equity(stock_marks, option_marks)

    def get_positions(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        for underlying, lots in self._broker.stock_lots.items():
            shares = sum(lot.shares for lot in lots)
            if shares <= 0:
                continue
            px = self._underlying_close(underlying)
            basis = self._broker.average_cost_basis(underlying) or 0.0
            market_value = (px or 0.0) * shares
            out.append(
                {
                    "symbol": underlying,
                    "qty": float(shares),
                    "side": "long",
                    "market_value": market_value,
                    "cost_basis": basis * shares,
                    # FC-065: the covered-call floor reads this field, so the
                    # simulated broker has to emit it too. FC-068 closed the
                    # interim gap: `_assign_put` now books the lot at
                    # `strike − put premium`, matching Alpaca's own
                    # `avg_entry_price` semantics, so the simulated floor is
                    # the production floor rather than one premium above it.
                    "avg_entry_price": basis,
                    "unrealized_pl": market_value - basis * shares,
                    "asset_class": "us_equity",
                }
            )

        for symbol, pos in self._broker.options.items():
            snap = self._snapshot(pos.underlying)
            quote = _find_quote(snap, symbol) if snap else None
            mark = quote.mark if quote is not None else 0.0
            # Short options: negative qty and negative market value, as Alpaca reports.
            market_value = -mark * 100 * pos.contracts
            cost_basis = -pos.entry_price * 100 * pos.contracts
            out.append(
                {
                    "symbol": symbol,
                    "qty": float(-pos.contracts),
                    "side": "short",
                    "market_value": market_value,
                    "cost_basis": cost_basis,
                    # Per-contract-share premium received, as Alpaca reports it
                    # for a short option (positive, unlike cost_basis).
                    "avg_entry_price": pos.entry_price,
                    # Short premium: profit as the option decays toward zero.
                    "unrealized_pl": (pos.entry_price - mark) * 100 * pos.contracts,
                    "asset_class": "us_option",
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    def get_stock_quote(self, symbol: str) -> Dict[str, Any]:
        px = self._underlying_close(symbol)
        if px is None:
            return {}
        # EOD replay has no book; the close is both sides. MarketDataManager
        # averages bid/ask, so this yields exactly the close.
        return {
            "symbol": symbol,
            "bid": px,
            "ask": px,
            "bid_size": 0,
            "ask_size": 0,
            "timestamp": self.today.isoformat(),
        }

    def get_stock_bars(self, symbol: str, days: int = 30) -> pd.DataFrame:
        """Daily OHLCV up to and including the simulated date. Never beyond it.

        The index must be tz-aware UTC, exactly like the live client's
        (``datetime64[ns, UTC]``, bars stamped 04:00 UTC), so that replay and
        live derive identical trading dates from the index.

        ``days`` is a CALENDAR-day lookback, not a bar count — the live client
        builds ``start = end - timedelta(days=days)``. Slicing the last ``days``
        *bars* instead hands the caller ~43% more history (50 calendar days is
        ~35 sessions), which silently changes every windowed statistic computed
        on it. The illustration that established this, from the gap-frequency
        filter FC-069 later deleted: it was a ratio over exactly this window,
        and with the longer window NVDA measured 18.6% against a 15% limit and
        was blocked for all of Nov 2025, while live traded it on 7 days.
        """
        cutoff = self.today - timedelta(days=days) if days else None
        bars = [
            b
            for b in self._stock_bars.get(symbol, [])
            if b.bar_date <= self.today and (cutoff is None or b.bar_date > cutoff)
        ]
        if not bars:
            return pd.DataFrame()
        index = pd.DatetimeIndex(
            [datetime.combine(b.bar_date, _BAR_STAMP) for b in bars], name="timestamp"
        ).tz_localize("UTC")
        return pd.DataFrame(
            {
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            },
            index=index,
        )

    def get_options_chain(self, underlying_symbol: str) -> List[Dict[str, Any]]:
        snap = self._snapshot(underlying_symbol)
        if snap is None:
            return []

        out: List[Dict[str, Any]] = []
        for quote in snap.all_quotes():
            # A contract whose IV would not solve has no delta. Live code does
            # abs(opt.get('delta', 0)) and would raise TypeError on None, so such
            # contracts are dropped: unpriceable means not a candidate.
            if quote.delta is None:
                continue
            out.append(
                {
                    "symbol": quote.symbol,
                    "underlying_symbol": underlying_symbol,
                    "option_type": quote.option_type,
                    "strike_price": quote.strike,
                    "expiration_date": quote.expiration.isoformat(),
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "bid_size": 0,
                    "ask_size": 0,
                    "last_price": quote.mark,
                    "volume": quote.volume,
                    # Mirrors live exactly; see module docstring.
                    "open_interest": 0,
                    "implied_volatility": quote.implied_volatility or 0.0,
                    "delta": quote.delta,
                    "gamma": 0.0,
                    "theta": 0.0,
                    "vega": 0.0,
                }
            )
        return out

    def get_option_quote(self, option_symbol: str) -> Dict[str, Any]:
        parsed = parse_option_symbol(option_symbol)
        snap = self._snapshot(parsed.get("underlying", ""))
        quote = _find_quote(snap, option_symbol) if snap else None
        if quote is None:
            return {}
        return {
            "symbol": option_symbol,
            "bid": quote.bid,
            "ask": quote.ask,
            "mid_price": (quote.bid + quote.ask) / 2,
            "bid_size": 0,
            "ask_size": 0,
        }

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    @contextmanager
    def order_intent(self, kind: Optional[str]) -> Iterator[None]:
        """Declare what the orders placed inside this window ARE (FC-116 D1b).

        The live `AlpacaClient` surface carries no order kind, and the adapter
        must not grow one — that would be a live API change for a replay
        concern. The simulator instead wraps the one seat in its day loop where
        the `CallRoller` is the only order placer:

            with client.order_intent("roll"):
                rolls = engine.run_rolling_cycle() or {}

        Rejected alternatives: a `kind=` kwarg threaded through `CallRoller`
        (changes the live surface); inferring "roll" from `side == "buy"` on a
        call (the CC monitor leg buys to close too, and must stay on the
        haircut path).
        """
        previous = self._order_intent
        self._order_intent = kind
        try:
            yield
        finally:
            self._order_intent = previous

    def place_option_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str = "limit",
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Fill against the day's modeled book. Two rules, by order intent.

        **Entry legs and the CC monitor leg** (no intent) fill at the broker's
        haircut price and the strategy's `limit_price` is recorded but not
        enforced: at one decision point per day there is no intraday path along
        which to decide whether a limit would have been touched. FC-072
        measured entry fills against exactly that convention, which is why
        `strategy.*_limit_spread_fraction` is refused as a sweep key.

        **Roll legs** (inside `order_intent("roll")`, under the default
        `roll_fill_mode="limit"`) fill AT THE PLACED LIMIT, capped by the book,
        or not at all — FC-116 D1:

            buy   limit >= ask_c      -> fills `ask_c` (marketable; Alpaca
                                                        fills at the best
                                                        offer, never at a
                                                        worse limit)
            buy   bid_c <= limit < ask_c -> fills `limit` (resting inside)
            buy   limit <  bid_c      -> `expired`, filled_qty 0
            sell  limit <= bid_c      -> fills `bid_c`
            sell  bid_c < limit <= ask_c -> fills `limit`
            sell  limit >  ask_c      -> `expired`, filled_qty 0
            either limit_price None   -> far quote (a market order under intent)

        where `bid_c`/`ask_c` are the modeled book QUANTISED TO CENTS. The
        roller places cent-rounded limits; comparing them against an unrounded
        model book would tag a marketable leg `limit_resting` roughly half the
        time (T1).

        Why: the live `CallRoller` is credit-only AT ITS PLACED LIMITS (base
        mode BTC at `round(ask, 2)` / STO at `round(bid, 2)`; imminence mode
        both legs at `round(mid +/- 0.05, 2)`), and its credit invariant is
        tested on those limits. Filling at mid would manufacture credit the
        live roller never sees.

        The ONE approximation: the replay has one modeled book per day and no
        intraday tape, so a limit resting INSIDE the spread is assumed touched
        within the leg's 120 s window. That is tagged `limit_resting` on the
        ledger event and counted per row (`roll_legs_resting`) rather than
        estimated away — the residual versus the far quote is `hs - 0.05` per
        share on an imminence-mode leg, which is NOT small on a high-mark
        chain.

        `roll_fill_mode="haircut"` ignores the intent entirely and is the
        pre-FC-116 regression arm.
        """
        parsed = parse_option_symbol(symbol)
        underlying = parsed.get("underlying", "")
        option_type = parsed.get("option_type", "unknown")
        strike = float(parsed.get("strike_price", 0.0))
        expiration = _parse_expiration(parsed)

        snap = self._snapshot(underlying)
        quote = _find_quote(snap, symbol) if snap else None
        if quote is None:
            return self._order_error(
                symbol, qty, side, "no_quote",
                f"No {symbol} bar on {self.today}: contract did not trade.",
            )

        # FC-116 D1 — the rule price and its ledger tag, or `None` to keep the
        # haircut. Computed BEFORE the broker call so the broker's cash check
        # and the `insufficient_cash` message both quote the price that would
        # actually be paid.
        rule_fill, fill_detail, rule_expired = self._roll_leg_fill(
            side, limit_price, quote,
        )

        if side == "sell":
            if option_type == "put":
                # Puts are never roll legs (the roller rolls calls), so this
                # branch is untouched by FC-116.
                fill = self._broker.sell_put_to_open(
                    symbol, underlying, strike, expiration, qty,
                    quote.mark, quote.bid, self.today,
                )
                if fill is None:
                    return self._order_error(
                        symbol, qty, side, "insufficient_collateral",
                        f"Need ${strike * 100 * qty:,.0f} collateral, "
                        f"${self._broker.available_cash:,.0f} available.",
                    )
            else:
                # E7 precedence: the position-level check runs BEFORE the fill
                # rule. Live Alpaca rejects an uncovered STO at placement, so
                # the roller must see `stc_rejected`, never `expired`.
                if self._broker.uncovered_shares(underlying) < 100 * qty:
                    return self._order_error(
                        symbol, qty, side, "insufficient_shares",
                        f"Need {qty * 100} uncovered shares of {underlying}; "
                        f"hold {self._broker.shares(underlying)}, of which "
                        f"{self._broker.pledged_shares(underlying)} already back "
                        f"open calls.",
                    )
                if rule_expired:
                    return self._order_expired(symbol, qty, side, limit_price)
                fill = self._broker.sell_call_to_open(
                    symbol, underlying, strike, expiration, qty,
                    quote.mark, quote.bid, self.today,
                    fill=rule_fill, fill_detail=fill_detail,
                )
                if fill is None:  # pragma: no cover - pre-checked above
                    return self._order_error(
                        symbol, qty, side, "insufficient_shares",
                        f"Need {qty * 100} uncovered shares of {underlying}; "
                        f"hold {self._broker.shares(underlying)}, of which "
                        f"{self._broker.pledged_shares(underlying)} already back "
                        f"open calls.",
                    )
        elif side == "buy":
            # Capture the reason BEFORE attempting, so a rejection is diagnosed
            # correctly. buy_to_close returns None for two different causes, and
            # reporting a cash shortfall as "no position" would mislead exactly
            # the rejection analysis this feeds.
            #
            # E7 precedence: `no_position` outranks `expired` — live Alpaca
            # rejects a close of a position you do not hold at placement, so the
            # roller must see `btc_rejected`, not a timeout. A leg that clears
            # the position check and whose limit lies outside the book expires;
            # only a leg that would actually fill can run out of cash, and that
            # message quotes the RULE price.
            position = self._broker.options.get(symbol)
            if position is None or qty > position.contracts:
                return self._order_error(
                    symbol, qty, side, "no_position",
                    f"No open short position in {symbol} to close "
                    f"({qty} contracts requested).",
                )
            if rule_expired:
                return self._order_expired(symbol, qty, side, limit_price)
            fill = self._broker.buy_to_close(
                symbol, qty, quote.mark, quote.ask, self.today,
                fill=rule_fill, fill_detail=fill_detail,
            )
            if fill is None:
                cost = (
                    self._broker.buy_fill(quote.mark, quote.ask)
                    if rule_fill is None else rule_fill
                ) * 100 * qty
                return self._order_error(
                    symbol, qty, side, "insufficient_cash",
                    f"Buy-to-close needs ${cost:,.2f} but only "
                    f"${self._broker.available_cash:,.2f} is available.",
                )
        else:
            raise UnsupportedBacktestCall(f"Unsupported order side: {side!r}")

        return self._order_success(symbol, qty, side, limit_price, fill)

    def _roll_leg_fill(self, side, limit_price, quote):
        """(fill, ledger detail, expired) for one leg — FC-116 D1.

        Returns ``(None, None, False)`` for every leg that is not a roll leg,
        which keeps the haircut path byte-identical: the broker is then called
        exactly as it was before FC-116 and stamps `fill_rule: "haircut"`
        itself.

        Under roll intent the returned price is `min(limit, ask_c)` on a buy
        and `max(limit, bid_c)` on a sell — the book caps the limit in the
        direction that favours the book, never the order. A limit on the WRONG
        side of the book (below the bid on a buy, above the ask on a sell) does
        not fill at all.

        **The book is quantised to cents first** (T1). The live `CallRoller`
        places `round(ask, 2)` / `round(bid, 2)` / `round(mid ± 0.05, 2)`,
        while a modeled chain's bid/ask carry full float precision. Comparing a
        cent-rounded limit against an unrounded book mis-tags roughly half of
        all base-mode legs: a BTC at `round(ask, 2) = 1.48` against an
        `ask = 1.4815` is `limit < ask`, so it would be called `limit_resting`
        and would fill 0.15 c inside a book it is in fact marketable against.
        Real exchanges quote in cents, so rounding the book — not the limit —
        is the faithful model, and the resting tag then means what it says:
        the limit is strictly inside the quantised spread.
        """
        if self._order_intent != ROLL_INTENT:
            return None, None, False
        if self._roll_fill_mode != ROLL_FILL_MODE_LIMIT:
            # The regression arm: price exactly as before, but still say which
            # limit was placed, so a haircut-mode ledger is comparable leg for
            # leg against a limit-mode one.
            return None, {
                "fill_rule": FILL_RULE_HAIRCUT, "limit_price": limit_price,
            }, False

        bid_c, ask_c = round(float(quote.bid), 2), round(float(quote.ask), 2)
        if limit_price is None:
            # A market order under roll intent: crosses to the far quote.
            far = ask_c if side == "buy" else bid_c
            return far, {
                "fill_rule": FILL_RULE_LIMIT_MARKETABLE, "limit_price": None,
            }, False

        limit = float(limit_price)
        if side == "buy":
            if limit >= ask_c:
                fill, rule = ask_c, FILL_RULE_LIMIT_MARKETABLE
            elif limit >= bid_c:
                fill, rule = limit, FILL_RULE_LIMIT_RESTING
            else:
                return None, None, True
        else:
            if limit <= bid_c:
                fill, rule = bid_c, FILL_RULE_LIMIT_MARKETABLE
            elif limit <= ask_c:
                fill, rule = limit, FILL_RULE_LIMIT_RESTING
            else:
                return None, None, True
        return fill, {"fill_rule": rule, "limit_price": limit}, False

    def _order_expired(self, symbol, qty, side, limit_price) -> Dict[str, Any]:
        """A placed order whose limit never touched the book — FC-116 D1.

        `success: True` with a TERMINAL status, deliberately: live Alpaca
        ACCEPTS this order, and the roller's own post-placement dispositions
        (`btc_timeout_canceled`, the STO ladder, `stc_failed_naked_exposure`)
        are what must run. Because `expired` is in
        `CallRoller._TERMINAL_ORDER_STATUSES`, `_poll_order_fill` returns on
        its first `get_order_by_id` read with NO `time.sleep` — the replay
        never waits 120 s per leg — and `_cancel_and_settle` is never reached.
        """
        self._order_seq += 1
        order_id = f"bt-{self._order_seq:06d}"
        stamp = clock.now().isoformat()
        record = {
            "order_id": order_id,
            "client_order_id": order_id,
            "symbol": symbol,
            "qty": int(qty),
            "filled_qty": 0,
            "remaining_qty": int(qty),
            "is_partial_fill": False,
            "side": side,
            "status": "expired",
            "order_type": "limit",
            "limit_price": limit_price,
            "filled_avg_price": None,
            "submitted_at": stamp,
            "filled_at": None,
            "expired_at": stamp,
            "canceled_at": None,
        }
        self._orders[order_id] = record
        return {"success": True, **{k: record[k] for k in (
            "order_id", "client_order_id", "symbol", "qty", "side",
            "limit_price", "status", "submitted_at")}}

    def _order_success(self, symbol, qty, side, limit_price, fill) -> Dict[str, Any]:
        self._order_seq += 1
        order_id = f"bt-{self._order_seq:06d}"
        stamp = clock.now().isoformat()
        record = {
            "order_id": order_id,
            "client_order_id": order_id,
            "symbol": symbol,
            "qty": int(qty),
            "filled_qty": int(qty),
            "remaining_qty": 0,
            "is_partial_fill": False,
            "side": side,
            "status": "filled",
            "order_type": "limit",
            "limit_price": limit_price,
            "filled_avg_price": fill,
            "submitted_at": stamp,
            "filled_at": stamp,
            "expired_at": None,
            "canceled_at": None,
        }
        self._orders[order_id] = record
        return {"success": True, **{k: record[k] for k in (
            "order_id", "client_order_id", "symbol", "qty", "side",
            "limit_price", "status", "submitted_at")}}

    @staticmethod
    def _order_error(symbol, qty, side, error_type, message) -> Dict[str, Any]:
        # Live returns (never raises) on rejection; strategy code checks success.
        return {
            "success": False,
            "error_type": error_type,
            "error_message": message,
            "non_retryable": True,
            "symbol": symbol,
            "qty": qty,
            "side": side,
        }

    def get_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        orders = list(self._orders.values())
        if status:
            orders = [o for o in orders if o["status"] == status]
        return orders

    def get_order_by_id(self, order_id: str) -> Dict[str, Any]:
        return dict(self._orders.get(order_id, {}))

    # ------------------------------------------------------------------ #
    # Account activities (assignment / expiration reconciliation)
    # ------------------------------------------------------------------ #
    def get_account_activities(
        self,
        activity_types: str = "OPASN,OPEXP",
        after: Optional[str] = None,
        page_size: int = 100,
        page_token: Optional[str] = None,
        direction: str = "desc",
    ) -> List[Dict[str, Any]]:
        """Synthesize Alpaca activities from the broker's ledger.

        wheel_engine dedupes by ``id`` across reconciliation runs, so ids must be
        stable for a given ledger event — hence keying on the event's index.
        """
        wanted = {t.strip() for t in activity_types.split(",") if t.strip()}
        after_date = _parse_iso_date(after) if after else None

        out: List[Dict[str, Any]] = []
        for idx, ev in enumerate(self._broker.ledger):
            activity_type = _LEDGER_TO_ACTIVITY.get(ev.kind)
            if activity_type is None or activity_type not in wanted:
                continue
            if after_date and ev.event_date < after_date:
                continue
            if ev.event_date > self.today:
                continue  # no lookahead, even into our own future ledger
            out.append(
                {
                    "id": self._activity_ids.setdefault(idx, f"bt-act-{idx:06d}"),
                    "activity_type": activity_type,
                    "symbol": ev.symbol,
                    "qty": float(ev.contracts),
                    "price": ev.price,
                    "net_amount": ev.cash_delta,
                    "date": ev.event_date.isoformat(),
                    "transaction_time": ev.event_date.isoformat(),
                }
            )
        out.sort(key=lambda a: a["date"], reverse=(direction == "desc"))
        return out[:page_size]

    # ------------------------------------------------------------------ #
    # Fail loud on anything else
    # ------------------------------------------------------------------ #
    def __getattr__(self, name: str) -> Any:
        raise UnsupportedBacktestCall(
            f"Live code called AlpacaClient.{name!r}, which the backtest adapter "
            "does not simulate. Implement it or the replay is silently reaching "
            "for production."
        )


def _find_quote(snap: Optional[ChainSnapshot], symbol: str):
    if snap is None:
        return None
    for quote in snap.all_quotes():
        if quote.symbol == symbol:
            return quote
    return None


def _parse_expiration(parsed: Dict[str, Any]) -> date:
    raw = parsed.get("expiration_date")
    if isinstance(raw, date):
        return raw
    return datetime.strptime(str(raw), "%Y-%m-%d").date()


def _parse_iso_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()
