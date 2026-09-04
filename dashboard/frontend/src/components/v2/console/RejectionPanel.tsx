// FC-096 Phase E PR-3, component 6: why the engine did nothing on the days it
// did nothing.
//
// The reasons render in the SERVED ORDER — the engine ranked them and this
// panel does not re-rank, re-sort or re-group them. The bar widths are relative
// to the top reason, which is a picture of the served order and not a second
// ordering of it (a test feeds a reversed list and asserts the panel reverses).
//
// The tally caveat prints VERBATIM. It is the paragraph that says this column
// was empty for every replay but the first in a process until the structlog
// proxy bug was fixed — a caveat that gets paraphrased is a caveat that stops
// being checkable against the report it came from.

import type { SimArtifact } from '../../../types/v2';

export interface RejectionPanelProps {
  artifact: SimArtifact | null;
  absence: string | null;
  /** The run's own list, from `GET /sweeps/{run_id}` — wider than this cell. */
  earningsSymbolsWithoutData: string[];
  /** `report.rejection_tally_caveat`, rendered verbatim. */
  caveat: string | null;
}

export default function RejectionPanel({
  artifact,
  absence,
  earningsSymbolsWithoutData,
  caveat,
}: RejectionPanelProps) {
  if (!artifact) {
    return (
      <section
        data-testid="rejection-panel"
        className="rounded-lg border border-gray-700 bg-gray-800 p-5"
      >
        <h3 className="text-base font-semibold text-white">Rejections and binding constraint</h3>
        <p data-testid="rejection-absent" className="text-sm text-gray-400 mt-2">
          {absence ?? 'No rejection tally was stored for this cell.'}
        </p>
      </section>
    );
  }

  const rejections = artifact.rejections;
  const top = rejections.length ? Math.max(...rejections.map((r) => r.days)) : 0;
  const counters = artifact.counters;
  const coverage = artifact.earnings_coverage;
  const unpriced = counters.unpriced_ex_div_calls ?? 0;

  return (
    <section
      data-testid="rejection-panel"
      className="rounded-lg border border-gray-700 bg-gray-800 p-5"
    >
      <h3 className="text-base font-semibold text-white">Rejections and binding constraint</h3>
      <p className="text-xs text-gray-500 mt-1 mb-3">
        <span data-testid="rejection-days">
          {counters.candidate_days ?? '—'} candidate days of {counters.decision_days ?? '—'}{' '}
          decision days
        </span>{' '}
        — the rest were rejected for the reasons below, in the engine&rsquo;s own ranked order.
      </p>

      {rejections.length === 0 ? (
        <p data-testid="rejection-empty" className="text-sm text-gray-400">
          This cell recorded no rejection reasons.
        </p>
      ) : (
        <ol data-testid="rejection-list" className="space-y-1">
          {rejections.map((rejection, i) => {
            const binding = rejection.reason === artifact.binding_constraint;
            return (
              <li key={`${rejection.reason}-${i}`} data-testid="rejection-row">
                <div className="flex items-baseline gap-2 text-xs">
                  <span className={binding ? 'text-amber-300 font-semibold' : 'text-gray-300'}>
                    {rejection.reason}
                    {binding && (
                      <span data-testid="rejection-binding" className="ml-2 text-[10px] uppercase">
                        binding constraint
                      </span>
                    )}
                  </span>
                  <span className="ml-auto text-gray-400 font-mono">{rejection.days}d</span>
                </div>
                <div
                  className={`h-1.5 rounded ${binding ? 'bg-amber-500/70' : 'bg-gray-600'}`}
                  style={{ width: `${top ? (rejection.days / top) * 100 : 0}%` }}
                />
              </li>
            );
          })}
        </ol>
      )}

      <div className="mt-4 space-y-1 text-xs text-gray-400">
        {coverage && (
          <p data-testid="rejection-earnings">
            Earnings coverage on this cell: {coverage.symbols_without_data.length
              ? `no calendar rows for ${coverage.symbols_without_data.join(', ')}`
              : 'every symbol had calendar rows'}
            {coverage.symbols_past_horizon.length
              ? `; past the horizon for ${coverage.symbols_past_horizon.join(', ')}`
              : ''}
            .
          </p>
        )}
        {earningsSymbolsWithoutData.length > 0 && (
          <p data-testid="rejection-earnings-run" className="text-amber-400">
            Across this whole run the earnings gate had no data for{' '}
            {earningsSymbolsWithoutData.join(', ')} — a caveat on those cells, not a defect.
          </p>
        )}
        {unpriced > 0 && (
          <p data-testid="rejection-unpriced" className="text-amber-400">
            {unpriced} ex-dividend call{unpriced === 1 ? '' : 's'} could not be priced for the
            early-assignment check.
          </p>
        )}
      </div>

      {caveat && (
        <p
          data-testid="rejection-caveat"
          className="mt-3 text-[11px] text-gray-500 whitespace-pre-wrap border-t border-gray-700 pt-2"
        >
          {caveat}
        </p>
      )}
    </section>
  );
}
