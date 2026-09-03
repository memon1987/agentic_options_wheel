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

import type {
  MonthlyCashflow,
  SimArtifact,
  SimBars,
  SimCycle,
  SimLedgerEvent,
} from '../../../types/v2';
import { parseOcc } from '../../../utils/format';
import { artifactStrategy, isWheelStrategy } from './normaliseArtifact';

// --------------------------------------------------------------------------- //
// The event sets, named once
// --------------------------------------------------------------------------- //

/** Cash RECEIVED for writing an option, before the fee deduction. */
export const OPEN_KINDS = ['sell_put_open', 'sell_call_open'] as const;

/**
 * The events that carry a fee.
 *
 * The fee RATE on the tile is `Σ fees ÷ Σ contracts` over THIS set, not over
 * the whole ledger: `expire_worthless` carries contracts and no fee, so mixing
 * the all-events fee sum with an open-events contract count prints $0.047 on
 * the captured fixture (1.68 ÷ 36) for an engine whose constant is $0.04. Same
 * set on top and bottom, or the label lies.
 */
export const FEE_BEARING_KINDS = ['sell_put_open', 'sell_call_open', 'buy_to_close'] as const;

/**
 * The option-lifecycle events whose cash movements are the option leg.
 *
 * Assignments and dividends are deliberately OUT: an assignment's `cash_delta`
 * is the share purchase or sale, which belongs to the stock leg. On the
 * captured fixture this set's cash sums to $4,453.49 — the row's `option_pnl`
 * exactly — which is the cheap check that the boundary is drawn where the
 * engine draws it.
 */
export const OPTION_CASH_KINDS = [
  'sell_put_open',
  'sell_call_open',
  'buy_to_close',
  'expire_worthless',
] as const;

const inSet = (kinds: readonly string[], kind: string): boolean => kinds.includes(kind);

// --------------------------------------------------------------------------- //
// Shapes
// --------------------------------------------------------------------------- //

export type CapitalBaseSource = 'stamped' | 'starting_cash';

export interface DeploymentReading {
  /**
   * Dollar-weighted mean over ALL decision days of
   * `(reserved_collateral + share value) / capital_base`.
   *
   * All days, not deployed days: "how much of the account was working" is a
   * question about the window, and averaging only the days with a position
   * answers a different one (on the captured fixture the reserved-only readings
   * are $14,328 all-days against $29,758 deployed-days — a 2× difference that
   * an unstated definition would hide).
   */
  ratio: number | null;
  /** `closes` = share value from the sidecar; `at cost` = the fallback. */
  basis: 'closes' | 'at cost' | 'none';
  days: number;
  /** The reserved-collateral component alone, in dollars. Pinned by test. */
  reservedMeanDollars: number;
  /** The share-value component alone, in dollars; `null` when unvaluable. */
  sharesValueMeanDollars: number | null;
  /** Days holding shares this module could not value. Non-zero ⇒ `ratio` null. */
  unresolvedShareDays: number;
  /**
   * Why those days could not be valued, for the tile (review round 1, F9).
   * `null` when there are none. An absent ratio with no reason beside it reads
   * as "not computed yet".
   */
  unresolvedReason: string | null;
}

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
  drawdownSeries: Array<{ date: string; equity: number; drawdown: number }>;
  deploymentSeries: Array<{
    date: string;
    reserved: number;
    sharesValue: number | null;
    ratio: number | null;
  }>;
  deployment: DeploymentReading;

  /** FOR THE TEST ONLY. Never rendered — the strip shows the ROW's numbers. */
  reconcile: DigestReconcile;
}

export interface DigestOptions {
  /** `spec.strategy` off the sweep row, when the run carries one. */
  specStrategy?: string | null;
}

// --------------------------------------------------------------------------- //
// Capital base
// --------------------------------------------------------------------------- //

export const CAPITAL_BASE_SUPPRESSED =
  'Capital base not stamped on this artifact, so every ratio on this cell would need a ' +
  'denominator this console had to guess. They are hidden rather than approximated: a ' +
  "covered-call cell divided by the wheel's starting cash prints a plausible, wrong " +
  'percentage under a correct-looking label.';

