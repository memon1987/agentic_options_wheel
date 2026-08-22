"""FC-067: the trade journal must label the option leg correctly.

Scanner opportunity dicts carry the leg under 'type' ('put'|'call') and no
'strategy' key. The journal previously read 'option_type'/'strategy' directly,
so {**opp} never supplied them and EVERY trade — puts and calls — was written
as option_type='put'/strategy='sell_put'. Covered calls were silently mislabeled
in BigQuery. These tests pin the fix: derive the leg from 'type' and the strategy
from the leg, with no wrong-leg guess when the trade genuinely carries none.
"""

from unittest.mock import Mock

import pytest

from src.data.trade_journal import TradeJournal


def _journal():
    """A TradeJournal wired to a fake BQ client (no real BigQuery)."""
    tj = TradeJournal.__new__(TradeJournal)
    tj._enabled = True
    tj._strategy_id = "wheel"     # FC-075 Seam 4: stamped onto every row
    tj._client = Mock()
    tj._client.insert_rows_json.return_value = []  # no insert errors
    tj._table_ref = "proj.options_wheel.trades"
    return tj


def _recorded_row(tj):
    """The single row dict passed to insert_rows_json."""
    args, _ = tj._client.insert_rows_json.call_args
    rows = args[1]
    assert len(rows) == 1
    return rows[0]


def test_call_opportunity_labeled_as_call():
    tj = _journal()
    # A covered-call opportunity as the scanner emits it: leg under 'type'.
    tj.record_trade({"symbol": "META", "type": "call", "contracts": 1,
                     "strike_price": 600, "premium": 3.10})
    row = _recorded_row(tj)
    assert row["option_type"] == "call"
    assert row["strategy"] == "sell_call"


def test_put_opportunity_still_labeled_as_put():
    tj = _journal()
    tj.record_trade({"symbol": "AAPL", "type": "put", "contracts": 1,
                     "strike_price": 190, "premium": 1.20})
    row = _recorded_row(tj)
    assert row["option_type"] == "put"
    assert row["strategy"] == "sell_put"


def test_occ_symbol_wins_over_contradictory_declared_type():
    # The label-vs-contract drift path (FC-048): execute_batch routes on the OCC
    # symbol and, on a declared/symbol mismatch, trades by the symbol. The audit
    # row must match what was traded — a C-contract must journal a call even if
    # the declared 'type' says put.
    tj = _journal()
    tj.record_trade({"type": "put", "strategy": "sell_put",
                     "option_symbol": "META260220C00600000"})
    row = _recorded_row(tj)
    assert row["option_type"] == "call"
    assert row["strategy"] == "sell_call"


def test_seller_shaped_dict_strategy_only_stays_consistent():
    # A dict carrying only 'strategy' (seller-shaped, no 'type'/symbol) must not
    # produce an inconsistent row (strategy=sell_call, option_type=null).
    tj = _journal()
    tj.record_trade({"symbol": "X", "strategy": "sell_call"})
    row = _recorded_row(tj)
    assert row["option_type"] == "call"
    assert row["strategy"] == "sell_call"


def test_explicit_option_type_and_strategy_win():
    tj = _journal()
    tj.record_trade({"symbol": "X", "type": "put",
                     "option_type": "call", "strategy": "sell_call"})
    row = _recorded_row(tj)
    assert row["option_type"] == "call"
    assert row["strategy"] == "sell_call"


def test_absent_leg_does_not_guess_put():
    # The old bug: a trade with no leg info was silently written as a put.
    # Now it writes null rather than a wrong guess.
    tj = _journal()
    tj.record_trade({"symbol": "X", "contracts": 1})
    row = _recorded_row(tj)
    assert row["option_type"] is None
    assert row["strategy"] is None


def test_disabled_journal_is_a_noop():
    tj = TradeJournal.__new__(TradeJournal)
    tj._enabled = False
    tj._strategy_id = "wheel"
    tj.record_trade({"type": "call"})  # must not raise / not touch a client
