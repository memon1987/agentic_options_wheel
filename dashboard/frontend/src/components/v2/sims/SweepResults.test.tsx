// FC-060 Layer 4 (PR-B): the results view.
//
// The rules under test are the ones that decide whether this page can mislead:
// the five cell kinds must be visually distinct and only `measured` may render
// as a return; the counts must add up; every aggregate must come from the
// server; the in-sample banner must appear IFF `in_sample_only`; and the
// runner's prose must reach the screen byte for byte.
//
// Fixture provenance is documented in `normaliseReport.test.ts`.

import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
// PR-5 made every grid cell a compare <Link>, so the grid needs a router.
import { MemoryRouter } from 'react-router-dom';
import SweepResults from './SweepResults';
import BiasFooter from './BiasFooter';
import { normaliseReport } from './normaliseReport';
import type { SweepReport, SweepRow } from '../../../types/v2';
import shapedHoldout from '../../../test/fixtures/sweep_shaped_holdout.json';
import shapedInsample from '../../../test/fixtures/sweep_shaped_insample.json';
import shapedUnknown from '../../../test/fixtures/sweep_shaped_unknown.json';

const sweep = (over: Partial<SweepRow> = {}): SweepRow => ({
  run_id: '0f1e2d3c4b5a6978',
  sweep_key: 'deadbeefdeadbeef',
  status: 'done',
  deduplicated_to: null,
  submitted_at: '2026-08-29T13:00:00+00:00',
  started_at: '2026-08-29T13:04:00+00:00',
  finished_at: '2026-08-29T13:12:00+00:00',
  submitted_via: 'dashboard',
  execution_name: 'backtest-sweep-abcde',
  git_commit: '344b1ac',
  engine_version: 'fc-069-scanner-rewire',
  base_config_hash: 'a1b2c3d4e5f60718',
  base_config_json: null,
  spec_json: null,
  symbols: ['AAPL', 'NVDA'],
  window_start: '2026-03-01',
  window_end: '2026-05-29',
  holdout_start: '2026-05-01',
  in_sample_only: false,
  scenario_count: 3,
  cell_count: 16,
  wall_seconds: 98.4,
  materialise_seconds: 60.1,
  replay_seconds: 11.5,
  provider_fetches: 4,
  bar_cache_hits: 812,
  lake_summary_json: null,
  error: null,
  ...over,
});

const holdout = () => normaliseReport(shapedHoldout, sweep())!;
const insample = () => normaliseReport(shapedInsample, sweep({ in_sample_only: true }))!;
const unknownReport = () => normaliseReport(shapedUnknown, sweep())!;

// The bias footer moved OUT of `SweepResults` in FC-096 Phase E PR-2 (review
// round 1, F2) and is rendered by the page, after the console. These tests mount
// the same pair the page does, so the byte-for-byte prose assertions below keep
// testing what they always tested — and `SimsRouting.test.tsx` is what asserts
// the footer is genuinely last on the real page.
const show = (report: SweepReport, row: SweepRow = sweep(), raw?: unknown) =>
  render(
    <MemoryRouter>
      <SweepResults sweep={row} report={report} raw={raw} />
      <BiasFooter report={report} />
    </MemoryRouter>,
  );

const cell = (scenario: string, symbol: string, split: string) =>
  screen.getByTestId(`cell-${scenario}-${symbol}-${split}`);

