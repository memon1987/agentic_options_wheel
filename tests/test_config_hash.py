"""``config_hash`` — FC-121 PR-2, T-2.

``src/backtesting/reporting/config_hash.py``. These four tests moved here
VERBATIM from the deleted screen test file (DD-6): the hash outlived the screen
and is stamped on every sweep row. Regression caught: the hash silently stops
covering a threshold or a scoring constant, so a threshold change would read as
a symbol change on every stored row.
"""

from __future__ import annotations

from src.backtesting.reporting.config_hash import config_hash


class TestProvenance:
    def test_config_hash_changes_when_a_threshold_changes(self):
        """A verdict is uninterpretable without the config that produced it."""
        class _Cfg:
            put_target_dte = 7
            put_delta_range = [0.10, 0.20]
            min_put_premium = 0.50

        a = _Cfg()
        h1 = config_hash(a)
        a.min_put_premium = 0.25
        h2 = config_hash(a)
        assert h1 != h2
        assert len(h1) == 16

    def test_config_hash_is_stable_for_identical_config(self):
        class _Cfg:
            put_target_dte = 7
            min_put_premium = 0.50

        assert config_hash(_Cfg()) == config_hash(_Cfg())


class TestConfigHashCoversScoring:
    """A threshold change must not be indistinguishable from a symbol change."""

    def test_hash_moves_when_a_scoring_constant_moves(self, monkeypatch):
        from src.utils.config import Config
        from src.backtesting.metrics import fitness as fit

        cfg = Config()
        before = config_hash(cfg)
        monkeypatch.setattr(fit, "RISK_FREE_RATE", 0.045)
        after = config_hash(cfg)
        assert before != after, (
            "changing the risk-free floor flips verdicts but left the hash "
            "identical — a threshold change would read as a symbol change"
        )

    def test_hash_moves_when_the_fill_assumption_moves(self, monkeypatch):
        from src.utils.config import Config
        from src.backtesting import evaluate as ev

        cfg = Config()
        before = config_hash(cfg)
        monkeypatch.setattr(ev, "DEFAULT_FILL_HAIRCUT", 0.5)
        after = config_hash(cfg)
        assert before != after
