// FC-060 Layer 4 (PR-B), region 1: the submit form.
//
// Holdout is ON by default and turning it off shows the warning INLINE, before
// the run rather than only in the report footer. That default is the whole
// argument of Layer 2: a ranking chosen on the window it was measured on is a
// hypothesis, not a result, and the operator should have to decide to give that
// up rather than fall into it.
//
// Every server refusal is shown verbatim. The 422 quotes the same reason string
// `src/backtesting/scenarios/overrides.py` gives the runner, and the 502 carries
// the exact grant command — rewording either would drop the actionable half.

import { useMemo, useState } from 'react';
import type {
  SweepAllowlist,
  SweepPreset,
  SweepScenarioSpec,
} from '../../../types/v2';
import { cls } from '../../../utils/format';
import { SESSION_EXPIRED_MESSAGE, submitSweep, type SubmitOutcome } from '../../../hooks/useSweeps';
import {
  DEFAULT_HOLDOUT_DAYS,
  DEFAULT_WINDOW_DAYS,
  FALLBACK_CAPS,
  addDays,
  buildSpec,
  defaultWindowEnd,
  parseScenariosJson,
  serialiseScenarios,
  validateSpec,
  type SpecIssue,
} from './specValidation';

interface Props {
  allowlist: SweepAllowlist | null;
  allowlistError: string | null;
  universe: string[];
  /** Called with the accepted run_id so the page can select it immediately. */
  onSubmitted: (runId: string) => void;
  /** Open another run (the `prior_done_run_id` hint) without submitting. */
  onSelectRun: (runId: string) => void;
}

const issuesFor = (issues: SpecIssue[], field: SpecIssue['field']) =>
  issues.filter((i) => i.field === field);

function IssueList({ issues }: { issues: SpecIssue[] }) {
  if (issues.length === 0) return null;
  return (
    <ul className="mt-1 space-y-0.5">
      {issues.map((issue, i) => (
        <li key={i} className="text-xs text-red-300">{issue.message}</li>
      ))}
    </ul>
  );
}

