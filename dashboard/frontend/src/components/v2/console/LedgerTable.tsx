// FC-096 Phase E PR-3, component 4: the trade ledger, sortable, filterable,
// exportable.
//
// ~150-300 rows for a 730-day cell (72 for the captured 189-day one), so it
// aggregates client-side with no virtualisation (decision 2). The sort and the
// filter are PURE functions in `ledgerCsv.ts` — this component owns the state
// and nothing else — and the export writes exactly the rows on screen, because
// a file that silently differs from the view is how a reader ends up arguing
// with a spreadsheet about what they were looking at.
//
// An unknown `kind` renders with a neutral badge and its raw string. It is
// never dropped and never crashes the table: Phase C's `synthetic_lot_open`
// will land in this build before this build knows the name.

import { useMemo, useState } from 'react';
import type { SimArtifact, SimLedgerEvent } from '../../../types/v2';
import { KNOWN_LEDGER_KINDS } from '../../../types/v2';
import { fmtCurrency, fmtCurrencyDetail, fmtDateShort, parseOcc } from '../../../utils/format';
import {
  exportJson,
  filterLedger,
  ledgerCsv,
  sortLedger,
  type ExportContext,
  type LedgerSortKey,
  type SortDirection,
} from './ledgerCsv';

const KIND_CLASS: Record<string, string> = {
  sell_put_open: 'bg-blue-950/60 text-blue-300 border-blue-800/70',
  sell_call_open: 'bg-violet-950/60 text-violet-300 border-violet-800/70',
  buy_to_close: 'bg-red-950/60 text-red-300 border-red-800/70',
  expire_worthless: 'bg-gray-700/60 text-gray-300 border-gray-600',
  put_assignment: 'bg-amber-950/60 text-amber-300 border-amber-800/70',
  call_assignment: 'bg-emerald-950/60 text-emerald-300 border-emerald-800/70',
  dividend: 'bg-cyan-950/60 text-cyan-300 border-cyan-800/70',
};

const NEUTRAL_KIND = 'bg-gray-800 text-gray-300 border-dashed border-gray-600';

const COLUMNS: Array<{ key: LedgerSortKey; label: string; numeric?: boolean }> = [
  { key: 'date', label: 'Date' },
  { key: 'kind', label: 'Kind' },
  { key: 'symbol', label: 'Contract' },
  { key: 'contracts', label: 'Contracts', numeric: true },
  { key: 'shares', label: 'Shares', numeric: true },
  { key: 'price', label: 'Price', numeric: true },
  { key: 'cash_delta', label: 'Cash Δ', numeric: true },
  { key: 'fees', label: 'Fees', numeric: true },
];

