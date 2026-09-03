// FC-060 Layer 4 (PR-B): the wire-shape adapter.
//
// FIXTURES. `sweep_shaped_*.json` are REAL output from PR-A's
// `dashboard/backend/services/sweeps.py::shape_results` (branch
// `fc-060/scenario-store-api`, 344b1ac), fed rows produced by the engine's own
// `report.py::render_json` over a synthetic SweepResult — so every key, every
// count and every verbatim caveat string is the producer's, not a guess. The
// three fields the contract adds concurrently (`scenario_hashes`,
// `scenario_config_hashes`, `windows`) and the three the router stamps on
// (`status`, `run_id`, `stuck`) are added to the fixtures by hand.
//
// The dev chain cache is empty (0 files), so a live `--command sweep` would have
// needed a full cold vendor materialisation. Shapes are exact; numbers are
// synthetic.

import { describe, expect, it } from 'vitest';
import { normaliseReport, normaliseSweepDetail, normaliseSweepList } from './normaliseReport';
import type { SweepReport, SweepRow } from '../../../types/v2';
import shapedHoldout from '../../../test/fixtures/sweep_shaped_holdout.json';
import shapedInsample from '../../../test/fixtures/sweep_shaped_insample.json';
import shapedPending from '../../../test/fixtures/sweep_shaped_pending.json';
import shapedUnknown from '../../../test/fixtures/sweep_shaped_unknown.json';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';

const sweep = (over: Partial<SweepRow> = {}): SweepRow =>
  ({
    run_id: '0f1e2d3c4b5a6978',
    sweep_key: 'deadbeefdeadbeef',
    status: 'done',
    base_config_hash: 'a1b2c3d4e5f60718',
    wall_seconds: 98.4,
    provider_fetches: 4,
    bar_cache_hits: 812,
    ...over,
  }) as SweepRow;

const cellOf = (report: SweepReport, scenario: string, symbol: string, split: string) =>
  report.rows.find((r) => r.scenario === scenario && r.symbol === symbol && r.split === split);

describe('normaliseReport — the grid is expanded back into rows', () => {
  const out = () => normaliseReport(shapedHoldout, sweep())!;

  it('rebuilds one row per grid cell, in spec order', () => {
    const report = out();
    expect(report.scenarios).toEqual(['base', 'puts_15_25', 'call_floor_0_50', 'at_the_bid']);
    expect(report.symbols).toEqual(['AAPL', 'NVDA']);
    expect(report.windows.map((w) => w.split)).toEqual(['fit', 'holdout']);
    expect(report.rows).toHaveLength(4 * 2 * 2);
  });

  it('expands `state` back into the partition booleans', () => {
    const report = out();
    const measured = cellOf(report, 'base', 'AAPL', 'fit')!;
    expect([measured.measured, measured.insufficient, measured.low_activity, !!measured.error])
      .toEqual([true, false, false, false]);

    const lowAct = cellOf(report, 'puts_15_25', 'NVDA', 'fit')!;
    expect([lowAct.measured, lowAct.insufficient, lowAct.low_activity, !!lowAct.error])
      .toEqual([false, false, true, false]);
    expect(lowAct.days_in_position_fraction).toBe(0.09);

    const insuf = cellOf(report, 'puts_15_25', 'NVDA', 'holdout')!;
    expect([insuf.measured, insuf.insufficient, insuf.low_activity, !!insuf.error])
      .toEqual([false, true, false, false]);

    const errored = cellOf(report, 'call_floor_0_50', 'AAPL', 'fit')!;
    expect(errored.measured).toBe(false);
    expect(errored.error).toMatch(/UnadjustedCorporateAction/);
  });

  it('expands an `unknown` state to NONE of the four — never to measured', () => {
    const row = cellOf(normaliseReport(shapedUnknown, sweep())!, 'at_the_bid', 'AAPL', 'fit')!;
    expect([row.measured, row.insufficient, row.low_activity, !!row.error])
      .toEqual([false, false, false, false]);
    // The row still carries a return. Nothing may present it as one.
    expect(row.annualized_return).toBe(0.071);
  });

  it('reads the windows off the payload rather than deriving them from the spec', () => {
    const report = out();
    expect(report.windows).toEqual([
      { split: 'fit', start: '2026-03-01', end: '2026-04-30' },
      { split: 'holdout', start: '2026-05-01', end: '2026-05-29' },
    ]);
  });
});

describe('normaliseReport — aggregates are READ, never derived', () => {
  it('passes the server summary straight through', () => {
    const report = normaliseReport(shapedHoldout, sweep())!;
    const fixture = (shapedHoldout as { summary: unknown[] }).summary;
    expect(report.summary).toHaveLength(fixture.length);
    const row = report.summary.find((s) => s.scenario === 'puts_15_25' && s.split === 'fit')!;
    expect(row).toMatchObject({
      median: 0.241,
      min: 0.241,
      max: 0.241,
      measured: 1,
      insufficient: 0,
      low_activity: 1,
      errors: 0,
      delta_symbols: 1,
    });
  });

  it('passes the deltas, sign agreement and verbatim footers through untouched', () => {
    const f = shapedHoldout as unknown as SweepReport;
    const report = normaliseReport(shapedHoldout, sweep())!;
    expect(report.delta_vs_base).toEqual(f.delta_vs_base);
    expect(report.sign_agreement).toEqual(f.sign_agreement);
    expect(report.cross_scenario_caveat).toBe(f.cross_scenario_caveat);
    expect(report.rejection_tally_caveat).toBe(f.rejection_tally_caveat);
    expect(report.known_biases).toEqual(f.known_biases);
    expect(report.min_days_in_position).toBe(0.25);
  });
});

