#!/usr/bin/env python3
"""FC-112 — the PRE-REGISTERED read of the wheel's roll trigger (0.98 vs 1.00).

Plan: ``docs/plans/fc-112.md`` (DD-1 the rule, DD-6 the selection).

WHAT THIS IS
------------
A read-only diagnostic. It selects the scenario cells the study is entitled to
read, computes the three paired contrasts, applies a decision rule that was
written down **before** any ``t100`` cell was replayed, and prints a verdict.

The rule is one-sided. ``1.00`` is the default (the operator's "same underlying
mechanisms" principle, FC-100 D-A); ``0.98`` keeps only by passing the KEEP rule
in full. Every other outcome except a measured conflict resolves to ``1.00``.

THE CONSTANTS ARE FROZEN AT THIS COMMIT
---------------------------------------
``TIE_BREAK``, ``MIN_EFFECT_PP`` and ``MIN_SIGN_COUNT`` are module constants.
**No command-line argument can change them** — there is deliberately no
``--tie-break``, no ``--min-effect`` and no ``--min-sign-count``, and argparse
rejects them. A different rule is a different commit with a different SHA, and
the verdict block prints the commit AND the blob hash of this file, so a record
that quotes a verdict also names the exact bytes that produced it.

**The tool REFUSES to run when it cannot make that claim**: an uncommitted edit
to this file, a missing ``git``, or a checkout it cannot resolve all exit
non-zero with no verdict printed. A verdict whose rule cannot be named is worse
than no verdict.

This is the whole point of the pre-registration: at M = 7 symbols the sign
count alone is a 6.25 % test on independent symbols and a 25 % test if the
symbols share half their variance (one market, one year — they do). A rule
chosen after the numbers are seen decides nothing.

WHAT IT DOES NOT DO
-------------------
It never writes. It creates no pins, submits no sweeps, POSTs nothing, and
touches no production config. BigQuery is read through ``google.cloud.bigquery``
and GCS artifacts (``--artifacts``) through ``google.cloud.storage``, both
read-only. It exits non-zero on VOID.

USAGE
-----
    # the decision read, from the two pins, at the latest window they share
    python tools/diagnostics/fc112_roll_trigger_read.py \
        --pin-id <decision> --pin-id <control> [--window-end YYYY-MM-DD] \
        [--oos-run-id <id>] [--artifacts]

    # the one-shot early read
    python tools/diagnostics/fc112_roll_trigger_read.py --run-id <id> [--run-id <id>]

    # the four-Saturday fragility monitor (DD-4: a fragility check, NOT
    # out-of-sample — consecutive trailing-year windows overlap 51/52 weeks)
    python tools/diagnostics/fc112_roll_trigger_read.py \
        --pin-id <decision> --pin-id <control> --last 4
"""

from __future__ import annotations

import argparse
import gzip
import json
import math

import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median, stdev
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# The pre-registered constants (DD-1; operator decision D-1, signed 2026-09-12).
#
# Frozen at the PR-2 merge, before any `t100` cell is replayed. NOT arguments.
# --------------------------------------------------------------------------- #

#: The default outcome when the KEEP rule does not fire: move to 1.00.
TIE_BREAK = "move"

#: Percentage points of annualised return. 0.5 pp on $100,000 = $500/yr, about
#: two rolls' credit on these rows — the smallest effect worth a different
#: mechanism from the covered-call profile.
MIN_EFFECT_PP = 0.5

#: N- (symbols where the t100 arm is WORSE) required, by M = the number of
#: symbols measured in BOTH arms of a read. M < 6 -> VOID(insufficient_measured).
MIN_SIGN_COUNT = {7: 6, 6: 5}

#: The window shape the study's spec bodies fix (DD-4 / §The experiment):
#: 365-day window, 90-day holdout, so the FIT split is 275 days. Used only to
#: annualise the option-leg location check (DD-1 condition 2), whose inputs are
#: dollar P&L over the fit split.
FIT_WINDOW_DAYS = 275
ANNUALISATION_DAYS = 365

#: The spec bodies' `starting_cash`. The denominator of the option-leg contrast.
STARTING_CASH = 100_000.0

#: Holdout sign agreement below this share REFUTES, and only when the holdout
#: has at least MIN_INFORMATIVE_COMPARABLE comparable symbols (DD-1 cond. 3).
HOLDOUT_AGREEMENT_FLOOR = 0.50
MIN_INFORMATIVE_COMPARABLE = 4

#: The seven wheel symbols that MEASURE in the fit window (read 2026-09-11;
#: DD-5, operator decision D-5). Frozen here beside the rest of the rule
#: because "which symbols does M count" is a rule parameter: a read that
#: quietly gained an eighth symbol would move MIN_SIGN_COUNT's key under the
#: reader. A deciding contrast is restricted to these; any other symbol in a
#: read is REPORTED and does not decide.
STUDY_SYMBOLS = frozenset({"AAPL", "AMD", "AMZN", "GOOGL", "IWM", "NVDA",
                           "UNH"})

#: The OOS read's own M floor. Below it the out-of-sample read is
#: `uninformative` and cannot refute a KEEP — the same logic as the holdout's
#: MIN_INFORMATIVE_COMPARABLE, and for the same reason: at M < 4 a median is
#: two symbols' noise. (Rule clarification R-c, 2026-09-12.)
OOS_MIN_M = 4


# --------------------------------------------------------------------------- #
# The arms, and what each one declares about itself.
# --------------------------------------------------------------------------- #

BASE_ARM = "base"

#: arm -> the `roll_fill_mode` that arm MUST have resolved to. A row whose
#: stored mode disagrees with its arm's declaration is a structural VOID: the
#: `haircut` read is the other end of DD-3's spread bracket, and a haircut arm
#: that actually filled at the limit is not that end.
ARM_FILL_MODE = {
    "base": "limit",
    "t100": "limit",
    "base_haircut": "haircut",
    "t100_haircut": "haircut",
    "noroll": "limit",
    "base_noimm": "limit",
    "t100_noimm": "limit",
    "t099": "limit",
}

#: The three paired contrasts of DD-1, in the order the record prints them.
#: `limit` is the PRIMARY read: the monitor's sign-flip check is on this one.
READS: Tuple[Tuple[str, str, str], ...] = (
    ("limit", "base", "t100"),
    ("haircut", "base_haircut", "t100_haircut"),
    ("noimm", "base_noimm", "t100_noimm"),
)
PRIMARY_READ = "limit"

#: Arms that run at `itm_trigger_ratio = 1.00`. `otm_roll_outs` is empty on
#: these BY CONSTRUCTION (`close/old_strike >= 1.0 -> itm_defence`), so a
#: non-zero count means the arm did not carry the override it claims.
T100_ARMS = frozenset({"t100", "t100_haircut", "t100_noimm"})

#: The roll-skip reasons the record must name (DD-1's reported set). Others are
#: printed too - this is the set that must never be silently absent.
REPORTED_SKIP_REASONS = (
    "no_credit_candidate",
    "no_suitable_replacement",
    "btc_quote_unavailable",
    "not_itm_enough",
)

#: The materialised ladder's edge (`overrides.MAX_SWEEPABLE_DTE`). A roll whose
#: replacement sits AT it is a search that hit the edge, not one that chose -
#: DD-2's cheap tell for the residual truncation on chained rolls. Reported per
#: arm; never a check.
REPLACEMENT_DTE_CUTOFF = 21

#: Where the battery writes artifacts (`artifact_store.DEFAULT_ARTIFACT_BUCKET`)
#: - the data bucket, NOT the chain lake (plan "Found while planning" note 9).
DEFAULT_ARTIFACT_BUCKET = "gen-lang-client-0607444019-options-data"
ARTIFACT_PREFIX = "sim-artifacts/v1"

DEFAULT_PROJECT = "gen-lang-client-0607444019"
DEFAULT_DATASET = "options_wheel"

#: DD-1's null-base-rate table, computed 2026-09-12 (200k draws per cell;
#: `Delta_s` i.i.d. N(0, sigma^2), or with a common factor at rho = 0.5). The
#: record must state the base rate of the read it actually made, so the tool
#: prints the row matching the empirical sigma it measured rather than leaving
#: the reader to look it up. (sigma_pp, iid_sign, iid_sign_and_median,
#: rho50_sign, rho50_sign_and_median)
NULL_RATE_TABLE: Tuple[Tuple[float, float, float, float, float], ...] = (
    (0.5, 6.2, 0.6, 25.0, 9.4),
    (1.0, 6.3, 3.8, 25.1, 20.5),
    (2.0, 6.3, 5.7, 25.0, 24.2),
    (4.0, 6.3, 6.2, 25.0, 24.9),
)

#: The exact binomial for the sign count alone, by M.
SIGN_ONLY_BINOMIAL = {7: 6.25, 6: 10.9}


# --------------------------------------------------------------------------- #
# SQL (DD-6). Read-only.
# --------------------------------------------------------------------------- #

#: A VERBATIM copy of `persist.LATEST_STATUS_ORDER_BY`. Copied rather than
#: imported for the reason the dashboard copies it: importing
#: `src.backtesting.scenarios.persist` pulls the whole engine into a read-only
#: diagnostic. `tests/test_fc112_roll_trigger_read.py` pins the two byte-equal,
#: so a future STATUS_RANK change fails in CI instead of silently giving this
#: tool a different "latest row" from the one the battery wrote.
LATEST_STATUS_ORDER_BY = (
    "written_at DESC, "
    "CASE status "
    "WHEN 'deduplicated' THEN 2 "
    "WHEN 'done' THEN 4 "
    "WHEN 'failed' THEN 3 "
    "WHEN 'running' THEN 1 "
    "WHEN 'submitted' THEN 0 "
    "ELSE -1 END DESC"
)

#: DD-1's non-deciding contrast metrics. Appended to the plan's SELECT list;
#: see the note in `_cell_sql`.
SECONDARY_METRIC_COLUMNS = (
    "assignment_rate",
    "cycles_completed",
    "calls_sold",
    "max_drawdown",
)


def _ctes(dataset_ref: str) -> str:
    """DD-6's `latest` + `resolved` CTEs, verbatim from the plan.

    Three things in here are load-bearing and each of them was a bug found
    while the plan was written:

    * **Latest status row per `run_id`.** `scenario_sweeps` is insert-only, one
      row per status transition, and every row of one submission shares
      `submitted_at` (the partition key) — so ordering by it is a three-way tie.
      `written_at` is the clock; `LATEST_STATUS_ORDER_BY` breaks a
      same-microsecond tie, and `done` outranks `failed` deliberately.
    * **Follow `deduplicated_to`.** A `deduplicated` sweep writes NO
      `scenario_runs` rows of its own; it names the run that already measured
      that key. Joining on `run_id` alone drops that point silently. The one
      realistic collision here is an early one-shot submitted at the coming
      Saturday's window, which the pin's first point then dedups INTO (DD-4).
      The **target must itself be `done`** — but that VOID is raised by
      `resolve_dedup`, not by this query: `s.status = 'done'` below would just
      drop the rows, and a read that silently loses a pin is exactly what the
      study cannot afford.
    * **The SOURCE run id is projected beside the resolved one.** `run_id`
      here is the run that HOLDS the cells; `source_run_id` is the sweep the
      caller named. `--run-id` selects on the SOURCE, because selecting on
      the resolved id returns a dedup pair TWICE — once through the
      `deduplicated` row that points at the target and once through the
      target's own row — and the second copy is a silent duplicate of every
      cell (D-4).
    * **The window shape travels with the row.** `window_start` /
      `holdout_start` / the spec's `starting_cash` are what the tool's
      `FIT_WINDOW_DAYS` and `STARTING_CASH` constants CLAIM; a row that
      disagrees annualises the option leg against the wrong denominator, so
      the claim is checked rather than assumed.
    * **Strategy via `IFNULL`, never a bare equality.** `strategy` lives in
      `spec_json` and an ABSENT key means wheel (`identity.canonical_spec`
      omits the wheel case).
    """
    return f"""WITH latest AS (
  SELECT * EXCEPT(rn) FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY
      {LATEST_STATUS_ORDER_BY}) AS rn
    FROM `{dataset_ref}.scenario_sweeps` WHERE submitted_at >= @since) WHERE rn = 1),
resolved AS (  -- follow deduplicated_to once; the target must be done
  SELECT l.pin_id, l.window_end, l.submitted_via,
         l.run_id AS source_run_id, l.window_start, l.holdout_start,
         SAFE_CAST(JSON_VALUE(l.spec_json, '$.starting_cash') AS FLOAT64) AS spec_starting_cash,
         IFNULL(t.run_id, l.run_id) AS run_id, IFNULL(t.status, l.status) AS status,
         IFNULL(t.engine_version, l.engine_version) AS engine_version,
         IFNULL(t.engine_identity, l.engine_identity) AS engine_identity
  FROM latest l LEFT JOIN latest t ON t.run_id = l.deduplicated_to AND l.status = 'deduplicated'
  WHERE IFNULL(JSON_VALUE(l.spec_json, '$.strategy'), 'wheel') = 'wheel')"""


