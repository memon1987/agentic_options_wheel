// FC-096 Phase E PR-3 (§PR-3): the chart-shaped pure seams.
//
// Every chart in the console is a `ResponsiveContainer`, and under jsdom's
// `ResizeObserver` stub a `ResponsiveContainer` is 0x0 and renders nothing
// (`AcbWalkChart.test.tsx` is the house precedent). So the LOGIC lives here, in
// functions that take arrays and return arrays, and the DOM tests are confined
// to captions, absence states and legend text. A chart test that renders a
// chart and asserts nothing about its numbers is a test that passes when the
// series is wrong.
//
// These builders were extracted from `artifactDigest.ts` rather than written
// beside it. Two implementations of "the drawdown series" — one for the tile,
// one for the chart — is two chances to disagree with the engine, and only one
// of them would have had the `reconcile` equality pinning it. The digest now
// imports every one of them and re-exports the names its own callers use.

import type {
  MonthlyCashflow,
  SimArtifact,
  SimBars,
  SimCycle,
  SimDailyState,
  SimLedgerEvent,
} from '../../../types/v2';
import { parseOcc } from '../../../utils/format';
import { artifactStrategy, isWheelStrategy } from './normaliseArtifact';

// --------------------------------------------------------------------------- //
// The denominator (§D-4). Lives here so `portfolioIndex` can resolve it without
// importing the digest that imports this file.
// --------------------------------------------------------------------------- //

export type CapitalBaseSource = 'stamped' | 'starting_cash';

export const CAPITAL_BASE_SUPPRESSED =
  'Capital base not stamped on this artifact, so every ratio on this cell would need a ' +
  'denominator this console had to guess. They are hidden rather than approximated: a ' +
  "covered-call cell divided by the wheel's starting cash prints a plausible, wrong " +
  'percentage under a correct-looking label.';

/**
 * THE denominator, resolved in the order §D-4 fixes.
 *
 * `provenance.capital_base` (PR-1's stamp) whenever present. Otherwise
 * `starting_cash` — but ONLY on a wheel artifact, where the two are the same
 * number by construction. On any other strategy the answer is "there isn't
 * one", which is a renderable state and a wrong denominator is not.
 */
export function resolveCapitalBase(
  artifact: SimArtifact,
  strategy: string,
): { base: number | null; source: CapitalBaseSource | null } {
  const stamped = artifact.provenance.capital_base;
  if (typeof stamped === 'number' && Number.isFinite(stamped) && stamped > 0) {
    return { base: stamped, source: 'stamped' };
  }
  const starting = artifact.provenance.starting_cash;
  if (
    isWheelStrategy(strategy) &&
    typeof starting === 'number' &&
    Number.isFinite(starting) &&
    starting > 0
  ) {
    return { base: starting, source: 'starting_cash' };
  }
  return { base: null, source: null };
}

// --------------------------------------------------------------------------- //
// The event sets, named once (§D-4)
// --------------------------------------------------------------------------- //

/** Cash RECEIVED for writing an option, before the fee deduction. */
export const OPEN_KINDS = ['sell_put_open', 'sell_call_open'] as const;

/**
 * The events that carry a fee.
 *
 * The fee RATE on the tile is `Σ fees ÷ Σ contracts` over THIS set, not over
 * the whole ledger: `expire_worthless` carries contracts and no fee, so mixing
 * the all-events fee sum with an open-events contract count prints $0.047 on
 * the captured fixture (1.68 ÷ 36) for an engine whose constant is $0.04. Same
 * set on top and bottom, or the label lies.
 */
export const FEE_BEARING_KINDS = ['sell_put_open', 'sell_call_open', 'buy_to_close'] as const;

/**
 * The option-lifecycle events whose cash movements are the option leg.
 *
 * Assignments and dividends are deliberately OUT: an assignment's `cash_delta`
 * is the share purchase or sale, which belongs to the stock leg. On the
 * captured fixture this set's cash sums to $4,453.49 — the row's `option_pnl`
 * exactly — which is the cheap check that the boundary is drawn where the
 * engine draws it.
 */
