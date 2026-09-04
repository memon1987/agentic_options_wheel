// FC-096 Phase E PR-2 (§D-4): the client digest, and the line it must not cross.
//
// A pure function over `(artifact, sidecar | null)`. Everything it produces is
// DISPLAY-ONLY: three tiles the row has no column for (gross premium, fees,
// dollar-weighted deployment) and the chart-shaped series PR-3 draws. Nothing
// here feeds a verdict, a median, a Δ, a colour, the forecast or a comparison,
// and the tiles that read from it are grey, uncoloured and labelled by source.
//
// The FC-060 rule "the UI computes nothing" was written about RANKING INPUTS
// and still holds in full. What it never meant was that a number the engine
// does not persist may not be shown at all — it means such a number may never
// become a ranking, and may never disagree with one that IS persisted. Both
// halves are enforced here:
//
//   * `reconcile` recomputes EIGHT quantities the engine already persists on the
//     row (option P&L, max drawdown, puts, calls, completed cycles, final
//     equity, average collateral and the annualised return on it) and the tests
//     assert each equals the row's value on a REAL captured artifact. It exists
//     for the test. The strip DISPLAYS the row's numbers, never `reconcile`'s —
//     the point is to catch this module drifting from the engine, not to become
//     a second engine.
//
//     Equalities 7 and 8 were added in review round 1 (F7). They are the ones
//     that pin the RoC TOOLTIP: the strip now states the engine's actual
//     denominator — mean reserved collateral over the days it was above zero,
//     annualised over calendar days — and a claim about a definition is only
//     worth making if something fails when the definition moves.
//   * Every ratio divides by `provenance.capital_base`, the stamp PR-1 added,
//     and is SUPPRESSED rather than approximated when a non-wheel artifact does
//     not carry one. A covered-call cell divided by the wheel's `starting_cash`
//     would print a plausible, wrong percentage with a correct-looking label.
//
// Rejected alternative (§D-4): an engine-side `summary` block. It would not
// exist on the 20 objects already stored, and monthly buckets are a chart, not
// a fact the engine should own.

import type { MonthlyCashflow, SimArtifact, SimBars } from '../../../types/v2';
import { artifactStrategy } from './normaliseArtifact';
import {
  CAPITAL_BASE_SUPPRESSED,
  deploymentSeries,
  drawdownSeries,
  FEE_BEARING_KINDS,
  inSet,
  maxDrawdownOf,
  monthlyFromLedger,
  OPEN_KINDS,
  resolveCapitalBase,
  type CapitalBaseSource,
  type DeploymentPoint,
  type DeploymentReading,
  type DrawdownPoint,
} from './series';

// --------------------------------------------------------------------------- //
// Re-exports.
//
// PR-3 moved the series builders and the capital-base resolver into `series.ts`
// so the CHARTS and these TILES read one definition rather than two. The names
// stay exported from here because this is where §D-4 describes them and where
// the callers already import them from; the bodies live next to the chart seams
// they also feed.
// --------------------------------------------------------------------------- //

export {
  CAPITAL_BASE_SUPPRESSED,
  FEE_BEARING_KINDS,
  maxDrawdownOf,
  OPEN_KINDS,
  OPTION_CASH_KINDS,
  resolveCapitalBase,
} from './series';
export type { CapitalBaseSource, DeploymentReading } from './series';

export interface DigestReconcile {
  optionPnl: number;
  maxDrawdown: number;
  putsSold: number;
  callsSold: number;
  cyclesCompleted: number;
  finalEquity: number;
  /**
   * `fitness.py:427` — the mean of `reserved_collateral` over the days it was
   * ABOVE ZERO, not over every decision day. On the captured fixture that is
   * $29,758.24 across 91 of 189 days; the all-days mean is $14,328.04, and the
   * deployment tile's denominator is the all-days one. Two different questions,
   * two different numbers, and the tooltips now say which is which.
   */
  avgCollateral: number | null;
  /** `total_pnl ÷ avgCollateral × 365 / calendar days`. `null` without a base. */
  annualizedReturnOnCollateral: number | null;
  /**
   * `(end - start).days` on the engine's own window — the CALENDAR span between
   * the first and last decision day, not the count of decision days. 273 on the
   * fixture, against 189 decision days; annualising by the wrong one of the two
   * moves the headline by 44%.
   */
  calendarDays: number | null;
}

export interface ArtifactDigest {
  strategy: string;
  capitalBase: number | null;
  capitalBaseSource: CapitalBaseSource | null;
  /** True when no honest denominator exists. Every ratio tile hides. */
  ratiosSuppressed: boolean;
  /** Why they are hidden, in the operator's words. Null when they are not. */
  suppressionReason: string | null;

  /** Σ (`cash_delta` + `fees`) over the two open kinds — cash before fees. */
  grossPremium: number;
  /** Σ `fees` over the WHOLE ledger. The tile's headline number. */
  fees: number;
  /** Contracts over the fee-bearing set — the rate's denominator. */
  feeContracts: number;
  /** `fees over the fee-bearing set ÷ feeContracts`, or null when there are none. */
  feeRatePerContract: number | null;