def _cell_sql(dataset_ref: str, selector: str) -> str:
    """DD-6's cell query. `selector` is the tail predicate on the SWEEP row.

    **The window filter belongs on the SWEEP row, not the runs row.**
    `scenario_runs.window_end` is PER SPLIT: a fit cell carries
    `holdout_start - 1` (the 09-11 battery's fit rows read `2026-06-12`), so
    filtering runs rows on the Saturday date returns the holdout cells only and
    silently drops every cell the decision is made on. Found while verifying
    DD-6; pinned by T-12e.

    The SELECT list is the plan's, plus two additions the plan's own text
    requires and its skeleton omitted: `r.measured` (DD-6's first sentence —
    "return contrasts use `measured` cells only"; the flags are computed by the
    engine and stored, and must never be re-derived) and DD-1's four
    non-deciding contrast metrics. The CTEs, the join and the filters — the
    parts a selection bug hides in — are unchanged.

    Two more columns were added under review. `r.error` — an errored cell is
    never `measured`, so it already sits outside every contrast, but a read
    that quietly loses a symbol to an error reports a SMALLER `M` and the
    sign count it is judged against changes with it; the tool VOIDs instead
    (D-5). And `run_id` joins the `ORDER BY`: with two pins in one read,
    both carrying the implicit `base` arm, `(symbol, split, scenario_name)`
    is not unique and the row BigQuery happens to return last would otherwise
    decide which of the two the index keeps (Q-1).
    """
    secondary = ", ".join(f"r.{c}" for c in SECONDARY_METRIC_COLUMNS)
    return f"""{_ctes(dataset_ref)}
SELECT s.pin_id, s.window_end, s.engine_version, s.engine_identity,
       s.submitted_via, s.run_id,
       s.window_start, s.holdout_start, s.spec_starting_cash, s.source_run_id,
       r.symbol, r.split, r.scenario_name, r.verdict, r.measured, r.error,
       r.annualized_return, r.total_return,
       r.option_pnl, r.stock_pnl_realized, r.stock_pnl_unrealized,
       r.rolls_executed, r.itm_rolls, r.otm_roll_outs, r.roll_net_credit, r.itm_roll_credit,
       r.otm_roll_out_credit, r.failed_roll_btc_debit, r.roll_legs_resting, r.roll_legs_marketable,
       r.roll_skips, r.roll_fill_mode, {secondary}
FROM resolved s JOIN `{dataset_ref}.scenario_runs` r USING (run_id)
WHERE s.status = 'done' AND {selector}
ORDER BY r.symbol, r.split, r.scenario_name, run_id"""


def _sweep_sql(dataset_ref: str, selector: str) -> str:
    """Sweep-level rows for `resolve_dedup`.

    Separate from `_cell_sql` because the cell query's `s.status = 'done'`
    DROPS a sweep whose dedup target never finished, and "this pin contributed
    nothing" must be a VOID rather than a smaller M. This projection keeps the
    SOURCE row beside the resolved one so the tool can say which pin pointed
    where.
    """
    return f"""{_ctes(dataset_ref)}
SELECT l.run_id AS source_run_id, l.status AS source_status, l.deduplicated_to,
       l.pin_id, l.window_end, l.submitted_via,
       IFNULL(t.run_id, l.run_id) AS resolved_run_id,
       IFNULL(t.status, l.status) AS resolved_status,
       IFNULL(t.engine_version, l.engine_version) AS engine_version,
       IFNULL(t.engine_identity, l.engine_identity) AS engine_identity
FROM latest l LEFT JOIN latest t ON t.run_id = l.deduplicated_to AND l.status = 'deduplicated'
WHERE IFNULL(JSON_VALUE(l.spec_json, '$.strategy'), 'wheel') = 'wheel' AND {selector}
ORDER BY l.window_end DESC, l.run_id"""


#: Tail predicates on the sweep row. `{a}` is the alias the caller binds:
#: `s` (the resolved CTE) in the cell query, `l` (the latest CTE) in the sweep
#: query. Pins and standing rows are separated on `pin_id` because the 09-11
#: battery holds GOOGL TWICE — the standing item and the GOOGL proof pin — and
#: any per-window aggregate that forgets that double-counts GOOGL (plan
#: "Found while planning" note 1; T-12d).
SELECT_PINS = "{a}.pin_id IN UNNEST(@pin_ids)"
SELECT_STANDING = "{a}.pin_id IS NULL AND {a}.submitted_via = 'battery'"
#: `{rid}` is the SOURCE run id under either alias — `s.source_run_id` in the
#: cell query, `l.run_id` in the sweep query (D-4). Selecting the cell query on
#: the RESOLVED id returns a dedup pair twice.
SELECT_RUN_IDS = "{rid} IN UNNEST(@run_ids)"
#: Appended to any of the three when a single window is asked for. It is on the
#: SWEEP row and never on the runs row — `scenario_runs.window_end` is PER
#: SPLIT (a fit cell carries `holdout_start - 1`), so the same predicate
#: against `r` returns the holdout cells only. Not appended under `--last N`:
#: the monitor needs every window in one query.
WINDOW_CLAUSE = " AND {a}.window_end = @window_end"


def bind(selector: str, alias: str, run_id_column: str) -> str:
    """Bind a tail predicate to the alias it runs under."""
    return selector.format(a=alias, rid=run_id_column)


# --------------------------------------------------------------------------- #
# The row shapes.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Cell:
    """One `scenario_runs` row: scenario x symbol x split."""

    run_id: str = ""
    source_run_id: Optional[str] = None
    pin_id: Optional[str] = None
    window_end: Optional[str] = None
    window_start: Optional[str] = None
    holdout_start: Optional[str] = None
    spec_starting_cash: Optional[float] = None
    engine_version: Optional[str] = None
    engine_identity: Optional[str] = None
    submitted_via: Optional[str] = None
    symbol: str = ""
    split: str = ""
    scenario_name: str = ""
    verdict: Optional[str] = None
    measured: bool = False
    error: Optional[str] = None
    annualized_return: Optional[float] = None
    total_return: Optional[float] = None
    option_pnl: Optional[float] = None
    stock_pnl_realized: Optional[float] = None
    stock_pnl_unrealized: Optional[float] = None
    rolls_executed: Optional[int] = None
    itm_rolls: Optional[int] = None
    otm_roll_outs: Optional[int] = None
    roll_net_credit: Optional[float] = None
    itm_roll_credit: Optional[float] = None
    otm_roll_out_credit: Optional[float] = None
    failed_roll_btc_debit: Optional[float] = None
    roll_legs_resting: Optional[int] = None
    roll_legs_marketable: Optional[int] = None
    roll_skips: Dict[str, int] = field(default_factory=dict)
    roll_fill_mode: Optional[str] = None
    assignment_rate: Optional[float] = None
    cycles_completed: Optional[int] = None
    calls_sold: Optional[int] = None
    max_drawdown: Optional[float] = None

    @classmethod
    def from_row(cls, row: Any) -> "Cell":
        """Build from a BigQuery row (or any mapping).

        `roll_skips` is stored as a JSON STRING and persists as NULL for an
        EMPTY dict as well as for a pre-FC-116 row — `roll_fill_mode IS NOT
        NULL` with `roll_skips IS NULL` is "this engine skipped nothing", both
        NULL is "this engine could not count skips". The tool keeps the
        distinction by leaving `roll_fill_mode` alone and reporting it.
        """
        d = dict(row)
        skips = d.get("roll_skips")
        if isinstance(skips, str) and skips.strip():
            try:
                skips = json.loads(skips)
            except ValueError:
                skips = {}
        if not isinstance(skips, dict):
            skips = {}
        known = {f for f in cls.__dataclass_fields__}  # noqa: F821
        kwargs = {k: v for k, v in d.items() if k in known}
        kwargs["roll_skips"] = {str(k): int(v) for k, v in skips.items()}
        for column in ("window_end", "window_start", "holdout_start"):
            value = d.get(column)
            if isinstance(value, (date, datetime)):
                value = value.isoformat()[:10]
            kwargs[column] = str(value)[:10] if value is not None else None
        kwargs["measured"] = bool(d.get("measured"))
        return cls(**kwargs)

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.scenario_name, self.symbol, self.split)


@dataclass(frozen=True)
class ReadSummary:
    """One paired contrast: per-symbol deltas in PERCENTAGE POINTS."""

    name: str
    deltas: Tuple[Tuple[str, float], ...]

    @property
    def values(self) -> List[float]:
        return [v for _s, v in self.deltas]

    @property
    def m(self) -> int:
        """Symbols measured in BOTH arms of this read."""
        return len(self.deltas)

    @property
    def n_pos(self) -> int:
        return sum(1 for v in self.values if v > 0)

    @property
    def n_neg(self) -> int:
        return sum(1 for v in self.values if v < 0)

    @property
    def n_zero(self) -> int:
        return sum(1 for v in self.values if v == 0)

    @property
    def median(self) -> Optional[float]:
        return median(self.values) if self.values else None

    @property
    def sigma(self) -> Optional[float]:
        """Sample s.d. across symbols — the row of the null-rate table to read."""
        return stdev(self.values) if len(self.values) >= 2 else None


@dataclass(frozen=True)
class HoldoutLine:
    agreeing: int
    comparable: int

    @property
    def informative(self) -> bool:
        return self.comparable >= MIN_INFORMATIVE_COMPARABLE

    @property
    def share(self) -> Optional[float]:
        return (self.agreeing / self.comparable) if self.comparable else None

    @property
    def refutes(self) -> bool:
        """Only an INFORMATIVE holdout can refute (DD-1 cond. 3).

        On the 09-11 wheel the holdout measured three cells, and a rule that
        let n = 3 block would be deciding on noise. WHICH symbols the holdout
        dropped, and why, is computed from the rows by `holdout_exclusions` and
        printed — never quoted from a remembered window.
        """
        return self.informative and (self.share or 0.0) < HOLDOUT_AGREEMENT_FLOOR


@dataclass(frozen=True)
class StructuralCheck:
    name: str
    passed: bool
    detail: str = ""


#: What a verdict resolves to when no rule fires. Derived from TIE_BREAK so the
#: constant is LOAD-BEARING, not decorative: change it and the default flips.
DEFAULT_RESOLUTION = "1.00" if TIE_BREAK == "move" else "0.98"


@dataclass(frozen=True)
class Verdict:
    label: str
    resolves_to: str
    reasons: Tuple[str, ...] = ()

    @property
    def is_void(self) -> bool:
        return self.label == "VOID"

    def __str__(self) -> str:
        tail = f" [{'; '.join(self.reasons)}]" if self.reasons else ""
        arrow = f"(->{self.resolves_to})" if self.resolves_to else ""
        return f"VERDICT: {self.label}{arrow}{tail}"


# --------------------------------------------------------------------------- #
# Pure functions. No BigQuery, no GCS, no clock — everything below takes rows
# and returns numbers, so the rule can be tested without a cloud.
# --------------------------------------------------------------------------- #


