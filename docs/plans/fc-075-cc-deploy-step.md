# Plan: FC-075 follow-on — CC service deploy step in cloudbuild + first tunables iteration (14 DTE)

**FC entry:** FC-075 (Phase 3 §deploy follow-up + OQ-1 tunables iteration)
**Plan file:** `docs/plans/fc-075-cc-deploy-step.md`
**Status:** Executing — PR [#91](https://github.com/memon1987/agentic_options_wheel/pull/91); two adversarial reviews dispositioned (see §Review disposition)
**Scope:** covered_call (primary; the cloudbuild edit touches the shared deploy pipeline — wheel/dashboard steps must be byte-identical, per Behavior contract)
**Size:** S (~45 lines cloudbuild.yaml + 1 value in covered_call.yaml; no src/ changes)
**Author:** Claude (Fable), 2026-08-27. **Builder:** Fable (deviation from the model table, noted openly: two mirrored yaml blocks + one config value — mechanical, single-right-answer work; Opus handoff adds ceremony without judgment). **Reviews:** two adversarial (Fable, fresh contexts) — cloudbuild is deploy config with FC-031/FC-081 history; no exemption.

## Context

1. **The CC service image is pinned** to the manually-deployed `27308f4` build (Phase 3 provisioning, 2026-08-23). Every merge to main since then deploys the wheel + dashboard but not `covered-call-engine` — the image drifts from main until a deploy step exists. Known follow-up since provisioning day.
2. **Operator decision 2026-08-27 (binding): `call_target_dte` 7 → 14** on the covered-call profile. Grounded in three days of live decision rows: UNH traded *above* basis on 08-26 and still zero-qualified — strikes near spot fail delta ≤ 0.25, strikes far enough for the band pay < $0.30 at ≤ 7 DTE. The delta band and premium floor have an empty intersection on a 7-DTE chain at current IV; extending DTE raises premium at unchanged delta. Mechanics verified: `call_target_dte` is the hard DTE **ceiling** (`market_data.py:811-813`, no minimum), so 14 strictly widens the candidate window; monitor-close handles > 7-DTE positions via `default_long_dte_target: 0.50`; earnings-lookahead validation holds (90 ≥ 14 + 7, `config.py:221-228`); the FC-013 span gate is DTE-invariant by design. Wheel untouched (`settings.yaml` keeps 7).

These ship as **one PR** because the cloudbuild step *is* the delivery mechanism for the yaml change: merge → trigger builds main → new cloudbuild deploys all three services → CC picks up 14 DTE. No manual redeploy, and the pinned-image gap closes permanently.

## Changes

**`config/covered_call.yaml`** — `strategy.call_target_dte: 7` → `14`, comment updated with the decision date + rationale pointer (this plan). Nothing else.

**`cloudbuild.yaml`** — three new steps after `promote-bot` (wheel-first ordering: CC deploys only if the wheel's canary promoted — the wheel is the earlier tripwire for a bad image), mirroring the wheel's existing canary → poll → promote pattern exactly:

1. `deploy-cc-canary`: `gcloud run deploy covered-call-engine --image=<strategy image>:$COMMIT_SHA --region=us-central1 --memory=512Mi --concurrency=10 --max-instances=1 --min-instances=0 --timeout=300 --set-env-vars=ALPACA_PAPER_TRADING=true,GCP_PROJECT=$PROJECT_ID,STRATEGY_CONFIG=config/covered_call.yaml --no-traffic --tag=canary`. `waitFor: ['promote-bot']`.
2. `smoke-test-cc`: the wheel's poll-for-ready loop verbatim with the service name swapped (30 × 5s; `False` fails fast; the poll exists because a single-shot check raced revision startup — observed 2026-07-18).
3. `promote-cc`: `update-traffic --to-latest`. `waitFor: ['smoke-test-cc']`.

**Env/secrets contract (the FC-081-class gotcha, stated explicitly):** `--set-env-vars` replaces the *literal* env-var set — the three values listed are the complete intended literal set and match what the service runs today. Secret-backed env vars (`ALPACA_API_KEY`/`ALPACA_SECRET_KEY` → `-cc` secrets, `FINNHUB_API_KEY`) are a separate list that `--set-env-vars` does not touch — the same verified behavior the wheel has relied on across every deploy since inception. The deploy step must NOT pass `--set-secrets` (it would be redundant at best; a typo'd secret name breaks the service).

## Behavior contract

- Wheel + dashboard deploy flow: byte-identical steps, unchanged ordering; total build gains ~1–3 min.
- CC service: every main merge now deploys it with the same image as the wheel; env vars re-asserted to the known-good literal set; secrets untouched; account interlock + `/health` unchanged.
- CC scan behavior after first deploy: DTE window widens to ≤ 14; expect `dte_too_high` reason-counts to collapse and (per the squeeze analysis) UNH-class chains to start qualifying when delta/premium intersect in week-2 expiries. No other gate changes.
- Rollback: revert the commit → next build redeploys previous behavior; the CC service itself keeps serving its last revision throughout any build failure (canary is `--no-traffic`).

## Tests / verification

- Pre-merge: both yamls parse; full pytest suite green (config validation tests cover the yaml load + earnings-lookahead rule); no test asserts `call_target_dte == 7` on the CC profile (verify by grep — if one does, it's asserting a tunable and gets updated to read the config).
- Post-merge (execution record): build runs all new steps green; CC revision serves `$COMMIT_SHA` image; `/health` healthy; next market-hours scan's `reason_counts` show the `dte_too_high` collapse; secrets still bound (interlock passing proves Alpaca creds; earnings gate events prove Finnhub).

## Risks

- **Bad cloudbuild syntax breaks all deploys** (the trigger runs this file for every merge). Mitigated: steps are copies of proven blocks; two reviews; canary pattern means a bad CC deploy never takes traffic; wheel promotes before CC deploys.
- **Env replacement drift**: if someone later adds a literal env var to the CC service out-of-band, the next build wipes it — same standing property as the wheel (documented gotcha); the fix is always "declare it in cloudbuild.yaml".
- **14-DTE positions live longer**: slower theta, fewer cycles, longer exposure per position — the operator's accepted trade. Earnings span gate still bounds every candidate.

---

## Review disposition (2026-08-27) — PR #91

Two adversarial reviews (Fable, fresh contexts: senior CI/CD-release engineer; senior options trader). **Both REQUEST_CHANGES; both affirmed the design and found the code faithful to the plan** (CI/CD lens verified the env/secrets contract against the *live* service and the step graph programmatically; trader lens proved the ranking math keeps week-1 preferred whenever it qualifies, so 14 is a true fallback). Union of findings and dispositions:

- **Required — build `timeout` 900s → 1200s (both reviewers, independently):** measured builds already run 8–12 min; the serialized CC chain adds up to ~4 min worst-case → timeout-on-every-merge risk (FC-031 class). The plan estimated "+1–3 min" but never checked it against the ceiling — a genuine plan gap. **Fixed in `aa54fa1`.**
- **Required — sequence the merge behind FC-083:** at filing time the PR's "merging deploys it" claim was false — main's suite was red (date-rotted earnings tests) and cloudbuild's test gate would have killed the merge build at step 0. Found and fixed mid-review (FC-083, PR #92, `88a6288`); **merge gate: main's builds for `88a6288`/`861a473` must show SUCCESS first.** The reviewer's sharpest framing is recorded verbatim: the red suite was "the delivery mechanism's off switch," and the PR had treated it as out of scope.
- **Required — band-cliff disposition (trader lens, the review's best find): ACCEPTED, in writing.** `dte_bands` encode day-of-life intent but key on DTE; valid only at 7-DTE entry. A 14-DTE position runs its first week at the 0.50 fallback, then crossing DTE 8→7 drops the target to 0.35 — the "just-opened anti-churn" band firing mid-life, making exits at ≥35% gain around day 7 likely. **Accepted as-is for now:** the behavior is profit-taking-early, not risk-adding; bands are OQ-1 operator-tunable and the right retune (day-of-life bands, or extending bands to 14) should be chosen from live 14-DTE decision data, not guessed. Revisit after the first weeks of 14-DTE exits.
- **Noted, no action:** unclamped `default_long_dte_target` path (inert at 0.50; escapes `[min,max]` bounds only if retuned — remember when touching); `_parse_dte_from_symbol` error-fallback of 7 mis-bands a 14-DTE position toward earlier exit (fail-safe direction); CC has no defensive roller so an ITM 14-DTE call pins shares up to 2 weeks (bounded — floor guarantees profitable assignment); post-deploy expectation set correctly: **watch `dte_too_high` collapse and re-file under other reasons — "UNH qualifies" is a hope, not an implication**; comment nit fixed in `aa54fa1`.
- **Follow-up filed — FC-084:** the smoke-test/promote pattern identifies "my canary" as `revisions list --limit=1` and promotes `--to-latest` — a newest-revision assumption shared by all three services' chains. Live evidence same day: two main builds started 3 seconds apart; build A can validate and promote build B's revision. Inherited, not introduced here; fix is pinning to the deployed revision name.

## Execution

- **PR:** https://github.com/memon1987/agentic_options_wheel/pull/91 (`024e4d5` + review fixes `aa54fa1`)
- **Merge gate:** main builds `88a6288`/`861a473` SUCCESS + scoped confirmation pass on `aa54fa1`.
- Post-merge verification per §Tests: build runs the CC chain green, CC revision serves the merge SHA, next scan's `reason_counts` shows the `dte_too_high` collapse.
