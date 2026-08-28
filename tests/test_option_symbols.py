"""Tests for the OCC symbol primitives in ``src.utils.option_symbols``.

FC-041 added :func:`occ_root`, the one normalization used everywhere an equity
symbol meets an OCC contract root. Alpaca renders Berkshire's B shares as the
equity symbol ``BRK.B`` while the contracts written on them carry the root
``BRKB`` (verified against the paper API on 2026-08-28:
``get_asset('BRK.B').symbol == 'BRK.B'``;
``get_option_contracts(underlying_symbols=['BRK.B'])`` returns
``BRKB260828C00270000`` with ``root_symbol='BRKB'``). Every raw-string join
between those two spellings misses, and in the share ledger a miss means a
covered call is written over shares that are already committed.
"""

import pytest

from src.utils.option_symbols import (
    OCC_STRICT_RE, occ_root, parse_option_symbol, strict_option_type)


class TestOccRoot:
    """The normalization table. This is the whole contract of the function."""

    @pytest.mark.parametrize("equity_symbol,expected", [
        ("BRK.B", "BRKB"),      # Alpaca's live spelling for the class share
        ("brk.b", "BRKB"),      # case is normalized
        # `/` and `-` are NOT Alpaca forms -- get_asset('BRK/B') and
        # get_asset('BF-B') both 404. They are the other two conventional
        # class-share separators, seen in hand-entered config, CSV exports and
        # other vendors' feeds, and are harmless to strip (no listed US equity
        # ticker contains either character).
        ("BRK/B", "BRKB"),
        ("BF-B", "BFB"),
        ("AAPL", "AAPL"),       # plain tickers are the identity
        ("", ""),               # empty stays empty, never a crash
    ])
    def test_the_normalization_table(self, equity_symbol, expected):
        assert occ_root(equity_symbol) == expected

    def test_whitespace_is_stripped(self):
        """Positions data is JSON from a third party; a stray space must not
        silently create a second, non-matching underlying."""
        assert occ_root("  BRK.B  ") == "BRKB"
        assert occ_root("AAPL\n") == "AAPL"

    def test_non_strings_normalize_to_empty_rather_than_raising(self):
        """A malformed position must fail closed, not blow up the ledger."""
        assert occ_root(None) == ""
        assert occ_root(123) == ""
        assert occ_root(["BRK.B"]) == ""

    def test_it_is_idempotent(self):
        """Both sides of a join may already be normalized; applying it twice
        must not move the answer."""
        for sym in ("BRK.B", "AAPL", "BF-B", ""):
            assert occ_root(occ_root(sym)) == occ_root(sym)

    def test_an_occ_contract_root_normalizes_to_itself(self):
        """The point of the join: the contract side is already the answer."""
        assert occ_root("BRKB") == "BRKB"
        assert occ_root("BRK.B") == occ_root("BRKB")

    def test_it_does_not_collapse_distinct_tickers(self):
        """Dropping separators must not manufacture collisions between real,
        distinct tickers — the failure mode that would make this fix worse than
        the bug."""
        assert occ_root("F") != occ_root("PFE")
        assert occ_root("BRK.A") != occ_root("BRK.B")


class TestOccRootDoesNotLoosenTheStrictParser:
    """DD-1: a dotted OCC symbol does not exist; the equity side normalizes.

    If a future change ever loosened ``OCC_STRICT_RE`` to admit dots instead,
    ``strict_option_type`` would start classifying strings that are not
    tradeable contracts — the exact heuristic-routing failure FC-048 removed.
    """

    def test_the_strict_regex_still_rejects_a_dotted_root(self):
        assert OCC_STRICT_RE.match("BRK.B260828C00270000") is None
        assert strict_option_type("BRK.B260828C00270000") is None

    def test_the_undotted_contract_still_parses(self):
        assert strict_option_type("BRKB260828C00270000") == "call"
        assert parse_option_symbol(
            "BRKB260828C00270000")["underlying"] == "BRKB"
