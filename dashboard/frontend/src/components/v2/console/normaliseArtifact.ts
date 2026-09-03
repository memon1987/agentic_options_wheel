// FC-096 Phase E PR-2: the wire object -> `SimArtifact` / `SimBars`.
//
// One job: reshape and refuse. This module computes NOTHING — no sum, no
// ratio, no verdict (that is `artifactDigest.ts`, and even there the numbers
// are display-only). What it does own is the three rules that decide whether a
// stored object may be rendered at all:
//
//   1. **`schema !== 1` -> null, with the reason.** A future schema is not a
//      slightly different schema: field meanings may have moved under the same
//      names, and rendering it "best effort" would put numbers on screen that
//      no longer mean what their labels say. The caller shows the reason.
//   2. **An unknown ledger `kind` SURVIVES.** Phase C will write
//      `synthetic_lot_open`; a build that predates it must show the event with
//      its raw kind and a neutral badge, never drop it. A dropped event is a
//      ledger that silently does not add up.
//   3. **An absent stamp stays absent.** `provenance.capital_base` and
//      `benchmark` are `undefined` on every object written before PR-1
//      deployed. They are NOT defaulted here, because a default becomes a
//      denominator two modules downstream and nothing on screen would say so.
//
// Missing ARRAYS, by contrast, become `[]`: an artifact with no rolls and an
// artifact whose `roll_records` key never existed render identically and
// truthfully ("no rolls"), and every consumer would otherwise need its own
// null guard.

import type {
  SimArtifact,
  SimArtifactProvenance,
  SimBar,
  SimBars,
  SimBarsProvenance,
  SimBenchmark,
  SimBuyAndHold,
  SimCounters,
  SimCycle,
  SimDailyState,
  SimEarningsCoverage,
  SimLedgerEvent,
  SimRejection,
  SimRollRecord,
} from '../../../types/v2';

/** The one schema version this build renders. Mirrors `artifact.ARTIFACT_SCHEMA`. */
export const SUPPORTED_ARTIFACT_SCHEMA = 1;
/** Mirrors `artifact.BARS_SCHEMA`. Same number today, versioned separately. */
export const SUPPORTED_BARS_SCHEMA = 1;

/** A parse either produced an object, or refused and said why. */
export type ArtifactParse<T> = { value: T; reason: null } | { value: null; reason: string };

const isRecord = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback);
const strOrNull = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
const numOrNull = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null;
const num = (v: unknown, fallback = 0): number =>
  typeof v === 'number' && Number.isFinite(v) ? v : fallback;
const bool = (v: unknown): boolean => v === true;
const rows = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? v.filter(isRecord) : [];
const strings = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((s): s is string => typeof s === 'string') : [];

/** `{SYM: n}` — anything non-numeric is dropped rather than coerced to 0. */
function sharesMap(v: unknown): Record<string, number> {
  if (!isRecord(v)) return {};
  const out: Record<string, number> = {};
  for (const [k, val] of Object.entries(v)) {
    const n = numOrNull(val);
    if (n !== null) out[k] = n;
  }
  return out;
}

function numberMap(v: unknown): Record<string, number> | null {
  if (!isRecord(v)) return null;
  const out: Record<string, number> = {};
  for (const [k, val] of Object.entries(v)) {
    const n = numOrNull(val);
    if (n !== null) out[k] = n;
  }
  return out;
}

// --------------------------------------------------------------------------- //
// Pieces
// --------------------------------------------------------------------------- //

function ledgerEvent(raw: Record<string, unknown>): SimLedgerEvent {
  return {
    date: str(raw.date),
    // NOT narrowed to the seven known kinds. See rule 2 in the header.
    kind: str(raw.kind, 'unknown'),
    underlying: str(raw.underlying),
    symbol: str(raw.symbol),
    contracts: num(raw.contracts),
    shares: num(raw.shares),
    price: numOrNull(raw.price),
    cash_delta: num(raw.cash_delta),
    fees: num(raw.fees),
    detail: isRecord(raw.detail) ? raw.detail : {},
  };
}

