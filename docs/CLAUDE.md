# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Every claim in this file was re-verified against the tree by grep/read on
2026-08-04** (FC-069 item 14, after S1–S6 landed). The previous version
described an orchestration path that had not run since 2025-10-03 and a risk
class that never validated anything. False architecture claims in this file are
not harmless: FC-069 item 14's own argument is that the fictional wheel-state
layer drew ten months of remediation work partly because these docs called it
real. **Do not add a claim here you
have not verified against the current tree.**

## Project Overview

An algorithmic trading system that runs an options wheel strategy: sell
cash-secured puts, take assignment, sell covered calls on the assigned shares,
get called away, repeat. All execution goes through the Alpaca API. Production
is a **stateless** Flask service on Cloud Run (`deploy/cloud_run_server.py`)
driven entirely by Cloud Scheduler; there is no long-lived process and no
durable strategy state (see §Accepted amnesia).

## Plan-First Development

Plan-First Development rules are defined in the parent `CLAUDE.md` and apply to
this project. Project-specific plan files live in `docs/plans/`.

**Scope tag (required since 2026-08-27, operator rule):** every FC entry and
every plan file carries a `**Scope:**` line — `wheel` | `covered_call` |
`shared` — naming which strategy the work serves (`shared` = machinery both
consume: writers, OCC parsers, ExecutionEngine, config, deploy/CI). Two
services now build from this repo; the tag is what lets a reviewer instantly
know which service's behavior a change can touch, and which neutrality
contract (wheel-neutral vs covered-call-neutral, see the reverse-neutrality
note in `docs/plans/fc-075-phase-2.md`) the PR must prove. Multi-scope work
names the primary and lists the rest. Entries filed before 2026-08-27 are not
retro-tagged; tag them opportunistically when touched.

## Development Setup

Install dependencies:
```bash
pip install -r requirements.txt
```

Set up environment:
```bash
cp .env.example .env
# Edit .env with your Alpaca API credentials
```

## Commands

**CLI** (`main.py` — `--command` accepts exactly `scan`, `status`, `report`,
`backtest`, `screen`, `sweep`):
```bash
python main.py --command scan    # Scan for opportunities (same OptionsScanner as production)
python main.py --command status  # Portfolio status  (PortfolioTracker — CLI only)
python main.py --command report  # Performance report (PortfolioTracker — CLI only)
```

`--command run` **no longer exists** (deleted by FC-068). It drove a separate
engine decision path that placed real orders against the live account and had
diverged from what production does. There is no CLI trade-execution entry point
by design — trading happens through the Cloud Run endpoints below.

**Testing:**
```bash
pytest tests/ -v                 # Run all tests (1262 collected as of 2026-08-04)
pytest tests/test_config.py -v   # Run specific test file
```

**Code Quality:**
```bash
black src/ tests/                # Format code
flake8 src/ tests/               # Check style
mypy src/                        # Type checking
cd dashboard/frontend && npx tsc --noEmit   # REQUIRED if you touched the frontend
```

The `tsc` line is not optional: an FC-069 S1 dashboard change passed pytest and
two reviews, then broke the Cloud Build image because nobody type-checked the
frontend.

## Architecture — the system that actually runs