describe('SweepResults — the five cell renderings', () => {
  it('renders a measured cell as a signed return plus the verdict glyph', () => {
    show(holdout());
    const c = cell('base', 'AAPL', 'fit');
    expect(c).toHaveAttribute('data-cell-kind', 'return');
    expect(c.textContent).toBe('+18.4% +'); // 'fit' -> '+'
    expect(c.className).toMatch(/text-green-400/);
  });

  it('renders a negative measured cell in red, still as a return', () => {
    show(holdout());
    const c = cell('call_floor_0_50', 'NVDA', 'fit');
    expect(c).toHaveAttribute('data-cell-kind', 'return');
    expect(c.textContent).toBe('-5.2% -'); // 'unfit' -> '-'
    expect(c.className).toMatch(/text-red-400/);
  });

  it('renders an insufficient cell as `insuf` — never as a number', () => {
    show(holdout());
    const c = cell('puts_15_25', 'NVDA', 'holdout');
    expect(c).toHaveAttribute('data-cell-kind', 'insuf');
    expect(c.textContent).toBe('insuf');
    expect(c.textContent).not.toMatch(/%/);
    expect(c.className).not.toMatch(/text-green-400|text-red-400/);
  });

  it('renders a low-activity cell as `low-act N%` with its fraction, not its return', () => {
    show(holdout());
    const c = cell('puts_15_25', 'NVDA', 'fit');
    expect(c).toHaveAttribute('data-cell-kind', 'low-act');
    // The underlying row carries annualized_return = 0.402. It must NOT show.
    expect(c.textContent).toBe('low-act 9%');
    expect(c.textContent).not.toMatch(/40/);
    expect(c.className).not.toMatch(/text-green-400/);
  });

  it('renders an errored cell as `err` with the error on hover', () => {
    show(holdout());
    const c = cell('call_floor_0_50', 'AAPL', 'fit');
    expect(c).toHaveAttribute('data-cell-kind', 'err');
    expect(c.textContent).toBe('err');
    // PR-5 wrapped every cell in a compare <Link> and moved the hover text onto
    // it — ONE title, because two nested ones race in the tooltip. The engine's
    // error is still the first thing it says.
    expect(c.closest('a')?.getAttribute('title')).toMatch(/UnadjustedCorporateAction/);
    expect(c.getAttribute('title')).toBeNull();
  });

  it('renders an UNCLASSIFIED cell as `unknown` — not as a green return', () => {
    // The regression: `renderCell` used to fall through to the return branch for
    // a row with no state flag, so a verdict that never resolved was shown as a
    // measured +7.1%.
    const report = unknownReport();
    expect(report.rows.find(
      (r) => r.scenario === 'at_the_bid' && r.symbol === 'AAPL' && r.split === 'fit',
    )!.annualized_return).toBe(0.071);
    show(report);
    const c = cell('at_the_bid', 'AAPL', 'fit');
    expect(c).toHaveAttribute('data-cell-kind', 'unknown');
    expect(c.textContent).toBe('unknown');
    expect(c.textContent).not.toMatch(/%|7\.1/);
    expect(c.className).not.toMatch(/text-green-400|text-red-400/);
  });

  it('gives the five kinds five distinct styles', () => {
    show(unknownReport());
    const styles = [
      cell('base', 'AAPL', 'fit'),
      cell('puts_15_25', 'NVDA', 'holdout'),
      cell('puts_15_25', 'NVDA', 'fit'),
      cell('call_floor_0_50', 'AAPL', 'fit'),
      cell('at_the_bid', 'AAPL', 'fit'),
    ].map((el) => el.className);
    expect(new Set(styles).size).toBe(5);
  });
});

