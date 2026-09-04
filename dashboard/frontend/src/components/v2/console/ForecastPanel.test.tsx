// FC-096 Phase E PR-3, component 7: what the forecast panel REFUSES to do.
//
// The arithmetic is pinned in `forecastRows.test.ts`; these are the panel's own
// rules, and all three are refusals: no caveat ⇒ no range at all, a server
// refusal printed verbatim instead of a number, and a portfolio sum that names
// its constituency with `n_summed` rather than borrowing the arm's
// `n_included`.

import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import type { SweepReport } from '../../../types/v2';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import ForecastPanel from './ForecastPanel';

const report = normaliseSweepDetail(shaped13cc)!.results!;

const show = (over: Partial<SweepReport> = {}, scenario = 'base', symbol = 'GOOGL') =>
  render(<ForecastPanel report={{ ...report, ...over }} scenario={scenario} symbol={symbol} />);

describe('ForecastPanel — the caveat is not optional', () => {
  it('renders the caveat verbatim beside the ranges', () => {
    show();
    const caveat = screen.getByTestId('forecast-caveat');
    expect(caveat.textContent).toBe(report.forecast_caveat);
    expect(caveat.textContent).toMatch(/NOT a confidence interval/);
    expect(caveat.textContent).toMatch(/this rate can rise while the strategy fails/);
  });

  it('REFUSES to render any range when the caveat is blank', () => {
    // The mutation: drop the guard, or `?? ''` the caveat into a rendered
    // empty string. A range without the paragraph that says it is two
    // run-rates and not a prediction is the artefact the guardrails exist for.
    show({ forecast_caveat: '   ' });
    expect(screen.getByTestId('forecast-no-caveat')).toBeInTheDocument();
    expect(screen.queryByTestId('forecast-range-symbol-net_option_pnl')).toBeNull();
    expect(screen.queryByTestId('forecast-range-symbol-total_pnl')).toBeNull();
    expect(screen.queryByTestId('forecast-symbol')).toBeNull();
  });

  it('lists the run’s known biases under the caveat', () => {
    show();
    expect(screen.getByTestId('forecast-biases').textContent).toContain(
      report.known_biases[0].title,
    );
  });
});

describe('ForecastPanel — refusals', () => {
  it('prints the server’s refusal verbatim and no range at all', () => {
    show({
      forecast: null,
      forecast_refusal:
        'This run was replayed in-sample only, so there is no holdout window to bound a forecast with.',
    });
    expect(screen.getByTestId('forecast-refusal').textContent).toBe(
      'This run was replayed in-sample only, so there is no holdout window to bound a forecast with.',
    );
    expect(screen.queryByTestId('forecast-range-symbol-net_option_pnl')).toBeNull();
    // The caveat still shows — it is what explains why there is nothing here.
    expect(screen.getByTestId('forecast-caveat')).toBeInTheDocument();
  });

  it('names the state that excluded this symbol from its arm', () => {
    show({}, 'position_20pct');
    expect(screen.getByTestId('forecast-symbol-refusal').textContent).toMatch(
      /GOOGL is excluded.*fit: insufficient/,
    );
    expect(screen.getByTestId('forecast-portfolio-refusal').textContent).toBe(
      'No symbol in this arm was measured in BOTH windows, so there is nothing to sum.',
    );
  });
});

describe('ForecastPanel — both bases and the horizon', () => {
  it('renders premium AND total, never one alone', () => {
    show();
    expect(screen.getByTestId('forecast-range-symbol-net_option_pnl')).toBeInTheDocument();
    expect(screen.getByTestId('forecast-range-symbol-total_pnl')).toBeInTheDocument();
    expect(screen.getByTestId('forecast-range-portfolio-net_option_pnl')).toBeInTheDocument();
    expect(screen.getByTestId('forecast-range-symbol-net_option_pnl').textContent).toMatch(
      /Premium \(net option P&L, cash basis\)/,
    );
  });

  it('opens on the HOLDOUT window’s length, not on a year', () => {
    show();
    const holdout = screen.getByTestId('forecast-horizon-90');
    expect(holdout.textContent).toBe('90d (holdout)');
    expect(holdout.className).toMatch(/border-blue-700/);
    expect(screen.queryByTestId('forecast-extrapolated')).toBeNull();
    // 90 days x $16.25-$26.76/day, from the server's own per-day rates.
    expect(screen.getByTestId('forecast-symbol').textContent).toMatch(/\$1,463 – \$2,409/);
  });

  it('says “extrapolated ×N” once the horizon runs past the holdout', () => {
    show();
    fireEvent.click(screen.getByTestId('forecast-horizon-365'));
    expect(screen.getByTestId('forecast-extrapolated').textContent).toMatch(
      /Extrapolated ×4\.1 beyond the holdout window \(90 days\)/,
    );
    expect(screen.getByTestId('forecast-symbol').textContent).toMatch(/\$5,933 – \$9,769/);
  });

  it('labels the portfolio sum with n_summed and counts the arm separately', () => {
    show();
    expect(screen.getByTestId('forecast-n-summed-portfolio-net_option_pnl').textContent).toMatch(
      /summed over 1 symbol/,
    );
    expect(screen.getByTestId('forecast-n-summed-portfolio-total_pnl').textContent).toMatch(
      /summed over 1 symbol/,
    );
    expect(screen.getByTestId('forecast-portfolio-count').textContent).toBe('1 of 1');
    expect(screen.getByTestId('forecast-portfolio').textContent).toMatch(
      /never a comparison between two arms/,
    );
  });
});
