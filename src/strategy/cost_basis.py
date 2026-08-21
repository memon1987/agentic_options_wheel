"""Shared cost-basis floor resolution for covered calls (FC-065 Phase 1).

Writing a covered call below the cost basis of the shares it covers is a
guaranteed loss. The floor that prevents it has to be the *same* number
wherever it is computed, so it is resolved here and used by both the scanner
(which produces opportunities) and the seller (which executes them).

FC-065 inverted the FC-029/FC-050 chain. The floor is now **Alpaca's reported
``avg_entry_price`` for the equity position** — one broker-authoritative field,
verifiable in the Alpaca UI, already weighted across lots. ``wheel_state`` is
gone from the chain entirely (``STATE_STORAGE_BUCKET`` has never been set, so
it has never been populated in production), and BigQuery is demoted from
floor-*setter* to floor-*verifier*: an inline divergence cross-check that
fail-closes the symbol when the broker's number disagrees with what the
assignment history says the shares cost.

Fail-closed is the discipline throughout: absent / zero / unreadable
``avg_entry_price`` yields no floor, and a divergent cross-check yields no
floor. "No floor" always means "no call", never "any strike".
"""

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import structlog

from ..api.alpaca_client import AlpacaClient
from ..utils.config import Config

logger = structlog.get_logger(__name__)

# Source labels returned alongside a resolved basis.
SOURCE_ALPACA = "alpaca"
SOURCE_UNRESOLVED = "unresolved"
# FC-065: the broker gave a number, but the BigQuery cross-check disagreed with
# it beyond tolerance. Distinct from "unresolved" because the failure mode is
# different — presence with a wrong value, which fail-closed-on-zero cannot see.
SOURCE_DIVERGENT = "divergent"

# Cross-check outcomes carried on the resolution detail.
CROSS_CHECK_OK = "ok"
CROSS_CHECK_DIVERGENT = "divergent"
CROSS_CHECK_UNAVAILABLE = "unavailable"

# FC-065: tolerance for the Alpaca-vs-reconstructed comparison.
DIVERGENCE_ABS_TOLERANCE = 0.10        # dollars per share
DIVERGENCE_REL_TOLERANCE = 0.001       # 0.1% of the broker's basis

# Enough assignment rows to reconstruct any plausible held quantity. Bounded so
# a symbol with years of history doesn't scan its whole partition set.
_MAX_ASSIGNMENT_ROWS = 20


def reconstruct_expected_basis(
    rows: Sequence[Mapping[str, Any]], shares_held: float
) -> Optional[Dict[str, Any]]:
    """Reconstruct the expected per-share basis from assignment rows.

    FC-065's derivation, stated in the plan: the cash paid at assignment is
    ``strike x 100`` per contract (OPASN rows carry no price — verified NULL),
    and Alpaca's ``avg_entry_price`` is premium-netted, so the value it should
    report is ``strike - premium`` where ``premium`` is the last sell-to-open
    fill of the assigned contract before assignment.

    Args:
        rows: assignment rows **newest first**, each a mapping with
            ``strike_price`` (float), ``shares`` (float, already contracts x
            100) and ``put_premium`` (float or None when no opening fill was
            found).
        shares_held: the quantity Alpaca reports for the position. Rows are
            consumed newest-first up to this quantity, which is what FIFO share
            disposal leaves behind after a partial call-away.

    Returns:
        ``None`` when nothing can be reconstructed at all, otherwise a dict of
        ``expected_basis_per_share`` (``None`` when any consumed lot has no
        derivable premium — the caller treats that as "no comparison possible",
        not as divergence), ``reconstructed_shares`` and ``lots``.
    """
    if shares_held <= 0:
        return None

    used = 0.0
    weighted = 0.0
    premium_missing = False
    lots: List[Dict[str, Any]] = []

    for row in rows:
        remaining = shares_held - used
        if remaining <= 0:
            break
        try:
            strike = float(row.get('strike_price') or 0)
            shares = float(row.get('shares') or 0)
        except (TypeError, ValueError):
            continue
        if strike <= 0 or shares <= 0:
            continue

        take = min(shares, remaining)
        raw_premium = row.get('put_premium')
        premium: Optional[float]
        try:
            premium = float(raw_premium) if raw_premium is not None else None
        except (TypeError, ValueError):
            premium = None

        if premium is None:
            premium_missing = True
        else:
            weighted += (strike - premium) * take

        used += take
        lots.append({
            'occ_symbol': row.get('occ_symbol'),
            'strike': strike,
            'shares_used': take,
            'put_premium': premium,
        })

    if used <= 0:
        return None

    return {
        'expected_basis_per_share': None if premium_missing else weighted / used,
        'reconstructed_shares': used,
        'lots': lots,
    }


