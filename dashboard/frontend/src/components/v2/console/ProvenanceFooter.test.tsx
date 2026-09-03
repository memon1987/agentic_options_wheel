// FC-096 Phase E PR-2: the footer's absence states and its one alarm.
//
// The footer is where "we do not know" and "it broke" have to stay different
// sentences, so the tests are about which sentence appears — never about
// layout.

import { describe, expect, it } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import artifactA48d from '../../../test/fixtures/artifact_a48d_base_googl_fit.json';
import bars13cc from '../../../test/fixtures/bars_13cc_googl_fit.json';
import type { SweepRow } from '../../../types/v2';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { normaliseArtifact, parseBars } from './normaliseArtifact';
import ProvenanceFooter from './ProvenanceFooter';

const detail = normaliseSweepDetail(shaped13cc)!;
const report = detail.results!;
const sweep = detail.sweep;
const artifact = normaliseArtifact(artifact13cc)!;
const sidecar = parseBars(bars13cc).value!;

/**
 * Render one footer, replacing any previous one.
 *
 * Several tests render twice to contrast two inputs (three-state completeness,
 * identity mismatch on and off); without the explicit cleanup both footers stay
 * in the document and every `getByTestId` becomes ambiguous.
 */
const show = (over: Partial<React.ComponentProps<typeof ProvenanceFooter>> = {}) => {
  cleanup();
  return render(
    <ProvenanceFooter
      sweep={sweep}
      report={report}
      scenario="base"
      artifact={artifact}
      artifactAbsence={null}
      bars={sidecar}
      barsAbsence={null}
      artifactRunId={sweep.run_id}
      baseEffective={{ 'risk.max_position_size': 0.35 }}
      strategy="wheel"
      {...over}
    />,
  );
};

const text = () => screen.getByTestId('provenance-footer').textContent ?? '';

describe('the run block', () => {
  it('prints the run identity and both windows', () => {
    show();
    expect(text()).toMatch(/13cc2729d1c74211/);
    expect(text()).toMatch(/129711f15fe0488a/);
    expect(text()).toMatch(/2025-09-01 → 2026-06-02/);
  });

  it('says "complete" for artifacts_complete true', () => {
    show();
    expect(text()).toMatch(/complete — every non-errored cell stored its artifact/);
  });

  it('distinguishes "we do not know" from "a write failed"', () => {
    // Three-state, and the two false-ish states must not collapse: one sends an
    // operator to the storage logs, the other sends them nowhere.
    show({ report: { ...report, artifacts_complete: null } });
    expect(text()).toMatch(/not recorded — this run predates the column/);
    expect(text()).not.toMatch(/INCOMPLETE/);

    show({ report: { ...report, artifacts_complete: false } });
    expect(text()).toMatch(/INCOMPLETE/);
  });

  it('names the symbols whose earnings calendar had no rows', () => {
    show();
    expect(text()).toMatch(/none — the gate had calendar rows for every symbol/);
    show({ report: { ...report, earnings_symbols_without_data: ['PFE', 'KMI'] } });
    expect(text()).toMatch(/PFE, KMI/);
  });

  it('marks a DTE reach above the default', () => {
    show();
    // The parenthesised marker, not the bare words: the cell block carries a
    // `masked reach` ROW unconditionally (the artifact's own stamp), and
    // matching on the words alone would assert nothing about the run's DTE.
    expect(text()).not.toMatch(/\(masked reach\)/);
    show({ report: { ...report, effective_max_dte: 14 } });
    expect(text()).toMatch(/14 \(masked reach\)/);
  });
});

