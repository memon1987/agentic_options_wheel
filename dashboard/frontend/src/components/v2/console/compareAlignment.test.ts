// FC-096 Phase E PR-5: the alignment matrix, on the two REAL runs first.
//
// `13cc2729d1c74211` and `a48d7bb064194e0f` are the same GOOGL spec replayed
// either side of the PR-1 identity move: same symbols, same windows, same
// `scenario_hash`es, same `base_config_hash`, different `engine_identity` and
// `git_commit`. That is the ideal compare fixture — everything aligns except
// the one row that must fire — and it is not a synthetic anyone can drift.
//
// The synthetics below it exist for the mismatches the real pair cannot show,
// and each is a copy of a real run with exactly ONE field moved.

import { describe, expect, it } from 'vitest';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import shapedA48d from '../../../test/fixtures/sweep_shaped_a48d.json';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { indexRows, lookupCell } from '../sims/resultCells';
import type { SweepReport, SweepRow } from '../../../types/v2';
import {
  alignCells,
  capitalBaseOf,
  comparePath,
  differenceOfDeltas,
  formatCellRef,
  overridesDiff,
  parseCellRef,
  sameRef,
  type CompareRef,
  type CompareSide,
} from './compareAlignment';

const detail = (payload: unknown) => {
  const d = normaliseSweepDetail(payload);
  if (!d || !d.results) throw new Error('fixture did not normalise');
  return { sweep: d.sweep, report: d.results };
};

const RUN_13CC = '13cc2729d1c74211';

const side = (
  payload: unknown,
  ref: Omit<CompareRef, 'runId'> & { runId?: string },
  over: { sweep?: Partial<SweepRow>; report?: Partial<SweepReport>; capitalBase?: number | null } = {},
): CompareSide => {
  const { sweep, report } = detail(payload);
  const merged = { ...report, ...(over.report ?? {}) };
  const full: CompareRef = { runId: over.sweep?.run_id ?? ref.runId ?? sweep.run_id, ...ref } as CompareRef;
  return {
    ref: full,
    sweep: { ...sweep, ...(over.sweep ?? {}) },
    report: merged,
    row: lookupCell(indexRows(merged.rows), full.scenario, full.symbol, full.split) ?? null,
    stampedCapitalBase: over.capitalBase === undefined ? 100000 : over.capitalBase,
  };
};

const outcome = (a: ReturnType<typeof alignCells>, id: string) =>
  a.rows.find((r) => r.id === id)?.outcome;

describe('parseCellRef / formatCellRef — the URL round-trip', () => {
  it('round-trips a four-part ref', () => {
    const ref = { runId: RUN_13CC, scenario: 'position_20pct', symbol: 'GOOGL', split: 'holdout' };
    expect(parseCellRef(formatCellRef(ref))).toEqual(ref);
  });

  it.each([
    ['too few parts', '13cc:base:GOOGL'],
    ['too many parts', '13cc:base:GOOGL:fit:extra'],
    ['an empty segment', '13cc:base::fit'],
    ['empty string', ''],
    ['null', null],
  ])('refuses %s rather than guessing which field was dropped', (_label, raw) => {
    expect(parseCellRef(raw as string | null)).toBeNull();
  });

  it('builds a compare path with and without b', () => {
    const a: CompareRef = { runId: 'r1', scenario: 'base', symbol: 'GOOGL', split: 'fit' };
    expect(comparePath(a)).toBe('/sims/compare?a=r1%3Abase%3AGOOGL%3Afit');
    expect(comparePath(a, null)).not.toContain('b=');
    expect(comparePath(a, { ...a, scenario: 'arm' })).toContain('b=r1%3Aarm%3AGOOGL%3Afit');
  });

  it('sameRef is false when either side is null', () => {
    const a: CompareRef = { runId: 'r1', scenario: 'base', symbol: 'GOOGL', split: 'fit' };
    expect(sameRef(a, a)).toBe(true);
    expect(sameRef(a, null)).toBe(false);
  });
});