class CostBasisResolver:
    """Resolve the per-share cost basis used as the covered-call strike floor."""

    def __init__(self, alpaca_client: AlpacaClient, config: Config,
                 allow_bigquery: bool = True):
        """Initialize the resolver.

        Args:
            alpaca_client: Alpaca API client.
            config: Configuration instance.
            allow_bigquery: whether the BigQuery divergence cross-check may run.
                A backtest passes False: the cross-check would otherwise query
                *production* trade history — against CURRENT_TIMESTAMP() —
                mixing real assignments into a simulated run. With it disabled
                the cross-check reports "unavailable", which keeps the floor;
                it never manufactures one.
        """
        self.alpaca = alpaca_client
        self.config = config
        self.allow_bigquery = allow_bigquery
        # FC-029 (HIGH 3), retained: cache the BigQuery client so repeated
        # cross-checks within one scan don't pay Client() construction twice.
        self._bq_client = None

    def resolve(self, symbol: str, stock_position: Dict[str, Any],
                shares_owned: int) -> float:
        """Return the per-share cost basis, or 0 if there is no usable floor."""
        return self.resolve_with_source(symbol, stock_position, shares_owned)[0]

    def resolve_with_source(self, symbol: str, stock_position: Dict[str, Any],
                            shares_owned: int) -> Tuple[float, str]:
        """Resolve the per-share cost basis and name the source it came from."""
        detail = self.resolve_detailed(symbol, stock_position, shares_owned)
        return detail['basis'], detail['source']

    def resolve_detailed(self, symbol: str, stock_position: Dict[str, Any],
                         shares_owned: int) -> Dict[str, Any]:
        """Resolve the floor and return the full decision, cross-check included.

        FC-065: Alpaca's ``avg_entry_price`` is the primary and only floor
        source. BigQuery runs inline as a divergence cross-check and can veto
        the broker's number, but can never supply one.

        Returns a dict of:
          - ``basis``: the floor to use, or ``0.0`` when there is none.
          - ``source``: ``alpaca`` / ``unresolved`` / ``divergent``.
          - ``broker_basis``: what Alpaca reported, whatever the verdict — so
            the caller can log the rejected number alongside the reason.
          - ``cross_check``: dict with ``status`` and, where derivable,
            ``expected_basis``, ``basis_delta``, ``tolerance``,
            ``reconstructed_shares`` and ``reason``.

        Args:
            symbol: Underlying ticker (e.g. ``"AAPL"``).
            stock_position: Alpaca position dict; ``avg_entry_price`` is read
                from it (``AlpacaClient.get_positions`` emits it since FC-065).
            shares_owned: Current share count, used to bound the cross-check's
                lot reconstruction.
        """
        broker_basis = self._broker_avg_entry_price(stock_position)
        detail: Dict[str, Any] = {
            'basis': 0.0,
            'source': SOURCE_UNRESOLVED,
            'broker_basis': broker_basis,
            'cross_check': {'status': CROSS_CHECK_UNAVAILABLE,
                            'reason': 'no_broker_basis'},
        }

        if broker_basis <= 0:
            # Fail closed. FC-029 (2026-05-08) reported Alpaca returning 0 for
            # assigned positions; that was not reproducible in 2026-07, but the
            # whole floor now rests on this field, so a regression to zero must
            # halt writing rather than write unprotected.
            logger.warning(
                "No usable Alpaca avg_entry_price — no cost-basis floor",
                event_category="risk",
                event_type="cost_basis_broker_basis_missing",
                symbol=symbol,
                shares=shares_owned,
            )
            return detail

        cross_check = self._cross_check(symbol, broker_basis, shares_owned)
        detail['cross_check'] = cross_check

        if cross_check['status'] == CROSS_CHECK_DIVERGENT:
            # Fail closed. Both plan reviewers rejected warn-only: the
            # dangerous case is presence with a wrong value (a mis-adjusted
            # split, a partial disposal, a corporate action), which
            # fail-closed-on-zero cannot catch.
            logger.warning(
                "Cost-basis cross-check divergent — no floor",
                event_category="risk",
                event_type="cost_basis_divergence_detected",
                symbol=symbol,
                shares=shares_owned,
                broker_basis=broker_basis,
                expected_basis=cross_check.get('expected_basis'),
                basis_delta=cross_check.get('basis_delta'),
                tolerance=cross_check.get('tolerance'),
                reconstructed_shares=cross_check.get('reconstructed_shares'),
                reason=cross_check.get('reason'),
            )
            detail['source'] = SOURCE_DIVERGENT
            return detail

        detail['basis'] = broker_basis
        detail['source'] = SOURCE_ALPACA
        return detail

    @staticmethod
    def _broker_avg_entry_price(stock_position: Dict[str, Any]) -> float:
        """Read Alpaca's ``avg_entry_price`` off a position dict.

        Deliberately strict: no derivation from ``cost_basis`` happens here.
        The client wrapper owns that fallback (``AlpacaClient.get_positions``),
        so a position dict reaching the resolver without the field is a
        plumbing defect, and the fail-closed path is the right response to it.
        """
        try:
            return float(stock_position.get('avg_entry_price') or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _cross_check(self, symbol: str, broker_basis: float,
                     shares_owned: int) -> Dict[str, Any]:
        """Compare the broker's basis against reconstructed assignment history.

        Runs per held symbol per scan. Fail-closes the symbol on divergence;
        degrades to "unavailable" (floor kept) when no comparison is possible.
        """
        if not self.allow_bigquery:
            return {'status': CROSS_CHECK_UNAVAILABLE, 'reason': 'bigquery_disabled'}

        reconstruction = self._lookup_assignment_basis(symbol, shares_owned)
        if not reconstruction:
            logger.info(
                "Cost-basis cross-check unavailable — no assignment history",
                event_category="risk",
                event_type="cost_basis_cross_check_unavailable",
                symbol=symbol,
                broker_basis=broker_basis,
                reason="no_assignment_history",
            )
            return {'status': CROSS_CHECK_UNAVAILABLE,
                    'reason': 'no_assignment_history'}

        reconstructed_shares = float(reconstruction.get('reconstructed_shares') or 0)
        expected = reconstruction.get('expected_basis_per_share')
        tolerance = max(DIVERGENCE_ABS_TOLERANCE,
                        DIVERGENCE_REL_TOLERANCE * broker_basis)

        # Plan: "if reconstructed shares != Alpaca qty, treat as divergence."
        # Checked before the price comparison because a quantity we cannot
        # reproduce means the lots we are pricing are not the lots we hold.
        if abs(reconstructed_shares - float(shares_owned)) >= 1:
            return {'status': CROSS_CHECK_DIVERGENT,
                    'reason': 'share_count_mismatch',
                    'expected_basis': expected,
                    'tolerance': tolerance,
                    'reconstructed_shares': reconstructed_shares}

        if expected is None:
            # No opening fill for the assigned contract: the put premium is not
            # derivable, so no comparison is possible. Log it, keep the floor —
            # fail closed only on zero/missing/divergent.
            logger.info(
                "Cost-basis cross-check unavailable — put premium underivable",
                event_category="risk",
                event_type="cost_basis_cross_check_unavailable",
                symbol=symbol,
                broker_basis=broker_basis,
                reconstructed_shares=reconstructed_shares,
                reason="premium_underivable",
            )
            return {'status': CROSS_CHECK_UNAVAILABLE,
                    'reason': 'premium_underivable',
                    'reconstructed_shares': reconstructed_shares}

        delta = broker_basis - float(expected)
        if abs(delta) > tolerance:
            return {'status': CROSS_CHECK_DIVERGENT,
                    'reason': 'basis_mismatch',
                    'expected_basis': float(expected),
                    'basis_delta': round(delta, 4),
                    'tolerance': round(tolerance, 4),
                    'reconstructed_shares': reconstructed_shares}

        return {'status': CROSS_CHECK_OK,
                'reason': None,
                'expected_basis': float(expected),
                'basis_delta': round(delta, 4),
                'tolerance': round(tolerance, 4),
                'reconstructed_shares': reconstructed_shares}

    def _lookup_assignment_basis(self, symbol: str,
                                 shares_held: int) -> Optional[Dict[str, Any]]:
        """Reconstruct the expected basis from BigQuery assignment history.

        **This is the single BigQuery chokepoint of the cost-basis layer.** The
        test suite's hermeticity guard (``tests/conftest.py``) patches this one
        method, so no unit test can reach live BigQuery; any future BQ read on
        this path must go through here rather than open a second door.

        The lookback bound FC-029 added is gone: it only existed because this
        query set the *floor*, where stale history was a hazard. As a
        cross-check, old history degrades to "no comparison available", never
        to a silently looser floor — which is what retired the 90-day cliff
        (AMZN/GOOGL 2026-09-04, NVDA 09-07, AAPL 09-11).

        Returns ``None`` when BigQuery is gated off, unreachable, errors, or
        has no assignment history for ``symbol`` — all of which mean "no
        comparison", not "no floor".
        """
        if not self.allow_bigquery:
            logger.debug("BigQuery cost-basis cross-check disabled",
                         event_category="data",
                         event_type="cost_basis_cross_check_disabled",
                         symbol=symbol)
            return None

        try:
            from google.cloud import bigquery
            if self._bq_client is None:
                project_id = (
                    os.environ.get('GCP_PROJECT')
                    or os.environ.get('GOOGLE_CLOUD_PROJECT')
                    or os.environ.get('GCP_PROJECT_ID')
                )
                self._bq_client = bigquery.Client(project=project_id) if project_id else bigquery.Client()
            # OPASN rows carry the assigned contract (OCC symbol), the strike
            # and the contract count, but no price. The put premium comes from
            # the LAST sell-to-open fill of that same contract before the
            # assignment — the roll/re-sell ambiguity is resolved as "last
            # fill" per the plan.
            # FC-075 Phase 2 (DD-4): dataset from config, not hardcoded. On the
            # covered-call service this must query the covered_call dataset — the
            # wheel's assignment history is a different account's lots and would
            # falsely diverge the cross-check, permanently blocking any shared
            # underlying. Defaults to options_wheel for the wheel.
            dataset = self.config.bigquery_dataset
            query = f"""
            SELECT
              a.symbol AS occ_symbol,
              a.strike_price AS strike_price,
              ABS(COALESCE(a.qty, 0)) * 100 AS shares,
              a.transaction_time AS assigned_at,
              f.price AS put_premium
            FROM `{dataset}.trades_from_activities` a
            LEFT JOIN `{dataset}.trades_from_activities` f
              ON f.activity_type = 'FILL'
             AND f.symbol = a.symbol
             AND f.option_type = 'put'
             AND f.side IN ('sell_short', 'sell')
             AND f.transaction_time <= a.transaction_time
            WHERE a.activity_type = 'OPASN'
              AND a.option_type = 'put'
              AND a.underlying = @symbol
              AND a.transaction_time <= CURRENT_TIMESTAMP()
            QUALIFY ROW_NUMBER() OVER (
              PARTITION BY a.activity_id ORDER BY f.transaction_time DESC
            ) = 1
            ORDER BY a.transaction_time DESC
            LIMIT @max_rows
            """
            job_config = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter('symbol', 'STRING', symbol),
                bigquery.ScalarQueryParameter('max_rows', 'INT64',
                                              _MAX_ASSIGNMENT_ROWS),
            ])
            job = self._bq_client.query(query, job_config=job_config)
            rows = [
                {
                    'occ_symbol': row.get('occ_symbol'),
                    'strike_price': row.get('strike_price'),
                    'shares': row.get('shares'),
                    'put_premium': row.get('put_premium'),
                }
                for row in job.result(timeout=10)
            ]
        except Exception as e:
            logger.warning(
                "Cost-basis cross-check lookup failed; keeping broker floor",
                event_category="risk",
                event_type="cost_basis_cross_check_lookup_failed",
                symbol=symbol, error=str(e),
            )
            return None

        return reconstruct_expected_basis(rows, float(shares_held))


