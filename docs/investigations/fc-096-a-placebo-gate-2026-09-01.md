# FC-096 Phase A — §5 gate (b): does the DTE knob move selection? (2026-09-01)

**Question.** `put_target_dte` is a CAP, and the scanner's attractiveness score prefers
shorter DTE; both plan reviews warned a DTE-14 config might keep selecting ~7-DTE
contracts and read as "DTE doesn't matter" — a placebo knob. PR-2 (opening the
allowlist) was gated on measuring this first.

**Method** (plan rev 3 §5(b)): two `--command backtest` runs on GOOGL,
2026-03-02 → 2026-08-28 (179 sessions), against the widened 22-reach lake
(`CHAIN_LAKE_BUCKET` set locally, warm reads): live `config/settings.yaml`
(`put_target_dte: 7`) vs the same file with `put_target_dte: 14` only.

**Result — the knob moves selection.**

| | live (DTE 7) | DTE 14 |
|---|---|---|
| cycles | 11 | 7 |
| longest cycle | 20 d (called away) | 29 d (called away) |
| cycle-length profile | 3–7 d puts, one 20 d | 4, 9, 29, 4, 6, 3 d + one OPEN at window end ($2,056 premium) |
| annualized return | 11.5 % | 7.3 % |
| total P&L | $5,652 | $3,583 |
| verdict | marginal | marginal |

The first cycle is identical (4 d, $141 — the short contract scored best both
times); divergence begins at cycle 2 (7 d vs 9 d) and compounds. Longer-dated
contracts are chosen when they score, so the cap is a real lever, not a placebo;
the scorer's short-DTE preference remains visible in the 3–6 d cycles.

**Do not read the return delta as a DTE verdict.** One symbol, one window,
in-sample, and DTE > 7 quotes carry the sparse-print ladder-hole caveat
(`DTE_REACH_BIAS`, PR-2). The gate asked only whether rows move. They do.

**Decision:** gate (b) PASSED → PR-2 proceeds. Artefacts (JSON/markdown of both
runs) were one-shot scratch and are not committed; the numbers above are the
record.
