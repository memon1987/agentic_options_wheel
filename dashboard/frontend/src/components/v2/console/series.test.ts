// FC-096 Phase E PR-3: the chart series, pinned against the ENGINE'S OWN
// numbers on the real captured cell (run `13cc2729d1c74211`, GOOGL fit).
//
// The equalities here are the reason the charts and the grid above them cannot
// disagree: the drawdown series' minimum IS the row's `max_drawdown`, and the
// cumulative option-cash line's last point IS the row's `option_pnl`. Neither
// is displayed twice — the headers show the ROW's number — but if the series
// ever stopped matching it, the picture would be telling a different story from
// the number printed over it.

import { describe, expect, it } from 'vitest';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import bars13cc from '../../../test/fixtures/bars_13cc_googl_fit.json';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import type { SimArtifact, SimBars } from '../../../types/v2';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { indexRows, lookupCell } from '../sims/resultCells';
import { normaliseArtifact, normaliseBars } from './normaliseArtifact';
import {
  deploymentSeries,
  drawdownSeries,
  equityOverlay,
  monthlyFromLedger,
  portfolioIndex,
} from './series';

const artifact = normaliseArtifact(artifact13cc) as SimArtifact;
const bars = normaliseBars(bars13cc) as SimBars;
const report = normaliseSweepDetail(shaped13cc)!.results!;
const row = lookupCell(indexRows(report.rows), 'base', 'GOOGL', 'fit')!;

describe('drawdownSeries', () => {
  it('has one point per decision day and a minimum equal to the ROW’s max drawdown', () => {
    const series = drawdownSeries(artifact.daily);
    expect(series).toHaveLength(189);
    const min = Math.min(...series.map((p) => p.drawdown));
    expect(min).toBeCloseTo(row.max_drawdown as number, 12);
    expect(row.max_drawdown).toBeCloseTo(-0.0337547641325223, 12);
  });

  it('measures from the RUNNING PEAK, not from the first day’s equity', () => {
    // The mutation this kills: `(equity - equity[0]) / equity[0]`. On a series
    // that rises and then falls back through its start, that reading bottoms
    // out at a different, smaller number than the engine's.
    const daily = [100, 120, 90, 95].map((equity, i) => ({
      date: `2026-01-0${i + 1}`,
      equity,
      cash: 0,
      reserved_collateral: 0,
      open_options: 0,
      shares_held: {},
    }));
    const series = drawdownSeries(daily);
    expect(series.map((p) => p.drawdown)).toEqual([0, 0, (90 - 120) / 120, (95 - 120) / 120]);
    expect(Math.min(...series.map((p) => p.drawdown))).toBeCloseTo(-0.25, 12);
  });
});

describe('monthlyFromLedger', () => {
  it('sums to the row’s option P&L, and the bars sum to the line’s last point', () => {
    const { monthly, cumulative, total } = monthlyFromLedger(artifact.ledger);
    expect(total).toBeCloseTo(row.option_pnl as number, 6);
    expect(total).toBeCloseTo(4453.488531114683, 6);
    const barSum = monthly.reduce((sum, m) => sum + m.net_option_cashflow, 0);
    expect(barSum).toBeCloseTo(total, 6);
    expect(cumulative[cumulative.length - 1].value).toBeCloseTo(total, 12);
  });

  it('buckets by calendar month in order, with put/call split and event counts', () => {
    const { monthly } = monthlyFromLedger(artifact.ledger);
    expect(monthly.map((m) => m.month)).toEqual([...monthly.map((m) => m.month)].sort());
    expect(monthly[0].month).toBe('2025-09');
    const events = monthly.reduce((sum, m) => sum + m.event_count, 0);
    // The four option-lifecycle kinds only: assignments and the dividend are
    // the stock leg and are deliberately outside this series.
    expect(events).toBe(23 + 13 + 6 + 22);
    for (const month of monthly) {
      expect(month.put_net_cashflow + month.call_net_cashflow).toBeCloseTo(
        month.net_option_cashflow,
        6,
      );
    }
  });
});

describe('deploymentSeries', () => {
  it('prices shares at the SIDECAR’s closes and yields the all-days mean ratio', () => {
    const { series, reading } = deploymentSeries(
      artifact.daily,
      bars,
      artifact.cycles,
      artifact.provenance.capital_base ?? null,
    );
    expect(series).toHaveLength(189);
    expect(reading.basis).toBe('closes');
    expect(reading.days).toBe(189);
    expect(reading.unresolvedShareDays).toBe(0);
    // The reserved-only component, pinned: $14,328 all-days (§D-4). The
    // deployed-days mean is $29,758 — a 2x difference, which is why the
    // definition is stated rather than assumed.
    expect(reading.reservedMeanDollars).toBeCloseTo(14328.04, 1);
    const mean =
      series.reduce((sum, p) => sum + p.reserved + (p.sharesValue ?? 0), 0) / series.length;
    expect(reading.ratio! * 100000).toBeCloseTo(mean, 6);
  });

  it('falls back to cost basis, labelled “at cost”, with no sidecar', () => {
    const { reading } = deploymentSeries(artifact.daily, null, artifact.cycles, 100000);
    expect(reading.basis).toBe('at cost');
    expect(reading.ratio).not.toBeNull();
  });

  it('withholds the ratio without a capital base', () => {
    const { reading, series } = deploymentSeries(artifact.daily, bars, artifact.cycles, null);
    expect(reading.ratio).toBeNull();
    expect(series.every((p) => p.ratio === null)).toBe(true);
  });
});

