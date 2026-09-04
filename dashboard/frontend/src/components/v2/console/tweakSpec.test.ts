// FC-096 Phase E PR-4 — the spec builder, against the REAL allowlist and the
// REAL run row (`13cc2729d1c74211`), not a hand-written stand-in.
//
// The two facts these tests exist to pin, because both are silent when wrong:
//
//  1. `base_config_json.effective` is FLAT with dotted keys. A nested-only read
//     prefills every control as "not recorded" and every submit then sends an
//     override for a field the operator never touched.
//  2. The overrides are the DIFF. A builder that sent every control's value
//     would post 19 overrides for a one-field tweak, and the arm would differ
//     from base in ways nobody chose.

import { describe, expect, it } from 'vitest';
import allowlistFixture from '../../../test/fixtures/sweep_allowlist.json';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import type { SweepAllowlist } from '../../../types/v2';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import {
  armNameFor,
  buildControls,
  buildTweak,
  coerceField,
  diffToBase,
  downwardOnlyWarnings,
  editableControls,
  initialFields,
  isValidArmName,
  listOnlyControls,
  parseRunArms,
  parseRunSpec,
  prefill,
  readDotted,
  sameOverrides,
  tweakSpec,
} from './tweakSpec';
import { validateSpec } from '../sims/specValidation';

const allowlist = allowlistFixture as unknown as SweepAllowlist;
const sweep = normaliseSweepDetail(shaped13cc)!.sweep;
const effective = JSON.parse(sweep.base_config_json!).effective as Record<string, unknown>;
const controls = buildControls(allowlist, effective);
const control = (key: string) => controls.find((c) => c.key === key)!;

describe('controls come from allowed x value_types', () => {
  it('types every allowed key the live allowlist types, and nothing else', () => {
    // 20 allowed keys, 20 typed — the live payload. A control built from
    // `described` (prose) is the thing decision 9 forbids; a control for a key
    // the server does not type is a guess.
    expect(controls).toHaveLength(20);
    expect(new Set(controls.map((c) => c.type))).toEqual(
      new Set(['band', 'number', 'int', 'bool', 'symbols']),
    );
    expect(control('strategy.put_delta_range').type).toBe('band');
    expect(control('strategy.put_target_dte').type).toBe('int');
    expect(control('earnings.enabled').type).toBe('bool');
    expect(control('strategy.min_put_premium').type).toBe('number');
  });

  it('gives a `symbols` key NO editable control (decision 9)', () => {
    // The mutation: drop the filter. `universe.excluded_symbols` is a list, not
    // a field; a text box for it would submit a string where the runner wants
    // an array and be refused by the service with a type error.
    expect(control('universe.excluded_symbols').type).toBe('symbols');
    expect(editableControls(controls).map((c) => c.key)).not.toContain(
      'universe.excluded_symbols',
    );
    expect(listOnlyControls(controls).map((c) => c.key)).toEqual(['universe.excluded_symbols']);
    expect(editableControls(controls)).toHaveLength(19);
  });

  it('skips a key the server allows but does not type', () => {
    const partial = {
      ...allowlist,
      allowed: [...allowlist.allowed, { key: 'strategy.unknown_knob', description: 'new' }],
    };
    expect(buildControls(partial, effective).map((c) => c.key)).not.toContain(
      'strategy.unknown_knob',
    );
  });

  it('is empty while the allowlist has not loaded', () => {
    expect(buildControls(null, effective)).toEqual([]);
  });
});

