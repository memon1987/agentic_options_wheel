// FC-096 Phase E PR-3: the renderers' DOM contract.
//
// Deliberately narrow. `ResponsiveContainer` is 0x0 under jsdom's
// `ResizeObserver` stub, so no assertion here can be about a mark on a chart —
// the numbers are pinned in `chartRows.test.ts`, `series.test.ts` and
// `ledgerCsv.test.ts`. What IS testable in the DOM is exactly what the plan
// asks for: captions, absence states, legend text, and the things these panels
// must never print.

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import bars13cc from '../../../test/fixtures/bars_13cc_googl_fit.json';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import type { SimArtifact, SimBars } from '../../../types/v2';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { indexRows, lookupCell } from '../sims/resultCells';
import { normaliseArtifact, normaliseBars } from './normaliseArtifact';
import { computeDigest } from './artifactDigest';
import { equityOverlay } from './series';
import PriceChart from './PriceChart';
import EquityChart from './EquityChart';
import DrawdownChart from './DrawdownChart';
import PremiumCharts from './PremiumCharts';
import DeploymentChart from './DeploymentChart';
import LedgerTable from './LedgerTable';
import SimCycleTable from './SimCycleTable';
import RejectionPanel from './RejectionPanel';
import type { ExportContext } from './ledgerCsv';

const artifact = normaliseArtifact(artifact13cc) as SimArtifact;
const bars = normaliseBars(bars13cc) as SimBars;
const report = normaliseSweepDetail(shaped13cc)!.results!;
const row = lookupCell(indexRows(report.rows), 'base', 'GOOGL', 'fit')!;
const digest = computeDigest(artifact, bars, { specStrategy: null });
const WINDOW = { start: '2025-09-01', end: '2026-06-02' };

const context: ExportContext = {
  runId: '13cc2729d1c74211',
  scenario: 'base',
  symbol: 'GOOGL',
  split: 'fit',
  engineIdentity: '129711f15fe0488a',
  fillBasis: 'mid',
  fillHaircut: 0.25,
  inSampleOnly: false,
  inSampleBanner: null,
  strategy: 'wheel',
  knownBiases: report.known_biases,
  row,
};

describe('PriceChart', () => {
  it('names the replay’s own bars, the event count and the rolls', () => {
    render(
      <PriceChart
        symbol="GOOGL"
        bars={bars}
        barsAbsence={null}
        artifact={artifact}
        artifactAbsence={null}
        window={WINDOW}
        strategy="wheel"
      />,
    );
    const caption = screen.getByTestId('price-caption-replay').textContent;
    expect(caption).toMatch(/189 sessions/);
    expect(caption).toMatch(/72 ledger events, 6 rolls/);
    expect(screen.getByTestId('price-roll-note').textContent).toMatch(/roll 252\.50 → 255\.00/);
    expect(screen.queryByTestId('price-off-session')).toBeNull();
    expect(screen.queryByTestId('price-dropped')).toBeNull();
  });

  it('falls back to BQ history with a caption and NO markers', async () => {
    // The one thing this mode must never do is draw a marker: a marker is a
    // claim that a decision was made at that price, on a series the replay
    // never saw.
    const rows = bars.bars.slice(0, 20).map((b) => ({ ...b }));
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: '',
      text: async () => JSON.stringify(rows),
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);
    render(
      <PriceChart
        symbol="GOOGL"
        bars={null}
        barsAbsence="No bars sidecar was stored for this run."
        artifact={artifact}
        artifactAbsence={null}
        window={WINDOW}
        strategy="wheel"
      />,
    );
    await waitFor(() =>
      expect(screen.queryByTestId('price-caption-fallback')).not.toBeNull(),
    );
    expect(screen.getByTestId('price-caption-fallback').textContent).toMatch(
      /Dashboard stock history, not the replay’s bars/,
    );
    expect(screen.getByTestId('price-caption-fallback').textContent).toMatch(/NO trade markers/);
    expect(screen.queryByTestId('price-roll-note')).toBeNull();
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/api\/v2\/symbol\/GOOGL\/stock-history\?days=/);
    vi.unstubAllGlobals();
  });

  it('shows the endpoint’s own words when nothing can be drawn', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        statusText: '',
        text: async () => '[]',
      } as unknown as Response),
    );
    render(
      <PriceChart
        symbol="CAND"
        bars={null}
        barsAbsence="No bars sidecar was stored for this run."
        artifact={null}
        artifactAbsence="No detail artifact for this cell in this run."
        window={WINDOW}
        strategy="wheel"
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId('price-chart-absent').textContent).toBe(
        'No bars sidecar was stored for this run.',
      ),
    );
    vi.unstubAllGlobals();
  });
});

