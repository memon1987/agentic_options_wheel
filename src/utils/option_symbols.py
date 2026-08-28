"""
Option symbol generation utilities for historical backtesting.

Generates proper option symbols based on OCC (Options Clearing Corporation) format:
UNDERLYING + YY + MM + DD + C/P + STRIKE*1000 (8 digits)

Example: AAPL250117C00185000
- AAPL: Underlying symbol  
- 25: Year (2025)
- 01: Month (January)
- 17: Day (17th)
- C: Call (P for Put)
- 00185000: Strike price $185.00 * 1000 with padding
"""

from datetime import date, datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
import calendar
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


class OptionSymbolGenerator:
    """Generate option symbols for backtesting with historical data."""
    
    def __init__(self):
        self.weekly_symbols = ['AAPL', 'SPY', 'QQQ', 'IWM', 'UNH', 'MSFT', 'GOOGL', 'NVDA']
        
    def get_expiration_dates(self, start_date: datetime, max_dte: int = 60) -> List[datetime]:
        """Get all option expiration dates within the specified DTE range.
        
        Args:
            start_date: Starting date for calculations
            max_dte: Maximum days to expiration to include
            
        Returns:
            List of expiration dates (Fridays) within the DTE range
        """
        expirations = []
        current_date = start_date
        end_date = start_date + timedelta(days=max_dte)
        
        # Find all Fridays within the range
        while current_date <= end_date:
            if current_date.weekday() == 4:  # Friday = 4
                expirations.append(current_date)
            current_date += timedelta(days=1)
        
        return expirations
    
    def get_monthly_expiration(self, year: int, month: int) -> datetime:
        """Get the monthly expiration date (3rd Friday) for a given month.
        
        Args:
            year: Year
            month: Month (1-12)
            
        Returns:
            Monthly expiration date
        """
        # Find first day of month
        first_day = datetime(year, month, 1)
        
        # Find first Friday
        days_ahead = 4 - first_day.weekday()  # Friday = 4
        if days_ahead <= 0:  # Target day already happened this week
            days_ahead += 7
        
        first_friday = first_day + timedelta(days=days_ahead)
        
        # Third Friday is first Friday + 14 days
        third_friday = first_friday + timedelta(days=14)
        
        return third_friday
    
    def generate_strike_prices(self, underlying_price: float, underlying: str) -> List[float]:
        """Generate reasonable strike prices around the current stock price.
        
        Args:
            underlying_price: Current stock price
            underlying: Stock symbol for strike spacing rules
            
        Returns:
            List of strike prices to test
        """
        strikes = []
        
        # Determine strike spacing based on stock price
        if underlying_price < 50:
            spacing = 1.0  # $1 increments
            range_factor = 0.20  # ±20%
        elif underlying_price < 100:
            spacing = 2.5  # $2.50 increments
            range_factor = 0.15  # ±15%
        elif underlying_price < 200:
            spacing = 5.0  # $5 increments
            range_factor = 0.15  # ±15%
        else:
            spacing = 10.0  # $10 increments
            range_factor = 0.10  # ±10%
        
        # Generate strikes around current price
        min_strike = underlying_price * (1 - range_factor)
        max_strike = underlying_price * (1 + range_factor)
        
        current_strike = int(min_strike / spacing) * spacing
        while current_strike <= max_strike:
            if current_strike > 0:
                strikes.append(current_strike)
            current_strike += spacing
        
        return strikes
    
    def format_option_symbol(self, underlying: str, expiration: datetime, 
                           option_type: str, strike: float) -> str:
        """Format an option symbol according to OCC standards.
        
        Args:
            underlying: Stock symbol
            expiration: Expiration date
            option_type: 'PUT' or 'CALL'
            strike: Strike price
            
        Returns:
            Properly formatted option symbol
        """
        # Format: UNDERLYING + YY + MM + DD + C/P + STRIKE*1000 (8 digits)
        year_str = f"{expiration.year % 100:02d}"
        month_str = f"{expiration.month:02d}"
        day_str = f"{expiration.day:02d}"
        type_str = "C" if option_type.upper() == "CALL" else "P"
        
        # Strike as 8-digit integer (price * 1000, padded)
        strike_int = int(strike * 1000)
        strike_str = f"{strike_int:08d}"
        
        symbol = f"{underlying}{year_str}{month_str}{day_str}{type_str}{strike_str}"
        
        return symbol
    
    def generate_option_universe(self, underlying: str, date: datetime, 
                               underlying_price: float, max_dte: int = 45) -> List[Dict[str, Any]]:
        """Generate a universe of option symbols for a given date and stock.
        
        Args:
            underlying: Stock symbol
            date: Current trading date
            underlying_price: Current stock price
            max_dte: Maximum days to expiration
            
        Returns:
            List of option metadata dictionaries
        """
        options = []
        
        # Get expiration dates
        expirations = self.get_expiration_dates(date, max_dte)
        
        # Get strike prices  
        strikes = self.generate_strike_prices(underlying_price, underlying)
        
        # Generate all combinations
        for expiration in expirations:
            dte = (expiration - date).days
            if dte <= 0:  # Skip expired options
                continue
                
            for strike in strikes:
                for option_type in ['PUT', 'CALL']:
                    symbol = self.format_option_symbol(underlying, expiration, option_type, strike)
                    
                    options.append({
                        'symbol': symbol,
                        'underlying': underlying,
                        'expiration_date': expiration,
                        'option_type': option_type,
                        'strike_price': strike,
                        'dte': dte
                    })
        
        logger.debug("Generated option universe", 
                    underlying=underlying, 
                    date=date.date(),
                    total_options=len(options),
                    expirations=len(expirations),
                    strikes=len(strikes))
        
        return options
    
    def validate_symbol_format(self, symbol: str) -> bool:
        """Validate that an option symbol follows the correct format.
        
        Args:
            symbol: Option symbol to validate
            
        Returns:
            True if valid format
        """
        try:
            # Basic length check (varies by underlying length)
            if len(symbol) < 15:
                return False
            
            # Should end with 8 digits
            if not symbol[-8:].isdigit():  # occ-substring-allowed
                return False
            
            # Should have C or P before the strike
            if symbol[-9] not in ['C', 'P']:  # occ-substring-allowed
                return False
            
            # Date part should be 6 digits  
            date_part = symbol[-15:-9]
            if not date_part.isdigit() or len(date_part) != 6:
                return False
            
            return True
            
        except Exception:
            return False


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
                # occ-substring-allowed — the documented last-resort heuristic.
                # This is the *only* blessed instance of the idiom in the
                # tree (FC-079 / tests/test_no_occ_substring.py). It exists so
                # messy historical input degrades instead of raising; callers
                # that must not inherit the guess use strict_option_type().
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


# Global instance for easy access
option_symbol_generator = OptionSymbolGenerator()