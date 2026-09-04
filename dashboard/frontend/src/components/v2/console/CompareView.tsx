// FC-096 Phase E PR-5 (§Compare view): two cells, side by side, under the
// alignment matrix.
//
// Order is the argument, exactly as it is in `Console`: the in-sample banner
// precedes everything it qualifies; the ALIGNMENT MATRIX precedes every number,
// because whether these two may be compared is prior to what they say; the
// refusal, if there is one, ends the page there; the A−B number appears only
// where the matrix permits it; the two provenance footers close the evidence;
// and the page renders the single bias footer after this component (the PR-2
// composition rule — the caveats are last, below everything they qualify).
//
// What is deliberately NOT here: a digest tile row (client-derived numbers are
// never compared), a portfolio view (per-scenario `included` sets differ, so
// arm-vs-arm portfolio comparison does not exist anywhere), and a forecast
// panel (same reason — and the forecast is per symbol per arm, not per pair).

import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { SimArtifact, SimBars, SweepReport, SweepResultRow, SweepRow } from '../../../types/v2';
import { fmtCurrency, fmtDateShort } from '../../../utils/format';
import { pctOrDash } from '../sims/resultCells';
import { IN_SAMPLE_FALLBACK } from '../sims/SweepResults';
import VerdictStrip from './VerdictStrip';
import ProvenanceFooter from './ProvenanceFooter';
import PriceChart from './PriceChart';
import DrawdownChart from './DrawdownChart';
import PremiumCharts from './PremiumCharts';
import DeploymentChart from './DeploymentChart';
import type { ArtifactDigest } from './artifactDigest';
import { alignCells, differenceOfDeltas, type Alignment, type CompareRef, type CompareSide } from './compareAlignment';
import { compareEquity, nonOverlap } from './compareSeries';

/** Everything one side of the comparison has loaded. Assembled by the page. */
export interface CompareSideData {
  ref: CompareRef;
  sweep: SweepRow | null;
  report: SweepReport | null;
  row: SweepResultRow | null;
  artifact: SimArtifact | null;
  artifactAbsence: string | null;
  /** The side's OWN base arm — "anchored to base" means each panel's own base. */
  baseArtifact: SimArtifact | null;
  baseAbsence: string | null;
  bars: SimBars | null;
  barsAbsence: string | null;
  digest: ArtifactDigest | null;
  digestAbsence: string | null;
  artifactRunId: string | null;
  baseEffective: Record<string, unknown> | null;
  strategy: string;
  /** The run's detail is still loading, or could not be read. */
  loading: boolean;
  error: string | null;
}

const OUTCOME_STYLE: Record<string, string> = {
  aligned: 'text-green-400',
  noted: 'text-blue-300',
  withheld: 'text-amber-300',
  refused: 'text-red-400',
  unknown: 'text-gray-500',
};

const OUTCOME_TEXT: Record<string, string> = {
  aligned: 'aligned',
  noted: 'noted',
  withheld: 'withheld',
  refused: 'refused',
  unknown: 'not checked',
};

const toSide = (d: CompareSideData): CompareSide => ({
  ref: d.ref,
  sweep: d.sweep,
  report: d.report,
  row: d.row,
  stampedCapitalBase: d.artifact?.provenance.capital_base ?? null,
});

const cellPathOf = (ref: CompareRef): string =>
  `/sims/${encodeURIComponent(ref.runId)}/${encodeURIComponent(ref.scenario)}/` +
  `${encodeURIComponent(ref.symbol)}/${encodeURIComponent(ref.split)}`;

const label = (ref: CompareRef): string => `${ref.scenario} · ${ref.symbol} · ${ref.split}`;

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-gray-700 bg-gray-800 p-5 space-y-3">
      <h3 className="text-base font-semibold text-white">{title}</h3>
      {children}
    </section>
  );
}

