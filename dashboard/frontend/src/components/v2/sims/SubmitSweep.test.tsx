// FC-060 Layer 4 (PR-B): the submit form as the operator meets it.
//
// `specValidation.test.ts` pins the state machine; this pins the WIRING — that
// the button really is gated on it, that holdout really is ON by default, and
// that a server refusal reaches the screen in the server's own words.
//
// `fireEvent` rather than `user-event`: the latter is not a dependency of this
// project and PR-B is not the place to add one.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import SubmitSweep from './SubmitSweep';
import type { SweepAllowlist } from '../../../types/v2';

const ALLOWLIST: SweepAllowlist = {
  allowed: [{ key: 'strategy.put_delta_range', description: 'put delta band [lo, hi]' }],
  rejected: [{ key: 'strategy.put_target_dte', reason: 'cached chains store universe_dte=8.' }],
  presets: [{ name: 'puts_15_25', overrides: { 'strategy.put_delta_range': [0.15, 0.25] } }],
  caps: {
    max_symbols: 12,
    max_scenarios: 20,
    max_cells: 240,
    max_window_days: 730,
    min_holdout_days: 60,
  },
};

let fetchMock: ReturnType<typeof vi.fn>;

const setup = (token = 'a-token') => {
  const onSubmitted = vi.fn();
  render(
    <SubmitSweep
      allowlist={ALLOWLIST}
      allowlistError={null}
      universe={['AAPL', 'NVDA']}
      token={token}
      onTokenChange={vi.fn()}
      onSubmitted={onSubmitted}
    />,
  );
  return { onSubmitted };
};

const submitButton = () => screen.getByTestId('submit-sweep');
const pickSymbol = (sym: string) => fireEvent.click(screen.getByRole('button', { name: sym }));
const addPreset = () => fireEvent.click(screen.getByRole('button', { name: '+ puts_15_25' }));
const setArms = (json: string) =>
  fireEvent.change(screen.getByLabelText('Arms'), { target: { value: json } });

const response = (status: number, body: unknown) => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: '',
  text: async () => JSON.stringify(body),
});

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SubmitSweep — the button is gated on the whole spec', () => {
  it('starts disabled: no symbols and no arms', () => {
    setup();
    expect(submitButton()).toBeDisabled();
  });

  it('becomes enabled only once symbols, an arm and a token are all present', () => {
    setup();
    pickSymbol('AAPL');
    expect(submitButton()).toBeDisabled(); // a symbol alone is not a comparison
    addPreset();
    expect(submitButton()).toBeEnabled();
  });

  it('stays disabled without a token, however complete the spec', () => {
    setup('');
    pickSymbol('AAPL');
    addPreset();
    expect(submitButton()).toBeDisabled();
    expect(screen.getByText(/token is required/i)).toBeInTheDocument();
  });

  it('goes back to disabled when the only arm is removed', () => {
    setup();
    pickSymbol('AAPL');
    addPreset();
    expect(submitButton()).toBeEnabled();
    setArms('[]');
    expect(submitButton()).toBeDisabled();
    expect(screen.getByText(/at least one arm/i)).toBeInTheDocument();
  });
});

describe('SubmitSweep — holdout is on by default', () => {
  it('checks the holdout box and hides the in-sample warning', () => {
    setup();
    expect(screen.getByRole('checkbox', { name: /holdout/i })).toBeChecked();
    expect(screen.queryByTestId('in-sample-warning')).toBeNull();
    // The default holdout start is prefilled, not left for the operator to find.
    expect((screen.getByLabelText('Holdout starts') as HTMLInputElement).value).toMatch(
      /^\d{4}-\d{2}-\d{2}$/,
    );
  });

  it('shows the in-sample warning INLINE the moment the toggle is turned off', () => {
    setup();
    fireEvent.click(screen.getByRole('checkbox', { name: /holdout/i }));
    const warning = screen.getByTestId('in-sample-warning');
    expect(warning.textContent).toMatch(/IN-SAMPLE ONLY/);
    expect(warning.textContent).toMatch(/refuted, not merely unconfirmed/);
  });

  it('halves the cell count when the holdout is off — one split, not two', () => {
    setup();
    pickSymbol('AAPL');
    addPreset();
    expect(screen.getByText(/^4 cells · 2 splits$/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: /holdout/i }));
    expect(screen.getByText(/^2 cells · 1 split$/)).toBeInTheDocument();
  });

  it('refuses a holdout shorter than the cap and says how short', () => {
    setup();
    pickSymbol('AAPL');
    addPreset();
    const end = (screen.getByLabelText('End') as HTMLInputElement).value;
    // Inside the window, but only 10 days of it — legal shape, illegal length.
    const tooLate = new Date(Date.parse(`${end}T00:00:00Z`) - 10 * 86_400_000)
      .toISOString()
      .slice(0, 10);
    fireEvent.change(screen.getByLabelText('Holdout starts'), { target: { value: tooLate } });
    expect(screen.getByText(/under the 60-day minimum/)).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();
  });
});

