# Plan: FC-060 Layer 1 — the chain lake (GCS-backed, write-through `ChainStore`)

**FC entry:** `docs/FUTURE_CONSIDERATIONS.md` FC-060 (Layer 1 of four; Layers 2–4 get their own plans)
**Plan file:** `docs/plans/fc-060-chain-lake.md`
**Scope:** shared (backtest engine only — no live trading path is touched)
**Status:** Done (code) — merged 2026-08-28 (PR #99, squash `63e04d5`); **rollout pending operator** (bucket, `objectAdmin`, Job `--image` + env, seed). Review history: `7744944` → fixes `eab2fb7` → confirmation REGRESSION-FOUND → fixes `4ab0089` → re-confirmation CONFIRMED-CLEAN. **Design changed by review (2026-08-28) — this text is superseded where it conflicts:** (1) the corrupt-path lake delete is REMOVED (a reader-side `MemoryError`/version skew would have deleted valid, possibly non-refetchable objects; `put` overwrites on rebuild anyway) — `ChainLake` has no `delete`; (2) uploads are **coverage-monotone**, not overwrite: an upload happens only if the new provenance ⊇ the existing object's (dte ≥, strike window ⊇, same model), guarded by `if_generation_match`; otherwise `chain_lake_overwrite_skipped`; merge-on-put is the Layer-2 follow-up; (3) a **circuit breaker** (credential failure remembered; 5 consecutive errors → disabled) + explicit per-call timeouts, so a lake outage cannot exceed the Job's 3h timeout; (4) a first-use bucket probe using an **object-scoped** RPC (`list_blobs(max_results=1)`) — `Bucket.exists()` needs `storage.buckets.get`, which `roles/storage.objectAdmin` lacks; (5) `exists()`-before-download dropped — `NotFound` is the miss; (6) counters `lake_rejected`/`lake_skipped` + run-level `chain_lake_run_summary`/`chain_lake_degraded` in `screen.py`; (7) `CHAIN_LAKE_BUCKET`/`PREFIX` pinned unset in `tests/conftest.py`; (8) the Job is SHA-pinned — Rollout step 4 must pass `--image`.
**Size:** S (one storage adapter + wiring + a seed tool); backtest-only → two adversarial reviews still apply (it writes a canonical data asset)
**Author:** Fable (plan), for Opus (build)
**Last updated:** 2026-08-28

## Context

`ChainStore` (`src/backtesting/data/chain_store.py`) is a local parquet cache keyed
`(underlying, as_of)` with request-coverage provenance and atomic writes. It is correct and it
is **thrown away every month**: the `backtest-screen` Cloud Run Job runs on an ephemeral
filesystem, so every monthly screen is cold (~5.5 min/symbol, 1h47m total —
`docs/BACKTEST_ENGINE.md` §Track D). Alpaca's option history starts 2024-02-01; a chain for a
settled session never changes. Every month not persisted is history we may not be able to
re-fetch later (vendor retention, tier, rate limits). FC-060's own text: "start persisting the
chain lake immediately — it is the only component whose value is lost by waiting."

Local dev cache today: ~5,400 files / 137 MB for ~2 years × 14 symbols → GCS cost ≈ $0.003/month.

## Design decisions (made)

- **Write-through, read-through local cache in front of a GCS "lake".** `ChainStore` keeps its
  local parquet layout and all of its `get`/`put`/`_covers` logic unchanged. A new optional
  collaborator `ChainLake` (same module or `chain_lake.py`) mirrors files:
  - `get()`: local hit → as today. Local miss and lake configured → `lake.download(underlying,
    as_of, local_path)`; if it lands, proceed with the existing read path (coverage check,
    narrowing, corrupt-file handling all apply to the downloaded file exactly as to a local one).
  - `put()`: existing atomic local write, then `lake.upload(local_path, underlying, as_of)`.
    Upload **after** `os.replace` so a torn local file is never uploaded.
  - Corrupt-file path in `get()` (`chain_cache_corrupt`): also `lake.delete(...)` so a bad object
    cannot be re-downloaded forever; log it.
- **Object layout mirrors the local one:** `gs://<bucket>/<prefix>/<UNDERLYING>/<YYYY-MM-DD>.parquet`.
  Default prefix `chains/v1`. The parquet schema (`_COLUMNS` + provenance columns) *is* the
  format contract; bump the prefix if it ever changes incompatibly.
- **Overwrite semantics = today's `put`**: a wider-coverage rebuild overwrites the object (same
  as it overwrites the local file). Objects are never deleted except on the corrupt path. No
  object versioning required; enable bucket versioning anyway (operator step) as cheap insurance.
