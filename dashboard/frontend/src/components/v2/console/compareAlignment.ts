// FC-096 Phase E PR-5 (§Compare view): the alignment matrix, as a pure module.
//
// The compare view's entire safety argument lives in this file. Two cells put
// side by side READ as comparable whether or not they are, so the question
// "may these two be compared, and on which axes" is answered here, once, in
// data — never inside a renderer that could quietly draw a Δ the matrix
// withheld.
//
// Three postures, and they are not degrees of the same thing:
//
//   * REFUSED    — the pair is not two configs at all (two symbols). Nothing is
//                  compared; the view offers to open each cell instead.
//   * WITHHELD   — the pair is comparable as pictures but not as numbers. The
//                  curves are drawn, every return/ratio/Δ tile is replaced by
//                  the reason, and no A−B exists.
//   * NOTED      — the pair is comparable and something about it must be said
//                  anyway (a different engine build, a different fill haircut,
//                  an in-sample side, "same name, different overrides").
//
// The matrix never *hides* a difference. A row that aligns says so, so an
// operator can tell "checked and equal" from "not checked".

import type { SweepReport, SweepResultRow, SweepRow } from '../../../types/v2';

/** One side of the comparison, exactly as the URL spells it. */
export interface CompareRef {
  runId: string;
  scenario: string;
  symbol: string;
  split: string;
}

/**
 * `run:scenario:symbol:split`.
 *
 * `:` is the separator because none of the four can contain one:
 * `SCENARIO_NAME_RE` and `SYMBOL_RE` (`identity.py:75,127`) admit letters,
 * digits, `_`, `-` and `.` only, and a run id is hex. A ref that does not split
 * into exactly four non-empty parts is malformed, not repairable — the page
 * sends it back to `/sims` rather than guessing which field was dropped.
 */
export function parseCellRef(raw: string | null | undefined): CompareRef | null {
  if (!raw) return null;
  const parts = raw.split(':');
  if (parts.length !== 4) return null;
  const [runId, scenario, symbol, split] = parts;
  if (!runId || !scenario || !symbol || !split) return null;
  return { runId, scenario, symbol, split };
}

export const formatCellRef = (ref: CompareRef): string =>
  `${ref.runId}:${ref.scenario}:${ref.symbol}:${ref.split}`;

export const sameRef = (a: CompareRef | null, b: CompareRef | null): boolean =>
  !!a && !!b && formatCellRef(a) === formatCellRef(b);

/** The compare page's own URL for a pair. `b` absent ⇒ only `a` is seeded. */
export function comparePath(a: CompareRef, b?: CompareRef | null): string {
  const query = new URLSearchParams({ a: formatCellRef(a) });
  if (b) query.set('b', formatCellRef(b));
  return `/sims/compare?${query.toString()}`;
}

/**
 * Everything the matrix reads about one side.
 *
 * Deliberately NOT the React state: this module takes plain values so every row
 * of the matrix is testable without a router, a fetch or a fixture of hooks.
 * `report`/`sweep` are `null` while the run is still loading, and the matrix
 * says "not yet known" rather than "aligned" — an unchecked row must never look
 * like a checked one.
 */
export interface CompareSide {
  ref: CompareRef;
  sweep: SweepRow | null;
  report: SweepReport | null;
  row: SweepResultRow | null;
  /**
   * `provenance.capital_base` off the CELL's artifact when it loaded. The
   * stamped base is the denominator the engine actually used; the spec's
   * `starting_cash` is the fallback and is labelled as such.
   */
  stampedCapitalBase: number | null;
}

export type AlignmentOutcome = 'aligned' | 'noted' | 'withheld' | 'refused' | 'unknown';

export type AlignmentRowId =
  | 'symbol'
  | 'split'
  | 'window'
  | 'arm_identity'
  | 'capital_base'
  | 'base_config'
  | 'engine_identity'
  | 'fill_haircut'
  | 'in_sample';

export interface AlignmentRow {
  id: AlignmentRowId;
  /** What is being checked, in the operator's words. */
  label: string;
  outcome: AlignmentOutcome;
  /** The two values, already formatted. `—` when a side has not loaded. */
  a: string;
  b: string;
  /** What the outcome MEANS. Rendered verbatim beside the row. */
  detail: string;
}

export interface OverridesDiffRow {
  key: string;
  a: string;
  b: string;
  /** `a`'s run's recorded base effective value for this key. */
  baseA: string;
  /** `b`'s run's recorded base effective value. Equal to `baseA` on one run. */
  baseB: string;
  /** Do the two arms set this key to the same thing? */
  same: boolean;
}

