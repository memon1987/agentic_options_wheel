"""Scenario-sweep API logic — validation, launch, dedup, shaping (FC-060 D5-D11).

Pure functions, no FastAPI and no cloud client, for the reason
``services/pause_alert.py`` gives: the bot's CI image has no FastAPI, so anything
that lives in a router is untested by the only suite that runs. Everything a
mistake here would cost — accepting an override the Job will refuse, launching a
second concurrent sweep, rendering an `insuf` cell as a return — is decided in
this file and pinned by
``tests/test_dashboard_sweeps.py``. The router is a thin caller.

**The allowlist is IMPORTED, not restated.** ``src/backtesting/scenarios/``'s
``overrides.py`` and ``identity.py`` are stdlib-only and are copied into the
dashboard image (``dashboard/Dockerfile``), so the API refuses exactly what the
Job refuses, with the same reason strings, and computes the same ``sweep_key``.
A second copy of those rules would drift, and its drift would be silent: the API
would accept an arm and the Job would die on it three minutes into a container
start, or the dedup would miss and quietly re-run an eight-minute sweep.

**What is copied, and why each copy is guarded.** Three things could not be
imported and are therefore duplicated with a byte-equality test in the root
suite (Cloud Build runs that suite as step 1, so drift fails the build):

* the report's operator-facing prose (``sweep_report_text.py``) — ``report.py``
  imports ``runner.py`` imports the simulator;
* ``ENGINE_VERSION`` — lives in ``screen.py``, which imports the engine;
* the ``scenario_sweeps`` row shape and the "latest status wins" ORDER BY —
  ``persist.py``'s package ``__init__`` imports the engine.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from services.sweep_report_text import (
    BASE_SCENARIO_NAME,
    CROSS_SCENARIO_CAVEAT,
    DTE_REACH_BIAS,
    DTE_REACH_BIAS_THRESHOLD,
    HOLDOUT_SEMANTICS,
    IN_SAMPLE_BANNER,
    MIN_DAYS_IN_POSITION,
    SWEEP_BIASES,
    TALLY_CAVEAT,
)

# The two stdlib-only engine modules. In the repo (and in the test suite) they
# are importable at their real package path; in the dashboard image
# `dashboard/Dockerfile` copies the SAME FILES flat, because the package's
# `__init__.py` imports the runner and would pull the whole engine into an image
# that has none of it. `tests/test_cloudbuild_contract.py` pins the Dockerfile's
# COPY destinations against the fallback names below, so a rename cannot leave
# this silently importing something else.
try:  # repo / test environment
    from src.backtesting.scenarios.identity import (
        DEFAULT_STARTING_CASH, MAX_SCENARIO_NAME_CHARS, SCENARIO_NAME_RE,
        canonical_spec, sweep_key, validate_scenario_name,
    )
    from src.backtesting.scenarios.overrides import (
        ALLOWED_OVERRIDES, DTE_OVERRIDE_KEYS, REJECTED_OVERRIDES, OverrideError,
        describe_allowlist, validate_override_key,
    )
except ImportError:  # dashboard image: the same files, copied flat
    from scenario_identity import (  # type: ignore
        DEFAULT_STARTING_CASH, MAX_SCENARIO_NAME_CHARS, SCENARIO_NAME_RE,
        canonical_spec, sweep_key, validate_scenario_name,
    )
    from scenario_overrides import (  # type: ignore
        ALLOWED_OVERRIDES, DTE_OVERRIDE_KEYS, REJECTED_OVERRIDES, OverrideError,
        describe_allowlist, validate_override_key,
    )

# ============================================================================ #
# Pinned copies — each has a byte-equality test against its original.
# ============================================================================ #

# `src.backtesting.screen.ENGINE_VERSION`. It is half of `sweep_key` (D4), so a
# dashboard that disagreed with the Job here would compute a key nothing ever
# matches and the dedup would never fire — costing a full replay every time,
# silently. Pinned by TestTheEngineVersionIsNotAFork.
ENGINE_VERSION = "fc-069-scanner-rewire"

logger = logging.getLogger(__name__)

# ============================================================================ #
# Engine identity (FC-096 Phase B) — read, never computed, on this side.
# ============================================================================ #

# `sweep_key`'s third component since FC-096 Phase B: the sha256 of `src/**`
# (`src/backtesting/scenarios/engine_identity.py`). It replaced `git_commit`,
# whose failure mode was over-invalidation — every merge to `main` threw away
# every stored sweep result, including the merges that cannot change a replay.
#
# **The dashboard image cannot compute it.** It ships two flat stdlib modules out
# of the engine package and no `src/` tree at all, so there is nothing to hash.
# `cloudbuild.yaml` runs the SHARED module against the same checkout that builds
# this image and bakes the answer in as this env var (Dockerfile ARG -> ENV).
# Parity is by shared-module invocation; a second implementation here is exactly
# the silent-drift class the whole file exists to avoid.
ENGINE_IDENTITY_ENV = "ENGINE_IDENTITY"

# One log line per process, not one per request: an image built without the
# build-arg would otherwise emit this on every submit for the life of the
# revision, and a log that repeats a thousand times a day is a log nobody reads.
_identity_absence_logged = False


def engine_identity_from_env() -> Optional[str]:
    """The baked-in engine identity, or ``None`` — loudly — when it is absent.

    **Absent means the dedup hint disables itself, never that it keys with a
    wrong value.** A key computed over an empty identity is a real, valid-looking
    16-hex string that collides across genuinely different engine builds, so a
    dashboard that fell back to `""` would eventually point an operator at
    another engine's numbers. Returning None costs a hint; guessing costs
    correctness.

    The Job is unaffected either way — it computes the identity from the tree it
    is actually running and does the dedup that decides anything.
    """
    global _identity_absence_logged
    value = (os.environ.get(ENGINE_IDENTITY_ENV) or "").strip()
    if value:
        return value
    if not _identity_absence_logged:
        _identity_absence_logged = True
        logger.error(
            "sweep_dedup_hint_disabled: %s is not set on this image, so the "
            "dashboard cannot compute sweep_key. Submissions still launch and "
            "the Job still deduplicates; only the `prior_done_run_id` hint and "
            "the submitted row's sweep_key are unavailable. Fix: the "
            "`compute-engine-identity` step in cloudbuild.yaml must run and its "
            "value must reach dashboard/Dockerfile's ENGINE_IDENTITY build-arg.",
            ENGINE_IDENTITY_ENV,
        )
    return None


# ---------------------------------------------------------------------------- #
# Surviving a dataset that has not been migrated yet.
# ---------------------------------------------------------------------------- #
#
# `engine_identity` is an ADDITIVE column, and the engine's writer reconcile
# adds it — but the DASHBOARD has no writer and does not own the schema
# (`insert_sweep_status` says so: one schema owner, and it is the side that
# knows every column). So there is a window, and there are environments, in
# which this image is new and the tables are old:
#
#   * between the dashboard deploy and the ALTER TABLE / first Job run;
#   * a fresh project, or any dataset restored from before the migration.
#
# In that state the hint query fails with `Unrecognized name: engine_identity`
# and the `submitted` insert fails with `no such field: engine_identity` — and
# BOTH are on the submit path, so every submission would 502/500. The dedup hint
# is a convenience; the SUBMISSION is not. Neither may take the endpoint down,
# and `force` (which skips the hint) must not die at the insert either.
#
# The two predicates below recognise that specific failure so the callers can
# degrade — hint-miss, and insert-without-the-column — instead of raising. They
# are deliberately NARROW: anything that does not name this column is a real
# error and still propagates.
_MISSING_COLUMN_MARKERS = (
    "unrecognized name",     # query: SELECT/WHERE on a column that is not there
    "no such field",         # streaming insert: unknown key in the row
    "unknown field",
    "invalid field name",
)

# **Every additive column this API stamps has the same window**, so the insert
# degrade is keyed off a CLOSED SET rather than off one column name. FC-096
# Phase B PR-b adds `artifacts_complete` on exactly the terms `engine_identity`
# arrived on in PR-a: the Job's writer owns the schema and its reconcile adds
# the column, but between a dashboard deploy and that reconcile the table does
# not have it, and `insert_rows_json` rejects the WHOLE request over one unknown
# key. Without this generalisation the second additive column would have
# reproduced PR-a's outage verbatim.
#
# A closed set, not "drop whatever the error names": a typo'd column
# (`sweep_ky`) must stay a loud failure rather than a silently narrowed row. The
# value is the column's BigQuery type, so the remediation the log prints is a
# runnable `ALTER TABLE`.
ADDITIVE_OPTIONAL_COLUMNS = (
    ("engine_identity", "STRING"),
    ("artifacts_complete", "BOOL"),
    # FC-096 Phase B PR-c. The sim service stamps a per-row liveness bound so a
    # killed instance releases the submit lock in ~25 min rather than 3 h 10 m.
    # This API never SETS it (it has run nothing), but `submitted_row` carries
    # the key as NULL — every status row of a run must have the same column set
    # — so an un-migrated table rejects the whole request over it exactly as it
    # would over the other two.
    ("liveness_seconds", "INT64"),
    # FC-096 Phase B B4. The pin a battery run re-measured. This API never SETS
    # it either (it runs nothing), but `submitted_row` carries the key as NULL
    # for the same one-column-set rule, so an un-migrated table rejects the
    # whole request over it exactly as it would over the other three.
    ("pin_id", "STRING"),
)

_identity_query_degraded_logged = False
_insert_degraded_logged: set = set()


def missing_optional_column(error: Any) -> Optional[str]:
    """Which additive column ``error`` says the table lacks, or ``None``.

    ``None`` for every other failure, including a missing column that is not one
    of ours — a 403, a 404 and a typo'd name all still raise, which is the same
    narrowness ``mentions_missing_identity_column`` was written with.
    """
    text = str(error).lower()
    if not any(marker in text for marker in _MISSING_COLUMN_MARKERS):
        return None
    for column, _type in ADDITIVE_OPTIONAL_COLUMNS:
        # A WORD-BOUNDED match, not a substring one. `engine_identity` is a
        # prefix of any future `engine_identity_hash`, and a message naming the
        # longer column would silently make us drop the shorter — writing a row
        # that is missing a field the table actually has, and leaving the real
        # unknown key in place so the retry fails anyway. `\b` does not help
        # here (`_` is a word character to `re`), so the boundary is spelled
        # explicitly as "not preceded or followed by a name character".
        if re.search(rf"(?<![0-9a-z_]){re.escape(column)}(?![0-9a-z_])", text):
            return column
    return None


def mentions_missing_identity_column(error: Any) -> bool:
    """Does ``error`` say the ``engine_identity`` column does not exist?

    Takes anything stringifiable — an exception, or the list of per-row error
    dicts ``insert_rows_json`` RETURNS rather than raises. Both spellings of the
    same fact have to be recognised, and neither is a typed exception.
    """
    text = str(error).lower()
    if "engine_identity" not in text:
        return False
    return any(marker in text for marker in _MISSING_COLUMN_MARKERS)


def note_identity_query_degraded(error: Any) -> None:
    """One log per process: the dedup hint is off until the column exists."""
    global _identity_query_degraded_logged
    if _identity_query_degraded_logged:
        return
    _identity_query_degraded_logged = True
    logger.error(
        "sweep_dedup_hint_disabled: the %s column does not exist on "
        "scenario_sweeps yet, so the dedup hint is unavailable. Submissions "
        "still launch and the Job still deduplicates. Fix: ALTER TABLE "
        "<dataset>.scenario_sweeps ADD COLUMN engine_identity STRING (the "
        "Job's writer reconcile adds it too, on its next run). Underlying "
        "error: %s", ENGINE_IDENTITY_ENV, str(error)[:300])


def note_optional_column_insert_degraded(column: str, error: Any) -> None:
    """One log per process PER COLUMN: rows are landing without ``column``.

    Per column rather than per process, because two missing columns are two
    separate migrations an operator has to run, and one shared flag would hide
    the second behind the first.
    """
    if column in _insert_degraded_logged:
        return
    _insert_degraded_logged.add(column)
    column_type = dict(ADDITIVE_OPTIONAL_COLUMNS).get(column, "STRING")
    logger.error(
        "%s_column_missing: scenario_sweeps has no %s column, so this "
        "`submitted` row was written WITHOUT it. The row is otherwise complete "
        "and the sweep runs normally; the Job's own rows will carry the value "
        "once the column exists. Fix: ALTER TABLE <dataset>.scenario_sweeps "
        "ADD COLUMN %s %s. Underlying error: %s",
        column, column, column, column_type, str(error)[:300])


def note_identity_insert_degraded(error: Any) -> None:
    """The ``engine_identity`` case of the above, kept under its own name
    because that is the one PR-a's tests and the rollout runbook reference."""
    note_optional_column_insert_degraded("engine_identity", error)


