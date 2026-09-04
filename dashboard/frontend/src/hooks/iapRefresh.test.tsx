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
  IAP_REFRESH_CLOSED_GRACE_MS,
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

/**
 * A response IAP wrote ITSELF rather than proxying — its own 401 sign-in page,
 * its own 403 for an account the policy does not list, its own 5xx. The
 * `x-goog-iap-generated-response` header is the ONLY thing that says so; the
 * status alone cannot, because the backend serves 403s and 5xx too.
 */
const iapRespond = (status: number): Response =>
  ({
    ok: false,
    status,
    statusText: '',
    type: 'basic',
    headers: new Headers({ 'x-goog-iap-generated-response': 'true' }),
    text: async () => '<html>IAP</html>',
    json: async () => ({}),
  }) as unknown as Response;

/**
 * A popup double.
 *
 * `closed` is NOT a stop signal (see the module header): a real popup that has
 * navigated to Google's sign-in reports `closed === true` through a severed
 * `WindowProxy` while the window is on screen and being typed into. These tests
 * flip it exactly as the browser would — early, and while the session is still
 * gone — and assert the handler keeps going.
 */
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

  it('a BACKEND 500 — no IAP header — is through-the-door: refreshed', async () => {
    const win = fakeWindow();
    const outcome = await startSessionRefresh({
      open: () => win,
      fetch: vi.fn(async () => respond(500)),
      ...instant(),
    });
    // The question this loop asks is "does IAP let a request through", not "is
    // the backend well". Requiring 200 would leave the operator behind a
    // sign-out banner because some unrelated dependency was down; the app's own
    // errors are the hooks' business to surface, not this one's.
    expect(outcome).toBe('refreshed');
  });

  it('an IAP-generated 403 ⇒ denied, with the popup LEFT OPEN', async () => {
    markSessionExpired();
    const win = fakeWindow();
    const doFetch = vi.fn(async () => iapRespond(403));
    const outcome = await startSessionRefresh({ open: () => win, fetch: doFetch, ...instant() });

    // The multi-account chooser landed on an account the IAP policy does not
    // list. Round 1 read this as success: popup closed, banner down, every hook
    // resumed into 403 HTML and retried for ever behind a page that looked
    // signed in.
    expect(outcome).toBe('denied');
    // The popup is the ONLY switch-account affordance there is. Closing it
    // leaves the operator with a banner and no way out but Reload.
    expect(win.close).not.toHaveBeenCalled();
    expect(sessionExpiredSnapshot()).toBe(true);
    expect(sessionGenerationSnapshot()).toBe(0);
  });

  it('an IAP-generated 5xx is not through the door either — it keeps polling', async () => {
    const win = fakeWindow();
    const doFetch = vi
      .fn()
      .mockResolvedValueOnce(iapRespond(502))
      .mockResolvedValueOnce(respond(200));
    const outcome = await startSessionRefresh({ open: () => win, fetch: doFetch, ...instant() });
    expect(outcome).toBe('refreshed');
    expect(doFetch).toHaveBeenCalledTimes(2);
  });

  it('win.closed does NOT stop the attempt: the probe in that same tick still runs', async () => {
    const win = fakeWindow();
    const doFetch = vi
      .fn()
      .mockResolvedValueOnce(respond(401))
      .mockResolvedValueOnce(respond(200));
    let sleeps = 0;
    const outcome = await startSessionRefresh({
      open: () => win,
      fetch: doFetch,
      now: () => 0,
      // The operator finishes signing in and closes the popup inside one 2 s
      // tick — or, far more often, the popup merely LOOKS closed because it
      // navigated to Google. Either way the session is back.
      sleep: async () => {
        sleeps += 1;
        if (sleeps === 2) win.closed = true;
      },
    });
    // Round 1 returned `'closed'` here, telling somebody who was already
    // signed in that the window closed before the session came back.
    expect(outcome).toBe('refreshed');
    expect(doFetch).toHaveBeenCalledTimes(2);
    expect(sessionGenerationSnapshot()).toBe(1);
    // IN THE SAME TICK, and this is the assertion that says so: two sleeps, two
    // probes. Opening the grace window and then `continue`-ing — probing only on
    // the NEXT tick — reaches the same outcome two seconds late and would pass
    // every line above; here it is three sleeps.
    expect(sleeps).toBe(2);
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

  it('closed + 401 for the whole grace window ⇒ closed, at its END and not before', async () => {
    vi.useFakeTimers();
    expect(IAP_REFRESH_CLOSED_GRACE_MS).toBe(60_000);
    const win = fakeWindow();
    const doFetch = vi.fn(async () => respond(401));
    const outcomes: string[] = [];
    void startSessionRefresh({ open: () => win, fetch: doFetch }).then((o) => outcomes.push(o));

    await vi.advanceTimersByTimeAsync(IAP_REFRESH_POLL_MS);
    expect(doFetch).toHaveBeenCalledTimes(1);

    // The browser now says the popup is gone. It may be; it may equally be
    // Google's sign-in page behind a severed WindowProxy.
    win.closed = true;
    await vi.advanceTimersByTimeAsync(IAP_REFRESH_POLL_MS);
    const atGraceStart = doFetch.mock.calls.length;
    expect(outcomes).toEqual([]);

    // Still 401, still polling, a whole minute of sign-in time later. A grace
    // window of zero — the mutation — resolves `'closed'` right here.
    await vi.advanceTimersByTimeAsync(IAP_REFRESH_CLOSED_GRACE_MS - IAP_REFRESH_POLL_MS);
    expect(outcomes).toEqual([]);
    expect(doFetch.mock.calls.length).toBeGreaterThan(atGraceStart + 20);

    await vi.advanceTimersByTimeAsync(IAP_REFRESH_POLL_MS * 2);
    expect(outcomes).toEqual(['closed']);
    // Nothing is claimed about the session, and the window the operator may
    // still be looking at is not touched.
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
