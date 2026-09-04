// FC-096 Phase E PR-5: `/sims/compare?a=…&b=…`.
//
// The URL is the state here too (decision 4), and the two cells are its two
// query parameters — `run:scenario:symbol:split` each. A deep link reloads onto
// the same comparison (`main.py:72` serves index.html for every non-`api/`
// path), Back walks the operator's own picks, and a malformed `a` goes to
// `/sims` rather than being guessed at.
//
// `b` is EMPTY unless the URL says otherwise. The page never picks a partner:
// a comparison the operator did not ask for is one whose alignment warnings
// they have no reason to read.
//
// The route is registered BEFORE `/sims/:runId` in `App.tsx`. React Router 6
// ranks static segments above dynamic ones, so `compare` would not be eaten by
// `:runId` in any case — but the order is written down rather than relied on.

import { useEffect, useMemo } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useSweepDetail, useSweepList } from '../../hooks/useSweeps';
import { useArtifact } from '../../hooks/useArtifact';
import { useBars } from '../../hooks/useBars';
import { computeDigest } from '../../components/v2/console/artifactDigest';
import { artifactStrategy } from '../../components/v2/console/normaliseArtifact';
import { parseBaseEffective, specStrategy, BASE_SCENARIO } from '../../components/v2/console/Console';
import CompareView, { type CompareSideData } from '../../components/v2/console/CompareView';
import BiasFooter from '../../components/v2/sims/BiasFooter';
import {
  formatCellRef,
  parseCellRef,
  type CompareRef,
} from '../../components/v2/console/compareAlignment';
import { indexRows, lookupCell } from '../../components/v2/sims/resultCells';
import { cellExists, defaultCell } from './Simulations';
import type { SimBars, SweepRow } from '../../types/v2';

/**
 * One side's data, assembled from the hooks.
 *
 * Every hook is called unconditionally with a possibly-`null` argument, because
 * hooks may not be called conditionally and because "no second cell" is a
 * normal state of this page rather than an error.
 */
function useSide(ref: CompareRef | null): CompareSideData | null {
  const { data: detail, error } = useSweepDetail(ref?.runId ?? null);
  // The Decision 4 invariant (PR-2 amendments): the report on screen belongs to
  // the URL's run. `usePolledGet` clears the previous run's data inside an
  // effect, so one committed render carries the NEW ref with the OLD detail.
  const matched = detail && ref && detail.sweep.run_id === ref.runId ? detail : null;
  const sweep: SweepRow | null = matched?.sweep ?? null;
  const report = matched?.results ?? null;

  const cell = useMemo(
    () => (ref ? { scenario: ref.scenario, symbol: ref.symbol, split: ref.split } : null),
    [ref],
  );
  const baseCell = useMemo(
    () =>
      ref && ref.scenario !== BASE_SCENARIO
        ? { scenario: BASE_SCENARIO, symbol: ref.symbol, split: ref.split }
        : null,
    [ref],
  );

  const artifactState = useArtifact(sweep, cell);
  const baseState = useArtifact(sweep, baseCell);
  const barsState = useBars(sweep, ref?.symbol ?? null, ref?.split ?? null);

  const specStrategyValue = useMemo(() => (sweep ? specStrategy(sweep) : null), [sweep]);
  const baseEffective = useMemo(
    () => parseBaseEffective(sweep?.base_config_json),
    [sweep?.base_config_json],
  );

  const row = useMemo(
    () =>
      report && ref ? lookupCell(indexRows(report.rows), ref.scenario, ref.symbol, ref.split) ?? null : null,
    [report, ref],
  );

  const digest = useMemo(
    () =>
      artifactState.data
        ? computeDigest(artifactState.data, barsState.data as SimBars | null, {
            specStrategy: specStrategyValue,
          })
        : null,
    [artifactState.data, barsState.data, specStrategyValue],
  );

  if (!ref) return null;

  const digestAbsence =
    artifactState.absent ??
    artifactState.error ??
    (artifactState.sessionExpired ? 'Session expired — reload to sign in again.' : null) ??
    (artifactState.loading ? 'Loading this cell’s artifact…' : null);

  return {
    ref,
    sweep,
    report,
    row,
    artifact: artifactState.data,
    artifactAbsence: artifactState.absent ?? artifactState.error,
    baseArtifact: baseState.data,
    baseAbsence: baseState.absent ?? baseState.error,
    bars: barsState.data,
    barsAbsence: barsState.absent ?? barsState.error,
    digest,
    digestAbsence,
    artifactRunId: artifactState.runId,
    baseEffective,
    strategy: artifactStrategy(specStrategyValue, artifactState.data),
    loading: !matched && !error,
    error: matched ? null : error,
  };
}

/**
 * One side's picker: run, arm, symbol, split.
 *
 * The arm/symbol/split options come from THAT side's own report, so a run with
 * no holdout never offers a `holdout` button and a run without an arm never
 * offers it — the same rule the cell selector on `/sims` follows. While the
 * report has not loaded the pickers show the URL's values and are disabled:
 * offering options derived from another run's report is how a stale deep link
 * turns into a 404 the operator cannot explain.
 */
