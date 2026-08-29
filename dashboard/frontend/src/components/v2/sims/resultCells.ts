// FC-060 Layer 4 (PR-B): how one sweep cell is rendered.
//
// Rendering only. There is no `median()` and no `summarise()` in this file, and
// there must never be: every median, min, max and count on the page is READ from
// the server's `summary` (plan D11). A second implementation of "which cells
// count" is a second chance to average an `insuf` cell into a ranking, and only
// one of the two would be the one under test.
//
// The rules this file enforces, both from the plan:
//
//   * `measured` / `insufficient` / `low_activity` / errored PARTITION the cells
//     that resolved, and `unknown` names the ones that did not. Five kinds, five
//     visually distinct renderings, and the counts add up to the cells shown.
//   * A cell that is not `measured` NEVER renders as a return. No number, no
//     P&L colour, no verdict glyph. "insufficient" is a statement about the
//     window; "unknown" is a statement about our records.

import type { SweepReport, SweepResultRow, SweepSummaryRow } from '../../../types/v2';

export type CellKind = 'return' | 'insuf' | 'low-act' | 'err' | 'unknown' | 'missing';

export interface RenderedCell {
  kind: CellKind;
  /** Exactly what the cell shows. Never a number unless `kind === 'return'`. */
  text: string;
  /** Tailwind classes. Only `return` is ever coloured by sign. */
  className: string;
  /** Hover text: the error, or why the cell carries no number. */
  title: string;
}

/** report.py `_VERDICT_GLYPH`. */
const VERDICT_GLYPH: Record<string, string> = {
  fit: '+',
  marginal: '~',
  unfit: '-',
  insufficient: '?',
};

/** report.py `_pct`: a missing number is an em dash, never "0.0%". */
export const pctOrDash = (value: number | null | undefined, digits = 1): string =>
  value === null || value === undefined || Number.isNaN(value)
    ? '—'
    : `${value >= 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`;

/**
 * Classify + render one cell.
 *
 * Order is the runner's: error, then `insufficient`, then `low_activity`, then
 * — and ONLY then — a measured return. `insufficient` wins over `low_activity`
 * because a window with no completed cycle also has a tiny days-in-position
 * fraction, and the two answer different questions.
 *
 * The final branch is gated on `row.measured` rather than reached by
 * fall-through. A cell whose state the server could not classify (`unknown`:
 * no flag set, e.g. a row written before a flag existed) previously fell
 * through and rendered as a green return — an unresolved verdict presented as
 * a result, which is the single worst thing this page could do.
 */
export function renderCell(row: SweepResultRow | undefined | null): RenderedCell {
  if (!row) {
    return {
      kind: 'missing',
      text: '—',
      className: 'text-gray-600',
      title: 'No cell was produced for this scenario/symbol.',
    };
  }
  if (row.error) {
    return {
      kind: 'err',
      text: 'err',
      className: 'bg-red-950/60 text-red-300 border border-red-800/70 font-semibold',
      title: row.error,
    };
  }
  if (row.insufficient) {
    return {
      kind: 'insuf',
      text: 'insuf',
      className: 'bg-gray-700/60 text-gray-300 border border-gray-600 font-mono',
      title:
        'No completed cycle in this window. This is a statement about the WINDOW, ' +
        'not a measurement of the arm — it is never averaged into a median.',
    };
  }
  if (row.low_activity) {
    const frac = row.days_in_position_fraction;
    return {
      kind: 'low-act',
      text: `low-act ${frac === null || frac === undefined ? '—' : `${Math.round(frac * 100)}%`}`,
      className: 'bg-amber-950/60 text-amber-300 border border-amber-800/70 font-mono',
      title:
        'A position was held on too few decision days for the annualised number to mean ' +
        'anything (the fewer days deployed, the more one lucky trade is multiplied by ' +
        '365/days). Counted, never averaged.',
    };
  }
  if (!row.measured) {
    return {
      kind: 'unknown',
      text: 'unknown',
      className: 'bg-purple-950/50 text-purple-300 border border-dashed border-purple-700 font-mono',
      title:
        'The stored row carries none of the four state flags, so this cell was never ' +
        'classified — a row written before a flag existed, or a verdict that never ' +
        'resolved. It is NOT a measurement: it is excluded from every median and Δ, ' +
        'and it is counted separately so the row still adds up.',
    };
  }
  const glyph = VERDICT_GLYPH[row.verdict ?? ''] ?? '?';
  const r = row.annualized_return;
  const sign =
    r === null || r === undefined
      ? 'text-gray-300'
      : r > 0
        ? 'text-green-400'
        : r < 0
          ? 'text-red-400'
          : 'text-gray-300';
  return {
    kind: 'return',
    text: `${pctOrDash(r)} ${glyph}`,
    className: `${sign} font-medium`,
    title: `verdict: ${row.verdict ?? 'unknown'}${
      row.days_in_position_fraction !== null && row.days_in_position_fraction !== undefined
        ? ` · in position ${Math.round(row.days_in_position_fraction * 100)}% of decision days`
        : ''
    }`,
  };
}

// --- lookups ---------------------------------------------------------------- //

export type RowIndex = Map<string, SweepResultRow>;

// The separator is an escaped NUL, which no scenario name, symbol or split can
// contain -- so two different triples can never collide on one key.
const cellKey = (scenario: string, symbol: string, split: string) =>
  `${scenario}\u0000${symbol}\u0000${split}`;

export function indexRows(rows: SweepResultRow[]): RowIndex {
  const index: RowIndex = new Map();
  for (const row of rows) index.set(cellKey(row.scenario, row.symbol, row.split), row);
  return index;
}

export const lookupCell = (
  index: RowIndex,
  scenario: string,
  symbol: string,
  split: string,
): SweepResultRow | undefined => index.get(cellKey(scenario, symbol, split));

/** The server's summary row for one (scenario, split), or undefined. */
export const summaryFor = (
  report: SweepReport,
  scenario: string,
  split: string,
): SweepSummaryRow | undefined =>
  report.summary.find((s) => s.scenario === scenario && s.split === split);

/**
 * How many cells in this (scenario, split) the server could not classify.
 *
 * This is a COUNT OF A RENDERED STATE, not a statistic: the server's summary has
 * no `unknown` column, and without this the four counts would not add up to the
 * cells actually on screen — and a reader who cannot add up the row stops
 * trusting the table (the exact bug the runner's own summary once had).
 */
export const unknownCount = (rows: SweepResultRow[], scenario: string, split: string): number =>
  rows.filter(
    (r) =>
      r.scenario === scenario &&
      r.split === split &&
      !r.error &&
      !r.insufficient &&
      !r.low_activity &&
      !r.measured,
  ).length;

/** The splits present in the report, in the order the runner ran them. */
export const splitsOf = (report: SweepReport): string[] => report.windows.map((w) => w.split);
