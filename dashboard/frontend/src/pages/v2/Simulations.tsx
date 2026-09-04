// FC-060 Layer 4 (PR-B): the "Simulations" page — /sims.
//
// Three regions, in the order an operator uses them: submit a sweep, watch it,
// read it. Depends on PR-A's four endpoints (`POST /api/v2/sweeps`,
// `GET /api/v2/sweeps`, `GET /api/v2/sweeps/{run_id}`,
// `GET /api/v2/sweeps/allowlist`).
//
// What this page is NOT: it is not the live book. A sweep report is a
// hypothesis about a config, replayed over historical chains by an engine with
// documented biases — never a record of what the bot did. That distinction is
// the reason sweeps get their own store (plan D1) instead of `backtest_runs`,
// and it is said in the header rather than left implicit.
//
// FC-096 Phase E PR-2 (decision 4): **the URL is the state.** The selected run
// and cell live in the path, not in `useState`, so a deep link, a reload and
// the Back button all land on the same screen. The push/replace split is the
// part worth stating: EVERY user selection pushes, so Back walks the operator's
// own history; only the AUTOMATIC choices — newest run on arrival, default cell
// when a run resolves — replace, so Back never has to fight an auto-select to
// leave the page.

import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { useApi } from '../../hooks/useApi';
import type { LiveStrategyConfig, SweepAllowlist, SweepDetail, SweepReport } from '../../types/v2';
import { useSweepAllowlist, useSweepDetail, useSweepList } from '../../hooks/useSweeps';
import SubmitSweep from '../../components/v2/sims/SubmitSweep';
import RunsList from '../../components/v2/sims/RunsList';
import SweepResults from '../../components/v2/sims/SweepResults';
import BiasFooter from '../../components/v2/sims/BiasFooter';
import Console from '../../components/v2/console/Console';

/** Where the console opens when nobody has chosen a cell. */
export interface CellSelection {
  scenario: string;
  symbol: string;
  split: string;
}

/**
 * The split a run opens on: **holdout if it has one, else the run's own first
 * split** (which is `all` on a run submitted without a holdout).
 *
 * A run's splits are `['all']` XOR `['fit', 'holdout']`, so this never invents
 * one. `fit` is deliberately never the default on a run that HAS a holdout:
 * opening on the in-sample window is exactly the reading the holdout exists to
 * prevent, and an operator who lands there does not necessarily notice which
 * window the numbers came from.
 */
export function defaultSplit(splits: string[]): string | null {
  if (splits.includes('holdout')) return 'holdout';
  return splits[0] ?? null;
}

/**
 * The cell a run opens on: the first NON-BASE arm if there is one, the first
 * symbol, the default split.
 *
 * Non-base first because the question that made someone submit a sweep is "what
 * does the change do", and base is the thing it is measured against rather than
 * the thing under test. A base-only run opens on base.
 */
export function defaultCell(report: SweepReport): CellSelection | null {
  const scenario = report.scenarios.find((s) => s !== 'base') ?? report.scenarios[0];
  const symbol = report.symbols[0];
  const split = defaultSplit(report.windows.map((w) => w.split));
  if (!scenario || !symbol || !split) return null;
  return { scenario, symbol, split };
}

/** Is this cell addressable in this run? A stale deep link is not. */
export function cellExists(report: SweepReport, cell: CellSelection): boolean {
  return (
    report.scenarios.includes(cell.scenario) &&
    report.symbols.includes(cell.symbol) &&
    report.windows.some((w) => w.split === cell.split)
  );
}

/**
 * The run a `deduplicated` row auto-followed FROM, off the history entry's own
 * state. Anything else -- a typed URL, a bookmark, a reload of a plain cell --
 * is `null`, and the destination says nothing about dedup.
 */
export function readDedupFrom(state: unknown): string | null {
  if (!state || typeof state !== 'object') return null;
  const value = (state as { dedupFrom?: unknown }).dedupFrom;
  return typeof value === 'string' && value ? value : null;
}

export const cellPath = (runId: string, cell: CellSelection): string =>
  `/sims/${encodeURIComponent(runId)}/${encodeURIComponent(cell.scenario)}/` +
  `${encodeURIComponent(cell.symbol)}/${encodeURIComponent(cell.split)}`;

