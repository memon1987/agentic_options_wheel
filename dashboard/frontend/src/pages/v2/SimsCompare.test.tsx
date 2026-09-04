// FC-096 Phase E PR-5: `/sims/compare?a=…&b=…` — the URL IS the comparison.
//
// The rules under test: a deep link lands on the pair it names and survives a
// reload; a user's pick PUSHES so Back walks their own comparisons; an
// automatic repair REPLACES so Back never fights it; a malformed `a` goes to
// `/sims`; a malformed `b` drops to "no second cell" without taking `a` down;
// `b` is EMPTY when the URL does not name one, and the page never fills it in.
//
// The static `/sims/compare` route must outrank `/sims/:runId`, so `compare` is
// never read as a run id. React Router 6 ranks static above dynamic whatever
// the source order — this asserts it rather than assuming it.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useNavigationType,
} from 'react-router-dom';
import SimsCompare from './SimsCompare';
import Simulations from './Simulations';
import shaped13cc from '../../test/fixtures/sweep_shaped_13cc.json';
import shapedA48d from '../../test/fixtures/sweep_shaped_a48d.json';
import { resetArtifactCacheForTests } from '../../hooks/artifactCache';
import { resetSessionExpiredSignal } from '../../hooks/iapSession';

const RUN = '13cc2729d1c74211';
const RUN_B = 'a48d7bb064194e0f';

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

const listRow = (runId: string) => ({
  run_id: runId,
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
});

let fetchMock: ReturnType<typeof vi.fn>;
let seen = { pathname: '', search: '' };
let navLog: Array<{ url: string; type: string }> = [];
let go: (delta: number) => void = () => undefined;

function Probe() {
  const location = useLocation();
  const type = useNavigationType();
  const navigate = useNavigate();
  go = (delta) => navigate(delta);
  seen = { pathname: location.pathname, search: location.search };
  const url = location.pathname + location.search;
  const last = navLog[navLog.length - 1];
  if (!last || last.url !== url || last.type !== type) navLog.push({ url, type });
  return null;
}

/** The real route table's `/sims` family, in App.tsx's own order. */
const show = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Probe />
      <Routes>
        <Route path="/sims" element={<Simulations />} />
        <Route path="/sims/compare" element={<SimsCompare />} />
        <Route path="/sims/:runId" element={<Simulations />} />
        <Route path="/sims/:runId/:scenario/:symbol/:split" element={<Simulations />} />
      </Routes>
    </MemoryRouter>,
  );

const back = async () => {
  await act(async () => {
    go(-1);
  });
};

const ref = (runId: string, scenario = 'base', symbol = 'GOOGL', split = 'fit') =>
  `${runId}:${scenario}:${symbol}:${split}`;

beforeEach(() => {
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
  seen = { pathname: '', search: '' };
  navLog = [];
  fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.startsWith('/api/v2/sweeps/allowlist')) {
      return Promise.resolve(ok({ allowed: [], rejected: [], presets: [], caps: {} }));
    }
    // Both stored objects answer 404 — a rendered state, not an error — which
    // keeps these routing tests off the artifact path entirely.
    if (/\/artifacts\/|\/bars\//.test(url)) {
      return Promise.resolve(notFound('No detail artifact for this cell in this run.'));
    }
    if (url === `/api/v2/sweeps/${RUN}`) return Promise.resolve(ok(shaped13cc));
    if (url === `/api/v2/sweeps/${RUN_B}`) return Promise.resolve(ok(shapedA48d));
    if (url.startsWith('/api/v2/sweeps/')) return Promise.resolve(notFound('no such run'));
    if (url === '/api/v2/sweeps') return Promise.resolve(ok([listRow(RUN), listRow(RUN_B)]));
    return Promise.resolve(ok({ stock_symbols: ['GOOGL'] }));
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
});

