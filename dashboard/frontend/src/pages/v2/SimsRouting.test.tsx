// FC-096 Phase E PR-2 (decision 4): the URL is the state, and push ≠ replace.
//
// The rule under test: a USER selection pushes so Back walks their own history;
// an AUTOMATIC selection (newest run on arrival, default cell when a run
// resolves, repair of a stale deep link) replaces so Back never has to fight an
// auto-select to leave /sims.
//
// History length is the assertion because it is the only observable that
// distinguishes the two. The mutation this kills — "replace on every
// navigation" — leaves every rendered screen identical and Back permanently
// broken.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
// `fireEvent` rather than `user-event`: the latter is not a dependency of this
// project and the plan adds none (decision 1's "no new dependency" applies to
// the whole PR, not only to charting). House precedent: `SubmitSweep.test.tsx`.
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useNavigationType,
} from 'react-router-dom';
import Simulations, { cellExists, defaultCell, defaultSplit } from './Simulations';
import shaped13cc from '../../test/fixtures/sweep_shaped_13cc.json';
import { normaliseSweepDetail } from '../../components/v2/sims/normaliseReport';
import { resetArtifactCacheForTests } from '../../hooks/artifactCache';
import { resetSessionExpiredSignal } from '../../hooks/iapSession';

const RUN = '13cc2729d1c74211';

const ok = (body: unknown) =>
  ({
    ok: true,
    status: 200,
    statusText: '',
    text: async () => JSON.stringify(body),
    json: async () => body,
  }) as unknown as Response;

const notFound = (detail: string) =>
  ({
    ok: false,
    status: 404,
    statusText: 'Not Found',
    text: async () => JSON.stringify({ detail }),
    json: async () => ({ detail }),
  }) as unknown as Response;

const listRow = {
  run_id: RUN,
  status: 'done',
  submitted_at: '2026-09-03T07:00:00+00:00',
  symbols: ['GOOGL'],
  window_start: '2025-09-01',
  window_end: '2026-06-02',
  cell_count: 4,
  scenario_count: 2,
  deduplicated_to: null,
  error: null,
  stuck: false,
};

let fetchMock: ReturnType<typeof vi.fn>;
let seen: { pathname: string } = { pathname: '' };
/** Every navigation this render performed, in order: PUSH / REPLACE / POP. */
let navLog: Array<{ pathname: string; type: string }> = [];

/** The router's own `navigate`, captured so a test can pop its stack. */
let go: (delta: number) => void = () => undefined;

function Probe() {
  const location = useLocation();
  const type = useNavigationType();
  const navigate = useNavigate();
  go = (delta) => navigate(delta);
  seen = { pathname: location.pathname };
  const last = navLog[navLog.length - 1];
  if (!last || last.pathname !== location.pathname || last.type !== type) {
    navLog.push({ pathname: location.pathname, type });
  }
  return null;
}

const show = (path = '/sims') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Probe />
      <Routes>
        <Route path="/sims" element={<Simulations />} />
        <Route path="/sims/:runId" element={<Simulations />} />
        <Route path="/sims/:runId/:scenario/:symbol/:split" element={<Simulations />} />
      </Routes>
    </MemoryRouter>,
  );

/**
 * A real POP through the router's OWN stack.
 *
 * `window.history.back()` is inert against `MemoryRouter` (it keeps its own
 * in-memory stack), and a data router cannot stand in — its internal
 * `new Request(...)` rejects jsdom's `AbortSignal` under undici. Popping through
 * the router's own `navigate(-1)` walks exactly the entries the page pushed.
 */
const back = async () => {
  await act(async () => {
    go(-1);
  });
};

