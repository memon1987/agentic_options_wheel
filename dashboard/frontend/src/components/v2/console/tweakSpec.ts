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
/**
 * The engine's `MAX_SWEEPABLE_DTE`. Declared ONCE here and interpolated into
 * the refusal text below, so the bound and the sentence explaining it can never
 * disagree — and the allowlist's own `described` ("1-21 — bounded by the stored
 * chain lake's reach") is the server-side statement of the same rule.
 */
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
  /**
   * The run has NO base value for this key — absent, or recorded as `null`
   * (`universe.max_spread_pct` is `null` on the live run). Prefill is empty and
   * the control says so, and a value typed into it is a change from nothing
   * rather than a change from a number the operator never saw.
   */
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
 * Read an allowlist key out of the run's `effective` config.
 *
 * **`effective` is FLAT and its keys are the dotted allowlist keys themselves**
 * — verified against the live payload of run `13cc2729d1c74211`, whose
 * `base_config_json.effective` is `{"strategy.min_put_premium": 0.5, ...}` (the
 * NESTED sections live beside it, as `base_config_json.strategy` and friends).
 * The flat lookup is therefore the primary one; the nested walk is kept as a
 * fallback so a payload that ever moves to `{strategy: {min_put_premium}}` does
 * not silently prefill every control as "not recorded".
 */
export function readDotted(
  effective: Record<string, unknown> | null | undefined,
  key: string,
): unknown {
  if (!effective) return undefined;
  if (Object.prototype.hasOwnProperty.call(effective, key)) return effective[key];
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
      baseMissing: baseValue === undefined || baseValue === null,
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

/**
 * Decimal only. `Number()` alone accepts `0x10`, `0b11`, `1_000` and `Infinity`
 * — a config value typed as `0x10` would silently become 16, which is not what
 * anyone meant by a premium floor.
 */
const DECIMAL_RE = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

const finite = (raw: string): number | null => {
  const trimmed = raw.trim();
  if (!DECIMAL_RE.test(trimmed)) return null;
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
const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const MAX_NAME_CHARS = 40;
/**
 * `__` is the ARTIFACT NAME SEPARATOR, and `validate_scenario_name` refuses a
 * name that carries one (`identity.py:110-117`: object names are
 * `<run_id>/<scenario>__<symbol>__<split>.json.gz` and are parsed back with
 * `rsplit('__', 2)`). A single underscore is fine; two are a 422 on a name the
 * operator never typed, which is the worst kind of refusal to read.
 */
const ARTIFACT_NAME_SEPARATOR = '__';

/** The FULL contract `validate_scenario_name` applies, in one predicate. */
export function isValidArmName(name: string): boolean {
  return (
    name.length > 0 &&
    name.length <= MAX_NAME_CHARS &&
    NAME_PATTERN.test(name) &&
    !name.includes(ARTIFACT_NAME_SEPARATOR)
  );
}

const valueToken = (value: unknown): string => {
  if (Array.isArray(value)) return value.map(valueToken).join('-');
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return String(value);
};

/**
 * One `key_value` token, with the KEY trimmed if the pair is over the cap.
 *
 * The whole dotted key is used, not its leaf: `earnings.enabled` and
 * `rolling.enabled` share a leaf, and two arms both named `enabled_false` are
 * two different questions wearing one name — exactly what the compare view's
 * "same name, different arm" warning exists to catch, produced here on purpose.
 *
 * Two rules the review round found the first version breaking:
 *
 *  * **The value is never trimmed.** Truncating it would collapse
 *    `..._threshold_0.25` and `..._threshold_0.30` onto one name, so a second
 *    submit would look like a re-run of the first. `null` (refuse) is the
 *    answer when the value alone will not fit, not a shortened value.
 *  * **The trimmed key may not end in a separator.** `0.123456789` leaves a
 *    28-character budget, which cuts `rolling_imminence_extrinsic_threshold`
 *    to `rolling_imminence_extrinsic_` — and the joining `_` then makes the
 *    `__` the runner refuses.
 */
function keyValueToken(key: string, value: unknown): string | null {
  const valueTok = valueToken(value).replace(NAME_SAFE, '_');
  // One key character and the joining `_` are the minimum a pair needs.
  if (valueTok.length > MAX_NAME_CHARS - 2) return null;
  const budget = MAX_NAME_CHARS - valueTok.length - 1;
  const keyTok = key
    .replace(/\./g, '_')
    .replace(NAME_SAFE, '_')
    .slice(0, budget)
    // Trailing separators go BEFORE the join, which is what keeps `__` out.
    .replace(/[_.-]+$/, '');
  return keyTok ? `${keyTok}_${valueTok}` : null;
}

/**
 * The arm's name, built FROM the change so the grid column says what it is.
 *
 * `strategy.min_put_premium = 0.6` becomes `strategy_min_put_premium_0.6`, and
 * a band becomes `strategy_put_delta_range_0.15-0.25`.
 *
 * `null` means "no name this runner would accept" — the caller reports it as a
 * blocking error rather than posting a name that comes back 422.
 */
export function armNameFor(changed: string[], overrides: Record<string, unknown>): string | null {
  if (changed.length === 0) return null;
  const tokens = changed.map((key) => keyValueToken(key, overrides[key]));
  if (tokens.some((t) => t === null)) return null;
  let name = (tokens as string[])
    .join('_')
    .replace(NAME_SAFE, '_')
    // Collapse any run of underscores the sanitiser or the join produced: a
    // single `__` anywhere is a refusal, wherever it came from.
    .replace(/_{2,}/g, '_')
    // The pattern demands an alphanumeric FIRST character, and a leading `-`
    // (from a negative value) or `_` would be refused by the runner with a
    // message about a name the operator never typed.
    .replace(/^[^A-Za-z0-9]+/, '');
  // `base` is reserved and runs implicitly; an arm that claimed it is a 422.
  if (name === 'base') name = 'tweak_base';
  // The contract, asserted rather than assumed: anything that still fails it
  // is refused here, where the operator can see why, not by the service.
  return isValidArmName(name) ? name : null;
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
  /** Non-blocking notes — the submit is still allowed (review round 1, R7). */
  warnings: string[];
  /** The arm of THIS run that already asks this question, if any (R2). */
  existingArm: RunArm | null;
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
  /** The run's own declared arms, for the already-asked check (R2). */
  arms?: RunArm[];
}): TweakBuild {
  const { changed, overrides, errors } = diffToBase(args.controls, args.fields);
  const all = [...errors];
  const warnings = downwardOnlyWarnings(args.controls, args.fields);
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
  // A `null` name with exactly one change is not "nothing to do": it is a value
  // this runner cannot name, and saying so here is the difference between a
  // sentence the operator can act on and a 422 about a name they never typed.
  if (changed.length === 1 && armName === null && errors.length === 0) {
    all.push(
      `${changed[0]}: that value is too long to name an arm with (names are capped at ` +
        `${MAX_NAME_CHARS} characters and carry the value). Round it and try again.`,
    );
  }
  const existingArm = changed.length > 0 ? existingArmFor(overrides, args.arms ?? []) : null;
  if (existingArm) {
    all.push(
      `This run's arm \u201C${existingArm.name}\u201D already carries exactly these overrides. ` +
        'Re-running it would NOT deduplicate \u2014 the arm name is part of the engine identity ' +
        'and this bar derives a different one \u2014 so it would replay, in full, an answer ' +
        'already on screen.',
    );
  }
  if (all.length > 0 || !armName || !args.basis) {
    return { spec: null, armName, changed, overrides, errors: all, warnings, existingArm };
  }
  return {
    spec: tweakSpec(args.basis, { name: armName, overrides }),
    armName,
    changed,
    overrides,
    errors: [],
    warnings,
    existingArm: null,
  };
}

// --------------------------------------------------------------------------- //
// The run's OTHER arms (review round 1, R2)
// --------------------------------------------------------------------------- //

/** One arm as the run's own `spec_json` declared it. */
export interface RunArm {
  name: string;
  overrides: Record<string, unknown>;
}

/** Every declared arm of the run, `base` excluded (it is implicit). */
export function parseRunArms(specJson: string | null | undefined): RunArm[] {
  if (!specJson) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(specJson);
  } catch {
    return [];
  }
  const scenarios = (parsed as { scenarios?: unknown } | null)?.scenarios;
  if (!Array.isArray(scenarios)) return [];
  const arms: RunArm[] = [];
  for (const entry of scenarios) {
    if (!entry || typeof entry !== 'object') continue;
    const arm = entry as { name?: unknown; overrides?: unknown };
    if (typeof arm.name !== 'string' || !arm.name) continue;
    const overrides =
      arm.overrides && typeof arm.overrides === 'object' && !Array.isArray(arm.overrides)
        ? (arm.overrides as Record<string, unknown>)
        : {};
    arms.push({ name: arm.name, overrides });
  }
  return arms;
}

