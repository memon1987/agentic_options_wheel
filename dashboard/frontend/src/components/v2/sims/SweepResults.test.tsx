// FC-060 Layer 4 (PR-B): the results view.
//
// The two plan rules under test are the ones that decide whether this page can
// mislead: the four cell kinds must be visually distinct, and no cell below the
// activity floor may be styled as a return. The third is the in-sample banner,
// which must appear IFF the run had no holdout.
//
// FIXTURES: `src/test/fixtures/sweep_report_*.json` were produced by running the
// engine's OWN `src/backtesting/scenarios/report.py::render_json` over a
// hand-built `SweepResult` (the dev chain cache is empty, so a live sweep would
// need a cold vendor materialisation). They are therefore byte-accurate in
// SHAPE — every key, including `sign_agreement`, `delta_vs_base`, the verbatim
// bias strings — while the numbers are synthetic. A shape drift in the runner
// breaks these tests, which is the point.

import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import SweepResults from './SweepResults';
import type { SweepReport, SweepRow } from '../../../types/v2';
import { normaliseReport } from './normaliseReport';
import holdoutJson from '../../../test/fixtures/sweep_report_holdout.json';
import insampleJson from '../../../test/fixtures/sweep_report_insample.json';
import shapedHoldout from '../../../test/fixtures/sweep_shaped_holdout.json';

const holdoutReport = holdoutJson as unknown as SweepReport;
const insampleReport = insampleJson as unknown as SweepReport;

const sweep = (over: Partial<SweepRow> = {}): SweepRow => ({
  // Deliberately NOT equal to the report's base_config_hash: the provenance
  // block must show four different identities, not one repeated.
  run_id: '0f1e2d3c4b5a6978',
  sweep_key: 'deadbeefdeadbeef',
  status: 'done',
  deduplicated_to: null,
  submitted_at: '2026-08-29T13:00:00+00:00',
  started_at: '2026-08-29T13:04:00+00:00',
  finished_at: '2026-08-29T13:12:00+00:00',
  submitted_via: 'dashboard',
  execution_name: 'backtest-sweep-abcde',
  git_commit: 'f9ffd22',
  engine_version: 'fc-069-scanner-rewire',
  base_config_hash: 'a1b2c3d4e5f60718',
  base_config_json: null,
  spec_json: null,
  symbols: ['AAPL', 'NVDA'],
  window_start: '2026-03-01',
  window_end: '2026-05-29',
  holdout_start: '2026-05-01',
  in_sample_only: false,
  scenario_count: 2,
  cell_count: 12,
  wall_seconds: 98.4,
  materialise_seconds: 60.1,
  replay_seconds: 11.5,
  provider_fetches: 4,
  bar_cache_hits: 812,
  lake_summary_json: null,
  error: null,
  ...over,
});

const cell = (scenario: string, symbol: string, split: string) =>
  screen.getByTestId(`cell-${scenario}-${symbol}-${split}`);

describe('SweepResults — the four cell renderings', () => {
  it('renders a measured cell as a signed return plus the verdict glyph', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    const c = cell('base', 'AAPL', 'fit');
    expect(c).toHaveAttribute('data-cell-kind', 'return');
    expect(c.textContent).toBe('+18.4% +'); // 'fit' -> '+'
    expect(c.className).toMatch(/text-green-400/);
  });

  it('renders a negative measured cell in red, still as a return', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    const c = cell('call_floor_0_50', 'NVDA', 'fit');
    expect(c).toHaveAttribute('data-cell-kind', 'return');
    expect(c.textContent).toBe('-5.2% -'); // 'unfit' -> '-'
    expect(c.className).toMatch(/text-red-400/);
  });

  it('renders an insufficient cell as `insuf` — never as a number', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    const c = cell('puts_15_25', 'NVDA', 'holdout');
    expect(c).toHaveAttribute('data-cell-kind', 'insuf');
    expect(c.textContent).toBe('insuf');
    expect(c.textContent).not.toMatch(/%/);
    // No P&L colour: an unmeasured cell must not read as a good or bad number.
    expect(c.className).not.toMatch(/text-green-400|text-red-400/);
  });

  it('renders a low-activity cell as `low-act N%` with its fraction, not its return', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    const c = cell('puts_15_25', 'NVDA', 'fit');
    expect(c).toHaveAttribute('data-cell-kind', 'low-act');
    // The underlying row carries annualized_return = 0.402. It must NOT show.
    expect(c.textContent).toBe('low-act 9%');
    expect(c.textContent).not.toMatch(/40/);
    expect(c.className).not.toMatch(/text-green-400/);
  });

  it('renders an errored cell as `err` with the error on hover', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    const c = cell('call_floor_0_50', 'AAPL', 'fit');
    expect(c).toHaveAttribute('data-cell-kind', 'err');
    expect(c.textContent).toBe('err');
    expect(c.getAttribute('title')).toMatch(/UnadjustedCorporateAction/);
  });

  it('gives the four kinds four distinct styles', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    const styles = [
      cell('base', 'AAPL', 'fit'),
      cell('puts_15_25', 'NVDA', 'holdout'),
      cell('puts_15_25', 'NVDA', 'fit'),
      cell('call_floor_0_50', 'AAPL', 'fit'),
    ].map((el) => el.className);
    expect(new Set(styles).size).toBe(4);
  });
});

