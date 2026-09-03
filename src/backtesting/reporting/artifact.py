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
from typing import Any, Dict, List, Optional, Sequence

from ..data.chain_builder import UNIVERSE_DTE_BUFFER
from ..engine.simulator import DailyState, SimulationResult
from ..metrics.cycles import build_cycles
from ..metrics.fitness import BuyAndHold

# The artifact JSON's own version. Bumped only for an incompatible change (a
# removed or renamed field); adding a field is additive and does not move it.
# Mirrored by the object-name prefix `identity.ARTIFACT_PREFIX`, which is what a
# reader keys off before it has parsed anything.
ARTIFACT_SCHEMA = 1

# The bars sidecar's own version (FC-096 Phase E PR-1). Separate from
# `ARTIFACT_SCHEMA` because the two objects are written, stored and read
# independently: a cell artifact and a sidecar for the same window can be
# produced by two different engine builds (a re-run replays only what moved),
# and one version number over both would force a bump on the object that did
# not change.
BARS_SCHEMA = 1

# What `provenance.source` says on a sidecar. Named rather than inlined for the
# same reason `MID_FILL_BASIS` is: the test that pins the stamp and the code
# that writes it read one constant. It is deliberately NOT "Alpaca" or "the bar
# cache" — the point of the stamp is that these are the bars the REPLAY was
# handed, after the settlement clamp and the warm-up buffer, not a series
# fetched afresh for the chart.
BARS_SOURCE = "materialised bars"

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
        benchmark: THIS cell's scored ``FitnessReport.benchmark`` (FC-096 Phase
            E PR-1), or ``None`` when the window had no usable entry/exit close.
            It is the SCORED object, never a re-derivation from the spec's cash:
            a covered-call replay's benchmark is a lot-based buy-and-hold whose
            ``starting_cash`` is the lot value, and re-deriving shares from the
            spec's $100k would silently describe a different investment.
        capital_base: the denominator every ratio on this cell is taken over —
            ``starting_cash`` for a wheel replay, the lot value for a Phase C
            covered-call one. Stamped separately from ``starting_cash`` because
            the console must divide by THIS field: falling back to the spec's
            cash on a CC artifact would scale every ratio by the wrong number
            and look perfectly plausible doing it.
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
    benchmark: Optional[BuyAndHold] = None
    capital_base: Optional[float] = None


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