export default function Simulations() {
  const { runId, scenario, symbol, split } = useParams<{
    runId?: string;
    scenario?: string;
    symbol?: string;
    split?: string;
  }>();
  const navigate = useNavigate();
  const location = useLocation();
  const selectedRunId = runId ?? null;
  // Set by the dedup auto-follow below, so the destination can say which run it
  // was reached from. Absent on a typed or bookmarked URL, which is exactly the
  // notice's scope.
  const dedupFrom = readDedupFrom(location.state);

  // The universe checkboxes track the LIVE strategy config rather than a
  // hardcoded list — a symbol added to `stocks.symbols` shows up here without a
  // frontend deploy. A failed read is not fatal: free text still works.
  const { data: liveConfig } = useApi<LiveStrategyConfig>('/api/live/config');
  const { data: allowlist, error: allowlistError } = useSweepAllowlist();
  const {
    data: list,
    loading: listLoading,
    error: listError,
    refetch: refetchList,
  } = useSweepList();
  const { data: detail, error: detailError } = useSweepDetail(selectedRunId);

  const sweeps = list ?? [];
  // FC-096 Phase D (review round 1, F1): this page USED to render its own
  // session-expiry banner. It no longer does — `LayoutV2` renders exactly one,
  // on every route, and the hooks here raise the tab-wide signal the instant a
  // poll comes back signed out, so the layout's banner is not waiting on its
  // own 60-second poll. Two banners on one screen would say the same thing
  // twice and disagree about which of them owns the reload button.

  /**
   * A user's choice. PUSHES, so Back returns to what they were looking at.
   *
   * Re-selecting the run already on screen is a NO-OP (review round 1, F9). It
   * used to push `/sims/<same run>`, which dropped the cell out of the URL and
   * let the default-cell effect replace it — so clicking the highlighted row
   * silently threw away the cell the operator was reading, and left a history
   * entry that does nothing but undo itself.
   */
  const selectRun = (id: string) => {
    if (id === selectedRunId) return;
    setTweakNotice(null);
    navigate(`/sims/${encodeURIComponent(id)}`);
  };
  const selectCell = (cell: CellSelection) => {
    if (selectedRunId) navigate(cellPath(selectedRunId, cell));
  };

  /**
   * PR-4. A tweak's answer is a CELL of another run, so submitting navigates
   * exactly like any other user selection: it PUSHES, and Back returns the
   * operator to the cell they tweaked from.
   *
   * The notice lives HERE and not in the bar, because the bar unmounts on the
   * way: the destination run's detail has to load, and `ResultsRegion` renders
   * its loading shell in between. It is cleared by the next run selection so
   * "answered from a stored run" cannot outlive the run it describes.
   */
  const [tweakNotice, setTweakNotice] = useState<{ runId: string; text: string } | null>(null);
  const onTweakSubmitted = (target: { runId: string; scenario: string; notice: string }) => {
    setTweakNotice({ runId: target.runId, text: target.notice });
    // The symbol and the split are the ones on screen; the SCENARIO is the arm
    // the tweak just built — which for a 200 dedup is an arm the prior run
    // already carries under exactly this name, because the name is derived from
    // the spec and the spec is what deduplicated.
    navigate(
      cellPath(target.runId, {
        scenario: target.scenario,
        symbol: symbol ?? '',
        split: split ?? '',
      }),
    );
    refetchList();
  };

  // Land on the newest run so the page is not empty on arrival — REPLACING, so
  // Back from here leaves /sims rather than bouncing off an auto-select.
  // Depends on `list`, not the derived `sweeps`: `?? []` is a fresh identity on
  // every render.
  useEffect(() => {
    const first = list?.[0];
    if (selectedRunId === null && first) {
      navigate(`/sims/${encodeURIComponent(first.run_id)}`, { replace: true });
    }
  }, [selectedRunId, list, navigate]);

  // F1 (review round 1): the report on screen must belong to the URL's run.
  //
  // `usePolledGet` clears the previous run's data inside an EFFECT, so the first
  // committed render after a run switch carries the NEW `selectedRunId` and the
  // OLD `detail`. Two things went wrong in that one frame: the default/repair
  // effect below read the stale report, decided the new run "does not have" the
  // URL's cell, and `navigate(replace)`d to a cell of run A under run B's id --
  // destroying the operator's own history entry, so Back landed on the wrong
  // cell; and `ResultsRegion` rendered run A's grid under run B's URL.
  //
  // So everything downstream is gated on the IDENTITY, not on presence. A
  // mismatch renders the loading state, which is what it actually is.
  const detailMatchesUrl = !!detail && detail.sweep.run_id === selectedRunId;
  const matchedDetail = detailMatchesUrl ? detail : null;
  const report = matchedDetail?.results ?? null;
  const selection = useMemo<CellSelection | null>(
    () => (scenario && symbol && split ? { scenario, symbol, split } : null),
    [scenario, symbol, split],
  );

  // Default the CELL once a run resolves, and repair a deep link that names a
  // cell this run does not have (a bookmark from a different sweep). Both are
  // automatic, so both replace.
  useEffect(() => {
    if (!selectedRunId || !report) return;
    if (selection && cellExists(report, selection)) return;
    const fallback = defaultCell(report);
    // `state` is CARRIED, not dropped (confirmation pass, F5 gap). Entering a
    // deduplicated run from the runs list is two replaces: the auto-follow puts
    // `{dedupFrom}` on the entry, and this one immediately replaced it away —
    // so the destination lost the only record of where it was reached from and
    // neither the notice nor the footer's "reached from" row rendered. Both
    // replaces edit the SAME history entry, so its state has to survive them.
    if (fallback) {
      navigate(cellPath(selectedRunId, fallback), { replace: true, state: location.state });
    }
  }, [selectedRunId, report, selection, navigate, location.state]);

  // F5 (review round 1): a `deduplicated` row stored NOTHING under its own id --
  // the whole point of dedup is that nothing was replayed. Sec D-3 says the page
  // "opens X", so it does: the same cell under `deduplicated_to`, REPLACING,
  // because a run that holds no evidence is not a screen worth keeping in the
  // operator's history. The run it was reached from travels in the history
  // entry's state, so the destination can say so rather than pretending the
  // operator asked for it.
  const dedupPointer =
    matchedDetail && matchedDetail.sweep.status === 'deduplicated'
      ? (matchedDetail.sweep.deduplicated_to ?? null)
      : null;
  // A row pointing at ITSELF is corrupt, not resolvable: following it would
  // navigate to the screen already on display, for ever. Not followed, and said
  // in its own words below rather than reported as a missing pointer.
  const dedupTarget = dedupPointer && dedupPointer !== selectedRunId ? dedupPointer : null;
  useEffect(() => {
    if (!dedupTarget || !selectedRunId) return;
    const path = selection
      ? cellPath(dedupTarget, selection)
      : `/sims/${encodeURIComponent(dedupTarget)}`;
    navigate(path, { replace: true, state: { dedupFrom: selectedRunId } });
  }, [dedupTarget, selectedRunId, selection, navigate]);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold text-white">Simulations</h1>
        <p className="text-gray-400 mt-1 text-sm">
          Replay config variants over historical chains and compare them. These are{' '}
          <strong className="text-gray-300">hypotheses about a config</strong>, never a record of
          what the bot did — for that, read the live pages.
        </p>
      </header>

      <SubmitSweep
        allowlist={allowlist}
        allowlistError={allowlistError}
        universe={liveConfig?.stock_symbols ?? []}
        onSubmitted={(id) => {
          selectRun(id);
          refetchList();
        }}
        onSelectRun={selectRun}
      />

      <RunsList
        sweeps={sweeps}
        selectedRunId={selectedRunId}
        onSelect={selectRun}
        loading={listLoading}
        error={listError}
      />

      {selectedRunId && (
        <ResultsRegion
          detail={matchedDetail}
          // A read error belongs to the run that produced it. While the URL and
          // the loaded detail disagree there is no error to attribute yet.
          detailError={detail && !detailMatchesUrl ? null : detailError}
          selection={selection}
          onSelectCell={selectCell}
          dedupFrom={dedupFrom}
          allowlist={allowlist}
          allowlistError={allowlistError}
          onTweakSubmitted={onTweakSubmitted}
          tweakNotice={tweakNotice && tweakNotice.runId === selectedRunId ? tweakNotice.text : null}
        />
      )}
    </div>
  );
}

