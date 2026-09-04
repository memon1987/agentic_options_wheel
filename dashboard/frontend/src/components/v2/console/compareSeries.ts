// FC-096 Phase E PR-5 (§Compare view, "Curves"): the two-sided equity series.
//
// Pure, like every other seam in `console/`. The one decision it owns is the
// one the alignment matrix hands it: when the two cells' CAPITAL BASES differ,
// dollars on a shared axis are a lie — a $200k replay towers over a $100k one
// whatever the strategies did — so both sides are rebased to an index of 100 at
// their own first day. Rebasing is the ONLY arithmetic here; every input point
// is the engine's own `daily.equity` or the sidecar's own buy-and-hold curve.
//
// What this module does NOT do: it does not average, rank, colour, difference
// or forecast anything. The A−B number lives in `compareAlignment`, is the
// difference of two SERVED Δs, and never touches these rows.

import type { SimArtifact, SimBars } from '../../../types/v2';

export interface CompareEquityRow {
  date: string;
  aEquity: number | null;
  aBase: number | null;
  bEquity: number | null;
  bBase: number | null;
  /** Drawn once, and only when both sides' sidecars agree it is the same curve. */
  benchmark: number | null;
  /** Drawn per side when they do not. */
  aBenchmark: number | null;
  bBenchmark: number | null;
}

export interface CompareEquityResult {
  rows: CompareEquityRow[];
  /** True ⇒ every series is an index at 100 on its own first day, not dollars. */
  base100: boolean;
  /** The axis label, so a renderer cannot disagree with the transform. */
  unit: string;
  /** One shared buy-and-hold line was drawn. */
  sharedBenchmark: boolean;
  /** Why the benchmark is shared, per-side, or absent. Rendered verbatim. */
  benchmarkNote: string;
  hasA: boolean;
  hasB: boolean;
  hasABase: boolean;
  hasBBase: boolean;
}

/** Series → date-keyed lookup, rebased to 100 on its own first point when asked. */
function pointsOf(
  points: Array<{ date: string; value: number }>,
  base100: boolean,
): Map<string, number> {
  if (points.length === 0) return new Map();
  const anchor = points[0].value;
  // An anchor of zero cannot be rebased — dividing by it yields Infinity, and a
  // chart of Infinity is worse than no chart. The series is dropped and the
  // caption says so via `hasA`/`hasB` rather than drawing a broken line.
  if (base100 && (anchor === 0 || !Number.isFinite(anchor))) return new Map();
  return new Map(
    points.map((p) => [p.date, base100 ? (p.value / anchor) * 100 : p.value] as const),
  );
}

const dailyPoints = (artifact: SimArtifact | null): Array<{ date: string; value: number }> =>
  (artifact?.daily ?? []).map((d) => ({ date: d.date, value: d.equity }));

const benchmarkPoints = (bars: SimBars | null): Array<{ date: string; value: number }> =>
  bars?.buy_and_hold?.daily?.map((p) => ({ date: p.date, value: p.value })) ?? [];

/** Two dollar figures from one scored replay. Cents are the tolerance. */
const sameMoney = (a: number, b: number): boolean => Math.abs(a - b) <= 0.01;

export interface CompareEquityInput {
  cell: SimArtifact | null;
  base: SimArtifact | null;
  bars: SimBars | null;
}

/**
 * One row array carrying both sides, each side's base, and the benchmark(s).
 *
 * Dates are the UNION of everything present, so a side whose window is shorter
 * does not truncate the other; a series absent on a date is `null` and recharts
 * draws through it. When the windows differ the union is exactly what the
 * matrix's window row promised, and the renderer shades the non-overlap.
 *
 * The buy-and-hold line is drawn ONCE only when both sidecars carry the same
 * `final_value` AND the same entry/exit days — the plan's rule. Two curves that
 * merely look alike are two curves.
 */
