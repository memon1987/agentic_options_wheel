"""Scenario config overrides, and the allowlist that bounds them (FC-060 D2/D3).

A scenario is a deep copy of the base ``Config`` with a handful of dotted keys
written into ``_config``. That is the same seam every diagnostic harness in this
repo already uses (``tools/diagnostics/fc034_premium_floor_study.py`` sets
``config._config["strategy"]["min_put_premium"]``) — ``Config``'s accessors are
read-only properties, so there is no other injection point.

**Why an allowlist, not a free-for-all.** FC-060's own framing calls a sweep a
"multiple-comparisons machine", and the cheapest way to produce a confidently
wrong answer here is not a bad statistic — it is an override that *silently does
not do what it says*:

* ``strategy.put_target_dte`` changes what the chain must CONTAIN. Every cached
  file was written at ``universe_dte = 8``; a scenario asking for more misses the
  cache on every single day and re-fetches a symbol-year from the vendor, and one
  asking for less is served a chain that still contains the longer-dated
  contracts. Either way the replay is not the one that was requested.
* ``strategy.call_target_dte`` above 7 does not widen the chain at all — the
  contracts simply are not in the file, so the scenario reads as "no call ever
  qualified" rather than "this reach was tested".
* ``risk.profit_taking.*`` and the stop-loss knobs are read only by ``/monitor``
  (``PutSeller.should_close_put_early`` / ``CallSeller.should_close_call_early``),
  and the replay's day loop does not run the monitor. A sweep over them would
  return ten identical rows and read as "profit-taking does not matter".
* ``strategy.{put,call}_limit_spread_fraction`` are selection-only and still
  dead: the replay RECORDS a limit price and fills at the broker's
  ``mid - haircut x half-spread`` anyway. Vary ``Scenario(fill_haircut=...)``.
* ``universe.min_open_interest`` reads a field the engine hardcodes to ``0``, so
  any floor rejects every call — a deterministic wipe-out dressed as a finding.
* ``rolling.fallback_strike_attempts`` governs rungs the replay never reaches:
  the adapter fills rung 1 unconditionally, so rung >= 3 came up 0 times over an
  instrumented 37 rolls x 7 arms. Live in production, inert here.

All four were on the plan's allowlist and were moved here by measurement, not by
reading: **selection-only is necessary but not sufficient**. A key also has to be
one the replay HONOURS, and the cheapest way to find out is to run the arm and
see whether any row differs from base. Every key that remains has been checked
that way.

Each of those gets a rejection message that names the actual reason, because a
generic "not allowed" invites the reader to assume the sweep is being
conservative when in fact the answer would have been fiction.

**Env-shadowed keys fail too.** ``earnings.enabled`` and ``rolling.enabled`` are
overridden at runtime by ``EARNINGS_ENABLED`` / ``ROLLER_ENABLED`` when those are
set (FC-013 DD-7, FC-078 DD-7). With one exported, a scenario that flips the yaml
key changes nothing and the sweep reports two arms as tied. That is the same
class of defect as the DTE keys, so it is refused the same way.

The fill haircut is deliberately NOT a config key: it lives on the module
(``evaluate.DEFAULT_FILL_HAIRCUT``) and ``config_hash`` hashes the module value,
so two scenarios differing only in haircut would share a hash. It is a
``Scenario`` field instead.
"""

from __future__ import annotations

import copy
import os
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    # STDLIB-ONLY AT RUNTIME, DELIBERATELY (FC-060 Layer 3, D7). This module is
    # copied verbatim into the dashboard image so the API refuses exactly what
    # the Job refuses, with the same reason strings — and the dashboard has
    # neither PyYAML, nor structlog's config stack, nor a settings file. A
    # runtime `from ...utils.config import Config` would drag all three in and,
    # worse, would make the copy un-importable outside its package. `Config` is
    # used here only as an annotation, and `from __future__ import annotations`
    # already makes annotations strings, so nothing is lost.
    from ...utils.config import Config


class OverrideError(ValueError):
    """A scenario override that is unknown, disallowed, or silently ineffective."""