**Core strategy flow** — this is what production has run since 2025-10-03 (the
date FC-068 established for the engine path's last live use):

```
/scan    OptionsScanner  ──►  GCS opportunity blob  ──►  /run   ExecutionEngine  ──►  PutSeller / CallSeller
         (stage gates)         (run_id + strategy_id)         filter → rank →
                               30-min freshness                select_batch → execute_batch

/monitor  PutSeller / CallSeller.should_close_*_early — DTE-banded profit-taking (closes only)
/roll     WheelEngine.run_rolling_cycle ──► CallRoller ──► RiskManager.validate_roll  (daily 15:30 ET)
```

1. **`/scan`** (`OptionsScanner`, `src/data/options_scanner.py`) finds and ranks
   opportunities for both legs and writes them to a Cloud Storage blob via
   `OpportunityStore`. The blob carries `strategy_id` (FC-075 Seam 1 — a
   consumer refuses a blob written by another strategy profile) and `run_id`
   (FC-065 P4 — stamped onto the envelope *and* onto every opportunity, because
   the two halves of a cycle are separate stateless HTTP requests).
2. **`/run`** (`ExecutionEngine`, `src/strategy/execution_engine.py`) reads the
   most recent blob that is younger than `strategy.opportunity_max_age_minutes`
   (30), then: `filter_duplicate_opportunities` / `filter_failed_opportunities`
   → `rank_opportunities` (type-aware sizing) → `select_batch` (**two pools**,
   FC-038) → `execute_batch` → `PutSeller.execute_put_sale` /
   `CallSeller.execute_call_sale`.
3. **`reconcile_positions`** (`WheelEngine`) runs first inside `/run` as
   pre-trade housekeeping. It diffs Alpaca against a per-request scratch pad and
   emits assignment/expiration telemetry. Its failures are caught and do **not**
   block execution.
4. **`/monitor`** closes positions that hit their DTE-banded profit target. It
   is never gated — a close reduces risk.
5. **`/roll`** evaluates short calls whose underlying rallied through the strike
   and rolls them up-and-out, **credit-only** (FC-078).

`WheelEngine` is **not an orchestrator**. Post-FC-068 its entire surface is
`reconcile_positions` and `run_rolling_cycle`.

**Scheduler cadence** (Cloud Scheduler, `America/New_York`, Mon–Fri —
live-verified 2026-08-04 with `gcloud scheduler jobs list`):

| Job family | Cadence | Endpoint |
|---|---|---|
| `scan-10am` … `scan-3pm` | `:00`, 10:00–15:00 ET | `/scan` |
| `execute-10-15am` … `execute-3-15pm` | `:15`, 10:15–15:15 ET | `/run` |
| `monitor-9-55am` … `monitor-2-55pm` | `:55`, 09:55–14:55 ET | `/monitor` |
| `options-wheel-roll-daily` | 15:30 ET | `/roll` |
| `regression-hourly` | `:45`, 10:45–15:45 ET | `/regression` |
| `activities-ingest-market-hours` / `-off-hours` | every 15 min 09–16 ET / hourly otherwise | `/ingest-activities` |
| `portfolio-history-ingest-daily` / `stock-history-ingest-daily` | 16:30 / 17:00 ET | ingest endpoints |

`options-wheel-roll-friday` still exists but is **PAUSED** — FC-078 replaced it
with the daily job. The scheduler owns all timing; no cadence knob in
`config/settings.yaml` controls it (a `monitoring.check_interval_minutes` key
used to read as if it did, and was deleted in FC-069 S1 for exactly that
reason).

**Key integration points:**
- All market data and trading flow through the `AlpacaClient` wrapper.
- Configuration is centralized in `Config` (`src/utils/config.py`) — YAML plus
  env-var substitution. One process runs **one** strategy profile, selected by
  `STRATEGY_CONFIG` (`config/settings.yaml` = wheel, `config/covered_call.yaml`
  = the covered-call profile, FC-075).
- Every trading endpoint (`/scan`, `/run`, `/monitor`, `/roll`, the ingest
  routes) is wrapped in `@require_account_match`: the service refuses to act
  when the live Alpaca account number does not equal
  `alpaca.expected_account_number` (503). This pins right-code to
  right-credentials.
- Structured logging throughout (structlog) — the events are the audit trail
  and the dashboard's raw material.

## Trading APIs

- **Alpaca API**: primary API for all trade execution, via `AlpacaClient`.
- **Paper Trading**: enabled by default (`alpaca.paper_trading: true`);
  activities are read from `https://paper-api.alpaca.markets/v2`.
- **Options Trading**: cash-secured puts and covered calls.
- **Finnhub**: earnings dates for the FC-013 gate (`EarningsCalendarService`).

## Key Components (Implemented)

- **OptionsScanner** (`src/data/options_scanner.py`): the opportunity finder —
  both legs, all scan-stage gates, decision records.
- **ExecutionEngine** (`src/strategy/execution_engine.py`): filter, rank,
  `select_batch` (two-pool), `execute_batch`, naked-call block.
- **OpportunityStore** (`src/data/opportunity_store.py`): the GCS blob that
  carries a cycle from `/scan` to `/run`.
- **PutSeller** / **CallSeller** (`src/strategy/put_seller.py`,
  `call_seller.py`): order construction and submission, plus the
  `should_close_*_early` profit-taking logic used by `/monitor`.
- **CallRoller** (`src/strategy/call_roller.py`): the daily credit-only
  defensive roller (FC-078).
- **CostBasisResolver** (`src/strategy/cost_basis.py`): resolves the per-share
  cost-basis floor from Alpaca `avg_entry_price`, with a BigQuery
  assignment-history cross-check; fails closed.
- **EarningsCalendarService** (`src/api/earnings_calendar.py`): tri-state
  (`known` / `unknown` / disabled) earnings dates with a two-layer cache.
- **MarketDataManager** (`src/api/market_data.py`): stock filtering and options
  chain analysis (`find_suitable_puts` / `find_suitable_calls`).
- **AlpacaClient** (`src/api/alpaca_client.py`): API wrapper.
- **RiskManager** (`src/risk/risk_manager.py`): **`validate_roll` only** — the
  roller's gate. It has exactly one public method. See §What actually bounds
  risk for why.
- **WheelEngine** (`src/strategy/wheel_engine.py`): `reconcile_positions` +
  `run_rolling_cycle`. Nothing else.
- **WheelStateManager** (`src/strategy/wheel_state_manager.py`): per-request,
  in-memory bookkeeping that `reconcile_positions` diffs against Alpaca. Built
  empty every request, thrown away at the end. Shrunk from 747 to 331 lines by
  FC-069 S6; its GCS persistence never worked (FC-039) and is gone.
- **PortfolioTracker** (`src/data/portfolio_tracker.py`): **CLI-only**
  (`main.py status` / `report`). Not on the deployed path — nothing in
  `deploy/` or `src/` imports it.
- **Config** (`src/utils/config.py`): centralized configuration.
- **ActivitiesIngestor** (`src/data/activities_ingestor.py`): pulls Alpaca
  account activities into BigQuery — the authoritative record of what happened.

## What actually bounds risk

**There is no central validator.** `RiskManager.validate_new_position` and its
five siblings were deleted by FC-069 S1 with **zero production call sites ever
recorded** — the old claim that "risk validation is required before any trade
execution" was false since inception. Enforcement is distributed by design, and
this is the inventory:

**Scan stage** (`OptionsScanner`, `MarketDataManager`)
- Price/volume band: `filter_suitable_stocks` — `$10 ≤ price ≤ $400`,
  ≥ 2M average daily volume.
- Contract admission: delta bands, DTE targets, premium floors
  (`_check_put_criteria_detailed` / `_check_call_criteria_detailed`).
- **Earnings gate (FC-013, live since 2026-08-03)** — the legs diverge
  deliberately: **puts** block a symbol when the next earnings date is 0–2
  calendar days out (`earnings.blackout_days: 2`, symbol-level); **calls** use a
  true **span** test per candidate — reject when
  `expiration_date >= next_earnings_date`, with no numeric knob, because span is
  the risk predicate itself and is DTE-invariant. Both legs **fail closed** on
  `unknown`.
- Existing-position skip: the put leg skips any symbol already holding a stock
  or option position (parse-exact since FC-069 S3 — it used to be an OCC
  substring test that suppressed every F put because `PFE…` contains `F`). The
  skip event carries a `reason` naming which limb fired — `stock_position`,
  `option_position`, or, since **FC-079**, `unparseable_position`: the
  fail-closed limb for a held option symbol with no contract structure. The
  posture is unchanged (it still skips); only the label is new. Sharing one
  bucket made a scan suppressed by garbage position data indistinguishable
  from one that was simply already invested.
- Cost-basis floor, scan side: a call opportunity is not created when the
  resolved basis is unresolved or diverges from the cross-check.

**Selection stage** (`ExecutionEngine.select_batch`, two independent ledgers)
- Calls draw down a per-underlying **available-shares** ledger; puts draw down
  **buying power**. Charging calls phantom cash collateral was the covered-call
  starvation bug (FC-038).
- `duplicate_underlying` drop — one position per underlying across both pools.
- Calls are selected first (they cost no cash, so they cannot displace a put).

**Execution stage** (sellers, `execute_batch`)
- Wrong-seller routing rejection (a call routed to `PutSeller` is refused).
- **Execute-time cost-basis floor** (FC-050 / FC-065): a call strike below the
  share cost basis is refused before order submit, reading the floor off the
  opportunity so scan and execute enforce the same number. The basis source is
  Alpaca `avg_entry_price`; it **fails closed**. This is the strongest control
  in the system.
- Naked-call block (`naked_call_blocked`).

**The full gate-by-gate inventory — every gate, its config, its event, and its
fail posture — lives in `docs/gates.md`. Read it before adding or changing a
gate.**

### The real per-ticker bound

There is **no absolute dollar cap per ticker**. `max_exposure_per_ticker:
40000` was deleted in FC-069 S1: it had no preventive consumer, and the
rationale that once justified it ("one put × `max_stock_price` 400 × 100 = $40k")
is arithmetically wrong — `max_stock_price` bounds one *contract*, not a ticker.
What actually bounds per-ticker exposure:

1. `risk.max_position_size: 0.35` × portfolio value, per position — a
   *proportional* bound that **floats with equity** — ≈$36k at the ~$103k
   portfolio of FC-069's sign-off, ≈$70k at $200k — where the deleted knob was
   absolute.
2. One option position per underlying at a time (the invariant below).
3. `strategy.max_stock_price: 400`, capping per-contract collateral.

The operator's decision (FC-069, 2026-08-01, binding) is that breadth is bounded
by **capital and the ticker universe** — both proportional — and that a fixed
count or a fixed dollar cap must not bind future growth. `max_total_positions`
was deleted for the same reason. Accepted residual, on the record: buying power
is the only preventive breadth limit on puts, and the burst scenario stays
count-ungated.

### The one-position-per-underlying invariant, and its caveat

The invariant is emergent, not a knob (`max_positions_per_stock` was deleted in
FC-069 S1). It comes from three legs, carried verbatim from FC-069 item 3:

> **One option position per underlying** emerges from (i) the scanner's
> put-side skip of any symbol with existing positions, (ii) `select_batch`'s
> `duplicate_underlying` drop (one per batch, both pools), (iii) the calls share
> ledger (no double-covering). The invariant is per-*position*, not
> per-*contract*.
>
> **All three enforcing legs are positions-based and blind to resting unfilled
> orders** — a submitted-but-unfilled put is invisible to the scanner skip, the
> batch dedup (across cycles), and the share ledger alike; the invariant is
> "one *position* per underlying, modulo the open-order window" (FC-009's
> standing territory).

## Strategy Configuration

Read `config/settings.yaml` for the live values; these are the ones that matter,
verified 2026-08-04:

- **DTE**: `put_target_dte: 7`, `call_target_dte: 7` — short-dated, for rapid
  theta decay.
- **Deltas**: puts `put_delta_range: [0.10, 0.20]`; calls `call_delta_range:
  [0.15, 0.25]`. **The two are not the same range** — FC-029 R1 tightened calls
  from `[0.30, 0.70]` after three cycles gave up ~$9k of share upside to
  aggressive call deltas.
- **Premium floors**: `min_put_premium: 0.50`, `min_call_premium: 0.30`. The put
  floor is a real universe constraint, not a formality — several configured
  symbols cannot clear it at all.
- **Sell-to-open limit pricing** (FC-072, both legs, both profiles — the shared
  implementation is `src/strategy/limit_pricing.py`):
  - **The quote is refreshed at execution time.** `/scan` quotes at `:00` and
    `/run` places at `:15`; nothing on the execute path used to re-quote, so
    every limit was priced off a book up to 15 minutes old. **That staleness,
    not the spread, is the dominant variable** in where these orders landed: of
    66 filled calls, 17 filled below the *scan-time* bid and 28 at or above
    scan-time mid. A refresh that fails never fails the order — it falls back
    to the scan-time quote and logs `quote_refresh_failed`; the
    `*_sale_executing` event carries `quote_source` (`live` / `blob`) and
    `quote_age_s` so the two populations can be told apart.
  - **Formula**: `mid + f × (ask − bid)`, `mid` recomputed from whichever book
    was used. `f` is `strategy.call_limit_spread_fraction` (**0.0** — rest at
    mid) and `strategy.put_limit_spread_fraction` (**0.10** — the put leg's
    historical bias toward the ask, unchanged). Both validated `[0.0, 0.5]`;
    `0.5` sits exactly on the ask.
  - **Usable book** = `bid > 0 and ask > 0 and ask >= bid`. A **locked** book
    (`ask == bid`) is usable and prices at the bid — never 5% *through* a
    locked market. A **wide** book is usable with no cap: wide means illiquid,
    not stale, and discounting it further concedes most where the mid is least
    informative. Only **one-sided** and **crossed** books fall back, to the
    historical `premium × 0.95`.
  - **Tick increments are a broker rule, not a preference.** Two regimes, and
    the distinction matters: **always-penny** names (`ALWAYS_PENNY_SYMBOLS` —
    SPY/QQQ/IWM) tick $0.01 at *every* price level, while **penny-program**
    roots tick $0.01 below $3.00 and **$0.05 at and above**. All 14 configured
    roots were verified `ppind=True` on 2026-08-28 and are listed in
    `VERIFIED_PENNY_PROGRAM_ROOTS`; an unrecognised root is priced under the
    penny-program rule and logged once per process as `tick_class_unverified`.
    A genuine **non-penny class ticks $0.05/$0.10** — increments this code
    deliberately **cannot emit**, because no such root is tradeable here and an
    untested third regime is worse than a loud warning. A non-conforming limit
    is rejected when no exchange accepts it, and sell limits snap **up** — with
    one exception: on a book straddling $3.00 the next legal $0.05 tick can
    sit *above the ask* (2.98/3.03 prices a raw 3.005, whose next legal price
    is 3.05), and a sell limit above the ask can never fill, so it rounds
    **down** to the last legal tick inside the book instead — marketable beats
    unfillable.
    **The paper simulator does not enforce increments**, so paper history is
    not evidence that a live account would accept an off-tick limit. Below the
    $0.05 regime the cent rounding is `ROUND_HALF_UP` on the exact decimal, not
    `round()` — banker's rounding over floats put a half-cent mid on the bid or
    the ask by luck.
  - **A one-tick book prices at the bid**, flagged `one_tick_book`, **on both
    legs**. When the spread is exactly one tick — measured at the *bid*, since a
    book straddling $3.00 is five ticks wide, not one — there is no midpoint to
    rest at and "mid rounded half-up" is the ask by another name; resting there
    on a DAY order risks a full cycle of theta to gain a cent. On a 0.15–0.25Δ
    call that is ≈−$30 in missed cycles against ≈+$6 of spread. The put leg
    inherits the rule by Symmetry, at a measured cost of −$1/contract on 5 of
    118 journaled rows — accepted deliberately (see `docs/plans/fc-072.md`).
  - **These are INDICATIVE quotes, not NBBO.** `alpaca_client.OPTION_QUOTE_FEED`
    is `"indicative"` and is passed explicitly on every option quote, because
    the **OPRA agreement is not signed**. Indicative is an *adjusted* BBO: fine
    for paper and for the FC-072 readout, not fine for real money, since a limit
    priced off it can rest away from the true market with nothing in the logs to
    say so. **Signing the OPRA agreement is a precondition before this code
    prices a real-money order.** The feed is echoed back on every quote and
    logged as `quote_feed` so that precondition is auditable rather than
    remembered.
  - **Only the opening writes are tick-correct.** The roller's STO/BTC limits
    and `/monitor`'s `ask × 0.95` buy-to-close still round to the cent and are
    therefore **off-tick above $3.00**. Harmless on paper (the simulator does
    not enforce increments), a rejected order on a live account — its own FC.
  - **Economics, corrected** (rev 1 of the plan got this wrong and the reviews
    caught it): the call leg's old `premium × 0.95` was **not** a 5% donation.
    On this book 5% of mid ≈ half a spread, so the limit sat about **at the
    bid** — marketable, filling at the bid. Realised `Σ(mid − fill)` over the
    journaled call fills was **−$87**. What the change buys is resting at mid
    instead of crossing, ≈ **+$5/write gross**, against an expected **75–80%**
    fill rate (vs ≈90% marketable). Do not repeat the retired "$2.3k" /
    "5% donated" framing. `docs/plans/fc-072.md` holds the two-week readout
    that decides whether the trade was worth it.
  - **The roller is deliberately not routed through this module.** `CallRoller`
    prices its sell-to-open **at the bid** (or `mid − $0.05` on imminence)
    because a credit-only defensive roll must execute in the same session as
    its buy-to-close leg. Opening writes can afford to rest; rolls cannot.
- **Universe**: 14 symbols in `stocks.symbols`. The **effective** universe is
  smaller: the `$400` price ceiling and the premium floors exclude several
  symbols entirely, so a symbol that never trades is a *filter* result, not a
  verdict on the symbol. `docs/BACKTEST_ENGINE.md` carries the current counts —
  do not memorize them, they move with config.
- **Position sizing — read this carefully, it is not what it looks like:**
  - **Puts execute at exactly 1 contract.**
    `PutSeller._calculate_position_size` computes `min(max_position_size ×
    portfolio ÷ collateral, buying_power ÷ collateral, 10)` and then returns
    `contracts = 1`. The computed maximum is a **feasibility gate** (0 → the
    opportunity is dropped as `sizing_failed`), never the executed size; the
    10-contract ceiling therefore cannot bind today.
  - **Calls are sized by shares**: `available_shares // 100` in
    `rank_opportunities`, then re-checked against the per-underlying share
    ledger in `select_batch` (FC-038 two-pool). A call consumes no buying power.
- **Cost-basis protection**: enforced in code at scan and execute time, sourced
  from Alpaca `avg_entry_price`. There is no knob to relax it.

## Risk Management Philosophy

**Puts**: no stop losses — the strategy is designed to take assignment on
quality stocks (`use_put_stop_loss: false`).
- Assignment probability ≈ |delta| (10–20% for the put range)
- Keep the full premium on most positions; take assignment on the rest at
  strikes chosen to be acceptable entry prices

**Calls**: **no stop losses either, since FC-010** (`use_call_stop_loss:
false`). The stop-loss machinery (`_check_call_stop_loss`,
`call_stop_loss_percent`, `stop_loss_multiplier`) is still in the tree but is
**live-dormant behind the off switch**, kept deliberately rather than deleted.
Disabling it was worth an estimated $1,000–$2,500 in avoided future losses
across 5 historical episodes (`docs/investigations/strategy-review-2026-05-07.md`).
Do not describe those knobs as active controls.

Live covered-call management is, in full:
1. **DTE-banded profit-taking** on `/monitor` — targets ramp 0.35 → 0.80 as DTE
   falls 7 → 0 (`risk.profit_taking.dte_bands`), bounded by
   `min_profit_target` / `max_profit_target`.
2. **The cost-basis floor** — never write a call below the share basis.
3. **Hold-uncovered-until-recovery** — when every strike above basis fails the
   chain criteria, the position simply stays uncovered rather than writing a
   guaranteed loss. `uncovered_days` (FC-065 P4) makes that visible instead of
   silent.
4. **The earnings span gate** — no call may expire on or after the next earnings
   date.
5. **The daily credit-only roller** — when the underlying rallies through the
   strike (`rolling.itm_trigger_ratio: 0.98`), roll up and out for a net credit
   on the placed limit prices, within `max_extension_days: 14` of the *old*
   expiry and under a `max_replacement_delta: 0.60` rail.

## Accepted amnesia (process-local state)

Three pieces of live state are per-instance and reset on every cold start. They
are **live controls with a known, accepted weakness — not fiction** — and the
honest posture is to document them, not to pretend they are durable:

- `_closed_today` (`deploy/cloud_run_server.py`) — `/monitor`'s duplicate-close
  dedup. A cold start between two monitor cycles can allow a duplicate close
  order. **FC-009 owns the fix** (check Alpaca for open buy-to-close orders —
  the cold-start-proof option); it is still open.
- `_failed_symbols` (`src/strategy/execution_engine.py`, module-global) —
  suppression of non-retryable failures within a day; clears on cold start.
- `strategy_status` (`deploy/cloud_run_server.py`, served by `/status`) —
  last-run bookkeeping; resets silently.

**Standing rule for anyone adding durable state here** (inherited from FC-039 /
FC-069 item 8): a configured-but-unresolvable persistence target must **fail
loudly at startup**, never silently no-op. A silent `storage_bucket=None` no-op
is exactly how the wheel-state layer stayed fictional for a year while the docs
called it canonical.

## The detective layer — `/regression`

`tools/testing/regression_monitor.py` is **not a dev tool**. It is served at
`POST /regression` and invoked hourly at `:45` during market hours by Cloud
Scheduler; **any check with status `fail` makes the endpoint return HTTP 500**.
Check groups: `endpoint_health`, `trade_execution`, `log_analysis`,
`position_reconciliation`, `performance_baseline` (**KNOWN DEAD — validates
nothing: it queries columns/tables that never existed; do not trust its
"pass". FC-085 owns the fix-or-delete decision**), `risk_parameters`,
`deploy_freshness`.
Since FC-082 (2026-08-27) the monitor's dataset is profile-derived
(`BQ_DATASET` env survives as explicit override only), and
`cc-regression-hourly` runs the same checks against the covered-call service.

`deploy_freshness` (FC-081 follow-up) is the only group that looks *outside*
GCP: it compares the commit the revision was built from (`GIT_COMMIT`, set to
`$COMMIT_SHA` by all three `cloudbuild.yaml` deploy steps and echoed by
`/health`) against `GET /repos/{GITHUB_REPO}/commits/main`. It exists because
the build-failure alert watches builds that *start*, and **a build that never
starts fires no failure alert** — FC-081's repo rename left `main` undeployed
for 16 days in silence. It emits exactly one result per run:

| Condition | status | name / log event |
|---|---|---|
| `main` ahead of the deployed commit by more than `DEPLOY_FRESHNESS_MAX_HOURS` (default 2.0) | `fail` | `deploy_freshness_drift` |
| GitHub **redirect** (301/302/307/308) — the repo was RENAMED or moved | `fail` | `deploy_freshness_repo_unreachable` |
| GitHub 404 — repo deleted, made private, or access lost | `fail` | `deploy_freshness_repo_unreachable` |
| `GIT_COMMIT` unset (pre-rollout, or a manual `gcloud builds submit`) | `warn` | `deploy_freshness_no_commit` |
| `GITHUB_REPO` is not a valid `owner/name` pair | `warn` | `deploy_freshness_unconfigured` |
| GitHub 401/403/429/5xx, timeout, malformed JSON, bad `sha`/date | `warn` | `deploy_freshness_github_error` |
| SHAs match, or mismatch younger than the window (build in flight) | `pass` | `deploy_freshness` |

**A rename is a REDIRECT, not a 404 — and that is the whole detector.** GitHub
answers a renamed repo with 301 plus the new name in `Location`, and keeps
doing so indefinitely; `requests` follows redirects by default, so the obvious
implementation of this check gets a cheerful 200 back from the renamed repo and
reports `pass` on the exact condition it exists to catch. `_github_get` passes
`allow_redirects=False` for that reason alone. Meanwhile the Cloud Build
trigger, which matches the *old literal name* and has no forwarding, is the
half that has actually stopped working — so the fix repoints **both**.

The two `fail` rows are emitted through `log_error_event` (which sets
`event_type` — `log_system_event` does not, FC-047) and are what
`deploy/monitoring/deploy_freshness_alert_policy.json` matches on. Everything
else is a `warn` on purpose: `fail` returns HTTP 500 from `/regression`, so a
GitHub outage must never trip it.

**No `warn` is silent.** Every degraded path also emits a
`deploy_freshness_degraded` log event carrying a `reason`
(`no_commit`, `unauthenticated`, `bad_repo`, `bad_window`, `http_error`,
`request_failed`, `bad_json`, `bad_sha`, `bad_date`, `future_commit_date`),
watched by `deploy/monitoring/deploy_freshness_degraded_alert_policy.json` at a
**24h notification rate limit — a once-a-day nag, not a page**. A detective
control that degrades quietly is indistinguishable from one that is working,
which is the same silence FC-081 was, in a different colour.

**The token is optional.** The repo is public, so an empty `GITHUB_TOKEN` makes
the request unauthenticated rather than terminal: the check still returns a
real verdict, sets `details.authenticated=false`, and nags with reason
`unauthenticated`. The token buys the 5000/hr authenticated rate limit instead
of the 60/hr per-IP unauthenticated pool (a 429 surfaces as `http_error`).

Four operational facts that decide whether an alert — or a silence — means
anything:

- **Cadence: six runs a day, weekdays only.** The check rides the existing
  `:45`, 10:45–15:45 ET schedulers. Nothing runs overnight or at the weekend, so
  **a trigger that dies on a Friday evening is first reported at 10:45 ET
  Monday**. Silence outside market hours is not health.
- **The clock is the commit's committer date, not the push time.** A change
  committed locally and pushed more than two hours later is already past the
  window when it lands, so it pages once even though the build ran promptly.
  This is accepted, not overlooked: the alternative is looking up the Cloud
  Build record for the commit, which would add an IAM dependency
  (`cloudbuild.builds.viewer`) on the *runtime* service account purely to soften
  an alert. This repo's workflow pushes immediately, so the case is rare and the
  extra runtime permission is the worse trade.
- **A deliberate traffic pin will alert every run.** While traffic is pinned to
  an older revision (rollback, canary hold), the deployed commit is
  intentionally behind `main` — which is drift by every definition the check
  has. Disable the drift policy for the duration of the pin and re-enable it
  when traffic returns to latest.
- **A manual `gcloud builds submit` supplies no `$COMMIT_SHA`**, so the revision
  comes up with an empty `GIT_COMMIT` and reports `deploy_freshness_no_commit` —
  a degraded warn on the *nag* policy, never drift on the paging one.

`position_reconciliation`'s **`reconcile_orphaned`** check (a short call with
no covering shares) classifies with `strict_option_type` and roots with
`parse_option_symbol` since **FC-079**. It used to ask `"C" in symbol` over the
whole OCC symbol, which reads a *put* on any root containing a C as a call and
warns it as an orphan — a short put holds no shares by design, so that is a
false alarm by construction. No root in either configured universe contains a C
today (the `'P'` roots — AAPL, SPY, PFE — are what broke the *reconcile
counting loop*, a separate site), so the defect was latent rather than live.
Option symbols that are not strict OCC contracts are neither passed nor warned
on there: they are listed in the check's `unclassifiable` detail field, because
`risk_unclassifiable_option` below is already their alarm and a second one for
the same fact is how an alarm layer gets muted.

