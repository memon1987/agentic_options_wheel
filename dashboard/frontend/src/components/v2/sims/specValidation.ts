// FC-060 Layer 4 (PR-B): client-side validation of a sweep spec.
//
// This is a COURTESY, not the gate. `POST /api/v2/sweeps` re-validates every
// rule here against `src/backtesting/scenarios/overrides.py` — the same module
// the Job imports — and a 422 from the server is rendered verbatim, never
// reworded. The reason to validate here at all is that a sweep costs 6-8 wall
// minutes on a Cloud Run Job: a typo caught before submit is a typo that did
// not burn an execution slot (only one sweep may run at a time).
//
// Pure by design: no React, no fetch. Everything the form's disabled/enabled
// state machine decides comes out of `validateSpec`, so the state machine is
// testable without rendering anything.

import type {
  SweepAllowlist,
  SweepCaps,
  SweepScenarioSpec,
  SweepSpec,
} from '../../../types/v2';

/** `base` is implicit and always runs first — it is the comparator. */
export const BASE_SCENARIO_NAME = 'base';

/** Used only until `/allowlist` answers; the server's caps always win. */
export const FALLBACK_CAPS: SweepCaps = {
  max_symbols: 12,
  max_scenarios: 20,
  max_cells: 240,
  max_window_days: 730,
  min_holdout_days: 60,
};

const SYMBOL_RE = /^[A-Z.]{1,6}$/;
const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

// The server's `validate_spec` requires only a non-empty name after trimming —
// no character class, no length cap. A stricter client rule would refuse a spec
// the API accepts, which is the one direction client validation must never fail
// in: the operator would be blocked by a rule that does not exist.
const isUsableScenarioName = (name: string): boolean => name.trim().length > 0;

/** Fallbacks only; the live `caps` from `/allowlist` always win. */
export const MIN_STARTING_CASH = 10_000;
export const MAX_STARTING_CASH = 1_000_000;

/** A single validation failure, addressed to one region of the form. */
export interface SpecIssue {
  field: 'symbols' | 'window' | 'holdout' | 'scenarios' | 'cash' | 'caps';
  message: string;
}

export interface SpecValidation {
  valid: boolean;
  issues: SpecIssue[];
  /** (scenarios + implicit base) x symbols x splits — what `max_cells` bounds. */
  cellCount: number;
  splitCount: number;
}

// --- date helpers ---------------------------------------------------------- //
// Windows are pure calendar dates. Everything here works on the YYYY-MM-DD
// string via UTC epoch days, so a viewer in Tokyo and a viewer in Los Angeles
// compute the same window (FC-028's lesson, applied to arithmetic rather than
// to rendering).

const MS_PER_DAY = 86_400_000;

export const isIsoDate = (s: string | null | undefined): boolean => {
  if (!s || !ISO_DATE_RE.test(s)) return false;
  const t = Date.parse(`${s}T00:00:00Z`);
  if (Number.isNaN(t)) return false;
  // Reject 2026-02-31 and friends: Date.parse rolls them over silently.
  return new Date(t).toISOString().slice(0, 10) === s;
};

export const toIsoDate = (d: Date): string => d.toISOString().slice(0, 10);

export const addDays = (iso: string, days: number): string =>
  toIsoDate(new Date(Date.parse(`${iso}T00:00:00Z`) + days * MS_PER_DAY));

export const daysBetween = (a: string, b: string): number =>
  Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / MS_PER_DAY);

/** Yesterday, in UTC. The vendor has no bar for today until the close. */
export const defaultWindowEnd = (now: Date = new Date()): string =>
  toIsoDate(new Date(now.getTime() - MS_PER_DAY));

export const DEFAULT_WINDOW_DAYS = 365;
export const DEFAULT_HOLDOUT_DAYS = 90;

// --- override-key checking -------------------------------------------------- //

export type OverrideKeyVerdict =
  | { ok: true }
  /** Verbatim from `/allowlist` — the runner's own words, never paraphrased. */
  | { ok: false; reason: string };

export function checkOverrideKey(key: string, allowlist: SweepAllowlist | null): OverrideKeyVerdict {
  // No allowlist yet (the fetch is in flight, or failed). Refusing every key
  // would make the form permanently invalid on a transient GET failure, so keys
  // pass client-side and the server's 422 becomes the authority.
  if (!allowlist) return { ok: true };
  const rejected = allowlist.rejected.find((r) => r.key === key);
  if (rejected) return { ok: false, reason: rejected.reason };
  if (allowlist.allowed.some((a) => a.key === key)) return { ok: true };
  const known = allowlist.allowed.map((a) => a.key).sort().join(', ');
  return {
    ok: false,
    reason: `${key} is not an allowed override. The allowlist is: ${known}.`,
  };
}

// --- the scenarios JSON editor ---------------------------------------------- //

export interface ScenarioParse {
  scenarios: SweepScenarioSpec[] | null;
  error: string | null;
}

