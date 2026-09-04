// FC-096 Phase E PR-3, component 3: equity vs base vs buy-and-hold.
//
// All three lines are in DOLLARS on the same capital base, and all three are
// the engine's own: the cell's `daily.equity`, the base arm's `daily.equity`,
// and the buy-and-hold curve the engine built from the SCORED benchmark and
// stored in the sidecar. Nothing here is rebased, rescaled or re-derived.
//
// The header numbers are the ROW's — total return, benchmark return, excess —
// so the picture and the grid above it cannot disagree. An unmeasured cell gets
// its state badge's words instead of numbers, per the guardrail table.

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { SweepResultRow } from '../../../types/v2';
import { fmtCurrency, fmtDateShort } from '../../../utils/format';
import { pctOrDash, renderCell } from '../sims/resultCells';
import type { EquityOverlayResult } from './series';

export interface EquityChartProps {
  overlay: EquityOverlayResult;
  row: SweepResultRow | null;
  /** True when the selected cell IS base — there is no second curve to draw. */
  isBase: boolean;
  /** The base cell's absence, when its artifact could not be read. */
  baseAbsence: string | null;
  artifactAbsence: string | null;
  capitalBase: number | null;
}

export default function EquityChart({
  overlay,
  row,
  isBase,
  baseAbsence,
  artifactAbsence,
  capitalBase,
}: EquityChartProps) {
  const measured = renderCell(row).kind === 'return';

  if (overlay.rows.length === 0) {
    return (
      <section data-testid="equity-chart" className="rounded-lg border border-gray-700 bg-gray-800 p-5">
        <h3 className="text-base font-semibold text-white">Equity vs base vs buy &amp; hold</h3>
        <p data-testid="equity-absent" className="text-sm text-gray-400 mt-2">
          {artifactAbsence ?? 'No daily state was stored for this cell.'}
        </p>
      </section>
    );
  }

  return (
    <section data-testid="equity-chart" className="rounded-lg border border-gray-700 bg-gray-800 p-5">
      <h3 className="text-base font-semibold text-white">Equity vs base vs buy &amp; hold</h3>
      <p className="text-xs text-gray-500 mt-1 mb-3">
        {measured ? (
          <span data-testid="equity-header-numbers">
            Total return {pctOrDash(row?.total_return)} · buy &amp; hold{' '}
            {pctOrDash(row?.benchmark_return)} · excess {pctOrDash(row?.excess_return)} — the
            engine&rsquo;s numbers, from the grid cell.
          </span>
        ) : (
          <span data-testid="equity-header-unmeasured">{renderCell(row).title}</span>
        )}
        {capitalBase !== null && (
          <span className="block">
            Dollars, on a capital base of {fmtCurrency(capitalBase)}.
          </span>
        )}
      </p>

      {overlay.benchmarkMismatch && (
        <p data-testid="equity-benchmark-mismatch" className="text-xs text-red-400 mb-2">
          {overlay.benchmarkMismatch}
        </p>
      )}
      {overlay.benchmarkUnverified && (
        <p data-testid="equity-benchmark-unverified" className="text-xs text-amber-400 mb-2">
          This cell carries no benchmark stamp to cross-check the curve against, so the buy-and-hold
          line below is drawn unverified.
        </p>
      )}
      {!overlay.hasBenchmark && !overlay.benchmarkMismatch && (
        <p data-testid="equity-no-benchmark" className="text-xs text-gray-500 mb-2">
          No buy-and-hold curve is stored for this window; the row&rsquo;s benchmark return{' '}
          {pctOrDash(row?.benchmark_return)} still stands.
        </p>
      )}
      {!overlay.hasBase && !isBase && (
        <p data-testid="equity-no-base" className="text-xs text-gray-500 mb-2">
          Base overlay omitted — {baseAbsence ?? 'the base cell&rsquo;s artifact is not loaded.'}
        </p>
      )}

      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={overlay.rows} margin={{ top: 5, right: 5, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis
              dataKey="date"
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              tickFormatter={fmtDateShort}
              minTickGap={28}
            />
            <YAxis
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              domain={['auto', 'auto']}
              tickFormatter={(n: number) => fmtCurrency(n, { compact: true })}
            />
            <Tooltip
              contentStyle={{ background: '#1f2937', border: '1px solid #374151', color: '#f3f4f6' }}
              labelFormatter={(label) => fmtDateShort(label as string)}
              formatter={(value: number, name: string) => [fmtCurrency(value), name]}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Line
              type="monotone"
              dataKey="equity"
              name="This arm"
              stroke="#60a5fa"
              strokeWidth={2}
              dot={false}
              connectNulls
            />
            {overlay.hasBase && (
              <Line
                type="monotone"
                dataKey="base"
                name="Base arm"
                stroke="#a78bfa"
                strokeWidth={1.5}
                strokeDasharray="5 3"
                dot={false}
                connectNulls
              />
            )}
            {overlay.hasBenchmark && (
              <Line
                type="monotone"
                dataKey="benchmark"
                name="Buy & hold (engine)"
                stroke="#9ca3af"
                strokeWidth={1.5}
                strokeDasharray="2 3"
                dot={false}
                connectNulls
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="text-xs text-gray-500 mt-2">
        Buy &amp; hold is the engine&rsquo;s own curve: whole shares bought at the first close and
        held to the last, dividends included — the remainder stays idle cash.
      </p>
    </section>
  );
}
