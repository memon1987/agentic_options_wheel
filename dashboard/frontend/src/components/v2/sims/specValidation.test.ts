// FC-060 Layer 4 (PR-B): the submit form's validation state machine.
//
// The regression each block is here to catch is named in its describe(). The
// point of testing the machine rather than the rendered form is that the form's
// disabled/enabled state IS this function's `valid` — so these cases pin the
// button without rendering anything.

import { describe, expect, it } from 'vitest';
import type { SweepAllowlist, SweepScenarioSpec } from '../../../types/v2';
import {
  DEFAULT_HOLDOUT_DAYS,
  DEFAULT_WINDOW_DAYS,
  addDays,
  buildSpec,
  checkOverrideKey,
  defaultWindowEnd,
  isIsoDate,
  parseScenariosJson,
  validateSpec,
} from './specValidation';

const ALLOWLIST: SweepAllowlist = {
  allowed: [
    { key: 'strategy.put_delta_range', description: 'put delta band [lo, hi]' },
    { key: 'strategy.min_call_premium', description: 'call premium floor, $/share' },
    { key: 'strategy.max_stock_price', description: 'stage-1 price ceiling' },
  ],
  rejected: [
    {
      // A REAL rejection, kept real on purpose: DTE became sweepable in FC-096
      // Phase A, and a fixture that goes on teaching the retired refusal is the
      // first thing a reader checks and the first thing they find false.
      key: 'universe.min_open_interest',
      reason:
        'the engine has no open-interest data: get_options_chain hardcodes open_interest: 0, so any floor >= 1 rejects EVERY call.',
    },
  ],
  presets: [{ name: 'price_ceiling_800', overrides: { 'strategy.max_stock_price': 800 } }],
  caps: {
    max_symbols: 12,
    max_scenarios: 20,
    max_cells: 240,
    max_window_days: 730,
    min_holdout_days: 60,
  },
};

const ARM: SweepScenarioSpec = {
  name: 'puts_15_25',
  overrides: { 'strategy.put_delta_range': [0.15, 0.25] },
};

const END = '2026-08-28';
const START = addDays(END, -365);
const HOLDOUT = addDays(END, -90);

const good = (over: Record<string, unknown> = {}) => ({
  spec: {
    symbols: ['AAPL', 'NVDA'],
    start: START,
    end: END,
    holdout_start: HOLDOUT,
    scenarios: [ARM],
    ...over,
  },
  holdoutEnabled: true,
  allowlist: ALLOWLIST,
});

const messages = (v: ReturnType<typeof validateSpec>) => v.issues.map((i) => i.message).join(' | ');

describe('validateSpec — the four things a submit needs', () => {
  it('accepts a well-formed spec', () => {
    const v = validateSpec(good());
    expect(v.valid, messages(v)).toBe(true);
  });

  it('is invalid with no symbols', () => {
    const v = validateSpec(good({ symbols: [] }));
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/at least one symbol/i);
  });

  it('is invalid with a malformed window', () => {
    expect(validateSpec(good({ start: '', end: END })).valid).toBe(false);
    // End on or before start is not a zero-length window, it is a mistake.
    expect(validateSpec(good({ start: END, end: END })).valid).toBe(false);
    expect(validateSpec(good({ start: END, end: START })).valid).toBe(false);
  });

  it('is invalid with no declared arm — `base` alone is not a comparison', () => {
    const v = validateSpec(good({ scenarios: [] }));
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/at least one arm/i);
  });

  it('is VALID with no credential of any kind (FC-096 Phase D PR-2)', () => {
    // The submit token was the fourth requirement and it is retired. The
    // browser's IAP session is the credential now, and this function cannot
    // see it — so there is nothing here to validate, and a lingering token
    // rule would disable the button for every operator, permanently.
    expect(validateSpec(good()).valid).toBe(true);
    expect(messages(validateSpec(good()))).not.toMatch(/token/i);
  });
});

describe('validateSpec — holdout', () => {
  it('defaults to 90 days back from an end of yesterday', () => {
    const now = new Date('2026-08-29T18:00:00Z');
    const end = defaultWindowEnd(now);
    expect(end).toBe('2026-08-28');
    expect(addDays(end, -DEFAULT_WINDOW_DAYS)).toBe('2025-08-28');
    expect(addDays(end, -DEFAULT_HOLDOUT_DAYS)).toBe('2026-05-30');
  });

  it('refuses a holdout outside the window', () => {
    expect(validateSpec(good({ holdout_start: addDays(END, 5) })).valid).toBe(false);
    expect(validateSpec(good({ holdout_start: addDays(START, -5) })).valid).toBe(false);
  });

  it('refuses a holdout under the minimum — a short holdout inflates `insuf`', () => {
    const v = validateSpec(good({ holdout_start: addDays(END, -10) }));
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/under the 60-day minimum/);
  });

  it('ignores the holdout entirely when the toggle is off', () => {
    // Disabling the holdout is legal — it is what makes the run in-sample only.
    const v = validateSpec({ ...good({ holdout_start: 'nonsense' }), holdoutEnabled: false });
    expect(v.valid, messages(v)).toBe(true);
    expect(v.splitCount).toBe(1);
  });
});