`check_risk_parameters` was synced to the real policy set by FC-069 S1 — four
checks that mirrored deleted knobs (global position count, cash reserve,
portfolio allocation, $40k per-ticker exposure) were removed, because an alarm
layer that mirrors a policy nothing enforces cries wolf and gets muted. What
survives:

| Check | What it verifies |
|---|---|
| `risk_duplicate_underlying` | the one-position-per-underlying invariant (fail → 500); inherits the open-order blindness |
| `risk_max_position_size` | positions ≤ `max_position_size`, **re-sourced from `Config`** and `STRATEGY_CONFIG`-aware, so it cannot drift from policy or validate the wrong profile |
| `risk_naked_call` | no short call without covering shares (fail → 500) |
| `risk_cost_basis_protection` | no call strike below basis, re-specced onto `avg_entry_price` — it used to derive basis from `cost_basis / qty`, which returns 0 for assigned positions, leaving it blind on exactly the lots it exists for |
| `risk_unclassifiable_option` | **warn** — an option symbol that is not a strict OCC contract (adjusted roots after a split). The two checks above exclude such positions; excluding them silently would have inverted the point |

If you change a policy, change its mirror here in the same PR.

## Config discipline

**A settings key with no live consumer is a defect.** This repo's recurring
failure mode is a knob that reads as live and gates nothing — FC-069 deleted
roughly 35 of them in one sweep, across two strata, plus whole blocks
(`monitoring.*`, `logging.*`, `gap_risk_controls.*`) that configured nothing at
all.