export function compareEquity(
  a: CompareEquityInput,
  b: CompareEquityInput,
  base100: boolean,
): CompareEquityResult {
  const aCurve = benchmarkPoints(a.bars);
  const bCurve = benchmarkPoints(b.bars);
  const aBh = a.bars?.buy_and_hold ?? null;
  const bBh = b.bars?.buy_and_hold ?? null;

  const shared =
    !!aBh &&
    !!bBh &&
    sameMoney(aBh.final_value, bBh.final_value) &&
    aBh.entry_day === bBh.entry_day &&
    aBh.exit_day === bBh.exit_day;

  let benchmarkNote: string;
  if (!aBh && !bBh) {
    benchmarkNote =
      'No buy-and-hold curve on either side: neither window has a bars sidecar. Every run ' +
      'replayed before the sidecar shipped is in this state — the ROW’s `benchmark_return` ' +
      'scalar is still the engine’s own number, it is simply not a curve.';
  } else if (shared) {
    benchmarkNote =
      'One buy-and-hold line: both sidecars report the same entry day, exit day and final ' +
      'value, so this is the same benchmark and drawing it twice would only thicken it.';
  } else if (aBh && bBh) {
    benchmarkNote =
      'TWO buy-and-hold lines. The two sidecars disagree on entry day, exit day or final ' +
      'value, so they are different benchmarks over different windows and are drawn separately.';
  } else {
    benchmarkNote =
      'A buy-and-hold curve exists on one side only — the other window has no bars sidecar. ' +
      'The single line belongs to the side it is named for and is NOT a comparator for both.';
  }

  const aEquity = pointsOf(dailyPoints(a.cell), base100);
  const aBase = pointsOf(dailyPoints(a.base), base100);
  const bEquity = pointsOf(dailyPoints(b.cell), base100);
  const bBase = pointsOf(dailyPoints(b.base), base100);
  const aBench = pointsOf(aCurve, base100);
  const bBench = pointsOf(bCurve, base100);

  const dates = new Set<string>();
  for (const m of [aEquity, aBase, bEquity, bBase, aBench, bBench]) {
    for (const d of m.keys()) dates.add(d);
  }

  const rows = [...dates].sort().map((date) => ({
    date,
    aEquity: aEquity.get(date) ?? null,
    aBase: aBase.get(date) ?? null,
    bEquity: bEquity.get(date) ?? null,
    bBase: bBase.get(date) ?? null,
    benchmark: shared ? (aBench.get(date) ?? null) : null,
    aBenchmark: shared ? null : (aBench.get(date) ?? null),
    bBenchmark: shared ? null : (bBench.get(date) ?? null),
  }));

  return {
    rows,
    base100,
    unit: base100 ? 'index — each series = 100 on its own first day' : 'dollars',
    sharedBenchmark: shared,
    benchmarkNote,
    hasA: aEquity.size > 0,
    hasB: bEquity.size > 0,
    hasABase: aBase.size > 0,
    hasBBase: bBase.size > 0,
  };
}

/**
 * The stretches of the union window that only ONE side covers.
 *
 * Returned as at most two closed ranges so the renderer can shade them. Both
 * `null` windows, or identical ones, give `[]` — there is nothing to shade.
 */
export function nonOverlap(
  wa: { start: string; end: string } | null,
  wb: { start: string; end: string } | null,
): Array<{ start: string; end: string; side: 'a' | 'b' }> {
  if (!wa || !wb) return [];
  const out: Array<{ start: string; end: string; side: 'a' | 'b' }> = [];
  if (wa.start < wb.start) out.push({ start: wa.start, end: wb.start, side: 'a' });
  if (wb.start < wa.start) out.push({ start: wb.start, end: wa.start, side: 'b' });
  if (wa.end > wb.end) out.push({ start: wb.end, end: wa.end, side: 'a' });
  if (wb.end > wa.end) out.push({ start: wa.end, end: wb.end, side: 'b' });
  return out;
}
