// FC-096 Phase E PR-6: recover an expired IAP session IN PLACE.
//
// Phase D shipped the minimum: `X-Requested-With` on every fetch so IAP answers
// 401 instead of a cross-origin 302, one banner in the app shell, and polling
// that stops. The remedy it offers is "reload the page", which works and throws
// away everything on screen — the selected run, the symbol tab, the scroll
// position, a half-filled tweak bar.
//
// This is Google's documented alternative
// (https://cloud.google.com/iap/docs/sessions-howto): open
// `/?gcp-iap-mode=DO_SESSION_REFRESH` in a POPUP, poll a real API endpoint
// until it stops answering 401, then carry on. Google's page "keeps on
// refreshing the session periodically" for as long as it is open, so the popup
// is the worker and this module is only the observer.
//
// Two things about it are easy to get wrong and are therefore pinned by tests:
//
//   1. `open` is called SYNCHRONOUSLY inside the click handler, as the very
//      first statement, with no `await` before it. Browsers grant a popup only
//      to a live user gesture; one awaited microtask and the gesture is spent,
//      the popup is blocked, and the feature silently degrades to the reload
//      button on every machine with default settings.
//   2. An IFRAME does not work. IAP's sign-in page sets
//      `X-Frame-Options`/CSP frame-ancestors, so the frame renders nothing and
//      the poll runs to timeout. A popup is not a stylistic choice here.

import { useCallback, useEffect, useRef, useState } from 'react';
import { IAP_XHR_HEADERS, markSessionRefreshed } from './iapSession';

// --------------------------------------------------------------------------- //
// Constants
// --------------------------------------------------------------------------- //

/** Google's session-refresh mode, served by IAP itself on our own origin. */
export const IAP_SESSION_REFRESH_URL = '/?gcp-iap-mode=DO_SESSION_REFRESH';

/**
 * A NAMED target, so a second click reuses the popup instead of spawning a
 * second one. The operator who clicks twice gets one window, and the window
 * left open by a `timeout` is the one the next attempt re-navigates.
 */
export const IAP_REFRESH_WINDOW_NAME = 'iap-session-refresh';

/** Small, and deliberately not `noopener`: `win.closed` is how `'closed'` is detected. */
export const IAP_REFRESH_WINDOW_FEATURES = 'width=560,height=680,menubar=no,toolbar=no';

/**
 * What the poll asks. `/api/health` (`dashboard/backend/main.py:45`) is the
 * cheapest authenticated endpoint on the service: no BigQuery, no Alpaca, no
 * GCS. It is behind IAP like everything else, which is the whole point — the
 * question is "does IAP let a request through yet", not "is the backend well".
 */
export const IAP_REFRESH_PROBE_URL = '/api/health';

/** Google's sample polls on an interval; 2 s is the plan's number (§IAP). */
export const IAP_REFRESH_POLL_MS = 2_000;

/** Five minutes. Longer than any sign-in takes, short enough to stop eventually. */
export const IAP_REFRESH_TIMEOUT_MS = 5 * 60_000;

/**
 * How the attempt ended.
 *
 *   * `refreshed` — a probe came back non-401. The session is good, the
 *     tab-wide generation is bumped, the stopped hooks resume.
 *   * `blocked`   — `window.open` returned null: the browser blocked the popup.
 *     The banner falls back to the reload copy, which always works.
 *   * `closed`    — the popup went away before the session came back (the
 *     operator closed it), or the caller abandoned the attempt. Polling stops;
 *     nothing is claimed about the session.
 *   * `timeout`   — five minutes of 401s. The popup is LEFT OPEN, because the
 *     operator may be mid-sign-in and closing it would destroy that.
 */
export type SessionRefreshOutcome = 'refreshed' | 'blocked' | 'closed' | 'timeout';

/** The slice of `Window` this module touches. Keeps the test double honest. */
export interface RefreshWindowHandle {
  readonly closed: boolean;
  close: () => void;
}

export interface SessionRefreshDeps {
  /** Defaults to `window.open`. MUST be callable synchronously (see the header). */
  open?: (url: string, target: string, features: string) => RefreshWindowHandle | null;
  fetch?: typeof globalThis.fetch;
  /** Injected so the deadline can be tested without five real minutes. */
  now?: () => number;
  /** Injected so the 2 s cadence is assertable. Defaults to `setTimeout`. */
  sleep?: (ms: number) => Promise<void>;
  /** `true` once the caller has abandoned the attempt (unmount, or a new one). */
  abandoned?: () => boolean;
}

// --------------------------------------------------------------------------- //
// The probe
// --------------------------------------------------------------------------- //

/**
 * Has IAP started letting requests through again?
 *
 * NON-401 is the test, not 200. A 500 from the backend still proves IAP passed
 * the request to it, which is exactly the question being asked; treating only
 * 200 as success would leave the operator staring at a sign-out banner because
 * an unrelated endpoint was unwell.
 *
 * `credentials: 'include'` is a SAME-ORIGIN NO-OP — `/api/health` is on this
 * origin and cookies go anyway. It is here for parity with Google's published
 * sample, so a reader comparing the two finds them the same. It is not
 * load-bearing and no test asserts that it does anything.
 */