# --------------------------------------------------------------------------- #
# The allowlist: selection-only keys — things that change WHICH contract the
# strategy picks out of a chain it already has, never which chain it gets.
# --------------------------------------------------------------------------- #
ALLOWED_OVERRIDES: Dict[str, str] = {
    # Contract selection
    "strategy.put_delta_range": "put delta band [lo, hi]",
    "strategy.call_delta_range": "call delta band [lo, hi]",
    "strategy.min_put_premium": "put premium floor, $/share",
    "strategy.min_call_premium": "call premium floor, $/share",
    # Allowed only DOWNWARD — see ``validate_override_key``. Shortening the call
    # leg's reach narrows a chain that is already wide enough; lengthening it
    # widens nothing, because the contracts are not in the cached file.
    "strategy.call_target_dte": "call DTE target (may only be LOWERED)",
    # Stage-1 universe screening
    "strategy.min_stock_price": "stage-1 price floor",
    "strategy.max_stock_price": "stage-1 price ceiling",
    "strategy.min_avg_volume": "stage-1 average-volume floor",
    # Sizing. DOWNWARD ONLY until FC-079: the put sizer hardcodes one contract,
    # so raising the cap cannot buy a second one and the arm comes back as base.
    # Lowering it does bind (it starves a position out entirely), which is why
    # the key is allowed at all rather than refused outright.
    "risk.max_position_size": (
        "per-position fraction of portfolio value — DOWNWARD ONLY until FC-079 "
        "(the sizer never sizes above 1 contract, so raising it is inert)"
    ),
    # Inventory / operator opt-outs. NOTE the section: these live under
    # `universe:`, not `strategy:` — the plan's D3 text says `strategy.*` and is
    # wrong about the path. `universe:` is absent from config/settings.yaml
    # entirely (only the covered-call profile declares it), which is exactly why
    # the allowlist and not `_config`'s current shape decides what is legal: the
    # accessors all have defaults, so an absent section is a default, not a typo.
    "universe.excluded_symbols": "symbols the call leg must never write",
    # Read by `_check_call_criteria_detailed` off MODELLED bid/ask, which
    # measures ~2.46x wider than the real book (FC-051). An arm here is
    # interpretable but only against that bias, which the report footer states.
    # Kept rather than refused because the datum is a documented model with a
    # measured error, not an absent field — unlike min_open_interest below.
    "universe.max_spread_pct": "inventory validator: max bid/ask as fraction of mid",
    # FC-013 earnings gate
    "earnings.enabled": "earnings gate on/off",
    "earnings.blackout_days": "put-leg symbol-level blackout window, days",
    # The roller (FC-078). Read by the replay's daily run_rolling_cycle.
    "rolling.enabled": "daily credit-only roller on/off",
    "rolling.itm_trigger_ratio": "stock/strike ratio that makes a call roll-eligible",
    "rolling.max_extension_days": "replacement expiry <= old expiry + N",
    "rolling.max_replacement_delta": "upper delta rail on a replacement call",
    "rolling.min_net_credit_per_contract": "minimum net credit, $/contract",
    "rolling.imminence_extrinsic_threshold": "extrinsic $/share below which pricing goes mid-based",
}

# --------------------------------------------------------------------------- #
# Explicit rejections. Exact keys first, then prefixes.
# --------------------------------------------------------------------------- #
_CHAIN_REACH = (
    "cached chains store universe_dte=8; a scenario needs a re-materialisation "
    "with a wider reach, which Layer 2 does not support. A shorter reach is no "
    "safer: the file still contains the longer-dated contracts, so the replay "
    "would see a chain it did not ask for."
)

_CALL_DTE_TOO_LONG = (
    "raising strategy.call_target_dte does NOT widen the chain — the chains for "
    "this window were built to reach {reach} DTE, so 9-15 DTE calls are simply "
    "not in the cached files. The scenario would read as 'no call ever "
    "qualified' rather than as a test of that reach. Lowering it is fine. "
    + _CHAIN_REACH
)

