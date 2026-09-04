// FC-096 Phase E PR-3, component 3: how much of the account was working.
//
// Reserved put collateral and share value, stacked, over the stamped capital
// base. This is a DIGEST series (§D-4): it is grey, it is NEVER coloured by
// sign, it never appears in a Δ, and it is not compared with anything —
// including with the RoC denominator on the strip, which is a different mean
// over a different set of days and says so there.
//
// Suppressed entirely without a stamped capital base. A covered-call cell
// divided by the wheel's starting cash would print a plausible, wrong
// percentage under a correct-looking label, and a label is not a denominator.

import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { fmtCurrency, fmtDateShort, fmtPercent } from '../../../utils/format';
import type { DeploymentPoint, DeploymentReading } from './series';
import StrategyBanner from './StrategyBanner';

export interface DeploymentChartProps {
  series: DeploymentPoint[];
  reading: DeploymentReading | null;
  capitalBase: number | null;
  suppressionReason: string | null;
  absence: string | null;
  strategy?: string;
  /**
   * PR-5 (review round 1, R3): the alignment matrix withheld this pair's
   * numbers, so every DOLLAR and RATE this panel would print is suppressed.
   * The curve's shape still belongs to this cell and is still drawn; what goes
   * is the figure a reader would set beside the panel next to it.
   */
  numbersWithheld?: boolean;
}

export default function DeploymentChart({
  series,
  reading,
  capitalBase,
  suppressionReason,
  absence,
  strategy = 'wheel',
  numbersWithheld = false,
}: DeploymentChartProps) {
  if (series.length === 0) {
    return (
      <section
        data-testid="deployment-chart"
        className="rounded-lg border border-gray-700 bg-gray-800 p-5"
      >
        <h3 className="text-base font-semibold text-white">Collateral and deployment</h3>
        <p data-testid="deployment-absent" className="text-sm text-gray-400 mt-2">
          {absence ?? 'No daily state was stored for this cell.'}
        </p>
      </section>
    );
  }

  if (capitalBase === null) {
    return (
      <section
        data-testid="deployment-chart"
        className="rounded-lg border border-gray-700 bg-gray-800 p-5"
      >
        <h3 className="text-base font-semibold text-white">Collateral and deployment</h3>
        <p data-testid="deployment-suppressed" className="text-sm text-gray-400 mt-2">
          {suppressionReason}
        </p>
      </section>
    );
  }

  return (
    <section
      data-testid="deployment-chart"
      className="rounded-lg border border-gray-700 bg-gray-800 p-5"
    >
      <h3 className="text-base font-semibold text-white">Collateral and deployment</h3>
      <StrategyBanner strategy={strategy} />
      <p className="text-xs text-gray-500 mt-1 mb-3">
        Reserved put collateral and share value, stacked, against a capital base of{' '}
        {fmtCurrency(capitalBase)}. From the artifact&rsquo;s daily state —{' '}
        {reading?.basis === 'closes'
          ? 'shares priced at this window’s closes'
          : reading?.hadSidecar
            ? 'shares priced AT COST on the days a holding this window’s sidecar does not price'
            : 'shares priced AT COST (no bars sidecar was stored for this run)'}
        . Display only: never coloured, never compared.
      </p>
      {reading?.unresolvedReason && (
        <p data-testid="deployment-unresolved" className="text-xs text-amber-400 mb-2">
          {reading.unresolvedReason}
        </p>
      )}
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
              tickFormatter={(n: number) => fmtCurrency(n, { compact: true })}
            />
            <Tooltip
              contentStyle={{ background: '#1f2937', border: '1px solid #374151', color: '#f3f4f6' }}
              labelFormatter={(label) => fmtDateShort(label as string)}
              formatter={(value: number, name: string) => [fmtCurrency(value), name]}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {/* Greys, deliberately. A green "deployed" area would read as a
                verdict on a number the engine never scored. */}
            <Area
              type="stepAfter"
              stackId="deployed"
              dataKey="reserved"
              name="Reserved collateral"
              stroke="#9ca3af"
              fill="#4b5563"
              fillOpacity={0.5}
              isAnimationActive={false}
            />
            <Area
              type="stepAfter"
              stackId="deployed"
              dataKey="sharesValue"
              name="Share value"
              stroke="#d1d5db"
              fill="#6b7280"
              fillOpacity={0.5}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      {numbersWithheld && (
        <p data-testid="deployment-withheld" className="text-xs text-gray-500 mt-2">
          The dollar-weighted mean is withheld: the alignment matrix says these two cells are not
          comparable on rates, and a share of capital is a rate.
        </p>
      )}
      {reading && !numbersWithheld && (
        <p data-testid="deployment-mean" className="text-xs text-gray-500 mt-2">
          Dollar-weighted mean over all {reading.days} decision days:{' '}
          {reading.ratio === null ? '—' : fmtPercent(reading.ratio, 1)} of capital
          {reading.ratio !== null && (
            <>
              {' '}
              ({fmtCurrency(reading.reservedMeanDollars)} collateral
              {reading.sharesValueMeanDollars !== null && (
                <> + {fmtCurrency(reading.sharesValueMeanDollars)} shares</>
              )}
              )
            </>
          )}
          . All days, not just the deployed ones — a different question from the strip&rsquo;s
          return on collateral, which averages over deployed days only.
        </p>
      )}
    </section>
  );
}
