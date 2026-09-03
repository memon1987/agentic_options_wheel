// FC-096 Phase E PR-2 (§D-3): the artifact cache's rules, each one a mutation.
//
// Everything here is about WHEN a request happens, so every assertion is a call
// count against a stubbed fetch rather than a rendered string. The four rules:
// one fetch per URL, a 404 memoised only on a finished run, a rejection
// evicted, and `deduplicated` reading under the run that actually replayed.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import artifact13cc from '../test/fixtures/artifact_13cc_base_googl_fit.json';
import bars13cc from '../test/fixtures/bars_13cc_googl_fit.json';
import type { SweepRow } from '../types/v2';
import { resetSessionExpiredSignal } from './iapSession';
import {
  ARTIFACT_CACHE_MAX,
  artifactCacheSize,
  fetchArtifact,
  resetArtifactCacheForTests,
  resolveArtifactRun,
} from './artifactCache';
import { HttpError } from './useSweeps';
import { useArtifact } from './useArtifact';
import { useBars } from './useBars';

const ok = (body: unknown) =>
  ({
    ok: true,
    status: 200,
    statusText: '',
    text: async () => JSON.stringify(body),
    json: async () => body,
  }) as unknown as Response;

const err = (status: number, detail: string) =>
  ({
    ok: false,
    status,
    statusText: '',
    text: async () => JSON.stringify({ detail }),
    json: async () => ({ detail }),
  }) as unknown as Response;

const row = (over: Partial<SweepRow> = {}): SweepRow =>
  ({
    run_id: '13cc2729d1c74211',
    status: 'done',
    deduplicated_to: null,
    ...over,
  }) as SweepRow;

const passthrough = (raw: unknown) => ({ value: raw, reason: null });

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  resetArtifactCacheForTests();
  resetSessionExpiredSignal();
});

