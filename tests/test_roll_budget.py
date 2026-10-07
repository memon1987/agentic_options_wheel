"""FC-120 PR-2 T-9 — the roller's per-position budget, as a function.

``src/strategy/roll_budget.py`` owns the timing constants and the DD-4 formula
(docs/plans/fc-120.md). These tests pin its arithmetic on the shapes the plan
names; T-19 (``tests/test_call_roller.py``) MEASURES the real roller against it,
and the consumers (``Config``, ``run_rolling_cycle``, the cloudbuild seam test)
are pinned where they live.

*Catches:* FC-113 (a) regressing to a literal; a read added to the formula (or
dropped from it) without the plan's numbers following; the module growing a
``src/`` import (the cycle it exists to avoid).
"""

import ast
from pathlib import Path

import pytest

from src.strategy import roll_budget
from src.strategy.roll_budget import per_position_budget_seconds

REPO = Path(__file__).resolve().parent.parent

DEFAULTS = dict(btc_fill_timeout_seconds=120, btc_reprice_attempts=2,
                stc_rung_timeout_seconds=30, stc_escalation_rungs=0,
                fallback_strike_attempts=2)


class TestThePlansNumbers:
    """DD-4's worked numbers, each a different ladder shape."""

    def test_the_shipped_defaults_are_567(self):
        assert per_position_budget_seconds(**DEFAULTS) == 567

    def test_two_escalation_rungs_are_715(self):
        assert per_position_budget_seconds(
            **{**DEFAULTS, 'stc_escalation_rungs': 2}) == 715

    def test_the_pre_pr2_ladder_shape_is_827(self):
        """One BTC placement over 120 s and 120 s STO rungs — what main ran,
        counted with the reads, trailing reads, cancel RTT and settle floor
        that the old literal 600 ignored."""
        assert per_position_budget_seconds(
            **{**DEFAULTS, 'btc_reprice_attempts': 0,
               'stc_rung_timeout_seconds': 120}) == 827

    def test_no_fallbacks_is_415(self):
        assert per_position_budget_seconds(
            **{**DEFAULTS, 'fallback_strike_attempts': 0}) == 415

    def test_the_components(self):
        """btc = 3 x 88 + 2 x 2 = 268; stc = 76 + 71 + 2 x 76 = 299."""
        no_stc_rungs = per_position_budget_seconds(
            **{**DEFAULTS, 'fallback_strike_attempts': 0})
        assert no_stc_rungs == 268 + 76 + 71
        per_fallback = (per_position_budget_seconds(**DEFAULTS)
                        - no_stc_rungs) // 2
        assert per_fallback == 76


class TestTheConstants:
    def test_the_modeled_worst_cases(self):
        assert roll_budget.ORDER_READ_WORST_SECONDS == 9.0     # 3 x 2.0 + 1 + 2
        assert roll_budget.ORDER_ACTION_WORST_SECONDS == 2.0   # no @api_retry
        assert roll_budget.PRICING_READ_TIMEOUT_SECONDS == 3.0
        assert roll_budget.DIAG_READ_TIMEOUT_SECONDS == 2.0
        assert roll_budget.CANCEL_SETTLE_TIMEOUT_SECONDS == 15
        assert roll_budget.CANCEL_SETTLE_MIN_READS == 2
        assert roll_budget.POLL_INTERVAL_SECONDS == 5

    def test_the_cycle_constants(self):
        assert roll_budget.CYCLE_BUDGET_SECONDS == 1500
        assert roll_budget.PREAMBLE_ALLOWANCE_SECONDS == 60
        assert roll_budget.MAX_PER_POSITION_BUDGET_SECONDS == 1440

    @pytest.mark.parametrize("escalations, rungs", [(0, 2), (1, 3), (3, 5)])
    def test_the_primary_ladder_shape(self, escalations, rungs):
        assert roll_budget.PRIMARY_LADDER_RUNGS(escalations) == rungs

    def test_two_worst_case_positions_fit_a_cycle_and_a_third_does_not(self):
        budget = per_position_budget_seconds(**DEFAULTS)
        assert 2 * budget <= roll_budget.CYCLE_BUDGET_SECONDS
        assert 3 * budget > roll_budget.CYCLE_BUDGET_SECONDS

    def test_the_seam_slack_is_240_seconds(self):
        budget = per_position_budget_seconds(**DEFAULTS)
        latest_start = roll_budget.CYCLE_BUDGET_SECONDS - budget
        assert latest_start == 933
        assert 1800 - (latest_start + budget
                       + roll_budget.PREAMBLE_ALLOWANCE_SECONDS) == 240


class TestMonotonicity:
    """Lengthening any input never shortens the budget — the property the
    cross-key bound in ``Config`` relies on."""

    @pytest.mark.parametrize("key, values", [
        ('btc_fill_timeout_seconds', (15, 60, 120, 300, 600)),
        ('btc_reprice_attempts', (0, 1, 2)),
        ('stc_rung_timeout_seconds', (5, 30, 120, 600)),
        ('stc_escalation_rungs', (0, 1, 2, 3)),
        ('fallback_strike_attempts', (0, 1, 2, 5))])
    def test_each_key(self, key, values):
        budgets = [per_position_budget_seconds(**{**DEFAULTS, key: v})
                   for v in values]
        assert budgets == sorted(budgets), (key, budgets)


class TestFromConfigAndTypes:
    class _Cfg:
        rolling_btc_fill_timeout_seconds = 120
        rolling_btc_reprice_attempts = 2
        rolling_stc_rung_timeout_seconds = 30
        rolling_stc_escalation_rungs = 0
        rolling_fallback_strike_attempts = 2

    def test_from_config_reads_the_five_keys(self):
        assert roll_budget.from_config(self._Cfg()) == 567
        assert roll_budget.BUDGET_KEYS == (
            'btc_fill_timeout_seconds', 'btc_reprice_attempts',
            'stc_rung_timeout_seconds', 'stc_escalation_rungs',
            'fallback_strike_attempts')

    @pytest.mark.parametrize("bad", [True, 1.0, "30", None])
    def test_a_non_int_is_a_loud_type_error(self, bad):
        with pytest.raises(TypeError, match="stc_rung_timeout_seconds"):
            per_position_budget_seconds(
                **{**DEFAULTS, 'stc_rung_timeout_seconds': bad})

    def test_a_mock_config_fails_loudly_not_plausibly(self):
        from unittest.mock import MagicMock, Mock
        for config in (Mock(), MagicMock()):
            with pytest.raises(TypeError):
                roll_budget.from_config(config)


def test_the_module_is_a_leaf():
    """stdlib only: ``Config`` imports it, and ``wheel_engine`` imports
    ``Config`` — any ``src`` import here re-creates the cycle it exists to
    avoid."""
    tree = ast.parse((REPO / "src/strategy/roll_budget.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and not (node.module or '').startswith('src'), \
                ast.dump(node)
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith('src'), alias.name