export const OPTION_CASH_KINDS = [
  'sell_put_open',
  'sell_call_open',
  'buy_to_close',
  'expire_worthless',
] as const;

export const inSet = (kinds: readonly string[], kind: string): boolean => kinds.includes(kind);

// --------------------------------------------------------------------------- //
// Option cash: cumulative and by month
// --------------------------------------------------------------------------- //

/** Put or call, from the OCC symbol where there is one, else from the kind. */
export function optionSide(event: SimLedgerEvent): 'put' | 'call' | null {
  const parsed = parseOcc(event.symbol);
  if (parsed.optionType === 'P') return 'put';
  if (parsed.optionType === 'C') return 'call';
  if (event.kind === 'sell_put_open' || event.kind === 'put_assignment') return 'put';
  if (event.kind === 'sell_call_open' || event.kind === 'call_assignment') return 'call';
  return null;
}

export interface OptionCashSeries {
  /** `MonthlyCashflow` shape exactly, so `MonthlyPremiumBars` is reused as is. */
  monthly: MonthlyCashflow[];
  /** Cumulative net option cash, one point per date that moved cash. */
  cumulative: Array<{ date: string; value: number }>;
  /** The terminal value of `cumulative`. */
  total: number;
}

/**
 * Net option cash by date and by month.
 *
 * The monthly bars and the cumulative line are built in ONE pass over ONE event
 * set, so the last point of the line is the sum of the bars by construction
 * rather than by luck (a test pins the equality anyway — that is what catches
 * the two drifting apart if someone adds a kind to one set and not the other).
 */
export function monthlyFromLedger(ledger: SimLedgerEvent[]): OptionCashSeries {
  const byDate = new Map<string, number>();
  const byMonth = new Map<string, MonthlyCashflow>();
  for (const event of ledger) {
    if (!inSet(OPTION_CASH_KINDS, event.kind)) continue;
    byDate.set(event.date, (byDate.get(event.date) ?? 0) + event.cash_delta);

    const month = event.date.slice(0, 7);
    const row = byMonth.get(month) ?? {
      month,
      net_option_cashflow: 0,
      put_net_cashflow: 0,
      call_net_cashflow: 0,
      gross_premium: 0,
      buyback_cost: 0,
      event_count: 0,
    };
    row.net_option_cashflow += event.cash_delta;
    const side = optionSide(event);
    if (side === 'put') row.put_net_cashflow += event.cash_delta;
    else if (side === 'call') row.call_net_cashflow += event.cash_delta;
    if (inSet(OPEN_KINDS, event.kind)) row.gross_premium += event.cash_delta + event.fees;
    if (event.kind === 'buy_to_close') row.buyback_cost += -event.cash_delta;
    row.event_count += 1;
    byMonth.set(month, row);
  }

  const cumulative: Array<{ date: string; value: number }> = [];
  let running = 0;
  for (const date of [...byDate.keys()].sort()) {
    running += byDate.get(date) as number;
    cumulative.push({ date, value: running });
  }
  return {
    monthly: [...byMonth.values()].sort((a, b) => a.month.localeCompare(b.month)),
    cumulative,
    total: cumulative.length ? cumulative[cumulative.length - 1].value : 0,
  };
}

// --------------------------------------------------------------------------- //
// Drawdown
// --------------------------------------------------------------------------- //

export interface DrawdownPoint {
  date: string;
  equity: number;
  /** `(equity - running peak) / running peak`. Never positive. */
  drawdown: number;
}

/** `fitness._max_drawdown`, including its collapse of sub-basis-point noise. */
export function maxDrawdownOf(equity: number[]): number {
  let peak = -Infinity;
  let worst = 0;
  for (const value of equity) {
    if (value > peak) peak = value;
    if (peak > 0) {
      const dd = (value - peak) / peak;
      if (dd < worst) worst = dd;
    }
  }
  return worst > -1e-9 ? 0 : worst;
}

