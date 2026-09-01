"""Per-cell detail artifacts — the replay's evidence, not its scalars.

FC-096 Phase B B2. A sweep row is ~30 numbers; the replay that produced it held
a daily equity curve, a complete money/position ledger, the wheel cycles those
events reconstruct into, the roller's records and the rejection tally. All of it
was discarded after aggregation, so Phase E's console — "why did THIS arm on
THIS symbol give up 4% in March?" — could not be built on what the store keeps.

This module turns one ``SimulationResult`` into one JSON-able dict. It does not
compute anything the engine does not already compute: ``build_cycles`` is the
same function ``evaluate.py`` scores with, called on the same ledger, so a cycle
table rendered from an artifact and one rendered from a fresh replay cannot
disagree. Re-deriving cycles here would be a second implementation of the wheel's
state machine, and its drift would present as a console that quietly contradicts
the scorecard.

**Four honesty properties are the point of the provenance block**, and each
exists because its absence would let a reader draw a wrong conclusion from a
correct artifact:

* **The fill assumption is stamped.** A sweep row carries ``bid_fill_return``
  from a SECOND replay at the bid, and that replay deliberately gets no
  artifact. Without ``fill.basis``/``fill.fill_haircut`` on the object, a reader
  comparing the artifact's ledger against the row's bid number would be
  comparing two different runs and would have no way to know it.
* **The MASKED reach is stamped, not the sweep's.** Since FC-096 Phase A PR-2
  each arm replays against ``narrow_to_dte(materialised, arm_reach)``. The
  parent ``Materialised`` may reach 21 DTE because some OTHER arm asked for it;
  this arm saw 7 (+ the universe buffer). Stamping the parent's number would
  describe a chain this cell never had.
* **The rejection tally is complete and ranked.** Post-FC-092 the tally actually
  binds per run, so an empty ``rejections`` now means "nothing was blocked"
  rather than "the logger cached the first run's tally". It is serialised as an
  ORDERED LIST rather than a dict so the ranking — which reason bound the most
  days — survives a JSON round-trip through readers that do not preserve
  insertion order.
* **``shares_held`` is on every daily row.** Equity alone cannot distinguish a
  cash account from an assigned one holding the same dollar value, which is the
  single most important thing to know when reading a wheel drawdown.

``"schema": 1`` is in the JSON, and a frozen-fixture test pins the full key set
including the per-``kind`` ``LedgerEvent.detail`` keys. Adding a field is
additive and needs the fixture updated; removing or renaming one is a schema
bump.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from ..data.chain_builder import UNIVERSE_DTE_BUFFER
from ..engine.simulator import SimulationResult
from ..metrics.cycles import build_cycles

# The artifact JSON's own version. Bumped only for an incompatible change (a
# removed or renamed field); adding a field is additive and does not move it.
# Mirrored by the object-name prefix `identity.ARTIFACT_PREFIX`, which is what a
# reader keys off before it has parsed anything.
ARTIFACT_SCHEMA = 1

# The fill model the serialised replay used. Only ever `"mid"`: the bid replay
# (`evaluate.BID_FILL_HAIRCUT`) exists to produce one scalar for the row and is
# deliberately not serialised, so a stamp that could say `"bid"` would be
# describing an object that does not exist. Named rather than inlined so the
# test that pins the stamp and the code that writes it read the same constant.
MID_FILL_BASIS = "mid"


@dataclass(frozen=True)
class ArtifactMeta:
    """Everything about a cell that the ``SimulationResult`` does not know.

    A ``SimulationResult`` knows its symbols, window and numbers; it does not
    know which sweep it belonged to, which arm produced it, which engine build
    ran it, what fill assumption the caller chose, or what the chain view it was
    handed had been masked to. All of that is the caller's knowledge, so it is
    passed in rather than guessed at.

    Attributes:
        run_id / scenario / symbol / split: the cell's coordinates. The object
            name is built from all four (``identity.artifact_object_name``).
        scenario_hash / config_hash: the same two hashes on the ``scenario_runs``
            row, so an artifact can be joined back to its row.
        engine_identity: ``engine_identity.engine_identity()`` — the content hash
            of ``src/**``. Half the dedup key, and the answer to "was this
            produced by the code I am reading?".
        arm_max_dte: the reach ``narrow_to_dte`` was called with FOR THIS ARM.
        sweep_max_dte: the parent materialisation's reach, for context only.
            Never the value a reader should treat as this cell's chain reach.
        window_start / window_end: the REQUESTED decision window, which is not
            necessarily ``daily[0].day``/``daily[-1].day`` (a window can open on
            a holiday).
        fill_haircut: the arm's haircut, 0..1 from mid toward the near side.
        starting_cash: the per-symbol notional, so a curve can be normalised.
        git_commit: provenance only; ``engine_identity`` is the identity.
    """

    run_id: Optional[str] = None
    scenario: str = ""
    symbol: str = ""
    split: str = "all"
    scenario_hash: Optional[str] = None
    config_hash: Optional[str] = None
    engine_identity: Optional[str] = None
    arm_max_dte: Optional[int] = None
    sweep_max_dte: Optional[int] = None
    window_start: Optional[date] = None
    window_end: Optional[date] = None
    fill_haircut: Optional[float] = None
    starting_cash: Optional[float] = None
    git_commit: Optional[str] = None


def _num(value: Any) -> Optional[float]:
    """A JSON-safe float: ``NaN``/``inf`` become ``None``.

    ``json.dumps`` spells them ``NaN``/``Infinity``, which is not legal JSON and
    which every strict reader (including BigQuery and the browser) rejects — so
    a single non-finite number would make the whole artifact unreadable rather
    than one field wrong. Same rule ``persist._finite`` applies to sweep rows.
    """
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _iso(value: Any) -> Optional[str]:
    """``date``/``datetime`` -> ISO text; anything else stringified or ``None``."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _daily_rows(result: SimulationResult) -> List[Dict[str, Any]]:
    """The equity curve, one dict per decision day.

    ``shares_held`` is carried whole (symbol -> shares) rather than summed: a
    sweep cell is single-symbol today, and a covered-call or multi-symbol replay
    tomorrow would silently lose the breakdown if this flattened it. Empty dicts
    are kept as ``{}`` — "flat that day" is a fact, and dropping the key would
    make it indistinguishable from "this run predates the field".
    """
    return [
        {
            "date": _iso(state.day),
            "equity": _num(state.equity),
            "cash": _num(state.cash),
            "reserved_collateral": _num(state.reserved_collateral),
            "open_options": int(state.open_options),
            "shares_held": {str(k): int(v)
                            for k, v in sorted((state.shares_held or {}).items())},
        }
        for state in result.daily
    ]


def _ledger_rows(result: SimulationResult) -> List[Dict[str, Any]]:
    """Every ``LedgerEvent``, every field, in the order the broker recorded them.

    ``detail`` is free-form by design and differs per ``kind`` — ``collateral``
    on a put open, ``collateral_released`` on a close or an expiry,
    ``strike``/``basis``/``collateral_released`` on an assignment, ``reason`` on
    a call-away. It is passed through rather than normalised into columns:
    flattening it would need a schema per kind here AND a matching one in the
    broker, and the two would drift the first time a kind gained a field. The
    frozen-fixture test pins the key set per kind so the drift is loud instead.
    """
    out: List[Dict[str, Any]] = []
    for event in result.broker.ledger:
        out.append({
            "date": _iso(event.event_date),
            "kind": event.kind,
            "underlying": event.underlying,
            "symbol": event.symbol,
            "contracts": int(event.contracts),
            "shares": int(event.shares),
            "price": _num(event.price),
            "cash_delta": _num(event.cash_delta),
            "fees": _num(event.fees),
            "detail": _jsonable(event.detail or {}),
        })
    return out


def _jsonable(value: Any) -> Any:
    """Recursively coerce a free-form ``detail`` payload into JSON-able types."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(),
                                                        key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int,)):
        return int(value)
    if isinstance(value, float):
        return _num(value)
    if isinstance(value, (date, datetime)):
        return _iso(value)
    return str(value)


def _cycle_rows(result: SimulationResult) -> List[Dict[str, Any]]:
    """``build_cycles`` output, serialised.

    **The builder is REUSED, never reimplemented.** ``evaluate._score`` calls
    ``build_cycles(result.broker.ledger)`` and scores the fitness report from
    what comes back; this calls the identical function on the identical ledger,
    so the artifact's cycle table and the row's cycle counts are the same
    objects rendered twice. A test asserts that equality directly rather than
    trusting this comment.

    The derived properties (``days``, ``total_pnl``, ``return_on_capital``,
    ``annualized_return``, ``outcome``) are materialised into the JSON rather
    than left for a reader to recompute: three consumers recomputing
    ``annualized_return`` is three chances to compound a 3-day cycle to a year.
    ``events`` is deliberately NOT re-embedded — every one of them is already in
    ``ledger``, and a second copy would roughly double the object for nothing.
    """
    rows: List[Dict[str, Any]] = []
    for cycle in build_cycles(result.broker.ledger):
        rows.append({
            "underlying": cycle.underlying,
            "start": _iso(cycle.start),
            "end": _iso(cycle.end),
            "is_open": bool(cycle.is_open),
            "days": int(cycle.days),
            "puts_sold": int(cycle.puts_sold),
            "calls_sold": int(cycle.calls_sold),
            "contracts_closed": int(cycle.contracts_closed),
            "rolls": int(cycle.rolls),
            "option_pnl": _num(cycle.option_pnl),
            "stock_pnl": _num(cycle.stock_pnl),
            "dividends": _num(cycle.dividends),
            "fees": _num(cycle.fees),
            "total_pnl": _num(cycle.total_pnl),
            "assigned": bool(cycle.assigned),
            "called_away": bool(cycle.called_away),
            "shares_acquired": int(cycle.shares_acquired),
            "cost_basis": _num(cycle.cost_basis),
            "exit_price": _num(cycle.exit_price),
            "max_collateral": _num(cycle.max_collateral),
            "capital_at_risk": _num(cycle.capital_at_risk),
            "return_on_capital": _num(cycle.return_on_capital),
            "annualized_return": _num(cycle.annualized_return),
            "outcome": cycle.outcome(),
            "event_count": len(cycle.events),
        })
    return rows


def _rejection_rows(result: SimulationResult) -> List[Dict[str, Any]]:
    """The tally as an ORDERED list, ranked as ``RejectionTally.summary`` ranked it.

    A dict would lose the ranking through any reader that does not preserve
    insertion order, and the ranking is the whole point — the first entry is the
    binding constraint. Complete, not truncated: FC-092's fix made the tally
    real, and the FC-057 lesson is that a truncated "top reasons" list reads as
    "these are the reasons".
    """
    return [{"reason": str(reason), "days": int(days)}
            for reason, days in (result.rejections or {}).items()]


def cell_artifact(result: SimulationResult, meta: ArtifactMeta) -> Dict[str, Any]:
    """One cell's full detail artifact, ready for ``json.dumps``.

    Args:
        result: the MID replay's ``SimulationResult``. The bid-sensitivity
            replay is deliberately never serialised — see ``MID_FILL_BASIS``.
        meta: the cell's coordinates, provenance and assumptions.

    Returns:
        A plain dict of JSON-able values. Nothing here reaches the network, the
        broker or the clock beyond one ``generated_at`` stamp, so it is safe to
        call from inside a replay loop and cheap to test.
    """
    reach = meta.arm_max_dte
    provenance: Dict[str, Any] = {
        "run_id": meta.run_id,
        "scenario": meta.scenario,
        "symbol": meta.symbol,
        "split": meta.split,
        "scenario_hash": meta.scenario_hash,
        "config_hash": meta.config_hash,
        "engine_identity": meta.engine_identity,
        "git_commit": meta.git_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "start": _iso(meta.window_start),
            "end": _iso(meta.window_end),
            # The replay's OWN first/last decision day, which can differ from
            # the requested window when it opens or closes on a non-session.
            "first_decision_day": _iso(result.daily[0].day) if result.daily else None,
            "last_decision_day": _iso(result.daily[-1].day) if result.daily else None,
        },
        # THIS ARM's chain reach, never the sweep's. `dte_cutoff` is the number
        # a quote was actually filtered on (`narrow_to_dte`: dte <= max_dte +
        # UNIVERSE_DTE_BUFFER, the same rule `ChainBuilder.build` fetches under),
        # so an operator comparing the artifact against a raw chain does not have
        # to know the buffer exists. `sweep_max_dte` is context: it says how much
        # wider the shared materialisation was, which is exactly the number that
        # must NOT be read as this cell's reach.
        "masked_reach": {
            "max_dte": None if reach is None else int(reach),
            "dte_buffer": int(UNIVERSE_DTE_BUFFER),
            "dte_cutoff": None if reach is None else int(reach) + int(UNIVERSE_DTE_BUFFER),
            "sweep_max_dte": (None if meta.sweep_max_dte is None
                              else int(meta.sweep_max_dte)),
        },
        # The fill assumption of THIS object. The row's `bid_fill_return` comes
        # from a replay that has no artifact; this stamp is what keeps the pair
        # honest.
        "fill": {
            "basis": MID_FILL_BASIS,
            "fill_haircut": _num(meta.fill_haircut),
        },
        "starting_cash": _num(meta.starting_cash
                              if meta.starting_cash is not None
                              else result.starting_cash),
    }

    return {
        "schema": ARTIFACT_SCHEMA,
        "provenance": provenance,
        "daily": _daily_rows(result),
        "ledger": _ledger_rows(result),
        "cycles": _cycle_rows(result),
        "roll_records": [_jsonable(record) for record in (result.roll_records or [])],
        "rejections": _rejection_rows(result),
        # The binding constraint, named rather than left as "read the first
        # element": a consumer that sorts the list for display would otherwise
        # silently change which reason it reports.
        "binding_constraint": (next(iter(result.rejections)) if result.rejections
                               else None),
        "counters": {
            "decision_days": len(result.daily),
            "candidate_days": int(result.candidate_days),
            "ledger_events": len(result.broker.ledger),
            "dividends_credited": _num(result.dividends_credited),
            "early_assignments": int(result.early_assignments),
            # The residual of C2: ITM short calls on an ex-date eve with no mark
            # to price extrinsic from, so the early-assignment test could not
            # run. Non-zero means some early assignments may be MISSING from the
            # ledger above — reported rather than assumed away.
            "unpriced_ex_div_calls": int(result.unpriced_ex_div_calls),
            "rolls_evaluated": int(result.rolls_evaluated),
            "rolls_executed": int(result.rolls_executed),
            "final_equity": _num(result.final_equity),
            "total_return": _num(result.total_return),
        },
        # FC-013 coverage, both reported rather than assumed away: a window that
        # reaches past a symbol's last table date stops gating it silently.
        "earnings_coverage": {
            "symbols_without_data": list(result.earnings_symbols_without_data or []),
            "symbols_past_horizon": list(result.earnings_symbols_past_horizon or []),
        },
    }
