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
import { submitSim, SIM_COLD_START_NOTE } from '../../../hooks/useSweeps';
import type { SimSubmitOutcome } from '../../../hooks/useSweeps';
import { validateSpec } from '../sims/specValidation';
import {
  buildControls,
  buildTweak,
  editableControls,

  listOnlyControls,
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
  'effective config, and only the field you change is sent as an override.';

export interface TweakBarProps {
  sweep: SweepRow;
  allowlist: SweepAllowlist | null;
  allowlistError: string | null;
  /** `base_config_json.effective`, parsed once by `Console`. */
  baseEffective: Record<string, unknown> | null;
  /** The cell on screen — the destination keeps the symbol and the split. */
  symbol: string;
  split: string;
  /**
   * Navigate to the answer. The NOTICE travels with it because this component
   * unmounts on the way: the destination run's detail has to load, and the
   * results region renders its loading shell in between.
   */
  onSubmitted: (target: { runId: string; scenario: string; notice: string }) => void;
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
function Outcome({ outcome, onRetry }: { outcome: SimRefusal; onRetry: () => void }) {
  const box = (tone: 'bad' | 'warn', heading: string, body: React.ReactNode) => (
    <div
      data-testid="tweak-outcome"
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
            className="mt-2 rounded border border-gray-600 px-2 py-1 text-gray-200 hover:bg-gray-700"
            onClick={onRetry}
          >
            Retry
          </button>
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
  symbol,
  split,
  onSubmitted,
}: TweakBarProps) {
  const controls = useMemo(() => buildControls(allowlist, baseEffective), [allowlist, baseEffective]);
  const editable = useMemo(() => editableControls(controls), [controls]);
  const listOnly = useMemo(() => listOnlyControls(controls), [controls]);
  const basis = useMemo(() => parseRunSpec(sweep.spec_json), [sweep.spec_json]);

  // Only the keys the operator has TOUCHED live in state; everything else falls
  // back to its prefill. So a late-arriving allowlist prefills correctly with no
  // effect to synchronise, and "reset" is one `setFields({})`.
  const [edits, setEdits] = useState<FieldMap>({});
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<SimRefusal | null>(null);

  const fields = useMemo<FieldMap>(() => {
    const merged: FieldMap = {};
    for (const control of controls) merged[control.key] = edits[control.key] ?? prefill(control);
    return merged;
  }, [controls, edits]);

  const build = useMemo(
    () => buildTweak({ controls, fields, basis }),
    [controls, fields, basis],
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

  const blocking = [...build.errors, ...(verdict?.issues.map((i) => i.message) ?? [])];
  const spec: SweepSpec | null = build.spec && blocking.length === 0 ? build.spec : null;

  const submit = async () => {
    if (!spec || busy) return;
    setBusy(true);
    setOutcome(null);
    const result = await submitSim(spec);
    setBusy(false);
    if (result.kind === 'accepted') {
      onSubmitted({
        runId: result.runId,
        scenario: build.armName!,
        notice:
          `Run ${result.runId} accepted — replaying arm “${build.armName}”` +
          `${result.cellCount === null ? '' : ` (${result.cellCount} cells)`}. ` +
          'This page polls it until it finishes.',
      });
      return;
    }
    if (result.kind === 'deduplicated') {
      onSubmitted({
        runId: result.runId,
        scenario: build.armName!,
        notice:
          `Answered from stored run ${result.runId} — an identical spec had already completed on ` +
          'this engine and commit, so NOTHING was replayed. The evidence below is that run’s.',
      });
      return;
    }
    setOutcome(result);
  };

  return (
    <section data-testid="tweak-bar" className="rounded border border-gray-700 bg-gray-850 p-3 space-y-3">
      <div>
        <h3 className="text-sm font-semibold text-white">Tweak one field and re-run</h3>
        <p data-testid="tweak-base-anchor" className="mt-1 text-xs text-gray-400 leading-snug">
          {TWEAK_BASE_ANCHOR}
        </p>
      </div>

      {allowlistError && (
        <p data-testid="tweak-allowlist-error" className="text-xs text-amber-400">
          The allowlist could not be read ({allowlistError}), so there are no controls to offer —
          the JSON editor above still submits to the batch Job.
        </p>
      )}

      {!allowlistError && editable.length === 0 && (
        <p className="text-xs text-gray-400">Loading the allowlist…</p>
      )}

      {editable.length > 0 && (
        <div data-testid="tweak-controls" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {editable.map((control) => (
            <ControlRow
              key={control.key}
              control={control}
              field={fields[control.key]}
              disabled={busy}
              onChange={(next) => setEdits((prev) => ({ ...prev, [control.key]: next }))}
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

      {blocking.length > 0 && (
        <ul data-testid="tweak-blocking" className="space-y-1 text-xs text-amber-400">
          {blocking.map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="tweak-submit"
          disabled={!spec || busy}
          onClick={submit}
          className={`rounded px-3 py-1 text-xs font-medium ${
            spec && !busy
              ? 'bg-blue-700 text-white hover:bg-blue-600'
              : 'bg-gray-700 text-gray-400 cursor-not-allowed'
          }`}
        >
          {busy ? 'Submitting…' : 'Run this tweak'}
        </button>
        <PinButton spec={spec} armName={build.armName} />
        <span className="text-[11px] text-gray-500">
          {verdict
            ? `${verdict.cellCount} cells (this arm + base) x ${
                basis?.symbols.length ?? 0
              } symbol(s) x ${verdict.splitCount} split(s); the service's cap is ${
                allowlist?.caps.max_cells ?? '—'
              } and IT validates.`
            : `Lands on ${symbol} / ${split} of the new run.`}
        </span>
      </div>

      {outcome && <Outcome outcome={outcome} onRetry={submit} />}
    </section>
  );
}