function dailyState(raw: Record<string, unknown>): SimDailyState {
  return {
    date: str(raw.date),
    equity: num(raw.equity),
    cash: num(raw.cash),
    reserved_collateral: num(raw.reserved_collateral),
    open_options: num(raw.open_options),
    shares_held: sharesMap(raw.shares_held),
  };
}

function cycle(raw: Record<string, unknown>): SimCycle {
  return {
    underlying: str(raw.underlying),
    start: str(raw.start),
    end: strOrNull(raw.end),
    days: numOrNull(raw.days),
    is_open: bool(raw.is_open),
    outcome: strOrNull(raw.outcome),
    puts_sold: num(raw.puts_sold),
    calls_sold: num(raw.calls_sold),
    contracts_closed: num(raw.contracts_closed),
    rolls: num(raw.rolls),
    event_count: num(raw.event_count),
    assigned: bool(raw.assigned),
    called_away: bool(raw.called_away),
    shares_acquired: num(raw.shares_acquired),
    cost_basis: numOrNull(raw.cost_basis),
    exit_price: numOrNull(raw.exit_price),
    capital_at_risk: numOrNull(raw.capital_at_risk),
    max_collateral: numOrNull(raw.max_collateral),
    option_pnl: num(raw.option_pnl),
    stock_pnl: num(raw.stock_pnl),
    dividends: num(raw.dividends),
    fees: num(raw.fees),
    total_pnl: num(raw.total_pnl),
    return_on_capital: numOrNull(raw.return_on_capital),
    annualized_return: numOrNull(raw.annualized_return),
  };
}

function rollRecord(raw: Record<string, unknown>): SimRollRecord {
  return {
    day: str(raw.day),
    underlying: str(raw.underlying),
    contracts: num(raw.contracts),
    old_strike: numOrNull(raw.old_strike),
    new_strike: numOrNull(raw.new_strike),
    net_credit: numOrNull(raw.net_credit),
    success: bool(raw.success),
    btc_order_id: strOrNull(raw.btc_order_id),
    stc_order_id: strOrNull(raw.stc_order_id),
  };
}

function rejection(raw: Record<string, unknown>): SimRejection {
  return { reason: str(raw.reason), days: num(raw.days) };
}

function counters(raw: unknown): SimCounters {
  if (!isRecord(raw)) return {};
  const out: SimCounters = {};
  const keys: Array<keyof SimCounters> = [
    'decision_days',
    'candidate_days',
    'ledger_events',
    'rolls_evaluated',
    'rolls_executed',
    'early_assignments',
    'dividends_credited',
    'unpriced_ex_div_calls',
    'final_equity',
    'total_return',
  ];
  for (const key of keys) {
    const n = numOrNull(raw[key]);
    if (n !== null) out[key] = n;
  }
  return out;
}

function earnings(raw: unknown): SimEarningsCoverage | null {
  if (!isRecord(raw)) return null;
  return {
    symbols_without_data: strings(raw.symbols_without_data),
    symbols_past_horizon: strings(raw.symbols_past_horizon),
  };
}

/**
 * The `benchmark` stamp.
 *
 * Three-state and it matters: the KEY being absent means "this object predates
 * PR-1" (`undefined`), while `null` means "PR-1 wrote it and the replay had no
 * benchmark". The footer says different things about the two.
 */
function benchmark(raw: unknown): SimBenchmark | null {
  if (!isRecord(raw)) return null;
  return {
    shares: num(raw.shares),
    entry_day: str(raw.entry_day),
    entry_price: num(raw.entry_price),
    exit_day: str(raw.exit_day),
    exit_price: num(raw.exit_price),
    dividends_per_share_total: num(raw.dividends_per_share_total),
    capital_base: num(raw.capital_base),
    final_value: num(raw.final_value),
    total_return: num(raw.total_return),
  };
}

