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
- **Objects are coverage-monotone via merge (FC-091).** The *chain* for a settled session
  never changes, but the *file* records the window it was built under, and that window is
  path-dependent — it moves with `cost_basis`/`low_anchor`, so a machine holding a position
  builds the same session under a different window than one that does not, and so does the
  same machine on a later run once its price range has moved. (It does *not* move between
  the mid and bid passes of `evaluate_symbol`: since Layer 2 the data is materialised once
  and replayed, so both passes read the same chains.)
  An object is therefore replaced only by a file whose request is a **superset** (same
  model, ≥ DTE reach, ⊇ strike window). A narrowing is refused and logged as
  `chain_lake_overwrite_skipped`; every upload carries an `if_generation_match`
  precondition so concurrent writers cannot clobber each other.
  Refusing the narrowing is not enough on its own: a window can move so that the rebuild is
  wider on one bound and narrower on the other, in which case *neither* file covers the
  other, every upload is refused and the symbol is re-fetched cold on **every** monthly run
  — which is exactly what the lake's first production run did to SPY, IWM and PFE
  (`rejected=231 skipped=231` apiece). So when a downloaded object fails `_covers`, the
  rebuild's `put` **merges** rather than replaces: the union of contracts keyed by OCC
  symbol (the new build wins on duplicates), stamped with the union of the two **strike**
  windows (`min` strike floor, `max` strike ceiling) at their **shared** DTE reach. The
  merged file is a superset, so the monotone rule accepts it and the day is warm from then
  on (`chain_lake_merged`).
- **Only one axis may widen, and that is what keeps the merge honest.** A build window is a
  rectangle in (DTE reach × strike range), and the union of two rectangles is an **L** — but
  the provenance columns can only describe a rectangle. Taking `max(universe_dte)` as well
  would stamp the merged file with the *bounding box*, claiming a corner that neither fetch
  ever asked for: e.g. old = reach 14 over [90, 110] merged with new = reach 7 over
  [100, 130] would claim "14 days out, strikes 90–130" while holding no 8–14 day contract
  above 110. A later request at reach 14 across the new strikes would then be a cache **hit
  that is silently missing rows** — a wrong backtest, not a slow one. So the reaches must be
  **equal** or the merge is refused (`chain_lake_merge_refused`, reason `dte_mismatch`) and
  the day falls back to the monotone path. This costs nothing on the case the feature exists
  for: `universe_dte` is `max_dte + UNIVERSE_DTE_BUFFER`, neither term path-dependent, so
  the observed SPY/IWM/PFE thrash is a pure strike move at a constant reach (8 on both
  sides). Two other refusals follow the same principle — **never claim coverage the file
  does not contain**:
  - **Gap** (`chain_lake_merge_gap`): the two strike windows do not overlap, so the union
    would span strikes neither file fetched. This is the one case that also uploads
    **nothing** at all.
  - **Overlap conflict** (`chain_lake_merge_refused`, reason `overlap_conflict`): where the
    two windows *do* overlap, both files asked the same question of the same settled
    session and must have found the same contracts. Compared by contract symbol, not by
    count. A disagreement means one fetch was truncated, rate-limited or restated by the
    vendor, and merging would absorb that into a file that is self-consistent, passes every
    coverage check, and is quietly missing rows.

  Merging is also refused, silently, when the models differ, when either side's provenance
  is unknown, or when the two files were built against different closes for the session.
  Row conversion is untouched: a merged file is read back through the same `_covers` +
  narrowing path as any other, and every row the union adds lies outside the new build's own
  window, so a request the unmerged file could answer gets the identical quotes in the
  identical order — **provided the two fetches agree on the overlap, which the merge
  verifies**.
- **Only the downloaded path heals.** A rejected frame is remembered only when it came off
  the *lake*, so healing is a property of the Job, whose filesystem is ephemeral and whose
  every chain-day therefore arrives from GCS. A developer machine with a persistent local
  cache never merges: a locally-rejected file is this machine's own, `put` overwrites it as
  it always has, and the pre-FC-091 semantics are unchanged. The same is true inside the
  scenario runner — its multi-window loops read materialised chains, and any file they
  reject locally is rebuilt, not unioned. If a dev machine's cache and the lake have
  diverged, the lever is `chain_lake_seed.py --force`, by hand.
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
  anything errored, the lake was disabled, or a merge was refused (`lake_merge_gaps` /
  `lake_merge_refused` — a thrashing symbol that failed to heal will be cold again next
  month, which is exactly the kind of thing that goes unnoticed). The failure mode of this
  feature is silence —
  a lake erroring on every call still produces a green run that took the full cold 1h47m.