/**
 * Drawdown from the RUNNING PEAK of equity, per decision day.
 *
 * From the peak, not from the first day and not from the starting cash: a
 * series measured off the entry price is a return series, and its minimum is
 * not the row's `max_drawdown`. The chart's header shows the ROW's number and a
 * test asserts this series' minimum equals it — which is the only reason the
 * two can be shown on the same card without inviting the reader to reconcile
 * them.
 */
export function drawdownSeries(daily: SimDailyState[]): DrawdownPoint[] {
  const out: DrawdownPoint[] = [];
  let peak = -Infinity;
  for (const day of daily) {
    if (day.equity > peak) peak = day.equity;
    out.push({
      date: day.date,
      equity: day.equity,
      drawdown: peak > 0 ? (day.equity - peak) / peak : 0,
    });
  }
  return out;
}

// --------------------------------------------------------------------------- //
// Deployment (§D-4)
// --------------------------------------------------------------------------- //

export interface DeploymentReading {
  /**
   * Dollar-weighted mean over ALL decision days of
   * `(reserved_collateral + share value) / capital_base`.
   *
   * All days, not deployed days: "how much of the account was working" is a
   * question about the window, and averaging only the days with a position
   * answers a different one (on the captured fixture the reserved-only readings
   * are $14,328 all-days against $29,758 deployed-days — a 2x difference that
   * an unstated definition would hide).
   */
  ratio: number | null;
  /** `closes` = share value from the sidecar; `at cost` = the fallback. */
  basis: 'closes' | 'at cost' | 'none';
  days: number;
  /** The reserved-collateral component alone, in dollars. Pinned by test. */
  reservedMeanDollars: number;
  /** The share-value component alone, in dollars; `null` when unvaluable. */
  sharesValueMeanDollars: number | null;
  /** Days holding shares this module could not value. Non-zero ⇒ `ratio` null. */
  unresolvedShareDays: number;
  /**
   * Did a sidecar with bars reach this reading at all?
   *
   * `basis: 'at cost'` has two causes and the caption must not conflate them:
   * no sidecar for the run, or a sidecar that prices a DIFFERENT underlying
   * from the one held (review round 1, LOW).
   */
  hadSidecar: boolean;
  /**
   * Why those days could not be valued, for the tile (review round 1, F9).
   * `null` when there are none. An absent ratio with no reason beside it reads
   * as "not computed yet".
   */
  unresolvedReason: string | null;
}

export interface DeploymentPoint {
  date: string;
  reserved: number;
  sharesValue: number | null;
  ratio: number | null;
}

export interface DeploymentResult {
  series: DeploymentPoint[];
  reading: DeploymentReading;
}

/**
 * The cost basis to value shares at on `date` when there is no sidecar close.
 *
 * The cycle with the LATEST start that still contains the day. An open cycle's
 * `end` is null, which extends it to the last decision day — the same reading
 * the engine's own cycle materialisation uses.
 */
export function costBasisOn(cycles: SimCycle[], underlying: string, date: string): number | null {
  let best: SimCycle | null = null;
  for (const cycle of cycles) {
    if (cycle.underlying !== underlying) continue;
    if (cycle.cost_basis === null) continue;
    if (cycle.start > date) continue;
    if (cycle.end !== null && cycle.end < date) continue;
    if (!best || cycle.start > best.start) best = cycle;
  }
  return best ? best.cost_basis : null;
}

/**
 * Reserved collateral + share value, per decision day and as one mean ratio.
 *
 * The sidecar is ONE symbol's bars (review round 1, F9). Its closes are applied
 * to the sidecar's OWN underlying and to nothing else: a cell holding shares of
 * another company priced at this symbol's close is a plausible number under a
 * correct-looking label, which is the exact failure §D-4 suppresses ratios to
 * avoid. Everything else falls back to the lot's cost basis, which is a STATED
 * approximation ("at cost") rather than another company's price, and a day this
 * module can value neither way withholds the ratio entirely.
 */
