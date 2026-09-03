// FC-096 Phase E PR-3: the price chart's rows, pinned on the REAL captured
// objects (run `13cc2729d1c74211`, GOOGL fit: 189 sessions, 72 ledger events,
// 6 rolls).
//
// This is where the chart is actually tested. Under jsdom's `ResizeObserver`
// stub recharts renders a 0x0 container and no marks at all, so a DOM test can
// only ever assert the caption — which is exactly why decision 1's marker
// alignment lives in a pure function with these tests around it.

import { describe, expect, it } from 'vitest';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import bars13cc from '../../../test/fixtures/bars_13cc_googl_fit.json';
import type { SimArtifact, SimBar, SimBars, SimLedgerEvent } from '../../../types/v2';
import { normaliseArtifact, normaliseBars } from './normaliseArtifact';
import {
  chartRows,
  clipBars,
  markerSpecFor,
  MARKER_SPECS,
  sessionIndexFor,
  UNKNOWN_MARKER_KIND,
} from './chartRows';

const artifact = normaliseArtifact(artifact13cc) as SimArtifact;
const bars = normaliseBars(bars13cc) as SimBars;

const build = () => chartRows(bars.bars, artifact.ledger, artifact.roll_records);

describe('chartRows — the captured GOOGL fit cell', () => {
  it('renders one row per SESSION, not one per event', () => {
    const { rows } = build();
    expect(rows).toHaveLength(189);
    expect(rows).toHaveLength(bars.bars.length);
    expect(rows[0].date).toBe('2025-09-02');
    expect(rows[rows.length - 1].date).toBe('2026-06-02');
  });

  it('places all 72 ledger events, in the engine-shaped marker counts', () => {
    const { markers, placedEvents, droppedEvents, offSessionEvents } = build();
    expect(placedEvents).toBe(72);
    expect(droppedEvents).toEqual([]);
    expect(offSessionEvents).toBe(0);
    const counts = Object.fromEntries(markers.map((m) => [m.kind, m.count]));
    expect(counts).toEqual({
      sell_put_open: 23,
      sell_call_open: 13,
      put_assignment: 4,
      call_assignment: 3,
      buy_to_close: 6,
      expire_worthless: 22,
      dividend: 1,
    });
    expect(Object.values(counts).reduce((a, b) => a + b, 0)).toBe(72);
  });

  it("every marker's x is its own session and its y is that session's close", () => {
    // THE test for decision 1. A `Scatter` carrying its own `data` would put
    // these on a 72-point domain; here each marker is a key on the session row.
    const { rows } = build();
    const byDate = new Map(rows.map((row) => [row.date, row]));
    let checked = 0;
    for (const event of artifact.ledger) {
      const row = byDate.get(event.date);
      expect(row, `no session row for ${event.date}`).toBeDefined();
      const spec = markerSpecFor(event.kind);
      expect(row![spec.key]).toBe(row!.close);
      expect(row!.events.some((placed) => placed.event === event)).toBe(true);
      checked += 1;
    }
    expect(checked).toBe(72);
  });

  it('draws one reference line per roll, on the roll’s own session', () => {
    const { rollLines } = build();
    expect(rollLines).toHaveLength(6);
    const days = artifact.roll_records.map((r) => r.day);
    expect(rollLines.map((line) => line.date)).toEqual(days);
    expect(rollLines.every((line) => !line.offSession)).toBe(true);
    expect(rollLines[0].label).toBe('roll 252.50 → 255.00');
  });

  it('offers a marker class only for the kinds present', () => {
    const { markers } = build();
    expect(markers.map((m) => m.kind)).not.toContain('synthetic_lot_open');
    expect(markers.map((m) => m.kind)).not.toContain(UNKNOWN_MARKER_KIND);
    // Legend order is the declared order, not first-seen order.
    const declared = MARKER_SPECS.map((s) => s.kind);
    const seen = markers.map((m) => m.kind);
    expect([...seen].sort((a, b) => declared.indexOf(a) - declared.indexOf(b))).toEqual(seen);
  });
});

describe('chartRows — placement rules', () => {
  const sessions: SimBar[] = [
    { date: '2026-01-02', open: 1, high: 1, low: 1, close: 100, volume: 1 },
    { date: '2026-01-05', open: 1, high: 1, low: 1, close: 110, volume: 1 },
  ];
  const event = (date: string, kind: string): SimLedgerEvent => ({
    date,
    kind,
    underlying: 'X',
    symbol: 'X',
    contracts: 1,
    shares: 0,
    price: 1,
    cash_delta: 1,
    fees: 0,
    detail: {},
  });

  it('attaches an OFF-SESSION event to the nearest prior session and flags it', () => {
    // A Saturday expiry. Dropping it would make the chart disagree with the
    // ledger table below it; attaching it forward to Monday would place a
    // Friday decision on a price it could not have been made against.
    const result = chartRows(sessions, [event('2026-01-03', 'expire_worthless')], []);
    expect(result.offSessionEvents).toBe(1);
    expect(result.droppedEvents).toEqual([]);
    expect(result.rows[0].hasOffSession).toBe(true);
    expect(result.rows[0].events[0].offSession).toBe(true);
    expect(result.rows[0].mk_expire_worthless).toBe(100);
    expect(result.rows[1].hasOffSession).toBe(false);
  });

  it('reports — never silently drops — an event before the first session', () => {
    const result = chartRows(sessions, [event('2025-12-31', 'sell_put_open')], []);
    expect(result.droppedEvents).toHaveLength(1);
    expect(result.placedEvents).toBe(0);
  });

  it('gives an unknown kind the neutral marker rather than dropping it', () => {
    const result = chartRows(sessions, [event('2026-01-05', 'teleported_lot')], []);
    expect(result.placedEvents).toBe(1);
    expect(result.markers.map((m) => m.kind)).toEqual([UNKNOWN_MARKER_KIND]);
    expect(result.rows[1].mk_unknown).toBe(110);
    expect(result.rows[1].events[0].event.kind).toBe('teleported_lot');
  });

  it('sessionIndexFor: exact hit, prior session, and before-the-window', () => {
    const dates = sessions.map((s) => s.date);
    expect(sessionIndexFor(dates, '2026-01-05')).toBe(1);
    expect(sessionIndexFor(dates, '2026-01-04')).toBe(0);
    expect(sessionIndexFor(dates, '2026-01-01')).toBe(-1);
  });

  it('clipBars keeps the window inclusive and sorts', () => {
    const shuffled = [...sessions].reverse();
    expect(clipBars(shuffled, '2026-01-02', '2026-01-05').map((b) => b.date)).toEqual([
      '2026-01-02',
      '2026-01-05',
    ]);
    expect(clipBars(shuffled, '2026-01-03', null).map((b) => b.date)).toEqual(['2026-01-05']);
  });
});
