// FC-060 Layer 4 (PR-B): the /sims data layer.
//
// Contracts under test, each of which costs something real if it breaks:
//
//   * The POST is issued EXACTLY ONCE. `useApi` would retry it three times; a
//     retried submit can launch a second 6-8 minute Cloud Run Job against the
//     one-at-a-time slot, or come back 409 against the run it just started.
//   * Polling STOPS on a terminal status, but KEEPS GOING while nothing has
//     loaded or the last read failed — one 500 on mount must not leave the page
//     blank forever.
//   * A URL change clears the old data before the new request goes out, so run
//     A's grid is never shown under run B's heading.
//   * The interval never stacks requests on a slow backend.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import {
  SESSION_EXPIRED_MESSAGE,
  SWEEP_POLL_MS,
  submitSweep,
  useSweepDetail,
  useSweepList,
} from './useSweeps';
import type { SweepRow, SweepSpec } from '../types/v2';
import shapedHoldout from '../test/fixtures/sweep_shaped_holdout.json';
import shapedPending from '../test/fixtures/sweep_shaped_pending.json';

const SPEC: SweepSpec = {
  symbols: ['AAPL'],
  start: '2025-08-28',
  end: '2026-08-28',
  scenarios: [{ name: 'puts_15_25', overrides: { 'strategy.put_delta_range': [0.15, 0.25] } }],
};

const jsonResponse = (status: number, body: unknown): Response =>
  ({
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    type: 'basic',
    text: async () => JSON.stringify(body),
    json: async () => body,
  }) as unknown as Response;

/** A 200 carrying HTML — what an expired IAP session serves in place of JSON. */
const htmlResponse = (body = '<html><body>Sign in</body></html>'): Response =>
  ({
    ok: true,
    status: 200,
    statusText: '',
    type: 'basic',
    text: async () => body,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON at position 0');
    },
  }) as unknown as Response;

/** A cross-origin redirect the fetch could not read. */
const opaqueResponse = (): Response =>
  ({
    ok: false,
    status: 0,
    statusText: '',
    type: 'opaqueredirect',
    text: async () => '',
    json: async () => null,
  }) as unknown as Response;

const row = (over: Partial<SweepRow> = {}): SweepRow => ({
  run_id: 'run1',
  sweep_key: null,
  status: 'running',
  deduplicated_to: null,
  submitted_at: new Date().toISOString(),
  started_at: null,
  finished_at: null,
  submitted_via: 'dashboard',
  execution_name: null,
  git_commit: null,
  engine_version: null,
  base_config_hash: null,
  base_config_json: null,
  spec_json: null,
  symbols: ['AAPL'],
  window_start: '2025-08-28',
  window_end: '2026-08-28',
  holdout_start: null,
  in_sample_only: true,
  scenario_count: 1,
  cell_count: 2,
  wall_seconds: null,
  materialise_seconds: null,
  replay_seconds: null,
  provider_fetches: null,
  bar_cache_hits: null,
  lake_summary_json: null,
  error: null,
  ...over,
});

let fetchMock: ReturnType<typeof vi.fn>;

/**
 * Flush the mount fetch's promise INSIDE act().
 *
 * `renderHook` returns before the first fetch resolves, so the resulting state
 * update lands outside act and React warns. Advancing the (fake) clock by zero
 * inside act is what settles it.
 */
const settle = async () => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
};