describe('SubmitSweep — cap violations and rejected keys are shown, not swallowed', () => {
  it('names the symbol cap', () => {
    setup();
    fireEvent.change(screen.getByLabelText('Additional symbols'), {
      target: { value: 'A B C D E F G H I J K L M' },
    });
    expect(screen.getByText(/exceeds the cap of 12/)).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();
  });

  it('names the cell cap with the arithmetic behind it', () => {
    setup();
    fireEvent.change(screen.getByLabelText('Additional symbols'), {
      target: { value: 'A B C D E F G' },
    });
    setArms(
      JSON.stringify(
        Array.from({ length: 19 }, (_, i) => ({
          name: `arm_${i}`,
          overrides: { 'strategy.put_delta_range': [0.15, 0.25] },
        })),
      ),
    );
    expect(screen.getByText(/280 cells .* exceeds the cap of 240/)).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();
  });

  it("quotes the runner's reason for a rejected override key", () => {
    setup();
    setArms('[{"name":"dte","overrides":{"strategy.put_target_dte":14}}]');
    expect(screen.getByText(/cached chains store universe_dte=8/)).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();
  });

  it('reports a JSON syntax error instead of silently ignoring the editor', () => {
    setup();
    pickSymbol('AAPL');
    setArms('[{not json');
    expect(screen.getByText(/Not valid JSON/)).toBeInTheDocument();
    expect(submitButton()).toBeDisabled();
  });
});

describe("SubmitSweep — the server's answer is shown verbatim", () => {
  const fill = () => {
    pickSymbol('AAPL');
    addPreset();
  };

  it('posts once and reports the accepted run_id', async () => {
    fetchMock.mockResolvedValue(response(200, { run_id: 'abc123', status: 'submitted' }));
    const { onSubmitted } = setup();
    fill();
    fireEvent.click(submitButton());
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith('abc123'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('submit-outcome').textContent).toMatch(/abc123/);
  });

  it('selects the ORIGINAL run on a dedup hit — nothing was replayed', async () => {
    fetchMock.mockResolvedValue(
      response(200, { run_id: 'new1', status: 'deduplicated', deduplicated_to: 'old1' }),
    );
    const { onSubmitted } = setup();
    fill();
    fireEvent.click(submitButton());
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith('old1'));
  });

  it('shows a 409 with the run already in flight', async () => {
    fetchMock.mockResolvedValue(
      response(409, { detail: 'a sweep is already running', running_run_id: 'run-old' }),
    );
    setup();
    fill();
    fireEvent.click(submitButton());
    const outcome = await screen.findByTestId('submit-outcome');
    expect(outcome.textContent).toMatch(/already in flight/i);
    expect(outcome.textContent).toMatch(/run-old/);
  });

  it('shows a 422 reason unaltered', async () => {
    const reason = 'strategy.put_target_dte — cached chains store universe_dte=8.';
    fetchMock.mockResolvedValue(response(422, { detail: reason }));
    setup();
    fill();
    fireEvent.click(submitButton());
    expect((await screen.findByTestId('submit-outcome')).textContent).toContain(reason);
  });

  it('shows a 502 grant text unaltered', async () => {
    const grant = 'SA lacks run.jobs.run — grant Cloud Run Invoker on backtest-sweep.';
    fetchMock.mockResolvedValue(response(502, { detail: grant }));
    setup();
    fill();
    fireEvent.click(submitButton());
    expect((await screen.findByTestId('submit-outcome')).textContent).toContain(grant);
  });

  it('shows a 503 as sweeps disabled', async () => {
    fetchMock.mockResolvedValue(response(503, { detail: 'sweeps disabled: no SWEEP_SUBMIT_TOKEN' }));
    setup();
    fill();
    fireEvent.click(submitButton());
    const outcome = await screen.findByTestId('submit-outcome');
    expect(outcome.textContent).toMatch(/Submits are disabled/);
    expect(outcome.textContent).toMatch(/no SWEEP_SUBMIT_TOKEN/);
  });

  it('says a failed submit was NOT retried — it may still have launched', async () => {
    fetchMock.mockRejectedValue(new Error('network down'));
    setup();
    fill();
    fireEvent.click(submitButton());
    const outcome = await screen.findByTestId('submit-outcome');
    expect(outcome.textContent).toMatch(/Nothing was retried/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
