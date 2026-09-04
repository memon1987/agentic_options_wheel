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

import type { SimArtifact, SweepReport, SweepResultRow, SweepRow } from '../../../types/v2';

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
  /**
   * The cell's stored artifact, when it loaded. The matrix reads two things off
   * it and nothing else: `provenance.fill` (the EFFECTIVE fill this cell was
   * replayed under — review round 1, R5) and, through `stampedCapitalBase`, the
   * denominator its ratios used.
   */
  artifact: SimArtifact | null;
  /** The run's status, so a non-`done` side is never compared as if it were. */
  status: string | null;
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
  /**
   * `override` — an arm sets this key. `base` — neither arm does, but the two
   * runs' RECORDED base configs disagree on it, which is a difference between
   * the two cells all the same (review round 1, R2b).
   */
  origin: 'override' | 'base';
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
  const w = side.report?.windows?.find((x) => x.split === side.ref.split);
  // A window row with a missing bound is UNKNOWN, never two empty strings that
  // compare equal — that would print "aligned" off two absent dates.
  if (!w || !w.start || !w.end) return null;
  return { start: w.start, end: w.end };
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
  const overrideKeys = new Set([...Object.keys(overridesA), ...Object.keys(overridesB)]);

  // Review round 1, R2b: two runs can record DIFFERENT bases, and a key neither
  // arm overrides is still a difference between the two cells when the bases
  // disagree on it. Both maps are already loaded, so the diff can say which
  // keys those are instead of leaving `base_config_hash` as an opaque note.
  // Self-limiting: identical (or absent) bases add nothing.
  const baseOnlyKeys = new Set<string>();
  for (const key of new Set([
    ...Object.keys(baseEffectiveA ?? {}),
    ...Object.keys(baseEffectiveB ?? {}),
  ])) {
    if (overrideKeys.has(key)) continue;
    if (baseEffectiveA === null || baseEffectiveB === null) continue;
    if (show(baseEffectiveA[key]) !== show(baseEffectiveB[key])) baseOnlyKeys.add(key);
  }

  const keys = [...overrideKeys, ...baseOnlyKeys].sort();
  return keys.map((key) => {
    const hasA = Object.prototype.hasOwnProperty.call(overridesA, key);
    const hasB = Object.prototype.hasOwnProperty.call(overridesB, key);
    const showA = hasA ? show(overridesA[key]) : '(base)';
    const showB = hasB ? show(overridesB[key]) : '(base)';
    const baseA = baseEffectiveValue(a.report, baseEffectiveA, key);
    const baseB = baseEffectiveValue(b.report, baseEffectiveB, key);
    return {
      key,
      origin: overrideKeys.has(key) ? ('override' as const) : ('base' as const),
      a: showA,
      b: showB,
      baseA,
      baseB,
      // "Same" means the two cells resolve this key the same way — an
      // un-overridden key whose BASES differ is not the same value, and the
      // amber row is exactly the point of listing it.
      same: showA === showB && (hasA || hasB || baseA === baseB),
    };
  });
}

// --------------------------------------------------------------------------- //
// The effective fill (review round 1, R5)
// --------------------------------------------------------------------------- //

export interface EffectiveFill {
  basis: string | null;
  haircut: number | null;
  /** Where the reading came from — `declared` is not `measured`. */
  source: string;
}

const finite = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null;

/**
 * The fill this cell was ACTUALLY replayed under, in the PR-2 source order.
 *
 * The cell artifact's own `provenance.fill` first, then the forecast's (which
 * omits excluded arms and in-sample runs), then the spec's DECLARED haircut —
 * and the source is returned beside the number, because "declared" and
 * "stamped on the replay" are different claims and the matrix must not compare
 * one side's declaration with the other side's measurement without saying so.
 * A null declared haircut is `engine default`, never `—`.
 */
