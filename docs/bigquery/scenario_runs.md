# `options_wheel.scenario_sweeps` / `options_wheel.scenario_runs`

The scenario store (FC-060 Layer 3). Written by
`src/backtesting/scenarios/persist.py` — from the `backtest-sweep` Cloud Run
Job, or from `python main.py --command sweep … --persist` — and read by the
dashboard's `/api/v2/sweeps*` endpoints.

Both tables are **day-partitioned on `submitted_at`** and **insert-only**.
Nothing in this system ever UPDATEs a row here.

> **These tables are not `backtest_runs`, and that separation is the point.**
> `backtest_runs`'s documented "current demotion candidates" query takes the
> latest `run_kind='full'` row (`docs/bigquery/backtest_runs.md`), so a
> persisted full-universe sweep written into it would displace a real screen
> with a hypothetical. Neither table here has a `run_kind` column at all, which
> makes that mistake unrepresentable rather than merely discouraged. **A sweep
> is a hypothesis; a screen is a record of the universe.**

## `scenario_sweeps` — one row per status transition

A submission produces a *sequence* of rows, not one row edited in place:

| written by | status | when |
|---|---|---|
| the dashboard (`POST /api/v2/sweeps`) | `submitted` | before the Job is launched |
| the dashboard, again | `submitted` | immediately after launch, now carrying `execution_name` |
| the Job | `running` | right after `Config()` — before the dedup lookup |
| **the Job only** | `deduplicated` | an identical `sweep_key` already reached `done` under the same effective config; nothing was replayed |
| the Job | `done` / `failed` | in a `finally`, after the cell rows are written |
| the dashboard | `failed` | the launch itself was refused (403/404/5xx from `jobs.run`) |

