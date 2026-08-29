"""Sweep identity — the hashes that decide whether two sweeps are the same run.

**Stdlib only, and deliberately so.** This module is copied verbatim into the
dashboard image (see ``dashboard/Dockerfile``) alongside ``overrides.py``,
because the API and the Job must agree bit-for-bit on what a sweep IS. The
dashboard computes ``sweep_key`` before it launches anything so it can answer
"we already ran this" without replaying; the Job computes the same key and
stamps it on its ``scenario_sweeps`` row. Two implementations of that agreement
are two implementations of the dedup, and the failure mode of a disagreement is
silent: the dashboard launches a duplicate and both rows look fine.

Two hashes live here and they answer different questions:

* ``scenario_arm_hash(overrides, fill_haircut)`` — identity of ONE ARM.
  ``Scenario.scenario_hash`` delegates to it, so the runner's per-row
  ``scenario_hash`` and the dashboard's dedup input are literally the same
  bytes. ``config_hash`` cannot do this job: it hashes nine strategy keys plus
  the module scoring constants, so 12 of the 19 allowlisted override keys and
  the per-scenario fill haircut are outside it entirely.
* ``sweep_key(spec, ...)`` — identity of a WHOLE SUBMISSION: the canonical spec
  plus ``engine_version`` plus ``git_commit``. The last two are in the key on
  purpose. The same arms replayed by a different engine build are a different
  experiment, and returning the old rows for them would be the worst kind of
  cache hit — one that looks like a result.

**Canonicalisation is what makes the key mean "the same question", not "the
same JSON".** Symbols are upper-cased and sorted; scenarios are sorted by
``(name, arm hash)``; an explicitly-declared ``base`` arm carrying no overrides
and no haircut is dropped, because the runner prepends that arm anyway — a spec
that spells it out and one that leaves it implicit describe the identical run
and must not produce two keys.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional, Sequence

# The comparator arm. Duplicated from ``runner.BASE_SCENARIO_NAME`` rather than
# imported: importing ``runner`` would drag the whole engine in, and this module
# exists precisely to be importable without it. Pinned equal by a test.
BASE_SCENARIO_NAME = "base"

# The per-symbol notional a spec that does not say otherwise is run at. Same
# default as ``run_sweep`` and ``main.py --starting-cash``; pinned by a test,
# because a canonical spec that filled in a different default would key two
# identical submissions differently.
DEFAULT_STARTING_CASH = 100_000.0


def scenario_arm_hash(
    overrides: Optional[Mapping[str, Any]], fill_haircut: Optional[float]
) -> str:
    """Identity of one arm: its effective overrides plus its fill haircut.

    ``default=str`` is load-bearing rather than defensive — an override value
    may be any YAML/JSON scalar or list, and a date or Decimal arriving from a
    hand-written spec must hash rather than raise.
    """
    payload = json.dumps(
        {
            "overrides": {k: overrides[k] for k in sorted(overrides or {})},
            "fill_haircut": fill_haircut,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _is_implicit_base(entry: Mapping[str, Any]) -> bool:
    """A declared ``base`` arm that is identical to the implicit one."""
    return (
        str(entry.get("name")) == BASE_SCENARIO_NAME
        and not (entry.get("overrides") or {})
        and entry.get("fill_haircut") is None
    )


def canonical_spec(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """The normalised form ``sweep_key`` is taken over.

    Order-independent in every dimension a caller can reorder without changing
    the run: symbol order, scenario order, and override key order inside an arm
    (the last via ``scenario_arm_hash``'s sorted dump). Duplicated symbols
    collapse — the runner would materialise the symbol once either way.

    Only the arm's HASH is carried, not its overrides: two arms that differ only
    in their *name* are genuinely different submissions (the grid is keyed by
    name), so the name stays; the overrides are already fully described by the
    hash, and repeating them would make the key sensitive to key ordering again.
    """
    scenarios = [
        {
            "name": str(entry.get("name")),
            "hash": scenario_arm_hash(
                entry.get("overrides") or {}, entry.get("fill_haircut")
            ),
        }
        for entry in (spec.get("scenarios") or [])
        if not _is_implicit_base(entry)
    ]
    scenarios.sort(key=lambda s: (s["name"], s["hash"]))

    symbols: Sequence[Any] = spec.get("symbols") or []
    holdout = spec.get("holdout_start")
    cash = spec.get("starting_cash")
    return {
        "symbols": sorted({str(s).strip().upper() for s in symbols}),
        "start": str(spec.get("start") or ""),
        "end": str(spec.get("end") or ""),
        "holdout_start": str(holdout) if holdout else None,
        "starting_cash": float(DEFAULT_STARTING_CASH if cash is None else cash),
        "run_sensitivity": bool(spec.get("run_sensitivity", False)),
        "scenarios": scenarios,
    }


def sweep_key(
    spec: Mapping[str, Any], *, engine_version: str, git_commit: Optional[str]
) -> str:
    """sha256[:16] over the canonical spec, the engine version and the commit.

    A missing ``git_commit`` hashes as the empty string rather than raising: a
    locally-run sweep has no commit stamp, and refusing to key it would mean
    refusing to persist it. The consequence is stated in D4 — a dashboard and a
    Job on different commits simply miss each other's cache, which costs a
    replay and never returns a wrong answer.
    """
    payload = json.dumps(
        {
            "spec": canonical_spec(spec),
            "engine_version": engine_version,
            "git_commit": git_commit or "",
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