/**
 * Cell selector + split switch.
 *
 * The split switch offers **exactly the splits this run has** — never a fixed
 * three-way `all/fit/holdout` toggle. A run is either windowed (`fit` +
 * `holdout`) or not (`all`), and offering the other shape's buttons invites a
 * click that can only produce a 404 and a confused reading of why.
 */
function CellSelector({
  report,
  selection,
  onSelectCell,
}: {
  report: SweepReport;
  selection: CellSelection;
  onSelectCell: (cell: CellSelection) => void;
}) {
  const splits = report.windows.map((w) => w.split);
  const chip = (active: boolean) =>
    `px-2 py-1 rounded text-xs border ${
      active
        ? 'bg-blue-950/60 border-blue-700 text-blue-200'
        : 'bg-gray-800 border-gray-700 text-gray-400 hover:text-gray-200'
    }`;

  return (
    <div data-testid="cell-selector" className="flex flex-wrap gap-4 items-start">
      <div>
        <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Arm</div>
        <div className="flex flex-wrap gap-1">
          {report.scenarios.map((s) => (
            <button
              key={s}
              type="button"
              className={chip(s === selection.scenario)}
              onClick={() => onSelectCell({ ...selection, scenario: s })}
            >
              {s}
            </button>
          ))}
        </div>
      </div>
      <div>
        <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Symbol</div>
        <div className="flex flex-wrap gap-1">
          {report.symbols.map((s) => (
            <button
              key={s}
              type="button"
              className={chip(s === selection.symbol)}
              onClick={() => onSelectCell({ ...selection, symbol: s })}
            >
              {s}
            </button>
          ))}
        </div>
      </div>
      <div>
        <div className="text-[11px] uppercase tracking-wide text-gray-500 mb-1">Split</div>
        <div data-testid="split-switch" className="flex flex-wrap gap-1">
          {splits.map((s) => (
            <button
              key={s}
              type="button"
              className={chip(s === selection.split)}
              onClick={() => onSelectCell({ ...selection, split: s })}
            >
              {s}
            </button>
          ))}
        </div>
        {report.holdout_semantics && (
          <p className="text-[11px] text-gray-500 mt-1 max-w-md">{report.holdout_semantics}</p>
        )}
      </div>
    </div>
  );
}