describe('SweepResults — aggregates come from the server, and never without the grid', () => {
  it('renders the per-symbol grid for every split', () => {
    const report = holdout();
    show(report);
    for (const split of ['fit', 'holdout']) {
      for (const scenario of report.scenarios) {
        for (const symbol of report.symbols) {
          expect(cell(scenario, symbol, split)).toBeInTheDocument();
        }
      }
    }
  });

  it('renders the median from the payload, not from a browser computation', () => {
    const report = holdout();
    // Poison the rows: if anything recomputed, the rendered median would move.
    const poisoned: SweepReport = {
      ...report,
      rows: report.rows.map((r) => (r.measured ? { ...r, annualized_return: 9.99 } : r)),
    };
    show(poisoned);
    const row = screen.getAllByText('puts_15_25')[0].closest('tr')!;
    const cells = within(row).getAllByRole('cell');
    // The server's median for puts_15_25/fit is 0.241 and must be what shows.
    expect(cells[cells.length - 3].textContent).toBe('+24.1%');
    expect(cells[cells.length - 3].textContent).not.toMatch(/999/);
  });

  it('shows min/max from the payload alongside the median', () => {
    show(holdout());
    const row = screen.getAllByText('base')[0].closest('tr')!;
    const cells = within(row).getAllByRole('cell');
    // base/fit: measured 0.184 and 0.061 -> median 0.1225, min 0.061, max 0.184.
    expect(cells[cells.length - 3].textContent).toBe('+12.3%');
    expect(cells[cells.length - 2].textContent).toBe('+6.1% / +18.4%');
  });

  it('shows five counts that add up to the cells in the row', () => {
    show(unknownReport());
    const counts = screen.getByTestId('counts-at_the_bid-fit').textContent!.replace(/\s/g, '');
    // 1 measured, 0 insuf, 0 low-act, 0 err, 1 unknown = the 2 symbols shown.
    expect(counts).toBe('1·0·0·0·1');
    const parts = counts.split('·').map(Number);
    expect(parts.reduce((a, b) => a + b, 0)).toBe(2);
  });

  it('shows the Δ vs base with its n', () => {
    show(holdout());
    const row = screen.getAllByText('puts_15_25')[0].closest('tr')!;
    const cells = within(row).getAllByRole('cell');
    expect(cells[cells.length - 1].textContent).toMatch(/\(n=1\)/);
  });

  it('says "no measured cell" instead of printing a median of nothing', () => {
    const report = holdout();
    const empty: SweepReport = {
      ...report,
      summary: report.summary.map((s) =>
        s.scenario === 'call_floor_0_50' ? { ...s, measured: 0, median: null } : s,
      ),
    };
    show(empty);
    expect(screen.getAllByText('no measured cell').length).toBeGreaterThan(0);
  });

  it('renders fit vs holdout with sign agreement only when a holdout ran', () => {
    const { unmount } = show(holdout());
    expect(screen.getByText('Fit vs holdout')).toBeInTheDocument();
    expect(screen.getByText('sign agreement')).toBeInTheDocument();
    unmount();
    show(insample(), sweep({ in_sample_only: true }));
    expect(screen.queryByText('Fit vs holdout')).toBeNull();
  });

  it('renders sign agreement as plain uncoloured text', () => {
    show(holdout());
    const agreement = screen.getByTestId('agreement-puts_15_25');
    expect(agreement.textContent).toBe('1/1');
    // 1/1 in green read as "validated". It is one symbol agreeing with itself.
    expect(agreement.className).not.toMatch(/text-green|text-yellow|text-red/);
  });
});

describe('SweepResults — the in-sample banner appears IFF in_sample_only', () => {
  it('is absent on a run with a holdout', () => {
    const report = holdout();
    expect(report.in_sample_only).toBe(false);
    show(report);
    expect(screen.queryByTestId('in-sample-banner')).toBeNull();
  });

  it('is present, verbatim, on a run without one', () => {
    const report = insample();
    expect(report.in_sample_only).toBe(true);
    show(report, sweep({ in_sample_only: true }));
    const banner = screen.getByTestId('in-sample-banner');
    expect(banner.textContent).toBe(report.in_sample_banner);
  });

  it('still warns when the flag is set but the banner string is missing', () => {
    // The banner keys on the FLAG. A null string must not be able to suppress it.
    const report: SweepReport = { ...insample(), in_sample_banner: null };
    show(report, sweep({ in_sample_only: true }));
    const banner = screen.getByTestId('in-sample-banner');
    expect(banner.textContent).toMatch(/IN-SAMPLE ONLY/);
    expect(banner.textContent).toMatch(/luckiest one than the best one/);
  });

  it('does not add a title of its own — the payload string already carries one', () => {
    const report = insample();
    show(report, sweep({ in_sample_only: true }));
    const banner = screen.getByTestId('in-sample-banner');
    // Exactly the payload's string, with nothing prepended.
    expect(banner.textContent).not.toMatch(/⚠/);
    expect(banner.querySelectorAll('h1,h2,h3,h4')).toHaveLength(0);
  });
});

