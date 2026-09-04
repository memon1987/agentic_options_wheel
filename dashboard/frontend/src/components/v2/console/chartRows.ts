// FC-096 Phase E PR-3 (design decision 1): the price chart's row array.
//
// ONE row per session, and every marker is a key ON THAT ROW whose value is the
// session's close. That is the whole design and it is not a stylistic choice:
// recharts positions a `Scatter` that carries its own `data` on ITS OWN domain,
// so a marker series built from ledger dates lands wherever those dates fall in
// a 72-point sequence rather than on the 189-point price axis — markers that
// look plausible, are drawn on the wrong days, and cannot be caught by eye. The
// test that pins this asserts each marker's row IS its session's row and each
// marker's y IS that row's close.
//
// An event dated on a NON-session (a weekend expiry, an off-session dividend)
// is attached to the nearest PRIOR session and flagged rather than dropped: the
// cash moved, and a chart that silently omits it is a chart that disagrees with
// the ledger table below it. An event before the first session has no prior
// session to attach to; it is counted and reported, never silently discarded.

import type { SimBar, SimLedgerEvent, SimRollRecord } from '../../../types/v2';

/** How one ledger kind is drawn. Order here is the legend's order. */
export interface MarkerSpec {
  kind: string;
  /** The row key holding this marker's y value. */
  key: string;
  label: string;
  /** A text glyph, for the legend and the tooltip — not the plotted shape. */
  glyph: string;
  /** A recharts `Scatter` shape name. */
  shape: 'circle' | 'cross' | 'diamond' | 'square' | 'star' | 'triangle' | 'wye';
  color: string;
}

/** The bucket every unrecognised `kind` falls into. Named, never dropped. */
export const UNKNOWN_MARKER_KIND = '__unknown__';

/**
 * The nine marker classes (§PR-3). `unknown` is a CLASS, not an error: a ledger
 * kind this build has never seen (Phase C's `synthetic_lot_open` before this
 * file knew it) renders with a neutral marker and its raw kind in the tooltip.
 */
export const MARKER_SPECS: MarkerSpec[] = [
  // Glyph and shape AGREE (review round 1, LOW): the legend's ▽ used to sit
  // beside a filled recharts `triangle` and its △ beside a `star`, so the key
  // described marks that were not on the chart. Colour plus label is what
  // distinguishes two kinds that share a shape.
  { kind: 'sell_put_open', key: 'mk_sell_put_open', label: 'Put sold', glyph: '▲', shape: 'triangle', color: '#60a5fa' },
  { kind: 'sell_call_open', key: 'mk_sell_call_open', label: 'Call sold', glyph: '★', shape: 'star', color: '#a78bfa' },
  { kind: 'put_assignment', key: 'mk_put_assignment', label: 'Put assigned', glyph: '◆', shape: 'diamond', color: '#f59e0b' },
  { kind: 'call_assignment', key: 'mk_call_assignment', label: 'Called away', glyph: '■', shape: 'square', color: '#34d399' },
  { kind: 'buy_to_close', key: 'mk_buy_to_close', label: 'Bought to close', glyph: '✚', shape: 'cross', color: '#f87171' },
  { kind: 'expire_worthless', key: 'mk_expire_worthless', label: 'Expired worthless', glyph: '●', shape: 'circle', color: '#9ca3af' },
  { kind: 'dividend', key: 'mk_dividend', label: 'Dividend', glyph: 'Y', shape: 'wye', color: '#22d3ee' },
  { kind: 'synthetic_lot_open', key: 'mk_synthetic_lot_open', label: 'Synthetic lot opened', glyph: '■', shape: 'square', color: '#e879f9' },
  { kind: UNKNOWN_MARKER_KIND, key: 'mk_unknown', label: 'Other event', glyph: '●', shape: 'circle', color: '#d1d5db' },
];

const SPEC_BY_KIND = new Map(MARKER_SPECS.map((s) => [s.kind, s]));

export const markerSpecFor = (kind: string): MarkerSpec =>
  SPEC_BY_KIND.get(kind) ?? (SPEC_BY_KIND.get(UNKNOWN_MARKER_KIND) as MarkerSpec);

/** One event as the chart holds it: the ledger event plus how it was placed. */
export interface PlacedEvent {
  event: SimLedgerEvent;
  /** True when the event's own date is not a session in this window. */
  offSession: boolean;
}

export interface ChartRow {
  date: string;
  close: number;
  open: number | null;
  high: number | null;
  low: number | null;
  /** Every event placed on this session, in ledger order. */
  events: PlacedEvent[];
  /** At least one of `events` was attached from a non-session date. */
  hasOffSession: boolean;
  /** `mk_*` -> this row's close, for each marker kind present on this session. */
  [markerKey: string]: unknown;
}

