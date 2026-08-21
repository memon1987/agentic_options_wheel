# Plan: FC-075 Phase 2 — the covered-call engine (file-level build spec)

**FC entry:** `docs/FUTURE_CONSIDERATIONS.md` FC-075
**Parent plan:** `docs/plans/fc-075.md` (architecture, isolation seams, policy decisions — all carried forward, not restated)
**Plan file:** `docs/plans/fc-075-phase-2.md`
**Status:** DONE — merged 2026-08-21 (PR #89, squash `2e08d0a`)
**Size:** M (~200 net production lines across 7 existing files; no new source file)
**Author:** Claude (Fable), design pass 2026-08-19 against `main` @ `f84b50a`
**Builder:** Opus. **Reviews:** two adversarial (Fable, fresh contexts); production trading logic, so at least one reviewer gets live-data access per the stakes calibration.

---

## Context

The parent plan deferred this phase's file-level spec to a design pass gated on FC-068 + FC-069; both are DONE (FC-068 deleted the dead engine path and repointed the backtest; FC-069's sweep executed in full 2026-08-04, incl. the WheelStateManager shrink and the scanner OCC rewire). This document is that design pass. The finding that shapes everything: **the covered-call strategy is mechanically the wheel's call leg run standalone, and the wheel's call leg already exists end-to-end** — holdings-derived scan with the `avg_entry_price` floor (`OptionsScanner.scan_for_call_opportunities`), two-pool share-ledger selection with the empty-put-pool degenerate case (`ExecutionEngine`), execute-time floor gate (`CallSeller.execute_call_sale`), and `/monitor` early close. Phase 2 is therefore **not a new engine**; it is call-only gating of the existing `/scan → /run → /monitor` pipeline, an inventory validator expressed through the existing chain-criteria machinery, strategy-aware BigQuery *read* routing, and the wheel-neutrality proof for all of it. Phase 1's isolation (strategy profiles, `strategy_config()`, account interlock, strategy-keyed `OpportunityStore`, `config/covered_call.yaml`) is merged and live.

## Design decision 1 — engine shape: NO `covered_call_engine.py`

**Decision:** build no new engine file and no state manager. The Cloud Run request handlers (`/scan`, `/run`, `/monitor`) already ARE the orchestrator the parent plan's "thin orchestrator" sketch imagined; every stage between them is call-capable today and strategy-agnostic except for four wheel-only branches (put scan, put pool, wheel-state reconcile housekeeping, wheel-dataset BQ reads). Phase 2 gates those four branches on `config.strategy_id` and adds nothing else structural.

- **Stateless confirmed against current code.** Production has no persistent strategy state: `WheelStateManager` (post-FC-069 S6, 331 lines) is constructed fresh per request inside `WheelEngine` and dies with it; `/run`'s trading path derives everything from the opportunity blob + a single Alpaca positions snapshot (`deploy/cloud_run_server.py:651`); `/monitor` derives everything from live positions. A covered-call state manager would re-create the fiction FC-069 just deleted.
- **Rejected alternative:** new `src/strategy/covered_call_engine.py` wrapping scan/select/execute. It would need its own endpoint wiring, its own decision-record plumbing, and its own copy of the `/run` safety rails (idempotency filter, non-retryable filter, `RunDecisionFlusher`, mark-executed) — a duplicate engine, the thing requirement #2 forbids. The sketch in the parent plan predates verifying how complete the shared pipeline already is; the parent plan itself instructed this pass to re-derive the shape (its OQ-1).
- **Rejected alternative:** a `trades_puts: false` config knob. A strategy's legs are intrinsic to its identity, not a tunable — `strategy_id` is already required, validated, and fail-closed (Phase 1 review hardened exactly this). A second boolean that can contradict the identity key is a new mis-configuration surface with no expressive gain. All gating below keys off `config.strategy_id != 'wheel'`.

## Design decision 2 — call-only scan (the parent plan's OQ-1, now decided)

**Decision:** gate the put leg inside the scanner method itself, not in the server route.

- **`src/data/options_scanner.py` — `scan_for_put_opportunities` (:122):** first statement of the method body: if `self.config.strategy_id != 'wheel'`, log one info event (`event_type="put_scan_skipped_non_wheel_profile"`, `strategy_id=...`) and `return []`. Placing the gate at the single chokepoint covers every caller — `/scan` (`deploy/cloud_run_server.py:327`), `scan_all_opportunities` (:625, the `main.py --command scan` CLI path), and any future job — rather than only the HTTP route.
- **Why this is mandatory, not hygiene:** on the covered-call profile, `scan_for_put_opportunities` reaches `self.config.stock_symbols` (:132/:137), whose property hard-indexes `self._config["stocks"]["symbols"]` (`src/utils/config.py:562`) — a section the profile validly omits (profile-aware validation, `config.py:158-160`). Today that raises `KeyError`, is swallowed by the method's own except (:195-205), and emits a `scan_failure` **error** event every scan, ~26×/day of alert noise masquerading as an outage. The gate converts a per-scan error into an explicit, tested no-op.
- **`/scan` needs no change for gating** (`trigger_scan` calls both scans; put scan now self-gates and contributes `[]` to the stored blob). It gains only the exposure alert (Design decision 5).
- **Defense in depth on `/run`:** the blob is strategy-keyed (Seam 1) and the put scan is gated, but "the covered-call service must NEVER sell puts" deserves a second, structural layer at the point of execution. In `trigger_strategy` (`deploy/cloud_run_server.py`), immediately after `blob_opportunities[:] = list(opportunities)` / run-id binding (:541-550): if `config.strategy_id != 'wheel'`, drop every opportunity whose `strict_option_type(opp.get('option_symbol') or '')` is not `'call'`, emitting `log_error_event(error_type="non_call_opportunity_refused", recoverable=True, ...)` per drop. Expected live frequency: zero (it can only fire on a hand-written or corrupted blob); that is what makes it cheap insurance rather than a behavior. Wheel path: branch not taken, byte-identical.

