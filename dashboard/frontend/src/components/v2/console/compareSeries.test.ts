// FC-096 Phase E PR-5: the two-sided equity series.
//
// The real cell artifact and the real bars sidecar of run `13cc2729d1c74211`
// anchor the dollar path; the rebasing and the benchmark-sharing rule are
// exercised on small hand-built inputs where the arithmetic is checkable by eye.

import { describe, expect, it } from 'vitest';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import bars13cc from '../../../test/fixtures/bars_13cc_googl_fit.json';
import { parseArtifact, parseBars } from './normaliseArtifact';
import { compareEquity, nonOverlap, type CompareEquityInput } from './compareSeries';
import type { SimArtifact, SimBars } from '../../../types/v2';

const real = parseArtifact(artifact13cc).value as SimArtifact;
const realBars = parseBars(bars13cc).value as SimBars;

/** A tiny artifact carrying nothing but a daily equity series. */
const stub = (points: Array<[string, number]>): SimArtifact =>
  ({
    ...real,
    daily: points.map(([date, equity]) => ({
      date,
      equity,
      cash: equity,
      reserved_collateral: 0,
      open_options: 0,
      shares_held: 0,
    })),
  }) as unknown as SimArtifact;

const barsWith = (
  overrides: Partial<NonNullable<SimBars['buy_and_hold']>>,
): SimBars =>
  ({
    ...realBars,
    buy_and_hold: { ...realBars.buy_and_hold!, ...overrides },
  }) as SimBars;

const input = (over: Partial<CompareEquityInput> = {}): CompareEquityInput => ({
  cell: null,
  base: null,
  bars: null,
  ...over,
});

describe('compareEquity — dollars when the capital bases agree', () => {
  const result = compareEquity(
    input({ cell: real, bars: realBars }),
    input({ cell: real, bars: realBars }),
    false,
  );

  it('carries the engine’s own dollars, untouched', () => {
    expect(result.base100).toBe(false);
    expect(result.unit).toBe('dollars');
    const first = result.rows[0];
    expect(first.aEquity).toBe(real.daily[0].equity);
    expect(first.bEquity).toBe(real.daily[0].equity);
  });

  it('spans every date either side carries, sorted', () => {
    const dates = result.rows.map((r) => r.date);
    expect([...dates].sort()).toEqual(dates);
    expect(dates.length).toBeGreaterThanOrEqual(real.daily.length);
  });

  it('draws ONE buy-and-hold line when both sidecars are the same benchmark', () => {
    expect(result.sharedBenchmark).toBe(true);
    expect(result.benchmarkNote).toContain('One buy-and-hold line');
    expect(result.rows.some((r) => r.benchmark !== null)).toBe(true);
    expect(result.rows.every((r) => r.aBenchmark === null && r.bBenchmark === null)).toBe(true);
  });
});

describe('compareEquity — base 100 when the capital bases differ', () => {
  const a = stub([
    ['2025-01-02', 100000],
    ['2025-01-03', 110000],
  ]);
  const b = stub([
    ['2025-01-02', 200000],
    ['2025-01-03', 210000],
  ]);
  const result = compareEquity(input({ cell: a }), input({ cell: b }), true);

  it('rebases each series to 100 on its OWN first day', () => {
    expect(result.unit).toContain('index');
    expect(result.rows[0].aEquity).toBe(100);
    expect(result.rows[0].bEquity).toBe(100);
    expect(result.rows[1].aEquity).toBeCloseTo(110, 10);
    expect(result.rows[1].bEquity).toBeCloseTo(105, 10);
  });

  it('rebases the base overlay and the benchmark on the same rule', () => {
    const withBase = compareEquity(
      input({ cell: a, base: b, bars: realBars }),
      input({ cell: b }),
      true,
    );
    expect(withBase.rows[0].aBase).toBe(100);
    const bench = withBase.rows.find((r) => r.aBenchmark !== null);
    expect(bench).toBeDefined();
  });

  it('drops a series whose first point is zero rather than dividing by it', () => {
    const zero = stub([
      ['2025-01-02', 0],
      ['2025-01-03', 100],
    ]);
    const result0 = compareEquity(input({ cell: zero }), input({ cell: b }), true);
    expect(result0.hasA).toBe(false);
    expect(result0.rows.every((r) => r.aEquity === null)).toBe(true);
    expect(result0.rows.some((r) => Number.isFinite(r.bEquity ?? NaN))).toBe(true);
  });
});

describe('compareEquity — the benchmark-sharing rule', () => {
  it('draws two lines when the final values differ', () => {
    const result = compareEquity(
      input({ cell: real, bars: realBars }),
      input({ cell: real, bars: barsWith({ final_value: realBars.buy_and_hold!.final_value + 5 }) }),
      false,
    );
    expect(result.sharedBenchmark).toBe(false);
    expect(result.benchmarkNote).toContain('TWO buy-and-hold lines');
    expect(result.rows.every((r) => r.benchmark === null)).toBe(true);
  });

  it('draws two lines when the windows differ, even at the same final value', () => {
    const result = compareEquity(
      input({ cell: real, bars: realBars }),
      input({ cell: real, bars: barsWith({ entry_day: '1999-01-04' }) }),
      false,
    );
    expect(result.sharedBenchmark).toBe(false);
  });

  it('says a single-sided curve is NOT a comparator for both', () => {
    const result = compareEquity(
      input({ cell: real, bars: realBars }),
      input({ cell: real }),
      false,
    );
    expect(result.sharedBenchmark).toBe(false);
    expect(result.benchmarkNote).toContain('one side only');
  });

  it('says the row’s scalar still stands when neither side has a sidecar', () => {
    const result = compareEquity(input({ cell: real }), input({ cell: real }), false);
    expect(result.benchmarkNote).toContain('benchmark_return');
  });
});

describe('nonOverlap — what only one side covers', () => {
  it('is empty for identical windows', () => {
    const w = { start: '2025-01-01', end: '2025-06-01' };
    expect(nonOverlap(w, w)).toEqual([]);
  });

  it('is empty when either window is unknown', () => {
    expect(nonOverlap(null, { start: '2025-01-01', end: '2025-06-01' })).toEqual([]);
  });

  it('names both edges and which side owns them', () => {
    expect(
      nonOverlap({ start: '2025-01-01', end: '2025-06-01' }, { start: '2025-02-01', end: '2025-07-01' }),
    ).toEqual([
      { start: '2025-01-01', end: '2025-02-01', side: 'a' },
      { start: '2025-06-01', end: '2025-07-01', side: 'b' },
    ]);
  });
});
