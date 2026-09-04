// FC-060 Layer 4 (PR-B): the /sims data layer.
//
// Deliberately NOT `useApi`. Two reasons, and the second is the important one:
//
//  1. `useApi` is GET-only.
//  2. `useApi` retries a failed request 3x. That is right for a read and WRONG
//     for this POST: a submit that times out may still have launched a Cloud Run
//     Job and inserted a `submitted` row, so a blind retry either duplicates a
//     6-8 minute execution or comes back 409 against the run it just started.
//     `submitSweep` therefore issues EXACTLY ONE request and reports what it
//     got — the operator decides whether to try again.
//
// Polling: 15s. A run that has reached a terminal status is insert-only history,
// so polling stops; but a list that has never loaded, or whose last read failed,
// keeps trying — otherwise one 500 on mount leaves the page permanently blank
// with no way back short of a reload.

import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  SweepAllowlist,
  SweepDetail,
  SweepRow,
  SweepSpec,
  SweepSubmitAccepted,
} from '../types/v2';
import { isTerminalSweepStatus } from '../types/v2';
import { normaliseSweepDetail, normaliseSweepList } from '../components/v2/sims/normaliseReport';
import {
  IAP_XHR_HEADERS,
  SESSION_EXPIRED_MESSAGE,
  SessionExpiredError,
  isSessionExpired,
  markSessionExpired,
  unauthorizedError,
} from './iapSession';

export const SWEEP_POLL_MS = 15_000;
const REQUEST_TIMEOUT_MS = 20_000;

// --------------------------------------------------------------------------- //
// IAP (FC-096 Phase D PR-2)
// --------------------------------------------------------------------------- //
//
// The header, the message, the error class and the 401 classifier all live in
// `iapSession.ts` now. They used to live here AND in `useApi.ts`, and the two
// copies drifted immediately — which is the defect review round 1 called F1.
// Re-exported because `Simulations.tsx` and `SubmitSweep.tsx` import them from
// this module.

export { SESSION_EXPIRED_MESSAGE, isSessionExpired } from './iapSession';

// --------------------------------------------------------------------------- //
// Submit
// --------------------------------------------------------------------------- //

/**
 * What came back from the one POST we made.
 *
 * Every failure carries the server's own words. The API's 422 quotes the exact
 * rejection reason `overrides.py` would give and its 502 carries the `gcloud`
 * grant command; paraphrasing either would delete the only actionable half.
 */
export type SubmitOutcome =
  | { kind: 'accepted'; body: SweepSubmitAccepted }
  /**
   * IAP's own 401, or an unreadable/non-JSON answer — the session is gone.
   * Split out from `unauthorized` because the remedy is completely different:
   * this one is fixed by reloading the page, and no amount of re-reading the
   * error helps.
   */
  | { kind: 'session_expired'; detail: string }
  /**
   * 401 from the BACKEND, carrying its own diagnostic (review round 1, F3).
   * `iap_audience_unconfigured` — whose detail carries the `--update-env-vars`
   * line that repairs it — an id-token with no `email` claim, and
   * `NO_ASSERTION_DETAIL` all arrive here. Rendered as an expiry instead, the
   * operator reloads for ever against a service that cannot recover on its own.
   */
  | { kind: 'unauthenticated'; detail: string }
  /** 403 — signed in, but not on the `OPERATORS` allowlist. */
  | { kind: 'unauthorized'; status: number; detail: string }
  /** 409 — another sweep is already in flight. The detail names it. */
  | { kind: 'conflict'; detail: string }
  /** 422 — the spec was refused. `detail` is the runner's reason, verbatim. */
  | { kind: 'invalid'; detail: string }
  /** 502 — the Job could not be launched (usually an IAM grant). */
  | { kind: 'launch_failed'; detail: string }
  /** 503 — the API refused: the sweep tables do not exist yet, or a dependency
   * this route needs is unconfigured. Fail-closed, with the reason in `detail`. */
  | { kind: 'disabled'; detail: string }
  | { kind: 'error'; status: number | null; detail: string };