def _benchmark_block(benchmark: Optional[BuyAndHold],
                     entry_day: Optional[date],
                     exit_day: Optional[date]) -> Optional[Dict[str, Any]]:
    """The scored buy-and-hold, serialised — or ``None`` when there was none.

    Every field is COPIED off the ``BuyAndHold`` instance the scorer built; not
    one of them is recomputed here. ``capital_base`` is its ``starting_cash``,
    which for a Phase C covered-call benchmark is the LOT value rather than the
    spec's cash — the one number a re-derivation would get wrong while looking
    entirely reasonable.

    ``entry_day``/``exit_day`` are the replay's own first and last decision days
    (``_buy_and_hold`` enters at the close of the first and exits at the close of
    the last), because the ``BuyAndHold`` dataclass carries prices and no dates.

    ``None`` exactly when the report carried no benchmark: an absent benchmark
    means the window had no usable entry or exit close, and a block of nulls
    would read as "the benchmark was flat".
    """
    if benchmark is None:
        return None
    return {
        "shares": int(benchmark.shares),
        "entry_day": _iso(entry_day),
        "entry_price": _num(benchmark.entry_price),
        "exit_day": _iso(exit_day),
        "exit_price": _num(benchmark.exit_price),
        "dividends_per_share_total": _num(benchmark.dividends_per_share),
        "capital_base": _num(benchmark.starting_cash),
        "final_value": _num(benchmark.final_value),
        "total_return": _num(benchmark.total_return),
    }


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
        # FC-096 Phase E PR-1. The denominator every ratio on this cell is taken
        # over. For a wheel replay it equals `starting_cash`; for a Phase C
        # covered-call one it is the synthetic lot's value, and the two are NOT
        # interchangeable. Stamped separately so the console divides by a number
        # the engine chose rather than by whichever field happens to be present.
        "capital_base": _num(meta.capital_base
                             if meta.capital_base is not None
                             else meta.starting_cash
                             if meta.starting_cash is not None
                             else result.starting_cash),
    }

    return {
        "schema": ARTIFACT_SCHEMA,
        "provenance": provenance,
        # FC-096 Phase E PR-1. THIS cell's scored buy-and-hold, copied off the
        # `FitnessReport` — the same object the row's `benchmark_return` and
        # `excess_return` came from. `None` when the report had none. The
        # console cross-checks the (run, symbol, split) sidecar's curve against
        # `final_value` here and refuses to draw it on a mismatch.
        "benchmark": _benchmark_block(
            meta.benchmark,
            result.daily[0].day if result.daily else None,
            result.daily[-1].day if result.daily else None,
        ),
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


# --------------------------------------------------------------------------- #
# The bars sidecar (FC-096 Phase E PR-1)
# --------------------------------------------------------------------------- #
def _bar_rows(bars: Sequence[Any], first: date, last: date) -> List[Dict[str, Any]]:
    """The OHLCV rows inside ``[first, last]``, in date order.

    The clip is the DECISION-DAY bounds, which is the same window
    ``evaluate._closes`` clips to and ``compute_fitness`` measures ``days`` over.
    Clipping to the REQUESTED window instead would include the warm-up buffer's
    tail and shift the curve's first point off the benchmark's entry.
    """
    rows = [b for b in bars if first <= b.bar_date <= last]
    rows.sort(key=lambda b: b.bar_date)
    return [
        {
            "date": _iso(b.bar_date),
            "open": _num(b.open),
            "high": _num(b.high),
            "low": _num(b.low),
            "close": _num(b.close),
            "volume": int(b.volume or 0),
        }
        for b in rows
    ]


def _buy_and_hold_block(benchmark: Optional[BuyAndHold], rows: List[Dict[str, Any]],
                        *, symbol: str, entry_day: date, exit_day: date,
                        dividends: Any) -> Optional[Dict[str, Any]]:
    """The engine's buy-and-hold, as a curve.

    ``value_t = capital_base + shares × (close_t − entry_price)
                + shares × div_ps(entry_day, t]``

    Every input is the SCORED ``BuyAndHold``'s: ``shares`` and ``entry_price``
    are copied, never re-derived from the spec's cash (a Phase C covered-call
    benchmark holds a fixed lot against a lot-sized ``starting_cash``, and
    ``cash // price`` would invent a different position). ``div_ps`` is
    ``DividendSchedule.total_between``, whose interval is half-open at the entry
    day — a buyer at that close does not collect a dividend going ex that day —
    which is exactly the interval ``evaluate._score`` scored the benchmark over.

    Both facts together are what make the parity property hold BY CONSTRUCTION
    rather than by luck: at ``t = exit_day`` the close is the benchmark's
    ``exit_price`` and the dividend term is its ``dividends_per_share``, so the
    last curve point equals ``BuyAndHold.final_value`` exactly.

    **The construction rests on one invariant the simulator does not state.**
    ``entry_day``/``exit_day`` are ``bars_artifact``'s clip — ``daily[0].day``
    and ``daily[-1].day`` of the base arm's ``SimulationResult`` — while
    ``compute_fitness`` takes ``report.start``/``report.end`` from the same two
    values of the ``daily`` list it is handed. The parity is therefore only as
    true as "the ``daily`` the sidecar clips to is the ``daily`` the report was
    scored over". Nothing in the simulator's contract promises that; a future
    change that filtered ``daily`` on the way into scoring (or returned a
    padded curve to the artifact) would move the clip off the scored window and
    the curve's last point off ``final_value``, silently. A test on the golden
    replay pins ``provenance.first_decision_day == report.start`` and
    ``last_decision_day == report.end`` so that change fails loudly instead.

    ``None`` exactly when ``benchmark`` is ``None``.
    """
    if benchmark is None:
        return None
    capital_base = float(benchmark.starting_cash)
    shares = int(benchmark.shares)
    entry_price = float(benchmark.entry_price)

    curve: List[Dict[str, Any]] = []
    for row in rows:
        close = row.get("close")
        if close is None:
            # A bar with no usable close cannot be a curve point. Skipped rather
            # than carried forward: an interpolated point would be a number the
            # engine never computed, sitting in an object whose whole claim is
            # that it did not compute anything.
            continue
        day = date.fromisoformat(row["date"])
        div_ps = (0.0 if dividends is None
                  else float(dividends.total_between(symbol, entry_day, day)))
        curve.append({
            "date": row["date"],
            "value": _num(capital_base
                          + shares * (float(close) - entry_price)
                          + shares * div_ps),
        })

    return {
        "capital_base": _num(capital_base),
        "shares": shares,
        "entry_day": _iso(entry_day),
        "entry_price": _num(entry_price),
        "exit_day": _iso(exit_day),
        "exit_price": _num(benchmark.exit_price),
        "dividends_per_share_total": _num(benchmark.dividends_per_share),
        "final_value": _num(benchmark.final_value),
        "daily": curve,
    }


def bars_artifact(
    materialised_bars: Sequence[Any],
    symbol: str,
    window: Any,
    *,
    daily: Sequence[DailyState],
    benchmark: Optional[BuyAndHold],
    dividends: Any,
    run_id: Optional[str] = None,
    engine_identity: Optional[str] = None,
    git_commit: Optional[str] = None,
) -> Dict[str, Any]:
    """One window's bars sidecar: the closes the replay saw, and the B&H curve.

    FC-096 Phase E PR-1, §D-2. The cell artifact deliberately holds no price
    series (`fc-096-b.md` §B2), and there is no durable bar store the dashboard
    can read: ``BarStore`` is a per-container local parquet cache by explicit
    design, neither bucket has a bars prefix, and BigQuery's stock history
    covers the LIVE universe only — never a candidate, and never the replay's
    own settlement-clamped series. So the engine writes the series, once per
    (run, symbol, split), from the base arm.

    Args:
        materialised_bars: the window's materialised ``StockBar``s, warm-up
            buffer included. Clipped here to the decision-day bounds.
        symbol: the underlying. Used for the dividend lookup and stamped.
        window: the runner's ``(split, start, end)`` tuple — the REQUESTED
            window, which is not necessarily the decision-day bounds.
        daily: the base arm's ``DailyState`` rows. ``daily[0].day`` and
            ``daily[-1].day`` are the clip, the benchmark's entry/exit days and
            the bounds ``compute_fitness`` measures ``days`` over — one
            definition, used three times.
        benchmark: the base arm's SCORED ``FitnessReport.benchmark``, or
            ``None``.
        dividends: the ``DividendSchedule`` the replay was scored with.
        run_id / engine_identity / git_commit: provenance, passed through.

    Returns:
        A plain dict of JSON-able values, ``"schema": BARS_SCHEMA``.

    Raises:
        ValueError: when ``daily`` is empty. A window with no decision day has
            no clip and no benchmark, and writing a sidecar of nulls for it
            would put an empty chart where an absent one belongs.
    """
    if not daily:
        raise ValueError(
            "cannot build a bars sidecar for a window with no decision days: "
            "there is nothing to clip to and no benchmark to curve")
    split, w_start, w_end = window
    first_day = daily[0].day
    last_day = daily[-1].day
    rows = _bar_rows(materialised_bars, first_day, last_day)
    all_dates = sorted(b.bar_date for b in materialised_bars)

    return {
        "schema": BARS_SCHEMA,
        "provenance": {
            "run_id": run_id,
            "symbol": symbol,
            "split": split,
            "window": {"start": _iso(w_start), "end": _iso(w_end)},
            "first_decision_day": _iso(first_day),
            "last_decision_day": _iso(last_day),
            "engine_identity": engine_identity,
            "git_commit": git_commit,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": BARS_SOURCE,
            # The FULL materialised span, warm-up included — the freshness fact
            # the console's provenance footer shows. Deliberately not the
            # clipped span, which is the window and says nothing about the data.
            "data_from": _iso(all_dates[0]) if all_dates else None,
            "data_to": _iso(all_dates[-1]) if all_dates else None,
            "bars_in_window": len(rows),
        },
        "bars": rows,
        "buy_and_hold": _buy_and_hold_block(
            benchmark, rows, symbol=symbol,
            entry_day=first_day, exit_day=last_day, dividends=dividends),
    }
