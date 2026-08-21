"""Configuration management for Options Wheel strategy."""

import os
import yaml
from pathlib import Path
from dotenv import load_dotenv
from typing import Dict, Any, List
import structlog

logger = structlog.get_logger(__name__)


_TRUE_TOKENS = {"1", "true", "t", "yes", "y", "on"}
_FALSE_TOKENS = {"0", "false", "f", "no", "n", "off"}


def _parse_bool(raw: str):
    """Parse an env-var boolean, or None when the token is not a boolean."""
    token = str(raw).strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return None


def _load_secret(secret_id: str, fallback_env_var: str) -> str:
    """Load secret from env var, falling back to GCP Secret Manager.

    Checks the environment variable first. If not set (or empty),
    attempts to retrieve the secret from GCP Secret Manager using
    the project ID from GOOGLE_CLOUD_PROJECT or GCP_PROJECT_ID env vars.

    Args:
        secret_id: The secret name in GCP Secret Manager.
        fallback_env_var: The environment variable to check first.

    Returns:
        The secret value, or an empty string if unavailable.
    """
    value = os.getenv(fallback_env_var)
    if value:
        return value
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        project_id = os.getenv('GOOGLE_CLOUD_PROJECT', os.getenv('GCP_PROJECT_ID'))
        if project_id:
            name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
            response = client.access_secret_version(request={"name": name})
            return response.payload.data.decode("UTF-8")
    except Exception:
        pass
    return ""