/** Pull a human string out of a FastAPI error body without inventing one. */
function detailOf(body: unknown, fallback: string): string {
  if (typeof body === 'string' && body.trim()) return body;
  if (body && typeof body === 'object') {
    const d = (body as { detail?: unknown }).detail;
    if (typeof d === 'string' && d.trim()) return d;
    // FastAPI's own 422 shape is a list of {loc, msg, type}.
    if (Array.isArray(d)) {
      const parts = d
        .map((e) => {
          if (typeof e === 'string') return e;
          const o = e as { loc?: unknown[]; msg?: unknown };
          const loc = Array.isArray(o.loc) ? o.loc.join('.') : '';
          return typeof o.msg === 'string' ? (loc ? `${loc}: ${o.msg}` : o.msg) : null;
        })
        .filter((s): s is string => !!s);
      if (parts.length) return parts.join('\n');
    }
    if (d !== undefined) return JSON.stringify(d);
  }
  return fallback;
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text().catch(() => '');
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * POST the spec. EXACTLY ONE request, no retry, ever.
 *
 * **No `Authorization` header.** The `SWEEP_SUBMIT_TOKEN` bearer was retired in
 * FC-096 Phase D PR-2; the browser's IAP session cookie is what authenticates
 * this request, and the backend authorises it against `OPERATORS`. There is
 * nothing for this function to carry.
 *
 * Exported as a plain function (not a hook) so the no-retry contract can be
 * tested directly against a fetch spy.
 */
export async function submitSweep(spec: SweepSpec): Promise<SubmitOutcome> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch('/api/v2/sweeps', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...IAP_XHR_HEADERS,
      },
      body: JSON.stringify(spec),
      signal: controller.signal,
    });
  } catch (err) {
    const e = err as Error;
    return {
      kind: 'error',
      status: null,
      detail:
        e.name === 'AbortError'
          ? `The submit did not answer within ${REQUEST_TIMEOUT_MS / 1000}s. It was NOT retried — ` +
            'the run may still have started. Check the runs list before submitting again.'
          : e.message || 'The submit could not be sent.',
    };
  } finally {
    clearTimeout(timer);
  }

  // A 401 is read BEFORE it is classified: IAP's is `text/html` (and carries
  // `x-goog-iap-generated-response`), the backend's is JSON with a `detail`
  // that names its own repair. Deciding first — which is what this did — threw
  // the second one away. (Review round 1, F3.)
  if (response.status === 401) {
    const err = unauthorizedError(response, await response.text().catch(() => ''));
    if (isSessionExpired(err)) {
      markSessionExpired();
      return { kind: 'session_expired', detail: SESSION_EXPIRED_MESSAGE };
    }
    return { kind: 'unauthenticated', detail: err.message };
  }

  const body = await readBody(response);

  if (response.ok) {
    // `readBody` hands back the RAW TEXT when the body would not parse as JSON.
    // On a 2xx that means the sign-in page arrived where the API's answer
    // should be — an expired session, not a malformed API. (Reported as an
    // expiry rather than "the API returned no run_id", which would send the
    // operator to the runs list to look for a sweep that was never submitted.)
    if (typeof body === 'string') {
      markSessionExpired();
      return { kind: 'session_expired', detail: SESSION_EXPIRED_MESSAGE };
    }
    const b = (body ?? {}) as Partial<SweepSubmitAccepted>;
    if (typeof b.run_id !== 'string') {
      return {
        kind: 'error',
        status: response.status,
        detail: 'The API accepted the sweep but returned no run_id.',
      };
    }
    return {
      kind: 'accepted',
      body: {
        run_id: b.run_id,
        status: (b.status ?? 'submitted') as SweepSubmitAccepted['status'],
        deduplicated_to: b.deduplicated_to ?? null,
        prior_done_run_id: b.prior_done_run_id ?? null,
      },
    };
  }

  const detail = detailOf(body, `HTTP ${response.status} ${response.statusText}`.trim());
  switch (response.status) {
    // 401 is handled above (expiry, or the backend's own diagnostic). 403 is
    // the other thing entirely: signed in, verified, and not on the write
    // allowlist — which no reload will fix, so the server's message is shown.
    case 403:
      return { kind: 'unauthorized', status: response.status, detail };
    // The 409 body is `{detail}` only — the detail already names the blocking
    // run and why one sweep runs at a time, so there is nothing to dig out.
    case 409:
      return { kind: 'conflict', detail };
    case 422:
      return { kind: 'invalid', detail };
    case 502:
      return { kind: 'launch_failed', detail };
    case 503:
      return { kind: 'disabled', detail };
    default:
      return { kind: 'error', status: response.status, detail };
  }
}

