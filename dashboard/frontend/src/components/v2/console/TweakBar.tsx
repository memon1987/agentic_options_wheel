// FC-096 Phase E PR-4 — the tweak bar (the FC's "field-per-input controls").
//
// One control per allowed key, typed by the allowlist, prefilled from THIS
// run's effective config; change one field, submit, and land on the new run's
// matching cell. The whole of the arithmetic is in `tweakSpec.ts`; this file is
// state, layout, and the exact rendering of every refusal.
//
// Two things it deliberately does NOT do:
//
//  * **It never hides a control from a viewer** (decision 11). There is no
//    `whoami`, so a viewer sees the same bar and gets the backend's own 403
//    when they submit. A hidden button would teach them the feature does not
//    exist; the 403 tells them who to ask.
//  * **It never decides what may run.** The caps and the DTE reach are HINTS
//    that save a round trip; the sim service holds the real `Config` and its
//    refusal is what appears on screen, verbatim, in every case.

import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { SweepAllowlist, SweepRow, SweepSpec } from '../../../types/v2';
import { SIM_COLD_START_NOTE } from '../../../hooks/useSweeps';
import type { SimSubmitOutcome } from '../../../hooks/useSweeps';
import { BASE_SCENARIO_NAME, validateSpec } from '../sims/specValidation';
import {
  buildControls,
  buildTweak,
  editableControls,

  listOnlyControls,
  parseRunArms,
  parseRunSpec,
  prefill,
} from './tweakSpec';
import type { FieldMap, TweakControl } from './tweakSpec';
import PinButton from './PinButton';

/**
 * The guardrail sentence, and it is load-bearing rather than decorative.
 *
 * A tweak does not re-score THIS run: it submits a new one, whose implicit
 * `base` arm is replayed beside the changed field. So the Δ on the destination
 * is arm-vs-base INSIDE that run — which is what "always anchored to the
 * current base" means (signed decision, §Compare view) — and not this run's
 * cell measured against that run's cell.
 */
export const TWEAK_BASE_ANCHOR =
  'Every comparison stays anchored to base. This submits a NEW run: your one changed field ' +
  'becomes an arm, and the runner replays `base` beside it, so the Δ you read on the ' +
  'destination is that arm against that run’s own base. The values below are THIS run’s ' +
  'effective config, and only the field you change is sent as an override — every OTHER ' +
  'field takes the sim service’s CURRENT config, which is not necessarily what is shown ' +
  'here if the config has moved since this run. The new run’s `base_config_hash` is how ' +
  'to tell: a different hash means a different base, and the comparison is against THAT.';

export interface TweakBarProps {
  sweep: SweepRow;
  allowlist: SweepAllowlist | null;
  allowlistError: string | null;
  /** `base_config_json.effective`, parsed once by `Console`. */
  baseEffective: Record<string, unknown> | null;
  /** The cell on screen — the destination keeps the symbol and the split. */
  scenario: string;
  symbol: string;
  split: string;
  /**
   * The submit is OWNED BY THE PAGE (review round 1, R5).
   *
   * A sim submit waits up to 150 s, and this component is keyed on the run: the
   * operator selecting another run mid-flight unmounts it, and a `setState` in
   * the promise's tail then lands on nothing — a 409 or a 422 the operator
   * never sees, or a late 202 that yanks them off what they had moved on to
   * read. So the request, its in-flight flag and its outcome all live on the
   * page, keyed to the run it was submitted FROM, and this component only
   * renders them.
   */
  submitting: boolean;
  /**
   * The run whose submit is in flight. PAGE-WIDE (confirmation pass): one sim
   * runs at a time, so a flight started from another run disables this bar too
   * — and the operator is told which run holds it, rather than clicking a live
   * button whose handler silently returns.
   */
  submittingFrom: string | null;
  outcome: SimRefusal | null;
  onSubmit: (spec: SweepSpec, armName: string) => void;
  /** Clear a stale outcome the moment the spec it belonged to changes (R8). */
  onClearOutcome: () => void;
  /** Open another cell of THIS run — the already-asked arm (R2). */
  onOpenCell: (cell: { scenario: string; symbol: string; split: string }) => void;
}