async function probeSession(doFetch: typeof globalThis.fetch): Promise<boolean> {
  try {
    const response = await doFetch(IAP_REFRESH_PROBE_URL, {
      credentials: 'include',
      headers: IAP_XHR_HEADERS,
      // A cached 200 from before the expiry would report success while the
      // session is still gone.
      cache: 'no-store',
    });
    return response.status !== 401;
  } catch {
    // A network blip mid-refresh is not evidence of anything. Keep polling
    // until the deadline rather than reporting a false `refreshed`.
    return false;
  }
}

// --------------------------------------------------------------------------- //
// The handler
// --------------------------------------------------------------------------- //

/**
 * Open Google's refresh page and poll until the session comes back.
 *
 * CALL ORDER IS THE CONTRACT: `open` runs before this function awaits anything.
 * An `async function` body runs synchronously up to its first `await`, so as
 * long as `open(...)` stays the first statement — and the caller does not
 * `await` before calling this — the popup is opened inside the user gesture.
 * Moving it below any `await` is the mutation this module's call-order test
 * exists to kill.
 */
export async function startSessionRefresh(
  deps: SessionRefreshDeps = {},
): Promise<SessionRefreshOutcome> {
  const {
    open = (url, target, features) =>
      window.open(url, target, features) as RefreshWindowHandle | null,
    fetch: doFetch = (...args: Parameters<typeof globalThis.fetch>) => globalThis.fetch(...args),
    now = () => Date.now(),
    sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)),
    abandoned = () => false,
  } = deps;

  // ---- FIRST STATEMENT. Nothing may be awaited above this line. ----
  const win = open(IAP_SESSION_REFRESH_URL, IAP_REFRESH_WINDOW_NAME, IAP_REFRESH_WINDOW_FEATURES);
  if (!win) return 'blocked';

  const deadline = now() + IAP_REFRESH_TIMEOUT_MS;

  while (now() < deadline) {
    await sleep(IAP_REFRESH_POLL_MS);

    // Checked BEFORE the probe: a closed popup means the operator gave up, and
    // one more request against a session that is still gone helps nobody.
    if (win.closed || abandoned()) return 'closed';

    if (await probeSession(doFetch)) {
      // Order matters only in that the window is closed before anything can
      // re-render — the popup has done its job and an orphan window is litter.
      try {
        win.close();
      } catch {
        // A popup that navigated cross-origin can refuse `close()`. Not fatal:
        // the session is back, which is the thing that was asked for.
      }
      markSessionRefreshed();
      return 'refreshed';
    }
  }

  return 'timeout';
}

// --------------------------------------------------------------------------- //
// The React seam
// --------------------------------------------------------------------------- //

export interface SessionRefreshState {
  /** An attempt is in flight. The button is disabled while this is true. */
  running: boolean;
  /** How the last attempt ended, or null if none has finished this mount. */
  outcome: SessionRefreshOutcome | null;
  /** Start an attempt. Safe to call from a click handler — and only from one. */
  start: () => void;
}

/**
 * The banner's half of the handler.
 *
 * `start` is NOT async and awaits nothing before `startSessionRefresh`, for the
 * same gesture reason as above. On unmount the in-flight attempt is abandoned:
 * the loop stops at its next tick and the promise's result is dropped, so
 * nothing sets state on a dead component and nothing keeps polling for the rest
 * of the five minutes. The popup itself is LEFT ALONE — closing someone's
 * half-finished Google sign-in because a component unmounted would be rude.
 */
export function useSessionRefresh(deps: SessionRefreshDeps = {}): SessionRefreshState {
  const [running, setRunning] = useState(false);
  const [outcome, setOutcome] = useState<SessionRefreshOutcome | null>(null);
  const abandonedRef = useRef(false);
  const runningRef = useRef(false);
  const depsRef = useRef(deps);
  depsRef.current = deps;

  useEffect(() => {
    abandonedRef.current = false;
    return () => {
      abandonedRef.current = true;
    };
  }, []);

  const start = useCallback(() => {
    // Two popups for one session is never right, and the second `open` on the
    // same window name would just re-navigate the first mid-sign-in.
    if (runningRef.current) return;
    runningRef.current = true;
    setRunning(true);
    setOutcome(null);

    // No `await` here — see the header. The promise is handled, not awaited.
    void startSessionRefresh({
      ...depsRef.current,
      abandoned: () => abandonedRef.current || depsRef.current.abandoned?.() === true,
    })
      .then((result) => {
        if (abandonedRef.current) return;
        setOutcome(result);
      })
      .catch(() => {
        // `startSessionRefresh` swallows fetch failures itself; this is the
        // belt for a `window.open` that throws (some hardened browsers do).
        if (!abandonedRef.current) setOutcome('blocked');
      })
      .finally(() => {
        runningRef.current = false;
        if (!abandonedRef.current) setRunning(false);
      });
  }, []);

  return { running, outcome, start };
}