**Read the latest row per `run_id`, ordered by `written_at` DESC — never
`submitted_at`.** Every row of one submission carries the *same* `submitted_at`
(it is the partition key and the submission's identity), so ordering by it is a
three-way tie and "latest status wins" resolves arbitrarily. The canonical
ordering lives in `persist.LATEST_STATUS_ORDER_BY`, mirrored in
`dashboard/backend/services/sweeps.py` and pinned equal by a test. **`done`
outranks `failed`** in the tiebreak: the one realistic collision is the launch
path — the API writes `failed` when `jobs.run` errors while an execution that in
fact started writes `done` — and a sweep whose cells are in the table is done,
whatever a launch-side timeout thought.

```sql
-- latest status per run
SELECT * EXCEPT(rn) FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY run_id
    ORDER BY written_at DESC,
             CASE status WHEN 'deduplicated' THEN 2 WHEN 'done' THEN 4
                         WHEN 'failed' THEN 3 WHEN 'running' THEN 1
                         WHEN 'submitted' THEN 0 ELSE -1 END DESC) AS rn
  FROM `options_wheel.scenario_sweeps`
)
WHERE rn = 1
ORDER BY submitted_at DESC
```

### Schema

| column | type | notes |
|---|---|---|
| `run_id` | STRING REQ | 16 hex characters; shared by every row of one submission and by its `scenario_runs` cells |
| `sweep_key` | STRING | sha256[:16] of the canonical spec + `engine_version` + `engine_identity`. The dedup key — see below |
| `status` | STRING REQ | `submitted` \| `running` \| `done` \| `failed` \| `deduplicated` |
| `deduplicated_to` | STRING | the earlier `run_id` whose results answer this submission |
| `submitted_at` | TIMESTAMP REQ | **partition key**; identical on every row of one run |
| `written_at` | TIMESTAMP REQ | per-row insert time; **this is what orders the sequence** |
| `started_at` / `finished_at` | TIMESTAMP | the Job's own clock |
| `submitted_via` | STRING | `dashboard` \| `cli` \| `sim-service` \| `smoke` \| `battery` (PR-d). Free-form by schema, but **not by any writer**: the sim service accepts `smoke` only, and only through the `X-Sim-Provenance` header against a closed allowlist (`sim_service.PROVENANCE_ALLOWLIST`) — anything else is a 422 rather than a silent downgrade. `smoke` marks the rows the **deploy smoke test** writes, and it is not a rare case: `smoke-test-sim`'s `POST /simulate` is a dedup hit only while the engine identity has not moved, so **every build that touches `src/**` or `requirements.txt` makes it a real ~45 s replay with real rows**. Battery and trend queries must exclude `submitted_via = 'smoke'` |
| `liveness_seconds` | INTEGER | FC-096 Phase B PR-c. How long a **non-terminal** row of this run may go without an update before a reader may declare it dead. **NULL means "use the Job's clock"** — `JOB_TASK_TIMEOUT_SECONDS` (3 h) plus the reader's 10-minute grace — which is what every row written before this column existed means and what every sweep-Job row still means. The sim service stamps **900**, so a scaled-in service instance releases the one-at-a-time submit lock in **~25 minutes** instead of 3 h 10 m. Read by `services/sweeps.row_liveness_seconds`, which treats junk and non-positive values as absence: shortening the bound is the dangerous direction (it would release the lock under a run that is still going, and two replays would contend for one chain cache) |
| `execution_name` | STRING | `CLOUD_RUN_EXECUTION`. Stored for operator debugging only — status is BigQuery-based (D3), because `run.executions.get` is unproven for this service account and grantable only in the console |
| `git_commit` | STRING | the commit the run was launched from. **Provenance only since FC-096 Phase B** — it used to be half of `sweep_key` and no longer is |
| `engine_version` | STRING | `screen.ENGINE_VERSION`; an input to `sweep_key` |
| `engine_identity` | STRING | sha256[:16] of the **contents of `src/**`** (`src/backtesting/scenarios/engine_identity.py`); the other input to `sweep_key`, and a dedup predicate in its own right. **NULL on every row written before 2026-09-01** — those rows were keyed by a commit SHA, so they are readable by `run_id` for ever and can never be served as a dedup hit |
| `base_config_hash` | STRING | sha256[:16] of the **effective** snapshot below. This is the dedup's configuration guard, not the `backtest_runs` linkage — see *Effective, not as written* |
| `base_config_json` | STRING | the `strategy`/`risk`/`earnings`/`rolling`/`universe` sections **plus an `effective` block read through `Config`'s accessors**. The payload, not just the hash — a hash proves two runs matched and tells a reader nothing about what they matched on. `alpaca:` is excluded: these tables are read by a public dashboard |
| `spec_json` | STRING | the normalised submission (symbols, window, arms with their overrides and haircuts, cash, sensitivity) |
| `symbols` | STRING REPEATED | denormalised out of `spec_json`, in **declaration order** (de-duplicated in place, never sorted) — the grid's columns are read in the order the operator typed their universe, and the API shapes the grid from this same list |
| `window_start` / `window_end` / `holdout_start` | DATE | |
| `in_sample_only` | BOOL | true when no holdout was asked for — **the default, and the dangerous case** |
| `scenario_count` | INTEGER | arms **including** the implicit `base` |
| `cell_count` | INTEGER | arms × symbols × splits |
| `wall_seconds`, `materialise_seconds`, `replay_seconds` | FLOAT | on terminal rows |
| `provider_fetches`, `bar_cache_hits` | INTEGER | network round-trips vs disk reads — **not the same number**, and conflating them describes an offline sweep as having called the vendor |
| `lake_summary_json` | STRING | the run's `ChainStore.summary()` — hits/misses/puts/rejects/errors |
| `error` | STRING | on a `failed` row |
| `rows_persisted` | INTEGER | how many `scenario_runs` rows actually landed. NULL unless the insert succeeded |
| `error_cells` | INTEGER | cells whose `error IS NOT NULL`. Counted from the cells, not inferred from the exit code |
| `artifacts_complete` | BOOL | FC-096 Phase B. TRUE when every **non-errored** cell also stored its detail artifact in GCS; FALSE when some did not (the console will show empty ledgers, and that is a storage failure rather than a quiet replay) — **including a run that crashed mid-replay having already written some objects**, which is incomplete rather than empty; **NULL** when the question does not apply: the run wrote no artifacts at all (a CLI run without `--persist`, a row written before the column existed), or it had **no non-errored cell to write one for** (every arm errored — `0 == 0` would claim a complete set the run has not one object of). NULL is not a defect |
| `engine_config_hash` | STRING | `bq_writer.config_hash` — nine strategy keys plus the scoring constants. This is the column that lines a sweep row up with a `backtest_runs` row; `base_config_hash` is a different hash answering a different question (below) |

### Effective, not as written

`base_config_json` carries two layers, and where they disagree the second one is
the truth:

1. the raw yaml sections, so the payload stays readable a year later;
2. an `effective` block read through `Config`'s **accessors** — every
   allowlisted override key plus `rolling.dry_run`.

The yaml is not the configuration. `EARNINGS_ENABLED`, `ROLLER_ENABLED` and
`ROLLER_DRY_RUN` win over their keys at runtime (FC-013 DD-7, FC-078 DD-7), so a
snapshot read from the file alone records a gate as ON that the run had OFF.
`base_config_hash` hashes layer 2, which is what makes it safe for the dedup to
read: two sweeps either side of a kill switch have different hashes and cannot
be served as one another's answer.

`config_hash` could not do this job — it covers nine strategy keys and the
scoring constants, so every `rolling.*` and `earnings.*` knob is invisible to
it. It is still stored, as `engine_config_hash`, because it is the only thing
that lines a sweep row up with a `backtest_runs` row.

### What `done` means

**`done` is not "the process exited".** It is written only when all three hold:

- the sweep did not raise;
- `write_runs` returned True — the cells are in `scenario_runs`;
- `rows_persisted` is recorded on the row.

Anything else is `failed`, carrying the reason (`cell rows not persisted: …`
when the sweep finished but its insert did not land). A run that produced cells
but errored on every one of them is still `done` — it ran — and is excluded from
the dedup by `error_cells` instead.

### The dedup predicate

`find_done_sweep` — in `persist.py` for the Job and in
`services/sweeps.py` for the API — requires **four** things, not one status
string:

```sql
status = 'done'
AND error_cells = 0
AND rows_persisted IS NOT NULL AND rows_persisted = cell_count
AND (@base_config_hash IS NULL OR base_config_hash = @base_config_hash)
AND engine_identity = @engine_identity
```

Each clause exists because `status = 'done'` alone would return something that
is not an answer: a run whose rows never landed (an empty grid), a run every arm
of which errored (a page of `err`, and the operator never learns to re-run), or
a run under a different effective base config (`sweep_key` covers the spec, the
engine version and the engine identity — it cannot see a kill switch flipped on
the Job), or a run whose `engine_identity` is NULL because it predates the
FC-096 re-key.

A duplicate replay costs eight minutes. A wrong dedup hit serves one
experiment's numbers as another's, silently.

**Only the Job deduplicates.** The API never skips a launch on the strength of
this query, and it binds no `base_config_hash`.

It used to: it passed the last `base_config_hash` any run on the same commit had
recorded. That was self-referential. After an operator flipped
`ROLLER_ENABLED` on the Job, a re-submitted spec matched the *pre-flip* run's own
hash, deduplicated to it, and — because nothing was launched — the Job's exact
check, the one that would have caught the flip, never ran. The dedup became
permanent and wrong, and nothing said so.

So `POST /api/v2/sweeps` always launches. `find_done_sweep` is still consulted
there, purely as a **hint**: the earlier run id comes back as
`prior_done_run_id` so an operator can open it while the new execution starts.
The Job then decides, against the config it is actually holding, and writes the
`deduplicated` row itself. The cost is one container start (~3-4 min) on a repeat
submission — the right price for never serving one experiment's numbers as
another's.

`force: true` suppresses the Job's dedup lookup — the one that actually decides
— and the API's hint lookup with it.

### `force`

The spec accepts an optional `force: true`. It **skips the Job's dedup lookup**
— the only one that decides anything — and suppresses the API's hint lookup with
it, so a forced submission neither dedups nor advertises a prior run. It is validated as a bool, travels in `SWEEP_SPEC_JSON`, and is
deliberately **excluded from `sweep_key`** (`identity.NON_IDENTITY_FIELDS`): a
forced re-run must key identically to the run it is reproducing, or the two are
never comparable and a second force would not dedup either.

## `scenario_runs` — one row per cell

One row per (scenario × symbol × split). Clustered on
`run_id, scenario_name, symbol`.

**Every cell gets a row, including errored ones.** Dropping them would make a
half-run sweep read as a complete one — the same rule `build_row` follows for a
failed screen symbol, and for the same reason: "this arm was not measured" and
"this arm was fine" must not look alike.

### Reading a cell honestly — the four states

`error`, `insufficient`, `low_activity` and `measured` **partition** every cell:
exactly one is true. The flags are computed by the engine
(`runner.ScenarioResult`) and stored; **do not re-derive them**, and do not
average anything that is not `measured`.

| state | means | why it must never be rendered as a return |
|---|---|---|
| `insufficient` | the window contained no completed cycle | rendering it as 0% makes "nothing happened" look like a measured flat result |
| `low_activity` | a position was held on under `MIN_DAYS_IN_POSITION` (25%) of decision days | the annualised number rests on capital that mostly sat idle; the fewer days deployed, the more one lucky trade is multiplied by 365/days |
| `error` | the cell was never measured | not implicitly fine |
| `measured` | carries a number worth ranking | the only state that belongs in a median |

`insufficient` **wins** over `low_activity` (a window with no cycle also has a
tiny days-in-position fraction). Without that precedence a cell counts twice and
the summary row stops adding up.

### Schema

| group | columns |
|---|---|
| identity | `run_id`, `submitted_at` (partition), `written_at` |
| arm | `scenario_name`, `scenario_hash`, `config_hash`, `overrides_json`, `fill_haircut` |
| cell | `symbol`, `split` (`all` \| `fit` \| `holdout`), `window_start`, `window_end` |
| verdict | `verdict`, `demote`, `insufficient`, `low_activity`, `measured` |
| performance | `total_return`, `annualized_return`, `annualized_return_on_collateral`, `benchmark_return`, `excess_return`, `option_pnl`, `stock_pnl_realized`, `stock_pnl_unrealized`, `max_drawdown`, `win_rate`, `assignment_rate` |
| activity | `puts_sold`, `calls_sold`, `cycles_completed`, `cycles_open`, `decision_days`, `days_in_position_fraction` |
| fill sensitivity | `bid_fill_return`, `verdict_flips_on_fill` |
| cost / failure | `replay_seconds`, `error` |
| provenance | `engine_version`, `git_commit`, `engine_identity` |

**`scenario_hash` is the identity of the ARM; `config_hash` is not.**
`config_hash` hashes nine strategy parameters plus the module scoring constants,
so 12 of the 19 allowlisted override keys — every `rolling.*` and `earnings.*`
key, `universe.*`, `min_avg_volume` — do not move it, and the haircut it hashes
is the module default rather than the scenario's. Two arms can share a
`config_hash` and be entirely different experiments. Use `scenario_hash` to tell
arms apart; use `config_hash` only to line a row up with a `backtest_runs` row.

## `sweep_key` — what "the same sweep" means

`sha256(canonical spec + engine_version + engine_identity)[:16]`, computed by
`src/backtesting/scenarios/identity.py` — one implementation, imported by the
Job and copied verbatim into the dashboard image.

Canonicalisation makes the key mean *the same question*, not *the same JSON*:
symbols are upper-cased and sorted, arms are sorted by `(name, arm hash)`,
override key order inside an arm does not matter, and an explicitly-declared
empty `base` arm is dropped (the runner prepends it anyway).

`engine_version` and `engine_identity` are **in** the key on purpose. The same
arms replayed by a different engine build are a different experiment, and
returning the old rows for them would be the worst kind of cache hit — one that
looks like a result.

### `engine_identity` — why the key is not the commit (FC-096 Phase B)

The third component was `git_commit` until 2026-09-01. That was sound and far
too coarse: **every** merge to `main` invalidated **every** stored result,
including the merges that cannot possibly change a replay — a README edit, a
dashboard CSS tweak, a `cloudbuild.yaml` flag. Under Phase B's weekly battery
that is the difference between a cheap Saturday and an expensive one.

`engine_identity` asks the narrower question: *did the code the replay executes
change?* It is `sha256[:16]` over the sorted relative paths and the **contents**
of every file under `src/`, of every type, plus the repo-root `requirements.txt`,
plus `ENGINE_VERSION`:

- **all of `src/`, not just the backtesting package** — the replay drives the
  live strategy, so `src/strategy/**`, `src/api/**`, `src/data/**` and
  `src/utils/**` are executed inside a simulated day;
- **all file types, not just `*.py`** — `src/backtesting/data/earnings_dates.json`
  and `dividend_history.json` are committed replay *inputs*; a table correction
  changes results without changing a line of code;
- **plus `requirements.txt`** — the replay's arithmetic runs inside pandas,
  numpy and the Alpaca SDK, and this file says which of them the image installs.
  Under `git_commit` a dependency edit invalidated the cache; a `src/**`-only
  hash would have quietly stopped doing that, which is a regression the re-key
  must not smuggle in;
- **nothing else outside `src/`** — `docs/`, `dashboard/`, `deploy/`, `tests/`
  and `cloudbuild.yaml` cannot change a replay's output;
- **content, never metadata** — no mtime, no directory order (the walk is sorted
  on the relative path, because the Job and the Cloud Build worker that stamps
  the dashboard image are two different filesystems with two different readdir
  orders); `__pycache__` and `*.pyc` are skipped, being derived; a **symlink
  anywhere under `src/` is refused outright**, because a followed link reads
  bytes from outside the boundary the hash is defined by.

**Residual: rebuild-resolution drift is outside the key.** `requirements.txt` is
fully unpinned today (`pandas`, not `pandas==2.1.4`), so two builds of the
identical file can resolve different wheel versions and still hash the same. The
key sees the file's *content*; it cannot see what pip decided on a given
afternoon. The discipline that covers the gap is manual: **bump `ENGINE_VERSION`
on any dependency change that could move replay numerics**, pinned or not.
Pinning the file would fold resolution back into the key and is the real fix — a
separate change with its own review.

`git_commit` is still stamped on every row. It is provenance; it is simply no
longer identity.

**Where each side gets the value.** The Job and the CLI call
`engine_identity.engine_identity()` on the tree they are running. The dashboard
image ships two flat stdlib modules and **no `src/` tree**, so it cannot hash
one: `cloudbuild.yaml`'s `compute-engine-identity` step runs the *same shared
module* against the checkout that builds the image and bakes the answer in as the
`ENGINE_IDENTITY` env var. If that env var is missing, the dashboard **disables
its dedup hint and logs why** — it never keys over a fallback, because a key
computed over `""` is a valid-looking 16-hex string that collides across
genuinely different engines.

**Migration.** The column is additive and NULLABLE. The engine's writer
reconcile adds it on the Job's next run; the two live tables were also altered
directly (`ALTER TABLE <dataset>.scenario_sweeps ADD COLUMN engine_identity
STRING`, same for `scenario_runs`) so the dashboard did not have to wait for a
Job. Until that column exists the dashboard **degrades rather than failing**: the
dedup hint reports a miss and logs `sweep_dedup_hint_disabled`, and the
`submitted` insert is retried once without the field, logging
`engine_identity_column_missing`. Submissions keep working throughout.

**One-time invalidation, on the record.** Every row written before the re-key has
a NULL `engine_identity` and a `sweep_key` computed over a commit. The first
re-submission of each old spec recomputes once and is stored under the new key;
the old rows stay readable by `run_id` for ever. The first weekly battery after
any engine change is the expensive one — that is the design, not a regression.

## Useful queries

```sql
-- Recent sweeps and where they got to
SELECT run_id, status, submitted_at, symbols, scenario_count, cell_count,
       wall_seconds, deduplicated_to, execution_name, error
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY run_id
    ORDER BY written_at DESC) AS rn
  FROM `options_wheel.scenario_sweeps`)
WHERE rn = 1
ORDER BY submitted_at DESC
LIMIT 20;

-- The grid for one run, MEASURED cells only (never average the others)
SELECT split, scenario_name, symbol, verdict, annualized_return
FROM `options_wheel.scenario_runs`
WHERE run_id = '<run_id>' AND measured
ORDER BY split, scenario_name, symbol;

-- How much of a run was actually measurable
SELECT split, scenario_name,
       COUNTIF(measured) AS measured,
       COUNTIF(insufficient) AS insuf,
       COUNTIF(low_activity) AS low_act,
       COUNTIF(error IS NOT NULL) AS errored
FROM `options_wheel.scenario_runs`
WHERE run_id = '<run_id>'
GROUP BY split, scenario_name
ORDER BY split, scenario_name;

-- Every sweep that ran the same question (dedup audit)
SELECT sweep_key, COUNT(DISTINCT run_id) AS runs,
       ARRAY_AGG(DISTINCT status) AS statuses
FROM `options_wheel.scenario_sweeps`
GROUP BY sweep_key
HAVING runs > 1;
```

## What the API serves — `GET /api/v2/sweeps/{run_id}`

`services/sweeps.shape_results` returns the stored rows plus everything derived
from them, so the UI renders rather than recomputes. Beyond the grid, the
per-scenario summary, `delta_vs_base`, `sign_agreement` and the bias footer, the
payload carries three fields read straight off the persisted rows:

| field | shape | from |
|---|---|---|
| `scenario_hashes` | `{scenario_name: sha}` | `scenario_runs.scenario_hash` |
| `scenario_config_hashes` | `{scenario_name: sha}` | `scenario_runs.config_hash` |
| `windows` | `{split: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}` | `scenario_runs.window_start` / `window_end` |

A run with no cells yet (`submitted`, `running`) returns `grid: {}`,
`splits: []`, `windows: {}` and empty hash maps — the page has to render that
state, not 500. `GET /api/v2/sweeps` stays a bare JSON array of rows, each with
an added `stuck` boolean.

`POST /api/v2/sweeps` answers **202 Accepted** — the sweep runs for minutes in a
Job and the caller polls the detail route. Its body carries `run_id`,
`sweep_key`, `execution_name`, `cell_count`, `forced`, `deduplicated_to` (always
`null` from this endpoint) and `prior_done_run_id` (the hint described above,
`null` when there is none or when `force` was set).

## Detail artifacts (FC-096 Phase B)

A `scenario_runs` row is ~30 numbers. The replay behind it held a daily equity
curve, a complete money/position ledger, the wheel cycles those events
reconstruct into, the roller's records and the rejection tally — all of which
used to be discarded after aggregation. Since FC-096 Phase B every non-errored
cell of a **persisting** run also writes one gzipped JSON object:

```
gs://gen-lang-client-0607444019-options-data/sim-artifacts/v1/<run_id>/<scenario>__<symbol>__<split>.json.gz
```

Read one with
`GET /api/v2/sweeps/{run_id}/artifacts/{scenario}/{symbol}/{split}` (served
decompressed as `application/json`).

Four things on the object exist so a correct artifact cannot tell a reader
something false, and all four are worth knowing before querying one:

- **`provenance.fill`** — the fill assumption of the SERIALISED replay, always
  `{"basis": "mid", "fill_haircut": <arm's haircut>}`. The row's
  `bid_fill_return` comes from a SECOND replay at the bid that deliberately gets
  no artifact; without this stamp, comparing the two would be comparing two
  different runs.
- **`provenance.masked_reach`** — this ARM's DTE reach and the chain cutoff it
  implies (`max_dte + UNIVERSE_DTE_BUFFER`, carried alongside as `dte_buffer` so
  a reader never has to know the constant's current value), never the sweep-wide
  materialisation reach (carried separately as `sweep_max_dte`, for context
  only). Since FC-096 Phase A PR-2
  each arm replays against a chain view masked to its own reach, so stamping the
  parent's number would describe a chain the cell never saw.
- **`rejections`** — an ORDERED list (`[{reason, days}, ...]`), ranked as
  `RejectionTally.summary()` ranked it, plus `binding_constraint`. A list rather
  than an object because the ranking is the answer and JSON objects have no
  guaranteed order. Post-FC-092 an EMPTY tally now means "nothing was blocked"
  rather than "the logger cached the first run's tally".
- **`daily[].shares_held`** — per-symbol share counts on every curve row. Equity
  alone cannot distinguish a cash account from an assigned one holding the same
  dollars, which is the first thing a reader needs from a wheel drawdown.

`"schema": 1` is in the JSON and mirrored by the `v1` in the object prefix; a
frozen-fixture test (`tests/test_sim_artifacts.py`) pins the full key set,
including the per-`kind` `LedgerEvent.detail` keys and the roll record's — which
is `CallRoller.execute_roll`'s success dict (`success`, `underlying`,
`old_strike`, `new_strike`, `contracts`, `net_credit`, `btc_order_id`,
`stc_order_id`) plus the ISO `day` the replay stamps on at capture, because
production's record carries no date at all (the log line's timestamp is the
date). Adding a field is additive; removing or renaming one bumps both.