- **Lake failures never fail a backtest.** Every lake call is wrapped: on any exception log
  `logger.warning(..., event_type="chain_lake_error", op=…, symbol=…, as_of=…)` and continue
  local-only. A GCS hiccup must not turn a 2-hour screen into a failed execution. Count
  `lake_hits`, `lake_misses`, `lake_puts`, `lake_errors` on the store and log them once at the
  end of `evaluate_symbol` (`chain_lake_summary`) so the first monthly run is measurable.
- **Configuration:** env `CHAIN_LAKE_BUCKET` (bucket name; empty/unset → no lake, behavior
  byte-identical to today) and optional `CHAIN_LAKE_PREFIX` (default `chains/v1`). Read in
  `src/backtesting/evaluate.py` where `ChainStore()` is constructed (`:71-73`) via a small
  factory `ChainStore.from_env()`. Not a `settings.yaml` key — it is deployment wiring, not
  strategy config, and the Job is configured by env (`docs/BACKTEST_ENGINE.md`).
- **GCS client:** `google.cloud.storage` (already a dependency; pattern in
  `src/data/opportunity_store.py`). Lazy-constructed on first use so unit tests and local runs
  without credentials never touch it. Use `blob.download_to_filename(tmp)` + `os.replace` for
  the same atomicity the local write has; `blob.upload_from_filename(path, if_generation_match=None)`.
  Do **not** use gcsfs/FUSE (FC-060 open question: per-object overhead and no atomic rename).
- **Seed tool:** `tools/diagnostics/chain_lake_seed.py` — uploads an existing local cache dir
  to the lake (skip objects that already exist unless `--force`), prints counts. Header comment
  per the diagnostics convention. This is how the 2 years of local history becomes the lake's
  day-one content instead of waiting for monthly runs to refetch it.
- **Today's session stays excluded** (`chain_builder._is_cacheable`) — unchanged; the lake only
  ever holds settled sessions.

## Files to touch

- `src/backtesting/data/chain_store.py`: `ChainLake` class (`download`, `upload`, `delete`,
  `exists`), `ChainStore.__init__(cache_dir, lake=None)`, `from_env()`, the two hook points in
  `get`/`put`, the counters + `summary()`.
- `src/backtesting/evaluate.py:71-73`: `ChainStore.from_env()`; log `chain_lake_summary` after
  the symbol's replay.
- `tools/diagnostics/chain_lake_seed.py`: new.
- `docs/BACKTEST_ENGINE.md` §Track D operating notes: the lake, the env var, the seed step,
  and the expected runtime change once warm.
- `docs/CLAUDE.md` §Data Analysis Policy: one bullet listing `gs://<bucket>` as the chain lake.

## Behavior contract

- `CHAIN_LAKE_BUCKET` unset: no GCS client is ever constructed; results, files, logs identical
  to today (pin with existing chain-store tests unchanged).
- Set: a symbol-day present in the lake but not locally is read from the lake and thereafter
  from local; a newly built chain lands in both; a lake outage degrades to local-only with a
  warning and the run completes.
- The replayed chain is byte-identical whether it came from local, lake, or provider —
  the existing `_covers` + narrowing guarantee is preserved because the lake only moves files.
