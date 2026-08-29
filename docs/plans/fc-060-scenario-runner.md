# Plan: FC-060 Layer 2 — the scenario runner (materialise once, replay many)

**FC entry:** `docs/FUTURE_CONSIDERATIONS.md` FC-060 (Layer 2 of four; Layer 1 = `docs/plans/fc-060-chain-lake.md`, shipped 2026-08-28)
**Plan file:** `docs/plans/fc-060-scenario-runner.md`
**Scope:** shared (backtest engine only — no live trading path is touched)
**Status:** Executing — PR #100 (`5ee2cd5`); two adversarial reviews in flight. **Corrections found in the build (2026-08-28):** D3's allowlist wrongly included `strategy.{put,call}_limit_spread_fraction` and `min_open_interest` — the replay does not honour them (limit prices are recorded then discarded by the fill model; `open_interest` is hardcoded 0 in the adapter) → moved to REJECTED so a sweep cannot claim to test them; `excluded_symbols`/`max_spread_pct`/`min_open_interest` live under `universe.*` (a section `settings.yaml` does not carry — the allowlist, not `_config`'s shape, decides legality); `call_target_dte` is allowed downward and refused above the chain reach; `Materialised` is multi-symbol; `evaluate_symbol` uses a fresh `Simulator` per haircut over one shared `Materialised` because `HistoricalEarningsCalendar` accumulates state. Acceptance: **10 scenarios × 6 symbols × 1 year = 17.1 s wall (30.5 s with sensitivity), 0 provider calls during replays; same 60 cells as independent runs on main = 190 s.** Pre-existing defects surfaced → FC-092.
**Size:** M (a materialise/replay split, a bars cache, a hot-path rewrite, a runner + CLI, a report); backtest-only → two adversarial reviews (it produces numbers the operator will act on)
**Author:** Fable (plan), for Opus (build)
**Last updated:** 2026-08-28

## Context — what the research pass measured (2026-08-28, dev cache, no lake)

