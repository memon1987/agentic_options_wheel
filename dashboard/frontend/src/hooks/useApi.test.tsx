// FC-096 Phase D PR-2, review round 1 (F2): the hook that reads EVERY page.
//
// It had no test file at all, which is how F1 got in: `useApi` learned to throw
// a `SessionExpiredError`, kept the fact in a `string` no caller read, and went
// on polling behind it for as long as the tab stayed open. The contracts below
// are the ones that failure violated, plus the 401-classification rule (F3)
// that keeps the backend's own diagnostics on the screen.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useApi } from './useApi';
import {
  SESSION_EXPIRED_MESSAGE,
  RELOAD_HINT,
  resetSessionExpiredSignal,
  sessionExpiredSnapshot,
} from './iapSession';

let fetchMock: ReturnType<typeof vi.fn>;

/** A response double. `headers` is a real `Headers`, because the classifier reads it. */
const respond = (
  status: number,
  body: string,
  headers: Record<string, string> = {},
): Response =>
  ({
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    type: 'basic',
    headers: new Headers(headers),
    text: async () => body,
    json: async () => JSON.parse(body),
  }) as unknown as Response;

const json = (status: number, body: unknown, headers?: Record<string, string>) =>
  respond(status, JSON.stringify(body), headers);

/** IAP's own 401: text/html, and its generated-response marker. */
const iapUnauthorized = () =>
  respond(401, '<html><body>Sign in with Google</body></html>', {
    'x-goog-iap-generated-response': 'true',
    'content-type': 'text/html',
  });

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
  vi.useFakeTimers();
  resetSessionExpiredSignal();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetSessionExpiredSignal();
});

describe('useApi — the IAP header goes on every read', () => {
  it('sends X-Requested-With: XMLHttpRequest', async () => {
    fetchMock.mockResolvedValue(json(200, { ok: true }));
    renderHook(() => useApi('/api/live/account'));
    await settle();
    const [, init] = fetchMock.mock.calls[0];
    // Without it IAP answers an expired session with a 302 to Google, which
    // this fetch follows cross-origin and reports as a network failure —
    // indistinguishable from the API being down.
    expect(init.headers['X-Requested-With']).toBe('XMLHttpRequest');
  });

  it('sends it on the POLLED reads too, not only the first', async () => {
    fetchMock.mockResolvedValue(json(200, { ok: true }));
    renderHook(() => useApi('/api/live/account', { refreshInterval: 1_000 }));
    await settle();
    await advance(3_000);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
    for (const [, init] of fetchMock.mock.calls) {
      expect(init.headers['X-Requested-With']).toBe('XMLHttpRequest');
    }
  });
});