export function effectiveFill(side: CompareSide): EffectiveFill {
  const stamped = side.artifact?.provenance.fill ?? null;
  if (stamped) {
    return {
      basis: stamped.basis ?? null,
      haircut: finite(stamped.fill_haircut),
      source: 'stamped on the cell artifact',
    };
  }
  const served =
    side.report?.forecast?.by_scenario?.[side.ref.scenario]?.symbols?.[side.ref.symbol]?.fill ??
    null;
  if (served) {
    return {
      basis: served.basis ?? null,
      haircut: finite(served.fill_haircut),
      source: served.is_engine_default === true ? 'served by the forecast (engine default)' : 'served by the forecast',
    };
  }
  const declared = side.report?.scenario_fill_haircuts?.[side.ref.scenario];
  if (declared === undefined) return { basis: null, haircut: null, source: 'not known' };
  if (declared === null) return { basis: null, haircut: null, source: 'declared: engine default' };
  return { basis: null, haircut: declared, source: 'declared on the spec' };
}

const fillText = (fill: EffectiveFill): string =>
  `${fill.basis ?? '—'} · haircut ${fill.haircut === null ? 'engine default' : fill.haircut} · ${fill.source}`;

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

export const DELTA_REFUSAL_SAME_CELL =
  'No A−B number: A and B are the SAME cell. The difference of a served Δ with itself is 0 by ' +
  'construction, and printing “0.0%” would read as a finding about two configs rather than as ' +
  'the tautology it is.';

