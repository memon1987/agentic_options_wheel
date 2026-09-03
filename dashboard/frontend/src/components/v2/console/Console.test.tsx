// FC-096 Phase E PR-2 (review round 1, F3): the console SHELL.
//
// The plan lists these tests under PR-2 and the first cut shipped without them,
// which left four claims about the shell resting on nothing:
//
//   * the in-sample banner appears IFF `in_sample_only` — on the FLAG, never on
//     the presence of the banner string, because a payload that sets the flag
//     and sends no text must not silently lose the warning;
//   * `artifacts_complete === false` says the evidence set is incomplete, and
//     `null` says nothing at all (three-state, `SweepResults` learned the same
//     lesson);
//   * NO digest tile is ever coloured by sign — they are client-derived and a
//     green one is indistinguishable from an engine verdict;
//   * the `low-act` and `err` halves of the cell-state partition behave like
//     `insuf`, which is the only one the strip's own file covers.
//
// Everything is driven through `Console` against a stubbed fetch, because three
// of the four are about the seam between the report, the hooks and the strip.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import bars13cc from '../../../test/fixtures/bars_13cc_googl_fit.json';
import type { SweepReport, SweepResultRow } from '../../../types/v2';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { IN_SAMPLE_FALLBACK } from '../sims/SweepResults';
import { resetArtifactCacheForTests } from '../../../hooks/artifactCache';
import { resetSessionExpiredSignal } from '../../../hooks/iapSession';
import Console from './Console';

const detail = normaliseSweepDetail(shaped13cc)!;
const sweep = detail.sweep;
const report = detail.results!;

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

let fetchMock: ReturnType<typeof vi.fn>;

/** Serve the real captured objects for every cell. */
const serveObjects = () =>
  fetchMock.mockImplementation((url: string) =>
    Promise.resolve(ok(url.includes('/bars/') ? bars13cc : artifact13cc)),
  );

/** Answer both object routes with the endpoint's own 404 prose. */
const serveNothing = () =>
  fetchMock.mockResolvedValue(notFound('No detail artifact for this cell in this run.'));

const show = (over: Partial<SweepReport> = {}, scenario = 'base', split = 'fit') =>
  render(
    <Console
      sweep={sweep}
      report={{ ...report, ...over }}
      scenario={scenario}
      symbol="GOOGL"
      split={split}
    />,
  );

/**
 * Replace one grid cell's state, so the partition can be exercised without a
 * second captured run. Everything else about the row is left exactly as served.
 */
const withCellState = (over: Partial<SweepResultRow>, scenario = 'base', split = 'fit') => ({
  rows: report.rows.map((r) =>
    r.scenario === scenario && r.symbol === 'GOOGL' && r.split === split ? { ...r, ...over } : r,
  ),
});

beforeEach(() => {
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  serveNothing();
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
});

describe('the in-sample banner keys on the FLAG', () => {
  it('is absent on a run with a holdout', () => {
    show();
    expect(report.in_sample_only).toBe(false);
    expect(screen.queryByTestId('console-in-sample-banner')).toBeNull();
  });

  it('prints the payload’s own words when the flag is set', () => {
    show({ in_sample_only: true, in_sample_banner: 'THE SERVER’S OWN WARNING.' });
    expect(screen.getByTestId('console-in-sample-banner').textContent).toBe(
      'THE SERVER’S OWN WARNING.',
    );
  });

  it('falls back to IN_SAMPLE_FALLBACK when the flag is set and the string is not', () => {
    // The mutation: `in_sample_only && in_sample_banner`, which is what shipped.
    // A missing string then silently suppresses the warning it is the text OF —
    // and the reader sees an unqualified ranking of an unvalidated run.
    show({ in_sample_only: true, in_sample_banner: null });
    const banner = screen.getByTestId('console-in-sample-banner');
    expect(banner.textContent).toBe(IN_SAMPLE_FALLBACK);
    expect(banner.textContent).toMatch(/has not been validated/);
  });

  it('is absent when the flag is false even if a banner string arrives', () => {
    show({ in_sample_only: false, in_sample_banner: 'stale text from a previous shape' });
    expect(screen.queryByTestId('console-in-sample-banner')).toBeNull();
  });
});

describe('artifacts_complete is three-state', () => {
  it('warns when a write is known to have failed', () => {
    show({ artifacts_complete: false });
    expect(screen.getByTestId('artifacts-incomplete').textContent).toMatch(
      /storage failure, not a quiet replay/,
    );
  });

  it('says nothing when it is true or unrecorded', () => {
    show({ artifacts_complete: true });
    expect(screen.queryByTestId('artifacts-incomplete')).toBeNull();
    show({ artifacts_complete: null });
    expect(screen.queryAllByTestId('artifacts-incomplete')).toHaveLength(0);
  });
});

