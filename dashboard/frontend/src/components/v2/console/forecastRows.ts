// FC-096 Phase E PR-3 (§Forecast, component 7): horizon scaling, and nothing
// else.
//
// Every rate in here was computed by the SERVER (`shape_results`, PR-1). This
// module multiplies a per-calendar-day rate by a horizon and picks the labels.
// It does not derive a rate, does not fill in a missing one, and does not turn
// a `null` into a `0` — a NULL input means the engine had no number, and a zero
// on a forecast panel reads as "we measured nothing happening", which is the
// opposite claim.
//
// Two things here are corrections the PR-1 build asked for in writing:
//
//   * **A portfolio sum is labelled with `n_summed`, not `n_included`.** They
//     differ: `included` is the symbol set measured in BOTH windows, while a
//     BASIS can still drop one of those symbols (a covered-call row with no
//     stamped capital base has no `total_pnl` rate to sum). `n_summed` is how
//     many symbols went into THIS sum; printing `n_included` beside it would
//     overstate the constituency of the number on screen.
//   * **Per-basis exclusions are named** from `excluded_by_basis`, merged with
//     the arm-level `excluded` so the panel can name every symbol that is not
//     in the sum it is standing next to.

import type { SimForecast, SimForecastBasis } from '../../../types/v2';

export type ForecastBasisKey = 'net_option_pnl' | 'total_pnl';

export const BASIS_ORDER: ForecastBasisKey[] = ['net_option_pnl', 'total_pnl'];

export const BASIS_LABEL: Record<ForecastBasisKey, string> = {
  net_option_pnl: 'Premium (net option P&L, cash basis)',
  total_pnl: 'Total P&L (incl. the stock leg, marked at the last close)',
};

export interface ForecastRange {
  basis: ForecastBasisKey;
  label: string;
  /** `low_per_day × horizon`. `null` when the server served no rate. */
  low: number | null;
  high: number | null;
  lowPerDay: number | null;
  highPerDay: number | null;
  fitPerDay: number | null;
  holdoutPerDay: number | null;
  /** Portfolio rows only: HOW MANY symbols this sum is over. Never `n_included`. */
  nSummed: number | null;
  /** Symbols left out of THIS basis' sum, named. Portfolio rows only. */
  excluded: Array<{ symbol: string; reason: string }>;
  /**
   * Why this basis has no rate, when it has none (review round 1, R6).
   *
   * `null` when a rate was served. A suppressed basis is not a server fault and
   * must not read as one: the covered-call case is a symbol with a premium rate
   * and no total-P&L rate because no capital base was stamped, and the server
   * says so in `excluded_by_basis`.
   */
  suppression: string | null;
}

export interface ForecastView {
  /** False ⇒ nothing to render but the refusal. */
  available: boolean;
  /** The server's refusal, or this module's reason. Rendered verbatim. */
  refusal: string | null;
  horizonDays: number;
  defaultHorizonDays: number;
  horizonChoices: number[];
  holdoutDays: number | null;
  /**
   * `horizon / holdout days` when the horizon runs PAST the window the rates
   * were measured over; `null` when it does not. The panel prints "extrapolated
   * ×N" off this — the honest reading of a 365-day number built from 90 days.
   */
  extrapolationFactor: number | null;
  symbol: {
    symbol: string;
    capitalBase: number | null;
    fill: { basis: string | null; fill_haircut: number | null; is_engine_default?: boolean } | null;
    days: { fit: number | null; holdout: number | null };
    ranges: ForecastRange[];
  } | null;
  portfolio: {
    included: string[];
    nSymbols: number;
    /** The server's own refusal for the sum, when it refused. */
    refusal: string | null;
    ranges: ForecastRange[];
  } | null;
}

const scale = (perDay: number | null | undefined, horizon: number): number | null =>
  perDay === null || perDay === undefined || !Number.isFinite(perDay) ? null : perDay * horizon;