/**
 * Parse the JSON editor's contents into arms.
 *
 * Accepts either a bare array of arms or `{scenarios: [...]}` — the second
 * mirrors `examples/scenarios_example.yaml`, so an operator can paste the shape
 * they already know. Shape errors are reported as text, not thrown.
 */
export function parseScenariosJson(text: string): ScenarioParse {
  const trimmed = text.trim();
  if (!trimmed) return { scenarios: [], error: null };
  let raw: unknown;
  try {
    raw = JSON.parse(trimmed);
  } catch (err) {
    return { scenarios: null, error: `Not valid JSON: ${(err as Error).message}` };
  }
  const list =
    Array.isArray(raw)
      ? raw
      : raw && typeof raw === 'object' && Array.isArray((raw as { scenarios?: unknown }).scenarios)
        ? (raw as { scenarios: unknown[] }).scenarios
        : null;
  if (!list) {
    return { scenarios: null, error: 'Expected an array of arms, or {"scenarios": [...]}.' };
  }
  const out: SweepScenarioSpec[] = [];
  for (let i = 0; i < list.length; i++) {
    const item = list[i];
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      return { scenarios: null, error: `Arm ${i + 1} is not an object.` };
    }
    const obj = item as Record<string, unknown>;
    if (typeof obj.name !== 'string') {
      return { scenarios: null, error: `Arm ${i + 1} has no string "name".` };
    }
    const overrides = obj.overrides;
    if (overrides !== undefined && (typeof overrides !== 'object' || overrides === null || Array.isArray(overrides))) {
      return { scenarios: null, error: `Arm "${obj.name}": "overrides" must be an object of dotted keys.` };
    }
    const haircut = obj.fill_haircut;
    if (haircut !== undefined && typeof haircut !== 'number') {
      return { scenarios: null, error: `Arm "${obj.name}": "fill_haircut" must be a number.` };
    }
    out.push({
      name: obj.name,
      overrides: (overrides as Record<string, unknown>) ?? {},
      ...(haircut === undefined ? {} : { fill_haircut: haircut }),
    });
  }
  return { scenarios: out, error: null };
}

export const serialiseScenarios = (scenarios: SweepScenarioSpec[]): string =>
  JSON.stringify(scenarios, null, 2);

// --- the state machine ------------------------------------------------------ //

export interface ValidateArgs {
  spec: Partial<SweepSpec>;
  /** Holdout is ON by default; disabling it is what makes a run in-sample only. */
  holdoutEnabled: boolean;
  allowlist: SweepAllowlist | null;
  /** A JSON parse error from the editor — surfaced as a scenarios issue. */
  scenarioParseError?: string | null;
}

/**
 * The submit button is enabled IFF this returns `valid`.
 *
 * The three things required before a submit is even possible: at least one
 * symbol, a well-formed window, and at least one declared arm.
 *
 * There is no fourth. A submit token used to be one — the operator pasted
 * `SWEEP_SUBMIT_TOKEN` into the form — and FC-096 Phase D retired it: the
 * browser's IAP session is the credential now, and the page cannot see whether
 * it is still valid, so there is nothing here to validate. A signed-out session
 * surfaces when the submit comes back 401, as its own state.
 */
