# Execution Plans

Published plans for changes to this codebase. **No medium or large change should land without a plan file here** — see `docs/CLAUDE.md` ("Plan-First Development") for the rule.

## How plans flow in

1. An idea starts as an entry in `docs/FUTURE_CONSIDERATIONS.md` (FC-NNN).
2. When ready to design, draft a plan file here using the template below.
3. Iterate asynchronously — update the plan over time.
4. When the plan is approved, update the FC entry's status to "Plan published" and link the plan file.
5. Execute against the plan. Reference the plan file path in commit messages and PR descriptions.
6. After merge, move the FC entry to "Completed" with a link to the plan and the commit/PR.

## Naming

- File name: `docs/plans/fc-NNN.md` matching the FC entry number (e.g., `fc-006.md` for FC-006)
- This ensures direct traceability between FC entries and their published plans.

## Allocating an FC number — read this first

**Always allocate against `origin/main`, never against your branch's copy of
`docs/FUTURE_CONSIDERATIONS.md`.**

```sh
git fetch origin main
git show origin/main:docs/FUTURE_CONSIDERATIONS.md \
  | grep -oE '^### FC-[0-9]+' | sort -u -t- -k2 -n | tail -1
```

Take the next number after that, and note that concurrent sessions or long-lived
branches may have unmerged claims — grep other active branches too if you are
working alongside someone:

```sh
for b in $(git branch -r --format='%(refname:short)' | grep -v HEAD); do
  git show "$b:docs/FUTURE_CONSIDERATIONS.md" 2>/dev/null \
    | grep -oE '^### FC-[0-9]+' | sed "s|^|$b |"
done | sort -u -k2 -t- | tail -20
```

**Why this exists.** On 2026-07-18 four FC-number collisions occurred in a single
day — FC-032 twice, FC-037 once, and a three-way clash on FC-038/039/040 between
two concurrent sessions — every one of them caused by reading a branch-local
`FUTURE_CONSIDERATIONS.md` that was behind `main`. Two plan files named
`fc-038.md` describing unrelated projects existed simultaneously on different
branches.

**Resolution rule when a collision does happen:** whatever is already on `main`
keeps its number; unmerged branches renumber. If two unmerged branches clash,
first to merge takes the number. Record the renumber in the plan file header so
older commit-message prefixes remain traceable.

**Concurrent sessions must use separate git worktrees.** Two sessions in one
working directory have already caused one session to commit another's
in-progress edits.

## Plan template

Copy `_template.md` in this directory to start a new plan.

## Index

_All plan files, regenerated 2026-08-28 (evening) from each file's `**Status:**` line (pr-body drafts omitted)._

