// FC-096 Phase E PR-3 (decision 3): the exports.
//
// The CSV is parsed back with a MINIMAL RFC-4180 reader written here rather
// than asserted with `toContain`. A substring assertion passes on a file whose
// quoting is broken in exactly the way that makes Excel merge two columns; a
// round trip does not.

import { describe, expect, it } from 'vitest';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import type { SimArtifact, SimLedgerEvent } from '../../../types/v2';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { indexRows, lookupCell } from '../sims/resultCells';
import { normaliseArtifact } from './normaliseArtifact';
import {
  describeFilter,
  EXPORT_SCOPE_CSV_COLUMNS,
  EXPORT_SERIALISATION_NOTE,
  exportJson,
  filterLedger,
  isFiltered,
  LEDGER_CSV_COLUMNS,
  ledgerCsv,
  PROVENANCE_CSV_COLUMNS,
  sortLedger,
  type ExportContext,
} from './ledgerCsv';

const artifact = normaliseArtifact(artifact13cc) as SimArtifact;
const report = normaliseSweepDetail(shaped13cc)!.results!;
const row = lookupCell(indexRows(report.rows), 'base', 'GOOGL', 'fit')!;

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

/** A minimal RFC-4180 reader: quoted fields, doubled quotes, CRLF rows. */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 1;
        } else quoted = false;
      } else field += ch;
      continue;
    }
    if (ch === '"') quoted = true;
    else if (ch === ',') {
      row.push(field);
      field = '';
    } else if (ch === '\r' && text[i + 1] === '\n') {
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
      i += 1;
    } else field += ch;
  }
  row.push(field);
  rows.push(row);
  return rows;
}

describe('ledgerCsv', () => {
  it('is a plain table: header, one row per event, no comment lines', () => {
    const csv = ledgerCsv(artifact.ledger, context);
    const rows = parseCsv(csv);
    expect(rows).toHaveLength(73); // 72 events + header
    expect(rows[0]).toEqual([
      ...LEDGER_CSV_COLUMNS,
      ...PROVENANCE_CSV_COLUMNS,
      ...EXPORT_SCOPE_CSV_COLUMNS,
    ]);
    // No `#` lines, ever: Excel renders them as a broken first row and pandas
    // needs a `comment='#'` nobody passes.
    expect(csv.split('\r\n').some((line) => line.startsWith('#'))).toBe(false);
  });

  it('carries the nine provenance columns on EVERY row', () => {
    const rows = parseCsv(ledgerCsv(artifact.ledger, context));
    const start = LEDGER_CSV_COLUMNS.length;
    for (const line of rows.slice(1)) {
      expect(line.slice(start, start + PROVENANCE_CSV_COLUMNS.length)).toEqual([
        '13cc2729d1c74211',
        'base',
        'GOOGL',
        'fit',
        '129711f15fe0488a',
        'mid',
        '0.25',
        'false',
        'wheel',
      ]);
    }
  });

  it('round-trips a detail containing a quote, a comma and a newline', () => {
    const nasty: SimLedgerEvent = {
      ...artifact.ledger[0],
      detail: { note: 'rolled "up", then out\nagain', collateral: 20250 },
    };
    const rows = parseCsv(ledgerCsv([nasty], context));
    const detail = rows[1][LEDGER_CSV_COLUMNS.indexOf('detail')];
    expect(JSON.parse(detail)).toEqual(nasty.detail);
    expect(rows[1]).toHaveLength(
      LEDGER_CSV_COLUMNS.length + PROVENANCE_CSV_COLUMNS.length + EXPORT_SCOPE_CSV_COLUMNS.length,
    );
  });

  it('exports exactly the rows it was handed — the table’s current view', () => {
    const filtered = filterLedger(artifact.ledger, { kinds: ['buy_to_close'] });
    expect(parseCsv(ledgerCsv(filtered, context))).toHaveLength(7);
  });
});

