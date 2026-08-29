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
import type { LiveStrategyConfig } from '../../types/v2';
import {
  readStoredToken,
  useSweepAllowlist,
  useSweepDetail,
  useSweepList,
} from '../../hooks/useSweeps';
import SubmitSweep from '../../components/v2/sims/SubmitSweep';
import RunsList from '../../components/v2/sims/RunsList';
import SweepResults from '../../components/v2/sims/SweepResults';

export default function Simulations() {
  const [token, setToken] = useState<string>(() => readStoredToken());
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  // The universe checkboxes track the LIVE strategy config rather than a
  // hardcoded list — a symbol added to `stocks.symbols` shows up here without a
  // frontend deploy. A failed read is not fatal: free text still works.
  const { data: liveConfig } = useApi<LiveStrategyConfig>('/api/live/config');
  const { data: allowlist, error: allowlistError } = useSweepAllowlist();
  const { data: list, loading: listLoading, error: listError, refetch: refetchList } = useSweepList();
  const { data: detail, error: detailError } = useSweepDetail(selectedRunId);

  const sweeps = list?.sweeps ?? [];

  // Land on the newest run so the page is not empty on arrival. Only until the
  // operator picks one — after that their choice sticks.
  // Depends on `list`, not the derived `sweeps`: `data?.sweeps ?? []` is a fresh
  // array identity every render, which would re-run this effect on every poll.
  useEffect(() => {
    const first = list?.sweeps?.[0];
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

      <SubmitSweep
        allowlist={allowlist}
        allowlistError={allowlistError}
        universe={liveConfig?.stock_symbols ?? []}
        token={token}
        onTokenChange={setToken}
        onSubmitted={(runId) => {
          setSelectedRunId(runId);
          refetchList();
        }}
      />

      <RunsList
        sweeps={sweeps}
        selectedRunId={selectedRunId}
        onSelect={setSelectedRunId}
        loading={listLoading}
        error={listError}
      />

      {selectedRunId && <ResultsRegion detailError={detailError} detail={detail} />}
    </div>
  );
}

type Detail = ReturnType<typeof useSweepDetail>['data'];

/**
 * The three-state card: unreadable, not-yet-there, and rendered.
 *
 * A finished-but-empty report is NOT rendered as an empty grid — that reads as
 * "the sweep measured nothing", which is a different claim from "the results
 * could not be read".
 */
function ResultsRegion({ detail, detailError }: { detail: Detail; detailError: string | null }) {
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

  const { sweep, results } = detail;

  if (!results) {
    const waiting = sweep.status === 'submitted' || sweep.status === 'running';
    return (
      <section className="rounded-lg border border-gray-700 bg-gray-800 p-5">
        <h2 className="text-base font-semibold text-white">Results</h2>
        <p className="text-sm text-gray-400 mt-2">
          {waiting ? (
            <>
              <span className="font-mono">{sweep.run_id}</span> is <strong>{sweep.status}</strong>.
              A 6-symbol x 10-arm sweep takes roughly 6-8 minutes; this page refreshes every 15
              seconds until it finishes.
            </>
          ) : sweep.status === 'failed' ? (
            <>
              <span className="font-mono">{sweep.run_id}</span> failed and produced no report.
            </>
          ) : (
            <>
              <span className="font-mono">{sweep.run_id}</span> is{' '}
              <strong>{sweep.status}</strong> but carries no report rows.
            </>
          )}
        </p>
        {sweep.error && (
          <pre className="mt-2 text-xs text-red-300 whitespace-pre-wrap break-words font-mono">
            {sweep.error}
          </pre>
        )}
        {sweep.deduplicated_to && (
          <p className="text-sm text-gray-400 mt-2">
            Deduplicated to <span className="font-mono">{sweep.deduplicated_to}</span> — select that
            run above to read its results.
          </p>
        )}
      </section>
    );
  }

  return (
    <>
      {detailError && (
        <p className="text-xs text-yellow-400">
          ⚠ Could not refresh this run ({detailError}) — showing the last good read.
        </p>
      )}
      <SweepResults sweep={sweep} report={results} />
    </>
  );
}