- [fc-006.md](fc-006.md) — Covered Call Rolling Engine (Friday EOW), status: Done
- [fc-007.md](fc-007.md) — Earnings Calendar Service, status: Done
- [fc-010.md](fc-010.md) — Disable Call Stop-Losses, status: Done
- [fc-012.md](fc-012.md) — Shift dashboard logging to Alpaca queries where authoritative, status: Done
- [fc-013.md](fc-013.md) — Earnings Blackout Gate on the Live Sell Path (rev 2), status: Done
- [fc-018.md](fc-018.md) — Wheel-Centric Dashboard Rebuild (frontend only), status: Done
- [fc-019.md](fc-019.md) — True P&L reconciliation — ingest JNLC/OPTRD and surface share-side P&L, status: Done
- [fc-020.md](fc-020.md) — FIFO cycle pairing in `wheel_cycles_from_activities`, status: Draft
- [fc-021.md](fc-021.md) — FC-021 — Synthetic activity correction for Alpaca paper-engine silent settlements, status: Done
- [fc-022.md](fc-022.md) — FC-022 — Trade Log + ET timezone + By-Symbol summary table, status: Done
- [fc-023.md](fc-023.md) — Per-symbol Realized P&L reconciliation, status: Done
- [fc-024.md](fc-024.md) — ACB walk view rewrite, status: Done
- [fc-025.md](fc-025.md) — FC-025 — Synthetic activity correction for AMZN silent paper-engine assignment, status: Done
- [fc-026.md](fc-026.md) — Decision Quality — surface Premium Received / Captured / Foregone, status: Done
- [fc-027.md](fc-027.md) — Cycle Table — separate Total Premium from Cycle P&L, status: Done
- [fc-029.md](fc-029.md) — FC-029 — Wheel strategy Phase 1 risk re-tune (call delta + cost-basis floor + drawdown pause), status: Done
- [fc-030.md](fc-030.md) — Drawdown-pause alerting — operator notification for extended pauses, status: Done
- [fc-031.md](fc-031.md) — Dashboard metrics overhaul — vetted portfolio metrics + bot execution health, status: Done
- [fc-032.md](fc-032.md) — Backtesting Engine Overhaul — Symbol Wheel-Fitness Evaluation, status: Done
- [fc-035.md](fc-035.md) — Delete the `poll_order_statuses` path and the `order_statuses` table, status: Done
- [fc-036.md](fc-036.md) — FC-036 — Fix the dead Stage-4 execution gap check (study first, then fix), status: Done
- [fc-038.md](fc-038.md) — Two-pool execution selection — covered calls stop competing for cash they don't need, status: Done
- [fc-041.md](fc-041.md) — FC-041(2) — dotted-ticker normalization + a parser-independent naked-call assertion, status: Done
- [fc-042.md](fc-042.md) — Backtest Engine Follow-on — Performance, Fidelity, and the Filter Studies, status: Done
- [fc-043.md](fc-043.md) — Fix `AlpacaClient.get_orders` — the status filter has never worked, status: Done
- [fc-048.md](fc-048.md) — FC-048 — Route execution on the contract, not a defaulted dict key (backtests model half a wheel), status: Done
- [fc-050.md](fc-050.md) — Restore the covered-call below-basis floor on the path production actually runs, status: Done
- [fc-060-chain-lake.md](fc-060-chain-lake.md) — FC-060 Layer 1 — the chain lake (GCS-backed, write-through `ChainStore`), status: Done
- [fc-065.md](fc-065.md) — One floor, one path, one decision record — the covered-call gating layer, status: Done
- [fc-068.md](fc-068.md) — Delete the dead engine call path; repoint the backtest to the real pipeline, status: Done
- [fc-069.md](fc-069.md) — FC-069 — the decommission sweep. 15 decision cards for operator sign-off, status: DONE
- [fc-071.md](fc-071.md) — FC-071 — at-floor scoring bonus aligned to the gate (`>=`), status: Done
- [fc-072.md](fc-072.md) — FC-072 — Price sell-to-open limits off a *fresh* mid; snap ticks; stop donating the discount (rev 2), status: Done
- [fc-075-cc-deploy-step.md](fc-075-cc-deploy-step.md) — FC-075 follow-on — CC service deploy step in cloudbuild + first tunables iteration (14 DTE), status: DONE
- [fc-075-phase-2.md](fc-075-phase-2.md) — FC-075 Phase 2 — the covered-call engine (file-level build spec), status: DONE
- [fc-075-seam-4.md](fc-075-seam-4.md) — FC-075 Seam 4 — BigQuery write-side dataset threading (+ `strategy_id` column, DD-7 removal), status: DONE
- [fc-075.md](fc-075.md) — Standalone covered-call strategy — separate account, shared machinery, status: LIVE
- [fc-078.md](fc-078.md) — Minimal roller revival — credit-only defensive rolls, daily evaluation, status: Done
- [fc-079.md](fc-079.md) — FC-079 — Rewire the last OCC-substring sites on the reconcile paths (absorbs FC-054), status: Done
- [fc-081.md](fc-081.md) — FC-081 follow-up — merged-vs-deployed freshness check + alert, status: Done
- [fc-084.md](fc-084.md) — FC-084 — Serialize builds per trigger; pin the smoke test to a build-owned revision; promote with `--to-latest`, status: Done

