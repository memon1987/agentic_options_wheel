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
/** A second, structurally identical run — what a run SWITCH switches to. */
const RUN_B = 'b0b0b0b0b0b0b0b0';
/** The run a `deduplicated` row points at. */
const RUN_ORIG = 'a48d7bb064194e0f';

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

const shapedFor = (runId: string, over: Record<string, unknown> = {}) => ({
  ...shaped13cc,
  run_id: runId,
  run: { ...shaped13cc.run, run_id: runId, ...((over.run as object) ?? {}) },
  ...over,
});

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
/** Per-run detail overrides, keyed by run id. Empty ⇒ every run is the 13cc shape. */
let detailByRun: Record<string, unknown> = {};
/** What `GET /api/v2/sweeps` answers. The FIRST row is the newest run. */
let listRows: unknown[] = [];
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
  detailByRun = {};
  listRows = [listRow];
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
    if (target.startsWith('/api/v2/sweeps/')) {
      const id = target.slice('/api/v2/sweeps/'.length);
      const custom = detailByRun[id];
      if (custom) return Promise.resolve(ok(custom));
      return Promise.resolve(ok(shapedFor(id)));
    }
    if (target === '/api/v2/sweeps') return Promise.resolve(ok(listRows));
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

  it('a run switch never replaces a cell out of the user’s history (F1)', async () => {
    // The bug: `usePolledGet` clears the previous run's data inside an EFFECT,
    // so the first committed render after the switch carried run B's id and run
    // A's report. The default/repair effect read that stale report, decided run
    // B "does not have" the URL's cell and REPLACED it — overwriting the entry
    // the user had just pushed, so Back landed on the wrong cell and their own
    // history entry was gone.
    //
    // Run B here has a DIFFERENT arm set, which is what makes the stale read
    // decide to repair: under run A's report `arm_b` does not exist.
    detailByRun[RUN_B] = shapedFor(RUN_B, {
      scenarios: ['base', 'arm_b'],
      grid: {
        fit: { base: shaped13cc.grid.fit.base, arm_b: shaped13cc.grid.fit.position_20pct },
        holdout: {
          base: shaped13cc.grid.holdout.base,
          arm_b: shaped13cc.grid.holdout.position_20pct,
        },
      },
    });

    listRows = [listRow, { ...listRow, run_id: RUN_B }];

    show(`/sims/${RUN}/base/GOOGL/fit`);
    await screen.findByTestId('cell-selector');
    const mine = `/sims/${RUN}/base/GOOGL/holdout`;

    fireEvent.click(screen.getByRole('button', { name: 'holdout' }));
    await waitFor(() => expect(seen.pathname).toBe(mine));

    // The user selects run B — one PUSH, and then whatever the page decides.
    const before = navLog.length;
    fireEvent.click(await screen.findByTestId(`run-row-${RUN_B}`));
    await waitFor(() => expect(seen.pathname).toBe(`/sims/${RUN_B}/arm_b/GOOGL/holdout`));

    // Exactly one PUSH (the click) and one REPLACE (run B's OWN default cell,
    // chosen from run B's OWN report). No replace fired off the stale report.
    const after = navLog.slice(before).map((n) => n.type);
    expect(after).toEqual(['PUSH', 'REPLACE']);

    // And Back lands on the cell the user was actually looking at.
    await back();
    expect(seen.pathname).toBe(mine);
    expect(navLog[navLog.length - 1].type).toBe('POP');
  });

  it('a deduplicated run auto-opens the run that answered it, by REPLACING (F5)', async () => {
    detailByRun[RUN] = shapedFor(RUN, {
      status: 'deduplicated',
      run: { status: 'deduplicated', deduplicated_to: RUN_ORIG },
    });
    show(`/sims/${RUN}/base/GOOGL/fit`);
    await waitFor(() => expect(seen.pathname).toBe(`/sims/${RUN_ORIG}/base/GOOGL/fit`));
    // REPLACE, not PUSH: a run that stored nothing is not a screen worth keeping
    // in the operator's history, and Back must leave rather than bounce back
    // onto the redirect.
    expect(navLog.map((n) => n.type)).toEqual(['POP', 'REPLACE']);
    // The console mounts on the destination and says where the question came
    // from — the notice, `followedDedup` and the footer row were all dead while
    // this path returned the "Open X" shell instead.
    const notice = await screen.findByTestId('followed-dedup');
    expect(notice.textContent).toMatch(new RegExp(RUN));
    expect(notice.textContent).toMatch(new RegExp(RUN_ORIG));
    expect(screen.getByTestId('provenance-footer').textContent).toMatch(/reached from/);
  });

  it('entering a dedup run with NO cell keeps the origin across the second replace', async () => {
    // The confirmation pass's FIXES-INCOMPLETE. Arriving from the runs list is
    // TWO replaces on one history entry: the auto-follow writes `{dedupFrom}`
    // onto it, and the default-cell effect immediately replaced it away — so
    // the destination lost the only record of where it was reached from, and
    // neither the notice nor the footer's "reached from" row rendered. The
    // deep-link case above never caught it because it arrives WITH a cell, so
    // the second replace never fires.
    detailByRun[RUN] = shapedFor(RUN, {
      status: 'deduplicated',
      run: { status: 'deduplicated', deduplicated_to: RUN_ORIG },
    });
    show(`/sims/${RUN}`);
    await waitFor(() =>
      expect(seen.pathname).toBe(`/sims/${RUN_ORIG}/position_20pct/GOOGL/holdout`),
    );
    // Both automatic steps replace, so Back still leaves rather than walking
    // back through the redirect.
    expect(navLog.map((n) => n.type)).toEqual(['POP', 'REPLACE', 'REPLACE']);

    const notice = await screen.findByTestId('followed-dedup');
    expect(notice.textContent).toMatch(new RegExp(RUN));
    expect(notice.textContent).toMatch(new RegExp(RUN_ORIG));
    expect(screen.getByTestId('provenance-footer').textContent).toMatch(
      new RegExp(`reached from.*${RUN}`),
    );
  });

  it('a dedup row pointing at ITSELF is not followed, and says so', async () => {
    // Corrupt data, not a missing pointer: following it would navigate to the
    // screen already on display. "The original run is not recorded" would send
    // an operator looking for a column that is populated.
    detailByRun[RUN] = shapedFor(RUN, {
      status: 'deduplicated',
      run: { status: 'deduplicated', deduplicated_to: RUN },
    });
    show(`/sims/${RUN}/base/GOOGL/fit`);
    const region = await screen.findByTestId('results-status');
    expect(region.textContent).toMatch(/points at ITSELF/);
    expect(region.textContent).not.toMatch(/not recorded on this row/);
    expect(seen.pathname).toBe(`/sims/${RUN}/base/GOOGL/fit`);
    // NOT followed at all. Following it lands on the path already on display,
    // which leaves the pathname identical and is therefore invisible except
    // here: the navigation itself must never happen, or every render of a
    // corrupt row spends a history write to arrive where it already is.
    expect(navLog.map((n) => n.type)).toEqual(['POP']);
  });

  it('the bias footer is the LAST thing on the page, below the console (F2)', async () => {
    show(`/sims/${RUN}/base/GOOGL/fit`);
    await screen.findByTestId('sim-console');
    const grid = screen.getByTestId('sweep-results');
    const console_ = screen.getByTestId('sim-console');
    const footer = screen.getByTestId('bias-footer');
    // `compareDocumentPosition` & DOCUMENT_POSITION_FOLLOWING === "b comes after
    // a in document order". Asserted on the real page rather than on
    // `SweepResults` alone, because the bug was one of COMPOSITION: the footer
    // was that component's last child and the console was rendered below it.
    const follows = (a: Element, b: Element) =>
      !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(follows(grid, console_)).toBe(true);
    expect(follows(console_, footer)).toBe(true);
    expect(follows(footer, grid)).toBe(false);
    // Exactly one of it — the extraction must not leave a second copy behind.
    expect(screen.getAllByTestId('bias-footer')).toHaveLength(1);
  });

  it('clicking the run already on screen does nothing at all (F9)', async () => {
    show(`/sims/${RUN}/base/GOOGL/fit`);
    await screen.findByTestId('cell-selector');
    const before = navLog.length;
    fireEvent.click(await screen.findByTestId(`run-row-${RUN}`));
    // It used to push `/sims/<same run>`, dropping the cell out of the URL and
    // letting the default-cell effect replace it — so clicking the highlighted
    // row silently threw away the cell being read, and left a history entry
    // that does nothing but undo itself.
    await waitFor(() => expect(seen.pathname).toBe(`/sims/${RUN}/base/GOOGL/fit`));
    expect(navLog.length).toBe(before);
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