describe('the real pair: 13cc vs a48d — same spec, one engine move', () => {
  const a = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' });
  const b = side(shapedA48d, { scenario: 'base', symbol: 'GOOGL', split: 'fit' });
  const alignment = alignCells(a, b);

  it('flags the engine identity and nothing else', () => {
    expect(outcome(alignment, 'engine_identity')).toBe('noted');
    const row = alignment.rows.find((r) => r.id === 'engine_identity')!;
    expect(row.a).toBe('129711f15fe0488a');
    expect(row.b).toBe('806a6058d4d363ce');
    expect(row.detail).toContain('DIFFERENT engine builds');
  });

  it('aligns symbol, split, window, arm identity, capital base and base config', () => {
    for (const id of ['symbol', 'split', 'window', 'arm_identity', 'capital_base', 'base_config']) {
      expect([id, outcome(alignment, id)]).toEqual([id, 'aligned']);
    }
  });

  it('withholds nothing and refuses nothing', () => {
    expect(alignment.refusal).toBeNull();
    expect(alignment.withheldReasons).toEqual([]);
    expect(alignment.tilesWithheld).toBe(false);
    expect(alignment.curvesBase100).toBe(false);
  });

  it('still refuses A−B, because the two cells are different RUNS', () => {
    expect(alignment.allowsDelta).toBe(false);
    expect(alignment.deltaRefusal).toContain('different runs');
    expect(differenceOfDeltas(alignment, a, b)).toBeNull();
  });

  it('reports every row, including the ones that pass — checked ≠ unchecked', () => {
    expect(alignment.rows.map((r) => r.id)).toEqual([
      'symbol',
      'split',
      'window',
      'arm_identity',
      'capital_base',
      'base_config',
      'engine_identity',
      'fill_haircut',
      'in_sample',
    ]);
    expect(alignment.rows.some((r) => r.outcome === 'unknown')).toBe(false);
  });
});

describe('same run, arm vs base — the default comparison', () => {
  const a = side(shaped13cc, { scenario: 'position_20pct', symbol: 'GOOGL', split: 'holdout' });
  const b = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'holdout' });
  const alignment = alignCells(a, b);

  it('aligns every dimension', () => {
    expect(alignment.refusal).toBeNull();
    expect(alignment.tilesWithheld).toBe(false);
    expect(outcome(alignment, 'engine_identity')).toBe('aligned');
  });

  it('notes the arm identity, because the two arms hash differently by design', () => {
    expect(outcome(alignment, 'arm_identity')).toBe('noted');
    expect(alignment.overridesDiff).toEqual([
      {
        key: 'risk.max_position_size',
        a: '0.2',
        b: '(base)',
        baseA: '—',
        baseB: '—',
        same: false,
      },
    ]);
  });

  it('refuses A−B anyway: base has no Δ against itself, and null is not 0', () => {
    expect(b.row?.delta_vs_base_annualized ?? null).toBeNull();
    expect(alignment.allowsDelta).toBe(false);
    expect(alignment.deltaRefusal).toContain('no served Δ');
    expect(differenceOfDeltas(alignment, a, b)).toBeNull();
  });
});

describe('same run, arm vs arm — where A−B actually exists', () => {
  // Two measured arms of one run: the fixture has one arm, so the second is the
  // SAME cell relabelled with its own served Δ. The point under test is the
  // rule, not the number: two served Δs, same run, same split, same base.
  const a = side(shaped13cc, { scenario: 'position_20pct', symbol: 'GOOGL', split: 'fit' });
  const armRow = { ...a.row!, delta_vs_base_annualized: -0.04 };
  const withDelta = (delta: number): CompareSide => ({
    ...a,
    row: { ...armRow, delta_vs_base_annualized: delta },
  });

  it('subtracts the two SERVED Δs and nothing else', () => {
    const x = withDelta(-0.04);
    const y = withDelta(0.01);
    const alignment = alignCells(x, y);
    expect(alignment.allowsDelta).toBe(true);
    expect(differenceOfDeltas(alignment, x, y)).toBeCloseTo(-0.05, 12);
  });

  it('never shows A−B when the matrix withheld the tiles', () => {
    const x = withDelta(-0.04);
    const y: CompareSide = {
      ...withDelta(0.01),
      ref: { ...x.ref, split: 'fit' },
      report: { ...x.report!, starting_cash: 250000 },
      stampedCapitalBase: 250000,
    };
    const alignment = alignCells(x, y);
    expect(alignment.tilesWithheld).toBe(true);
    expect(alignment.allowsDelta).toBe(false);
    expect(differenceOfDeltas(alignment, x, y)).toBeNull();
  });
});