**What the artifact deliberately does NOT carry: any price series.** There is no
underlying bar history and no buy-and-hold benchmark curve on the object — only
what the replay itself produced (trades, the equity curve, cycles, rolls, the
tally). Phase E's price-overlay and vs-benchmark components read bars through
their own path, defined in Phase E's plan; duplicating a bar series into every
cell's artifact would multiply one shared series by `arms x symbols x splits` and
create a second, staler copy of data the lake already owns.

**Storage is best-effort and accounted for.** Artifacts are evidence, not
results: a GCS failure logs `sim_artifact_write_failed`, is counted, and never
fails a cell. `artifacts_complete` on the terminal `scenario_sweeps` row is what
tells an operator whether the set is whole, without listing the bucket. There is
no lifecycle rule on the prefix today; revisit at ~1 GB.

## Operational notes

- **The tables are created by the Job's writer**, not by the dashboard. The
  dashboard's only write is the `submitted` row, and it deliberately does not
  auto-create: one schema owner, and it is the side that knows every column. A
  submit issued before the first Job execution therefore fails loudly rather
  than creating a table with half a schema.
- **Schema reconcile is additive only.** A new field is added to an existing
  table on the next writer construction; a removed or retyped field is a
  migration and must be a deliberate operator action. This matters more than it
  looks: `insert_rows_json` rejects the **whole request** on one unknown key, so
  an unreconciled new column means a run writes ZERO rows and still reports
  itself clean.