The authoritative key-by-key census (every leaf key in `config/settings.yaml`
against its verified consumer) is the **appendix of `docs/plans/fc-069.md`**.
Two things to know before you use it:

- **Any new key must land with its consumer named** — in the code review, and
  in the census if you are touching it. A key without a consumer is born a
  corpse.
- **The census predates `config/covered_call.yaml`** (FC-075 Phase 1, merged
  2026-08-03). Two profiles now exist, and the census's "78 leaf keys" headline
  covers only `settings.yaml`. A knob deleted from one profile must be deleted
  from both — S1 found six swept keys mirrored in the covered-call profile.

## Wheel Strategy Symmetry Principle

**CRITICAL DEVELOPMENT RULE**: the wheel has two phases that must be treated
symmetrically:

1. **Put selling phase** — entry via `find_suitable_puts()` and `put_seller.py`
2. **Call selling phase** — position management via `find_suitable_calls()` and
   `call_seller.py`

**When making changes to one side (puts OR calls), ALWAYS consider the
equivalent change on the other:**
- Logging enhancements → both legs
- Filtering improvements → both legs
- Error handling → both legs
- Performance metrics → both legs

**Why**: the wheel is a complete lifecycle (sell put → assignment → sell call →
called away → repeat). Both phases need equal observability and consistent
logging for effective debugging.

