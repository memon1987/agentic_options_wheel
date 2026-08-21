# Plan: FC-075 Seam 4 — BigQuery write-side dataset threading (+ `strategy_id` column, DD-7 removal)

**FC entry:** `docs/FUTURE_CONSIDERATIONS.md` FC-075 (also closes out FC-067's corrective-UPDATE rider)
**Parent plan:** `docs/plans/fc-075.md` §Seam 4 (deferral rationale); `docs/plans/fc-075-phase-2.md` DD-7 + HIGH-2 (the interlock this seam removes)
**Plan file:** `docs/plans/fc-075-seam-4.md`
**Status:** Executing — PR [#90](https://github.com/memon1987/agentic_options_wheel/pull/90) open; **R1 (the 7 live-table ALTERs) is PENDING and gates merge** — see §Execution
**Size:** S–M (~150 net production lines across 9 files + tests; plus 7 idempotent DDLs and one corrective DML run outside the PR)
**Author:** Claude (Fable), 2026-08-21, against `main` @ `b034b93`
**Builder:** Opus. **Reviews:** two adversarial (Fable, fresh contexts, different personas). Stakes calibration: this PR touches every canonical BigQuery table the dashboard and analytics read — at least one reviewer gets live BQ access and must verify schema/row claims against the real dataset, not the diff.

---

## Context

FC-075 Phase 2 (PR #89) left the covered-call service code-complete but deliberately inert: DD-7 (`config.writes_isolated` → `require_write_isolation` on 7 routes + a `main.py` CLI guard) fails every non-wheel profile closed on all write-producing paths, because the five BigQuery writers hardcode `dataset_id="options_wheel"`. Seam 4 makes the writers dataset-correct and strategy-stamped, then removes the guard as its final step. It is the unlock for Phase 3 (deploy) → shadow week → first paper trade.

**Live facts verified 2026-08-21 (claude-operator SA, read-only):**

- All 7 wheel tables lack `strategy_id`: `trades`, `errors`, `executions`, `decision_events`, `trades_from_activities`, `equity_history_from_alpaca`, `stock_history_from_alpaca`.
- FC-067's mislabeled call rows in `options_wheel.trades` now number **62, not the 29 in the FC entry** — all side=`sell`, all labeled `put`/`sell_put` on a C-contract OCC symbol. The count grew because **FC-067's fix is merged but not deployed** (next bullet); calls kept journaling through the poisoned defaults after the 07-31 investigation count.
- **The wheel service is running `f84b50a` (built 2026-08-05).** The Cloud Build trigger (`deploy-options-wheel-strategy`, `^main$`, enabled, no path filters) fired for **none** of today's four merges to main — #88 (FC-067), #89 (Phase 2), and two bookkeeping commits produced no build at all. FC-031 class, worse: not a red build nobody saw, but no build. Filed as **FC-081**; it is a **hard external gate** on this plan's rollout (steps R3–R5 need a working deploy path), not on authoring/reviewing/building it.

**What Seam 4 is, in one sentence:** five constructor-level threading changes + one singleton hand-off + one NULLABLE column stamped at five write chokepoints + seven pre-merge ALTERs + one post-deploy corrective UPDATE + the deletion of DD-7.

## Design decision 1 — writers take `dataset_id` and `strategy_id` as **required** keyword args

All five writers currently default `dataset_id="options_wheel"` (`src/data/trade_journal.py:70`, `src/data/analytics_writer.py:174`, `src/data/activities_ingestor.py:92`, `src/data/portfolio_history_ingestor.py:66`, `src/data/stock_history_ingestor.py:75`). The default is the defect: it is precisely how a second profile silently writes the wheel's dataset, and DD-7 exists only to compensate for it.

**Change:** each writer's `__init__` takes `dataset_id: str` and `strategy_id: str` as **required keyword-only** arguments — no defaults. Every construction site states its dataset from config:

| Construction site | Change |
|---|---|
| `src/strategy/execution_engine.py:96` — `self.trade_journal = trade_journal or TradeJournal()` | `TradeJournal(dataset_id=config.bigquery_dataset, strategy_id=config.strategy_id)` (`config` is the `__init__` param; assign after `self.config`) |
| `deploy/cloud_run_server.py:1659` — `ActivitiesIngestor(alpaca_client)` | add `dataset_id=config.bigquery_dataset, strategy_id=config.strategy_id` (`config = strategy_config()` already in scope in the handler; add the local if absent) |
| `deploy/cloud_run_server.py:1717` — `PortfolioHistoryIngestor(alpaca_client)` | same |
| `deploy/cloud_run_server.py:1772` — `StockHistoryIngestor(alpaca_client)` | same |
| `src/data/analytics_writer.py:395` — bare `AnalyticsWriter()` inside the singleton factory | replaced by Design decision 2 |
| `src/backtesting/engine/simulator.py:405` + `src/backtesting/engine/no_op_analytics.py` — `NoOpTradeJournal()` / `NoOpAnalyticsWriter()` | verify their `__init__`s don't call `super().__init__()` without the new args; adjust to pass inert values (`dataset_id="noop", strategy_id="backtest"`) if they do |

*Rejected alternative — keep wheel defaults for "compatibility":* a silent default is the original sin (see also `docs/CLAUDE.md` §Config discipline: a key nothing consumes is a defect; a default nothing questions is the same defect one layer down). Required args flush any hidden construction site at build time instead of at contamination time. The wheel's behavior is preserved by its config (`bigquery_dataset` defaults to `options_wheel`, `strategy_id` to `wheel` — `src/utils/config.py:408-417, :383`), not by writer defaults.

## Design decision 2 — the `AnalyticsWriter` singleton gets an explicit configure hand-off, fail-closed when unconfigured

`get_analytics_writer()` (`analytics_writer.py:388-396`) lazily constructs the process singleton with no config in scope; its deepest caller is the decision-record flusher (`src/data/decision_record.py:741-742`). The parent plan deferred Seam 4 partly for exactly this: the singleton needs a per-process dataset source.

**Change (in `src/data/analytics_writer.py`):**

- New module function `configure_analytics_writer(*, dataset_id: str, strategy_id: str)` storing module-level defaults for singleton construction. Calling it after the singleton exists with **different** values raises (`RuntimeError`) — one process, one profile, no silent re-pointing.
- `get_analytics_writer()`: thread-local override (backtests, unchanged) → else if configured, construct/return the singleton with the stored values → else return a **disabled** writer (`_enabled = False`, the class's existing no-op posture), emit one `analytics_writer_unconfigured` warning, and do **not** cache it — a later call after configuration gets the real writer.
- Two configure call sites, at the two process entry chokepoints:
  - `deploy/cloud_run_server.py` `strategy_config()` (:218-227) — immediately after `_CONFIG_CACHE = Config(...)`. Every route resolves config via decorators (`require_account_match`) before any handler writes, so the singleton is configured before first use on every request path.
  - `main.py` — immediately after `config = Config(args.config)` (:48). Note the CLI selects its profile via `--config`, **not** `STRATEGY_CONFIG` — this is why the singleton cannot self-resolve from the env var (rejected below).

*Rejected — self-resolve from `STRATEGY_CONFIG` inside `get_analytics_writer()`:* diverges from the CLI's `--config` source of truth, and puts a global-config load inside a data-layer module. *Rejected — keep constructing with a default dataset when unconfigured:* that is the fail-open DD-7 exists to prevent; unconfigured must mean no-op, never `options_wheel`.

## Design decision 3 — `strategy_id` column, stamped at the five write chokepoints

**Schema:** add `bigquery.SchemaField("strategy_id", "STRING", description="Strategy profile that wrote this row; NULL = wheel (pre-Seam-4 rows)")` to all seven code-defined schemas: `_SCHEMAS["errors"|"executions"|"decision_events"]` (`analytics_writer.py:68+`), `_TABLE_SCHEMA` (`trade_journal.py:30-57`), and the three ingestor schemas (`activities_ingestor.py:49+`, `portfolio_history_ingestor.py:44+`, `stock_history_ingestor.py:51+`). NULLABLE — additive-only, the writers' own stated schema-change rule (`analytics_writer.py` module docstring).

**Stamping — at the insert chokepoint, not at each producer:**

| Writer | Stamp site |
|---|---|
| `AnalyticsWriter` | `_write` (:234) and `_write_batch` (:249) — `row["strategy_id"] = self._strategy_id` — covers `write_error`, `write_execution`, `write_decision_events` and every current/future producer at two lines |
| `TradeJournal` | the `row` dict in `record_trade` (built :175-203) |
| `ActivitiesIngestor` | row construction feeding `insert_rows_json` (:429) |
| `PortfolioHistoryIngestor` | row construction feeding :275 |
| `StockHistoryIngestor` | row construction feeding :311 (`row_ids`/insertIds unchanged — dedup keys must not change meaning) |

**Interpretation rule (document in `docs/CLAUDE.md` Data Analysis section as part of this PR):** historical rows have `strategy_id IS NULL`; queries segmenting by strategy use `IFNULL(strategy_id, 'wheel')`. **No backfill** of historical wheel rows — parent plan OQ-5, decided. Fresh `covered_call` tables (Phase 3) are born with the column via the writers' existing ensure-table logic, which creates tables from these code-defined schemas.

## Design decision 4 — the live wheel-table ALTER runs **before** merge, as a standalone verified step

BigQuery streaming inserts **fail on unknown fields** but tolerate missing NULLABLE fields. Therefore ALTER-first is strictly safe (old code + new column ⇒ NULLs), and deploy-first is broken (new code + old schema ⇒ every insert on all 7 tables errors). The ordering is the heart of this plan.

**DDL (idempotent, run once per table by claude-operator; if `bigquery.tables.update` is denied — untested for this SA — the operator runs them; either way paste the `bq show` verification output into §Execution):**

```sql
ALTER TABLE `gen-lang-client-0607444019.options_wheel.trades`                    ADD COLUMN IF NOT EXISTS strategy_id STRING;
ALTER TABLE `gen-lang-client-0607444019.options_wheel.errors`                    ADD COLUMN IF NOT EXISTS strategy_id STRING;
ALTER TABLE `gen-lang-client-0607444019.options_wheel.executions`                ADD COLUMN IF NOT EXISTS strategy_id STRING;
ALTER TABLE `gen-lang-client-0607444019.options_wheel.decision_events`           ADD COLUMN IF NOT EXISTS strategy_id STRING;
ALTER TABLE `gen-lang-client-0607444019.options_wheel.trades_from_activities`    ADD COLUMN IF NOT EXISTS strategy_id STRING;
ALTER TABLE `gen-lang-client-0607444019.options_wheel.equity_history_from_alpaca` ADD COLUMN IF NOT EXISTS strategy_id STRING;
ALTER TABLE `gen-lang-client-0607444019.options_wheel.stock_history_from_alpaca` ADD COLUMN IF NOT EXISTS strategy_id STRING;
```

`ADD COLUMN` is legal on tables with an active streaming buffer; no quiesce needed. Downstream readers are safe by construction: BQ logical views re-resolve at query time and a NULLABLE column is additive; the dashboard backend selects named fields. (Verification item V6 confirms rather than assumes.)

## Design decision 5 — FC-067's corrective UPDATE rides this plan, and runs **after** the fixed writer is verified live

House rules (`~/CLAUDE.md` §Synthetic / corrective data writes) require the plan to carry rationale, exact rows, audit query, and rollback. This is a **label correction on real rows**, not synthetic row insertion, so the marking discipline is the deterministic predicate + a pre-UPDATE snapshot, not a key prefix.

- **Rationale:** `TradeJournal.record_trade` defaulted `option_type`/`strategy` to `put`/`sell_put` for every scanner-produced row (FC-067, fixed in #88 `b02f48d`). The upstream producer is our own bug, now fixed forward-only; the historical rows misstate which leg was traded and derailed two investigations (FC-067 entry).
- **Ordering:** run only after the Seam 4 deploy is verified (R4) — the deploy carries #88's fix (currently **merged-but-undeployed**, which is why the row count is still growing). Running earlier means recounting later.
- **Audit (before):**
  ```sql
  SELECT COUNT(*) FROM `gen-lang-client-0607444019.options_wheel.trades`
  WHERE REGEXP_CONTAINS(symbol, r'^[A-Z]+[0-9]{6}C[0-9]{8}$')
    AND (option_type != 'call' OR strategy != 'sell_call');
  -- 62 as of 2026-08-21; re-count at execution time
  ```
- **Snapshot (rollback bound):** save `SELECT order_id, option_type, strategy FROM ... WHERE <same predicate>` output into `docs/investigations/fc-067-label-correction-<date>.md` before updating. Rollback = restore those columns for exactly those `order_id`s — the predicate alone must NOT be the rollback key, or it would clobber future correctly-labeled call rows.
- **UPDATE:**
  ```sql
  UPDATE `gen-lang-client-0607444019.options_wheel.trades`
  SET option_type = 'call', strategy = 'sell_call'
  WHERE REGEXP_CONTAINS(symbol, r'^[A-Z]+[0-9]{6}C[0-9]{8}$')
    AND (option_type != 'call' OR strategy != 'sell_call');
  ```
  All 62 current rows are side=`sell` (verified), so `sell_call` reproduces exactly what the fixed writer (`trade_journal.py:155-171`) would have written. DML fails on rows in the streaming buffer — run outside market hours or retry after the buffer drains (~90 min).
- **Audit (after):** the before-query returns 0; row count of the table unchanged.
- **Scope:** labels only. `expiration`/`dte` NULL-normalization on old rows (FC-067's open question) is **out of scope** — different derivation, different risk, no current consumer blocked (non-blocking OQ-2 below).
- These rows are wheel rows; per no-backfill (DD-3) the UPDATE does **not** touch `strategy_id`.

## Design decision 6 — DD-7 removal is the final commit, and it deletes rather than disables

Once writers are dataset-correct, `writes_isolated` is a tautology (`True` for every correctly-constructed process) — a control with no failure mode left to control. Per config discipline, delete it:

- `src/utils/config.py:419-430` — delete the `writes_isolated` property; update the `bigquery_dataset` docstring (:407-417) which currently says the write side is "Seam 4, still deferred".
- `deploy/cloud_run_server.py:314-349` — delete `require_write_isolation` and its 7 decorations (`/scan` :394, `/run` :557, `/monitor` :1004, `/roll` :1312, `/ingest-activities` :1641, `/ingest-portfolio-history` :1700, `/ingest-stock-history` :1752). `require_api_key` and `require_account_match` (Seam 2) stay untouched.
- `main.py:83-97` — delete the CLI guard + its comment block.
- `config/covered_call.yaml:29-35` — rewrite the "NOT YET WIRED" comment to state the dataset is live-wired by Seam 4.
- Tests asserting the 503/`write_isolation_unavailable` behavior (in `tests/test_fc075_phase2.py`) are **replaced**, not deleted: each becomes its positive twin — the CC profile's write path proceeds and constructs its writers against `covered_call` (T5 below).

The Phase 2 review (HIGH-2) mandated this hand-off be recorded explicitly: **this plan is that record.** DD-7 was introduced 2026-08-21 by PR #89; it is removed here and nowhere else, and nothing else in the interim may weaken it.

## Explicitly NOT in scope

- **`src/backtesting/reporting/bq_writer.py` (`backtest_runs`).** A local measurement tool run under operator credentials writing one shared, deliberately cross-strategy table keyed by `engine_version`; it is not one of the five service writers and no service endpoint reaches it. Phase 4 (covered-call backtest) revisits if needed.
- **Phase 3 provisioning** (create `covered_call` dataset + IAM, service, scheduler, `FINNHUB_API_KEY`, alert policies). Needs operator perms; own checklist in the parent plan. Note the writers' ensure-table logic calls `create_dataset(exists_ok=True)` — on the CC service this works only after the operator creates the dataset and grants dataset-level WRITER, same shape as the wheel's existing grant.
- **Fixing the dead build trigger (FC-081).** External gate; this plan only sequences around it.
- **`strategy_id` backfill of historical wheel rows** (OQ-5, decided: no).
- **FC-046 / decision-events log sink revival, FC-079 OCC sites, FC-076 structural interlock** — unchanged by this work.

## Behavior contract

- **Wheel service (settings.yaml):** every row it writes after deploy carries `strategy_id='wheel'`; dataset, tables, insertIds, row shapes otherwise byte-identical. Pre-existing rows unchanged (`NULL` = wheel). No endpoint behavior changes — the deleted DD-7 guard was inert for the wheel by construction.
- **Covered-call profile (covered_call.yaml):** write paths no longer refuse. Writers construct against `covered_call`. Pre-provisioning (dataset absent), every writer degrades to its existing disabled/no-op posture at init (`create_dataset`/`create_table` raise → caught → `_enabled=False`) — a CLI `--command scan` against the CC profile after this merge is a **safe no-op toward BigQuery** even before Phase 3, with zero possibility of writing `options_wheel`. This property replaces DD-7 and is pinned by test T5b.
- **CLI (`main.py`):** `--command scan` on any profile configures the singleton from `--config`'s Config and proceeds; no more exit-code-2 refusal.
- **Unconfigured singleton:** no-op + one warning event, never a default dataset (fail-closed; T2).
- **Backtests:** thread-local `set_analytics_writer` override and NoOp classes behave exactly as today.

## Exact files & functions to touch

All refs verified against `b034b93`:

| File | Change |
|---|---|
| `src/data/analytics_writer.py` | Required kwargs (:174); `configure_analytics_writer` + fail-closed `get_analytics_writer` (:388-396); `strategy_id` in `_SCHEMAS` ×3; stamp in `_write` (:234) + `_write_batch` (:249); module docstring table list note |
| `src/data/trade_journal.py` | Required kwargs (:70); `strategy_id` in `_TABLE_SCHEMA` (:30-57); stamp in `record_trade` row (:175-203) |
| `src/data/activities_ingestor.py` | Required kwargs (:92); schema field; stamp rows (:429) |
| `src/data/portfolio_history_ingestor.py` | Required kwargs (:66); schema field; stamp rows (:275) |
| `src/data/stock_history_ingestor.py` | Required kwargs (:75); schema field; stamp rows (:311); `row_ids` untouched |
| `src/strategy/execution_engine.py` | :96 `TradeJournal(dataset_id=..., strategy_id=...)` from `config` |
| `deploy/cloud_run_server.py` | Ingestor kwargs (:1659, :1717, :1772); `configure_analytics_writer` in `strategy_config()` (:218-227); delete `require_write_isolation` (:314-349) + 7 decorations |
| `main.py` | `configure_analytics_writer` after :48; delete guard (:83-97) |
| `src/utils/config.py` | Delete `writes_isolated` (:419-430); fix `bigquery_dataset` docstring (:407-417) |
| `config/covered_call.yaml` | Comment rewrite (:29-35) |
| `src/backtesting/engine/no_op_analytics.py` / `simulator.py` | Verify/adjust NoOp constructors for required kwargs |
| `docs/CLAUDE.md` | `IFNULL(strategy_id,'wheel')` interpretation rule in Data Analysis section |
| `tests/test_fc075_seam4.py` (new) + `tests/test_fc075_phase2.py` | See Test requirements |

## Test requirements

New `tests/test_fc075_seam4.py` plus edits to `tests/test_fc075_phase2.py`. Each names its regression:

1. **T1 — required args:** constructing any of the five writers without `dataset_id`/`strategy_id` raises `TypeError`; constructing with them stores both (catches a silent-default writer reappearing).
2. **T2 — singleton lifecycle:** unconfigured `get_analytics_writer()` → disabled writer + `analytics_writer_unconfigured` warning, not cached; after `configure_analytics_writer` → real singleton with the configured dataset; reconfigure with different values → raises; thread-local override still wins (catches fail-open default and ordering hazards).
3. **T3 — row stamping:** for each of the five writers, a captured insert row carries `strategy_id` equal to the constructor value; `StockHistoryIngestor` insertIds unchanged (catches a writer whose rows silently lose attribution; catches dedup-key drift).
4. **T4 — schema parity:** all seven code-defined schemas contain `strategy_id` STRING NULLABLE (guards Phase 3's fresh-table creation — a missing field here creates a CC table that rejects its own writer's rows).
5. **T5 — DD-7 flip:** (a) the Phase 2 interlock tests inverted — CC profile `/scan`/`/run`/`/monitor`/`/roll`/`/ingest-*` no longer return `write_isolation_unavailable` (account interlock still enforced — assert 503s from Seam 2 remain when account mismatches); `main.py` scan path proceeds under a CC config (mocked scanner). (b) **fail-closed pre-provisioning pin:** CC-profile `TradeJournal`/`AnalyticsWriter` whose BQ client raises NotFound at init become disabled no-ops — assert no insert is ever attempted against any dataset (this is the property that replaces DD-7; a regression here is the contamination DD-7 existed to stop).
6. **T6 — wheel construction values:** with `settings.yaml`, `ExecutionEngine`'s journal and the server's ingestors/singleton all construct with (`options_wheel`, `wheel`) (catches the threading inverting wheel behavior).
7. **T7 — lint gate:** repo-wide grep test — no `dataset_id: str = "options_wheel"` (or any dataset default) remains in `src/data/`; no `writes_isolated`/`require_write_isolation` references remain outside docs (catches partial DD-7 removal and default re-introduction).
8. **T8 — wheel regression:** full suite green (1301 at last count).

## Execution sequence

Order is load-bearing; R1 before R3 is the only dangerous inversion (see DD-4).

- **R1 (pre-merge, manual):** run the 7 ALTERs; verify with `bq show`; record output in §Execution. Blocked only on `tables.update` permission (fallback: operator).
- **R2:** build PR on a branch (`claude/fc-075-seam-4`): DD-1/2/3 commits first, DD-6 (interlock deletion) as the **final** commit. Two adversarial reviews (fresh Fable contexts, different personas, one with live BQ access) → disposition → confirmation pass if fixes land in code → merge.
- **R3:** deploy. **Gated on FC-081** — as of authoring, pushes to main produce no build. Do not consider Seam 4 landed at merge; FC-031 taught exactly this. Manual fallback: `gcloud builds triggers run deploy-options-wheel-strategy --branch=main` (operator or claude-operator, permission untested).
- **R4 (post-deploy verify):** next wheel scan/run cycle writes rows with `strategy_id='wheel'` in all live-written tables (one query per table); zero streaming-insert errors in service logs; wheel cadence/blob shape unchanged. Also confirms #88/#89 finally live.
- **R5:** FC-067 corrective UPDATE per DD-5 (snapshot → UPDATE → audit 0 → record counts + investigation doc).
- **R6 (bookkeeping):** this plan → Status DONE + §Execution; parent plan §Execution "Remaining" updated (Seam 4 done, Phase 3 next); FC-075 + FC-067 index entries updated; memory update.

## Risks

- **Deploy path broken (FC-081):** merge without deploy leaves DD-7 deleted in code but still enforced in prod (old revision) — safe, since no CC service exists; the risk is *believing* Seam 4 is live when it isn't. R4 is the control.
- **ALTER permission unknown for claude-operator:** first DDL tells; operator fallback named. Zero risk to data (idempotent, additive).
- **UPDATE hits streaming buffer:** DML error, retry off-hours; no partial-write risk (single-statement atomic).
- **Hidden writer construction site missed:** required kwargs make this a `TypeError` at first execution, not silent contamination; T7's grep gate and reviewer sweep back it up.
- **A future profile forgets `configure_analytics_writer`:** fail-closed no-op + warning event (DD-2), never wheel-dataset writes.
- **Reverse neutrality:** Phase 2's two-way burden note applies — this PR must not change CC behavior other than un-gating writes; T5 encodes it.

## Open questions

- **OQ-1 (non-blocking, operator):** after a stable week of `strategy_id='wheel'` rows, backfill historical wheel rows anyway for query simplicity? Default stays no (parent OQ-5); `IFNULL` covers it.
- **OQ-2 (non-blocking):** FC-067's `expiration`/`dte` NULL-normalization on old journal rows — deferred; file under FC-067's entry if ever needed by a real consumer.
- No blocking open questions. An unflagged ambiguity found during the build is a plan defect — surface it, don't improvise.

---

## Review disposition (2026-08-21) — authoritative amendments

Two adversarial plan reviews (Fable, fresh contexts: senior BigQuery/DW DBA with live-data access; production systems engineer hunting fail-open paths). **Both REQUEST_CHANGES; both affirmed the architecture** ("fundamentally sound and unusually well-verified" / "unusually well-grounded" — every live-data claim survived attack: 62 rows all-sell, complete decoration inventory, complete 7-table writer census, no downstream `SELECT *` breakage, deploy gap confirmed). The two HIGHs were found **independently by both reviewers**. Every finding is a plan-text fix; none reopens the design. This section is authoritative where it amends anything above; the builder follows it.

### HIGH-A — the unconfigured "disabled writer" must be constructible with zero BQ side effects (both reviewers, independently)

DD-2 said "return a disabled writer" without naming the mechanism. The naive build — `AnalyticsWriter(dataset_id="unconfigured", ...)` — runs `_ensure_all_tables()`, which under real credentials **creates a junk `unconfigured` dataset + three tables in the prod project and comes up `_enabled=True`**: the exact fail-open DD-2 prohibits, and invisible to tests (conftest's `_no_production_bigquery` guard makes client init fail in-suite, so the wrong build passes T2). Amendments:

- The unconfigured path returns a **module-level `_DisabledAnalyticsWriter` sentinel** — a tiny subclass whose `__init__` sets `_enabled = False`, `_tables = {}` and touches nothing else (no client, no ensure, no env reads). Structurally incapable of the failure, not test-pinned against it.
- **T2 strengthened:** patch `bigquery.Client` and assert it is **never constructed** on the unconfigured path — `_enabled is False` alone is not the assertion.

### HIGH-B — the singleton needs a test-reset seam, and the reconfigure-raises rule must not fight the suite (both reviewers, independently)

`configure_analytics_writer` adds process-global state (configured defaults, warn-once flag) atop the cached `_instance`; nothing in the suite resets any of it, and `tests/test_fc075_phase2.py` flips `STRATEGY_CONFIG` between profiles within one pytest process via `server.reset_strategy_state()` (~26 uses). As drafted, the first test to construct the singleton makes every later profile flip raise `RuntimeError` — T2/T5/T6 cannot coexist. Amendments:

- New `analytics_writer._reset_for_tests()` clearing `_instance`, the configured defaults, and the warn-once flag. **Wire it into `server.reset_strategy_state()`** (which profile-flipping tests already call) and the autouse conftest reset. Underscore-named and documented test-only: it must never become a prod re-pointing seam.
- **Semantics stated:** `configure_analytics_writer` with different values raises only **after** the singleton exists; before first construction it silently re-points (harmless in prod — one env/`--config` per process; noted so the builder doesn't "fix" it).
- **Double-checked lock** on lazy construction (`get_analytics_writer` is unlocked under Flask `threaded=True`; the one-process-one-profile invariant now attaches to `_instance` identity, so make it honest — two lines).

### MEDIUM-A — writers stop creating datasets (adopts the DBA's stricter alternative; closes the pre-provisioning overclaim)

Both reviewers flagged the Behavior contract's "safe no-op toward BigQuery" as overclaimed: every writer calls `create_dataset(exists_ok=True)` before `create_table`, so under dataset-create-capable credentials (operator ADC, claude-operator) a post-merge CC-profile CLI scan would **silently create the `covered_call` dataset itself**, side-stepping Phase 3 provisioning/IAM. Amendment — adopt the structural fix rather than reword the claim:

- **Delete the `create_dataset` call from all five writers' ensure paths; keep `create_table(..., exists_ok=True)`.** "Datasets are provisioned by an operator" becomes a code property. Wheel-neutral: `options_wheel` exists, so its init path never needed the call. Pre-provisioning CC behavior is now NotFound → disabled no-op **under any credentials**, and the Behavior contract's claim becomes true as written.
- **T5b extended:** cover both the exception path and the missing-dataset path; assert no `create_dataset` call exists in any writer (grep-level or mock-level).
- Phase 3 checklist consequence (parent plan): dataset creation is now unconditionally the operator's provisioning step — which it already was on paper (runtime SA lacks `datasets.create`); the code now agrees under every identity.

### MEDIUM-B — completeness fixes to the touch/scope lists

- **`scripts/backfill_analytics.py:29`** constructs `AnalyticsWriter(project_id=...)` — a seventh construction site the plan missed; post-DD-1 it's a `TypeError`. Disposition: **delete the script** (one-shot historical migration, hardcoded window 2025-10-15→2026-04-06, long since run). Added to touch list.
- **`src/data/decision_record.py:498`** — `UncoveredDaysResolver.__init__`'s dormant `dataset_id="options_wheel"` default contradicted T7 as written (its only caller passes `config.bigquery_dataset` explicitly — verified). Disposition: **remove the default** (read-side, one line, caller-verified) rather than carve T7 around it. Added to touch list.
- **T7 rescoped repo-wide** (`src/ deploy/ tools/ scripts/`), allowlisting only `src/backtesting/reporting/bq_writer.py` (deliberately cross-strategy, out of scope) — a future writer added outside `src/data/` must not escape the gate.
- **Four existing test files** build writer fixtures via `object.__new__` + hand-set attributes and will `AttributeError` on the stamp: `tests/test_trade_journal_labeling.py` (:20, :98), `tests/test_activities_ingestor.py` (:255, :394), `tests/test_portfolio_history_ingestor.py` (:26, :70), `tests/test_stock_history_ingestor.py` (:25). Added to touch list — fixtures gain `_strategy_id`, and the builder extends the same pattern, not a patch-around.

### MEDIUM-C — the dangling "V6" citation

DD-4 cited "Verification item V6", a leftover from a cut section; no V-list exists. Disposition: the citation is **withdrawn**; the downstream-reader survey it promised was **executed by both reviewers during this review** — findings: the only `SELECT *` sites are `trades_with_outcomes`'s single-table head, closed CTE chains in `wheel_cycles_from_activities`, a named-projection CTE at `dashboard/backend/services/bigquery.py:63`, and dict-consumed reads in `regression_monitor.py`; the dataset's three `UNION ALL`s use fully named columns. A NULLABLE column breaks none of them. **R4 additionally verifies post-deploy:** dashboard endpoints healthy and `/regression` status unchanged.

### LOW batch (all adopted)

- **UPDATE predicate hardened:** add `AND side = 'sell'` and IFNULL-wrap the label tests — `WHERE REGEXP_CONTAINS(symbol, r'^[A-Z]+[0-9]{6}C[0-9]{8}$') AND side = 'sell' AND (IFNULL(option_type,'') != 'call' OR IFNULL(strategy,'') != 'sell_call')` — audit query identically. R5 pre-flight adds `COUNTIF(side='sell') = COUNT(*)` over the predicate's regex match (62/62 today).
- **Snapshot is a BQ table, not markdown:** `CREATE TABLE options_wheel.fc067_label_snapshot_YYYYMMDD AS SELECT order_id, option_type, strategy FROM trades WHERE <predicate>` — machine-restorable rollback (`UPDATE ... FROM` the snapshot), permanent in-dataset audit artifact, 7-day time travel as backstop only. The investigation doc stays as narrative; the table is the rollback.
- **R4 verification windows made explicit:** `executions`/`errors`/`decision_events`/`trades_from_activities` verify on the next scan/run/ingest cycle; `equity_history_from_alpaca`/`stock_history_from_alpaca` only after the 16:30/17:00 ET daily jobs; `trades` on the **next fill**, whenever that is. R5 runs after the full 7-table verify completes (spans at least one trading day) — do not start it on a partial verify.
- **R1-is-pre-MERGE, second reason recorded:** between merge and deploy (indefinite while FC-081 stands), local wheel-profile CLI runs on main already write stamped rows — harmless only because R1 ran first. A future re-sequencer must not "optimize" R1 to pre-deploy.
- **Warn-once flag** for `analytics_writer_unconfigured` (uncached path must not re-warn per call); flag covered by the HIGH-B reset seam.

### Critique carried forward (recorded, no code)

- **DD-7's replacement is an emergent property, not a single control.** Post-deletion, "CC cannot write the wheel dataset" = required-kwargs + no-defaults (T1/T7) + the configure hand-off — **T1 and T7 are the only standing guards**; weakening either re-opens the hole. A mismatched pair (`dataset_id="options_wheel", strategy_id="covered_call"`) remains representable; a frozen identity object passed as one value would make it unrepresentable but is heavier than this seam warrants. Accepted; FC-076 (structural interlock) remains the eventual home for a stronger invariant.
- **The `strategy_id` column buys nothing today** — separate datasets already attribute rows, and NULL-means-wheel taxes every future query with `IFNULL`. It earns its keep as cheap insurance against exactly the cross-dataset contamination class this project keeps hitting (six OCC-substring instances, DD-4 of Phase 2, FC-067 itself), and fresh CC tables get it free. The tension is real and the trade was made knowingly.
- **Pre-existing defect found during review (not this plan's):** `tools/testing/regression_monitor.py:265-268` filters `trades` on `timestamp_iso`, a column that does not exist — the trade-execution check group has been warn-degrading on every hourly run. Filed as **FC-082**.

### Net verdict

Both reviews approve-with-changes; amendments above are plan-text plus three small spec upgrades (sentinel class, dataset-creation removal, reset seam). With these folded in, the plan is build-ready — proceed to the Opus build, then two adversarial code reviews + confirmation pass per house rules.

---

## Code-review disposition (2026-08-21) — PR #90

Opus build landed as `a487722` (DD-1/2/3) + `50982c8` (tests/docs) + `aab1ed7` (DD-6, isolated final commit); suite 1301 → 1341. Two adversarial code reviews (fresh Fable contexts: senior options trader/Python dev; production data-reliability engineer with live BQ access). **Both REQUEST_CHANGES; both explicitly found ZERO code defects** — each independently re-ran the full suite (1341 green), walked the neutrality contract end-to-end, verified field-by-field schema parity for all seven tables against live BQ, confirmed the sentinel is structurally inert and the DD-7 deletion total, and verified both declared build deviations with no undeclared ones. The union of findings and their dispositions:

- **BLOCKER (both, sequencing not code): R1 has not run** — verified live, all 7 tables lack `strategy_id`; merging first arms a *silent* 7-table row-dropping incident (`insert_rows_json` defaults `ignoreUnknownValues=false`; `_write`/`record_trade` log-and-swallow; ActivitiesIngestor deliberately returns 200 "partial"). **Disposition: this is the plan's own R1 gate, now recorded in-tree (Status line + §Execution below). ALTERs run next, verification pasted before merge.**
- **Recommended code fix (reviewer 1, LOW): configure-then-cache ordering race in `strategy_config()`** — a concurrent cold-start request could observe the cache non-None before the singleton was configured and silently drop its rows via the sentinel. **Fixed in `08460b5`** (configure before publishing the cache); full suite re-run green. Scoped confirmation pass follows per house rules.
- **MEDIUM (reviewer 2): singleton disabled-cache asymmetry** — pre-provisioning, the CC AnalyticsWriter singleton caches `_enabled=False` for the process lifetime (per-request writers self-heal post-provisioning; the singleton needs a restart). **Disposition: Phase 3 checklist note added to the parent plan** (provision the dataset before the CC service's first start, or bounce it after).
- **LOW (reviewer 2): `regression_monitor.py:51` `BQ_DATASET` env default** is a surviving read-side dataset default outside T7's pattern. **Disposition: folded into FC-082** (that file is already open there).
- **LOW (both, gate-hardening):** T7's regexes are pattern-bound (constant-indirection defaults, renamed params, or a differently-named interlock would pass). **Disposition: accepted as recorded in this plan's Critique-carried-forward — T1+T7 are the standing guards; FC-076 is the structural home.**
- **LOW (reviewer 2, pre-existing): `stock_history_from_alpaca` drift** — code declares `date`/`symbol` REQUIRED, live table has both NULLABLE. Predates this PR; harmless for inserts and the ALTER; matters only cosmetically for a fresh Phase-3 CC table. **Disposition: recorded here, no action.**
- **LOW (reviewer 2, pre-existing): `_reset_for_tests` doesn't clear the thread-local `set_analytics_writer` override.** Same exposure predates the PR; the simulator restores in `finally`. **Disposition: noted, no action.**
- **LOW (reviewer 1, cosmetic): sentinel's `dataset_id`/`strategy_id` return `None` against `-> str` annotations.** No caller string-ops them. **Disposition: accepted; tidy opportunistically in a future touch.**
- **Reviewer 1's PR-body "auto-deploy vs FC-081" flag is moot on stale premises:** FC-081 was resolved earlier the same evening (trigger rebound + three auto-builds verified); the reviewer read the plan text written before the fix.

## Execution

- **PR:** https://github.com/memon1987/agentic_options_wheel/pull/90 (branch `claude/fc-075-seam-4`: `a487722`, `50982c8`, `aab1ed7`, review fix `08460b5`)
- **R1 (7 live-table ALTERs): PENDING — gates merge.** `bq show` verification output to be pasted here when run.
- **Confirmation pass (scoped, on `08460b5`): pending.**
- Remaining after merge: R3 deploy (FC-081 fixed — trigger live), R4 per-table verify (windows per LOW batch), R5 FC-067 UPDATE (62 rows re-verified by reviewer 2 with the hardened predicate), R6 bookkeeping.
