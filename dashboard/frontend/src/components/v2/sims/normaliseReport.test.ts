// FC-060 Layer 4 (PR-B): the wire-shape adapter.
//
// The fixtures are BOTH real producer output for the SAME synthetic sweep:
//
//   sweep_report_*.json — `src/backtesting/scenarios/report.py::render_json`
//   sweep_shaped_*.json — PR-A's `dashboard/backend/services/sweeps.py::shape_results`
//
// So this file is where the two contracts are pinned against each other. The
// assertion that matters is not "the adapter runs" but "both shapes produce the
// same verdict for the same cell" — if PR-A and the runner ever disagree about
// which cells are measured, the grid would show one thing and the medians
// another, and nothing else in this codebase would notice.

import { describe, expect, it } from 'vitest';
import { normaliseReport, normaliseSweepDetail } from './normaliseReport';
import type { SweepReport, SweepRow } from '../../../types/v2';
import renderJsonHoldout from '../../../test/fixtures/sweep_report_holdout.json';
import renderJsonInsample from '../../../test/fixtures/sweep_report_insample.json';
import shapedHoldout from '../../../test/fixtures/sweep_shaped_holdout.json';
import shapedInsample from '../../../test/fixtures/sweep_shaped_insample.json';

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

describe('normaliseReport — render_json passes through untouched', () => {
  it('recognises the flat `rows` + `windows` shape', () => {
    const out = normaliseReport(renderJsonHoldout, sweep())!;
    expect(out.rows).toHaveLength((renderJsonHoldout as { rows: unknown[] }).rows.length);
    expect(out.windows.map((w) => w.split)).toEqual(['fit', 'holdout']);
    expect(out.scenario_hashes?.puts_15_25).toBe('1111111111111111');
  });
});