export default function SubmitSweep({
  allowlist,
  allowlistError,
  universe,
  onSubmitted,
  onSelectRun,
}: Props) {
  const end0 = useMemo(() => defaultWindowEnd(), []);

  const [symbols, setSymbols] = useState<string[]>([]);
  const [freeText, setFreeText] = useState('');
  const [start, setStart] = useState(() => addDays(end0, -DEFAULT_WINDOW_DAYS));
  const [end, setEnd] = useState(end0);
  const [holdoutEnabled, setHoldoutEnabled] = useState(true);
  const [holdoutStart, setHoldoutStart] = useState(() => addDays(end0, -DEFAULT_HOLDOUT_DAYS));
  const [startingCash, setStartingCash] = useState('');
  const [runSensitivity, setRunSensitivity] = useState(false);
  const [fillHaircut, setFillHaircut] = useState('');
  const [scenariosText, setScenariosText] = useState('[]');
  const [submitting, setSubmitting] = useState(false);
  const [outcome, setOutcome] = useState<SubmitOutcome | null>(null);

  const caps = allowlist?.caps ?? FALLBACK_CAPS;

  const parsed = useMemo(() => parseScenariosJson(scenariosText), [scenariosText]);

  // Free-text symbols merge with the universe checkboxes; both are normalised
  // and deduped so a symbol typed AND ticked counts once.
  const freeSymbols = useMemo(
    () =>
      freeText
        .split(/[,\s]+/)
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
    [freeText],
  );
  const allSymbols = useMemo(
    () => Array.from(new Set([...symbols, ...freeSymbols])),
    [symbols, freeSymbols],
  );

  // The haircut is a scenario FIELD, not a config key (config_hash hashes the
  // module default, so two arms differing only in haircut would share a hash).
  // Applying it here stamps it onto every declared arm.
  const haircutValue = fillHaircut.trim() === '' ? undefined : Number(fillHaircut);
  const scenarios: SweepScenarioSpec[] = useMemo(() => {
    const base = parsed.scenarios ?? [];
    if (haircutValue === undefined) return base;
    return base.map((s) => ({ ...s, fill_haircut: s.fill_haircut ?? haircutValue }));
  }, [parsed.scenarios, haircutValue]);

  const cashValue = startingCash.trim() === '' ? undefined : Number(startingCash);

  const validation = useMemo(
    () =>
      validateSpec({
        spec: {
          symbols: allSymbols,
          start,
          end,
          ...(holdoutEnabled ? { holdout_start: holdoutStart } : {}),
          ...(cashValue === undefined ? {} : { starting_cash: cashValue }),
          scenarios,
        },
        holdoutEnabled,
        allowlist,
        scenarioParseError: parsed.error,
      }),
    [allSymbols, start, end, holdoutEnabled, holdoutStart, cashValue, scenarios, allowlist, parsed.error],
  );

  // A haircut typed outside [0, 1] is a form error even with no arms declared.
  const haircutBad =
    haircutValue !== undefined && (!Number.isFinite(haircutValue) || haircutValue < 0 || haircutValue > 1);

  const canSubmit = validation.valid && !haircutBad && !submitting;

  const applyPreset = (preset: SweepPreset) => {
    const existing = parsed.scenarios ?? [];
    if (existing.some((s) => s.name === preset.name)) return;
    // A preset may carry a fill_haircut instead of an override (the "at the bid"
    // arm has no config key at all — the haircut IS the variation).
    const arm: SweepScenarioSpec = {
      name: preset.name,
      overrides: preset.overrides ?? {},
      ...(preset.fill_haircut === undefined ? {} : { fill_haircut: preset.fill_haircut }),
    };
    setScenariosText(serialiseScenarios([...existing, arm]));
  };

  const onSubmit = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setOutcome(null);
    const spec = buildSpec({
      symbols: allSymbols,
      start,
      end,
      holdoutEnabled,
      holdoutStart,
      startingCash: cashValue,
      runSensitivity,
      scenarios,
    });
    // ONE request. See `submitSweep`: a retried submit can duplicate a Job.
    const result = await submitSweep(spec);
    setSubmitting(false);
    setOutcome(result);
    if (result.kind === 'accepted') {
      onSubmitted(result.body.run_id);
    }
  };

  return (
    <form
      onSubmit={onSubmit}
      className="rounded-lg border border-gray-700 bg-gray-800 p-4 space-y-4"
    >
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <h2 className="text-base font-semibold text-white">New sweep</h2>
        <span className="text-xs text-gray-500">
          {validation.cellCount} cells (cap {caps.max_cells}) · one sweep runs at a time
        </span>
      </div>

      {allowlistError && (
        <p className="text-xs text-yellow-400">
          ⚠ The allowlist could not be read ({allowlistError}). Override keys are not checked here;
          the API still refuses an illegal key with its own reason.
        </p>
      )}

      {/* --- symbols --- */}
      <div>
        <label className="block text-sm font-medium text-gray-300">Symbols</label>
        {universe.length > 0 ? (
          <div className="flex flex-wrap gap-2 mt-2">
            {universe.map((sym) => {
              const on = symbols.includes(sym);
              return (
                <button
                  key={sym}
                  type="button"
                  aria-pressed={on}
                  onClick={() =>
                    setSymbols((prev) => (on ? prev.filter((s) => s !== sym) : [...prev, sym]))
                  }
                  className={cls(
                    'px-2.5 py-1 rounded text-xs font-mono border transition-colors',
                    on
                      ? 'bg-blue-600 border-blue-500 text-white'
                      : 'bg-gray-900 border-gray-600 text-gray-300 hover:bg-gray-700',
                  )}
                >
                  {sym}
                </button>
              );
            })}
          </div>
        ) : (
          <p className="text-xs text-gray-500 mt-1">
            The live universe could not be read — type symbols below instead.
          </p>
        )}
        <input
          type="text"
          value={freeText}
          onChange={(e) => setFreeText(e.target.value)}
          placeholder="or type: SPY, QQQ, AMD"
          aria-label="Additional symbols"
          className="mt-2 w-full bg-gray-900 border border-gray-600 rounded px-3 py-1.5 text-sm text-gray-200 font-mono"
        />
        <p className="text-xs text-gray-500 mt-1">
          {allSymbols.length}/{caps.max_symbols} selected
          {allSymbols.length > 0 && `: ${allSymbols.join(', ')}`}
        </p>
        <IssueList issues={issuesFor(validation.issues, 'symbols')} />
      </div>

      {/* --- window --- */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className="block text-sm font-medium text-gray-300" htmlFor="sweep-start">Start</label>
          <input
            id="sweep-start"
            type="date"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            className="mt-1 w-full bg-gray-900 border border-gray-600 rounded px-3 py-1.5 text-sm text-gray-200"
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-300" htmlFor="sweep-end">End</label>
          <input
            id="sweep-end"
            type="date"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            className="mt-1 w-full bg-gray-900 border border-gray-600 rounded px-3 py-1.5 text-sm text-gray-200"
          />
        </div>
      </div>
      <IssueList issues={issuesFor(validation.issues, 'window')} />

      {/* --- holdout --- */}
      <div>
        <label className="flex items-center gap-2 text-sm text-gray-300">
          <input
            type="checkbox"
            checked={holdoutEnabled}
            onChange={(e) => setHoldoutEnabled(e.target.checked)}
            className="rounded border-gray-600 bg-gray-900"
          />
          Out-of-sample holdout
        </label>
        {holdoutEnabled ? (
          <div className="mt-2">
            <label className="block text-xs text-gray-400" htmlFor="sweep-holdout">Holdout starts</label>
            <input
              id="sweep-holdout"
              type="date"
              value={holdoutStart}
              onChange={(e) => setHoldoutStart(e.target.value)}
              className="mt-1 bg-gray-900 border border-gray-600 rounded px-3 py-1.5 text-sm text-gray-200"
            />
            <p className="text-xs text-gray-500 mt-1">
              Two independent replays: the fit window ends the day BEFORE this date and carries no
              position across the boundary.
            </p>
          </div>
        ) : (
          <p
            data-testid="in-sample-warning"
            className="mt-2 text-xs text-yellow-400 border border-yellow-800/60 bg-yellow-950/30 rounded p-2"
          >
            ⚠ IN-SAMPLE ONLY. Every arm would be measured on the window it is chosen from, over a
            single volatility regime. The best-looking arm is more often the luckiest one than the
            best one — a ranking that does not survive out of sample has been refuted, not merely
            unconfirmed.
          </p>
        )}
        <IssueList issues={issuesFor(validation.issues, 'holdout')} />
      </div>

      {/* --- scenarios --- */}
      <div>
        <div className="flex items-baseline justify-between flex-wrap gap-2">
          <label className="block text-sm font-medium text-gray-300" htmlFor="sweep-scenarios">
            Arms
          </label>
          <span className="text-xs text-gray-500">
            `base` runs implicitly as the comparator — do not declare it
          </span>
        </div>
        {(allowlist?.presets?.length ?? 0) > 0 && (
          <div className="flex flex-wrap gap-2 mt-2">
            {allowlist!.presets.map((preset) => (
              <button
                key={preset.name}
                type="button"
                onClick={() => applyPreset(preset)}
                title={`${preset.name} — ${JSON.stringify(preset.overrides ?? {})}${
                  preset.fill_haircut === undefined ? '' : ` fill_haircut=${preset.fill_haircut}`
                }`}
                className="px-2.5 py-1 rounded text-xs font-mono bg-gray-900 border border-gray-600 text-gray-300 hover:bg-gray-700"
              >
                + {preset.label ?? preset.name}
              </button>
            ))}
          </div>
        )}
        <textarea
          id="sweep-scenarios"
          value={scenariosText}
          onChange={(e) => setScenariosText(e.target.value)}
          rows={8}
          spellCheck={false}
          className="mt-2 w-full bg-gray-900 border border-gray-600 rounded px-3 py-2 text-xs text-gray-200 font-mono"
        />
        <IssueList issues={issuesFor(validation.issues, 'scenarios')} />
        {allowlist && (
          <details className="mt-2">
            <summary className="text-xs text-gray-500 cursor-pointer">
              {allowlist.allowed.length} allowed override keys ({allowlist.rejected.length} refused, with reasons)
            </summary>
            <ul className="mt-2 space-y-1">
              {allowlist.allowed.map((a) => (
                <li key={a.key} className="text-xs text-gray-400">
                  <span className="font-mono text-blue-300">{a.key}</span> — {a.description}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>

      {/* --- fill + sensitivity + cash --- */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div>
          <label className="block text-sm font-medium text-gray-300" htmlFor="sweep-haircut">
            Fill haircut
          </label>
          <input
            id="sweep-haircut"
            type="number"
            step="0.05"
            min="0"
            max="1"
            value={fillHaircut}
            onChange={(e) => setFillHaircut(e.target.value)}
            placeholder="engine default"
            className="mt-1 w-full bg-gray-900 border border-gray-600 rounded px-3 py-1.5 text-sm text-gray-200"
          />
          <p className="text-xs text-gray-500 mt-1">
            0 = mid, 1 = at the bid. Applied to every declared arm that does not set its own.
          </p>
          {haircutBad && (
            <p className="text-xs text-red-300 mt-1">Fill haircut must be between 0 and 1.</p>
          )}
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-300" htmlFor="sweep-cash">
            Starting cash
          </label>
          <input
            id="sweep-cash"
            type="number"
            value={startingCash}
            onChange={(e) => setStartingCash(e.target.value)}
            placeholder="engine default"
            className="mt-1 w-full bg-gray-900 border border-gray-600 rounded px-3 py-1.5 text-sm text-gray-200"
          />
          <IssueList issues={issuesFor(validation.issues, 'cash')} />
        </div>
        <div>
          <span className="block text-sm font-medium text-gray-300">Sensitivity</span>
          <label className="flex items-center gap-2 text-sm text-gray-300 mt-2">
            <input
              type="checkbox"
              checked={runSensitivity}
              onChange={(e) => setRunSensitivity(e.target.checked)}
              className="rounded border-gray-600 bg-gray-900"
            />
            run_sensitivity
          </label>
          <p className="text-xs text-gray-500 mt-1">Roughly doubles the wall time.</p>
        </div>
      </div>

      {/* No credential field. FC-096 Phase D retired `SWEEP_SUBMIT_TOKEN`: the
          page is behind IAP, the session cookie is the credential, and the API
          authorises the write against its `OPERATORS` allowlist. A viewer's
          submit comes back 403 with the server's own message; a signed-out one
          comes back 401 and is rendered as the session-expired state below. */}

      <IssueList issues={issuesFor(validation.issues, 'caps')} />

      <div className="flex items-center gap-3 flex-wrap">
        <button
          type="submit"
          disabled={!canSubmit}
          data-testid="submit-sweep"
          className={cls(
            'px-4 py-2 rounded text-sm font-medium transition-colors',
            canSubmit
              ? 'bg-blue-600 hover:bg-blue-500 text-white'
              : 'bg-gray-700 text-gray-500 cursor-not-allowed',
          )}
        >
          {submitting ? 'Submitting…' : 'Run sweep'}
        </button>
        <span className="text-xs text-gray-500">
          {validation.cellCount} cells · {validation.splitCount} split
          {validation.splitCount === 1 ? '' : 's'}
        </span>
      </div>

      {/* --- the server's answer, verbatim --- */}
      {outcome && <Outcome outcome={outcome} onSelectRun={onSelectRun} />}
    </form>
  );
}

function Outcome({
  outcome,
  onSelectRun,
}: {
  outcome: SubmitOutcome;
  onSelectRun: (runId: string) => void;
}) {
  // The IAP session is gone. Rendered on its own, with the one action that
  // helps, because every other refusal on this form is about the SPEC and this
  // one is not: re-reading it, fixing it or resubmitting it all fail the same
  // way until the page is reloaded and the operator signs in again.
  if (outcome.kind === 'session_expired') {
    return (
      <div
        data-testid="submit-outcome"
        className="rounded border border-yellow-700/70 bg-yellow-950/30 p-3 space-y-2"
      >
        <p data-testid="session-expired" className="text-sm font-medium text-yellow-300">
          {SESSION_EXPIRED_MESSAGE}
        </p>
        <p className="text-xs text-yellow-200/80">
          Nothing was submitted. Your Identity-Aware Proxy session has ended — reloading takes you
          through Google sign-in and back to this page.
        </p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="px-3 py-1.5 rounded text-xs font-medium bg-yellow-700 hover:bg-yellow-600 text-white"
        >
          Reload
        </button>
      </div>
    );
  }

  if (outcome.kind === 'accepted') {
    const prior = outcome.body.prior_done_run_id;
    return (
      <div
        data-testid="submit-outcome"
        className="rounded border border-green-800/70 bg-green-950/40 p-3 text-sm text-green-300 space-y-1"
      >
        <p>
          Submitted as <span className="font-mono">{outcome.body.run_id}</span>. Container start is
          3-4 minutes before the first <span className="font-mono">running</span> row appears.
        </p>
        {/* A HINT, not a verdict, and deliberately uncoloured: the launch DID
            happen, and only the Job — which alone can see its effective config —
            decides whether to deduplicate. Nothing auto-redirects; the run just
            submitted is the one selected. */}
        {prior && (
          <p data-testid="prior-done-hint" className="text-gray-400 text-xs">
            An identical sweep already completed as run{' '}
            <button
              type="button"
              data-testid="prior-done-link"
              onClick={() => onSelectRun(prior)}
              className="font-mono underline hover:text-gray-200"
            >
              {prior}
            </button>{' '}
            (click to open). This submission was launched anyway; the Job may mark it
            deduplicated.
          </p>
        )}
      </div>
    );
  }

  const label: Record<
    Exclude<SubmitOutcome['kind'], 'accepted' | 'session_expired'>,
    string
  > = {
    unauthorized: 'Refused — your account is not an operator',
    conflict: 'Refused — a sweep is already in flight',
    invalid: 'Refused — the spec is not legal',
    launch_failed: 'The Job could not be launched',
    disabled: 'Submits are unavailable',
    error: 'The submit failed',
  };

  return (
    <div
      data-testid="submit-outcome"
      className="rounded border border-red-800/70 bg-red-950/40 p-3 space-y-1"
    >
      <p className="text-sm font-medium text-red-300">{label[outcome.kind]}</p>
      {/* Verbatim. The 422 carries the runner's own rejection reason and the 502
          carries the gcloud grant command; rewording drops the useful half. */}
      <pre className="text-xs text-red-200/90 whitespace-pre-wrap break-words font-mono">
        {outcome.detail}
      </pre>
      {outcome.kind === 'error' && (
        <p className="text-xs text-red-200/80">
          Nothing was retried. Check the runs list before submitting again — a submit that failed to
          answer may still have launched.
        </p>
      )}
    </div>
  );
}