def opportunity_floor_per_share(opportunity: Dict[str, Any]) -> float:
    """Return the per-share cost-basis floor carried by an opportunity.

    FC-050: the two producers of call opportunities disagree on shape — the
    scanner emits ``cost_basis_per_share`` + ``max_contracts``, the wheel
    engine emits a *total* ``stock_cost_basis`` + ``shares_covered``. Reading
    only the wheel-engine keys is why the execute-time floor never fired on
    the path production runs.

    Resolution order, first positive wins:
      1. ``cost_basis_per_share`` (scanner shape).
      2. ``stock_cost_basis / shares_covered`` (wheel-engine shape), where
         ``shares_covered`` falls back to ``contracts * 100``. Never a bare
         100: post-FC-038 a covered call can be multi-contract, and dividing
         a total basis by 100 would understate the floor by the contract
         count.

    Returns 0.0 when no floor can be derived; callers must fail closed.
    """
    try:
        per_share = float(opportunity.get('cost_basis_per_share') or 0)
        if per_share > 0:
            return per_share

        total_basis = float(opportunity.get('stock_cost_basis') or 0)
        if total_basis > 0:
            shares_covered = float(opportunity.get('shares_covered') or 0)
            if shares_covered <= 0:
                shares_covered = float(opportunity.get('contracts') or 0) * 100
            if shares_covered > 0:
                return total_basis / shares_covered
    except (TypeError, ValueError):
        return 0.0

    return 0.0