describe('the route', () => {
  it('renders the compare page — `compare` is never read as a run id', async () => {
    show(`/sims/compare?a=${ref(RUN)}`);
    await screen.findByText('Compare two cells');
    // The run page would have rendered its own header instead.
    expect(screen.queryByText('Simulations')).toBeNull();
  });

  it('lands on the pair the URL names, both sides', async () => {
    show(`/sims/compare?a=${ref(RUN, 'position_20pct')}&b=${ref(RUN, 'base')}`);
    await waitFor(() => expect(screen.getByTestId('alignment-matrix')).toBeTruthy());
    expect((screen.getByTestId('picker-A-arm') as HTMLSelectElement).value).toBe('position_20pct');
    expect((screen.getByTestId('picker-B-arm') as HTMLSelectElement).value).toBe('base');
  });

  it('survives a reload: the same URL rendered fresh is the same screen', async () => {
    const url = `/sims/compare?a=${ref(RUN, 'position_20pct', 'GOOGL', 'holdout')}&b=${ref(RUN)}`;
    show(url);
    await waitFor(() => expect(screen.getByTestId('alignment-matrix')).toBeTruthy());
    const first = (screen.getByTestId('picker-A-split') as HTMLSelectElement).value;
    // A "reload" is a second mount of the same entry with no carried state.
    resetArtifactCacheForTests();
    show(url);
    await waitFor(() => expect(screen.getAllByTestId('alignment-matrix').length).toBe(2));
    const splits = screen.getAllByTestId('picker-A-split') as HTMLSelectElement[];
    expect(splits[1].value).toBe(first);
    expect(splits[1].value).toBe('holdout');
  });
});

describe('`b` is empty unless the URL names one', () => {
  it('renders A alone and asks for a second cell — it does not pick one', async () => {
    show(`/sims/compare?a=${ref(RUN)}`);
    await screen.findByTestId('compare-awaiting-b');
    expect(seen.search).not.toContain('b=');
    // The mutation this kills: defaulting `b` to base, or to the first arm.
    expect((screen.getByTestId('picker-B') as HTMLElement).textContent).toContain(
      'No cell chosen',
    );
    expect(screen.queryByTestId('alignment-matrix')).toBeNull();
  });

  it('clearing B removes it from the URL and stops comparing', async () => {
    show(`/sims/compare?a=${ref(RUN, 'position_20pct')}&b=${ref(RUN)}`);
    await waitFor(() => expect(screen.getByTestId('alignment-matrix')).toBeTruthy());
    await act(async () => {
      fireEvent.click(screen.getByTestId('picker-B-clear'));
    });
    await waitFor(() => expect(screen.queryByTestId('alignment-matrix')).toBeNull());
    expect(seen.search).not.toContain('b=');
  });
});

describe('push vs replace', () => {
  it('a user’s pick PUSHES, so Back returns to the previous comparison', async () => {
    show(`/sims/compare?a=${ref(RUN, 'position_20pct')}&b=${ref(RUN, 'base')}`);
    await waitFor(() => expect(screen.getByTestId('alignment-matrix')).toBeTruthy());
    const before = seen.search;

    await act(async () => {
      fireEvent.change(screen.getByTestId('picker-A-split'), { target: { value: 'holdout' } });
    });
    await waitFor(() => expect(seen.search).toContain('holdout'));
    expect(navLog[navLog.length - 1].type).toBe('PUSH');

    await back();
    await waitFor(() => expect(seen.search).toBe(before));
  });

  it('the automatic repair of a stale cell REPLACES', async () => {
    // `no_such_arm` is not in this run: the repair effect swaps in the run's
    // default cell. Replacing, so Back leaves the page rather than bouncing off
    // a correction the operator never made.
    show(`/sims/compare?a=${ref(RUN, 'no_such_arm')}`);
    await waitFor(() => expect(seen.search).toContain('position_20pct'));
    expect(navLog.filter((n) => n.type === 'PUSH')).toHaveLength(0);
    expect(navLog[navLog.length - 1].type).toBe('REPLACE');
  });

  it('swapping A and B is a user action, so it pushes', async () => {
    show(`/sims/compare?a=${ref(RUN, 'position_20pct')}&b=${ref(RUN, 'base')}`);
    await waitFor(() => expect(screen.getByTestId('alignment-matrix')).toBeTruthy());
    await act(async () => {
      fireEvent.click(screen.getByTestId('compare-swap'));
    });
    await waitFor(() =>
      expect((screen.getByTestId('picker-A-arm') as HTMLSelectElement).value).toBe('base'),
    );
    expect(navLog[navLog.length - 1].type).toBe('PUSH');
  });
});

