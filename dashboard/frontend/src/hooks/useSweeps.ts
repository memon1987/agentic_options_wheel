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

export const SWEEP_POLL_MS = 15_000;
const REQUEST_TIMEOUT_MS = 20_000;

/** Where the operator's token lives. sessionStorage: gone when the tab closes. */
export const SWEEP_TOKEN_STORAGE_KEY = 'fc060.sweepSubmitToken';

export const readStoredToken = (): string => {
  try {
    return window.sessionStorage.getItem(SWEEP_TOKEN_STORAGE_KEY) ?? '';
  } catch {
    // Safari private mode throws on sessionStorage access. An unreachable store
    // means "no token remembered", never a crashed page.
    return '';
  }
};

export const writeStoredToken = (token: string): void => {
  try {
    if (token) window.sessionStorage.setItem(SWEEP_TOKEN_STORAGE_KEY, token);
    else window.sessionStorage.removeItem(SWEEP_TOKEN_STORAGE_KEY);
  } catch {
    /* see readStoredToken */
  }
};

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
  /** 401/403 — the token is missing or wrong. */
  | { kind: 'unauthorized'; status: number; detail: string }
  /** 409 — another sweep is already in flight. The detail names it. */
  | { kind: 'conflict'; detail: string }
  /** 422 — the spec was refused. `detail` is the runner's reason, verbatim. */
  | { kind: 'invalid'; detail: string }
  /** 502 — the Job could not be launched (usually an IAM grant). */
  | { kind: 'launch_failed'; detail: string }
  /** 503 — `SWEEP_SUBMIT_TOKEN` is not configured; submits are fail-closed off. */
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
 * Exported as a plain function (not a hook) so the no-retry contract can be
 * tested directly against a fetch spy.
 */
export async function submitSweep(spec: SweepSpec, token: string): Promise<SubmitOutcome> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch('/api/v2/sweeps', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
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

  const body = await readBody(response);

  if (response.ok) {
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
      },
    };
  }

  const detail = detailOf(body, `HTTP ${response.status} ${response.statusText}`.trim());
  switch (response.status) {
    case 401:
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
}

async function getJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { signal });
  if (!response.ok) {
    const body = await readBody(response);
    throw new Error(detailOf(body, `HTTP ${response.status} ${response.statusText}`.trim()));
  }
  return (await response.json()) as T;
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
  const [state, setState] = useState<PollState<T>>({ data: null, loading: !!url, error: null });
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
      setState({ data: null, loading: false, error: null });
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
      setState({ data: null, loading: true, error: null });
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
        if (!cancelled && mounted.current) setState({ data, loading: false, error: null });
      } catch (err) {
        if (cancelled || !mounted.current) return;
        const e = err as Error;
        if (e.name === 'AbortError') return;
        setState((prev) => ({ data: prev.data, loading: false, error: e.message }));
      } finally {
        inFlight = false;
      }
    };

    void load();

    const interval = setInterval(() => {
      const { data, error } = stateRef.current;
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
