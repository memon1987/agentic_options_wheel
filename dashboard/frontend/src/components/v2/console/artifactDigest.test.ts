// FC-096 Phase E PR-2 (§D-4): the digest, reconciled to the ENGINE'S OWN ROW.
//
// The six `reconcile` equalities are the point of this file. Each one recomputes
// from the stored artifact a quantity `scenario_runs` already persists, and
// asserts the two agree on a REAL captured cell — `13cc2729d1c74211` / base /
// GOOGL / fit, the run the operator verified live in rollout step 1. That is
// what stops this module quietly becoming a second engine: a drift in any of
// the six shows up here rather than as two different numbers on one screen.
//
// The row is read out of the captured `shape_results` payload rather than
// retyped, so "the digest agrees with the row" is a statement about the server's
// numbers and not about numbers a test author copied.

import { describe, expect, it } from 'vitest';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import artifactA48d from '../../../test/fixtures/artifact_a48d_base_googl_fit.json';
import bars13cc from '../../../test/fixtures/bars_13cc_googl_fit.json';
import sweep13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import { normaliseArtifact, parseBars } from './normaliseArtifact';
import { computeDigest, maxDrawdownOf, resolveCapitalBase } from './artifactDigest';

const artifact = normaliseArtifact(artifact13cc)!;
const sidecar = parseBars(bars13cc).value!;
const row = sweep13cc.grid.fit.base.GOOGL;

describe('the six reconcile equalities, against the served row', () => {
  const { reconcile } = computeDigest(artifact, sidecar);

  it('option P&L: Σ cycles[].option_pnl === row.option_pnl', () => {
    expect(reconcile.optionPnl).toBeCloseTo(row.option_pnl, 6);
    expect(reconcile.optionPnl).toBeCloseTo(4453.4885311, 6);
  });

  it('max drawdown: the peak formula === row.max_drawdown', () => {
    expect(reconcile.maxDrawdown).toBeCloseTo(row.max_drawdown, 12);
    expect(reconcile.maxDrawdown).toBeCloseTo(-0.03375476, 8);
  });

  it('puts sold: count of sell_put_open === row.puts_sold', () => {
    expect(reconcile.putsSold).toBe(row.puts_sold);
    expect(reconcile.putsSold).toBe(23);
  });

  it('calls sold: count of sell_call_open === row.calls_sold', () => {
    expect(reconcile.callsSold).toBe(row.calls_sold);
    expect(reconcile.callsSold).toBe(13);
  });

  it('cycles completed: count of !is_open === row.cycles_completed', () => {
    expect(reconcile.cyclesCompleted).toBe(row.cycles_completed);
    expect(reconcile.cyclesCompleted).toBe(22);
  });

  it('final equity: daily[-1].equity === counters.final_equity === the row total return', () => {
    expect(reconcile.finalEquity).toBeCloseTo(artifact13cc.counters.final_equity, 6);
    // …and the row's `total_return` over the stamped capital base is the same
    // dollar figure, which is the equality that ties the artifact to the grid.
    expect(reconcile.finalEquity).toBeCloseTo(100_000 * (1 + row.total_return), 6);
  });
});

describe('the definitions §D-4 fixes, pinned on the captured fixture', () => {
  const digest = computeDigest(artifact, sidecar);

  it('gross premium is cash RECEIVED before fees, over the two open kinds', () => {
    expect(digest.grossPremium).toBeCloseTo(5835.54017, 4);
    // Strictly greater than net option cash: the buybacks and the fees are the
    // difference, and a "gross" that equalled the net would mean the fee
    // add-back had been dropped.
    expect(digest.grossPremium).toBeGreaterThan(digest.netOptionCash);
    expect(digest.netOptionCash).toBeCloseTo(row.option_pnl, 6);
  });

  it('the fee RATE divides the same event set it sums', () => {
    // 1.68 / 42 = $0.040 — the engine's `fees_per_contract` default. Dividing
    // the whole-ledger fee sum by the OPEN-event contract count gives 1.68/36 =
    // $0.047 for the same engine, which is the label lying about a constant.
    expect(digest.fees).toBeCloseTo(1.68, 6);
    expect(digest.feeContracts).toBe(42);
    expect(digest.feeRatePerContract).toBeCloseTo(0.04, 6);
    expect(digest.feeRatePerContract).not.toBeCloseTo(0.047, 3);
  });

  it('deployment is dollar-weighted over ALL decision days, not deployed days', () => {
    expect(digest.deployment.days).toBe(189);
    expect(digest.deployment.basis).toBe('closes');
    expect(digest.deployment.unresolvedShareDays).toBe(0);
    // The reserved-only component the reviewer computed by hand: $14,328 as an
    // all-days mean. The deployed-days mean of the same column is $29,758 — a
    // 2x difference, which is exactly why the definition is stated rather than
    // implied.
    expect(digest.deployment.reservedMeanDollars).toBeCloseTo(14_328.04, 2);
    expect(digest.deployment.reservedMeanDollars).not.toBeCloseTo(29_758.24, 2);
    expect(digest.deployment.sharesValueMeanDollars).toBeCloseTo(10_082.81, 2);
    expect(digest.deployment.ratio).toBeCloseTo(0.24410857, 8);
  });

  it('the monthly buckets sum to the cumulative series terminal value', () => {
    const summed = digest.monthly.reduce((s, m) => s + m.net_option_cashflow, 0);
    expect(summed).toBeCloseTo(digest.netOptionCash, 6);
    expect(digest.monthly).toHaveLength(10);
    expect(digest.monthly[0].month).toBe('2025-09');
  });

  it('the drawdown series minimum equals the reconciled max drawdown', () => {
    const worst = Math.min(...digest.drawdownSeries.map((d) => d.drawdown));
    expect(worst).toBeCloseTo(digest.reconcile.maxDrawdown, 12);
    expect(digest.drawdownSeries).toHaveLength(189);
  });
});