describe('malformed refs', () => {
  it('sends a malformed `a` back to /sims rather than guessing', async () => {
    show('/sims/compare?a=13cc:base:GOOGL');
    await waitFor(() => expect(seen.pathname).toBe('/sims'));
  });

  it('drops a malformed `b` and keeps rendering `a`', async () => {
    show(`/sims/compare?a=${ref(RUN)}&b=not-a-ref`);
    await screen.findByTestId('compare-awaiting-b');
    expect(seen.pathname).toBe('/sims/compare');
    expect(seen.search).not.toContain('b=');
  });
});

describe('cross-run comparison from two different runs', () => {
  it('reads each side from its OWN run and notes the engine move', async () => {
    show(`/sims/compare?a=${ref(RUN)}&b=${ref(RUN_B)}`);
    await waitFor(() =>
      expect(screen.getByTestId('alignment-engine_identity').getAttribute('data-outcome')).toBe(
        'noted',
      ),
    );
    expect(screen.getByTestId('alignment-symbol').getAttribute('data-outcome')).toBe('aligned');
    expect(screen.getByTestId('ab-refused').textContent).toContain('different runs');
  });

  it('renders exactly ONE bias footer, last, and says whose it is', async () => {
    show(`/sims/compare?a=${ref(RUN)}&b=${ref(RUN_B)}`);
    await waitFor(() => expect(screen.getByTestId('bias-footer')).toBeTruthy());
    expect(screen.getAllByTestId('bias-footer')).toHaveLength(1);
    const view = screen.getByTestId('compare-view');
    const footer = screen.getByTestId('bias-footer');
    expect(view.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByTestId('bias-footer-source').textContent).toContain('records the same set');
    // Nothing may follow the caveats.
    const source = screen.getByTestId('bias-footer-source');
    expect(footer.compareDocumentPosition(source) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe('the entry points', () => {
  it('a grid cell links to the compare view with itself as A and base as B', async () => {
    show(`/sims/${RUN}`);
    const link = await screen.findByTestId('compare-cell-position_20pct-GOOGL-holdout');
    expect(link.getAttribute('href')).toBe(
      `/sims/compare?a=${encodeURIComponent(ref(RUN, 'position_20pct', 'GOOGL', 'holdout'))}` +
        `&b=${encodeURIComponent(ref(RUN, 'base', 'GOOGL', 'holdout'))}`,
    );
  });

  it('a BASE grid cell links with A alone — base against itself is not a question', async () => {
    show(`/sims/${RUN}`);
    const link = await screen.findByTestId('compare-cell-base-GOOGL-holdout');
    expect(link.getAttribute('href')).toContain('a=');
    expect(link.getAttribute('href')).not.toContain('b=');
  });

  it('the console’s own link pre-fills base as B', async () => {
    show(`/sims/${RUN}/position_20pct/GOOGL/holdout`);
    const link = await screen.findByTestId('compare-link');
    expect(link.getAttribute('href')).toContain(
      `b=${encodeURIComponent(ref(RUN, 'base', 'GOOGL', 'holdout'))}`,
    );
  });
});

// --------------------------------------------------------------------------- //
// Review round 1
// --------------------------------------------------------------------------- //

describe('R7 — one detail fetch per DISTINCT run', () => {
  const detailCalls = (runId: string) =>
    fetchMock.mock.calls.filter((c) => c[0] === `/api/v2/sweeps/${runId}`).length;

  it('a same-run pair polls that run ONCE, not twice', async () => {
    show(`/sims/compare?a=${ref(RUN, 'position_20pct')}&b=${ref(RUN, 'base')}`);
    await waitFor(() => expect(screen.getByTestId('alignment-matrix')).toBeTruthy());
    expect(detailCalls(RUN)).toBe(1);
  });

  it('a cross-run pair fetches each run exactly once', async () => {
    show(`/sims/compare?a=${ref(RUN)}&b=${ref(RUN_B)}`);
    await waitFor(() => expect(screen.getByTestId('alignment-matrix')).toBeTruthy());
    expect(detailCalls(RUN)).toBe(1);
    expect(detailCalls(RUN_B)).toBe(1);
  });
});

describe('R1 — a deduplicated slot follows its pointer', () => {
  const DEDUP = 'deadbeefdeadbeef';

  beforeEach(() => {
    const dedupPayload = {
      ...shaped13cc,
      run_id: DEDUP,
      status: 'deduplicated',
      run: {
        ...shaped13cc.run,
        run_id: DEDUP,
        status: 'deduplicated',
        deduplicated_to: RUN,
      },
    };
    fetchMock.mockImplementation((url: string) => {
      if (url.startsWith('/api/v2/sweeps/allowlist')) {
        return Promise.resolve(ok({ allowed: [], rejected: [], presets: [], caps: {} }));
      }
      if (/\/artifacts\/|\/bars\//.test(url)) {
        return Promise.resolve(notFound('No detail artifact for this cell in this run.'));
      }
      if (url === `/api/v2/sweeps/${DEDUP}`) return Promise.resolve(ok(dedupPayload));
      if (url === `/api/v2/sweeps/${RUN}`) return Promise.resolve(ok(shaped13cc));
      if (url === `/api/v2/sweeps/${RUN_B}`) return Promise.resolve(ok(shapedA48d));
      if (url.startsWith('/api/v2/sweeps/')) return Promise.resolve(notFound('no such run'));
      if (url === '/api/v2/sweeps') return Promise.resolve(ok([listRow(RUN), listRow(RUN_B)]));
      return Promise.resolve(ok({ stock_symbols: ['GOOGL'] }));
    });
  });

  it('REPLACES the slot with the run that holds the evidence, and says so', async () => {
    show(`/sims/compare?a=${ref(RUN)}&b=${ref(DEDUP)}`);
    // `b` now points at the target, not at the deduplicated run.
    await waitFor(() => expect(seen.search).not.toContain(DEDUP));
    expect(seen.search).toContain(encodeURIComponent(ref(RUN, 'base')));
    expect(navLog.filter((n) => n.type === 'PUSH')).toHaveLength(0);
    expect(navLog[navLog.length - 1].type).toBe('REPLACE');
    await waitFor(() =>
      expect(screen.getByTestId('picker-B-dedup').textContent).toContain(DEDUP),
    );
  });

  it('does not follow a row that points at ITSELF — that is corrupt, not resolvable', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.startsWith('/api/v2/sweeps/allowlist')) {
        return Promise.resolve(ok({ allowed: [], rejected: [], presets: [], caps: {} }));
      }
      if (/\/artifacts\/|\/bars\//.test(url)) return Promise.resolve(notFound('absent'));
      if (url === `/api/v2/sweeps/${RUN}`) return Promise.resolve(ok(shaped13cc));
      if (url === `/api/v2/sweeps/${DEDUP}`) {
        return Promise.resolve(
          ok({
            ...shaped13cc,
            run_id: DEDUP,
            status: 'deduplicated',
            run: { ...shaped13cc.run, run_id: DEDUP, status: 'deduplicated', deduplicated_to: DEDUP },
          }),
        );
      }
      if (url === '/api/v2/sweeps') return Promise.resolve(ok([listRow(RUN)]));
      return Promise.resolve(ok({ stock_symbols: ['GOOGL'] }));
    });
    show(`/sims/compare?a=${ref(RUN)}&b=${ref(DEDUP)}`);
    await waitFor(() =>
      expect(screen.getByTestId('strip-B-absent').getAttribute('data-run-status')).toBe(
        'deduplicated',
      ),
    );
    expect(seen.search).toContain(DEDUP);
    expect(screen.getByTestId('strip-B-absent').textContent).toContain('nothing to open');
  });
});

describe('R1 — a non-`done` run is labelled, never "loading"', () => {
  it('labels the run picker options by status', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.startsWith('/api/v2/sweeps/allowlist')) {
        return Promise.resolve(ok({ allowed: [], rejected: [], presets: [], caps: {} }));
      }
      if (/\/artifacts\/|\/bars\//.test(url)) return Promise.resolve(notFound('absent'));
      if (url === `/api/v2/sweeps/${RUN}`) return Promise.resolve(ok(shaped13cc));
      if (url === '/api/v2/sweeps') {
        return Promise.resolve(
          ok([listRow(RUN), { ...listRow(RUN_B), status: 'running' }]),
        );
      }
      return Promise.resolve(ok({ stock_symbols: ['GOOGL'] }));
    });
    show(`/sims/compare?a=${ref(RUN)}`);
    const select = (await screen.findByTestId('picker-A-run')) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(2));
    const labels = [...select.options].map((o) => o.textContent);
    expect(labels).toContain(`${RUN_B} — running`);
    expect(labels).toContain(RUN);
  });

  it('renders a failed run’s status rather than a loading sentence', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.startsWith('/api/v2/sweeps/allowlist')) {
        return Promise.resolve(ok({ allowed: [], rejected: [], presets: [], caps: {} }));
      }
      if (/\/artifacts\/|\/bars\//.test(url)) return Promise.resolve(notFound('absent'));
      if (url === `/api/v2/sweeps/${RUN}`) return Promise.resolve(ok(shaped13cc));
      if (url === `/api/v2/sweeps/${RUN_B}`) {
        return Promise.resolve(
          ok({
            ...shapedA48d,
            status: 'failed',
            run: { ...shapedA48d.run, status: 'failed', error: 'chain fetch blew up' },
          }),
        );
      }
      if (url === '/api/v2/sweeps') return Promise.resolve(ok([listRow(RUN), listRow(RUN_B)]));
      return Promise.resolve(ok({ stock_symbols: ['GOOGL'] }));
    });
    show(`/sims/compare?a=${ref(RUN)}&b=${ref(RUN_B)}`);
    await waitFor(() =>
      expect(screen.getByTestId('strip-B-absent').getAttribute('data-run-status')).toBe('failed'),
    );
    expect(screen.getByTestId('strip-B-absent').textContent).toContain('chain fetch blew up');
    expect(screen.getByTestId('strip-B-absent').textContent).not.toContain('Loading this run');
    expect(screen.getByTestId('alignment-window').getAttribute('data-outcome')).toBe('unknown');
  });
});