def _reset_engine_identity_warning() -> None:
    """Test seam: re-arm every one-shot log. Never called in production."""
    global _identity_absence_logged
    global _identity_query_degraded_logged
    _identity_absence_logged = False
    _identity_query_degraded_logged = False
    _insert_degraded_logged.clear()


SWEEPS_TABLE = "scenario_sweeps"
RUNS_TABLE = "scenario_runs"

STATUS_SUBMITTED = "submitted"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_DEDUPLICATED = "deduplicated"
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_DEDUPLICATED)

# `persist.STATUS_RANK`, and the ORDER BY built from it. Rows of one submission
# share `submitted_at` (it is the partition key), so `written_at` is the only
# real clock; the CASE is the tiebreak for two rows stamped in one microsecond.
# A divergence here would make the API report a finished sweep as still running.
# `done` outranks `failed`: the one realistic same-microsecond collision is the
# launch path (this API writes `failed` when `jobs.run` errors, while an
# execution that in fact started writes `done`), and a sweep whose cells are in
# the table is done whatever a launch-side timeout thought.
STATUS_RANK = {
    STATUS_SUBMITTED: 0,
    STATUS_RUNNING: 1,
    STATUS_DEDUPLICATED: 2,
    STATUS_FAILED: 3,
    STATUS_DONE: 4,
}
LATEST_STATUS_ORDER_BY = (
    "written_at DESC, "
    "CASE status "
    + " ".join(f"WHEN '{status}' THEN {rank}"
               for status, rank in sorted(STATUS_RANK.items()))
    + " ELSE -1 END DESC"
)

# ============================================================================ #
# Caps (D6 / §Scope 5)
# ============================================================================ #

MAX_SYMBOLS = 12
MAX_SCENARIOS = 20          # declared arms, before the implicit `base`
MAX_CELLS = 240             # (arms incl. base) x symbols x splits
MAX_WINDOW_DAYS = 730
MIN_HOLDOUT_DAYS = 60
MIN_STARTING_CASH = 10_000.0
MAX_STARTING_CASH = 1_000_000.0
# Cloud Run caps a container's whole environment at 32 KiB and the spec is one
# variable among several. Mirrored by `main.MAX_SPEC_BYTES` on the Job side.
MAX_SPEC_BYTES = 24 * 1024

# The Job's `--task-timeout`, in seconds. MUST match `deploy-sweep-job` in
# cloudbuild.yaml (pinned by a contract test). It is the API's clock for
# everything about liveness: a `running` sweep cannot outlive it, because Cloud
# Run kills the task at that point.
JOB_TASK_TIMEOUT_SECONDS = 10_800   # 3h

# Grace on top of the task timeout before a non-terminal row is declared dead.
# Covers container start (3-4 min) plus the SIGTERM window in which the Job
# writes its terminal row.
STALE_GRACE_MINUTES = 10

# A `submitted` row with no `running` row after this long renders as "stuck".
# Container start is 3-4 minutes, so 10 leaves real headroom. It is a LABEL, not
# a cancel: nothing here can stop an execution, and pretending otherwise would
# be worse than saying so (D3).
STUCK_AFTER_MINUTES = 10

# `\Z`, not `$` — Python's `$` also matches before a trailing newline. The
# value is stripped before it reaches here, so this is defence in depth
# rather than a live hole; the anchor is spelled correctly in all four
# places (here, `identity.SCENARIO_NAME_RE`, `identity.SYMBOL_RE`,
# `services/artifacts.RUN_ID_RE`) so none of them is the one that rots.
# Leading character is a LETTER, not `[A-Z.]`. The old `^[A-Z.]{1,6}$`
# accepted `"."` and `".AB"` — symbols no exchange lists, and (since
# FC-096 Phase B) symbols the artifact endpoint cannot address, so a
# sweep submitted under one would write evidence that is permanently
# unreachable. Caught by the subset test in tests/
# test_dashboard_artifacts.py, which pins this rule as strictly narrower
# than `identity.SYMBOL_RE`.
SYMBOL_RE = re.compile(r"^[A-Z][A-Z.]{0,5}\Z")

SPEC_FIELDS = frozenset({
    "symbols", "start", "end", "holdout_start", "starting_cash",
    "run_sensitivity", "scenarios", "force",
})
SCENARIO_FIELDS = frozenset({"name", "overrides", "fill_haircut"})

# `MAX_SCENARIO_NAME_CHARS` / `SCENARIO_NAME_RE` / `validate_scenario_name` are
# IMPORTED from `identity.py` above, not defined here: `main._scenarios_from_entries`
# applies the same rule to the CLI/YAML path, and a second copy would let a
# `--persist` sweep land a name this API would have refused.

# What each allowlisted key's value must LOOK like. The allowlist says which
# knobs may be turned; this says what a legal setting of one is.
#
# Without it the API accepts `{"strategy.min_put_premium": "not-a-number"}` and
# the arm dies three minutes into a container start with a TypeError from deep
# inside the strategy — or worse, does not die: a string where a float is
# expected can silently compare false everywhere and read as "this floor
# rejected everything". `validate_override_key` deliberately checks only the KEY
# (plus the one value rule for `call_target_dte`), so the values are checked
# here, once, on the boundary.
_NUM = "number"
_BOOL = "bool"
_INT = "int"
_BAND = "band"          # [lo, hi], both numbers, lo < hi, both in [0, 1]
_SYMBOLS = "symbols"    # list of tickers
OVERRIDE_VALUE_TYPES: Dict[str, str] = {
    "strategy.put_delta_range": _BAND,
    "strategy.call_delta_range": _BAND,
    "strategy.min_put_premium": _NUM,
    "strategy.min_call_premium": _NUM,
    # Both DTE targets, allowlisted since FC-096 Phase A PR-2. The RANGE
    # (1..MAX_SWEEPABLE_DTE) is the engine's rule and is enforced by the imported
    # `validate_override_key`, which runs first; this table only says the value
    # must be a whole number, so the two checks cannot drift apart on the bound.
    "strategy.put_target_dte": _INT,
    "strategy.call_target_dte": _INT,
    "strategy.min_stock_price": _NUM,
    "strategy.max_stock_price": _NUM,
    "strategy.min_avg_volume": _NUM,
    "risk.max_position_size": _NUM,
    "universe.excluded_symbols": _SYMBOLS,
    "universe.max_spread_pct": _NUM,
    "earnings.enabled": _BOOL,
    "earnings.blackout_days": _INT,
    "rolling.enabled": _BOOL,
    "rolling.itm_trigger_ratio": _NUM,
    "rolling.max_extension_days": _INT,
    "rolling.max_replacement_delta": _NUM,
    "rolling.min_net_credit_per_contract": _NUM,
    "rolling.imminence_extrinsic_threshold": _NUM,
}


