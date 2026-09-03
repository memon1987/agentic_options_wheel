// FC-096 Phase E PR-2: the verdict strip — component 1 of the console.
//
// Every number with a verdict attached to it is the ENGINE'S, read straight off
// the grid cell that `shape_results` served. The strip does no arithmetic on
// any of them: no ratio, no delta, no annualisation, no re-derivation of a
// number the row already carries. That is the FC-060 rule, and it is what makes
// the strip and the grid above it impossible to disagree.
//
// Three tiles are the exception and they are fenced off in their own row:
// gross premium, fees and dollar-weighted deployment come from the client
// digest over the stored artifact (§D-4), because the engine persists no column
// for them. They are grey, never coloured by sign, labelled with their source,
// suppressed entirely when the artifact carries no capital base, and they never
// appear in a Δ or a comparison.
//
// The cell-state partition is the other load-bearing rule. On an `insuf`,
// `low-act`, `err` or `unknown` cell EVERY return, RoC, Δ and benchmark tile is
// replaced by the state badge's own title text. `annualized_return` is present
// and equal to 0.0 on an insufficient cell — rendering it would put "+0.0%" on
// screen for a window that measured nothing.

import type { ReactNode } from 'react';
import type { SweepReport, SweepResultRow } from '../../../types/v2';
import { pctOrDash, renderCell } from '../sims/resultCells';
import { fmtCurrency, fmtNumber } from '../../../utils/format';
import type { ArtifactDigest } from './artifactDigest';

/** One tile. `tone` is the only place a sign becomes a colour. */
function Tile({
  label,
  value,
  sub,
  title,
  tone = 'plain',
  testId,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  title?: string;
  tone?: 'plain' | 'signed-positive' | 'signed-negative' | 'digest';
  testId?: string;
}) {
  const valueClass =
    tone === 'signed-positive'
      ? 'text-green-400'
      : tone === 'signed-negative'
        ? 'text-red-400'
        : tone === 'digest'
          ? 'text-gray-300'
          : 'text-white';
  return (
    <div
      data-testid={testId}
      title={title}
      className={`rounded border px-3 py-2 ${
        tone === 'digest' ? 'border-gray-700 bg-gray-800/40' : 'border-gray-700 bg-gray-800'
      }`}
    >
      <div className="text-[11px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`text-lg font-semibold ${valueClass}`}>{value}</div>
      {sub && <div className="text-[11px] text-gray-500 mt-0.5">{sub}</div>}
    </div>
  );
}

/** Sign → tone, for the row's own numbers only. Null is never coloured. */
const toneOf = (n: number | null | undefined): 'plain' | 'signed-positive' | 'signed-negative' =>
  n === null || n === undefined || n === 0
    ? 'plain'
    : n > 0
      ? 'signed-positive'
      : 'signed-negative';

export interface VerdictStripProps {
  row: SweepResultRow | null;
  report: SweepReport;
  scenario: string;
  symbol: string;
  split: string;
  /** Non-null only when the artifact loaded AND parsed. */
  digest: ArtifactDigest | null;
  /** Why there is no digest, in the endpoint's words. Rendered on the tiles. */
  digestAbsence: string | null;
}

/**
 * The scenario-level sign-agreement count, labelled for what it is.
 *
 * `report.sign_agreement[scenario]` counts SYMBOLS whose fit/holdout pair moves
 * the same way as base's — it is not a statement about this symbol, and the two
 * are shown side by side precisely so one is never read as the other.
 */
function scenarioAgreement(report: SweepReport, scenario: string): string {
  const entry = report.sign_agreement?.[scenario];
  if (!entry) return '—';
  const { agreeing, comparable } = entry;
  if (comparable === null || comparable === undefined || comparable === 0) return '—';
  return `${agreeing ?? 0}/${comparable} across ${comparable} symbols`;
}

/** The effective fill assumption — read from `forecast.fill` (PR-1 §Execution). */
function fillLabel(report: SweepReport, scenario: string, symbol: string): string {
  const fill = report.forecast?.by_scenario?.[scenario]?.symbols?.[symbol]?.fill;
  const declared = report.scenario_fill_haircuts?.[scenario];
  const basis = fill?.basis ?? 'mid';
  const haircut = fill?.fill_haircut ?? declared ?? null;
  const pct = haircut === null ? '—' : `${(haircut * 100).toFixed(0)}%`;
  return `${basis} · haircut ${pct}${fill?.is_engine_default ? ' (engine default)' : ''}`;
}