  /** Terminal value of `netOptionCashSeries`. */
  netOptionCash: number;
  netOptionCashSeries: Array<{ date: string; value: number }>;
  monthly: MonthlyCashflow[];
  drawdownSeries: DrawdownPoint[];
  deploymentSeries: DeploymentPoint[];
  deployment: DeploymentReading;

  /** FOR THE TEST ONLY. Never rendered — the strip shows the ROW's numbers. */
  reconcile: DigestReconcile;
}

export interface DigestOptions {
  /** `spec.strategy` off the sweep row, when the run carries one. */
  specStrategy?: string | null;
}

// --------------------------------------------------------------------------- //
// Pieces
// --------------------------------------------------------------------------- //

/**
 * The engine's `days`: `(end - start).days` over the first and last DECISION
 * day, in calendar days.
 *
 * `fitness.py:143-152` annualises by this, not by `decision_days` — 273 against
 * 189 on the captured fixture. Whole days only, so a UTC subtraction is exact
 * and no timezone can shift it.
 */
export function calendarSpan(artifact: SimArtifact): number | null {
  const { first_decision_day: first, last_decision_day: last } = artifact.provenance.window;
  if (!first || !last) return null;
  const a = Date.parse(`${first}T00:00:00Z`);
  const b = Date.parse(`${last}T00:00:00Z`);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.round((b - a) / 86_400_000);
}

// --------------------------------------------------------------------------- //
// The digest
// --------------------------------------------------------------------------- //

export function computeDigest(
  artifact: SimArtifact,
  sidecar: SimBars | null,
  options: DigestOptions = {},
): ArtifactDigest {
  const strategy = artifactStrategy(options.specStrategy, artifact);
  const { base: capitalBase, source: capitalBaseSource } = resolveCapitalBase(artifact, strategy);

  // --- ledger sums ------------------------------------------------------- //
  let grossPremium = 0;
  let fees = 0;
  let feeBearingFees = 0;
  let feeContracts = 0;
  for (const event of artifact.ledger) {
    fees += event.fees;
    if (inSet(OPEN_KINDS, event.kind)) grossPremium += event.cash_delta + event.fees;
    if (inSet(FEE_BEARING_KINDS, event.kind)) {
      feeBearingFees += event.fees;
      feeContracts += event.contracts;
    }
  }
  const feeRatePerContract = feeContracts > 0 ? feeBearingFees / feeContracts : null;

  // --- option cash, cumulative and by month ------------------------------ //
  //
  // One builder, shared with `PremiumCharts` (§PR-3). The tile's number and the
  // chart's last point cannot disagree because there is only one of them.
  const optionCash = monthlyFromLedger(artifact.ledger);

  // --- drawdown ---------------------------------------------------------- //
  const drawdown = drawdownSeries(artifact.daily);

  // --- deployment -------------------------------------------------------- //
  const deployed = deploymentSeries(artifact.daily, sidecar, artifact.cycles, capitalBase);

  // --- reconcile (test-only; see the header) ------------------------------ //
  const finalEquity = artifact.daily.length
    ? artifact.daily[artifact.daily.length - 1].equity
    : 0;
  const deployedCollateral = artifact.daily
    .map((d) => d.reserved_collateral)
    .filter((c) => c > 0);
  const avgCollateral = deployedCollateral.length
    ? deployedCollateral.reduce((a, b) => a + b, 0) / deployedCollateral.length
    : null;
  const calendarDays = calendarSpan(artifact);
  const startingCash = artifact.provenance.starting_cash;
  const totalPnl =
    typeof startingCash === 'number' && Number.isFinite(startingCash)
      ? finalEquity - startingCash
      : null;
  const reconcile: DigestReconcile = {
    optionPnl: artifact.cycles.reduce((sum, c) => sum + c.option_pnl, 0),
    maxDrawdown: maxDrawdownOf(artifact.daily.map((d) => d.equity)),
    putsSold: artifact.ledger.filter((e) => e.kind === 'sell_put_open').length,
    callsSold: artifact.ledger.filter((e) => e.kind === 'sell_call_open').length,
    cyclesCompleted: artifact.cycles.filter((c) => !c.is_open).length,
    finalEquity,
    avgCollateral,
    annualizedReturnOnCollateral:
      avgCollateral && totalPnl !== null && calendarDays && calendarDays > 0
        ? (totalPnl / avgCollateral) * (365 / calendarDays)
        : null,
    calendarDays,
  };

  return {
    strategy,
    capitalBase,
    capitalBaseSource,
    ratiosSuppressed: capitalBase === null,
    suppressionReason: capitalBase === null ? CAPITAL_BASE_SUPPRESSED : null,
    grossPremium,
    fees,
    feeContracts,
    feeRatePerContract,
    netOptionCash: optionCash.total,
    netOptionCashSeries: optionCash.cumulative,
    monthly: optionCash.monthly,
    drawdownSeries: drawdown,
    deploymentSeries: deployed.series,
    deployment: deployed.reading,
    reconcile,
  };
}
