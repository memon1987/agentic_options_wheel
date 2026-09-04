// FC-096 Phase E PR-6: the contracts of the DO_SESSION_REFRESH handler.
//
// The one that matters most is CALL ORDER. A popup is granted to a live user
// gesture and to nothing else, so `window.open` has to run before this module
// awaits anything at all. That failure is invisible in review (the code reads
// fine either way) and invisible in production too — the popup is simply
// blocked and the operator reaches for Reload, which is what the feature was
// meant to replace. Hence a test that asserts `open` has ALREADY been called
// before the returned promise is awaited.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import {
  IAP_REFRESH_POLL_MS,
  IAP_REFRESH_PROBE_URL,
  IAP_REFRESH_TIMEOUT_MS,
  IAP_REFRESH_WINDOW_NAME,
  IAP_SESSION_REFRESH_URL,
  startSessionRefresh,
  useSessionRefresh,
} from './iapRefresh';
import {
  markSessionExpired,
  resetSessionExpiredSignal,
  resetSessionGeneration,
  sessionExpiredSnapshot,
  sessionGenerationSnapshot,
} from './iapSession';

const respond = (status: number): Response =>
  ({
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    type: 'basic',
    headers: new Headers(),
    text: async () => '',
    json: async () => ({}),
  }) as unknown as Response;

/** A popup double. `closed` is the browser's own signal and the `'closed'` outcome. */
function fakeWindow() {
  const win = {
    closed: false,
    close: vi.fn(() => {
      win.closed = true;
    }),
  };
  return win;
}

/** Deps that make the loop run at machine speed: no real timers, frozen clock. */
const instant = () => ({ sleep: async () => {}, now: () => 0 });

beforeEach(() => {
  resetSessionExpiredSignal();
  resetSessionGeneration();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetSessionExpiredSignal();
  resetSessionGeneration();
});

describe('startSessionRefresh — the synchronous-open contract', () => {
  it('calls open BEFORE it awaits anything, and before the first fetch', async () => {
    const calls: string[] = [];
    const win = fakeWindow();
    const open = vi.fn(() => {
      calls.push('open');
      return win;
    });
    const doFetch = vi.fn(async () => {
      calls.push('fetch');
      return respond(200);
    });

    // Deliberately NOT awaited yet: an `async function` runs synchronously up
    // to its first `await`, so if `open` is the first statement it has already
    // happened by the time this line executes. Move it below any `await` — the
    // mutation — and `calls` is empty here.
    const promise = startSessionRefresh({
      open,
      fetch: doFetch,
      sleep: async () => {
        calls.push('sleep');
      },
      now: () => 0,
    });

    // The synchronous state. `sleep` is in it because CALLING an async function
    // runs its body up to its own first `await` — what matters is that `open`
    // ran FIRST and that nothing has been fetched yet, both of which stop being
    // true the moment `open` moves below an `await`.
    expect(open).toHaveBeenCalledTimes(1);
    expect(calls[0]).toBe('open');
    expect(calls).not.toContain('fetch');
    await expect(promise).resolves.toBe('refreshed');
    expect([...calls]).toEqual(['open', 'sleep', 'fetch']);
    expect(calls.indexOf('open')).toBeLessThan(calls.indexOf('fetch'));
  });

  it('opens Google’s refresh URL, in a named window', async () => {
    const win = fakeWindow();
    const open = vi.fn(() => win);
    await startSessionRefresh({
      open,
      fetch: vi.fn(async () => respond(200)),
      ...instant(),
    });
    expect(open).toHaveBeenCalledTimes(1);
    const [url, name] = open.mock.calls[0] as unknown as [string, string];
    expect(url).toBe(IAP_SESSION_REFRESH_URL);
    expect(url).toBe('/?gcp-iap-mode=DO_SESSION_REFRESH');
    // Named, so a second click re-navigates the one popup rather than opening
    // a second sign-in window beside the first.
    expect(name).toBe(IAP_REFRESH_WINDOW_NAME);
  });
});

