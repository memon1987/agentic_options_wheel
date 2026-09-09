"""Tests for configuration management."""

import pytest
import os
import tempfile
from unittest.mock import patch
import yaml

from src.utils.config import Config


class TestConfig:
    """Test configuration loading and management."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.test_config_data = {
            'strategy_id': 'wheel',  # FC-075: required top-level key
            'alpaca': {
                'paper_trading': True,
                'api_key_id': '${TEST_API_KEY}',
                'secret_key': '${TEST_SECRET_KEY}',
                'expected_account_number': 'PA_TEST_ACCT'  # FC-075 Seam 2 interlock
            },
            'strategy': {
                'put_target_dte': 7,
                'call_target_dte': 7,
                'put_delta_range': [0.10, 0.20],
                'call_delta_range': [0.10, 0.20],
                'min_put_premium': 0.50,
                'min_call_premium': 0.30,
                'min_stock_price': 20.0,
                'max_stock_price': 500.0,
                'min_avg_volume': 1000000
                # FC-069 S1 deleted max_positions_per_stock,
                # max_total_positions and max_exposure_per_ticker.
            },
            'risk': {
                'max_position_size': 0.10,
                'use_put_stop_loss': False,
                'use_call_stop_loss': True,
                'put_stop_loss_percent': 0.50,
                'call_stop_loss_percent': 0.50,
                'stop_loss_multiplier': 1.5,
                'profit_taking': {
                    'use_dynamic_profit_target': True,
                    'static_profit_target': 0.50,
                    'min_profit_target': 0.30,
                    'max_profit_target': 0.80,
                    'default_long_dte_target': 0.50,
                    'dte_bands': []
                }
                # FC-069 S1 deleted max_portfolio_allocation, min_cash_reserve,
                # profit_target_percent and the whole gap_risk_controls block.
            },
            'stocks': {
                'symbols': ['AAPL', 'MSFT', 'GOOGL']
            }
            # FC-069 S1 deleted the monitoring block and dropped it from
            # required_sections.
        }
    
    def test_config_loading(self):
        """Test basic configuration loading."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(self.test_config_data, f)
            config_path = f.name
        
        try:
            with patch.dict(os.environ, {'TEST_API_KEY': 'test_key', 'TEST_SECRET_KEY': 'test_secret'}):
                config = Config(config_path)
                
                assert config.paper_trading == True
                assert config.alpaca_api_key == 'test_key'
                assert config.alpaca_secret_key == 'test_secret'
                assert config.put_target_dte == 7
                assert config.call_target_dte == 7
                assert config.put_delta_range == [0.10, 0.20]
                assert config.call_delta_range == [0.10, 0.20]
                assert config.max_position_size == 0.10
                assert config.use_put_stop_loss == False
                assert config.use_call_stop_loss == True
                assert config.stop_loss_multiplier == 1.5
                assert config.stock_symbols == ['AAPL', 'MSFT', 'GOOGL']
        finally:
            os.unlink(config_path)
    
    def test_environment_variable_substitution(self):
        """Test environment variable substitution in config values."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(self.test_config_data, f)
            config_path = f.name
        
        try:
            with patch.dict(os.environ, {'TEST_API_KEY': 'my_api_key', 'TEST_SECRET_KEY': 'my_secret'}):
                config = Config(config_path)
                assert config.alpaca_api_key == 'my_api_key'
                assert config.alpaca_secret_key == 'my_secret'
        finally:
            os.unlink(config_path)
    
    def test_config_get_method(self):
        """Test the get method for accessing nested config values."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(self.test_config_data, f)
            config_path = f.name

        try:
            with patch.dict(os.environ, {'TEST_API_KEY': 'test_key', 'TEST_SECRET_KEY': 'test_secret'}):
                config = Config(config_path)

                assert config.get('alpaca.paper_trading') == True
                assert config.get('strategy.put_target_dte') == 7
                assert config.get('nonexistent.key', 'default') == 'default'
                assert config.get('strategy.nonexistent', 42) == 42
        finally:
            os.unlink(config_path)
    
    def test_missing_config_file(self):
        """Test handling of missing configuration file."""
        with pytest.raises(FileNotFoundError):
            Config('nonexistent_config.yaml')
    
    def test_invalid_yaml(self):
        """Test handling of invalid YAML content."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("invalid: yaml: content: [")
            config_path = f.name

        try:
            with pytest.raises(yaml.YAMLError):
                Config(config_path)
        finally:
            os.unlink(config_path)


# =========================================================================== #
# FC-078 — the rolling knob set (T-13)
#
# Plan: docs/plans/fc-078.md DD-6 / DD-7. Four knobs added, eight deleted, one
# env override added mirroring EARNINGS_ENABLED.
# =========================================================================== #

from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _settings(tmp_path, **rolling_overrides):
    with open(REPO / 'config' / 'settings.yaml') as fh:
        data = yaml.safe_load(fh)
    data.setdefault('rolling', {}).update(rolling_overrides)
    path = tmp_path / 'settings.yaml'
    path.write_text(yaml.safe_dump(data))
    return str(path)


class TestTheRollingKnobs:

    def test_the_shipped_config_parses_the_four_new_knobs(self):
        config = Config(str(REPO / 'config' / 'settings.yaml'))
        assert config.rolling_max_extension_days == 14
        assert config.rolling_max_replacement_delta == 0.60
        assert config.rolling_min_net_credit_per_contract == 0.00
        assert config.rolling_imminence_extrinsic_threshold == 0.20
        # Kept knobs.
        assert config.rolling_itm_trigger_ratio == 0.98
        assert config.rolling_btc_fill_timeout_seconds == 120
        assert config.rolling_fallback_strike_attempts == 2

    def test_the_deleted_properties_are_gone(self):
        """*Catches:* a half-deletion that leaves a knob readable but unread —
        the state the max-rolls gate lived in for a year."""
        config = Config(str(REPO / 'config' / 'settings.yaml'))
        for gone in ('rolling_trigger_time_et', 'rolling_max_current_dte',
                     'rolling_max_debit_pct_of_premium',
                     'rolling_max_debit_pct_of_notional',
                     'rolling_max_rolls_per_position',
                     'rolling_earnings_blackout_days',
                     'rolling_btc_limit_over_ask_pct',
                     'rolling_stc_limit_under_bid_pct'):
            assert not hasattr(config, gone), f"{gone} survived the deletion"

    def test_the_deleted_keys_are_gone_from_the_shipped_yaml(self):
        with open(REPO / 'config' / 'settings.yaml') as fh:
            rolling = yaml.safe_load(fh)['rolling']
        for gone in ('trigger_time_et', 'max_current_dte',
                     'max_debit_pct_of_premium', 'max_debit_pct_of_notional',
                     'max_rolls_per_position', 'earnings_blackout_days',
                     'btc_limit_over_ask_pct', 'stc_limit_under_bid_pct'):
            assert gone not in rolling, f"rolling.{gone} survived the deletion"

    @pytest.mark.parametrize("overrides,fragment", [
        ({'max_extension_days': 0}, 'max_extension_days'),
        ({'max_extension_days': 2.5}, 'max_extension_days'),
        ({'max_replacement_delta': 0}, 'max_replacement_delta'),
        ({'max_replacement_delta': 1.5}, 'max_replacement_delta'),
        ({'min_net_credit_per_contract': -1}, 'min_net_credit_per_contract'),
        ({'imminence_extrinsic_threshold': -0.01},
         'imminence_extrinsic_threshold'),
    ])
    def test_out_of_bounds_knobs_are_refused_at_load(self, tmp_path, overrides,
                                                     fragment):
        with pytest.raises(ValueError, match=fragment):
            Config(_settings(tmp_path, **overrides))

    def test_bounds_are_inclusive_where_they_should_be(self, tmp_path):
        config = Config(_settings(tmp_path, max_extension_days=1,
                                  max_replacement_delta=1.0,
                                  min_net_credit_per_contract=0,
                                  imminence_extrinsic_threshold=0))
        assert config.rolling_max_extension_days == 1
        assert config.rolling_max_replacement_delta == 1.0


class TestTheRollerEnvOverrides:
    """DD-7. The yaml value is baked into the image, so "turn the roller off"
    would otherwise mean commit -> Cloud Build -> deploy — the pipeline that
    once sat silently red for 11 days (FC-031). This roller places live two-leg
    orders; its stop lever must not need a build."""

    def test_roller_enabled_beats_the_baked_yaml_value(self, monkeypatch,
                                                       tmp_path):
        path = _settings(tmp_path)
        monkeypatch.delenv("ROLLER_ENABLED", raising=False)
        assert Config(path).rolling_enabled is True

        monkeypatch.setenv("ROLLER_ENABLED", "false")
        assert Config(path).rolling_enabled is False

        monkeypatch.setenv("ROLLER_ENABLED", "TRUE")
        assert Config(path).rolling_enabled is True

    def test_an_unparseable_value_falls_back_to_yaml(self, monkeypatch,
                                                     tmp_path):
        """A typo must neither disable the roller nor enable it — yaml wins."""
        path = _settings(tmp_path)
        monkeypatch.setenv("ROLLER_ENABLED", "maybe")
        assert Config(path).rolling_enabled is True

        path_off = _settings(tmp_path, enabled=False)
        assert Config(path_off).rolling_enabled is False

    def test_dry_run_is_env_only_and_defaults_off(self, monkeypatch, tmp_path):
        path = _settings(tmp_path)
        monkeypatch.delenv("ROLLER_DRY_RUN", raising=False)
        assert Config(path).roller_dry_run is False

        monkeypatch.setenv("ROLLER_DRY_RUN", "true")
        assert Config(path).roller_dry_run is True

    def test_an_unparseable_dry_run_is_treated_as_false(self, monkeypatch,
                                                        tmp_path):
        """Unlike ROLLER_ENABLED there is no yaml key to fall back to, so the
        fallback is the default: off. A typo here costs the operator a debug
        session, never an order."""
        path = _settings(tmp_path)
        monkeypatch.setenv("ROLLER_DRY_RUN", "sometimes")
        assert Config(path).roller_dry_run is False

# Sentinel for "delete this key", distinct from any legal yaml value.
_ABSENT = object()


def _settings_strategy(tmp_path, **strategy_overrides):
    """The shipped wheel config with `strategy` keys overridden or removed.

    A value of ``_ABSENT`` deletes the key, which is how the "profile omits the
    knob entirely" case is exercised against the real config rather than a
    hand-rolled stub.
    """
    with open(REPO / 'config' / 'settings.yaml') as fh:
        data = yaml.safe_load(fh)
    for key, value in strategy_overrides.items():
        if value is _ABSENT:
            data['strategy'].pop(key, None)
        else:
            data['strategy'][key] = value
    path = tmp_path / 'settings.yaml'
    path.write_text(yaml.safe_dump(data))
    return str(path)


class TestLimitSpreadFractionsFC072:
    """FC-072: one sell-to-open pricing knob per leg, same bounds.

    The bounds are the point. Below 0.0 means "price under mid" — deliberately
    marketable, which is what the pre-FC-072 call formula did in effect, and
    reversing that decision must be a code change with a plan behind it rather
    than a config flip. Above 0.5 is a limit outside the book: at f=0.5 the
    limit sits exactly on the ask, and beyond it the order cannot fill until
    the market moves to it.
    """

    KEYS = ('call_limit_spread_fraction', 'put_limit_spread_fraction')
    SHIPPED = {'call_limit_spread_fraction': 0.0,
               'put_limit_spread_fraction': 0.10}

    def test_both_shipped_profiles_carry_both_keys(self):
        """A knob added to one profile and forgotten on the other is this
        repo's documented failure mode (FC-069 S1 found six such keys)."""
        for profile in ('settings.yaml', 'covered_call.yaml'):
            with open(REPO / 'config' / profile) as fh:
                strategy = yaml.safe_load(fh)['strategy']
            for key in self.KEYS:
                assert key in strategy, f"{profile} is missing {key}"
                assert strategy[key] == self.SHIPPED[key], f"{profile}:{key}"

    def test_the_shipped_wheel_config_reads_back_both_knobs(self):
        config = Config(str(REPO / 'config' / 'settings.yaml'))
        assert config.call_limit_spread_fraction == 0.0
        assert config.put_limit_spread_fraction == 0.10

    @pytest.mark.parametrize("key,expected", [
        ('call_limit_spread_fraction', 0.0),
        ('put_limit_spread_fraction', 0.10),
    ])
    def test_each_default_applies_when_its_key_is_absent(self, tmp_path, key,
                                                         expected):
        """A profile that omits a knob prices at its documented default rather
        than failing to load."""
        config = Config(_settings_strategy(tmp_path, **{key: _ABSENT}))
        assert getattr(config, key) == expected

    @pytest.mark.parametrize("key", KEYS)
    @pytest.mark.parametrize("bad", [0.6, 1.0, -0.01, -0.10, 'mid', None, True])
    def test_out_of_bounds_values_are_refused_at_load(self, tmp_path, key, bad):
        """Refused at load, not clamped at use: a config that means something
        other than what it says must not start."""
        with pytest.raises(ValueError, match=key):
            Config(_settings_strategy(tmp_path, **{key: bad}))

    @pytest.mark.parametrize("key", KEYS)
    @pytest.mark.parametrize("ok", [0.0, 0.05, 0.10, 0.5])
    def test_the_inclusive_bounds_load(self, tmp_path, key, ok):
        config = Config(_settings_strategy(tmp_path, **{key: ok}))
        assert getattr(config, key) == ok

    def test_a_configured_value_is_read_back(self, tmp_path):
        config = Config(_settings_strategy(tmp_path,
                                           call_limit_spread_fraction=0.10,
                                           put_limit_spread_fraction=0.25))
        assert config.call_limit_spread_fraction == 0.10
        assert config.put_limit_spread_fraction == 0.25