def finite(value: Any) -> Optional[float]:
    """`float(value)` when it is a real number, else None.

    BigQuery FLOAT64 carries NaN and +/-Inf, and both reach this tool as a
    Python float. `nan > 0` and `nan < 0` are BOTH False, so a NaN delta would
    land in neither `n_pos` nor `n_neg` while still counting towards `M` — it
    would lower the sign count the read is judged against without appearing in
    it, and `median()` over a list containing one would return nonsense. A
    non-finite number is treated as the NULL it is.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def index_cells(cells: Iterable[Cell]) -> Dict[Tuple[str, str, str], Cell]:
    """LAST-wins index by `(scenario_name, symbol, split)`.

    Duplicate keys are real: two pins in one read both carry the implicit
    `base` arm, so every shared symbol has TWO `base` rows. Which one wins is
    not this function's question — `duplicate_cell_keys_differ` (a structural
    check) refuses the read when they disagree on anything the study reads, so
    by the time the rule runs, "last wins" and "first wins" are the same answer.
    """
    return {c.key: c for c in cells}


def duplicate_key_groups(cells: Iterable[Cell],
                         ) -> Dict[Tuple[str, str, str], List[Cell]]:
    """Every `(scenario, symbol, split)` key carried by more than one row."""
    groups: Dict[Tuple[str, str, str], List[Cell]] = {}
    for cell in cells:
        groups.setdefault(cell.key, []).append(cell)
    return {k: v for k, v in sorted(groups.items()) if len(v) > 1}


def dedupe_cells(cells: Iterable[Cell]) -> List[Cell]:
    """One row per cell key, in key order. For the REPORTED tables.

    Printing the economics twice for every symbol the two pins share would
    read as a doubled roll count. Safe only because the structural check above
    has already refused the read if the duplicates differ.
    """
    seen: Dict[Tuple[str, str, str], Cell] = {}
    for cell in cells:
        seen.setdefault(cell.key, cell)
    return [seen[k] for k in sorted(seen)]


def off_study_symbols(cells: Iterable[Cell]) -> List[str]:
    """Symbols present in the rows that are NOT in `STUDY_SYMBOLS`.

    Reported, never deciding (rule clarification R-e): the one-shot may carry
    all fourteen (DD-5) and the extra seven do not measure.
    """
    return sorted({c.symbol for c in cells if c.symbol
                   and c.symbol not in STUDY_SYMBOLS})


def contrast_rows(
    cells: Iterable[Cell], base_arm: str, arm: str, split: str = "fit",
    restrict: Optional[Iterable[str]] = None,
) -> List[Tuple[str, Cell, Cell]]:
    """`(symbol, base_cell, arm_cell)` for symbols MEASURED IN BOTH arms.

    Per-symbol deltas are taken first and the median of THOSE is reported — not
    the difference of two medians, which compares an arm's four symbols against
    base's six and calls the gap a result. It flatters whichever arm traded
    less, and an arm that stops trading is precisely what this study must not
    reward.

    `measured` is the engine's own flag, read from the row. `insufficient`,
    `low_activity` and `error` cells carry no number worth ranking and are
    excluded here — they still appear in the roll-economics tables (T-12f).

    `restrict` defaults to `STUDY_SYMBOLS` (R-e): `M` is the number the sign
    count is judged against, so which symbols may enter it is part of the
    frozen rule and not something a submission can change. Pass an explicit
    set (or `STUDY_SYMBOLS | extras`) only for a REPORTED, non-deciding line.
    """
    allowed = STUDY_SYMBOLS if restrict is None else frozenset(restrict)
    index = index_cells(cells)
    symbols = sorted({c.symbol for c in index.values()} & set(allowed))
    out: List[Tuple[str, Cell, Cell]] = []
    for symbol in symbols:
        b = index.get((base_arm, symbol, split))
        a = index.get((arm, symbol, split))
        if b is None or a is None or not b.measured or not a.measured:
            continue
        if finite(b.annualized_return) is None or finite(
                a.annualized_return) is None:
            continue
        out.append((symbol, b, a))
    return out


def delta_ann_pp(base: Cell, arm: Cell) -> float:
    """Delta annualised return, in PERCENTAGE POINTS."""
    return (arm.annualized_return - base.annualized_return) * 100.0


def delta_opt_pp(base: Cell, arm: Cell) -> Optional[float]:
    """DD-1 condition 2 — the same contrast on the OPTION LEG alone.

    `(option_pnl(arm) - option_pnl(base)) * (365/275) / starting_cash`, in pp.
    A "KEEP" carried by `stock_pnl_*` is a call-strike / assignment-timing
    effect — which is the roller's OWN claim, so if it is real it has to show
    up in the option leg too.
    """
    base_pnl, arm_pnl = finite(base.option_pnl), finite(arm.option_pnl)
    if base_pnl is None or arm_pnl is None:
        return None
    scaled = (arm_pnl - base_pnl) * (
        ANNUALISATION_DAYS / FIT_WINDOW_DAYS) / STARTING_CASH
    return scaled * 100.0


def read_summary(name: str, pairs: Sequence[Tuple[str, Cell, Cell]],
                 value=delta_ann_pp) -> ReadSummary:
    deltas = []
    for symbol, b, a in pairs:
        v = finite(value(b, a))
        if v is not None:
            deltas.append((symbol, v))
    return ReadSummary(name=name, deltas=tuple(deltas))


def sign_agreement_rows(cells: Iterable[Cell], base_arm: str,
                        arm: str) -> HoldoutLine:
    """`(agreeing, comparable)` — `report.sign_agreement` over BigQuery rows.

    "Agrees" means this arm's annualised return MINUS base's, in the fit
    window, has the same sign as the same difference in the holdout. Measuring
    the delta against base rather than the raw return is the point: an arm
    whose raw return is positive in both windows has shown nothing about
    itself, only that the market went up.

    A symbol is comparable only when ALL FOUR cells are measured — which
    excludes `insuf` and `low-act` alike. Zero is a legitimate sign of its own
    and agrees only with another zero. Pinned equal to `report.sign_agreement`
    by T-13.
    """
    index = index_cells(cells)
    symbols = sorted({c.symbol for c in index.values()} & set(STUDY_SYMBOLS))
    agreeing = comparable = 0
    for symbol in symbols:
        quad = [index.get((arm, symbol, "fit")), index.get((arm, symbol, "holdout")),
                index.get((base_arm, symbol, "fit")),
                index.get((base_arm, symbol, "holdout"))]
        if any(c is None or not c.measured
               or finite(c.annualized_return) is None for c in quad):
            continue
        fit_delta = quad[0].annualized_return - quad[2].annualized_return
        hold_delta = quad[1].annualized_return - quad[3].annualized_return
        comparable += 1
        if (fit_delta > 0) == (hold_delta > 0) and (fit_delta < 0) == (hold_delta < 0):
            agreeing += 1
    return HoldoutLine(agreeing=agreeing, comparable=comparable)


def total_return_sign_agreement(cells: Iterable[Cell], base_arm: str = BASE_ARM,
                                arm: str = "t100") -> HoldoutLine:
    """DD-1's SECONDARY holdout line: `total_return`, all NON-ERRORED cells.

    REPORTED, never deciding, and deliberately a different question from the
    primary line above. That one drops any symbol whose cell is not `measured`,
    which on the 09-11 wheel holdout leaves three; this one drops only cells
    that ERRORED, so the symbols the engine called `insufficient` — they rolled,
    they have a `total_return`, the engine simply would not rank them — still
    contribute a sign. Wider and weaker: it is stated beside the primary line
    so a reader can see whether the primary line's n = 3 is hiding a pattern,
    and it can never move the verdict.
    """
    index = index_cells(cells)
    agreeing = comparable = 0
    for symbol in sorted({c.symbol for c in index.values()}):
        quad = [index.get((arm, symbol, "fit")), index.get((arm, symbol, "holdout")),
                index.get((base_arm, symbol, "fit")),
                index.get((base_arm, symbol, "holdout"))]
        if any(c is None or c.error or finite(c.total_return) is None
               for c in quad):
            continue
        fit_delta = quad[0].total_return - quad[2].total_return
        hold_delta = quad[1].total_return - quad[3].total_return
        comparable += 1
        if (fit_delta > 0) == (hold_delta > 0) and (fit_delta < 0) == (hold_delta < 0):
            agreeing += 1
    return HoldoutLine(agreeing=agreeing, comparable=comparable)


def holdout_exclusions(cells: Iterable[Cell], base_arm: str = BASE_ARM,
                       arm: str = "t100") -> List[str]:
    """Why each holdout symbol is NOT comparable — computed, never remembered.

    The record has to say which symbols the holdout line dropped and why. The
    09-11 answer was AMZN and GOOGL (`insufficient` after seven rolls each),
    but quoting that pair in the tool would have it printed on a window where
    it is no longer true. The tool reads it off the rows instead.
    """
    index = index_cells(cells)
    out: List[str] = []
    for symbol in sorted({c.symbol for c in index.values()}):
        quad = {(a, sp): index.get((a, symbol, sp))
                for a in (base_arm, arm) for sp in ("fit", "holdout")}
        if all(c is not None and c.measured
               and finite(c.annualized_return) is not None
               for c in quad.values()):
            continue
        if all(quad[(a, "holdout")] is None for a in (base_arm, arm)):
            continue  # not in the holdout at all — not a holdout exclusion
        why: List[str] = []
        for (a, sp), c in sorted(quad.items()):
            if c is None:
                why.append(f"{a}/{sp} absent")
            elif c.error:
                why.append(f"{a}/{sp} errored")
            elif not c.measured:
                why.append(f"{a}/{sp} {c.verdict or 'not measured'} "
                           f"(rolls {_n(c.rolls_executed)})")
            elif finite(c.annualized_return) is None:
                why.append(f"{a}/{sp} annualized_return not finite")
        if symbol not in STUDY_SYMBOLS:
            why.append("not in STUDY_SYMBOLS")
        out.append(f"{symbol}: " + ", ".join(why))
    return out


def scope_sweeps(sweep_rows: Iterable[Any],
                 windows: Iterable[str]) -> List[Dict[str, Any]]:
    """The sweep rows whose `window_end` is one this read actually reads.

    `resolve_dedup` VOIDs on a `deduplicated` sweep whose target never
    finished. Run unscoped, that is a TRAP on a rolling pin: the pin
    accumulates one point per Saturday and `--since` reaches back 400 days, so
    a single broken point from three months ago would VOID every read of that
    pin for the rest of its life — and the fix (delete the row) is not
    available. A dedup failure on a window this read does not read says
    nothing about the window it does. Scoped to the target window plus the
    monitor windows actually read (D-1).
    """
    wanted = {str(w) for w in windows}
    out: List[Dict[str, Any]] = []
    for row in sweep_rows:
        r = dict(row)
        value = r.get("window_end")
        if isinstance(value, (date, datetime)):
            value = value.isoformat()[:10]
        if str(value)[:10] in wanted:
            out.append(r)
    return out


def sweep_status_lines(sweep_rows: Iterable[Any]) -> List[str]:
    """One line per sweep row read: which pin, which window, which status.

    The read's own provenance. A pin whose point that week is `failed` (or
    still `running`) contributes no cells, and the reader must be able to see
    that from the record rather than inferring it from a short table.
    """
    lines: List[str] = []
    for row in sorted((dict(r) for r in sweep_rows),
                      key=lambda r: (str(r.get("pin_id") or ""),
                                     str(r.get("window_end") or ""),
                                     str(r.get("source_run_id") or ""))):
        target = ("" if row.get("source_run_id") == row.get("resolved_run_id")
                  else f" -> {row.get('resolved_run_id')}"
                        f" [{row.get('resolved_status')}]")
        lines.append(
            f"pin {row.get('pin_id') or '(standing)'} "
            f"window {str(row.get('window_end'))[:10]}: "
            f"{row.get('source_run_id')} [{row.get('source_status')}]{target}")
    return lines


def resolve_dedup(sweep_rows: Iterable[Any]) -> Tuple[Dict[str, str], List[str]]:
    """`({source_run_id: resolved_run_id}, [void reasons])` (DD-6, T-12c).

    A `deduplicated` sweep wrote NO cell rows of its own — it points at the run
    that already measured that key. The cell query follows the pointer, but its
    `s.status = 'done'` would simply DROP a sweep whose target never finished,
    and "this pin silently contributed nothing" is the one failure this study
    cannot absorb: a read with six symbols instead of seven changes
    `MIN_SIGN_COUNT` under the reader's feet. So the VOID is raised HERE, from
    the sweep rows, not by the query.

    The collision is real and expected: a one-shot whose `end` is the coming
    Saturday's `last_settled_day` dedups the pin's FIRST point (the cell key
    carries no `pin_id` and no `submitted_via`). The runbook forbids submitting
    one on a Saturday morning UTC for exactly this reason; this function is
    what makes the read survive it happening anyway.
    """
    mapping: Dict[str, str] = {}
    voids: List[str] = []
    for row in sweep_rows:
        r = dict(row)
        source = str(r.get("source_run_id") or "")
        resolved = str(r.get("resolved_run_id") or source)
        source_status = r.get("source_status")
        resolved_status = r.get("resolved_status")
        mapping[source] = resolved
        if source_status == "deduplicated" and resolved_status != "done":
            voids.append(
                f"dedup_target_not_done:{source}->{r.get('deduplicated_to')}"
                f" (target status {resolved_status!r})")
    return mapping, voids


def structural_checks(cells: Sequence[Cell],
                      standing: Optional[Sequence[Cell]] = None,
                      ) -> List[StructuralCheck]:
    """DD-1's structural validity checks. Any FAIL is a VOID, never a MIXED.

    A MIXED is a statement about the market; a VOID is a statement about the
    measurement. Collapsing the two would let a broken arm be reported as a
    finding about the roller.
    """
    out: List[StructuralCheck] = []

    bad = [f"{c.scenario_name}/{c.symbol}/{c.split}={c.otm_roll_outs}"
           for c in cells
           if c.scenario_name in T100_ARMS and (c.otm_roll_outs or 0) != 0]
    out.append(StructuralCheck(
        "t100_arms_have_no_otm_roll_outs", not bad,
        "; ".join(bad) or "every t100* row has otm_roll_outs == 0, as 1.00 "
        "requires by construction (close/old_strike >= 1.0 -> itm_defence)"))

    bad = [f"{c.scenario_name}/{c.symbol}/{c.split}={c.roll_fill_mode!r}"
           for c in cells
           if c.scenario_name in ARM_FILL_MODE
           and c.roll_fill_mode is not None
           and c.roll_fill_mode != ARM_FILL_MODE[c.scenario_name]]
    out.append(StructuralCheck(
        "roll_fill_mode_matches_its_arm", not bad,
        "; ".join(bad) or "every row's resolved mode equals its arm's declared "
        "mode (the haircut read IS the other end of the bracket)"))

    legacy = [c for c in cells if c.roll_fill_mode is None]
    out.append(StructuralCheck(
        "no_pre_fc116_rows", not legacy,
        "; ".join(sorted({f"{c.scenario_name}/{c.symbol}" for c in legacy}))
        or "every row carries a resolved roll_fill_mode (post-FC-116)"))

    bad = [f"{c.scenario_name}/{c.symbol}/{c.split}"
           for c in cells
           if c.rolls_executed is not None
           and c.rolls_executed != (c.itm_rolls or 0) + (c.otm_roll_outs or 0)]
    out.append(StructuralCheck(
        "rolls_executed_equals_itm_plus_otm", not bad,
        "; ".join(bad) or "the ITM/OTM split accounts for every executed roll"))

    for column in ("engine_version", "engine_identity"):
        values = sorted({str(getattr(c, column)) for c in cells
                         if getattr(c, column)})
        # A read that straddled a deploy is not a read: the arms were replayed
        # by two different engines and the contrast between them is the deploy.
        out.append(StructuralCheck(
            f"{column}_uniform", len(values) <= 1,
            ", ".join(values) if values else "no rows"))

    out.append(_duplicate_key_check(cells))
    out.append(_errored_cells_check(cells))
    out.append(_frozen_constants_check(cells))
    out.append(_placebo_check(cells))
    out.append(_pin_base_check(cells, standing))
    return out


def _duplicate_key_check(cells: Sequence[Cell]) -> StructuralCheck:
    """Same `(arm, symbol, split)` twice, DIFFERING on what the study reads.

    Q-1. The decision pin and the control pin each carry the implicit `base`
    arm, so every symbol they share has two `base` rows in one read. Duplicates
    are therefore EXPECTED and not themselves an error — but `index_cells` is
    last-wins, and "last" is whatever order BigQuery returned, so two rows that
    DISAGREE would silently decide the contrast by row order. Identical
    duplicates are deduped and the read continues; differing ones are a VOID,
    because the two pins were then not asking the same base question.
    """
    groups = duplicate_key_groups(cells)
    differing: List[str] = []
    for key, rows in groups.items():
        cols = [col for col in _IDENTITY_COLUMNS
                if len({getattr(r, col) for r in rows}) > 1]
        if cols:
            runs = ", ".join(sorted({str(r.run_id) for r in rows}))
            differing.append(f"{'/'.join(key)}: {', '.join(cols)} (runs {runs})")
    if not groups:
        return StructuralCheck("duplicate_cell_keys_differ", True,
                               "every (arm, symbol, split) key appears once")
    return StructuralCheck(
        "duplicate_cell_keys_differ", not differing,
        "; ".join(differing) or
        f"{len(groups)} keys appear twice (both pins carry `base`), every one "
        f"identical on {', '.join(_IDENTITY_COLUMNS)} — deduped for the tables")


def _errored_cells_check(cells: Sequence[Cell]) -> StructuralCheck:
    """A cell that ERRORED (D-5). VOID, never a quietly smaller `M`.

    An errored cell is not `measured`, so it already falls out of every
    contrast — which is exactly the danger: the read comes back with M = 6
    instead of 7 and is judged against `MIN_SIGN_COUNT[6] = 5` without anyone
    stating that a symbol failed to replay. The rows STAY in the economics
    tables; only the verdict is refused.
    """
    bad = [f"{c.scenario_name}/{c.symbol}/{c.split}: {str(c.error)[:70]}"
           for c in cells if c.error]
    return StructuralCheck("errored_cells", not bad,
                           "; ".join(sorted(bad))
                           or "no cell carries an `error`")


def _frozen_constants_check(cells: Sequence[Cell]) -> StructuralCheck:
    """`FIT_WINDOW_DAYS` and `STARTING_CASH` are CLAIMS about the rows.

    `delta_opt_pp` annualises dollar option P&L by `365 / FIT_WINDOW_DAYS` and
    divides by `STARTING_CASH`. Both are frozen module constants, so a row
    whose sweep actually ran a different window shape or a different cash base
    silently rescales DD-1 condition 2. The sweep row carries `window_start`
    and `holdout_start` (the fit split is the days between them) and the spec's
    `starting_cash`; a row that disagrees is a VOID, not a rescale.
    """
    bad: List[str] = []
    for cell in cells:
        if cell.window_start and cell.holdout_start:
            try:
                fit_days = (date.fromisoformat(cell.holdout_start)
                            - date.fromisoformat(cell.window_start)).days
            except ValueError:
                bad.append(f"{cell.run_id}: unparseable window "
                           f"{cell.window_start}..{cell.holdout_start}")
                continue
            if fit_days != FIT_WINDOW_DAYS:
                bad.append(f"{cell.run_id}: fit split {fit_days}d "
                           f"!= FIT_WINDOW_DAYS {FIT_WINDOW_DAYS}")
        cash = finite(cell.spec_starting_cash)
        if cash is not None and cash != STARTING_CASH:
            bad.append(f"{cell.run_id}: starting_cash {cash:,.0f} "
                       f"!= STARTING_CASH {STARTING_CASH:,.0f}")
    return StructuralCheck(
        "frozen_constants_match_the_spec", not bad,
        "; ".join(sorted(set(bad)))
        or f"every row's fit split is {FIT_WINDOW_DAYS}d on "
           f"${STARTING_CASH:,.0f} (or does not say)")


#: The structural checks the OOS read carries in its own right (R-c). Not the
#: fill-mode or roll-arithmetic ones: those hold per row and the OOS rows go
#: through the same writer, but the three below are the ones that would let an
#: OOS read REFUTE a KEEP while measuring nothing.
_OOS_STRUCTURAL_NAMES = ("t100_arms_have_no_otm_roll_outs",
                         "no_pre_fc116_rows", "placebo_gate")


def oos_structural_checks(oos_cells: Sequence[Cell],
                          decision_cells: Sequence[Cell],
                          ) -> List[StructuralCheck]:
    """The OOS read's own structural burden (rule clarification R-c).

    The OOS read is a KEEP condition: it can turn a passing KEEP into
    MIXED-CONFLICT. A condition with that power has to be a real measurement,
    so it carries the placebo gate and the two construction checks in its own
    right — AND it must have been replayed by the SAME engine as the decision
    read, because an OOS from another engine is a different measurement, not an
    out-of-sample one.
    """
    out: List[StructuralCheck] = []
    if not oos_cells:
        return out
    for check in structural_checks(oos_cells):
        if check.name in _OOS_STRUCTURAL_NAMES:
            out.append(StructuralCheck(f"oos_{check.name}", check.passed,
                                       check.detail))
    for column in ("engine_version", "engine_identity"):
        theirs = sorted({str(getattr(c, column)) for c in oos_cells
                         if getattr(c, column)})
        ours = sorted({str(getattr(c, column)) for c in decision_cells
                       if getattr(c, column)})
        out.append(StructuralCheck(
            f"oos_{column}_equals_the_decision_read", theirs == ours,
            f"oos {theirs or ['(none)']} vs decision {ours or ['(none)']}"))
    return out


#: The columns the placebo gate and the pin/standing equality compare. Changing
#: a trigger ratio MUST move at least one of these on a row that actually
#: rolled; a pin's base cell MUST match the standing cell on all three.
_IDENTITY_COLUMNS = ("annualized_return", "rolls_executed", "option_pnl")


def _placebo_check(cells: Sequence[Cell]) -> StructuralCheck:
    """`t100` must differ from `base` on at least one ROLL-BEARING row.

    FC-096-a's placebo gate. Two arms that come back byte-identical mean the
    override did not reach the engine — which reads, to an unwary reader, as
    "the trigger does not matter". It is the single most dangerous null this
    study can produce, so it is a VOID and not a MIXED-NULL.
    """
    index = index_cells(cells)
    roll_bearing = 0
    differing = 0
    for (arm, symbol, split), cell in sorted(index.items()):
        if arm != BASE_ARM:
            continue
        if not (cell.rolls_executed or 0):
            continue
        other = index.get(("t100", symbol, split))
        if other is None:
            continue
        roll_bearing += 1
        if any(getattr(other, col) != getattr(cell, col)
               for col in _IDENTITY_COLUMNS):
            differing += 1
    if roll_bearing == 0:
        return StructuralCheck(
            "placebo_gate", False,
            "no roll-bearing base/t100 row pair to compare — the window "
            "measured no roll at all, so the study measured nothing")
    return StructuralCheck(
        "placebo_gate", differing > 0,
        f"{differing} of {roll_bearing} roll-bearing base/t100 row pairs differ")


def _pin_base_check(cells: Sequence[Cell],
                    standing: Optional[Sequence[Cell]]) -> StructuralCheck:
    """Every pin's `base` cell must equal the standing wheel item's (DD-4).

    Free, and it catches the one mistake a pin body can make silently: a
    `window_days` of 364 instead of 365. The GOOGL proof pin is 364 and its fit
    cell differs from the standing one — 0.085731 vs 0.085773, `option_pnl`
    4,151 vs 4,178 — one day of option P&L. A pin meant to ask the standing
    question and asking a one-day-shorter one would put that $27 inside every
    delta the study reads.
    """
    if not standing:
        return StructuralCheck(
            "pin_base_equals_standing_base", True,
            "SKIPPED - no standing rows read (--run-id mode, or the standing "
            "set was not at this window)")
    pin_index = index_cells(cells)
    std_index = index_cells(standing)
    compared = 0
    bad: List[str] = []
    for (arm, symbol, split), cell in sorted(pin_index.items()):
        if arm != BASE_ARM or cell.pin_id is None:
            continue
        other = std_index.get((BASE_ARM, symbol, split))
        if other is None:
            continue
        compared += 1
        diffs = [col for col in _IDENTITY_COLUMNS
                 if getattr(cell, col) != getattr(other, col)]
        if diffs:
            bad.append(f"{cell.pin_id}/{symbol}/{split}: {', '.join(diffs)}")
    if compared == 0:
        return StructuralCheck(
            "pin_base_equals_standing_base", True,
            "SKIPPED - no standing base cell shares a symbol/split with a pin")
    return StructuralCheck(
        "pin_base_equals_standing_base", not bad,
        "; ".join(bad) or f"{compared} pin base cells byte-equal the standing "
        f"cell on {', '.join(_IDENTITY_COLUMNS)}")


def _crosses(summary: ReadSummary, sign: int) -> bool:
    """Does this read's MEDIAN cross MIN_EFFECT_PP in `sign`'s direction?

    The median, not the sign count: "crosses the opposite threshold" in DD-1's
    MIXED-CONFLICT row is about `MIN_EFFECT_PP`, the one number that is a
    threshold on a quantity. A read whose sign count leans the other way but
    whose median is inside +/- 0.5 pp has not measured a conflicting effect; it
    has measured noise, and calling that a conflict would park the study on the
    status quo on the strength of nothing.
    """
    if summary.median is None:
        return False
    return (summary.median >= MIN_EFFECT_PP if sign > 0
            else summary.median <= -MIN_EFFECT_PP)


def _opposes(summary: ReadSummary, sign: int) -> bool:
    """Does this read point the OPPOSITE way from `sign`? (R-a, 2026-09-12.)

    Wider than `_crosses`, deliberately. DD-1's MIXED-CONFLICT row is "another
    read crosses the OPPOSITE threshold", and rev 3 read "threshold" as the
    median crossing `MIN_EFFECT_PP` only. The program owner's clarification
    widens it: a partial result in which one read passes KEEP while another has
    a median of the opposite SIGN — at any magnitude — or an opposite sign
    count that would itself have met `MIN_SIGN_COUNT[M]`, is a conflict. The
    reads are the same trades re-priced, so two of them disagreeing about the
    DIRECTION is a statement about the measurement, and the study resolves that
    by review rather than by rule.
    """
    median_value = finite(summary.median)
    if median_value is not None and median_value != 0.0:
        if (median_value > 0) == (sign < 0):
            return True
    required = MIN_SIGN_COUNT.get(summary.m)
    if required is None:
        return False
    opposite_count = summary.n_pos if sign < 0 else summary.n_neg
    return opposite_count >= required


def _passes(summary: ReadSummary, sign: int) -> bool:
    """Sign count AND median, in `sign`'s direction, for one read."""
    required = MIN_SIGN_COUNT.get(summary.m)
    if required is None:
        return False
    count = summary.n_pos if sign > 0 else summary.n_neg
    return count >= required and _crosses(summary, sign)


