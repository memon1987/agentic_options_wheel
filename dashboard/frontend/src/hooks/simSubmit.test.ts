// FC-096 Phase E PR-4 — the sim proxy client.
//
// Every 409 body below is COPIED from `deploy/sim_service.py`, not invented:
// the busy pair at `:1074` and `:1090`, the coverage refusal at `:583`, and the
// two budget refusals at `:546` and `:555`. `SpecRefused` serialises as
// `{"detail": ..., **extra}` (`:970-972`), so the extras ARE the discriminator
// — there is no `kind` field to read, and there never was.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  SIM_COLD_START_NOTE,
  classifySimConflict,
  pinSpec,
  submitSim,
} from './useSweeps';
import { resetSessionExpiredSignal } from './iapSession';
import type { SweepSpec } from '../types/v2';

const SPEC: SweepSpec = {
  symbols: ['GOOGL'],
  start: '2025-09-01',
  end: '2026-09-01',
  holdout_start: '2026-06-03',
  scenarios: [{ name: 'strategy_min_put_premium_0.65', overrides: { 'strategy.min_put_premium': 0.65 } }],
};

/** The instance-lock 409 (`sim_service.py:1090`) — `run_id` can be null. */
const BUSY_LOCK = {
  detail:
    'this instance is already replaying a simulation. The engine mutates process-global state ' +
    'during a replay, so exactly one runs at a time; wait for it (poll ' +
    'GET /api/v2/sweeps/<run_id>) or submit to the `backtest-sweep` Job.',
  run_id: null,
};

/** The same-spec-in-flight 409 (`sim_service.py:1074`). */
const BUSY_SAME_SPEC = {
  detail:
    'this exact spec is already running as 9f2c1a4b7e0d3c55 (submitted via operator). Poll ' +
    'GET /api/v2/sweeps/9f2c1a4b7e0d3c55 rather than replaying it twice.',
  run_id: '9f2c1a4b7e0d3c55',
};

/** The coverage 409 (`sim_service.py:583`) — `report.describe()` plus fields. */
const COVERAGE = {
  detail:
    'the chain lake is missing 37 symbol-days inside this window. Backfill them first: '
    + 'python main.py backfill-chains --symbols GOOGL --start 2025-09-02 --end 2025-09-05.',
  sessions: 252,
  weekdays: 261,
  missing_symbol_days: 37,
  boundary: '2025-09-02',
  no_sessions: [],
  missing: { GOOGL: ['2025-09-02', '2025-09-03', '2025-09-04'] },
};

/** The cell-cap 409 (`sim_service.py:546`). */
const BUDGET_CELLS = {
  detail:
    '260 cells exceeds this service’s cap of 240. Use the batch path: submit it to the '
    + '`backtest-sweep` Job (POST /api/v2/sweeps), which has a 3-hour task timeout.',
  cells: 260,
  max_cells: 240,
};

/** The estimate 409 (`sim_service.py:555`). */
const BUDGET_SECONDS = {
  detail:
    'estimated 180s (150s materialising 5 symbol(s) x 2 split(s) at 15s each, plus 30s replaying '
    + '20 cell(s)), over this service’s 120s interactive budget. Use the batch path.',
  cells: 20,
  materialise_seconds: 150.0,
  replay_seconds: 30.0,
  total_seconds: 180.0,
};

