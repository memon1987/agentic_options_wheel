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
  // A REAL rejection: DTE became sweepable in FC-096 Phase A, and a fixture
  // that goes on teaching the retired refusal is the first thing a reader
  // checks and the first thing they find false.
  rejected: [
    {
      key: 'universe.min_open_interest',
      reason: 'the engine hardcodes open_interest: 0, so any floor rejects EVERY call.',
    },
  ],
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

const setup = () => {
  const onSubmitted = vi.fn();
  const onSelectRun = vi.fn();
  render(
    <SubmitSweep
      allowlist={ALLOWLIST}
      allowlistError={null}
      universe={['AAPL', 'NVDA']}
      onSubmitted={onSubmitted}
      onSelectRun={onSelectRun}
    />,
  );
  return { onSubmitted, onSelectRun };
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
  type: 'basic',
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

  it('becomes enabled once symbols and an arm are present — there is no third thing', () => {
    setup();
    pickSymbol('AAPL');
    expect(submitButton()).toBeDisabled(); // a symbol alone is not a comparison
    addPreset();
    // FC-096 Phase D PR-2: a submit token used to be required here. The button
    // must now enable WITHOUT one, or the form is unusable post-retirement.
    expect(submitButton()).toBeEnabled();
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

describe('SubmitSweep — the submit token is gone (FC-096 Phase D PR-2)', () => {
  it('renders no credential field at all', () => {
    setup();
    // Not merely optional — ABSENT. A password box labelled "Submit token" on a
    // page behind IAP invites the operator to paste a secret that nothing reads.
    expect(screen.queryByLabelText(/submit token/i)).toBeNull();
    expect(screen.queryByPlaceholderText(/SWEEP_SUBMIT_TOKEN/)).toBeNull();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByText(/token is required/i)).toBeNull();
  });

  it('posts with NO Authorization header and WITH X-Requested-With', async () => {
    fetchMock.mockResolvedValue(response(200, { run_id: 'abc', status: 'submitted' }));
    const { onSubmitted } = setup();
    pickSymbol('AAPL');
    addPreset();
    fireEvent.click(submitButton());
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith('abc'));
    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers.Authorization).toBeUndefined();
    expect(init.headers['X-Requested-With']).toBe('XMLHttpRequest');
  });

  it('renders the session-expired state on a 401, with a reload action', async () => {
    fetchMock.mockResolvedValue(response(401, { detail: 'no IAP assertion on this request' }));
    setup();
    pickSymbol('AAPL');
    addPreset();
    fireEvent.click(submitButton());
    const outcome = await screen.findByTestId('submit-outcome');
    expect(screen.getByTestId('session-expired').textContent).toMatch(/Session expired/i);
    expect(outcome.textContent).toMatch(/reload/i);
    expect(outcome.textContent).toMatch(/Nothing was submitted/);
    // Not dressed as a spec problem: the operator must not go hunting the form.
    expect(outcome.textContent).not.toMatch(/token/i);
  });

  it('renders a 403 as an allowlist refusal, in the server’s words, NOT as an expiry', async () => {
    const detail =
      'viewer@example.com is signed in and may read everything, but writes are limited to OPERATORS.';
    fetchMock.mockResolvedValue(response(403, { detail }));
    setup();
    pickSymbol('AAPL');
    addPreset();
    fireEvent.click(submitButton());
    const outcome = await screen.findByTestId('submit-outcome');
    expect(screen.queryByTestId('session-expired')).toBeNull();
    expect(outcome.textContent).toMatch(/not an operator/i);
    expect(outcome.textContent).toContain(detail);
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
    setArms('[{"name":"oi","overrides":{"universe.min_open_interest":1}}]');
    expect(screen.getByText(/hardcodes open_interest: 0/)).toBeInTheDocument();
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

  it('offers prior_done_run_id as a HINT and still selects the NEW run', async () => {
    // 202, `deduplicated_to: null`, `prior_done_run_id` set. The launch happened
    // — only the Job, which alone sees its effective config, decides dedup — so
    // nothing may auto-redirect away from the run just submitted.
    fetchMock.mockResolvedValue(
      response(202, {
        run_id: 'new1',
        status: 'submitted',
        deduplicated_to: null,
        prior_done_run_id: 'old1',
      }),
    );
    const { onSubmitted, onSelectRun } = setup();
    fill();
    fireEvent.click(submitButton());

    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith('new1'));
    const hint = screen.getByTestId('prior-done-hint');
    expect(hint.textContent).toMatch(/An identical sweep already completed as run/);
    expect(hint.textContent).toMatch(/launched anyway; the Job may mark it deduplicated/);
    // Neutral: this is information, not a warning and not a success.
    expect(hint.className).not.toMatch(/text-green|text-yellow|text-red/);

    // Clicking opens the prior run, without disturbing what was selected.
    expect(onSelectRun).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('prior-done-link'));
    expect(onSelectRun).toHaveBeenCalledWith('old1');
    expect(onSubmitted).toHaveBeenCalledTimes(1);
  });

  it('renders no prior-run notice when the 202 carries none', async () => {
    fetchMock.mockResolvedValue(
      response(202, { run_id: 'new2', status: 'submitted', deduplicated_to: null }),
    );
    const { onSubmitted } = setup();
    fill();
    fireEvent.click(submitButton());
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith('new2'));
    expect(screen.queryByTestId('prior-done-hint')).toBeNull();
    expect(screen.getByTestId('submit-outcome').textContent).toMatch(/Submitted as new2/);
  });

  it('shows a 409 with the server detail, which names the run in flight', async () => {
    fetchMock.mockResolvedValue(
      response(409, { detail: 'sweep run-old is running; one sweep runs at a time.' }),
    );
    setup();
    fill();
    fireEvent.click(submitButton());
    const outcome = await screen.findByTestId('submit-outcome');
    expect(outcome.textContent).toMatch(/already in flight/i);
    expect(outcome.textContent).toContain('sweep run-old is running; one sweep runs at a time.');
  });

  it('shows a 422 reason unaltered', async () => {
    const reason = 'universe.min_open_interest — the engine hardcodes open_interest: 0.';
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

  it('shows a 503 with the API’s own reason', async () => {
    fetchMock.mockResolvedValue(
      response(503, { detail: 'the scenario_sweeps tables do not exist yet' }),
    );
    setup();
    fill();
    fireEvent.click(submitButton());
    const outcome = await screen.findByTestId('submit-outcome');
    expect(outcome.textContent).toMatch(/Submits are unavailable/);
    expect(outcome.textContent).toMatch(/tables do not exist yet/);
  });

  it('submits on Enter from a field, not only on the button', async () => {
    fetchMock.mockResolvedValue(response(200, { run_id: 'entered', status: 'submitted' }));
    const { onSubmitted } = setup();
    fill();
    fireEvent.submit(submitButton().closest('form')!);
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith('entered'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
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
