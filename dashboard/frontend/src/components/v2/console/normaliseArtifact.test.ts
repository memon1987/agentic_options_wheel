// FC-096 Phase E PR-2: `normaliseArtifact` against the REAL captured objects.
//
// Both fixtures are live captures (plan §Fixtures), and they are here for
// different reasons: `13cc` is post-PR-1 and pins the full path (stamps,
// sidecar), `a48d` is pre-PR-1 and pins the DEGRADE path — no `benchmark` key
// at all, no `provenance.capital_base`, `git_commit` null. A hand-written
// "pre-PR-1" object would have been written by someone who already knew the
// answer; these were written by the engine.

import { describe, expect, it } from 'vitest';
import artifact13cc from '../../../test/fixtures/artifact_13cc_base_googl_fit.json';
import artifactA48d from '../../../test/fixtures/artifact_a48d_base_googl_fit.json';
import bars13cc from '../../../test/fixtures/bars_13cc_googl_fit.json';
import {
  artifactStrategy,
  isWheelStrategy,
  normaliseArtifact,
  parseArtifact,
  parseBars,
} from './normaliseArtifact';

describe('fixture provenance', () => {
  // A hand-edited fixture is the failure mode this catches: the file name says
  // which live run it came from, and the object says it too. If someone tunes a
  // number to make a test pass, the two stop agreeing.
  it('each artifact fixture carries the run id its filename names', () => {
    expect(artifact13cc.provenance.run_id).toBe('13cc2729d1c74211');
    expect(artifactA48d.provenance.run_id).toBe('a48d7bb064194e0f');
    expect(bars13cc.provenance.run_id).toBe('13cc2729d1c74211');
  });
});

describe('normaliseArtifact — the post-PR-1 capture', () => {
  const parsed = parseArtifact(artifact13cc);

  it('parses, with the shapes the live run actually produced', () => {
    expect(parsed.reason).toBeNull();
    const a = parsed.value!;
    expect(a.daily).toHaveLength(189);
    expect(a.ledger).toHaveLength(72);
    expect(a.cycles).toHaveLength(23);
    expect(a.roll_records).toHaveLength(6);
    expect(a.provenance.symbol).toBe('GOOGL');
    expect(a.provenance.window.first_decision_day).toBe('2025-09-02');
    expect(a.provenance.window.last_decision_day).toBe('2026-06-02');
  });

  it('keeps the PR-1 stamps rather than defaulting them', () => {
    const a = parsed.value!;
    expect(a.provenance.capital_base).toBe(100_000);
    expect(a.provenance.git_commit).toBe('79a2df3d5d3a617cccba9c706cb3cf130c32f889');
    expect(a.benchmark).not.toBeNull();
    expect(a.benchmark!.shares).toBe(473);
    expect(a.benchmark!.final_value).toBeCloseTo(171_484.49, 2);
  });
});

describe('normaliseArtifact — the pre-PR-1 capture degrades honestly', () => {
  const a = normaliseArtifact(artifactA48d)!;

  it('parses the same body shape', () => {
    expect(a.daily).toHaveLength(189);
    expect(a.ledger).toHaveLength(72);
    expect(a.cycles).toHaveLength(23);
  });

  it('leaves an absent stamp ABSENT — not null, not zero', () => {
    // `undefined` is the load-bearing value: `'capital_base' in provenance` is
    // then a true statement about the STORED OBJECT. A `null` here would be a
    // statement about the parser, and a `0` would silently become a denominator.
    expect(a.provenance.capital_base).toBeUndefined();
    expect('benchmark' in a).toBe(false);
    expect(a.provenance.git_commit).toBeNull();
    expect(a.provenance.starting_cash).toBe(100_000);
  });
});

