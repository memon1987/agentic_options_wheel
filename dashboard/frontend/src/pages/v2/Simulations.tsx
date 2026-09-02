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

import { useEffect, useState } from 'react';
import { useApi } from '../../hooks/useApi';
import type { LiveStrategyConfig, SweepDetail } from '../../types/v2';
import {
  SESSION_EXPIRED_MESSAGE,
  useSweepAllowlist,
  useSweepDetail,
  useSweepList,
} from '../../hooks/useSweeps';
import SubmitSweep from '../../components/v2/sims/SubmitSweep';
import RunsList from '../../components/v2/sims/RunsList';
import SweepResults from '../../components/v2/sims/SweepResults';

export default function Simulations() {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  // The universe checkboxes track the LIVE strategy config rather than a
  // hardcoded list — a symbol added to `stocks.symbols` shows up here without a
  // frontend deploy. A failed read is not fatal: free text still works.
  const { data: liveConfig } = useApi<LiveStrategyConfig>('/api/live/config');
  const { data: allowlist, error: allowlistError } = useSweepAllowlist();
  const {
    data: list,
    loading: listLoading,
    error: listError,
    sessionExpired: listSessionExpired,
    refetch: refetchList,
  } = useSweepList();
  const {
    data: detail,
    error: detailError,
    sessionExpired: detailSessionExpired,
  } = useSweepDetail(selectedRunId);

  const sweeps = list ?? [];
  // FC-096 Phase D: the page is behind IAP, and an expired session makes every
  // poll fail identically for ever. Said once, at the top, with the only action
  // that helps — otherwise the operator reads three separate "could not read"
  // errors and concludes the backend is down.
  const sessionExpired = listSessionExpired || detailSessionExpired;

  // Land on the newest run so the page is not empty on arrival. Only until the
  // operator picks one — after that their choice sticks. Depends on `list`, not
  // the derived `sweeps`: `?? []` is a fresh identity on every render.
  useEffect(() => {
    const first = list?.[0];
    if (selectedRunId === null && first) setSelectedRunId(first.run_id);
  }, [selectedRunId, list]);

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

      {sessionExpired && (
        <section
          data-testid="session-expired-banner"
          className="rounded-lg border border-yellow-700/70 bg-yellow-950/30 p-4 flex items-center justify-between gap-4 flex-wrap"
        >
          <div>
            <p className="text-sm font-medium text-yellow-300">{SESSION_EXPIRED_MESSAGE}</p>
            <p className="text-xs text-yellow-200/80 mt-1">
              Nothing on this page is refreshing any more. Reloading signs you back in through
              Google and returns you here.
            </p>
          </div>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="px-3 py-1.5 rounded text-xs font-medium bg-yellow-700 hover:bg-yellow-600 text-white"
          >
            Reload
          </button>
        </section>
      )}

      <SubmitSweep
        allowlist={allowlist}
        allowlistError={allowlistError}
        universe={liveConfig?.stock_symbols ?? []}
        onSubmitted={(runId) => {
          setSelectedRunId(runId);
          refetchList();
        }}
        onSelectRun={setSelectedRunId}
      />

      <RunsList
        sweeps={sweeps}
        selectedRunId={selectedRunId}
        onSelect={setSelectedRunId}
        loading={listLoading}
        error={listError}
      />

      {selectedRunId && (
        <ResultsRegion detail={detail} detailError={detailError} onSelect={setSelectedRunId} />
      )}
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
  onSelect,
}: {
  detail: SweepDetail | null;
  detailError: string | null;
  onSelect: (runId: string) => void;
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

  if (sweep.status === 'deduplicated') {
    return shell(
      <p className="text-sm text-gray-400 mt-2">
        {id} was deduplicated: this exact spec had already completed on this engine and commit, so
        nothing was replayed.{' '}
        {sweep.deduplicated_to ? (
          <button
            type="button"
            onClick={() => onSelect(sweep.deduplicated_to as string)}
            className="text-blue-400 hover:text-blue-300 underline font-mono"
          >
            Open {sweep.deduplicated_to}
          </button>
        ) : (
          'The original run is not recorded on this row.'
        )}
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

  return (
    <>
      {detailError && (
        <p className="text-xs text-yellow-400">
          ⚠ Could not refresh this run ({detailError}) — showing the last good read.
        </p>
      )}
      <SweepResults sweep={sweep} report={results} raw={raw} />
    </>
  );
}