def opportunity_shares_covered(opportunity: Dict[str, Any]) -> int:
    """Shares this covered call actually covers, across both shapes.

    FC-065 Phase 4, same root cause as ``opportunity_floor_per_share``: the
    ``call_sale_executed`` event read ``shares_covered`` — a wheel-engine-only
    key — so **every production covered-call fill logged
    ``shares_covered=0``**, and FC-029's standing production-validation item
    ("confirm the floor held on a real write") has been unsatisfiable ever
    since.

    Resolution order, first positive wins:
      1. ``shares_covered`` (wheel-engine shape).
      2. ``contracts * 100`` — the quantity actually being sold, which is the
         number the event should report.
      3. ``max_contracts * 100`` (scanner shape, pre-sizing).

    Never falls back to ``shares_owned``: a 300-share position writing one
    contract covers 100 shares, not 300, and the event is about the write.
    Returns 0 when nothing is derivable.
    """
    try:
        shares = float(opportunity.get('shares_covered') or 0)
        if shares > 0:
            return int(shares)

        for key in ('contracts', 'max_contracts'):
            contracts = float(opportunity.get(key) or 0)
            if contracts > 0:
                return int(contracts * 100)
    except (TypeError, ValueError):
        return 0

    return 0


def opportunity_total_return_if_called(opportunity: Dict[str, Any]) -> float:
    """Premium plus capital gain to the floor, if the call is exercised.

    Derived from the quantities actually being sold rather than read off the
    opportunity, because neither producer carries a usable figure at execution
    time: the wheel-engine key ``total_return_if_called`` is absent on scanner
    opportunities (the FC-050 shape bug), and the scanner's own
    ``total_return_if_assigned`` is scaled by ``max_contracts``, which
    overstates the number whenever selection sizes down to fewer contracts.

    ``premium * shares + (strike - floor) * shares``.  Negative when the
    strike sits below the floor — which the execute-time gate blocks, so a
    negative value in this table means the gate was bypassed and is worth
    an investigation.

    Falls back to a positive declared ``total_return_if_called`` only when the
    derivation has no inputs; returns 0.0 when neither is available.
    """
    try:
        shares = opportunity_shares_covered(opportunity)
        floor = opportunity_floor_per_share(opportunity)
        strike = float(opportunity.get('strike_price') or 0)
        premium = float(opportunity.get('premium') or 0)

        if shares > 0 and floor > 0 and strike > 0:
            return premium * shares + (strike - floor) * shares

        declared = float(opportunity.get('total_return_if_called') or 0)
        if declared:
            return declared
    except (TypeError, ValueError):
        return 0.0

    return 0.0