describe('SweepResults — the runner’s prose reaches the screen byte for byte', () => {
  it('prints the cross-scenario and tally caveats unaltered, markdown included', () => {
    const report = holdout();
    show(report);
    const footer = screen.getByTestId('bias-footer');
    // Byte equality, not a substring match: an earlier cut stripped `**` and
    // backticks out of these strings, which silently edited a warning.
    expect(footer.textContent).toContain(report.cross_scenario_caveat);
    expect(footer.textContent).toContain(report.rejection_tally_caveat);
    expect(report.cross_scenario_caveat).toMatch(/\*\*/);
  });

  it('prints every known bias detail unaltered', () => {
    const report = holdout();
    show(report);
    const footer = screen.getByTestId('bias-footer');
    for (const bias of report.known_biases) {
      expect(within(footer).getByText(bias.title)).toBeInTheDocument();
      expect(footer.textContent).toContain(bias.detail);
    }
  });

  it('prints the holdout semantics unaltered', () => {
    const report = holdout();
    show(report);
    expect(document.body.textContent).toContain(report.holdout_semantics!);
    expect(report.holdout_semantics).toMatch(/\*\*/);
  });
});

describe('SweepResults — provenance', () => {
  it('shows the run, sweep and per-arm hashes read from the payload', () => {
    show(holdout());
    expect(screen.getByText('0f1e2d3c4b5a6978')).toBeInTheDocument(); // run_id
    expect(screen.getByText('deadbeefdeadbeef')).toBeInTheDocument(); // sweep_key
    expect(screen.getByText('1111111111111111')).toBeInTheDocument(); // arm identity
    expect(screen.getByText('b2c3d4e5f6071829')).toBeInTheDocument(); // arm config hash
  });

  it('marks an arm whose config_hash equals base’s', () => {
    show(holdout());
    // `at_the_bid` changes only the fill, which config_hash does not cover.
    const markers = screen.getAllByText('= base');
    expect(markers.length).toBe(1);
    expect(markers[0].getAttribute('title')).toMatch(/changed nothing the hash covers/);
  });

  it('labels a haircut-only arm as fill-model sensitivity, in the grid and the provenance table', () => {
    show(holdout());
    const labels = screen.getAllByText(/fill-model sensitivity/);
    // Once per split in the grid (2), once in the provenance table.
    expect(labels.length).toBe(3);
    expect(labels[0].getAttribute('title')).toMatch(/changes no config key/);
  });

  it('exports the RAW server payload, not the normalised reconstruction', () => {
    // jsdom's Blob carries no `.text()`, so capture what was handed to the
    // constructor instead.
    const parts: string[] = [];
    const OriginalBlob = globalThis.Blob;
    class CapturingBlob {
      constructor(chunks: string[]) {
        parts.push(chunks.join(''));
      }
    }
    vi.stubGlobal('Blob', CapturingBlob);
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: () => 'blob:x',
      revokeObjectURL: () => {},
    });
    try {
      show(holdout(), sweep(), shapedHoldout);
      screen.getByRole('button', { name: /download json/i }).click();
      expect(parts).toHaveLength(1);
      const parsed = JSON.parse(parts[0]);
      // Byte-for-byte the response, including keys the normalised report drops
      // (`run`, `spec`, `summary`) and its nested `grid`.
      expect(parsed).toEqual(shapedHoldout);
      expect(parsed.grid).toBeDefined();
      expect(parsed.run).toBeDefined();
      expect(parsed.summary).toBeDefined();
      // And NOT the reconstruction, which has a flat `rows` the payload lacks.
      expect(parsed.rows).toBeUndefined();
    } finally {
      vi.unstubAllGlobals();
      globalThis.Blob = OriginalBlob;
    }
  });
});
