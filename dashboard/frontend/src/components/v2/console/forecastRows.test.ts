// FC-096 Phase E PR-3 (§Forecast): horizon scaling against the SERVER's own
// annual pair, on the real payload of run `13cc2729d1c74211`.
//
// The `H = 365` equality is the whole point: the server serves `annual_low` /
// `annual_high` for exactly one horizon, so scaling the per-day rate by 365 and
// landing on those two numbers proves the UI is multiplying and not deriving.

import { describe, expect, it } from 'vitest';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { defaultHorizon, forecastRows } from './forecastRows';

const report = normaliseSweepDetail(shaped13cc)!.results!;
const forecast = report.forecast!;

describe('forecastRows — the served run', () => {
  it('defaults to the HOLDOUT window’s length, not to a year', () => {
    // The mutation this kills: a hardcoded 365. The default reads as "the next
    // period like the one you held out"; a year reads as a promise.
    expect(defaultHorizon(forecast)).toBe(90);
    expect(defaultHorizon(forecast)).toBe(forecast.days.holdout);
    expect(forecast.horizon_choices).toEqual([30, 90, 365]);
  });

  it('scales per-day rates to the horizon and matches the server at H = 365', () => {
    const view = forecastRows(forecast, 'base', 'GOOGL', 365);
    const served = forecast.by_scenario.base.symbols.GOOGL;
    const premium = view.symbol!.ranges.find((r) => r.basis === 'net_option_pnl')!;
    const total = view.symbol!.ranges.find((r) => r.basis === 'total_pnl')!;
    expect(premium.low).toBeCloseTo(served.net_option_pnl.annual_low as number, 6);
    expect(premium.high).toBeCloseTo(served.net_option_pnl.annual_high as number, 6);
    expect(total.low).toBeCloseTo(served.total_pnl.annual_low as number, 6);
    expect(total.high).toBeCloseTo(served.total_pnl.annual_high as number, 6);
    expect(premium.low).toBeCloseTo(5932.5668388936465, 6);
    expect(total.high).toBeCloseTo(8993.771218455684, 6);
  });

  it('renders BOTH bases, in premium-then-total order, never one alone', () => {
    // The bases invert on this fixture: premium rises fit -> holdout while
    // total P&L falls. A panel showing only premium would read as improvement.
    const view = forecastRows(forecast, 'base', 'GOOGL', 90);
    expect(view.symbol!.ranges.map((r) => r.basis)).toEqual(['net_option_pnl', 'total_pnl']);
    const [premium, total] = view.symbol!.ranges;
    expect(premium.fitPerDay!).toBeLessThan(premium.holdoutPerDay!);
    expect(total.fitPerDay!).toBeGreaterThan(total.holdoutPerDay!);
  });

  it('marks extrapolation only past the holdout window', () => {
    expect(forecastRows(forecast, 'base', 'GOOGL', 90).extrapolationFactor).toBeNull();
    expect(forecastRows(forecast, 'base', 'GOOGL', 30).extrapolationFactor).toBeNull();
    expect(forecastRows(forecast, 'base', 'GOOGL', 365).extrapolationFactor).toBeCloseTo(
      365 / 90,
      9,
    );
  });

  it('labels a portfolio sum with n_summed — never n_included', () => {
    // PR-1's §Execution note, binding on this PR: a BASIS can drop a symbol
    // that `included` still lists, so `n_included` beside a sum overstates it.
    const view = forecastRows(forecast, 'base', 'GOOGL', 90);
    const portfolio = view.portfolio!;
    expect(portfolio.ranges.every((r) => r.nSummed !== null)).toBe(true);
    expect(portfolio.ranges[0].nSummed).toBe(
      forecast.by_scenario.base.portfolio.net_option_pnl!.n_summed,
    );
    expect(portfolio.ranges[0].nSummed).toBe(1);
    expect(portfolio.nSymbols).toBe(1);
  });

  it('carries the fill stamp, including the engine-default flag', () => {
    const view = forecastRows(forecast, 'base', 'GOOGL', 90);
    expect(view.symbol!.fill).toEqual({ basis: 'mid', fill_haircut: 0.25, is_engine_default: true });
    expect(view.symbol!.capitalBase).toBe(100000);
  });
});

describe('forecastRows — refusals and exclusions', () => {
  it('refuses an arm with no measured pair, in the server’s words', () => {
    const view = forecastRows(forecast, 'position_20pct', 'GOOGL', 90);
    expect(view.symbol).toBeNull();
    expect(view.refusal).toContain('GOOGL is excluded');
    expect(view.refusal).toContain('fit: insufficient');
    expect(view.portfolio!.refusal).toBe(
      'No symbol in this arm was measured in BOTH windows, so there is nothing to sum.',
    );
    expect(view.portfolio!.ranges.every((r) => r.low === null && r.high === null)).toBe(true);
  });

  it('names every symbol excluded from a basis’ sum', () => {
    const view = forecastRows(forecast, 'position_20pct', 'GOOGL', 90);
    for (const range of view.portfolio!.ranges) {
      expect(range.excluded).toEqual([{ symbol: 'GOOGL', reason: 'fit: insufficient' }]);
    }
  });

  it('never turns a NULL rate into a zero', () => {
    const nulled = {
      ...forecast,
      by_scenario: {
        base: {
          ...forecast.by_scenario.base,
          symbols: {
            GOOGL: {
              ...forecast.by_scenario.base.symbols.GOOGL,
              total_pnl: {
                fit_per_day: null,
                holdout_per_day: null,
                low_per_day: null,
                high_per_day: null,
                annual_low: null,
                annual_high: null,
              },
            },
          },
        },
      },
    };
    const view = forecastRows(nulled, 'base', 'GOOGL', 365);
    const total = view.symbol!.ranges.find((r) => r.basis === 'total_pnl')!;
    expect(total.low).toBeNull();
    expect(total.high).toBeNull();
  });

  it('refuses when the run carries no forecast at all', () => {
    const view = forecastRows(null, 'base', 'GOOGL', 90);
    expect(view.available).toBe(false);
    expect(view.refusal).toBe('This run carries no forecast.');
    expect(view.symbol).toBeNull();
    expect(view.portfolio).toBeNull();
  });
});
