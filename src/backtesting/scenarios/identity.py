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
  plus ``engine_version`` plus ``engine_identity``. The last two are in the key
  on purpose. The same arms replayed by a different engine build are a different
  experiment, and returning the old rows for them would be the worst kind of
  cache hit — one that looks like a result.

  **``engine_identity`` replaced ``git_commit`` in FC-096 Phase B.** The commit
  was sound and far too coarse: every merge to ``main`` invalidated every stored
  result, including the merges that cannot change a replay (a README edit, a
  dashboard tweak, a build flag). ``engine_identity`` is the content hash of
  ``src/**`` — see ``engine_identity.py`` — so it moves when, and only when, the
  code a replay executes moves. ``git_commit`` is still STORED on every row: it
  is provenance, it is just no longer identity. Callers pass the value in (this
  module stays stdlib-only and importable without walking a tree); the Job and
  the CLI compute it from ``engine_identity.engine_identity()``, and the
  dashboard image reads it out of the ``ENGINE_IDENTITY`` env baked in at build
  time.

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
import re
from typing import Any, Dict, Mapping, Optional, Sequence

# The comparator arm. Duplicated from ``runner.BASE_SCENARIO_NAME`` rather than
# imported: importing ``runner`` would drag the whole engine in, and this module
# exists precisely to be importable without it. Pinned equal by a test.
BASE_SCENARIO_NAME = "base"

# What a scenario NAME may be. Here rather than in either caller because both
# have to agree: the API refuses a name at the boundary, and the CLI/YAML path
# must refuse the same one — a `--persist` sweep that landed a 200-character name
# would put a column header three screens wide in the grid the API then serves,
# and the API would have refused to create it. One rule, imported by both.
#
# 40 characters is generous for a label and short enough that a pasted blob is
# refused rather than rendered. The pattern keeps names to things that are safe
# as a grid key, a report cell and an env-var payload.
MAX_SCENARIO_NAME_CHARS = 40
# `\Z`, not `$`. In Python `$` also matches immediately BEFORE a trailing
# newline, so `"tighter\n"` satisfied this pattern end to end: the API accepted
# the arm, the CLI accepted the YAML entry, and the name then travelled into an
# env var, a grid column header and — since FC-096 Phase B — a GCS object name,
# where the newline is a header-splitting character the client library rejects at
# serve time. A name is a single line by construction; `\Z` is how that is
# actually spelled.
SCENARIO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*\Z")

# The separator FC-096 Phase B's detail-artifact object names are built from:
# ``<run_id>/<scenario>__<symbol>__<split>.json.gz``, parsed back with
# ``rsplit("__", 2)``. A scenario name containing ``__`` would make that parse
# ambiguous — ``a__b__AAPL__all`` splits into ``("a__b", "AAPL", "all")`` only by
# luck of the rsplit direction, and ``a__b__c`` with a one-token symbol parses to
# a scenario nobody named. So the name rule forbids it, in the ONE place both
# sides read (this module is copied flat into the dashboard image), and the
# artifact writer never has to sanitise a name the validator already refused.
ARTIFACT_NAME_SEPARATOR = "__"

# Object-name prefix inside the artifacts bucket. Versioned like the chain
# lake's (``chain_store.DEFAULT_LAKE_PREFIX``): bump it when the artifact JSON
# schema changes incompatibly, so two generations of object can never be read as
# each other.
ARTIFACT_PREFIX = "sim-artifacts/v1"


def validate_scenario_name(name: str, where: str) -> None:
    """Raise ``ValueError`` unless ``name`` is a legal scenario name.

    ``where`` names the source in the message — a YAML path for the CLI, the
    literal ``'sweep spec'`` for the Job, the arm's position for the API.
    """
    if len(name) > MAX_SCENARIO_NAME_CHARS:
        raise ValueError(
            f"{where}: scenario name {name[:20]!r}... is {len(name)} characters; "
            f"the cap is {MAX_SCENARIO_NAME_CHARS}. Names key the grid and its "
            f"column headers, and they travel in the spec env var")
    if not SCENARIO_NAME_RE.match(name):
        raise ValueError(
            f"{where}: scenario name {name!r} must start alphanumeric and "
            f"contain only letters, digits, '_', '.' or '-' "
            f"(pattern {SCENARIO_NAME_RE.pattern})")
    if ARTIFACT_NAME_SEPARATOR in name:
        raise ValueError(
            f"{where}: scenario name {name!r} may not contain "
            f"{ARTIFACT_NAME_SEPARATOR!r}. It is the field separator in the "
            f"detail-artifact object name "
            f"(<run_id>/<scenario>__<symbol>__<split>.json.gz), which is parsed "
            f"back with rsplit('__', 2); a name carrying it would make that "
            f"parse silently wrong. A single underscore is fine.")


# What an UNDERLYING may be, for the same reason a scenario name is bounded: it
# becomes a field in the artifact object name. Real tickers carry dots and
# dashes (BRK.B, RDS-A); nothing legitimate carries `__`, a slash, a space or a
# newline. The dashboard's serve-side check (`services/artifacts.SYMBOL_RE`)
# is this rule, and the CLI applies it too — a hand-typed `--symbols` value
# reaching the writer is the one path that could otherwise create an object no
# reader can address.
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}\Z")


def validate_symbol(symbol: str, where: str) -> None:
    """Raise ``ValueError`` unless ``symbol`` can key an artifact object."""
    if not SYMBOL_RE.match(symbol or ""):
        raise ValueError(
            f"{where}: symbol {str(symbol)[:32]!r} must be an uppercase ticker "
            f"of at most 16 characters from [A-Z0-9.-] "
            f"(pattern {SYMBOL_RE.pattern}). Symbols key the per-cell detail "
            f"artifact's object name, so one carrying '__', a slash or "
            f"whitespace would write an object nothing can address.")


def artifact_object_name(run_id: str, scenario: str, symbol: str,
                         split: str) -> str:
    """The GCS object name for one cell's detail artifact (FC-096 Phase B B2).

    ``<prefix>/<run_id>/<scenario>__<symbol>__<split>.json.gz``. Here rather
    than in the engine's ``reporting/artifact.py`` for the reason the whole
    module exists: the dashboard serves these objects and ships no engine, so
    the writer and the reader have to agree bit-for-bit on the name. One
    function, imported by both — a second implementation would drift and its
    drift would present as a 404 on an artifact that exists.

    ``scenario`` is NOT sanitised: ``validate_scenario_name`` already refuses
    every character that could break the name, ``__`` included. Sanitising here
    would silently map two distinct arms onto one object.
    """
    return (f"{ARTIFACT_PREFIX}/{run_id}/{scenario}"
            f"{ARTIFACT_NAME_SEPARATOR}{symbol}"
            f"{ARTIFACT_NAME_SEPARATOR}{split}.json.gz")


def parse_artifact_object_name(name: str) -> Dict[str, str]:
    """``{run_id, scenario, symbol, split}`` from an artifact object name.

    The inverse of ``artifact_object_name``, and the only parser anything is
    allowed to use: ``rsplit(ARTIFACT_NAME_SEPARATOR, 2)`` from the RIGHT, so a
    scenario name is whatever is left over. That direction is what makes the
    ``__``-in-a-name rule sufficient rather than merely helpful.

    Raises ``ValueError`` on anything that is not one of these names.
    """
    stem = name
    if stem.endswith(".json.gz"):
        stem = stem[: -len(".json.gz")]
    else:
        raise ValueError(f"not an artifact object name (no .json.gz): {name!r}")
    head, _, tail = stem.rpartition("/")
    run_id = head.rsplit("/", 1)[-1] if head else ""
    parts = tail.rsplit(ARTIFACT_NAME_SEPARATOR, 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            f"not an artifact object name (expected "
            f"<scenario>__<symbol>__<split>.json.gz): {name!r}")
    scenario, symbol, split = parts
    return {"run_id": run_id, "scenario": scenario,
            "symbol": symbol, "split": split}


# The per-symbol notional a spec that does not say otherwise is run at. Same
# default as ``run_sweep`` and ``main.py --starting-cash``; pinned by a test,
# because a canonical spec that filled in a different default would key two
# identical submissions differently.
DEFAULT_STARTING_CASH = 100_000.0

# ``evaluate.DEFAULT_FILL_HAIRCUT``. Duplicated for the same reason as
# ``BASE_SCENARIO_NAME`` — importing ``evaluate`` would drag the engine in — and
# pinned equal by a test. It is here rather than in the caller because an arm
# that spells out the default haircut and one that omits it are the SAME arm:
# the runner substitutes this exact value for ``None``.
DEFAULT_FILL_HAIRCUT = 0.25

# Spec fields that describe HOW to run rather than WHAT to measure, and are
# therefore excluded from the identity. ``force`` is an operator's instruction to
# skip the dedup lookup; including it in the key would make a forced re-run
# produce a different key from the run it is deliberately reproducing, so the two
# would never be comparable and a second force would never dedup either.
NON_IDENTITY_FIELDS = frozenset({"force"})


def _canonical_number(value: Any) -> Any:
    """Fold numeric leaves so ``1000`` and ``1000.0`` hash the same.

    A price ceiling typed as an integer in a YAML file and as a float by a JSON
    form is the same threshold, and the engine coerces both to float before it
    compares anything. Without this fold the two spell different sweeps and the
    dedup never fires between the CLI and the dashboard — which is precisely the
    pair D4 exists to connect. Booleans are left alone: ``True`` is not ``1.0``
    here, and folding it would make ``earnings.enabled: true`` collide with an
    integer 1 in some other key.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        # -0.0 and 0.0 are the same threshold; json.dumps spells them
        # differently.
        return value + 0.0
    if isinstance(value, (list, tuple)):
        return [_canonical_number(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _canonical_number(v) for k, v in value.items()}
    return value


def scenario_arm_hash(
    overrides: Optional[Mapping[str, Any]], fill_haircut: Optional[float]
) -> str:
    """Identity of one arm: its effective overrides plus its fill haircut.

    Two normalisations, both because the same arm can be *spelled* two ways:

    * numeric leaves are folded to float (``1000`` == ``1000.0``), since the
      engine coerces before it compares and a YAML integer and a JSON float are
      the same threshold;
    * a ``fill_haircut`` equal to ``DEFAULT_FILL_HAIRCUT`` folds to ``None``,
      because the runner substitutes exactly that value for ``None`` — an arm
      that spells the default out and one that omits it run identically.

    ``default=str`` is load-bearing rather than defensive: an override value may
    be any YAML/JSON scalar or list, and a date arriving from a hand-written spec
    must hash rather than raise.
    """
    if fill_haircut is not None and float(fill_haircut) == DEFAULT_FILL_HAIRCUT:
        fill_haircut = None
    payload = json.dumps(
        {
            "overrides": {
                k: _canonical_number(overrides[k]) for k in sorted(overrides or {})
            },
            "fill_haircut": (None if fill_haircut is None
                             else _canonical_number(fill_haircut)),
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
    # `force` is deliberately absent — see NON_IDENTITY_FIELDS.


def sweep_key(
    spec: Mapping[str, Any], *, engine_version: str,
    engine_identity: Optional[str]
) -> str:
    """sha256[:16] over the canonical spec, the engine version and the engine hash.

    ``engine_identity`` is ``engine_identity.engine_identity()`` — the content
    hash of ``src/**``. It is a PARAMETER rather than a call, exactly as
    ``git_commit`` was before it, because this module is copied into the
    dashboard image, which ships no ``src/`` tree to hash: the dashboard reads
    the value from an env var baked in at image-build time from the same
    checkout, and the Job/CLI compute it directly.

    A missing ``engine_identity`` hashes as the empty string rather than
    raising: refusing to key a sweep would mean refusing to persist it. The
    consequence is the same one D4 states for a missing commit — two sides that
    disagree simply miss each other's cache, which costs a replay and never
    returns a wrong answer. The dashboard nonetheless refuses to *use* an empty
    value (it disables its dedup hint loudly instead), because a key computed
    over "" would collide across genuinely different engines.
    """
    payload = json.dumps(
        {
            "spec": canonical_spec(spec),
            "engine_version": engine_version,
            "engine_identity": engine_identity or "",
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
