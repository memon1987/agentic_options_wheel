// FC-096 Phase D PR-2 (review round 1): the ONE place the SPA decides what a
// refusal from behind Identity-Aware Proxy means.
//
// This module exists because the same four things — the XHR header, the
// message, the error class and the "is this a sign-out?" test — were written
// TWICE, once in `useApi.ts` and once in `useSweeps.ts`. The copies drifted
// immediately: `useSweeps` stopped polling and surfaced the state, `useApi`
// threw the same error and then went on polling for ever behind a banner
// nobody rendered. Two implementations of one rule is how that happens; there
// is now one.

import { useSyncExternalStore } from 'react';

// --------------------------------------------------------------------------- //
// The wire
// --------------------------------------------------------------------------- //

/**
 * Sent on EVERY request the SPA makes.
 *
 * IAP answers a request whose session has expired with a **302 to Google's
 * sign-in page**. A `fetch` follows that cross-origin and the SPA gets back a
 * rejected `TypeError` or a lump of HTML — in both cases something that reads
 * as "the API is down" rather than "you are signed out". With this header IAP
 * answers **401** instead, which is a thing this module can recognise and say
 * out loud. It is not authentication; it is the difference between a
 * diagnosable state and a mystery.
 */
export const IAP_XHR_HEADERS = { 'X-Requested-With': 'XMLHttpRequest' } as const;

/**
 * The header IAP stamps on responses IT generated (rather than proxied).
 *
 * Verified live by both round-1 reviewers: IAP's own 401 is `text/html` and
 * carries `x-goog-iap-generated-response: true`; the backend's 401 is
 * `application/json` with a `detail` and carries no such header. That is the
 * whole basis of `classifyUnauthorized` below.
 */
export const IAP_GENERATED_HEADER = 'x-goog-iap-generated-response';

/** What the operator is told when their IAP session is gone. */
export const SESSION_EXPIRED_MESSAGE = 'Session expired — reload the page to sign in again.';

/**
 * Appended to a BACKEND 401's own words.
 *
 * A backend 401 is not necessarily a sign-out (it is also what
 * `iap_audience_unconfigured` and "no email claim" look like), but a stale
 * session is the commonest cause, so the remedy is offered without displacing
 * the server's message — which is the half that carries the fix.
 */
export const RELOAD_HINT = 'If you were signed in a moment ago, reloading the page may fix it.';

// --------------------------------------------------------------------------- //
// Errors
// --------------------------------------------------------------------------- //

/** The session is gone. Only a reload fixes it — never a retry. */
export class SessionExpiredError extends Error {
  readonly sessionExpired = true;
  readonly noRetry = true;
  constructor(message: string = SESSION_EXPIRED_MESSAGE) {
    super(message);
    this.name = 'SessionExpiredError';
  }
}

/**
 * A 401 the BACKEND produced, carrying its own diagnostic.
 *
 * Distinct from `SessionExpiredError` because the remedy is different and the
 * message is load-bearing: `iap_audience_unconfigured`'s detail carries the
 * `gcloud run services update --update-env-vars` line that repairs it. Told
 * "session expired — reload" instead, the operator reloads for ever against a
 * service that will never come back on its own.
 */
export class UnauthorizedError extends Error {
  readonly noRetry = true;
  constructor(detail: string) {
    super(detail);
    this.name = 'UnauthorizedError';
  }
}

/** True for the error thrown when a response looked like a signed-out session. */
export const isSessionExpired = (err: unknown): boolean =>
  !!err && (err as { sessionExpired?: boolean }).sessionExpired === true;

/** True for an error no number of retries can improve. */
export const isNoRetry = (err: unknown): boolean =>
  !!err && (err as { noRetry?: boolean }).noRetry === true;

// --------------------------------------------------------------------------- //
// Classification
// --------------------------------------------------------------------------- //

/** Did IAP itself write this response, rather than proxying ours? */
export function isIapGenerated(response: Pick<Response, 'headers'>): boolean {
  try {
    return response.headers?.get(IAP_GENERATED_HEADER) != null;
  } catch {
    // A test double, or a browser that hid the header. Absence proves nothing
    // either way, which is why the JSON-body test below is the primary signal.
    return false;
  }
}

/** The `detail` string out of a FastAPI error body, or null if there is none. */
export function jsonDetail(bodyText: string): string | null {
  if (!bodyText) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  const detail = (parsed as { detail?: unknown }).detail;
  return typeof detail === 'string' && detail.trim() ? detail : null;
}

export type Unauthorized =
  | { kind: 'session_expired'; detail: string }
  | { kind: 'backend'; detail: string };

/**
 * What a 401 actually means — IAP's, or the backend's?
 *
 * Round-1 finding (both reviewers): classifying every 401 as an expiry BEFORE
 * reading the body swallowed the backend's own diagnostics. Three of them
 * exist and each names its own repair — `iap_audience_unconfigured` (whose
 * detail carries the `--update-env-vars` recovery), the no-email-claim
 * refusal, and `NO_ASSERTION_DETAIL` (which names the programmatic recipe).
 * Reported as "session expired — reload", the operator reloads for ever.
 *
 * The rule, in the order it is applied:
 *   1. IAP's own header present ⇒ IAP wrote it ⇒ **expiry**.
 *   2. JSON body with a string `detail` ⇒ the backend wrote it ⇒ **its words**,
 *      plus a short reload hint.
 *   3. Anything else — HTML, an empty body, JSON without a detail ⇒ **expiry**.
 */
