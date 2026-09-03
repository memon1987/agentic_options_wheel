// FC-096 Phase E PR-2: the strip, confined to text and absence.
//
// Every assertion is "this string is on screen" or "this tile is NOT on
// screen", because that is what the guardrails are about: which numbers a PM
// can read off an unmeasured cell, whether a null Δ is rendered as a Δ, and
// whether a client-derived number can be mistaken for an engine one. No layout,
// no styling, no chart.

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import shaped13cc from '../../../test/fixtures/sweep_shaped_13cc.json';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import artifactA48d from '../../../test/fixtures/artifact_a48d_base_googl_fit.json';
import bars13cc from '../../../test/fixtures/bars_13cc_googl_fit.json';
import { normaliseSweepDetail } from '../sims/normaliseReport';
import { indexRows, lookupCell } from '../sims/resultCells';
import { normaliseArtifact, parseBars } from './normaliseArtifact';
import { computeDigest } from './artifactDigest';
import VerdictStrip from './VerdictStrip';

const report = normaliseSweepDetail(shaped13cc)!.results!;
const rowFor = (scenario: string, symbol: string, split: string) =>
  lookupCell(indexRows(report.rows), scenario, symbol, split) ?? null;

const artifact = normaliseArtifact(artifact13cc)!;
const sidecar = parseBars(bars13cc).value!;
const digest = computeDigest(artifact, sidecar);

const show = (
  scenario: string,
  split: string,
  over: Partial<React.ComponentProps<typeof VerdictStrip>> = {},
) =>
  render(
    <VerdictStrip
      row={rowFor(scenario, 'GOOGL', split)}
      report={report}
      scenario={scenario}
      symbol="GOOGL"
      split={split}
      digest={digest}
      digestAbsence={null}
      artifact={artifact}
      {...over}
    />,
  );

describe('a measured cell', () => {
  it('renders the ENGINE numbers, exactly as the row carries them', () => {
    show('base', 'fit');
    // 0.09026715… annualised, 0.30333497… on collateral, 0.71484490 benchmark.
    expect(screen.getByTestId('tile-annualized').textContent).toMatch(/\+9\.0%/);
    expect(screen.getByTestId('tile-roc').textContent).toMatch(/\+30\.3%/);
    expect(screen.getByTestId('tile-benchmark').textContent).toMatch(/\+71\.5%/);
    expect(screen.getByTestId('tile-benchmark').textContent).toMatch(/-64\.7%/);
    expect(screen.getByTestId('tile-drawdown').textContent).toMatch(/-3\.4%/);
    expect(screen.getByTestId('tile-cycles').textContent).toMatch(/22 done/);
    expect(screen.getByTestId('tile-cycles').textContent).toMatch(/23 puts \/ 13 calls/);
    expect(screen.queryByTestId('unmeasured-notice')).toBeNull();
  });

  it('labels the benchmark with the NUMBER, and says whole shares (F9)', () => {
    show('base', 'fit');
    const title = screen.getByTestId('tile-benchmark').getAttribute('title')!;
    // `$capital_base` on screen is a variable name only its author can read;
    // the stamped base is $100,000 on this run. And the engine buys WHOLE
    // shares (473, ~$31 left idle), so "full investment" was not quite true.
    expect(title).toMatch(/\$100,000/);
    expect(title).not.toMatch(/capital_base/);
    expect(title).toMatch(/[Ww]hole shares/);
    expect(title).toMatch(/idle cash/);
  });

  it('renders NO Δ tile on the base cell — the server serves null there', () => {
    // The mutation: rendering a base Δ as 0. "This arm matched base" and "there
    // is no Δ to state" are different claims, and only one of them is true.
    show('base', 'fit');
    expect(screen.queryByTestId('tile-delta')).toBeNull();
  });

  it('reads the effective fill assumption off the forecast', () => {
    show('base', 'fit');
    const label = screen.getByTestId('fill-label').textContent;
    expect(label).toMatch(/mid/);
    expect(label).toMatch(/haircut 25%/);
    expect(label).toMatch(/engine default/);
  });
});