export interface Alignment {
  rows: AlignmentRow[];
  /** Non-null ⇒ the pair is REFUSED and nothing below it renders. */
  refusal: string | null;
  /** Every row that withholds the numbers, in matrix order. Empty ⇒ tiles show. */
  withheldReasons: string[];
  tilesWithheld: boolean;
  /**
   * The capital bases differ, so dollar curves would be two different
   * strategies drawn on one axis. The equity chart rebases both to 100.
   */
  curvesBase100: boolean;
  /** May an A−B number be shown at all, and if not, why not. */
  allowsDelta: boolean;
  deltaRefusal: string | null;
  /** Rendered whenever the arm-identity row is not `aligned`. */
  overridesDiff: OverridesDiffRow[];
  /** Either side's run is in-sample only. The banner is then mandatory. */
  inSample: boolean;
  /** The union window, when the two differ. `null` when they agree or are unknown. */
  windowUnion: { start: string; end: string } | null;
}

// --------------------------------------------------------------------------- //
// Small formatters. `—` is "not known", never "not set".
// --------------------------------------------------------------------------- //

const DASH = '—';

const show = (value: unknown): string => {
  if (value === null || value === undefined) return DASH;
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
};

const windowOf = (side: CompareSide): { start: string; end: string } | null => {
  const w = side.report?.windows.find((x) => x.split === side.ref.split);
  return w ? { start: w.start, end: w.end } : null;
};

const windowText = (side: CompareSide): string => {
  const w = windowOf(side);
  return w ? `${w.start} → ${w.end}` : DASH;
};

/**
 * The declared starting cash and the stamped capital base, as ONE reading.
 *
 * They are different facts — `starting_cash` is what the spec asked for,
 * `provenance.capital_base` is what the engine divided by — and the row prefers
 * the stamp because that is the denominator every ratio on the cell used. The
 * label says which one is on screen so "declared" is never read as "measured".
 */
export function capitalBaseOf(side: CompareSide): { value: number | null; source: string } {
  if (side.stampedCapitalBase !== null && side.stampedCapitalBase !== undefined) {
    return { value: side.stampedCapitalBase, source: 'stamped on the cell artifact' };
  }
  const declared = side.report?.starting_cash;
  if (declared !== null && declared !== undefined) {
    return { value: declared, source: 'declared starting cash (no stamp read yet)' };
  }
  return { value: null, source: 'not known' };
}