REJECTED_OVERRIDES: Dict[str, str] = {
    "strategy.put_target_dte": _CHAIN_REACH,
    # --- Three the plan's D3 allowlisted and the build measured to be dead. --
    # D3's classification was "selection-only" — correct, none of them
    # invalidates a chain — but selection-only is necessary, not sufficient: the
    # replay also has to HONOUR the key. These three do not survive that second
    # test, and each was caught by running it, not by reading it.
    "strategy.put_limit_spread_fraction": (
        "the replay does not honour limit prices. "
        "`BacktestAlpacaClient.place_option_order` records `limit_price` and "
        "fills at the broker's `mid - fill_haircut x half-spread` instead — at "
        "one decision point per day there is no intraday path along which to "
        "decide whether a limit would have been touched. Measured: a "
        "`put_limit_spread_fraction: 0.0` arm returned results byte-identical to "
        "base on all six effective-universe symbols over a year. Vary the FILL "
        "ASSUMPTION instead, with `Scenario(fill_haircut=...)`."
    ),
    "strategy.call_limit_spread_fraction": (
        "same as put_limit_spread_fraction: the replay records the limit price "
        "and fills at `mid - fill_haircut x half-spread` regardless "
        "(`BacktestAlpacaClient.place_option_order`). Use "
        "`Scenario(fill_haircut=...)` to vary the fill."
    ),
    "universe.min_open_interest": (
        "the engine has no open-interest data: "
        "`BacktestAlpacaClient.get_options_chain` hardcodes `open_interest: 0` "
        "(the vendor's historical chains carry no OI). Any floor >= 1 therefore "
        "rejects EVERY call, deterministically, and the arm reads as 'this "
        "threshold kills the call leg' when the truth is 'the engine cannot see "
        "the number'. `universe.max_spread_pct` is allowed because its input is "
        "a documented model with a measured error, not an absent field."
    ),
    "rolling.fallback_strike_attempts": (
        "unreachable in a replay. The knob decides how many FURTHER strikes the "
        "roller tries after the first two rungs, and rung 1 always fills here: "
        "`BacktestAlpacaClient.place_option_order` fills immediately at the "
        "broker's haircut price rather than resting a limit that can go unfilled. "
        "Instrumented over 37 rolls x 7 arms, rung >= 3 was reached 0 times, so "
        "every arm returns the base row. It is live in production, where a real "
        "limit can miss; it is inert here."
    ),
    "strategy.opportunity_max_age_minutes": (
        "the replay hands opportunities from scan to execute in memory; there is "
        "no GCS blob and no age to test (see simulator.py's module docstring)."
    ),
    "strategy_id": (
        "the strategy profile is chosen by --config, not by an override. A "
        "sweep that switched profiles mid-run would compare two engines."
    ),
    "bigquery_dataset": (
        "a sweep never writes to BigQuery (FC-060 Layer 3 owns persistence), so "
        "this key changes nothing here."
    ),
    "stocks.symbols": (
        "the universe is run SCOPE, not a scenario: pass --symbols. A candidate "
        "symbol is a cold materialisation, which belongs to Layer 3's onboarding "
        "story."
    ),
}

REJECTED_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("risk.profit_taking.", (
        "profit-taking targets are read only by /monitor "
        "(PutSeller.should_close_put_early / CallSeller.should_close_call_early), "
        "and the replay's day loop does not run the monitor. Every arm would "
        "return an identical row, which reads as 'this knob does not matter'."
    )),
    ("alpaca.", (
        "credentials and endpoints are not strategy parameters, and a sweep must "
        "not be able to repoint the account interlock."
    )),
)

# Stop-loss knobs — same reason as profit-taking, but they are not under one
# prefix, so they are listed.
_STOP_LOSS_REASON = (
    "stop losses are evaluated on /monitor, which the replay's day loop does not "
    "run; both switches are off in production anyway (FC-010). An arm that "
    "toggled this would be indistinguishable from the base."
)
for _key in (
    "risk.use_put_stop_loss", "risk.use_call_stop_loss",
    "risk.put_stop_loss_percent", "risk.call_stop_loss_percent",
    "risk.stop_loss_multiplier",
):
    REJECTED_OVERRIDES[_key] = _STOP_LOSS_REASON
del _key

# Keys whose effective value is taken from the environment when that variable is
# set, so a yaml override is silently ineffective. FC-013 DD-7 / FC-078 DD-7.
ENV_SHADOWED: Dict[str, str] = {
    "earnings.enabled": "EARNINGS_ENABLED",
    "rolling.enabled": "ROLLER_ENABLED",
}


def _reject_reason(key: str) -> Optional[str]:
    if key in REJECTED_OVERRIDES:
        return REJECTED_OVERRIDES[key]
    for prefix, reason in REJECTED_PREFIXES:
        if key.startswith(prefix):
            return reason
    return None