describe('normaliseReport — provenance', () => {
  it('reads the per-arm hashes off the payload', () => {
    const report = normaliseReport(shapedHoldout, sweep())!;
    expect(report.scenario_hashes).toEqual({
      base: '0000000000000000',
      puts_15_25: '1111111111111111',
      call_floor_0_50: '2222222222222222',
      at_the_bid: '3333333333333333',
    });
    // `at_the_bid` varies only the FILL, which config_hash does not cover — so
    // its config_hash equals base's. The UI marks that "= base".
    expect(report.scenario_config_hashes?.at_the_bid).toBe(
      report.scenario_config_hashes?.base,
    );
    expect(report.scenario_config_hashes?.puts_15_25).not.toBe(
      report.scenario_config_hashes?.base,
    );
  });

  it('recovers timings and provider counts the shaped payload drops, off the sweep row', () => {
    const report = normaliseReport(shapedHoldout, sweep())!;
    expect(report.base_config_hash).toBe('a1b2c3d4e5f60718');
    expect(report.timing?.wall_seconds).toBe(98.4);
    expect(report.provider_calls?.fetches).toBe(4);
    expect(report.provider_calls?.bar_cache_hits).toBe(812);
  });

  it('reads the arms’ overrides and haircuts off the spec', () => {
    const report = normaliseReport(shapedHoldout, sweep())!;
    expect(report.scenario_overrides?.puts_15_25).toEqual({
      'strategy.put_delta_range': [0.15, 0.25],
    });
    // A haircut-only arm: no override, a haircut. The UI must not read it as a
    // strategy variant.
    expect(report.scenario_overrides?.at_the_bid).toEqual({});
    expect(report.scenario_fill_haircuts?.at_the_bid).toBe(1);
  });
});

describe('normaliseReport — in-sample runs', () => {
  it('carries in_sample_only, the banner text and no sign agreement', () => {
    const report = normaliseReport(shapedInsample, sweep())!;
    expect(report.in_sample_only).toBe(true);
    expect(report.in_sample_banner).toMatch(/IN-SAMPLE ONLY/);
    expect(report.holdout_semantics).toBeNull();
    expect(report.sign_agreement).toBeNull();
    expect(report.windows.map((w) => w.split)).toEqual(['all']);
  });
});

describe('normaliseReport — refusals', () => {
  it('returns null rather than an empty grid for an unreadable payload', () => {
    expect(normaliseReport(null, null)).toBeNull();
    expect(normaliseReport('nope', null)).toBeNull();
    expect(normaliseReport({ status: 'running' }, null)).toBeNull();
  });

  it('returns null for a run that has not produced a grid yet', () => {
    // `grid: {}` and `splits: []` — exactly what the router serves a
    // `submitted` run. Rendering that would claim the sweep measured nothing.
    expect(normaliseReport(shapedPending, sweep({ status: 'submitted' }))).toBeNull();
  });

  it('skips a null grid cell instead of inventing an empty row', () => {
    const holed = JSON.parse(JSON.stringify(shapedHoldout));
    holed.grid.fit.base.NVDA = null;
    const report = normaliseReport(holed, sweep())!;
    expect(cellOf(report, 'base', 'NVDA', 'fit')).toBeUndefined();
    expect(report.rows).toHaveLength(15);
  });
});

describe('normaliseSweepDetail — the bare shape_results envelope', () => {
  it('reads the run row off `run`, with the router’s top-level values winning', () => {
    const detail = normaliseSweepDetail(shapedHoldout)!;
    expect(detail.sweep.run_id).toBe('0f1e2d3c4b5a6978');
    expect(detail.sweep.status).toBe('done');
    expect(detail.sweep.stuck).toBe(false);
    expect(detail.results?.rows).toHaveLength(16);
  });

  it('keeps the raw payload for a faithful export', () => {
    const detail = normaliseSweepDetail(shapedHoldout)!;
    expect(detail.raw).toBe(shapedHoldout);
  });

  it.each(['submitted', 'running', 'failed', 'deduplicated'] as const)(
    'gives a %s run NULL results, whatever the grid says',
    (status) => {
      const payload = { ...JSON.parse(JSON.stringify(shapedHoldout)), status };
      const detail = normaliseSweepDetail(payload)!;
      expect(detail.sweep.status).toBe(status);
      // The fixture has a FULL grid. A non-done status must still refuse it:
      // a status is the authority on whether a run finished, not a grid.
      expect(detail.results).toBeNull();
    },
  );

  it('gives a pending run null results and a submitted status', () => {
    const detail = normaliseSweepDetail(shapedPending)!;
    expect(detail.sweep.status).toBe('submitted');
    expect(detail.results).toBeNull();
  });

  it('returns null when there is no run row at all', () => {
    expect(normaliseSweepDetail({ status: 'done' })).toBeNull();
    expect(normaliseSweepDetail({ run: { status: 'done' } })).toBeNull();
    expect(normaliseSweepDetail(null)).toBeNull();
  });

  it('falls back to the top-level run_id when the row omits it', () => {
    const payload = JSON.parse(JSON.stringify(shapedHoldout));
    delete payload.run.run_id;
    expect(normaliseSweepDetail(payload)!.sweep.run_id).toBe('0f1e2d3c4b5a6978');
  });
});