| counter | meaning |
|---|---|
| `lake_hits` | object downloaded and used |
| `lake_misses` | object absent |
| `lake_rejected` | downloaded, then failed the coverage check — the window is thrashing. Not itself a problem now that a rejection heals: expect `lake_merged` to track it and both to fall to ~0 on the next run |
| `lake_puts` | object written |
| `lake_skipped` | upload declined, all reasons — would have narrowed coverage, changed model, or lost a generation race |
| `lake_skipped_unreadable_remote` | **subset of `lake_skipped`**: the existing object's provenance could not be read, so coverage could not be compared. The other skips are the guard working; this one is a poisoned object that only `chain_lake_seed.py --force` will clear |
| `lake_merged` | a rejected object was unioned with the rebuild, so the file that replaces it is a superset (FC-091) — whether the upload then landed is `lake_puts`/`lake_errors`. This is the heal: a symbol that thrashed should show `lake_merged ≈ lake_rejected` once, then neither again |
| `lake_merge_gaps` | the two strike windows did not overlap, so the merge was refused and **nothing** was uploaded — the union would have claimed strikes neither file fetched. Persistent non-zero on a symbol means the price range is moving further than one window's width between runs. There is no knob to widen the window (it is derived from the session's bars and the run's positions in `Simulator._strike_anchors`, not from `settings.yaml`); the only lever is a deliberate `chain_lake_seed.py --force` from a local build spanning both windows |
| `lake_merge_refused` | a merge the windows allowed but a correctness check refused: `dte_mismatch` (the two DTE reaches differ, so the union is not a rectangle) or `overlap_conflict` (the two fetches disagree about the strikes they both cover). Unlike a gap these fall through to the coverage-monotone path, and each carries its own WARNING naming the reason. Non-zero means a day did **not** heal and will be re-fetched cold next run |
| `lake_errors` | operation failed; the run continued local-only |

- **Cost is negligible**: ~5,400 files / 137 MB for ~2 years × 14 symbols ≈ $0.003/month.
- **Clock caveat.** What may be cached at all is decided by `chain_builder._is_cacheable`
  and `alpaca_provider._is_settled`, both `date.today()` — the *process's local* date, not
  an exchange calendar. That was previously a local-cache-only hazard; with the lake, a
  machine with a skewed clock or a surprising timezone could publish a partial session to
  storage everyone reads. The Job runs UTC at 02:00 ET, well after the close, so it is
  correct today; treat it as a constraint on where the engine may be run, not as a
  guarantee.


### The bars cache (FC-060 Layer 2)

Chains have had a parquet cache since FC-042 Track A; **underlying bars never did**. Every
replay re-fetched them, four network calls per symbol per `evaluate_symbol`
(`Simulator._load_stock_bars` twice, `evaluate._closes` twice), so a "warm" run was never
actually offline — the chains came off disk and then the first bar fetch went to Alpaca.

`BarStore` closes that. Settled daily bars are immutable, same premise as the chain cache.

| | |
|---|---|
| Layout | `cache/backtest/bars/<SYMBOL>.parquet`, one file per symbol |
| Env | `BACKTEST_BARS_CACHE_DIR` (default `cache/backtest/bars`) |
| Wiring | `evaluate_symbol` wraps the provider in `CachedBarProvider` unless one is injected; the sweep runner shares one across the whole sweep. Composition, never a change to `AlpacaDataProvider` — the cache belongs to the run, not the vendor client |
| Not mirrored to the lake | deliberate: one call per symbol per run on the Job, and bars are always re-fetchable. The lake exists because *chains* may not be |

- **Coverage is proven, not inferred.** The file records the *request window* it answers
  (`covered_from`/`covered_to`) exactly as the chain cache records its strike/DTE window,
  and a read is a hit only against that. Inferring coverage from the stored dates cannot
  work: a weekday with no row is indistinguishable between "market holiday" and "we never
  asked", and guessing wrong serves a replay a window with a hole in it.
- **Coverage stays one contiguous interval.** A request disjoint from the stored window
  fetches their **union** rather than appending a second range. Bars are one API call, so
  the extra days are cheaper than interval algebra in the read path.
