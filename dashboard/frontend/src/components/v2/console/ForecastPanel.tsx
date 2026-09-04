// FC-096 Phase E PR-3, component 7: the forecast RANGE.
//
// Three rules, each of which the panel enforces rather than merely documents:
//
//   1. **The caveat is mandatory.** No caveat, no panel. It is the paragraph
//      that says these are two run-rates and not a confidence interval, that
//      the premium basis is cash-basis and can RISE while the strategy fails,
//      and that an in-sample run has no forecast at all. A range shown without
//      it is the exact artefact the FC-060 guardrails exist to prevent, so a
//      blank `forecast_caveat` makes this component refuse.
//   2. **Both bases, always.** On the captured run the premium rate rises fit ->
//      holdout ($5.9k -> $9.8k/yr) while total P&L falls ($9.0k -> $6.2k/yr):
//      the wheel writes MORE calls on assigned shares while the position bleeds.
//      Rendering premium alone would show that as improvement.
//   3. **A portfolio sum is labelled with `n_summed`.** Not `n_included` — a
//      basis can drop a symbol the arm still counts as included. And there is
//      NO arm-vs-arm portfolio comparison anywhere: two arms' `included` sets
//      differ, so their sums are over different symbol sets.
//
// Everything numeric is the server's per-day rate multiplied by the horizon.
// This component does no other arithmetic.

import { useState } from 'react';
import type { SweepReport } from '../../../types/v2';
import { fmtCurrency } from '../../../utils/format';
import { defaultHorizon, forecastRows, type ForecastRange } from './forecastRows';

export interface ForecastPanelProps {
  report: SweepReport;
  scenario: string;
  symbol: string;
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <section
      data-testid="forecast-panel"
      className="rounded-lg border border-gray-700 bg-gray-800 p-5"
    >
      <h3 className="text-base font-semibold text-white">Forecast range</h3>
      {children}
    </section>
  );
}

function RangeRow({
  range,
  horizon,
  scope,
}: {
  range: ForecastRange;
  horizon: number;
  /** `symbol` or `portfolio` — the two ranges are never interchangeable. */
  scope: 'symbol' | 'portfolio';
}) {
  return (
    <div data-testid={`forecast-range-${scope}-${range.basis}`} className="py-2 border-t border-gray-700/60">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <span className="text-xs text-gray-400">{range.label}</span>
        <span className="text-lg font-semibold text-gray-200">
          {range.low === null || range.high === null ? (
            <span data-testid={`forecast-null-${scope}-${range.basis}`}>not served</span>
          ) : (
            `${fmtCurrency(range.low)} – ${fmtCurrency(range.high)}`
          )}
        </span>
      </div>
      <div className="text-[11px] text-gray-500">
        over {horizon} days
        {range.fitPerDay !== null && range.holdoutPerDay !== null && (
          <>
            {' '}
            · fit {fmtCurrency(range.fitPerDay)}/day, holdout{' '}
            {fmtCurrency(range.holdoutPerDay)}/day
          </>
        )}
        {range.nSummed !== null && (
          <span data-testid={`forecast-n-summed-${scope}-${range.basis}`}>
            {' '}
            · summed over {range.nSummed} symbol{range.nSummed === 1 ? '' : 's'}
          </span>
        )}
      </div>
      {range.excluded.length > 0 && (
        <div data-testid={`forecast-excluded-${scope}-${range.basis}`} className="text-[11px] text-gray-500">
          not in this sum:{' '}
          {range.excluded.map((e) => `${e.symbol} (${e.reason})`).join(', ')}
        </div>
      )}
    </div>
  );
}

