// FC-096 Phase E PR-5: the compare view's DOM contract.
//
// Narrow, like `renderers.test.tsx`: `ResponsiveContainer` is 0x0 under jsdom,
// so nothing here asserts a mark on a chart. What it asserts is what the plan
// asks the compare view for — that the matrix's outcome reaches the screen for
// every row, that a withheld pair prints no return tile, that a refused pair
// prints nothing below the refusal, that the digest tiles are absent, and that
// the bias footer is last.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import bars13cc from '../../../test/fixtures/bars_13cc_googl_fit.json';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import shapedA48d from '../../../test/fixtures/sweep_shaped_a48d.json';
import type { SimArtifact, SimBars, SweepReport } from '../../../types/v2';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { indexRows, lookupCell } from '../sims/resultCells';
import { normaliseArtifact, normaliseBars } from './normaliseArtifact';
import { computeDigest } from './artifactDigest';
import CompareView, { type CompareSideData } from './CompareView';
import type { CompareRef } from './compareAlignment';

const artifact = normaliseArtifact(artifact13cc) as SimArtifact;
const bars = normaliseBars(bars13cc) as SimBars;
const digest = computeDigest(artifact, bars, { specStrategy: null });

const detailOf = (payload: unknown) => {
  const d = normaliseSweepDetail(payload)!;
  return { sweep: d.sweep, report: d.results! };
};

const sideFor = (
  payload: unknown,
  ref: Omit<CompareRef, 'runId'>,
  over: Omit<Partial<CompareSideData>, 'report'> & { report?: Partial<SweepReport> | null } = {},
): CompareSideData => {
  const { sweep, report } = detailOf(payload);
  // The report override is MERGED into the real report, so a test that sets one
  // field does not silently drop `windows` and crash the matrix. Pulled out of
  // the trailing spread for exactly that reason.
  const { report: reportOver, ...rest } = over;
  const merged = reportOver === null ? null : ({ ...report, ...(reportOver ?? {}) } as SweepReport);
  const full: CompareRef = { runId: sweep.run_id, ...ref };
  return {
    ref: full,
    sweep,
    report: merged,
    row: merged
      ? (lookupCell(indexRows(merged.rows), full.scenario, full.symbol, full.split) ?? null)
      : null,
    artifact,
    artifactAbsence: null,
    baseArtifact: artifact,
    baseAbsence: null,
    bars,
    barsAbsence: null,
    barsLoading: false,
    digest,
    digestAbsence: null,
    artifactRunId: sweep.run_id,
    baseEffective: null,
    strategy: 'wheel',
    loading: false,
    error: null,
    status: 'done',
    dedupFrom: null,
    ...(rest as Partial<CompareSideData>),
  };
};

const show = (a: CompareSideData, b: CompareSideData | null) =>
  render(
    <MemoryRouter>
      <CompareView a={a} b={b} />
    </MemoryRouter>,
  );

beforeEach(() => {
  // `PriceChart`'s BigQuery fallback fires only once the sidecar has SETTLED
  // absent; both sides here carry one, so nothing should reach the network. The
  // stub is here so a regression on that rule shows up as a failed assertion
  // rather than as an unhandled fetch.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new Error('no fetch expected in the compare view');
    }),
  );
});

const A = { scenario: 'position_20pct', symbol: 'GOOGL', split: 'fit' };
const BASE = { scenario: 'base', symbol: 'GOOGL', split: 'fit' };

describe('no second cell chosen', () => {
  it('says nothing is chosen and does NOT choose one', () => {
    show(sideFor(shaped13cc, A), null);
    expect(screen.getByTestId('compare-awaiting-b').textContent).toContain('Nothing is chosen');
    expect(screen.queryByTestId('alignment-matrix')).toBeNull();
    expect(screen.queryByTestId('strip-A')).toBeNull();
  });
});