describe('startSessionRefresh — outcomes', () => {
  it('401 × 3 then 200 ⇒ refreshed: window closed, generation bumped', async () => {
    markSessionExpired();
    const before = sessionGenerationSnapshot();
    const win = fakeWindow();
    const doFetch = vi
      .fn()
      .mockResolvedValueOnce(respond(401))
      .mockResolvedValueOnce(respond(401))
      .mockResolvedValueOnce(respond(401))
      .mockResolvedValueOnce(respond(200));

    const outcome = await startSessionRefresh({ open: () => win, fetch: doFetch, ...instant() });

    expect(outcome).toBe('refreshed');
    expect(doFetch).toHaveBeenCalledTimes(4);
    expect(win.close).toHaveBeenCalledTimes(1);
    // The two halves of "the tab is signed in again": the banner comes down,
    // and the hooks that stopped polling get something to resume on.
    expect(sessionExpiredSnapshot()).toBe(false);
    expect(sessionGenerationSnapshot()).toBe(before + 1);
  });

  it('treats any NON-401 as through-the-door — a 500 is IAP passing the request on', async () => {
    const win = fakeWindow();
    const outcome = await startSessionRefresh({
      open: () => win,
      fetch: vi.fn(async () => respond(500)),
      ...instant(),
    });
    // The question is "does IAP let a request through", not "is the backend
    // well". Requiring 200 would leave the operator behind a sign-out banner
    // because some unrelated dependency was down.
    expect(outcome).toBe('refreshed');
  });

  it('win.closed ⇒ closed: polling stops and nothing is claimed', async () => {
    const win = fakeWindow();
    const doFetch = vi.fn(async () => respond(401));
    let sleeps = 0;
    const outcome = await startSessionRefresh({
      open: () => win,
      fetch: doFetch,
      now: () => 0,
      sleep: async () => {
        sleeps += 1;
        // The operator closes the popup during the third wait.
        if (sleeps === 3) win.closed = true;
      },
    });
    expect(outcome).toBe('closed');
    // Two probes ran; the third cycle saw the closed window and stopped BEFORE
    // probing again. A `closed` that kept polling would be four requests a
    // minute for five minutes against a session that is still gone.
    expect(doFetch).toHaveBeenCalledTimes(2);
    expect(sessionGenerationSnapshot()).toBe(0);
  });

  it('open returns null ⇒ blocked, with no probe at all', async () => {
    const doFetch = vi.fn(async () => respond(200));
    const outcome = await startSessionRefresh({ open: () => null, fetch: doFetch, ...instant() });
    expect(outcome).toBe('blocked');
    expect(doFetch).not.toHaveBeenCalled();
    expect(sessionGenerationSnapshot()).toBe(0);
  });

  it('abandoned() ⇒ closed, checked before the probe', async () => {
    const win = fakeWindow();
    const doFetch = vi.fn(async () => respond(401));
    const outcome = await startSessionRefresh({
      open: () => win,
      fetch: doFetch,
      ...instant(),
      abandoned: () => true,
    });
    expect(outcome).toBe('closed');
    expect(doFetch).not.toHaveBeenCalled();
    // The popup is NOT closed: the operator may be mid-sign-in and the caller
    // going away is no reason to destroy that.
    expect(win.close).not.toHaveBeenCalled();
  });

  it('keeps polling through a network failure rather than reporting one', async () => {
    const win = fakeWindow();
    const doFetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(respond(200));
    const outcome = await startSessionRefresh({ open: () => win, fetch: doFetch, ...instant() });
    expect(outcome).toBe('refreshed');
    expect(doFetch).toHaveBeenCalledTimes(2);
  });
});

describe('startSessionRefresh — the probe request', () => {
  it('asks /api/health with the IAP XHR header and credentials: include', async () => {
    const win = fakeWindow();
    const doFetch = vi.fn(async () => respond(200));
    await startSessionRefresh({ open: () => win, fetch: doFetch, ...instant() });

    const [url, init] = doFetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(IAP_REFRESH_PROBE_URL);
    expect(url).toBe('/api/health');
    // Load-bearing: without it IAP answers a 302 to Google rather than the 401
    // this loop is watching for, and the poll never terminates.
    expect(new Headers(init.headers).get('X-Requested-With')).toBe('XMLHttpRequest');
    // NOT load-bearing: `/api/health` is same-origin, so cookies are sent
    // either way. Asserted as PRESENT only, for parity with Google's published
    // sample — no test claims it changes the outcome.
    expect(init.credentials).toBe('include');
  });
});

