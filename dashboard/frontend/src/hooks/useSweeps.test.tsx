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
import { SWEEP_POLL_MS, submitSweep, useSweepDetail, useSweepList } from './useSweeps';
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
    text: async () => JSON.stringify(body),
    json: async () => body,
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
    const outcome = await submitSweep(SPEC, 'tok');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual({
      kind: 'accepted',
      body: { run_id: 'abc', status: 'submitted', deduplicated_to: null, prior_done_run_id: null },
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v2/sweeps');
    expect(init.method).toBe('POST');
    expect(init.headers.Authorization).toBe('Bearer tok');
    expect(JSON.parse(init.body)).toEqual(SPEC);
  });

  it.each([
    [401, 'unauthorized'],
    [403, 'unauthorized'],
    [409, 'conflict'],
    [422, 'invalid'],
    [502, 'launch_failed'],
    [503, 'disabled'],
    [500, 'error'],
  ])('issues exactly one POST on HTTP %i and classifies it as %s', async (status, kind) => {
    fetchMock.mockResolvedValue(jsonResponse(status, { detail: 'because' }));
    const outcome = await submitSweep(SPEC, 'tok');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(outcome.kind).toBe(kind);
  });

  it('issues exactly one POST when the network throws — a retry could duplicate a Job', async () => {
    fetchMock.mockRejectedValue(new Error('network down'));
    const outcome = await submitSweep(SPEC, 'tok');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual({ kind: 'error', status: null, detail: 'network down' });
  });

  it('surfaces a 422 reason VERBATIM', async () => {
    const reason =
      'strategy.put_target_dte — cached chains store universe_dte=8; a scenario needs a re-materialisation.';
    fetchMock.mockResolvedValue(jsonResponse(422, { detail: reason }));
    expect(await submitSweep(SPEC, 'tok')).toEqual({ kind: 'invalid', detail: reason });
  });

  it('surfaces a 502 grant text VERBATIM', async () => {
    const grant = 'The dashboard SA lacks run.jobs.run. Grant: gcloud run jobs add-iam-policy-binding …';
    fetchMock.mockResolvedValue(jsonResponse(502, { detail: grant }));
    expect(await submitSweep(SPEC, 'tok')).toEqual({ kind: 'launch_failed', detail: grant });
  });

  it('surfaces the 409 detail, which already names the blocking run', async () => {
    // The server sends `{detail}` and nothing else — the detail carries the run
    // id and why one sweep runs at a time.
    const detail =
      'sweep run-old is running; one sweep runs at a time (the Job is a single vCPU).';
    fetchMock.mockResolvedValue(jsonResponse(409, { detail }));
    expect(await submitSweep(SPEC, 'tok')).toEqual({ kind: 'conflict', detail });
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
    expect(await submitSweep(SPEC, 'tok')).toEqual({
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
    expect(await submitSweep(SPEC, 'tok')).toMatchObject({
      kind: 'accepted',
      body: { run_id: 'new', deduplicated_to: null, prior_done_run_id: null },
    });
  });

  it('flattens FastAPI’s list-shaped 422 rather than printing [object Object]', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { detail: [{ loc: ['body', 'symbols'], msg: 'field required' }] }),
    );
    expect(await submitSweep(SPEC, 'tok')).toEqual({
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
