"""Sell-to-open limit pricing, shared by both wheel legs (FC-072 rev 2).

## What this module does

Turns a broker quote into the limit price of a sell-to-open order, for the put
leg and the call leg alike, so the two cannot drift apart (Symmetry Principle,
`docs/CLAUDE.md`). Three things happen here, in order:

1. **Re-quote at execute time** (`refresh_quote`). `/scan` quotes the chain at
   `:00` and `/run` places the order at `:15`; nothing on the execute path used
   to re-quote, so every limit was computed from a quote up to 15 minutes old.
   That staleness -- not the spread -- is the dominant variable in where these
   orders land. `refresh_quote` asks the broker for the current book and falls
   back to the scan-time quote (never failing the order) when it cannot.
2. **Price** (`sell_limit_price`): ``mid + f x spread`` off whichever quote was
   used, with ``mid`` always recomputed from that quote's own bid/ask.
3. **Round to a legal tick** (`round_to_tick`), which is a broker constraint,
   not a preference -- see below.

## The economics, corrected

The call leg's historical formula was ``round(premium x 0.95, 2)``. It is
tempting to read that as "5% below mid, donated" -- the first revision of this
plan did, and it was wrong. On a typical call book here (mid ~$1.30, spread
~$0.13) five percent of mid is about half a spread, so that limit sat
**roughly at the bid**. A sell limit at or below the bid is marketable: it
fills at the current bid, not at the limit. Measured over the 66 journaled call
fills, realised ``sum(mid - fill)`` was **-$87** -- essentially nothing was
being given away per fill. The retired "$2.3k left on the table" headline was
``sum(mid - limit)``, an accounting of prices never traded at, not of money.

What IS recoverable is the difference between selling at the bid and resting at
the mid, expected around **+$5 per write gross**, and the cost is fill rate:
pricing at mid is expected to fill roughly **75-80%** of the time, against
about 90% for a marketable at-or-below-bid order. That trade is the point of
``call_limit_spread_fraction``, and the two-week readout in
``docs/plans/fc-072.md`` is how it gets judged. Do not restate the retired
"$2.3k / 5% donated" framing anywhere.

## Tick increments are a broker rule

Alpaca's options pricing increments: **$0.01 below $3.00, $0.05 at and above
$3.00**, except penny-program names (`PENNY_PROGRAM_SYMBOLS`) which are penny
at all prices. A non-conforming limit is routed to an exchange that accepts it
if one exists and is **rejected** otherwise. The paper simulator does not
enforce this -- a year of paper history shows off-tick limits above $3
"accepted" -- so paper evidence cannot clear a live account. Sell-side snapping
rounds **up**: rounding a sell limit down would hand over up to a tick for
nothing.

Rounding below $3.00 is ``ROUND_HALF_UP`` on the exact decimal value, not
``round()``. Python's ``round()`` is banker's rounding over binary floats, so a
half-cent mid on a one-cent book (bid 0.60 / ask 0.61 -> mid 0.605) landed on
the bid or the ask depending on float representation. Half-up puts it on the
ask, where the order rests instead of crossing.

## What is deliberately NOT priced here

``CallRoller`` prices its sell-to-open **at the bid** (or mid - $0.05 when the
roll is imminent) and is not routed through this module. That asymmetry is
intentional: a defensive roll is credit-only and must actually execute in the
same session as its buy-to-close leg, so it pays the spread to guarantee the
fill. Opening writes have the opposite priority -- they can afford to rest.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Dict, NamedTuple, Optional, Tuple

from ..utils import clock

# Penny-program underlyings: quoted in $0.01 increments at every price level.
# Everything else ticks at $0.05 from $3.00 up.
PENNY_PROGRAM_SYMBOLS = frozenset({"SPY", "QQQ", "IWM"})

_TICK_THRESHOLD = Decimal("3.00")
_NICKEL = Decimal("0.05")
_PENNY = Decimal("0.01")

# The historical fallback for an unusable book: 5% under the scan-time premium.
# Unchanged on both legs, and deliberately so -- see the module docstring on why
# this is a marketable order rather than a donation.
FALLBACK_PREMIUM_DISCOUNT = Decimal("0.95")


class ExecutionQuote(NamedTuple):
    """The book the limit will be priced off, and where it came from.

    ``source`` is ``"live"`` when the execute-time refresh returned a usable
    two-sided quote and ``"blob"`` when the scan-time quote off the opportunity
    was used instead. ``age_s`` is 0.0 for a live quote and the age of the scan
    stamp for a blob quote (None when the opportunity carries no stamp).
    """

    bid: Optional[float]
    ask: Optional[float]
    source: str
    age_s: Optional[float]


class PricedLimit(NamedTuple):
    """The priced order.

    ``mid`` and ``spread_fraction`` are None when the book was unusable and the
    ``premium x 0.95`` fallback priced the order -- that is what makes a
    fallback write distinguishable from a priced-at-mid one in the logs.
    ``tick_snapped`` is True only when the $0.05 rule actually MOVED the price.
    """

    limit_price: float
    mid: Optional[float]
    spread: Optional[float]
    spread_fraction: Optional[float]
    tick_snapped: bool


def _as_number(value: Any) -> Optional[float]:
    """Coerce a quote field to float, or None when it is not a real number.

    ``bool`` is excluded explicitly: it is an ``int`` subclass, and a stray
    ``True`` must not read as a $1.00 bid.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dec(value: float) -> Decimal:
    """Exact decimal for a quote value.

    Via ``str`` on purpose: ``Decimal(0.605)`` is 0.60499999..., which rounds
    half-up to 0.60 and reintroduces exactly the float luck this module exists
    to remove.
    """
    return Decimal(str(float(value)))