**Symmetry is a prompt, not a law.** Where the two legs genuinely differ, the
difference must be *stated and justified*, not quietly introduced — the earnings
gate is the model: puts use a symbol-level N=2 window, calls use a per-candidate
span test, and `docs/gates.md` records exactly why. Call deltas differ from put
deltas for the same kind of reason.

**Key files**:
- Filtering: `src/api/market_data.py` (`find_suitable_puts` /
  `find_suitable_calls`, `_check_put_criteria_detailed` /
  `_check_call_criteria_detailed`)
- Scanning: `src/data/options_scanner.py`
  (`scan_for_put_opportunities` / `scan_for_call_opportunities`)
- Execution: `src/strategy/put_seller.py` and `src/strategy/call_seller.py`

## Data Analysis Policy

**IMPORTANT: Cloud-First Data Analysis**
- For ALL analysis of *what the bot actually did*, use data on Google Cloud
  Platform — not local files or caches.
- Primary sources:
  - BigQuery `options_wheel.trades_from_activities` — real fills, the source of
    truth for realized behavior
  - BigQuery `options_wheel.backtest_runs` — screening results (see
    `docs/bigquery/backtest_runs.md`)
  - Google Cloud Storage: `gs://gen-lang-client-0607444019-options-data/`
  - Google Cloud Storage: `gs://options-wheel-chain-lake/chains/v1/` — the
    **chain lake** (FC-060 Layer 1): the point-in-time option chains the
    backtest engine replays, one parquet per `<UNDERLYING>/<YYYY-MM-DD>`,
    mirrored write-through from `ChainStore`. It is *input data for
    simulations*, never a record of what the bot did — see the backtest
    exception below. Objects are only ever replaced by a file covering a
    **wider** request and are never deleted by code; the vendor may not serve
    these chains again, so treat the bucket as append-mostly history.
  - Cloud Run dashboard endpoints
