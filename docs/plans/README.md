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

_List active plans here as they are added._

- [fc-006.md](fc-006.md) — FC-006: Covered call rolling engine, status: done
- [fc-007.md](fc-007.md) — FC-007: Earnings calendar service, status: done
- [fc-010.md](fc-010.md) — FC-010: Disable call stop-losses, status: done
- [fc-012.md](fc-012.md) — FC-012: Shift dashboard logging to Alpaca queries, status: done
- [fc-013.md](fc-013.md) — FC-013: Gate health audit & earnings blackout symmetry, status: done (PR #81, merged 2026-08-03)
- [fc-018.md](fc-018.md) — FC-018: Wheel-centric dashboard rebuild (frontend only), status: draft
- [fc-019.md](fc-019.md) — FC-019: True P&L reconciliation (JNLC + OPTRD ingest), status: done
- [fc-020.md](fc-020.md) — FC-020: FIFO cycle pairing for overlapping share lots, status: draft
- [fc-030.md](fc-030.md) — FC-030: Drawdown-pause alerting (operator notification for extended pauses), status: done
- [fc-031.md](fc-031.md) — FC-031: Dashboard metrics overhaul (vetted portfolio metrics + bot execution health), status: done
- [fc-032.md](fc-032.md) — FC-032: Backtesting engine overhaul (symbol wheel-fitness evaluation), status: draft
- [fc-038.md](fc-038.md) — FC-038: Two-pool execution selection (covered-call phantom-collateral fix), status: done
- [fc-050.md](fc-050.md) — FC-050: Restore the covered-call below-basis floor on the production path, status: done
- [fc-065.md](fc-065.md) — FC-065: One floor, one path, one decision record (covered-call gating layer), status: draft — awaiting review
- [fc-075.md](fc-075.md) — FC-075: Standalone covered-call strategy (separate account, shared machinery), status: executing — Phases 0–2 done, Seam 4 next
- [fc-075-phase-2.md](fc-075-phase-2.md) — FC-075 Phase 2: the covered-call engine (call-only gating of the shared pipeline), status: done (PR #89)
