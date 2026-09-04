// FC-096 Phase E PR-3, component 3: premium, cumulative and monthly.
//
// `MonthlyPremiumBars` is reused UNCHANGED — the digest emits `MonthlyCashflow`
// rows precisely so this panel needs no second bar chart with its own idea of
// what a month is. The cumulative line beside it is the same event set summed
// by date, so its last point is the sum of the bars by construction.
//
// Both are DIGEST numbers: client-derived, grey, uncoloured, labelled with
// their source, and never compared to anything (§D-4). The engine's own
// `option_pnl` for the cell is on the strip above, from the row.

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  ReferenceLine,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import MonthlyPremiumBars from '../MonthlyPremiumBars';
import { fmtCurrency, fmtDateShort } from '../../../utils/format';
import type { ArtifactDigest } from './artifactDigest';
import StrategyBanner from './StrategyBanner';

export interface PremiumChartsProps {
  digest: ArtifactDigest | null;
  absence: string | null;
  strategy?: string;
  /** The cell's state, for the empty-ledger message (review R9). */
  stateLabel?: string | null;
}

export default function PremiumCharts({
  digest,
  absence,
  strategy = 'wheel',
  stateLabel = null,
}: PremiumChartsProps) {
  if (!digest) {
    return (
      <section
        data-testid="premium-charts"
        className="rounded-lg border border-gray-700 bg-gray-800 p-5"
      >
        <h3 className="text-base font-semibold text-white">Premium: cumulative and monthly</h3>
        <p data-testid="premium-absent" className="text-sm text-gray-400 mt-2">
          {absence ?? 'No ledger was stored for this cell.'}
        </p>
      </section>
    );
  }

  // R9: an artifact with 189 decision days and ZERO ledger events is a real
  // stored object (the live `position_20pct` cell is one). Its chart is an empty
  // AreaChart with axes and no line — a rendering that says "loading" or "broken"
  // rather than "this arm never traded in this window", which is the finding.
  if (digest.netOptionCashSeries.length === 0) {
    return (
      <section
        data-testid="premium-charts"
        className="rounded-lg border border-gray-700 bg-gray-800 p-5"
      >
        <h3 className="text-base font-semibold text-white">Premium: cumulative and monthly</h3>
        <StrategyBanner strategy={strategy} />
        <p data-testid="premium-no-events" className="text-sm text-gray-400 mt-2">
          No option event in this window — the engine opened nothing, so there is no cash flow to
          chart{stateLabel ? ` (this cell is ${stateLabel})` : ''}. The rejection panel below says
          what bound it.
        </p>
      </section>
    );
  }

  return (
    <section data-testid="premium-charts" className="space-y-4">
      <div className="rounded-lg border border-gray-700 bg-gray-800 p-5">
        <h3 className="text-base font-semibold text-white">Cumulative net option cash</h3>
        <StrategyBanner strategy={strategy} />
        <p className="text-xs text-gray-500 mt-1 mb-3">
          Premiums received minus buy-to-close costs, running total by the date the cash moved —
          NET of fees, because the ledger&rsquo;s <span className="font-mono">cash_delta</span> is.
          (The monthly tooltip below shows gross premium and buybacks separately; those two differ
          from the net bar by the fees.) From the ledger — display only, never ranked or compared.
        </p>
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={digest.netOptionCashSeries}
              margin={{ top: 5, right: 5, left: 0, bottom: 5 }}
            >
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis
                dataKey="date"
                tick={{ fill: '#9ca3af', fontSize: 11 }}
                tickFormatter={fmtDateShort}
                minTickGap={28}
              />
              <YAxis
                tick={{ fill: '#9ca3af', fontSize: 11 }}
                tickFormatter={(n: number) => fmtCurrency(n, { compact: true })}
              />
              <Tooltip
                contentStyle={{
                  background: '#1f2937',
                  border: '1px solid #374151',
                  color: '#f3f4f6',
                }}
                labelFormatter={(label) => fmtDateShort(label as string)}
                formatter={(value: number) => [fmtCurrency(value), 'Cumulative']}
              />
              <ReferenceLine y={0} stroke="#6b7280" />
              <Area
                type="monotone"
                dataKey="value"
                name="Cumulative"
                stroke="#9ca3af"
                fill="#4b5563"
                fillOpacity={0.35}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
        <p data-testid="premium-total" className="text-xs text-gray-500 mt-2">
          Ends at {fmtCurrency(digest.netOptionCash)} over{' '}
          {digest.netOptionCashSeries.length} cash-moving days.
        </p>
      </div>

      <MonthlyPremiumBars data={digest.monthly} />
    </section>
  );
}
