// FC-096 Phase E PR-2: the console shell.
//
// Composition, top to bottom, is FIXED by the plan and the order is an argument
// rather than a layout preference: the in-sample banner precedes everything it
// qualifies, the strip's engine numbers precede any picture of them, and the
// provenance footer closes the page under the evidence it describes. PR-3 fills
// the placeholders between the strip and the footer with the five charts and
// the four tables; the shell renders their absence states now so an operator
// on PR-2 sees "not built yet" rather than a hole.
//
// This component fetches: one cell artifact, one bars sidecar. It does NOT
// fetch the base cell's artifact — that is PR-3's overlay, and the strip's "vs
// base" number is served per cell by PR-1 rather than derived from two objects.

import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { comparePath } from './compareAlignment';
import type { SimBars, SweepAllowlist, SweepReport, SweepRow, SweepSpec } from '../../../types/v2';
import { useArtifact } from '../../../hooks/useArtifact';
import { useBars } from '../../../hooks/useBars';
import { indexRows, lookupCell, renderCell } from '../sims/resultCells';
import { computeDigest } from './artifactDigest';
import { artifactStrategy } from './normaliseArtifact';
import { equityOverlay } from './series';
import VerdictStrip from './VerdictStrip';
import ProvenanceFooter from './ProvenanceFooter';
import TweakBar from './TweakBar';
import type { SimRefusal } from './TweakBar';
import PriceChart from './PriceChart';
import PortfolioEquityView from './PortfolioEquityView';
import EquityChart from './EquityChart';
import PremiumCharts from './PremiumCharts';
import DrawdownChart from './DrawdownChart';
import DeploymentChart from './DeploymentChart';
import ForecastPanel from './ForecastPanel';
import LedgerTable from './LedgerTable';
import SimCycleTable from './SimCycleTable';
import RejectionPanel from './RejectionPanel';
import type { ExportContext } from './ledgerCsv';
import { IN_SAMPLE_FALLBACK } from '../sims/SweepResults';

/** The name the runner reserves for the implicit base arm. */
export const BASE_SCENARIO = 'base';

/** The portfolio tab's sentinel — never a symbol, because symbols are tickers. */
export const PORTFOLIO_TAB = '__portfolio__';