class TestTheRollingBlockOnBothProfilesFC100:
    """T-1/T-2/T-5. FC-100 wires the roller for the covered-call profile.

    The block is deliberately NOT dict-equal to the wheel's: `itm_trigger_ratio`
    is 1.00 on CC and 0.98 on the wheel (operator decision D-A). So the census
    is per key, and the divergence set is asserted to be *exactly* that one key
    — a second silent divergence is the failure this class exists to catch, and
    a dict-equality assertion could not tell the two apart.
    """

    KEYS = ('enabled', 'itm_trigger_ratio', 'max_extension_days',
            'max_replacement_delta', 'min_net_credit_per_contract',
            'imminence_extrinsic_threshold', 'btc_fill_timeout_seconds',
            'fallback_strike_attempts')

    EXPECTED_WHEEL = {
        'enabled': True,
        'itm_trigger_ratio': 0.98,
        'max_extension_days': 14,
        'max_replacement_delta': 0.60,
        'min_net_credit_per_contract': 0.00,
        'imminence_extrinsic_threshold': 0.20,
        'btc_fill_timeout_seconds': 120,
        'fallback_strike_attempts': 2,
    }
    EXPECTED_CC = dict(EXPECTED_WHEEL, itm_trigger_ratio=1.00)

    @staticmethod
    def _rolling(profile):
        with open(REPO / 'config' / profile) as fh:
            return yaml.safe_load(fh)['rolling']

    @pytest.mark.parametrize("profile,expected", [
        ('settings.yaml', EXPECTED_WHEEL),
        ('covered_call.yaml', EXPECTED_CC),
    ])
    def test_each_profile_carries_every_key_at_its_expected_value(
            self, profile, expected):
        """*Catches:* a knob retuned on one profile and not the other (FC-069
        S1 found six such keys), a typo'd key that silently falls to a default,
        and the CC trigger drifting back to the wheel's 0.98."""
        rolling = self._rolling(profile)
        for key in self.KEYS:
            assert key in rolling, f"{profile} is missing rolling.{key}"
            assert rolling[key] == expected[key], f"{profile}:rolling.{key}"

    def test_exactly_one_key_differs_between_the_profiles(self):
        """The whole point of DD-1: seven keys mirror, one is a stated
        difference. A second divergence must be a test failure, not a diff
        nobody reads."""
        wheel = self._rolling('settings.yaml')
        cc = self._rolling('covered_call.yaml')
        differing = {k for k in self.KEYS if wheel[k] != cc[k]}
        assert differing == {'itm_trigger_ratio'}, differing
        assert wheel['itm_trigger_ratio'] == 0.98
        assert cc['itm_trigger_ratio'] == 1.00

    def test_the_cc_config_reads_back_every_accessor(self):
        """The yaml keys are only worth pinning if Config exposes them; a key
        the loader never reads is the FC-069 corpse shape."""
        config = Config(str(REPO / 'config' / 'covered_call.yaml'))
        assert config.rolling_enabled is True
        assert config.rolling_itm_trigger_ratio == 1.00
        assert config.rolling_max_extension_days == 14
        assert config.rolling_max_replacement_delta == 0.60
        assert config.rolling_min_net_credit_per_contract == 0.00
        assert config.rolling_imminence_extrinsic_threshold == 0.20
        assert config.rolling_btc_fill_timeout_seconds == 120
        assert config.rolling_fallback_strike_attempts == 2

    def test_the_wheel_profile_is_untouched_by_fc100(self):
        """T-5, the neutrality pin at the config layer. FC-100 is a CC-scope
        change; the wheel's roller must read back exactly what it read before,
        0.98 included."""
        config = Config(str(REPO / 'config' / 'settings.yaml'))
        assert config.rolling_enabled is True
        assert config.rolling_itm_trigger_ratio == 0.98
        assert config.rolling_max_extension_days == 14
        assert config.rolling_max_replacement_delta == 0.60
        assert config.rolling_min_net_credit_per_contract == 0.00
        assert config.rolling_imminence_extrinsic_threshold == 0.20
        assert config.rolling_btc_fill_timeout_seconds == 120
        assert config.rolling_fallback_strike_attempts == 2