describe('prefill reads the run’s effective config', () => {
  it('reads FLAT dotted keys — the shape the live run actually stores', () => {
    expect(readDotted(effective, 'strategy.min_put_premium')).toBe(0.5);
    expect(readDotted(effective, 'strategy.put_delta_range')).toEqual([0.1, 0.2]);
    expect(readDotted(effective, 'earnings.enabled')).toBe(true);
    expect(prefill(control('strategy.min_put_premium'))).toEqual({ value: '0.5' });
    expect(prefill(control('strategy.put_delta_range'))).toEqual({ lo: '0.1', hi: '0.2' });
    expect(prefill(control('earnings.enabled'))).toEqual({ value: 'true' });
    expect(prefill(control('strategy.put_target_dte'))).toEqual({ value: '7' });
  });

  it('falls back to a NESTED walk if the payload ever moves', () => {
    expect(readDotted({ strategy: { min_put_premium: 0.9 } }, 'strategy.min_put_premium')).toBe(0.9);
    expect(readDotted({ strategy: { min_put_premium: 0.9 } }, 'strategy.missing')).toBeUndefined();
    expect(readDotted(null, 'strategy.min_put_premium')).toBeUndefined();
  });

  it('marks a key the run recorded as null as MISSING, with an empty prefill', () => {
    // `universe.max_spread_pct` is `null` on the live run. Prefilling "null" as
    // a value would let it be sent back as an override of itself.
    expect(effective['universe.max_spread_pct']).toBeNull();
    expect(control('universe.max_spread_pct').baseMissing).toBe(true);
    expect(prefill(control('universe.max_spread_pct'))).toEqual({ value: '' });
    expect(control('strategy.min_put_premium').baseMissing).toBe(false);
  });

  it('prefills every control when the run has no base config at all', () => {
    const blind = buildControls(allowlist, null);
    expect(blind.every((c) => c.baseMissing)).toBe(true);
    const fields = initialFields(blind);
    expect(fields['strategy.min_put_premium']).toEqual({ value: '' });
    expect(fields['strategy.put_delta_range']).toEqual({ lo: '', hi: '' });
  });
});

describe('coercion by type', () => {
  it('accepts a well-formed band and refuses an inverted one', () => {
    const band = control('strategy.put_delta_range');
    expect(coerceField(band, { lo: '0.15', hi: '0.25' })).toEqual({ ok: true, value: [0.15, 0.25] });
    // The mutation: accept `lo >= hi`. The engine would take an empty band and
    // reject every contract, and the arm would read as "this config trades
    // nothing" rather than as a typo.
    const inverted = coerceField(band, { lo: '0.3', hi: '0.2' });
    expect(inverted.ok).toBe(false);
    expect((inverted as { error: string }).error).toContain('below its high end');
    const equal = coerceField(band, { lo: '0.2', hi: '0.2' });
    expect(equal.ok).toBe(false);
  });

  it('refuses a band outside [0, 1] and a non-numeric end', () => {
    const band = control('strategy.call_delta_range');
    expect(coerceField(band, { lo: '-0.1', hi: '0.5' }).ok).toBe(false);
    expect(coerceField(band, { lo: '0.1', hi: '1.5' }).ok).toBe(false);
    expect(coerceField(band, { lo: '0.1', hi: 'abc' }).ok).toBe(false);
  });

  it('holds an int to whole numbers, and a DTE key to the lake’s 1-21 reach', () => {
    const dte = control('strategy.put_target_dte');
    expect(coerceField(dte, { value: '14' })).toEqual({ ok: true, value: 14 });
    expect(coerceField(dte, { value: '7.5' }).ok).toBe(false);
    expect(coerceField(dte, { value: '0' }).ok).toBe(false);
    expect(coerceField(dte, { value: '22' }).ok).toBe(false);
    expect((coerceField(dte, { value: '22' }) as { error: string }).error).toContain('chain lake');
    // A non-DTE int is NOT bounded by the lake's reach.
    expect(coerceField(control('rolling.max_extension_days'), { value: '45' })).toEqual({
      ok: true,
      value: 45,
    });
  });

  it('coerces numbers and bools, and refuses anything else', () => {
    expect(coerceField(control('strategy.min_put_premium'), { value: '0.65' })).toEqual({
      ok: true,
      value: 0.65,
    });
    expect(coerceField(control('strategy.min_put_premium'), { value: 'x' }).ok).toBe(false);
    expect(coerceField(control('earnings.enabled'), { value: 'false' })).toEqual({
      ok: true,
      value: false,
    });
    expect(coerceField(control('earnings.enabled'), { value: 'yes' }).ok).toBe(false);
    expect(coerceField(control('universe.excluded_symbols'), { value: 'F' }).ok).toBe(false);
  });
});