describe('the withheld rows, one synthetic field each', () => {
  const base = () => side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' });

  it('symbol mismatch is REFUSED, not withheld', () => {
    const a = base();
    const b = side(shaped13cc, { scenario: 'base', symbol: 'MSFT', split: 'fit' });
    const alignment = alignCells(a, b);
    expect(outcome(alignment, 'symbol')).toBe('refused');
    expect(alignment.refusal).toContain('two SYMBOLS, not two configs');
    expect(alignment.allowsDelta).toBe(false);
  });

  it('split mismatch withholds the tiles and keeps the curves', () => {
    const a = base();
    const b = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'holdout' });
    const alignment = alignCells(a, b);
    expect(outcome(alignment, 'split')).toBe('withheld');
    expect(alignment.tilesWithheld).toBe(true);
    expect(alignment.withheldReasons.join(' ')).toContain('In-sample vs holdout');
    expect(alignment.refusal).toBeNull();
    expect(alignment.curvesBase100).toBe(false);
  });

  it('window mismatch withholds, and names the UNION window', () => {
    const a = base();
    const b = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' }, {
      report: {
        windows: [
          { split: 'fit', start: '2025-10-01', end: '2026-07-02' },
          { split: 'holdout', start: '2026-06-03', end: '2026-09-01' },
        ],
      },
    });
    const alignment = alignCells(a, b);
    expect(outcome(alignment, 'window')).toBe('withheld');
    expect(alignment.windowUnion).toEqual({ start: '2025-09-01', end: '2026-07-02' });
    expect(alignment.tilesWithheld).toBe(true);
  });

  it('capital-base mismatch withholds the tiles AND rebases the curves', () => {
    const a = base();
    const b = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' }, {
      capitalBase: 250000,
    });
    const alignment = alignCells(a, b);
    expect(outcome(alignment, 'capital_base')).toBe('withheld');
    expect(alignment.curvesBase100).toBe(true);
    expect(alignment.withheldReasons.join(' ')).toContain('DIFFERENT STRATEGY');
  });

  it('a base-config mismatch is NOTED and kills A−B, but withholds nothing', () => {
    const a = base();
    const b = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' }, {
      sweep: { base_config_hash: 'ffffffffffffffff' },
    });
    const alignment = alignCells(a, b);
    expect(outcome(alignment, 'base_config')).toBe('noted');
    expect(alignment.tilesWithheld).toBe(false);
    expect(alignment.allowsDelta).toBe(false);
    expect(alignment.deltaRefusal).toContain('different base configs');
  });

  it('a differing fill haircut is noted on the varying side', () => {
    const a = base();
    const b = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' }, {
      report: { scenario_fill_haircuts: { base: 0.5 } },
    });
    const alignment = alignCells(a, b);
    expect(outcome(alignment, 'fill_haircut')).toBe('noted');
    const row = alignment.rows.find((r) => r.id === 'fill_haircut')!;
    expect(row.a).toBe('engine default');
    expect(row.b).toBe('0.5');
  });

  it('an in-sample side is noted, and it is enough that ONE side is', () => {
    const a = base();
    const b = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' }, {
      report: { in_sample_only: true },
    });
    expect(alignCells(a, b).inSample).toBe(true);
    expect(outcome(alignCells(a, b), 'in_sample')).toBe('noted');
  });
});

