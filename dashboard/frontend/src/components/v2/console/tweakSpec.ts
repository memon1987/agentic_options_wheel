// FC-096 Phase E PR-4 — the tweak bar's pure half.
//
// One field -> one arm (plan §Non-goals). Everything here is a pure function
// over the allowlist, the run's stored `base_config_json.effective`, and the
// operator's typed values; the component owns only React state and the POST.
//
// Two rules decide the whole module:
//
//  1. **Controls come from `allowed[].key` x `value_types[key]`**, never from
//     `described`, which is prose (decision 9). A control set that depended on
//     how a sentence was phrased would change shape when someone reworded a
//     docstring.
//  2. **An override is a DIFFERENCE from this run's base.** A field left at its
//     prefilled value is not sent, because an override that restates the base
//     value is still an override to the runner: it would make an arm that is
//     numerically identical to base and cost a cell to say so.

import type { SweepAllowlist, SweepSpec, SweepScenarioSpec } from '../../../types/v2';

/** `OVERRIDE_VALUE_TYPES` (`services/sweeps.py`). */
export type TweakType = 'band' | 'number' | 'int' | 'bool' | 'symbols';

const TWEAK_TYPES: TweakType[] = ['band', 'number', 'int', 'bool', 'symbols'];

/**
 * The engine's DTE reach, restated by the allowlist's own `described` text
 * ("1-21 — bounded by the stored chain lake's reach"). Client-side it is a
 * HINT: the service validates, and its refusal is what the operator reads.
 */
export const DTE_MIN = 1;
export const DTE_MAX = 21;

/** Keys the DTE bound applies to — matched on the leaf, not the whole path. */
const isDteKey = (key: string): boolean => key.endsWith('_target_dte');

export interface TweakControl {
  /** The dotted allowlist key, exactly as the runner spells it. */
  key: string;
  type: TweakType;
  /** `allowed[].description`, verbatim — shown as the control's help text. */
  description: string;
  /** `base_config_json.effective[key]`, or `undefined` when the run has none. */
  baseValue: unknown;
  /** The run recorded no value for this key: prefill is empty and says so. */
  baseMissing: boolean;
}

/** The operator's typed state for one control. Bands carry two strings. */
export interface FieldValue {
  value?: string;
  lo?: string;
  hi?: string;
}

export type FieldMap = Record<string, FieldValue>;

/**
 * Read a DOTTED key out of the run's `effective` config.
 *
 * `effective` is nested (`{strategy: {min_put_premium: 0.5}}`), and the
 * allowlist keys are dotted paths into it. A flat `effective[key]` lookup — the
 * obvious reading of "prefilled from `base_config_json.effective[key]`" — finds
 * nothing and would silently prefill every control as "not recorded".
 */
export function readDotted(
  effective: Record<string, unknown> | null | undefined,
  key: string,
): unknown {
  if (!effective) return undefined;
  let node: unknown = effective;
  for (const part of key.split('.')) {
    if (!node || typeof node !== 'object' || Array.isArray(node)) return undefined;
    node = (node as Record<string, unknown>)[part];
    if (node === undefined) return undefined;
  }
  return node;
}

/** The typed control set, in the allowlist's own order. */
export function buildControls(
  allowlist: SweepAllowlist | null,
  baseEffective: Record<string, unknown> | null,
): TweakControl[] {
  if (!allowlist) return [];
  const types = allowlist.value_types ?? {};
  const controls: TweakControl[] = [];
  for (const entry of allowlist.allowed ?? []) {
    const raw = types[entry.key];
    // A key the server allows but does not TYPE gets no control: guessing a
    // type from the value would render a checkbox for a number the first time
    // a run happened to store `0`.
    if (!raw || !TWEAK_TYPES.includes(raw as TweakType)) continue;
    const baseValue = readDotted(baseEffective, entry.key);
    controls.push({
      key: entry.key,
      type: raw as TweakType,
      description: entry.description ?? '',
      baseValue,
      baseMissing: baseValue === undefined,
    });
  }
  return controls;
}

/** Controls the bar renders. `symbols` keys are excluded (decision 9). */
export const editableControls = (controls: TweakControl[]): TweakControl[] =>
  controls.filter((c) => c.type !== 'symbols');

/** Controls the bar FOOTNOTES rather than renders. */
export const listOnlyControls = (controls: TweakControl[]): TweakControl[] =>
  controls.filter((c) => c.type === 'symbols');

const numText = (v: unknown): string =>
  typeof v === 'number' && Number.isFinite(v) ? String(v) : '';

/** The prefill for one control, from the run's effective config. */
export function prefill(control: TweakControl): FieldValue {
  if (control.type === 'band') {
    const arr = Array.isArray(control.baseValue) ? control.baseValue : [];
    return { lo: numText(arr[0]), hi: numText(arr[1]) };
  }
  if (control.type === 'bool') {
    return { value: typeof control.baseValue === 'boolean' ? String(control.baseValue) : '' };
  }
  return { value: numText(control.baseValue) };
}

