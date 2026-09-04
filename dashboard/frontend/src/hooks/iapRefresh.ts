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
//   3. `win.closed` is ADVISORY, never a stop signal. Google's sign-in page
//      sets `Cross-Origin-Opener-Policy: same-origin-allow-popups`, which
//      severs the opener relationship: once the popup navigates there the
//      browsing-context group swaps and this `WindowProxy.closed` reports
//      TRUE while the window is visibly open, with the operator typing a
//      password into it. On the path that matters — signed out of Google, a
//      real sign-in page rendered — the FIRST 2 s tick would therefore have
//      ended the attempt as `'closed'`. So observing it opens a bounded grace
//      window (`IAP_REFRESH_CLOSED_GRACE_MS`) during which polling continues,
//      and `'closed'` is returned only if the session is still gone at its
//      end.

import { useCallback, useEffect, useRef, useState } from 'react';
import { IAP_XHR_HEADERS, isIapGenerated, markSessionRefreshed } from './iapSession';

// --------------------------------------------------------------------------- //
// Constants
// --------------------------------------------------------------------------- //

/** Google's session-refresh mode, served by IAP itself on our own origin. */
export const IAP_SESSION_REFRESH_URL = '/?gcp-iap-mode=DO_SESSION_REFRESH';

/**
 * A NAMED target. Where the browser still resolves the name, a second click
 * re-navigates the one popup instead of spawning a second sign-in beside it.
 *
 * WHERE, precisely: only until the popup navigates cross-origin. Google's
 * sign-in sets `Cross-Origin-Opener-Policy: same-origin-allow-popups`, and a
 * name in one browsing-context group is not resolvable from another — so once
 * the operator is on Google's page, a fresh `open` with this name opens a NEW
 * window rather than re-using the old one. The name is worth having for the
 * clicks that land before that (double-clicks, and a `blocked`/`timeout`
 * retry the operator makes without touching the popup); it is not a guarantee,
 * and no copy may promise one.
 */
export const IAP_REFRESH_WINDOW_NAME = 'iap-session-refresh';

/**
 * Small, and deliberately WITHOUT `noopener`.
 *
 * The trade-off, stated: `noopener` is the safe default because it denies the
 * opened page a handle on this one. It is omitted here because without an
 * opener there is no `WindowProxy` at all — no `closed` to read, no `close()`
 * to call — and the handler would have nothing but the five-minute deadline.
 * The exposure it buys back is acceptable and bounded: the popup's content is
 * Google's own sign-in and IAP's own refresh handler, on Google infrastructure
 * that already owns this session outright. Pointing it at anything else would
 * make the omission indefensible, which is why the URL is a constant.
 */
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
 * How long a popup reported `closed` keeps being polled anyway.
 *
 * Two populations share this window and both are served by it: the operator
 * who really did close the popup (they wait a minute for a banner that stays
 * up — cheap), and the operator whose popup only LOOKS closed because it
 * navigated to Google (header note 3 — they get the whole minute of sign-in
 * time they actually need, and 401s are the cheapest request the service
 * serves). Bounded rather than open-ended so a genuine give-up still ends the
 * attempt long before the five-minute deadline would.
 */
export const IAP_REFRESH_CLOSED_GRACE_MS = 60_000;

/**
 * How the attempt ended.
 *
 *   * `refreshed` — a probe got THROUGH IAP: non-401, and not a response IAP
 *     wrote itself. The session is good, the tab-wide generation is bumped,
 *     the stopped hooks resume.
 *   * `blocked`   — `window.open` returned null: the browser blocked the popup.
 *     The banner falls back to the reload copy, which always works.
 *   * `denied`    — IAP answered, and its answer was "not this account": an
 *     IAP-GENERATED 403. The operator signed in successfully, at Google, as
 *     somebody the IAP policy does not list — the ordinary outcome of the
 *     multi-account chooser. The popup is LEFT OPEN on purpose: it is the only
 *     affordance for switching accounts, and closing it would leave the
 *     operator with a banner and no way out but Reload.
 *   * `closed`    — the popup was reported closed and the session was STILL
 *     gone one grace window later (see `IAP_REFRESH_CLOSED_GRACE_MS`; `closed`
 *     alone proves nothing — header note 3), or the caller abandoned the
 *     attempt. Polling stops; nothing is claimed about the session.
 *   * `timeout`   — five minutes without getting through. The popup is LEFT
 *     OPEN, because the operator may be mid-sign-in and closing it would
 *     destroy that.
 */
export type SessionRefreshOutcome = 'refreshed' | 'blocked' | 'denied' | 'closed' | 'timeout';

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

