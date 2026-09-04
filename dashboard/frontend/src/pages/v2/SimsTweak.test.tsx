// FC-096 Phase E PR-4 — where a submitted tweak LANDS, through the real page.
//
// `TweakBar.test.tsx` pins the component's own outcome handling; this pins the
// half the component cannot see: that the answer becomes a URL, that it PUSHES
// (decision 4 — a submit is a user selection, so Back returns to the cell they
// tweaked from), and that the notice survives the destination run's load, which
// unmounts the bar that produced it.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from 'react-router-dom';
import Simulations from './Simulations';
import shaped13cc from '../../test/fixtures/sweep_shaped_13cc.json';
import allowlistFixture from '../../test/fixtures/sweep_allowlist.json';
import { resetArtifactCacheForTests } from '../../hooks/artifactCache';
import { resetSessionExpiredSignal } from '../../hooks/iapSession';

const RUN = '13cc2729d1c74211';
const ARM = 'strategy_min_put_premium_0.65';

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

const posted = (status: number, body: unknown) =>
  ({
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    text: async () => JSON.stringify(body),
    json: async () => body,
    headers: new Headers({ 'Content-Type': 'application/json' }),
  }) as unknown as Response;

const shapedFor = (runId: string, over: Record<string, unknown> = {}) => ({
  ...shaped13cc,
  run_id: runId,
  run: { ...shaped13cc.run, run_id: runId, ...((over.run as object) ?? {}) },
  ...over,
});

/** The destination of a 202: a run that exists and has not finished. */
const runningRun = (runId: string) =>
  shapedFor(runId, {
    run: { ...shaped13cc.run, run_id: runId, status: 'running' },
    status: 'running',
    grid: {},
    splits: [],
  });

const listRow = {
  run_id: RUN,
  status: 'done',
  submitted_at: '2026-09-03T07:00:00+00:00',
  symbols: ['GOOGL'],
  window_start: '2025-09-01',
  window_end: '2026-09-01',
  cell_count: 4,
  scenario_count: 2,
  deduplicated_to: null,
  error: null,
  stuck: false,
};

let fetchMock: ReturnType<typeof vi.fn>;
let simAnswer: Response;
let detailByRun: Record<string, unknown>;
let navLog: Array<{ pathname: string; type: string }>;

function Probe() {
  const location = useLocation();
  const type = useNavigationType();
  const last = navLog[navLog.length - 1];
  if (!last || last.pathname !== location.pathname || last.type !== type) {
    navLog.push({ pathname: location.pathname, type });
  }
  return null;
}

const show = (path: string) =>
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

beforeEach(() => {
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
  navLog = [];
  detailByRun = {};
  simAnswer = posted(202, { run_id: 'new456', cell_count: 4 });
  fetchMock = vi.fn();
  fetchMock.mockImplementation((url: string) => {
    if (url === '/api/v2/sims/run') return Promise.resolve(simAnswer);
    if (url.startsWith('/api/v2/sweeps/allowlist')) return Promise.resolve(ok(allowlistFixture));
    if (/\/artifacts\/|\/bars\//.test(url)) {
      return Promise.resolve(notFound('No detail artifact for this cell in this run.'));
    }
    if (url.startsWith('/api/v2/sweeps/')) {
      const id = url.slice('/api/v2/sweeps/'.length);
      return Promise.resolve(ok(detailByRun[id] ?? shapedFor(id)));
    }
    if (url === '/api/v2/sweeps') return Promise.resolve(ok([listRow]));
    return Promise.resolve(ok({ stock_symbols: ['GOOGL'] }));
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
});

const tweakAndSubmit = async () => {
  show(`/sims/${RUN}/position_20pct/GOOGL/holdout`);
  await waitFor(() => expect(screen.getByTestId('tweak-bar')).toBeTruthy());
  // The controls only exist once the allowlist has arrived.
  await waitFor(() => expect(screen.getByLabelText('strategy.min_put_premium')).toBeTruthy());
  fireEvent.change(screen.getByLabelText('strategy.min_put_premium'), {
    target: { value: '0.65' },
  });
  fireEvent.click(screen.getByTestId('tweak-submit'));
};

describe('a 202 lands on the NEW run’s cell and polls it', () => {
  it('pushes /sims/<new run>/<arm>/<symbol>/<split> and keeps the symbol and split', async () => {
    detailByRun.new456 = runningRun('new456');
    await tweakAndSubmit();
    await waitFor(() =>
      expect(navLog.map((n) => n.pathname)).toContain(`/sims/new456/${ARM}/GOOGL/holdout`),
    );
    const entry = navLog.find((n) => n.pathname === `/sims/new456/${ARM}/GOOGL/holdout`)!;
    // PUSH, not REPLACE: a submit is the operator's own selection, so Back
    // returns them to the cell they tweaked from (decision 4).
    expect(entry.type).toBe('PUSH');
    // A `running` destination renders the polling shell, never a report.
    await waitFor(() =>
      expect(screen.getByTestId('results-status').dataset.runStatus).toBe('running'),
    );
  });

  it('polls the destination — it re-reads the new run, not the old one', async () => {
    detailByRun.new456 = runningRun('new456');
    await tweakAndSubmit();
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => c[0] === '/api/v2/sweeps/new456')).toBe(true),
    );
  });
});

describe('a 200 lands on the run that already answered', () => {
  it('pushes to the PRIOR run and says nothing was replayed', async () => {
    // The mutation: treat a 200 like a 202. The operator would be told their
    // tweak is being measured, and the page would poll a finished run for ever.
    simAnswer = posted(200, { run_id: 'prior123', deduplicated: true, sweep_key: 'k' });
    await tweakAndSubmit();
    await waitFor(() =>
      expect(navLog.some((n) => n.pathname.startsWith('/sims/prior123/'))).toBe(true),
    );
    const entry = navLog.find((n) => n.pathname === `/sims/prior123/${ARM}/GOOGL/holdout`)!;
    expect(entry.type).toBe('PUSH');
    const notice = await screen.findByTestId('tweak-notice');
    expect(notice.textContent).toContain('prior123');
    expect(notice.textContent).toContain('NOTHING was replayed');
  });

  it('survives the destination’s load — the bar that produced it unmounts', async () => {
    // The notice cannot live in `TweakBar`: `ResultsRegion` renders its loading
    // shell while the destination run's detail is in flight, which unmounts the
    // whole console. It lives on the page and is keyed to the run it describes.
    simAnswer = posted(200, { run_id: 'prior123', deduplicated: true, sweep_key: 'k' });
    await tweakAndSubmit();
    const notice = await screen.findByTestId('tweak-notice');
    expect(notice.textContent).toContain('Answered from stored run');
  });
});

describe('a refusal never navigates', () => {
  it('stays on the cell and renders the 409 in place', async () => {
    simAnswer = posted(409, {
      detail: 'this instance is already replaying a simulation.',
      run_id: null,
    });
    await tweakAndSubmit();
    await waitFor(() => expect(screen.getByTestId('tweak-outcome')).toBeTruthy());
    expect(navLog.some((n) => n.pathname.startsWith('/sims/new456'))).toBe(false);
    expect(screen.queryByTestId('tweak-notice')).toBeNull();
  });
});