export default function ForecastPanel({ report, scenario, symbol }: ForecastPanelProps) {
  const forecast = report.forecast;
  const [horizon, setHorizon] = useState<number | null>(null);
  const caveat = (report.forecast_caveat ?? '').trim();

  // Rule 1, first and unconditionally: a range without its caveat is not shown.
  if (!caveat) {
    return (
      <Shell>
        <p data-testid="forecast-no-caveat" className="text-sm text-amber-400 mt-2">
          This run served no forecast caveat, so no range is rendered. The caveat is what says the
          bounds are two run-rates rather than a confidence interval; a range without it would be
          read as a prediction.
        </p>
      </Shell>
    );
  }

  if (!forecast || report.forecast_refusal) {
    return (
      <Shell>
        <p data-testid="forecast-refusal" className="text-sm text-gray-300 mt-2 whitespace-pre-wrap">
          {report.forecast_refusal ?? 'This run carries no forecast.'}
        </p>
        <p data-testid="forecast-caveat" className="mt-3 text-[11px] text-gray-500 whitespace-pre-wrap">
          {caveat}
        </p>
      </Shell>
    );
  }

  const chosen = horizon ?? defaultHorizon(forecast);
  const view = forecastRows(forecast, scenario, symbol, chosen);
  const choices = view.horizonChoices.includes(view.defaultHorizonDays)
    ? view.horizonChoices
    : [view.defaultHorizonDays, ...view.horizonChoices];

  return (
    <Shell>
      <div className="flex items-center gap-2 mt-2 flex-wrap">
        <span className="text-xs text-gray-500">Horizon</span>
        {choices.map((days) => (
          <button
            key={days}
            type="button"
            data-testid={`forecast-horizon-${days}`}
            onClick={() => setHorizon(days)}
            className={`px-2 py-1 rounded text-xs border ${
              days === chosen
                ? 'bg-blue-950/60 border-blue-700 text-blue-200'
                : 'bg-gray-800 border-gray-700 text-gray-400 hover:text-gray-200'
            }`}
          >
            {days}d{days === view.defaultHorizonDays ? ' (holdout)' : ''}
          </button>
        ))}
      </div>

      {view.extrapolationFactor !== null && (
        <p data-testid="forecast-extrapolated" className="text-xs text-amber-400 mt-2">
          Extrapolated ×{view.extrapolationFactor.toFixed(1)} beyond the holdout window (
          {view.holdoutDays} days).
        </p>
      )}

      {view.refusal && (
        <p data-testid="forecast-symbol-refusal" className="text-sm text-gray-300 mt-3">
          {view.refusal}
        </p>
      )}

      {view.symbol && (
        <div data-testid="forecast-symbol" className="mt-3">
          <h4 className="text-sm font-semibold text-gray-300">
            {symbol} · {scenario}
          </h4>
          {view.symbol.ranges.map((range) => (
            <RangeRow key={range.basis} range={range} horizon={chosen} scope="symbol" />
          ))}
          <p className="text-[11px] text-gray-500 mt-1">
            Fill: {view.symbol.fill?.basis ?? '—'}
            {view.symbol.fill?.fill_haircut !== null && view.symbol.fill?.fill_haircut !== undefined
              ? ` · haircut ${view.symbol.fill.fill_haircut}${
                  view.symbol.fill.is_engine_default ? ' (engine default)' : ''
                }`
              : ''}
            {view.symbol.capitalBase !== null && (
              <> · capital base {fmtCurrency(view.symbol.capitalBase)}</>
            )}
          </p>
        </div>
      )}

      {view.portfolio && (
        <div data-testid="forecast-portfolio" className="mt-4">
          <h4 className="text-sm font-semibold text-gray-300">
            All symbols in this arm —{' '}
            <span data-testid="forecast-portfolio-count">
              {view.portfolio.included.length} of {view.portfolio.nSymbols}
            </span>{' '}
            measured in both windows
          </h4>
          {view.portfolio.refusal ? (
            <p data-testid="forecast-portfolio-refusal" className="text-sm text-gray-300 mt-1">
              {view.portfolio.refusal}
            </p>
          ) : (
            view.portfolio.ranges.map((range) => (
              <RangeRow key={range.basis} range={range} horizon={chosen} scope="portfolio" />
            ))
          )}
          <p className="text-[11px] text-gray-500 mt-1">
            Each symbol is an independent replay on its own capital, so this is a sum over the
            symbols named — never a comparison between two arms, whose measured sets differ.
          </p>
        </div>
      )}

      <p
        data-testid="forecast-caveat"
        className="mt-4 text-[11px] text-gray-500 whitespace-pre-wrap border-t border-gray-700 pt-2"
      >
        {caveat}
      </p>
      {report.known_biases.length > 0 && (
        <ul data-testid="forecast-biases" className="mt-2 text-[11px] text-gray-500 list-disc pl-4">
          {report.known_biases.map((bias, i) => (
            <li key={i}>{bias.title}</li>
          ))}
        </ul>
      )}
    </Shell>
  );
}