const inputClass =
  'w-full rounded border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-100 ' +
  'focus:border-blue-600 focus:outline-none';

function ControlRow({
  control,
  field,
  onChange,
  disabled,
}: {
  control: TweakControl;
  field: FieldMap[string] | undefined;
  onChange: (next: FieldMap[string]) => void;
  disabled: boolean;
}) {
  const label = control.key;
  const common = { disabled, className: inputClass };
  return (
    <div data-testid={`tweak-control-${control.key}`} className="space-y-1">
      <label className="block text-xs font-mono text-gray-300" htmlFor={`tweak-${control.key}`}>
        {label}
      </label>
      {control.type === 'band' ? (
        <div className="flex items-center gap-1">
          <input
            {...common}
            id={`tweak-${control.key}`}
            aria-label={`${control.key} low`}
            value={field?.lo ?? ''}
            onChange={(e) => onChange({ ...field, lo: e.target.value })}
          />
          <span className="text-xs text-gray-500">to</span>
          <input
            {...common}
            aria-label={`${control.key} high`}
            value={field?.hi ?? ''}
            onChange={(e) => onChange({ ...field, hi: e.target.value })}
          />
        </div>
      ) : control.type === 'bool' ? (
        <select
          {...common}
          id={`tweak-${control.key}`}
          aria-label={control.key}
          value={field?.value ?? ''}
          onChange={(e) => onChange({ value: e.target.value })}
        >
          <option value="">(not recorded)</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      ) : (
        <input
          {...common}
          id={`tweak-${control.key}`}
          aria-label={control.key}
          inputMode="decimal"
          value={field?.value ?? ''}
          onChange={(e) => onChange({ value: e.target.value })}
        />
      )}
      <p className="text-[11px] text-gray-500 leading-snug">
        {control.baseMissing ? (
          <span className="text-amber-400">
            base value not recorded on this run — anything you enter here is a change from nothing.{' '}
          </span>
        ) : null}
        {control.description}
      </p>
    </div>
  );
}


/**
 * The outcomes that STAY on this screen: the two that navigate away are
 * excluded by the type, so the `default:` arm below cannot silently swallow an
 * acceptance if a future branch forgets to navigate.
 */
export type SimRefusal = Exclude<SimSubmitOutcome, { kind: 'accepted' } | { kind: 'deduplicated' }>;