- Must NOT change: `_covers` semantics, provenance columns, `_is_cacheable`, the
  `ChainBuilder` interface, anything under `src/strategy` or `deploy/`.

## Tests (`tests/test_chain_store_lake.py`, new; use a fake lake — an in-memory dict keyed by
object name — never the real client)

1. No env → `from_env()` returns a store with `lake is None`; `get` on a miss makes no lake
   call (the fake records calls).
2. Local miss + lake hit → file downloaded to the local path, `get` returns the snapshot,
   `lake_hits == 1`; second `get` is a local hit (`lake_hits` unchanged).
3. Local miss + lake miss → provider path (returns `None` from `get`), `lake_misses == 1`.
4. `put` → local file exists AND fake lake has the object with identical bytes, `lake_puts == 1`.
5. Fake lake raising on download/upload → `get`/`put` behave local-only, `lake_errors`
   incremented, warning event emitted, no exception propagates.
6. Corrupt local file with a lake configured → local unlinked, `lake.delete` called, `None`.
7. Coverage narrowing still applies to a lake-sourced file (download a wide file, request a
   narrower window → narrowed result; request a wider one → miss).
8. Seed tool: given a temp cache dir with 3 files and a fake lake holding 1 of them → uploads
   2, skips 1, reports counts; `--force` uploads 3.
9. `from_env()` with `CHAIN_LAKE_BUCKET` set constructs a lake with the bucket/prefix but does
   **not** construct a `storage.Client` until first use (monkeypatch the client factory to
   raise; construction must succeed).

Mutation checks: remove the upload call → test 4 fails; swallow-without-counting on error →
test 5 fails; drop the post-download coverage check → test 7 fails.

## Rollout (ordered)

1. ⚙ Operator: `gcloud storage buckets create gs://options-wheel-chain-lake --project gen-lang-client-0607444019 --location us-central1 --uniform-bucket-level-access`;
   `gcloud storage buckets update gs://options-wheel-chain-lake --versioning`; grant the Job's
   service account (`gcloud run jobs describe backtest-screen --format='value(spec.template.spec.template.spec.serviceAccountName)'`,
   likely `799970961417-compute@…`) `roles/storage.objectAdmin` on the bucket; grant the
   `claude-operator` SA the same so the seed can run from here.
2. Merge (no live-path change for the services; the JOB does not auto-deploy — see step 4).
3. Seed: `python tools/diagnostics/chain_lake_seed.py --cache-dir cache/backtest/chains --bucket options-wheel-chain-lake` from the dev machine. Record counts in §Execution.
4. **The Job is SHA-pinned** (`…:4e810c45…`, FC-059) and `cloudbuild.yaml` has no `jobs update` step, so an env-only update would run old code (review finding). Run: `gcloud run jobs update backtest-screen --region us-central1 --image us-central1-docker.pkg.dev/gen-lang-client-0607444019/options-wheel/options-wheel-strategy:<merged full SHA> --update-env-vars CHAIN_LAKE_BUCKET=options-wheel-chain-lake` (Jobs: `--update-env-vars` is additive — verify the three secrets survive with `jobs describe`).
5. Trigger one manual execution (`gcloud run jobs execute backtest-screen`) off-hours; confirm
   `chain_lake_summary` shows hits ≫ puts and the runtime drops from ~1h47m. Record the number.
6. §Execution + FC-060 entry note ("Layer 1 live; Layer 2 next").

## Open questions

- **Non-blocking:** should the local dev cache dir also be the lake mirror path for every
  developer (i.e. `from_env()` used by `main.py --command backtest` too)? Yes — same factory,
  same env var; a dev without the env var is unaffected.
- **Non-blocking:** lifecycle rule to delete `.tmp` stragglers older than 1 day (a crashed
  upload leaves none — uploads go from a completed local file — so probably unnecessary).

## Execution