describe('overrides are the DIFF to base, and nothing else', () => {
  const fields = initialFields(controls);

  it('sends NO overrides when every control still holds its prefill', () => {
    // The mutation: send every control's value. It would post 19 overrides for
    // a tweak of none, and the "arm" would restate base key by key.
    const diff = diffToBase(controls, fields);
    expect(diff.changed).toEqual([]);
    expect(diff.overrides).toEqual({});
    expect(diff.errors).toEqual([]);
  });

  it('sends exactly the changed key, with the coerced value', () => {
    const diff = diffToBase(controls, {
      ...fields,
      'strategy.min_put_premium': { value: '0.65' },
    });
    expect(diff.changed).toEqual(['strategy.min_put_premium']);
    expect(diff.overrides).toEqual({ 'strategy.min_put_premium': 0.65 });
  });

  it('treats a re-typed but numerically equal value as UNCHANGED', () => {
    // `0.50` and `0.5` are the same number; sending an override for one is
    // paying a cell to learn that base equals base.
    const diff = diffToBase(controls, {
      ...fields,
      'strategy.min_put_premium': { value: '0.50' },
      'strategy.put_delta_range': { lo: '0.10', hi: '0.20' },
    });
    expect(diff.changed).toEqual([]);
  });

  it('treats a blank field as untouched, never as an error', () => {
    const diff = diffToBase(controls, {
      ...fields,
      'strategy.min_put_premium': { value: '  ' },
      'strategy.put_delta_range': { lo: '', hi: '' },
    });
    expect(diff.changed).toEqual([]);
    expect(diff.errors).toEqual([]);
  });

  it('reports a coercion failure without dropping the other controls', () => {
    const diff = diffToBase(controls, {
      ...fields,
      'strategy.put_delta_range': { lo: '0.4', hi: '0.3' },
      'earnings.enabled': { value: 'false' },
    });
    expect(diff.errors).toHaveLength(1);
    expect(diff.changed).toEqual(['earnings.enabled']);
    expect(diff.overrides).toEqual({ 'earnings.enabled': false });
  });

  it('counts a value typed into a control the run never recorded as a change', () => {
    const diff = diffToBase(controls, {
      ...fields,
      'universe.max_spread_pct': { value: '0.05' },
    });
    expect(diff.changed).toEqual(['universe.max_spread_pct']);
  });
});

describe('the arm is named from the change', () => {
  it('names a scalar, a band and a bool from the WHOLE key', () => {
    expect(armNameFor(['strategy.min_put_premium'], { 'strategy.min_put_premium': 0.65 })).toBe(
      'strategy_min_put_premium_0.65',
    );
    expect(
      armNameFor(['strategy.put_delta_range'], { 'strategy.put_delta_range': [0.15, 0.25] }),
    ).toBe('strategy_put_delta_range_0.15-0.25');
    // The leaf alone would name BOTH `earnings.enabled` and `rolling.enabled`
    // `enabled_false` — two different questions under one arm name.
    expect(armNameFor(['earnings.enabled'], { 'earnings.enabled': false })).toBe(
      'earnings_enabled_false',
    );
    expect(armNameFor(['rolling.enabled'], { 'rolling.enabled': false })).toBe(
      'rolling_enabled_false',
    );
  });

  it('trims the KEY, never the value, when the pair is over the 40-char cap', () => {
    // Truncating the value would collapse two different values onto one name,
    // and the second submit would read as a dedup of the first.
    const key = 'rolling.imminence_extrinsic_threshold';
    const a = armNameFor([key], { [key]: 0.25 })!;
    const b = armNameFor([key], { [key]: 0.3 })!;
    expect(a.length).toBeLessThanOrEqual(40);
    expect(a.endsWith('_0.25')).toBe(true);
    expect(b.endsWith('_0.3')).toBe(true);
    expect(a).not.toBe(b);
  });

  it('produces a name the runner’s own pattern accepts', () => {
    const pattern = new RegExp(allowlist.caps.scenario_name_pattern!.replace('\\Z', '$'));
    for (const [key, value] of [
      ['strategy.min_put_premium', 0.65],
      ['strategy.put_delta_range', [0.15, 0.25]],
      ['earnings.enabled', true],
      ['rolling.min_net_credit_per_contract', -0.5],
      ['strategy.min_avg_volume', 2_500_000],
    ] as Array<[string, unknown]>) {
      const name = armNameFor([key], { [key]: value })!;
      expect(name).toMatch(pattern);
      expect(name.length).toBeLessThanOrEqual(allowlist.caps.max_scenario_name_chars ?? 40);
      expect(name).not.toBe('base');
    }
  });

  it('is null when nothing changed', () => {
    expect(armNameFor([], {})).toBeNull();
  });
});

