"""Scenario sweeps over the backtest engine (FC-060 Layer 2).

Ask "what would this config change have done?" across the whole effective
universe in one pass, instead of one `--command backtest` per arm.

    python main.py --command sweep --scenarios scenarios.yaml \\
        --symbols AAPL,AMZN,GOOGL,IWM,NVDA,UNH \\
        --start 2025-08-01 --end 2026-07-31 --no-sensitivity \\
        --out sweep.md --json-out sweep.json

Three things bound what this can claim, and all three are enforced in code
rather than documented and hoped for:

* **Overrides are selection-only** (``overrides.py``). A key that would change
  what the chain must contain, or that the replay never reads, is refused with
  the specific reason — see that module's docstring.
* **Replays make zero provider calls** (``runner.py``). Asserted on a counter,
  because a regression here would still produce correct numbers.
* **Nothing is persisted.** ``backtest_runs`` stays the production screen's
  table; Layer 3 owns a store for sweeps.
"""

from .overrides import (
    ALLOWED_OVERRIDES,
    REJECTED_OVERRIDES,
    OverrideError,
    apply_overrides,
    describe_allowlist,
    validate_overrides,
)
from .report import (
    CROSS_SCENARIO_CAVEAT,
    HOLDOUT_SEMANTICS,
    IN_SAMPLE_BANNER,
    SWEEP_BIASES,
    TALLY_CAVEAT,
    common_delta,
    render_json,
    render_markdown,
    sign_agreement,
)
from .runner import (
    BASE_SCENARIO_NAME,
    Scenario,
    ScenarioResult,
    SweepResult,
    run_sweep,
)

__all__ = [
    "ALLOWED_OVERRIDES",
    "BASE_SCENARIO_NAME",
    "CROSS_SCENARIO_CAVEAT",
    "HOLDOUT_SEMANTICS",
    "IN_SAMPLE_BANNER",
    "SWEEP_BIASES",
    "OverrideError",
    "REJECTED_OVERRIDES",
    "Scenario",
    "ScenarioResult",
    "SweepResult",
    "TALLY_CAVEAT",
    "apply_overrides",
    "common_delta",
    "describe_allowlist",
    "render_json",
    "render_markdown",
    "run_sweep",
    "sign_agreement",
    "validate_overrides",
]
