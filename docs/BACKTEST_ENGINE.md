# The backtest engine — what it measures, and what not to trust

**Status:** complete as a **measurement tool**, and **live in production** — a monthly Cloud
Run Job writes to `options_wheel.backtest_runs`. Not wired to any automated *action*:
`demote` is a column, not a trigger.
**Last updated:** 2026-08-01 (FC-068 — the replay now drives the production pipeline)

Programmatic demotion is deliberately **out of scope** — a later motion, once the engine
has been generating real data for a while. Nothing in this system changes the trading
universe today. `demote` is a column, not a trigger.

---

## What it is

It replays the **live strategy code** over historical Alpaca data. It does not
reimplement the strategy — since **FC-068** the replayed objects are exactly the ones
`/scan` and `/run` use:

```
WheelEngine.reconcile_positions()          # pre-trade housekeeping, as /run does
OptionsScanner.scan_for_put_opportunities()
  + .scan_for_call_opportunities()         # candidate generation  (= /scan)
ExecutionEngine.filter_* -> rank_opportunities -> select_batch -> execute_batch
PutSeller.execute_put_sale / CallSeller.execute_call_sale     (= /run)
WheelEngine.run_rolling_cycle()            # Fridays, as the /roll scheduler does
```

driven by a frozen clock against a `BacktestAlpacaClient` adapter. That is the whole
design premise: a reimplementation would drift from production, and a drift you cannot
see is worse than no backtest.

**Before FC-068 the premise was broken in exactly that way.** The replay called
`WheelEngine.run_strategy_cycle()` — a path production abandoned on 2025-10-03, three days
before the live account's first fill. Every backtest run before 2026-08-01 therefore
measured a strategy with a drawdown pause, gap filtering (stages 2 and 4), wheel-state
phase gating on a state layer that has never been populated, per-cycle position caps, and
**single-candidate selection** (first suitable contract per symbol) that production does
not have — and *without* production's top-3-per-symbol → attractiveness-ranked → two-pool
batch selection and its committed-share ledger. Rows written by either engine are
distinguished by `engine_version` (see "things that will mislead you", item 5).

### What changed in the measurement, and why