const money = (value: number | null): string =>
  value === null ? DASH : `$${value.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;

/** Both sides loaded enough to answer this row? */
const known = (a: unknown, b: unknown): boolean =>
  a !== null && a !== undefined && b !== null && b !== undefined;

// --------------------------------------------------------------------------- //
// The overrides diff
// --------------------------------------------------------------------------- //

/** `base_config_json.effective` is FLAT with DOTTED keys (PR-4 amendments). */
function baseEffectiveValue(
  report: SweepReport | null,
  effective: Record<string, unknown> | null,
  key: string,
): string {
  void report;
  if (!effective) return DASH;
  if (Object.prototype.hasOwnProperty.call(effective, key)) return show(effective[key]);
  return DASH;
}

/**
 * Key → a / b / each run's recorded base.
 *
 * The union of both arms' override keys, so a key one arm sets and the other
 * leaves at base is VISIBLE as exactly that: `a` shows the override, `b` shows
 * "(base)" and the base column shows what that means. A diff that listed only
 * the keys both arms set would hide the more interesting half.
 *
 * The base columns are each run's **recorded** `base_config_json.effective` —
 * what that run replayed against — not the sim service's current config. Two
 * runs can record different bases, which is what the `base_config` row is for.
 */
export function overridesDiff(
  a: CompareSide,
  b: CompareSide,
  baseEffectiveA: Record<string, unknown> | null,
  baseEffectiveB: Record<string, unknown> | null,
): OverridesDiffRow[] {
  const overridesA = a.report?.scenario_overrides?.[a.ref.scenario] ?? {};
  const overridesB = b.report?.scenario_overrides?.[b.ref.scenario] ?? {};
  const keys = [...new Set([...Object.keys(overridesA), ...Object.keys(overridesB)])].sort();
  return keys.map((key) => {
    const hasA = Object.prototype.hasOwnProperty.call(overridesA, key);
    const hasB = Object.prototype.hasOwnProperty.call(overridesB, key);
    const showA = hasA ? show(overridesA[key]) : '(base)';
    const showB = hasB ? show(overridesB[key]) : '(base)';
    return {
      key,
      a: showA,
      b: showB,
      baseA: baseEffectiveValue(a.report, baseEffectiveA, key),
      baseB: baseEffectiveValue(b.report, baseEffectiveB, key),
      same: showA === showB,
    };
  });
}

// --------------------------------------------------------------------------- //
// The matrix
// --------------------------------------------------------------------------- //

/** Refusal text, in one place, because the page and the tests both need it. */
export const SYMBOL_REFUSAL =
  'Refused: these are two SYMBOLS, not two configs. A wheel on one underlying and a wheel on ' +
  'another are two different questions, and putting their curves on one axis invites reading the ' +
  'difference as a result of the config. Open each cell instead.';

export const DELTA_REFUSAL_CROSS_RUN =
  'No A−B number: the two cells were replayed by different runs. Each side’s Δ is against ' +
  'its OWN run’s base, so subtracting them would difference two quantities measured against ' +
  'different comparators.';

export const DELTA_REFUSAL_SPLIT =
  'No A−B number: the two cells are different splits. A fit Δ and a holdout Δ answer different ' +
  'questions about different windows.';

export const DELTA_REFUSAL_BASE_HASH =
  'No A−B number: the two runs recorded different base configs (`base_config_hash`), so each ' +
  'side’s Δ is against a different base.';

export const DELTA_REFUSAL_WITHHELD =
  'No A−B number: the alignment matrix withheld the return tiles on this pair, and a difference ' +
  'of two withheld numbers is not a number the matrix permits.';

export const DELTA_REFUSAL_NULL =
  'No A−B number: at least one side has no served Δ against its base. A cell that IS base has no ' +
  'Δ, and an unmeasured cell has none either — the server sends `null`, never `0`.';

/**
 * Evaluate every dimension of §Compare view, in the plan's order.
 *
 * The order matters for the operator, not for the logic: symbol first because
 * it is the only refusal, then the two window dimensions, then arm identity,
 * then the denominators, then the provenance notes.
 */
export function alignCells(
  a: CompareSide,
  b: CompareSide,
  baseEffectiveA: Record<string, unknown> | null = null,
  baseEffectiveB: Record<string, unknown> | null = null,
): Alignment {
  const rows: AlignmentRow[] = [];
  const withheldReasons: string[] = [];

  const withhold = (row: AlignmentRow) => {
    rows.push(row);
    if (row.outcome === 'withheld') withheldReasons.push(`${row.label}: ${row.detail}`);
  };

  // --- symbol: the only refusal ------------------------------------------- //
  const symbolAligned = a.ref.symbol === b.ref.symbol;
  rows.push({
    id: 'symbol',
    label: 'Symbol',
    outcome: symbolAligned ? 'aligned' : 'refused',
    a: a.ref.symbol,
    b: b.ref.symbol,
    detail: symbolAligned ? 'Same underlying.' : SYMBOL_REFUSAL,
  });
  const refusal = symbolAligned ? null : SYMBOL_REFUSAL;

  // --- split --------------------------------------------------------------- //
  const splitAligned = a.ref.split === b.ref.split;
  withhold({
    id: 'split',
    label: 'Split',
    outcome: splitAligned ? 'aligned' : 'withheld',
    a: a.ref.split,
    b: b.ref.split,
    detail: splitAligned
      ? 'Same window role.'
      : 'In-sample vs holdout — not comparable. The fit window is the one the config was chosen ' +
        'on and the holdout is the one it was not; a difference between them is a statement about ' +
        'the windows, not about the arms. Curves are drawn, numbers are withheld.',
  });

  // --- window -------------------------------------------------------------- //
  const wa = windowOf(a);
  const wb = windowOf(b);
  let windowUnion: { start: string; end: string } | null = null;
  let windowOutcome: AlignmentOutcome = 'unknown';
  let windowDetail =
    'Not yet known — at least one run’s report has not loaded, so this row is UNCHECKED ' +
    'rather than aligned.';
  if (wa && wb) {
    if (wa.start === wb.start && wa.end === wb.end) {
      windowOutcome = 'aligned';
      windowDetail = 'Identical calendar window.';
    } else {
      windowOutcome = 'withheld';
      windowUnion = {
        start: wa.start < wb.start ? wa.start : wb.start,
        end: wa.end > wb.end ? wa.end : wb.end,
      };
      windowDetail =
        'Different calendar windows. Returns are over different amounts of market, so the numbers ' +
        'are withheld; the curves are drawn over the UNION window ' +
        `(${windowUnion.start} → ${windowUnion.end}) with each side’s non-overlap shaded. ` +
        'A rolling pin slides its window every week, so two Saturdays of one pin fire this row.';
    }
  }
  withhold({
    id: 'window',
    label: 'Window',
    outcome: windowOutcome,
    a: windowText(a),
    b: windowText(b),
    detail: windowDetail,
  });

  // --- arm identity: same NAME is not same OVERRIDES ----------------------- //
  const hashA = a.report?.scenario_hashes?.[a.ref.scenario] ?? null;
  const hashB = b.report?.scenario_hashes?.[b.ref.scenario] ?? null;
  const diff = overridesDiff(a, b, baseEffectiveA, baseEffectiveB);
  let armOutcome: AlignmentOutcome = 'unknown';
  let armDetail =
    'Not yet known — `scenario_hash` has not been read for at least one side. UNCHECKED, not equal.';
  if (known(hashA, hashB)) {
    if (hashA === hashB) {
      armOutcome = 'aligned';
      armDetail =
        'Same `scenario_hash`: the two arms carry the same overrides, whatever they are named.';
    } else {
      armOutcome = 'noted';
      armDetail =
        a.ref.scenario === b.ref.scenario
          ? 'SAME NAME, DIFFERENT ARM. Both are called ' +
            `“${a.ref.scenario}” and they hash differently, so they do not set the same ` +
            'overrides. The diff below is what actually differs; the name is not evidence.'
          : 'Different arms, as named. The diff below is what each sets against its run’s ' +
            'recorded base.';
    }
  } else if (a.report && b.report) {
    // Both reports loaded but a hash is missing: fall back to the overrides
    // themselves rather than calling the row aligned on no evidence.
    armOutcome = 'noted';
    armDetail =
      'No `scenario_hash` on at least one side, so the arms are compared by their recorded ' +
      'overrides instead — a weaker check, and the diff below is the whole of it.';
  }
  rows.push({
    id: 'arm_identity',
    label: 'Arm identity',
    outcome: armOutcome,
    a: `${a.ref.scenario}${hashA ? ` (${hashA})` : ''}`,
    b: `${b.ref.scenario}${hashB ? ` (${hashB})` : ''}`,
    detail: armDetail,
  });

  // --- capital base --------------------------------------------------------- //
  const capA = capitalBaseOf(a);
  const capB = capitalBaseOf(b);
  let capOutcome: AlignmentOutcome = 'unknown';
  let capDetail =
    'Not yet known — neither a stamp nor a declared starting cash has been read for one side.';
  let curvesBase100 = false;
  if (known(capA.value, capB.value)) {
    if (capA.value === capB.value) {
      capOutcome = 'aligned';
      capDetail = `Same capital base (a: ${capA.source}; b: ${capB.source}).`;
    } else {
      capOutcome = 'withheld';
      curvesBase100 = true;
      capDetail =
        'Different capital bases. Position sizing is ' +
        '`int(cash × max_position_size // strike × 100)`, so a different cash is a DIFFERENT ' +
        'STRATEGY rather than the same one rescaled — the contract counts, and therefore the ' +
        'premium, the assignments and the drawdown, are not proportional. Dollar tiles are ' +
        'withheld and the curves are drawn rebased to 100.';
    }
  }
  withhold({
    id: 'capital_base',
    label: 'Capital base',
    outcome: capOutcome,
    a: `${money(capA.value)} · ${capA.source}`,
    b: `${money(capB.value)} · ${capB.source}`,
    detail: capDetail,
  });

  // --- base config ---------------------------------------------------------- //
  const baseHashA = a.sweep?.base_config_hash ?? null;
  const baseHashB = b.sweep?.base_config_hash ?? null;
  let baseOutcome: AlignmentOutcome = 'unknown';
  let baseDetail =
    'Not yet known — `base_config_hash` has not been read for at least one run. UNCHECKED.';
  if (known(baseHashA, baseHashB)) {
    if (baseHashA === baseHashB) {
      baseOutcome = 'aligned';
      baseDetail =
        'Both runs recorded the same base config, so each side’s Δ is against the same base. ' +
        'This is the run’s RECORDED base, not the sim service’s current one.';
    } else {
      baseOutcome = 'noted';
      baseDetail =
        'The two runs recorded DIFFERENT base configs. Each side’s Δ vs base is therefore ' +
        'against a different base, and no A−B number is shown. The service’s base moves when ' +
        'its config is redeployed; this hash is how that shows up.';
    }
  }
  rows.push({
    id: 'base_config',
    label: 'Base config',
    outcome: baseOutcome,
    a: show(baseHashA),
    b: show(baseHashB),
    detail: baseDetail,
  });

  // --- engine identity ------------------------------------------------------ //
  const engA = a.sweep?.engine_identity ?? null;
  const engB = b.sweep?.engine_identity ?? null;
  let engOutcome: AlignmentOutcome = 'unknown';
  let engDetail = 'Not yet known — `engine_identity` has not been read for at least one run.';
  if (known(engA, engB)) {
    if (engA === engB) {
      engOutcome = 'aligned';
      engDetail = 'Both cells were replayed by the same engine build.';
    } else {
      engOutcome = 'noted';
      engDetail =
        `Replayed by DIFFERENT engine builds (“${engA}” / “${engB}”). ` +
        'Two Saturdays of one pin usually SHARE an identity — that is what dedup means — so a ' +
        'difference here says the engine changed between them, and any move in the levels has a ' +
        'cause that is not the config.';
    }
  }
  rows.push({
    id: 'engine_identity',
    label: 'Engine identity',
    outcome: engOutcome,
    a: show(engA),
    b: show(engB),
    detail: engDetail,
  });

  // --- fill haircut ---------------------------------------------------------- //
  const fillA = a.report?.scenario_fill_haircuts?.[a.ref.scenario] ?? null;
  const fillB = b.report?.scenario_fill_haircuts?.[b.ref.scenario] ?? null;
  const fillAligned = fillA === fillB;
  rows.push({
    id: 'fill_haircut',
    label: 'Fill haircut',
    outcome: fillAligned ? 'aligned' : 'noted',
    a: fillA === null ? 'engine default' : String(fillA),
    b: fillB === null ? 'engine default' : String(fillB),
    detail: fillAligned
      ? 'Same fill assumption on both sides. `engine default` means the spec declared no ' +
        'haircut and the engine applied its own.'
      : 'DIFFERENT fill assumptions. Part of the difference between these two cells is the ' +
        'assumed fill price rather than the config, and the varying side carries an amber fill ' +
        'label wherever its numbers appear. `engine default` means the spec declared none.',
  });

  // --- in-sample -------------------------------------------------------------- //
  const inSample = !!a.report?.in_sample_only || !!b.report?.in_sample_only;
  rows.push({
    id: 'in_sample',
    label: 'In-sample',
    outcome: inSample ? 'noted' : 'aligned',
    a: a.report ? String(!!a.report.in_sample_only) : DASH,
    b: b.report ? String(!!b.report.in_sample_only) : DASH,
    detail: inSample
      ? 'At least one side is an IN-SAMPLE run. That run’s own banner is printed above, ' +
        'verbatim, and it applies to everything below it.'
      : 'Neither run is in-sample only.',
  });

  // --- the A−B rule ------------------------------------------------------------ //
  const tilesWithheld = withheldReasons.length > 0;
  const deltaA = a.row?.delta_vs_base_annualized ?? null;
  const deltaB = b.row?.delta_vs_base_annualized ?? null;
  let deltaRefusal: string | null = null;
  if (refusal) deltaRefusal = refusal;
  else if (a.ref.runId !== b.ref.runId) deltaRefusal = DELTA_REFUSAL_CROSS_RUN;
  else if (a.ref.split !== b.ref.split) deltaRefusal = DELTA_REFUSAL_SPLIT;
  else if (baseOutcome !== 'aligned') deltaRefusal = DELTA_REFUSAL_BASE_HASH;
  else if (tilesWithheld) deltaRefusal = DELTA_REFUSAL_WITHHELD;
  else if (deltaA === null || deltaB === null) deltaRefusal = DELTA_REFUSAL_NULL;

  return {
    rows,
    refusal,
    withheldReasons,
    tilesWithheld,
    curvesBase100,
    allowsDelta: deltaRefusal === null,
    deltaRefusal,
    overridesDiff: diff,
    inSample,
    windowUnion,
  };
}

/**
 * The A−B number itself: Δa − Δb, and NOTHING else.
 *
 * "Difference of two served Δs" is the whole definition. Both inputs are the
 * server's `delta_vs_base_annualized`, so this subtracts two quantities the
 * engine computed against the same base over the same symbol and split. It is
 * never a difference of two annualised returns, which would be a Δ this page
 * derived — the FC-060 rule the console does not cross.
 */
export function differenceOfDeltas(alignment: Alignment, a: CompareSide, b: CompareSide): number | null {
  if (!alignment.allowsDelta) return null;
  const deltaA = a.row?.delta_vs_base_annualized ?? null;
  const deltaB = b.row?.delta_vs_base_annualized ?? null;
  if (deltaA === null || deltaB === null) return null;
  return deltaA - deltaB;
}