export const DELTA_REFUSAL_BASE_UNKNOWN =
  'No A−B number: at least one run records no `base_config_hash`, so whether the two Δs are ' +
  'against the same base is UNKNOWN — not equal, and not different. Runs written before the ' +
  'hash was stored are in this state.';

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
      : [a.ref.split, b.ref.split].includes('all')
        ? `A whole-run window (\`all\`) against a \`${a.ref.split === 'all' ? b.ref.split : a.ref.split}\` ` +
          'window — one contains the other, so a difference between them is arithmetic about ' +
          'overlapping periods rather than a comparison. Curves are drawn, numbers are withheld.'
        : 'In-sample vs holdout — not comparable. The fit window is the one the config was chosen ' +
          'on and the holdout is the one it was not; a difference between them is a statement ' +
          'about the windows, not about the arms. Curves are drawn, numbers are withheld.',
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
  // Review round 1, R2a: `scenario_hash` is the OVERRIDES' hash, so two arms
  // that set the same overrides against DIFFERENT bases hash the same and would
  // have read "aligned" while running different configs. `config_hash` is the
  // hash of the effective config the cell actually replayed, which is the
  // question this row is asking.
  const configA = a.report?.scenario_config_hashes?.[a.ref.scenario] ?? null;
  const configB = b.report?.scenario_config_hashes?.[b.ref.scenario] ?? null;
  const diff = overridesDiff(a, b, baseEffectiveA, baseEffectiveB);
  let armOutcome: AlignmentOutcome = 'unknown';
  let armDetail =
    'Not yet known — `scenario_hash` has not been read for at least one side. UNCHECKED, not equal.';
  if (known(configA, configB) && configA !== configB) {
    armOutcome = 'noted';
    armDetail =
      `Same overrides (\`scenario_hash\` ${hashA === hashB ? 'equal' : 'differs too'}) but a ` +
      'DIFFERENT effective config (`config_hash`): the two cells replayed different configs. ' +
      'The diff below lists every key that differs, base keys included.';
  } else if (known(hashA, hashB)) {
    if (hashA === hashB) {
      armOutcome = known(configA, configB) ? 'aligned' : 'noted';
      armDetail =
        known(configA, configB)
          ? 'Same `scenario_hash` AND same `config_hash`: the two arms carry the same overrides ' +
            'over the same effective config, whatever they are named.'
          : 'Same `scenario_hash`, but at least one side records no `config_hash`, so whether ' +
            'the EFFECTIVE configs match is unchecked — same overrides is not same config.';
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
    a: `${a.ref.scenario}${hashA ? ` (${hashA}` : ''}${configA ? ` / cfg ${configA})` : hashA ? ')' : ''}`,
    b: `${b.ref.scenario}${hashB ? ` (${hashB}` : ''}${configB ? ` / cfg ${configB})` : hashB ? ')' : ''}`,
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
    'Not yet known — `base_config_hash` has not been read for at least one run. UNCHECKED: not ' +
    'equal, and not different.';
  if (known(baseHashA, baseHashB)) {
    if (baseHashA === baseHashB) {
      baseOutcome = 'aligned';
      baseDetail =
        'Both runs recorded the same base config, so each side’s Δ is against the same base. ' +
        'This is the run’s RECORDED base, not the sim service’s current one.';
    } else {
      // Review round 1, R2c: this was a note, and a note is too weak. A base
      // that moved a risk or strategy key is a DIFFERENT STRATEGY on the other
      // side, not the same one rescaled — the same argument the capital-base
      // row makes. The diff below names the keys; the numbers are withheld.
      baseOutcome = 'withheld';
      baseDetail =
        'The two runs recorded DIFFERENT base configs. Every key neither arm overrides was still ' +
        'resolved differently on the two sides — a moved `put_delta_range` or `max_position_size` ' +
        'is a different strategy, not a rescaled one — so the numbers are withheld and the diff ' +
        'below lists the keys. Each side’s Δ is also against a different base, so there is no ' +
        'A−B. The service’s base moves when its config is redeployed; this hash is how that shows.';
    }
  }
  withhold({
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
  const fillA = effectiveFill(a);
  const fillB = effectiveFill(b);
  const fillAligned = fillA.basis === fillB.basis && fillA.haircut === fillB.haircut;
  rows.push({
    id: 'fill_haircut',
    label: 'Fill',
    outcome: fillAligned ? 'aligned' : 'noted',
    a: fillText(fillA),
    b: fillText(fillB),
    detail: fillAligned
      ? 'Same effective fill on both sides, read in the PR-2 source order: the cell artifact’s ' +
        'own stamp, then the forecast’s, then the spec’s declared haircut. The source is printed ' +
        'beside each reading — a DECLARED haircut is not a measured one, and the two must not be ' +
        'compared as if they were. `engine default` means the spec declared none.'
      : 'DIFFERENT effective fills. Part of the difference between these two cells is the assumed ' +
        'fill price rather than the config, and the varying side carries an amber fill label ' +
        'wherever its numbers appear. Check the source on each side before reading the gap as a ' +
        'real one: a stamped fill against a declared one may be the same fill, differently known.',
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
  // Review round 1, R8: A−B of a cell with ITSELF is 0 by construction. It used
  // to print as "A−B 0.0%", which reads as a finding about two configs.
  else if (sameRef(a.ref, b.ref)) deltaRefusal = DELTA_REFUSAL_SAME_CELL;
  else if (a.ref.runId !== b.ref.runId) deltaRefusal = DELTA_REFUSAL_CROSS_RUN;
  else if (a.ref.split !== b.ref.split) deltaRefusal = DELTA_REFUSAL_SPLIT;
  // `unknown` and `withheld` are different answers and get different words: a
  // run with no recorded hash is not a run with a DIFFERENT base.
  else if (baseOutcome === 'unknown') deltaRefusal = DELTA_REFUSAL_BASE_UNKNOWN;
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
 * engine computed against the same base over the same symbol and split.
 *
 * Worth being straight about (review round 1, LOW): because the gate requires
 * one run, one split and one aligned base, the base term cancels and this
 * number IS `annualized_return(a) − annualized_return(b)`. The point of routing
 * it through the served Δs is not that it computes something different — it is
 * that every input is the ENGINE's, so the page cannot produce a difference
 * where the server refused to produce a Δ (an unmeasured cell, a base cell, a
 * mismatched base). That is the FC-060 rule the console does not cross.
 */
export function differenceOfDeltas(alignment: Alignment, a: CompareSide, b: CompareSide): number | null {
  if (!alignment.allowsDelta) return null;
  const deltaA = a.row?.delta_vs_base_annualized ?? null;
  const deltaB = b.row?.delta_vs_base_annualized ?? null;
  if (deltaA === null || deltaB === null) return null;
  return deltaA - deltaB;
}
