# `options_wheel.backtest_runs`

One row per **symbol per screening run** (FC-032 Phase 5). Written by
`src/backtesting/reporting/bq_writer.py` via the `/backtest/screen` endpoint or
`python main.py --command screen`.

Day-partitioned on `timestamp`.

> **PROVENANCE — rows written before 2026-07-29 describe a put-only engine.**
> FC-048 found that covered calls were misrouted and rejected, so every backtest
> before merge commit `ea5cfa5` modelled the put half of the wheel only: assigned
> shares were never called away, no wheel cycle ever completed, and call premium was
> never earned. **Do not compare rows across that boundary.** Older rows are left in
> place deliberately — provenance is the `timestamp` plus `config_hash`, and deleting
> history would lose the audit trail. See `docs/investigations/fc-048-revalidation.md`.

## What this table is for

Answering "which symbols we currently trade have stopped being a fit?" — and
keeping a queryable audit trail of *why*, under *which* configuration.

**`demote` is a recommendation, not an action.** Nothing in the pipeline changes
the trading universe. The plan requires two observed screening cycles before any
automation is considered, and the engine's known biases are the reason.

## Reading a row honestly

Three columns exist specifically to stop the headline number being misread:

| column | why it matters |
|---|---|
| `config_hash` | A verdict is uninterpretable without the thresholds that produced it. Two runs with different hashes are not comparable — a changed premium floor looks identical to a changed symbol. |
| `option_pnl_share` | Fraction of gross P&L from premium rather than the stock leg. Published wheel studies find 94–99% comes from the stock; a "profitable" symbol at a low share is mostly a long position. `NULL` when the legs have opposite signs, because a percentage would mislead. |
| `days_in_position_fraction` | Time occupancy. A symbol that cannot clear the premium floor shows a flattering return on the handful of days it traded — KMI scored +138% annualized on one 8-day cycle in 273 days. |

Also: `reconciliation_gap` should be ~0. A non-zero value means a cash flow
escaped the attribution columns, and the split between `option_pnl` and the
stock columns should not be trusted for that row.

And `verdict_flips_on_fill` — if true, the verdict depends on whether fills come
at mid or at the bid, which means it is not a verdict.

## Schema

### Run identity
| column | type | notes |
|---|---|---|
| `run_id` | STRING | Shared by every row of one screening run |
| `timestamp` | TIMESTAMP | Partition key |
| `symbol` | STRING | |
| `window_start` / `window_end` | DATE | Evaluation window |
| `config_hash` | STRING | 16-char hash of the thresholds **and scoring constants** that shaped the verdict |
| `engine_version` | STRING | |
| `run_kind` | STRING | `full` (whole configured universe) or `adhoc` (a subset). **Always filter on this** when asking "what is the state of the universe?" — an ad-hoc probe is more recent but answers a different question |
| `universe_size` | INTEGER | Symbols attempted in this run |

