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
// Polling: 15s, and only while a run is non-terminal. A finished sweep is
// insert-only history; re-reading it forever would be a standing BigQuery bill
// for a page left open on a monitor.

import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  SweepAllowlist,
  SweepDetail,
  SweepListResponse,
  SweepRow,
  SweepSpec,
  SweepSubmitAccepted,
} from '../types/v2';
import { isTerminalSweepStatus } from '../types/v2';
import { normaliseSweepDetail } from '../components/v2/sims/normaliseReport';

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
  /** 409 — another sweep is already in flight. */
  | { kind: 'conflict'; runningRunId: string | null; detail: string }
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
      return { kind: 'error', status: response.status, detail: 'The API accepted the sweep but returned no run_id.' };
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
    case 409: {
      const running =
        body && typeof body === 'object'
          ? ((body as { running_run_id?: unknown }).running_run_id ??
             (body as { detail?: { running_run_id?: unknown } }).detail?.running_run_id)
          : undefined;
      return {
        kind: 'conflict',
        runningRunId: typeof running === 'string' ? running : null,
        detail,
      };
    }
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
 * Fetch `url` once, then every `SWEEP_POLL_MS` for as long as `shouldPoll` says
 * the data is still live. A single GET failure keeps the last good data and
 * shows the error beside it — a transient 500 must not blank a rendered grid.
 */
function usePolledGet<T>(
  url: string | null,
  shouldPoll: (data: T | null) => boolean,
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
  const dataRef = useRef<T | null>(null);
  dataRef.current = state.data;

  const refetch = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!url) {
      setState({ data: null, loading: false, error: null });
      return;
    }
    const controller = new AbortController();
    let cancelled = false;

    const load = async () => {
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
      }
    };

    setState((prev) => ({ ...prev, loading: prev.data === null }));
    void load();

    const interval = setInterval(() => {
      // The whole point of the hook: a terminal run is history, so stop asking.
      if (!shouldPollRef.current(dataRef.current)) return;
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

const anyLive = (list: SweepListResponse | null): boolean =>
  (list?.sweeps ?? []).some((s) => !isTerminalSweepStatus(s.status));

/** Recent sweeps. Polls only while at least one of them is non-terminal. */
export function useSweepList() {
  return usePolledGet<SweepListResponse>('/api/v2/sweeps', anyLive);
}

/**
 * One sweep + its report. Polls only while THAT sweep is non-terminal.
 *
 * The response runs through `normaliseSweepDetail` because the API may send
 * either `{sweep, results}` with the Layer 2 `render_json` payload or a bare
 * `shape_results` payload (plan D11). See `normaliseReport.ts` for why both are
 * accepted rather than one being picked.
 */
export function useSweepDetail(runId: string | null) {
  return usePolledGet<SweepDetail>(
    runId ? `/api/v2/sweeps/${encodeURIComponent(runId)}` : null,
    (d) => !isTerminalSweepStatus(d?.sweep?.status),
    normaliseSweepDetail,
  );
}

/** The allowlist, caps and presets. Static per deploy — fetched once, never polled. */
export function useSweepAllowlist() {
  return usePolledGet<SweepAllowlist>('/api/v2/sweeps/allowlist', () => false);
}

/**
 * A `submitted` row older than this with no `running` row means the execution
 * never came up. Container start is 3-4 minutes, so 10 is a real signal rather
 * than impatience (plan D3). It is a HINT: nothing is cancelled automatically.
 */
export const STUCK_AFTER_MS = 10 * 60_000;

export function isStuck(row: SweepRow, now: number = Date.now()): boolean {
  if (row.status !== 'submitted') return false;
  const t = Date.parse(row.submitted_at);
  if (Number.isNaN(t)) return false;
  return now - t > STUCK_AFTER_MS;
}
