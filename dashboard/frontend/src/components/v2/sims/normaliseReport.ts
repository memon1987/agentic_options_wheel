// FC-060 Layer 4 (PR-B): adapt ONE wire shape into what the page renders.
//
// The producer is `dashboard/backend/services/sweeps.py::shape_results` (plan
// D11), served bare by `GET /api/v2/sweeps/{run_id}` with `status`, `run_id` and
// `stuck` added at the top level. It sends a nested
// `grid[split][scenario][symbol]` whose cells carry a single `state` string,
// plus `summary`, `windows`, `delta_vs_base`, `sign_agreement`, the hashes and
// the verbatim footer strings.
//
// This file's ONLY job is reshaping: grid -> flat rows, `state` -> the partition
// booleans the renderer reads, `windows` map -> ordered array. It computes NO
// statistic. Every median, min, max, count, delta and sign agreement is read
// from the payload and passed through untouched, because three implementations
// of "which cells count" would be three chances to average an `insuf` cell into
// a ranking — and only one of the three would be the one under test.
//
// The CLI's `render_json` shape is deliberately NOT accepted. One wire shape
// means one code path to review; a dual-shape adapter would have left the
// untested branch as the one that shipped.

import type {
  SweepDetail,
  SweepReport,
  SweepResultRow,
  SweepRow,
  SweepSummaryRow,
  SweepWindow,
} from '../../../types/v2';

const isRecord = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback);
const num = (v: unknown): number | null => (typeof v === 'number' && !Number.isNaN(v) ? v : null);
const int = (v: unknown): number => (typeof v === 'number' && !Number.isNaN(v) ? v : 0);
const bool = (v: unknown): boolean | null => (typeof v === 'boolean' ? v : null);

/** `_cell_state`'s four states, plus its explicit `unknown` escape hatch. */
export type CellState = 'measured' | 'insufficient' | 'low_activity' | 'error' | 'unknown';

/** Only a `done` run can carry a readable report. */
const REPORTABLE_STATUS = 'done';

/**
 * Rebuild a flat row from one grid cell.
 *
 * `state` is expanded back into the three booleans the renderer reads.
 * `unknown` — a stored row with no flag set, e.g. one written before a flag
 * existed — expands to NONE of the three. It must not fall through to
 * "measured": a row whose verdict never resolved is not a return, and rendering
 * it as one is how an unfinished cell gets read as a result.
 */
function rowFromCell(
  cell: Record<string, unknown>,
  scenario: string,
  symbol: string,
  split: string,
  window: SweepWindow | undefined,
): SweepResultRow {
  const state = str(cell.state, 'unknown') as CellState;
  const error = typeof cell.error === 'string' && cell.error ? cell.error : null;
  return {
    scenario,
    symbol,
    split,
    start: window?.start ?? '',
    end: window?.end ?? '',
    config_hash: str(cell.config_hash),
    scenario_hash: str(cell.scenario_hash),
    verdict: typeof cell.verdict === 'string' ? cell.verdict : null,
    demote: bool(cell.demote),
    total_return: num(cell.total_return),
    annualized_return: num(cell.annualized_return),
    annualized_return_on_collateral: num(cell.annualized_return_on_collateral),
    benchmark_return: num(cell.benchmark_return),
    excess_return: num(cell.excess_return),
    option_pnl: num(cell.option_pnl),
    stock_pnl_realized: num(cell.stock_pnl_realized),
    stock_pnl_unrealized: num(cell.stock_pnl_unrealized),
    max_drawdown: num(cell.max_drawdown),
    win_rate: num(cell.win_rate),
    assignment_rate: num(cell.assignment_rate),
    puts_sold: num(cell.puts_sold),
    calls_sold: num(cell.calls_sold),
    cycles_completed: num(cell.cycles_completed),
    cycles_open: num(cell.cycles_open),
    decision_days: num(cell.decision_days),
    days_in_position_fraction: num(cell.days_in_position_fraction),
    bid_fill_return: num(cell.bid_fill_return),
    verdict_flips_on_fill: bool(cell.verdict_flips_on_fill),
    replay_seconds: num(cell.replay_seconds),
    error: state === 'error' ? (error ?? 'errored') : error,
    insufficient: state === 'insufficient',
    low_activity: state === 'low_activity',
    measured: state === 'measured',
  };
}