describe('equityOverlay', () => {
  it('draws the engine’s own buy-and-hold curve beside the cell’s equity', () => {
    const overlay = equityOverlay(artifact, null, bars);
    expect(overlay.rows).toHaveLength(189);
    expect(overlay.hasBenchmark).toBe(true);
    expect(overlay.hasBase).toBe(false);
    expect(overlay.benchmarkMismatch).toBeNull();
    expect(overlay.benchmarkUnverified).toBe(false);
    const last = overlay.rows[overlay.rows.length - 1];
    expect(last.equity).toBeCloseTo(artifact.counters.final_equity as number, 6);
    // The engine's number, not one this module derived: 100,000 x (1 + 0.7148).
    expect(last.benchmark).toBeCloseTo(171484.49, 2);
    expect(last.benchmark).toBeCloseTo(bars.buy_and_hold!.final_value, 6);
  });

  it('REFUSES a sidecar whose final value disagrees with the cell’s stamp', () => {
    const wrong = {
      ...bars,
      buy_and_hold: { ...bars.buy_and_hold!, final_value: 171484.49 + 1000 },
    } as SimBars;
    const overlay = equityOverlay(artifact, null, wrong);
    expect(overlay.hasBenchmark).toBe(false);
    expect(overlay.benchmarkMismatch).toContain('Benchmark mismatch');
    expect(overlay.rows.every((r) => r.benchmark === null)).toBe(true);
  });

  it('unions the dates so a shorter base arm cannot truncate the cell', () => {
    const shortBase = {
      ...artifact,
      daily: artifact.daily.slice(0, 10),
    } as SimArtifact;
    const overlay = equityOverlay(artifact, shortBase, bars);
    expect(overlay.hasBase).toBe(true);
    expect(overlay.rows).toHaveLength(189);
    expect(overlay.rows[9].base).not.toBeNull();
    expect(overlay.rows[10].base).toBeNull();
    expect(overlay.rows[10].equity).not.toBeNull();
  });

  it('omits the benchmark entirely when there is no sidecar', () => {
    const overlay = equityOverlay(artifact, null, null);
    expect(overlay.hasBenchmark).toBe(false);
    expect(overlay.benchmarkMismatch).toBeNull();
    expect(overlay.rows.every((r) => r.benchmark === null)).toBe(true);
  });
});

describe('portfolioIndex', () => {
  /** A synthetic single-symbol artifact, so the index has something to average. */
  const cell = (equities: number[], base: number | null): SimArtifact =>
    ({
      ...artifact,
      provenance: { ...artifact.provenance, capital_base: base ?? undefined, starting_cash: base },
      daily: equities.map((equity, i) => ({
        date: `2026-01-0${i + 1}`,
        equity,
        cash: 0,
        reserved_collateral: 0,
        open_options: 0,
        shares_held: {},
      })),
    }) as SimArtifact;

  it('averages equity/capital_base and starts at 100', () => {
    const result = portfolioIndex(
      {
        AAA: { artifact: cell([100, 110], 100) },
        BBB: { artifact: cell([200, 180], 200) },
      },
      ['AAA', 'BBB'],
    );
    expect(result.included).toEqual(['AAA', 'BBB']);
    expect(result.loaded).toBe(2);
    expect(result.total).toBe(2);
    expect(result.rows.map((r) => r.index)).toEqual([100, (1.1 + 0.9) / 2 * 100]);
  });

  it('EXCLUDES a non-measured symbol and NAMES it', () => {
    // The mutation this kills: including an `insuf` cell. Such a cell is a flat
    // line at its starting cash, and averaging it in reads as stability.
    const result = portfolioIndex(
      {
        AAA: { artifact: cell([100, 110], 100) },
        BBB: { artifact: cell([200, 200], 200) },
      },
      ['AAA'],
    );
    expect(result.included).toEqual(['AAA']);
    expect(result.rows[0].index).toBeCloseTo(100, 9);
    // 110, not 100: averaging BBB's flat 200/200 back in would drag it there.
    expect(result.rows[1].index).toBeCloseTo(110, 9);
    expect(result.excluded.map((e) => e.symbol)).not.toContain('AAA');
  });

  it('excludes a cell with no stamped capital base rather than guessing one', () => {
    const noBase = { ...cell([100, 110], null) } as SimArtifact;
    noBase.provenance = { ...noBase.provenance, capital_base: undefined, starting_cash: 100 };
    const result = portfolioIndex(
      { AAA: { artifact: noBase, specStrategy: 'covered_call' } },
      ['AAA'],
    );
    expect(result.included).toEqual([]);
    expect(result.excluded[0].symbol).toBe('AAA');
    expect(result.excluded[0].reason).toContain('Capital base not stamped');
  });

  it('names an absent artifact and reports “k of N” while the rest load', () => {
    const result = portfolioIndex(
      {
        AAA: { artifact: cell([100, 110], 100) },
        BBB: { artifact: null, absence: 'No detail artifact for this cell in this run.' },
        CCC: { artifact: null, absence: null },
      },
      ['AAA', 'BBB', 'CCC'],
    );
    expect(result.loaded).toBe(1);
    expect(result.total).toBe(3);
    expect(result.excluded).toEqual([
      { symbol: 'BBB', reason: 'No detail artifact for this cell in this run.' },
    ]);
  });

  it('builds over the date INTERSECTION so a late symbol cannot step the line', () => {
    const result = portfolioIndex(
      {
        AAA: { artifact: cell([100, 110, 120], 100) },
        BBB: { artifact: cell([200, 220], 200) },
      },
      ['AAA', 'BBB'],
    );
    expect(result.rows).toHaveLength(2);
    expect(result.droppedDates).toBe(1);
  });
});
