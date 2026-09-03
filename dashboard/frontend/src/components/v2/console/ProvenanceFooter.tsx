// FC-096 Phase E PR-2: the provenance footer — component 8 of the console.
//
// Three blocks, three sources, and they are NOT merged: the run (the sweep
// row), the arm against base (the spec), and the cell (the stored artifact and
// its bars sidecar). Merging them would hide the one fact this footer exists to
// surface — that a stored object can disagree with the run it claims to belong
// to. `engine_identity` on the artifact is compared against the run's and
// flagged in red when they differ, because that combination means the object
// was written by a different engine build than the row was.
//
// Absence is a rendered state everywhere here, never a blank: "not stored for
// this run" and "we do not know" are different sentences, and
// `artifacts_complete` is three-state for exactly that reason.

import type { SimArtifact, SimBars, SweepReport, SweepRow } from '../../../types/v2';
import { fmtCurrency } from '../../../utils/format';

const DASH = '—';

function Row({ label, value, title }: { label: string; value: React.ReactNode; title?: string }) {
  return (
    <div className="flex gap-2 text-xs" title={title}>
      <dt className="text-gray-500 shrink-0 w-44">{label}</dt>
      <dd className="text-gray-300 font-mono break-all">{value}</dd>
    </div>
  );
}

function Block({ heading, children }: { heading: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <h4 className="text-[11px] uppercase tracking-wide text-gray-500">{heading}</h4>
      <dl className="space-y-0.5">{children}</dl>
    </div>
  );
}

/**
 * `artifacts_complete`, in the three sentences `scenario_runs.md` fixes.
 *
 * `null` is NOT `false`. "At least one artifact write failed" sends an operator
 * to the storage logs; "this run predates the column" sends them nowhere at
 * all, and collapsing the two would manufacture incidents on every old run.
 */
function completeness(value: boolean | null | undefined): string {
  if (value === true) return 'complete — every non-errored cell stored its artifact';
  if (value === false)
    return 'INCOMPLETE — at least one artifact write failed, so a 404 on a cell of this run is a storage failure, not a quiet replay';
  return 'not recorded — this run predates the column, which says nothing either way';
}

export interface ProvenanceFooterProps {
  sweep: SweepRow;
  report: SweepReport;
  scenario: string;
  artifact: SimArtifact | null;
  artifactAbsence: string | null;
  bars: SimBars | null;
  barsAbsence: string | null;
  /** The run whose objects were read — differs from `sweep.run_id` under dedup. */
  artifactRunId: string | null;
  /**
   * The `deduplicated` run this screen was auto-opened FROM (review round 1,
   * F5). The evidence on screen answers that run's spec as well as this one's,
   * and an operator who followed a link from it needs the chain written down.
   */
  dedupFrom?: string | null;
  /**
   * `base_config_json.effective` — the base arm's value per DOTTED key, so an
   * override can be shown beside the value it replaced. `null` on a run written
   * before the snapshot existed, and the block prints `—` rather than pretending
   * the base value was absent from the config.
   */
  baseEffective: Record<string, unknown> | null;
  /** §Degrading for CC: `spec.strategy`, then the stamp, else `wheel`. */
  strategy: string;
}