def _is_number(value: Any) -> bool:
    """A real number. `True` is not one: `bool` is an `int` subclass in Python,
    and accepting it here would let `min_put_premium: true` mean `1.0`."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_override_value(key: str, value: Any) -> None:
    """Raise ``SweepValidationError`` unless ``value`` is a legal setting of ``key``."""
    kind = OVERRIDE_VALUE_TYPES.get(key)
    if kind is None:
        # An allowlisted key with no declared value shape. Refused rather than
        # waved through: the table above and `ALLOWED_OVERRIDES` are kept in
        # step by a test, so reaching here means somebody added a knob without
        # saying what a legal setting of it is.
        raise SweepValidationError(
            f"override '{key}' has no declared value type in this API; it "
            f"cannot be validated, so it is refused rather than passed through "
            f"unchecked"
        )
    if kind == _BOOL:
        if not isinstance(value, bool):
            raise SweepValidationError(
                f"override '{key}' must be true or false; got {value!r}")
        return
    if kind == _INT:
        if not isinstance(value, int) or isinstance(value, bool):
            raise SweepValidationError(
                f"override '{key}' must be a whole number of days; got {value!r}")
        return
    if kind == _NUM:
        if not _is_number(value):
            raise SweepValidationError(
                f"override '{key}' must be a number; got {value!r}")
        return
    if kind == _BAND:
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or not all(_is_number(v) for v in value)):
            raise SweepValidationError(
                f"override '{key}' must be a two-element [lo, hi] band of "
                f"numbers; got {value!r}")
        lo, hi = float(value[0]), float(value[1])
        if not 0.0 <= lo < hi <= 1.0:
            raise SweepValidationError(
                f"override '{key}' band must satisfy 0 <= lo < hi <= 1 (delta "
                f"is a probability); got [{lo}, {hi}]")
        return
    if kind == _SYMBOLS:
        if not isinstance(value, list) or not all(
                isinstance(v, str) and SYMBOL_RE.match(v.strip().upper())
                for v in value):
            raise SweepValidationError(
                f"override '{key}' must be a list of tickers; got {value!r}")
        return


class SweepValidationError(ValueError):
    """A spec the runner would refuse. Surfaced verbatim as HTTP 422."""


# ============================================================================ #
# Validation (D7)
# ============================================================================ #

def _parse_date(value: Any, field: str):
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise SweepValidationError(
            f"'{field}' must be an ISO date (YYYY-MM-DD); got {value!r}"
        )


def validate_spec(spec: Any) -> Dict[str, Any]:
    """Return the normalised spec, or raise ``SweepValidationError``.

    Normalised, not merely checked: symbols are upper-cased, dates are
    re-rendered ISO, defaults are filled in. The returned dict is what gets
    hashed into ``sweep_key`` and what gets shipped to the Job, so validation and
    canonicalisation cannot disagree about what was submitted.

    Every rejection carries the reason the runner would give — for an override
    that means the exact ``OverrideError`` text, which explains *why* the arm
    would not have measured what it claims rather than saying "not allowed".
    """
    if not isinstance(spec, dict):
        raise SweepValidationError(
            f"expected a JSON object; got {type(spec).__name__}"
        )
    unknown = set(spec) - SPEC_FIELDS
    if unknown:
        raise SweepValidationError(
            f"unknown field(s) {sorted(unknown)}. A misspelled field would "
            f"silently do nothing — 'holdout' instead of 'holdout_start' would "
            f"run in-sample and report itself as validated. "
            f"Known fields: {sorted(SPEC_FIELDS)}."
        )
    force = spec.get("force", False)
    if not isinstance(force, bool):
        raise SweepValidationError("'force' must be true or false")

    # -- symbols -----------------------------------------------------------
    raw_symbols = spec.get("symbols")
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise SweepValidationError("'symbols' must be a non-empty list")
    symbols: List[str] = []
    for entry in raw_symbols:
        sym = str(entry).strip().upper()
        if not SYMBOL_RE.match(sym):
            raise SweepValidationError(
                f"symbol {entry!r} is not a plausible ticker "
                f"(expected 1-6 characters matching {SYMBOL_RE.pattern})"
            )
        if sym in symbols:
            raise SweepValidationError(
                f"symbol {sym} appears twice; the universe is a set — a repeat "
                f"would materialise once and render as one column anyway"
            )
        symbols.append(sym)
    if len(symbols) > MAX_SYMBOLS:
        raise SweepValidationError(
            f"{len(symbols)} symbols exceeds the cap of {MAX_SYMBOLS}. Each one "
            f"costs a materialisation (~25-40 s warm, minutes cold) on a "
            f"single-vCPU Job."
        )

    # -- window ------------------------------------------------------------
    start = _parse_date(spec.get("start"), "start")
    end = _parse_date(spec.get("end"), "end")
    if start is None or end is None:
        raise SweepValidationError("'start' and 'end' are both required")
    if end <= start:
        raise SweepValidationError(f"'end' ({end}) must be after 'start' ({start})")
    if (end - start).days > MAX_WINDOW_DAYS:
        raise SweepValidationError(
            f"window is {(end - start).days} days, over the "
            f"{MAX_WINDOW_DAYS}-day cap"
        )
    holdout_start = _parse_date(spec.get("holdout_start"), "holdout_start")
    if holdout_start is not None:
        if not (start < holdout_start <= end):
            raise SweepValidationError(
                f"'holdout_start' {holdout_start} must fall inside "
                f"({start}, {end}]; otherwise one of the two windows is empty"
            )
        if (end - holdout_start).days < MIN_HOLDOUT_DAYS:
            raise SweepValidationError(
                f"holdout window is {(end - holdout_start).days} days; at least "
                f"{MIN_HOLDOUT_DAYS} are required. A cycle needs a put written, "
                f"held and resolved, so a short holdout comes back as `insuf` on "
                f"symbols that traded perfectly well — and an `insuf` column is "
                f"read as a verdict on the arm."
            )

    # -- cash / sensitivity -------------------------------------------------
    cash = spec.get("starting_cash")
    cash = DEFAULT_STARTING_CASH if cash is None else cash
    try:
        cash = float(cash)
    except (TypeError, ValueError):
        raise SweepValidationError(f"'starting_cash' must be a number; got {cash!r}")
    if not MIN_STARTING_CASH <= cash <= MAX_STARTING_CASH:
        raise SweepValidationError(
            f"'starting_cash' {cash:,.0f} is outside "
            f"[{MIN_STARTING_CASH:,.0f}, {MAX_STARTING_CASH:,.0f}]"
        )
    run_sensitivity = spec.get("run_sensitivity", False)
    if not isinstance(run_sensitivity, bool):
        raise SweepValidationError("'run_sensitivity' must be true or false")

    # -- scenarios ----------------------------------------------------------
    raw_scenarios = spec.get("scenarios")
    if not isinstance(raw_scenarios, list):
        raise SweepValidationError("'scenarios' must be a list (it may be empty)")
    if len(raw_scenarios) > MAX_SCENARIOS:
        raise SweepValidationError(
            f"{len(raw_scenarios)} scenarios exceeds the cap of {MAX_SCENARIOS}"
        )
    scenarios: List[Dict[str, Any]] = []
    seen: set = set()
    for i, entry in enumerate(raw_scenarios):
        if not isinstance(entry, dict):
            raise SweepValidationError(f"scenario #{i + 1} must be an object")
        extra = set(entry) - SCENARIO_FIELDS
        if extra:
            raise SweepValidationError(
                f"scenario #{i + 1} has unknown field(s) {sorted(extra)}; "
                f"known: {sorted(SCENARIO_FIELDS)}"
            )
        name = str(entry.get("name") or "").strip()
        if not name:
            raise SweepValidationError(f"scenario #{i + 1} has no 'name'")
        try:
            validate_scenario_name(name, f"scenario #{i + 1}")
        except ValueError as exc:
            raise SweepValidationError(str(exc))
        if name == BASE_SCENARIO_NAME:
            raise SweepValidationError(
                f"'{BASE_SCENARIO_NAME}' is reserved: it is the comparator every "
                f"delta and the whole sign-agreement column is measured against, "
                f"and it is added automatically. An arm that redefined it would "
                f"move every other number in the table while looking ordinary."
            )
        if name in seen:
            raise SweepValidationError(
                f"duplicate scenario name {name!r}: cells are keyed by "
                f"(scenario, symbol, split), so two arms sharing a name would "
                f"overwrite each other in the grid"
            )
        seen.add(name)

        overrides = entry.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise SweepValidationError(
                f"scenario '{name}' has a non-object 'overrides'"
            )
        for key, value in overrides.items():
            try:
                # The Job's own validator, imported. Its message names the actual
                # reason (which cached chains store, which knob the replay never
                # reads), which is the only kind of rejection a reader learns
                # anything from.
                validate_override_key(str(key), value)
            except OverrideError as exc:
                raise SweepValidationError(f"scenario '{name}': {exc}")
            # ...and then the VALUE. The runner's validator checks the key (plus
            # the one `call_target_dte` rule); a wrong-typed value would sail
            # past it and fail three minutes into a container start, or not fail
            # at all and read as a threshold that rejected everything.
            try:
                validate_override_value(str(key), value)
            except SweepValidationError as exc:
                raise SweepValidationError(f"scenario '{name}': {exc}")

        haircut = entry.get("fill_haircut")
        if haircut is not None:
            try:
                haircut = float(haircut)
            except (TypeError, ValueError):
                raise SweepValidationError(
                    f"scenario '{name}' fill_haircut={haircut!r} is not a number"
                )
            if not 0.0 <= haircut <= 1.0:
                raise SweepValidationError(
                    f"scenario '{name}' fill_haircut={haircut} is outside "
                    f"[0, 1] (0 = mid, 1 = at the bid)"
                )
        scenarios.append({
            "name": name,
            "overrides": {str(k): v for k, v in overrides.items()},
            "fill_haircut": haircut,
        })

    # -- the cell budget ----------------------------------------------------
    arms = len(scenarios) + 1                      # the implicit `base`
    splits = 2 if holdout_start is not None else 1
    cells = arms * len(symbols) * splits
    if cells > MAX_CELLS:
        raise SweepValidationError(
            f"{arms} arms x {len(symbols)} symbols x {splits} split(s) = {cells} "
            f"cells, over the cap of {MAX_CELLS}. Replay is ~1-2 s per cell on "
            f"the Job's single vCPU, on top of one materialisation per "
            f"(symbol, split)."
        )

    normalised = {
        "symbols": symbols,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "holdout_start": holdout_start.isoformat() if holdout_start else None,
        "starting_cash": cash,
        "run_sensitivity": run_sensitivity,
        "scenarios": scenarios,
    }
    # Carried into the spec the Job receives, and EXCLUDED from `sweep_key`
    # (identity.NON_IDENTITY_FIELDS): a forced re-run must key identically to the
    # run it is deliberately reproducing, or the two are never comparable and a
    # second force would not dedup either.
    if force:
        normalised["force"] = True
    encoded = json.dumps(normalised, sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_SPEC_BYTES:
        raise SweepValidationError(
            f"the normalised spec is {len(encoded.encode('utf-8'))} bytes, over "
            f"the {MAX_SPEC_BYTES}-byte transport limit"
        )
    return normalised


def cell_count(spec: Dict[str, Any]) -> int:
    """Cells a validated spec will produce: arms (incl. base) x symbols x splits."""
    arms = len(spec.get("scenarios") or []) + 1
    splits = 2 if spec.get("holdout_start") else 1
    return arms * len(spec.get("symbols") or []) * splits


# ============================================================================ #
# Auth — RETIRED (FC-096 Phase D PR-2)
#
# `extract_bearer` and `token_matches` lived here, and with them the whole
# `SWEEP_SUBMIT_TOKEN` gate: a shared secret pasted into a browser field,
# compared with `hmac.compare_digest` because the origin was reachable by
# `allUsers` and a byte-at-a-time compare was a real remote timing oracle.
#
# Both are DELETED rather than left unused. Authentication now happens one
# layer up — Identity-Aware Proxy admits the request and stamps a signed
# assertion — and authorization in `services/auth.py`, which is FastAPI-free
# for exactly the reason this module is. A retired credential path that still
# compiles is a path something can call again; the deletion is what makes
# "the token is gone" checkable by grep rather than by reading.
#
# The secret's own lifecycle is an OPERATOR step, in this order and no other:
# this revision deploys first, THEN `gcloud run services update
# --remove-secrets=SWEEP_SUBMIT_TOKEN`, THEN the `sweep-submit-token` secret is
# deleted.
#
# DELETING IT FIRST DOES NOT BREAK "THE NEXT REVISION" - IT BREAKS THIS ONE.
# The binding is a PINNED SECRET VERSION on the LIVE revision's spec, and the
# dashboard runs `--min-instances=0`: there is no container most of the time, so
# the next request is a COLD START, and a cold start whose `--set-secrets`/
# `--update-secrets` names a secret that no longer exists cannot resolve it and
# the instance never comes up. The dashboard goes down on the next page load,
# not at the next merge, and the symptom is a 5xx with nothing in the
# application logs - because the application never started. The unbind is what
# makes the deletion safe: it creates a NEW revision with no such binding, and
# only then is the secret unreferenced.
# ============================================================================ #


# ============================================================================ #
# Launch (D5)
# ============================================================================ #

def job_run_url(project: str, job: str, region: str = "us-central1") -> str:
    """Cloud Run **v2** ``jobs.run``. v1 has a different path and body shape."""
    return (f"https://run.googleapis.com/v2/projects/{project}/locations/"
            f"{region}/jobs/{job}:run")


def launch_body(*, spec_json: str, run_id: str, submitted_at: str,
                submitted_via: str = "dashboard") -> Dict[str, Any]:
    """The ``jobs.run`` request body: per-execution container env overrides.

    ``containerOverrides[].env`` MERGES with the Job's own environment rather
    than replacing it, which is what lets one Job definition serve every spec —
    ``GCP_PROJECT``, ``CHAIN_LAKE_BUCKET``, ``GIT_COMMIT`` and the three secrets
    stay bound by the Job, and only the four variables below vary per execution.

    ``SWEEP_SUBMITTED_AT`` is carried so the Job's rows land in the same
    ``submitted_at`` partition — and therefore the same run — as the
    ``submitted`` row this API wrote. Without it the Job would stamp its own
    clock and one submission would be split across two partitions, which a
    partition-pruned read would see as half a timeline.
    """
    return {
        "overrides": {
            "containerOverrides": [
                {
                    "env": [
                        {"name": "SWEEP_SPEC_JSON", "value": spec_json},
                        {"name": "SWEEP_RUN_ID", "value": run_id},
                        {"name": "SWEEP_SUBMITTED_AT", "value": submitted_at},
                        {"name": "SWEEP_SUBMITTED_VIA", "value": submitted_via},
                    ]
                }
            ]
        }
    }


def forbidden_detail(project: str, job: str, sa: str) -> str:
    """The 403 -> 502 body: what to grant, to whom, on what (D5).

    A 403 from ``jobs.run`` means one thing and one thing only — the dashboard's
    service account cannot execute this Job — and the grant is console-only on
    this project (the Cloud Resource Manager API is disabled, so
    ``add-iam-policy-binding`` does not work). An error that says "403" and stops
    sends an operator to the wrong page.
    """
    return (
        f"Cloud Run refused the launch with 403: the dashboard's service account "
        f"({sa}) lacks run.jobs.run on the '{job}' Job. Grant it in the console "
        f"(project IAM is not writable by CLI here — the Cloud Resource Manager "
        f"API is disabled): Cloud Run > Jobs > {job} > Permissions > add "
        f"{sa} as 'Cloud Run Invoker' (or 'Cloud Run Developer' on project "
        f"{project}). Nothing was launched and no row was written."
    )


# ============================================================================ #
# Rows and identity
# ============================================================================ #

def new_run_id() -> str:
    """16 hex characters, matching the Job's own ``uuid4().hex[:16]``."""
    return uuid.uuid4().hex[:16]