describe('SweepResults — aggregates never appear without the grid', () => {
  it('renders the per-symbol grid for every split', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    for (const split of ['fit', 'holdout']) {
      for (const scenario of holdoutReport.scenarios) {
        for (const symbol of holdoutReport.symbols) {
          expect(cell(scenario, symbol, split)).toBeInTheDocument();
        }
      }
    }
  });

  it('shows the partition counts and the Δ vs base with its n', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    // puts_15_25 on fit: AAPL measured, NVDA low-act -> 1 · 0 · 1 · 0
    const row = screen.getAllByText('puts_15_25')[0].closest('tr')!;
    const cells = within(row).getAllByRole('cell');
    const counts = cells[cells.length - 3].textContent!.replace(/\s/g, '');
    expect(counts).toBe('1·0·1·0');
    // Δ over the common measured subset — one symbol, and it says so.
    expect(cells[cells.length - 1].textContent).toMatch(/\(n=1\)/);
  });

  it('says "no measured cell" instead of printing a median of nothing', () => {
    const empty: SweepReport = {
      ...holdoutReport,
      rows: holdoutReport.rows.map((r) =>
        r.scenario === 'call_floor_0_50'
          ? { ...r, measured: false, insufficient: true, low_activity: false, error: null }
          : r,
      ),
    };
    render(<SweepResults sweep={sweep()} report={empty} />);
    expect(screen.getAllByText('no measured cell').length).toBeGreaterThan(0);
  });

  it('renders fit vs holdout with sign agreement only when a holdout ran', () => {
    const { unmount } = render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    expect(screen.getByText('Fit vs holdout')).toBeInTheDocument();
    expect(screen.getByText('sign agreement')).toBeInTheDocument();
    unmount();
    render(<SweepResults sweep={sweep({ in_sample_only: true })} report={insampleReport} />);
    expect(screen.queryByText('Fit vs holdout')).toBeNull();
  });
});

describe('SweepResults — the in-sample banner appears IFF in_sample_only', () => {
  it('is absent on a run with a holdout', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    expect(holdoutReport.in_sample_only).toBe(false);
    expect(holdoutReport.in_sample_banner).toBeNull();
    expect(screen.queryByTestId('in-sample-banner')).toBeNull();
  });

  it('is present, verbatim, on a run without one', () => {
    render(<SweepResults sweep={sweep({ in_sample_only: true })} report={insampleReport} />);
    expect(insampleReport.in_sample_only).toBe(true);
    const banner = screen.getByTestId('in-sample-banner');
    expect(banner.textContent).toMatch(/IN-SAMPLE ONLY/);
    // The runner's own argument, not a paraphrase of it.
    expect(banner.textContent).toMatch(/luckiest one than the best one/);
    expect(banner.textContent).toMatch(/refuted, not merely unconfirmed/);
  });
});

describe('SweepResults — renders identically from either wire shape', () => {
  it('gives the same four cell kinds when fed PR-A’s shape_results payload', () => {
    const normalised = normaliseReport(shapedHoldout, sweep())!;
    render(<SweepResults sweep={sweep()} report={normalised} />);
    expect(cell('base', 'AAPL', 'fit')).toHaveAttribute('data-cell-kind', 'return');
    expect(cell('puts_15_25', 'NVDA', 'fit')).toHaveAttribute('data-cell-kind', 'low-act');
    expect(cell('puts_15_25', 'NVDA', 'holdout')).toHaveAttribute('data-cell-kind', 'insuf');
    expect(cell('call_floor_0_50', 'AAPL', 'fit')).toHaveAttribute('data-cell-kind', 'err');
    // Same text too — the low-act cell still refuses to print its 40.2% return.
    expect(cell('puts_15_25', 'NVDA', 'fit').textContent).toBe('low-act 9%');
    expect(cell('base', 'AAPL', 'fit').textContent).toBe('+18.4% +');
  });
});

describe('SweepResults — the bias footer and hashes are verbatim provenance', () => {
  it('prints every known bias, the cross-scenario caveat and the tally caveat', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    const footer = screen.getByTestId('bias-footer');
    for (const bias of holdoutReport.known_biases) {
      expect(within(footer).getByText(bias.title)).toBeInTheDocument();
    }
    expect(footer.textContent).toMatch(/biased against the call-heavier one/);
    expect(footer.textContent).toMatch(/deliberately carries no binding_constraint column/);
  });

  it('shows the run, sweep and scenario hashes', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    expect(screen.getByText('0f1e2d3c4b5a6978')).toBeInTheDocument(); // run_id
    expect(screen.getByText('deadbeefdeadbeef')).toBeInTheDocument(); // sweep_key
    // The base config hash, which the `base` ARM's config_hash equals by
    // construction — so it legitimately shows twice.
    expect(screen.getAllByText('a1b2c3d4e5f60718').length).toBe(2);
    // The ARM's identity. config_hash cannot separate two arms that differ
    // outside its nine strategy keys, so scenario_hash has to be shown too.
    expect(screen.getByText('1111111111111111')).toBeInTheDocument();
    expect(screen.getByText('c3d4e5f607182930')).toBeInTheDocument();
  });

  it('offers a JSON download', () => {
    render(<SweepResults sweep={sweep()} report={holdoutReport} />);
    expect(screen.getByRole('button', { name: /download json/i })).toBeInTheDocument();
  });
});