/** What one probe established. */
type ProbeVerdict =
  /** IAP passed the request to the backend. Whatever came back, we are in. */
  | 'through'
  /** IAP answered it ITSELF with a 403: signed in, but not an allowed account. */
  | 'denied'
  /** Still shut out, or nothing was learned. Keep polling. */
  | 'not-yet';

/**
 * Has IAP started letting requests through again?
 *
 * The test is `status !== 401` AND `!isIapGenerated(response)`. "Any non-401"
 * was the round-1 rule and it was wrong in both directions that matter:
 *
 *   * an IAP-generated **403** (the multi-account chooser landed on an account
 *     the IAP policy does not list) would have been read as success — the
 *     popup closed, the banner taken down, every hook resumed straight into
 *     403 HTML, and the retry loops running for ever behind a page that looks
 *     signed in. It is reported as `'denied'` instead, with the popup intact.
 *   * an IAP-generated **5xx** would have done the same, minus the 403's
 *     diagnosis. It is `'not-yet'`: transient, keep polling.
 *
 * A BACKEND non-401 still counts as through, 5xx included — the question asked
 * here is "did IAP pass the request on", not "is the backend well", and the
 * app's own errors are the hooks' business to surface. The
 * `x-goog-iap-generated-response` header is what tells the two apart, and it
 * is readable because `/api/health` is same-origin.
 *
 * `credentials: 'include'` is a SAME-ORIGIN NO-OP — `/api/health` is on this
 * origin and cookies go anyway. It is here for parity with Google's published
 * sample, so a reader comparing the two finds them the same. It is not
 * load-bearing and no test asserts that it does anything.
 */
async function probeSession(doFetch: typeof globalThis.fetch): Promise<ProbeVerdict> {
  try {
    const response = await doFetch(IAP_REFRESH_PROBE_URL, {
      credentials: 'include',
      headers: IAP_XHR_HEADERS,
      // A cached 200 from before the expiry would report success while the
      // session is still gone.
      cache: 'no-store',
    });
    if (response.status === 401) return 'not-yet';
    if (isIapGenerated(response)) return response.status === 403 ? 'denied' : 'not-yet';
    return 'through';
  } catch {
    // A network blip mid-refresh is not evidence of anything. Keep polling
    // until the deadline rather than reporting a false `refreshed`.
    return 'not-yet';
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

  // Null until `win.closed` has been observed; then the instant at which a
  // still-shut-out session is finally reported as `'closed'`.
  let graceUntil: number | null = null;

  for (;;) {
    // `abandoned()` is the caller saying stop — a real stop signal, unlike
    // `win.closed`. No grace window: nobody is waiting for the answer.
    if (abandoned()) return 'closed';

    if (graceUntil === null) {
      if (now() >= deadline) return 'timeout';
    } else if (now() >= graceUntil) {
      // A whole grace window of 401s after the window was reported closed.
      // Now it means what it says.
      return 'closed';
    }

    await sleep(IAP_REFRESH_POLL_MS);
    if (abandoned()) return 'closed';

    // ADVISORY (header note 3). Observing `closed` starts the grace window and
    // NOTHING else — in particular it does not skip the probe below, which is
    // the one immediate probe owed to the operator who finished signing in and
    // then closed the popup within a single tick. Ending the attempt here, as
    // round 1 did, reported `'closed'` to somebody who was already signed in.
    if (graceUntil === null && win.closed) graceUntil = now() + IAP_REFRESH_CLOSED_GRACE_MS;

    const verdict = await probeSession(doFetch);

    // Signed in as the wrong account. The popup STAYS: it is the switch-account
    // affordance, and the session is not refreshed, so nothing is marked.
    if (verdict === 'denied') return 'denied';

    if (verdict === 'through') {
      // Order matters only in that the window is closed before anything can
      // re-render — the popup has done its job and an orphan window is litter.
      try {
        win.close();
      } catch {
        // Best-effort, and frequently a NO-OP rather than a throw: after the
        // popup has navigated cross-origin this `WindowProxy` is severed
        // (header note 3) and `close()` on it does nothing at all. Either way
        // the session is back, which is the thing that was asked for; an
        // orphan popup is litter, not a failure.
      }
      markSessionRefreshed();
      return 'refreshed';
    }
  }
}

// --------------------------------------------------------------------------- //
// The React seam
// --------------------------------------------------------------------------- //

export interface SessionRefreshState {
  /**
   * An attempt is in flight. The button marks itself `aria-disabled` /
   * `aria-busy` while this is true — not `disabled`, which would blow focus
   * off the button the operator just pressed and leave a screen reader with
   * nothing to announce. Re-entrancy is `start`'s own business (`runningRef`),
   * not the DOM's.
   */
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