def apply_rule(reads: Dict[str, ReadSummary],
               opt_read: ReadSummary,
               holdout: HoldoutLine,
               oos: Optional[ReadSummary] = None,
               monitor_flip: bool = False,
               structural: Sequence[StructuralCheck] = ()) -> Verdict:
    """DD-1's ONE-SIDED pre-registered rule. Takes NO thresholds (T-10).

    Every number this rule turns on is a module constant frozen at this
    commit's SHA — `MIN_EFFECT_PP`, `MIN_SIGN_COUNT`, `TIE_BREAK`,
    `HOLDOUT_AGREEMENT_FLOOR`, `MIN_INFORMATIVE_COMPARABLE`. There is
    deliberately no parameter to pass one in, because a threshold that can be
    passed in is a threshold that can be chosen after the data is seen, and at
    M = 7 the sign count alone is a 6.25 % test (25 % under a common factor).

    DD-1's verdict table left five gaps. Both PR-2 reviewers found them; the
    PROGRAM OWNER closed them on 2026-09-12 (plan §Amendments rev 4), and the
    rulings are implemented here literally — each one closes TOWARDS operator
    review and none moves a threshold or the signed direction:

    * **The holdout and the OOS read are KEEP conditions** (DD-1 cond. 3 and
      4), so they turn a would-be KEEP into MIXED-CONFLICT. They do not
      manufacture a conflict when nothing pointed at 0.98 in the first place: a
      null decision read plus an OOS leaning to 1.00 is two things agreeing,
      and MIXED-CONFLICT parks on the status quo (0.98). **R-b** extends the
      holdout's refutation to MOVE as well as KEEP, per DD-1's literal "any of".
    * **R-a, partial outcomes.** Some reads passing while another read points
      the OPPOSITE way (opposite median sign at any magnitude, or an opposite
      sign count at `MIN_SIGN_COUNT[M]`) is MIXED-CONFLICT. Some reads passing
      with the others same-signed but under threshold is NOT MIXED-NULL: it
      resolves to the default under its own label, `PARTIAL-SAME-SIGN`, so the
      record can never call a partial result "no measurable difference". A
      partial in which a read CROSSED `MIN_EFFECT_PP` without meeting its sign
      count — which never reaches the conflict branch, since nothing passes —
      while another read points the opposite way resolves to the default too,
      under `PARTIAL-CROSSED-OPPOSED`: same resolution, a label that does not
      assert the sign agreement the reads did not show, with the opposing
      reads named in the reason.
      MIXED-NULL keeps DD-1's literal definition — NO read crosses EITHER
      threshold anywhere, and no sign conflict.
    * **R-d, the option leg.** `opt_read` must be computed on the same symbols
      as the `limit` read; a different `M` means the two are not the same
      contrast and is `VOID(option_leg_m_mismatch)`. A genuine KEEP refused
      ONLY because the effect is not located in the option leg resolves to the
      default under its own label, `KEEP-REFUSED-OPTION-LEG` — not MIXED-NULL.
      If the holdout ALSO refutes, MIXED-CONFLICT wins, for consistency with
      the KEEP branch.
    * **R-c, the OOS floor.** An OOS read with fewer than `OOS_MIN_M` symbols
      is `uninformative` and cannot refute. Its structural checks run on the
      OOS rows (`oos_structural_checks`) and reach this function as ordinary
      structural checks.
    """
    failed = [c for c in structural if not c.passed]
    if failed:
        return Verdict("VOID", "", tuple(f"structural:{c.name}" for c in failed))

    missing = [name for name, _b, _t in READS if name not in reads]
    if missing:
        return Verdict("VOID", "", tuple(f"missing_read:{n}" for n in missing))

    for name, summary in sorted(reads.items()):
        if summary.m < min(MIN_SIGN_COUNT):
            return Verdict("VOID", "",
                           (f"insufficient_measured:{name}:M={summary.m}",))
        if summary.m not in MIN_SIGN_COUNT:
            return Verdict("VOID", "",
                           (f"unregistered_M:{name}:M={summary.m}",))

    # R-d. The option-leg contrast is the SAME contrast on a different column,
    # so it must rest on the same symbols. A different M means one of them
    # dropped a symbol the other kept, and condition 2 would then be comparing
    # two different portfolios.
    primary = reads.get(PRIMARY_READ)
    if primary is not None and opt_read.m != primary.m:
        return Verdict("VOID", "",
                       (f"option_leg_m_mismatch:option leg M={opt_read.m} vs "
                        f"`{PRIMARY_READ}` M={primary.m}",))

    keeps = {n: _passes(s, -1) for n, s in reads.items()}
    moves = {n: _passes(s, +1) for n, s in reads.items()}

    conflicts: List[str] = []
    if any(keeps.values()):
        opposing = sorted(n for n, s in reads.items() if _opposes(s, -1))
        if opposing:
            conflicts.append(
                "read_sign_conflict:a read passes KEEP while "
                + ",".join(opposing) + " points the other way (opposite "
                "median sign, or an opposite sign count at MIN_SIGN_COUNT)")
    if any(moves.values()):
        opposing = sorted(n for n, s in reads.items() if _opposes(s, +1))
        if opposing:
            conflicts.append(
                "read_sign_conflict:a read passes MOVE while "
                + ",".join(opposing) + " points the other way (opposite "
                "median sign, or an opposite sign count at MIN_SIGN_COUNT)")
    if monitor_flip:
        conflicts.append("monitor_sign_flip:the primary `limit` median changed "
                         "sign across the monitor points")
    if conflicts:
        return Verdict("MIXED-CONFLICT", "0.98", tuple(conflicts))

    holdout_reason = (f"holdout_refutes:{holdout.agreeing}/"
                      f"{holdout.comparable} sign agreement")
    oos_informative = oos is not None and oos.m >= OOS_MIN_M

    if all(keeps.values()):
        # Order matters (R-d): a KEEP that the holdout refutes is a CONFLICT
        # whether or not the option leg located it.
        if holdout.refutes:
            return Verdict("MIXED-CONFLICT", "0.98", (holdout_reason,))
        if oos_informative and _crosses(oos, +1):
            return Verdict("MIXED-CONFLICT", "0.98",
                           (f"oos_refutes:median {oos.median:+.2f} pp "
                            f"(M={oos.m})",))
        if not _passes(opt_read, -1):
            return Verdict("KEEP-REFUSED-OPTION-LEG", DEFAULT_RESOLUTION,
                           ("keep_refused_option_leg_not_located:the effect is "
                            "not in the option leg (DD-1 condition 2)",))
        return Verdict("KEEP", "0.98", ())

    if all(moves.values()):
        # R-b. DD-1's MIXED-CONFLICT row says "the holdout refutes" without
        # restricting it to a KEEP, and a MOVE the holdout contradicts is the
        # same kind of unreliable as a KEEP the holdout contradicts.
        if holdout.refutes:
            return Verdict("MIXED-CONFLICT", "0.98", (holdout_reason,))
        return Verdict("MOVE", "1.00", ())

    # R-a. Everything that reaches here resolves to the default, and only a
    # read where NOTHING crossed either threshold anywhere is MIXED-NULL,
    # because "no measurable difference" is a claim and a partial result is
    # not it. The two partials differ in what they may assert about SIGN: a
    # `passing` partial IS same-signed (a passing read beside an opposing one
    # returned MIXED-CONFLICT above), but a read that merely CROSSED without
    # meeting its sign count never reaches that branch, so an opposing read
    # can still be standing here. Same resolution, separate label — the record
    # must not claim an agreement the reads did not show.
    passing = sorted(n for n in reads if keeps[n] or moves[n])
    crossed = sorted(n for n, s in reads.items()
                     if _crosses(s, +1) or _crosses(s, -1))
    if passing:
        return Verdict(
            "PARTIAL-SAME-SIGN", DEFAULT_RESOLUTION,
            ("partial_same_sign:" + ",".join(passing) + " passed the full rule;"
             " the other reads agree in sign but stay under threshold",))
    if crossed:
        crossed_signs = [sign for sign in (-1, +1)
                         if any(_crosses(reads[n], sign) for n in crossed)]
        opposing = sorted({n for n, s in reads.items()
                           for sign in crossed_signs if _opposes(s, sign)})
        if opposing:
            return Verdict(
                "PARTIAL-CROSSED-OPPOSED", DEFAULT_RESOLUTION,
                ("partial_crossed_opposed:" + ",".join(crossed) + " crossed "
                 "MIN_EFFECT_PP without meeting its sign count, while "
                 + ",".join(opposing) + " points the other way (opposite "
                 "median sign, or an opposite sign count at MIN_SIGN_COUNT)",))
        return Verdict(
            "PARTIAL-SAME-SIGN", DEFAULT_RESOLUTION,
            ("partial_same_sign:" + ",".join(crossed) + " crossed "
             "MIN_EFFECT_PP without meeting its sign count",))
    return Verdict("MIXED-NULL", DEFAULT_RESOLUTION,
                   ("no_read_crosses_either_threshold",))