describe('classifySimConflict — key PRESENCE, the deploy smoke’s rule', () => {
  it('reads a busy 409 whose run_id is NULL as busy, not as generic', () => {
    // The mutation this kills: `if (body.run_id)` instead of `'run_id' in body`.
    // The instance-lock body carries `run_id: null` whenever `_CURRENT` was
    // cleared between the lock failing and the message being built, and a
    // truthiness test files that as "generic" — so the operator is told the
    // service refused, with no hint that a replay is holding the slot.
    const busy = classifySimConflict(BUSY_LOCK);
    expect(busy.kind).toBe('busy');
    expect(busy.runId).toBeNull();
    expect(busy.detail).toBe(BUSY_LOCK.detail);
  });

  it('reads the same-spec-in-flight 409 as busy and keeps the run id', () => {
    const busy = classifySimConflict(BUSY_SAME_SPEC);
    expect(busy.kind).toBe('busy');
    expect(busy.runId).toBe('9f2c1a4b7e0d3c55');
  });

  it('reads the coverage 409, with the symbol-days and the per-symbol list', () => {
    const coverage = classifySimConflict(COVERAGE);
    expect(coverage.kind).toBe('coverage');
    expect(coverage.missingSymbolDays).toBe(37);
    expect(coverage.missing).toEqual({ GOOGL: ['2025-09-02', '2025-09-03', '2025-09-04'] });
    expect(coverage.detail).toBe(COVERAGE.detail);
    expect(coverage.runId).toBeNull();
  });

  it('reads BOTH budget 409s — the cell cap and the estimate', () => {
    expect(classifySimConflict(BUDGET_CELLS).kind).toBe('budget');
    expect(classifySimConflict(BUDGET_SECONDS).kind).toBe('budget');
    expect(classifySimConflict(BUDGET_SECONDS).detail).toBe(BUDGET_SECONDS.detail);
  });

  it('falls back to generic for a body with none of the four markers', () => {
    const generic = classifySimConflict({ detail: 'something else entirely' });
    expect(generic.kind).toBe('generic');
    expect(generic.detail).toBe('something else entirely');
    // And for a body that is not an object at all.
    expect(classifySimConflict('plain text').kind).toBe('generic');
    expect(classifySimConflict(null).kind).toBe('generic');
  });
});

const jsonResponse = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

describe('submitSim — one POST, and the status decides the outcome', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetSessionExpiredSignal();
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('posts the spec to the proxy exactly once', async () => {
    fetchMock.mockResolvedValue(jsonResponse(202, { run_id: 'abc', cell_count: 4 }));
    await submitSim(SPEC);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v2/sims/run');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual(SPEC);
    // The IAP session cookie is the credential; no Authorization header exists.
    expect(init.headers.Authorization).toBeUndefined();
  });

  it('separates 200 (dedup, nothing replayed) from 202 (accepted)', async () => {
    // The mutation: treat any 2xx as an acceptance. A 200 is the PRIOR run's
    // id — polling it would show a finished run and call a stored answer new
    // work, and the operator would believe their tweak had just been measured.
    fetchMock.mockResolvedValue(
      jsonResponse(200, { run_id: 'prior123', deduplicated: true, sweep_key: 'k' }),
    );
    expect(await submitSim(SPEC)).toEqual({ kind: 'deduplicated', runId: 'prior123' });

    fetchMock.mockResolvedValue(
      jsonResponse(202, { run_id: 'new456', status: 'running', cell_count: 4 }),
    );
    expect(await submitSim(SPEC)).toEqual({ kind: 'accepted', runId: 'new456', cellCount: 4 });
  });

  it('reports a 2xx with no run_id rather than navigating nowhere', async () => {
    fetchMock.mockResolvedValue(jsonResponse(202, { status: 'running' }));
    const out = await submitSim(SPEC);
    expect(out.kind).toBe('error');
  });

  it('classifies all four 409 shapes through to the caller', async () => {
    for (const [body, kind] of [
      [BUSY_LOCK, 'busy'],
      [COVERAGE, 'coverage'],
      [BUDGET_CELLS, 'budget'],
      [{ detail: 'no marker' }, 'generic'],
    ] as Array<[unknown, string]>) {
      fetchMock.mockResolvedValue(jsonResponse(409, body));
      const out = await submitSim(SPEC);
      expect(out.kind).toBe('conflict');
      expect(out.kind === 'conflict' && out.conflict.kind).toBe(kind);
    }
  });

  it('passes 422, 502 and 503 through with the server’s own words', async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, { detail: 'override key not allowed: x.y' }));
    expect(await submitSim(SPEC)).toEqual({ kind: 'invalid', detail: 'override key not allowed: x.y' });

    fetchMock.mockResolvedValue(jsonResponse(502, { detail: 'could not reach the sim service' }));
    expect(await submitSim(SPEC)).toEqual({
      kind: 'unreachable',
      detail: 'could not reach the sim service',
    });

    fetchMock.mockResolvedValue(jsonResponse(503, { detail: 'SIM_SERVICE_URL is unset' }));
    expect(await submitSim(SPEC)).toEqual({ kind: 'disabled', detail: 'SIM_SERVICE_URL is unset' });
  });

  it('keeps 403 as its own outcome — a viewer sees the backend’s reason', async () => {
    // Decision 11: there is no `whoami`, so the button is never hidden; the
    // server's 403 is the whole answer and is shown verbatim.
    fetchMock.mockResolvedValue(
      jsonResponse(403, { detail: 'zeshan@example.com is not in OPERATORS' }),
    );
    expect(await submitSim(SPEC)).toEqual({
      kind: 'unauthorized',
      detail: 'zeshan@example.com is not in OPERATORS',
    });
  });

  it('splits IAP’s 401 from the backend’s own 401', async () => {
    fetchMock.mockResolvedValue(
      new Response('<html>sign in</html>', {
        status: 401,
        headers: { 'Content-Type': 'text/html', 'x-goog-iap-generated-response': 'true' },
      }),
    );
    expect((await submitSim(SPEC)).kind).toBe('session_expired');

    resetSessionExpiredSignal();
    fetchMock.mockResolvedValue(
      jsonResponse(401, { detail: 'iap_audience_unconfigured: set IAP_AUDIENCE' }),
    );
    const out = await submitSim(SPEC);
    expect(out.kind).toBe('unauthenticated');
    expect(out.kind === 'unauthenticated' && out.detail).toContain('iap_audience_unconfigured');
  });

  it('never retries a network failure', async () => {
    fetchMock.mockRejectedValue(new Error('network down'));
    const out = await submitSim(SPEC);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(out).toEqual({ kind: 'error', status: null, detail: 'network down' });
  });

  it('carries a cold-start sentence for the UI’s 502 branch to render', () => {
    expect(SIM_COLD_START_NOTE).toContain('cold-start');
    expect(SIM_COLD_START_NOTE).toContain('150 s');
    // R4: the client CANNOT know nothing ran — Cloud Run queues the request
    // and the service can accept it after the proxy gave up. The note must say
    // what the operator can actually check instead of asserting a fact.
    expect(SIM_COLD_START_NOTE).not.toContain('Nothing was run');
    expect(SIM_COLD_START_NOTE).toContain('not knowable from here');
    expect(SIM_COLD_START_NOTE).toContain('Retry answers 409');
  });
});

