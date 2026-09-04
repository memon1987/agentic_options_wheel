// FC-096 Phase D PR-2, review round 1 (F1): the ONE session-expiry banner.
//
// It lives here rather than on /sims because this layout wraps every route.
// Before this fix the banner was on /sims only, so Overview, By Symbol and Bot
// Health polled a signed-out API for ever with nothing on screen to say so.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import LayoutV2 from './LayoutV2';
import {
  markSessionExpired,
  resetSessionExpiredSignal,
  resetSessionGeneration,
} from '../../hooks/iapSession';

let fetchMock: ReturnType<typeof vi.fn>;

const respond = (status: number, body: string, headers: Record<string, string> = {}): Response =>
  ({
    ok: status >= 200 && status < 300,
    status,
    statusText: '',
    type: 'basic',
    headers: new Headers(headers),
    text: async () => body,
    json: async () => JSON.parse(body),
  }) as unknown as Response;

const iapUnauthorized = () =>
  respond(401, '<html>Sign in</html>', { 'x-goog-iap-generated-response': 'true' });

const show = () =>
  render(
    <MemoryRouter initialEntries={['/overview']}>
      <Routes>
        <Route element={<LayoutV2 />}>
          <Route path="/overview" element={<div>the page</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => {
  resetSessionExpiredSignal();
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  // `vi.spyOn(window, 'open')` is not a global stub — without this the spy
  // leaks into every later test in this file.
  vi.restoreAllMocks();
  resetSessionExpiredSignal();
  resetSessionGeneration();
});

describe('LayoutV2 — the session-expiry banner', () => {
  it('renders it with a reload action when its own poll comes back signed out', async () => {
    fetchMock.mockResolvedValue(iapUnauthorized());
    show();
    const banner = await screen.findByTestId('session-expired-banner');
    expect(banner.textContent).toMatch(/Session expired/i);
    expect(screen.getByRole('button', { name: /reload/i })).toBeInTheDocument();
  });

  it('renders it when ANOTHER hook raises the tab-wide signal first', async () => {
    // The latency half of F1: this layout polls once a MINUTE, the /sims hooks
    // poll every 15s. Without the shared signal the banner would trail the
    // page's own errors by up to 45 seconds.
    fetchMock.mockResolvedValue(respond(200, JSON.stringify({ paper_trading: true })));
    show();
    await waitFor(() => expect(screen.getByText('the page')).toBeInTheDocument());
    expect(screen.queryByTestId('session-expired-banner')).toBeNull();
    act(() => markSessionExpired());
    expect(screen.getByTestId('session-expired-banner')).toBeInTheDocument();
  });

  it('renders no banner while the session is good', async () => {
    fetchMock.mockResolvedValue(respond(200, JSON.stringify({ paper_trading: true })));
    show();
    await waitFor(() => expect(screen.getByText('the page')).toBeInTheDocument());
    expect(screen.queryByTestId('session-expired-banner')).toBeNull();
  });

  it('renders no banner for an ordinary read failure — that is not a sign-out', async () => {
    fetchMock.mockResolvedValue(respond(500, JSON.stringify({ detail: 'boom' })));
    show();
    await waitFor(() => expect(screen.getByText('the page')).toBeInTheDocument());
    expect(screen.queryByTestId('session-expired-banner')).toBeNull();
  });

  it('shows exactly ONE banner, never a stack of them', async () => {
    fetchMock.mockResolvedValue(iapUnauthorized());
    show();
    await screen.findByTestId('session-expired-banner');
    act(() => markSessionExpired());
    expect(screen.getAllByTestId('session-expired-banner')).toHaveLength(1);
  });
});

// FC-096 Phase E PR-6. The button is the whole user-visible half of the PR: the
// handler is unreachable without it, and "Reload" has to survive beside it
// because a blocked popup leaves reloading as the only remedy that works.
describe('LayoutV2 — the Refresh session button', () => {
  it('appears only in the expired state, beside Reload', async () => {
    fetchMock.mockResolvedValue(respond(200, JSON.stringify({ paper_trading: true })));
    show();
    await waitFor(() => expect(screen.getByText('the page')).toBeInTheDocument());
    expect(screen.queryByTestId('session-refresh-button')).toBeNull();

    act(() => markSessionExpired());
    expect(screen.getByTestId('session-refresh-button')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /refresh session/i })).toBeInTheDocument();
    // The fallback stays. It is the only thing that works when the popup is
    // blocked, and it is the remedy the operator already knows.
    expect(screen.getByRole('button', { name: /^reload$/i })).toBeInTheDocument();
  });

  it("opens Google's refresh URL in a popup when clicked", async () => {
    fetchMock.mockResolvedValue(iapUnauthorized());
    const win = { closed: false, close: vi.fn() };
    const open = vi.fn(() => win as unknown as Window);
    vi.spyOn(window, 'open').mockImplementation(open);
    show();
    await screen.findByTestId('session-expired-banner');

    act(() => {
      screen.getByTestId('session-refresh-button').click();
    });
    expect(open).toHaveBeenCalledTimes(1);
    expect((open.mock.calls[0] as unknown as string[])[0]).toBe(
      '/?gcp-iap-mode=DO_SESSION_REFRESH',
    );
    // Marked busy while it runs, NOT `disabled`. A `disabled` button leaves the
    // tab order the instant it is pressed, throwing the operator's focus to the
    // document with nothing announced — on the one control whose whole job is a
    // slow, invisible background task. Two popups for one session is still
    // never right; that rule is the hook's `runningRef`, asserted just below by
    // the single `open` call above surviving a second click.
    const button = screen.getByTestId('session-refresh-button');
    expect(button).not.toBeDisabled();
    expect(button).toHaveAttribute('aria-disabled', 'true');
    expect(button).toHaveAttribute('aria-busy', 'true');

    act(() => button.click());
    expect(open).toHaveBeenCalledTimes(1);
  });

  it('an IAP-generated 403 says which account, and leaves the popup open', async () => {
    vi.useFakeTimers();
    // The probe and the layout's own poll share one `fetch`, so they are told
    // apart by URL: IAP refuses `/api/health` with ITS OWN 403 (the operator
    // signed in fine, as somebody the IAP policy does not list).
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).startsWith('/api/health')
        ? respond(403, '<html>Forbidden</html>', { 'x-goog-iap-generated-response': 'true' })
        : iapUnauthorized(),
    );
    const win = { closed: false, close: vi.fn() };
    vi.spyOn(window, 'open').mockImplementation(() => win as unknown as Window);
    show();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    screen.getByTestId('session-expired-banner');

    await act(async () => {
      screen.getByTestId('session-refresh-button').click();
      await vi.advanceTimersByTimeAsync(2_000);
    });

    const note = screen.getByTestId('session-refresh-note');
    expect(note.textContent).toMatch(/not allowed on this dashboard/i);
    expect(note.textContent).toMatch(/switch accounts/i);
    // Closing it would remove the only affordance the copy just pointed at.
    expect(win.close).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it('the timeout copy says polling has STOPPED and what to click', async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(iapUnauthorized());
    const win = { closed: false, close: vi.fn() };
    vi.spyOn(window, 'open').mockImplementation(() => win as unknown as Window);
    show();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    screen.getByTestId('session-expired-banner');

    await act(async () => {
      screen.getByTestId('session-refresh-button').click();
      await vi.advanceTimersByTimeAsync(5 * 60_000 + 4_000);
    });

    const note = screen.getByTestId('session-refresh-note');
    // The round-1 copy said "finish signing in there" beside a poller that had
    // already stopped, which reads as "and it will pick up". It will not.
    expect(note.textContent).toMatch(/stopped checking/i);
    expect(note.textContent).toMatch(/click Refresh session again/i);
    // And the window left open is not wasted while it is open.
    expect(note.textContent).toMatch(/keeps refreshing the session/i);
    expect(win.close).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it('falls back to the reload copy when the browser blocks the popup', async () => {
    fetchMock.mockResolvedValue(iapUnauthorized());
    vi.spyOn(window, 'open').mockImplementation(() => null);
    show();
    await screen.findByTestId('session-expired-banner');

    await act(async () => {
      screen.getByTestId('session-refresh-button').click();
    });
    const note = await screen.findByTestId('session-refresh-note');
    expect(note.textContent).toMatch(/blocked the sign-in popup/i);
    expect(note.textContent).toMatch(/Reload/);
    expect(screen.getByRole('button', { name: /^reload$/i })).toBeInTheDocument();
  });
});
