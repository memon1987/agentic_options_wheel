// FC-060 Layer 4 (PR-B), region 3: render a finished sweep.
//
// The plan's two hard rules, restated because they are what this file is for:
//
//   * NO AGGREGATE WITHOUT THE GRID. The per-scenario summary and the
//     fit/holdout medians are rendered from inside this component only, and this
//     component always renders the per-symbol grid above them. A median with no
//     visible grid is a ranking with no way to see that four of its six cells
//     were `insuf`.
//   * NO BELOW-FLOOR CELL STYLED AS A RETURN. Enforced in `renderCell`.
//
// Nothing here computes a statistic. Median, min, max and the state counts are
// read from the server's `summary`; deltas from `delta_vs_base`; agreement from
// `sign_agreement`. The one thing counted client-side is `unknown` cells, which
// the server's summary has no column for and without which the row would not
// add up to the cells on screen.
//
// Everything narrative — the in-sample banner, the holdout semantics, the bias
// footer, the cross-scenario and rejection-tally caveats — is printed BYTE FOR
// BYTE from the API. These are the runner's own words about how far its numbers
// can be trusted. An earlier cut stripped markdown out of them, which quietly
// edited a warning; the fix is to render the string and nothing but the string.

import type { SweepReport, SweepRow } from '../../../types/v2';
import { fmtNumber, cls } from '../../../utils/format';
import {
  indexRows,
  lookupCell,
  pctOrDash,
  renderCell,
  splitsOf,
  summaryFor,
  unknownCount,
} from './resultCells';

interface Props {
  sweep: SweepRow;
  report: SweepReport;
  /** The response exactly as served — what "Download JSON" writes out. */
  raw?: unknown;
}

const SPLIT_LABEL: Record<string, string> = { fit: 'Fit', holdout: 'Holdout', all: 'Full window' };
const splitLabel = (s: string) => SPLIT_LABEL[s] ?? s;

/**
 * Shown when `in_sample_only` is true but the payload carried no banner string.
 *
 * The banner keys on the FLAG, never on the presence of the text: a missing
 * string must not be able to suppress the warning. This copy is the fallback of
 * last resort — the payload's own words are used whenever they arrive.
 */
const IN_SAMPLE_FALLBACK =
  'IN-SAMPLE ONLY — this ranking has not been validated. Every arm below was ' +
  'measured on the same window it would be chosen from, over a single volatility ' +
  'regime. The best-looking arm is more often the luckiest one than the best one. ' +
  'Re-run with a holdout and act on the sign-agreement column, not on this table.';

/** An arm that varies only the fill assumption, not any config key. */
const isHaircutOnly = (report: SweepReport, scenario: string): boolean =>
  report.scenario_fill_haircuts?.[scenario] != null &&
  Object.keys(report.scenario_overrides?.[scenario] ?? {}).length === 0;

/** The server's prose, rendered exactly as sent. No parsing, no stripping. */
function Verbatim({ text, className }: { text: string; className?: string }) {
  return <p className={cls('whitespace-pre-wrap', className)}>{text}</p>;
}

