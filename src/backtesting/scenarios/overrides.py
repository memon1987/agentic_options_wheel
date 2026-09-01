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

**The two DTE targets graduated by measurement too, and the same way round
(FC-096 Phase A, PR-2).** ``strategy.put_target_dte`` used to be refused outright
and ``strategy.call_target_dte`` allowed downward only, both for one reason: the
stored chains reached 7-day DTE, so a longer arm was not narrowed data but ABSENT
data, and it read as "nothing qualified" rather than as a test of that reach. The
FC-096 backfill widened the lake to ``universe_dte = 22``, the placebo gate
confirmed the knob moves selection — DTE-14 restructured the cycles; see
``docs/investigations/fc-096-a-placebo-gate-2026-09-01.md`` — and both keys now
carry the same static rule: ``1 <= int <= MAX_SWEEPABLE_DTE``. Note what
did NOT change: the bound is what the DATA reaches, not what the strategy could
want, so it stays a constant here rather than a knob. A longer arm than the lake
covers is still the old defect wearing a new number.

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


# The longest DTE target the stored chain lake reaches (FC-096 D1a, Phase A).
# The weekly `data-backfill` Job writes every day at `universe_dte = 22`, i.e. a
# 21-day target plus the universe buffer, so 21 is the longest reach a replay can
# be served real contracts for. **This is a property of the DATA, not a policy
# about the strategy** — which is why it is a constant and not a knob: raising it
# without widening the lake first re-creates exactly the defect the DTE keys were
# refused for, an arm that reads "nothing qualified" because the contracts are
# not in the file.
#
# It lives HERE, in the stdlib-only module that is flat-copied into the dashboard
# image, and ``src/backtesting/data/backfill.py`` imports it — not the other way
# round. Backfill can import this file; this file cannot import backfill (that
# would drag pandas, structlog and the whole data layer into the dashboard). One
# number, three consumers (the value rules below, the Job, the dashboard's flat
# copy), no drift.
MAX_SWEEPABLE_DTE = 21


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
    # Both DTE targets, bounded by what the LAKE reaches — see
    # ``validate_override_key``. FC-096 Phase A widened the stored chains to
    # `universe_dte = 22`, so anything from 1 to MAX_SWEEPABLE_DTE is served real
    # contracts; beyond that the file has nothing and the arm would read as
    # "nothing qualified".
    "strategy.put_target_dte": (
        f"put DTE target, days (1-{MAX_SWEEPABLE_DTE} — bounded by the stored "
        f"chain lake's reach)"
    ),
    "strategy.call_target_dte": (
        f"call DTE target, days (1-{MAX_SWEEPABLE_DTE} — bounded by the stored "
        f"chain lake's reach)"
    ),
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
#: The two override keys that move a run's chain reach. PUBLIC because three
#: places need exactly this set and a second copy is a place for them to
#: disagree: the value rule below, ``runner.effective_max_dte`` (which decides
#: how far to materialise), and the dashboard's per-run footer derivation. This
#: module is the one all three can import — it is stdlib-only and flat-copied
#: into the dashboard image.
DTE_OVERRIDE_KEYS = ("strategy.put_target_dte", "strategy.call_target_dte")

# The two DTE targets share one value rule, so they share one refusal. It names
# the constant rather than a literal, because the number moves with the lake and
# a message quoting a stale reach is how an operator concludes the allowlist is
# broken rather than that their arm is out of range.

_DTE_OUT_OF_RANGE = (
    "the stored chain lake reaches MAX_SWEEPABLE_DTE = {cap} days "
    "(`universe_dte = 22`, written by the weekly data-backfill Job — FC-096 "
    "Phase A). A target beyond that is not a narrower view of data we hold, it "
    "is data we do not hold: the contracts are absent from the file, so the arm "
    "would read as 'nothing ever qualified' rather than as a test of that reach. "
    "A target below 1 is not a contract at all. Widen the lake before widening "
    "this bound."
)

REJECTED_OVERRIDES: Dict[str, str] = {
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


def validate_override_key(key: str, value: Any = None) -> None:
    """Raise ``OverrideError`` unless ``key`` (at ``value``) is a legal override.

    Order matters: a key with a *specific* reason gets that reason, and only a
    key nobody has thought about falls through to the generic message. A reader
    who is told "put_target_dte is out of range" learns nothing; one who is told
    the lake reaches 21 days knows what to do next.

    **No ``chain_reach_dte`` parameter, deliberately (FC-096 Phase A, PR-2).**
    The DTE rule used to be relative to the CALLER's own reach — the base
    config's ``put_target_dte`` — which made the same arm legal in one sweep and
    refused in another, and made the dashboard (which holds no Config) validate
    against a default it had no way to check. The bound is a property of the
    stored lake, so it is static: ``1 <= int <= MAX_SWEEPABLE_DTE``, everywhere,
    for both legs.
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
    # The one value-dependent rule, and it is the same rule on both legs: a DTE
    # target has to be a whole number of days the lake actually stores.
    if key in DTE_OVERRIDE_KEYS:
        # `bool` is an `int` subclass, and `put_target_dte: true` would otherwise
        # mean "1 day" — a legal-looking arm nobody asked for.
        if isinstance(value, bool) or not isinstance(value, int):
            raise OverrideError(
                f"scenario override '{key}' must be an integer number of days "
                f"(got {value!r})"
            )
        if not 1 <= value <= MAX_SWEEPABLE_DTE:
            raise OverrideError(
                f"scenario override '{key}' = {value} is refused: "
                + _DTE_OUT_OF_RANGE.format(cap=MAX_SWEEPABLE_DTE)
            )
    env_var = ENV_SHADOWED.get(key)
    if env_var and os.environ.get(env_var) is not None:
        raise OverrideError(
            f"scenario override '{key}' would be silently ignored: "
            f"{env_var}={os.environ[env_var]!r} is set in the environment and "
            f"wins over the yaml key. Unset it before sweeping this key, or the "
            f"sweep will report two arms as tied when they were never different."
        )


def validate_overrides(overrides: Mapping[str, Any]) -> None:
    """Validate every key in one scenario. Raises on the first offender."""
    for key, value in overrides.items():
        validate_override_key(key, value)


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
    validate_overrides(overrides)
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