def compute_sweep_key(spec: Dict[str, Any],
                      engine_identity: Optional[str]) -> str:
    """The Job's key, computed from the identity baked into this image.

    The second argument was ``git_commit`` until FC-096 Phase B. Callers that
    have no identity should not call this at all — see
    ``engine_identity_from_env`` for why an absent identity disables the hint
    instead of keying over ``""``.
    """
    return sweep_key(spec, engine_version=ENGINE_VERSION,
                     engine_identity=engine_identity)


def submitted_row(*, run_id: str, spec: Dict[str, Any],
                  sweep_key_value: Optional[str],
                  submitted_at: str, git_commit: Optional[str],
                  engine_identity: Optional[str] = None,
                  execution_name: Optional[str] = None,
                  status: str = STATUS_SUBMITTED,
                  deduplicated_to: Optional[str] = None) -> Dict[str, Any]:
    """The ``scenario_sweeps`` row this API writes.

    Deliberately the same column set ``persist.status_row`` produces — pinned by
    ``TestTheSubmittedRowMatchesTheJobsRowShape``, because a column the API
    omits is a column the results view silently renders as blank for every
    dashboard-launched sweep while looking correct for CLI ones.

    The API never fills ``base_config_json`` / ``base_config_hash``: it has no
    Config. The Job's ``running`` row carries them, and the reader takes the
    latest row, so nothing is lost.
    """
    return {
        "run_id": run_id,
        "sweep_key": sweep_key_value,
        "status": status,
        "deduplicated_to": deduplicated_to,
        "submitted_at": submitted_at,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "finished_at": (datetime.now(timezone.utc).isoformat()
                        if status in TERMINAL_STATUSES else None),
        "submitted_via": "dashboard",
        "execution_name": execution_name,
        "git_commit": git_commit,
        "engine_version": ENGINE_VERSION,
        # Stamped from the image's baked-in value. NULL when the build-arg never
        # arrived — the same state that disables the hint — and the Job's own
        # `running`/`done` rows carry the correct value regardless, so the run is
        # still dedup-reachable once it finishes.
        "engine_identity": engine_identity,
        "base_config_hash": None,
        "base_config_json": None,
        "spec_json": json.dumps(spec, sort_keys=True),
        "symbols": list(spec.get("symbols") or []),
        "window_start": spec.get("start"),
        "window_end": spec.get("end"),
        "holdout_start": spec.get("holdout_start"),
        "in_sample_only": not spec.get("holdout_start"),
        "scenario_count": len(spec.get("scenarios") or []) + 1,
        "cell_count": cell_count(spec),
        "wall_seconds": None,
        "materialise_seconds": None,
        "replay_seconds": None,
        "provider_fetches": None,
        "bar_cache_hits": None,
        "lake_summary_json": None,
        "error": None,
        # Completeness, filled in by the Job's terminal row. NULL here is what
        # keeps a `submitted`/`failed` row out of the dedup: `find_done_sweep`
        # requires `rows_persisted = cell_count`.
        "rows_persisted": None,
        "error_cells": None,
        # FC-096 Phase B B2. The API writes no artifacts (it has run nothing),
        # so this is NULL here and filled by the Job's terminal row — but
        # PRESENT, because every status row of a run must carry the same column
        # set or the two writers diverge by whichever one happened to know a
        # field.
        "artifacts_complete": None,
        # FC-096 Phase B PR-c. The API launches the JOB, whose liveness bound is
        # `JOB_TASK_TIMEOUT_SECONDS` — the reader's default — so this is NULL
        # here by meaning, not by omission. Present because every status row of
        # a run must carry the same column set.
        "liveness_seconds": None,
        # The API has no Config, so it cannot compute the engine hash either.
        "engine_config_hash": None,
        # FC-096 A4. Only a replay knows which symbols the earnings table was
        # missing; the API has run nothing. NULL here, filled by the Job's
        # terminal row — but PRESENT, because every status row of a run must
        # carry the same column set or the two writers diverge by whichever one
        # happened to know a field.
        "earnings_symbols_without_data": None,
        # FC-096 Phase B B4. A dashboard submission is never a pin's weekly
        # re-measurement — only `main.run_battery_cmd` stamps this — so it is
        # NULL here by MEANING, not by omission. Present for the column-set
        # rule above.
        "pin_id": None,
    }


def stale_cutoff(now: Optional[datetime] = None) -> datetime:
    """The moment before which a non-terminal row cannot still be alive.

    ``JOB_TASK_TIMEOUT_SECONDS`` plus a grace, measured from the row's own
    ``written_at``. Cloud Run kills the task at the timeout, so a `running` row
    older than that has no process behind it — whatever it says.

    The previous rule (``submitted_at`` + 1 h) was wrong in both directions once
    the timeout went to 3 h for cold materialisation: it released the lock while
    a legitimate sweep was still replaying (letting a second one contend for the
    same chain cache), and it measured from the submission rather than from the
    last sign of life, so a run that was demonstrably alive 5 minutes ago was
    judged by when it started.
    """
    now = now or datetime.now(timezone.utc)
    return now - timedelta(seconds=JOB_TASK_TIMEOUT_SECONDS
                           + STALE_GRACE_MINUTES * 60)