/**
 * The results region, gated on the run's STATUS — not on whether a payload
 * happened to parse.
 *
 * A `submitted` or `running` sweep comes back with `grid: {}` and `splits: []`.
 * Rendering that as a report would tell the operator their sweep finished and
 * measured nothing, which is the opposite of the truth and unrecoverable from
 * the screen. So `results` is populated only for `done` (see
 * `normaliseSweepDetail`), and every other status gets a message that says what
 * is actually happening.
 */
function ResultsRegion({
  detail,
  detailError,
  selection,
  onSelectCell,
  dedupFrom,
  allowlist,
  allowlistError,
  onTweakSubmitted,
  tweakNotice,
}: {
  detail: SweepDetail | null;
  detailError: string | null;
  /** The cell in the URL, or `null` until the default effect has replaced it. */
  selection: CellSelection | null;
  onSelectCell: (cell: CellSelection) => void;
  /** The deduplicated run this screen was auto-opened from, if any. */
  dedupFrom: string | null;
  /** PR-4: the tweak bar's control types, and where a submit lands. */
  allowlist: SweepAllowlist | null;
  allowlistError: string | null;
  onTweakSubmitted: (target: { runId: string; scenario: string; notice: string }) => void;
  /** What the submit that brought us here said, or `null`. */
  tweakNotice: string | null;
}) {
  if (detailError && !detail) {
    return (
      <section className="rounded-lg border border-yellow-700/60 bg-gray-800 p-5">
        <h2 className="text-base font-semibold text-white">Results</h2>
        <p className="text-sm text-yellow-400 mt-2">
          ⚠ This run could not be read: {detailError}. Its state is <strong>unknown</strong>, not empty.
        </p>
      </section>
    );
  }
  if (!detail) {
    return (
      <section className="rounded-lg border border-gray-700 bg-gray-800 p-5">
        <h2 className="text-base font-semibold text-white">Results</h2>
        <p className="text-sm text-gray-400 mt-2">Loading…</p>
      </section>
    );
  }

  const { sweep, results, raw } = detail;
  const id = <span className="font-mono">{sweep.run_id}</span>;

  const shell = (body: React.ReactNode, tone: 'neutral' | 'bad' = 'neutral') => (
    <section
      data-testid="results-status"
      data-run-status={sweep.status}
      className={`rounded-lg border bg-gray-800 p-5 ${
        tone === 'bad' ? 'border-red-800/60' : 'border-gray-700'
      }`}
    >
      <h2 className="text-base font-semibold text-white">Results</h2>
      {body}
    </section>
  );

  if (sweep.status === 'submitted' || sweep.status === 'running') {
    return shell(
      <>
        <p className="text-sm text-gray-400 mt-2">
          {id} is <strong>{sweep.status}</strong>. A 6-symbol x 10-arm sweep takes roughly 6-8
          minutes; this page refreshes every 15 seconds until it finishes.
        </p>
        {sweep.stuck && (
          <p
            className="text-sm text-yellow-400 mt-2"
            title="Container start is 3-4 minutes. Past that with no `running` row, the execution probably never came up. Nothing is cancelled automatically."
          >
            ⚠ Still <span className="font-mono">submitted</span> past the container-start window —
            check the execution
            {sweep.execution_name && (
              <span className="block text-xs text-gray-500 font-mono break-all">
                {sweep.execution_name}
              </span>
            )}
          </p>
        )}
      </>,
    );
  }

  if (sweep.status === 'failed') {
    return shell(
      <>
        <p className="text-sm text-gray-300 mt-2">{id} failed and produced no report.</p>
        {sweep.error ? (
          <pre className="mt-2 text-xs text-red-300 whitespace-pre-wrap break-words font-mono">
            {sweep.error}
          </pre>
        ) : (
          <p className="text-xs text-gray-500 mt-2">
            The run recorded no error text. Check the execution
            {sweep.execution_name ? ` (${sweep.execution_name})` : ''}.
          </p>
        )}
      </>,
      'bad',
    );
  }

  // A `deduplicated` row WITH a pointer never reaches here: the page follows it
  // (see the auto-follow effect above) rather than parking the operator on a run
  // that stored nothing. Only the dead end is rendered -- a dedup row whose
  // pointer was never recorded, which is unopenable and has to say so.
  if (sweep.status === 'deduplicated') {
    return shell(
      <p className="text-sm text-gray-400 mt-2">
        {id} was deduplicated: this exact spec had already completed on this engine and commit, so
        nothing was replayed.{' '}
        {sweep.deduplicated_to === sweep.run_id
          ? 'This row points at ITSELF, which cannot be true — the pointer is corrupt, not missing. The run that answered this spec has to be found by its spec in the runs list.'
          : 'The original run is not recorded on this row, so there is nothing to open — find it by its spec in the runs list.'}
      </p>,
    );
  }

  // `done`, but the payload carried nothing renderable.
  if (!results) {
    return shell(
      <p className="text-sm text-gray-400 mt-2">
        {id} is <strong>{sweep.status}</strong> but carries no report rows. That is a gap in the
        store, not a sweep that measured nothing.
      </p>,
      'bad',
    );
  }

  // FC-096 Phase E PR-2: the grid stays exactly where it was, and the console
  // mounts UNDER it. The grid is how a run is read across arms and symbols; the
  // console is how one cell of it is read in depth, and neither replaces the
  // other.
  //
  // The bias footer is LAST, below the console (review round 1, F2). It used to
  // be `SweepResults`'s own final child, which put "how far to trust these
  // numbers" in the MIDDLE of the page the moment the console mounted beneath
  // it — the caveats above half the evidence they qualify, and the operator's
  // last word on the screen a provenance table. The order is asserted with
  // `compareDocumentPosition` rather than left to whoever edits this JSX next.
  return (
    <div className="space-y-6">
      {detailError && (
        <p className="text-xs text-yellow-400">
          ⚠ Could not refresh this run ({detailError}) — showing the last good read.
        </p>
      )}
      {tweakNotice && (
        <p
          data-testid="tweak-notice"
          className="rounded border border-blue-800/60 bg-blue-950/30 px-3 py-2 text-sm text-blue-200"
        >
          {tweakNotice}
        </p>
      )}
      <SweepResults sweep={sweep} report={results} raw={raw} />
      {selection && cellExists(results, selection) && (
        <section className="rounded-lg border border-gray-700 bg-gray-800 p-5 space-y-4">
          <h2 className="text-base font-semibold text-white">Cell detail</h2>
          <CellSelector report={results} selection={selection} onSelectCell={onSelectCell} />
          <Console
            sweep={sweep}
            report={results}
            scenario={selection.scenario}
            symbol={selection.symbol}
            split={selection.split}
            dedupFrom={dedupFrom}
            // A symbol TAB is a cell selection like any other: it pushes, so
            // Back walks the operator's own history through it.
            onSelectSymbol={(symbol) => onSelectCell({ ...selection, symbol })}
            allowlist={allowlist}
            allowlistError={allowlistError}
            onTweakSubmitted={onTweakSubmitted}
          />
        </section>
      )}
      <BiasFooter report={results} />
    </div>
  );
}