const advance = async (ms: number) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('submitSweep — one request, never a retry', () => {
  it('issues exactly one POST on success', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { run_id: 'abc', status: 'submitted' }));
    const outcome = await submitSweep(SPEC);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual({
      kind: 'accepted',
      body: { run_id: 'abc', status: 'submitted', deduplicated_to: null, prior_done_run_id: null },
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v2/sweeps');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual(SPEC);
    // FC-096 Phase D PR-2: the `SWEEP_SUBMIT_TOKEN` bearer is retired. Sending
    // one would be sending a dead credential to a service that ignores it.
    expect(init.headers.Authorization).toBeUndefined();
    // ...and this is what makes an expired IAP session answer 401 instead of
    // redirecting the fetch to Google's sign-in page.
    expect(init.headers['X-Requested-With']).toBe('XMLHttpRequest');
  });

  it.each([
    [401, 'session_expired'],
    [403, 'unauthorized'],
    [409, 'conflict'],
    [422, 'invalid'],
    [502, 'launch_failed'],
    [503, 'disabled'],
    [500, 'error'],
  ])('issues exactly one POST on HTTP %i and classifies it as %s', async (status, kind) => {
    fetchMock.mockResolvedValue(jsonResponse(status, { detail: 'because' }));
    const outcome = await submitSweep(SPEC);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(outcome.kind).toBe(kind);
  });

  it('classifies a 401 as a session expiry, not as a rejected credential', async () => {
    // Post-IAP the only way a write reaches the backend without an assertion is
    // a session that has ended. Telling the operator their "token" was rejected
    // would send them looking for a credential that no longer exists.
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'no IAP assertion' }));
    expect(await submitSweep(SPEC)).toEqual({
      kind: 'session_expired',
      detail: SESSION_EXPIRED_MESSAGE,
    });
  });

  it('classifies a 200 carrying HTML as a session expiry', async () => {
    // IAP without `X-Requested-With` serves the sign-in page; belt and braces
    // for any hop that strips the header.
    fetchMock.mockResolvedValue(htmlResponse());
    expect((await submitSweep(SPEC)).kind).toBe('session_expired');
  });

  it('classifies an opaque redirect as a session expiry', async () => {
    fetchMock.mockResolvedValue(opaqueResponse());
    expect((await submitSweep(SPEC)).kind).toBe('session_expired');
  });

  it('still reports a 403 as an authorization refusal, in the server’s words', async () => {
    // 403 is the OTHER thing: signed in, verified, and not on the OPERATORS
    // allowlist. A reload cannot fix it, so it must not be shown as an expiry.
    const detail = 'someone@else.com is signed in and may read everything, but writes are limited…';
    fetchMock.mockResolvedValue(jsonResponse(403, { detail }));
    expect(await submitSweep(SPEC)).toEqual({ kind: 'unauthorized', status: 403, detail });
  });

  it('issues exactly one POST when the network throws — a retry could duplicate a Job', async () => {
    fetchMock.mockRejectedValue(new Error('network down'));
    const outcome = await submitSweep(SPEC);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual({ kind: 'error', status: null, detail: 'network down' });
  });

  it('surfaces a 422 reason VERBATIM', async () => {
    const reason =
      'universe.min_open_interest — the engine hardcodes open_interest: 0, so any floor rejects EVERY call.';
    fetchMock.mockResolvedValue(jsonResponse(422, { detail: reason }));
    expect(await submitSweep(SPEC)).toEqual({ kind: 'invalid', detail: reason });
  });

  it('surfaces a 502 grant text VERBATIM', async () => {
    const grant = 'The dashboard SA lacks run.jobs.run. Grant: gcloud run jobs add-iam-policy-binding …';
    fetchMock.mockResolvedValue(jsonResponse(502, { detail: grant }));
    expect(await submitSweep(SPEC)).toEqual({ kind: 'launch_failed', detail: grant });
  });

  it('surfaces the 409 detail, which already names the blocking run', async () => {
    // The server sends `{detail}` and nothing else — the detail carries the run
    // id and why one sweep runs at a time.
    const detail =
      'sweep run-old is running; one sweep runs at a time (the Job is a single vCPU).';
    fetchMock.mockResolvedValue(jsonResponse(409, { detail }));
    expect(await submitSweep(SPEC)).toEqual({ kind: 'conflict', detail });
  });

  it('accepts a 202 and carries the prior_done_run_id hint through', async () => {
    // The submit endpoint answers 202, never decides dedup itself
    // (`deduplicated_to` is always null), and offers the earlier completed run
    // only as a hint — the launch happened regardless.
    fetchMock.mockResolvedValue(
      jsonResponse(202, {
        run_id: 'new',
        status: 'submitted',
        deduplicated_to: null,
        prior_done_run_id: 'old',
      }),
    );
    expect(await submitSweep(SPEC)).toEqual({
      kind: 'accepted',
      body: {
        run_id: 'new',
        status: 'submitted',
        deduplicated_to: null,
        prior_done_run_id: 'old',
      },
    });
  });

  it('defaults prior_done_run_id to null when the 202 omits it', async () => {
    fetchMock.mockResolvedValue(jsonResponse(202, { run_id: 'new', status: 'submitted' }));
    expect(await submitSweep(SPEC)).toMatchObject({
      kind: 'accepted',
      body: { run_id: 'new', deduplicated_to: null, prior_done_run_id: null },
    });
  });

  it('flattens FastAPI’s list-shaped 422 rather than printing [object Object]', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { detail: [{ loc: ['body', 'symbols'], msg: 'field required' }] }),
    );
    expect(await submitSweep(SPEC)).toEqual({
      kind: 'invalid',
      detail: 'body.symbols: field required',
    });
  });
});