export function classifyUnauthorized(
  response: Pick<Response, 'headers'>,
  bodyText: string,
): Unauthorized {
  if (!isIapGenerated(response)) {
    const detail = jsonDetail(bodyText);
    if (detail) return { kind: 'backend', detail: `${detail}\n\n${RELOAD_HINT}` };
  }
  return { kind: 'session_expired', detail: SESSION_EXPIRED_MESSAGE };
}

/**
 * Turn a 401 into the error the caller should throw.
 *
 * NOTE what is deliberately NOT here: the old `looksSignedOut` also treated
 * `type === 'opaque' | 'opaqueredirect'` and `status === 0` as a sign-out.
 * Those branches are unreachable with default `fetch` — a cross-origin
 * redirect on a same-origin request ends as a REJECTED `TypeError`, not as an
 * opaque `Response` (opaque responses require `mode: 'no-cors'`, which nothing
 * here uses). They were dead code with live tests, which is worse than
 * neither, so both are gone.
 */
export function unauthorizedError(
  response: Pick<Response, 'headers'>,
  bodyText: string,
): SessionExpiredError | UnauthorizedError {
  const verdict = classifyUnauthorized(response, bodyText);
  return verdict.kind === 'session_expired'
    ? new SessionExpiredError()
    : new UnauthorizedError(verdict.detail);
}

// --------------------------------------------------------------------------- //
// The tab-wide signal
// --------------------------------------------------------------------------- //
//
// An expired session is a fact about the TAB, not about one hook. `LayoutV2`
// polls `/api/live/account` once a minute and is mounted on every route, so it
// is where the single banner lives — but the /sims hooks poll every 15s and
// would learn of the expiry up to 45s earlier. Without a shared signal that
// window is exactly the confusing state this fix exists to remove: three "could
// not be read" errors on the page and no banner above them.
//
// Hence: whichever hook notices first marks it, and the layout renders once.

let expired = false;
const listeners = new Set<() => void>();

/** Announce that this tab's IAP session is gone. Idempotent. */
export function markSessionExpired(): void {
  if (expired) return;
  expired = true;
  listeners.forEach((notify) => notify());
}

export function sessionExpiredSnapshot(): boolean {
  return expired;
}

export function subscribeSessionExpired(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

/** Subscribe a component to the tab-wide signal. */
export function useSessionExpiredSignal(): boolean {
  return useSyncExternalStore(subscribeSessionExpired, sessionExpiredSnapshot, sessionExpiredSnapshot);
}

/**
 * Clear the signal. Exported for tests ONLY — module state outlives a
 * `render()`, so one expired test would otherwise poison every later one.
 * Listeners are NOTIFIED rather than dropped: a component mounted across the
 * reset must re-render, not go stale.
 */
export function resetSessionExpiredSignal(): void {
  expired = false;
  listeners.forEach((notify) => notify());
}

// --------------------------------------------------------------------------- //
// The tab-wide GENERATION (FC-096 Phase E PR-6)
// --------------------------------------------------------------------------- //
//
// `markSessionExpired` is one-way: once a hook has seen a 401 the whole tab
// stops polling and the banner says "reload". PR-6 makes the session
// recoverable in place (Google's `DO_SESSION_REFRESH` popup), and a recovery
// has to reach the hooks that already gave up — none of which is listening for
// "un-expired", because until now that state did not exist.
//
// A COUNTER rather than a boolean, deliberately: a hook resumes on a CHANGE,
// and a change is a thing you can compare against what you last saw. A boolean
// flipping false→true→false would leave a hook that mounted mid-refresh unable
// to tell "already resumed" from "never expired", and a second expiry→refresh
// cycle in one tab would produce the same value twice.
//
// NAMING, on the record: §IAP of `docs/plans/fc-096-e.md` spells the success
// outcome `'restored'` / `markSessionRestored()`. The PR-6 build brief spells
// it `'refreshed'` / `markSessionRefreshed()`, and the brief won — one word,
// used consistently, matching Google's own `DO_SESSION_REFRESH`. Recorded so
// the plan/code divergence is a decision rather than a drift.

let generation = 0;
const generationListeners = new Set<() => void>();

/**
 * The IAP session came back. Clears the expiry signal and bumps the counter.
 *
 * Called by `startSessionRefresh` (`iapRefresh.ts`) on the first non-401 probe,
 * and by nothing else — an expiry is cleared by EVIDENCE (a request that got
 * through), never by optimism.
 */
export function markSessionRefreshed(): void {
  expired = false;
  generation += 1;
  // Both sets: the banner has to come down (expiry listeners) AND the hooks
  // that stopped polling have to re-arm (generation listeners).
  listeners.forEach((notify) => notify());
  generationListeners.forEach((notify) => notify());
}

export function sessionGenerationSnapshot(): number {
  return generation;
}

export function subscribeSessionGeneration(notify: () => void): () => void {
  generationListeners.add(notify);
  return () => {
    generationListeners.delete(notify);
  };
}

/**
 * Subscribe a hook to the tab-wide refresh counter.
 *
 * Starts at 0 and only ever increases. A hook that stopped polling on an expiry
 * resumes when this value differs from the one it last saw — see `useApi` and
 * `usePolledGet`.
 */
export function useSessionGeneration(): number {
  return useSyncExternalStore(
    subscribeSessionGeneration,
    sessionGenerationSnapshot,
    sessionGenerationSnapshot,
  );
}

/**
 * Reset the generation. Tests ONLY, for the same reason as
 * `resetSessionExpiredSignal`: module state outlives a `render()`.
 */
export function resetSessionGeneration(): void {
  generation = 0;
  generationListeners.forEach((notify) => notify());
}
