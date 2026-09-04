// FC-096 Phase E PR-3, component 5: the wheel cycles the engine materialised.
//
// ENGINE FIELDS ONLY. Every column here is a value `compute_fitness` wrote onto
// the cycle; this table computes nothing, and in particular it prints NO column
// total and NO column mean. `annualized_return` is the one that makes that rule
// load-bearing: it is a per-cycle figure over a 3-day or 40-day holding period,
// and averaging the column would produce a number with no denominator anyone
// could name. The hover says so on the header.
//
// The live `CycleTable.tsx` is deliberately NOT reused: it renders Alpaca-shaped
// cycles from BigQuery with the live P&L conventions, and a replay's cycle is a
// different object with a different provenance. One table serving both would
// have to lie about one of them.

import type { SimArtifact, SimCycle } from '../../../types/v2';
import { fmtCurrency, fmtCurrencyDetail, fmtDateShort } from '../../../utils/format';
import { pctOrDash } from '../sims/resultCells';

export interface SimCycleTableProps {
  artifact: SimArtifact | null;
  absence: string | null;
}

const OUTCOME_CLASS: Record<string, string> = {
  expired_worthless: 'text-gray-300',
  bought_to_close: 'text-red-300',
  assigned: 'text-amber-300',
  called_away: 'text-emerald-300',
};

const cycleTitle = (cycle: SimCycle): string =>
  `${cycle.puts_sold} puts, ${cycle.calls_sold} calls, ${cycle.rolls} rolls, ` +
  `${cycle.event_count} ledger events`;

export default function SimCycleTable({ artifact, absence }: SimCycleTableProps) {
  if (!artifact) {
    return (
      <section data-testid="cycle-table" className="rounded-lg border border-gray-700 bg-gray-800 p-5">
        <h3 className="text-base font-semibold text-white">Wheel cycles</h3>
        <p data-testid="cycle-absent" className="text-sm text-gray-400 mt-2">
          {absence ?? 'No cycles were stored for this cell.'}
        </p>
      </section>
    );
  }

  const cycles = artifact.cycles;
  const open = cycles.filter((c) => c.is_open).length;

  return (
    <section data-testid="cycle-table" className="rounded-lg border border-gray-700 bg-gray-800 p-5">
      <h3 className="text-base font-semibold text-white">Wheel cycles</h3>
      <p className="text-xs text-gray-500 mt-1 mb-3">
        {cycles.length} cycles ({cycles.length - open} completed, {open} open), exactly as{' '}
        <span className="font-mono">compute_fitness</span> materialised them. No column is totalled
        or averaged here.
      </p>
      <div className="overflow-x-auto">
        <table className="min-w-full text-xs">
          <thead>
            <tr className="text-gray-500 uppercase tracking-wide text-[10px] text-left">
              <th className="px-2 py-1">Start</th>
              <th className="px-2 py-1">End</th>
              <th className="px-2 py-1 text-right">Days</th>
              <th className="px-2 py-1">Outcome</th>
              <th className="px-2 py-1 text-right">Cost basis</th>
              <th className="px-2 py-1 text-right">Exit</th>
              <th className="px-2 py-1 text-right">Capital at risk</th>
              <th className="px-2 py-1 text-right">Option P&amp;L</th>
              <th className="px-2 py-1 text-right">Stock P&amp;L</th>
              <th className="px-2 py-1 text-right">Divs</th>
              <th className="px-2 py-1 text-right">Fees</th>
              <th className="px-2 py-1 text-right">Total</th>
              <th className="px-2 py-1 text-right">RoC</th>
              <th
                className="px-2 py-1 text-right"
                data-testid="cycle-annualised-header"
                title="Per cycle; never average this column. Each figure annualises ONE holding period — a 3-day cycle and a 40-day cycle produce numbers with different denominators, and their mean has none."
              >
                Annualised ⓘ
              </th>
            </tr>
          </thead>
          <tbody>
            {cycles.map((cycle, i) => (
              <tr key={`${cycle.start}-${i}`} className="border-t border-gray-700/60" title={cycleTitle(cycle)}>
                <td className="px-2 py-1 text-gray-300 whitespace-nowrap">{fmtDateShort(cycle.start)}</td>
                <td className="px-2 py-1 text-gray-300 whitespace-nowrap">
                  {cycle.end ? (
                    fmtDateShort(cycle.end)
                  ) : (
                    <span className="px-1.5 py-0.5 rounded border border-blue-800/70 bg-blue-950/60 text-blue-300 text-[10px]">
                      open
                    </span>
                  )}
                </td>
                <td className="px-2 py-1 text-right text-gray-300">{cycle.days ?? '—'}</td>
                <td className={`px-2 py-1 ${OUTCOME_CLASS[cycle.outcome ?? ''] ?? 'text-gray-400'}`}>
                  {cycle.outcome ?? '—'}
                </td>
                <td className="px-2 py-1 text-right text-gray-300">
                  {cycle.cost_basis === null ? '—' : fmtCurrencyDetail(cycle.cost_basis)}
                </td>
                <td className="px-2 py-1 text-right text-gray-300">
                  {cycle.exit_price === null ? '—' : fmtCurrencyDetail(cycle.exit_price)}
                </td>
                <td className="px-2 py-1 text-right text-gray-300">
                  {cycle.capital_at_risk === null ? '—' : fmtCurrency(cycle.capital_at_risk)}
                </td>
                <td className="px-2 py-1 text-right text-gray-300">{fmtCurrency(cycle.option_pnl)}</td>
                <td className="px-2 py-1 text-right text-gray-300">{fmtCurrency(cycle.stock_pnl)}</td>
                <td className="px-2 py-1 text-right text-gray-400">{fmtCurrency(cycle.dividends)}</td>
                <td className="px-2 py-1 text-right text-gray-400">{fmtCurrencyDetail(cycle.fees)}</td>
                <td className="px-2 py-1 text-right text-gray-200">{fmtCurrency(cycle.total_pnl)}</td>
                <td className="px-2 py-1 text-right text-gray-300">
                  {pctOrDash(cycle.return_on_capital, 2)}
                </td>
                <td className="px-2 py-1 text-right text-gray-300">
                  {pctOrDash(cycle.annualized_return, 1)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