def quote_is_usable(bid: Any, ask: Any) -> bool:
    """True when a two-sided book can price a limit.

    ``bid > 0 and ask > 0 and ask >= bid``. Three notes, each of which was a
    decision:

    * A **locked** book (``ask == bid``) is usable. The formula then prices at
      the bid, which is the only price the market is showing -- the alternative
      (fall back to ``premium x 0.95``) prices 5% THROUGH a locked market.
    * A **wide** book is usable, with no cap. A book wider than half of mid is
      illiquid, not stale, and discounting it a further 5% concedes most where
      the mid is least informative. (Rev 1 of FC-072 had such a cap; it made the
      put leg give up more on the one historical wide book it had, and it is
      deliberately removed.)
    * **One-sided** (``bid == 0`` or ``ask == 0``) and **crossed**
      (``ask < bid``) books are not markets, and are the only unusable cases.
    """
    bid_f, ask_f = _as_number(bid), _as_number(ask)
    if bid_f is None or ask_f is None:
        return False
    return bid_f > 0 and ask_f > 0 and ask_f >= bid_f


def quote_age_seconds(scanned_at: Any) -> Optional[float]:
    """Seconds between the scan stamp and now, or None if it cannot be read.

    Reads ``scan_timestamp`` off an opportunity -- the stamp the scanner has
    always written. It is produced by ``clock.now()``, which is naive local time
    (UTC in Cloud Run), so a naive stamp is compared against ``clock.now()`` and
    an aware one against ``clock.now_utc()``; mixing them would raise. Never
    raises: an unreadable stamp is None, which is a log field, not an order
    decision.

    A negative value is returned as-is rather than clamped -- it means clock
    skew between the scan and the run, which is worth seeing in the logs.
    """
    if isinstance(scanned_at, datetime):
        parsed = scanned_at
    elif isinstance(scanned_at, str) and scanned_at.strip():
        try:
            parsed = datetime.fromisoformat(
                scanned_at.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    now = clock.now_utc() if parsed.tzinfo is not None else clock.now()
    try:
        return round((now - parsed).total_seconds(), 1)
    except TypeError:
        return None


def refresh_quote(alpaca: Any, option_symbol: str,
                  opportunity: Dict[str, Any],
                  logger: Any = None) -> ExecutionQuote:
    """Re-quote at execute time; fall back to the scan-time quote.

    The order is NEVER failed because the refresh failed -- a stale quote prices
    a worse limit, a refused order costs the whole cycle. A refresh that raises
    or returns nothing is logged as ``quote_refresh_failed`` and falls back.

    A live quote that comes back one-sided or crossed is not "live" for our
    purposes: it falls back to the blob quote, which then faces the same
    usability test in :func:`sell_limit_price`.

    Note for replay: this calls whatever client it is handed.
    ``BacktestAlpacaClient.get_option_quote`` reads the same frozen daily chain
    snapshot the opportunity came from, so a backtest re-quote is offline and
    returns the identical book -- no network, and deterministic by construction.
    """
    live: Dict[str, Any] = {}
    error: Optional[str] = None
    try:
        live = alpaca.get_option_quote(option_symbol) or {}
    except Exception as exc:  # never fail the order on a quote refresh
        error = str(exc)

    bid, ask = live.get("bid"), live.get("ask")
    if quote_is_usable(bid, ask):
        return ExecutionQuote(float(bid), float(ask), "live", 0.0)

    if logger is not None and (error is not None or not live):
        logger.warning(
            "Execute-time quote refresh failed - pricing off the scan-time quote",
            event_category="data",
            event_type="quote_refresh_failed",
            symbol=option_symbol,
            reason="exception" if error else "no_quote",
            error=error,
        )

    return ExecutionQuote(
        opportunity.get("bid"),
        opportunity.get("ask"),
        "blob",
        quote_age_seconds(opportunity.get("scan_timestamp")),
    )


def round_to_tick(value: Decimal, underlying: str = "") -> Tuple[float, bool]:
    """Round a SELL limit to a legal increment. Returns ``(price, snapped)``.

    At and above $3.00 on a non-penny-program name, round UP to the next $0.05.
    Below $3.00 (and always for penny-program names) round to the cent, half-up.
    ``snapped`` is True only when the $0.05 rule moved the price off the
    cent-rounded value.

    The $3.00 test is on the UNROUNDED value: a raw 2.995 is a sub-$3 price, so
    it rounds half-up to 3.00 -- which is itself on a $0.05 tick, so nothing is
    lost by not re-testing after rounding.
    """
    cent = value.quantize(_PENNY, rounding=ROUND_HALF_UP)
    root = (underlying or "").strip().upper()
    if value >= _TICK_THRESHOLD and root not in PENNY_PROGRAM_SYMBOLS:
        nickel = (value / _NICKEL).to_integral_value(
            rounding=ROUND_CEILING) * _NICKEL
        nickel = nickel.quantize(_PENNY)
        return float(nickel), nickel != cent
    return float(cent), False


def sell_limit_price(bid: Any, ask: Any, fallback_premium: Any,
                     spread_fraction: float,
                     underlying: str = "") -> PricedLimit:
    """Price one sell-to-open limit order.

    Args:
        bid: Bid of the book being priced off (live or blob).
        ask: Ask of that book.
        fallback_premium: The opportunity's ``premium``, used only when the book
            is unusable.
        spread_fraction: Fraction of the spread added to mid. ``0.0`` prices at
            mid; the put leg's historical ``0.10`` biases toward the ask.
        underlying: Ticker, for the penny-program tick exemption.

    Returns:
        A :class:`PricedLimit`.
    """
    bid_f, ask_f = _as_number(bid), _as_number(ask)
    spread = (ask_f - bid_f) if (bid_f is not None and ask_f is not None) else None

    if quote_is_usable(bid_f, ask_f):
        bid_d, ask_d = _dec(bid_f), _dec(ask_f)
        mid_d = (bid_d + ask_d) / 2
        raw = mid_d + _dec(spread_fraction) * (ask_d - bid_d)
        limit, snapped = round_to_tick(raw, underlying)
        return PricedLimit(limit, float(mid_d), float(ask_d - bid_d),
                           float(spread_fraction), snapped)

    premium = _as_number(fallback_premium) or 0.0
    limit, snapped = round_to_tick(
        _dec(premium) * FALLBACK_PREMIUM_DISCOUNT, underlying)
    return PricedLimit(limit, None, spread, None, snapped)
