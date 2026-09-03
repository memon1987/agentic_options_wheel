/**
 * Global error reporter — posts frontend errors to the backend.
 * Also installs window.onerror and window.onunhandledrejection handlers.
 */

const ERROR_ENDPOINT = '/api/errors';

interface ErrorReport {
  message: string;
  source?: string;
  lineno?: number;
  colno?: number;
  stack?: string;
  timestamp: string;
  url: string;
  userAgent: string;
}

export function reportError(error: ErrorReport): void {
  try {
    // Fire-and-forget POST — we don't want error reporting to itself cause issues
    fetch(ERROR_ENDPOINT, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // FC-096 Phase D: behind IAP, an expired session answers a plain fetch
        // with a 302 to Google's sign-in page. This makes it a 401 instead, so
        // a report from a signed-out tab fails as one request rather than as a
        // cross-origin redirect chain. The sink itself stays ungated — the
        // crash report of a session that just expired is one worth having.
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify(error),
    }).catch(() => {
      // Silently swallow — cannot do anything if error reporting fails
    });
  } catch {
    // Silently swallow
  }
}

export function buildErrorReport(
  message: string,
  source?: string,
  lineno?: number,
  colno?: number,
  stack?: string,
): ErrorReport {
  return {
    message,
    source,
    lineno,
    colno,
    stack,
    timestamp: new Date().toISOString(),
    url: window.location.href,
    userAgent: navigator.userAgent,
  };
}

/**
 * Call once at app startup to install global handlers.
 */
export function installGlobalErrorHandlers(): void {
  window.onerror = (
    message: string | Event,
    source?: string,
    lineno?: number,
    colno?: number,
    error?: Error,
  ) => {
    const msg = typeof message === 'string' ? message : 'Unknown error';
    reportError(buildErrorReport(msg, source, lineno, colno, error?.stack));
  };

  window.onunhandledrejection = (event: PromiseRejectionEvent) => {
    const reason = event.reason;
    const message =
      reason instanceof Error ? reason.message : String(reason ?? 'Unhandled promise rejection');
    const stack = reason instanceof Error ? reason.stack : undefined;
    reportError(buildErrorReport(message, 'unhandledrejection', undefined, undefined, stack));
  };
}