describe('the cell block', () => {
  it('prints the stamps the post-PR-1 object carries', () => {
    show();
    expect(text()).toMatch(/79a2df3d5d3a617cccba9c706cb3cf130c32f889/);
    expect(text()).toMatch(/473 sh/);
    expect(text()).toMatch(/2025-09-02 @ 211\.35/);
    expect(text()).toMatch(/189 in window · materialised bars/);
    expect(text()).toMatch(/2025-07-03 → 2026-06-02/);
  });

  it('says an absent stamp is absent — on the real pre-PR-1 capture', () => {
    show({ artifact: normaliseArtifact(artifactA48d)! });
    expect(text()).toMatch(/not stamped \(object written before FC-096 Phase E\)/);
  });

  it('distinguishes an unstamped benchmark from a null one', () => {
    // Key absent = "this object predates PR-1". `null` = "PR-1 wrote it and the
    // replay scored no benchmark". Different facts, different sentences.
    show({ artifact: { ...artifact, benchmark: null } });
    expect(text()).toMatch(/none — this replay scored no benchmark/);
  });

  it('renders the endpoint’s own words for an absent artifact and sidecar', () => {
    show({
      artifact: null,
      artifactAbsence: 'No detail artifact for base/GOOGL/fit in run 13cc.',
      bars: null,
      barsAbsence: 'No bars sidecar for GOOGL/fit in run 13cc.',
    });
    expect(text()).toMatch(/No detail artifact for base\/GOOGL\/fit/);
    expect(text()).toMatch(/No bars sidecar for GOOGL\/fit/);
  });

  it('flags an artifact written by a different engine build than the run', () => {
    const foreign = {
      ...artifact,
      provenance: { ...artifact.provenance, engine_identity: 'deadbeefdeadbeef' },
    };
    show({ artifact: foreign });
    expect(text()).toMatch(/≠ run/);
    show();
    expect(text()).not.toMatch(/≠ run/);
  });

  it('says the artifact is the MID replay, not the bid one', () => {
    show();
    const fill = within(screen.getByTestId('provenance-footer')).getByTitle(/MID replay/);
    expect(fill.textContent).toMatch(/mid · haircut 0\.25/);
  });
});

describe('the arm block and dedup', () => {
  it('shows an override beside the base value it replaced', () => {
    show({
      scenario: 'position_20pct',
      report: {
        ...report,
        scenario_overrides: { position_20pct: { 'risk.max_position_size': 0.2 } },
      },
    });
    expect(text()).toMatch(/0\.2\s+\(base: 0\.35\)/);
  });

  it('prints an em dash when the base value was never recorded', () => {
    show({
      scenario: 'position_20pct',
      baseEffective: null,
      report: {
        ...report,
        scenario_overrides: { position_20pct: { 'risk.max_position_size': 0.2 } },
      },
    });
    expect(text()).toMatch(/\(base: —\)/);
  });

  it('labels the covered-call premise on a CC run', () => {
    show({ strategy: 'covered_call' });
    expect(text()).toMatch(/synthetic lot: 100 shares at the window-start close/);
  });

  it('names the run the evidence actually came from under dedup', () => {
    show({
      sweep: { ...sweep, run_id: 'aaaabbbbccccdddd' } as SweepRow,
      artifactRunId: '13cc2729d1c74211',
    });
    expect(text()).toMatch(/evidence read from/);
    expect(text()).toMatch(/13cc2729d1c74211/);
  });
});

describe('review round 1 additions', () => {
  it('an undeclared fill_haircut says "engine default", not "—" (F4)', () => {
    show();
    const footer = screen.getByTestId('provenance-footer');
    // `null` or absent does NOT mean "no haircut": it means the arm declared
    // none and the engine applied its own. An em dash reads as "unknown" and
    // sends the operator looking for a field that was never going to be there.
    expect(footer.textContent).toMatch(/not declared — engine default \(see cell\)/);
  });

  it('the identity-mismatch hover names only what came from the object (F9)', () => {
    show({
      artifact: { ...artifact, provenance: { ...artifact.provenance, engine_identity: 'other' } },
    });
    const rows = screen.getByTestId('provenance-footer').querySelectorAll('[title]');
    const hover = Array.from(rows)
      .map((r) => r.getAttribute('title') ?? '')
      .find((t) => t.includes('different engine build'))!;
    // The strip's verdict numbers are the ROW's; only the three digest tiles
    // are read out of the stored object. "The numbers on screen came from the
    // object" was wrong about most of them.
    expect(hover).toMatch(/Only the three digest tiles/);
    expect(hover).not.toMatch(/The numbers on screen came from the object/);
  });
});