function save(name: string, body: string, type: string): void {
  const blob = new Blob([body], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function contractOf(event: SimLedgerEvent): string {
  const occ = parseOcc(event.symbol);
  if (!occ.optionType) return event.symbol;
  return `${occ.underlying} ${occ.expiration} ${occ.optionType} ${occ.strike?.toFixed(2)}`;
}

export interface LedgerTableProps {
  artifact: SimArtifact | null;
  absence: string | null;
  context: ExportContext;
}

export default function LedgerTable({ artifact, absence, context }: LedgerTableProps) {
  const [sortKey, setSortKey] = useState<LedgerSortKey>('date');
  const [direction, setDirection] = useState<SortDirection>('asc');
  const [kinds, setKinds] = useState<string[]>([]);
  const [text, setText] = useState('');

  // Memoised so the sort and filter below are not re-run on every keystroke
  // against a fresh `[]` identity (eslint react-hooks/exhaustive-deps).
  const ledger = useMemo(() => artifact?.ledger ?? [], [artifact]);
  const kindsPresent = useMemo(() => {
    const seen = new Set(ledger.map((e) => e.kind));
    const known = KNOWN_LEDGER_KINDS.filter((k) => seen.has(k)) as string[];
    const unknown = [...seen].filter((k) => !KNOWN_LEDGER_KINDS.includes(k as never)).sort();
    return [...known, ...unknown];
  }, [ledger]);

  const rows = useMemo(
    () => sortLedger(filterLedger(ledger, { kinds, text }), sortKey, direction),
    [ledger, kinds, text, sortKey, direction],
  );

  if (!artifact) {
    return (
      <section data-testid="ledger-table" className="rounded-lg border border-gray-700 bg-gray-800 p-5">
        <h3 className="text-base font-semibold text-white">Trade ledger</h3>
        <p data-testid="ledger-absent" className="text-sm text-gray-400 mt-2">
          {absence ?? 'No ledger was stored for this cell.'}
        </p>
      </section>
    );
  }

  const toggleKind = (kind: string) =>
    setKinds((prev) => (prev.includes(kind) ? prev.filter((k) => k !== kind) : [...prev, kind]));

  const sortBy = (key: LedgerSortKey) => {
    if (key === sortKey) setDirection(direction === 'asc' ? 'desc' : 'asc');
    else {
      setSortKey(key);
      setDirection('asc');
    }
  };

  return (
    <section data-testid="ledger-table" className="rounded-lg border border-gray-700 bg-gray-800 p-5">
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <h3 className="text-base font-semibold text-white">Trade ledger</h3>
        <div className="flex gap-2">
          <button
            type="button"
            data-testid="ledger-export-csv"
            className="text-xs px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-gray-200"
            onClick={() =>
              save(
                `ledger-${context.runId}-${context.scenario}-${context.symbol}-${context.split}.csv`,
                ledgerCsv(rows, context),
                'text/csv;charset=utf-8',
              )
            }
          >
            Export CSV
          </button>
          <button
            type="button"
            data-testid="ledger-export-json"
            className="text-xs px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-gray-200"
            onClick={() =>
              save(
                `artifact-${context.runId}-${context.scenario}-${context.symbol}-${context.split}.json`,
                JSON.stringify(exportJson(artifact, context), null, 2),
                'application/json',
              )
            }
          >
            Export JSON
          </button>
        </div>
      </div>
      <p className="text-xs text-gray-500 mt-1">
        {rows.length} of {ledger.length} events. Both exports carry this run, arm, symbol, split,
        engine identity and fill basis on every row — a ledger cannot circulate unlabelled.
      </p>

      <div className="flex flex-wrap gap-1 mt-3 items-center">
        {kindsPresent.map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => toggleKind(kind)}
            className={`px-2 py-1 rounded text-[11px] border font-mono ${
              kinds.includes(kind) || kinds.length === 0
                ? (KIND_CLASS[kind] ?? NEUTRAL_KIND)
                : 'bg-gray-900 text-gray-600 border-gray-700'
            }`}
          >
            {kind}
          </button>
        ))}
        <input
          type="search"
          data-testid="ledger-filter-text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="filter…"
          className="ml-2 px-2 py-1 text-xs rounded bg-gray-900 border border-gray-700 text-gray-200"
        />
      </div>

      <div className="overflow-x-auto mt-3">
        <table className="min-w-full text-xs">
          <thead>
            <tr className="text-gray-500 uppercase tracking-wide text-[10px]">
              {COLUMNS.map((column) => (
                <th
                  key={column.key}
                  className={`px-2 py-1 ${column.numeric ? 'text-right' : 'text-left'}`}
                >
                  <button type="button" onClick={() => sortBy(column.key)} className="hover:text-gray-300">
                    {column.label}
                    {sortKey === column.key ? (direction === 'asc' ? ' ▲' : ' ▼') : ''}
                  </button>
                </th>
              ))}
              <th className="px-2 py-1 text-left">Detail</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((event, i) => (
              <tr key={`${event.date}-${i}`} className="border-t border-gray-700/60">
                <td className="px-2 py-1 text-gray-300 whitespace-nowrap">{fmtDateShort(event.date)}</td>
                <td className="px-2 py-1">
                  <span
                    className={`px-1.5 py-0.5 rounded border font-mono text-[10px] ${
                      KIND_CLASS[event.kind] ?? NEUTRAL_KIND
                    }`}
                  >
                    {event.kind}
                  </span>
                </td>
                <td className="px-2 py-1 text-gray-400 font-mono whitespace-nowrap">
                  {contractOf(event)}
                </td>
                <td className="px-2 py-1 text-right text-gray-300">{event.contracts || ''}</td>
                <td className="px-2 py-1 text-right text-gray-300">{event.shares || ''}</td>
                <td className="px-2 py-1 text-right text-gray-300">
                  {event.price === null ? '—' : fmtCurrencyDetail(event.price)}
                </td>
                <td className="px-2 py-1 text-right text-gray-300">{fmtCurrency(event.cash_delta)}</td>
                <td className="px-2 py-1 text-right text-gray-400">
                  {event.fees ? fmtCurrencyDetail(event.fees) : ''}
                </td>
                <td className="px-2 py-1 text-gray-500 font-mono max-w-xs truncate" title={JSON.stringify(event.detail)}>
                  {JSON.stringify(event.detail)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p data-testid="ledger-filtered-empty" className="text-xs text-gray-500 mt-2">
            No event matches this filter. The cell has {ledger.length}.
          </p>
        )}
      </div>
    </section>
  );
}