function artifactProvenance(raw: Record<string, unknown>): SimArtifactProvenance {
  const window = isRecord(raw.window) ? raw.window : {};
  const fill = isRecord(raw.fill) ? raw.fill : null;
  const prov: SimArtifactProvenance = {
    run_id: str(raw.run_id),
    scenario: str(raw.scenario),
    symbol: str(raw.symbol),
    split: str(raw.split),
    window: {
      start: str(window.start),
      end: str(window.end),
      first_decision_day: str(window.first_decision_day),
      last_decision_day: str(window.last_decision_day),
    },
    engine_identity: strOrNull(raw.engine_identity),
    git_commit: strOrNull(raw.git_commit),
    generated_at: strOrNull(raw.generated_at),
    config_hash: strOrNull(raw.config_hash),
    scenario_hash: strOrNull(raw.scenario_hash),
    starting_cash: numOrNull(raw.starting_cash),
    fill: fill
      ? { basis: strOrNull(fill.basis), fill_haircut: numOrNull(fill.fill_haircut) }
      : null,
    masked_reach: numberMap(raw.masked_reach),
  };
  // Assigned CONDITIONALLY: an absent stamp must stay `undefined`, not become
  // `null`, so `'capital_base' in provenance` is a true statement about the
  // stored object rather than about this parser.
  const capitalBase = numOrNull(raw.capital_base);
  if (capitalBase !== null) prov.capital_base = capitalBase;
  const strategy = strOrNull(raw.strategy);
  if (strategy !== null) prov.strategy = strategy;
  return prov;
}

// --------------------------------------------------------------------------- //
// The cell artifact
// --------------------------------------------------------------------------- //

/**
 * Parse a cell artifact, or refuse with a reason the caller can render.
 *
 * The reason is written for an operator, not a developer: it says what was
 * received and what this build supports, because "could not parse" on a page
 * whose whole purpose is evidence is indistinguishable from "there is no
 * evidence".
 */
export function parseArtifact(raw: unknown): ArtifactParse<SimArtifact> {
  if (!isRecord(raw)) {
    return { value: null, reason: 'The artifact endpoint did not return a JSON object.' };
  }
  const schema = numOrNull(raw.schema);
  if (schema === null) {
    return { value: null, reason: 'This artifact carries no `schema` version, so it cannot be read safely.' };
  }
  if (schema !== SUPPORTED_ARTIFACT_SCHEMA) {
    return {
      value: null,
      reason:
        `This artifact is schema ${schema}; this build of the console reads schema ` +
        `${SUPPORTED_ARTIFACT_SCHEMA} only. Field meanings can move under the same names ` +
        'between versions, so it is not rendered. Reload the dashboard, or redeploy it.',
    };
  }
  if (!isRecord(raw.provenance)) {
    return { value: null, reason: 'This artifact carries no `provenance` block, so nothing on it can be attributed.' };
  }

  const artifact: SimArtifact = {
    schema,
    provenance: artifactProvenance(raw.provenance),
    daily: rows(raw.daily).map(dailyState),
    ledger: rows(raw.ledger).map(ledgerEvent),
    cycles: rows(raw.cycles).map(cycle),
    roll_records: rows(raw.roll_records).map(rollRecord),
    rejections: rows(raw.rejections).map(rejection),
    binding_constraint: strOrNull(raw.binding_constraint),
    counters: counters(raw.counters),
    earnings_coverage: earnings(raw.earnings_coverage),
  };
  if ('benchmark' in raw) artifact.benchmark = benchmark(raw.benchmark);
  return { value: artifact, reason: null };
}

/** `parseArtifact`, dropping the reason. The shape the plan names. */
export const normaliseArtifact = (raw: unknown): SimArtifact | null => parseArtifact(raw).value;

// --------------------------------------------------------------------------- //
// The bars sidecar
// --------------------------------------------------------------------------- //