// --------------------------------------------------------------------------- //
// Reads
// --------------------------------------------------------------------------- //

interface PollState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  /** The last read failed because the IAP session is gone, not because the API
   *  is broken. Rendered as its own state: the fix is a reload, not a retry. */
  sessionExpired: boolean;
}

/**
 * A non-401 HTTP failure, carrying the STATUS as well as the server's words.
 *
 * FC-096 Phase E PR-2 (§D-3, review item B3). `getJson` used to throw a bare
 * `Error` whose message was the detail, which was enough for a banner and not
 * enough for the artifact cache: "no artifact for this cell" (404, a normal
 * answer for an errored cell or a pre-Phase-B run, worth memoising on a
 * finished run) and "the bucket grant is missing" (502, must be retried on the
 * next mount) arrived indistinguishable. `message` is still the detail, so
 * every existing `catch (e) { e.message }` renders exactly what it did before.
 *
 * `SessionExpiredError` and `UnauthorizedError` are NOT subclasses of this and
 * are thrown ahead of it, unchanged — the 401 family has its own remedy and
 * `isSessionExpired`/`isNoRetry` must keep classifying them.
 */
export class HttpError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'HttpError';
    this.status = status;
    this.detail = detail;
  }
}

/** `true` for an `HttpError` with this exact status. Narrows for the caller. */
export const isHttpStatus = (err: unknown, status: number): boolean =>
  err instanceof HttpError && err.status === status;

export async function getJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal, headers: IAP_XHR_HEADERS });
  // Body first, verdict second — see `submitSweep`. IAP's 401 is a sign-out;
  // the backend's carries a diagnostic that must reach the screen intact.
  if (response.status === 401) {
    throw unauthorizedError(response, await response.text().catch(() => ''));
  }
  if (!response.ok) {
    const body = await readBody(response);
    throw new HttpError(
      response.status,
      detailOf(body, `HTTP ${response.status} ${response.statusText}`.trim()),
    );
  }
  // NOT `response.json()`: an expired IAP session can answer 200 with the
  // sign-in page's HTML, and `json()` would report that as a syntax error at
  // character 0 — a message that sends the reader to the API instead of to the
  // sign-in button.
  const text = await response.text();
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new SessionExpiredError();
  }
}

/**
 * Fetch `url` once, then every `SWEEP_POLL_MS` while there is a reason to.
 *
 * Three behaviours worth stating, because each fixes a way this page could lie:
 *
 *   * On a URL CHANGE — and ONLY a url change — the state is cleared to
 *     `{null, loading: true, null}` before the new request goes out. Without
 *     that, selecting run B showed run A's grid under B's heading until B
 *     answered: the worst kind of wrong, because it looks right. A manual
 *     `refetch` of the SAME url keeps the rendered data and only marks it
 *     loading; clearing there would blank the runs list on every submit.
 *   * A read failure keeps the last good data and reports the error beside it,
 *     and keeps polling. A transient 500 must neither blank a rendered grid nor
 *     permanently stop the refresh.
 *   * An IN-FLIGHT GUARD stops the interval stacking requests. A read slower
 *     than 15s would otherwise queue one per tick against a backend that is
 *     already struggling.
 */