- **Put leg** — the engine emitted ≤1 put candidate per symbol per cycle behind gap +
  wheel-state + per-cycle caps. The scanner emits top-3 per suitable symbol; `select_batch`
  takes puts by ROI, one per underlying, until buying power runs out, with **no global
  position cap** (production's actual behaviour). Expect more concurrent short puts and
  faster capital deployment; block attribution moves from "gap filter / wheel state" to
  "insufficient buying power / sizing".
- **Call leg** — the drawdown pause is gone (FC-065 OQ-3: it is not ported to the live
  path), so a symbol 5–15% underwater whose chain still clears floor + delta + premium now
  writes calls in replays, as production does. Selection moves from `suitable[0]` to
  ranked-by-`attractiveness_score`, and the FC-038 committed-share ledger applies, so a
  covered position cannot be double-covered. Net direction is symbol-dependent — which is
  why verdicts must be **re-run, not extrapolated**.
- **Gap filter** — stages 2 and 4 no longer run anywhere. Symbols the gap filter excluded
  in elevated-vol regimes (AMD from 2025-01-13 in the FC-002 study) now trade in replays,
  as they always did in production.
- **Assignment basis** — the backtest broker books an assigned lot at `strike − put
  premium`, matching Alpaca's `avg_entry_price`. It used to book it at `strike`, leaving
  the simulated covered-call floor one premium **above** production's. On a $1 strike grid
  (IWM) that was worth a full strike rung.

```bash
python main.py --command backtest --symbol NVDA --start 2025-11-01 --end 2025-12-01
python main.py --command screen                    # whole universe -> BigQuery
python main.py --command screen --no-persist       # analysis only, writes nothing
```

## What it is good for

- **Comparing symbols against each other** under one configuration.
- **Attributing why a symbol does or doesn't trade** — which stage blocked it, on how many
  days. Since FC-057 this includes stage 1, which was previously silent.
- **Answering A/B questions about thresholds** — this is what it did for FC-002, FC-034
  and FC-036, in each case producing a verdict that contradicted the prevailing assumption.

## What it is NOT good for

- **Absolute return levels.** Every known bias points the same way (below). Read a return
  as a floor.
- **The call leg specifically.** See the fidelity table.
- **Anything on a symbol it never traded.** A `0% days in position` row tells you about
  the *filters*, not the symbol.

---

## Fidelity, measured — the two legs are not equal

> ⚠️ **STALE PENDING RE-MEASUREMENT (FC-068, 2026-08-01).** Every number in this section
> was produced by `parity_check.py` mirroring the *old* selection model (`suitable[0]`,
> matching the sellers FC-068 deleted). The mirror has been rewritten per leg — calls by
> `attractiveness_score`, puts by ROI, both over the scanner's top 3 — but **neither leg
> has been re-run yet**. The *strike-reproduction* figures are the ones directly at risk;
> the premium ratios and the 100% delta-band result depend on the chain model rather than
> the selection rule and should move little. Do not quote 81% / 55.2% / 0.676 as current.
>
> **FC-078 (2026-08-04) folds into the same re-baseline:** the replay now runs
> `run_rolling_cycle()` *every* trading day instead of Fridays only, mirroring the revived
> daily roller, and that roller can now actually execute — so replays after FC-078 can
> close and re-sell short calls mid-window where every earlier replay could not. Roll
> frequency and credit capture are new inputs to the fidelity numbers, not just the
> selection rule.

| | decisions | strike reproduction | premium on **identical** contracts | delta band |
|---|---:|---:|---:|---:|
| **put leg** (stale) | 204 | **81%** | ~0.93 of live | 100% |
| **call leg** (stale) | 80 | **55.2%** | **0.676 of live** | 100% |

The call leg's pricing error is roughly **5× the put leg's**, and its cause is unknown —
DTE mix was the obvious explanation and was **tested and disconfirmed** (FC-056). The call
leg was unmeasurable until FC-048, because before that the engine could not produce a
covered call at all.

**Delta-band accuracy is 100% on both legs.** The engine always selects a correct-*risk*
contract. It is the price — and on calls the strike — that drift.

### Every known bias points the same direction

| bias | magnitude | direction |
|---|---|---|
| put-leg premium | ~7% low (identical contracts) | conservative |
| call-leg premium | ~32% low (identical contracts) | conservative |
| modeled bid/ask spread | 2.46× wider than real, RTH-measured | conservative |
| dividends | modeled both legs since FC-042 C1 | ~neutral |
| ex-div early assignment | **never fired on real data** | optimistic |

**Reported returns are a floor, not a forecast.** For a screening tool this is the correct
failure mode: it cannot flatter a symbol into looking tradeable.

---

## Things that will mislead you if you don't know them

1. **A symbol with 0% days in position is a filter result, not a verdict on the symbol.**
   As of this writing **8 of 14 configured symbols cannot meaningfully trade**: SPY, QQQ
   and AMD are above the `$400 max_stock_price` ceiling (FC-055); F, PFE, KMI and VZ
   cannot clear the `$0.50 min_put_premium` floor (FC-034); MSFT oscillates on the
   ceiling. The effective universe is six: AAPL, AMZN, GOOGL, IWM, NVDA, UNH.
2. **"Completed cycles" counts put-expire-worthless turns.** A number like "44 completed
   cycles" can contain exactly one full wheel. Look for `called_away`.
3. **Ex-dividend early assignment has never executed on real data.** It needs a dividend
   payer holding an ITM short call, and the payers are precisely the symbols that cannot
   open a position. Validated by unit tests only.
4. **The engine refuses split-spanning windows** (`UnadjustedCorporateAction`) by design —
   raw bars are correct for point-in-time chain work but cannot span a split. Pick a
   window that avoids the split date; the error message names it.
5. **Three non-comparability boundaries in `backtest_runs`, and only two are machine-queryable.**
   - Rows before **2026-07-29** describe a **put-only** engine (FC-048 — every backtest
     this project ever ran before it misrouted covered calls to the put seller). FC-048
     did not bump `engine_version`, so this boundary is **timestamp-only**.
   - Rows with `engine_version = 'fc-032-phase-5'` describe the **dead engine path**;
     `engine_version = 'fc-068-prod-pipeline'` describes the production pipeline
     (FC-068). Query the version, not the date.
   - `engine_version = 'fc-069-scanner-rewire'` (FC-069 item 12, **2026-08-04**) changes
     the replayed scanner *and* the rejection vocabulary. The put-side existing-position
     check no longer substring-matches OCC symbols, so a symbol the replay used to skip
     on a spelling coincidence (`'F' ⊂ 'PFE…'`, `'F' ⊂ 'MSFT…'`) is now scanned. And
     `blocked_days_by_reason` gained an "already holds this underlying (scan, put)"
     bucket which is deliberately **excluded from `binding_constraint` selection** — it
     means the wheel was deployed, not blocked. So pre/post rows differ in
     `blocked_days_by_reason` *and* in `binding_constraint` semantics even where the
     verdict is unchanged: a pre-bump row's `binding_constraint` was chosen from a
     vocabulary that did not contain this bucket, and a post-bump `NULL` can now mean
     "the only thing that stopped it was already holding it."

   Do not compare across any of the three boundaries. Old rows are never mutated —
   provenance is `engine_version` + `timestamp` + `config_hash`.
6. **There is no gap filter** (FC-049, FC-068, FC-069). Production never ran the stage-2
   filter; FC-068 removed the backtest's only caller with the engine path; **FC-069 item 5
   then deleted `GapDetector` and all twelve `gap_risk_controls` knobs outright** (the code
   lives at pre-sweep `main` SHA `afb6698`). Gap risk is absent by decision. There is no
   stage-2 or stage-4 block rate any more. Note that FC-069 also dropped
   `gap_lookback_days` / `max_gap_frequency` / `execution_gap_threshold` from
   `config_hash`, which is a **second non-comparability boundary** on `backtest_runs`
   alongside the `engine_version` one above: hashes computed before and after 2026-08-04
   differ even when every surviving parameter is identical.
7. **~~The put-side "already have a position on this symbol" skip is silent.~~ CLOSED by
   FC-069 item 12 (2026-08-04).** `_has_existing_position` now emits
   `put_scan_skipped_existing_position` (`reason`: `stock_position` / `option_position`)
   on the live path, so the replay emits it too and the tally counts it as "already holds
   this underlying (scan, put)" — the old stage-6 bucket's replacement. The same change
   replaced the substring match (`symbol in position['symbol']`, which over-blocked: a
   held `PFE…` contract suppressed every F put) with `parse_option_symbol(...)
   ['underlying'] == symbol`, so replays from 2026-08-04 forward reproduce the *fixed*
   check. Two residual limits: the API-error limb still fails closed under its own
   `position_check_failed` event, which is deliberately unmapped (an outage is not a
   holding); and the skip remains positions-based, so a submitted-but-unfilled put is
   invisible to it (FC-009 territory).
8. **The drawdown pause does not exist.** It was never on the live path, and FC-065 OQ-3
   decided it never will be. A replay showing a call written on an underwater position is
   reproducing production, not missing a guard — the cost-basis floor is the guard.
9. **Monitor-cycle churn is unmodeled.** Early profit-taking closed **52%** of real call
   positions before expiry; the replay holds every contract to expiry or assignment. This
   was true before FC-068 and is still true.
10. **Scan time == execution time.** Production scans and executes ~15 minutes apart, so
    live fills against fresher quotes than it scanned on; the replay uses one snapshot for
    both. Unchanged by FC-068, stated here for the first time.

---

## Re-running the fidelity checks

None of the numbers above are asserted; each has a re-runnable derivation.

```bash
python tools/diagnostics/parity_check.py                     # put leg
python tools/diagnostics/parity_check.py --side call         # call leg
python tools/diagnostics/spread_model_check.py --require-rth # spread, intraday only
python tools/backtesting/coverage_report.py --out coverage.json
```

`--require-rth` exists because an after-hours spread sample makes the model look better
than it is; it refuses to emit a conclusion when the market is closed.

---

## Track D — DONE (2026-07-30). The screen is live.

The engine runs monthly as a **Cloud Run Job**. `/backtest/screen` remains disabled
(503) and should stay that way — a full screen takes **1h47m**, so no synchronous HTTP
request can serve it.

### What is deployed

| | |
|---|---|
| Job | `backtest-screen` (us-central1) |
| Image | `us-central1-docker.pkg.dev/<PROJECT>/options-wheel/options-wheel-strategy:<SHA>` — **Artifact Registry**, SHA-pinned |
| Resources | 1 vCPU, 1 GiB, `--task-timeout 10800s`, `--max-retries 0` |
| Credentials | `--set-secrets` → `alpaca-api-key`, `alpaca-secret-key`, `finnhub-api-key` |
| Schedule | `monthly-performance-review`, **ENABLED**, `0 6 1 * *` UTC (= 02:00 ET) |
| Trigger | Scheduler → **OAuth** → `run.googleapis.com/...jobs/backtest-screen:run` |

### Verified end to end, 2026-07-30

- Execution `backtest-screen-s5dp7` **succeeded in 1h47m39s**
- Wrote **14 rows** to `options_wheel.backtest_runs` (`run_kind='full'`, window 2025-07-30 → 2026-07-29)
- **Verdicts identical to a local run** of the same window — same code, two environments,
  same answer on all 14 symbols (6 `marginal`, 6 `insufficient`, 2 `unfit`)
- Scheduler trigger test-fired and confirmed to create an execution (then cancelled)

### Corrections to what this doc previously said

Three things here were wrong before Track D was attempted, and each would have broken the
deploy. Recorded because the same mistakes are easy to repeat:

1. **Registry.** It said `gcr.io/...`. It is **Artifact Registry**. `jobs create` would have
   failed on image pull.
2. **`:latest`.** It said no `latest` tag is published. **One is** — the build tags both the
   SHA and `latest`. SHA-pinning is still correct for a Job (reproducibility), but the claim
   was false.
3. **Timeout.** It said `3600s`. The real run takes **1h47m**, so an hour would have timed
   out. Now `10800s`.

### The chain lake (FC-060 Layer 1)

Cloud Run's filesystem is ephemeral, so the local parquet chain cache is thrown away after
every monthly run and every screen was cold. The **chain lake** is a GCS mirror of that
cache, and `ChainStore` uses it write-through:

| | |
|---|---|
| Bucket | `gs://options-wheel-chain-lake` (operator-provisioned; versioning on) |
| IAM | **`roles/storage.objectAdmin` on the bucket, and nothing bucket-level.** The lake only lists, reads and writes *objects* — including its startup health probe, which lists one object rather than calling `Bucket.exists()` (that is `storage.buckets.get`, which objectAdmin does **not** grant; using it would 403 and disable the lake on the first call of every run). Do not widen IAM to make a health check work. |
| Layout | `<prefix>/<UNDERLYING>/<YYYY-MM-DD>.parquet` — one object per local file, same bytes |
| Env | `CHAIN_LAKE_BUCKET` (unset ⇒ **no lake, no GCS client, behaviour identical to before**), `CHAIN_LAKE_PREFIX` (default `chains/v1`) |
| Read by | `ChainStore.from_env()` — the Job (`screen.py`, which builds **one** lake for the whole run), `main.py --command backtest` via `evaluate_symbol`, and the `fc034` / `fc036` diagnostics. A `ChainStore()` constructed directly anywhere else bypasses the lake by design. |
| Seed | `python tools/diagnostics/chain_lake_seed.py --cache-dir cache/backtest/chains --bucket options-wheel-chain-lake` |

- **A local miss tries the lake before the provider**; a chain built from the provider is
  uploaded after the atomic local write. A miss is a single RPC (`NotFound`), not a probe
  followed by a fetch.
- **The coverage check is unchanged and still governs.** A lake-sourced file goes through
  `_covers` and the narrowing read path exactly as a local file does, so a warm run and a
  cold run return the identical chain. The lake moves files; it never answers questions.
- **Objects are coverage-monotone, not immutable.** The *chain* for a settled session never
  changes, but the *file* records the window it was built under, and that window is
  path-dependent — the bid pass of `evaluate_symbol` rebuilds the same session under a
  different `cost_basis`/`low_anchor` than the mid pass, and another machine gets a third.
  An object is therefore replaced only by a file whose request is a **superset** (same
  model, ≥ DTE reach, ⊇ strike window). A narrowing is refused and logged as
  `chain_lake_overwrite_skipped`; every upload carries an `if_generation_match`
  precondition so concurrent writers cannot clobber each other. Merging two partial windows
  into their union on write is the **Layer 2** follow-up.
- **A model change needs a prefix bump.** The `model` fingerprint is part of the format
  contract, and a different model is never a *wider* answer — such an upload is skipped, so
  without bumping `CHAIN_LAKE_PREFIX` a model change silently stops populating the lake.
- **Nothing ever deletes an object.** An unreadable local file is discarded locally and the
  mirror is left alone: `except Exception` around `read_parquet` fires on a pyarrow version
  skew, a `MemoryError` or an exhausted fd limit as readily as on real corruption, and the
  lake exists precisely because the vendor may not serve these chains again. The only way
  to replace a genuinely bad object is `chain_lake_seed.py --force`, by hand.
- **A lake failure is never fatal, and never unbounded.** Every call is wrapped: the failure
  is logged as `chain_lake_error` and the run continues local-only. Every RPC carries a 30s
  timeout and a 60s retry deadline. Five consecutive failures — or a credentials failure, or
  a bucket that is missing/unreachable on the one-time startup probe — switch the lake off
  for the rest of the run (`chain_lake_disabled` / `chain_lake_unavailable`), so an outage
  costs the run once rather than 5,400 times. The disable is sticky and one-way: every later
  operation refuses without an RPC, direct callers (the seed tool) included.
- **The startup probe lists one object.** A missing bucket surfaces as `NotFound`
  (`bucket_missing`); anything else — 403, DNS, TLS, timeout — is `bucket_unreachable`; zero
  objects is a healthy *empty* lake, which is the day-one state before the seed runs.
- **Measure it from the logs.** Each symbol emits one `chain_lake_summary`; the run emits
  one `chain_lake_run_summary` with the totals, plus a `chain_lake_degraded` **warning** if
  anything errored or the lake was disabled. The failure mode of this feature is silence —
  a lake erroring on every call still produces a green run that took the full cold 1h47m.

| counter | meaning |
|---|---|
| `lake_hits` | object downloaded and used |
| `lake_misses` | object absent |
| `lake_rejected` | downloaded, then failed the coverage check (should be rare; persistent non-zero means the window is thrashing) |
| `lake_puts` | object written |
| `lake_skipped` | upload declined, all reasons — would have narrowed coverage, changed model, or lost a generation race |
| `lake_skipped_unreadable_remote` | **subset of `lake_skipped`**: the existing object's provenance could not be read, so coverage could not be compared. The other skips are the guard working; this one is a poisoned object that only `chain_lake_seed.py --force` will clear |
| `lake_errors` | operation failed; the run continued local-only |

- **Cost is negligible**: ~5,400 files / 137 MB for ~2 years × 14 symbols ≈ $0.003/month.
- **Clock caveat.** What may be cached at all is decided by `chain_builder._is_cacheable`
  and `alpaca_provider._is_settled`, both `date.today()` — the *process's local* date, not
  an exchange calendar. That was previously a local-cache-only hazard; with the lake, a
  machine with a skewed clock or a surprising timezone could publish a partial session to
  storage everyone reads. The Job runs UTC at 02:00 ET, well after the close, so it is
  correct today; treat it as a constraint on where the engine may be run, not as a
  guarantee.

### Operating notes

- **~5.5 min/symbol on a cold run.** Before FC-060 the cache never warmed — Cloud Run's
  filesystem is ephemeral, so every run paid the full ~1h47m. With `CHAIN_LAKE_BUCKET` set
  and the lake seeded, only the days since the previous run are fetched from Alpaca and the
  rest are downloaded; the expected steady state is a small fraction of that. **Record the
  measured runtime of the first warm execution here** — the number above is the cold-run
  figure and should not be quoted as current once the lake is live.
- Roughly 16 of those cold minutes are spent building chains for F, PFE and VZ
  only to discover no put clears the `$0.50` floor: they pass the price band, so the engine
  cannot know until it looks. That is a concrete cost of FC-034 remaining unactioned. The
  lake removes the *fetch* cost of those days, not the decision cost.
- **Schedule is 02:00 ET deliberately.** A ~2h run must not overlap the trading session; the
  previous `0 12 1 * *` (08:00 ET) would have finished ~09:47 ET, on top of the open and
  contending with the live bot for the same Alpaca quota.
- **`--max-retries 0` is deliberate.** The default of 3 would mean a failing screen hammering
  contract discovery three times.
- **The job name is now a misnomer** — `monthly-performance-review` runs a screen. Left as-is
  because renaming means delete-and-recreate, losing history.

### If it fails

Logs work now (FC-059 — Cloud Run **Jobs** set `CLOUD_RUN_JOB`, not `K_SERVICE`, so log
output previously went to a file inside an ephemeral container and vanished):

```bash
gcloud run jobs executions list --job backtest-screen --region us-central1
gcloud logging read 'resource.labels.job_name="backtest-screen"' --limit 50 --freshness=3h
```

A failure writes **zero** rows — persistence is a single batch after the loop — so a partial
run cannot corrupt `backtest_runs`.

### Before the first *persisted* screen

Persisted rows land in `options_wheel.backtest_runs`, which historically fed demotion
recommendations. Given the engine is being adopted as a measurement tool only, the useful
sequence is: run the Job, read the output, and treat the first few months as **data
collection**. The `demote` column is a recommendation for a human, and the biases above
are the reason it needs one.