describe('R6 — a PENDING sidecar is not an absent one', () => {
  it('says the price history is loading, never "stored no price series"', async () => {
    let releaseBars: (() => void) | null = null;
    const barsGate = new Promise<void>((resolve) => {
      releaseBars = resolve;
    });
    fetchMock.mockImplementation((url: string) => {
      if (url.startsWith('/api/v2/sweeps/allowlist')) {
        return Promise.resolve(ok({ allowed: [], rejected: [], presets: [], caps: {} }));
      }
      if (/\/bars\//.test(url)) {
        return barsGate.then(() => notFound('This run stored no price series.'));
      }
      if (/\/artifacts\//.test(url)) return Promise.resolve(notFound('absent'));
      if (url === `/api/v2/sweeps/${RUN}`) return Promise.resolve(ok(shaped13cc));
      if (url === '/api/v2/sweeps') return Promise.resolve(ok([listRow(RUN)]));
      return Promise.resolve(ok({ stock_symbols: ['GOOGL'] }));
    });
    show(`/sims/compare?a=${ref(RUN, 'position_20pct')}&b=${ref(RUN, 'base')}`);
    await waitFor(() => expect(screen.getAllByTestId('price-chart-absent').length).toBe(2));
    // Still in flight: the panels say LOADING, never "stored no price series".
    for (const panel of screen.getAllByTestId('price-chart-absent')) {
      expect(panel.textContent).toContain('Loading price history');
      expect(panel.textContent).not.toContain('stored no price series');
    }
    await act(async () => {
      releaseBars?.();
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(screen.getAllByTestId('price-chart-absent')[0].textContent).toContain(
        'stored no price series',
      ),
    );
  });
});

describe('R8 / LOW — swap is a no-op on a pair with itself', () => {
  it('offers no swap when A and B are the same cell', async () => {
    show(`/sims/compare?a=${ref(RUN)}&b=${ref(RUN)}`);
    await waitFor(() => expect(screen.getByTestId('alignment-matrix')).toBeTruthy());
    const before = navLog.length;
    await act(async () => {
      fireEvent.click(screen.getByTestId('compare-swap'));
    });
    expect(navLog.length).toBe(before);
    expect(screen.getByTestId('ab-refused').textContent).toContain('SAME cell');
  });
});