describe('ledgerCsv — a filtered file says so (R7)', () => {
  it('carries rows_exported / rows_total / filter on every row', () => {
    const filtered = filterLedger(artifact.ledger, { kinds: ['buy_to_close'] });
    const rows = parseCsv(
      ledgerCsv(filtered, context, { rowsTotal: artifact.ledger.length, filter: { kinds: ['buy_to_close'] } }),
    );
    const start = LEDGER_CSV_COLUMNS.length + PROVENANCE_CSV_COLUMNS.length;
    expect(rows).toHaveLength(7);
    for (const line of rows.slice(1)) {
      expect(line.slice(start)).toEqual(['6', '72', 'kinds=buy_to_close']);
    }
  });

  it('marks an UNfiltered file as the whole ledger', () => {
    const rows = parseCsv(
      ledgerCsv(artifact.ledger, context, { rowsTotal: artifact.ledger.length, filter: {} }),
    );
    const start = LEDGER_CSV_COLUMNS.length + PROVENANCE_CSV_COLUMNS.length;
    expect(rows[1].slice(start)).toEqual(['72', '72', '']);
    expect(isFiltered({ rowsTotal: 72, filter: {} }, 72)).toBe(false);
    expect(isFiltered({ rowsTotal: 72, filter: { text: 'x' } }, 72)).toBe(true);
    expect(isFiltered({ rowsTotal: 72, filter: {} }, 6)).toBe(true);
  });

  it('names the filter in words, kinds sorted and text trimmed', () => {
    expect(describeFilter({ kinds: ['sell_put_open', 'buy_to_close'], text: '  GOOGL ' })).toBe(
      'kinds=buy_to_close|sell_put_open;text=GOOGL',
    );
    expect(describeFilter({})).toBe('');
  });

  it('prints “engine default” rather than a blank fill haircut', () => {
    const rows = parseCsv(ledgerCsv([artifact.ledger[0]], { ...context, fillHaircut: null }));
    const at = LEDGER_CSV_COLUMNS.length + PROVENANCE_CSV_COLUMNS.indexOf('fill_haircut');
    expect(rows[1][at]).toBe('engine default');
  });
});

describe('exportJson', () => {
  it('wraps the artifact in a sweep context that says what it is', () => {
    const payload = exportJson(artifact, context);
    expect(Object.keys(payload)).toEqual(['sweep_context', 'artifact']);
    expect(payload.artifact).toBe(artifact);
    expect(payload.sweep_context.run_id).toBe('13cc2729d1c74211');
    expect(payload.sweep_context.cell).toEqual({
      scenario: 'base',
      symbol: 'GOOGL',
      split: 'fit',
    });
    expect(payload.sweep_context.in_sample_only).toBe(false);
    expect(payload.sweep_context.known_biases.length).toBeGreaterThan(0);
    expect(payload.sweep_context.row?.annualized_return).toBeCloseTo(0.09026715435373105, 12);
    // Never "byte-identical" (review B9): it is re-serialised and says so.
    expect(payload.sweep_context.serialisation).toBe(EXPORT_SERIALISATION_NOTE);
    expect(JSON.stringify(payload).length).toBeGreaterThan(1000);
  });
});

describe('sortLedger / filterLedger', () => {
  it('sorts on a copy, stably, in both directions', () => {
    // The artifact is shared through the module-level cache: a sort in place
    // would reorder the ledger under every other panel reading it.
    const before = [...artifact.ledger];
    const asc = sortLedger(artifact.ledger, 'cash_delta', 'asc');
    const desc = sortLedger(artifact.ledger, 'cash_delta', 'desc');
    expect(artifact.ledger).toEqual(before);
    expect(artifact.ledger[0]).toBe(before[0]);
    expect(asc[0].cash_delta).toBeLessThanOrEqual(asc[asc.length - 1].cash_delta);
    expect(desc[0].cash_delta).toBe(asc[asc.length - 1].cash_delta);
    expect(asc).toHaveLength(72);
  });

  it('keeps the engine’s event order as the tiebreak within a date', () => {
    const sorted = sortLedger(artifact.ledger, 'date', 'asc');
    const sameDay = artifact.ledger.filter((e) => e.date === sorted[0].date);
    expect(sorted.slice(0, sameDay.length)).toEqual(sameDay);
  });

  it('filters by kind and by free text over kind/underlying/contract/detail', () => {
    expect(filterLedger(artifact.ledger, { kinds: ['sell_put_open'] })).toHaveLength(23);
    expect(filterLedger(artifact.ledger, { kinds: [] })).toHaveLength(72);
    expect(filterLedger(artifact.ledger, { text: 'GOOGL' })).toHaveLength(72);
    expect(filterLedger(artifact.ledger, { text: 'no-such-thing' })).toHaveLength(0);
    expect(
      filterLedger(artifact.ledger, { kinds: ['sell_call_open'], text: 'C00' }).length,
    ).toBeGreaterThan(0);
  });
});