- **Today is never stored and never claimed.** Same `_is_settled` rule as the chain cache;
  `covered_to` is clamped to the last settled session, so a run started mid-session cannot
  leave a file that freezes a half-formed close as final.
- A corrupt or old-schema file is discarded and refetched, never raised — same policy as
  `ChainStore.get`, and local-only. So is a file whose *contents* will not parse (a
  `bar_date` that is not an ISO date, a volume that will not cast): the alternative is
  wedging every future run on one bad cell.
- **An empty answer never becomes a coverage claim.** Alpaca returns HTTP 200 with an empty
  payload for an unknown symbol, for an unentitled window and for a transient outage —
  identical on the wire. `put` refuses to widen the window at all when handed no bars, so
  the next run asks again. Believing one of those would serve "this symbol has no history"
  for the life of the file.
- **A truncated non-empty answer cannot be detected from inside**, so the file records both
  what was asked (`covered_from`/`covered_to`, which the hit test uses — a request whose
  edge lands on a holiday must still be a hit) and what came back
  (`data_from`/`data_to`). **What you can rely on is the data span.** When the two diverge
  by more than a plausible market closure, the file is suspect.
- **There is no restatement guard.** A settled daily bar is treated as immutable; a vendor
  that later revises one is not noticed.

**Invalidating the bars cache** — the recipe for both of the above, and for anything else
that smells wrong:

```bash
rm cache/backtest/bars/NVDA.parquet     # one symbol
rm -rf cache/backtest/bars              # all of them
# or, without deleting anything:
BACKTEST_BARS_CACHE_DIR=/tmp/throwaway python main.py --command sweep ...
```

`evaluate_symbol(use_cache=False)` bypasses both caches for a single call.

### Scenario sweeps — `--command sweep` (FC-060 Layer 2)

Ask "what would this config change have done?" across the whole effective universe in one
pass, instead of one `--command backtest` per arm.

```bash
python main.py --command sweep \
    --scenarios examples/scenarios_example.yaml \
    --symbols AAPL,AMZN,GOOGL,IWM,NVDA,UNH \
    --start 2025-08-01 --end 2026-07-31 --no-sensitivity \
    --out sweep.md --json-out sweep.json
# optional: --holdout-start 2026-02-01   --starting-cash 100000
#           --persist   -> options_wheel.scenario_sweeps / scenario_runs (FC-060 Layer 3)
```

**Two entry shapes, one code path** (FC-060 Layer 3). `--scenarios <yaml>` is the
operator CLI above. `--spec-env <VAR>` is the `backtest-sweep` Cloud Run Job's entry
point: the JSON spec and the run id arrive as per-execution container env overrides
(`SWEEP_SPEC_JSON`, `SWEEP_RUN_ID`, `SWEEP_SUBMITTED_AT`, `SWEEP_SUBMITTED_VIA`), and
persistence is implied — an execution whose results nobody can read is an execution
nobody should have launched. Both shapes are normalised into the same spec payload
before anything is hashed or replayed, so a sweep run from a YAML file and the same
sweep submitted from the dashboard produce the same `sweep_key` and dedup against each
other.

```bash
# what the dashboard does, by hand
SPEC='{"symbols":["AAPL"],"start":"2025-08-01","end":"2026-07-31","scenarios":[{"name":"tighter","overrides":{"strategy.min_put_premium":0.75}}]}'
gcloud run jobs execute backtest-sweep --region us-central1 \
  --update-env-vars "^@^SWEEP_SPEC_JSON=$SPEC@SWEEP_RUN_ID=manual1"
```

**The `^@^` is not optional.** `gcloud`'s default list delimiter for
`--update-env-vars` is the comma, and a JSON spec is full of commas — without an
alternate delimiter gcloud parses the spec as a dozen malformed `KEY=VALUE`
pairs and the execution starts with no usable spec. The dashboard is unaffected:
it uses the REST `jobs.run` body, where the value is a JSON string and nothing
is delimiter-parsed.

**The Job is auto-deployed by `cloudbuild.yaml`** (`deploy-sweep-job`), so an ad-hoc
sweep always runs current `main`. `backtest-screen` stays SHA-pinned and untouched: a
monthly screen must be reproducible, and an ad-hoc sweep must answer a question about
the code as it is now. Those are opposite requirements, which is why they are two Jobs.
The step runs **last**, behind all three service promotes — it can fail because the
build service account lacks `run.jobs.create`, and a measurement tool must not be able
to strand a production deploy.