beforeEach(() => {
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
  seen = { pathname: '' };
  navLog = [];
  fetchMock = vi.fn();
  fetchMock.mockImplementation((url: string) => {
    const target = url;
    if (target.startsWith('/api/v2/sweeps/allowlist')) {
      return Promise.resolve(
        ok({
          allowed: [],
          rejected: [],
          presets: [],
          caps: {
            max_symbols: 12,
            max_scenarios: 20,
            max_cells: 240,
            max_window_days: 730,
            min_holdout_days: 60,
          },
        }),
      );
    }
    // The console's two reads: absent, which is a rendered state and not an
    // error, and keeps these routing tests off the artifact path entirely.
    if (/\/artifacts\/|\/bars\//.test(target)) {
      return Promise.resolve(notFound('No detail artifact for this cell in this run.'));
    }
    if (target.startsWith('/api/v2/sweeps/')) return Promise.resolve(ok(shaped13cc));
    if (target === '/api/v2/sweeps') return Promise.resolve(ok([listRow]));
    return Promise.resolve(ok({ stock_symbols: ['GOOGL'] }));
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
});

describe('the pure selection rules', () => {
  const report = normaliseSweepDetail(shaped13cc)!.results!;

  it('defaults to holdout when the run has one — never fit', () => {
    expect(defaultSplit(['fit', 'holdout'])).toBe('holdout');
    expect(defaultSplit(['all'])).toBe('all');
    expect(defaultSplit([])).toBeNull();
    // The mutation: falling back to the FIRST split, which is `fit` on a
    // windowed run. Opening on the in-sample window is the reading the holdout
    // exists to prevent.
    expect(defaultSplit(['fit', 'holdout'])).not.toBe('fit');
  });

  it('opens on the first non-base arm, the first symbol, the default split', () => {
    expect(defaultCell(report)).toEqual({
      scenario: 'position_20pct',
      symbol: 'GOOGL',
      split: 'holdout',
    });
  });

  it('a base-only run opens on base', () => {
    expect(defaultCell({ ...report, scenarios: ['base'] })).toEqual({
      scenario: 'base',
      symbol: 'GOOGL',
      split: 'holdout',
    });
  });

  it('cellExists rejects a cell this run does not have', () => {
    expect(cellExists(report, { scenario: 'base', symbol: 'GOOGL', split: 'fit' })).toBe(true);
    expect(cellExists(report, { scenario: 'nope', symbol: 'GOOGL', split: 'fit' })).toBe(false);
    expect(cellExists(report, { scenario: 'base', symbol: 'NVDA', split: 'fit' })).toBe(false);
    expect(cellExists(report, { scenario: 'base', symbol: 'GOOGL', split: 'all' })).toBe(false);
  });
});

describe('deep links and navigation', () => {
  it('a deep link opens exactly that cell, with no redirect', async () => {
    show(`/sims/${RUN}/base/GOOGL/fit`);
    await screen.findByTestId('verdict-strip');
    expect(seen.pathname).toBe(`/sims/${RUN}/base/GOOGL/fit`);
    expect(screen.getByTestId('verdict-strip').textContent).toMatch(/base/);
    expect(screen.getByTestId('verdict-strip').textContent).toMatch(/fit/);
  });

  it('/sims auto-selects the newest run and its default cell by REPLACING', async () => {
    show('/sims');
    await waitFor(() => expect(seen.pathname).toBe(`/sims/${RUN}/position_20pct/GOOGL/holdout`));
    // Two auto-steps happened (run, then cell) and NEITHER pushed. The mutation
    // — pushing here — leaves every rendered screen identical and makes Back
    // walk through two auto-selects before it can leave /sims.
    expect(navLog.map((n) => n.type)).toEqual(['POP', 'REPLACE', 'REPLACE']);
    expect(navLog.map((n) => n.pathname)).toEqual([
      '/sims',
      `/sims/${RUN}`,
      `/sims/${RUN}/position_20pct/GOOGL/holdout`,
    ]);
  });

  it('a stale deep link is repaired to the default cell, by replacing', async () => {
    show(`/sims/${RUN}/an_arm_this_run_never_had/GOOGL/fit`);
    await waitFor(() => expect(seen.pathname).toBe(`/sims/${RUN}/position_20pct/GOOGL/holdout`));
    expect(navLog.map((n) => n.type)).toEqual(['POP', 'REPLACE']);
  });

  it('a user cell selection PUSHES, and Back returns to the previous cell', async () => {
    show(`/sims/${RUN}/base/GOOGL/fit`);
    await screen.findByTestId('cell-selector');

    fireEvent.click(screen.getByRole('button', { name: 'holdout' }));
    await waitFor(() => expect(seen.pathname).toBe(`/sims/${RUN}/base/GOOGL/holdout`));

    fireEvent.click(screen.getByRole('button', { name: 'position_20pct' }));
    await waitFor(() =>
      expect(seen.pathname).toBe(`/sims/${RUN}/position_20pct/GOOGL/holdout`),
    );

    expect(navLog.map((n) => n.type)).toEqual(['POP', 'PUSH', 'PUSH']);

    await back();
    expect(seen.pathname).toBe(`/sims/${RUN}/base/GOOGL/holdout`);
    await back();
    expect(seen.pathname).toBe(`/sims/${RUN}/base/GOOGL/fit`);
  });

  it('the split switch offers exactly the run’s splits', async () => {
    show(`/sims/${RUN}/base/GOOGL/fit`);
    const swi = await screen.findByTestId('split-switch');
    const labels = Array.from(swi.querySelectorAll('button')).map((b) => b.textContent);
    expect(labels).toEqual(['fit', 'holdout']);
    // `all` is the OTHER shape a run can have; offering it here can only
    // produce a 404 and a confused reading of why.
    expect(labels).not.toContain('all');
  });
});
