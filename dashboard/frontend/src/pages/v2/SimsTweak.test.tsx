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
/** A second run, so the operator can switch away mid-flight (R5). */
const OTHER_RUN = 'aaaa1111bbbb2222';

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
let simAnswer: Response | Promise<Response>;
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
      const custom = detailByRun[id];
      if (custom === 'MISSING') return Promise.resolve(notFound(`no sweep ${id}`));
      return Promise.resolve(ok(custom ?? shapedFor(id)));
    }
    if (url === '/api/v2/sweeps') {
      return Promise.resolve(ok([listRow, { ...listRow, run_id: OTHER_RUN }]));
    }
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

describe('the notice sits ABOVE the shells (review R3)', () => {
  it('acknowledges a 202 on the RUNNING shell, where the generic Job copy is', async () => {
    // It used to render only inside the `done` branch, so the destination of a
    // 202 — which is by definition not done — showed "6-8 minutes" and no sign
    // that anything had been submitted.
    detailByRun.new456 = runningRun('new456');
    await tweakAndSubmit();
    const notice = await screen.findByTestId('tweak-notice');
    expect(notice.dataset.noticeKind).toBe('accepted');
    expect(notice.textContent).toContain('new456');
    expect(notice.textContent).toContain('polls it until it finishes');
    const shell = screen.getByTestId('results-status');
    expect(shell.dataset.runStatus).toBe('running');
    // ABOVE, asserted rather than left to whoever edits the JSX next.
    expect(notice.compareDocumentPosition(shell) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('drops the polling claim once the destination is terminal', async () => {
    // The same notice on a finished run used to go on saying "this page polls
    // it until it finishes" about a run that had already finished.
    detailByRun.new456 = shapedFor('new456');
    await tweakAndSubmit();
    // The destination's detail arrives a tick after the notice does; the
    // clause is a function of the LIVE status, so it settles on "finished".
    await waitFor(() =>
      expect(screen.getByTestId('tweak-notice').textContent).toContain('It has finished.'),
    );
    expect(screen.getByTestId('tweak-notice').textContent).not.toContain(
      'polls it until it finishes',
    );
  });

  it('reads a first-15s 404 as streaming lag, not as an unreadable run', async () => {
    detailByRun.new456 = 'MISSING';
    await tweakAndSubmit();
    const lag = await screen.findByTestId('results-streaming-lag');
    expect(lag.textContent).toContain('not visible yet');
    expect(screen.queryByText(/state is/)).toBeNull();
  });
});

describe('an in-flight submit outlives the bar (review R5)', () => {
  it('surfaces a LATE refusal on the run it was submitted from', async () => {
    // The bar is keyed on the run, so a run switch during the ≤150 s wait
    // unmounts it. The refusal must still land — on the source run's screen,
    // which is where the operator asked the question.
    let release: (r: Response) => void = () => undefined;
    simAnswer = new Promise<Response>((r) => (release = r));
    await tweakAndSubmit();
    await waitFor(() => expect(screen.getByTestId('tweak-submit')).toBeDisabled());
    release(posted(422, { detail: 'the service refused this spec' }));
    const outcome = await screen.findByTestId('tweak-outcome');
    expect(outcome.dataset.outcome).toBe('invalid');
    expect(screen.getByTestId('tweak-detail').textContent).toBe('the service refused this spec');
  });

  it('does NOT navigate on a late 2xx once the operator has moved on', async () => {
    // Yanking them off what they moved to read is the same defect as losing
    // the refusal. The answer is offered as a link instead.
    detailByRun.new456 = runningRun('new456');
    let release: (r: Response) => void = () => undefined;
    simAnswer = new Promise<Response>((r) => (release = r));
    await tweakAndSubmit();
    await waitFor(() => expect(screen.getByTestId('tweak-submit')).toBeDisabled());

    // The operator selects another run while the submit is in flight.
    fireEvent.click(screen.getByText(OTHER_RUN));
    await waitFor(() => expect(navLog.some((n) => n.pathname.includes(OTHER_RUN))).toBe(true));
    const before = navLog.length;

    release(posted(202, { run_id: 'new456', cell_count: 4 }));
    const notice = await screen.findByTestId('tweak-notice');
    expect(notice.textContent).toContain('You had moved to another run');
    expect(screen.getByRole('link', { name: /Open run new456/ })).toBeTruthy();
    expect(navLog.length).toBe(before);
    expect(navLog.some((n) => n.pathname.startsWith('/sims/new456'))).toBe(false);
  });
});