describe('the capital base, and what happens without one', () => {
  it('the stamp is preferred, and named as the source', () => {
    const digest = computeDigest(artifact, sidecar);
    expect(digest.capitalBase).toBe(100_000);
    expect(digest.capitalBaseSource).toBe('stamped');
    expect(digest.ratiosSuppressed).toBe(false);
  });

  it('a WHEEL artifact with no stamp falls back to starting_cash', () => {
    // The pre-PR-1 capture: the two are the same number by construction on a
    // wheel replay, so the fallback is honest here and only here.
    const pre = normaliseArtifact(artifactA48d)!;
    const digest = computeDigest(pre, null);
    expect(digest.capitalBase).toBe(100_000);
    expect(digest.capitalBaseSource).toBe('starting_cash');
    expect(digest.ratiosSuppressed).toBe(false);
  });

  it('a COVERED-CALL artifact with no stamp suppresses every ratio', () => {
    // The mutation this kills: falling back to `starting_cash` regardless of
    // strategy. On a CC cell that is the $5k float, not the lot value, and the
    // result is a plausible wrong percentage under a correct-looking label.
    const cc = normaliseArtifact({
      ...artifactA48d,
      provenance: { ...artifactA48d.provenance, strategy: 'covered_call' },
    })!;
    const digest = computeDigest(cc, null);
    expect(digest.strategy).toBe('covered_call');
    expect(digest.capitalBase).toBeNull();
    expect(digest.capitalBaseSource).toBeNull();
    expect(digest.ratiosSuppressed).toBe(true);
    expect(digest.suppressionReason).toMatch(/not stamped/);
    expect(digest.deployment.ratio).toBeNull();
    expect(digest.deploymentSeries.every((d) => d.ratio === null)).toBe(true);
    // The DOLLAR tiles still render: they need no denominator.
    expect(digest.grossPremium).toBeGreaterThan(0);
    expect(digest.fees).toBeGreaterThan(0);
  });

  it('a CC artifact WITH a stamped base computes normally', () => {
    const cc = normaliseArtifact({
      ...artifact13cc,
      provenance: { ...artifact13cc.provenance, strategy: 'covered_call', capital_base: 36_185 },
    })!;
    const digest = computeDigest(cc, sidecar);
    expect(digest.capitalBase).toBe(36_185);
    expect(digest.ratiosSuppressed).toBe(false);
  });

  it('resolveCapitalBase refuses a zero or negative base', () => {
    const zeroed = normaliseArtifact({
      ...artifact13cc,
      provenance: { ...artifact13cc.provenance, capital_base: 0, starting_cash: 0 },
    })!;
    expect(resolveCapitalBase(zeroed, 'wheel')).toEqual({ base: null, source: null });
  });
});

describe('deployment without a sidecar', () => {
  it('falls back to the cycle cost basis and says so', () => {
    const digest = computeDigest(artifact, null);
    expect(digest.deployment.basis).toBe('at cost');
    expect(digest.deployment.unresolvedShareDays).toBe(0);
    expect(digest.deployment.reservedMeanDollars).toBeCloseTo(14_328.04, 2);
    // At cost rather than at market: a different number, correctly labelled,
    // never silently substituted for the closes basis.
    expect(digest.deployment.ratio).not.toBeCloseTo(0.24410857, 6);
    expect(digest.deployment.ratio).toBeGreaterThan(0);
  });
});

describe('maxDrawdownOf', () => {
  it('collapses sub-basis-point noise to a flat zero, like the engine', () => {
    expect(maxDrawdownOf([100, 100, 100])).toBe(0);
    expect(maxDrawdownOf([100, 100 - 1e-9])).toBe(0);
    expect(maxDrawdownOf([100, 50])).toBeCloseTo(-0.5, 12);
    // Recovery does not erase the trough.
    expect(maxDrawdownOf([100, 50, 200])).toBeCloseTo(-0.5, 12);
    expect(maxDrawdownOf([])).toBe(0);
  });
});
