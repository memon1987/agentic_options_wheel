// FC-096 Phase E PR-3, component 3: drawdown from the running peak.
//
// The HEADER number is the ROW's `max_drawdown` — the engine's, from the grid
// cell. The series' minimum equals it (pinned in `series.test.ts`) and is never
// printed as a second number: two numbers for one quantity invite the reader to
// reconcile them, and the one they would trust is whichever is worse.

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { SweepResultRow } from '../../../types/v2';
import { fmtDateShort, fmtPercent } from '../../../utils/format';
import { pctOrDash, renderCell } from '../sims/resultCells';
import type { DrawdownPoint } from './series';
import StrategyBanner from './StrategyBanner';

export interface DrawdownChartProps {
  series: DrawdownPoint[];
  row: SweepResultRow | null;
  absence: string | null;
  strategy?: string;
}

export default function DrawdownChart({
  series,
  row,
  absence,
  strategy = 'wheel',
}: DrawdownChartProps) {
  const measured = renderCell(row).kind === 'return';

  if (series.length === 0) {
    return (
      <section
        data-testid="drawdown-chart"
        className="rounded-lg border border-gray-700 bg-gray-800 p-5"
      >
        <h3 className="text-base font-semibold text-white">Drawdown</h3>
        <p data-testid="drawdown-absent" className="text-sm text-gray-400 mt-2">
          {absence ?? 'No daily state was stored for this cell.'}
        </p>
      </section>
    );
  }

  return (
    <section
      data-testid="drawdown-chart"
      className="rounded-lg border border-gray-700 bg-gray-800 p-5"
    >
      <h3 className="text-base font-semibold text-white">Drawdown</h3>
      <StrategyBanner strategy={strategy} />
      <p className="text-xs text-gray-500 mt-1 mb-3">
        {measured ? (
          <span data-testid="drawdown-header">
            Max drawdown {pctOrDash(row?.max_drawdown)} — the engine&rsquo;s number, from the grid
            cell.
          </span>
        ) : (
          <span data-testid="drawdown-unmeasured">{renderCell(row).title}</span>
        )}{' '}
        Measured from the running peak of equity over {series.length} decision days.
      </p>
      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={series} margin={{ top: 5, right: 5, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis
              dataKey="date"
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              tickFormatter={fmtDateShort}
              minTickGap={28}
            />
            <YAxis
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              tickFormatter={(n: number) => fmtPercent(n, 1)}
            />
            <Tooltip
              contentStyle={{ background: '#1f2937', border: '1px solid #374151', color: '#f3f4f6' }}
              labelFormatter={(label) => fmtDateShort(label as string)}
              formatter={(value: number) => [fmtPercent(value, 2), 'Drawdown']}
            />
            <Area
              type="monotone"
              dataKey="drawdown"
              name="Drawdown"
              stroke="#f87171"
              fill="#7f1d1d"
              fillOpacity={0.4}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}