class Config:
    """Configuration manager for the options wheel strategy."""
    
    def __init__(self, config_path: str = "config/settings.yaml"):
        """Initialize configuration.
        
        Args:
            config_path: Path to the YAML configuration file
        """
        # Load environment variables
        load_dotenv()
        
        # Load YAML configuration
        self.config_path = Path(config_path)
        self._config = self._load_config()
        
        # Substitute environment variables
        self._substitute_env_vars()

        # Validate configuration
        self._validate_config()

        logger.info("Configuration loaded", event_category="system", event_type="config_loaded", config_path=config_path)
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        try:
            with open(self.config_path, 'r') as file:
                return yaml.safe_load(file)
        except FileNotFoundError:
            logger.error("Configuration file not found", event_category="error", event_type="config_file_not_found", path=self.config_path)
            raise
        except yaml.YAMLError as e:
            logger.error("Error parsing YAML configuration", event_category="error", event_type="config_yaml_parse_error", error=str(e))
            raise
    
    # Mapping of env var names to their GCP Secret Manager secret IDs.
    # Used by _substitute_env_vars to fall back to Secret Manager when
    # an env var is not set locally.
    _SECRET_MANAGER_MAP = {
        "ALPACA_API_KEY": "alpaca-api-key",
        "ALPACA_SECRET_KEY": "alpaca-secret-key",
        "FINNHUB_API_KEY": "finnhub-api-key",
    }

    def _substitute_env_vars(self):
        """Substitute environment variables in configuration values.

        For variables listed in _SECRET_MANAGER_MAP, this will fall back
        to GCP Secret Manager when the env var is not set locally.
        """
        def substitute_recursive(obj):
            if isinstance(obj, dict):
                return {k: substitute_recursive(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [substitute_recursive(item) for item in obj]
            elif isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
                env_var = obj[2:-1]
                if env_var in self._SECRET_MANAGER_MAP:
                    value = _load_secret(self._SECRET_MANAGER_MAP[env_var], env_var)
                    return value if value else obj
                return os.getenv(env_var, obj)
            else:
                return obj

        self._config = substitute_recursive(self._config)

    # Strategy profiles this codebase knows how to run. Each Cloud Run service
    # selects one via the top-level ``strategy_id`` key (see STRATEGY_CONFIG in
    # deploy/cloud_run_server.py). ``wheel`` is the historical default.
    _KNOWN_STRATEGY_IDS = ("wheel", "covered_call")

    def _validate_config(self):
        """Validate configuration values for correctness and safety."""
        errors = []

        # strategy_id is REQUIRED, not defaulted. Defaulting to 'wheel' was a
        # fail-open isolation hole: a covered-call config that omitted the key
        # would validate as wheel, pass the account interlock (its own account
        # matches), fall back to the wheel's opportunity bucket, and place wheel
        # orders in the covered-call account. Every config must declare identity.
        if 'strategy_id' not in self._config:
            raise ValueError(
                "strategy_id is required (top-level key; "
                f"one of {list(self._KNOWN_STRATEGY_IDS)}). It selects the "
                "validation profile and the isolation boundary; it must never "
                "be inferred."
            )
        strategy_id = self._config['strategy_id']
        if strategy_id not in self._KNOWN_STRATEGY_IDS:
            raise ValueError(
                f"Unknown strategy_id '{strategy_id}' "
                f"(expected one of {list(self._KNOWN_STRATEGY_IDS)})"
            )

        # Required sections. ``stocks`` is a configured trading universe, which
        # only the wheel has — the covered-call universe is derived from account
        # holdings, so that profile has no ``stocks`` section.
        # ``monitoring`` was dropped from this list by FC-069 S1 (card 17):
        # the block it required had no consumer, so requiring it only forced
        # every config to carry a corpse.
        required_sections = ['alpaca', 'strategy', 'risk']
        if strategy_id == 'wheel':
            required_sections.append('stocks')
        for section in required_sections:
            if section not in self._config:
                errors.append(f"Missing required section: '{section}'")

        if errors:
            # Can't proceed with validation if sections missing
            raise ValueError(f"Configuration validation failed:\n" + "\n".join(errors))

        # Account-number interlock (Seam 2). Required for every profile so a
        # service can assert at startup that its credentials point at the
        # expected brokerage account. Placed in the alpaca block.
        if not self._config.get('alpaca', {}).get('expected_account_number'):
            errors.append(
                "alpaca.expected_account_number is required (pins the service to "
                "a specific brokerage account; see the account-number interlock)"
            )

        # Isolation resources must be declared explicitly for any non-wheel
        # profile — never allowed to fall back to the wheel's bucket/dataset,
        # which would silently co-mingle a second strategy's data with the
        # wheel's. The wheel keeps its historical defaults.
        if strategy_id != 'wheel':
            if not self._config.get('gcs', {}).get('opportunity_bucket'):
                errors.append(
                    f"gcs.opportunity_bucket is required for strategy_id="
                    f"'{strategy_id}' (must not default to the wheel's bucket)"
                )
            if not self._config.get('bigquery', {}).get('dataset'):
                errors.append(
                    f"bigquery.dataset is required for strategy_id='{strategy_id}' "
                    f"(must not default to the wheel's dataset)"
                )

        # Alpaca validation
        alpaca = self._config.get('alpaca', {})
        if not alpaca.get('api_key_id') or alpaca.get('api_key_id', '').startswith('${'):
            errors.append("Alpaca API key not configured (check ALPACA_API_KEY env var)")
        if not alpaca.get('secret_key') or alpaca.get('secret_key', '').startswith('${'):
            errors.append("Alpaca secret key not configured (check ALPACA_SECRET_KEY env var)")

        # Strategy validation
        strategy = self._config.get('strategy', {})

        # Call DTE + delta apply to every option-writing profile.
        call_dte = strategy.get('call_target_dte', 0)
        if call_dte <= 0:
            errors.append(f"call_target_dte must be positive (got {call_dte})")
        call_delta = strategy.get('call_delta_range', [])
        if len(call_delta) != 2 or not (0 <= call_delta[0] <= call_delta[1] <= 1):
            errors.append(f"call_delta_range must be [min, max] with 0 <= min <= max <= 1 (got {call_delta})")

        # FC-013 DD-3 lookahead invariant. The call leg's span test asks "does
        # this candidate's expiry fall on or after the next earnings date" — a
        # question the calendar can only answer for expiries inside its own
        # lookahead. With call DTE <= 45 the default 90-day lookahead covers it
        # structurally; this assertion exists so a future DTE extension cannot
        # silently outrun the calendar and turn the gate into a no-op for the
        # longest-dated (highest-exposure) candidates. Checked only when the
        # gate is on, so a disabled-earnings config is unaffected.
        earnings_cfg = self._config.get('earnings', {}) or {}
        if self.earnings_enabled and call_dte > 0:
            lookahead = earnings_cfg.get('lookahead_days', 90)
            if lookahead < call_dte + 7:
                errors.append(
                    f"earnings.lookahead_days ({lookahead}) must be at least "
                    f"call_target_dte + 7 ({call_dte + 7}) so the FC-013 span "
                    "test can see every candidate's expiry"
                )

        # Put parameters and universe price-screening are wheel-only. The
        # covered-call profile has no put phase and derives its universe from
        # holdings, so these are not required (and not present) for it. Gating
        # on strategy_id keeps the wheel's validation path byte-identical.
        if strategy_id == 'wheel':
            put_dte = strategy.get('put_target_dte', 0)
            if put_dte <= 0:
                errors.append(f"put_target_dte must be positive (got {put_dte})")

            put_delta = strategy.get('put_delta_range', [])
            if len(put_delta) != 2 or not (0 <= put_delta[0] <= put_delta[1] <= 1):
                errors.append(f"put_delta_range must be [min, max] with 0 <= min <= max <= 1 (got {put_delta})")

            min_price = strategy.get('min_stock_price', 0)
            max_price = strategy.get('max_stock_price', 0)
            if min_price < 0:
                errors.append(f"min_stock_price must be non-negative (got {min_price})")
            if max_price <= 0:
                errors.append(f"max_stock_price must be positive (got {max_price})")
            if min_price >= max_price:
                errors.append(f"min_stock_price ({min_price}) must be less than max_stock_price ({max_price})")

        # FC-069 S1 removed the validation of `max_total_positions`,
        # `max_positions_per_stock`, `max_exposure_per_ticker`,
        # `max_portfolio_allocation` and `min_cash_reserve`. Validating a knob
        # nothing enforces only guarantees the corpse stays well-formed.

        # Risk management validation
        risk = self._config.get('risk', {})

        max_pos_size = risk.get('max_position_size', 0)
        if not (0 < max_pos_size <= 1):
            errors.append(f"max_position_size must be between 0 and 1 (got {max_pos_size})")

        # Stop loss percentages
        if risk.get('use_put_stop_loss'):
            put_sl = risk.get('put_stop_loss_percent', 0)
            if not (0 < put_sl <= 10):  # Max 1000% loss seems reasonable
                errors.append(f"put_stop_loss_percent should be between 0 and 10 (got {put_sl})")

        if risk.get('use_call_stop_loss'):
            call_sl = risk.get('call_stop_loss_percent', 0)
            if not (0 < call_sl <= 10):
                errors.append(f"call_stop_loss_percent should be between 0 and 10 (got {call_sl})")

        # Profit taking validation
        profit_taking = risk.get('profit_taking', {})
        if profit_taking:
            min_target = profit_taking.get('min_profit_target', 0)
            max_target = profit_taking.get('max_profit_target', 1)
            if not (0 < min_target <= max_target <= 1):
                errors.append(f"profit_taking targets invalid: min={min_target}, max={max_target} (need 0 < min <= max <= 1)")

        # Stock universe validation — only the wheel configures a universe;
        # the covered-call profile derives it from account holdings.
        if strategy_id == 'wheel':
            stocks = self._config.get('stocks', {})
            symbols = stocks.get('symbols', [])
            if not symbols:
                errors.append("No stock symbols configured in stocks.symbols")

        # Rolling validation (FC-078 DD-6). Bounds on the four knobs the roller
        # prices and gates live orders with. Absent keys validate against their
        # property defaults, which are in range by construction — a config with
        # no rolling block is valid, a config with a nonsense one is not.
        rolling = self._config.get('rolling', {})

        extension_days = rolling.get('max_extension_days', 14)
        if not isinstance(extension_days, int) or isinstance(extension_days, bool) \
                or extension_days < 1:
            errors.append(
                f"rolling.max_extension_days must be an integer >= 1 "
                f"(got {extension_days!r})")

        max_delta = rolling.get('max_replacement_delta', 0.60)
        if not isinstance(max_delta, (int, float)) or isinstance(max_delta, bool) \
                or not (0 < max_delta <= 1):
            errors.append(
                f"rolling.max_replacement_delta must satisfy 0 < x <= 1 "
                f"(got {max_delta!r})")

        min_credit = rolling.get('min_net_credit_per_contract', 0.00)
        if not isinstance(min_credit, (int, float)) or isinstance(min_credit, bool) \
                or min_credit < 0:
            errors.append(
                f"rolling.min_net_credit_per_contract must be >= 0 "
                f"(got {min_credit!r}) — a negative minimum is a debit "
                f"allowance, which FC-078 makes structurally impossible")

        imminence = rolling.get('imminence_extrinsic_threshold', 0.20)
        if not isinstance(imminence, (int, float)) or isinstance(imminence, bool) \
                or imminence < 0:
            errors.append(
                f"rolling.imminence_extrinsic_threshold must be >= 0 "
                f"(got {imminence!r})")

        # FC-069 S1 (card 17) deleted the `monitoring:` block and its
        # `check_interval_minutes` validation — the real cadence is Cloud
        # Scheduler, and no code ever read the key.

        # FC-075 Phase 2 — covered-call profile tunables (presence-gated so the
        # wheel, which sets none of these, is unaffected).
        universe = self._config.get('universe', {})
        excluded = universe.get('excluded_symbols')
        if excluded is not None and not isinstance(excluded, list):
            errors.append(f"universe.excluded_symbols must be a list (got {type(excluded).__name__})")
        min_oi = universe.get('min_open_interest')
        if min_oi is not None and (isinstance(min_oi, bool) or not isinstance(min_oi, int) or min_oi < 0):
            errors.append(f"universe.min_open_interest must be a non-negative int (got {min_oi!r})")
        max_spread = universe.get('max_spread_pct')
        if max_spread is not None and not (isinstance(max_spread, (int, float)) and not isinstance(max_spread, bool) and 0 < max_spread <= 1):
            errors.append(f"universe.max_spread_pct must be in (0, 1] (got {max_spread!r})")
        sizing_basis = self._config.get('risk', {}).get('sizing_basis')
        if sizing_basis is not None and sizing_basis not in ('buying_power', 'equity'):
            errors.append(f"risk.sizing_basis must be 'buying_power' or 'equity' (got {sizing_basis!r})")

        # Report errors
        if errors:
            error_msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            logger.error("Configuration validation failed",
                        event_category="error",
                        event_type="config_validation_error",
                        errors=errors)
            raise ValueError(error_msg)

        logger.debug("Configuration validation passed",
                    event_category="system",
                    event_type="config_validated")

    # Alpaca API Settings
    @property
    def alpaca_api_key(self) -> str:
        """Get Alpaca API key."""
        return self._config["alpaca"]["api_key_id"]
    
    @property
    def alpaca_secret_key(self) -> str:
        """Get Alpaca secret key."""
        return self._config["alpaca"]["secret_key"]
    
    @property
    def paper_trading(self) -> bool:
        """Check if paper trading is enabled."""
        return self._config["alpaca"]["paper_trading"]

    # Strategy identity & isolation (FC-075 Phase 1)
    @property
    def strategy_id(self) -> str:
        """Which strategy profile this config drives ('wheel' | 'covered_call').

        Absent means the legacy wheel profile, so an unmodified settings.yaml
        keeps working unchanged.
        """
        return self._config.get("strategy_id", "wheel")

    @property
    def expected_account_number(self) -> str:
        """Alpaca account number this service is pinned to (Seam 2 interlock).

        The startup interlock refuses to trade if the live account number does
        not match this value — the single control that makes "right code, wrong
        credentials" impossible across separate-account strategies.
        """
        return self._config["alpaca"]["expected_account_number"]

    @property
    def opportunity_bucket(self) -> str:
        """GCS bucket for the scan→execute opportunity handoff (Seam 1).

        Strategy-scoped so two services built from the same image cannot read
        each other's scan output. Defaults to the wheel's historical bucket, so
        the wheel is untouched when the key is absent.
        """
        return self._config.get("gcs", {}).get(
            "opportunity_bucket", "options-wheel-opportunities"
        )

    @property
    def bigquery_dataset(self) -> str:
        """BigQuery dataset for this strategy's analytics/reads.

        Defaults to ``options_wheel`` so the wheel is unchanged when the key is
        absent. FC-075 Phase 2 consumes this on the READ side (CostBasisResolver
        + UncoveredDaysResolver, Design decision 4). The WRITE side (the five BQ
        writers + a strategy_id column) is Seam 4, still deferred — see
        ``writes_isolated`` for the interim interlock.
        """
        return self._config.get("bigquery", {}).get("dataset", "options_wheel")

    @property
    def writes_isolated(self) -> bool:
        """Whether this profile's BigQuery WRITES land in the right dataset (DD-7).

        Until Seam 4 threads ``bigquery_dataset`` through the writers, they all
        hardcode ``options_wheel``. A non-wheel profile therefore cannot write
        without contaminating the wheel's dataset, so its write-producing paths
        must fail closed. The wheel profile is always isolated (it owns that
        dataset). Seam 4 will replace this with a real writer-vs-config dataset
        check and drop the guard.
        """
        return self.strategy_id == "wheel"

    # --- Covered-call profile tunables (FC-075 Phase 2) ---
    @property
    def excluded_symbols(self) -> set:
        """Symbols the bot must never write calls against (operator opt-out).

        Normalized to upper/stripped so an operator writing ``aapl`` still
        excludes AAPL. Empty (no exclusions) for the wheel.
        """
        raw = self._config.get("universe", {}).get("excluded_symbols", []) or []
        return {str(s).strip().upper() for s in raw}

    @property
    def min_open_interest(self):
        """Minimum call open interest (inventory validator); None = check skipped."""
        return self._config.get("universe", {}).get("min_open_interest")

    @property
    def max_spread_pct(self):
        """Maximum call bid/ask spread as a fraction of mid; None = check skipped."""
        return self._config.get("universe", {}).get("max_spread_pct")

    @property
    def sizing_basis(self) -> str:
        """'buying_power' (wheel default) | 'equity' (covered-call 4x-margin discipline)."""
        return self._config.get("risk", {}).get("sizing_basis", "buying_power")

    @property
    def max_long_market_value_pct_of_equity(self) -> float:
        """Exposure-alert threshold: alert when long_market_value > equity * this."""
        return self._config.get("risk", {}).get("max_long_market_value_pct_of_equity", 1.0)

    # Strategy Parameters
    @property
    def opportunity_max_age_minutes(self) -> int:
        """Maximum age in minutes for opportunities to be considered valid."""
        return self._config["strategy"].get("opportunity_max_age_minutes", 30)

    @property
    def put_target_dte(self) -> int:
        """Target days to expiration for puts."""
        return self._config["strategy"]["put_target_dte"]
    
    @property
    def call_target_dte(self) -> int:
        """Target days to expiration for calls."""
        return self._config["strategy"]["call_target_dte"]
    
    @property
    def put_delta_range(self) -> List[float]:
        """Delta range for put options."""
        return self._config["strategy"]["put_delta_range"]
    
    @property
    def call_delta_range(self) -> List[float]:
        """Delta range for call options."""
        return self._config["strategy"]["call_delta_range"]

    # FC-069 S1 (item 9) deleted `call_drawdown_pause_threshold`. The pause is
    # dead by operator decision (FC-065 OQ-3), FC-068 deleted its last
    # consumer, and FC-065 P4 removed the dashboard read. There is no drawdown
    # pause in this system.

    @property
    def min_put_premium(self) -> float:
        """Minimum premium for put options."""
        return self._config["strategy"]["min_put_premium"]
    
    @property
    def min_call_premium(self) -> float:
        """Minimum premium for call options."""
        return self._config["strategy"]["min_call_premium"]
    
    @property
    def min_stock_price(self) -> float:
        """Minimum stock price for selection."""
        return self._config["strategy"]["min_stock_price"]
    
    @property
    def max_stock_price(self) -> float:
        """Maximum stock price for selection."""
        return self._config["strategy"]["max_stock_price"]
    
    @property
    def min_avg_volume(self) -> int:
        """Minimum average volume for stock selection."""
        return self._config["strategy"]["min_avg_volume"]
    
    # FC-069 S1 deleted `max_positions_per_stock` (item 3),
    # `max_total_positions` (item 1) and `max_exposure_per_ticker` (item 2).
    # None had a preventive consumer: their only reader was the never-called
    # `RiskManager.validate_new_position`, deleted by item 7. The breadth
    # invariant that actually holds — one option position per underlying —
    # is emergent from the scanner skip, `select_batch`'s duplicate_underlying
    # drop and the calls share ledger, and is blind to resting unfilled orders.
    # Per-ticker exposure is bounded by `max_position_size` x portfolio, which
    # floats with equity, not by an absolute dollar cap.

    # FC-068 deleted `max_stocks_evaluated_per_cycle` and
    # `max_new_positions_per_cycle`. Their only consumers were stage 3 and
    # stage 9 of `WheelEngine._find_new_opportunities`, which production
    # abandoned in 2025 and this FC removed. Both were `null` in
    # settings.yaml, so neither ever limited anything. Deleted here rather than
    # left for FC-069 because they are on no inventory line — leaving them
    # would mint exactly the unowned corpses this FC family exists to end.

    # Risk Management
    # FC-069 S1 (card 17 / item 7 rider) deleted `max_portfolio_allocation`
    # and `min_cash_reserve`. Neither had an enforcing consumer; cash
    # discipline on this system is buying-power-bounded batching in
    # `ExecutionEngine.select_batch`.

    @property
    def max_position_size(self) -> float:
        """Maximum position size as percentage of portfolio."""
        return self._config["risk"]["max_position_size"]

    @property
    def use_put_stop_loss(self) -> bool:
        """Whether to use stop loss for put positions."""
        return self._config["risk"]["use_put_stop_loss"]
    
    @property
    def use_call_stop_loss(self) -> bool:
        """Whether to use stop loss for call positions."""
        return self._config["risk"]["use_call_stop_loss"]
    
    @property
    def put_stop_loss_percent(self) -> float:
        """Stop loss percentage for put positions."""
        return self._config["risk"]["put_stop_loss_percent"]
    
    @property
    def call_stop_loss_percent(self) -> float:
        """Stop loss percentage for call positions."""
        return self._config["risk"]["call_stop_loss_percent"]
    
    @property
    def stop_loss_multiplier(self) -> float:
        """Stop loss multiplier for short-term options (accounts for time decay)."""
        return self._config["risk"]["stop_loss_multiplier"]
    
    # FC-069 S1 (card 17) deleted `profit_target_percent` — self-labeled legacy
    # in both the yaml and the property, superseded by the `profit_taking.*`
    # DTE bands, zero consumers.

    # Profit Taking Configuration (Dynamic DTE-based)
    @property
    def use_dynamic_profit_target(self) -> bool:
        """Whether to use dynamic DTE-based profit targets."""
        return self._config["risk"]["profit_taking"]["use_dynamic_profit_target"]

    @property
    def profit_taking_static_target(self) -> float:
        """Fallback static profit target when dynamic is disabled."""
        return self._config["risk"]["profit_taking"]["static_profit_target"]

    @property
    def profit_taking_dte_bands(self) -> List[Dict[str, Any]]:
        """DTE-based profit target bands."""
        return self._config["risk"]["profit_taking"]["dte_bands"]

    @property
    def profit_taking_min_target(self) -> float:
        """Minimum profit target (safety bound)."""
        return self._config["risk"]["profit_taking"]["min_profit_target"]

    @property
    def profit_taking_max_target(self) -> float:
        """Maximum profit target (safety bound)."""
        return self._config["risk"]["profit_taking"]["max_profit_target"]

    # FC-069 S1 (item 6, absorbing FC-015) deleted
    # `profit_taking_min_hold_hours`. The gate it fed read `_entry_times`, an
    # in-process dict on sellers that are constructed per request — so it has
    # been open since inception (4 of 35 sub-4h closes observed 2026-05-12).
    # The raised DTE bands above carry the hold discipline. If a hold gate is
    # ever wanted, derive entry time statelessly from Alpaca order history
    # (`filled_at`); do not resurrect process state.

    @property
    def profit_taking_default_long_dte(self) -> float:
        """Default profit target for DTE > 7."""
        return self._config["risk"]["profit_taking"]["default_long_dte_target"]

    # Stock Universe
    @property
    def stock_symbols(self) -> List[str]:
        """List of configured stock symbols to trade.

        FC-075 Phase 2: returns [] when there is no ``stocks`` section (the
        covered-call profile, whose universe is holdings-derived) rather than
        raising KeyError. The wheel is unchanged — it always has the section, and
        validation still requires it non-empty for the wheel profile. This makes
        the read-only callers (`/config`, `/backtest/screen`, `get_market_overview`)
        safe on a non-wheel profile.
        """
        return self._config.get("stocks", {}).get("symbols", [])
    
    # FC-069 S1 deleted the `monitoring:` accessor (card 17) and the whole
    # gap-risk accessor block (item 5) together with
    # `src/risk/gap_detector.py`, `risk.gap_risk_controls.*` and the dead
    # `earnings_avoidance_days` sibling (item 4). Gap risk is absent by
    # decision: FC-036's study rejected arming the execution gate and
    # FC-049 found the stage-2 filter would have refused 123 of 327 real
    # entries. The module and its knobs live at pre-S1 main SHA afb6698;
    # FC-049 owns any evidence-based revival.
    # NOTE: `earnings.blackout_days` is a different key and is LIVE via
    # FC-013 (`earnings_blackout_days` below) — it is not part of this.

    # Call Rolling (FC-006; knob set rewritten by FC-078 DD-6)
    @property
    def rolling_enabled(self) -> bool:
        """Whether the call rolling engine is enabled.

        **``ROLLER_ENABLED`` wins over the yaml key when it is set** (FC-078
        DD-7), mirroring ``EARNINGS_ENABLED`` exactly. The yaml value is baked
        into the image, so "turn the roller off" would otherwise mean commit ->
        Cloud Build -> deploy — and the roller places live two-leg orders, so
        the stop lever must not need a build:

            gcloud run services update options-wheel-strategy \\
                --update-env-vars ROLLER_ENABLED=false

        ``--update-env-vars``, never ``--set-env-vars`` — the latter wipes the
        whole env set.

        An unparseable value is ignored (yaml wins) rather than silently read as
        one or the other: a typo must neither disable the roller nor enable it.
        """
        override = os.getenv("ROLLER_ENABLED")
        if override is not None:
            parsed = _parse_bool(override)
            if parsed is not None:
                return parsed
            logger.warning(
                "ROLLER_ENABLED is set but unparseable; falling back to settings.yaml",
                event_category="system",
                event_type="config_env_override_ignored",
                variable="ROLLER_ENABLED",
                value=override,
            )
        return self._config.get("rolling", {}).get("enabled", False)

    @property
    def roller_dry_run(self) -> bool:
        """Whether the roller evaluates but places NEITHER leg (FC-078 DD-7).

        Env-only, default false. An operational debugging flag — deliberately
        not a yaml key and not a launch gate (operator decision, FC-078). An
        unparseable value falls back to false: dry-run is the *safe* posture, so
        a typo that lands here costs a cycle, never an order.
        """
        override = os.getenv("ROLLER_DRY_RUN")
        if override is None:
            return False
        parsed = _parse_bool(override)
        if parsed is not None:
            return parsed
        logger.warning(
            "ROLLER_DRY_RUN is set but unparseable; treating as false",
            event_category="system",
            event_type="config_env_override_ignored",
            variable="ROLLER_DRY_RUN",
            value=override,
        )
        return False

    @property
    def rolling_itm_trigger_ratio(self) -> float:
        """Stock price / strike ratio to trigger roll evaluation."""
        return self._config.get("rolling", {}).get("itm_trigger_ratio", 0.98)

    @property
    def rolling_max_extension_days(self) -> int:
        """Calendar days past the OLD expiry a replacement may expire (FC-078 DD-3).

        Expiry-relative, not evaluation-relative: roll economics extend the
        existing contract's tenor. An eval-relative frame would have excluded
        the flagship GOOGL C370 8/07 -> C375 8/21 roll, which is 17 DTE from the
        evaluation date but exactly old-expiry + 14.
        """
        return self._config.get("rolling", {}).get("max_extension_days", 14)

    @property
    def rolling_max_replacement_delta(self) -> float:
        """Upper delta rail on a replacement call (FC-078 DD-3). No lower bound."""
        return self._config.get("rolling", {}).get("max_replacement_delta", 0.60)

    @property
    def rolling_min_net_credit_per_contract(self) -> float:
        """Minimum net credit per contract, in dollars, on the placed limits.

        Default 0.00: any non-negative roll at conservative prices executes the
        day it appears. The knob exists so a churn guard can be added without a
        code change (FC-078 DD-1).
        """
        return self._config.get("rolling", {}).get("min_net_credit_per_contract", 0.00)

    @property
    def rolling_imminence_extrinsic_threshold(self) -> float:
        """Per-share extrinsic at or below which assignment is treated as
        imminent and the roller switches to mid-based pricing (FC-078 DD-2)."""
        return self._config.get("rolling", {}).get("imminence_extrinsic_threshold", 0.20)

    @property
    def rolling_btc_fill_timeout_seconds(self) -> int:
        """Seconds to wait for BTC/STC order fill."""
        return self._config.get("rolling", {}).get("btc_fill_timeout_seconds", 120)

    @property
    def rolling_fallback_strike_attempts(self) -> int:
        """Number of fallback strikes to try if first STO fails."""
        return self._config.get("rolling", {}).get("fallback_strike_attempts", 2)

    # Finnhub / Earnings Calendar
    @property
    def finnhub_api_key(self) -> str:
        """Get Finnhub API key for earnings calendar."""
        return os.getenv("FINNHUB_API_KEY", "")

    @property
    def earnings_enabled(self) -> bool:
        """Whether the earnings calendar service — and therefore the FC-013
        scanner gate — is on.

        **``EARNINGS_ENABLED`` wins over the yaml key when it is set** (FC-013
        DD-7). The yaml value is baked into the image, so "roll the config back"
        actually means commit -> Cloud Build -> deploy, behind the same pipeline
        that once sat silently red for 11 days (FC-031). The gate's persistent-
        failure mode (Finnhub key wiped or revoked) blocks *all* opens until
        someone intervenes, so the intervention must not need a build:

            gcloud run services update options-wheel-strategy \\
                --update-env-vars EARNINGS_ENABLED=false

        ``--update-env-vars``, never ``--set-env-vars`` — the latter wipes the
        whole env set, which is itself one of this gate's failure scenarios.

        The yaml key remains the default and the documented home; the env var is
        the emergency lever. An unparseable value is ignored (yaml wins) rather
        than silently read as false — a typo must not disable a risk control.
        """
        override = os.getenv("EARNINGS_ENABLED")
        if override is not None:
            parsed = _parse_bool(override)
            if parsed is not None:
                return parsed
            logger.warning(
                "EARNINGS_ENABLED is set but unparseable; falling back to settings.yaml",
                event_category="system",
                event_type="config_env_override_ignored",
                variable="EARNINGS_ENABLED",
                value=override,
            )
        return self._config.get("earnings", {}).get("enabled", False)

    @property
    def earnings_cache_ttl_hours(self) -> int:
        """Cache TTL in hours for earnings data."""
        return self._config.get("earnings", {}).get("cache_ttl_hours", 24)

    @property
    def earnings_blackout_days(self) -> int:
        """Days before earnings to block new positions/rolls."""
        return self._config.get("earnings", {}).get("blackout_days", 2)

    @property
    def earnings_lookahead_days(self) -> int:
        """Days to look ahead when querying earnings calendar."""
        return self._config.get("earnings", {}).get("lookahead_days", 90)

    def get(self, key: str, default=None):
        """Get configuration value by key path (e.g., 'alpaca.paper_trading')."""
        keys = key.split('.')
        value = self._config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value