def null_rate_row(sigma: Optional[float]) -> Optional[
        Tuple[float, float, float, float, float]]:
    """The NULL_RATE_TABLE row matching an empirical sigma (nearest sigma)."""
    if sigma is None:
        return None
    return min(NULL_RATE_TABLE, key=lambda row: abs(row[0] - sigma))


# --------------------------------------------------------------------------- #
# Artifacts (--artifacts). Optional: the BigQuery read alone yields a verdict.
# --------------------------------------------------------------------------- #

FILL_RULE_RESTING = "limit_resting"
_ROLL_LEG_KINDS = frozenset({"buy_to_close", "sell_call_open"})
_TERMINAL_CALL_KINDS = ("expire_worthless", "call_assignment", "buy_to_close")


def artifact_blob_name(run_id: str, arm: str, symbol: str, split: str) -> str:
    return f"{ARTIFACT_PREFIX}/{run_id}/{arm}__{symbol}__{split}.json.gz"


def _ev_date(event: Dict[str, Any]) -> str:
    """The stored artifact's ledger key is `date`.

    `artifact._ledger_rows` (`src/backtesting/reporting/artifact.py:216`)
    serialises `LedgerEvent.event_date` under the key **`date`**, which is what
    DD-6 says and what every object in the bucket carries. `event_date` is
    accepted as a fallback only so an in-memory `LedgerEvent` dict — the shape
    the broker holds before serialisation — reads the same way.
    """
    value = event.get("date") or event.get("event_date") or ""
    return str(value)[:10]


def _ev_underlying(event: Dict[str, Any]) -> str:
    return str(event.get("underlying") or event.get("symbol") or "")


def occ_expiry(option_symbol: str) -> Optional[date]:
    """`AAPL260918C00230000` -> 2026-09-18. None if it does not parse."""
    body = str(option_symbol or "").strip().upper()
    for i, ch in enumerate(body):
        if ch.isdigit():
            digits = body[i:i + 6]
            if len(digits) == 6 and digits.isdigit():
                try:
                    return datetime.strptime(digits, "%y%m%d").date()
                except ValueError:
                    return None
            return None
    return None


def resting_by_kind(artifact: Dict[str, Any]) -> Dict[str, Tuple[int, int]]:
    """`roll_kind -> (resting legs, total legs)` (DD-3's reported line).

    The imminence residual is NOT of unknown sign: a leg priced at mid +/- 0.05
    and assumed touched inside the modelled spread costs $0.10 per share per
    roll, below BOTH ends of the bracket whenever the half-spread exceeds
    $0.20 — while live, a resting limit that is not touched is
    `btc_timeout_canceled`, i.e. no roll at all. So it always favours the
    roller. The split by kind is what says WHOSE roller: on the 09-11 base arm
    it was 36.1 % of `itm_defence` legs (which both arms execute) and 1.6 % of
    `otm_roll_out` legs (which only 0.98 executes).
    """
    by_day: Dict[Tuple[str, str], str] = {}
    for record in artifact.get("roll_records") or []:
        if not record.get("success", True):
            continue
        by_day[(str(record.get("day"))[:10],
                str(record.get("underlying") or ""))] = str(
                    record.get("roll_kind") or "unknown")
    out: Dict[str, List[int]] = {}
    for event in artifact.get("ledger") or []:
        if str(event.get("kind")) not in _ROLL_LEG_KINDS:
            continue
        kind = by_day.get((_ev_date(event), _ev_underlying(event)))
        if kind is None:
            continue
        bucket = out.setdefault(kind, [0, 0])
        bucket[1] += 1
        if str((event.get("detail") or {}).get("fill_rule")) == FILL_RULE_RESTING:
            bucket[0] += 1
    return {k: (v[0], v[1]) for k, v in sorted(out.items())}


def replacement_dte_tell(artifact: Dict[str, Any]) -> Dict[str, int]:
    """DD-2's cheap tell: how many replacements sat AT the ladder's edge.

    PR-1 removed the truncation for the FIRST roll of a chain only. A call
    already rolled out is held at ~15-22 DTE, so its own next horizon is
    `old_expiry + 14` = 29-36 days against a 21-DTE ladder, and every chained
    roll past the first still chooses from a truncated set — fewer and shorter
    candidates, on the arm that rolls MORE. A replacement whose DTE sits at the
    cutoff is a search that hit the edge, not one that chose.

    The roll record carries no expiry (`call_roller`'s success dict is
    `underlying/old_strike/new_strike/contracts/net_credit/*_order_id`), so the
    replacement is recovered from the day's `sell_call_open` OCC symbol.
    `unknown` is reported rather than folded into `below_cutoff`: "we could not
    tell" and "it chose freely" are opposite findings.
    """
    opens: Dict[Tuple[str, str], str] = {}
    for event in artifact.get("ledger") or []:
        if str(event.get("kind")) == "sell_call_open":
            opens[(_ev_date(event), _ev_underlying(event))] = str(
                event.get("symbol") or "")
    tally = {"at_cutoff": 0, "below_cutoff": 0, "unknown": 0}
    for record in artifact.get("roll_records") or []:
        if not record.get("success", True):
            continue
        day = str(record.get("day"))[:10]
        symbol = opens.get((day, str(record.get("underlying") or "")))
        expiry = occ_expiry(symbol) if symbol else None
        try:
            decided = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            decided = None
        if expiry is None or decided is None:
            tally["unknown"] += 1
            continue
        dte = (expiry - decided).days
        tally["at_cutoff" if dte >= REPLACEMENT_DTE_CUTOFF
              else "below_cutoff"] += 1
    return tally


