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
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { useSweepDetail, useSweepList } from '../../hooks/useSweeps';
import { useArtifact } from '../../hooks/useArtifact';
import { useBars } from '../../hooks/useBars';
import { computeDigest } from '../../components/v2/console/artifactDigest';
import { artifactStrategy } from '../../components/v2/console/normaliseArtifact';
import { parseBaseEffective, specStrategy, BASE_SCENARIO } from '../../components/v2/console/Console';
import CompareView, {
  sideStatusNote,
  type CompareSideData,
} from '../../components/v2/console/CompareView';
import BiasFooter from '../../components/v2/sims/BiasFooter';
import {
  formatCellRef,
  parseCellRef,
  sameRef,
  type CompareRef,
} from '../../components/v2/console/compareAlignment';
import { indexRows, lookupCell } from '../../components/v2/sims/resultCells';
import { cellExists, defaultCell } from './Simulations';
import type { SimBars, SweepDetail, SweepRow } from '../../types/v2';

/**
 * One side's data, assembled from the hooks.
 *
 * The run DETAIL is handed in rather than fetched here (review round 1, R7).
 * Two sides of one run used to mount two `useSweepDetail`s on the same URL,
 * which is two polls of one row for as long as the page is open; the page now
 * fetches the SET of distinct runs and passes each side its own.
 *
 * Every remaining hook is called unconditionally with a possibly-`null`
 * argument, because hooks may not be called conditionally and because "no
 * second cell" is a normal state of this page rather than an error.
 */
function useSide(
  ref: CompareRef | null,
  detail: SweepDetail | null,
  error: string | null,
  dedupFrom: string | null,
): CompareSideData | null {
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
    barsLoading: barsState.loading,
    digest,
    digestAbsence,
    artifactRunId: artifactState.runId,
    baseEffective,
    strategy: artifactStrategy(specStrategyValue, artifactState.data),
    loading: !matched && !error,
    error: matched ? null : error,
    status: sweep?.status ?? null,
    dedupFrom,
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
  // NOT `ref`: React reserves that name and strips it from a function
  // component's props, so a picker prop called `ref` arrives `undefined` and
  // every side renders as "no cell chosen".
  cellRef,
  runs,
  side,
  onChange,
  onClear,
}: {
  tag: 'A' | 'B';
  cellRef: CompareRef | null;
  runs: SweepRow[];
  side: CompareSideData | null;
  onChange: (next: CompareRef) => void;
  onClear?: () => void;
}) {
  const report = side?.report ?? null;
  // Each option is LABELLED with its status (review round 1, R1), so an
  // operator never picks a `failed` or `running` run and then reads its empty
  // side as "nothing to compare". `done` is left unlabelled — it is the norm.
  const runOptions = useMemo(() => {
    const opts = runs.map((r) => ({
      value: r.run_id,
      label: r.status === 'done' ? r.run_id : `${r.run_id} — ${r.status}`,
    }));
    // A deep-linked run that is not in the recent list is still selected: the
    // list is the last N runs, not the set of runs that exist.
    if (cellRef && !opts.some((o) => o.value === cellRef.runId)) {
      return [{ value: cellRef.runId, label: cellRef.runId }, ...opts];
    }
    return opts;
  }, [runs, cellRef]);

  const select = (
    labelText: string,
    value: string,
    options: Array<{ value: string; label: string }>,
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
          <option key={o.value} value={o.value}>
            {o.label}
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
          (report?.scenarios ?? []).map((v) => ({ value: v, label: v })),
          (scenario) => onChange({ ...cellRef, scenario }),
          `picker-${tag}-arm`,
        )}
        {select(
          'Symbol',
          cellRef.symbol,
          (report?.symbols ?? []).map((v) => ({ value: v, label: v })),
          (symbol) => onChange({ ...cellRef, symbol }),
          `picker-${tag}-symbol`,
        )}
        {select(
          'Split',
          cellRef.split,
          (report?.windows ?? []).map((w) => ({ value: w.split, label: w.split })),
          (split) => onChange({ ...cellRef, split }),
          `picker-${tag}-split`,
        )}
      </div>
      {!report && (
        <p className="text-xs text-gray-500" data-testid={`picker-${tag}-loading`}>
          {sideStatusNote(side)}
        </p>
      )}
      {side?.dedupFrom && (
        <p className="text-xs text-gray-400" data-testid={`picker-${tag}-dedup`}>
          Run <span className="font-mono">{side.dedupFrom}</span> was deduplicated — nothing was
          replayed under its own id — so this side was opened on run{' '}
          <span className="font-mono">{side.ref.runId}</span>, which holds the answer.
        </p>
      )}
    </div>
  );
}