describe('PriceChart — the fallback is gated on the sidecar settling (R5)', () => {
  it('issues NO stock-history request while the sidecar fetch is in flight', async () => {
    // The bug: `useBars` reports `data: null` while loading, so a fallback
    // gated on "no sidecar yet" fired a BigQuery query on every cell open —
    // including for every run that HAS a sidecar — and discarded the answer.
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const { rerender } = render(
      <PriceChart
        symbol="GOOGL"
        bars={null}
        barsAbsence={null}
        barsLoading
        artifact={artifact}
        artifactAbsence={null}
        window={WINDOW}
        strategy="wheel"
      />,
    );
    await waitFor(() => expect(screen.getByTestId('price-chart-absent')).toBeInTheDocument());
    expect(fetchMock).not.toHaveBeenCalled();

    // Settled WITH a sidecar: still nothing.
    rerender(
      <PriceChart
        symbol="GOOGL"
        bars={bars}
        barsAbsence={null}
        barsLoading={false}
        artifact={artifact}
        artifactAbsence={null}
        window={WINDOW}
        strategy="wheel"
      />,
    );
    await waitFor(() => expect(screen.getByTestId('price-caption-replay')).toBeInTheDocument());
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('issues none when the sidecar was never asked for at all', async () => {
    // Not loading, no data, no absence: `useStoredObject`'s idle state, which
    // is what a cell with no split or symbol yet looks like. Nothing has said
    // there is no sidecar, so there is nothing to fall back FROM.
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    render(
      <PriceChart
        symbol="GOOGL"
        bars={null}
        barsAbsence={null}
        barsLoading={false}
        artifact={artifact}
        artifactAbsence={null}
        window={WINDOW}
        strategy="wheel"
      />,
    );
    await waitFor(() => expect(screen.getByTestId('price-chart-absent')).toBeInTheDocument());
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('issues exactly ONE once the sidecar has settled absent', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: '',
      text: async () => '[]',
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);
    render(
      <PriceChart
        symbol="GOOGL"
        bars={null}
        barsAbsence="No bars sidecar was stored for this run."
        barsLoading={false}
        artifact={artifact}
        artifactAbsence={null}
        window={WINDOW}
        strategy="wheel"
      />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    vi.unstubAllGlobals();
  });
});

describe('EquityChart', () => {
  it('reads its header numbers off the ROW, not off the curve', () => {
    render(
      <EquityChart
        overlay={equityOverlay(artifact, null, bars)}
        row={row}
        isBase
        baseAbsence={null}
        artifactAbsence={null}
        capitalBase={100000}
      />,
    );
    const header = screen.getByTestId('equity-header-numbers').textContent;
    expect(header).toMatch(/Total return \+6\.8%/);
    expect(header).toMatch(/buy & hold \+71\.5%/);
    expect(header).toMatch(/excess -64\.7%/);
  });

  it('omits the buy-and-hold curve on a mismatch and says why', () => {
    const wrong = {
      ...bars,
      buy_and_hold: { ...bars.buy_and_hold!, final_value: 1 },
    } as SimBars;
    render(
      <EquityChart
        overlay={equityOverlay(artifact, null, wrong)}
        row={row}
        isBase
        baseAbsence={null}
        artifactAbsence={null}
        capitalBase={100000}
      />,
    );
    expect(screen.getByTestId('equity-benchmark-mismatch').textContent).toMatch(
      /Benchmark mismatch/,
    );
  });

  it('names an omitted base overlay and an absent benchmark', () => {
    render(
      <EquityChart
        overlay={equityOverlay(artifact, null, null)}
        row={row}
        isBase={false}
        baseAbsence="No detail artifact for this cell in this run."
        artifactAbsence={null}
        capitalBase={100000}
      />,
    );
    expect(screen.getByTestId('equity-no-base').textContent).toMatch(
      /No detail artifact for this cell in this run/,
    );
    expect(screen.getByTestId('equity-no-benchmark').textContent).toMatch(
      /the row’s benchmark return \+71\.5% still stands/,
    );
  });

  it('replaces the numbers with the state’s own words on an unmeasured cell', () => {
    const insufficient = { ...row, measured: false, insufficient: true };
    render(
      <EquityChart
        overlay={equityOverlay(artifact, null, bars)}
        row={insufficient}
        isBase
        baseAbsence={null}
        artifactAbsence={null}
        capitalBase={100000}
      />,
    );
    expect(screen.queryByTestId('equity-header-numbers')).toBeNull();
    expect(screen.getByTestId('equity-header-unmeasured').textContent).toMatch(
      /No completed cycle in this window/,
    );
  });
});

describe('DrawdownChart', () => {
  it('prints the ROW’s max drawdown, once, as the header', () => {
    render(<DrawdownChart series={digest.drawdownSeries} row={row} absence={null} />);
    const header = screen.getByTestId('drawdown-chart').textContent!;
    expect(screen.getByTestId('drawdown-header').textContent).toMatch(
      /Max drawdown -3\.4% — the engine’s number/,
    );
    // Exactly one drawdown percentage on the card: the series' own minimum is
    // never printed beside the row's, because a reader asked to reconcile two
    // numbers for one quantity will trust the worse one.
    expect(header.match(/-3\.4%/g)).toHaveLength(1);
    expect(header).toMatch(/189 decision days/);
  });

  it('shows the absence text rather than an empty chart', () => {
    render(
      <DrawdownChart series={[]} row={row} absence="No detail artifact for this cell in this run." />,
    );
    expect(screen.getByTestId('drawdown-absent').textContent).toBe(
      'No detail artifact for this cell in this run.',
    );
  });
});

describe('DeploymentChart', () => {
  it('is never coloured by sign and says which denominator it is not', () => {
    render(
      <DeploymentChart
        series={digest.deploymentSeries}
        reading={digest.deployment}
        capitalBase={digest.capitalBase}
        suppressionReason={digest.suppressionReason}
        absence={null}
      />,
    );
    const card = screen.getByTestId('deployment-chart');
    expect(card.innerHTML).not.toMatch(/text-green-400|text-red-400/);
    expect(screen.getByTestId('deployment-mean').textContent).toMatch(
      /All days, not just the deployed ones/,
    );
    expect(screen.getByTestId('deployment-mean').textContent).toMatch(
      /different question from the strip’s return on collateral/,
    );
  });

  it('SUPPRESSES itself without a stamped capital base', () => {
    // The mutation: divide by `starting_cash` anyway. A covered-call cell over
    // the wheel's cash prints a plausible, wrong percentage under a
    // correct-looking label.
    render(
      <DeploymentChart
        series={digest.deploymentSeries}
        reading={digest.deployment}
        capitalBase={null}
        suppressionReason="Capital base not stamped on this artifact…"
        absence={null}
      />,
    );
    expect(screen.getByTestId('deployment-suppressed').textContent).toMatch(
      /Capital base not stamped/,
    );
    expect(screen.queryByTestId('deployment-mean')).toBeNull();
  });
});

describe('LedgerTable', () => {
  it('renders every event, badges an unknown kind and filters', () => {
    const withUnknown = {
      ...artifact,
      ledger: [
        ...artifact.ledger,
        { ...artifact.ledger[0], kind: 'synthetic_lot_open', date: '2025-09-03' },
      ],
    } as SimArtifact;
    render(<LedgerTable artifact={withUnknown} absence={null} context={context} />);
    expect(screen.getByTestId('ledger-table').textContent).toMatch(/73 of 73 events/);
    // Twice: the filter chip and the row's own badge. Neither drops it.
    expect(screen.getAllByText('synthetic_lot_open').length).toBeGreaterThanOrEqual(2);

    fireEvent.change(screen.getByTestId('ledger-filter-text'), {
      target: { value: 'no-such-event' },
    });
    expect(screen.getByTestId('ledger-filtered-empty').textContent).toMatch(
      /No event matches this filter\. The cell has 73\./,
    );
  });

  it('says what the exports carry, and shows the endpoint’s words when absent', () => {
    render(<LedgerTable artifact={artifact} absence={null} context={context} />);
    expect(screen.getByTestId('ledger-table').textContent).toMatch(
      /engine identity and fill basis on every row/,
    );
    render(
      <LedgerTable
        artifact={null}
        absence="No detail artifact for this cell in this run."
        context={context}
      />,
    );
    expect(screen.getAllByTestId('ledger-absent')[0].textContent).toBe(
      'No detail artifact for this cell in this run.',
    );
  });
});

describe('SimCycleTable', () => {
  it('totals and averages NOTHING, and warns off the annualised column', () => {
    render(<SimCycleTable artifact={artifact} absence={null} />);
    const card = screen.getByTestId('cycle-table');
    expect(card.textContent).toMatch(/23 cycles \(22 completed, 1 open\)/);
    expect(card.textContent).toMatch(/No column is totalled or averaged here/);
    // No footer row, and no summary line: the mutation this kills is a
    // `<tfoot>` with a column sum or mean, which for `annualized_return` would
    // be a number with no denominator anyone could name.
    expect(card.querySelector('tfoot')).toBeNull();
    expect(card.textContent).not.toMatch(/Total:|Average|Mean/);
    expect(screen.getByTestId('cycle-annualised-header').getAttribute('title')).toMatch(
      /never average this column/,
    );
    // The one open cycle's badge (the caption above says "1 open" separately).
    expect(screen.getAllByText('open').length).toBeGreaterThanOrEqual(1);
  });
});

describe('SimCycleTable — the open cycle (R3)', () => {
  it('labels the realised columns and refuses the engine’s 0 sentinels', () => {
    render(<SimCycleTable artifact={artifact} absence={null} row={row} />);
    const card = screen.getByTestId('cycle-table');
    expect(card.textContent).toMatch(/Stock P&L \(realised\)/);
    expect(card.textContent).toMatch(/Total \(realised; open lot unmarked\)/);

    // The open cycle is the LAST row and carries days 0, RoC 0.83%, annualised
    // 0.0 — sentinels for a cycle that has not finished, which rendered as a
    // measured 0% over 0 days.
    const cells = Array.from(
      card.querySelectorAll('tbody tr:last-child td'),
    ).map((td) => td.textContent);
    expect(cells[1]).toBe('open');
    expect(cells[2]).toBe('—'); // days
    expect(cells[cells.length - 2]).toBe('—'); // return on capital
    expect(cells[cells.length - 1]).toBe('—'); // annualised
  });

  it('names the unrealised leg and why the Total column does not add up', () => {
    render(<SimCycleTable artifact={artifact} absence={null} row={row} />);
    const caveat = screen.getByTestId('cycle-open-caveat').textContent!;
    expect(caveat).toMatch(/1 open cycle/);
    expect(caveat).toMatch(/-\$1,565/); // the row's stock_pnl_unrealized
    expect(caveat).toMatch(/\$8,474/); // the column's sum
    expect(caveat).toMatch(/\$6,751/); // the cell's total P&L
  });

  it('says nothing about open cycles when every cycle closed', () => {
    const closed = {
      ...artifact,
      cycles: artifact.cycles.filter((c) => !c.is_open),
    } as SimArtifact;
    render(<SimCycleTable artifact={closed} absence={null} row={row} />);
    expect(screen.queryByTestId('cycle-open-caveat')).toBeNull();
  });
});

describe('RejectionPanel', () => {
  it('preserves the SERVED order and highlights the binding constraint', () => {
    render(
      <RejectionPanel
        artifact={artifact}
        absence={null}
        earningsSymbolsWithoutData={[]}
        caveat={report.rejection_tally_caveat}
      />,
    );
    const reasons = Array.from(screen.getAllByTestId('rejection-row')).map(
      (li) => li.textContent!.split('binding constraint')[0],
    );
    expect(reasons[0]).toMatch(/already holds this underlying \(scan, put\)/);
    expect(reasons[1]).toMatch(/selection: insufficient available shares/);
    expect(screen.getByTestId('rejection-binding')).toBeInTheDocument();
    expect(screen.getByTestId('rejection-days').textContent).toMatch(
      /25 candidate days of 189 decision days/,
    );
    expect(screen.getByTestId('rejection-caveat').textContent).toBe(
      report.rejection_tally_caveat,
    );
  });

  it('does NOT claim the reasons partition the decision days (R4)', () => {
    render(
      <RejectionPanel
        artifact={artifact}
        absence={null}
        earningsSymbolsWithoutData={[]}
        caveat={report.rejection_tally_caveat}
      />,
    );
    const card = screen.getByTestId('rejection-panel').textContent!;
    expect(card).not.toMatch(/the rest were rejected/);
    // 252 reason-days over 189 decision days: the tally OVERLAPS, so it cannot
    // be the complement of `candidate_days`.
    const sum = artifact.rejections.reduce((total, r) => total + r.days, 0);
    expect(sum).toBe(252);
    expect(sum).toBeGreaterThan(artifact.counters.decision_days as number);
    expect(screen.getByTestId('rejection-sum').textContent).toMatch(
      /sum to 252 reason-days over 189 decision days/,
    );
    expect(card).toMatch(/BEFORE selection and sizing had their say/);
    expect(card).toMatch(/one day can be counted under several/);
    expect(screen.getByTestId('rejection-in-position').textContent).toMatch(
      /the wheel being IN POSITION/,
    );
    expect(screen.getByTestId('rejection-binding-note').textContent).toMatch(
      /stamped on THIS cell’s artifact/,
    );
  });

  it('reverses when the served order reverses — it does not re-rank', () => {
    const reversed = { ...artifact, rejections: [...artifact.rejections].reverse() } as SimArtifact;
    render(
      <RejectionPanel
        artifact={reversed}
        absence={null}
        earningsSymbolsWithoutData={['PFE']}
        caveat={null}
      />,
    );
    const first = screen.getAllByTestId('rejection-row')[0].textContent!;
    expect(first).toBe(
      `${artifact.rejections[artifact.rejections.length - 1].reason}${
        artifact.rejections[artifact.rejections.length - 1].days
      }d`,
    );
    expect(screen.getByTestId('rejection-earnings-run').textContent).toMatch(/PFE/);
  });
});

describe('the synthetic-lot premise (R8)', () => {
  /** The Phase C shape: a `covered_call` artifact with a lot-based base. */
  const cc = {
    ...artifact,
    provenance: { ...artifact.provenance, strategy: 'covered_call' },
  } as SimArtifact;
  const ccDigest = computeDigest(cc, bars, { specStrategy: 'covered_call' });

  const renderers: Array<[string, (strategy: string) => JSX.Element]> = [
    ['price', (strategy) => (
      <PriceChart symbol="GOOGL" bars={bars} barsAbsence={null} artifact={cc}
        artifactAbsence={null} window={WINDOW} strategy={strategy} />
    )],
    ['equity', (strategy) => (
      <EquityChart overlay={equityOverlay(cc, null, bars)} row={row} isBase baseAbsence={null}
        artifactAbsence={null} capitalBase={100000} strategy={strategy} />
    )],
    ['premium', (strategy) => <PremiumCharts digest={ccDigest} absence={null} strategy={strategy} />],
    ['drawdown', (strategy) => (
      <DrawdownChart series={ccDigest.drawdownSeries} row={row} absence={null} strategy={strategy} />
    )],
    ['deployment', (strategy) => (
      <DeploymentChart series={ccDigest.deploymentSeries} reading={ccDigest.deployment}
        capitalBase={100000} suppressionReason={null} absence={null} strategy={strategy} />
    )],
    ['ledger', (strategy) => (
      <LedgerTable artifact={cc} absence={null} context={{ ...context, strategy }} />
    )],
    ['cycles', (strategy) => (
      <SimCycleTable artifact={cc} absence={null} row={row} strategy={strategy} />
    )],
    ['rejections', (strategy) => (
      <RejectionPanel artifact={cc} absence={null} earningsSymbolsWithoutData={[]}
        caveat={null} strategy={strategy} />
    )],
  ];

  it.each(renderers)('%s renders the premise on a covered-call cell', (_name, build) => {
    render(build('covered_call'));
    expect(screen.getAllByTestId('cc-lot-banner')[0].textContent).toMatch(
      /Synthetic lot — 100 shares at the window-start close \(D2\)/,
    );
  });

  it.each(renderers)('%s renders NOTHING of the kind on a wheel cell', (_name, build) => {
    render(build('wheel'));
    expect(screen.queryByTestId('cc-lot-banner')).toBeNull();
  });
});

describe('a stored artifact with no ledger at all (R9)', () => {
  // The live `position_20pct` cell: 189 daily rows, zero events. An empty
  // AreaChart and "0 of 0 events" read as broken; the finding is that the arm
  // never opened anything.
  const empty = { ...artifact, ledger: [], cycles: [] } as SimArtifact;
  const emptyDigest = computeDigest(empty, bars, { specStrategy: null });

  it('the premium panel says so instead of drawing an empty chart', () => {
    render(<PremiumCharts digest={emptyDigest} absence={null} stateLabel="insuf" />);
    expect(screen.getByTestId('premium-no-events').textContent).toMatch(
      /No option event in this window — the engine opened nothing/,
    );
    expect(screen.getByTestId('premium-no-events').textContent).toMatch(/this cell is insuf/);
  });

  it('the ledger table says so instead of an empty table', () => {
    render(<LedgerTable artifact={empty} absence={null} context={context} stateLabel="insuf" />);
    expect(screen.getByTestId('ledger-no-events').textContent).toMatch(
      /No option event in this window/,
    );
    expect(screen.getByTestId('ledger-no-events').textContent).toMatch(
      /a stored result, not a missing artifact/,
    );
    expect(screen.queryByTestId('ledger-export-csv')).toBeNull();
  });
});