def paired_events(base_artifact: Dict[str, Any],
                  t100_artifact: Dict[str, Any]) -> List[Dict[str, Any]]:
    """DD-1's paired-event table: what the verdict MEANS in trades.

    Each day on which `base` executed an `otm_roll_out` is a day `t100` did
    not — at 1.00 the trigger cannot fire below the strike, which is why the
    t100 arm's `not_itm_enough` tally is the corroborating aggregate (the
    artifact's `roll_skips` is reason -> COUNT, with no day on it, so the
    per-day pairing is structural rather than looked up).

    For each such day the row carries the base roll's economics and the
    TERMINAL outcome of the un-rolled call on the t100 side: expired worthless,
    called away, or bought back.

    Three things this table has to get right, all found in review (Q-8):

    * **The t100 call must still be OPEN on `day`.** Taking the last
      `sell_call_open` at or before `day` finds a contract even when that
      contract was assigned or expired WEEKS earlier and the t100 arm was flat
      on the day in question. The row then reports a terminal event that has
      nothing to do with the base arm's roll. A terminal event between the open
      and `day` disqualifies the pairing, and the row says so.
    * **t100 may have rolled that day too.** At 1.00 the trigger cannot fire
      BELOW the strike, but it fires at or above it — so a day on which base
      rolled OTM can also be a day t100 executed an `itm_defence`. The premise
      "t100 could not roll" is then false for that row, and the column says so
      instead of the table implying otherwise.
    * **A `buy_to_close` that is a ROLL's near leg is not "bought back".**
      Every roll closes the held call, so the terminal scan finds a
      `buy_to_close` and would report the call as closed out when it was in
      fact rolled forward. Cross-checked against t100's own `roll_records` for
      that day, and labelled as the roll it was.
    """
    rows: List[Dict[str, Any]] = []
    ledger = sorted((t100_artifact.get("ledger") or []), key=_ev_date)
    t100_rolls: Dict[Tuple[str, str], str] = {}
    for record in t100_artifact.get("roll_records") or []:
        if not record.get("success", True):
            continue
        t100_rolls[(str(record.get("day"))[:10],
                    str(record.get("underlying") or ""))] = str(
                        record.get("roll_kind") or "unknown")
    for record in base_artifact.get("roll_records") or []:
        if not record.get("success", True):
            continue
        if str(record.get("roll_kind")) != "otm_roll_out":
            continue
        day = str(record.get("day"))[:10]
        underlying = str(record.get("underlying") or "")
        held: Optional[str] = None
        opened = ""
        for event in ledger:
            if (str(event.get("kind")) == "sell_call_open"
                    and _ev_underlying(event) == underlying
                    and _ev_date(event) <= day):
                held = str(event.get("symbol") or "")
                opened = _ev_date(event)
        outcome = ("no open t100 call found" if held is None
                   else "still open at window end")
        outcome_day = ""
        if held is not None:
            closed_first = next(
                (e for e in ledger
                 if str(e.get("symbol")) == held
                 and str(e.get("kind")) in _TERMINAL_CALL_KINDS
                 and opened <= _ev_date(e) < day), None)
            if closed_first is not None:
                outcome = (f"NOT HELD on the day — that call ended "
                           f"{closed_first.get('kind')} "
                           f"{_ev_date(closed_first)}")
                held = None
        if held is not None:
            for event in ledger:
                if (str(event.get("symbol")) == held
                        and str(event.get("kind")) in _TERMINAL_CALL_KINDS
                        and _ev_date(event) >= day):
                    kind = str(event.get("kind"))
                    outcome_day = _ev_date(event)
                    rolled = t100_rolls.get((outcome_day, underlying))
                    outcome = (f"rolled ({rolled}) — NOT bought back"
                               if kind == "buy_to_close" and rolled
                               else kind)
                    break
        rows.append({
            "day": day,
            "symbol": underlying,
            "net_credit": record.get("net_credit"),
            "old_strike": record.get("old_strike"),
            "new_strike": record.get("new_strike"),
            "itm_ratio": record.get("itm_ratio"),
            "t100_contract": held or "",
            "t100_outcome": outcome,
            "t100_outcome_day": outcome_day,
            "t100_same_day_roll": t100_rolls.get((day, underlying), ""),
        })
    return rows


# --------------------------------------------------------------------------- #
# I/O. Read-only, and the only part of this module that touches a cloud.
# --------------------------------------------------------------------------- #

class ProvenanceError(RuntimeError):
    """The tool cannot name the rule it is about to apply. It REFUSES to run.

    Q-4 / D-3. The whole pre-registration rests on one claim: the SHA printed
    beside the verdict identifies the source that produced it. Three ways that
    claim can be false, and all three used to degrade into a verdict with the
    string "unavailable (no git)" where the SHA belongs:

    * the tool's own file is MODIFIED in the working tree — the printed HEAD
      names a rule that is not the one that ran;
    * `git` is not installed, or the tool is running outside a checkout;
    * `git` answers, but with something that is not a SHA.

    A verdict that cannot name its rule is not a pre-registered verdict, and a
    record is worse for containing one than for containing nothing. So the tool
    exits non-zero and prints no verdict at all.
    """


@dataclass(frozen=True)
class Provenance:
    """The commit the tool ran from, and the blob of the tool ITSELF.

    The blob hash is the stronger claim of the two: HEAD identifies a tree,
    and a reader has to trust that the tree contained this file unchanged;
    `git rev-parse HEAD:<path>` names the exact bytes of the rule. Both are
    printed, so a record can be checked with `git cat-file -p <blob>`.
    """

    head: str
    blob: str
    path: str


def _git(args: Sequence[str], cwd: str) -> str:
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvenanceError(f"git is unavailable ({exc})") from exc
    if out.returncode != 0:
        raise ProvenanceError(
            f"`git {' '.join(args)}` failed: "
            f"{(out.stderr or '').strip()[:200] or 'no stderr'}")
    return (out.stdout or "").strip()


def tool_provenance(path: Optional[str] = None) -> Provenance:
    """HEAD + the tool's own blob hash, or raise `ProvenanceError`."""
    path = os.path.abspath(path or __file__)
    top = _git(["rev-parse", "--show-toplevel"], os.path.dirname(path))
    rel = os.path.relpath(path, top)
    dirty = _git(["status", "--porcelain", "--", rel], top)
    if dirty:
        raise ProvenanceError(
            f"{rel} is MODIFIED in the working tree "
            f"({dirty.splitlines()[0].strip()}). The SHA this tool would print "
            "names a version of the rule that is not the one about to run. "
            "Commit or stash the change, then re-run — a verdict is only "
            "pre-registered if the record can name the bytes that produced it.")
    head = _git(["rev-parse", "HEAD"], top)
    blob = _git(["rev-parse", f"HEAD:{rel}"], top)
    if len(head) != 40 or len(blob) != 40:
        raise ProvenanceError(
            f"git returned a malformed object name (HEAD {head!r}, "
            f"blob {blob!r})")
    return Provenance(head=head, blob=blob, path=rel)


def _rows(client: Any, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
    from google.cloud import bigquery  # imported late: the rule needs no cloud

    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=list(params)))
    return [dict(r) for r in job.result()]


def _params(since: str, *, pin_ids: Sequence[str] = (),
            run_ids: Sequence[str] = (),
            window_end: Optional[str] = None) -> List[Any]:
    from google.cloud import bigquery

    out: List[Any] = [bigquery.ScalarQueryParameter("since", "TIMESTAMP",
                                                    f"{since}T00:00:00Z")]
    if pin_ids:
        out.append(bigquery.ArrayQueryParameter("pin_ids", "STRING",
                                                list(pin_ids)))
    if run_ids:
        out.append(bigquery.ArrayQueryParameter("run_ids", "STRING",
                                                list(run_ids)))
    if window_end:
        out.append(bigquery.ScalarQueryParameter("window_end", "DATE",
                                                 window_end))
    return out


def fetch_cells(client: Any, dataset_ref: str, selector: str,
                params: Sequence[Any]) -> List[Cell]:
    sql = _cell_sql(dataset_ref, bind(selector, "s", "s.source_run_id"))
    return [Cell.from_row(r) for r in _rows(client, sql, params)]


def fetch_sweeps(client: Any, dataset_ref: str, selector: str,
                 params: Sequence[Any]) -> List[Dict[str, Any]]:
    sql = _sweep_sql(dataset_ref, bind(selector, "l", "l.run_id"))
    return _rows(client, sql, params)