// These use the REAL `sleep` and the REAL clock reading, faked — the cadence
// and the deadline are the two numbers a test with an injected `sleep` cannot
// see at all.
describe('startSessionRefresh — cadence and deadline', () => {
  it('polls every 2 s, and not a moment sooner', async () => {
    vi.useFakeTimers();
    expect(IAP_REFRESH_POLL_MS).toBe(2_000);
    const win = fakeWindow();
    const doFetch = vi.fn(async () => respond(401));
    void startSessionRefresh({ open: () => win, fetch: doFetch });

    await vi.advanceTimersByTimeAsync(IAP_REFRESH_POLL_MS - 1);
    // At half a second — the mutation — there would already be three.
    expect(doFetch).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(doFetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(IAP_REFRESH_POLL_MS);
    expect(doFetch).toHaveBeenCalledTimes(2);
  });

  it('gives up after five minutes with `timeout`, leaving the popup open', async () => {
    vi.useFakeTimers();
    expect(IAP_REFRESH_TIMEOUT_MS).toBe(300_000);
    const win = fakeWindow();
    const doFetch = vi.fn(async () => respond(401));
    const outcomes: string[] = [];
    void startSessionRefresh({ open: () => win, fetch: doFetch }).then((o) => outcomes.push(o));

    await vi.advanceTimersByTimeAsync(IAP_REFRESH_TIMEOUT_MS - IAP_REFRESH_POLL_MS);
    // Still going: a deadline that never fires is the mutation this catches
    // only in company with the assertion below.
    expect(outcomes).toEqual([]);
    expect(doFetch.mock.calls.length).toBeGreaterThan(100);

    await vi.advanceTimersByTimeAsync(IAP_REFRESH_POLL_MS * 2);
    expect(outcomes).toEqual(['timeout']);
    // Left open ON PURPOSE: five minutes in, the likeliest explanation is a
    // sign-in still in progress, and closing it would throw that away.
    expect(win.close).not.toHaveBeenCalled();
    expect(sessionGenerationSnapshot()).toBe(0);
  });
});

describe('useSessionRefresh — the banner’s half', () => {
  it('runs ONE attempt per click and reports the outcome', async () => {
    vi.useFakeTimers();
    const win = fakeWindow();
    const open = vi.fn(() => win);
    const doFetch = vi.fn(async () => respond(200));
    const { result } = renderHook(() => useSessionRefresh({ open, fetch: doFetch }));

    expect(result.current.running).toBe(false);
    act(() => {
      result.current.start();
      // A second click while the first is in flight: two popups for one session
      // is never right, and on a named window the second would re-navigate the
      // first mid-sign-in.
      result.current.start();
    });
    expect(open).toHaveBeenCalledTimes(1);
    expect(result.current.running).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(IAP_REFRESH_POLL_MS);
    });
    expect(result.current.running).toBe(false);
    expect(result.current.outcome).toBe('refreshed');
  });

  it('reports `blocked` without ever polling', async () => {
    vi.useFakeTimers();
    const doFetch = vi.fn(async () => respond(200));
    const { result } = renderHook(() => useSessionRefresh({ open: () => null, fetch: doFetch }));
    await act(async () => {
      result.current.start();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.outcome).toBe('blocked');
    expect(result.current.running).toBe(false);
    expect(doFetch).not.toHaveBeenCalled();
  });

  it('abandons the attempt on unmount rather than polling out the five minutes', async () => {
    vi.useFakeTimers();
    const win = fakeWindow();
    const doFetch = vi.fn(async () => respond(401));
    const { result, unmount } = renderHook(() =>
      useSessionRefresh({ open: () => win, fetch: doFetch }),
    );
    act(() => result.current.start());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IAP_REFRESH_POLL_MS);
    });
    expect(doFetch).toHaveBeenCalledTimes(1);

    unmount();
    await vi.advanceTimersByTimeAsync(IAP_REFRESH_POLL_MS * 10);
    // One more request after the component is gone would be 150 of them.
    expect(doFetch).toHaveBeenCalledTimes(1);
  });
});