## Design decision 3 — inventory validator + `excluded_symbols`

The operator policy (parent plan, do not reopen): universe = every equity position; controls only exclude or validate, never select; invariant "this account holds nothing you are unwilling to have called away."

**Decision:** no standalone `InventoryValidator` component. The validator materializes as three mechanisms that each hook where the data already is:

1. **`excluded_symbols` — symbol-level skip in the scan loop.** `src/data/options_scanner.py`, inside the `scan_for_call_opportunities` position loop, immediately after `labels` is built (:249-253) and **before** the `shares < 100` check (an excluded symbol must report as excluded, not as under-lotted, and must spend zero further work): if `symbol in self.config.excluded_symbols` → `recorder.record(symbol, OUTCOME_NOT_ELIGIBLE, REASON_EXCLUDED_BY_CONFIG, **labels)`, append to `recorded_symbols`, `continue`. New vocabulary in `src/data/decision_record.py`: `REASON_EXCLUDED_BY_CONFIG = "excluded_by_config"` added to the `OUTCOME_NOT_ELIGIBLE` frozenset (:132). `decision_events.reason` is a STRING column — no schema change.
2. **Chain liquidity + spread width — new config-driven criteria in the existing chain filter.** `src/api/market_data.py` `_check_call_criteria_detailed`, entry profile (:806-822), after the legacy liquidity check: if `self.config.min_open_interest` is set and `call_option['open_interest'] < min_open_interest` → `'open_interest_too_low'`; if `self.config.max_spread_pct` is set and `mid_price > 0` and `(ask - bid) / mid_price > max_spread_pct` → `'spread_too_wide'`. Initialize both counters in `find_suitable_calls`' `rejection_stats` dict (:494-512 — an unknown reason key raises `KeyError` at :550, so this is required, not cosmetic). The counters flow automatically into `last_call_rejection_stats` (:621) → `reason_counts` on the symbol's decision row (REPEATED record, no schema change) — which **is** the "flag/skip + logged, never traded" behavior. Roll profile untouched. Wheel: `settings.yaml` carries neither key → both properties `None` → checks skipped → byte-identical.
   *Rejected alternative:* a symbol-level pre-chain validator. It would need its own chain fetch (the only source of OI/spread), duplicating the one `find_suitable_calls` already makes, and creating a second source of truth for liquidity.
3. **Earnings proximity — the FC-013 gate, reused, not duplicated.** `scan_for_call_opportunities` already implements the covered-call earnings policy: symbol-level fail-closed skip on `EARNINGS_UNKNOWN` (:271-282) and the per-candidate span predicate threaded into `find_suitable_calls` (:366-370). Enable it for the profile: add an `earnings:` block to `config/covered_call.yaml` (`enabled: true`, `lookahead_days: 90` — satisfies the `lookahead >= call_target_dte + 7` validation at `config.py:221-228`). **Delete the `universe.earnings_exclusion_days` key** from `covered_call.yaml`: it has no consumer anywhere (verified by grep), and a symbol-level N-day blackout is the design FC-013 rev 2.2 explicitly rejected for the call leg (a symbol reporting in 3 days may legally sell a call expiring in 2; the span predicate encodes that). Adding a knob that contradicts a settled DD would be a defect, not a feature.
   *Deployment consequence (Phase 3 checklist item):* the covered-call service must bind `FINNHUB_API_KEY`. With the gate enabled and no key, every symbol resolves `EARNINGS_UNKNOWN` and the engine fail-closes into writing **nothing** — safe, loud, and useless.

Optionability needs no code: a non-optionable holding yields an empty chain → the existing `OUTCOME_NO_CANDIDATES` / `REASON_NO_QUALIFYING_STRIKES` decision row (:412-424).

## Design decision 4 — strategy-aware BigQuery READS (a live cross-contamination bug, fixed here)

Two read chokepoints on the scan path hardcode the wheel's dataset, and one of them can **block covered-call trading with wheel data**:

- **`CostBasisResolver._lookup_assignment_basis`** (`src/strategy/cost_basis.py:366-389`) queries `options_wheel.trades_from_activities` with no dataset parameter. On the covered-call service, the divergence cross-check would reconstruct the **wheel account's** put-assignment lots for any shared underlying (both accounts holding AAPL is the expected steady state) and compare them against the covered-call account's `avg_entry_price`. Manual purchases never match assignment-reconstructed lots: the result is `share_count_mismatch` or `basis_mismatch` → `SOURCE_DIVERGENT` → **symbol permanently blocked** (`options_scanner.py:303-321` fail-closes on divergence). This is wrong-strategy data vetoing a correct broker floor.
- **`UncoveredDaysResolver`** (`src/data/decision_record.py:496-499`) already takes `dataset_id` (default `"options_wheel"`) but `OptionsScanner.__init__` constructs it without one (:108-110); on the covered-call service its label would be wheel-derived and wrong (telemetry-only impact).