export default function SimsCompare() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const rawA = params.get('a');
  const rawB = params.get('b');

  // Parsed once per query string, so the refs are stable objects and the side
  // hooks below do not refetch on every render.
  const refA = useMemo(() => parseCellRef(rawA), [rawA]);
  const refB = useMemo(() => parseCellRef(rawB), [rawB]);

  const { data: runs } = useSweepList();

  // R7: ONE detail fetch per DISTINCT run. Two sides of one run share a poll —
  // `useSweepDetail(null)` is inert, so the second hook simply does not fetch.
  const sameRun = !!refA && !!refB && refA.runId === refB.runId;
  const detailA = useSweepDetail(refA?.runId ?? null);
  const detailB = useSweepDetail(sameRun ? null : (refB?.runId ?? null));
  const forB = sameRun ? detailA : detailB;

  // Which run each slot was auto-followed FROM, off the history entry's own
  // state (the PR-2 dedup pattern). Absent on a typed or bookmarked URL, which
  // is exactly the notice's scope.
  const followed = (location.state ?? null) as { dedupA?: string; dedupB?: string } | null;

  const sideA = useSide(refA, detailA.data, detailA.error, followed?.dedupA ?? null);
  const sideB = useSide(refB, forB.data, forB.error, followed?.dedupB ?? null);

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
      // FUNCTIONAL updater (review round 1, LOW): two automatic corrections can
      // land in one commit, and each reading `params` from the closure would
      // write a copy of the OTHER's stale value back — the second silently
      // undoing the first.
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete('b');
          return next;
        },
        { replace: true },
      );
    }
  }, [rawB, refB, setParams]);

  /**
   * Follow a `deduplicated` run to the run that holds the evidence (R1).
   *
   * The PR-2 amendment, applied to both slots: a deduplicated run stored
   * NOTHING under its own id — that is what dedup means — so comparing against
   * it would compare against an empty side for ever. The same cell under
   * `deduplicated_to` REPLACES the slot, and the run it was reached from
   * travels in the history entry's state so the destination can say so.
   *
   * A row pointing at ITSELF is corrupt rather than resolvable and is not
   * followed; `ResultsRegion` on `/sims` says so in its own words, and here the
   * side simply renders its status.
   */
  const followDedup = (key: 'a' | 'b', ref: CompareRef | null, side: CompareSideData | null) => {
    if (!ref || !side?.sweep) return;
    if (side.sweep.status !== 'deduplicated') return;
    const target = side.sweep.deduplicated_to ?? null;
    if (!target || target === ref.runId) return;
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set(key, formatCellRef({ ...ref, runId: target }));
        return next;
      },
      {
        replace: true,
        state: { ...(followed ?? {}), [key === 'a' ? 'dedupA' : 'dedupB']: ref.runId },
      },
    );
  };

  /**
   * Repair a cell this run does not have — a deep link from another sweep, or
   * the arm/symbol/split carried across a RUN change in the picker.
   *
   * Automatic, so it REPLACES (decision 4). Gated on the report belonging to
   * the URL's run, which `useSide` has already enforced — and on the run being
   * `done`, because a `running` run has no report to repair against and a
   * `failed` one never will.
   */
  const repair = (key: 'a' | 'b', ref: CompareRef | null, side: CompareSideData | null) => {
    if (!ref || !side?.report) return;
    if (cellExists(side.report, ref)) return;
    const fallback = defaultCell(side.report);
    if (!fallback) return;
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set(key, formatCellRef({ runId: ref.runId, ...fallback }));
        return next;
      },
      { replace: true },
    );
  };

  const statusA = sideA?.sweep?.status ?? null;
  const statusB = sideB?.sweep?.status ?? null;
  useEffect(() => {
    followDedup('a', refA, sideA);
    repair('a', refA, sideA);
    // The two corrections read the ref and the loaded run and nothing else;
    // depending on the side OBJECT (new on every render) would loop. `statusA`
    // is in the list so the dedup follow fires the moment the row resolves.
  }, [refA, sideA?.report, statusA]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    followDedup('b', refB, sideB);
    repair('b', refB, sideB);
  }, [refB, sideB?.report, statusB]); // eslint-disable-line react-hooks/exhaustive-deps

  /** A user's pick. PUSHES, so Back walks their own comparisons. */
  const setSide = (key: 'a' | 'b', ref: CompareRef) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set(key, formatCellRef(ref));
      return next;
    });
  };
  const clearSide = (key: 'a' | 'b') => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete(key);
      return next;
    });
  };

  const swap = () => {
    // A no-op when the two slots hold the same cell (review round 1, LOW):
    // swapping a pair with itself pushes a history entry that changes nothing
    // and that Back then has to walk back through.
    if (!refA || !refB || sameRef(refA, refB)) return;
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('a', formatCellRef(refB));
      next.set('b', formatCellRef(refA));
      return next;
    });
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
            cellRef={refA}
            runs={runs ?? []}
            side={sideA}
            onChange={(next) => setSide('a', next)}
          />
          <SidePicker
            tag="B"
            cellRef={refB}
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

/**
 * Do the two runs record the same known biases? A difference is a finding.
 *
 * TITLE AND BODY (review round 1, LOW). Comparing titles alone would call two
 * runs identical when one of them reworded what a caveat actually says, which
 * is the case where showing only A's footer does the most damage.
 */
function sameBiasTitles(a: CompareSideData, b: CompareSideData): boolean {
  const flatten = (side: CompareSideData) =>
    (side.report?.known_biases ?? []).map((x) => `${x.title}\u0000${x.detail}`).join('\u0001');
  return flatten(a) === flatten(b);
}