def row_liveness_seconds(row: Dict[str, Any]) -> int:
    """How long THIS row's writer may go quiet before it is presumed dead.

    ``liveness_seconds`` off the row when it carries a usable one (FC-096 Phase
    B PR-c), else ``JOB_TASK_TIMEOUT_SECONDS``.

    **NULL is not zero and not a default worth inventing.** Every row written
    before the column existed, and every row the sweep Job writes today, has no
    value — and for those the Job's task timeout is exactly right, because Cloud
    Run kills the task at that point and nothing will ever write their terminal
    row afterwards. Only a writer with a *shorter* life says so, and the sim
    service is the first one: a Cloud Run service instance scaled in mid-replay
    dies in seconds, and holding the one-at-a-time lock for 3 h 10 m on it would
    take the feature offline for the rest of the afternoon.

    Junk is treated as absence, deliberately. A non-numeric or non-positive
    value would otherwise shorten the bound to nothing and release the lock
    under a run that is still going — the one direction of error that lets two
    replays contend for one chain cache. An unreadable stamp falls back to the
    conservative clock and says nothing; the row is still visible and still
    terminalises normally.
    """
    raw = row.get("liveness_seconds")
    # `bool` is an `int` in Python, so `True` would become a ONE-SECOND bound
    # and release the lock under every live run. Treated as absence.
    if raw is None or isinstance(raw, bool):
        return JOB_TASK_TIMEOUT_SECONDS
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return JOB_TASK_TIMEOUT_SECONDS
    return seconds if seconds > 0 else JOB_TASK_TIMEOUT_SECONDS


def row_stale_cutoff(row: Dict[str, Any],
                     now: Optional[datetime] = None) -> datetime:
    """``stale_cutoff`` for one row, honouring its own liveness bound.

    The grace stays ``STALE_GRACE_MINUTES`` whatever the bound: it covers the
    window in which a writer that has just been told to stop is writing its
    terminal row, and that window is a property of Cloud Run's SIGTERM handling,
    not of how long the writer was allowed to run. 900 s + 10 min = ~25 min for
    the sim service; unchanged 3 h 10 m for the Job.
    """
    now = now or datetime.now(timezone.utc)
    return now - timedelta(seconds=row_liveness_seconds(row)
                           + STALE_GRACE_MINUTES * 60)


def _last_seen(row: Dict[str, Any]) -> Optional[datetime]:
    """The row's own clock: ``written_at``, falling back to ``submitted_at``."""
    return (_as_datetime(row.get("written_at"))
            or _as_datetime(row.get("submitted_at")))


def blocking_sweep(rows: Sequence[Dict[str, Any]],
                   now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The non-terminal sweep that must block a new submission, or None (D6).

    One sweep at a time: the Job is a single 1-vCPU container and two concurrent
    executions would contend on the same chain cache while each reporting the
    other's fetches as its own.

    **Two expiries, because the two states mean different things.**

    * A ``running`` row holds the lock until ``row_stale_cutoff`` — the row's
      OWN liveness bound plus a grace — because until then a legitimate cold
      sweep may well still be replaying, and releasing early lets a second
      execution contend with it for the same chain cache. The bound is the
      Job's task timeout for a Job row (and for every row written before
      FC-096 Phase B PR-c) and 900 s for a sim-service row, whose writer is a
      Cloud Run *service* instance that can be scaled in mid-replay.
    * A ``submitted`` row holds it only until ``STUCK_AFTER_MINUTES``. It has
      already been declared *stuck* at that point (``is_stuck``), and a
      submission that produced no ``running`` row within ten minutes — three to
      four times container start — is not running. Holding the lock for the full
      three hours on a launch that never happened was the round-1 regression: the
      row rendered "stuck — check the execution" while the endpoint went on
      refusing every submit for another three hours.

    A lock expiring at all is deliberate: a Job killed before its ``finally``
    (OOM, eviction, a cancelled execution) leaves a row nothing will ever
    terminalise, and a permanent lock on a dead run takes the feature offline
    with no way back except a manual BigQuery insert.
    """
    now = now or datetime.now(timezone.utc)
    submitted_cutoff = now - timedelta(minutes=STUCK_AFTER_MINUTES)
    for row in rows:
        status = row.get("status")
        if status in TERMINAL_STATUSES:
            continue
        stamp = _last_seen(row)
        if stamp is None:
            return row
        if status == STATUS_SUBMITTED:
            if stamp < submitted_cutoff:
                continue
        # PER ROW, not one cutoff for the batch (FC-096 Phase B PR-c): a
        # `running` row written by the sim service carries a 900 s bound and a
        # Job row carries none, and the two can be in the same history list.
        elif stamp < row_stale_cutoff(row, now):
            continue
        return row
    return None


def is_stuck(row: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """Whether a non-terminal row should render as "stuck — check the execution".

    Two shapes, because they mean different things:

    * ``submitted`` with no ``running`` row after ``STUCK_AFTER_MINUTES`` — the
      Job never reported in. Container start is 3-4 minutes, so 10 is real
      headroom rather than crying wolf.
    * ``running`` past ``row_stale_cutoff`` — whatever was running it has been
      killed by now (the Job's ``--task-timeout``, or a scaled-in service
      instance past its stamped ``liveness_seconds``), so nothing will ever
      write its terminal row.

    **Both clocks are ``_last_seen`` (``written_at``), the same one
    ``blocking_sweep`` uses.** They must agree, and reading ``submitted_at`` here
    made them disagree for up to ~30 s: the API writes a second ``submitted``
    row after the launch to record ``execution_name``, so the row's
    ``written_at`` moves while ``submitted_at`` does not. A row could therefore
    be labelled stuck while still holding the lock, or the reverse — and the two
    are read side by side in the UI.

    A LABEL, not a cancel. D3 is explicit that nothing here polls or cancels an
    execution: ``run.executions.get`` is unproven for this service account and
    grantable only in the console, so the honest thing is to say the Job stopped
    reporting and name the execution to look at.
    """
    status = row.get("status")
    if status in TERMINAL_STATUSES:
        return False
    now = now or datetime.now(timezone.utc)
    stamp = _last_seen(row)
    if stamp is None:
        return False
    if status == STATUS_RUNNING:
        # The row's own bound (FC-096 Phase B PR-c). It MUST be the same clock
        # `blocking_sweep` uses, for the reason the paragraph above gives about
        # `written_at`: the two are read side by side in the UI, and a row
        # labelled stuck while still holding the lock is a contradiction the
        # operator has to resolve by guessing.
        return stamp < row_stale_cutoff(row, now)
    if status != STATUS_SUBMITTED:
        return False
    return now - stamp > timedelta(minutes=STUCK_AFTER_MINUTES)


def _json_list(value: Any) -> List[str]:
    """A stored JSON array of strings, or ``[]`` for anything unreadable.

    Never raises and never 500s the results page: the column is NULL on every
    run written before it existed, and a page that cannot render an old sweep is
    worse than one that renders it without a caveat it never recorded.
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def _as_date_str(value: Any) -> Optional[str]:
    """A stored DATE as ``YYYY-MM-DD``. BigQuery hands back ``datetime.date``."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _as_datetime(value: Any) -> Optional[datetime]:
    """Whatever BigQuery handed back, as an aware UTC datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    if text.endswith(" UTC"):
        text = text[:-4].replace(" ", "T") + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ============================================================================ #
# Pins (FC-096 Phase B B4)
#
# A pin is a spec the weekly battery re-measures for ever, until somebody
# un-pins it. Everything about one that could be got wrong is decided here, in
# the file the bot CI image can actually run, for this module's stated reason:
# `routers/v2.py` is a thin caller.
#
# The table is `persist.py`'s (one schema owner, and it is the side that knows
# every column); this file writes rows into it and reads them back, exactly as
# it does for `scenario_sweeps`.
# ============================================================================ #

PINS_TABLE = "scenario_pins"

# ROLLING PINS (FC-096 D1: "pinned combos re-measured weekly").
#
# A pin stores the SHAPE of its window — how many calendar days it spans and
# how many of those are holdout — and the battery re-anchors both to
# `last_settled_day()` every Saturday. The absolute dates the operator
# submitted are kept in `spec_json` as the record of what they typed; they are
# not what gets replayed.
#
# The alternative, which this PR's first build shipped by reusing
# `validate_spec` unchanged, is a pin frozen to a historical window. Its answer
# cannot change, so the engine-identity dedup hits on the SECOND Saturday and
# every one after it, and the pin's trend series holds exactly one point
# forever. That is not an inefficiency; it is the feature silently not
# existing, and nothing in the UI would have said so.

# `persist.MAX_ACTIVE_PINS` — FC-096 D1's "capped ~20". Pinned equal by a test.
#
# The cap is a WRITE-time rule and it is enforced here, because this is the only
# side that can say what to do about it ("un-pin one first" needs the list). The
# battery deliberately does NOT truncate to it: silently dropping measurement is
# worse than a long Saturday, and its wall cap is what bounds the execution.
MAX_ACTIVE_PINS = 20

# `persist.PIN_NOTE_MAX_CHARS`. A note is a reminder to its author, not a
# document, and it is operator-typed text that a public dashboard renders.
PIN_NOTE_MAX_CHARS = 200

# `persist.PINS_LATEST_ORDER_BY`. "Latest row wins" for one `pin_id`, with
# INACTIVE winning a same-microsecond tie — a create and a delete landing
# together must resolve to deleted, because the other direction leaves a pin the
# operator removed running every Saturday for ever. Pinned equal by a test for
# `LATEST_STATUS_ORDER_BY`'s reason: two sides disagreeing about which row is
# current would show a pin the battery does not run, or hide one it does.
PINS_LATEST_ORDER_BY = "written_at DESC, active ASC"

# The pin body's top-level fields. A closed set, like `SPEC_FIELDS`, and for the
# same reason: a misspelled `notes` would be silently dropped and the operator
# would find their reminder missing weeks later.
PIN_BODY_FIELDS = frozenset({"spec", "note"})


def new_pin_id() -> str:
    """16 hex characters, the same shape as a ``run_id`` (``persist.new_pin_id``).

    Deliberately not derived from the spec: two operators may pin the same
    question for different reasons, and un-pinning one must not un-pin the
    other. "No two ACTIVE pins with the same spec" is enforced below, where the
    refusal can name the pin that already asks it.
    """
    return uuid.uuid4().hex[:16]