- This ensures analysis reflects production-ready, persistent, centralized data.

**Reading `strategy_id` (FC-075 Seam 4).** Every row written by the five BigQuery
writers — `trades`, `errors`, `executions`, `decision_events`,
`trades_from_activities`, `equity_history_from_alpaca`,
`stock_history_from_alpaca` — carries a `strategy_id` naming the profile that
wrote it. The column is NULLABLE and was **not backfilled**: rows written before
Seam 4 deployed have `strategy_id IS NULL`, and every one of them is a wheel row.
Any query that segments by strategy must therefore read
`IFNULL(strategy_id, 'wheel')`, never bare `strategy_id` — a bare
`WHERE strategy_id = 'wheel'` silently drops the entire pre-Seam-4 history.
Each strategy also has its own dataset (`options_wheel`, `covered_call`), so
within one dataset the column is a cross-check, not the primary filter.

**Backtests are the exception, and the distinction matters.** The old
`/backtest`, `/backtest/results`, `/backtest/history` and `/cache/*` endpoints
were **deleted in FC-032** — the engine behind them had never produced a single
trade and one component emitted fabricated numbers. Do not reference them.

The rebuilt engine (FC-032) runs **locally by design**: it replays live strategy
code over historical Alpaca data and needs no cloud round-trip.

```bash
python main.py --command backtest --symbol NVDA --start 2025-10-01 --end 2026-07-01
python main.py --command screen            # whole universe -> options_wheel.backtest_runs
python main.py --command sweep --scenarios examples/scenarios_example.yaml \
    --symbols AAPL,AMZN,GOOGL,IWM,NVDA,UNH --start 2025-08-01 --end 2026-07-31
```

**Scenario sweeps are local and are NEVER persisted (FC-060 Layer 2).**
`--command sweep` replays many config variants over many symbols and writes
markdown/JSON to disk only (a cold window does still populate the shared chain
cache and, when `CHAIN_LAKE_BUCKET` is set, the chain lake — that is vendor data,
not results). It must stay that way while `backtest_runs` is the
production screen's table: the documented "current demotion candidates" query
takes the latest `run_kind='full'` row, so a persisted full-universe sweep would
displace a real screen with a hypothetical. A sweep report is a hypothesis, not a
record of the universe. Overrides are restricted to a **selection-only
allowlist** — a key that changes what the cached chain must contain, or that the
replay never reads, is refused with the reason. **Without `--holdout-start` the
report is labelled IN-SAMPLE ONLY**: a ranking chosen on the window it was
measured on is a hypothesis, not a result. See `docs/BACKTEST_ENGINE.md`
§"Scenario sweeps"; FC-060 Layer 3 owns a store for sweep results that cannot
displace the screen.

**`docs/BACKTEST_ENGINE.md` is the single home of every measured backtest
figure. Read it before quoting any backtest number, and quote numbers from
there, not from here** — fidelity percentages, per-symbol tradability counts and
coverage figures all drift with re-measurement and config changes, and a number
copied into a second document is a number that will be stale in a month.

Three things this file *does* pin, because they are boundaries rather than
measurements — a `backtest_runs` row is not comparable across any of them:

- `engine_version = 'fc-032-phase-5'` — the dead engine path.
- `engine_version = 'fc-068-prod-pipeline'` — the production pipeline replay.
- `engine_version = 'fc-069-scanner-rewire'` — the scanner rewire plus a
  rejection-vocabulary change (2026-08-04).
- Plus a **timestamp-only** boundary: rows before **2026-07-29** describe a
  **put-only** engine (FC-048 — every backtest before that misrouted covered
  calls to the put seller).

Screening results *are* persisted to BigQuery and are cloud-first like
everything else. A local `--command backtest` run is a simulation, never
evidence of what the bot did — for that, always go to
`trades_from_activities`.

**Read backtest output with its stated biases.** Every report carries a
known-bias footer; read it, do not assume its contents. In particular, dividends
and ex-dividend early assignment **are modeled** (FC-042 Track C) — an older
version of this file said they were not, which understated the engine. The
footer states the real remaining caveats, including that dividend coverage
depends on the committed table covering the run window and that early assignment
for non-dividend reasons is still unmodeled.

## Dashboard & Backend Cascading Impact Analysis

**CRITICAL: Before making changes to the dashboard or backend, ALWAYS perform a cascading impact analysis.**

The dashboard architecture has multiple interconnected layers. Changes to one layer can break functionality in others. Always trace data flow end-to-end:

### Data Flow Architecture
```
Trading Bot (Cloud Run) → Cloud Logging → BigQuery Views → Dashboard Backend → Dashboard Frontend
```

### Pre-Change Checklist

Before modifying ANY dashboard or backend component, analyze:

1. **Data Source Changes (BigQuery Views/Tables)**
   - Which backend queries use this data?
   - What fields does the frontend expect?
   - Are there aggregations that depend on specific field values?

2. **Backend API Changes (FastAPI endpoints)**
   - Which frontend hooks consume this endpoint?
   - What TypeScript types need updating?
   - Are there caching considerations?

3. **Frontend Component Changes**
   - What API data does this component expect?
   - Are there shared components (e.g., RecentTrades used in multiple places)?
   - Does the status/display logic match backend data format?