**Persistence is recorded honestly.** With `--persist` (or in Job mode) the markdown
report names the dataset, the `run_id` and the `sweep_key`, and the JSON carries
`persisted: true` with the same three. Without it both say so. A `done` row is written
only when the cell rows actually landed; a sweep whose insert failed is `failed`, and
the CLI exits non-zero rather than printing a "Stored as" line that points at nothing.

**`force: true`** in a spec skips the dedup lookup on both sides, for re-running a
question whose answer you no longer trust. It is excluded from `sweep_key`, so a forced
run stays comparable with the run it reproduces.

**The Job's `--task-timeout` is 10800s (3h)**, matching `backtest-screen`'s rationale:
a cold window materialises at ~5.5 min/symbol. A task killed at the timeout receives
SIGTERM, which the CLI turns into a recorded `failed` row rather than a sweep that
sits as `running` for ever.

**How it is affordable: materialise once, replay many.** Data assembly is
config-independent — the chain model fingerprint takes no `settings.yaml` input and the
strike window comes from bars alone — so `Simulator.run()` was split into `materialise()`
(bars, trading days, strike anchors, chains) and `replay()` (the day loop). A sweep pays
the first once per (symbol, window) and runs the second per scenario, in memory.
`run()` is exactly `replay(materialise())` and its output is byte-identical to the
pre-split engine.

**Overrides are restricted to selection-only keys**, and the refusals carry their reason:

| refused | why |
|---|---|
| `strategy.put_target_dte` | changes the chain's DTE reach; cached files store `universe_dte=8`, so a wider arm misses the cache on every day and a narrower one is served contracts it did not ask for |
| `strategy.call_target_dte` **above** the chain reach | does not widen anything — the 9–15 DTE calls are simply absent, so the arm reads as "no call ever qualified" rather than as a test of that reach. **Lowering it is allowed** |
| `risk.profit_taking.*`, the stop-loss switches | `/monitor`-only; the replay's day loop never runs the monitor, so every arm would return an identical row — which reads as "this knob does not matter" |
| `strategy.{put,call}_limit_spread_fraction` | the replay does not honour limit prices — `BacktestAlpacaClient.place_option_order` *records* `limit_price` and fills at `mid − fill_haircut × half-spread` regardless. **Measured**: a `put_limit_spread_fraction: 0.0` arm came back byte-identical to base on all six symbols over a year. Vary `fill_haircut` on the scenario instead |
| `universe.min_open_interest` | the engine has no OI data — `get_options_chain` hardcodes `open_interest: 0` — so any floor ≥ 1 rejects **every** call, and the arm reads as "this threshold kills the call leg" rather than "the engine cannot see the number". (`universe.max_spread_pct` *is* allowed: its input is a documented model with a measured error, not an absent field) |
| `rolling.fallback_strike_attempts` | governs strike rungs the replay never reaches — the adapter fills rung 1 unconditionally, so rung ≥ 3 came up **0 times over an instrumented 37 rolls × 7 arms**. Live in production, inert here |
| `stocks.symbols` | the universe is run *scope*: pass `--symbols`. A candidate symbol is a cold materialisation |
| `alpaca.*`, `strategy_id`, `bigquery_dataset` | not strategy parameters |
| `earnings.enabled` / `rolling.enabled` **when `EARNINGS_ENABLED` / `ROLLER_ENABLED` is exported** | the env var wins over the yaml key (FC-013 DD-7, FC-078 DD-7), so the arm would be silently identical to base and the sweep would report two arms as tied |

Everything else on the list is in `src/backtesting/scenarios/overrides.py`
(`ALLOWED_OVERRIDES`) and is reproduced in every report. The **fill haircut is a scenario
field, not a config key** — `config_hash` hashes the module default, so two arms differing
only in haircut would share a hash.

**One allowed key is direction-limited.** `risk.max_position_size` binds **downward
only** until FC-079: the put sizer hardcodes one contract, so raising the cap cannot buy a
second one and the arm comes back identical to base. Lowering it does bind — it starves a
position out entirely — which is why the key is allowed rather than refused.