describe('the submitted spec carries the run’s own question', () => {
  const basis = parseRunSpec(sweep.spec_json)!;

  it('reads the run’s window, symbols, cash and sensitivity verbatim', () => {
    expect(basis).toEqual({
      symbols: ['GOOGL'],
      start: '2025-09-01',
      end: '2026-09-01',
      holdout_start: '2026-06-03',
      starting_cash: 100000,
      run_sensitivity: false,
    });
  });

  it('carries holdout_start into the POST body — the mutation that drops it', () => {
    // Dropping it submits an IN-SAMPLE-ONLY run against a windowed one, and
    // the console would then sit an unmarked in-sample number beside a holdout
    // number with nothing on screen saying they are different kinds of answer.
    const spec = tweakSpec(basis, { name: 'x', overrides: { 'strategy.min_put_premium': 0.65 } });
    expect(spec.holdout_start).toBe('2026-06-03');
    expect(spec.start).toBe('2025-09-01');
    expect(spec.end).toBe('2026-09-01');
    expect(spec.symbols).toEqual(['GOOGL']);
    expect(spec.starting_cash).toBe(100000);
    expect(spec.scenarios).toHaveLength(1);
  });

  it('omits optional keys the run did not carry, rather than nulling them', () => {
    const spec = tweakSpec({ symbols: ['AAPL'], start: '2025-01-01', end: '2025-06-01' }, {
      name: 'x',
      overrides: {},
    });
    expect('holdout_start' in spec).toBe(false);
    expect('starting_cash' in spec).toBe(false);
    expect('run_sensitivity' in spec).toBe(false);
  });

  it('refuses a run whose spec is missing or unreadable', () => {
    expect(parseRunSpec(null)).toBeNull();
    expect(parseRunSpec('{ not json')).toBeNull();
    expect(parseRunSpec('{"symbols": [], "start": "a", "end": "b"}')).toBeNull();
    expect(parseRunSpec('{"symbols": ["A"], "start": "a"}')).toBeNull();
  });
});