/** Every refusal, in the server's own words, under a heading that says which. */
function Outcome({
  outcome,
  onRetry,
  canRetry,
}: {
  outcome: SimRefusal;
  onRetry: () => void;
  /** False when the bar holds no submittable spec — a live button that does
   *  nothing is worse than a disabled one that says why. */
  canRetry: boolean;
}) {
  const box = (tone: 'bad' | 'warn', heading: string, body: React.ReactNode) => (
    <div
      data-testid="tweak-outcome"
      role="status"
      aria-live="polite"
      data-outcome={outcome.kind}
      className={`rounded border px-3 py-2 text-xs ${
        tone === 'bad'
          ? 'border-red-800/60 bg-red-950/30 text-red-200'
          : 'border-amber-700/60 bg-amber-950/30 text-amber-200'
      }`}
    >
      <p className="font-semibold">{heading}</p>
      {body}
    </div>
  );
  const verbatim = (text: string) => (
    <p data-testid="tweak-detail" className="mt-1 whitespace-pre-wrap text-gray-300">
      {text}
    </p>
  );

  switch (outcome.kind) {
    case 'conflict': {
      const c = outcome.conflict;
      if (c.kind === 'busy') {
        return box(
          'warn',
          'A replay is already holding the sim service',
          <>
            {verbatim(c.detail)}
            {c.runId && (
              <p className="mt-1">
                <Link className="text-blue-400 underline" to={`/sims/${encodeURIComponent(c.runId)}`}>
                  Open run {c.runId}
                </Link>
              </p>
            )}
          </>,
        );
      }
      if (c.kind === 'coverage') {
        return box(
          'warn',
          'The chain lake does not cover this window',
          <>
            {verbatim(c.detail)}
            {c.missingSymbolDays !== null && (
              <p className="mt-1 text-gray-400">
                {c.missingSymbolDays} symbol-day{c.missingSymbolDays === 1 ? '' : 's'} missing.
              </p>
            )}
            {c.missing && (
              <ul data-testid="tweak-missing-days" className="mt-1 space-y-0.5 font-mono text-gray-400">
                {Object.entries(c.missing).map(([sym, days]) => (
                  <li key={sym}>
                    {sym}: {days.length} day{days.length === 1 ? '' : 's'} ({days.slice(0, 6).join(', ')}
                    {days.length > 6 ? ', …' : ''})
                  </li>
                ))}
              </ul>
            )}
          </>,
        );
      }
      if (c.kind === 'budget') {
        return box('warn', 'Over the interactive budget — use the batch path', verbatim(c.detail));
      }
      return box('warn', 'The sim service refused this spec', verbatim(c.detail));
    }
    case 'invalid':
      return box('bad', 'The spec was refused', verbatim(outcome.detail));
    case 'unauthorized':
      // Decision 11: no `whoami`, so the control was never hidden and this is
      // the first and only place a viewer learns they cannot submit.
      return box('bad', 'Signed in, but not an operator', verbatim(outcome.detail));
    case 'unauthenticated':
      return box('bad', 'Not authenticated', verbatim(outcome.detail));
    case 'session_expired':
      return box('bad', 'Session expired', verbatim(outcome.detail));
    case 'unreachable':
      return box(
        'warn',
        'The sim service could not be reached',
        <>
          {verbatim(outcome.detail)}
          <p data-testid="tweak-cold-start" className="mt-1 text-gray-400">
            {SIM_COLD_START_NOTE}
          </p>
          <button
            type="button"
            data-testid="tweak-retry"
            disabled={!canRetry}
            className={`mt-2 rounded border px-2 py-1 ${
              canRetry
                ? 'border-gray-600 text-gray-200 hover:bg-gray-700'
                : 'border-gray-700 text-gray-500 cursor-not-allowed'
            }`}
            onClick={onRetry}
          >
            Retry
          </button>
          {!canRetry && (
            <p data-testid="tweak-retry-disabled" className="mt-1 text-gray-500">
              This outcome is from a submit made before you navigated away, and the controls have
              been prefilled again since — change a field to build the arm and submit it.
            </p>
          )}
        </>,
      );
    case 'disabled':
      return box('bad', 'The sim service is not configured on this revision', verbatim(outcome.detail));
    default:
      return box(
        'bad',
        outcome.status ? `The submit failed (HTTP ${outcome.status})` : 'The submit failed',
        verbatim(outcome.detail),
      );
  }
}