describe('validateSpec — caps', () => {
  it('shows the symbol cap', () => {
    const many = Array.from({ length: 13 }, (_, i) => `SYM${i}`.slice(0, 6).toUpperCase());
    const v = validateSpec(good({ symbols: many }));
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/exceeds the cap of 12/);
  });

  it('shows the window cap', () => {
    const v = validateSpec(good({ start: addDays(END, -900), holdout_start: HOLDOUT }));
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/exceeds the cap of 730 days/);
  });

  it('shows the arm cap', () => {
    const arms = Array.from({ length: 21 }, (_, i) => ({ ...ARM, name: `arm_${i}` }));
    const v = validateSpec(good({ scenarios: arms }));
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/exceeds the cap of 20/);
  });

  it('counts cells as (arms + implicit base) x symbols x splits, and caps them', () => {
    const arms = Array.from({ length: 19 }, (_, i) => ({ ...ARM, name: `arm_${i}` }));
    const symbols = ['A', 'B', 'C', 'D', 'E', 'F', 'G'];
    const v = validateSpec(good({ scenarios: arms, symbols }));
    expect(v.cellCount).toBe(20 * 7 * 2); // 280
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/280 cells .* exceeds the cap of 240/);
  });

  it('uses the fallback caps when the allowlist has not answered', () => {
    const v = validateSpec({ ...good(), allowlist: null });
    expect(v.valid, messages(v)).toBe(true);
  });
});

describe('validateSpec — arms', () => {
  it('refuses a redeclared `base` — it runs implicitly as the comparator', () => {
    const v = validateSpec(good({ scenarios: [{ name: 'base', overrides: {} }] }));
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/reserved/);
  });

  it('refuses two arms with the same name', () => {
    const v = validateSpec(good({ scenarios: [ARM, { ...ARM }] }));
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/both named/);
  });

  it('refuses an arm with nothing in it — it would duplicate base', () => {
    const v = validateSpec(good({ scenarios: [{ name: 'empty', overrides: {} }] }));
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/no overrides, no fill_haircut and no roll_fill_mode/);
  });

  it('refuses a rejected override key WITH THE RUNNER’S OWN REASON', () => {
    const v = validateSpec(
      good({ scenarios: [{ name: 'oi_1', overrides: { 'universe.min_open_interest': 1 } }] }),
    );
    expect(v.valid).toBe(false);
    // Verbatim, not a paraphrase: the reason is the actionable half.
    expect(messages(v)).toContain('hardcodes open_interest: 0');
  });

  it('refuses an unknown override key and names the allowlist', () => {
    const v = validateSpec(good({ scenarios: [{ name: 'x', overrides: { 'strategy.nope': 1 } }] }));
    expect(v.valid).toBe(false);
    expect(messages(v)).toMatch(/not an allowed override/);
  });

  it('refuses a fill_haircut outside [0, 1]', () => {
    expect(validateSpec(good({ scenarios: [{ ...ARM, fill_haircut: 1.5 }] })).valid).toBe(false);
    expect(validateSpec(good({ scenarios: [{ ...ARM, fill_haircut: 1 }] })).valid).toBe(true);
  });
});

describe('checkOverrideKey', () => {
  it('passes every key when no allowlist is loaded — a failed GET must not lock the form', () => {
    expect(checkOverrideKey('anything.at.all', null)).toEqual({ ok: true });
  });

  it('prefers the rejection reason over the generic not-allowed message', () => {
    const verdict = checkOverrideKey('universe.min_open_interest', ALLOWLIST);
    expect(verdict.ok).toBe(false);
    expect((verdict as { reason: string }).reason).toContain('open_interest: 0');
  });
});

describe('parseScenariosJson', () => {
  it('accepts a bare array and the {scenarios: [...]} shape alike', () => {
    expect(parseScenariosJson('[]')).toEqual({ scenarios: [], error: null });
    const both = [
      parseScenariosJson(JSON.stringify([ARM])),
      parseScenariosJson(JSON.stringify({ scenarios: [ARM] })),
    ];
    expect(both[0].scenarios).toEqual(both[1].scenarios);
    expect(both[0].scenarios).toEqual([ARM]);
  });

  it('reports a syntax error as text rather than throwing', () => {
    const parsed = parseScenariosJson('{not json');
    expect(parsed.scenarios).toBeNull();
    expect(parsed.error).toMatch(/Not valid JSON/);
  });

  it('rejects an arm with no name', () => {
    expect(parseScenariosJson('[{"overrides":{}}]').error).toMatch(/no string "name"/);
  });

  it('rejects a non-object overrides bag', () => {
    expect(parseScenariosJson('[{"name":"a","overrides":[1]}]').error).toMatch(/must be an object/);
  });

  it('surfaces a parse error as a scenarios issue and blocks submit', () => {
    const v = validateSpec({ ...good(), scenarioParseError: 'Not valid JSON: boom' });
    expect(v.valid).toBe(false);
    expect(messages(v)).toContain('Not valid JSON: boom');
  });
});