describe('same NAME is not same OVERRIDES', () => {
  it('notes two arms that share a name and hash differently, and diffs them', () => {
    const a = side(shaped13cc, { scenario: 'position_20pct', symbol: 'GOOGL', split: 'fit' });
    const b = side(shapedA48d, { scenario: 'position_20pct', symbol: 'GOOGL', split: 'fit' }, {
      report: {
        scenario_hashes: { base: '34f837365f81cd76', position_20pct: 'deadbeefdeadbeef' },
        scenario_overrides: { position_20pct: { 'risk.max_position_size': 0.35 } },
      },
    });
    const alignment = alignCells(a, b);
    expect(outcome(alignment, 'arm_identity')).toBe('noted');
    expect(alignment.rows.find((r) => r.id === 'arm_identity')!.detail).toContain(
      'SAME NAME, DIFFERENT ARM',
    );
    expect(alignment.overridesDiff).toEqual([
      { key: 'risk.max_position_size', a: '0.2', b: '0.35', baseA: '—', baseB: '—', same: false },
    ]);
  });

  it('aligns two arms whose hashes match, whatever they are called', () => {
    const a = side(shaped13cc, { scenario: 'position_20pct', symbol: 'GOOGL', split: 'fit' });
    const b = side(shapedA48d, { scenario: 'position_20pct', symbol: 'GOOGL', split: 'fit' });
    expect(outcome(alignCells(a, b), 'arm_identity')).toBe('aligned');
  });
});

describe('an unloaded side is UNCHECKED, never aligned', () => {
  const loaded = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' });
  const empty: CompareSide = {
    ref: { runId: 'pending', scenario: 'base', symbol: 'GOOGL', split: 'fit' },
    sweep: null,
    report: null,
    row: null,
    stampedCapitalBase: null,
  };
  const alignment = alignCells(loaded, empty);

  it.each(['window', 'arm_identity', 'capital_base', 'base_config', 'engine_identity'])(
    'leaves %s unknown rather than claiming it passed',
    (id) => {
      expect(outcome(alignment, id)).toBe('unknown');
    },
  );

  it('does not withhold on an unknown row — it has nothing to say yet', () => {
    expect(alignment.tilesWithheld).toBe(false);
    expect(alignment.allowsDelta).toBe(false);
  });
});

describe('capitalBaseOf — stamped beats declared, and says which', () => {
  it('prefers the artifact stamp', () => {
    const s = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' }, {
      capitalBase: 123,
    });
    expect(capitalBaseOf(s)).toEqual({ value: 123, source: 'stamped on the cell artifact' });
  });

  it('falls back to the spec’s declared starting cash, labelled as declared', () => {
    const s = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' }, {
      capitalBase: null,
    });
    expect(capitalBaseOf(s).value).toBe(100000);
    expect(capitalBaseOf(s).source).toContain('declared');
  });
});

describe('overridesDiff — the union of both arms’ keys, with each run’s base', () => {
  it('shows a key one arm sets and the other leaves at base', () => {
    const a = side(shaped13cc, { scenario: 'position_20pct', symbol: 'GOOGL', split: 'fit' });
    const b = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' });
    const diff = overridesDiff(a, b, { 'risk.max_position_size': 0.1 }, { 'risk.max_position_size': 0.1 });
    expect(diff).toEqual([
      { key: 'risk.max_position_size', a: '0.2', b: '(base)', baseA: '0.1', baseB: '0.1', same: false },
    ]);
  });

  it('reads the FLAT dotted key, never a nested walk', () => {
    const a = side(shaped13cc, { scenario: 'position_20pct', symbol: 'GOOGL', split: 'fit' });
    const b = side(shaped13cc, { scenario: 'base', symbol: 'GOOGL', split: 'fit' });
    const nested = { risk: { max_position_size: 0.1 } };
    expect(overridesDiff(a, b, nested, nested)[0].baseA).toBe('—');
  });
});