export function deploymentSeries(
  daily: SimDailyState[],
  sidecar: SimBars | null,
  cycles: SimCycle[],
  capitalBase: number | null,
): DeploymentResult {
  const closes = new Map<string, number>();
  for (const bar of sidecar?.bars ?? []) closes.set(bar.date, bar.close);
  const sidecarSymbol = sidecar?.provenance.symbol ?? null;
  const hasCloses = closes.size > 0;

  const series: DeploymentPoint[] = [];
  let reservedSum = 0;
  let sharesValueSum = 0;
  let unresolvedShareDays = 0;
  let valuedAtCost = 0;
  const unvaluable = new Set<string>();

  for (const day of daily) {
    reservedSum += day.reserved_collateral;
    let sharesValue: number | null = 0;
    for (const [underlying, shares] of Object.entries(day.shares_held)) {
      if (shares === 0) continue;
      const close =
        hasCloses && underlying === sidecarSymbol ? (closes.get(day.date) ?? null) : null;
      const price = close ?? costBasisOn(cycles, underlying, day.date);
      if (price === null) {
        sharesValue = null;
        unvaluable.add(underlying);
        break;
      }
      if (close === null) valuedAtCost += 1;
      sharesValue += shares * price;
    }
    if (sharesValue === null) unresolvedShareDays += 1;
    else sharesValueSum += sharesValue;
    series.push({
      date: day.date,
      reserved: day.reserved_collateral,
      sharesValue,
      ratio:
        capitalBase && sharesValue !== null
          ? (day.reserved_collateral + sharesValue) / capitalBase
          : null,
    });
  }

  const days = daily.length;
  const valuable = days > 0 && unresolvedShareDays === 0;
  return {
    series,
    reading: {
      ratio: valuable && capitalBase ? (reservedSum + sharesValueSum) / days / capitalBase : null,
      basis: days === 0 ? 'none' : !hasCloses || valuedAtCost > 0 ? 'at cost' : 'closes',
      hadSidecar: hasCloses,
      days,
      reservedMeanDollars: days > 0 ? reservedSum / days : 0,
      sharesValueMeanDollars: valuable ? sharesValueSum / days : null,
      unresolvedShareDays,
      unresolvedReason: unresolvedShareDays
        ? `${unresolvedShareDays} day${unresolvedShareDays === 1 ? '' : 's'} held ` +
          `${[...unvaluable].sort().join(', ')} at a price this console could not establish — ` +
          'no bar in the sidecar for that symbol and no cycle cost basis covering the day. ' +
          'The ratio is withheld rather than computed over a partial position.'
        : null,
    },
  };
}

// --------------------------------------------------------------------------- //
// Equity vs base vs buy-and-hold (§PR-3, component 3)
// --------------------------------------------------------------------------- //

export interface EquityOverlayRow {
  date: string;
  /** This cell's equity, in dollars. */
  equity: number | null;
  /** The base arm's equity on the same day, when its artifact loaded. */
  base: number | null;
  /** The ENGINE's buy-and-hold curve from the sidecar. Never re-derived here. */
  benchmark: number | null;
}

export interface EquityOverlayResult {
  rows: EquityOverlayRow[];
  hasBase: boolean;
  hasBenchmark: boolean;
  /**
   * Set when the sidecar's `buy_and_hold.final_value` disagrees with the CELL's
   * own `benchmark.final_value` stamp. The curve is then HIDDEN rather than
   * drawn: the two come from the same scored replay of the same window, so a
   * disagreement is a provenance failure, and a benchmark line that silently
   * belongs to a different window is worse than no benchmark line at all.
   */
  benchmarkMismatch: string | null;
  /**
   * The cell carried no `benchmark` stamp to cross-check against (an object
   * written before PR-1 deployed). Such runs have no sidecar either, so this is
   * unreachable in practice today — it is a state, not a fallback, and the
   * caption says the curve is unverified rather than pretending it was checked.
   */
  benchmarkUnverified: boolean;
}