function SidePicker({
  tag,
  ref: cellRef,
  runs,
  side,
  onChange,
  onClear,
}: {
  tag: 'A' | 'B';
  ref: CompareRef | null;
  runs: SweepRow[];
  side: CompareSideData | null;
  onChange: (next: CompareRef) => void;
  onClear?: () => void;
}) {
  const report = side?.report ?? null;
  const runOptions = useMemo(() => {
    const ids = runs.map((r) => r.run_id);
    // A deep-linked run that is not in the recent list is still selected: the
    // list is the last N runs, not the set of runs that exist.
    if (cellRef && !ids.includes(cellRef.runId)) return [cellRef.runId, ...ids];
    return ids;
  }, [runs, cellRef]);

  const select = (
    labelText: string,
    value: string,
    options: string[],
    onPick: (v: string) => void,
    testId: string,
  ) => (
    <label className="text-xs text-gray-400">
      <span className="block text-[11px] uppercase tracking-wide text-gray-500 mb-1">
        {labelText}
      </span>
      <select
        data-testid={testId}
        className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 max-w-[16rem]"
        value={value}
        disabled={options.length === 0}
        onChange={(e) => onPick(e.target.value)}
      >
        {options.length === 0 && <option value={value}>{value || '—'}</option>}
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );

  if (!cellRef) {
    return (
      <div data-testid={`picker-${tag}`} className="space-y-2">
        <p className="text-[11px] uppercase tracking-wide text-gray-500">{tag}</p>
        <p className="text-sm text-gray-400">
          No cell chosen. Pick a run to start — or reach this page from a cell&rsquo;s
          &ldquo;Compare&rdquo; link, which fills A in for you.
        </p>
        {runs.length > 0 && (
          <button
            type="button"
            data-testid={`picker-${tag}-start`}
            className="px-2 py-1 rounded text-xs border bg-gray-800 border-gray-700 text-gray-300 hover:text-white"
            // A first guess, not a claim: the run's own row names its symbols,
            // and the repair effect REPLACES this with that run's default cell
            // the moment its report loads. Never an empty segment — a ref with
            // one is unparseable, and the page would drop the pick it just made.
            onClick={() =>
              onChange({
                runId: runs[0].run_id,
                scenario: BASE_SCENARIO,
                symbol: runs[0].symbols?.[0] ?? 'pending',
                split: 'all',
              })
            }
          >
            Choose run {runs[0].run_id}
          </button>
        )}
      </div>
    );
  }

  return (
    <div data-testid={`picker-${tag}`} className="space-y-2">
      <div className="flex items-baseline gap-2">
        <p className="text-[11px] uppercase tracking-wide text-gray-500">{tag}</p>
        {onClear && (
          <button
            type="button"
            data-testid={`picker-${tag}-clear`}
            className="text-[11px] underline text-gray-500 hover:text-gray-300"
            onClick={onClear}
          >
            clear
          </button>
        )}
      </div>
      <div className="flex flex-wrap gap-3">
        {select(
          'Run',
          cellRef.runId,
          runOptions,
          (runId) => onChange({ ...cellRef, runId }),
          `picker-${tag}-run`,
        )}
        {select(
          'Arm',
          cellRef.scenario,
          report?.scenarios ?? [],
          (scenario) => onChange({ ...cellRef, scenario }),
          `picker-${tag}-arm`,
        )}
        {select(
          'Symbol',
          cellRef.symbol,
          report?.symbols ?? [],
          (symbol) => onChange({ ...cellRef, symbol }),
          `picker-${tag}-symbol`,
        )}
        {select(
          'Split',
          cellRef.split,
          report?.windows.map((w) => w.split) ?? [],
          (split) => onChange({ ...cellRef, split }),
          `picker-${tag}-split`,
        )}
      </div>
      {!report && (
        <p className="text-xs text-gray-500" data-testid={`picker-${tag}-loading`}>
          {side?.error
            ? `This run could not be read: ${side.error}. Its state is unknown, not empty.`
            : 'Loading this run — the arm, symbol and split options come from its own report.'}
        </p>
      )}
    </div>
  );
}

