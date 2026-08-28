"""FC-079 (G7) — the two ad-hoc analysis tools' symbol adapters.

`tools/testing/debug_expired_put_analysis.py` and
`detailed_expired_options_analysis.py` each carried their own
`parse_option_symbol`, both of which did `symbol.split('P')` / `symbol.split('C')`
on the first such letter anywhere in the string. On `PFE250926P00025000` that
splits on the P in the ROOT, and the "underlying" comes back as the empty
string. FC-079 replaced both bodies with thin adapters over
`src/utils/option_symbols`.

These are ad-hoc scripts, not production code, so what needs pinning is narrow:
the adapter's RETURN SHAPE is unchanged (their call sites read
`['underlying']`, `['expiration']`, `['option_type']`, `['strike_price']` and
would break silently on a renamed or retyped key), and an unparseable symbol
still returns `None` rather than a half-filled dict. The scripts import
`alpaca.data` and `pandas` at module scope, so the adapters are loaded by file
path rather than by package import — importing the modules would drag the
Alpaca SDK into the test run for no benefit.
"""

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(rel_path, module_name):
    """Import a tools/ script without executing its heavy imports eagerly."""
    pytest.importorskip("pandas")
    pytest.importorskip("alpaca.data")
    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def debug_parse():
    mod = _load("tools/testing/debug_expired_put_analysis.py",
                "fc079_debug_expired_put_analysis")
    return mod.parse_option_symbol


@pytest.fixture(scope="module")
def detailed_parse():
    mod = _load("tools/testing/detailed_expired_options_analysis.py",
                "fc079_detailed_expired_options_analysis")
    analyzer = mod.DetailedExpiredOptionsAnalyzer.__new__(
        mod.DetailedExpiredOptionsAnalyzer)
    return analyzer.parse_option_symbol


# The pre-change contract, key for key. Both scripts built exactly this dict.
EXPECTED_KEYS = {'underlying', 'expiration', 'option_type', 'strike_price'}


def _assert_shape(parsed, underlying, option_type, strike, expiry):
    assert set(parsed) == EXPECTED_KEYS, parsed
    assert isinstance(parsed['underlying'], str)
    assert isinstance(parsed['expiration'], datetime)
    assert isinstance(parsed['option_type'], str)
    assert isinstance(parsed['strike_price'], float)
    assert parsed['underlying'] == underlying
    assert parsed['option_type'] == option_type
    assert parsed['strike_price'] == strike
    assert parsed['expiration'] == expiry


@pytest.fixture(params=['debug', 'detailed'])
def adapter(request, debug_parse, detailed_parse):
    return debug_parse if request.param == 'debug' else detailed_parse


def test_a_strict_symbol_keeps_the_pre_change_shape(adapter):
    _assert_shape(adapter('UNH250926P00340000'),
                  underlying='UNH', option_type='PUT', strike=340.0,
                  expiry=datetime(2025, 9, 26))


def test_a_call_on_a_p_bearing_root_parses_correctly(adapter):
    """`'P' in 'PFE...'` is what the old `.split('P')` tripped over."""
    _assert_shape(adapter('PFE250926C00030000'),
                  underlying='PFE', option_type='CALL', strike=30.0,
                  expiry=datetime(2025, 9, 26))


def test_an_adjusted_root_returns_none(adapter):
    """Not a strict OCC contract: the adapter abstains rather than guessing.

    Pre-change these returned a dict with `underlying=''` (the split consumed
    the root) — a confident, wrong answer.
    """
    assert adapter('AAPL1250926C00230000') is None


@pytest.mark.parametrize("junk", ['', 'NOT_AN_OCC', 'AAPL', '123456'])
def test_junk_returns_none(adapter, junk):
    assert adapter(junk) is None