describe('date helpers', () => {
  it('rejects a rolled-over date like 2026-02-31', () => {
    expect(isIsoDate('2026-02-31')).toBe(false);
    expect(isIsoDate('2026-02-28')).toBe(true);
    expect(isIsoDate('not-a-date')).toBe(false);
  });
});

describe('buildSpec', () => {
  it('omits holdout_start when the toggle is off, rather than sending null', () => {
    const spec = buildSpec({
      symbols: ['AAPL'],
      start: START,
      end: END,
      holdoutEnabled: false,
      holdoutStart: HOLDOUT,
      startingCash: undefined,
      runSensitivity: false,
      scenarios: [ARM],
    });
    expect('holdout_start' in spec).toBe(false);
    expect('starting_cash' in spec).toBe(false);
  });

  it('includes holdout_start and starting_cash when they are set', () => {
    const spec = buildSpec({
      symbols: ['AAPL'],
      start: START,
      end: END,
      holdoutEnabled: true,
      holdoutStart: HOLDOUT,
      startingCash: 100_000,
      runSensitivity: true,
      scenarios: [ARM],
    });
    expect(spec.holdout_start).toBe(HOLDOUT);
    expect(spec.starting_cash).toBe(100_000);
    expect(spec.run_sensitivity).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// FC-116 T9/T14 — the console can submit the roll fill mode at all
//
// `parseScenariosJson` REBUILDS every arm field by field, so before FC-116 any
// key it did not know was silently DROPPED: a `roll_fill_mode` typed into the
// console would have vanished between the textarea and the request, the run
// would have come back on the default, and nothing anywhere would have said the
// request was ignored. That is the regression this block exists for.
// --------------------------------------------------------------------------- //
describe('roll_fill_mode survives the arm rebuild (FC-116)', () => {
  it('carries the key through instead of dropping it', () => {
    const { scenarios, error } = parseScenariosJson(
      JSON.stringify([{ name: 'haircut_fills', roll_fill_mode: 'haircut' }]),
    );
    expect(error).toBeNull();
    expect(scenarios).toEqual([
      { name: 'haircut_fills', overrides: {}, roll_fill_mode: 'haircut' },
    ]);
  });

  it('keeps an explicit "limit" rather than folding it away here', () => {
    // The FOLD is `scenario_arm_hash`'s job, server-side: both spellings key
    // identically, and the stored spec still records what was asked for.
    const { scenarios } = parseScenariosJson(
      JSON.stringify([{ name: 'honest', roll_fill_mode: 'limit' }]),
    );
    expect(scenarios![0].roll_fill_mode).toBe('limit');
  });

  it('leaves the key absent when the arm does not set it', () => {
    const { scenarios } = parseScenariosJson(
      JSON.stringify([{ name: 'tighter', overrides: { 'strategy.min_call_premium': 0.5 } }]),
    );
    expect('roll_fill_mode' in scenarios![0]).toBe(false);
  });

  it('refuses a mode that is neither limit nor haircut', () => {
    const { scenarios, error } = parseScenariosJson(
      JSON.stringify([{ name: 'nonsense', roll_fill_mode: 'mid' }]),
    );
    expect(scenarios).toBeNull();
    expect(error).toMatch(/"limit" or "haircut"/);
  });

  it('a mode-only arm is NOT a duplicate of base', () => {
    // The regression arm of the whole before/after IS `{name, roll_fill_mode}`
    // with no overrides. Refusing it would refuse the comparison.
    const v = validateSpec(
      good({ scenarios: [{ name: 'haircut_fills', overrides: {}, roll_fill_mode: 'haircut' }] }),
    );
    expect(v.valid).toBe(true);
  });

  it('buildSpec carries it into the request body', () => {
    const spec = buildSpec({
      symbols: ['AAPL'],
      start: START,
      end: END,
      holdoutEnabled: false,
      holdoutStart: HOLDOUT,
      startingCash: undefined,
      runSensitivity: false,
      scenarios: [
        { name: 'haircut_fills', overrides: {}, roll_fill_mode: 'haircut' },
      ] as SweepScenarioSpec[],
    });
    expect(spec.scenarios[0].roll_fill_mode).toBe('haircut');
  });
});