4. **Logging Event Changes (logging_events.py)**
   - Which BigQuery views depend on this event_type?
   - Will existing queries still work?
   - Do frontend status mappings need updating?

### Key Files by Layer

| Layer | Key Files | Impact When Changed |
|-------|-----------|---------------------|
| Bot Logging | `src/utils/logging_events.py` | BigQuery views, dashboard status displays |
| BigQuery | `dashboard/backend/services/bigquery.py` | All dashboard endpoints |
| Backend API | `dashboard/backend/routers/*.py` | Frontend hooks, data types |
| Frontend Hooks | `dashboard/frontend/src/hooks/useApi.ts` | All consuming components |
| Frontend Components | `dashboard/frontend/src/components/*.tsx` | UI display, user experience |
| Frontend Pages | `dashboard/frontend/src/pages/*.tsx` | Feature functionality |

### Example: Order Status Display

Order status (filled / expired / assigned) is **not** derived from bot logs. It
comes from Alpaca's account-activities feed, which is authoritative and
idempotent. Trace a change through these five layers:

1. **Ingest Layer**: `src/data/activities_ingestor.py` pulls Alpaca activities
   (`FILL`, `OPASN`, `OPEXP`, ...) and dedupes on `activity_id`
2. **BigQuery Layer**: rows land in `options_wheel.trades_from_activities`;
   `trades_with_outcomes` joins them into per-position outcomes
3. **Backend Layer**: `dashboard/backend/services/bigquery.py` queries those
   views — confirm any new field is selected and returned
4. **Frontend Hooks**: add the field to the TypeScript interface in
   `dashboard/frontend/src/hooks/useApi.ts`
5. **Frontend Components**: update the status mapping that renders it
6. **Verify**: premium calculations and other aggregates are unaffected

**Do not add a bot-side order-status poller.** One existed
(`poll_order_statuses`) and was deleted in FC-035. It *ran* on every `/run`
cycle for ~4 months, but produced nothing — 0 events and 0 rows across ~490
invocations — because it only ever inspected open orders, which are never in
a final state. Nothing read its output, and it would have been a second,
non-idempotent writer of facts the activities feed already carries. See
`docs/plans/fc-035.md`.

**Do not write "completed cycle" rows from the bot either.** The
`options_wheel.wheel_cycles` writer was deleted and the table dropped in FC-069
S2: every row it ever wrote had `capital_gain = 0` (it read a state field that
never resolved) and most were cold-start duplicates. The dashboard reads the
`wheel_cycles_from_activities` view, which derives cycles from the activities
feed. Note the separate `options_wheel_logs.wheel_cycles` **view** still has
NULL `strike_price` / `premium` from every feeder — pre-existing rot, not a
regression; do not treat those columns as data.

### Verification Steps

After any change:
1. Check that existing features still work (premium tracking, trade history, etc.)
2. Verify TypeScript compiles without errors (`npx tsc --noEmit` — a required
   step, not a nicety)
3. Test the full data flow from bot to dashboard display
4. Confirm metrics and aggregations are unaffected by display-only changes

## Deploy / CI — `cloudbuild.yaml`

One Cloud Build trigger on `main` builds two images and runs three canary chains
— `options-wheel-strategy`, `covered-call-engine`, `options-wheel-dashboard` —
each `deploy → smoke-test → promote`. **Since FC-084 builds of that trigger are
serialized, and each build deploys a revision it named itself.**

**`serialize-builds` gates the deploys** (`waitFor: ['push-bot-image']`). It must
be **listed after** `push-bot-image`, not first: Cloud Build resolves `waitFor`
only against ids defined earlier in the list and rejects the config outright
otherwise, and a step with no `waitFor` implicitly waits on everything listed
before it — so listing the gate first also made `run-tests` wait on it, a cycle.
It reads this build's `createTime`, `startTime` and `buildTriggerId` from
`gcloud builds describe $BUILD_ID`, then polls `gcloud builds list` for
PENDING/QUEUED/WORKING builds of the same trigger:

- **A newer build exists** → write `/workspace/superseded`, log `SUPERSEDED`,
  **exit 0**. Newer code wins. This is not a failure and must not trip the
  build-failure alert, so the exit code stays 0 — grep the logs for the literal
  `SUPERSEDED` to count them.
- **An older build is still running** → wait (20 s poll; the cap is
  `startTime + BUILD_TIMEOUT_SECONDS − 90`, i.e. ~23.5 min at the current 1500 s
  timeout — see the timing budget below — then fail loudly).
- **Alone and newest** → proceed.

Cloud Build has no conditional steps, so the marker is the mechanism: **every
deploy/smoke/promote step begins with**
`[ -f /workspace/superseded ] && { echo "skip: superseded"; exit 0; }`.
`run-tests` / `build-*` / `push-*` are deliberately *not* gated — a superseded
build still validates and pushes its image, which is what makes rollback-by-SHA
possible.

**Timing budget.** Measured single-build runtime is **max 798 s, p95 662 s**
(queue time sits on top and does not count against the build timeout: ≤110 s).
When one build waits for an older one:

```
waiting build total = wait for the older build to finish (≤798 s)
                    + this build's own deploy chain (~360 s)
                    ≈ 1158 s
```

Under the old 1200 s timeout that left **~42 s** of headroom, so `timeout:` is
raised to **1500 s** (~342 s of headroom). Both adversarial reviews called the
original "don't touch 1200 s" a plan defect. The gate derives its own deadline
from that number — `BUILD_TIMEOUT_SECONDS` in `serialize-builds`, asserted equal
to `timeout:` by a contract test — and gives up **90 s early** so its explanation
of *which* build it was waiting for is what the log shows, instead of an opaque
`TIMEOUT`. Note the placement of the gate does not change this total: the wait
ends when the older build ends regardless of when the gate started polling.

**Which service account this runs as — and the IAM grant it needs.** Trigger
builds run as the **trigger's** service account,
`799970961417-compute@developer.gserviceaccount.com`. The `serviceAccount:` field
at the bottom of `cloudbuild.yaml` is **ignored for trigger builds** (it names
`799970961417@cloudbuild.gserviceaccount.com`, which no trigger build has ever
used — all 372 historical builds ran as the compute account); it is kept only for
a manual `gcloud builds submit`. The gate needs `cloudbuild.builds.get`/`.list`,
and a live probe build (`3d4db40d`, 2026-08-28) proved the compute account **does
not have them** — `gcloud builds list` inside the build fails with
`PERMISSION_DENIED … authenticated as 799970961417-compute@…`. Grant it
`roles/cloudbuild.builds.viewer` ("Cloud Build Viewer"). **`gcloud projects
add-iam-policy-binding` will not work** — the Cloud Resource Manager API is
disabled on this project — so use the console IAM page:
<https://console.cloud.google.com/iam-admin/iam?project=gen-lang-client-0607444019>.
Verify with `gcloud builds submit --no-source --config=<one-step yaml running
gcloud builds list>`; it should print a build id rather than PERMISSION_DENIED.