def validate_pin_body(body: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """``(normalised_spec, note)`` for a pin request, or raise.

    The body is ``{"spec": {...}, "note": "..."}`` rather than a spec with a
    ``note`` beside its own fields, and the wrapper is deliberate:
    ``validate_spec`` refuses unknown top-level keys — that rule is what stops a
    typo'd ``holdout`` from silently running in-sample — so a note at the spec's
    level would either be refused or have to be stripped before validation, and
    stripping is how ``holdout`` becomes a note.

    The spec itself goes through ``validate_spec`` unchanged: a pin that the
    submit endpoint would refuse must be refused at pin time, not discovered by
    the battery three Saturdays later.

    ``force`` is refused ON A PIN specifically. It is an instruction about one
    submission ("replay this even though the answer is stored"), and a standing
    weekly instruction to ignore the dedup would make every battery re-replay
    that spec for ever — turning the cheap week the dedup exists to give into
    the expensive one, silently, until somebody read the bill.
    """
    if not isinstance(body, dict):
        raise SweepValidationError(
            f"expected a JSON object with a 'spec'; got {type(body).__name__}")
    unknown = set(body) - PIN_BODY_FIELDS
    if unknown:
        raise SweepValidationError(
            f"unknown field(s) {sorted(unknown)}. A pin is "
            f"{{\"spec\": {{...}}, \"note\": \"optional\"}} — the spec is "
            f"wrapped rather than spread so a misspelled spec field is still "
            f"refused as one. Known fields: {sorted(PIN_BODY_FIELDS)}.")
    if "spec" not in body:
        raise SweepValidationError(
            "a pin needs a 'spec': the body is "
            "{\"spec\": {...}, \"note\": \"optional\"}")
    spec = validate_spec(body["spec"])
    if spec.get("force"):
        raise SweepValidationError(
            "'force' cannot be pinned: it means 'replay this even though the "
            "answer is stored', and as a STANDING weekly instruction it would "
            "make the battery re-replay this spec every Saturday for ever. Pin "
            "the spec without it; submit a one-off forced run through POST "
            "/api/v2/sweeps when you actually want to bypass the dedup.")
    note = body.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise SweepValidationError(
                f"'note' must be a string; got {type(note).__name__}")
        note = note.strip() or None
        if note is not None and len(note) > PIN_NOTE_MAX_CHARS:
            raise SweepValidationError(
                f"'note' is {len(note)} characters, over the "
                f"{PIN_NOTE_MAX_CHARS}-character limit. It is a reminder, not a "
                f"document.")
    return spec, note


def pin_window_shape(spec: Dict[str, Any]) -> Tuple[int, Optional[int]]:
    """``(window_days, holdout_days)`` — the ROLLING shape of a pinned spec.

    Derived from the absolute window the operator submitted, so the API keeps
    exactly the shape it had before pins were rolling: they post a spec with
    dates, and the conversion to a moving window happens here, once, at create
    time. Requiring them to post day-counts instead would have made a pin a
    different object from a sweep, for no gain.

    ``holdout_days`` is measured from the END, like ``window_days``, because
    that is the edge both of them are re-anchored to. Measuring it from the
    start would move the holdout boundary every time the window slid.
    """
    start = datetime.strptime(spec["start"], "%Y-%m-%d").date()
    end = datetime.strptime(spec["end"], "%Y-%m-%d").date()
    window_days = (end - start).days
    holdout = spec.get("holdout_start")
    holdout_days = None
    if holdout:
        holdout_days = (end - datetime.strptime(holdout, "%Y-%m-%d").date()).days
    return window_days, holdout_days


def pin_spec_json(spec: Dict[str, Any]) -> str:
    """The canonical text a pin stores and the duplicate check compares.

    ``sort_keys=True`` over the OUTPUT of ``validate_spec``, which is the one
    canonical form in this system — so a pin submitted with its symbols in a
    different order is the same string here and is correctly refused as a
    duplicate. Comparing the raw request bodies instead would let the same
    question be pinned five times.
    """
    return json.dumps(spec, sort_keys=True)


def pin_row(*, pin_id: str, spec_json: str, active: bool,
            window_days: Optional[int] = None,
            holdout_days: Optional[int] = None,
            note: Optional[str] = None) -> Dict[str, Any]:
    """The ``scenario_pins`` row this API writes (``persist.pin_row``'s shape).

    Pinned equal to the engine's builder by a test, for the reason
    ``submitted_row`` is: a column one side omits is a column the other side's
    reader renders as blank while looking correct.

    A deactivation carries the SAME payload as the create it retires — spec,
    window shape and note — because the row is a state transition, not a
    tombstone, so a reader taking the latest row can see what was un-pinned
    without walking the history.
    """
    return {
        "pin_id": pin_id,
        "spec_json": spec_json,
        "active": bool(active),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "note": (note[:PIN_NOTE_MAX_CHARS] if note else None),
        # The ROLLING shape. A deactivation carries it too — the row is a state
        # transition, not a tombstone.
        "window_days": (None if window_days is None else int(window_days)),
        "holdout_days": (None if holdout_days is None else int(holdout_days)),
    }


def pin_identity(spec: Dict[str, Any], *, window_days: int,
                 holdout_days: Optional[int]) -> str:
    """What makes two pins THE SAME QUESTION, as comparable text.

    Two things are deliberate here, and each was a defect in an earlier build.

    **It is `identity.canonical_spec`, not byte equality on `spec_json`.**
    ``validate_spec`` deliberately does NOT sort ``symbols`` (the grid's
    columns are read in the order the operator typed their universe), so
    ``["AAPL","NVDA"]`` and ``["NVDA","AAPL"]`` are two strings and ONE sweep.
    Comparing the stored text would have accepted both pins, and the battery
    would replay one and deduplicate the other every Saturday for ever.
    ``canonical_spec`` collapses scenario order too, and drops ``force``.

    **And it is taken over the RELATIVE window**: the absolute ``start`` /
    ``end`` / ``holdout_start`` are removed and replaced by the two day-counts.
    A rolling pin's dates are re-anchored every week, so an identity that
    included them would make the same pin a different question on every
    Saturday — and would let the same question be pinned once a week for ever,
    each copy looking new.
    """
    canon = dict(canonical_spec(spec))
    for absolute in ("start", "end", "holdout_start"):
        canon.pop(absolute, None)
    canon["window_days"] = int(window_days)
    canon["holdout_days"] = (None if holdout_days is None else int(holdout_days))
    return json.dumps(canon, sort_keys=True)


def duplicate_active_pin(spec: Dict[str, Any], *, window_days: int,
                         holdout_days: Optional[int],
                         pins: Iterable[Dict[str, Any]]
                         ) -> Optional[Dict[str, Any]]:
    """The ACTIVE pin already asking this same question, or None.

    Compared on ``pin_identity`` — the relative window — not on the stored
    text. A pin whose ``spec_json`` will not decode, or which carries no
    ``window_days``, is SKIPPED rather than treated as a match: it is not
    comparable to anything, the battery refuses it every week and says so, and
    blocking a good pin behind a broken one would be the wrong direction.

    Inactive pins are ignored on purpose: re-pinning something you un-pinned
    last month is a perfectly ordinary thing to do, and refusing it would make
    un-pinning a one-way door.
    """
    identity = pin_identity(spec, window_days=window_days,
                            holdout_days=holdout_days)
    for pin in pins:
        if not pin.get("active"):
            continue
        stored_window = pin.get("window_days")
        if stored_window is None:
            continue
        try:
            stored = json.loads(pin.get("spec_json") or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(stored, dict):
            continue
        try:
            other = pin_identity(stored, window_days=stored_window,
                                 holdout_days=pin.get("holdout_days"))
        except (TypeError, ValueError):
            continue
        if other == identity:
            return pin
    return None


def active_pin_count(pins: Iterable[Dict[str, Any]]) -> int:
    return sum(1 for pin in pins if pin.get("active"))


def shape_pin(row: Dict[str, Any]) -> Dict[str, Any]:
    """One pin as the API returns it: the spec DECODED, never the raw text.

    The stored column is text; handing it back as text would make every caller
    parse it, and the one that forgot would render JSON in a table cell. A row
    whose ``spec_json`` will not decode is returned with ``spec: null`` and the
    text on ``spec_json`` rather than dropped — it is exactly the row an
    operator needs to see, because the battery refuses it every week.
    """
    raw = row.get("spec_json")
    try:
        spec = json.loads(raw) if raw else None
        if not isinstance(spec, dict):
            spec = None
    except (TypeError, ValueError):
        spec = None
    written = row.get("written_at")
    return {
        "pin_id": row.get("pin_id"),
        "active": bool(row.get("active")),
        "note": row.get("note"),
        "spec": spec,
        "spec_json": raw if spec is None else None,
        "written_at": (written.isoformat() if hasattr(written, "isoformat")
                       else written),
        # The rolling shape, surfaced because it — not the dates inside `spec`
        # — is what the battery will actually replay every Saturday. A console
        # that showed only the stored dates would describe a window the pin
        # stopped using the week after it was created.
        "window_days": row.get("window_days"),
        "holdout_days": row.get("holdout_days"),
    }


def pins_sql(dataset: str) -> str:
    """The CURRENT state of every pin: latest row per ``pin_id``.

    ``@active_only`` is a parameter rather than two queries so the battery's
    question and the console's are demonstrably the same one with a filter.
    """
    return f"""
    SELECT * EXCEPT(rn) FROM (
      SELECT *, ROW_NUMBER() OVER (
        PARTITION BY pin_id ORDER BY {PINS_LATEST_ORDER_BY}) AS rn
      FROM `{dataset}.{PINS_TABLE}`
    )
    WHERE rn = 1
      AND (@active_only = FALSE OR active = TRUE)
    ORDER BY written_at DESC
    """


def one_pin_sql(dataset: str) -> str:
    """The current state of ONE pin, or nothing. Used by the delete path."""
    return f"""
    SELECT * EXCEPT(rn) FROM (
      SELECT *, ROW_NUMBER() OVER (
        PARTITION BY pin_id ORDER BY {PINS_LATEST_ORDER_BY}) AS rn
      FROM `{dataset}.{PINS_TABLE}`
      WHERE pin_id = @pin_id
    )
    WHERE rn = 1
    """



# ============================================================================ #
# SQL (built here so it is pinned by a test that needs no credentials)
# ============================================================================ #

def latest_status_per_run_sql(dataset: str, inner_where: str = "") -> str:
    """One row per ``run_id``: its latest status, plus everything on that row.

    ``inner_where`` is pushed INSIDE the window function, not applied to its
    result. That is not an optimisation detail — a predicate on ``run_id`` or
    ``sweep_key`` applied outside would still make BigQuery rank every run in the
    table on every call, and both columns are clustering keys precisely so it
    does not have to. It carries no user text: every caller passes a fixed
    fragment and binds its value as a query parameter.
    """
    where = f"WHERE {inner_where}" if inner_where else ""
    return f"""
    SELECT * EXCEPT(rn) FROM (
      SELECT *, ROW_NUMBER() OVER (
        PARTITION BY run_id ORDER BY {LATEST_STATUS_ORDER_BY}) AS rn
      FROM `{dataset}.{SWEEPS_TABLE}`
      {where}
    )
    WHERE rn = 1
    """


def recent_sweeps_sql(dataset: str) -> str:
    return latest_status_per_run_sql(dataset) + (
        " ORDER BY submitted_at DESC LIMIT @limit"
    )


def one_sweep_sql(dataset: str) -> str:
    return latest_status_per_run_sql(dataset, "run_id = @run_id")


def done_by_key_sql(dataset: str) -> str:
    """The most recent COMPLETED run under a ``sweep_key``.

    **The JOB uses this to dedup. The API uses it only as a HINT** (round-2 fix
    1). The API cannot compute the Job's effective configuration — it has no
    ``Config`` — and the round-1 attempt to approximate it (bind
    ``@base_config_hash`` to whatever a prior run had recorded on the same
    commit) was self-referential: after an operator flipped ``ROLLER_ENABLED`` on
    the Job, a re-submitted spec matched the *pre-flip* run's own hash and
    deduplicated to it for ever, and the Job's exact check — the one that would
    have caught the flip — never ran, because nothing was ever launched. So the
    API always launches, and the Job decides.

    "Completed" is four conditions, not one status string, and each one exists
    because ``status = 'done'`` alone would return something that is not an
    answer:

    * ``error_cells = 0`` — a run every arm of which errored is a page of `err`,
      not a result, and serving it means the operator never learns to re-run;
    * ``rows_persisted = cell_count`` — a run whose ``scenario_runs`` insert did
      not land has an EMPTY grid; the cached answer would be nothing at all;
    * ``base_config_hash`` equality — ``sweep_key`` covers the spec, the engine
      version and the engine identity, and cannot see an operator flipping
      ``EARNINGS_ENABLED`` on the Job between two otherwise identical
      submissions. That value is not in the yaml the key hashes. **Only the Job
      binds this parameter**; the API passes NULL, which is why its answer is a
      hint and not a decision;
    * ``engine_identity`` equality (FC-096 Phase B) — redundant against the key
      for every row written since the re-key, and NOT redundant for the rows
      written before it, whose column is NULL and whose ``sweep_key`` was
      computed over a commit SHA. ``NULL = @engine_identity`` is never true, so a
      pre-migration row can never be served as a hit. Both callers bind it, and
      a caller with no identity does not call this at all.

    A duplicate replay costs eight minutes. A wrong dedup hit serves one
    experiment's numbers as another's, silently — so the side that cannot check
    exactly does not get to decide.
    """
    return latest_status_per_run_sql(dataset, "sweep_key = @sweep_key") + (
        f" AND status = '{STATUS_DONE}'"
        " AND error_cells = 0"
        " AND rows_persisted IS NOT NULL"
        " AND rows_persisted = cell_count"
        " AND (@base_config_hash IS NULL OR base_config_hash = @base_config_hash)"
        " AND engine_identity = @engine_identity"
        " ORDER BY submitted_at DESC LIMIT 1"
    )


def sweep_rows_sql(dataset: str) -> str:
    return f"""
    SELECT * FROM `{dataset}.{RUNS_TABLE}`
    WHERE run_id = @run_id
    ORDER BY split, scenario_name, symbol
    """


# ============================================================================ #
# Shaping (D11) — the UI renders, it does not recompute.
# ============================================================================ #

def _median(values: Sequence[float]) -> Optional[float]:
    """``statistics.median`` semantics, restated so this module stays tiny.

    The even-length case averages the two middle values, exactly as
    ``statistics.median`` does — the equality test against ``report.py`` fails on
    any other convention.
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _cell_state(row: Dict[str, Any]) -> str:
    """``error`` | ``insufficient`` | ``low_activity`` | ``measured``.

    Read off the STORED flags, never re-derived. The four states partition every
    cell (``runner.ScenarioResult``), ``insufficient`` wins over
    ``low_activity``, and a reader that recomputed the precedence would
    eventually double-count a cell — which is how a summary row stops adding up
    and the reader stops trusting the table.
    """
    if row.get("error"):
        return "error"
    if row.get("insufficient"):
        return "insufficient"
    if row.get("low_activity"):
        return "low_activity"
    if row.get("measured"):
        return "measured"
    # Neither measured nor flagged: a row written before a flag existed, or a
    # verdict that never resolved. Named rather than quietly counted as measured.
    return "unknown"


def _index(rows: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    return {
        (str(r.get("scenario_name")), str(r.get("symbol")), str(r.get("split"))): r
        for r in rows
    }


def _measured_value(row: Optional[Dict[str, Any]]) -> Optional[float]:
    """The cell's annualized return IF it is rankable, else None.

    The single choke point through which every aggregate reads a number. An
    `insuf` or `low-act` cell has an ``annualized_return`` in the row — it just
    must never contribute to a median, because "nothing happened" and "the wheel
    sat idle 95% of the time" are not small returns.
    """
    if row is None or _cell_state(row) != "measured":
        return None
    value = row.get("annualized_return")
    return None if value is None else float(value)


def _common_delta(index, scenarios_symbols, scenario: str, split: str
                  ) -> Tuple[Optional[float], int]:
    """``(median per-symbol delta vs base, n)`` over symbols measured in BOTH arms.

    Per-symbol deltas first, median of THOSE — not the difference of two medians.
    They coincide only when both arms measure the same symbols; when they do not,
    the difference-of-medians compares an arm's four symbols against base's six
    and calls the gap a result. It flatters whichever arm traded less, which is
    the wrong direction for a sweep to be wrong in.
    """
    deltas = []
    for symbol in scenarios_symbols:
        arm = _measured_value(index.get((scenario, symbol, split)))
        base = _measured_value(index.get((BASE_SCENARIO_NAME, symbol, split)))
        if arm is None or base is None:
            continue
        deltas.append(arm - base)
    if not deltas:
        return None, 0
    return _median(deltas), len(deltas)


def _sign_agreement(index, symbols, scenario: str) -> Tuple[int, int]:
    """``(agreeing, comparable)`` for one arm's fit/holdout pair.

    "Agrees" is about the delta VERSUS BASE, not the raw return: an arm whose raw
    return is positive in both windows has shown nothing about itself, only that
    the market went up. A symbol counts only when all four cells are measured.
    """
    agreeing = comparable = 0
    for symbol in symbols:
        cells = [
            _measured_value(index.get((scenario, symbol, "fit"))),
            _measured_value(index.get((scenario, symbol, "holdout"))),
            _measured_value(index.get((BASE_SCENARIO_NAME, symbol, "fit"))),
            _measured_value(index.get((BASE_SCENARIO_NAME, symbol, "holdout"))),
        ]
        if any(c is None for c in cells):
            continue
        fit_delta = cells[0] - cells[2]
        hold_delta = cells[1] - cells[3]
        comparable += 1
        if (fit_delta > 0) == (hold_delta > 0) and (fit_delta < 0) == (hold_delta < 0):
            agreeing += 1
    return agreeing, comparable


def _ordering(sweep_row: Dict[str, Any], run_rows: Sequence[Dict[str, Any]]):
    """Scenario, symbol and split order — from the SPEC when it is available.

    Declaration order is information: an operator reads their arms in the order
    they wrote them. Alphabetical order would silently reshuffle the grid.
    ``base`` is always first, because every other row is read relative to it.
    """
    spec: Dict[str, Any] = {}
    raw = sweep_row.get("spec_json")
    if raw:
        try:
            spec = json.loads(raw)
        except (TypeError, ValueError):
            spec = {}

    declared = [str(s.get("name")) for s in (spec.get("scenarios") or [])]
    seen_scen = [str(r.get("scenario_name")) for r in run_rows]
    scenarios = [BASE_SCENARIO_NAME]
    for name in declared + seen_scen:
        if name and name not in scenarios:
            scenarios.append(name)
    if not run_rows and len(scenarios) == 1 and not declared:
        scenarios = []

    symbols = [str(s) for s in (spec.get("symbols") or [])]
    for row in run_rows:
        sym = str(row.get("symbol"))
        if sym and sym not in symbols:
            symbols.append(sym)

    order = {"all": 0, "fit": 1, "holdout": 2}
    splits = sorted({str(r.get("split")) for r in run_rows},
                    key=lambda s: (order.get(s, 99), s))
    return scenarios, symbols, splits, spec


def spec_max_dte(spec: Dict[str, Any]) -> int:
    """The DTE reach a persisted spec's arms imply — the dashboard's half of
    ``runner.effective_max_dte``.

    The runner takes the max over the base config AND every arm's DTE overrides.
    This side cannot see the base config — it holds a spec, not a ``Config`` —
    so the floor is ``DTE_REACH_BIAS_THRESHOLD``, which is the live profile's
    own ``put_target_dte``. The two therefore agree on the only thing the answer
    is used for: whether the run reached PAST 7. They would disagree if the
    wheel profile's base DTE ever moved off 7, which is a config change that has
    to update the threshold anyway (the fidelity figures in ``SWEEP_BIASES``
    were measured at 7 and the constant says so).

    Reads the SPEC rather than the per-cell ``overrides_json`` because the spec
    is the run's declaration: an arm that errored in every cell still asked for
    its reach, and the caveat is about the DATA the window was built on, not
    about which cells came back.
    """
    reach = DTE_REACH_BIAS_THRESHOLD
    for arm in (spec.get("scenarios") or []):
        if not isinstance(arm, dict):
            continue
        overrides = arm.get("overrides") or {}
        if not isinstance(overrides, dict):
            continue
        for key in DTE_OVERRIDE_KEYS:
            value = overrides.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                reach = max(reach, value)
    return reach


def shape_results(sweep_row: Dict[str, Any],
                  run_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Everything ``/sims`` renders, derived once here (D11).

    The UI must not recompute a median, a delta or a sign agreement: three
    reimplementations of "which cells count" is three chances to average an
    `insuf` cell into a ranking. The grid is always present — there is no shape
    in which a caller gets aggregates without the per-symbol cells, because a
    single blended number hides "one symbol carried the arm" and "better
    everywhere by a hair" equally, and those are opposite findings.
    """
    scenarios, symbols, splits, spec = _ordering(sweep_row, run_rows)
    index = _index(run_rows)

    # PR-B contract: the arm hashes and the per-split window bounds, read off the
    # PERSISTED rows rather than recomputed. The UI renders a "scenario
    # definitions" table with both hashes (the CLI report has one) and labels
    # each grid with its window; deriving either client-side would be a third
    # implementation of something already stored.
    scenario_hashes: Dict[str, Optional[str]] = {}
    scenario_config_hashes: Dict[str, Optional[str]] = {}
    windows: Dict[str, Dict[str, Any]] = {}
    for row in run_rows:
        name = str(row.get("scenario_name"))
        scenario_hashes.setdefault(name, row.get("scenario_hash"))
        scenario_config_hashes.setdefault(name, row.get("config_hash"))
        split = str(row.get("split"))
        if split not in windows:
            windows[split] = {
                "start": _as_date_str(row.get("window_start")),
                "end": _as_date_str(row.get("window_end")),
            }

    grid: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for split in splits:
        by_scenario: Dict[str, Dict[str, Any]] = {}
        for scenario in scenarios:
            cells: Dict[str, Any] = {}
            for symbol in symbols:
                row = index.get((scenario, symbol, split))
                if row is None:
                    cells[symbol] = None
                    continue
                cells[symbol] = {
                    "state": _cell_state(row),
                    "verdict": row.get("verdict"),
                    "demote": row.get("demote"),
                    "annualized_return": row.get("annualized_return"),
                    "total_return": row.get("total_return"),
                    "days_in_position_fraction": row.get("days_in_position_fraction"),
                    "cycles_completed": row.get("cycles_completed"),
                    "puts_sold": row.get("puts_sold"),
                    "calls_sold": row.get("calls_sold"),
                    "bid_fill_return": row.get("bid_fill_return"),
                    "verdict_flips_on_fill": row.get("verdict_flips_on_fill"),
                    "error": row.get("error"),
                }
            by_scenario[scenario] = cells
        grid[split] = by_scenario

    summary: List[Dict[str, Any]] = []
    delta_vs_base: Dict[str, Dict[str, Any]] = {}
    for split in splits:
        delta_vs_base[split] = {}
        for scenario in scenarios:
            rows = [index[(scenario, s, split)] for s in symbols
                    if (scenario, s, split) in index]
            values = [v for v in (_measured_value(r) for r in rows) if v is not None]
            states = [_cell_state(r) for r in rows]
            median_delta, n = _common_delta(index, symbols, scenario, split)
            delta_vs_base[split][scenario] = {"median": median_delta, "symbols": n}
            summary.append({
                "scenario": scenario,
                "split": split,
                "median": _median(values),
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "measured": states.count("measured"),
                "insufficient": states.count("insufficient"),
                "low_activity": states.count("low_activity"),
                "errors": states.count("error"),
                "demote_flags": sum(1 for r in rows if r.get("demote")),
                # `—` for base itself: an arm is not a delta against itself.
                "delta_vs_base": None if scenario == BASE_SCENARIO_NAME else median_delta,
                "delta_symbols": 0 if scenario == BASE_SCENARIO_NAME else n,
            })

    has_holdout = "holdout" in splits
    sign_agreement = None
    if has_holdout:
        sign_agreement = {}
        for scenario in scenarios:
            agreeing, comparable = _sign_agreement(index, symbols, scenario)
            sign_agreement[scenario] = {
                "agreeing": agreeing, "comparable": comparable,
            }

    in_sample_only = sweep_row.get("in_sample_only")
    if in_sample_only is None:
        in_sample_only = not has_holdout

    # FC-096 Phase A PR-2. Derived per run, appended to THIS run's footer only:
    # a 7-reach sweep has not earned the caveat, and a footer that warns about
    # something the run did not do is one people stop reading.
    effective_max_dte = spec_max_dte(spec)
    known_biases = list(SWEEP_BIASES)
    if effective_max_dte > DTE_REACH_BIAS_THRESHOLD:
        known_biases.append(DTE_REACH_BIAS)

    # FC-096 A4. Read off the PERSISTED row, never re-derived: this is a
    # property of the earnings table as it stood when the run replayed, and a
    # symbol onboarded today and refreshed tomorrow has the same spec and a
    # different answer. `[]` for a run that found no gaps AND for one written
    # before the column existed — the two are indistinguishable downstream, so
    # the page must not present an empty list as "checked, all clear".
    earnings_gaps = _json_list(sweep_row.get("earnings_symbols_without_data"))

    return {
        "run": dict(sweep_row),
        "spec": spec,
        "scenarios": scenarios,
        "symbols": symbols,
        "splits": splits,
        "grid": grid,
        # Empty for a run with no cells yet — a `submitted` or `running` sweep
        # has a status row and nothing else, and the page has to render that
        # rather than 500. `splits: []` goes with it.
        "windows": windows,
        "scenario_hashes": scenario_hashes,
        "scenario_config_hashes": scenario_config_hashes,
        "summary": summary,
        "delta_vs_base": delta_vs_base,
        "sign_agreement": sign_agreement,
        "in_sample_only": bool(in_sample_only),
        "min_days_in_position": MIN_DAYS_IN_POSITION,
        # The footer, verbatim from the CLI report. Served rather than restated
        # in the frontend so the two readers are warned in the same words.
        "known_biases": [{"title": t, "detail": d} for t, d in known_biases],
        # Served so a reader can check the footer against its own input — the
        # CLI report carries the same field.
        "effective_max_dte": effective_max_dte,
        # FC-096 A4. Symbols the FC-013 gate could not gate, because they are
        # absent from the committed earnings table. Rendering it on `/sims` is
        # Phase E; serving it now means the number exists to render.
        "earnings_symbols_without_data": earnings_gaps,
        "cross_scenario_caveat": CROSS_SCENARIO_CAVEAT,
        "rejection_tally_caveat": TALLY_CAVEAT,
        "in_sample_banner": IN_SAMPLE_BANNER if in_sample_only else None,
        "holdout_semantics": HOLDOUT_SEMANTICS if has_holdout else None,
    }


# ============================================================================ #
# The allowlist endpoint (D10's client-side validation source)
# ============================================================================ #

# Well-formed arms an operator can start from. Every one is inside the allowlist
# by construction — a preset that produced a 422 would be worse than no preset,
# because the reader would conclude the allowlist is broken rather than that the
# preset is.
PRESETS: List[Dict[str, Any]] = [
    {"name": "tighter_calls", "label": "Tighter call delta band",
     "overrides": {"strategy.call_delta_range": [0.10, 0.20]}},
    {"name": "wider_calls", "label": "Wider call delta band",
     "overrides": {"strategy.call_delta_range": [0.20, 0.35]}},
    {"name": "tighter_puts", "label": "Tighter put delta band",
     "overrides": {"strategy.put_delta_range": [0.08, 0.15]}},
    {"name": "put_floor_025", "label": "Put premium floor $0.25",
     "overrides": {"strategy.min_put_premium": 0.25}},
    {"name": "call_floor_015", "label": "Call premium floor $0.15",
     "overrides": {"strategy.min_call_premium": 0.15}},
    {"name": "ceiling_1000", "label": "Price ceiling $1000 (FC-055)",
     "overrides": {"strategy.max_stock_price": 1000}},
    {"name": "position_20pct", "label": "Position size 20%",
     "overrides": {"risk.max_position_size": 0.20}},
    {"name": "earnings_off", "label": "Earnings gate off",
     "overrides": {"earnings.enabled": False}},
    {"name": "roller_off", "label": "Roller off",
     "overrides": {"rolling.enabled": False}},
    {"name": "at_the_bid", "label": "Fill at the bid (worst case)",
     "overrides": {}, "fill_haircut": 1.0},
]


def allowlist_payload() -> Dict[str, Any]:
    """What ``GET /api/v2/sweeps/allowlist`` serves.

    Rejections are served alongside the allowed keys, with their reasons. A form
    that only listed what is allowed teaches nothing; the reasons are the whole
    value — "universe.min_open_interest is not in the allowlist" tells an
    operator nothing, "the engine hardcodes open_interest to 0, so any floor
    rejects every call" tells them what to do next.
    """
    return {
        "allowed": [{"key": key, "description": why}
                    for key, why in sorted(ALLOWED_OVERRIDES.items())],
        "rejected": [{"key": key, "reason": reason}
                     for key, reason in sorted(REJECTED_OVERRIDES.items())],
        "described": describe_allowlist(),
        "presets": PRESETS,
        "caps": {
            "max_symbols": MAX_SYMBOLS,
            "max_scenarios": MAX_SCENARIOS,
            "max_cells": MAX_CELLS,
            "max_window_days": MAX_WINDOW_DAYS,
            "min_holdout_days": MIN_HOLDOUT_DAYS,
            "min_starting_cash": MIN_STARTING_CASH,
            "max_starting_cash": MAX_STARTING_CASH,
            "max_spec_bytes": MAX_SPEC_BYTES,
            "max_scenario_name_chars": MAX_SCENARIO_NAME_CHARS,
            "scenario_name_pattern": SCENARIO_NAME_RE.pattern,
            "symbol_pattern": SYMBOL_RE.pattern,
            "job_task_timeout_seconds": JOB_TASK_TIMEOUT_SECONDS,
            "stale_grace_minutes": STALE_GRACE_MINUTES,
        },
        # What a legal setting of each knob looks like, so the form can refuse a
        # bad value before a round trip.
        "value_types": OVERRIDE_VALUE_TYPES,
        "defaults": {
            "starting_cash": DEFAULT_STARTING_CASH,
            "run_sensitivity": False,
        },
        "base_scenario_name": BASE_SCENARIO_NAME,
        "engine_version": ENGINE_VERSION,
        # None on an image built without the build-arg; the UI can then say why
        # `prior_done_run_id` never appears instead of looking broken.
        "engine_identity": engine_identity_from_env(),
        "min_days_in_position": MIN_DAYS_IN_POSITION,
    }
