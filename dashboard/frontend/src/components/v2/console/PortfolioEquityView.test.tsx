// FC-096 Phase E PR-3: the portfolio view's refusals.
//
// The index arithmetic is pinned in `series.test.ts`. What is tested here is
// what the PANEL must never do: print an aggregate number, quietly include a
// symbol that was not measured, or fetch the base overlay nobody asked for.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import type { SweepReport } from '../../../types/v2';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { resetArtifactCacheForTests } from '../../../hooks/artifactCache';
import { resetSessionExpiredSignal } from '../../../hooks/iapSession';
import PortfolioEquityView from './PortfolioEquityView';

const detail = normaliseSweepDetail(shaped13cc)!;
const sweep = detail.sweep;
const report = detail.results!;

/** GOOGL's rows cloned onto two more measured symbols, plus one `insuf`. */
const multi: SweepReport = {
  ...report,
  symbols: ['GOOGL', 'AAA', 'BBB', 'DUD'],
  rows: report.rows.flatMap((row) =>
    row.symbol !== 'GOOGL'
      ? [row]
      : [
          row,
          { ...row, symbol: 'AAA' },
          { ...row, symbol: 'BBB' },
          { ...row, symbol: 'DUD', measured: false, insufficient: true },
        ],
  ),
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
  fetchMock = vi.fn().mockImplementation(() =>
    Promise.resolve({
      ok: true,
      status: 200,
      statusText: '',
      text: async () => JSON.stringify(artifact13cc),
    } as unknown as Response),
  );
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetArtifactCacheForTests();
});

const show = (scenario = 'base', reportOver: SweepReport = multi) =>
  render(
    <PortfolioEquityView
      sweep={sweep}
      report={reportOver}
      scenario={scenario}
      split="fit"
      specStrategy={null}
    />,
  );

describe('PortfolioEquityView', () => {
  it('captions itself with “k of N” and what it is NOT', async () => {
    show();
    await waitFor(() =>
      expect(screen.getByTestId('portfolio-loaded').textContent).toBe('3 of 3 loaded'),
    );
    expect(screen.getByTestId('portfolio-caption').textContent).toMatch(
      /independent single-symbol replays, each on its own capital — not a portfolio simulation/,
    );
  });

  it('prints NO aggregate number anywhere', async () => {
    // The mutation this kills: a "portfolio return" or an index level in text.
    // One number is all it takes for N independent replays to be read as a
    // diversified book.
    show();
    await waitFor(() => expect(screen.getByTestId('portfolio-members')).toBeInTheDocument());
    const text = screen.getByTestId('portfolio-equity-view').textContent!;
    expect(text).not.toMatch(/\$/);
    expect(text).not.toMatch(/%/);
  });

  it('EXCLUDES an unmeasured symbol and names its state', async () => {
    show();
    await waitFor(() => expect(screen.getByTestId('portfolio-excluded')).toBeInTheDocument());
    const excluded = screen.getByTestId('portfolio-excluded').textContent!;
    expect(excluded).toMatch(/DUD/);
    expect(excluded).toMatch(/No completed cycle in this window/);
    expect(screen.getByTestId('portfolio-members').textContent).toBe(
      'In the index: AAA, BBB, GOOGL.',
    );
  });

  /** Every arm cell measured; base measures all but DUD. */
  const armMeasured: SweepReport = {
    ...multi,
    rows: multi.rows.map((r) =>
      r.scenario === 'position_20pct' ? { ...r, measured: true, insufficient: false } : r,
    ),
  };

  it('fetches N artifacts, and only the base cells base MEASURED (R2)', async () => {
    show('position_20pct', armMeasured);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(
      fetchMock.mock.calls.every((call) => String(call[0]).includes('/position_20pct/')),
    ).toBe(true);

    fireEvent.click(screen.getByTestId('portfolio-base-toggle'));
    // THREE, not four: base's DUD cell is `insufficient`, so it can never enter
    // the overlay and requesting it is a round trip for nothing.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(7));
    const baseCalls = fetchMock.mock.calls.filter((call) => String(call[0]).includes('/base/'));
    expect(baseCalls).toHaveLength(3);
    expect(baseCalls.some((call) => String(call[0]).includes('/DUD/'))).toBe(false);
  });

  it('OMITS the base overlay when base did not measure a member, and names it', async () => {
    // The mutation this kills: build the base index over the ARM's measured set.
    // Base's DUD cell is a flat line at its starting cash, so averaging it in
    // drags the base index toward 100 and the arm reads as beating a benchmark
    // that was never computed for that symbol.
    show('position_20pct', armMeasured);
    fireEvent.click(screen.getByTestId('portfolio-base-toggle'));
    await waitFor(() => expect(screen.getByTestId('portfolio-base-omitted')).toBeInTheDocument());
    const omitted = screen.getByTestId('portfolio-base-omitted').textContent!;
    expect(omitted).toMatch(/base did not measure DUD/);
    expect(omitted).toMatch(/DUD: insuf/);
    expect(omitted).toMatch(/flat starting-cash line/);
  });

  it('DRAWS the overlay when base measures every member', async () => {
    const bothMeasured: SweepReport = {
      ...armMeasured,
      symbols: ['GOOGL', 'AAA', 'BBB'],
    };
    show('position_20pct', bothMeasured);
    fireEvent.click(screen.getByTestId('portfolio-base-toggle'));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(6));
    await waitFor(() => expect(screen.queryByTestId('portfolio-base-omitted')).toBeNull());
  });

  it('says there is no index when nothing in the arm was measured', async () => {
    show('position_20pct');
    await waitFor(() => expect(screen.getByTestId('portfolio-empty')).toBeInTheDocument());
    expect(screen.getByTestId('portfolio-empty').textContent).toMatch(
      /No symbol in this arm and window is measured/,
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