export default function TweakBar({
  sweep,
  allowlist,
  allowlistError,
  baseEffective,
  scenario,
  symbol,
  split,
  submitting,
  submittingFrom,
  outcome,
  onSubmit,
  onClearOutcome,
  onOpenCell,
}: TweakBarProps) {
  const controls = useMemo(() => buildControls(allowlist, baseEffective), [allowlist, baseEffective]);
  const editable = useMemo(() => editableControls(controls), [controls]);
  const listOnly = useMemo(() => listOnlyControls(controls), [controls]);
  const basis = useMemo(() => parseRunSpec(sweep.spec_json), [sweep.spec_json]);
  const arms = useMemo(() => parseRunArms(sweep.spec_json), [sweep.spec_json]);

  // Only the keys the operator has TOUCHED live in state; everything else falls
  // back to its prefill. So a late-arriving allowlist prefills correctly with no
  // effect to synchronise, and "reset" is one `setEdits({})`.
  const [edits, setEdits] = useState<FieldMap>({});

  const fields = useMemo<FieldMap>(() => {
    const merged: FieldMap = {};
    for (const control of controls) merged[control.key] = edits[control.key] ?? prefill(control);
    return merged;
  }, [controls, edits]);

  const build = useMemo(
    () => buildTweak({ controls, fields, basis, arms }),
    [controls, fields, basis, arms],
  );

  // The SHARED validator the JSON editor uses (`specValidation.ts:198`) — the
  // cell cap, the window and the cash bounds, checked once rather than
  // reimplemented here with a second set of numbers that could drift.
  const verdict = useMemo(
    () =>
      build.spec
        ? validateSpec({
            spec: build.spec,
            holdoutEnabled: !!build.spec.holdout_start,
            allowlist,
          })
        : null,
    [build.spec, allowlist],
  );

  /**
   * A `validateSpec` issue on this bar is never about a control: every field it
   * checks — window, holdout, symbols, cash — is CARRIED from the run's own
   * spec, unedited. Worded as a control error it reads as "fix your input" for
   * an input that does not exist here (review round 1, LOW).
   */
  const carriedIssues = (verdict?.issues ?? []).map(
    (issue) =>
      `This run's own spec no longer satisfies the current caps (${issue.field}): ${issue.message} ` +
      'Nothing on this bar can change it — re-submit the run from the form above with a window ' +
      'that fits.',
  );
  const blocking = [...build.errors, ...carriedIssues];
  const spec: SweepSpec | null = build.spec && blocking.length === 0 ? build.spec : null;

  const edit = (key: string, next: FieldMap[string]) => {
    // R8: an outcome belongs to the spec that produced it. The moment the spec
    // changes, a refusal on screen is about a request nobody would make again.
    onClearOutcome();
    setEdits((prev) => ({ ...prev, [key]: next }));
  };

  const elsewhere = submitting && !!submittingFrom && submittingFrom !== sweep.run_id;

  const submit = () => {
    // R5/LOW: `submitting` is the page's flag, so a second click during the
    // flight cannot start a second POST even though this component remounted.
    if (!spec || submitting || !build.armName) return;
    onSubmit(spec, build.armName);
  };

  const armOverrides = (arm: string) => {
    const found = arms.find((a) => a.name === arm);
    if (!found) return null;
    const pairs = Object.entries(found.overrides).map(([k, v]) => `${k}: ${JSON.stringify(v)}`);
    return pairs.length > 0 ? pairs.join(', ') : 'no overrides';
  };

  return (
    <section data-testid="tweak-bar" className="rounded border border-gray-700 bg-gray-800 p-3 space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-white">Tweak one field and re-run</h3>
        <p data-testid="tweak-base-anchor" className="mt-1 text-xs text-gray-400 leading-snug">
          {TWEAK_BASE_ANCHOR}
        </p>
        {/* R2: the controls are the run's BASE values whatever cell is on
            screen. Read as the selected arm's config they are simply wrong,
            and the arm's own overrides are NOT carried into the tweak. */}
        {scenario !== BASE_SCENARIO_NAME && (
          <p data-testid="tweak-non-base-caption" className="mt-1 text-xs text-amber-400 leading-snug">
            The controls below show this run&rsquo;s <strong>base</strong> values, not{' '}
            <span className="font-mono">{scenario}</span>&rsquo;s.{' '}
            <span className="font-mono">{scenario}</span>&rsquo;s own overrides
            {armOverrides(scenario) ? ` (${armOverrides(scenario)})` : ''} are{' '}
            <strong>not carried</strong> into the arm this bar builds — it is built from base plus
            the one field you change.
          </p>
        )}
      </div>

      {allowlistError && (
        <p data-testid="tweak-allowlist-error" className="text-xs text-amber-400">
          The allowlist could not be read ({allowlistError}), so there are no controls to offer —
          the JSON editor above still submits to the batch Job.
        </p>
      )}

      {!allowlistError && !allowlist && (
        <p className="text-xs text-gray-400">Loading the allowlist…</p>
      )}

      {/* LOW: a LOADED allowlist that types nothing is a served fact, not a
          pending fetch, and "Loading…" for ever is the wrong reading of it. */}
      {!allowlistError && allowlist && editable.length === 0 && (
        <p data-testid="tweak-no-typed-keys" className="text-xs text-amber-400">
          This deploy&rsquo;s allowlist types no editable keys ({(allowlist.allowed ?? []).length}{' '}
          allowed, {Object.keys(allowlist.value_types ?? {}).length} typed), so there is nothing to
          offer here. The JSON editor above still takes any allowed override.
        </p>
      )}

      {/* A real form, so Enter in any field submits (review round 1, LOW). */}
      <form
        data-testid="tweak-form"
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
      {editable.length > 0 && (
        <div data-testid="tweak-controls" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {editable.map((control) => (
            <ControlRow
              key={control.key}
              control={control}
              field={fields[control.key]}
              disabled={submitting}
              onChange={(next) => edit(control.key, next)}
            />
          ))}
        </div>
      )}

      {listOnly.length > 0 && (
        <p data-testid="tweak-symbols-footnote" className="text-[11px] text-gray-500">
          {listOnly.map((c) => c.key).join(', ')} {listOnly.length === 1 ? 'is a symbol list' : 'are symbol lists'},
          not a field — set {listOnly.length === 1 ? 'it' : 'them'} in the JSON editor above.
        </p>
      )}

      {build.warnings.length > 0 && (
        <ul data-testid="tweak-warnings" className="space-y-1 text-xs text-amber-400">
          {build.warnings.map((message) => (
            <li key={message}>⚠ {message}</li>
          ))}
        </ul>
      )}

      {elsewhere && (
        <p data-testid="tweak-other-run-flight" className="text-xs text-amber-400">
          A submit from run <span className="font-mono">{submittingFrom}</span> is still in flight.
          The sim service replays one spec at a time, so this bar waits for it — its answer will
          appear on that run&rsquo;s screen.
        </p>
      )}

      {blocking.length > 0 && (
        <ul data-testid="tweak-blocking" className="space-y-1 text-xs text-amber-400">
          {blocking.map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      )}

      {/* R2b: the same question already answered on this run. A link, because
          the remedy is to READ that cell, not to re-measure it. */}
      {build.existingArm && (
        <button
          type="button"
          data-testid="tweak-open-existing-arm"
          className="text-xs text-blue-400 underline"
          onClick={() =>
            onOpenCell({ scenario: build.existingArm!.name, symbol, split })
          }
        >
          Open {build.existingArm.name} / {symbol} / {split}
        </button>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="submit"
          data-testid="tweak-submit"
          disabled={!spec || submitting}
          className={`rounded px-3 py-1 text-xs font-medium ${
            spec && !submitting
              ? 'bg-blue-700 text-white hover:bg-blue-600'
              : 'bg-gray-700 text-gray-400 cursor-not-allowed'
          }`}
        >
          {submitting ? 'Submitting…' : 'Run this tweak'}
        </button>
        {/* Keyed on the arm: a pin outcome belongs to the spec that produced
            it, and a changed field makes it a different question (R8). */}
        <PinButton key={build.armName ?? ''} spec={spec} />
        <span className="text-[11px] text-gray-500">
          {verdict
            ? `This submit is ${verdict.cellCount} cells (this arm + base) x ${
                basis?.symbols.length ?? 0
              } symbol(s) x ${verdict.splitCount} split(s); the SIM SERVICE caps one interactive ` +
              `run at ${allowlist?.caps.max_cells ?? '—'} cells and IT validates. A pin has its ` +
              'own standing cap, enforced when you pin.'
            : `Lands on ${symbol} / ${split} of the new run.`}
        </span>
        </div>
      </form>

      {outcome && <Outcome outcome={outcome} onRetry={submit} canRetry={!!spec && !submitting} />}
    </section>
  );
}