describe('fetchArtifact — memoisation', () => {
  it('fetches a URL once however many callers ask for it', async () => {
    fetchMock.mockResolvedValue(ok(artifact13cc));
    const a = fetchArtifact('/api/x', 'done', passthrough, () => 'no');
    const b = fetchArtifact('/api/x', 'done', passthrough, () => 'no');
    expect(await a).toEqual(await b);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('memoises a 404 on a `done` run — the normal answer for an errored cell', async () => {
    fetchMock.mockResolvedValue(err(404, 'No detail artifact for this cell.'));
    const first = await fetchArtifact('/api/x', 'done', passthrough, () => 'no');
    expect(first).toEqual({ kind: 'absent', detail: 'No detail artifact for this cell.' });
    await fetchArtifact('/api/x', 'done', passthrough, () => 'no');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('does NOT memoise a 404 on a run that has not finished', async () => {
    // The mutation: caching this. On a `running` run the 404 means "not written
    // YET"; memoised, the panel stays empty for the life of the tab against a
    // run that finished thirty seconds later.
    fetchMock.mockResolvedValue(err(404, 'not yet'));
    const first = await fetchArtifact('/api/x', 'running', passthrough, () => 'no');
    expect(first.kind).toBe('absent');
    expect(artifactCacheSize()).toBe(0);
    await fetchArtifact('/api/x', 'running', passthrough, () => 'no');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('evicts a rejected promise so the next mount retries', async () => {
    fetchMock.mockResolvedValueOnce(err(502, 'bucket unreachable'));
    await expect(fetchArtifact('/api/x', 'done', passthrough, () => 'no')).rejects.toThrow(
      /bucket unreachable/,
    );
    expect(artifactCacheSize()).toBe(0);
    fetchMock.mockResolvedValueOnce(ok(artifact13cc));
    const second = await fetchArtifact('/api/x', 'done', passthrough, () => 'no');
    expect(second.kind).toBe('ok');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('the thrown error carries the STATUS, not only the message', async () => {
    // Without this the cache cannot tell 404 from 502 and either retries a
    // permanent absence for ever or caches a transient failure for ever.
    fetchMock.mockResolvedValue(err(502, 'bucket unreachable'));
    let caught: unknown;
    await fetchArtifact('/api/y', 'done', passthrough, () => 'no').catch((e: unknown) => {
      caught = e;
    });
    expect(caught).toBeInstanceOf(HttpError);
    expect((caught as HttpError).status).toBe(502);
    expect((caught as HttpError).detail).toBe('bucket unreachable');
  });

  it('caches a REFUSED parse — the bytes will not change on a retry', async () => {
    fetchMock.mockResolvedValue(ok({ schema: 2 }));
    const result = await fetchArtifact('/api/z', 'done', () => null, () => 'schema 2');
    expect(result).toEqual({ kind: 'absent', detail: 'schema 2' });
    await fetchArtifact('/api/z', 'done', () => null, () => 'schema 2');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('is a real LRU: a HIT moves an entry to the back (F9)', async () => {
    // The mutation this kills: dropping the `remember` call on the hit path,
    // which makes this a FIFO wearing an LRU's name — the cell the operator
    // keeps coming back to becomes the one it evicts.
    fetchMock.mockResolvedValue(ok(artifact13cc));
    for (let i = 0; i < ARTIFACT_CACHE_MAX; i += 1) {
      await fetchArtifact(`/api/cell/${i}`, 'done', passthrough, () => 'no');
    }
    expect(fetchMock).toHaveBeenCalledTimes(ARTIFACT_CACHE_MAX);

    // Touch the OLDEST entry, then insert one more. Under an LRU the entry we
    // just used survives and entry 1 is evicted instead.
    await fetchArtifact('/api/cell/0', 'done', passthrough, () => 'no');
    await fetchArtifact('/api/cell/new', 'done', passthrough, () => 'no');
    expect(artifactCacheSize()).toBe(ARTIFACT_CACHE_MAX);

    await fetchArtifact('/api/cell/0', 'done', passthrough, () => 'no');
    expect(fetchMock).toHaveBeenCalledTimes(ARTIFACT_CACHE_MAX + 1); // still cached
    await fetchArtifact('/api/cell/1', 'done', passthrough, () => 'no');
    expect(fetchMock).toHaveBeenCalledTimes(ARTIFACT_CACHE_MAX + 2); // evicted
  });

  it('is bounded — the oldest entry falls out past the cap', async () => {
    fetchMock.mockResolvedValue(ok(artifact13cc));
    for (let i = 0; i < ARTIFACT_CACHE_MAX + 3; i += 1) {
      await fetchArtifact(`/api/cell/${i}`, 'done', passthrough, () => 'no');
    }
    expect(artifactCacheSize()).toBe(ARTIFACT_CACHE_MAX);
  });
});

describe('resolveArtifactRun — which run holds the evidence', () => {
  it('reads a done run under its own id, and hands the caller its status', () => {
    expect(resolveArtifactRun('abc', 'done', null)).toEqual({
      runId: 'abc',
      followed: false,
      status: 'done',
    });
  });

  it('follows `deduplicated_to`, and resolves the TARGET’s status', () => {
    // `deduplicated` is only ever written against a run that ALREADY COMPLETED
    // — that is what dedup means, and why nothing was replayed — so the run
    // being read is `done`. The status resolved here is the one the hooks
    // forward; F6's rule is that it is decided ONCE, beside the run id it
    // belongs to, never re-decided by a hook.
    expect(resolveArtifactRun('abc', 'deduplicated', 'xyz')).toEqual({
      runId: 'xyz',
      followed: true,
      status: 'done',
    });
  });

  it('refuses every status with nothing stored, and a dedup row with no pointer', () => {
    expect(resolveArtifactRun('abc', 'running', null)).toBeNull();
    expect(resolveArtifactRun('abc', 'submitted', null)).toBeNull();
    expect(resolveArtifactRun('abc', 'failed', null)).toBeNull();
    expect(resolveArtifactRun('abc', 'deduplicated', null)).toBeNull();
    expect(resolveArtifactRun(null, 'done', null)).toBeNull();
  });
});

describe('useArtifact / useBars', () => {
  const cell = { scenario: 'base', symbol: 'GOOGL', split: 'fit' };

  it('loads a cell and its sidecar from the run in the row', async () => {
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(ok(url.includes('/bars/') ? bars13cc : artifact13cc)),
    );
    const artifact = renderHook(() => useArtifact(row(), cell));
    const bars = renderHook(() => useBars(row(), 'GOOGL', 'fit'));
    await waitFor(() => expect(artifact.result.current.data).not.toBeNull());
    await waitFor(() => expect(bars.result.current.data).not.toBeNull());
    expect(artifact.result.current.data!.ledger).toHaveLength(72);
    expect(bars.result.current.data!.bars).toHaveLength(189);
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
      '/api/v2/sweeps/13cc2729d1c74211/artifacts/base/GOOGL/fit',
      '/api/v2/sweeps/13cc2729d1c74211/bars/GOOGL/fit',
    ]);
  });

  it('never fetches for a running run', async () => {
    const { result } = renderHook(() => useArtifact(row({ status: 'running' }), cell));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.data).toBeNull();
    expect(result.current.absent).toBeNull();
  });

  it('a deduplicated run reads under the run that answered it', async () => {
    fetchMock.mockResolvedValue(ok(artifact13cc));
    const { result } = renderHook(() =>
      useArtifact(row({ status: 'deduplicated', deduplicated_to: 'aaaa1111bbbb2222' }), cell),
    );
    await waitFor(() => expect(result.current.data).not.toBeNull());
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v2/sweeps/aaaa1111bbbb2222/artifacts/base/GOOGL/fit',
      expect.anything(),
    );
    expect(result.current.runId).toBe('aaaa1111bbbb2222');
    expect(result.current.followedDedup).toBe(true);
  });

  it('renders a 404 as ABSENCE with the endpoint’s own words, not an error', async () => {
    fetchMock.mockResolvedValue(err(404, 'No detail artifact for base/GOOGL/fit in run 13cc.'));
    const { result } = renderHook(() => useArtifact(row(), cell));
    await waitFor(() => expect(result.current.absent).not.toBeNull());
    expect(result.current.absent).toBe('No detail artifact for base/GOOGL/fit in run 13cc.');
    expect(result.current.error).toBeNull();
  });

  it('renders a 502 as an ERROR, and a later mount tries again', async () => {
    fetchMock.mockResolvedValueOnce(err(502, 'bucket unreachable'));
    const first = renderHook(() => useArtifact(row(), cell));
    await waitFor(() => expect(first.result.current.error).not.toBeNull());
    expect(first.result.current.absent).toBeNull();

    fetchMock.mockResolvedValueOnce(ok(artifact13cc));
    const second = renderHook(() => useArtifact(row(), cell));
    await waitFor(() => expect(second.result.current.data).not.toBeNull());
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('a followed dedup read MEMOISES its 404 — the target completed by construction', async () => {
    // The mutation this kills: `resolveArtifactRun` handing back the ROW's own
    // `'deduplicated'` for the followed case. That is not the status of the run
    // being read, and under it every missing cell of a dedup'd run costs one
    // request per mount for a fact that cannot change. (The rule it must not
    // break — a genuinely unfinished run's 404 is never memoised — is pinned
    // directly against `fetchArtifact` above, where the status is an argument.)
    fetchMock.mockResolvedValue(err(404, 'nothing stored under the pointer'));
    const dedup = row({ status: 'deduplicated', deduplicated_to: 'aaaa1111bbbb2222' });
    const first = renderHook(() => useArtifact(dedup, cell));
    await waitFor(() => expect(first.result.current.absent).not.toBeNull());
    first.unmount();
    const second = renderHook(() => useArtifact(dedup, cell));
    await waitFor(() => expect(second.result.current.absent).not.toBeNull());
    expect(second.result.current.absent).toBe('nothing stored under the pointer');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(artifactCacheSize()).toBe(1);
  });

  it('two mounts of the same cell share one request', async () => {
    fetchMock.mockResolvedValue(ok(artifact13cc));
    const first = renderHook(() => useArtifact(row(), cell));
    await waitFor(() => expect(first.result.current.data).not.toBeNull());
    first.unmount();
    const second = renderHook(() => useArtifact(row(), cell));
    await waitFor(() => expect(second.result.current.data).not.toBeNull());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
