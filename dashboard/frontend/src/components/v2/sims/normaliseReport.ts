// FC-060 Layer 4 (PR-B): one internal report shape, two accepted wire shapes.
//
// WHY THIS EXISTS. PR-A (`dashboard/backend/services/sweeps.py`) and Layer 2
// (`src/backtesting/scenarios/report.py::render_json`) describe the same sweep
// in two different payloads, and both are legitimate:
//
//   * `render_json` — the CLI's machine-readable form. Flat `rows` of
//     `ScenarioResult.as_dict()`, each carrying `measured` / `insufficient` /
//     `low_activity` booleans, plus `windows`, the hashes and the timing block.
//   * `shape_results` (plan D11) — the API's UI-shaped form. A nested
//     `grid[split][scenario][symbol]` whose cells carry a single `state` string,
//     a `summary` list, and `run` / `spec` instead of `windows` and hashes.
//
// The page renders from ONE shape. Converting at the boundary means a wire-shape
// change on either side is a change to this file alone, and — the part that
// matters for a page whose whole job is not to mislead — the four cell states
// stay a partition through the conversion instead of being re-derived a third
// time in the JSX.
//
// What is NOT recomputed here: medians, deltas, sign agreement. Those come from
// whichever producer sent them. Deriving them a second way is exactly how an
// `insuf` cell ends up averaged into a ranking.

import type {
  SweepDetail,
  SweepReport,
  SweepResultRow,
  SweepRow,
  SweepWindow,
} from '../../../types/v2';

const isRecord = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback);
const num = (v: unknown): number | null => (typeof v === 'number' && !Number.isNaN(v) ? v : null);
const bool = (v: unknown): boolean | null => (typeof v === 'boolean' ? v : null);

/** `shape_results`' four states, plus its explicit `unknown` escape hatch. */
type CellState = 'measured' | 'insufficient' | 'low_activity' | 'error' | 'unknown';

/**
 * Rebuild a flat row from a grid cell.
 *
 * The state string is expanded back into the three booleans the renderer reads.
 * `unknown` — a stored row with no flag set, e.g. one written before a flag
 * existed — is deliberately expanded to "none of the three", so it renders as
 * neither a return nor a named rejection rather than being quietly counted as
 * measured.
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

/**
 * The windows `render_json` sends explicitly, reconstructed from the spec.
 *
 * The fit window ends the day BEFORE the holdout starts — the two are
 * independent replays and never overlap (`HOLDOUT_SEMANTICS`). Getting that
 * boundary wrong here would print a window a day longer than the one that ran.
 */
function windowsFromSpec(splits: string[], spec: Record<string, unknown>): SweepWindow[] {
  const start = str(spec.start);
  const end = str(spec.end);
  const holdout = str(spec.holdout_start);
  const dayBefore = (iso: string): string => {
    const t = Date.parse(`${iso}T00:00:00Z`);
    return Number.isNaN(t) ? '' : new Date(t - 86_400_000).toISOString().slice(0, 10);
  };
  return splits.map((split) => {
    if (split === 'fit' && holdout) return { split, start, end: dayBefore(holdout) };
    if (split === 'holdout' && holdout) return { split, start: holdout, end };
    return { split, start, end };
  });
}

/** True when the payload is `render_json`'s form rather than `shape_results`'. */
const looksLikeRenderJson = (p: Record<string, unknown>): boolean =>
  Array.isArray(p.rows) && Array.isArray(p.windows);

function fromShapeResults(p: Record<string, unknown>, sweep: SweepRow | null): SweepReport {
  const scenarios = Array.isArray(p.scenarios) ? p.scenarios.map((s) => str(s)) : [];
  const symbols = Array.isArray(p.symbols) ? p.symbols.map((s) => str(s)) : [];
  const splits = Array.isArray(p.splits) ? p.splits.map((s) => str(s)) : [];
  const spec = isRecord(p.spec) ? p.spec : {};
  const windows = windowsFromSpec(splits, spec);
  const grid = isRecord(p.grid) ? p.grid : {};

  const rows: SweepResultRow[] = [];
  for (const split of splits) {
    const bySplit = isRecord(grid[split]) ? (grid[split] as Record<string, unknown>) : {};
    const window = windows.find((w) => w.split === split);
    for (const scenario of scenarios) {
      const byScenario = isRecord(bySplit[scenario]) ? (bySplit[scenario] as Record<string, unknown>) : {};
      for (const symbol of symbols) {
        const cell = byScenario[symbol];
        // `null` means the runner produced no cell there. Skipping keeps the
        // grid's "—" placeholder honest instead of inventing an empty row.
        if (!isRecord(cell)) continue;
        rows.push(rowFromCell(cell, scenario, symbol, split, window));
      }
    }
  }

  // Overrides and haircuts come off the spec — `shape_results` does not repeat
  // them, and the provenance table is worth more with them than without.
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
    scenario_overrides: overrides,
    scenario_fill_haircuts: haircuts,
    in_sample_only: p.in_sample_only === true,
    min_days_in_position: num(p.min_days_in_position) ?? 0.25,
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
    sign_agreement: isRecord(p.sign_agreement)
      ? (p.sign_agreement as SweepReport['sign_agreement'])
      : null,
    delta_vs_base: isRecord(p.delta_vs_base)
      ? (p.delta_vs_base as SweepReport['delta_vs_base'])
      : {},
    known_biases: Array.isArray(p.known_biases)
      ? (p.known_biases as SweepReport['known_biases'])
      : [],
    cross_scenario_caveat: str(p.cross_scenario_caveat),
    rejection_tally_caveat: str(p.rejection_tally_caveat),
    in_sample_banner: typeof p.in_sample_banner === 'string' ? p.in_sample_banner : null,
    holdout_semantics: typeof p.holdout_semantics === 'string' ? p.holdout_semantics : null,
  };
}

/** Accepts either wire shape and returns the one the page renders. */
export function normaliseReport(payload: unknown, sweep: SweepRow | null): SweepReport | null {
  if (!isRecord(payload)) return null;
  if (looksLikeRenderJson(payload)) return payload as unknown as SweepReport;
  // A `shape_results` payload without a grid has nothing to render. Returning
  // null lands on the "carries no report rows" state, which is a true statement,
  // rather than an empty grid, which reads as "the sweep measured nothing".
  if (!isRecord(payload.grid)) return null;
  return fromShapeResults(payload, sweep);
}

/**
 * Accepts either `{sweep, results}` or a bare `shape_results` payload (which
 * carries the row as `run`), and returns the former.
 */
export function normaliseSweepDetail(payload: unknown): SweepDetail | null {
  if (!isRecord(payload)) return null;

  const wrapped = isRecord(payload.sweep) ? payload.sweep : null;
  const inline = isRecord(payload.run) ? payload.run : null;
  const sweep = (wrapped ?? inline) as unknown as SweepRow | null;
  if (!sweep || typeof sweep.run_id !== 'string') return null;

  const results = wrapped
    ? normaliseReport(payload.results, sweep)
    : normaliseReport(payload, sweep);

  return { sweep, results };
}
