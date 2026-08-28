"""OCC option-symbol parsing — the canonical primitives.

OCC contract format: UNDERLYING + YYMMDD + C/P + STRIKE*1000 (8 digits).

Example: AAPL250117C00185000
- AAPL: Underlying symbol
- 250117: Expiration (2025-01-17)
- C: Call (P for Put)
- 00185000: Strike price $185.00 * 1000 with padding

Two parsers, and the choice between them matters:

* ``strict_option_type`` — fully anchored, returns ``None`` for anything that
  is not an exact OCC contract. Use it whenever the answer decides which order
  gets placed, or which leg a position is counted as.
* ``parse_option_symbol`` — tolerant, for messy historical input. Its
  last-resort branch guesses the type from a substring, which is the bug family
  behind FC-041/043/045/048/052/054/079. That branch is the ONE place in the
  tree the idiom is allowed, and it carries an ``# occ-substring-allowed``
  marker; ``tests/test_no_occ_substring.py`` enforces that there is no other.

FC-079 deleted ``OptionSymbolGenerator`` (symbol *generation* for the
never-completed backtest strike search) and its module-level instance. The
class had zero references anywhere in the tree — it was carried since FC-032,
which explicitly left it "pending a Phase 1 decision" that never came — and its
``validate_symbol_format`` was a third copy of the OCC layout, drifting
independently from ``OCC_STRICT_RE``.
"""

from datetime import date, datetime, timezone
from typing import Dict, Any, Optional
import re
import structlog

from . import clock

logger = structlog.get_logger(__name__)


def coerce_expiry_date(value) -> Optional[date]:
    """Normalize an expiration to a plain ``date``, or None if unusable.

    Contracts arrive with ``expiration_date`` as an ISO string from both the
    live client (``parse_option_symbol``) and the backtest adapter
    (``quote.expiration.isoformat()``), but ``get_option_chain_with_analysis``
    also tolerates ``datetime``, and test fixtures pass ``date``. One coercion
    so an expiry comparison cannot silently compare a str to a date.

    Lives here (FC-078) rather than in ``market_data`` because the roll-horizon
    bound in ``RiskManager.validate_roll`` needs the same coercion, and a
    second copy of a date parser is how two gates end up disagreeing about
    what a contract expires on.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10].replace('Z', '')).date()
    except (TypeError, ValueError):
        return None


# Fully-anchored OCC contract symbol: ROOT + YYMMDD + C/P + STRIKE*1000.
# Deliberately strict — no adjusted roots (leading digits), no dotted tickers.
OCC_STRICT_RE = re.compile(r'^([A-Z]{1,6})(\d{6})([PC])(\d{8})$')


def strict_option_type(option_symbol: str) -> Optional[str]:
    """``'put'`` / ``'call'`` from a fully-anchored OCC symbol, else ``None``.

    Use this — never ``parse_option_symbol`` — when the answer decides *which
    order gets placed*. ``parse_option_symbol`` has a last-resort heuristic
    (``'C' if 'C' in symbol``) for tolerating messy historical input, which is
    the substring-matching family behind FC-041/FC-043/FC-045. It resolves a
    bare ``'AAPL'`` to ``'put'`` and ``'NOT_AN_OCC'`` to ``'call'`` — routing on
    that would hand a non-contract to a seller and place a plain equity order.

    Returns None for anything that is not an exact OCC contract symbol, so the
    caller must decide explicitly what to do rather than inheriting a guess.
    """
    if not isinstance(option_symbol, str):
        return None
    m = OCC_STRICT_RE.match(option_symbol.strip().upper())
    if not m:
        return None
    return 'call' if m.group(3) == 'C' else 'put'


def parse_option_symbol(option_symbol: str, underlying_hint: Optional[str] = None) -> Dict[str, Any]:
    """Parse an OCC-format option symbol into its components.

    OCC format: UNDERLYING + YYMMDD + C/P + STRIKE*1000 (8 digits)
    Example: AAPL250117C00185000 -> AAPL, 2025-01-17, call, $185.00

    Args:
        option_symbol: Full OCC option symbol string
        underlying_hint: Optional underlying symbol to assist parsing
            (used when the underlying length is ambiguous)

    Returns:
        Dictionary with keys:
            underlying: str - underlying stock symbol
            expiration_date: str or None - "YYYY-MM-DD" format
            option_type: str - "call", "put", or "unknown"
            strike_price: float - strike price in dollars
            dte: int - days to expiration (0 if expired)
    """
    if not option_symbol:
        return {
            'underlying': '', 'expiration_date': None,
            'option_type': 'unknown', 'strike_price': 0.0, 'dte': 0,
        }

    result = {
        'underlying': option_symbol[:3] if len(option_symbol) >= 3 else option_symbol,
        'expiration_date': None,
        'option_type': 'unknown',
        'strike_price': 0.0,
        'dte': 0,
    }

    try:
        symbol = option_symbol.strip().upper()

        # Primary: fully-anchored OCC regex
        pattern = r'^([A-Z]{1,6})(\d{6})([PC])(\d{8})$'
        match = re.match(pattern, symbol)

        if match:
            underlying = match.group(1)
            date_str = match.group(2)
            type_char = match.group(3)
            strike_str = match.group(4)
        else:
            # Fallback: use underlying_hint or heuristic extraction
            if underlying_hint:
                underlying = underlying_hint.upper()
                remainder = symbol[len(underlying):]
            else:
                # Letters at start are the underlying
                ul_match = re.match(r'^([A-Z]+)', symbol)
                underlying = ul_match.group(1) if ul_match else symbol[:3]
                remainder = symbol[len(underlying):]

            # Extract date (6 digits) + type (P/C) + strike (8 digits)
            parts_match = re.match(r'^(\d{6})([PC])(\d{8})$', remainder)
            if parts_match:
                date_str = parts_match.group(1)
                type_char = parts_match.group(2)
                strike_str = parts_match.group(3)
            else:
                # Last-resort partial extraction
                date_match = re.search(r'(\d{6})[PC]', symbol)
                date_str = date_match.group(1) if date_match else None
                # The documented last-resort heuristic, and the ONLY blessed
                # instance of the idiom in the tree — the marker comment on the
                # line below is what exempts it, and
                # tests/test_no_occ_substring.py asserts that exactly one such
                # exemption exists (FC-079). It is here so messy historical
                # input degrades instead of raising; any caller that must not
                # inherit the guess uses strict_option_type().
                type_char = 'C' if 'C' in symbol else ('P' if 'P' in symbol else None)  # occ-substring-allowed
                strike_match = re.search(r'[PC](\d{8})$', symbol)
                strike_str = strike_match.group(1) if strike_match else None

        result['underlying'] = underlying

        # Parse option type
        if type_char == 'C':
            result['option_type'] = 'call'
        elif type_char == 'P':
            result['option_type'] = 'put'

        # Parse strike price
        if strike_str:
            result['strike_price'] = float(strike_str) / 1000.0

        # Parse expiration date and calculate DTE
        if date_str and len(date_str) == 6:
            year = 2000 + int(date_str[0:2])
            month = int(date_str[2:4])
            day = int(date_str[4:6])
            result['expiration_date'] = f"{year:04d}-{month:02d}-{day:02d}"

            exp_date = datetime(year, month, day, tzinfo=timezone.utc)
            now = clock.now_utc()
            result['dte'] = max(0, (exp_date.date() - now.date()).days)

    except Exception as e:
        logger.debug("Failed to parse option symbol",
                    event_category="data",
                    event_type="option_symbol_parse_error",
                    symbol=option_symbol,
                    error=str(e))

    return result
