// FC-096 Phase E PR-4 — the tweak bar as the operator meets it.
//
// `tweakSpec.test.ts` pins the arithmetic and `simSubmit.test.ts` pins the
// client; this pins the WIRING — that the controls really are typed from the
// allowlist, that the body posted is the diff and not the form, and that every
// status the sim service can answer with reaches the screen as the right thing.
//
// The allowlist and the run row are the REAL captured payloads.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import allowlistFixture from '../../../test/fixtures/sweep_allowlist.json';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import type { SweepAllowlist } from '../../../types/v2';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { resetSessionExpiredSignal } from '../../../hooks/iapSession';
import { submitSim } from '../../../hooks/useSweeps';
import TweakBar, { TWEAK_BASE_ANCHOR } from './TweakBar';
import type { SimRefusal } from './TweakBar';

const allowlist = allowlistFixture as unknown as SweepAllowlist;
const sweep = normaliseSweepDetail(shaped13cc)!.sweep;
const baseEffective = JSON.parse(sweep.base_config_json!).effective as Record<string, unknown>;

let fetchMock: ReturnType<typeof vi.fn>;

/**
 * The bar is a CONTROLLED component now (review round 1, R5): the page owns the
 * request, the in-flight flag and the outcome. `Harness` is the page's half,
 * small enough to keep the wiring under test rather than mocked away.
 */