describe('no digest tile is ever coloured by sign', () => {
  const SIGN_CLASSES = /text-green-400|text-red-400/;

  it('the three digest tiles are grey, whatever their values', async () => {
    serveObjects();
    show();
    await waitFor(() => expect(screen.queryByTestId('tile-deployment')).not.toBeNull());
    const tiles = screen.getByTestId('digest-tiles');
    // The mutation: `tone={toneOf(digest.grossPremium)}` on any one of them.
    // A green gross-premium tile is indistinguishable from a green ENGINE
    // number, and the whole point of §D-4's fence is that a client-derived
    // figure can never be read as a verdict.
    expect(tiles.innerHTML).not.toMatch(SIGN_CLASSES);
    for (const id of ['tile-gross-premium', 'tile-fees', 'tile-deployment']) {
      expect(screen.getByTestId(id).innerHTML).not.toMatch(SIGN_CLASSES);
    }
    // …and they are visibly there, so the assertion is not vacuous.
    expect(screen.getByTestId('tile-fees').textContent).toMatch(/\$0\.040\/contract/);
  });

  it('the ENGINE tiles still are coloured — so the check above means something', async () => {
    serveObjects();
    show();
    await waitFor(() => expect(screen.queryByTestId('tile-deployment')).not.toBeNull());
    expect(screen.getByTestId('tile-annualized').innerHTML).toMatch(/text-green-400/);
    expect(screen.getByTestId('tile-drawdown').innerHTML).toMatch(/text-red-400/);
  });

  it('renders the endpoint’s own words where the tiles would be, on a 404', async () => {
    show();
    await waitFor(() => expect(screen.queryByTestId('digest-absent')).not.toBeNull());
    expect(screen.getByTestId('digest-absent').textContent).toBe(
      'No detail artifact for this cell in this run.',
    );
    expect(screen.queryByTestId('tile-deployment')).toBeNull();
  });
});

describe('the cell-state partition, beyond `insuf`', () => {
  const VERDICT_TILES = ['tile-annualized', 'tile-roc', 'tile-delta', 'tile-benchmark'];

  it('a `low-act` cell renders no return, RoC, Δ or benchmark tile', () => {
    show(withCellState({ measured: false, low_activity: true, days_in_position_fraction: 0.04 }));
    expect(screen.getByTestId('cell-state-badge').textContent).toMatch(/^low-act/);
    for (const id of VERDICT_TILES) expect(screen.queryByTestId(id)).toBeNull();
    expect(screen.getByTestId('unmeasured-notice').textContent).toMatch(/too few decision days/);
    // The activity counts survive — they are facts about the window, and they
    // are what make the state legible rather than merely absent.
    expect(screen.getByTestId('tile-cycles')).toBeTruthy();
    expect(screen.getByTestId('tile-time-in-position').textContent).toMatch(/4%/);
  });

  it('an `err` cell shows the engine’s error text and no numbers', () => {
    show(withCellState({ measured: false, error: 'UnadjustedCorporateAction on GOOGL' }));
    expect(screen.getByTestId('cell-state-badge').textContent).toBe('err');
    for (const id of VERDICT_TILES) expect(screen.queryByTestId(id)).toBeNull();
    expect(screen.getByTestId('unmeasured-notice').textContent).toMatch(
      /UnadjustedCorporateAction on GOOGL/,
    );
  });

  it('neither state colours the dollar tiles it DOES render', () => {
    show(withCellState({ measured: false, low_activity: true, days_in_position_fraction: 0.04 }));
    // `option_pnl` is +$4,453 on this row. Green, on a cell whose whole point is
    // that it measured nothing, reads as a verdict (review round 1, F9).
    expect(screen.getByTestId('tile-option-pnl').innerHTML).not.toMatch(/text-green-400/);
    expect(screen.getByTestId('tile-drawdown').innerHTML).not.toMatch(/text-red-400/);
  });
});

describe('the shell’s composition', () => {
  it('renders the strip, the six PR-3 placeholders and the footer, in order', async () => {
    serveObjects();
    show();
    await screen.findByTestId('verdict-strip');
    const ids = Array.from(
      screen.getByTestId('sim-console').querySelectorAll('[data-testid]'),
    ).map((el) => el.getAttribute('data-testid'));
    const ordered = [
      'verdict-strip',
      'placeholder-price',
      'placeholder-equity',
      'placeholder-premium',
      'placeholder-forecast',
      'placeholder-ledger',
      'placeholder-rejections',
      'provenance-footer',
    ];
    expect(ids.filter((id) => ordered.includes(id!))).toEqual(ordered);
  });
});