export default function ProvenanceFooter({
  sweep,
  report,
  scenario,
  artifact,
  artifactAbsence,
  bars,
  barsAbsence,
  artifactRunId,
  dedupFrom = null,
  baseEffective,
  strategy,
}: ProvenanceFooterProps) {
  const overrides = report.scenario_overrides?.[scenario] ?? {};
  const overrideKeys = Object.keys(overrides);
  const identityMismatch =
    !!artifact?.provenance.engine_identity &&
    !!sweep.engine_identity &&
    artifact.provenance.engine_identity !== sweep.engine_identity;

  return (
    <section
      data-testid="provenance-footer"
      className="rounded-lg border border-gray-700 bg-gray-900/60 p-4 grid gap-5 md:grid-cols-3"
    >
      <Block heading="Run">
        <Row label="run_id" value={sweep.run_id} />
        {dedupFrom && (
          <Row
            label="reached from"
            value={`${dedupFrom} (deduplicated)`}
            title="That run was deduplicated: nothing was replayed under its own id, so the page opened the run that answered it. The evidence below is this run's."
          />
        )}
        {artifactRunId && artifactRunId !== sweep.run_id && (
          <Row
            label="evidence read from"
            value={artifactRunId}
            title="This run was deduplicated: nothing was replayed under its own id, so its objects live under the run that answered it."
          />
        )}
        <Row label="submitted via" value={sweep.submitted_via ?? DASH} />
        <Row label="submitted / finished" value={`${sweep.submitted_at ?? DASH} → ${sweep.finished_at ?? DASH}`} />
        <Row label="engine_version" value={sweep.engine_version ?? DASH} />
        <Row label="engine_identity" value={sweep.engine_identity ?? DASH} />
        <Row label="git_commit" value={sweep.git_commit ?? DASH} />
        <Row label="base_config_hash" value={report.base_config_hash ?? DASH} />
        <Row
          label="effective max DTE"
          value={
            report.effective_max_dte === null
              ? DASH
              : `${report.effective_max_dte}${report.effective_max_dte > 7 ? ' (masked reach)' : ''}`
          }
        />
        <Row label="artifacts" value={completeness(report.artifacts_complete)} />
        <Row
          label="earnings gaps"
          value={
            report.earnings_symbols_without_data.length === 0
              ? 'none — the gate had calendar rows for every symbol'
              : report.earnings_symbols_without_data.join(', ')
          }
          title="Symbols whose earnings calendar had no rows for this window: the gate could not run for them. A caveat on their cells, not a defect."
        />
        <Row label="in-sample only" value={report.in_sample_only ? 'yes' : 'no'} />
        {report.windows.map((w) => (
          <Row key={w.split} label={`window · ${w.split}`} value={`${w.start} → ${w.end}`} />
        ))}
      </Block>

      <Block heading={`Arm "${scenario}" vs base`}>
        {overrideKeys.length === 0 ? (
          <Row
            label="overrides"
            value={scenario === 'base' ? 'none — this IS the base arm' : 'none recorded on this run'}
          />
        ) : (
          overrideKeys.map((key) => (
            <Row
              key={key}
              label={key}
              value={`${JSON.stringify(overrides[key])}  (base: ${
                baseEffective && key in baseEffective
                  ? JSON.stringify(baseEffective[key])
                  : DASH
              })`}
            />
          ))
        )}
        <Row
          label="fill_haircut"
          value={
            report.scenario_fill_haircuts?.[scenario] === null ||
            report.scenario_fill_haircuts?.[scenario] === undefined
              ? 'not declared — engine default (see cell)'
              : String(report.scenario_fill_haircuts[scenario])
          }
          title="What the SPEC declared for this arm. `null` or absent does not mean 'no haircut': it means the arm declared none and the engine applied its own default, which the cell block below shows as the replay actually used it."
        />
        <Row label="scenario_hash" value={report.scenario_hashes?.[scenario] ?? DASH} />
        <Row
          label="config_hash"
          value={
            report.scenario_config_hashes?.[scenario]
              ? `${report.scenario_config_hashes[scenario]}${
                  report.scenario_config_hashes[scenario] === report.base_config_hash ? ' (= base)' : ''
                }`
              : DASH
          }
        />
        <Row
          label="strategy"
          value={
            strategy === 'covered_call'
              ? 'covered_call — synthetic lot: 100 shares at the window-start close (D2)'
              : strategy
          }
        />
      </Block>

      <Block heading="Cell (artifact + bars sidecar)">
        {artifact === null ? (
          <Row label="artifact" value={artifactAbsence ?? 'not stored'} />
        ) : (
          <>
            <Row label="generated_at" value={artifact.provenance.generated_at ?? DASH} />
            <Row
              label="engine_identity"
              value={
                <span className={identityMismatch ? 'text-red-400' : undefined}>
                  {artifact.provenance.engine_identity ?? DASH}
                  {identityMismatch ? '  ≠ run' : ''}
                </span>
              }
              title="A stored object written by a different engine build than the run row claims. Only the three digest tiles came from this object — every verdict number on the strip is the ROW's, and the row is what dedup keys on."
            />
            <Row label="git_commit" value={artifact.provenance.git_commit ?? DASH} />
            <Row
              label="fill"
              value={`${artifact.provenance.fill?.basis ?? DASH} · haircut ${
                artifact.provenance.fill?.fill_haircut ?? DASH
              }`}
              title="This artifact is the MID replay. The row's `bid_fill_return` is a SECOND replay with no artifact of its own — the ledger shown here is not that one."
            />
            <Row
              label="masked reach"
              value={
                artifact.provenance.masked_reach
                  ? Object.entries(artifact.provenance.masked_reach)
                      .map(([k, v]) => `${k}=${v}`)
                      .join(' ')
                  : DASH
              }
            />
            <Row
              label="decision days"
              value={`${artifact.provenance.window.first_decision_day} → ${artifact.provenance.window.last_decision_day}`}
              title="The engine's own bounds. The forecast's per-day rates divide by the REQUESTED window instead, which the dashboard can see and these bounds it cannot — the two differ by the non-session edges."
            />
            <Row
              label="capital_base"
              value={
                artifact.provenance.capital_base === undefined
                  ? 'not stamped (object written before FC-096 Phase E)'
                  : fmtCurrency(artifact.provenance.capital_base)
              }
            />
            {artifact.benchmark === undefined ? (
              <Row label="benchmark" value="not stamped (object written before FC-096 Phase E)" />
            ) : artifact.benchmark === null ? (
              <Row label="benchmark" value="none — this replay scored no benchmark" />
            ) : (
              <Row
                label="benchmark"
                value={`${artifact.benchmark.shares} sh · ${artifact.benchmark.entry_day} @ ${artifact.benchmark.entry_price} → ${artifact.benchmark.exit_day} @ ${artifact.benchmark.exit_price} · div/sh ${artifact.benchmark.dividends_per_share_total} · final ${fmtCurrency(artifact.benchmark.final_value)}`}
                title={`Whole shares bought with ${fmtCurrency(
                  artifact.provenance.capital_base,
                )} at the first close and held to the last, dividends included. Whole shares only: the remainder stays idle cash, so the benchmark is not quite fully invested.`}
              />
            )}
          </>
        )}
        {bars === null ? (
          <Row label="price series" value={barsAbsence ?? 'not stored for this run'} />
        ) : (
          <>
            <Row
              label="bars"
              value={`${bars.provenance.bars_in_window ?? DASH} in window · ${bars.provenance.source ?? DASH}`}
            />
            <Row
              label="data range"
              value={`${bars.provenance.data_from ?? DASH} → ${bars.provenance.data_to ?? DASH}`}
              title="The materialised range behind this window. Lake freshness proper has no dashboard-readable source; this is the closest fact stored."
            />
          </>
        )}
      </Block>
    </section>
  );
}