def load_artifact(storage_client: Any, bucket: str, run_id: str, arm: str,
                  symbol: str, split: str) -> Optional[Dict[str, Any]]:
    """One cell's stored artifact, or None when it is not there.

    None rather than an exception: `--artifacts` is an opt-in enrichment and a
    missing object must degrade the paired-event table, never the verdict.
    """
    name = artifact_blob_name(run_id, arm, symbol, split)
    try:
        blob = storage_client.bucket(bucket).blob(name)
        if not blob.exists():
            return None
        raw = blob.download_as_bytes()
    except Exception:  # noqa: BLE001 - a read-only enrichment never blocks
        return None
    try:
        return json.loads(gzip.decompress(raw).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Assembling one read.
# --------------------------------------------------------------------------- #


def monitor_sign_flip(medians: Sequence[Optional[float]]) -> bool:
    """Did the PRIMARY `limit` median change sign across the monitor points?

    DD-4: consecutive trailing-year windows overlap 51 of 52 weeks, so four
    Saturdays are ONE read with the edges moved, not four reads. The monitor is
    a fragility check and nothing else — the rule is never re-applied to
    4-week medians. A sign flip on the primary read is the one thing four
    near-identical windows CAN say, and it says MIXED-CONFLICT.

    A zero median is NOT a third sign: a read whose median is exactly 0.0 has
    not pointed anywhere, and counting it as its own sign would report a "flip"
    between a null window and a signed one. Non-finite medians are ignored for
    the same reason (`finite`).
    """
    values = [v for v in (finite(m) for m in medians)
              if v is not None and v != 0.0]
    return len({1 if v > 0 else -1 for v in values}) > 1


@dataclass
class Analysis:
    reads: Dict[str, ReadSummary]
    opt_read: ReadSummary
    holdout: HoldoutLine
    structural: List[StructuralCheck]
    verdict: Verdict
    oos: Optional[ReadSummary] = None
    monitor_flip: bool = False
    monitor_windows: int = 0
    cells: Sequence[Cell] = ()
    standing: Sequence[Cell] = ()
    holdout_total_return: Optional[HoldoutLine] = None
    holdout_exclusions: Sequence[str] = ()
    off_study: Sequence[str] = ()


def analyse(cells: Sequence[Cell],
            standing: Optional[Sequence[Cell]] = None,
            oos_cells: Optional[Sequence[Cell]] = None,
            monitor_medians: Sequence[Optional[float]] = (),
            dedup_voids: Sequence[str] = ()) -> Analysis:
    """Rows in, verdict out. Pure: no clock, no cloud, no arguments to the rule."""
    structural = list(structural_checks(cells, standing))
    structural += oos_structural_checks(list(oos_cells or ()), cells)
    for reason in dedup_voids:
        structural.append(StructuralCheck(reason, False, reason))

    reads: Dict[str, ReadSummary] = {}
    for name, base_arm, arm in READS:
        reads[name] = read_summary(
            name, contrast_rows(cells, base_arm, arm, "fit"))
    opt_read = read_summary(
        "limit_option_leg", contrast_rows(cells, BASE_ARM, "t100", "fit"),
        delta_opt_pp)
    holdout = sign_agreement_rows(cells, BASE_ARM, "t100")
    oos = None
    if oos_cells:
        oos = read_summary("oos",
                           contrast_rows(oos_cells, BASE_ARM, "t100", "fit"))
    flip = monitor_sign_flip(monitor_medians)
    verdict = apply_rule(reads, opt_read, holdout, oos=oos, monitor_flip=flip,
                         structural=structural)
    return Analysis(reads=reads, opt_read=opt_read, holdout=holdout,
                    structural=structural, verdict=verdict, oos=oos,
                    monitor_flip=flip,
                    monitor_windows=len([m for m in monitor_medians
                                         if finite(m) is not None]),
                    cells=tuple(cells), standing=tuple(standing or ()),
                    holdout_total_return=total_return_sign_agreement(cells),
                    holdout_exclusions=tuple(holdout_exclusions(cells)),
                    off_study=tuple(off_study_symbols(cells)))


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #


def _n(value: Any, places: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{places}f}{suffix}"
    return f"{value:,}{suffix}" if isinstance(value, int) else str(value)


def render_provenance(provenance: Optional[Provenance] = None) -> List[str]:
    """The verdict block's header: the rule, and the SHA that froze it (T-14)."""
    provenance = provenance if provenance is not None else tool_provenance()
    return [
        "THE RULE, PRE-REGISTERED AND FROZEN AT THIS COMMIT",
        f"  tool commit     : {provenance.head}",
        f"  tool blob       : {provenance.blob}  ({provenance.path}; the exact "
        f"bytes — `git cat-file -p <blob>`)",
        "  working tree    : CLEAN for this file (checked; the tool refuses to "
        "run otherwise)",
        f"  TIE_BREAK       : {TIE_BREAK}  (so the default outcome is "
        f"{DEFAULT_RESOLUTION})",
        f"  MIN_EFFECT_PP   : {MIN_EFFECT_PP}",
        f"  MIN_SIGN_COUNT  : {dict(sorted(MIN_SIGN_COUNT.items()))}",
        f"  STUDY_SYMBOLS   : {', '.join(sorted(STUDY_SYMBOLS))}",
        f"  OOS_MIN_M       : {OOS_MIN_M}   FIT_WINDOW_DAYS: "
        f"{FIT_WINDOW_DAYS}   STARTING_CASH: {STARTING_CASH:,.0f}",
        "  These are module constants. No command-line argument can change "
        "them;",
        "  a different rule is a different commit and a different SHA above.",
    ]


def render_reads(analysis: Analysis) -> List[str]:
    lines: List[str] = []
    for name, base_arm, arm in READS:
        summary = analysis.reads.get(name)
        if summary is None:
            continue
        lines.append("")
        lines.append(f"READ `{name}`  ({base_arm} vs {arm}, fit split)")
        lines.append("  | symbol | base ann | arm ann | delta (pp) |")
        lines.append("  |---|---:|---:|---:|")
        by_symbol = dict(summary.deltas)
        for symbol, base, cell in contrast_rows(analysis.cells, base_arm, arm,
                                                "fit"):
            lines.append(
                f"  | {symbol} | {base.annualized_return * 100:,.2f}% | "
                f"{cell.annualized_return * 100:,.2f}% | "
                f"{by_symbol.get(symbol, float('nan')):+.2f} |")
        lines.append(
            f"  M={summary.m}  N+={summary.n_pos}  N-={summary.n_neg}  "
            f"N0={summary.n_zero}  median={_n(summary.median)} pp  "
            f"sigma={_n(summary.sigma)} pp")
        row = null_rate_row(summary.sigma)
        if row:
            lines.append(
                f"  null base rate at sigma ~ {row[0]} pp: sign only "
                f"{row[1]}% i.i.d. / {row[3]}% at rho=0.5; sign AND median "
                f"{row[2]}% / {row[4]}%")
        binom = SIGN_ONLY_BINOMIAL.get(summary.m)
        if binom is not None:
            lines.append(f"  exact binomial, sign only, M={summary.m}: {binom}%")
    return lines


def render_economics(cells: Sequence[Cell]) -> List[str]:
    """DD-1's reported-but-non-deciding set, per arm. All of it, always."""
    cells = dedupe_cells(cells)
    lines = ["", "PER-ARM P&L AND ROLL ECONOMICS (reported, non-deciding)",
             "  (deduped by (arm, symbol, split): two pins in one read both "
             "carry `base`)",
             "  | arm | split | symbol | verdict | err | ann | option_pnl | "
             "stock_realized | stock_unrealized |",
             "  |---|---|---|---|---|---:|---:|---:|---:|"]
    for cell in sorted(cells, key=lambda c: (c.scenario_name, c.split, c.symbol)):
        ann = ("-" if cell.annualized_return is None
               else f"{cell.annualized_return * 100:,.2f}%")
        lines.append(
            f"  | {cell.scenario_name} | {cell.split} | {cell.symbol} | "
            f"{cell.verdict or '-'} | {'ERR' if cell.error else '-'} | {ann} | "
            f"{_n(cell.option_pnl)} | "
            f"{_n(cell.stock_pnl_realized)} | {_n(cell.stock_pnl_unrealized)} |")
    lines += ["", "  | arm | split | symbol | rolls (itm/otm) | net credit | "
              "itm credit | otm credit | failed BTC debit | resting/legs | mode |",
              "  |---|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for cell in sorted(cells, key=lambda c: (c.scenario_name, c.split, c.symbol)):
        legs = (cell.roll_legs_resting or 0) + (cell.roll_legs_marketable or 0)
        share = f"{cell.roll_legs_resting or 0}/{legs}"
        if legs:
            share += f" ({(cell.roll_legs_resting or 0) / legs:.1%})"
        lines.append(
            f"  | {cell.scenario_name} | {cell.split} | {cell.symbol} | "
            f"{_n(cell.rolls_executed)} ({_n(cell.itm_rolls)}/"
            f"{_n(cell.otm_roll_outs)}) | {_n(cell.roll_net_credit)} | "
            f"{_n(cell.itm_roll_credit)} | {_n(cell.otm_roll_out_credit)} | "
            f"{_n(cell.failed_roll_btc_debit)} | {share} | "
            f"{cell.roll_fill_mode or 'PRE-FC-116'} |")
    reasons = sorted({r for c in cells for r in c.roll_skips}
                     | set(REPORTED_SKIP_REASONS))
    lines += ["", "  roll skips by reason (`btc_quote_unavailable` is skip-only "
              "and so stays conservative on the arm that rolls more):",
              "  | arm | split | symbol | " + " | ".join(reasons) + " |",
              "  |---|---|---|" + "---:|" * len(reasons)]
    for cell in sorted(cells, key=lambda c: (c.scenario_name, c.split, c.symbol)):
        counts = " | ".join(str(cell.roll_skips.get(r, 0)) for r in reasons)
        lines.append(f"  | {cell.scenario_name} | {cell.split} | {cell.symbol} "
                     f"| {counts} |")
    return lines


def render_secondary(cells: Sequence[Cell]) -> List[str]:
    """The secondary contrasts and the controls — reported, never deciding."""
    cells = dedupe_cells(cells)
    lines = ["", "SECONDARY CONTRASTS vs base, fit split (reported, "
             "non-deciding)"]
    for metric in ("total_return",) + SECONDARY_METRIC_COLUMNS:
        for _name, base_arm, arm in READS:
            pairs = contrast_rows(cells, base_arm, arm, "fit")
            values = [finite(getattr(a, metric)) - finite(getattr(b, metric))
                      for _s, b, a in pairs
                      if finite(getattr(a, metric)) is not None
                      and finite(getattr(b, metric)) is not None]
            if values:
                lines.append(f"  {metric:<24} {arm:<14} median delta "
                             f"{median(values):+.4f} (n={len(values)})")
    for control in ("noroll", "t099"):
        pairs = contrast_rows(cells, BASE_ARM, control, "fit")
        if pairs:
            summary = read_summary(control, pairs)
            lines.append(
                f"  CONTROL {control:<8} median delta ann "
                f"{_n(summary.median)} pp (M={summary.m}, N+={summary.n_pos}, "
                f"N-={summary.n_neg})")
    otm: Dict[str, int] = {}
    for c in cells:
        if c.split == "fit" and c.scenario_name in (BASE_ARM, "t099", "t100"):
            otm[c.scenario_name] = otm.get(c.scenario_name, 0) + (
                c.otm_roll_outs or 0)
    if len(otm) >= 2:
        shape = " >= ".join(f"{k}:{otm[k]}" for k in (BASE_ARM, "t099", "t100")
                            if k in otm)
        monotone = (otm.get(BASE_ARM, 0) >= otm.get("t099", 0)
                    >= otm.get("t100", 0))
        lines.append(f"  dose-response OTM roll-outs (summed over the printed "
                     f"cells): {shape} -> "
                     f"{'monotone as expected' if monotone else 'NOT MONOTONE - a FINDING, not a VOID (DD-7)'}")
    return lines


def render_holdout_and_oos(analysis: Analysis) -> List[str]:
    holdout = analysis.holdout
    lines = ["", "HOLDOUT (DD-1 condition 3)",
             f"  sign agreement {holdout.agreeing}/{holdout.comparable}"
             + (f" = {holdout.share:.0%}" if holdout.share is not None else "")]
    if not holdout.informative:
        lines.append(
            f"  comparable = {holdout.comparable} < {MIN_INFORMATIVE_COMPARABLE}"
            " -> the holdout is UNINFORMATIVE: stated, and it does not block.")
    else:
        lines.append(f"  comparable >= {MIN_INFORMATIVE_COMPARABLE}: the "
                     f"holdout can refute (floor "
                     f"{HOLDOUT_AGREEMENT_FLOOR:.0%}), for a KEEP and for a "
                     f"MOVE alike (R-b).")
    lines.append("  excluded from the holdout line, and why (computed from "
                 "these rows, not remembered):")
    for line in (analysis.holdout_exclusions or ["    (none)"]):
        lines.append(f"    {line}" if not line.startswith("    ") else line)
    secondary = analysis.holdout_total_return
    if secondary is not None:
        lines.append(
            f"  SECONDARY (reported, NEVER deciding): `total_return` sign "
            f"agreement over all NON-ERRORED holdout cells "
            f"{secondary.agreeing}/{secondary.comparable}"
            + (f" = {secondary.share:.0%}" if secondary.share is not None
               else "")
            + " — wider than the primary line, which drops every cell the "
              "engine did not call `measured`.")
    lines += ["", "OUT-OF-SAMPLE (DD-1 condition 4, DD-10)"]
    if analysis.oos is None:
        lines.append("  no OOS read supplied -> condition 4 is VACUOUS and the "
                     "record must be labelled single-window.")
    else:
        oos = analysis.oos
        lines.append(f"  M={oos.m}  N+={oos.n_pos}  N-={oos.n_neg}  "
                     f"median={_n(oos.median)} pp  sigma={_n(oos.sigma)} pp")
        if oos.m < OOS_MIN_M:
            lines.append(
                f"  M < OOS_MIN_M ({OOS_MIN_M}) -> the OOS read is "
                "UNINFORMATIVE: reported, and it cannot refute (R-c).")
        else:
            lines.append("  refutes a KEEP iff median >= "
                         f"+{MIN_EFFECT_PP} pp: "
                         f"{'YES' if _crosses(oos, 1) else 'no'}")
    return lines


def render_structural(checks: Sequence[StructuralCheck]) -> List[str]:
    lines = ["", "STRUCTURAL CHECKS (any FAIL is a VOID - file it, and do not "
             "read further)"]
    for check in checks:
        lines.append(f"  {'PASS' if check.passed else 'FAIL'}  {check.name}"
                     + (f" - {check.detail}" if check.detail else ""))
    return lines


def render_artifacts(bundle: Dict[str, Any]) -> List[str]:
    """The artifact-only lines: resting by kind, the DTE tell, the pairs."""
    lines = ["", "ARTIFACTS (--artifacts)"]
    resting = bundle.get("resting_by_kind") or {}
    if resting:
        lines.append("  resting legs by roll kind, per arm "
                     "(always favours the roller; outside DD-3's bracket):")
        for arm in sorted(resting):
            for kind, (rest, total) in sorted(resting[arm].items()):
                share = f" ({rest / total:.1%})" if total else ""
                lines.append(f"    {arm:<14} {kind:<12} {rest}/{total}{share}")
    tell = bundle.get("replacement_dte") or {}
    if tell:
        lines.append(f"  replacement DTE at the ladder's edge "
                     f"(>= {REPLACEMENT_DTE_CUTOFF} DTE) - DD-2's tell for the "
                     f"residual truncation on CHAINED rolls:")
        for arm in sorted(tell):
            t = tell[arm]
            lines.append(f"    {arm:<14} at_cutoff={t['at_cutoff']}  "
                         f"below={t['below_cutoff']}  unknown={t['unknown']}")
    pairs = bundle.get("paired_events") or []
    lines.append(f"  paired events (base executed an otm_roll_out; t100, at "
                 f"1.00, could not): {len(pairs)}")
    if pairs:
        lines.append("  | day | symbol | base net credit | old -> new strike | "
                     "itm ratio | t100 rolled that day? | t100 outcome |")
        lines.append("  |---|---|---:|---|---:|---|---|")
        for row in pairs:
            same_day = row.get("t100_same_day_roll") or ""
            lines.append(
                f"  | {row['day']} | {row['symbol']} | "
                f"{_n(row['net_credit'])} | {_n(row['old_strike'])} -> "
                f"{_n(row['new_strike'])} | {_n(row['itm_ratio'], 4)} | "
                f"{('YES: ' + same_day) if same_day else 'no'} | "
                f"{row['t100_outcome']}"
                + (f" {row['t100_outcome_day']}" if row['t100_outcome_day']
                   else "") + " |")
        rolled_too = sum(1 for r in pairs if r.get("t100_same_day_roll"))
        if rolled_too:
            lines.append(
                f"  NOTE: on {rolled_too} of these {len(pairs)} days t100 "
                "executed a roll of its own (an `itm_defence` at or above the "
                "strike). The pairing premise — t100 could not roll — does not "
                "hold for those rows; read them as 'a different roll', not "
                "'no roll'.")
    missing = bundle.get("missing") or []
    if missing:
        lines.append(f"  artifacts not found for {len(missing)} cells: "
                     + ", ".join(sorted(missing)[:12])
                     + (" ..." if len(missing) > 12 else ""))
    return lines


def render_report(analysis: Analysis, selection: Sequence[str],
                  artifacts: Optional[Dict[str, Any]] = None,
                  provenance: Optional[Provenance] = None) -> str:
    rule = "=" * 78
    lines = [rule,
             "FC-112 - the pre-registered roll-trigger read "
             "(wheel: itm_trigger_ratio 0.98 vs 1.00)",
             "Plan: docs/plans/fc-112.md   DD-1 the rule, DD-6 the selection",
             rule, ""]
    lines += render_provenance(provenance)
    lines += ["", "SELECTION (DD-6)"] + [f"  {s}" for s in selection]
    if analysis.off_study:
        lines.append(
            f"  symbols present but NOT in STUDY_SYMBOLS (reported, "
            f"non-deciding): {', '.join(analysis.off_study)}")
    lines += render_structural(analysis.structural)
    lines += render_reads(analysis)
    opt = analysis.opt_read
    lines += ["", "OPTION-LEG LOCATION (DD-1 condition 2, `limit` read)",
              f"  M={opt.m}  N+={opt.n_pos}  N-={opt.n_neg}  "
              f"median={_n(opt.median)} pp  sigma={_n(opt.sigma)} pp",
              f"  the option leg ALONE would pass KEEP: "
              f"{'yes' if _passes(opt, -1) else 'no'}",
              "  A KEEP carried by the stock legs is a call-strike / "
              "assignment-timing effect -",
              "  which is the roller's own claim, and must show up here if it "
              "is real."]
    lines += render_holdout_and_oos(analysis)
    lines += ["", "MONITOR (DD-4, fragility only)"]
    if analysis.monitor_windows >= 2:
        lines.append(f"  primary `{PRIMARY_READ}` median sign flip = "
                     f"{analysis.monitor_flip} "
                     f"(over {analysis.monitor_windows} windows)")
    else:
        lines.append(
            f"  VACUOUS ({analysis.monitor_windows} window"
            f"{'' if analysis.monitor_windows == 1 else 's'}) — a fragility "
            "check needs at least two points, so NO sign-flip claim is made. "
            "Run `--last 4` once four Saturdays exist (runbook step 6).")
    lines += render_economics(analysis.cells)
    lines += render_secondary(analysis.cells)
    if artifacts is not None:
        lines += render_artifacts(artifacts)
    lines += ["", rule, str(analysis.verdict), rule]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #

#: Substrings no command-line flag of this tool may ever contain. The rule is
#: pre-registered; a flag that names one of these is a flag that reopens it.
#: Asserted at parser-build time so the failure is loud and local rather than a
#: reviewer noticing it in a diff (T-14).
FORBIDDEN_FLAG_SUBSTRINGS = ("tie-break", "tie_break", "min-effect",
                             "min_effect", "sign-count", "sign_count",
                             "threshold", "keep-rule", "rule")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fc112_roll_trigger_read.py",
        description=("FC-112's pre-registered read. The decision rule is "
                     "frozen in module constants and CANNOT be changed by an "
                     "argument."),
        epilog=("There is deliberately no --tie-break, no --min-effect and no "
                "--min-sign-count: a rule that can be passed in is a rule that "
                "can be chosen after the numbers are seen."))
    p.add_argument("--pin-id", action="append", default=[], dest="pin_ids",
                   help="a pin to read (repeat: the decision and control pins)")
    p.add_argument("--run-id", action="append", default=[], dest="run_ids",
                   help="a one-shot run to read (repeat)")
    p.add_argument("--oos-run-id", default=None,
                   help="the DD-10 prior-year out-of-sample run (optional)")
    p.add_argument("--window-end", default=None,
                   help="YYYY-MM-DD on the SWEEP row (never the runs row: "
                        "scenario_runs.window_end is per split); default the "
                        "latest shared. Ignored in the query under --last N")
    p.add_argument("--last", type=int, default=1, metavar="N",
                   help="read the latest N windows as DD-4's fragility monitor")
    p.add_argument("--artifacts", action="store_true",
                   help="also read GCS artifacts: resting-by-kind, the "
                        "replacement-DTE tell and the paired-event table")
    p.add_argument("--bucket", default=DEFAULT_ARTIFACT_BUCKET)
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--since", default=None,
                   help="YYYY-MM-DD lower bound on submitted_at "
                        "(default: 400 days before today)")
    p.add_argument("--json-out", default=None,
                   help="also write the machine-readable summary here")
    for action in p._actions:  # noqa: SLF001 - the guard is the point
        for option in action.option_strings:
            for banned in FORBIDDEN_FLAG_SUBSTRINGS:
                assert banned not in option, (
                    f"{option} would make the pre-registered rule an argument")
    return p