/** `{split: {start, end}}` -> the ordered array the grid iterates. */
function windowsFromPayload(splits: string[], raw: unknown): SweepWindow[] {
  const map = isRecord(raw) ? raw : {};
  return splits.map((split) => {
    const w = isRecord(map[split]) ? (map[split] as Record<string, unknown>) : {};
    return { split, start: str(w.start), end: str(w.end) };
  });
}

function summaryFromPayload(raw: unknown): SweepSummaryRow[] {
  if (!Array.isArray(raw)) return [];
  const out: SweepSummaryRow[] = [];
  for (const item of raw) {
    if (!isRecord(item)) continue;
    out.push({
      scenario: str(item.scenario),
      split: str(item.split),
      median: num(item.median),
      min: num(item.min),
      max: num(item.max),
      measured: int(item.measured),
      insufficient: int(item.insufficient),
      low_activity: int(item.low_activity),
      errors: int(item.errors),
      demote_flags: int(item.demote_flags),
      delta_vs_base: num(item.delta_vs_base),
      delta_symbols: int(item.delta_symbols),
    });
  }
  return out;
}

const hashMap = (raw: unknown): Record<string, string> => {
  if (!isRecord(raw)) return {};
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(raw)) if (typeof v === 'string') out[k] = v;
  return out;
};

/**
 * Reshape a `shape_results` payload.
 *
 * Returns null when there is nothing to render — no grid, no splits, or a grid
 * the run has not filled yet. The caller turns that into a status-specific
 * message rather than an empty table, because an empty grid reads as "this
 * sweep measured nothing", which is a very different claim from "this sweep has
 * not finished".
 */