- **PR:** https://github.com/memon1987/agentic_options_wheel/pull/99
- **Commit:** `63e04d5` (squash; branch `7744944` build, `eab2fb7` review fixes L1–L10, `4ab0089` confirmation fixes M1–M5)
- **Date:** 2026-08-28 (code). Rollout: step 1 bucket + `objectAdmin` DONE (operator, 2026-08-28); step 3 seed DONE 2026-08-28 17:39 ET — `scanned=5424 uploaded=5424 skipped_identical=0 skipped_differs=0 unparseable=0 failed=0`, 14 symbol prefixes under `chains/v1/`; step 4 Job update DONE by the operator 2026-08-28 ~20:20 ET (image `5e98ff7`); step 5 first execution `backtest-screen-9qc6x` 2026-08-28 20:23 ET: **per symbol `lake_hits=231 lake_misses=20 lake_puts=20 lake_errors=0 lake_rejected=0`** (231 chain-days from the lake; the 20 sessions newer than the seed were fetched once and persisted), **~50 s/symbol vs ~5.5 min cold** (AAPL 00:28:34Z → MSFT 00:29:23Z → GOOGL 00:30:09Z). **One symbol rebuilt cold on this run — the predicted window-thrash case, self-healed:** AMZN `lake_hits=231 lake_rejected=231 lake_puts=251 lake_errors=0` — every seeded AMZN file was downloaded, failed `_covers` (the Job's window-derived strike anchors were wider than the dev cache's), rebuilt from the provider, and uploaded (accepted as a coverage superset), so the lake now holds the wider files and the next run is warm. Cost: one cold symbol (~5 min) once. **SPY and IWM did NOT self-heal:** its rebuilt files were wider on one strike bound and narrower on the other, so every upload was skipped (`chain_lake_overwrite_skipped` ×212+) — SPY stays cold monthly until **FC-091 (merge-on-put)** lands. Heavier row-count names (QQQ/SPY/IWM) run ~2 min/symbol warm on the 1-vCPU Job vs ~50 s for AAPL-class. **Run complete:** 00:25:52 → 01:26:18 UTC = **60 min 26 s** (was 1h47m cold) with four symbols rebuilt cold on this first run (AMZN healed; SPY/IWM/PFE did not — FC-091). Run summary: `lake_hits=3234 lake_misses=280 lake_puts=511 lake_errors=0 lake_rejected=924 lake_skipped=693 lake_disabled=False`. `backtest_runs`: run `5348db6a182d4440`, 14 rows, `run_kind=full`, window 2025-08-29→2026-08-28, verdicts 7 marginal / 5 insufficient / 2 unfit / 0 errors — identical distribution to the prior screen. Expected next run: 11 warm symbols at ~1–2 min each; the three thrashers stay cold until FC-091.
- **Notes:** Reviews (data-lake + backtest-fidelity personas) both REQUEST_CHANGES; both verified the lake is invisible to results (byte-identical backtest output with and without it). Design changes forced by review are listed in the Status header: no lake delete; coverage-monotone uploads with `if_generation_match`; circuit breaker + timeouts; object-scoped bucket probe (`list_blobs`, because `Bucket.exists()` needs `storage.buckets.get` which `objectAdmin` lacks — the first confirmation pass caught this as a would-be production outage); `NotFound`-as-miss; counters `lake_rejected`/`lake_skipped`/`lake_skipped_unreadable_remote` + run-level summary/degraded event; conftest pin; seed tool md5 compare + `--force` + rc 2 on an unusable lake; fc034/fc036 diagnostics routed through one per-process lake. Suite 1429 (80 lake tests). **Known Layer-2 items:** merge-on-put (union of contracts) so window thrash stops re-fetching; provenance in object metadata to avoid the probe download; `lake_rejected` will tell us how much the window thrashes on the first warm run. **Rollout:** step 1 bucket + IAM (operator), step 3 seed from the dev cache (5,424 files), step 4 `jobs update --image <63e04d5 image> --update-env-vars CHAIN_LAKE_BUCKET=…`, step 5 manual execution off-hours and record `chain_lake_run_summary`.