/** Numeric-folded deep equality over two override maps. */
export function sameOverrides(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every(
    (key) => Object.prototype.hasOwnProperty.call(b, key) && sameValue(a[key], b[key]),
  );
}

/**
 * The arm of THIS run that already asks this question, if there is one.
 *
 * Submitting it again would not deduplicate: the arm NAME is part of the
 * engine identity, and this bar derives a different name from the same
 * overrides — so the service would replay, in full, an answer already on
 * screen. Refused with a pointer to the cell instead.
 */
export function existingArmFor(
  overrides: Record<string, unknown>,
  arms: RunArm[],
): RunArm | null {
  return arms.find((arm) => sameOverrides(arm.overrides, overrides)) ?? null;
}

/**
 * A key the served description marks DOWNWARD ONLY, raised above its base.
 *
 * `risk.max_position_size` is the live instance: the sizer never sizes above
 * one contract (FC-079), so an arm that raises it replays to a grid cell
 * numerically identical to base. A WARNING and not a block — the allowlist
 * allows the key, the service will run it, and the operator may want the
 * negative result on the record. The rule is read off the server's own prose
 * rather than hardcoded to a key, so a second downward-only knob inherits it.
 */
export function downwardOnlyWarnings(controls: TweakControl[], fields: FieldMap): string[] {
  const warnings: string[] = [];
  for (const control of editableControls(controls)) {
    if (!/DOWNWARD ONLY/i.test(control.description)) continue;
    if (control.baseMissing || typeof control.baseValue !== 'number') continue;
    const field = fields[control.key];
    if (isBlank(control, field)) continue;
    const coerced = coerceField(control, field);
    if (!coerced.ok || typeof coerced.value !== 'number') continue;
    if (coerced.value > control.baseValue) {
      warnings.push(
        `${control.key} is ${coerced.value}, above this run's base of ${control.baseValue} — the ` +
          'served description says DOWNWARD ONLY, so raising it is inert and this arm will come ' +
          'back numerically identical to base. It will still run.',
      );
    }
  }
  return warnings;
}