/**
 * THE denominator, resolved in the order §D-4 fixes.
 *
 * `provenance.capital_base` (PR-1's stamp) whenever present. Otherwise
 * `starting_cash` — but ONLY on a wheel artifact, where the two are the same
 * number by construction. On any other strategy the answer is "there isn't
 * one", which is a renderable state and a wrong denominator is not.
 */
export function resolveCapitalBase(
  artifact: SimArtifact,
  strategy: string,
): { base: number | null; source: CapitalBaseSource | null } {
  const stamped = artifact.provenance.capital_base;
  if (typeof stamped === 'number' && Number.isFinite(stamped) && stamped > 0) {
    return { base: stamped, source: 'stamped' };
  }
  const starting = artifact.provenance.starting_cash;
  if (isWheelStrategy(strategy) && typeof starting === 'number' && Number.isFinite(starting) && starting > 0) {
    return { base: starting, source: 'starting_cash' };
  }
  return { base: null, source: null };
}

// --------------------------------------------------------------------------- //
// Pieces
// --------------------------------------------------------------------------- //

/** Put or call, from the OCC symbol where there is one, else from the kind. */
function optionSide(event: SimLedgerEvent): 'put' | 'call' | null {
  const parsed = parseOcc(event.symbol);
  if (parsed.optionType === 'P') return 'put';
  if (parsed.optionType === 'C') return 'call';
  if (event.kind === 'sell_put_open' || event.kind === 'put_assignment') return 'put';
  if (event.kind === 'sell_call_open' || event.kind === 'call_assignment') return 'call';
  return null;
}

const monthOf = (date: string): string => date.slice(0, 7);

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

/** `fitness._max_drawdown`, including its collapse of sub-basis-point noise. */
export function maxDrawdownOf(equity: number[]): number {
  let peak = -Infinity;
  let worst = 0;
  for (const value of equity) {
    if (value > peak) peak = value;
    if (peak > 0) {
      const dd = (value - peak) / peak;
      if (dd < worst) worst = dd;
    }
  }
  return worst > -1e-9 ? 0 : worst;
}

/**
 * The cost basis to value shares at on `date` when there is no sidecar.
 *
 * The cycle with the LATEST start that still contains the day. An open cycle's
 * `end` is null, which extends it to the last decision day — the same reading
 * the engine's own cycle materialisation uses.
 */