export function normaliseReport(payload: unknown, sweep: SweepRow | null): SweepReport | null {
  if (!isRecord(payload) || !isRecord(payload.grid)) return null;

  const scenarios = Array.isArray(payload.scenarios) ? payload.scenarios.map((s) => str(s)) : [];
  const symbols = Array.isArray(payload.symbols) ? payload.symbols.map((s) => str(s)) : [];
  const splits = Array.isArray(payload.splits) ? payload.splits.map((s) => str(s)) : [];
  if (splits.length === 0 || scenarios.length === 0) return null;

  const spec = isRecord(payload.spec) ? payload.spec : {};
  const windows = windowsFromPayload(splits, payload.windows);
  const grid = payload.grid as Record<string, unknown>;

  const rows: SweepResultRow[] = [];
  for (const split of splits) {
    const bySplit = isRecord(grid[split]) ? (grid[split] as Record<string, unknown>) : {};
    const window = windows.find((w) => w.split === split);
    for (const scenario of scenarios) {
      const byScenario = isRecord(bySplit[scenario])
        ? (bySplit[scenario] as Record<string, unknown>)
        : {};
      for (const symbol of symbols) {
        const cell = byScenario[symbol];
        // `null` means the runner produced no cell there. Skipping keeps the
        // grid's "—" placeholder honest instead of inventing an empty row.
        if (!isRecord(cell)) continue;
        rows.push(rowFromCell(cell, scenario, symbol, split, window));
      }
    }
  }
  if (rows.length === 0) return null;

  // The arms' overrides and haircuts, off the spec — `shape_results` does not
  // repeat them, and the provenance table is worth more with them than without.
  const specScenarios = Array.isArray(spec.scenarios) ? spec.scenarios : [];
  const overrides: Record<string, Record<string, unknown>> = {};
  const haircuts: Record<string, number | null> = {};
  for (const arm of specScenarios) {
    if (!isRecord(arm)) continue;
    const name = str(arm.name);
    if (!name) continue;
    overrides[name] = isRecord(arm.overrides) ? arm.overrides : {};
    haircuts[name] = num(arm.fill_haircut);
  }

  return {
    scenarios,
    symbols,
    windows,
    starting_cash: num(spec.starting_cash),
    run_sensitivity: bool(spec.run_sensitivity),
    base_config_hash: sweep?.base_config_hash ?? null,
    scenario_hashes: hashMap(payload.scenario_hashes),
    scenario_config_hashes: hashMap(payload.scenario_config_hashes),
    scenario_overrides: overrides,
    scenario_fill_haircuts: haircuts,
    in_sample_only: payload.in_sample_only === true,
    min_days_in_position: num(payload.min_days_in_position) ?? 0.25,
    timing: {
      wall_seconds: sweep?.wall_seconds ?? undefined,
      materialise_seconds: undefined,
      replay_seconds: undefined,
    },
    provider_calls: {
      fetches: sweep?.provider_fetches ?? undefined,
      bar_cache_hits: sweep?.bar_cache_hits ?? undefined,
    },
    rows,
    summary: summaryFromPayload(payload.summary),
    sign_agreement: isRecord(payload.sign_agreement)
      ? (payload.sign_agreement as SweepReport['sign_agreement'])
      : null,
    delta_vs_base: isRecord(payload.delta_vs_base)
      ? (payload.delta_vs_base as SweepReport['delta_vs_base'])
      : {},
    known_biases: Array.isArray(payload.known_biases)
      ? (payload.known_biases as SweepReport['known_biases'])
      : [],
    cross_scenario_caveat: str(payload.cross_scenario_caveat),
    rejection_tally_caveat: str(payload.rejection_tally_caveat),
    in_sample_banner:
      typeof payload.in_sample_banner === 'string' ? payload.in_sample_banner : null,
    holdout_semantics:
      typeof payload.holdout_semantics === 'string' ? payload.holdout_semantics : null,
  };
}

/**
 * The bare `shape_results` response -> `{sweep, results, raw}`.
 *
 * The sweep row is `run`, with `status`, `run_id` and `stuck` repeated at the
 * top level by the router; the top-level values win, because they are the ones
 * the router computed for THIS response.
 *
 * `results` is populated ONLY for a `done` run. Every other status gets null so
 * the page renders a status message instead of a report — a `submitted` run's
 * payload carries `grid: {}` and `splits: []`, and rendering that as a finished
 * report would tell the operator their sweep measured nothing.
 */
export function normaliseSweepDetail(payload: unknown): SweepDetail | null {
  if (!isRecord(payload)) return null;
  if (!isRecord(payload.run)) return null;

  const sweep = { ...payload.run } as unknown as SweepRow;
  if (typeof sweep.run_id !== 'string' || !sweep.run_id) {
    if (typeof payload.run_id === 'string' && payload.run_id) sweep.run_id = payload.run_id;
    else return null;
  }
  if (typeof payload.status === 'string') sweep.status = payload.status as SweepRow['status'];
  if (typeof payload.stuck === 'boolean') sweep.stuck = payload.stuck;

  const results = sweep.status === REPORTABLE_STATUS ? normaliseReport(payload, sweep) : null;
  return { sweep, results, raw: payload };
}

/**
 * `GET /api/v2/sweeps` serves a BARE ARRAY of rows.
 *
 * The `{sweeps: [...]}` envelope is still accepted because it costs one line and
 * the alternative — a silently empty runs list that also never polls and never
 * auto-selects — is indistinguishable from "no sweeps have ever run".
 */
export function normaliseSweepList(payload: unknown): SweepRow[] {
  if (Array.isArray(payload)) return payload as SweepRow[];
  if (isRecord(payload) && Array.isArray(payload.sweeps)) return payload.sweeps as SweepRow[];
  return [];
}