function bar(raw: Record<string, unknown>): SimBar {
  return {
    date: str(raw.date),
    open: num(raw.open),
    high: num(raw.high),
    low: num(raw.low),
    close: num(raw.close),
    volume: num(raw.volume),
  };
}

function buyAndHold(raw: unknown): SimBuyAndHold | null {
  if (!isRecord(raw)) return null;
  return {
    capital_base: num(raw.capital_base),
    shares: num(raw.shares),
    entry_day: str(raw.entry_day),
    entry_price: num(raw.entry_price),
    exit_day: str(raw.exit_day),
    exit_price: num(raw.exit_price),
    dividends_per_share_total: num(raw.dividends_per_share_total),
    final_value: num(raw.final_value),
    // The curve key is `daily`, verified against the live object — not `curve`.
    daily: rows(raw.daily).map((p) => ({ date: str(p.date), value: num(p.value) })),
  };
}

function barsProvenance(raw: Record<string, unknown>): SimBarsProvenance {
  const window = isRecord(raw.window) ? raw.window : {};
  return {
    run_id: str(raw.run_id),
    symbol: str(raw.symbol),
    split: str(raw.split),
    window: { start: str(window.start), end: str(window.end) },
    first_decision_day: str(raw.first_decision_day),
    last_decision_day: str(raw.last_decision_day),
    engine_identity: strOrNull(raw.engine_identity),
    git_commit: strOrNull(raw.git_commit),
    generated_at: strOrNull(raw.generated_at),
    source: strOrNull(raw.source),
    data_from: strOrNull(raw.data_from),
    data_to: strOrNull(raw.data_to),
    // `bars_in_window` lives under `provenance`, not at the top level.
    bars_in_window: numOrNull(raw.bars_in_window),
  };
}

export function parseBars(raw: unknown): ArtifactParse<SimBars> {
  if (!isRecord(raw)) {
    return { value: null, reason: 'The bars endpoint did not return a JSON object.' };
  }
  const schema = numOrNull(raw.schema);
  if (schema === null) {
    return { value: null, reason: 'This bars sidecar carries no `schema` version, so it cannot be read safely.' };
  }
  if (schema !== SUPPORTED_BARS_SCHEMA) {
    return {
      value: null,
      reason:
        `This bars sidecar is schema ${schema}; this build reads schema ` +
        `${SUPPORTED_BARS_SCHEMA} only, so it is not rendered.`,
    };
  }
  if (!isRecord(raw.provenance)) {
    return { value: null, reason: 'This bars sidecar carries no `provenance` block.' };
  }
  return {
    value: {
      schema,
      provenance: barsProvenance(raw.provenance),
      bars: rows(raw.bars).map(bar),
      buy_and_hold: buyAndHold(raw.buy_and_hold),
    },
    reason: null,
  };
}

export const normaliseBars = (raw: unknown): SimBars | null => parseBars(raw).value;

// --------------------------------------------------------------------------- //
// Strategy
// --------------------------------------------------------------------------- //

/**
 * Which strategy wrote this cell — read in the order §Degrading for CC fixes.
 *
 * `spec.strategy` on the sweep row first (canonical by omission), then
 * `provenance.strategy`, and ABSENCE MEANS WHEEL. Phase C has not shipped, so
 * every stored object today takes the last branch; the order is fixed now so
 * that when Phase C does stamp it, nothing here has to be renegotiated.
 */
export function artifactStrategy(
  specStrategy: string | null | undefined,
  artifact: SimArtifact | null,
): string {
  if (typeof specStrategy === 'string' && specStrategy) return specStrategy;
  const stamped = artifact?.provenance.strategy;
  if (typeof stamped === 'string' && stamped) return stamped;
  return 'wheel';
}

/** `true` for the one strategy whose ratios may fall back to `starting_cash`. */
export const isWheelStrategy = (strategy: string): boolean => strategy === 'wheel';