export default function SimsCompare() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const rawA = params.get('a');
  const rawB = params.get('b');

  // Parsed once per query string, so the refs are stable objects and the side
  // hooks below do not refetch on every render.
  const refA = useMemo(() => parseCellRef(rawA), [rawA]);
  const refB = useMemo(() => parseCellRef(rawB), [rawB]);

  const { data: runs } = useSweepList();
  const sideA = useSide(refA);
  const sideB = useSide(refB);

  // A malformed `a` is not repairable — which of the four fields was dropped is
  // not knowable — so the page hands the operator back to `/sims` rather than
  // rendering half a comparison. REPLACING: a URL that cannot be read is not a
  // screen worth keeping in their history.
  useEffect(() => {
    if (rawA !== null && refA === null) navigate('/sims', { replace: true });
  }, [rawA, refA, navigate]);

  // A malformed `b` drops to "no second cell" instead of taking the whole page
  // down: `a` is still readable and worth showing.
  useEffect(() => {
    if (rawB !== null && refB === null) {
      const next = new URLSearchParams(params);
      next.delete('b');
      setParams(next, { replace: true });
    }
  }, [rawB, refB, params, setParams]);

  /**
   * Repair a cell this run does not have — a deep link from another sweep, or
   * the arm/symbol/split carried across a RUN change in the picker.
   *
   * Automatic, so it REPLACES (decision 4). Gated on the report belonging to
   * the URL's run, which `useSide` has already enforced.
   */
  const repair = (key: 'a' | 'b', ref: CompareRef | null, side: CompareSideData | null) => {
    if (!ref || !side?.report) return;
    if (cellExists(side.report, ref)) return;
    const fallback = defaultCell(side.report);
    if (!fallback) return;
    const next = new URLSearchParams(params);
    next.set(key, formatCellRef({ runId: ref.runId, ...fallback }));
    setParams(next, { replace: true });
  };
  useEffect(() => {
    repair('a', refA, sideA);
    // The repair reads `params` and writes it; depending on the side objects
    // (new on every render) would loop. The refs and the reports are what
    // actually decide whether a repair is needed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refA, sideA?.report]);
  useEffect(() => {
    repair('b', refB, sideB);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refB, sideB?.report]);

  /** A user's pick. PUSHES, so Back walks their own comparisons. */
  const setSide = (key: 'a' | 'b', ref: CompareRef) => {
    const next = new URLSearchParams(params);
    next.set(key, formatCellRef(ref));
    setParams(next);
  };
  const clearSide = (key: 'a' | 'b') => {
    const next = new URLSearchParams(params);
    next.delete(key);
    setParams(next);
  };

  const swap = () => {
    if (!refA || !refB) return;
    const next = new URLSearchParams(params);
    next.set('a', formatCellRef(refB));
    next.set('b', formatCellRef(refA));
    setParams(next);
  };

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-white">Compare two cells</h1>
        <p className="text-gray-400 mt-1 text-sm">
          Two replays side by side, each anchored to <strong>its own run&rsquo;s base</strong>. The
          alignment matrix below decides what may be compared; where it withholds the numbers, the
          curves are still drawn and the numbers are not. These are hypotheses about a config, never
          a record of what the bot did.
        </p>
        <p className="text-xs text-gray-500 mt-2">
          <Link className="underline" to="/sims">
            ← back to Simulations
          </Link>
        </p>
      </header>

      <section className="rounded-lg border border-gray-700 bg-gray-800 p-5 space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <h2 className="text-base font-semibold text-white">Cells</h2>
          {refA && refB && (
            <button
              type="button"
              data-testid="compare-swap"
              className="px-2 py-1 rounded text-xs border bg-gray-800 border-gray-700 text-gray-300 hover:text-white"
              onClick={swap}
            >
              Swap A and B
            </button>
          )}
        </div>
        <div className="grid gap-6 lg:grid-cols-2">
          <SidePicker
            tag="A"
            ref={refA}
            runs={runs ?? []}
            side={sideA}
            onChange={(next) => setSide('a', next)}
          />
          <SidePicker
            tag="B"
            ref={refB}
            runs={runs ?? []}
            side={sideB}
            onChange={(next) => setSide('b', next)}
            onClear={() => clearSide('b')}
          />
        </div>
      </section>

      {sideA ? (
        <CompareView a={sideA} b={sideB} />
      ) : (
        <p data-testid="compare-no-a" className="text-sm text-gray-400">
          No first cell in the URL. Open a cell on{' '}
          <Link className="underline" to="/sims">
            Simulations
          </Link>{' '}
          and use its Compare link, or pick a run above.
        </p>
      )}

      {/* The bias footer is LAST, below everything it qualifies — the PR-2
          composition rule. ONE footer, from A's run: the caveats are the
          engine's and both runs carry the same set. When B's run carries a
          different set of bias titles that is itself a finding, so it is named
          rather than merged away. */}
      {sideA?.report && <BiasFooter report={sideA.report} />}
      {sideA?.report && sideB?.report && (
        <p data-testid="bias-footer-source" className="text-xs text-gray-500">
          {sameBiasTitles(sideA, sideB)
            ? `The caveats above are run ${sideA.ref.runId}'s, and run ${sideB.ref.runId} records the same set.`
            : `⚠ The caveats above are run ${sideA.ref.runId}'s. Run ${sideB.ref.runId} records a DIFFERENT set of known biases — open B's own cell to read them; they are not merged here.`}
        </p>
      )}
    </div>
  );
}

/** Do the two runs record the same known-bias titles? A difference is a finding. */
function sameBiasTitles(a: CompareSideData, b: CompareSideData): boolean {
  const ta = (a.report?.known_biases ?? []).map((x) => x.title).join('|');
  const tb = (b.report?.known_biases ?? []).map((x) => x.title).join('|');
  return ta === tb;
}