def _default_since() -> str:
    return date.fromordinal(date.today().toordinal() - 400).isoformat()


def _group_by_window(cells: Sequence[Cell]) -> Dict[str, List[Cell]]:
    out: Dict[str, List[Cell]] = {}
    for cell in cells:
        out.setdefault(str(cell.window_end), []).append(cell)
    return out


def _collect_artifacts(storage_client: Any, bucket: str,
                       cells: Sequence[Cell]) -> Dict[str, Any]:
    loaded: Dict[Tuple[str, str], Dict[str, Any]] = {}
    missing: List[str] = []
    for cell in cells:
        if cell.split != "fit" or not cell.run_id:
            continue
        art = load_artifact(storage_client, bucket, cell.run_id,
                            cell.scenario_name, cell.symbol, cell.split)
        if art is None:
            missing.append(f"{cell.scenario_name}/{cell.symbol}")
        else:
            loaded[(cell.scenario_name, cell.symbol)] = art
    resting: Dict[str, Dict[str, List[int]]] = {}
    tell: Dict[str, Dict[str, int]] = {}
    for (arm, _symbol), art in loaded.items():
        for kind, (rest, total) in resting_by_kind(art).items():
            bucket_ = resting.setdefault(arm, {}).setdefault(kind, [0, 0])
            bucket_[0] += rest
            bucket_[1] += total
        for key, value in replacement_dte_tell(art).items():
            tell.setdefault(arm, {"at_cutoff": 0, "below_cutoff": 0,
                                  "unknown": 0})[key] += value
    pairs: List[Dict[str, Any]] = []
    for (arm, symbol), art in sorted(loaded.items()):
        if arm != BASE_ARM:
            continue
        other = loaded.get(("t100", symbol))
        if other is not None:
            pairs.extend(paired_events(art, other))
    return {
        "resting_by_kind": {a: {k: (v[0], v[1]) for k, v in kinds.items()}
                            for a, kinds in resting.items()},
        "replacement_dte": tell,
        "paired_events": sorted(pairs, key=lambda r: (r["day"], r["symbol"])),
        "missing": missing,
    }


def summary_json(analysis: Analysis, provenance: Provenance,
                 selection: Sequence[str] = ()) -> Dict[str, Any]:
    return {
        "tool_commit": provenance.head,
        "tool_blob": provenance.blob,
        "tool_path": provenance.path,
        # The machine-readable summary must answer "what did this verdict
        # read?" without the reader going back to the terminal scrollback:
        # a verdict divorced from its selection is not reproducible.
        "selection": list(selection),
        "constants": {"TIE_BREAK": TIE_BREAK, "MIN_EFFECT_PP": MIN_EFFECT_PP,
                      "MIN_SIGN_COUNT": {str(k): v
                                         for k, v in MIN_SIGN_COUNT.items()},
                      "STUDY_SYMBOLS": sorted(STUDY_SYMBOLS),
                      "OOS_MIN_M": OOS_MIN_M,
                      "FIT_WINDOW_DAYS": FIT_WINDOW_DAYS,
                      "STARTING_CASH": STARTING_CASH},
        "verdict": analysis.verdict.label,
        "resolves_to": analysis.verdict.resolves_to,
        "reasons": list(analysis.verdict.reasons),
        "reads": {name: {"M": s.m, "n_pos": s.n_pos, "n_neg": s.n_neg,
                         "n_zero": s.n_zero, "median_pp": s.median,
                         "sigma_pp": s.sigma, "deltas_pp": dict(s.deltas)}
                  for name, s in analysis.reads.items()},
        "option_leg": {"M": analysis.opt_read.m,
                       "median_pp": analysis.opt_read.median,
                       "passes": _passes(analysis.opt_read, -1)},
        "holdout": {"agreeing": analysis.holdout.agreeing,
                    "comparable": analysis.holdout.comparable,
                    "informative": analysis.holdout.informative,
                    "exclusions": list(analysis.holdout_exclusions)},
        "holdout_total_return": None if analysis.holdout_total_return is None
        else {"agreeing": analysis.holdout_total_return.agreeing,
              "comparable": analysis.holdout_total_return.comparable},
        "oos": None if analysis.oos is None else {
            "M": analysis.oos.m, "median_pp": analysis.oos.median,
            "informative": analysis.oos.m >= OOS_MIN_M},
        "off_study_symbols": list(analysis.off_study),
        "monitor_sign_flip": analysis.monitor_flip,
        "monitor_windows": analysis.monitor_windows,
        "structural": {c.name: c.passed for c in analysis.structural},
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.pin_ids) == bool(args.run_ids):
        print("give either --pin-id (one or more) or --run-id, not both",
              file=sys.stderr)
        return 2

    # Provenance FIRST, before a single byte is read: a run that cannot name
    # its own rule must not produce a verdict at all (Q-4 / D-3).
    try:
        provenance = tool_provenance()
    except ProvenanceError as exc:
        print(f"REFUSING TO RUN: {exc}", file=sys.stderr)
        return 4

    from google.cloud import bigquery

    since = args.since or _default_since()
    dataset_ref = f"{args.project}.{args.dataset}"
    client = bigquery.Client(project=args.project)
    selection = [f"dataset {dataset_ref}", f"submitted_at >= {since}",
                 f"tool commit {provenance.head}",
                 f"tool blob {provenance.blob} ({provenance.path})"]

    # One named window is pushed into the QUERY (on the sweep row). `--last N`
    # deliberately is not: the monitor reads every window in one round trip.
    one_window = args.window_end if args.last <= 1 else None
    tail = WINDOW_CLAUSE if one_window else ""

    standing: List[Cell] = []
    if args.pin_ids:
        params = _params(since, pin_ids=args.pin_ids, window_end=one_window)
        sweeps = fetch_sweeps(client, dataset_ref, SELECT_PINS + tail, params)
        cells = fetch_cells(client, dataset_ref, SELECT_PINS + tail, params)
        standing = fetch_cells(client, dataset_ref, SELECT_STANDING + tail,
                               _params(since, window_end=one_window))
        selection.append(f"pins {', '.join(args.pin_ids)}")
    else:
        params = _params(since, run_ids=args.run_ids, window_end=one_window)
        sweeps = fetch_sweeps(client, dataset_ref, SELECT_RUN_IDS + tail,
                              params)
        # D-4: select the cells by the SOURCE run id. Resolving first and
        # selecting on the target returns a deduped pair TWICE — once through
        # the `deduplicated` row and once through the target's own row.
        cells = fetch_cells(client, dataset_ref, SELECT_RUN_IDS + tail, params)
        selection.append(f"runs {', '.join(args.run_ids)} (selected by SOURCE "
                         f"run id; dedup pointers followed in-query)")

    by_window = _group_by_window(cells)
    windows = sorted(by_window, reverse=True)
    if args.window_end:
        windows = [w for w in windows if w == args.window_end]
    if not windows:
        print("no rows for the requested selection/window", file=sys.stderr)
        return 3
    target = windows[0]
    monitor_windows = windows[:max(1, args.last)]
    selection.append(f"window_end {target} (of {len(by_window)} read; "
                     f"monitor over {len(monitor_windows)})")

    # D-1: `resolve_dedup` sees ONLY the windows this read reads. Unscoped, a
    # single broken point from months ago would VOID every later read of a
    # rolling pin for ever.
    scoped = scope_sweeps(sweeps, monitor_windows)
    _mapping, dedup_voids = resolve_dedup(scoped)
    selection.append(f"sweep rows in scope: {len(scoped)} of {len(sweeps)} "
                     f"read (dedup resolution is scoped to the windows above)")

    monitor_medians = [
        read_summary(PRIMARY_READ,
                     contrast_rows(by_window[w], BASE_ARM, "t100", "fit")).median
        for w in monitor_windows]

    oos_cells: List[Cell] = []
    if args.oos_run_id:
        oos_cells = fetch_cells(client, dataset_ref, SELECT_RUN_IDS,
                                _params(since, run_ids=[args.oos_run_id]))
        selection.append(f"oos run {args.oos_run_id}")

    analysis = analyse(by_window[target],
                       [c for c in standing if str(c.window_end) == target],
                       oos_cells=oos_cells, monitor_medians=monitor_medians,
                       dedup_voids=dedup_voids)

    artifacts = None
    if args.artifacts:
        from google.cloud import storage

        artifacts = _collect_artifacts(storage.Client(project=args.project),
                                       args.bucket, by_window[target])

    selection += ["sweep rows read:"] + [f"  {line}" for line
                                        in sweep_status_lines(scoped)]
    print(render_report(analysis, selection, artifacts, provenance))
    if args.json_out:
        payload = summary_json(analysis, provenance, selection)
        if artifacts is not None:
            payload["artifacts"] = artifacts
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
    return 1 if analysis.verdict.is_void else 0


if __name__ == "__main__":
    raise SystemExit(main())