### Verdict
| column | type | notes |
|---|---|---|
| `verdict` | STRING | `fit` / `marginal` / `unfit`; NULL when the symbol errored |
| `demote` | BOOL | `verdict == 'unfit'`. Recommendation only |
| `verdict_reasons` | STRING[] | Ordered, prefixed `BLOCK:` / `WARN:` / `OK:` |
| `binding_constraint` | STRING | Which filter blocked the most days. **NULL by artifact on every row written before the FC-096 Phase B PR-a deploy (PR #109, `f18d0c5`, 2026-09-01), except the first symbol of each run** — see below |

#### `binding_constraint` is NULL by artifact on historical rows (FC-092)

Until 2026-09-01 the `RejectionTally` only counted the FIRST replay in a
process. `setup_logging` sets structlog's `cache_logger_on_first_use=True`; a
`BoundLoggerLazyProxy` caches its whole processor chain on first use, and the
`structlog.configure()` the tally used to install itself does not invalidate
that cache — so every strategy logger bound replay #1's tally and delivered to
it for the life of the process.

The monthly screen evaluates 14 symbols in one process. **13 of every 14 rows
therefore carry an empty `blocked_days_by_reason`, `candidate_days = 0` and a
NULL `binding_constraint`** — not because the strategy was never blocked, but
because nothing counted it. Separately, `summary()` iterated a set, so the
reported constraint could differ between two runs of the same replay depending
on `PYTHONHASHSEED`.

Both are fixed by **FC-096 Phase B PR-a (PR #109, commit `f18d0c5`, merged
2026-09-01)** — cite the commit rather than only the date when auditing a row,
because the deploy that carried it is what actually moves the boundary: the
tally binds through
`utils.logger.tally_dispatch`, a process-stable processor that reads the active
tally from a contextvar, and the ranking is sorted with an explicit tiebreak.
**Nothing was backfilled** — the honest repair is to re-run the screen, not to
invent counts for replays whose events are gone. When reading rows older than
2026-09-01, treat a NULL `binding_constraint` as "not measured", never as "never
blocked".

### Performance
`starting_cash`, `final_equity`, `total_return`, `annualized_return`,
`annualized_return_on_collateral`, `benchmark_return`, `excess_return` — all FLOAT.

Prefer **`annualized_return_on_collateral`**. `total_return` divides by the whole
account, so it is dominated by the arbitrary per-symbol notional and mostly
measures idle cash.

### Attribution
`option_pnl`, `stock_pnl_realized`, `stock_pnl_unrealized`, `option_pnl_share`,
`reconciliation_gap` — all FLOAT. Realized and unrealized are separate because
counting only realized understates the stock leg whenever a cycle is still open.

### Activity and risk
`decision_days`, `days_in_position`, `days_in_position_fraction`,
`cycles_completed`, `cycles_open`, `puts_sold`, `calls_sold`, `win_rate`,
`assignment_rate`, `max_drawdown`, `days_underwater`, `avg_collateral`.

`win_rate` is high by construction at 0.10–0.20 delta — read it beside
`max_drawdown` and `days_underwater`, never alone.

### Fill sensitivity and provenance
`bid_fill_return`, `verdict_flips_on_fill`, `known_biases` (STRING[]), `error`.

A symbol that failed still gets a row with `error` set and a NULL verdict.
**A NULL verdict does not mean the symbol is fine — it means it was never
checked.** Filter explicitly.

## Running a full screen

**The `/backtest/screen` endpoint is DISABLED by default** (503 unless
`ENABLE_SCREEN_ENDPOINT=true`), and should stay that way until the Cloud Run Job
below is deployed.

Measured cost is **~25 minutes per symbol** cold — ~50 with the sensitivity pass
— because `ChainStore` is not yet wired in and Cloud Run's filesystem is
ephemeral regardless. Against a 300s request timeout, no synchronous request
finishes even a single symbol. Running it there would burn Alpaca quota the live
bot shares and time out with no record. (A timeout does not corrupt the table:
persistence is one write after the loop, so a timeout writes zero rows.)

Run the full universe as a batch job instead:

```bash
# Locally (writes to options_wheel.backtest_runs):
python main.py --command screen

# As a Cloud Run Job (the intended monthly path):
gcloud run jobs create backtest-screen \
  --image gcr.io/gen-lang-client-0607444019/options-wheel-strategy \
  --region us-central1 \
  --task-timeout 3600s \
  --set-env-vars GCP_PROJECT=gen-lang-client-0607444019 \
  --command python --args "main.py,--command,screen"

gcloud run jobs execute backtest-screen --region us-central1
```

Then point a monthly Cloud Scheduler job at the Job's `:run` endpoint rather
than at the HTTP service.

**Not yet deployed.** Four scheduler jobs are currently PAUSED because they
target endpoints deleted in Phase 0: `monthly-performance-review`
(`/backtest/performance-comparison`), `daily-quick-backtest` and
`weekly-comprehensive-backtest` (`/backtest`), and `daily-cache-maintenance`
(`/cache/cleanup`). `monthly-performance-review` is the one to re-point at the
Job; the other three have no replacement and should be deleted.

Before the first real screening run is used for a demotion decision, two gaps
should close: wire `ChainStore` (turns hours into minutes) and model dividends
(the bias runs **toward** demoting on income names — see below).

## Queries

Current demotion candidates from the most recent run:
```sql
-- run_kind='full' is load-bearing: an ad-hoc subset run is more RECENT but is
-- not a picture of the universe. Without it this silently returns the ad-hoc
-- run's symbols and hides the full screen's demotion candidates.
WITH latest_full AS (
  SELECT run_id FROM `options_wheel.backtest_runs`
  WHERE run_kind = 'full'
  ORDER BY timestamp DESC LIMIT 1
)
SELECT symbol, verdict, total_return, annualized_return_on_collateral,
       days_in_position_fraction, ARRAY_TO_STRING(verdict_reasons, '; ') AS reasons
FROM `options_wheel.backtest_runs`
WHERE run_id IN (SELECT run_id FROM latest_full) AND demote
ORDER BY total_return;
```

Symbols that were never actually checked (do not mistake these for passes):
```sql
SELECT symbol, error FROM `options_wheel.backtest_runs`
WHERE verdict IS NULL
  AND run_id = (SELECT run_id FROM `options_wheel.backtest_runs`
                WHERE run_kind = 'full'
                ORDER BY timestamp DESC LIMIT 1);
```

Verdict drift for one symbol — only meaningful within a single `config_hash`:
```sql
SELECT DATE(timestamp) AS run_date, config_hash, verdict,
       total_return, option_pnl_share
FROM `options_wheel.backtest_runs`
WHERE symbol = 'NVDA'
ORDER BY timestamp DESC;
```

Rows whose attribution does not reconcile (treat their split as unreliable):
```sql
SELECT run_id, symbol, reconciliation_gap
FROM `options_wheel.backtest_runs`
WHERE ABS(IFNULL(reconciliation_gap, 0)) > 0.01;
```

## Known biases carried in every row

`known_biases` lists them by title; the full text is in each run's markdown
report and in `src/backtesting/reporting/report.py`. The two that most affect a
demotion decision:

- **Dividends are not modeled, and the bias runs BOTH ways.** It *flatters* the
  wheel on `excess_return` (the benchmark holds shares every day and forgoes the
  whole dividend stream — ~15 points on a 6.5% yielder over 2.4 years), but it
  *penalises* the wheel on `total_return` and return-on-collateral, which are the
  **absolute gates that actually produce a demotion**. On a 5–7% yielder the
  missing dividends alone can push annualized return under the 4% risk-free
  floor. So on the income names the demote flag is biased **toward** demoting
  while the headline comparison leans the other way. Do not judge a dividend
  payer on this engine yet.
- **Early assignment is not modeled**, also optimistic, and concentrated on the
  same dividend payers.