export interface RollLine {
  /** The SESSION the line is drawn on — a roll's own day when it is one. */
  date: string;
  day: string;
  label: string;
  offSession: boolean;
}

export interface ChartRowsResult {
  rows: ChartRow[];
  /** Only the marker classes actually present, with their counts. */
  markers: Array<MarkerSpec & { count: number }>;
  rollLines: RollLine[];
  /** Events attached to an earlier session because their date was not one. */
  offSessionEvents: number;
  /** Events dated BEFORE the first session: nowhere to attach. Reported. */
  droppedEvents: SimLedgerEvent[];
  /** Events (and rolls) actually placed on a row. */
  placedEvents: number;
  /**
   * Events hidden UNDER another marker of the same kind on the same session
   * (review round 1, LOW).
   *
   * One row key holds one y value, so two puts sold on one day draw one
   * triangle while the legend counts two events. The count is reported so the
   * chart can say the marks are sessions-with-an-event, not events.
   */
  collapsedEvents: number;
}

/**
 * The index of the session an event on `date` belongs to.
 *
 * An exact session wins. Otherwise the nearest PRIOR session — an expiry on a
 * Saturday belongs to Friday's close, not to Monday's, because Friday is the
 * last price the decision could have been made against. `-1` means the date
 * precedes every session in the window.
 */
export function sessionIndexFor(sessions: string[], date: string): number {
  let lo = 0;
  let hi = sessions.length - 1;
  let best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (sessions[mid] <= date) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return best;
}

/** Bars clipped to `[start, end]` inclusive, in date order. */
export function clipBars(bars: SimBar[], start: string | null, end: string | null): SimBar[] {
  return bars
    .filter((b) => (!start || b.date >= start) && (!end || b.date <= end))
    .sort((a, b) => a.date.localeCompare(b.date));
}

/**
 * One row array for the price chart: closes, markers and roll lines.
 *
 * `bars` are the sessions — the sidecar's, or the BQ fallback's. `ledger` and
 * `rollRecords` are empty in fallback mode, which is how the fallback chart
 * carries no markers without a second code path deciding it.
 */
export function chartRows(
  bars: SimBar[],
  ledger: SimLedgerEvent[],
  rollRecords: SimRollRecord[],
): ChartRowsResult {
  const sorted = [...bars].sort((a, b) => a.date.localeCompare(b.date));
  const sessions = sorted.map((b) => b.date);
  const rows: ChartRow[] = sorted.map((bar) => ({
    date: bar.date,
    close: bar.close,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    events: [],
    hasOffSession: false,
  }));

  const counts = new Map<string, number>();
  const droppedEvents: SimLedgerEvent[] = [];
  let offSessionEvents = 0;
  let placedEvents = 0;
  let collapsedEvents = 0;

  for (const event of ledger) {
    const index = sessionIndexFor(sessions, event.date);
    if (index < 0) {
      droppedEvents.push(event);
      continue;
    }
    const row = rows[index];
    const offSession = row.date !== event.date;
    if (offSession) {
      offSessionEvents += 1;
      row.hasOffSession = true;
    }
    row.events.push({ event, offSession });
    const spec = markerSpecFor(SPEC_BY_KIND.has(event.kind) ? event.kind : UNKNOWN_MARKER_KIND);
    if (row[spec.key] !== undefined) collapsedEvents += 1;
    // The marker's y IS the session's close. Never the strike, never the fill
    // price: a marker drawn at its own price would sit off the line it is
    // meant to annotate, and the tooltip carries the trade's price anyway.
    row[spec.key] = row.close;
    counts.set(spec.kind, (counts.get(spec.kind) ?? 0) + 1);
    placedEvents += 1;
  }

  const rollLines: RollLine[] = [];
  for (const roll of rollRecords) {
    const index = sessionIndexFor(sessions, roll.day);
    if (index < 0) continue;
    const from = roll.old_strike === null ? '?' : roll.old_strike.toFixed(2);
    const to = roll.new_strike === null ? '?' : roll.new_strike.toFixed(2);
    rollLines.push({
      date: rows[index].date,
      day: roll.day,
      label: `roll ${from} → ${to}`,
      offSession: rows[index].date !== roll.day,
    });
  }

  const markers = MARKER_SPECS.filter((s) => (counts.get(s.kind) ?? 0) > 0).map((s) => ({
    ...s,
    count: counts.get(s.kind) as number,
  }));

  return {
    rows,
    markers,
    rollLines,
    offSessionEvents,
    droppedEvents,
    placedEvents,
    collapsedEvents,
  };
}
