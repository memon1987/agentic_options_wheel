# Plan: <Title>

**FC entry:** `docs/FUTURE_CONSIDERATIONS.md` FC-NNN
**Plan file:** `docs/plans/fc-NNN.md`
**Status:** Draft | In review | Approved | Executing | Done
**Scope:** wheel | covered_call | shared  (required — must match the FC entry's tag)
**Size:** S | M | L
**Author:** <name>
**Last updated:** YYYY-MM-DD

---

## Context

Why this change is being made. What problem or opportunity it addresses, what prompted it, and what the intended outcome is. Include a link to the FC entry and any relevant evals, logs, or incidents.

## Goals

- Goal 1
- Goal 2

## Non-goals

- Explicitly out of scope
- Things we are deliberately not changing

## Proposed approach

The recommended approach. Be specific: name files, functions, data flows, config keys. Avoid listing every alternative — summarize the chosen path and why.

## Critical files

- `path/to/file.py` — what changes here and why
- `path/to/other.py` — ...

## Reuse

Existing utilities/functions that should be reused rather than re-implemented.

- `src/...` — ...

## Risks & mitigations

- Risk: ...
  - Mitigation: ...

## Rollout

- Deployment order
- Feature flag / dry-run strategy
- Monitoring / dashboards to watch
- Rollback plan

## Verification

End-to-end test plan:

- [ ] Unit tests: ...
- [ ] Integration / manual test: ...
- [ ] Production monitoring for N days after deploy: ...

## Open questions

- ...

## Execution

_Filled in after implementation is complete._

- **PR:** https://github.com/owner/repo/pull/NNN
- **Commit:** `abc1234`
- **Date:** YYYY-MM-DD
- **Notes:** Any deviations from plan, issues encountered, or follow-up items.