export function validateSpec(args: ValidateArgs): SpecValidation {
  const { spec, holdoutEnabled, allowlist, scenarioParseError } = args;
  const caps = allowlist?.caps ?? FALLBACK_CAPS;
  const issues: SpecIssue[] = [];

  // --- symbols ---
  const symbols = spec.symbols ?? [];
  if (symbols.length === 0) {
    issues.push({ field: 'symbols', message: 'Pick at least one symbol.' });
  }
  const bad = symbols.filter((s) => !SYMBOL_RE.test(s));
  if (bad.length > 0) {
    issues.push({
      field: 'symbols',
      message: `Not symbols: ${bad.join(', ')} — upper-case letters and dots, 1-6 characters.`,
    });
  }
  if (new Set(symbols).size !== symbols.length) {
    issues.push({ field: 'symbols', message: 'The same symbol is listed twice.' });
  }
  if (symbols.length > caps.max_symbols) {
    issues.push({
      field: 'caps',
      message: `${symbols.length} symbols exceeds the cap of ${caps.max_symbols}.`,
    });
  }

  // --- window ---
  const start = spec.start ?? '';
  const end = spec.end ?? '';
  let windowOk = true;
  if (!isIsoDate(start) || !isIsoDate(end)) {
    windowOk = false;
    issues.push({ field: 'window', message: 'Start and end must both be real YYYY-MM-DD dates.' });
  } else if (daysBetween(start, end) <= 0) {
    windowOk = false;
    issues.push({ field: 'window', message: 'The window ends on or before it starts.' });
  } else if (daysBetween(start, end) > caps.max_window_days) {
    issues.push({
      field: 'caps',
      message: `A ${daysBetween(start, end)}-day window exceeds the cap of ${caps.max_window_days} days.`,
    });
  }

  // --- holdout ---
  // Holdout ON is the default because a ranking chosen on the window it was
  // measured on is a hypothesis, not a result. Turning it off is legal; the form
  // shows the in-sample warning inline when it is off, and the report itself
  // carries the banner.
  const holdout = spec.holdout_start;
  const splitCount = holdoutEnabled ? 2 : 1;
  if (holdoutEnabled) {
    if (!isIsoDate(holdout)) {
      issues.push({ field: 'holdout', message: 'Holdout start must be a real YYYY-MM-DD date.' });
    } else if (windowOk) {
      const h = holdout as string;
      if (daysBetween(start, h) <= 0 || daysBetween(h, end) <= 0) {
        issues.push({
          field: 'holdout',
          message: 'Holdout start must fall strictly inside the window (start < holdout < end).',
        });
      } else if (daysBetween(h, end) < caps.min_holdout_days) {
        issues.push({
          field: 'holdout',
          message:
            `A ${daysBetween(h, end)}-day holdout is under the ${caps.min_holdout_days}-day minimum. ` +
            'A short holdout mostly inflates `insuf`: a cycle needs a put written, held and resolved.',
        });
      }
    }
  }

  // --- scenarios ---
  const scenarios = spec.scenarios ?? [];
  if (scenarioParseError) {
    issues.push({ field: 'scenarios', message: scenarioParseError });
  } else {
    if (scenarios.length === 0) {
      issues.push({
        field: 'scenarios',
        message: 'Declare at least one arm. `base` runs implicitly as the comparator.',
      });
    }
    if (scenarios.length > caps.max_scenarios) {
      issues.push({
        field: 'caps',
        message: `${scenarios.length} arms exceeds the cap of ${caps.max_scenarios}.`,
      });
    }
    const seen = new Set<string>();
    for (const arm of scenarios) {
      if (arm.name === BASE_SCENARIO_NAME) {
        issues.push({
          field: 'scenarios',
          message: '`base` is reserved — it runs implicitly and must not be declared.',
        });
      }
      if (!isUsableScenarioName(arm.name)) {
        issues.push({ field: 'scenarios', message: 'An arm has an empty name.' });
      }
      if (seen.has(arm.name)) {
        issues.push({ field: 'scenarios', message: `Two arms are both named "${arm.name}".` });
      }
      seen.add(arm.name);
      const keys = Object.keys(arm.overrides ?? {});
      if (keys.length === 0 && arm.fill_haircut === undefined) {
        issues.push({
          field: 'scenarios',
          message: `"${arm.name}" has no overrides and no fill_haircut — it would be a duplicate of base.`,
        });
      }
      for (const key of keys) {
        const verdict = checkOverrideKey(key, allowlist);
        if (!verdict.ok) {
          issues.push({ field: 'scenarios', message: `"${arm.name}": ${verdict.reason}` });
        }
      }
      if (
        arm.fill_haircut !== undefined &&
        (!Number.isFinite(arm.fill_haircut) || arm.fill_haircut < 0 || arm.fill_haircut > 1)
      ) {
        issues.push({
          field: 'scenarios',
          message: `"${arm.name}": fill_haircut must be between 0 (mid) and 1 (at the bid).`,
        });
      }
    }
  }

  // --- starting cash ---
  const cash = spec.starting_cash;
  // Bounds from the served caps when they are there — a hard-coded pair would
  // silently diverge the first time the server moved one.
  const minCash = caps.min_starting_cash ?? MIN_STARTING_CASH;
  const maxCash = caps.max_starting_cash ?? MAX_STARTING_CASH;
  if (cash !== undefined && (!Number.isFinite(cash) || cash < minCash || cash > maxCash)) {
    issues.push({
      field: 'cash',
      message: `Starting cash must be between $${minCash.toLocaleString()} and $${maxCash.toLocaleString()}.`,
    });
  }

  // --- cells ---
  // +1 for the implicit base arm; the runner replays it too and it counts
  // against the cap exactly like any other.
  const cellCount = (scenarios.length + 1) * symbols.length * splitCount;
  if (cellCount > caps.max_cells) {
    issues.push({
      field: 'caps',
      message:
        `${cellCount} cells ((${scenarios.length} arms + base) x ${symbols.length} symbols x ` +
        `${splitCount} split${splitCount === 1 ? '' : 's'}) exceeds the cap of ${caps.max_cells}.`,
    });
  }

  return { valid: issues.length === 0, issues, cellCount, splitCount };
}

/** The exact body posted to `/api/v2/sweeps`. Optional keys are omitted, not nulled. */
export function buildSpec(args: {
  symbols: string[];
  start: string;
  end: string;
  holdoutEnabled: boolean;
  holdoutStart: string;
  startingCash: number | undefined;
  runSensitivity: boolean;
  scenarios: SweepScenarioSpec[];
}): SweepSpec {
  const spec: SweepSpec = {
    symbols: args.symbols,
    start: args.start,
    end: args.end,
    scenarios: args.scenarios,
    run_sensitivity: args.runSensitivity,
  };
  if (args.holdoutEnabled && args.holdoutStart) spec.holdout_start = args.holdoutStart;
  if (args.startingCash !== undefined) spec.starting_cash = args.startingCash;
  return spec;
}
