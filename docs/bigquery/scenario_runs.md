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
| the Job | `deduplicated` | an identical `sweep_key` already reached `done`; nothing was replayed |
| the Job | `done` / `failed` | in a `finally`, after the cell rows are written |
| the dashboard | `failed` | the launch itself was refused (403/404/5xx from `jobs.run`) |

**Read the latest row per `run_id`, ordered by `written_at` DESC — never
`submitted_at`.** Every row of one submission carries the *same* `submitted_at`
(it is the partition key and the submission's identity), so ordering by it is a
three-way tie and "latest status wins" resolves arbitrarily. The canonical
ordering lives in `persist.LATEST_STATUS_ORDER_BY`, mirrored in
`dashboard/backend/services/sweeps.py` and pinned equal by a test:

```sql
-- latest status per run
SELECT * EXCEPT(rn) FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY run_id
    ORDER BY written_at DESC,
             CASE status WHEN 'deduplicated' THEN 2 WHEN 'done' THEN 3
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
| `sweep_key` | STRING | sha256[:16] of the canonical spec + `engine_version` + `git_commit`. The dedup key — see below |
| `status` | STRING REQ | `submitted` \| `running` \| `done` \| `failed` \| `deduplicated` |
| `deduplicated_to` | STRING | the earlier `run_id` whose results answer this submission |
| `submitted_at` | TIMESTAMP REQ | **partition key**; identical on every row of one run |
| `written_at` | TIMESTAMP REQ | per-row insert time; **this is what orders the sequence** |
| `started_at` / `finished_at` | TIMESTAMP | the Job's own clock |
| `submitted_via` | STRING | `dashboard` \| `cli` |
| `execution_name` | STRING | `CLOUD_RUN_EXECUTION`. Stored for operator debugging only — status is BigQuery-based (D3), because `run.executions.get` is unproven for this service account and grantable only in the console |
| `git_commit` / `engine_version` | STRING | provenance, and both are inputs to `sweep_key` |
| `base_config_hash` | STRING | `bq_writer.config_hash` — lines a sweep row up with a `backtest_runs` row |
| `base_config_json` | STRING | the `strategy`/`risk`/`earnings`/`rolling`/`universe` sections of the effective base config. **The payload, not just the hash** — a hash proves two runs matched and tells a reader nothing about what they matched on. `alpaca:` is excluded: these tables are read by a public dashboard |
| `spec_json` | STRING | the normalised submission (symbols, window, arms with their overrides and haircuts, cash, sensitivity) |
| `symbols` | STRING REPEATED | denormalised out of `spec_json` so scope is queryable without `JSON_VALUE` |
| `window_start` / `window_end` / `holdout_start` | DATE | |
| `in_sample_only` | BOOL | true when no holdout was asked for — **the default, and the dangerous case** |
| `scenario_count` | INTEGER | arms **including** the implicit `base` |
| `cell_count` | INTEGER | arms × symbols × splits |
| `wall_seconds`, `materialise_seconds`, `replay_seconds` | FLOAT | on terminal rows |
| `provider_fetches`, `bar_cache_hits` | INTEGER | network round-trips vs disk reads — **not the same number**, and conflating them describes an offline sweep as having called the vendor |
| `lake_summary_json` | STRING | the run's `ChainStore.summary()` — hits/misses/puts/rejects/errors |
| `error` | STRING | on a `failed` row |

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
| provenance | `engine_version`, `git_commit` |

**`scenario_hash` is the identity of the ARM; `config_hash` is not.**
`config_hash` hashes nine strategy parameters plus the module scoring constants,
so 12 of the 19 allowlisted override keys — every `rolling.*` and `earnings.*`
key, `universe.*`, `min_avg_volume` — do not move it, and the haircut it hashes
is the module default rather than the scenario's. Two arms can share a
`config_hash` and be entirely different experiments. Use `scenario_hash` to tell
arms apart; use `config_hash` only to line a row up with a `backtest_runs` row.

## `sweep_key` — what "the same sweep" means

`sha256(canonical spec + engine_version + git_commit)[:16]`, computed by
`src/backtesting/scenarios/identity.py` — one implementation, imported by the
Job and copied verbatim into the dashboard image.

Canonicalisation makes the key mean *the same question*, not *the same JSON*:
symbols are upper-cased and sorted, arms are sorted by `(name, arm hash)`,
override key order inside an arm does not matter, and an explicitly-declared
empty `base` arm is dropped (the runner prepends it anyway).

`engine_version` and `git_commit` are **in** the key on purpose. The same arms
replayed by a different engine build are a different experiment, and returning
the old rows for them would be the worst kind of cache hit — one that looks like
a result. A missing `git_commit` hashes as the empty string rather than raising:
a locally-run sweep has no commit stamp, and the consequence of a mismatch is
merely a missed cache, never a wrong answer.

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