/** Two dollar figures from the same scored replay. Cents are the tolerance. */
const sameMoney = (a: number, b: number): boolean => Math.abs(a - b) <= 0.01;

/**
 * One row array for the equity chart: this cell, the base arm, buy-and-hold.
 *
 * Dates are the UNION of whatever each side carries, so an arm that stopped
 * trading early does not truncate the base curve beside it; a series missing on
 * a date is `null` and recharts' `connectNulls` draws through it. All three are
 * in DOLLARS on the same capital base — the cell and base are the engine's own
 * `daily.equity`, and the benchmark is the engine's own curve read out of the
 * sidecar. Nothing here rebases, rescales or re-derives a single point.
 */
export function equityOverlay(
  cell: SimArtifact | null,
  baseCell: SimArtifact | null,
  sidecar: SimBars | null,
): EquityOverlayResult {
  // `undefined` and `null` are DIFFERENT answers here and the distinction is
  // the whole check: `undefined` is "this object predates the stamp", `null` is
  // "the engine scored this cell with no benchmark at all". Collapsing them with
  // `??` would turn the first into the second and hide a curve that is fine.
  const stamp = cell ? cell.benchmark : undefined;
  const curve = sidecar?.buy_and_hold ?? null;

  let benchmarkMismatch: string | null = null;
  let benchmarkUnverified = false;
  if (curve) {
    if (!cell || stamp === undefined) {
      benchmarkUnverified = true;
    } else if (stamp === null) {
      benchmarkMismatch =
        'This cell was scored with no buy-and-hold benchmark, but the window’s sidecar ' +
        'carries a curve. The curve is not drawn: it cannot be attributed to this cell.';
    } else if (!sameMoney(stamp.final_value, curve.final_value)) {
      benchmarkMismatch =
        `Benchmark mismatch: the sidecar's buy-and-hold ends at ` +
        `${curve.final_value.toFixed(2)} and this cell's own benchmark stamp says ` +
        `${stamp.final_value.toFixed(2)}. A same-spec run cannot produce that, so ` +
        'the curve is hidden rather than drawn against numbers it does not belong to.';
    }
  }

  const drawBenchmark = !!curve && benchmarkMismatch === null;
  const dates = new Set<string>();
  for (const d of cell?.daily ?? []) dates.add(d.date);
  for (const d of baseCell?.daily ?? []) dates.add(d.date);
  if (drawBenchmark) for (const p of curve.daily) dates.add(p.date);

  const cellBy = new Map((cell?.daily ?? []).map((d) => [d.date, d.equity]));
  const baseBy = new Map((baseCell?.daily ?? []).map((d) => [d.date, d.equity]));
  const bhBy = new Map(drawBenchmark ? curve.daily.map((p) => [p.date, p.value]) : []);

  const rows = [...dates].sort().map((date) => ({
    date,
    equity: cellBy.get(date) ?? null,
    base: baseBy.get(date) ?? null,
    benchmark: bhBy.get(date) ?? null,
  }));

  return {
    rows,
    hasBase: baseBy.size > 0,
    hasBenchmark: drawBenchmark && bhBy.size > 0,
    benchmarkMismatch,
    benchmarkUnverified,
  };
}

// --------------------------------------------------------------------------- //
// The equal-weight portfolio index (§PR-3, component 2/3)
// --------------------------------------------------------------------------- //

/** One symbol's cell, as the portfolio view has it at this moment. */
export interface PortfolioCell {
  /** Loaded and parsed, or null while loading / absent / unmeasured. */
  artifact: SimArtifact | null;
  /** The endpoint's own words when there is no object. Null while loading. */
  absence?: string | null;
  /** `spec.strategy` for the run, so a CC cell resolves its own base. */
  specStrategy?: string | null;
}