export default function VerdictStrip({
  row,
  report,
  scenario,
  symbol,
  split,
  digest,
  digestAbsence,
}: VerdictStripProps) {
  const rendered = renderCell(row);
  const measured = rendered.kind === 'return';
  const delta = row?.delta_vs_base_annualized ?? null;
  const isBase = scenario === 'base';

  return (
    <section data-testid="verdict-strip" className="space-y-3">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-base font-semibold text-white">
          <span className="font-mono">{scenario}</span> · <span className="font-mono">{symbol}</span>{' '}
          · <span className="font-mono">{split}</span>
        </h3>
        <span
          data-testid="cell-state-badge"
          title={rendered.title}
          className={`px-2 py-0.5 rounded text-xs ${rendered.className}`}
        >
          {rendered.text}
        </span>
        <span className="text-xs text-gray-500" data-testid="fill-label">
          fill: {fillLabel(report, scenario, symbol)}
        </span>
      </header>

      {!measured && (
        <p data-testid="unmeasured-notice" className="text-sm text-gray-400">
          This cell is <strong>{rendered.text}</strong>, so it carries no return, no return on
          collateral, no Δ against base and no benchmark comparison. {rendered.title}
        </p>
      )}

      {/* --- the engine's numbers ------------------------------------------ */}
      <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-6 gap-2">
        {measured && (
          <>
            <Tile
              testId="tile-annualized"
              label="Annualised return"
              value={pctOrDash(row?.annualized_return)}
              sub={`total ${pctOrDash(row?.total_return)}`}
              tone={toneOf(row?.annualized_return)}
              title="The engine's `annualized_return`: total_return × 365 / decision-day span (fitness.py:148)."
            />
            <Tile
              testId="tile-roc"
              label="Ann. return on collateral"
              value={pctOrDash(row?.annualized_return_on_collateral)}
              sub="the engine's headline RoC"
              tone={toneOf(row?.annualized_return_on_collateral)}
              title="Return annualised over capital actually put at risk, not the whole account. The engine's number, not a ratio computed here."
            />
            {!isBase && delta !== null && (
              <Tile
                testId="tile-delta"
                label="Δ vs base"
                value={pctOrDash(delta)}
                sub="served per cell"
                tone={toneOf(delta)}
                title="`delta_vs_base_annualized`: this cell's annualised return minus the base cell's, computed by the server over the same symbol and split."
              />
            )}
            <Tile
              testId="tile-benchmark"
              label="Buy & hold"
              value={pctOrDash(row?.benchmark_return)}
              sub={`excess ${pctOrDash(row?.excess_return)}`}
              tone={toneOf(row?.excess_return)}
              title="Full investment of $capital_base at the first close, held to the last, dividends included."
            />
          </>
        )}
        <Tile
          testId="tile-option-pnl"
          label="Net option P&L"
          value={fmtCurrency(row?.option_pnl)}
          sub="engine, cash basis"
          tone={toneOf(row?.option_pnl)}
        />
        <Tile
          testId="tile-win-rate"
          label="Win rate"
          value={row?.win_rate === null || row?.win_rate === undefined ? '—' : pctOrDash(row.win_rate, 0)}
          sub={`assignment ${row?.assignment_rate === null || row?.assignment_rate === undefined ? '—' : pctOrDash(row.assignment_rate, 0)}`}
          title="The engine's definition (fitness.py:200): the share of completed cycles with a positive total P&L."
        />
        <Tile
          testId="tile-drawdown"
          label="Max drawdown"
          value={pctOrDash(row?.max_drawdown)}
          sub="peak-to-trough on equity"
          tone={toneOf(row?.max_drawdown)}
        />
        <Tile
          testId="tile-cycles"
          label="Cycles"
          value={`${fmtNumber(row?.cycles_completed)} done`}
          sub={`${fmtNumber(row?.cycles_open)} open · ${fmtNumber(row?.puts_sold)} puts / ${fmtNumber(row?.calls_sold)} calls`}
        />
        <Tile
          testId="tile-time-in-position"
          label="Time in position"
          value={
            row?.days_in_position_fraction === null || row?.days_in_position_fraction === undefined
              ? '—'
              : pctOrDash(row.days_in_position_fraction, 0)
          }
          sub={`${fmtNumber(row?.decision_days)} decision days`}
        />
        <Tile
          testId="tile-sign-agreement"
          label="Sign agreement"
          value={row?.sign_agrees === null || row?.sign_agrees === undefined ? '—' : row.sign_agrees ? 'agrees' : 'disagrees'}
          sub={`this cell · arm: ${scenarioAgreement(report, scenario)}`}
          title="Per-cell: does this symbol's fit/holdout pair move the same way as base's? The arm figure beside it is a COUNT ACROSS SYMBOLS, not this symbol's answer."
        />
      </div>

      {/* --- the digest, fenced off ---------------------------------------- */}
      <div data-testid="digest-tiles" className="space-y-1">
        <p className="text-[11px] uppercase tracking-wide text-gray-500">
          From this cell's stored artifact — display only, never ranked or compared
        </p>
        {digest === null ? (
          <p data-testid="digest-absent" className="text-sm text-gray-400">
            {digestAbsence ?? 'Not stored for this cell.'}
          </p>
        ) : digest.ratiosSuppressed ? (
          <>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
              <Tile
                testId="tile-gross-premium"
                label="Gross premium"
                value={fmtCurrency(digest.grossPremium)}
                sub="from ledger, before fees"
                tone="digest"
              />
              <Tile
                testId="tile-fees"
                label="Fees"
                value={fmtCurrency(digest.fees)}
                sub={
                  digest.feeRatePerContract === null
                    ? 'from ledger'
                    : `regulatory pass-through @ $${digest.feeRatePerContract.toFixed(3)}/contract (engine constant)`
                }
                tone="digest"
              />
            </div>
            <p data-testid="ratios-suppressed" className="text-sm text-amber-400">
              {digest.suppressionReason}
            </p>
          </>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Tile
              testId="tile-gross-premium"
              label="Gross premium"
              value={fmtCurrency(digest.grossPremium)}
              sub="from ledger, before fees"
              tone="digest"
            />
            <Tile
              testId="tile-fees"
              label="Fees"
              value={fmtCurrency(digest.fees)}
              sub={
                digest.feeRatePerContract === null
                  ? 'from ledger'
                  : `regulatory pass-through @ $${digest.feeRatePerContract.toFixed(3)}/contract (engine constant)`
              }
              tone="digest"
            />
            <Tile
              testId="tile-deployment"
              label="Deployment (dollar-weighted)"
              value={digest.deployment.ratio === null ? '—' : pctOrDash(digest.deployment.ratio, 1)}
              sub={`from daily state, ${digest.deployment.basis}, over ${digest.deployment.days} decision days`}
              tone="digest"
              title="Mean over ALL decision days of (reserved collateral + share value) / capital base. Averaging only the days with a position answers a different question and gives roughly twice the number."
            />
          </div>
        )}
      </div>
    </section>
  );
}