**Change:** thread `config.bigquery_dataset` (Phase 1 property, `config.py:392-403`, default `options_wheel`) into both:
- `cost_basis.py`: build the query with the dataset interpolated (same pattern as `uncovered_days_sql(dataset_id)`), sourced from `self.config.bigquery_dataset` — the resolver already holds `config` (:149).
- `options_scanner.py:108-110`: `UncoveredDaysResolver(allow_bigquery=allow_bigquery, dataset_id=config.bigquery_dataset)`.

**Why this is safe in every state:** wheel service → property defaults to `options_wheel`, byte-identical. Covered-call service pre-provisioning → the `covered_call` dataset does not exist → query raises → both chokepoints already degrade correctly (`cross_check: unavailable`, floor kept, `cost_basis.py:405-412`; `uncovered_days → None`, `decision_record.py:562-567`). Post-provisioning → `trades_from_activities` in `covered_call` will never contain put assignments (the strategy sells no puts) → cross-check permanently `no_assignment_history` → unavailable → floor stands on the broker number, which is precisely the FC-065 design for a manual-entry book.

**This is not Seam 4.** Seam 4 is the *write* side: the five BQ writers' `dataset_id` defaults + the `strategy_id` column + the live-table ALTER + the `AnalyticsWriter` singleton's dataset source. Nothing here touches a writer, a schema, or the singleton. The read threading is pulled into Phase 2 because it is a trading-decision correctness bug, not analytics routing.

## Design decision 5 — sizing and the 4× margin discipline