/** Every control's prefill, keyed by allowlist key. */
export function initialFields(controls: TweakControl[]): FieldMap {
  const fields: FieldMap = {};
  for (const control of controls) fields[control.key] = prefill(control);
  return fields;
}

/** A field the operator has not filled in — untouched, never an error. */
export function isBlank(control: TweakControl, field: FieldValue | undefined): boolean {
  if (!field) return true;
  if (control.type === 'band') return !(field.lo ?? '').trim() && !(field.hi ?? '').trim();
  return !(field.value ?? '').trim();
}

export type Coerced = { ok: true; value: unknown } | { ok: false; error: string };

const finite = (raw: string): number | null => {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
};

/**
 * One typed value, or the reason it is not one.
 *
 * These messages are the CLIENT's, and they are hints: the sim service holds
 * the real `Config` and its refusal is the authority (§D-3). They exist so a
 * typo does not cost a round trip, not so the browser can decide what runs.
 */
export function coerceField(control: TweakControl, field: FieldValue | undefined): Coerced {
  const leaf = control.key.split('.').pop() ?? control.key;
  if (control.type === 'band') {
    const lo = finite(field?.lo ?? '');
    const hi = finite(field?.hi ?? '');
    if (lo === null || hi === null) {
      return { ok: false, error: `${leaf}: both ends of the band must be numbers.` };
    }
    if (!(lo < hi)) {
      return { ok: false, error: `${leaf}: the band's low end must be below its high end (${lo} >= ${hi}).` };
    }
    if (lo < 0 || hi > 1) {
      return { ok: false, error: `${leaf}: a delta band lies inside [0, 1]; got [${lo}, ${hi}].` };
    }
    return { ok: true, value: [lo, hi] };
  }
  if (control.type === 'bool') {
    const raw = (field?.value ?? '').trim();
    if (raw !== 'true' && raw !== 'false') {
      return { ok: false, error: `${leaf}: must be true or false.` };
    }
    return { ok: true, value: raw === 'true' };
  }
  if (control.type === 'symbols') {
    // Unreachable through the bar (decision 9) and refused rather than guessed
    // at, so a future caller that forgets the exclusion fails loudly.
    return { ok: false, error: `${leaf} is a symbol list — set it in the JSON editor.` };
  }
  const n = finite(field?.value ?? '');
  if (n === null) return { ok: false, error: `${leaf}: must be a number.` };
  if (control.type === 'int') {
    if (!Number.isInteger(n)) return { ok: false, error: `${leaf}: must be a whole number of days.` };
    if (isDteKey(control.key) && (n < DTE_MIN || n > DTE_MAX)) {
      return {
        ok: false,
        error:
          `${leaf}: the stored chain lake reaches ${DTE_MIN}-${DTE_MAX} days, so a target outside ` +
          `that replays against expiries the lake does not hold.`,
      };
    }
    return { ok: true, value: n };
  }
  return { ok: true, value: n };
}

/** Deep-equal for the four shapes a coerced value can take. */
function sameValue(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => sameValue(v, b[i]));
  }
  if (typeof a === 'number' && typeof b === 'number') return a === b;
  return a === b;
}

export interface TweakDiff {
  /** Keys whose coerced value differs from this run's base value. */
  changed: string[];
  /** `{dotted key: coerced value}` for exactly those keys. */
  overrides: Record<string, unknown>;
  /** Coercion failures, in control order. Blank fields never appear here. */
  errors: string[];
}

/**
 * The diff to base — the arm's whole content.
 *
 * A field left blank is UNTOUCHED (no override, no error): a control whose base
 * value the run never recorded prefills empty, and an empty control the
 * operator ignored must not refuse the submit for the control next to it.
 */
export function diffToBase(controls: TweakControl[], fields: FieldMap): TweakDiff {
  const changed: string[] = [];
  const overrides: Record<string, unknown> = {};
  const errors: string[] = [];
  for (const control of editableControls(controls)) {
    const field = fields[control.key];
    if (isBlank(control, field)) continue;
    const coerced = coerceField(control, field);
    if (!coerced.ok) {
      errors.push(coerced.error);
      continue;
    }
    if (!control.baseMissing && sameValue(coerced.value, control.baseValue)) continue;
    changed.push(control.key);
    overrides[control.key] = coerced.value;
  }
  return { changed, overrides, errors };
}

/** `caps.scenario_name_pattern` admits `[A-Za-z0-9]` then `[A-Za-z0-9_.-]*`. */
const NAME_SAFE = /[^A-Za-z0-9_.-]/g;
const MAX_NAME_CHARS = 40;

const valueToken = (value: unknown): string => {
  if (Array.isArray(value)) return value.map(valueToken).join('-');
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return String(value);
};