describe('sign agreement — two different questions, labelled apart', () => {
  it('renders an em dash for a null per-cell answer, never "agrees"', () => {
    // The mutation: treating null as false, or as agreement. Both invent an
    // answer where all four cells were required and were not all measured.
    show('base', 'fit');
    const tile = screen.getByTestId('tile-sign-agreement');
    expect(tile.textContent).toMatch(/—/);
    expect(tile.textContent).not.toMatch(/agrees/);
    // On the BASE arm the scenario-level count is base-vs-base and therefore
    // vacuously 1/1 — it is labelled as such rather than printed as evidence
    // (review round 1, F9).
    expect(tile.textContent).toMatch(/base is the comparator/);
    expect(tile.textContent).not.toMatch(/1\/1 across 1 symbols/);
  });

  it('renders the per-cell boolean when the server supplies one', () => {
    const row = { ...rowFor('base', 'GOOGL', 'fit')!, sign_agrees: true };
    render(
      <VerdictStrip
        row={row}
        report={report}
        scenario="base"
        symbol="GOOGL"
        split="fit"
        digest={digest}
        digestAbsence={null}
        artifact={artifact}
      />,
    );
    expect(screen.getByTestId('tile-sign-agreement').textContent).toMatch(/agrees/);
  });
});

describe('the cell-state partition', () => {
  it('an insufficient cell renders NO return, RoC, Δ or benchmark tile', () => {
    // `position_20pct` is `insufficient` in both windows on this run, and its
    // `annualized_return` is 0.0 — which is exactly the number that must not
    // reach the screen as "+0.0%".
    show('position_20pct', 'fit');
    expect(screen.getByTestId('cell-state-badge').textContent).toBe('insuf');
    expect(screen.queryByTestId('tile-annualized')).toBeNull();
    expect(screen.queryByTestId('tile-roc')).toBeNull();
    expect(screen.queryByTestId('tile-delta')).toBeNull();
    expect(screen.queryByTestId('tile-benchmark')).toBeNull();
    expect(screen.getByTestId('unmeasured-notice').textContent).toMatch(/No completed cycle/);
    // The activity counts still render — they are facts about the window.
    expect(screen.getByTestId('tile-cycles')).toBeTruthy();
  });

  it('a missing row renders the missing badge and no numbers', () => {
    show('base', 'fit', { row: null });
    expect(screen.getByTestId('cell-state-badge').textContent).toBe('—');
    expect(screen.queryByTestId('tile-annualized')).toBeNull();
  });
});

describe('the tooltips state what the ENGINE does (review round 1, F7)', () => {
  const titleOf = (id: string) => screen.getByTestId(id).getAttribute('title')!;

  it('annualisation divides by CALENDAR days, and the hover says so', () => {
    show('base', 'fit');
    const title = titleOf('tile-annualized');
    expect(title).toMatch(/CALENDAR days/);
    expect(title).toMatch(/365\/273/);
    // The mutation: "decision-day span". 0.0675 × 365/189 is +13.0%, not the
    // +9.0% on the tile — a hover that explains a number the tile is not
    // showing is worse than no hover. The hover names the wrong divisor only to
    // rule it out, and says by how much the two differ.
    expect(title).not.toMatch(/decision-day span/);
    expect(title).toMatch(/not ÷ decision days/);
    expect(title).toMatch(/not 365\/189/);
    expect(title).toMatch(/44%/);
  });

  it('the RoC hover gives the engine’s denominator, this cell’s figures and the caveat', () => {
    show('base', 'fit');
    const title = titleOf('tile-roc');
    // Mean reserved PUT collateral over the days it was above zero: 91 of 189,
    // averaging $29,758.24. Read off the artifact, not retyped.
    expect(title).toMatch(/91 of 189 days/);
    expect(title).toMatch(/\$29,758\.24/);
    expect(title).toMatch(/365 ÷ calendar days/);
    // Held shares are NOT in that denominator (FC-103 owns the fix), and the
    // deployment tile below uses a different one — both said outright, because
    // side by side the two invite exactly the wrong subtraction.
    expect(title).toMatch(/Shares held after assignment are NOT/);
    expect(title).toMatch(/FC-103/);
    expect(title).toMatch(/DIFFERENT/);
    expect(title).toMatch(/dollar-weighted over ALL decision days/);
  });
});

