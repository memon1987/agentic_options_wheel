// FC-060 Layer 4 (PR-B), region 2: the runs list.
//
// Status comes from BigQuery, not from polling the Cloud Run execution (plan
// D3): `run.executions.get` for the dashboard SA is unproven and grantable only
// from the console, while BQ reads are proven. The cost of that choice is that a
// Job that dies before its first write leaves a `submitted` row that never
// advances — so the list carries an explicit "stuck" hint rather than a spinner
// that spins forever. Nothing is cancelled automatically; the timeout is the
// operator's signal.

import type { SweepRow } from '../../../types/v2';
import { fmtDateTime, fmtRelativeAge, cls } from '../../../utils/format';

interface Props {
  sweeps: SweepRow[];
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
  loading: boolean;
  error: string | null;
}

const PILL: Record<string, string> = {
  submitted: 'bg-blue-900/60 text-blue-300 border-blue-700',
  running: 'bg-indigo-900/60 text-indigo-300 border-indigo-700',
  done: 'bg-green-900/60 text-green-300 border-green-700',
  failed: 'bg-red-900/60 text-red-300 border-red-700',
  deduplicated: 'bg-gray-700/60 text-gray-300 border-gray-600',
};

function StatusPill({ status }: { status: string }) {
  return (
    <span
      data-testid={`status-${status}`}
      className={cls(
        'px-2 py-0.5 rounded text-xs font-medium border whitespace-nowrap',
        PILL[status] ?? 'bg-gray-700/60 text-gray-300 border-gray-600',
      )}
    >
      {status}
    </span>
  );
}

export default function RunsList({ sweeps, selectedRunId, onSelect, loading, error }: Props) {
  return (
    <section className="rounded-lg border border-gray-700 bg-gray-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-700 flex items-baseline justify-between flex-wrap gap-2">
        <h2 className="text-base font-semibold text-white">Runs</h2>
        <span className="text-xs text-gray-500">
          status from <span className="font-mono">scenario_sweeps</span> · refreshes every 15s while a run is live
        </span>
      </div>

      {/* A read failure keeps whatever was already rendered and says so beside
          it — a transient 500 must not blank a list the operator is watching. */}
      {error && (
        <p className="px-4 py-2 text-xs text-yellow-400 border-b border-gray-700">
          ⚠ Could not refresh the runs list: {error}
        </p>
      )}

      {loading && sweeps.length === 0 ? (
        <p className="px-4 py-6 text-sm text-gray-400">Loading…</p>
      ) : sweeps.length === 0 ? (
        <p className="px-4 py-6 text-sm text-gray-400">
          No sweeps yet. Submit one above — a 6-symbol x 10-arm sweep takes roughly 6-8 minutes.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs uppercase tracking-wide text-gray-400">
              <tr>
                <th className="text-left px-4 py-2">Run</th>
                <th className="text-left px-3 py-2">Status</th>
                <th className="text-left px-3 py-2">Submitted</th>
                <th className="text-left px-3 py-2">Window</th>
                <th className="text-right px-3 py-2">Cells</th>
                <th className="text-right px-4 py-2">Wall</th>
              </tr>
            </thead>
            <tbody>
              {sweeps.map((row) => {
                const stuck = row.stuck === true;
                return (
                  <tr
                    key={row.run_id}
                    data-testid={`run-row-${row.run_id}`}
                    onClick={() => onSelect(row.run_id)}
                    // Selecting a run is a real action, so it has to be reachable
                    // without a mouse. A row is the click target rather than a
                    // cell, so the role and the key handler go here.
                    role="button"
                    tabIndex={0}
                    aria-pressed={selectedRunId === row.run_id}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        onSelect(row.run_id);
                      }
                    }}
                    className={cls(
                      'border-t border-gray-700/60 cursor-pointer hover:bg-gray-700/40',
                      'focus:outline-none focus:ring-2 focus:ring-inset focus:ring-blue-500',
                      selectedRunId === row.run_id && 'bg-gray-700/50',
                    )}
                  >
                    <td className="px-4 py-2">
                      <span className="font-mono text-blue-300 text-xs">{row.run_id}</span>
                      <div className="text-xs text-gray-500 mt-0.5">
                        {(row.symbols ?? []).join(', ') || '—'}
                        {row.scenario_count != null && ` · ${row.scenario_count} arms`}
                        {row.in_sample_only && (
                          <span className="text-yellow-500" title="No holdout — the ranking is unvalidated.">
                            {' '}· in-sample only
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <StatusPill status={row.status} />
                      {row.deduplicated_to && (
                        <div className="mt-1">
                          <button
                            type="button"
                            data-testid={`dedup-link-${row.run_id}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              onSelect(row.deduplicated_to as string);
                            }}
                            className="text-xs text-blue-400 hover:text-blue-300 underline font-mono"
                            title="An identical spec on the same commit had already run. Nothing was replayed; these are those results."
                          >
                            → {row.deduplicated_to}
                          </button>
                        </div>
                      )}
                      {stuck && (
                        <div
                          className="mt-1 text-xs text-yellow-400"
                          title="Container start is 3-4 minutes. Past 10 with no `running` row, the execution probably never came up. Nothing is cancelled automatically — check the execution."
                        >
                          ⚠ stuck — check the execution
                          {row.execution_name && (
                            <span className="block text-gray-500 font-mono break-all">{row.execution_name}</span>
                          )}
                        </div>
                      )}
                      {row.status === 'failed' && row.error && (
                        <div className="mt-1 text-xs text-red-300 max-w-md break-words">{row.error}</div>
                      )}
                    </td>
                    <td className="px-3 py-2 text-gray-300 text-xs whitespace-nowrap">
                      {fmtDateTime(row.submitted_at)}
                      <span className="block text-gray-500">{fmtRelativeAge(row.submitted_at)}</span>
                    </td>
                    <td className="px-3 py-2 text-gray-400 text-xs whitespace-nowrap">
                      {row.window_start ?? '—'} → {row.window_end ?? '—'}
                      {row.holdout_start && (
                        <span className="block text-gray-500">holdout from {row.holdout_start}</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right text-gray-300">{row.cell_count ?? '—'}</td>
                    <td className="px-4 py-2 text-right text-gray-300">
                      {row.wall_seconds != null ? `${Math.round(row.wall_seconds)}s` : '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