function usePolledGet<T>(
  url: string | null,
  /** Whether a SUCCESSFUL read should be polled again. */
  shouldPoll: (data: T) => boolean,
  /** Adapts the wire payload. A null result is reported as a shape error. */
  normalise?: (raw: unknown) => T | null,
): PollState<T> & { refetch: () => void } {
  const [state, setState] = useState<PollState<T>>({
    data: null,
    loading: !!url,
    error: null,
    sessionExpired: false,
  });
  const [tick, setTick] = useState(0);
  const mounted = useRef(true);
  // Read inside the interval callback so the timer never closes over stale data.
  const shouldPollRef = useRef(shouldPoll);
  shouldPollRef.current = shouldPoll;
  const normaliseRef = useRef(normalise);
  normaliseRef.current = normalise;
  const stateRef = useRef(state);
  stateRef.current = state;
  // The url the current data belongs to. The effect below re-runs for two very
  // different reasons — a new url, or a manual refetch — and only the first may
  // throw away what is on screen.
  const loadedUrlRef = useRef<string | null | undefined>(undefined);

  const refetch = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!url) {
      loadedUrlRef.current = null;
      setState({ data: null, loading: false, error: null, sessionExpired: false });
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    let inFlight = false;

    // A NEW url: clear first, because nothing from the previous one may survive
    // under it — run A's grid under run B's heading is the worst kind of wrong,
    // since it looks right.
    //
    // The SAME url (a manual `refetch`, which bumps `tick` and re-runs this
    // effect): keep what is on screen and only mark it loading. Blanking here
    // would drop the runs list to "Loading..." the instant the operator submits
    // a sweep — `Simulations` refetches the list on every accepted submit — and
    // would lose the rows entirely if that read then failed, which is exactly
    // what the keep-last-good-data rule below exists to prevent.
    if (loadedUrlRef.current !== url) {
      loadedUrlRef.current = url;
      setState({ data: null, loading: true, error: null, sessionExpired: false });
    } else {
      setState((prev) => ({ ...prev, loading: true }));
    }

    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const raw = await getJson<unknown>(url, controller.signal);
        const adapt = normaliseRef.current;
        const data = adapt ? adapt(raw) : (raw as T);
        if (data === null && adapt) {
          throw new Error('The API returned a sweep payload this build cannot read.');
        }
        if (!cancelled && mounted.current)
          setState({ data, loading: false, error: null, sessionExpired: false });
      } catch (err) {
        if (cancelled || !mounted.current) return;
        const e = err as Error;
        if (e.name === 'AbortError') return;
        // The layout polls `/api/live/account` once a MINUTE; this polls every
        // 15s. Marking the tab-wide signal is what puts the banner up now
        // rather than up to 45s from now, behind three unexplained errors.
        if (isSessionExpired(e)) markSessionExpired();
        setState((prev) => ({
          data: prev.data,
          loading: false,
          error: e.message,
          sessionExpired: isSessionExpired(e),
        }));
      } finally {
        inFlight = false;
      }
    };

    void load();

    const interval = setInterval(() => {
      const { data, error, sessionExpired } = stateRef.current;
      // A signed-out session is the ONE failure worth stopping for. Every other
      // error keeps polling, because it may be transient; this one cannot
      // recover in this page's lifetime — the operator has to reload and sign
      // in — so polling on would be four requests a minute that can only fail,
      // for as long as the tab is open.
      if (sessionExpired) return;
      // Nothing loaded yet, or the last read failed: keep trying. Only a
      // SUCCESSFUL read gets to say "this is final, stop asking".
      if (data !== null && !error && !shouldPollRef.current(data)) return;
      void load();
    }, SWEEP_POLL_MS);

    return () => {
      cancelled = true;
      controller.abort();
      clearInterval(interval);
    };
  }, [url, tick]);

  return { ...state, refetch };
}

const anyLive = (rows: SweepRow[]): boolean =>
  rows.some((s) => !isTerminalSweepStatus(s.status));

/**
 * Recent sweeps. Polls while at least one is non-terminal — and while the list
 * has never loaded or its last read failed.
 *
 * `GET /api/v2/sweeps` serves a BARE ARRAY; `normaliseSweepList` is what keeps
 * an envelope change from silently emptying the page.
 */
export function useSweepList() {
  return usePolledGet<SweepRow[]>('/api/v2/sweeps', anyLive, normaliseSweepList);
}

/**
 * One sweep + its report. Polls only while THAT sweep is non-terminal.
 *
 * The response is a bare `shape_results` payload; `normaliseSweepDetail` splits
 * it into the run row, the report (only when `done`) and the raw payload the
 * export button serves.
 */
export function useSweepDetail(runId: string | null) {
  return usePolledGet<SweepDetail>(
    runId ? `/api/v2/sweeps/${encodeURIComponent(runId)}` : null,
    (d) => !isTerminalSweepStatus(d.sweep?.status),
    normaliseSweepDetail,
  );
}