describe('pinSpec — {spec, note}, one POST', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetSessionExpiredSignal();
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('sends the spec and the note, and returns the pin id and rolling shape', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(201, {
        pin_id: 'pin_7f3a',
        active: true,
        window_days: 365,
        holdout_days: 90,
        active_pins: 3,
        max_active_pins: 6,
      }),
    );
    const out = await pinSpec(SPEC, 'watch the premium floor');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/v2/sims/pins');
    expect(JSON.parse(init.body)).toEqual({ spec: SPEC, note: 'watch the premium floor' });
    expect(out).toEqual({ kind: 'created', pinId: 'pin_7f3a', windowDays: 365, holdoutDays: 90 });
  });

  it('passes the 409 through verbatim — it NAMES the pin or the cap', async () => {
    // The mutation: replace the detail with "already pinned". The body's own
    // words carry the pin id, which is the only actionable half: the remedy is
    // to edit or un-pin THAT pin.
    const detail =
      'pin pin_1234 already asks this exact question (note: premium floor). The battery would ' +
      'replay one and deduplicate the other. Edit or un-pin that one instead.';
    fetchMock.mockResolvedValue(jsonResponse(409, { detail }));
    expect(await pinSpec(SPEC, '')).toEqual({ kind: 'conflict', detail });
  });

  it('passes 422 and 403 through with the server’s words', async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, { detail: 'a pinned spec may not set force' }));
    expect(await pinSpec(SPEC, '')).toEqual({
      kind: 'invalid',
      detail: 'a pinned spec may not set force',
    });
    fetchMock.mockResolvedValue(jsonResponse(403, { detail: 'not an operator' }));
    expect(await pinSpec(SPEC, '')).toEqual({ kind: 'unauthorized', detail: 'not an operator' });
  });
});