function costBasisOn(cycles: SimCycle[], underlying: string, date: string): number | null {
  let best: SimCycle | null = null;
  for (const cycle of cycles) {
    if (cycle.underlying !== underlying) continue;
    if (cycle.cost_basis === null) continue;
    if (cycle.start > date) continue;
    if (cycle.end !== null && cycle.end < date) continue;
    if (!best || cycle.start > best.start) best = cycle;
  }
  return best ? best.cost_basis : null;
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
  const byDate = new Map<string, number>();
  const byMonth = new Map<string, MonthlyCashflow>();
  for (const event of artifact.ledger) {
    if (!inSet(OPTION_CASH_KINDS, event.kind)) continue;
    byDate.set(event.date, (byDate.get(event.date) ?? 0) + event.cash_delta);

    const month = monthOf(event.date);
    const row = byMonth.get(month) ?? {
      month,
      net_option_cashflow: 0,
      put_net_cashflow: 0,
      call_net_cashflow: 0,
      gross_premium: 0,
      buyback_cost: 0,
      event_count: 0,
    };
    row.net_option_cashflow += event.cash_delta;
    const side = optionSide(event);
    if (side === 'put') row.put_net_cashflow += event.cash_delta;
    else if (side === 'call') row.call_net_cashflow += event.cash_delta;
    if (inSet(OPEN_KINDS, event.kind)) row.gross_premium += event.cash_delta + event.fees;
    if (event.kind === 'buy_to_close') row.buyback_cost += -event.cash_delta;
    row.event_count += 1;
    byMonth.set(month, row);
  }
  const netOptionCashSeries: Array<{ date: string; value: number }> = [];
  let running = 0;
  for (const date of [...byDate.keys()].sort()) {
    running += byDate.get(date) as number;
    netOptionCashSeries.push({ date, value: running });
  }
  const monthly = [...byMonth.values()].sort((a, b) => a.month.localeCompare(b.month));

  // --- drawdown ---------------------------------------------------------- //
  const drawdownSeries: Array<{ date: string; equity: number; drawdown: number }> = [];
  let peak = -Infinity;
  for (const day of artifact.daily) {
    if (day.equity > peak) peak = day.equity;
    drawdownSeries.push({
      date: day.date,
      equity: day.equity,
      drawdown: peak > 0 ? (day.equity - peak) / peak : 0,
    });
  }

  // --- deployment -------------------------------------------------------- //
  //
  // The sidecar is ONE symbol's bars (review round 1, F9). It was being applied
  // to every underlying in `shares_held`: on a cell holding shares of anything
  // but the sidecar's symbol, the deployment ratio was that symbol's share count
  // priced at a DIFFERENT company's close — a plausible number under a
  // correct-looking label, which is the exact failure §D-4 suppresses ratios to
  // avoid. Today `shares_held` carries one underlying per cell, so this was
  // latent; a portfolio-level artifact would have made it wrong on arrival.
  const closes = new Map<string, number>();
  for (const bar of sidecar?.bars ?? []) closes.set(bar.date, bar.close);
  const sidecarSymbol = sidecar?.provenance.symbol ?? null;
  const hasCloses = closes.size > 0;

  const deploymentSeries: ArtifactDigest['deploymentSeries'] = [];
  let reservedSum = 0;
  let sharesValueSum = 0;
  let unresolvedShareDays = 0;
  let valuedAtCost = 0;
  const unvaluable = new Set<string>();
  for (const day of artifact.daily) {
    reservedSum += day.reserved_collateral;
    let sharesValue: number | null = 0;
    for (const [underlying, shares] of Object.entries(day.shares_held)) {
      if (shares === 0) continue;
      // The sidecar's close ONLY for the sidecar's own symbol. Anything else
      // falls back to the lot's cost basis, which is a stated approximation
      // ("at cost") rather than another company's price.
      const close =
        hasCloses && underlying === sidecarSymbol ? (closes.get(day.date) ?? null) : null;
      const price = close ?? costBasisOn(artifact.cycles, underlying, day.date);
      if (price === null) {
        sharesValue = null;
        unvaluable.add(underlying);
        break;
      }
      if (close === null) valuedAtCost += 1;
      sharesValue += shares * price;
    }
    if (sharesValue === null) unresolvedShareDays += 1;
    else sharesValueSum += sharesValue;
    deploymentSeries.push({
      date: day.date,
      reserved: day.reserved_collateral,
      sharesValue,
      ratio:
        capitalBase && sharesValue !== null
          ? (day.reserved_collateral + sharesValue) / capitalBase
          : null,
    });
  }
  const days = artifact.daily.length;
  const valuable = days > 0 && unresolvedShareDays === 0;
  const deployment: DeploymentReading = {
    ratio: valuable && capitalBase ? (reservedSum + sharesValueSum) / days / capitalBase : null,
    basis: days === 0 ? 'none' : !hasCloses || valuedAtCost > 0 ? 'at cost' : 'closes',
    days,
    reservedMeanDollars: days > 0 ? reservedSum / days : 0,
    sharesValueMeanDollars: valuable ? sharesValueSum / days : null,
    unresolvedShareDays,
    unresolvedReason: unresolvedShareDays
      ? `${unresolvedShareDays} day${unresolvedShareDays === 1 ? '' : 's'} held ` +
        `${[...unvaluable].sort().join(', ')} at a price this console could not establish — ` +
        'no bar in the sidecar for that symbol and no cycle cost basis covering the day. ' +
        'The ratio is withheld rather than computed over a partial position.'
      : null,
  };

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
    netOptionCash: netOptionCashSeries.length
      ? netOptionCashSeries[netOptionCashSeries.length - 1].value
      : 0,
    netOptionCashSeries,
    monthly,
    drawdownSeries,
    deploymentSeries,
    deployment,
    reconcile,
  };
}