/**
 * The arm's name, built FROM the change so the grid column says what it is.
 *
 * `strategy.min_put_premium = 0.6` becomes `min_put_premium_0.6`, and a band
 * becomes `put_delta_range_0.15-0.25`. The leaf is used rather than the dotted
 * key because a dot in a name reads as a config path in a column header, and
 * because the 40-character cap is tight.
 */
export function armNameFor(changed: string[], overrides: Record<string, unknown>): string | null {
  if (changed.length === 0) return null;
  const raw = changed
    .map((key) => `${key.split('.').pop() ?? key}_${valueToken(overrides[key])}`)
    .join('_');
  let name = raw.replace(NAME_SAFE, '_').slice(0, MAX_NAME_CHARS);
  // The pattern demands an alphanumeric FIRST character, and a leading `-`
  // (from a negative value) or `_` would be refused by the runner with a
  // message about a name the operator never typed.
  name = name.replace(/^[^A-Za-z0-9]+/, '');
  if (!name) name = 'tweak';
  // `base` is reserved and runs implicitly; an arm that claimed it is a 422.
  return name === 'base' ? 'tweak_base' : name;
}

/** The subset of a run's stored spec a tweak carries forward verbatim. */
export interface RunSpecBasis {
  symbols: string[];
  start: string;
  end: string;
  holdout_start?: string;
  starting_cash?: number;
  run_sensitivity?: boolean;
}

/**
 * The run's own spec, parsed out of `spec_json`.
 *
 * `null` when the row carries no spec or an unreadable one — the bar then has
 * no window to submit against and says so, rather than inventing a default
 * window that would answer a different question from the one on screen.
 */
export function parseRunSpec(specJson: string | null | undefined): RunSpecBasis | null {
  if (!specJson) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(specJson);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  const spec = parsed as Record<string, unknown>;
  const symbols = Array.isArray(spec.symbols)
    ? spec.symbols.filter((s): s is string => typeof s === 'string')
    : [];
  if (symbols.length === 0) return null;
  if (typeof spec.start !== 'string' || typeof spec.end !== 'string') return null;
  const basis: RunSpecBasis = { symbols, start: spec.start, end: spec.end };
  // Carried VERBATIM, all three. A tweak that quietly dropped `holdout_start`
  // would submit an in-sample-only run against a windowed one and put an
  // unmarked in-sample number beside a holdout number.
  if (typeof spec.holdout_start === 'string') basis.holdout_start = spec.holdout_start;
  if (typeof spec.starting_cash === 'number') basis.starting_cash = spec.starting_cash;
  if (typeof spec.run_sensitivity === 'boolean') basis.run_sensitivity = spec.run_sensitivity;
  return basis;
}

/** The exact body posted to `/api/v2/sims/run`. One arm, plus implicit base. */
export function tweakSpec(basis: RunSpecBasis, arm: SweepScenarioSpec): SweepSpec {
  const spec: SweepSpec = {
    symbols: basis.symbols,
    start: basis.start,
    end: basis.end,
    scenarios: [arm],
  };
  if (basis.holdout_start) spec.holdout_start = basis.holdout_start;
  if (basis.starting_cash !== undefined) spec.starting_cash = basis.starting_cash;
  if (basis.run_sensitivity !== undefined) spec.run_sensitivity = basis.run_sensitivity;
  return spec;
}

export interface TweakBuild {
  spec: SweepSpec | null;
  armName: string | null;
  changed: string[];
  overrides: Record<string, unknown>;
  /** Every reason this cannot be submitted, in the order they were found. */
  errors: string[];
}

/**
 * The whole builder: controls + typed fields + the run's spec, to one POST body.
 *
 * ONE changed field per submit (plan §Non-goals: "v1 is one field -> one arm").
 * The multi-arm path is the JSON editor in `SubmitSweep`, which is still there
 * and still takes anything the runner accepts.
 */
export function buildTweak(args: {
  controls: TweakControl[];
  fields: FieldMap;
  basis: RunSpecBasis | null;
}): TweakBuild {
  const { changed, overrides, errors } = diffToBase(args.controls, args.fields);
  const all = [...errors];
  if (changed.length === 0 && errors.length === 0) {
    all.push('Nothing is changed yet — edit one field to build an arm.');
  }
  if (changed.length > 1) {
    all.push(
      `${changed.length} fields changed (${changed.join(', ')}). This bar submits ONE field per ` +
        'arm; use the JSON editor above for a multi-field arm.',
    );
  }
  if (!args.basis) {
    all.push('This run did not store a readable spec, so there is no window to replay against.');
  }
  const armName = changed.length === 1 ? armNameFor(changed, overrides) : null;
  if (all.length > 0 || !armName || !args.basis) {
    return { spec: null, armName, changed, overrides, errors: all };
  }
  return {
    spec: tweakSpec(args.basis, { name: armName, overrides }),
    armName,
    changed,
    overrides,
    errors: [],
  };
}