describe('buildTweak — one field, one arm', () => {
  const basis = parseRunSpec(sweep.spec_json);
  const fields = initialFields(controls);
  const build = (f: typeof fields) => buildTweak({ controls, fields: f, basis });

  it('builds the whole POST body from one changed field', () => {
    const out = build({ ...fields, 'strategy.min_put_premium': { value: '0.65' } });
    expect(out.errors).toEqual([]);
    expect(out.armName).toBe('strategy_min_put_premium_0.65');
    expect(out.spec).toEqual({
      symbols: ['GOOGL'],
      start: '2025-09-01',
      end: '2026-09-01',
      holdout_start: '2026-06-03',
      starting_cash: 100000,
      run_sensitivity: false,
      scenarios: [
        { name: 'strategy_min_put_premium_0.65', overrides: { 'strategy.min_put_premium': 0.65 } },
      ],
    });
  });

  it('refuses an unchanged form, and says what to do', () => {
    const out = build(fields);
    expect(out.spec).toBeNull();
    expect(out.errors.join(' ')).toContain('Nothing is changed yet');
  });

  it('refuses TWO changed fields and points at the JSON editor (§Non-goals)', () => {
    const out = build({
      ...fields,
      'strategy.min_put_premium': { value: '0.65' },
      'earnings.enabled': { value: 'false' },
    });
    expect(out.spec).toBeNull();
    expect(out.changed).toHaveLength(2);
    expect(out.errors.join(' ')).toContain('ONE field per arm');
  });

  it('refuses when the run stored no readable spec', () => {
    const out = buildTweak({
      controls,
      fields: { ...fields, 'strategy.min_put_premium': { value: '0.65' } },
      basis: null,
    });
    expect(out.spec).toBeNull();
    expect(out.errors.join(' ')).toContain('no window to replay against');
  });

  it('passes the arm through the shared validator the JSON editor uses', () => {
    const out = build({ ...fields, 'strategy.put_delta_range': { lo: '0.15', hi: '0.25' } });
    const verdict = validateSpec({
      spec: out.spec!,
      holdoutEnabled: true,
      allowlist,
    });
    expect(verdict.valid).toBe(true);
    expect(verdict.cellCount).toBe(4);
  });
});

describe('the arm name satisfies the runner’s FULL contract (review R1)', () => {
  const contract = (name: string) => {
    const pattern = new RegExp(allowlist.caps.scenario_name_pattern!.replace('\\Z', '$'));
    return {
      pattern: pattern.test(name),
      length: name.length <= (allowlist.caps.max_scenario_name_chars ?? 40),
      noSeparator: !name.includes('__'),
    };
  };

  it('never emits `__`, which the artifact-name parse refuses', () => {
    // `validate_scenario_name` (identity.py:110-117) rejects `__` outright: it
    // is the field separator in `<run>/<scenario>__<symbol>__<split>.json.gz`.
    // Each of these three trims the key to a boundary that USED to leave a
    // trailing `_` right before the joining one.
    const cases: Array<[string, unknown]> = [
      ['rolling.imminence_extrinsic_threshold', 0.123456789],
      ['rolling.min_net_credit_per_contract', 0.3333333333],
      ['strategy.put_delta_range', [0.1234567, 0.23456789]],
    ];
    for (const [key, value] of cases) {
      const name = armNameFor([key], { [key]: value })!;
      expect(name).not.toBeNull();
      expect(contract(name)).toEqual({ pattern: true, length: true, noSeparator: true });
      expect(isValidArmName(name)).toBe(true);
    }
  });

  it('keeps the whole value in the name — it is the key that gives way', () => {
    const key = 'rolling.imminence_extrinsic_threshold';
    const a = armNameFor([key], { [key]: 0.123456789 })!;
    const b = armNameFor([key], { [key]: 0.123456788 })!;
    expect(a.endsWith('_0.123456789')).toBe(true);
    expect(a).not.toBe(b);
  });

  it('refuses (null) rather than truncating a value that cannot fit', () => {
    const key = 'strategy.put_delta_range';
    // Long enough that no useful part of the key survives — the name would be
    // legal and unreadable, so it is refused instead.
    const value = [0.12345678901234568, 0.2345678901234567];
    expect(armNameFor([key], { [key]: value })).toBeNull();
  });

  it('agrees with `isValidArmName` on the rules it enforces', () => {
    expect(isValidArmName('a__b')).toBe(false);
    expect(isValidArmName('_leading')).toBe(false);
    expect(isValidArmName('x'.repeat(41))).toBe(false);
    expect(isValidArmName('ok_0.5-0.6')).toBe(true);
  });
});