/** The allowlist, caps and presets. Static per deploy — fetched once. */
export function useSweepAllowlist() {
  return usePolledGet<SweepAllowlist>('/api/v2/sweeps/allowlist', () => false);
}

// --------------------------------------------------------------------------- //
// The sim proxy and the pin (FC-096 Phase E PR-4)
// --------------------------------------------------------------------------- //
//
// `POST /api/v2/sims/run` is a PROXY (`routers/v2.py:948`): it hands back the
// sim service's own status and body untouched, so everything below reads the
// service's contract (`deploy/sim_service.py`), not the dashboard's:
//
//   200 {run_id, deduplicated: true}  an identical spec already completed
//   202 {run_id, status, ...}         accepted; poll GET /api/v2/sweeps/{id}
//   409                               busy | coverage | budget | other
//   422                               the spec was refused, with the reason
//   502                               token/network — includes the cold tail
//
// The proxy waits 150 s, which is longer than this module's 20 s read timeout,
// so the sim submit gets its own: aborting at 20 s on a request the service is
// still working on produces exactly the failure the proxy's own comment names —
// the operator resubmits and the second attempt 409s against the first.

export const SIM_REQUEST_TIMEOUT_MS = 160_000;

/**
 * The sentence the 502 branch adds to the server's own words.
 *
 * The proxy's network-failure 502 already explains the cold tail; its
 * token-mint 502 does not, and both arrive here. Rendering the detail verbatim
 * AND this sentence means the operator is never left with "502" and no next
 * move, and never has the server's IAM diagnostic paraphrased away.
 */
export const SIM_COLD_START_NOTE =
  'The sim service is scale-to-zero with a measured cold-start tail of up to ~4 minutes, ' +
  'which is longer than the proxy’s 150 s timeout — so a FIRST submit after an idle period ' +
  'can land here having woken the instance anyway. Retrying is the right move: the instance ' +
  'this attempt woke is warm now. Whether this attempt RAN is not knowable from here — ' +
  'Cloud Run queues the request, so the service can still accept it after the proxy gave up. ' +
  'If the first attempt landed, Retry answers 409 and links that run.';

/** The four shapes a `/simulate` 409 takes. */
export type SimConflictKind = 'busy' | 'coverage' | 'budget' | 'generic';

export interface SimConflict {
  kind: SimConflictKind;
  /** The service's own words, always. */
  detail: string;
  /** The blocking run, when the body names one. `null` is a real answer here:
   *  the instance-lock 409 sends `run_id: null` when the current run is gone. */
  runId: string | null;
  /** Coverage only: how many symbol-days the lake is missing. */
  missingSymbolDays: number | null;
  /** Coverage only: `{SYMBOL: [ISO dates]}` as the service listed them. */
  missing: Record<string, string[]> | null;
}

const asRecord = (body: unknown): Record<string, unknown> | null =>
  body && typeof body === 'object' && !Array.isArray(body)
    ? (body as Record<string, unknown>)
    : null;

/**
 * Which 409 this is, by KEY PRESENCE — the deploy smoke's rule
 * (`cloudbuild.yaml:1264-1272`), which is the only rule that works.
 *
 * The bodies carry no `kind` discriminator, so the fields are the
 * discriminator: the busy 409s carry `run_id` (`sim_service.py:1081,1097`), the
 * coverage 409 carries `missing_symbol_days` (`:586`), and the two budget 409s
 * carry `max_cells` (`:551`) or `total_seconds` (`:560`). Presence, never
 * VALUE: the instance-lock 409 sets `run_id` to `null` when `_CURRENT` has
 * already been cleared, and a truthiness test would file that as "generic" and
 * tell the operator nothing about the replay that is blocking them.
 */
