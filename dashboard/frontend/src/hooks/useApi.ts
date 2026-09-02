import { useState, useEffect, useCallback, useRef } from 'react';

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
  refetch: () => void;
}

const TIMEOUT_MS = 15_000;
const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 2_000;

/**
 * FC-096 Phase D: sent on every read.
 *
 * The dashboard sits behind Identity-Aware Proxy, which answers an expired
 * session with a 302 to Google's sign-in page. A `fetch` follows that to
 * another origin and this hook gets a CORS failure or a page of HTML — read as
 * "the API is down". With this header IAP answers 401 instead, which is
 * recognisable and is reported as a signed-out session rather than an outage.
 */
const IAP_XHR_HEADERS = { 'X-Requested-With': 'XMLHttpRequest' } as const;

/** What the operator is told when their IAP session is gone. */
export const SESSION_EXPIRED_MESSAGE =
  'Session expired — reload the page to sign in again.';

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

/** A signed-out session, in the three shapes it can arrive in. */
const looksSignedOut = (response: Response): boolean =>
  response.status === 401 ||
  response.status === 0 ||
  response.type === 'opaque' ||
  response.type === 'opaqueredirect';

class SessionExpiredError extends Error {
  readonly sessionExpired = true;
  constructor() {
    super(SESSION_EXPIRED_MESSAGE);
    this.name = 'SessionExpiredError';
  }
}

async function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchWithRetry<T>(url: string): Promise<T> {
  let lastError: Error | null = null;

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      const response = await fetchWithTimeout(url, TIMEOUT_MS);
      if (looksSignedOut(response)) throw new SessionExpiredError();
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
    } catch (err) {
      lastError = err instanceof Error ? err : new Error(String(err));

      if (lastError.name === 'AbortError') {
        lastError = new Error(`Request timed out after ${TIMEOUT_MS / 1000}s`);
      }

      // A signed-out session is not transient: three retries two seconds apart
      // cannot sign anyone back in, and each one is another failed request.
      if (lastError.name === 'SessionExpiredError') throw lastError;

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
      }
    } catch (err) {
      if (mountedRef.current && urlRef.current === url) {
        const message = err instanceof Error ? err.message : 'An unknown error occurred';
        setError(message);
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

  useEffect(() => {
    if (refreshInterval <= 0 || !url) return;

    const interval = setInterval(() => {
      if (mountedRef.current) {
        fetchData();
      }
    }, refreshInterval);

    return () => clearInterval(interval);
  }, [fetchData, refreshInterval, url]);

  return { data, loading, error, refetch: fetchData };
}
