// FC-096 Phase E PR-3 (design decision 3): the two exports, and the table's
// sort and filter.
//
// The rule that shapes the CSV: **a file that leaves this console must carry
// what it is on every row.** Not in a `#` header block — Excel renders those as
// a broken first row and `pandas.read_csv` needs a `comment='#'` nobody passes,
// so the provenance would be the first thing lost and the file would circulate
// as an unlabelled table of trades from an unnamed engine build. Instead the
// nine provenance fields are TRAILING COLUMNS, repeated on every row: the file
// is a plain RFC-4180 table everywhere, and any row of it, pasted anywhere,
// still names its run, its arm, its window and its engine.
//
// The JSON export wraps rather than dumps, for the same reason: `{sweep_context,
// artifact}`. It is RE-SERIALISED from the parsed object and says so in the
// context — the endpoint's bytes are gzipped and long gone by the time the
// download happens, and claiming byte-identity we cannot deliver is worse than
// stating the truth in a field the reader can see.

import type {
  SimArtifact,
  SimLedgerEvent,
  SweepBias,
  SweepResultRow,
} from '../../../types/v2';

/** Everything an exported file must carry to be readable on its own. */
export interface ExportContext {
  runId: string;
  scenario: string;
  symbol: string;
  split: string;
  engineIdentity: string | null;
  fillBasis: string | null;
  fillHaircut: number | null;
  inSampleOnly: boolean;
  inSampleBanner: string | null;
  strategy: string;
  knownBiases: SweepBias[];
  /** The grid cell's scalars, exactly as the server served them. */
  row: SweepResultRow | null;
}

/** The event columns, then the nine trailing provenance columns. */
export const LEDGER_CSV_COLUMNS = [
  'date',
  'kind',
  'underlying',
  'contract',
  'contracts',
  'shares',
  'price',
  'cash_delta',
  'fees',
  'detail',
] as const;

export const PROVENANCE_CSV_COLUMNS = [
  'run_id',
  'scenario',
  'symbol',
  'split',
  'engine_identity',
  'fill_basis',
  'fill_haircut',
  'in_sample_only',
  'strategy',
] as const;

/**
 * RFC-4180 quoting: a field containing a comma, a quote or a newline is
 * wrapped in quotes and its own quotes are doubled.
 *
 * `detail` is the field that makes this load-bearing rather than theoretical —
 * it is a JSON object stringified into ONE column, so it contains quotes and
 * commas on almost every row.
 */
export function csvField(value: unknown): string {
  const text =
    value === null || value === undefined
      ? ''
      : typeof value === 'string'
        ? value
        : typeof value === 'object'
          ? JSON.stringify(value)
          : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** The provenance cells, in `PROVENANCE_CSV_COLUMNS` order. */
function provenanceCells(context: ExportContext): unknown[] {
  return [
    context.runId,
    context.scenario,
    context.symbol,
    context.split,
    context.engineIdentity,
    context.fillBasis,
    context.fillHaircut,
    context.inSampleOnly,
    context.strategy,
  ];
}

/**
 * The ledger as a CSV, one row per event, provenance on every row.
 *
 * `rows` is the table's CURRENT view — sorted and filtered — because an export
 * that silently differs from the screen is how a reader ends up arguing with a
 * file about what they were looking at. The header block is the columns and
 * nothing else: no `#` lines, ever.
 */
export function ledgerCsv(rows: SimLedgerEvent[], context: ExportContext): string {
  const header = [...LEDGER_CSV_COLUMNS, ...PROVENANCE_CSV_COLUMNS].join(',');
  const provenance = provenanceCells(context).map(csvField);
  const lines = rows.map((event) =>
    [
      csvField(event.date),
      csvField(event.kind),
      csvField(event.underlying),
      csvField(event.symbol),
      csvField(event.contracts),
      csvField(event.shares),
      csvField(event.price),
      csvField(event.cash_delta),
      csvField(event.fees),
      csvField(event.detail),
      ...provenance,
    ].join(','),
  );
  return [header, ...lines].join('\r\n');
}

export const EXPORT_SERIALISATION_NOTE =
  'artifact re-serialised from the parsed object; not byte-identical to the endpoint response';

export interface ExportJson {
  sweep_context: {
    run_id: string;
    cell: { scenario: string; symbol: string; split: string };
    in_sample_only: boolean;
    in_sample_banner: string | null;
    known_biases: SweepBias[];
    row: SweepResultRow | null;
    serialisation: string;
  };
  artifact: SimArtifact;
}

/** `{sweep_context, artifact}` — the artifact, and what it is. */
export function exportJson(artifact: SimArtifact, context: ExportContext): ExportJson {
  return {
    sweep_context: {
      run_id: context.runId,
      cell: { scenario: context.scenario, symbol: context.symbol, split: context.split },
      in_sample_only: context.inSampleOnly,
      in_sample_banner: context.inSampleBanner,
      known_biases: context.knownBiases,
      row: context.row,
      serialisation: EXPORT_SERIALISATION_NOTE,
    },
    artifact,
  };
}

// --------------------------------------------------------------------------- //
// Sort and filter (pure; the table only holds the state)
// --------------------------------------------------------------------------- //

export type LedgerSortKey =
  | 'date'
  | 'kind'
  | 'symbol'
  | 'contracts'
  | 'shares'
  | 'price'
  | 'cash_delta'
  | 'fees';

export type SortDirection = 'asc' | 'desc';

const compareValues = (a: unknown, b: unknown): number => {
  if (a === null || a === undefined) return b === null || b === undefined ? 0 : -1;
  if (b === null || b === undefined) return 1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b));
};

/**
 * A STABLE sort on a copy.
 *
 * A copy because the artifact is shared through the module-level cache and two
 * panels sorting the same array in place would reorder each other's data. Stable
 * because the ledger's natural order is the engine's own event order, and it is
 * the only sensible tiebreak within a day.
 */
export function sortLedger(
  rows: SimLedgerEvent[],
  key: LedgerSortKey,
  direction: SortDirection,
): SimLedgerEvent[] {
  const sign = direction === 'asc' ? 1 : -1;
  return rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const delta = compareValues(
        a.row[key as keyof SimLedgerEvent],
        b.row[key as keyof SimLedgerEvent],
      );
      return delta !== 0 ? sign * delta : a.index - b.index;
    })
    .map((entry) => entry.row);
}

export interface LedgerFilter {
  /** Empty ⇒ every kind. Otherwise exactly these kinds. */
  kinds?: string[];
  /** Case-insensitive substring over kind, underlying, OCC symbol and detail. */
  text?: string;
}

/** Filter without reordering. Empty filters return the input order untouched. */
export function filterLedger(rows: SimLedgerEvent[], filter: LedgerFilter): SimLedgerEvent[] {
  const kinds = filter.kinds && filter.kinds.length ? new Set(filter.kinds) : null;
  const needle = (filter.text ?? '').trim().toLowerCase();
  return rows.filter((row) => {
    if (kinds && !kinds.has(row.kind)) return false;
    if (!needle) return true;
    const haystack = [row.kind, row.underlying, row.symbol, JSON.stringify(row.detail)]
      .join(' ')
      .toLowerCase();
    return haystack.includes(needle);
  });
}
