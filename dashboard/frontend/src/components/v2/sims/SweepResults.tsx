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
// Everything narrative here — the in-sample banner, the holdout semantics, the
// bias footer, the cross-scenario and rejection-tally caveats — is printed
// VERBATIM from the API payload. These are the runner's own words about how far
// its numbers can be trusted; paraphrasing them in a second place is how the two
// copies drift and the weaker one gets quoted.

import type { SweepReport, SweepRow } from '../../../types/v2';
import { fmtNumber, cls } from '../../../utils/format';
import {
  indexRows,
  lookupCell,
  pctOrDash,
  renderCell,
  summarise,
  splitsOf,
} from './resultCells';

interface Props {
  sweep: SweepRow;
  report: SweepReport;
}

const SPLIT_LABEL: Record<string, string> = { fit: 'Fit', holdout: 'Holdout', all: 'Full window' };
const splitLabel = (s: string) => SPLIT_LABEL[s] ?? s;

/**
 * Markdown-ish blockquote text (the banner ships as `> ## …` lines) rendered as
 * plain text with the quote markers stripped. Deliberately NOT a markdown
 * renderer: no dependency, and no chance of an upstream string being swallowed
 * by a parser that does not like it.
 */
function Prose({ text, className }: { text: string; className?: string }) {
  const stripped = text
    .split('\n')
    .map((line) => line.replace(/^>\s?/, '').replace(/^#+\s*/, ''))
    .join('\n')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .trim();
  return <p className={cls('whitespace-pre-wrap', className)}>{stripped}</p>;
}

export default function SweepResults({ sweep, report }: Props) {
  const splits = splitsOf(report);
  const index = indexRows(report.rows);
  const hasHoldout = splits.includes('holdout');

  const download = () => {
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
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
      {/* The headline caveat, and the case that needs it most is the default. */}
      {report.in_sample_only && report.in_sample_banner && (
        <div
          data-testid="in-sample-banner"
          className="rounded-lg border border-yellow-700/70 bg-yellow-950/40 p-4"
        >
          <h3 className="text-sm font-semibold text-yellow-300">
            ⚠ IN-SAMPLE ONLY — this ranking has not been validated
          </h3>
          <Prose text={report.in_sample_banner} className="text-xs text-yellow-200/90 mt-2" />
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
                    <th className="text-right px-3 py-2" title="Cells carrying a number worth ranking / no completed cycle / held on too few days / errored">
                      measured · insuf · low-act · err
                    </th>
                    <th className="text-right px-3 py-2" title="Median annualised return over the MEASURED cells only">
                      median
                    </th>
                    <th className="text-right px-4 py-2" title="Median of the per-symbol deltas over the common measured subset">
                      Δ vs base
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {report.scenarios.map((scenario) => {
                    const summary = summarise(report.rows, scenario, split);
                    const delta = report.delta_vs_base?.[split]?.[scenario];
                    const isBase = scenario === 'base';
                    return (
                      <tr key={scenario} className="border-t border-gray-700/60">
                        <td className="px-4 py-2 font-mono text-blue-300 whitespace-nowrap">
                          {scenario}
                          {report.scenario_fill_haircuts?.[scenario] != null && (
                            <span className="text-gray-500 text-xs ml-2">
                              haircut {report.scenario_fill_haircuts[scenario]}
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
                        <td className="px-3 py-2 text-right text-xs whitespace-nowrap">
                          <span className="text-gray-200">{summary.measured}</span>
                          <span className="text-gray-600"> · </span>
                          <span className={summary.insufficient ? 'text-gray-300' : 'text-gray-600'}>
                            {summary.insufficient}
                          </span>
                          <span className="text-gray-600"> · </span>
                          <span className={summary.lowActivity ? 'text-amber-300' : 'text-gray-600'}>
                            {summary.lowActivity}
                          </span>
                          <span className="text-gray-600"> · </span>
                          <span className={summary.errored ? 'text-red-300' : 'text-gray-600'}>
                            {summary.errored}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-right text-gray-200">
                          {summary.measured === 0 ? (
                            <span
                              className="text-xs text-gray-500"
                              title="Nothing was measured in this row, so there is no median to take."
                            >
                              no measured cell
                            </span>
                          ) : (
                            pctOrDash(summary.median)
                          )}
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
                    title="Symbols where the arm's sign vs base is the SAME in both windows, over the symbols comparable in both. This is the column to act on."
                  >
                    sign agreement
                  </th>
                </tr>
              </thead>
              <tbody>
                {report.scenarios.map((scenario) => {
                  const fit = summarise(report.rows, scenario, 'fit');
                  const hold = summarise(report.rows, scenario, 'holdout');
                  const dFit = report.delta_vs_base?.fit?.[scenario];
                  const dHold = report.delta_vs_base?.holdout?.[scenario];
                  const agree = report.sign_agreement?.[scenario];
                  const isBase = scenario === 'base';
                  return (
                    <tr key={scenario} className="border-t border-gray-700/60">
                      <td className="px-4 py-2 font-mono text-blue-300">{scenario}</td>
                      <td className="px-3 py-2 text-right text-gray-200">
                        {fit.measured === 0 ? <span className="text-xs text-gray-500">—</span> : pctOrDash(fit.median)}
                      </td>
                      <td className="px-3 py-2 text-right text-gray-200">
                        {hold.measured === 0 ? <span className="text-xs text-gray-500">—</span> : pctOrDash(hold.median)}
                      </td>
                      <td className="px-3 py-2 text-right text-gray-200">
                        {isBase ? '—' : `${pctOrDash(dFit?.median ?? null)} (n=${dFit?.symbols ?? 0})`}
                      </td>
                      <td className="px-3 py-2 text-right text-gray-200">
                        {isBase ? '—' : `${pctOrDash(dHold?.median ?? null)} (n=${dHold?.symbols ?? 0})`}
                      </td>
                      <td className="px-4 py-2 text-right">
                        {isBase || !agree || agree.comparable === 0 ? (
                          <span className="text-gray-600 text-xs">—</span>
                        ) : (
                          <span
                            className={cls(
                              'font-mono',
                              agree.agreeing === agree.comparable ? 'text-green-400' : 'text-yellow-300',
                            )}
                          >
                            {agree.agreeing}/{agree.comparable}
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {report.holdout_semantics && (
            <div className="px-4 py-3 border-t border-gray-700">
              <Prose text={report.holdout_semantics} className="text-xs text-gray-400" />
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
            <dd className="font-mono text-gray-300 break-all">{report.base_config_hash ?? '—'}</dd>
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
            <dt className="text-gray-500" title="Network round-trips to the vendor. A bar served from the cache is a cache hit, not a fetch.">
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
                <th className="text-left py-1" title="Identity of the ARM. config_hash cannot separate two arms that differ outside its nine strategy keys.">
                  scenario_hash
                </th>
                <th className="text-left py-1">config_hash</th>
                <th className="text-left py-1">overrides</th>
              </tr>
            </thead>
            <tbody>
              {report.scenarios.map((s) => (
                <tr key={s} className="border-t border-gray-700/50">
                  <td className="py-1 font-mono text-blue-300 pr-3">{s}</td>
                  <td className="py-1 font-mono text-gray-400 pr-3">{report.scenario_hashes?.[s] ?? '—'}</td>
                  <td className="py-1 font-mono text-gray-400 pr-3">{report.scenario_config_hashes?.[s] ?? '—'}</td>
                  <td className="py-1 font-mono text-gray-400">
                    {JSON.stringify(report.scenario_overrides?.[s] ?? {})}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* ---- The bias footer, verbatim. ---- */}
      <div className="rounded-lg border border-gray-700 bg-gray-900/60 p-4" data-testid="bias-footer">
        <h3 className="text-sm font-semibold text-gray-300">How far to trust these numbers</h3>
        <Prose text={report.cross_scenario_caveat} className="text-xs text-gray-400 mt-3" />
        <Prose text={report.rejection_tally_caveat} className="text-xs text-gray-400 mt-3" />
        <ul className="mt-3 space-y-2">
          {report.known_biases.map((bias) => (
            <li key={bias.title}>
              <p className="text-xs font-semibold text-gray-300">{bias.title}</p>
              <Prose text={bias.detail} className="text-xs text-gray-500" />
            </li>
          ))}
        </ul>
        <p className="text-xs text-gray-500 mt-3">
          A cell is <span className="font-mono text-gray-300">insuf</span> when the window contained no
          completed cycle, and <span className="font-mono text-amber-300">low-act N%</span> when a position
          was held on under {Math.round((report.min_days_in_position ?? 0.25) * 100)}% of decision days.
          Neither is a small return: both are excluded from every median and every Δ on this page.
        </p>
      </div>
    </section>
  );
}