describe('normaliseSweepList — the list endpoint is a BARE ARRAY', () => {
  it('accepts a bare array', () => {
    const rows = [{ run_id: 'a' }, { run_id: 'b' }];
    expect(normaliseSweepList(rows)).toHaveLength(2);
    expect(normaliseSweepList(rows)[0].run_id).toBe('a');
  });

  it('also accepts a {sweeps: [...]} envelope rather than silently emptying the page', () => {
    expect(normaliseSweepList({ sweeps: [{ run_id: 'a' }] })).toHaveLength(1);
  });

  it('returns an empty array for anything else', () => {
    expect(normaliseSweepList(null)).toEqual([]);
    expect(normaliseSweepList('nope')).toEqual([]);
    expect(normaliseSweepList({ items: [] })).toEqual([]);
  });
});

describe('the forecast is coerced, not cast (review round 1, F9)', () => {
  it('passes a real forecast through with its numbers intact', () => {
    const report = normaliseSweepDetail(shaped13cc)!.results!;
    const base = report.forecast!.by_scenario.base;
    expect(report.forecast!.capital_base).toBe(100000);
    expect(base.symbols.GOOGL.fill).toEqual({
      basis: 'mid',
      fill_haircut: 0.25,
      is_engine_default: true,
    });
    expect(base.portfolio.net_option_pnl!.annual_low).toBeCloseTo(5932.5668389, 6);
    expect(base.portfolio.included).toEqual(['GOOGL']);
    // `position_20pct` was excluded in both windows: refusal string, null sums.
    const arm = report.forecast!.by_scenario.position_20pct;
    expect(arm.portfolio.net_option_pnl).toBeNull();
    expect(arm.portfolio.refusal).toMatch(/nothing to sum/);
    expect(arm.portfolio.excluded).toEqual({ GOOGL: 'fit: insufficient' });
  });

  it('turns a non-number into null rather than letting NaN reach the screen', () => {
    // This used to be a bare cast, so every rate the panel divides, multiplies
    // or formats arrived unchecked — a string, a `"NaN"` or a missing key would
    // have rendered as `NaN%` with no way to tell an unreadable forecast from a
    // refused one.
    const poisoned = {
      ...shaped13cc,
      forecast: {
        ...shaped13cc.forecast,
        capital_base: 'one hundred thousand',
        days: { fit: '189', holdout: null },
        by_scenario: {
          base: {
            symbols: {
              GOOGL: {
                fill: { basis: 42, fill_haircut: 'a quarter' },
                days: {},
                capital_base: Number.NaN,
                net_option_pnl: { annual_low: 'lots', annual_high: null },
                total_pnl: null,
              },
            },
            portfolio: {
              included: ['GOOGL', 7],
              excluded: { GOOGL: 3 },
              n_included: 'one',
              net_option_pnl: { annual_low: {} },
            },
          },
        },
      },
    };
    const forecast = normaliseSweepDetail(poisoned)!.results!.forecast!;
    expect(forecast.capital_base).toBeNull();
    expect(forecast.days).toEqual({ fit: null, holdout: null });
    const cell = forecast.by_scenario.base.symbols.GOOGL;
    expect(cell.fill).toEqual({ basis: null, fill_haircut: null });
    expect(cell.capital_base).toBeNull();
    expect(cell.net_option_pnl.annual_low).toBeNull();
    // `total_pnl: null` still yields the all-null shape the panel can render,
    // never `undefined.annual_low`.
    expect(cell.total_pnl.annual_high).toBeNull();
    const portfolio = forecast.by_scenario.base.portfolio;
    expect(portfolio.included).toEqual(['GOOGL']);
    expect(portfolio.excluded).toEqual({});
    expect(portfolio.n_included).toBe(0);
    expect(portfolio.net_option_pnl!.annual_low).toBeNull();
    // Nothing anywhere in the tree is NaN.
    expect(JSON.stringify(forecast)).not.toMatch(/NaN/);
  });

  it('a forecast that is not an object is null, not an empty shell', () => {
    for (const bad of [null, 'no', 7, []]) {
      const out = normaliseSweepDetail({ ...shaped13cc, forecast: bad })!.results!.forecast;
      expect(out).toBeNull();
    }
  });
});