- **`--max-retries=0` on the Job is load-bearing.** A retried task would replay
  under the same `SWEEP_RUN_ID` and write a second full set of `scenario_runs`
  rows for one run, and the results view would render every cell twice.
- **`--task-timeout=10800` (3h) is the API's liveness clock.** A cold window
  materialises at ~5.5 min/symbol (~50 s warm), so a 12-symbol year over
  unseeded chains is comfortably past an hour. `services/sweeps.py` mirrors the
  number as `JOB_TASK_TIMEOUT_SECONDS` and a contract test pins the two equal:
  the one-at-a-time lock releases, and a `running` row is labelled `stuck`, once
  the row's own `written_at` is older than that plus a 10-minute grace — the
  reasoning being that Cloud Run has killed the task by then. Set it lower than
  the Job's and the lock releases mid-replay, letting a second execution contend
  for the same chain cache.
- **A retyped column raises rather than being ignored.** The schema reconcile is
  additive only; a column that exists with the wrong type would make every
  insert fail on that field, so the sweep writes zero rows while reporting a
  clean run. `_ensure_table` refuses to start in that state and names the
  offending columns — retyping is an operator migration.
- **Termination is recorded, from the first write onward.** Cloud Run sends
  SIGTERM and SIGKILL 10 s later; `main.py` installs a handler that raises, so
  the `finally` still writes a terminal row. The handler goes on **before** the
  `running` insert — the insert, the dedup query (up to 60 s) and the chain
  lake's bucket probe all precede the replay, and a cancel there is exactly the
  case that used to orphan a `running` row. During the terminal writes SIGTERM
  is set to `SIG_IGN` instead, because that stretch is the one that must not be
  interrupted; the ten-second grace is the budget for those two inserts.
- **The 409 lock and the `stuck` label agree.** A `submitted` row releases the
  lock at the same 10-minute threshold at which it is labelled stuck (a launch
  that produced no `running` row by then is not running). A `running` row keeps
  the task-timeout + grace rule, because a cold sweep may legitimately still be
  replaying.