# The DTE reach the chains are built to when nothing says otherwise.
# ``evaluate`` sets it from ``config.put_target_dte``, so a caller holding a
# Config should pass it rather than rely on this.
DEFAULT_CHAIN_REACH_DTE = 7


def validate_override_key(
    key: str, value: Any = None, *, chain_reach_dte: int = DEFAULT_CHAIN_REACH_DTE
) -> None:
    """Raise ``OverrideError`` unless ``key`` (at ``value``) is a legal override.

    Order matters: a key with a *specific* reason gets that reason, and only a
    key nobody has thought about falls through to the generic message. A reader
    who is told "put_target_dte is not in the allowlist" learns nothing; one who
    is told the cached chains store ``universe_dte=8`` knows what to do next.
    """
    reason = _reject_reason(key)
    if reason is not None:
        raise OverrideError(
            f"scenario override '{key}' is refused: {reason}"
        )
    if key not in ALLOWED_OVERRIDES:
        raise OverrideError(
            f"scenario override '{key}' is not a known selection-only key. "
            f"The allowlist is: {', '.join(sorted(ALLOWED_OVERRIDES))}. "
            "A key outside it either changes what the chain must contain (which "
            "needs a re-materialisation) or is not read by the replay at all — "
            "in both cases the scenario would not measure what it claims."
        )
    # The one value-dependent rule. ``call_target_dte`` is a legal override
    # DOWNWARD (it narrows a chain that already covers the reach) and an illegal
    # one upward (the contracts are not in the file, so the arm silently reads as
    # "nothing qualified"). D3 draws the line here rather than banning the key
    # outright, because shortening the call leg is a real question a sweep should
    # be able to ask.
    if key == "strategy.call_target_dte":
        try:
            requested = int(value)
        except (TypeError, ValueError):
            raise OverrideError(
                "scenario override 'strategy.call_target_dte' must be an "
                f"integer number of days (got {value!r})"
            )
        if requested > chain_reach_dte:
            raise OverrideError(
                f"scenario override 'strategy.call_target_dte' = {requested} is "
                "refused: " + _CALL_DTE_TOO_LONG.format(reach=chain_reach_dte)
            )
    env_var = ENV_SHADOWED.get(key)
    if env_var and os.environ.get(env_var) is not None:
        raise OverrideError(
            f"scenario override '{key}' would be silently ignored: "
            f"{env_var}={os.environ[env_var]!r} is set in the environment and "
            f"wins over the yaml key. Unset it before sweeping this key, or the "
            f"sweep will report two arms as tied when they were never different."
        )


def validate_overrides(
    overrides: Mapping[str, Any],
    *,
    chain_reach_dte: int = DEFAULT_CHAIN_REACH_DTE,
) -> None:
    """Validate every key in one scenario. Raises on the first offender."""
    for key, value in overrides.items():
        validate_override_key(key, value, chain_reach_dte=chain_reach_dte)


def apply_overrides(config: Config, overrides: Mapping[str, Any]) -> Config:
    """A DEEP COPY of ``config`` carrying ``overrides``.

    The base is never touched, and neither is any other scenario's config: each
    call copies first and writes into the copy. Intermediate sections are created
    when absent — ``universe:`` does not exist in ``config/settings.yaml``, and
    its accessors all carry defaults, so "absent" is a legitimate starting state
    rather than a typo. That is precisely why the allowlist, not the current
    shape of ``_config``, is what decides whether a key is real.

    Raises:
        OverrideError: for any key that is unknown, disallowed, or shadowed by an
            environment variable. Raised BEFORE anything is written, so a
            scenario cannot be half-applied.
    """
    validate_overrides(
        overrides,
        chain_reach_dte=int(
            getattr(config, "put_target_dte", DEFAULT_CHAIN_REACH_DTE)
        ),
    )
    scenario_config = copy.deepcopy(config)
    for key, value in overrides.items():
        section, _, leaf = key.rpartition(".")
        target = scenario_config._config
        if section:
            for part in section.split("."):
                nxt = target.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    target[part] = nxt
                target = nxt
        target[leaf] = copy.deepcopy(value)
    return scenario_config


def describe_allowlist() -> List[str]:
    """One line per allowed key, for the report and for ``--help``-style output."""
    return [f"{key} — {why}" for key, why in sorted(ALLOWED_OVERRIDES.items())]


def allowed_keys() -> Iterable[str]:
    return sorted(ALLOWED_OVERRIDES)