**The test is two-part, and the second half is empirical.** A key must be *selection-only*
(it must not change what the chain has to contain) **and** the replay must actually
*honour* it. The three limit-pricing / open-interest rows above pass the first test and
fail the second, which is only visible by running the arm and finding its rows identical to
base. Before adding a key to the allowlist, sweep it once and check that it moves something.

**Guardrails, because a sweep is a multiple-comparisons machine:**

- the per-symbol grid always renders; there is no mode that prints one blended number;
- an `insufficient` cell (no completed cycle in the window) is flagged and excluded from
  every median/min/max — never rendered as a return, never as 0%;
- **without `--holdout-start` the report opens with an IN-SAMPLE ONLY banner**, and the
  JSON carries `"in_sample_only": true`. That is the default path, and it is the dangerous
  one: 60 numbers chosen on the same window they were measured on, over a single vol
  regime (option history starts 2024-02-01), means the best-looking arm is more often the
  luckiest than the best;
- a `--holdout-start` split adds a **sign-agreement** column: does this arm's advantage
  *over base* keep its sign out of sample. **The two windows are independent replays** —
  each starts flat with the full `--starting-cash`, carries no position across the
  boundary, and derives its own strike anchors from its own bars; the fit window ends the
  day *before* `--holdout-start`, so they never overlap. A **short holdout inflates
  `insuf`**, because a cycle needs a put written, held and resolved: read that column
  before the medians;
- a cell whose wheel held a position on under `MIN_DAYS_IN_POSITION` (25%) of decision days
  renders as **`low-act N%`** and is excluded from every median/min/max, exactly like
  `insuf` — an annualised number earned on capital that mostly sat idle multiplies one
  lucky trade by 365/days;
- **`Δ vs base` is computed over the symbols measured in BOTH arms**, with the subset size
  printed (`+2.0% (n=5)`) and blank when that subset is empty. Differencing two medians
  taken over different symbol sets systematically flatters whichever arm traded less,
  which is the wrong direction for a sweep to be wrong in;
- every row, the scenario table and the JSON carry a **`scenario_hash`** (sha256 over the
  sorted effective overrides plus `fill_haircut`). `config_hash` is kept for comparability
  with `backtest_runs` rows but cannot tell two arms apart on its own: 12 of the 19
  allowlisted keys are outside it, and the haircut it hashes is the module default. Where
  it matches base's, the table prints `= base` rather than repeating the hex;
- the report footer carries the engine's known biases plus the one that only matters when
  comparing arms — the call leg is priced at 0.676 of live (FC-056), so an arm that writes
  more calls is marked down for doing so;
- one bad arm records its error on its rows and the sweep continues; the command exits
  non-zero if any cell never produced a verdict;
- **zero data-layer reads during replays is asserted on counters**, not logged. Both
  halves are guarded: network round-trips (counted on a proxy wrapped directly around the
  vendor client, so a cache hit is *not* one) and bar-cache reads. A regression that
  re-fetched — or merely re-read disk — per replay would still produce correct numbers, so
  nothing about the results would reveal it. The report header and the JSON say
  `provider fetches N (0 during replays), bar-cache hits M`; conflating the two described a
  fully-offline sweep as having made six provider calls.

**Results never reach `backtest_runs`.** That table's documented "current demotion
candidates" query takes the latest `run_kind='full'` row, so a persisted full-universe
sweep written into it would displace the production screen with a hypothetical.

**Since FC-060 Layer 3 a sweep CAN be persisted — to its own tables.**
`--persist` (and the `backtest-sweep` Cloud Run Job, which implies it) writes
`options_wheel.scenario_sweeps` + `options_wheel.scenario_runs`, documented in
`docs/bigquery/scenario_runs.md`. Neither table has a `run_kind` column, so the
displacement above is unrepresentable rather than merely discouraged. **Without
`--persist`, `--command sweep` still writes nothing and the report is still the only
record of the run** — the Layer-2 behaviour is unchanged, byte for byte.

That is a statement about *results*, not about I/O. A sweep over a **cold** window does
write: chains land in the local parquet cache and, when `CHAIN_LAKE_BUCKET` is set, are
mirrored to the GCS chain lake exactly as a `--command backtest` or a screen would mirror
them. That is the shared chain store doing its job — the objects are point-in-time vendor
data, identical whichever command fetched them — and it is independent of anything about
the sweep's conclusions. Bars land in the local bars cache the same way. If you want a
sweep to touch neither, unset `CHAIN_LAKE_BUCKET` and point `BACKTEST_BARS_CACHE_DIR`
somewhere disposable.