/** `base_config_json` is a JSON STRING on the row; a bad one is not fatal. */
export function parseBaseEffective(raw: string | null | undefined): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object') return null;
    const effective = (parsed as Record<string, unknown>).effective;
    return effective && typeof effective === 'object'
      ? (effective as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** `spec.strategy` off the run's own spec, when it carries one (Phase C). */
export function specStrategy(sweep: SweepRow): string | null {
  if (!sweep.spec_json) return null;
  try {
    const spec = JSON.parse(sweep.spec_json) as Record<string, unknown>;
    return typeof spec.strategy === 'string' && spec.strategy ? spec.strategy : null;
  } catch {
    return null;
  }
}

export interface ConsoleProps {
  sweep: SweepRow;
  report: SweepReport;
  scenario: string;
  symbol: string;
  split: string;
  /**
   * The `deduplicated` run this screen was auto-opened FROM (review round 1,
   * F5). The page follows `deduplicated_to` rather than parking the operator on
   * a run that stored nothing, and this is how the destination says so.
   */
  dedupFrom?: string | null;
  /**
   * Selecting a symbol TAB is selecting a cell, so it goes through the URL like
   * every other user selection (decision 4) rather than into local state that
   * the address bar knows nothing about. The tabs are inert without it.
   */
  onSelectSymbol?: (symbol: string) => void;
  /** Open any cell of THIS run — the tweak bar's already-asked-arm link. */
  onSelectCell?: (cell: { scenario: string; symbol: string; split: string }) => void;
  /**
   * PR-4. The allowlist types the tweak bar's controls; `null` while it loads
   * and an error string when it could not be read. Both are passed down rather
   * than fetched here: `Simulations` already holds one `useSweepAllowlist`, and
   * a second call would be a second poll of a payload that is static per deploy.
   */
  allowlist?: SweepAllowlist | null;
  allowlistError?: string | null;
  /**
   * The tweak submit, OWNED BY THE PAGE (review round 1, R5) — a request that
   * outlives this subtree cannot have its state in it. Absent ⇒ the bar is not
   * rendered at all, which keeps `Console` usable from a screen with no router.
   */
  onTweakSubmit?: (spec: SweepSpec, armName: string) => void;
  /** The page's in-flight flag and refusal, both keyed to THIS run. */
  tweakSubmitting?: boolean;
  /** The run whose submit is in flight — named on every OTHER run's bar. */
  tweakSubmittingFrom?: string | null;
  tweakOutcome?: SimRefusal | null;
  onClearTweakOutcome?: () => void;
}

export default function Console({
  sweep,
  report,
  scenario,
  symbol,
  split,
  dedupFrom = null,
  onSelectSymbol,
  onSelectCell,
  allowlist = null,
  allowlistError = null,
  onTweakSubmit,
  tweakSubmitting = false,
  tweakSubmittingFrom = null,
  tweakOutcome = null,
  onClearTweakOutcome,
}: ConsoleProps) {
  const cell = useMemo(() => ({ scenario, symbol, split }), [scenario, symbol, split]);
  const artifactState = useArtifact(sweep, cell);
  const barsState = useBars(sweep, symbol, split);
  // The BASE cell's artifact, for the equity overlay only. Not fetched when the
  // selected cell IS base: the overlay would be the same curve twice.
  const isBase = scenario === BASE_SCENARIO;
  const baseCell = useMemo(
    () => (isBase ? null : { scenario: BASE_SCENARIO, symbol, split }),
    [isBase, symbol, split],
  );
  const baseState = useArtifact(sweep, baseCell);
  const [tab, setTab] = useState<string | null>(null);

  // Both are JSON STRINGS on the row and both are parsed exactly once per row,
  // not once per render and not three times per render (review round 1, F9).
  const specStrategyValue = useMemo(() => specStrategy(sweep), [sweep]);
  const baseEffective = useMemo(() => parseBaseEffective(sweep.base_config_json), [sweep]);

  const row = useMemo(
    () => lookupCell(indexRows(report.rows), scenario, symbol, split) ?? null,
    [report.rows, scenario, symbol, split],
  );

  const strategy = artifactStrategy(specStrategyValue, artifactState.data);
  const digest = useMemo(
    () =>
      artifactState.data
        ? computeDigest(artifactState.data, barsState.data as SimBars | null, {
            specStrategy: specStrategyValue,
          })
        : null,
    [artifactState.data, barsState.data, specStrategyValue],
  );

  const digestAbsence =
    artifactState.absent ??
    artifactState.error ??
    (artifactState.sessionExpired ? 'Session expired — reload to sign in again.' : null) ??
    (artifactState.loading ? 'Loading this cell’s artifact…' : null);

  const overlay = useMemo(
    () => equityOverlay(artifactState.data, baseState.data, barsState.data),
    [artifactState.data, baseState.data, barsState.data],
  );

  const window = useMemo(
    () => report.windows.find((w) => w.split === split) ?? null,
    [report.windows, split],
  );

  /**
   * The fill label's source order (PR-2 amendments, F4): the CELL's own stamp
   * first, then the forecast's — which omits excluded arms and in-sample runs —
   * then the spec's declared haircut. A basis is never invented.
   */
  const fill = useMemo(() => {
    const stamped = artifactState.data?.provenance.fill ?? null;
    if (stamped) return stamped;
    const served = report.forecast?.by_scenario?.[scenario]?.symbols?.[symbol]?.fill ?? null;
    if (served) return served;
    const declared = report.scenario_fill_haircuts?.[scenario];
    return declared === undefined ? null : { basis: null, fill_haircut: declared };
  }, [artifactState.data, report.forecast, report.scenario_fill_haircuts, scenario, symbol]);

  const exportContext: ExportContext = useMemo(
    () => ({
      runId: artifactState.runId ?? sweep.run_id,
      scenario,
      symbol,
      split,
      engineIdentity: artifactState.data?.provenance.engine_identity ?? sweep.engine_identity ?? null,
      fillBasis: fill?.basis ?? null,
      fillHaircut: fill?.fill_haircut ?? null,
      inSampleOnly: !!report.in_sample_only,
      inSampleBanner: report.in_sample_banner ?? null,
      strategy,
      knownBiases: report.known_biases,
      row,
    }),
    [artifactState.runId, artifactState.data, sweep, scenario, symbol, split, fill, report, strategy, row],
  );

  // The cell's state in the partition's own words — for the panels that must
  // say WHY a stored artifact has nothing in it (review round 1, R9).
  const stateLabel = renderCell(row).kind === 'return' ? null : renderCell(row).text;

  const symbolTabClass = (active: boolean) =>
    `px-2 py-1 rounded text-xs border ${
      active
        ? 'bg-blue-950/60 border-blue-700 text-blue-200'
        : 'bg-gray-800 border-gray-700 text-gray-400 hover:text-gray-200'
    }`;
  // The symbol tabs mirror the URL, always — only the portfolio tab is local
  // state. Otherwise a symbol picked in the cell selector above would leave the
  // tab strip highlighting the previous one, and the two would disagree about
  // what is on screen.
  const activeTab = tab === PORTFOLIO_TAB ? PORTFOLIO_TAB : symbol;

  return (
    <section data-testid="sim-console" className="space-y-4">
      {/* Keyed on the FLAG alone (review round 1, F8). A payload that set
          `in_sample_only` and carried no banner string used to render nothing
          at all — a missing string silently suppressing the warning it is the
          text OF. The grid's own fallback copy is reused rather than reworded:
          two spellings of the same warning on one page invite the reader to
          look for a difference between them. */}
      {report.in_sample_only && (
        <p
          data-testid="console-in-sample-banner"
          className="rounded border border-amber-700/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-300 whitespace-pre-wrap"
        >
          {report.in_sample_banner ?? IN_SAMPLE_FALLBACK}
        </p>
      )}

      {report.artifacts_complete === false && (
        <p data-testid="artifacts-incomplete" className="text-sm text-amber-400">
          This run&rsquo;s evidence set is incomplete (storage failure, not a quiet replay): at
          least one artifact write failed, so a missing cell below is a lost object rather than a
          cell that had nothing to store.
        </p>
      )}

      {(dedupFrom || (artifactState.followedDedup && artifactState.runId)) && (
        <p data-testid="followed-dedup" className="text-sm text-gray-400">
          Run <span className="font-mono">{dedupFrom ?? sweep.run_id}</span> was deduplicated —
          nothing was replayed under its own id. The evidence below is answered by run{' '}
          <span className="font-mono">{dedupFrom ? sweep.run_id : artifactState.runId}</span>.
        </p>
      )}

      {/* PR-5 entry point. `a` is the cell on screen; `b` is pre-filled with
          THIS run's base arm at the same split, which is the comparison the
          console is already anchored to. On the base cell itself there is no
          default partner — comparing base with base is not a question — so the
          link carries `a` alone and the compare page's `b` picker stays empty.
          The page never chooses a partner on its own. */}
      <p className="text-xs">
        <Link
          data-testid="compare-link"
          className="underline text-blue-300"
          to={comparePath(
            { runId: sweep.run_id, scenario, symbol, split },
            isBase ? null : { runId: sweep.run_id, scenario: BASE_SCENARIO, symbol, split },
          )}
        >
          Compare this cell{isBase ? '…' : ' with base'}
        </Link>
      </p>

      <VerdictStrip
        row={row}
        report={report}
        scenario={scenario}
        symbol={symbol}
        split={split}
        digest={digest}
        digestAbsence={digestAbsence}
        artifact={artifactState.data}
      />

      {/* Symbol tabs, in the run's DECLARATION order, plus the portfolio view.
          Choosing a symbol is choosing a cell, so it travels through the URL;
          the portfolio tab is a view of the same cell selection and is local. */}
      <div data-testid="symbol-tabs" className="flex flex-wrap gap-1 items-center">
        {report.symbols.map((s) => (
          <button
            key={s}
            type="button"
            className={symbolTabClass(activeTab === s)}
            onClick={() => {
              setTab(null);
              if (s !== symbol) onSelectSymbol?.(s);
            }}
          >
            {s}
          </button>
        ))}
        <button
          type="button"
          data-testid="portfolio-tab"
          className={symbolTabClass(activeTab === PORTFOLIO_TAB)}
          onClick={() => setTab(PORTFOLIO_TAB)}
        >
          Portfolio
        </button>
      </div>

      {activeTab === PORTFOLIO_TAB ? (
        <PortfolioEquityView
          sweep={sweep}
          report={report}
          scenario={scenario}
          split={split}
          specStrategy={specStrategyValue}
        />
      ) : (
        <PriceChart
          symbol={symbol}
          bars={barsState.data}
          barsAbsence={barsState.absent ?? barsState.error}
          barsLoading={barsState.loading}
          artifact={artifactState.data}
          artifactAbsence={artifactState.absent ?? artifactState.error}
          window={window ? { start: window.start, end: window.end } : null}
          strategy={strategy}
        />
      )}

      <EquityChart
        overlay={overlay}
        row={row}
        isBase={isBase}
        baseAbsence={baseState.absent ?? baseState.error}
        artifactAbsence={digestAbsence}
        capitalBase={digest?.capitalBase ?? null}
        strategy={strategy}
      />

      <PremiumCharts
        digest={digest}
        absence={digestAbsence}
        strategy={strategy}
        stateLabel={stateLabel}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <DrawdownChart
          series={digest?.drawdownSeries ?? []}
          row={row}
          absence={digestAbsence}
          strategy={strategy}
        />
        <DeploymentChart
          series={digest?.deploymentSeries ?? []}
          reading={digest?.deployment ?? null}
          capitalBase={digest?.capitalBase ?? null}
          suppressionReason={digest?.suppressionReason ?? null}
          absence={digestAbsence}
          strategy={strategy}
        />
      </div>

      {/* Keyed on the cell so the horizon selector resets when the operator
          moves to another run, arm or symbol (review round 1, LOW). A 365-day
          horizon chosen on one cell silently carrying over to the next is a
          number read against the wrong window. */}
      <ForecastPanel
        key={`${sweep.run_id}:${scenario}:${symbol}`}
        report={report}
        scenario={scenario}
        symbol={symbol}
      />

      <LedgerTable
        artifact={artifactState.data}
        absence={digestAbsence}
        context={exportContext}
        stateLabel={stateLabel}
      />

      <SimCycleTable
        artifact={artifactState.data}
        absence={digestAbsence}
        row={row}
        strategy={strategy}
      />

      <RejectionPanel
        artifact={artifactState.data}
        absence={digestAbsence}
        earningsSymbolsWithoutData={report.earnings_symbols_without_data ?? []}
        caveat={report.rejection_tally_caveat}
        strategy={strategy}
      />

      {onTweakSubmit && (
        <TweakBar
          // Keyed on the RUN: the controls are prefilled from this run's
          // effective config, so carrying a half-typed field across a run
          // switch would show one run's number under another run's key.
          key={sweep.run_id}
          sweep={sweep}
          allowlist={allowlist}
          allowlistError={allowlistError}
          baseEffective={baseEffective}
          scenario={scenario}
          symbol={symbol}
          split={split}
          submitting={tweakSubmitting}
          submittingFrom={tweakSubmittingFrom}
          outcome={tweakOutcome}
          onSubmit={onTweakSubmit}
          onClearOutcome={onClearTweakOutcome ?? (() => undefined)}
          // The already-asked arm is a cell of THIS run, so opening it is a
          // cell selection like any other and travels through the URL.
          onOpenCell={(cell) => onSelectCell?.(cell)}
        />
      )}

      <ProvenanceFooter
        sweep={sweep}
        report={report}
        scenario={scenario}
        artifact={artifactState.data}
        artifactAbsence={artifactState.absent ?? artifactState.error}
        bars={barsState.data}
        barsAbsence={barsState.absent ?? barsState.error}
        artifactRunId={artifactState.runId}
        dedupFrom={dedupFrom}
        baseEffective={baseEffective}
        strategy={strategy}
      />
    </section>
  );
}