**The alternative that was considered and deferred: ordering by git ancestry.**
The gate could ask the public GitHub API whether this build's commit is an
ancestor of the branch head instead of asking Cloud Build which builds exist —
no Cloud Build IAM dependency at all, and "newer" would mean *newer commit*
rather than *newer build*, which is arguably the more honest definition. It was
not taken because the Cloud Build oracle is already built and twice-reviewed
while the grant is a one-time click, and because it changes the rollback story:
re-running an older commit's trigger would no longer win, so rolling back would
have to be `git revert` (a forward commit) rather than a trigger re-run. Worth
revisiting if the IAM grant proves hard to get or if trigger re-runs turn out not
to carry a `buildTriggerId`.

**Revision identity comes from `--revision-suffix=<shortsha>-<buildid8>`**, so
`<service>-<shortsha>-<buildid8>` is known *before* the deploy runs; the deploy
then confirms it exists with `revisions describe` and writes it to
`/workspace/rev-<service>.txt`, and the smoke test polls exactly that revision
(reading the condition whose `type == "Ready"` — Ready-first ordering in
`status.conditions` is observed, not contractual). **Never go back to reading the
revision out of the deploy's own output.** `status.latestCreatedRevisionName` is
re-read after gcloud's 100–190 s Ready wait and returns *another* build's
revision when writes overlap — proven live on 2026-08-27, when build `046fd075`
printed the revision that build `0d9756c0` created. gcloud's own source calls the
field "slightly racy".

**Promote is `update-traffic --to-latest`, and must stay that way.** Pinning
traffic with `--to-revisions=REV=100` permanently removes `latestRevision: true`
from the service's traffic spec; Cloud Run then keeps that split for every later
revision, so a `gcloud run services update --update-env-vars ROLLER_ENABLED=false`
creates a revision serving **0%** and the kill switch silently stops working.
Every lever in §Development Notes (`EARNINGS_ENABLED`, `ROLLER_ENABLED`,
`ROLLER_DRY_RUN`) depends on this. `--to-latest` is safe here because
serialization means "latest" is this build's revision, and each promote asserts
`status.latestReadyRevisionName == REV` before shifting traffic.

**Kill switch applied mid-build.** Between a chain's `--no-traffic` deploy and its
promote there is a window in which an operator `gcloud run services update
--update-env-vars ROLLER_ENABLED=false` creates a new 0%-traffic revision. A
promote that refused on any mismatch would leave the service pinned to the **old**
revision with the kill switch **not applied** — the opposite of what the operator
asked for. So the promote compares images: if the latest ready revision runs *this
build's image*, it is that env-only change and gets promoted
(`PROMOTING latest ready <rev> (env-only change on this build's image)`). If it
runs a **different** image, the promote fails loudly, naming the serving revision,
the latest ready revision and the remedy
(`gcloud run services update-traffic <svc> --to-latest`) — promoting would ship
code this build never tested. The whole check re-runs on every retry attempt;
`--to-latest` is never retried blind. Two things to know regardless: a kill switch
applied mid-build **is** carried forward by that build, and **any** kill switch is
wiped by the next `--set-env-vars` deploy, because `--set-env-vars` replaces the
entire env set.

**Rollback semantics.** Re-running an older commit's trigger creates the *newest
build*, so it wins the gate and deploys the older code on purpose; a forward
build racing it is superseded. Ordering is by build `createTime`, never by git
ancestry — the Cloud Build checkout is shallow, and "newest build wins" is what
makes a deliberate rollback work. Read `createTime` from `--format=json`
(`2026-08-28T16:43:45.221420Z`) and **parse it to a datetime** — the gate does.
Protobuf JSON emits 0, 3, 6 or 9 fractional digits, so widths differ between
builds and lexicographic order is wrong across them (`…:45Z` is *earlier* than
`…:45.221420Z` but sorts *after* it). Do **not** use
`--format='value(createTime)'` either: it renders `2026-08-28T16:43:45+00:00`
and drops the fraction that separates two builds pushed in the same second.

Deploy and promote both retry 3× / 20 s on Cloud Run's optimistic-concurrency
error (`Conflict for resource ...: version 'X' was specified but current version
is 'Y'`). With serialization that can only come from an out-of-band write — an
operator `services update` during a build. A retried deploy reuses the same
suffix, so `already exists` is treated as success.

**Operational consequences worth knowing before you need them.**

- **A rollback re-run leaves `main` ahead of production.** Re-running an older
  commit's trigger wins the gate and deploys older code on purpose — but `main`
  still points at the newer commit, so `deploy_freshness_drift` pages on **both**
  services after 2 h until `main` is reverted to match. Either revert `main` or
  expect the page.
- **A SUPERSEDED commit is never retried.** If the superseding build then fails or
  is cancelled, the older *good* commit stays undeployed and its build is green —
  nothing retries it. Recovery is to push a fix forward or re-run the older
  commit's trigger. The 2 h weekday-hours freshness check is the backstop that
  makes this visible rather than silent.
- **A `promote-bot` failure strands the chain unevenly.** `deploy-cc-canary`
  waits on `promote-bot`, so the covered-call engine never deploys that build —
  while the dashboard chain, which does not, promotes normally. That leaves three
  services on two different commits until the next successful build.
- **Fast rollback recipe:** `gcloud run services update <svc> --image=<image at
  the good SHA>`. It creates a new revision, keeps `latestRevision: true`, and
  serves in about a minute. **Never** roll back with `--to-revisions` — that
  strips `latestRevision: true` and breaks the kill switches permanently.
- **A superseded build still runs its nine deploy/smoke/promote steps as no-ops**,
  each pulling the cloud-sdk image before exiting. That is a few seconds and a few
  image pulls per superseded build — accepted, in exchange for the marker being
  the only conditional mechanism Cloud Build offers.

`tests/test_cloudbuild_contract.py` pins all of the above plus the
must-not-change list — step ids, `waitFor` edges, and **every** `gcloud run
deploy` flag per service — against a frozen fixture, and unit-tests the gate's
decision logic directly. That suite is step 1 of the build itself, so a
regression fails before anything deploys.

## Development Notes

**Alpaca Setup**: requires options trading approval and the paper trading endpoint
**Testing**: comprehensive suite in `/tests` (1262 tests as of 2026-08-04)
**Logging**: structured logging with structlog for trade audit trails
**Configuration**: YAML settings with environment-variable substitution;
`STRATEGY_CONFIG` selects the profile
**Env levers that bypass a deploy**: `EARNINGS_ENABLED`, `ROLLER_ENABLED`,
`ROLLER_DRY_RUN`. Use `gcloud run services update --update-env-vars`, **never**
`--set-env-vars` — the latter wipes the entire env set.