**Sequential by design (no `--workers`).** The engine is process-safe but thread-UNSAFE:
`ExecutionEngine._failed_symbols` is a module global the day loop clears, and
`RejectionTally` installs itself with a process-global `structlog.configure()`. At the
measured cost below, multiprocessing is not worth the work.

**No `binding_constraint` column, and that is deliberate.** Only the **first** replay in a
process gets a working `RejectionTally`: `setup_logging` sets
`cache_logger_on_first_use=True`, a structlog lazy proxy caches its whole processor chain
on first use, and `structlog.configure()` — which is how the tally installs itself — does
not invalidate that cache. So every strategy logger keeps delivering to replay #1's tally
for the life of the process, and replays 2..N would report an empty
`blocked_days_by_reason`. Measured on `main` at `7087007`: two `evaluate_symbol` calls in
one process give `{'already holds this underlying (scan, put)': 16, 'selection: duplicate
underlying': 4}` then `{}`. **This is pre-existing and also affects the monthly screen**,
which runs 14 symbols in one process — 13 of every 14 `backtest_runs` rows already carry an
empty tally and a NULL `binding_constraint`. Fixing it changes what that table means, so it
needs its own FC; what the sweep will not do is report a column that is NULL by artifact.

#### Measured (dev machine, 2026-08-28, warm caches, no lake)

Ten scenarios (base + 9 selection-only deltas) × the six effective-universe symbols ×
one year, 2025-08-01 → 2026-07-31:

| | `--no-sensitivity` (60 replays) | with sensitivity (120 replays) |
|---|---:|---:|
| materialise, total | 3.66 s (0.50–0.91 s/symbol) | 3.65 s |
| replay, total | 13.42 s (0.224 s/cell) | 26.80 s (0.447 s/cell) |
| **wall clock** | **17.1 s** | **30.5 s** |
| network fetches during replays | 0 | 0 |
| bar-cache reads during replays | 0 | 0 |

The same 60 cells as independent full runs on `main` — warm caches, no bar cache, which is
what a shell loop over `--command backtest` would cost — is **190.2 s (3.17 s/cell)**. So
the sweep is ~11× faster than the thing it replaces, and the gap widens with scenario
count because materialisation is paid once rather than per arm.

The first sweep of a *cold* window costs its materialisation once — 60.9 s wall on the run
that warmed the bar cache and refetched the strike windows a year-long window widened. The
plan's target was under two minutes; the cold case meets it and the warm case is an order
of magnitude inside it.

**Every allowlisted key has been checked against a live replay**, not just read: all 19
move at least one row at an extreme value over AAPL+NVDA × one year.

`rolling.fallback_strike_attempts` was the twentieth, was carried as *unproven* on the
reading that "did not bind" is not "cannot bind", and is now **refused**. A reviewer
settled it by instrumenting the roller: the knob governs the third and later strike rungs,
and rung 1 always fills in a replay — `BacktestAlpacaClient.place_option_order` fills
immediately at the broker's haircut price rather than resting a limit that can go unfilled
— so over 37 rolls × 7 arms, rung ≥ 3 was reached **0 times**. It is live in production,
where a real limit can miss; it is inert here. The general lesson is in the allowlist's
own docstring: *unproven* is a reason to go and measure, not a reason to ship the key.

Single warm pass over one symbol-year, before and after the row-conversion rewrite (D5):

| | `main` | this branch |
|---|---:|---:|
| AAPL, chain assembly | 1.63 s | 0.51 s |
| AAPL, full pass | 3.65 s | 0.90 s |
| NVDA, full pass | 4.20 s | 0.95 s |
| row conversion alone, AAPL symbol-year (46,643 quotes) | 1.361 s | 0.267 s (**5.1×**) |
| row conversion alone, NVDA symbol-year (57,720 quotes) | 1.706 s | 0.303 s (**5.6×**) |

`ChainStore.get` now narrows with a vectorised mask and converts with `itertuples` instead
of `df.iterrows()` plus a per-cell label lookup. Output is identical, pinned by
`tests/test_backtest_data.py::TestRowConversionIsIdenticalToTheLegacyLoop`, which keeps a
verbatim copy of the old converter as its oracle and runs it against real cached files.

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