describe('useSweepList — the endpoint serves a BARE ARRAY', () => {
  it('reads a bare array', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, [row({ run_id: 'a' }), row({ run_id: 'b' })]));
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.data).toHaveLength(2);
    expect(result.current.data?.[0].run_id).toBe('a');
  });

  it('also reads a {sweeps: [...]} envelope rather than rendering an empty list', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, { sweeps: [row()] }));
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.data).toHaveLength(1);
  });
});

describe('the IAP session expiring is its own state (FC-096 Phase D)', () => {
  it('sends X-Requested-With on every read', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, []));
    renderHook(() => useSweepList());
    await settle();
    const [, init] = fetchMock.mock.calls[0];
    // Without this IAP answers an expired session with a 302 to Google, which
    // this SPA cannot follow or parse — the failure would arrive as a CORS
    // error indistinguishable from the API being down.
    expect(init.headers['X-Requested-With']).toBe('XMLHttpRequest');
  });

  it('flags a 401 as sessionExpired with the reload message', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'no IAP assertion' }));
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.sessionExpired).toBe(true);
    expect(result.current.error).toBe(SESSION_EXPIRED_MESSAGE);
  });

  it('flags a 200 of HTML as sessionExpired rather than a JSON syntax error', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(htmlResponse());
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.sessionExpired).toBe(true);
    expect(result.current.error).toBe(SESSION_EXPIRED_MESSAGE);
  });

  it('flags an opaque redirect as sessionExpired', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(opaqueResponse());
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.sessionExpired).toBe(true);
  });

  it('STOPS polling once the session is gone', async () => {
    // Every other error keeps polling, because it may be transient. This one
    // cannot recover without a reload, so polling on is four failed requests a
    // minute for as long as the tab stays open.
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'no IAP assertion' }));
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await advance(SWEEP_POLL_MS * 5);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.sessionExpired).toBe(true);
  });

  it('an ordinary 500 is NOT a session expiry and keeps polling', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(500, { detail: 'boom' }));
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.sessionExpired).toBe(false);
    await advance(SWEEP_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('polling stops on a terminal status — and only then', () => {
  it('keeps polling the list while a run is running, and stops when it is done', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, [row({ status: 'running' })]));
    const { result } = renderHook(() => useSweepList());

    await settle();
    expect(result.current.data).not.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await advance(SWEEP_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // The run finishes. From here nothing more should be requested, ever.
    fetchMock.mockResolvedValue(jsonResponse(200, [row({ status: 'done' })]));
    await advance(SWEEP_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(3);

    await advance(SWEEP_POLL_MS * 6);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each(['done', 'failed', 'deduplicated'] as const)(
    'never polls a run that is already %s',
    async (status) => {
      vi.useFakeTimers();
      fetchMock.mockResolvedValue(
        jsonResponse(200, { ...shapedPending, status, run: { ...shapedPending.run, status } }),
      );
      const { result } = renderHook(() => useSweepDetail('run1'));
      await settle();
      expect(result.current.data).not.toBeNull();
      expect(fetchMock).toHaveBeenCalledTimes(1);

      await advance(SWEEP_POLL_MS * 5);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it('keeps polling a status it does not recognise — unknown is a reason to look, not to stop', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, { ...shapedPending, status: 'quarantined' }));
    const { result } = renderHook(() => useSweepDetail('run1'));
    await settle();
    expect(result.current.data).not.toBeNull();
    await advance(SWEEP_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not fetch at all with no run selected', () => {
    renderHook(() => useSweepDetail(null));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('reads recover instead of dead-ending', () => {
  it('keeps polling after an initial failure — one 500 must not blank the page forever', async () => {
    vi.useFakeTimers();
    fetchMock.mockRejectedValue(new Error('BigQuery timeout'));
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatch(/BigQuery timeout/);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // It must try again — `anyLive([])` is false, so an unguarded implementation
    // would decide an empty list is terminal and never ask again.
    await advance(SWEEP_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    fetchMock.mockResolvedValue(jsonResponse(200, [row({ status: 'done' })]));
    await advance(SWEEP_POLL_MS);
    expect(result.current.data).toHaveLength(1);
    expect(result.current.error).toBeNull();
  });

  it('keeps the last good data when a refresh fails, and keeps retrying', async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, [row({ status: 'running' })]))
      .mockRejectedValue(new Error('BigQuery timeout'));
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.data).toHaveLength(1);

    await advance(SWEEP_POLL_MS);
    expect(result.current.error).toMatch(/BigQuery timeout/);
    // A transient failure must never blank a list the operator is watching.
    expect(result.current.data).toHaveLength(1);

    // And it must keep trying: the last read failed, so the run's own status is
    // not trustworthy evidence that there is nothing left to watch.
    await advance(SWEEP_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(3);

    fetchMock.mockResolvedValue(jsonResponse(200, [row({ status: 'done' })]));
    await advance(SWEEP_POLL_MS);
    expect(result.current.error).toBeNull();
    expect(result.current.data?.[0].status).toBe('done');
  });

  it('does not stack requests when a read outlives the poll interval', async () => {
    vi.useFakeTimers();
    let release: ((r: Response) => void) | null = null;
    fetchMock.mockImplementation(
      () => new Promise<Response>((resolve) => {
        release = resolve;
      }),
    );
    renderHook(() => useSweepList());
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Three intervals pass with the first request still outstanding.
    await advance(SWEEP_POLL_MS * 3);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    release!(jsonResponse(200, [row({ status: 'running' })]));
    await advance(SWEEP_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('useSweepDetail — one run’s data never shows under another run’s id', () => {
  it('clears data and shows loading the moment the selected run changes', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, shapedHoldout));
    const { result, rerender } = renderHook(({ id }) => useSweepDetail(id), {
      initialProps: { id: 'runA' as string | null },
    });
    await settle();
    expect(result.current.data?.sweep.run_id).toBe('0f1e2d3c4b5a6978');

    // Selecting run B must not leave run A's grid on screen while B loads.
    fetchMock.mockImplementation(() => new Promise<Response>(() => {}));
    rerender({ id: 'runB' });
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it('does NOT clear the list on a manual refetch of the same url', async () => {
    // `Simulations` calls `refetchList()` on every accepted submit. `refetch`
    // bumps the same effect that handles a url change, so an unconditional
    // reset there drops the runs list to "Loading..." the instant the operator
    // submits — and loses the rows entirely if that read then fails.
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, [row({ status: 'done' })]));
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.data).toHaveLength(1);

    // Hold the refetch open: the rows must still be there while it is in flight.
    fetchMock.mockImplementation(() => new Promise<Response>(() => {}));
    act(() => {
      result.current.refetch();
    });
    expect(result.current.data).toHaveLength(1);
    expect(result.current.data?.[0].run_id).toBe('run1');
    expect(result.current.loading).toBe(true);
  });

  it('keeps the previous rows when a manual refetch FAILS, with the error set', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, [row({ status: 'done' })]));
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.data).toHaveLength(1);

    fetchMock.mockRejectedValue(new Error('BigQuery timeout'));
    act(() => {
      result.current.refetch();
    });
    await settle();
    expect(result.current.error).toMatch(/BigQuery timeout/);
    // The rule from the keep-last-good-data fix, which the reset was breaking.
    expect(result.current.data).toHaveLength(1);
    expect(result.current.loading).toBe(false);
  });

  it('still clears everything when the url genuinely changes after a refetch', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, shapedHoldout));
    const { result, rerender } = renderHook(({ id }) => useSweepDetail(id), {
      initialProps: { id: 'runA' as string | null },
    });
    await settle();
    act(() => {
      result.current.refetch();
    });
    await settle();
    expect(result.current.data).not.toBeNull();

    fetchMock.mockImplementation(() => new Promise<Response>(() => {}));
    rerender({ id: 'runB' });
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(true);
  });

  it('clears a stale error when the selection changes', async () => {
    vi.useFakeTimers();
    fetchMock.mockRejectedValue(new Error('boom'));
    const { result, rerender } = renderHook(({ id }) => useSweepDetail(id), {
      initialProps: { id: 'runA' as string | null },
    });
    await settle();
    expect(result.current.error).toMatch(/boom/);

    fetchMock.mockImplementation(() => new Promise<Response>(() => {}));
    rerender({ id: 'runB' });
    expect(result.current.error).toBeNull();
  });

  it('normalises the bare shape_results payload into {sweep, results, raw}', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, shapedHoldout));
    const { result } = renderHook(() => useSweepDetail('run1'));
    await settle();
    expect(result.current.data?.sweep.run_id).toBe('0f1e2d3c4b5a6978');
    expect(result.current.data?.results?.rows).toHaveLength(16);
    expect(result.current.data?.raw).toBeTruthy();
    await advance(SWEEP_POLL_MS * 3);
    expect(fetchMock).toHaveBeenCalledTimes(1); // terminal
  });

  it('gives a non-done run null results rather than an empty report', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, shapedPending));
    const { result } = renderHook(() => useSweepDetail('run1'));
    await settle();
    expect(result.current.data?.sweep.status).toBe('submitted');
    expect(result.current.data?.results).toBeNull();
    // Non-terminal, so it keeps watching.
    await advance(SWEEP_POLL_MS);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('reports an unreadable payload as an error rather than an empty page', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, { unexpected: true }));
    const { result } = renderHook(() => useSweepDetail('run1'));
    await settle();
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatch(/cannot read/);
  });
});
