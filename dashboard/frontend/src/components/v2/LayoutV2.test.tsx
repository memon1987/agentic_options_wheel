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
    // Disabled while it runs: two popups for one session is never right.
    expect(screen.getByTestId('session-refresh-button')).toBeDisabled();
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