describe('the fill label (review round 1, F4)', () => {
  const labelOf = () => screen.getByTestId('fill-label').textContent;
  /** What the 13cc run's engine default actually was, per the captured object. */
  const ENGINE_DEFAULT = 'fill: mid · haircut 25% (engine default)';

  it('reads the CELL ARTIFACT first — base/GOOGL/fit', () => {
    show('base', 'fit');
    // `provenance.fill` on the captured object: {basis: "mid", fill_haircut:
    // 0.25}. The spec declares no haircut for any arm on this run, so the 0.25
    // is the engine's own default and the label says so.
    expect(labelOf()).toBe(ENGINE_DEFAULT);
  });

  it('an arm EXCLUDED from the forecast still gets its own artifact’s label', () => {
    // `position_20pct` is `insufficient` in both windows, so the forecast
    // excludes it entirely and carries no `symbols` entry for it. Reading the
    // forecast first printed a dash for exactly the arm an operator is most
    // likely to be interrogating; the artifact is the object whose ledger is on
    // screen, so it answers first — and the answer is byte-identical to base's,
    // because the same engine default applied to both.
    const armArtifact = normaliseArtifact({
      ...artifact13cc,
      provenance: { ...artifact13cc.provenance, scenario: 'position_20pct' },
    })!;
    show('position_20pct', 'fit', { artifact: armArtifact });
    expect(labelOf()).toBe(ENGINE_DEFAULT);
  });

  it('falls back to the forecast when there is no artifact', () => {
    show('base', 'fit', { artifact: null });
    expect(labelOf()).toBe(ENGINE_DEFAULT);
  });

  it('never invents a basis, and calls an undeclared haircut what it is', () => {
    // No artifact and no forecast: the basis has NO source, so it prints `—`.
    // The mutation this kills is the old default of `'mid'` — right today
    // because the engine's default is mid, and silently wrong the day it is
    // not. The haircut is a different case: the spec declaring none does not
    // mean "no haircut", it means the engine's default applied.
    show('base', 'fit', { artifact: null, report: { ...report, forecast: null } });
    expect(labelOf()).toBe('fill: — · haircut engine default');
  });

  it('prints a DECLARED haircut as declared, with no engine-default suffix', () => {
    show('base', 'fit', {
      artifact: null,
      report: { ...report, forecast: null, scenario_fill_haircuts: { base: 0.4 } },
    });
    expect(labelOf()).toBe('fill: — · haircut 40%');
  });

  it('is amber, like the grid’s own fill-model label', () => {
    show('base', 'fit');
    expect(screen.getByTestId('fill-label').className).toMatch(/text-amber-/);
  });
});

describe('the digest tiles', () => {
  it('renders them with their source and the derived fee rate', () => {
    show('base', 'fit');
    expect(screen.getByTestId('tile-gross-premium').textContent).toMatch(/from ledger/);
    // $0.040/contract — 1.68 / 42 over the fee-BEARING set. $0.047 is what the
    // mismatched sets give, and is the number this label must never print.
    expect(screen.getByTestId('tile-fees').textContent).toMatch(/\$0\.040\/contract/);
    expect(screen.getByTestId('tile-fees').textContent).not.toMatch(/0\.047/);
    // No `+` prefix: a share of the account cannot be negative, and borrowing
    // the signed-return formatter made it read as a gain (review round 1, F9).
    expect(screen.getByTestId('tile-deployment').textContent).toMatch(/24\.4%/);
    expect(screen.getByTestId('tile-deployment').textContent).not.toMatch(/\+24\.4%/);
    expect(screen.getByTestId('tile-deployment').textContent).toMatch(/over 189 decision days/);
    expect(screen.getByTestId('digest-tiles').textContent).toMatch(
      /display only, never ranked or compared/,
    );
  });

  it('says "at cost" when there is no sidecar to value shares with', () => {
    show('base', 'fit', { digest: computeDigest(artifact, null) });
    expect(screen.getByTestId('tile-deployment').textContent).toMatch(/at cost/);
  });

  it('suppresses the RATIO tile on an artifact with no stamped capital base', () => {
    const cc = normaliseArtifact({
      ...artifactA48d,
      provenance: { ...artifactA48d.provenance, strategy: 'covered_call' },
    })!;
    show('base', 'fit', { digest: computeDigest(cc, null) });
    expect(screen.queryByTestId('tile-deployment')).toBeNull();
    expect(screen.getByTestId('ratios-suppressed').textContent).toMatch(/not stamped/);
    // The dollar tiles survive — they need no denominator.
    expect(screen.getByTestId('tile-gross-premium')).toBeTruthy();
  });

  it('shows the endpoint’s own words when the artifact is absent', () => {
    show('base', 'fit', {
      digest: null,
      digestAbsence: 'No detail artifact for base/GOOGL/fit in run 13cc.',
    });
    expect(screen.getByTestId('digest-absent').textContent).toBe(
      'No detail artifact for base/GOOGL/fit in run 13cc.',
    );
    expect(screen.queryByTestId('tile-gross-premium')).toBeNull();
    // …and the ENGINE tiles are unaffected: the row is still the row.
    expect(screen.getByTestId('tile-annualized')).toBeTruthy();
  });
});