describe('useApi — a signed-out session is its own state', () => {
  it("sets sessionExpired on IAP's 401 and does NOT retry it three times", async () => {
    fetchMock.mockResolvedValue(iapUnauthorized());
    const { result } = renderHook(() => useApi('/api/live/account'));
    await settle();
    // The retry loop is 3 attempts 2s apart. Advancing past both delays proves
    // the short-circuit rather than the timing: no sign-in happens in 4s.
    await advance(10_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.sessionExpired).toBe(true);
    expect(result.current.error).toBe(SESSION_EXPIRED_MESSAGE);
  });

  it('STOPS the refresh interval once the session is gone', async () => {
    // The F1 defect exactly: the interval effect kept calling fetchData for as
    // long as the tab was open, four failed requests a minute, for ever.
    fetchMock.mockResolvedValue(iapUnauthorized());
    const { result } = renderHook(() =>
      useApi('/api/live/account', { refreshInterval: 1_000 }),
    );
    await settle();
    expect(result.current.sessionExpired).toBe(true);
    await advance(1_000 * 10);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('treats a 200 carrying HTML as an expiry, not as a malformed API', async () => {
    // Belt and braces for any hop that strips X-Requested-With: IAP then serves
    // the sign-in page with a 200, and `JSON.parse` fails at character 0.
    fetchMock.mockResolvedValue(respond(200, '<html><body>Sign in</body></html>'));
    const { result } = renderHook(() => useApi('/api/live/account'));
    await settle();
    await advance(10_000);
    expect(result.current.sessionExpired).toBe(true);
    expect(result.current.error).toBe(SESSION_EXPIRED_MESSAGE);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('raises the tab-wide signal, so the layout banner does not wait 60s', async () => {
    expect(sessionExpiredSnapshot()).toBe(false);
    fetchMock.mockResolvedValue(iapUnauthorized());
    renderHook(() => useApi('/api/live/account'));
    await settle();
    expect(sessionExpiredSnapshot()).toBe(true);
  });
});

describe("useApi — a BACKEND 401 keeps the backend's words (F3)", () => {
  const AUDIENCE_401 =
    'IAP_AUDIENCE is unset or blank on this revision, so every assertion is ' +
    'refused. Recovery: `gcloud run services update options-wheel-dashboard ' +
    '--update-env-vars IAP_AUDIENCE=/projects/799970961417/...`';

  it('surfaces the detail rather than "session expired"', async () => {
    fetchMock.mockResolvedValue(json(401, { detail: AUDIENCE_401 }));
    const { result } = renderHook(() => useApi('/api/live/account'));
    await settle();
    await advance(10_000);
    expect(result.current.error).toContain('IAP_AUDIENCE');
    expect(result.current.error).toContain(RELOAD_HINT);
    expect(result.current.error).not.toBe(SESSION_EXPIRED_MESSAGE);
    // NOT an expiry: the fix is an env var, and a reload loop never applies it.
    expect(result.current.sessionExpired).toBe(false);
    expect(sessionExpiredSnapshot()).toBe(false);
  });

  it('does not retry it either — an unset env var is not transient', async () => {
    fetchMock.mockResolvedValue(json(401, { detail: AUDIENCE_401 }));
    renderHook(() => useApi('/api/live/account'));
    await settle();
    await advance(10_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reads IAP's marker header even when the body happens to parse", async () => {
    // Defence in depth: if IAP ever answers JSON, its own header still wins.
    fetchMock.mockResolvedValue(
      json(401, { detail: 'anything' }, { 'x-goog-iap-generated-response': 'true' }),
    );
    const { result } = renderHook(() => useApi('/api/live/account'));
    await settle();
    await advance(10_000);
    expect(result.current.sessionExpired).toBe(true);
    expect(result.current.error).toBe(SESSION_EXPIRED_MESSAGE);
  });
});

describe('useApi — an ordinary failure still retries', () => {
  it('retries a 500 three times and then reports it, with no expiry', async () => {
    fetchMock.mockResolvedValue(json(500, { detail: 'boom' }));
    const { result } = renderHook(() => useApi('/api/live/account'));
    await settle();
    await advance(10_000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(result.current.sessionExpired).toBe(false);
    expect(result.current.error).toMatch(/HTTP 500/);
  });

  it('keeps polling after a 500 — a transient outage must recover on its own', async () => {
    fetchMock.mockResolvedValue(json(500, { detail: 'boom' }));
    renderHook(() => useApi('/api/live/account', { refreshInterval: 1_000 }));
    await settle();
    await advance(10_000);
    // 3 attempts on mount, then at least one more interval tick's worth.
    expect(fetchMock.mock.calls.length).toBeGreaterThan(3);
  });

  it('recovers to data on a good response', async () => {
    fetchMock.mockResolvedValue(json(200, { paper_trading: false }));
    const { result } = renderHook(() => useApi<{ paper_trading: boolean }>('/api/live/account'));
    await settle();
    expect(result.current.data).toEqual({ paper_trading: false });
    expect(result.current.error).toBeNull();
    expect(result.current.sessionExpired).toBe(false);
  });
});
