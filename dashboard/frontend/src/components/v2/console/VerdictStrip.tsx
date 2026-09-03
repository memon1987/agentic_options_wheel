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
import type { SimArtifact, SweepReport, SweepResultRow } from '../../../types/v2';
import { pctOrDash, renderCell } from '../sims/resultCells';
import { fmtCurrency, fmtCurrencyDetail, fmtNumber, fmtPercent } from '../../../utils/format';
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

/**
 * A fraction that has NO sign to report — a share of something, not a return.
 *
 * `pctOrDash` prefixes `+` because the numbers it was written for are signed
 * returns where the sign is the headline. "+82% of decision days", "+100% win
 * rate" and "+24.4% deployed" borrow that headline for a quantity that cannot
 * be negative, which reads as a gain (review round 1, F9).
 */
const shareOrDash = (value: number | null | undefined, digits = 0): string =>
  value === null || value === undefined || Number.isNaN(value) ? '—' : fmtPercent(value, digits);

/**
 * What the engine's benchmark actually is, with the number rather than the
 * variable name (review round 1, F9).
 *
 * `$capital_base` on screen is a literal that only the person who wrote the
 * code can read. And "full investment" is not quite what the engine does: it
 * buys WHOLE shares at the first close, so the remainder stays idle cash — 473
 * shares and about $31 idle on the captured fixture.
 */