export function classifySimConflict(body: unknown): SimConflict {
  const record = asRecord(body);
  const detail = detailOf(body, 'The sim service refused this spec (409).');
  const base: SimConflict = {
    kind: 'generic',
    detail,
    runId: null,
    missingSymbolDays: null,
    missing: null,
  };
  if (!record) return base;
  if ('run_id' in record) {
    const raw = record.run_id;
    return { ...base, kind: 'busy', runId: typeof raw === 'string' && raw ? raw : null };
  }
  if ('missing_symbol_days' in record) {
    const days = record.missing_symbol_days;
    const missing = asRecord(record.missing);
    const listed: Record<string, string[]> = {};
    if (missing) {
      for (const [symbol, value] of Object.entries(missing)) {
        if (Array.isArray(value)) {
          listed[symbol] = value.filter((d): d is string => typeof d === 'string');
        }
      }
    }
    return {
      ...base,
      kind: 'coverage',
      missingSymbolDays: typeof days === 'number' ? days : null,
      missing: Object.keys(listed).length > 0 ? listed : null,
    };
  }
  if ('max_cells' in record || 'total_seconds' in record) {
    return { ...base, kind: 'budget' };
  }
  return base;
}

/** What came back from the ONE POST to `/api/v2/sims/run`. */
export type SimSubmitOutcome =
  /** 202 — accepted and running on the service's worker thread. Poll the run. */
  | { kind: 'accepted'; runId: string; cellCount: number | null }
  /** 200 — an identical spec had already completed. NOTHING was replayed. */
  | { kind: 'deduplicated'; runId: string }
  | { kind: 'session_expired'; detail: string }
  | { kind: 'unauthenticated'; detail: string }
  /** 403 — a verified identity that is not an operator. Shown, not hidden. */
  | { kind: 'unauthorized'; detail: string }
  | { kind: 'conflict'; conflict: SimConflict }
  /** 422 — the spec was refused; `detail` is the service's reason, verbatim. */
  | { kind: 'invalid'; detail: string }
  /** 502 — token or network, including the cold-start tail. Retryable. */
  | { kind: 'unreachable'; detail: string }
  /** 503 — `SIM_SERVICE_URL` unset on this revision, or the service refused. */
  | { kind: 'disabled'; detail: string }
  | { kind: 'error'; status: number | null; detail: string };

/**
 * POST the spec to the sim proxy. EXACTLY ONE request, no retry, ever.
 *
 * Same contract as `submitSweep` and for a sharper reason: this endpoint takes
 * a process-wide lock on the sim service, so an automatic second attempt would
 * 409 against the first and report the operator's own submit as a conflict.
 */
export async function submitSim(spec: SweepSpec): Promise<SimSubmitOutcome> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SIM_REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch('/api/v2/sims/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...IAP_XHR_HEADERS },
      body: JSON.stringify(spec),
      signal: controller.signal,
    });
  } catch (err) {
    const e = err as Error;
    return {
      kind: 'error',
      status: null,
      detail:
        e.name === 'AbortError'
          ? `The submit did not answer within ${SIM_REQUEST_TIMEOUT_MS / 1000}s. It was NOT ` +
            'retried, and whether it ran is not knowable from here — the request may still be ' +
            'queued. If it landed, a Retry answers 409 and links that run; the runs list shows it too.'
          : e.message || 'The submit could not be sent.',
    };
  } finally {
    clearTimeout(timer);
  }

  // Body first, verdict second — IAP's 401 is HTML, the backend's is JSON with
  // a diagnostic that names its own repair (`submitSweep`, review round 1 F3).
  if (response.status === 401) {
    const err = unauthorizedError(response, await response.text().catch(() => ''));
    if (isSessionExpired(err)) {
      markSessionExpired();
      return { kind: 'session_expired', detail: SESSION_EXPIRED_MESSAGE };
    }
    return { kind: 'unauthenticated', detail: err.message };
  }

  const body = await readBody(response);

  if (response.ok) {
    if (typeof body === 'string') {
      // A 2xx whose body will not parse is the sign-in page, not the API.
      markSessionExpired();
      return { kind: 'session_expired', detail: SESSION_EXPIRED_MESSAGE };
    }
    const record = asRecord(body) ?? {};
    const runId = typeof record.run_id === 'string' ? record.run_id : null;
    if (!runId) {
      return {
        kind: 'error',
        status: response.status,
        detail: 'The sim service answered without a run_id, so there is nothing to open.',
      };
    }
    // 200 and 202 are DIFFERENT ANSWERS and the status is what separates them.
    // A 200 replayed nothing: it is the prior run's id, and treating it as an
    // acceptance would poll a finished run and call a stored answer new work.
    if (response.status === 200) return { kind: 'deduplicated', runId };
    if (response.status === 202) {
      const cells = record.cell_count;
      return { kind: 'accepted', runId, cellCount: typeof cells === 'number' ? cells : null };
    }
    return {
      kind: 'error',
      status: response.status,
      detail: `The sim service answered ${response.status}, which is neither a dedup hit (200) nor an acceptance (202).`,
    };
  }

  const detail = detailOf(body, `HTTP ${response.status} ${response.statusText}`.trim());
  switch (response.status) {
    case 403:
      return { kind: 'unauthorized', detail };
    case 409:
      return { kind: 'conflict', conflict: classifySimConflict(body) };
    case 422:
      return { kind: 'invalid', detail };
    case 502:
      return { kind: 'unreachable', detail };
    case 503:
      return { kind: 'disabled', detail };
    default:
      return { kind: 'error', status: response.status, detail };
  }
}