**Finding (corrects the brief's framing):** a call-only batch never touches dollars. Calls are sized as `available_shares // 100` (`execution_engine.py:409-427`, `collateral = 0.0`), selected against the per-underlying share ledger (`select_batch` pool 1, :545-575), and the buying-power pool only ever funds puts (pool 2, :577-602) — which Design decision 2 makes structurally empty. `risk.sizing_basis: equity` therefore has **no sizing consumer to build**: the "equity basis" policy is already enforced by shares-only sizing plus the fact that the engine never buys stock. What remains implementable is the alert.

**Change:** in `trigger_scan` (`deploy/cloud_run_server.py`, after the call scan, before storing): if `config.sizing_basis == 'equity'`, fetch `alpaca_client.get_account()` and when `float(long_market_value) > float(equity) * config.max_long_market_value_pct_of_equity`, emit `log_error_event(error_type="long_exposure_exceeds_equity", event_category risk semantics, recoverable=True, long_market_value=..., equity=..., threshold=...)`. **Alert-only — it must not block the scan** (operator decision lineage: FC-065 "operator decisions are binding; floor-only gating, no pause"). The FC-030 alert channel reads Cloud Logging, so an error event is sufficient wiring; adding the alert policy is a Phase 3 item.

New `Config` properties (`src/utils/config.py`), all with wheel-preserving defaults:

| Property | Reads | Default (wheel behavior) |
|---|---|---|
| `excluded_symbols` | `universe.excluded_symbols` | `[]` |
| `min_open_interest` | `universe.min_open_interest` | `None` (check skipped) |
| `max_spread_pct` | `universe.max_spread_pct` | `None` (check skipped) |
| `sizing_basis` | `risk.sizing_basis` | `'buying_power'` (alert never runs) |
| `max_long_market_value_pct_of_equity` | `risk.max_long_market_value_pct_of_equity` | `1.0` |

Validation (in `_validate_config`, only when the keys are present): `excluded_symbols` must be a list of strings; `0 < max_spread_pct <= 1`; `min_open_interest` a non-negative int; `sizing_basis in ('buying_power', 'equity')`.

## Design decision 6 — skip the wheel-state reconcile housekeeping (and with it, the FC-054/079 code)

**Finding (corrects the brief's premise):** the stateless covered-call path **does** traverse the FC-054/079 reconcile code today. `/run`'s pre-trade housekeeping constructs `WheelEngine(config)` and calls `reconcile_positions()` (`deploy/cloud_run_server.py:491-510`), which contains the live OCC-substring sites — `'P' in option_symbol` / `'C' in option_symbol` (`src/strategy/wheel_engine.py:304-307`) and `'C' in opt_sym` + raw `opt_sym[-8:]` strike parsing (:406-413). (The parent plan's `:923/:925/:1027` refs predate the FC-069 shrink; `options_scanner.py:611` was **fixed** by FC-069 item 12 — `_option_position_matches` now uses the canonical parser, :979-1010.)

On the covered-call profile this housekeeping is pure liability: it feeds a per-request, ephemeral `WheelStateManager` that nothing downstream on the trading path reads; every covered-call position on a P-containing ticker (AAPL, PFE, SPY…) would be miscounted put-vs-call by :304-307; and its `log_trade_event` emissions (`put_assignment_from_activity` etc., :196-207/:224-235) would put wheel-shaped trade events from the covered-call account into shared telemetry.

**Change:** gate the whole housekeeping block (`cloud_run_server.py:491-510`) on `config.strategy_id == 'wheel'`; the else-branch logs one info event (`event_type="reconcile_skipped_non_wheel_profile"`). The `WheelEngine` import stays inside the branch. Trading correctness on `/run` is unaffected by the skip: opportunities come from the blob, sizing/selection from the positions snapshot, idempotency from `get_option_positions` — none of it reads wheel state.

**Consequence for the accepted-risk item:** after this gate, the covered-call scan/execute/monitor path does **not** traverse the FC-054/079 sites — safely avoided rather than newly blocked. The fix for those sites remains FC-079's own PR (its entry already mandates a separate two-reviewer PR because it changes live wheel reconcile behavior).

## Journal labeling — FC-067 coordination

Verified current behavior: `ExecutionEngine.execute_batch` journals `{**opp, ...}` (`execution_engine.py:824-831`); scanner call opportunities carry `type: 'call'` but neither `option_type` nor `strategy`, and `TradeJournal.record_trade` defaults exactly those two fields (`src/data/trade_journal.py:148` → `'put'`, `:161` → `'sell_put'`). So **the current call-execution path relies on the poisoned defaults** — FC-067 is about the defaults, and this path is a victim of them (as its entry's 29-row evidence shows). The right fix is FC-067's own (derive `option_type`/`strategy` from the OCC symbol inside `record_trade` via `strict_option_type`/`parse_option_symbol` — retro-fixing every producer), which is in flight as a parallel PR.

**This plan deliberately adds no field-passing in `execute_batch`:** duplicating the derivation at the call site would leave two sources of truth for the same row. Phase 2 instead **depends on FC-067** and pins the outcome with a test: an executed covered-call opportunity journals `option_type='call'`, `strategy='sell_call'` (fake journal capture through `execute_batch`). Ordering: see Dependencies. If FC-067's landed shape differs from its entry's fix direction, the builder adapts the test, not the engine.

## Explicitly NOT built (with the code-verified reason)

- **Rolling.** Out of scope per the parent plan. The roller now works (FC-078; first roll executed 2026-08-04) but v1 stays minimal: early-close + re-write carries the value. `covered_call.yaml` has no `rolling:` block → `rolling_enabled` defaults `False` (`config.py:607`) → `/roll` returns `{'skipped': 'rolling_disabled'}` (`wheel_engine.py:608-609`). Phase 3 must not schedule `/roll` for the service and must not set `ROLLER_ENABLED` on it (the env override wins over yaml, `config.py:595-599`).
- **Stock buying.** Manual entry via the Alpaca UI; the engine adopts holdings (the holdings-derived scan does this inherently). No order-side `buy` path exists for equities in the engine and none is added.
- **A covered-call state manager.** See Design decision 1.
- **Seam 4** (BQ write threading + `strategy_id` column + ALTER + `AnalyticsWriter` dataset source). Own work item; ordering below.
- **`AlpacaClient` structural interlock** (FC-076): `main.py` and future Jobs still bypass the route decorators. Noted, not built here.
- **Backtest profile** (Phase 4): unchanged scope, still after this phase.

## Exact files & functions to touch

All references verified in this tree (`f84b50a`):

| File | Change |
|---|---|
| `src/data/options_scanner.py` | Put-leg gate at top of `scan_for_put_opportunities` (:122); `excluded_symbols` skip in the `scan_for_call_opportunities` loop (before the `shares < 100` check at :256); `UncoveredDaysResolver(..., dataset_id=config.bigquery_dataset)` (:108-110) |
| `src/strategy/cost_basis.py` | `_lookup_assignment_basis` query dataset from `self.config.bigquery_dataset` (:366-389, both table references) |
| `src/data/decision_record.py` | `REASON_EXCLUDED_BY_CONFIG` + add to `OUTCOME_NOT_ELIGIBLE` vocabulary (:105, :132) |
| `src/api/market_data.py` | `open_interest_too_low` / `spread_too_wide` criteria in `_check_call_criteria_detailed` entry profile (:806-822); both counters initialized in `find_suitable_calls` `rejection_stats` (:494-512) |
| `src/utils/config.py` | Five properties + presence-gated validation (Design decision 5) |
| `deploy/cloud_run_server.py` | `/run`: reconcile-housekeeping gate (:491-510) + non-wheel call-only filter (after :541-550); `/scan`: equity-exposure alert in `trigger_scan` |
| `config/covered_call.yaml` | Add `earnings:` block (`enabled: true`, `cache_ttl_hours: 24`, `lookahead_days: 90`); populate `risk.profit_taking.dte_bands` (copy the wheel's call-side bands from `settings.yaml:85+` as starting defaults — today's empty `dte_bands: []` makes every `/monitor` close-evaluation fall through to the static-fallback **warning** at `call_seller.py:366-371`); delete `universe.earnings_exclusion_days` |
| `config/settings.yaml` | **Untouched.** |
| `tests/test_fc075_phase2.py` (new) + touched suites | See Test requirements |

## Behavior contract

**Inputs:** `STRATEGY_CONFIG=config/covered_call.yaml`; equity positions manually established in `PA37XLNWDLB3`; Cloud Scheduler POSTs to `/scan`, `/run`, `/monitor` (Phase 3).

**Covered-call service behavior:**
- `/scan`: put leg no-ops with one info event; call leg scans **every** equity holding except `excluded_symbols`, applies (in order) exclusion → 100-share lot floor → FC-013 earnings gate → `avg_entry_price` floor via `CostBasisResolver` (cross-check against the `covered_call` dataset only; unavailable ⇒ floor kept) → chain criteria incl. new OI/spread thresholds → stores call-only, strategy-tagged blob. Every terminated symbol has a decision row; exposure alert fires when `long_market_value > equity × threshold`.
- `/run`: no `WheelEngine` construction; non-call opportunities refused loudly (expected count 0); ranking/selection/execution identical to the wheel's call pool — multi-contract sizing from uncommitted shares (partial coverage of a lot is correct), execute-time floor gate in `CallSeller`, journal row (post-FC-067) labeled `call`/`sell_call`.
- `/monitor`: existing early-close path as-is (`strict_option_type` routing is already canonical); DTE-band profit targets from the profile.
- `/roll`: disabled no-op.
- Edge cases: empty account → clean no-op scan (0 rows, 0 opportunities, no errors); `<100`-share holding → `not_eligible/insufficient_shares` row; all shares committed to existing short calls → ranking drop (`insufficient_available_shares`); positions fetch outage → `PositionsUnavailable` fail-closed (nothing sold); divergence/missing basis → symbol blocked, event logged; earnings unanswerable → symbol blocked (fail closed); same underlying held in both accounts → no cross-account data flow in either direction.

**What must NOT change for the wheel (the neutrality contract):** with `settings.yaml` (no new keys), every gate added here is provably inert — `strategy_id == 'wheel'` short-circuits the put gate, the `/run` filter, and the reconcile gate; `excluded_symbols=[]`, `min_open_interest=None`, `max_spread_pct=None`, `sizing_basis='buying_power'` disable the rest; `bigquery_dataset` defaults to `options_wheel` so both BQ reads are byte-identical. Blob layout, `client_order_id` scheme, decision-record vocabulary consumed by existing rows, and all wheel datasets are untouched.

## Test requirements

New file `tests/test_fc075_phase2.py` (mirroring the Phase 1 convention) plus targeted additions to existing suites. Each test names the regression it catches:

1. **Put-leg gate:** covered-call config → `scan_for_put_opportunities` returns `[]`, emits the skip event, and never touches `market_data` (assert `filter_suitable_stocks` not called — catches the `stock_symbols` KeyError→`scan_failure` noise regression). Wheel config → existing behavior (existing suite).
2. **`/run` call-only filter:** covered-call service + blob containing a put opportunity → put dropped with `non_call_opportunity_refused`, `put_seller.execute_put_sale` never called; wheel service + same blob → put flows to ranking (catches over-gating the wheel).
3. **Reconcile gate:** covered-call `/run` never constructs `WheelEngine` (patch + assert); wheel `/run` still calls `reconcile_positions` (catches both directions).
4. **`excluded_symbols`:** excluded holding → `not_eligible/excluded_by_config` decision row, zero chain fetches for it; non-excluded holdings unaffected. Also: exclusion beats the lot-size check (a 50-share excluded symbol reports `excluded`, not `insufficient_shares`).
5. **Chain criteria:** with `min_open_interest`/`max_spread_pct` set, fixture strikes below/above thresholds are rejected with the new counters visible in `last_call_rejection_stats` and in the symbol's `reason_counts`; with the keys absent, a fixture chain produces the **identical** accept/reject set as before the change (the wheel-neutrality proof at the filter level; also catches the `rejection_stats` KeyError if a counter is uninitialized).
6. **BQ read threading:** covered-call config → both chokepoints query the `covered_call` dataset (assert on the SQL / constructor arg); wheel config → `options_wheel`; query failure → `cross_check unavailable` + floor kept and `uncovered_days=None` (posture pins — catches the cross-account divergence block described in Design decision 4; a regression here silently blocks covered-call symbols with wheel history).
7. **Journal labeling (post-FC-067):** fake journal through `execute_batch` on a scanner-shaped call opportunity → row has `option_type='call'`, `strategy='sell_call'` (catches FC-067 regressing or Phase 2 landing without it).
8. **Exposure alert:** `sizing_basis: equity` + `long_market_value > equity` → event emitted, scan proceeds (alert-only); `<=` → no event; wheel config → account never fetched for the check.
9. **Config surface:** `covered_call.yaml` loads and validates post-edit (earnings lookahead rule included); new properties' defaults on the wheel profile; validation rejects `max_spread_pct > 1`, non-list `excluded_symbols`, unknown `sizing_basis`.
10. **OCC grep gate** (parent plan Verification item, still absent from `tests/test_lint_gates.py` — verified): add the repo-wide test failing on `'P' in`/`'C' in` over option-symbol variables outside `src/utils/option_symbols.py`, with a scoped allowlist for the known legacy sites (`wheel_engine.py:304/:306/:408`, `tools/testing/regression_monitor.py:~628`) that FC-079 will drain. Catches the seventh family member before it ships in this or any future PR.
11. **Wheel regression:** full existing suite green (1262+ tests at last count).

## Dependencies & ordering

| Dependency | Status | Ordering decision |
|---|---|---|
| **FC-067** (journal defaults) | Parallel PR in flight | **Merge before Phase 2.** Phase 2's test 7 asserts its outcome; the engine itself adds no labeling code. If FC-067 slips, Phase 2 may still merge with test 7 marked xfail-pending-FC-067 **only if** the Seam-4 deploy gate below is respected (no covered-call service runs, so no rows are written either way) — but the default is FC-067 first. |
| **Seam 4** (BQ write threading + `strategy_id` column) | Deferred to Phase 3 (parent plan) | **Phase 2 merges before Seam 4; the covered-call service must not be DEPLOYED until Seam 4 lands.** Stronger than the parent plan's "must not run `/ingest-*` or analytics jobs": verified in code, the core loop itself writes the wheel's dataset today — `/scan` and `/run` write `decision_events` via the `get_analytics_writer()` singleton (`decision_record.py:738-742`; `analytics_writer.py` default `dataset_id="options_wheel"`), all endpoints write `executions`/`errors` rows, and `execute_batch` writes `trades` via `TradeJournal()` (default dataset, `trade_journal.py:68`). A running covered-call service therefore pollutes wheel analytics on its **first scan**, not its first ingest. Merging Phase 2 is safe at any time (code is inert until a service runs the profile); deployment is the gated act. Sequence: **FC-067 → Phase 2 → Seam 4 → Phase 3 deploy.** |
| FC-013 machinery + `FINNHUB_API_KEY` | Live on wheel | Profile enables the gate; the key binding is a Phase 3 deploy item (fail-closed if missing: no calls written, `call_scan_skipped_earnings_unknown` events). |

## Accepted risks (confirmed, not fixed here)

- **FC-061 — share ledger blind to open unfilled short-call orders.** Confirmed still true (`_available_shares` counts filled positions only, `execution_engine.py:145-183`; `AlpacaClient.get_positions` still strips `qty_available`). Accepted per FC-061: Alpaca's share-lock rejects the second order at placement (live-verified 2026-07-30); failure mode is a wasted slot + noise, not a naked call. Watch broker share-lock rejects in the first weeks (parent plan's monitoring list).
- **FC-054 / FC-079 — OCC-substring sites on the reconcile path.** Confirmed live at `wheel_engine.py:304-307` and `:406-413`, **but the covered-call path no longer traverses them** after Design decision 6's gate — safely avoided. Their fix stays FC-079's own PR. The scanner site the parent plan listed (`options_scanner.py:611`) no longer exists (fixed by FC-069 item 12).
- **FC-053 — `/monitor` silently skips unparseable/adjusted contracts.** Carries into the covered-call service unchanged; FC-053 owns the alert fix.
- **FC-072 — call-side limit pricing** (flat `mid × 0.95`, `call_seller.py:159`, no spread-aware improvement like the put side). Inherited execution-quality gap, not a gate.

## Open questions

- **OQ-1 (non-blocking, operator):** initial tunable values — `call_delta_range [0.15,0.25]`, `call_target_dte 7`, `min_call_premium 0.30`, `min_open_interest 500`, `max_spread_pct 0.10`, and the copied `dte_bands` — all operator-adjustable in `covered_call.yaml` before first sale (parent plan OQ-2 carried forward). No code impact.
- **OQ-2 (non-blocking):** should `long_exposure > equity` ever escalate from alert to blocking new call writes? Shipped as alert-only per the operator's floor-only-gating precedent; revisit with live data.
- **OQ-3 (non-blocking):** scheduler cadence for the covered-call service — parent plan OQ-3, default mirror-the-wheel; a Phase 3 decision.
- No blocking open questions. An unflagged ambiguity found during the build is a plan defect — surface it, don't improvise.

## Rollout (paper account; shadow → first trade)

1. FC-067 merges (parallel PR). Phase 2 PR: two adversarial reviews (fresh Fable contexts, different personas; one with live-data access) → disposition → confirmation pass if fixes land in code → merge. Wheel service redeploys with these changes; verify wheel behavior unchanged in production logs (no new event types on the wheel profile, scan/run cadence and blob shape identical).
2. Seam 4 PR (own plan/design), then Phase 3 provisioning (bucket exists per Phase 1 config; create `covered_call` dataset + IAM; deploy `covered-call-engine` with `STRATEGY_CONFIG`, CC secrets, `FINNHUB_API_KEY`; do **not** schedule `/roll`; never `--set-env-vars`).
3. Shadow week, empty account: scheduler on for `/scan`/`/run`/`/monitor`; expect clean no-ops — zero opportunities, zero decision rows, zero `scan_failure`/`non_call_opportunity_refused` events, interlock `ok` on `/health`. Run the parent plan's interlock drill and cross-contamination probe in this window.
4. Operator buys the first lot via the Alpaca UI (≥100 shares of something they will let go). Next scan should produce decision rows and, if the chain qualifies, the first covered-call opportunity; first `/run` writes the first call. Verify: journal row labeled `call`/`sell_call` in the `covered_call` dataset, floor honored (`strike >= avg_entry_price`), share ledger sized correctly, wheel account untouched.
5. Rollback at any point: delete the service + scheduler jobs — zero wheel impact (own account/bucket/dataset). The Phase 2 code changes are wheel-inert by the neutrality contract and revert cleanly by git.

## Corrections to the parent plan discovered by this pass

Recorded so the parent plan's staleness doesn't mislead a future reader; update `fc-075.md` only via its normal bookkeeping:

- FC-054 site list: `wheel_engine.py` refs are now `:304-307`/`:406-413` (post-FC-069 shrink); `options_scanner.py:611` is fixed, not live.
- Seam 4 gate wording: `/scan`//`/run`//`/monitor` are themselves analytics writers (decision events, executions, errors, trades) — the deploy gate, not merely an ingest-job gate.
- The repo-wide OCC grep gate promised in the parent plan's Verification does not exist yet; it ships here (test requirement 10).
- The cost-basis cross-check is not merely wheel-scoped telemetry: unthreaded, it is a covered-call trading blocker for shared underlyings (Design decision 4).

---

## Review disposition (2026-08-21) — authoritative amendments

Two adversarial plan reviews (Fable, fresh contexts: senior options trader; production reliability/data engineer). **Both REQUEST_CHANGES; both affirmed the architecture** ("sound and unusually well-verified"; a hand-walk of all three handlers found no fifth wheel-specific branch). Every finding is a plan-text fix — none reopen the design. This section is authoritative where it amends anything above; the builder follows it.

### HIGH-1 — DD-5 exposure alert is unbuildable as written and would brick the CC scan (both reviewers)
`AlpacaClient.get_account()` (`src/api/alpaca_client.py:224-236`) returns no `long_market_value`; built literally, `float(None)` throws inside `trigger_scan`'s try → every CC scan aborts as `scan_failure`. Amendments:
- **Add `long_market_value` to `get_account()`** — `float(getattr(account, 'long_market_value', 0.0) or 0.0)`. Additive, wheel-neutral. **Add `src/api/alpaca_client.py` to the touch table.**
- **Exception-isolate the alert:** wrap the `get_account()` fetch + comparison in its own try/except (mirror the analytics-write isolation at `cloud_run_server.py:385-396`); on any failure log one event and **let the scan proceed** — the alert must never block a scan.
- **Test 8 gains a case:** `get_account` raises → scan completes and stores the blob, no alert; also assert the wheel profile never fetches the account for this check.

### HIGH-2 — the Seam-4 "deploy gate" is a doc promise, not a control; enforce it in code (reliability reviewer)
`/scan` and `/run` write `decision_events` (via the `get_analytics_writer()` singleton, default `options_wheel`) and `trades` (via `TradeJournal()`, default `options_wheel`) **today**. `main.py --command scan` with `STRATEGY_CONFIG=config/covered_call.yaml` and ambient GCP creds therefore writes covered-call rows into the **wheel's** dataset with no deploy and nothing refusing it. "Don't deploy until Seam 4" is exactly the FC-031-class promise-not-a-control this project has been burned by. Amendment — **Design decision 7 (new): pre-Seam-4 write interlock.**
- Add a shared guard `_writes_are_isolated(config)` → `True` only when `config.strategy_id == 'wheel'` **or** the BQ writers honor `config.bigquery_dataset` (the flag Seam 4 flips). Until Seam 4, a non-wheel profile returns `False`.
- On `False`, the write-producing paths **fail closed**: `/scan`, `/run`, `/monitor` (and the `main.py` scan/run CLI entry, so the decorator-bypass path is covered too) refuse with one `write_isolation_unavailable` error event and do no work. This makes "the CC service cannot contaminate the wheel dataset before Seam 4" a **code property**, not a sequencing note.
- **Seam 4 removes this guard** as its final step (once writers are dataset-correct). Record that hand-off explicitly in the Seam 4 plan.
- Tests: non-wheel profile pre-Seam-4 → `/scan` and CLI scan refuse + emit the event, write nothing; wheel profile → unaffected.
- Consequence for the rollout: the CC shadow week necessarily follows Seam 4 (the service is inert before it) — which already matches the FC-067 → Phase 2 → Seam 4 → Phase 3 ordering. Update the rollout to state the shadow week is post-Seam-4.

### MEDIUM — OCC grep-gate allowlist is incomplete + comment handling unspecified (both; independently re-verified)
Live `'P' in`/`'C' in` code sites over option symbols are **four** files, not two: `wheel_engine.py:304/:306/:408`, `regression_monitor.py:628`, **and** `tools/testing/debug_expired_put_analysis.py:23/:26` and `tools/testing/detailed_expired_options_analysis.py:183/:186`. Amendments: allowlist all four; the gate matches **code only** (strip comments/docstrings — there are 7 comment mentions in `option_symbols.py`, `execution_engine.py`, `cloud_run_server.py`, `regression_monitor.py` that must not trip it); gate scope stays `src/ tools/ deploy/`.

### MEDIUM — `/run` call-only filter corrupts decision-row attribution (reliability reviewer)
The filter drops non-calls after `blob_opportunities[:] = list(opportunities)` (`:541`), but `_underlyings_removed(blob_opportunities, opportunities)` (`:571`) then diffs against the blob → an underlying whose only opp was a refused put is mislabeled `previously_failed`. Amendment: drop refused opportunities from **both** lists (or exclude filter-dropped underlyings from the diff) so the refusal telemetry isn't corrupted; **extend test 2** to assert no `previously_failed` mislabel on the refused put.

### MEDIUM — DD-4 post-provisioning mechanism mis-stated; Phase 3 must provision the read tables (reliability reviewer)
The "cross-check → `no_assignment_history`" story assumes `covered_call.trades_from_activities` **exists**; nothing in Phase 2/3 creates it (it's ingestor-written, Seam-4-gated). Actual pre-provision behavior: BQ NotFound → `cost_basis_cross_check_lookup_failed` **WARNING per held symbol per scan** + `uncovered_days_lookup_failed` per scan (whose SQL also needs `stock_history_from_alpaca`). Safe (floor kept) but permanent per-scan warning noise on a service whose alerting reads Cloud Logging. Amendments: correct DD-4's post-provisioning paragraph to the NotFound-degradation mechanism; **Phase 3 checklist**: create + ingest `trades_from_activities` and `stock_history_from_alpaca` in the `covered_call` dataset, or the plan explicitly accepts the per-scan warnings as steady state.

### MEDIUM — ex-dividend early assignment unaddressed (options-trader reviewer)
The classic covered-call event, likelier here (manual entry into a 4× margin account invites dividend payers; `/monitor` only closes at profit so an ITM call rides; no roller). The hard floor bounds it — assignment realizes `strike ≥ avg_entry_price` + premium, so it's forfeited upside/dividend and early cycle-end, not a loss — but the plan must own it. Amendments: add an **accepted-risk** entry with the floor-bounds-it reasoning; add an early-assignment **monitoring line** to the shadow-week/first-trades list; operator note that high-yield tickers belong in `excluded_symbols` or are accepted-forfeit.

### MEDIUM — reframe the permanently-dead cross-check as an accepted risk (options-trader reviewer)
On the CC book the FC-065 divergence check is structurally dead forever (`covered_call.trades_from_activities` never holds put assignments), so a broker mis-adjustment (split/corporate action) on a manually-entered lot sets a wrong floor with nothing watching. Amendment: move the "precisely the FC-065 design" framing in DD-4 to an **accepted risk** with named mitigations (fail-closed on zero basis; strict OCC parse refuses adjusted `1AAPL` roots; broker `avg_entry_price` is genuinely authoritative for plain manual purchases).

### LOW (batch)
- **FC-067 section + dependency row are stale** — #88 (`b02f48d`) is merged **beneath this plan commit**; `record_trade` already derives the leg from the OCC symbol. Rewrite "Journal labeling" to past tense (FC-067 merged; Phase 2 only pins the outcome with test 7, unconditionally buildable) and set the Dependencies row to "merged (#88)". Remove the xfail contingency.
- **New chain-criteria missing-field semantics:** OI/`bid`/`ask` absent or `None` with a threshold set → **fail closed** (reject the strike), never `KeyError` out of `_check_call_criteria_detailed`; note the spread check's fail-open-on-`mid<=0` is shielded only by `min_call_premium` running earlier — state that coupling. Add a fixture case.
- **Dead-knob consistency:** apply the same "no-consumer knob is a defect" rule used to delete `earnings_exclusion_days` to `floor_mode`, `max_position_size`, `use_put_stop_loss` in `covered_call.yaml` — either validate `floor_mode == 'avg_entry_price'` (give it a consumer) or delete/re-comment the three; fix the false "Consumed in Phase 2" comments.
- **`excluded_symbols` normalization:** compare `symbol.upper().strip()` against a normalized set; add a test (`aapl` excludes AAPL).
- **`stock_symbols` stragglers:** `/config` (`cloud_run_server.py:1290`), `/backtest/screen` (`:1411`), `main.py:112 get_market_overview` use `getattr(config, 'stock_symbols', [])`, which does **not** swallow the property's `KeyError` → 500 on the CC profile. Pre-existing, but Phase 2 claims to cover every `stock_symbols` caller — fix all three (guard the profile) or explicitly defer with a one-line note.

### Critique carried forward (no code, record in the plan)
- **Reverse-neutrality burden:** after this merges, every future *wheel* PR must also prove it didn't change *covered-call* behavior; only tests 5/11 partially encode that. Add a one-line note to the neutrality contract making the two-way burden explicit.

### Net verdict
Both reviews approve-with-changes; all amendments above are plan-text/spec fixes plus one small new code requirement (Design decision 7, the write interlock). No architecture change. With these folded in, the plan is build-ready — proceed to the Opus build, then two Fable code reviews + confirmation.


## Execution (as-merged 2026-08-21)

- **PR:** #89 (squash `2e08d0a`). Depends on FC-067 (#88, `b02f48d`). Blocks Seam 4.
- **Process:** design pass (Fable) → 2 adversarial plan reviews → Opus build (4 commits) → 2 adversarial code reviews (both REQUEST_CHANGES, both affirmed the code correct + wheel-neutral; findings were an under-delivered test contract + an overclaiming docstring) → dispositioned (11 added tests, 2 helpers extracted for testability) → scoped confirmation pass CONFIRMED-CLEAN. Suite 1269 → 1301.
- **As-built:** no new engine — `strategy_id`-keyed gating of the existing pipeline exactly as the design pass predicted. DD-4 fixed a live cross-account cost-basis blocker. DD-7 (`config.writes_isolated`) fails a non-wheel profile closed on every write path + the CLI until Seam 4.
- **Next:** Seam 4 (threads `config.bigquery_dataset` through the 5 writers + `strategy_id` column + live-table ALTER; **removes the DD-7 guard** as its final step) → Phase 3 provisioning + deploy → shadow week → first paper trade.
