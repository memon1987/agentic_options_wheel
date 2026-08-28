# Plan: FC-060 Layer 1 — the chain lake (GCS-backed, write-through `ChainStore`)

**FC entry:** `docs/FUTURE_CONSIDERATIONS.md` FC-060 (Layer 1 of four; Layers 2–4 get their own plans)
**Plan file:** `docs/plans/fc-060-chain-lake.md`
**Scope:** shared (backtest engine only — no live trading path is touched)
**Status:** Executing — PR #99 (`7744944`); two adversarial reviews in flight. Bucket provisioning is an operator step (Rollout step 1).
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

_Filled in after implementation is complete._

- **PR:**
- **Commit:**
- **Date:**
- **Notes:**
