#!/usr/bin/env python3
"""Detailed analysis of UNH expired options premiums (9/23 → 9/26)."""

import os
import sys
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import pandas as pd
from dotenv import load_dotenv

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.utils.config import Config
from src.utils.option_symbols import (
    parse_option_symbol as parse_occ_symbol,
    strict_option_type,
)
from alpaca.data import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame


class DetailedExpiredOptionsAnalyzer:
    """Detailed analysis of expired options premiums using OptionBarsRequest."""

    def __init__(self, config_path: str = "config/settings.yaml"):
        """Initialize analyzer.

        Args:
            config_path: Path to configuration file
        """
        load_dotenv()
        self.config = Config(config_path)

        self.option_client = OptionHistoricalDataClient(
            api_key=self.config.alpaca_api_key,
            secret_key=self.config.alpaca_secret_key
        )

        self.stock_client = StockHistoricalDataClient(
            api_key=self.config.alpaca_api_key,
            secret_key=self.config.alpaca_secret_key
        )

    def get_historical_stock_price(self, symbol: str, date: str) -> Optional[float]:
        """Get historical stock price for a specific date.

        Args:
            symbol: Stock symbol
            date: Date string (YYYY-MM-DD)

        Returns:
            Stock closing price or None
        """
        try:
            target_date = datetime.strptime(date, '%Y-%m-%d')

            request = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=TimeFrame.Day,
                start=target_date,
                end=target_date + timedelta(days=1)
            )

            bars = self.stock_client.get_stock_bars(request)
            df = bars.df

            if not df.empty:
                # Reset index and filter for our symbol
                df = df.reset_index()
                symbol_data = df[df['symbol'] == symbol]
                if not symbol_data.empty:
                    return float(symbol_data['close'].iloc[0])

            return None

        except Exception as e:
            print(f"❌ Error getting stock price: {e}")
            return None

    def generate_comprehensive_option_symbols(self, underlying: str, expiration: str,
                                            stock_price: float) -> List[str]:
        """Generate comprehensive list of option symbols around stock price.

        Args:
            underlying: Stock symbol
            expiration: Expiration date (YYYY-MM-DD)
            stock_price: Current stock price for strike selection

        Returns:
            List of option symbols
        """
        exp_date = datetime.strptime(expiration, '%Y-%m-%d')
        exp_str = exp_date.strftime('%y%m%d')

        # Generate strikes around stock price (±10% range with $2.50 increments)
        base_strike = round(stock_price / 2.5) * 2.5
        strikes = []

        # Add strikes from -10% to +10% in $2.50 increments
        for i in range(-8, 9):  # -20 to +20 dollars in $2.50 steps
            strike = base_strike + (i * 2.5)
            if strike > 0:
                strikes.append(strike)

        symbols = []
        for strike in strikes:
            strike_str = f"{int(strike * 1000):08d}"

            # Generate both puts and calls
            put_symbol = f"{underlying}{exp_str}P{strike_str}"
            call_symbol = f"{underlying}{exp_str}C{strike_str}"

            symbols.extend([put_symbol, call_symbol])

        return symbols

    def get_expired_options_data(self, symbols: List[str], date: str) -> pd.DataFrame:
        """Get historical options data for expired contracts.

        Args:
            symbols: List of option symbols
            date: Historical date (YYYY-MM-DD)

        Returns:
            DataFrame with options data
        """
        try:
            target_date = datetime.strptime(date, '%Y-%m-%d')

            print(f"🔍 Fetching data for {len(symbols)} option symbols on {date}")

            # Process in batches to avoid API limits
            batch_size = 25
            all_data = []

            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i + batch_size]
                print(f"   Processing batch {i//batch_size + 1}/{(len(symbols)-1)//batch_size + 1}")

                try:
                    bars_request = OptionBarsRequest(
                        symbol_or_symbols=batch,
                        timeframe=TimeFrame.Day,
                        start=target_date,
                        end=target_date + timedelta(days=1)
                    )

                    bars_response = self.option_client.get_option_bars(bars_request)

                    if hasattr(bars_response, 'df') and not bars_response.df.empty:
                        batch_df = bars_response.df.copy()
                        all_data.append(batch_df)

                except Exception as e:
                    print(f"   ⚠️  Batch error: {e}")
                    continue

            if all_data:
                combined_df = pd.concat(all_data, ignore_index=False)
                print(f"✅ Successfully retrieved data for {len(combined_df)} option records")
                return combined_df
            else:
                print(f"❌ No data retrieved")
                return pd.DataFrame()

        except Exception as e:
            print(f"❌ Error fetching options data: {e}")
            return pd.DataFrame()

    def parse_option_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Parse an option symbol via the canonical OCC parsers (FC-079).

        This used to split on the first ``'P'`` / ``'C'`` found anywhere in the
        string, which mis-parses every root containing one (AAPL, SPY, PFE).
        Kept as a thin adapter so the script's existing call sites and dict
        shape are unchanged; the parsing itself is now
        ``src/utils/option_symbols``.

        Args:
            symbol: Option symbol (e.g., UNH250926P00340000)

        Returns:
            Dict with parsed details or None
        """
        option_type = strict_option_type(symbol)
        if option_type is None:
            return None

        parsed = parse_occ_symbol(symbol)
        expiration = parsed.get('expiration_date')
        if not expiration:
            return None

        return {
            'underlying': parsed['underlying'],
            'expiration': datetime.strptime(expiration, '%Y-%m-%d'),
            'option_type': option_type.upper(),
            'strike_price': parsed['strike_price'],
        }

    def analyze_options_data(self, df: pd.DataFrame, stock_price: float,
                           analysis_date: str) -> Dict[str, Any]:
        """Analyze the options data and apply strategy criteria.

        Args:
            df: Options data DataFrame
            stock_price: Stock price on analysis date
            analysis_date: Analysis date string

        Returns:
            Analysis results
        """
        print(f"\n📊 ANALYZING OPTIONS DATA")
        print("=" * 50)

        if df.empty:
            return {'error': 'No options data to analyze'}

        # Parse all symbols and add details
        parsed_options = []

        if hasattr(df.index, 'get_level_values'):
            symbols = df.index.get_level_values('symbol').unique()
        else:
            symbols = df.index.unique() if 'symbol' not in df.columns else df['symbol'].unique()

        for symbol in symbols:
            parsed = self.parse_option_symbol(symbol)
            if parsed:
                # Get the data for this symbol
                if hasattr(df.index, 'get_level_values'):
                    symbol_data = df.loc[symbol]
                else:
                    symbol_data = df[df.index == symbol] if 'symbol' not in df.columns else df[df['symbol'] == symbol]

                if not symbol_data.empty:
                    # Take the first row if multiple
                    if len(symbol_data) > 1:
                        symbol_data = symbol_data.iloc[0]

                    option_info = {
                        'symbol': symbol,
                        'underlying': parsed['underlying'],
                        'option_type': parsed['option_type'],
                        'strike_price': parsed['strike_price'],
                        'expiration': parsed['expiration'],
                        'close': float(symbol_data['close']) if 'close' in symbol_data else 0.0,
                        'volume': int(symbol_data['volume']) if 'volume' in symbol_data else 0,
                        'open': float(symbol_data['open']) if 'open' in symbol_data else 0.0,
                        'high': float(symbol_data['high']) if 'high' in symbol_data else 0.0,
                        'low': float(symbol_data['low']) if 'low' in symbol_data else 0.0,
                    }

                    # Calculate additional metrics
                    option_info['moneyness'] = stock_price / parsed['strike_price'] if parsed['strike_price'] > 0 else 0
                    option_info['itm'] = (
                        (parsed['option_type'] == 'PUT' and stock_price < parsed['strike_price']) or
                        (parsed['option_type'] == 'CALL' and stock_price > parsed['strike_price'])
                    )

                    parsed_options.append(option_info)

        # Filter for puts only (wheel strategy focus)
        puts = [opt for opt in parsed_options if opt['option_type'] == 'PUT']

        print(f"Total options found: {len(parsed_options)}")
        print(f"Put options: {len(puts)}")
        print(f"Call options: {len(parsed_options) - len(puts)}")

        if not puts:
            return {'error': 'No put options found'}

        # Apply wheel strategy criteria
        strategy_puts = []
        min_premium = self.config.min_put_premium

        for put in puts:
            # Basic filters
            if (put['close'] >= min_premium and
                put['volume'] > 0 and
                put['close'] > 0):

                # Calculate approximate delta (simplified)
                # For puts: delta ≈ -1 + (stock_price / strike_price) for OTM puts
                if put['strike_price'] <= stock_price:  # OTM puts
                    approx_delta = abs(1 - (stock_price / put['strike_price']))
                else:  # ITM puts
                    approx_delta = abs((put['strike_price'] - stock_price) / put['strike_price'])

                put['approx_delta'] = approx_delta

                # Check if in our target delta range
                delta_min, delta_max = self.config.put_delta_range
                if delta_min <= approx_delta <= delta_max:
                    strategy_puts.append(put)

        # Sort by premium (highest first)
        strategy_puts.sort(key=lambda x: x['close'], reverse=True)

        # Calculate statistics
        if strategy_puts:
            premiums = [put['close'] for put in strategy_puts]
            deltas = [put['approx_delta'] for put in strategy_puts]
            strikes = [put['strike_price'] for put in strategy_puts]

            analysis = {
                'analysis_date': analysis_date,
                'stock_price': stock_price,
                'total_puts': len(puts),
                'strategy_suitable_puts': len(strategy_puts),
                'premium_stats': {
                    'min': min(premiums),
                    'max': max(premiums),
                    'avg': sum(premiums) / len(premiums),
                    'median': sorted(premiums)[len(premiums)//2]
                },
                'delta_stats': {
                    'min': min(deltas),
                    'max': max(deltas),
                    'avg': sum(deltas) / len(deltas)
                },
                'strike_stats': {
                    'min': min(strikes),
                    'max': max(strikes),
                    'avg': sum(strikes) / len(strikes)
                },
                'top_options': strategy_puts[:10]  # Top 10 options
            }

            return analysis
        else:
            return {'error': 'No options meeting strategy criteria'}

    def print_detailed_analysis(self, analysis: Dict[str, Any]):
        """Print detailed analysis results.

        Args:
            analysis: Analysis results dictionary
        """
        if 'error' in analysis:
            print(f"❌ {analysis['error']}")
            return

        print(f"\n📈 DETAILED EXPIRED OPTIONS ANALYSIS")
        print("=" * 70)
        print(f"Analysis Date: {analysis['analysis_date']}")
        print(f"UNH Stock Price: ${analysis['stock_price']:.2f}")
        print(f"Total Put Options: {analysis['total_puts']}")
        print(f"Strategy-Suitable Options: {analysis['strategy_suitable_puts']}")

        if analysis['strategy_suitable_puts'] > 0:
            premium_stats = analysis['premium_stats']
            delta_stats = analysis['delta_stats']
            strike_stats = analysis['strike_stats']

            print(f"\n💰 PREMIUM ANALYSIS:")
            print(f"  Range: ${premium_stats['min']:.2f} - ${premium_stats['max']:.2f}")
            print(f"  Average: ${premium_stats['avg']:.2f}")
            print(f"  Median: ${premium_stats['median']:.2f}")

            print(f"\n📊 DELTA ANALYSIS (Approximate):")
            print(f"  Range: {delta_stats['min']:.3f} - {delta_stats['max']:.3f}")
            print(f"  Average: {delta_stats['avg']:.3f}")

            print(f"\n🎯 STRIKE ANALYSIS:")
            print(f"  Range: ${strike_stats['min']:.2f} - ${strike_stats['max']:.2f}")
            print(f"  Average: ${strike_stats['avg']:.2f}")

            print(f"\n🏆 TOP 10 OPTIONS BY PREMIUM:")
            print("-" * 110)
            print(f"{'Strike':<8} {'Premium':<8} {'Volume':<8} {'Delta':<8} {'Moneyness':<10} {'ITM':<5} {'Symbol'}")
            print("-" * 110)

            for option in analysis['top_options']:
                strike = option['strike_price']
                premium = option['close']
                volume = option['volume']
                delta = option['approx_delta']
                moneyness = option['moneyness']
                itm = 'Yes' if option['itm'] else 'No'
                symbol = option['symbol']

                print(f"{strike:<8.1f} ${premium:<7.2f} {volume:<8} {delta:<8.3f} {moneyness:<10.3f} {itm:<5} {symbol}")

        # Compare with current data
        print(f"\n🔄 COMPARISON WITH CURRENT UNH OPTIONS:")
        print("-" * 50)
        print(f"Historical (9/23 → 9/26): 3-day expired options")
        if 'premium_stats' in analysis:
            print(f"  Average Premium: ${analysis['premium_stats']['avg']:.2f}")
            print(f"  Premium Range: ${analysis['premium_stats']['min']:.2f} - ${analysis['premium_stats']['max']:.2f}")
        print(f"Current (9/29 → 10/3): 4-day active options")
        print(f"  Average Premium: $0.93 (from previous validation)")
        print(f"  Premium Range: $0.58 - $1.31")

        if 'premium_stats' in analysis:
            hist_avg = analysis['premium_stats']['avg']
            current_avg = 0.93
            diff = ((current_avg - hist_avg) / hist_avg * 100) if hist_avg > 0 else 0

            print(f"\n📊 PREMIUM COMPARISON:")
            print(f"  Difference: {diff:+.1f}%")

            if abs(diff) < 25:
                print(f"  ✅ Premiums are consistent between historical and current periods")
            else:
                print(f"  ⚠️  Significant difference detected")


def main():
    """Main analysis function."""
    print("🔍 DETAILED EXPIRED OPTIONS PREMIUM ANALYSIS")
    print("=" * 70)
    print("Analyzing UNH expired options: September 23rd → September 26th expiration")

    # Initialize analyzer
    try:
        analyzer = DetailedExpiredOptionsAnalyzer()
    except Exception as e:
        print(f"❌ Failed to initialize analyzer: {e}")
        return

    # Parameters
    symbol = "UNH"
    analysis_date = "2025-09-23"  # 3 days before expiration
    expiration_date = "2025-09-26"  # Expired expiration

    print(f"\nTarget Analysis:")
    print(f"  Symbol: {symbol}")
    print(f"  Date: {analysis_date}")
    print(f"  Expiration: {expiration_date}")
    print(f"  Days to Expiration: 3")

    # Get historical stock price
    stock_price = analyzer.get_historical_stock_price(symbol, analysis_date)
    if not stock_price:
        print(f"❌ Could not get stock price for {analysis_date}")
        return

    print(f"  Stock Price: ${stock_price:.2f}")

    # Generate option symbols
    option_symbols = analyzer.generate_comprehensive_option_symbols(
        symbol, expiration_date, stock_price
    )

    print(f"\nGenerated {len(option_symbols)} option symbols to analyze")

    # Get options data
    options_df = analyzer.get_expired_options_data(option_symbols, analysis_date)

    if options_df.empty:
        print(f"❌ No options data retrieved")
        return

    # Analyze the data
    analysis = analyzer.analyze_options_data(options_df, stock_price, analysis_date)

    # Print results
    analyzer.print_detailed_analysis(analysis)

    print(f"\n✅ Analysis complete!")


if __name__ == '__main__':
    main()