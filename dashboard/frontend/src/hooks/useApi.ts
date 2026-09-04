import { useState, useEffect, useCallback, useRef } from 'react';
import {
  IAP_XHR_HEADERS,
  SessionExpiredError,
  isNoRetry,
  isSessionExpired,
  markSessionExpired,
  unauthorizedError,
  useSessionGeneration,
} from './iapSession';

export { SESSION_EXPIRED_MESSAGE } from './iapSession';

interface UseApiOptions {
  /** Auto-refresh interval in milliseconds. 0 = no auto-refresh. Default: 0 */
  refreshInterval?: number;
  /** Whether to fetch immediately on mount. Default: true */
  immediate?: boolean;
}

interface UseApiResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  /**
   * The last read failed because the IAP session is gone, not because the API
   * is broken.
   *
   * FC-096 Phase D PR-2, review round 1: this hook already knew the difference
   * and kept it to itself — `error` was the only channel out, no caller read
   * it, and the refresh interval went on firing a request a minute that could
   * only fail. It is now a first-class return, the interval stops on it, and
   * `LayoutV2` (mounted on every route) renders the one banner.
   */
  sessionExpired: boolean;
  refetch: () => void;
}

const TIMEOUT_MS = 15_000;
const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 2_000;

async function fetchWithTimeout(url: string, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: IAP_XHR_HEADERS,
    });
    return response;
  } finally {
    clearTimeout(timer);
  }
}

async function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchOnce<T>(url: string): Promise<T> {
  const response = await fetchWithTimeout(url, TIMEOUT_MS);

  // A 401 is either IAP's (an expired session) or the backend's (a diagnostic
  // that names its own repair). Only the body can tell them apart, so it is
  // read BEFORE the verdict — the round-1 bug was deciding first.
  if (response.status === 401) {
    const text = await response.text().catch(() => '');
    throw unauthorizedError(response, text);
  }

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }

  // NOT `response.json()`: an expired session can answer 200 with the
  // sign-in page's HTML, and the parse error for that reads as an API bug.
  const text = await response.text();
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new SessionExpiredError();
  }
}

async function fetchWithRetry<T>(url: string): Promise<T> {
  let lastError: Error | null = null;

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      return await fetchOnce<T>(url);
    } catch (err) {
      lastError = err instanceof Error ? err : new Error(String(err));

      if (lastError.name === 'AbortError') {
        lastError = new Error(`Request timed out after ${TIMEOUT_MS / 1000}s`);
      }

      // Neither a signed-out session nor a 401 the backend chose is transient:
      // three retries two seconds apart cannot sign anyone back in and cannot
      // set an env var, and each one is another failed request.
      if (isNoRetry(lastError)) throw lastError;

      if (attempt < MAX_RETRIES) {
        await sleep(RETRY_DELAY_MS);
      }
    }
  }

  throw lastError ?? new Error('Fetch failed after retries');
}

export function useApi<T>(url: string | null, options: UseApiOptions = {}): UseApiResult<T> {
  const { refreshInterval = 0, immediate = true } = options;

  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(immediate && !!url);
  const [error, setError] = useState<string | null>(null);
  const [sessionExpired, setSessionExpired] = useState(false);

  const mountedRef = useRef(true);
  const urlRef = useRef(url);
  urlRef.current = url;

  const fetchData = useCallback(async () => {
    if (!url) {
      // Skip mode — pages pass null to suspend a request that depends on
      // a value (e.g., a route param) that isn't available yet.
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);

    try {
      const result = await fetchWithRetry<T>(url);
      if (mountedRef.current && urlRef.current === url) {
        setData(result);
        setError(null);
        setSessionExpired(false);
      }
    } catch (err) {
      if (mountedRef.current && urlRef.current === url) {
        const message = err instanceof Error ? err.message : 'An unknown error occurred';
        setError(message);
        if (isSessionExpired(err)) {
          setSessionExpired(true);
          // Tell the rest of the tab. `LayoutV2` polls once a MINUTE; the /sims
          // hooks poll every 15s, and without this the banner would trail the
          // page's own errors by up to 45 seconds.
          markSessionExpired();
        }
      }
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
    }
  }, [url]);

  useEffect(() => {
    mountedRef.current = true;

    if (immediate && url) {
      fetchData();
    } else if (!url) {
      // Clear stale data when URL becomes null (e.g., navigating away from a symbol).
      setData(null);
      setLoading(false);
    }

    return () => {
      mountedRef.current = false;
    };
  }, [fetchData, immediate, url]);

  // FC-096 Phase E PR-6: the way back from an expiry.
  //
  // The interval effect below tears the timer down when `sessionExpired` flips
  // true and never builds it again — correct in Phase D, where the only remedy
  // was a reload that destroyed this hook anyway. With the refresh popup there
  // is now a live tab in which the session came back and the hook is still
  // mounted, still expired, still not polling.
  //
  // The ref guard is what keeps this from firing on MOUNT: a hook mounted after
  // a refresh sees a non-zero generation on its first render and must not read
  // that as a change. Only hooks that actually stopped are restarted — one that
  // never expired is left exactly as it was, with no extra request.
  const generation = useSessionGeneration();
  const lastGenerationRef = useRef(generation);
  const sessionExpiredRef = useRef(sessionExpired);
  sessionExpiredRef.current = sessionExpired;

  useEffect(() => {
    if (lastGenerationRef.current === generation) return;
    lastGenerationRef.current = generation;
    if (!sessionExpiredRef.current) return;
    // Clear first, then refetch. Clearing re-arms the interval effect below;
    // the refetch is what puts data back on screen without waiting a whole
    // refresh interval for it. If the session is somehow gone again, this
    // fetch's own 401 sets the flag straight back.
    setSessionExpired(false);
    if (url) fetchData();
  }, [generation, fetchData, url]);

  useEffect(() => {
    // `sessionExpired` is BOTH a guard and a dep, and that is the whole of the
    // fix: the dep is what re-runs this effect the moment the flag flips, and
    // the guard is what makes the re-run tear the interval down instead of
    // building a new one. Polling on would be a request a minute that can only
    // fail, for as long as the tab stays open, behind a banner that already
    // says the only thing that helps.
    //
    // No second in-tick check on a ref. It was written and then removed: React
    // commits this effect in the same task as the state update, so a 60s (or
    // 15s) timer cannot fire in the gap. A guard that cannot be reached is the
    // same dead-branch-with-a-passing-test that F7 deleted elsewhere in this
    // PR, and it also hides whether the real guard works — with both in place,
    // deleting either one on its own left every test green.
    if (refreshInterval <= 0 || !url || sessionExpired) return;

    const interval = setInterval(() => {
      if (mountedRef.current) {
        fetchData();
      }
    }, refreshInterval);

    return () => clearInterval(interval);
  }, [fetchData, refreshInterval, url, sessionExpired]);

  return { data, loading, error, sessionExpired, refetch: fetchData };
}