describe('the run’s other arms (review R2b)', () => {
  const fields = initialFields(controls);
  const arms = parseRunArms(sweep.spec_json);

  it('reads the run’s declared arms off spec_json', () => {
    expect(arms).toEqual([
      { name: 'position_20pct', overrides: { 'risk.max_position_size': 0.2 } },
    ]);
    expect(parseRunArms(null)).toEqual([]);
    expect(parseRunArms('{ not json')).toEqual([]);
  });

  it('refuses a tweak that reproduces an arm this run already has', () => {
    // Re-running it would NOT dedup: the arm name is part of the engine
    // identity and this bar derives a different one, so the service would
    // replay in full an answer already on screen.
    const out = buildTweak({
      controls,
      fields: { ...fields, 'risk.max_position_size': { value: '0.2' } },
      basis: parseRunSpec(sweep.spec_json),
      arms,
    });
    expect(out.spec).toBeNull();
    expect(out.existingArm?.name).toBe('position_20pct');
    expect(out.errors.join(' ')).toContain('would NOT deduplicate');
  });

  it('folds numerically — `0.20` is the same arm as `0.2`', () => {
    expect(sameOverrides({ a: 0.2 }, { a: 0.2 })).toBe(true);
    expect(sameOverrides({ a: [0.1, 0.2] }, { a: [0.1, 0.2] })).toBe(true);
    expect(sameOverrides({ a: 0.2 }, { a: 0.3 })).toBe(false);
    expect(sameOverrides({ a: 0.2 }, { a: 0.2, b: 1 })).toBe(false);
    const out = buildTweak({
      controls,
      fields: { ...initialFields(controls), 'risk.max_position_size': { value: '0.20' } },
      basis: parseRunSpec(sweep.spec_json),
      arms,
    });
    expect(out.existingArm?.name).toBe('position_20pct');
  });

  it('allows a DIFFERENT value for the same key', () => {
    const out = buildTweak({
      controls,
      fields: { ...fields, 'risk.max_position_size': { value: '0.25' } },
      basis: parseRunSpec(sweep.spec_json),
      arms,
    });
    expect(out.existingArm).toBeNull();
    expect(out.spec).not.toBeNull();
  });
});

describe('DOWNWARD ONLY keys warn without blocking (review R7)', () => {
  const fields = initialFields(controls);

  it('warns when the value is raised above base, and still submits', () => {
    // The served description says `risk.max_position_size` is DOWNWARD ONLY
    // until FC-079 — the sizer never sizes above one contract, so an arm that
    // raises it comes back numerically identical to base.
    expect(control('risk.max_position_size').description).toContain('DOWNWARD ONLY');
    expect(control('risk.max_position_size').baseValue).toBe(0.35);
    const out = buildTweak({
      controls,
      fields: { ...fields, 'risk.max_position_size': { value: '0.5' } },
      basis: parseRunSpec(sweep.spec_json),
    });
    expect(out.warnings.join(' ')).toContain('DOWNWARD ONLY');
    expect(out.warnings.join(' ')).toContain('identical to base');
    expect(out.spec).not.toBeNull();
    expect(out.errors).toEqual([]);
  });

  it('says nothing when the value is lowered', () => {
    const out = buildTweak({
      controls,
      fields: { ...fields, 'risk.max_position_size': { value: '0.25' } },
      basis: parseRunSpec(sweep.spec_json),
    });
    expect(out.warnings).toEqual([]);
  });

  it('reads the rule off the served prose, not a hardcoded key', () => {
    expect(downwardOnlyWarnings(controls, { ...fields, 'strategy.min_put_premium': { value: '9' } })).toEqual(
      [],
    );
  });
});

describe('a decimal is a decimal (review LOW)', () => {
  it('refuses hex, binary and separators that `Number()` would accept', () => {
    const premium = control('strategy.min_put_premium');
    expect(coerceField(premium, { value: '0x10' }).ok).toBe(false);
    expect(coerceField(premium, { value: '0b11' }).ok).toBe(false);
    expect(coerceField(premium, { value: '1_000' }).ok).toBe(false);
    expect(coerceField(premium, { value: 'Infinity' }).ok).toBe(false);
    expect(coerceField(premium, { value: '1e-2' })).toEqual({ ok: true, value: 0.01 });
    expect(coerceField(premium, { value: '.5' })).toEqual({ ok: true, value: 0.5 });
  });
});