describe('normaliseArtifact — refusals and survivals', () => {
  it('an unknown ledger kind SURVIVES with its raw string', () => {
    // Phase C will write `synthetic_lot_open`. A build that predates it must
    // show the event, not drop it: a dropped event is a ledger that silently
    // does not add up.
    const raw = {
      ...artifact13cc,
      ledger: [
        ...artifact13cc.ledger,
        {
          date: '2026-06-02',
          kind: 'synthetic_lot_open',
          underlying: 'GOOGL',
          symbol: 'GOOGL',
          contracts: 0,
          shares: 100,
          price: 361.85,
          cash_delta: -36_185,
          fees: 0,
          detail: {},
        },
      ],
    };
    const a = normaliseArtifact(raw)!;
    expect(a.ledger).toHaveLength(73);
    expect(a.ledger[72].kind).toBe('synthetic_lot_open');
    expect(a.ledger[72].shares).toBe(100);
  });

  it('a schema this build does not read returns null AND a reason', () => {
    const result = parseArtifact({ ...artifact13cc, schema: 2 });
    expect(result.value).toBeNull();
    expect(result.reason).toMatch(/schema 2/);
    expect(result.reason).toMatch(/schema 1 only/);
  });

  it('refuses an object with no schema and one with no provenance', () => {
    expect(parseArtifact({ provenance: {} }).value).toBeNull();
    expect(parseArtifact({ schema: 1 }).value).toBeNull();
    expect(parseArtifact('not an object').value).toBeNull();
  });

  it('a missing array becomes [] rather than a crash or an undefined', () => {
    const a = normaliseArtifact({ schema: 1, provenance: artifact13cc.provenance })!;
    expect(a.ledger).toEqual([]);
    expect(a.cycles).toEqual([]);
    expect(a.roll_records).toEqual([]);
    expect(a.daily).toEqual([]);
  });
});

describe('parseBars — the real sidecar', () => {
  const parsed = parseBars(bars13cc);

  it('reads the curve off `buy_and_hold.daily`, and the count off provenance', () => {
    expect(parsed.reason).toBeNull();
    const b = parsed.value!;
    expect(b.bars).toHaveLength(189);
    expect(b.buy_and_hold!.daily).toHaveLength(189);
    // `bars_in_window` lives UNDER `provenance` on the real object; reading it
    // from the top level would have silently produced null for ever.
    expect(b.provenance.bars_in_window).toBe(189);
    expect(b.provenance.data_from).toBe('2025-07-03');
  });

  it('the curve ends at the engine-scored final value', () => {
    const b = parsed.value!;
    const last = b.buy_and_hold!.daily[188];
    expect(last.value).toBeCloseTo(b.buy_and_hold!.final_value, 2);
    expect(last.value).toBeCloseTo(171_484.49, 2);
    // …and equals the CELL's own benchmark stamp, which is the cross-check the
    // console uses before it will draw the curve at all.
    expect(last.value).toBeCloseTo(artifact13cc.benchmark.final_value, 2);
  });

  it('refuses a bars schema it does not read', () => {
    const result = parseBars({ ...bars13cc, schema: 9 });
    expect(result.value).toBeNull();
    expect(result.reason).toMatch(/schema 9/);
  });

  it('a null benchmark is a null curve, not an empty one', () => {
    const b = parseBars({ ...bars13cc, buy_and_hold: null }).value!;
    expect(b.buy_and_hold).toBeNull();
  });
});

describe('artifactStrategy — absence means wheel', () => {
  const a = normaliseArtifact(artifact13cc)!;

  it('reads the spec first, the stamp second, and defaults to wheel', () => {
    expect(artifactStrategy('covered_call', a)).toBe('covered_call');
    expect(artifactStrategy(null, a)).toBe('wheel');
    const stamped = normaliseArtifact({
      ...artifact13cc,
      provenance: { ...artifact13cc.provenance, strategy: 'covered_call' },
    })!;
    expect(artifactStrategy(null, stamped)).toBe('covered_call');
    // The spec wins over the stamp — canonical by omission (§Degrading for CC).
    expect(artifactStrategy('wheel', stamped)).toBe('wheel');
  });

  it('only `wheel` may fall back to starting_cash', () => {
    expect(isWheelStrategy('wheel')).toBe(true);
    expect(isWheelStrategy('covered_call')).toBe(false);
  });
});
