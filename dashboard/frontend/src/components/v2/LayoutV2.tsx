import { useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useApi } from '../../hooks/useApi';
import { SESSION_EXPIRED_MESSAGE, useSessionExpiredSignal } from '../../hooks/iapSession';
import { useSessionRefresh } from '../../hooks/iapRefresh';

interface AccountData {
  paper_trading?: boolean;
}

const navItems = [
  { path: '/overview', label: 'Overview' },
  { path: '/symbol', label: 'By Symbol' },
  { path: '/bot-health', label: 'Bot Health' },
  // FC-060 Layer 4. Last in the list on purpose: every page above it shows what
  // the bot DID; this one shows what a config variant WOULD have done.
  { path: '/sims', label: 'Sims' },
];

export default function LayoutV2() {
  const [menuOpen, setMenuOpen] = useState(false);
  const { data: account, sessionExpired: accountSessionExpired } = useApi<AccountData>(
    '/api/live/account',
    { refreshInterval: 60_000 },
  );

  // FC-031: default to PAPER when the flag is missing — the safe direction.
  const isPaperTrading = account?.paper_trading ?? true;

  // FC-096 Phase D PR-2 (review round 1, F1): the ONE session-expiry banner,
  // here because this layout wraps EVERY route — the /sims page having its own
  // was why the other three pages had none and polled on for ever.
  //
  // Two sources, deliberately:
  //   * this layout's own poll, which is the guarantee — the banner does not
  //     depend on any other hook being wired up;
  //   * the tab-wide signal, which is the LATENCY — this poll runs once a
  //     minute and the /sims hooks run every 15s, so without it the banner
  //     could trail the page's own failures by up to 45 seconds.
  const signalled = useSessionExpiredSignal();
  const sessionExpired = accountSessionExpired || signalled;

  // FC-096 Phase E PR-6: recover in place instead of reloading. "Reload" stays
  // as the fallback — it is the only remedy that survives a blocked popup, and
  // it is the one an operator already knows works.
  const { running, outcome, start } = useSessionRefresh();
  const refreshNote = running
    ? 'Opening Google sign-in. Finish signing in in the popup window — this page picks up on its own, no reload needed.'
    : outcome === 'blocked'
      ? 'Your browser blocked the sign-in popup. Allow popups for this site and try again, or use Reload.'
      : outcome === 'closed'
        ? 'The sign-in window closed before the session came back. Try again, or use Reload.'
        : outcome === 'timeout'
          ? 'Five minutes passed and the session has not come back. The sign-in window is still open — finish signing in there, or use Reload.'
          : null;

  return (
    <div className="min-h-screen bg-gray-900">
      {/* Mobile Header */}
      <header className="lg:hidden fixed top-0 left-0 right-0 z-50 bg-gray-800 border-b border-gray-700">
        <div className="flex items-center justify-between px-4 py-3">
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-bold text-white">Options Wheel</h1>
            {isPaperTrading && (
              <span className="px-2 py-0.5 bg-yellow-900 text-yellow-300 text-xs font-medium rounded">
                PAPER
              </span>
            )}
          </div>
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className="p-2 text-gray-400 hover:text-white"
            aria-label="Toggle menu"
          >
            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              {menuOpen ? (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
              )}
            </svg>
          </button>
        </div>

        {menuOpen && (
          <nav className="px-4 py-2 bg-gray-800 border-t border-gray-700">
            {navItems.map((item) => (
              <NavLink
                key={item.path}
                to={item.path}
                onClick={() => setMenuOpen(false)}
                className={({ isActive }) =>
                  `block py-3 px-4 rounded-lg mb-1 transition-colors ${
                    isActive ? 'bg-blue-600 text-white' : 'text-gray-300 hover:bg-gray-700'
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        )}
      </header>

      {/* Desktop Sidebar */}
      <aside className="hidden lg:flex fixed top-0 left-0 h-full w-60 bg-gray-800 border-r border-gray-700 flex-col">
        <div className="px-6 py-5 border-b border-gray-700">
          <div className="flex flex-col gap-1">
            <h1 className="text-xl font-bold text-white">Options Wheel</h1>
            {isPaperTrading && (
              <span className="px-2 py-0.5 bg-yellow-900 text-yellow-300 text-xs font-medium rounded w-fit mt-1">
                PAPER
              </span>
            )}
          </div>
        </div>

        <nav className="flex-1 px-4 py-4">
          {navItems.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) =>
                `flex items-center py-3 px-4 rounded-lg mb-1 transition-colors ${
                  isActive ? 'bg-blue-600 text-white' : 'text-gray-300 hover:bg-gray-700'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* Main Content */}
      <main className="lg:pl-60 pt-16 lg:pt-0">
        <div className="p-4 lg:p-6 max-w-7xl">
          {sessionExpired && (
            <section
              data-testid="session-expired-banner"
              role="alert"
              className="mb-6 rounded-lg border border-yellow-700/70 bg-yellow-950/30 p-4 flex items-center justify-between gap-4 flex-wrap"
            >
              <div>
                <p className="text-sm font-medium text-yellow-300">{SESSION_EXPIRED_MESSAGE}</p>
                <p className="text-xs text-yellow-200/80 mt-1">
                  Nothing on this page is refreshing any more. Refreshing the session signs you back
                  in through Google in a popup and keeps everything on this page; reloading also
                  works and starts over.
                </p>
                {refreshNote && (
                  <p data-testid="session-refresh-note" className="text-xs text-yellow-200/80 mt-1">
                    {refreshNote}
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  data-testid="session-refresh-button"
                  onClick={start}
                  disabled={running}
                  className="px-3 py-1.5 rounded text-xs font-medium bg-blue-600 hover:bg-blue-500 disabled:bg-blue-900 disabled:text-blue-300 text-white"
                >
                  {running ? 'Signing in…' : 'Refresh session'}
                </button>
                <button
                  type="button"
                  onClick={() => window.location.reload()}
                  className="px-3 py-1.5 rounded text-xs font-medium bg-yellow-700 hover:bg-yellow-600 text-white"
                >
                  Reload
                </button>
              </div>
            </section>
          )}
          <Outlet />
        </div>
      </main>
    </div>
  );
}
