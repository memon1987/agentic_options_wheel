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
the verdict block prints the SHA of the tree this tool ran from, so a record
that quotes a verdict also names the rule that produced it.

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
    """
    secondary = ", ".join(f"r.{c}" for c in SECONDARY_METRIC_COLUMNS)
    return f"""{_ctes(dataset_ref)}
SELECT s.pin_id, s.window_end, s.engine_version, s.engine_identity,
       s.submitted_via, s.run_id,
       r.symbol, r.split, r.scenario_name, r.verdict, r.measured, r.annualized_return,
       r.total_return,
       r.option_pnl, r.stock_pnl_realized, r.stock_pnl_unrealized,
       r.rolls_executed, r.itm_rolls, r.otm_roll_outs, r.roll_net_credit, r.itm_roll_credit,
       r.otm_roll_out_credit, r.failed_roll_btc_debit, r.roll_legs_resting, r.roll_legs_marketable,
       r.roll_skips, r.roll_fill_mode, {secondary}
FROM resolved s JOIN `{dataset_ref}.scenario_runs` r USING (run_id)
WHERE s.status = 'done' AND {selector}
ORDER BY r.symbol, r.split, r.scenario_name"""


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
SELECT_RUN_IDS = "{a}.run_id IN UNNEST(@run_ids)"
WINDOW_CLAUSE = " AND {a}.window_end = @window_end"


# --------------------------------------------------------------------------- #
# The row shapes.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Cell:
    """One `scenario_runs` row: scenario x symbol x split."""

    run_id: str = ""
    pin_id: Optional[str] = None
    window_end: Optional[str] = None
    engine_version: Optional[str] = None
    engine_identity: Optional[str] = None
    submitted_via: Optional[str] = None
    symbol: str = ""
    split: str = ""
    scenario_name: str = ""
    verdict: Optional[str] = None
    measured: bool = False
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
        window_end = d.get("window_end")
        if isinstance(window_end, (date, datetime)):
            window_end = window_end.isoformat()[:10]
        known = {f for f in cls.__dataclass_fields__}  # noqa: F821
        kwargs = {k: v for k, v in d.items() if k in known}
        kwargs["roll_skips"] = {str(k): int(v) for k, v in skips.items()}
        kwargs["window_end"] = window_end
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

        Today the wheel's holdout measures three cells; AMZN and GOOGL rolled
        seven times each in it and are `insufficient`. A rule that let n = 3
        block would be deciding on noise.
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


def index_cells(cells: Iterable[Cell]) -> Dict[Tuple[str, str, str], Cell]:
    return {c.key: c for c in cells}


def contrast_rows(
    cells: Iterable[Cell], base_arm: str, arm: str, split: str = "fit"
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
    """
    index = index_cells(cells)
    symbols = sorted({c.symbol for c in index.values()})
    out: List[Tuple[str, Cell, Cell]] = []
    for symbol in symbols:
        b = index.get((base_arm, symbol, split))
        a = index.get((arm, symbol, split))
        if b is None or a is None or not b.measured or not a.measured:
            continue
        if b.annualized_return is None or a.annualized_return is None:
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
    if base.option_pnl is None or arm.option_pnl is None:
        return None
    scaled = (arm.option_pnl - base.option_pnl) * (
        ANNUALISATION_DAYS / FIT_WINDOW_DAYS) / STARTING_CASH
    return scaled * 100.0


