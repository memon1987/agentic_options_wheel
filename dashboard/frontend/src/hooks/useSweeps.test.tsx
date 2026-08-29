// FC-060 Layer 4 (PR-B): the /sims data layer.
//
// Two contracts, both of which cost real money if they break:
//
//   * The POST is issued EXACTLY ONCE. `useApi` would retry it three times; a
//     retried submit can launch a second 6-8 minute Cloud Run Job against the
//     one-at-a-time slot, or come back 409 against the run it just started.
//   * Polling STOPS the moment a run reaches a terminal status. A page left open
//     on a monitor otherwise bills a BigQuery read every 15 seconds forever.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import {
  SWEEP_POLL_MS,
  isStuck,
  submitSweep,
  useSweepDetail,
  useSweepList,
} from './useSweeps';
import type { SweepDetail, SweepRow, SweepSpec } from '../types/v2';
import shapedHoldout from '../test/fixtures/sweep_shaped_holdout.json';

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
      body: { run_id: 'abc', status: 'submitted', deduplicated_to: null },
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
    const outcome = await submitSweep(SPEC, 'tok');
    expect(outcome).toEqual({ kind: 'invalid', detail: reason });
  });

  it('surfaces a 502 grant text VERBATIM', async () => {
    const grant = 'The dashboard SA lacks run.jobs.run. Grant: gcloud run jobs add-iam-policy-binding …';
    fetchMock.mockResolvedValue(jsonResponse(502, { detail: grant }));
    const outcome = await submitSweep(SPEC, 'tok');
    expect(outcome).toEqual({ kind: 'launch_failed', detail: grant });
  });

  it('pulls running_run_id out of a 409 so the UI can point at the run in flight', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(409, { detail: 'a sweep is already running', running_run_id: 'run-in-flight' }),
    );
    const outcome = await submitSweep(SPEC, 'tok');
    expect(outcome).toMatchObject({ kind: 'conflict', runningRunId: 'run-in-flight' });
  });

  it('reports a dedup hit as accepted with the run it points at', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { run_id: 'new', status: 'deduplicated', deduplicated_to: 'old' }),
    );
    const outcome = await submitSweep(SPEC, 'tok');
    expect(outcome).toMatchObject({ kind: 'accepted', body: { deduplicated_to: 'old' } });
  });

  it('flattens FastAPI’s list-shaped 422 rather than printing [object Object]', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { detail: [{ loc: ['body', 'symbols'], msg: 'field required' }] }),
    );
    const outcome = await submitSweep(SPEC, 'tok');
    expect(outcome).toEqual({ kind: 'invalid', detail: 'body.symbols: field required' });
  });
});

describe('polling stops on a terminal status', () => {
  it('keeps polling the list while a run is running, and stops when it is done', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, { sweeps: [row({ status: 'running' })] }));
    const { result } = renderHook(() => useSweepList());

    await settle();
    expect(result.current.data).not.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(SWEEP_POLL_MS);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // The run finishes. From here nothing more should be requested, ever.
    fetchMock.mockResolvedValue(jsonResponse(200, { sweeps: [row({ status: 'done' })] }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SWEEP_POLL_MS);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(SWEEP_POLL_MS * 6);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each(['done', 'failed', 'deduplicated'] as const)(
    'never polls a run that is already %s',
    async (status) => {
      vi.useFakeTimers();
      const detail: SweepDetail = { sweep: row({ status }), results: null };
      fetchMock.mockResolvedValue(jsonResponse(200, detail));
      const { result } = renderHook(() => useSweepDetail('run1'));

      await settle();
      expect(result.current.data).not.toBeNull();
      expect(fetchMock).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(SWEEP_POLL_MS * 5);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it('keeps polling a status it does not recognise — unknown is a reason to look, not to stop', async () => {
    vi.useFakeTimers();
    const detail = { sweep: row({ status: 'quarantined' as never }), results: null };
    fetchMock.mockResolvedValue(jsonResponse(200, detail));
    const { result } = renderHook(() => useSweepDetail('run1'));

    await settle();
    expect(result.current.data).not.toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SWEEP_POLL_MS);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not fetch at all with no run selected', () => {
    renderHook(() => useSweepDetail(null));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('normalises a bare shape_results payload into {sweep, results}', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, shapedHoldout));
    const { result } = renderHook(() => useSweepDetail('run1'));
    await settle();
    expect(result.current.data?.sweep.run_id).toBe('0f1e2d3c4b5a6978');
    expect(result.current.data?.results?.rows).toHaveLength(12);
    // status is terminal in the fixture, so nothing more is asked for.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SWEEP_POLL_MS * 3);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('reports an unreadable payload as an error rather than an empty page', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(200, { unexpected: true }));
    const { result } = renderHook(() => useSweepDetail('run1'));
    await settle();
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatch(/cannot read/);
  });

  it('keeps the last good data when a refresh fails', async () => {
    // Fake timers must be installed BEFORE the hook mounts: the poll interval is
    // registered on mount, and one created against the real clock is untouched
    // by `advanceTimersByTime`.
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, { sweeps: [row({ status: 'running' })] }))
      .mockRejectedValue(new Error('BigQuery timeout'));
    const { result } = renderHook(() => useSweepList());
    await settle();
    expect(result.current.data).not.toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(SWEEP_POLL_MS);
    });
    await vi.waitFor(() => expect(result.current.error).toBe('BigQuery timeout'));
    // A transient read failure must never blank a list the operator is watching.
    expect(result.current.data?.sweeps).toHaveLength(1);
  });
});

describe('isStuck', () => {
  const now = Date.parse('2026-08-29T13:00:00Z');

  it('is false for a fresh submit — container start is 3-4 minutes', () => {
    expect(isStuck(row({ status: 'submitted', submitted_at: '2026-08-29T12:55:00Z' }), now)).toBe(false);
  });

  it('is true for a submit older than 10 minutes with no running row', () => {
    expect(isStuck(row({ status: 'submitted', submitted_at: '2026-08-29T12:45:00Z' }), now)).toBe(true);
  });

  it('is false once the run reports running, however old', () => {
    expect(isStuck(row({ status: 'running', submitted_at: '2026-08-29T10:00:00Z' }), now)).toBe(false);
  });

  it('is false on an unparseable timestamp rather than crying wolf', () => {
    expect(isStuck(row({ status: 'submitted', submitted_at: 'not-a-time' }), now)).toBe(false);
  });
});
