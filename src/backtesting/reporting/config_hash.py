"""``config_hash`` — the stable hash of the strategy parameters behind a result.

Stamped on every sweep: ``scenario_runs.config_hash`` per cell (the runner) and
``scenario_sweeps.engine_config_hash`` per run (the sweep CLI/Job and the sim
service). It is also what lines a sweep row up with a historical
``backtest_runs`` row, which carried the same hash.

Moved here VERBATIM by FC-121 PR-2 from the retired monthly screen's BigQuery
writer module, which was deleted with the screen: the function outlived the
writer, and a writer module that writes nothing is stale naming. Its output is
byte-identical to what that module produced, for every profile — a change here
is a hash discontinuity on both sweep tables, so treat it as one.
"""

from __future__ import annotations

import hashlib
import json


def config_hash(config) -> str:
    """Stable hash of the strategy parameters that shape a verdict.

    A verdict is only interpretable alongside the thresholds that produced it;
    without this, a re-run under a changed premium floor is indistinguishable
    from a genuine change in the symbol.
    """
    # HASH DISCONTINUITY (FC-069 S1, 2026-08-04): `gap_lookback_days`,
    # `max_gap_frequency` and `execution_gap_threshold` were dropped from this
    # list when item 5 deleted GapDetector and the `gap_risk_controls` knobs.
    # `config_hash` values therefore change for all `backtest_runs` rows written
    # at or after this boundary; rows either side are not hash-comparable even
    # when every surviving parameter is identical. This is the second
    # non-comparability marker on the table, beside FC-068's `engine_version`
    # boundary — one boundary, two markers.
    keys = [
        "put_target_dte", "call_target_dte", "put_delta_range", "call_delta_range",
        "min_put_premium", "min_call_premium", "max_position_size",
        "max_stock_price", "min_stock_price",
    ]
    # NOT `getattr(config, k, None)` (FC-096 Phase C, review round 1 B1).
    #
    # Four of these nine keys are PUT-side, and `Config`'s accessors for them are
    # properties that index `_config["strategy"]` directly — so a profile without
    # the key raises `KeyError` from INSIDE the property, and `getattr`'s default
    # only swallows `AttributeError`. `config/covered_call.yaml` declares
    # `call_target_dte` and no put keys at all, so the three-argument `getattr`
    # raised `KeyError: 'put_target_dte'` on EVERY covered-call sweep, at all
    # three call sites (`runner.run_sweep`, `main.run_sweep_cmd`,
    # `sim_service`) — before a single day was replayed.
    #
    # `runner.config_target_dte` already carried this lesson; this reader did
    # not. A profile that does not declare a knob hashes it as `None`, which is
    # the honest statement: the run had no such setting.
    payload = {}
    for key in keys:
        try:
            payload[key] = getattr(config, key)
        except Exception:  # noqa: BLE001 - an absent knob is not a hash failure
            payload[key] = None

    # The verdict is not computed from config alone: these live as module
    # constants and a default argument, and changing any of them flips symbols.
    # Omitting them made a threshold change byte-identical to a symbol change —
    # the exact confusion this hash exists to prevent.
    from ..evaluate import DEFAULT_FILL_HAIRCUT
    from ..metrics import fitness as _fit
    payload["_scoring"] = {
        "min_days_in_position": _fit.MIN_DAYS_IN_POSITION,
        "risk_free_rate": _fit.RISK_FREE_RATE,
        "max_drawdown_warn": _fit.MAX_DRAWDOWN_WARN,
        "fill_haircut": DEFAULT_FILL_HAIRCUT,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