describe('the aligned pair — arm vs its own base, one run', () => {
  beforeEach(() => {
    show(sideFor(shaped13cc, A), sideFor(shaped13cc, BASE));
  });

  it('renders every matrix row with an explicit outcome', () => {
    for (const id of [
      'symbol',
      'split',
      'window',
      'arm_identity',
      'capital_base',
      'base_config',
      'engine_identity',
      'fill_haircut',
      'in_sample',
    ]) {
      expect(screen.getByTestId(`alignment-${id}`)).toBeTruthy();
    }
    expect(screen.getByTestId('alignment-symbol').getAttribute('data-outcome')).toBe('aligned');
  });

  it('renders both strips and NO digest tiles on either', () => {
    expect(screen.getByTestId('strip-A')).toBeTruthy();
    expect(screen.getByTestId('strip-B')).toBeTruthy();
    expect(screen.queryAllByTestId('digest-tiles')).toHaveLength(0);
    expect(screen.queryAllByTestId('tile-gross-premium')).toHaveLength(0);
    expect(screen.queryAllByTestId('tile-deployment')).toHaveLength(0);
    expect(screen.queryAllByTestId('digest-omitted')).toHaveLength(2);
  });

  it('withholds nothing — and still renders no return tile for the UNMEASURED side', () => {
    // `position_20pct` is `insufficient` in both windows of this real run, so A
    // carries no return by the cell-state partition while B does. That is the
    // guardrail, not a withholding: the two reasons a tile is missing are kept
    // apart on screen and here.
    expect(screen.queryByTestId('tiles-withheld')).toBeNull();
    expect(screen.queryAllByTestId('tile-annualized')).toHaveLength(1);
    expect(screen.queryAllByTestId('unmeasured-notice')).toHaveLength(1);
    expect(screen.getByTestId('unmeasured-notice').textContent).toContain('insuf');
  });

  it('refuses A−B against base, and says base has no Δ against itself', () => {
    expect(screen.queryByTestId('ab-delta')).toBeNull();
    expect(screen.getByTestId('ab-refused').textContent).toContain('no served Δ');
  });

  it('renders the overrides diff with the arm’s key and the base column', () => {
    expect(screen.getByTestId('overrides-row-risk.max_position_size')).toBeTruthy();
    expect(
      screen.getByTestId('overrides-row-risk.max_position_size').getAttribute('data-same'),
    ).toBe('false');
  });

  it('renders both provenance footers, A before B', () => {
    const a = screen.getByTestId('provenance-A');
    const b = screen.getByTestId('provenance-B');
    expect(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('says the paired charts are paired, not overlaid', () => {
    expect(screen.getByTestId('paired-charts-note').textContent).toContain('PAIRED');
  });
});

describe('a withheld pair — fit vs holdout of one arm', () => {
  beforeEach(() => {
    show(sideFor(shaped13cc, BASE), sideFor(shaped13cc, { ...BASE, split: 'holdout' }));
  });

  it('names the split as the withholding row', () => {
    expect(screen.getByTestId('alignment-split').getAttribute('data-outcome')).toBe('withheld');
    expect(screen.getAllByTestId('tiles-withheld')).toHaveLength(2);
    expect(screen.getAllByTestId('tiles-withheld')[0].textContent).toContain(
      'In-sample vs holdout',
    );
  });

  it('prints NO return, RoC, Δ, benchmark, P&L, win-rate or drawdown tile on either side', () => {
    for (const id of [
      'tile-annualized',
      'tile-roc',
      'tile-delta',
      'tile-benchmark',
      'tile-option-pnl',
      'tile-win-rate',
      'tile-drawdown',
      'tile-time-in-position',
    ]) {
      expect([id, screen.queryAllByTestId(id).length]).toEqual([id, 0]);
    }
  });

  it('keeps the activity COUNTS, which the window does not rescale', () => {
    expect(screen.queryAllByTestId('tile-cycles')).toHaveLength(2);
    expect(screen.queryAllByTestId('tile-decision-days')).toHaveLength(2);
  });

  it('draws the curves anyway — withheld is not refused', () => {
    expect(screen.queryByTestId('compare-refusal')).toBeNull();
    expect(screen.getByTestId('compare-equity-unit')).toBeTruthy();
  });

  it('refuses A−B and says the splits are the reason', () => {
    expect(screen.queryByTestId('ab-delta')).toBeNull();
    expect(screen.getByTestId('ab-refused').textContent).toContain('different splits');
  });
});

describe('a capital-base mismatch', () => {
  it('withholds the tiles and rebases the curves to 100', () => {
    const b = sideFor(shaped13cc, BASE, {
      artifact: {
        ...artifact,
        provenance: { ...artifact.provenance, capital_base: 250000 },
      } as SimArtifact,
    });
    show(sideFor(shaped13cc, BASE), b);
    expect(screen.getByTestId('alignment-capital_base').getAttribute('data-outcome')).toBe(
      'withheld',
    );
    expect(screen.getByTestId('compare-equity-unit').textContent).toContain('rebased to 100');
    expect(screen.queryAllByTestId('tile-annualized')).toHaveLength(0);
  });
});

describe('a symbol mismatch is refused', () => {
  beforeEach(() => {
    show(sideFor(shaped13cc, BASE), sideFor(shaped13cc, { ...BASE, symbol: 'MSFT' }));
  });

  it('prints the refusal and nothing that could be compared', () => {
    expect(screen.getByTestId('compare-refusal').textContent).toContain('two SYMBOLS');
    expect(screen.queryByTestId('strip-A')).toBeNull();
    expect(screen.queryByTestId('strip-B')).toBeNull();
    expect(screen.queryByTestId('overrides-diff')).toBeNull();
    expect(screen.queryByTestId('ab-delta')).toBeNull();
    expect(screen.queryByTestId('compare-equity-unit')).toBeNull();
  });

  it('still renders the matrix and both provenance footers', () => {
    expect(screen.getByTestId('alignment-matrix')).toBeTruthy();
    expect(screen.getByTestId('provenance-A')).toBeTruthy();
    expect(screen.getByTestId('provenance-B')).toBeTruthy();
  });
});

describe('cross-run: 13cc vs a48d, the same spec across an engine move', () => {
  beforeEach(() => {
    show(sideFor(shaped13cc, BASE), sideFor(shapedA48d, BASE));
  });

  it('notes the engine identity and names both builds', () => {
    const row = screen.getByTestId('alignment-engine_identity');
    expect(row.getAttribute('data-outcome')).toBe('noted');
    expect(row.parentElement?.textContent).toContain('129711f15fe0488a');
    expect(row.parentElement?.textContent).toContain('806a6058d4d363ce');
  });

  it('shows the tiles — an engine note withholds nothing — but never an A−B', () => {
    expect(screen.queryByTestId('tiles-withheld')).toBeNull();
    expect(screen.queryAllByTestId('tile-annualized')).toHaveLength(2);
    expect(screen.getByTestId('ab-refused').textContent).toContain('different runs');
  });

  it('gives the overrides diff a base column per run', () => {
    expect(screen.getByTestId('overrides-diff-empty')).toBeTruthy();
  });
});

describe('in-sample', () => {
  it('prints the run’s own banner verbatim, per side, naming which side', () => {
    const b = sideFor(shaped13cc, BASE, {
      report: { in_sample_only: true, in_sample_banner: 'THE SERVER’S OWN WORDS.' },
    });
    show(sideFor(shaped13cc, BASE), b);
    expect(screen.queryByTestId('compare-in-sample-a')).toBeNull();
    const banner = screen.getByTestId('compare-in-sample-b');
    expect(banner.textContent).toContain('THE SERVER’S OWN WORDS.');
    expect(banner.textContent).toContain('B (');
  });

  it('falls back to the grid’s wording when the flag is set with no banner', () => {
    const b = sideFor(shaped13cc, BASE, {
      report: { in_sample_only: true, in_sample_banner: null },
    });
    show(sideFor(shaped13cc, BASE), b);
    expect(screen.getByTestId('compare-in-sample-b').textContent?.length).toBeGreaterThan(40);
  });
});

describe('absence states', () => {
  it('prints the endpoint’s own words when neither cell stored a curve', () => {
    const bare = { artifact: null, baseArtifact: null, bars: null, digest: null } as const;
    show(
      sideFor(shaped13cc, BASE, { ...bare, artifactAbsence: 'No detail artifact for this cell.' }),
      sideFor(shaped13cc, BASE, { ...bare, artifactAbsence: 'No detail artifact for this cell.' }),
    );
    expect(screen.getByTestId('compare-equity-absent').textContent).toContain(
      'No detail artifact for this cell.',
    );
  });

  it('says a side could not be read rather than rendering an empty strip', () => {
    show(
      sideFor(shaped13cc, BASE),
      sideFor(shaped13cc, BASE, { report: null, sweep: null, error: 'read failed' }),
    );
    expect(screen.getByTestId('strip-B-absent').textContent).toContain('read failed');
    expect(screen.getByTestId('provenance-B-absent').textContent).toContain('read failed');
    expect(screen.getByTestId('strip-A')).toBeTruthy();
  });
});

// --------------------------------------------------------------------------- //
// Review round 1
// --------------------------------------------------------------------------- //

describe('R3 — withholding reaches the paired renderers', () => {
  beforeEach(() => {
    show(sideFor(shaped13cc, BASE), sideFor(shaped13cc, { ...BASE, split: 'holdout' }));
  });

  it('prints no drawdown header number on either side', () => {
    expect(screen.queryAllByTestId('drawdown-header')).toHaveLength(0);
    expect(screen.queryAllByTestId('drawdown-withheld')).toHaveLength(2);
  });

  it('prints no deployment reading and no dollar means', () => {
    expect(screen.queryAllByTestId('deployment-mean')).toHaveLength(0);
    expect(screen.queryAllByTestId('deployment-withheld')).toHaveLength(2);
  });

  it('prints no cumulative premium total and no monthly bars', () => {
    expect(screen.queryAllByTestId('premium-total')).toHaveLength(0);
    expect(screen.queryAllByTestId('premium-withheld')).toHaveLength(2);
    expect(screen.queryAllByTestId('monthly-premium-bars')).toHaveLength(0);
  });

  it('still draws the curves — the panels are present, only the numbers are gone', () => {
    expect(screen.queryAllByTestId('drawdown-chart')).toHaveLength(2);
    expect(screen.queryAllByTestId('deployment-chart')).toHaveLength(2);
  });
});

describe('R3 — an ALIGNED pair keeps every one of those numbers', () => {
  it('so the withheld assertions above mean something', () => {
    show(sideFor(shaped13cc, BASE), sideFor(shaped13cc, A));
    expect(screen.queryAllByTestId('drawdown-header').length).toBeGreaterThan(0);
    expect(screen.queryAllByTestId('deployment-mean').length).toBeGreaterThan(0);
    expect(screen.queryAllByTestId('premium-total').length).toBeGreaterThan(0);
    expect(screen.queryAllByTestId('drawdown-withheld')).toHaveLength(0);
  });
});

describe('R2 — a base-config mismatch is a different STRATEGY', () => {
  // TWO runs, so both base columns render and the mismatch is the real shape:
  // one recorded base per run, disagreeing on a key neither arm overrides.
  const withBase = (payload: unknown, hash: string, putDelta: number) =>
    sideFor(payload, BASE, {
      sweep: { ...detailOf(payload).sweep, base_config_hash: hash },
      baseEffective: { 'strategy.put_delta_range': [putDelta, putDelta + 0.1] },
    });

  beforeEach(() => {
    show(
      withBase(shaped13cc, '4c6e74c428681de7', 0.15),
      withBase(shapedA48d, 'ffffffffffffffff', 0.3),
    );
  });

  it('does not call the base row aligned, and withholds the tiles', () => {
    expect(screen.getByTestId('alignment-base_config').getAttribute('data-outcome')).toBe(
      'withheld',
    );
    expect(screen.getAllByTestId('tiles-withheld').length).toBe(2);
    expect(screen.queryAllByTestId('tile-annualized')).toHaveLength(0);
  });

  it('lists the base-only key both arms leave alone, marked as base-only', () => {
    const row = screen.getByTestId('overrides-row-strategy.put_delta_range');
    expect(row.getAttribute('data-origin')).toBe('base');
    expect(row.getAttribute('data-same')).toBe('false');
    expect(row.textContent).toContain('0.15');
    expect(row.textContent).toContain('0.3');
    expect(screen.getByTestId('overrides-base-keys').textContent).toContain('NEITHER arm');
  });
});

describe('R2a — same overrides, different effective config', () => {
  it('is NOT aligned, whatever `scenario_hash` says', () => {
    const a = sideFor(shaped13cc, BASE);
    const b = sideFor(shapedA48d, BASE, {
      report: {
        // Same overrides hash, DIFFERENT effective config hash: the pre-fix row
        // read only the first and called this pair aligned.
        scenario_hashes: { base: '34f837365f81cd76', position_20pct: '552c55c7166a3e7b' },
        scenario_config_hashes: { base: 'deadbeefdeadbeef', position_20pct: '9881345d1e116d17' },
      },
    });
    show(a, b);
    expect(screen.getByTestId('alignment-arm_identity').getAttribute('data-outcome')).toBe('noted');
    expect(screen.getByTestId('alignment-arm_identity').parentElement?.textContent).toContain(
      'DIFFERENT effective config',
    );
  });
});

describe('R1 — a non-`done` side says what the run IS', () => {
  it('names a failed run and its error, and leaves the matrix unchecked', () => {
    const failed = sideFor(shaped13cc, BASE, {
      report: null,
      sweep: { ...detailOf(shaped13cc).sweep, status: 'failed', error: 'chain fetch blew up' },
      status: 'failed',
    });
    show(sideFor(shaped13cc, BASE), failed);
    const absent = screen.getByTestId('strip-B-absent');
    expect(absent.getAttribute('data-run-status')).toBe('failed');
    expect(absent.textContent).toContain('FAILED');
    expect(absent.textContent).toContain('chain fetch blew up');
    expect(absent.textContent).not.toContain('Loading this run');
    // Every row that needs B's report is UNCHECKED, never aligned.
    for (const id of ['window', 'arm_identity', 'base_config']) {
      expect([id, screen.getByTestId(`alignment-${id}`).getAttribute('data-outcome')]).toEqual([
        id,
        'unknown',
      ]);
    }
  });

  it('says a running run is still running, not that it is loading', () => {
    const running = sideFor(shaped13cc, BASE, {
      report: null,
      sweep: { ...detailOf(shaped13cc).sweep, status: 'running' },
      status: 'running',
    });
    show(sideFor(shaped13cc, BASE), running);
    expect(screen.getByTestId('strip-B-absent').textContent).toContain('running');
    expect(screen.getByTestId('strip-B-absent').textContent).toContain('keeps polling');
  });

  it('shows the "answered by run X" notice on a followed side', () => {
    show(sideFor(shaped13cc, BASE), sideFor(shaped13cc, A, { dedupFrom: 'cafebabecafebabe' }));
    expect(screen.getByTestId('provenance-B-dedup').textContent).toContain('cafebabecafebabe');
    expect(screen.getByTestId('provenance-B-dedup').textContent).toContain('deduplicated');
  });
});

describe('R8 — a cell against itself is never "A−B 0.0%"', () => {
  it('refuses, and says the difference is a tautology', () => {
    show(sideFor(shaped13cc, BASE), sideFor(shaped13cc, BASE));
    expect(screen.queryByTestId('ab-delta')).toBeNull();
    expect(screen.getByTestId('ab-refused').textContent).toContain('SAME cell');
  });
});