function benchmarkTitle(digest: ArtifactDigest | null): string {
  const base = digest?.capitalBase ?? null;
  const amount = base === null ? 'the run’s capital base' : fmtCurrency(base);
  return (
    `Whole shares bought with ${amount} at the first close and held to the last, ` +
    'dividends included. Whole shares only — the remainder stays idle cash, so this is ' +
    'not quite a full investment.'
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
  /** The cell's stored object — the fill label's first and best source (F4). */
  artifact: SimArtifact | null;
}

/**
 * The scenario-level sign-agreement count, labelled for what it is.
 *
 * `report.sign_agreement[scenario]` counts SYMBOLS whose fit/holdout pair moves
 * the same way as base's — it is not a statement about this symbol, and the two
 * are shown side by side precisely so one is never read as the other.
 */
function scenarioAgreement(report: SweepReport, scenario: string): string {
  // On BASE the count is base against itself, so it is 1/1 across every symbol
  // by construction (review round 1, F9). Printed as a figure it looks like
  // evidence of stability and is evidence of nothing; the arm it would qualify
  // is the comparator.
  if (scenario === 'base') return 'n/a — base is the comparator';
  const entry = report.sign_agreement?.[scenario];
  if (!entry) return '—';
  const { agreeing, comparable } = entry;
  if (comparable === null || comparable === undefined || comparable === 0) return '—';
  return `${agreeing ?? 0}/${comparable} across ${comparable} symbols`;
}

/**
 * The effective fill assumption for THIS cell, in the order the sources are
 * actually authoritative (review round 1, F4).
 *
 *   1. **The cell artifact's own `provenance.fill`.** This is the fill the
 *      ledger on screen was replayed under — the truth about the object being
 *      read, not about the run in general.
 *   2. `forecast.by_scenario[arm].symbols[symbol].fill`. Present only for a
 *      symbol the forecast could include: an arm that is `insufficient` in
 *      either window is EXCLUDED from the forecast entirely, which is why
 *      reading this first printed a dash for exactly the arms whose fill an
 *      operator is most likely to be interrogating.
 *   3. The spec's declared haircut for the arm.
 *
 * Two rules on top of the order:
 *
 *   * **The basis is never invented.** The old code defaulted it to `'mid'`,
 *     which is the engine's current default and therefore right until the day
 *     it is not — a label that is right by coincidence is a label that lies
 *     silently. No source ⇒ `—`.
 *   * **An undeclared haircut is "engine default", never `—`.** A spec that
 *     declares no haircut did not ask for "no haircut"; it accepted the
 *     engine's. `—` reads as "unknown" and sends the operator looking for a
 *     missing field.
 */
export function fillLabel(
  report: SweepReport,
  scenario: string,
  symbol: string,
  artifact: SimArtifact | null,
): string {
  const fromArtifact = artifact?.provenance.fill ?? null;
  const fromForecast = report.forecast?.by_scenario?.[scenario]?.symbols?.[symbol]?.fill ?? null;
  const declared = report.scenario_fill_haircuts?.[scenario];
  const undeclared = declared === null || declared === undefined;

  const basis = fromArtifact?.basis ?? fromForecast?.basis ?? null;
  const number = (v: unknown): number | null =>
    typeof v === 'number' && Number.isFinite(v) ? v : null;
  const haircut =
    number(fromArtifact?.fill_haircut) ??
    number(fromForecast?.fill_haircut) ??
    (undeclared ? null : number(declared));

  // The engine's own default is what applied whenever the spec declared
  // nothing; the forecast says so outright when it can.
  const engineDefault = fromForecast?.is_engine_default === true || undeclared;
  const haircutText =
    haircut === null
      ? undeclared
        ? 'engine default'
        : '—'
      : `${(haircut * 100).toFixed(0)}%${engineDefault ? ' (engine default)' : ''}`;
  return `${basis ?? '—'} · haircut ${haircutText}`;
}

export default function VerdictStrip({
  row,
  report,
  scenario,
  symbol,
  split,
  digest,
  digestAbsence,
  artifact,
}: VerdictStripProps) {
  const rendered = renderCell(row);
  const measured = rendered.kind === 'return';
  // Straight off the artifact, for the hover only — never rendered as a tile
  // and never compared with the row. `reconcile` is where it is pinned against
  // the engine's own `annualized_return_on_collateral`.
  const roc =
    digest && digest.reconcile.avgCollateral !== null
      ? {
          avgCollateral: digest.reconcile.avgCollateral,
          days: digest.deploymentSeries.filter((d) => d.reserved > 0).length,
          decisionDays: digest.deployment.days,
        }
      : null;
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
        {/* Amber, like the grid's own fill-model label (`SweepResults.tsx`): a
            result that depends on an assumed fill price is qualified in the
            same colour wherever it is shown, so the two cannot be read as
            different kinds of statement. */}
        <span
          className="text-xs text-amber-400/80"
          data-testid="fill-label"
          title="The fill this cell's ledger was replayed under: the stored artifact's own stamp where there is one, then the forecast's, then what the spec declared. `engine default` means the spec declared no haircut and the engine applied its own."
        >
          fill: {fillLabel(report, scenario, symbol, artifact)}
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
              title="The engine's `annualized_return` (fitness.py:143-152): total_return × 365 ÷ the CALENDAR days between the first and last decision day — not ÷ decision days. On this run that is 365/273, not 365/189: the two differ by 44%."
            />
            <Tile
              testId="tile-roc"
              label="Ann. return on collateral"
              value={pctOrDash(row?.annualized_return_on_collateral)}
              sub="the engine's headline RoC"
              tone={toneOf(row?.annualized_return_on_collateral)}
              title={
                'The engine\'s number (fitness.py:240-259), not a ratio computed here: ' +
                'TOTAL P&L ÷ the mean reserved PUT collateral over the days collateral was ' +
                'above zero, × 365 ÷ calendar days. ' +
                (roc === null
                  ? ''
                  : `On this cell: ${roc.days} of ${roc.decisionDays} days carried collateral, averaging ${fmtCurrencyDetail(roc.avgCollateral)}. `) +
                'Shares held after assignment are NOT in that denominator even though the ' +
                'cash that bought them is committed, so the ratio reads high on an assigned ' +
                'cycle (FC-103 owns the fix). The deployment tile below uses a DIFFERENT ' +
                'denominator — dollar-weighted over ALL decision days — so the two are not ' +
                'two readings of one number.'
              }
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
              title={benchmarkTitle(digest)}
            />
          </>
        )}
        <Tile
          testId="tile-option-pnl"
          label="Net option P&L"
          value={fmtCurrency(row?.option_pnl)}
          sub="engine, cash basis"
          // Sign colour only on a MEASURED cell (review round 1, F9). A green
          // +$40 of premium on an `insuf` or `low-act` cell reads as a verdict
          // on a window whose whole point is that it produced none.
          tone={measured ? toneOf(row?.option_pnl) : 'plain'}
        />
        <Tile
          testId="tile-win-rate"
          label="Win rate"
          value={shareOrDash(row?.win_rate)}
          sub={`assignment ${shareOrDash(row?.assignment_rate)}`}
          title="The engine's definition (fitness.py:200): the share of completed cycles with a positive total P&L."
        />
        <Tile
          testId="tile-drawdown"
          label="Max drawdown"
          value={pctOrDash(row?.max_drawdown)}
          sub="peak-to-trough on equity"
          tone={measured ? toneOf(row?.max_drawdown) : 'plain'}
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
          value={shareOrDash(row?.days_in_position_fraction)}
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
                // Two decimals (review round 1, F9): "$2" printed beside
                // "$0.040/contract" is not a rounding, it is a contradiction.
                value={fmtCurrencyDetail(digest.fees)}
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
              value={fmtCurrencyDetail(digest.fees)}
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
              value={shareOrDash(digest.deployment.ratio, 1)}
              sub={
                digest.deployment.unresolvedReason ??
                `from daily state, ${digest.deployment.basis}, over ${digest.deployment.days} decision days`
              }
              tone="digest"
              title="Mean over ALL decision days of (reserved collateral + share value) / capital base. Averaging only the days with a position answers a different question and gives roughly twice the number — which is the denominator the RoC tile above uses."
            />
          </div>
        )}
      </div>
    </section>
  );
}
