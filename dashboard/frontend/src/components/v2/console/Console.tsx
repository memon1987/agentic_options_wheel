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

import { useMemo } from 'react';
import type { SimBars, SweepReport, SweepRow } from '../../../types/v2';
import { useArtifact } from '../../../hooks/useArtifact';
import { useBars } from '../../../hooks/useBars';
import { indexRows, lookupCell } from '../sims/resultCells';
import { computeDigest } from './artifactDigest';
import { artifactStrategy } from './normaliseArtifact';
import VerdictStrip from './VerdictStrip';
import ProvenanceFooter from './ProvenanceFooter';

/** A panel PR-3 will fill. Named so its absence is legible, not a gap. */
function Placeholder({ title, testId }: { title: string; testId: string }) {
  return (
    <section
      data-testid={testId}
      className="rounded-lg border border-dashed border-gray-700 bg-gray-900/40 p-4"
    >
      <h3 className="text-sm font-semibold text-gray-400">{title}</h3>
      <p className="text-xs text-gray-600 mt-1">
        Rendered in FC-096 Phase E PR-3. The evidence for it is already loaded.
      </p>
    </section>
  );
}

/** `base_config_json` is a JSON STRING on the row; a bad one is not fatal. */
function parseBaseEffective(raw: string | null | undefined): Record<string, unknown> | null {
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
function specStrategy(sweep: SweepRow): string | null {
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
}

export default function Console({ sweep, report, scenario, symbol, split }: ConsoleProps) {
  const cell = useMemo(() => ({ scenario, symbol, split }), [scenario, symbol, split]);
  const artifactState = useArtifact(sweep, cell);
  const barsState = useBars(sweep, symbol, split);

  const row = useMemo(
    () => lookupCell(indexRows(report.rows), scenario, symbol, split) ?? null,
    [report.rows, scenario, symbol, split],
  );

  const strategy = artifactStrategy(specStrategy(sweep), artifactState.data);
  const digest = useMemo(
    () =>
      artifactState.data
        ? computeDigest(artifactState.data, barsState.data as SimBars | null, {
            specStrategy: specStrategy(sweep),
          })
        : null,
    // `sweep` is stable per poll; the digest is cheap (72 events, 189 days) and
    // re-running it on a poll tick is cheaper than memoising it wrongly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [artifactState.data, barsState.data, sweep.spec_json],
  );

  const digestAbsence =
    artifactState.absent ??
    artifactState.error ??
    (artifactState.sessionExpired ? 'Session expired — reload to sign in again.' : null) ??
    (artifactState.loading ? 'Loading this cell’s artifact…' : null);

  return (
    <section data-testid="sim-console" className="space-y-4">
      {report.in_sample_only && report.in_sample_banner && (
        <p
          data-testid="console-in-sample-banner"
          className="rounded border border-amber-700/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-300"
        >
          {report.in_sample_banner}
        </p>
      )}

      {report.artifacts_complete === false && (
        <p data-testid="artifacts-incomplete" className="text-sm text-amber-400">
          This run&rsquo;s evidence set is incomplete (storage failure, not a quiet replay): at
          least one artifact write failed, so a missing cell below is a lost object rather than a
          cell that had nothing to store.
        </p>
      )}

      {artifactState.followedDedup && artifactState.runId && (
        <p data-testid="followed-dedup" className="text-sm text-gray-400">
          This run was deduplicated — nothing was replayed under its own id. The evidence below is
          answered by run <span className="font-mono">{artifactState.runId}</span>.
        </p>
      )}

      <VerdictStrip
        row={row}
        report={report}
        scenario={scenario}
        symbol={symbol}
        split={split}
        digest={digest}
        digestAbsence={digestAbsence}
      />

      <Placeholder testId="placeholder-price" title="Price + event markers (component 2)" />
      <Placeholder testId="placeholder-equity" title="Equity vs base vs buy & hold (component 3)" />
      <Placeholder testId="placeholder-premium" title="Premium: cumulative and monthly (component 3)" />
      <Placeholder testId="placeholder-forecast" title="Forecast range (component 7)" />
      <Placeholder testId="placeholder-ledger" title="Ledger + cycle tables (components 4 and 5)" />
      <Placeholder testId="placeholder-rejections" title="Rejections and binding constraint (component 6)" />

      <ProvenanceFooter
        sweep={sweep}
        report={report}
        scenario={scenario}
        artifact={artifactState.data}
        artifactAbsence={artifactState.absent ?? artifactState.error}
        bars={barsState.data}
        barsAbsence={barsState.absent ?? barsState.error}
        artifactRunId={artifactState.runId}
        baseEffective={parseBaseEffective(sweep.base_config_json)}
        strategy={strategy}
      />
    </section>
  );
}