describe('normaliseReport — shape_results is expanded back into rows', () => {
  const out = () => normaliseReport(shapedHoldout, sweep())!;

  it('rebuilds one row per grid cell, in spec order', () => {
    const report = out();
    expect(report.scenarios).toEqual(['base', 'puts_15_25', 'call_floor_0_50']);
    expect(report.symbols).toEqual(['AAPL', 'NVDA']);
    expect(report.windows.map((w) => w.split)).toEqual(['fit', 'holdout']);
    expect(report.rows).toHaveLength(3 * 2 * 2);
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

  it('AGREES WITH render_json on every cell’s state — the load-bearing assertion', () => {
    const fromShape = out();
    const fromJson = normaliseReport(renderJsonHoldout, sweep())!;
    for (const row of fromJson.rows) {
      const other = cellOf(fromShape, row.scenario, row.symbol, row.split);
      expect(other, `${row.scenario}/${row.symbol}/${row.split}`).toBeDefined();
      expect({
        measured: other!.measured,
        insufficient: other!.insufficient,
        low_activity: other!.low_activity,
        errored: !!other!.error,
        annualized_return: other!.annualized_return,
      }).toEqual({
        measured: row.measured,
        insufficient: row.insufficient,
        low_activity: row.low_activity,
        errored: !!row.error,
        annualized_return: row.annualized_return,
      });
    }
  });

  it('carries the deltas, sign agreement and verbatim footers straight through', () => {
    const fromShape = out();
    const fromJson = normaliseReport(renderJsonHoldout, sweep())!;
    expect(fromShape.delta_vs_base).toEqual(fromJson.delta_vs_base);
    expect(fromShape.sign_agreement).toEqual(fromJson.sign_agreement);
    expect(fromShape.cross_scenario_caveat).toBe(fromJson.cross_scenario_caveat);
    expect(fromShape.rejection_tally_caveat).toBe(fromJson.rejection_tally_caveat);
    expect(fromShape.known_biases).toEqual(fromJson.known_biases);
    expect(fromShape.min_days_in_position).toBe(0.25);
  });

  it('rebuilds the fit window as ending the day BEFORE the holdout starts', () => {
    const report = out();
    const fit = report.windows.find((w) => w.split === 'fit')!;
    const holdout = report.windows.find((w) => w.split === 'holdout')!;
    expect(fit.start).toBe('2026-03-01');
    expect(fit.end).toBe('2026-04-30'); // holdout starts 2026-05-01
    expect(holdout.start).toBe('2026-05-01');
    expect(holdout.end).toBe('2026-05-29');
    // Byte-identical to what the runner itself reported.
    expect(report.windows).toEqual(
      (renderJsonHoldout as unknown as SweepReport).windows,
    );
  });

  it('recovers provenance the shaped payload drops, off the sweep row', () => {
    const report = out();
    expect(report.base_config_hash).toBe('a1b2c3d4e5f60718');
    expect(report.timing?.wall_seconds).toBe(98.4);
    expect(report.provider_calls?.fetches).toBe(4);
    expect(report.provider_calls?.bar_cache_hits).toBe(812);
    // Overrides come off the spec, so the provenance table is still populated.
    expect(report.scenario_overrides?.puts_15_25).toEqual({
      'strategy.put_delta_range': [0.15, 0.25],
    });
    expect(report.scenario_fill_haircuts?.call_floor_0_50).toBe(1);
  });

  it('carries in_sample_only and the banner on a run with no holdout', () => {
    const report = normaliseReport(shapedInsample, sweep())!;
    expect(report.in_sample_only).toBe(true);
    expect(report.in_sample_banner).toMatch(/IN-SAMPLE ONLY/);
    expect(report.holdout_semantics).toBeNull();
    expect(report.sign_agreement).toBeNull();
    expect(report.windows.map((w) => w.split)).toEqual(['all']);
    // And the flat form says the same thing about the same sweep.
    expect((renderJsonInsample as unknown as SweepReport).in_sample_only).toBe(true);
  });
});

describe('normaliseReport — refusals', () => {
  it('returns null rather than an empty grid for an unreadable payload', () => {
    expect(normaliseReport(null, null)).toBeNull();
    expect(normaliseReport('nope', null)).toBeNull();
    expect(normaliseReport({ status: 'running' }, null)).toBeNull();
  });

  it('skips a null grid cell instead of inventing an empty row', () => {
    const holed = JSON.parse(JSON.stringify(shapedHoldout));
    holed.grid.fit.base.NVDA = null;
    const report = normaliseReport(holed, sweep())!;
    expect(cellOf(report, 'base', 'NVDA', 'fit')).toBeUndefined();
    expect(report.rows).toHaveLength(11);
  });

  it('treats an unrecognised cell state as none of the four, not as measured', () => {
    const odd = JSON.parse(JSON.stringify(shapedHoldout));
    odd.grid.fit.base.AAPL.state = 'quarantined';
    const row = cellOf(normaliseReport(odd, sweep())!, 'base', 'AAPL', 'fit')!;
    expect([row.measured, row.insufficient, row.low_activity, !!row.error])
      .toEqual([false, false, false, false]);
  });
});

describe('normaliseSweepDetail — both envelopes', () => {
  it('accepts {sweep, results}', () => {
    const detail = normaliseSweepDetail({ sweep: sweep(), results: renderJsonHoldout })!;
    expect(detail.sweep.run_id).toBe('0f1e2d3c4b5a6978');
    expect(detail.results?.rows.length).toBeGreaterThan(0);
  });

  it('accepts a bare shape_results payload, reading the row off `run`', () => {
    const detail = normaliseSweepDetail(shapedHoldout)!;
    expect(detail.sweep.run_id).toBe('0f1e2d3c4b5a6978');
    expect(detail.sweep.status).toBe('done');
    expect(detail.results?.rows).toHaveLength(12);
  });

  it('returns a sweep with null results when the run has produced none yet', () => {
    const detail = normaliseSweepDetail({ sweep: sweep({ status: 'running' }), results: null })!;
    expect(detail.sweep.status).toBe('running');
    expect(detail.results).toBeNull();
  });

  it('returns null when there is no run row at all', () => {
    expect(normaliseSweepDetail({ results: renderJsonHoldout })).toBeNull();
    expect(normaliseSweepDetail({ sweep: { status: 'done' } })).toBeNull();
  });
});