export interface PortfolioIndexResult {
  /** `date -> index`, base 100 at the first common date. No aggregate number. */
  rows: Array<{ date: string; index: number }>;
  included: string[];
  /** Every symbol left out, with the reason IN WORDS. Never a silent drop. */
  excluded: Array<{ symbol: string; reason: string }>;
  /** k in "k of N loaded" — measured symbols currently IN the index. */
  loaded: number;
  /** N in "k of N loaded" — measured symbols this index wants. */
  total: number;
  /**
   * Dates dropped because at least one included symbol had no state that day.
   * The index is built over the INTERSECTION so a symbol arriving mid-series
   * cannot step the line; a non-zero count is reported in the caption.
   */
  droppedDates: number;
}

/**
 * Which symbols a base OVERLAY may be built over, and which are in the way.
 *
 * Extracted so the rule is stated once and testable on its own (review round 1,
 * R2): an overlay may only average base cells BASE MEASURED. An `insufficient`
 * base cell is a flat line at its own starting cash, so including it drags the
 * base index toward 100 and the arm reads as beating a benchmark that was never
 * computed for that symbol.
 */
export function overlayConstituency(
  armIncluded: string[],
  baseMeasured: string[],
): { target: string[]; missing: string[] } {
  const measured = new Set(baseMeasured);
  return {
    target: armIncluded.filter((symbol) => measured.has(symbol)),
    missing: armIncluded.filter((symbol) => !measured.has(symbol)),
  };
}

/**
 * An equal-weight index of `equity / capital_base`, base 100, over the MEASURED
 * symbols only.
 *
 * Measured only, because an `insufficient` cell is a flat line at its starting
 * cash — a symbol that never traded would pull the index toward 100 and read as
 * stability rather than as absence. Every symbol left out is NAMED with its
 * state, so the reader can see the index's constituency instead of inferring it
 * from the count.
 *
 * This is not a portfolio simulation and the view never prints a number off it:
 * these are N independent single-symbol replays, each on its own full capital,
 * and summing or averaging their returns would claim a diversification the
 * engine never modelled.
 */
export function portfolioIndex(
  cellsBySymbol: Record<string, PortfolioCell>,
  measuredSymbols: string[],
): PortfolioIndexResult {
  const measured = new Set(measuredSymbols);
  const excluded: Array<{ symbol: string; reason: string }> = [];
  const included: string[] = [];
  const ratios = new Map<string, Map<string, number>>();

  for (const symbol of Object.keys(cellsBySymbol)) {
    if (!measured.has(symbol)) continue;
    const cell = cellsBySymbol[symbol];
    if (!cell.artifact) {
      if (cell.absence) excluded.push({ symbol, reason: cell.absence });
      continue;
    }
    const strategy = artifactStrategy(cell.specStrategy, cell.artifact);
    const { base } = resolveCapitalBase(cell.artifact, strategy);
    if (base === null) {
      excluded.push({ symbol, reason: CAPITAL_BASE_SUPPRESSED });
      continue;
    }
    included.push(symbol);
    ratios.set(symbol, new Map(cell.artifact.daily.map((d) => [d.date, d.equity / base])));
  }

  for (const symbol of measuredSymbols) {
    if (!(symbol in cellsBySymbol)) excluded.push({ symbol, reason: 'not loaded' });
  }

  included.sort();
  excluded.sort((a, b) => a.symbol.localeCompare(b.symbol));

  const allDates = new Set<string>();
  for (const map of ratios.values()) for (const date of map.keys()) allDates.add(date);
  const sorted = [...allDates].sort();
  const rows: Array<{ date: string; index: number }> = [];
  let droppedDates = 0;
  for (const date of sorted) {
    let sum = 0;
    let complete = true;
    for (const symbol of included) {
      const value = ratios.get(symbol)?.get(date);
      if (value === undefined) {
        complete = false;
        break;
      }
      sum += value;
    }
    if (!complete) {
      droppedDates += 1;
      continue;
    }
    rows.push({ date, index: (sum / included.length) * 100 });
  }

  return {
    rows,
    included,
    excluded,
    loaded: included.length,
    total: measuredSymbols.length,
    droppedDates,
  };
}