/** The matrix, rendered row by row with its outcome named rather than implied. */
export function AlignmentMatrix({ alignment }: { alignment: Alignment }) {
  return (
    <Panel title="Alignment — may these two be compared?">
      <p className="text-xs text-gray-500">
        Every dimension is checked and its outcome shown, including the ones that pass: an operator
        must be able to tell &ldquo;checked and equal&rdquo; from &ldquo;not checked&rdquo;.{' '}
        <span className="text-amber-300">withheld</span> means the curves are drawn and the numbers
        are not; <span className="text-blue-300">noted</span> means comparable with something said;{' '}
        <span className="text-red-400">refused</span> means the pair is not two configs at all.
      </p>
      <table className="w-full text-sm" data-testid="alignment-matrix">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-gray-500">
            <th className="py-1 pr-3">Dimension</th>
            <th className="py-1 pr-3">A</th>
            <th className="py-1 pr-3">B</th>
            <th className="py-1 pr-3">Outcome</th>
          </tr>
        </thead>
        <tbody>
          {alignment.rows.map((row) => (
            <tr key={row.id} className="border-t border-gray-700/60 align-top">
              <td className="py-2 pr-3 text-gray-300 whitespace-nowrap">{row.label}</td>
              <td className="py-2 pr-3 font-mono text-xs text-gray-400 break-all">{row.a}</td>
              <td className="py-2 pr-3 font-mono text-xs text-gray-400 break-all">{row.b}</td>
              <td className="py-2 pr-3">
                <span
                  data-testid={`alignment-${row.id}`}
                  data-outcome={row.outcome}
                  className={`text-xs font-semibold ${OUTCOME_STYLE[row.outcome]}`}
                >
                  {OUTCOME_TEXT[row.outcome]}
                </span>
                <p className="text-xs text-gray-500 mt-1 max-w-2xl">{row.detail}</p>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

/** The overrides diff: key → a / b / each run's RECORDED base. */
export function OverridesDiff({
  alignment,
  a,
  b,
}: {
  alignment: Alignment;
  a: CompareRef;
  b: CompareRef;
}) {
  const oneRun = a.runId === b.runId;
  return (
    <Panel title="Overrides — what each arm actually sets">
      <p className="text-xs text-gray-500">
        The union of both arms&rsquo; override keys, so a key one arm sets and the other leaves at
        base is visible as exactly that. The base column{oneRun ? '' : 's'} show
        {oneRun ? 's' : ''} each run&rsquo;s <strong>recorded</strong>{' '}
        <span className="font-mono">base_config_json.effective</span> — what that run replayed
        against, not the sim service&rsquo;s current config, which may have moved since.
      </p>
      {alignment.overridesDiff.length === 0 ? (
        <p data-testid="overrides-diff-empty" className="text-sm text-gray-400">
          Neither arm records an override: both are the run&rsquo;s base arm.
        </p>
      ) : (
        <table className="w-full text-sm" data-testid="overrides-diff">
          <thead>
            <tr className="text-left text-[11px] uppercase tracking-wide text-gray-500">
              <th className="py-1 pr-3">Key</th>
              <th className="py-1 pr-3">A · {a.scenario}</th>
              <th className="py-1 pr-3">B · {b.scenario}</th>
              {oneRun ? (
                <th className="py-1 pr-3">base (recorded)</th>
              ) : (
                <>
                  <th className="py-1 pr-3">base of A&rsquo;s run</th>
                  <th className="py-1 pr-3">base of B&rsquo;s run</th>
                </>
              )}
            </tr>
          </thead>
          <tbody>
            {alignment.overridesDiff.map((row) => (
              <tr
                key={row.key}
                data-testid={`overrides-row-${row.key}`}
                data-same={row.same ? 'true' : 'false'}
                className={`border-t border-gray-700/60 ${row.same ? '' : 'bg-amber-950/20'}`}
              >
                <td className="py-1 pr-3 font-mono text-xs text-blue-300 break-all">{row.key}</td>
                <td className="py-1 pr-3 font-mono text-xs text-gray-200">{row.a}</td>
                <td className="py-1 pr-3 font-mono text-xs text-gray-200">{row.b}</td>
                <td className="py-1 pr-3 font-mono text-xs text-gray-500">{row.baseA}</td>
                {!oneRun && (
                  <td className="py-1 pr-3 font-mono text-xs text-gray-500">{row.baseB}</td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

/** The A−B number, or the sentence saying why there is none. */
export function DeltaOfDeltas({
  alignment,
  a,
  b,
}: {
  alignment: Alignment;
  a: CompareSideData;
  b: CompareSideData;
}) {
  const value = differenceOfDeltas(alignment, toSide(a), toSide(b));
  if (value === null) {
    return (
      <p data-testid="ab-refused" className="text-sm text-gray-400">
        {alignment.deltaRefusal ??
          'No A−B number: at least one side has no served Δ against its base.'}
      </p>
    );
  }
  return (
    <div data-testid="ab-delta" className="text-sm">
      <span className="text-[11px] uppercase tracking-wide text-gray-500 block">
        A−B · difference of two served Δs
      </span>
      <span className="text-lg font-semibold text-gray-100">{pctOrDash(value)}</span>
      <p className="text-xs text-gray-500 mt-1 max-w-3xl">
        <span className="font-mono">delta_vs_base_annualized</span> of{' '}
        <span className="font-mono">{a.ref.scenario}</span> minus that of{' '}
        <span className="font-mono">{b.ref.scenario}</span>, both served by the server for the same
        symbol, split and base. This page did not compute either Δ and does not compute annualised
        returns; it subtracts two numbers the engine already published.
      </p>
    </div>
  );
}

/**
 * Both sides' equity, each anchored to its OWN base, on one axis.
 *
 * Dollars when the capital bases agree; an index at 100 on each series' own
 * first day when they do not. The unit comes from `compareEquity` rather than
 * from a prop, so the axis label and the transform cannot disagree.
 */
export function CompareEquityChart({
  alignment,
  a,
  b,
}: {
  alignment: Alignment;
  a: CompareSideData;
  b: CompareSideData;
}) {
  const series = compareEquity(
    { cell: a.artifact, base: a.baseArtifact, bars: a.bars },
    { cell: b.artifact, base: b.baseArtifact, bars: b.bars },
    alignment.curvesBase100,
  );
  const wa = a.report?.windows.find((w) => w.split === a.ref.split) ?? null;
  const wb = b.report?.windows.find((w) => w.split === b.ref.split) ?? null;
  const shades = nonOverlap(wa, wb);
  const fmt = (n: number) => (series.base100 ? n.toFixed(1) : fmtCurrency(n, { compact: true }));

  if (series.rows.length === 0) {
    return (
      <Panel title="Equity — both sides, each anchored to its own base">
        <p data-testid="compare-equity-absent" className="text-sm text-gray-400">
          {a.artifactAbsence ??
            b.artifactAbsence ??
            'No daily state was stored for either cell, so there is no curve to draw.'}
        </p>
      </Panel>
    );
  }

  return (
    <Panel title="Equity — both sides, each anchored to its own base">
      <p className="text-xs text-gray-500" data-testid="compare-equity-unit">
        Unit: {series.unit}.{' '}
        {series.base100
          ? 'The two cells were replayed on DIFFERENT capital bases, so dollars on one axis would ' +
            'compare sizes rather than strategies. Each series is rebased to 100 on its own first ' +
            'day — which is not the same calendar day when the windows differ.'
          : 'Both sides share a capital base, so these are the engine’s own dollars, unrescaled.'}
      </p>
      <p className="text-xs text-gray-500" data-testid="compare-benchmark-note">
        {series.benchmarkNote}
      </p>
      {shades.length > 0 && (
        <p className="text-xs text-amber-300" data-testid="compare-nonoverlap">
          Shaded: the stretches only one side&rsquo;s window covers. Nothing inside them is a
          comparison — one of the two curves does not exist there.
        </p>
      )}
      <div className="h-80">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={series.rows} margin={{ top: 5, right: 5, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            {shades.map((s) => (
              <ReferenceArea
                key={`${s.side}-${s.start}`}
                x1={s.start}
                x2={s.end}
                fill="#f59e0b"
                fillOpacity={0.08}
              />
            ))}
            <XAxis
              dataKey="date"
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              tickFormatter={fmtDateShort}
              minTickGap={28}
            />
            <YAxis
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              domain={['auto', 'auto']}
              tickFormatter={fmt}
            />
            <Tooltip
              contentStyle={{ background: '#1f2937', border: '1px solid #374151', color: '#f3f4f6' }}
              labelFormatter={(l) => fmtDateShort(l as string)}
              formatter={(value: number, name: string) => [fmt(value), name]}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {series.hasA && (
              <Line type="monotone" dataKey="aEquity" name={`A · ${label(a.ref)}`} stroke="#60a5fa" strokeWidth={2} dot={false} connectNulls />
            )}
            {series.hasABase && (
              <Line type="monotone" dataKey="aBase" name="A · its base" stroke="#60a5fa" strokeWidth={1.25} strokeDasharray="5 3" dot={false} connectNulls />
            )}
            {series.hasB && (
              <Line type="monotone" dataKey="bEquity" name={`B · ${label(b.ref)}`} stroke="#fbbf24" strokeWidth={2} dot={false} connectNulls />
            )}
            {series.hasBBase && (
              <Line type="monotone" dataKey="bBase" name="B · its base" stroke="#fbbf24" strokeWidth={1.25} strokeDasharray="5 3" dot={false} connectNulls />
            )}
            {series.sharedBenchmark && (
              <Line type="monotone" dataKey="benchmark" name="Buy & hold (engine, shared)" stroke="#9ca3af" strokeWidth={1.5} strokeDasharray="2 3" dot={false} connectNulls />
            )}
            {!series.sharedBenchmark && (
              <Line type="monotone" dataKey="aBenchmark" name="A · buy & hold" stroke="#93c5fd" strokeWidth={1.25} strokeDasharray="2 3" dot={false} connectNulls />
            )}
            {!series.sharedBenchmark && (
              <Line type="monotone" dataKey="bBenchmark" name="B · buy & hold" stroke="#fcd34d" strokeWidth={1.25} strokeDasharray="2 3" dot={false} connectNulls />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="text-xs text-gray-500">
        Each side is drawn with its OWN run&rsquo;s base arm beneath it (dashed, same hue). That is
        what &ldquo;anchored to base&rdquo; means here: neither side is drawn against the
        other&rsquo;s comparator.
      </p>
    </Panel>
  );
}

export interface CompareViewProps {
  a: CompareSideData;
  /** `null` until the operator picks a second cell. The page never picks one. */
  b: CompareSideData | null;
}

/** One side's provenance footer, headed so the two cannot be confused. */
function SideProvenance({ side, tag }: { side: CompareSideData; tag: 'A' | 'B' }) {
  if (!side.sweep || !side.report) {
    return (
      <p data-testid={`provenance-${tag}-absent`} className="text-sm text-gray-400">
        {tag}: {side.error ?? 'this run has not loaded, so its provenance cannot be shown.'}
      </p>
    );
  }
  return (
    <div data-testid={`provenance-${tag}`}>
      <p className="text-[11px] uppercase tracking-wide text-gray-500 mb-1">
        {tag} · {side.ref.runId} · {label(side.ref)}
      </p>
      <ProvenanceFooter
        sweep={side.sweep}
        report={side.report}
        scenario={side.ref.scenario}
        artifact={side.artifact}
        artifactAbsence={side.artifactAbsence}
        bars={side.bars}
        barsAbsence={side.barsAbsence}
        artifactRunId={side.artifactRunId}
        baseEffective={side.baseEffective}
        strategy={side.strategy}
      />
    </div>
  );
}

export default function CompareView({ a, b }: CompareViewProps) {
  if (!b) {
    return (
      <section data-testid="compare-view" className="space-y-4">
        <p data-testid="compare-awaiting-b" className="text-sm text-gray-400">
          Pick a second cell to compare against. Nothing is chosen for you: a partner picked by the
          page would be a comparison the operator did not ask for, and the alignment matrix cannot
          warn about a question nobody posed.
        </p>
      </section>
    );
  }

  const alignment = alignCells(toSide(a), toSide(b), a.baseEffective, b.baseEffective);
  const strip = (side: CompareSideData, tag: 'A' | 'B') =>
    side.report ? (
      <div data-testid={`strip-${tag}`} className="rounded-lg border border-gray-700 bg-gray-800 p-4">
        <p className="text-[11px] uppercase tracking-wide text-gray-500 mb-2">
          {tag} · run <span className="font-mono">{side.ref.runId}</span>
        </p>
        <VerdictStrip
          row={side.row}
          report={side.report}
          scenario={side.ref.scenario}
          symbol={side.ref.symbol}
          split={side.ref.split}
          digest={side.digest}
          digestAbsence={side.digestAbsence}
          artifact={side.artifact}
          variant="compare"
          withheldReasons={alignment.withheldReasons}
        />
      </div>
    ) : (
      <p data-testid={`strip-${tag}-absent`} className="text-sm text-gray-400">
        {tag}: {side.error ?? 'loading this run…'}
      </p>
    );

  return (
    <section data-testid="compare-view" className="space-y-6">
      {/* Keyed on each run's own flag, and printed for EACH side that carries
          it — an in-sample A beside an out-of-sample B is a fact about A, and a
          single merged banner would not say which side it was about. */}
      {[a, b].map((side, i) =>
        side.report?.in_sample_only ? (
          <p
            key={side.ref.runId + i}
            data-testid={`compare-in-sample-${i === 0 ? 'a' : 'b'}`}
            className="rounded border border-amber-700/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-300 whitespace-pre-wrap"
          >
            {i === 0 ? 'A' : 'B'} ({side.ref.runId}): {side.report.in_sample_banner ?? IN_SAMPLE_FALLBACK}
          </p>
        ) : null,
      )}

      <AlignmentMatrix alignment={alignment} />

      {alignment.refusal ? (
        <Panel title="Refused">
          <p data-testid="compare-refusal" className="text-sm text-red-300">
            {alignment.refusal}
          </p>
          <div className="flex flex-wrap gap-3 text-sm">
            <Link className="underline text-blue-300" to={cellPathOf(a.ref)}>
              Open A · {label(a.ref)}
            </Link>
            <Link className="underline text-blue-300" to={cellPathOf(b.ref)}>
              Open B · {label(b.ref)}
            </Link>
          </div>
        </Panel>
      ) : (
        <>
          <Panel title="A versus B">
            <DeltaOfDeltas alignment={alignment} a={a} b={b} />
          </Panel>

          <div className="grid gap-4 lg:grid-cols-2">
            {strip(a, 'A')}
            {strip(b, 'B')}
          </div>

          <CompareEquityChart alignment={alignment} a={a} b={b} />

          <div className="grid gap-4 lg:grid-cols-2">
            <PriceChart
              symbol={a.ref.symbol}
              bars={a.bars}
              barsAbsence={a.barsAbsence}
              barsLoading={a.loading}
              artifact={a.artifact}
              artifactAbsence={a.artifactAbsence}
              window={a.report?.windows.find((w) => w.split === a.ref.split) ?? null}
              strategy={a.strategy}
            />
            <PriceChart
              symbol={b.ref.symbol}
              bars={b.bars}
              barsAbsence={b.barsAbsence}
              barsLoading={b.loading}
              artifact={b.artifact}
              artifactAbsence={b.artifactAbsence}
              window={b.report?.windows.find((w) => w.split === b.ref.split) ?? null}
              strategy={b.strategy}
            />
            <DrawdownChart
              series={a.digest?.drawdownSeries ?? []}
              row={a.row}
              absence={a.digestAbsence}
              strategy={a.strategy}
            />
            <DrawdownChart
              series={b.digest?.drawdownSeries ?? []}
              row={b.row}
              absence={b.digestAbsence}
              strategy={b.strategy}
            />
            <PremiumCharts digest={a.digest} absence={a.digestAbsence} strategy={a.strategy} stateLabel={null} />
            <PremiumCharts digest={b.digest} absence={b.digestAbsence} strategy={b.strategy} stateLabel={null} />
            <DeploymentChart
              series={a.digest?.deploymentSeries ?? []}
              reading={a.digest?.deployment ?? null}
              capitalBase={a.digest?.capitalBase ?? null}
              suppressionReason={a.digest?.suppressionReason ?? null}
              absence={a.digestAbsence}
              strategy={a.strategy}
            />
            <DeploymentChart
              series={b.digest?.deploymentSeries ?? []}
              reading={b.digest?.deployment ?? null}
              capitalBase={b.digest?.capitalBase ?? null}
              suppressionReason={b.digest?.suppressionReason ?? null}
              absence={b.digestAbsence}
              strategy={b.strategy}
            />
          </div>
          <p className="text-xs text-gray-500" data-testid="paired-charts-note">
            The four charts below the equity panel are PAIRED, left A and right B — not overlaid.
            Drawdown, premium and deployment are each read against their own cell&rsquo;s window and
            capital base, and stacking them on one axis would state a comparison the matrix above
            may have withheld. Nothing on this row is differenced.
          </p>

          <OverridesDiff alignment={alignment} a={a.ref} b={b.ref} />
        </>
      )}

      <Panel title="Provenance — both sides">
        <div className="space-y-6">
          <SideProvenance side={a} tag="A" />
          <SideProvenance side={b} tag="B" />
        </div>
      </Panel>
    </section>
  );
}