function rangeFrom(
  basis: ForecastBasisKey,
  served: SimForecastBasis | null | undefined,
  horizon: number,
  excluded: Array<{ symbol: string; reason: string }>,
  suppression: string | null = null,
): ForecastRange {
  return {
    suppression: served?.low_per_day === null || served?.low_per_day === undefined
      ? (suppression ?? 'no rate served for this basis')
      : null,
    basis,
    label: BASIS_LABEL[basis],
    low: scale(served?.low_per_day, horizon),
    high: scale(served?.high_per_day, horizon),
    lowPerDay: served?.low_per_day ?? null,
    highPerDay: served?.high_per_day ?? null,
    fitPerDay: served?.fit_per_day ?? null,
    holdoutPerDay: served?.holdout_per_day ?? null,
    // `n_summed`, and nothing else, ever. `n_included` is a different count.
    nSummed: served?.n_summed ?? null,
    excluded,
  };
}

/** The horizon a panel opens on: the holdout window's own length. */
export const defaultHorizon = (forecast: SimForecast | null): number =>
  forecast?.default_horizon_days ?? forecast?.days?.holdout ?? 0;

/**
 * One arm/symbol's forecast, scaled to `horizonDays`.
 *
 * `null` forecast, an arm the server did not forecast, or a symbol it excluded
 * all produce `available: false` with the reason — an in-sample run's refusal
 * comes from the report and is passed in by the panel, and everything else is
 * named here.
 */
export function forecastRows(
  forecast: SimForecast | null,
  scenario: string,
  symbol: string,
  horizonDays: number,
): ForecastView {
  const holdoutDays = forecast?.days?.holdout ?? null;
  const base: ForecastView = {
    available: false,
    refusal: null,
    horizonDays,
    defaultHorizonDays: defaultHorizon(forecast),
    horizonChoices: forecast?.horizon_choices ?? [],
    holdoutDays,
    extrapolationFactor:
      holdoutDays && holdoutDays > 0 && horizonDays > holdoutDays
        ? horizonDays / holdoutDays
        : null,
    symbol: null,
    portfolio: null,
  };

  if (!forecast) {
    return { ...base, refusal: 'This run carries no forecast.' };
  }
  const arm = forecast.by_scenario?.[scenario];
  if (!arm) {
    return {
      ...base,
      refusal: `No forecast was served for arm “${scenario}”: the server forecasts only arms with a measured fit AND holdout cell.`,
    };
  }

  const portfolio = arm.portfolio;
  const byBasis = portfolio?.excluded_by_basis ?? {};
  const armExcluded = portfolio?.excluded ?? {};
  const excludedFor = (basis: ForecastBasisKey): Array<{ symbol: string; reason: string }> => {
    const merged = new Map<string, string>();
    for (const [name, reason] of Object.entries(armExcluded)) merged.set(name, reason);
    for (const [name, reason] of Object.entries(byBasis[basis] ?? {})) merged.set(name, reason);
    return [...merged.entries()]
      .map(([name, reason]) => ({ symbol: name, reason }))
      .sort((a, b) => a.symbol.localeCompare(b.symbol));
  };

  const symbolForecast = arm.symbols?.[symbol] ?? null;
  const symbolView = symbolForecast
    ? {
        symbol,
        capitalBase: symbolForecast.capital_base,
        fill: symbolForecast.fill ?? null,
        days: symbolForecast.days,
        ranges: BASIS_ORDER.map((basis) =>
          rangeFrom(
            basis,
            symbolForecast[basis],
            horizonDays,
            [],
            byBasis[basis]?.[symbol] ??
              (symbolForecast.capital_base === null && basis === 'total_pnl'
                ? 'suppressed: no stamped capital base on this cell, so total P&L has no denominator'
                : null),
          ),
        ),
      }
    : null;

  const portfolioView = portfolio
    ? {
        included: portfolio.included ?? [],
        nSymbols: portfolio.n_symbols ?? 0,
        refusal: portfolio.refusal ?? null,
        ranges: BASIS_ORDER.map((basis) =>
          rangeFrom(basis, portfolio[basis], horizonDays, excludedFor(basis)),
        ),
      }
    : null;

  return {
    ...base,
    available: !!symbolView || !!portfolioView,
    refusal:
      !symbolView && armExcluded[symbol]
        ? `${symbol} is excluded from this arm's forecast — ${armExcluded[symbol]}.`
        : null,
    symbol: symbolView,
    portfolio: portfolioView,
  };
}