function Harness(props: Partial<React.ComponentProps<typeof TweakBar>> & {
  onSubmitSpy?: ReturnType<typeof vi.fn>;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [outcome, setOutcome] = useState<SimRefusal | null>(null);
  const [accepted, setAccepted] = useState<string | null>(null);
  return (
    <MemoryRouter>
      <TweakBar
        sweep={sweep}
        allowlist={allowlist}
        allowlistError={null}
        baseEffective={baseEffective}
        scenario="base"
        symbol="GOOGL"
        split="holdout"
        submitting={submitting}
        outcome={outcome}
        onSubmit={(spec, armName) => {
          props.onSubmitSpy?.(spec, armName);
          setSubmitting(true);
          setOutcome(null);
          void submitSim(spec).then((result) => {
            setSubmitting(false);
            if (result.kind === 'accepted' || result.kind === 'deduplicated') {
              setAccepted(`${result.kind}:${result.runId}:${armName}`);
              return;
            }
            setOutcome(result);
          });
        }}
        onClearOutcome={() => setOutcome(null)}
        onOpenCell={props.onOpenCell ?? (() => undefined)}
        {...props}
      />
      {accepted && <span data-testid="harness-accepted">{accepted}</span>}
    </MemoryRouter>
  );
}

const setup = (props: Partial<React.ComponentProps<typeof TweakBar>> = {}) => {
  const onSubmitted = vi.fn();
  render(<Harness onSubmitSpy={onSubmitted} {...props} />);
  return { onSubmitted };
};

const jsonResponse = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const field = (key: string) => screen.getByLabelText(key) as HTMLInputElement;
const submit = () => screen.getByTestId('tweak-submit');
const typeInto = (key: string, value: string) =>
  fireEvent.change(field(key), { target: { value } });

beforeEach(() => {
  resetSessionExpiredSignal();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('the controls', () => {
  it('renders one control per typed key, prefilled from the run’s config', () => {
    setup();
    expect(field('strategy.min_put_premium').value).toBe('0.5');
    expect(field('strategy.put_target_dte').value).toBe('7');
    expect((screen.getByLabelText('earnings.enabled') as HTMLSelectElement).value).toBe('true');
    // A band is TWO inputs, not one comma-separated text box.
    expect((screen.getByLabelText('strategy.put_delta_range low') as HTMLInputElement).value).toBe(
      '0.1',
    );
    expect((screen.getByLabelText('strategy.put_delta_range high') as HTMLInputElement).value).toBe(
      '0.2',
    );
  });

  it('renders NO control for a `symbols` key, and footnotes it instead', () => {
    // The mutation: render one. A text box for `universe.excluded_symbols`
    // posts a string where the runner wants an array (decision 9).
    setup();
    expect(screen.queryByLabelText('universe.excluded_symbols')).toBeNull();
    expect(screen.getByTestId('tweak-symbols-footnote').textContent).toContain(
      'universe.excluded_symbols',
    );
    expect(screen.getByTestId('tweak-symbols-footnote').textContent).toContain('JSON editor');
  });

  it('names a key the run recorded no value for', () => {
    setup();
    const control = screen.getByTestId('tweak-control-universe.max_spread_pct');
    expect(control.textContent).toContain('base value not recorded on this run');
    expect((screen.getByLabelText('universe.max_spread_pct') as HTMLInputElement).value).toBe('');
  });

  it('states the base anchor, verbatim', () => {
    // The mutation: delete the sentence. Without it the operator can read the
    // destination's Δ as "this run's cell minus that run's cell", which it is
    // not — it is that run's arm against that run's own base.
    setup();
    expect(screen.getByTestId('tweak-base-anchor').textContent).toBe(TWEAK_BASE_ANCHOR);
    expect(TWEAK_BASE_ANCHOR).toContain('anchored to base');
  });

  it('says so when the allowlist could not be read', () => {
    setup({ allowlist: null, allowlistError: 'HTTP 503' });
    expect(screen.getByTestId('tweak-allowlist-error').textContent).toContain('HTTP 503');
    expect(screen.queryByTestId('tweak-controls')).toBeNull();
  });
});

describe('the gate before the POST', () => {
  it('is disabled until a field actually changes', () => {
    setup();
    expect(submit()).toBeDisabled();
    expect(screen.getByTestId('tweak-blocking').textContent).toContain('Nothing is changed yet');
    typeInto('strategy.min_put_premium', '0.65');
    expect(submit()).not.toBeDisabled();
  });

  it('refuses an inverted band client-side, in words, without posting', () => {
    setup();
    fireEvent.change(screen.getByLabelText('strategy.put_delta_range low'), {
      target: { value: '0.4' },
    });
    expect(screen.getByTestId('tweak-blocking').textContent).toContain('below its high end');
    expect(submit()).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses a DTE outside the lake’s reach without posting', () => {
    setup();
    typeInto('strategy.put_target_dte', '30');
    expect(screen.getByTestId('tweak-blocking').textContent).toContain('chain lake');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses TWO changed fields and points at the JSON editor', () => {
    setup();
    typeInto('strategy.min_put_premium', '0.65');
    typeInto('strategy.min_call_premium', '0.4');
    expect(screen.getByTestId('tweak-blocking').textContent).toContain('ONE field per arm');
    expect(submit()).toBeDisabled();
  });
});

describe('the POST, and where each answer lands', () => {
  it('posts the DIFF exactly once, carrying the run’s window verbatim', async () => {
    // The two mutations this kills: sending every control's value, and dropping
    // `holdout_start` from the body.
    fetchMock.mockResolvedValue(jsonResponse(202, { run_id: 'new456', cell_count: 4 }));
    setup();
    typeInto('strategy.min_put_premium', '0.65');
    fireEvent.click(submit());
    await waitFor(() => expect(screen.getByTestId('harness-accepted')).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v2/sims/run');
    expect(JSON.parse(init.body)).toEqual({
      symbols: ['GOOGL'],
      start: '2025-09-01',
      end: '2026-09-01',
      holdout_start: '2026-06-03',
      starting_cash: 100000,
      run_sensitivity: false,
      scenarios: [
        {
          name: 'strategy_min_put_premium_0.65',
          overrides: { 'strategy.min_put_premium': 0.65 },
        },
      ],
    });
  });

  it('navigates to the NEW run’s arm on a 202, and says it is polling', async () => {
    fetchMock.mockResolvedValue(jsonResponse(202, { run_id: 'new456', cell_count: 4 }));
    setup();
    typeInto('strategy.min_put_premium', '0.65');
    fireEvent.click(submit());
    await waitFor(() => expect(screen.getByTestId('harness-accepted')).toBeTruthy());
    expect(screen.getByTestId('harness-accepted').textContent).toBe(
      'accepted:new456:strategy_min_put_premium_0.65',
    );
  });

  it('navigates to the PRIOR run on a 200 and says nothing was replayed', async () => {
    // The mutation: treat 200 as 202. The operator would be told their tweak is
    // being measured when it was answered from storage — and the poll would sit
    // on a finished run for ever.
    fetchMock.mockResolvedValue(
      jsonResponse(200, { run_id: 'prior123', deduplicated: true, sweep_key: 'k' }),
    );
    setup();
    typeInto('strategy.min_put_premium', '0.65');
    fireEvent.click(submit());
    await waitFor(() => expect(screen.getByTestId('harness-accepted')).toBeTruthy());
    expect(screen.getByTestId('harness-accepted').textContent).toBe(
      'deduplicated:prior123:strategy_min_put_premium_0.65',
    );
  });
});

describe('every refusal, in the service’s own words', () => {
  const tweakAndSubmit = async (status: number, body: unknown) => {
    fetchMock.mockResolvedValue(jsonResponse(status, body));
    const handles = setup();
    typeInto('strategy.min_put_premium', '0.65');
    fireEvent.click(submit());
    await waitFor(() => expect(screen.getByTestId('tweak-outcome')).toBeTruthy());
    return handles;
  };

  it('reads a busy 409 as busy and LINKS the blocking run', async () => {
    const detail = 'this exact spec is already running as 9f2c1a4b7e0d3c55 (submitted via operator).';
    await tweakAndSubmit(409, { detail, run_id: '9f2c1a4b7e0d3c55' });
    expect(screen.getByTestId('tweak-outcome').dataset.outcome).toBe('conflict');
    expect(screen.getByTestId('tweak-detail').textContent).toBe(detail);
    const link = screen.getByRole('link', { name: /Open run 9f2c1a4b7e0d3c55/ });
    expect(link.getAttribute('href')).toBe('/sims/9f2c1a4b7e0d3c55');
    // A refusal is not an answer: nothing is accepted.
    expect(screen.queryByTestId('harness-accepted')).toBeNull();
  });

  it('reads the instance-lock 409 (run_id: NULL) as busy, with no link', async () => {
    await tweakAndSubmit(409, {
      detail: 'this instance is already replaying a simulation.',
      run_id: null,
    });
    expect(screen.getByText('A replay is already holding the sim service')).toBeTruthy();
    expect(screen.queryByRole('link', { name: /Open run/ })).toBeNull();
  });

  it('reads a coverage 409 and lists the missing symbol-days', async () => {
    await tweakAndSubmit(409, {
      detail: 'the chain lake is missing 3 symbol-days. Backfill: python main.py backfill-chains.',
      sessions: 252,
      weekdays: 261,
      missing_symbol_days: 3,
      boundary: '2025-09-02',
      no_sessions: [],
      missing: { GOOGL: ['2025-09-02', '2025-09-03', '2025-09-04'] },
    });
    expect(screen.getByText('The chain lake does not cover this window')).toBeTruthy();
    // The remedy is in the body, verbatim; the list is beside it.
    expect(screen.getByTestId('tweak-detail').textContent).toContain('backfill-chains');
    expect(screen.getByTestId('tweak-missing-days').textContent).toContain('GOOGL: 3 days');
  });

  it('reads a budget 409 and sends the operator to the batch path', async () => {
    await tweakAndSubmit(409, {
      detail: '260 cells exceeds this service’s cap of 240. Use the batch path.',
      cells: 260,
      max_cells: 240,
    });
    expect(screen.getByText('Over the interactive budget — use the batch path')).toBeTruthy();
    expect(screen.getByTestId('tweak-detail').textContent).toContain('260 cells');
  });

  it('falls back to generic for a 409 with none of the four markers', async () => {
    await tweakAndSubmit(409, { detail: 'refused for a reason this UI has never seen' });
    expect(screen.getByText('The sim service refused this spec')).toBeTruthy();
    expect(screen.getByTestId('tweak-detail').textContent).toBe(
      'refused for a reason this UI has never seen',
    );
  });

  it('renders a 422 verbatim', async () => {
    await tweakAndSubmit(422, { detail: 'override strategy.min_put_premium must be >= 0' });
    expect(screen.getByTestId('tweak-outcome').dataset.outcome).toBe('invalid');
    expect(screen.getByTestId('tweak-detail').textContent).toBe(
      'override strategy.min_put_premium must be >= 0',
    );
  });

  it('shows a viewer the backend’s 403 — the control was never hidden', async () => {
    // Decision 11: no `whoami`. A hidden button would teach a viewer the
    // feature does not exist instead of telling them who to ask.
    await tweakAndSubmit(403, { detail: 'viewer@example.com is not in OPERATORS' });
    expect(screen.getByTestId('tweak-outcome').dataset.outcome).toBe('unauthorized');
    expect(screen.getByTestId('tweak-detail').textContent).toBe(
      'viewer@example.com is not in OPERATORS',
    );
  });

  it('renders a 502 verbatim PLUS the cold-start sentence, and offers a retry', async () => {
    // The mutation: rewrite the 502 into a generic "the service is down". The
    // server's words carry the IAM grant on the token path, and the cold-start
    // sentence is what tells the operator the retry is likely to work.
    await tweakAndSubmit(502, {
      detail: 'could not obtain an identity token: needs roles/run.invoker on `sim-service`.',
    });
    expect(screen.getByTestId('tweak-detail').textContent).toContain('roles/run.invoker');
    expect(screen.getByTestId('tweak-cold-start').textContent).toContain('cold-start');
    fetchMock.mockResolvedValue(jsonResponse(202, { run_id: 'warm789', cell_count: 4 }));
    fireEvent.click(screen.getByTestId('tweak-retry'));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it('renders a 503 as "not configured on this revision"', async () => {
    await tweakAndSubmit(503, { detail: 'SIM_SERVICE_URL is unset on this revision' });
    expect(screen.getByTestId('tweak-outcome').dataset.outcome).toBe('disabled');
    expect(screen.getByTestId('tweak-detail').textContent).toContain('SIM_SERVICE_URL');
  });
});

describe('the pin button', () => {
  const armTheBar = () => {
    const handles = setup();
    typeInto('strategy.min_put_premium', '0.65');
    return handles;
  };

  it('is disabled until there is a spec to pin', () => {
    setup();
    expect(screen.getByTestId('pin-open')).toBeDisabled();
  });

  it('posts {spec, note} and reports the pin id and the ROLLING shape', async () => {
    armTheBar();
    fireEvent.click(screen.getByTestId('pin-open'));
    fireEvent.change(screen.getByLabelText('pin note'), { target: { value: 'premium floor' } });
    fetchMock.mockResolvedValue(
      jsonResponse(201, { pin_id: 'pin_7f3a', window_days: 365, holdout_days: 90 }),
    );
    fireEvent.click(screen.getByTestId('pin-confirm'));
    await waitFor(() => expect(screen.getByTestId('pin-outcome')).toBeTruthy());
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v2/sims/pins');
    const body = JSON.parse(init.body);
    expect(body.note).toBe('premium floor');
    expect(body.spec.scenarios[0].overrides).toEqual({ 'strategy.min_put_premium': 0.65 });
    expect(screen.getByTestId('pin-outcome').textContent).toContain('pin_7f3a');
    expect(screen.getByTestId('pin-outcome').textContent).toContain('rolling 365-day window');
  });

  it('renders a pin 409 verbatim — it NAMES the pin that already asks this', async () => {
    // The mutation: replace it with "already pinned". The id is the whole
    // remedy: the operator has to edit or un-pin THAT pin.
    const detail =
      'pin pin_1234 already asks this exact question (note: premium floor). Edit or un-pin that one instead.';
    armTheBar();
    fireEvent.click(screen.getByTestId('pin-open'));
    fetchMock.mockResolvedValue(jsonResponse(409, { detail }));
    fireEvent.click(screen.getByTestId('pin-confirm'));
    await waitFor(() => expect(screen.getByTestId('pin-detail')).toBeTruthy());
    expect(screen.getByTestId('pin-detail').textContent).toBe(detail);
    expect(screen.getByTestId('pin-outcome').dataset.outcome).toBe('conflict');
  });

  it('renders a pin 422 verbatim', async () => {
    armTheBar();
    fireEvent.click(screen.getByTestId('pin-open'));
    fetchMock.mockResolvedValue(jsonResponse(422, { detail: 'a pinned spec may not set force' }));
    fireEvent.click(screen.getByTestId('pin-confirm'));
    await waitFor(() => expect(screen.getByTestId('pin-detail')).toBeTruthy());
    expect(screen.getByTestId('pin-detail').textContent).toBe('a pinned spec may not set force');
  });

  it('warns that the window becomes a rolling SHAPE before it is confirmed', () => {
    armTheBar();
    fireEvent.click(screen.getByTestId('pin-open'));
    expect(screen.getByTestId('pin-rolling-note').textContent).toContain('re-anchored');
  });
});

describe('a non-base cell says whose values these are (review R2a)', () => {
  it('captions the arm, and names the overrides it is NOT carrying', () => {
    // The bar prefills from the run's BASE whatever cell is on screen. Read as
    // the selected arm's config it is simply wrong, and the arm's own
    // overrides are not carried into the tweak.
    setup({ scenario: 'position_20pct' });
    const caption = screen.getByTestId('tweak-non-base-caption');
    expect(caption.textContent).toContain('base');
    expect(caption.textContent).toContain('position_20pct');
    expect(caption.textContent).toContain('risk.max_position_size');
    expect(caption.textContent).toContain('not carried');
  });

  it('says nothing on the base cell, where the prefill IS the cell', () => {
    setup({ scenario: 'base' });
    expect(screen.queryByTestId('tweak-non-base-caption')).toBeNull();
  });

  it('refuses a tweak that reproduces an existing arm, and links its cell', () => {
    const onOpenCell = vi.fn();
    setup({ onOpenCell });
    typeInto('risk.max_position_size', '0.2');
    expect(screen.getByTestId('tweak-blocking').textContent).toContain('would NOT deduplicate');
    expect(submit()).toBeDisabled();
    fireEvent.click(screen.getByTestId('tweak-open-existing-arm'));
    expect(onOpenCell).toHaveBeenCalledWith({
      scenario: 'position_20pct',
      symbol: 'GOOGL',
      split: 'holdout',
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('the DOWNWARD ONLY warning (review R7)', () => {
  it('warns above base without blocking the submit', () => {
    setup();
    typeInto('risk.max_position_size', '0.5');
    expect(screen.getByTestId('tweak-warnings').textContent).toContain('DOWNWARD ONLY');
    expect(submit()).not.toBeDisabled();
  });

  it('says nothing when the value is lowered', () => {
    setup();
    typeInto('risk.max_position_size', '0.25');
    expect(screen.queryByTestId('tweak-warnings')).toBeNull();
  });
});

describe('the outcome belongs to the spec that produced it (review R8)', () => {
  it('clears the moment a control changes', async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, { detail: 'refused for a reason' }));
    setup();
    typeInto('strategy.min_put_premium', '0.65');
    fireEvent.click(submit());
    await waitFor(() => expect(screen.getByTestId('tweak-outcome')).toBeTruthy());
    typeInto('strategy.min_put_premium', '0.7');
    expect(screen.queryByTestId('tweak-outcome')).toBeNull();
  });

  it('shows the rolling-window warning on EVERY confirm, not just the first', async () => {
    setup();
    typeInto('strategy.min_put_premium', '0.65');
    fireEvent.click(screen.getByTestId('pin-open'));
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: 'pin pin_1234 already asks this' }));
    fireEvent.click(screen.getByTestId('pin-confirm'));
    await waitFor(() => expect(screen.getByTestId('pin-detail')).toBeTruthy());
    // The refusal did not close the panel, so the standing-cost warning has to
    // still be there when the operator confirms again.
    expect(screen.getByTestId('pin-rolling-note')).toBeTruthy();
  });
});

describe('the small things a reviewer checks (review LOW)', () => {
  it('keeps the submit button rendered and ENABLED for a viewer’s 403', async () => {
    // Decision 11: no `whoami`, so the control is never hidden — and it must
    // stay usable, because the operator who can submit shares this screen.
    fetchMock.mockResolvedValue(jsonResponse(403, { detail: 'not in OPERATORS' }));
    setup();
    typeInto('strategy.min_put_premium', '0.65');
    fireEvent.click(submit());
    await waitFor(() => expect(screen.getByTestId('tweak-outcome')).toBeTruthy());
    expect(submit()).toBeVisible();
    expect(submit()).not.toBeDisabled();
  });

  it('does not POST twice when the button is clicked during the flight', async () => {
    let release: (r: Response) => void = () => undefined;
    fetchMock.mockReturnValue(
      new Promise<Response>((resolve) => {
        release = resolve;
      }),
    );
    setup();
    typeInto('strategy.min_put_premium', '0.65');
    fireEvent.click(submit());
    await waitFor(() => expect(submit()).toBeDisabled());
    fireEvent.click(submit());
    fireEvent.submit(screen.getByTestId('tweak-form'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    release(jsonResponse(202, { run_id: 'x', cell_count: 4 }));
    await waitFor(() => expect(screen.getByTestId('harness-accepted')).toBeTruthy());
  });

  it('submits on Enter, because the controls are in a real form', async () => {
    fetchMock.mockResolvedValue(jsonResponse(202, { run_id: 'new456', cell_count: 4 }));
    setup();
    typeInto('strategy.min_put_premium', '0.65');
    fireEvent.submit(screen.getByTestId('tweak-form'));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it('says a LOADED allowlist types nothing, rather than "Loading" for ever', () => {
    setup({ allowlist: { ...allowlist, value_types: {} } });
    expect(screen.getByTestId('tweak-no-typed-keys').textContent).toContain('types no editable keys');
    expect(screen.queryByText('Loading the allowlist…')).toBeNull();
  });

  it('blames the RUN’s spec, not the operator, for a carried-field cap breach', () => {
    // Every field `validateSpec` checks here is carried from the run unedited,
    // so "fix your input" names an input this bar does not have.
    setup({ allowlist: { ...allowlist, caps: { ...allowlist.caps, max_window_days: 30 } } });
    typeInto('strategy.min_put_premium', '0.65');
    expect(screen.getByTestId('tweak-blocking').textContent).toContain(
      "This run's own spec no longer satisfies the current caps",
    );
  });

  it('scopes the cell-count hint to the SIM service, not to the pin', () => {
    setup();
    typeInto('strategy.min_put_premium', '0.65');
    expect(screen.getByTestId('tweak-form').textContent).toContain('SIM SERVICE caps');
    expect(screen.getByTestId('tweak-form').textContent).toContain('A pin has its own standing cap');
  });
});
