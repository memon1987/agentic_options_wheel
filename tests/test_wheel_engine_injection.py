"""WheelEngine dependency-injection seam (FC-032 Phase 3).

The backtest replays the *actual* live code rather than a reimplementation of
it. That only works if injecting one client redirects every component the
engine builds. If any of them quietly keeps a real AlpacaClient, a replay would
fire live API calls mid-simulation.

FC-068 shrank what the engine builds: the put seller, call seller and gap
detector existed only to feed the deleted orchestration path, and the simulator
now constructs its own scanner and sellers on the same injected client (see
``tests/test_backtest_simulator.py``). What the engine still hangs off the
client is its market-data manager and, per roll cycle, the CallRoller.

Production behavior is unchanged: omit the argument and the real client is built.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from src.strategy.wheel_engine import WheelEngine
from src.strategy.wheel_state_manager import WheelStateManager


def _cycle_config():
    """A Mock config that can reach ``run_rolling_cycle``. FC-120 PR-2: the
    cycle derives its per-position budget from the five ``rolling.*`` timing
    keys (``roll_budget.from_config``), and a Mock there is a TypeError."""
    config = Mock()
    config.rolling_enabled = True
    config.earnings_enabled = False
    config.rolling_btc_fill_timeout_seconds = 120
    config.rolling_btc_reprice_attempts = 2
    config.rolling_stc_rung_timeout_seconds = 30
    config.rolling_stc_escalation_rungs = 0
    config.rolling_fallback_strike_attempts = 2
    return config


class TestAlpacaClientInjection:
    def test_injected_client_reaches_every_component(self):
        sentinel = Mock(name="BacktestAlpacaClient")
        engine = WheelEngine(Mock(), alpaca_client=sentinel, wheel_state=WheelStateManager())

        assert engine.alpaca is sentinel
        # The whole surviving graph must hang off the injected client.
        assert engine.market_data.alpaca is sentinel

    def test_the_injected_client_reaches_the_roller_too(self):
        """The roll cycle builds its CallRoller per invocation, so the seam has
        to hold at call time rather than construction time — and it runs inside
        the replay (Fridays)."""
        sentinel = Mock(name="BacktestAlpacaClient")
        sentinel.get_positions.return_value = []
        config = _cycle_config()
        engine = WheelEngine(config, alpaca_client=sentinel,
                             wheel_state=WheelStateManager())

        with patch("src.strategy.wheel_engine.CallRoller") as roller:
            engine.run_rolling_cycle()

        assert roller.call_args.args[0] is sentinel
        assert roller.call_args.args[1] is engine.market_data

    def test_injection_constructs_no_real_client(self):
        """A replay must not build an AlpacaClient at all — it would read creds."""
        with patch("src.strategy.wheel_engine.AlpacaClient") as real_client:
            WheelEngine(Mock(), alpaca_client=Mock(), wheel_state=WheelStateManager())
        real_client.assert_not_called()

    def test_default_still_builds_the_real_client(self):
        """Production path is untouched."""
        with patch("src.strategy.wheel_engine.AlpacaClient") as real_client:
            config = Mock()
            engine = WheelEngine(config)
        real_client.assert_called_once_with(config)
        assert engine.alpaca is real_client.return_value


class TestWheelStateInjection:
    def test_the_state_is_deliberately_NOT_handed_to_the_roller(self):
        """FC-078 DD-6 — T-12's engine half.

        This assertion is inverted from what it was. Pre-FC-068 the shared
        consumer was the engine's CallSeller; FC-068 deleted that seller and
        left the roller as the component the state reached. FC-078 deleted the
        roller's dependency outright: the only consumer was the debit
        tolerance's ``original_premium``, credit-only has no debit to tolerate,
        and ``STATE_STORAGE_BUCKET`` has been unset since project start so the
        state was never persisted to read back in the first place.

        Half-maintaining the fiction is worse than none, so the engine must not
        pass it — and this test fails if anyone re-wires it.
        """
        state = WheelStateManager()
        alpaca = Mock()
        alpaca.get_positions.return_value = []
        alpaca.get_orders.return_value = []
        config = _cycle_config()
        engine = WheelEngine(config, alpaca_client=alpaca, wheel_state=state)
        # The engine still keeps it — reconcile_positions is a real consumer.
        assert engine.wheel_state is state

        with patch("src.strategy.wheel_engine.CallRoller") as roller:
            engine.run_rolling_cycle()

        assert state not in roller.call_args.args
        assert state not in roller.call_args.kwargs.values()
        # Positional contract: (alpaca, market_data, config, risk_manager,
        # earnings_calendar). The state used to sit at index 3.
        assert roller.call_args.args[3].__class__.__name__ == 'RiskManager'

    def test_the_state_manager_cannot_touch_cloud_storage_at_all(self):
        """FC-069 item 8 stage 2 deleted the persistence, not just disabled it.

        The old assertion was ``_storage_client is None`` — which passed for a
        year *while the layer was fictional*, because the bucket was never
        configured. A None-valued attribute is not evidence of absence, so the
        replacement checks the attributes and the module source instead: there
        is nothing left to configure.
        """
        state = WheelStateManager()
        for attr in ("_storage_client", "_bucket_name", "_save_state",
                     "_load_state"):
            assert not hasattr(state, attr), (
                f"WheelStateManager.{attr} is back; FC-039 closed on the "
                "premise that this layer has no persistence target")

        import inspect
        import src.strategy.wheel_state_manager as wsm_module
        # Docstrings deliberately still *narrate* the deleted persistence
        # (that history is the point of FC-039), so the ban is on the
        # mechanisms, which no prose has reason to name.
        source = inspect.getsource(wsm_module)
        for banned in ("google.cloud", "upload_from_string",
                       "download_as_string", "blob("):
            assert banned not in source, (
                f"{banned!r} reappeared in wheel_state_manager")
        # And the constructor is the actual contract: no arguments to give.
        params = list(inspect.signature(WheelStateManager).parameters)
        assert params == [], params

    def test_default_state_takes_no_storage_argument(self):
        """The engine builds a plain, empty bookkeeping instance.

        Pre-FC-069 this asserted the constructor was handed
        ``storage_bucket=<config value>``; the config key
        (``state_storage_bucket`` / ``STATE_STORAGE_BUCKET``) never existed in
        production, which is exactly what FC-039 found.
        """
        with patch("src.strategy.wheel_engine.AlpacaClient"), patch(
            "src.strategy.wheel_engine.WheelStateManager"
        ) as wsm:
            WheelEngine(Mock())
        wsm.assert_called_once_with()

    def test_the_engine_no_longer_reads_a_state_bucket_setting(self):
        """Mutation guard for the deletion above: a resurrected bucket lookup
        in ``wheel_engine`` would re-create the silent no-op FC-039 named."""
        import inspect
        import src.strategy.wheel_engine as engine_module
        source = inspect.getsource(engine_module)
        for banned in ("state_storage_bucket", "STATE_STORAGE_BUCKET",
                       "storage_bucket", "_save_state"):
            assert banned not in source, (
                f"{banned!r} reappeared in wheel_engine")