def read_summary(name: str, pairs: Sequence[Tuple[str, Cell, Cell]],
                 value=delta_ann_pp) -> ReadSummary:
    deltas = []
    for symbol, b, a in pairs:
        v = value(b, a)
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
    symbols = sorted({c.symbol for c in index.values()})
    agreeing = comparable = 0
    for symbol in symbols:
        quad = [index.get((arm, symbol, "fit")), index.get((arm, symbol, "holdout")),
                index.get((base_arm, symbol, "fit")),
                index.get((base_arm, symbol, "holdout"))]
        if any(c is None or not c.measured or c.annualized_return is None
               for c in quad):
            continue
        fit_delta = quad[0].annualized_return - quad[2].annualized_return
        hold_delta = quad[1].annualized_return - quad[3].annualized_return
        comparable += 1
        if (fit_delta > 0) == (hold_delta > 0) and (fit_delta < 0) == (hold_delta < 0):
            agreeing += 1
    return HoldoutLine(agreeing=agreeing, comparable=comparable)


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

    out.append(_placebo_check(cells))
    out.append(_pin_base_check(cells, standing))
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

    Two readings of DD-1's verdict table are written down here rather than left
    to the reader, because the plan's prose does not settle either:

    * **The holdout and the OOS read are KEEP conditions** (DD-1 cond. 3 and
      4), so they turn a would-be KEEP into MIXED-CONFLICT. They do not
      manufacture a conflict when nothing pointed at 0.98 in the first place: a
      null decision read plus an OOS leaning to 1.00 is two things agreeing,
      and MIXED-CONFLICT parks on the status quo (0.98).
    * **A KEEP refused only because the effect is not located in the option
      leg** (cond. 2) is not in DD-1's MIXED-CONFLICT list and is not
      MIXED-NULL's "no read crosses either threshold" either. It resolves to
      the DEFAULT — which is what one-sidedness means — and the reason is
      printed beside the verdict rather than swallowed.
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

    keeps = {n: _passes(s, -1) for n, s in reads.items()}
    moves = {n: _passes(s, +1) for n, s in reads.items()}

    conflicts: List[str] = []
    if any(keeps.values()) and any(_crosses(s, +1) for s in reads.values()):
        conflicts.append("read_sign_conflict:a read passes KEEP while another "
                         "crosses the +MIN_EFFECT_PP threshold")
    if any(moves.values()) and any(_crosses(s, -1) for s in reads.values()):
        conflicts.append("read_sign_conflict:a read passes MOVE while another "
                         "crosses the -MIN_EFFECT_PP threshold")
    if monitor_flip:
        conflicts.append("monitor_sign_flip:the primary `limit` median changed "
                         "sign across the monitor points")
    if conflicts:
        return Verdict("MIXED-CONFLICT", "0.98", tuple(conflicts))

    if all(keeps.values()):
        opt_located = _passes(opt_read, -1)
        if holdout.refutes:
            return Verdict("MIXED-CONFLICT", "0.98",
                           (f"holdout_refutes:{holdout.agreeing}/"
                            f"{holdout.comparable} sign agreement",))
        if oos is not None and _crosses(oos, +1):
            return Verdict("MIXED-CONFLICT", "0.98",
                           (f"oos_refutes:median {oos.median:+.2f} pp",))
        if not opt_located:
            return Verdict("MIXED-NULL", DEFAULT_RESOLUTION,
                           ("keep_refused_option_leg_not_located",))
        return Verdict("KEEP", "0.98", ())

    if all(moves.values()):
        return Verdict("MOVE", "1.00", ())

    crossed = sorted(n for n, s in reads.items()
                     if _crosses(s, +1) or _crosses(s, -1))
    reason = ("no_read_crosses_either_threshold" if not crossed
              else "partial:" + ",".join(crossed) + " crossed, no rule passed")
    return Verdict("MIXED-NULL", DEFAULT_RESOLUTION, (reason,))


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
    """The battery's ledger key is `event_date`; older notes say `date`."""
    value = event.get("event_date") or event.get("date") or ""
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
    """
    rows: List[Dict[str, Any]] = []
    ledger = sorted((t100_artifact.get("ledger") or []), key=_ev_date)
    for record in base_artifact.get("roll_records") or []:
        if not record.get("success", True):
            continue
        if str(record.get("roll_kind")) != "otm_roll_out":
            continue
        day = str(record.get("day"))[:10]
        underlying = str(record.get("underlying") or "")
        held: Optional[str] = None
        for event in ledger:
            if (str(event.get("kind")) == "sell_call_open"
                    and _ev_underlying(event) == underlying
                    and _ev_date(event) <= day):
                held = str(event.get("symbol") or "")
        outcome = ("no open t100 call found" if held is None
                   else "still open at window end")
        outcome_day = ""
        if held is not None:
            for event in ledger:
                if (str(event.get("symbol")) == held
                        and str(event.get("kind")) in _TERMINAL_CALL_KINDS
                        and _ev_date(event) >= day):
                    outcome = str(event.get("kind"))
                    outcome_day = _ev_date(event)
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
        })
    return rows


# --------------------------------------------------------------------------- #
# I/O. Read-only, and the only part of this module that touches a cloud.
# --------------------------------------------------------------------------- #

SHA_UNAVAILABLE = "unavailable (no git)"


def tool_commit_sha() -> str:
    """The 40-hex SHA of the tree this tool ran from.

    Printed in the verdict block beside the constants, because a verdict is
    only pre-registered if the record can name the rule that produced it. A
    quoted verdict with no SHA is a number with no rule behind it.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run(["git", "-C", here, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return SHA_UNAVAILABLE
    sha = (out.stdout or "").strip()
    return sha if out.returncode == 0 and len(sha) == 40 else SHA_UNAVAILABLE


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
    sql = _cell_sql(dataset_ref, selector.format(a="s"))
    return [Cell.from_row(r) for r in _rows(client, sql, params)]


def fetch_sweeps(client: Any, dataset_ref: str, selector: str,
                 params: Sequence[Any]) -> List[Dict[str, Any]]:
    sql = _sweep_sql(dataset_ref, selector.format(a="l"))
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
    """
    signs = {(0 if m == 0 else (1 if m > 0 else -1))
             for m in medians if m is not None}
    return len(signs) > 1


@dataclass
class Analysis:
    reads: Dict[str, ReadSummary]
    opt_read: ReadSummary
    holdout: HoldoutLine
    structural: List[StructuralCheck]
    verdict: Verdict
    oos: Optional[ReadSummary] = None
    monitor_flip: bool = False
    cells: Sequence[Cell] = ()
    standing: Sequence[Cell] = ()


def analyse(cells: Sequence[Cell],
            standing: Optional[Sequence[Cell]] = None,
            oos_cells: Optional[Sequence[Cell]] = None,
            monitor_medians: Sequence[Optional[float]] = (),
            dedup_voids: Sequence[str] = ()) -> Analysis:
    """Rows in, verdict out. Pure: no clock, no cloud, no arguments to the rule."""
    structural = list(structural_checks(cells, standing))
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
                    monitor_flip=flip, cells=tuple(cells),
                    standing=tuple(standing or ()))


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #


def _n(value: Any, places: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{places}f}{suffix}"
    return f"{value:,}{suffix}" if isinstance(value, int) else str(value)


def render_provenance(sha: Optional[str] = None) -> List[str]:
    """The verdict block's header: the rule, and the SHA that froze it (T-14)."""
    sha = sha if sha is not None else tool_commit_sha()
    return [
        "THE RULE, PRE-REGISTERED AND FROZEN AT THIS COMMIT",
        f"  tool commit     : {sha}",
        f"  TIE_BREAK       : {TIE_BREAK}  (so the default outcome is "
        f"{DEFAULT_RESOLUTION})",
        f"  MIN_EFFECT_PP   : {MIN_EFFECT_PP}",
        f"  MIN_SIGN_COUNT  : {dict(sorted(MIN_SIGN_COUNT.items()))}",
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
    lines = ["", "PER-ARM P&L AND ROLL ECONOMICS (reported, non-deciding)",
             "  | arm | split | symbol | verdict | ann | option_pnl | "
             "stock_realized | stock_unrealized |",
             "  |---|---|---|---|---:|---:|---:|---:|"]
    for cell in sorted(cells, key=lambda c: (c.scenario_name, c.split, c.symbol)):
        ann = ("-" if cell.annualized_return is None
               else f"{cell.annualized_return * 100:,.2f}%")
        lines.append(
            f"  | {cell.scenario_name} | {cell.split} | {cell.symbol} | "
            f"{cell.verdict or '-'} | {ann} | {_n(cell.option_pnl)} | "
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
    lines = ["", "SECONDARY CONTRASTS vs base, fit split (reported, "
             "non-deciding)"]
    for metric in ("total_return",) + SECONDARY_METRIC_COLUMNS:
        for _name, base_arm, arm in READS:
            pairs = contrast_rows(cells, base_arm, arm, "fit")
            values = [(getattr(a, metric) - getattr(b, metric))
                      for _s, b, a in pairs
                      if getattr(a, metric) is not None
                      and getattr(b, metric) is not None]
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
        lines.append(
            "  Say it in the record either way: AMZN and GOOGL rolled seven "
            "times each in the 09-11 holdout and are `insufficient`, so they "
            "carry no number to take a sign from.")
    else:
        lines.append(f"  comparable >= {MIN_INFORMATIVE_COMPARABLE}: the "
                     f"holdout can refute (floor "
                     f"{HOLDOUT_AGREEMENT_FLOOR:.0%}).")
    lines += ["", "OUT-OF-SAMPLE (DD-1 condition 4, DD-10)"]
    if analysis.oos is None:
        lines.append("  no OOS read supplied -> condition 4 is VACUOUS and the "
                     "record must be labelled single-window.")
    else:
        oos = analysis.oos
        lines.append(f"  M={oos.m}  N+={oos.n_pos}  N-={oos.n_neg}  "
                     f"median={_n(oos.median)} pp  sigma={_n(oos.sigma)} pp")
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
                     "itm ratio | t100 outcome |")
        lines.append("  |---|---|---:|---|---:|---|")
        for row in pairs:
            lines.append(
                f"  | {row['day']} | {row['symbol']} | "
                f"{_n(row['net_credit'])} | {_n(row['old_strike'])} -> "
                f"{_n(row['new_strike'])} | {_n(row['itm_ratio'], 4)} | "
                f"{row['t100_outcome']}"
                + (f" {row['t100_outcome_day']}" if row['t100_outcome_day']
                   else "") + " |")
    missing = bundle.get("missing") or []
    if missing:
        lines.append(f"  artifacts not found for {len(missing)} cells: "
                     + ", ".join(sorted(missing)[:12])
                     + (" ..." if len(missing) > 12 else ""))
    return lines


def render_report(analysis: Analysis, selection: Sequence[str],
                  artifacts: Optional[Dict[str, Any]] = None,
                  sha: Optional[str] = None) -> str:
    rule = "=" * 78
    lines = [rule,
             "FC-112 - the pre-registered roll-trigger read "
             "(wheel: itm_trigger_ratio 0.98 vs 1.00)",
             "Plan: docs/plans/fc-112.md   DD-1 the rule, DD-6 the selection",
             rule, ""]
    lines += render_provenance(sha)
    lines += ["", "SELECTION (DD-6)"] + [f"  {s}" for s in selection]
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
    lines += ["", f"MONITOR (DD-4, fragility only): primary `{PRIMARY_READ}` "
              f"median sign flip = {analysis.monitor_flip}"]
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
                   help="YYYY-MM-DD on the SWEEP row; default the latest shared")
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


def summary_json(analysis: Analysis, sha: str) -> Dict[str, Any]:
    return {
        "tool_commit": sha,
        "constants": {"TIE_BREAK": TIE_BREAK, "MIN_EFFECT_PP": MIN_EFFECT_PP,
                      "MIN_SIGN_COUNT": {str(k): v
                                         for k, v in MIN_SIGN_COUNT.items()}},
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
                    "informative": analysis.holdout.informative},
        "oos": None if analysis.oos is None else {
            "M": analysis.oos.m, "median_pp": analysis.oos.median},
        "monitor_sign_flip": analysis.monitor_flip,
        "structural": {c.name: c.passed for c in analysis.structural},
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.pin_ids) == bool(args.run_ids):
        print("give either --pin-id (one or more) or --run-id, not both",
              file=sys.stderr)
        return 2

    from google.cloud import bigquery

    sha = tool_commit_sha()
    since = args.since or _default_since()
    dataset_ref = f"{args.project}.{args.dataset}"
    client = bigquery.Client(project=args.project)
    selection = [f"dataset {dataset_ref}", f"submitted_at >= {since}",
                 f"tool commit {sha}"]

    standing: List[Cell] = []
    if args.pin_ids:
        params = _params(since, pin_ids=args.pin_ids)
        sweeps = fetch_sweeps(client, dataset_ref, SELECT_PINS, params)
        _mapping, dedup_voids = resolve_dedup(sweeps)
        cells = fetch_cells(client, dataset_ref, SELECT_PINS, params)
        standing = fetch_cells(client, dataset_ref, SELECT_STANDING,
                               _params(since))
        selection.append(f"pins {', '.join(args.pin_ids)}")
    else:
        params = _params(since, run_ids=args.run_ids)
        sweeps = fetch_sweeps(client, dataset_ref, SELECT_RUN_IDS, params)
        mapping, dedup_voids = resolve_dedup(sweeps)
        resolved = sorted({mapping.get(r, r) for r in args.run_ids})
        cells = fetch_cells(client, dataset_ref, SELECT_RUN_IDS,
                            _params(since, run_ids=resolved))
        selection.append(f"runs {', '.join(args.run_ids)} -> "
                         f"{', '.join(resolved)}")

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

    print(render_report(analysis, selection, artifacts, sha))
    if args.json_out:
        payload = summary_json(analysis, sha)
        if artifacts is not None:
            payload["artifacts"] = artifacts
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
    return 1 if analysis.verdict.is_void else 0


if __name__ == "__main__":
    raise SystemExit(main())