| fact | number / location |
|---|---|
| Warm symbol-year replay, one pass | AAPL 1.6 s · NVDA 2.0 s · SPY 9.3 s (rows/day 186 / 230 / 1,156) |
| Where the time goes | `_build_chains` 78–98% — and inside it `ChainStore.get`'s `df.iterrows()` + `_row_to_quote` (`chain_store.py:1035-1049,1128`) ≈ 80% of runtime; `pd.read_parquet` ≈ 9%; the whole scan→select→execute→roll day loop ≈ 0.3 s/pass |
| `evaluate_symbol` today | **two** full `Simulator.run()`s (mid + bid sensitivity), each re-materialising chains from parquet (502 `get`s for 251 days); **four** `get_stock_bars` network calls per symbol (`simulator.py:229` ×2, `evaluate.py:167` ×2) — bars have no cache anywhere; a socket-blocked run dies at the first one |
| Config seam | every harness mutates `config._config[section][key]` then passes `config=`; `restrict_symbols` deep-copies (`simulator.py:98-106`); `config_hash` reflects the mutation |
| Chain-invalidating vs selection-only | the model fingerprint (`chain_builder.py:191-220`) takes NO config input, so no `settings.yaml` key moves it. Two practical exceptions: `put_target_dte ≥ 8` misses every cached file (`universe_dte=8` everywhere; `evaluate.py:77` sets chain reach); `call_target_dte > 7` does **not** widen chains — it silently sees no 9–15-DTE calls |
| Window thrash | strike anchors are window-derived (`simulator.py:269-311`); a window whose min/max close exceeds a stored file's bounds refetches those days (SPY 3 days, 79 SDK calls) |
| Parallelism | process-safe; thread-UNSAFE (`_failed_symbols` module global; `RejectionTally`'s process-global `structlog.configure`) |
| Persistence | `backtest_runs` has no scenario id / config payload — only the 16-hex `config_hash`; `run_kind` is scope, not provenance; the documented "current demotion candidates" query takes `run_kind='full' ORDER BY timestamp DESC LIMIT 1`, so a persisted full-universe sweep would **displace the production screen** |
| Harnesses | `fc002_gap_filter_ab.py` and `fc036_gap_gate_study.py` no longer run on main (FC-069 deleted the gap module + keys); `fc034` runs |
| Sweep estimate | 10 scenarios × 6 effective-universe symbols: ~4–6 min sequential as-is; **~30–40 s** once chains are materialised once per (symbol, window) and replayed in memory |

**The bet holds without parallelism.** Layer 2 therefore ships sequential, and spends its effort on the three things that actually gate a usable sweep: materialise-once, cached bars (true zero-network), and the row-conversion hot path.

## Scope

1. **Materialise/replay split** in `Simulator`: `materialise(symbol, window) → Materialised` (stock bars, trading days, anchors, chains) and `replay(materialised, config, fill_haircut) → SimulationResult`. `Simulator.run()` becomes `replay(materialise())` — behavior byte-identical.
2. **Bars cache** (`BarStore`, local parquet per symbol under `cache/backtest/bars/`): settled daily bars are immutable; `get_stock_bars(symbol, start, end)` is served from the cache when it covers the range, else fetched once and appended. Lake mirroring of bars is **not** in scope (one call per symbol per month on the Job is negligible; bars are refetchable).
3. **Hot-path rewrite** of `ChainStore.get`'s row conversion (`itertuples`/vectorised, no `iterrows`), byte-identical output.
4. **`evaluate_symbol`** materialises once and replays twice (mid + bid); `_score` reuses the materialised bars.
5. **The runner** (`src/backtesting/scenarios/`): `Scenario(name, overrides)`; `run_sweep(...)` materialises each symbol once, replays every scenario in memory, scores, returns a results table; optional fit/holdout split; markdown + JSON report; `main.py --command sweep`.
6. **Guardrails baked in** (FC-060 §"multiple-comparisons machine"): selection-only allowlist for overrides (chain-invalidating keys rejected with an explanation); per-symbol rows always shown; `insufficient` verdicts flagged, never averaged away; optional holdout; the report footer carries the engine's known biases.
7. **No BigQuery persistence** in this layer (Layer 3). The runner never touches `backtest_runs`.

Out of scope: multiprocessing (`--workers`, Layer 2b if ever needed — the measured cost doesn't justify the thread-safety work); lake mirroring of bars; Layer 3 store / Layer 4 UI; reviving fc002/fc036; changing `ENGINE_VERSION`; any `src/strategy` or `deploy/` change.

## Design decisions (made — do not relitigate in the build)

**D1. `Materialised` is a plain dataclass** in `src/backtesting/engine/simulator.py`:
`symbol, start, end, stock_bars: Dict[str, List[StockBar]], days: Sequence[date], anchors: (ceiling, floor), chains: Dict[str, Dict[date, ChainSnapshot]], split_check: …, max_dte: int, built_at, model_fingerprint`. Built by `Simulator.materialise()` which contains exactly today's `run()` prologue (`_load_stock_bars` → `_trading_days` → split guard → `_build_chains`). `Simulator.replay(materialised, *, fill_haircut=None)` contains today's `run()` body from the broker construction onward, using `materialised.*` instead of recomputing. `run()` = `self.replay(self.materialise())`. `replay` **must not** mutate `materialised` (chains are shared across scenarios): the broker/client get the same dict objects — verify `BacktestAlpacaClient` never mutates a `ChainSnapshot` (grep for assignment into chains; if any, deep-copy at that site only).

**D2. Config per scenario = deep copy of the base + dotted overrides applied to `_config`.** `apply_overrides(config, {"strategy.put_delta_range": [0.15, 0.25], "risk.max_position_size": 0.25})` → new `Config` (deep-copied), each key resolved against `_config`; **unknown keys raise** (typo protection — this is the FC-069 "key without a consumer" rule applied to scenarios). `restrict_symbols` then copies again per `Simulator` as today — acceptable.

**D3. Override allowlist = selection-only keys**, from the research classification: `strategy.{put_delta_range, call_delta_range, min_put_premium, min_call_premium, min_stock_price, max_stock_price, min_avg_volume, excluded_symbols, put_limit_spread_fraction, call_limit_spread_fraction, min_open_interest, max_spread_pct}`, `risk.max_position_size`, `earnings.{enabled, blackout_days}`, `rolling.*` (the roller keys the replay reads). **Rejected with a specific message:** `strategy.put_target_dte` (chain reach — "cached chains store universe_dte=8; a scenario needs a re-materialisation with a wider reach, not supported in Layer 2"), `strategy.call_target_dte > 7` (silently invisible contracts), `risk.profit_taking.*` and stop-loss keys (not read by the replay — say so), anything under `alpaca.*`/`bigquery_dataset`/`strategy_id`. Fill haircut is a scenario field, not a config key (`Scenario(fill_haircut=…)`), because `config_hash` hashes the module default (research surprise 8).

**D4. `BarStore`**: `cache/backtest/bars/<SYMBOL>.parquet` holding every fetched settled daily bar (columns = `StockBar` fields), keyed by date, deduped on write. `AlpacaDataProvider.get_stock_bars` is wrapped by a `CachedBarProvider` (composition, not a change to the provider): if the store covers `[start, end]` (every trading day present — use the store's own date set; a missing weekday inside the range = miss for that range) → serve from disk; else fetch the whole range once, union, write atomically (tmp + `os.replace`, same as `ChainStore`), serve. Today's session excluded (same `_is_settled` rule as chains). Env `BACKTEST_BARS_CACHE_DIR` (default `cache/backtest/bars`). `evaluate_symbol` wires it in; the screen Job gets it for free (ephemeral fs → one fetch per symbol per run, as today).

**D5. Row conversion**: `ChainStore.get` builds `ChainQuote`s from `df.itertuples(index=False)` (or column arrays) instead of `iterrows` + per-cell `Series.__getitem__`. Output must be **identical** — pin with the existing `warm_chain_is_identical_to_cold` plus a new test that compares the old and new conversion on a real cached file (keep the old function as `_row_to_quote_legacy` in the test module only, not in `src`). Expected: warm AAPL pass 1.6 s → ≤ 0.5 s; record the number.

**D6. `evaluate_symbol`**: `m = sim.materialise()`; `mid = sim.replay(m)`; `bid = sim.replay(m, fill_haircut=1.0)` when `run_sensitivity`; `_score` takes `m.stock_bars` (no refetch). Public signature unchanged. `screen.py` unchanged in behavior.

**D7. Runner API** (`src/backtesting/scenarios/runner.py`):
```python
@dataclass(frozen=True)
class Scenario: name: str; overrides: Dict[str, Any]; fill_haircut: Optional[float] = None
@dataclass
class ScenarioResult: scenario: str; symbol: str; window: (start, end); split: "fit"|"holdout"|"all"; verdict; fitness fields (same as build_row's metric columns); config_hash; decision_days; error: Optional[str]
def run_sweep(base_config, scenarios, symbols, start, end, *, holdout_start=None, starting_cash, run_sensitivity=False, chain_store=None, bar_provider=None, log=None) -> SweepResult
```
Order of work: for each symbol → materialise once per window (two windows if `holdout_start`) → for each scenario → `apply_overrides` → `Simulator(config_s, …).replay(m)` → `_score` → row. A scenario that raises records `error` on its row and the sweep continues (one bad override must not lose the other 59 replays). `SweepResult` carries rows, the base `config_hash`, per-scenario `config_hash`, window(s), timing per phase (materialise s, replay s per scenario), and **provider call counts** (must be 0 during replays — asserted, not just logged).

**D8. Report** (`src/backtesting/scenarios/report.py`): markdown with (a) a scenario × symbol grid of `annualized_return` with `verdict` glyphs and `insufficient` cells greyed/flagged; (b) per-scenario rows: median / min / max across symbols, count of `insufficient`, count of demote-flags; (c) fit vs holdout side by side when present, with a **sign-agreement** column (does the scenario's rank order hold out of sample?); (d) the engine's known-bias footer copied from `reporting` (put leg ~7% low, call leg ~32% low — FC-056 — spread model 2.46×), and one line: "comparisons between scenarios that differ in call-leg activity are biased against the call-heavier one until FC-056 is fixed". JSON output = the rows + metadata. Never a single blended number without the per-symbol grid.

**D9. CLI**: `main.py --command sweep --scenarios <yaml> [--symbols A,B] [--start] [--end] [--holdout-start] [--starting-cash] [--no-sensitivity] [--out report.md] [--json-out sweep.json]`. Scenario YAML: `scenarios: [{name, overrides: {dotted: value}, fill_haircut?}]` plus an implicit `base` scenario (no overrides) always run first as the comparator. Exit non-zero if any scenario errored.

**D10. Sequential only.** No threads (unsafe), no processes (cost not justified: 6-symbol × 10-scenario ≈ 30–40 s). Document the two thread hazards in the module docstring so nobody adds a `ThreadPoolExecutor` later.

**D11. Logging**: the sweep runs the replays at WARNING level for the strategy loggers (the day loop's INFO stream is ~1.8 MB/pass; 60 replays = 100+ MB of `logs/options_wheel.log`) and re-enables INFO for the runner's own phase/timing lines. The `RejectionTally` must still count (it hooks structlog processors, not levels — verify).

**D12. Diagnostics**: `fc034_premium_floor_study.py` is left alone (works). fc002/fc036 are dead on main (gap module deleted) — add a one-line note to each file header saying so and pointing at the runner; do not delete (they carry SHA pointers FC-049 relies on).

## Files to touch

- `src/backtesting/engine/simulator.py`: `Materialised`, `materialise()`, `replay()`, `run()` delegating; no behavior change.
- `src/backtesting/data/bar_store.py` (new): `BarStore`, `CachedBarProvider`.
- `src/backtesting/data/chain_store.py`: `get()` row conversion (D5) only.
- `src/backtesting/evaluate.py`: D6; accept `bar_provider`/`chain_store` injection (already has `chain_store`).
- `src/backtesting/scenarios/__init__.py`, `runner.py`, `overrides.py` (allowlist + `apply_overrides`), `report.py` (new).
- `main.py`: `sweep` command.
- `docs/BACKTEST_ENGINE.md`: new §"Scenario sweeps" (how to run, the allowlist, the guardrails, the measured numbers), bars cache note, updated warm timings; correct the FC-060 entry's stale claim that gap keys are in `config_hash` (entry text — bookkeeping by the orchestrator).
- `docs/CLAUDE.md` §Data Analysis Policy: one bullet (sweeps are local, never persisted; `backtest_runs` remains the production screen's table).
- `tools/diagnostics/fc002_gap_filter_ab.py`, `fc036_gap_gate_study.py`: header note (D12).

## Behavior contract

- `Simulator.run()`, `evaluate_symbol`, `run_screen`: outputs byte-identical to `main` for the same inputs (prove: run AAPL 2026-05-01→05-29 and one call-writing window — e.g. NVDA 2025-10-01→2026-01-31 — on `main` and on the branch; JSON + markdown hashes equal). The golden-replay, stage-order, day-loop, anchor and isolation tests pass unchanged.
- After the first run of a window, a second run of the same window makes **zero** provider calls (bars + chains) — assert with a socket-blocking fixture.
- `run_sweep` with N scenarios makes exactly the provider calls of one materialisation per (symbol, window) — **zero during replays** — asserted via the provider call counters.
- Overrides never mutate the base `Config` or another scenario's; unknown/disallowed keys raise before any replay starts.
- Must NOT change: `ENGINE_VERSION`, `backtest_runs` writes, the model fingerprint, `_covers`, `ChainLake`, anything under `src/strategy`/`deploy`.

## Tests

`tests/test_backtest_materialise.py` (new):
1. `run()` ≡ `replay(materialise())`: identical `SimulationResult` (ledger, daily, rejections, roll records) on a fixture window.
2. `replay` twice on one `Materialised` (mid, bid) → each identical to a fresh `run()` at that haircut; `Materialised` unchanged after (deep-compare chains before/after).
3. `evaluate_symbol` materialises once: `_build_chains` called once, `get_stock_bars` called once per symbol-window (spy on the builder/provider).

`tests/test_bar_store.py` (new):
4. Miss → fetch → store; hit → zero provider calls; partial range → one fetch for the whole range; today excluded; atomic write leaves no tmp; corrupt file → refetch, not crash.
5. Socket-blocked second run of a window completes (bars + chains warm) — the "zero API calls" contract.

`tests/test_backtest_data.py` (extend):
6. New row conversion ≡ legacy conversion on a real cached file (all columns, NaN sentinels, empty-chain sentinel row) — and `warm_chain_is_identical_to_cold` still passes.

`tests/test_scenarios.py` (new):
7. `apply_overrides`: dotted key applied to a deep copy; base unchanged; unknown key raises naming the key; disallowed key raises with the reason text (`put_target_dte`, `call_target_dte: 14`, `risk.profit_taking.min_profit_target`).
8. `run_sweep` with 3 scenarios × 2 symbols on a fixture: one materialisation per symbol; provider calls during replays == 0; one row per (scenario, symbol); base scenario first; a raising scenario yields `error` on its rows and the others complete.
9. Holdout: two windows materialised; rows carry `split`; the report's sign-agreement column computed.
10. Report: `insufficient` cells flagged; per-symbol grid present; bias footer present; JSON round-trips.
11. CLI: `main.py --command sweep` on a 2-scenario YAML against the fixture writes `--out`/`--json-out`; non-zero exit on an errored scenario.

Mutation checks: make `replay` mutate a chain → test 2 fails; drop the allowlist → test 7 fails; skip the bars cache → test 5 fails; reintroduce `iterrows` → test 6 still passes (it must — identity) but the timing assertion in §Rollout regresses (record, don't test-gate wall-clock).

## Rollout

1. Merge (two adversarial reviews — a quant-platform/fidelity persona and a data/perf persona — plus confirmation). Deploy is a no-op for the services; the Job picks up the bars cache and the faster `get` on its next run.
2. **Acceptance measurement (the bet):** on the dev machine, `main.py --command sweep` with 10 scenarios (the base + 9 selection-only deltas across delta bands / premium floors / price ceiling / position size / earnings on-off) over the 6-symbol effective universe (AAPL AMZN GOOGL IWM NVDA UNH), one year: record materialise time, total replay time, wall-clock. **Target < 2 min wall-clock sequential.** Record in §Execution and in `docs/BACKTEST_ENGINE.md`.
3. First real use: **FC-055** (`max_stock_price` ceiling — add SPY/QQQ/AMD via `strategy.max_stock_price` + `stocks.symbols` override? — note `stocks.symbols` is run scope, so the sweep takes `--symbols` including SPY/QQQ/AMD and a scenario that lifts the ceiling) and **FC-001/034** (universe). Those become their own plans with the sweep as the instrument.
4. FC-060 entry: Layer 2 shipped; Layer 3 (store: scenario id + config payload + a `run_kind` that cannot displace the production screen) is the next plan.

## Open questions

- **Non-blocking:** should `Scenario` allow `symbols` overrides (a universe scenario)? Not in Layer 2 — `--symbols` is the run scope; a "candidate symbol" is a cold materialisation and belongs to the onboarding story in Layer 3.
- **Non-blocking:** SPY-class row counts (1,156/day) make even the fast path ~3–4 s/pass; if the price-ceiling study needs many SPY/QQQ scenarios, a per-window in-memory chain cache across CLI invocations (pickle of `Materialised`) is the next lever — not now.

## Execution

_Filled in after implementation is complete._

- **PR:**
- **Commit:**
- **Date:**
- **Notes:**
