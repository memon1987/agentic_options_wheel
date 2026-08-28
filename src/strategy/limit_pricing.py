"""Sell-to-open limit pricing, shared by both wheel legs (FC-072).

## What this module does

Turns a broker quote into the limit price of a sell-to-open order, for the put
leg and the call leg alike, so the two cannot drift apart (Symmetry Principle,
`docs/CLAUDE.md`). Three things happen here, in order:

1. **Re-quote at execute time** (`refresh_quote`). `/scan` quotes the chain at
   `:00` and `/run` places the order at `:15`; nothing on the execute path used
   to re-quote, so every limit was computed from a quote up to 15 minutes old.
   That staleness -- not the spread -- is the dominant variable in where these
   orders land. `refresh_quote` asks the broker for the current book and falls
   back to the scan-time quote (never failing the order) when it cannot, always
   saying which happened and why.
2. **Price** (`sell_limit_price`): ``mid + f x spread`` off whichever quote was
   used, with ``mid`` always recomputed from that quote's own bid/ask -- except
   on a **one-tick book**, where there is no mid to speak of and the order
   prices at the bid (see below).
3. **Round to a legal tick** (`round_to_tick`), which is a broker constraint,
   not a preference -- see below.

## Where these quotes come from, and the precondition nobody should skip

This account reads Alpaca's **indicative** options feed
(``alpaca_client.OPTION_QUOTE_FEED``), not OPRA: the OPRA agreement is not
signed. Indicative quotes are an *adjusted* best-bid/offer, not the NBBO. They
are good enough to price a paper order and to run the fill-rate readout, and
they are **not** good enough to trade real money against -- a limit computed
from a non-NBBO book can rest away from the real market without anyone
noticing. **Signing the OPRA agreement is a precondition before this code
prices a real-money order**; the feed is passed explicitly and echoed back on
every quote (``quote_feed`` on the executing events) so that precondition is
auditable from the logs rather than remembered.

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

Alpaca quotes options in one of two increment regimes, and the distinction the
names below draw is the one that matters:

* **Always-penny** (`ALWAYS_PENNY_SYMBOLS`): $0.01 at *every* price level.
  SPY, QQQ and IWM are the standing exception to the $3.00 rule.
* **Penny-program** (`VERIFIED_PENNY_PROGRAM_ROOTS`): $0.01 below $3.00,
  **$0.05 at and above $3.00**. Every one of the 14 configured roots was
  verified ``ppind=True`` on 2026-08-28, so in this universe this is the rule.

A genuine **non-penny-program** class ticks $0.05 below $3.00 and $0.10 above,
and this module deliberately **cannot emit those increments**: no such root is
tradeable here today, and inventing an untested third regime would be worse
than the loud warning it gets instead. An unrecognised root is priced under the
penny-program rule and logged once per process as ``tick_class_unverified`` --
if that event ever appears, verify the root's ``ppind`` before trusting the
limit.

A non-conforming limit is routed to an exchange that accepts it if one exists
and is **rejected** otherwise. The paper simulator does not enforce increments
-- a year of paper history shows off-tick limits above $3 "accepted" -- so
paper evidence cannot clear a live account. Sell-side snapping rounds **up**:
rounding a sell limit down would hand over up to a tick for nothing.

Rounding below the $0.05 regime is ``ROUND_HALF_UP`` on the exact decimal, not
``round()``. Python's ``round()`` is banker's rounding over binary floats, so a
half-cent mid on a one-cent book (bid 0.60 / ask 0.61 -> mid 0.605) landed on
the bid or the ask depending on float representation.

## One-tick books price at the bid

When the usable book is exactly one tick wide there is no midpoint to rest at:
the only two prices in the market are the bid and the ask, and "mid, rounded
half-up" is just the ask by another name. On a 0.15-0.25 delta call, resting at
the ask on a one-tick book costs more in missed cycles than it gains (on the
journal sample, roughly -$30 of forgone cycles against +$6 of captured spread),
so a one-tick book prices **at the bid** and logs ``one_tick_book=True``.

## What is deliberately NOT priced here

``CallRoller`` prices its sell-to-open **at the bid** (or mid - $0.05 when the
roll is imminent) and is not routed through this module. That asymmetry is
intentional: a defensive roll is credit-only and must actually execute in the
same session as its buy-to-close leg, so it pays the spread to guarantee the
fill. Opening writes have the opposite priority -- they can afford to rest.
The roller's limits, and ``/monitor``'s ``ask x 0.95`` buy-to-close, are
therefore **still un-snapped above $3.00** (paper-only today; its own FC).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Dict, NamedTuple, Optional, Tuple

import structlog

from ..utils import clock

logger = structlog.get_logger(__name__)

# Always $0.01, at every price level. The standing exception to the $3.00 rule.
ALWAYS_PENNY_SYMBOLS = frozenset({"SPY", "QQQ", "IWM"})

# Roots verified ``ppind=True`` (penny-pilot/penny-program: $0.01 below $3.00,
# $0.05 at and above) against Alpaca's contract metadata on 2026-08-28. This is
# every root the two profiles can trade: `config/settings.yaml` `stocks.symbols`
# for the wheel, and the covered-call profile's holdings-derived universe draws
# from the same account. A root outside this set is priced under the same rule
# and warned about once -- see `round_to_tick`.
VERIFIED_PENNY_PROGRAM_ROOTS = frozenset({
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "AMD", "QQQ", "SPY", "IWM",
    "UNH", "F", "PFE", "KMI", "VZ",
})

_TICK_THRESHOLD = Decimal("3.00")
_NICKEL = Decimal("0.05")
_PENNY = Decimal("0.01")

# The historical fallback for an unusable book: 5% under the scan-time premium.
# Unchanged on both legs, and deliberately so -- see the module docstring on why
# this is a marketable order rather than a donation.
FALLBACK_PREMIUM_DISCOUNT = Decimal("0.95")

# Roots already warned about this process, so an unverified underlying produces
# one line per process rather than one per order. Cleared by tests.
_WARNED_UNVERIFIED_ROOTS = set()


class ExecutionQuote(NamedTuple):
    """The book the limit will be priced off, and where it came from.

    ``source`` is ``"live"`` when the execute-time refresh returned a usable
    two-sided quote and ``"blob"`` when the scan-time quote off the opportunity
    was used instead. ``fallback_reason`` is None on the live path and names the
    cause otherwise (``"api_error"``, ``"empty"``, ``"unusable_live_book"``).

    ``age_s`` is the age of the book being priced off: for a live quote, now
    minus the broker's own quote timestamp -- **not** 0.0, because a halted or
    thin name's "latest" quote can be hours old and must not read as fresh. It
    is None when the age cannot be established at all.
    """

    bid: Optional[float]
    ask: Optional[float]
    source: str
    age_s: Optional[float]
    fallback_reason: Optional[str]
    feed: Optional[str]
    ts: Optional[str]


class PricedLimit(NamedTuple):
    """The priced order.

    ``mid`` is None when the book was unusable and the ``premium x 0.95``
    fallback priced the order; ``spread_fraction`` is None in that same case,
    which is what makes a fallback write distinguishable from a priced-at-mid
    one in the logs. On a **one-tick book** ``spread_fraction`` reports the
    configured value that ``one_tick_book`` overrode -- the book was usable, the
    fraction simply had no midpoint to apply to.

    ``tick_snapped`` is True only when the $0.05 rule actually MOVED the price.
    """

    limit_price: float
    mid: Optional[float]
    spread: Optional[float]
    spread_fraction: Optional[float]
    tick_snapped: bool
    one_tick_book: bool


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


def _root(underlying: str) -> str:
    return (underlying or "").strip().upper()


def tick_size(price: Decimal, underlying: str = "") -> Decimal:
    """The legal increment for ``underlying`` at ``price``.

    Always-penny roots are $0.01 everywhere. Everything else -- verified
    penny-program roots and, with a warning, anything unrecognised -- is $0.01
    below $3.00 and $0.05 at and above.
    """
    if _root(underlying) in ALWAYS_PENNY_SYMBOLS:
        return _PENNY
    return _NICKEL if price >= _TICK_THRESHOLD else _PENNY


def _warn_if_unverified(underlying: str) -> None:
    """One line per process per unrecognised root, not one per order."""
    root = _root(underlying)
    if not root or root in ALWAYS_PENNY_SYMBOLS:
        return
    if root in VERIFIED_PENNY_PROGRAM_ROOTS or root in _WARNED_UNVERIFIED_ROOTS:
        return
    _WARNED_UNVERIFIED_ROOTS.add(root)
    logger.warning(
        "Pricing an underlying whose tick class was never verified - "
        "assuming penny-program ($0.01 under $3.00, $0.05 at and above)",
        event_category="data",
        event_type="tick_class_unverified",
        symbol=root,
    )


def pricing_log_fields(quote: ExecutionQuote, priced: PricedLimit) -> Dict[str, Any]:
    """The `*_sale_executing` pricing fields, identical on both legs.

    One function rather than two call sites so the call leg and the put leg
    cannot emit different field sets -- the readout in `docs/plans/fc-072.md`
    compares them directly, and a field present on one leg only would silently
    halve its sample.

    ``quote_feed`` is on every line on purpose: this account prices off Alpaca's
    *indicative* feed until the OPRA agreement is signed, and that is a
    precondition to trading real money, not a footnote.
    """
    return {
        "bid": quote.bid,
        "ask": quote.ask,
        "mid": priced.mid,
        "spread": priced.spread,
        # The fraction applied to the midpoint. None means the book was unusable
        # and the premium fallback priced the order; on a one-tick book it is
        # the configured value that `one_tick_book` overrode.
        "spread_fraction": priced.spread_fraction,
        "quote_source": quote.source,
        "quote_age_s": quote.age_s,
        # None on the live path; "api_error" / "empty" / "unusable_live_book"
        # otherwise. Without it a book we actively REFUSED looks identical to a
        # broker that never answered.
        "quote_fallback_reason": quote.fallback_reason,
        "quote_feed": quote.feed,
        "quote_ts": quote.ts,
        "tick_snapped": priced.tick_snapped,
        "one_tick_book": priced.one_tick_book,
        "limit_price": priced.limit_price,
    }


def executing_quote_fields(quote: ExecutionQuote,
                           priced: PricedLimit) -> Dict[str, Any]:
    """The executing quote, for the seller's result dict and thence the journal.

    ``execution_engine.execute_batch`` used to build its `options_wheel.trades`
    row from ``{**opportunity, "limit_price": ...}``, which put the :00 scan
    blob's bid/ask/premium next to a limit priced off the :15 live book, and
    left ``mid_price`` NULL. Any analysis of "where did we price relative to the
    market" read the wrong book. These fields travel back with the result so the
    journal records what the order was actually priced off.
    """
    return {
        "bid": quote.bid,
        "ask": quote.ask,
        "mid": priced.mid,
        "quote_source": quote.source,
        "quote_age_s": quote.age_s,
        "tick_snapped": priced.tick_snapped,
    }


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


def quote_age_seconds(stamped_at: Any) -> Optional[float]:
    """Seconds between a timestamp and now, or None if it cannot be read.

    Two callers. For a blob quote this reads ``scan_timestamp`` off the
    opportunity -- the stamp the scanner has always written, produced by
    ``clock.now()``, which is naive local time (UTC in Cloud Run). For a live
    quote it reads the broker's own quote timestamp, which is aware UTC. A naive
    stamp is compared against ``clock.now()`` and an aware one against
    ``clock.now_utc()``; mixing them would raise.

    Never raises: an unreadable stamp is None, which is a log field, not an
    order decision. A negative value is returned as-is rather than clamped -- it
    means clock skew, which is worth seeing in the logs.
    """
    if isinstance(stamped_at, datetime):
        parsed = stamped_at
    elif isinstance(stamped_at, str) and stamped_at.strip():
        try:
            parsed = datetime.fromisoformat(
                stamped_at.strip().replace("Z", "+00:00"))
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
                  log: Any = None) -> ExecutionQuote:
    """Re-quote at execute time; fall back to the scan-time quote.

    The order is NEVER failed because the refresh failed -- a stale quote prices
    a worse limit, a refused order costs the whole cycle. Every fallback is
    logged as ``quote_refresh_failed`` with a ``reason``, and the reason is
    carried on the executing event as ``quote_fallback_reason`` so the three
    populations are separable in the readout:

    * ``api_error`` -- the call raised.
    * ``empty`` -- the broker returned nothing for the contract.
    * ``unusable_live_book`` -- a quote came back, but one-sided or crossed.
      This one used to be silent, which meant a book we actively refused looked
      identical in the logs to a broker that never answered.

    Note for replay: this calls whatever client it is handed.
    ``BacktestAlpacaClient.get_option_quote`` reads the same frozen daily chain
    snapshot the opportunity came from, so a backtest re-quote is offline and
    returns the identical book -- no network, and deterministic by construction.
    """
    live: Dict[str, Any] = {}
    reason: Optional[str] = None
    error: Optional[str] = None
    try:
        live = alpaca.get_option_quote(option_symbol) or {}
    except Exception as exc:  # never fail the order on a quote refresh
        error = str(exc)
        reason = "api_error"

    bid, ask = live.get("bid"), live.get("ask")
    feed = live.get("feed")
    ts = live.get("timestamp")

    if reason is None and not live:
        reason = "empty"
    elif reason is None and not quote_is_usable(bid, ask):
        reason = "unusable_live_book"

    if reason is None:
        # A live quote's age is measured from the BROKER's stamp: a halted or
        # thin name's "latest" quote can be hours old, and reporting 0.0 would
        # launder that into apparent freshness. None when there is no stamp.
        return ExecutionQuote(float(bid), float(ask), "live",
                              quote_age_seconds(ts), None, feed,
                              ts.isoformat() if isinstance(ts, datetime) else ts)

    if log is not None:
        log.warning(
            "Execute-time quote refresh unusable - pricing off the scan-time quote",
            event_category="data",
            event_type="quote_refresh_failed",
            symbol=option_symbol,
            reason=reason,
            error=error,
            live_bid=bid,
            live_ask=ask,
        )

    return ExecutionQuote(
        opportunity.get("bid"),
        opportunity.get("ask"),
        "blob",
        quote_age_seconds(opportunity.get("scan_timestamp")),
        reason,
        feed,
        None,
    )


def round_to_tick(value: Decimal, underlying: str = "") -> Tuple[float, bool]:
    """Round a SELL limit to a legal increment. Returns ``(price, snapped)``.

    Always-penny roots round to the cent, half-up, at every price level.
    Everything else rounds to the cent below $3.00 and **UP** to the next $0.05
    at and above it. ``snapped`` is True only when the $0.05 rule moved the
    price off the cent-rounded value.

    The $3.00 test is on the UNROUNDED value: a raw 2.995 is a sub-$3 price, so
    it rounds half-up to 3.00 -- which is itself on a $0.05 tick, so nothing is
    lost by not re-testing after rounding.
    """
    _warn_if_unverified(underlying)
    cent = value.quantize(_PENNY, rounding=ROUND_HALF_UP)
    if tick_size(value, underlying) == _NICKEL:
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
        underlying: Ticker, for the tick class.

    Returns:
        A :class:`PricedLimit`.
    """
    bid_f, ask_f = _as_number(bid), _as_number(ask)
    spread = (ask_f - bid_f) if (bid_f is not None and ask_f is not None) else None

    if quote_is_usable(bid_f, ask_f):
        bid_d, ask_d = _dec(bid_f), _dec(ask_f)
        mid_d = (bid_d + ask_d) / 2
        spread_d = ask_d - bid_d

        # A one-tick book has no midpoint to rest at: the only two prices in the
        # market are the bid and the ask, and "mid rounded half-up" is the ask
        # under another name. Resting at the ask on a low-delta call costs more
        # in missed cycles than it captures, so take the bid.
        if spread_d == tick_size(mid_d, underlying):
            _warn_if_unverified(underlying)
            return PricedLimit(float(bid_d), float(mid_d), float(spread_d),
                               float(spread_fraction), False, True)

        raw = mid_d + _dec(spread_fraction) * spread_d
        limit, snapped = round_to_tick(raw, underlying)
        return PricedLimit(limit, float(mid_d), float(spread_d),
                           float(spread_fraction), snapped, False)

    premium = _as_number(fallback_premium) or 0.0
    limit, snapped = round_to_tick(
        _dec(premium) * FALLBACK_PREMIUM_DISCOUNT, underlying)
    return PricedLimit(limit, None, spread, None, snapped, False)