export default function SweepResults({ sweep, report, raw }: Props) {
  const splits = splitsOf(report);
  const index = indexRows(report.rows);
  const hasHoldout = splits.includes('holdout');
  const baseConfigHash = report.scenario_config_hashes?.base ?? report.base_config_hash ?? null;

  const download = () => {
    // The RAW response, not the normalised reconstruction. An export that
    // differed from what the API served would be useless for reproducing a run
    // or filing a bug against it.
    const payload = raw === undefined ? report : raw;
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `sweep-${sweep.run_id}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <section className="space-y-5" data-testid="sweep-results">
      {/* Keyed on the FLAG alone. A missing banner string cannot suppress it. */}
      {report.in_sample_only && (
        <div
          data-testid="in-sample-banner"
          className="rounded-lg border border-yellow-700/70 bg-yellow-950/40 p-4"
        >
          <Verbatim
            text={report.in_sample_banner ?? IN_SAMPLE_FALLBACK}
            className="text-xs text-yellow-200/90"
          />
        </div>
      )}

      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <h2 className="text-lg font-semibold text-white">Results</h2>
        <button
          type="button"
          onClick={download}
          className="text-xs px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-gray-200"
        >
          Download JSON
        </button>
      </div>

      {/* ---- THE GRID. Rendered first and unconditionally. ---- */}
      {splits.map((split) => {
        const window = report.windows.find((w) => w.split === split);
        return (
          <div key={split} className="rounded-lg border border-gray-700 bg-gray-800 overflow-hidden">
            <div className="px-4 py-2 border-b border-gray-700 flex items-baseline justify-between flex-wrap gap-2">
              <h3 className="text-sm font-semibold text-white">
                {splitLabel(split)}
                <span className="text-xs text-gray-500 font-normal ml-2">
                  {window ? `${window.start} → ${window.end}` : ''}
                </span>
              </h3>
              <span className="text-xs text-gray-500">
                annualised return · <span className="text-gray-400">+</span> fit ·{' '}
                <span className="text-gray-400">~</span> marginal · <span className="text-gray-400">-</span> unfit
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-xs uppercase tracking-wide text-gray-400 bg-gray-800/80">
                  <tr>
                    <th className="text-left px-4 py-2">Scenario</th>
                    {report.symbols.map((sym) => (
                      <th key={sym} className="text-right px-3 py-2 font-mono">{sym}</th>
                    ))}
                    <th
                      className="text-right px-3 py-2"
                      title="Cells carrying a number worth ranking / no completed cycle / held on too few days / errored / never classified. These sum to the cells in the row."
                    >
                      measured · insuf · low-act · err · unk
                    </th>
                    <th className="text-right px-3 py-2" title="Server-computed median over the MEASURED cells only">
                      median
                    </th>
                    <th className="text-right px-3 py-2" title="Server-computed min and max over the MEASURED cells only">
                      min / max
                    </th>
                    <th className="text-right px-4 py-2" title="Median of the per-symbol deltas over the common measured subset">
                      Δ vs base
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {report.scenarios.map((scenario) => {
                    const summary = summaryFor(report, scenario, split);
                    const unk = unknownCount(report.rows, scenario, split);
                    const delta = report.delta_vs_base?.[split]?.[scenario];
                    const isBase = scenario === 'base';
                    const measured = summary?.measured ?? 0;
                    return (
                      <tr key={scenario} className="border-t border-gray-700/60">
                        <td className="px-4 py-2 font-mono text-blue-300 whitespace-nowrap">
                          {scenario}
                          {isHaircutOnly(report, scenario) ? (
                            <span
                              className="text-amber-400/80 text-xs ml-2 font-sans"
                              title="This arm changes no config key — only the assumed fill price. It measures how sensitive the result is to the fill model, not whether a different strategy is better."
                            >
                              fill-model sensitivity (haircut{' '}
                              {report.scenario_fill_haircuts?.[scenario]})
                            </span>
                          ) : (
                            report.scenario_fill_haircuts?.[scenario] != null && (
                              <span className="text-gray-500 text-xs ml-2">
                                haircut {report.scenario_fill_haircuts[scenario]}
                              </span>
                            )
                          )}
                          {(summary?.demote_flags ?? 0) > 0 && (
                            <span
                              className="text-xs text-gray-500 ml-2 font-sans"
                              title="Cells the engine flagged as demotion candidates."
                            >
                              {summary?.demote_flags} demote
                            </span>
                          )}
                        </td>
                        {report.symbols.map((sym) => {
                          const cell = renderCell(lookupCell(index, scenario, sym, split));
                          return (
                            <td key={sym} className="px-3 py-2 text-right">
                              <span
                                data-testid={`cell-${scenario}-${sym}-${split}`}
                                data-cell-kind={cell.kind}
                                title={cell.title}
                                className={cls('inline-block px-2 py-0.5 rounded text-xs', cell.className)}
                              >
                                {cell.text}
                              </span>
                            </td>
                          );
                        })}
                        <td
                          className="px-3 py-2 text-right text-xs whitespace-nowrap"
                          data-testid={`counts-${scenario}-${split}`}
                        >
                          <span className="text-gray-200">{measured}</span>
                          <span className="text-gray-600"> · </span>
                          <span className={summary?.insufficient ? 'text-gray-300' : 'text-gray-600'}>
                            {summary?.insufficient ?? 0}
                          </span>
                          <span className="text-gray-600"> · </span>
                          <span className={summary?.low_activity ? 'text-amber-300' : 'text-gray-600'}>
                            {summary?.low_activity ?? 0}
                          </span>
                          <span className="text-gray-600"> · </span>
                          <span className={summary?.errors ? 'text-red-300' : 'text-gray-600'}>
                            {summary?.errors ?? 0}
                          </span>
                          <span className="text-gray-600"> · </span>
                          <span className={unk ? 'text-purple-300' : 'text-gray-600'}>{unk}</span>
                        </td>
                        <td className="px-3 py-2 text-right text-gray-200">
                          {measured === 0 ? (
                            <span
                              className="text-xs text-gray-500"
                              title="Nothing was measured in this row, so there is no median to take."
                            >
                              no measured cell
                            </span>
                          ) : (
                            pctOrDash(summary?.median ?? null)
                          )}
                        </td>
                        <td className="px-3 py-2 text-right text-gray-400 text-xs whitespace-nowrap">
                          {measured === 0
                            ? '—'
                            : `${pctOrDash(summary?.min ?? null)} / ${pctOrDash(summary?.max ?? null)}`}
                        </td>
                        <td className="px-4 py-2 text-right whitespace-nowrap">
                          {isBase ? (
                            <span className="text-gray-600 text-xs">—</span>
                          ) : (
                            <>
                              <span className="text-gray-200">{pctOrDash(delta?.median ?? null)}</span>
                              <span
                                className="text-xs text-gray-500 ml-1"
                                title="Symbols measured in BOTH this arm and base. A delta over one symbol is an anecdote."
                              >
                                (n={delta?.symbols ?? 0})
                              </span>
                            </>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}

      {/* ---- Fit vs holdout, side by side, with sign agreement. ---- */}
      {hasHoldout && (
        <div className="rounded-lg border border-gray-700 bg-gray-800 overflow-hidden">
          <div className="px-4 py-2 border-b border-gray-700">
            <h3 className="text-sm font-semibold text-white">Fit vs holdout</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase tracking-wide text-gray-400">
                <tr>
                  <th className="text-left px-4 py-2">Scenario</th>
                  <th className="text-right px-3 py-2">median (fit)</th>
                  <th className="text-right px-3 py-2">median (holdout)</th>
                  <th className="text-right px-3 py-2">Δ vs base (fit)</th>
                  <th className="text-right px-3 py-2">Δ vs base (holdout)</th>
                  <th
                    className="text-right px-4 py-2"
                    title="Symbols where the arm's sign vs base is the SAME in both windows, over the symbols comparable in both. Read it as evidence, not as a pass mark: 1/1 is one symbol agreeing with itself."
                  >
                    sign agreement
                  </th>
                </tr>
              </thead>
              <tbody>
                {report.scenarios.map((scenario) => {
                  const fit = summaryFor(report, scenario, 'fit');
                  const hold = summaryFor(report, scenario, 'holdout');
                  const dFit = report.delta_vs_base?.fit?.[scenario];
                  const dHold = report.delta_vs_base?.holdout?.[scenario];
                  const agree = report.sign_agreement?.[scenario];
                  const isBase = scenario === 'base';
                  return (
                    <tr key={scenario} className="border-t border-gray-700/60">
                      <td className="px-4 py-2 font-mono text-blue-300">{scenario}</td>
                      <td className="px-3 py-2 text-right text-gray-200">
                        {(fit?.measured ?? 0) === 0 ? (
                          <span className="text-xs text-gray-500">—</span>
                        ) : (
                          pctOrDash(fit?.median ?? null)
                        )}
                      </td>
                      <td className="px-3 py-2 text-right text-gray-200">
                        {(hold?.measured ?? 0) === 0 ? (
                          <span className="text-xs text-gray-500">—</span>
                        ) : (
                          pctOrDash(hold?.median ?? null)
                        )}
                      </td>
                      <td className="px-3 py-2 text-right text-gray-200">
                        {isBase ? '—' : `${pctOrDash(dFit?.median ?? null)} (n=${dFit?.symbols ?? 0})`}
                      </td>
                      <td className="px-3 py-2 text-right text-gray-200">
                        {isBase ? '—' : `${pctOrDash(dHold?.median ?? null)} (n=${dHold?.symbols ?? 0})`}
                      </td>
                      {/* Plain text, deliberately uncoloured. Green on 1/1 read
                          as "validated" — it is one symbol agreeing with itself. */}
                      <td
                        className="px-4 py-2 text-right font-mono text-gray-200"
                        data-testid={`agreement-${scenario}`}
                      >
                        {isBase || !agree || agree.comparable === 0
                          ? '—'
                          : `${agree.agreeing}/${agree.comparable}`}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {report.holdout_semantics && (
            <div className="px-4 py-3 border-t border-gray-700">
              <Verbatim text={report.holdout_semantics} className="text-xs text-gray-400" />
            </div>
          )}
        </div>
      )}

      {/* ---- Provenance. What was actually run. ---- */}
      <div className="rounded-lg border border-gray-700 bg-gray-800 p-4">
        <h3 className="text-sm font-semibold text-white">Provenance</h3>
        <dl className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-2 mt-3 text-xs">
          <div>
            <dt className="text-gray-500">run_id</dt>
            <dd className="font-mono text-gray-300 break-all">{sweep.run_id}</dd>
          </div>
          <div>
            <dt className="text-gray-500">sweep_key</dt>
            <dd className="font-mono text-gray-300 break-all">{sweep.sweep_key ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-gray-500">git commit</dt>
            <dd className="font-mono text-gray-300 break-all">{sweep.git_commit ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-gray-500">engine version</dt>
            <dd className="font-mono text-gray-300 break-all">{sweep.engine_version ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-gray-500">base config hash</dt>
            <dd className="font-mono text-gray-300 break-all">{baseConfigHash ?? '—'}</dd>
          </div>
          <div>
            <dt className="text-gray-500">starting cash</dt>
            <dd className="text-gray-300">
              {report.starting_cash != null ? `$${fmtNumber(report.starting_cash)}` : '—'}
            </dd>
          </div>
          <div>
            <dt className="text-gray-500">wall</dt>
            <dd className="text-gray-300">
              {report.timing?.wall_seconds != null ? `${report.timing.wall_seconds.toFixed(1)}s` : '—'}
            </dd>
          </div>
          <div>
            <dt
              className="text-gray-500"
              title="Network round-trips to the vendor. A bar served from the cache is a cache hit, not a fetch."
            >
              provider fetches
            </dt>
            <dd className="text-gray-300">
              {report.provider_calls?.fetches ?? '—'}
              <span className="text-gray-500"> · {report.provider_calls?.bar_cache_hits ?? '—'} cache hits</span>
            </dd>
          </div>
        </dl>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-gray-500">
              <tr>
                <th className="text-left py-1">arm</th>
                <th
                  className="text-left py-1"
                  title="Identity of the ARM. config_hash cannot separate two arms that differ outside its nine strategy keys."
                >
                  scenario_hash
                </th>
                <th className="text-left py-1">config_hash</th>
                <th className="text-left py-1">overrides</th>
              </tr>
            </thead>
            <tbody>
              {report.scenarios.map((s) => {
                const configHash = report.scenario_config_hashes?.[s] ?? null;
                // An equality check against base's hash, NOT a recomputation.
                // An arm whose config hashes to base's changed nothing the hash
                // covers — worth saying out loud, because it will read as base.
                const sameAsBase = s !== 'base' && !!configHash && configHash === baseConfigHash;
                return (
                  <tr key={s} className="border-t border-gray-700/50">
                    <td className="py-1 font-mono text-blue-300 pr-3 whitespace-nowrap">
                      {s}
                      {isHaircutOnly(report, s) && (
                        <span className="text-amber-400/80 ml-2 font-sans">fill-model sensitivity</span>
                      )}
                    </td>
                    <td className="py-1 font-mono text-gray-400 pr-3">
                      {report.scenario_hashes?.[s] ?? '—'}
                    </td>
                    <td className="py-1 font-mono text-gray-400 pr-3 whitespace-nowrap">
                      {configHash ?? '—'}
                      {sameAsBase && (
                        <span
                          className="text-gray-500 ml-2 font-sans"
                          title="This arm's config_hash equals base's: it changed nothing the hash covers. Either the difference lives outside the nine hashed keys (a fill_haircut does), or the override did not bind."
                        >
                          = base
                        </span>
                      )}
                    </td>
                    <td className="py-1 font-mono text-gray-400">
                      {JSON.stringify(report.scenario_overrides?.[s] ?? {})}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* ---- The bias footer, byte for byte. ---- */}
      <div className="rounded-lg border border-gray-700 bg-gray-900/60 p-4" data-testid="bias-footer">
        <h3 className="text-sm font-semibold text-gray-300">How far to trust these numbers</h3>
        <Verbatim text={report.cross_scenario_caveat} className="text-xs text-gray-400 mt-3" />
        <Verbatim text={report.rejection_tally_caveat} className="text-xs text-gray-400 mt-3" />
        <ul className="mt-3 space-y-2">
          {report.known_biases.map((bias) => (
            <li key={bias.title}>
              <p className="text-xs font-semibold text-gray-300">{bias.title}</p>
              <Verbatim text={bias.detail} className="text-xs text-gray-500" />
            </li>
          ))}
        </ul>
        <p className="text-xs text-gray-500 mt-3">
          A cell is <span className="font-mono text-gray-300">insuf</span> when the window contained no
          completed cycle, <span className="font-mono text-amber-300">low-act N%</span> when a position
          was held on under {Math.round((report.min_days_in_position ?? 0.25) * 100)}% of decision days,
          and <span className="font-mono text-purple-300">unknown</span> when the stored row carries no
          state flag at all. None of the three is a small return: all are excluded from every median
          and every Δ on this page.
        </p>
      </div>
    </section>
  );
}