/** What came back from the ONE POST to `/api/v2/sims/pins`. */
export type PinOutcome =
  /** 201 — pinned. `detail` carries the rolling-window echo the route returns. */
  | { kind: 'created'; pinId: string; windowDays: number | null; holdoutDays: number | null }
  /** 409 — already pinned, or the active cap. The body NAMES which; verbatim. */
  | { kind: 'conflict'; detail: string }
  | { kind: 'invalid'; detail: string }
  | { kind: 'session_expired'; detail: string }
  | { kind: 'unauthenticated'; detail: string }
  | { kind: 'unauthorized'; detail: string }
  | { kind: 'error'; status: number | null; detail: string };

/**
 * Pin a spec into the weekly battery. `{spec, note}`, one request, no retry.
 *
 * `DELETE` is deliberately NOT offered here (plan §PR-4): un-pinning is a
 * decision about the battery's standing cost, not a step in reading one cell.
 */
export async function pinSpec(spec: SweepSpec, note: string): Promise<PinOutcome> {
  // A blank note is sent as NO note. The route takes `{spec, note}` with `note`
  // optional, and filling it with the derived arm name would put a string the
  // operator never wrote into the pin list as if it were their annotation.
  const trimmed = note.trim();
  const pinBody: Record<string, unknown> = trimmed ? { spec, note: trimmed } : { spec };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch('/api/v2/sims/pins', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...IAP_XHR_HEADERS },
      body: JSON.stringify(pinBody),
      signal: controller.signal,
    });
  } catch (err) {
    const e = err as Error;
    return {
      kind: 'error',
      status: null,
      detail:
        e.name === 'AbortError'
          ? 'The pin did not answer in time. It was NOT retried — check GET /api/v2/sims/pins.'
          : e.message || 'The pin could not be sent.',
    };
  } finally {
    clearTimeout(timer);
  }

  if (response.status === 401) {
    const err = unauthorizedError(response, await response.text().catch(() => ''));
    if (isSessionExpired(err)) {
      markSessionExpired();
      return { kind: 'session_expired', detail: SESSION_EXPIRED_MESSAGE };
    }
    return { kind: 'unauthenticated', detail: err.message };
  }

  const body = await readBody(response);

  if (response.ok) {
    if (typeof body === 'string') {
      markSessionExpired();
      return { kind: 'session_expired', detail: SESSION_EXPIRED_MESSAGE };
    }
    const record = asRecord(body) ?? {};
    const pinId = typeof record.pin_id === 'string' ? record.pin_id : null;
    if (!pinId) {
      return {
        kind: 'error',
        status: response.status,
        detail: 'The pin was accepted but came back without a pin_id.',
      };
    }
    return {
      kind: 'created',
      pinId,
      windowDays: typeof record.window_days === 'number' ? record.window_days : null,
      holdoutDays: typeof record.holdout_days === 'number' ? record.holdout_days : null,
    };
  }

  const detail = detailOf(body, `HTTP ${response.status} ${response.statusText}`.trim());
  switch (response.status) {
    case 403:
      return { kind: 'unauthorized', detail };
    case 409:
      return { kind: 'conflict', detail };
    case 422:
      return { kind: 'invalid', detail };
    default:
      return { kind: 'error', status: response.status, detail };
  }
}