def _profile_copy(tmp_path, profile, **rolling_overrides):
    """A shipped profile copied to tmp with `rolling` keys overridden.

    `_settings()` above is wheel-only (it always reads settings.yaml); the
    kill-switch contract has to be exercised against BOTH shipped profiles,
    so this takes the profile name.
    """
    with open(REPO / 'config' / profile) as fh:
        data = yaml.safe_load(fh)
    data.setdefault('rolling', {}).update(rolling_overrides)
    path = tmp_path / profile
    path.write_text(yaml.safe_dump(data))
    return str(path)


@pytest.mark.parametrize("profile", ['settings.yaml', 'covered_call.yaml'])
class TestTheRollerEnvLeversOnBothProfilesFC100:
    """T-2. The kill switch and the dry-run lever are per-process env reads, so
    nothing about them is profile-specific *in the code* — which is exactly why
    a profile can be wired for rolling and never checked. This roller places
    live two-leg orders on two accounts now; its stop lever is asserted on both.
    """

    def test_the_env_kill_switch_beats_the_baked_yaml_value(
            self, monkeypatch, tmp_path, profile):
        """*Catches:* a profile on which ROLLER_ENABLED=false does not stop the
        roller — the one lever that works without a build."""
        path = _profile_copy(tmp_path, profile)

        monkeypatch.delenv("ROLLER_ENABLED", raising=False)
        assert Config(path).rolling_enabled is True, f"{profile} ships disabled"

        monkeypatch.setenv("ROLLER_ENABLED", "false")
        assert Config(path).rolling_enabled is False, f"{profile} ignored the kill"

        monkeypatch.setenv("ROLLER_ENABLED", "TRUE")
        assert Config(path).rolling_enabled is True

    def test_the_durable_off_is_the_yaml_key(self, monkeypatch, tmp_path,
                                             profile):
        """The env override is wiped by the next deploy's --set-env-vars, so
        `enabled: false` in the yaml is the durable stop.

        Scope, stated precisely: this asserts `Config.rolling_enabled is
        False`, which is the value the cycle gate reads — `run_rolling_cycle`
        returns `{'skipped': 'rolling_disabled'}` on it before evaluating
        anything (`src/strategy/wheel_engine.py:722`). It does not itself drive
        the cycle; `tests/test_wheel_engine.py` owns that gate."""
        monkeypatch.delenv("ROLLER_ENABLED", raising=False)
        path_off = _profile_copy(tmp_path, profile, enabled=False)
        assert Config(path_off).rolling_enabled is False

    def test_dry_run_is_env_only_and_defaults_off_on_both(
            self, monkeypatch, tmp_path, profile):
        """No yaml key backs it — a profile must never ship in dry-run, and a
        typo must not silently disarm the roller."""
        path = _profile_copy(tmp_path, profile)

        monkeypatch.delenv("ROLLER_DRY_RUN", raising=False)
        assert Config(path).roller_dry_run is False

        monkeypatch.setenv("ROLLER_DRY_RUN", "true")
        assert Config(path).roller_dry_run is True

        monkeypatch.setenv("ROLLER_DRY_RUN", "sometimes")
        assert Config(path).roller_dry_run is False
