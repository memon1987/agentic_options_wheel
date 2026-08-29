// FC-060 Layer 4 (PR-B): the results region is gated on the run's STATUS.
//
// The regression: a `submitted` run comes back with `grid: {}` and
// `splits: []`. The first cut treated "the payload parsed" as "the run
// finished", so an in-flight sweep rendered as a completed report that measured
// nothing — the opposite of the truth, and unrecoverable from the screen. Every
// non-`done` status now gets a message that says what is actually happening.
//
// Driven through the real page (and therefore the real hook) against a stubbed
// fetch, because the bug lived in the seam between the two, not inside either.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Simulations from './Simulations';
import shapedHoldout from '../../test/fixtures/sweep_shaped_holdout.json';
import shapedPending from '../../test/fixtures/sweep_shaped_pending.json';

const listRow = (over: Record<string, unknown> = {}) => ({
  run_id: '0f1e2d3c4b5a6978',
  status: 'done',
  submitted_at: '2026-08-29T13:00:00+00:00',
  symbols: ['AAPL', 'NVDA'],
  window_start: '2026-03-01',
  window_end: '2026-05-29',
  cell_count: 16,
  wall_seconds: 98.4,
  scenario_count: 3,
  deduplicated_to: null,
  error: null,
  stuck: false,
  ...over,
});

const ok = (body: unknown) =>
  ({
    ok: true,
    status: 200,
    statusText: '',
    text: async () => JSON.stringify(body),
    json: async () => body,
  }) as unknown as Response;

let fetchMock: ReturnType<typeof vi.fn>;

/** Route each URL the page reads to a canned response. */
const serve = (detail: unknown, listOver: Record<string, unknown> = {}) => {
  fetchMock.mockImplementation((url: string) => {
    if (url.startsWith('/api/v2/sweeps/allowlist')) {
      return Promise.resolve(
        ok({ allowed: [], rejected: [], presets: [], caps: {
          max_symbols: 12, max_scenarios: 20, max_cells: 240,
          max_window_days: 730, min_holdout_days: 60,
        } }),
      );
    }
    if (url.startsWith('/api/v2/sweeps/')) return Promise.resolve(ok(detail));
    if (url === '/api/v2/sweeps') return Promise.resolve(ok([listRow(listOver)]));
    return Promise.resolve(ok({ stock_symbols: ['AAPL', 'NVDA'] }));
  });
};

const show = () =>
  render(
    <MemoryRouter>
      <Simulations />
    </MemoryRouter>,
  );

const statusRegion = async () => await screen.findByTestId('results-status');

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Simulations — a run that has not finished never renders as a report', () => {
  it('shows "running" copy for a submitted run with an empty grid', async () => {
    serve(shapedPending, { status: 'submitted' });
    show();
    const region = await statusRegion();
    expect(region).toHaveAttribute('data-run-status', 'submitted');
    expect(region.textContent).toMatch(/is submitted/);
    expect(region.textContent).toMatch(/refreshes every 15 seconds/);
    // No grid, no medians, no banner — none of it would be true yet.
    expect(screen.queryByTestId('sweep-results')).toBeNull();
    expect(screen.queryByTestId('in-sample-banner')).toBeNull();
  });

  it('shows the stuck hint on a submitted run the server flagged', async () => {
    serve(
      { ...shapedPending, stuck: true, run: { ...shapedPending.run, execution_name: 'exec-9' } },
      { status: 'submitted', stuck: true },
    );
    show();
    const region = await statusRegion();
    expect(region.textContent).toMatch(/past the container-start window/);
    expect(region.textContent).toMatch(/exec-9/);
  });

  it('shows the error text for a failed run', async () => {
    serve(
      {
        ...shapedPending,
        status: 'failed',
        run: { ...shapedPending.run, status: 'failed', error: 'UnadjustedCorporateAction on IWM' },
      },
      { status: 'failed', error: 'UnadjustedCorporateAction on IWM' },
    );
    show();
    const region = await statusRegion();
    expect(region).toHaveAttribute('data-run-status', 'failed');
    expect(region.textContent).toMatch(/failed and produced no report/);
    expect(region.textContent).toMatch(/UnadjustedCorporateAction on IWM/);
    expect(screen.queryByTestId('sweep-results')).toBeNull();
  });

  it('links a deduplicated run at the original rather than showing a report', async () => {
    serve(
      {
        ...shapedPending,
        status: 'deduplicated',
        run: { ...shapedPending.run, status: 'deduplicated', deduplicated_to: 'older-run' },
      },
      { status: 'deduplicated', deduplicated_to: 'older-run' },
    );
    show();
    const region = await statusRegion();
    expect(region.textContent).toMatch(/nothing was replayed/);
    expect(screen.getByRole('button', { name: /Open older-run/ })).toBeInTheDocument();
    expect(screen.queryByTestId('sweep-results')).toBeNull();
  });

  it('refuses to render even a FULL grid under a non-done status', async () => {
    // The payload here has a complete grid; only the status says otherwise.
    // The status is the authority on whether a run finished.
    serve({ ...shapedHoldout, status: 'running', run: { ...shapedHoldout.run, status: 'running' } });
    show();
    const region = await statusRegion();
    expect(region).toHaveAttribute('data-run-status', 'running');
    expect(screen.queryByTestId('sweep-results')).toBeNull();
  });
});

describe('Simulations — a done run renders the report', () => {
  it('renders the grid, and no status placeholder', async () => {
    serve(shapedHoldout);
    show();
    await waitFor(() => expect(screen.getByTestId('sweep-results')).toBeInTheDocument());
    expect(screen.queryByTestId('results-status')).toBeNull();
    expect(screen.getByTestId('cell-base-AAPL-fit')).toBeInTheDocument();
  });

  it('auto-selects the newest run off the BARE ARRAY the list endpoint serves', async () => {
    serve(shapedHoldout);
    show();
    // Nothing is selected until the list parses; if the array were read as
    // `{sweeps}` this would never resolve.
    await waitFor(() => expect(screen.getByTestId('sweep-results')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v2/sweeps/0f1e2d3c4b5a6978'),
      expect.anything(),
    );
  });
});
